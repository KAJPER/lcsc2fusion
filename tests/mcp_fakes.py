"""Test doubles for the MCP tests (no real network, no Fusion).

`FakeAddin` is a real HTTP server on 127.0.0.1 (port 0) that speaks the local add-in API, so the
Fusion client is tested over a real socket. `StubEasyEda` serves the synthetic part fixtures like
the stub in test_cli.py. Not a test module itself, so pytest does not collect it.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import TracebackType
from typing import Any

from step_samples import make_box_step

from lcsc2fusion.discovery import ApiEndpoint, write_api_file
from lcsc2fusion.errors import ModelError, PartNotFoundError
from lcsc2fusion.mcp.fusion_client import FusionClient
from lcsc2fusion.mcp.tools import Backend
from lcsc2fusion.model import SearchResult

TOKEN = "k" * 32
SYNTH = Path(__file__).parent / "fixtures" / "synthetic"


# Fake Fusion add-in


@dataclass
class Recorded:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


def job_doc(
    job: int = 1,
    state: str = "running",
    *,
    lcsc_id: str = "C9000001",
    ok: bool | None = None,
    status: str = "Working",
    message: str = "",
    steps: list[str] | None = None,
    added: dict[str, Any] | None = None,
    fusion: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A job document as the add-in serves it."""
    return {
        "job": job,
        "lcsc_id": lcsc_id,
        "state": state,
        "ok": ok,
        "status": status,
        "message": message,
        "steps": steps if steps is not None else [],
        "added": added,
        "fusion": fusion,
    }


def done_doc(job: int = 1, lcsc_id: str = "C9000001", three_d: str = "none") -> dict[str, Any]:
    return job_doc(
        job,
        "done",
        lcsc_id=lcsc_id,
        ok=True,
        status="Done",
        message="Added to the library.",
        steps=["Downloading", "Merging into the library", "Done"],
        added={
            "path": "C:/Users/x/LCSC2Fusion/libraries/C9000001.lbr",
            "mpn": "SYN-R0603-10K",
            "warnings": ["added warning"],
            "step_path": None,
            "placed_step_path": None,
            "with_3d": three_d != "none",
        },
        fusion={
            "created_library": False,
            "merged": True,
            "already_present": False,
            "three_d": three_d,
            "library_name": "LCSC2Fusion Library",
            "message": "Merged into LCSC2Fusion Library.",
            "warnings": ["fusion warning"],
        },
    )


def failed_doc(job: int = 1, message: str = "Fusion refused the library.") -> dict[str, Any]:
    return job_doc(job, "failed", ok=False, status="Failed", message=message, steps=["Downloading"])


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, format: str, *args: Any) -> None:  # silence the default stderr log
        return

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def _handle(self, method: str) -> None:
        addin = self.server.addin
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        headers = {key.lower(): value for key, value in self.headers.items()}
        with addin.lock:
            addin.requests.append(Recorded(method, self.path, headers, body))
        if addin.delay_s and (addin.delay_for is None or addin.delay_for in self.path):
            time.sleep(addin.delay_s)
        status, payload = addin.respond(method, self.path, headers, body)
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        with contextlib.suppress(OSError):  # the client gave up (timeout test)
            self.wfile.write(data)
        self.close_connection = True


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    addin: FakeAddin


@dataclass
class FakeAddin:
    """The local API of the add-in. Configure the public fields, then call `start`."""

    token: str = TOKEN
    busy: bool = False
    delay_s: float = 0.0
    delay_for: str | None = None
    add_reply: tuple[int, dict[str, Any]] | None = None  # forces the answer of POST add
    ping_reply: tuple[int, dict[str, Any]] | None = None
    script_factory: Callable[[int, str, bool], list[dict[str, Any]]] | None = None
    job_scripts: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    requests: list[Recorded] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    next_job: int = 1
    accepted_tokens: set[str] = field(default_factory=set)
    auth_status: int = 401  # what a wrong token gets (401 or 403)
    on_unauthorized: Callable[[], None] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.accepted_tokens.add(self.token)
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.addin = self
        self._thread = threading.Thread(
            target=lambda: self._server.serve_forever(poll_interval=0.01), daemon=True
        )

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> FakeAddin:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> FakeAddin:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()

    def write_api_file(self, path: Path, token: str | None = None) -> None:
        write_api_file(path, ApiEndpoint(self.url, token or self.token, os.getpid()))

    def calls(self, method: str, prefix: str) -> list[Recorded]:
        with self.lock:
            return [r for r in self.requests if r.method == method and r.path.startswith(prefix)]

    # The API

    def respond(
        self, method: str, path: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, dict[str, Any]]:
        if headers.get("authorization", "").removeprefix("Bearer ") not in self.accepted_tokens:
            if self.on_unauthorized is not None:
                self.on_unauthorized()
            return self.auth_status, {"error": "unauthorized"}
        if "origin" in headers:
            self.violations.append("Origin header sent")
            return 403, {"error": "forbidden"}
        if method == "POST" and (
            headers.get("content-type") != "application/json" or headers.get("x-lcsc") != "1"
        ):
            self.violations.append("POST without Content-Type or X-LCSC")
            return 403, {"error": "forbidden"}
        if method == "GET" and path == "/api/local/ping":
            return self.ping_reply or (200, {"ok": True, "version": 1, "busy": self.busy})
        if method == "POST" and path == "/api/local/add":
            return self._add(body)
        if method == "GET" and path.startswith("/api/local/job/"):
            return self._job(path.rsplit("/", 1)[1])
        return 404, {"error": "not_found"}

    def _add(self, body: bytes) -> tuple[int, dict[str, Any]]:
        if self.add_reply is not None:
            return self.add_reply
        request = json.loads(body)
        job = self.next_job
        self.next_job += 1
        lcsc_id, with_3d = str(request["lcsc_id"]), bool(request["with_3d"])
        if self.script_factory is not None:
            script = self.script_factory(job, lcsc_id, with_3d)
        else:
            script = [done_doc(job, lcsc_id, "started" if with_3d else "none")]
        self.job_scripts[job] = script
        return 202, {"job": job}

    def _job(self, text: str) -> tuple[int, dict[str, Any]]:
        script = self.job_scripts.get(int(text)) if text.isdigit() else None
        if not script:
            return 404, {"error": "unknown_job"}
        return 200, script.pop(0) if len(script) > 1 else script[0]


def free_port() -> int:
    """A port nothing listens on (bound and released again)."""
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class FakeClock:
    """Fake time for polling tests: `sleep` advances `now`, nothing really waits."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


# Stub EasyEDA


DOCS = {
    "C9000001": SYNTH / "r0603.json",
    "C9000004": SYNTH / "sot23.json",
    "C9000016": SYNTH / "box3d_rot90.json",
    "C9000017": SYNTH / "box3d_z.json",
}
MODEL_SIDECARS = {
    "cc9537b400bd60d0fa1f049537c4bf44": SYNTH / "box3d_rot90.expected.json",
    "86bcd63db8c66b1af2659db97a969b57": SYNTH / "box3d_z.expected.json",
    "8ce2a36307c68382e5e789edc6b90caf": SYNTH / "r0603.expected.json",
}


class StubEasyEda:
    """Serves synthetic documents and, on request, STEP boxes. Records every call."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.model_failure: Exception | None = None

    def fetch_part(self, lcsc_id: str) -> dict[str, Any]:
        self.calls.append(f"part {lcsc_id}")
        path = DOCS.get(lcsc_id)
        if path is None:
            raise PartNotFoundError(f"Part {lcsc_id} not found")
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        return data

    def fetch_datasheet_url(self, lcsc_id: str) -> str:
        self.calls.append(f"datasheet {lcsc_id}")
        return "https://example.invalid/ds.pdf"

    def fetch_model_step(self, uuid: str) -> bytes:
        self.calls.append(f"model {uuid}")
        if self.model_failure is not None:
            raise self.model_failure
        sidecar = MODEL_SIDECARS.get(uuid)
        if sidecar is None:
            raise ModelError(f"3D model {uuid} not found on EasyEDA (HTTP 404).")
        native = json.loads(sidecar.read_text(encoding="utf-8"))["model3d"]["native_size_mm"]
        return make_box_step(size=(native[0], native[1], 0.9), center=(10.0, 5.0), zmin=-0.2)


SEARCH_RESULTS = [
    SearchResult(
        "C9000001",
        "SYN-R0603-10K",
        "SYN-Maker",
        "0603",
        "10k resistor",
        datasheet_url="https://example.invalid/r.pdf",
        library_type="Basic",
        stock=1234,
    ),
    SearchResult("C9000004", "SYN-SOT23", "SYN-Maker", "SOT-23", "transistor"),
]


def make_backend(
    api_path: Path | None = None,
    *,
    stub: StubEasyEda | None = None,
    results: list[SearchResult] | None = None,
    out_dir: Path | None = None,
    clock: FakeClock | None = None,
    **fusion_options: Any,
) -> Backend:
    """A backend with the stub client, canned search results and a Fusion client for `api_path`."""
    stub = stub if stub is not None else StubEasyEda()
    canned = list(SEARCH_RESULTS) if results is None else results
    fake_clock = clock if clock is not None else FakeClock()
    options: dict[str, Any] = {
        "poll_interval_s": 1.0,
        "sleep": fake_clock.sleep,
        "clock": fake_clock.clock,
        **fusion_options,
    }

    def fusion() -> FusionClient:
        return FusionClient(api_path, **options)

    def search(query: str, limit: int, client: Any) -> list[SearchResult]:
        stub.calls.append(f"search {query!r} {limit}")
        return canned[:limit]

    backend = Backend(
        client_factory=lambda: stub,  # type: ignore[arg-type, return-value]
        search=search,
        fusion_factory=fusion,
    )
    if out_dir is not None:
        backend.out_dir_default = lambda: out_dir
    return backend
