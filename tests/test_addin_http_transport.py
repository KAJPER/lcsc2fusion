"""Tests of the local HTTP transport and the hub (real server on an ephemeral loopback port)."""

from __future__ import annotations

import contextlib
import http.client
import json
import re
import secrets
import socket
import struct
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import http_transport as ht
import pytest
from controller import PaletteSink

# Helpers ------------------------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def wait_for(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


class _Default:
    """Marker: use the correct value of a header."""


DEFAULT = _Default()


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    def json(self) -> Any:
        return json.loads(self.body)


def request(
    port: int,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    host: _Default | str | None = DEFAULT,
    cookie: str | None = None,
    origin: str | None = None,
    timeout: float = 5.0,
) -> Response:
    """One request with full control over the Host header (None omits it)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        if isinstance(host, _Default):
            conn.putheader("Host", f"127.0.0.1:{port}")
        elif host is not None:
            conn.putheader("Host", host)
        sent = {name.lower() for name in (headers or {})}
        for name, value in (headers or {}).items():
            conn.putheader(name, value)
        if cookie is not None:
            conn.putheader("Cookie", cookie)
        if origin is not None:
            conn.putheader("Origin", origin)
        if body is not None and "content-length" not in sent:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        response = conn.getresponse()
        data = response.read()
        raw = {name.lower(): value for name, value in response.getheaders()}
        return Response(response.status, raw, data)
    finally:
        conn.close()


def raw_exchange(
    port: int, data: bytes, timeout: float = 5.0, *, half_close: bool = False
) -> bytes:
    """Send raw bytes, return everything the server answers until it closes the connection."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(data)
        if half_close:
            sock.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        while True:
            try:
                chunk = sock.recv(65536)
            except ConnectionError:
                break
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks)


def raw_status(answer: bytes) -> int:
    match = re.match(rb"HTTP/1\.[01] (\d{3}) ", answer)
    assert match is not None, answer[:80]
    return int(match.group(1))


def assert_security_headers(response: Response) -> None:
    assert response.header("cache-control") == "no-store"
    assert response.header("x-content-type-options") == "nosniff"
    assert response.header("referrer-policy") == "no-referrer"
    assert response.header("content-security-policy") == (
        "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    )
    assert response.header("x-frame-options") == "DENY"
    assert response.header("cross-origin-resource-policy") == "same-origin"
    assert not [name for name in response.headers if name.startswith("access-control-")]
    assert response.header("server") == "LCSC2Fusion"


class SseClient:
    """Minimal raw socket Server-Sent Events reader."""

    def __init__(
        self,
        port: int,
        cookie: str,
        *,
        last_event_id: str | None = None,
        host: str | None = None,
        read_preamble: bool = True,
    ) -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        lines = [
            "GET /api/events HTTP/1.1",
            f"Host: {host or f'127.0.0.1:{port}'}",
            f"Cookie: {cookie}",
        ]
        if last_event_id is not None:
            lines.append(f"Last-Event-ID: {last_event_id}")
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
        self.buf = b""
        head = self._read_until(b"\r\n\r\n", 5.0)
        assert head is not None
        status_line, *header_lines = head.decode("latin-1").split("\r\n")
        self.status = int(status_line.split()[1])
        self.headers = {
            line.split(":", 1)[0].lower(): line.split(":", 1)[1].strip()
            for line in header_lines
            if ":" in line
        }
        if read_preamble and self.status == 200:
            assert self.read_block(3.0) == "retry: 1000"

    def _read_until(self, marker: bytes, timeout: float) -> bytes | None:
        deadline = time.monotonic() + timeout
        while marker not in self.buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            self.sock.settimeout(remaining)
            try:
                chunk = self.sock.recv(65536)
            except ConnectionError:
                chunk = b""
            if not chunk:
                return None
            self.buf += chunk
        block, _, self.buf = self.buf.partition(marker)
        return block

    def read_block(self, timeout: float = 3.0) -> str | None:
        """Next blank line terminated block, None at end of stream, TimeoutError when idle."""
        block = self._read_until(b"\n\n", timeout)
        return None if block is None else block.decode("ascii")

    def read_event(self, timeout: float = 3.0) -> tuple[int | None, dict[str, Any]]:
        """Next data event as (id, parsed JSON), skipping comments. TimeoutError when idle."""
        deadline = time.monotonic() + timeout
        while True:
            block = self.read_block(max(0.0, deadline - time.monotonic()))
            assert block is not None, "stream ended"
            lines = block.split("\n")
            if all(line.startswith((":", "retry:")) for line in lines):
                continue
            event_id: int | None = None
            data = ""
            for line in lines:
                if line.startswith("id: "):
                    event_id = int(line[4:])
                elif line.startswith("data: "):
                    data = line[6:]
            return event_id, json.loads(data)

    def at_eof(self, timeout: float = 3.0) -> bool:
        """True when the server ends the stream within `timeout` (heartbeats are skipped)."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                block = self.read_block(max(0.0, deadline - time.monotonic()))
            except TimeoutError:
                return False
            if block is None:
                return True

    def close(self, *, reset: bool = False) -> None:
        if reset:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        self.sock.close()


@dataclass
class Rig:
    """A started transport with recorders for dispatch calls and log lines."""

    transport: ht.HttpTransport
    hub: ht.Hub
    clock: FakeClock
    logs: list[str] = field(default_factory=list)
    calls: list[tuple[str, str, str]] = field(default_factory=list)
    handler: Callable[[str, str], str] = lambda action, data: "OK"

    @property
    def port(self) -> int:
        return int(self.transport.url.rsplit(":", 1)[1])

    def dispatch(self, action: str, data: str) -> str:
        self.calls.append((action, data, threading.current_thread().name))
        return self.handler(action, data)

    def login(self) -> str:
        """Run the launch exchange and return the `name=value` cookie."""
        launch = self.transport.launch_url()
        response = request(self.port, "GET", launch.removeprefix(self.transport.url))
        assert response.status == 302
        cookie = response.header("set-cookie")
        assert cookie is not None
        return cookie.split(";")[0]

    def get(self, path: str, cookie: str, **kwargs: Any) -> Response:
        return request(self.port, "GET", path, cookie=cookie, **kwargs)

    def act(
        self,
        cookie: str,
        payload: Any = None,
        *,
        raw: bytes | None = None,
        content_type: str | None = "application/json",
        x_lcsc: str | None = "1",
        extra: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> Response:
        body = raw if raw is not None else json.dumps(payload).encode("utf-8")
        headers = dict(extra or {})
        if content_type is not None:
            headers["Content-Type"] = content_type
        if x_lcsc is not None:
            headers["X-LCSC"] = x_lcsc
        return request(
            self.port, "POST", "/api/act", headers=headers, body=body, cookie=cookie, **kwargs
        )


STATIC_CONTENT_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
    "http-bridge.js": "text/javascript; charset=utf-8",
    "mock-bridge.js": "text/javascript; charset=utf-8",
}


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "static"
    folder.mkdir()
    for name in STATIC_CONTENT_TYPES:
        (folder / name).write_bytes(f"content of {name}\n".encode())
    (folder / "controller.py").write_bytes(b"SECRET = 1\n")
    (folder / "secret.txt").write_bytes(b"secret\n")
    (folder / "sub").mkdir()
    (folder / "sub" / "app.js").write_bytes(b"nested\n")
    return folder


@pytest.fixture
def make_rig(static_dir: Path) -> Iterator[Callable[..., Rig]]:
    rigs: list[Rig] = []

    def factory(
        *,
        hub: ht.Hub | None = None,
        log: Callable[[str], None] | None = None,
        **kwargs: Any,
    ) -> Rig:
        clock = FakeClock()
        the_hub = hub if hub is not None else ht.Hub()
        options: dict[str, Any] = {"heartbeat_s": 0.2, "connection_timeout_s": 2.0}
        options.update(kwargs)
        holder: list[Rig] = []

        def write_log(line: str) -> None:
            holder[0].logs.append(line)
            if log is not None:
                log(line)

        transport = ht.HttpTransport(
            the_hub,
            lambda action, data: holder[0].dispatch(action, data),
            write_log,
            static_dir,
            clock=clock,
            **options,
        )
        rig = Rig(transport, the_hub, clock)
        holder.append(rig)
        rigs.append(rig)
        transport.start()
        return rig

    yield factory
    for rig in rigs:
        rig.transport.stop()


@pytest.fixture
def rig(make_rig: Callable[..., Rig]) -> Rig:
    return make_rig()


@pytest.fixture
def cookie(rig: Rig) -> str:
    return rig.login()


# Hub ----------------------------------------------------------------------------------------


def test_publish_returns_monotonic_ids_starting_at_one() -> None:
    hub = ht.Hub()
    assert [hub.publish("a", str(i)) for i in range(4)] == [1, 2, 3, 4]


def test_subscribe_without_last_id_starts_at_the_head() -> None:
    hub = ht.Hub()
    for i in range(3):
        hub.publish("old", str(i))
    sub = hub.subscribe()
    assert sub.get(0.05) is None  # no replay
    hub.publish("new", "x")
    assert sub.get(1.0) == ht.HubEvent(4, "new", "x")


def test_subscribe_with_last_id_replays_newer_events_then_goes_live() -> None:
    hub = ht.Hub()
    for i in range(1, 6):
        hub.publish("a", str(i))
    sub = hub.subscribe(2)
    hub.publish("a", "6")
    got = [sub.get(1.0) for _ in range(4)]
    assert [e.id for e in got if e] == [3, 4, 5, 6]
    assert sub.get(0.05) is None


def test_replay_from_zero_returns_the_whole_ring_and_from_a_future_id_nothing() -> None:
    hub = ht.Hub()
    for _ in range(3):
        hub.publish("a", "")
    everything = hub.subscribe(0)
    assert [everything.get(0.5).id for _ in range(3)] == [1, 2, 3]  # type: ignore[union-attr]
    future = hub.subscribe(99)
    assert future.get(0.05) is None
    hub.publish("a", "")
    event = future.get(1.0)
    assert event is not None
    assert event.id == 4  # live events are delivered whatever the stale id was


def test_ring_keeps_only_the_newest_events() -> None:
    hub = ht.Hub(ring_size=5)
    for i in range(12):
        hub.publish("a", str(i))
    sub = hub.subscribe(0)
    ids = []
    while (event := sub.get(0.05)) is not None:
        ids.append(event.id)
    assert ids == [8, 9, 10, 11, 12]


def test_hub_full_and_a_closed_subscription_frees_its_slot() -> None:
    hub = ht.Hub(max_subscribers=2)
    first = hub.subscribe()
    hub.subscribe()
    assert hub.subscriber_count == 2
    with pytest.raises(ht.HubFull):
        hub.subscribe()
    first.close()
    assert hub.subscriber_count == 1
    hub.subscribe()
    assert hub.subscriber_count == 2


def test_get_returns_none_on_timeout() -> None:
    sub = ht.Hub().subscribe()
    started = time.monotonic()
    assert sub.get(0.1) is None
    assert time.monotonic() - started >= 0.09
    assert sub.get(0) is None
    assert sub.get(-1) is None


def test_get_wakes_for_an_event_published_from_another_thread() -> None:
    hub = ht.Hub()
    sub = hub.subscribe()
    threading.Timer(0.05, hub.publish, args=("x", "y")).start()
    started = time.monotonic()
    event = sub.get(5.0)
    assert event == ht.HubEvent(1, "x", "y")
    assert time.monotonic() - started < 2.0


def test_hub_close_wakes_a_blocked_get() -> None:
    hub = ht.Hub()
    sub = hub.subscribe()
    outcome: list[str] = []

    def reader() -> None:
        try:
            sub.get(10.0)
            outcome.append("event")
        except ht.HubClosed:
            outcome.append("closed")

    thread = threading.Thread(target=reader)
    thread.start()
    time.sleep(0.05)
    hub.close()
    thread.join(2.0)
    assert not thread.is_alive()
    assert outcome == ["closed"]


def test_hub_close_lets_subscribers_drain_their_queue_first() -> None:
    hub = ht.Hub()
    sub = hub.subscribe()
    hub.publish("a", "1")
    hub.publish("b", "2")
    hub.close()
    assert sub.get(0.5) == ht.HubEvent(1, "a", "1")
    assert sub.get(0.5) == ht.HubEvent(2, "b", "2")
    with pytest.raises(ht.HubClosed):
        sub.get(0.5)
    assert hub.subscriber_count == 0


def test_publish_and_subscribe_after_close() -> None:
    hub = ht.Hub()
    hub.close()
    hub.close()  # idempotent
    assert hub.publish("a", "b") == 0
    with pytest.raises(ht.HubClosed):
        hub.subscribe()


def test_subscription_close_is_idempotent_discards_the_queue_and_ends_get() -> None:
    hub = ht.Hub()
    sub = hub.subscribe()
    hub.publish("a", "1")
    sub.close()
    sub.close()
    assert hub.subscriber_count == 0
    with pytest.raises(ht.HubClosed):
        sub.get(0.1)
    hub.publish("a", "2")  # must not fail for a closed subscriber


def test_slow_subscriber_is_dropped_with_an_overflow_marker() -> None:
    hub = ht.Hub(ring_size=3)
    slow = hub.subscribe()
    fast = hub.subscribe()
    fast_ids = []
    for i in range(10):
        hub.publish("a", str(i))
        event = fast.get(1.0)
        assert event is not None
        fast_ids.append(event.id)
    assert fast_ids == list(range(1, 11))
    assert hub.subscriber_count == 1  # the slow one is gone
    marker = slow.get(0.5)
    assert marker == ht.HubEvent(0, ht.OVERFLOW_ACTION, "{}")
    with pytest.raises(ht.HubClosed):
        slow.get(0.5)
    # It reconnects with its last id and gets what the ring still holds.
    again = hub.subscribe(0)
    assert [again.get(0.5).id for _ in range(3)] == [8, 9, 10]  # type: ignore[union-attr]


def test_the_replay_does_not_count_against_the_backlog_limit() -> None:
    hub = ht.Hub(ring_size=3)
    for _ in range(3):
        hub.publish("a", "")
    sub = hub.subscribe(0)  # 3 replayed events
    hub.publish("a", "")
    assert hub.subscriber_count == 1
    assert [sub.get(0.5).id for _ in range(4)] == [1, 2, 3, 4]  # type: ignore[union-attr]


def test_subscribers_are_independent() -> None:
    hub = ht.Hub()
    one = hub.subscribe()
    two = hub.subscribe()
    hub.publish("a", "1")
    assert one.get(0.5) == ht.HubEvent(1, "a", "1")
    assert two.get(0.5) == ht.HubEvent(1, "a", "1")
    one.close()
    hub.publish("a", "2")
    assert two.get(0.5) == ht.HubEvent(2, "a", "2")


def test_many_publishers_keep_ids_unique_and_the_stream_ordered() -> None:
    hub = ht.Hub(ring_size=10_000)
    sub = hub.subscribe()
    publishers, per_thread = 8, 400
    ids: list[int] = []
    ids_lock = threading.Lock()

    def publish() -> None:
        for i in range(per_thread):
            event_id = hub.publish("a", str(i))
            with ids_lock:
                ids.append(event_id)

    threads = [threading.Thread(target=publish) for _ in range(publishers)]
    for thread in threads:
        thread.start()
    received: list[int] = []
    while len(received) < publishers * per_thread:
        event = sub.get(5.0)
        assert event is not None
        received.append(event.id)
    for thread in threads:
        thread.join(5.0)
    assert sorted(ids) == list(range(1, publishers * per_thread + 1))
    assert received == sorted(received)
    assert received == list(range(1, publishers * per_thread + 1))


@pytest.mark.parametrize("kwargs", [{"ring_size": 0}, {"max_subscribers": 0}, {"ring_size": -1}])
def test_hub_rejects_invalid_sizes(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        ht.Hub(**kwargs)


def test_hub_sink_publishes_and_satisfies_the_palette_sink_protocol() -> None:
    hub = ht.Hub()
    sink: PaletteSink = ht.HubSink(hub)
    sub = hub.subscribe()
    sink.send("status", '{"level": "info"}')
    assert sub.get(0.5) == ht.HubEvent(1, "status", '{"level": "info"}')


def test_sse_frame_is_one_ascii_line_and_marker_events_carry_no_id() -> None:
    nasty = 'line1\nline2\r\n\u2028 "quoted" \u00e9 \U0001f600 \\ data: x'
    frame = ht.format_sse_event(ht.HubEvent(7, "status", nasty))
    text = frame.decode("ascii")
    assert text.startswith("id: 7\ndata: ")
    assert text.endswith("\n\n")
    assert text.count("\n") == 3  # id line, data line, blank line
    assert json.loads(text.split("data: ", 1)[1]) == {"action": "status", "data": nasty}
    assert not ht.format_sse_event(ht.HubEvent(0, "overflow", "{}")).startswith(b"id:")


# Lifecycle ----------------------------------------------------------------------------------


def test_start_binds_an_ephemeral_loopback_port_and_stop_releases_it(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    match = re.fullmatch(r"http://127\.0\.0\.1:(\d+)", rig.transport.url)
    assert match is not None
    assert int(match.group(1)) > 0
    assert request(rig.port, "GET", "/api/ping").status == 403  # alive
    rig.transport.stop()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", rig.port), timeout=1).close()


def test_the_serve_thread_is_a_daemon_and_ends_with_stop(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    threads = [t for t in threading.enumerate() if t.name == "lcsc2fusion-http"]
    assert len(threads) == 1
    assert threads[0].daemon
    rig.transport.stop()
    assert not threads[0].is_alive()


def test_server_class_never_reuses_the_address_and_uses_daemon_threads() -> None:
    assert ht._Server.allow_reuse_address is False
    assert ht._Server.daemon_threads is True


def test_start_twice_raises_and_stop_is_idempotent(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    with pytest.raises(RuntimeError):
        rig.transport.start()
    rig.transport.stop()
    rig.transport.stop()
    with pytest.raises(RuntimeError):
        rig.transport.start()  # one shot object


def test_stop_before_start_is_harmless(static_dir: Path) -> None:
    transport = ht.HttpTransport(ht.Hub(), lambda a, d: "OK", lambda line: None, static_dir)
    transport.stop()
    with pytest.raises(RuntimeError):
        transport.launch_url()
    with pytest.raises(RuntimeError):
        _ = transport.url


@pytest.mark.parametrize("host", ["0.0.0.0", "localhost", "::1", "192.168.1.10", "", "127.0.0.2"])
def test_non_loopback_hosts_are_refused(static_dir: Path, host: str) -> None:
    with pytest.raises(ValueError, match="host"):
        ht.HttpTransport(ht.Hub(), lambda a, d: "OK", lambda line: None, static_dir, host=host)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"port": -1},
        {"port": 70000},
        {"heartbeat_s": 0},
        {"dispatch_timeout_s": -1},
        {"connection_timeout_s": 0},
    ],
)
def test_invalid_numbers_are_refused(static_dir: Path, kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        ht.HttpTransport(ht.Hub(), lambda a, d: "OK", lambda line: None, static_dir, **kwargs)


def test_port_in_use_raises_oserror(static_dir: Path) -> None:
    blocker = socket.socket()
    try:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        transport = ht.HttpTransport(
            ht.Hub(), lambda a, d: "OK", lambda line: None, static_dir, port=port
        )
        with pytest.raises(OSError):
            transport.start()
        transport.stop()  # never started: still fine
    finally:
        blocker.close()


def test_stop_immediately_after_start_never_hangs(make_rig: Callable[..., Rig]) -> None:
    for _ in range(5):
        rig = make_rig()
        started = time.monotonic()
        rig.transport.stop()
        assert time.monotonic() - started < 3.0


def test_stop_ends_idle_connections_quickly(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig(connection_timeout_s=30.0)
    idle = [socket.create_connection(("127.0.0.1", rig.port)) for _ in range(3)]
    time.sleep(0.1)
    started = time.monotonic()
    rig.transport.stop()
    assert time.monotonic() - started < 1.5
    for sock in idle:
        sock.settimeout(2.0)
        with contextlib.suppress(ConnectionError):
            assert sock.recv(10) == b""
        sock.close()


def test_stop_is_bounded_even_with_a_stalled_partial_request(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig(connection_timeout_s=30.0)
    loris = socket.create_connection(("127.0.0.1", rig.port))
    loris.sendall(b"GET /api/ping HTTP/1.1\r\nHost: 127.0.0.1")
    time.sleep(0.1)
    started = time.monotonic()
    rig.transport.stop()
    assert time.monotonic() - started < 6.0
    loris.close()


# Launch token and session cookie -----------------------------------------------------------


def test_launch_url_carries_a_128_bit_url_safe_token(rig: Rig) -> None:
    urls = [rig.transport.launch_url() for _ in range(50)]
    prefix = rig.transport.url + "/?t="
    tokens = [u.removeprefix(prefix) for u in urls]
    assert all(u.startswith(prefix) for u in urls)
    assert len(set(tokens)) == 50
    for token in tokens:
        assert re.fullmatch(r"[A-Za-z0-9_-]{22,}", token)  # 16 random bytes, base64 encoded


def test_token_exchange_sets_the_session_cookie_and_redirects(rig: Rig) -> None:
    launch = rig.transport.launch_url()
    response = request(rig.port, "GET", launch.removeprefix(rig.transport.url))
    assert response.status == 302
    assert response.header("location") == "/"
    assert response.body == b""
    assert response.header("content-length") == "0"
    cookie = response.header("set-cookie")
    assert cookie is not None
    name_value, *attributes = [part.strip() for part in cookie.split(";")]
    name, _, value = name_value.partition("=")
    assert name == "lcsc_s"
    assert re.fullmatch(r"[A-Za-z0-9_-]{40,}", value)
    assert attributes == ["HttpOnly", "SameSite=Strict", "Path=/"]
    assert_security_headers(response)
    assert rig.get("/api/ping", name_value).status == 200


def test_a_replayed_token_is_refused_without_a_cookie(rig: Rig) -> None:
    path = rig.transport.launch_url().removeprefix(rig.transport.url)
    assert request(rig.port, "GET", path).status == 302
    again = request(rig.port, "GET", path)
    assert again.status == 403
    assert again.header("set-cookie") is None
    assert again.header("location") is None
    assert again.header("content-type") == "text/plain; charset=utf-8"
    assert len(again.body) < 64
    assert_security_headers(again)


def test_an_expired_token_is_refused(rig: Rig) -> None:
    path = rig.transport.launch_url().removeprefix(rig.transport.url)
    rig.clock.advance(61)
    response = request(rig.port, "GET", path)
    assert response.status == 403
    assert response.header("set-cookie") is None


def test_a_token_is_valid_until_sixty_seconds_have_passed(rig: Rig) -> None:
    inside = rig.transport.launch_url().removeprefix(rig.transport.url)
    boundary = rig.transport.launch_url().removeprefix(rig.transport.url)
    rig.clock.advance(59.9)
    assert request(rig.port, "GET", inside).status == 302
    rig.clock.advance(0.1)  # exactly 60 s after issue
    assert request(rig.port, "GET", boundary).status == 403


def test_several_launches_give_independent_sessions_that_all_stay_valid(rig: Rig) -> None:
    first = rig.login()
    second = rig.login()
    assert first != second
    assert rig.get("/api/ping", first).status == 200
    assert rig.get("/api/ping", second).status == 200


@pytest.mark.parametrize(
    "query",
    ["t=", "t=x", "t=" + "A" * 22, "t=%FF%FE", "t=a&t=b", "t=%00", "t=" + "A" * 5000, "T=whatever"],
)
def test_bad_tokens_are_refused(rig: Rig, query: str) -> None:
    response = request(rig.port, "GET", "/?" + query)
    assert response.status == 403
    assert response.header("set-cookie") is None


def test_a_slightly_wrong_token_is_refused_and_the_real_one_still_works(rig: Rig) -> None:
    token = rig.transport.launch_url().rsplit("=", 1)[1]
    for variant in (token[:-1], token + "x", token.swapcase(), token[::-1]):
        assert request(rig.port, "GET", f"/?t={variant}").status == 403
    assert request(rig.port, "GET", f"/?t={token}").status == 302


def test_a_token_is_only_accepted_on_the_root_path(rig: Rig) -> None:
    token = rig.transport.launch_url().rsplit("=", 1)[1]
    assert request(rig.port, "GET", f"/app.js?t={token}").status == 403
    assert request(rig.port, "GET", f"/api/ping?t={token}").status == 403
    assert request(rig.port, "POST", f"/?t={token}", body=b"{}").status == 403
    assert request(rig.port, "GET", f"/?t={token}").status == 302  # untouched by the above


def test_a_refused_request_does_not_consume_the_token(rig: Rig) -> None:
    token = rig.transport.launch_url().rsplit("=", 1)[1]
    assert request(rig.port, "GET", f"/?t={token}", host="evil.com").status == 403
    assert request(rig.port, "GET", f"/?t={token}", origin="http://evil.com").status == 403
    assert request(rig.port, "GET", f"/?t={token}").status == 302


def test_a_valid_cookie_does_not_rescue_a_bad_token(rig: Rig, cookie: str) -> None:
    response = request(rig.port, "GET", "/?t=nope", cookie=cookie)
    assert response.status == 403


def test_the_root_without_a_token_needs_a_session(rig: Rig, cookie: str) -> None:
    assert request(rig.port, "GET", "/").status == 403
    assert rig.get("/", cookie).status == 200


def test_cookie_values_are_compared_with_compare_digest(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = rig.login(), rig.login()
    seen: list[tuple[bytes, bytes]] = []
    real = secrets.compare_digest

    def spy(a: Any, b: Any) -> bool:
        seen.append((a, b))
        return real(a, b)

    monkeypatch.setattr(ht.secrets, "compare_digest", spy)
    assert rig.get("/api/ping", second).status == 200
    candidates = {a for a, _ in seen}
    assert second.partition("=")[2].encode() in candidates
    assert len(seen) >= 2  # compared against every known session, not just until a match
    assert first.partition("=")[2].encode() in {b for _, b in seen}


@pytest.mark.parametrize(
    "mangle",
    [
        lambda c: c + "x",
        lambda c: c[:-1],
        lambda c: c[:-1] + ("A" if c[-1] != "A" else "B"),
        lambda c: c.partition("=")[0] + "=",
        lambda c: "other=" + c.partition("=")[2],
        lambda c: "x" + c,
        lambda c: c.partition("=")[0] + "=%s" % ("A" * 5000),
        lambda c: c.partition("=")[0].upper() + "=" + c.partition("=")[2],
    ],
)
def test_wrong_or_malformed_cookies_are_refused(
    rig: Rig, cookie: str, mangle: Callable[[str], str]
) -> None:
    response = rig.get("/api/ping", mangle(cookie))
    assert response.status == 403
    assert response.header("set-cookie") is None


def test_the_cookie_may_sit_among_other_cookies(rig: Rig, cookie: str) -> None:
    assert rig.get("/api/ping", f"a=b; {cookie}; c=d").status == 200
    assert rig.get("/api/ping", f"lcsc_s=wrong; {cookie}").status == 200
    assert rig.get("/api/ping", "a=b; c=d").status == 403


# Request refusals ---------------------------------------------------------------------------

FOREIGN_HOSTS = [
    "localhost:{port}",
    "evil.com",
    "evil.com:{port}",
    "127.0.0.1",
    "127.0.0.1:1",
    "127.0.0.1:{port}0",
    "[::1]:{port}",
    "LOCALHOST:{port}",
    "127.0.0.1.evil.com:{port}",
    "0.0.0.0:{port}",
    "",
]


@pytest.mark.parametrize("host", FOREIGN_HOSTS)
@pytest.mark.parametrize("path", ["/api/ping", "/", "/app.js", "/api/events"])
def test_wrong_host_is_refused(rig: Rig, cookie: str, host: str, path: str) -> None:
    response = rig.get(path, cookie, host=host.format(port=rig.port))
    assert response.status == 403
    assert response.header("set-cookie") is None
    assert_security_headers(response)


def test_missing_host_is_refused(rig: Rig, cookie: str) -> None:
    response = rig.get("/api/ping", cookie, host=None)
    assert response.status == 403
    answer = raw_exchange(
        rig.port, f"GET /api/ping HTTP/1.0\r\nCookie: {cookie}\r\n\r\n".encode("ascii")
    )
    assert raw_status(answer) == 403


def test_duplicate_host_headers_are_refused(rig: Rig, cookie: str) -> None:
    host = f"127.0.0.1:{rig.port}"
    data = f"GET /api/ping HTTP/1.1\r\nHost: {host}\r\nHost: {host}\r\nCookie: {cookie}\r\n\r\n"
    assert raw_status(raw_exchange(rig.port, data.encode("ascii"))) == 403


def test_host_is_checked_before_the_session(rig: Rig) -> None:
    for host in ("evil.com", None):
        response = request(rig.port, "GET", "/api/ping", host=host)
        assert response.status == 403


@pytest.mark.parametrize(
    "path", ["/", "/app.js", "/api/ping", "/api/events", "/favicon.ico", "/nonexistent", "/../x"]
)
def test_get_without_a_session_is_refused_with_403(rig: Rig, path: str) -> None:
    response = request(rig.port, "GET", path)
    assert response.status == 403
    assert response.header("set-cookie") is None
    assert len(response.body) < 64
    assert_security_headers(response)


def test_post_without_a_session_is_refused_and_not_dispatched(rig: Rig) -> None:
    response = request(
        rig.port,
        "POST",
        "/api/act",
        headers={"Content-Type": "application/json", "X-LCSC": "1"},
        body=b'{"action": "a", "data": "b"}',
    )
    assert response.status == 403
    assert rig.calls == []


@pytest.mark.parametrize(
    "origin",
    [
        "http://evil.com",
        "http://127.0.0.1:1",
        "null",
        "http://localhost:{port}",
        "https://127.0.0.1:{port}",
        "http://127.0.0.1:{port}/",
        "http://127.0.0.1",
        "",
    ],
)
def test_foreign_origin_is_refused(rig: Rig, cookie: str, origin: str) -> None:
    origin = origin.format(port=rig.port)
    get = rig.get("/api/ping", cookie, origin=origin)
    assert get.status == 403
    assert_security_headers(get)
    post = rig.act(cookie, {"action": "a", "data": "b"}, origin=origin)
    assert post.status == 403
    assert rig.calls == []


def test_own_origin_is_accepted(rig: Rig, cookie: str) -> None:
    origin = f"http://127.0.0.1:{rig.port}"
    assert rig.get("/api/ping", cookie, origin=origin).status == 200
    assert rig.act(cookie, {"action": "a", "data": "b"}, origin=origin).status == 200


def test_duplicate_origin_headers_are_refused(rig: Rig, cookie: str) -> None:
    origin = f"http://127.0.0.1:{rig.port}"
    data = (
        f"GET /api/ping HTTP/1.1\r\nHost: 127.0.0.1:{rig.port}\r\nCookie: {cookie}\r\n"
        f"Origin: {origin}\r\nOrigin: {origin}\r\n\r\n"
    )
    assert raw_status(raw_exchange(rig.port, data.encode("ascii"))) == 403


def test_post_without_the_x_lcsc_header_is_refused(rig: Rig, cookie: str) -> None:
    payload = {"action": "a", "data": "b"}
    assert rig.act(cookie, payload, x_lcsc=None).status == 403
    for value in ("0", "true", "", "11"):
        assert rig.act(cookie, payload, x_lcsc=value).status == 403
    assert rig.calls == []


@pytest.mark.parametrize(
    "content_type",
    [
        "text/plain",
        "application/x-www-form-urlencoded",
        "multipart/form-data; boundary=x",
        "application/jsonx",
        "text/json",
        None,
    ],
)
def test_post_with_another_content_type_is_refused_with_415(
    rig: Rig, cookie: str, content_type: str | None
) -> None:
    response = rig.act(cookie, {"action": "a", "data": "b"}, content_type=content_type)
    assert response.status == 415
    assert rig.calls == []


@pytest.mark.parametrize(
    "content_type", ["application/json", "application/json; charset=utf-8", "Application/JSON"]
)
def test_json_content_types_are_accepted(rig: Rig, cookie: str, content_type: str) -> None:
    assert rig.act(cookie, {"action": "a", "data": "b"}, content_type=content_type).status == 200


def test_an_oversized_body_is_refused_with_413_and_never_dispatched(rig: Rig, cookie: str) -> None:
    response = rig.act(cookie, raw=b"x" * (ht.MAX_BODY_BYTES + 1))
    assert response.status == 413
    assert_security_headers(response)
    assert rig.calls == []
    # A declared 4 MiB body is refused from the header alone, the server never waits for it.
    declared = (
        f"POST /api/act HTTP/1.1\r\nHost: 127.0.0.1:{rig.port}\r\nCookie: {cookie}\r\n"
        f"Content-Type: application/json\r\nX-LCSC: 1\r\nContent-Length: {4 << 20}\r\n\r\n"
    )
    started = time.monotonic()
    with socket.create_connection(("127.0.0.1", rig.port), timeout=5) as sock:
        sock.sendall(declared.encode("ascii"))
        assert raw_status(sock.recv(65536)) == 413
    assert time.monotonic() - started < 1.0
    assert rig.calls == []


def test_a_body_of_exactly_the_limit_is_accepted(rig: Rig, cookie: str) -> None:
    core = b'{"action": "big", "data": "x"}'
    body = core + b" " * (ht.MAX_BODY_BYTES - len(core))
    assert len(body) == ht.MAX_BODY_BYTES
    assert rig.act(cookie, raw=body).status == 200
    assert rig.calls[0][0] == "big"


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"[]",
        b'"text"',
        b"null",
        b"42",
        b'{"action": "a"}',
        b'{"data": "d"}',
        b'{"action": 1, "data": "d"}',
        b'{"action": "a", "data": 1}',
        b'{"action": "a", "data": null}',
        b'{"action": "a", "data": {"x": 1}}',
        b'{"action": ["a"], "data": "d"}',
        b'{"action": "", "data": "d"}',
        b"\xff\xfe\xfd",
        pytest.param(b"[" * 60000, id="deeply-nested"),
        b'{"action": "a", "data": "d"',
    ],
)
def test_invalid_json_or_fields_are_refused_with_400(rig: Rig, cookie: str, body: bytes) -> None:
    response = rig.act(cookie, raw=body)
    assert response.status == 400
    assert rig.calls == []


def test_field_length_limits(rig: Rig, cookie: str) -> None:
    assert rig.act(cookie, {"action": "a" * 65, "data": ""}).status == 400
    assert rig.act(cookie, {"action": "a", "data": "d" * 60001}).status == 400
    assert rig.calls == []
    assert rig.act(cookie, {"action": "a" * 64, "data": "d" * 60000}).status == 200
    assert len(rig.calls) == 1


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ("", 411),
        ("Content-Length: abc\r\n", 400),
        ("Content-Length: -5\r\n", 400),
        ("Content-Length: +5\r\n", 400),
        ("Content-Length: 5\r\nContent-Length: 6\r\n", 400),
        ("Transfer-Encoding: chunked\r\n", 501),
    ],
)
def test_bad_framing_of_a_post_is_refused(
    rig: Rig, cookie: str, headers: str, expected: int
) -> None:
    data = (
        f"POST /api/act HTTP/1.1\r\nHost: 127.0.0.1:{rig.port}\r\nCookie: {cookie}\r\n"
        f"Content-Type: application/json\r\nX-LCSC: 1\r\n{headers}\r\n5\r\nhello\r\n0\r\n\r\n"
    )
    answer = raw_exchange(rig.port, data.encode("ascii"))
    assert raw_status(answer) == expected
    assert rig.calls == []


def test_a_truncated_body_is_refused(rig: Rig, cookie: str) -> None:
    data = (
        f"POST /api/act HTTP/1.1\r\nHost: 127.0.0.1:{rig.port}\r\nCookie: {cookie}\r\n"
        f"Content-Type: application/json\r\nX-LCSC: 1\r\nContent-Length: 100\r\n\r\n"
        f'{{"action": "a"'
    )
    answer = raw_exchange(rig.port, data.encode("ascii"), half_close=True)
    assert raw_status(answer) == 400
    assert rig.calls == []


def test_a_pipelined_second_request_is_never_executed(rig: Rig, cookie: str) -> None:
    """The connection closes after one response, so smuggled bytes after a body are ignored."""

    def post(action: str) -> bytes:
        body = json.dumps({"action": action, "data": ""}).encode("ascii")
        head = (
            f"POST /api/act HTTP/1.1\r\nHost: 127.0.0.1:{rig.port}\r\nCookie: {cookie}\r\n"
            f"Content-Type: application/json\r\nX-LCSC: 1\r\nContent-Length: {len(body)}\r\n\r\n"
        )
        return head.encode("ascii") + body

    answer = raw_exchange(rig.port, post("first") + post("second"))
    assert raw_status(answer) == 200
    assert [call[0] for call in rig.calls] == ["first"]


@pytest.mark.parametrize(
    "path",
    [
        "/nonexistent",
        "/../x",
        "/%2e%2e/x",
        "/%2E%2E/x",
        "/palette/../controller.py",
        "/app.js/",
        "//app.js",
        "///app.js",
        "/..\\controller.py",
        "/%5c..%5ccontroller.py",
        "/controller.py",
        "/secret.txt",
        "/sub/app.js",
        "/APP.JS",
        "/app.js%00",
        "/app.js%2f",
        "/%61pp.js",
        "/.",
        "/app.js;x",
        "/index.html/",
        "/app.js.",
        "/controller.py?x=/app.js",
        "/controller.py?/app.js",
        "/api/",
        "/api/act/",
        "/api/unknown",
        "/static/app.js",
        "http://127.0.0.1:{port}/controller.py",
        "*",
    ],
)
def test_unknown_paths_and_traversal_attempts_get_404(rig: Rig, cookie: str, path: str) -> None:
    response = rig.get(path.format(port=rig.port), cookie)
    assert response.status == 404
    assert b"SECRET" not in response.body
    assert b"secret" not in response.body
    assert_security_headers(response)


def test_raw_traversal_targets_get_404_or_a_protocol_error(rig: Rig, cookie: str) -> None:
    for target in ("/../../../../Windows/win.ini", "/%2e%2e%5c%2e%2e%5cwin.ini", "/\\..\\x"):
        data = (
            f"GET {target} HTTP/1.1\r\nHost: 127.0.0.1:{rig.port}\r\nCookie: {cookie}\r\n\r\n"
        ).encode("ascii")
        assert raw_status(raw_exchange(rig.port, data)) == 404


@pytest.mark.parametrize("method", ["PUT", "DELETE", "OPTIONS", "PATCH", "TRACE", "HEAD"])
def test_other_methods_get_405_without_cors_headers(rig: Rig, cookie: str, method: str) -> None:
    for path in ("/", "/api/act", "/api/ping", "/nonexistent"):
        response = request(rig.port, method, path, cookie=cookie)
        assert response.status == 405
        assert response.header("allow") == "GET, POST"
        if method == "HEAD":
            assert response.body == b""
        assert_security_headers(response)


def test_a_preflight_request_is_never_answered_with_cors_headers(rig: Rig, cookie: str) -> None:
    headers = {
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "x-lcsc, content-type",
    }
    own = request(
        rig.port,
        "OPTIONS",
        "/api/act",
        cookie=cookie,
        origin=f"http://127.0.0.1:{rig.port}",
        headers=headers,
    )
    foreign = request(
        rig.port, "OPTIONS", "/api/act", cookie=cookie, origin="http://evil.com", headers=headers
    )
    anonymous = request(rig.port, "OPTIONS", "/api/act", origin="http://evil.com", headers=headers)
    assert (own.status, foreign.status, anonymous.status) == (405, 403, 403)
    for response in (own, foreign, anonymous):
        assert_security_headers(response)


def test_other_methods_without_a_session_are_403_not_405(rig: Rig) -> None:
    for method in ("PUT", "DELETE", "OPTIONS", "HEAD"):
        assert request(rig.port, method, "/api/act").status == 403


def test_an_unknown_method_gets_a_protocol_error_with_the_security_headers(rig: Rig) -> None:
    response = request(rig.port, "FROBNICATE", "/")
    assert response.status == 501
    assert_security_headers(response)


@pytest.mark.parametrize(
    ("method", "path", "allow"),
    [
        ("GET", "/api/act", "POST"),
        ("POST", "/api/ping", "GET"),
        ("POST", "/api/events", "GET"),
        ("POST", "/", "GET"),
        ("POST", "/app.js", "GET"),
        ("POST", "/favicon.ico", "GET"),
    ],
)
def test_a_known_path_with_the_wrong_method_gets_405(
    rig: Rig, cookie: str, method: str, path: str, allow: str
) -> None:
    response = request(
        rig.port, method, path, cookie=cookie, body=b"{}" if method == "POST" else None
    )
    assert response.status == 405
    assert response.header("allow") == allow
    assert rig.calls == []


def test_malformed_request_lines_are_refused_cleanly(rig: Rig) -> None:
    for data in (b"GARBAGE\r\n\r\n", b"GET / HTTP/9.9\r\n\r\n", b"\x00\x01\x02\r\n\r\n"):
        answer = raw_exchange(rig.port, data)
        # Garbage may be dropped, answered like HTTP/0.9 (body only) or with a proper error.
        assert b"Traceback" not in answer
        assert len(answer) < 1000
        if answer.startswith(b"HTTP/"):
            assert raw_status(answer) in (400, 403, 501, 505)
            assert b"Cache-Control: no-store" in answer
    assert request(rig.port, "GET", "/api/ping").status == 403  # still serving


def test_very_long_request_lines_and_header_floods_are_refused(rig: Rig, cookie: str) -> None:
    long_line = b"GET /" + b"a" * 70000 + b" HTTP/1.1\r\n\r\n"
    assert raw_status(raw_exchange(rig.port, long_line)) == 414
    many = "".join(f"X-{i}: v\r\n" for i in range(200))
    data = f"GET /api/ping HTTP/1.1\r\nHost: 127.0.0.1:{rig.port}\r\n{many}\r\n"
    assert raw_status(raw_exchange(rig.port, data.encode("ascii"))) == 431


# Static files -------------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "content_type"), STATIC_CONTENT_TYPES.items())
def test_allow_listed_files_are_served_with_type_and_security_headers(
    rig: Rig, cookie: str, name: str, content_type: str
) -> None:
    response = rig.get("/" + name, cookie)
    assert response.status == 200
    assert response.header("content-type") == content_type
    assert response.body == f"content of {name}\n".encode()
    assert response.header("content-length") == str(len(response.body))
    assert_security_headers(response)


def test_the_root_serves_index_html(rig: Rig, cookie: str) -> None:
    root = rig.get("/", cookie)
    index = rig.get("/index.html", cookie)
    assert root.status == 200
    assert root.body == index.body == b"content of index.html\n"
    assert root.header("content-type") == "text/html; charset=utf-8"


def test_the_query_string_is_ignored_for_allow_listed_files(rig: Rig, cookie: str) -> None:
    assert rig.get("/app.js?v=3", cookie).status == 200
    assert rig.get("/?x=1", cookie).body == b"content of index.html\n"


def test_files_are_read_on_every_request(rig: Rig, cookie: str, static_dir: Path) -> None:
    assert rig.get("/app.js", cookie).body == b"content of app.js\n"
    (static_dir / "app.js").write_bytes(b"changed\n")
    assert rig.get("/app.js", cookie).body == b"changed\n"


def test_an_allow_listed_name_that_is_missing_or_a_directory_gets_404(
    rig: Rig, cookie: str, static_dir: Path
) -> None:
    (static_dir / "style.css").unlink()
    (static_dir / "http-bridge.js").unlink()
    (static_dir / "http-bridge.js").mkdir()
    assert rig.get("/style.css", cookie).status == 404
    assert rig.get("/http-bridge.js", cookie).status == 404
    assert rig.get("/app.js", cookie).status == 200


def test_files_beside_the_allow_list_are_never_served(rig: Rig, cookie: str) -> None:
    for name in ("controller.py", "secret.txt", "sub/app.js", "sub/"):
        response = rig.get("/" + name, cookie)
        assert response.status == 404
        assert b"SECRET" not in response.body


def test_favicon_is_an_empty_204(rig: Rig, cookie: str) -> None:
    response = rig.get("/favicon.ico", cookie)
    assert response.status == 204
    assert response.body == b""
    assert_security_headers(response)


def test_head_is_not_a_way_to_probe_files(rig: Rig, cookie: str) -> None:
    response = request(rig.port, "HEAD", "/app.js", cookie=cookie)
    assert response.status == 405
    assert response.body == b""


# /api/act -----------------------------------------------------------------------------------


def test_dispatch_gets_the_exact_strings_and_the_reply_is_passed_through(
    rig: Rig, cookie: str
) -> None:
    data = '{"query": "0603 100nF", "note": "caf\u00e9 \u4e2d\u6587 \U0001f600 \\" \\\\ \\n"}\n\t'
    rig.handler = lambda action, payload: f"echo:{action}:{payload}"
    response = rig.act(cookie, {"action": "search", "data": data})
    assert response.status == 200
    assert response.header("content-type") == "application/json; charset=utf-8"
    assert response.json() == {"reply": f"echo:search:{data}"}
    assert rig.calls[0][:2] == ("search", data)
    assert_security_headers(response)


def test_dispatch_runs_in_a_request_thread_and_empty_data_is_allowed(rig: Rig, cookie: str) -> None:
    assert rig.act(cookie, {"action": "ready", "data": ""}).json() == {"reply": "OK"}
    assert rig.calls[0][1] == ""
    assert rig.calls[0][2] != threading.main_thread().name


def test_a_lone_surrogate_in_the_data_does_not_break_the_reply(rig: Rig, cookie: str) -> None:
    rig.handler = lambda action, payload: payload
    response = rig.act(cookie, raw=b'{"action": "a", "data": "\\ud800x"}')
    assert response.status == 200
    assert response.json() == {"reply": "\ud800x"}


def test_a_timeout_in_dispatch_answers_504(rig: Rig, cookie: str) -> None:
    def slow(action: str, data: str) -> str:
        raise TimeoutError("took too long, internal detail")

    rig.handler = slow
    response = rig.act(cookie, {"action": "a", "data": ""})
    assert response.status == 504
    assert response.json() == {"reply": "ERROR", "error": "timeout"}
    assert b"internal detail" not in response.body
    assert_security_headers(response)


def test_an_exception_in_dispatch_answers_500_without_leaking_the_message(
    rig: Rig, cookie: str
) -> None:
    def broken(action: str, data: str) -> str:
        raise RuntimeError("secret internal detail C:\\Users\\x")

    rig.handler = broken
    response = rig.act(cookie, {"action": "a", "data": ""})
    assert response.status == 500
    assert response.json() == {"reply": "ERROR"}
    assert b"secret" not in response.body
    lines = [line for line in rig.logs if "failed" in line]
    assert len(lines) == 1
    assert "RuntimeError" in lines[0]
    assert "\n" not in lines[0]
    assert "Traceback" not in lines[0]
    assert request(rig.port, "GET", "/api/ping", cookie=cookie).status == 200  # still serving


def test_concurrent_posts_run_concurrently_and_hold_no_transport_lock(
    rig: Rig, cookie: str
) -> None:
    parties = 6
    barrier = threading.Barrier(parties, timeout=5.0)

    def meet(action: str, data: str) -> str:
        barrier.wait()  # every dispatch must be running at the same time to get past this
        return data

    rig.handler = meet
    results: list[Response] = []

    def post(index: int) -> None:
        results.append(rig.act(cookie, {"action": "a", "data": str(index)}, timeout=10.0))

    threads = [threading.Thread(target=post, args=(i,)) for i in range(parties)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10.0)
    assert sorted(r.json()["reply"] for r in results) == [str(i) for i in range(parties)]
    assert all(r.status == 200 for r in results)


def test_a_blocked_dispatch_does_not_block_other_requests(rig: Rig, cookie: str) -> None:
    release = threading.Event()
    entered = threading.Event()

    def block(action: str, data: str) -> str:
        entered.set()
        release.wait(10.0)
        return "late"

    rig.handler = block
    result: list[Response] = []
    thread = threading.Thread(
        target=lambda: result.append(rig.act(cookie, {"action": "a", "data": ""}, timeout=10))
    )
    thread.start()
    assert entered.wait(3.0)
    started = time.monotonic()
    assert rig.get("/api/ping", cookie).status == 200
    assert rig.get("/app.js", cookie).status == 200
    assert rig.transport.launch_url()  # takes the auth lock
    assert time.monotonic() - started < 2.0
    release.set()
    thread.join(5.0)
    assert result[0].json() == {"reply": "late"}


def test_dispatch_is_not_blocked_by_an_open_event_stream(rig: Rig, cookie: str) -> None:
    def reply_and_publish(action: str, data: str) -> str:
        rig.hub.publish("status", json.dumps({"text": data}))
        return "OK"

    rig.handler = reply_and_publish
    stream = SseClient(rig.port, cookie)
    try:
        assert rig.act(cookie, {"action": "a", "data": "hello"}).json() == {"reply": "OK"}
        event_id, payload = stream.read_event()
        assert event_id == 1
        assert payload == {"action": "status", "data": '{"text": "hello"}'}
    finally:
        stream.close()


# Server-Sent Events -------------------------------------------------------------------------


def test_the_event_stream_headers(rig: Rig, cookie: str) -> None:
    stream = SseClient(rig.port, cookie)
    try:
        assert stream.status == 200
        assert stream.headers["content-type"] == "text/event-stream; charset=utf-8"
        assert "content-length" not in stream.headers
        assert stream.headers["connection"] == "close"
        assert stream.headers["cache-control"] == "no-store"
        assert stream.headers["x-content-type-options"] == "nosniff"
        assert stream.headers["cross-origin-resource-policy"] == "same-origin"
        assert not [h for h in stream.headers if h.startswith("access-control-")]
    finally:
        stream.close()


def test_events_arrive_in_order_after_publish(rig: Rig, cookie: str) -> None:
    stream = SseClient(rig.port, cookie)
    try:
        for i in range(5):
            rig.hub.publish("status", json.dumps({"n": i}))
        got = [stream.read_event() for _ in range(5)]
        assert [event_id for event_id, _ in got] == [1, 2, 3, 4, 5]
        assert [json.loads(p["data"])["n"] for _, p in got] == [0, 1, 2, 3, 4]
        assert all(p["action"] == "status" for _, p in got)
    finally:
        stream.close()


def test_awkward_payloads_stay_on_one_data_line(rig: Rig, cookie: str) -> None:
    nasty = "a\nb\r\nc\u2028d\u2029e \u00e9\U0001f600 data: x\n\n"
    stream = SseClient(rig.port, cookie)
    try:
        rig.hub.publish("status", nasty)
        rig.hub.publish("status", "after")
        assert stream.read_event()[1]["data"] == nasty
        assert stream.read_event()[1]["data"] == "after"
    finally:
        stream.close()


def test_last_event_id_replays_missed_events_then_continues_live(rig: Rig, cookie: str) -> None:
    for i in range(1, 6):
        rig.hub.publish("status", str(i))
    stream = SseClient(rig.port, cookie, last_event_id="3")
    try:
        assert [stream.read_event()[0] for _ in range(2)] == [4, 5]
        rig.hub.publish("status", "6")
        assert stream.read_event()[0] == 6
    finally:
        stream.close()


def test_a_fresh_connection_does_not_replay_the_ring(rig: Rig, cookie: str) -> None:
    for i in range(3):
        rig.hub.publish("status", str(i))
    stream = SseClient(rig.port, cookie)
    try:
        with pytest.raises(TimeoutError):
            stream.read_event(0.5)  # a heartbeat is fine, an event is not
        rig.hub.publish("status", "new")
        event_id, payload = stream.read_event()
        assert (event_id, payload["data"]) == (4, "new")
    finally:
        stream.close()


@pytest.mark.parametrize(
    "garbage", ["abc", "-1", "1.5", "", " ", "0x10", "9" * 40, "1 2", "\u00e9"]
)
def test_a_garbage_last_event_id_counts_as_absent(rig: Rig, cookie: str, garbage: str) -> None:
    for i in range(3):
        rig.hub.publish("status", str(i))
    sock = socket.create_connection(("127.0.0.1", rig.port), timeout=5)
    try:
        head = (
            f"GET /api/events HTTP/1.1\r\nHost: 127.0.0.1:{rig.port}\r\nCookie: {cookie}\r\n"
            f"Last-Event-ID: {garbage}\r\n\r\n"
        )
        sock.sendall(head.encode("latin-1"))
        time.sleep(0.3)  # long enough for a replay to have been written
        sock.settimeout(0.1)
        data = b""
        try:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
        except TimeoutError:
            pass
        assert b"200" in data.split(b"\r\n", 1)[0]
        assert b"data:" not in data
    finally:
        sock.close()


def test_heartbeat_comments_arrive_while_idle(rig: Rig, cookie: str) -> None:
    stream = SseClient(rig.port, cookie)
    try:
        started = time.monotonic()
        assert stream.read_block(3.0) == ": ping"
        assert stream.read_block(3.0) == ": ping"
        assert time.monotonic() - started < 2.0
        rig.hub.publish("status", "x")
        assert stream.read_event()[0] == 1
    finally:
        stream.close()


def test_a_published_event_resets_the_heartbeat_timer(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig(heartbeat_s=0.6)
    stream = SseClient(rig.port, rig.login())
    try:
        for _ in range(4):
            time.sleep(0.3)
            rig.hub.publish("status", "tick")
        events = 0
        while events < 4:
            block = stream.read_block(1.0)
            assert block is not None
            assert block != ": ping"  # events every 0.3 s keep the 0.6 s timer from firing
            events += 1
    finally:
        stream.close()


def test_the_stream_ends_when_the_transport_stops(rig: Rig, cookie: str) -> None:
    stream = SseClient(rig.port, cookie)
    try:
        started = time.monotonic()
        rig.transport.stop()
        assert time.monotonic() - started < 3.0
        assert stream.at_eof(2.0)
    finally:
        stream.close()


def test_events_published_before_stop_are_still_delivered(rig: Rig, cookie: str) -> None:
    stream = SseClient(rig.port, cookie)
    try:
        rig.hub.publish("shutdown", "{}")
        rig.transport.stop()
        event_id, payload = stream.read_event()
        assert (event_id, payload["action"]) == (1, "shutdown")
        assert stream.at_eof(2.0)
    finally:
        stream.close()


def test_the_stream_ends_when_the_hub_closes(rig: Rig, cookie: str) -> None:
    stream = SseClient(rig.port, cookie)
    try:
        rig.hub.publish("status", "last")
        rig.hub.close()
        assert stream.read_event()[1]["data"] == "last"
        assert stream.at_eof(2.0)
        assert wait_for(lambda: rig.hub.subscriber_count == 0)
    finally:
        stream.close()


@pytest.mark.parametrize("reset", [False, True])
def test_a_disconnecting_client_frees_its_subscription_promptly(
    rig: Rig, cookie: str, reset: bool
) -> None:
    stream = SseClient(rig.port, cookie)
    assert rig.get("/api/ping", cookie).json() == {"ok": True, "subscribers": 1}
    started = time.monotonic()
    stream.close(reset=reset)
    assert wait_for(lambda: rig.hub.subscriber_count == 0, 2.0)
    assert time.monotonic() - started < 1.5  # well below the 15 s heartbeat default
    assert rig.get("/api/ping", cookie).json() == {"ok": True, "subscribers": 0}


def test_a_disconnect_is_noticed_even_while_events_keep_flowing(rig: Rig, cookie: str) -> None:
    stream = SseClient(rig.port, cookie)
    stream.close(reset=True)
    for _ in range(20):
        rig.hub.publish("status", "x" * 1000)
        time.sleep(0.02)
    assert wait_for(lambda: rig.hub.subscriber_count == 0, 2.0)


def test_too_many_streams_get_503_and_a_free_slot_works_again(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(hub=ht.Hub(max_subscribers=2))
    cookie = rig.login()
    first = SseClient(rig.port, cookie)
    second = SseClient(rig.port, cookie)
    try:
        third = SseClient(rig.port, cookie, read_preamble=False)
        assert third.status == 503
        assert third.headers["retry-after"] == "2"
        assert third.headers["cache-control"] == "no-store"
        third.close()
        first.close()
        assert wait_for(lambda: rig.hub.subscriber_count == 1)
        fourth = SseClient(rig.port, cookie)
        assert fourth.status == 200
        fourth.close()
    finally:
        first.close()
        second.close()


def test_an_event_published_from_another_thread_reaches_the_client(rig: Rig, cookie: str) -> None:
    stream = SseClient(rig.port, cookie)
    try:
        threading.Timer(0.1, rig.hub.publish, args=("busy", '{"value": true}')).start()
        event_id, payload = stream.read_event()
        assert (event_id, payload) == (1, {"action": "busy", "data": '{"value": true}'})
    finally:
        stream.close()


def test_every_connected_client_receives_every_event(rig: Rig, cookie: str) -> None:
    streams = [SseClient(rig.port, cookie) for _ in range(3)]
    try:
        for i in range(3):
            rig.hub.publish("status", str(i))
        for stream in streams:
            assert [stream.read_event()[0] for _ in range(3)] == [1, 2, 3]
        assert rig.get("/api/ping", cookie).json()["subscribers"] == 3
    finally:
        for stream in streams:
            stream.close()


def test_the_event_stream_needs_the_session_the_host_and_the_origin(rig: Rig, cookie: str) -> None:
    for bad_cookie, host in (("lcsc_s=nope", None), (cookie, "evil.com")):
        client = SseClient(rig.port, bad_cookie, host=host, read_preamble=False)
        assert client.status == 403
        client.close()
    assert rig.hub.subscriber_count == 0
    assert rig.get("/api/events", cookie, origin="http://evil.com").status == 403
    assert rig.hub.subscriber_count == 0


def test_ping_reports_the_subscriber_count(rig: Rig, cookie: str) -> None:
    response = rig.get("/api/ping", cookie)
    assert response.status == 200
    assert response.json() == {"ok": True, "subscribers": 0}
    assert response.header("content-type") == "application/json; charset=utf-8"
    assert_security_headers(response)


# Logging, stdout and stderr, robustness -----------------------------------------------------


def test_nothing_is_written_to_stdout_or_stderr_during_a_normal_run(
    make_rig: Callable[..., Rig], capfd: pytest.CaptureFixture[str]
) -> None:
    rig = make_rig()
    cookie = rig.login()
    for reset in (False, True, False, True):
        stream = SseClient(rig.port, cookie)
        rig.hub.publish("status", "x")
        stream.close(reset=reset)
    for _ in range(10):  # abrupt closes at every stage of a request
        sock = socket.create_connection(("127.0.0.1", rig.port))
        sock.close()
        sock = socket.create_connection(("127.0.0.1", rig.port))
        sock.sendall(b"GET /api/ping HTTP/1.1\r\nHost: 127.0.0.1")
        sock.close()
        sock = socket.create_connection(("127.0.0.1", rig.port))
        sock.sendall(f"GET /api/ping HTTP/1.1\r\nHost: 127.0.0.1:{rig.port}\r\n\r\n".encode())
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        sock.close()
    stream = SseClient(rig.port, cookie)
    rig.transport.stop()
    stream.close()
    time.sleep(0.2)
    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_log_lines_are_short_single_lines(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    cookie = rig.login()
    request(rig.port, "GET", "/api/ping")
    request(rig.port, "GET", "/api/ping", host="evil.com")
    rig.handler = lambda a, d: (_ for _ in ()).throw(ValueError("multi\nline\nmessage " * 50))
    rig.act(cookie, {"action": "a", "data": ""})
    SseClient(rig.port, cookie).close()
    raw_exchange(rig.port, b"GARBAGE\r\n\r\n")
    wait_for(lambda: rig.hub.subscriber_count == 0)
    rig.transport.stop()
    assert rig.logs
    assert all("\n" not in line and len(line) <= 200 for line in rig.logs), rig.logs
    assert any("listening" in line for line in rig.logs)
    assert any("refused 403" in line for line in rig.logs)
    assert rig.logs[-1] == "http transport stopped"


def test_a_bug_in_a_route_answers_500_and_is_logged_not_printed(
    make_rig: Callable[..., Rig],
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    def broken(self: object) -> None:
        raise RuntimeError("boom secret detail")

    rig = make_rig()
    cookie = rig.login()
    monkeypatch.setattr(ht._Handler, "_ping", broken)
    response = rig.get("/api/ping", cookie)
    assert response.status == 500
    assert b"boom" not in response.body
    assert_security_headers(response)
    assert any("RuntimeError: boom secret detail" in line for line in rig.logs)
    monkeypatch.undo()
    assert rig.get("/api/ping", cookie).status == 200  # the server is fine
    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_a_body_on_a_get_request_is_never_read_as_the_next_request(rig: Rig, cookie: str) -> None:
    body = f"GET /api/ping HTTP/1.1\r\nHost: 127.0.0.1:{rig.port}\r\n\r\n"
    data = (
        f"GET /api/ping HTTP/1.1\r\nHost: 127.0.0.1:{rig.port}\r\nCookie: {cookie}\r\n"
        f"Content-Length: {len(body)}\r\n\r\n{body}"
    )
    answer = raw_exchange(rig.port, data.encode("ascii"))
    assert raw_status(answer) == 200
    assert answer.count(b"HTTP/1.1 ") == 1  # exactly one response on the connection


def test_the_url_survives_stop_but_a_stopped_transport_issues_no_launch_urls(rig: Rig) -> None:
    url = rig.transport.url
    rig.transport.stop()
    assert rig.transport.url == url
    with pytest.raises(RuntimeError):
        rig.transport.launch_url()


def test_refusal_logging_is_capped(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    for _ in range(80):
        request(rig.port, "GET", "/api/ping")
    refusals = [line for line in rig.logs if line.startswith("refused")]
    assert len(refusals) == 50
    assert sum("not logged" in line for line in rig.logs) == 1


def test_log_is_never_called_with_a_transport_lock_held(make_rig: Callable[..., Rig]) -> None:
    violations: list[str] = []
    rig_holder: list[Rig] = []

    def probe(line: str) -> None:
        if not rig_holder:
            return  # the "listening" line is written while start() is still returning
        transport = rig_holder[0].transport
        for name in ("_state_lock", "_auth_lock", "_log_lock"):
            lock = getattr(transport, name)
            if not lock.acquire(timeout=0.3):
                violations.append(f"{name} held while logging {line!r}")
            else:
                lock.release()

    rig = make_rig(log=probe)
    rig_holder.append(rig)
    cookie = rig.login()
    request(rig.port, "GET", "/api/ping")  # refusal line
    rig.handler = lambda a, d: (_ for _ in ()).throw(RuntimeError("x"))
    rig.act(cookie, {"action": "a", "data": ""})  # failure line
    stream = SseClient(rig.port, cookie)  # opened line
    stream.close()
    assert wait_for(lambda: any("closed" in line for line in rig.logs))
    rig.transport.stop()  # stopped line
    assert violations == []
    assert any("stopped" in line for line in rig.logs)


def test_a_failing_log_callback_does_not_break_the_server(make_rig: Callable[..., Rig]) -> None:
    def explode(line: str) -> None:
        raise RuntimeError("logger is broken")

    rig = make_rig(log=explode)
    cookie = rig.login()
    assert request(rig.port, "GET", "/api/ping").status == 403
    assert rig.get("/api/ping", cookie).status == 200


def test_stress_many_requests_with_abrupt_closes(
    make_rig: Callable[..., Rig], capfd: pytest.CaptureFixture[str]
) -> None:
    rig = make_rig()
    cookie = rig.login()
    failures: list[str] = []
    counter = {"ok": 0}
    lock = threading.Lock()

    def worker(seed: int) -> None:
        for i in range(25):
            kind = (seed + i) % 6
            try:
                if kind == 0:
                    assert rig.get("/api/ping", cookie).status == 200
                elif kind == 1:
                    assert request(rig.port, "GET", "/api/ping").status == 403
                elif kind == 2:
                    assert rig.act(cookie, {"action": "a", "data": str(i)}).status == 200
                elif kind == 3:
                    assert rig.get("/app.js", cookie).status == 200
                elif kind == 4:
                    sock = socket.create_connection(("127.0.0.1", rig.port))
                    sock.sendall(
                        b"POST /api/act HTTP/1.1\r\nHost: x\r\nContent-Length: 500\r\n\r\n{"
                    )
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                    sock.close()
                else:
                    socket.create_connection(("127.0.0.1", rig.port)).close()
                with lock:
                    counter["ok"] += 1
            except Exception as exc:  # collected and asserted below
                failures.append(f"{kind}: {type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60.0)
    assert failures == []
    assert counter["ok"] == 200
    assert rig.get("/api/ping", cookie).json() == {"ok": True, "subscribers": 0}
    rig.transport.stop()
    captured = capfd.readouterr()
    assert captured.err == ""
    assert captured.out == ""
    assert all(len(line) <= 200 and "\n" not in line for line in rig.logs)


def test_slow_loris_does_not_block_other_requests_and_is_dropped(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(connection_timeout_s=0.6)
    cookie = rig.login()
    lorises = []
    for _ in range(10):
        sock = socket.create_connection(("127.0.0.1", rig.port))
        sock.sendall(b"GET /api/ping HTTP/1.1\r\nHost: 127.0.0.1")  # never completed
        lorises.append(sock)
    silent = socket.create_connection(("127.0.0.1", rig.port))  # connects, sends nothing
    started = time.monotonic()
    for _ in range(10):
        assert rig.get("/api/ping", cookie).status == 200
    assert time.monotonic() - started < 2.0
    for sock in [*lorises, silent]:
        sock.settimeout(4.0)
        with contextlib.suppress(ConnectionError):
            assert sock.recv(100) == b""  # the server gave up on it
        sock.close()
    assert rig.get("/api/ping", cookie).status == 200


def test_a_client_that_never_reads_cannot_hold_a_stream_forever(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(connection_timeout_s=0.5)
    cookie = rig.login()
    stream = SseClient(rig.port, cookie)
    stream.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
    try:
        payload = "x" * 50000
        for _ in range(200):
            rig.hub.publish("status", payload)
        assert wait_for(lambda: rig.hub.subscriber_count == 0, 8.0)
    finally:
        stream.close()


def test_the_server_survives_a_burst_of_new_launches(rig: Rig) -> None:
    cookies = [rig.login() for _ in range(20)]
    assert all(rig.get("/api/ping", c).status == 200 for c in cookies)
