"""Local HTTP transport: serves the add-in page to a separate browser window (no `adsk` here).

The page (`palette/index.html` plus `app.js`) runs in a normal browser window instead of a Fusion
palette. This module carries the same two way protocol over loopback HTTP with the standard
library only:

    page to Python   POST /api/act   {"action": str, "data": str}  ->  {"reply": str}
    Python to page   GET  /api/events  Server-Sent Events, one JSON object per event

`Hub` is the thread safe fan out of Python to page messages (a ring buffer for replay plus one
queue per connected page). `HubSink` adapts it to `controller.PaletteSink`. `HttpTransport` owns
the server. The `dispatch` callable (action, data -> reply) runs in the HTTP request thread and
may block while the main thread works; it must not be called with any transport lock held, and it
is not.

Security model (every request is checked in this order, any failure is a plain 403/404/405):
    1. The Host header is exactly `127.0.0.1:<port>` (defeats DNS rebinding).
    2. A session cookie issued by a launch token exchange (`GET /?t=<token>` answers 302 plus
       Set-Cookie). Launch tokens are single use, 128 bit and valid for 60 seconds.
    3. An Origin header, when present, equals our own origin (defeats cross site requests).
    4. The method is allowed for the route. POST also needs `Content-Type: application/json`,
       `X-LCSC: 1` and a body of at most 64 KiB. There are no CORS headers and no WebSocket.
Only an allow list of static files is served, never a directory listing or a path derived from
the request. All server logging goes to the injected `log`, nothing is written to stderr.

Every response closes its connection (no keep-alive): there are no idle connections to manage,
no pipelining and no leftover request bytes, and stop() can end everything promptly. Loopback
connections are cheap. The event stream is the only long lived connection.
"""

from __future__ import annotations

import collections
import contextlib
import enum
import http.client
import http.server
import json
import secrets
import select
import socket
import socketserver
import sys
import threading
import time
import traceback
import urllib.parse
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

__all__ = [
    "Dispatch",
    "HttpTransport",
    "Hub",
    "HubClosed",
    "HubEvent",
    "HubFull",
    "HubSink",
    "Subscription",
    "format_sse_event",
]

LOOPBACK_HOST = "127.0.0.1"
SESSION_COOKIE = "lcsc_s"
LAUNCH_TOKEN_TTL_S = 60.0
LAUNCH_TOKEN_BYTES = 16  # 128 bit
SESSION_BYTES = 32

MAX_BODY_BYTES = 65536
MAX_ACTION_CHARS = 64
MAX_DATA_CHARS = 60000

# Synthetic event (id 0, never part of the id sequence) that ends a stream whose consumer fell too
# far behind. The page reconnects and the hub replays what it still has.
OVERFLOW_ACTION = "overflow"

# Static file allow list: file name -> Content-Type. Nothing else is ever read from `static_dir`.
STATIC_FILES: dict[str, str] = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
    "http-bridge.js": "text/javascript; charset=utf-8",
    "mock-bridge.js": "text/javascript; charset=utf-8",
}

_SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    (
        "Content-Security-Policy",
        "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    ),
    ("X-Frame-Options", "DENY"),
    # Not CORS: stops other origins (another local web server) from loading our
    # responses with no-cors tags such as <script> or <img>.
    ("Cross-Origin-Resource-Policy", "same-origin"),
)

_JSON_TYPE = "application/json; charset=utf-8"
_TEXT_TYPE = "text/plain; charset=utf-8"
_SSE_TYPE = "text/event-stream; charset=utf-8"

_ROUTE_GET = "GET"
_ROUTE_POST = "POST"
_PATH_ACT = "/api/act"
_PATH_EVENTS = "/api/events"
_PATH_PING = "/api/ping"
_PATH_FAVICON = "/favicon.ico"
_ROUTES: dict[str, str] = {
    "/": _ROUTE_GET,
    _PATH_FAVICON: _ROUTE_GET,
    _PATH_PING: _ROUTE_GET,
    _PATH_EVENTS: _ROUTE_GET,
    _PATH_ACT: _ROUTE_POST,
    **{f"/{name}": _ROUTE_GET for name in STATIC_FILES},
}

_SSE_POLL_S = 0.2  # how often an idle stream checks whether the browser went away
_IDLE_POLL_S = 0.2  # how often a connection that has sent nothing yet checks for shutdown
_SERVER_POLL_S = 0.05  # serve_forever poll interval: bounds the time stop() needs
_STOP_JOIN_S = 5.0
_CONNECTION_GRACE_S = 0.5
_LINGER_S = 1.0  # after refusing a request body we did not read, swallow it this long
_LINGER_MAX_BYTES = 1 << 20
_MAX_LOG_CHARS = 200
_MAX_LIMITED_LOG_LINES = 50  # refusals and connection errors logged per transport lifetime
_MAX_LAST_EVENT_ID_DIGITS = 18

Dispatch = Callable[[str, str], str]
Log = Callable[[str], None]


# Hub ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class HubEvent:
    """One Python to page message. `id` is monotonic from 1; 0 marks a synthetic event."""

    id: int
    action: str
    data: str


class HubFull(Exception):
    """The maximum number of subscribers is already connected."""


class HubClosed(Exception):
    """The hub (or the subscription) is closed and nothing is left to read."""


class Subscription:
    """One consumer of a `Hub`. Created by `Hub.subscribe`; call `close()` when done."""

    def __init__(self, hub: Hub, replay: Iterable[HubEvent], backlog_limit: int) -> None:
        self._hub = hub
        self._cond = threading.Condition()
        self._queue: collections.deque[HubEvent] = collections.deque(replay)
        # The replay does not count against the limit, only events that arrive afterwards do.
        self._limit = len(self._queue) + backlog_limit
        self._ended = False

    def get(self, timeout: float) -> HubEvent | None:
        """Next event, or None after `timeout` seconds (the caller sends a heartbeat).

        Raises HubClosed once the subscription ended and every queued event was read.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cond:
            while True:
                if self._queue:
                    return self._queue.popleft()
                if self._ended:
                    raise HubClosed
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)

    def close(self) -> None:
        """Leave the hub and drop whatever is still queued. Safe to call more than once."""
        self._hub._discard(self)  # hub lock only, never while holding our own condition
        with self._cond:
            self._queue.clear()
            self._ended = True
            self._cond.notify_all()

    def finish(self) -> None:
        """End the stream after the queued events were read (used when the server stops)."""
        with self._cond:
            self._ended = True
            self._cond.notify_all()

    def _push(self, event: HubEvent) -> bool:
        """Called by the hub with its lock held. Returns False when the subscriber was dropped."""
        with self._cond:
            if self._ended:
                return False
            if len(self._queue) >= self._limit:
                # Too far behind: replace the backlog by one marker and end, so that the consumer
                # reconnects and the hub replays what is still in the ring.
                self._queue.clear()
                self._queue.append(HubEvent(0, OVERFLOW_ACTION, "{}"))
                self._ended = True
                self._cond.notify_all()
                return False
            self._queue.append(event)
            self._cond.notify()
            return True


class Hub:
    """Thread safe publish/subscribe with a replay ring buffer. `publish` never blocks."""

    def __init__(self, ring_size: int = 200, max_subscribers: int = 4) -> None:
        if ring_size < 1:
            raise ValueError("ring_size must be at least 1")
        if max_subscribers < 1:
            raise ValueError("max_subscribers must be at least 1")
        self._ring_size = ring_size
        self._max_subscribers = max_subscribers
        self._lock = threading.Lock()
        self._ring: collections.deque[HubEvent] = collections.deque(maxlen=ring_size)
        self._next_id = 1
        self._subscriptions: list[Subscription] = []
        self._closed = False

    def publish(self, action: str, data: str) -> int:
        """Queue an event for every subscriber and return its id (0 after `close()`)."""
        with self._lock:
            if self._closed:
                return 0
            event = HubEvent(self._next_id, action, data)
            self._next_id += 1
            self._ring.append(event)
            self._subscriptions = [s for s in self._subscriptions if s._push(event)]
            return event.id

    def subscribe(self, last_event_id: int | None = None) -> Subscription:
        """None starts at the head, an int replays the ring events with id above it.

        Raises HubFull at the subscriber limit and HubClosed after `close()`.
        """
        with self._lock:
            if self._closed:
                raise HubClosed
            if len(self._subscriptions) >= self._max_subscribers:
                raise HubFull
            replay = (
                [] if last_event_id is None else [e for e in self._ring if e.id > last_event_id]
            )
            subscription = Subscription(self, replay, self._ring_size)
            self._subscriptions.append(subscription)
            return subscription

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscriptions)

    def close(self) -> None:
        """Stop accepting events and end every subscription once it has read its queue."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            subscriptions, self._subscriptions = self._subscriptions, []
        for subscription in subscriptions:
            subscription.finish()

    def _discard(self, subscription: Subscription) -> None:
        with self._lock, contextlib.suppress(ValueError):
            self._subscriptions.remove(subscription)


class HubSink:
    """`controller.PaletteSink` that publishes to a hub."""

    def __init__(self, hub: Hub) -> None:
        self._hub = hub

    def send(self, action: str, data: str) -> None:
        self._hub.publish(action, data)


def format_sse_event(event: HubEvent) -> bytes:
    """One SSE frame. ASCII only and a single data line, whatever the payload contains."""
    payload = json.dumps({"action": event.action, "data": event.data})
    head = f"id: {event.id}\n" if event.id > 0 else ""  # no id line: do not move Last-Event-ID
    return f"{head}data: {payload}\n\n".encode("ascii")


# Helpers ------------------------------------------------------------------------------------


def _one_line(text: str, limit: int = _MAX_LOG_CHARS) -> str:
    cleaned = "".join(ch if ch.isprintable() else " " for ch in text)
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 3] + "..."


def _parse_last_event_id(value: str | None) -> int | None:
    """A non negative integer, anything else (garbage, negative, huge) counts as absent."""
    if value is None:
        return None
    text = value.strip()
    if not (text.isascii() and text.isdigit()) or len(text) > _MAX_LAST_EVENT_ID_DIGITS:
        return None
    return int(text)


def _parse_query(query: str) -> dict[str, list[str]]:
    try:
        return urllib.parse.parse_qs(query, keep_blank_values=True, max_num_fields=8)
    except ValueError:
        return {}


def _constant_time_match(candidate: bytes, known: Iterable[bytes]) -> int | None:
    """Index of the entry equal to `candidate`; always compares against every entry."""
    found: int | None = None
    for index, value in enumerate(known):
        if secrets.compare_digest(candidate, value) and found is None:
            found = index
    return found


def _to_bytes(value: str) -> bytes:
    return value.encode("utf-8", "replace")


# HTTP server --------------------------------------------------------------------------------


class _Server(http.server.ThreadingHTTPServer):
    """Loopback only server that reports everything to its owner instead of stderr."""

    allow_reuse_address = False  # never share the port with another socket
    daemon_threads = True
    block_on_close = False
    request_queue_size = 32

    def __init__(self, owner: HttpTransport, host: str, port: int) -> None:
        self.owner = owner
        self._connections: set[socket.socket] = set()
        self._connections_lock = threading.Lock()
        super().__init__((host, port), _Handler)

    def server_bind(self) -> None:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)  # Windows only
        if exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        # Skip HTTPServer.server_bind: its getfqdn() lookup can take seconds on Windows.
        socketserver.TCPServer.server_bind(self)
        self.server_name = LOOPBACK_HOST
        self.server_port = self.server_address[1]

    def process_request(self, request: Any, client_address: Any) -> None:
        with self._connections_lock:
            self._connections.add(request)
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._forget(request)
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._forget(request)

    def _forget(self, request: Any) -> None:
        with self._connections_lock:
            self._connections.discard(request)

    def open_connections(self) -> int:
        with self._connections_lock:
            return len(self._connections)

    def abort_connections(self) -> None:
        """Best effort: end connections that are blocked in a read or a write.

        Effective on POSIX. On Windows shutdown() does not wake a thread that is
        waiting in a timed read, so stop() relies on the idle polling in handle() and
        on the socket timeout for those."""
        with self._connections_lock:
            sockets = list(self._connections)
        for connection in sockets:
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, ConnectionError):
            # Broken pipe, reset and abort are normal when the window is closed.
            self.owner._log_limited(f"connection closed by the client ({type(exc).__name__})")
        elif isinstance(exc, OSError):
            self.owner._log_limited(f"connection error: {type(exc).__name__}: {exc}")
        else:
            self.owner._emit(
                "unexpected error in the request handler:\n" + traceback.format_exc(),
                multiline=True,
            )


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LCSC2Fusion"
    sys_version = ""
    disable_nagle_algorithm = True  # small SSE frames must not wait for a delayed ACK

    # Per request state (class level defaults: the base class handles a request in __init__).
    _unread_body = False
    _linger = False
    _responded = False

    @property
    def _owner(self) -> HttpTransport:
        return cast(_Server, self.server).owner

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(self._owner.connection_timeout_s)  # idle and stall limit

    def handle(self) -> None:
        # One request per connection (every response says `Connection: close`), so the wait for
        # the first byte is the only idle period. Polling it lets stop() end idle connections,
        # which shutdown() cannot do for a socket blocked in a timed read on Windows.
        if self._wait_for_request():
            super().handle()

    def finish(self) -> None:
        super().finish()
        if self._linger:
            self._drain_input()

    def _wait_for_request(self) -> bool:
        owner = self._owner
        deadline = time.monotonic() + owner.connection_timeout_s
        while not owner.is_stopping and time.monotonic() < deadline:
            try:
                readable, _, _ = select.select([self.connection], [], [], _IDLE_POLL_S)
            except (OSError, ValueError):
                return False
            if readable:
                return True
        return False

    def version_string(self) -> str:
        return self.server_version

    # Logging goes to the add-in log, never to stderr -------------------------------------------

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        pass

    def log_message(self, format: str, *args: Any) -> None:
        try:
            text = format % args
        except (TypeError, ValueError):
            text = format
        if text.startswith("Request timed out"):
            return  # an idle keep-alive connection was closed: nothing worth a line
        self._owner._log_limited("http: " + text)

    # Entry point for every method --------------------------------------------------------------

    def do_GET(self) -> None:
        try:
            self._serve()
        except OSError:
            raise  # the client went away or timed out: the server reports it in one short line
        except Exception:  # boundary: a bug in a route must not silently drop the connection
            self._owner._emit(
                "unexpected error while handling a request:\n" + traceback.format_exc(),
                multiline=True,
            )
            if not self._responded:
                self._reply(500, b"500 Internal Server Error\n", _TEXT_TYPE)

    do_HEAD = do_PUT = do_DELETE = do_OPTIONS = do_PATCH = do_TRACE = do_CONNECT = do_POST = do_GET

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """Protocol level errors (bad request line, too long headers, unknown method)."""
        self.log_error("code %d, message %s", code, message)
        self.close_connection = True
        self._reply(code, _status_text(code).encode("ascii"), _TEXT_TYPE)

    def _serve(self) -> None:
        owner = self._owner
        self.close_connection = True  # one request per connection, see handle()
        self._unread_body = self._declares_body()
        path, query = self._split_target()

        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or hosts[0] != owner.host_header:
            self._reject(403, "unexpected Host header")
            return
        origins = self.headers.get_all("Origin", [])
        origin_ok = not origins or (len(origins) == 1 and origins[0] == owner.origin)

        params = _parse_query(query)
        if self.command == "GET" and path == "/" and "t" in params:
            self._launch(params["t"], origin_ok)
            return

        if not owner._session_valid(self._session_cookies()):
            self._reject(403, "no valid session")
            return
        if not origin_ok:
            self._reject(403, "foreign Origin")
            return
        if self.command not in (_ROUTE_GET, _ROUTE_POST):
            self._method_not_allowed("GET, POST")
            return
        allowed = _ROUTES.get(path)
        if allowed is None:
            self._reply(404, b"404 Not Found\n", _TEXT_TYPE)
            return
        if self.command != allowed:
            self._method_not_allowed(allowed)
            return

        if path == _PATH_ACT:
            self._act()
        elif path == _PATH_EVENTS:
            self._events()
        elif path == _PATH_PING:
            self._ping()
        elif path == _PATH_FAVICON:
            self._reply(204, b"", None)
        else:
            self._static("index.html" if path == "/" else path[1:])

    # Request parsing ---------------------------------------------------------------------------

    def _split_target(self) -> tuple[str, str]:
        """Path and query exactly as the client wrote them (no normalisation, no decoding).

        `self.path` is not used: newer Python versions collapse a leading `//` in it.
        """
        words = self.requestline.split()
        target = words[1] if len(words) >= 2 else self.path
        path, _, query = target.partition("?")
        return path, query

    def _declares_body(self) -> bool:
        if self.headers.get("Transfer-Encoding") is not None:
            return True
        length = self.headers.get("Content-Length")
        return length is not None and length.strip() not in ("", "0")

    def _session_cookies(self) -> list[str]:
        values: list[str] = []
        for header in self.headers.get_all("Cookie", []):
            for part in header.split(";"):
                name, separator, value = part.strip().partition("=")
                if separator and name == SESSION_COOKIE:
                    values.append(value.strip())
        return values

    # Responses ---------------------------------------------------------------------------------

    def end_headers(self) -> None:
        for name, value in _SECURITY_HEADERS:
            self.send_header(name, value)
        super().end_headers()

    def _reply(
        self,
        status: int,
        body: bytes,
        content_type: str | None,
        *,
        headers: Iterable[tuple[str, str]] = (),
    ) -> None:
        if self._unread_body:
            self._linger = True  # the client is still sending a body we never read
        self._responded = True
        self.send_response(status)
        if content_type is not None:
            self.send_header("Content-Type", content_type)
        if status != 204:
            self.send_header("Content-Length", str(len(body)))
        for name, value in headers:
            self.send_header(name, value)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _reply_json(self, status: int, payload: dict[str, Any]) -> None:
        self._reply(status, json.dumps(payload).encode("ascii"), _JSON_TYPE)

    def _reject(self, status: int, reason: str) -> None:
        self._owner._log_limited(f"refused {status}: {reason}")
        self._reply(status, _status_text(status).encode("ascii"), _TEXT_TYPE)

    def _method_not_allowed(self, allow: str) -> None:
        self._reply(405, b"405 Method Not Allowed\n", _TEXT_TYPE, headers=[("Allow", allow)])

    def _drain_input(self) -> None:
        """Lingering close: read what the client still sends so the reset cannot eat our reply."""
        connection = self.connection
        with contextlib.suppress(OSError):
            connection.shutdown(socket.SHUT_WR)
            deadline = time.monotonic() + _LINGER_S
            budget = _LINGER_MAX_BYTES
            while budget > 0:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                connection.settimeout(left)
                chunk = connection.recv(65536)
                if not chunk:
                    break
                budget -= len(chunk)

    # Routes ------------------------------------------------------------------------------------

    def _launch(self, tokens: list[str], origin_ok: bool) -> None:
        """Exchange a single use launch token for a session cookie."""
        if not origin_ok:
            self._reject(403, "foreign Origin")
            return
        session = self._owner._exchange_token(tokens[0]) if len(tokens) == 1 else None
        if session is None:
            self._reject(403, "bad or expired launch token")
            return
        cookie = f"{SESSION_COOKIE}={session}; HttpOnly; SameSite=Strict; Path=/"
        self._reply(302, b"", None, headers=[("Location", "/"), ("Set-Cookie", cookie)])

    def _static(self, name: str) -> None:
        content_type = STATIC_FILES.get(name)
        if content_type is None:  # unreachable through _ROUTES, kept as a second barrier
            self._reply(404, b"404 Not Found\n", _TEXT_TYPE)
            return
        try:
            body = (self._owner.static_dir / name).read_bytes()
        except OSError as exc:
            self._owner._log_limited(f"static file {name} is not readable: {exc}")
            self._reply(404, b"404 Not Found\n", _TEXT_TYPE)
            return
        self._reply(200, body, content_type)

    def _ping(self) -> None:
        self._reply_json(200, {"ok": True, "subscribers": self._owner.hub.subscriber_count})

    def _act(self) -> None:
        if self.headers.get("X-LCSC") != "1":
            self._reject(403, "missing X-LCSC header")
            return
        if self.headers.get_content_type() != "application/json":
            self._reject(415, "content type is not application/json")
            return
        body = self._read_body()
        if body is None:
            return
        parsed = _parse_act_body(body)
        if parsed is None:
            self._reply(400, b"400 Bad Request\n", _TEXT_TYPE)
            return
        action, data = parsed
        owner = self._owner
        try:
            reply = owner.dispatch(action, data)
            payload = json.dumps({"reply": reply}).encode("ascii")
        except TimeoutError:
            owner._emit(f"dispatch of {action!r} timed out")
            self._reply_json(504, {"reply": "ERROR", "error": "timeout"})
            return
        except Exception as exc:  # boundary: whatever dispatch raises must not leak to the page
            owner._emit(f"dispatch of {action!r} failed: {type(exc).__name__}: {exc}")
            self._reply_json(500, {"reply": "ERROR"})
            return
        self._reply(200, payload, _JSON_TYPE)

    def _read_body(self) -> bytes | None:
        """The request body, or None after an error response was sent."""
        if self.headers.get("Transfer-Encoding") is not None:
            self._reject(501, "Transfer-Encoding is not supported")
            return None
        lengths = self.headers.get_all("Content-Length", [])
        if not lengths:
            self._reject(411, "Content-Length is required")
            return None
        text = lengths[0].strip()
        if len(set(lengths)) != 1 or not (text.isascii() and text.isdigit()) or len(text) > 12:
            self._reject(400, "invalid Content-Length")
            return None
        length = int(text)
        if length > MAX_BODY_BYTES:
            self._reject(413, f"body of {length} bytes is too large")
            return None
        body = self.rfile.read(length)
        self._unread_body = False
        if len(body) != length:
            self.close_connection = True
            self._reply(400, b"400 Bad Request\n", _TEXT_TYPE)
            return None
        return body

    # Server-Sent Events ------------------------------------------------------------------------

    def _events(self) -> None:
        owner = self._owner
        last_id = _parse_last_event_id(self.headers.get("Last-Event-ID"))
        try:
            subscription = owner.hub.subscribe(last_id)
        except (HubFull, HubClosed) as exc:
            self._owner._log_limited(f"event stream refused: {type(exc).__name__}")
            self._reply(
                503, b"503 Service Unavailable\n", _TEXT_TYPE, headers=[("Retry-After", "2")]
            )
            return
        if not owner._track(subscription):
            subscription.close()
            self._reply(503, b"503 Service Unavailable\n", _TEXT_TYPE)
            return
        owner._emit("event stream opened")
        try:
            # The body ends when the connection closes: no Content-Length, no keep-alive.
            self.close_connection = True
            self._responded = True
            self.send_response(200)
            self.send_header("Content-Type", _SSE_TYPE)
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"retry: 1000\n\n")
            self._pump(subscription)
        except (OSError, ValueError):
            pass  # the browser went away (or stop() aborted the socket): not an error
        finally:
            owner._untrack(subscription)
            subscription.close()
            owner._emit("event stream closed")

    def _pump(self, subscription: Subscription) -> None:
        heartbeat = self._owner.heartbeat_s
        last_write = time.monotonic()
        while True:
            wait = min(_SSE_POLL_S, max(0.0, last_write + heartbeat - time.monotonic()))
            try:
                event = subscription.get(wait)
            except HubClosed:
                return
            if event is not None:
                self.wfile.write(format_sse_event(event))
                last_write = time.monotonic()
                continue
            if self._peer_closed():
                return
            if time.monotonic() - last_write >= heartbeat:
                self.wfile.write(b": ping\n\n")
                last_write = time.monotonic()

    def _peer_closed(self) -> bool:
        """True when the browser closed its end (an idle stream would only notice at a write)."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            if not readable:
                return False
            peeked: bytes = self.connection.recv(1, socket.MSG_PEEK)
            return peeked == b""
        except OSError:
            return True


def _status_text(status: int) -> str:
    return f"{status} {http.client.responses.get(status, 'Error')}\n"


def _parse_act_body(body: bytes) -> tuple[str, str] | None:
    """(action, data) from a request body, or None when it is not the expected JSON object."""
    try:
        value = json.loads(body.decode("utf-8"))
    except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError
        return None
    if not isinstance(value, dict):
        return None
    action = value.get("action")
    data = value.get("data")
    if not isinstance(action, str) or not isinstance(data, str):
        return None
    if not 0 < len(action) <= MAX_ACTION_CHARS or len(data) > MAX_DATA_CHARS:
        return None
    return action, data


# Transport ----------------------------------------------------------------------------------


class _State(enum.Enum):
    NEW = "new"
    RUNNING = "running"
    STOPPED = "stopped"


class HttpTransport:
    """Loopback HTTP server for the add-in page. One instance runs once: start, then stop."""

    def __init__(
        self,
        hub: Hub,
        dispatch: Dispatch,
        log: Log,
        static_dir: Path,
        *,
        host: str = LOOPBACK_HOST,
        port: int = 0,
        dispatch_timeout_s: float = 30.0,
        heartbeat_s: float = 15.0,
        connection_timeout_s: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if host != LOOPBACK_HOST:
            raise ValueError(f"only {LOOPBACK_HOST} is allowed as host, got {host!r}")
        if not 0 <= port <= 65535:
            raise ValueError(f"invalid port {port}")
        for name, value in (
            ("dispatch_timeout_s", dispatch_timeout_s),
            ("heartbeat_s", heartbeat_s),
            ("connection_timeout_s", connection_timeout_s),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        self.hub = hub
        self.dispatch = dispatch
        self.static_dir = Path(static_dir)
        # `dispatch` runs in the request thread and cannot be interrupted from here, so it is
        # expected to honour this value itself (raise TimeoutError). Kept for the caller's use.
        self.dispatch_timeout_s = dispatch_timeout_s
        self.heartbeat_s = heartbeat_s
        self.connection_timeout_s = connection_timeout_s
        self._log = log
        self._port = port
        self._clock = clock
        self._state = _State.NEW
        self._state_lock = threading.Lock()  # lifecycle and the subscription set
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self._subscriptions: set[Subscription] = set()
        self._auth_lock = threading.Lock()  # launch tokens and sessions
        self._tokens: dict[str, float] = {}
        self._sessions: list[bytes] = []
        self._log_lock = threading.Lock()
        self._limited_lines = 0

    # Public API --------------------------------------------------------------------------------

    @property
    def url(self) -> str:
        return f"http://{self.host_header}"

    @property
    def is_stopping(self) -> bool:
        return self._state is _State.STOPPED

    @property
    def host_header(self) -> str:
        if self._state is _State.NEW:
            raise RuntimeError("the transport has not been started")
        return f"{LOOPBACK_HOST}:{self._port}"

    @property
    def origin(self) -> str:
        return self.url

    def start(self) -> None:
        """Bind and serve in a daemon thread. OSError propagates when the port is unavailable."""
        with self._state_lock:
            if self._state is not _State.NEW:
                raise RuntimeError("the transport was already started")
            server = _Server(self, LOOPBACK_HOST, self._port)  # may raise OSError
            self._port = int(server.server_address[1])
            thread = threading.Thread(
                target=self._serve_loop, args=(server,), name="lcsc2fusion-http", daemon=True
            )
            self._server = server
            self._thread = thread
            self._state = _State.RUNNING  # before the loop runs: handlers read it
            try:
                thread.start()
            except RuntimeError:  # no thread available: give the port back
                self._state = _State.NEW
                server.server_close()
                raise
        # The socket is already listening, so a client may connect before the loop runs.
        self._emit(f"http transport listening on {self.url}")

    def stop(self) -> None:
        """Close event streams, stop serving and join the thread. Safe to call repeatedly."""
        with self._state_lock:
            if self._state is not _State.RUNNING:
                return
            self._state = _State.STOPPED
            server = self._server
            thread = self._thread
            subscriptions = list(self._subscriptions)
        if server is None or thread is None:
            return
        for subscription in subscriptions:
            subscription.finish()  # streams write what is queued, then end
        server.shutdown()  # also fine when the loop has not started yet: it then exits at once
        server.server_close()
        thread.join(_STOP_JOIN_S)
        _wait_until(lambda: server.open_connections() == 0, _CONNECTION_GRACE_S)
        server.abort_connections()
        _wait_until(lambda: server.open_connections() == 0, _CONNECTION_GRACE_S)
        self._emit("http transport stopped")

    def launch_url(self) -> str:
        """URL carrying a fresh single use token (valid 60 s) that opens a session."""
        if self._state is not _State.RUNNING:
            raise RuntimeError("the transport is not running")
        token = secrets.token_urlsafe(LAUNCH_TOKEN_BYTES)
        now = self._clock()
        with self._auth_lock:
            self._purge_tokens(now)
            self._tokens[token] = now + LAUNCH_TOKEN_TTL_S
        return f"{self.url}/?t={token}"

    # Internals used by the request handler -----------------------------------------------------

    def _serve_loop(self, server: _Server) -> None:
        try:
            server.serve_forever(poll_interval=_SERVER_POLL_S)
        except Exception:  # boundary of the server thread: report instead of dying silently
            self._emit("the HTTP server loop crashed:\n" + traceback.format_exc(), multiline=True)

    def _purge_tokens(self, now: float) -> None:
        for token in [t for t, expiry in self._tokens.items() if expiry <= now]:
            del self._tokens[token]

    def _exchange_token(self, candidate: str) -> str | None:
        """Consume a launch token and return a new session id, or None."""
        now = self._clock()
        with self._auth_lock:
            self._purge_tokens(now)
            known = list(self._tokens)
            index = _constant_time_match(_to_bytes(candidate), [_to_bytes(t) for t in known])
            if index is None:
                return None
            del self._tokens[known[index]]
            session = secrets.token_urlsafe(SESSION_BYTES)
            self._sessions.append(_to_bytes(session))
            return session

    def _session_valid(self, cookies: list[str]) -> bool:
        with self._auth_lock:
            sessions = list(self._sessions)
        valid = False
        for cookie in cookies:
            if _constant_time_match(_to_bytes(cookie), sessions) is not None:
                valid = True
        return valid

    def _track(self, subscription: Subscription) -> bool:
        with self._state_lock:
            if self._state is not _State.RUNNING:
                return False
            self._subscriptions.add(subscription)
            return True

    def _untrack(self, subscription: Subscription) -> None:
        with self._state_lock:
            self._subscriptions.discard(subscription)

    def _emit(self, message: str, *, multiline: bool = False) -> None:
        """Send a line to the add-in log. Never called with one of our locks held."""
        with contextlib.suppress(Exception):  # a failing logger must not break the server
            self._log(message if multiline else _one_line(message))

    def _log_limited(self, message: str) -> None:
        """Refusals and connection errors: capped, a hostile client must not flood the log."""
        with self._log_lock:
            self._limited_lines += 1
            count = self._limited_lines
        if count <= _MAX_LIMITED_LOG_LINES:
            self._emit(message)
        elif count == _MAX_LIMITED_LOG_LINES + 1:
            self._emit("further refusals and connection errors are not logged")


def _wait_until(predicate: Callable[[], bool], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True
