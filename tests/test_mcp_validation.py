"""Argument and path validation, the lazy backend, and the EasyEDA client settings."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from mcp_fakes import StubEasyEda

from lcsc2fusion.mcp import tools
from lcsc2fusion.mcp import validation as v
from lcsc2fusion.mcp.server import McpServer
from lcsc2fusion.mcp.validation import InvalidArguments, OutDirError

DEFAULT = Path("/default/libs") if os.name != "nt" else Path("C:/default/libs")
ABSOLUTE = Path(os.path.abspath(os.sep + "base"))


# Scalars


@pytest.mark.parametrize("value", [True, False])
def test_as_bool_accepts_booleans(value: bool) -> None:
    assert v.as_bool(value, "x") is value


@pytest.mark.parametrize("value", [0, 1, "true", "false", None, [], {}, 1.0])
def test_as_bool_refuses_everything_else(value: Any) -> None:
    with pytest.raises(InvalidArguments, match="'x'"):
        v.as_bool(value, "x")


@pytest.mark.parametrize(("value", "expected"), [(1, 1), (20, 20), (5.0, 5), (-0.0, 0)])
def test_as_int_accepts_integers_and_integral_floats(value: Any, expected: int) -> None:
    low = min(expected, 0)
    assert v.as_int(value, "n", low, 20) == expected


@pytest.mark.parametrize(
    "value", [True, False, "3", None, 1.5, float("nan"), float("inf"), [1], 0, 21, -1]
)
def test_as_int_refuses_bad_values(value: Any) -> None:
    with pytest.raises(InvalidArguments, match="'n'"):
        v.as_int(value, "n", 1, 20)


@pytest.mark.parametrize(("value", "expected"), [(10, 10.0), (12.5, 12.5), (600, 600.0)])
def test_as_number_accepts_numbers_in_range(value: Any, expected: float) -> None:
    assert v.as_number(value, "t", 10, 600) == expected


@pytest.mark.parametrize(
    "value", [True, "10", None, float("nan"), float("inf"), float("-inf"), 9.99, 600.01, [10]]
)
def test_as_number_refuses_bad_values(value: Any) -> None:
    with pytest.raises(InvalidArguments, match="'t'"):
        v.as_number(value, "t", 10, 600)


# Text


def test_as_text_accepts_ordinary_and_unicode_text() -> None:
    assert v.as_text("ü 日本 🙂 \n tab\t", "t") == "ü 日本 🙂 \n tab\t"


@pytest.mark.parametrize("value", [5, None, b"bytes", ["a"], "a\x00b", "\ud800", "x" * 1001])
def test_as_text_refuses_bad_text(value: Any) -> None:
    with pytest.raises(InvalidArguments):
        v.as_text(value, "t")


def test_as_query_collapses_whitespace_and_counts_after_collapsing() -> None:
    assert v.as_query("  a \n\t b   c ") == "a b c"
    assert v.as_query("x" * 100) == "x" * 100
    assert v.as_query("x" * 49 + "    " + "x" * 50) == "x" * 49 + " " + "x" * 50  # 100 characters


@pytest.mark.parametrize("value", ["", " \t\n ", "x" * 101, "a\x01b", "a\x7fb", "a\x1bb"])
def test_as_query_refuses_bad_queries(value: str) -> None:
    with pytest.raises(InvalidArguments):
        v.as_query(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("C2040", "C2040"),
        ("c2040", "C2040"),
        (" C1 ", "C1"),
        ("\tc123456789012\n", "C123456789012"),
    ],
)
def test_as_lcsc_id_normalises(value: str, expected: str) -> None:
    assert v.as_lcsc_id(value) == expected


@pytest.mark.parametrize(
    "value",
    ["", "C", "C0x1", "C1.5", "C-1", "C1234567890123", "\u212a1", "\uff23123", "C\u0661", 5, None],
)
def test_as_lcsc_id_refuses_everything_else(value: Any) -> None:
    with pytest.raises(InvalidArguments):
        v.as_lcsc_id(value)


def test_reject_unknown_names_the_extras_and_the_allowed_set() -> None:
    with pytest.raises(InvalidArguments, match=r"Unknown argument\(s\): b, c\. Allowed: a\."):
        v.reject_unknown({"a": 1, "b": 2, "c": 3}, ("a",))
    v.reject_unknown({}, ())


def test_require() -> None:
    assert v.require({"a": None}, "a") is None
    with pytest.raises(InvalidArguments, match="Missing required argument 'a'"):
        v.require({}, "a")


# Output directory


def test_resolve_out_dir_normalises_an_absolute_path() -> None:
    messy = ABSOLUTE / "a" / ".." / "b" / "."
    assert v.resolve_out_dir(str(messy), DEFAULT) == ABSOLUTE / "b"


def test_resolve_out_dir_strips_surrounding_whitespace() -> None:
    assert v.resolve_out_dir(f"  {ABSOLUTE}  ", DEFAULT) == ABSOLUTE


def test_resolve_out_dir_expands_tilde(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert v.resolve_out_dir("~/x", DEFAULT) == tmp_path / "x"
    assert v.resolve_out_dir("~", DEFAULT) == tmp_path


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "  ",
        "rel",
        "./rel",
        "../up",
        "\\\\host\\share",
        "//host/share",
        "\\\\?\\C:\\x",
        "a\x00b",
        "x" * 1025,
        7,
        ["a"],
    ],
)
def test_resolve_out_dir_refuses_bad_values(value: Any) -> None:
    with pytest.raises(InvalidArguments):
        v.resolve_out_dir(value, DEFAULT)


def test_the_relative_path_error_names_the_default() -> None:
    with pytest.raises(InvalidArguments) as info:
        v.resolve_out_dir("rel", DEFAULT)
    assert str(DEFAULT) in str(info.value)


def test_prepare_out_dir_creates_and_proves_it_is_writable(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b"
    assert v.prepare_out_dir(target) == target
    assert target.is_dir() and list(target.iterdir()) == []  # the probe file is gone
    assert v.prepare_out_dir(target) == target  # existing is fine


def test_prepare_out_dir_refuses_a_file(tmp_path: Path) -> None:
    plain = tmp_path / "f"
    plain.write_text("x", encoding="utf-8")
    with pytest.raises(OutDirError, match="not a directory"):
        v.prepare_out_dir(plain)
    with pytest.raises(OutDirError, match="Cannot create"):
        v.prepare_out_dir(plain / "sub")


def test_prepare_out_dir_reports_an_unwritable_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def deny(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(v.tempfile, "TemporaryFile", deny)
    with pytest.raises(OutDirError, match="Cannot write to the directory"):
        v.prepare_out_dir(tmp_path)


# Backend and client


def test_the_client_is_created_once_and_only_when_needed() -> None:
    created: list[int] = []

    def factory() -> Any:
        created.append(1)
        return StubEasyEda()

    backend = tools.Backend(client_factory=factory)
    server = McpServer(backend)
    server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        }
    )
    server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    server.handle_message({"jsonrpc": "2.0", "id": 3, "method": "ping"})
    assert created == []  # starting up and listing tools touch neither disk nor network
    assert backend.client() is backend.client()
    assert created == [1]


def test_make_client_uses_short_timeouts_and_two_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(tools, "EasyEdaClient", lambda **kwargs: seen.update(kwargs))
    tools.make_client()
    assert seen == {
        "timeout_s": tools.CLIENT_TIMEOUT_S,
        "retry_attempts": tools.CLIENT_RETRY_ATTEMPTS,
    }
    assert tools.CLIENT_TIMEOUT_S <= 30 and tools.CLIENT_RETRY_ATTEMPTS == 2


def test_default_backend_wires_the_real_service_functions() -> None:
    from lcsc2fusion import service

    backend = tools.Backend()
    assert backend.search is service.search
    assert backend.load_part is service.load_part
    assert backend.convert is service.convert_parts


def test_failure_results_never_carry_a_stack_trace() -> None:
    from lcsc2fusion.errors import ParseError

    result = tools.failure_result(ParseError("easyeda.com sent\nbad JSON\x00"))
    assert result.is_error and result.data["error_kind"] == "parse_error"
    assert "\n" not in result.text and "\x00" not in result.text
    assert "Traceback" not in result.text
