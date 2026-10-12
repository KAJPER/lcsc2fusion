"""Add a converted part to the accumulating "LCSC2Fusion Library" (no `adsk` import).

This module is the decision logic only: which Fusion operation comes next, what is recorded in the
registry and what the user is told. Every Fusion operation sits behind `FusionLibraryApi`, which
the add-in implements with `adsk` (`fusion_adapter.py`) and the tests implement with a recording
fake. `FusionLibraryFlow.run` must be called on Fusion's main thread, because that is where every
`FusionLibraryApi` call has to run; it blocks until the flow is over (tens of seconds in real use).

The sequence copies the installed SnapEDA add-in (`SnapEDA.py`), see DECISIONS and the plan:

* first part: the part .lbr itself becomes the library (`create_library_from_part`), nothing is
  merged because the part is already inside;
* later parts: `open_library`, `merge_part` (Electron `Use`, `Copy`, `Use -`), `save_active`;
* optional 3D, after the 2D work succeeded: `create_3d_package`, `import_step`. The user then
  checks the position and finishes in Fusion (Finish, Save, OK).

Rules that keep the registry honest: the registry (`StateStore`) is written only after the
Fusion calls of the 2D step returned without an exception, a part id already in the registry is
never merged again, and a stale registry (its library is gone) is dropped, with a warning, before
a new library is created.

Progress goes to `status(level, text)` with level `info`, `ok`, `warn` or `error`: one `info` line
before every slow step and, as the last call, the result line, whose text equals
`FlowResult.message`. `run` never raises (`Exception` is caught at this boundary; the details go
to `log`).
"""

from __future__ import annotations

import contextlib
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, TypeVar

from library_registry import (
    LibraryState,
    PartLbrError,
    PartLbrInfo,
    PartRecord,
    StateStore,
    forget_library,
    inspect_part_lbr,
    normalize_id,
)

__all__ = [
    "LIBRARY_NAME",
    "FlowRequest",
    "FlowResult",
    "FusionLibraryApi",
    "FusionLibraryFlow",
    "LibraryRef",
    "forget_library",
]

LIBRARY_NAME = "LCSC2Fusion Library"

T = TypeVar("T")

LEVEL_INFO = "info"
LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_ERROR = "error"

# "none": no 3D step was requested. "started": the 3D package editor is open with the model
# imported, the user finishes it. "failed": the 3D step was tried and raised. "skipped": a 3D step
# was requested but not tried (STEP file missing, or the 2D work failed first).
ThreeDOutcome = Literal["none", "started", "failed", "skipped"]

StatusSink = Callable[[str, str], None]
LogSink = Callable[[str], None]


@dataclass(frozen=True)
class LibraryRef:
    """A library document in Fusion's data panel: its `DataFile.id` and its name."""

    file_id: str
    name: str


class FusionLibraryApi(Protocol):
    """Everything the flow needs from Fusion. Every call runs on Fusion's main thread.

    Any exception means the operation failed. The flow reports it and does not retry.
    """

    def find_library(self, file_id: str | None, name: str) -> LibraryRef | None:
        """The stored `file_id` if it is still a valid file, else the file called `name` in the
        root folder of the active project. None when there is no such library."""
        ...

    def create_library_from_part(self, lbr_path: Path, name: str) -> LibraryRef:
        """Open the part .lbr (`Document.newDesignFromLocal`) and `saveAs` it as `name` in the
        root folder of the active project. The new library stays the active document."""
        ...

    def open_library(self, ref: LibraryRef) -> None:
        """`documents.open` the library; it becomes the active document."""
        ...

    def merge_part(self, lbr_path: Path, deviceset: str) -> None:
        """Into the active library: `Use '<lbr>'`, `Copy "<deviceset>.dev@<lbr name without
        .lbr>"`, `Use "-<lbr name without .lbr>"` (three `Electron.run` text commands)."""
        ...

    def save_active(self, description: str) -> None:
        """`document.save(description)` of the active document."""
        ...

    def create_3d_package(self, lbr_path: Path) -> None:
        """`Electron.Create3DPackage "<lbr>"`, then `saveAs` of the new design into the project
        root. The new 3D package design becomes the active document."""
        ...

    def import_step(self, step_path: Path) -> None:
        """Import the STEP into the active design's root component and rotate the occurrence
        like SnapEDA: columns X=(1,0,0), Y=(0,0,1), Z=(0,-1,0), that is (x, y, z) -> (x, -z, y)."""
        ...


@dataclass(frozen=True)
class FlowRequest:
    """One part to add. `lbr_path` is the part's own .lbr written by the converter.

    `placed_step_path` is the STEP already moved into the package frame, or None when the user
    did not ask for the 3D model.
    """

    lcsc_id: str
    mpn: str
    lbr_path: Path
    placed_step_path: Path | None = None


@dataclass(frozen=True)
class FlowResult:
    """Outcome of `FusionLibraryFlow.run`.

    `ok` is true when the part is safely in the saved library (newly added, or there before).
    `created_library` is true when this run made the library, `merged` when the part was merged
    into an existing one (it is true even if saving then failed, `ok` is false in that case),
    `already_present` when the registry already listed the part, so it was not merged again.
    `three_d` is a `ThreeDOutcome`. `message` is the one-line summary for the user and
    `warnings` the notes collected on the way.
    """

    ok: bool
    created_library: bool
    merged: bool
    already_present: bool
    three_d: ThreeDOutcome
    library_name: str
    message: str
    warnings: tuple[str, ...] = ()


class _Failure(Exception):
    """A step failed in a way the user should hear about. `level` is the final status level."""

    def __init__(self, message: str, level: str = LEVEL_ERROR) -> None:
        super().__init__(message)
        self.message = message
        self.level = level


@dataclass
class _Run:
    """Mutable bookkeeping of one `run` call."""

    request: FlowRequest
    created: bool = False
    merged: bool = False
    already_present: bool = False
    three_d: ThreeDOutcome = "none"
    warnings: list[str] = field(default_factory=list)


class FusionLibraryFlow:
    """See the module docstring. One instance can serve many `run` calls, one at a time."""

    def __init__(
        self,
        api: FusionLibraryApi,
        store: StateStore,
        status: StatusSink,
        log: LogSink,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._api = api
        self._store = store
        self._status = status
        self._log = log
        self._clock = clock
        self._running = False

    def run(self, request: FlowRequest) -> FlowResult:
        """Add the part. Never raises."""
        run = _Run(request)
        if self._running:
            return self._conclude(
                run,
                ok=False,
                message="Another library operation is still running. Wait for it to finish.",
                level=LEVEL_WARN,
            )
        self._running = True
        try:
            message = self._execute(run)
        except _Failure as failure:
            return self._conclude(run, ok=False, message=failure.message, level=failure.level)
        except Exception as exc:  # boundary: the caller is a Fusion event handler
            self._log(f"Unexpected error in the library flow:\n{traceback.format_exc()}")
            return self._conclude(
                run,
                ok=False,
                message=f"Unexpected error while adding {_label(request)}: {_describe(exc)}",
                level=LEVEL_ERROR,
            )
        finally:
            self._running = False
        level = LEVEL_WARN if run.warnings else LEVEL_OK
        if run.already_present and run.three_d != "started" and not run.warnings:
            level = LEVEL_INFO
        return self._conclude(run, ok=True, message=message, level=level)

    # The flow -------------------------------------------------------------------------------

    def _execute(self, run: _Run) -> str:
        """Do the work and return the success message; raise `_Failure` when it cannot be done."""
        request = run.request
        lcsc_id = normalize_id(request.lcsc_id)
        if not request.lbr_path.is_file():
            raise _Failure(f"The part library file does not exist: {request.lbr_path}")
        step = request.placed_step_path
        if step is not None and not step.is_file():
            run.three_d = "skipped"
            self._warn(
                run, f"The 3D model file does not exist ({step}), so the 3D step is skipped."
            )
            step = None
        try:
            info = inspect_part_lbr(request.lbr_path)
        except PartLbrError as exc:
            raise _Failure(str(exc)) from exc

        state = self._store.load()
        for text in self._store.warnings:
            self._warn(run, text)

        ref = self._call(
            "look for the library",
            lambda: self._api.find_library(state.library_file_id, LIBRARY_NAME),
        )
        if ref is None and (state.library_file_id is not None or state.parts):
            self._warn(
                run,
                f"The {LIBRARY_NAME} from the registry no longer exists (deleted or moved?). "
                "A new one is created; parts added earlier stay in the old document.",
            )
            state = state.without_contents()

        if ref is not None and state.has_part(lcsc_id):
            run.already_present = True
            action = (
                f"{_label(request)} is already in the {LIBRARY_NAME}, so it was not added again."
            )
        else:
            self._check_names(run, state, info, lcsc_id)
            if ref is None:
                ref = self._create_library(run)
                action = f"Created the {LIBRARY_NAME} with {_label(request)}."
            else:
                self._merge(run, ref, info)
                action = f"Added {_label(request)} to the {LIBRARY_NAME}."
            self._record(run, state, ref, info)

        if step is None:
            return action
        self._three_d(run, step, reopen=ref if run.already_present else None)
        if run.three_d == "started":
            return (
                f"{action} The 3D package editor is open: check the position of the model, "
                "then click Finish, Save, OK."
            )
        return f"{action} The 3D package could not be created, see the warnings."

    def _check_names(self, run: _Run, state: LibraryState, info: PartLbrInfo, lcsc_id: str) -> None:
        """Refuse to overwrite a different footprint, warn about a reused device set name."""
        known = {name.upper(): fingerprint for name, fingerprint in state.packages.items()}
        for name, fingerprint in info.packages.items():
            existing = known.get(name.upper())
            if existing is not None and existing != fingerprint:
                raise _Failure(
                    f"The footprint {name} is already in the {LIBRARY_NAME} with a different "
                    "shape, so merging would overwrite it. Add the part again so that it is "
                    "converted with a new footprint name."
                )
        for other in state.parts.values():
            if (
                normalize_id(other.lcsc_id) != lcsc_id
                and other.deviceset.upper() == info.deviceset.upper()
            ):
                self._warn(
                    run,
                    f"The device set {info.deviceset} is already in the library (part "
                    f"{other.lcsc_id}). Merging replaces it with this part.",
                )

    def _create_library(self, run: _Run) -> LibraryRef:
        self._say(LEVEL_INFO, f"Creating the {LIBRARY_NAME} from {_label(run.request)}...")
        ref = self._call(
            "create the library",
            lambda: self._api.create_library_from_part(run.request.lbr_path, LIBRARY_NAME),
        )
        run.created = True
        return ref

    def _merge(self, run: _Run, ref: LibraryRef, info: PartLbrInfo) -> None:
        request = run.request
        self._say(LEVEL_INFO, f"Opening the {ref.name}...")
        self._call("open the library", lambda: self._api.open_library(ref))
        self._say(LEVEL_INFO, f"Merging {_label(request)} into the library...")
        self._call(
            "merge the part",
            lambda: self._api.merge_part(request.lbr_path, info.deviceset),
            hint=(
                "The library may be open with unsaved changes: close it without saving before "
                "you try again."
            ),
        )
        run.merged = True
        self._say(LEVEL_INFO, "Saving the library...")
        description = f"Added {request.lcsc_id} {request.mpn}".strip()
        try:
            self._api.save_active(description)
        except Exception as exc:  # boundary to Fusion: report, do not record the part
            self._log(f"Saving the library failed:\n{traceback.format_exc()}")
            raise _Failure(
                f"{_label(request)} was merged into the open library but saving it failed "
                f"({_describe(exc)}). Save the library yourself (File > Save). The part is not "
                "recorded, so adding it again merges it again.",
                LEVEL_WARN,
            ) from exc

    def _record(self, run: _Run, state: LibraryState, ref: LibraryRef, info: PartLbrInfo) -> None:
        """Remember the part. A registry that cannot be written is a warning, not a failure."""
        request = run.request
        package, fingerprint = next(iter(info.packages.items()), ("", ""))
        record = PartRecord(
            lcsc_id=normalize_id(request.lcsc_id),
            deviceset=info.deviceset,
            package=package,
            fingerprint=fingerprint,
            added_at=self._clock(),
        )
        updated = state.with_library(ref.file_id, ref.name).with_part(record, info.packages)
        try:
            self._store.save(updated)
        except OSError as exc:
            self._log(f"Writing the registry failed:\n{traceback.format_exc()}")
            self._warn(
                run,
                f"The part was added, but the registry {self._store.path} could not be written "
                f"({_describe(exc)}). The part will be merged again if you add it again.",
            )

    def _three_d(self, run: _Run, step: Path, *, reopen: LibraryRef | None) -> None:
        """The 3D step. It never undoes the 2D result: a failure is a warning."""
        request = run.request
        stage = "open the library"
        try:
            if reopen is not None:  # the library is not the active document yet
                self._say(LEVEL_INFO, f"Opening the {reopen.name}...")
                self._api.open_library(reopen)
            stage = "create the 3D package"
            self._say(LEVEL_INFO, f"Creating the 3D package for {_label(request)}...")
            self._api.create_3d_package(request.lbr_path)
            stage = f"import {step.name}"
            self._say(LEVEL_INFO, "Importing the 3D model...")
            self._api.import_step(step)
        except Exception as exc:  # boundary to Fusion: the 2D result stays
            self._log(f"3D step failed ({stage}):\n{traceback.format_exc()}")
            run.three_d = "failed"
            hint = f" You can import {step} by hand." if stage.startswith("import") else ""
            self._warn(run, f"3D: could not {stage}: {_describe(exc)}.{hint}")
            return
        run.three_d = "started"

    # Helpers --------------------------------------------------------------------------------

    def _call(self, what: str, operation: Callable[[], T], *, hint: str = "") -> T:
        """Run one Fusion operation; any exception becomes a `_Failure` naming `what`."""
        try:
            return operation()
        except Exception as exc:  # boundary to Fusion
            self._log(f"Could not {what}:\n{traceback.format_exc()}")
            extra = f" {hint}" if hint else ""
            raise _Failure(f"Could not {what}: {_describe(exc)}.{extra}") from exc

    def _conclude(self, run: _Run, *, ok: bool, message: str, level: str) -> FlowResult:
        request = run.request
        three_d = run.three_d
        if not ok and request.placed_step_path is not None and three_d == "none":
            three_d = "skipped"
        result = FlowResult(
            ok=ok,
            created_library=run.created,
            merged=run.merged,
            already_present=run.already_present,
            three_d=three_d,
            library_name=LIBRARY_NAME,
            message=message,
            warnings=tuple(run.warnings),
        )
        self._say(level, message)
        return result

    def _warn(self, run: _Run, text: str) -> None:
        run.warnings.append(text)
        self._say(LEVEL_WARN, text)

    def _say(self, level: str, text: str) -> None:
        """Tell the user and the log. A failing sink must never break the flow."""
        with contextlib.suppress(Exception):
            self._status(level, text)
        with contextlib.suppress(Exception):
            self._log(f"[{level}] {text}")


def _label(request: FlowRequest) -> str:
    return f"{request.lcsc_id} ({request.mpn})" if request.mpn else request.lcsc_id


def _describe(exc: BaseException) -> str:
    return str(exc).strip() or type(exc).__name__
