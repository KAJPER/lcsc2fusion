"""Fusion specific glue. This is the ONLY add-in module that imports `adsk`.

Keep it thin: decisions live in `controller.py`. Threading rules (RESEARCH 3.1, plan decision 3):

* Every Fusion API call (including `app.log`) runs on the main thread only.
* The UI is a separate browser window served by `http_transport.HttpTransport`. Its request
  threads never touch `adsk`: they hand `(action, data)` to `MainThreadDispatcher`, which wakes the
  main thread with `app.fireCustomEvent` (safe from any thread) and waits for the answer.
* Log lines written from other threads go through `ThreadSafeLog`, which queues them and flushes
  them from the same custom event.
* Fusion deletes all palettes when the workspace changes, but add-ins keep running, so the window
  (not a palette) survives environment switches. The toolbar button is registered in every
  Electronics environment (`TARGETS`).
"""

from __future__ import annotations

import contextlib
import os
import queue
import re
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable, Iterable
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple, TypeVar

import adsk.core
from browser_window import BrowserWindow
from controller import Controller, ImportOutcome, JobResult
from core_service import CorePartService
from http_transport import HttpTransport, Hub, HubSink

T = TypeVar("T")

PALETTE_ID = "LCSC2Fusion_Palette"
PALETTE_TITLE = "LCSC2Fusion"
PALETTE_WIDTH = 450
PALETTE_HEIGHT = 640
COMMAND_ID = "LCSC2Fusion_ShowPalette"
COMMAND_NAME = "LCSC2Fusion"
COMMAND_TOOLTIP = "Search LCSC parts and add them to a Fusion Electronics library"
CUSTOM_EVENT_ID = "LCSC2Fusion_JobDone"
DISPATCH_EVENT_ID = "LCSC2Fusion_Dispatch"
LOG_PREFIX = "[LCSC2Fusion] "
OPEN_COMMAND = "Document.newDesignFromLocal"

DISPATCH_TIMEOUT_S = 30.0  # how long an HTTP request may wait for the main thread
MAX_PENDING_REQUESTS = 64  # queued requests while the main thread is busy (modal dialog)
MAX_PENDING_LOG_LINES = 500
SHUTDOWN_WAIT_S = 1.0  # stop(): how long to wait for the window to disconnect
LAUNCH_GRACE_S = 30.0  # a window that was just launched may still be loading its page
# Temporary switch (D-023 removes it): LCSC2FUSION_UI=palette keeps the old Fusion palette.
UI_ENV_VAR = "LCSC2FUSION_UI"
UI_PALETTE = "palette"

ACTION_SHUTDOWN = "shutdown"
MODE_TAB = "tab"  # BrowserWindow.launch result for a default browser tab (untracked)


class ButtonTarget(NamedTuple):
    workspace: str
    tab: str
    panel: str


# Environments that get the open-window button. The ids come from Fusion's toolbar XML and the
# Autodesk add-ins; registration is fail soft and `register_buttons` logs what it finds. Confirmed
# on Fusion 2705.1.30: these four exist (`PCBDesignEnvironement` does not: "foundEnv" validation
# error from `ui.workspaces.itemById`).
TARGETS: tuple[ButtonTarget, ...] = (
    ButtonTarget("FusionSolidEnvironment", "ToolsTab", "SolidScriptsAddinsPanel"),
    ButtonTarget("ElectronEmptyLbrEnvironment", "ToolsTab", "EagleLbrScriptsAddinsPanel"),
    ButtonTarget("SchEditorEnvironement", "ToolsTab", "EagleSchScriptsAddinsPanel"),
    ButtonTarget("BoardLayoutEnvironement", "ToolsTab", "EaglePcbScriptsAddinsPanel"),
)

_TOKEN_PARAMETER = re.compile(r"([?&]t=)[^&\s'\"]+")


def log(message: str) -> None:
    """Write to the Text Commands window and to Fusion's log file. Main thread only."""
    app = adsk.core.Application.get()
    text = f"{LOG_PREFIX}{message}"
    app.log(text)
    app.log(text, adsk.core.LogLevels.InfoLogLevel, adsk.core.LogTypes.FileLogType)


def report_error(title: str, details: str) -> None:
    """Log a failure and show it in a message box."""
    log(f"{title}\n{details}")
    ui = adsk.core.Application.get().userInterface
    if ui is not None:
        ui.messageBox(f"{title}\n\n{details}", COMMAND_NAME)


def _guarded(label: str, fn: Callable[[], object]) -> None:
    """Run `fn`; Fusion event handlers must not raise, so a failure is only logged."""
    try:
        fn()
    except Exception:  # boundary with Fusion's event loop, the traceback is logged
        log(f"{label} failed:\n{_redact(traceback.format_exc())}")


def _redact(text: str) -> str:
    """Hide the one-time token of the launch URL (`?t=...`) in text that is about to be logged."""
    return _TOKEN_PARAMETER.sub(r"\1<redacted>", text)


# Thread safe logging --------------------------------------------------------------------


class ThreadSafeLog:
    """Logger that may be called from any thread.

    `app.log` is main thread only, so lines written on another thread are queued and flushed by
    the dispatcher's custom event (`bind` supplies the wake function). The main thread is the one
    that called `bind`: the Fusion API thread is not necessarily Python's `main_thread()`.
    """

    def __init__(self, direct: Callable[[str], None] | None = None) -> None:
        self._direct = direct if direct is not None else log
        self._main_ident: int | None = None
        self._wake: Callable[[], None] | None = None
        self._pending: deque[str] = deque(maxlen=MAX_PENDING_LOG_LINES)

    def bind(self, wake: Callable[[], None]) -> None:
        """Remember the calling thread as the main thread; `wake` is safe from any thread."""
        self._main_ident = threading.get_ident()
        self._wake = wake

    def unbind(self) -> None:
        """Stop waking the main thread. Lines from other threads stay queued for `flush`."""
        self._wake = None

    def __call__(self, message: str) -> None:
        if self._main_ident is None or threading.get_ident() == self._main_ident:
            self.flush()  # keeps the order: older lines of other threads first
            self._direct(message)
            return
        self._pending.append(message)
        wake = self._wake
        if wake is not None:
            with contextlib.suppress(Exception):  # the line stays queued until the next flush
                wake()

    def flush(self) -> None:
        """Write the queued lines. Main thread only."""
        while True:
            try:
                message = self._pending.popleft()
            except IndexError:
                return
            self._direct(message)


# Main thread dispatcher ------------------------------------------------------------------


class DispatcherStoppedError(RuntimeError):
    """The dispatcher was closed: Fusion is shutting the add-in down."""


class DispatcherBusyError(RuntimeError):
    """Too many requests wait for the main thread."""


@dataclass(eq=False)  # identity comparison: a request is removed from the queue by identity
class _Request:
    action: str
    data: str
    future: Future[str]


class _DrainHandler(adsk.core.CustomEventHandler):
    def __init__(self, drain: Callable[[], None], label: str = "Job completion") -> None:
        super().__init__()
        self._drain = drain
        self._label = label

    def notify(self, args: adsk.core.CustomEventArgs) -> None:
        _guarded(self._label, self._drain)


class MainThreadDispatcher:
    """Runs `handle(action, data)` on Fusion's main thread for callers on other threads.

    `dispatch` (any thread) queues a request, fires the custom event and waits. `drain` (the
    event handler, main thread) runs the queued requests and flushes the `ThreadSafeLog`.
    """

    def __init__(
        self,
        app: adsk.core.Application,
        event_id: str = DISPATCH_EVENT_ID,
        *,
        handle: Callable[[str, str], str] | None = None,
        safe_log: ThreadSafeLog | None = None,
        timeout_s: float = DISPATCH_TIMEOUT_S,
        fallback_fire: Callable[[], object] | None = None,
    ) -> None:
        self._app = app
        self._event_id = event_id
        self._fallback_fire = fallback_fire
        self._handle = handle
        self._safe_log = safe_log
        self._timeout_s = timeout_s
        self._lock = threading.Lock()
        self._queue: deque[_Request] = deque()
        self._closed = False
        self._false_reported = False
        self._true_reported = False
        self._drain_reported = False
        self._handler = _DrainHandler(self.drain, "Dispatch")
        self._event: adsk.core.CustomEvent | None = None

    def set_handler(self, handle: Callable[[str, str], str]) -> None:
        self._handle = handle

    def set_fallback_fire(self, fallback_fire: Callable[[], object]) -> None:
        """Another way to wake the main thread (it must lead to `drain`), see `wake`."""
        self._fallback_fire = fallback_fire

    def start(self) -> None:
        self._app.unregisterCustomEvent(self._event_id)  # leftover of an unclean stop
        event = self._app.registerCustomEvent(self._event_id)
        if event is None:
            raise RuntimeError(f"Could not register the custom event {self._event_id!r}.")
        added = event.add(self._handler)
        self._event = event
        if added is False and self._safe_log is not None:
            self._safe_log(f"event.add returned False for {self._event_id!r}: no handler attached?")
        with self._lock:
            self._closed = False
        if self._safe_log is not None:
            self._safe_log.bind(self.wake)

    def close(self) -> None:
        """Refuse new requests and fail the waiting ones. Needs no Fusion call, idempotent."""
        with self._lock:
            self._closed = True
            waiting = list(self._queue)
            self._queue.clear()
        for request in waiting:
            if request.future.set_running_or_notify_cancel():
                request.future.set_exception(DispatcherStoppedError("The add-in is stopping."))

    def stop(self) -> None:
        """`close` plus unregistering the custom event (main thread), idempotent."""
        self.close()
        try:
            if self._event is not None:
                self._event.remove(self._handler)
        finally:
            self._event = None
            self._app.unregisterCustomEvent(self._event_id)
            if self._safe_log is not None:
                self._safe_log.unbind()
                self._safe_log.flush()

    def wake(self) -> None:
        """Wake the main thread without reporting anything (used by the log, so no recursion)."""
        self._fire(report=False)

    def _fire(self, *, report: bool) -> None:
        """Ask Fusion to run `drain` on the main thread. Safe from any thread.

        The return value of `fireCustomEvent` is not trusted: the documentation says it is True when
        the event was queued, yet on Fusion 2705 it returned False for calls made from the HTTP
        server threads. When it does, the main thread is woken through the fallback as well (the
        job event, which is known to be delivered, and whose handler also runs `drain`). A request
        that really never runs ends in the dispatch timeout. The first False is logged once.
        """
        queued = self._app.fireCustomEvent(self._event_id, "x")
        if queued is not False:
            if report and not self._true_reported:
                self._true_reported = True
                if self._safe_log is not None:
                    self._safe_log(f"fireCustomEvent({self._event_id!r}) returned {queued!r}.")
            return
        via = "no fallback"
        fallback = self._fallback_fire
        if fallback is not None:
            try:
                via = f"fallback returned {fallback()!r}"
            except Exception as exc:  # the request then ends in the dispatch timeout
                via = f"fallback failed: {exc!r}"
        if report and not self._false_reported:
            self._false_reported = True
            if self._safe_log is not None:
                self._safe_log(
                    f"fireCustomEvent({self._event_id!r}) returned False from a worker thread "
                    f"({via}). A request that never runs ends in a timeout."
                )

    def dispatch(self, action: str, data: str) -> str:
        """Run the action on the main thread and return its reply (any thread, blocks).

        Raises `TimeoutError` when the main thread did not get to it in time (the request is then
        dropped without running), `DispatcherStoppedError` when the add-in is stopping, or the
        exception the handler raised.
        """
        request = _Request(action, data, Future())
        with self._lock:
            if self._closed:
                raise DispatcherStoppedError("The add-in is stopping.")
            if len(self._queue) >= MAX_PENDING_REQUESTS:
                raise DispatcherBusyError("Fusion is busy: too many pending requests.")
            self._queue.append(request)
        try:
            self._fire(report=True)
        except Exception as exc:  # the request would never run: take it back
            self._abandon(request)
            raise RuntimeError(f"Could not reach Fusion's main thread: {exc}") from exc
        try:
            return request.future.result(timeout=self._timeout_s)
        except TimeoutError:
            if request.future.done():  # the handler itself raised a TimeoutError
                raise
            self._abandon(request)
            raise TimeoutError(
                f"Fusion did not process {action!r} within {self._timeout_s:g} s "
                "(is a dialog open?)."
            ) from None

    def drain(self) -> None:
        """Run the queued requests and flush queued log lines (main thread)."""
        while True:
            with self._lock:
                if not self._queue:
                    break
                request = self._queue.popleft()
            if not self._drain_reported:
                self._drain_reported = True
                if self._safe_log is not None:
                    self._safe_log("The first request reached the main thread.")
            self._run(request)
        if self._safe_log is not None:
            self._safe_log.flush()

    def _run(self, request: _Request) -> None:
        future = request.future
        if not future.set_running_or_notify_cancel():
            return  # the caller gave up (timeout): the action must not run
        handle = self._handle
        try:
            if handle is None:
                raise DispatcherStoppedError("The add-in is not ready yet.")
            future.set_result(handle(request.action, request.data))
        except Exception as exc:  # handed to the waiting HTTP thread, which reports it
            future.set_exception(exc)

    def _abandon(self, request: _Request) -> None:
        """Make sure a request that nobody waits for any more is skipped and not kept."""
        request.future.cancel()
        with self._lock, contextlib.suppress(ValueError):
            self._queue.remove(request)


# Palette output -------------------------------------------------------------------------


class PaletteSinkAdapter:
    """`PaletteSink` that forwards to `palette.sendInfoToHTML` (only for LCSC2FUSION_UI=palette)."""

    def __init__(self, ui: adsk.core.UserInterface, palette_id: str = PALETTE_ID) -> None:
        self._ui = ui
        self._palette_id = palette_id

    def send(self, action: str, data: str) -> None:
        palette = self._ui.palettes.itemById(self._palette_id)
        if palette is None:
            log(f"Dropped '{action}': the palette does not exist.")
            return
        try:
            reply = palette.sendInfoToHTML(action, data)
        except Exception:  # palette not loaded or closing: nothing else can be done here
            log(f"sendInfoToHTML('{action}') failed:\n{traceback.format_exc()}")
            return
        if not reply:  # the page's fusionJavaScriptHandler.handle must return a non empty string
            log(f"The palette did not acknowledge '{action}'.")


# Background jobs ------------------------------------------------------------------------


class ThreadJobRunner:
    """`JobRunner`: job on a worker thread, completion callback on the main thread."""

    def __init__(
        self,
        app: adsk.core.Application,
        event_id: str = CUSTOM_EVENT_ID,
        extra_drain: Callable[[], None] | None = None,
    ) -> None:
        self._app = app
        self._event_id = event_id
        self._finished: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self._extra_drain = extra_drain
        self._handler = _DrainHandler(self.drain)
        self._event: adsk.core.CustomEvent | None = None

    def set_extra_drain(self, extra_drain: Callable[[], None]) -> None:
        """Also run `extra_drain` (main thread) whenever this runner's event is handled."""
        self._extra_drain = extra_drain

    def fire(self) -> object:
        """Fire this runner's custom event (any thread). This event is known to be delivered."""
        return self._app.fireCustomEvent(self._event_id, "done")

    def start(self) -> None:
        self._app.unregisterCustomEvent(self._event_id)  # leftover of an unclean stop
        event = self._app.registerCustomEvent(self._event_id)
        if event is None:
            raise RuntimeError(f"Could not register the custom event {self._event_id!r}.")
        event.add(self._handler)
        self._event = event

    def stop(self) -> None:
        if self._event is not None:
            self._event.remove(self._handler)
            self._event = None
        self._app.unregisterCustomEvent(self._event_id)

    def run(self, fn: Callable[[], T], on_done: Callable[[JobResult[T]], None]) -> None:
        def work() -> None:
            try:
                result: JobResult[T] = JobResult(value=fn())
            except Exception as exc:  # handed to on_done, which reports it on the main thread
                result = JobResult(error=exc)
            self._finished.put(lambda: on_done(result))
            self.fire()

        threading.Thread(target=work, name="LCSC2Fusion-worker", daemon=True).start()

    def drain(self) -> None:
        """Run all queued completion callbacks (main thread)."""
        while True:
            try:
                callback = self._finished.get_nowait()
            except queue.Empty:
                break
            callback()
        extra = self._extra_drain
        if extra is not None:
            extra()


# Opening the library --------------------------------------------------------------------


def build_open_command(path: Path) -> str:
    """Text command for D-006 variant A. Forward slashes, path quoted. Raises ValueError."""
    if not path.is_absolute():
        raise ValueError(f"The library path must be absolute: {path}")
    text = path.as_posix()
    if '"' in text:
        raise ValueError(f"The library path contains a double quote: {text}")
    return f'{OPEN_COMMAND} "{text}"'


class FusionImporter:
    """`LibraryImporter` using the undocumented `Document.newDesignFromLocal` text command.

    Called from the main thread by the `open_library` action (a palette `incomingFromHTML`
    handler, or the custom event handler of the dispatcher; whether a text command is safe in
    the latter is the open risk of plan gate 4). Any failure is returned as
    `ImportOutcome(opened=False, ...)`; the controller then shows the manual steps (variant C),
    which always work.
    """

    def __init__(self, app: adsk.core.Application, log_fn: Callable[[str], None] = log) -> None:
        self._app = app
        self._log = log_fn

    def open_library(self, path: Path) -> ImportOutcome:
        try:
            command = build_open_command(path)
        except ValueError as exc:
            return ImportOutcome(opened=False, message=str(exc))
        if not path.is_file():
            return ImportOutcome(opened=False, message=f"Library file not found: {path}")
        try:
            reply = self._app.executeTextCommand(command)
        except Exception as exc:  # undocumented command: whatever goes wrong, fall back to manual
            self._log(f"{command} failed: {exc!r}")
            return ImportOutcome(opened=False, message=f"Fusion rejected the open command ({exc})")
        self._log(f"{command} -> {reply!r}")
        return ImportOutcome(opened=True, message="Open command sent to Fusion.")


# Toolbar buttons ------------------------------------------------------------------------


def _first_panel_for(ui: adsk.core.UserInterface, target: ButtonTarget) -> tuple[Any, str]:
    """The toolbar panel of `target` and, when it is None, why. Never raises."""
    reason = "workspace not found"
    try:
        workspace = ui.workspaces.itemById(target.workspace)
        if workspace is not None:
            reason = "tab not found"
            tab = workspace.toolbarTabs.itemById(target.tab)
            if tab is not None:
                reason = "panel not found"
                panel = tab.toolbarPanels.itemById(target.panel)
                if panel is not None:
                    return panel, ""
    except Exception as exc:  # a workspace that is not loaded yet may refuse the lookup
        reason = f"lookup failed: {type(exc).__name__}: {exc}"
    try:
        panel = ui.allToolbarPanels.itemById(target.panel)  # the panel may sit elsewhere
    except Exception as exc:
        return None, f"lookup failed: {type(exc).__name__}: {exc}"
    return (panel, "") if panel is not None else (None, reason)


def _related_workspace_ids(panel: Any) -> list[str]:
    """Ids of the workspaces a panel is shown in (to confirm `TARGETS`). Empty when unknown."""
    try:
        collection = panel.relatedWorkspaces
        if collection is None:
            return []
        return [str(collection.item(index).id) for index in range(collection.count)]
    except Exception:  # purely informational, the property may not exist for every panel
        return []


def register_buttons(
    ui: adsk.core.UserInterface,
    definition: adsk.core.CommandDefinition,
    log_fn: Callable[[str], None],
) -> list[str]:
    """Add a promoted button for `definition` to the panel of every target (fail soft).

    A panel shared by several targets gets one control, an existing control is left alone.
    One log line per target says `added`, `exists`, `missing` or `failed`. Returns the ids of the
    panels where a control was added, which `unregister_buttons` removes again.
    """
    added: list[str] = []
    handled: set[str] = set()
    for target in TARGETS:
        label = f"button: {target.workspace} / {target.panel} ->"
        if target.panel in handled:
            log_fn(f"{label} exists (shared panel)")
            continue
        panel, reason = _first_panel_for(ui, target)
        if panel is None:
            log_fn(f"{label} missing ({reason})")
            continue
        try:
            if panel.controls.itemById(COMMAND_ID) is not None:
                handled.add(target.panel)
                log_fn(f"{label} exists")
                continue
            control = panel.controls.addCommand(definition)
        except Exception as exc:  # fail soft: the other environments still get their button
            log_fn(f"{label} failed ({type(exc).__name__}: {exc})")
            continue
        handled.add(target.panel)
        added.append(target.panel)
        note = ""
        try:
            control.isPromoted = True  # otherwise it hides in the panel dropdown
        except Exception as exc:
            note = f", could not promote it ({type(exc).__name__}: {exc})"
        related = _related_workspace_ids(panel)
        if related:
            note += f" [panel shown in: {', '.join(related)}]"
        log_fn(f"{label} added{note}")
    return added


def _panel_for_id(ui: adsk.core.UserInterface, panel_id: str) -> Any:
    for target in TARGETS:
        if target.panel == panel_id:
            panel, _ = _first_panel_for(ui, target)
            if panel is not None:
                return panel
    try:
        return ui.allToolbarPanels.itemById(panel_id)
    except Exception:  # nothing to remove from a panel that cannot be looked up
        return None


def unregister_buttons(
    ui: adsk.core.UserInterface, panel_ids: Iterable[str], log_fn: Callable[[str], None]
) -> None:
    """Delete our control from each given panel. One failure does not stop the others."""
    for panel_id in panel_ids:
        panel = _panel_for_id(ui, panel_id)
        if panel is None:
            continue  # nothing to remove from a panel that does not exist
        try:
            control = panel.controls.itemById(COMMAND_ID)
            if control is not None:
                control.deleteMe()
        except Exception as exc:
            log_fn(f"button: could not remove the control from {panel_id} ({exc!r})")


# UI wiring ------------------------------------------------------------------------------


class _HtmlHandler(adsk.core.HTMLEventHandler):
    def __init__(self, handle: Callable[[str, str], str]) -> None:
        super().__init__()
        self._handle = handle

    def notify(self, args: adsk.core.HTMLEventArgs) -> None:
        reply = "ERROR"
        try:
            reply = self._handle(args.action, args.data)
        except Exception:  # Controller.handle never raises; this guards the glue itself
            log(f"Palette event failed:\n{traceback.format_exc()}")
        args.returnData = reply


class _NavigationHandler(adsk.core.NavigationEventHandler):
    """Send web links to the system browser so the palette page is never replaced."""

    def notify(self, args: adsk.core.NavigationEventArgs) -> None:
        if args.navigationURL.startswith(("http://", "https://")):
            args.launchExternally = True


class _CommandExecuteHandler(adsk.core.CommandEventHandler):
    def __init__(self, callback: Callable[[], None]) -> None:
        super().__init__()
        self._callback = callback

    def notify(self, args: adsk.core.CommandEventArgs) -> None:
        _guarded("Show window", self._callback)


class _CommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def __init__(self, execute_handler: _CommandExecuteHandler) -> None:
        super().__init__()
        self._execute_handler = execute_handler

    def notify(self, args: adsk.core.CommandCreatedEventArgs) -> None:
        _guarded("Command created", lambda: args.command.execute.add(self._execute_handler))


class _WorkspaceActivatedHandler(adsk.core.WorkspaceEventHandler):
    def __init__(self, callback: Callable[[], None]) -> None:
        super().__init__()
        self._callback = callback

    def notify(self, args: adsk.core.WorkspaceEventArgs) -> None:
        _guarded("Workspace activated", self._callback)


class _DocumentActivatedHandler(adsk.core.DocumentEventHandler):
    def __init__(self, callback: Callable[[], None]) -> None:
        super().__init__()
        self._callback = callback

    def notify(self, args: adsk.core.DocumentEventArgs) -> None:
        _guarded("Document activated", self._callback)


class AddinApp:
    """Owns every Fusion object the add-in creates and removes them again in `stop`."""

    def __init__(self, addin_dir: Path) -> None:
        self._addin_dir = addin_dir
        self._app = adsk.core.Application.get()
        self._ui = self._app.userInterface
        self._use_palette = os.environ.get(UI_ENV_VAR, "").strip().lower() == UI_PALETTE
        self._log = ThreadSafeLog()
        self._runner = ThreadJobRunner(self._app)
        self._dispatcher = MainThreadDispatcher(self._app, DISPATCH_EVENT_ID, safe_log=self._log)
        # The job event is the one known to be delivered: use it as the second way to wake the
        # main thread, and let its handler also drain the dispatcher.
        self._runner.set_extra_drain(self._dispatcher.drain)
        self._dispatcher.set_fallback_fire(self._runner.fire)
        self._hub: Hub | None = None
        self._transport: HttpTransport | None = None
        self._window: BrowserWindow | None = None
        self._window_mode: str | None = None  # mode of the last launch
        self._launched_at: float | None = None
        self._palette_controller: Controller | None = None  # only with LCSC2FUSION_UI=palette
        self._definition: adsk.core.CommandDefinition | None = None
        self._button_panels: list[str] = []  # panels where we added our control
        self._reported_buttons: set[str] = set()
        # Fusion holds handlers weakly: keep references until stop().
        self._execute_handler = _CommandExecuteHandler(self.show_window)
        self._workspace_handler = _WorkspaceActivatedHandler(self._on_environment_changed)
        self._document_handler = _DocumentActivatedHandler(self._on_environment_changed)
        self._handlers: list[object] = [self._execute_handler]
        self._events_connected = False

    # Lifecycle ------------------------------------------------------------------------------

    def start(self, show_window: bool = True) -> None:
        """Register everything. `show_window` is False for a start at application startup."""
        self._runner.start()
        self._delete_palette()  # leftover of an unclean stop or of an older version
        if self._use_palette:
            self._start_palette_controller()
        else:
            self._start_window_stack()
        self._create_command()
        if show_window:
            self.show_window()

    def stop(self) -> None:
        hub, transport, window = self._hub, self._transport, self._window
        if hub is not None:
            _guarded("Announce shutdown", lambda: hub.publish(ACTION_SHUTDOWN, ""))
        _guarded("Stop dispatching", self._dispatcher.close)  # waiting requests fail at once
        _guarded("Wait for the window", self._wait_for_disconnect)
        if window is not None:
            _guarded("Close window", window.close)
        if transport is not None:
            _guarded("Stop transport", transport.stop)
        if hub is not None:
            _guarded("Close hub", hub.close)
        self._hub = self._transport = self._window = None
        _guarded("Disconnect environment handlers", self._disconnect_environment_events)
        _guarded("Remove toolbar controls", self._remove_controls)
        _guarded("Delete command definition", self._delete_command_definition)
        _guarded("Delete palette", self._delete_palette)
        _guarded("Stop dispatcher", self._dispatcher.stop)
        _guarded("Stop job runner", self._runner.stop)
        self._handlers.clear()

    def _start_window_stack(self) -> None:
        """Dispatcher, controller, local HTTP server and the (not yet shown) window helper."""
        self._dispatcher.start()
        hub = self._hub = Hub()
        controller = Controller(
            CorePartService(),
            FusionImporter(self._app, self._log),
            HubSink(hub),
            self._runner,
            self._log,
        )
        self._dispatcher.set_handler(controller.handle)
        transport = self._transport = HttpTransport(
            hub, self._dispatcher.dispatch, self._log, self._addin_dir / "palette"
        )
        transport.start()
        self._window = BrowserWindow(log=self._log)

    def _start_palette_controller(self) -> None:
        """Temporary (D-023): the old palette UI, for comparison with the window."""
        controller = Controller(
            CorePartService(),
            FusionImporter(self._app),
            PaletteSinkAdapter(self._ui),
            self._runner,
            log,
        )
        self._palette_controller = controller

    # Window ---------------------------------------------------------------------------------

    def show_window(self) -> None:
        """Open the window, or bring the existing one to the front (main thread)."""
        if self._use_palette:
            self.show_palette()
            return
        hub, transport, window = self._hub, self._transport, self._window
        if hub is None or transport is None or window is None:
            self._log("The window cannot be shown: the add-in is not running.")
            return
        connected = hub.subscriber_count > 0
        if window.is_alive():
            launched_at = self._launched_at
            starting = launched_at is not None and time.monotonic() - launched_at < LAUNCH_GRACE_S
            if connected or starting:  # one window is enough
                if not window.focus():
                    self._log("The window is already open (could not bring it to the front).")
                return
        elif connected and self._window_mode == MODE_TAB:
            self._log("The window is already connected (browser tab).")  # a tab has no handle
            return
        try:
            result = window.launch(transport.launch_url())
        except Exception as exc:  # BrowserLaunchError, or the URL was rejected
            self._log(f"Could not open the window:\n{_redact(traceback.format_exc())}")
            self._ui.messageBox(
                f"Could not open the LCSC2Fusion window.\n\n{_redact(str(exc))}", COMMAND_NAME
            )
            return
        self._window_mode = result.mode
        self._launched_at = time.monotonic()
        self._log(f"Window opened: mode {result.mode}, browser {result.browser}.")

    def _wait_for_disconnect(self) -> None:
        """Give the page a moment to close itself after the `shutdown` event."""
        hub = self._hub
        deadline = time.monotonic() + SHUTDOWN_WAIT_S
        while hub is not None and hub.subscriber_count > 0 and time.monotonic() < deadline:
            time.sleep(0.05)

    # Palette (temporary, D-023) ---------------------------------------------------------------

    def show_palette(self) -> None:
        palettes = self._ui.palettes
        palette = palettes.itemById(PALETTE_ID)
        if palette is None:
            url = (self._addin_dir / "palette" / "index.html").as_posix()
            palette = palettes.add(
                PALETTE_ID,
                PALETTE_TITLE,
                url,
                True,  # isVisible
                True,  # showCloseButton
                True,  # isResizable
                PALETTE_WIDTH,
                PALETTE_HEIGHT,
                True,  # useNewWebBrowser (ignored, Qt is always used)
            )
            assert self._palette_controller is not None  # set by start() in palette mode
            html_handler = _HtmlHandler(self._palette_controller.handle)
            navigation_handler = _NavigationHandler()
            palette.incomingFromHTML.add(html_handler)
            palette.navigatingURL.add(navigation_handler)
            self._handlers += [html_handler, navigation_handler]
            palette.dockingState = adsk.core.PaletteDockingStates.PaletteDockStateRight
        palette.isVisible = True

    def _delete_palette(self) -> None:
        palette = self._ui.palettes.itemById(PALETTE_ID)
        if palette is not None:
            palette.deleteMe()

    # Command and toolbar --------------------------------------------------------------------

    def _create_command(self) -> None:
        definitions = self._ui.commandDefinitions
        all_panels = [target.panel for target in TARGETS]
        unregister_buttons(self._ui, all_panels, self._log)  # leftovers of an unclean stop
        self._delete_command_definition()
        resources = os.path.join(str(self._addin_dir / "resources"), "")
        definition = definitions.addButtonDefinition(
            COMMAND_ID, COMMAND_NAME, COMMAND_TOOLTIP, resources
        )
        self._definition = definition
        created_handler = _CommandCreatedHandler(self._execute_handler)
        definition.commandCreated.add(created_handler)
        self._handlers.append(created_handler)
        self._button_panels = register_buttons(self._ui, definition, self._log)
        if not self._button_panels:
            self._log("No toolbar panel found: the button is missing in every environment.")
        self._connect_environment_events()

    def _connect_environment_events(self) -> None:
        self._ui.workspaceActivated.add(self._workspace_handler)
        self._app.documentActivated.add(self._document_handler)
        self._events_connected = True

    def _disconnect_environment_events(self) -> None:
        if not self._events_connected:
            return
        self._events_connected = False
        try:
            self._ui.workspaceActivated.remove(self._workspace_handler)
        finally:  # the second removal must not depend on the first
            self._app.documentActivated.remove(self._document_handler)

    def _on_environment_changed(self) -> None:
        """Fusion greys the command out in some environments: enable it again (as Autodesk's own
        add-ins do) and put the button back where it vanished or a workspace loaded late."""
        definition = self._definition
        if definition is None:
            return
        _guarded(
            "Enable the command", lambda: setattr(definition.controlDefinition, "isEnabled", True)
        )
        _guarded("Check toolbar buttons", lambda: self._check_buttons(definition))

    def _check_buttons(self, definition: adsk.core.CommandDefinition) -> None:
        added = register_buttons(self._ui, definition, self._log_new_button_events)
        for panel_id in added:
            if panel_id not in self._button_panels:
                self._button_panels.append(panel_id)

    def _log_new_button_events(self, message: str) -> None:
        """Only news after the first registration, and each of them once."""
        if (
            "-> added" in message or "-> failed" in message
        ) and message not in self._reported_buttons:
            self._reported_buttons.add(message)
            self._log(message)

    def _remove_controls(self) -> None:
        panel_ids, self._button_panels = self._button_panels, []
        unregister_buttons(self._ui, panel_ids, self._log)

    def _delete_command_definition(self) -> None:
        self._definition = None
        definition = self._ui.commandDefinitions.itemById(COMMAND_ID)
        if definition is not None:
            definition.deleteMe()
