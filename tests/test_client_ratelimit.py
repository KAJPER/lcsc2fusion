"""Offline tests for RateLimiter and BlockGuard (fake clock and sleep, no real waiting)."""

from __future__ import annotations

import threading

import pytest

from lcsc2fusion.client.ratelimit import BlockGuard, RateLimiter
from lcsc2fusion.errors import NetworkError, RateLimitedError


class FakeTime:
    """A clock whose `sleep` advances it, so limiter logic can be checked deterministically."""

    def __init__(self, now: float = 100.0) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _limiter(interval: float, fake: FakeTime) -> RateLimiter:
    return RateLimiter(interval, clock=fake.clock, sleep=fake.sleep)


def test_first_wait_does_not_sleep() -> None:
    fake = FakeTime()
    assert _limiter(2.0, fake).wait() == 0.0
    assert fake.sleeps == []


def test_second_wait_sleeps_the_remaining_interval() -> None:
    fake = FakeTime()
    limiter = _limiter(2.0, fake)
    limiter.wait()
    fake.now += 0.5
    assert limiter.wait() == pytest.approx(1.5)
    assert fake.sleeps == [pytest.approx(1.5)]


def test_no_sleep_when_the_interval_has_already_passed() -> None:
    fake = FakeTime()
    limiter = _limiter(2.0, fake)
    limiter.wait()
    fake.now += 5.0
    limiter.wait()
    assert fake.sleeps == []


def test_back_to_back_waits_are_spaced_by_the_interval() -> None:
    fake = FakeTime()
    limiter = _limiter(1.0, fake)
    starts = []
    for _ in range(4):
        limiter.wait()
        starts.append(fake.now)
    assert starts == [100.0, 101.0, 102.0, 103.0]


def test_zero_interval_never_sleeps() -> None:
    fake = FakeTime()
    limiter = _limiter(0.0, fake)
    for _ in range(5):
        limiter.wait()
    assert fake.sleeps == []


def test_negative_interval_is_rejected() -> None:
    with pytest.raises(ValueError):
        RateLimiter(-1.0)


def test_concurrent_waiters_are_serialised() -> None:
    fake = FakeTime()
    limiter = _limiter(1.0, fake)
    starts: list[float] = []
    start_lock = threading.Lock()

    def worker() -> None:
        limiter.wait()
        with start_lock:
            starts.append(fake.now)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert len(starts) == 6
    ordered = sorted(starts)
    assert ordered == [pytest.approx(100.0 + i) for i in range(6)]


# BlockGuard


def _guard(fake: FakeTime, cooldown: float = 300.0) -> BlockGuard:
    return BlockGuard(cooldown_s=cooldown, clock=fake.clock)


def test_untripped_guard_lets_everything_through() -> None:
    guard = _guard(FakeTime())
    guard.check("easyeda.com")
    assert guard.remaining("easyeda.com") == 0.0


def test_tripped_guard_fails_fast_with_remaining_wait() -> None:
    fake = FakeTime()
    guard = _guard(fake)
    guard.trip("easyeda.com")
    fake.now += 100
    with pytest.raises(RateLimitedError, match=r"easyeda.com") as info:
        guard.check("easyeda.com")
    assert info.value.retry_after_s == pytest.approx(200.0)
    assert isinstance(info.value, NetworkError)


def test_guard_clears_after_cooldown() -> None:
    fake = FakeTime()
    guard = _guard(fake)
    guard.trip("easyeda.com")
    fake.now += 299.9
    with pytest.raises(RateLimitedError):
        guard.check("easyeda.com")
    fake.now += 0.2
    guard.check("easyeda.com")
    assert guard.remaining("easyeda.com") == 0.0


def test_guard_is_per_host_and_case_insensitive() -> None:
    guard = _guard(FakeTime())
    guard.trip("EasyEDA.com")
    guard.check("jlcpcb.com")
    with pytest.raises(RateLimitedError):
        guard.check("easyeda.COM")


def test_custom_trip_duration_overrides_default() -> None:
    fake = FakeTime()
    guard = _guard(fake, cooldown=300)
    guard.trip("easyeda.com", cooldown_s=10)
    fake.now += 11
    guard.check("easyeda.com")


def test_tripping_again_extends_the_block() -> None:
    fake = FakeTime()
    guard = _guard(fake)
    guard.trip("easyeda.com")
    fake.now += 250
    guard.trip("easyeda.com")
    fake.now += 100
    with pytest.raises(RateLimitedError):
        guard.check("easyeda.com")


def test_guard_rejects_non_positive_cooldown() -> None:
    with pytest.raises(ValueError):
        BlockGuard(cooldown_s=0)


def test_default_cooldown_is_five_minutes() -> None:
    assert BlockGuard().cooldown_s == 300.0
