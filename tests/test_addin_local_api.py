"""Tests of the local API for the MCP server: job tracker, sink tee, HTTP routes, discovery file.

The HTTP tests run the real `HttpTransport` on an ephemeral loopback port together with the real
`LocalApi`, `JobTracker` and `Controller` (with the fakes of the controller tests), so a request
travels exactly the way it does in Fusion, minus the main thread hop.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import controller as controller_module
import http_transport as ht
import local_api as la
import pytest
from controller import Controller
from fusion_library_flow import LIBRARY_NAME, FlowResult
from test_addin_controller import (
    FLOW_OK,
    DeferQueue,
    DeferredRunner,
    FakeImporter,
    FakeLibrary,
    FakeService,
    SyncRunner,
)
from test_addin_http_transport import (
    FakeClock,
    Response,
    assert_security_headers,
    raw_exchange,
    raw_status,
    request,
)

from lcsc2fusion.discovery import API_ENV_VAR, ApiEndpoint, default_api_path, read_api_file
from lcsc2fusion.errors import NetworkError

TOKEN = "t0ken-" + "x" * 40
JOB_FIELDS = {
    "job",
    "lcsc_id",
    "state",
    "ok",
    "status",
    "message",
    "steps",
    "added",
    "fusion",
}
ADDED_KEYS = {"path", "mpn", "warnings", "step_path", "placed_step_path", "with_3d"}
FUSION_KEYS = {
    "created_library",
    "merged",
    "already_present",
    "three_d",
    "library_name",
    "message",
    "warnings",
}


def emit(tracker: la.JobTracker, action: str, **doc: Any) -> None:
    tracker.observe(action, json.dumps(doc))


def added_message(lcsc_id: str = "C2040", **overrides: Any) -> dict[str, Any]:
    message: dict[str, Any] = {
        "lcsc_id": lcsc_id,
        "path": f"C:/libs/{lcsc_id}.lbr",
        "mpn": "RP2040",
        "warnings": ["a warning"],
        "opened": False,
        "instructions": "Press the button.",
        "step_path": None,
        "placed_step_path": None,
        "with_3d": False,
    }
    message.update(overrides)
    return message


def fusion_done_message(lcsc_id: str = "C2040", **overrides: Any) -> dict[str, Any]:
    message: dict[str, Any] = {
        "lcsc_id": lcsc_id,
        "ok": True,
        "created_library": True,
        "merged": False,
        "already_present": False,
        "three_d": "none",
        "library_name": LIBRARY_NAME,
        "message": "Created the library.",
        "warnings": [],
    }
    message.update(overrides)
    return message


def started(tracker: la.JobTracker, lcsc_id: str = "C2040", fusion: bool = True) -> int:
    return tracker.start(lcsc_id, False, fusion)


def snapshot(tracker: la.JobTracker, job: int) -> dict[str, Any]:
    document = tracker.snapshot(job)
    assert document is not None
    return document


# Job tracker: state machine ------------------------------------------------------------------


def test_the_job_document_has_exactly_the_documented_fields() -> None:
    tracker = la.JobTracker()
    job = started(tracker)
    document = snapshot(tracker, job)
    assert set(document) == JOB_FIELDS
    assert document == {
        "job": 1,
        "lcsc_id": "C2040",
        "state": "running",
        "ok": None,
        "status": "",
        "message": "",
        "steps": [],
        "added": None,
        "fusion": None,
    }
    emit(tracker, "added", **added_message())
    emit(tracker, "fusion_done", **fusion_done_message())
    done = snapshot(tracker, job)
    assert set(done["added"]) == ADDED_KEYS
    assert set(done["fusion"]) == FUSION_KEYS


def test_the_documented_keys_exist_in_the_controller_messages() -> None:
    assert set(controller_module.ADDED_FIELDS) >= ADDED_KEYS
    assert set(controller_module.FUSION_DONE_FIELDS) >= FUSION_KEYS


def test_a_fusion_job_succeeds_with_the_fusion_done_message() -> None:
    tracker = la.JobTracker()
    job = started(tracker)
    emit(tracker, "status", level="info", text="Downloading C2040...")
    emit(tracker, "busy", value=True)
    emit(tracker, "added", **added_message(with_3d=True, step_path="s.step"))
    mid = snapshot(tracker, job)
    assert (mid["state"], mid["ok"], mid["status"]) == ("running", None, "Downloading C2040...")
    assert mid["added"] == {
        "path": "C:/libs/C2040.lbr",
        "mpn": "RP2040",
        "warnings": ["a warning"],
        "step_path": "s.step",
        "placed_step_path": None,
        "with_3d": True,
    }
    emit(tracker, "status", level="info", text="Sending C2040 to Fusion...")
    emit(tracker, "fusion_done", **fusion_done_message(created_library=True, three_d="started"))

    final = snapshot(tracker, job)
    assert (final["state"], final["ok"]) == ("done", True)
    assert final["message"] == "Created the library."
    assert final["steps"] == ["Downloading C2040...", "Sending C2040 to Fusion..."]
    assert final["status"] == "Sending C2040 to Fusion..."
    assert final["fusion"] == {
        "created_library": True,
        "merged": False,
        "already_present": False,
        "three_d": "started",
        "library_name": LIBRARY_NAME,
        "message": "Created the library.",
        "warnings": [],
    }
    assert not tracker.has_active()


def test_a_fusion_job_fails_when_fusion_done_says_not_ok() -> None:
    tracker = la.JobTracker()
    job = started(tracker)
    emit(tracker, "added", **added_message())
    emit(tracker, "fusion_done", **fusion_done_message(ok=False, message="Fusion refused."))

    final = snapshot(tracker, job)
    assert (final["state"], final["ok"], final["message"]) == ("failed", False, "Fusion refused.")
    assert final["added"] is not None  # the library file was written, only the hand-off failed
    assert final["fusion"]["message"] == "Fusion refused."
    assert not tracker.has_active()


def test_a_job_without_the_fusion_hand_off_is_done_when_the_part_is_saved() -> None:
    tracker = la.JobTracker()
    job = started(tracker, fusion=False)
    emit(tracker, "added", **added_message())

    final = snapshot(tracker, job)
    assert (final["state"], final["ok"]) == ("done", True)
    assert final["message"] == "Saved C2040 (RP2040) to C:/libs/C2040.lbr"
    assert final["fusion"] is None
    assert not tracker.has_active()
    emit(tracker, "status", level="ok", text="Saved C2040 to C:/libs/C2040.lbr")  # after the end
    assert snapshot(tracker, job)["steps"] == []


def test_a_job_that_asked_for_3d_and_got_none_says_so() -> None:
    tracker = la.JobTracker()
    job = tracker.start("C2040", True, False)
    emit(tracker, "added", **added_message(with_3d=True))
    assert "without a 3D model" in snapshot(tracker, job)["message"]

    other = tracker.start("C2040", True, False)
    emit(tracker, "added", **added_message(with_3d=True, placed_step_path="p.step"))
    assert "3D" not in snapshot(tracker, other)["message"]


def test_an_error_message_fails_the_job() -> None:
    tracker = la.JobTracker()
    job = started(tracker)
    emit(tracker, "status", level="info", text="Downloading C2040...")
    emit(tracker, "error", message="EasyEDA does not know C2040.")

    final = snapshot(tracker, job)
    assert (final["state"], final["ok"]) == ("failed", False)
    assert final["message"] == "EasyEDA does not know C2040."
    assert final["status"] == "EasyEDA does not know C2040."
    assert final["steps"][-1] == "EasyEDA does not know C2040."
    assert final["added"] is None
    assert not tracker.has_active()


def test_a_job_the_controller_refused_can_be_cancelled_and_its_id_is_not_reused() -> None:
    tracker = la.JobTracker()
    first = started(tracker)
    emit(tracker, "status", level="warn", text="Another request is still running.")
    tracker.cancel(first)
    assert tracker.snapshot(first) is None
    assert not tracker.has_active()
    assert started(tracker) == first + 1
    tracker.cancel(999)  # unknown: nothing happens


def test_messages_without_an_active_job_are_ignored() -> None:
    tracker = la.JobTracker()
    emit(tracker, "status", level="info", text="the user searched")
    emit(tracker, "added", **added_message())
    emit(tracker, "error", message="x")
    job = started(tracker, fusion=False)
    assert snapshot(tracker, job)["steps"] == []  # nothing leaked into the job started later


def test_messages_for_another_part_do_not_finish_the_job() -> None:
    tracker = la.JobTracker()
    job = started(tracker, "C2040")
    emit(tracker, "added", **added_message("C25804"))
    emit(tracker, "fusion_done", **fusion_done_message("C25804"))
    document = snapshot(tracker, job)
    assert (document["state"], document["added"], document["fusion"]) == ("running", None, None)


@pytest.mark.parametrize("action", ["results", "busy", "ready", "response", "mystery"])
def test_other_actions_are_ignored(action: str) -> None:
    tracker = la.JobTracker()
    job = started(tracker)
    emit(tracker, action, value=True, items=[], text="x")
    document = snapshot(tracker, job)
    assert (document["state"], document["steps"]) == ("running", [])


@pytest.mark.parametrize(
    "data",
    ["", "not json", "[]", "null", "5", '"text"', "{"],
)
def test_malformed_messages_never_raise_and_never_change_the_job(data: str) -> None:
    logs: list[str] = []
    tracker = la.JobTracker(logs.append)
    job = started(tracker)
    for action in ("status", "added", "fusion_done", "error"):
        tracker.observe(action, data)
    document = snapshot(tracker, job)
    assert (document["state"], document["steps"]) == ("running", [])
    assert len(logs) == 4
    assert all("malformed" in line for line in logs)


def test_fields_of_the_wrong_type_are_tolerated() -> None:
    tracker = la.JobTracker()
    job = started(tracker)
    emit(tracker, "status", text=5)
    emit(tracker, "added", lcsc_id="C2040", path=None, mpn=3, warnings="nope", with_3d="yes")
    emit(tracker, "fusion_done", lcsc_id="C2040", ok="true", warnings=[1, "kept"], three_d=None)
    final = snapshot(tracker, job)
    assert final["steps"] == []
    assert final["added"] == {
        "path": "",
        "mpn": "",
        "warnings": [],
        "step_path": None,
        "placed_step_path": None,
        "with_3d": False,
    }
    assert final["fusion"]["warnings"] == ["kept"]
    assert (final["state"], final["ok"]) == ("failed", False)  # only a real true counts


def test_a_failing_logger_does_not_break_observe() -> None:
    def broken(message: str) -> None:
        raise RuntimeError("log is gone")

    tracker = la.JobTracker(broken)
    started(tracker)
    tracker.observe("status", "not json")  # must not raise


def test_a_second_job_cannot_start_while_one_runs() -> None:
    tracker = la.JobTracker()
    started(tracker)
    with pytest.raises(la.JobRunningError):
        started(tracker)


def test_only_the_last_20_jobs_are_kept() -> None:
    tracker = la.JobTracker()
    for _ in range(25):
        job = started(tracker, fusion=False)
        emit(tracker, "added", **added_message())
        assert tracker.snapshot(job) is not None
    assert [n for n in range(1, 26) if tracker.snapshot(n) is not None] == list(range(6, 26))


def test_a_running_job_survives_the_trim() -> None:
    tracker = la.JobTracker()
    for _ in range(la.MAX_JOBS):
        started(tracker, fusion=False)
        emit(tracker, "added", **added_message())
    running = started(tracker)
    assert tracker.snapshot(1) is None
    assert snapshot(tracker, running)["state"] == "running"


def test_only_the_newest_50_steps_are_kept() -> None:
    tracker = la.JobTracker()
    job = started(tracker)
    for index in range(80):
        emit(tracker, "status", level="info", text=f"step {index}")
    document = snapshot(tracker, job)
    assert len(document["steps"]) == la.MAX_STEPS
    assert document["steps"][0] == "step 30"
    assert document["steps"][-1] == document["status"] == "step 79"


def test_very_long_texts_are_clipped() -> None:
    tracker = la.JobTracker()
    job = started(tracker)
    emit(tracker, "status", level="info", text="x" * 50000)
    assert len(snapshot(tracker, job)["status"]) == la.MAX_TEXT_CHARS


def test_a_snapshot_is_a_copy() -> None:
    tracker = la.JobTracker()
    job = started(tracker)
    emit(tracker, "added", **added_message())
    document = snapshot(tracker, job)
    document["steps"].append("tampered")
    document["added"]["warnings"].append("tampered")
    document["state"] = "failed"
    again = snapshot(tracker, job)
    assert again["steps"] == []
    assert again["added"]["warnings"] == ["a warning"]
    assert again["state"] == "running"


def test_an_idle_controller_fails_an_old_running_job() -> None:
    clock = FakeClock()
    tracker = la.JobTracker(clock=clock)
    job = started(tracker)
    clock.advance(59)
    assert tracker.fail_orphan(60.0, "lost") is False
    clock.advance(2)
    assert tracker.fail_orphan(60.0, "lost") is True
    final = snapshot(tracker, job)
    assert (final["state"], final["ok"], final["message"]) == ("failed", False, "lost")
    assert not tracker.has_active()
    assert tracker.fail_orphan(60.0, "lost") is False  # nothing left


def test_concurrent_observe_and_snapshot_never_show_a_torn_job() -> None:
    tracker = la.JobTracker()
    stop = threading.Event()
    problems: list[str] = []

    def check(document: dict[str, Any]) -> None:
        expected = {"running": None, "done": True, "failed": False}[document["state"]]
        if document["ok"] is not expected:
            problems.append(f"state {document['state']} with ok {document['ok']}")
        if document["state"] == "running" and document["message"]:
            problems.append("a running job has a message")
        if document["state"] != "running" and not document["message"]:
            problems.append("a finished job has no message")
        if len(document["steps"]) > la.MAX_STEPS:
            problems.append("too many steps")
        if document["state"] == "done" and (document["added"] is None):
            problems.append("done without the added document")

    def reader() -> None:
        while not stop.is_set():
            for number in range(1, 30):
                document = tracker.snapshot(number)
                if document is not None:
                    check(document)

    readers = [threading.Thread(target=reader, daemon=True) for _ in range(4)]
    for thread in readers:
        thread.start()
    try:
        for index in range(25):
            job = started(tracker, "C2040", fusion=bool(index % 2))
            for step in range(70):
                emit(tracker, "status", level="info", text=f"step {step}")
            emit(tracker, "added", **added_message())
            if index % 2:
                emit(tracker, "fusion_done", **fusion_done_message(ok=bool(index % 3)))
            assert snapshot(tracker, job)["state"] != "running"
    finally:
        stop.set()
        for thread in readers:
            thread.join(5)
    assert problems == []


# Sink tee ------------------------------------------------------------------------------------


class RecordingSink:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.error: Exception | None = None

    def send(self, action: str, data: str) -> None:
        self.events.append(f"first:{action}")
        if self.error is not None:
            raise self.error


class SpyTracker(la.JobTracker):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    def observe(self, action: str, data: str) -> None:
        self.events.append(f"tracker:{action}")
        super().observe(action, data)


def test_the_tee_forwards_to_the_window_first_and_then_to_the_tracker() -> None:
    events: list[str] = []
    tee = la.TeeSink(RecordingSink(events), SpyTracker(events))
    tee.send("status", "{}")
    assert events == ["first:status", "tracker:status"]


def test_the_tracker_still_sees_the_message_when_the_window_sink_fails() -> None:
    events: list[str] = []
    first = RecordingSink(events)
    first.error = RuntimeError("window sink failed")
    tee = la.TeeSink(first, SpyTracker(events))
    with pytest.raises(RuntimeError, match="window sink failed"):
        tee.send("status", "{}")
    assert events == ["first:status", "tracker:status"]


# LocalApi: validation and dispatch -----------------------------------------------------------


@dataclass
class ApiParts:
    api: la.LocalApi
    tracker: la.JobTracker
    dispatched: list[tuple[str, str]] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    reply: str = "OK"
    busy: bool = False
    error: Exception | None = None
    during: Callable[[], None] | None = None
    clock: FakeClock = field(default_factory=FakeClock)


def make_api_parts() -> ApiParts:
    clock = FakeClock()
    tracker = la.JobTracker(clock=clock)
    holder: list[ApiParts] = []

    def dispatch(action: str, data: str) -> str:
        parts = holder[0]
        parts.dispatched.append((action, data))
        if parts.during is not None:
            parts.during()
        if parts.error is not None:
            raise parts.error
        return parts.reply

    logs: list[str] = []
    api = la.LocalApi(dispatch, tracker, TOKEN, lambda: holder[0].busy, logs.append)
    parts = ApiParts(api, tracker, logs=logs, clock=clock)
    holder.append(parts)
    return parts


def test_ping_reports_the_protocol_version_and_whether_a_job_runs() -> None:
    parts = make_api_parts()
    assert parts.api.ping() == (200, {"ok": True, "version": 1, "busy": False})
    parts.busy = True  # the window works
    assert parts.api.ping()[1]["busy"] is True
    parts.busy = False
    parts.api.submit({"lcsc_id": "C2040"})
    assert parts.api.ping()[1]["busy"] is True  # our own job
    assert parts.api.token == TOKEN


def test_a_short_token_is_refused() -> None:
    with pytest.raises(ValueError, match="token"):
        la.LocalApi(lambda a, d: "OK", la.JobTracker(), "short", lambda: False, print)


def test_a_valid_request_starts_a_job_and_dispatches_add() -> None:
    parts = make_api_parts()
    status, body = parts.api.submit({"lcsc_id": " c2040 ", "with_3d": True, "fusion": False})
    assert (status, body) == (202, {"job": 1})
    ((action, data),) = parts.dispatched
    assert action == "add"
    assert json.loads(data) == {"lcsc_id": "C2040", "with_3d": True, "fusion": False}
    assert snapshot(parts.tracker, 1)["state"] == "running"
    assert any("job 1" in line for line in parts.logs)


def test_defaults_are_no_3d_and_the_fusion_hand_off() -> None:
    parts = make_api_parts()
    parts.api.submit({"lcsc_id": "C2040", "unknown": [1], "other": None})
    assert json.loads(parts.dispatched[0][1]) == {
        "lcsc_id": "C2040",
        "with_3d": False,
        "fusion": True,
    }


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"lcsc_id": None},
        {"lcsc_id": 2040},
        {"lcsc_id": ""},
        {"lcsc_id": "C"},
        {"lcsc_id": "2040"},
        {"lcsc_id": "X2040"},
        {"lcsc_id": "C20 40"},
        {"lcsc_id": "C20\n40"},
        {"lcsc_id": "C" + "1" * 13},
        {"lcsc_id": "C2040", "with_3d": "yes"},
        {"lcsc_id": "C2040", "with_3d": 1},
        {"lcsc_id": "C2040", "with_3d": None},
        {"lcsc_id": "C2040", "fusion": "true"},
        {"lcsc_id": "C2040", "fusion": 0},
    ],
)
def test_invalid_requests_are_refused_without_a_job(body: dict[str, Any]) -> None:
    parts = make_api_parts()
    status, payload = parts.api.submit(body)
    assert status == 400
    assert payload["error"] == "bad_request"
    assert payload["message"]
    assert parts.dispatched == []
    assert not parts.tracker.has_active()


def test_the_longest_id_is_accepted() -> None:
    parts = make_api_parts()
    assert parts.api.submit({"lcsc_id": "C" + "9" * 12})[0] == 202


def test_a_busy_controller_gets_409_and_nothing_is_dispatched() -> None:
    parts = make_api_parts()
    parts.busy = True
    status, payload = parts.api.submit({"lcsc_id": "C2040"})
    assert status == 409
    assert payload["error"] == "busy"
    assert parts.dispatched == []
    assert parts.tracker.snapshot(1) is None


def test_a_running_job_gets_409_for_the_second_request() -> None:
    parts = make_api_parts()
    assert parts.api.submit({"lcsc_id": "C2040"})[0] == 202
    parts.busy = True
    assert parts.api.submit({"lcsc_id": "C25804"})[0] == 409
    assert len(parts.dispatched) == 1


def test_the_controller_saying_busy_cancels_the_job() -> None:
    parts = make_api_parts()
    parts.reply = "BUSY"  # it became busy between our check and the dispatch
    status, payload = parts.api.submit({"lcsc_id": "C2040"})
    assert (status, payload["error"]) == (409, "busy")
    assert parts.tracker.snapshot(1) is None
    assert not parts.tracker.has_active()
    parts.reply = "OK"
    assert parts.api.submit({"lcsc_id": "C2040"}) == (202, {"job": 2})


def test_the_controller_refusing_with_an_error_returns_its_message() -> None:
    parts = make_api_parts()
    parts.reply = "ERROR"
    parts.during = lambda: emit(parts.tracker, "error", message="That part cannot be used.")
    status, payload = parts.api.submit({"lcsc_id": "C2040"})
    assert (status, payload) == (
        400,
        {"error": "bad_request", "message": "That part cannot be used."},
    )
    assert parts.tracker.snapshot(1) is None


def test_an_error_reply_without_a_message_is_generic() -> None:
    parts = make_api_parts()
    parts.reply = "ERROR"
    status, payload = parts.api.submit({"lcsc_id": "C2040"})
    assert (status, payload["error"]) == (400, "bad_request")
    assert payload["message"]


@pytest.mark.parametrize(
    "error", [TimeoutError("Fusion did not process 'add' within 30 s"), RuntimeError("stopping")]
)
def test_a_dispatch_that_does_not_arrive_gives_503_and_forgets_the_job(error: Exception) -> None:
    parts = make_api_parts()
    parts.error = error
    status, payload = parts.api.submit({"lcsc_id": "C2040"})
    assert (status, payload["error"]) == (503, "unavailable")
    assert str(error) in payload["message"]
    assert parts.tracker.snapshot(1) is None
    assert not parts.tracker.has_active()
    assert not parts.api.ping()[1]["busy"]


def test_an_unexpected_dispatch_failure_gives_500_and_forgets_the_job() -> None:
    parts = make_api_parts()
    parts.error = ValueError("bug")
    status, payload = parts.api.submit({"lcsc_id": "C2040"})
    assert (status, payload["error"]) == (500, "internal")
    assert "bug" not in payload["message"]
    assert any("bug" in line for line in parts.logs)  # the details go to the add-in log
    assert not parts.tracker.has_active()


def test_unknown_jobs_give_404() -> None:
    parts = make_api_parts()
    status, payload = parts.api.job(7)
    assert (status, payload["error"]) == (404, "unknown_job")
    parts.api.submit({"lcsc_id": "C2040"})
    assert parts.api.job(1)[0] == 200


def test_a_job_that_lost_its_controller_does_not_block_the_api_forever() -> None:
    parts = make_api_parts()
    parts.api.submit({"lcsc_id": "C2040"})
    assert parts.api.submit({"lcsc_id": "C2040"})[0] == 409  # the job is young
    parts.clock.advance(la.ORPHAN_AFTER_S + 1)  # and the controller is idle (busy is False)
    assert parts.api.submit({"lcsc_id": "C25804"}) == (202, {"job": 2})
    assert snapshot(parts.tracker, 1)["state"] == "failed"


def test_an_old_job_of_a_busy_controller_is_not_declared_lost() -> None:
    parts = make_api_parts()
    parts.api.submit({"lcsc_id": "C2040"})
    parts.clock.advance(la.ORPHAN_AFTER_S * 100)
    parts.busy = True  # Fusion is still working (a slow library flow)
    assert parts.api.submit({"lcsc_id": "C25804"})[0] == 409
    assert snapshot(parts.tracker, 1)["state"] == "running"


def test_two_requests_at_the_same_time_start_one_job() -> None:
    parts = make_api_parts()
    release = threading.Event()
    entered = threading.Event()

    def slow_dispatch() -> None:
        entered.set()
        assert release.wait(10)

    parts.during = slow_dispatch
    results: list[tuple[int, dict[str, Any]]] = []
    first = threading.Thread(target=lambda: results.append(parts.api.submit({"lcsc_id": "C2040"})))
    first.start()
    assert entered.wait(5)
    assert parts.api.submit({"lcsc_id": "C25804"})[0] == 409  # the first one is still dispatching
    release.set()
    first.join(5)
    assert results == [(202, {"job": 1})]
    assert len(parts.dispatched) == 1


# The HTTP layer, real transport, real controller ---------------------------------------------


@dataclass
class ApiRig:
    transport: ht.HttpTransport
    hub: ht.Hub
    tracker: la.JobTracker
    controller: Controller
    service: FakeService
    library: FakeLibrary
    deferred: DeferQueue
    runner: Any
    clock: FakeClock
    logs: list[str]
    dispatched: list[tuple[str, str]]

    @property
    def port(self) -> int:
        return int(self.transport.url.rsplit(":", 1)[1])

    def call(
        self,
        method: str,
        path: str,
        *,
        auth: str | None = f"Bearer {TOKEN}",
        payload: Any = None,
        raw: bytes | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> Response:
        sent = dict(headers or {})
        if auth is not None:
            sent["Authorization"] = auth
        body = raw
        if body is None and payload is not None:
            body = json.dumps(payload).encode("utf-8")
        if body is not None:
            sent.setdefault("Content-Type", "application/json")
            sent.setdefault("X-LCSC", "1")
        return request(self.port, method, path, headers=sent, body=body, **kwargs)

    def get(self, path: str, **kwargs: Any) -> Response:
        return self.call("GET", path, **kwargs)

    def add(self, payload: Any, **kwargs: Any) -> Response:
        return self.call("POST", "/api/local/add", payload=payload, **kwargs)

    def job(self, number: int) -> dict[str, Any]:
        response = self.get(f"/api/local/job/{number}")
        assert response.status == 200
        document = response.json()
        assert isinstance(document, dict)
        return document

    def login(self) -> str:
        launch = self.transport.launch_url()
        response = request(self.port, "GET", launch.removeprefix(self.transport.url))
        cookie = response.header("set-cookie")
        assert response.status == 302 and cookie is not None
        return cookie.split(";")[0]


@pytest.fixture
def make_rig(tmp_path: Path) -> Iterator[Callable[..., ApiRig]]:
    rigs: list[ApiRig] = []

    def factory(
        *,
        runner: Any = None,
        hook: bool = True,
        dispatch: Callable[[str, str], str] | None = None,
        token: str = TOKEN,
    ) -> ApiRig:
        static = tmp_path / f"static{len(rigs)}"
        static.mkdir()
        (static / "index.html").write_text("<html></html>", encoding="utf-8")
        logs: list[str] = []
        dispatched: list[tuple[str, str]] = []
        hub = ht.Hub()
        tracker = la.JobTracker(logs.append)
        service = FakeService()
        library = FakeLibrary()
        deferred = DeferQueue()
        the_runner = runner if runner is not None else DeferredRunner()
        ctrl = Controller(
            service,
            FakeImporter(),
            la.TeeSink(ht.HubSink(hub), tracker),
            the_runner,
            logs.append,
            out_dir=tmp_path / "libs",
            folder_opener=lambda path: None,
            library=library,
            defer=deferred,
        )

        def default_dispatch(action: str, data: str) -> str:
            dispatched.append((action, data))
            return ctrl.handle(action, data)

        api = la.LocalApi(
            dispatch or default_dispatch, tracker, token, lambda: ctrl.busy, logs.append
        )
        clock = FakeClock()
        transport = ht.HttpTransport(
            hub,
            default_dispatch,
            logs.append,
            static,
            clock=clock,
            heartbeat_s=0.2,
            connection_timeout_s=2.0,
            local_api=api if hook else None,
        )
        transport.start()
        rig = ApiRig(
            transport,
            hub,
            tracker,
            ctrl,
            service,
            library,
            deferred,
            the_runner,
            clock,
            logs,
            dispatched,
        )
        rigs.append(rig)
        return rig

    yield factory
    for rig in rigs:
        rig.transport.stop()


@pytest.fixture
def api(make_rig: Callable[..., ApiRig]) -> ApiRig:
    return make_rig()


def test_ping_with_the_token_answers_json_with_the_security_headers(api: ApiRig) -> None:
    response = api.get("/api/local/ping")
    assert response.status == 200
    assert response.header("content-type") == "application/json; charset=utf-8"
    assert response.json() == {"ok": True, "version": 1, "busy": False}
    assert_security_headers(response)


def test_add_with_the_fusion_hand_off_end_to_end(api: ApiRig) -> None:
    response = api.add({"lcsc_id": "c2040", "with_3d": True})
    assert (response.status, response.json()) == (202, {"job": 1})
    assert_security_headers(response)

    running = api.job(1)
    assert set(running) == JOB_FIELDS
    assert (running["state"], running["ok"]) == ("running", None)
    assert running["steps"] == ["Downloading C2040 and building the library and the 3D model..."]
    assert api.get("/api/local/ping").json()["busy"] is True

    api.runner.finish()  # the worker thread returns: the part is saved
    saved = api.job(1)
    assert saved["state"] == "running"  # the Fusion hand-off is still to come
    assert set(saved["added"]) == ADDED_KEYS
    assert saved["added"]["path"] == str(api.controller.out_dir / "C2040.lbr")
    assert saved["added"]["mpn"] == "RP2040"
    assert saved["added"]["with_3d"] is True
    assert saved["added"]["step_path"].endswith("C2040.step")
    assert saved["added"]["placed_step_path"].endswith("C2040.placed.step")
    assert saved["fusion"] is None
    assert api.get("/api/local/ping").json()["busy"] is True

    api.deferred.run_all()  # Fusion's main thread runs the library flow
    final = api.job(1)
    assert (final["state"], final["ok"]) == ("done", True)
    assert final["message"] == FLOW_OK.message
    assert set(final["fusion"]) == FUSION_KEYS
    assert final["fusion"]["created_library"] is True
    assert final["fusion"]["library_name"] == LIBRARY_NAME
    assert "Sending C2040 to Fusion..." in final["steps"]
    assert FLOW_OK.message in final["steps"]
    assert api.library.requests[0].lcsc_id == "C2040"
    assert api.get("/api/local/ping").json()["busy"] is False
    assert api.add({"lcsc_id": "C25804"}).json() == {"job": 2}  # free again


def test_add_without_the_fusion_hand_off(api: ApiRig) -> None:
    assert api.add({"lcsc_id": "C2040", "fusion": False}).status == 202
    api.runner.finish()
    final = api.job(1)
    assert (final["state"], final["ok"]) == ("done", True)
    assert final["fusion"] is None
    assert final["added"]["with_3d"] is False
    assert api.library.requests == []
    assert api.service.add_3d_flags == [False]


def test_a_synchronous_job_is_final_on_the_first_poll(make_rig: Callable[..., ApiRig]) -> None:
    api = make_rig(runner=SyncRunner())
    assert api.add({"lcsc_id": "C2040", "fusion": False}).json() == {"job": 1}
    assert api.job(1)["state"] == "done"


def test_the_fusion_hand_off_failing_fails_the_job(api: ApiRig) -> None:
    api.library.result = FlowResult(
        ok=False,
        created_library=False,
        merged=False,
        already_present=False,
        three_d="none",
        library_name=LIBRARY_NAME,
        message="Fusion could not save the library.",
    )
    api.add({"lcsc_id": "C2040"})
    api.runner.finish()
    api.deferred.run_all()
    final = api.job(1)
    assert (final["state"], final["ok"]) == ("failed", False)
    assert final["message"] == "Fusion could not save the library."
    assert final["added"] is not None


def test_a_part_that_cannot_be_downloaded_fails_the_job_with_the_library_message(
    api: ApiRig,
) -> None:
    api.service.add_error = NetworkError("EasyEDA cannot be reached.")
    api.add({"lcsc_id": "C2040"})
    api.runner.finish()
    final = api.job(1)
    assert (final["state"], final["ok"]) == ("failed", False)
    assert final["message"] == "EasyEDA cannot be reached."
    assert final["added"] is None
    assert api.get("/api/local/ping").json()["busy"] is False


def test_the_fusion_hand_off_being_unavailable_fails_the_job(
    make_rig: Callable[..., ApiRig],
) -> None:
    api = make_rig()
    api.controller._library = None  # an add-in session without the accumulating library
    api.add({"lcsc_id": "C2040"})
    api.runner.finish()
    final = api.job(1)
    assert (final["state"], final["ok"]) == ("failed", False)
    assert final["added"] is not None  # the library file exists, only the hand-off is missing


def test_the_window_sees_every_message_the_job_tracker_sees(api: ApiRig) -> None:
    subscription = api.hub.subscribe()
    api.add({"lcsc_id": "C2040"})
    api.runner.finish()
    api.deferred.run_all()
    actions: list[str] = []
    while (event := subscription.get(0.05)) is not None:
        actions.append(event.action)
    subscription.close()
    assert "added" in actions
    assert "fusion_done" in actions
    assert actions.index("added") < actions.index("fusion_done")
    assert api.job(1)["state"] == "done"


def test_a_job_started_in_the_window_makes_the_api_busy(api: ApiRig) -> None:
    assert api.controller.handle("add", json.dumps({"lcsc_id": "C25804"})) == "OK"
    assert api.get("/api/local/ping").json()["busy"] is True

    response = api.add({"lcsc_id": "C2040"})

    assert response.status == 409
    assert response.json()["error"] == "busy"
    assert api.tracker.snapshot(1) is None
    api.runner.finish()  # the window's job ends: it did not touch any tracker job
    assert api.add({"lcsc_id": "C2040"}).json() == {"job": 1}


def test_a_second_request_while_a_job_runs_gets_409(api: ApiRig) -> None:
    assert api.add({"lcsc_id": "C2040"}).status == 202
    response = api.add({"lcsc_id": "C25804"})
    assert response.status == 409
    assert response.json()["error"] == "busy"
    assert api.service.add_calls == []  # the first job has not even run yet
    api.runner.finish()
    assert [call[0] for call in api.service.add_calls] == ["C2040"]  # and the second never will


def test_messages_from_the_window_do_not_change_a_finished_job(api: ApiRig) -> None:
    api.add({"lcsc_id": "C2040", "fusion": False})
    api.runner.finish()
    before = api.job(1)
    api.controller.handle("ready", "{}")
    api.controller.handle("search", json.dumps({"query": "resistor"}))
    api.runner.finish()
    assert api.job(1) == before


def test_a_dispatch_that_times_out_gives_503(make_rig: Callable[..., ApiRig]) -> None:
    def timeout(action: str, data: str) -> str:
        raise TimeoutError("Fusion did not process 'add' within 30 s (is a dialog open?).")

    api = make_rig(dispatch=timeout)
    response = api.add({"lcsc_id": "C2040"})
    assert response.status == 503
    assert response.json()["error"] == "unavailable"
    assert "dialog" in response.json()["message"]
    assert api.get("/api/local/job/1").status == 404
    assert api.get("/api/local/ping").json()["busy"] is False


def test_unknown_jobs_give_404_json(api: ApiRig) -> None:
    response = api.get("/api/local/job/1")
    assert response.status == 404
    assert response.json()["error"] == "unknown_job"
    assert api.get("/api/local/job/0").status == 404


def test_the_job_route_serves_old_jobs_but_only_the_last_20(api: ApiRig) -> None:
    for _ in range(22):
        assert api.add({"lcsc_id": "C2040", "fusion": False}).status == 202
        api.runner.finish()
    assert api.get("/api/local/job/2").status == 404
    assert api.get("/api/local/job/3").status == 200
    assert api.get("/api/local/job/22").status == 200


# The HTTP layer: authentication --------------------------------------------------------------

LOCAL_PATHS = [
    ("GET", "/api/local/ping"),
    ("POST", "/api/local/add"),
    ("GET", "/api/local/job/1"),
    ("GET", "/api/local/nothing"),
]


@pytest.mark.parametrize(("method", "path"), LOCAL_PATHS)
def test_no_token_gives_401_and_nothing_is_dispatched(api: ApiRig, method: str, path: str) -> None:
    response = api.call(method, path, auth=None, payload={"lcsc_id": "C2040"})
    assert response.status == 401
    assert response.header("www-authenticate") == "Bearer"
    assert response.header("content-type") == "text/plain; charset=utf-8"
    assert b"C2040" not in response.body
    assert_security_headers(response)
    assert api.dispatched == []
    assert api.tracker.snapshot(1) is None


@pytest.mark.parametrize(
    "auth",
    [
        "Bearer wrong",
        "Bearer ",
        "Bearer",
        "Basic " + TOKEN,
        TOKEN,
        "Bearer " + TOKEN + "x",
        "Bearer " + TOKEN[:-1],
        "Bearer " + "A" * 600,
        "Bearer \xe9" + TOKEN,
        "Token " + TOKEN,
    ],
)
def test_a_wrong_token_gives_401(api: ApiRig, auth: str) -> None:
    assert api.get("/api/local/ping", auth=auth).status == 401
    assert api.add({"lcsc_id": "C2040"}, auth=auth).status == 401
    assert api.dispatched == []


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER"])
def test_the_scheme_is_case_insensitive(api: ApiRig, scheme: str) -> None:
    assert api.get("/api/local/ping", auth=f"{scheme} {TOKEN}").status == 200


def test_two_authorization_headers_are_refused(api: ApiRig) -> None:
    head = (
        f"GET /api/local/ping HTTP/1.1\r\nHost: 127.0.0.1:{api.port}\r\n"
        f"Authorization: Bearer {TOKEN}\r\nAuthorization: Bearer {TOKEN}\r\n\r\n"
    )
    assert raw_status(raw_exchange(api.port, head.encode("ascii"))) == 401


def test_a_session_cookie_does_not_open_the_local_api(api: ApiRig) -> None:
    cookie = api.login()
    for method, path in LOCAL_PATHS[:3]:
        response = api.call(method, path, auth=None, cookie=cookie, payload={"lcsc_id": "C2040"})
        assert response.status == 401
    assert api.dispatched == []
    # the cookie plus the token works, and the cookie plays no part in it
    assert api.get("/api/local/ping", cookie=cookie).status == 200
    assert api.get("/api/local/ping", cookie="lcsc_s=garbage").status == 200


@pytest.mark.parametrize("origin", ["http://evil.example", "null", "", "http://127.0.0.1:1"])
def test_any_origin_header_is_refused_even_with_the_token(api: ApiRig, origin: str) -> None:
    assert api.get("/api/local/ping", origin=origin).status == 403
    assert api.add({"lcsc_id": "C2040"}, origin=origin).status == 403
    own = f"http://127.0.0.1:{api.port}"
    assert api.get("/api/local/ping", origin=own).status == 403  # not even our own origin
    assert api.dispatched == []


def test_the_origin_is_checked_before_the_token(api: ApiRig) -> None:
    assert api.get("/api/local/ping", auth=None, origin="http://evil.example").status == 403


@pytest.mark.parametrize("host", ["localhost:1", "evil.example", "127.0.0.1", None])
def test_a_wrong_host_is_refused_even_with_the_token(api: ApiRig, host: str | None) -> None:
    assert api.get("/api/local/ping", host=host).status == 403
    assert api.add({"lcsc_id": "C2040"}, host=host).status == 403
    assert api.dispatched == []


def test_without_the_hook_the_local_routes_do_not_exist(make_rig: Callable[..., ApiRig]) -> None:
    api = make_rig(hook=False)
    for method, path in LOCAL_PATHS:
        assert api.call(method, path, payload={"lcsc_id": "C2040"}).status == 404
        assert api.call(method, path, auth=None, payload={"lcsc_id": "C2040"}).status == 404


def test_a_hook_with_a_short_token_is_refused_at_construction(tmp_path: Path) -> None:
    class Hook:
        token = "short"

        def ping(self) -> tuple[int, dict[str, Any]]:
            return 200, {}

        def submit(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            return 200, {}

        def job(self, job_id: int) -> tuple[int, dict[str, Any]]:
            return 200, {}

    with pytest.raises(ValueError, match="token"):
        ht.HttpTransport(ht.Hub(), lambda a, d: "OK", print, tmp_path, local_api=Hook())


def test_failed_logins_are_logged_at_most_20_times_a_minute(api: ApiRig) -> None:
    def refusals() -> int:
        return sum("refused 401" in line for line in api.logs)

    for _ in range(30):
        assert api.get("/api/local/ping", auth="Bearer wrong").status == 401
    assert refusals() == 20
    api.clock.advance(30)
    assert api.get("/api/local/ping", auth="Bearer wrong").status == 401
    assert refusals() == 20
    api.clock.advance(31)  # the first burst is more than a minute old now
    assert api.get("/api/local/ping", auth="Bearer wrong").status == 401
    assert refusals() == 21
    assert api.get("/api/local/ping").status == 200  # and there is no lockout


def test_the_token_and_the_tried_tokens_never_reach_the_log(api: ApiRig) -> None:
    api.get("/api/local/ping", auth="Bearer guess-12345")
    api.add({"lcsc_id": "C2040"})
    api.runner.finish()
    api.deferred.run_all()
    log = "\n".join(api.logs)
    assert TOKEN not in log
    assert "guess-12345" not in log
    assert "Bearer" not in log


# The HTTP layer: methods, paths, bodies ------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "allow"),
    [
        ("POST", "/api/local/ping", "GET"),
        ("PUT", "/api/local/ping", "GET"),
        ("HEAD", "/api/local/ping", "GET"),
        ("DELETE", "/api/local/job/1", "GET"),
        ("POST", "/api/local/job/1", "GET"),
        ("GET", "/api/local/add", "POST"),
        ("PUT", "/api/local/add", "POST"),
        ("DELETE", "/api/local/add", "POST"),
    ],
)
def test_a_wrong_method_gives_405_with_the_allowed_one(
    api: ApiRig, method: str, path: str, allow: str
) -> None:
    response = api.call(method, path)
    assert response.status == 405
    assert response.header("allow") == allow
    assert api.dispatched == []


@pytest.mark.parametrize(
    "path",
    [
        "/api/local/",
        "/api/local/nothing",
        "/api/local/ping/",
        "/api/local/ping/x",
        "/api/local/job",
        "/api/local/job/",
        "/api/local/job/abc",
        "/api/local/job/-1",
        "/api/local/job/1.5",
        "/api/local/job/1/2",
        "/api/local/job/1234567890",
        "/api/local/job/%D9%A3",
        "/api/local/../act",
    ],
)
def test_unknown_local_paths_give_404_after_the_token_check(api: ApiRig, path: str) -> None:
    assert api.get(path).status == 404
    assert api.get(path, auth=None).status == 401  # nobody learns which routes exist


def test_the_path_without_the_trailing_slash_is_not_a_local_route(api: ApiRig) -> None:
    assert api.get("/api/local").status == 403  # the cookie routes answer, the token is no use


def test_a_job_number_in_non_ascii_digits_is_not_a_job(api: ApiRig) -> None:
    head = (
        "GET /api/local/job/\u0663 HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{api.port}\r\nAuthorization: Bearer {TOKEN}\r\n\r\n"
    )
    assert raw_status(raw_exchange(api.port, head.encode("utf-8"))) == 404


def test_a_nine_digit_job_number_is_accepted(api: ApiRig) -> None:
    assert api.get("/api/local/job/999999999").status == 404
    assert api.get("/api/local/job/999999999").json()["error"] == "unknown_job"


def test_a_query_string_is_ignored(api: ApiRig) -> None:
    assert api.get("/api/local/ping?x=1&y=2").status == 200


def test_post_needs_the_x_lcsc_header(api: ApiRig) -> None:
    response = api.call(
        "POST", "/api/local/add", payload={"lcsc_id": "C2040"}, headers={"X-LCSC": "0"}
    )
    assert response.status == 403
    raw = json.dumps({"lcsc_id": "C2040"}).encode("utf-8")
    sent = {"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"}
    assert request(api.port, "POST", "/api/local/add", headers=sent, body=raw).status == 403
    assert api.dispatched == []


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded", ""])
def test_post_needs_the_json_content_type(api: ApiRig, content_type: str) -> None:
    response = api.call(
        "POST",
        "/api/local/add",
        payload={"lcsc_id": "C2040"},
        headers={"Content-Type": content_type},
    )
    assert response.status == 415
    assert api.dispatched == []


@pytest.mark.parametrize(
    "body",
    [
        b"{",
        b"[]",
        b'"C2040"',
        b"null",
        b"5",
        b"\xff\xfe",
        b"",
        pytest.param(b"[" * 60000, id="deeply-nested"),
    ],
)
def test_a_body_that_is_not_a_json_object_gives_400(api: ApiRig, body: bytes) -> None:
    response = api.call("POST", "/api/local/add", raw=body)
    assert response.status == 400
    assert response.json()["error"] == "bad_request"
    assert api.dispatched == []


def test_invalid_fields_give_400_json_through_http(api: ApiRig) -> None:
    for payload in ({"lcsc_id": "nope"}, {"lcsc_id": "C2040", "fusion": "yes"}, {}):
        response = api.add(payload)
        assert response.status == 400
        assert response.json()["error"] == "bad_request"
    assert api.dispatched == []


def test_an_oversized_body_gives_413(api: ApiRig) -> None:
    response = api.call("POST", "/api/local/add", raw=b"x" * (ht.MAX_BODY_BYTES + 1))
    assert response.status == 413
    assert api.dispatched == []


def test_a_post_without_content_length_gives_411(api: ApiRig) -> None:
    head = (
        f"POST /api/local/add HTTP/1.1\r\nHost: 127.0.0.1:{api.port}\r\n"
        f"Authorization: Bearer {TOKEN}\r\nContent-Type: application/json\r\nX-LCSC: 1\r\n\r\n"
    )
    assert raw_status(raw_exchange(api.port, head.encode("ascii"), half_close=True)) == 411


def test_unknown_keys_in_the_request_are_ignored(api: ApiRig) -> None:
    response = api.add({"lcsc_id": "C2040", "extra": {"a": 1}, "fusion": False})
    assert (response.status, response.json()) == (202, {"job": 1})
    assert json.loads(api.dispatched[0][1]) == {
        "lcsc_id": "C2040",
        "with_3d": False,
        "fusion": False,
    }


def test_an_api_that_raises_gives_500_json_without_details(make_rig: Callable[..., ApiRig]) -> None:
    api = make_rig()
    assert api.transport.local_api is not None

    def explode(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        raise RuntimeError("secret internals")

    api.transport.local_api.submit = explode  # type: ignore[method-assign]
    response = api.add({"lcsc_id": "C2040"})
    assert response.status == 500
    assert response.json()["error"] == "internal"
    assert b"secret internals" not in response.body
    assert any("secret internals" in line for line in api.logs)
    assert api.get("/api/local/ping").status == 200  # the server carries on


# Regression: the window's routes are unchanged -----------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/"),
        ("GET", "/index.html"),
        ("GET", "/api/ping"),
        ("GET", "/api/events"),
        ("POST", "/api/act"),
    ],
)
def test_the_bearer_token_does_not_open_the_window_routes(
    api: ApiRig, method: str, path: str
) -> None:
    payload = {"action": "ready", "data": "{}"} if method == "POST" else None
    response = api.call(method, path, payload=payload)
    assert response.status == 403
    assert api.dispatched == []


def test_the_window_routes_still_work_with_the_cookie_alone(api: ApiRig) -> None:
    cookie = api.login()
    ping = request(api.port, "GET", "/api/ping", cookie=cookie)
    assert ping.status == 200
    assert ping.json()["ok"] is True
    body = json.dumps({"action": "ready", "data": "{}"}).encode("utf-8")
    act = request(
        api.port,
        "POST",
        "/api/act",
        headers={"Content-Type": "application/json", "X-LCSC": "1"},
        body=body,
        cookie=cookie,
        origin=f"http://127.0.0.1:{api.port}",
    )
    assert (act.status, act.json()) == (200, {"reply": "OK"})
    assert [call[0] for call in api.dispatched] == ["ready"]
    # a bearer token next to the cookie neither helps nor hurts
    assert (
        request(
            api.port,
            "GET",
            "/api/ping",
            cookie=cookie,
            headers={"Authorization": f"Bearer {TOKEN}"},
        ).status
        == 200
    )


def test_the_window_can_still_start_jobs_and_the_api_sees_nothing_of_them(api: ApiRig) -> None:
    cookie = api.login()
    data = json.dumps({"lcsc_id": "C25804", "fusion": True})
    body = json.dumps({"action": "add", "data": data}).encode()
    act = request(
        api.port,
        "POST",
        "/api/act",
        headers={"Content-Type": "application/json", "X-LCSC": "1"},
        body=body,
        cookie=cookie,
    )
    assert act.json() == {"reply": "OK"}
    api.runner.finish()
    assert api.tracker.snapshot(1) is None
    assert api.get("/api/local/ping").json()["busy"] is True  # the hand-off to Fusion is queued


# Discovery file ------------------------------------------------------------------------------


def test_publish_writes_the_endpoint_and_withdraw_removes_it(tmp_path: Path) -> None:
    path = tmp_path / "state" / "api.json"
    logs: list[str] = []
    publisher = la.DiscoveryPublisher(TOKEN, logs.append, path, pid=4321)

    assert publisher.publish("http://127.0.0.1:50000") is True

    assert read_api_file(path) == ApiEndpoint("http://127.0.0.1:50000", TOKEN, 4321)
    assert any(str(path) in line for line in logs)
    assert TOKEN not in "\n".join(logs)
    publisher.withdraw()
    assert not path.exists()
    publisher.withdraw()  # twice is fine


def test_the_default_path_follows_the_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "custom.json"
    monkeypatch.setenv(API_ENV_VAR, str(path))
    publisher = la.DiscoveryPublisher(TOKEN, lambda line: None)
    assert default_api_path() == path
    assert publisher.publish("http://127.0.0.1:50001") is True
    assert read_api_file(path).pid == os.getpid()
    publisher.withdraw()
    assert not path.exists()


def test_withdraw_leaves_a_file_of_a_newer_add_in_alone(tmp_path: Path) -> None:
    path = tmp_path / "api.json"
    older = la.DiscoveryPublisher("older-" + "o" * 20, lambda line: None, path)
    newer = la.DiscoveryPublisher("newer-" + "n" * 20, lambda line: None, path)
    older.publish("http://127.0.0.1:1")
    newer.publish("http://127.0.0.1:2")
    older.withdraw()
    assert read_api_file(path).url == "http://127.0.0.1:2"
    newer.withdraw()
    assert not path.exists()


def test_a_file_that_cannot_be_written_is_only_logged(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a directory", encoding="utf-8")
    logs: list[str] = []
    publisher = la.DiscoveryPublisher(TOKEN, logs.append, blocker / "api.json")

    assert publisher.publish("http://127.0.0.1:50000") is False

    assert len(logs) == 1
    assert logs[0].startswith("MCP discovery file not written")
    assert TOKEN not in logs[0]
    publisher.withdraw()  # nothing was written: nothing to remove, no error


def test_a_failing_logger_does_not_break_publishing(tmp_path: Path) -> None:
    def broken(message: str) -> None:
        raise RuntimeError("log is gone")

    publisher = la.DiscoveryPublisher(TOKEN, broken, tmp_path / "api.json")
    assert publisher.publish("http://127.0.0.1:50000") is True
    publisher.withdraw()


def test_the_published_file_works_against_a_live_transport(
    make_rig: Callable[..., ApiRig], tmp_path: Path
) -> None:
    api = make_rig()
    path = tmp_path / "found" / "api.json"
    la.DiscoveryPublisher(TOKEN, lambda line: None, path).publish(api.transport.url)

    endpoint = read_api_file(path)  # what the MCP server does

    assert endpoint.url == api.transport.url
    port = int(endpoint.url.rsplit(":", 1)[1])
    response = request(
        port, "GET", "/api/local/ping", headers={"Authorization": f"Bearer {endpoint.token}"}
    )
    assert response.json() == {"ok": True, "version": 1, "busy": False}


def test_the_api_answers_quickly_while_a_slow_dispatch_blocks_another_request(
    make_rig: Callable[..., ApiRig],
) -> None:
    release = threading.Event()
    entered = threading.Event()

    def slow(action: str, data: str) -> str:
        entered.set()
        assert release.wait(10)
        return "OK"

    api = make_rig(dispatch=slow)
    answer: list[Response] = []
    worker = threading.Thread(target=lambda: answer.append(api.add({"lcsc_id": "C2040"})))
    worker.start()
    assert entered.wait(5)
    started_at = time.monotonic()
    ping = api.get("/api/local/ping")  # served by another thread of the server
    assert ping.status == 200
    assert ping.json()["busy"] is True  # the job exists, the main thread has not taken it yet
    assert time.monotonic() - started_at < 2.0
    release.set()
    worker.join(5)
    assert answer[0].status == 202
