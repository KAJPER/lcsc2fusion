"""Palette controller: all add-in logic that does not need Fusion (no `adsk` import here).

The palette (HTML/JS) talks to Python through `adsk.fusionSendData(action, json)` and receives
messages through `palette.sendInfoToHTML(action, json)`. This module owns that protocol.

Palette to Python actions (`INCOMING_ACTIONS`), payload is a JSON object:
    ready                          the page finished loading
    search       {query}           LCSC number (C2040) or free text phrase
    add          {lcsc_id, with_3d, fusion}
                                   download the part and write its .lbr (does not open it);
                                   `with_3d` is an optional bool (default false) that also
                                   writes the STEP files next to the .lbr; `fusion` is an
                                   optional bool (default false): once the part is written,
                                   hand it to the accumulating "LCSC2Fusion Library" in Fusion
    add_to_fusion {lcsc_id}        the same hand-off for a part added earlier in this session
                                   (auto hand-off off, or a failed one that is retried)
    open_folder  {path}            show the folder holding a saved library
    open_library {path}            ask Fusion to open a saved .lbr as a separate library
                                   (the fallback; answers with a status)
    check_update {force}           look for a newer release on GitHub now; `force` is an optional
                                   bool (default false). Without it the check runs only when the
                                   automatic schedule says it is due; with it (the "Check now"
                                   button) the 24 hour limit is skipped, but two forced checks
                                   less than 60 s apart are answered from the cache
    dismiss_update {tag}           hide the "new version" banner for this release tag ("v1.2.3")
                                   until a newer release appears
    set_update_check {enabled}     turn the automatic once a day check on or off (required bool)

Python to palette actions (`OUTGOING_ACTIONS`):
    status   {level: info|ok|warn|error, text}
    results  {query, items: [SearchResult as dict plus lcsc_url]}
                                   `lcsc_url` is the product page on lcsc.com ("" when the id
                                   is not a canonical C number); the page links to it
    added    {lcsc_id, path, mpn, warnings: [str], opened: false, instructions: str,
              step_path: str|null, placed_step_path: str|null, with_3d: bool}
    busy     {value: bool}
    error    {message}
    fusion_done {lcsc_id, ok, created_library, merged, already_present, three_d, library_name,
                 message, warnings: [str]}
                                   the end of one hand-off (always sent, also on failure);
                                   `three_d` is none|started|failed|skipped. Progress of the
                                   hand-off arrives as `status` messages in between.
    update   {state, current, latest, release_url, installer_url, checked_at, enabled, dismissed}
                                   what is known about newer releases. `state` is one of
                                   available|current|error|disabled|checking. `current` is the
                                   running version ("0.1.0"). `latest` is the newest stable release
                                   ("0.2.0") or null when nothing is known. `release_url` and
                                   `installer_url` are built by the add-in from the validated
                                   version (never copied from GitHub); `installer_url` is null
                                   unless the release lists the installer file. `checked_at` is
                                   the epoch time of the last attempt, or null. `enabled` is the
                                   once a day check. `dismissed` is true for "available" when the
                                   user hid this version. The message carries codes only: the page
                                   words everything (see update_service.py for the state rules).
                                   Sent on `ready` from the cache (no network), around every
                                   check ("checking", then the result) and after dismiss_update
                                   and set_update_check. The check runs in the background: it does
                                   not set `busy`, so it never makes a search or an add fail.

Everything the controller depends on is a Protocol, so tests use plain fakes and the Fusion
specific implementations live in `fusion_adapter.py`. All methods run on Fusion's main thread;
only the callables handed to `JobRunner.run` run on a worker and must not touch Fusion.

Opening a library in Fusion Electronics uses an undocumented text command. RESEARCH 3.3 confirms
only palette `incomingFromHTML` handlers as a safe context for such commands, so `add` never opens
anything: the user presses a button and `open_library` runs inside that handler.

The hand-off to the accumulating library (`LibraryAdder`) takes up to a minute of Fusion's main
thread, far longer than the 30 s HTTP dispatch timeout. So the request that asks for it returns at
once and the work is queued with `defer`, which runs it later on the main thread. While it is
queued or running the controller counts as busy, so no other request starts.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar, cast

from fusion_library_flow import LIBRARY_NAME, FlowRequest, FlowResult
from update_check import CheckResult, parse_tag
from update_service import UpdateView

from lcsc2fusion import __version__
from lcsc2fusion.errors import LcscError
from lcsc2fusion.links import lcsc_product_url
from lcsc2fusion.model import SearchResult

ACTION_READY = "ready"
ACTION_SEARCH = "search"
ACTION_ADD = "add"
ACTION_OPEN_FOLDER = "open_folder"
ACTION_OPEN_LIBRARY = "open_library"
ACTION_ADD_TO_FUSION = "add_to_fusion"
ACTION_CHECK_UPDATE = "check_update"
ACTION_DISMISS_UPDATE = "dismiss_update"
ACTION_SET_UPDATE_CHECK = "set_update_check"
# Not sent by our page: on the Qt browser Fusion answers every `sendInfoToHTML` with this event,
# data being the return value of the page's fusionJavaScriptHandler. It is only an acknowledgement.
ACTION_RESPONSE = "response"

ACTION_STATUS = "status"
ACTION_RESULTS = "results"
ACTION_ADDED = "added"
ACTION_BUSY = "busy"
ACTION_ERROR = "error"
ACTION_FUSION_DONE = "fusion_done"
ACTION_UPDATE = "update"

# Payload fields, checked against app.js and mock-bridge.js by the tests.
RESULT_FIELDS = (
    "lcsc_id",
    "mpn",
    "manufacturer",
    "package",
    "description",
    "datasheet_url",
    "image_url",
    "library_type",
    "stock",
    "lcsc_url",
)
ADD_REQUEST_FIELDS = ("lcsc_id", "with_3d", "fusion")
ADD_TO_FUSION_REQUEST_FIELDS = ("lcsc_id",)
CHECK_UPDATE_REQUEST_FIELDS = ("force",)
DISMISS_UPDATE_REQUEST_FIELDS = ("tag",)
SET_UPDATE_CHECK_REQUEST_FIELDS = ("enabled",)
ADDED_FIELDS = (
    "lcsc_id",
    "path",
    "mpn",
    "warnings",
    "opened",
    "instructions",
    "step_path",
    "placed_step_path",
    "with_3d",
)
FUSION_DONE_FIELDS = (
    "lcsc_id",
    "ok",
    "created_library",
    "merged",
    "already_present",
    "three_d",
    "library_name",
    "message",
    "warnings",
)
UPDATE_FIELDS = (
    "state",
    "current",
    "latest",
    "release_url",
    "installer_url",
    "checked_at",
    "enabled",
    "dismissed",
)
UPDATE_STATES = ("available", "current", "error", "disabled", "checking")

INCOMING_ACTIONS = (
    ACTION_READY,
    ACTION_SEARCH,
    ACTION_ADD,
    ACTION_OPEN_FOLDER,
    ACTION_OPEN_LIBRARY,
    ACTION_ADD_TO_FUSION,
    ACTION_CHECK_UPDATE,
    ACTION_DISMISS_UPDATE,
    ACTION_SET_UPDATE_CHECK,
)
OUTGOING_ACTIONS = (
    ACTION_STATUS,
    ACTION_RESULTS,
    ACTION_ADDED,
    ACTION_BUSY,
    ACTION_ERROR,
    ACTION_FUSION_DONE,
    ACTION_UPDATE,
)

# The button captions, also used in the texts below and checked against app.js by the tests.
BUTTON_ADD_TO_FUSION = "Add to Fusion library"
BUTTON_OPEN_SEPARATE = "Open as a separate library"

REPLY_OK = "OK"
REPLY_ERROR = "ERROR"
REPLY_BUSY = "BUSY"

SEARCH_LIMIT = 20
MAX_QUERY_LENGTH = 100
_LCSC_ID_RE = re.compile(r"C[0-9]{1,12}")

GENERIC_ERROR_TEXT = (
    "Unexpected error. The details were written to the Fusion Text Commands window."
)
MANUAL_STEPS = (
    "File > Open > Open from my computer, select the .lbr file, then File > Save into a "
    "project so it appears in the Library Manager."
)
FUSION_UNAVAILABLE_TEXT = (
    "Adding to the Fusion library is not available in this session. The library file was "
    f'saved; use "{BUTTON_OPEN_SEPARATE}" instead.'
)

T = TypeVar("T")


class Level(StrEnum):
    INFO = "info"
    OK = "ok"
    WARN = "warn"
    ERROR = "error"


@dataclass(frozen=True)
class AddedPart:
    """Result of `PartService.add_part`."""

    path: Path
    mpn: str
    warnings: tuple[str, ...] = ()
    step_path: Path | None = None  # raw STEP, only when 3D was requested and produced
    placed_step_path: Path | None = None  # STEP transformed into the package frame


@dataclass(frozen=True)
class _AddedRecord:
    """A part added in this session, kept so that `add_to_fusion` can hand it over later."""

    part: AddedPart
    with_3d: bool


@dataclass(frozen=True)
class ImportOutcome:
    """Result of `LibraryImporter.open_library`. `opened` means Fusion accepted the request."""

    opened: bool
    message: str = ""


@dataclass(frozen=True)
class JobResult(Generic[T]):
    """Outcome of a background job: either `value` or `error`."""

    value: T | None = None
    error: Exception | None = None

    def unwrap(self) -> T:
        if self.error is not None:
            raise self.error
        return cast(T, self.value)


class LibraryAdder(Protocol):
    """Adds a converted part to the accumulating Fusion library (`FusionLibraryFlow` in the add-in).

    `add` blocks for the whole Fusion sequence and must run on Fusion's main thread. It reports
    progress through `status(level, text)` and does not raise; the controller guards anyway.
    """

    def add(self, request: FlowRequest, status: Callable[[str, str], None]) -> FlowResult: ...


class PartService(Protocol):
    def search(self, query: str, limit: int) -> list[SearchResult]: ...

    def add_part(self, lcsc_id: str, out_dir: Path, with_3d: bool = False) -> AddedPart: ...


class LibraryImporter(Protocol):
    def open_library(self, path: Path) -> ImportOutcome: ...


class PaletteSink(Protocol):
    def send(self, action: str, data: str) -> None: ...


class UpdateCheckService(Protocol):
    """The update check as the controller uses it (`UpdateService` in update_service.py).

    All methods run on the main thread except `fetch_latest`, which is the job handed to the
    `JobRunner`. `begin_check` says whether a check must start now (and marks it as running);
    `finish_check` or `abort_check` must follow, whatever happened to the job.
    """

    def view(self) -> UpdateView: ...

    def begin_check(self, force: bool) -> bool: ...

    def fetch_latest(self) -> CheckResult: ...

    def finish_check(self, result: CheckResult) -> UpdateView: ...

    def abort_check(self) -> UpdateView: ...

    def dismiss(self, tag: str) -> UpdateView: ...

    def set_enabled(self, enabled: bool) -> UpdateView: ...


class JobRunner(Protocol):
    def run(self, fn: Callable[[], T], on_done: Callable[[JobResult[T]], None]) -> None:
        """Run `fn` off the main thread, then call `on_done(result)` on the main thread."""
        ...


Logger = Callable[[str], None]
FolderOpener = Callable[[Path], None]
# Queues a callable to run later on Fusion's main thread and returns at once.
Defer = Callable[[Callable[[], None]], None]


class _BadRequest(Exception):
    """The palette sent something invalid. The message is shown to the user."""


class _Busy(Exception):
    """A job is already running."""


def default_out_dir() -> Path:
    return Path.home() / "LCSC2Fusion" / "libraries"


def search_result_to_dict(result: SearchResult) -> dict[str, Any]:
    """JSON friendly form of a search hit: the dataclass fields plus `lcsc_url`.

    `lcsc_url` is the product page on lcsc.com, or "" when the id is not a canonical C number
    (the page then shows the id as plain text). The page validates it again before linking.
    """
    data = dataclasses.asdict(result)
    try:
        data["lcsc_url"] = lcsc_product_url(result.lcsc_id)
    except ValueError:
        data["lcsc_url"] = ""
    return data


def build_instructions(fusion_library: bool = True) -> str:
    """Text shown with a saved library. The manual steps are always included.

    `fusion_library` tells whether the hand-off to the accumulating library is available.
    """
    steps = (
        "then use File > Save in the editor and pick a project so the library appears in the "
        f"Library Manager. If that does not work, do it manually: {MANUAL_STEPS}"
    )
    if not fusion_library:
        return f'Press "{BUTTON_OPEN_SEPARATE}", {steps}'
    return (
        f'Press "{BUTTON_ADD_TO_FUSION}" to merge the part into your {LIBRARY_NAME}. '
        f'To use just this part on its own, press "{BUTTON_OPEN_SEPARATE}", {steps}'
    )


def describe_open_outcome(outcome: ImportOutcome, name: str) -> tuple[Level, str]:
    """Status level and text for the result of asking Fusion to open `name`."""
    if outcome.opened:
        return Level.OK, (
            f"Sent {name} to Fusion for opening. In the editor use File > Save and pick a "
            "project so it appears in the Library Manager."
        )
    reason = f" ({outcome.message})" if outcome.message else ""
    return Level.WARN, f"Fusion did not open {name}{reason}. Open it manually: {MANUAL_STEPS}"


def open_folder_in_os(path: Path) -> None:
    """Show `path` in the system file manager. Raises OSError when it cannot be started."""
    if sys.platform == "win32":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


class Controller:
    """Handles palette events. One background job or Fusion hand-off at a time.

    `library` and `defer` switch the hand-off to the accumulating library on; without both,
    `fusion` requests are answered with a warning and everything else works as before.
    """

    def __init__(
        self,
        service: PartService,
        importer: LibraryImporter,
        sink: PaletteSink,
        runner: JobRunner,
        log: Logger,
        *,
        out_dir: Path | None = None,
        folder_opener: FolderOpener = open_folder_in_os,
        library: LibraryAdder | None = None,
        defer: Defer | None = None,
        update_service: UpdateCheckService | None = None,
    ) -> None:
        self._service = service
        self._importer = importer
        self._sink = sink
        self._runner = runner
        self._log = log
        self._out_dir = (out_dir if out_dir is not None else default_out_dir()).expanduser()
        self._folder_opener = folder_opener
        self._library = library
        self._defer = defer
        self._updates = update_service
        self._busy = False  # a background job (search, add) is running
        self._fusion_pending = False  # a hand-off is queued or running (cleared in `finally`)
        self._added: dict[str, _AddedRecord] = {}
        self._handlers: dict[str, Callable[[dict[str, Any]], None]] = {
            ACTION_READY: self._on_ready,
            ACTION_SEARCH: self._on_search,
            ACTION_ADD: self._on_add,
            ACTION_OPEN_FOLDER: self._on_open_folder,
            ACTION_OPEN_LIBRARY: self._on_open_library,
            ACTION_ADD_TO_FUSION: self._on_add_to_fusion,
            ACTION_CHECK_UPDATE: self._on_check_update,
            ACTION_DISMISS_UPDATE: self._on_dismiss_update,
            ACTION_SET_UPDATE_CHECK: self._on_set_update_check,
        }

    @property
    def out_dir(self) -> Path:
        return self._out_dir

    @property
    def busy(self) -> bool:
        """True while a background job runs or a Fusion hand-off is queued or running."""
        return self._busy or self._fusion_pending

    @property
    def fusion_available(self) -> bool:
        return self._library is not None and self._defer is not None

    # Entry point ---------------------------------------------------------------------------

    def handle(self, action: str, data: str) -> str:
        """Process one palette event. Never raises; returns the reply string for the page."""
        if action == ACTION_RESPONSE:
            # Must come before payload parsing (the data is "OK", not JSON). Answering it with an
            # error message would make Fusion acknowledge that message too, and so on forever.
            if data not in ("", REPLY_OK):
                self._log(f"The palette answered a message with {data!r}.")
            return REPLY_OK
        try:
            payload = _parse_payload(data)
            handler = self._handlers.get(action)
            if handler is None:
                raise _BadRequest(f"Unknown palette action {action!r}.")
            handler(payload)
            return REPLY_OK
        except _Busy:
            self._status(Level.WARN, "Another request is still running. Please wait for it.")
            return REPLY_BUSY
        except _BadRequest as exc:
            self._send(ACTION_ERROR, {"message": str(exc)})
            return REPLY_ERROR
        except Exception as exc:  # boundary: nothing may escape into Fusion's event loop
            self._report_exception(exc)
            return REPLY_ERROR

    # Palette actions -----------------------------------------------------------------------

    def _on_ready(self, payload: dict[str, Any]) -> None:
        self._send(ACTION_BUSY, {"value": self.busy})
        self._status(Level.INFO, f"Ready. Libraries are saved to {self._out_dir}")
        if self._updates is not None:  # nothing to say about updates without the service
            try:
                self._send_update(self._updates.view())  # from the cache, no network
                self._start_update_check(force=False, answer=False)
            except Exception:  # boundary: the update notice is optional, never an error box
                self._log("The update notice failed:\n" + traceback.format_exc())

    def _on_search(self, payload: dict[str, Any]) -> None:
        query = _require_query(payload)
        self._start_job(
            f"Searching for {query!r}...",
            lambda: self._service.search(query, SEARCH_LIMIT),
            lambda items: self._finish_search(query, items),
        )

    def _on_add(self, payload: dict[str, Any]) -> None:
        lcsc_id = _require_lcsc_id(payload)
        with_3d = _optional_bool(payload, "with_3d")
        fusion = _optional_bool(payload, "fusion")
        out_dir = self._out_dir

        def work() -> AddedPart:  # runs on the worker thread: no Fusion calls here
            out_dir.mkdir(parents=True, exist_ok=True)
            return self._service.add_part(lcsc_id, out_dir, with_3d=with_3d)

        what = "the library and the 3D model" if with_3d else "the library"
        self._start_job(
            f"Downloading {lcsc_id} and building {what}...",
            work,
            lambda part: self._finish_add(lcsc_id, part, with_3d, fusion),
        )

    def _on_add_to_fusion(self, payload: dict[str, Any]) -> None:
        """Hand a part added earlier in this session to the accumulating library."""
        lcsc_id = _require_lcsc_id(payload)
        request = self._fusion_request(lcsc_id)  # unknown id or foreign path: _BadRequest
        if self.busy:
            raise _Busy
        self._queue_fusion(request)

    def _on_open_folder(self, payload: dict[str, Any]) -> None:
        raw = payload.get("path")
        if not isinstance(raw, str) or not raw.strip():
            raise _BadRequest("No folder given.")
        target = Path(raw)
        folder = target if target.is_dir() else target.parent
        if not self._inside_out_dir(folder):
            raise _BadRequest("Refusing to open a folder outside the library output directory.")
        if not folder.is_dir():
            raise _BadRequest(f"Folder does not exist: {folder}")
        try:
            self._folder_opener(folder)
        except OSError as exc:
            raise _BadRequest(f"Could not open the folder: {exc}") from exc

    def _on_open_library(self, payload: dict[str, Any]) -> None:
        """Runs inside the palette's incomingFromHTML handler, the only context RESEARCH 3.3
        confirms for Electronics text commands. Needs no job, so it works while one is busy."""
        raw = payload.get("path")
        if not isinstance(raw, str) or not raw.strip():
            raise _BadRequest("No library file given.")
        path = Path(raw)
        if not self._inside_out_dir(path):
            raise _BadRequest("Refusing to open a file outside the library output directory.")
        if path.suffix.lower() != ".lbr" or not path.is_file():
            raise _BadRequest(f"Library file does not exist: {path}")
        level, text = describe_open_outcome(self._open_in_fusion(path), path.name)
        self._status(level, text)

    # Update notice -------------------------------------------------------------------------
    #
    # The check is NOT a job in the sense of `_start_job`: it does not set `busy`, so it can never
    # make a search or an add fail with BUSY, and a search that is running does not delay it. The
    # runner starts a worker per call, so both can be in flight at once.

    def _on_check_update(self, payload: dict[str, Any]) -> None:
        self._start_update_check(force=_optional_bool(payload, "force"), answer=True)

    def _on_dismiss_update(self, payload: dict[str, Any]) -> None:
        tag = payload.get("tag")
        if not isinstance(tag, str) or parse_tag(tag) is None:
            raise _BadRequest("Invalid request: tag must be a release tag such as v1.2.3.")
        service = self._updates
        self._send_update(service.dismiss(tag) if service is not None else _unavailable_update())

    def _on_set_update_check(self, payload: dict[str, Any]) -> None:
        enabled = _required_bool(payload, "enabled")
        service = self._updates
        if service is None:
            self._send_update(_unavailable_update())
            return
        self._send_update(service.set_enabled(enabled))
        if enabled:  # switching it on checks at once when a check is due
            self._start_update_check(force=False, answer=False)

    def _start_update_check(self, *, force: bool, answer: bool) -> None:
        """Start the lookup in the background when it is due (or forced).

        `answer` says the page asked for it: then exactly one `update` message comes back even when
        nothing starts. Otherwise (automatic) nothing is sent when nothing starts.
        """
        service = self._updates
        if service is None:
            if answer:
                self._send_update(_unavailable_update())
            return
        if not service.begin_check(force):
            if answer:
                self._send_update(service.view())
            return
        try:
            self._send_update(service.view())  # "checking"
            self._runner.run(service.fetch_latest, self._finish_update_check)
        except Exception:  # boundary: the check must be marked as over, whatever failed
            self._log("Could not start the update check:\n" + traceback.format_exc())
            self._send_update_guarded(service.abort_check)

    def _finish_update_check(self, result: JobResult[CheckResult]) -> None:
        """Runs on the main thread when the worker is done. Never raises."""
        service = self._updates
        if service is None:
            return
        try:
            view = service.finish_check(result.unwrap())
        except Exception:  # boundary: runs from a Fusion event, must not escape
            self._log("The update check failed:\n" + traceback.format_exc())
            self._send_update_guarded(service.abort_check)
            return
        self._send_guarded(ACTION_UPDATE, view.to_message())

    def _send_update_guarded(self, make_view: Callable[[], UpdateView]) -> None:
        try:
            self._send_guarded(ACTION_UPDATE, make_view().to_message())
        except Exception:  # boundary: see above
            self._log("Could not report the update check:\n" + traceback.format_exc())

    def _send_update(self, view: UpdateView) -> None:
        self._send(ACTION_UPDATE, view.to_message())

    def _inside_out_dir(self, path: Path) -> bool:
        return path.resolve().is_relative_to(self._out_dir.resolve())

    # Jobs ----------------------------------------------------------------------------------

    def _start_job(
        self, description: str, fn: Callable[[], T], on_success: Callable[[T], None]
    ) -> None:
        if self.busy:  # a job, or a Fusion hand-off that is queued or running
            raise _Busy
        self._set_busy(True)
        self._status(Level.INFO, description)
        try:
            self._runner.run(fn, lambda result: self._complete(result, on_success))
        except Exception:
            self._set_busy(False)  # the job never started, do not leave the UI locked
            raise

    def _complete(self, result: JobResult[T], on_success: Callable[[T], None]) -> None:
        try:
            on_success(result.unwrap())
        except Exception as exc:  # boundary: runs from a Fusion event, must not escape
            self._report_exception(exc)
        finally:
            self._set_busy(False)

    def _finish_search(self, query: str, items: list[SearchResult]) -> None:
        self._send(
            ACTION_RESULTS,
            {"query": query, "items": [search_result_to_dict(item) for item in items]},
        )
        if items:
            self._status(Level.OK, f"Found {len(items)} part(s) for {query!r}.")
        else:
            self._status(
                Level.WARN,
                f"No parts found for {query!r}. Try an LCSC number such as C2040 or fewer words.",
            )

    def _finish_add(self, lcsc_id: str, part: AddedPart, with_3d: bool, fusion: bool) -> None:
        self._added[lcsc_id] = _AddedRecord(part, with_3d)
        self._send(
            ACTION_ADDED,
            {
                "lcsc_id": lcsc_id,
                "path": str(part.path),
                "mpn": part.mpn,
                "warnings": list(part.warnings),
                "opened": False,  # opening is a separate, user triggered action
                "instructions": build_instructions(self.fusion_available),
                "step_path": _path_or_none(part.step_path),
                "placed_step_path": _path_or_none(part.placed_step_path),
                "with_3d": with_3d,
            },
        )
        label = f"{lcsc_id} ({part.mpn})" if part.mpn else lcsc_id
        has_model = part.step_path is not None or part.placed_step_path is not None
        if with_3d and not has_model:
            self._status(
                Level.WARN,
                f"Saved {label} without a 3D model: none was produced for this part. "
                "See the warnings on the result.",
            )
        elif part.warnings:
            self._status(Level.WARN, f"Saved {label} with {len(part.warnings)} warning(s).")
        elif with_3d:
            self._status(Level.OK, f"Saved {label} and its 3D model to {part.path.parent}")
        else:
            self._status(Level.OK, f"Saved {label} to {part.path}")
        if fusion:
            self._hand_off_after_add(lcsc_id)

    def _hand_off_after_add(self, lcsc_id: str) -> None:
        """The `fusion` flag of `add`: queue the hand-off, or tell the page why there is none."""
        try:
            self._queue_fusion(self._fusion_request(lcsc_id))
        except _BadRequest as exc:
            self._fusion_refused(lcsc_id, str(exc))
        except _Busy:  # cannot happen (the add job itself held the busy flag), but never lose it
            self._fusion_refused(lcsc_id, "Another Fusion request is still running.")

    # Fusion hand-off -------------------------------------------------------------------------

    def _fusion_request(self, lcsc_id: str) -> FlowRequest:
        """The flow request for a part added in this session. Raises `_BadRequest`."""
        record = self._added.get(lcsc_id)
        if record is None:
            raise _BadRequest(
                f"{lcsc_id} was not added in this session. Press Add to library for it first."
            )
        lbr_path = record.part.path
        placed = record.part.placed_step_path if record.with_3d else None
        for path in (lbr_path, placed):
            if path is not None and not self._inside_out_dir(path):
                raise _BadRequest("Refusing to use a file outside the library output directory.")
        return FlowRequest(
            lcsc_id=lcsc_id, mpn=record.part.mpn, lbr_path=lbr_path, placed_step_path=placed
        )

    def _queue_fusion(self, request: FlowRequest) -> None:
        """Queue the Fusion work on the main thread and return at once.

        Raises `_Busy` when anything else is pending. Without `library` and `defer` the page is
        told that the feature is off (a warning, not an error).
        """
        library, defer = self._library, self._defer
        if library is None or defer is None:
            self._fusion_refused(request.lcsc_id, FUSION_UNAVAILABLE_TEXT)
            return
        if self._fusion_pending:
            raise _Busy
        self._set_fusion_pending(True)
        try:
            defer(lambda: self._run_fusion(library, request))
        except Exception:  # boundary: the queue is a Fusion plug-in point
            self._log(f"Could not queue the Fusion request:\n{traceback.format_exc()}")
            self._set_fusion_pending(False)
            self._fusion_refused(
                request.lcsc_id, "Could not start the Fusion request. See the Text Commands window."
            )

    def _run_fusion(self, library: LibraryAdder, request: FlowRequest) -> None:
        """Runs on the main thread, queued by `_queue_fusion`. Blocks for the whole sequence.

        Always ends with `fusion_done` and a cleared busy flag, whatever happens inside.
        """
        lcsc_id = request.lcsc_id
        try:
            self._send_guarded(ACTION_BUSY, {"value": True})
            sending = f"Sending {lcsc_id} to Fusion..."
            self._send_guarded(ACTION_STATUS, {"level": Level.INFO.value, "text": sending})
            result = library.add(request, self._forward_flow_status)
            self._send(ACTION_FUSION_DONE, _fusion_done_payload(lcsc_id, result))
        except Exception:  # boundary: runs from a Fusion event, must not escape
            self._log(f"Adding {lcsc_id} to the Fusion library failed:\n{traceback.format_exc()}")
            self._fusion_refused(lcsc_id, GENERIC_ERROR_TEXT, Level.ERROR)
        finally:
            self._fusion_pending = False
            self._send_guarded(ACTION_BUSY, {"value": self.busy})

    def _forward_flow_status(self, level: str, text: str) -> None:
        """Progress of the flow becomes a `status` message. A failing sink never stops the flow."""
        try:
            known = Level(level)
        except ValueError:
            known = Level.INFO
        self._send_guarded(ACTION_STATUS, {"level": known.value, "text": text})

    def _fusion_refused(self, lcsc_id: str, message: str, level: Level = Level.WARN) -> None:
        """The hand-off did not happen: a status line plus a failed `fusion_done` for the card."""
        self._send_guarded(ACTION_STATUS, {"level": level.value, "text": message})
        payload = _fusion_done_payload(lcsc_id, _failed_result(message))
        self._send_guarded(ACTION_FUSION_DONE, payload)

    def _open_in_fusion(self, path: Path) -> ImportOutcome:
        try:
            return self._importer.open_library(path)
        except Exception as exc:  # importer is a Fusion plug-in point, report instead of raising
            self._log(f"open_library failed:\n{traceback.format_exc()}")
            return ImportOutcome(opened=False, message=str(exc) or type(exc).__name__)

    # Output --------------------------------------------------------------------------------

    def _set_busy(self, value: bool) -> None:
        """Set the job flag. The page hears about it only when the overall state changes, so a
        job that hands over to a queued Fusion hand-off never flashes idle in between."""
        was_busy = self.busy
        self._busy = value
        if self.busy != was_busy:
            self._send_busy()

    def _set_fusion_pending(self, value: bool) -> None:
        was_busy = self.busy
        self._fusion_pending = value
        if self.busy != was_busy:
            self._send_busy()

    def _send_busy(self) -> None:
        self._send(ACTION_BUSY, {"value": self.busy})

    def _send_guarded(self, action: str, data: dict[str, Any]) -> None:
        """Send from a place where a failing sink must not abort the Fusion bookkeeping."""
        try:
            self._send(action, data)
        except Exception:  # boundary: the sink may be gone, the controller state is kept anyway
            self._log(f"Could not send {action!r} to the palette:\n{traceback.format_exc()}")

    def _status(self, level: Level, text: str) -> None:
        self._send(ACTION_STATUS, {"level": level.value, "text": text})

    def _send(self, action: str, data: dict[str, Any]) -> None:
        self._sink.send(action, json.dumps(data, ensure_ascii=True))

    def _report_exception(self, exc: BaseException) -> None:
        """Show a failure to the user: library errors verbatim, anything else generically."""
        if isinstance(exc, LcscError):
            self._log(f"{type(exc).__name__}: {exc}")
            message = str(exc)
        elif isinstance(exc, OSError):
            self._log(f"{type(exc).__name__}: {exc}")
            message = f"File system error: {exc}"
        else:
            self._log("".join(traceback.format_exception(exc)))
            message = GENERIC_ERROR_TEXT
        self._send(ACTION_ERROR, {"message": message})


def _parse_payload(data: str) -> dict[str, Any]:
    if not data or not data.strip():
        return {}
    try:
        value = json.loads(data)
    except ValueError as exc:
        raise _BadRequest("Malformed request from the palette (invalid JSON).") from exc
    if not isinstance(value, dict):
        raise _BadRequest("Malformed request from the palette (expected a JSON object).")
    return value


def _require_query(payload: dict[str, Any]) -> str:
    raw = payload.get("query")
    if not isinstance(raw, str):
        raise _BadRequest("Enter an LCSC part number (for example C2040) or a search phrase.")
    query = " ".join(raw.split())
    if not query:
        raise _BadRequest("Enter an LCSC part number (for example C2040) or a search phrase.")
    if len(query) > MAX_QUERY_LENGTH:
        raise _BadRequest(f"The search text is too long (limit {MAX_QUERY_LENGTH} characters).")
    if not query.isprintable():
        raise _BadRequest("The search text contains unsupported characters.")
    candidate = query.upper()
    return candidate if _LCSC_ID_RE.fullmatch(candidate) else query


def _optional_bool(payload: dict[str, Any], key: str) -> bool:
    """Absent means False; anything present must be a real JSON boolean (0, 1, "true" are not)."""
    if key not in payload:
        return False
    value = payload[key]
    if not isinstance(value, bool):
        raise _BadRequest(f"Invalid request: {key} must be true or false.")
    return value


def _required_bool(payload: dict[str, Any], key: str) -> bool:
    """Like `_optional_bool`, but a missing key is an error: absent must never mean False here."""
    value = payload.get(key)
    if not isinstance(value, bool):
        raise _BadRequest(f"Invalid request: {key} must be true or false.")
    return value


def _unavailable_update() -> UpdateView:
    """The `update` message of a session without the update check (the old palette mode)."""
    return UpdateView(
        state="disabled",
        current=__version__,
        latest=None,
        release_url=None,
        installer_url=None,
        checked_at=None,
        enabled=False,
        dismissed=False,
    )


def _failed_result(message: str) -> FlowResult:
    """What the page hears when the hand-off could not even start."""
    return FlowResult(
        ok=False,
        created_library=False,
        merged=False,
        already_present=False,
        three_d="none",
        library_name=LIBRARY_NAME,
        message=message,
    )


def _fusion_done_payload(lcsc_id: str, result: FlowResult) -> dict[str, Any]:
    """The `fusion_done` message (field names are `FUSION_DONE_FIELDS`)."""
    return {
        "lcsc_id": lcsc_id,
        "ok": bool(result.ok),
        "created_library": bool(result.created_library),
        "merged": bool(result.merged),
        "already_present": bool(result.already_present),
        "three_d": str(result.three_d),
        "library_name": str(result.library_name),
        "message": str(result.message),
        "warnings": [str(text) for text in result.warnings],
    }


def _path_or_none(path: Path | None) -> str | None:
    return None if path is None else str(path)


def _require_lcsc_id(payload: dict[str, Any]) -> str:
    raw = payload.get("lcsc_id")
    candidate = raw.strip().upper() if isinstance(raw, str) else ""
    if not _LCSC_ID_RE.fullmatch(candidate):
        raise _BadRequest(
            f"{raw!r} is not a valid LCSC part number. Use the letter C followed by digits, "
            "for example C2040."
        )
    return candidate
