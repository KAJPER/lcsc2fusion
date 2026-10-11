"""Matplotlib renderer for the internal model, used for visual verification.

Footprints (`render_package`) and symbols (`render_symbol`) are drawn in millimetres, Y axis up,
equal aspect, so the picture can be compared with the EasyEDA preview. Output format follows the
file extension (.png or .svg).

matplotlib is a dev-only dependency: it is imported lazily, so importing this module always works
and a missing matplotlib surfaces as `RenderError` only when a render is requested. The figure is
built directly on `matplotlib.figure.Figure` with the Agg canvas (no pyplot, no GUI backend).

The geometry helpers at the top of the module are pure Python and independent of matplotlib.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from lcsc2fusion.constants import (
    COURTYARD_WIDTH_MM,
    PIN_LENGTH_LONG_MM,
    PIN_LENGTH_MIDDLE_MM,
    PIN_LENGTH_POINT_MM,
    PIN_LENGTH_SHORT_MM,
    SYMBOL_WIRE_WIDTH_MM,
    Layer,
)
from lcsc2fusion.errors import LcscError
from lcsc2fusion.model import (
    Circle,
    Graphic,
    Hole,
    Label,
    Package,
    Pad,
    PadShape,
    Part,
    Pin,
    PinDirection,
    PinLength,
    Polygon,
    Rectangle,
    Symbol,
    TextAlign,
    Wire,
)

if TYPE_CHECKING:
    from matplotlib.artist import Artist
    from matplotlib.axes import Axes
    from matplotlib.figure import Figure

__all__ = [
    "RenderError",
    "arc_geometry",
    "arc_points",
    "render_package",
    "render_part",
    "render_symbol",
]

XY = tuple[float, float]


class RenderError(LcscError):
    """Rendering was requested but is impossible (matplotlib missing, bad format, write error)."""


# ---------------------------------------------------------------------------------------------
# Tables (all style constants live here)
# ---------------------------------------------------------------------------------------------

SUPPORTED_FORMATS = ("png", "svg")

_BG = "#0b0d10"
_FG = "#d0d7de"
_GRID = "#ffffff"
_GRID_ALPHA = 0.10
_AXIS_COLOR = "#6e7681"
_LEGEND_BG = "#161b22"

_COLOR_PAD_TOP = "#d9423a"
_COLOR_PAD_BOTTOM = "#3b82f6"
_COLOR_PAD_THT = "#d4af37"
_COLOR_HOLE_FILL = "#ffffff"
_COLOR_HOLE_RIM = "#2b2b2b"
_COLOR_NPTH_RIM = "#7d8590"
_PAD_ALPHA = 0.85
_TEXT_DARK = "#111111"
_TEXT_LIGHT = "#ffffff"
_TEXT_LUMINANCE_SPLIT = 0.5

_MIN_LINE_PT = 0.6
_DASH_PATTERN = (0, (5.0, 3.0))
_ARC_STEP_DEG = 3.0
_ELLIPSE_SEGMENTS = 64
_CAP_SEGMENTS = 16
_TEXT_ADVANCE_EM = 0.62  # average glyph advance in em, used for fitting and bounds estimates
_FIG_LONG_SIDE_IN = 7.5
_FIG_RATIO_MIN = 0.3
_FIG_RATIO_MAX = 3.3
_BOUNDS_PAD_FRACTION = 0.08
_BOUNDS_PAD_MIN_MM = 0.6

# Symbol pin drawing (mm).
_PIN_NAME_SIZE_MM = 1.27
_PIN_NUMBER_SIZE_MM = 0.9
_PIN_NAME_GAP_MM = 0.6
_PIN_NUMBER_GAP_MM = 0.4
_PIN_DOT_RADIUS_MM = 0.5
_PIN_CLOCK_HALF_BASE_MM = 0.7
_PIN_CLOCK_HEIGHT_MM = 1.0
_PIN_MARKER_RADIUS_MM = 0.3
_SYMBOL_GRID_MM = 2.54

_PIN_LENGTH_MM: dict[PinLength, float] = {
    PinLength.POINT: PIN_LENGTH_POINT_MM,
    PinLength.SHORT: PIN_LENGTH_SHORT_MM,
    PinLength.MIDDLE: PIN_LENGTH_MIDDLE_MM,
    PinLength.LONG: PIN_LENGTH_LONG_MM,
}

_PIN_DIRECTION_COLOR: dict[PinDirection, str] = {
    PinDirection.IN: "#58a6ff",
    PinDirection.OUT: "#ff8c42",
    PinDirection.IO: "#c792ea",
    PinDirection.PWR: "#ff5555",
    PinDirection.PAS: "#3fb950",
    PinDirection.NC: "#8b949e",
    PinDirection.OC: "#e3b341",
    PinDirection.HIZ: "#39c5cf",
    PinDirection.SUP: "#ff79c6",
}

_TEXT_ALIGN: dict[TextAlign, tuple[str, str]] = {
    TextAlign.BOTTOM_LEFT: ("left", "bottom"),
    TextAlign.BOTTOM_CENTER: ("center", "bottom"),
    TextAlign.BOTTOM_RIGHT: ("right", "bottom"),
    TextAlign.CENTER_LEFT: ("left", "center"),
    TextAlign.CENTER: ("center", "center"),
    TextAlign.CENTER_RIGHT: ("right", "center"),
    TextAlign.TOP_LEFT: ("left", "top"),
    TextAlign.TOP_CENTER: ("center", "top"),
    TextAlign.TOP_RIGHT: ("right", "top"),
}


@dataclass(frozen=True)
class LayerStyle:
    label: str
    color: str
    dashed: bool = False
    fill: bool = True  # filled shapes (rectangles, polygons, discs) or outline only
    fill_alpha: float = 0.9
    zorder: float = 3.0


# One table for every Eagle layer the renderer knows how to colour.
LAYER_STYLES: dict[Layer, LayerStyle] = {
    Layer.TOP: LayerStyle("Top copper (1)", _COLOR_PAD_TOP, fill_alpha=0.85, zorder=2.0),
    Layer.BOTTOM: LayerStyle("Bottom copper (16)", _COLOR_PAD_BOTTOM, fill_alpha=0.7, zorder=2.0),
    Layer.DIMENSION: LayerStyle("Dimension (20)", "#ff9f1c", fill=False),
    Layer.T_PLACE: LayerStyle("tPlace silkscreen (21)", "#f5e6a1"),
    Layer.B_PLACE: LayerStyle("bPlace silkscreen (22)", "#b39ddb"),
    Layer.T_NAMES: LayerStyle("tNames (25)", "#8bd5ca"),
    Layer.B_NAMES: LayerStyle("bNames (26)", "#7aa2a0"),
    Layer.T_VALUES: LayerStyle("tValues (27)", "#c6a0f6"),
    Layer.B_VALUES: LayerStyle("bValues (28)", "#a58bc9"),
    Layer.T_KEEPOUT: LayerStyle("tKeepout / courtyard (39)", "#ff4fd8", dashed=True, fill=False),
    Layer.B_KEEPOUT: LayerStyle("bKeepout (40)", "#c05cff", dashed=True, fill=False),
    Layer.MILLING: LayerStyle("Milling (46)", "#3ddc84", fill=False),
    Layer.DOCUMENT: LayerStyle("Document (48)", "#9aa0a6", fill_alpha=0.35, zorder=1.5),
    Layer.REFERENCE: LayerStyle("Reference (49)", "#9aa0a6", fill_alpha=0.35, zorder=1.5),
    Layer.T_DOCU: LayerStyle("tDocu (51)", "#9aa0a6", fill_alpha=0.35, zorder=1.5),
    Layer.B_DOCU: LayerStyle("bDocu (52)", "#6b7078", fill_alpha=0.35, zorder=1.5),
    Layer.PINS: LayerStyle("Pins (93)", "#3fb950"),
    Layer.SYMBOLS: LayerStyle("Symbols (94)", "#ff6b5e", fill_alpha=0.5),
    Layer.NAMES: LayerStyle("Names (95)", "#d0d7de"),
    Layer.VALUES: LayerStyle("Values (96)", "#b6c2cf"),
    Layer.INFO: LayerStyle("Info (97)", "#8b949e"),
    Layer.GUIDE: LayerStyle("Guide (98)", "#d29922", dashed=True, fill=False),
}
_FALLBACK_COLOR = "#bbbbbb"


def layer_style(layer: Layer) -> LayerStyle:
    style = LAYER_STYLES.get(layer)
    if style is not None:
        return style
    return LayerStyle(f"layer {int(layer)}", _FALLBACK_COLOR)


# ---------------------------------------------------------------------------------------------
# Pure geometry helpers (no matplotlib)
# ---------------------------------------------------------------------------------------------


def _rotate_translate(points: Iterable[XY], cx: float, cy: float, angle_deg: float) -> list[XY]:
    """Rotate local points CCW by angle_deg about the origin, then move them to (cx, cy)."""
    a = math.radians(angle_deg)
    ca, sa = math.cos(a), math.sin(a)
    return [(cx + px * ca - py * sa, cy + px * sa + py * ca) for px, py in points]


def rect_polygon(cx: float, cy: float, width: float, height: float, angle_deg: float) -> list[XY]:
    """Corners of a (width x height) rectangle centred on (cx, cy), rotated CCW about its centre."""
    hw, hh = width / 2.0, height / 2.0
    local = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]
    return _rotate_translate(local, cx, cy, angle_deg)


def ellipse_polygon(
    cx: float,
    cy: float,
    width: float,
    height: float,
    angle_deg: float = 0.0,
    segments: int = _ELLIPSE_SEGMENTS,
) -> list[XY]:
    """Ellipse with full axes `width` (along the rotated x axis) and `height`."""
    rx, ry = width / 2.0, height / 2.0
    local = [
        (rx * math.cos(2.0 * math.pi * i / segments), ry * math.sin(2.0 * math.pi * i / segments))
        for i in range(segments)
    ]
    return _rotate_translate(local, cx, cy, angle_deg)


def stadium_polygon(
    cx: float,
    cy: float,
    length: float,
    thickness: float,
    angle_deg: float,
    cap_segments: int = _CAP_SEGMENTS,
) -> list[XY]:
    """Stadium (rectangle with semicircular ends): `length` is the total extent along the axis
    at `angle_deg`, `thickness` the width across. length < thickness degenerates to a circle."""
    length = max(length, thickness)
    r = thickness / 2.0
    half = (length - thickness) / 2.0
    local: list[XY] = []
    for i in range(cap_segments + 1):  # right cap, -90 .. +90 degrees
        t = -math.pi / 2.0 + math.pi * i / cap_segments
        local.append((half + r * math.cos(t), r * math.sin(t)))
    for i in range(cap_segments + 1):  # left cap, +90 .. +270 degrees
        t = math.pi / 2.0 + math.pi * i / cap_segments
        local.append((-half + r * math.cos(t), r * math.sin(t)))
    return _rotate_translate(local, cx, cy, angle_deg)


def pad_polygon(pad: Pad) -> list[XY]:
    """Copper outline of a pad in package coordinates (mm)."""
    if pad.shape is PadShape.POLYGON and len(pad.outline) >= 3:
        return [(p.x, p.y) for p in pad.outline]
    w, h = pad.width, pad.height
    if pad.shape is PadShape.ROUND:
        return ellipse_polygon(pad.x, pad.y, w, h, pad.rotation)
    if pad.shape is PadShape.OVAL:
        if w >= h:
            return stadium_polygon(pad.x, pad.y, w, h, pad.rotation)
        return stadium_polygon(pad.x, pad.y, h, w, pad.rotation + 90.0)
    return rect_polygon(pad.x, pad.y, w, h, pad.rotation)  # RECT, POLYGON without outline


def drill_polygon(pad: Pad) -> list[XY]:
    """Hole outline of a through-hole pad: circle, or stadium when slot_length > drill."""
    if pad.slot_length > pad.drill:
        return stadium_polygon(pad.x, pad.y, pad.slot_length, pad.drill, pad.slot_angle)
    return ellipse_polygon(pad.x, pad.y, pad.drill, pad.drill)


def arc_geometry(
    x1: float, y1: float, x2: float, y2: float, curve: float
) -> tuple[float, float, float, float, float]:
    """Convert an Eagle arc wire to (cx, cy, radius, theta1, theta2).

    `curve` is the signed sweep from p1 to p2 in degrees (CCW positive, Y up). The centre lies to
    the left of the directed chord p1 -> p2 for 0 < curve < 180 and for -360 < curve < -180, and
    to the right for 180 < curve < 360 and -180 < curve < 0.

    Angles are in degrees describing the CCW span of the arc (as matplotlib's Arc expects):
    theta1 is in [0, 360) and theta2 = theta1 + |curve|. For curve > 0 the wire runs from theta1
    to theta2 (p1 is at theta1), for curve < 0 it runs from theta2 back to theta1 (p1 is at
    theta2). Raises ValueError for a straight wire (curve 0), a zero chord, or |curve| >= 360.
    """
    dx, dy = x2 - x1, y2 - y1
    chord = math.hypot(dx, dy)
    if chord == 0.0:
        raise ValueError("arc with zero chord")
    if curve == 0.0 or abs(curve) >= 360.0:
        raise ValueError(f"curve {curve} does not describe an arc")
    half = math.radians(curve) / 2.0
    radius = chord / (2.0 * math.sin(abs(half)))
    # Signed distance from the chord midpoint to the centre along the left normal of p1 -> p2.
    offset = math.copysign(1.0, curve) * radius * math.cos(half)
    cx = (x1 + x2) / 2.0 + (-dy / chord) * offset
    cy = (y1 + y2) / 2.0 + (dx / chord) * offset
    start_x, start_y = (x1, y1) if curve > 0 else (x2, y2)
    theta1 = math.degrees(math.atan2(start_y - cy, start_x - cx)) % 360.0
    if theta1 >= 360.0 - 1e-9:
        theta1 -= 360.0
    return cx, cy, radius, theta1, theta1 + abs(curve)


def arc_points(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    curve: float,
    step_deg: float = _ARC_STEP_DEG,
) -> list[XY]:
    """Polyline from p1 to p2 following the arc, endpoints exactly p1 and p2.

    A zero curve or a zero chord returns the plain two-point segment."""
    if curve == 0.0 or (x1 == x2 and y1 == y2):
        return [(x1, y1), (x2, y2)]
    cx, cy, radius, _, _ = arc_geometry(x1, y1, x2, y2, curve)
    start = math.atan2(y1 - cy, x1 - cx)
    steps = max(2, math.ceil(abs(curve) / step_deg))
    pts: list[XY] = []
    for i in range(steps + 1):
        a = start + math.radians(curve) * i / steps
        pts.append((cx + radius * math.cos(a), cy + radius * math.sin(a)))
    pts[0] = (x1, y1)
    pts[-1] = (x2, y2)
    return pts


def _label_corners(label: Label) -> list[XY]:
    """Estimated text rectangle corners (the real glyph extents are only known at draw time)."""
    w = _TEXT_ADVANCE_EM * label.size * max(len(label.text), 1)
    h = label.size
    ha, va = _TEXT_ALIGN[label.align]
    x0 = {"left": 0.0, "center": -w / 2.0, "right": -w}[ha]
    y0 = {"bottom": 0.0, "center": -h / 2.0, "top": -h}[va]
    local = [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]
    return _rotate_translate(local, label.x, label.y, label.rotation)


_CORNERS = ((-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0))


def _graphic_points(g: Graphic) -> list[XY]:
    """Points whose bounding box covers the graphic (stroke widths included)."""
    if isinstance(g, Wire):
        half = g.width / 2.0
        pts = arc_points(g.x1, g.y1, g.x2, g.y2, g.curve)
        return [(x + sx * half, y + sy * half) for x, y in pts for sx, sy in _CORNERS]
    if isinstance(g, Circle):
        reach = g.radius + g.width / 2.0
        return [(g.x + sx * reach, g.y + sy * reach) for sx, sy in _CORNERS]
    if isinstance(g, Rectangle):
        return _rectangle_polygon(g)
    if isinstance(g, Polygon):
        return [(p.x, p.y) for p in g.points]
    return _label_corners(g)


def _rectangle_polygon(r: Rectangle) -> list[XY]:
    """Eagle rectangles rotate about their own centre."""
    cx, cy = (r.x1 + r.x2) / 2.0, (r.y1 + r.y2) / 2.0
    return rect_polygon(cx, cy, abs(r.x2 - r.x1), abs(r.y2 - r.y1), r.rotation)


def _pin_geometry(pin: Pin) -> tuple[XY, XY, float, float]:
    """(connection point, body end of the pin, unit direction x, unit direction y)."""
    a = math.radians(pin.angle)
    dx, dy = math.cos(a), math.sin(a)
    length = _PIN_LENGTH_MM[pin.length]
    return (pin.x, pin.y), (pin.x + dx * length, pin.y + dy * length), dx, dy


def _luminance(hex_color: str, alpha: float = 1.0) -> float:
    """Perceived luminance (0..1) of a #rrggbb colour blended over the black-ish background."""
    r, g, b = (int(hex_color[i : i + 2], 16) / 255.0 for i in (1, 3, 5))
    return alpha * (0.299 * r + 0.587 * g + 0.114 * b)


def _contrast_text_color(hex_color: str, alpha: float = 1.0) -> str:
    return _TEXT_DARK if _luminance(hex_color, alpha) > _TEXT_LUMINANCE_SPLIT else _TEXT_LIGHT


@dataclass
class _Bounds:
    xmin: float = math.inf
    xmax: float = -math.inf
    ymin: float = math.inf
    ymax: float = -math.inf

    def include(self, points: Iterable[XY]) -> None:
        for x, y in points:
            self.xmin = min(self.xmin, x)
            self.xmax = max(self.xmax, x)
            self.ymin = min(self.ymin, y)
            self.ymax = max(self.ymax, y)

    def padded(self) -> tuple[float, float, float, float]:
        if self.xmin > self.xmax:
            return -1.0, 1.0, -1.0, 1.0
        pad = max(
            _BOUNDS_PAD_MIN_MM,
            _BOUNDS_PAD_FRACTION * max(self.xmax - self.xmin, self.ymax - self.ymin),
        )
        return self.xmin - pad, self.xmax + pad, self.ymin - pad, self.ymax + pad


# ---------------------------------------------------------------------------------------------
# matplotlib access
# ---------------------------------------------------------------------------------------------


def _require_matplotlib() -> None:
    try:
        import matplotlib  # noqa: F401
    except ImportError as exc:
        raise RenderError(
            "Rendering needs matplotlib, which is a dev dependency. "
            "Install it in the venv: pip install -r requirements-dev.txt"
        ) from exc


def _normalise_format(path: Path) -> str:
    fmt = path.suffix.lower().lstrip(".")
    if fmt not in SUPPORTED_FORMATS:
        raise RenderError(
            f"Unsupported output format '{path.suffix}' for {path}. "
            f"Use one of: {', '.join('.' + f for f in SUPPORTED_FORMATS)}"
        )
    return fmt


class _Painter:
    """Owns one matplotlib figure and draws model objects onto it in millimetres."""

    def __init__(
        self,
        bounds: _Bounds,
        *,
        title: str | None,
        dpi: int,
        grid_step: float | None = None,
    ) -> None:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
        from matplotlib.ticker import MultipleLocator

        xmin, xmax, ymin, ymax = bounds.padded()
        xspan, yspan = xmax - xmin, ymax - ymin
        ratio = min(max(xspan / yspan, _FIG_RATIO_MIN), _FIG_RATIO_MAX)
        if ratio >= 1.0:
            ax_w, ax_h = _FIG_LONG_SIDE_IN, _FIG_LONG_SIDE_IN / ratio
        else:
            ax_w, ax_h = _FIG_LONG_SIDE_IN * ratio, _FIG_LONG_SIDE_IN
        margin_l, margin_r, margin_b, margin_t = 0.9, 0.3, 0.7, 0.5
        fig_w, fig_h = ax_w + margin_l + margin_r, ax_h + margin_b + margin_t

        self.dpi = dpi
        self.fig: Figure = Figure(figsize=(fig_w, fig_h), dpi=dpi, facecolor=_BG)
        FigureCanvasAgg(self.fig)
        self.ax: Axes = self.fig.add_axes(
            (margin_l / fig_w, margin_b / fig_h, ax_w / fig_w, ax_h / fig_h), facecolor=_BG
        )
        ax = self.ax
        ax.set_xlim(xmin, xmax)
        ax.set_ylim(ymin, ymax)
        ax.set_aspect("equal", adjustable="box")
        ax.apply_aspect()
        # Points per millimetre after the aspect adjustment, used to size strokes and fonts.
        self.pt_per_mm = ax.get_position().width * self.fig.get_figwidth() * 72.0 / xspan

        ax.set_axisbelow(True)
        ax.grid(True, color=_GRID, alpha=_GRID_ALPHA, linewidth=0.5)
        if grid_step is not None and max(xspan, yspan) / grid_step <= 40:
            ax.xaxis.set_major_locator(MultipleLocator(grid_step))
            ax.yaxis.set_major_locator(MultipleLocator(grid_step))
        ax.axhline(0.0, color=_AXIS_COLOR, linewidth=0.5, zorder=0.5)
        ax.axvline(0.0, color=_AXIS_COLOR, linewidth=0.5, zorder=0.5)
        ax.set_xlabel("X (mm)", color=_FG, fontsize=9)
        ax.set_ylabel("Y (mm)", color=_FG, fontsize=9)
        ax.tick_params(colors=_FG, labelsize=8)
        for spine in ax.spines.values():
            spine.set_color(_AXIS_COLOR)
        if title:
            ax.set_title(title, color=_FG, fontsize=11, pad=8)

        self._legend: dict[str, tuple[str, str, bool]] = {}  # label -> (kind, color, dashed)

    # -- primitives -------------------------------------------------------------------------

    def line_width_pt(self, width_mm: float) -> float:
        return max(width_mm * self.pt_per_mm, _MIN_LINE_PT)

    def note(self, label: str, color: str, kind: str = "line", dashed: bool = False) -> None:
        """Register a legend entry (first registration wins, order is kept)."""
        self._legend.setdefault(label, (kind, color, dashed))

    def polyline(
        self,
        points: Sequence[XY],
        width_mm: float,
        color: str,
        *,
        dashed: bool = False,
        zorder: float = 3.0,
    ) -> None:
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        self.ax.plot(
            xs,
            ys,
            color=color,
            linewidth=self.line_width_pt(width_mm),
            linestyle=_DASH_PATTERN if dashed else "solid",
            solid_capstyle="round",
            dash_capstyle="butt",
            solid_joinstyle="round",
            zorder=zorder,
        )

    def polygon(
        self,
        points: Sequence[XY],
        *,
        face: str | None,
        alpha: float = 1.0,
        edge: str | None = None,
        edge_width_mm: float = 0.0,
        dashed: bool = False,
        zorder: float = 3.0,
    ) -> None:
        from matplotlib.patches import Polygon as MplPolygon

        patch = MplPolygon(
            list(points),
            closed=True,
            facecolor=face if face is not None else "none",
            edgecolor=edge if edge is not None else "none",
            alpha=alpha,
            linewidth=self.line_width_pt(edge_width_mm) if edge is not None else 0.0,
            linestyle=_DASH_PATTERN if dashed else "solid",
            joinstyle="round",
            zorder=zorder,
        )
        self.ax.add_patch(patch)

    def text(
        self,
        x: float,
        y: float,
        s: str,
        size_mm: float,
        color: str,
        *,
        rotation: float = 0.0,
        ha: str = "left",
        va: str = "bottom",
        zorder: float = 6.0,
        weight: str = "normal",
    ) -> None:
        self.ax.text(
            x,
            y,
            s,
            color=color,
            fontsize=max(size_mm * self.pt_per_mm, 1.0),
            rotation=rotation,
            rotation_mode="anchor",
            ha=ha,
            va=va,
            zorder=zorder,
            fontweight=weight,
            clip_on=False,
        )

    # -- graphics ---------------------------------------------------------------------------

    def graphics(self, graphics: Iterable[Graphic]) -> None:
        for g in graphics:
            self.graphic(g)

    def graphic(self, g: Graphic) -> None:
        style = layer_style(g.layer)
        kind = "patch" if style.fill and not isinstance(g, Wire | Label) else "line"
        self.note(style.label, style.color, kind, style.dashed)
        z = style.zorder
        if isinstance(g, Wire):
            pts = arc_points(g.x1, g.y1, g.x2, g.y2, g.curve)
            self.polyline(pts, g.width, style.color, dashed=style.dashed, zorder=z)
        elif isinstance(g, Circle):
            self._circle(g, style)
        elif isinstance(g, Rectangle):
            self._shape(_rectangle_polygon(g), 0.0, style)
        elif isinstance(g, Polygon):
            self._shape([(p.x, p.y) for p in g.points], g.width, style)
        else:
            ha, va = _TEXT_ALIGN[g.align]
            self.text(
                g.x, g.y, g.text, g.size, style.color, rotation=g.rotation, ha=ha, va=va, zorder=z
            )

    def _circle(self, c: Circle, style: LayerStyle) -> None:
        if c.width <= 0.0:  # filled disc
            ring = ellipse_polygon(c.x, c.y, 2.0 * c.radius, 2.0 * c.radius)
            self._shape(ring, 0.0, style)
            return
        if c.radius <= 0.0:  # degenerate ring: a dot of diameter `width`
            dot = ellipse_polygon(c.x, c.y, c.width, c.width)
            self.polygon(dot, face=style.color, zorder=style.zorder)
            return
        ring = ellipse_polygon(c.x, c.y, 2.0 * c.radius, 2.0 * c.radius)
        self.polyline(
            [*ring, ring[0]], c.width, style.color, dashed=style.dashed, zorder=style.zorder
        )

    def _shape(self, points: Sequence[XY], width_mm: float, style: LayerStyle) -> None:
        """Rectangle, disc or polygon: filled, or outline only for layers like keepout."""
        if style.fill:
            self.polygon(
                points,
                face=style.color,
                alpha=style.fill_alpha,
                edge=style.color,
                edge_width_mm=width_mm,
                zorder=style.zorder,
            )
        else:
            self.polygon(
                points,
                face=None,
                edge=style.color,
                edge_width_mm=max(width_mm, COURTYARD_WIDTH_MM),
                dashed=style.dashed,
                zorder=style.zorder,
            )

    # -- footprint --------------------------------------------------------------------------

    def pad(self, pad: Pad) -> None:
        outline = pad_polygon(pad)
        if pad.is_smd:
            bottom = pad.layer is Layer.BOTTOM
            color = _COLOR_PAD_BOTTOM if bottom else _COLOR_PAD_TOP
            self.note("SMD pad, bottom" if bottom else "SMD pad, top", color, "patch")
        else:
            color = _COLOR_PAD_THT
            self.note("Through-hole pad", color, "patch")
        self.polygon(outline, face=color, alpha=_PAD_ALPHA, zorder=2.0)
        if not pad.is_smd:
            self.polygon(
                drill_polygon(pad),
                face=_COLOR_HOLE_FILL,
                edge=_COLOR_HOLE_RIM,
                edge_width_mm=0.05,
                zorder=4.0,
            )
        if pad.name:
            xs = [p[0] for p in outline]
            ys = [p[1] for p in outline]
            fit_w = (max(xs) - min(xs)) / (_TEXT_ADVANCE_EM * len(pad.name))
            fit_h = (max(ys) - min(ys)) * 0.7
            size_mm = max(min(fit_w, fit_h), 2.0 / self.pt_per_mm)
            size_mm = min(size_mm, 12.0 / self.pt_per_mm)
            text_color = _contrast_text_color(color, _PAD_ALPHA) if pad.is_smd else _TEXT_DARK
            self.text(
                pad.x,
                pad.y,
                pad.name,
                size_mm,
                text_color,
                ha="center",
                va="center",
                zorder=6.0,
                weight="bold",
            )

    def hole(self, hole: Hole) -> None:
        self.note("Non-plated hole", _COLOR_HOLE_FILL, "patch")
        ring = ellipse_polygon(hole.x, hole.y, hole.drill, hole.drill)
        self.polygon(
            ring,
            face=_COLOR_HOLE_FILL,
            edge=_COLOR_NPTH_RIM,
            edge_width_mm=0.08,
            zorder=4.0,
        )

    # -- symbol -----------------------------------------------------------------------------

    def pin(self, pin: Pin) -> None:
        color = _PIN_DIRECTION_COLOR[pin.direction]
        self.note(f"Pin {pin.direction.value}", color)
        (x0, y0), (x1, y1), dx, dy = _pin_geometry(pin)
        nx, ny = -dy, dx  # left normal of the pin direction
        vertical = abs(dy) > abs(dx)
        if pin.length is not PinLength.POINT:
            self.polyline([(x0, y0), (x1, y1)], SYMBOL_WIRE_WIDTH_MM, color, zorder=3.5)

        if pin.inverted:
            self.note("Inverted pin (dot)", _FG)
            cx, cy = x1 - dx * _PIN_DOT_RADIUS_MM, y1 - dy * _PIN_DOT_RADIUS_MM
            dot = ellipse_polygon(cx, cy, 2.0 * _PIN_DOT_RADIUS_MM, 2.0 * _PIN_DOT_RADIUS_MM)
            self.polygon(
                dot,
                face=_BG,
                edge=color,
                edge_width_mm=SYMBOL_WIRE_WIDTH_MM,
                zorder=3.6,
            )
        if pin.clock:
            self.note("Clock pin (triangle)", _FG)
            tri = [
                (x1 + nx * _PIN_CLOCK_HALF_BASE_MM, y1 + ny * _PIN_CLOCK_HALF_BASE_MM),
                (x1 + dx * _PIN_CLOCK_HEIGHT_MM, y1 + dy * _PIN_CLOCK_HEIGHT_MM),
                (x1 - nx * _PIN_CLOCK_HALF_BASE_MM, y1 - ny * _PIN_CLOCK_HALF_BASE_MM),
            ]
            self.polyline(tri, SYMBOL_WIRE_WIDTH_MM, color, zorder=3.6)

        # Connection point marker.
        self.note("Connection point", _FG, "marker")
        marker = ellipse_polygon(x0, y0, 2.0 * _PIN_MARKER_RADIUS_MM, 2.0 * _PIN_MARKER_RADIUS_MM)
        self.polygon(marker, face=color, edge=_FG, edge_width_mm=0.06, zorder=5.0)

        # Pin name inside the body, beyond the body end of the pin.
        name_x = x1 + dx * _PIN_NAME_GAP_MM
        name_y = y1 + dy * _PIN_NAME_GAP_MM
        # dx >= 0 means the text starts at the anchor and runs along the pin direction.
        ha = "left" if (dx > 0.5 or dy > 0.5) else "right"
        self.text(
            name_x,
            name_y,
            pin.name,
            _PIN_NAME_SIZE_MM,
            _FG,
            rotation=90.0 if vertical else 0.0,
            ha=ha,
            va="center",
        )
        # Pin number beside the pin line, on the side that does not collide with the line.
        mid_x = (x0 + x1) / 2.0
        mid_y = (y0 + y1) / 2.0
        if vertical:
            num_x, num_y = mid_x - _PIN_NUMBER_GAP_MM, mid_y
        else:
            num_x, num_y = mid_x, mid_y + _PIN_NUMBER_GAP_MM
        self.text(
            num_x,
            num_y,
            pin.number,
            _PIN_NUMBER_SIZE_MM,
            color,
            rotation=90.0 if vertical else 0.0,
            ha="center",
            va="bottom",
        )

    # -- output -----------------------------------------------------------------------------

    def _legend_handles(self) -> list[Artist]:
        from matplotlib.lines import Line2D
        from matplotlib.patches import Patch

        handles: list[Artist] = []
        for label, (kind, color, dashed) in self._legend.items():
            if kind == "patch":
                handles.append(Patch(facecolor=color, edgecolor=color, label=label, alpha=0.9))
            elif kind == "marker":
                handles.append(
                    Line2D(
                        [0],
                        [0],
                        marker="o",
                        linestyle="none",
                        markerfacecolor=_PIN_DIRECTION_COLOR[PinDirection.PAS],
                        markeredgecolor=color,
                        markersize=6,
                        label=label,
                    )
                )
            else:
                handles.append(
                    Line2D(
                        [0],
                        [0],
                        color=color,
                        linewidth=2.0,
                        linestyle=_DASH_PATTERN if dashed else "solid",
                        label=label,
                    )
                )
        return handles

    def save(self, path: Path, fmt: str) -> Path:
        handles = self._legend_handles()
        if handles:
            self.ax.legend(
                handles=handles,
                loc="upper left",
                bbox_to_anchor=(1.02, 1.0),
                borderaxespad=0.0,
                fontsize=8,
                frameon=True,
                facecolor=_LEGEND_BG,
                edgecolor=_AXIS_COLOR,
                labelcolor=_FG,
            )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.fig.savefig(
                path,
                format=fmt,
                dpi=self.dpi,
                bbox_inches="tight",
                pad_inches=0.15,
                facecolor=_BG,
                metadata={"Date": None} if fmt == "svg" else None,
            )
        except OSError as exc:
            raise RenderError(f"Cannot write rendered image to {path}: {exc}") from exc
        return path


# ---------------------------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------------------------


def _prepare(path: str | Path, dpi: int) -> tuple[Path, str]:
    _require_matplotlib()
    if dpi <= 0:
        raise RenderError(f"dpi must be positive, got {dpi}")
    out = Path(path)
    return out, _normalise_format(out)


def render_package(
    package: Package,
    path: str | Path,
    *,
    title: str | None = None,
    dpi: int = 160,
) -> Path:
    """Render a footprint to a .png or .svg file (mm, Y up, equal aspect) and return the path."""
    out, fmt = _prepare(path, dpi)

    bounds = _Bounds()
    for pad in package.pads:
        bounds.include(pad_polygon(pad))
        if not pad.is_smd:
            bounds.include(drill_polygon(pad))
    for hole in package.holes:
        r = hole.drill / 2.0
        bounds.include([(hole.x - r, hole.y - r), (hole.x + r, hole.y + r)])
    for g in package.graphics:
        bounds.include(_graphic_points(g))

    if title is None:
        title = f"{package.name}  |  {len(package.pads)} pads"
        if package.holes:
            title += f", {len(package.holes)} holes"
    painter = _Painter(bounds, title=title, dpi=dpi)
    # Graphics first so that pad names and drills end up on top (zorder decides, order is free).
    painter.graphics(package.graphics)
    for pad in package.pads:
        painter.pad(pad)
    for hole in package.holes:
        painter.hole(hole)
    return painter.save(out, fmt)


def render_symbol(
    symbol: Symbol,
    path: str | Path,
    *,
    title: str | None = None,
    dpi: int = 160,
) -> Path:
    """Render a symbol to a .png or .svg file (mm, Y up) and return the path.

    Pins are lines from the connection point (marked) towards the body, coloured by electrical
    direction; a ring marks inverted pins and a small triangle clock pins."""
    out, fmt = _prepare(path, dpi)

    bounds = _Bounds()
    for g in symbol.graphics:
        bounds.include(_graphic_points(g))
    for pin in symbol.pins:
        start, end, dx, dy = _pin_geometry(pin)
        bounds.include([start, end])
        # Estimated extent of the name text inside the body.
        name_len = _TEXT_ADVANCE_EM * _PIN_NAME_SIZE_MM * max(len(pin.name), 1)
        far = (
            end[0] + dx * (_PIN_NAME_GAP_MM + name_len),
            end[1] + dy * (_PIN_NAME_GAP_MM + name_len),
        )
        bounds.include([far])

    if title is None:
        title = f"{symbol.name}  |  {len(symbol.pins)} pins"
    painter = _Painter(bounds, title=title, dpi=dpi, grid_step=_SYMBOL_GRID_MM)
    painter.graphics(symbol.graphics)
    for pin in symbol.pins:
        painter.pin(pin)
    return painter.save(out, fmt)


def _safe_stem(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", text) or "part"


def render_part(
    part: Part,
    directory: str | Path,
    *,
    formats: Sequence[str] = ("png",),
    dpi: int = 160,
) -> list[Path]:
    """Write `<lcsc_id>_footprint.<fmt>` and `<lcsc_id>_symbol_<n>.<fmt>` (n from 1) per format."""
    _require_matplotlib()
    wanted: list[str] = []
    for f in formats:
        fmt = f.lower().lstrip(".")
        if fmt not in SUPPORTED_FORMATS:
            raise RenderError(
                f"Unsupported output format '{f}'. Use one of: {', '.join(SUPPORTED_FORMATS)}"
            )
        if fmt not in wanted:
            wanted.append(fmt)

    out_dir = Path(directory)
    stem = _safe_stem(part.lcsc_id)
    head = f"{part.lcsc_id}  {part.mpn}".strip()
    written: list[Path] = []
    for fmt in wanted:
        written.append(
            render_package(
                part.package,
                out_dir / f"{stem}_footprint.{fmt}",
                title=f"{head}  |  footprint {part.package.name}  |  {len(part.package.pads)} pads",
                dpi=dpi,
            )
        )
        for n, symbol in enumerate(part.symbols, start=1):
            written.append(
                render_symbol(
                    symbol,
                    out_dir / f"{stem}_symbol_{n}.{fmt}",
                    title=f"{head}  |  symbol {symbol.name}  |  {len(symbol.pins)} pins",
                    dpi=dpi,
                )
            )
    return written
