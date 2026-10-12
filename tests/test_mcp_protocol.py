"""JSON-RPC and MCP behaviour of `McpServer.handle_message` / `handle_line` (no processes)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from lcsc2fusion import __version__
from lcsc2fusion.errors import LcscError
from lcsc2fusion.mcp import protocol
from lcsc2fusion.mcp.server import INSTRUCTIONS, McpServer, encode_message
from lcsc2fusion.mcp.tools import Backend, Tool, ToolContext, ToolResult, build_tools
from lcsc2fusion.mcp.validation import InvalidArguments

TOOL_NAMES = [
    "lcsc_search",
    "lcsc_part_info",
    "lcsc_convert",
    "fusion_status",
    "fusion_add_part",
    "fusion_job",
]


def req(method: str, params: Any = None, id: Any = 1) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def note(method: str, params: Any = None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    return message


def reply(server: McpServer, message: Any, notify: Any = None) -> dict[str, Any]:
    response = server.handle_message(message, notify)
    assert response is not None
    json.dumps(response)  # every reply must be serialisable
    return response


def error_code(response: Mapping[str, Any]) -> int:
    assert "result" not in response
    code = response["error"]["code"]
    assert isinstance(code, int)
    return code


@pytest.fixture
def server() -> McpServer:
    return McpServer(Backend())


# initialize


@pytest.mark.parametrize("version", ["2025-06-18", "2025-03-26", "2024-11-05"])
def test_initialize_answers_with_the_clients_supported_version(
    server: McpServer, version: str
) -> None:
    response = reply(
        server,
        req(
            "initialize",
            {
                "protocolVersion": version,
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "1"},
            },
        ),
    )
    assert response["id"] == 1 and response["jsonrpc"] == "2.0"
    assert response["result"]["protocolVersion"] == version


@pytest.mark.parametrize("version", ["1999-01-01", "2026-01-01", "", "latest", "2025-06-18 "])
def test_initialize_with_an_unknown_version_answers_the_newest(
    server: McpServer, version: str
) -> None:
    response = reply(server, req("initialize", {"protocolVersion": version}))
    assert response["result"]["protocolVersion"] == "2025-06-18"


def test_initialize_result_shape(server: McpServer) -> None:
    result = reply(server, req("initialize", {"protocolVersion": "2025-06-18"}))["result"]
    assert result["capabilities"] == {"tools": {}}
    assert result["serverInfo"] == {"name": "lcsc2fusion", "version": __version__}
    assert result["instructions"] == INSTRUCTIONS
    text = result["instructions"]
    for needle in ("personal use", "JLCEDA/EasyEDA", "bulk", "fusion_add_part", "lcsc_search"):
        assert needle in text


@pytest.mark.parametrize(
    "params",
    [{}, {"protocolVersion": 20250618}, {"protocolVersion": None}, {"protocolVersion": ["x"]}],
)
def test_initialize_needs_a_string_protocol_version(server: McpServer, params: Any) -> None:
    assert error_code(reply(server, req("initialize", params))) == protocol.INVALID_PARAMS


def test_initialize_without_params_is_invalid(server: McpServer) -> None:
    assert error_code(reply(server, req("initialize"))) == protocol.INVALID_PARAMS


def test_initialize_rejects_non_object_capabilities(server: McpServer) -> None:
    response = reply(
        server, req("initialize", {"protocolVersion": "2025-06-18", "capabilities": 3})
    )
    assert error_code(response) == protocol.INVALID_PARAMS


def test_the_version_can_be_overridden() -> None:
    custom = McpServer(Backend(), version="9.9.9")
    result = reply(custom, req("initialize", {"protocolVersion": "2025-06-18"}))["result"]
    assert result["serverInfo"]["version"] == "9.9.9"


# notifications and ping


@pytest.mark.parametrize(
    "message",
    [
        note("notifications/initialized"),
        note("notifications/initialized", {"junk": 1}),
        note("notifications/cancelled", {"requestId": 1, "reason": "user"}),
        note("notifications/roots/list_changed"),
        note("some/unknown/notification", [1, 2]),
        note("tools/call", {"name": "nope"}),  # a request method sent as a notification
        note("ping"),
    ],
)
def test_notifications_never_get_a_reply(server: McpServer, message: dict[str, Any]) -> None:
    assert server.handle_message(message) is None


def test_responses_from_the_client_are_ignored(server: McpServer) -> None:
    assert server.handle_message({"jsonrpc": "2.0", "id": 5, "result": {}}) is None
    assert server.handle_message({"jsonrpc": "2.0", "id": 5, "error": {"code": 1}}) is None


@pytest.mark.parametrize("request_id", [0, 1, -5, 2**40, "abc", "", "ünï"])
def test_ping_returns_an_empty_result_with_the_same_id(server: McpServer, request_id: Any) -> None:
    response = reply(server, req("ping", id=request_id))
    assert response == {"jsonrpc": "2.0", "id": request_id, "result": {}}


def test_ping_works_before_initialize(server: McpServer) -> None:
    assert reply(server, req("ping"))["result"] == {}


# unknown methods and invalid requests


def test_unknown_method_is_method_not_found(server: McpServer) -> None:
    response = reply(server, req("resources/list", id=7))
    assert error_code(response) == protocol.METHOD_NOT_FOUND
    assert response["id"] == 7


def test_unknown_method_name_in_the_error_is_truncated(server: McpServer) -> None:
    response = reply(server, req("x" * 5000))
    assert len(response["error"]["message"]) < 200


@pytest.mark.parametrize("message", [123, "text", None, True, 1.5])
def test_non_object_messages_are_invalid_requests(server: McpServer, message: Any) -> None:
    response = reply(server, message)
    assert error_code(response) == protocol.INVALID_REQUEST
    assert response["id"] is None


@pytest.mark.parametrize("batch", [[], [req("ping")], [req("ping"), note("x")], [1, 2]])
def test_batches_are_rejected(server: McpServer, batch: list[Any]) -> None:
    response = reply(server, batch)
    assert error_code(response) == protocol.INVALID_REQUEST
    assert response["id"] is None
    assert "batch" in response["error"]["message"]


@pytest.mark.parametrize(
    "message",
    [
        {},
        {"foo": "bar"},
        {"jsonrpc": "2.0"},
        {"jsonrpc": "2.0", "id": 1},
        {"jsonrpc": "1.0", "id": 1, "method": "ping"},
        {"id": 1, "method": "ping"},
        {"jsonrpc": 2.0, "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": 1, "method": 5},
        {"jsonrpc": "2.0", "id": 1, "method": None},
        {"jsonrpc": "2.0", "id": 1, "method": ["ping"]},
        {"method": "ping"},  # not JSON-RPC 2.0, so not a notification either
    ],
)
def test_malformed_requests_are_invalid_requests(server: McpServer, message: Any) -> None:
    assert error_code(reply(server, message)) == protocol.INVALID_REQUEST


def test_the_error_of_an_invalid_request_keeps_a_valid_id(server: McpServer) -> None:
    response = reply(server, {"jsonrpc": "1.0", "id": 42, "method": "ping"})
    assert response["id"] == 42


@pytest.mark.parametrize("bad_id", [None, 1.5, True, False, [], {}, [1]])
def test_ids_must_be_strings_or_integers(server: McpServer, bad_id: Any) -> None:
    response = reply(server, {"jsonrpc": "2.0", "id": bad_id, "method": "ping"})
    assert error_code(response) == protocol.INVALID_REQUEST
    assert response["id"] is None


@pytest.mark.parametrize("params", [[], [1], "text", 5, True])
def test_params_must_be_an_object(server: McpServer, params: Any) -> None:
    for method in ("ping", "tools/list", "tools/call", "initialize"):
        response = reply(server, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        assert error_code(response) == protocol.INVALID_PARAMS


def test_null_params_count_as_missing(server: McpServer) -> None:
    assert (
        reply(server, {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": None})["result"] == {}
    )


# tools/list


def test_tools_list_names_and_order(server: McpServer) -> None:
    tools = reply(server, req("tools/list"))["result"]["tools"]
    assert [tool["name"] for tool in tools] == TOOL_NAMES


def test_tools_list_entries_are_valid(server: McpServer) -> None:
    tools = reply(server, req("tools/list", {}))["result"]["tools"]
    for tool in tools:
        assert set(tool) == {"name", "title", "description", "inputSchema", "annotations"}
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert set(schema.get("required", [])) <= set(schema["properties"])
        annotations = tool["annotations"]
        assert annotations["destructiveHint"] is False
        assert isinstance(annotations["readOnlyHint"], bool)
        assert isinstance(annotations["idempotentHint"], bool)
        assert isinstance(annotations["openWorldHint"], bool)
        assert annotations["title"] == tool["title"]
        assert 40 < len(tool["description"]) < 1500
    json.dumps(tools)


def test_tool_annotations_by_tool(server: McpServer) -> None:
    tools = {t["name"]: t for t in reply(server, req("tools/list"))["result"]["tools"]}
    read_only = {name for name, tool in tools.items() if tool["annotations"]["readOnlyHint"]}
    assert read_only == {"lcsc_search", "lcsc_part_info", "fusion_status", "fusion_job"}
    for name in ("lcsc_search", "lcsc_part_info", "lcsc_convert", "fusion_add_part"):
        assert tools[name]["annotations"]["openWorldHint"] is True
    for name in ("fusion_status", "fusion_job"):
        assert tools[name]["annotations"]["openWorldHint"] is False


def test_schema_defaults_and_bounds(server: McpServer) -> None:
    tools = {t["name"]: t for t in reply(server, req("tools/list"))["result"]["tools"]}
    search = tools["lcsc_search"]["inputSchema"]["properties"]
    assert search["query"]["maxLength"] == 100
    assert (search["limit"]["minimum"], search["limit"]["maximum"]) == (1, 20)
    assert search["limit"]["default"] == 10
    add = tools["fusion_add_part"]["inputSchema"]["properties"]
    assert add["with_3d"]["default"] is False and add["wait"]["default"] is True
    assert (add["timeout_s"]["minimum"], add["timeout_s"]["maximum"]) == (10, 600)
    assert add["timeout_s"]["default"] == 240
    assert tools["lcsc_convert"]["inputSchema"]["required"] == ["lcsc_id"]
    assert tools["fusion_job"]["inputSchema"]["required"] == ["job"]


def test_fusion_add_part_description_warns_about_the_human_step(server: McpServer) -> None:
    tools = {t["name"]: t for t in reply(server, req("tools/list"))["result"]["tools"]}
    description = tools["fusion_add_part"]["description"]
    for needle in ("Finish", "Save", "OK", "minute", "fusion_job", "3D"):
        assert needle in description


def test_tools_list_cursor(server: McpServer) -> None:
    assert reply(server, req("tools/list", {"cursor": None}))["result"]["tools"]
    assert reply(server, req("tools/list", {"cursor": ""}))["result"]["tools"]
    assert (
        error_code(reply(server, req("tools/list", {"cursor": "page2"}))) == protocol.INVALID_PARAMS
    )
    assert error_code(reply(server, req("tools/list", {"cursor": 3}))) == protocol.INVALID_PARAMS


# tools/call envelope


@pytest.mark.parametrize(
    "params",
    [{}, {"name": 5}, {"name": None}, {"name": ["lcsc_search"]}, {"arguments": {}}],
)
def test_tools_call_needs_a_string_name(server: McpServer, params: Any) -> None:
    assert error_code(reply(server, req("tools/call", params))) == protocol.INVALID_PARAMS


def test_unknown_tool_is_invalid_params(server: McpServer) -> None:
    response = reply(server, req("tools/call", {"name": "delete_everything", "arguments": {}}))
    assert error_code(response) == protocol.INVALID_PARAMS
    assert "Unknown tool" in response["error"]["message"]


def test_unknown_tool_name_is_truncated_in_the_error(server: McpServer) -> None:
    response = reply(server, req("tools/call", {"name": "z" * 10_000}))
    assert len(response["error"]["message"]) < 200


@pytest.mark.parametrize("arguments", [[], "x", 5, True, [{"query": "x"}]])
def test_arguments_must_be_an_object(server: McpServer, arguments: Any) -> None:
    response = reply(server, req("tools/call", {"name": "fusion_status", "arguments": arguments}))
    assert error_code(response) == protocol.INVALID_PARAMS


def test_missing_and_null_arguments_count_as_empty(server: McpServer) -> None:
    for params in ({"name": "lcsc_search"}, {"name": "lcsc_search", "arguments": None}):
        response = reply(server, req("tools/call", params))
        assert error_code(response) == protocol.INVALID_PARAMS
        assert "query" in response["error"]["message"]


def test_a_tool_result_has_content_structured_content_and_the_error_flag() -> None:
    def handler(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        return ToolResult(text="hello ü", data={"echo": dict(arguments)})

    result = reply(
        server_with(handler), req("tools/call", {"name": "probe", "arguments": {"a": 1}})
    )["result"]
    assert result == {
        "content": [{"type": "text", "text": "hello ü"}],
        "structuredContent": {"echo": {"a": 1}},
        "isError": False,
    }


# Handler failures


def probe_tool(handler: Any) -> Tool:
    return Tool(
        name="probe",
        title="Probe",
        description="test tool",
        input_schema={"type": "object", "properties": {}},
        annotations={},
        handler=handler,
    )


def server_with(handler: Any) -> McpServer:
    return McpServer(Backend(), tools=[probe_tool(handler)])


def call_probe(server: McpServer, **kwargs: Any) -> dict[str, Any]:
    return reply(server, req("tools/call", {"name": "probe", "arguments": {}, **kwargs}))


def test_invalid_arguments_from_a_handler_become_invalid_params() -> None:
    def handler(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        raise InvalidArguments("'x' must be 3")

    response = call_probe(server_with(handler))
    assert error_code(response) == protocol.INVALID_PARAMS
    assert response["error"]["message"] == "'x' must be 3"


def test_expected_failures_become_error_results_not_protocol_errors() -> None:
    def handler(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        raise LcscError("The part is gone.")

    result = call_probe(server_with(handler))["result"]
    assert result["isError"] is True
    assert "The part is gone." in result["content"][0]["text"]
    assert result["structuredContent"]["error_kind"] == "error"


def test_os_errors_become_error_results() -> None:
    def handler(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        raise PermissionError(13, "Permission denied", "C:\\secret\\path")

    result = call_probe(server_with(handler))["result"]
    assert result["isError"] is True
    assert "Permission denied" in result["content"][0]["text"]
    assert "secret" not in result["content"][0]["text"]
    assert result["structuredContent"]["error_kind"] == "filesystem"


def test_bugs_become_generic_error_results_without_a_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        raise ZeroDivisionError("secret internals")

    result = call_probe(server_with(handler))["result"]
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "Internal error" in text
    assert "secret internals" not in text and "Traceback" not in text
    assert "secret internals" in caplog.text  # the details went to the log


def test_a_bug_outside_the_tool_is_an_internal_error(
    server: McpServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(self: McpServer, params: Any, notify: Any) -> dict[str, Any]:
        raise RuntimeError("kaput")

    monkeypatch.setattr(McpServer, "_call_tool", boom)
    response = reply(server, req("tools/call", {"name": "fusion_status"}))
    assert error_code(response) == protocol.INTERNAL_ERROR
    assert "kaput" not in response["error"]["message"]


# Progress


def progress_tool(messages: list[str]) -> McpServer:
    def handler(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        for message in messages:
            context.progress(message)
        return ToolResult(text="ok", data={})

    return server_with(handler)


@pytest.mark.parametrize("token", ["tok", 7, 0, "ünï"])
def test_progress_notifications_use_the_clients_token(token: Any) -> None:
    sent: list[dict[str, Any]] = []
    server = progress_tool(["first", "second", "third"])
    response = reply(
        server,
        req("tools/call", {"name": "probe", "arguments": {}, "_meta": {"progressToken": token}}),
        sent.append,
    )
    assert response["result"]["isError"] is False
    assert [m["method"] for m in sent] == ["notifications/progress"] * 3
    assert [m["params"]["progressToken"] for m in sent] == [token] * 3
    assert [m["params"]["progress"] for m in sent] == [1, 2, 3]
    assert [m["params"]["message"] for m in sent] == ["first", "second", "third"]
    assert all("id" not in m and m["jsonrpc"] == "2.0" for m in sent)


@pytest.mark.parametrize(
    "meta",
    [
        None,
        {},
        {"progressToken": None},
        {"progressToken": True},
        {"progressToken": 1.5},
        {"progressToken": [1]},
        "x",
        5,
    ],
)
def test_no_progress_without_a_usable_token(meta: Any) -> None:
    sent: list[dict[str, Any]] = []
    params: dict[str, Any] = {"name": "probe", "arguments": {}}
    if meta is not None:
        params["_meta"] = meta
    reply(progress_tool(["a", "b"]), req("tools/call", params), sent.append)
    assert sent == []


def test_a_token_without_a_notify_callback_is_harmless() -> None:
    params = {"name": "probe", "arguments": {}, "_meta": {"progressToken": 1}}
    assert reply(progress_tool(["a"]), req("tools/call", params))["result"]["isError"] is False


# Raw lines


def test_malformed_json_is_a_parse_error_with_a_null_id(server: McpServer) -> None:
    for line in (b"{not json", b'{"jsonrpc": "2.0", "id": 1,', b"]", b"'single'", b"\x00\x01"):
        response = server.handle_line(line)
        assert response is not None and error_code(response) == protocol.PARSE_ERROR
        assert response["id"] is None


def test_non_standard_json_constants_are_parse_errors(server: McpServer) -> None:
    for line in (b'{"jsonrpc":"2.0","id":NaN,"method":"ping"}', b'{"a": Infinity}', b"[-Infinity]"):
        response = server.handle_line(line)
        assert response is not None and error_code(response) == protocol.PARSE_ERROR


def test_invalid_utf8_is_a_parse_error(server: McpServer) -> None:
    response = server.handle_line(b'{"jsonrpc":"2.0","id":1,"method":"\xff\xfe"}')
    assert response is not None and error_code(response) == protocol.PARSE_ERROR


def test_very_deep_nesting_is_a_parse_error_not_a_crash(server: McpServer) -> None:
    response = server.handle_line(b"[" * 200_000 + b"]" * 200_000)
    assert response is not None and error_code(response) == protocol.PARSE_ERROR


def test_blank_lines_are_ignored(server: McpServer) -> None:
    for line in (b"", b"\n", b"  \r\n", b"\t"):
        assert server.handle_line(line) is None


def test_a_valid_line_with_crlf_and_bom_works(server: McpServer) -> None:
    line = b'\xef\xbb\xbf{"jsonrpc":"2.0","id":1,"method":"ping"}\r\n'
    response = server.handle_line(line)
    assert response is not None and response["result"] == {}


def test_a_valid_non_ascii_request_works(server: McpServer) -> None:
    line = json.dumps(req("ping", id="ünï 日本 🙂"), ensure_ascii=False).encode("utf-8")
    response = server.handle_line(line)
    assert response is not None and response["id"] == "ünï 日本 🙂"


# Encoding


def test_encoded_replies_are_one_ascii_line() -> None:
    message = {"jsonrpc": "2.0", "id": 1, "result": {"text": "ü 日本 🙂 \n\r\t \u2028 \ud800"}}
    data = encode_message(message)
    assert data.endswith(b"\n") and data.count(b"\n") == 1
    data.decode("ascii")  # must not raise, even for a lone surrogate
    assert json.loads(data)["result"]["text"] == message["result"]["text"]  # type: ignore[index]


def test_encoded_replies_have_no_spaces_after_separators() -> None:
    assert encode_message({"a": 1, "b": [1, 2]}) == b'{"a":1,"b":[1,2]}\n'


def test_tool_names_match_the_registry() -> None:
    assert [tool.name for tool in build_tools()] == TOOL_NAMES
