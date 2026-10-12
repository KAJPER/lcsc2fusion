"""Tests for the palette controller (pure Python, no Fusion, no network)."""

from __future__ import annotations

import dataclasses
import inspect
import json
import subprocess
import sys
import textwrap
import types
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypeVar

import controller
import pytest
from controller import (
    ACTION_ADD,
    ACTION_ADDED,
    ACTION_BUSY,
    ACTION_ERROR,
    ACTION_OPEN_FOLDER,
    ACTION_OPEN_LIBRARY,
    ACTION_READY,
    ACTION_RESULTS,
    ACTION_SEARCH,
    ACTION_STATUS,
    ADD_REQUEST_FIELDS,
    ADDED_FIELDS,
    GENERIC_ERROR_TEXT,
    MANUAL_STEPS,
    AddedPart,
    Controller,
    ImportOutcome,
    JobResult,
    search_result_to_dict,
)

from lcsc2fusion.errors import ConversionError, NetworkError, PartNotFoundError, RateLimitedError
from lcsc2fusion.model import ModelFiles, SearchResult

T = TypeVar("T")

RP2040 = SearchResult(
    lcsc_id="C2040",
    mpn="RP2040",
    manufacturer="Raspberry Pi",
    package="LQFN-56",
    description="Dual ARM Cortex-M0+ MCU, 10kΩ test ±1%",
    datasheet_url="https://example.invalid/rp2040.pdf",
    library_type="Extended",
    stock=1200,
)
RESISTOR = SearchResult(
    lcsc_id="C25804",
    mpn="0603WAF1002T5E",
    manufacturer="UNI-ROYAL",
    package="0603",
    description="10k 0603",
    library_type="Basic",
)


# Fakes ---------------------------------------------------------------------------------------


class FakeService:
    def __init__(self, out_check: Callable[[Path], None] | None = None) -> None:
        self.search_calls: list[tuple[str, int]] = []
        self.add_calls: list[tuple[str, Path]] = []
        self.add_3d_flags: list[bool] = []
        self.produce_models = True  # with_3d requested: write the STEP files unless False
        self.search_results: list[SearchResult] = [RP2040, RESISTOR]
        self.search_error: Exception | None = None
        self.add_error: Exception | None = None
        self.add_mpn = "RP2040"
        self.add_warnings: tuple[str, ...] = ()

    def search(self, query: str, limit: int) -> list[SearchResult]:
        self.search_calls.append((query, limit))
        if self.search_error is not None:
            raise self.search_error
        return list(self.search_results)

    def add_part(self, lcsc_id: str, out_dir: Path, with_3d: bool = False) -> AddedPart:
        self.add_calls.append((lcsc_id, out_dir))
        self.add_3d_flags.append(with_3d)
        if self.add_error is not None:
            raise self.add_error
        path = out_dir / f"{lcsc_id}.lbr"
        path.write_text("<eagle/>", encoding="utf-8")
        step: Path | None = None
        placed: Path | None = None
        if with_3d and self.produce_models:
            step = out_dir / f"{lcsc_id}.step"
            placed = out_dir / f"{lcsc_id}.placed.step"
            step.write_text("ISO-10303-21;", encoding="utf-8")
            placed.write_text("ISO-10303-21;", encoding="utf-8")
        return AddedPart(
            path=path,
            mpn=self.add_mpn,
            warnings=self.add_warnings,
            step_path=step,
            placed_step_path=placed,
        )


class FakeImporter:
    def __init__(self) -> None:
        self.opened: list[Path] = []
        self.outcome = ImportOutcome(opened=True, message="sent")
        self.error: Exception | None = None

    def open_library(self, path: Path) -> ImportOutcome:
        self.opened.append(path)
        if self.error is not None:
            raise self.error
        return self.outcome


class RecordingSink:
    def __init__(self) -> None:
        self.raw: list[tuple[str, str]] = []

    def send(self, action: str, data: str) -> None:
        self.raw.append((action, data))

    @property
    def messages(self) -> list[tuple[str, dict[str, Any]]]:
        return [(action, json.loads(data)) for action, data in self.raw]

    def actions(self) -> list[str]:
        return [action for action, _ in self.raw]

    def of(self, action: str) -> list[dict[str, Any]]:
        return [data for name, data in self.messages if name == action]

    def last(self, action: str) -> dict[str, Any]:
        return self.of(action)[-1]


class SyncRunner:
    def run(self, fn: Callable[[], T], on_done: Callable[[JobResult[T]], None]) -> None:
        try:
            result: JobResult[T] = JobResult(value=fn())
        except Exception as exc:
            result = JobResult(error=exc)
        on_done(result)


class DeferredRunner:
    """Keeps the job until `finish()`, like a worker thread that has not completed yet."""

    def __init__(self) -> None:
        self.pending: list[Callable[[], None]] = []

    def run(self, fn: Callable[[], T], on_done: Callable[[JobResult[T]], None]) -> None:
        def complete() -> None:
            try:
                result: JobResult[T] = JobResult(value=fn())
            except Exception as exc:
                result = JobResult(error=exc)
            on_done(result)

        self.pending.append(complete)

    def finish(self) -> None:
        pending, self.pending = self.pending, []
        for complete in pending:
            complete()


class Harness(SimpleNamespace):
    controller: Controller
    service: FakeService
    importer: FakeImporter
    sink: RecordingSink
    logs: list[str]
    opened_folders: list[Path]
    out_dir: Path


def make_harness(tmp_path: Path, *, runner: Any = None, out_dir: Path | None = None) -> Harness:
    service = FakeService()
    importer = FakeImporter()
    sink = RecordingSink()
    logs: list[str] = []
    opened_folders: list[Path] = []
    target = out_dir if out_dir is not None else tmp_path / "libs"
    ctrl = Controller(
        service,
        importer,
        sink,
        runner if runner is not None else SyncRunner(),
        logs.append,
        out_dir=target,
        folder_opener=opened_folders.append,
    )
    return Harness(
        controller=ctrl,
        service=service,
        importer=importer,
        sink=sink,
        logs=logs,
        opened_folders=opened_folders,
        out_dir=target,
    )


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return make_harness(tmp_path)


def call(harness: Harness, action: str, payload: Any = None) -> str:
    data = (
        payload if isinstance(payload, str) else json.dumps(payload if payload is not None else {})
    )
    return harness.controller.handle(action, data)


# Protocol constants --------------------------------------------------------------------------


def test_action_constants_are_unique_and_complete() -> None:
    assert controller.INCOMING_ACTIONS == (
        ACTION_READY,
        ACTION_SEARCH,
        ACTION_ADD,
        ACTION_OPEN_FOLDER,
        ACTION_OPEN_LIBRARY,
    )
    assert controller.OUTGOING_ACTIONS == (
        ACTION_STATUS,
        ACTION_RESULTS,
        ACTION_ADDED,
        ACTION_BUSY,
        ACTION_ERROR,
    )
    everything = controller.INCOMING_ACTIONS + controller.OUTGOING_ACTIONS
    assert len(set(everything)) == len(everything)


def test_payload_field_constants_are_unique() -> None:
    assert ADD_REQUEST_FIELDS == ("lcsc_id", "with_3d")
    assert len(set(ADDED_FIELDS)) == len(ADDED_FIELDS)
    assert {"step_path", "placed_step_path", "with_3d"} <= set(ADDED_FIELDS)


# ready ---------------------------------------------------------------------------------------


def test_ready_reports_idle_and_output_directory(h: Harness) -> None:
    assert call(h, ACTION_READY) == "OK"
    assert h.sink.last(ACTION_BUSY) == {"value": False}
    status = h.sink.last(ACTION_STATUS)
    assert status["level"] == "info"
    assert str(h.out_dir) in status["text"]


def test_ready_accepts_empty_payload_string(h: Harness) -> None:
    assert h.controller.handle(ACTION_READY, "") == "OK"


def test_ready_while_busy_reports_busy(tmp_path: Path) -> None:
    deferred = DeferredRunner()
    harness = make_harness(tmp_path, runner=deferred)
    call(harness, ACTION_SEARCH, {"query": "C2040"})
    harness.sink.raw.clear()
    call(harness, ACTION_READY)
    assert harness.sink.last(ACTION_BUSY) == {"value": True}


# search --------------------------------------------------------------------------------------


def test_search_sends_busy_status_results_in_order(h: Harness) -> None:
    assert call(h, ACTION_SEARCH, {"query": "RP2040"}) == "OK"
    assert h.sink.actions() == [
        ACTION_BUSY,
        ACTION_STATUS,
        ACTION_RESULTS,
        ACTION_STATUS,
        ACTION_BUSY,
    ]
    assert [m["value"] for m in h.sink.of(ACTION_BUSY)] == [True, False]
    results = h.sink.last(ACTION_RESULTS)
    assert results["query"] == "RP2040"
    assert [item["lcsc_id"] for item in results["items"]] == ["C2040", "C25804"]
    assert h.sink.last(ACTION_STATUS)["level"] == "ok"
    assert h.service.search_calls == [("RP2040", controller.SEARCH_LIMIT)]
    assert h.controller.busy is False


def test_search_without_hits_warns(h: Harness) -> None:
    h.service.search_results = []
    call(h, ACTION_SEARCH, {"query": "zzzz"})
    assert h.sink.last(ACTION_RESULTS) == {"query": "zzzz", "items": []}
    assert h.sink.last(ACTION_STATUS)["level"] == "warn"


def test_search_normalises_lcsc_ids_and_whitespace(h: Harness) -> None:
    call(h, ACTION_SEARCH, {"query": "  c2040 "})
    call(h, ACTION_SEARCH, {"query": "  0603    100nF\t"})
    assert [q for q, _ in h.service.search_calls] == ["C2040", "0603 100nF"]


def test_search_result_dict_is_json_safe_and_complete() -> None:
    data = search_result_to_dict(RP2040)
    assert set(data) == {
        "lcsc_id",
        "mpn",
        "manufacturer",
        "package",
        "description",
        "datasheet_url",
        "image_url",
        "library_type",
        "stock",
    }
    assert data["stock"] == 1200
    assert data["image_url"] == ""
    assert json.loads(json.dumps(data)) == data
    assert search_result_to_dict(RESISTOR)["stock"] is None


def test_unicode_is_sent_as_ascii_json(h: Harness) -> None:
    call(h, ACTION_SEARCH, {"query": "C2040"})
    raw = next(data for action, data in h.sink.raw if action == ACTION_RESULTS)
    assert raw.isascii()
    assert h.sink.last(ACTION_RESULTS)["items"][0]["description"] == RP2040.description


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"query": ""},
        {"query": "   "},
        {"query": 5},
        {"query": None},
        {"query": ["C2040"]},
        {"query": "x" * (controller.MAX_QUERY_LENGTH + 1)},
        {"query": "bad\x00char"},
    ],
)
def test_search_rejects_invalid_query(h: Harness, payload: dict[str, Any]) -> None:
    assert call(h, ACTION_SEARCH, payload) == "ERROR"
    assert h.sink.actions() == [ACTION_ERROR]
    assert h.service.search_calls == []
    assert h.controller.busy is False


def test_search_accepts_query_at_length_limit(h: Harness) -> None:
    call(h, ACTION_SEARCH, {"query": "x" * controller.MAX_QUERY_LENGTH})
    assert len(h.service.search_calls) == 1


# add -----------------------------------------------------------------------------------------


def test_add_saves_reports_and_never_opens_automatically(h: Harness) -> None:
    h.service.add_warnings = ("C2040: polygon pad approximated",)
    assert call(h, ACTION_ADD, {"lcsc_id": "C2040"}) == "OK"
    added = h.sink.last(ACTION_ADDED)
    expected_path = h.out_dir / "C2040.lbr"
    assert added["lcsc_id"] == "C2040"
    assert added["path"] == str(expected_path)
    assert added["mpn"] == "RP2040"
    assert added["warnings"] == ["C2040: polygon pad approximated"]
    assert added["opened"] is False
    assert MANUAL_STEPS in added["instructions"]
    assert "Open in Fusion Electronics" in added["instructions"]
    assert h.importer.opened == []  # opening is a separate palette action
    assert expected_path.is_file()
    assert h.sink.last(ACTION_STATUS)["level"] == "warn"
    assert h.sink.actions()[0] == ACTION_BUSY
    assert h.sink.actions()[-1] == ACTION_BUSY
    assert h.controller.busy is False


def test_added_message_has_exactly_the_documented_fields(h: Harness) -> None:
    call(h, ACTION_ADD, {"lcsc_id": "C2040"})
    assert set(h.sink.last(ACTION_ADDED)) == set(ADDED_FIELDS)


def test_add_without_warnings_reports_ok(h: Harness) -> None:
    call(h, ACTION_ADD, {"lcsc_id": "C2040"})
    assert h.sink.last(ACTION_ADDED)["warnings"] == []
    status = h.sink.of(ACTION_STATUS)[-1]
    assert status["level"] == "ok"
    assert "C2040" in status["text"]


def test_add_normalises_id(h: Harness) -> None:
    call(h, ACTION_ADD, {"lcsc_id": " c2040 "})
    assert h.service.add_calls[0][0] == "C2040"


def test_add_creates_output_directory_on_demand(h: Harness) -> None:
    assert not h.out_dir.exists()
    call(h, ACTION_ADD, {"lcsc_id": "C2040"})
    assert h.out_dir.is_dir()
    assert h.service.add_calls[0][1] == h.out_dir


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"lcsc_id": None},
        {"lcsc_id": 2040},
        {"lcsc_id": ""},
        {"lcsc_id": "2040"},
        {"lcsc_id": "C"},
        {"lcsc_id": "C20x"},
        {"lcsc_id": "C2040; rm"},
        {"lcsc_id": "C2040 C2041"},
        {"lcsc_id": "C" + "1" * 20},
        {"lcsc_id": "../C2040"},
    ],
)
def test_add_rejects_invalid_id(h: Harness, payload: dict[str, Any]) -> None:
    assert call(h, ACTION_ADD, payload) == "ERROR"
    assert h.sink.actions() == [ACTION_ERROR]
    assert (
        "LCSC" in h.sink.last(ACTION_ERROR)["message"]
        or "Enter" in h.sink.last(ACTION_ERROR)["message"]
    )
    assert h.service.add_calls == []
    assert not h.out_dir.exists()


# add with 3D ---------------------------------------------------------------------------------


def test_add_without_with_3d_means_no_3d(h: Harness) -> None:
    assert call(h, ACTION_ADD, {"lcsc_id": "C2040"}) == "OK"
    assert h.service.add_3d_flags == [False]
    added = h.sink.last(ACTION_ADDED)
    assert added["with_3d"] is False
    assert added["step_path"] is None
    assert added["placed_step_path"] is None
    first_status = h.sink.of(ACTION_STATUS)[0]["text"]
    assert first_status == "Downloading C2040 and building the library..."


def test_add_with_3d_false_is_accepted(h: Harness) -> None:
    assert call(h, ACTION_ADD, {"lcsc_id": "C2040", "with_3d": False}) == "OK"
    assert h.service.add_3d_flags == [False]
    assert h.sink.last(ACTION_ADDED)["with_3d"] is False


def test_add_with_3d_is_passed_to_the_service_and_reports_both_files(h: Harness) -> None:
    assert call(h, ACTION_ADD, {"lcsc_id": "C25804", "with_3d": True}) == "OK"
    assert h.service.add_3d_flags == [True]
    assert h.service.add_calls == [("C25804", h.out_dir)]
    added = h.sink.last(ACTION_ADDED)
    assert added["with_3d"] is True
    assert added["step_path"] == str(h.out_dir / "C25804.step")
    assert added["placed_step_path"] == str(h.out_dir / "C25804.placed.step")
    assert added["opened"] is False
    assert MANUAL_STEPS in added["instructions"]
    assert "3D model" in h.sink.of(ACTION_STATUS)[0]["text"]
    status = h.sink.of(ACTION_STATUS)[-1]
    assert status["level"] == "ok"
    assert "3D model" in status["text"]
    assert h.importer.opened == []  # 3D does not change the "never opens by itself" rule


def test_add_with_3d_keeps_warnings_and_reports_them(h: Harness) -> None:
    h.service.add_warnings = ("C25804: model sits 0.2 mm off the pads",)
    call(h, ACTION_ADD, {"lcsc_id": "C25804", "with_3d": True})
    added = h.sink.last(ACTION_ADDED)
    assert added["warnings"] == ["C25804: model sits 0.2 mm off the pads"]
    assert added["placed_step_path"] is not None
    assert h.sink.of(ACTION_STATUS)[-1]["level"] == "warn"


def test_add_with_3d_but_no_model_produced_warns_and_has_no_paths(h: Harness) -> None:
    h.service.produce_models = False
    h.service.add_warnings = ("C476261: the part has no 3D model on EasyEDA",)
    assert call(h, ACTION_ADD, {"lcsc_id": "C476261", "with_3d": True}) == "OK"
    added = h.sink.last(ACTION_ADDED)
    assert added["with_3d"] is True  # requested, so the palette can say it is missing
    assert added["step_path"] is None
    assert added["placed_step_path"] is None
    assert added["warnings"] == ["C476261: the part has no 3D model on EasyEDA"]
    assert h.sink.of(ACTION_ERROR) == []
    status = h.sink.of(ACTION_STATUS)[-1]
    assert status["level"] == "warn"
    assert "without a 3D model" in status["text"]
    assert (h.out_dir / "C476261.lbr").is_file()  # the library is still written


@pytest.mark.parametrize("value", ["true", "yes", "", 1, 0, 1.0, None, [], [True], {}, {"a": 1}])
def test_add_rejects_non_boolean_with_3d(h: Harness, value: Any) -> None:
    assert call(h, ACTION_ADD, {"lcsc_id": "C2040", "with_3d": value}) == "ERROR"
    assert h.sink.actions() == [ACTION_ERROR]
    assert "with_3d" in h.sink.last(ACTION_ERROR)["message"]
    assert h.service.add_calls == []
    assert not h.out_dir.exists()
    assert h.controller.busy is False


def test_invalid_id_is_reported_before_with_3d(h: Harness) -> None:
    assert call(h, ACTION_ADD, {"lcsc_id": "nope", "with_3d": "maybe"}) == "ERROR"
    assert "not a valid LCSC part number" in h.sink.last(ACTION_ERROR)["message"]


def test_open_folder_accepts_the_step_files_of_a_3d_add(h: Harness) -> None:
    call(h, ACTION_ADD, {"lcsc_id": "C25804", "with_3d": True})
    added = h.sink.last(ACTION_ADDED)
    for key in ("path", "step_path", "placed_step_path"):
        assert call(h, ACTION_OPEN_FOLDER, {"path": added[key]}) == "OK"
    assert h.opened_folders == [h.out_dir] * 3


def test_open_folder_refuses_a_step_file_outside_the_output_directory(
    h: Harness, tmp_path: Path
) -> None:
    h.out_dir.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    stray = outside / "C1.placed.step"
    stray.write_text("ISO-10303-21;", encoding="utf-8")
    assert call(h, ACTION_OPEN_FOLDER, {"path": str(stray)}) == "ERROR"
    assert "outside" in h.sink.last(ACTION_ERROR)["message"]
    assert h.opened_folders == []


def test_added_part_defaults_to_no_3d_files() -> None:
    part = AddedPart(path=Path("x.lbr"), mpn="")
    assert part.step_path is None
    assert part.placed_step_path is None


def test_second_add_with_3d_while_busy_is_refused(tmp_path: Path) -> None:
    deferred = DeferredRunner()
    harness = make_harness(tmp_path, runner=deferred)
    assert call(harness, ACTION_ADD, {"lcsc_id": "C25804", "with_3d": True}) == "OK"
    assert call(harness, ACTION_ADD, {"lcsc_id": "C2040", "with_3d": True}) == "BUSY"
    deferred.finish()
    assert harness.service.add_3d_flags == [True]
    assert harness.sink.last(ACTION_ADDED)["lcsc_id"] == "C25804"


# busy handling -------------------------------------------------------------------------------


def test_second_request_while_busy_is_refused_with_warning(tmp_path: Path) -> None:
    deferred = DeferredRunner()
    harness = make_harness(tmp_path, runner=deferred)
    assert call(harness, ACTION_SEARCH, {"query": "C2040"}) == "OK"
    assert harness.controller.busy is True
    harness.sink.raw.clear()

    assert call(harness, ACTION_ADD, {"lcsc_id": "C2040"}) == "BUSY"
    assert call(harness, ACTION_SEARCH, {"query": "other"}) == "BUSY"
    assert harness.sink.actions() == [ACTION_STATUS, ACTION_STATUS]
    assert all(m["level"] == "warn" for m in harness.sink.of(ACTION_STATUS))
    assert harness.service.add_calls == []
    assert len(deferred.pending) == 1

    deferred.finish()
    assert harness.controller.busy is False
    assert harness.sink.last(ACTION_BUSY) == {"value": False}
    assert call(harness, ACTION_ADD, {"lcsc_id": "C2040"}) == "OK"
    assert harness.controller.busy is True
    deferred.finish()
    assert len(harness.service.add_calls) == 1
    assert harness.controller.busy is False


def test_busy_is_cleared_when_the_runner_cannot_start(tmp_path: Path) -> None:
    class BrokenRunner:
        def run(self, fn: Callable[[], T], on_done: Callable[[JobResult[T]], None]) -> None:
            raise RuntimeError("cannot start thread")

    harness = make_harness(tmp_path, runner=BrokenRunner())
    assert call(harness, ACTION_SEARCH, {"query": "C2040"}) == "ERROR"
    assert harness.controller.busy is False
    assert harness.sink.last(ACTION_BUSY) == {"value": False}
    assert harness.sink.last(ACTION_ERROR)["message"] == GENERIC_ERROR_TEXT


# error mapping -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        PartNotFoundError("Part C1 has no EasyEDA symbol or footprint data."),
        RateLimitedError("EasyEDA blocked us, retry in 5 minutes."),
        NetworkError("Could not reach easyeda.com (timeout)."),
        ConversionError("Pad shape cannot be represented."),
    ],
)
def test_lcsc_errors_are_shown_verbatim(h: Harness, error: Exception) -> None:
    h.service.search_error = error
    h.service.add_error = error
    for action, payload in ((ACTION_SEARCH, {"query": "C1"}), (ACTION_ADD, {"lcsc_id": "C1"})):
        h.sink.raw.clear()
        assert call(h, action, payload) == "OK"
        assert h.sink.last(ACTION_ERROR) == {"message": str(error)}
        assert h.sink.last(ACTION_BUSY) == {"value": False}
        assert h.sink.of(ACTION_RESULTS) == []
        assert h.sink.of(ACTION_ADDED) == []
        assert h.controller.busy is False


def test_unexpected_exception_is_generic_and_logged_with_traceback(h: Harness) -> None:
    h.service.search_error = ZeroDivisionError("internal detail 1234")
    assert call(h, ACTION_SEARCH, {"query": "C1"}) == "OK"
    assert h.sink.last(ACTION_ERROR) == {"message": GENERIC_ERROR_TEXT}
    assert "internal detail" not in h.sink.last(ACTION_ERROR)["message"]
    logged = "\n".join(h.logs)
    assert "Traceback" in logged
    assert "ZeroDivisionError" in logged
    assert h.controller.busy is False


def test_file_system_error_is_reported(h: Harness) -> None:
    h.service.add_error = PermissionError("access denied")
    call(h, ACTION_ADD, {"lcsc_id": "C1"})
    assert h.sink.last(ACTION_ERROR)["message"].startswith("File system error")
    assert "access denied" in h.sink.last(ACTION_ERROR)["message"]


def test_unusable_output_directory_is_reported(tmp_path: Path) -> None:
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")
    harness = make_harness(tmp_path, out_dir=blocker)
    assert call(harness, ACTION_ADD, {"lcsc_id": "C1"}) == "OK"
    assert harness.sink.last(ACTION_ERROR)["message"].startswith("File system error")
    assert harness.service.add_calls == []
    assert harness.controller.busy is False


def test_failing_sink_during_completion_does_not_leave_ui_busy(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)

    class FlakySink(RecordingSink):
        def send(self, action: str, data: str) -> None:
            if action == ACTION_RESULTS:
                raise OSError("palette went away")
            super().send(action, data)

    flaky = FlakySink()
    harness.controller._sink = flaky  # type: ignore[attr-defined]
    assert call(harness, ACTION_SEARCH, {"query": "C1"}) == "OK"
    assert harness.controller.busy is False
    assert flaky.last(ACTION_BUSY) == {"value": False}
    assert "File system error" in flaky.last(ACTION_ERROR)["message"]


# malformed input -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "data"),
    [
        (ACTION_SEARCH, "not json"),
        (ACTION_SEARCH, "{"),
        (ACTION_SEARCH, "[]"),
        (ACTION_SEARCH, "null"),
        (ACTION_SEARCH, "123"),
        (ACTION_SEARCH, '"C2040"'),
        (ACTION_ADD, "not json"),
        (ACTION_ADD, "[1, 2]"),
        (ACTION_OPEN_FOLDER, "{}"),
        (ACTION_OPEN_FOLDER, '{"path": 5}'),
        (ACTION_OPEN_LIBRARY, "{}"),
        (ACTION_OPEN_LIBRARY, '{"path": 5}'),
        (ACTION_OPEN_LIBRARY, '{"path": "  "}'),
        (ACTION_OPEN_LIBRARY, "not json"),
        ("unknown_action", "{}"),
        ("", ""),
        ("SEARCH", '{"query": "C1"}'),
    ],
)
def test_malformed_requests_get_an_error_and_never_raise(
    h: Harness, action: str, data: str
) -> None:
    assert h.controller.handle(action, data) == "ERROR"
    assert h.sink.actions() == [ACTION_ERROR]
    assert h.sink.last(ACTION_ERROR)["message"]
    assert h.service.search_calls == []
    assert h.service.add_calls == []
    assert h.importer.opened == []
    assert h.controller.busy is False


# response acknowledgement -------------------------------------------------------------------


@pytest.mark.parametrize("data", ["OK", ""])
def test_fusion_response_acknowledgement_is_ignored_silently(h: Harness, data: str) -> None:
    assert h.controller.handle("response", data) == "OK"
    assert h.sink.raw == []  # no error message, so no endless acknowledgement ping pong
    assert h.logs == []
    assert h.service.search_calls == []
    assert h.service.add_calls == []


def test_fusion_response_with_a_failure_text_is_logged_but_not_shown(h: Harness) -> None:
    assert h.controller.handle("response", "ERROR") == "OK"
    assert h.sink.raw == []
    assert any("ERROR" in line for line in h.logs)


def test_every_message_the_controller_sends_may_be_acknowledged_without_new_traffic(
    h: Harness,
) -> None:
    call(h, ACTION_SEARCH, {"query": "C2040"})
    sent = len(h.sink.raw)
    assert sent > 0
    for _ in range(sent):
        h.controller.handle("response", "OK")  # Fusion acknowledges each one
    assert len(h.sink.raw) == sent


# open_folder ---------------------------------------------------------------------------------


def test_open_folder_opens_parent_of_a_saved_library(h: Harness) -> None:
    call(h, ACTION_ADD, {"lcsc_id": "C2040"})
    path = h.sink.last(ACTION_ADDED)["path"]
    assert call(h, ACTION_OPEN_FOLDER, {"path": path}) == "OK"
    assert h.opened_folders == [h.out_dir]


def test_open_folder_accepts_the_output_directory_itself(h: Harness) -> None:
    h.out_dir.mkdir()
    assert call(h, ACTION_OPEN_FOLDER, {"path": str(h.out_dir)}) == "OK"
    assert h.opened_folders == [h.out_dir]


def test_open_folder_refuses_paths_outside_the_output_directory(h: Harness, tmp_path: Path) -> None:
    h.out_dir.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert call(h, ACTION_OPEN_FOLDER, {"path": str(outside)}) == "ERROR"
    assert "outside" in h.sink.last(ACTION_ERROR)["message"]
    sneaky = h.out_dir / ".." / "elsewhere"
    assert call(h, ACTION_OPEN_FOLDER, {"path": str(sneaky)}) == "ERROR"
    assert h.opened_folders == []


def test_open_folder_reports_missing_folder(h: Harness) -> None:
    assert call(h, ACTION_OPEN_FOLDER, {"path": str(h.out_dir / "nope" / "x.lbr")}) == "ERROR"
    assert "does not exist" in h.sink.last(ACTION_ERROR)["message"]
    assert h.opened_folders == []


def test_open_folder_reports_opener_failure(tmp_path: Path) -> None:
    def failing_opener(path: Path) -> None:
        raise OSError("no file manager")

    harness = make_harness(tmp_path)
    harness.out_dir.mkdir()
    harness.controller._folder_opener = failing_opener  # type: ignore[attr-defined]
    assert call(harness, ACTION_OPEN_FOLDER, {"path": str(harness.out_dir)}) == "ERROR"
    assert "no file manager" in harness.sink.last(ACTION_ERROR)["message"]


# open_library --------------------------------------------------------------------------------


def saved_library(h: Harness) -> Path:
    call(h, ACTION_ADD, {"lcsc_id": "C2040"})
    h.sink.raw.clear()
    return h.out_dir / "C2040.lbr"


def test_open_library_asks_the_importer_and_reports_success(h: Harness) -> None:
    library = saved_library(h)
    assert call(h, ACTION_OPEN_LIBRARY, {"path": str(library)}) == "OK"
    assert h.importer.opened == [library]
    assert h.sink.actions() == [ACTION_STATUS]
    status = h.sink.last(ACTION_STATUS)
    assert status["level"] == "ok"
    assert "C2040.lbr" in status["text"]
    assert "File > Save" in status["text"]


def test_open_library_failure_warns_with_manual_steps(h: Harness) -> None:
    library = saved_library(h)
    h.importer.outcome = ImportOutcome(opened=False, message="command rejected")
    assert call(h, ACTION_OPEN_LIBRARY, {"path": str(library)}) == "OK"
    status = h.sink.last(ACTION_STATUS)
    assert status["level"] == "warn"
    assert "command rejected" in status["text"]
    assert MANUAL_STEPS in status["text"]
    assert h.sink.of(ACTION_ERROR) == []


def test_open_library_survives_importer_exception(h: Harness) -> None:
    library = saved_library(h)
    h.importer.error = RuntimeError("kaboom")
    assert call(h, ACTION_OPEN_LIBRARY, {"path": str(library)}) == "OK"
    status = h.sink.last(ACTION_STATUS)
    assert status["level"] == "warn"
    assert "kaboom" in status["text"]
    assert MANUAL_STEPS in status["text"]
    assert any("Traceback" in line and "kaboom" in line for line in h.logs)
    assert h.sink.of(ACTION_ERROR) == []


def test_open_library_works_while_a_job_is_running(tmp_path: Path) -> None:
    deferred = DeferredRunner()
    harness = make_harness(tmp_path, runner=deferred)
    harness.out_dir.mkdir()
    library = harness.out_dir / "C1.lbr"
    library.write_text("<eagle/>", encoding="utf-8")
    call(harness, ACTION_SEARCH, {"query": "C2040"})
    assert harness.controller.busy is True
    assert call(harness, ACTION_OPEN_LIBRARY, {"path": str(library)}) == "OK"
    assert harness.importer.opened == [library]


def test_open_library_refuses_files_outside_the_output_directory(
    h: Harness, tmp_path: Path
) -> None:
    h.out_dir.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    foreign = outside / "evil.lbr"
    foreign.write_text("<eagle/>", encoding="utf-8")
    assert call(h, ACTION_OPEN_LIBRARY, {"path": str(foreign)}) == "ERROR"
    assert "outside" in h.sink.last(ACTION_ERROR)["message"]
    sneaky = h.out_dir / ".." / "elsewhere" / "evil.lbr"
    assert call(h, ACTION_OPEN_LIBRARY, {"path": str(sneaky)}) == "ERROR"
    assert "outside" in h.sink.last(ACTION_ERROR)["message"]
    assert h.importer.opened == []


def test_open_library_rejects_missing_files_and_other_file_types(h: Harness) -> None:
    h.out_dir.mkdir()
    notes = h.out_dir / "notes.txt"
    notes.write_text("x", encoding="utf-8")
    for path in (h.out_dir / "missing.lbr", notes, h.out_dir):
        assert call(h, ACTION_OPEN_LIBRARY, {"path": str(path)}) == "ERROR"
        assert "does not exist" in h.sink.last(ACTION_ERROR)["message"]
    assert h.importer.opened == []


def test_add_never_calls_the_importer_even_when_it_would_fail(h: Harness) -> None:
    h.importer.error = RuntimeError("must not be called")
    assert call(h, ACTION_ADD, {"lcsc_id": "C2040"}) == "OK"
    assert h.importer.opened == []
    assert h.sink.of(ACTION_ERROR) == []


# misc ----------------------------------------------------------------------------------------


def test_default_output_directory_is_under_the_home_directory() -> None:
    assert controller.default_out_dir() == Path.home() / "LCSC2Fusion" / "libraries"
    ctrl = Controller(FakeService(), FakeImporter(), RecordingSink(), SyncRunner(), lambda _: None)
    assert ctrl.out_dir == controller.default_out_dir()


def test_instructions_name_the_button_and_the_manual_steps() -> None:
    text = controller.build_instructions()
    assert MANUAL_STEPS in text
    assert "Open in Fusion Electronics" in text
    assert "File > Save" in text


def test_describe_open_outcome() -> None:
    level, text = controller.describe_open_outcome(ImportOutcome(opened=True), "C1.lbr")
    assert (level, "C1.lbr" in text) == (controller.Level.OK, True)
    level, text = controller.describe_open_outcome(ImportOutcome(opened=False), "C1.lbr")
    assert level == controller.Level.WARN
    assert MANUAL_STEPS in text
    assert "()" not in text
    _, text = controller.describe_open_outcome(ImportOutcome(False, "boom"), "C1.lbr")
    assert "(boom)" in text


def test_job_result_unwrap() -> None:
    assert JobResult(value=3).unwrap() == 3
    with pytest.raises(ValueError, match="bad"):
        JobResult(error=ValueError("bad")).unwrap()


# core_service and import hygiene -------------------------------------------------------------


class FakeServiceModule(types.ModuleType):
    """Stands in for `lcsc2fusion.service`: records how the adapter calls it."""

    def __init__(self, models: tuple[ModelFiles, ...] | None = ()) -> None:
        super().__init__("lcsc2fusion.service")
        self.convert_calls: list[tuple[list[str], Path, dict[str, Any]]] = []
        self.search_calls: list[tuple[str, int, Any]] = []
        self.models = models  # None: the result has no `models` attribute at all
        self.warnings: tuple[str, ...] = ("C2040: something",)

    def convert_parts(self, ids: list[str], out_dir: Path, **kwargs: Any) -> Any:
        self.convert_calls.append((ids, out_dir, kwargs))
        part = SimpleNamespace(mpn="RP2040")
        result = SimpleNamespace(path=out_dir / "C2040.lbr", parts=(part,), warnings=self.warnings)
        if self.models is not None:
            result.models = self.models
        return result

    def search(self, query: str, limit: int, client: Any = None) -> list[SearchResult]:
        self.search_calls.append((query, limit, client))
        return [RP2040]


def install_fake_service(monkeypatch: pytest.MonkeyPatch, fake: FakeServiceModule) -> Any:
    """Make `from lcsc2fusion import service` resolve to `fake` (sys.modules alone is not enough
    when the real submodule is already attached to the package)."""
    import lcsc2fusion

    monkeypatch.setitem(sys.modules, "lcsc2fusion.service", fake)
    monkeypatch.setattr(lcsc2fusion, "service", fake, raising=False)
    import core_service

    return core_service


def model_files(tmp_path: Path, lcsc_id: str = "C2040", *warnings: str) -> ModelFiles:
    return ModelFiles(
        lcsc_id=lcsc_id,
        step_path=tmp_path / f"{lcsc_id}.step",
        placed_step_path=tmp_path / f"{lcsc_id}.placed.step",
        info_path=tmp_path / f"{lcsc_id}.3d.json",
        warnings=warnings,
    )


def test_core_service_without_3d_passes_with_3d_false_and_ignores_models(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakeServiceModule(models=None)  # result has no `models`: it must not be read
    core_service = install_fake_service(monkeypatch, fake)
    marker = object()
    service = core_service.CorePartService(client=marker)  # type: ignore[arg-type]

    part = service.add_part("C2040", tmp_path)

    assert part == AddedPart(
        path=tmp_path / "C2040.lbr", mpn="RP2040", warnings=("C2040: something",)
    )
    assert part.step_path is None
    assert part.placed_step_path is None
    assert fake.convert_calls == [(["C2040"], tmp_path, {"client": marker, "with_3d": False})]

    assert service.search("RP2040", 5) == [RP2040]
    assert fake.search_calls == [("RP2040", 5, marker)]


def test_core_service_with_3d_maps_the_matching_model_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    other = model_files(tmp_path, "C1")
    wanted = model_files(tmp_path, "C2040", "model sits 0.2 mm off", "C2040 STEP is 3 MB")
    second_match = model_files(tmp_path, "C2040", "must be ignored")
    fake = FakeServiceModule(models=(other, wanted, second_match))
    core_service = install_fake_service(monkeypatch, fake)
    marker = object()
    service = core_service.CorePartService(client=marker)  # type: ignore[arg-type]

    part = service.add_part("C2040", tmp_path, with_3d=True)

    assert fake.convert_calls == [(["C2040"], tmp_path, {"client": marker, "with_3d": True})]
    assert part.path == tmp_path / "C2040.lbr"
    assert part.mpn == "RP2040"
    assert part.step_path == tmp_path / "C2040.step"
    assert part.placed_step_path == tmp_path / "C2040.placed.step"
    assert part.warnings == (
        "C2040: something",
        "C2040: model sits 0.2 mm off",
        "C2040: C2040 STEP is 3 MB",
    )


def test_core_service_with_3d_does_not_repeat_a_warning_the_core_already_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakeServiceModule(models=(model_files(tmp_path, "C2040", "something"),))
    core_service = install_fake_service(monkeypatch, fake)
    part = core_service.CorePartService(client=object()).add_part("C2040", tmp_path, with_3d=True)
    assert part.warnings == ("C2040: something",)


def test_core_service_with_3d_and_no_model_leaves_the_paths_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakeServiceModule(models=(model_files(tmp_path, "C1"),))  # nothing for C2040
    fake.warnings = ("C2040: no 3D model on EasyEDA for this part",)
    core_service = install_fake_service(monkeypatch, fake)
    part = core_service.CorePartService(client=object()).add_part("C2040", tmp_path, with_3d=True)
    assert part.step_path is None
    assert part.placed_step_path is None
    assert part.warnings == ("C2040: no 3D model on EasyEDA for this part",)

    fake.models = ()
    part = core_service.CorePartService(client=object()).add_part("C2040", tmp_path, with_3d=True)
    assert part.placed_step_path is None


def test_core_service_matches_the_model_by_id_ignoring_case(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakeServiceModule(models=(model_files(tmp_path, "c2040"),))
    core_service = install_fake_service(monkeypatch, fake)
    part = core_service.CorePartService(client=object()).add_part("C2040", tmp_path, with_3d=True)
    assert part.placed_step_path == tmp_path / "c2040.placed.step"


def test_core_with_3d_contract_when_the_core_provides_it() -> None:
    """Checks the real core against the contract the add-in codes to; skipped until it lands."""
    service_module = pytest.importorskip("lcsc2fusion.service")
    parameter = inspect.signature(service_module.convert_parts).parameters.get("with_3d")
    if parameter is None:
        pytest.skip("core convert_parts has no with_3d yet")
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is False
    fields = {f.name for f in dataclasses.fields(service_module.ConversionResult)}
    assert "models" in fields


def test_controller_and_core_service_import_neither_adsk_nor_the_core_service_module() -> None:
    addin_dir = Path(controller.__file__).resolve().parent
    code = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(addin_dir)!r})
        import controller, core_service
        loaded = {{name for name in sys.modules if name == "adsk" or name.startswith("adsk.")}}
        assert not loaded, loaded
        assert "lcsc2fusion.service" not in sys.modules, "service must be imported lazily"
        assert "lcsc2fusion.client.easyeda" not in sys.modules
        print("clean")
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False, timeout=60
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "clean"
