"""Retry with exponential backoff for transient network failures."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

from lcsc2fusion.client.http import TransientNetworkError
from lcsc2fusion.errors import ParseError, PartNotFoundError, RateLimitedError

T = TypeVar("T")

# Retrying these never helps (or makes things worse), even if a caller's `retry_on` covers them.
NEVER_RETRY: tuple[type[Exception], ...] = (RateLimitedError, PartNotFoundError, ParseError)


def retry_call(
    func: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay_s: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    retry_on: tuple[type[Exception], ...] = (TransientNetworkError,),
) -> T:
    """Call `func` up to `attempts` times, sleeping base * 2**n seconds between failures.

    Only exceptions matching `retry_on` are retried, and never `RateLimitedError` (a 403/429
    needs a long pause, handled by `BlockGuard`), `PartNotFoundError` or `ParseError`.
    The last exception is re-raised once the attempts are used up.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    attempt = 1
    while True:
        try:
            return func()
        except NEVER_RETRY:
            raise
        except retry_on:
            if attempt >= attempts:
                raise
        sleep(base_delay_s * 2 ** (attempt - 1))
        attempt += 1
