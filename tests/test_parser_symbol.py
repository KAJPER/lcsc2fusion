"""Tests for the EasyEDA symbol parser (parser/symbol.py)."""

from __future__ import annotations

import contextlib
import json
import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from lcsc2fusion.constants import (
    EE_UNIT_MM,
    LABEL_SIZE_SYMBOL_MM,
    SYMBOL_WIRE_WIDTH_MM,
    Layer,
)
from lcsc2fusion.errors import ParseError
from lcsc2fusion.model import (
    Circle,
    Label,
    PinDirection,
    PinLength,
    Polygon,
    Symbol,
    TextAlign,
    Wire,
)
from lcsc2fusion.parser.symbol import parse_symbol_units, symbol_prefix

FIXTURES = Path(__file__).parent / "fixtures"
REAL_DIR = FIXTURES / "real"
SYNTH_DIR = FIXTURES / "synthetic"

U = EE_UNIT_MM  # one EasyEDA unit in mm

# ---------------------------------------------------------------------------------------------
# Builders for hand-written EasyEDA data
# ---------------------------------------------------------------------------------------------

# Pin line of a pin with EasyEDA rotation r: the line runs from the connection point to the body.
_LINE_DIR = {"": (-1, 0), "0": (-1, 0), "90": (0, 1), "180": (1, 0), "270": (0, -1)}


def pin(
    x: float,
    y: float,
    rot: str,
    number: str,
    name: str,
    *,
    ptype: str = "0",
    length: float = 10.0,
    inverted: str = "0",
    clock: str = "0",
    show: str = "show",
    spice: str | None = None,
) -> str:
    dx, dy = _LINE_DIR.get(rot, (-1, 0))
    if length == 0:
        path = f"M {x:g} {y:g}"
    elif dx:
        path = f"M {x:g} {y:g} h {dx * length:g}"
    else:
        path = f"M {x:g} {y:g} v {dy * length:g}"
    spice_no = number if spice is None else spice
    return (
        f"P~{show}~{ptype}~{spice_no}~{x:g}~{y:g}~{rot}~gge1~0"
        f"^^{x:g}~{y:g}"
        f"^^{path}~#880000"
        f"^^0~{x + 3:g}~{y + 3:g}~0~{name}~start~~~#0000FF"
        f"^^1~{x + 1:g}~{y - 1:g}~0~{number}~end~~~#0000FF"
        f"^^{inverted}~{x + 8:g}~{y:g}"
        f"^^{clock}~M {x:g} {y - 3:g} L {x + 3:g} {y:g} L {x:g} {y + 3:g}"
    )


BODY = "R~-10~-10~~~20~20~#880000~1~0~none~gge2~0~"
DEFAULT_BBOX = (-10.0, -10.0, 20.0, 20.0)  # centre (0, 0)


def make_result(
    shapes: list[str],
    *,
    bbox: tuple[float, float, float, float] | None = DEFAULT_BBOX,
    head: tuple[float, float] = (0.0, 0.0),
    pre: str | None = "U?",
) -> dict[str, Any]:
    c_para: dict[str, Any] = {} if pre is None else {"pre": pre}
    data_str: dict[str, Any] = {
        "head": {"x": head[0], "y": head[1], "c_para": c_para},
        "shape": shapes,
    }
    if bbox is not None:
        data_str["BBox"] = dict(zip(("x", "y", "width", "height"), bbox, strict=True))
    return {"dataStr": data_str}


def parse_one(result: Mapping[str, Any]) -> tuple[Symbol, list[str]]:
    symbols, warnings = parse_symbol_units(result, base_name="T")
    assert len(symbols) == 1
    return symbols[0], warnings


def with_pin(shape: str) -> dict[str, Any]:
    """A symbol with one filler pin plus the given graphic shape."""
    return make_result([pin(20, 0, "0", "1", "A"), shape])


def body(symbol: Symbol) -> list[Any]:
    return [g for g in symbol.graphics if not isinstance(g, Label)]


def wire_tuples(symbol: Symbol) -> list[tuple[float, float, float, float]]:
    return [(g.x1, g.y1, g.x2, g.y2) for g in body(symbol) if isinstance(g, Wire)]


# ---------------------------------------------------------------------------------------------
# Pins
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rot", "ee_xy", "expected_angle", "expected_xy"),
    [
        ("0", (20, 0), 180, (20 * U, 0.0)),  # right edge, line goes left
        ("90", (0, -20), 270, (0.0, 20 * U)),  # top edge (Y down), line goes down
        ("180", (-20, 0), 0, (-20 * U, 0.0)),  # left edge, line goes right
        ("270", (0, 20), 90, (0.0, -20 * U)),  # bottom edge, line goes up
        ("", (20, 0), 180, (20 * U, 0.0)),  # empty rotation means 0
    ],
)
def test_pin_rotation_and_position(
    rot: str,
    ee_xy: tuple[float, float],
    expected_angle: int,
    expected_xy: tuple[float, float],
) -> None:
    sym, warnings = parse_one(make_result([BODY, pin(ee_xy[0], ee_xy[1], rot, "1", "A")]))
    (p,) = sym.pins
    assert p.angle == expected_angle
    assert (p.x, p.y) == pytest.approx(expected_xy, abs=1e-6)
    assert warnings == []


def test_pin_rotation_odd_values() -> None:
    sym, warnings = parse_one(
        make_result(
            [
                BODY,
                pin(20, 0, "-90", "1", "A"),
                pin(20, 10, "360", "2", "B"),
                pin(20, 20, "45", "3", "C"),
            ]
        )
    )
    angles = {p.number: p.angle for p in sym.pins}
    assert angles["1"] == 90
    assert angles["2"] == 180
    assert angles["3"] in {0, 90, 180, 270}
    assert any("rotation" in w for w in warnings)


@pytest.mark.parametrize(
    ("length_units", "keyword", "warns"),
    [
        (0, PinLength.POINT, False),
        (3, PinLength.SHORT, True),  # 0.76 mm rounded up to 2.54 mm
        (10, PinLength.SHORT, False),  # 2.54 mm exactly
        (15, PinLength.MIDDLE, True),  # 3.81 mm rounded up to 5.08 mm
        (20, PinLength.MIDDLE, False),
        (25, PinLength.LONG, True),  # 6.35 mm
        (30, PinLength.LONG, False),  # 7.62 mm exactly
        (60, PinLength.LONG, True),
    ],
)
def test_pin_length_keyword(length_units: float, keyword: PinLength, warns: bool) -> None:
    sym, warnings = parse_one(make_result([BODY, pin(20, 0, "0", "1", "A", length=length_units)]))
    assert sym.pins[0].length == keyword
    assert any("pin length" in w for w in warnings) is warns


def test_pin_length_ignores_path_direction() -> None:
    # Some real symbols draw the pin line from the body end to the connection point.
    raw = "P~show~0~1~-20~0~180~gge1~0^^-20~0^^M -10 0 h -10~#800^^0~-6~0~0~A~start~~~#800"
    sym, _ = parse_one(make_result([BODY, raw]))
    assert sym.pins[0].length == PinLength.SHORT
    assert sym.pins[0].angle == 0


def test_pin_without_pin_line_is_a_point() -> None:
    raw = "P~show~0~1~20~0~0~gge1~0^^20~0^^^^0~0~0~0~A~start^^1~0~0~0~1~end"
    sym, _ = parse_one(make_result([BODY, raw]))
    assert sym.pins[0].length == PinLength.POINT


def test_pin_unreadable_pin_line_warns() -> None:
    raw = "P~show~0~1~20~0~0~gge1~0^^20~0^^M 20 0 X 5~#800^^0~0~0~0~A~start^^1~0~0~0~1~end"
    sym, warnings = parse_one(make_result([BODY, raw]))
    assert sym.pins[0].length == PinLength.POINT
    assert any("pin line" in w for w in warnings)


@pytest.mark.parametrize(
    ("ptype", "direction"),
    [
        ("0", PinDirection.PAS),
        ("", PinDirection.PAS),
        ("1", PinDirection.IN),
        ("2", PinDirection.OUT),
        ("3", PinDirection.IO),
        ("4", PinDirection.PWR),
    ],
)
def test_pin_direction(ptype: str, direction: PinDirection) -> None:
    sym, warnings = parse_one(make_result([BODY, pin(20, 0, "0", "1", "A", ptype=ptype)]))
    assert sym.pins[0].direction == direction
    assert warnings == []


def test_pin_unknown_type_falls_back_to_passive() -> None:
    sym, warnings = parse_one(make_result([BODY, pin(20, 0, "0", "1", "A", ptype="9")]))
    assert sym.pins[0].direction == PinDirection.PAS
    assert any("pin type" in w for w in warnings)


def test_pin_inverted_and_clock() -> None:
    sym, _ = parse_one(
        make_result(
            [
                BODY,
                pin(20, 0, "0", "1", "PLAIN"),
                pin(20, 10, "0", "2", "INV", inverted="1"),
                pin(20, 20, "0", "3", "CLK", clock="1"),
                pin(20, -10, "0", "4", "BOTH", inverted="1", clock="1"),
            ]
        )
    )
    flags = {p.number: (p.inverted, p.clock) for p in sym.pins}
    assert flags == {
        "1": (False, False),
        "2": (True, False),
        "3": (False, True),
        "4": (True, True),
    }


def test_hidden_pins_are_kept() -> None:
    sym, _ = parse_one(make_result([BODY, pin(20, 0, "0", "1", "A", show="hide")]))
    assert [p.number for p in sym.pins] == ["1"]


def test_pin_number_falls_back_to_spice_number() -> None:
    raw = "P~show~0~7~20~0~0~gge1~0^^20~0^^M 20 0 h -10~#800^^0~0~0~0~SIG~start^^1~0~0~0~~end"
    sym, _ = parse_one(make_result([BODY, raw]))
    assert sym.pins[0].number == "7"
    assert sym.pins[0].name == "SIG"


def test_pin_number_section_overrides_spice_number() -> None:
    sym, _ = parse_one(make_result([BODY, pin(20, 0, "0", "A1B12", "VBUS", spice="3")]))
    assert sym.pins[0].number == "A1B12"


def test_pin_without_any_number_is_an_error() -> None:
    raw = "P~show~0~~20~0~0~gge1~0^^20~0^^M 20 0 h -10~#800^^0~0~0~0~SIG~start^^1~0~0~0~~end"
    with pytest.raises(ParseError, match="number"):
        parse_symbol_units(make_result([BODY, raw]), base_name="T")


def test_pin_names_empty_and_unsafe() -> None:
    sym, _ = parse_one(
        make_result(
            [
                BODY,
                pin(20, 0, "0", "1", ""),  # empty name -> number
                pin(20, 10, "0", "2", "A B;C"),  # whitespace and ';' -> '_'
                pin(20, 20, "0", "3", "X@Y"),  # '@' is Eagle's suffix delimiter
                pin(20, 30, "0", "4", "PB5(SCK/PCINT5)"),  # other characters survive
            ]
        )
    )
    assert [p.name for p in sym.pins] == ["1", "A_B_C", "X_Y", "PB5(SCK/PCINT5)"]


def test_pin_duplicate_names_get_suffix_on_all_occurrences() -> None:
    sym, _ = parse_one(
        make_result(
            [
                BODY,
                pin(20, 0, "0", "1", "GND"),
                pin(20, 10, "0", "2", "VCC"),
                pin(20, 20, "0", "3", "GND"),
                pin(20, 30, "0", "4", "gnd"),  # names are unique case-insensitively in Eagle
                pin(20, 40, "0", "5", ""),
                pin(20, 50, "0", "6", "5"),  # empty name above falls back to "5"
            ]
        )
    )
    assert [p.name for p in sym.pins] == ["GND@1", "VCC", "GND@2", "gnd@3", "5@1", "5@2"]
    assert len({p.name.casefold() for p in sym.pins}) == len(sym.pins)


def test_repeated_pin_numbers_warn() -> None:
    _, warnings = parse_one(
        make_result([BODY, pin(20, 0, "0", "1", "A"), pin(20, 10, "0", "1", "B")])
    )
    assert any("repeated pin numbers" in w for w in warnings)


@pytest.mark.parametrize(
    "bad_pin",
    [
        "P~show~0~1~abc~0~0~gge1~0^^0~0^^M 0 0 h -10",  # x is not a number
        "P~show~0~1~0~nan~0~gge1~0^^0~0^^M 0 0 h -10",  # not finite
        "P~show~x~1~0~0~0~gge1~0^^0~0^^M 0 0 h -10",  # type is not a number
        "P~show~0~1~0~0~r~gge1~0^^0~0^^M 0 0 h -10",  # rotation is not a number
        "P~show~0~1",  # truncated
    ],
)
def test_malformed_pin_is_parse_error(bad_pin: str) -> None:
    with pytest.raises(ParseError):
        parse_symbol_units(make_result([BODY, bad_pin]), base_name="T")


# ---------------------------------------------------------------------------------------------
# Graphics
# ---------------------------------------------------------------------------------------------


def test_rect_becomes_four_wires() -> None:
    sym, warnings = parse_one(make_result([pin(20, 0, "0", "1", "A"), BODY]))
    edge = 10 * U
    assert wire_tuples(sym) == [
        pytest.approx((-edge, edge, edge, edge), abs=1e-6),
        pytest.approx((edge, edge, edge, -edge), abs=1e-6),
        pytest.approx((edge, -edge, -edge, -edge), abs=1e-6),
        pytest.approx((-edge, -edge, -edge, edge), abs=1e-6),
    ]
    for g in body(sym):
        assert isinstance(g, Wire)
        assert g.width == SYMBOL_WIRE_WIDTH_MM
        assert g.layer == Layer.SYMBOLS
        assert g.curve == 0.0
    assert warnings == []


def test_rect_ignores_corner_radius_and_fill() -> None:
    shape = "R~-10~-10~2~2~20~20~#880000~1~0~#FFFFCC~gge2~0~"
    sym, _ = parse_one(make_result([pin(20, 0, "0", "1", "A"), shape]))
    assert len(wire_tuples(sym)) == 4


def test_circle_ring_and_filled_disc() -> None:
    ring = "C~5~5~4~#880000~1~0~none~gge3~0"
    disc = "C~-5~-5~1.5~#880000~1~0~#880000~gge4~0"
    sym, _ = parse_one(make_result([pin(20, 0, "0", "1", "A"), ring, disc]))
    ring_c, disc_c = body(sym)
    assert isinstance(ring_c, Circle)
    assert (ring_c.x, ring_c.y, ring_c.radius) == pytest.approx((5 * U, -5 * U, 4 * U))
    assert ring_c.width == SYMBOL_WIRE_WIDTH_MM
    assert ring_c.layer == Layer.SYMBOLS
    assert isinstance(disc_c, Circle)
    assert (disc_c.x, disc_c.y, disc_c.radius) == pytest.approx((-5 * U, 5 * U, 1.5 * U))
    assert disc_c.width == 0.0


def test_ellipse_with_equal_radii_is_a_circle() -> None:
    sym, _ = parse_one(with_pin("E~0~0~3~3~#880000~1~0~none~gge5~0"))
    (circle,) = body(sym)
    assert isinstance(circle, Circle)
    assert circle.radius == pytest.approx(3 * U)
    assert circle.width == SYMBOL_WIRE_WIDTH_MM


def test_true_ellipse_is_a_closed_polyline() -> None:
    sym, _ = parse_one(with_pin("E~0~0~10~5~#880000~1~0~none~gge5~0"))
    wires = [g for g in body(sym) if isinstance(g, Wire)]
    assert len(wires) == 36
    xs = [w.x1 for w in wires]
    ys = [w.y1 for w in wires]
    assert max(xs) == pytest.approx(10 * U)
    assert min(xs) == pytest.approx(-10 * U)
    assert max(ys) == pytest.approx(5 * U)
    assert min(ys) == pytest.approx(-5 * U)
    for a, b in zip(wires, [*wires[1:], wires[0]], strict=True):  # closed chain
        assert (a.x2, a.y2) == pytest.approx((b.x1, b.y1))


def test_filled_ellipse_polyline_becomes_polygon() -> None:
    sym, _ = parse_one(with_pin("E~0~0~10~5~#880000~1~0~#880000~gge5~0"))
    (poly,) = body(sym)
    assert isinstance(poly, Polygon)
    assert len(poly.points) == 36


def test_arc_semicircle_has_curve_minus_180() -> None:
    # SVG sweep=1 is clockwise on the Y down screen, which is negative `curve` in Eagle.
    sym, _ = parse_one(with_pin("A~M 0 0 A 5 5 0 0 1 10 0~~#880000~1~0~none~gge6~0"))
    (w,) = body(sym)
    assert isinstance(w, Wire)
    assert (w.x1, w.y1, w.x2, w.y2) == pytest.approx((0.0, 0.0, 10 * U, 0.0))
    assert w.curve == pytest.approx(-180.0)
    assert w.width == SYMBOL_WIRE_WIDTH_MM
    assert w.layer == Layer.SYMBOLS


def test_arc_quarter_signs() -> None:
    cw, _ = parse_one(with_pin("A~M 0 0 A 10 10 0 0 1 10 10~~#880000~1~0~none~gge6~0"))
    ccw, _ = parse_one(with_pin("A~M 0 0 A 10 10 0 0 0 10 10~~#880000~1~0~none~gge6~0"))
    (w_cw,) = body(cw)
    (w_ccw,) = body(ccw)
    assert isinstance(w_cw, Wire)
    assert isinstance(w_ccw, Wire)
    assert w_cw.curve == pytest.approx(-90.0)
    assert w_ccw.curve == pytest.approx(90.0)
    assert (w_cw.x2, w_cw.y2) == pytest.approx((10 * U, -10 * U))  # Y flipped


def test_arc_degenerate_is_dropped_with_warning() -> None:
    sym, warnings = parse_one(with_pin("A~M 4 4 A 5 5 0 0 1 4 4~~#880000~1~0~none~gge6~0"))
    assert body(sym) == []
    assert any("degenerate" in w for w in warnings)


def test_polyline() -> None:
    sym, _ = parse_one(with_pin("PL~0 0 10 0 10 10~#880000~1~0~none~gge7~0"))
    assert wire_tuples(sym) == [
        pytest.approx((0.0, 0.0, 10 * U, 0.0)),
        pytest.approx((10 * U, 0.0, 10 * U, -10 * U)),
    ]


def test_polyline_zero_length_is_dropped_with_warning() -> None:
    sym, warnings = parse_one(with_pin("PL~5 5 5 5~#880000~1~0~none~gge7~0"))
    assert body(sym) == []
    assert any("PL" in w and "degenerate" in w for w in warnings)


def test_polyline_odd_coordinate_count_is_parse_error() -> None:
    with pytest.raises(ParseError, match="odd"):
        parse_symbol_units(with_pin("PL~0 0 10~#880000~1~0~none~gge7~0"), base_name="T")


def test_polygon_outline_and_filled() -> None:
    outline, _ = parse_one(with_pin("PG~0 0 10 0 10 10~#880000~1~0~none~gge8~0"))
    assert wire_tuples(outline) == [
        pytest.approx((0.0, 0.0, 10 * U, 0.0)),
        pytest.approx((10 * U, 0.0, 10 * U, -10 * U)),
        pytest.approx((10 * U, -10 * U, 0.0, 0.0)),  # closing wire
    ]
    filled, _ = parse_one(with_pin("PG~0 0 10 0 10 10~#880000~1~0~#880000~gge8~0"))
    (poly,) = body(filled)
    assert isinstance(poly, Polygon)
    assert [(p.x, p.y) for p in poly.points] == pytest.approx(
        [(0.0, 0.0), (10 * U, 0.0), (10 * U, -10 * U)]
    )
    assert poly.layer == Layer.SYMBOLS
    assert poly.width == SYMBOL_WIRE_WIDTH_MM


def test_path_with_lines_and_close() -> None:
    sym, _ = parse_one(with_pin("PT~M 0 0 L 10 0 L 10 10 Z~#880000~1~0~none~gge9~0~"))
    assert wire_tuples(sym) == [
        pytest.approx((0.0, 0.0, 10 * U, 0.0)),
        pytest.approx((10 * U, 0.0, 10 * U, -10 * U)),
        pytest.approx((10 * U, -10 * U, 0.0, 0.0)),
    ]


def test_filled_path_becomes_polygon() -> None:
    sym, _ = parse_one(with_pin("PT~M 5 -6 L -5 0 L 5 6 Z~#880000~1~0~#880000~gge9~0~"))
    (poly,) = body(sym)
    assert isinstance(poly, Polygon)
    assert len(poly.points) == 3


def test_path_single_arc_stays_curved() -> None:
    sym, _ = parse_one(with_pin("PT~M 0 0 A 5 5 0 0 1 10 0~#880000~1~0~none~gge9~0~"))
    (w,) = body(sym)
    assert isinstance(w, Wire)
    assert w.curve == pytest.approx(-180.0)


def test_path_bezier_is_flattened() -> None:
    sym, _ = parse_one(with_pin("PT~M 0 0 C 0 5 5 10 10 10~#880000~1~0~none~gge9~0~"))
    wires = [g for g in body(sym) if isinstance(g, Wire)]
    assert len(wires) == 8
    assert (wires[0].x1, wires[0].y1) == pytest.approx((0.0, 0.0))
    assert (wires[-1].x2, wires[-1].y2) == pytest.approx((10 * U, -10 * U))
    assert all(w.curve == 0.0 for w in wires)


def test_path_errors() -> None:
    with pytest.raises(ParseError):
        parse_symbol_units(
            with_pin("PT~M 0 0 A 5 5 0 2 1 10 0~#880000~1~0~none~gge9~0~"), base_name="T"
        )
    with pytest.raises(ParseError):
        parse_symbol_units(with_pin("PT~L 0 0 10 10~#880000~1~0~none~gge9~0~"), base_name="T")


def test_text_arrowheads_and_unknown_shapes() -> None:
    shapes = [
        pin(20, 0, "0", "1", "A"),
        "T~L~0~0~0~#0000FF~Tahoma~11.5pt~0.1~~middle~comment~Hello~1~middle~gge1~0~pinpart",
        "AR~1~2~3",
        "ZZ~1~2",
        "ZZ~3~4",
    ]
    sym, warnings = parse_one(make_result(shapes))
    assert body(sym) == []
    assert len(warnings) == 1  # only the unknown tag, once
    assert "ZZ" in warnings[0]


def test_blank_shape_entries_are_skipped() -> None:
    sym, _ = parse_one(make_result([pin(20, 0, "0", "1", "A"), "", "  "]))
    assert len(sym.pins) == 1


# ---------------------------------------------------------------------------------------------
# Origin
# ---------------------------------------------------------------------------------------------


def test_origin_is_bbox_centre_snapped_to_the_5_unit_grid() -> None:
    # BBox centre (12, 13) snaps to (10, 15), so a pin at (10, 15) lands on (0, 0).
    result = make_result([pin(10, 15, "0", "1", "A")], bbox=(2.0, 3.0, 20.0, 20.0), head=(99, 99))
    sym, _ = parse_one(result)
    assert (sym.pins[0].x, sym.pins[0].y) == (0.0, 0.0)


def test_origin_snap_rounds_to_nearest() -> None:
    # centre (7.4, 12.6) -> (5, 15)
    result = make_result([pin(5, 15, "0", "1", "A")], bbox=(-2.6, 2.6, 20.0, 20.0))
    sym, _ = parse_one(result)
    assert (sym.pins[0].x, sym.pins[0].y) == (0.0, 0.0)


def test_origin_falls_back_to_head_when_bbox_is_empty() -> None:
    for bbox in (None, (0.0, 0.0, 0.0, 0.0)):
        result = make_result([pin(60, 40, "0", "1", "A")], bbox=bbox, head=(50, 40))
        sym, _ = parse_one(result)
        assert (sym.pins[0].x, sym.pins[0].y) == pytest.approx((10 * U, 0.0))


def test_origin_accepts_string_numbers_and_flips_y() -> None:
    result = make_result([pin(10, 20, "0", "1", "A")], bbox=None, head=(0, 0))
    result["dataStr"]["head"] = {"x": "0", "y": "0"}
    sym, _ = parse_one(result)
    assert (sym.pins[0].x, sym.pins[0].y) == pytest.approx((10 * U, -20 * U))


def test_no_negative_zero_coordinates() -> None:
    sym, _ = parse_one(make_result([BODY, pin(20, 0, "0", "1", "A")]))
    assert math.copysign(1.0, sym.pins[0].y) == 1.0


# ---------------------------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------------------------


def test_name_and_value_labels() -> None:
    shapes = [BODY, pin(20, 0, "0", "1", "A"), pin(0, -20, "90", "2", "B")]
    sym, _ = parse_one(make_result(shapes))
    labels = [g for g in sym.graphics if isinstance(g, Label)]
    name, value = labels
    assert (name.text, name.layer, name.align) == (">NAME", Layer.NAMES, TextAlign.BOTTOM_LEFT)
    assert (value.text, value.layer, value.align) == (">VALUE", Layer.VALUES, TextAlign.TOP_LEFT)
    assert name.size == value.size == LABEL_SIZE_SYMBOL_MM
    assert name.x == value.x == pytest.approx(-10 * U)  # left edge of the body
    top_pin_y = 20 * U
    assert name.y > top_pin_y  # clears the pin on the top edge
    assert value.y < -10 * U  # below the body
    assert sym.graphics[-2:] == (name, value) or list(sym.graphics[-2:]) == [name, value]


def test_labels_without_graphics_use_pin_extent() -> None:
    sym, _ = parse_one(make_result([pin(20, 0, "0", "1", "A"), pin(-20, 10, "180", "2", "B")]))
    name, value = (g for g in sym.graphics if isinstance(g, Label))
    assert name.x == value.x == pytest.approx(-20 * U)
    assert name.y > 0.0 > -10 * U > value.y


def test_labels_clear_curved_graphics() -> None:
    # Half circle bulging upwards (towards +Y): the label must sit above the bulge.
    sym, _ = parse_one(
        make_result([pin(20, 0, "0", "1", "A"), "A~M -5 0 A 5 5 0 0 1 5 0~~#880000~1~0~none~g~0"])
    )
    name = next(g for g in sym.graphics if isinstance(g, Label) and g.text == ">NAME")
    assert name.y > 5 * U


# ---------------------------------------------------------------------------------------------
# Prefix
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pre", "expected"),
    [
        ("U?", "U"),
        ("R?", "R"),
        ("U?.1", "U"),
        ("USBC?", "USBC"),
        ("R12", "R"),
        ("J1.2", "J"),
        ("  LED? ", "LED"),
        ("?", "U"),
        ("", "U"),
        ("12", "U"),
        (None, "U"),
    ],
)
def test_symbol_prefix(pre: str | None, expected: str) -> None:
    assert symbol_prefix(make_result([BODY], pre=pre)) == expected


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"dataStr": None},
        {"dataStr": "not json"},
        {"dataStr": {}},
        {"dataStr": {"head": None}},
        {"dataStr": {"head": {"c_para": None}}},
        {"dataStr": {"head": {"c_para": {"pre": 5}}}},
    ],
)
def test_symbol_prefix_falls_back_to_u(result: dict[str, Any]) -> None:
    assert symbol_prefix(result) == "U"


# ---------------------------------------------------------------------------------------------
# Multi-unit parts and structure errors
# ---------------------------------------------------------------------------------------------


def unit_data(origin: tuple[float, float], shapes: list[str]) -> dict[str, Any]:
    ox, oy = origin
    return {
        "docType": "6",
        "dataStr": {
            "head": {"x": ox, "y": oy, "c_para": {"pre": "U?.1"}},
            "BBox": {"x": ox - 20, "y": oy - 20, "width": 40, "height": 40},
            "shape": shapes,
        },
    }


def multi_result(units: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "dataStr": {
            "head": {"x": 90, "y": 40, "c_para": {"pre": "U?"}},
            "BBox": {"x": 0, "y": 0, "width": 0, "height": 0},
            "shape": [],
        },
        "subparts": units,
    }


def test_multi_unit_one_symbol_per_unit() -> None:
    result = multi_result(
        [
            unit_data((100, 120), [pin(60, 130, "180", "3", "IN"), pin(140, 120, "0", "1", "OUT")]),
            unit_data((125, 155), [pin(85, 165, "180", "5", "IN"), pin(165, 155, "0", "7", "OUT")]),
        ]
    )
    symbols, warnings = parse_symbol_units(result, base_name="OPAMP")
    assert [s.name for s in symbols] == ["OPAMP_1", "OPAMP_2"]
    assert [[p.number for p in s.pins] for s in symbols] == [["3", "1"], ["5", "7"]]
    # each unit is centred on its own origin
    assert (symbols[0].pins[0].x, symbols[0].pins[0].y) == pytest.approx((-40 * U, -10 * U))
    assert (symbols[1].pins[0].x, symbols[1].pins[0].y) == pytest.approx((-40 * U, -10 * U))
    assert (symbols[1].pins[1].x, symbols[1].pins[1].y) == pytest.approx((40 * U, 0.0))
    assert warnings == []
    assert symbol_prefix(result) == "U"


def test_multi_unit_skips_units_without_pins() -> None:
    result = multi_result(
        [
            unit_data((100, 120), [BODY]),
            unit_data((125, 155), [pin(85, 165, "180", "5", "IN")]),
        ]
    )
    symbols, warnings = parse_symbol_units(result, base_name="X")
    assert [s.name for s in symbols] == ["X"]
    assert any("no pins" in w for w in warnings)


def test_multi_unit_without_any_pins_is_an_error() -> None:
    with pytest.raises(ParseError, match="no unit"):
        parse_symbol_units(multi_result([unit_data((0, 0), [BODY])]), base_name="X")


def test_main_symbol_with_pins_wins_over_subparts() -> None:
    result = make_result([BODY, pin(20, 0, "0", "1", "A")])
    result["subparts"] = [unit_data((0, 0), [pin(20, 0, "0", "9", "Z")])]
    symbols, warnings = parse_symbol_units(result, base_name="X")
    assert [s.name for s in symbols] == ["X"]
    assert symbols[0].pins[0].number == "1"
    assert any("subparts" in w for w in warnings)


def test_data_str_may_be_a_json_string() -> None:
    result = make_result([BODY, pin(20, 0, "0", "1", "A")])
    result["dataStr"] = json.dumps(result["dataStr"])
    sym, _ = parse_one(result)
    assert len(sym.pins) == 1
    assert symbol_prefix(result) == "U"


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (make_result([BODY]), "no pins"),
        (make_result([]), "no pins"),
        ({"dataStr": {"head": {}}}, "no pins"),
        ({}, "dataStr"),
        ({"dataStr": None}, "dataStr"),
        ({"dataStr": "{broken"}, "JSON"),
        ({"dataStr": [1, 2]}, "dataStr"),
        ({"dataStr": {"shape": "P~show"}}, "list"),
        ({"dataStr": {"shape": [1, 2]}}, "strings"),
        ({"dataStr": {"shape": ["~abc"]}}, "Empty shape tag"),
        ({"dataStr": {"shape": [BODY], "BBox": {"x": "abc", "width": 1}}}, "BBox|x"),
        ({"dataStr": {"shape": [BODY], "head": {"x": [1]}}}, "head|x"),
        (make_result([BODY, "R~a~b~~~c~d"]), "number"),
        (make_result([BODY, pin(0, 0, "0", "1", "A"), "C~1~2~abc"]), "r"),
    ],
)
def test_malformed_input_raises_parse_error(result: dict[str, Any], message: str) -> None:
    with pytest.raises(ParseError) as info:
        parse_symbol_units(result, base_name="T")
    assert re.search(message, str(info.value)), str(info.value)


def test_subparts_are_not_inspected_when_the_main_symbol_has_pins() -> None:
    result = make_result([BODY, pin(20, 0, "0", "1", "A")])
    result["subparts"] = [5]
    symbols, warnings = parse_symbol_units(result, base_name="T")
    assert len(symbols) == 1
    assert any("subparts" in w for w in warnings)


def test_result_not_a_mapping_is_parse_error() -> None:
    with pytest.raises(ParseError):
        parse_symbol_units([1, 2], base_name="T")  # type: ignore[arg-type]


def test_multi_unit_with_malformed_subpart_is_parse_error() -> None:
    with pytest.raises(ParseError, match="Subpart 1"):
        parse_symbol_units(multi_result([{"docType": "6"}]), base_name="X")
    with pytest.raises(ParseError, match="Subpart 1"):
        parse_symbol_units(multi_result([5]), base_name="X")  # type: ignore[list-item]


@pytest.mark.parametrize("junk", ["", "P", "P~", "P~show", "R~", "PL~", "PT~", "A~", "E~", "C~"])
def test_truncated_shapes_never_raise_foreign_exceptions(junk: str) -> None:
    result = make_result([BODY, pin(20, 0, "0", "1", "A"), junk])
    with contextlib.suppress(ParseError):
        parse_symbol_units(result, base_name="T")


# ---------------------------------------------------------------------------------------------
# Real EasyEDA responses (local only, gitignored)
# ---------------------------------------------------------------------------------------------

REAL_FILES = sorted(REAL_DIR.glob("*.json")) if REAL_DIR.is_dir() else []

# Pin count per unit, from the live reference set (docs/RESEARCH.md 1.12).
EXPECTED_PINS: dict[str, list[int]] = {
    "C1035": [2],
    "C12674": [2],
    "C13738": [4],
    "C14663": [2],
    "C14877": [32],
    "C15483": [15],
    "C165948": [16],
    "C2040": [57],
    "C20526": [3],
    "C2286": [2],
    "C25804": [2],
    "C2895846": [40],
    "C2913202": [41],
    "C318884": [4],
    "C454188": [9],
    "C46749": [8],
    "C476261": [2],
    "C6186": [4],
    "C6961": [5, 3],
    "C7593": [8],
    "C7943": [14],
    "C81598": [2],
    "C8678": [2],
    "C8734": [48],
}

# Extent of EasyEDA symbols is small; a wrong origin would put the body far from (0, 0).
MAX_CENTRE_OFFSET_MM = 2.6
_NAME_RE = re.compile(r"^[^\s;@]+(@\d+)?$")
_ANGLE_VECTORS = {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}


def load_real(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    result = data["result"]
    assert isinstance(result, dict)
    return result


def _extent(symbol: Symbol) -> tuple[list[float], list[float]]:
    xs: list[float] = [p.x for p in symbol.pins]
    ys: list[float] = [p.y for p in symbol.pins]
    for g in body(symbol):
        if isinstance(g, Wire):
            xs += [g.x1, g.x2]
            ys += [g.y1, g.y2]
        elif isinstance(g, Circle):
            xs += [g.x - g.radius, g.x + g.radius]
            ys += [g.y - g.radius, g.y + g.radius]
        elif isinstance(g, Polygon):
            xs += [p.x for p in g.points]
            ys += [p.y for p in g.points]
    return xs, ys


@pytest.mark.real
@pytest.mark.parametrize("path", REAL_FILES, ids=[p.stem for p in REAL_FILES])
def test_real_part_parses(path: Path) -> None:
    result = load_real(path)
    symbols, warnings = parse_symbol_units(result, base_name=path.stem)
    assert [len(s.pins) for s in symbols] == EXPECTED_PINS[path.stem]
    assert isinstance(warnings, list)
    assert re.fullmatch(r"[A-Za-z]+", symbol_prefix(result))

    for symbol in symbols:
        assert symbol.pins
        numbers = [p.number for p in symbol.pins]
        assert all(n.strip() == n and n for n in numbers)
        assert len(set(numbers)) == len(numbers)
        names = [p.name for p in symbol.pins]
        assert all(_NAME_RE.match(n) for n in names), names
        assert len({n.casefold() for n in names}) == len(names)
        assert {p.angle for p in symbol.pins} <= set(_ANGLE_VECTORS)
        assert all(math.copysign(1.0, c) == 1.0 for p in symbol.pins for c in (p.x, p.y) if c == 0)

        graphics = body(symbol)
        assert graphics, "symbol has no body graphics"
        labels = [g for g in symbol.graphics if isinstance(g, Label)]
        assert [(lb.text, lb.layer) for lb in labels] == [
            (">NAME", Layer.NAMES),
            (">VALUE", Layer.VALUES),
        ]
        assert {g.layer for g in graphics} == {Layer.SYMBOLS}

        # The symbol is centred on its origin (within the snapping error).
        xs, ys = _extent(symbol)
        assert abs((min(xs) + max(xs)) / 2) <= MAX_CENTRE_OFFSET_MM
        assert abs((min(ys) + max(ys)) / 2) <= MAX_CENTRE_OFFSET_MM

        # Every pin line points from its connection point towards the body.
        gxs, gys = _extent(symbol.__class__(symbol.name, (), symbol.graphics))
        bx, by = (min(gxs) + max(gxs)) / 2, (min(gys) + max(gys)) / 2
        for p in symbol.pins:
            dx, dy = _ANGLE_VECTORS[p.angle]
            assert dx * (bx - p.x) + dy * (by - p.y) > 0, f"pin {p.number} points away"


@pytest.mark.real
@pytest.mark.skipif(not (REAL_DIR / "C2040.json").exists(), reason="C2040 fixture missing")
def test_real_rp2040_pin_geometry() -> None:
    (symbol,) = parse_symbol_units(load_real(REAL_DIR / "C2040.json"), base_name="RP2040")[0]
    by_number = {p.number: p for p in symbol.pins}
    assert len(by_number) == 57
    # Bottom edge pin with an empty electrical type: EasyEDA rotation 270, line goes up.
    p19 = by_number["19"]
    assert (p19.x, p19.y) == pytest.approx((-12.7, -50.8))
    assert p19.angle == 90
    assert p19.direction == PinDirection.PAS
    assert p19.length == PinLength.SHORT
    # Right edge pin: rotation 0, line goes left.
    p9 = by_number["9"]
    assert (p9.x, p9.y) == pytest.approx((31.75, 15.24))
    assert p9.angle == 180
    assert p9.direction == PinDirection.IO


@pytest.mark.real
@pytest.mark.skipif(not (REAL_DIR / "C6961.json").exists(), reason="C6961 fixture missing")
def test_real_multi_unit_opamp() -> None:
    result = load_real(REAL_DIR / "C6961.json")
    assert result["dataStr"]["shape"] == []  # units live in subparts
    symbols, warnings = parse_symbol_units(result, base_name="TL072")
    assert [s.name for s in symbols] == ["TL072_1", "TL072_2"]
    assert [len(s.pins) for s in symbols] == [5, 3]
    assert warnings == []
    unit1 = {p.number: p for p in symbols[0].pins}
    assert set(unit1) == {"1", "2", "3", "4", "8"}
    assert (unit1["1"].x, unit1["1"].y, unit1["1"].angle) == pytest.approx((10.16, 0.0, 180))
    assert unit1["1"].direction == PinDirection.OUT
    assert (unit1["8"].x, unit1["8"].y, unit1["8"].angle) == pytest.approx((0.0, 10.16, 270))
    assert unit1["8"].direction == PinDirection.PWR
    assert (unit1["4"].x, unit1["4"].y, unit1["4"].angle) == pytest.approx((0.0, -10.16, 90))
    assert (unit1["3"].x, unit1["3"].y, unit1["3"].angle) == pytest.approx((-10.16, -2.54, 0))
    assert {p.number for p in symbols[1].pins} == {"5", "6", "7"}


@pytest.mark.real
@pytest.mark.skipif(not (REAL_DIR / "C25804.json").exists(), reason="C25804 fixture missing")
def test_real_resistor() -> None:
    result = load_real(REAL_DIR / "C25804.json")
    symbols, warnings = parse_symbol_units(result, base_name="R")
    (symbol,) = symbols
    assert symbol_prefix(result) == "R"
    assert warnings == []
    pins = {p.number: p for p in symbol.pins}
    assert (pins["1"].x, pins["1"].y, pins["1"].angle) == pytest.approx((-5.08, 0.0, 0))
    assert (pins["2"].x, pins["2"].y, pins["2"].angle) == pytest.approx((5.08, 0.0, 180))
    assert len(wire_tuples(symbol)) == 4  # rectangle body


@pytest.mark.real
@pytest.mark.skipif(not (REAL_DIR / "C14663.json").exists(), reason="C14663 fixture missing")
def test_real_capacitor_pin_line_drawn_backwards() -> None:
    # This part draws its pin lines from the body end to the connection point.
    (symbol,) = parse_symbol_units(load_real(REAL_DIR / "C14663.json"), base_name="C")[0]
    pins = {p.number: p for p in symbol.pins}
    assert pins["1"].angle == 0
    assert pins["2"].angle == 180
    assert pins["1"].length == pins["2"].length == PinLength.SHORT


# ---------------------------------------------------------------------------------------------
# Synthetic fixtures with expected pin tables (written by hand, committed)
# ---------------------------------------------------------------------------------------------

SYNTH_CASES = (
    sorted(p for p in SYNTH_DIR.glob("*.json") if not p.name.endswith(".expected.json"))
    if SYNTH_DIR.is_dir()
    else []
)


def _expected_for(path: Path) -> Path:
    return path.with_name(f"{path.stem}.expected.json")


def _expected_symbols(expected: Any) -> list[list[dict[str, Any]]]:
    if isinstance(expected, dict) and "symbols" in expected:
        return [list(s["pins"]) for s in expected["symbols"]]
    if isinstance(expected, dict) and "pins" in expected:
        pins = list(expected["pins"])
        if any("unit" in pin for pin in pins):
            units = sorted({int(pin.get("unit", 1)) for pin in pins})
            return [[pin for pin in pins if int(pin.get("unit", 1)) == unit] for unit in units]
        return [pins]
    if isinstance(expected, list):
        return [list(expected)]
    raise AssertionError("unrecognised expected file layout")


def assert_symbols_match(result: Mapping[str, Any], expected_doc: Any, base_name: str) -> None:
    expected = _expected_symbols(expected_doc)
    symbols, _ = parse_symbol_units(result, base_name=base_name)
    assert len(symbols) == len(expected)
    for symbol, exp_pins in zip(symbols, expected, strict=True):
        assert len(symbol.pins) == len(exp_pins)
        for got, want in zip(symbol.pins, exp_pins, strict=True):
            assert got.number == str(want["number"])
            # Duplicate names get "@n" suffixes; Eagle displays only the part before "@".
            assert got.name.split("@")[0] == want["name"]
            assert got.x == pytest.approx(want["x_mm"], abs=1e-3)
            assert got.y == pytest.approx(want["y_mm"], abs=1e-3)
            assert got.angle == int(want["angle_deg"])
            assert got.direction.value == want["direction"]


@pytest.mark.parametrize(
    "path",
    [p for p in SYNTH_CASES if _expected_for(p).exists()],
    ids=lambda p: p.stem,
)
def test_synthetic_symbols_match_expected(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    expected = json.loads(_expected_for(path).read_text(encoding="utf-8"))
    assert_symbols_match(data.get("result", data), expected, path.stem)


def test_expected_comparison_helper() -> None:
    result = make_result([BODY, pin(20, 0, "0", "1", "A", ptype="1")])
    pins = [
        {"number": "1", "name": "A", "x_mm": 5.08, "y_mm": 0.0, "angle_deg": 180, "direction": "in"}
    ]
    assert_symbols_match(result, {"symbols": [{"pins": pins}]}, "T")
    assert_symbols_match(result, {"pins": pins}, "T")
    assert_symbols_match(result, pins, "T")
    with pytest.raises(AssertionError):
        assert_symbols_match(result, [{**pins[0], "angle_deg": 0}], "T")
    with pytest.raises(AssertionError):
        assert_symbols_match(result, [{**pins[0], "x_mm": 5.0}], "T")
    with pytest.raises(AssertionError):
        assert_symbols_match(result, {"unknown": 1}, "T")
