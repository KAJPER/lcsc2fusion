"""Offline tests for search_parts (fake transport, tmp cache, no real waiting)."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from lcsc2fusion.client.cache import DiskCache
from lcsc2fusion.client.easyeda import EasyEdaClient
from lcsc2fusion.client.http import HttpResponse, TransientNetworkError
from lcsc2fusion.client.ratelimit import BlockGuard, RateLimiter
from lcsc2fusion.client.search import JLCPCB_SEARCH_URL, SEARCH_TTL_S, search_parts
from lcsc2fusion.errors import NetworkError, ParseError, PartNotFoundError, RateLimitedError
from lcsc2fusion.model import SearchResult

# Test doubles


class FakeTransport:
    def __init__(self, *outcomes: HttpResponse | Exception) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def queue(self, *outcomes: HttpResponse | Exception) -> None:
        self._outcomes.extend(outcomes)

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
        cache_clock: Callable[[], float] = time.time,
    ) -> None:
        self.transport = FakeTransport(*outcomes)
        self.limiter = CountingLimiter()
        self.cache = DiskCache(tmp_path / "cache", 7 * 24 * 3600.0, clock=cache_clock)
        self.client = EasyEdaClient(
            self.transport,
            self.cache,
            self.limiter,
            guard=BlockGuard(300.0),
            sleep=lambda _seconds: None,
        )


def reply(payload: Any, status: int = 200) -> HttpResponse:
    return HttpResponse(status, json.dumps(payload).encode("utf-8"), {})


def item(
    code: str = "C25804",
    *,
    model: str = "0603WAF1002T5E",
    brand: str = "UNI-ROYAL(厚声)",
    package: str = "0603",
    describe: str = "10kΩ ±1% 100mW",
    manual: str | None = "https://datasheet.lcsc.com/x.pdf",
    library: str = "base",
    stock: int | None = 123456,
    image: str = "",
) -> dict[str, Any]:
    return {
        "componentCode": code,
        "componentModelEn": model,
        "componentBrandEn": brand,
        "componentSpecificationEn": package,
        "describe": describe,
        "dataManualUrl": manual,
        "componentLibraryType": library,
        "stockCount": stock,
        "componentImageUrl": image,
    }


def jlc_reply(*items: dict[str, Any]) -> HttpResponse:
    return reply(
        {"code": 200, "data": {"componentPageInfo": {"total": len(items), "list": list(items)}}}
    )


# Phrase search


def test_phrase_search_posts_the_documented_request(tmp_path: Path) -> None:
    harness = Harness(tmp_path, jlc_reply(item()))
    search_parts("100nF 0603", 15, harness.client)
    (call,) = harness.transport.calls
    assert call["method"] == "POST"
    assert call["url"] == (
        "https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList"
    )
    assert call["url"] == JLCPCB_SEARCH_URL
    assert call["headers"] == {"Content-Type": "application/json"}
    assert json.loads(call["data"]) == {"keyword": "100nF 0603", "currentPage": 1, "pageSize": 15}


def test_phrase_is_trimmed_and_whitespace_collapsed(tmp_path: Path) -> None:
    harness = Harness(tmp_path, jlc_reply(item()))
    search_parts("  100nF \t 0603\n", 20, harness.client)
    assert json.loads(harness.transport.calls[0]["data"])["keyword"] == "100nF 0603"


def test_phrase_search_maps_fields_to_search_result(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        jlc_reply(
            item("C25804"),
            item(
                "C1525",
                model="CL05B104KO5NNNC",
                brand="Samsung Electro-Mechanics",
                package="0402",
                describe="16V 100nF X7R",
                manual=None,
                library="expand",
                stock=None,
                image="https://assets.example.com/c1525.jpg",
            ),
        ),
    )
    results = search_parts("resistor", 20, harness.client)
    assert results == [
        SearchResult(
            lcsc_id="C25804",
            mpn="0603WAF1002T5E",
            manufacturer="UNI-ROYAL(厚声)",
            package="0603",
            description="10kΩ ±1% 100mW",
            datasheet_url="https://datasheet.lcsc.com/x.pdf",
            library_type="Basic",
            stock=123456,
        ),
        SearchResult(
            lcsc_id="C1525",
            mpn="CL05B104KO5NNNC",
            manufacturer="Samsung Electro-Mechanics",
            package="0402",
            description="16V 100nF X7R",
            datasheet_url="",
            image_url="https://assets.example.com/c1525.jpg",
            library_type="Extended",
            stock=None,
        ),
    ]


def test_jlcpcb_assembly_and_non_lcsc_codes_are_dropped(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        jlc_reply(
            item("C9900012345", brand="JLCPCB Assembly"),
            item("C11111", brand="jlcpcb assembly"),
            item("C22222", brand=" JLCPCB Assembly "),
            item("X123", brand="Acme"),
            item("", brand="Acme"),
            item("C12a", brand="Acme"),
            {"componentCode": None},
            "garbage",
            item("C33333", brand="Acme"),
        ),
    )
    results = search_parts("anything", 20, harness.client)
    assert [r.lcsc_id for r in results] == ["C33333"]


def test_duplicate_codes_are_collapsed(tmp_path: Path) -> None:
    harness = Harness(tmp_path, jlc_reply(item("C1"), item("C1"), item("C2")))
    assert [r.lcsc_id for r in search_parts("dup", 20, harness.client)] == ["C1", "C2"]


def test_limit_caps_the_number_of_results_and_the_page_size(tmp_path: Path) -> None:
    items = [item(f"C{n}") for n in range(1, 10)]
    harness = Harness(tmp_path, jlc_reply(*items))
    results = search_parts("many", 3, harness.client)
    assert [r.lcsc_id for r in results] == ["C1", "C2", "C3"]
    assert json.loads(harness.transport.calls[0]["data"])["pageSize"] == 3


def test_page_size_is_clamped(tmp_path: Path) -> None:
    harness = Harness(tmp_path, jlc_reply())
    search_parts("many", 5000, harness.client)
    assert json.loads(harness.transport.calls[0]["data"])["pageSize"] == 100


@pytest.mark.parametrize("query", ["", "   ", "\n\t"])
def test_blank_query_returns_empty_list_without_network(tmp_path: Path, query: str) -> None:
    harness = Harness(tmp_path)
    assert search_parts(query, 20, harness.client) == []
    assert harness.transport.calls == []


@pytest.mark.parametrize("limit", [0, -1])
def test_non_positive_limit_returns_empty_list_without_network(tmp_path: Path, limit: int) -> None:
    harness = Harness(tmp_path)
    assert search_parts("100nF", limit, harness.client) == []
    assert harness.transport.calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {"code": 200, "data": {"componentPageInfo": None}},
        {"code": 200, "data": {"componentPageInfo": {"total": 0, "list": None}}},
        {"code": 200, "data": {"componentPageInfo": {"total": 0, "list": []}}},
        {"code": 200, "data": {}},
    ],
)
def test_no_hits_variants_return_empty_list(tmp_path: Path, payload: Any) -> None:
    harness = Harness(tmp_path, reply(payload))
    assert search_parts("zzzz", 20, harness.client) == []


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"code": 500, "message": "boom", "data": None},
        {"data": []},
        {"data": {"componentPageInfo": 3}},
        {"data": {"componentPageInfo": {"list": {}}}},
    ],
)
def test_unexpected_shapes_raise_parse_error(tmp_path: Path, payload: Any) -> None:
    harness = Harness(tmp_path, reply(payload))
    with pytest.raises(ParseError):
        search_parts("zzzz", 20, harness.client)
    assert list((tmp_path / "cache").glob("jlcpcb-search-*")) == []  # bad shapes are never cached


def test_non_json_response_raises_parse_error(tmp_path: Path) -> None:
    harness = Harness(tmp_path, HttpResponse(200, b"<html>maintenance</html>", {}))
    with pytest.raises(ParseError, match="not valid JSON"):
        search_parts("zzzz", 20, harness.client)


def test_http_error_status_raises_network_error(tmp_path: Path) -> None:
    harness = Harness(tmp_path, HttpResponse(404, b"", {}))
    with pytest.raises(NetworkError, match="HTTP status 404"):
        search_parts("zzzz", 20, harness.client)


def test_403_raises_rate_limited_and_then_fails_fast(tmp_path: Path) -> None:
    harness = Harness(tmp_path, HttpResponse(403, b"blocked", {}))
    with pytest.raises(RateLimitedError):
        search_parts("zzzz", 20, harness.client)
    with pytest.raises(RateLimitedError):
        search_parts("other phrase", 20, harness.client)
    assert len(harness.transport.calls) == 1


def test_5xx_is_retried_then_succeeds(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path, HttpResponse(503, b"", {}), TransientNetworkError("timed out"), jlc_reply(item())
    )
    assert [r.lcsc_id for r in search_parts("resistor", 20, harness.client)] == ["C25804"]
    assert len(harness.transport.calls) == 3


# Caching and shared limiter


def test_identical_searches_are_served_from_cache(tmp_path: Path) -> None:
    harness = Harness(tmp_path, jlc_reply(item()))
    first = search_parts("100nF 0603", 20, harness.client)
    second = search_parts("  100NF   0603 ", 20, harness.client)
    assert second == first
    assert len(harness.transport.calls) == 1


def test_different_limit_or_query_is_a_different_cache_entry(tmp_path: Path) -> None:
    harness = Harness(tmp_path, jlc_reply(item()), jlc_reply(item()), jlc_reply(item()))
    search_parts("100nF", 20, harness.client)
    search_parts("100nF", 10, harness.client)
    search_parts("10nF", 20, harness.client)
    assert len(harness.transport.calls) == 3


def test_search_cache_expires_after_one_hour(tmp_path: Path) -> None:
    offset = {"s": 0.0}
    harness = Harness(
        tmp_path,
        jlc_reply(item()),
        jlc_reply(item()),
        cache_clock=lambda: time.time() + offset["s"],
    )
    search_parts("100nF", 20, harness.client)
    offset["s"] = SEARCH_TTL_S - 60
    search_parts("100nF", 20, harness.client)
    assert len(harness.transport.calls) == 1
    offset["s"] = SEARCH_TTL_S + 60
    search_parts("100nF", 20, harness.client)
    assert len(harness.transport.calls) == 2
    assert SEARCH_TTL_S == 3600.0


def test_corrupt_search_cache_entry_is_replaced(tmp_path: Path) -> None:
    harness = Harness(tmp_path, jlc_reply(item()))
    search_parts("100nF", 20, harness.client)
    entry = next((tmp_path / "cache").glob("jlcpcb-search-*"))
    entry.write_bytes(b"\xff\xfe garbage")
    harness.transport.queue(jlc_reply(item("C2")))
    assert [r.lcsc_id for r in search_parts("100nF", 20, harness.client)] == ["C2"]


def test_searches_use_the_shared_rate_limiter(tmp_path: Path) -> None:
    harness = Harness(tmp_path, jlc_reply(item()), jlc_reply(item()))
    search_parts("a", 20, harness.client)
    search_parts("b", 20, harness.client)
    assert harness.limiter.waits == 2


# Id queries


def make_document(number: str = "C25804") -> dict[str, Any]:
    return {
        "success": True,
        "code": 0,
        "result": {
            "uuid": "0123456789abcdef0123456789abcdef",
            "title": "TITLE-MPN",
            "description": "10kΩ ±1% tiny test",
            "lcsc": {"number": number, "stock": 4321},
            "szlcsc": {"image": "https://assets.example.com/96x96/front.jpg"},
            "dataStr": {
                "head": {
                    "c_para": {
                        "package": "R0603",
                        "Manufacturer": "UNI-ROYAL(厚声)",
                        "Manufacturer Part": "0603WAF1002T5E",
                        "JLCPCB Part Class": "Basic Part",
                    }
                }
            },
            "packageDetail": {"dataStr": {"head": {"c_para": {"package": "R0603-FP"}}}},
        },
    }


def id_harness(tmp_path: Path, document: dict[str, Any] | None = None) -> Harness:
    search = reply({"success": True, "result": [{"uuid": "u1", "number": "C25804"}]})
    return Harness(tmp_path, search, reply(document if document is not None else make_document()))


@pytest.mark.parametrize("query", ["C25804", "c25804", "  c25804 "])
def test_id_query_returns_exactly_one_result_from_the_document(tmp_path: Path, query: str) -> None:
    harness = id_harness(tmp_path)
    results = search_parts(query, 20, harness.client)
    assert results == [
        SearchResult(
            lcsc_id="C25804",
            mpn="0603WAF1002T5E",
            manufacturer="UNI-ROYAL(厚声)",
            package="R0603",
            description="10kΩ ±1% tiny test",
            image_url="https://assets.example.com/96x96/front.jpg",
            library_type="Basic",
            stock=4321,
        )
    ]
    assert all("jlcpcb.com" not in call["url"] for call in harness.transport.calls)


def test_id_query_ignores_the_limit(tmp_path: Path) -> None:
    assert len(search_parts("C25804", 1, id_harness(tmp_path).client)) == 1


def test_id_query_falls_back_to_title_and_package_detail(tmp_path: Path) -> None:
    document = make_document()
    para = document["result"]["dataStr"]["head"]["c_para"]
    del para["Manufacturer Part"], para["package"], para["JLCPCB Part Class"]
    result = search_parts("C25804", 20, id_harness(tmp_path, document).client)[0]
    assert result.mpn == "TITLE-MPN"
    assert result.package == "R0603-FP"
    assert result.library_type == ""


def test_id_query_survives_a_sparse_document(tmp_path: Path) -> None:
    sparse = {"success": True, "result": {"title": "X"}}
    result = search_parts("c25804", 20, id_harness(tmp_path, sparse).client)[0]
    assert result == SearchResult(
        lcsc_id="C25804", mpn="X", manufacturer="", package="", description=""
    )


def test_id_query_extended_part_class(tmp_path: Path) -> None:
    document = make_document()
    document["result"]["dataStr"]["head"]["c_para"]["JLCPCB Part Class"] = "Extended Part"
    result = search_parts("C25804", 20, id_harness(tmp_path, document).client)[0]
    assert result.library_type == "Extended"


def test_id_query_for_unknown_part_raises_part_not_found(tmp_path: Path) -> None:
    empty = reply({"success": True, "result": []})
    missing = reply({"success": False, "code": 404})
    harness = Harness(tmp_path, empty, missing)
    with pytest.raises(PartNotFoundError, match="C25804"):
        search_parts("C25804", 20, harness.client)


def test_id_query_shares_the_part_cache(tmp_path: Path) -> None:
    harness = id_harness(tmp_path)
    search_parts("C25804", 20, harness.client)
    assert harness.client.fetch_part("C25804")["result"]["title"] == "TITLE-MPN"
    search_parts("C25804", 20, harness.client)
    assert len(harness.transport.calls) == 2
