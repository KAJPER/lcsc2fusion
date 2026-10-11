"""Tests for lcsc2fusion.render: geometry helpers (pure) and image output (needs matplotlib)."""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

from lcsc2fusion.constants import Layer
from lcsc2fusion.model import (
    Circle,
    Hole,
    Label,
    Package,
    Pad,
    PadShape,
    Part,
    Pin,
    PinDirection,
    PinLength,
    Point,
    Polygon,
    Rectangle,
    Symbol,
    TextAlign,
    Wire,
)
from lcsc2fusion.render import (
    RenderError,
    arc_geometry,
    arc_points,
    drill_polygon,
    ellipse_polygon,
    pad_polygon,
    rect_polygon,
    render_package,
    render_part,
    render_symbol,
    stadium_polygon,
)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _angle_diff(a: float, b: float) -> float:
    """Smallest absolute difference between two angles in degrees."""
    return abs((a - b + 180.0) % 360.0 - 180.0)


def _bbox(points: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


# ---------------------------------------------------------------------------------------------
# arc_geometry
# ---------------------------------------------------------------------------------------------


def test_arc_quarter_circle_ccw_is_around_origin() -> None:
    cx, cy, r, t1, t2 = arc_geometry(1.0, 0.0, 0.0, 1.0, 90.0)
    assert (cx, cy) == pytest.approx((0.0, 0.0), abs=1e-12)
    assert r == pytest.approx(1.0)
    assert _angle_diff(t1, 0.0) == pytest.approx(0.0, abs=1e-9)
    assert t2 - t1 == pytest.approx(90.0)


def test_arc_quarter_circle_cw_is_around_one_one() -> None:
    cx, cy, r, t1, t2 = arc_geometry(1.0, 0.0, 0.0, 1.0, -90.0)
    assert (cx, cy) == pytest.approx((1.0, 1.0), abs=1e-12)
    assert r == pytest.approx(1.0)
    # CCW span from p2 (180 deg about the centre) to p1 (270 deg).
    assert t1 == pytest.approx(180.0)
    assert t2 == pytest.approx(270.0)


def test_arc_semicircle_both_directions_share_the_chord_midpoint() -> None:
    for curve in (180.0, -180.0):
        cx, cy, r, t1, t2 = arc_geometry(1.0, 0.0, 0.0, 1.0, curve)
        assert (cx, cy) == pytest.approx((0.5, 0.5), abs=1e-12)
        assert r == pytest.approx(math.sqrt(2.0) / 2.0)
        assert t2 - t1 == pytest.approx(180.0)
    # CCW half circle starts at p1 (315 deg), CW half circle starts at p2 (135 deg).
    assert arc_geometry(1.0, 0.0, 0.0, 1.0, 180.0)[3] == pytest.approx(315.0)
    assert arc_geometry(1.0, 0.0, 0.0, 1.0, -180.0)[3] == pytest.approx(135.0)


def test_arc_270_degrees_centre_is_on_the_right_of_the_chord() -> None:
    cx, cy, r, t1, t2 = arc_geometry(1.0, 0.0, 0.0, 1.0, 270.0)
    assert (cx, cy) == pytest.approx((1.0, 1.0), abs=1e-12)
    assert r == pytest.approx(1.0)
    assert t1 == pytest.approx(270.0)
    assert t2 == pytest.approx(540.0)


def test_arc_minus_270_degrees_mirrors_the_270_case() -> None:
    cx, cy, r, t1, t2 = arc_geometry(1.0, 0.0, 0.0, 1.0, -270.0)
    assert (cx, cy) == pytest.approx((0.0, 0.0), abs=1e-12)
    assert r == pytest.approx(1.0)
    assert t2 - t1 == pytest.approx(270.0)
    # p2 = (0, 1) sits at 90 deg and is the CCW span start for a CW wire.
    assert t1 == pytest.approx(90.0)


def test_arc_tiny_curve_has_huge_radius_and_still_passes_through_both_ends() -> None:
    curve = 0.01
    x1, y1, x2, y2 = 0.0, 0.0, 2.0, 0.0
    cx, cy, r, t1, t2 = arc_geometry(x1, y1, x2, y2, curve)
    assert r == pytest.approx(2.0 / (2.0 * math.sin(math.radians(curve) / 2.0)))
    assert r > 1e4
    assert t2 - t1 == pytest.approx(curve)
    # Positive curve: p1 is at theta1, p2 at theta2.
    p1 = (cx + r * math.cos(math.radians(t1)), cy + r * math.sin(math.radians(t1)))
    p2 = (cx + r * math.cos(math.radians(t2)), cy + r * math.sin(math.radians(t2)))
    assert p1 == pytest.approx((x1, y1), abs=1e-6)
    assert p2 == pytest.approx((x2, y2), abs=1e-6)
    # Centre lies to the left (+y) of the chord direction (+x) for a CCW arc.
    assert cy > 0.0


@pytest.mark.parametrize("curve", [5.0, 45.0, 90.0, 179.0, 200.0, 270.0, 359.0])
@pytest.mark.parametrize("sign", [1.0, -1.0])
def test_arc_geometry_reproduces_endpoints(curve: float, sign: float) -> None:
    c = curve * sign
    x1, y1, x2, y2 = 1.5, -0.7, -2.0, 3.1
    cx, cy, r, t1, t2 = arc_geometry(x1, y1, x2, y2, c)
    # Wire start is at theta1 for CCW arcs and at theta2 for CW arcs.
    a_start, a_end = (t1, t2) if c > 0 else (t2, t1)
    start = (cx + r * math.cos(math.radians(a_start)), cy + r * math.sin(math.radians(a_start)))
    end = (cx + r * math.cos(math.radians(a_end)), cy + r * math.sin(math.radians(a_end)))
    assert start == pytest.approx((x1, y1), abs=1e-9)
    assert end == pytest.approx((x2, y2), abs=1e-9)
    assert 0.0 <= t1 < 360.0
    assert t2 - t1 == pytest.approx(abs(c))


@pytest.mark.parametrize(
    ("coords", "curve"),
    [((0.0, 0.0, 0.0, 0.0), 90.0), ((0.0, 0.0, 1.0, 0.0), 0.0), ((0.0, 0.0, 1.0, 0.0), 360.0)],
)
def test_arc_geometry_rejects_degenerate_input(
    coords: tuple[float, float, float, float], curve: float
) -> None:
    with pytest.raises(ValueError, match=r"arc|curve"):
        arc_geometry(*coords, curve)


def test_arc_points_follow_the_arc_with_exact_endpoints() -> None:
    pts = arc_points(1.0, 0.0, 0.0, 1.0, 90.0)
    assert pts[0] == (1.0, 0.0)
    assert pts[-1] == (0.0, 1.0)
    assert len(pts) >= 31
    assert all(math.hypot(x, y) == pytest.approx(1.0, abs=1e-9) for x, y in pts)
    # Positive curve bulges to the right of p1 -> p2, i.e. towards (+x, +y) here.
    mid = pts[len(pts) // 2]
    assert mid == pytest.approx((math.sqrt(0.5), math.sqrt(0.5)), abs=1e-3)


def test_arc_points_negative_curve_bulges_to_the_left() -> None:
    pts = arc_points(1.0, 0.0, 0.0, 1.0, -90.0)
    assert pts[0] == (1.0, 0.0)
    assert pts[-1] == (0.0, 1.0)
    assert all(math.hypot(x - 1.0, y - 1.0) == pytest.approx(1.0, abs=1e-9) for x, y in pts)
    mid = pts[len(pts) // 2]
    assert mid == pytest.approx((1.0 - math.sqrt(0.5), 1.0 - math.sqrt(0.5)), abs=1e-3)


def test_arc_points_straight_and_degenerate_cases() -> None:
    assert arc_points(0.0, 0.0, 1.0, 0.0, 0.0) == [(0.0, 0.0), (1.0, 0.0)]
    assert arc_points(2.0, 2.0, 2.0, 2.0, 90.0) == [(2.0, 2.0), (2.0, 2.0)]


# ---------------------------------------------------------------------------------------------
# Shape helpers
# ---------------------------------------------------------------------------------------------


def test_rect_polygon_rotation() -> None:
    assert _bbox(rect_polygon(1.0, 2.0, 4.0, 2.0, 0.0)) == pytest.approx((-1.0, 1.0, 3.0, 3.0))
    assert _bbox(rect_polygon(1.0, 2.0, 4.0, 2.0, 90.0)) == pytest.approx((0.0, 0.0, 2.0, 4.0))
    side = math.sqrt(2.0)
    w45 = _bbox(rect_polygon(0.0, 0.0, 2.0, 2.0, 45.0))
    assert w45 == pytest.approx((-side, -side, side, side))


def test_stadium_polygon_extents_and_axis() -> None:
    bb = _bbox(stadium_polygon(0.0, 0.0, 3.0, 1.0, 0.0))
    assert bb == pytest.approx((-1.5, -0.5, 1.5, 0.5), abs=1e-9)
    bb90 = _bbox(stadium_polygon(0.0, 0.0, 3.0, 1.0, 90.0))
    assert bb90 == pytest.approx((-0.5, -1.5, 0.5, 1.5), abs=1e-9)
    # length <= thickness degenerates to a circle of the thickness.
    circle = _bbox(stadium_polygon(0.0, 0.0, 0.5, 1.0, 0.0))
    assert circle == pytest.approx((-0.5, -0.5, 0.5, 0.5), abs=1e-9)


def test_ellipse_polygon_radius() -> None:
    pts = ellipse_polygon(1.0, -1.0, 2.0, 2.0)
    assert all(math.hypot(x - 1.0, y + 1.0) == pytest.approx(1.0) for x, y in pts)


def _pad(**kw: object) -> Pad:
    base: dict[str, object] = {
        "name": "1",
        "number": "1",
        "x": 0.0,
        "y": 0.0,
        "width": 1.0,
        "height": 0.6,
        "shape": PadShape.RECT,
    }
    base.update(kw)
    return Pad(**base)  # type: ignore[arg-type]


def test_pad_polygon_shapes() -> None:
    assert _bbox(pad_polygon(_pad(x=2.0, y=1.0))) == pytest.approx((1.5, 0.7, 2.5, 1.3))
    # Rotated 90 degrees swaps the extents.
    assert _bbox(pad_polygon(_pad(rotation=90.0))) == pytest.approx((-0.3, -0.5, 0.3, 0.5))
    # OVAL longer in y is a stadium along y.
    tall = _bbox(pad_polygon(_pad(shape=PadShape.OVAL, width=0.6, height=1.4)))
    assert tall == pytest.approx((-0.3, -0.7, 0.3, 0.7), abs=1e-9)
    # ROUND uses the full width and height.
    rnd = _bbox(pad_polygon(_pad(shape=PadShape.ROUND, width=1.2, height=1.2)))
    assert rnd == pytest.approx((-0.6, -0.6, 0.6, 0.6), abs=1e-9)


def test_pad_polygon_uses_outline_for_polygon_pads() -> None:
    outline = (Point(0.0, 0.0), Point(2.0, 0.0), Point(1.0, 1.5))
    pad = _pad(shape=PadShape.POLYGON, outline=outline)
    assert pad_polygon(pad) == [(0.0, 0.0), (2.0, 0.0), (1.0, 1.5)]
    # Without an outline it falls back to the rectangle.
    assert len(pad_polygon(_pad(shape=PadShape.POLYGON))) == 4


def test_drill_polygon_round_and_slot() -> None:
    round_pad = _pad(shape=PadShape.ROUND, width=1.6, height=1.6, drill=0.8)
    assert _bbox(drill_polygon(round_pad)) == pytest.approx((-0.4, -0.4, 0.4, 0.4), abs=1e-9)
    slot_pad = _pad(
        shape=PadShape.OVAL, width=2.4, height=1.6, drill=0.8, slot_length=1.8, slot_angle=90.0
    )
    assert _bbox(drill_polygon(slot_pad)) == pytest.approx((-0.4, -0.9, 0.4, 0.9), abs=1e-9)


# ---------------------------------------------------------------------------------------------
# Rendering (needs matplotlib)
# ---------------------------------------------------------------------------------------------


def _sample_package() -> Package:
    pads = (
        _pad(name="1", number="1", x=-3.0, y=1.5, width=1.0, height=0.6),
        _pad(name="2", number="2", x=-1.5, y=1.5, width=1.2, height=0.6, shape=PadShape.OVAL),
        _pad(name="3", number="3", x=0.0, y=1.5, width=0.9, height=0.9, shape=PadShape.ROUND),
        _pad(name="4", number="4", x=1.5, y=1.5, width=1.4, height=0.5, rotation=45.0),
        _pad(
            name="5",
            number="5",
            x=3.0,
            y=1.5,
            shape=PadShape.POLYGON,
            outline=(Point(2.6, 1.1), Point(3.6, 1.1), Point(3.1, 1.9), Point(2.7, 1.9)),
        ),
        _pad(name="6", number="6", x=-3.0, y=-1.5, layer=Layer.BOTTOM),
        _pad(
            name="7",
            number="7",
            x=-1.0,
            y=-1.5,
            width=1.7,
            height=1.7,
            shape=PadShape.ROUND,
            drill=0.9,
        ),
        _pad(
            name="8",
            number="8",
            x=1.5,
            y=-1.5,
            width=2.6,
            height=1.6,
            shape=PadShape.OVAL,
            drill=0.8,
            slot_length=1.8,
            slot_angle=0.0,
        ),
    )
    graphics = (
        Wire(-4.0, -3.0, 4.0, -3.0, 0.15, Layer.T_PLACE),
        Wire(4.0, -3.0, 4.0, 3.0, 0.15, Layer.T_PLACE, curve=90.0),
        Wire(-4.0, 3.0, 0.0, 3.0, 0.15, Layer.T_PLACE, curve=-270.0),
        Wire(-4.0, -3.0, -4.0, 0.0, 0.1, Layer.T_DOCU),
        Wire(-4.5, 3.5, 4.5, 3.5, 0.05, Layer.DIMENSION),
        Wire(2.0, -3.4, 3.0, -3.4, 0.1, Layer.MILLING),
        Rectangle(-3.9, -2.9, 3.9, 2.9, Layer.T_KEEPOUT),
        Rectangle(0.0, -0.4, 1.0, 0.4, Layer.T_DOCU, rotation=30.0),
        Polygon((Point(2.0, -0.5), Point(3.0, -0.5), Point(2.5, 0.4)), 0.1, Layer.T_PLACE),
        Circle(-3.5, 0.2, 0.3, 0.0, Layer.T_PLACE),
        Circle(3.5, 0.0, 0.4, 0.1, Layer.T_DOCU),
        Label(-4.0, -3.8, ">NAME", 0.8, Layer.T_NAMES),
        Label(0.0, 4.0, ">VALUE", 0.8, Layer.T_VALUES, rotation=0.0, align=TextAlign.CENTER),
        Label(4.8, 0.0, "rot", 0.6, Layer.T_PLACE, rotation=90.0, align=TextAlign.TOP_RIGHT),
    )
    return Package(
        name="TEST-PKG",
        description="synthetic",
        pads=pads,
        holes=(Hole(0.0, -3.0, 1.2),),
        graphics=graphics,
    )


def _sample_symbol() -> Symbol:
    pins = (
        Pin("IN", "1", -10.16, 5.08, PinLength.LONG, 0, PinDirection.IN),
        Pin("CLK", "2", -10.16, 0.0, PinLength.MIDDLE, 0, PinDirection.IN, clock=True),
        Pin("NRST", "3", -10.16, -5.08, PinLength.SHORT, 0, PinDirection.IN, inverted=True),
        Pin("OUT", "4", 10.16, 5.08, PinLength.LONG, 180, PinDirection.OUT),
        Pin("IO", "5", 10.16, 0.0, PinLength.SHORT, 180, PinDirection.IO),
        Pin("VCC", "6", 0.0, 10.16, PinLength.SHORT, 270, PinDirection.PWR),
        Pin("GND", "7", 0.0, -10.16, PinLength.SHORT, 90, PinDirection.PWR),
        Pin("NC", "8", 10.16, -5.08, PinLength.POINT, 180, PinDirection.NC),
        Pin("PAS", "9", 5.08, 10.16, PinLength.SHORT, 270, PinDirection.PAS),
    )
    graphics = (
        Rectangle(-7.62, -7.62, 7.62, 7.62, Layer.SYMBOLS),
        Wire(-7.62, 7.62, 7.62, 7.62, 0.254, Layer.SYMBOLS),
        Label(-7.62, 8.0, ">NAME", 1.778, Layer.NAMES),
        Label(-7.62, -9.5, ">VALUE", 1.778, Layer.VALUES),
    )
    return Symbol(name="TEST-SYM", pins=pins, graphics=graphics)


def _sample_part() -> Part:
    sym = _sample_symbol()
    return Part(
        lcsc_id="C99999",
        mpn="TEST-MPN",
        manufacturer="ACME",
        description="test part",
        prefix="U",
        symbols=(sym, Symbol(name="UNIT-B", pins=sym.pins[:2], graphics=sym.graphics)),
        package=_sample_package(),
    )


def _assert_png(path: Path) -> None:
    data = path.read_bytes()
    assert data[:8] == PNG_SIGNATURE
    assert len(data) > 5000


def _assert_svg(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert text.lstrip().startswith("<?xml")
    assert "<svg" in text
    assert len(text) > 5000


class TestRendering:
    @pytest.fixture(autouse=True)
    def _need_matplotlib(self) -> None:
        pytest.importorskip("matplotlib")

    def test_render_package_png(self, tmp_path: Path) -> None:
        out = render_package(_sample_package(), tmp_path / "pkg.png")
        assert out == tmp_path / "pkg.png"
        _assert_png(out)

    def test_render_package_svg(self, tmp_path: Path) -> None:
        out = render_package(_sample_package(), str(tmp_path / "pkg.svg"), title="custom title")
        assert isinstance(out, Path)
        _assert_svg(out)
        assert "custom title" in out.read_text(encoding="utf-8")

    def test_render_package_png_has_red_and_blue_pads(self, tmp_path: Path) -> None:
        import matplotlib.image as mpimg

        out = render_package(_sample_package(), tmp_path / "pkg.png", dpi=100)
        img = mpimg.imread(out)  # float RGB(A) in 0..1
        r, g, b = img[..., 0], img[..., 1], img[..., 2]
        red = (r > 0.55) & (g < 0.35) & (b < 0.35)
        blue = (b > 0.55) & (r < 0.35) & (g < 0.6) & (b - r > 0.35)
        gold = (r > 0.6) & (g > 0.5) & (b < 0.35) & (r - b > 0.4)
        assert int(red.sum()) > 200
        assert int(blue.sum()) > 100
        assert int(gold.sum()) > 200

    def test_render_package_creates_missing_directories(self, tmp_path: Path) -> None:
        out = render_package(_sample_package(), tmp_path / "a" / "b" / "pkg.png")
        _assert_png(out)

    def test_render_package_minimal_and_empty(self, tmp_path: Path) -> None:
        single = Package(name="ONE", description="", pads=(_pad(),))
        _assert_png(render_package(single, tmp_path / "one.png"))
        empty = Package(name="EMPTY", description="", pads=())
        _assert_png(render_package(empty, tmp_path / "empty.png"))

    def test_render_package_degenerate_graphics(self, tmp_path: Path) -> None:
        pkg = Package(
            name="DEGEN",
            description="",
            pads=(_pad(),),
            graphics=(
                Wire(0.0, 0.0, 0.0, 0.0, 0.1, Layer.T_PLACE, curve=90.0),
                Circle(0.0, 0.0, 0.0, 0.2, Layer.T_PLACE),
                # A layer without a table entry falls back to the default style.
                Wire(0.0, 0.0, 1.0, 0.0, 0.1, Layer.T_RESTRICT),
            ),
        )
        _assert_png(render_package(pkg, tmp_path / "degen.png"))

    def test_render_symbol_png_and_svg(self, tmp_path: Path) -> None:
        _assert_png(render_symbol(_sample_symbol(), tmp_path / "sym.png"))
        _assert_svg(render_symbol(_sample_symbol(), tmp_path / "sym.svg", title="sym title"))

    def test_render_symbol_without_pins_or_graphics(self, tmp_path: Path) -> None:
        _assert_png(render_symbol(Symbol(name="EMPTY", pins=()), tmp_path / "empty.png"))

    def test_render_part_writes_footprint_and_every_symbol(self, tmp_path: Path) -> None:
        paths = render_part(_sample_part(), tmp_path / "out")
        names = [p.name for p in paths]
        assert names == ["C99999_footprint.png", "C99999_symbol_1.png", "C99999_symbol_2.png"]
        for p in paths:
            assert p.parent == tmp_path / "out"
            _assert_png(p)

    def test_render_part_multiple_formats(self, tmp_path: Path) -> None:
        paths = render_part(_sample_part(), tmp_path, formats=("png", ".svg", "PNG"))
        names = sorted(p.name for p in paths)
        assert names == sorted(
            [
                "C99999_footprint.png",
                "C99999_symbol_1.png",
                "C99999_symbol_2.png",
                "C99999_footprint.svg",
                "C99999_symbol_1.svg",
                "C99999_symbol_2.svg",
            ]
        )
        for p in paths:
            (_assert_png if p.suffix == ".png" else _assert_svg)(p)

    def test_unsupported_extension_raises(self, tmp_path: Path) -> None:
        with pytest.raises(RenderError, match="Unsupported output format"):
            render_package(_sample_package(), tmp_path / "pkg.jpg")
        with pytest.raises(RenderError, match="Unsupported output format"):
            render_symbol(_sample_symbol(), tmp_path / "noext")
        with pytest.raises(RenderError, match="Unsupported output format"):
            render_part(_sample_part(), tmp_path, formats=("pdf",))

    def test_bad_dpi_raises(self, tmp_path: Path) -> None:
        with pytest.raises(RenderError, match="dpi"):
            render_package(_sample_package(), tmp_path / "pkg.png", dpi=0)

    def test_unwritable_target_raises_render_error(self, tmp_path: Path) -> None:
        blocker = tmp_path / "file"
        blocker.write_text("x")
        with pytest.raises(RenderError, match="Cannot write"):
            render_package(_sample_package(), blocker / "pkg.png")


def test_missing_matplotlib_gives_clear_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A None entry in sys.modules makes `import matplotlib` raise ImportError.
    monkeypatch.setitem(sys.modules, "matplotlib", None)
    with pytest.raises(RenderError, match="matplotlib"):
        render_package(_sample_package(), tmp_path / "pkg.png")
    with pytest.raises(RenderError, match="matplotlib"):
        render_symbol(_sample_symbol(), tmp_path / "sym.png")
    with pytest.raises(RenderError, match="matplotlib"):
        render_part(_sample_part(), tmp_path)


def test_render_error_is_an_lcsc_error() -> None:
    from lcsc2fusion.errors import LcscError

    assert issubclass(RenderError, LcscError)
