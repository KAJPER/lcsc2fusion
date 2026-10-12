"""EasyEDA/LCSC part download.

There is no official public API, so everything here is defensive: one request at a time with a
minimum interval, an on-disk cache, fail fast after a block, and clear errors.

Default path (D-003): `searchByNumbers` resolves the LCSC id to the symbol uuid, then
`/api/components/{uuid}` returns the full document (symbol plus `packageDetail`). The rate
limited `/api/products/{id}/components` endpoint is used only as a fallback.

3D models (stage 3, opt-in): `fetch_model_step(uuid)` downloads the STEP file of a footprint from
the separate host modules.easyeda.com. It goes through the same limiter and retry policy, but has
its own per host block guard entry and its own, longer cache lifetime.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from lcsc2fusion.client.cache import DiskCache, default_cache_dir
from lcsc2fusion.client.http import (
    DEFAULT_TIMEOUT_S,
    HttpResponse,
    TransientNetworkError,
    Transport,
    UrllibTransport,
)
from lcsc2fusion.client.ratelimit import BlockGuard, RateLimiter
from lcsc2fusion.client.retry import retry_call
from lcsc2fusion.errors import (
    LcscError,
    ModelError,
    NetworkError,
    ParseError,
    PartNotFoundError,
    RateLimitedError,
)
from lcsc2fusion.step import read_step

_log = logging.getLogger(__name__)

EASYEDA_BASE_URL = "https://easyeda.com"
SEARCH_BY_NUMBERS_URL = f"{EASYEDA_BASE_URL}/api/components/searchByNumbers"
COMPONENT_URL = f"{EASYEDA_BASE_URL}/api/components/{{uuid}}"
PRODUCT_COMPONENTS_URL = f"{EASYEDA_BASE_URL}/api/products/{{lcsc_id}}/components"
LCSC_DETAIL_URL = "https://wmsc.lcsc.com/ftps/wm/product/detail?productCode={lcsc_id}"

# 3D models live on their own host. The first path segment is a fixed bucket id of the EasyEDA
# model store (found by research, undocumented, so it may change), followed by the model uuid
# (`SVGNODE.attrs.uuid` of the footprint, see parser/model3d.py).
MODEL_BUCKET = "qAxj6KHrDKw4blvCG8QJPs7Y"
MODEL_STEP_URL = f"https://modules.easyeda.com/{MODEL_BUCKET}/{{uuid}}"

FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"

DEFAULT_PART_TTL_S = 7 * 24 * 3600.0
# Models are updated in place under the same uuid, so they expire sooner than "never" but far
# later than part documents: a STEP file is 1 to 3 MB and rarely changes.
DEFAULT_MODEL_TTL_S = 30 * 24 * 3600.0
DEFAULT_MIN_INTERVAL_S = 0.5

_LCSC_ID_RE = re.compile(r"C[0-9]+")
_MODEL_UUID_RE = re.compile(r"[0-9a-f]{32}")
_BLOCK_STATUSES = frozenset({403, 429})
_MAX_RETRY_AFTER_S = 3600.0


def is_lcsc_id(text: str) -> bool:
    """True when `text`, trimmed and upper-cased, looks like an LCSC part number (C2040)."""
    return _LCSC_ID_RE.fullmatch(text.strip().upper()) is not None


def normalize_lcsc_id(text: str) -> str:
    """Return the canonical (upper case) LCSC id or raise `PartNotFoundError`."""
    candidate = text.strip().upper()
    if _LCSC_ID_RE.fullmatch(candidate) is None:
        raise PartNotFoundError(
            f"{text!r} is not a valid LCSC part number. Expected the letter C followed by "
            "digits, for example C2040."
        )
    return candidate


class EasyEdaClient:
    """Downloads EasyEDA part documents with caching, rate limiting, retry and block detection.

    `transport`, `cache`, `limiter` and `guard` are injectable so tests run fully offline.
    The same instance (and so the same limiter, guard and cache) should be shared with
    `search_parts`.
    """

    def __init__(
        self,
        transport: Transport | None = None,
        cache: DiskCache | None = None,
        limiter: RateLimiter | None = None,
        ttl_s: float = DEFAULT_PART_TTL_S,
        *,
        guard: BlockGuard | None = None,
        sleep: Callable[[float], None] = time.sleep,
        retry_attempts: int = 3,
        retry_base_delay_s: float = 1.0,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        model_ttl_s: float = DEFAULT_MODEL_TTL_S,
    ) -> None:
        self._transport: Transport = transport if transport is not None else UrllibTransport()
        self._cache = cache if cache is not None else DiskCache(default_cache_dir(), ttl_s)
        self._limiter = limiter if limiter is not None else RateLimiter(DEFAULT_MIN_INTERVAL_S)
        self._guard = guard if guard is not None else BlockGuard()
        self._ttl_s = ttl_s
        self._model_ttl_s = model_ttl_s
        self._sleep = sleep
        self._retry_attempts = retry_attempts
        self._retry_base_delay_s = retry_base_delay_s
        self._timeout_s = timeout_s

    @property
    def cache(self) -> DiskCache:
        return self._cache

    @property
    def limiter(self) -> RateLimiter:
        return self._limiter

    @property
    def guard(self) -> BlockGuard:
        return self._guard

    # Low level requests (shared with search.py)

    def request(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        """Send one request through the block guard, rate limiter and retry policy.

        Raises `RateLimitedError` on HTTP 403/429 (and then fails fast for that host until the
        cooldown passes) and `TransientNetworkError` on timeouts and HTTP 5xx once the retries
        are used up. Any other status is returned for the caller to interpret.
        """
        return retry_call(
            lambda: self._attempt(method, url, data, headers),
            attempts=self._retry_attempts,
            base_delay_s=self._retry_base_delay_s,
            sleep=self._sleep,
        )

    def request_json(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """Like `request`, but demands a 2xx status and returns the parsed JSON body."""
        response = self.request(method, url, data=data, headers=headers)
        _require_ok(response, url)
        return parse_json(response, url)

    def _attempt(
        self,
        method: str,
        url: str,
        data: bytes | None,
        headers: Mapping[str, str] | None,
    ) -> HttpResponse:
        host = urlsplit(url).hostname or ""
        self._guard.check(host)
        self._limiter.wait()
        response = self._transport.request(
            method, url, data=data, headers=headers, timeout=self._timeout_s
        )
        if response.status in _BLOCK_STATUSES:
            pause = _retry_after_s(response, self._guard.cooldown_s)
            self._guard.trip(host, pause)
            raise RateLimitedError(
                f"{host} blocked the request (HTTP {response.status}). Such blocks usually "
                f"clear on their own; try again in about {pause:.0f} s.",
                retry_after_s=pause,
            )
        if response.status >= 500:
            raise TransientNetworkError(f"{host} returned HTTP {response.status}")
        return response

    # Parts

    def fetch_part(self, lcsc_id: str) -> dict[str, Any]:
        """Return the whole top-level EasyEDA response (`{"success": ..., "result": {...}}`).

        Raises `PartNotFoundError` for malformed or unknown ids and for parts without EDA data,
        `RateLimitedError` when blocked, `NetworkError` on transport failures and `ParseError`
        when the server answers with something that is not JSON.
        """
        part_id = normalize_lcsc_id(lcsc_id)
        key = _part_key(part_id)
        cached = self._read_cached_document(key)
        if cached is not None:
            return cached
        document, raw = self._download_document(part_id)
        self._cache.put(key, raw)
        return document

    def fetch_datasheet_url(self, lcsc_id: str) -> str:
        """Best effort datasheet PDF URL from the LCSC detail endpoint; "" on any failure.

        That host rejects some clients with 403. We do not disguise ourselves, we just give up.
        """
        try:
            part_id = normalize_lcsc_id(lcsc_id)
        except PartNotFoundError:
            return ""
        key = f"datasheet-{part_id}"
        cached = self._cache.get(key, ttl_s=self._ttl_s)
        if cached:
            try:
                return cached.decode("utf-8")
            except UnicodeDecodeError:
                self._cache.discard(key)
        try:
            payload = self.request_json("GET", LCSC_DETAIL_URL.format(lcsc_id=part_id))
        except LcscError as exc:
            _log.debug("No datasheet URL for %s: %s", part_id, exc)
            return ""
        url = _extract_pdf_url(payload)
        if url:
            self._cache.put(key, url.encode("utf-8"))
        return url

    def fetch_model_step(self, uuid: str) -> bytes:
        """Return the STEP file of one EasyEDA 3D model, byte for byte as served.

        `uuid` is the model id from the footprint (32 lowercase hex digits). The bytes are
        cached under `step-{uuid}` for `model_ttl_s`; a cached entry that no longer validates is
        discarded and downloaded once more. Invalid downloads are never cached.

        Raises `ModelError` for a malformed uuid, HTTP 404, and an answer that is not a complete
        millimetre STEP file (an HTML page, a truncated body), `RateLimitedError` on HTTP 403/429
        and `NetworkError` on transport failures and other unexpected statuses.
        """
        model_id = _validated_model_uuid(uuid)
        key = _model_key(model_id)
        cached = self._cache.get(key, ttl_s=self._model_ttl_s)
        if cached is not None:
            try:
                read_step(cached)
            except ModelError:
                _log.debug("Dropping unusable cache entry %s", key)
                self._cache.discard(key)
            else:
                return cached
        data = self._download_model(model_id)
        self._cache.put(key, data)
        return data

    def _download_model(self, model_id: str) -> bytes:
        url = MODEL_STEP_URL.format(uuid=model_id)
        response = self.request("GET", url)
        if response.status == 404:
            raise ModelError(f"3D model {model_id} not found on EasyEDA (HTTP 404).")
        _require_ok(response, url)
        try:
            read_step(response.body)
        except ModelError as exc:
            raise ModelError(f"EasyEDA returned an unusable 3D model {model_id}: {exc}") from exc
        return response.body

    def _read_cached_document(self, key: str) -> dict[str, Any] | None:
        raw = self._cache.get(key, ttl_s=self._ttl_s)
        if raw is None:
            return None
        try:
            document = _validated_document(_decode_json(raw), "cache")
        except (ValueError, LcscError):
            _log.debug("Dropping unusable cache entry %s", key)
            self._cache.discard(key)
            return None
        return document

    def _download_document(self, part_id: str) -> tuple[dict[str, Any], bytes]:
        uuid = self._lookup_uuid_or_none(part_id)
        if uuid is not None:
            url = COMPONENT_URL.format(uuid=quote(uuid, safe=""))
            try:
                return self._get_document(url, part_id)
            except RateLimitedError:
                raise
            except (NetworkError, ParseError, PartNotFoundError) as exc:
                _log.debug(
                    "Component fetch by uuid failed for %s (%s), using fallback", part_id, exc
                )
        return self._get_document(PRODUCT_COMPONENTS_URL.format(lcsc_id=part_id), part_id)

    def _lookup_uuid_or_none(self, part_id: str) -> str | None:
        """Resolve the symbol uuid; None means "use the fallback endpoint"."""
        try:
            return self._lookup_uuid(part_id)
        except RateLimitedError:
            raise
        except (NetworkError, ParseError) as exc:
            _log.debug("searchByNumbers failed for %s (%s), using fallback", part_id, exc)
            return None

    def _lookup_uuid(self, part_id: str) -> str | None:
        body = urlencode({"numbers": json.dumps([part_id], separators=(",", ":"))}).encode("ascii")
        payload = self.request_json(
            "POST",
            SEARCH_BY_NUMBERS_URL,
            data=body,
            headers={"Content-Type": FORM_CONTENT_TYPE},
        )
        if not isinstance(payload, dict) or payload.get("success") is False:
            return None
        result = payload.get("result")
        if not isinstance(result, list):
            return None
        for item in result:
            if not isinstance(item, dict):
                continue
            uuid = item.get("uuid")
            number = item.get("number")
            if (
                isinstance(uuid, str)
                and uuid
                and (number is None or str(number).upper() == part_id)
            ):
                return uuid
        return None

    def _get_document(self, url: str, part_id: str) -> tuple[dict[str, Any], bytes]:
        response = self.request("GET", url)
        if response.status == 404:
            raise PartNotFoundError(f"EasyEDA has no data for {part_id} (HTTP 404).")
        _require_ok(response, url)
        document = _validated_document(parse_json(response, url), part_id)
        return document, response.body


# Helpers


def _part_key(part_id: str) -> str:
    return f"easyeda-{part_id}"


def _model_key(model_id: str) -> str:
    return f"step-{model_id}"


def _validated_model_uuid(uuid: str) -> str:
    """The uuid unchanged, or `ModelError`. Nothing else may reach the URL."""
    if not isinstance(uuid, str) or _MODEL_UUID_RE.fullmatch(uuid) is None:
        shown = uuid[:40] if isinstance(uuid, str) else type(uuid).__name__
        raise ModelError(
            f"{shown!r} is not a valid 3D model id. Expected 32 lowercase hexadecimal digits."
        )
    return uuid


def _decode_json(raw: bytes) -> Any:
    return json.loads(raw.decode("utf-8-sig"))


def parse_json(response: HttpResponse, url: str) -> Any:
    """Parse a response body as JSON or raise `ParseError` naming the host."""
    host = urlsplit(url).hostname or url
    try:
        return _decode_json(response.body)
    except ValueError as exc:
        raise ParseError(f"{host} returned a response that is not valid JSON: {exc}") from exc


def _require_ok(response: HttpResponse, url: str) -> None:
    if not response.ok:
        host = urlsplit(url).hostname or url
        raise NetworkError(f"{host} returned unexpected HTTP status {response.status}.")


def _validated_document(payload: Any, part_id: str) -> dict[str, Any]:
    """EasyEDA answers HTTP 200 even for unknown parts, so `success`/`result` decide."""
    if not isinstance(payload, dict):
        raise ParseError("EasyEDA returned JSON that is not an object.")
    if payload.get("success") is False or not isinstance(payload.get("result"), dict):
        raise PartNotFoundError(
            f"EasyEDA has no symbol/footprint data for {part_id}. "
            "The part may not exist or may not have EDA data."
        )
    return payload


def _retry_after_s(response: HttpResponse, default_s: float) -> float:
    raw = response.headers.get("retry-after")
    if raw is None:
        return default_s
    try:
        seconds = float(raw)
    except ValueError:
        return default_s
    if seconds <= 0:
        return default_s
    return min(seconds, _MAX_RETRY_AFTER_S)


def _extract_pdf_url(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    result = payload.get("result")
    if not isinstance(result, dict):
        return ""
    url = result.get("pdfUrl")
    if isinstance(url, str) and url.strip().lower().startswith(("http://", "https://")):
        return url.strip()
    return ""
