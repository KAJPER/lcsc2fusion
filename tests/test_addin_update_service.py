"""update.json, the schedule and the service of the update check (update_service.py), offline."""

from __future__ import annotations

import ast
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

import controller
import pytest
import update_service
from update_check import (
    STATE_AVAILABLE,
    STATE_CURRENT,
    STATE_ERROR,
    CheckResult,
    FetchResponse,
    UpdateInfo,
    installer_download_url,
    release_page_url,
)
from update_service import (
    AUTO_INTERVAL_S,
    FLOOD_LIMIT_S,
    RETRY_AFTER_FAILURE_S,
    LastResult,
    UpdateService,
    UpdateSettings,
    UpdateStore,
    UpdateView,
)

HOUR = 3600.0
T0 = 1_760_000_000.0
CURRENT = "0.1.0"
RELEASE_020 = "https://github.com/KAJPER/lcsc2fusion/releases/tag/v0.2.0"
INSTALLER_020 = (
    "https://github.com/KAJPER/lcsc2fusion/releases/download/v0.2.0/LCSC2Fusion-Setup-0.2.0.exe"
)


class Clock:
    def __init__(self, start: float = T0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def available(
    version: tuple[int, int, int] = (0, 2, 0), *, installer: bool = True, at: float = T0
) -> CheckResult:
    text = ".".join(str(n) for n in version)
    info = UpdateInfo(
        version=text,
        tag="v" + text,
        release_url=release_page_url(version),
        installer_url=installer_download_url(version) if installer else None,
    )
    return CheckResult(
        state=STATE_AVAILABLE, current=CURRENT, checked_at=at, update=info, latest=text
    )


def current_result(latest: str = "0.1.0", at: float = T0) -> CheckResult:
    return CheckResult(state=STATE_CURRENT, current=CURRENT, checked_at=at, latest=latest)


def failed(reason: str = "timeout", at: float = T0) -> CheckResult:
    return CheckResult(state=STATE_ERROR, current=CURRENT, checked_at=at, reason=reason)


class Env:
    """A service on a temporary update.json with a controllable clock."""

    def __init__(self, tmp_path: Path, *, current: str = CURRENT) -> None:
        self.path = tmp_path / "update.json"
        self.clock = Clock()
        self.logs: list[str] = []
        self.current = current
        self.service = self.new_service()

    def new_service(self, fetch: Any = None) -> UpdateService:
        return UpdateService(
            UpdateStore(self.path),
            current_version=self.current,
            fetch=fetch,
            now=self.clock,
            log=self.logs.append,
        )

    def saved(self) -> dict[str, Any]:
        loaded = json.loads(self.path.read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)
        return loaded

    def run_check(self, result: CheckResult, *, force: bool = False) -> UpdateView:
        assert self.service.begin_check(force)
        return self.service.finish_check(result)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


# The file location ------------------------------------------------------------------------------


def test_update_json_sits_next_to_state_json_on_every_platform(tmp_path: Path) -> None:
    from library_registry import default_state_path

    cases: list[tuple[dict[str, str], str]] = [
        ({"LOCALAPPDATA": str(tmp_path / "local")}, "win32"),
        ({}, "win32"),
        ({}, "darwin"),
        ({"XDG_STATE_HOME": str(tmp_path / "state")}, "linux"),
        ({}, "linux"),
        ({"LCSC2FUSION_STATE": str(tmp_path / "somewhere" / "my-state.json")}, "win32"),
    ]
    for environ, platform in cases:
        state = default_state_path(environ, platform, tmp_path)
        update = update_service.default_update_path(environ, platform, tmp_path)
        assert update.name == "update.json"
        assert update.parent == state.parent


def test_the_windows_location_is_local_app_data_lcsc2fusion(tmp_path: Path) -> None:
    path = update_service.default_update_path(
        {"LOCALAPPDATA": str(tmp_path / "L")}, "win32", tmp_path
    )
    assert path == tmp_path / "L" / "LCSC2Fusion" / "update.json"


# Loading and saving ------------------------------------------------------------------------------


def test_a_missing_file_gives_the_defaults_with_the_check_on(tmp_path: Path) -> None:
    store = UpdateStore(tmp_path / "update.json")
    assert store.load() == UpdateSettings()
    assert store.load().enabled is True
    assert store.warnings == ()


def test_settings_survive_a_round_trip(tmp_path: Path) -> None:
    store = UpdateStore(tmp_path / "sub" / "update.json")  # the folder is created on demand
    settings = UpdateSettings(
        enabled=False,
        last_checked_at=T0,
        last_attempt_ok=False,
        last_result=LastResult("v0.2.0", True, False),
        dismissed_tag="v0.2.0",
    )
    store.save(settings)
    assert store.load() == settings
    assert store.warnings == ()
    document = json.loads(store.path.read_text(encoding="utf-8"))
    assert set(document) == {
        "version",
        "enabled",
        "last_checked_at",
        "last_attempt_ok",
        "last_result",
        "dismissed_tag",
    }
    assert document["last_result"] == {"latest_tag": "v0.2.0", "newer": True, "installer": False}


def test_save_leaves_no_temporary_files(tmp_path: Path) -> None:
    store = UpdateStore(tmp_path / "update.json")
    store.save(UpdateSettings())
    store.save(UpdateSettings(enabled=False))
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["update.json"]


def test_a_failed_save_keeps_the_old_file_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = UpdateStore(tmp_path / "update.json")
    store.save(UpdateSettings(enabled=False))

    def refuse(source: str, target: object) -> None:
        raise PermissionError("locked")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError, match="locked"):
        store.save(UpdateSettings(enabled=True))
    monkeypatch.undo()
    assert UpdateStore(tmp_path / "update.json").load().enabled is False
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["update.json"]


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"   \n",
        b"not json",
        b"[]",
        b'"enabled"',
        b"42",
        b"null",
        b'{"version": 2, "enabled": true}',
        b'{"enabled": true}',  # no version: not our file
        b'{"version": "1", "enabled": true}',
        b'{"version": true}',
        b"\xff\xfe\x00 not utf-8",
        b'{"version": 1, "enabled": tru',
        pytest.param(b"[" * 100_000, id="deep-nesting"),
    ],
)
def test_a_file_that_cannot_be_understood_switches_the_check_off(
    tmp_path: Path, content: bytes
) -> None:
    path = tmp_path / "update.json"
    path.write_bytes(content)
    store = UpdateStore(path)
    settings = store.load()
    assert settings == UpdateSettings(enabled=False)
    assert len(store.warnings) == 1
    assert "off" in store.warnings[0]
    assert str(path) in store.warnings[0]


def test_a_path_that_is_a_directory_switches_the_check_off(tmp_path: Path) -> None:
    folder = tmp_path / "update.json"
    folder.mkdir()
    store = UpdateStore(folder)
    assert store.load().enabled is False
    assert store.warnings


@pytest.mark.parametrize("value", ["true", "yes", 1, 0, None, [], {}])
def test_an_enabled_field_that_is_not_a_boolean_switches_the_check_off(
    tmp_path: Path, value: Any
) -> None:
    path = tmp_path / "update.json"
    path.write_text(json.dumps({"version": 1, "enabled": value}), encoding="utf-8")
    store = UpdateStore(path)
    assert store.load().enabled is False
    assert store.warnings


def test_a_missing_enabled_field_means_on(tmp_path: Path) -> None:
    path = tmp_path / "update.json"
    path.write_text(json.dumps({"version": 1}), encoding="utf-8")
    store = UpdateStore(path)
    assert store.load() == UpdateSettings()
    assert store.warnings == ()


@pytest.mark.parametrize("value", ["1760000000", -1, True, False, [], {}, math.nan, math.inf])
def test_a_bad_last_checked_at_is_dropped_and_the_rest_kept(tmp_path: Path, value: Any) -> None:
    path = tmp_path / "update.json"
    path.write_text(
        json.dumps({"version": 1, "enabled": False, "last_checked_at": value}), encoding="utf-8"
    )
    store = UpdateStore(path)
    settings = store.load()
    assert settings.last_checked_at is None
    assert settings.enabled is False
    assert store.warnings


def test_an_integer_timestamp_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "update.json"
    path.write_text(json.dumps({"version": 1, "last_checked_at": 1760000000}), encoding="utf-8")
    assert UpdateStore(path).load().last_checked_at == 1760000000.0


@pytest.mark.parametrize(
    "value",
    [
        "v0.2.0",
        [],
        {"latest_tag": "v0.2.0"},
        {"latest_tag": "0.2.0", "newer": True, "installer": True},
        {"latest_tag": "v0.2", "newer": True, "installer": True},
        {"latest_tag": "v0.2.0", "newer": "yes", "installer": True},
        {"latest_tag": "v0.2.0", "newer": True, "installer": 1},
        {"latest_tag": 2, "newer": True, "installer": True},
    ],
)
def test_a_bad_last_result_is_dropped(tmp_path: Path, value: Any) -> None:
    path = tmp_path / "update.json"
    path.write_text(json.dumps({"version": 1, "last_result": value}), encoding="utf-8")
    store = UpdateStore(path)
    assert store.load().last_result is None
    assert store.warnings


@pytest.mark.parametrize("value", ["0.2.0", "v0.2", "v1.2.3-evil", 5, [], {}, True])
def test_a_bad_dismissed_tag_is_dropped(tmp_path: Path, value: Any) -> None:
    path = tmp_path / "update.json"
    path.write_text(json.dumps({"version": 1, "dismissed_tag": value}), encoding="utf-8")
    store = UpdateStore(path)
    assert store.load().dismissed_tag is None
    assert store.warnings


def test_a_bad_attempt_flag_counts_as_a_success_and_unknown_keys_are_ignored(
    tmp_path: Path,
) -> None:
    path = tmp_path / "update.json"
    path.write_text(
        json.dumps({"version": 1, "last_attempt_ok": "no", "future_key": {"a": 1}}),
        encoding="utf-8",
    )
    store = UpdateStore(path)
    assert store.load().last_attempt_ok is True
    assert store.warnings


# The view ----------------------------------------------------------------------------------------


def test_the_view_has_exactly_the_fields_of_the_protocol(env: Env) -> None:
    message = env.service.view().to_message()
    assert tuple(message) == controller.UPDATE_FIELDS
    assert json.loads(json.dumps(message)) == message


def test_a_fresh_service_knows_nothing_and_says_current(env: Env) -> None:
    assert env.service.view() == UpdateView(
        state="current",
        current=CURRENT,
        latest=None,
        release_url=None,
        installer_url=None,
        checked_at=None,
        enabled=True,
        dismissed=False,
    )
    assert not env.path.exists()  # looking never writes


def test_an_available_release_is_reported_with_urls_built_here(env: Env) -> None:
    view = env.run_check(available())
    assert view == UpdateView(
        state="available",
        current=CURRENT,
        latest="0.2.0",
        release_url=RELEASE_020,
        installer_url=INSTALLER_020,
        checked_at=T0,
        enabled=True,
        dismissed=False,
    )
    assert view.state in controller.UPDATE_STATES


def test_a_release_without_the_installer_has_no_installer_url(env: Env) -> None:
    view = env.run_check(available(installer=False))
    assert (view.state, view.installer_url) == ("available", None)
    assert view.release_url == RELEASE_020
    assert env.service.view().installer_url is None  # also from the cache


def test_the_result_is_saved_and_a_new_session_reads_it_without_the_network(env: Env) -> None:
    env.run_check(available())
    assert env.saved() == {
        "version": 1,
        "enabled": True,
        "last_checked_at": T0,
        "last_attempt_ok": True,
        "last_result": {"latest_tag": "v0.2.0", "newer": True, "installer": True},
        "dismissed_tag": None,
    }

    def no_network(*args: object) -> FetchResponse:
        raise AssertionError("the cached view must not touch the network")

    again = env.new_service(fetch=no_network)
    view = again.view()
    assert (view.state, view.latest, view.installer_url) == ("available", "0.2.0", INSTALLER_020)
    assert view.checked_at == T0


def test_a_current_result_is_saved_too(env: Env) -> None:
    view = env.run_check(current_result("0.1.0"))
    assert (view.state, view.latest, view.release_url) == ("current", "0.1.0", None)
    assert env.saved()["last_result"] == {
        "latest_tag": "v0.1.0",
        "newer": False,
        "installer": False,
    }


def test_the_banner_goes_away_once_the_running_version_has_caught_up(tmp_path: Path) -> None:
    first = Env(tmp_path)
    first.run_check(available())
    upgraded = Env(tmp_path, current="0.2.0")
    view = upgraded.service.view()
    assert (view.state, view.release_url, view.installer_url) == ("current", None, None)
    assert view.latest == "0.2.0"
    ahead = Env(tmp_path, current="0.3.0")
    assert ahead.service.view().state == "current"


def test_an_error_result_is_reported_once_and_the_cache_never_says_error(env: Env) -> None:
    env.run_check(available())
    env.clock.advance(AUTO_INTERVAL_S)
    view = env.run_check(failed("network"))
    assert view.state == "error"
    assert view.latest == "0.2.0"  # the knowledge of the earlier success is kept
    assert env.service.view().state == "available"
    assert env.service.view().latest == "0.2.0"
    assert any("network" in line for line in env.logs)


def test_a_failure_without_earlier_knowledge_has_none(env: Env) -> None:
    view = env.run_check(failed())
    assert (view.state, view.latest, view.release_url) == ("error", None, None)
    assert env.service.view().state == "current"


def test_checking_overlays_the_cached_knowledge(env: Env) -> None:
    env.run_check(available())
    env.clock.advance(AUTO_INTERVAL_S)
    assert env.service.begin_check(False)
    view = env.service.view()
    assert (view.state, view.latest, view.release_url) == ("checking", "0.2.0", RELEASE_020)
    env.service.finish_check(current_result("0.2.0"))
    assert env.service.view().state == "available"


def test_an_unparsable_running_version_never_claims_an_update(tmp_path: Path) -> None:
    odd = Env(tmp_path, current="dev")
    odd.run_check(available())
    view = odd.service.view()
    assert (view.state, view.current) == ("current", "dev")


# The schedule -------------------------------------------------------------------------------------


def test_the_first_automatic_check_is_due_at_once(env: Env) -> None:
    assert env.service.begin_check(False) is True


def test_an_automatic_check_waits_24_hours_after_a_success(env: Env) -> None:
    env.run_check(current_result())
    env.clock.advance(AUTO_INTERVAL_S - 1)
    assert env.service.begin_check(False) is False
    env.clock.advance(1)
    assert env.service.begin_check(False) is True


def test_an_automatic_check_waits_6_hours_after_a_failure(env: Env) -> None:
    env.run_check(failed())
    env.clock.advance(RETRY_AFTER_FAILURE_S - 1)
    assert env.service.begin_check(False) is False
    env.clock.advance(1)
    assert env.service.begin_check(False) is True


def test_a_second_failure_backs_off_again_from_its_own_time(env: Env) -> None:
    env.run_check(failed())
    env.clock.advance(RETRY_AFTER_FAILURE_S)
    env.run_check(failed(at=env.clock.value))
    env.clock.advance(RETRY_AFTER_FAILURE_S - 1)
    assert env.service.begin_check(False) is False


def test_the_numbers_are_the_documented_ones() -> None:
    assert AUTO_INTERVAL_S == 24 * HOUR
    assert RETRY_AFTER_FAILURE_S == 6 * HOUR
    assert FLOOD_LIMIT_S == 60.0


def test_the_schedule_survives_a_restart(env: Env) -> None:
    env.run_check(current_result())
    env.clock.advance(HOUR)
    assert env.new_service().begin_check(False) is False
    env.clock.advance(AUTO_INTERVAL_S)
    assert env.new_service().begin_check(False) is True


def test_the_backoff_survives_a_restart(env: Env) -> None:
    env.run_check(failed())
    env.clock.advance(HOUR)
    assert env.new_service().begin_check(False) is False
    env.clock.advance(6 * HOUR)
    assert env.new_service().begin_check(False) is True


def test_a_stored_time_in_the_future_does_not_block_the_check_for_ever(env: Env) -> None:
    env.run_check(current_result())
    env.clock.advance(-10 * 24 * HOUR)  # the clock was set back
    assert env.service.begin_check(False) is True


def test_only_one_check_runs_at_a_time(env: Env) -> None:
    assert env.service.begin_check(False) is True
    assert env.service.begin_check(False) is False
    assert env.service.begin_check(True) is False
    env.service.finish_check(current_result())
    env.clock.advance(FLOOD_LIMIT_S)
    assert env.service.begin_check(True) is True


def test_a_manual_check_ignores_the_24_hour_limit(env: Env) -> None:
    env.run_check(current_result())
    env.clock.advance(HOUR)
    assert env.service.begin_check(False) is False
    assert env.service.begin_check(True) is True


def test_a_manual_check_ignores_the_6_hour_backoff(env: Env) -> None:
    env.run_check(failed())
    env.clock.advance(FLOOD_LIMIT_S)
    assert env.service.begin_check(False) is False
    assert env.service.begin_check(True) is True


def test_two_manual_checks_less_than_a_minute_apart_start_only_one(env: Env) -> None:
    env.run_check(current_result(), force=True)
    env.clock.advance(FLOOD_LIMIT_S - 1)
    assert env.service.begin_check(True) is False
    env.clock.advance(1)
    assert env.service.begin_check(True) is True


def test_a_manual_check_right_after_an_automatic_one_is_a_flood_too(env: Env) -> None:
    env.run_check(current_result())  # the automatic check of this session
    env.clock.advance(5)
    assert env.service.begin_check(True) is False


def test_the_flood_limit_counts_one_session_not_the_file(env: Env) -> None:
    env.run_check(current_result(), force=True)
    env.clock.advance(5)
    assert env.new_service().begin_check(True) is True  # a new add-in session


def test_a_manual_check_still_works_when_the_automatic_one_is_off(env: Env) -> None:
    env.service.set_enabled(False)
    assert env.service.begin_check(False) is False
    assert env.service.begin_check(True) is True


# Switch and dismiss -----------------------------------------------------------------------------


def test_the_switch_is_saved_and_survives_a_restart(env: Env) -> None:
    view = env.service.set_enabled(False)
    assert (view.state, view.enabled) == ("disabled", False)
    assert env.saved()["enabled"] is False
    assert env.new_service().view().enabled is False
    view = env.service.set_enabled(True)
    assert (view.state, view.enabled) == ("current", True)
    assert env.saved()["enabled"] is True


def test_a_disabled_check_never_starts_by_itself(env: Env) -> None:
    env.service.set_enabled(False)
    for _ in range(3):
        env.clock.advance(AUTO_INTERVAL_S * 10)
        assert env.service.begin_check(False) is False


def test_disabled_hides_what_is_known_but_keeps_it(env: Env) -> None:
    env.run_check(available())
    view = env.service.set_enabled(False)
    assert view.state == "disabled"
    assert (view.latest, view.release_url, view.installer_url, view.dismissed) == (
        None,
        None,
        None,
        False,
    )
    assert env.saved()["last_result"]["latest_tag"] == "v0.2.0"
    assert env.service.set_enabled(True).state == "available"


def test_the_result_of_a_manual_check_is_shown_even_when_the_check_is_off(env: Env) -> None:
    env.service.set_enabled(False)
    view = env.run_check(available(), force=True)
    assert (view.state, view.latest, view.enabled) == ("available", "0.2.0", False)
    assert env.service.view().state == "disabled"  # the next message hides it again
    env.service.set_enabled(False)
    env.clock.advance(FLOOD_LIMIT_S)
    assert env.run_check(failed(at=env.clock.value), force=True).state == "error"
    env.clock.advance(FLOOD_LIMIT_S)
    assert env.run_check(current_result(), force=True).state == "current"


def test_dismissing_hides_the_banner_for_that_version_only(env: Env) -> None:
    env.run_check(available())
    view = env.service.dismiss("v0.2.0")
    assert (view.state, view.dismissed) == ("available", True)
    assert env.saved()["dismissed_tag"] == "v0.2.0"
    assert env.new_service().view().dismissed is True
    env.clock.advance(AUTO_INTERVAL_S)
    newer = env.run_check(available((0, 3, 0)))
    assert (newer.state, newer.latest, newer.dismissed) == ("available", "0.3.0", False)


def test_dismissing_survives_the_next_check_of_the_same_version(env: Env) -> None:
    env.run_check(available())
    env.service.dismiss("v0.2.0")
    env.clock.advance(AUTO_INTERVAL_S)
    again = env.run_check(available(at=env.clock.value))
    assert again.dismissed is True


def test_dismissing_a_tag_that_is_not_the_latest_changes_nothing_visible(env: Env) -> None:
    env.run_check(available())
    view = env.service.dismiss("v0.1.5")
    assert view.dismissed is False


@pytest.mark.parametrize("tag", ["0.2.0", "v0.2", "v1.2.3-evil", "", "v01.0.0", "latest"])
def test_dismissing_something_that_is_not_a_tag_raises(env: Env, tag: str) -> None:
    with pytest.raises(ValueError, match="not a release tag"):
        env.service.dismiss(tag)
    assert not env.path.exists()


# Failures around the service ---------------------------------------------------------------------


def test_an_unwritable_file_is_logged_and_the_session_carries_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a folder should be", encoding="utf-8")
    env = Env(tmp_path)
    env.path = blocker / "update.json"
    env.service = env.new_service()
    view = env.run_check(available())
    assert view.state == "available"
    assert any("Could not write" in line for line in env.logs)
    env.clock.advance(HOUR)
    assert env.service.begin_check(False) is False  # the session still remembers the attempt
    assert env.service.set_enabled(False).enabled is False


def test_a_corrupt_file_logs_a_warning_once_and_keeps_the_check_off(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.path.write_text("{broken", encoding="utf-8")
    service = env.new_service()
    assert service.view().state == "disabled"
    assert service.begin_check(False) is False
    assert len([line for line in env.logs if "update settings" in line]) == 1
    assert service.set_enabled(True).state == "current"  # the user's choice repairs the file
    assert env.saved()["enabled"] is True


def test_abort_check_ends_the_check_and_counts_as_a_failed_attempt(env: Env) -> None:
    assert env.service.begin_check(False)
    view = env.service.abort_check()
    assert view.state == "error"
    assert env.saved()["last_attempt_ok"] is False
    env.clock.advance(FLOOD_LIMIT_S)
    assert env.service.begin_check(True) is True  # no longer "in flight"
    env.service.abort_check()
    assert env.service.begin_check(False) is False  # backed off


@pytest.mark.parametrize(
    "result",
    [
        None,
        "boom",
        CheckResult(state=STATE_AVAILABLE, current=CURRENT, checked_at=T0, latest=None),
        CheckResult(state=STATE_CURRENT, current=CURRENT, checked_at=T0, latest="v0.1.0"),
        CheckResult(state="weird", current=CURRENT, checked_at=T0, latest="0.2.0"),
    ],
)
def test_a_result_that_makes_no_sense_is_a_failed_attempt_not_an_exception(
    env: Env, result: Any
) -> None:
    assert env.service.begin_check(False)
    view = env.service.finish_check(result)
    assert view.state == "error"
    assert env.saved()["last_attempt_ok"] is False
    assert env.saved()["last_result"] is None


def test_a_broken_clock_does_not_raise(tmp_path: Path) -> None:
    def broken() -> float:
        raise RuntimeError("no clock")

    service = UpdateService(
        UpdateStore(tmp_path / "update.json"), current_version=CURRENT, now=broken
    )
    assert service.begin_check(False) is True
    view = service.finish_check(current_result(at=0.0))
    assert view.state == "current"


def test_fetch_latest_runs_the_real_lookup_through_the_injected_fetch(env: Env) -> None:
    calls: list[str] = []

    def fetch(url: str, headers: Any, timeout: float, max_bytes: int) -> FetchResponse:
        calls.append(url)
        document = {
            "tag_name": "v0.2.0",
            "draft": False,
            "prerelease": False,
            "assets": [{"name": "LCSC2Fusion-Setup-0.2.0.exe"}],
        }
        return FetchResponse(200, json.dumps(document).encode("utf-8"))

    service = env.new_service(fetch=fetch)
    assert service.begin_check(False)
    result = service.fetch_latest()
    assert calls == ["https://api.github.com/repos/KAJPER/lcsc2fusion/releases/latest"]
    view = service.finish_check(result)
    assert (view.state, view.installer_url) == ("available", INSTALLER_020)


def test_fetch_latest_never_raises(env: Env) -> None:
    def fetch(url: str, headers: Any, timeout: float, max_bytes: int) -> FetchResponse:
        raise RuntimeError("boom")

    result = env.new_service(fetch=fetch).fetch_latest()
    assert (result.state, result.reason) == ("error", "internal")


def test_the_default_current_version_is_the_package_version(tmp_path: Path) -> None:
    import lcsc2fusion

    service = UpdateService(UpdateStore(tmp_path / "update.json"))
    assert service.view().current == lcsc2fusion.__version__


def test_the_modules_are_free_of_fusion_and_third_party_code() -> None:
    allowed = set(sys.stdlib_module_names) | {"__future__", "update_check", "lcsc2fusion"}
    allowed |= {"library_registry"}  # imported lazily, for the location of state.json
    tree = ast.parse(Path(update_service.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    assert names <= allowed, names - allowed
    # the top level imports must not pull in the core or the registry (kept light to import)
    top = {
        node.module.split(".")[0]
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not top & {"lcsc2fusion", "library_registry"}


def test_the_built_add_in_carries_both_update_modules(tmp_path: Path) -> None:
    import importlib.util
    import zipfile

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "build_addin_for_update_test", root / "scripts" / "build_addin.py"
    )
    assert spec is not None
    assert spec.loader is not None
    build_addin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = build_addin  # dataclasses look the defining module up here
    try:
        spec.loader.exec_module(build_addin)
        result = build_addin.build(root=root, dist=tmp_path)
    finally:
        del sys.modules[spec.name]
    with zipfile.ZipFile(result.zip_path) as archive:
        names = set(archive.namelist())
    assert "LCSC2Fusion/update_check.py" in names
    assert "LCSC2Fusion/update_service.py" in names


def test_a_file_saved_with_a_byte_order_mark_is_still_read(tmp_path: Path) -> None:
    """Notepad and PowerShell 5 add a BOM: that must not switch the check off."""
    path = tmp_path / "update.json"
    document = {"version": 1, "enabled": True, "dismissed_tag": "v0.2.0"}
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(document).encode("utf-8"))
    store = UpdateStore(path)
    settings = store.load()
    assert (settings.enabled, settings.dismissed_tag) == (True, "v0.2.0")
    assert store.warnings == ()
