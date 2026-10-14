"""Looks up the newest LCSC2Fusion release on GitHub (no `adsk`, standard library only).

This module only LOOKS. It never downloads an installer and never runs anything: the user starts
the installer by hand. See "Possible one click update" in addin/README.md for why.

`check_latest` makes ONE request, `GET https://api.github.com/repos/<owner>/<repo>/releases/latest`,
and never raises: every failure is a `CheckResult` with the state `error` and a short reason code.
The answer is UNTRUSTED data. Of the whole document only four things are read: `tag_name`, `draft`,
`prerelease` and the NAMES of the assets. Everything that is shown or linked is built here from a
validated `X.Y.Z` version, never copied from the answer (not `html_url`, not
`browser_download_url`, not the release text). So a hostile or merely odd answer can at worst make
the add-in say that a version number exists.

What the request carries: `Accept`, `X-GitHub-Api-Version` and `User-Agent: LCSC2Fusion/<version>`.
No token, cookie, account or machine identifier. The connection is HTTPS with certificate
verification, uses the system proxy settings like any program, follows redirects only to the same
host, has a timeout and a response size cap.

The network sits behind `Fetch` (a callable) and the clock behind `Clock`, so tests never touch the
network. `fetch_https` is the real implementation.
"""

from __future__ import annotations

import http.client
import json
import math
import re
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

GITHUB_OWNER = "KAJPER"
GITHUB_REPO = "lcsc2fusion"
API_HOST = "api.github.com"
LATEST_RELEASE_URL = f"https://{API_HOST}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases"
INSTALLER_NAME_PREFIX = "LCSC2Fusion-Setup-"
INSTALLER_NAME_SUFFIX = ".exe"

REQUEST_TIMEOUT_S = 8.0
MAX_RESPONSE_BYTES = 256 * 1024
MAX_REDIRECTS = 3
_READ_CHUNK = 16 * 1024
_MAX_ASSETS_SCANNED = 1000  # a release has a handful; do not walk a hostile list forever

STATE_AVAILABLE = "available"
STATE_CURRENT = "current"
STATE_ERROR = "error"

# Reason codes of a failed check. Codes only, never a sentence: the log shows them, the page
# does not.
REASON_NETWORK = "network"
REASON_TIMEOUT = "timeout"
REASON_TLS = "tls"
REASON_REDIRECT = "redirect"
REASON_RATE_LIMITED = "rate_limited"
REASON_NOT_FOUND = "not_found"
REASON_SERVER = "server_error"
REASON_HTTP = "http_error"
REASON_TOO_LARGE = "too_large"
REASON_BAD_RESPONSE = "bad_response"
REASON_BAD_TAG = "bad_tag"
REASON_NOT_STABLE = "not_stable"
REASON_BAD_CURRENT = "bad_current_version"
REASON_BAD_URL = "bad_url"
REASON_INTERNAL = "internal"

# A version is three numbers without leading zeros and with at most 6 digits each. That is stricter
# than "[0-9]+" on purpose: "v01.2.3" is not a tag this project ever makes, and a bounded number
# cannot be turned into a huge integer by a hostile answer.
_NUMBER = r"(0|[1-9][0-9]{0,5})"
VERSION_PATTERN = rf"{_NUMBER}\.{_NUMBER}\.{_NUMBER}"
_VERSION_RE = re.compile(VERSION_PATTERN)
_TAG_RE = re.compile(f"v{VERSION_PATTERN}")

Version = tuple[int, int, int]
Clock = Callable[[], float]


class FetchError(Exception):
    """A request that did not give an answer. `reason` is one of the REASON_* codes."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class FetchResponse:
    """The answer of one request: the HTTP status and the body (already capped)."""

    status: int
    body: bytes


# (url, headers, timeout in seconds, maximum body size in bytes) -> the answer. Raises FetchError.
Fetch = Callable[[str, Mapping[str, str], float, int], FetchResponse]


@dataclass(frozen=True)
class UpdateInfo:
    """A newer stable release. Every field is built here from the validated version."""

    version: str  # "0.2.0"
    tag: str  # "v0.2.0"
    release_url: str
    installer_url: str | None  # None unless the release lists the installer file by its exact name


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one check. `update` is set only in the state `available`.

    `latest` is the newest stable version ("0.2.0") for `available` and `current`, None for
    `error`. `reason` is a REASON_* code, set only for `error`. `checked_at` is the epoch time of
    the attempt.
    """

    state: str
    current: str
    checked_at: float
    update: UpdateInfo | None = None
    latest: str | None = None
    reason: str | None = None


# Versions, tags and the URLs built from them ---------------------------------------------------


def parse_version(text: object) -> Version | None:
    """`(1, 2, 3)` for "1.2.3", None for anything that is not exactly that shape."""
    if not isinstance(text, str):
        return None
    found = _VERSION_RE.fullmatch(text)
    if found is None:
        return None
    major, minor, patch = (int(part) for part in found.groups())
    return major, minor, patch


def parse_tag(text: object) -> Version | None:
    """`(1, 2, 3)` for the tag "v1.2.3", None for "1.2.3", "v1.2", "v1.2.3-rc1", "v01.2.3"..."""
    if not isinstance(text, str):
        return None
    found = _TAG_RE.fullmatch(text)
    if found is None:
        return None
    major, minor, patch = (int(part) for part in found.groups())
    return major, minor, patch


def format_version(version: Version) -> str:
    return ".".join(str(part) for part in version)


def format_tag(version: Version) -> str:
    return "v" + format_version(version)


def release_page_url(version: Version) -> str:
    return f"{RELEASES_URL}/tag/{format_tag(version)}"


def installer_file_name(version: Version) -> str:
    return f"{INSTALLER_NAME_PREFIX}{format_version(version)}{INSTALLER_NAME_SUFFIX}"


def installer_download_url(version: Version) -> str:
    return f"{RELEASES_URL}/download/{format_tag(version)}/{installer_file_name(version)}"


# The check -------------------------------------------------------------------------------------


def check_latest(
    current_version: str,
    *,
    fetch: Fetch | None = None,
    now: Clock = time.time,
) -> CheckResult:
    """Ask GitHub for the latest release and compare it with `current_version`. Never raises.

    `fetch` defaults to `fetch_https`. A release older than or equal to the running version is the
    state `current`: a downgrade is never offered.
    """
    checked_at = _read_clock(now)
    try:
        return _check(current_version, fetch if fetch is not None else fetch_https, checked_at)
    except Exception:  # boundary: a lookup must never take the add-in down, whatever it hit
        return _failed(current_version, checked_at, REASON_INTERNAL)


def _read_clock(now: Clock) -> float:
    try:
        value = float(now())
    except Exception:  # boundary: a broken clock only costs the timestamp
        return 0.0
    return value if math.isfinite(value) else 0.0


def _failed(current_version: str, checked_at: float, reason: str) -> CheckResult:
    return CheckResult(
        state=STATE_ERROR,
        current=current_version if isinstance(current_version, str) else "",
        checked_at=checked_at,
        reason=reason,
    )


def _check(current_version: str, fetch: Fetch, checked_at: float) -> CheckResult:
    current = parse_version(current_version)
    if current is None:
        return _failed(current_version, checked_at, REASON_BAD_CURRENT)
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": f"LCSC2Fusion/{format_version(current)}",
    }
    try:
        response = fetch(LATEST_RELEASE_URL, headers, REQUEST_TIMEOUT_S, MAX_RESPONSE_BYTES)
    except FetchError as exc:
        return _failed(current_version, checked_at, exc.reason)
    except TimeoutError:
        return _failed(current_version, checked_at, REASON_TIMEOUT)
    except ssl.SSLError:
        return _failed(current_version, checked_at, REASON_TLS)
    except (OSError, http.client.HTTPException):
        return _failed(current_version, checked_at, REASON_NETWORK)
    reason = _answer_problem(response)
    if reason is not None:
        return _failed(current_version, checked_at, reason)
    document = _parse_document(response.body)
    if document is None:
        return _failed(current_version, checked_at, REASON_BAD_RESPONSE)
    return _compare(current, document, checked_at)


def _answer_problem(response: object) -> str | None:
    """A reason code when the answer is not a usable 200, else None."""
    if not isinstance(response, FetchResponse):
        return REASON_BAD_RESPONSE
    status, body = response.status, response.body
    if isinstance(status, bool) or not isinstance(status, int) or not isinstance(body, bytes):
        return REASON_BAD_RESPONSE
    if status in (403, 429):
        return REASON_RATE_LIMITED
    if status == 404:
        return REASON_NOT_FOUND
    if status >= 500:
        return REASON_SERVER
    if status != 200:
        return REASON_HTTP
    if len(body) > MAX_RESPONSE_BYTES:
        return REASON_TOO_LARGE
    return None


def _parse_document(body: bytes) -> dict[str, Any] | None:
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):  # not JSON, or nested absurdly deep
        return None
    return document if isinstance(document, dict) else None


def _compare(current: Version, document: dict[str, Any], checked_at: float) -> CheckResult:
    current_text = format_version(current)
    draft, prerelease = document.get("draft"), document.get("prerelease")
    if not isinstance(draft, bool) or not isinstance(prerelease, bool):
        return _failed(current_text, checked_at, REASON_BAD_RESPONSE)
    if draft or prerelease:  # `releases/latest` never returns these; if one shows up, ignore it
        return _failed(current_text, checked_at, REASON_NOT_STABLE)
    latest = parse_tag(document.get("tag_name"))
    if latest is None:
        return _failed(current_text, checked_at, REASON_BAD_TAG)
    if latest <= current:  # equal: up to date. older: never offer a downgrade
        return CheckResult(
            state=STATE_CURRENT,
            current=current_text,
            checked_at=checked_at,
            latest=format_version(latest),
        )
    installer = installer_download_url(latest) if _lists_installer(document, latest) else None
    update = UpdateInfo(
        version=format_version(latest),
        tag=format_tag(latest),
        release_url=release_page_url(latest),
        installer_url=installer,
    )
    return CheckResult(
        state=STATE_AVAILABLE,
        current=current_text,
        checked_at=checked_at,
        update=update,
        latest=update.version,
    )


def _lists_installer(document: dict[str, Any], version: Version) -> bool:
    """True when the release lists an asset named exactly like our installer for `version`."""
    assets = document.get("assets")
    if not isinstance(assets, list):
        return False
    wanted = installer_file_name(version)
    for asset in assets[:_MAX_ASSETS_SCANNED]:
        if isinstance(asset, dict) and asset.get("name") == wanted:
            return True
    return False


# The real network access -----------------------------------------------------------------------


def _is_api_url(url: str) -> bool:
    """True for `https://api.github.com/...` with no credentials and no port."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and parts.hostname == API_HOST
        and port is None
        and parts.username is None
        and parts.password is None
    )


class _SameHostRedirects(urllib.request.HTTPRedirectHandler):
    """Follows a redirect (GitHub does that for a renamed repository) only to the API host."""

    max_redirections = MAX_REDIRECTS

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Any:
        if not _is_api_url(newurl):
            raise FetchError(REASON_REDIRECT)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_opener() -> urllib.request.OpenerDirector:
    context = ssl.create_default_context()  # system certificate store, hostname check, verify on
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    # The default handlers stay (they include the system proxy settings); these two replace the
    # default HTTPS and redirect handlers. There is no cookie handler, so nothing is stored.
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context), _SameHostRedirects()
    )


def fetch_https(
    url: str, headers: Mapping[str, str], timeout: float, max_bytes: int
) -> FetchResponse:
    """GET `url` (https on the GitHub API host only). Raises `FetchError`, nothing else.

    HTTP error statuses are returned as a response (the caller maps them). The body is read in
    chunks and refused when it is larger than `max_bytes`; a body that takes longer than `timeout`
    seconds overall counts as a timeout, so a drip fed answer cannot hold the worker for long.
    """
    if not _is_api_url(url):
        raise FetchError(REASON_BAD_URL)
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with _build_opener().open(request, timeout=timeout) as response:
            return FetchResponse(response.status, _read_capped(response, timeout, max_bytes))
    except FetchError:
        raise
    except urllib.error.HTTPError as exc:  # also a URLError: must come first
        with exc:
            return FetchResponse(exc.code, b"")
    except TimeoutError as exc:
        raise FetchError(REASON_TIMEOUT) from exc
    except ssl.SSLError as exc:
        raise FetchError(REASON_TLS) from exc
    except urllib.error.URLError as exc:
        raise FetchError(_url_error_reason(exc)) from exc
    except (http.client.HTTPException, OSError) as exc:
        raise FetchError(REASON_NETWORK) from exc


def _url_error_reason(exc: urllib.error.URLError) -> str:
    reason = exc.reason
    if isinstance(reason, TimeoutError):
        return REASON_TIMEOUT
    if isinstance(reason, ssl.SSLError):
        return REASON_TLS
    return REASON_NETWORK


def _read_capped(response: Any, timeout: float, max_bytes: int) -> bytes:
    """The body of `response`, at most `max_bytes`, read within about `timeout` seconds."""
    encoding = str(response.headers.get("Content-Encoding", "")).strip().lower()
    if encoding not in ("", "identity"):  # we never ask for compression; refuse a compressed bomb
        raise FetchError(REASON_BAD_RESPONSE)
    declared = str(response.headers.get("Content-Length", "")).strip()
    if declared.isascii() and declared.isdigit() and int(declared) > max_bytes:
        raise FetchError(REASON_TOO_LARGE)
    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(_READ_CHUNK, max_bytes + 1 - total))
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > max_bytes:
            raise FetchError(REASON_TOO_LARGE)
        chunks.append(chunk)
        if time.monotonic() > deadline:
            raise FetchError(REASON_TIMEOUT)
