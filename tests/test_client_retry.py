"""Offline tests for retry_call (fake sleep, no real waiting)."""

from __future__ import annotations

import pytest

from lcsc2fusion.client.http import TransientNetworkError
from lcsc2fusion.client.retry import retry_call
from lcsc2fusion.errors import NetworkError, ParseError, PartNotFoundError, RateLimitedError


class Flaky:
    """Fails with the given exceptions in order, then returns 'ok'."""

    def __init__(self, *failures: Exception) -> None:
        self._failures = list(failures)
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        if self._failures:
            raise self._failures.pop(0)
        return "ok"


def test_returns_immediately_on_success() -> None:
    sleeps: list[float] = []
    flaky = Flaky()
    assert retry_call(flaky, sleep=sleeps.append) == "ok"
    assert (flaky.calls, sleeps) == (1, [])


def test_retries_transient_errors_with_exponential_backoff() -> None:
    sleeps: list[float] = []
    flaky = Flaky(TransientNetworkError("t1"), TransientNetworkError("t2"))
    assert retry_call(flaky, attempts=3, base_delay_s=1.0, sleep=sleeps.append) == "ok"
    assert flaky.calls == 3
    assert sleeps == [1.0, 2.0]


def test_backoff_scales_with_base_delay() -> None:
    sleeps: list[float] = []
    flaky = Flaky(*[TransientNetworkError("t")] * 3)
    retry_call(flaky, attempts=4, base_delay_s=0.5, sleep=sleeps.append)
    assert sleeps == [0.5, 1.0, 2.0]


def test_reraises_last_error_when_attempts_are_exhausted() -> None:
    sleeps: list[float] = []
    flaky = Flaky(*[TransientNetworkError(f"fail {i}") for i in range(5)])
    with pytest.raises(TransientNetworkError, match="fail 2"):
        retry_call(flaky, attempts=3, sleep=sleeps.append)
    assert flaky.calls == 3
    assert sleeps == [1.0, 2.0]  # no sleep after the final failure


@pytest.mark.parametrize(
    "error",
    [
        RateLimitedError("blocked"),
        PartNotFoundError("missing"),
        ParseError("garbled"),
        NetworkError("plain network error"),
        ValueError("unrelated"),
    ],
)
def test_non_transient_errors_are_not_retried(error: Exception) -> None:
    sleeps: list[float] = []
    flaky = Flaky(error)
    with pytest.raises(type(error)):
        retry_call(flaky, sleep=sleeps.append)
    assert (flaky.calls, sleeps) == (1, [])


def test_rate_limited_is_never_retried_even_if_the_caller_asks_for_network_errors() -> None:
    flaky = Flaky(RateLimitedError("blocked"))
    with pytest.raises(RateLimitedError):
        retry_call(flaky, retry_on=(NetworkError,), sleep=lambda _s: None)
    assert flaky.calls == 1


def test_custom_retry_on_covers_plain_network_errors() -> None:
    flaky = Flaky(NetworkError("flaky"))
    assert retry_call(flaky, retry_on=(NetworkError,), sleep=lambda _s: None) == "ok"
    assert flaky.calls == 2


def test_single_attempt_means_no_retry() -> None:
    flaky = Flaky(TransientNetworkError("t"))
    with pytest.raises(TransientNetworkError):
        retry_call(flaky, attempts=1, sleep=lambda _s: None)
    assert flaky.calls == 1


def test_zero_attempts_is_rejected() -> None:
    with pytest.raises(ValueError):
        retry_call(lambda: 1, attempts=0)
