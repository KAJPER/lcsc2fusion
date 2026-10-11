"""Tests for fusion_adapter against the official adsk stubs (skipped when they are missing).

The stubs provide the real base classes of the Fusion event handlers. Everything else Fusion
provides (application, UI, palette) is replaced by small recording fakes.
"""

from __future__ import annotations

import importlib
import json
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.usefixtures("adsk_stubs")


@pytest.fixture(scope="module")
def fa(adsk_stubs: Path) -> ModuleType:
    return importlib.import_module("fusion_adapter")


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
        return self.items.get(palette_id)

    def add(self, palette_id: str, *args: Any) -> FakePalette:
        palette = FakePalette(palette_id, args)
        self.items[palette_id] = palette
        return palette


class FakeControl:
    def __init__(self, definition: Any) -> None:
        self.definition = definition
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


class FakeUi:
    def __init__(
        self, *, workspace_panel: FakePanel | None, generic_panel: FakePanel | None
    ) -> None:
        self.palettes = FakePalettes()
        self.commandDefinitions = FakeDefinitions()
        workspace = (
            None
            if workspace_panel is None
            else SimpleNamespace(
                toolbarPanels=SimpleNamespace(itemById=lambda _id: workspace_panel)
            )
        )
        self.workspaces = SimpleNamespace(itemById=lambda _id: workspace)
        self.allToolbarPanels = SimpleNamespace(itemById=lambda _id: generic_panel)
        self.boxes: list[tuple[str, str]] = []

    def messageBox(self, text: str, title: str = "") -> None:
        self.boxes.append((title, text))


class FakeApp:
    def __init__(self, ui: FakeUi | None = None) -> None:
        self.userInterface = ui
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
    ui = FakeUi(workspace_panel=None, generic_panel=None)
    palette = ui.palettes.add(fa.PALETTE_ID)
    fa.PaletteSinkAdapter(ui).send("status", '{"level": "info"}')
    assert palette.sent == [("status", '{"level": "info"}')]
    assert app.logged == []


def test_sink_survives_a_missing_palette_and_an_empty_reply(fa: ModuleType, app: FakeApp) -> None:
    ui = FakeUi(workspace_panel=None, generic_panel=None)
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
    ui = FakeUi(workspace_panel=None, generic_panel=None)
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


# AddinApp wiring -----------------------------------------------------------------------------


def make_app(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, ui: FakeUi, tmp_path: Path
) -> tuple[Any, FakeApp]:
    fake = FakeApp(ui)
    monkeypatch.setattr(fa.adsk.core.Application, "get", staticmethod(lambda: fake))
    return fa.AddinApp(tmp_path / "LCSC2Fusion"), fake


def test_start_builds_palette_command_and_control_then_stop_removes_everything(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    panel = FakePanel()
    ui = FakeUi(workspace_panel=panel, generic_panel=None)
    addin, fake = make_app(fa, monkeypatch, ui, tmp_path)

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
    assert fa.COMMAND_ID in panel.controls.items
    assert fa.CUSTOM_EVENT_ID in fake.registered

    addin.stop()

    assert palette.deleted is True
    assert definition.deleted is True
    assert panel.controls.items[fa.COMMAND_ID].deleted is True
    assert fake.registered[fa.CUSTOM_EVENT_ID].handlers == []
    assert fake.unregistered[-1] == fa.CUSTOM_EVENT_ID


def test_palette_events_reach_the_controller_and_reply(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ui = FakeUi(workspace_panel=FakePanel(), generic_panel=None)
    addin, _ = make_app(fa, monkeypatch, ui, tmp_path)
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


def start_with_library(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Any, FakeApp, FakePalette, Path]:
    """Start the add-in with its output directory redirected into tmp_path."""
    libs = tmp_path / "libs"
    libs.mkdir()
    monkeypatch.setattr(importlib.import_module("controller"), "default_out_dir", lambda: libs)
    ui = FakeUi(workspace_panel=FakePanel(), generic_panel=None)
    addin, fake = make_app(fa, monkeypatch, ui, tmp_path)
    addin.start()
    return addin, fake, ui.palettes.items[fa.PALETTE_ID], libs


def test_open_library_runs_the_text_command_inside_the_palette_handler(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, fake, palette, libs = start_with_library(fa, monkeypatch, tmp_path)
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
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, fake, palette, libs = start_with_library(fa, monkeypatch, tmp_path)
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
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, fake, palette, _ = start_with_library(fa, monkeypatch, tmp_path)
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
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    core_service = importlib.import_module("core_service")
    controller = importlib.import_module("controller")
    _, fake, palette, libs = start_with_library(fa, monkeypatch, tmp_path)

    def fake_add_part(self: Any, lcsc_id: str, out_dir: Path) -> Any:
        path = out_dir / f"{lcsc_id}.lbr"
        path.write_text("<eagle/>", encoding="utf-8")
        return controller.AddedPart(path=path, mpn="RP2040")

    monkeypatch.setattr(core_service.CorePartService, "add_part", fake_add_part)
    event = SimpleNamespace(action="add", data='{"lcsc_id": "C2040"}', returnData="")
    palette.incomingFromHTML.handlers[0].notify(event)
    assert fake.fired.wait(5)
    fake.registered[fa.CUSTOM_EVENT_ID].handlers[0].notify(None)  # main thread completion

    added = [json.loads(data) for action, data in palette.sent if action == "added"]
    assert len(added) == 1
    assert added[0]["opened"] is False
    assert added[0]["path"] == str(libs / "C2040.lbr")
    assert fake.commands == []  # opening needs the user's click on "Open in Fusion Electronics"


def test_web_links_are_opened_externally_and_local_pages_are_not(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ui = FakeUi(workspace_panel=FakePanel(), generic_panel=None)
    addin, _ = make_app(fa, monkeypatch, ui, tmp_path)
    addin.start()
    handler = ui.palettes.items[fa.PALETTE_ID].navigatingURL.handlers[0]
    web = SimpleNamespace(navigationURL="https://easyeda.com/x", launchExternally=False)
    local = SimpleNamespace(navigationURL="file:///C:/x/index.html", launchExternally=False)
    handler.notify(web)
    handler.notify(local)
    assert web.launchExternally is True
    assert local.launchExternally is False


def test_command_button_shows_the_palette_again(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ui = FakeUi(workspace_panel=FakePanel(), generic_panel=None)
    addin, _ = make_app(fa, monkeypatch, ui, tmp_path)
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


def test_falls_back_to_the_generic_panel_when_the_workspace_is_missing(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    generic = FakePanel()
    ui = FakeUi(workspace_panel=None, generic_panel=generic)
    addin, fake = make_app(fa, monkeypatch, ui, tmp_path)
    addin.start()
    assert fa.COMMAND_ID in generic.controls.items
    assert any("not found" in line for line in fake.logged)
    addin.stop()
    assert generic.controls.items[fa.COMMAND_ID].deleted is True


def test_start_still_shows_the_palette_when_no_panel_exists(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ui = FakeUi(workspace_panel=None, generic_panel=None)
    addin, fake = make_app(fa, monkeypatch, ui, tmp_path)
    addin.start()
    assert ui.palettes.items[fa.PALETTE_ID].isVisible is True
    assert fa.COMMAND_ID in ui.commandDefinitions.items
    assert any("No toolbar panel" in line for line in fake.logged)
    addin.stop()


def test_stop_continues_after_a_failing_cleanup_step(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    panel = FakePanel()
    ui = FakeUi(workspace_panel=panel, generic_panel=None)
    addin, fake = make_app(fa, monkeypatch, ui, tmp_path)
    addin.start()

    def explode() -> bool:
        raise RuntimeError("control already gone")

    panel.controls.items[fa.COMMAND_ID].deleteMe = explode  # type: ignore[method-assign]
    addin.stop()
    assert ui.palettes.items[fa.PALETTE_ID].deleted is True
    assert ui.commandDefinitions.items[fa.COMMAND_ID].deleted is True
    assert any("control already gone" in line for line in fake.logged)


def test_report_error_logs_and_shows_a_message_box(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    ui = FakeUi(workspace_panel=None, generic_panel=None)
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


def test_entry_point_runs_and_stops_the_add_in(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, preserved_modules: None
) -> None:
    panel = FakePanel()
    ui = FakeUi(workspace_panel=panel, generic_panel=None)
    fake = FakeApp(ui)
    monkeypatch.setattr(fa.adsk.core.Application, "get", staticmethod(lambda: fake))
    entry = importlib.import_module("LCSC2Fusion")

    entry.run(None)
    assert fa.PALETTE_ID in ui.palettes.items
    assert fa.COMMAND_ID in panel.controls.items

    entry.stop(None)
    assert ui.palettes.items[fa.PALETTE_ID].deleted is True
    assert panel.controls.items[fa.COMMAND_ID].deleted is True
    assert "controller" not in sys.modules  # purged so an edited add-in reloads cleanly


def test_entry_point_reports_a_failing_start_with_a_message_box(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, preserved_modules: None
) -> None:
    ui = FakeUi(workspace_panel=FakePanel(), generic_panel=None)

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
