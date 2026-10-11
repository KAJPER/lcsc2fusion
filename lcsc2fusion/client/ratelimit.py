"""Client side politeness: a minimum interval between requests and a fail fast block guard."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from lcsc2fusion.errors import RateLimitedError

DEFAULT_COOLDOWN_S = 300.0


class RateLimiter:
    """Enforces a minimum interval between consecutive `wait()` returns (concurrency 1).

    Thread safe. The lock is held while sleeping, so concurrent callers are serialised and
    released one by one, each at least `min_interval_s` after the previous one.
    """

    def __init__(
        self,
        min_interval_s: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if min_interval_s < 0:
            raise ValueError("min_interval_s must not be negative")
        self._min_interval_s = min_interval_s
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._ready_at: float | None = None

    @property
    def min_interval_s(self) -> float:
        return self._min_interval_s

    def wait(self) -> float:
        """Block until a request may be sent. Returns the number of seconds slept."""
        with self._lock:
            now = self._clock()
            slept = 0.0
            start = now
            if self._ready_at is not None and now < self._ready_at:
                slept = self._ready_at - now
                self._sleep(slept)
                start = self._ready_at
            self._ready_at = start + self._min_interval_s
            return slept


class BlockGuard:
    """Remembers hosts that blocked us and makes further requests to them fail fast.

    CloudFront blocks last 2.5 to 5 minutes, and hammering a blocked host only prolongs the
    problem, so after `trip(host)` every `check(host)` raises until the cooldown has passed.
    """

    def __init__(
        self,
        cooldown_s: float = DEFAULT_COOLDOWN_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if cooldown_s <= 0:
            raise ValueError("cooldown_s must be positive")
        self._cooldown_s = cooldown_s
        self._clock = clock
        self._lock = threading.Lock()
        self._blocked_until: dict[str, float] = {}

    @property
    def cooldown_s(self) -> float:
        return self._cooldown_s

    def trip(self, host: str, cooldown_s: float | None = None) -> None:
        """Record that `host` blocked us; requests to it fail fast for the cooldown."""
        duration = self._cooldown_s if cooldown_s is None else cooldown_s
        with self._lock:
            self._blocked_until[host.lower()] = self._clock() + duration

    def remaining(self, host: str) -> float:
        """Seconds until `host` may be contacted again (0.0 when it is not blocked)."""
        key = host.lower()
        with self._lock:
            until = self._blocked_until.get(key)
            if until is None:
                return 0.0
            left = until - self._clock()
            if left <= 0:
                del self._blocked_until[key]
                return 0.0
            return left

    def check(self, host: str) -> None:
        """Raise `RateLimitedError` with the remaining wait while `host` is in cooldown."""
        left = self.remaining(host)
        if left > 0:
            raise RateLimitedError(
                f"Requests to {host} are paused after it blocked us. "
                f"Try again in about {left:.0f} s.",
                retry_after_s=left,
            )
