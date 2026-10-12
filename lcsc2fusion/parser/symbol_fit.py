"""Fit the rectangular body of a symbol to the pin names Eagle draws inside it.

EasyEDA sizes symbol bodies for its own tiny font. Eagle draws the name of every pin INSIDE the
body, starting a couple of millimetres from the body edge, with a vector font about 1.78 mm tall.
In a small body the names of a left and a right pin on the same row collide, and the vertical
names of top and bottom pins (drawn along the pin line, into the body) collide with the
horizontal names of the side pins.

``fit_symbol`` grows the body, never shrinks it, in whole grid steps (2.54 mm) per edge and moves
the pins with their edge, so the pin position along the edge, the distance of every connection
point from its edge and the pin grid are all preserved.

Scope. Only a body that is one axis aligned rectangle made of four straight wires on
``Layer.SYMBOLS`` is handled (the rectangle is the bounding box of all non label graphics).
Anything else (op-amp triangle, arcs, pins that do not touch the box) is returned unchanged, as
is a symbol that already fits. Other graphics (inner marks) stay where they are, except that the
end of a straight inner wire that lies on an edge of the old body moves with that edge, so the
inner lines of a crystal symbol stay connected to the pin lines.

Side of a pin from its angle (the direction the pin line extends from the connection point
towards the body): 0 is the LEFT edge, 180 the RIGHT edge, 270 the TOP edge (name runs down into
the body) and 90 the BOTTOM edge (name runs up).

Required sizes (derived from the calibration constants below). A name starts ``inset`` away
from its edge, so it must end inside the body and two names that face each other need ``inset``
at both ends plus a gap:

* width: ``inset + longest name`` for each edge with pins, and for every left/right pair on the
  same row (``|dy| < cap + gap``) ``left_name + right_name + 2 * inset + gap``. Growth is split
  equally over both edges.
* height floor: the same rule for top/bottom names, with pairs in the same column. Growth is
  split equally over top and bottom.
* vertical against horizontal names: a vertical name (top or bottom pin) must end before the
  horizontal name row of every side pin whose name it crosses, i.e. the top edge must sit at least
  ``top_name + inset + cap / 2 + gap`` above the row (symmetrically for the bottom edge). A side
  name is crossed when its span along the edge, widened by ``cap / 2 + gap``, contains the column
  of the vertical name. Top and bottom grow independently.

Names that cannot meet are no obstacle for each other, which keeps wide bodies (microcontrollers,
modules) and small ones with short names from growing for nothing. Symbols with two pins or less
are never touched: the writer hides their pin names (``visible="off"``).

Idempotent: a fitted symbol fits.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, replace

from lcsc2fusion.constants import GRID_MM, Layer
from lcsc2fusion.model import Circle, Graphic, Label, Pin, Polygon, Symbol, Wire
from lcsc2fusion.parser.symbol import _graphics_extent, build_name_value_labels

__all__ = [
    "PIN_NAME_CAP_HEIGHT_MM",
    "PIN_NAME_CHAR_ADVANCE_MM",
    "PIN_NAME_GAP_MM",
    "PIN_NAME_INSET_MM",
    "display_name",
    "fit_symbol",
    "name_length_mm",
]

# Calibration measured on the user's Fusion screenshot of the synthetic SOIC-8 symbol (body width
# 10.16 mm, pin lines of 2.54 mm give the scale): the text "VDD" is about 4.06 mm wide and starts
# about 2.3 mm inside the body edge, because Eagle offsets pin names from the pin end.
PIN_NAME_CHAR_ADVANCE_MM = 1.35
# Cap height of the pin name text (measured about 1.3 mm, rounded up).
PIN_NAME_CAP_HEIGHT_MM = 1.4
# Distance between the body edge and the first character of a name.
PIN_NAME_INSET_MM = 2.2
# Free space kept between two names that face each other.
PIN_NAME_GAP_MM = 0.5

_DUPLICATE_SEPARATOR = "@"  # Eagle displays only the part before it
_OVERBAR_MARK = "!"  # toggles an overbar, not drawn as a character
_NAME_LABEL_TEXTS = frozenset({">NAME", ">VALUE"})

# Pin angles (the direction the pin line extends from the connection point).
_ANGLE_LEFT = 0
_ANGLE_BOTTOM = 90
_ANGLE_RIGHT = 180
_ANGLE_TOP = 270

# lbr_writer draws no pin names for symbols with this many pins or fewer (visible="off").
_MAX_PINS_WITHOUT_NAMES = 2

_EPS = 1e-6  # tolerance for geometry comparisons and grid rounding (mm)
_ROUND_DIGITS = 6  # coordinates are rounded to nanometres to drop float noise


def display_name(name: str) -> str:
    """Text Eagle shows for a pin name: before the ``@`` suffix, without overbar marks."""
    return name.split(_DUPLICATE_SEPARATOR, 1)[0].replace(_OVERBAR_MARK, "")


def name_length_mm(name: str) -> float:
    """Estimated drawn length of a pin name in mm."""
    return len(display_name(name)) * PIN_NAME_CHAR_ADVANCE_MM


# ---------------------------------------------------------------------------------------------
# Body detection
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Body:
    left: float
    bottom: float
    right: float
    top: float
    wire_width: float
    edge_indices: tuple[int, ...]  # indices into Symbol.graphics of the rectangle wires

    @property
    def width(self) -> float:
        return self.right - self.left

    @property
    def height(self) -> float:
        return self.top - self.bottom


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= _EPS


def _edge_of(wire: Wire, left: float, bottom: float, right: float, top: float) -> str | None:
    """Which edge of the box ("top", "bottom", "left", "right") a straight wire traces, if any."""
    if wire.curve != 0.0 or wire.layer != Layer.SYMBOLS:
        return None
    if _close(wire.y1, wire.y2):
        spans = _close(min(wire.x1, wire.x2), left) and _close(max(wire.x1, wire.x2), right)
        if spans and _close(wire.y1, top):
            return "top"
        if spans and _close(wire.y1, bottom):
            return "bottom"
    elif _close(wire.x1, wire.x2):
        spans = _close(min(wire.y1, wire.y2), bottom) and _close(max(wire.y1, wire.y2), top)
        if spans and _close(wire.x1, left):
            return "left"
        if spans and _close(wire.x1, right):
            return "right"
    return None


def _find_body(symbol: Symbol) -> _Body | None:
    """The single rectangle that bounds all non label graphics, or None."""
    shapes = [g for g in symbol.graphics if not isinstance(g, Label)]
    if not shapes or not all(isinstance(g, Wire | Circle | Polygon) for g in shapes):
        return None
    xs, ys = _graphics_extent(shapes)
    if not xs or not ys:
        return None
    left, right, bottom, top = min(xs), max(xs), min(ys), max(ys)
    if right - left <= _EPS or top - bottom <= _EPS:
        return None
    found: dict[str, list[tuple[int, Wire]]] = {"top": [], "bottom": [], "left": [], "right": []}
    for index, graphic in enumerate(symbol.graphics):
        if isinstance(graphic, Wire):
            edge = _edge_of(graphic, left, bottom, right, top)
            if edge is not None:
                found[edge].append((index, graphic))
    if not all(found.values()):
        return None
    edge_wires = sorted((item for group in found.values() for item in group), key=lambda t: t[0])
    indices = tuple(index for index, _ in edge_wires)
    return _Body(left, bottom, right, top, edge_wires[0][1].width, indices)


# ---------------------------------------------------------------------------------------------
# Pins per side
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Sides:
    left: tuple[Pin, ...]
    right: tuple[Pin, ...]
    top: tuple[Pin, ...]
    bottom: tuple[Pin, ...]


def _touches_side(pin: Pin, body: _Body) -> bool:
    """True if the pin sits outside (or on) its own edge and within the extent of that edge."""
    in_x = body.left - _EPS <= pin.x <= body.right + _EPS
    in_y = body.bottom - _EPS <= pin.y <= body.top + _EPS
    if pin.angle == _ANGLE_LEFT:
        return pin.x <= body.left + _EPS and in_y
    if pin.angle == _ANGLE_RIGHT:
        return pin.x >= body.right - _EPS and in_y
    if pin.angle == _ANGLE_TOP:
        return pin.y >= body.top - _EPS and in_x
    if pin.angle == _ANGLE_BOTTOM:
        return pin.y <= body.bottom + _EPS and in_x
    return False


def _split_sides(pins: tuple[Pin, ...], body: _Body) -> _Sides | None:
    """Pins grouped by edge, or None if any pin does not belong to an edge of the box."""
    if not all(_touches_side(p, body) for p in pins):
        return None
    return _Sides(
        left=tuple(p for p in pins if p.angle == _ANGLE_LEFT),
        right=tuple(p for p in pins if p.angle == _ANGLE_RIGHT),
        top=tuple(p for p in pins if p.angle == _ANGLE_TOP),
        bottom=tuple(p for p in pins if p.angle == _ANGLE_BOTTOM),
    )


def _longest(pins: tuple[Pin, ...]) -> float:
    return max((name_length_mm(p.name) for p in pins), default=0.0)


# ---------------------------------------------------------------------------------------------
# Required growth
# ---------------------------------------------------------------------------------------------


def _grid_ceil(deficit: float) -> float:
    """Smallest whole number of grid steps (in mm) that covers ``deficit`` (0 if none needed)."""
    if deficit <= _EPS:
        return 0.0
    return math.ceil(deficit / GRID_MM - _EPS) * GRID_MM


def _alone_extent(pins: tuple[Pin, ...]) -> float:
    """Minimum body size along one axis for the longest name of one edge (it must end inside)."""
    return PIN_NAME_INSET_MM + _longest(pins) if pins else 0.0


def _facing_extent(
    first: tuple[Pin, ...], second: tuple[Pin, ...], position: Callable[[Pin], float]
) -> float:
    """Minimum body size along one axis for names that start at two opposite edges.

    Only pins whose names can meet count as a pair: their positions along the edge (``position``)
    are closer than one cap height plus the gap, i.e. they sit on the same row or column.
    """
    reach = PIN_NAME_CAP_HEIGHT_MM + PIN_NAME_GAP_MM
    pairs = (
        name_length_mm(a.name) + name_length_mm(b.name) + 2.0 * PIN_NAME_INSET_MM + PIN_NAME_GAP_MM
        for a in first
        for b in second
        if abs(position(a) - position(b)) < reach
    )
    return max(_alone_extent(first), _alone_extent(second), *pairs)


@dataclass(frozen=True)
class _Growth:
    """Growth per edge in mm (all multiples of the grid)."""

    left: float = 0.0
    right: float = 0.0
    top: float = 0.0
    bottom: float = 0.0

    @property
    def any(self) -> bool:
        return self.left > 0.0 or self.right > 0.0 or self.top > 0.0 or self.bottom > 0.0


def _crosses(column_x: float, side_pin: Pin, left: float, right: float) -> bool:
    """True if the vertical name at ``column_x`` comes near the horizontal name of a side pin."""
    length = name_length_mm(side_pin.name)
    if side_pin.angle == _ANGLE_LEFT:
        start = left + PIN_NAME_INSET_MM
        end = start + length
    else:
        end = right - PIN_NAME_INSET_MM
        start = end - length
    reach = PIN_NAME_CAP_HEIGHT_MM / 2.0 + PIN_NAME_GAP_MM
    return start - reach < column_x < end + reach


def _required_growth(body: _Body, sides: _Sides) -> _Growth:
    # Width: both edges grow by the same amount so the symbol stays symmetric.
    need_width = _facing_extent(sides.left, sides.right, lambda p: p.y)
    side_growth = _grid_ceil((need_width - body.width) / 2.0)
    left = body.left - side_growth
    right = body.right + side_growth

    # Height floor: the vertical names have to fit into the body at all.
    need_height = _facing_extent(sides.top, sides.bottom, lambda p: p.x)
    floor_growth = _grid_ceil((need_height - body.height) / 2.0)
    top_edge = body.top + floor_growth
    bottom_edge = body.bottom - floor_growth

    # Vertical names must end before the name rows of the side pins they cross.
    side_pins = (*sides.left, *sides.right)
    row_margin = PIN_NAME_INSET_MM + PIN_NAME_CAP_HEIGHT_MM / 2.0 + PIN_NAME_GAP_MM
    need_top = max(
        (
            s.y + name_length_mm(t.name) + row_margin
            for t in sides.top
            for s in side_pins
            if _crosses(t.x, s, left, right)
        ),
        default=top_edge,
    )
    need_bottom = min(
        (
            s.y - name_length_mm(b.name) - row_margin
            for b in sides.bottom
            for s in side_pins
            if _crosses(b.x, s, left, right)
        ),
        default=bottom_edge,
    )
    return _Growth(
        side_growth,
        side_growth,
        floor_growth + _grid_ceil(need_top - top_edge),
        floor_growth + _grid_ceil(bottom_edge - need_bottom),
    )


# ---------------------------------------------------------------------------------------------
# Rebuild
# ---------------------------------------------------------------------------------------------


def _round(value: float) -> float:
    return round(value, _ROUND_DIGITS) + 0.0  # + 0.0 turns a negative zero into 0.0


def _move_pin(pin: Pin, growth: _Growth) -> Pin:
    """Move a pin with its edge; the position along the edge is unchanged."""
    if pin.angle == _ANGLE_LEFT:
        return replace(pin, x=_round(pin.x - growth.left))
    if pin.angle == _ANGLE_RIGHT:
        return replace(pin, x=_round(pin.x + growth.right))
    if pin.angle == _ANGLE_TOP:
        return replace(pin, y=_round(pin.y + growth.top))
    return replace(pin, y=_round(pin.y - growth.bottom))


def _rectangle(body: _Body, growth: _Growth) -> list[Wire]:
    """Enlarged body as four wires, in the order the parser writes a rectangle."""
    left = _round(body.left - growth.left)
    right = _round(body.right + growth.right)
    bottom = _round(body.bottom - growth.bottom)
    top = _round(body.top + growth.top)
    w, layer = body.wire_width, Layer.SYMBOLS
    return [
        Wire(left, top, right, top, w, layer),
        Wire(right, top, right, bottom, w, layer),
        Wire(right, bottom, left, bottom, w, layer),
        Wire(left, bottom, left, top, w, layer),
    ]


def _stretch_end(x: float, y: float, body: _Body, growth: _Growth) -> tuple[float, float]:
    """Move a wire end that lies on an edge of the old body together with that edge."""
    new_x, new_y = x, y
    in_x = body.left - _EPS <= x <= body.right + _EPS
    in_y = body.bottom - _EPS <= y <= body.top + _EPS
    if in_y and _close(x, body.left):
        new_x = _round(x - growth.left)
    elif in_y and _close(x, body.right):
        new_x = _round(x + growth.right)
    if in_x and _close(y, body.top):
        new_y = _round(y + growth.top)
    elif in_x and _close(y, body.bottom):
        new_y = _round(y - growth.bottom)
    return new_x, new_y


def _stretch_wire(wire: Wire, body: _Body, growth: _Growth) -> Wire:
    """Keep inner straight lines that ended on the old edge connected to the moved edge."""
    if wire.curve != 0.0:
        return wire
    x1, y1 = _stretch_end(wire.x1, wire.y1, body, growth)
    x2, y2 = _stretch_end(wire.x2, wire.y2, body, growth)
    return replace(wire, x1=x1, y1=y1, x2=x2, y2=y2)


def _rebuilt_graphics(
    symbol: Symbol, body: _Body, growth: _Growth, pins: tuple[Pin, ...]
) -> tuple[Graphic, ...]:
    edge_indices = set(body.edge_indices)
    graphics: list[Graphic] = []
    for index, graphic in enumerate(symbol.graphics):
        if index == body.edge_indices[0]:
            graphics.extend(_rectangle(body, growth))
        if index in edge_indices:
            continue
        if isinstance(graphic, Label) and graphic.text in _NAME_LABEL_TEXTS:
            continue
        graphics.append(
            _stretch_wire(graphic, body, growth) if isinstance(graphic, Wire) else graphic
        )
    return (*graphics, *build_name_value_labels(graphics, pins))


def fit_symbol(symbol: Symbol) -> tuple[Symbol, list[str]]:
    """Enlarge the rectangular body of ``symbol`` so Eagle's pin names fit inside it.

    Returns the (possibly enlarged) symbol and a list with at most one note. The symbol is
    returned unchanged (the same object) when it has two pins or less, no plain rectangular body,
    a pin that does not touch the box, or when it already fits.
    """
    if len(symbol.pins) <= _MAX_PINS_WITHOUT_NAMES:
        return symbol, []
    body = _find_body(symbol)
    if body is None:
        return symbol, []
    sides = _split_sides(symbol.pins, body)
    if sides is None:
        return symbol, []
    growth = _required_growth(body, sides)
    if not growth.any:
        return symbol, []

    pins = tuple(_move_pin(p, growth) for p in symbol.pins)
    graphics = _rebuilt_graphics(symbol, body, growth, pins)
    fitted = replace(symbol, pins=pins, graphics=graphics)
    note = (
        f"Symbol {symbol.name}: body enlarged by {growth.left + growth.right:.2f} x "
        f"{growth.top + growth.bottom:.2f} mm to fit pin names"
    )
    return fitted, [note]
