"""The Fusion client against a fake add-in (real HTTP on 127.0.0.1) and against fake time."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from mcp_fakes import (
    TOKEN,
    FakeAddin,
    FakeClock,
    done_doc,
    failed_doc,
    free_port,
    job_doc,
)

from lcsc2fusion.discovery import ApiEndpoint, write_api_file
from lcsc2fusion.mcp import fusion_client as fc
from lcsc2fusion.mcp.fusion_client import (
    FusionAuthError,
    FusionBusyError,
    FusionClient,
    FusionError,
    FusionNoAnswerError,
    FusionNotRunningError,
    FusionProtocolError,
    FusionRejectedError,
    FusionUnavailableError,
    JobInfo,
    UnknownJobError,
    new_steps,
    parse_job,
)


@pytest.fixture
def addin(tmp_path: Path) -> Any:
    with FakeAddin() as fake:
        fake.write_api_file(tmp_path / "api.json")
        yield fake


def make_client(tmp_path: Path, **options: Any) -> FusionClient:
    clock = FakeClock()
    defaults: dict[str, Any] = {"sleep": clock.sleep, "clock": clock.clock}
    return FusionClient(tmp_path / "api.json", **{**defaults, **options})


# Ping


def test_ping_sends_the_documented_headers(addin: FakeAddin, tmp_path: Path) -> None:
    ping = make_client(tmp_path).ping()
    assert (ping.busy, ping.version) == (False, 1)
    (request,) = addin.calls("GET", "/api/local/ping")
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["host"] == f"127.0.0.1:{addin.port}"
    assert "origin" not in request.headers
    assert request.headers["connection"] == "close"
    assert request.headers["user-agent"].startswith("lcsc2fusion-mcp/")
    assert addin.violations == []


def test_ping_reports_busy(addin: FakeAddin, tmp_path: Path) -> None:
    addin.busy = True
    assert make_client(tmp_path).ping().busy is True


@pytest.mark.parametrize(
    "reply",
    [
        (200, {"ok": False, "version": 1, "busy": False}),
        (200, {"ok": True, "version": "1", "busy": False}),
        (200, {"ok": True, "version": 1, "busy": "no"}),
        (200, {}),
        (500, {"error": "boom"}),
        (418, {"message": "teapot"}),
    ],
)
def test_ping_with_an_off_contract_answer_is_a_protocol_error(
    addin: FakeAddin, tmp_path: Path, reply: tuple[int, dict[str, Any]]
) -> None:
    addin.ping_reply = reply
    with pytest.raises(FusionProtocolError):
        make_client(tmp_path).ping()


def test_oversized_answer_is_refused(addin: FakeAddin, tmp_path: Path) -> None:
    addin.ping_reply = (200, {"ok": True, "pad": "x" * 1_200_000})
    with pytest.raises(FusionProtocolError, match="large"):
        make_client(tmp_path).ping()


def test_missing_api_file_says_the_add_in_is_not_running(tmp_path: Path) -> None:
    with pytest.raises(FusionNotRunningError, match="not running"):
        make_client(tmp_path).ping()


def test_stale_api_file_means_connection_refused(tmp_path: Path) -> None:
    endpoint = ApiEndpoint(f"http://127.0.0.1:{free_port()}", TOKEN, os.getpid())
    write_api_file(tmp_path / "api.json", endpoint)
    with pytest.raises(FusionNotRunningError, match="stale") as info:
        make_client(tmp_path).ping()
    assert "Add-Ins" in str(info.value)  # tells how to start it
    assert TOKEN not in str(info.value)


def test_unreadable_api_file_is_not_running(tmp_path: Path) -> None:
    (tmp_path / "api.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(FusionNotRunningError, match="not valid JSON"):
        make_client(tmp_path).ping()


def test_default_path_comes_from_the_environment(
    addin: FakeAddin, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "elsewhere.json"
    addin.write_api_file(target)
    monkeypatch.setenv("LCSC2FUSION_API", str(target))
    assert FusionClient().ping().busy is False


def test_a_silent_add_in_times_out(addin: FakeAddin, tmp_path: Path) -> None:
    addin.delay_s = 1.5
    with pytest.raises(FusionNoAnswerError, match="did not answer"):
        make_client(tmp_path, ping_timeout_s=0.2).ping()


# Token handling


def write_token(path: Path, addin: FakeAddin, token: str) -> None:
    write_api_file(path, ApiEndpoint(addin.url, token, os.getpid()))


@pytest.mark.parametrize("status", [401, 403])
def test_a_stale_token_is_retried_once_after_rereading_the_file(
    addin: FakeAddin, tmp_path: Path, status: int
) -> None:
    path = tmp_path / "api.json"
    write_token(path, addin, "stale-token-" + "x" * 20)  # the add-in was restarted since
    addin.auth_status = status
    addin.on_unauthorized = lambda: write_token(path, addin, TOKEN)  # api.json is rewritten
    assert make_client(tmp_path).ping().busy is False
    assert len(addin.calls("GET", "/api/local/ping")) == 2


@pytest.mark.parametrize("status", [401, 403])
def test_a_token_that_stays_wrong_is_an_auth_error_after_one_retry(
    addin: FakeAddin, tmp_path: Path, status: int
) -> None:
    bad = "wrong-token-" + "y" * 20
    write_token(tmp_path / "api.json", addin, bad)
    addin.auth_status = status
    with pytest.raises(FusionAuthError, match="Restart") as info:
        make_client(tmp_path).ping()
    assert len(addin.calls("GET", "/api/local/ping")) == 2
    assert bad not in str(info.value)


def test_a_rejected_add_is_retried_with_the_same_body(addin: FakeAddin, tmp_path: Path) -> None:
    path = tmp_path / "api.json"
    write_token(path, addin, "stale-token-" + "x" * 20)
    addin.on_unauthorized = lambda: write_token(path, addin, TOKEN)
    assert make_client(tmp_path).add("C9000001", False) == 1
    first, second = addin.calls("POST", "/api/local/add")
    assert first.body == second.body
    assert len(addin.job_scripts) == 1  # only the authorised request created a job


@pytest.mark.parametrize(
    "url",
    [
        "http://evil.example:1234",
        "http://127.0.0.1.evil.example:1234",
        "http://user:secret@127.0.0.1:1234",
        "https://127.0.0.1:1234",
        "http://127.0.0.1:99999",
        "http://127.0.0.1:0",
        "http://127.0.0.1",
        "http://127.0.0.1:1234/path",
        "http://127.0.0.1:1234?x=1",
        "http://[::1]:1234",
        "http://localhost:1234",
        "ftp://127.0.0.1:1234",
    ],
)
def test_only_a_plain_loopback_url_ever_gets_the_token(url: str) -> None:
    with pytest.raises(FusionNotRunningError, match=r"127\.0\.0\.1"):
        fc._loopback_port(url)


def test_the_client_refuses_a_foreign_host_before_connecting(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    endpoint = ApiEndpoint("http://evil.example:80", TOKEN, 1)
    with pytest.raises(FusionNotRunningError, match=r"127\.0\.0\.1"):
        client._send(endpoint, "GET", "/api/local/ping", None, 1.0)


def test_a_plain_loopback_url_gives_its_port() -> None:
    assert fc._loopback_port("http://127.0.0.1:51234") == 51234
    assert fc._loopback_port("http://127.0.0.1:51234/") == 51234


# Add


def test_add_posts_the_documented_body_and_headers(addin: FakeAddin, tmp_path: Path) -> None:
    assert make_client(tmp_path).add("C9000001", True) == 1
    (request,) = addin.calls("POST", "/api/local/add")
    assert json.loads(request.body) == {"lcsc_id": "C9000001", "with_3d": True, "fusion": True}
    assert request.headers["content-type"] == "application/json"
    assert request.headers["x-lcsc"] == "1"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert "origin" not in request.headers
    assert addin.violations == []


@pytest.mark.parametrize(
    ("reply", "error", "fragment"),
    [
        ((409, {"error": "busy", "message": "job 3 runs"}), FusionBusyError, "job 3 runs"),
        ((400, {"error": "bad_request", "message": "bad id"}), FusionRejectedError, "bad id"),
        ((503, {"error": "unavailable", "message": "no hub"}), FusionUnavailableError, "no hub"),
        ((500, {"error": "boom"}), FusionProtocolError, "HTTP 500"),
        ((202, {}), FusionProtocolError, "job number"),
        ((202, {"job": "7"}), FusionProtocolError, "job number"),
        ((202, {"job": True}), FusionProtocolError, "job number"),
        ((202, {"job": -1}), FusionProtocolError, "job number"),
    ],
)
def test_add_errors_become_specific_exceptions(
    addin: FakeAddin,
    tmp_path: Path,
    reply: tuple[int, dict[str, Any]],
    error: type[FusionError],
    fragment: str,
) -> None:
    addin.add_reply = reply
    with pytest.raises(error, match=fragment):
        make_client(tmp_path).add("C9000001", False)


def test_add_refuses_a_non_canonical_id_without_a_request(addin: FakeAddin, tmp_path: Path) -> None:
    for bad in ("c2040", "C2040 ", "2040", "C", "C2040/../x", ""):
        with pytest.raises(FusionRejectedError):
            make_client(tmp_path).add(bad, False)
    assert addin.requests == []


def test_a_slow_add_times_out(addin: FakeAddin, tmp_path: Path) -> None:
    addin.delay_s = 1.5
    with pytest.raises(FusionNoAnswerError, match="may still have been accepted"):
        make_client(tmp_path, add_timeout_s=0.2).add("C9000001", False)


# Job


def test_job_document_is_parsed(addin: FakeAddin, tmp_path: Path) -> None:
    addin.job_scripts[4] = [done_doc(4, "C9000001", "started")]
    info = make_client(tmp_path).job(4)
    assert info.job == 4
    assert info.terminal and info.succeeded
    assert info.steps == ("Downloading", "Merging into the library", "Done")
    assert info.added is not None and info.added.mpn == "SYN-R0603-10K"
    assert info.fusion is not None
    assert (info.fusion.three_d, info.fusion.library_name) == ("started", "LCSC2Fusion Library")
    assert info.fusion.merged and not info.fusion.created_library


def test_unknown_job_is_a_specific_error(addin: FakeAddin, tmp_path: Path) -> None:
    with pytest.raises(UnknownJobError, match="job 9"):
        make_client(tmp_path).job(9)


@pytest.mark.parametrize("bad", [-1, True, "1", 1.5, None])
def test_job_numbers_are_checked_before_they_reach_the_path(
    addin: FakeAddin, tmp_path: Path, bad: Any
) -> None:
    with pytest.raises(FusionRejectedError):
        make_client(tmp_path).job(bad)
    assert addin.requests == []


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"job": 1},
        {"job": 1, "state": "weird"},
        {"job": "1", "state": "running"},
        {"job": True, "state": "running"},
    ],
)
def test_off_contract_job_documents_are_protocol_errors(
    addin: FakeAddin, tmp_path: Path, document: dict[str, Any]
) -> None:
    addin.job_scripts[1] = [document]
    with pytest.raises(FusionProtocolError):
        make_client(tmp_path).job(1)


def test_parse_job_is_defensive_about_field_types() -> None:
    info = parse_job(
        {
            "job": 2,
            "state": "running",
            "ok": "yes",
            "status": "line\nbreak\x00\x07",
            "message": 5,
            "steps": ["a", 3, None, "b"],
            "added": "nope",
            "fusion": {"three_d": "bogus", "warnings": "x", "merged": 1},
        }
    )
    assert info.ok is None
    assert info.status == "line break"
    assert info.message == ""
    assert info.steps == ("a", "b")
    assert info.added is None
    assert info.fusion is not None
    assert info.fusion.three_d == "none"
    assert info.fusion.merged is False  # only a real true counts
    assert info.fusion.warnings == ()


def test_parse_job_caps_text_and_lists() -> None:
    info = parse_job(
        {
            "job": 1,
            "state": "done",
            "ok": True,
            "message": "m" * 5000,
            "steps": [f"s{i}" for i in range(500)],
        }
    )
    assert len(info.message) == 2000 and info.message.endswith("...")
    assert len(info.steps) == 100


def test_succeeded_needs_done_and_not_ok_false() -> None:
    assert parse_job(done_doc()).succeeded
    assert not parse_job(failed_doc()).succeeded
    assert not parse_job(job_doc(state="running")).succeeded
    assert parse_job(job_doc(state="done", ok=None)).succeeded
    assert not parse_job(job_doc(state="done", ok=False)).succeeded


# Steps


@pytest.mark.parametrize(
    ("previous", "current", "expected"),
    [
        ([], [], []),
        ([], ["a", "b"], ["a", "b"]),
        (["a"], ["a", "b", "c"], ["b", "c"]),
        (["a", "b"], ["a", "b"], []),
        (["a", "b", "c"], ["b", "c", "d"], ["d"]),  # the oldest line was dropped
        (["a", "b", "c"], ["c", "d", "e"], ["d", "e"]),
        (["a", "b"], ["x", "y"], ["x", "y"]),  # nothing in common
    ],
)
def test_new_steps(previous: list[str], current: list[str], expected: list[str]) -> None:
    assert new_steps(previous, current) == expected


# Waiting


def test_wait_polls_until_the_job_is_done(addin: FakeAddin, tmp_path: Path) -> None:
    addin.job_scripts[1] = [
        job_doc(1, steps=["one"]),
        job_doc(1, steps=["one", "two"]),
        done_doc(1),
    ]
    clock = FakeClock()
    polls: list[tuple[JobInfo | None, float]] = []
    client = make_client(tmp_path, sleep=clock.sleep, clock=clock.clock)
    info = client.wait_for_job(1, 240, lambda i, elapsed: polls.append((i, elapsed)))
    assert info.state == "done"
    assert [p[0].state for p in polls if p[0]] == ["running", "running", "done"]
    assert clock.sleeps == [1.0, 1.0]  # polled once a second
    assert [p[1] for p in polls] == [0.0, 1.0, 2.0]


def test_wait_returns_the_last_state_after_the_timeout(addin: FakeAddin, tmp_path: Path) -> None:
    addin.job_scripts[1] = [job_doc(1, steps=["one"], status="Still going")]
    clock = FakeClock()
    client = make_client(tmp_path, sleep=clock.sleep, clock=clock.clock)
    info = client.wait_for_job(1, 5.0)
    assert info.state == "running" and info.status == "Still going"
    assert clock.now == pytest.approx(5.0)
    assert len(addin.calls("GET", "/api/local/job/1")) == 6  # t = 0, 1, 2, 3, 4, 5


def test_wait_stops_on_a_failed_job(addin: FakeAddin, tmp_path: Path) -> None:
    addin.job_scripts[1] = [job_doc(1), failed_doc(1)]
    info = make_client(tmp_path).wait_for_job(1, 60)
    assert info.state == "failed" and info.message == "Fusion refused the library."


def test_wait_with_a_short_timeout_sleeps_only_the_remaining_time(
    addin: FakeAddin, tmp_path: Path
) -> None:
    addin.job_scripts[1] = [job_doc(1)]
    clock = FakeClock()
    client = make_client(tmp_path, sleep=clock.sleep, clock=clock.clock, poll_interval_s=1.0)
    client.wait_for_job(1, 0.4)
    assert clock.sleeps == [pytest.approx(0.4)]


class ScriptedJobs(FusionClient):
    """A client whose `job` follows a script of documents and exceptions."""

    def __init__(self, script: list[JobInfo | FusionError], **options: Any) -> None:
        clock = FakeClock()
        super().__init__(None, sleep=clock.sleep, clock=clock.clock, **options)
        self.script = script
        self.fake = clock
        self.polls = 0

    def job(self, job: int) -> JobInfo:
        self.polls += 1
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, FusionError):
            raise item
        return item


def running() -> JobInfo:
    return parse_job(job_doc(1))


def test_wait_tolerates_a_busy_fusion_that_does_not_answer() -> None:
    client = ScriptedJobs(
        [FusionNoAnswerError("busy")] * 5 + [parse_job(done_doc(1))],
    )
    seen: list[JobInfo | None] = []
    info = client.wait_for_job(1, 600, lambda i, _e: seen.append(i))
    assert info.state == "done"
    assert seen[:5] == [None] * 5


def test_wait_gives_up_when_the_add_in_never_answers() -> None:
    client = ScriptedJobs([FusionNoAnswerError("busy")])
    with pytest.raises(FusionNoAnswerError, match="fusion_job"):
        client.wait_for_job(1, 5)


def test_wait_returns_the_last_good_state_when_later_polls_time_out() -> None:
    client = ScriptedJobs([running(), FusionNoAnswerError("busy")])
    info = client.wait_for_job(1, 5)
    assert info.state == "running"


def test_wait_fails_after_three_connection_failures_in_a_row() -> None:
    client = ScriptedJobs([running(), FusionNotRunningError("gone")])
    with pytest.raises(FusionNotRunningError):
        client.wait_for_job(1, 600)
    assert client.polls == 1 + fc.MAX_POLL_CONNECTION_FAILURES


def test_wait_survives_two_connection_failures_in_a_row() -> None:
    gone = FusionNotRunningError("gone")
    client = ScriptedJobs([gone, gone, parse_job(done_doc(1))])
    assert client.wait_for_job(1, 600).state == "done"


def test_connection_failures_must_be_consecutive() -> None:
    gone = FusionNotRunningError("gone")
    client = ScriptedJobs([gone, gone, running(), gone, gone, running(), parse_job(done_doc(1))])
    assert client.wait_for_job(1, 600).state == "done"


def test_unknown_job_during_the_wait_is_raised() -> None:
    client = ScriptedJobs([UnknownJobError("forgotten")])
    with pytest.raises(UnknownJobError):
        client.wait_for_job(1, 600)


# No secrets in messages


def test_no_error_message_contains_the_token(addin: FakeAddin, tmp_path: Path) -> None:
    client = make_client(tmp_path, ping_timeout_s=0.2, add_timeout_s=0.2)
    addin.add_reply = (409, {"message": "busy with C1"})
    addin.accepted_tokens.clear()  # every request is now refused as unauthorised
    actions = [lambda: client.ping(), lambda: client.add("C9000001", False), lambda: client.job(1)]
    for action in actions:
        with pytest.raises(FusionAuthError) as info:
            action()
        assert TOKEN not in str(info.value)
    addin.accepted_tokens.add(TOKEN)
    addin.delay_s = 1.0
    with pytest.raises(FusionNoAnswerError) as slow:
        client.ping()
    assert TOKEN not in str(slow.value)
