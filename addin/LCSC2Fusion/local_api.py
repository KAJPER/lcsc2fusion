"""Local API for the MCP server (another process): job tracking and discovery, no `adsk` here.

The add-in already has everything needed to add a part: the controller runs the `add` action on
Fusion's main thread and reports progress as messages. This module lets a second client use that
without a browser window:

* `TeeSink` sits between the controller and the window's sink. Every message still reaches the
  window first, then `JobTracker.observe` follows it, so the browser window keeps working as before.
* `JobTracker` turns the message stream of the one running `add` into a job document that the
  MCP client polls (`LocalApi.job`).
* `LocalApi` validates a request, starts a job and hands `add` to the controller through the same
  dispatch function the window uses (`http_transport` serves it under `/api/local/`).
* `DiscoveryPublisher` writes the endpoint and the secret token to `api.json` (shared format in
  `lcsc2fusion.discovery`) while the add-in runs, and removes it again.

Threads: messages arrive on Fusion's main thread, HTTP handlers run on request threads, so the
tracker keeps everything behind one lock and hands out copies. Job document (exact fields, the MCP
client depends on them):

    {"job": int, "lcsc_id": str, "state": "running"|"done"|"failed", "ok": bool|null,
     "status": str, "message": str, "steps": [str],
     "added": {"path", "mpn", "warnings", "step_path", "placed_step_path", "with_3d"}|null,
     "fusion": {"created_library", "merged", "already_present", "three_d", "library_name",
                "message", "warnings"}|null}

Only one job runs at a time, in line with the controller, which refuses a second request.
"""

from __future__ import annotations

import collections
import contextlib
import json
import os
import re
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from lcsc2fusion.discovery import ApiEndpoint, default_api_path, remove_api_file, write_api_file

__all__ = [
    "API_VERSION",
    "DiscoveryPublisher",
    "JobRunningError",
    "JobTracker",
    "LocalApi",
    "Sink",
    "TeeSink",
]

API_VERSION = 1  # of the HTTP protocol below /api/local/, reported by ping
MIN_TOKEN_CHARS = 16

MAX_JOBS = 20  # finished jobs are kept for polling, the oldest ones are forgotten
MAX_STEPS = 50  # status lines kept per job (the newest ones)
MAX_LIST_ITEMS = 50
MAX_TEXT_CHARS = 1000
MAX_PATH_CHARS = 4096
# A job whose controller is idle cannot make progress any more. The longest legitimate gap is the
# dispatch of the `add` itself (30 s), so after this long the job counts as lost (`fail_orphan`).
ORPHAN_AFTER_S = 60.0

STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"

# Controller protocol (see controller.py): kept as plain strings, this module must not import it.
ACTION_ADD = "add"
ACTION_STATUS = "status"
ACTION_ADDED = "added"
ACTION_ERROR = "error"
ACTION_FUSION_DONE = "fusion_done"
REPLY_OK = "OK"
REPLY_BUSY = "BUSY"

_LCSC_ID_RE = re.compile(r"C[0-9]{1,12}")

Log = Callable[[str], None]
Dispatch = Callable[[str, str], str]
Reply = tuple[int, dict[str, Any]]


def _ignore(message: str) -> None:
    pass


class Sink(Protocol):
    """Same shape as `controller.PaletteSink`."""

    def send(self, action: str, data: str) -> None: ...


class JobRunningError(RuntimeError):
    """`JobTracker.start` was called while another job still runs."""


# Parsing helpers -----------------------------------------------------------------------------


def _text(value: object, limit: int = MAX_TEXT_CHARS) -> str:
    if not isinstance(value, str):
        return ""
    return value if len(value) <= limit else value[: limit - 3] + "..."


def _path_or_none(value: object) -> str | None:
    text = _text(value, MAX_PATH_CHARS)
    return text or None


def _text_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_text(item) for item in value[:MAX_LIST_ITEMS] if isinstance(item, str)]


def _added_document(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": _text(doc.get("path"), MAX_PATH_CHARS),
        "mpn": _text(doc.get("mpn")),
        "warnings": _text_list(doc.get("warnings")),
        "step_path": _path_or_none(doc.get("step_path")),
        "placed_step_path": _path_or_none(doc.get("placed_step_path")),
        "with_3d": doc.get("with_3d") is True,
    }


def _fusion_document(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "created_library": doc.get("created_library") is True,
        "merged": doc.get("merged") is True,
        "already_present": doc.get("already_present") is True,
        "three_d": _text(doc.get("three_d")),
        "library_name": _text(doc.get("library_name")),
        "message": _text(doc.get("message")),
        "warnings": _text_list(doc.get("warnings")),
    }


# Job tracker ---------------------------------------------------------------------------------


@dataclass
class _Job:
    job: int
    lcsc_id: str
    with_3d: bool
    fusion: bool
    started: float
    state: str = STATE_RUNNING
    ok: bool | None = None
    status: str = ""
    message: str = ""
    steps: list[str] = field(default_factory=list)
    added: dict[str, Any] | None = None
    fusion_result: dict[str, Any] | None = None

    def document(self) -> dict[str, Any]:
        """A copy that shares nothing with the live job."""
        added = (
            None if self.added is None else {**self.added, "warnings": list(self.added["warnings"])}
        )
        fusion = (
            None
            if self.fusion_result is None
            else {**self.fusion_result, "warnings": list(self.fusion_result["warnings"])}
        )
        return {
            "job": self.job,
            "lcsc_id": self.lcsc_id,
            "state": self.state,
            "ok": self.ok,
            "status": self.status,
            "message": self.message,
            "steps": list(self.steps),
            "added": added,
            "fusion": fusion,
        }


class JobTracker:
    """Follows the controller's messages for the one active job. Thread safe."""

    def __init__(self, log: Log | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self._log = log if log is not None else _ignore
        self._clock = clock
        self._lock = threading.Lock()
        self._jobs: collections.OrderedDict[int, _Job] = collections.OrderedDict()
        self._active: _Job | None = None
        self._next_id = 1
        self._observers: dict[str, Callable[[_Job, dict[str, Any]], None]] = {
            ACTION_STATUS: self._on_status,
            ACTION_ERROR: self._on_error,
            ACTION_ADDED: self._on_added,
            ACTION_FUSION_DONE: self._on_fusion_done,
        }

    # Called by LocalApi (request threads) --------------------------------------------------------

    def start(self, lcsc_id: str, with_3d: bool, fusion: bool) -> int:
        """Register a new running job and return its id. Raises `JobRunningError`."""
        with self._lock:
            if self._active is not None:
                raise JobRunningError(f"job {self._active.job} is still running")
            job = _Job(self._next_id, lcsc_id, with_3d, fusion, self._clock())
            self._next_id += 1
            self._jobs[job.job] = job
            self._active = job
            while len(self._jobs) > MAX_JOBS:
                oldest = next(iter(self._jobs))
                del self._jobs[oldest]
            return job.job

    def cancel(self, job_id: int) -> None:
        """Forget a job that the controller refused (it never ran). Its id is not reused."""
        with self._lock:
            job = self._jobs.pop(job_id, None)
            if job is not None and self._active is job:
                self._active = None

    def snapshot(self, job_id: int) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return None if job is None else job.document()

    def has_active(self) -> bool:
        with self._lock:
            return self._active is not None

    def fail_orphan(self, min_age_s: float, message: str) -> bool:
        """Fail the active job when it is at least `min_age_s` old. True when it did.

        The caller uses this only when the controller reports that it is idle: then no message can
        ever finish the job, and it would block every later request.
        """
        with self._lock:
            job = self._active
            if job is None or self._clock() - job.started < min_age_s:
                return False
            self._finish(job, STATE_FAILED, False, message)
        self._note(f"Local API job {job.job} ({job.lcsc_id}) lost: {message}")
        return True

    # Called by TeeSink (Fusion's main thread) -----------------------------------------------------

    def observe(self, action: str, data: str) -> None:
        """Update the active job from one controller message. Never raises."""
        observer = self._observers.get(action)
        if observer is None:
            return
        finished: _Job | None = None
        try:
            doc = json.loads(data)
            if not isinstance(doc, dict):
                raise ValueError("the message is not a JSON object")
            with self._lock:
                job = self._active
                if job is None:
                    return  # the user clicked in the window: not ours
                observer(job, doc)
                if self._active is None:
                    finished = job
        except Exception as exc:  # boundary: runs inside the controller's send, must not escape
            self._note(
                f"Local API ignored a malformed {action!r} message: {type(exc).__name__}: {exc}"
            )
            return
        if finished is not None:
            self._note(f"Local API job {finished.job} ({finished.lcsc_id}) {finished.state}")

    # Observers (lock held) -----------------------------------------------------------------------

    def _on_status(self, job: _Job, doc: dict[str, Any]) -> None:
        text = _text(doc.get("text"))
        if text:
            job.status = text
            self._add_step(job, text)

    def _on_error(self, job: _Job, doc: dict[str, Any]) -> None:
        message = _text(doc.get("message")) or "The add-in reported an error."
        job.status = message
        self._add_step(job, message)
        self._finish(job, STATE_FAILED, False, message)

    def _on_added(self, job: _Job, doc: dict[str, Any]) -> None:
        if doc.get("lcsc_id") != job.lcsc_id:
            return
        job.added = _added_document(doc)
        if not job.fusion:
            mpn = job.added["mpn"]
            label = f"{job.lcsc_id} ({mpn})" if mpn else job.lcsc_id
            no_model = job.with_3d and not (job.added["step_path"] or job.added["placed_step_path"])
            note = " without a 3D model (none was produced)" if no_model else ""
            self._finish(job, STATE_DONE, True, f"Saved {label}{note} to {job.added['path']}")

    def _on_fusion_done(self, job: _Job, doc: dict[str, Any]) -> None:
        if doc.get("lcsc_id") != job.lcsc_id:
            return
        job.fusion_result = _fusion_document(doc)
        ok = doc.get("ok") is True
        message = job.fusion_result["message"] or (
            "Added to the Fusion library." if ok else "The Fusion hand-off failed."
        )
        self._finish(job, STATE_DONE if ok else STATE_FAILED, ok, message)

    def _add_step(self, job: _Job, text: str) -> None:
        job.steps.append(text)
        del job.steps[:-MAX_STEPS]

    def _finish(self, job: _Job, state: str, ok: bool, message: str) -> None:
        job.state = state
        job.ok = ok
        job.message = message
        if self._active is job:
            self._active = None

    def _note(self, message: str) -> None:
        with contextlib.suppress(Exception):  # a failing logger must not break the controller
            self._log(message)


class TeeSink:
    """Forwards every message to `first` (the window), then lets the tracker look at it."""

    def __init__(self, first: Sink, tracker: JobTracker) -> None:
        self._first = first
        self._tracker = tracker

    def send(self, action: str, data: str) -> None:
        try:
            self._first.send(action, data)
        finally:  # the tracker must see the message even when the window's sink failed
            self._tracker.observe(action, data)


# The API the HTTP layer calls ---------------------------------------------------------------


def _error(kind: str, message: str) -> dict[str, Any]:
    return {"error": kind, "message": message}


class _InvalidRequest(ValueError):
    """The request body is not acceptable. The message goes to the client."""


def _parse_add_request(body: dict[str, Any]) -> tuple[str, bool, bool]:
    """(lcsc_id, with_3d, fusion). Unknown keys are ignored, a wrong type is refused."""
    raw = body.get("lcsc_id")
    lcsc_id = raw.strip().upper() if isinstance(raw, str) else ""
    if _LCSC_ID_RE.fullmatch(lcsc_id) is None:
        raise _InvalidRequest(
            f"{raw!r} is not a valid LCSC part number. Use the letter C followed by digits, "
            "for example C2040."
        )
    flags = []
    for key, default in (("with_3d", False), ("fusion", True)):
        value = body.get(key, default)
        if not isinstance(value, bool):
            raise _InvalidRequest(f"{key} must be true or false.")
        flags.append(value)
    return lcsc_id, flags[0], flags[1]


class LocalApi:
    """The three operations behind `/api/local/`. `http_transport` handles auth and parsing."""

    def __init__(
        self,
        dispatch: Dispatch,
        tracker: JobTracker,
        token: str,
        busy: Callable[[], bool],
        log: Log,
    ) -> None:
        if len(token) < MIN_TOKEN_CHARS:
            raise ValueError(f"the token must have at least {MIN_TOKEN_CHARS} characters")
        self._dispatch = dispatch
        self._tracker = tracker
        self._token = token
        self._busy = busy
        self._log = log

    @property
    def token(self) -> str:
        return self._token

    def ping(self) -> Reply:
        busy = self._busy() or self._tracker.has_active()
        return 200, {"ok": True, "version": API_VERSION, "busy": busy}

    def submit(self, body: dict[str, Any]) -> Reply:
        """Validate, start a job and hand `add` to the controller."""
        try:
            lcsc_id, with_3d, fusion = _parse_add_request(body)
        except _InvalidRequest as exc:
            return 400, _error("bad_request", str(exc))
        if not self._busy():
            self._tracker.fail_orphan(ORPHAN_AFTER_S, "Lost track of the job: Fusion is idle.")
        busy_reply = (
            409,
            _error("busy", "Another request is running in Fusion. Try again when it has finished."),
        )
        if self._busy():
            return busy_reply
        try:
            job = self._tracker.start(lcsc_id, with_3d, fusion)
        except JobRunningError:
            return busy_reply
        request = json.dumps({"lcsc_id": lcsc_id, "with_3d": with_3d, "fusion": fusion})
        try:
            reply = self._dispatch(ACTION_ADD, request)
        except (TimeoutError, RuntimeError) as exc:  # the main thread did not take it in time
            self._tracker.cancel(job)
            self._note(f"Local API request for {lcsc_id} not delivered: {exc}")
            return 503, _error("unavailable", f"Fusion did not accept the request: {exc}")
        except Exception:  # boundary: the controller never raises, so this is a bug
            self._tracker.cancel(job)
            self._note(f"Local API request for {lcsc_id} failed:\n{traceback.format_exc()}")
            return 500, _error("internal", "Unexpected error. See the Fusion Text Commands window.")
        if reply == REPLY_OK:
            self._note(
                f"Local API job {job}: add {lcsc_id} (3D {with_3d}, Fusion library {fusion})"
            )
            return 202, {"job": job}
        snapshot = self._tracker.snapshot(job)
        self._tracker.cancel(job)
        if reply == REPLY_BUSY:
            return busy_reply
        failure = (
            snapshot["message"]
            if snapshot is not None and snapshot["state"] == STATE_FAILED
            else ""
        )
        return 400, _error("bad_request", failure or "The add-in refused the request.")

    def job(self, job_id: int) -> Reply:
        document = self._tracker.snapshot(job_id)
        if document is None:
            return 404, _error("unknown_job", f"There is no job {job_id}.")
        return 200, document

    def _note(self, message: str) -> None:
        with contextlib.suppress(Exception):  # a failing logger must not break a request
            self._log(message)


# Discovery -----------------------------------------------------------------------------------


class DiscoveryPublisher:
    """Writes `api.json` for the MCP server while the add-in runs. Neither method raises."""

    def __init__(
        self, token: str, log: Log, path: Path | None = None, pid: int | None = None
    ) -> None:
        self._token = token
        self._log = log
        self._path = path
        self._pid = pid
        self._written: Path | None = None

    def publish(self, url: str) -> bool:
        """Write the file. False (and one log line) when it could not be written."""
        try:
            path = self._path if self._path is not None else default_api_path()
            pid = self._pid if self._pid is not None else os.getpid()
            write_api_file(path, ApiEndpoint(url=url, token=self._token, pid=pid))
        except (OSError, ValueError) as exc:
            self._note(f"MCP discovery file not written: {type(exc).__name__}: {exc}")
            return False
        self._written = path
        self._note(f"MCP discovery file written: {path}")
        return True

    def withdraw(self) -> None:
        """Remove the file, but only when it still holds our token."""
        path, self._written = self._written, None
        if path is None:
            return
        try:
            remove_api_file(path, self._token)
        except Exception as exc:  # boundary: shutting down must go on
            self._note(f"MCP discovery file not removed: {type(exc).__name__}: {exc}")

    def _note(self, message: str) -> None:
        with contextlib.suppress(Exception):
            self._log(message)
