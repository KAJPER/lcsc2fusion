"""Offline tests for the urllib transport (fake opener, plus a loopback server for realism)."""

from __future__ import annotations

import gzip
import http.client
import http.server
import io
import ssl
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any, ClassVar

import pytest

from lcsc2fusion import __version__
from lcsc2fusion.client.http import (
    DEFAULT_USER_AGENT,
    HttpResponse,
    TransientNetworkError,
    UrllibTransport,
)
from lcsc2fusion.errors import NetworkError


def _headers(**items: str) -> http.client.HTTPMessage:
    message = http.client.HTTPMessage()
    for name, value in items.items():
        message[name.replace("_", "-")] = value
    return message


class FakeUrlResponse:
    """Stands in for the object returned by `urlopen` (context manager with status/read/headers)."""

    def __init__(
        self, status: int = 200, body: bytes = b"", headers: http.client.HTTPMessage | None = None
    ) -> None:
        self.status = status
        self._body = body
        self.headers = headers if headers is not None else _headers()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> FakeUrlResponse:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


class FakeOpener:
    def __init__(self, outcome: Any) -> None:
        self._outcome = outcome
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float] = []

    def open(self, fullurl: urllib.request.Request, data: bytes | None = None, timeout: float = 0):
        self.requests.append(fullurl)
        self.timeouts.append(timeout)
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome

    def sent_headers(self) -> dict[str, str]:
        return {name.lower(): value for name, value in self.requests[-1].header_items()}


def _transport(outcome: Any) -> tuple[UrllibTransport, FakeOpener]:
    opener = FakeOpener(outcome)
    return UrllibTransport(opener=opener), opener


def test_default_user_agent_identifies_the_project() -> None:
    assert (
        f"lcsc2fusion/{__version__} (+https://github.com/KAJPER/lcsc2fusion)" == DEFAULT_USER_AGENT
    )
    assert "urllib" not in DEFAULT_USER_AGENT.lower()


def test_sends_project_user_agent_and_gzip_by_default() -> None:
    transport, opener = _transport(FakeUrlResponse(body=b"{}"))
    transport.request("GET", "https://example.com/x")
    sent = opener.sent_headers()
    assert sent["user-agent"] == DEFAULT_USER_AGENT
    assert sent["accept-encoding"] == "gzip"


def test_caller_headers_win_over_defaults() -> None:
    transport, opener = _transport(FakeUrlResponse())
    transport.request(
        "GET", "https://example.com/x", headers={"user-agent": "custom/1", "X-Test": "1"}
    )
    sent = opener.sent_headers()
    assert sent["user-agent"] == "custom/1"
    assert sent["x-test"] == "1"
    assert len(opener.requests[0].header_items()) == 3  # UA, X-Test, Accept-Encoding


def test_passes_method_body_and_timeout() -> None:
    transport, opener = _transport(FakeUrlResponse())
    transport.request(
        "post",
        "https://example.com/api",
        data=b"a=1",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=7.5,
    )
    request = opener.requests[0]
    assert request.get_method() == "POST"
    assert request.data == b"a=1"
    assert request.full_url == "https://example.com/api"
    assert opener.timeouts == [7.5]
    assert opener.sent_headers()["content-type"] == "application/x-www-form-urlencoded"


def test_returns_status_body_and_lowercased_headers() -> None:
    headers = _headers(Content_Type="application/json", X_Custom="Yes")
    transport, _ = _transport(FakeUrlResponse(200, b'{"a": 1}', headers))
    response = transport.request("GET", "https://example.com/x")
    assert response == HttpResponse(
        200, b'{"a": 1}', {"content-type": "application/json", "x-custom": "Yes"}
    )
    assert response.ok


def test_gzip_body_is_decoded() -> None:
    payload = b'{"hello": "world"}' * 20
    headers = _headers(Content_Encoding="gzip")
    transport, _ = _transport(FakeUrlResponse(200, gzip.compress(payload), headers))
    assert transport.request("GET", "https://example.com/x").body == payload


def test_corrupt_gzip_raises_network_error() -> None:
    headers = _headers(Content_Encoding="gzip")
    transport, _ = _transport(FakeUrlResponse(200, b"definitely not gzip", headers))
    with pytest.raises(NetworkError, match="corrupt gzip"):
        transport.request("GET", "https://example.com/x")


def test_http_error_is_returned_not_raised_with_body() -> None:
    error = urllib.error.HTTPError(
        "https://example.com/x",
        403,
        "Forbidden",
        _headers(Server="CloudFront"),
        io.BytesIO(b"blocked"),
    )
    transport, _ = _transport(error)
    response = transport.request("GET", "https://example.com/x")
    assert response.status == 403
    assert response.body == b"blocked"
    assert response.headers["server"] == "CloudFront"
    assert not response.ok


def test_http_error_with_gzip_body_is_decoded() -> None:
    error = urllib.error.HTTPError(
        "https://example.com/x",
        500,
        "Server Error",
        _headers(Content_Encoding="gzip"),
        io.BytesIO(gzip.compress(b"oops")),
    )
    transport, _ = _transport(error)
    response = transport.request("GET", "https://example.com/x")
    assert (response.status, response.body) == (500, b"oops")


def test_timeout_becomes_transient_network_error() -> None:
    transport, _ = _transport(TimeoutError("timed out"))
    with pytest.raises(TransientNetworkError, match=r"example\.com timed out after 5 s"):
        transport.request("GET", "https://example.com/x", timeout=5)


def test_timeout_wrapped_in_urlerror_becomes_transient_network_error() -> None:
    transport, _ = _transport(urllib.error.URLError(TimeoutError("timed out")))
    with pytest.raises(TransientNetworkError, match="timed out"):
        transport.request("GET", "https://example.com/x")


def test_urlerror_becomes_transient_network_error_with_reason() -> None:
    transport, _ = _transport(urllib.error.URLError(OSError("Name or service not known")))
    with pytest.raises(TransientNetworkError, match=r"Cannot reach example\.com: .*not known"):
        transport.request("GET", "https://example.com/x")


def test_connection_reset_during_read_is_transient() -> None:
    transport, _ = _transport(ConnectionResetError("reset by peer"))
    with pytest.raises(TransientNetworkError, match=r"Connection to example.com failed"):
        transport.request("GET", "https://example.com/x")


def test_incomplete_read_is_transient() -> None:
    transport, _ = _transport(http.client.IncompleteRead(b"par"))
    with pytest.raises(TransientNetworkError):
        transport.request("GET", "https://example.com/x")


def test_ssl_error_is_a_plain_network_error_not_transient() -> None:
    cert_error = ssl.SSLCertVerificationError("certificate verify failed")
    transport, _ = _transport(urllib.error.URLError(cert_error))
    with pytest.raises(NetworkError, match="TLS error") as info:
        transport.request("GET", "https://example.com/x")
    assert not isinstance(info.value, TransientNetworkError)


def test_bare_ssl_error_is_a_plain_network_error() -> None:
    transport, _ = _transport(ssl.SSLError("handshake failure"))
    with pytest.raises(NetworkError, match="TLS error") as info:
        transport.request("GET", "https://example.com/x")
    assert not isinstance(info.value, TransientNetworkError)


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "ftp://example.com/x", "example.com", "http://"]
)
def test_rejects_non_http_urls(url: str) -> None:
    transport, opener = _transport(FakeUrlResponse())
    with pytest.raises(NetworkError, match="Unsupported or malformed URL"):
        transport.request("GET", url)
    assert opener.requests == []


def test_transient_network_error_is_a_network_error() -> None:
    assert issubclass(TransientNetworkError, NetworkError)


# A real socket round trip on the loopback interface (no external network involved)


class _Handler(http.server.BaseHTTPRequestHandler):
    seen: ClassVar[list[dict[str, str]]] = []

    def do_GET(self) -> None:
        type(self).seen.append({k.lower(): v for k, v in self.headers.items()})
        if self.path == "/gzip":
            body = gzip.compress(b'{"ok": true}')
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
        elif self.path == "/boom":
            body = b"upstream down"
            self.send_response(503)
        else:
            body = b"nope"
            self.send_response(404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return None


@pytest.fixture
def loopback_url() -> Iterator[str]:
    _Handler.seen = []
    try:
        server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    except OSError as exc:  # pragma: no cover - sandbox without loopback sockets
        pytest.skip(f"cannot bind a loopback socket: {exc}")
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_loopback_gzip_and_user_agent(loopback_url: str) -> None:
    response = UrllibTransport().request("GET", f"{loopback_url}/gzip", timeout=5)
    assert (response.status, response.body) == (200, b'{"ok": true}')
    assert _Handler.seen[-1]["user-agent"] == DEFAULT_USER_AGENT
    assert _Handler.seen[-1]["accept-encoding"] == "gzip"


def test_loopback_error_statuses_are_returned(loopback_url: str) -> None:
    transport = UrllibTransport()
    missing = transport.request("GET", f"{loopback_url}/missing", timeout=5)
    boom = transport.request("GET", f"{loopback_url}/boom", timeout=5)
    assert (missing.status, missing.body) == (404, b"nope")
    assert (boom.status, boom.body) == (503, b"upstream down")
