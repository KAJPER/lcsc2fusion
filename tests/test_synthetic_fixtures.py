"""Sanity tests for the synthetic EasyEDA fixtures in tests/fixtures/synthetic.

These tests do not use the project parser. They check that the generated documents have
the real EasyEDA field layout, that the `*.expected.json` sidecars agree with the shape
strings through a tiny independent re-reader, and that the committed files equal the
generator output byte for byte.
"""

from __future__ import annotations

import importlib.util
import json
import math
import re
import sys
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

TESTS_DIR = Path(__file__).resolve().parent
FIXTURE_DIR = TESTS_DIR / "fixtures" / "synthetic"
REAL_DIR = TESTS_DIR / "fixtures" / "real"
REAL_REF = REAL_DIR / "C25804.json"
UNIT = 0.254
LICENSE = "Synthetic test fixture, not EasyEDA data"
REQUIRED = {
    "r0603",
    "cap0603_arc",
    "led0603",
    "sot23",
    "soic8",
    "qfn16_ep",
    "tqfp32",
    "header1x6",
    "usbc_like",
    "module_dup",
    "xtal4",
    "inductor",
    "tact4",
    "opamp2",
}
LAYER_BY_ID = {"1": "top", "2": "bottom", "11": "thru"}
DIRECTION_BY_TYPE = {"1": "in", "2": "out", "3": "io", "4": "pwr", "0": "pas", "": "pas"}
SYMBOL_TOKENS = {
    "R": {13, 14},
    "C": {10},
    "E": {11},
    "PL": {8},
    "PG": {8},
    "PT": {9},
    "A": {9},
    "T": {18},
}
FOOTPRINT_TOKENS = {
    "PAD": {20},
    "TRACK": {7},
    "CIRCLE": {10},
    "ARC": {8},
    "RECT": {13},
    "HOLE": {6},
    "SOLIDREGION": {10},
    "TEXT": {15},
}
PIN_PATH = re.compile(r"M\s*(-?[\d.]+)[ ,]\s*(-?[\d.]+)\s*([hv])\s*(-?[\d.]+)")
ARC_PATH = re.compile(
    r"M\s*(-?[\d.]+)[ ,]\s*(-?[\d.]+)\s*A\s*([\d.]+)[ ,]\s*([\d.]+)\s+(-?[\d.]+)\s+([01])\s+([01])"
    r"\s+(-?[\d.]+)[ ,]\s*(-?[\d.]+)"
)


def _load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("synthetic_generate", FIXTURE_DIR / "generate.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["synthetic_generate"] = module  # dataclasses needs the module to be registered
    spec.loader.exec_module(module)
    return module


GEN = _load_generator()
NAMES = sorted(
    p.name[: -len(".json")]
    for p in FIXTURE_DIR.glob("*.json")
    if not p.name.endswith(".expected.json")
)


@cache
def load(name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    doc = json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))
    exp = json.loads((FIXTURE_DIR / f"{name}.expected.json").read_text(encoding="utf-8"))
    return doc, exp


def symbol_units(doc: dict[str, Any]) -> list[dict[str, Any]]:
    res = doc["result"]
    return [sp["dataStr"] for sp in res["subparts"]] if res.get("subparts") else [res["dataStr"]]


def footprint(doc: dict[str, Any]) -> dict[str, Any]:
    fp: dict[str, Any] = doc["result"]["packageDetail"]["dataStr"]
    return fp


def close(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol


# ---------------------------------------------------------------------------
# File set and generator
# ---------------------------------------------------------------------------


def test_committed_files_equal_generator_output() -> None:
    fresh = GEN.build_all()
    on_disk = {p.name: p.read_bytes() for p in FIXTURE_DIR.iterdir() if p.suffix == ".json"}
    assert sorted(on_disk) == sorted(fresh), "file set differs from the generator"
    stale = [name for name, data in fresh.items() if on_disk[name] != data]
    assert not stale, f"out of date, run python tests/fixtures/synthetic/generate.py: {stale}"


def test_generator_is_deterministic() -> None:
    assert GEN.build_all() == GEN.build_all()


def test_required_fixtures_exist() -> None:
    assert set(NAMES) >= REQUIRED


def test_feature_coverage() -> None:
    docs = {n: load(n) for n in NAMES}
    pads = [(n, p) for n, (_, e) in docs.items() for p in e["pads"]]
    assert any(p["shape"] == "POLYGON" for _, p in pads)
    assert any(p["shape"] == "OVAL" and p["slot_length_mm"] > 0 for _, p in pads)
    assert any(p["shape"] == "ELLIPSE" and p["layer"] == "thru" for _, p in pads)
    assert any(p["shape"] == "RECT" and p["layer"] == "thru" for _, p in pads)
    assert any(p["layer"] == "bottom" for _, p in pads)
    assert any(p["rotation_deg"] % 90 not in (0.0,) for _, p in pads)
    assert any(
        p["rotation_deg"] == 90.0 and not close(p["width_mm"], p["height_mm"], 1e-3)
        for _, p in pads
    )
    assert any(e["holes"] for _, e in docs.values())
    assert any(e["pad_count"] > e["unique_pad_numbers"] for _, e in docs.values())
    assert any(e["unit_count"] > 1 for _, e in docs.values())
    assert any(e["footprint_shapes"]["arcs"] for _, e in docs.values())
    assert any(any(not p["plated"] for p in e["pads"]) for _, e in docs.values())
    pins = [p for _, e in docs.values() for p in e["pins"]]
    assert any(p["inverted"] for p in pins)
    assert any(p["clock"] for p in pins)
    assert {p["direction"] for p in pins} == {"in", "out", "io", "pwr", "pas"}
    assert {p["angle_deg"] for p in pins} == {0, 90, 180, 270}
    tags = {
        s.split("~", 1)[0]
        for _, (d, _) in docs.items()
        for u in symbol_units(d)
        for s in u["shape"]
    }
    assert {"R", "C", "E", "PL", "PG", "PT", "A", "T", "P"} <= tags
    fp_tags = {s.split("~", 1)[0] for _, (d, _) in docs.items() for s in footprint(d)["shape"]}
    expected = {"PAD", "TRACK", "CIRCLE", "ARC", "RECT", "HOLE", "SOLIDREGION", "TEXT", "SVGNODE"}
    assert expected <= fp_tags


# ---------------------------------------------------------------------------
# Structure of the documents
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", NAMES)
def test_identity_license_and_metadata(name: str) -> None:
    doc, exp = load(name)
    assert doc["success"] is True
    assert doc["code"] == 0
    res = doc["result"]
    assert re.fullmatch(r"C9\d{6}", res["lcsc"]["number"])
    assert res["lcsc"]["number"] == res["szlcsc"]["number"] == exp["lcsc_id"]
    assert res["title"].startswith("SYN-")
    assert res["title"] == exp["mpn"]
    fp = footprint(doc)
    heads = [res["dataStr"]["head"], fp["head"], *[u["head"] for u in symbol_units(doc)]]
    assert all(h["license"] == LICENSE for h in heads)
    c_para = res["dataStr"]["head"]["c_para"]
    assert c_para["Supplier Part"] == exp["lcsc_id"]
    assert c_para["Manufacturer Part"] == exp["mpn"]
    assert c_para["pre"] == exp["prefix"] + "?"
    assert c_para["package"] == exp["package_name"] == fp["head"]["c_para"]["package"]
    assert res["packageDetail"]["title"] == exp["package_name"]
    assert res["dataStr"]["head"]["puuid"] == res["packageDetail"]["uuid"]
    assert exp["unit_count"] == len(symbol_units(doc))


@pytest.mark.parametrize("name", NAMES)
def test_synthetic_text_has_no_easyeda_license_or_branding(name: str) -> None:
    text = (FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8")
    for needle in ("JLCEDA", "lceda.cn", "easyeda.com", "EasyEDA Official Library", "嘉立创"):
        assert needle not in text


@pytest.mark.skipif(not REAL_REF.exists(), reason="real fixture C25804 is local only")
@pytest.mark.parametrize("name", NAMES)
def test_same_structural_keys_as_real_fixture(name: str) -> None:
    real = json.loads(REAL_REF.read_text(encoding="utf-8"))["result"]
    res = load(name)[0]["result"]
    exp = load(name)[1]
    assert set(res) - {"subparts"} == set(real)
    for key in ("lcsc", "szlcsc", "owner"):
        assert set(res[key]) == set(real[key])
    assert set(res["dataStr"]) == set(real["dataStr"])
    assert set(res["dataStr"]["head"]) == set(real["dataStr"]["head"])
    assert set(res["dataStr"]["BBox"]) == set(real["dataStr"]["BBox"])
    assert len(res["dataStr"]["canvas"].split("~")) == len(real["dataStr"]["canvas"].split("~"))
    real_cpara = set(real["dataStr"]["head"]["c_para"])
    if exp["value"] is None:
        real_cpara -= {"nameAlias", "Value"}
    assert set(res["dataStr"]["head"]["c_para"]) - {"subpart_no"} == real_cpara
    assert set(res["packageDetail"]) == set(real["packageDetail"])
    fp, real_fp = footprint(load(name)[0]), real["packageDetail"]["dataStr"]
    assert set(fp) == set(real_fp)
    assert set(fp["head"]) == set(real_fp["head"])
    assert set(fp["head"]["c_para"]) == set(real_fp["head"]["c_para"])
    assert set(fp["BBox"]) == set(real_fp["BBox"])
    assert len(fp["canvas"].split("~")) == len(real_fp["canvas"].split("~"))
    assert len(fp["layers"]) == len(real_fp["layers"])
    assert [len(x.split("~")) for x in fp["layers"]] == [
        len(x.split("~")) for x in real_fp["layers"]
    ]
    assert fp["objects"] == real_fp["objects"]


@pytest.mark.parametrize("name", NAMES)
def test_shape_strings_have_documented_field_counts(name: str) -> None:
    doc = load(name)[0]
    for unit in symbol_units(doc):
        for shape in unit["shape"]:
            tag = shape.split("~", 1)[0]
            if tag == "P":
                sections = shape.split("^^")
                assert [len(s.split("~")) for s in sections] == [9, 2, 2, 9, 9, 3, 2]
            else:
                assert tag in SYMBOL_TOKENS, tag
                assert len(shape.split("~")) in SYMBOL_TOKENS[tag], shape
    for shape in footprint(doc)["shape"]:
        tag = shape.split("~", 1)[0]
        if tag == "SVGNODE":
            node = json.loads(shape.split("~", 1)[1])
            assert node["gId"] == "g1_outline"
            continue
        assert tag in FOOTPRINT_TOKENS, tag
        fields = shape.split("~")
        assert len(fields) in FOOTPRINT_TOKENS[tag], shape
        if tag == "PAD":
            assert len(fields) >= 16
            assert fields[1] in {"RECT", "OVAL", "ELLIPSE", "POLYGON"}


@pytest.mark.parametrize("name", NAMES)
def test_shape_ids_are_unique_per_document(name: str) -> None:
    doc = load(name)[0]
    for shapes in [u["shape"] for u in symbol_units(doc)] + [footprint(doc)["shape"]]:
        ids = []
        for shape in shapes:
            tag = shape.split("~", 1)[0]
            if tag == "SVGNODE":
                continue
            ids.append(shape.split("^^")[0].split("~")[7] if tag == "P" else _shape_id(shape))
        assert len(ids) == len(set(ids))


def _shape_id(shape: str) -> str:
    match = re.search(r"~(gge\d+)(?:~|$)", shape)
    assert match, shape
    return match.group(1)


# ---------------------------------------------------------------------------
# Expected sidecar consistency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", NAMES)
def test_expected_is_internally_consistent(name: str) -> None:
    exp = load(name)[1]
    pads = exp["pads"]
    numbers = [p["number"] for p in pads]
    assert exp["pad_count"] == len(pads) >= exp["unique_pad_numbers"] > 0
    assert exp["unique_pad_numbers"] == len(set(numbers))
    assert exp["pad_numbers"] == list(dict.fromkeys(numbers))
    pins = exp["pins"]
    pin_numbers = [p["number"] for p in pins]
    assert len(pin_numbers) == len(set(pin_numbers)), "pin numbers must be unique"
    assert set(pin_numbers) == set(numbers), "every pad number needs a pin and vice versa"
    assert len(pins) == exp["unique_pad_numbers"]
    for pin in pins:
        assert pin["angle_deg"] in (0, 90, 180, 270)
        assert pin["direction"] in {"in", "out", "io", "pwr", "pas"}
        assert 1 <= pin["unit"] <= exp["unit_count"]
        assert pin["length_mm"] > 0
    assert sum(u["pin_count"] for u in exp["symbol_units"]) == len(pins)
    box = exp["bbox_mm"]
    for pad in pads:
        assert pad["layer"] in {"top", "bottom", "thru"}
        if pad["layer"] == "thru":
            assert pad["drill_mm"] > 0
        else:
            assert pad["drill_mm"] == 0
        if pad["slot_length_mm"]:
            assert pad["slot_length_mm"] > pad["drill_mm"]
            assert pad["shape"] == "OVAL"
        lo_x, lo_y, hi_x, hi_y = pad["extent_mm"]
        assert box["min_x"] <= lo_x < hi_x <= box["max_x"]
        assert box["min_y"] <= lo_y < hi_y <= box["max_y"]
        assert lo_x < pad["x_mm"] < hi_x
        assert lo_y < pad["y_mm"] < hi_y
    for hole in exp["holes"]:
        assert hole["drill_mm"] > 0


@pytest.mark.parametrize("name", NAMES)
def test_pads_match_shape_strings(name: str) -> None:
    doc, exp = load(name)
    fp = footprint(doc)
    ox, oy = fp["head"]["x"], fp["head"]["y"]
    pad_strings = [s.split("~") for s in fp["shape"] if s.startswith("PAD~")]
    assert len(pad_strings) == len(exp["pads"])
    for f, pad in zip(pad_strings, exp["pads"], strict=True):
        assert f[8] == pad["number"]
        assert f[1] == pad["shape"]
        assert close((float(f[2]) - ox) * UNIT, pad["x_mm"])
        assert close(-(float(f[3]) - oy) * UNIT, pad["y_mm"])
        assert close(float(f[4]) * UNIT, pad["width_mm"])
        assert close(float(f[5]) * UNIT, pad["height_mm"])
        assert LAYER_BY_ID[f[6]] == pad["layer"]
        assert close(2 * float(f[9] or 0) * UNIT, pad["drill_mm"])
        assert float(f[11] or 0) % 360 == pad["rotation_deg"]
        assert (f[15] == "Y") == pad["plated"]
        assert close(float(f[13] or 0) * UNIT, pad["slot_length_mm"])
        assert f[19] == f"{f[2]},{f[3]}"
        pts = [float(v) for v in f[10].split()]
        assert len(pts) == 2 * len(pad["points_mm"])
        for i, (px, py) in enumerate(pad["points_mm"]):
            assert close((pts[2 * i] - ox) * UNIT, px)
            assert close(-(pts[2 * i + 1] - oy) * UNIT, py)
        assert len(f[14].split()) == 2 * len(pad["slot_axis_mm"])
    hole_strings = [s.split("~") for s in fp["shape"] if s.startswith("HOLE~")]
    assert len(hole_strings) == len(exp["holes"])
    for f, hole in zip(hole_strings, exp["holes"], strict=True):
        assert close((float(f[1]) - ox) * UNIT, hole["x_mm"])
        assert close(-(float(f[2]) - oy) * UNIT, hole["y_mm"])
        assert close(2 * float(f[3]) * UNIT, hole["drill_mm"])


@pytest.mark.parametrize("name", NAMES)
def test_pad_outlines_encode_the_rotation(name: str) -> None:
    """Outline vertices are absolute and include the rotation (counter-clockwise, Y up)."""
    for pad in load(name)[1]["pads"]:
        pts = pad["points_mm"]
        theta = pad["rotation_deg"]
        w, h = pad["width_mm"], pad["height_mm"]
        if pad["shape"] == "RECT":
            assert len(pts) == 4
            cx = sum(p[0] for p in pts) / 4
            cy = sum(p[1] for p in pts) / 4
            assert close(cx, pad["x_mm"], 1e-4)
            assert close(cy, pad["y_mm"], 1e-4)
            edges = [
                (pts[(i + 1) % 4][0] - pts[i][0], pts[(i + 1) % 4][1] - pts[i][1]) for i in range(4)
            ]
            lengths = sorted(math.hypot(*e) for e in edges)
            assert close(lengths[0], min(w, h), 1e-3)
            assert close(lengths[3], max(w, h), 1e-3)
            if not close(w, h, 1e-3):
                # The edge of length `width` runs along the pad x axis, rotated by theta.
                along_w = next(e for e in edges if close(math.hypot(*e), w, 1e-3))
                angle = math.degrees(math.atan2(along_w[1], along_w[0])) % 180
                diff = abs(angle - theta % 180)
                assert min(diff, 180 - diff) < 0.1
        elif pad["shape"] == "OVAL":
            assert len(pts) == 2
            (x1, y1), (x2, y2) = pts
            assert close((x1 + x2) / 2, pad["x_mm"], 1e-4)
            assert close((y1 + y2) / 2, pad["y_mm"], 1e-4)
            assert close(math.hypot(x2 - x1, y2 - y1), abs(w - h), 1e-3)
            if not close(w, h, 1e-3):
                local = 0.0 if w > h else 90.0
                angle = math.degrees(math.atan2(y2 - y1, x2 - x1)) % 180
                diff = abs(angle - (theta + local) % 180)
                assert min(diff, 180 - diff) < 0.1
        elif pad["shape"] == "POLYGON":
            assert len(pts) >= 3
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            assert close(max(xs) - min(xs), w, 1e-3)
            assert close(max(ys) - min(ys), h, 1e-3)
            assert close((max(xs) + min(xs)) / 2, pad["x_mm"], 1e-3)
            assert close((max(ys) + min(ys)) / 2, pad["y_mm"], 1e-3)
        else:
            assert pts == []
        if pad["slot_length_mm"]:
            (x1, y1), (x2, y2) = pad["slot_axis_mm"]
            assert close(
                math.hypot(x2 - x1, y2 - y1), pad["slot_length_mm"] - pad["drill_mm"], 1e-3
            )
            assert close((x1 + x2) / 2, pad["x_mm"], 1e-4)
            assert close((y1 + y2) / 2, pad["y_mm"], 1e-4)


@pytest.mark.parametrize("name", NAMES)
def test_pins_match_shape_strings(name: str) -> None:
    doc, exp = load(name)
    expected_pins = iter(exp["pins"])
    for unit_index, unit in enumerate(symbol_units(doc), start=1):
        ox, oy = unit["head"]["x"], unit["head"]["y"]
        for shape in unit["shape"]:
            if not shape.startswith("P~"):
                continue
            pin = next(expected_pins)
            sec = shape.split("^^")
            s0, s2, s3, s4 = (
                sec[0].split("~"),
                sec[2].split("~"),
                sec[3].split("~"),
                sec[4].split("~"),
            )
            rot = int(s0[6] or 0)
            assert pin["unit"] == unit_index
            assert pin["number"] == s4[4]
            assert pin["name"] == s3[4]
            assert pin["angle_deg"] == (rot + 180) % 360
            assert pin["direction"] == DIRECTION_BY_TYPE[s0[2]]
            assert close((float(s0[4]) - ox) * UNIT, pin["x_mm"])
            assert close(-(float(s0[5]) - oy) * UNIT, pin["y_mm"])
            assert pin["inverted"] == (sec[5].split("~")[0] == "1")
            assert pin["clock"] == (sec[6].split("~")[0] == "1")
            # The pin line runs from the connection point towards the body: its direction in
            # the Y up frame must equal the expected angle.
            m = PIN_PATH.fullmatch(s2[0])
            assert m, s2[0]
            x0, y0, axis, run = float(m[1]), float(m[2]), m[3], float(m[4])
            assert (x0, y0) == (float(s0[4]), float(s0[5]))
            # +y is down on screen, so a positive vertical run points down (270 in Y up).
            line_angle = {("h", True): 0, ("h", False): 180, ("v", True): 270, ("v", False): 90}[
                (axis, run > 0)
            ]
            assert pin["angle_deg"] == line_angle
            assert close(abs(run) * UNIT, pin["length_mm"])
    assert next(expected_pins, None) is None


@pytest.mark.parametrize("name", NAMES)
def test_arcs_match_svg_paths(name: str) -> None:
    doc, exp = load(name)
    fp = footprint(doc)
    ox, oy = fp["head"]["x"], fp["head"]["y"]
    arc_strings = [s.split("~") for s in fp["shape"] if s.startswith("ARC~")]
    arcs = exp["footprint_shapes"]["arcs"]
    assert len(arc_strings) == len(arcs)
    for f, arc in zip(arc_strings, arcs, strict=True):
        m = ARC_PATH.fullmatch(f[4])
        assert m, f[4]
        assert int(m[6]) == arc["large_arc"]
        assert int(m[7]) == arc["svg_sweep_flag"]
        sx, sy = (float(m[1]) - ox) * UNIT, -(float(m[2]) - oy) * UNIT
        ex, ey = (float(m[8]) - ox) * UNIT, -(float(m[9]) - oy) * UNIT
        assert close(sx, arc["start_mm"][0]) and close(sy, arc["start_mm"][1])
        assert close(ex, arc["end_mm"][0]) and close(ey, arc["end_mm"][1])
        _check_arc_geometry(arc, (sx, sy), (ex, ey))
    for unit_exp, unit in zip(exp["symbol_units"], symbol_units(doc), strict=True):
        uox, uoy = unit["head"]["x"], unit["head"]["y"]
        strings = [s.split("~") for s in unit["shape"] if s.startswith("A~")]
        assert len(strings) == len(unit_exp["arcs"])
        for f, arc in zip(strings, unit_exp["arcs"], strict=True):
            m = ARC_PATH.fullmatch(f[1])
            assert m, f[1]
            assert int(m[6]) == arc["large_arc"]
            assert int(m[7]) == arc["svg_sweep_flag"]
            sx, sy = (float(m[1]) - uox) * UNIT, -(float(m[2]) - uoy) * UNIT
            ex, ey = (float(m[8]) - uox) * UNIT, -(float(m[9]) - uoy) * UNIT
            _check_arc_geometry(arc, (sx, sy), (ex, ey))


def _check_arc_geometry(
    arc: dict[str, Any], start: tuple[float, float], end: tuple[float, float]
) -> None:
    """Centre, radius and signed sweep (counter-clockwise positive, Y up) from the end points."""
    cx, cy, r = arc["cx_mm"], arc["cy_mm"], arc["radius_mm"]
    assert close(math.hypot(start[0] - cx, start[1] - cy), r, arc_tol(arc))
    assert close(math.hypot(end[0] - cx, end[1] - cy), r, arc_tol(arc))
    a0 = math.degrees(math.atan2(start[1] - cy, start[0] - cx))
    a1 = math.degrees(math.atan2(end[1] - cy, end[0] - cx))
    sweep = arc["sweep_deg"]
    delta = (a1 - a0) % 360 if sweep > 0 else -((a0 - a1) % 360)
    assert abs(delta - sweep) < 1.0
    assert (
        abs(a0 % 360 - arc["start_deg"]) < 1.0 or abs(abs(a0 % 360 - arc["start_deg"]) - 360) < 1.0
    )
    assert (arc["svg_sweep_flag"] == 1) == (sweep < 0)
    assert (arc["large_arc"] == 1) == (abs(sweep) > 180)


def arc_tol(_arc: dict[str, Any]) -> float:
    return 1e-3


# ---------------------------------------------------------------------------
# Symbol origin and multi-unit rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", NAMES)
def test_symbol_bbox_is_centred_on_head_origin(name: str) -> None:
    doc, exp = load(name)
    for unit, unit_exp in zip(symbol_units(doc), exp["symbol_units"], strict=True):
        x, y = unit["head"]["x"], unit["head"]["y"]
        box = unit["BBox"]
        assert x % 5 == 0
        assert y % 5 == 0
        assert box["x"] + box["width"] / 2 == x
        assert box["y"] + box["height"] / 2 == y
        assert unit_exp["origin_ee"] == [x, y]
        for shape in unit["shape"]:
            if shape.startswith("P~"):
                s0 = shape.split("^^")[0].split("~")
                px, py = float(s0[4]), float(s0[5])
                assert box["x"] <= px <= box["x"] + box["width"]
                assert box["y"] <= py <= box["y"] + box["height"]


def test_multi_unit_layout() -> None:
    doc, exp = load("opamp2")
    res = doc["result"]
    assert res["dataStr"]["shape"] == []
    assert res["dataStr"]["BBox"] == {"x": 0, "y": 0, "width": 0, "height": 0}
    assert exp["unit_count"] == len(res["subparts"]) == 3
    assert all(sp["dataStr"]["head"]["docType"] == "6" for sp in res["subparts"])
    assert all(sp["dataStr"]["shape"] for sp in res["subparts"])
    first = res["subparts"][0]["dataStr"]["head"]
    assert (res["dataStr"]["head"]["x"], res["dataStr"]["head"]["y"]) != (first["x"], first["y"])
    assert [u["pin_count"] for u in exp["symbol_units"]] == [3, 3, 2]
    assert exp["pad_count"] == exp["unique_pad_numbers"] == len(exp["pins"]) == 8
    origins = [tuple(u["origin_ee"]) for u in exp["symbol_units"]]
    assert len(set(origins)) == 3


def test_duplicate_pad_numbers_fixture() -> None:
    exp = load("module_dup")[1]
    assert exp["pad_count"] > exp["unique_pad_numbers"]
    dup = [p for p in exp["pads"] if p["number"] == "25"]
    assert len(dup) == 4
    assert len([p for p in exp["pins"] if p["number"] == "25"]) == 1


def test_exposed_pad_shares_number_with_symbol_pin() -> None:
    exp = load("qfn16_ep")[1]
    ep = [p for p in exp["pads"] if p["number"] == "17"]
    assert len(ep) == 1
    assert ep[0]["shape"] == "RECT"
    assert max(ep[0]["width_mm"], ep[0]["height_mm"]) > 1.5
    assert any(p["number"] == "17" for p in exp["pins"])


# ---------------------------------------------------------------------------
# Public repo hygiene
# ---------------------------------------------------------------------------


def _real_files() -> list[Path]:
    return sorted(REAL_DIR.glob("*.json")) if REAL_DIR.exists() else []


@pytest.mark.skipif(not _real_files(), reason="no local real fixtures")
def test_nothing_copied_from_real_fixtures() -> None:
    real_ids: set[str] = set()
    real_shapes: set[str] = set()
    real_license_heads: set[str] = set()
    for path in _real_files():
        res = json.loads(path.read_text(encoding="utf-8"))["result"]
        pkg = res.get("packageDetail") or {}
        parts = [res, pkg, *res.get("subparts", [])]
        for part in parts:
            for key in ("uuid", "datastrid"):
                if part.get(key):
                    real_ids.add(part[key])
        datas = [res["dataStr"], *[sp["dataStr"] for sp in res.get("subparts", [])]]
        if pkg:
            datas.append(pkg["dataStr"])
        for data in datas:
            head = data["head"]
            for key in ("uuid", "puuid", "uuid_3d"):
                if head.get(key):
                    real_ids.add(head[key])
            if head.get("license"):
                real_license_heads.add(head["license"][:80])
            real_shapes.update(s for s in data["shape"] if not s.startswith("SVGNODE"))
    assert real_ids
    for name in NAMES:
        text = (FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8")
        for rid in real_ids:
            assert rid not in text, f"{name} contains a real id"
        for lic in real_license_heads:
            assert lic not in text
        doc = load(name)[0]
        shapes = {s for u in symbol_units(doc) for s in u["shape"]} | set(footprint(doc)["shape"])
        shared = shapes & real_shapes
        assert not shared, f"{name} copies real shape strings: {sorted(shared)[:2]}"
