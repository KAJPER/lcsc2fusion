"""Client for the local API of the running Fusion add-in (stdlib only, no `adsk`).

The add-in listens on a random port of 127.0.0.1 and writes `api.json` (url and token) while it
runs, see `lcsc2fusion.discovery`. This module reads that file and talks to the add-in:

    GET  /api/local/ping          -> {"ok": true, "version": 1, "busy": bool}
    POST /api/local/add           -> 202 {"job": int} | 409 | 400 | 503
    GET  /api/local/job/<n>       -> the job document, or 404

Rules kept here, because the token is a secret:
- the token goes only into the Authorization header of a request to 127.0.0.1 (the url from the
  discovery file is validated again here), and is never logged, returned or put in a message;
- redirects are never followed, every request uses its own connection with explicit timeouts;
- every failure becomes a `FusionError` whose message tells the model what to do next.
"""

from __future__ import annotations

import http.client
import json
import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeGuard
from urllib.parse import urlsplit

from lcsc2fusion import __version__
from lcsc2fusion.discovery import ApiEndpoint, DiscoveryError, default_api_path, read_api_file
from lcsc2fusion.errors import LcscError

_log = logging.getLogger(__name__)

PING_PATH = "/api/local/ping"
ADD_PATH = "/api/local/add"
JOB_PATH = "/api/local/job/{job}"

PING_TIMEOUT_S = 10.0
ADD_TIMEOUT_S = 35.0
JOB_TIMEOUT_S = 10.0
POLL_INTERVAL_S = 1.0
# Failed polls (connection refused or reset) in a row before a running job is given up on.
MAX_POLL_CONNECTION_FAILURES = 3

STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"
JOB_STATES = (STATE_RUNNING, STATE_DONE, STATE_FAILED)
THREE_D_STATES = ("none", "started", "failed", "skipped")

START_HINT = (
    "Start Fusion, open Utilities > Add-Ins > Scripts and Add-Ins (Shift+S), run the LCSC2Fusion "
    "add-in and wait until its window opens."
)

_LOOPBACK_HOST = "127.0.0.1"
_MAX_BODY_BYTES = 1_000_000
_MAX_TEXT_CHARS = 2000
_MAX_LIST_ITEMS = 100
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_LCSC_ID_RE = re.compile(r"C[0-9]{1,12}")

Sleep = Callable[[float], None]
Clock = Callable[[], float]


class FusionError(LcscError):
    """Talking to the Fusion add-in failed. The message says what to do next."""


class FusionNotRunningError(FusionError):
    """No add-in answers: not started, Fusion closed, or a stale api.json."""


class FusionNoAnswerError(FusionError):
    """The add-in accepted the connection but did not answer in time (Fusion is probably busy)."""


class FusionAuthError(FusionError):
    """The add-in refused the token even after re-reading api.json."""


class FusionBusyError(FusionError):
    """The add-in is working on another part (HTTP 409)."""


class FusionRejectedError(FusionError):
    """The add-in refused the request (HTTP 400)."""


class FusionUnavailableError(FusionError):
    """The add-in cannot work right now (HTTP 503), for example Fusion is not ready."""


class FusionProtocolError(FusionError):
    """The add-in answered with something this client does not understand."""


class UnknownJobError(FusionError):
    """The add-in does not know the job (it forgets jobs when it restarts)."""


# Documents


@dataclass(frozen=True)
class Ping:
    busy: bool
    version: int


@dataclass(frozen=True)
class AddedInfo:
    """What the add-in downloaded and wrote (the `added` part of a job)."""

    path: str
    mpn: str
    warnings: tuple[str, ...]
    step_path: str | None
    placed_step_path: str | None
    with_3d: bool


@dataclass(frozen=True)
class FusionOutcome:
    """What happened inside Fusion (the `fusion` part of a job)."""

    created_library: bool
    merged: bool
    already_present: bool
    three_d: str  # "none", "started", "failed" or "skipped"
    library_name: str
    message: str
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class JobInfo:
    job: int
    lcsc_id: str
    state: str  # "running", "done" or "failed"
    ok: bool | None
    status: str
    message: str
    steps: tuple[str, ...]
    added: AddedInfo | None
    fusion: FusionOutcome | None

    @property
    def terminal(self) -> bool:
        return self.state != STATE_RUNNING

    @property
    def succeeded(self) -> bool:
        return self.state == STATE_DONE and self.ok is not False


def new_steps(previous: list[str], current: list[str]) -> list[str]:
    """Lines of `current` that are not in `previous`.

    The add-in keeps at most 50 steps and may drop the oldest, so the old list can be a shifted
    window of the new one; the longest overlap decides where the new lines start.
    """
    if current[: len(previous)] == previous:
        return current[len(previous) :]
    for size in range(min(len(previous), len(current)), 0, -1):
        if previous[-size:] == current[:size]:
            return current[size:]
    return list(current)


# Client


class FusionClient:
    """Talks to the add-in. One instance per tool call is fine; nothing is cached but timeouts."""

    def __init__(
        self,
        api_path: Path | None = None,
        *,
        ping_timeout_s: float = PING_TIMEOUT_S,
        add_timeout_s: float = ADD_TIMEOUT_S,
        job_timeout_s: float = JOB_TIMEOUT_S,
        poll_interval_s: float = POLL_INTERVAL_S,
        sleep: Sleep = time.sleep,
        clock: Clock = time.monotonic,
    ) -> None:
        self._api_path = api_path
        self._ping_timeout_s = ping_timeout_s
        self._add_timeout_s = add_timeout_s
        self._job_timeout_s = job_timeout_s
        self._poll_interval_s = poll_interval_s
        self._sleep = sleep
        self._clock = clock

    # Public operations

    def ping(self) -> Ping:
        """Raises `FusionNotRunningError`, `FusionNoAnswerError`, `FusionAuthError`, ..."""
        status, payload = self._request("GET", PING_PATH, None, self._ping_timeout_s)
        if status != 200:
            raise _unexpected_status(status, payload)
        busy, version = payload.get("busy"), payload.get("version")
        if payload.get("ok") is not True or not isinstance(busy, bool) or not _is_int(version):
            raise FusionProtocolError("The add-in answered the ping with an unexpected document.")
        return Ping(busy=busy, version=int(version))

    def add(self, lcsc_id: str, with_3d: bool) -> int:
        """Ask the add-in to download a part and merge it into the Fusion library; the job id."""
        if _LCSC_ID_RE.fullmatch(lcsc_id) is None:
            raise FusionRejectedError(f"{lcsc_id!r} is not a canonical LCSC part number.")
        body = {"lcsc_id": lcsc_id, "with_3d": bool(with_3d), "fusion": True}
        try:
            status, payload = self._request("POST", ADD_PATH, body, self._add_timeout_s)
        except FusionNoAnswerError as exc:
            raise FusionNoAnswerError(
                f"{exc} The request may still have been accepted: look at Fusion and call "
                "fusion_status before asking again."
            ) from None
        message = _text(payload.get("message"))
        if status == 202:
            job = payload.get("job")
            if not _is_int(job) or int(job) < 0:
                raise FusionProtocolError("The add-in accepted the part but sent no job number.")
            return int(job)
        if status == 409:
            reason = message or "it is working on another request."
            raise FusionBusyError(
                f"The Fusion add-in is busy: {reason} Wait until fusion_status shows busy false, "
                "then try again."
            )
        if status == 400:
            raise FusionRejectedError(
                f"The Fusion add-in rejected the request: {message or 'bad request'}."
            )
        if status == 503:
            raise FusionUnavailableError(
                f"The Fusion add-in cannot add parts right now: {message or 'unavailable'}. "
                "Check that Fusion is idle and signed in to a Hub project, then try again."
            )
        raise _unexpected_status(status, payload)

    def job(self, job: int) -> JobInfo:
        """The current document of a job. Raises `UnknownJobError` for an unknown number."""
        if not _is_int(job) or job < 0:
            raise FusionRejectedError(f"{job!r} is not a job number.")
        status, payload = self._request(
            "GET", JOB_PATH.format(job=int(job)), None, self._job_timeout_s
        )
        if status == 404:
            raise UnknownJobError(
                f"The Fusion add-in does not know job {job}. It forgets jobs when it is restarted."
            )
        if status != 200:
            raise _unexpected_status(status, payload)
        return parse_job(payload)

    def wait_for_job(
        self,
        job: int,
        timeout_s: float,
        on_poll: Callable[[JobInfo | None, float], None] | None = None,
    ) -> JobInfo:
        """Poll until the job is done or failed, or `timeout_s` passed (then the last state).

        `on_poll(info, elapsed_s)` runs after every poll; `info` is None when the add-in did not
        answer in time (Fusion can be unresponsive for about a minute while it works, which is
        not an error). Connection failures in a row end the wait with `FusionNotRunningError`.
        """
        started = self._clock()
        last: JobInfo | None = None
        connection_failures = 0
        while True:
            info: JobInfo | None = None
            try:
                info = self.job(job)
                connection_failures = 0
            except FusionNoAnswerError:
                _log.debug("Poll of job %s timed out, still waiting", job)
            except FusionNotRunningError:
                connection_failures += 1
                if connection_failures >= MAX_POLL_CONNECTION_FAILURES:
                    raise
            elapsed = self._clock() - started
            if info is not None:
                last = info
            if on_poll is not None:
                on_poll(info, elapsed)
            if info is not None and info.terminal:
                return info
            remaining = timeout_s - elapsed
            if remaining <= 0:
                if last is None:
                    raise FusionNoAnswerError(
                        "The Fusion add-in did not answer while waiting for the job. Fusion may "
                        "be busy (it can be unresponsive for about a minute). Check the job later "
                        f"with fusion_job (job {job})."
                    )
                return last
            self._sleep(min(self._poll_interval_s, remaining))

    # HTTP

    def _request(
        self, method: str, path: str, body: Mapping[str, Any] | None, timeout_s: float
    ) -> tuple[int, dict[str, Any]]:
        """One request, with one retry after a 401/403 (the add-in may have been restarted)."""
        for attempt in (1, 2):
            endpoint = self._endpoint()
            status, payload = self._send(endpoint, method, path, body, timeout_s)
            if status not in (401, 403):
                return status, payload
            if attempt == 1:
                _log.debug("The add-in refused the token, re-reading the discovery file")
        raise FusionAuthError(
            "The Fusion add-in refused the access token. Restart the LCSC2Fusion add-in in "
            "Fusion (it writes a new token when it starts), then try again."
        )

    def _endpoint(self) -> ApiEndpoint:
        path = self._api_path if self._api_path is not None else default_api_path()
        try:
            return read_api_file(path)
        except DiscoveryError as exc:
            raise FusionNotRunningError(str(exc)) from exc

    def _send(
        self,
        endpoint: ApiEndpoint,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        timeout_s: float,
    ) -> tuple[int, dict[str, Any]]:
        port = _loopback_port(endpoint.url)
        headers = {
            "Authorization": f"Bearer {endpoint.token}",
            "Accept": "application/json",
            "Connection": "close",
            "User-Agent": f"lcsc2fusion-mcp/{__version__}",
        }
        data: bytes | None = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["X-LCSC"] = "1"
        connection = http.client.HTTPConnection(_LOOPBACK_HOST, port, timeout=timeout_s)
        try:
            connection.request(method, path, body=data, headers=headers)
            response = connection.getresponse()
            raw = response.read(_MAX_BODY_BYTES + 1)
            status = response.status
        except TimeoutError:
            raise FusionNoAnswerError(
                f"The Fusion add-in did not answer within {timeout_s:g} s. Fusion may be busy "
                "(it can be unresponsive for about a minute while it works); try again shortly."
            ) from None
        except ConnectionError:
            raise FusionNotRunningError(
                "The Fusion add-in does not answer (connection refused or lost). Fusion may be "
                f"closed or the add-in stopped, and api.json is stale. {START_HINT}"
            ) from None
        except http.client.HTTPException:
            raise FusionProtocolError("The Fusion add-in sent a malformed HTTP response.") from None
        except OSError as exc:
            raise FusionNotRunningError(
                f"Cannot reach the Fusion add-in ({exc.strerror or type(exc).__name__}). "
                f"{START_HINT}"
            ) from None
        finally:
            connection.close()
        if len(raw) > _MAX_BODY_BYTES:
            raise FusionProtocolError("The Fusion add-in sent an unexpectedly large response.")
        return status, _json_object(raw)


# Parsing helpers


def parse_job(document: Mapping[str, Any]) -> JobInfo:
    """Turn a job document into a `JobInfo`; anything off-contract is a `FusionProtocolError`."""
    job = document.get("job")
    state = document.get("state")
    if not _is_int(job) or state not in JOB_STATES:
        raise FusionProtocolError("The Fusion add-in sent a job document this client cannot read.")
    ok = document.get("ok")
    return JobInfo(
        job=int(job),
        lcsc_id=_text(document.get("lcsc_id")),
        state=str(state),
        ok=ok if isinstance(ok, bool) else None,
        status=_text(document.get("status")),
        message=_text(document.get("message")),
        steps=_text_list(document.get("steps")),
        added=_parse_added(document.get("added")),
        fusion=_parse_fusion(document.get("fusion")),
    )


def _parse_added(value: Any) -> AddedInfo | None:
    if not isinstance(value, dict):
        return None
    return AddedInfo(
        path=_text(value.get("path")),
        mpn=_text(value.get("mpn")),
        warnings=_text_list(value.get("warnings")),
        step_path=_optional_text(value.get("step_path")),
        placed_step_path=_optional_text(value.get("placed_step_path")),
        with_3d=value.get("with_3d") is True,
    )


def _parse_fusion(value: Any) -> FusionOutcome | None:
    if not isinstance(value, dict):
        return None
    three_d = value.get("three_d")
    return FusionOutcome(
        created_library=value.get("created_library") is True,
        merged=value.get("merged") is True,
        already_present=value.get("already_present") is True,
        three_d=three_d if three_d in THREE_D_STATES else "none",
        library_name=_text(value.get("library_name")),
        message=_text(value.get("message")),
        warnings=_text_list(value.get("warnings")),
    )


def _text(value: Any) -> str:
    """A short single line: control characters become spaces, the length is capped."""
    if not isinstance(value, str):
        return ""
    cleaned = _CONTROL_RE.sub(" ", value).strip()
    if len(cleaned) > _MAX_TEXT_CHARS:
        return cleaned[: _MAX_TEXT_CHARS - 3] + "..."
    return cleaned


def _optional_text(value: Any) -> str | None:
    text = _text(value)
    return text or None


def _text_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(_text(item) for item in value[:_MAX_LIST_ITEMS] if isinstance(item, str))


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _json_object(raw: bytes) -> dict[str, Any]:
    """The body as a dict; an empty or non-JSON body is an empty dict (the status says enough)."""
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _unexpected_status(status: int, payload: Mapping[str, Any]) -> FusionError:
    message = _text(payload.get("message")) or _text(payload.get("error"))
    detail = f": {message}" if message else ""
    return FusionProtocolError(f"The Fusion add-in answered with unexpected HTTP {status}{detail}.")


def _loopback_port(url: str) -> int:
    """The port of a plain `http://127.0.0.1:<port>` url. Anything else is refused.

    `read_api_file` already checks the shape; this repeats the check right before the token is
    sent, so a wrong host can never receive it whatever calls this module.
    """
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise _bad_endpoint() from None
    if (
        port is None
        or not 0 < port < 65536
        or parts.scheme != "http"
        or parts.hostname != _LOOPBACK_HOST
        or parts.username is not None
        or parts.password is not None
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        raise _bad_endpoint()
    return port


def _bad_endpoint() -> FusionNotRunningError:
    return FusionNotRunningError(
        "api.json does not point to 127.0.0.1, so it is not used. Restart the LCSC2Fusion add-in."
    )
