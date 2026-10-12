"""Tests for the Fusion library flow: a recording fake facade stands in for Fusion (no adsk)."""

from __future__ import annotations

import dataclasses
import itertools
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fusion_library_flow import (
    LIBRARY_NAME,
    FlowRequest,
    FlowResult,
    FusionLibraryFlow,
    LibraryRef,
    forget_library,
)
from library_registry import (
    LibraryState,
    PartRecord,
    StateStore,
    inspect_part_lbr,
    make_part_transform,
)

from lcsc2fusion.lbr_writer import package_fingerprint, write_library
from lcsc2fusion.lbr_writer.writer import sanitize_name
from lcsc2fusion.model import Part
from lcsc2fusion.parser.part import parse_part

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"
FIRST_LIBRARY_ID = "urn:lib:1"


def load_part(name: str) -> Part:
    return parse_part(json.loads((SYNTH / f"{name}.json").read_text(encoding="utf-8")))


class FakeApi:
    """Records every call in `events` (shared with the status sink, so order is checkable)."""

    def __init__(self, events: list[tuple[Any, ...]]) -> None:
        self.events = events
        self.libraries: dict[str, str] = {}  # file id -> name, as in the project root folder
        self.failures: dict[str, BaseException] = {}
        self.active: str | None = None
        self.merge_targets: list[str | None] = []
        self.on_call: dict[str, Callable[[], None]] = {}
        self._ids = itertools.count(1)

    def _enter(self, name: str, *args: object) -> None:
        self.events.append(("api", name, *args))
        hook = self.on_call.get(name)
        if hook is not None:
            hook()
        failure = self.failures.get(name)
        if failure is not None:
            raise failure

    def find_library(self, file_id: str | None, name: str) -> LibraryRef | None:
        self._enter("find_library", file_id, name)
        if file_id is not None and file_id in self.libraries:
            return LibraryRef(file_id, self.libraries[file_id])
        for known_id, known_name in self.libraries.items():
            if known_name == name:
                return LibraryRef(known_id, known_name)
        return None

    def create_library_from_part(self, lbr_path: Path, name: str) -> LibraryRef:
        self._enter("create_library_from_part", lbr_path, name)
        file_id = f"urn:lib:{next(self._ids)}"
        self.libraries[file_id] = name
        self.active = file_id
        return LibraryRef(file_id, name)

    def open_library(self, ref: LibraryRef) -> None:
        self._enter("open_library", ref)
        self.active = ref.file_id

    def merge_part(self, lbr_path: Path, deviceset: str) -> None:
        self._enter("merge_part", lbr_path, deviceset)
        self.merge_targets.append(self.active)

    def save_active(self, description: str) -> None:
        self._enter("save_active", description)

    def create_3d_package(self, lbr_path: Path) -> None:
        self._enter("create_3d_package", lbr_path)

    def import_step(self, step_path: Path) -> None:
        self._enter("import_step", step_path)

    @property
    def calls(self) -> list[tuple[Any, ...]]:
        return [event[1:] for event in self.events if event[0] == "api"]

    @property
    def call_names(self) -> list[str]:
        return [call[0] for call in self.calls]


@dataclass
class Harness:
    tmp_path: Path
    events: list[tuple[Any, ...]] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    api: FakeApi = field(init=False)
    store: StateStore = field(init=False)
    flow: FusionLibraryFlow = field(init=False)

    def __post_init__(self) -> None:
        self.api = FakeApi(self.events)
        self.store = StateStore(self.tmp_path / "registry" / "state.json")
        ticks = itertools.count(1000)
        self.flow = FusionLibraryFlow(
            self.api,
            self.store,
            self._status,
            self.logs.append,
            clock=lambda: float(next(ticks)),
        )

    def _status(self, level: str, text: str) -> None:
        self.events.append(("status", level, text))

    @property
    def statuses(self) -> list[tuple[str, str]]:
        return [(event[1], event[2]) for event in self.events if event[0] == "status"]

    def request(
        self,
        lcsc_id: str = "C100",
        mpn: str = "MPN-100",
        *,
        fixture: str = "r0603",
        step: bool = False,
        missing_step: bool = False,
        part: Part | None = None,
    ) -> FlowRequest:
        base = part if part is not None else load_part(fixture)
        written = dataclasses.replace(base, lcsc_id=lcsc_id, mpn=mpn)
        lbr = write_library(self.tmp_path / "out" / f"{lcsc_id}.lbr", [written])
        step_path: Path | None = None
        if step or missing_step:
            step_path = self.tmp_path / "out" / f"{lcsc_id}.placed.step"
            if not missing_step:
                step_path.write_text("ISO-10303-21;", encoding="utf-8")
        return FlowRequest(lcsc_id, mpn, lbr, step_path)

    def clear_events(self) -> None:
        self.events.clear()

    def state_bytes(self) -> bytes | None:
        return self.store.path.read_bytes() if self.store.path.exists() else None


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def assert_last_status_is_the_message(h: Harness, result: FlowResult) -> None:
    assert h.statuses, "no status line at all"
    assert h.statuses[-1][1] == result.message


def run_first_part(h: Harness) -> FlowResult:
    result = h.flow.run(h.request("C100", "MPN-100"))
    assert result.ok
    h.clear_events()
    h.logs.clear()
    return result


# ----- first part: the library is created ------------------------------------------------------


def test_first_part_creates_the_library_without_merging(h: Harness) -> None:
    request = h.request()
    result = h.flow.run(request)

    assert result.ok
    assert (result.created_library, result.merged, result.already_present) == (True, False, False)
    assert result.three_d == "none"
    assert result.library_name == LIBRARY_NAME
    assert result.warnings == ()
    assert h.api.calls == [
        ("find_library", None, LIBRARY_NAME),
        ("create_library_from_part", request.lbr_path, LIBRARY_NAME),
    ]
    assert h.statuses == [
        ("info", "Creating the LCSC2Fusion Library from C100 (MPN-100)..."),
        ("ok", result.message),
    ]
    assert "C100" in result.message


def test_first_part_is_recorded_in_the_registry(h: Harness) -> None:
    request = h.request()
    h.flow.run(request)

    state = h.store.load()
    info = inspect_part_lbr(request.lbr_path)
    assert state.library_file_id == FIRST_LIBRARY_ID
    assert state.library_name == LIBRARY_NAME
    assert state.parts == {
        "C100": PartRecord(
            lcsc_id="C100",
            deviceset=sanitize_name("MPN-100"),
            package=next(iter(info.packages)),
            fingerprint=next(iter(info.packages.values())),
            added_at=1000.0,  # the injected clock
        )
    }
    assert state.packages == info.packages


# ----- later parts: merge -----------------------------------------------------------------------


def test_second_part_is_merged_in_exactly_this_order(h: Harness) -> None:
    run_first_part(h)
    request = h.request("C200", "MPN-200", fixture="soic8")
    result = h.flow.run(request)

    assert result.ok
    assert (result.created_library, result.merged, result.already_present) == (False, True, False)
    assert h.api.calls == [
        ("find_library", FIRST_LIBRARY_ID, LIBRARY_NAME),
        ("open_library", LibraryRef(FIRST_LIBRARY_ID, LIBRARY_NAME)),
        ("merge_part", request.lbr_path, sanitize_name("MPN-200")),
        ("save_active", "Added C200 MPN-200"),
    ]
    assert h.api.merge_targets == [FIRST_LIBRARY_ID]  # merged into the opened library


def test_second_part_status_lines_come_before_each_slow_step(h: Harness) -> None:
    run_first_part(h)
    result = h.flow.run(h.request("C200", "MPN-200", fixture="soic8"))

    assert [event[1] if event[0] == "api" else event[2] for event in h.events] == [
        "find_library",
        f"Opening the {LIBRARY_NAME}...",
        "open_library",
        "Merging C200 (MPN-200) into the library...",
        "merge_part",
        "Saving the library...",
        "save_active",
        result.message,
    ]
    assert [level for level, _ in h.statuses] == ["info", "info", "info", "ok"]
    assert_last_status_is_the_message(h, result)


def test_the_registry_is_written_after_the_merge_and_save(h: Harness) -> None:
    run_first_part(h)
    seen: list[bytes | None] = []

    h.api.on_call["save_active"] = lambda: seen.append(h.state_bytes())
    before = h.state_bytes()
    h.flow.run(h.request("C200", "MPN-200", fixture="soic8"))

    assert seen == [before]  # still the old registry while the library was being saved
    state = h.store.load()
    assert sorted(state.parts) == ["C100", "C200"]
    assert len(state.packages) == 2


# ----- the same part twice ----------------------------------------------------------------------


def test_the_same_part_twice_is_not_merged_again(h: Harness) -> None:
    request = h.request()
    h.flow.run(request)
    saved = h.state_bytes()
    h.clear_events()

    result = h.flow.run(request)

    assert result.ok
    assert (result.created_library, result.merged, result.already_present) == (False, False, True)
    assert result.three_d == "none"
    assert h.api.call_names == ["find_library"]
    assert "already" in result.message
    assert "C100" in result.message
    assert h.statuses == [("info", result.message)]
    assert h.state_bytes() == saved  # not even rewritten


def test_the_same_part_three_times_stays_idempotent(h: Harness) -> None:
    request = h.request()
    results = [h.flow.run(request) for _ in range(3)]
    assert [r.created_library for r in results] == [True, False, False]
    assert [r.already_present for r in results] == [False, True, True]
    assert h.api.call_names.count("create_library_from_part") == 1
    assert h.api.call_names.count("merge_part") == 0


def test_a_part_already_present_still_gets_its_3d_package(h: Harness) -> None:
    h.flow.run(h.request())
    h.clear_events()
    again = h.request(step=True)

    result = h.flow.run(again)

    assert result.ok
    assert result.already_present
    assert result.three_d == "started"
    assert h.api.calls == [
        ("find_library", FIRST_LIBRARY_ID, LIBRARY_NAME),
        ("open_library", LibraryRef(FIRST_LIBRARY_ID, LIBRARY_NAME)),  # 3D needs the library open
        ("create_3d_package", again.lbr_path),
        ("import_step", again.placed_step_path),
    ]
    assert "already" in result.message
    assert "Finish, Save, OK" in result.message


def test_ids_are_compared_normalised(h: Harness) -> None:
    h.flow.run(h.request("C100"))
    result = h.flow.run(dataclasses.replace(h.request("C100"), lcsc_id=" c100 "))
    assert result.already_present


# ----- stale registry ---------------------------------------------------------------------------


def test_deleted_library_means_a_new_one_and_a_fresh_registry(h: Harness) -> None:
    h.flow.run(h.request("C100", "MPN-100"))
    h.api.libraries.clear()  # the user deleted the document in the data panel
    h.clear_events()

    request = h.request("C200", "MPN-200", fixture="soic8")
    result = h.flow.run(request)

    assert result.ok
    assert result.created_library
    assert not result.merged
    assert h.api.call_names == ["find_library", "create_library_from_part"]
    assert len(result.warnings) == 1
    assert "no longer exists" in result.warnings[0]
    assert "new one" in result.warnings[0]
    assert ("warn", result.warnings[0]) in h.statuses
    state = h.store.load()
    assert state.library_file_id == "urn:lib:2"
    assert sorted(state.parts) == ["C200"]  # C100 lived in the deleted library
    assert set(state.packages) == set(inspect_part_lbr(request.lbr_path).packages)


def test_a_part_of_the_deleted_library_is_added_again_not_skipped(h: Harness) -> None:
    request = h.request("C100", "MPN-100")
    h.flow.run(request)
    h.api.libraries.clear()

    result = h.flow.run(request)

    assert result.ok
    assert result.created_library
    assert not result.already_present


def test_stale_stored_id_but_the_library_exists_under_its_name(h: Harness) -> None:
    h.store.save(
        LibraryState("urn:gone", LIBRARY_NAME).with_part(
            PartRecord("C1", "DEV1", "PKG1", "f" * 64, 1.0), {"PKG1": "f" * 64}
        )
    )
    h.api.libraries["urn:renewed"] = LIBRARY_NAME
    request = h.request("C200", "MPN-200", fixture="soic8")

    result = h.flow.run(request)

    assert result.ok
    assert result.merged
    assert not result.created_library
    assert result.warnings == ()
    assert h.api.calls[:2] == [
        ("find_library", "urn:gone", LIBRARY_NAME),
        ("open_library", LibraryRef("urn:renewed", LIBRARY_NAME)),
    ]
    state = h.store.load()
    assert state.library_file_id == "urn:renewed"
    assert sorted(state.parts) == ["C1", "C200"]


def test_corrupt_registry_is_reported_and_the_library_is_found_by_name(h: Harness) -> None:
    h.store.path.parent.mkdir(parents=True)
    h.store.path.write_text("{ not json", encoding="utf-8")
    h.api.libraries["urn:existing"] = LIBRARY_NAME

    result = h.flow.run(h.request())

    assert result.ok
    assert result.merged
    assert any("empty registry" in w for w in result.warnings)
    assert h.api.calls[0] == ("find_library", None, LIBRARY_NAME)
    assert sorted(h.store.load().parts) == ["C100"]  # a valid registry again
    assert h.store.warnings == ()


# ----- failures: nothing is recorded ------------------------------------------------------------


def test_merge_failure_reports_it_and_leaves_the_registry_alone(h: Harness) -> None:
    run_first_part(h)
    saved = h.state_bytes()
    h.api.failures["merge_part"] = RuntimeError("Electron refused the command")

    result = h.flow.run(h.request("C200", "MPN-200", fixture="soic8"))

    assert not result.ok
    assert not result.merged
    assert result.three_d == "none"
    assert "merge the part" in result.message
    assert "Electron refused the command" in result.message
    assert "unsaved changes" in result.message
    assert "save_active" not in h.api.call_names
    assert h.state_bytes() == saved
    assert h.statuses[-1] == ("error", result.message)
    assert any("Electron refused the command" in line for line in h.logs)
    assert any("Traceback" in line for line in h.logs)


@pytest.mark.parametrize("method", ["find_library", "open_library", "merge_part"])
def test_a_failing_step_stops_the_flow_and_records_nothing(h: Harness, method: str) -> None:
    run_first_part(h)
    saved = h.state_bytes()
    h.api.failures[method] = ValueError("boom")

    result = h.flow.run(h.request("C200", "MPN-200", fixture="soic8"))

    assert not result.ok
    assert "boom" in result.message
    assert h.state_bytes() == saved
    assert h.api.call_names[-1] == method  # nothing was tried after the failure
    assert_last_status_is_the_message(h, result)


def test_failure_to_create_the_library_records_nothing(h: Harness) -> None:
    h.api.failures["create_library_from_part"] = RuntimeError("cannot save into the project")

    result = h.flow.run(h.request())

    assert not result.ok
    assert not result.created_library
    assert "create the library" in result.message
    assert "cannot save into the project" in result.message
    assert h.state_bytes() is None


def test_save_failure_after_the_merge_is_a_warning_and_is_not_recorded(h: Harness) -> None:
    run_first_part(h)
    saved = h.state_bytes()
    h.api.failures["save_active"] = OSError("the cloud said no")

    result = h.flow.run(h.request("C200", "MPN-200", fixture="soic8", step=True))

    assert not result.ok
    assert result.merged  # the part IS in the open library
    assert "merged" in result.message
    assert "not recorded" in result.message
    assert "the cloud said no" in result.message
    assert h.statuses[-1] == ("warn", result.message)
    assert h.state_bytes() == saved
    assert result.three_d == "skipped"
    assert "create_3d_package" not in h.api.call_names


def test_failed_2d_skips_the_requested_3d(h: Harness) -> None:
    run_first_part(h)
    h.api.failures["merge_part"] = RuntimeError("x")
    result = h.flow.run(h.request("C200", "MPN-200", fixture="soic8", step=True))
    assert not result.ok
    assert result.three_d == "skipped"
    assert "create_3d_package" not in h.api.call_names


def test_missing_part_library_file_is_reported_before_touching_fusion(h: Harness) -> None:
    request = h.request()
    request.lbr_path.unlink()

    result = h.flow.run(request)

    assert not result.ok
    assert "does not exist" in result.message
    assert str(request.lbr_path) in result.message
    assert h.api.calls == []
    assert h.state_bytes() is None
    assert h.statuses == [("error", result.message)]


def test_unreadable_part_library_is_reported_before_touching_fusion(h: Harness) -> None:
    request = h.request()
    request.lbr_path.write_text("<not a library", encoding="utf-8")
    result = h.flow.run(request)
    assert not result.ok
    assert request.lbr_path.name in result.message
    assert h.api.calls == []


def test_a_different_footprint_under_a_known_name_is_refused(h: Harness) -> None:
    request = h.request()
    name = next(iter(inspect_part_lbr(request.lbr_path).packages))
    h.store.save(
        LibraryState(FIRST_LIBRARY_ID, LIBRARY_NAME).with_part(
            PartRecord("C1", "DEV1", name, "0" * 64, 1.0), {name: "0" * 64}
        )
    )
    h.api.libraries[FIRST_LIBRARY_ID] = LIBRARY_NAME
    saved = h.state_bytes()

    result = h.flow.run(request)

    assert not result.ok
    assert "different shape" in result.message
    assert name in result.message
    assert h.api.call_names == ["find_library"]  # nothing was opened or merged
    assert h.state_bytes() == saved


def test_a_reused_device_set_name_is_a_warning(h: Harness) -> None:
    run_first_part(h)

    result = h.flow.run(h.request("C300", "MPN-100", fixture="soic8"))  # same MPN, other LCSC id

    assert result.ok
    assert result.merged
    (warning,) = result.warnings
    assert "MPN-100" in warning
    assert "C100" in warning
    assert sorted(h.store.load().parts) == ["C100", "C300"]
    assert h.statuses[-1][0] == "warn"


def test_registry_that_cannot_be_written_is_a_warning_not_a_failure(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a folder is needed", encoding="utf-8")
    harness = Harness(tmp_path)
    harness.store = StateStore(blocker / "state.json")
    harness.flow = FusionLibraryFlow(
        harness.api, harness.store, harness._status, harness.logs.append
    )

    result = harness.flow.run(harness.request())

    assert result.ok
    assert result.created_library
    assert any("registry" in w and "could not be written" in w for w in result.warnings)
    assert harness.statuses[-1][0] == "warn"


# ----- 3D ---------------------------------------------------------------------------------------


def test_3d_after_the_first_part(h: Harness) -> None:
    request = h.request(step=True)
    result = h.flow.run(request)

    assert result.ok
    assert result.created_library
    assert result.three_d == "started"
    assert h.api.calls == [
        ("find_library", None, LIBRARY_NAME),
        ("create_library_from_part", request.lbr_path, LIBRARY_NAME),
        ("create_3d_package", request.lbr_path),
        ("import_step", request.placed_step_path),
    ]
    assert "check the position" in result.message
    assert "Finish, Save, OK" in result.message
    assert h.statuses[-1] == ("ok", result.message)
    assert sorted(h.store.load().parts) == ["C100"]


def test_3d_after_a_merge_comes_after_the_save(h: Harness) -> None:
    run_first_part(h)
    request = h.request("C200", "MPN-200", fixture="soic8", step=True)

    result = h.flow.run(request)

    assert result.three_d == "started"
    assert h.api.call_names == [
        "find_library",
        "open_library",
        "merge_part",
        "save_active",
        "create_3d_package",
        "import_step",
    ]
    texts = [text for _, text in h.statuses]
    assert texts.index("Saving the library...") < texts.index(
        "Creating the 3D package for C200 (MPN-200)..."
    )
    assert texts.index("Creating the 3D package for C200 (MPN-200)...") < texts.index(
        "Importing the 3D model..."
    )
    assert_last_status_is_the_message(h, result)


def test_no_3d_step_without_a_placed_step(h: Harness) -> None:
    result = h.flow.run(h.request())
    assert result.three_d == "none"
    assert "create_3d_package" not in h.api.call_names
    assert "import_step" not in h.api.call_names


def test_missing_step_file_skips_3d_and_is_reported_before_touching_fusion(h: Harness) -> None:
    request = h.request(missing_step=True)

    result = h.flow.run(request)

    assert result.ok  # the 2D result stands
    assert result.created_library
    assert result.three_d == "skipped"
    assert "create_3d_package" not in h.api.call_names
    (warning,) = result.warnings
    assert str(request.placed_step_path) in warning
    first = h.events[0]
    assert first[:2] == ("status", "warn")  # before the first Fusion call
    assert h.statuses[-1][0] == "warn"


def test_3d_failure_does_not_undo_the_2d_success(h: Harness) -> None:
    run_first_part(h)
    h.api.failures["create_3d_package"] = RuntimeError("Create3DPackage is not available")

    result = h.flow.run(h.request("C200", "MPN-200", fixture="soic8", step=True))

    assert result.ok
    assert result.merged
    assert result.three_d == "failed"
    (warning,) = result.warnings
    assert "create the 3D package" in warning
    assert "Create3DPackage is not available" in warning
    assert "import_step" not in h.api.call_names
    assert sorted(h.store.load().parts) == ["C100", "C200"]  # recorded all the same
    assert h.statuses[-1][0] == "warn"
    assert_last_status_is_the_message(h, result)
    assert any("Create3DPackage is not available" in line for line in h.logs)


def test_import_failure_tells_the_user_where_the_file_is(h: Harness) -> None:
    request = h.request(step=True)
    h.api.failures["import_step"] = RuntimeError("STEP import failed")

    result = h.flow.run(request)

    assert result.ok
    assert result.three_d == "failed"
    (warning,) = result.warnings
    assert "STEP import failed" in warning
    assert str(request.placed_step_path) in warning  # for a manual import
    assert sorted(h.store.load().parts) == ["C100"]


def test_3d_failure_when_reopening_the_library_for_an_existing_part(h: Harness) -> None:
    h.flow.run(h.request())
    h.api.failures["open_library"] = RuntimeError("cannot open")

    result = h.flow.run(h.request(step=True))

    assert result.ok
    assert result.already_present
    assert result.three_d == "failed"
    assert "create_3d_package" not in h.api.call_names


# ----- robustness -------------------------------------------------------------------------------


@pytest.mark.parametrize("exception", [KeyError(), ValueError("x"), OSError(13, "denied")])
def test_run_never_raises_for_any_exception_from_fusion(h: Harness, exception: Exception) -> None:
    for method in ("find_library", "create_library_from_part"):
        h.api.failures.clear()
        h.api.failures[method] = exception
        result = h.flow.run(h.request())
        assert isinstance(result, FlowResult)
        assert not result.ok
        assert result.message
        assert_last_status_is_the_message(h, result)


def test_an_exception_without_a_message_still_gives_a_readable_text(h: Harness) -> None:
    h.api.failures["find_library"] = KeyError()
    result = h.flow.run(h.request())
    assert "KeyError" in result.message


def test_unexpected_errors_in_the_flow_itself_become_a_failed_result(h: Harness) -> None:
    class BrokenStore(StateStore):
        def load(self) -> LibraryState:
            raise RuntimeError("registry blew up")

    flow = FusionLibraryFlow(h.api, BrokenStore(h.store.path), h._status, h.logs.append)

    result = flow.run(h.request())

    assert not result.ok
    assert "registry blew up" in result.message
    assert h.api.calls == []
    assert any("Traceback" in line and "registry blew up" in line for line in h.logs)


def test_keyboard_interrupt_is_not_swallowed_and_the_flow_stays_usable(h: Harness) -> None:
    h.api.failures["find_library"] = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        h.flow.run(h.request())

    h.api.failures.clear()
    result = h.flow.run(h.request())
    assert result.ok
    assert "still running" not in result.message


def test_broken_status_and_log_sinks_do_not_break_the_flow(h: Harness) -> None:
    def broken_status(level: str, text: str) -> None:
        raise RuntimeError("palette is gone")

    def broken_log(text: str) -> None:
        raise RuntimeError("log is gone")

    flow = FusionLibraryFlow(h.api, h.store, broken_status, broken_log)
    result = flow.run(h.request())
    assert result.ok
    assert result.created_library


def test_a_second_run_during_a_run_is_refused(h: Harness) -> None:
    inner: list[FlowResult] = []
    other = h.request("C200", "MPN-200", fixture="soic8")
    h.api.on_call["find_library"] = lambda: inner.append(h.flow.run(other))

    outer = h.flow.run(h.request())

    assert outer.ok
    (refused,) = inner
    assert not refused.ok
    assert "still running" in refused.message
    assert h.api.call_names.count("find_library") == 1  # the refused run did not reach Fusion
    h.api.on_call.clear()
    assert h.flow.run(other).ok  # and afterwards the flow is free again


def test_result_and_request_are_frozen(h: Harness) -> None:
    request = h.request()
    result = h.flow.run(request)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.ok = False  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.lcsc_id = "C1"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        LibraryRef("a", "b").name = "c"  # type: ignore[misc]


def test_forget_library_makes_the_next_part_look_for_the_library_afresh(h: Harness) -> None:
    request = h.request()
    h.flow.run(request)
    forget_library(h.store)
    h.clear_events()

    again = h.flow.run(request)

    assert h.api.calls[0] == ("find_library", None, LIBRARY_NAME)  # no stored id any more
    assert again.merged  # the document still exists in Fusion, so it is found by name
    assert not again.already_present

    h.api.libraries.clear()  # once the user removed the document, "start over" creates a new one
    forget_library(h.store)
    assert h.flow.run(request).created_library


# ----- with the part transform ------------------------------------------------------------------


def test_clashing_footprints_are_renamed_by_the_transform_and_merge_cleanly(h: Harness) -> None:
    first = load_part("soic8")
    pad = first.package.pads[0]
    reshaped = dataclasses.replace(
        first.package, pads=(dataclasses.replace(pad, x=pad.x + 0.5), *first.package.pads[1:])
    )
    second = dataclasses.replace(first, package=reshaped)  # same name, other geometry
    assert second.package.name == first.package.name

    one = h.flow.run(h.request("C500", "MPN-500", part=first))
    converted = make_part_transform(h.store.load())(second)
    two = h.flow.run(h.request("C600", "MPN-600", part=converted))

    assert one.ok
    assert two.ok
    assert two.merged
    state = h.store.load()
    assert len(state.packages) == 2
    assert converted.package.name != first.package.name
    assert state.packages[sanitize_name(first.package.name)] == package_fingerprint(first.package)
    assert state.packages[converted.package.name] == package_fingerprint(second.package)

    # converting the same part again gives the same name, so the flow knows it
    again = make_part_transform(state)(second)
    assert again.package.name == converted.package.name
    assert h.flow.run(h.request("C600", "MPN-600", part=again)).already_present


def test_without_the_transform_the_flow_refuses_the_clash(h: Harness) -> None:
    first = load_part("soic8")
    pad = first.package.pads[0]
    reshaped = dataclasses.replace(
        first.package, pads=(dataclasses.replace(pad, x=pad.x + 0.5), *first.package.pads[1:])
    )
    h.flow.run(h.request("C500", "MPN-500", part=first))
    h.clear_events()
    saved = h.state_bytes()

    result = h.flow.run(
        h.request("C600", "MPN-600", part=dataclasses.replace(first, package=reshaped))
    )

    assert not result.ok
    assert "different shape" in result.message
    assert h.api.call_names == ["find_library"]
    assert h.state_bytes() == saved
