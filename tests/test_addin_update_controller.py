"""The update notice inside the controller: protocol, background check, no interference."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypeVar

import controller
import pytest
from controller import (
    ACTION_BUSY,
    ACTION_CHECK_UPDATE,
    ACTION_DISMISS_UPDATE,
    ACTION_ERROR,
    ACTION_READY,
    ACTION_SEARCH,
    ACTION_SET_UPDATE_CHECK,
    ACTION_STATUS,
    ACTION_UPDATE,
    Controller,
    JobResult,
)
from update_check import STATE_AVAILABLE, CheckResult, FetchResponse, UpdateInfo
from update_service import FLOOD_LIMIT_S, UpdateService, UpdateStore, UpdateView

from lcsc2fusion.model import SearchResult

T = TypeVar("T")

T0 = 1_760_000_000.0
DAY = 24 * 3600.0
CURRENT = "0.1.0"
RELEASE_020 = "https://github.com/KAJPER/lcsc2fusion/releases/tag/v0.2.0"
INSTALLER_020 = (
    "https://github.com/KAJPER/lcsc2fusion/releases/download/v0.2.0/LCSC2Fusion-Setup-0.2.0.exe"
)
BAD_TAG_TEXT = "Invalid request: tag must be a release tag such as v1.2.3."


# Fakes ------------------------------------------------------------------------------------------


class Sink:
    def __init__(self) -> None:
        self.raw: list[tuple[str, str]] = []
        self.fail = False

    def send(self, action: str, data: str) -> None:
        if self.fail:
            raise RuntimeError("the window is gone")
        self.raw.append((action, data))

    def of(self, action: str) -> list[dict[str, Any]]:
        return [json.loads(data) for name, data in self.raw if name == action]

    def updates(self) -> list[dict[str, Any]]:
        return self.of(ACTION_UPDATE)

    def states(self) -> list[str]:
        return [message["state"] for message in self.updates()]

    def actions(self) -> list[str]:
        return [name for name, _ in self.raw]


class SyncRunner:
    def run(self, fn: Callable[[], T], on_done: Callable[[JobResult[T]], None]) -> None:
        try:
            result: JobResult[T] = JobResult(value=fn())
        except Exception as exc:
            result = JobResult(error=exc)
        on_done(result)


class HeldRunner:
    """Keeps every job until `finish()`, like worker threads that have not completed yet."""

    def __init__(self) -> None:
        self.pending: list[Callable[[], None]] = []
        self.started = 0

    def run(self, fn: Callable[[], T], on_done: Callable[[JobResult[T]], None]) -> None:
        self.started += 1

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


class BrokenRunner:
    def run(self, fn: Callable[[], T], on_done: Callable[[JobResult[T]], None]) -> None:
        raise RuntimeError("cannot start a thread")


class Service:
    """Stands in for the search/add side: one canned answer, no files."""

    def search(self, query: str, limit: int) -> list[SearchResult]:
        return [SearchResult("C2040", "RP2040", "Raspberry Pi", "LQFN-56", "MCU")]

    def add_part(self, lcsc_id: str, out_dir: Path, with_3d: bool = False) -> Any:
        raise AssertionError("not used here")


class Importer:
    def open_library(self, path: Path) -> Any:
        raise AssertionError("not used here")


class Clock:
    def __init__(self) -> None:
        self.value = T0

    def __call__(self) -> float:
        return self.value


class Github:
    """The fake network: counts requests, answers with a prepared release."""

    def __init__(self) -> None:
        self.calls = 0
        self.tag = "v0.2.0"
        self.error: Exception | None = None
        self.assets: list[dict[str, Any]] = [{"name": "LCSC2Fusion-Setup-0.2.0.exe"}]

    def __call__(self, url: str, headers: Any, timeout: float, max_bytes: int) -> FetchResponse:
        self.calls += 1
        if self.error is not None:
            raise self.error
        document = {
            "tag_name": self.tag,
            "draft": False,
            "prerelease": False,
            "assets": self.assets,
        }
        return FetchResponse(200, json.dumps(document).encode("utf-8"))


def make(
    tmp_path: Path, *, runner: Any = None, with_service: bool = True, current: str = CURRENT
) -> SimpleNamespace:
    sink = Sink()
    clock = Clock()
    github = Github()
    logs: list[str] = []
    update_path = tmp_path / "update.json"
    updates = (
        UpdateService(
            UpdateStore(update_path),
            current_version=current,
            fetch=github,
            now=clock,
            log=logs.append,
        )
        if with_service
        else None
    )
    ctrl = Controller(
        Service(),
        Importer(),
        sink,
        runner if runner is not None else SyncRunner(),
        logs.append,
        out_dir=tmp_path / "libs",
        update_service=updates,
    )
    return SimpleNamespace(
        controller=ctrl,
        sink=sink,
        clock=clock,
        github=github,
        logs=logs,
        updates=updates,
        update_path=update_path,
    )


def call(h: SimpleNamespace, action: str, payload: Any = None) -> str:
    data = payload if isinstance(payload, str) else json.dumps(payload if payload else {})
    return str(h.controller.handle(action, data))


def saved(h: SimpleNamespace) -> dict[str, Any]:
    loaded = json.loads(h.update_path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


# ready ------------------------------------------------------------------------------------------


def test_without_a_service_ready_says_nothing_about_updates(tmp_path: Path) -> None:
    h = make(tmp_path, with_service=False)
    assert call(h, ACTION_READY) == "OK"
    assert h.sink.updates() == []


def test_ready_sends_the_cached_state_then_checks_in_the_background(tmp_path: Path) -> None:
    runner = HeldRunner()
    h = make(tmp_path, runner=runner)
    assert call(h, ACTION_READY) == "OK"
    assert h.github.calls == 0  # the worker has not run yet: the page was answered first
    assert h.sink.states() == ["current", "checking"]
    first = h.sink.updates()[0]
    assert (first["latest"], first["checked_at"], first["enabled"]) == (None, None, True)
    runner.finish()
    assert h.github.calls == 1
    assert h.sink.states() == ["current", "checking", "available"]
    result = h.sink.updates()[-1]
    assert result == {
        "state": "available",
        "current": CURRENT,
        "latest": "0.2.0",
        "release_url": RELEASE_020,
        "installer_url": INSTALLER_020,
        "checked_at": T0,
        "enabled": True,
        "dismissed": False,
    }
    assert tuple(result) == controller.UPDATE_FIELDS


def test_ready_with_a_check_that_is_not_due_only_sends_the_cache(tmp_path: Path) -> None:
    h = make(tmp_path)
    call(h, ACTION_READY)
    assert h.github.calls == 1
    h.clock.value += 3600
    h.sink.raw.clear()
    call(h, ACTION_READY)  # the page reconnected
    assert h.github.calls == 1
    assert h.sink.states() == ["available"]  # the banner comes back from the cache


def test_ready_checks_again_after_a_day(tmp_path: Path) -> None:
    h = make(tmp_path)
    call(h, ACTION_READY)
    h.clock.value += DAY
    call(h, ACTION_READY)
    assert h.github.calls == 2


def test_ready_during_a_running_check_does_not_start_another(tmp_path: Path) -> None:
    runner = HeldRunner()
    h = make(tmp_path, runner=runner)
    call(h, ACTION_READY)
    call(h, ACTION_READY)
    assert runner.started == 1
    assert h.sink.states() == ["current", "checking", "checking"]
    runner.finish()
    assert h.github.calls == 1
    assert h.sink.states()[-1] == "available"


def test_ready_with_the_check_switched_off_sends_disabled_and_never_connects(
    tmp_path: Path,
) -> None:
    h = make(tmp_path)
    call(h, ACTION_SET_UPDATE_CHECK, {"enabled": False})
    h.sink.raw.clear()
    call(h, ACTION_READY)
    assert h.sink.states() == ["disabled"]
    assert h.sink.updates()[0]["enabled"] is False
    assert h.github.calls == 0


def test_a_failed_automatic_check_is_silent_apart_from_the_update_message(
    tmp_path: Path,
) -> None:
    h = make(tmp_path)
    h.github.error = OSError("offline")
    assert call(h, ACTION_READY) == "OK"
    assert h.sink.states() == ["current", "checking", "error"]
    assert ACTION_ERROR not in h.sink.actions()
    statuses = [m["text"] for m in h.sink.of(ACTION_STATUS)]
    assert not any("update" in text.lower() for text in statuses)
    assert any("Update check failed: network" in line for line in h.logs)
    assert h.sink.of(ACTION_BUSY) == [{"value": False}]  # only the one from `ready` itself


def test_a_failed_check_backs_off_for_six_hours(tmp_path: Path) -> None:
    h = make(tmp_path)
    h.github.error = OSError("offline")
    call(h, ACTION_READY)
    h.github.error = None
    h.clock.value += 6 * 3600 - 1
    call(h, ACTION_READY)
    assert h.github.calls == 1
    h.clock.value += 1
    call(h, ACTION_READY)
    assert h.github.calls == 2
    assert h.sink.states()[-1] == "available"


# The check next to searches and adds ------------------------------------------------------------


def test_the_check_never_sets_busy(tmp_path: Path) -> None:
    runner = HeldRunner()
    h = make(tmp_path, runner=runner)
    call(h, ACTION_READY)
    assert h.controller.busy is False
    assert [m["value"] for m in h.sink.of(ACTION_BUSY)] == [False]
    runner.finish()
    assert h.controller.busy is False
    assert [m["value"] for m in h.sink.of(ACTION_BUSY)] == [False]


def test_a_search_works_while_the_check_is_still_running(tmp_path: Path) -> None:
    runner = HeldRunner()
    h = make(tmp_path, runner=runner)
    call(h, ACTION_READY)
    assert call(h, ACTION_SEARCH, {"query": "C2040"}) == "OK"  # not BUSY
    assert h.controller.busy is True
    runner.finish()
    assert h.controller.busy is False
    assert h.sink.of("results")[0]["query"] == "C2040"
    assert h.sink.states()[-1] == "available"
    assert ACTION_ERROR not in h.sink.actions()


def test_a_check_can_start_while_a_search_is_running(tmp_path: Path) -> None:
    runner = HeldRunner()
    h = make(tmp_path, runner=runner)
    assert call(h, ACTION_SEARCH, {"query": "C2040"}) == "OK"
    assert h.controller.busy is True
    assert call(h, ACTION_CHECK_UPDATE, {"force": True}) == "OK"  # not BUSY
    assert h.sink.states() == ["checking"]
    runner.finish()
    assert h.controller.busy is False
    assert h.sink.states() == ["checking", "available"]
    assert h.sink.of("results")


def test_a_search_is_not_delayed_by_the_check_in_the_busy_flag_of_the_page(
    tmp_path: Path,
) -> None:
    runner = HeldRunner()
    h = make(tmp_path, runner=runner)
    call(h, ACTION_READY)
    call(h, ACTION_SEARCH, {"query": "C2040"})
    busy_values = [m["value"] for m in h.sink.of(ACTION_BUSY)]
    assert busy_values == [False, True]  # the search alone made the page busy
    runner.finish()
    assert [m["value"] for m in h.sink.of(ACTION_BUSY)] == [False, True, False]


def test_a_second_search_is_still_refused_while_the_first_runs_but_not_for_the_check(
    tmp_path: Path,
) -> None:
    runner = HeldRunner()
    h = make(tmp_path, runner=runner)
    call(h, ACTION_SEARCH, {"query": "C2040"})
    assert call(h, ACTION_SEARCH, {"query": "C25804"}) == "BUSY"
    assert call(h, ACTION_CHECK_UPDATE, {"force": True}) == "OK"


# check_update -----------------------------------------------------------------------------------


def test_a_forced_check_runs_even_when_none_is_due(tmp_path: Path) -> None:
    h = make(tmp_path)
    call(h, ACTION_READY)
    h.clock.value += FLOOD_LIMIT_S
    h.sink.raw.clear()
    assert call(h, ACTION_CHECK_UPDATE, {"force": True}) == "OK"
    assert h.github.calls == 2
    assert h.sink.states() == ["checking", "available"]


def test_an_unforced_check_that_is_not_due_answers_once_from_the_cache(tmp_path: Path) -> None:
    h = make(tmp_path)
    call(h, ACTION_READY)
    h.sink.raw.clear()
    assert call(h, ACTION_CHECK_UPDATE, {}) == "OK"
    assert h.github.calls == 1
    assert h.sink.states() == ["available"]


def test_two_quick_forced_checks_ask_github_once(tmp_path: Path) -> None:
    h = make(tmp_path)
    call(h, ACTION_CHECK_UPDATE, {"force": True})
    h.clock.value += FLOOD_LIMIT_S - 1
    h.sink.raw.clear()
    assert call(h, ACTION_CHECK_UPDATE, {"force": True}) == "OK"
    assert h.github.calls == 1
    assert h.sink.states() == ["available"]  # one answer, from the cache
    h.clock.value += 1
    call(h, ACTION_CHECK_UPDATE, {"force": True})
    assert h.github.calls == 2


def test_a_forced_check_while_one_runs_answers_checking_and_starts_nothing(
    tmp_path: Path,
) -> None:
    runner = HeldRunner()
    h = make(tmp_path, runner=runner)
    call(h, ACTION_CHECK_UPDATE, {"force": True})
    h.sink.raw.clear()
    call(h, ACTION_CHECK_UPDATE, {"force": True})
    assert runner.started == 1
    assert h.sink.states() == ["checking"]


def test_a_manual_check_reports_up_to_date(tmp_path: Path) -> None:
    h = make(tmp_path)
    h.github.tag = "v0.1.0"
    call(h, ACTION_CHECK_UPDATE, {"force": True})
    assert h.sink.states() == ["checking", "current"]
    assert h.sink.updates()[-1]["latest"] == "0.1.0"
    assert h.sink.updates()[-1]["release_url"] is None


def test_a_manual_check_reports_a_failure_as_error(tmp_path: Path) -> None:
    h = make(tmp_path)
    h.github.error = TimeoutError("slow")
    call(h, ACTION_CHECK_UPDATE, {"force": True})
    assert h.sink.states() == ["checking", "error"]
    assert ACTION_ERROR not in h.sink.actions()


def test_a_release_without_an_installer_has_no_installer_url(tmp_path: Path) -> None:
    h = make(tmp_path)
    h.github.assets = []
    call(h, ACTION_CHECK_UPDATE, {"force": True})
    last = h.sink.updates()[-1]
    assert (last["state"], last["installer_url"], last["release_url"]) == (
        "available",
        None,
        RELEASE_020,
    )


def test_a_hostile_answer_cannot_put_text_or_urls_into_the_message(tmp_path: Path) -> None:
    h = make(tmp_path)
    h.github.tag = "v0.2.0"
    h.github.assets = [
        {
            "name": "LCSC2Fusion-Setup-0.2.0.exe",
            "browser_download_url": "https://evil.example/x.exe",
        }
    ]
    call(h, ACTION_CHECK_UPDATE, {"force": True})
    assert "evil" not in " ".join(data for _, data in h.sink.raw)


@pytest.mark.parametrize("force", ["true", 1, 0, None, [], {}])
def test_force_must_be_a_real_boolean(tmp_path: Path, force: Any) -> None:
    h = make(tmp_path)
    assert call(h, ACTION_CHECK_UPDATE, {"force": force}) == "ERROR"
    assert h.sink.of(ACTION_ERROR) == [{"message": "Invalid request: force must be true or false."}]
    assert h.github.calls == 0


@pytest.mark.parametrize("payload", ["not json", "[]", "5", '"x"'])
def test_a_malformed_payload_is_an_error_not_a_crash(tmp_path: Path, payload: str) -> None:
    h = make(tmp_path)
    for action in (ACTION_CHECK_UPDATE, ACTION_DISMISS_UPDATE, ACTION_SET_UPDATE_CHECK):
        assert call(h, action, payload) == "ERROR"
    assert h.github.calls == 0


# dismiss_update ---------------------------------------------------------------------------------


def test_dismiss_hides_the_banner_and_is_remembered(tmp_path: Path) -> None:
    h = make(tmp_path)
    call(h, ACTION_READY)
    h.sink.raw.clear()
    assert call(h, ACTION_DISMISS_UPDATE, {"tag": "v0.2.0"}) == "OK"
    assert h.sink.states() == ["available"]
    assert h.sink.updates()[0]["dismissed"] is True
    assert saved(h)["dismissed_tag"] == "v0.2.0"
    # a new window (new controller) keeps it hidden, and a newer release shows again
    again = make(tmp_path)
    call(again, ACTION_READY)
    assert again.sink.updates()[0]["dismissed"] is True
    again.github.tag = "v0.3.0"
    again.clock.value += DAY
    call(again, ACTION_READY)
    assert again.sink.updates()[-1]["latest"] == "0.3.0"
    assert again.sink.updates()[-1]["dismissed"] is False


@pytest.mark.parametrize(
    "tag", ["0.2.0", "v0.2", "v0.2.0-evil", "v01.2.3", "V0.2.0", "", "../x", None, 2, [], {}]
)
def test_dismiss_with_a_bad_tag_is_an_error(tmp_path: Path, tag: Any) -> None:
    h = make(tmp_path)
    assert call(h, ACTION_DISMISS_UPDATE, {"tag": tag}) == "ERROR"
    assert h.sink.of(ACTION_ERROR) == [{"message": BAD_TAG_TEXT}]
    assert not h.update_path.exists()


def test_dismiss_without_a_tag_is_an_error(tmp_path: Path) -> None:
    h = make(tmp_path)
    assert call(h, ACTION_DISMISS_UPDATE, {}) == "ERROR"
    assert h.sink.of(ACTION_ERROR) == [{"message": BAD_TAG_TEXT}]


# set_update_check -------------------------------------------------------------------------------


def test_switching_the_check_off_is_remembered(tmp_path: Path) -> None:
    h = make(tmp_path)
    assert call(h, ACTION_SET_UPDATE_CHECK, {"enabled": False}) == "OK"
    assert h.sink.states() == ["disabled"]
    assert h.sink.updates()[0]["enabled"] is False
    assert saved(h)["enabled"] is False
    again = make(tmp_path)
    call(again, ACTION_READY)
    assert again.sink.states() == ["disabled"]
    assert again.github.calls == 0


def test_switching_the_check_on_checks_at_once_when_due(tmp_path: Path) -> None:
    h = make(tmp_path)
    call(h, ACTION_SET_UPDATE_CHECK, {"enabled": False})
    h.sink.raw.clear()
    assert call(h, ACTION_SET_UPDATE_CHECK, {"enabled": True}) == "OK"
    assert h.github.calls == 1
    assert h.sink.states() == ["current", "checking", "available"]
    assert saved(h)["enabled"] is True


def test_switching_the_check_on_does_not_check_again_when_not_due(tmp_path: Path) -> None:
    h = make(tmp_path)
    call(h, ACTION_READY)
    call(h, ACTION_SET_UPDATE_CHECK, {"enabled": False})
    h.sink.raw.clear()
    call(h, ACTION_SET_UPDATE_CHECK, {"enabled": True})
    assert h.github.calls == 1
    assert h.sink.states() == ["available"]


@pytest.mark.parametrize("enabled", ["true", 1, 0, None, [], "false"])
def test_enabled_must_be_a_real_boolean(tmp_path: Path, enabled: Any) -> None:
    h = make(tmp_path)
    assert call(h, ACTION_SET_UPDATE_CHECK, {"enabled": enabled}) == "ERROR"
    assert h.sink.of(ACTION_ERROR) == [
        {"message": "Invalid request: enabled must be true or false."}
    ]
    assert not h.update_path.exists()


def test_a_missing_enabled_never_means_off(tmp_path: Path) -> None:
    h = make(tmp_path)
    assert call(h, ACTION_SET_UPDATE_CHECK, {}) == "ERROR"
    assert not h.update_path.exists()
    assert h.sink.of(ACTION_ERROR) == [
        {"message": "Invalid request: enabled must be true or false."}
    ]


# Without a service (the old palette mode) -------------------------------------------------------


def test_without_a_service_the_actions_answer_disabled(tmp_path: Path) -> None:
    h = make(tmp_path, with_service=False)
    assert call(h, ACTION_CHECK_UPDATE, {"force": True}) == "OK"
    assert call(h, ACTION_SET_UPDATE_CHECK, {"enabled": True}) == "OK"
    assert call(h, ACTION_DISMISS_UPDATE, {"tag": "v0.2.0"}) == "OK"
    assert h.sink.states() == ["disabled", "disabled", "disabled"]
    first = h.sink.updates()[0]
    assert tuple(first) == controller.UPDATE_FIELDS
    assert (first["enabled"], first["latest"], first["installer_url"]) == (False, None, None)
    assert call(h, ACTION_DISMISS_UPDATE, {"tag": "nope"}) == "ERROR"


# When something inside goes wrong ---------------------------------------------------------------


class FlakyService:
    """A service whose methods can be made to raise, to prove the controller guards them."""

    def __init__(self) -> None:
        self.begun = False
        self.aborted = 0
        self.finish_error: Exception | None = None
        self.fetch_error: Exception | None = None
        self.abort_error: Exception | None = None
        self.view_error: Exception | None = None

    def _view(self, state: str) -> UpdateView:
        return UpdateView(state, CURRENT, None, None, None, None, True, False)

    def view(self) -> UpdateView:
        if self.view_error is not None:
            raise self.view_error
        return self._view("checking" if self.begun else "current")

    def begin_check(self, force: bool) -> bool:
        self.begun = True
        return True

    def fetch_latest(self) -> CheckResult:
        if self.fetch_error is not None:
            raise self.fetch_error
        info = UpdateInfo("0.2.0", "v0.2.0", RELEASE_020, None)
        return CheckResult(STATE_AVAILABLE, CURRENT, T0, info, "0.2.0")

    def finish_check(self, result: CheckResult) -> UpdateView:
        self.begun = False
        if self.finish_error is not None:
            raise self.finish_error
        return self._view("available")

    def abort_check(self) -> UpdateView:
        self.begun = False
        self.aborted += 1
        if self.abort_error is not None:
            raise self.abort_error
        return self._view("error")

    def dismiss(self, tag: str) -> UpdateView:
        return self._view("current")

    def set_enabled(self, enabled: bool) -> UpdateView:
        return self._view("current")


def flaky(runner: Any) -> tuple[Controller, Sink, FlakyService, list[str]]:
    sink, service, logs = Sink(), FlakyService(), []
    ctrl = Controller(
        Service(),
        Importer(),
        sink,
        runner,
        logs.append,
        out_dir=Path("unused"),
        update_service=service,
    )
    return ctrl, sink, service, logs


def test_a_worker_that_raises_ends_the_check_with_an_error_message() -> None:
    ctrl, sink, service, logs = flaky(SyncRunner())
    service.fetch_error = RuntimeError("worker crashed")
    assert ctrl.handle(ACTION_CHECK_UPDATE, '{"force": true}') == "OK"
    assert sink.states() == ["checking", "error"]
    assert service.aborted == 1
    assert any("worker crashed" in line for line in logs)
    assert ctrl.busy is False


def test_a_service_that_fails_to_record_the_result_is_aborted_and_logged() -> None:
    ctrl, sink, service, logs = flaky(SyncRunner())
    service.finish_error = RuntimeError("disk exploded")
    ctrl.handle(ACTION_CHECK_UPDATE, '{"force": true}')
    assert sink.states() == ["checking", "error"]
    assert service.aborted == 1
    assert any("disk exploded" in line for line in logs)


def test_a_runner_that_cannot_start_ends_the_check_and_does_not_raise() -> None:
    ctrl, sink, service, logs = flaky(BrokenRunner())
    assert ctrl.handle(ACTION_CHECK_UPDATE, '{"force": true}') == "OK"
    assert sink.states() == ["checking", "error"]
    assert service.aborted == 1
    assert any("cannot start a thread" in line for line in logs)
    assert ctrl.busy is False


def test_an_abort_that_also_fails_is_logged_and_never_escapes() -> None:
    ctrl, sink, service, logs = flaky(SyncRunner())
    service.fetch_error = RuntimeError("worker crashed")
    service.abort_error = RuntimeError("abort failed too")
    assert ctrl.handle(ACTION_CHECK_UPDATE, '{"force": true}') == "OK"
    assert sink.states() == ["checking"]
    assert any("abort failed too" in line for line in logs)


def test_a_failing_sink_during_completion_does_not_raise() -> None:
    held = HeldRunner()
    ctrl, sink, _service, logs = flaky(held)
    ctrl.handle(ACTION_CHECK_UPDATE, '{"force": true}')
    sink.fail = True
    held.finish()  # must not raise into Fusion's event loop
    assert any("Could not send 'update'" in line for line in logs)


def test_a_service_that_raises_on_ready_is_logged_and_never_shown_as_an_error() -> None:
    ctrl, sink, service, logs = flaky(SyncRunner())
    service.view_error = RuntimeError("view broke")
    assert ctrl.handle(ACTION_READY, "{}") == "OK"
    assert ACTION_ERROR not in sink.actions()
    assert sink.states() == []
    assert any("view broke" in line for line in logs)
    assert sink.of(ACTION_STATUS)  # the rest of `ready` was answered


# Consistency with the rest ----------------------------------------------------------------------


def test_every_update_action_has_a_handler_and_the_message_is_documented() -> None:
    doc = controller.__doc__ or ""
    for action in (
        ACTION_CHECK_UPDATE,
        ACTION_DISMISS_UPDATE,
        ACTION_SET_UPDATE_CHECK,
        ACTION_UPDATE,
    ):
        assert re.search(rf"^\s+{action}\b", doc, re.MULTILINE), action
    for field in controller.UPDATE_FIELDS:
        assert field in doc
    for state in controller.UPDATE_STATES:
        assert state in doc


def test_update_messages_are_ignored_by_the_local_api_job_tracker() -> None:
    from local_api import JobTracker

    logs: list[str] = []
    tracker = JobTracker(logs.append)
    h = make(Path("."), with_service=False)
    call(h, ACTION_CHECK_UPDATE, {"force": True})
    for action, data in h.sink.raw:
        tracker.observe(action, data)
    assert logs == []


def test_the_adapter_hands_the_controller_a_real_update_service() -> None:
    source = Path(controller.__file__).with_name("fusion_adapter.py").read_text(encoding="utf-8")
    assert "update_service=UpdateService(UpdateStore(default_update_path())" in source
    assert "from update_service import" in source
    entry = Path(controller.__file__).with_name("LCSC2Fusion.py").read_text(encoding="utf-8")
    assert '"update_check"' in entry
    assert '"update_service"' in entry
