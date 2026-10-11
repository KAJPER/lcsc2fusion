"""Part search: LCSC ids go straight to EasyEDA, free text goes to the JLCPCB parts search."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from lcsc2fusion.client.cache import DiskCache
from lcsc2fusion.client.easyeda import (
    EasyEdaClient,
    is_lcsc_id,
    normalize_lcsc_id,
    parse_json,
)
from lcsc2fusion.errors import NetworkError, ParseError
from lcsc2fusion.model import SearchResult

JLCPCB_SEARCH_URL = (
    "https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList"
)
SEARCH_TTL_S = 3600.0
MAX_LIMIT = 100

_LCSC_CODE_RE = re.compile(r"C[0-9]+")
_JLCPCB_ASSEMBLY_BRAND = "jlcpcb assembly"
_LIBRARY_TYPES = {"base": "Basic", "expand": "Extended"}
_PART_CLASS_TYPES = {"basic part": "Basic", "extended part": "Extended"}


def search_parts(
    query: str,
    limit: int = 20,
    client: EasyEdaClient | None = None,
) -> list[SearchResult]:
    """Find parts by LCSC id or by free text.

    An id (for example `c2040`) returns exactly one result built from the EasyEDA document, so a
    UI can treat ids and phrases alike. For ids, `PartNotFoundError` is raised when the part has
    no EasyEDA data (the message explains why). A phrase returns up to `limit` JLCPCB hits that
    have an EasyEDA counterpart, and an empty or blank query returns an empty list.

    Pass the same `client` used for downloads so the rate limiter, block guard and cache are shared.
    """
    phrase = " ".join(query.split())
    if not phrase or limit < 1:
        return []
    active = client if client is not None else EasyEdaClient()
    if is_lcsc_id(phrase):
        return [_result_from_document(active.fetch_part(phrase), normalize_lcsc_id(phrase))]
    return _search_jlcpcb(active, phrase, min(limit, MAX_LIMIT))


# Phrase search


def _search_jlcpcb(client: EasyEdaClient, phrase: str, limit: int) -> list[SearchResult]:
    key = _search_key(phrase, limit)
    payload = _read_cached_search(client.cache, key)
    if payload is None:
        body = json.dumps(
            {"keyword": phrase, "currentPage": 1, "pageSize": limit},
            ensure_ascii=False,
        ).encode("utf-8")
        response = client.request(
            "POST",
            JLCPCB_SEARCH_URL,
            data=body,
            headers={"Content-Type": "application/json"},
        )
        if not response.ok:
            raise NetworkError(f"jlcpcb.com returned unexpected HTTP status {response.status}.")
        payload = parse_json(response, JLCPCB_SEARCH_URL)
        items = _extract_items(payload)  # validates the shape before anything is cached
        client.cache.put(key, response.body)
    else:
        items = _extract_items(payload)
    return _map_items(items, limit)


def _search_key(phrase: str, limit: int) -> str:
    digest = hashlib.sha256(phrase.casefold().encode("utf-8")).hexdigest()[:16]
    return f"jlcpcb-search-{limit}-{digest}"


def _read_cached_search(cache: DiskCache, key: str) -> Any | None:
    raw = cache.get(key, ttl_s=SEARCH_TTL_S)
    if raw is None:
        return None
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
        _extract_items(payload)
    except (ValueError, ParseError):
        cache.discard(key)
        return None
    return payload


def _extract_items(payload: Any) -> list[Any]:
    if not isinstance(payload, dict):
        raise ParseError("jlcpcb.com returned JSON that is not an object.")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ParseError(
            f"jlcpcb.com search response has no data (code {payload.get('code')!r}, "
            f"message {payload.get('message')!r})."
        )
    info = data.get("componentPageInfo")
    if info is None:
        return []
    if not isinstance(info, dict):
        raise ParseError("jlcpcb.com search response has an unexpected componentPageInfo.")
    items = info.get("list")
    if items is None:
        return []
    if not isinstance(items, list):
        raise ParseError("jlcpcb.com search response has an unexpected result list.")
    return items


def _map_items(items: list[Any], limit: int) -> list[SearchResult]:
    results: list[SearchResult] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        code = _text(item.get("componentCode")).upper()
        brand = _text(item.get("componentBrandEn"))
        # "JLCPCB Assembly" entries (C99xxxxxxxx) are assembly services that do not exist in
        # EasyEDA, and anything that is not a plain LCSC id cannot be downloaded either.
        if _LCSC_CODE_RE.fullmatch(code) is None or brand.casefold() == _JLCPCB_ASSEMBLY_BRAND:
            continue
        if code in seen:
            continue
        seen.add(code)
        results.append(
            SearchResult(
                lcsc_id=code,
                mpn=_text(item.get("componentModelEn")),
                manufacturer=brand,
                package=_text(item.get("componentSpecificationEn")),
                description=_text(item.get("describe")),
                datasheet_url=_text(item.get("dataManualUrl")),
                image_url=_image_url(item.get("componentImageUrl"))
                or _image_url(item.get("minImage")),
                library_type=_LIBRARY_TYPES.get(
                    _text(item.get("componentLibraryType")).lower(), ""
                ),
                stock=_int_or_none(item.get("stockCount")),
            )
        )
        if len(results) >= limit:
            break
    return results


# Id lookup


def _result_from_document(document: dict[str, Any], part_id: str) -> SearchResult:
    result = _as_dict(document.get("result"))
    symbol_para = _as_dict(_as_dict(_as_dict(result.get("dataStr")).get("head")).get("c_para"))
    package_detail = _as_dict(result.get("packageDetail"))
    package_para = _as_dict(
        _as_dict(_as_dict(package_detail.get("dataStr")).get("head")).get("c_para")
    )
    lcsc = _as_dict(result.get("lcsc"))
    szlcsc = _as_dict(result.get("szlcsc"))
    return SearchResult(
        lcsc_id=_text(lcsc.get("number")).upper() or part_id,
        mpn=_text(symbol_para.get("Manufacturer Part")) or _text(result.get("title")),
        manufacturer=_text(symbol_para.get("Manufacturer")),
        package=_text(symbol_para.get("package")) or _text(package_para.get("package")),
        description=_text(result.get("description")),
        image_url=_image_url(szlcsc.get("image")),
        library_type=_PART_CLASS_TYPES.get(_text(symbol_para.get("JLCPCB Part Class")).lower(), ""),
        stock=_int_or_none(lcsc.get("stock")),
    )


def _image_url(value: Any) -> str:
    url = _text(value)
    return url if url.lower().startswith(("http://", "https://")) else ""


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None
