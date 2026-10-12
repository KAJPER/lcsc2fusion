"""The MCP documentation and configuration files stay in step with the code."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from lcsc2fusion.mcp.server import INSTRUCTIONS
from lcsc2fusion.mcp.tools import build_tools

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "MCP.md"
README = ROOT / "README.md"
MCP_FILES = [
    DOC,
    ROOT / ".mcp.json",
    *sorted((ROOT / "lcsc2fusion" / "mcp").glob("*.py")),
    *sorted((ROOT / "tests").glob("test_mcp_*.py")),
    ROOT / "tests" / "mcp_fakes.py",
]


def prose(text: str) -> str:
    """The text without fenced code blocks (commands there may need a literal double hyphen)."""
    return re.sub(r"```.*?```", "", text, flags=re.DOTALL)


@pytest.mark.parametrize("path", MCP_FILES, ids=lambda p: p.name)
def test_no_em_or_en_dashes_and_no_double_hyphen_prose(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for dash in (chr(0x2014), chr(0x2013)):
        assert dash not in text
    if path.suffix == ".md":
        assert " -- " not in prose(text)


def test_the_doc_covers_every_tool_and_the_required_topics() -> None:
    text = DOC.read_text(encoding="utf-8")
    for tool in build_tools():
        assert f"`{tool.name}`" in text
    for needle in (
        "claude mcp add lcsc2fusion --scope user",
        "PYTHONPATH",
        "claude_desktop_config.json",
        "lcsc2fusion-mcp",
        "api.json",
        "%LOCALAPPDATA%",
        "Bearer",
        "X-LCSC",
        "JLCEDA/EasyEDA",
        "bulk",
        "Finish",
        "-32602",
        ".mcp.json",
    ):
        assert needle in text, needle


def test_the_readme_links_the_doc() -> None:
    text = README.read_text(encoding="utf-8")
    assert "## Use from another Claude (MCP)" in text
    assert "(docs/MCP.md)" in text


def test_project_scope_configuration() -> None:
    config = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
    assert config == {
        "mcpServers": {"lcsc2fusion": {"command": "python", "args": ["-m", "lcsc2fusion.mcp"]}}
    }


def test_the_instructions_carry_the_terms_note() -> None:
    assert "personal use" in INSTRUCTIONS and "JLCEDA/EasyEDA" in INSTRUCTIONS
