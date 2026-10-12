"""The stdio loop: lines in, lines out, EOF, huge and broken input, and real subprocesses."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from mcp_fakes import TOKEN, FakeAddin, FakeClock, done_doc, job_doc, make_backend

from lcsc2fusion import __version__
from lcsc2fusion.mcp import protocol
from lcsc2fusion.mcp.server import (
    MAX_MESSAGE_BYTES,
    McpServer,
    main,
    read_line,
    serve,
)
from lcsc2fusion.mcp.tools import Backend

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"


def encode(*messages: Any) -> bytes:
    return b"".join(json.dumps(m).encode("utf-8") + b"\n" for m in messages)


def ping(request_id: Any = 1) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "method": "ping"}


def drive(data: bytes, server: McpServer | None = None) -> list[dict[str, Any]]:
    """Run the loop over `data` and return every line it wrote as a parsed message."""
    out = io.BytesIO()
    serve(server or McpServer(Backend()), io.BytesIO(data), out)
    raw = out.getvalue()
    assert raw == b"" or raw.endswith(b"\n")
    lines = raw.split(b"\n")[:-1]
    for line in lines:
        line.decode("ascii")  # replies are always plain ASCII JSON
    return [json.loads(line) for line in lines]


# read_line


def test_read_line_returns_lines_then_end_of_input() -> None:
    stream = io.BytesIO(b"one\ntwo\nthree")
    assert read_line(stream) == (b"one\n", False)
    assert read_line(stream) == (b"two\n", False)
    assert read_line(stream) == (b"three", False)  # a last line without newline still counts
    assert read_line(stream) == (None, False)


def test_read_line_skips_an_oversize_line_without_keeping_it() -> None:
    stream = io.BytesIO(b"x" * 100 + b"\nnext\n")
    assert read_line(stream, limit=10) == (b"", True)
    assert read_line(stream, limit=10) == (b"next\n", False)


def test_read_line_limit_is_inclusive_of_the_newline() -> None:
    exact = io.BytesIO(b"x" * 10 + b"\n")
    assert read_line(exact, limit=10) == (b"x" * 10 + b"\n", False)
    over = io.BytesIO(b"x" * 11 + b"\n")
    assert read_line(over, limit=10) == (b"", True)
    no_newline = io.BytesIO(b"x" * 11)
    assert read_line(no_newline, limit=10) == (b"", True)
    assert read_line(no_newline, limit=10) == (None, False)


# serve


def test_empty_input_ends_cleanly() -> None:
    assert drive(b"") == []


def test_blank_lines_get_no_reply() -> None:
    assert drive(b"\n\n   \n\r\n") == []


def test_requests_are_answered_in_order() -> None:
    replies = drive(encode(ping(1), ping("two"), ping(3)))
    assert [r["id"] for r in replies] == [1, "two", 3]


def test_notifications_produce_no_output() -> None:
    data = encode(
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        ping(1),
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}},
    )
    assert [r["id"] for r in drive(data)] == [1]


def test_crlf_line_endings_work() -> None:
    assert drive(b'{"jsonrpc":"2.0","id":1,"method":"ping"}\r\n')[0]["result"] == {}


def test_the_last_line_needs_no_newline() -> None:
    assert drive(b'{"jsonrpc":"2.0","id":1,"method":"ping"}')[0]["id"] == 1


def test_a_malformed_line_does_not_stop_the_loop() -> None:
    data = b"{broken\n" + encode(ping(2)) + b"[1,2]\n" + b"\xff\xfe\n" + encode(ping(3))
    replies = drive(data)
    assert [r["id"] for r in replies] == [None, 2, None, None, 3]
    assert replies[0]["error"]["code"] == protocol.PARSE_ERROR
    assert replies[2]["error"]["code"] == protocol.INVALID_REQUEST
    assert replies[3]["error"]["code"] == protocol.PARSE_ERROR


def test_a_huge_line_is_rejected_and_the_loop_goes_on() -> None:
    huge = (
        b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"pad":"' + b"x" * 3_000_000 + b'"}}\n'
    )
    replies = drive(huge + encode(ping(2)))
    assert [r["id"] for r in replies] == [None, 2]
    assert replies[0]["error"]["code"] == protocol.INVALID_REQUEST
    assert "longer than" in replies[0]["error"]["message"]


def test_a_huge_last_line_without_newline_ends_cleanly() -> None:
    replies = drive(encode(ping(1)) + b"y" * (MAX_MESSAGE_BYTES * 2))
    assert [r.get("id") for r in replies] == [1, None]


def test_a_message_of_exactly_the_limit_is_accepted() -> None:
    head = b'{"jsonrpc":"2.0","id":1,"method":"ping"'
    pad = MAX_MESSAGE_BYTES - len(head) - 1
    line = head + b" " * pad + b"}"
    assert len(line) == MAX_MESSAGE_BYTES
    replies = drive(line + b"\n" + encode(ping(2)))
    assert [r["id"] for r in replies] == [1, 2]
    assert "result" in replies[0]


def test_one_byte_over_the_limit_is_rejected() -> None:
    head = b'{"jsonrpc":"2.0","id":1,"method":"ping"'
    line = head + b" " * (MAX_MESSAGE_BYTES - len(head)) + b"}"
    assert len(line) == MAX_MESSAGE_BYTES + 1
    replies = drive(line + b"\n" + encode(ping(2)))
    assert [r["id"] for r in replies] == [None, 2]


def test_unicode_survives_a_round_trip_and_the_output_stays_ascii() -> None:
    query = "ESP32 ü 日本語 🙂"
    backend = make_backend()
    server = McpServer(backend)
    call = {
        "jsonrpc": "2.0",
        "id": "ünï-日本",
        "method": "tools/call",
        "params": {"name": "lcsc_search", "arguments": {"query": query}},
    }
    (reply,) = drive(json.dumps(call, ensure_ascii=False).encode("utf-8") + b"\n", server)
    assert reply["id"] == "ünï-日本"
    assert reply["result"]["structuredContent"]["query"] == query


def test_escaped_lone_surrogates_in_the_input_do_not_crash_the_server() -> None:
    line = b'{"jsonrpc":"2.0","id":"\\ud800","method":"ping"}\n'
    (reply,) = drive(line)
    assert reply["id"] == "\ud800" and reply["result"] == {}
    call = (
        b'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"lcsc_search",'
        b'"arguments":{"query":"x\\ud800y"}}}\n'
    )
    (rejected,) = drive(call)
    assert rejected["error"]["code"] == protocol.INVALID_PARAMS


def test_a_closed_stdout_ends_the_loop_quietly() -> None:
    class Closed(io.BytesIO):
        def write(self, data: Any) -> int:
            raise BrokenPipeError

    serve(McpServer(Backend()), io.BytesIO(encode(ping(1), ping(2))), Closed())


def test_a_crash_inside_the_server_is_answered_and_the_loop_goes_on() -> None:
    class Flaky(McpServer):
        def handle_line(self, line: bytes, notify: Any = None) -> Any:
            if b'"boom"' in line:
                raise RuntimeError("kaput")
            return super().handle_line(line, notify)

    replies = drive(encode({"id": "boom"}, ping(2)), Flaky(Backend()))
    assert replies[0]["error"]["code"] == protocol.INTERNAL_ERROR
    assert "kaput" not in replies[0]["error"]["message"]
    assert replies[1]["id"] == 2


def test_progress_notifications_come_before_the_response(tmp_path: Path) -> None:
    with FakeAddin() as addin:
        addin.write_api_file(tmp_path / "api.json")
        addin.script_factory = lambda job, lcsc_id, with_3d: [
            job_doc(job, steps=["Downloading"]),
            {**done_doc(job, lcsc_id), "steps": ["Downloading", "Done"]},
        ]
        server = McpServer(make_backend(tmp_path / "api.json", clock=FakeClock()))
        call = {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {
                "name": "fusion_add_part",
                "arguments": {"lcsc_id": "C9000001"},
                "_meta": {"progressToken": "abc"},
            },
        }
        lines = drive(encode(call, ping(12)), server)
    kinds = [m.get("method") or f"response {m['id']}" for m in lines]
    assert kinds == ["notifications/progress"] * 3 + ["response 11", "response 12"]
    assert [m["params"]["progress"] for m in lines[:3]] == [1, 2, 3]
    assert all(m["params"]["progressToken"] == "abc" for m in lines[:3])
    assert lines[3]["result"]["isError"] is False


# main() with injected streams


def test_main_runs_the_loop_on_the_given_streams_and_returns_zero() -> None:
    out = io.BytesIO()
    code = main([], server=McpServer(Backend()), stdin=io.BytesIO(encode(ping(7))), stdout=out)
    assert code == 0
    assert json.loads(out.getvalue()) == {"jsonrpc": "2.0", "id": 7, "result": {}}


def test_main_leaves_sys_stdout_alone_when_streams_are_injected() -> None:
    before = sys.stdout
    main([], server=McpServer(Backend()), stdin=io.BytesIO(b""), stdout=io.BytesIO())
    assert sys.stdout is before


def test_main_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main(["--version"])
    assert info.value.code == 0
    assert __version__ in capsys.readouterr().out


# Real processes


def child_env(api_file: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    env["LCSC2FUSION_API"] = str(api_file)
    env["PYTHONPATH"] = str(ROOT)
    return env


def run_server(
    args: list[str], data: bytes, env: dict[str, str], timeout: float = 60
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, *args],
        input=data,
        capture_output=True,
        timeout=timeout,
        cwd=ROOT,
        env=env,
    )


def parse_stdout(process: subprocess.CompletedProcess[bytes]) -> list[dict[str, Any]]:
    assert process.stdout.endswith(b"\n") or process.stdout == b""
    return [json.loads(line) for line in process.stdout.decode("ascii").splitlines()]


def test_python_dash_m_handshake_over_real_pipes(tmp_path: Path) -> None:
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        ping(2),
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "fusion_status", "arguments": {}},
        },
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "no_such_tool", "arguments": {}},
        },
        {"jsonrpc": "2.0", "id": 6, "method": "nope"},
    ]
    data = encode(*messages) + b"this is not json\n"
    process = run_server(["-m", "lcsc2fusion.mcp"], data, child_env(tmp_path / "none.json"))
    assert process.returncode == 0  # stdin EOF ends the server cleanly
    assert process.stderr == b""
    replies = parse_stdout(process)
    assert [r["id"] for r in replies] == [1, 2, 3, 4, 5, 6, None]
    assert replies[0]["result"]["protocolVersion"] == "2024-11-05"
    assert replies[0]["result"]["serverInfo"] == {"name": "lcsc2fusion", "version": __version__}
    assert replies[1]["result"] == {}
    assert len(replies[2]["result"]["tools"]) == 6
    status = replies[3]["result"]
    assert status["isError"] is False
    assert status["structuredContent"]["running"] is False
    assert "Add-Ins" in status["content"][0]["text"]
    assert replies[4]["error"]["code"] == protocol.INVALID_PARAMS
    assert replies[5]["error"]["code"] == protocol.METHOD_NOT_FOUND
    assert replies[6]["error"]["code"] == protocol.PARSE_ERROR


def test_the_console_script_module_entry_point_exists() -> None:
    from lcsc2fusion.mcp import main as exported

    assert exported is main
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'lcsc2fusion-mcp = "lcsc2fusion.mcp:main"' in pyproject


def test_version_flag_in_a_real_process(tmp_path: Path) -> None:
    process = run_server(["-m", "lcsc2fusion.mcp", "--version"], b"", child_env(tmp_path / "x"))
    assert process.returncode == 0
    assert __version__ in process.stdout.decode("ascii")


def test_empty_stdin_exits_at_once(tmp_path: Path) -> None:
    process = run_server(["-m", "lcsc2fusion.mcp"], b"", child_env(tmp_path / "x"), timeout=30)
    assert (process.returncode, process.stdout, process.stderr) == (0, b"", b"")


LAUNCHER = textwrap.dedent(
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, sys.argv[1])
    sys.path.insert(0, sys.argv[2])
    from mcp_fakes import make_backend
    from lcsc2fusion.mcp.server import McpServer, main

    backend = make_backend(out_dir=Path(sys.argv[3]))
    inner = backend.search

    def noisy(query, limit, client):
        print("stray print that must not reach the protocol stream")
        sys.stdout.write("more stray output\\n")
        return inner(query, limit, client)

    backend.search = noisy
    raise SystemExit(main([], server=McpServer(backend)))
    """
)


def test_tools_over_real_pipes_with_a_stub_backend_and_stray_prints(tmp_path: Path) -> None:
    launcher = tmp_path / "launcher.py"
    launcher.write_text(LAUNCHER, encoding="utf-8")
    out_dir = tmp_path / "libs"

    def call(request_id: int, name: str, **arguments: Any) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }

    data = encode(
        call(1, "lcsc_search", query="10k"),
        call(2, "lcsc_part_info", lcsc_id="C9000016"),
        call(3, "lcsc_convert", lcsc_id="C9000016", with_3d=True),
        call(4, "lcsc_part_info", lcsc_id="C1"),
    )
    process = subprocess.run(
        [sys.executable, str(launcher), str(TESTS), str(ROOT), str(out_dir)],
        input=data,
        capture_output=True,
        timeout=60,
        cwd=tmp_path,
    )
    assert process.returncode == 0, process.stderr.decode("utf-8", "replace")
    replies = parse_stdout(process)  # every stdout line is a JSON-RPC reply: no stray text
    assert [r["id"] for r in replies] == [1, 2, 3, 4]
    assert b"stray print" in process.stderr  # it went to stderr instead
    assert b"stray" not in process.stdout
    assert replies[0]["result"]["structuredContent"]["count"] == 2
    assert replies[1]["result"]["structuredContent"]["pad_count"] == 2
    assert replies[2]["result"]["isError"] is False
    assert sorted(p.name for p in out_dir.iterdir()) == [
        "C9000016.3d.json",
        "C9000016.lbr",
        "C9000016.placed.step",
        "C9000016.step",
    ]
    assert replies[3]["result"]["isError"] is True


def test_fusion_tools_over_real_pipes_against_a_fake_add_in(tmp_path: Path) -> None:
    with FakeAddin() as addin:
        api = tmp_path / "api.json"
        addin.write_api_file(api)
        addin.script_factory = lambda job, lcsc_id, with_3d: [
            job_doc(job, lcsc_id=lcsc_id, steps=["Downloading part"]),
            {
                **done_doc(job, lcsc_id, "started"),
                "steps": ["Downloading part", "Opening 3D editor"],
            },
        ]
        add_call = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "fusion_add_part",
                "arguments": {"lcsc_id": "c9000001", "with_3d": True},
                "_meta": {"progressToken": 99},
            },
        }
        status_call = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "fusion_status", "arguments": {}},
        }
        process = run_server(
            ["-m", "lcsc2fusion.mcp"], encode(status_call, add_call), child_env(api)
        )
    assert process.returncode == 0, process.stderr.decode("utf-8", "replace")
    replies = parse_stdout(process)
    assert replies[0]["id"] == 1 and replies[0]["result"]["structuredContent"]["running"] is True
    progress = [r for r in replies if r.get("method") == "notifications/progress"]
    final = replies[-1]
    assert final["id"] == 2 and final["result"]["isError"] is False
    assert [p["params"]["message"] for p in progress] == [
        "Asking the Fusion add-in to add C9000001...",
        "Downloading part",
        "Opening 3D editor",
    ]
    assert replies.index(progress[-1]) < replies.index(final)
    assert final["result"]["structuredContent"]["needs_human_confirmation"] is True
    assert addin.violations == []
    (request,) = addin.calls("POST", "/api/local/add")
    assert json.loads(request.body) == {"lcsc_id": "C9000001", "with_3d": True, "fusion": True}
    # The secret never leaves through any channel.
    assert TOKEN.encode() not in process.stdout and TOKEN.encode() not in process.stderr
