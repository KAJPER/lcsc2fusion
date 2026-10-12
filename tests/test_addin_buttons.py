"""Tests for the toolbar buttons of fusion_adapter (register, clean up, keep alive).

Toolbar objects are replaced by small recording fakes that are built from a layout dictionary, so
tests can make workspaces, tabs and panels missing, or appear later like a lazily loaded
environment. Needs the official adsk stubs for the handler base classes (skipped without them).
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.usefixtures("adsk_stubs")

Layout = dict[tuple[str, str, str], "FakePanel"]


@pytest.fixture(scope="module")
def fa(adsk_stubs: Path) -> ModuleType:
    return importlib.import_module("fusion_adapter")


# Fakes ---------------------------------------------------------------------------------------


class FakeEvent:
    def __init__(self) -> None:
        self.handlers: list[Any] = []

    def add(self, handler: Any) -> bool:
        self.handlers.append(handler)
        return True

    def remove(self, handler: Any) -> bool:
        self.handlers.remove(handler)
        return True

    def fire(self) -> None:
        for handler in list(self.handlers):
            handler.notify(None)


class FakeControl:
    def __init__(self, definition: Any) -> None:
        self.definition = definition
        self.isPromoted = False
        self.deleted = False
        self.delete_error: Exception | None = None

    def deleteMe(self) -> bool:
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted = True
        return True


class FakeControls:
    def __init__(self) -> None:
        self.items: dict[str, FakeControl] = {}
        self.add_error: Exception | None = None
        self.added = 0

    def addCommand(self, definition: Any) -> FakeControl:
        if self.add_error is not None:
            raise self.add_error
        control = FakeControl(definition)
        self.items[definition.id] = control
        self.added += 1
        return control

    def itemById(self, control_id: str) -> FakeControl | None:
        control = self.items.get(control_id)
        return None if control is None or control.deleted else control


class FakeCollection:
    """The slice of adsk.core.ObjectCollection that is used: count and item(i)."""

    def __init__(self, ids: list[str]) -> None:
        self._ids = ids

    @property
    def count(self) -> int:
        return len(self._ids)

    def item(self, index: int) -> Any:
        return SimpleNamespace(id=self._ids[index])


class FakePanel:
    def __init__(self, related: list[str] | None = None) -> None:
        self.controls = FakeControls()
        self._related = related
        self.related_error: Exception | None = None

    @property
    def relatedWorkspaces(self) -> FakeCollection | None:
        if self.related_error is not None:
            raise self.related_error
        return None if self._related is None else FakeCollection(self._related)


class Lookup:
    def __init__(self, items: dict[str, Any], error: Exception | None = None) -> None:
        self._items = items
        self._error = error

    def itemById(self, item_id: str) -> Any:
        if self._error is not None:
            raise self._error
        return self._items.get(item_id)


class FakeWorkspaces:
    def __init__(self, ui: FakeUi) -> None:
        self._ui = ui

    def itemById(self, workspace_id: str) -> Any:
        if self._ui.workspace_error is not None:
            raise self._ui.workspace_error
        tabs: dict[str, dict[str, FakePanel]] = {}
        for (workspace, tab, panel_id), panel in self._ui.layout.items():
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


class FakeAllPanels:
    def __init__(self, ui: FakeUi) -> None:
        self._ui = ui

    def itemById(self, panel_id: str) -> FakePanel | None:
        for (_, _, known), panel in self._ui.layout.items():
            if known == panel_id:
                return panel
        return self._ui.extra_panels.get(panel_id)


class FakeDefinition:
    def __init__(self, definition_id: str, name: str, tooltip: str, resources: str) -> None:
        self.id = definition_id
        self.name = name
        self.commandCreated = FakeEvent()
        self.controlDefinition = SimpleNamespace(isEnabled=False)
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
    def __init__(self, layout: Layout) -> None:
        self.layout = layout
        self.extra_panels: dict[str, FakePanel] = {}  # reachable only through allToolbarPanels
        self.workspace_error: Exception | None = None
        self.workspaces = FakeWorkspaces(self)
        self.allToolbarPanels = FakeAllPanels(self)
        self.palettes = Lookup({})
        self.commandDefinitions = FakeDefinitions()
        self.workspaceActivated = FakeEvent()
        self.boxes: list[str] = []

    def messageBox(self, text: str, title: str = "") -> None:
        self.boxes.append(text)


def full_layout(fa: ModuleType) -> Layout:
    return {(t.workspace, t.tab, t.panel): FakePanel(related=[t.workspace]) for t in fa.TARGETS}


def definition_stub(fa: ModuleType) -> Any:
    return SimpleNamespace(id=fa.COMMAND_ID)


# register_buttons ----------------------------------------------------------------------------


def test_targets_cover_the_four_electronics_environments_and_design(fa: ModuleType) -> None:
    assert [(t.workspace, t.tab, t.panel) for t in fa.TARGETS] == [
        ("FusionSolidEnvironment", "ToolsTab", "SolidScriptsAddinsPanel"),
        ("ElectronEmptyLbrEnvironment", "ToolsTab", "EagleLbrScriptsAddinsPanel"),
        ("SchEditorEnvironement", "ToolsTab", "EagleSchScriptsAddinsPanel"),
        ("BoardLayoutEnvironement", "ToolsTab", "EaglePcbScriptsAddinsPanel"),
        ("PCBDesignEnvironement", "ToolsTab", "EagleScriptsAddinsPanel"),
    ]


def test_every_present_panel_gets_one_promoted_control(fa: ModuleType) -> None:
    layout = full_layout(fa)
    ui = FakeUi(layout)
    definition = definition_stub(fa)
    logs: list[str] = []

    added = fa.register_buttons(ui, definition, logs.append)

    assert added == [t.panel for t in fa.TARGETS]
    for panel in layout.values():
        control = panel.controls.items[fa.COMMAND_ID]
        assert control.isPromoted is True  # otherwise it hides in the panel dropdown
        assert control.definition is definition
        assert panel.controls.added == 1
    assert len(logs) == len(fa.TARGETS)
    assert logs[0].startswith("button: FusionSolidEnvironment / SolidScriptsAddinsPanel -> added")
    assert "[panel shown in: FusionSolidEnvironment]" in logs[0]  # to confirm the id mapping


def test_missing_workspace_tab_and_panel_are_logged_and_skipped(fa: ModuleType) -> None:
    layout = full_layout(fa)
    del layout[("ElectronEmptyLbrEnvironment", "ToolsTab", "EagleLbrScriptsAddinsPanel")]
    layout[("SchEditorEnvironement", "OtherTab", "UnrelatedPanelA")] = layout.pop(
        ("SchEditorEnvironement", "ToolsTab", "EagleSchScriptsAddinsPanel")
    )
    layout[("BoardLayoutEnvironement", "ToolsTab", "UnrelatedPanelB")] = layout.pop(
        ("BoardLayoutEnvironement", "ToolsTab", "EaglePcbScriptsAddinsPanel")
    )
    ui = FakeUi(layout)
    logs: list[str] = []

    added = fa.register_buttons(ui, definition_stub(fa), logs.append)

    assert added == ["SolidScriptsAddinsPanel", "EagleScriptsAddinsPanel"]
    by_workspace = {line.split(" / ")[0]: line for line in logs}
    assert "-> missing (workspace not found)" in by_workspace["button: ElectronEmptyLbrEnvironment"]
    assert "-> missing (tab not found)" in by_workspace["button: SchEditorEnvironement"]
    assert "-> missing (panel not found)" in by_workspace["button: BoardLayoutEnvironement"]


def test_a_panel_found_only_through_all_toolbar_panels_is_still_used(fa: ModuleType) -> None:
    layout = full_layout(fa)
    lbr = layout.pop(("ElectronEmptyLbrEnvironment", "ToolsTab", "EagleLbrScriptsAddinsPanel"))
    ui = FakeUi(layout)
    ui.extra_panels["EagleLbrScriptsAddinsPanel"] = lbr

    added = fa.register_buttons(ui, definition_stub(fa), lambda _: None)

    assert "EagleLbrScriptsAddinsPanel" in added
    assert fa.COMMAND_ID in lbr.controls.items


def test_a_failing_lookup_is_logged_and_does_not_stop_the_other_targets(fa: ModuleType) -> None:
    ui = FakeUi(full_layout(fa))
    ui.workspace_error = RuntimeError("workspace not loaded")
    logs: list[str] = []

    added = fa.register_buttons(ui, definition_stub(fa), logs.append)

    # the generic panel lookup still finds every panel
    assert added == [t.panel for t in fa.TARGETS]
    ui.layout.clear()
    logs.clear()
    assert fa.register_buttons(ui, definition_stub(fa), logs.append) == []
    assert all("lookup failed: RuntimeError: workspace not loaded" in line for line in logs)


def test_an_existing_control_is_left_alone_and_not_reported_as_added(fa: ModuleType) -> None:
    layout = full_layout(fa)
    first = layout[("FusionSolidEnvironment", "ToolsTab", "SolidScriptsAddinsPanel")]
    existing = first.controls.addCommand(definition_stub(fa))
    first.controls.added = 0
    logs: list[str] = []

    added = fa.register_buttons(FakeUi(layout), definition_stub(fa), logs.append)

    assert "SolidScriptsAddinsPanel" not in added
    assert first.controls.added == 0
    assert first.controls.items[fa.COMMAND_ID] is existing
    assert logs[0] == "button: FusionSolidEnvironment / SolidScriptsAddinsPanel -> exists"
    assert len(added) == len(fa.TARGETS) - 1


def test_a_panel_shared_by_two_targets_gets_a_single_control(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = (
        fa.ButtonTarget("EnvA", "ToolsTab", "SharedPanel"),
        fa.ButtonTarget("EnvB", "ToolsTab", "SharedPanel"),
    )
    monkeypatch.setattr(fa, "TARGETS", shared)
    panel = FakePanel()
    ui = FakeUi(
        {("EnvA", "ToolsTab", "SharedPanel"): panel, ("EnvB", "ToolsTab", "SharedPanel"): panel}
    )
    logs: list[str] = []

    added = fa.register_buttons(ui, definition_stub(fa), logs.append)

    assert added == ["SharedPanel"]
    assert panel.controls.added == 1
    assert logs[1] == "button: EnvB / SharedPanel -> exists (shared panel)"


def test_a_raising_add_command_is_logged_and_the_other_panels_still_get_their_button(
    fa: ModuleType,
) -> None:
    layout = full_layout(fa)
    layout[
        ("SchEditorEnvironement", "ToolsTab", "EagleSchScriptsAddinsPanel")
    ].controls.add_error = RuntimeError("panel is read only")
    logs: list[str] = []

    added = fa.register_buttons(FakeUi(layout), definition_stub(fa), logs.append)

    assert "EagleSchScriptsAddinsPanel" not in added
    assert len(added) == len(fa.TARGETS) - 1
    failed = [line for line in logs if "-> failed" in line]
    assert failed == [
        "button: SchEditorEnvironement / EagleSchScriptsAddinsPanel -> "
        "failed (RuntimeError: panel is read only)"
    ]


def test_a_control_that_cannot_be_promoted_still_counts_as_added(fa: ModuleType) -> None:
    class Unpromotable:
        deleted = False

        def __init__(self, definition: Any) -> None:
            self.definition = definition

        @property
        def isPromoted(self) -> bool:
            return False

        @isPromoted.setter
        def isPromoted(self, value: bool) -> None:
            raise RuntimeError("no promotion here")

    layout = full_layout(fa)
    panel = layout[("FusionSolidEnvironment", "ToolsTab", "SolidScriptsAddinsPanel")]
    panel.controls.addCommand = lambda d: panel.controls.items.setdefault(d.id, Unpromotable(d))  # type: ignore[method-assign,assignment]
    logs: list[str] = []

    added = fa.register_buttons(FakeUi(layout), definition_stub(fa), logs.append)

    assert "SolidScriptsAddinsPanel" in added
    assert "could not promote it (RuntimeError: no promotion here)" in logs[0]


def test_related_workspaces_are_optional_information(fa: ModuleType) -> None:
    layout = full_layout(fa)
    keys = list(layout)
    layout[keys[0]].related_error = RuntimeError("not supported")
    layout[keys[1]] = FakePanel(related=None)
    layout[keys[2]] = FakePanel(related=["A", "B"])
    logs: list[str] = []

    fa.register_buttons(FakeUi(layout), definition_stub(fa), logs.append)

    assert logs[0].endswith("-> added")
    assert logs[1].endswith("-> added")
    assert logs[2].endswith("-> added [panel shown in: A, B]")


# unregister_buttons --------------------------------------------------------------------------


def test_unregister_removes_exactly_the_controls_that_were_added(fa: ModuleType) -> None:
    layout = full_layout(fa)
    solid = layout[("FusionSolidEnvironment", "ToolsTab", "SolidScriptsAddinsPanel")]
    foreign = solid.controls.addCommand(SimpleNamespace(id="SomeoneElse_Command"))
    ui = FakeUi(layout)
    added = fa.register_buttons(ui, definition_stub(fa), lambda _: None)
    ours = {panel_id: p.controls.items[fa.COMMAND_ID] for (_, _, panel_id), p in layout.items()}

    fa.unregister_buttons(ui, added[:3], lambda _: None)

    assert [ours[panel_id].deleted for panel_id in added] == [True, True, True, False, False]
    assert foreign.deleted is False

    fa.unregister_buttons(ui, added, lambda _: None)  # idempotent, finishes the rest
    assert all(control.deleted for control in ours.values())
    assert foreign.deleted is False


def test_unregister_continues_after_a_failure_and_ignores_unknown_panels(fa: ModuleType) -> None:
    layout = full_layout(fa)
    ui = FakeUi(layout)
    added = fa.register_buttons(ui, definition_stub(fa), lambda _: None)
    first = layout[("FusionSolidEnvironment", "ToolsTab", "SolidScriptsAddinsPanel")]
    first.controls.items[fa.COMMAND_ID].delete_error = RuntimeError("control already gone")
    logs: list[str] = []

    fa.unregister_buttons(ui, [*added, "NoSuchPanel"], logs.append)

    assert logs == [
        "button: could not remove the control from SolidScriptsAddinsPanel "
        "(RuntimeError('control already gone'))"
    ]
    for key, panel in layout.items():
        if key[2] != "SolidScriptsAddinsPanel":
            assert panel.controls.items[fa.COMMAND_ID].deleted is True


# AddinApp: keep the button alive across environment switches ----------------------------------


class FakeApp:
    def __init__(self, ui: FakeUi) -> None:
        self.userInterface = ui
        self.documentActivated = FakeEvent()
        self.logged: list[str] = []
        self.registered: dict[str, FakeEvent] = {}

    def log(self, message: str, *args: Any) -> None:
        if not args:  # the adapter logs every line twice: console and file
            self.logged.append(message)

    def registerCustomEvent(self, event_id: str) -> FakeEvent:
        self.registered[event_id] = FakeEvent()
        return self.registered[event_id]

    def unregisterCustomEvent(self, event_id: str) -> bool:
        return True

    def fireCustomEvent(self, event_id: str, info: str = "") -> bool:
        return True


class FakeHub:
    subscriber_count = 0

    def publish(self, action: str, data: str) -> int:
        return 1

    def close(self) -> None:
        pass


class FakeTransport:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def launch_url(self) -> str:
        return "http://127.0.0.1:1/?t=secret-token"


class FakeWindow:
    def __init__(self, **kwargs: Any) -> None:
        pass

    def is_alive(self) -> bool:
        return False

    def launch(self, url: str) -> Any:
        return SimpleNamespace(mode="app", pid=1, browser="edge")

    def close(self) -> None:
        pass

    def focus(self) -> bool:
        return True


def start_app(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, ui: FakeUi, tmp_path: Path
) -> tuple[Any, FakeApp]:
    fake = FakeApp(ui)
    monkeypatch.setattr(fa.adsk.core.Application, "get", staticmethod(lambda: fake))
    monkeypatch.setattr(fa, "Hub", FakeHub)
    monkeypatch.setattr(fa, "HttpTransport", FakeTransport)
    monkeypatch.setattr(fa, "BrowserWindow", FakeWindow)
    monkeypatch.setattr(fa, "SHUTDOWN_WAIT_S", 0.0)
    addin = fa.AddinApp(tmp_path / "LCSC2Fusion")
    addin.start(show_window=False)
    return addin, fake


def our_controls(layout: Layout, fa: ModuleType) -> list[FakeControl]:
    return [
        p.controls.items[fa.COMMAND_ID]
        for p in layout.values()
        if fa.COMMAND_ID in p.controls.items
    ]


def test_start_registers_the_button_everywhere_and_connects_both_activation_events(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    layout = full_layout(fa)
    ui = FakeUi(layout)

    addin, fake = start_app(fa, monkeypatch, ui, tmp_path)

    assert len(our_controls(layout, fa)) == len(fa.TARGETS)
    assert len(ui.workspaceActivated.handlers) == 1
    assert len(fake.documentActivated.handlers) == 1
    assert sum("-> added" in line for line in fake.logged) == len(fa.TARGETS)
    addin.stop()


def test_workspace_and_document_activation_enable_the_command_again(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ui = FakeUi(full_layout(fa))
    addin, fake = start_app(fa, monkeypatch, ui, tmp_path)
    control_definition = ui.commandDefinitions.items[fa.COMMAND_ID].controlDefinition
    assert control_definition.isEnabled is False  # Fusion may grey it out in another environment

    ui.workspaceActivated.fire()
    assert control_definition.isEnabled is True

    control_definition.isEnabled = False
    fake.documentActivated.fire()
    assert control_definition.isEnabled is True
    addin.stop()


def test_a_vanished_control_is_added_again_and_reported_once(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    layout = full_layout(fa)
    ui = FakeUi(layout)
    addin, fake = start_app(fa, monkeypatch, ui, tmp_path)
    panel = layout[("SchEditorEnvironement", "ToolsTab", "EagleSchScriptsAddinsPanel")]
    added_lines = sum("-> added" in line for line in fake.logged)

    panel.controls.items[fa.COMMAND_ID].deleted = True  # Fusion dropped it
    ui.workspaceActivated.fire()

    assert panel.controls.itemById(fa.COMMAND_ID) is not None
    assert panel.controls.items[fa.COMMAND_ID].isPromoted is True
    assert sum("-> added" in line for line in fake.logged) == added_lines + 1

    panel.controls.items[fa.COMMAND_ID].deleted = True
    ui.workspaceActivated.fire()  # vanishes again: restored again, but the log stays quiet
    assert panel.controls.itemById(fa.COMMAND_ID) is not None
    assert sum("-> added" in line for line in fake.logged) == added_lines + 1
    addin.stop()


def test_an_unchanged_toolbar_logs_nothing_on_activation(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    layout = full_layout(fa)
    del layout[("PCBDesignEnvironement", "ToolsTab", "EagleScriptsAddinsPanel")]
    ui = FakeUi(layout)
    addin, fake = start_app(fa, monkeypatch, ui, tmp_path)
    before = list(fake.logged)

    for _ in range(3):
        ui.workspaceActivated.fire()
        fake.documentActivated.fire()

    assert fake.logged == before
    addin.stop()


def test_a_panel_that_appears_later_gets_its_button_and_loses_it_on_stop(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    layout = full_layout(fa)
    late_key = ("BoardLayoutEnvironement", "ToolsTab", "EaglePcbScriptsAddinsPanel")
    late_panel = layout.pop(late_key)
    ui = FakeUi(layout)
    addin, fake = start_app(fa, monkeypatch, ui, tmp_path)
    assert fa.COMMAND_ID not in late_panel.controls.items

    layout[late_key] = late_panel  # the environment finished loading
    ui.workspaceActivated.fire()

    assert fa.COMMAND_ID in late_panel.controls.items
    assert any(
        "BoardLayoutEnvironement" in line
        and line.endswith("-> added [panel shown in: BoardLayoutEnvironement]")
        for line in fake.logged
    )
    addin.stop()
    assert late_panel.controls.items[fa.COMMAND_ID].deleted is True


def test_stop_removes_our_controls_the_command_and_the_event_handlers_only(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    layout = full_layout(fa)
    solid = layout[("FusionSolidEnvironment", "ToolsTab", "SolidScriptsAddinsPanel")]
    foreign = solid.controls.addCommand(SimpleNamespace(id="SomeoneElse_Command"))
    ui = FakeUi(layout)
    addin, fake = start_app(fa, monkeypatch, ui, tmp_path)
    controls = our_controls(layout, fa)
    definition = ui.commandDefinitions.items[fa.COMMAND_ID]

    addin.stop()

    assert all(control.deleted for control in controls)
    assert foreign.deleted is False
    assert definition.deleted is True
    assert ui.workspaceActivated.handlers == []
    assert fake.documentActivated.handlers == []

    addin.stop()  # idempotent
    ui.workspaceActivated.fire()  # nothing left to react


def test_a_leftover_control_of_an_unclean_stop_is_replaced_at_start(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    layout = full_layout(fa)
    panel = layout[("FusionSolidEnvironment", "ToolsTab", "SolidScriptsAddinsPanel")]
    leftover = panel.controls.addCommand(definition_stub(fa))

    addin, _ = start_app(fa, monkeypatch, FakeUi(layout), tmp_path)

    assert leftover.deleted is True
    assert panel.controls.items[fa.COMMAND_ID] is not leftover
    assert panel.controls.itemById(fa.COMMAND_ID) is not None
    addin.stop()


def test_a_failing_enable_step_is_logged_and_does_not_skip_the_button_check(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    layout = full_layout(fa)
    ui = FakeUi(layout)
    addin, fake = start_app(fa, monkeypatch, ui, tmp_path)
    definition = ui.commandDefinitions.items[fa.COMMAND_ID]
    panel = layout[("FusionSolidEnvironment", "ToolsTab", "SolidScriptsAddinsPanel")]
    panel.controls.items[fa.COMMAND_ID].deleted = True

    class Broken:
        @property
        def isEnabled(self) -> bool:
            return False

        @isEnabled.setter
        def isEnabled(self, value: bool) -> None:
            raise RuntimeError("definition is gone")

    definition.controlDefinition = Broken()
    ui.workspaceActivated.fire()  # must not raise

    assert any("definition is gone" in line for line in fake.logged)
    assert panel.controls.itemById(fa.COMMAND_ID) is not None
    addin.stop()


def test_activation_handlers_derive_from_the_real_adsk_bases(fa: ModuleType) -> None:
    core = fa.adsk.core
    assert issubclass(fa._WorkspaceActivatedHandler, core.WorkspaceEventHandler)
    assert issubclass(fa._DocumentActivatedHandler, core.DocumentEventHandler)
