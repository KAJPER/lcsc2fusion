"""Offline tests for EasyEdaClient (fake transport, fake clock and sleep, tmp cache)."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import pytest

from lcsc2fusion.client.cache import DiskCache
from lcsc2fusion.client.easyeda import (
    COMPONENT_URL,
    LCSC_DETAIL_URL,
    PRODUCT_COMPONENTS_URL,
    SEARCH_BY_NUMBERS_URL,
    EasyEdaClient,
    is_lcsc_id,
    normalize_lcsc_id,
)
from lcsc2fusion.client.http import HttpResponse, TransientNetworkError
from lcsc2fusion.client.ratelimit import BlockGuard, RateLimiter
from lcsc2fusion.errors import (
    LcscError,
    NetworkError,
    ParseError,
    PartNotFoundError,
    RateLimitedError,
)

UUID = "0123456789abcdef0123456789abcdef"


# Test doubles


class FakeTime:
    def __init__(self) -> None:
        self.now = 1000.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeTransport:
    """Replays outcomes in order and records every request. An exhausted script is a test bug."""

    def __init__(self, *outcomes: HttpResponse | Exception) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = 30.0,
    ) -> HttpResponse:
        self.calls.append(
            {"method": method, "url": url, "data": data, "headers": dict(headers or {})}
        )
        if not self._outcomes:
            raise AssertionError(f"unexpected request: {method} {url}")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def urls(self) -> list[str]:
        return [f"{call['method']} {call['url']}" for call in self.calls]


class CountingLimiter(RateLimiter):
    def __init__(self) -> None:
        super().__init__(0.0)
        self.waits = 0

    def wait(self) -> float:
        self.waits += 1
        return 0.0


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        *outcomes: HttpResponse | Exception,
        ttl_s: float = 3600.0,
        cache_clock: Callable[[], float] = time.time,
    ) -> None:
        self.time = FakeTime()
        self.retry_sleeps: list[float] = []
        self.transport = FakeTransport(*outcomes)
        self.limiter = CountingLimiter()
        self.guard = BlockGuard(300.0, clock=self.time.clock)
        self.cache = DiskCache(tmp_path / "cache", ttl_s, clock=cache_clock)
        self.client = EasyEdaClient(
            self.transport,
            self.cache,
            self.limiter,
            ttl_s,
            guard=self.guard,
            sleep=self.retry_sleeps.append,
        )


def reply(
    payload: Any, status: int = 200, headers: Mapping[str, str] | None = None
) -> HttpResponse:
    return HttpResponse(status, json.dumps(payload).encode("utf-8"), dict(headers or {}))


def raw_reply(body: bytes, status: int = 200) -> HttpResponse:
    return HttpResponse(status, body, {})


def search_reply(uuid: str = UUID, number: str = "C2040") -> HttpResponse:
    return reply(
        {
            "success": True,
            "result": [{"uuid": uuid, "puuid": "pkg-uuid", "number": number, "step": "step-uuid"}],
        }
    )


def make_document(number: str = "C2040", title: str = "RP2040") -> dict[str, Any]:
    return {
        "success": True,
        "code": 0,
        "result": {
            "uuid": UUID,
            "title": title,
            "description": "tiny test part",
            "lcsc": {"number": number},
            "dataStr": {"head": {"c_para": {"Manufacturer Part": title}}, "shape": []},
            "packageDetail": {"dataStr": {"shape": []}},
        },
    }


def doc_reply(number: str = "C2040") -> HttpResponse:
    return reply(make_document(number))


NOT_FOUND = {"success": False, "code": 404, "message": "not found"}
FORBIDDEN = HttpResponse(403, b"Request blocked", {"server": "CloudFront"})
COMPONENT = COMPONENT_URL.format(uuid=UUID)
PRODUCT = PRODUCT_COMPONENTS_URL.format(lcsc_id="C2040")


# Id handling


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("C2040", "C2040"), ("c2040", "C2040"), ("  c25804\n", "C25804"), ("C1", "C1")],
)
def test_normalize_lcsc_id(raw: str, expected: str) -> None:
    assert normalize_lcsc_id(raw) == expected
    assert is_lcsc_id(raw)


@pytest.mark.parametrize(
    "bad", ["", "   ", "2040", "C", "CX1", "C12a", "C-1", "C 2040", "CC2040", "C2040\nC1", "C٣٤"]
)
def test_invalid_ids_raise_part_not_found_without_network(tmp_path: Path, bad: str) -> None:
    harness = Harness(tmp_path)
    with pytest.raises(PartNotFoundError, match="not a valid LCSC part number"):
        harness.client.fetch_part(bad)
    assert harness.transport.calls == []
    assert not is_lcsc_id(bad)


# Happy path


def test_fetch_part_default_path_requests_exactly(tmp_path: Path) -> None:
    harness = Harness(tmp_path, search_reply(), doc_reply())
    document = harness.client.fetch_part(" c2040 ")

    search, component = harness.transport.calls
    assert search["method"] == "POST"
    assert search["url"] == "https://easyeda.com/api/components/searchByNumbers"
    assert search["url"] == SEARCH_BY_NUMBERS_URL
    assert search["headers"] == {"Content-Type": "application/x-www-form-urlencoded"}
    assert search["data"] == b"numbers=%5B%22C2040%22%5D"
    assert parse_qs(search["data"].decode()) == {"numbers": ['["C2040"]']}
    assert json.loads(parse_qs(search["data"].decode())["numbers"][0]) == ["C2040"]
    assert component["method"] == "GET"
    assert component["url"] == f"https://easyeda.com/api/components/{UUID}"
    assert component["data"] is None

    assert document == make_document()  # the whole top-level response, including "success"
    assert document["result"]["title"] == "RP2040"


def test_each_request_attempt_goes_through_the_rate_limiter(tmp_path: Path) -> None:
    harness = Harness(tmp_path, search_reply(), doc_reply())
    harness.client.fetch_part("C2040")
    assert harness.limiter.waits == 2


def test_uuid_is_url_quoted(tmp_path: Path) -> None:
    harness = Harness(tmp_path, search_reply(uuid="a/b c"), doc_reply())
    harness.client.fetch_part("C2040")
    assert harness.transport.calls[1]["url"] == "https://easyeda.com/api/components/a%2Fb%20c"


def test_search_result_with_other_number_is_ignored(tmp_path: Path) -> None:
    harness = Harness(tmp_path, search_reply(number="C999"), doc_reply())
    harness.client.fetch_part("C2040")
    assert harness.transport.urls()[1] == f"GET {PRODUCT}"


# Cache


def test_cache_hit_makes_no_network_call(tmp_path: Path) -> None:
    harness = Harness(tmp_path, search_reply(), doc_reply())
    first = harness.client.fetch_part("C2040")
    calls_after_first = len(harness.transport.calls)
    second = harness.client.fetch_part("c2040")
    assert second == first
    assert len(harness.transport.calls) == calls_after_first == 2


def test_cache_is_shared_between_clients_via_the_directory(tmp_path: Path) -> None:
    first = Harness(tmp_path, search_reply(), doc_reply())
    first.client.fetch_part("C2040")
    second = Harness(tmp_path)  # same cache directory, transport has no scripted replies
    assert second.client.fetch_part("C2040") == make_document()
    assert second.transport.calls == []


def test_cache_ttl_expiry_refetches(tmp_path: Path) -> None:
    now = {"offset": 0.0}
    harness = Harness(
        tmp_path,
        search_reply(),
        doc_reply(),
        search_reply(),
        doc_reply(),
        ttl_s=100,
        cache_clock=lambda: time.time() + now["offset"],
    )
    harness.client.fetch_part("C2040")
    now["offset"] = 50.0
    harness.client.fetch_part("C2040")
    assert len(harness.transport.calls) == 2
    now["offset"] = 500.0
    harness.client.fetch_part("C2040")
    assert len(harness.transport.calls) == 4


@pytest.mark.parametrize(
    "garbage",
    [
        b"\x00\xff not json at all",
        b"[1, 2, 3]",
        b'{"success": true}',
        b'{"success": true, "result": null}',
    ],
)
def test_corrupt_cache_entry_is_ignored_and_replaced(tmp_path: Path, garbage: bytes) -> None:
    harness = Harness(tmp_path, search_reply(), doc_reply())
    harness.cache.put("easyeda-C2040", garbage)
    assert harness.client.fetch_part("C2040") == make_document()
    assert len(harness.transport.calls) == 2
    assert harness.client.fetch_part("C2040") == make_document()
    assert len(harness.transport.calls) == 2  # healed: second read is a clean hit


def test_failed_lookups_are_not_cached(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path, search_reply(), reply(NOT_FOUND), reply(NOT_FOUND), search_reply(), doc_reply()
    )
    with pytest.raises(PartNotFoundError):
        harness.client.fetch_part("C2040")
    assert harness.client.fetch_part("C2040") == make_document()


def test_default_client_construction_touches_neither_network_nor_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "never-created"
    monkeypatch.setenv("LCSC2FUSION_CACHE", str(target))
    client = EasyEdaClient()
    assert client.cache.directory == target
    assert not target.exists()


# Fallback


def test_fallback_when_search_by_numbers_returns_nothing(tmp_path: Path) -> None:
    harness = Harness(tmp_path, reply({"success": True, "result": []}), doc_reply())
    assert harness.client.fetch_part("C2040") == make_document()
    assert harness.transport.urls() == [f"POST {SEARCH_BY_NUMBERS_URL}", f"GET {PRODUCT}"]
    assert PRODUCT == "https://easyeda.com/api/products/C2040/components"


def test_fallback_when_search_by_numbers_reports_failure(tmp_path: Path) -> None:
    harness = Harness(tmp_path, reply({"success": False}), doc_reply())
    harness.client.fetch_part("C2040")
    assert harness.transport.urls()[1] == f"GET {PRODUCT}"


def test_fallback_when_search_by_numbers_is_not_json(tmp_path: Path) -> None:
    harness = Harness(tmp_path, raw_reply(b"<html>oops</html>"), doc_reply())
    harness.client.fetch_part("C2040")
    assert harness.transport.urls()[1] == f"GET {PRODUCT}"


def test_fallback_when_search_by_numbers_fails_with_client_error(tmp_path: Path) -> None:
    harness = Harness(tmp_path, raw_reply(b"bad request", status=400), doc_reply())
    harness.client.fetch_part("C2040")
    assert harness.transport.urls() == [f"POST {SEARCH_BY_NUMBERS_URL}", f"GET {PRODUCT}"]
    assert harness.retry_sleeps == []  # a 400 is not retried


def test_fallback_when_search_by_numbers_keeps_failing_with_5xx(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path, raw_reply(b"", 503), raw_reply(b"", 502), raw_reply(b"", 500), doc_reply()
    )
    harness.client.fetch_part("C2040")
    assert harness.transport.urls() == [f"POST {SEARCH_BY_NUMBERS_URL}"] * 3 + [f"GET {PRODUCT}"]


def test_fallback_when_component_by_uuid_has_no_data(tmp_path: Path) -> None:
    harness = Harness(tmp_path, search_reply(), reply(NOT_FOUND), doc_reply())
    assert harness.client.fetch_part("C2040") == make_document()
    assert harness.transport.urls() == [
        f"POST {SEARCH_BY_NUMBERS_URL}",
        f"GET {COMPONENT}",
        f"GET {PRODUCT}",
    ]


def test_fallback_when_component_by_uuid_is_http_404(tmp_path: Path) -> None:
    harness = Harness(tmp_path, search_reply(), raw_reply(b"", 404), doc_reply())
    harness.client.fetch_part("C2040")
    assert harness.transport.urls()[-1] == f"GET {PRODUCT}"


# Not found


@pytest.mark.parametrize(
    "payload",
    [
        {"success": False, "code": 404},
        {"success": True, "code": 0},
        {"success": True, "result": None},
        {"success": True, "result": []},
        {"result": "oops"},
        [],
    ],
)
def test_part_without_data_raises_part_not_found(tmp_path: Path, payload: Any) -> None:
    harness = Harness(tmp_path, reply({"success": True, "result": []}), reply(payload))
    with pytest.raises(LcscError) as info:
        harness.client.fetch_part("C2040")
    if isinstance(payload, list):
        assert isinstance(info.value, ParseError)
    else:
        assert isinstance(info.value, PartNotFoundError)
        assert "C2040" in str(info.value)


def test_unknown_id_on_both_endpoints_is_part_not_found(tmp_path: Path) -> None:
    harness = Harness(tmp_path, reply({"success": True, "result": []}), reply(NOT_FOUND, 200))
    with pytest.raises(PartNotFoundError, match="no symbol/footprint data for C2040"):
        harness.client.fetch_part("C2040")
    assert len(harness.transport.calls) == 2
    assert harness.cache.get("easyeda-C2040") is None


def test_invalid_json_from_final_endpoint_is_parse_error(tmp_path: Path) -> None:
    harness = Harness(tmp_path, reply({"success": True, "result": []}), raw_reply(b"<html>"))
    with pytest.raises(ParseError, match="not valid JSON"):
        harness.client.fetch_part("C2040")


def test_unexpected_status_on_final_endpoint_is_network_error(tmp_path: Path) -> None:
    harness = Harness(tmp_path, reply({"success": True, "result": []}), raw_reply(b"", 400))
    with pytest.raises(NetworkError, match="HTTP status 400") as info:
        harness.client.fetch_part("C2040")
    assert not isinstance(info.value, TransientNetworkError)
    assert len(harness.transport.calls) == 2


def test_http_404_on_final_endpoint_is_part_not_found(tmp_path: Path) -> None:
    harness = Harness(tmp_path, reply({"success": True, "result": []}), raw_reply(b"", 404))
    with pytest.raises(PartNotFoundError):
        harness.client.fetch_part("C2040")


# Blocking


def test_403_raises_rate_limited_and_does_not_fall_back(tmp_path: Path) -> None:
    harness = Harness(tmp_path, FORBIDDEN)
    with pytest.raises(RateLimitedError) as info:
        harness.client.fetch_part("C2040")
    assert info.value.retry_after_s == 300.0
    assert "easyeda.com" in str(info.value)
    assert len(harness.transport.calls) == 1
    assert harness.retry_sleeps == []  # never retried


def test_403_on_component_fetch_also_stops_immediately(tmp_path: Path) -> None:
    harness = Harness(tmp_path, search_reply(), FORBIDDEN)
    with pytest.raises(RateLimitedError):
        harness.client.fetch_part("C2040")
    assert len(harness.transport.calls) == 2


def test_after_a_block_requests_fail_fast_until_the_cooldown_passes(tmp_path: Path) -> None:
    harness = Harness(tmp_path, FORBIDDEN, search_reply(), doc_reply())
    with pytest.raises(RateLimitedError):
        harness.client.fetch_part("C2040")

    harness.time.now += 100
    with pytest.raises(RateLimitedError) as info:
        harness.client.fetch_part("C2040")
    assert info.value.retry_after_s == pytest.approx(200.0)
    with pytest.raises(RateLimitedError):
        harness.client.fetch_part("C25804")
    assert len(harness.transport.calls) == 1  # fail fast: nothing hit the network

    harness.time.now += 201
    assert harness.client.fetch_part("C2040") == make_document()
    assert len(harness.transport.calls) == 3


def test_cached_parts_are_still_served_while_blocked(tmp_path: Path) -> None:
    harness = Harness(tmp_path, search_reply(), doc_reply(), FORBIDDEN)
    harness.client.fetch_part("C2040")
    with pytest.raises(RateLimitedError):
        harness.client.fetch_part("C25804")
    assert harness.client.fetch_part("C2040") == make_document()


def test_429_honours_retry_after_header(tmp_path: Path) -> None:
    throttled = HttpResponse(429, b"slow down", {"retry-after": "42"})
    harness = Harness(tmp_path, throttled)
    with pytest.raises(RateLimitedError) as info:
        harness.client.fetch_part("C2040")
    assert info.value.retry_after_s == 42.0
    assert harness.guard.remaining("easyeda.com") == pytest.approx(42.0)


@pytest.mark.parametrize("value", ["soon", "-5", "0"])
def test_unusable_retry_after_falls_back_to_default_cooldown(tmp_path: Path, value: str) -> None:
    harness = Harness(tmp_path, HttpResponse(429, b"", {"retry-after": value}))
    with pytest.raises(RateLimitedError) as info:
        harness.client.fetch_part("C2040")
    assert info.value.retry_after_s == 300.0


# Retry


def test_5xx_is_retried_with_exponential_backoff(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path, raw_reply(b"", 503), raw_reply(b"", 502), search_reply(), doc_reply()
    )
    assert harness.client.fetch_part("C2040") == make_document()
    assert harness.retry_sleeps == [1.0, 2.0]
    assert harness.limiter.waits == 4  # every attempt is rate limited


def test_timeouts_are_retried(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        TransientNetworkError("Request to easyeda.com timed out after 30 s"),
        TransientNetworkError("Request to easyeda.com timed out after 30 s"),
        search_reply(),
        doc_reply(),
    )
    assert harness.client.fetch_part("C2040") == make_document()
    assert harness.retry_sleeps == [1.0, 2.0]


def test_persistent_failure_raises_a_network_error_after_three_attempts(tmp_path: Path) -> None:
    down = [TransientNetworkError("Cannot reach easyeda.com: down")] * 6
    harness = Harness(tmp_path, *down)
    with pytest.raises(NetworkError, match=r"Cannot reach easyeda.com"):
        harness.client.fetch_part("C2040")
    # three attempts for searchByNumbers, then three for the fallback endpoint
    assert len(harness.transport.calls) == 6
    assert harness.retry_sleeps == [1.0, 2.0, 1.0, 2.0]


def test_plain_network_error_from_transport_is_not_retried(tmp_path: Path) -> None:
    harness = Harness(tmp_path, NetworkError("TLS error"), NetworkError("TLS error"))
    with pytest.raises(NetworkError, match="TLS error"):
        harness.client.fetch_part("C2040")
    assert len(harness.transport.calls) == 2  # one per endpoint, no retries
    assert harness.retry_sleeps == []


# Datasheet


def test_datasheet_url_from_lcsc_detail(tmp_path: Path) -> None:
    pdf = "https://www.lcsc.com/datasheet/lcsc_datasheet_C2040.pdf"
    harness = Harness(tmp_path, reply({"code": 200, "result": {"pdfUrl": pdf}}))
    assert harness.client.fetch_datasheet_url("c2040") == pdf
    call = harness.transport.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == "https://wmsc.lcsc.com/ftps/wm/product/detail?productCode=C2040"
    assert call["url"] == LCSC_DETAIL_URL.format(lcsc_id="C2040")


def test_datasheet_url_is_cached(tmp_path: Path) -> None:
    pdf = "https://www.lcsc.com/datasheet/x.pdf"
    harness = Harness(tmp_path, reply({"result": {"pdfUrl": pdf}}))
    assert harness.client.fetch_datasheet_url("C2040") == pdf
    assert harness.client.fetch_datasheet_url("C2040") == pdf
    assert len(harness.transport.calls) == 1


@pytest.mark.parametrize(
    "outcome",
    [
        reply({"code": 200, "result": None}),
        reply({"result": {}}),
        reply({"result": {"pdfUrl": None}}),
        reply({"result": {"pdfUrl": "javascript:alert(1)"}}),
        reply({"result": {"pdfUrl": ""}}),
        reply(["unexpected"]),
        raw_reply(b"<html>not json</html>"),
        raw_reply(b"", 500),
        raw_reply(b"", 404),
        HttpResponse(403, b"Forbidden", {}),
        HttpResponse(429, b"", {}),
        TransientNetworkError("timed out"),
        NetworkError("TLS error"),
    ],
)
def test_datasheet_is_best_effort_and_never_raises(tmp_path: Path, outcome: Any) -> None:
    # Script enough copies for the retries triggered by 5xx / transient failures.
    harness = Harness(tmp_path, outcome, outcome, outcome)
    assert harness.client.fetch_datasheet_url("C2040") == ""


def test_datasheet_for_an_invalid_id_is_empty_without_network(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    assert harness.client.fetch_datasheet_url("nonsense") == ""
    assert harness.transport.calls == []


def test_datasheet_403_blocks_only_that_host(tmp_path: Path) -> None:
    harness = Harness(tmp_path, FORBIDDEN, search_reply(), doc_reply())
    assert harness.client.fetch_datasheet_url("C2040") == ""
    assert harness.guard.remaining("wmsc.lcsc.com") > 0
    assert harness.guard.remaining("easyeda.com") == 0.0
    assert harness.client.fetch_datasheet_url("C2040") == ""  # fail fast, no new request
    assert len(harness.transport.calls) == 1
    assert harness.client.fetch_part("C2040") == make_document()  # easyeda.com unaffected
