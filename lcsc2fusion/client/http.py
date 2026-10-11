"""HTTP transport: a small `Transport` protocol plus a stdlib (urllib) implementation.

Design notes:
- Non-2xx responses are returned as `HttpResponse`, never raised, so the caller decides what a
  403 or a 404 means. Only transport failures (DNS, timeout, TLS, broken gzip) raise.
- easyeda.com sits behind CloudFront, which answers 403 to the default python-urllib user agent,
  so a descriptive project user agent is always sent unless the caller overrides it.
"""

from __future__ import annotations

import gzip
import http.client
import ssl
import urllib.error
import urllib.request
import zlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from lcsc2fusion import __version__
from lcsc2fusion.errors import NetworkError

DEFAULT_USER_AGENT = f"lcsc2fusion/{__version__} (+https://github.com/KAJPER/lcsc2fusion)"
DEFAULT_TIMEOUT_S = 30.0

_GZIP_ENCODINGS = frozenset({"gzip", "x-gzip"})


class TransientNetworkError(NetworkError):
    """A failure that may succeed on a retry: timeout, connection problem or HTTP 5xx."""


@dataclass(frozen=True)
class HttpResponse:
    """A completed HTTP exchange. `body` is already decompressed, header names are lower case."""

    status: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class Transport(Protocol):
    """Anything that can perform one HTTP request. Tests plug in a fake."""

    def request(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> HttpResponse: ...


class _Opener(Protocol):
    """The subset of `urllib.request.OpenerDirector` that `UrllibTransport` uses."""

    def open(
        self,
        fullurl: urllib.request.Request,
        data: bytes | None = None,
        timeout: float = ...,
    ) -> Any: ...


class UrllibTransport:
    """`Transport` implemented with `urllib.request` (stdlib only, works inside Fusion)."""

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        opener: _Opener | None = None,
    ) -> None:
        self._user_agent = user_agent
        self._opener: _Opener = opener if opener is not None else urllib.request.build_opener()

    def request(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> HttpResponse:
        parts = urlsplit(url)
        host = parts.hostname
        if parts.scheme not in ("http", "https") or not host:
            raise NetworkError(f"Unsupported or malformed URL: {url!r}")
        request = urllib.request.Request(
            url,
            data=data,
            headers=self._merge_headers(headers),
            method=method.upper(),
        )
        try:
            try:
                with self._opener.open(request, timeout=timeout) as response:
                    return _build_response(response.status, response.read(), response.headers, host)
            except urllib.error.HTTPError as exc:
                with exc:
                    return _build_response(exc.code, exc.read(), exc.headers, host)
        except TimeoutError as exc:
            raise TransientNetworkError(f"Request to {host} timed out after {timeout:g} s") from exc
        except ssl.SSLError as exc:
            raise NetworkError(f"TLS error while talking to {host}: {exc}") from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, TimeoutError):
                raise TransientNetworkError(
                    f"Request to {host} timed out after {timeout:g} s"
                ) from exc
            if isinstance(reason, ssl.SSLError):
                raise NetworkError(f"TLS error while talking to {host}: {reason}") from exc
            raise TransientNetworkError(f"Cannot reach {host}: {reason}") from exc
        except http.client.InvalidURL as exc:
            raise NetworkError(f"Malformed URL {url!r}: {exc}") from exc
        except (http.client.HTTPException, OSError) as exc:
            raise TransientNetworkError(f"Connection to {host} failed: {exc}") from exc

    def _merge_headers(self, headers: Mapping[str, str] | None) -> dict[str, str]:
        merged = dict(headers) if headers else {}
        present = {name.lower() for name in merged}
        if "user-agent" not in present:
            merged["User-Agent"] = self._user_agent
        if "accept-encoding" not in present:
            merged["Accept-Encoding"] = "gzip"
        return merged


def _build_response(status: int, raw: bytes, headers: Any, host: str) -> HttpResponse:
    """Lower-case the header names and transparently decode gzip bodies."""
    lowered = {str(name).lower(): str(value) for name, value in headers.items()}
    encoding = lowered.get("content-encoding", "").strip().lower()
    body = raw
    if encoding in _GZIP_ENCODINGS and raw:
        try:
            body = gzip.decompress(raw)
        except (OSError, EOFError, zlib.error) as exc:
            raise NetworkError(f"{host} sent a corrupt gzip response: {exc}") from exc
    return HttpResponse(status=status, body=body, headers=lowered)
