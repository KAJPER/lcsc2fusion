"""Offline tests for EasyEdaClient.fetch_model_step (fake transport, fake clock, tmp cache)."""

from __future__ import annotations

import http.client
import time
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from step_samples import make_box_step

from lcsc2fusion.client import DEFAULT_MODEL_TTL_S, MODEL_STEP_URL
from lcsc2fusion.client.cache import DiskCache
from lcsc2fusion.client.easyeda import EasyEdaClient
from lcsc2fusion.client.http import (
    DEFAULT_USER_AGENT,
    HttpResponse,
    TransientNetworkError,
    UrllibTransport,
)
from lcsc2fusion.client.ratelimit import BlockGuard, RateLimiter
from lcsc2fusion.errors import ModelError, NetworkError, RateLimitedError
from lcsc2fusion.step import read_step

UUID = "6bd5cd867e9542ebae21caaf5d2d4c4d"
OTHER_UUID = "617b05f9bba7410b96c001093d8189e4"
STEP = make_box_step()  # AP214 style box with raw non-ASCII bytes in a solid name
URL = f"https://modules.easyeda.com/qAxj6KHrDKw4blvCG8QJPs7Y/{UUID}"
CACHE_KEY = f"step-{UUID}"
DAY_S = 24 * 3600.0

FORBIDDEN = HttpResponse(403, b"Request blocked", {"server": "CloudFront"})


class FakeTime:
    def __init__(self) -> None:
        self.now = 1000.0

    def clock(self) -> float:
        return self.now


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
        part_ttl_s: float = 3600.0,
        model_ttl_s: float | None = None,
        cache_clock: Callable[[], float] = time.time,
    ) -> None:
        self.time = FakeTime()
        self.retry_sleeps: list[float] = []
        self.transport = FakeTransport(*outcomes)
        self.limiter = CountingLimiter()
        self.guard = BlockGuard(300.0, clock=self.time.clock)
        self.cache = DiskCache(tmp_path / "cache", part_ttl_s, clock=cache_clock)
        options: dict[str, Any] = {}
        if model_ttl_s is not None:
            options["model_ttl_s"] = model_ttl_s
        self.client = EasyEdaClient(
            self.transport,
            self.cache,
            self.limiter,
            part_ttl_s,
            guard=self.guard,
            sleep=self.retry_sleeps.append,
            **options,
        )


def served(body: bytes = STEP, status: int = 200) -> HttpResponse:
    return HttpResponse(status, body, {"content-type": "application/octet-stream"})


# Request


def test_requests_the_model_store_url_with_get_and_no_body(tmp_path: Path) -> None:
    harness = Harness(tmp_path, served())
    harness.client.fetch_model_step(UUID)
    (call,) = harness.transport.calls
    assert call["method"] == "GET"
    assert call["url"] == URL
    assert call["url"] == MODEL_STEP_URL.format(uuid=UUID)
    assert call["data"] is None


def test_returns_the_downloaded_bytes_unchanged(tmp_path: Path) -> None:
    harness = Harness(tmp_path, served())
    data = harness.client.fetch_model_step(UUID)
    assert data == STEP
    assert b"\xb5\xc4\xbe\xb5\xcf\xf2" in data  # GBK like bytes survive
    assert read_step(data).units_mm


def test_every_attempt_goes_through_the_rate_limiter(tmp_path: Path) -> None:
    harness = Harness(tmp_path, served(b"", 503), served())
    harness.client.fetch_model_step(UUID)
    assert harness.limiter.waits == 2


def test_default_transport_sends_the_descriptive_user_agent(tmp_path: Path) -> None:
    """With the real urllib transport (fake opener) the project user agent is on the request."""

    class FakeResponse:
        status = 200
        headers = http.client.HTTPMessage()

        def read(self) -> bytes:
            return STEP

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *exc_info: object) -> None:
            return None

    class FakeOpener:
        def __init__(self) -> None:
            self.requests: list[urllib.request.Request] = []

        def open(
            self, fullurl: urllib.request.Request, data: bytes | None = None, timeout: float = 30.0
        ) -> FakeResponse:
            self.requests.append(fullurl)
            return FakeResponse()

    opener = FakeOpener()
    client = EasyEdaClient(
        UrllibTransport(opener=opener),
        DiskCache(tmp_path / "cache", 3600.0),
        RateLimiter(0.0),
    )
    assert client.fetch_model_step(UUID) == STEP
    (request,) = opener.requests
    assert request.full_url == URL
    assert request.get_method() == "GET"
    assert request.get_header("User-agent") == DEFAULT_USER_AGENT
    assert DEFAULT_USER_AGENT.startswith("lcsc2fusion/")


# uuid validation


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "abc",
        UUID.upper(),
        UUID + "0",
        UUID[:-1],
        UUID[:-1] + "g",
        f"../{UUID}",
        f"{UUID}/../x",
        f"{UUID}?x=1",
        f"{UUID}%2e",
        f" {UUID}",
        f"{UUID}\n",
        "0123456789abcdef0123456789abcde٣",
    ],
)
def test_bad_uuid_raises_model_error_without_network(tmp_path: Path, bad: str) -> None:
    harness = Harness(tmp_path)
    with pytest.raises(ModelError, match="not a valid 3D model id"):
        harness.client.fetch_model_step(bad)
    assert harness.transport.calls == []


def test_non_string_uuid_raises_model_error(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    with pytest.raises(ModelError, match="not a valid 3D model id"):
        harness.client.fetch_model_step(None)  # type: ignore[arg-type]
    assert harness.transport.calls == []


# Cache


def test_second_call_is_a_cache_hit_without_network(tmp_path: Path) -> None:
    harness = Harness(tmp_path, served())
    first = harness.client.fetch_model_step(UUID)
    second = harness.client.fetch_model_step(UUID)
    assert second == first == STEP
    assert len(harness.transport.calls) == 1


def test_bytes_are_cached_under_the_step_key(tmp_path: Path) -> None:
    harness = Harness(tmp_path, served())
    harness.client.fetch_model_step(UUID)
    assert harness.cache.get(CACHE_KEY) == STEP
    assert harness.cache.path_for(CACHE_KEY).read_bytes() == STEP


def test_different_uuids_do_not_share_an_entry(tmp_path: Path) -> None:
    other = make_box_step(size=(2.0, 1.0, 0.7))
    harness = Harness(tmp_path, served(), served(other))
    assert harness.client.fetch_model_step(UUID) == STEP
    assert harness.client.fetch_model_step(OTHER_UUID) == other
    assert harness.client.fetch_model_step(UUID) == STEP
    assert len(harness.transport.calls) == 2


def test_cache_is_shared_between_clients_via_the_directory(tmp_path: Path) -> None:
    Harness(tmp_path, served()).client.fetch_model_step(UUID)
    second = Harness(tmp_path)  # same cache directory, transport has no scripted replies
    assert second.client.fetch_model_step(UUID) == STEP
    assert second.transport.calls == []


def test_default_model_ttl_is_thirty_days(tmp_path: Path) -> None:
    assert DEFAULT_MODEL_TTL_S == 30 * DAY_S
    offset = {"s": 0.0}
    harness = Harness(
        tmp_path,
        served(),
        served(),
        part_ttl_s=7 * DAY_S,
        cache_clock=lambda: time.time() + offset["s"],
    )
    harness.client.fetch_model_step(UUID)
    offset["s"] = 29 * DAY_S  # parts would be long expired here, models are not
    harness.client.fetch_model_step(UUID)
    assert len(harness.transport.calls) == 1
    offset["s"] = 31 * DAY_S
    harness.client.fetch_model_step(UUID)
    assert len(harness.transport.calls) == 2


def test_model_ttl_is_independent_of_the_part_ttl(tmp_path: Path) -> None:
    offset = {"s": 0.0}
    harness = Harness(
        tmp_path,
        served(),
        served(),
        part_ttl_s=1000 * DAY_S,
        model_ttl_s=100.0,
        cache_clock=lambda: time.time() + offset["s"],
    )
    harness.client.fetch_model_step(UUID)
    offset["s"] = 50.0
    harness.client.fetch_model_step(UUID)
    assert len(harness.transport.calls) == 1
    offset["s"] = 500.0
    harness.client.fetch_model_step(UUID)
    assert len(harness.transport.calls) == 2


def test_model_ttl_zero_never_reads_the_cache(tmp_path: Path) -> None:
    harness = Harness(tmp_path, served(), served(), model_ttl_s=0.0)
    harness.client.fetch_model_step(UUID)
    harness.client.fetch_model_step(UUID)
    assert len(harness.transport.calls) == 2


def test_corrupt_cache_entry_is_discarded_and_refetched_once(tmp_path: Path) -> None:
    harness = Harness(tmp_path, served())
    harness.cache.put(CACHE_KEY, b"\x00\xff garbage that is no STEP file")
    assert harness.client.fetch_model_step(UUID) == STEP
    assert len(harness.transport.calls) == 1
    assert harness.cache.get(CACHE_KEY) == STEP  # healed
    assert harness.client.fetch_model_step(UUID) == STEP
    assert len(harness.transport.calls) == 1


@pytest.mark.parametrize(
    "stale",
    [
        STEP[: len(STEP) // 2],  # truncated
        b"<html><body>Access denied</body></html>",
        b"",
        STEP.replace(b".MILLI., .METRE.", b"$, .METRE."),  # wrong unit
    ],
    ids=["truncated", "html", "empty", "metres"],
)
def test_invalid_cache_entries_never_reach_the_caller(tmp_path: Path, stale: bytes) -> None:
    harness = Harness(tmp_path, served())
    harness.cache.put(CACHE_KEY, stale)
    assert harness.client.fetch_model_step(UUID) == STEP
    assert len(harness.transport.calls) == 1


def test_corrupt_cache_entry_with_an_invalid_download_is_dropped_and_raises(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, served(b"<html>nope</html>"))
    harness.cache.put(CACHE_KEY, b"garbage")
    with pytest.raises(ModelError, match="HTML"):
        harness.client.fetch_model_step(UUID)
    assert len(harness.transport.calls) == 1  # refetched once, not in a loop
    assert harness.cache.get(CACHE_KEY) is None


def test_corrupt_cache_entry_is_dropped_even_when_the_refetch_fails(tmp_path: Path) -> None:
    harness = Harness(tmp_path, served(b"", 404))
    harness.cache.put(CACHE_KEY, b"garbage")
    with pytest.raises(ModelError, match="not found"):
        harness.client.fetch_model_step(UUID)
    assert harness.cache.get(CACHE_KEY) is None


def test_cached_model_is_still_served_while_the_host_is_blocked(tmp_path: Path) -> None:
    harness = Harness(tmp_path, served(), FORBIDDEN)
    harness.client.fetch_model_step(UUID)
    with pytest.raises(RateLimitedError):
        harness.client.fetch_model_step(OTHER_UUID)
    assert harness.client.fetch_model_step(UUID) == STEP


# Errors


def test_http_404_is_a_model_error(tmp_path: Path) -> None:
    harness = Harness(tmp_path, served(b"<Error>NoSuchKey</Error>", 404))
    with pytest.raises(ModelError, match=r"3D model .* not found") as info:
        harness.client.fetch_model_step(UUID)
    assert UUID in str(info.value)
    assert not isinstance(info.value, NetworkError)
    assert len(harness.transport.calls) == 1  # a 404 is final, no retries
    assert harness.retry_sleeps == []
    assert harness.cache.get(CACHE_KEY) is None


def test_404_does_not_block_the_host(tmp_path: Path) -> None:
    harness = Harness(tmp_path, served(b"", 404), served())
    with pytest.raises(ModelError):
        harness.client.fetch_model_step(OTHER_UUID)
    assert harness.client.fetch_model_step(UUID) == STEP
    assert harness.guard.remaining("modules.easyeda.com") == 0.0


def test_403_is_rate_limited_and_trips_the_guard(tmp_path: Path) -> None:
    harness = Harness(tmp_path, FORBIDDEN, served())
    with pytest.raises(RateLimitedError) as info:
        harness.client.fetch_model_step(UUID)
    assert info.value.retry_after_s == 300.0
    assert "modules.easyeda.com" in str(info.value)
    assert harness.retry_sleeps == []  # never retried
    assert harness.guard.remaining("modules.easyeda.com") == pytest.approx(300.0)
    assert harness.cache.get(CACHE_KEY) is None

    # Fail fast: the next request does not reach the transport until the cooldown has passed.
    harness.time.now += 100
    with pytest.raises(RateLimitedError) as again:
        harness.client.fetch_model_step(UUID)
    assert again.value.retry_after_s == pytest.approx(200.0)
    assert len(harness.transport.calls) == 1

    harness.time.now += 201
    assert harness.client.fetch_model_step(UUID) == STEP
    assert len(harness.transport.calls) == 2


def test_429_honours_retry_after(tmp_path: Path) -> None:
    harness = Harness(tmp_path, HttpResponse(429, b"slow down", {"retry-after": "42"}))
    with pytest.raises(RateLimitedError) as info:
        harness.client.fetch_model_step(UUID)
    assert info.value.retry_after_s == 42.0
    assert harness.guard.remaining("modules.easyeda.com") == pytest.approx(42.0)


def test_the_block_guard_is_per_host(tmp_path: Path) -> None:
    """A block on the model host leaves easyeda.com alone, and the other way round."""
    harness = Harness(tmp_path, FORBIDDEN)
    with pytest.raises(RateLimitedError):
        harness.client.fetch_model_step(UUID)
    assert harness.guard.remaining("modules.easyeda.com") > 0
    assert harness.guard.remaining("easyeda.com") == 0.0
    harness.guard.check("easyeda.com")  # does not raise

    other = Harness(tmp_path / "second", served())
    other.guard.trip("easyeda.com", 300.0)
    assert other.client.fetch_model_step(UUID) == STEP


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        (b"<!DOCTYPE html><html><body>Not a model</body></html>" + b" " * 2000, "HTML"),
        (b"<html><head></head><body>" + b"x" * 2000 + b"</body></html>", "HTML"),
        (b"", "ISO-10303-21"),
        (b'{"error": "nope"}' + b" " * 2000, "ISO-10303-21"),
        (STEP[: len(STEP) - 40], "END-ISO-10303-21"),
        (STEP[:300], "too small"),
    ],
    ids=["doctype", "html", "empty", "json", "truncated", "tiny"],
)
def test_http_200_with_an_unusable_body_is_a_model_error_and_not_cached(
    tmp_path: Path, body: bytes, fragment: str
) -> None:
    harness = Harness(tmp_path, served(body))
    with pytest.raises(ModelError, match=fragment) as info:
        harness.client.fetch_model_step(UUID)
    assert UUID in str(info.value)
    assert harness.cache.get(CACHE_KEY) is None
    assert harness.retry_sleeps == []


def test_wrong_length_unit_is_a_model_error_and_not_cached(tmp_path: Path) -> None:
    metres = STEP.replace(b".MILLI., .METRE.", b"$, .METRE.")
    harness = Harness(tmp_path, served(metres))
    with pytest.raises(ModelError, match="unit"):
        harness.client.fetch_model_step(UUID)
    assert harness.cache.get(CACHE_KEY) is None


def test_invalid_download_is_not_cached_so_the_next_call_asks_again(tmp_path: Path) -> None:
    harness = Harness(tmp_path, served(STEP[:500]), served())
    with pytest.raises(ModelError):
        harness.client.fetch_model_step(UUID)
    assert harness.client.fetch_model_step(UUID) == STEP
    assert len(harness.transport.calls) == 2


def test_other_statuses_are_network_errors(tmp_path: Path) -> None:
    harness = Harness(tmp_path, served(b"", 400))
    with pytest.raises(NetworkError, match="HTTP status 400") as info:
        harness.client.fetch_model_step(UUID)
    assert not isinstance(info.value, (ModelError, TransientNetworkError))
    assert len(harness.transport.calls) == 1


def test_5xx_is_retried_with_backoff(tmp_path: Path) -> None:
    harness = Harness(tmp_path, served(b"", 503), served(b"", 502), served())
    assert harness.client.fetch_model_step(UUID) == STEP
    assert harness.retry_sleeps == [1.0, 2.0]
    assert len(harness.transport.calls) == 3


def test_persistent_5xx_gives_a_network_error_after_three_attempts(tmp_path: Path) -> None:
    harness = Harness(tmp_path, *[served(b"", 500)] * 3)
    with pytest.raises(NetworkError, match="HTTP 500"):
        harness.client.fetch_model_step(UUID)
    assert len(harness.transport.calls) == 3
    assert harness.cache.get(CACHE_KEY) is None


def test_timeouts_are_retried(tmp_path: Path) -> None:
    timeout = TransientNetworkError("Request to modules.easyeda.com timed out after 30 s")
    harness = Harness(tmp_path, timeout, timeout, served())
    assert harness.client.fetch_model_step(UUID) == STEP
    assert harness.retry_sleeps == [1.0, 2.0]


def test_plain_network_error_is_not_retried(tmp_path: Path) -> None:
    harness = Harness(tmp_path, NetworkError("TLS error"), served())
    with pytest.raises(NetworkError, match="TLS error"):
        harness.client.fetch_model_step(UUID)
    assert len(harness.transport.calls) == 1
