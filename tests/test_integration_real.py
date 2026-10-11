"""End to end checks on real EasyEDA responses (local only, skipped without the directory)."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

from lcsc2fusion.lbr_writer import build_library, lint_library
from lcsc2fusion.model import Part
from lcsc2fusion.parser.part import parse_part

REAL_DIR = Path(__file__).parent / "fixtures" / "real"
REAL_FILES = sorted(REAL_DIR.glob("*.json")) if REAL_DIR.is_dir() else []

pytestmark = [
    pytest.mark.real,
    pytest.mark.skipif(not REAL_FILES, reason="no real fixtures in tests/fixtures/real"),
]


def _load(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _pin_numbers(part: Part) -> set[str]:
    return {pin.number for symbol in part.symbols for pin in symbol.pins}


def _pad_numbers(part: Part) -> set[str]:
    return {pad.number for pad in part.package.pads}


@pytest.fixture(scope="module")
def parsed() -> dict[str, Part]:
    return {path.stem: parse_part(_load(path)) for path in REAL_FILES}


@pytest.mark.parametrize("path", REAL_FILES, ids=lambda p: p.stem)
def test_full_pipeline_lints_clean(path: Path) -> None:
    part = parse_part(_load(path))
    xml = build_library([part])
    errors = [i for i in lint_library(xml) if i.severity == "error"]
    assert errors == []
    ET.fromstring(xml)


@pytest.mark.parametrize("path", REAL_FILES, ids=lambda p: p.stem)
def test_every_pin_has_a_pad_and_counts_match(path: Path) -> None:
    part = parse_part(_load(path))
    pins = _pin_numbers(part)
    pads = _pad_numbers(part)
    assert pins <= pads, f"pins without pad: {sorted(pins - pads)}"
    total_pins = sum(len(s.pins) for s in part.symbols)
    # Symbol pin count equals the number of distinct pad numbers wired to a pin.
    assert total_pins == len(pins)


def test_multi_part_library_lints_clean(parsed: dict[str, Part]) -> None:
    xml = build_library(list(parsed.values()))
    errors = [i for i in lint_library(xml) if i.severity == "error"]
    assert errors == []


def test_rp2040_reference_facts(parsed: dict[str, Part]) -> None:
    part = parsed.get("C2040")
    if part is None:
        pytest.skip("C2040 not available")
    assert sum(len(s.pins) for s in part.symbols) == 57
    assert len(part.package.pads) == 57
    assert part.prefix.rstrip("?") == "U"
