"""Fusion specific glue. This is the ONLY add-in module that imports `adsk`.

Keep it thin: decisions live in `controller.py`. Threading rule (RESEARCH 3.1): the Fusion API is
used on the main thread only. The worker thread runs the job function and then calls
`app.fireCustomEvent`; a custom event handler runs the queued completion callback on the main
thread.
"""

from __future__ import annotations

import os
import queue
import threading
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import adsk.core
from controller import Controller, ImportOutcome, JobResult
from core_service import CorePartService

T = TypeVar("T")

PALETTE_ID = "LCSC2Fusion_Palette"
PALETTE_TITLE = "LCSC2Fusion"
PALETTE_WIDTH = 450
PALETTE_HEIGHT = 640
COMMAND_ID = "LCSC2Fusion_ShowPalette"
COMMAND_NAME = "LCSC2Fusion"
COMMAND_TOOLTIP = "Search LCSC parts and add them to a Fusion Electronics library"
CUSTOM_EVENT_ID = "LCSC2Fusion_JobDone"
# Design workspace, Utilities tab, ADD-INS panel (same ids the default Fusion add-in template uses).
WORKSPACE_ID = "FusionSolidEnvironment"
PANEL_ID = "SolidScriptsAddinsPanel"
LOG_PREFIX = "[LCSC2Fusion] "
OPEN_COMMAND = "Document.newDesignFromLocal"


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
        log(f"{label} failed:\n{traceback.format_exc()}")


# Palette output -------------------------------------------------------------------------


class PaletteSinkAdapter:
    """`PaletteSink` that forwards to `palette.sendInfoToHTML`."""

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


class _DrainHandler(adsk.core.CustomEventHandler):
    def __init__(self, drain: Callable[[], None]) -> None:
        super().__init__()
        self._drain = drain

    def notify(self, args: adsk.core.CustomEventArgs) -> None:
        _guarded("Job completion", self._drain)


class ThreadJobRunner:
    """`JobRunner`: job on a worker thread, completion callback on the main thread."""

    def __init__(self, app: adsk.core.Application, event_id: str = CUSTOM_EVENT_ID) -> None:
        self._app = app
        self._event_id = event_id
        self._finished: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self._handler = _DrainHandler(self.drain)
        self._event: adsk.core.CustomEvent | None = None

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
            self._app.fireCustomEvent(self._event_id, "done")

        threading.Thread(target=work, name="LCSC2Fusion-worker", daemon=True).start()

    def drain(self) -> None:
        """Run all queued completion callbacks (main thread)."""
        while True:
            try:
                callback = self._finished.get_nowait()
            except queue.Empty:
                return
            callback()


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

    Called only from the palette's `incomingFromHTML` handler (the `open_library` action), the one
    context RESEARCH 3.3 confirms for Electronics text commands. Any failure is returned as
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
        _guarded("Show palette", self._callback)


class _CommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def __init__(self, execute_handler: _CommandExecuteHandler) -> None:
        super().__init__()
        self._execute_handler = execute_handler

    def notify(self, args: adsk.core.CommandCreatedEventArgs) -> None:
        _guarded("Command created", lambda: args.command.execute.add(self._execute_handler))


class AddinApp:
    """Owns every Fusion object the add-in creates and removes them again in `stop`."""

    def __init__(self, addin_dir: Path) -> None:
        self._addin_dir = addin_dir
        self._app = adsk.core.Application.get()
        self._ui = self._app.userInterface
        self._runner = ThreadJobRunner(self._app)
        self._controller = Controller(
            CorePartService(),
            FusionImporter(self._app),
            PaletteSinkAdapter(self._ui),
            self._runner,
            log,
        )
        # Fusion holds handlers weakly: keep references until stop().
        self._execute_handler = _CommandExecuteHandler(self.show_palette)
        self._handlers: list[object] = [self._execute_handler]

    def start(self) -> None:
        self._runner.start()
        self._delete_palette()  # leftover of an unclean stop, its handlers are gone
        self._create_command()
        self.show_palette()

    def stop(self) -> None:
        _guarded("Remove toolbar control", self._remove_control)
        _guarded("Delete command definition", self._delete_command_definition)
        _guarded("Delete palette", self._delete_palette)
        _guarded("Stop job runner", self._runner.stop)
        self._handlers.clear()

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
            html_handler = _HtmlHandler(self._controller.handle)
            navigation_handler = _NavigationHandler()
            palette.incomingFromHTML.add(html_handler)
            palette.navigatingURL.add(navigation_handler)
            self._handlers += [html_handler, navigation_handler]
            palette.dockingState = adsk.core.PaletteDockingStates.PaletteDockStateRight
        palette.isVisible = True

    # Command and toolbar --------------------------------------------------------------------

    def _create_command(self) -> None:
        definitions = self._ui.commandDefinitions
        self._remove_control()  # leftovers of an unclean stop
        self._delete_command_definition()
        resources = os.path.join(str(self._addin_dir / "resources"), "")
        definition = definitions.addButtonDefinition(
            COMMAND_ID, COMMAND_NAME, COMMAND_TOOLTIP, resources
        )
        created_handler = _CommandCreatedHandler(self._execute_handler)
        definition.commandCreated.add(created_handler)
        self._handlers.append(created_handler)
        panel = self._find_panel()
        if panel is None:
            log("No toolbar panel found: the palette is shown now, restart the add-in to reopen.")
            return
        panel.controls.addCommand(definition)

    def _find_panel(self) -> adsk.core.ToolbarPanel | None:
        workspace = self._ui.workspaces.itemById(WORKSPACE_ID)
        if workspace is None:
            log(f"Workspace {WORKSPACE_ID} not found, using the generic Add-ins panel.")
        else:
            panel = workspace.toolbarPanels.itemById(PANEL_ID)
            if panel is not None:
                return panel
            log(f"Panel {PANEL_ID} not found in {WORKSPACE_ID}, using the generic Add-ins panel.")
        return self._ui.allToolbarPanels.itemById(PANEL_ID)

    def _remove_control(self) -> None:
        panel = self._find_panel()
        control = panel.controls.itemById(COMMAND_ID) if panel is not None else None
        if control is not None:
            control.deleteMe()

    def _delete_command_definition(self) -> None:
        definition = self._ui.commandDefinitions.itemById(COMMAND_ID)
        if definition is not None:
            definition.deleteMe()

    def _delete_palette(self) -> None:
        palette = self._ui.palettes.itemById(PALETTE_ID)
        if palette is not None:
            palette.deleteMe()
