"""Full pipeline (EasyEDA JSON -> model -> .lbr XML) checked against independent expectations.

The expected values come from tests/fixtures/synthetic/*.expected.json, computed by the fixture
generator from its own parameters, so they do not depend on the parser or the writer. The checks
run on the final XML, i.e. what Fusion would read.
"""

from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

from lcsc2fusion.constants import GRID_MM
from lcsc2fusion.lbr_writer import build_library, lint_library
from lcsc2fusion.model import Part
from lcsc2fusion.parser.part import parse_part

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"
GOLDEN = Path(__file__).parent / "golden"
NAMES = sorted(p.name.removesuffix(".expected.json") for p in SYNTH.glob("*.expected.json"))
GOLDEN_PARTS = ["r0603", "cap0603_arc", "soic8", "qfn16_ep", "usbc_like", "module_dup", "opamp2"]
TOL = 1e-3


def _load(name: str, *, fit_symbols: bool = True) -> tuple[Part, dict[str, Any]]:
    doc = json.loads((SYNTH / f"{name}.json").read_text(encoding="utf-8"))
    expected = json.loads((SYNTH / f"{name}.expected.json").read_text(encoding="utf-8"))
    return parse_part(doc, fit_symbols=fit_symbols), expected


def _rot(value: str | None) -> float:
    if not value:
        return 0.0
    match = re.fullmatch(r"R(\d+(?:\.\d+)?)", value)
    assert match, value
    return float(match.group(1)) % 360.0


def _angle_close(a: float, b: float, tol: float = 1e-2) -> bool:
    return abs((a - b + 180.0) % 360.0 - 180.0) < tol


@pytest.mark.parametrize("name", NAMES)
def test_pipeline_output_is_lint_clean(name: str) -> None:
    part, _ = _load(name)
    xml = build_library([part])
    assert [i for i in lint_library(xml) if i.severity == "error"] == []


@pytest.mark.parametrize("name", NAMES)
def test_pads_in_xml_match_expected(name: str) -> None:
    part, expected = _load(name)
    root = ET.fromstring(build_library([part]))
    package = root.find(".//packages/package")
    assert package is not None
    pads = [e for e in package if e.tag in ("smd", "pad")]
    want = expected["pads"]
    assert len(pads) == len(want) == expected["pad_count"]
    for got, exp in zip(pads, want, strict=True):
        where = f"{name} pad {exp['number']}"
        assert float(got.get("x", "nan")) == pytest.approx(exp["x_mm"], abs=TOL), where
        assert float(got.get("y", "nan")) == pytest.approx(exp["y_mm"], abs=TOL), where
        if got.tag == "smd":
            assert exp["drill_mm"] == 0.0, where
            assert float(got.get("dx", "nan")) == pytest.approx(exp["width_mm"], abs=TOL), where
            assert float(got.get("dy", "nan")) == pytest.approx(exp["height_mm"], abs=TOL), where
            if exp["shape"] != "POLYGON":
                assert _angle_close(_rot(got.get("rot")), exp["rotation_deg"]), where
            assert got.get("layer") == ("16" if exp["layer"] == "bottom" else "1"), where
        else:
            assert exp["drill_mm"] > 0.0, where
            slot = exp.get("slot_length_mm", 0.0)
            if slot <= exp["drill_mm"] + TOL:
                assert float(got.get("drill", "nan")) == pytest.approx(exp["drill_mm"], abs=TOL)


@pytest.mark.parametrize("name", NAMES)
def test_holes_in_xml_match_expected(name: str) -> None:
    part, expected = _load(name)
    root = ET.fromstring(build_library([part]))
    holes = root.findall(".//packages/package/hole")
    assert len(holes) == len(expected["holes"])
    for got, exp in zip(holes, expected["holes"], strict=True):
        assert float(got.get("x", "nan")) == pytest.approx(exp["x_mm"], abs=TOL)
        assert float(got.get("y", "nan")) == pytest.approx(exp["y_mm"], abs=TOL)
        assert float(got.get("drill", "nan")) == pytest.approx(exp["drill_mm"], abs=TOL)


@pytest.mark.parametrize("name", NAMES)
def test_symbol_pins_in_xml_match_expected(name: str) -> None:
    # Source fidelity: symbol fitting (which moves pins on purpose) is switched off here.
    part, expected = _load(name, fit_symbols=False)
    root = ET.fromstring(build_library([part]))
    symbols = root.findall(".//symbols/symbol")
    assert len(symbols) == expected["unit_count"]
    pins_by_unit: dict[int, list[dict[str, Any]]] = {}
    for pin in expected["pins"]:
        pins_by_unit.setdefault(int(pin.get("unit", 1)), []).append(pin)
    for unit, symbol in enumerate(symbols, start=1):
        xml_pins = symbol.findall("pin")
        want = pins_by_unit[unit]
        assert len(xml_pins) == len(want), f"{name} unit {unit}"
        for got, exp in zip(xml_pins, want, strict=True):
            where = f"{name} pin {exp['number']}"
            assert (got.get("name") or "").split("@")[0] == exp["name"], where
            assert float(got.get("x", "nan")) == pytest.approx(exp["x_mm"], abs=TOL), where
            assert float(got.get("y", "nan")) == pytest.approx(exp["y_mm"], abs=TOL), where
            assert _angle_close(_rot(got.get("rot")), exp["angle_deg"]), where
            assert got.get("direction") == exp["direction"], where


@pytest.mark.parametrize("name", NAMES)
def test_pin_count_equals_connected_pads(name: str) -> None:
    """Every symbol pin is connected and the pin count equals the unique pad numbers it wires."""
    part, expected = _load(name)
    root = ET.fromstring(build_library([part]))
    connects = root.findall(".//devicesets/deviceset/devices/device/connects/connect")
    assert len(connects) == len(expected["pins"])
    connected_pads: set[str] = set()
    for connect in connects:
        pads = (connect.get("pad") or "").split()
        assert pads, f"{name}: connect without pads"
        connected_pads.update(pads)
    package = root.find(".//packages/package")
    assert package is not None
    names_by_number: dict[str, list[str]] = {}
    for pad, exp in zip(
        [e for e in package if e.tag in ("smd", "pad")], expected["pads"], strict=True
    ):
        names_by_number.setdefault(exp["number"], []).append(pad.get("name") or "")
    wired_numbers = {exp["number"] for exp in expected["pins"]}
    assert wired_numbers <= set(names_by_number)
    for number in wired_numbers:
        assert set(names_by_number[number]) <= connected_pads, f"{name}: pad {number} unconnected"


def test_duplicate_pad_numbers_are_all_wired_to_one_pin() -> None:
    part, _ = _load("module_dup")
    root = ET.fromstring(build_library([part]))
    connects = {
        c.get("pin"): (c.get("pad") or "").split()
        for c in root.findall(".//devicesets/deviceset/devices/device/connects/connect")
    }
    assert any(len(pads) == 4 for pads in connects.values())


@pytest.mark.parametrize("name", GOLDEN_PARTS)
def test_full_pipeline_golden(name: str) -> None:
    part, _ = _load(name)
    text = build_library([part])
    path = GOLDEN / f"pipeline_{name}.lbr"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        path.write_text(text, encoding="utf-8", newline="\n")
    assert path.exists(), f"missing golden {path.name}; run with UPDATE_GOLDEN=1"
    assert text == path.read_text(encoding="utf-8").replace("\r\n", "\n")


@pytest.mark.parametrize("name", NAMES)
def test_fitting_only_moves_pins_outwards_by_whole_grid_steps(name: str) -> None:
    """Fitted symbols keep pin order, numbers, names and sides; coordinates only move outwards."""
    plain, _ = _load(name, fit_symbols=False)
    fitted, _ = _load(name)
    assert len(plain.symbols) == len(fitted.symbols)
    for before, after in zip(plain.symbols, fitted.symbols, strict=True):
        assert before.name == after.name
        assert [(p.name, p.number, p.angle, p.direction, p.length) for p in before.pins] == [
            (p.name, p.number, p.angle, p.direction, p.length) for p in after.pins
        ]
        for old, new in zip(before.pins, after.pins, strict=True):
            dx, dy = new.x - old.x, new.y - old.y
            # The position along the edge never changes, the other coordinate moves outwards.
            outward = {
                0: (-dx, dy),  # left pin: x decreases
                180: (dx, dy),  # right pin: x increases
                270: (dy, dx),  # top pin: y increases
                90: (-dy, dx),  # bottom pin: y decreases
            }[old.angle]
            shift, along = outward
            assert abs(along) < 1e-6, f"{name} pin {old.number} moved along its edge"
            assert shift > -1e-6, f"{name} pin {old.number} moved inwards"
            steps = shift / GRID_MM
            assert abs(steps - round(steps)) < 1e-6, f"{name} pin {old.number}: {shift} mm"
        if before.pins != after.pins:
            assert any(
                "body enlarged" in w
                for w in fitted.warnings
                if w.startswith(f"Symbol {before.name}:")
            )


def test_fitting_changes_pipeline_output_only_for_symbols_that_do_not_fit() -> None:
    """Two pin symbols (names hidden), non rectangular bodies and short names stay as they are."""
    for name in ("r0603", "header1x6", "cap0603_arc", "opamp2", "module_dup"):
        plain, _ = _load(name, fit_symbols=False)
        fitted, _ = _load(name)
        assert plain == fitted, name
    for name in ("soic8", "qfn16_ep", "usbc_like", "xtal4"):
        plain, _ = _load(name, fit_symbols=False)
        fitted, _ = _load(name)
        assert plain.symbols != fitted.symbols, name
        assert plain.package == fitted.package, name
