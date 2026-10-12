"""EasyEDA symbol parser: component ``result`` object to internal ``Symbol`` models.

Input is the ``result`` object of an EasyEDA component document (keys ``dataStr``, ``subparts``,
...). Output is one ``Symbol`` per unit plus a list of human-readable warnings.

Units. A single-unit part keeps its shapes in ``dataStr.shape``. A multi-unit part (TL072, ...)
has an empty ``dataStr.shape`` and a zero ``BBox``; the units live in ``subparts[]`` and each has
its own ``dataStr`` (shapes, ``head.x/y``, ``BBox``). Every unit becomes its own symbol
(decision D-007). Symbols are named ``base_name`` (one unit) or ``base_name_1``, ``base_name_2``...

Origin rule. The symbol origin is the centre of ``dataStr.BBox`` snapped to the 5 unit EasyEDA
symbol grid (``constants.EE_SYMBOL_GRID``), falling back to ``head.x/head.y`` when the BBox is
empty. Checked on the 24 real parts (local fixtures): the snapped centre equals ``head.x/y`` of
every multi-unit subpart, the body ends up centred on (0, 0) within half a grid step, and every
pin lands on the 5 unit grid (1.27 mm). The rule is therefore used unchanged for multi-unit
parts too. Note that EasyEDA pins sit on a 5 unit grid, not a 10 unit one, so many symbols have
pins at odd multiples of 1.27 mm, which is off Eagle's default 2.54 mm grid.

Pins. Section 0 gives the connection point, the electrical type and the rotation. The internal
``angle`` is the direction in which the pin line extends from the connection point towards the
body (Y up): ``angle = (ee_rotation + 180) % 360``. The pin length is the length of the line in
section 2 and is rounded UP to the next Eagle keyword length so the pin line always reaches the
body (thresholds in ``_PIN_LENGTH_STEPS``); the connection point is never moved. Names are made
Eagle-safe (whitespace, ``;`` and ``@`` replaced by ``_``) and duplicates within a symbol get
``@1``, ``@2``... on all occurrences (Eagle displays only the part before ``@``).

Graphics. Rectangles become four wires, circles and ellipses circles (or a polyline for a real
ellipse), arcs curved wires, polylines and polygons wires, paths wires (arcs preserved as curved
wires). Filled circles become filled discs and filled polygons/paths without arcs become
polygons. Text and arrowheads are not converted.

Labels. ``>NAME`` (above) and ``>VALUE`` (below) sit at the left edge of the graphics bounding
box. Their Y position clears the graphics and the pin connection points, so labels do not
collide with pins on the top or bottom edge.
"""

from __future__ import annotations

import itertools
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from lcsc2fusion.constants import (
    EE_PIN_TYPE_TO_DIRECTION,
    EE_SYMBOL_GRID,
    LABEL_SIZE_SYMBOL_MM,
    PIN_LENGTH_LONG_MM,
    PIN_LENGTH_MIDDLE_MM,
    PIN_LENGTH_POINT_MM,
    PIN_LENGTH_SHORT_MM,
    SYMBOL_WIRE_WIDTH_MM,
    Layer,
)
from lcsc2fusion.errors import ParseError
from lcsc2fusion.model import (
    Circle,
    Graphic,
    Label,
    Pin,
    PinDirection,
    PinLength,
    Point,
    Polygon,
    Symbol,
    TextAlign,
    Wire,
)
from lcsc2fusion.parser.shapes import field, split_pin, split_shape, to_float
from lcsc2fusion.parser.svgpath import (
    ArcTo,
    ClosePath,
    LineTo,
    MoveTo,
    Segment,
    arc_curve_deg,
    flatten_path,
    parse_path,
)
from lcsc2fusion.parser.units import Transform

__all__ = ["build_name_value_labels", "parse_symbol_units", "symbol_prefix"]

DEFAULT_PREFIX = "U"

# Pin line length (mm) is rounded up to the first step whose limit it does not exceed.
_PIN_LENGTH_STEPS: tuple[tuple[float, PinLength, float], ...] = (
    (0.01, PinLength.POINT, PIN_LENGTH_POINT_MM),
    (2.6, PinLength.SHORT, PIN_LENGTH_SHORT_MM),
    (5.1, PinLength.MIDDLE, PIN_LENGTH_MIDDLE_MM),
)
_PIN_LENGTH_LONG = (PinLength.LONG, PIN_LENGTH_LONG_MM)
# Report pins whose source length differs from the chosen Eagle keyword length by more than this.
_PIN_LENGTH_WARN_MM = 0.3

# EasyEDA pin rotation to the direction of the pin line (see module docstring).
_PIN_ANGLE_OFFSET_DEG = 180
_RIGHT_ANGLE_DEG = 90
_FULL_TURN_DEG = 360

# Eagle limits `curve` to +-359.9 degrees.
_MAX_CURVE_DEG = 359.9
_ELLIPSE_SEGMENTS = 36
_ARC_BBOX_SAMPLES = 16
_ELLIPSE_RATIO_TOL = 1e-3

_LABEL_GAP_MM = 0.635
_NAME_TEXT = ">NAME"
_VALUE_TEXT = ">VALUE"

# Shapes that carry no symbol geometry we convert (text and arrowheads), skipped silently.
_IGNORED_TAGS = frozenset({"T", "AR"})
_TRUE_TEXT = frozenset({"1", "true", "yes", "y", "show"})
_NOT_FILLED_TEXT = frozenset({"", "none", "transparent"})
# Whitespace, ';' (list separator) and '@' (Eagle's duplicate suffix delimiter).
_UNSAFE_NAME_CHARS = re.compile(r"[\s;@]+")
_UNIT_SUFFIX = re.compile(r"\.\d+$")
_PREFIX_TAIL = re.compile(r"[?\d]+$")
_PREFIX_UNSAFE = re.compile(r"[\s;]+")


# ---------------------------------------------------------------------------------------------
# Small input helpers. Everything malformed ends up as ParseError.
# ---------------------------------------------------------------------------------------------


def _number(text: str, what: str) -> float:
    """Parse a numeric field (empty means 0), rejecting nan and inf."""
    try:
        value = to_float(text)
    except ParseError as exc:
        raise ParseError(f"{what}: {exc}") from exc
    if not math.isfinite(value):
        raise ParseError(f"{what}: not a finite number: {text!r}")
    return value


def _is_true(text: str) -> bool:
    return text.strip().lower() in _TRUE_TEXT


def _is_filled(text: str) -> bool:
    return text.strip().lower() not in _NOT_FILLED_TEXT


def _as_list(value: object) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, str):
        return list(value)
    return []


def _data_str(container: object, what: str) -> Mapping[str, Any]:
    """Return ``container['dataStr']`` as a mapping (it is occasionally a JSON string)."""
    if not isinstance(container, Mapping):
        raise ParseError(f"{what}: expected an object, got {type(container).__name__}")
    raw = container.get("dataStr")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ParseError(f"{what}: dataStr is not valid JSON: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ParseError(f"{what}: missing 'dataStr' object")
    return raw


def _shape_strings(data_str: Mapping[str, Any], what: str) -> list[str]:
    raw = data_str.get("shape")
    if raw is None:
        return []
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        raise ParseError(f"{what}: 'shape' must be a list of strings")
    shapes: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ParseError(f"{what}: shape entries must be strings, got {type(item).__name__}")
        shapes.append(item)
    return shapes


def _has_pins(shapes: Sequence[str]) -> bool:
    return any(s.startswith("P~") for s in shapes)


# ---------------------------------------------------------------------------------------------
# Prefix
# ---------------------------------------------------------------------------------------------


def symbol_prefix(result: Mapping[str, Any]) -> str:
    """Reference designator prefix from ``dataStr.head.c_para.pre`` ('U?' -> 'U', 'U?.1' -> 'U').

    Falls back to 'U' when the field is missing or empty.
    """
    try:
        data_str = _data_str(result, "Component")
    except ParseError:
        return DEFAULT_PREFIX
    head = data_str.get("head")
    c_para = head.get("c_para") if isinstance(head, Mapping) else None
    pre = c_para.get("pre") if isinstance(c_para, Mapping) else None
    if not isinstance(pre, str):
        return DEFAULT_PREFIX
    text = _UNIT_SUFFIX.sub("", pre.strip())
    text = _PREFIX_TAIL.sub("", text)
    text = _PREFIX_UNSAFE.sub("", text)
    return text or DEFAULT_PREFIX


# ---------------------------------------------------------------------------------------------
# Origin
# ---------------------------------------------------------------------------------------------


def _snap(value: float) -> float:
    return math.floor(value / EE_SYMBOL_GRID + 0.5) * EE_SYMBOL_GRID


def _origin(data_str: Mapping[str, Any], what: str) -> tuple[float, float]:
    """Symbol origin in EasyEDA coordinates (see module docstring)."""
    bbox = data_str.get("BBox")
    if isinstance(bbox, Mapping):
        x = _numeric_entry(bbox, "x", what)
        y = _numeric_entry(bbox, "y", what)
        width = _numeric_entry(bbox, "width", what)
        height = _numeric_entry(bbox, "height", what)
        if width > 0.0 or height > 0.0:
            return _snap(x + width / 2.0), _snap(y + height / 2.0)
    head = data_str.get("head")
    if isinstance(head, Mapping):
        return (
            _numeric_entry(head, "x", what),
            _numeric_entry(head, "y", what),
        )
    return 0.0, 0.0


def _numeric_entry(source: Mapping[str, Any], key: str, what: str) -> float:
    raw = source.get(key)
    if raw is None:
        return 0.0
    if isinstance(raw, bool) or not isinstance(raw, int | float | str):
        raise ParseError(f"{what}: {key} must be a number, got {raw!r}")
    return _number(str(raw), f"{what}: {key}")


# ---------------------------------------------------------------------------------------------
# Path geometry shared by A, PT and ellipse shapes
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Edge:
    p1: Point
    p2: Point
    curve: float = 0.0


@dataclass
class _Run:
    edges: list[_Edge]
    closed: bool = False


def _point(tf: Transform, x: float, y: float) -> Point:
    """Transform a point; ``+ 0.0`` turns a negative zero (from the Y flip) into 0.0."""
    p = tf.point(x, y)
    if not (math.isfinite(p.x) and math.isfinite(p.y)):
        raise ParseError(f"coordinate out of range: {x!r}, {y!r}")
    return Point(p.x + 0.0, p.y + 0.0)


def _clamp_curve(curve: float) -> float:
    return max(-_MAX_CURVE_DEG, min(_MAX_CURVE_DEG, curve))


def _path_runs(data: str, tf: Transform) -> list[_Run]:
    """Convert SVG path data (EasyEDA coordinates) into runs of edges in millimetres.

    Single arc segments stay curved edges, Bezier curves are flattened.
    """
    runs: list[_Run] = []
    run: _Run | None = None
    cx = cy = sx = sy = 0.0
    have_start = False
    for seg in parse_path(data):
        if isinstance(seg, MoveTo):
            cx = sx = seg.x
            cy = sy = seg.y
            have_start = True
            run = None
            continue
        if not have_start:
            raise ParseError("SVG path draws before any moveto")
        if isinstance(seg, ClosePath):
            if run is not None:
                if (cx, cy) != (sx, sy):
                    run.edges.append(_Edge(_point(tf, cx, cy), _point(tf, sx, sy)))
                run.closed = True
            run = None
            cx, cy = sx, sy
            continue
        if run is None:
            run = _Run([])
            runs.append(run)
        if isinstance(seg, LineTo):
            run.edges.append(_Edge(_point(tf, cx, cy), _point(tf, seg.x, seg.y)))
            cx, cy = seg.x, seg.y
        elif isinstance(seg, ArcTo):
            curve = _clamp_curve(arc_curve_deg(cx, cy, seg))
            run.edges.append(_Edge(_point(tf, cx, cy), _point(tf, seg.x, seg.y), curve))
            cx, cy = seg.x, seg.y
        else:  # CubicTo / QuadTo, approximated by a polyline
            curve_path: list[Segment] = [MoveTo(cx, cy), seg]
            for sub in flatten_path(curve_path):
                px, py = sub.points[0]
                for qx, qy in sub.points[1:]:
                    run.edges.append(_Edge(_point(tf, px, py), _point(tf, qx, qy)))
                    px, py = qx, qy
            cx, cy = seg.x, seg.y
    return runs


def _poly_edges(points: Sequence[Point], closed: bool) -> list[_Edge]:
    edges = [_Edge(a, b) for a, b in itertools.pairwise(points)]
    if closed and len(points) > 2:
        edges.append(_Edge(points[-1], points[0]))
    return edges


def _run_polygon(run: _Run) -> Polygon | None:
    """Filled polygon for a run made of straight edges only, else None."""
    if not run.edges or any(e.curve != 0.0 for e in run.edges):
        return None
    vertices: list[Point] = [run.edges[0].p1]
    for edge in run.edges:
        if edge.p2 != vertices[-1]:
            vertices.append(edge.p2)
    if len(vertices) > 1 and vertices[0] == vertices[-1]:
        vertices.pop()
    if len(vertices) < 3:
        return None
    return Polygon(tuple(vertices), SYMBOL_WIRE_WIDTH_MM, Layer.SYMBOLS)


def _wires(edges: Sequence[_Edge]) -> list[Wire]:
    """Wires for edges; zero length edges are dropped."""
    wires: list[Wire] = []
    for e in edges:
        if e.p1 == e.p2:
            continue
        wires.append(
            Wire(e.p1.x, e.p1.y, e.p2.x, e.p2.y, SYMBOL_WIRE_WIDTH_MM, Layer.SYMBOLS, e.curve)
        )
    return wires


# ---------------------------------------------------------------------------------------------
# Pins
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _RawPin:
    name: str
    number: str
    x: float
    y: float
    length: PinLength
    length_error_mm: float
    angle: int
    direction: PinDirection
    inverted: bool
    clock: bool


def _eagle_name(raw: str) -> str:
    return _UNSAFE_NAME_CHARS.sub("_", raw.strip())


def _pin_length(length_mm: float) -> tuple[PinLength, float]:
    """Eagle keyword for a pin line length and the absolute mismatch in mm."""
    for limit, keyword, keyword_mm in _PIN_LENGTH_STEPS:
        if length_mm <= limit:
            return keyword, abs(keyword_mm - length_mm)
    keyword, keyword_mm = _PIN_LENGTH_LONG
    return keyword, abs(keyword_mm - length_mm)


def _section(sections: list[list[str]], index: int) -> list[str]:
    return sections[index] if index < len(sections) else []


def _pin_line_length_ee(path: str) -> float:
    """Length of the pin line in EasyEDA units; 0 for an empty path."""
    if not path.strip():
        return 0.0
    segments = parse_path(path)
    start: tuple[float, float] | None = None
    end: tuple[float, float] | None = None
    for seg in segments:
        if isinstance(seg, MoveTo):
            start = (seg.x, seg.y)
            end = start
        elif isinstance(seg, LineTo):
            end = (seg.x, seg.y)
    if start is None or end is None:
        return 0.0
    return math.hypot(end[0] - start[0], end[1] - start[1])


# ---------------------------------------------------------------------------------------------
# Symbol builder
# ---------------------------------------------------------------------------------------------


class _UnitBuilder:
    """Builds one ``Symbol`` from the shapes of one unit."""

    def __init__(
        self,
        name: str,
        data_str: Mapping[str, Any],
        shapes: Sequence[str],
        warnings: list[str],
    ) -> None:
        self.name = name
        self.shapes = shapes
        self.warnings = warnings
        self.tf = Transform(*_origin(data_str, f"Symbol {name}"))
        self.graphics: list[Graphic] = []
        self.raw_pins: list[_RawPin] = []
        self._warned_tags: set[str] = set()

    # -- public -------------------------------------------------------------------------------

    def build(self) -> Symbol:
        for raw in self.shapes:
            if not raw.strip():
                continue
            try:
                tag, fields = split_shape(raw)
                self._shape(tag, fields, raw)
            except (ParseError, ArithmeticError) as exc:
                snippet = raw.partition("~")[0][:8]
                raise ParseError(f"Symbol {self.name}: bad {snippet!r} shape: {exc}") from exc
        if not self.raw_pins:
            raise ParseError(f"Symbol {self.name} has no pins")
        pins = self._finish_pins()
        graphics = [*self.graphics, *build_name_value_labels(self.graphics, pins)]
        return Symbol(self.name, tuple(pins), tuple(graphics))

    # -- helpers ------------------------------------------------------------------------------

    def _warn(self, message: str) -> None:
        self.warnings.append(f"Symbol {self.name}: {message}")

    def _dropped(self, tag: str) -> None:
        self._warn(f"dropped degenerate {tag} shape")

    def _shape(self, tag: str, f: list[str], raw: str) -> None:
        if tag == "P":
            self.raw_pins.append(self._pin(raw))
        elif tag == "R":
            self._rect(f)
        elif tag == "C":
            self._circle(
                _number(field(f, 0), "cx"),
                _number(field(f, 1), "cy"),
                abs(_number(field(f, 2), "r")),
                _is_filled(field(f, 6)),
                tag,
            )
        elif tag == "E":
            self._ellipse(f)
        elif tag == "A":
            self._path(field(f, 0), filled=False, tag=tag)
        elif tag == "PL":
            self._polyline(f, closed=False, filled=False, tag=tag)
        elif tag == "PG":
            self._polyline(f, closed=True, filled=_is_filled(field(f, 4)), tag=tag)
        elif tag == "PT":
            self._path(field(f, 0), filled=_is_filled(field(f, 4)), tag=tag)
        elif tag in _IGNORED_TAGS:
            return
        elif tag not in self._warned_tags:
            self._warned_tags.add(tag)
            self._warn(f"unsupported shape {tag!r} ignored")

    # -- graphics -----------------------------------------------------------------------------

    def _rect(self, f: list[str]) -> None:
        x = _number(field(f, 0), "x")
        y = _number(field(f, 1), "y")
        w = _number(field(f, 4), "width")
        h = _number(field(f, 5), "height")
        corners = [
            _point(self.tf, x, y),
            _point(self.tf, x + w, y),
            _point(self.tf, x + w, y + h),
            _point(self.tf, x, y + h),
        ]
        wires = _wires(_poly_edges(corners, closed=True))
        if not wires:
            self._dropped("R")
        self.graphics.extend(wires)

    def _circle(self, cx: float, cy: float, r: float, filled: bool, tag: str) -> None:
        radius = self.tf.length(r)
        if radius <= 0.0:
            self._dropped(tag)
            return
        centre = _point(self.tf, cx, cy)
        width = 0.0 if filled else SYMBOL_WIRE_WIDTH_MM
        self.graphics.append(Circle(centre.x, centre.y, radius, width, Layer.SYMBOLS))

    def _ellipse(self, f: list[str]) -> None:
        cx = _number(field(f, 0), "cx")
        cy = _number(field(f, 1), "cy")
        rx = abs(_number(field(f, 2), "rx"))
        ry = abs(_number(field(f, 3), "ry"))
        filled = _is_filled(field(f, 7))
        if rx == 0.0 or ry == 0.0:
            self._dropped("E")
        elif math.isclose(rx, ry, rel_tol=_ELLIPSE_RATIO_TOL, abs_tol=_ELLIPSE_RATIO_TOL):
            self._circle(cx, cy, (rx + ry) / 2.0, filled, "E")
        else:
            points = [
                _point(
                    self.tf,
                    cx + rx * math.cos(2.0 * math.pi * i / _ELLIPSE_SEGMENTS),
                    cy + ry * math.sin(2.0 * math.pi * i / _ELLIPSE_SEGMENTS),
                )
                for i in range(_ELLIPSE_SEGMENTS)
            ]
            self._emit_run(_Run(_poly_edges(points, closed=True), closed=True), filled, "E")

    def _polyline(self, f: list[str], *, closed: bool, filled: bool, tag: str) -> None:
        coords = [_number(token, "point") for token in field(f, 0).replace(",", " ").split()]
        if len(coords) % 2 != 0:
            raise ParseError("odd number of coordinates")
        points = [_point(self.tf, coords[i], coords[i + 1]) for i in range(0, len(coords), 2)]
        self._emit_run(_Run(_poly_edges(points, closed), closed), filled, tag)

    def _path(self, data: str, *, filled: bool, tag: str) -> None:
        runs = _path_runs(data, self.tf)
        if not runs:
            self._dropped(tag)
        for run in runs:
            self._emit_run(run, filled, tag)

    def _emit_run(self, run: _Run, filled: bool, tag: str) -> None:
        if filled:
            polygon = _run_polygon(run)
            if polygon is not None:
                self.graphics.append(polygon)
                return
        wires = _wires(run.edges)
        if not wires:
            self._dropped(tag)
        self.graphics.extend(wires)

    # -- pins ---------------------------------------------------------------------------------

    def _pin(self, raw: str) -> _RawPin:
        sections = split_pin(raw)
        head = sections[0]
        spice = field(head, 3).strip()
        type_code = round(_number(field(head, 2), "pin type"))
        direction_text = EE_PIN_TYPE_TO_DIRECTION.get(type_code)
        if direction_text is None:
            self._warn(f"unknown pin type {type_code}, using passive")
            direction_text = EE_PIN_TYPE_TO_DIRECTION[0]
        if not field(head, 4).strip() or not field(head, 5).strip():
            raise ParseError("pin has no position")
        x = _number(field(head, 4), "pin x")
        y = _number(field(head, 5), "pin y")
        ee_rotation = _number(field(head, 6), "pin rotation")
        quarter = round(ee_rotation / _RIGHT_ANGLE_DEG)
        if abs(ee_rotation - quarter * _RIGHT_ANGLE_DEG) > 1e-6:
            self._warn(f"pin rotation {ee_rotation:g} snapped to a multiple of 90")
        angle = (quarter * _RIGHT_ANGLE_DEG + _PIN_ANGLE_OFFSET_DEG) % _FULL_TURN_DEG

        name_text = field(_section(sections, 3), 4)
        number = field(_section(sections, 4), 4).strip() or spice
        if not number:
            raise ParseError("pin without a number")

        length_mm = self._pin_length_mm(_section(sections, 2), number)
        length, error_mm = _pin_length(length_mm)
        point = _point(self.tf, x, y)
        return _RawPin(
            name=_eagle_name(name_text) or number,
            number=number,
            x=point.x,
            y=point.y,
            length=length,
            length_error_mm=error_mm,
            angle=angle,
            direction=PinDirection(direction_text),
            inverted=_is_true(field(_section(sections, 5), 0)),
            clock=_is_true(field(_section(sections, 6), 0)),
        )

    def _pin_length_mm(self, section: list[str], number: str) -> float:
        try:
            return self.tf.length(_pin_line_length_ee(field(section, 0)))
        except ParseError as exc:
            self._warn(f"pin {number}: unreadable pin line ({exc}), length set to 0")
            return 0.0

    def _finish_pins(self) -> list[Pin]:
        counts: dict[str, int] = {}
        for pin in self.raw_pins:
            counts[pin.name.casefold()] = counts.get(pin.name.casefold(), 0) + 1
        seen: dict[str, int] = {}
        pins: list[Pin] = []
        for raw in self.raw_pins:
            key = raw.name.casefold()
            name = raw.name
            if counts[key] > 1:
                seen[key] = seen.get(key, 0) + 1
                name = f"{raw.name}@{seen[key]}"
            pins.append(
                Pin(
                    name=name,
                    number=raw.number,
                    x=raw.x,
                    y=raw.y,
                    length=raw.length,
                    angle=raw.angle,
                    direction=raw.direction,
                    inverted=raw.inverted,
                    clock=raw.clock,
                )
            )
        self._pin_warnings()
        return pins

    def _pin_warnings(self) -> None:
        off = [p for p in self.raw_pins if p.length_error_mm > _PIN_LENGTH_WARN_MM]
        if off:
            worst = max(p.length_error_mm for p in off)
            self._warn(
                f"{len(off)} pin(s) have a length that does not match an Eagle pin length "
                f"(worst mismatch {worst:.2f} mm)"
            )
        numbers: dict[str, int] = {}
        for pin in self.raw_pins:
            numbers[pin.number] = numbers.get(pin.number, 0) + 1
        repeated = sorted(n for n, c in numbers.items() if c > 1)
        if repeated:
            self._warn(f"repeated pin numbers: {', '.join(repeated)}")


def _wire_extent_points(wire: Wire) -> list[tuple[float, float]]:
    """Endpoints of a wire plus samples along its arc when it is curved."""
    p1 = (wire.x1, wire.y1)
    p2 = (wire.x2, wire.y2)
    if wire.curve == 0.0:
        return [p1, p2]
    theta = math.radians(wire.curve)
    chord = math.hypot(wire.x2 - wire.x1, wire.y2 - wire.y1)
    half_angle = abs(theta) / 2.0
    # Distance from the chord midpoint to the centre, towards the left of p1 -> p2 for CCW arcs.
    offset = (chord / 2.0) / math.tan(half_angle)
    ux, uy = (wire.x2 - wire.x1) / chord, (wire.y2 - wire.y1) / chord
    side = 1.0 if theta > 0 else -1.0
    cx = (wire.x1 + wire.x2) / 2.0 - uy * offset * side
    cy = (wire.y1 + wire.y2) / 2.0 + ux * offset * side
    start = math.atan2(wire.y1 - cy, wire.x1 - cx)
    radius = math.hypot(wire.x1 - cx, wire.y1 - cy)
    return [
        (
            cx + radius * math.cos(start + theta * i / _ARC_BBOX_SAMPLES),
            cy + radius * math.sin(start + theta * i / _ARC_BBOX_SAMPLES),
        )
        for i in range(_ARC_BBOX_SAMPLES + 1)
    ]


def _graphics_extent(graphics: Sequence[Graphic]) -> tuple[list[float], list[float]]:
    """X and Y coordinates that bound the graphics (empty lists when there are none)."""
    points: list[tuple[float, float]] = []
    for g in graphics:
        if isinstance(g, Wire):
            points.extend(_wire_extent_points(g))
        elif isinstance(g, Circle):
            points.extend([(g.x - g.radius, g.y - g.radius), (g.x + g.radius, g.y + g.radius)])
        elif isinstance(g, Polygon):
            points.extend((p.x, p.y) for p in g.points)
    return [p[0] for p in points], [p[1] for p in points]


def build_name_value_labels(graphics: Sequence[Graphic], pins: Sequence[Pin]) -> list[Label]:
    """``>NAME`` (above) and ``>VALUE`` (below) labels for a symbol.

    Both sit at the left edge of the graphics bounding box. ``graphics`` are the symbol graphics
    without labels; the Y position clears them and the pin connection points, so the labels do
    not collide with pins on the top or bottom edge. Shared by the parser and by symbol fitting.
    """
    xs, ys = _graphics_extent(graphics)
    pin_xs = [p.x for p in pins]
    pin_ys = [p.y for p in pins]
    left = min(xs) if xs else min(pin_xs)
    top = max([*ys, *pin_ys])
    bottom = min([*ys, *pin_ys])
    return [
        Label(
            round(left, 6),
            round(top + _LABEL_GAP_MM, 6),
            _NAME_TEXT,
            LABEL_SIZE_SYMBOL_MM,
            Layer.NAMES,
            align=TextAlign.BOTTOM_LEFT,
        ),
        Label(
            round(left, 6),
            round(bottom - _LABEL_GAP_MM, 6),
            _VALUE_TEXT,
            LABEL_SIZE_SYMBOL_MM,
            Layer.VALUES,
            align=TextAlign.TOP_LEFT,
        ),
    ]


# ---------------------------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------------------------


def parse_symbol_units(
    result: Mapping[str, Any], *, base_name: str
) -> tuple[list[Symbol], list[str]]:
    """Parse the symbol(s) of a component document ``result`` object.

    Returns one ``Symbol`` per unit (named ``base_name`` for one unit, ``base_name_1``,
    ``base_name_2``... for several) and a list of warnings. Raises ``ParseError`` for malformed
    input, a missing ``dataStr`` or a symbol without pins.
    """
    main = _data_str(result, "Component")
    main_shapes = _shape_strings(main, "Component symbol")
    subparts = _as_list(result.get("subparts"))
    warnings: list[str] = []

    units: list[tuple[Mapping[str, Any], list[str]]]
    if subparts and not _has_pins(main_shapes):
        units = []
        for index, sub in enumerate(subparts, start=1):
            what = f"Subpart {index}"
            sub_data = _data_str(sub, what)
            sub_shapes = _shape_strings(sub_data, what)
            if _has_pins(sub_shapes):
                units.append((sub_data, sub_shapes))
            else:
                warnings.append(f"{what} has no pins and was skipped")
        if not units:
            raise ParseError("Multi-unit symbol has no unit with pins")
    else:
        if subparts:
            warnings.append("Part has subparts but the main symbol has pins; subparts ignored")
        units = [(main, main_shapes)]

    symbols: list[Symbol] = []
    for index, (data_str, shapes) in enumerate(units, start=1):
        name = base_name if len(units) == 1 else f"{base_name}_{index}"
        symbols.append(_UnitBuilder(name, data_str, shapes, warnings).build())
    return symbols, warnings
