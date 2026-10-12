"""JSON-RPC 2.0 and MCP constants and message builders (stdlib only)."""

from __future__ import annotations

from typing import Any

JSONRPC_VERSION = "2.0"

# Protocol versions this server can speak, newest first. The server has no features that differ
# between them (tools only), so it answers with the client's version when it is on the list.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

SERVER_NAME = "lcsc2fusion"

# JSON-RPC error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

RequestId = str | int
Message = dict[str, Any]


def negotiate_protocol_version(requested: str) -> str:
    """The client's version when supported, else the newest one this server knows."""
    if requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return LATEST_PROTOCOL_VERSION


def result_response(request_id: RequestId, result: dict[str, Any]) -> Message:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def error_response(request_id: RequestId | None, code: int, message: str) -> Message:
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def notification(method: str, params: dict[str, Any]) -> Message:
    return {"jsonrpc": JSONRPC_VERSION, "method": method, "params": params}


def is_request_id(value: object) -> bool:
    """A JSON-RPC id as MCP allows it: a string or an integer (never null, never a bool)."""
    return isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))
