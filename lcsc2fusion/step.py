"""STEP (ISO 10303-21) helpers for EasyEDA 3D models: validation, bounding box, baked placement.

Stdlib only, core code that never imports `adsk`. Everything works on bytes and decodes them as
latin-1 (a lossless one to one byte mapping), because EasyEDA files carry raw GBK bytes in solid
names and must never be round-tripped through UTF-8. Only the numbers inside 3D `CARTESIAN_POINT`
and `DIRECTION` entities are ever rewritten; every other byte of the file is preserved exactly.

Facts about the EasyEDA files this module is built for (see docs/RESEARCH.md): AP214 written by
SolidWorks, millimetres, a single PRODUCT, no assembly tree, all geometry in global coordinates.
Because of the last point, applying a rigid transform to every 3D point and rotating every 3D
direction is an exact transform of the whole model. Files that do have an assembly structure are
refused rather than transformed wrongly.

Bounding box: the baseline is the set of `VERTEX_POINT` coordinates, as agreed in the plan. This
matched the OBJ bounding box of all researched parts to 0.01 mm. Because SolidWorks puts the
vertices of a round body only at the seams of its circular edges, the extremes of every CIRCLE arc
edge are added too (a cylinder would otherwise have zero width in one axis). Bulges of faces that
are not bounded by circle arcs (spheres, tori, B-spline surfaces) are not covered, and ELLIPSE
edges are not handled. `place_step` warns when the XY size disagrees with the size the EasyEDA
footprint claims, which catches such cases.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from lcsc2fusion.errors import ModelError
from lcsc2fusion.model import Model3D

MIN_STEP_BYTES = 1024
MAX_STEP_BYTES = 64 * 1024 * 1024
SIZE_TOLERANCE_MM = 0.05  # allowed difference between STEP bbox size and Model3D.native_size_mm

_BOM = b"\xef\xbb\xbf"
_HEADER_MAGIC = b"ISO-10303-21;"
_FOOTER_MAGIC = b"END-ISO-10303-21;"
_RIGID_TOLERANCE = 1e-9
_ASSEMBLY_ENTITIES = (
    "NEXT_ASSEMBLY_USAGE_OCCURRENCE",
    "ITEM_DEFINED_TRANSFORMATION",
    "MAPPED_ITEM",
)

_Row = tuple[float, float, float, float]
_Vec = tuple[float, float, float]


@dataclass(frozen=True)
class BBox3:
    """Axis aligned bounding box in millimetres."""

    min_x: float
    min_y: float
    min_z: float
    max_x: float
    max_y: float
    max_z: float

    @property
    def center_xy(self) -> tuple[float, float]:
        return ((self.min_x + self.max_x) / 2.0, (self.min_y + self.max_y) / 2.0)

    @property
    def size_xy(self) -> tuple[float, float]:
        return (self.max_x - self.min_x, self.max_y - self.min_y)

    @property
    def size(self) -> tuple[float, float, float]:
        return (self.max_x - self.min_x, self.max_y - self.min_y, self.max_z - self.min_z)


@dataclass(frozen=True)
class StepInfo:
    """What `read_step` learned about a valid file."""

    size_bytes: int
    schema: str  # first FILE_SCHEMA name, e.g. "AUTOMOTIVE_DESIGN"
    solids: int  # MANIFOLD_SOLID_BREP and BREP_WITH_VOIDS entities
    vertices: int  # VERTEX_POINT entities
    units_mm: bool  # the length unit is millimetres (read_step rejects everything else)
    has_assembly_transforms: bool  # NAUO, ITEM_DEFINED_TRANSFORMATION or MAPPED_ITEM present


@dataclass(frozen=True)
class Matrix3x4:
    """Affine transform `p' = R p + t` stored as three rows `(r0 r1 r2 t)`.

    Only rigid transforms (orthonormal R with determinant +1) are accepted by `transform_step`.
    `a @ b` is the transform that applies `b` first and `a` second, as in matrix algebra.
    """

    rows: tuple[_Row, _Row, _Row]

    @classmethod
    def identity(cls) -> Matrix3x4:
        return cls(((1.0, 0.0, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0)))

    @classmethod
    def translation(cls, dx: float, dy: float, dz: float) -> Matrix3x4:
        return cls(((1.0, 0.0, 0.0, dx), (0.0, 1.0, 0.0, dy), (0.0, 0.0, 1.0, dz)))

    @classmethod
    def rotation_z(cls, degrees: float) -> Matrix3x4:
        """Right handed rotation about +Z, counter-clockwise when viewed from +Z."""
        s, c = _sin_cos_deg(degrees)
        return cls(((c, -s, 0.0, 0.0), (s, c, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0)))

    @classmethod
    def rotation_x(cls, degrees: float) -> Matrix3x4:
        """Right handed rotation about +X: y' = y cos - z sin, z' = y sin + z cos.

        Counter-clockwise when viewed from +X towards the origin. For 90 degrees (x, y, z) maps
        to (x, -z, y), so +Y goes to +Z and +Z goes to -Y.
        """
        s, c = _sin_cos_deg(degrees)
        return cls(((1.0, 0.0, 0.0, 0.0), (0.0, c, -s, 0.0), (0.0, s, c, 0.0)))

    def apply_point(self, p: _Vec) -> _Vec:
        r0, r1, r2 = self.rows
        x, y, z = p
        return (
            r0[0] * x + r0[1] * y + r0[2] * z + r0[3],
            r1[0] * x + r1[1] * y + r1[2] * z + r1[3],
            r2[0] * x + r2[1] * y + r2[2] * z + r2[3],
        )

    def apply_direction(self, d: _Vec) -> _Vec:
        """Rotation part only, the translation does not act on direction vectors."""
        r0, r1, r2 = self.rows
        x, y, z = d
        return (
            r0[0] * x + r0[1] * y + r0[2] * z,
            r1[0] * x + r1[1] * y + r1[2] * z,
            r2[0] * x + r2[1] * y + r2[2] * z,
        )

    def __matmul__(self, other: Matrix3x4) -> Matrix3x4:
        a, b = self.rows, other.rows
        rows: list[_Row] = []
        for i in range(3):
            rot = [sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)]
            trans = sum(a[i][k] * b[k][3] for k in range(3)) + a[i][3]
            rows.append((rot[0], rot[1], rot[2], trans))
        return Matrix3x4((rows[0], rows[1], rows[2]))

    def inverse(self) -> Matrix3x4:
        """Inverse of a rigid transform: R transposed and t' = -R^T t."""
        a = self.rows
        t = (a[0][3], a[1][3], a[2][3])
        rows: list[_Row] = []
        for j in range(3):
            trans = -sum(a[k][j] * t[k] for k in range(3))
            rows.append((a[0][j], a[1][j], a[2][j], trans))
        return Matrix3x4((rows[0], rows[1], rows[2]))

    def is_identity(self) -> bool:
        return self == Matrix3x4.identity()

    def check_rigid(self) -> None:
        """Raise ModelError unless this is a finite rotation (orthonormal, det +1) plus offset."""
        values = [v for row in self.rows for v in row]
        if not all(math.isfinite(v) for v in values):
            raise ModelError("Cannot transform STEP: the transform contains non-finite numbers")
        rot = [row[:3] for row in self.rows]
        for i in range(3):
            for j in range(3):
                dot = sum(rot[i][k] * rot[j][k] for k in range(3))
                if abs(dot - (1.0 if i == j else 0.0)) > _RIGID_TOLERANCE:
                    raise ModelError("Cannot transform STEP: the transform is not a rigid motion")
        det = (
            rot[0][0] * (rot[1][1] * rot[2][2] - rot[1][2] * rot[2][1])
            - rot[0][1] * (rot[1][0] * rot[2][2] - rot[1][2] * rot[2][0])
            + rot[0][2] * (rot[1][0] * rot[2][1] - rot[1][1] * rot[2][0])
        )
        if abs(det - 1.0) > _RIGID_TOLERANCE:
            raise ModelError("Cannot transform STEP: the transform mirrors the model")


def _sin_cos_deg(degrees: float) -> tuple[float, float]:
    """sin and cos of an angle in degrees, exact for multiples of 90 (no 6e-17 noise)."""
    if not math.isfinite(degrees):
        raise ModelError("Cannot transform STEP: the rotation angle is not finite")
    d = degrees % 360.0
    exact = {0.0: (0.0, 1.0), 90.0: (1.0, 0.0), 180.0: (0.0, -1.0), 270.0: (-1.0, 0.0)}
    if d in exact:
        return exact[d]
    rad = math.radians(d)
    return (math.sin(rad), math.cos(rad))


# One scan over the DATA section finds everything we need. Alternatives, in order:
#   skip   quoted strings and /* comments */ so that their content is never mistaken for entities
#   kind   a simple CARTESIAN_POINT or DIRECTION entity with 2 or 3 numbers (x, y[, z]); the
#          number groups give exact spans so only the digits are replaced
#   vref   a VERTEX_POINT and the id of its CARTESIAN_POINT
#   other  the token CARTESIAN_POINT( or DIRECTION( anywhere else (complex entity, odd layout):
#          such a file cannot be transformed safely
_STR = r"'[^']*(?:''[^']*)*'"
_NUM = r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?"
_SCAN_RE = re.compile(
    rf"(?P<skip>{_STR}|/\*.*?\*/)"
    rf"|#(?P<id>\d+)\s*=\s*(?:"
    rf"(?P<kind>CARTESIAN_POINT|DIRECTION)\s*\(\s*(?:{_STR}|\$)\s*,\s*\(\s*"
    rf"(?P<x>{_NUM})\s*,\s*(?P<y>{_NUM})\s*(?:,\s*(?P<z>{_NUM})\s*)?\)\s*\)\s*;"
    rf"|VERTEX_POINT\s*\(\s*(?:{_STR}|\$)\s*,\s*#(?P<vref>\d+)\s*\)\s*;"
    rf")"
    rf"|\b(?P<other>CARTESIAN_POINT|DIRECTION)\s*\(",
    re.DOTALL,
)
# String agnostic count of entity definitions, used to detect a desynchronised scan.
# Circle arcs for the bounding box: placement frames, circles, edges and curve wrappers.
_AXIS_RE = re.compile(
    rf"#(?P<id>\d+)\s*=\s*AXIS2_PLACEMENT_3D\s*\(\s*(?:{_STR}|\$)\s*,\s*#(?P<loc>\d+)\s*,"
    r"\s*#(?P<axis>\d+)\s*,\s*#(?P<ref>\d+)\s*\)\s*;"
)
_CIRCLE_RE = re.compile(
    rf"#(\d+)\s*=\s*CIRCLE\s*\(\s*(?:{_STR}|\$)\s*,\s*#(\d+)\s*,\s*({_NUM})\s*\)\s*;"
)
_EDGE_CURVE_RE = re.compile(
    rf"#\d+\s*=\s*EDGE_CURVE\s*\(\s*(?:{_STR}|\$)\s*,\s*#(\d+)\s*,\s*#(\d+)\s*,"
    r"\s*#(\d+)\s*,\s*\.([TF])\.\s*\)\s*;"
)
_CURVE_WRAPPER_RE = re.compile(
    rf"#(\d+)\s*=\s*(?:SURFACE_CURVE|SEAM_CURVE|INTERSECTION_CURVE)\s*\(\s*(?:{_STR}|\$)\s*,"
    r"\s*#(\d+)\s*,"
)
_PLAIN_COORD_RE = re.compile(r"#\d+\s*=\s*(?:CARTESIAN_POINT|DIRECTION)\s*\(")
_HEADER_SECTION_RE = re.compile(r"\bHEADER\s*;")
_DATA_SECTION_RE = re.compile(r"\bDATA\s*;")
_SCHEMA_RE = re.compile(rf"FILE_SCHEMA\s*\(\s*\(\s*({_STR})")
_SOLID_RE = re.compile(r"=\s*(?:MANIFOLD_SOLID_BREP|BREP_WITH_VOIDS)\s*\(")
_ASSEMBLY_RE = re.compile(r"\b(?:" + "|".join(_ASSEMBLY_ENTITIES) + r")\b")
# A complex entity statement that declares a LENGTH_UNIT, e.g.
# #24 =( LENGTH_UNIT ( ) NAMED_UNIT ( * ) SI_UNIT ( .MILLI., .METRE. ) );
_LENGTH_UNIT_RE = re.compile(r"#\d+\s*=\s*\((?=[^;]*\bLENGTH_UNIT\s*\()([^;]*)\)\s*;")
_SI_UNIT_RE = re.compile(r"\bSI_UNIT\s*\(\s*(\$|\.[A-Z_]+\.)\s*,\s*\.METRE\.\s*\)")
_CONVERSION_UNIT_RE = re.compile(r"\bCONVERSION_BASED_UNIT\s*\(\s*'([^']*)'")


@dataclass(frozen=True)
class _Parsed:
    """A validated file together with the result of the coordinate scan."""

    data: bytes
    text: str  # latin-1 decoding of `data`, same length in characters as `data` in bytes
    info: StepInfo
    coords: tuple[re.Match[str], ...]  # CARTESIAN_POINT / DIRECTION entities, file order
    vertex_refs: tuple[tuple[int, int], ...]  # (VERTEX_POINT id, CARTESIAN_POINT id)
    data_start: int  # offset in `text` where the DATA section content begins
    unsafe_reason: str | None  # why a transform would be wrong, None when it is safe


def _check_container(data: bytes) -> None:
    size = len(data)
    if size > MAX_STEP_BYTES:
        raise ModelError(f"Invalid STEP file: {size} bytes exceeds the {MAX_STEP_BYTES} byte limit")
    head = data[:512].lstrip(_BOM).lstrip()
    if not head.startswith(_HEADER_MAGIC):
        lowered = head.lower()
        if lowered.startswith((b"<!doctype", b"<html", b"<?xml")) or b"<html" in lowered:
            raise ModelError("Invalid STEP file: got an HTML page instead of a STEP model")
        raise ModelError("Invalid STEP file: missing the ISO-10303-21; header")
    if size < MIN_STEP_BYTES:
        raise ModelError(f"Invalid STEP file: only {size} bytes, too small to be a model")
    if not data[-256:].rstrip().endswith(_FOOTER_MAGIC):
        raise ModelError("Invalid STEP file: missing END-ISO-10303-21; (truncated download?)")


def _length_unit(text: str) -> None:
    """Raise ModelError unless every declared LENGTH_UNIT is millimetres."""
    units: set[str] = set()
    for m in _LENGTH_UNIT_RE.finditer(text):
        body = m.group(1)
        si = _SI_UNIT_RE.search(body)
        conv = _CONVERSION_UNIT_RE.search(body)
        if si is not None:
            prefix = si.group(1)
            if prefix == "$":
                units.add("metres")
            else:
                units.add(prefix.strip(".").lower() + "metres")
        elif conv is not None:
            units.add(f"'{conv.group(1)}'")
        else:
            units.add("an unrecognised unit")
    if not units:
        raise ModelError("Unsupported unit: the STEP file declares no LENGTH_UNIT, expected mm")
    if units != {"millimetres"}:
        raise ModelError(
            f"Unsupported unit: expected millimetres, the STEP file uses {', '.join(sorted(units))}"
        )


def _parse(data: bytes) -> _Parsed:
    _check_container(data)
    text = data.decode("latin-1")
    header = _HEADER_SECTION_RE.search(text)
    body = _DATA_SECTION_RE.search(text)
    if header is None or body is None or header.end() > body.start():
        raise ModelError("Invalid STEP file: missing HEADER; or DATA; section")
    schema = _SCHEMA_RE.search(text, header.end(), body.start())
    if schema is None:
        raise ModelError("Invalid STEP file: missing FILE_SCHEMA in the header")
    _length_unit(text)

    coords: list[re.Match[str]] = []
    vertex_refs: list[tuple[int, int]] = []
    other = 0
    for m in _SCAN_RE.finditer(text, body.end()):
        if m["kind"] is not None:
            coords.append(m)
        elif m["vref"] is not None:
            vertex_refs.append((int(m["id"]), int(m["vref"])))
        elif m["other"] is not None:
            other += 1
    if not vertex_refs:
        raise ModelError("Invalid STEP file: no VERTEX_POINT entities, the model has no geometry")

    unsafe: str | None = None
    plain = sum(1 for _ in _PLAIN_COORD_RE.finditer(text, body.end()))
    if other:
        unsafe = f"{other} CARTESIAN_POINT or DIRECTION entities have an unsupported layout"
    elif plain != len(coords):
        unsafe = (
            "quoted strings or comments hide or imitate coordinate entities (unbalanced quote?)"
        )
    info = StepInfo(
        size_bytes=len(data),
        schema=schema.group(1)[1:-1].replace("''", "'"),
        solids=len(_SOLID_RE.findall(text, body.end())),
        vertices=len(vertex_refs),
        units_mm=True,
        has_assembly_transforms=_ASSEMBLY_RE.search(text, body.end()) is not None,
    )
    return _Parsed(data, text, info, tuple(coords), tuple(vertex_refs), body.end(), unsafe)


def read_step(data: bytes) -> StepInfo:
    """Validate a STEP file and describe it. Raises ModelError for anything unusable.

    Accepts leading whitespace or a UTF-8 BOM and trailing whitespace. Rejects HTML bodies,
    truncated or oversized files, a missing HEADER, DATA or FILE_SCHEMA, files without vertices
    and every length unit except millimetres (metres, inches and other prefixes are refused
    explicitly instead of being guessed).
    """
    return _parse(data).info


def _unit(v: _Vec) -> _Vec | None:
    norm = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    if not math.isfinite(norm) or norm < 1e-12:
        return None
    return (v[0] / norm, v[1] / norm, v[2] / norm)


def _dot(a: _Vec, b: _Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _circle_frames(
    parsed: _Parsed, points: dict[int, _Vec]
) -> dict[int, tuple[_Vec, _Vec, _Vec, float]]:
    """CIRCLE id -> (centre, e1, e2, radius): an orthonormal frame in the plane of the circle."""
    text, start = parsed.text, parsed.data_start
    directions = {
        int(m["id"]): (float(m["x"]), float(m["y"]), float(m["z"]))
        for m in parsed.coords
        if m["kind"] == "DIRECTION" and m["z"] is not None
    }
    placements: dict[int, tuple[_Vec, _Vec, _Vec]] = {}
    for m in _AXIS_RE.finditer(text, start):
        centre = points.get(int(m["loc"]))
        normal = directions.get(int(m["axis"]))
        ref = directions.get(int(m["ref"]))
        if centre is None or normal is None or ref is None:
            continue
        n = _unit(normal)
        if n is None:
            continue
        along = _dot(ref, n)  # make the reference direction orthogonal to the axis
        e1 = _unit((ref[0] - along * n[0], ref[1] - along * n[1], ref[2] - along * n[2]))
        if e1 is None:
            continue
        e2 = (n[1] * e1[2] - n[2] * e1[1], n[2] * e1[0] - n[0] * e1[2], n[0] * e1[1] - n[1] * e1[0])
        placements[int(m["id"])] = (centre, e1, e2)
    frames: dict[int, tuple[_Vec, _Vec, _Vec, float]] = {}
    for m in _CIRCLE_RE.finditer(text, start):
        placement = placements.get(int(m[2]))
        radius = float(m[3])
        if placement is not None and math.isfinite(radius) and radius > 0.0:
            frames[int(m[1])] = (*placement, radius)
    return frames


def _arc_extreme_points(
    parsed: _Parsed, points: dict[int, _Vec], vertices: dict[int, _Vec]
) -> list[_Vec]:
    """Points where circular edges reach their extreme X, Y or Z, when the arc covers them.

    An EDGE_CURVE with same_sense .T. runs counter-clockwise (about the circle axis) from its
    first to its second vertex, .F. runs the other way. Coincident vertices mean a full circle.
    """
    frames = _circle_frames(parsed, points)
    if not frames:
        return []
    text, start = parsed.text, parsed.data_start
    wrapped = {int(m[1]): int(m[2]) for m in _CURVE_WRAPPER_RE.finditer(text, start)}
    two_pi = 2.0 * math.pi
    result: list[_Vec] = []
    for m in _EDGE_CURVE_RE.finditer(text, start):
        curve = int(m[3])
        frame = frames.get(wrapped.get(curve, curve))
        p1, p2 = vertices.get(int(m[1])), vertices.get(int(m[2]))
        if frame is None or p1 is None or p2 is None:
            continue
        centre, e1, e2, radius = frame
        angles = [
            math.atan2(
                _dot((p[0] - centre[0], p[1] - centre[1], p[2] - centre[2]), e2),
                _dot((p[0] - centre[0], p[1] - centre[1], p[2] - centre[2]), e1),
            )
            for p in (p1, p2)
        ]
        if m[4] == "F":
            angles.reverse()
        full = math.dist(p1, p2) < 1e-9
        span = two_pi if full else (angles[1] - angles[0]) % two_pi
        for axis in range(3):
            peak = math.atan2(e2[axis], e1[axis])  # angle where coordinate `axis` is largest
            for theta in (peak, peak + math.pi):
                if full or (theta - angles[0]) % two_pi <= span + 1e-9:
                    c, s = math.cos(theta), math.sin(theta)
                    result.append(
                        (
                            centre[0] + radius * (c * e1[0] + s * e2[0]),
                            centre[1] + radius * (c * e1[1] + s * e2[1]),
                            centre[2] + radius * (c * e1[2] + s * e2[2]),
                        )
                    )
    return result


def _bbox(parsed: _Parsed) -> BBox3:
    points: dict[int, _Vec] = {}
    for m in parsed.coords:
        if m["kind"] == "CARTESIAN_POINT" and m["z"] is not None:
            points[int(m["id"])] = (float(m["x"]), float(m["y"]), float(m["z"]))
    vertices: dict[int, _Vec] = {}
    for vertex_id, point_id in parsed.vertex_refs:
        point = points.get(point_id)
        if point is None:
            raise ModelError(
                f"Invalid STEP file: VERTEX_POINT #{vertex_id} refers to missing 3D point "
                f"#{point_id}"
            )
        vertices[vertex_id] = point
    extent = [*vertices.values(), *_arc_extreme_points(parsed, points, vertices)]
    xs, ys, zs = zip(*extent, strict=True)
    box = BBox3(min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))
    if not all(math.isfinite(v) for v in (*box.center_xy, *box.size)):
        raise ModelError("Invalid STEP file: vertex coordinates are not finite")
    return box


def step_bbox(data: bytes) -> BBox3:
    """Bounding box in mm of the 3D vertices plus the extremes of circle arc edges.

    Validates like `read_step`. Not computed from every CARTESIAN_POINT: those include surface
    and curve frames and B-spline control points that lie outside the body.
    """
    return _bbox(_parse(data))


def _fmt_real(value: float) -> str:
    """STEP REAL syntax: has a '.', optional signed 'E' exponent, shortest round-trip digits."""
    if not math.isfinite(value):
        raise ModelError("Cannot transform STEP: a transformed coordinate is not finite")
    text = repr(value + 0.0)  # + 0.0 turns -0.0 into 0.0
    mantissa, has_exp, exponent = text.partition("e")
    if "." not in mantissa:
        mantissa += ".0"
    return f"{mantissa}E{int(exponent):+d}" if has_exp else mantissa


def _apply(parsed: _Parsed, matrix: Matrix3x4) -> bytes:
    if parsed.info.has_assembly_transforms:
        raise ModelError(
            "STEP file has an assembly structure (NEXT_ASSEMBLY_USAGE_OCCURRENCE, "
            "ITEM_DEFINED_TRANSFORMATION or MAPPED_ITEM), cannot bake a transform into an assembly"
        )
    if parsed.unsafe_reason is not None:
        raise ModelError(f"Cannot transform STEP safely: {parsed.unsafe_reason}")
    matrix.check_rigid()
    if matrix.is_identity():
        return parsed.data

    text = parsed.text
    out: list[str] = []
    pos = 0
    for m in parsed.coords:
        if m["z"] is None:
            continue  # 2D parameter space point or direction: not model space, never touched
        old: _Vec = (float(m["x"]), float(m["y"]), float(m["z"]))
        if m["kind"] == "CARTESIAN_POINT":
            new = matrix.apply_point(old)
        else:
            new = matrix.apply_direction(old)
        for group, before, after in zip(("x", "y", "z"), old, new, strict=True):
            if after == before:
                continue  # keep the original digits when a component does not change
            start, end = m.span(group)
            out.append(text[pos:start])
            out.append(_fmt_real(after))
            pos = end
    out.append(text[pos:])
    return "".join(out).encode("latin-1")


def transform_step(data: bytes, matrix: Matrix3x4) -> bytes:
    """Bake a rigid transform into a STEP file.

    Every 3D CARTESIAN_POINT gets the full transform, every 3D DIRECTION only the rotation.
    2D entities are left alone. Raises ModelError for an invalid file, a non rigid matrix, an
    assembly structure, or a layout the scanner cannot cover completely. An identity transform
    returns `data` unchanged, and components that do not change keep their original digits.
    """
    return _apply(_parse(data), matrix)


def place_step(data: bytes, model: Model3D) -> tuple[bytes, list[str]]:
    """Bake the EasyEDA placement into the STEP file, returning (new bytes, warnings).

    With c the XY centre of the vertex bounding box and zmin its lowest Z:
    `q_xy = Rz(rz) * (p_xy - c) + offset_mm` and `q_z = p_z - zmin + bottom_z_mm`, where
    rz = model.rotation_deg[2] (counter-clockwise viewed from +Z). Rotations about X and Y are
    ignored with a warning. Also warns when the bbox XY size differs from the unrotated
    `native_size_mm` by more than 0.05 mm (a native dimension <= 0 means unknown and is skipped).
    """
    values = (*model.offset_mm, model.bottom_z_mm, *model.rotation_deg, *model.native_size_mm)
    if not all(math.isfinite(v) for v in values):
        raise ModelError("Cannot place STEP: the 3D placement contains non-finite numbers")
    parsed = _parse(data)
    box = _bbox(parsed)

    warnings: list[str] = []
    rx, ry, rz = model.rotation_deg
    if abs(rx) > 1e-9 or abs(ry) > 1e-9:
        warnings.append(
            f"3D model rotation about X/Y (rx={rx:g}, ry={ry:g} degrees) is not supported "
            "and was ignored"
        )
    size_x, size_y = box.size_xy
    native_x, native_y = model.native_size_mm
    if (
        native_x > 0.0
        and native_y > 0.0
        and (
            abs(size_x - native_x) > SIZE_TOLERANCE_MM or abs(size_y - native_y) > SIZE_TOLERANCE_MM
        )
    ):
        warnings.append(
            f"3D model size {size_x:.3f} x {size_y:.3f} mm differs from the expected "
            f"{native_x:.3f} x {native_y:.3f} mm, the placement may be off"
        )

    cx, cy = box.center_xy
    matrix = (
        Matrix3x4.translation(model.offset_mm[0], model.offset_mm[1], model.bottom_z_mm)
        @ Matrix3x4.rotation_z(rz)
        @ Matrix3x4.translation(-cx, -cy, -box.min_z)
    )
    return _apply(parsed, matrix), warnings
