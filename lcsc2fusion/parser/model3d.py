"""EasyEDA 3D model reference (the `SVGNODE` shape of a footprint) to the internal `Model3D`.

The footprint `shape` list may contain one `SVGNODE~{json}` entry. Its JSON describes where the
model sits relative to the footprint (field layout checked on 24 real responses):

- `attrs.uuid`: model id, the key of the STEP download (`head.uuid_3d` is a different id).
- `attrs.title`: model name.
- `attrs.c_origin`: "x,y" in absolute canvas units, the placement of the model bounding box
  centre. Same frame as the footprint shapes, so the package origin is `dataStr.head.x/y`.
- `attrs.z`: height of the lowest point of the model above the board (EasyEDA units, the sign is
  kept: negative values sink the model into the board, as THT pins do).
- `attrs.c_rotation`: "rx,ry,rz" in degrees. Only rz is ever non-zero in the real data.
- `attrs.c_width` / `attrs.c_height`: native, unrotated X and Y size of the model.
- `childNodes[].attrs.points`: "x y x y ..." absolute canvas coordinates, the XY outline of the
  model as placed (rotation applied).

`c_origin` is sometimes stale (4 of 23 real parts): it then points anywhere from half a
millimetre to over a metre away from the model. The outline is always right, so when the outline
bounding box centre disagrees with `c_origin` by more than STALE_ORIGIN_TOLERANCE_MM the outline
centre wins and `placement_source` says so.

All unit and axis conversion goes through `parser.units` (`Transform` flips Y and converts to mm,
`ee_to_mm` converts lengths). A malformed or absent 3D node never fails the part: the result is
`(None, [warning])` (or `(None, [])` when there is no node at all).
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any

from lcsc2fusion.errors import ParseError
from lcsc2fusion.model import Model3D, Point
from lcsc2fusion.parser.shapes import split_shape
from lcsc2fusion.parser.units import MM_DECIMALS, Transform, ee_to_mm

PLACEMENT_C_ORIGIN = "c_origin"
PLACEMENT_OUTLINE_CENTRE = "outline_centre"

# Every warning produced here starts with this prefix. The warnings travel in `Part.warnings`
# even when nobody asked for 3D output, so the service filters on the prefix (see service.py).
MODEL_WARNING_PREFIX = "3D model: "

# A c_origin further than this (mm) from the outline centre is treated as stale.
STALE_ORIGIN_TOLERANCE_MM = 0.1
# Values beyond this magnitude (EasyEDA units) are corrupt; real canvases stay within a few 1e3.
_MAX_ABS_VALUE = 1e6
# Rotations about X or Y closer than this to a multiple of 360 degrees count as zero.
_ANGLE_EPS_DEG = 1e-6
_FULL_TURN_DEG = 360.0

_UUID = re.compile(r"[0-9a-f]{32}")
_NUMBER_SPLIT = re.compile(r"[,\s]+")

_Bounds = tuple[float, float, float, float]  # xmin, ymin, xmax, ymax


def parse_model3d(package_detail: Mapping[str, Any]) -> tuple[Model3D | None, list[str]]:
    """Read the 3D model placement from an EasyEDA `packageDetail` object.

    Returns `(None, [])` when the footprint has no SVGNODE, `(None, [warning])` when the node is
    unusable, otherwise the model and the warnings about it. Never raises ParseError. The footprint
    parser reports a missing `head.x` / `head.y`; here a missing value is taken as 0 as well so
    both agree on the package origin.
    """
    warnings: list[str] = []
    try:
        data = _footprint_data(package_detail)
        payloads = _svgnode_payloads(data)
        if not payloads:
            return None, warnings
        if len(payloads) > 1:
            warnings.append(
                f"{MODEL_WARNING_PREFIX}footprint has {len(payloads)} SVGNODE shapes, "
                "using the first"
            )
        model, notes = _build(data, payloads[0])
    except ParseError as exc:
        return None, [*warnings, f"{MODEL_WARNING_PREFIX}ignored ({exc})"]
    return model, warnings + notes


# ============================================================
# Footprint access
# ============================================================


def _footprint_data(package_detail: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(package_detail, Mapping):
        raise ParseError("footprint (packageDetail) is not a JSON object")
    data: object = package_detail.get("dataStr")
    if isinstance(data, str):
        data = _load_json(data, "footprint dataStr")
    if not isinstance(data, Mapping):
        raise ParseError("footprint dataStr is missing or not an object")
    return data


def _svgnode_payloads(data: Mapping[str, Any]) -> list[str]:
    """JSON text of every SVGNODE shape. Everything after the first `~` belongs to the JSON."""
    shapes = data.get("shape")
    if not isinstance(shapes, (list, tuple)):
        raise ParseError("footprint dataStr.shape is missing or not a list")
    payloads: list[str] = []
    for raw in shapes:
        if isinstance(raw, str) and raw.startswith("SVGNODE"):
            tag, fields = split_shape(raw)
            if tag == "SVGNODE":
                payloads.append("~".join(fields))
    return payloads


def _load_json(text: str, what: str) -> object:
    try:
        value: object = json.loads(text)
    except (ValueError, RecursionError) as exc:  # JSONDecodeError is a ValueError
        raise ParseError(f"{what} is not valid JSON ({exc})") from exc
    return value


# ============================================================
# Small input helpers. All of them raise ParseError.
# ============================================================


def _number(value: object, what: str) -> float:
    """Finite number from a JSON number or a numeric string, magnitude capped."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ParseError(f"{what} is not a number")
    try:
        number = float(value.strip() if isinstance(value, str) else value)
    except (ValueError, OverflowError) as exc:
        raise ParseError(f"{what} is not a number") from exc
    if not math.isfinite(number) or abs(number) > _MAX_ABS_VALUE:
        raise ParseError(f"{what} is out of range")
    return number


def _is_missing(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _optional_number(value: object, what: str, default: float) -> float:
    return default if _is_missing(value) else _number(value, what)


def _number_list(value: object, count: int, what: str) -> list[float]:
    """`count` numbers from a "a,b,c" style string (commas and/or spaces)."""
    if not isinstance(value, str):
        raise ParseError(f"{what} is missing or not a string")
    numbers = [_number(token, what) for token in _NUMBER_SPLIT.split(value.strip()) if token]
    if len(numbers) != count:
        raise ParseError(f"{what} needs {count} numbers, found {len(numbers)}")
    return numbers


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _mm(value_ee: float) -> float:
    """Length in mm without axis or origin, rounded like Transform and without negative zero."""
    return round(ee_to_mm(value_ee), MM_DECIMALS) + 0.0


# ============================================================
# The node
# ============================================================


def _build(data: Mapping[str, Any], payload: str) -> tuple[Model3D, list[str]]:
    node = _load_json(payload, "SVGNODE")
    if not isinstance(node, Mapping):
        raise ParseError("SVGNODE is not a JSON object")
    attrs = node.get("attrs")
    if not isinstance(attrs, Mapping):
        raise ParseError("SVGNODE has no attrs object")

    uuid = _text(attrs.get("uuid"))
    if not _UUID.fullmatch(uuid):
        raise ParseError("SVGNODE attrs.uuid is missing or not a 32 digit lowercase hex string")

    head = data.get("head")
    if not isinstance(head, Mapping):
        raise ParseError("footprint dataStr.head is missing or not an object")
    tf = Transform(
        _optional_number(head.get("x"), "dataStr.head.x", 0.0),
        _optional_number(head.get("y"), "dataStr.head.y", 0.0),
    )

    notes: list[str] = []
    rx, ry, rz = _rotation(attrs.get("c_rotation"))
    if not (_is_zero_angle(rx) and _is_zero_angle(ry)):
        notes.append(
            f"{MODEL_WARNING_PREFIX}rotated about X or Y (rx={rx:g}, ry={ry:g}); only the "
            "rotation about Z is applied"
        )
    width = _size(attrs.get("c_width"), "c_width")
    height = _size(attrs.get("c_height"), "c_height")
    bottom_z = _mm(_optional_number(attrs.get("z"), "z", 0.0))

    origin, origin_problem = _c_origin(attrs.get("c_origin"), tf)
    centre = _outline_centre(node, tf, notes)
    offset, source = origin, PLACEMENT_C_ORIGIN
    if centre is not None:
        if origin is None:
            offset, source = centre, PLACEMENT_OUTLINE_CENTRE
            notes.append(f"{MODEL_WARNING_PREFIX}{origin_problem}, placed by the outline centre")
        else:
            distance = math.hypot(origin.x - centre.x, origin.y - centre.y)
            if distance > STALE_ORIGIN_TOLERANCE_MM:
                offset, source = centre, PLACEMENT_OUTLINE_CENTRE
                notes.append(
                    f"{MODEL_WARNING_PREFIX}c_origin is stale ({distance:.2f} mm from the "
                    "outline centre), placed by the outline centre"
                )
    if offset is None:
        raise ParseError(f"{origin_problem} and the model has no usable outline")

    model = Model3D(
        uuid=uuid,
        name=_text(attrs.get("title")),
        offset_mm=(offset.x + 0.0, offset.y + 0.0),
        bottom_z_mm=bottom_z,
        rotation_deg=(rx + 0.0, ry + 0.0, rz + 0.0),
        native_size_mm=(width, height),
        placement_source=source,
    )
    return model, notes


def _rotation(value: object) -> tuple[float, float, float]:
    if _is_missing(value):
        return 0.0, 0.0, 0.0
    rx, ry, rz = _number_list(value, 3, "c_rotation")
    return rx, ry, rz


def _is_zero_angle(degrees: float) -> bool:
    """True for 0 and for any whole number of turns."""
    turns = degrees % _FULL_TURN_DEG
    return min(turns, _FULL_TURN_DEG - turns) < _ANGLE_EPS_DEG


def _size(value: object, what: str) -> float:
    size = _number(value, what)
    if size < 0.0:
        raise ParseError(f"{what} is negative")
    return _mm(size)


def _c_origin(value: object, tf: Transform) -> tuple[Point | None, str]:
    """The c_origin offset in package mm, or (None, problem) when it is missing or malformed."""
    if _is_missing(value):
        return None, "c_origin is missing"
    try:
        x, y = _number_list(value, 2, "c_origin")
    except ParseError as exc:
        return None, str(exc)
    return tf.point(x, y), ""


def _outline_centre(node: Mapping[str, Any], tf: Transform, notes: list[str]) -> Point | None:
    """Bounding box centre of all outline polylines in package mm, or None without an outline."""
    try:
        bounds = _outline_bounds(node)
    except ParseError as exc:
        notes.append(f"{MODEL_WARNING_PREFIX}outline ignored: {exc}")
        return None
    if bounds is None:
        return None
    return tf.point((bounds[0] + bounds[2]) / 2.0, (bounds[1] + bounds[3]) / 2.0)


def _outline_bounds(node: Mapping[str, Any]) -> _Bounds | None:
    """Bounds (EasyEDA canvas units) of every `childNodes[].attrs.points` polyline."""
    children = node.get("childNodes")
    if children is None:
        return None
    if not isinstance(children, (list, tuple)):
        raise ParseError("childNodes is not a list")
    xs: list[float] = []
    ys: list[float] = []
    for child in children:
        child_attrs = child.get("attrs") if isinstance(child, Mapping) else None
        points = child_attrs.get("points") if isinstance(child_attrs, Mapping) else None
        if points is None:
            continue
        if not isinstance(points, str):
            raise ParseError("polyline points are not a string")
        values = [
            _number(token, "polyline point") for token in _NUMBER_SPLIT.split(points) if token
        ]
        if len(values) % 2:
            raise ParseError("polyline has an odd number of coordinates")
        xs.extend(values[0::2])
        ys.extend(values[1::2])
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)
