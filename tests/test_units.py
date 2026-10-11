"""Unit and axis conversion, including the evidence for the EasyEDA rotation sign.

Rotation sign finding: positive EasyEDA rotation is counter-clockwise on screen. The `points`
field of a RECT pad holds its absolute corner vertices (rotation included), listed as top-left,
top-right, bottom-right, bottom-left of the unrotated pad and then rotated together with it. For
pads rotated by 90 and 270 degrees (width != height) the first listed vertex lands exactly where a
counter-clockwise screen rotation sends the top-left corner, and nowhere near where a clockwise
rotation would send it. The real data only contains multiples of 90 degrees, so oblique angles
rest on this convention being a rotation of the pad about its centre.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from lcsc2fusion.constants import EE_UNIT_MM
from lcsc2fusion.model import Point
from lcsc2fusion.parser.units import MM_DECIMALS, Transform, ee_rotation_to_ccw, ee_to_mm

REAL_DIR = Path(__file__).parent / "fixtures" / "real"
TOL = 1e-6


def rotate_ccw_on_screen(dx: float, dy: float, degrees: float) -> tuple[float, float]:
    """Rotate an offset given in the EasyEDA frame (Y down) counter-clockwise as seen on screen.

    Hand derived: on a Y-down screen a counter-clockwise turn by r maps the +x axis (1, 0) to
    (cos r, -sin r) and the +y axis (0, 1) to (sin r, cos r).
    """
    r = math.radians(degrees)
    return dx * math.cos(r) + dy * math.sin(r), -dx * math.sin(r) + dy * math.cos(r)


def rect_corner_offsets(w: float, h: float) -> list[tuple[float, float]]:
    """Unrotated corner order used by EasyEDA: top-left, top-right, bottom-right, bottom-left."""
    return [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]


def ee_outline(
    cx: float, cy: float, w: float, h: float, rotation: float
) -> list[tuple[float, float]]:
    """Absolute EasyEDA outline of a pad built from first principles."""
    return [
        (cx + rx, cy + ry)
        for rx, ry in (
            rotate_ccw_on_screen(dx, dy, rotation) for dx, dy in rect_corner_offsets(w, h)
        )
    ]


def model_outline(
    tf: Transform, cx: float, cy: float, w: float, h: float, rotation: float
) -> list[Point]:
    """The same corners as the internal model describes them: Y up mm, rotation via the helper."""
    centre = tf.point(cx, cy)
    width, height = tf.length(w), tf.length(h)
    phi = math.radians(ee_rotation_to_ccw(rotation))  # counter-clockwise in Y up space
    # Local corners in the Y-up frame: the EasyEDA top-left corner is (-w/2, +h/2) there.
    local = [
        (-width / 2, height / 2),
        (width / 2, height / 2),
        (width / 2, -height / 2),
        (-width / 2, -height / 2),
    ]
    return [
        Point(
            centre.x + lx * math.cos(phi) - ly * math.sin(phi),
            centre.y + lx * math.sin(phi) + ly * math.cos(phi),
        )
        for lx, ly in local
    ]


# Transform and plain conversions


def test_ee_to_mm_scale() -> None:
    assert ee_to_mm(1.0) == pytest.approx(EE_UNIT_MM)
    assert ee_to_mm(10.0) == pytest.approx(2.54)
    assert ee_to_mm(0.0) == 0.0
    assert ee_to_mm(-5.0) == pytest.approx(-1.27)


def test_transform_origin_maps_to_zero() -> None:
    tf = Transform(4000.0, 3000.0)
    p = tf.point(4000.0, 3000.0)
    assert (p.x, p.y) == (0.0, 0.0)


def test_transform_shift_and_y_flip() -> None:
    tf = Transform(100.0, 200.0)
    right_down = tf.point(110.0, 210.0)  # right and down on screen
    assert right_down.x == pytest.approx(2.54)
    assert right_down.y == pytest.approx(-2.54)  # down on screen is negative Y in Eagle
    left_up = tf.point(95.0, 190.0)
    assert left_up.x == pytest.approx(-1.27)
    assert left_up.y == pytest.approx(2.54)


def test_transform_ignores_absolute_position() -> None:
    a = Transform(0.0, 0.0).point(10.0, 20.0)
    b = Transform(4000.0, -574.0).point(4010.0, -554.0)
    assert (a.x, a.y) == (b.x, b.y)


def test_transform_length_has_no_axis_or_origin() -> None:
    tf = Transform(123.0, 456.0)
    assert tf.length(10.0) == pytest.approx(2.54)
    assert tf.length(-10.0) == pytest.approx(-2.54)
    assert tf.length(0.0) == 0.0


def test_transform_rounds_to_micrometres() -> None:
    tf = Transform(0.0, 0.0)
    assert tf.length(1.234567891) == round(1.234567891 * EE_UNIT_MM, MM_DECIMALS)
    assert tf.point(1.234567891, 0.0).x == round(1.234567891 * EE_UNIT_MM, MM_DECIMALS)


@pytest.mark.parametrize(
    ("ee", "ccw"),
    [(0, 0), (90, 90), (180, 180), (270, 270), (360, 0), (450, 90), (-90, 270), (-450, 270)],
)
def test_rotation_is_not_mirrored(ee: float, ccw: float) -> None:
    assert ee_rotation_to_ccw(ee) == pytest.approx(ccw)


def test_rotation_range() -> None:
    for angle in (-725.0, -1.0, 0.0, 0.5, 359.999, 360.0, 1080.5):
        assert 0.0 <= ee_rotation_to_ccw(angle) < 360.0


# Rotation sign evidence on a hand made pad


def test_hand_made_pad_rotated_90_matches_ccw_convention() -> None:
    """Pad 20 x 6 units at (100, 200) rotated 90: vertices written out by hand."""
    expected = [(97.0, 210.0), (97.0, 190.0), (103.0, 190.0), (103.0, 210.0)]
    for got, want in zip(ee_outline(100.0, 200.0, 20.0, 6.0, 90.0), expected, strict=True):
        assert got == pytest.approx(want, abs=TOL)


def test_hand_made_pad_rotated_270_matches_ccw_convention() -> None:
    expected = [(103.0, 190.0), (103.0, 210.0), (97.0, 210.0), (97.0, 190.0)]
    for got, want in zip(ee_outline(100.0, 200.0, 20.0, 6.0, 270.0), expected, strict=True):
        assert got == pytest.approx(want, abs=TOL)


@pytest.mark.parametrize("rotation", [0.0, 30.0, 45.0, 90.0, 135.0, 180.0, 225.0, 270.0, 330.0])
def test_model_rotation_reproduces_ee_outline(rotation: float) -> None:
    """The internal Y-up pad rotated by ee_rotation_to_ccw covers the same corners as the
    EasyEDA outline (after Transform), so rotating the pad in Eagle looks like on EasyEDA."""
    tf = Transform(100.0, 200.0)
    cx, cy, w, h = 130.0, 180.0, 20.0, 6.0
    from_ee = [tf.point(x, y) for x, y in ee_outline(cx, cy, w, h, rotation)]
    from_model = model_outline(tf, cx, cy, w, h, rotation)
    for a, b in zip(from_ee, from_model, strict=True):
        assert a.x == pytest.approx(b.x, abs=1e-4)
        assert a.y == pytest.approx(b.y, abs=1e-4)


def test_opposite_sign_would_differ_at_90_degrees() -> None:
    """Guards the discrimination: a clockwise convention gives different vertex positions."""
    ccw = ee_outline(100.0, 200.0, 20.0, 6.0, 90.0)
    cw = ee_outline(100.0, 200.0, 20.0, 6.0, -90.0)
    assert ccw[0] != pytest.approx(cw[0], abs=1e-3)


# Evidence on real pads (local fixtures only)


def _real_rect_pads() -> list[tuple[str, float, float, float, float, float, list[float]]]:
    pads = []
    for path in sorted(REAL_DIR.glob("*.json")):
        detail = json.loads(path.read_text(encoding="utf-8"))["result"].get("packageDetail")
        if not detail:
            continue
        for raw in detail["dataStr"]["shape"]:
            f = raw.split("~")
            if f[0] != "PAD" or f[1] != "RECT" or float(f[11] or 0) == 0.0:
                continue
            pts = [float(v) for v in f[10].replace(",", " ").split()]
            pads.append((path.name, *(float(v) for v in f[2:6]), float(f[11]), pts))
    return pads


@pytest.mark.real
def test_real_pads_follow_ccw_convention() -> None:
    pads = _real_rect_pads()
    if not pads:
        pytest.skip("no real fixtures in tests/fixtures/real")
    discriminating = 0
    for name, cx, cy, w, h, rotation, pts in pads:
        assert len(pts) == 8, name
        got = list(zip(pts[0::2], pts[1::2], strict=True))
        want = ee_outline(cx, cy, w, h, rotation)
        for g, e in zip(got, want, strict=True):
            assert g == pytest.approx(e, abs=2e-3), f"{name} pad rotated {rotation}"
        if abs(w - h) > 1.0 and rotation % 180.0 != 0.0:
            discriminating += 1
            opposite = ee_outline(cx, cy, w, h, -rotation)
            assert got[0] != pytest.approx(opposite[0], abs=1e-2), name
    assert discriminating > 0, "no real pad distinguishes clockwise from counter-clockwise"
