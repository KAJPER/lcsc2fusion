from __future__ import annotations

import math

import pytest

from lcsc2fusion.errors import ParseError
from lcsc2fusion.parser.svgpath import (
    ArcTo,
    ClosePath,
    LineTo,
    MoveTo,
    arc_curve_deg,
    arc_to_center,
    flatten_arc,
    flatten_path,
    parse_path,
)


def test_parse_absolute_and_implicit_lineto() -> None:
    segs = parse_path("M 1 2 3 4 L 5 6 Z")
    assert segs == [MoveTo(1, 2), LineTo(3, 4), LineTo(5, 6), ClosePath()]


def test_parse_relative_and_hv() -> None:
    segs = parse_path("m1 1 h2 v3 l-1 -1 z")
    assert segs == [MoveTo(1, 1), LineTo(3, 1), LineTo(3, 4), LineTo(2, 3), ClosePath()]


def test_parse_compact_numbers_and_flags() -> None:
    segs = parse_path("M0,0A5 5 0 015 5")
    assert segs[1] == ArcTo(5, 5, 0, False, True, 5, 5)
    assert parse_path("M1.5.5L-1-2") == [MoveTo(1.5, 0.5), LineTo(-1, -2)]


def test_parse_exponent() -> None:
    assert parse_path("M1e1 2E-1")[0] == MoveTo(10, 0.2)


def test_parse_errors() -> None:
    with pytest.raises(ParseError):
        parse_path("L 1 2 3")
    with pytest.raises(ParseError):
        parse_path("1 2")
    with pytest.raises(ParseError):
        parse_path("M 0 0 S 1 1 2 2")
    with pytest.raises(ParseError):
        parse_path("M 0 0 A 1 1 0 2 0 1 1")


def _arc(path: str) -> tuple[float, float, ArcTo]:
    segs = parse_path(path)
    start, arc = segs[0], segs[1]
    assert isinstance(start, MoveTo)
    assert isinstance(arc, ArcTo)
    return start.x, start.y, arc


def test_quarter_circle_real_footprint_arc() -> None:
    # Arc seen in a real 0603 capacitor footprint (centre at 4004.2527, 2998.4261).
    x1, y1, arc = _arc("M 4004.2527 2997.2062 A 1.2198 1.2198 0 0 1 4005.4725 2998.4261")
    center = arc_to_center(x1, y1, arc)
    assert center is not None
    assert center.cx == pytest.approx(4004.2527, abs=1e-3)
    assert center.cy == pytest.approx(2998.4261, abs=1e-3)
    assert math.degrees(center.delta) == pytest.approx(90.0, abs=0.01)
    # sweep=1 is clockwise on the Y-down screen, which is clockwise (negative) in Eagle.
    assert arc_curve_deg(x1, y1, arc) == pytest.approx(-90.0, abs=0.01)


def test_sweep_zero_gives_positive_curve() -> None:
    x1, y1, arc = _arc("M 1 0 A 1 1 0 0 0 0 -1")
    assert arc_curve_deg(x1, y1, arc) == pytest.approx(90.0, abs=1e-6)


def test_large_arc_flag() -> None:
    x1, y1, small = _arc("M 1 0 A 1 1 0 0 1 0 1")
    _, _, large = _arc("M 1 0 A 1 1 0 1 1 0 1")
    assert abs(arc_curve_deg(x1, y1, small)) == pytest.approx(90.0, abs=1e-6)
    assert abs(arc_curve_deg(x1, y1, large)) == pytest.approx(270.0, abs=1e-6)


def test_radius_too_small_is_scaled_to_half_circle() -> None:
    x1, y1, arc = _arc("M 0 0 A 0.1 0.1 0 0 1 2 0")
    assert abs(arc_curve_deg(x1, y1, arc)) == pytest.approx(180.0, abs=1e-6)


def test_degenerate_arcs() -> None:
    x1, y1, zero_r = _arc("M 0 0 A 0 0 0 0 1 1 1")
    assert arc_to_center(x1, y1, zero_r) is None
    assert arc_curve_deg(x1, y1, zero_r) == 0.0
    x1, y1, same = _arc("M 1 1 A 1 1 0 0 1 1 1")
    assert arc_curve_deg(x1, y1, same) == 0.0


def test_flatten_arc_stays_on_circle_and_ends_exactly() -> None:
    x1, y1, arc = _arc("M 1 0 A 1 1 0 0 1 -1 0")
    pts = flatten_arc(x1, y1, arc, step_deg=10.0)
    assert pts[-1] == (-1.0, 0.0)
    for px, py in pts:
        assert math.hypot(px, py) == pytest.approx(1.0, abs=1e-9)
    # sweep=1 in a Y-down frame goes through (0, 1) (visually clockwise from +x).
    assert any(py > 0.9 for _, py in pts)


def test_flatten_path_closed_rectangle_with_curves() -> None:
    subs = flatten_path(parse_path("M 0 0 L 4 0 L 4 3 Q 4 4 3 4 C 2 4 1 4 0 3 Z"))
    assert len(subs) == 1
    assert subs[0].closed
    assert subs[0].points[0] == (0.0, 0.0)
    assert subs[0].points[-1] == (0.0, 3.0)


def test_flatten_path_multiple_subpaths() -> None:
    subs = flatten_path(parse_path("M 0 0 L 1 0 M 5 5 L 6 5 L 6 6 Z"))
    assert [s.closed for s in subs] == [False, True]
