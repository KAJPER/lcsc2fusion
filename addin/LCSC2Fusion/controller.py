"""Palette controller: all add-in logic that does not need Fusion (no `adsk` import here).

The palette (HTML/JS) talks to Python through `adsk.fusionSendData(action, json)` and receives
messages through `palette.sendInfoToHTML(action, json)`. This module owns that protocol.

Palette to Python actions (`INCOMING_ACTIONS`), payload is a JSON object:
    ready                          the page finished loading
    search       {query}           LCSC number (C2040) or free text phrase
    add          {lcsc_id}         download the part and write its .lbr (does not open it)
    open_folder  {path}            show the folder holding a saved library
    open_library {path}            ask Fusion to open a saved .lbr (answers with a status)

Python to palette actions (`OUTGOING_ACTIONS`):
    status   {level: info|ok|warn|error, text}
    results  {query, items: [SearchResult as dict]}
    added    {lcsc_id, path, mpn, warnings: [str], opened: false, instructions: str}
    busy     {value: bool}
    error    {message}

Everything the controller depends on is a Protocol, so tests use plain fakes and the Fusion
specific implementations live in `fusion_adapter.py`. All methods run on Fusion's main thread;
only the callables handed to `JobRunner.run` run on a worker and must not touch Fusion.

Opening a library in Fusion Electronics uses an undocumented text command. RESEARCH 3.3 confirms
only palette `incomingFromHTML` handlers as a safe context for such commands, so `add` never opens
anything: the user presses a button and `open_library` runs inside that handler.
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

from lcsc2fusion.errors import LcscError
from lcsc2fusion.model import SearchResult

ACTION_READY = "ready"
ACTION_SEARCH = "search"
ACTION_ADD = "add"
ACTION_OPEN_FOLDER = "open_folder"
ACTION_OPEN_LIBRARY = "open_library"

ACTION_STATUS = "status"
ACTION_RESULTS = "results"
ACTION_ADDED = "added"
ACTION_BUSY = "busy"
ACTION_ERROR = "error"

INCOMING_ACTIONS = (
    ACTION_READY,
    ACTION_SEARCH,
    ACTION_ADD,
    ACTION_OPEN_FOLDER,
    ACTION_OPEN_LIBRARY,
)
OUTGOING_ACTIONS = (ACTION_STATUS, ACTION_RESULTS, ACTION_ADDED, ACTION_BUSY, ACTION_ERROR)

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


class PartService(Protocol):
    def search(self, query: str, limit: int) -> list[SearchResult]: ...

    def add_part(self, lcsc_id: str, out_dir: Path) -> AddedPart: ...


class LibraryImporter(Protocol):
    def open_library(self, path: Path) -> ImportOutcome: ...


class PaletteSink(Protocol):
    def send(self, action: str, data: str) -> None: ...


class JobRunner(Protocol):
    def run(self, fn: Callable[[], T], on_done: Callable[[JobResult[T]], None]) -> None:
        """Run `fn` off the main thread, then call `on_done(result)` on the main thread."""
        ...


Logger = Callable[[str], None]
FolderOpener = Callable[[Path], None]


class _BadRequest(Exception):
    """The palette sent something invalid. The message is shown to the user."""


class _Busy(Exception):
    """A job is already running."""


def default_out_dir() -> Path:
    return Path.home() / "LCSC2Fusion" / "libraries"


def search_result_to_dict(result: SearchResult) -> dict[str, Any]:
    """JSON friendly form of a search hit (same field names as the dataclass)."""
    return dataclasses.asdict(result)


def build_instructions() -> str:
    """Text shown with a saved library. The manual steps are always included."""
    return (
        'Press "Open in Fusion Electronics", then use File > Save in the editor and pick a '
        "project so the library appears in the Library Manager. If that does not work, do it "
        f"manually: {MANUAL_STEPS}"
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
    """Handles palette events. One background job at a time."""

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
    ) -> None:
        self._service = service
        self._importer = importer
        self._sink = sink
        self._runner = runner
        self._log = log
        self._out_dir = (out_dir if out_dir is not None else default_out_dir()).expanduser()
        self._folder_opener = folder_opener
        self._busy = False
        self._handlers: dict[str, Callable[[dict[str, Any]], None]] = {
            ACTION_READY: self._on_ready,
            ACTION_SEARCH: self._on_search,
            ACTION_ADD: self._on_add,
            ACTION_OPEN_FOLDER: self._on_open_folder,
            ACTION_OPEN_LIBRARY: self._on_open_library,
        }

    @property
    def out_dir(self) -> Path:
        return self._out_dir

    @property
    def busy(self) -> bool:
        return self._busy

    # Entry point ---------------------------------------------------------------------------

    def handle(self, action: str, data: str) -> str:
        """Process one palette event. Never raises; returns the reply string for the page."""
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
        self._send(ACTION_BUSY, {"value": self._busy})
        self._status(Level.INFO, f"Ready. Libraries are saved to {self._out_dir}")

    def _on_search(self, payload: dict[str, Any]) -> None:
        query = _require_query(payload)
        self._start_job(
            f"Searching for {query!r}...",
            lambda: self._service.search(query, SEARCH_LIMIT),
            lambda items: self._finish_search(query, items),
        )

    def _on_add(self, payload: dict[str, Any]) -> None:
        lcsc_id = _require_lcsc_id(payload)
        out_dir = self._out_dir

        def work() -> AddedPart:  # runs on the worker thread: no Fusion calls here
            out_dir.mkdir(parents=True, exist_ok=True)
            return self._service.add_part(lcsc_id, out_dir)

        self._start_job(
            f"Downloading {lcsc_id} and building the library...",
            work,
            lambda part: self._finish_add(lcsc_id, part),
        )

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

    def _inside_out_dir(self, path: Path) -> bool:
        return path.resolve().is_relative_to(self._out_dir.resolve())

    # Jobs ----------------------------------------------------------------------------------

    def _start_job(
        self, description: str, fn: Callable[[], T], on_success: Callable[[T], None]
    ) -> None:
        if self._busy:
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

    def _finish_add(self, lcsc_id: str, part: AddedPart) -> None:
        self._send(
            ACTION_ADDED,
            {
                "lcsc_id": lcsc_id,
                "path": str(part.path),
                "mpn": part.mpn,
                "warnings": list(part.warnings),
                "opened": False,  # opening is a separate, user triggered action
                "instructions": build_instructions(),
            },
        )
        label = f"{lcsc_id} ({part.mpn})" if part.mpn else lcsc_id
        if part.warnings:
            self._status(Level.WARN, f"Saved {label} with {len(part.warnings)} warning(s).")
        else:
            self._status(Level.OK, f"Saved {label} to {part.path}")

    def _open_in_fusion(self, path: Path) -> ImportOutcome:
        try:
            return self._importer.open_library(path)
        except Exception as exc:  # importer is a Fusion plug-in point, report instead of raising
            self._log(f"open_library failed:\n{traceback.format_exc()}")
            return ImportOutcome(opened=False, message=str(exc) or type(exc).__name__)

    # Output --------------------------------------------------------------------------------

    def _set_busy(self, value: bool) -> None:
        self._busy = value
        self._send(ACTION_BUSY, {"value": value})

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


def _require_lcsc_id(payload: dict[str, Any]) -> str:
    raw = payload.get("lcsc_id")
    candidate = raw.strip().upper() if isinstance(raw, str) else ""
    if not _LCSC_ID_RE.fullmatch(candidate):
        raise _BadRequest(
            f"{raw!r} is not a valid LCSC part number. Use the letter C followed by digits, "
            "for example C2040."
        )
    return candidate
