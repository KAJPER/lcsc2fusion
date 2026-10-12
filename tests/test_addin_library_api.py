"""Tests for the adsk side of the accumulating library (`AdskLibraryApi`, `FlowLibraryAdder`).

The official adsk stubs provide the real classes (the module is skipped without them); everything
Fusion does is replaced by a recording world (`Fusion`): a project with a root folder, documents, a
text command interpreter that opens documents like the real commands do, an import manager. The
last tests run the real flow through the real facade on that fake world, and then the whole add-in
(real HTTP transport, hub, controller, dispatcher) with a recording `FusionLibraryApi`.
"""

from __future__ import annotations

import dataclasses
import http.client
import importlib
import inspect
import itertools
import json
import math
import re
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, ClassVar
from urllib.parse import urlsplit

import pytest
from fusion_library_flow import LIBRARY_NAME, FlowRequest, LibraryRef
from library_registry import StateStore
from test_addin_adapter import (
    ADDIN_DIR,
    EventStream,
    FakeApp,
    FakeProcess,
    FakeTransport,
    FakeUi,
    FakeWindow,
    FakeWindowApi,
    LoopFakeApp,
    wait_until,
)
from test_addin_dispatcher import MainLoop

from lcsc2fusion.lbr_writer import write_library
from lcsc2fusion.parser.part import parse_part

pytestmark = pytest.mark.usefixtures("adsk_stubs")

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"


@pytest.fixture(scope="module")
def fa(adsk_stubs: Path) -> ModuleType:
    return importlib.import_module("fusion_adapter")


# A fake Fusion world ---------------------------------------------------------------------------


class FakeDataFile:
    def __init__(self, file_id: str, name: str, extension: str = "flbr") -> None:
        self.id = file_id
        self.name = name
        self.fileExtension = extension


class FakeCollection:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    @property
    def count(self) -> int:
        return len(self._items)

    def item(self, index: int) -> Any:
        return self._items[index]


class FakeFolder:
    def __init__(self) -> None:
        self.files: list[Any] = []

    @property
    def dataFiles(self) -> FakeCollection:
        return FakeCollection(self.files)


class FakeProject:
    def __init__(self, root: FakeFolder | None) -> None:
        self.rootFolder = root


class FakeDesign:
    def __init__(self) -> None:
        self.rootComponent = object()


class FakeDocument:
    """A Fusion document. `saveAs` makes it a saved one with a data file in the root folder."""

    def __init__(self, world: Fusion, name: str, *, extension: str = "flbr") -> None:
        self.world = world
        self.name = name
        self.extension = extension
        self.isSaved = False
        self.dataFile: Any = None
        self.saveas_result = world.new_documents_saveas_result
        self.saveas_creates_file = world.new_documents_get_a_file
        self.save_result = True
        self.activate_result = True

    def saveAs(self, name: str, folder: Any, description: str, tag: str) -> bool:
        self.world.journal.append(("saveAs", self.name, name, folder, description, tag))
        if not self.saveas_result:
            return False
        self.isSaved = True
        self.name = name or "Untitled-1"
        if self.saveas_creates_file:
            self.dataFile = FakeDataFile(
                f"urn:file:{next(self.world.ids)}", self.name, self.extension
            )
            folder.files.append(self.dataFile)
            self.world.files_by_id[self.dataFile.id] = self.dataFile
        return True

    def save(self, description: str) -> bool:
        self.world.journal.append(("save", self.name, description))
        return self.save_result

    def activate(self) -> bool:
        self.world.journal.append(("activate", self.name))
        if self.activate_result:
            self.world.app.activeDocument = self
        return self.activate_result

    def close(self, save: bool) -> bool:
        raise AssertionError("the library document must never be closed")


class FakeData:
    def __init__(self, world: Fusion) -> None:
        self._world = world
        self.root = FakeFolder()
        self.active_project: FakeProject | None = FakeProject(self.root)
        self.active_project_error: Exception | None = None
        self.projects: list[FakeProject] = []

    @property
    def activeProject(self) -> FakeProject | None:
        if self.active_project_error is not None:
            raise self.active_project_error
        return self.active_project

    @property
    def dataProjects(self) -> FakeCollection:
        return FakeCollection(self.projects)

    def findFileById(self, file_id: str) -> Any:
        self._world.journal.append(("findFileById", file_id))
        if file_id not in self._world.files_by_id:
            if self._world.unknown_id_returns_none:
                return None
            raise RuntimeError(f"RuntimeError: 3 : invalid id {file_id}")
        return self._world.files_by_id[file_id]


class FakeDocuments:
    def __init__(self, world: Fusion) -> None:
        self._world = world
        self.open_result_none = False
        self.open_activates = True  # real Fusion makes a document it opens the active one
        self.opened_activate_result = True

    @property
    def count(self) -> int:
        return len(self._world.documents)

    def open(self, data_file: Any, visible: bool = True) -> Any:
        world = self._world
        world.journal.append(("documents.open", data_file.id, visible))
        if self.open_result_none:
            return None
        document = next((d for d in world.documents if d.dataFile is data_file), None)
        if document is None:
            document = FakeDocument(world, data_file.name)
            document.isSaved = True
            document.dataFile = data_file
            document.activate_result = self.opened_activate_result
            world.documents.append(document)
        if self.open_activates:
            world.app.activeDocument = document
        return document


class FakeImportManager:
    def __init__(self, world: Fusion) -> None:
        self._world = world
        self.result: Any = None

    def createSTEPImportOptions(self, path: str) -> tuple[str, str]:
        self._world.journal.append(("createSTEPImportOptions", path))
        return ("options", path)

    def importToTarget2(self, options: Any, target: Any) -> Any:
        self._world.journal.append(("importToTarget2", options, target))
        return self.result


class FakeLibApp:
    def __init__(self, world: Fusion) -> None:
        self._world = world
        self.data = world.data
        self.documents = FakeDocuments(world)
        self.importManager = FakeImportManager(world)
        self.activeDocument: Any = None
        self.activeProduct: Any = None

    def executeTextCommand(self, command: str) -> str:
        world = self._world
        world.journal.append(("text", command))
        if world.command_error is not None and command.startswith(world.command_error_prefix):
            raise world.command_error
        for prefix, effect in world.command_effects.items():
            if command.startswith(prefix):
                effect()
        return world.reply


class Fusion:
    """Everything the facade talks to, plus the journal of what it did (in order)."""

    def __init__(self) -> None:
        self.journal: list[tuple[Any, ...]] = []
        self.logs: list[str] = []
        self.ids = itertools.count(1)
        self.files_by_id: dict[str, Any] = {}
        self.unknown_id_returns_none = False
        self.documents: list[FakeDocument] = []
        self.command_error: Exception | None = None
        self.command_error_prefix = ""
        self.reply = ""
        self.new_documents_saveas_result = True
        self.new_documents_get_a_file = True
        self.data = FakeData(self)
        self.app = FakeLibApp(self)
        self.ui = SimpleNamespace(activeWorkspace=SimpleNamespace(id="ElectronEmptyLbrEnvironment"))
        # What the text commands do to the application, like the real ones (clear it to make a
        # command do nothing).
        self.command_effects: dict[str, Callable[[], object]] = {
            "Document.newDesignFromLocal": lambda: self.open_document("part library"),
            "Electron.Create3DPackage": lambda: self.open_document(
                "3D package", extension="f3d", design=True
            ),
        }

    def open_document(
        self, name: str, *, extension: str = "flbr", design: bool = False
    ) -> FakeDocument:
        document = FakeDocument(self, name, extension=extension)
        self.documents.append(document)
        self.app.activeDocument = document
        self.app.activeProduct = FakeDesign() if design else None
        return document

    def add_library_file(self, name: str = LIBRARY_NAME, extension: str = "flbr") -> FakeDataFile:
        data_file = FakeDataFile(f"urn:file:{next(self.ids)}", name, extension)
        self.data.root.files.append(data_file)
        self.files_by_id[data_file.id] = data_file
        return data_file

    def calls(self, kind: str) -> list[tuple[Any, ...]]:
        return [entry for entry in self.journal if entry[0] == kind]

    @property
    def commands(self) -> list[str]:
        return [entry[1] for entry in self.journal if entry[0] == "text"]


@pytest.fixture
def world() -> Fusion:
    return Fusion()


@pytest.fixture
def api(fa: ModuleType, world: Fusion) -> Any:
    return fa.AdskLibraryApi(world.app, world.ui, world.logs.append)


class FakeMatrix:
    def __init__(self, result: bool) -> None:
        self.coordinate_system: tuple[Any, ...] | None = None
        self.result = result

    def setWithCoordinateSystem(self, *args: Any) -> bool:
        self.coordinate_system = args
        return self.result


@dataclasses.dataclass
class Casts:
    """What the patched `adsk.fusion` / `adsk.core` factories hand out."""

    matrices: list[FakeMatrix] = dataclasses.field(default_factory=list)
    occurrence_cast_works: bool = True
    matrix_result: bool = True


@pytest.fixture
def casts(fa: ModuleType, monkeypatch: pytest.MonkeyPatch) -> Casts:
    state = Casts()

    def make_matrix() -> FakeMatrix:
        state.matrices.append(FakeMatrix(state.matrix_result))
        return state.matrices[-1]

    monkeypatch.setattr(fa.adsk.core.Matrix3D, "create", staticmethod(make_matrix))
    monkeypatch.setattr(
        fa.adsk.core.Point3D, "create", staticmethod(lambda x, y, z: ("point", x, y, z))
    )
    monkeypatch.setattr(
        fa.adsk.core.Vector3D, "create", staticmethod(lambda x, y, z: ("vector", x, y, z))
    )
    monkeypatch.setattr(
        fa.adsk.fusion.Design,
        "cast",
        staticmethod(lambda product: product if isinstance(product, FakeDesign) else None),
    )
    monkeypatch.setattr(
        fa.adsk.fusion.Occurrence,
        "cast",
        staticmethod(lambda item: item if state.occurrence_cast_works else None),
    )
    return state


class OccurrenceWithTransform2:
    def __init__(self) -> None:
        self.transform2: Any = None


class OccurrenceWithoutTransform2:
    def __init__(self) -> None:
        self.transform: Any = None


class OccurrenceWithBrokenTransform2:
    transform: Any = None

    @property
    def transform2(self) -> Any:
        return None

    @transform2.setter
    def transform2(self, value: Any) -> None:
        raise RuntimeError("RuntimeError: 2 : retired")


def write_step(directory: Path, name: str = "C100.placed.step") -> Path:
    path = directory / name
    path.write_text("ISO-10303-21;", encoding="utf-8")
    return path


def write_part_lbr(directory: Path, lcsc_id: str, mpn: str) -> Path:
    raw = json.loads((SYNTH / "r0603.json").read_text(encoding="utf-8"))
    part = dataclasses.replace(parse_part(raw), lcsc_id=lcsc_id, mpn=mpn)
    directory.mkdir(parents=True, exist_ok=True)
    return write_library(directory / f"{lcsc_id}.lbr", [part])


# Pure helpers: text commands -----------------------------------------------------------------


def test_the_merge_commands_are_built_like_snapeda(fa: ModuleType, tmp_path: Path) -> None:
    path = tmp_path / "My Libs" / "C2040.lbr"
    use, copy, unuse = fa.build_merge_commands(path, "RP2040")
    assert use == f"Electron.run \"Use '{path.as_posix()}'\""
    assert copy == 'Electron.run "Copy "RP2040.dev@C2040""'
    assert unuse == 'Electron.run "Use "-C2040""'
    assert "\\" not in use + copy + unuse


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path syntax")
def test_the_merge_command_path_has_forward_slashes_on_windows(fa: ModuleType) -> None:
    path = Path(r"C:\Users\John Doe\LCSC2Fusion\libraries\C2040.lbr")
    use, _, _ = fa.build_merge_commands(path, "X")
    assert use == "Electron.run \"Use 'C:/Users/John Doe/LCSC2Fusion/libraries/C2040.lbr'\""


def test_the_stem_is_the_file_name_without_the_extension(fa: ModuleType, tmp_path: Path) -> None:
    _, copy, unuse = fa.build_merge_commands(tmp_path / "C2040.placed.lbr", "DEV")
    assert copy == 'Electron.run "Copy "DEV.dev@C2040.placed""'
    assert unuse == 'Electron.run "Use "-C2040.placed""'


@pytest.mark.parametrize("kind", ["relative", "double quote", "single quote"])
def test_merge_commands_refuse_paths_they_cannot_carry(
    fa: ModuleType, tmp_path: Path, kind: str
) -> None:
    paths = {
        "relative": Path("C2040.lbr"),
        "double quote": tmp_path / 'we"ird.lbr',
        "single quote": tmp_path / "O'Brien" / "C2040.lbr",
    }
    match = "absolute" if kind == "relative" else kind
    with pytest.raises(ValueError, match=match):
        fa.build_merge_commands(paths[kind], "RP2040")


@pytest.mark.parametrize("deviceset", ["", "   ", 'BAD"NAME', "TAB\tNAME"])
def test_merge_commands_refuse_a_device_set_name_they_cannot_carry(
    fa: ModuleType, tmp_path: Path, deviceset: str
) -> None:
    with pytest.raises(ValueError, match="device set name"):
        fa.build_merge_commands(tmp_path / "C2040.lbr", deviceset)


def test_merge_commands_refuse_a_file_name_with_an_at_sign(fa: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="library file name"):
        fa.build_merge_commands(tmp_path / "C20@40.lbr", "RP2040")


def test_the_create_3d_command_quotes_the_path_with_forward_slashes(
    fa: ModuleType, tmp_path: Path
) -> None:
    path = tmp_path / "My Libs" / "C2040.lbr"
    assert fa.build_create_3d_command(path) == f'Electron.Create3DPackage "{path.as_posix()}"'
    with pytest.raises(ValueError, match="absolute"):
        fa.build_create_3d_command(Path("C2040.lbr"))
    with pytest.raises(ValueError, match="double quote"):
        fa.build_create_3d_command(tmp_path / 'we"ird.lbr')


# Pure helpers: the STEP turn -------------------------------------------------------------------


def apply_axes(axes: tuple[Any, ...], vector: tuple[float, float, float]) -> tuple[float, ...]:
    """Transform `vector` by the matrix whose columns are the axes (what Fusion builds)."""
    _origin, x_axis, y_axis, z_axis = axes
    x, y, z = vector
    return tuple(x * a + y * b + z * c for a, b, c in zip(x_axis, y_axis, z_axis, strict=True))


def test_the_turn_maps_xyz_to_x_minus_z_y(fa: ModuleType) -> None:
    axes = fa.step_turn_axes()
    assert axes[0] == (0.0, 0.0, 0.0)
    assert apply_axes(axes, (1, 0, 0)) == (1.0, 0.0, 0.0)
    assert apply_axes(axes, (0, 1, 0)) == (0.0, 0.0, 1.0)
    assert apply_axes(axes, (0, 0, 1)) == (0.0, -1.0, 0.0)
    assert apply_axes(axes, (2, 3, 5)) == (2.0, -5.0, 3.0)


def test_the_turn_axes_are_snapedas_without_float_noise(fa: ModuleType) -> None:
    angle = math.pi * 0.5
    assert angle == fa.STEP_IMPORT_TURN_RAD
    _, x_axis, y_axis, z_axis = fa.step_turn_axes()
    assert x_axis == (1.0, 0.0, 0.0)
    assert y_axis == (0.0, 0.0, 1.0)  # snapeda: (0, cos a, sin a) with cos(pi/2) = 6e-17
    assert z_axis == (0.0, -1.0, 0.0)  # snapeda: (0, cos(a + pi/2), sin(a + pi/2))
    assert y_axis[1:] == pytest.approx((math.cos(angle), math.sin(angle)), abs=1e-12)
    assert z_axis[1:] == pytest.approx(
        (math.cos(angle + math.pi / 2), math.sin(angle + math.pi / 2)), abs=1e-12
    )


def test_a_zero_turn_is_the_identity(fa: ModuleType) -> None:
    assert apply_axes(fa.step_turn_axes(0.0), (4, 5, 6)) == (4.0, 5.0, 6.0)


def test_the_matrix_is_built_from_the_axes(fa: ModuleType, casts: Casts) -> None:
    matrix = fa.build_turn_matrix()
    assert casts.matrices == [matrix]
    assert matrix.coordinate_system == (
        ("point", 0.0, 0.0, 0.0),
        ("vector", 1.0, 0.0, 0.0),
        ("vector", 0.0, 0.0, 1.0),
        ("vector", 0.0, -1.0, 0.0),
    )


def test_a_matrix_fusion_rejects_is_an_error(fa: ModuleType, casts: Casts) -> None:
    casts.matrix_result = False
    with pytest.raises(fa.LibraryApiError, match="rotation matrix"):
        fa.build_turn_matrix()


def test_transform2_is_used_when_the_occurrence_has_it(fa: ModuleType) -> None:
    occurrence = OccurrenceWithTransform2()
    assert fa.turn_occurrence(occurrence, "M", lambda _: None) == "transform2"
    assert occurrence.transform2 == "M"


def test_transform_is_the_fallback_when_there_is_no_transform2(fa: ModuleType) -> None:
    occurrence = OccurrenceWithoutTransform2()
    assert fa.turn_occurrence(occurrence, "M", lambda _: None) == "transform"
    assert occurrence.transform == "M"


def test_transform_is_the_fallback_when_assigning_transform2_raises(fa: ModuleType) -> None:
    occurrence = OccurrenceWithBrokenTransform2()
    logs: list[str] = []
    assert fa.turn_occurrence(occurrence, "M", logs.append) == "transform"
    assert occurrence.transform == "M"
    assert any("transform2 failed" in line and "retired" in line for line in logs)


# Pure helpers: documents and folders ---------------------------------------------------------


class Doc:
    """A document wrapper: identity comparison, like an `adsk` object that defines no `==`."""

    def __init__(self, *, saved: bool = False, file_id: str | None = None) -> None:
        self.isSaved = saved
        self.dataFile = None if file_id is None else SimpleNamespace(id=file_id)


def test_document_changed_signals(fa: ModuleType) -> None:
    changed = fa.document_changed
    saved, other_saved, unsaved = Doc(saved=True), Doc(saved=True), Doc()
    assert changed(saved, None, 1, 1) is False  # nothing is active
    assert changed(None, unsaved, None, None) is True  # nothing was active before
    assert changed(saved, saved, 1, 1) is False
    assert changed(saved, unsaved, 1, 2) is True  # a document appeared
    assert changed(Doc(), Doc(), 1, 1) is False  # new wrapper, nothing to tell them apart
    assert changed(Doc(saved=True, file_id="a"), Doc(saved=True, file_id="b"), 1, 1) is True
    assert changed(Doc(saved=True, file_id="a"), Doc(saved=True, file_id="a"), 1, 1) is False
    assert changed(saved, unsaved, 1, 1) is True  # a saved library against a new design
    assert changed(saved, other_saved, None, None) is False


def test_pick_root_folder_prefers_the_active_project(fa: ModuleType, world: Fusion) -> None:
    world.data.projects = [FakeProject(FakeFolder())]
    assert fa.pick_root_folder(world.data) is world.data.root


@pytest.mark.parametrize("error", [None, RuntimeError("no hub selected")])
def test_pick_root_folder_falls_back_to_the_first_project(
    fa: ModuleType, world: Fusion, error: Exception | None
) -> None:
    first, second = FakeFolder(), FakeFolder()
    world.data.active_project = None
    world.data.active_project_error = error
    world.data.projects = [FakeProject(first), FakeProject(second)]
    assert fa.pick_root_folder(world.data) is first


def test_pick_root_folder_without_any_project_is_a_clear_error(
    fa: ModuleType, world: Fusion
) -> None:
    world.data.active_project = None
    with pytest.raises(fa.LibraryApiError, match="no project"):
        fa.pick_root_folder(world.data)


def test_pick_root_folder_without_a_root_is_a_clear_error(fa: ModuleType, world: Fusion) -> None:
    world.data.active_project = FakeProject(None)
    with pytest.raises(fa.LibraryApiError, match="root folder"):
        fa.pick_root_folder(world.data)


# find_library --------------------------------------------------------------------------------


def test_find_library_by_a_valid_id_does_not_look_at_the_folder(api: Any, world: Fusion) -> None:
    stored = world.add_library_file()
    world.data.active_project = None  # a project is not needed when the id is good

    ref = api.find_library(stored.id, LIBRARY_NAME)

    assert ref == LibraryRef(stored.id, LIBRARY_NAME)
    assert world.journal == [("findFileById", stored.id)]
    assert any(line.startswith("find_library:") and "by id" in line for line in world.logs)


@pytest.mark.parametrize("none_instead_of_raising", [False, True])
def test_find_library_with_a_stale_id_falls_back_to_the_name(
    api: Any, world: Fusion, none_instead_of_raising: bool
) -> None:
    world.unknown_id_returns_none = none_instead_of_raising
    existing = world.add_library_file()

    ref = api.find_library("urn:file:gone", LIBRARY_NAME)

    assert ref == LibraryRef(existing.id, LIBRARY_NAME)
    assert world.calls("findFileById") == [("findFileById", "urn:file:gone")]
    assert any("by name" in line for line in world.logs)


def test_find_library_with_a_stale_id_and_no_file_is_none(api: Any, world: Fusion) -> None:
    assert api.find_library("urn:file:gone", LIBRARY_NAME) is None
    assert any("no library" in line for line in world.logs)


def test_find_library_without_an_id_searches_the_root_folder_by_exact_name(
    api: Any, world: Fusion
) -> None:
    world.add_library_file("SnapEDA Library")
    world.add_library_file(LIBRARY_NAME + " copy")
    wanted = world.add_library_file()

    assert api.find_library(None, LIBRARY_NAME) == LibraryRef(wanted.id, LIBRARY_NAME)
    assert world.calls("findFileById") == []


def test_a_file_with_the_right_name_but_another_type_is_not_the_library(
    api: Any, world: Fusion
) -> None:
    world.add_library_file(LIBRARY_NAME, extension="f3d")
    assert api.find_library(None, LIBRARY_NAME) is None
    assert any("'f3d'" in line for line in world.logs)


def test_an_id_of_another_type_of_file_is_rejected(api: Any, world: Fusion) -> None:
    design = world.add_library_file("Some design", extension="f3d")
    real = world.add_library_file()

    assert api.find_library(design.id, LIBRARY_NAME) == LibraryRef(real.id, LIBRARY_NAME)


def test_the_file_extension_is_compared_case_insensitively_and_may_be_missing(
    api: Any, world: Fusion
) -> None:
    upper = world.add_library_file(extension="FLBR")
    assert api.find_library(None, LIBRARY_NAME) == LibraryRef(upper.id, LIBRARY_NAME)

    bare = SimpleNamespace(id="urn:file:bare", name="Bare")  # no fileExtension at all
    world.files_by_id[bare.id] = bare
    assert api.find_library(bare.id, "Bare") == LibraryRef("urn:file:bare", "Bare")


def test_with_two_libraries_of_the_same_name_the_first_wins_and_it_is_logged(
    api: Any, world: Fusion
) -> None:
    first = world.add_library_file()
    world.add_library_file()

    assert api.find_library(None, LIBRARY_NAME) == LibraryRef(first.id, LIBRARY_NAME)
    assert any("2 libraries" in line for line in world.logs)


def test_find_library_uses_the_first_project_when_none_is_active(api: Any, world: Fusion) -> None:
    folder = FakeFolder()
    folder.files.append(FakeDataFile("urn:file:inside", LIBRARY_NAME))
    world.data.active_project = None
    world.data.projects = [FakeProject(folder)]

    assert api.find_library(None, LIBRARY_NAME) == LibraryRef("urn:file:inside", LIBRARY_NAME)


def test_find_library_without_a_project_and_a_stale_id_raises(
    fa: ModuleType, api: Any, world: Fusion
) -> None:
    world.data.active_project = None
    with pytest.raises(fa.LibraryApiError, match="no project"):
        api.find_library("urn:file:gone", LIBRARY_NAME)
    assert any("find_library failed" in line for line in world.logs)


# create_library_from_part --------------------------------------------------------------------


def test_create_library_opens_the_part_and_saves_it_into_the_root_folder(
    api: Any, world: Fusion, tmp_path: Path
) -> None:
    lbr = tmp_path / "C100.lbr"

    ref = api.create_library_from_part(lbr, LIBRARY_NAME)

    document = world.app.activeDocument
    assert world.journal == [
        ("text", f'Document.newDesignFromLocal "{lbr.as_posix()}"'),
        ("saveAs", "part library", LIBRARY_NAME, world.data.root, "", ""),
    ]
    assert document.isSaved is True  # and FakeDocument.close would assert: it is never closed
    assert ref == LibraryRef(document.dataFile.id, LIBRARY_NAME)
    assert world.data.root.files == [document.dataFile]
    assert any("create_library_from_part:" in line and "kept open" in line for line in world.logs)
    assert any("Document.newDesignFromLocal" in line and "-> ''" in line for line in world.logs)


def test_an_already_saved_active_document_is_not_renamed(
    fa: ModuleType, api: Any, world: Fusion, tmp_path: Path
) -> None:
    def opens_a_saved_document() -> None:
        world.open_document("users design").isSaved = True

    world.command_effects["Document.newDesignFromLocal"] = opens_a_saved_document

    with pytest.raises(fa.LibraryApiError, match="already saved"):
        api.create_library_from_part(tmp_path / "C100.lbr", LIBRARY_NAME)
    assert world.calls("saveAs") == []


def test_create_library_fails_when_the_open_command_opened_nothing(
    fa: ModuleType, api: Any, world: Fusion, tmp_path: Path
) -> None:
    world.command_effects.clear()
    with pytest.raises(fa.LibraryApiError, match="did not open"):
        api.create_library_from_part(tmp_path / "C100.lbr", LIBRARY_NAME)
    assert world.calls("saveAs") == []
    assert any("create_library_from_part failed" in line for line in world.logs)


def test_create_library_fails_when_save_as_fails(
    fa: ModuleType, api: Any, world: Fusion, tmp_path: Path
) -> None:
    world.new_documents_saveas_result = False
    with pytest.raises(fa.LibraryApiError, match="could not save the new library"):
        api.create_library_from_part(tmp_path / "C100.lbr", LIBRARY_NAME)


def test_create_library_fails_when_fusion_reports_no_data_file(
    fa: ModuleType, api: Any, world: Fusion, tmp_path: Path
) -> None:
    world.new_documents_get_a_file = False
    with pytest.raises(fa.LibraryApiError, match="did not report its file"):
        api.create_library_from_part(tmp_path / "C100.lbr", LIBRARY_NAME)


def test_create_library_without_a_project_runs_no_command(
    fa: ModuleType, api: Any, world: Fusion, tmp_path: Path
) -> None:
    world.data.active_project = None
    with pytest.raises(fa.LibraryApiError, match="no project"):
        api.create_library_from_part(tmp_path / "C100.lbr", LIBRARY_NAME)
    assert world.journal == []


def test_create_library_with_a_bad_path_runs_no_command(
    api: Any, world: Fusion, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="double quote"):
        api.create_library_from_part(tmp_path / 'bad"name.lbr', LIBRARY_NAME)
    with pytest.raises(ValueError, match="absolute"):
        api.create_library_from_part(Path("C100.lbr"), LIBRARY_NAME)
    assert world.journal == []


def test_create_library_passes_on_a_failing_command_without_saving(
    api: Any, world: Fusion, tmp_path: Path
) -> None:
    world.command_error = RuntimeError("RuntimeError: 3 : unknown command")
    with pytest.raises(RuntimeError, match="unknown command"):
        api.create_library_from_part(tmp_path / "C100.lbr", LIBRARY_NAME)
    assert world.calls("saveAs") == []
    assert any("raised" in line for line in world.logs)


# open_library --------------------------------------------------------------------------------


def test_open_library_opens_the_data_file_and_activates_it(api: Any, world: Fusion) -> None:
    stored = world.add_library_file()
    world.open_document("users design")  # another document is active

    api.open_library(LibraryRef(stored.id, LIBRARY_NAME))

    assert world.journal == [("findFileById", stored.id), ("documents.open", stored.id, True)]
    assert world.app.activeDocument.dataFile is stored
    assert any(
        line.startswith("open_library:") and "ElectronEmptyLbrEnvironment" in line
        for line in world.logs
    )


def test_open_library_activates_the_document_when_open_did_not(api: Any, world: Fusion) -> None:
    stored = world.add_library_file()
    world.open_document("users design")
    world.app.documents.open_activates = False

    api.open_library(LibraryRef(stored.id, LIBRARY_NAME))

    assert world.calls("activate") == [("activate", LIBRARY_NAME)]
    assert world.app.activeDocument.dataFile is stored


def test_open_library_does_not_activate_what_is_already_active(api: Any, world: Fusion) -> None:
    stored = world.add_library_file()

    api.open_library(LibraryRef(stored.id, LIBRARY_NAME))

    assert world.calls("activate") == []


def test_open_library_with_a_stale_id_raises(fa: ModuleType, api: Any, world: Fusion) -> None:
    with pytest.raises(RuntimeError, match="invalid id"):
        api.open_library(LibraryRef("urn:file:gone", LIBRARY_NAME))
    world.unknown_id_returns_none = True
    with pytest.raises(fa.LibraryApiError, match="cannot find the library"):
        api.open_library(LibraryRef("urn:file:gone", LIBRARY_NAME))
    assert world.calls("documents.open") == []


def test_open_library_fails_when_fusion_opens_nothing(
    fa: ModuleType, api: Any, world: Fusion
) -> None:
    stored = world.add_library_file()
    world.app.documents.open_result_none = True
    with pytest.raises(fa.LibraryApiError, match="could not open"):
        api.open_library(LibraryRef(stored.id, LIBRARY_NAME))


def test_open_library_fails_when_the_document_cannot_be_activated(
    fa: ModuleType, api: Any, world: Fusion
) -> None:
    stored = world.add_library_file()
    world.open_document("users design")
    world.app.documents.open_activates = False
    world.app.documents.opened_activate_result = False
    with pytest.raises(fa.LibraryApiError, match="could not activate"):
        api.open_library(LibraryRef(stored.id, LIBRARY_NAME))


# merge_part ----------------------------------------------------------------------------------


def open_library_for_merge(api: Any, world: Fusion) -> None:
    stored = world.add_library_file()
    api.open_library(LibraryRef(stored.id, LIBRARY_NAME))
    world.journal.clear()
    world.logs.clear()


def test_merge_part_runs_the_three_commands_in_order_and_logs_the_replies(
    api: Any, world: Fusion, tmp_path: Path
) -> None:
    open_library_for_merge(api, world)
    world.reply = "ok-ish"
    lbr = tmp_path / "C2040.lbr"

    api.merge_part(lbr, "RP2040")

    assert world.commands == [
        f"Electron.run \"Use '{lbr.as_posix()}'\"",
        'Electron.run "Copy "RP2040.dev@C2040""',
        'Electron.run "Use "-C2040""',
    ]
    assert len([line for line in world.logs if "-> 'ok-ish'" in line]) == 3
    assert any(line.startswith("merge_part: merged RP2040 from C2040.lbr") for line in world.logs)


def test_an_empty_reply_is_not_an_error(api: Any, world: Fusion, tmp_path: Path) -> None:
    open_library_for_merge(api, world)
    api.merge_part(tmp_path / "C2040.lbr", "RP2040")  # Fusion answers '' for every command
    assert len(world.commands) == 3


def test_a_failing_command_stops_the_merge_and_is_logged(
    api: Any, world: Fusion, tmp_path: Path
) -> None:
    open_library_for_merge(api, world)
    world.command_error = RuntimeError("RuntimeError: 3 : bad copy")
    world.command_error_prefix = 'Electron.run "Copy'

    with pytest.raises(RuntimeError, match="bad copy"):
        api.merge_part(tmp_path / "C2040.lbr", "RP2040")

    assert len(world.commands) == 2  # the third command (Use -) did not run
    assert any("Copy" in line and "raised" in line for line in world.logs)
    assert any("merge_part failed" in line for line in world.logs)


def test_merge_part_refuses_a_bad_name_before_any_command(
    api: Any, world: Fusion, tmp_path: Path
) -> None:
    open_library_for_merge(api, world)
    with pytest.raises(ValueError, match="device set name"):
        api.merge_part(tmp_path / "C2040.lbr", 'BAD"')
    assert world.commands == []


def test_merge_part_refuses_to_run_in_another_document(
    fa: ModuleType, api: Any, world: Fusion, tmp_path: Path
) -> None:
    open_library_for_merge(api, world)
    schematic = world.open_document("users schematic")
    schematic.isSaved = True
    schematic.dataFile = FakeDataFile("urn:file:schematic", "schematic", "fsch")

    with pytest.raises(fa.LibraryApiError, match="not the library"):
        api.merge_part(tmp_path / "C2040.lbr", "RP2040")
    assert world.commands == []


def test_merge_part_without_a_known_library_just_runs(
    api: Any, world: Fusion, tmp_path: Path
) -> None:
    api.merge_part(tmp_path / "C2040.lbr", "RP2040")  # nothing to compare the active document with
    assert len(world.commands) == 3


# save_active ---------------------------------------------------------------------------------


def test_save_active_saves_the_active_document_with_the_description(
    api: Any, world: Fusion
) -> None:
    world.open_document(LIBRARY_NAME)

    api.save_active("Added C2040 RP2040")

    assert world.journal == [("save", LIBRARY_NAME, "Added C2040 RP2040")]
    assert any(line.startswith("save_active:") for line in world.logs)


def test_save_active_raises_when_fusion_returns_false(
    fa: ModuleType, api: Any, world: Fusion
) -> None:
    world.open_document(LIBRARY_NAME).save_result = False
    with pytest.raises(fa.LibraryApiError, match="could not save"):
        api.save_active("x")


def test_save_active_without_a_document_raises(fa: ModuleType, api: Any) -> None:
    with pytest.raises(fa.LibraryApiError, match="no active document"):
        api.save_active("x")


# create_3d_package ---------------------------------------------------------------------------


def test_create_3d_package_runs_the_command_and_saves_the_new_design(
    api: Any, world: Fusion, tmp_path: Path
) -> None:
    library = world.open_document(LIBRARY_NAME)
    library.isSaved = True
    lbr = tmp_path / "C100.lbr"

    api.create_3d_package(lbr)

    assert world.journal == [
        ("text", f'Electron.Create3DPackage "{lbr.as_posix()}"'),
        ("saveAs", "3D package", "", world.data.root, "LCSC2Fusion 3D package", ""),
    ]
    assert world.app.activeDocument is not library  # the package editor is the active document
    assert any(line.startswith("create_3d_package:") for line in world.logs)


def test_create_3d_package_after_the_library_was_created_uses_it_as_the_reference(
    api: Any, world: Fusion, tmp_path: Path
) -> None:
    api.create_library_from_part(tmp_path / "C100.lbr", LIBRARY_NAME)
    library = world.app.activeDocument
    world.journal.clear()

    api.create_3d_package(tmp_path / "C100.lbr")

    assert [entry[0] for entry in world.journal] == ["text", "saveAs"]
    assert world.app.activeDocument is not library
    assert library.isSaved is True


def test_create_3d_package_fails_when_the_active_document_did_not_change(
    fa: ModuleType, api: Any, world: Fusion, tmp_path: Path
) -> None:
    world.open_document(LIBRARY_NAME).isSaved = True
    world.command_effects.clear()

    with pytest.raises(fa.LibraryApiError, match="3D package editor did not open"):
        api.create_3d_package(tmp_path / "C100.lbr")

    assert world.calls("saveAs") == []  # the library must not be saved as something else


def test_create_3d_package_without_a_project_runs_no_command(
    fa: ModuleType, api: Any, world: Fusion, tmp_path: Path
) -> None:
    world.data.active_project = None
    with pytest.raises(fa.LibraryApiError, match="no project"):
        api.create_3d_package(tmp_path / "C100.lbr")
    assert world.journal == []


def test_create_3d_package_fails_when_save_as_fails(
    fa: ModuleType, api: Any, world: Fusion, tmp_path: Path
) -> None:
    world.open_document(LIBRARY_NAME).isSaved = True
    world.new_documents_saveas_result = False
    with pytest.raises(fa.LibraryApiError, match="could not save the new 3D package"):
        api.create_3d_package(tmp_path / "C100.lbr")


def test_create_3d_package_with_a_bad_path_runs_no_command(
    api: Any, world: Fusion, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="double quote"):
        api.create_3d_package(tmp_path / 'bad"name.lbr')
    assert world.journal == []


# import_step ---------------------------------------------------------------------------------


def start_design(world: Fusion, result: Any) -> FakeDesign:
    world.open_document("3D package", design=True)
    world.app.importManager.result = result
    assert isinstance(world.app.activeProduct, FakeDesign)
    return world.app.activeProduct


def test_import_step_imports_into_the_root_component_and_turns_the_occurrence(
    api: Any, world: Fusion, casts: Casts, tmp_path: Path
) -> None:
    step = write_step(tmp_path)
    occurrence = OccurrenceWithTransform2()
    design = start_design(world, FakeCollection([occurrence]))

    api.import_step(step)

    assert world.journal == [
        ("createSTEPImportOptions", str(step)),  # the native separators, like SnapEDA
        ("importToTarget2", ("options", str(step)), design.rootComponent),
    ]
    assert occurrence.transform2 is casts.matrices[0]
    assert casts.matrices[0].coordinate_system == (
        ("point", 0.0, 0.0, 0.0),
        ("vector", 1.0, 0.0, 0.0),
        ("vector", 0.0, 0.0, 1.0),
        ("vector", 0.0, -1.0, 0.0),
    )
    assert any(line.startswith("import_step:") and "transform2" in line for line in world.logs)


def test_import_step_falls_back_to_transform(
    api: Any, world: Fusion, casts: Casts, tmp_path: Path
) -> None:
    occurrence = OccurrenceWithoutTransform2()
    start_design(world, FakeCollection([occurrence]))

    api.import_step(write_step(tmp_path))

    assert occurrence.transform is casts.matrices[0]


def test_import_step_survives_a_transform2_that_raises(
    api: Any, world: Fusion, casts: Casts, tmp_path: Path
) -> None:
    occurrence = OccurrenceWithBrokenTransform2()
    start_design(world, FakeCollection([occurrence]))

    api.import_step(write_step(tmp_path))

    assert occurrence.transform is casts.matrices[0]
    assert any("transform2 failed" in line for line in world.logs)


def test_import_step_takes_the_first_imported_object(
    api: Any, world: Fusion, casts: Casts, tmp_path: Path
) -> None:
    first, second = OccurrenceWithTransform2(), OccurrenceWithTransform2()
    start_design(world, FakeCollection([first, second]))

    api.import_step(write_step(tmp_path))

    assert first.transform2 is not None
    assert second.transform2 is None


def test_import_step_without_a_design_imports_nothing(
    fa: ModuleType, api: Any, world: Fusion, casts: Casts, tmp_path: Path
) -> None:
    world.open_document("a schematic")  # its product is not a design
    with pytest.raises(fa.LibraryApiError, match="not a design"):
        api.import_step(write_step(tmp_path))
    assert world.journal == []


@pytest.mark.parametrize("result", [None, FakeCollection([])])
def test_import_step_that_imports_nothing_raises(
    fa: ModuleType, api: Any, world: Fusion, casts: Casts, tmp_path: Path, result: Any
) -> None:
    start_design(world, result)
    with pytest.raises(fa.LibraryApiError, match="imported nothing"):
        api.import_step(write_step(tmp_path))
    assert casts.matrices == []


def test_import_step_without_an_occurrence_raises(
    fa: ModuleType, api: Any, world: Fusion, casts: Casts, tmp_path: Path
) -> None:
    start_design(world, FakeCollection([object()]))
    casts.occurrence_cast_works = False
    with pytest.raises(fa.LibraryApiError, match="did not produce an occurrence"):
        api.import_step(write_step(tmp_path))


def test_import_step_with_a_missing_or_relative_file_raises_before_touching_fusion(
    fa: ModuleType, api: Any, world: Fusion, casts: Casts, tmp_path: Path
) -> None:
    start_design(world, FakeCollection([OccurrenceWithTransform2()]))
    with pytest.raises(fa.LibraryApiError, match="does not exist"):
        api.import_step(tmp_path / "missing.step")
    with pytest.raises(fa.LibraryApiError, match="does not exist"):
        api.import_step(Path("relative.step"))
    assert world.journal == []


def test_import_step_passes_on_a_failing_import(
    api: Any, world: Fusion, casts: Casts, tmp_path: Path
) -> None:
    start_design(world, None)

    def boom(options: Any, target: Any) -> Any:
        raise RuntimeError("RuntimeError: 3 : cannot read the STEP file")

    world.app.importManager.importToTarget2 = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="cannot read the STEP"):
        api.import_step(write_step(tmp_path))
    assert any("import_step failed" in line for line in world.logs)


# Logging -------------------------------------------------------------------------------------


def test_every_method_logs_one_summary_line_with_the_elapsed_time(
    api: Any, world: Fusion, tmp_path: Path
) -> None:
    api.find_library(None, LIBRARY_NAME)
    api.create_library_from_part(tmp_path / "C100.lbr", LIBRARY_NAME)
    api.save_active("d")
    api.merge_part(tmp_path / "C100.lbr", "DEV")

    summary = re.compile(r"^(\w+): .* \(\d+\.\d s\)$")
    names = [m.group(1) for m in map(summary.match, world.logs) if m]
    assert names == ["find_library", "create_library_from_part", "save_active", "merge_part"]


# FlowLibraryAdder ----------------------------------------------------------------------------


def make_request(tmp_path: Path, *, step: bool = False) -> FlowRequest:
    lbr = tmp_path / "C100.lbr"
    lbr.write_text("<eagle/>", encoding="utf-8")
    return FlowRequest("C100", "MPN-100", lbr, write_step(tmp_path) if step else None)


class StubFlow:
    """Replaces `FusionLibraryFlow` in the adapter."""

    created: ClassVar[list[StubFlow]] = []
    run_error: BaseException | None = None
    init_error: BaseException | None = None
    result: Any = None

    def __init__(self, api: Any, store: Any, status: Any, log: Any) -> None:
        if StubFlow.init_error is not None:
            raise StubFlow.init_error
        self.args = (api, store, status, log)
        self.requests: list[Any] = []
        StubFlow.created.append(self)

    def run(self, request: Any) -> Any:
        self.requests.append(request)
        if StubFlow.run_error is not None:
            raise StubFlow.run_error
        return StubFlow.result


@pytest.fixture
def stub_flow(fa: ModuleType, monkeypatch: pytest.MonkeyPatch) -> type[StubFlow]:
    StubFlow.created = []
    StubFlow.run_error = StubFlow.init_error = StubFlow.result = None
    monkeypatch.setattr(fa, "FusionLibraryFlow", StubFlow)
    return StubFlow


def test_the_adder_builds_a_flow_per_call_and_returns_its_result(
    fa: ModuleType, stub_flow: type[StubFlow], tmp_path: Path
) -> None:
    expected = object()
    stub_flow.result = expected
    api, store, logs = object(), StateStore(tmp_path / "state.json"), []
    adder = fa.FlowLibraryAdder(api, store, logs.append)
    request = make_request(tmp_path)
    statuses: list[tuple[str, str]] = []

    assert adder.add(request, lambda level, text: statuses.append((level, text))) is expected
    assert adder.add(request, lambda level, text: None) is expected

    assert len(stub_flow.created) == 2
    first = stub_flow.created[0]
    assert first.args[0] is api
    assert first.args[1] is store
    assert first.args[3] == logs.append
    first.args[2]("info", "hello")  # the status callback is passed straight through
    assert statuses == [("info", "hello")]
    assert first.requests == [request]


@pytest.mark.parametrize("where", ["init", "run"])
def test_the_adder_never_raises_and_reports_a_failed_result(
    fa: ModuleType, stub_flow: type[StubFlow], tmp_path: Path, where: str
) -> None:
    error = RuntimeError("flow exploded")
    if where == "init":
        stub_flow.init_error = error
    else:
        stub_flow.run_error = error
    logs: list[str] = []
    statuses: list[tuple[str, str]] = []
    adder = fa.FlowLibraryAdder(object(), StateStore(tmp_path / "s.json"), logs.append)

    result = adder.add(
        make_request(tmp_path, step=True), lambda level, text: statuses.append((level, text))
    )

    assert result.ok is False
    assert result.created_library is False
    assert result.merged is False
    assert result.already_present is False
    assert result.three_d == "skipped"  # a 3D model was requested and not tried
    assert result.library_name == LIBRARY_NAME
    assert "flow exploded" in result.message
    assert "C100" in result.message
    assert statuses == [("error", result.message)]
    assert any("Traceback" in line and "flow exploded" in line for line in logs)


def test_a_failed_result_without_a_3d_request_says_none(
    fa: ModuleType, stub_flow: type[StubFlow], tmp_path: Path
) -> None:
    stub_flow.run_error = ValueError()
    adder = fa.FlowLibraryAdder(object(), StateStore(tmp_path / "s.json"), lambda _: None)
    result = adder.add(make_request(tmp_path), lambda level, text: None)
    assert result.three_d == "none"
    assert "ValueError" in result.message  # an exception without a text is still readable


def test_a_failing_status_sink_does_not_make_the_adder_raise(
    fa: ModuleType, stub_flow: type[StubFlow], tmp_path: Path
) -> None:
    stub_flow.run_error = RuntimeError("flow exploded")

    def broken_sink(level: str, text: str) -> None:
        raise OSError("hub gone")

    adder = fa.FlowLibraryAdder(object(), StateStore(tmp_path / "s.json"), lambda _: None)
    assert adder.add(make_request(tmp_path), broken_sink).ok is False


def test_a_nested_add_is_refused_without_starting_a_second_flow(
    fa: ModuleType, stub_flow: type[StubFlow], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adder = fa.FlowLibraryAdder(object(), StateStore(tmp_path / "s.json"), lambda _: None)
    request = make_request(tmp_path)
    nested: list[Any] = []

    def run_nested(self: StubFlow, req: Any) -> Any:
        nested.append(adder.add(req, lambda level, text: None))  # Fusion pumped events
        return "outer"

    monkeypatch.setattr(StubFlow, "run", run_nested)
    assert adder.add(request, lambda level, text: None) == "outer"

    assert nested[0].ok is False
    assert "still running" in nested[0].message
    assert len(stub_flow.created) == 1


def test_the_adder_is_usable_again_after_a_keyboard_interrupt(
    fa: ModuleType, stub_flow: type[StubFlow], tmp_path: Path
) -> None:
    stub_flow.run_error = KeyboardInterrupt()
    adder = fa.FlowLibraryAdder(object(), StateStore(tmp_path / "s.json"), lambda _: None)
    with pytest.raises(KeyboardInterrupt):
        adder.add(make_request(tmp_path), lambda level, text: None)

    stub_flow.run_error = None
    stub_flow.result = "fine"
    assert adder.add(make_request(tmp_path), lambda level, text: None) == "fine"


# The real flow through the facade on the fake world ------------------------------------------


def test_the_real_flow_creates_then_merges_through_the_adsk_facade(
    fa: ModuleType, world: Fusion, api: Any, tmp_path: Path
) -> None:
    store = StateStore(tmp_path / "registry" / "state.json")
    statuses: list[tuple[str, str]] = []
    adder = fa.FlowLibraryAdder(api, store, world.logs.append)

    def status(level: str, text: str) -> None:
        statuses.append((level, text))

    first = adder.add(
        FlowRequest("C100", "MPN-100", write_part_lbr(tmp_path / "out", "C100", "MPN-100")), status
    )

    assert first.ok is True
    assert first.created_library is True
    assert [entry[0] for entry in world.journal] == ["text", "saveAs"]
    library_file = world.data.root.files[0]
    assert (library_file.name, library_file.fileExtension) == (LIBRARY_NAME, "flbr")
    assert store.load().library_file_id == library_file.id
    world.journal.clear()

    lbr = write_part_lbr(tmp_path / "out", "C200", "MPN-200")
    second = adder.add(FlowRequest("C200", "MPN-200", lbr), status)

    assert second.ok is True
    assert second.merged is True
    assert [entry[0] for entry in world.journal] == [
        "findFileById",  # find_library
        "findFileById",  # open_library
        "documents.open",
        "text",
        "text",
        "text",
        "save",
    ]
    assert world.commands[0] == f"Electron.run \"Use '{lbr.as_posix()}'\""
    assert re.fullmatch(r'Electron\.run "Copy "[^"]+\.dev@C200""', world.commands[1])
    assert world.commands[2] == 'Electron.run "Use "-C200""'
    assert world.calls("save") == [("save", LIBRARY_NAME, "Added C200 MPN-200")]
    assert set(store.load().parts) == {"C100", "C200"}
    assert statuses[-1][1] == second.message


def test_the_real_flow_with_3d_after_the_first_part(
    fa: ModuleType, world: Fusion, api: Any, casts: Casts, tmp_path: Path
) -> None:
    occurrence = OccurrenceWithTransform2()
    world.app.importManager.result = FakeCollection([occurrence])
    store = StateStore(tmp_path / "state.json")
    adder = fa.FlowLibraryAdder(api, store, world.logs.append)

    result = adder.add(
        FlowRequest(
            "C100",
            "MPN-100",
            write_part_lbr(tmp_path / "out", "C100", "MPN-100"),
            write_step(tmp_path),
        ),
        lambda level, text: None,
    )

    assert result.ok is True
    assert result.three_d == "started"
    assert [entry[0] for entry in world.journal] == [
        "text",  # Document.newDesignFromLocal
        "saveAs",  # LCSC2Fusion Library
        "text",  # Electron.Create3DPackage
        "saveAs",  # the 3D package design
        "createSTEPImportOptions",
        "importToTarget2",
    ]
    assert occurrence.transform2 is casts.matrices[0]


def test_a_3d_package_that_fails_is_a_warning_and_keeps_the_library(
    fa: ModuleType, world: Fusion, api: Any, casts: Casts, tmp_path: Path
) -> None:
    world.command_error = RuntimeError("RuntimeError: 3 : no 3D package for you")
    world.command_error_prefix = "Electron.Create3DPackage"
    store = StateStore(tmp_path / "state.json")
    adder = fa.FlowLibraryAdder(api, store, world.logs.append)

    result = adder.add(
        FlowRequest(
            "C100",
            "MPN-100",
            write_part_lbr(tmp_path / "out", "C100", "MPN-100"),
            write_step(tmp_path),
        ),
        lambda level, text: None,
    )

    assert result.ok is True
    assert result.three_d == "failed"
    assert any("no 3D package for you" in warning for warning in result.warnings)
    assert store.load().has_part("C100")


# The whole add-in: real HTTP transport, hub, controller and dispatcher --------------------


def controller_accepts_the_library_keywords() -> bool:
    controller = importlib.import_module("controller")
    parameters = inspect.signature(controller.Controller).parameters
    return "library" in parameters and "defer" in parameters


needs_library_keywords = pytest.mark.skipif(
    not controller_accepts_the_library_keywords(),
    reason="Controller does not accept the library/defer keywords yet (controller.py not landed)",
)


class RecordingApi:
    """A `FusionLibraryApi` that records its calls and the thread they ran on."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.libraries: dict[str, str] = {}
        self.gate = threading.Event()
        self.entered = threading.Event()
        self.block_create = False

    def _record(self, name: str) -> None:
        self.calls.append((name, threading.get_ident()))

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def find_library(self, file_id: str | None, name: str) -> LibraryRef | None:
        self._record("find_library")
        if file_id in self.libraries:
            return LibraryRef(file_id, self.libraries[file_id])
        return None

    def create_library_from_part(self, lbr_path: Path, name: str) -> LibraryRef:
        self._record("create_library_from_part")
        if self.block_create:
            self.entered.set()
            assert self.gate.wait(10), "the test never released the flow"
        ref = LibraryRef("urn:lib:1", name)
        self.libraries[ref.file_id] = name
        return ref

    def open_library(self, ref: LibraryRef) -> None:
        self._record("open_library")

    def merge_part(self, lbr_path: Path, deviceset: str) -> None:
        self._record("merge_part")

    def save_active(self, description: str) -> None:
        self._record("save_active")

    def create_3d_package(self, lbr_path: Path) -> None:
        self._record("create_3d_package")

    def import_step(self, step_path: Path) -> None:
        self._record("import_step")


class Browser:
    """Plays the part of the window: opens the launch URL, keeps the cookie, posts actions."""

    def __init__(self, launch_url: str) -> None:
        launch = urlsplit(launch_url)
        assert launch.port is not None
        self.port = launch.port
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", f"{launch.path}?{launch.query}")
        redirect = conn.getresponse()
        redirect.read()
        assert redirect.status == 302
        self.cookie = (redirect.getheader("Set-Cookie") or "").split(";")[0]
        conn.close()
        self._events_connection: http.client.HTTPConnection | None = None

    def open_events(self) -> EventStream:
        conn = self._events_connection = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=30
        )
        conn.request("GET", "/api/events", headers={"Cookie": self.cookie})
        response = conn.getresponse()
        assert response.status == 200
        return EventStream(response)

    def act(self, action: str, data: dict[str, Any]) -> tuple[int, Any]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request(
            "POST",
            "/api/act",
            body=json.dumps({"action": action, "data": json.dumps(data)}),
            headers={
                "Cookie": self.cookie,
                "Origin": f"http://127.0.0.1:{self.port}",
                "Content-Type": "application/json",
                "X-LCSC": "1",
            },
        )
        reply = conn.getresponse()
        payload = json.loads(reply.read())
        conn.close()
        return reply.status, payload


def events_of(stream: EventStream, action: str) -> list[dict[str, Any]]:
    return [json.loads(event["data"]) for event in list(stream.events) if event["action"] == action]


@needs_library_keywords
def test_add_with_fusion_true_runs_the_flow_on_the_main_thread_and_reports_fusion_done(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    browser_window = importlib.import_module("browser_window")
    controller = importlib.import_module("controller")
    core_service = importlib.import_module("core_service")
    libs = tmp_path / "libs"
    state_path = tmp_path / "state" / "state.json"
    monkeypatch.setenv("LCSC2FUSION_STATE", str(state_path))
    monkeypatch.setattr(controller, "default_out_dir", lambda: libs)
    recording = RecordingApi()
    monkeypatch.setattr(fa, "AdskLibraryApi", lambda app, ui, log: recording)

    def fake_add_part(self: Any, lcsc_id: str, out_dir: Path, with_3d: bool = False) -> Any:
        path = write_part_lbr(out_dir, lcsc_id, f"MPN-{lcsc_id}")
        placed = write_step(out_dir, f"{lcsc_id}.placed.step") if with_3d else None
        return controller.AddedPart(path=path, mpn=f"MPN-{lcsc_id}", placed_step_path=placed)

    monkeypatch.setattr(core_service.CorePartService, "add_part", fake_add_part)
    commands: list[list[str]] = []

    def fake_popen(command: list[str], **options: Any) -> FakeProcess:
        commands.append(command)
        return FakeProcess()

    monkeypatch.setattr(
        fa,
        "BrowserWindow",
        lambda *, log: browser_window.BrowserWindow(
            log=log,
            finder=lambda: browser_window.BrowserChoice("edge", Path("C:/fake/msedge.exe")),
            popen=fake_popen,
            profile_dir=tmp_path / "profile",
            window_api=FakeWindowApi(),
        ),
    )
    loop = MainLoop()
    fake = LoopFakeApp(FakeUi(), loop)
    monkeypatch.setattr(fa.adsk.core.Application, "get", staticmethod(lambda: fake))
    addin = fa.AddinApp(ADDIN_DIR)
    stream: EventStream | None = None
    try:
        loop.call(lambda: addin.start(True))
        (app_argument,) = [arg for arg in commands[0] if arg.startswith("--app=")]
        browser = Browser(app_argument.removeprefix("--app="))
        stream = browser.open_events()
        wait_until(lambda: addin._hub is not None and addin._hub.subscriber_count >= 1)

        # First part with the 3D model: the library is created, then the 3D package. The flow is
        # held inside the facade to prove that the HTTP request does not wait for it.
        recording.block_create = True
        status, reply = browser.act("add", {"lcsc_id": "C100", "with_3d": True, "fusion": True})
        assert (status, reply) == (200, {"reply": "OK"})
        assert recording.entered.wait(10), "the flow never started"
        assert recording.calls[-1][1] == loop.ident  # on the main thread, not an HTTP one
        assert events_of(stream, "fusion_done") == []

        # A request that arrives while the main thread is busy is served once the flow is over.
        late: list[tuple[int, Any]] = []
        asker = threading.Thread(target=lambda: late.append(browser.act("ready", {})), daemon=True)
        asker.start()
        wait_until(lambda: len(addin._dispatcher._queue) == 1)
        assert late == []
        recording.gate.set()
        wait_until(lambda: len(events_of(stream, "fusion_done")) == 1)
        asker.join(10)
        assert late == [(200, {"reply": "OK"})]

        (done,) = events_of(stream, "fusion_done")
        assert done["lcsc_id"] == "C100"
        assert done["ok"] is True
        assert done["created_library"] is True
        assert done["three_d"] == "started"
        assert done["library_name"] == LIBRARY_NAME
        assert recording.names == [
            "find_library",
            "create_library_from_part",
            "create_3d_package",
            "import_step",
        ]
        assert {ident for _, ident in recording.calls} == {loop.ident}
        progress = [event["text"] for event in events_of(stream, "status")]
        assert any(text.startswith(f"Creating the {LIBRARY_NAME}") for text in progress)
        assert any("3D package editor is open" in text for text in progress)
        assert json.loads(state_path.read_text(encoding="utf-8"))["library_file_id"] == "urn:lib:1"
        wait_until(lambda: events_of(stream, "busy")[-1] == {"value": False})

        # Second part, 2D only: it is merged into the library that exists now.
        recording.calls.clear()
        recording.block_create = False
        status, reply = browser.act("add", {"lcsc_id": "C200", "with_3d": False, "fusion": True})
        assert (status, reply) == (200, {"reply": "OK"})
        wait_until(lambda: len(events_of(stream, "fusion_done")) == 2)
        second = events_of(stream, "fusion_done")[1]
        assert second["ok"] is True
        assert second["merged"] is True
        assert second["created_library"] is False
        assert recording.names == ["find_library", "open_library", "merge_part", "save_active"]

        loop.call(addin.stop)
        assert stream.closed.wait(10)
    finally:
        loop.call(addin.stop)
        loop.close()
        if stream is not None:
            stream.closed.wait(5)


# Wiring and shutdown in AddinApp -------------------------------------------------------------


@pytest.fixture
def window_stack(fa: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """AddinApp with a fake window and transport but the real controller and dispatcher."""
    monkeypatch.setattr(
        fa,
        "HttpTransport",
        lambda hub, dispatch, log, static_dir: FakeTransport([], hub, dispatch, log, static_dir),
    )
    monkeypatch.setattr(fa, "BrowserWindow", lambda *, log: FakeWindow([], log))


def started_addin(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Any, FakeApp]:
    app = FakeApp(FakeUi())
    monkeypatch.setattr(fa.adsk.core.Application, "get", staticmethod(lambda: app))
    addin = fa.AddinApp(tmp_path / "LCSC2Fusion")
    addin.start(False)
    return addin, app


@needs_library_keywords
@pytest.mark.usefixtures("window_stack")
def test_the_add_in_wires_the_library_flow_and_the_dispatcher_into_the_controller(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    addin, _ = started_addin(fa, monkeypatch, tmp_path)
    try:
        controller = addin._dispatcher._handle.__self__
        assert controller.fusion_available is True
        adder = controller._library
        assert isinstance(adder, fa.FlowLibraryAdder)
        assert isinstance(adder._api, fa.AdskLibraryApi)
        assert adder._store is controller._service.state_store  # the registry the service uses
        assert controller._defer == addin._dispatcher.post
    finally:
        addin.stop()


@pytest.mark.usefixtures("window_stack")
def test_stop_drops_the_queued_tasks_and_refuses_new_ones(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    addin, app = started_addin(fa, monkeypatch, tmp_path)
    ran: list[str] = []
    dispatcher = addin._dispatcher
    dispatcher.post(lambda: ran.append("queued"))  # the main thread has not drained yet

    addin.stop()

    assert ran == []
    assert any("Dropped 1 queued task(s)" in line for line in app.logged)
    with pytest.raises(fa.DispatcherStoppedError):
        dispatcher.post(lambda: ran.append("late"))


@pytest.mark.usefixtures("window_stack")
def test_stopping_the_add_in_purges_the_library_modules(
    fa: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = dict(sys.modules)
    try:
        app = FakeApp(FakeUi())
        monkeypatch.setattr(fa.adsk.core.Application, "get", staticmethod(lambda: app))
        entry = importlib.import_module("LCSC2Fusion")
        entry.run(json.dumps({"IsApplicationStartup": True}))
        assert "library_registry" in sys.modules
        assert "fusion_library_flow" in sys.modules

        entry.stop(None)

        assert "library_registry" not in sys.modules
        assert "fusion_library_flow" not in sys.modules
        assert entry._app is None
    finally:
        for name in set(sys.modules) - set(saved):
            del sys.modules[name]
        sys.modules.update(saved)
