"""The GitHub release lookup (update_check.py): pure logic, fake fetch, no network."""

from __future__ import annotations

import http.client
import json
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
import update_check
from update_check import (
    MAX_RESPONSE_BYTES,
    REQUEST_TIMEOUT_S,
    CheckResult,
    FetchError,
    FetchResponse,
    check_latest,
)

CURRENT = "0.1.0"
NOW = 1_760_000_000.0
INSTALLER_020 = "LCSC2Fusion-Setup-0.2.0.exe"


def release(
    tag: Any = "v0.2.0",
    *,
    assets: Any = None,
    draft: Any = False,
    prerelease: Any = False,
    **extra: Any,
) -> dict[str, Any]:
    """A release document shaped like the GitHub answer (the parts the add-in reads, plus noise)."""
    if assets is None:
        assets = [
            {
                "name": "SHA256SUMS.txt",
                "browser_download_url": "https://github.com/x/SHA256SUMS.txt",
            },
            {"name": INSTALLER_020, "browser_download_url": "https://github.com/x/setup.exe"},
        ]
    document: dict[str, Any] = {
        "url": "https://api.github.com/repos/KAJPER/lcsc2fusion/releases/1",
        "html_url": "https://github.com/KAJPER/lcsc2fusion/releases/tag/v0.2.0",
        "tag_name": tag,
        "name": "Release",
        "draft": draft,
        "prerelease": prerelease,
        "assets": assets,
        "body": "Release notes. Click https://evil.example/ now!",
    }
    document.update(extra)
    return document


class FakeFetch:
    """Records the request and answers with a prepared response or error."""

    def __init__(self, answer: FetchResponse | Exception | object) -> None:
        self.answer = answer
        self.calls: list[tuple[str, dict[str, str], float, int]] = []

    def __call__(
        self, url: str, headers: Mapping[str, str], timeout: float, max_bytes: int
    ) -> FetchResponse:
        self.calls.append((url, dict(headers), timeout, max_bytes))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer  # type: ignore[return-value]


def json_response(document: Any, status: int = 200) -> FetchResponse:
    return FetchResponse(status, json.dumps(document).encode("utf-8"))


def run(answer: FetchResponse | Exception | object, current: str = CURRENT) -> CheckResult:
    return check_latest(current, fetch=FakeFetch(answer), now=lambda: NOW)


# Versions and tags --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0.1.0", (0, 1, 0)),
        ("1.2.3", (1, 2, 3)),
        ("10.20.30", (10, 20, 30)),
        ("999999.0.0", (999999, 0, 0)),
    ],
)
def test_parse_version_accepts_three_plain_numbers(
    text: str, expected: tuple[int, int, int]
) -> None:
    assert update_check.parse_version(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "1.2",
        "1.2.3.4",
        "01.2.3",
        "1.02.3",
        "1.2.03",
        "1.2.3-rc1",
        "1.2.3+build",
        "v1.2.3",
        " 1.2.3",
        "1.2.3 ",
        "1.2.3\n",
        "1.2.1000000",
        "\uff11.\uff12.\uff13",  # full width digits
        "1.2.-3",
        None,
        5,
        (1, 2, 3),
    ],
)
def test_parse_version_rejects_everything_else(text: object) -> None:
    assert update_check.parse_version(text) is None


@pytest.mark.parametrize(
    "tag",
    [
        "1.2.3",  # no v
        "V1.2.3",
        "vv1.2.3",
        "v1.2",
        "v1.2.3.4",
        "v1.2.3-evil",
        "v1.2.3-rc1",
        "v1.2.3+meta",
        "v01.2.3",  # leading zeros are rejected on purpose
        "v1.2.3 ",
        "v1.2.3\n",
        "v1.2.3\x00",
        "v1.2.1000000",
        "v1,2,3",
        "",
        "latest",
        None,
        3,
        ["v1.2.3"],
    ],
)
def test_parse_tag_rejects_anything_but_v_and_three_numbers(tag: object) -> None:
    assert update_check.parse_tag(tag) is None


def test_parse_tag_accepts_v_and_three_numbers() -> None:
    assert update_check.parse_tag("v0.1.0") == (0, 1, 0)
    assert update_check.parse_tag("v12.0.345") == (12, 0, 345)


def test_urls_are_built_from_constants_and_the_version() -> None:
    version = (0, 2, 0)
    assert (
        update_check.LATEST_RELEASE_URL
        == "https://api.github.com/repos/KAJPER/lcsc2fusion/releases/latest"
    )
    assert (
        update_check.release_page_url(version)
        == "https://github.com/KAJPER/lcsc2fusion/releases/tag/v0.2.0"
    )
    assert update_check.installer_file_name(version) == INSTALLER_020
    assert update_check.installer_download_url(version) == (
        "https://github.com/KAJPER/lcsc2fusion/releases/download/v0.2.0/" + INSTALLER_020
    )
    assert update_check.format_tag(version) == "v0.2.0"


def test_owner_and_repository_live_in_one_place() -> None:
    assert (update_check.GITHUB_OWNER, update_check.GITHUB_REPO) == ("KAJPER", "lcsc2fusion")
    source = Path(update_check.__file__).read_text(encoding="utf-8")
    assert source.count('"KAJPER"') == 1
    assert source.count('"lcsc2fusion"') == 1


# Outcomes -----------------------------------------------------------------------------------------


def test_a_newer_release_is_available_with_urls_built_here() -> None:
    result = run(json_response(release()))
    assert result.state == "available"
    assert (result.current, result.latest, result.checked_at, result.reason) == (
        CURRENT,
        "0.2.0",
        NOW,
        None,
    )
    assert result.update is not None
    assert result.update.version == "0.2.0"
    assert result.update.tag == "v0.2.0"
    assert result.update.release_url == "https://github.com/KAJPER/lcsc2fusion/releases/tag/v0.2.0"
    assert result.update.installer_url == (
        "https://github.com/KAJPER/lcsc2fusion/releases/download/v0.2.0/" + INSTALLER_020
    )


def test_the_same_version_is_current() -> None:
    result = run(json_response(release("v0.1.0")))
    assert (result.state, result.latest, result.update, result.reason) == (
        "current",
        "0.1.0",
        None,
        None,
    )


@pytest.mark.parametrize("tag", ["v0.0.9", "v0.0.1", "v0.1.0"])
def test_an_older_release_is_never_offered(tag: str) -> None:
    result = run(json_response(release(tag)))
    assert result.state == "current"
    assert result.update is None


def test_numbers_are_compared_as_numbers_not_as_text() -> None:
    assert run(json_response(release("v0.10.0")), current="0.9.0").state == "available"
    assert run(json_response(release("v0.9.0")), current="0.10.0").state == "current"
    assert run(json_response(release("v2.0.0")), current="1.99.99").state == "available"


def test_a_release_without_the_installer_asset_has_no_installer_link() -> None:
    result = run(json_response(release(assets=[{"name": "SHA256SUMS.txt"}])))
    assert result.state == "available"
    assert result.update is not None
    assert result.update.installer_url is None
    assert result.update.release_url.endswith("/releases/tag/v0.2.0")


@pytest.mark.parametrize(
    "name",
    [
        "../LCSC2Fusion-Setup-0.2.0.exe",
        "..\\LCSC2Fusion-Setup-0.2.0.exe",
        "/LCSC2Fusion-Setup-0.2.0.exe",
        "x/LCSC2Fusion-Setup-0.2.0.exe",
        "LCSC2Fusion-Setup-0.2.0.exe.zip",
        "LCSC2Fusion-Setup-0.2.0.exe ",
        " LCSC2Fusion-Setup-0.2.0.exe",
        "LCSC2Fusion-Setup-0.2.0.EXE",
        "lcsc2fusion-setup-0.2.0.exe",
        "LCSC2Fusion-Setup-0.2.1.exe",
        "LCSC2Fusion-Setup-0.1.0.exe",
        "LCSC2Fusion-Setup-.exe",
        "LCSC2Fusion-Setup-0.2.0.exe\x00.txt",
        "LCSC2Fusion-0.2.0-installer.zip",
        None,
        5,
        ["LCSC2Fusion-Setup-0.2.0.exe"],
    ],
)
def test_only_an_asset_with_exactly_the_installer_name_counts(name: Any) -> None:
    result = run(json_response(release(assets=[{"name": name}])))
    assert result.state == "available"
    assert result.update is not None
    assert result.update.installer_url is None


@pytest.mark.parametrize("assets", [None, "x", 5, {"name": INSTALLER_020}, [None, 5, "x", []]])
def test_odd_asset_lists_mean_no_installer_not_an_error(assets: Any) -> None:
    document = release()
    document["assets"] = assets
    result = run(json_response(document))
    assert result.state == "available"
    assert result.update is not None
    assert result.update.installer_url is None


def test_urls_in_the_answer_are_never_used() -> None:
    hostile = release(
        html_url="https://evil.example/phish",
        assets=[
            {
                "name": INSTALLER_020,
                "browser_download_url": "https://evil.example/LCSC2Fusion-Setup-0.2.0.exe",
                "url": "https://evil.example/api",
            }
        ],
        zipball_url="https://evil.example/zip",
    )
    result = run(json_response(hostile))
    assert result.update is not None
    shown = f"{result.update.release_url} {result.update.installer_url}"
    assert "evil" not in shown
    assert result.update.installer_url == update_check.installer_download_url((0, 2, 0))


def test_the_release_text_is_not_part_of_the_result() -> None:
    result = run(json_response(release()))
    assert "evil" not in repr(result)
    assert "Release notes" not in repr(result)


@pytest.mark.parametrize("flag", ["draft", "prerelease"])
def test_drafts_and_prereleases_are_ignored(flag: str) -> None:
    result = run(json_response(release(**{flag: True})))
    assert (result.state, result.reason, result.update) == ("error", "not_stable", None)


@pytest.mark.parametrize("value", ["false", 0, 1, None, [], "true"])
@pytest.mark.parametrize("flag", ["draft", "prerelease"])
def test_draft_and_prerelease_must_be_real_booleans(flag: str, value: Any) -> None:
    result = run(json_response(release(**{flag: value})))
    assert (result.state, result.reason) == ("error", "bad_response")


def test_missing_draft_or_prerelease_is_a_bad_response() -> None:
    document = release()
    del document["draft"]
    assert run(json_response(document)).reason == "bad_response"


@pytest.mark.parametrize(
    "tag",
    ["v1.2", "1.2.3", "v1.2.3-evil", "v01.2.3", "V1.2.3", "", None, 123, ["v1.2.3"], {"a": 1}],
)
def test_a_malformed_tag_is_an_error(tag: Any) -> None:
    result = run(json_response(release(tag)))
    assert (result.state, result.reason, result.update, result.latest) == (
        "error",
        "bad_tag",
        None,
        None,
    )


def test_a_missing_tag_is_an_error() -> None:
    document = release()
    del document["tag_name"]
    assert run(json_response(document)).reason == "bad_tag"


# Hostile and broken answers -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"<html>captive portal</html>",
        b"\xff\xfe\x00\x00",
        b"null",
        b"[]",
        b'"v9.9.9"',
        b"12",
        b"true",
        b'{"tag_name": ',
        pytest.param(b"[" * 100_000 + b"]" * 100_000, id="deep-nesting"),  # RecursionError
    ],
)
def test_a_body_that_is_not_a_json_object_is_a_bad_response(body: bytes) -> None:
    result = run(FetchResponse(200, body))
    assert (result.state, result.reason) == ("error", "bad_response")


def test_a_huge_body_is_refused_before_it_is_parsed() -> None:
    body = b'{"tag_name": "v9.9.9", "pad": "' + b"x" * MAX_RESPONSE_BYTES + b'"}'
    result = run(FetchResponse(200, body))
    assert (result.state, result.reason) == ("error", "too_large")


def test_a_body_exactly_at_the_cap_is_still_read() -> None:
    document = json.dumps(release()).encode("utf-8")
    padded = document[:-1] + b"," + b'"pad":"' + b"x" * (MAX_RESPONSE_BYTES - len(document) - 10)
    padded += b'"}'
    assert len(padded) <= MAX_RESPONSE_BYTES
    assert run(FetchResponse(200, padded)).state == "available"


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (403, "rate_limited"),
        (429, "rate_limited"),
        (404, "not_found"),
        (500, "server_error"),
        (502, "server_error"),
        (503, "server_error"),
        (301, "http_error"),
        (304, "http_error"),
        (401, "http_error"),
        (204, "http_error"),
    ],
)
def test_http_statuses_other_than_200_map_to_short_reasons(status: int, reason: str) -> None:
    result = run(json_response(release(), status=status))
    assert (result.state, result.reason) == ("error", reason)


@pytest.mark.parametrize(
    "answer",
    [None, "200", b"{}", (200, b"{}"), {"status": 200}, FetchResponse("200", b"{}")],  # type: ignore[arg-type]
)
def test_a_fetch_that_returns_the_wrong_thing_is_a_bad_response(answer: object) -> None:
    result = run(answer)
    assert (result.state, result.reason) == ("error", "bad_response")


def test_a_boolean_status_is_not_accepted() -> None:
    assert run(FetchResponse(True, b"{}")).reason == "bad_response"  # type: ignore[arg-type]


def test_a_body_of_the_wrong_type_is_a_bad_response() -> None:
    assert run(FetchResponse(200, "{}")).reason == "bad_response"  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (FetchError("tls"), "tls"),
        (FetchError("redirect"), "redirect"),
        (TimeoutError("timed out"), "timeout"),
        (ssl.SSLError("bad certificate"), "tls"),
        (ssl.SSLCertVerificationError("verify failed"), "tls"),
        (ConnectionRefusedError("no"), "network"),
        (ConnectionResetError("reset"), "network"),
        (OSError("unreachable"), "network"),
        (http.client.IncompleteRead(b"x"), "network"),
        (http.client.BadStatusLine("x"), "network"),
        (RuntimeError("anything else"), "internal"),
        (ValueError("anything else"), "internal"),
        (MemoryError(), "internal"),
    ],
)
def test_network_errors_become_an_error_result_and_never_raise(
    error: Exception, reason: str
) -> None:
    result = run(error)
    assert (result.state, result.reason, result.update, result.latest) == (
        "error",
        reason,
        None,
        None,
    )
    assert result.current == CURRENT
    assert result.checked_at == NOW


def test_fuzzed_documents_never_raise() -> None:
    values: list[Any] = [None, True, 0, -1, 1.5, "", "v1.2.3", [], {}, [[]], {"a": {"b": []}}]
    for tag in values:
        for assets in values:
            for draft in (False, None):
                document = {"tag_name": tag, "assets": assets, "draft": draft, "prerelease": False}
                result = run(json_response(document))
                assert result.state in ("available", "current", "error")


# The request --------------------------------------------------------------------------------------


def test_exactly_one_request_with_the_documented_headers_and_limits() -> None:
    fetch = FakeFetch(json_response(release()))
    check_latest(CURRENT, fetch=fetch, now=lambda: NOW)
    assert len(fetch.calls) == 1
    url, headers, timeout, cap = fetch.calls[0]
    assert url == "https://api.github.com/repos/KAJPER/lcsc2fusion/releases/latest"
    assert headers == {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "LCSC2Fusion/0.1.0",
    }
    assert timeout == REQUEST_TIMEOUT_S == 8.0
    assert cap == MAX_RESPONSE_BYTES == 256 * 1024


def test_the_request_identifies_nothing_about_the_user() -> None:
    fetch = FakeFetch(json_response(release()))
    check_latest(CURRENT, fetch=fetch, now=lambda: NOW)
    names = {name.lower() for name in fetch.calls[0][1]}
    assert names == {"accept", "x-github-api-version", "user-agent"}
    assert not names & {"authorization", "cookie", "x-forwarded-for", "referer", "from"}


@pytest.mark.parametrize("current", ["dev", "", "0.1", "0.1.0-dev", "v0.1.0", "01.0.0", "1.2.3 "])
def test_an_unparsable_running_version_is_an_error_and_sends_nothing(current: str) -> None:
    fetch = FakeFetch(json_response(release()))
    result = check_latest(current, fetch=fetch, now=lambda: NOW)
    assert (result.state, result.reason) == ("error", "bad_current_version")
    assert fetch.calls == []


def test_the_user_agent_never_carries_a_hostile_version() -> None:
    fetch = FakeFetch(json_response(release()))
    check_latest("0.1.0\r\nX-Evil: 1", fetch=fetch, now=lambda: NOW)
    assert fetch.calls == []


def test_the_clock_is_injectable_and_a_broken_one_costs_only_the_timestamp() -> None:
    def broken() -> float:
        raise RuntimeError("no clock")

    result = check_latest(CURRENT, fetch=FakeFetch(json_response(release())), now=broken)
    assert result.state == "available"
    assert result.checked_at == 0.0
    nan = check_latest(CURRENT, fetch=FakeFetch(json_response(release())), now=lambda: float("nan"))
    assert nan.checked_at == 0.0


# The real fetch, with a fake opener ---------------------------------------------------------------


class FakeBody:
    """Stands in for the object `opener.open` returns."""

    def __init__(
        self,
        data: bytes,
        status: int = 200,
        headers: Mapping[str, str] | None = None,
        chunk_limit: int | None = None,
    ) -> None:
        self._data = data
        self.status = status
        self.headers = dict(headers or {})
        self.reads: list[int] = []
        self._chunk_limit = chunk_limit

    def read(self, size: int = -1) -> bytes:
        self.reads.append(size)
        if size < 0 or self._chunk_limit is not None:
            size = min(size if size >= 0 else len(self._data), self._chunk_limit or len(self._data))
        taken, self._data = self._data[:size], self._data[size:]
        return taken

    def __enter__(self) -> FakeBody:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeOpener:
    def __init__(self, outcome: Callable[[urllib.request.Request, float], Any]) -> None:
        self._outcome = outcome
        self.requests: list[urllib.request.Request] = []

    def open(self, request: urllib.request.Request, timeout: float) -> Any:
        self.requests.append(request)
        return self._outcome(request, timeout)


def with_opener(
    monkeypatch: pytest.MonkeyPatch, outcome: Callable[[urllib.request.Request, float], Any]
) -> FakeOpener:
    opener = FakeOpener(outcome)
    monkeypatch.setattr(update_check, "_build_opener", lambda: opener)
    return opener


def fetch_real() -> FetchResponse:
    return update_check.fetch_https(
        update_check.LATEST_RELEASE_URL, {"Accept": "x"}, 8.0, MAX_RESPONSE_BYTES
    )


def test_fetch_https_returns_status_and_body(monkeypatch: pytest.MonkeyPatch) -> None:
    opener = with_opener(monkeypatch, lambda request, timeout: FakeBody(b'{"ok": 1}'))
    response = fetch_real()
    assert (response.status, response.body) == (200, b'{"ok": 1}')
    assert opener.requests[0].full_url == update_check.LATEST_RELEASE_URL
    assert opener.requests[0].get_method() == "GET"
    assert opener.requests[0].data is None


@pytest.mark.parametrize(
    "url",
    [
        "http://api.github.com/repos/KAJPER/lcsc2fusion/releases/latest",
        "https://github.com/repos/KAJPER/lcsc2fusion/releases/latest",
        "https://evil.example/repos/KAJPER/lcsc2fusion/releases/latest",
        "https://api.github.com.evil.example/x",
        "https://user@api.github.com/x",
        "https://api.github.com:8443/x",
        "file:///etc/passwd",
        "ftp://api.github.com/x",
        "",
    ],
)
def test_fetch_https_refuses_any_url_but_https_on_the_api_host(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    opener = with_opener(monkeypatch, lambda request, timeout: FakeBody(b"{}"))
    with pytest.raises(FetchError) as caught:
        update_check.fetch_https(url, {}, 8.0, 1000)
    assert caught.value.reason == "bad_url"
    assert opener.requests == []


def test_fetch_https_turns_an_http_error_into_a_response(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(request: urllib.request.Request, timeout: float) -> Any:
        raise urllib.error.HTTPError(request.full_url, 403, "rate limit", {}, None)  # type: ignore[arg-type]

    with_opener(monkeypatch, fail)
    response = fetch_real()
    assert (response.status, response.body) == (403, b"")


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (TimeoutError("timed out"), "timeout"),
        (urllib.error.URLError(TimeoutError("timed out")), "timeout"),
        (ssl.SSLError("x"), "tls"),
        (urllib.error.URLError(ssl.SSLCertVerificationError("x")), "tls"),
        (urllib.error.URLError(OSError("getaddrinfo failed")), "network"),
        (ConnectionResetError("reset"), "network"),
        (http.client.RemoteDisconnected("closed"), "network"),
        (FetchError("redirect"), "redirect"),
    ],
)
def test_fetch_https_maps_failures_to_fetch_errors(
    error: Exception, reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(request: urllib.request.Request, timeout: float) -> Any:
        raise error

    with_opener(monkeypatch, fail)
    with pytest.raises(FetchError) as caught:
        fetch_real()
    assert caught.value.reason == reason


def test_fetch_https_refuses_a_body_over_the_cap_while_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = FakeBody(b"x" * (MAX_RESPONSE_BYTES * 4))
    with_opener(monkeypatch, lambda request, timeout: body)
    with pytest.raises(FetchError) as caught:
        fetch_real()
    assert caught.value.reason == "too_large"
    assert sum(size for size in body.reads if size > 0) <= MAX_RESPONSE_BYTES + 16 * 1024 + 1


def test_fetch_https_refuses_a_declared_size_over_the_cap_without_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = FakeBody(b"{}", headers={"Content-Length": str(MAX_RESPONSE_BYTES + 1)})
    with_opener(monkeypatch, lambda request, timeout: body)
    with pytest.raises(FetchError) as caught:
        fetch_real()
    assert caught.value.reason == "too_large"
    assert body.reads == []


def test_fetch_https_accepts_a_body_exactly_at_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    body = FakeBody(b"x" * MAX_RESPONSE_BYTES, headers={"Content-Length": str(MAX_RESPONSE_BYTES)})
    with_opener(monkeypatch, lambda request, timeout: body)
    assert len(fetch_real().body) == MAX_RESPONSE_BYTES


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br", "GZIP"])
def test_fetch_https_refuses_a_compressed_answer(
    encoding: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_opener(
        monkeypatch,
        lambda request, timeout: FakeBody(b"{}", headers={"Content-Encoding": encoding}),
    )
    with pytest.raises(FetchError) as caught:
        fetch_real()
    assert caught.value.reason == "bad_response"


def test_fetch_https_gives_up_on_an_answer_that_drips_in_too_slowly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = iter(range(0, 1000, 5))  # every call of the clock is five seconds later
    monkeypatch.setattr(update_check.time, "monotonic", lambda: float(next(ticks)))
    body = FakeBody(b"x" * 1000, chunk_limit=10)
    with_opener(monkeypatch, lambda request, timeout: body)
    with pytest.raises(FetchError) as caught:
        fetch_real()
    assert caught.value.reason == "timeout"


def test_the_opener_verifies_certificates_and_keeps_no_cookies() -> None:
    opener = update_check._build_opener()
    https = [h for h in opener.handlers if isinstance(h, urllib.request.HTTPSHandler)]
    assert len(https) == 1
    context = https[0]._context  # type: ignore[attr-defined]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    kinds = {type(handler) for handler in opener.handlers}
    assert urllib.request.HTTPCookieProcessor not in kinds
    assert update_check._SameHostRedirects in kinds
    assert update_check._SameHostRedirects.max_redirections <= 3


def test_the_opener_follows_the_system_proxy_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    opener = update_check._build_opener()
    proxies = [h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
    assert proxies
    assert proxies[0].proxies["https"] == "http://proxy.invalid:3128"  # type: ignore[attr-defined]


def request_for_redirect() -> urllib.request.Request:
    return urllib.request.Request(update_check.LATEST_RELEASE_URL, headers={"Accept": "x"})


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example/x",
        "http://api.github.com/repositories/1/releases/latest",
        "https://api.github.com:444/repositories/1/releases/latest",
        "https://user:pw@api.github.com/repositories/1/releases/latest",
        "https://api.github.com.evil.example/x",
        "https://github.com/KAJPER/lcsc2fusion/releases/latest",
        "file:///c:/windows/win.ini",
        "ftp://api.github.com/x",
    ],
)
def test_redirects_to_another_host_or_scheme_are_refused(target: str) -> None:
    handler = update_check._SameHostRedirects()
    with pytest.raises(FetchError) as caught:
        handler.redirect_request(request_for_redirect(), None, 301, "Moved", {}, target)
    assert caught.value.reason == "redirect"


def test_a_redirect_inside_the_api_host_is_followed_with_the_same_headers() -> None:
    handler = update_check._SameHostRedirects()
    target = "https://api.github.com/repositories/123/releases/latest"
    new = handler.redirect_request(request_for_redirect(), None, 301, "Moved", {}, target)
    assert new is not None
    assert new.full_url == target
    assert new.get_method() == "GET"
    assert new.get_header("Accept") == "x"


def test_the_module_imports_only_the_standard_library() -> None:
    import ast
    import sys

    tree = ast.parse(Path(update_check.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    assert names <= set(sys.stdlib_module_names) | {"__future__"}
    assert "adsk" not in names


# The recorded answer of the real API --------------------------------------------------------------

REAL_ANSWER = Path(__file__).parent / "fixtures" / "github_release_latest.json"


def real_response() -> FetchResponse:
    return FetchResponse(200, REAL_ANSWER.read_bytes())


def test_the_recorded_real_answer_for_the_running_release_is_current() -> None:
    result = run(real_response(), current="0.1.0")
    assert (result.state, result.latest, result.update, result.reason) == (
        "current",
        "0.1.0",
        None,
        None,
    )


def test_the_recorded_real_answer_offers_the_real_installer_to_an_older_version() -> None:
    result = run(real_response(), current="0.0.1")
    assert result.state == "available"
    assert result.update is not None
    assert result.update.release_url == "https://github.com/KAJPER/lcsc2fusion/releases/tag/v0.1.0"
    assert result.update.installer_url == (
        "https://github.com/KAJPER/lcsc2fusion/releases/download/v0.1.0/LCSC2Fusion-Setup-0.1.0.exe"
    )
    document = json.loads(REAL_ANSWER.read_text(encoding="utf-8"))
    listed = {asset["browser_download_url"] for asset in document["assets"]}
    assert result.update.installer_url in listed  # the link we build is the one GitHub serves


def test_the_suite_cannot_reach_github_even_by_accident() -> None:
    """The conftest guard makes the real fetch fail like an offline machine (no request is sent)."""
    with pytest.raises(FetchError) as caught:
        update_check.fetch_https(update_check.LATEST_RELEASE_URL, {"Accept": "x"}, 8.0, 1000)
    assert caught.value.reason == "network"
    result = check_latest(CURRENT, now=lambda: NOW)  # the default fetch
    assert (result.state, result.reason) == ("error", "network")
