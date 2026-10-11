"""EasyEDA footprint (`result.packageDetail`) to the internal `Package` model.

Field layouts of the `~` separated shape strings were derived from real responses, see
docs/RESEARCH.md 1.8. Every unit conversion and the Y flip go through `parser.units.Transform`,
layers go through `constants.EE_FOOTPRINT_LAYER_TO_EAGLE`.

The result is the pads and holes of the footprint, its drawing shapes (silk, documentation),
plus a generated courtyard on tKeepout and `>NAME` / `>VALUE` labels (DECISIONS D-011).

Conventions worth knowing:
- `Pad.number` is the raw EasyEDA number (stripped). Real data has numbers such as `41`, `EP`,
  `A1B12` and no parenthesised ones, so `Pad.number` is never rewritten: the symbol parser must
  see the same text. `Pad.name` is the unique Eagle name: whitespace, `;` and parentheses are
  removed (`A(1)` becomes `A1`) and repeats get `_2`, `_3`, ... (DECISIONS D-008).
- Pad `rotation` is counter-clockwise in the Y-up frame and `width`/`height` are in the pad's own
  frame (not swapped for 90 degree pads). EasyEDA positive rotation is counter-clockwise on
  screen, see tests/test_units.py. POLYGON pads carry their absolute outline instead.
- The courtyard covers pads, polygon outlines, drills and holes only, not the
  body outline shapes.
"""

from __future__ import annotations

import itertools
import json
import math
import re
from collections.abc import Mapping
from typing import Any

from lcsc2fusion.constants import (
    COURTYARD_MARGIN_MM,
    COURTYARD_WIDTH_MM,
    DOCU_WIDTH_MM,
    EE_FOOTPRINT_LAYER_TO_EAGLE,
    EE_LAYER_MULTI,
    LABEL_SIZE_PACKAGE_MM,
    MIN_POLYGON_WIDTH_MM,
    Layer,
)
from lcsc2fusion.errors import ParseError
from lcsc2fusion.model import (
    Circle,
    Graphic,
    Hole,
    Label,
    Package,
    Pad,
    PadShape,
    Point,
    Polygon,
    TextAlign,
    Wire,
)
from lcsc2fusion.parser.shapes import field, split_shape, to_float
from lcsc2fusion.parser.svgpath import (
    ArcTo,
    ClosePath,
    CubicTo,
    LineTo,
    MoveTo,
    QuadTo,
    Segment,
    arc_curve_deg,
    flatten_path,
    parse_path,
)
from lcsc2fusion.parser.units import Transform, ee_rotation_to_ccw

# Courtyard corners are snapped outward to this grid (mm). The snap tolerance is expressed in
# grid steps and absorbs the 6 digit rounding done by Transform.
COURTYARD_SNAP_MM = 0.05
_SNAP_EPS_STEPS = 1e-4
# Field values beyond this magnitude (EasyEDA units) are corrupt; real footprints stay within 1e5.
# Rejecting them keeps the arithmetic below finite, so no OverflowError can escape.
_MAX_ABS_VALUE = 1e9
# A hole is a slot when its total length exceeds the drill diameter by more than this (mm).
_SLOT_EPS_MM = 1e-4
# Slot axes are undirected: angles are reported in [0, 180).
_HALF_TURN_DEG = 180.0
_QUARTER_TURN_DEG = 90.0

_PAD_SHAPES: dict[str, PadShape] = {
    "RECT": PadShape.RECT,
    "OVAL": PadShape.OVAL,
    "ELLIPSE": PadShape.ROUND,
    "POLYGON": PadShape.POLYGON,
}
# Characters removed from pad names: whitespace, ';' and parentheses (`A(1)` becomes `A1`).
_PAD_NAME_STRIP = re.compile(r"[\s;()]+")
_NUMBER_SPLIT = re.compile(r"[,\s]+")

# Indices into the fields that follow the shape tag (RESEARCH 1.8 counts the tag as index 0).
_PAD_SHAPE, _PAD_X, _PAD_Y, _PAD_W, _PAD_H, _PAD_LAYER = 0, 1, 2, 3, 4, 5
_PAD_NUMBER, _PAD_HOLE_RADIUS, _PAD_POINTS, _PAD_ROTATION = 7, 8, 9, 10
_PAD_HOLE_LENGTH, _PAD_SLOT_OUTLINE, _PAD_PLATED = 12, 13, 14
_TRACK_WIDTH, _TRACK_LAYER, _TRACK_POINTS = 0, 1, 3
_CIRCLE_X, _CIRCLE_Y, _CIRCLE_R, _CIRCLE_WIDTH, _CIRCLE_LAYER = 0, 1, 2, 3, 4
_ARC_WIDTH, _ARC_LAYER, _ARC_PATH = 0, 1, 3
_RECT_X, _RECT_Y, _RECT_W, _RECT_H, _RECT_LAYER, _RECT_WIDTH = 0, 1, 2, 3, 4, 7
_HOLE_X, _HOLE_Y, _HOLE_RADIUS = 0, 1, 2
_REGION_LAYER, _REGION_PATH, _REGION_KIND = 0, 2, 3

_Bounds = tuple[float, float, float, float]  # xmin, ymin, xmax, ymax


def parse_footprint(package_detail: Mapping[str, Any]) -> tuple[Package, list[str]]:
    """Convert an EasyEDA `packageDetail` object into a `Package`.

    Returns the package and a list of human readable warnings (approximations, ignored shapes).
    Raises ParseError for structurally broken input or a footprint without pads and holes.
    """
    return _FootprintParser(package_detail).run()


# ============================================================
# Small input helpers. All of them raise ParseError instead of KeyError/ValueError.
# ============================================================


def _num(text: str, default: float = 0.0) -> float:
    """Parse a numeric shape field (empty means default) and reject NaN, inf and absurd sizes."""
    value = to_float(text, default)
    if not math.isfinite(value) or abs(value) > _MAX_ABS_VALUE:
        raise ParseError(f"Number out of range: {text!r}")
    return value


def _floats(text: str) -> list[float]:
    """Parse 'x y x y ...' (space or comma separated) into numbers."""
    return [_num(token) for token in _NUMBER_SPLIT.split(text.strip()) if token]


def _json_number(value: object, what: str, default: float) -> float:
    """Number from a JSON value that EasyEDA may deliver as a number or as a string."""
    if value is None or value == "":
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ParseError(f"{what} is not a number: {value!r}")
    return _num(str(value))


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _as_mapping(value: object, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ParseError(f"{what} is missing or not an object")
    return value


def _clean(value: float) -> float:
    """Normalise negative zero, which Transform produces for points on the origin axes."""
    return value + 0.0


def _bounds_of(points: list[Point]) -> _Bounds:
    xs = [p.x for p in points]
    ys = [p.y for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _merge(a: _Bounds | None, b: _Bounds) -> _Bounds:
    if a is None:
        return b
    return min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])


def _snap_down(value: float) -> float:
    return round(math.floor(value / COURTYARD_SNAP_MM + _SNAP_EPS_STEPS) * COURTYARD_SNAP_MM, 6)


def _snap_up(value: float) -> float:
    return round(math.ceil(value / COURTYARD_SNAP_MM - _SNAP_EPS_STEPS) * COURTYARD_SNAP_MM, 6)


def _pad_bounds(pad: Pad) -> _Bounds:
    """Axis aligned extent of a pad (copper outline, and the slot or hole when larger)."""
    if pad.shape is PadShape.POLYGON and pad.outline:
        bounds = _bounds_of(list(pad.outline))
    else:
        rad = math.radians(pad.rotation)
        c, s = abs(math.cos(rad)), abs(math.sin(rad))
        half_w, half_h = pad.width / 2.0, pad.height / 2.0
        if pad.shape is PadShape.ROUND:
            ex, ey = math.hypot(half_w * c, half_h * s), math.hypot(half_w * s, half_h * c)
        elif pad.shape is PadShape.OVAL:
            r = min(half_w, half_h)
            a = max(half_w, half_h) - r  # half length of the stadium centre line
            ex, ey = (a * c + r, a * s + r) if half_w >= half_h else (a * s + r, a * c + r)
        else:
            ex, ey = half_w * c + half_h * s, half_w * s + half_h * c
        bounds = (pad.x - ex, pad.y - ey, pad.x + ex, pad.y + ey)
    if pad.drill > 0.0:
        r = pad.drill / 2.0
        a = max(pad.slot_length - pad.drill, 0.0) / 2.0
        rad = math.radians(pad.slot_angle)
        ex, ey = a * abs(math.cos(rad)) + r, a * abs(math.sin(rad)) + r
        bounds = _merge(bounds, (pad.x - ex, pad.y - ey, pad.x + ex, pad.y + ey))
    return bounds


class _FootprintParser:
    """One parse run. Holds the transform, the collected objects and the warnings."""

    def __init__(self, package_detail: Mapping[str, Any]) -> None:
        if not isinstance(package_detail, Mapping):
            raise ParseError("Footprint (packageDetail) is not a JSON object")
        data_str: object = package_detail.get("dataStr")
        if isinstance(data_str, str):
            try:
                data_str = json.loads(data_str)
            except json.JSONDecodeError as exc:
                raise ParseError(f"Footprint dataStr is not valid JSON: {exc}") from exc
        data = _as_mapping(data_str, "Footprint dataStr")
        head = _as_mapping(data.get("head"), "Footprint dataStr.head")
        c_para_raw = head.get("c_para")
        c_para: Mapping[str, Any] = c_para_raw if isinstance(c_para_raw, Mapping) else {}
        shapes = data.get("shape")
        if not isinstance(shapes, (list, tuple)):
            raise ParseError("Footprint dataStr.shape is missing or not a list")

        self.warnings: list[str] = []
        origin_x = self._head_coordinate(head, "x")
        origin_y = self._head_coordinate(head, "y")
        self.tf = Transform(origin_x, origin_y)
        title = _text(package_detail.get("title"))
        ee_package = _text(c_para.get("package"))
        self.name = title or ee_package
        if not self.name:
            raise ParseError("Footprint has neither a title nor c_para.package")
        self.description = (
            self.name
            if not ee_package or ee_package == self.name
            else f"{self.name} (EasyEDA package {ee_package})"
        )
        self.shapes: list[object] = list(shapes)

        self.pads: list[Pad] = []
        self.holes: list[Hole] = []
        self.graphics: list[Graphic] = []
        self._used_names: set[str] = set()
        self._unknown_layers: set[int] = set()
        self._unknown_tags: set[str] = set()
        self._ignored: dict[str, int] = {}
        self._polygon_pads: list[str] = []

    def _head_coordinate(self, head: Mapping[str, Any], key: str) -> float:
        if head.get(key) in (None, ""):
            self.warnings.append(f"Footprint dataStr.head has no {key}, origin {key} assumed 0")
        return _json_number(head.get(key), f"dataStr.head.{key}", 0.0)

    # driver

    def run(self) -> tuple[Package, list[str]]:
        for index, raw in enumerate(self.shapes):
            if not isinstance(raw, str):
                raise ParseError(f"Footprint shape #{index} is not a string")
            if not raw.strip():
                continue
            tag, fields = split_shape(raw)
            if tag == "PAD":
                try:
                    self._pad(fields)
                except ParseError as exc:
                    raise ParseError(f"Bad PAD shape #{index}: {exc}") from exc
                continue
            try:
                self._graphic(tag, fields)
            except ParseError as exc:
                self.warnings.append(f"Skipped {tag} shape #{index}: {exc}")
        if not self.pads and not self.holes:
            raise ParseError(f"Footprint {self.name!r} has no pads or holes")
        self._summarise_ignored()
        self._add_courtyard_and_labels()
        package = Package(
            name=self.name,
            description=self.description,
            pads=tuple(self.pads),
            holes=tuple(self.holes),
            graphics=tuple(self.graphics),
        )
        return package, self.warnings

    def _graphic(self, tag: str, fields: list[str]) -> None:
        if tag == "TRACK":
            self._track(fields)
        elif tag == "ARC":
            self._arc(fields)
        elif tag == "CIRCLE":
            self._circle(fields)
        elif tag == "RECT":
            self._rect(fields)
        elif tag == "HOLE":
            self._hole(fields)
        elif tag == "SOLIDREGION":
            self._solid_region(fields)
        elif tag in ("TEXT", "SVGNODE"):
            return  # labels are generated, SVGNODE only carries the 3D model reference
        elif tag in ("VIA", "COPPERAREA"):
            self._ignored[tag] = self._ignored.get(tag, 0) + 1
        elif tag not in self._unknown_tags:
            self._unknown_tags.add(tag)
            self.warnings.append(f"Ignored unknown EasyEDA footprint shape type {tag!r}")

    def _summarise_ignored(self) -> None:
        for key, count in sorted(self._ignored.items()):
            self.warnings.append(f"Ignored {count} {key} shape(s): not representable in Eagle")
        if self._polygon_pads:
            names = ", ".join(self._polygon_pads)
            self.warnings.append(
                f"{len(self._polygon_pads)} polygon pad(s) ({names}) are approximated "
                "by the writer using the outline bounding box"
            )

    # helpers

    def _point(self, x: float, y: float) -> Point:
        p = self.tf.point(x, y)
        return Point(_clean(p.x), _clean(p.y))

    def _layer(self, text: str) -> Layer | None:
        """Eagle layer for an EasyEDA layer id, or None when the shape must be skipped."""
        if not text.strip():
            raise ParseError("shape has no layer")
        ee_layer = round(_num(text))
        if ee_layer in EE_FOOTPRINT_LAYER_TO_EAGLE:
            return EE_FOOTPRINT_LAYER_TO_EAGLE[ee_layer]
        if ee_layer not in self._unknown_layers:
            self._unknown_layers.add(ee_layer)
            self.warnings.append(f"Shapes on unsupported EasyEDA layer {ee_layer} were skipped")
        return None

    def _stroke(self, width_ee: float) -> float:
        width = self.tf.length(width_ee)
        return width if width > 0.0 else DOCU_WIDTH_MM

    def _unique_name(self, number: str, ordinal: int) -> str:
        base = _PAD_NAME_STRIP.sub("", number) or f"P${ordinal}"
        name, suffix = base, 1
        while name.lower() in self._used_names:
            suffix += 1
            name = f"{base}_{suffix}"
        self._used_names.add(name.lower())
        return name

    # pads

    def _pad(self, f: list[str]) -> None:
        ordinal = len(self.pads) + 1
        number = field(f, _PAD_NUMBER).strip()
        if not number:
            self.warnings.append(f"Pad #{ordinal} has no number and is named P${ordinal}")
        name = self._unique_name(number, ordinal)
        label = f"pad {number or name!r}"

        ee_shape = field(f, _PAD_SHAPE).strip().upper()
        shape = _PAD_SHAPES.get(ee_shape)
        if shape is None:
            self.warnings.append(f"Unknown shape {ee_shape!r} of {label}, treated as RECT")
            shape = PadShape.RECT
        center = self._point(_num(field(f, _PAD_X)), _num(field(f, _PAD_Y)))
        width = self.tf.length(_num(field(f, _PAD_W)))
        height = self.tf.length(_num(field(f, _PAD_H)))
        rotation = round(ee_rotation_to_ccw(_num(field(f, _PAD_ROTATION))), 6) % 360.0

        radius_ee = _num(field(f, _PAD_HOLE_RADIUS))
        if radius_ee < 0.0:
            raise ParseError(f"{label} has a negative hole radius")
        drill = self.tf.length(2.0 * radius_ee)  # diameter = 2 * radius
        layer = self._pad_layer(field(f, _PAD_LAYER), label, drill)
        plated = field(f, _PAD_PLATED).strip().upper() != "N"

        outline: tuple[Point, ...] = ()
        if shape is PadShape.POLYGON:
            outline = self._pad_outline(field(f, _PAD_POINTS))
            if len(outline) < 3:
                self.warnings.append(f"{label} is a POLYGON without an outline, treated as RECT")
                shape = PadShape.RECT
            else:
                # The outline is absolute and already includes the rotation.
                x_min, y_min, x_max, y_max = _bounds_of(list(outline))
                center = Point(
                    _clean(round((x_min + x_max) / 2.0, 6)), _clean(round((y_min + y_max) / 2.0, 6))
                )
                width, height = round(x_max - x_min, 6), round(y_max - y_min, 6)
                rotation = 0.0
                self._polygon_pads.append(name)
        if width <= 0.0 or height <= 0.0:
            raise ParseError(f"{label} has a non-positive size")

        slot_length, slot_angle = 0.0, 0.0
        if drill > 0.0:
            total = self.tf.length(_num(field(f, _PAD_HOLE_LENGTH)))
            if total - drill > _SLOT_EPS_MM:
                slot_length = total
                slot_angle = self._slot_angle(field(f, _PAD_SLOT_OUTLINE), width, height, rotation)
        self.pads.append(
            Pad(
                name=name,
                number=number,
                x=center.x,
                y=center.y,
                width=width,
                height=height,
                shape=shape,
                rotation=rotation,
                layer=layer,
                drill=drill,
                slot_length=slot_length,
                slot_angle=slot_angle,
                plated=plated,
                outline=outline,
            )
        )

    def _pad_layer(self, text: str, label: str, drill: float) -> Layer:
        """Copper layer of a pad. Through-hole pads (multi layer) are reported on top."""
        if not text.strip():
            self.warnings.append(f"{label} has no layer, assumed top")
            return Layer.TOP
        ee_layer = round(_num(text))
        if ee_layer == EE_LAYER_MULTI:
            if drill <= 0.0:
                self.warnings.append(f"{label} is on the multi layer without a hole, made SMD")
            return Layer.TOP
        mapped = EE_FOOTPRINT_LAYER_TO_EAGLE.get(ee_layer)
        if mapped in (Layer.TOP, Layer.BOTTOM):
            return mapped
        self.warnings.append(f"{label} is on unsupported EasyEDA layer {ee_layer}, assumed top")
        return Layer.TOP

    def _pad_outline(self, text: str) -> tuple[Point, ...]:
        values = _floats(text)
        if len(values) % 2:
            raise ParseError("pad outline has an odd number of coordinates")
        points: list[Point] = []
        for i in range(0, len(values), 2):
            point = self._point(values[i], values[i + 1])
            if not points or points[-1] != point:
                points.append(point)
        if len(points) > 1 and points[0] == points[-1]:
            points.pop()
        return tuple(points)

    def _slot_angle(self, outline_text: str, width: float, height: float, rotation: float) -> float:
        """Slot axis angle in [0, 180), Y up. Uses the slot centre line when present."""
        values = _floats(outline_text)
        if len(values) >= 4:
            p1 = self._point(values[0], values[1])
            p2 = self._point(values[2], values[3])
            if p1 != p2:
                angle = math.degrees(math.atan2(p2.y - p1.y, p2.x - p1.x))
                return round(angle, 6) % _HALF_TURN_DEG
        base = 0.0 if width >= height else _QUARTER_TURN_DEG
        return round(base + rotation, 6) % _HALF_TURN_DEG

    # drawing shapes

    def _wire(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        width: float,
        layer: Layer,
        curve: float = 0.0,
    ) -> None:
        p1, p2 = self._point(*start), self._point(*end)
        if p1 != p2:
            self.graphics.append(Wire(p1.x, p1.y, p2.x, p2.y, width, layer, round(curve, 6)))

    def _track(self, f: list[str]) -> None:
        layer = self._layer(field(f, _TRACK_LAYER))
        if layer is None:
            return
        width = self._stroke(_num(field(f, _TRACK_WIDTH)))
        values = _floats(field(f, _TRACK_POINTS))
        if len(values) < 4 or len(values) % 2:
            raise ParseError("track needs at least two complete points")
        points = list(zip(values[0::2], values[1::2], strict=True))
        for start, end in itertools.pairwise(points):
            self._wire(start, end, width, layer)

    def _arc(self, f: list[str]) -> None:
        layer = self._layer(field(f, _ARC_LAYER))
        if layer is None:
            return
        width = self._stroke(_num(field(f, _ARC_WIDTH)))
        segments = parse_path(field(f, _ARC_PATH))
        self._path_wires(segments, width, layer)

    def _path_wires(self, segments: list[Segment], width: float, layer: Layer) -> None:
        """Turn path segments into wires; arcs become curved wires, Beziers are flattened."""
        current: tuple[float, float] | None = None
        start: tuple[float, float] | None = None
        for seg in segments:
            if isinstance(seg, MoveTo):
                current = start = (seg.x, seg.y)
                continue
            if current is None or start is None:
                raise ParseError("path draws before any moveto")
            if isinstance(seg, ClosePath):
                self._wire(current, start, width, layer)
                current = start
            elif isinstance(seg, LineTo):
                self._wire(current, (seg.x, seg.y), width, layer)
                current = (seg.x, seg.y)
            elif isinstance(seg, ArcTo):
                curve = arc_curve_deg(current[0], current[1], seg)
                if curve != 0.0:  # degenerate arcs are dropped
                    self._wire(current, (seg.x, seg.y), width, layer, curve)
                current = (seg.x, seg.y)
            elif isinstance(seg, (CubicTo, QuadTo)):
                for sub in flatten_path([MoveTo(*current), seg]):
                    for p, q in itertools.pairwise(sub.points):
                        self._wire(p, q, width, layer)
                current = (seg.x, seg.y)

    def _circle(self, f: list[str]) -> None:
        layer = self._layer(field(f, _CIRCLE_LAYER))
        if layer is None:
            return
        radius = self.tf.length(_num(field(f, _CIRCLE_R)))
        if radius <= 0.0:
            raise ParseError("circle radius must be positive")
        center = self._point(_num(field(f, _CIRCLE_X)), _num(field(f, _CIRCLE_Y)))
        width = self.tf.length(_num(field(f, _CIRCLE_WIDTH)))  # 0 means a filled disc
        self.graphics.append(Circle(center.x, center.y, radius, width, layer))

    def _rect(self, f: list[str]) -> None:
        layer = self._layer(field(f, _RECT_LAYER))
        if layer is None:
            return
        x, y = _num(field(f, _RECT_X)), _num(field(f, _RECT_Y))
        w, h = _num(field(f, _RECT_W)), _num(field(f, _RECT_H))
        if w == 0.0 or h == 0.0:
            raise ParseError("rectangle has zero width or height")
        width = self._stroke(_num(field(f, _RECT_WIDTH)))
        # EasyEDA stores the top-left corner in the Y-down frame.
        corners = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
        for i, corner in enumerate(corners):
            self._wire(corner, corners[(i + 1) % len(corners)], width, layer)

    def _hole(self, f: list[str]) -> None:
        radius = _num(field(f, _HOLE_RADIUS))
        if radius <= 0.0:
            raise ParseError("hole radius must be positive")
        center = self._point(_num(field(f, _HOLE_X)), _num(field(f, _HOLE_Y)))
        self.holes.append(Hole(center.x, center.y, self.tf.length(2.0 * radius)))

    def _solid_region(self, f: list[str]) -> None:
        layer = self._layer(field(f, _REGION_LAYER))
        if layer is None:
            return
        kind = field(f, _REGION_KIND).strip().lower() or "solid"
        if kind != "solid":
            key = f"SOLIDREGION {kind}"
            self._ignored[key] = self._ignored.get(key, 0) + 1
            return
        for sub in flatten_path(parse_path(field(f, _REGION_PATH))):
            points: list[Point] = []
            for x, y in sub.points:
                point = self._point(x, y)
                if not points or points[-1] != point:
                    points.append(point)
            if len(points) > 1 and points[0] == points[-1]:
                points.pop()
            if len(points) >= 3:
                self.graphics.append(Polygon(tuple(points), MIN_POLYGON_WIDTH_MM, layer))

    # generated geometry (D-011)

    def _add_courtyard_and_labels(self) -> None:
        bounds: _Bounds | None = None
        for pad in self.pads:
            bounds = _merge(bounds, _pad_bounds(pad))
        for hole in self.holes:
            r = hole.drill / 2.0
            bounds = _merge(bounds, (hole.x - r, hole.y - r, hole.x + r, hole.y + r))
        if bounds is None:  # unreachable: run() rejects footprints without pads and holes
            raise ParseError("Footprint has no geometry")
        x1 = _snap_down(bounds[0] - COURTYARD_MARGIN_MM)
        y1 = _snap_down(bounds[1] - COURTYARD_MARGIN_MM)
        x2 = _snap_up(bounds[2] + COURTYARD_MARGIN_MM)
        y2 = _snap_up(bounds[3] + COURTYARD_MARGIN_MM)
        corners = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
        for i, (ax, ay) in enumerate(corners):
            bx, by = corners[(i + 1) % len(corners)]
            self.graphics.append(Wire(ax, ay, bx, by, COURTYARD_WIDTH_MM, Layer.T_KEEPOUT))
        mid_x = round((x1 + x2) / 2.0, 6)
        self.graphics.append(
            Label(
                mid_x,
                y2,
                ">NAME",
                LABEL_SIZE_PACKAGE_MM,
                Layer.T_NAMES,
                align=TextAlign.BOTTOM_CENTER,
            )
        )
        self.graphics.append(
            Label(
                mid_x,
                y1,
                ">VALUE",
                LABEL_SIZE_PACKAGE_MM,
                Layer.T_VALUES,
                align=TextAlign.TOP_CENTER,
            )
        )
