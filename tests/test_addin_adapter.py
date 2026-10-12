"""Tests for fusion_adapter against the official adsk stubs (skipped when they are missing).

The stubs provide the real base classes of the Fusion event handlers. Everything else Fusion
provides (application, UI, palette) is replaced by small recording fakes. The HTTP server and the
browser window are replaced by fakes too, except in the one end to end test at the bottom, which
runs the real `HttpTransport`, `Hub` and `BrowserWindow` (with a fake browser process).
"""

from __future__ import annotations

import http.client
import importlib
import json
import os
import sys
import threading
import time
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import pytest
from test_addin_dispatcher import MainLoop

pytestmark = pytest.mark.usefixtures("adsk_stubs")

ROOT = Path(__file__).resolve().parents[1]
ADDIN_DIR = ROOT / "addin" / "LCSC2Fusion"
DESIGN_KEY = ("FusionSolidEnvironment", "ToolsTab", "SolidScriptsAddinsPanel")


@pytest.fixture(scope="module")
def fa(adsk_stubs: Path) -> ModuleType:
    return importlib.import_module("fusion_adapter")


@pytest.fixture(autouse=True)
def isolated_api_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    """The MCP discovery file of every started add-in goes here, never to the real LOCALAPPDATA."""
    path = tmp_path_factory.mktemp("api") / "api.json"
    monkeypatch.setenv("LCSC2FUSION_API", str(path))
    return path


def wait_until(condition: Callable[[], bool], timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        assert time.monotonic() < deadline, "condition not reached in time"
        time.sleep(0.005)


def fired(fake: Any, event_id: str) -> bool:
    return any(fired_id == event_id for fired_id, _ in fake.fired_with)


# Recording fakes -----------------------------------------------------------------------------


class FakeEvent:
    def __init__(self) -> None:
        self.handlers: list[Any] = []

    def add(self, handler: Any) -> bool:
        self.handlers.append(handler)
        return True

    def remove(self, handler: Any) -> bool:
        self.handlers.remove(handler)
        return True


class FakePalette:
    def __init__(self, palette_id: str, args: tuple[Any, ...]) -> None:
        self.id = palette_id
        self.args = args
        self.incomingFromHTML = FakeEvent()
        self.navigatingURL = FakeEvent()
        self.isVisible = False
        self.dockingState: Any = None
        self.sent: list[tuple[str, str]] = []
        self.reply = "OK"
        self.deleted = False

    def sendInfoToHTML(self, action: str, data: str) -> str:
        self.sent.append((action, data))
        return self.reply

    def deleteMe(self) -> bool:
        self.deleted = True
        return True


class FakePalettes:
    def __init__(self) -> None:
        self.items: dict[str, FakePalette] = {}

    def itemById(self, palette_id: str) -> FakePalette | None:
        palette = self.items.get(palette_id)
        return None if palette is None or palette.deleted else palette

    def add(self, palette_id: str, *args: Any) -> FakePalette:
        palette = FakePalette(palette_id, args)
        self.items[palette_id] = palette
        return palette


class FakeControl:
    def __init__(self, definition: Any) -> None:
        self.definition = definition
        self.isPromoted = False
        self.deleted = False

    def deleteMe(self) -> bool:
        self.deleted = True
        return True


class FakeControls:
    def __init__(self) -> None:
        self.items: dict[str, FakeControl] = {}

    def addCommand(self, definition: Any) -> FakeControl:
        control = FakeControl(definition)
        self.items[definition.id] = control
        return control

    def itemById(self, control_id: str) -> FakeControl | None:
        control = self.items.get(control_id)
        return None if control is None or control.deleted else control


class FakePanel:
    def __init__(self) -> None:
        self.controls = FakeControls()


class FakeDefinition:
    def __init__(self, definition_id: str, name: str, tooltip: str, resources: str) -> None:
        self.id = definition_id
        self.name = name
        self.tooltip = tooltip
        self.resources = resources
        self.commandCreated = FakeEvent()
        self.controlDefinition = SimpleNamespace(isEnabled=True)
        self.deleted = False

    def deleteMe(self) -> bool:
        self.deleted = True
        return True


class FakeDefinitions:
    def __init__(self) -> None:
        self.items: dict[str, FakeDefinition] = {}

    def itemById(self, definition_id: str) -> FakeDefinition | None:
        item = self.items.get(definition_id)
        return None if item is None or item.deleted else item

    def addButtonDefinition(self, *args: str) -> FakeDefinition:
        definition = FakeDefinition(*args)
        self.items[definition.id] = definition
        return definition


class Lookup:
    def __init__(self, items: dict[str, Any]) -> None:
        self._items = items

    def itemById(self, item_id: str) -> Any:
        return self._items.get(item_id)


class FakeUi:
    """`layout` maps (workspace, tab, panel) ids to panels; the default is the Design panel."""

    def __init__(self, layout: dict[tuple[str, str, str], FakePanel] | None = None) -> None:
        self.layout = {DESIGN_KEY: FakePanel()} if layout is None else layout
        self.palettes = FakePalettes()
        self.commandDefinitions = FakeDefinitions()
        self.workspaces = SimpleNamespace(itemById=self._workspace)
        self.allToolbarPanels = SimpleNamespace(itemById=self._panel)
        self.workspaceActivated = FakeEvent()
        self.boxes: list[tuple[str, str]] = []

    @property
    def design_panel(self) -> FakePanel:
        return self.layout[DESIGN_KEY]

    def _workspace(self, workspace_id: str) -> Any:
        tabs: dict[str, dict[str, FakePanel]] = {}
        for (workspace, tab, panel_id), panel in self.layout.items():
            if workspace == workspace_id:
                tabs.setdefault(tab, {})[panel_id] = panel
        if not tabs:
            return None
        return SimpleNamespace(
            toolbarTabs=Lookup(
                {
                    name: SimpleNamespace(toolbarPanels=Lookup(panels))
                    for name, panels in tabs.items()
                }
            )
        )

    def _panel(self, panel_id: str) -> FakePanel | None:
        return next((p for (_, _, known), p in self.layout.items() if known == panel_id), None)

    def messageBox(self, text: str, title: str = "") -> None:
        self.boxes.append((title, text))


class FakeApp:
    def __init__(self, ui: FakeUi | None = None) -> None:
        self.userInterface = ui
        self.documentActivated = FakeEvent()
        self.commands: list[str] = []
        self.command_error: Exception | None = None
        self.command_reply = ""
        self.registered: dict[str, FakeEvent] = {}
        self.unregistered: list[str] = []
        self.fired = threading.Event()
        self.fired_with: list[tuple[str, str]] = []
        self.logged: list[str] = []

    def log(self, message: str, *args: Any) -> None:
        self.logged.append(message)

    def executeTextCommand(self, command: str) -> str:
        self.commands.append(command)
        if self.command_error is not None:
            raise self.command_error
        return self.command_reply

    def registerCustomEvent(self, event_id: str) -> FakeEvent:
        event = FakeEvent()
        self.registered[event_id] = event
        return event

    def unregisterCustomEvent(self, event_id: str) -> bool:
        self.unregistered.append(event_id)
        return True

    def fireCustomEvent(self, event_id: str, info: str = "") -> bool:
        self.fired_with.append((event_id, info))
        self.fired.set()
        return True


@pytest.fixture
def app(fa: ModuleType, monkeypatch: pytest.MonkeyPatch) -> FakeApp:
    """A fake application that `adsk.core.Application.get()` returns."""
    fake = FakeApp()
    monkeypatch.setattr(fa.adsk.core.Application, "get", staticmethod(lambda: fake))
    return fake


# Fakes for the window stack ------------------------------------------------------------------


class FakeHub:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls
        self.published: list[tuple[str, str]] = []
        self.subscriber_count = 0
        self.drop_on_shutdown = True
        self.closed = False

    def publish(self, action: str, data: str) -> int:
        self.published.append((action, data))
        self._calls.append(f"publish:{action}")
        if action == "shutdown" and self.drop_on_shutdown:
            self.subscriber_count = 0
        return len(self.published)

    def close(self) -> None:
        self.closed = True
        self._calls.append("hub.close")


class FakeTransport:
    url = "http://127.0.0.1:5000"

    def __init__(
        self,
        calls: list[str],
        hub: Any,
        dispatch: Any,
        log: Any,
        static_dir: Path,
        local_api: Any = None,
    ) -> None:
        self._calls = calls
        self.hub = hub
        self.dispatch = dispatch
        self.log = log
        self.static_dir = static_dir
        self.local_api = local_api
        self.started = False
        self.stopped = False
        self.start_error: Exception | None = None
        self.stop_error: Exception | None = None
        self.launches = 0

    def start(self) -> None:
        if self.start_error is not None:
            raise self.start_error
        self.started = True

    def stop(self) -> None:
        self._calls.append("transport.stop")
        self.stopped = True
        if self.stop_error is not None:
            raise self.stop_error

    def launch_url(self) -> str:
        self.launches += 1
        return f"http://127.0.0.1:5000/?t=secret-token-{self.launches}"


class FakeWindow:
    def __init__(self, calls: list[str], log: Any) -> None:
        self._calls = calls
        self.log = log
        self.alive = False
        self.mode = "app"
        self.launched: list[str] = []
        self.focused = 0
        self.focus_result = True
        self.closed = 0
        self.launch_error: Exception | None = None
        self.close_error: Exception | None = None

    def is_alive(self) -> bool:
        return self.alive

    def launch(self, url: str) -> Any:
        if self.launch_error is not None:
            raise self.launch_error
        self.launched.append(url)
        self.alive = self.mode == "app"
        return SimpleNamespace(
            mode=self.mode, pid=123 if self.mode == "app" else None, browser="edge"
        )

    def focus(self) -> bool:
        self.focused += 1
        return self.focus_result

    def close(self) -> None:
        self._calls.append("window.close")
        self.closed += 1
        self.alive = False
        if self.close_error is not None:
            raise self.close_error


class Stack:
    """The fakes `AddinApp.start()` creates, filled in as it creates them."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.hub: FakeHub | None = None
        self.transport: FakeTransport | None = None
        self.window: FakeWindow | None = None


@pytest.fixture
def stack(fa: ModuleType, monkeypatch: pytest.MonkeyPatch) -> Stack:
    """Replace Hub, HttpTransport and BrowserWindow in the adapter by recording fakes."""
    created = Stack()

    def make_hub() -> FakeHub:
        created.hub = FakeHub(created.calls)
        return created.hub

    def make_transport(
        hub: Any, dispatch: Any, log: Any, static_dir: Path, local_api: Any = None
    ) -> FakeTransport:
        created.transport = FakeTransport(created.calls, hub, dispatch, log, static_dir, local_api)
        return created.transport

    def make_window(*, log: Any) -> FakeWindow:
        created.window = FakeWindow(created.calls, log)
        return created.window

    monkeypatch.setattr(fa, "Hub", make_hub)
    monkeypatch.setattr(fa, "HttpTransport", make_transport)
    monkeypatch.setattr(fa, "BrowserWindow", make_window)
    monkeypatch.setattr(fa, "SHUTDOWN_WAIT_S", 0.3)
    return created


# build_open_command and FusionImporter -------------------------------------------------------


def test_open_command_uses_forward_slashes_and_quotes_the_path(
    fa: ModuleType, tmp_path: Path
) -> None:
    path = tmp_path / "My Libs" / "C2040.lbr"
    assert fa.build_open_command(path) == f'Document.newDesignFromLocal "{path.as_posix()}"'
    assert "\\" not in fa.build_open_command(path)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path syntax")
def test_open_command_for_a_windows_path_with_spaces(fa: ModuleType) -> None:
    path = Path(r"C:\Users\John Doe\LCSC2Fusion\libraries\C2040.lbr")
    assert fa.build_open_command(path) == (
        'Document.newDesignFromLocal "C:/Users/John Doe/LCSC2Fusion/libraries/C2040.lbr"'
    )


def test_open_command_rejects_relative_paths_and_quotes(fa: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        fa.build_open_command(Path("C2040.lbr"))
    with pytest.raises(ValueError, match="double quote"):
        fa.build_open_command(tmp_path / 'we"ird.lbr')


def test_importer_sends_the_text_command(fa: ModuleType, tmp_path: Path) -> None:
    library = tmp_path / "C2040.lbr"
    library.write_text("<eagle/>", encoding="utf-8")
    app = FakeApp()
    logs: list[str] = []
    outcome = fa.FusionImporter(app, logs.append).open_library(library)
    assert outcome.opened is True
    assert app.commands == [f'Document.newDesignFromLocal "{library.as_posix()}"']
    assert logs


def test_importer_turns_any_failure_into_a_manual_fallback(fa: ModuleType, tmp_path: Path) -> None:
    library = tmp_path / "C2040.lbr"
    library.write_text("<eagle/>", encoding="utf-8")
    app = FakeApp()
    app.command_error = RuntimeError("Unknown command Document.newDesignFromLocal")
    logs: list[str] = []
    outcome = fa.FusionImporter(app, logs.append).open_library(library)
    assert outcome.opened is False
    assert "Unknown command" in outcome.message
    assert any("failed" in line for line in logs)


def test_importer_does_not_run_a_command_for_a_missing_file(fa: ModuleType, tmp_path: Path) -> None:
    app = FakeApp()
    outcome = fa.FusionImporter(app, lambda _: None).open_library(tmp_path / "missing.lbr")
    assert outcome.opened is False
    assert "not found" in outcome.message
    assert app.commands == []


def test_importer_rejects_a_relative_path_without_a_command(fa: ModuleType) -> None:
    app = FakeApp()
    outcome = fa.FusionImporter(app, lambda _: None).open_library(Path("relative.lbr"))
    assert outcome.opened is False
    assert app.commands == []


# PaletteSinkAdapter --------------------------------------------------------------------------


def test_sink_forwards_to_the_palette(fa: ModuleType, app: FakeApp) -> None:
    ui = FakeUi({})
    palette = ui.palettes.add(fa.PALETTE_ID)
    fa.PaletteSinkAdapter(ui).send("status", '{"level": "info"}')
    assert palette.sent == [("status", '{"level": "info"}')]
    assert app.logged == []


def test_sink_survives_a_missing_palette_and_an_empty_reply(fa: ModuleType, app: FakeApp) -> None:
    ui = FakeUi({})
    sink = fa.PaletteSinkAdapter(ui)
    sink.send("status", "{}")
    assert any("does not exist" in line for line in app.logged)
    palette = ui.palettes.add(fa.PALETTE_ID)
    palette.reply = ""
    sink.send("busy", "{}")
    assert any("did not acknowledge" in line for line in app.logged)


def test_sink_logs_instead_of_raising_when_the_palette_call_fails(
    fa: ModuleType, app: FakeApp
) -> None:
    ui = FakeUi({})
    palette = ui.palettes.add(fa.PALETTE_ID)

    def broken(action: str, data: str) -> str:
        raise RuntimeError("page not loaded")

    palette.sendInfoToHTML = broken  # type: ignore[method-assign]
    fa.PaletteSinkAdapter(ui).send("status", "{}")
    assert any("page not loaded" in line for line in app.logged)


# ThreadJobRunner -----------------------------------------------------------------------------


def test_runner_executes_off_the_main_thread_and_completes_on_drain(fa: ModuleType) -> None:
    app = FakeApp()
    runner = fa.ThreadJobRunner(app)
    runner.start()
    assert runner._handler in app.registered[fa.CUSTOM_EVENT_ID].handlers
    worker_ids: list[int] = []
    completed: list[Any] = []

    def job() -> int:
        worker_ids.append(threading.get_ident())
        return 42

    runner.run(job, completed.append)
    assert app.fired.wait(5)
    assert app.fired_with[0][0] == fa.CUSTOM_EVENT_ID
    assert worker_ids
    assert worker_ids[0] != threading.get_ident()
    assert completed == []  # the callback waits for the main thread
    app.registered[fa.CUSTOM_EVENT_ID].handlers[0].notify(None)  # Fusion fires the handler
    assert [result.unwrap() for result in completed] == [42]


def test_runner_forwards_job_exceptions(fa: ModuleType) -> None:
    app = FakeApp()
    runner = fa.ThreadJobRunner(app)

    def job() -> int:
        raise ValueError("bad job")

    completed: list[Any] = []
    runner.run(job, completed.append)
    assert app.fired.wait(5)
    runner.drain()
    assert len(completed) == 1
    assert isinstance(completed[0].error, ValueError)
    with pytest.raises(ValueError, match="bad job"):
        completed[0].unwrap()


def test_runner_start_and_stop_manage_the_custom_event(fa: ModuleType) -> None:
    app = FakeApp()
    runner = fa.ThreadJobRunner(app)
    runner.start()
    event = app.registered[fa.CUSTOM_EVENT_ID]
    assert len(event.handlers) == 1
    runner.stop()
    assert event.handlers == []
    assert app.unregistered.count(fa.CUSTOM_EVENT_ID) == 2  # once before register, once on stop
    runner.stop()  # idempotent


def test_runner_start_fails_clearly_when_the_event_cannot_be_registered(fa: ModuleType) -> None:
    app = FakeApp()
    app.registerCustomEvent = lambda event_id: None  # type: ignore[method-assign,assignment]
    with pytest.raises(RuntimeError, match="custom event"):
        fa.ThreadJobRunner(app).start()


def test_handler_classes_derive_from_the_real_adsk_bases(fa: ModuleType) -> None:
    core = fa.adsk.core
    assert issubclass(fa._DrainHandler, core.CustomEventHandler)
    assert issubclass(fa._HtmlHandler, core.HTMLEventHandler)
    assert issubclass(fa._NavigationHandler, core.NavigationEventHandler)
    assert issubclass(fa._CommandExecuteHandler, core.CommandEventHandler)
    assert issubclass(fa._CommandCreatedHandler, core.CommandCreatedEventHandler)
    assert issubclass(fa._WorkspaceActivatedHandler, core.WorkspaceEventHandler)
    assert issubclass(fa._DocumentActivatedHandler, core.DocumentEventHandler)


# AddinApp wiring: the window (default) -------------------------------------------------------


def make_app(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, ui: FakeUi, tmp_path: Path
) -> tuple[Any, FakeApp]:
    fake = FakeApp(ui)
    monkeypatch.setattr(fa.adsk.core.Application, "get", staticmethod(lambda: fake))
    return fa.AddinApp(tmp_path / "LCSC2Fusion"), fake


def started_window_app(
    fa: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stack: Stack,
    *,
    show_window: bool = True,
    ui: FakeUi | None = None,
) -> tuple[Any, FakeApp, FakeUi]:
    ui = FakeUi() if ui is None else ui
    addin, fake = make_app(fa, monkeypatch, ui, tmp_path)
    addin.start(show_window)
    return addin, fake, ui


def test_start_builds_the_window_stack_and_opens_the_window(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stack: Stack
) -> None:
    addin, fake, ui = started_window_app(fa, monkeypatch, tmp_path, stack)

    assert stack.transport is not None
    assert stack.window is not None
    assert stack.hub is not None
    assert stack.transport.started is True
    assert stack.transport.hub is stack.hub
    assert stack.transport.static_dir == tmp_path / "LCSC2Fusion" / "palette"
    assert isinstance(stack.transport.log, fa.ThreadSafeLog)
    assert stack.window.log is stack.transport.log
    assert stack.window.launched == ["http://127.0.0.1:5000/?t=secret-token-1"]
    assert ui.palettes.items == {}  # no palette in the default path
    assert fa.DISPATCH_EVENT_ID in fake.registered
    assert fa.CUSTOM_EVENT_ID in fake.registered
    assert fa.COMMAND_ID in ui.design_panel.controls.items
    assert ui.design_panel.controls.items[fa.COMMAND_ID].isPromoted is True
    logged = "\n".join(fake.logged)
    assert "Window opened: mode app, browser edge" in logged
    assert "secret-token" not in logged  # the one-time token never reaches the log
    addin.stop()


def test_start_at_application_startup_registers_everything_but_shows_no_window(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stack: Stack
) -> None:
    addin, _, ui = started_window_app(fa, monkeypatch, tmp_path, stack, show_window=False)

    assert stack.transport is not None
    assert stack.transport.started is True
    assert stack.window is not None
    assert stack.window.launched == []
    assert fa.COMMAND_ID in ui.design_panel.controls.items
    addin.stop()


def test_the_command_button_opens_the_window_again(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stack: Stack
) -> None:
    addin, _, ui = started_window_app(fa, monkeypatch, tmp_path, stack, show_window=False)
    assert stack.window is not None
    created = ui.commandDefinitions.items[fa.COMMAND_ID].commandCreated.handlers[0]
    command = SimpleNamespace(execute=FakeEvent())
    created.notify(SimpleNamespace(command=command))
    assert len(command.execute.handlers) == 1

    command.execute.handlers[0].notify(None)  # the user clicks the button

    assert len(stack.window.launched) == 1
    addin.stop()


def test_show_window_brings_a_connected_window_to_the_front_instead_of_opening_a_second(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stack: Stack
) -> None:
    addin, _, _ = started_window_app(fa, monkeypatch, tmp_path, stack)
    assert stack.window is not None
    assert stack.hub is not None
    stack.hub.subscriber_count = 1  # the page connected to the event stream

    addin.show_window()

    assert len(stack.window.launched) == 1
    assert stack.window.focused == 1
    addin.stop()


def test_show_window_does_not_restart_a_window_that_is_still_loading(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stack: Stack
) -> None:
    addin, _, _ = started_window_app(fa, monkeypatch, tmp_path, stack)
    assert stack.window is not None

    addin.show_window()  # alive, page not connected yet, launched a moment ago

    assert len(stack.window.launched) == 1
    assert stack.window.focused == 1
    addin.stop()


def test_show_window_relaunches_a_window_that_never_connected(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stack: Stack
) -> None:
    addin, _, _ = started_window_app(fa, monkeypatch, tmp_path, stack)
    assert stack.window is not None
    monkeypatch.setattr(fa, "LAUNCH_GRACE_S", 0.0)

    addin.show_window()  # alive for a long time but the page never connected

    assert len(stack.window.launched) == 2
    assert stack.window.launched[1].endswith("secret-token-2")  # a fresh token per launch
    addin.stop()


def test_show_window_opens_a_new_window_after_the_user_closed_it(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stack: Stack
) -> None:
    addin, _, _ = started_window_app(fa, monkeypatch, tmp_path, stack)
    assert stack.window is not None
    stack.window.alive = False

    addin.show_window()

    assert len(stack.window.launched) == 2
    addin.stop()


def test_show_window_logs_when_the_window_cannot_be_brought_to_the_front(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stack: Stack
) -> None:
    addin, fake, _ = started_window_app(fa, monkeypatch, tmp_path, stack)
    assert stack.window is not None
    assert stack.hub is not None
    stack.hub.subscriber_count = 1
    stack.window.focus_result = False

    addin.show_window()

    assert any("could not bring it to the front" in line for line in fake.logged)
    assert len(stack.window.launched) == 1
    addin.stop()


def test_in_tab_mode_a_connected_page_is_not_opened_a_second_time(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stack: Stack
) -> None:
    ui = FakeUi()
    addin, fake = make_app(fa, monkeypatch, ui, tmp_path)
    addin.start(False)
    assert stack.window is not None
    assert stack.hub is not None
    stack.window.mode = "tab"
    addin.show_window()  # first click: a default browser tab, no process handle
    assert stack.window.alive is False
    assert len(stack.window.launched) == 1

    stack.hub.subscriber_count = 1
    addin.show_window()
    assert len(stack.window.launched) == 1
    assert any("already connected" in line for line in fake.logged)

    stack.hub.subscriber_count = 0  # the tab was closed
    addin.show_window()
    assert len(stack.window.launched) == 2
    addin.stop()


def test_a_window_that_cannot_be_opened_is_reported_and_the_add_in_keeps_running(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stack: Stack
) -> None:
    ui = FakeUi()
    addin, fake = make_app(fa, monkeypatch, ui, tmp_path)
    addin.start(False)
    assert stack.window is not None
    stack.window.launch_error = RuntimeError(
        "no browser at http://127.0.0.1:5000/?t=secret-token-1"
    )

    addin.show_window()  # must not raise

    assert ui.boxes
    assert "Could not open the LCSC2Fusion window" in ui.boxes[0][1]
    assert "secret-token" not in ui.boxes[0][1]
    assert "secret-token" not in "\n".join(fake.logged)
    assert any("Could not open the window" in line for line in fake.logged)
    addin.stop()


def test_show_window_before_start_only_logs(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stack: Stack
) -> None:
    addin, fake = make_app(fa, monkeypatch, FakeUi(), tmp_path)
    addin.show_window()
    assert any("not running" in line for line in fake.logged)


def test_dispatch_runs_controller_actions_on_the_main_thread_and_publishes_to_the_hub(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stack: Stack
) -> None:
    addin, fake, _ = started_window_app(fa, monkeypatch, tmp_path, stack, show_window=False)
    assert stack.transport is not None
    assert stack.hub is not None
    replies: list[str] = []
    worker = threading.Thread(
        target=lambda: replies.append(stack.transport.dispatch("ready", "{}")),  # type: ignore[union-attr]
        daemon=True,
    )
    worker.start()
    wait_until(lambda: fired(fake, fa.DISPATCH_EVENT_ID))

    fake.registered[fa.DISPATCH_EVENT_ID].handlers[0].notify(None)  # Fusion is idle: main thread
    worker.join(5)

    assert replies == ["OK"]
    assert [action for action, _ in stack.hub.published] == ["busy", "status"]
    addin.stop()


def test_open_library_goes_through_the_dispatcher_to_the_importer(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stack: Stack
) -> None:
    libs = tmp_path / "libs"
    libs.mkdir()
    library = libs / "C2040.lbr"
    library.write_text("<eagle/>", encoding="utf-8")
    monkeypatch.setattr(importlib.import_module("controller"), "default_out_dir", lambda: libs)
    addin, fake, _ = started_window_app(fa, monkeypatch, tmp_path, stack, show_window=False)
    assert stack.transport is not None
    assert stack.hub is not None
    payload = json.dumps({"path": str(library)})
    replies: list[str] = []
    worker = threading.Thread(
        target=lambda: replies.append(stack.transport.dispatch("open_library", payload)),  # type: ignore[union-attr]
        daemon=True,
    )
    worker.start()
    wait_until(lambda: fired(fake, fa.DISPATCH_EVENT_ID))

    fake.registered[fa.DISPATCH_EVENT_ID].handlers[0].notify(None)
    worker.join(5)

    assert replies == ["OK"]
    assert fake.commands == [f'Document.newDesignFromLocal "{library.as_posix()}"']
    assert json.loads(stack.hub.published[-1][1])["level"] == "ok"
    addin.stop()


def test_stop_announces_shutdown_then_closes_everything_in_order(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stack: Stack
) -> None:
    addin, fake, ui = started_window_app(fa, monkeypatch, tmp_path, stack)
    definition = ui.commandDefinitions.items[fa.COMMAND_ID]
    control = ui.design_panel.controls.items[fa.COMMAND_ID]
    dispatch_event = fake.registered[fa.DISPATCH_EVENT_ID]
    job_event = fake.registered[fa.CUSTOM_EVENT_ID]
    assert stack.hub is not None
    assert stack.transport is not None
    assert stack.window is not None
    assert stack.hub.subscriber_count == 0
    stack.hub.subscriber_count = 1  # the page is connected until it receives `shutdown`

    addin.stop()

    assert stack.calls == ["publish:shutdown", "window.close", "transport.stop", "hub.close"]
    assert stack.hub.published == [("shutdown", "")]
    assert control.deleted is True
    assert definition.deleted is True
    assert dispatch_event.handlers == []
    assert job_event.handlers == []
    assert ui.workspaceActivated.handlers == []
    assert fake.documentActivated.handlers == []
    assert fake.unregistered[-2:] == [fa.DISPATCH_EVENT_ID, fa.CUSTOM_EVENT_ID]


def test_stop_does_not_wait_when_the_window_disconnects_and_waits_a_moment_when_it_does_not(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stack: Stack
) -> None:
    addin, _, _ = started_window_app(fa, monkeypatch, tmp_path, stack, show_window=False)
    assert stack.hub is not None
    stack.hub.subscriber_count = 1
    started = time.monotonic()
    addin.stop()
    assert time.monotonic() - started < 0.25  # the fake drops the subscriber on `shutdown`

    addin2, _, _ = started_window_app(fa, monkeypatch, tmp_path, stack, show_window=False)
    assert stack.hub is not None
    stack.hub.subscriber_count = 1
    stack.hub.drop_on_shutdown = False  # the window ignores the event
    started = time.monotonic()
    addin2.stop()
    assert time.monotonic() - started >= 0.3  # SHUTDOWN_WAIT_S is patched to 0.3 by the fixture
    assert stack.window is not None
    assert stack.window.closed == 1  # and is closed by force


def test_stop_is_idempotent(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stack: Stack
) -> None:
    addin, fake, _ = started_window_app(fa, monkeypatch, tmp_path, stack)
    addin.stop()
    calls = list(stack.calls)
    logged = len(fake.logged)

    addin.stop()

    assert stack.calls == calls
    assert not [line for line in fake.logged[logged:] if "failed" in line]


def test_stop_continues_after_failing_steps(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stack: Stack
) -> None:
    addin, fake, ui = started_window_app(fa, monkeypatch, tmp_path, stack)
    assert stack.window is not None
    assert stack.transport is not None
    assert stack.hub is not None
    stack.window.close_error = RuntimeError("window already gone")
    stack.transport.stop_error = RuntimeError("server did not stop")
    stack.hub.drop_on_shutdown = True

    addin.stop()

    assert stack.hub.closed is True
    assert ui.design_panel.controls.items[fa.COMMAND_ID].deleted is True
    assert ui.commandDefinitions.items[fa.COMMAND_ID].deleted is True
    assert fake.registered[fa.DISPATCH_EVENT_ID].handlers == []
    logged = "\n".join(fake.logged)
    assert "window already gone" in logged
    assert "server did not stop" in logged


def test_a_failing_start_can_be_cleaned_up_with_stop(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stack: Stack
) -> None:
    ui = FakeUi()
    addin, fake = make_app(fa, monkeypatch, ui, tmp_path)
    original = fa.HttpTransport

    def failing_transport(*args: Any, **kwargs: Any) -> FakeTransport:
        transport = original(*args, **kwargs)
        transport.start_error = OSError("port in use")
        return transport

    monkeypatch.setattr(fa, "HttpTransport", failing_transport)

    with pytest.raises(OSError, match="port in use"):
        addin.start()

    addin.stop()  # what the entry point does after a failed start
    assert stack.hub is not None
    assert stack.hub.closed is True
    assert stack.window is None
    assert fake.registered[fa.DISPATCH_EVENT_ID].handlers == []
    assert ui.commandDefinitions.items == {}  # the command was never created


def test_a_leftover_palette_of_an_older_version_is_deleted_at_start_and_stop(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stack: Stack
) -> None:
    ui = FakeUi()
    leftover = ui.palettes.add(fa.PALETTE_ID)
    addin, _ = make_app(fa, monkeypatch, ui, tmp_path)
    addin.start(False)
    assert leftover.deleted is True

    again = ui.palettes.add(fa.PALETTE_ID)  # another leftover appears while the add-in runs
    addin.stop()
    assert again.deleted is True


def test_add_through_the_dispatcher_reports_the_result_to_the_window(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stack: Stack
) -> None:
    core_service = importlib.import_module("core_service")
    controller = importlib.import_module("controller")
    libs = tmp_path / "libs"
    libs.mkdir()
    monkeypatch.setattr(controller, "default_out_dir", lambda: libs)

    def fake_add_part(self: Any, lcsc_id: str, out_dir: Path, with_3d: bool = False) -> Any:
        path = out_dir / f"{lcsc_id}.lbr"
        path.write_text("<eagle/>", encoding="utf-8")
        return controller.AddedPart(path=path, mpn="RP2040")

    monkeypatch.setattr(core_service.CorePartService, "add_part", fake_add_part)
    addin, fake, _ = started_window_app(fa, monkeypatch, tmp_path, stack, show_window=False)
    assert stack.transport is not None
    assert stack.hub is not None
    worker = threading.Thread(
        target=lambda: stack.transport.dispatch("add", '{"lcsc_id": "C2040"}'),  # type: ignore[union-attr]
        daemon=True,
    )
    worker.start()
    wait_until(lambda: fired(fake, fa.DISPATCH_EVENT_ID))
    fake.registered[fa.DISPATCH_EVENT_ID].handlers[0].notify(None)
    worker.join(5)
    wait_until(lambda: fired(fake, fa.CUSTOM_EVENT_ID))  # the add job finished on its worker

    fake.registered[fa.CUSTOM_EVENT_ID].handlers[0].notify(None)  # the worker job finished

    added = [json.loads(data) for action, data in stack.hub.published if action == "added"]
    assert len(added) == 1
    assert added[0]["path"] == str(libs / "C2040.lbr")
    assert fake.commands == []  # nothing is opened without the user's click
    addin.stop()


# AddinApp wiring: the local API for the MCP server ------------------------------------------------


def test_start_publishes_the_local_api_in_the_discovery_file(
    fa: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stack: Stack,
    isolated_api_file: Path,
) -> None:
    discovery = importlib.import_module("lcsc2fusion.discovery")
    local_api = importlib.import_module("local_api")
    addin, fake, _ = started_window_app(fa, monkeypatch, tmp_path, stack)
    assert stack.transport is not None
    api = stack.transport.local_api
    assert isinstance(api, local_api.LocalApi)
    assert len(api.token) >= 32

    endpoint = discovery.read_api_file(isolated_api_file)

    assert endpoint == discovery.ApiEndpoint(
        url=stack.transport.url, token=api.token, pid=os.getpid()
    )
    logged = "\n".join(fake.logged)
    assert "MCP discovery file written" in logged
    assert api.token not in logged  # the secret never reaches the log
    addin.stop()
    assert not isolated_api_file.exists()


def test_every_start_gets_a_new_token(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stack: Stack
) -> None:
    tokens = []
    for _ in range(2):
        addin, _, _ = started_window_app(fa, monkeypatch, tmp_path, stack, show_window=False)
        assert stack.transport is not None
        assert stack.transport.local_api is not None
        tokens.append(stack.transport.local_api.token)
        addin.stop()
    assert tokens[0] != tokens[1]


def test_stop_withdraws_the_discovery_file_before_the_server_goes_down(
    fa: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stack: Stack,
    isolated_api_file: Path,
) -> None:
    addin, _, _ = started_window_app(fa, monkeypatch, tmp_path, stack)
    assert stack.hub is not None
    assert stack.transport is not None
    assert isolated_api_file.exists()
    seen: list[bool] = []
    publish = stack.hub.publish

    def watching_publish(action: str, data: str) -> int:
        seen.append(isolated_api_file.exists())
        return publish(action, data)

    stack.hub.publish = watching_publish  # type: ignore[method-assign]

    addin.stop()

    assert seen == [False]  # gone when `shutdown` is announced, so no new client connects
    addin.stop()  # idempotent


def test_a_newer_add_in_keeps_its_discovery_file_when_an_older_one_stops(
    fa: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stack: Stack,
    isolated_api_file: Path,
) -> None:
    discovery = importlib.import_module("lcsc2fusion.discovery")
    addin, _, _ = started_window_app(fa, monkeypatch, tmp_path, stack, show_window=False)
    newer = discovery.ApiEndpoint("http://127.0.0.1:6000", "newer-" + "n" * 30, 1)
    discovery.write_api_file(isolated_api_file, newer)

    addin.stop()

    assert discovery.read_api_file(isolated_api_file) == newer


def test_a_discovery_file_that_cannot_be_written_does_not_stop_the_add_in(
    fa: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stack: Stack,
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a directory is needed", encoding="utf-8")
    monkeypatch.setenv("LCSC2FUSION_API", str(blocker / "api.json"))

    addin, fake, ui = started_window_app(fa, monkeypatch, tmp_path, stack)

    assert stack.transport is not None
    assert stack.transport.started is True
    assert stack.window is not None
    assert len(stack.window.launched) == 1  # the window opened as usual
    assert fa.COMMAND_ID in ui.design_panel.controls.items
    assert any("MCP discovery file not written" in line for line in fake.logged)
    addin.stop()


def test_no_discovery_file_is_written_when_the_server_does_not_start(
    fa: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stack: Stack,
    isolated_api_file: Path,
) -> None:
    addin, _ = make_app(fa, monkeypatch, FakeUi(), tmp_path)
    original = fa.HttpTransport

    def failing_transport(*args: Any, **kwargs: Any) -> FakeTransport:
        transport = original(*args, **kwargs)
        transport.start_error = OSError("port in use")
        return transport

    monkeypatch.setattr(fa, "HttpTransport", failing_transport)

    with pytest.raises(OSError, match="port in use"):
        addin.start()
    addin.stop()

    assert not isolated_api_file.exists()


def test_the_palette_mode_has_no_local_api(
    fa: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stack: Stack,
    isolated_api_file: Path,
) -> None:
    monkeypatch.setenv(fa.UI_ENV_VAR, "palette")
    addin, _ = make_app(fa, monkeypatch, FakeUi(), tmp_path)
    addin.start(False)
    assert stack.transport is None
    assert not isolated_api_file.exists()
    addin.stop()


def test_an_mcp_request_runs_through_the_dispatcher_and_the_window_sees_it_too(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stack: Stack
) -> None:
    core_service = importlib.import_module("core_service")
    controller = importlib.import_module("controller")
    libs = tmp_path / "libs"
    libs.mkdir()
    monkeypatch.setattr(controller, "default_out_dir", lambda: libs)

    def fake_add_part(self: Any, lcsc_id: str, out_dir: Path, with_3d: bool = False) -> Any:
        path = out_dir / f"{lcsc_id}.lbr"
        path.write_text("<eagle/>", encoding="utf-8")
        return controller.AddedPart(path=path, mpn="RP2040")

    monkeypatch.setattr(core_service.CorePartService, "add_part", fake_add_part)
    addin, fake, _ = started_window_app(fa, monkeypatch, tmp_path, stack, show_window=False)
    assert stack.transport is not None
    assert stack.hub is not None
    api = stack.transport.local_api
    assert api is not None
    replies: list[Any] = []
    worker = threading.Thread(
        target=lambda: replies.append(api.submit({"lcsc_id": "c2040", "fusion": False})),
        daemon=True,
    )
    worker.start()
    wait_until(lambda: fired(fake, fa.DISPATCH_EVENT_ID))

    fake.registered[fa.DISPATCH_EVENT_ID].handlers[0].notify(None)  # the main thread takes it
    worker.join(5)

    assert replies == [(202, {"job": 1})]
    assert api.ping()[1]["busy"] is True
    assert api.job(1)[1]["state"] == "running"
    wait_until(lambda: fired(fake, fa.CUSTOM_EVENT_ID))  # the add job finished on its worker
    fake.registered[fa.CUSTOM_EVENT_ID].handlers[0].notify(None)

    status, job = api.job(1)
    assert status == 200
    assert (job["state"], job["ok"]) == ("done", True)
    assert job["added"]["path"] == str(libs / "C2040.lbr")
    assert job["added"]["mpn"] == "RP2040"
    assert api.ping()[1]["busy"] is False
    assert "added" in [action for action, _ in stack.hub.published]  # the window got it too
    addin.stop()


# AddinApp wiring: the old palette (LCSC2FUSION_UI=palette, temporary, D-023) ------------------


def make_palette_app(
    fa: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    ui: FakeUi,
    tmp_path: Path,
    stack: Stack,
) -> tuple[Any, FakeApp]:
    monkeypatch.setenv(fa.UI_ENV_VAR, "palette")
    return make_app(fa, monkeypatch, ui, tmp_path)


def test_the_ui_switch_defaults_to_the_window(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(fa.UI_ENV_VAR, raising=False)
    addin, _ = make_app(fa, monkeypatch, FakeUi(), tmp_path)
    assert addin._use_palette is False
    monkeypatch.setenv(fa.UI_ENV_VAR, "  PALETTE ")
    addin, _ = make_app(fa, monkeypatch, FakeUi(), tmp_path)
    assert addin._use_palette is True
    monkeypatch.setenv(fa.UI_ENV_VAR, "window")
    addin, _ = make_app(fa, monkeypatch, FakeUi(), tmp_path)
    assert addin._use_palette is False


def test_palette_mode_builds_the_old_palette_and_no_window_stack(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stack: Stack
) -> None:
    ui = FakeUi()
    addin, fake = make_palette_app(fa, monkeypatch, ui, tmp_path, stack)

    addin.start()

    palette = ui.palettes.items[fa.PALETTE_ID]
    assert palette.args[0] == fa.PALETTE_TITLE
    assert palette.args[1] == (tmp_path / "LCSC2Fusion" / "palette" / "index.html").as_posix()
    assert palette.args[2] is True  # visible
    assert palette.args[5:7] == (fa.PALETTE_WIDTH, fa.PALETTE_HEIGHT)
    assert palette.dockingState == fa.adsk.core.PaletteDockingStates.PaletteDockStateRight
    assert palette.isVisible is True
    assert len(palette.incomingFromHTML.handlers) == 1
    assert len(palette.navigatingURL.handlers) == 1
    definition = ui.commandDefinitions.items[fa.COMMAND_ID]
    assert len(definition.commandCreated.handlers) == 1
    assert fa.COMMAND_ID in ui.design_panel.controls.items
    assert fa.CUSTOM_EVENT_ID in fake.registered
    assert fa.DISPATCH_EVENT_ID not in fake.registered
    assert (stack.hub, stack.transport, stack.window) == (None, None, None)

    addin.stop()

    assert palette.deleted is True
    assert definition.deleted is True
    assert ui.design_panel.controls.items[fa.COMMAND_ID].deleted is True
    assert fake.registered[fa.CUSTOM_EVENT_ID].handlers == []
    assert fake.unregistered[-1] == fa.CUSTOM_EVENT_ID


def test_palette_events_reach_the_controller_and_reply(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stack: Stack
) -> None:
    ui = FakeUi()
    addin, _ = make_palette_app(fa, monkeypatch, ui, tmp_path, stack)
    addin.start()
    palette = ui.palettes.items[fa.PALETTE_ID]
    handler = palette.incomingFromHTML.handlers[0]

    ready = SimpleNamespace(action="ready", data="{}", returnData="")
    handler.notify(ready)
    assert ready.returnData == "OK"
    assert [action for action, _ in palette.sent] == ["busy", "status"]
    assert json.loads(palette.sent[0][1]) == {"value": False}

    bad = SimpleNamespace(action="search", data="not json", returnData="")
    handler.notify(bad)
    assert bad.returnData == "ERROR"
    assert palette.sent[-1][0] == "error"


def start_palette_with_library(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stack: Stack
) -> tuple[Any, FakeApp, FakePalette, Path]:
    """Start the palette add-in with its output directory redirected into tmp_path."""
    libs = tmp_path / "libs"
    libs.mkdir()
    monkeypatch.setattr(importlib.import_module("controller"), "default_out_dir", lambda: libs)
    ui = FakeUi()
    addin, fake = make_palette_app(fa, monkeypatch, ui, tmp_path, stack)
    addin.start()
    return addin, fake, ui.palettes.items[fa.PALETTE_ID], libs


def test_open_library_runs_the_text_command_inside_the_palette_handler(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stack: Stack
) -> None:
    _, fake, palette, libs = start_palette_with_library(fa, monkeypatch, tmp_path, stack)
    library = libs / "C2040.lbr"
    library.write_text("<eagle/>", encoding="utf-8")

    event = SimpleNamespace(
        action="open_library", data=json.dumps({"path": str(library)}), returnData=""
    )
    palette.incomingFromHTML.handlers[0].notify(event)

    assert event.returnData == "OK"
    assert fake.commands == [f'Document.newDesignFromLocal "{library.as_posix()}"']
    action, data = palette.sent[-1]
    assert action == "status"
    assert json.loads(data)["level"] == "ok"


def test_open_library_failure_reaches_the_palette_as_a_warning_with_manual_steps(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stack: Stack
) -> None:
    _, fake, palette, libs = start_palette_with_library(fa, monkeypatch, tmp_path, stack)
    library = libs / "C2040.lbr"
    library.write_text("<eagle/>", encoding="utf-8")
    fake.command_error = RuntimeError("Unknown command Document.newDesignFromLocal")

    event = SimpleNamespace(
        action="open_library", data=json.dumps({"path": str(library)}), returnData=""
    )
    palette.incomingFromHTML.handlers[0].notify(event)

    status = json.loads(palette.sent[-1][1])
    assert status["level"] == "warn"
    assert "Unknown command" in status["text"]
    assert "File > Open > Open from my computer" in status["text"]


def test_open_library_outside_the_output_directory_is_refused(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stack: Stack
) -> None:
    _, fake, palette, _ = start_palette_with_library(fa, monkeypatch, tmp_path, stack)
    foreign = tmp_path / "evil.lbr"
    foreign.write_text("<eagle/>", encoding="utf-8")

    event = SimpleNamespace(
        action="open_library", data=json.dumps({"path": str(foreign)}), returnData=""
    )
    palette.incomingFromHTML.handlers[0].notify(event)

    assert event.returnData == "ERROR"
    assert fake.commands == []
    assert palette.sent[-1][0] == "error"


def test_add_finishing_on_the_custom_event_never_runs_a_text_command(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stack: Stack
) -> None:
    core_service = importlib.import_module("core_service")
    controller = importlib.import_module("controller")
    _, fake, palette, libs = start_palette_with_library(fa, monkeypatch, tmp_path, stack)

    seen: list[tuple[str, bool]] = []

    def fake_add_part(self: Any, lcsc_id: str, out_dir: Path, with_3d: bool = False) -> Any:
        seen.append((lcsc_id, with_3d))
        path = out_dir / f"{lcsc_id}.lbr"
        path.write_text("<eagle/>", encoding="utf-8")
        placed = out_dir / f"{lcsc_id}.placed.step" if with_3d else None
        return controller.AddedPart(path=path, mpn="RP2040", placed_step_path=placed)

    monkeypatch.setattr(core_service.CorePartService, "add_part", fake_add_part)
    event = SimpleNamespace(
        action="add", data='{"lcsc_id": "C2040", "with_3d": true}', returnData=""
    )
    palette.incomingFromHTML.handlers[0].notify(event)
    assert fake.fired.wait(5)
    fake.registered[fa.CUSTOM_EVENT_ID].handlers[0].notify(None)  # main thread completion

    assert seen == [("C2040", True)]
    added = [json.loads(data) for action, data in palette.sent if action == "added"]
    assert len(added) == 1
    assert added[0]["opened"] is False
    assert added[0]["path"] == str(libs / "C2040.lbr")
    assert added[0]["with_3d"] is True
    assert added[0]["placed_step_path"] == str(libs / "C2040.placed.step")
    assert added[0]["step_path"] is None
    assert fake.commands == []  # opening needs the user's click on "Open in Fusion Electronics"


def test_web_links_are_opened_externally_and_local_pages_are_not(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stack: Stack
) -> None:
    ui = FakeUi()
    addin, _ = make_palette_app(fa, monkeypatch, ui, tmp_path, stack)
    addin.start()
    handler = ui.palettes.items[fa.PALETTE_ID].navigatingURL.handlers[0]
    web = SimpleNamespace(navigationURL="https://easyeda.com/x", launchExternally=False)
    local = SimpleNamespace(navigationURL="file:///C:/x/index.html", launchExternally=False)
    handler.notify(web)
    handler.notify(local)
    assert web.launchExternally is True
    assert local.launchExternally is False


def test_command_button_shows_the_palette_again(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stack: Stack
) -> None:
    ui = FakeUi()
    addin, _ = make_palette_app(fa, monkeypatch, ui, tmp_path, stack)
    addin.start()
    palette = ui.palettes.items[fa.PALETTE_ID]
    palette.isVisible = False  # user closed it
    created = ui.commandDefinitions.items[fa.COMMAND_ID].commandCreated.handlers[0]
    command = SimpleNamespace(execute=FakeEvent())
    created.notify(SimpleNamespace(command=command))
    assert len(command.execute.handlers) == 1
    command.execute.handlers[0].notify(None)
    assert palette.isVisible is True
    assert len(ui.palettes.items) == 1


def test_palette_mode_still_shows_the_palette_when_no_panel_exists(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stack: Stack
) -> None:
    ui = FakeUi({})
    addin, fake = make_palette_app(fa, monkeypatch, ui, tmp_path, stack)
    addin.start()
    assert ui.palettes.items[fa.PALETTE_ID].isVisible is True
    assert fa.COMMAND_ID in ui.commandDefinitions.items
    assert any("No toolbar panel" in line for line in fake.logged)
    addin.stop()


def test_palette_mode_stop_continues_after_a_failing_cleanup_step(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stack: Stack
) -> None:
    ui = FakeUi()
    addin, fake = make_palette_app(fa, monkeypatch, ui, tmp_path, stack)
    addin.start()

    def explode() -> bool:
        raise RuntimeError("control already gone")

    ui.design_panel.controls.items[fa.COMMAND_ID].deleteMe = explode  # type: ignore[method-assign]
    addin.stop()
    assert ui.palettes.items[fa.PALETTE_ID].deleted is True
    assert ui.commandDefinitions.items[fa.COMMAND_ID].deleted is True
    assert any("control already gone" in line for line in fake.logged)


def test_report_error_logs_and_shows_a_message_box(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    ui = FakeUi({})
    fake = FakeApp(ui)
    monkeypatch.setattr(fa.adsk.core.Application, "get", staticmethod(lambda: fake))
    fa.report_error("LCSC2Fusion failed to start", "Traceback: boom")
    assert ui.boxes
    assert "Traceback: boom" in ui.boxes[0][1]
    assert any("Traceback: boom" in line for line in fake.logged)


# Entry point ---------------------------------------------------------------------------------


@pytest.fixture
def preserved_modules() -> Iterator[None]:
    """`stop()` purges the add-in modules from sys.modules; put everything back afterwards."""
    saved = dict(sys.modules)
    yield
    for name in set(sys.modules) - set(saved):
        del sys.modules[name]
    sys.modules.update(saved)


STARTUP = json.dumps({"IsApplicationStartup": True})
MANUAL = json.dumps({"IsApplicationStartup": False})


@pytest.mark.parametrize(
    ("context", "expected"),
    [
        (STARTUP, False),
        (STARTUP.encode(), False),
        ({"IsApplicationStartup": True}, False),
        ('{"IsApplicationStartup": "true"}', False),
        (MANUAL, True),
        ("{}", True),
        ('{"SomethingElse": 1}', True),
        ("", True),
        ("not json at all", True),
        ("[1, 2]", True),
        ('"IsApplicationStartup"', True),
        (b"\xff\xfe", True),
        (None, True),
        (42, True),
        ('{"IsApplicationStartup": null}', True),
        ('{"IsApplicationStartup": 1}', True),
    ],
)
def test_the_window_is_shown_unless_fusion_reports_an_application_startup(
    context: Any, expected: bool
) -> None:
    entry = importlib.import_module("LCSC2Fusion")
    assert entry.should_show_window(context) is expected


def run_entry_point(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, context: Any
) -> tuple[Any, FakeApp, FakeUi]:
    ui = FakeUi()
    fake = FakeApp(ui)
    monkeypatch.setattr(fa.adsk.core.Application, "get", staticmethod(lambda: fake))
    entry = importlib.import_module("LCSC2Fusion")
    entry.run(context)
    return entry, fake, ui


def test_entry_point_runs_and_stops_the_add_in(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, preserved_modules: None, stack: Stack
) -> None:
    entry, _, ui = run_entry_point(fa, monkeypatch, MANUAL)
    assert stack.window is not None
    assert len(stack.window.launched) == 1
    assert fa.COMMAND_ID in ui.design_panel.controls.items
    for module in ("http_transport", "browser_window", "controller"):
        assert module in sys.modules

    entry.stop(None)

    assert ui.design_panel.controls.items[fa.COMMAND_ID].deleted is True
    assert stack.window.closed == 1
    for module in ("controller", "http_transport", "browser_window", "core_service"):
        assert module not in sys.modules  # purged so an edited add-in reloads cleanly
    assert entry._app is None


def test_entry_point_shows_no_window_when_fusion_starts_the_add_in_at_startup(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, preserved_modules: None, stack: Stack
) -> None:
    entry, _, ui = run_entry_point(fa, monkeypatch, STARTUP)

    assert stack.window is not None
    assert stack.window.launched == []
    assert stack.transport is not None
    assert stack.transport.started is True
    assert fa.COMMAND_ID in ui.design_panel.controls.items

    entry.stop(None)


def test_entry_point_shows_the_window_for_a_garbage_context(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, preserved_modules: None, stack: Stack
) -> None:
    entry, _, _ = run_entry_point(fa, monkeypatch, "{{{ not json")

    assert stack.window is not None
    assert len(stack.window.launched) == 1

    entry.stop(None)


def test_entry_point_reports_a_failing_start_with_a_message_box(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, preserved_modules: None, stack: Stack
) -> None:
    ui = FakeUi()
    fake = FakeApp(ui)
    monkeypatch.setattr(fa.adsk.core.Application, "get", staticmethod(lambda: fake))
    original = fa.HttpTransport

    def failing_transport(*args: Any, **kwargs: Any) -> FakeTransport:
        transport = original(*args, **kwargs)
        transport.start_error = OSError("address already in use")
        return transport

    monkeypatch.setattr(fa, "HttpTransport", failing_transport)
    entry = importlib.import_module("LCSC2Fusion")

    entry.run(None)  # must not raise

    assert ui.boxes
    assert "address already in use" in ui.boxes[0][1]
    assert "Traceback" in ui.boxes[0][1]
    assert any("address already in use" in line for line in fake.logged)
    assert entry._app is None
    assert stack.hub is not None
    assert stack.hub.closed is True  # partial start cleaned up
    assert fake.registered[fa.DISPATCH_EVENT_ID].handlers == []


def test_entry_point_reports_a_failing_palette_start_with_a_message_box(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, preserved_modules: None
) -> None:
    monkeypatch.setenv(fa.UI_ENV_VAR, "palette")
    ui = FakeUi()

    def failing_add(*args: Any) -> None:
        raise RuntimeError("palette creation exploded")

    ui.palettes.add = failing_add  # type: ignore[method-assign,assignment]
    fake = FakeApp(ui)
    monkeypatch.setattr(fa.adsk.core.Application, "get", staticmethod(lambda: fake))
    entry = importlib.import_module("LCSC2Fusion")

    entry.run(None)  # must not raise

    assert ui.boxes
    assert "palette creation exploded" in ui.boxes[0][1]
    assert "Traceback" in ui.boxes[0][1]
    assert any("palette creation exploded" in line for line in fake.logged)
    assert entry._app is None
    assert ui.commandDefinitions.items[fa.COMMAND_ID].deleted is True  # partial start cleaned up


# Packaging -----------------------------------------------------------------------------------


def test_the_built_package_contains_the_window_modules_and_the_bridge(tmp_path: Path) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "build_addin_for_adapter_tests", ROOT / "scripts" / "build_addin.py"
    )
    assert spec is not None
    assert spec.loader is not None
    build_addin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = build_addin  # dataclasses look the defining module up here
    try:
        spec.loader.exec_module(build_addin)
        result = build_addin.build(root=ROOT, dist=tmp_path)
    finally:
        del sys.modules[spec.name]

    with zipfile.ZipFile(result.zip_path) as archive:
        names = set(archive.namelist())
    for member in (
        "LCSC2Fusion/http_transport.py",
        "LCSC2Fusion/browser_window.py",
        "LCSC2Fusion/palette/http-bridge.js",
        "LCSC2Fusion/fusion_adapter.py",
        "LCSC2Fusion/controller.py",
    ):
        assert member in names


# End to end with the real transport ----------------------------------------------------------


class FakeProcess:
    """Stands in for the browser process that `BrowserWindow` would start."""

    pid = 4242

    def __init__(self) -> None:
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return 0 if self.terminated or self.killed else None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        return 0


class FakeWindowApi:
    """The Win32 window calls behind `BrowserWindow.focus`."""

    def __init__(self) -> None:
        self.asked_for: list[int] = []
        self.raised: list[int] = []

    def top_level_windows(self, pid: int) -> list[int]:
        self.asked_for.append(pid)
        return [77]

    def bring_to_front(self, hwnd: int) -> bool:
        self.raised.append(hwnd)
        return True


class LoopFakeApp(FakeApp):
    """A fake application whose custom events run on the simulated main thread."""

    def __init__(self, ui: FakeUi, loop: MainLoop) -> None:
        super().__init__(ui)
        self.loop = loop

    def fireCustomEvent(self, event_id: str, info: str = "") -> bool:
        super().fireCustomEvent(event_id, info)
        event = self.registered.get(event_id)
        if event is not None:
            self.loop.post(lambda: [handler.notify(None) for handler in list(event.handlers)])
        return True


class EventStream:
    """Reads a Server-Sent Events response in a background thread."""

    def __init__(self, response: http.client.HTTPResponse) -> None:
        self.events: list[dict[str, str]] = []
        self.closed = threading.Event()
        self._response = response
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        try:
            while True:
                line = self._response.readline()
                if not line:
                    return
                text = line.decode("ascii").strip()
                if text.startswith("data:"):
                    event = json.loads(text[len("data:") :])
                    self.events.append(event)
                    if event["action"] == "shutdown":
                        self._response.close()  # the page closes its window
                        return
        except (OSError, http.client.HTTPException):
            return
        finally:
            self.closed.set()

    def wait_for(self, count: int, timeout_s: float = 10.0) -> list[dict[str, str]]:
        deadline = time.monotonic() + timeout_s
        while len(self.events) < count:
            assert time.monotonic() < deadline, f"only got {self.events}"
            time.sleep(0.01)
        return list(self.events)


def test_end_to_end_with_the_real_transport_hub_and_window(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    browser_window = importlib.import_module("browser_window")
    controller = importlib.import_module("controller")
    commands: list[list[str]] = []
    processes: list[FakeProcess] = []

    def fake_popen(command: list[str], **options: Any) -> FakeProcess:
        commands.append(command)
        processes.append(FakeProcess())
        return processes[-1]

    def make_window(*, log: Any) -> Any:
        return browser_window.BrowserWindow(
            log=log,
            finder=lambda: browser_window.BrowserChoice("edge", Path("C:/fake/msedge.exe")),
            popen=fake_popen,
            profile_dir=tmp_path / "profile",
            window_api=window_api,
        )

    window_api = FakeWindowApi()
    handled_on: list[int] = []
    real_handle = controller.Controller.handle

    def recording_handle(self: Any, action: str, data: str) -> str:
        handled_on.append(threading.get_ident())
        return real_handle(self, action, data)

    monkeypatch.setattr(controller.Controller, "handle", recording_handle)
    monkeypatch.setattr(fa, "BrowserWindow", make_window)
    loop = MainLoop()
    ui = FakeUi()
    fake = LoopFakeApp(ui, loop)
    monkeypatch.setattr(fa.adsk.core.Application, "get", staticmethod(lambda: fake))
    addin = fa.AddinApp(ADDIN_DIR)
    stream: EventStream | None = None
    try:
        loop.call(lambda: addin.start(True))  # Fusion's main thread runs the add-in start

        # The browser was started with the launch URL: open it like the browser would.
        assert len(commands) == 1
        (app_argument,) = [arg for arg in commands[0] if arg.startswith("--app=")]
        launch = urlsplit(app_argument.removeprefix("--app="))
        assert launch.hostname == "127.0.0.1"
        assert launch.port is not None
        port = launch.port
        host = f"127.0.0.1:{port}"

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", f"{launch.path}?{launch.query}")
        redirect = conn.getresponse()
        redirect.read()
        assert redirect.status == 302
        assert redirect.getheader("Location") == "/"
        cookie = (redirect.getheader("Set-Cookie") or "").split(";")[0]
        assert cookie.startswith("lcsc_s=")
        conn.close()

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/", headers={"Cookie": cookie})
        page = conn.getresponse()
        assert page.status == 200
        assert b"LCSC2Fusion" in page.read()
        conn.close()

        events = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        events.request("GET", "/api/events", headers={"Cookie": cookie})
        stream_response = events.getresponse()
        assert stream_response.status == 200
        stream = EventStream(stream_response)
        deadline = time.monotonic() + 10
        assert addin._hub is not None
        while addin._hub.subscriber_count < 1:
            assert time.monotonic() < deadline
            time.sleep(0.01)

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        body = json.dumps({"action": "ready", "data": "{}"})
        conn.request(
            "POST",
            "/api/act",
            body=body,
            headers={
                "Cookie": cookie,
                "Origin": f"http://{host}",
                "Content-Type": "application/json",
                "X-LCSC": "1",
            },
        )
        reply = conn.getresponse()
        assert reply.status == 200
        assert json.loads(reply.read()) == {"reply": "OK"}
        conn.close()

        received = stream.wait_for(2)
        assert [event["action"] for event in received] == ["busy", "status"]
        assert json.loads(received[0]["data"]) == {"value": False}
        assert json.loads(received[1]["data"])["level"] == "info"
        assert handled_on == [loop.ident]  # the controller ran on the main thread, not an HTTP one

        # A second click on the button while the page is connected only focuses the window.
        loop.call(addin.show_window)
        assert len(commands) == 1
        assert window_api.asked_for == [4242]
        assert window_api.raised == [77]

        loop.call(addin.stop)  # Stop in the Add-Ins dialog
        assert stream.closed.wait(10)
        assert stream.events[-1]["action"] == "shutdown"
        assert processes[0].terminated is True
        with pytest.raises(OSError):
            http.client.HTTPConnection("127.0.0.1", port, timeout=2).request("GET", "/api/ping")
        loop.call(lambda: None)
        assert "t=" not in "\n".join(fake.logged)  # the launch token never reached the log
    finally:
        loop.call(addin.stop)
        loop.close()
        if stream is not None:
            stream.closed.wait(5)
