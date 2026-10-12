"""Tests of palette/http-bridge.js, the shim that runs the palette page in a browser window.

Two layers, both without Fusion and without network access beyond 127.0.0.1:

* tests/js/http_bridge_harness.js evaluates the bridge under Node with a hand written fake DOM,
  fetch, EventSource and a fake clock (every timing rule runs instantly). Skipped without Node.
* A real browser run (Playwright, found in any Python on PATH) serves the palette files from a
  small stdlib fixture server that fakes the three endpoints and the controller, then checks that
  app.js and http-bridge.js work together. Skipped when Playwright or its browser is missing.
  Set LCSC_BRIDGE_SHOTS to a folder to keep the screenshots (520 px wide, light and dark).
"""

from __future__ import annotations

import functools
import http.server
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import controller
import pytest

ROOT = Path(__file__).resolve().parents[1]
PALETTE_DIR = ROOT / "addin" / "LCSC2Fusion" / "palette"
BRIDGE = PALETTE_DIR / "http-bridge.js"
HARNESS = ROOT / "tests" / "js" / "http_bridge_harness.js"
DRIVER = ROOT / "tests" / "js" / "http_bridge_browser.py"
NODE = shutil.which("node")

TEXT_LOST = "Lost the connection to Fusion. If Fusion is closed, close this window."
TEXT_STOPPED = "LCSC2Fusion was stopped in Fusion. You can close this window."

# Node harness ----------------------------------------------------------------------------------


def _run_harness(mode: str) -> subprocess.CompletedProcess[str]:
    assert NODE is not None
    return subprocess.run(
        [NODE, str(HARNESS), str(BRIDGE), mode],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=120,
    )


def _scenario_names() -> list[str]:
    if NODE is None:
        return []
    try:
        names = json.loads(_run_harness("--list").stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    return [str(name) for name in names]


SCENARIOS = _scenario_names()


@pytest.fixture(scope="module")
def harness_results() -> dict[str, str]:
    completed = _run_harness("--all")
    assert completed.returncode == 0, completed.stderr
    results = json.loads(completed.stdout)
    assert isinstance(results, dict)
    return {str(name): str(outcome) for name, outcome in results.items()}


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_harness_lists_the_expected_scenarios() -> None:
    expected = {
        "not_installed_on_file_protocol",
        "not_installed_when_adsk_exists",
        "post_request_shape",
        "post_timeout_after_35_seconds",
        "events_wait_for_the_handler_and_keep_their_order",
        "queue_keeps_the_newest_200",
        "handler_exceptions_do_not_break_the_stream",
        "shutdown_stops_the_bridge_and_closes_the_window",
        "banner_after_15_seconds_and_recovery",
        "retry_creates_a_fresh_event_source_when_the_browser_gave_up",
        "gives_up_after_90_seconds_and_keeps_the_banner",
        "pagehide_stops_retries_and_clears_the_timers",
    }
    assert expected <= set(SCENARIOS)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.parametrize("name", SCENARIOS)
def test_bridge_scenario(name: str, harness_results: dict[str, str]) -> None:
    assert harness_results[name] == "ok", harness_results[name]


# Real browser run -----------------------------------------------------------------------------

STATIC_FILES = {
    "/style.css": "text/css; charset=utf-8",
    "/app.js": "text/javascript; charset=utf-8",
    "/mock-bridge.js": "text/javascript; charset=utf-8",
    "/http-bridge.js": "text/javascript; charset=utf-8",
}
SESSION_COOKIE = "lcsc_session=fixture"
CSP = "default-src 'self'"


class _FixtureServer:
    """Same origin HTTP server like the add-in's: palette files, /api/act, /api/events (SSE) and
    /api/ping behind a session cookie, plus /__test/ switches. The controller is a small fake."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.history: list[tuple[int, str]] = []
        self.subscribers: list[queue.Queue[tuple[int, str] | None]] = []
        self.posts: list[dict[str, Any]] = []
        self.down = False
        self.stream_delay = 0.0
        handler = type("Handler", (_Handler,), {"fixture": self})
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.drop(keep_down=False)
        self.httpd.shutdown()
        self.httpd.server_close()

    def publish(self, action: str, data: str) -> None:
        with self.lock:
            event = (len(self.history) + 1, json.dumps({"action": action, "data": data}))
            self.history.append(event)
            for subscriber in self.subscribers:
                subscriber.put(event)

    def drop(self, keep_down: bool = True) -> None:
        """Simulates an outage: close every stream and refuse new requests."""
        with self.lock:
            self.down = keep_down
            for subscriber in self.subscribers:
                subscriber.put(None)

    def restore(self) -> None:
        with self.lock:
            self.down = False

    def fake_controller(self, action: str, data: str) -> None:
        payload = json.loads(data) if data else {}
        if action == "ready":
            self.publish("busy", json.dumps({"value": False}))
            self.publish("status", json.dumps({"level": "info", "text": "Ready (fixture)"}))
        elif action == "search":
            item = {
                "lcsc_id": "C2040",
                "mpn": "RP2040",
                "manufacturer": "Raspberry Pi",
                "package": "LQFN-56",
                "description": "Dual ARM Cortex-M0+ microcontroller",
                "library_type": "Extended",
                "stock": 1,
            }
            self.publish("busy", json.dumps({"value": True}))
            self.publish("results", json.dumps({"query": payload["query"], "items": [item]}))
            self.publish("status", json.dumps({"level": "ok", "text": "Found 1 part(s)."}))
            self.publish("busy", json.dumps({"value": False}))
        elif action == "add":
            added: dict[str, Any] = dict.fromkeys(controller.ADDED_FIELDS)
            added.update(
                lcsc_id=payload["lcsc_id"],
                path="C:\\fixture\\" + payload["lcsc_id"] + ".lbr",
                mpn="RP2040",
                warnings=[],
                opened=False,
                instructions="Fixture instructions.",
                with_3d=False,
            )
            self.publish("added", json.dumps(added))
            self.publish("status", json.dumps({"level": "ok", "text": "Saved."}))


class _Handler(http.server.BaseHTTPRequestHandler):
    fixture: _FixtureServer

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _send(
        self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

    def _api_allowed(self) -> bool:
        """Cookie and outage checks shared by every /api/ endpoint."""
        if self.fixture.down:
            self._send_json(503, {"reply": "ERROR"})
            return False
        if SESSION_COOKIE not in (self.headers.get("Cookie") or ""):
            self._send_json(403, {"reply": "ERROR"})
            return False
        return True

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/":
            body = (PALETTE_DIR / "index.html").read_bytes()
            cookie = {"Set-Cookie": f"{SESSION_COOKIE}; HttpOnly; SameSite=Strict; Path=/"}
            self._send(200, body, "text/html; charset=utf-8", cookie)
        elif path in STATIC_FILES:
            self._send(200, (PALETTE_DIR / path.lstrip("/")).read_bytes(), STATIC_FILES[path])
        elif path == "/api/ping":
            if self._api_allowed():
                count = len(self.fixture.subscribers)
                self._send_json(200, {"ok": True, "subscribers": count})
        elif path == "/api/events":
            if self._api_allowed():
                self._stream()
        else:
            self._send_json(404, {"reply": "ERROR"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        path = self.path.split("?", 1)[0]
        if path.startswith("/__test/"):
            self._control(path, raw)
            return
        if path != "/api/act":
            self._send_json(404, {"reply": "ERROR"})
            return
        if not self._api_allowed():
            return
        content_type = self.headers.get("Content-Type", "")
        if self.headers.get("X-LCSC") != "1" or content_type != "application/json":
            self._send_json(403, {"reply": "ERROR"})
            return
        message = json.loads(raw)
        self.fixture.posts.append({"action": message["action"], "data": message["data"]})
        self.fixture.fake_controller(message["action"], message["data"])
        self._send_json(200, {"reply": "OK"})

    def _control(self, path: str, raw: bytes) -> None:
        if path == "/__test/drop":
            self.fixture.drop()
        elif path == "/__test/restore":
            self.fixture.restore()
        elif path == "/__test/delay":
            self.fixture.stream_delay = float(json.loads(raw)["seconds"])
        elif path == "/__test/publish":
            message = json.loads(raw)
            self.fixture.publish(message["action"], message["data"])
        self._send_json(200, {"ok": True})

    def _stream(self) -> None:
        fixture = self.fixture
        time.sleep(fixture.stream_delay)  # a slow accept: what is published meanwhile is missed
        header = self.headers.get("Last-Event-ID")
        last_id = int(header) if header and header.isdigit() else None
        events: queue.Queue[tuple[int, str] | None] = queue.Queue()
        with fixture.lock:
            # Like the add-in: only a reconnect with Last-Event-ID replays, a fresh one starts
            # at the head of the stream.
            replay = [] if last_id is None else [e for e in fixture.history if e[0] > last_id]
            fixture.subscribers.append(events)
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", CSP)
            self.end_headers()
            self.wfile.write(b"retry: 1000\n\n")
            self.wfile.flush()
            pending: list[tuple[int, str] | None] = list(replay)
            while True:
                for event in pending:
                    if event is None:
                        return
                    self.wfile.write(f"id: {event[0]}\ndata: {event[1]}\n\n".encode())
                self.wfile.flush()
                try:
                    pending = [events.get(timeout=0.5)]
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    pending = []
        except OSError:
            return
        finally:
            with fixture.lock:
                fixture.subscribers.remove(events)


@functools.lru_cache(maxsize=1)
def _python_with_playwright() -> str | None:
    """The project venv has no Playwright; look for any other interpreter that does."""
    candidates = [sys.executable]
    candidates += [found for name in ("python", "python3") if (found := shutil.which(name))]
    launcher = shutil.which("py")
    if launcher:
        try:
            listed = subprocess.run(
                [launcher, "-3", "-c", "import sys; print(sys.executable)"],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            listed = None
        if listed is not None and listed.returncode == 0 and listed.stdout.strip():
            candidates.append(listed.stdout.strip())
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(os.path.abspath(candidate))
        if key in seen:
            continue
        seen.add(key)
        try:
            probe = subprocess.run(
                [candidate, "-c", "import playwright.sync_api"],
                capture_output=True,
                check=False,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0:
            return candidate
    return None


@dataclass
class BrowserRun:
    report: dict[str, Any]
    server: _FixtureServer
    shots: Path


@pytest.fixture(scope="module")
def browser_run(tmp_path_factory: pytest.TempPathFactory) -> Iterator[BrowserRun]:
    python = _python_with_playwright()
    if python is None:
        pytest.skip("Playwright is not installed in any Python on PATH")
    configured = os.environ.get("LCSC_BRIDGE_SHOTS")
    shots = Path(configured) if configured else tmp_path_factory.mktemp("bridge-shots")
    server = _FixtureServer()
    server.start()
    try:
        completed = subprocess.run(
            [python, str(DRIVER), server.base, str(shots)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=240,
        )
        assert completed.returncode == 0, completed.stderr[-3000:]
        report = json.loads(completed.stdout.strip().splitlines()[-1])
        if "skipped" in report:
            pytest.skip(report["skipped"])
        yield BrowserRun(report, server, shots)
    finally:
        server.stop()


def test_browser_page_connects_through_the_bridge(browser_run: BrowserRun) -> None:
    report = browser_run.report
    assert report["initial_status"] == "Ready (fixture)"
    assert report["mock_banner_hidden"] is True  # the real bridge was found, not the mock
    assert report["conn_banner_hidden_at_start"] is True
    assert report["state_at_start"] == "open"


def test_browser_search_and_add_round_trip(browser_run: BrowserRun) -> None:
    report = browser_run.report
    assert len(report["result_rows"]) == 1
    row = report["result_rows"][0]
    assert "C2040" in row
    assert "RP2040" in row
    assert report["status_after_search"] == "Found 1 part(s)."
    assert report["added_items_before"] == 1
    posts = browser_run.server.posts
    assert [post["action"] for post in posts[:3]] == ["ready", "search", "add"]
    assert json.loads(posts[1]["data"]) == {"query": "C2040"}
    assert json.loads(posts[2]["data"]) == {"lcsc_id": "C2040", "with_3d": False, "fusion": True}


def test_browser_first_request_waits_for_a_slow_stream(browser_run: BrowserRun) -> None:
    # The fixture subscribes 0.5 s late and, like the add-in, does not replay to a fresh
    # connection: the status that answers `ready` is only seen because the request waited.
    assert browser_run.report["initial_status"] == "Ready (fixture)"
    assert browser_run.report["stream_delay_used"] == 0.5


def test_browser_requests_pass_the_server_checks(browser_run: BrowserRun) -> None:
    # The fixture answers 403 without the cookie, X-LCSC and the JSON content type, so the
    # recorded posts prove that the bridge sent all three.
    assert len(browser_run.server.posts) >= 3
    assert browser_run.report["page_errors"] == []
    assert not [e for e in browser_run.report["console_errors"] if "Content Security" in e]


def test_browser_lost_connection_banner_and_recovery(browser_run: BrowserRun) -> None:
    report = browser_run.report
    assert report["conn_banner_hidden_before_15s"] is True
    assert report["lost_banner_text"] == TEXT_LOST
    assert report["lost_banner_role"] == "status"
    assert report["lost_banner_live"] == "polite"
    assert report["state_when_lost"] == "broken"
    assert report["state_after_recovery"] == "open"
    # the fresh stream replays the whole buffer: nothing may be rendered a second time
    assert report["added_items_after"] == report["added_items_before"] == 1
    assert report["result_rows_after"] == 1
    assert report["close_calls_before_shutdown"] == 0
    # events published during the outage are gone (no replay), so the page asked again: the
    # status of the second `ready` is on screen and exactly one more `ready` was posted
    assert report["status_after_recovery"] == "Ready (fixture)"
    actions = [post["action"] for post in browser_run.server.posts]
    assert actions == ["ready", "search", "add", "ready"]


def test_browser_shutdown_message_stops_the_bridge(browser_run: BrowserRun) -> None:
    report = browser_run.report
    assert report["shutdown_banner_text"] == TEXT_STOPPED
    assert report["state_after_shutdown"] == "stopped"
    assert report["close_calls_after_shutdown"] == 1


def test_browser_screenshots_were_taken(browser_run: BrowserRun) -> None:
    names = {Path(path).name for path in browser_run.report["shots"]}
    assert names == {
        "session_light.png",
        "lost_light.png",
        "lost_dark.png",
        "stopped_light.png",
        "stopped_dark.png",
    }
    for path in browser_run.report["shots"]:
        assert Path(path).stat().st_size > 2000
