"""MCP server over stdio: newline delimited JSON-RPC 2.0, tools only (stdlib only).

One JSON object per line on stdin, one per line on stdout. Nothing else is ever written to
stdout (`main` points `sys.stdout` at stderr while serving, so a stray `print` cannot corrupt the
stream); diagnostics go to stderr. Requests are handled one at a time on the main thread.

`McpServer.handle_message` is the whole protocol and is easy to test; `serve` is the line loop
around it. Protocol errors use JSON-RPC codes; failures of a tool are tool results with
`isError` true (see `tools.py`).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any, BinaryIO

from lcsc2fusion import __version__
from lcsc2fusion.mcp import protocol
from lcsc2fusion.mcp.protocol import Message
from lcsc2fusion.mcp.tools import Backend, Tool, ToolContext, ToolResult, build_tools, call_tool
from lcsc2fusion.mcp.validation import InvalidArguments

_log = logging.getLogger(__name__)

MAX_MESSAGE_BYTES = 1_048_576  # one JSON-RPC line; the tool arguments are tiny
_DISCARD_CHUNK_BYTES = 65_536

INSTRUCTIONS = (
    "lcsc2fusion turns parts from LCSC (the JLCPCB / EasyEDA library) into Eagle .lbr libraries "
    "for Autodesk Fusion Electronics. Typical flow: lcsc_search (an LCSC number like C2040, or "
    "free text), lcsc_part_info (package, pad and pin counts, 3D model), then either "
    "lcsc_convert (writes the .lbr and optionally STEP files to disk) or fusion_add_part (the "
    "running Fusion add-in downloads the part and merges it into the 'LCSC2Fusion Library' in "
    "the user's Fusion project; needs Fusion running with the LCSC2Fusion add-in, see "
    "fusion_status). With with_3d=true Fusion opens a 3D package editor that a person must "
    "confirm (Finish, Save, OK), and Fusion can be unresponsive for up to about a minute while "
    "it works. Handle one part at a time and only parts the user asked for. The data comes from "
    "unofficial EasyEDA/LCSC endpoints and belongs to JLCEDA/EasyEDA: it is for personal use "
    "and must not be scraped in bulk (requests are cached and rate limited). Tell the user to "
    "check footprint and pinout against the datasheet before ordering boards."
)

Notify = Callable[[Message], None]
Progress = Callable[[str], None]


def _no_progress(message: str) -> None:
    _log.debug("progress: %s", message)


class McpServer:
    """Answers MCP requests. Holds no connection state, so tests can call it directly."""

    def __init__(
        self,
        backend: Backend | None = None,
        tools: Sequence[Tool] | None = None,
        version: str = __version__,
    ) -> None:
        self._backend = backend if backend is not None else Backend()
        self._tools = {tool.name: tool for tool in (tools if tools is not None else build_tools())}
        self._version = version

    # Entry points

    def handle_line(self, line: bytes, notify: Notify | None = None) -> Message | None:
        """One raw line from stdin: the reply to send, or None (notification, blank line)."""
        try:
            text = line.decode("utf-8-sig").strip()
        except UnicodeDecodeError:
            return protocol.error_response(None, protocol.PARSE_ERROR, "Parse error: not UTF-8")
        if not text:
            return None
        try:
            message = json.loads(text, parse_constant=_reject_constant)
        except (ValueError, RecursionError) as exc:
            _log.debug("unparsable line: %s", exc)
            return protocol.error_response(None, protocol.PARSE_ERROR, "Parse error: invalid JSON")
        return self.handle_message(message, notify)

    def handle_message(self, message: object, notify: Notify | None = None) -> Message | None:
        """One parsed JSON value. Returns the response, or None when none must be sent."""
        if isinstance(message, list):
            return protocol.error_response(
                None, protocol.INVALID_REQUEST, "Invalid Request: batch requests are not supported"
            )
        if not isinstance(message, dict):
            return protocol.error_response(
                None, protocol.INVALID_REQUEST, "Invalid Request: expected a JSON object"
            )
        method = message.get("method")
        if method is None and ("result" in message or "error" in message):
            return None  # a response from the client; this server never sends requests
        has_id = "id" in message
        request_id: Any = message.get("id")
        id_ok = not has_id or protocol.is_request_id(request_id)
        if message.get("jsonrpc") != protocol.JSONRPC_VERSION or not isinstance(method, str):
            return protocol.error_response(
                request_id if has_id and id_ok else None,
                protocol.INVALID_REQUEST,
                "Invalid Request: not a JSON-RPC 2.0 request or notification",
            )
        if not id_ok:
            return protocol.error_response(
                None, protocol.INVALID_REQUEST, "Invalid Request: id must be a string or integer"
            )
        if not has_id:
            self._handle_notification(method)
            return None
        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return protocol.error_response(
                request_id, protocol.INVALID_PARAMS, "Invalid params: expected an object"
            )
        try:
            return self._dispatch(request_id, method, params, notify)
        except InvalidArguments as exc:
            return protocol.error_response(request_id, protocol.INVALID_PARAMS, str(exc))
        except Exception:  # a bug in a handler must not end the session
            _log.exception("Unhandled error in %s", method)
            return protocol.error_response(
                request_id, protocol.INTERNAL_ERROR, "Internal error (see the server log)"
            )

    # Dispatch

    def _handle_notification(self, method: str) -> None:
        if method == "notifications/initialized":
            _log.debug("client initialized")
        elif method == "notifications/cancelled":
            _log.debug("cancellation ignored: requests run to completion")
        else:
            _log.debug("ignoring notification %s", method)

    def _dispatch(
        self,
        request_id: protocol.RequestId,
        method: str,
        params: dict[str, Any],
        notify: Notify | None,
    ) -> Message:
        if method == "initialize":
            return protocol.result_response(request_id, self._initialize(params))
        if method == "ping":
            return protocol.result_response(request_id, {})
        if method == "tools/list":
            return protocol.result_response(request_id, self._list_tools(params))
        if method == "tools/call":
            return protocol.result_response(request_id, self._call_tool(params, notify))
        return protocol.error_response(
            request_id, protocol.METHOD_NOT_FOUND, f"Method not found: {method[:100]}"
        )

    def _initialize(self, params: Mapping[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        if not isinstance(requested, str):
            raise InvalidArguments("Invalid params: protocolVersion must be a string")
        capabilities = params.get("capabilities", {})
        if not isinstance(capabilities, dict):
            raise InvalidArguments("Invalid params: capabilities must be an object")
        return {
            "protocolVersion": protocol.negotiate_protocol_version(requested),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": protocol.SERVER_NAME, "version": self._version},
            "instructions": INSTRUCTIONS,
        }

    def _list_tools(self, params: Mapping[str, Any]) -> dict[str, Any]:
        cursor = params.get("cursor")
        if cursor is not None and not isinstance(cursor, str):
            raise InvalidArguments("Invalid params: cursor must be a string")
        if cursor:
            raise InvalidArguments("Invalid params: unknown cursor (the tool list has one page)")
        return {"tools": [tool.listing() for tool in self._tools.values()]}

    def _call_tool(self, params: Mapping[str, Any], notify: Notify | None) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str):
            raise InvalidArguments("Invalid params: name must be a string")
        tool = self._tools.get(name)
        if tool is None:
            raise InvalidArguments(f"Unknown tool: {name[:100]}")
        arguments = params.get("arguments", {})
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise InvalidArguments("Invalid params: arguments must be an object")
        progress = self._progress_for(params.get("_meta"), notify)
        result = call_tool(tool, arguments, ToolContext(backend=self._backend, progress=progress))
        return _tool_response(result)

    @staticmethod
    def _progress_for(meta: object, notify: Notify | None) -> Progress:
        """A progress reporter when the client sent a progress token, else a no-op."""
        token = meta.get("progressToken") if isinstance(meta, dict) else None
        if notify is None or not protocol.is_request_id(token):
            return _no_progress
        counter = 0

        def report(message: str) -> None:
            nonlocal counter
            counter += 1
            notify(
                protocol.notification(
                    "notifications/progress",
                    {"progressToken": token, "progress": counter, "message": message},
                )
            )

        return report


def _tool_response(result: ToolResult) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": result.text}],
        "structuredContent": result.data,
        "isError": result.is_error,
    }


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


# Line loop


def encode_message(message: Message) -> bytes:
    """One line of ASCII JSON (non ASCII text is escaped, so no encoding can go wrong)."""
    return json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n"


def read_line(stream: BinaryIO, limit: int = MAX_MESSAGE_BYTES) -> tuple[bytes | None, bool]:
    """The next line as (bytes, oversize). (None, False) is end of input.

    A line longer than `limit` bytes is skipped to its end without being kept in memory and
    reported as oversize with empty content.
    """
    chunk = stream.readline(limit + 1)
    if not chunk:
        return None, False
    if len(chunk) > limit and not chunk.endswith(b"\n"):
        while True:
            rest = stream.readline(_DISCARD_CHUNK_BYTES)
            if not rest or rest.endswith(b"\n"):
                break
        return b"", True
    return chunk, False


def serve(server: McpServer, stdin: BinaryIO, stdout: BinaryIO) -> None:
    """Run until end of input (or until the client closes our output)."""
    closed = False

    def send(message: Message) -> None:
        nonlocal closed
        if closed:
            return
        try:
            stdout.write(encode_message(message))
            stdout.flush()
        except (BrokenPipeError, ValueError, OSError):
            _log.info("stdout is closed, stopping")
            closed = True

    while not closed:
        try:
            line, oversize = read_line(stdin)
        except (OSError, ValueError):  # stdin closed or broken: same as end of input
            _log.info("cannot read stdin, stopping")
            return
        if line is None:
            _log.debug("end of input")
            return
        if oversize:
            send(
                protocol.error_response(
                    None,
                    protocol.INVALID_REQUEST,
                    f"Invalid Request: message longer than {MAX_MESSAGE_BYTES} bytes",
                )
            )
            continue
        try:
            reply = server.handle_line(line, send)
        except Exception:  # the loop must outlive any bug in a handler
            _log.exception("Unhandled error while handling a message")
            reply = protocol.error_response(
                None, protocol.INTERNAL_ERROR, "Internal error (see the server log)"
            )
        if reply is not None:
            send(reply)


# Entry point


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lcsc2fusion-mcp",
        description=(
            "MCP server (stdio) for lcsc2fusion: search LCSC parts, convert them to .lbr and "
            "add them to Fusion through the running add-in. See docs/MCP.md."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--verbose", action="store_true", help="log debug output to stderr")
    return parser


def _tolerate_console_encoding() -> None:
    """Never crash while logging text that the console encoding cannot represent."""
    reconfigure = getattr(sys.stderr, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="replace")


def main(
    argv: Sequence[str] | None = None,
    *,
    server: McpServer | None = None,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
) -> int:
    """Run the server on stdin and stdout. `server`, `stdin` and `stdout` exist for tests."""
    args = _build_parser().parse_args(argv)
    _tolerate_console_encoding()
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="lcsc2fusion-mcp %(levelname)s: %(message)s",
    )
    in_stream = stdin if stdin is not None else sys.stdin.buffer
    out_stream = stdout if stdout is not None else sys.stdout.buffer
    if stdin is None and sys.stdin.isatty():
        print(
            "lcsc2fusion MCP server: waiting for JSON-RPC messages on stdin. It is meant to be "
            "started by an MCP client such as Claude Code, see docs/MCP.md.",
            file=sys.stderr,
        )
    original_stdout = sys.stdout
    if stdout is None:
        sys.stdout = sys.stderr  # a stray print must never end up in the protocol stream
    try:
        serve(server if server is not None else McpServer(), in_stream, out_stream)
    except KeyboardInterrupt:
        _log.info("interrupted")
    finally:
        sys.stdout = original_stdout
    return 0
