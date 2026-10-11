"""Network layer: HTTP transport, cache, rate limiting, retry, EasyEDA fetch, part search."""

from lcsc2fusion.client.cache import DiskCache, default_cache_dir
from lcsc2fusion.client.easyeda import EasyEdaClient, is_lcsc_id, normalize_lcsc_id
from lcsc2fusion.client.http import (
    HttpResponse,
    TransientNetworkError,
    Transport,
    UrllibTransport,
)
from lcsc2fusion.client.ratelimit import BlockGuard, RateLimiter
from lcsc2fusion.client.retry import retry_call
from lcsc2fusion.client.search import search_parts

__all__ = [
    "BlockGuard",
    "DiskCache",
    "EasyEdaClient",
    "HttpResponse",
    "RateLimiter",
    "TransientNetworkError",
    "Transport",
    "UrllibTransport",
    "default_cache_dir",
    "is_lcsc_id",
    "normalize_lcsc_id",
    "retry_call",
    "search_parts",
]
