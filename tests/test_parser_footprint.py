"""Tests for the EasyEDA footprint parser.

Inline tests use hand-written shape strings with the origin at (100, 200) EasyEDA units, so
10 units are exactly 2.54 mm and a point 10 units above the origin is +2.54 mm in Y.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import math
import random
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from lcsc2fusion.constants import (
    COURTYARD_WIDTH_MM,
    DOCU_WIDTH_MM,
    LABEL_SIZE_PACKAGE_MM,
    MIN_POLYGON_WIDTH_MM,
    Layer,
)
from lcsc2fusion.errors import ParseError
from lcsc2fusion.model import (
    Circle,
    Hole,
    Label,
    Package,
    Pad,
    PadShape,
    Polygon,
    TextAlign,
    Wire,
)
from lcsc2fusion.parser.footprint import parse_footprint

FIXTURES = Path(__file__).parent / "fixtures"
REAL_DIR = FIXTURES / "real"
SYNTHETIC_DIR = FIXTURES / "synthetic"
ORIGIN = (100.0, 200.0)


def detail(shapes: list[Any], title: str = "TEST-FP", **head: Any) -> dict[str, Any]:
    return {
        "title": title,
        "dataStr": {
            "head": {"x": ORIGIN[0], "y": ORIGIN[1], "c_para": {"package": title}, **head},
            "shape": shapes,
            "layers": [],
        },
    }


def parse(*shapes: str, **kwargs: Any) -> tuple[Package, list[str]]:
    return parse_footprint(detail(list(shapes), **kwargs))


def pad_str(
    shape: str,
    x: float,
    y: float,
    w: float,
    h: float,
    layer: int,
    number: str,
    *,
    hole: float = 0.0,
    points: str = "",
    rotation: float = 0.0,
    hole_length: float = 0.0,
    slot: str = "",
    plated: str = "Y",
) -> str:
    return (
        f"PAD~{shape}~{x}~{y}~{w}~{h}~{layer}~~{number}~{hole}~{points}~{rotation}~gge1~"
        f"{hole_length}~{slot}~{plated}~0~0~0.2~{x},{y}"
    )


def graphics(package: Package, kind: type) -> list[Any]:
    return [g for g in package.graphics if isinstance(g, kind)]


def wires_on(package: Package, layer: Layer) -> list[Wire]:
    return [w for w in graphics(package, Wire) if w.layer == layer]


def pad_named(package: Package, name: str) -> Pad:
    return next(p for p in package.pads if p.name == name)


# pads


def test_smd_rect_pad_position_size_and_y_flip() -> None:
    pkg, warnings = parse(pad_str("RECT", 110, 190, 20, 10, 1, "1"))
    assert warnings == []
    (pad,) = pkg.pads
    assert pad.name == "1"
    assert pad.number == "1"
    assert pad.shape is PadShape.RECT
    assert (pad.x, pad.y) == pytest.approx((2.54, 2.54))  # above the origin means +Y
    assert (pad.width, pad.height) == pytest.approx((5.08, 2.54))
    assert pad.rotation == 0.0
    assert pad.layer is Layer.TOP
    assert pad.is_smd
    assert pad.drill == 0.0
    assert pad.plated
    assert pad.outline == ()


def test_pad_below_and_left_of_origin() -> None:
    pkg, _ = parse(pad_str("RECT", 95, 210, 4, 4, 1, "1"))
    assert (pkg.pads[0].x, pkg.pads[0].y) == pytest.approx((-1.27, -2.54))


def test_package_name_and_description() -> None:
    pkg, _ = parse(pad_str("RECT", 100, 200, 10, 10, 1, "1"), title="R0603")
    assert pkg.name == "R0603"
    assert pkg.description == "R0603"


def test_package_name_falls_back_to_c_para_package() -> None:
    data = detail([pad_str("RECT", 100, 200, 10, 10, 1, "1")], title="")
    data["dataStr"]["head"]["c_para"] = {"package": "FALLBACK-PKG"}
    pkg, _ = parse_footprint(data)
    assert pkg.name == "FALLBACK-PKG"


def test_description_mentions_a_different_easyeda_package() -> None:
    data = detail([pad_str("RECT", 100, 200, 10, 10, 1, "1")], title="Nice name")
    data["dataStr"]["head"]["c_para"] = {"package": "RAW-PKG"}
    pkg, _ = parse_footprint(data)
    assert pkg.name == "Nice name"
    assert "RAW-PKG" in pkg.description


def test_through_hole_round_pad() -> None:
    pkg, _ = parse(pad_str("ELLIPSE", 100, 200, 8, 8, 11, "2", hole=2))
    (pad,) = pkg.pads
    assert pad.shape is PadShape.ROUND  # ELLIPSE becomes ROUND
    assert (pad.x, pad.y) == (0.0, 0.0)
    assert (pad.width, pad.height) == pytest.approx((2.032, 2.032))
    assert pad.drill == pytest.approx(1.016)  # diameter = 2 * radius
    assert not pad.is_smd
    assert pad.layer is Layer.TOP
    assert pad.plated
    assert pad.slot_length == 0.0


def test_negative_zero_is_normalised() -> None:
    pkg, _ = parse(pad_str("RECT", 110, 200, 10, 10, 1, "1"))
    assert math.copysign(1.0, pkg.pads[0].y) == 1.0


def test_non_plated_pad_with_drill_stays_a_pad() -> None:
    pkg, _ = parse(pad_str("ELLIPSE", 100, 200, 8, 8, 11, "1", hole=2, plated="N"))
    assert len(pkg.pads) == 1
    assert pkg.pads[0].plated is False
    assert pkg.holes == ()


def test_bottom_layer_smd_pad() -> None:
    pkg, _ = parse(pad_str("RECT", 100, 200, 10, 10, 2, "1"))
    assert pkg.pads[0].layer is Layer.BOTTOM
    assert pkg.pads[0].is_smd


def test_oval_pad() -> None:
    pkg, _ = parse(pad_str("OVAL", 100, 200, 20, 10, 1, "1"))
    assert pkg.pads[0].shape is PadShape.OVAL
    assert (pkg.pads[0].width, pkg.pads[0].height) == pytest.approx((5.08, 2.54))


@pytest.mark.parametrize(
    ("ee_rotation", "expected"),
    [(0, 0.0), (30, 30.0), (90, 90.0), (180, 180.0), (270, 270.0), (-90, 270.0), (450, 90.0)],
)
def test_rotation_is_counter_clockwise(ee_rotation: float, expected: float) -> None:
    pkg, _ = parse(pad_str("RECT", 100, 200, 20, 10, 1, "1", rotation=ee_rotation))
    assert pkg.pads[0].rotation == pytest.approx(expected)
    # width and height stay in the pad's own frame, they are not swapped
    assert (pkg.pads[0].width, pkg.pads[0].height) == pytest.approx((5.08, 2.54))


def test_slot_pad_vertical_from_outline() -> None:
    # hole radius 4 (drill 2.032 mm), slot length 12 units (3.048 mm), centre line x=100 vertical
    slot_pad = pad_str(
        "OVAL", 100, 200, 12, 20, 11, "1", hole=4, hole_length=12, slot="100 203 100 197"
    )
    (pad,) = parse(slot_pad)[0].pads
    assert pad.drill == pytest.approx(2.032)
    assert pad.slot_length == pytest.approx(3.048)
    assert pad.slot_angle == pytest.approx(90.0)


def test_slot_pad_angle_horizontal_and_diagonal() -> None:
    horizontal = pad_str(
        "OVAL", 100, 200, 20, 12, 11, "1", hole=4, hole_length=20, slot="94 200 106 200"
    )
    assert parse(horizontal)[0].pads[0].slot_angle == pytest.approx(0.0)
    # EasyEDA (+10, -10) is up and to the right: 45 degrees counter-clockwise with Y up
    diagonal = pad_str(
        "OVAL", 100, 200, 20, 12, 11, "1", hole=4, hole_length=20, slot="100 200 110 190"
    )
    assert parse(diagonal)[0].pads[0].slot_angle == pytest.approx(45.0)
    # the same axis written in the other direction gives the same undirected angle
    reverse = pad_str(
        "OVAL", 100, 200, 20, 12, 11, "1", hole=4, hole_length=20, slot="110 190 100 200"
    )
    assert parse(reverse)[0].pads[0].slot_angle == pytest.approx(45.0)
    # up and to the left is 135 degrees
    other = pad_str(
        "OVAL", 100, 200, 20, 12, 11, "1", hole=4, hole_length=20, slot="100 200 90 190"
    )
    assert parse(other)[0].pads[0].slot_angle == pytest.approx(135.0)


def test_slot_angle_without_outline_follows_longer_dimension() -> None:
    tall = pad_str("OVAL", 100, 200, 12, 20, 11, "1", hole=4, hole_length=12)
    wide = pad_str("OVAL", 100, 200, 20, 12, 11, "1", hole=4, hole_length=20)
    rotated = pad_str("OVAL", 100, 200, 20, 12, 11, "1", hole=4, hole_length=20, rotation=90)
    assert parse(tall)[0].pads[0].slot_angle == pytest.approx(90.0)
    assert parse(wide)[0].pads[0].slot_angle == pytest.approx(0.0)
    assert parse(rotated)[0].pads[0].slot_angle == pytest.approx(90.0)


def test_round_hole_is_not_a_slot() -> None:
    # hole_length equal to the hole diameter (2 * radius) or zero is a plain round hole
    for length in (0, 8):
        pkg, _ = parse(pad_str("ELLIPSE", 100, 200, 12, 12, 11, "1", hole=4, hole_length=length))
        assert pkg.pads[0].slot_length == 0.0
        assert pkg.pads[0].slot_angle == 0.0


def test_hole_length_without_a_drill_is_ignored() -> None:
    pkg, _ = parse(pad_str("RECT", 100, 200, 12, 12, 1, "1", hole_length=30))
    assert pkg.pads[0].slot_length == 0.0
    assert pkg.pads[0].is_smd


def test_polygon_pad_uses_absolute_outline() -> None:
    # L shaped outline, bbox x 100..120, y 200..215 -> centre (110, 207.5)
    outline = "100 200 120 200 120 205 105 205 105 215 100 215"
    pkg, warnings = parse(pad_str("POLYGON", 110, 207, 20, 15, 1, "5", points=outline, rotation=45))
    (pad,) = pkg.pads
    assert pad.shape is PadShape.POLYGON
    assert pad.rotation == 0.0  # the outline already contains the rotation
    assert len(pad.outline) == 6
    assert [(p.x, p.y) for p in pad.outline] == pytest.approx(
        [(0, 0), (5.08, 0), (5.08, -1.27), (1.27, -1.27), (1.27, -3.81), (0, -3.81)]
    )
    assert (pad.width, pad.height) == pytest.approx((5.08, 3.81))
    assert (pad.x, pad.y) == pytest.approx((2.54, -1.905))  # bbox centre
    assert any("polygon" in w.lower() for w in warnings)


def test_polygon_pad_drops_closing_duplicate_point() -> None:
    outline = "100 200 110 200 110 210 100 210 100 200"
    pkg, _ = parse(pad_str("POLYGON", 105, 205, 10, 10, 1, "1", points=outline))
    assert len(pkg.pads[0].outline) == 4


def test_polygon_pad_without_outline_falls_back_to_rect() -> None:
    pkg, warnings = parse(pad_str("POLYGON", 100, 200, 10, 10, 1, "1"))
    assert pkg.pads[0].shape is PadShape.RECT
    assert pkg.pads[0].outline == ()
    assert any("POLYGON" in w for w in warnings)


def test_unknown_pad_shape_becomes_rect_with_warning() -> None:
    pkg, warnings = parse(pad_str("TRIANGLE", 100, 200, 10, 10, 1, "1"))
    assert pkg.pads[0].shape is PadShape.RECT
    assert any("TRIANGLE" in w for w in warnings)


def test_duplicate_pad_numbers_get_unique_names() -> None:
    pkg, _ = parse(*(pad_str("RECT", 100 + 20 * i, 200, 10, 10, 1, "41") for i in range(3)))
    assert [p.name for p in pkg.pads] == ["41", "41_2", "41_3"]
    assert [p.number for p in pkg.pads] == ["41", "41", "41"]


def test_pad_names_are_unique_case_insensitively() -> None:
    pkg, _ = parse(
        pad_str("RECT", 100, 200, 10, 10, 1, "A1"),
        pad_str("RECT", 120, 200, 10, 10, 1, "a1"),
        pad_str("RECT", 140, 200, 10, 10, 1, "A(1)"),
    )
    assert [p.name for p in pkg.pads] == ["A1", "a1_2", "A1_3"]
    assert [p.number for p in pkg.pads] == ["A1", "a1", "A(1)"]  # raw text is kept


def test_generated_name_does_not_collide_with_real_number() -> None:
    pkg, _ = parse(
        pad_str("RECT", 100, 200, 10, 10, 1, "7"),
        pad_str("RECT", 120, 200, 10, 10, 1, "7_2"),
        pad_str("RECT", 140, 200, 10, 10, 1, "7"),
    )
    names = [p.name for p in pkg.pads]
    assert len({n.lower() for n in names}) == 3
    assert names[:2] == ["7", "7_2"]


def test_alphanumeric_pad_numbers() -> None:
    pkg, _ = parse(
        pad_str("RECT", 100, 200, 10, 10, 1, "A1B12"),
        pad_str("RECT", 120, 200, 10, 10, 1, "EP"),
    )
    assert [p.name for p in pkg.pads] == ["A1B12", "EP"]


def test_pad_names_have_no_whitespace_or_semicolon() -> None:
    pkg, _ = parse(pad_str("RECT", 100, 200, 10, 10, 1, "1 2;3"))
    assert pkg.pads[0].name == "123"
    assert pkg.pads[0].number == "1 2;3"


def test_empty_pad_numbers_get_generated_names_and_warning() -> None:
    pkg, warnings = parse(
        pad_str("RECT", 100, 200, 10, 10, 1, "1"),
        pad_str("RECT", 120, 200, 10, 10, 1, ""),
        pad_str("RECT", 140, 200, 10, 10, 1, ""),
    )
    assert [p.name for p in pkg.pads] == ["1", "P$2", "P$3"]
    assert sum("no number" in w for w in warnings) == 2


def test_pad_on_unsupported_layer_warns_and_stays_a_top_pad() -> None:
    pkg, warnings = parse(pad_str("RECT", 100, 200, 10, 10, 21, "1"))
    assert pkg.pads[0].layer is Layer.TOP
    assert any("21" in w for w in warnings)


def test_multi_layer_pad_without_hole_warns() -> None:
    pkg, warnings = parse(pad_str("RECT", 100, 200, 10, 10, 11, "1"))
    assert pkg.pads[0].is_smd
    assert any("multi layer" in w for w in warnings)


# drawing shapes


def test_track_becomes_wires_on_mapped_layer() -> None:
    pkg, _ = parse(
        pad_str("RECT", 100, 200, 10, 10, 1, "1"),
        "TRACK~1~3~~100 200 110 200 110 190~gge5~0",
    )
    silk = wires_on(pkg, Layer.T_PLACE)
    assert len(silk) == 2
    assert (silk[0].x1, silk[0].y1, silk[0].x2, silk[0].y2) == pytest.approx((0, 0, 2.54, 0))
    assert (silk[1].x1, silk[1].y1, silk[1].x2, silk[1].y2) == pytest.approx((2.54, 0, 2.54, 2.54))
    assert all(w.width == pytest.approx(0.254) and w.curve == 0.0 for w in silk)


def test_zero_width_track_gets_minimum_width() -> None:
    pkg, _ = parse(pad_str("RECT", 100, 200, 10, 10, 1, "1"), "TRACK~0~12~~100 200 110 200~gge5~0")
    (wire,) = wires_on(pkg, Layer.T_DOCU)
    assert wire.width == DOCU_WIDTH_MM


def test_track_layers_map_through_the_table() -> None:
    pkg, _ = parse(
        pad_str("RECT", 100, 200, 10, 10, 1, "1"),
        "TRACK~1~13~~100 200 110 200~a~0",
        "TRACK~1~14~~100 210 110 210~b~0",
        "TRACK~1~4~~100 220 110 220~c~0",
        "TRACK~1~10~~100 230 110 230~d~0",
    )
    assert len(wires_on(pkg, Layer.T_DOCU)) == 1
    assert len(wires_on(pkg, Layer.B_DOCU)) == 1
    assert len(wires_on(pkg, Layer.B_PLACE)) == 1
    assert len(wires_on(pkg, Layer.DIMENSION)) == 1


def test_zero_length_track_segments_are_dropped() -> None:
    pkg, _ = parse(
        pad_str("RECT", 100, 200, 10, 10, 1, "1"), "TRACK~1~3~~100 200 100 200 110 200~a~0"
    )
    assert len(wires_on(pkg, Layer.T_PLACE)) == 1


def test_arc_becomes_curved_wire() -> None:
    # from the bottom of the centre (110, 190) clockwise on screen to its left side: -90 degrees
    pkg, _ = parse(
        pad_str("RECT", 100, 200, 10, 10, 1, "1"), "ARC~1~3~~M 110 200 A 10 10 0 0 1 100 190~~g~0"
    )
    (arc,) = wires_on(pkg, Layer.T_PLACE)
    assert (arc.x1, arc.y1, arc.x2, arc.y2) == pytest.approx((2.54, 0.0, 0.0, 2.54))
    assert arc.curve == pytest.approx(-90.0)
    assert arc.width == pytest.approx(0.254)


def test_counter_clockwise_arc_has_positive_curve() -> None:
    pkg, _ = parse(
        pad_str("RECT", 100, 200, 10, 10, 1, "1"), "ARC~1~3~~M 100 190 A 10 10 0 0 0 110 200~~g~0"
    )
    (arc,) = wires_on(pkg, Layer.T_PLACE)
    assert arc.curve == pytest.approx(90.0)


def test_degenerate_arcs_are_dropped() -> None:
    pkg, warnings = parse(
        pad_str("RECT", 100, 200, 10, 10, 1, "1"),
        "ARC~1~3~~M 110 200 A 0 0 0 0 1 100 190~~g~0",
        "ARC~1~3~~M 110 200 A 5 5 0 0 1 110 200~~h~0",
    )
    assert wires_on(pkg, Layer.T_PLACE) == []
    assert warnings == []


def test_circle_ring_and_disc() -> None:
    pkg, _ = parse(
        pad_str("RECT", 100, 200, 10, 10, 1, "1"),
        "CIRCLE~110~190~5~2~3~a~0",
        "CIRCLE~100~200~5~0~12~b~0",
    )
    ring, disc = graphics(pkg, Circle)
    assert (ring.x, ring.y, ring.radius, ring.width) == pytest.approx((2.54, 2.54, 1.27, 0.508))
    assert ring.layer is Layer.T_PLACE
    assert (disc.x, disc.y, disc.width) == (0.0, 0.0, 0.0)
    assert disc.layer is Layer.T_DOCU


def test_rect_becomes_four_wires_from_top_left_corner() -> None:
    pkg, _ = parse(pad_str("RECT", 100, 200, 10, 10, 1, "1"), "RECT~100~190~20~10~12~gge8~0~1~none")
    wires = wires_on(pkg, Layer.T_DOCU)
    # top-left (0, 2.54), 5.08 wide and 2.54 high, so the bottom edge lies on y = 0
    want = [
        (0.0, 2.54, 5.08, 2.54),
        (5.08, 2.54, 5.08, 0.0),
        (5.08, 0.0, 0.0, 0.0),
        (0.0, 0.0, 0.0, 2.54),
    ]
    assert len(wires) == 4
    for edge in want:
        assert any((w.x1, w.y1, w.x2, w.y2) == pytest.approx(edge, abs=1e-6) for w in wires)
    assert all(w.width == pytest.approx(0.254) for w in wires)


def test_hole_shape_becomes_non_plated_hole() -> None:
    pkg, _ = parse(pad_str("RECT", 100, 200, 10, 10, 1, "1"), "HOLE~110~190~3~gge9~0")
    assert len(pkg.pads) == 1
    (hole,) = pkg.holes
    assert isinstance(hole, Hole)
    assert (hole.x, hole.y) == pytest.approx((2.54, 2.54))
    assert hole.drill == pytest.approx(1.524)  # radius 3 units, diameter 6 units


def test_footprint_with_only_holes_is_valid() -> None:
    pkg, _ = parse("HOLE~110~190~3~gge9~0")
    assert pkg.pads == ()
    assert len(pkg.holes) == 1
    assert len(wires_on(pkg, Layer.T_KEEPOUT)) == 4


def test_solid_region_polygon() -> None:
    pkg, _ = parse(
        pad_str("RECT", 100, 200, 10, 10, 1, "1"),
        "SOLIDREGION~3~~M 100 200 L 110 200 L 110 190 Z~solid~gge10~~~~0",
    )
    (poly,) = graphics(pkg, Polygon)
    assert poly.layer is Layer.T_PLACE
    assert poly.width == MIN_POLYGON_WIDTH_MM
    assert [(p.x, p.y) for p in poly.points] == pytest.approx([(0, 0), (2.54, 0), (2.54, 2.54)])


def test_solid_region_with_arcs_is_flattened() -> None:
    pkg, _ = parse(
        pad_str("RECT", 100, 200, 10, 10, 1, "1"),
        "SOLIDREGION~12~~M 100 200 A 10 10 0 1 1 120 200 A 10 10 0 1 1 100 200 Z~solid~g~~~~0",
    )
    (poly,) = graphics(pkg, Polygon)
    assert len(poly.points) > 8
    radii = {round(math.hypot(p.x - 2.54, p.y), 3) for p in poly.points}
    assert radii == {2.54}


def test_cutout_and_npth_regions_are_ignored_with_a_warning() -> None:
    pkg, warnings = parse(
        pad_str("RECT", 100, 200, 10, 10, 1, "1"),
        "SOLIDREGION~12~~M 100 200 L 110 200 L 110 190 Z~cutout~g~~~~0",
        "SOLIDREGION~12~~M 100 200 L 110 200 L 110 190 Z~cutout~h~~~~0",
        "SOLIDREGION~12~~M 100 200 L 110 200 L 110 190 Z~npth~i~~~~0",
    )
    assert graphics(pkg, Polygon) == []
    assert any("cutout" in w and "2" in w for w in warnings)
    assert any("npth" in w for w in warnings)


def test_mask_and_paste_layers_are_skipped_silently() -> None:
    pkg, warnings = parse(
        pad_str("RECT", 100, 200, 10, 10, 1, "1"),
        *(f"TRACK~1~{layer}~~100 200 110 200~g~0" for layer in (5, 6, 7, 8)),
        "SOLIDREGION~5~~M 100 200 L 110 200 L 110 190 Z~solid~g~~~~0",
        "CIRCLE~100~200~5~1~7~g~0",
    )
    assert warnings == []
    assert [type(g) for g in pkg.graphics].count(Polygon) == 0
    assert graphics(pkg, Circle) == []
    assert len(pkg.graphics) == 6  # only courtyard and labels


def test_unknown_layer_warns_once_per_layer() -> None:
    pkg, warnings = parse(
        pad_str("RECT", 100, 200, 10, 10, 1, "1"),
        "TRACK~1~21~~100 200 110 200~a~0",
        "TRACK~1~21~~100 210 110 210~b~0",
        "TRACK~1~9~~100 220 110 220~c~0",
        "CIRCLE~100~200~5~1~40~g~0",
    )
    assert sum("layer 21" in w for w in warnings) == 1
    assert sum("layer 9" in w for w in warnings) == 1
    assert sum("layer 40" in w for w in warnings) == 1
    assert graphics(pkg, Circle) == []


def test_ignored_shape_types() -> None:
    pkg, warnings = parse(
        pad_str("RECT", 100, 200, 10, 10, 1, "1"),
        'SVGNODE~{"gId":"x"}',
        "TEXT~L~100~200~0.2~0~0~3~~6~hello~M 1 1~none~gge1~0",
        "VIA~100~200~5~~2~g~0",
        "COPPERAREA~1~~100 200 110 200 110 190~solid~g",
        "FANCY~1~2~3",
    )
    assert any("VIA" in w for w in warnings)
    assert any("COPPERAREA" in w for w in warnings)
    assert any("FANCY" in w for w in warnings)
    assert not any("SVGNODE" in w or "TEXT" in w for w in warnings)
    assert len(pkg.graphics) == 6  # nothing but courtyard and labels


def test_broken_graphic_becomes_a_warning_not_an_error() -> None:
    pkg, warnings = parse(
        pad_str("RECT", 100, 200, 10, 10, 1, "1"),
        "TRACK~1~3~~1 2 3~g~0",
        "ARC~1~3~~M 1 2 A 1~~g~0",
        "CIRCLE~x~y~z~1~3~g~0",
        "HOLE~1~2~0~g~0",
    )
    assert len(pkg.pads) == 1
    assert sum(w.startswith("Skipped") for w in warnings) == 4


# courtyard and labels


def courtyard(package: Package) -> tuple[float, float, float, float]:
    wires = wires_on(package, Layer.T_KEEPOUT)
    assert len(wires) == 4
    assert all(w.width == COURTYARD_WIDTH_MM and w.curve == 0.0 for w in wires)
    xs = [v for w in wires for v in (w.x1, w.x2)]
    ys = [v for w in wires for v in (w.y1, w.y2)]
    return min(xs), min(ys), max(xs), max(ys)


def test_courtyard_is_snapped_outward_to_005_mm() -> None:
    # pad 2.54 x 2.54 mm: half size 1.27 + margin 0.25 = 1.52, snapped outward to 1.55
    pkg, _ = parse(pad_str("RECT", 100, 200, 10, 10, 1, "1"))
    assert courtyard(pkg) == pytest.approx((-1.55, -1.55, 1.55, 1.55))


def test_courtyard_on_grid_does_not_grow() -> None:
    # pad exactly 2.5 mm wide: 1.25 + 0.25 = 1.5 is already on the grid
    side = 2.5 / 0.254
    pkg, _ = parse(pad_str("RECT", 100, 200, round(side, 5), round(side, 5), 1, "1"))
    assert courtyard(pkg) == pytest.approx((-1.5, -1.5, 1.5, 1.5))


def test_courtyard_covers_all_pads() -> None:
    pkg, _ = parse(
        pad_str("RECT", 110, 190, 20, 10, 1, "1"),  # x 0..5.08, y 1.27..3.81
        pad_str("RECT", 100, 200, 10, 10, 1, "2"),  # +-1.27
    )
    # x: -1.27-0.25 = -1.52 -> -1.55 and 5.08+0.25 = 5.33 -> 5.35; y: -1.55 and 3.81+0.25 -> 4.1
    assert courtyard(pkg) == pytest.approx((-1.55, -1.55, 5.35, 4.1))


def test_courtyard_accounts_for_pad_rotation() -> None:
    pkg, _ = parse(pad_str("RECT", 100, 200, 20, 10, 1, "1", rotation=90))
    # unrotated 5.08 x 2.54, rotated 90 gives 2.54 x 5.08: half sizes 1.27 and 2.54
    assert courtyard(pkg) == pytest.approx((-1.55, -2.8, 1.55, 2.8))


def test_courtyard_includes_holes_and_polygon_outlines() -> None:
    pkg, _ = parse(
        pad_str("RECT", 100, 200, 10, 10, 1, "1"),
        "HOLE~200~200~5~g~0",  # centre x = 25.4, drill 2.54 -> right edge 26.67
    )
    assert courtyard(pkg)[2] == pytest.approx(26.95)
    outline = "100 200 120 200 120 210 100 210"  # right edge x = 5.08
    pkg2, _ = parse(pad_str("POLYGON", 110, 205, 20, 10, 1, "1", points=outline))
    # outline x 0..5.08, y -2.54..0 plus 0.25, snapped outward
    assert courtyard(pkg2) == pytest.approx((-0.25, -2.8, 5.35, 0.25))


def test_courtyard_includes_drill_of_through_hole_pad() -> None:
    # pad is small but the drill (radius 6 units = 3.048 mm diameter) is larger than the copper
    pkg, _ = parse(pad_str("ELLIPSE", 100, 200, 4, 4, 11, "1", hole=6))
    assert courtyard(pkg)[2] == pytest.approx(1.8)  # 1.524 + 0.25 = 1.774 -> 1.8


def test_name_and_value_labels() -> None:
    pkg, _ = parse(pad_str("RECT", 110, 190, 20, 10, 1, "1"))
    x1, y1, x2, y2 = courtyard(pkg)
    labels = graphics(pkg, Label)
    assert len(labels) == 2
    name = next(label for label in labels if label.text == ">NAME")
    value = next(label for label in labels if label.text == ">VALUE")
    assert name.layer is Layer.T_NAMES
    assert name.size == LABEL_SIZE_PACKAGE_MM
    assert name.align is TextAlign.BOTTOM_CENTER
    assert (name.x, name.y) == pytest.approx(((x1 + x2) / 2, y2))
    assert value.layer is Layer.T_VALUES
    assert value.size == LABEL_SIZE_PACKAGE_MM
    assert value.align is TextAlign.TOP_CENTER
    assert (value.x, value.y) == pytest.approx(((x1 + x2) / 2, y1))


# error handling


def test_missing_pads_is_an_error() -> None:
    with pytest.raises(ParseError, match="no pads"):
        parse("TRACK~1~3~~100 200 110 200~g~0")


def test_empty_shape_list_is_an_error() -> None:
    with pytest.raises(ParseError):
        parse()


@pytest.mark.parametrize(
    "broken",
    [
        None,
        [],
        "text",
        {},
        {"title": "X"},
        {"title": "X", "dataStr": None},
        {"title": "X", "dataStr": []},
        {"title": "X", "dataStr": {}},
        {"title": "X", "dataStr": {"head": {}}},
        {"title": "X", "dataStr": {"head": {"x": 1, "y": 2}, "shape": None}},
        {"title": "X", "dataStr": {"head": {"x": 1, "y": 2}, "shape": "PAD~RECT"}},
        {"title": "X", "dataStr": {"head": [], "shape": []}},
        {"title": "X", "dataStr": "not json"},
        {"title": "X", "dataStr": '{"head": {"x": 1, "y": 2}, "shape": 5}'},
    ],
)
def test_malformed_container_raises_parse_error(broken: Any) -> None:
    with pytest.raises(ParseError):
        parse_footprint(broken)


def test_json_string_data_str_is_accepted() -> None:
    inner = detail([pad_str("RECT", 100, 200, 10, 10, 1, "1")])["dataStr"]
    pkg, _ = parse_footprint({"title": "X", "dataStr": json.dumps(inner)})
    assert len(pkg.pads) == 1


def test_missing_title_and_package_is_an_error() -> None:
    data = detail([pad_str("RECT", 100, 200, 10, 10, 1, "1")], title="")
    data["dataStr"]["head"]["c_para"] = {}
    with pytest.raises(ParseError, match="title"):
        parse_footprint(data)


def test_missing_origin_warns_and_assumes_zero() -> None:
    data = detail([pad_str("RECT", 10, 20, 10, 10, 1, "1")])
    del data["dataStr"]["head"]["x"]
    del data["dataStr"]["head"]["y"]
    pkg, warnings = parse_footprint(data)
    assert any("origin" in w for w in warnings)
    assert (pkg.pads[0].x, pkg.pads[0].y) == pytest.approx((2.54, -5.08))


def test_origin_may_be_given_as_strings() -> None:
    data = detail([pad_str("RECT", 110, 190, 10, 10, 1, "1")])
    data["dataStr"]["head"]["x"] = "100"
    data["dataStr"]["head"]["y"] = "200.0"
    assert parse_footprint(data)[0].pads[0].x == pytest.approx(2.54)


@pytest.mark.parametrize(
    "shape",
    [
        "PAD",
        "PAD~RECT",
        "PAD~RECT~1~2~3",
        "PAD~RECT~a~b~c~d~1~~1",
        "PAD~RECT~100~200~0~0~1~~1",  # zero size
        "PAD~RECT~100~200~-5~5~1~~1",  # negative size
        "PAD~RECT~100~200~5~5~1~~1~-2",  # negative hole radius
        "PAD~RECT~nan~200~5~5~1~~1",
        "PAD~RECT~100~200~inf~5~1~~1",
        "PAD~RECT~1e308~200~5~5~1~~1",
        "PAD~RECT~100~200~5~5~1~~1~1e300",
        "PAD~RECT~100~200~5~5~x~~1",
        "PAD~POLYGON~100~200~5~5~1~~1~0~1 2 3",  # odd number of outline coordinates
        "PAD~RECT~100~200~5~5~1~~1~0~~rot",
        "",
        "~",
        12,
    ],
)
def test_malformed_pads_raise_parse_error(shape: Any) -> None:
    pad_ok = pad_str("RECT", 120, 200, 10, 10, 1, "9")
    try:
        pkg, _ = parse_footprint(detail([shape, pad_ok]))
    except ParseError:
        return
    # blank shapes are skipped, everything else must have raised
    assert shape == ""
    assert len(pkg.pads) == 1


@pytest.mark.parametrize("length", range(0, 60, 3))
def test_truncated_pad_strings_never_leak_other_exceptions(length: int) -> None:
    full = pad_str("OVAL", 100, 200, 12, 20, 11, "1", hole=4, hole_length=12, slot="1 2 3 4")
    other = pad_str("RECT", 120, 200, 10, 10, 1, "2")
    with contextlib.suppress(ParseError):
        parse_footprint(detail([full[:length], other]))


@pytest.mark.parametrize("tag", ["TRACK", "ARC", "CIRCLE", "RECT", "HOLE", "SOLIDREGION"])
@pytest.mark.parametrize("length", range(0, 24, 2))
def test_truncated_graphics_never_leak_other_exceptions(tag: str, length: int) -> None:
    samples = {
        "TRACK": "TRACK~1~3~~100 200 110 200 110 190~gge5~0",
        "ARC": "ARC~1~3~~M 110 200 A 10 10 0 0 1 100 190~~g~0",
        "CIRCLE": "CIRCLE~110~190~5~2~3~a~0",
        "RECT": "RECT~100~190~20~10~12~gge8~0~1~none",
        "HOLE": "HOLE~110~190~3~gge9~0",
        "SOLIDREGION": "SOLIDREGION~3~~M 100 200 L 110 200 L 110 190 Z~solid~g~~~~0",
    }
    shape = samples[tag][:length]
    pkg, _ = parse_footprint(detail([pad_str("RECT", 100, 200, 10, 10, 1, "1"), shape]))
    assert len(pkg.pads) == 1


def test_random_mutations_only_raise_parse_error() -> None:
    """Seeded mutation fuzz over a hand-written footprint: no foreign exception may escape."""
    base = [
        pad_str("RECT", 110, 190, 20, 10, 1, "1", rotation=90),
        pad_str("OVAL", 130, 190, 12, 20, 11, "2", hole=4, hole_length=12, slot="130 195 130 185"),
        pad_str("POLYGON", 150, 190, 10, 10, 1, "3", points="145 185 155 185 155 195 145 195"),
        "TRACK~1~3~~100 200 110 200 110 190~gge5~0",
        "ARC~1~3~~M 110 200 A 10 10 0 0 1 100 190~~g~0",
        "CIRCLE~110~190~5~2~3~a~0",
        "RECT~100~190~20~10~12~gge8~0~1~none",
        "HOLE~110~190~3~gge9~0",
        "SOLIDREGION~3~~M 100 200 L 110 200 L 110 190 Z~solid~gge10~~~~0",
    ]
    junk = ["", "x", "nan", "inf", "-1", "0", "1e308", "M", "A 1 2", "~", "1,2,3", "--5"]
    rng = random.Random(20240601)
    for _ in range(600):
        shapes = list(base)
        for _ in range(rng.randint(1, 3)):
            i = rng.randrange(len(shapes))
            fields = shapes[i].split("~")
            mode = rng.random()
            if mode < 0.5:
                fields[rng.randrange(len(fields))] = rng.choice(junk)
                shapes[i] = "~".join(fields)
            elif mode < 0.8:
                shapes[i] = shapes[i][: rng.randrange(len(shapes[i]) + 1)]
            else:
                del fields[rng.randrange(len(fields))]
                shapes[i] = "~".join(fields)
        with contextlib.suppress(ParseError):
            pkg, _ = parse_footprint(detail(shapes))
            assert all(math.isfinite(v) for v in all_numbers(pkg))


# result invariants


def all_numbers(package: Package) -> Iterable[float]:
    for pad in package.pads:
        yield from (pad.x, pad.y, pad.width, pad.height, pad.rotation, pad.drill)
        yield from (pad.slot_length, pad.slot_angle)
        for point in pad.outline:
            yield from (point.x, point.y)
    for hole in package.holes:
        yield from (hole.x, hole.y, hole.drill)
    for g in package.graphics:
        if isinstance(g, Wire):
            yield from (g.x1, g.y1, g.x2, g.y2, g.width, g.curve)
        elif isinstance(g, Circle):
            yield from (g.x, g.y, g.radius, g.width)
        elif isinstance(g, Polygon):
            for point in g.points:
                yield from (point.x, point.y)
            yield g.width
        elif isinstance(g, Label):
            yield from (g.x, g.y, g.size)


def pad_extent(pad: Pad) -> list[tuple[float, float]]:
    """Corner points of a conservative (rectangular) extent of a pad, for containment checks."""
    if pad.outline:
        return [(p.x, p.y) for p in pad.outline]
    rad = math.radians(pad.rotation)
    corners = []
    for sx in (-1, 1):
        for sy in (-1, 1):
            lx, ly = sx * pad.width / 2, sy * pad.height / 2
            corners.append(
                (
                    pad.x + lx * math.cos(rad) - ly * math.sin(rad),
                    pad.y + lx * math.sin(rad) + ly * math.cos(rad),
                )
            )
    return corners


def load_real(name: str) -> dict[str, Any]:
    path = REAL_DIR / f"{name}.json"
    if not path.exists():
        pytest.skip(f"real fixture {name} not available")
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    detail_obj: dict[str, Any] = data["result"]["packageDetail"]
    return detail_obj


def real_ids() -> list[str]:
    return sorted(p.stem for p in REAL_DIR.glob("*.json")) if REAL_DIR.is_dir() else []


@pytest.mark.real
@pytest.mark.parametrize("part", real_ids() or ["none"])
def test_real_footprint_invariants(part: str) -> None:
    if part == "none":
        pytest.skip("no real fixtures in tests/fixtures/real")
    raw = load_real(part)
    pkg, _ = parse_footprint(raw)
    source_pads = [s for s in raw["dataStr"]["shape"] if s.startswith("PAD~")]
    assert len(pkg.pads) == len(source_pads)
    assert len({p.name.lower() for p in pkg.pads}) == len(pkg.pads)
    assert all(math.isfinite(v) for v in all_numbers(pkg))
    assert all(p.name and not any(c.isspace() or c == ";" for c in p.name) for p in pkg.pads)
    x1, y1, x2, y2 = courtyard(pkg)
    for pad in pkg.pads:
        for cx, cy in pad_extent(pad):
            assert x1 <= cx <= x2 and y1 <= cy <= y2, f"pad {pad.name} outside courtyard"
    assert pkg.name == raw["title"]
    assert [g.text for g in graphics(pkg, Label)] == [">NAME", ">VALUE"]


@pytest.mark.real
def test_real_c2040_qfn_with_thermal_pad() -> None:
    pkg, _ = parse_footprint(load_real("C2040"))
    assert len(pkg.pads) == 57
    thermal = next(p for p in pkg.pads if p.number == "57")
    assert thermal.shape is PadShape.RECT
    assert (thermal.x, thermal.y) == pytest.approx((0.0, 0.0), abs=1e-3)
    assert (thermal.width, thermal.height) == pytest.approx((3.1, 3.1), abs=0.01)
    # 0.4 mm pitch along the left edge
    left = sorted(
        (p for p in pkg.pads if p.x < -2.5 and p.rotation in (0.0, 180.0)), key=lambda p: p.y
    )
    assert len(left) == 14
    pitches = {round(b.y - a.y, 2) for a, b in itertools.pairwise(left)}
    assert pitches == {0.4}


@pytest.mark.real
def test_real_c165948_usb_c_polygon_slot_and_hole() -> None:
    pkg, warnings = parse_footprint(load_real("C165948"))
    polygons = [p for p in pkg.pads if p.shape is PadShape.POLYGON]
    assert len(polygons) == 4
    assert all(len(p.outline) >= 4 and p.rotation == 0.0 for p in polygons)
    assert any("polygon" in w.lower() for w in warnings)
    slots = [p for p in pkg.pads if p.slot_length > 0.0]
    assert len(slots) == 4
    for slot in slots:
        assert slot.drill == pytest.approx(0.8, abs=0.01)
        assert slot.slot_length > slot.drill
        assert slot.slot_angle == pytest.approx(90.0)
        assert not slot.is_smd
    assert len(pkg.holes) == 2
    assert all(h.drill == pytest.approx(0.6, abs=0.01) for h in pkg.holes)
    assert "A1B12" in {p.number for p in pkg.pads}
    assert len(pkg.pads) == 16


@pytest.mark.real
def test_real_c2913202_esp32_shared_pad_number() -> None:
    pkg, _ = parse_footprint(load_real("C2913202"))
    assert len(pkg.pads) == 49
    shared = [p for p in pkg.pads if p.number == "41"]
    assert len(shared) == 9
    assert [p.name for p in shared] == ["41"] + [f"41_{i}" for i in range(2, 10)]
    assert len({p.name for p in pkg.pads}) == 49


@pytest.mark.real
def test_real_c14663_capacitor_has_four_quarter_arcs() -> None:
    pkg, _ = parse_footprint(load_real("C14663"))
    arcs = [g for g in graphics(pkg, Wire) if g.curve != 0.0]
    assert len(arcs) == 4
    assert all(a.layer is Layer.T_PLACE for a in arcs)
    assert all(abs(abs(a.curve) - 90.0) < 0.1 for a in arcs)
    a, b = pkg.pads
    assert a.x == pytest.approx(-b.x, abs=0.01)
    assert a.y == pytest.approx(0.0, abs=0.01)


@pytest.mark.real
def test_real_c46749_dip_through_hole_pads() -> None:
    pkg, _ = parse_footprint(load_real("C46749"))
    assert len(pkg.pads) == 8
    assert all(not p.is_smd and p.plated and p.layer is Layer.TOP for p in pkg.pads)
    assert pkg.pads[0].shape is PadShape.RECT  # pin 1 is square
    assert all(p.shape is PadShape.ROUND for p in pkg.pads[1:])
    assert all(p.drill == pytest.approx(0.9, abs=0.01) for p in pkg.pads)


@pytest.mark.real
def test_real_c7943_has_rect_outline_wires() -> None:
    pkg, _ = parse_footprint(load_real("C7943"))
    assert len(wires_on(pkg, Layer.T_PLACE)) >= 4


# synthetic fixtures with expected pad tables


def synthetic_cases() -> list[Path]:
    if not SYNTHETIC_DIR.is_dir():
        return []
    return sorted(
        p
        for p in SYNTHETIC_DIR.glob("*.json")
        if not p.name.endswith(".expected.json") and p.with_suffix(".expected.json").exists()
    )


def find_package_detail(data: dict[str, Any]) -> dict[str, Any]:
    result = data.get("result", data)
    detail_obj = result.get("packageDetail", result)
    if not isinstance(detail_obj, dict):
        raise AssertionError("synthetic fixture has no packageDetail")
    return detail_obj


def angle_diff(a: float, b: float) -> float:
    return (a - b + 180.0) % 360.0 - 180.0


def line_angle(points: list[Any]) -> float:
    """Direction (mod 180) of a two point line given flat or as [[x1, y1], [x2, y2]]."""
    flat = [float(v) for pt in points for v in (pt if isinstance(pt, list) else [pt])]
    return math.degrees(math.atan2(flat[3] - flat[1], flat[2] - flat[0])) % 180.0


@pytest.mark.parametrize("path", synthetic_cases() or [None], ids=lambda p: p.stem if p else "none")
def test_synthetic_fixture_matches_expected_pads(path: Path | None) -> None:
    """Compare against the sidecar written by tests/fixtures/synthetic/generate.py."""
    if path is None:
        pytest.skip("no synthetic fixtures with .expected.json yet")
    data = json.loads(path.read_text(encoding="utf-8"))
    expected = json.loads(path.with_suffix(".expected.json").read_text(encoding="utf-8"))
    pkg, _ = parse_footprint(find_package_detail(data))
    want_pads = expected["pads"]
    assert len(pkg.pads) == len(want_pads) == expected.get("pad_count", len(want_pads))
    shapes = {
        "RECT": PadShape.RECT,
        "OVAL": PadShape.OVAL,
        "ELLIPSE": PadShape.ROUND,
        "POLYGON": PadShape.POLYGON,
    }
    for pad, want in zip(pkg.pads, want_pads, strict=True):
        where = f"{path.stem} pad {want['number']}"
        assert pad.number == str(want["number"]), where
        assert pad.shape is shapes[want["shape"]], where
        assert pad.x == pytest.approx(want["x_mm"], abs=1e-3), where
        assert pad.y == pytest.approx(want["y_mm"], abs=1e-3), where
        assert pad.width == pytest.approx(want["width_mm"], abs=1e-3), where
        assert pad.height == pytest.approx(want["height_mm"], abs=1e-3), where
        assert pad.drill == pytest.approx(want.get("drill_mm", 0.0), abs=1e-3), where
        assert pad.plated is want.get("plated", True), where
        layer = {"top": Layer.TOP, "bottom": Layer.BOTTOM, "thru": Layer.TOP}[want["layer"]]
        assert pad.layer is layer, where
        if pad.shape is PadShape.POLYGON:
            assert pad.rotation == 0.0, where
            got = [(q.x, q.y) for q in pad.outline]
            raw = want["points_mm"]
            pairs = (
                [tuple(pt) for pt in raw]
                if raw and isinstance(raw[0], list)
                else [tuple(raw[i : i + 2]) for i in range(0, len(raw), 2)]
            )
            # The parser merges consecutive duplicate vertices and drops a closing duplicate.
            exp = [pt for i, pt in enumerate(pairs) if i == 0 or pt != pairs[i - 1]]
            assert len(got) in (len(exp), len(exp) - 1), where
            flat_got = [v for pt in got for v in pt]
            flat_exp = [v for pt in exp[: len(got)] for v in pt]
            assert flat_got == pytest.approx(flat_exp, abs=1e-3), where
        else:
            rotation = want.get("rotation_deg", want.get("rotation", 0.0))
            assert abs(angle_diff(pad.rotation, rotation)) < 1e-3, where
        slot = want.get("slot_length_mm", 0.0)
        if pad.drill > 0.0 and slot > pad.drill + 1e-3:
            assert pad.slot_length == pytest.approx(slot, abs=1e-3), where
            if want.get("slot_axis_mm"):
                diff = angle_diff(pad.slot_angle, line_angle(want["slot_axis_mm"]))
                assert min(abs(diff), 180.0 - abs(diff)) < 1e-2, where
        else:
            assert pad.slot_length == 0.0, where
    assert [p.number for p in pkg.pads if p.number] == [str(w["number"]) for w in want_pads]
    for hole, want_hole in zip(pkg.holes, expected.get("holes", []), strict=True):
        assert (hole.x, hole.y, hole.drill) == pytest.approx(
            (want_hole["x_mm"], want_hole["y_mm"], want_hole["drill_mm"]), abs=1e-3
        )
    assert len(pkg.holes) == len(expected.get("holes", []))
    if expected.get("package_name"):
        assert pkg.name == expected["package_name"]
    # the courtyard keeps the margin around every pad extent and the label sits on it
    if "bbox_mm" in expected:
        box = expected["bbox_mm"]
        x1, y1, x2, y2 = courtyard(pkg)
        tol = 1e-4  # the sidecar and the parser round pad sizes independently
        assert x1 <= box["min_x"] - 0.25 + tol and y1 <= box["min_y"] - 0.25 + tol
        assert x2 >= box["max_x"] + 0.25 - tol and y2 >= box["max_y"] + 0.25 - tol
