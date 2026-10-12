"""Tests for lcsc2fusion.step: validation, bounding box and baked placement transforms.

Offline tests use the hand-written boxes from step_samples. Tests marked `real` use STEP files
downloaded from EasyEDA into tests/fixtures/real (gitignored, local only) and skip without them.
Expected values are computed here with plain math, independent of the module under test.
"""

from __future__ import annotations

import math
import random
import re
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from step_samples import (
    LENGTH_UNIT_BODY,
    NON_ASCII_BYTES,
    PARAM_DIRECTION_2D,
    PARAM_POINT_2D,
    make_assembly_step,
    make_box_step,
    make_cylinder_step,
)

from lcsc2fusion.errors import ModelError
from lcsc2fusion.model import Model3D
from lcsc2fusion.step import (
    BBox3,
    Matrix3x4,
    StepInfo,
    place_step,
    read_step,
    step_bbox,
    transform_step,
)

Vec = tuple[float, float, float]

_COORD_LINE = re.compile(
    rb"^#(\d+) = (CARTESIAN_POINT|DIRECTION) \( '([^']*)',\s+\(([^)]*)\) \) ;$"
)
_VERTEX_LINE = re.compile(rb"^#(\d+) = VERTEX_POINT \( 'NONE', #(\d+) \) ;$")
_STEP_REAL = re.compile(r"[+-]?\d+\.\d*(?:E[+-]?\d+)?")
_NUMBER_BYTES = rb"[-+]?\d+\.\d*(?:E[-+]?\d+)?"


def _model(
    offset: tuple[float, float] = (0.0, 0.0),
    bottom_z: float = 0.0,
    rz: float = 0.0,
    native: tuple[float, float] = (1.6, 0.8),
    rotation: tuple[float, float, float] | None = None,
) -> Model3D:
    return Model3D(
        uuid="0" * 32,
        name="test",
        offset_mm=offset,
        bottom_z_mm=bottom_z,
        rotation_deg=rotation if rotation is not None else (0.0, 0.0, rz),
        native_size_mm=native,
        placement_source="c_origin",
    )


def _lines(data: bytes) -> list[bytes]:
    return data.split(b"\r\n")


def _coords(data: bytes) -> dict[int, tuple[bytes, tuple[float, ...]]]:
    """All CARTESIAN_POINT / DIRECTION entities: id -> (kind, numbers)."""
    result: dict[int, tuple[bytes, tuple[float, ...]]] = {}
    for line in _lines(data):
        m = _COORD_LINE.match(line)
        if m:
            numbers = tuple(float(v) for v in m.group(4).split(b","))
            result[int(m.group(1))] = (m.group(2), numbers)
    return result


def _vertices(data: bytes) -> dict[int, Vec]:
    """VERTEX_POINT id -> coordinates of the 3D point it refers to."""
    points = {i: v for i, (kind, v) in _coords(data).items() if kind == b"CARTESIAN_POINT"}
    result: dict[int, Vec] = {}
    for line in _lines(data):
        m = _VERTEX_LINE.match(line)
        if m:
            x, y, z = points[int(m.group(2))]
            result[int(m.group(1))] = (x, y, z)
    return result


def _assert_vec(actual: Vec, expected: Vec, tol: float = 1e-9) -> None:
    assert actual == pytest.approx(expected, abs=tol)


def _rotate_z(x: float, y: float, degrees: float) -> tuple[float, float]:
    c, s = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
    return (c * x - s * y, s * x + c * y)


# ===== the sample itself =====


def _check_closed_shell(data: bytes) -> int:
    """Every edge is used by exactly two oriented edges in opposite senses and every loop is
    connected. Returns the number of EDGE_CURVE entities."""
    text = data.decode("latin-1")
    edges = {
        m[1]: (m[2], m[3])
        for m in re.finditer(r"#(\d+) = EDGE_CURVE \( 'NONE', #(\d+), #(\d+),", text)
    }
    oriented = {
        m[1]: (m[2], m[3])
        for m in re.finditer(r"#(\d+) = ORIENTED_EDGE \( 'NONE', \*, \*, #(\d+), (\.[TF]\.)", text)
    }
    uses: dict[str, list[str]] = {}
    for edge, sense in oriented.values():
        uses.setdefault(edge, []).append(sense)
    assert set(uses) == set(edges)
    assert all(sorted(v) == [".F.", ".T."] for v in uses.values())  # opposite senses
    loops = list(re.finditer(r"= EDGE_LOOP \( 'NONE', \(([^)]*)\)", text))
    assert loops
    for m in loops:
        ends = []
        for item in m[1].split(","):
            edge, sense = oriented[item.strip().lstrip("#")]
            start, end = edges[edge]
            ends.append((start, end) if sense == ".T." else (end, start))
        for i, (_, tail) in enumerate(ends):
            assert tail == ends[(i + 1) % len(ends)][0]  # the loop is closed and connected
    return len(edges)


def _check_sample_basics(data: bytes) -> None:
    text = data.decode("latin-1")
    assert len(data) >= 1024
    assert "\n" not in text.replace("\r\n", "")  # CRLF only
    ids = re.findall(r"^#(\d+) ?=", text, flags=re.M)
    assert len(ids) == len(set(ids))


def test_sample_is_a_closed_manifold_box() -> None:
    data = make_box_step()
    _check_sample_basics(data)
    assert _check_closed_shell(data) == 12
    text = data.decode("latin-1")
    assert text.count("VERTEX_POINT") == 8
    assert text.count("= ADVANCED_FACE") == 6
    assert NON_ASCII_BYTES in data
    with pytest.raises(UnicodeDecodeError):
        data.decode("utf-8")


@pytest.mark.parametrize(("split_rims", "edge_count"), [(True, 6), (False, 3)])
def test_cylinder_sample_is_a_closed_manifold(split_rims: bool, edge_count: int) -> None:
    data = make_cylinder_step(split_rims=split_rims)
    _check_sample_basics(data)
    assert _check_closed_shell(data) == edge_count
    assert data.count(b"= CIRCLE") == 2
    assert len(_vertices(data)) == (4 if split_rims else 2)


def test_sample_parameters_are_honoured() -> None:
    data = make_box_step(size=(2.0, 3.0, 4.0), center=(5.0, -6.0), zmin=-1.0, non_ascii_name=False)
    assert NON_ASCII_BYTES not in data
    assert set(_vertices(data).values()) == {
        (x, y, z) for x in (4.0, 6.0) for y in (-7.5, -4.5) for z in (-1.0, 3.0)
    }


# ===== validation =====


def test_read_step_describes_the_box() -> None:
    data = make_box_step()
    assert read_step(data) == StepInfo(
        size_bytes=len(data),
        schema="AUTOMOTIVE_DESIGN",
        solids=1,
        vertices=8,
        units_mm=True,
        has_assembly_transforms=False,
    )


def test_read_step_flags_assembly_structure() -> None:
    assert read_step(make_assembly_step()).has_assembly_transforms is True


def test_read_step_accepts_bom_and_surrounding_whitespace() -> None:
    data = make_box_step()
    assert read_step(b"\xef\xbb\xbf \r\n\t" + data + b"\r\n  \n").schema == "AUTOMOTIVE_DESIGN"


def test_read_step_html_page() -> None:
    page = b"<!DOCTYPE html><html><body>Access denied</body></html>" + b" " * 2000
    with pytest.raises(ModelError, match="HTML"):
        read_step(page)
    with pytest.raises(ModelError, match="HTML"):
        read_step(b"\r\n  <html lang='en'>" + b" " * 2000)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d[: len(d) // 2], "END-ISO"),
        (lambda d: d[:-30], "END-ISO"),
        (lambda d: d.replace(b"END-ISO-10303-21;", b"END-ISO-10303-22;"), "END-ISO"),
        (lambda d: d[5:], "header"),
        (lambda d: d.replace(b"ISO-10303-21;", b"ISO-10303-22;", 1), "header"),
        (lambda d: b'{"success":false,"message":"nope"}' + b" " * 2000, "header"),
        (lambda d: b"", "header"),
        (lambda d: d.replace(b"HEADER;", b"HEADEX;"), "HEADER"),
        (lambda d: d.replace(b"DATA;", b"DATX;"), "DATA"),
        (lambda d: d.replace(b"FILE_SCHEMA", b"FILE_SCHEMX"), "FILE_SCHEMA"),
    ],
)
def test_read_step_rejects_malformed(mutate: Callable[[bytes], bytes], message: str) -> None:
    with pytest.raises(ModelError, match=message):
        read_step(mutate(make_box_step()))


def test_read_step_rejects_tiny_file() -> None:
    tiny = b"ISO-10303-21;\r\nHEADER;\r\nENDSEC;\r\nDATA;\r\nENDSEC;\r\nEND-ISO-10303-21;\r\n"
    with pytest.raises(ModelError, match="too small"):
        read_step(tiny)


def test_read_step_rejects_huge_file() -> None:
    huge = b"ISO-10303-21;" + bytes(64 * 1024 * 1024)
    with pytest.raises(ModelError, match="exceeds"):
        read_step(huge)


def test_read_step_rejects_file_without_vertices() -> None:
    data = make_box_step().replace(b"VERTEX_POINT", b"VERTEX_LOOP ")
    with pytest.raises(ModelError, match="VERTEX_POINT"):
        read_step(data)


def test_unit_metres_is_refused() -> None:
    data = make_box_step().replace(b"SI_UNIT ( .MILLI., .METRE. )", b"SI_UNIT ( $, .METRE. )")
    with pytest.raises(ModelError, match=r"Unsupported unit.*metres"):
        read_step(data)


def test_unit_centimetres_is_refused() -> None:
    data = make_box_step().replace(b".MILLI.", b".CENTI.")
    with pytest.raises(ModelError, match=r"Unsupported unit.*centimetres"):
        read_step(data)


def test_unit_inches_is_refused() -> None:
    inch = b"( CONVERSION_BASED_UNIT ( 'INCH', #99 ) LENGTH_UNIT ( ) NAMED_UNIT ( #98 ) )"
    data = make_box_step().replace(LENGTH_UNIT_BODY.encode(), inch)
    assert inch in data
    with pytest.raises(ModelError, match=r"Unsupported unit.*INCH"):
        read_step(data)


def test_missing_length_unit_is_refused() -> None:
    data = make_box_step().replace(b"LENGTH_UNIT ( )", b"AREA_UNIT ( )")
    with pytest.raises(ModelError, match="no LENGTH_UNIT"):
        read_step(data)


def test_mixed_length_units_are_refused() -> None:
    extra = b"#900 =( LENGTH_UNIT ( ) NAMED_UNIT ( * ) SI_UNIT ( $, .METRE. ) );\r\n"
    data = make_box_step().replace(b"ENDSEC;\r\nEND-", extra + b"ENDSEC;\r\nEND-")
    with pytest.raises(ModelError, match="Unsupported unit"):
        read_step(data)


# ===== bounding box =====


def test_bbox3_properties() -> None:
    box = BBox3(-1.0, -2.0, 0.5, 3.0, 4.0, 2.5)
    assert box.center_xy == (1.0, 1.0)
    assert box.size_xy == (4.0, 6.0)
    assert box.size == (4.0, 6.0, 2.0)


def test_bbox_of_default_box() -> None:
    box = step_bbox(make_box_step())
    assert (box.min_x, box.min_y, box.min_z) == pytest.approx((-0.8, -0.4, 0.0), abs=1e-12)
    assert (box.max_x, box.max_y, box.max_z) == pytest.approx((0.8, 0.4, 0.5), abs=1e-12)


@pytest.mark.parametrize(
    ("size", "center", "zmin"),
    [
        ((1.6, 0.8, 0.5), (0.0, 0.0), 0.0),
        ((2.0, 3.0, 4.0), (50.0, -30.0), -0.85),  # the global origin lies far outside the box
        ((0.31, 7.77, 1.25), (-12.5, 8.25), 3.0),
    ],
)
def test_bbox_equals_parameters_and_ignores_non_vertex_points(
    size: Vec, center: tuple[float, float], zmin: float
) -> None:
    box = step_bbox(make_box_step(size=size, center=center, zmin=zmin))
    assert box.center_xy == pytest.approx(center, abs=1e-9)
    assert box.size == pytest.approx(size, abs=1e-9)
    assert box.min_z == pytest.approx(zmin, abs=1e-9)


def test_bbox_requires_vertices() -> None:
    data = make_box_step().replace(b"VERTEX_POINT", b"VERTEX_LOOP ")
    with pytest.raises(ModelError):
        step_bbox(data)


def test_bbox_rejects_vertex_with_missing_point() -> None:
    data = make_box_step()
    first = re.search(rb"#(\d+) = VERTEX_POINT \( 'NONE', #(\d+) \)", data)
    assert first is not None
    broken = data.replace(first.group(0), first.group(0).replace(first.group(2), b"99999"))
    with pytest.raises(ModelError, match="missing 3D point"):
        step_bbox(broken)


# ===== round bodies (circle arcs) =====


def _circle_oracle(
    radius: float, centre: tuple[float, float], zs: tuple[float, float], matrix: Matrix3x4
) -> BBox3:
    """Bounding box of the two rims by dense sampling, as an independent check."""
    pts: list[Vec] = []
    for z in zs:
        for i in range(7200):
            t = 2.0 * math.pi * i / 7200
            pts.append(
                matrix.apply_point(
                    (centre[0] + radius * math.cos(t), centre[1] + radius * math.sin(t), z)
                )
            )
    xs, ys, zs_ = zip(*pts, strict=True)
    return BBox3(min(xs), min(ys), min(zs_), max(xs), max(ys), max(zs_))


def _assert_bbox(actual: BBox3, expected: BBox3, tol: float) -> None:
    got = (actual.min_x, actual.min_y, actual.min_z, actual.max_x, actual.max_y, actual.max_z)
    want = (
        expected.min_x,
        expected.min_y,
        expected.min_z,
        expected.max_x,
        expected.max_y,
        expected.max_z,
    )
    assert got == pytest.approx(want, abs=tol)


@pytest.mark.parametrize("split_rims", [True, False])
def test_bbox_of_round_body_comes_from_the_arcs(split_rims: bool) -> None:
    data = make_cylinder_step(
        radius=2.5, height=5.0, center=(1.0, -2.0), zmin=0.5, split_rims=split_rims
    )
    verts = list(_vertices(data).values())
    assert {v[1] for v in verts} == {-2.0}  # the vertices alone have no extent in Y at all
    box = step_bbox(data)
    assert (box.min_x, box.max_x) == pytest.approx((-1.5, 3.5), abs=1e-9)
    assert (box.min_y, box.max_y) == pytest.approx((-4.5, 0.5), abs=1e-9)
    assert (box.min_z, box.max_z) == pytest.approx((0.5, 5.5), abs=1e-9)


@pytest.mark.parametrize("split_rims", [True, False])
def test_bbox_of_rotated_round_body_matches_sampling(split_rims: bool) -> None:
    data = make_cylinder_step(
        radius=1.7, height=3.0, center=(0.4, 0.9), zmin=-1.0, split_rims=split_rims
    )
    rng = random.Random(3)
    matrices = [Matrix3x4.rotation_x(90), Matrix3x4.rotation_x(-90), Matrix3x4.rotation_z(33)]
    for _ in range(6):
        matrices.append(
            Matrix3x4.translation(*(rng.uniform(-20, 20) for _ in range(3)))
            @ Matrix3x4.rotation_z(rng.uniform(0, 360))
            @ Matrix3x4.rotation_x(rng.uniform(0, 360))
        )
    for m in matrices:
        expected = _circle_oracle(1.7, (0.4, 0.9), (-1.0, 2.0), m)
        _assert_bbox(step_bbox(transform_step(data, m)), expected, 1e-5)


def test_arc_direction_follows_the_edge_sense() -> None:
    data = make_cylinder_step(radius=2.0, height=1.0, center=(0.0, 0.0), zmin=0.0)
    text = data.decode("latin-1")
    verts = _vertices(data)
    start_at_angle_zero = {i for i, v in verts.items() if v[0] > 0}
    circles = set(re.findall(r"#(\d+) = CIRCLE", text))

    def flip(m: re.Match[str]) -> str:
        if m[3] in circles and int(m[1]) in start_at_angle_zero:
            return f"EDGE_CURVE ( 'NONE', #{m[1]}, #{m[2]}, #{m[3]}, .F. )"
        return m[0]

    flipped = re.sub(r"EDGE_CURVE \( 'NONE', #(\d+), #(\d+), #(\d+), \.T\. \)", flip, text).encode(
        "latin-1"
    )
    assert flipped != data
    # Arcs 0 to pi now run pi to 2 pi (the lower half) on both rims, so Y never reaches +2.
    box = step_bbox(flipped)
    assert (box.min_y, box.max_y) == pytest.approx((-2.0, 0.0), abs=1e-9)
    assert (box.min_x, box.max_x) == pytest.approx((-2.0, 2.0), abs=1e-9)


def test_circle_wrapped_in_surface_curve_is_found() -> None:
    data = make_cylinder_step(radius=2.0, height=1.0)
    text = data.decode("latin-1")
    circles = re.findall(r"#(\d+) = CIRCLE", text)
    extra = []
    for n, circle in enumerate(circles):
        wrapper = 9000 + n
        extra.append(f"#{wrapper} = SURFACE_CURVE ( 'NONE', #{circle}, ( #1, #2 ), .PCURVE_S1. ) ;")
        text = re.sub(
            rf"(EDGE_CURVE \( 'NONE', #\d+, #\d+, )#{circle}(, \.[TF]\. \))",
            rf"\1#{wrapper}\2",
            text,
        )
    text = text.replace("ENDSEC;\r\nEND-", "\r\n".join(extra) + "\r\nENDSEC;\r\nEND-")
    assert text.count("SURFACE_CURVE") == 2
    box = step_bbox(text.encode("latin-1"))
    assert box.size == pytest.approx((4.0, 4.0, 1.0), abs=1e-9)


def test_round_body_placement_centres_the_full_circle() -> None:
    data = make_cylinder_step(radius=2.5, height=5.0, center=(7.0, -3.0), zmin=2.0)
    model = _model(offset=(0.5, 1.5), bottom_z=0.0, rz=90.0, native=(5.0, 5.0))
    out, warnings = place_step(data, model)
    assert warnings == []
    box = step_bbox(out)
    assert box.center_xy == pytest.approx((0.5, 1.5), abs=1e-9)
    assert box.size == pytest.approx((5.0, 5.0, 5.0), abs=1e-9)
    assert box.min_z == pytest.approx(0.0, abs=1e-9)


# ===== Matrix3x4 =====


def test_matrix_compose_inverse_and_identity() -> None:
    m = (
        Matrix3x4.translation(1.5, -2.0, 3.0)
        @ Matrix3x4.rotation_z(33.0)
        @ Matrix3x4.rotation_x(-71.0)
    )
    m.check_rigid()
    p = (0.3, -4.0, 2.2)
    back = m.inverse().apply_point(m.apply_point(p))
    assert back == pytest.approx(p, abs=1e-12)
    assert (m @ m.inverse()).apply_point(p) == pytest.approx(p, abs=1e-12)
    assert Matrix3x4.identity().is_identity()
    assert not m.is_identity()


def test_matrix_rotation_conventions() -> None:
    # Rz(90): +X -> +Y (counter-clockwise from +Z). Rx(90): (x, y, z) -> (x, -z, y).
    assert Matrix3x4.rotation_z(90).apply_point((1.0, 0.0, 5.0)) == (0.0, 1.0, 5.0)
    assert Matrix3x4.rotation_x(90).apply_point((2.0, 3.0, 4.0)) == (2.0, -4.0, 3.0)
    assert Matrix3x4.rotation_x(-90).apply_point((2.0, 3.0, 4.0)) == (2.0, 4.0, -3.0)
    assert Matrix3x4.rotation_z(360).is_identity()


def test_matrix_direction_ignores_translation() -> None:
    m = Matrix3x4.translation(10.0, 20.0, 30.0) @ Matrix3x4.rotation_z(90)
    assert m.apply_direction((1.0, 0.0, 0.0)) == (0.0, 1.0, 0.0)
    assert m.apply_point((1.0, 0.0, 0.0)) == (10.0, 21.0, 30.0)


@pytest.mark.parametrize(
    "bad",
    [
        Matrix3x4(((2.0, 0.0, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0))),  # scale
        Matrix3x4(((-1.0, 0.0, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0))),  # mirror
        Matrix3x4(((1.0, 0.0, 0.0, math.nan), (0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0))),
        Matrix3x4(((1.0, 0.1, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0))),  # shear
    ],
)
def test_transform_step_rejects_non_rigid_matrices(bad: Matrix3x4) -> None:
    with pytest.raises(ModelError):
        transform_step(make_box_step(), bad)


def test_rotation_angle_must_be_finite() -> None:
    with pytest.raises(ModelError):
        Matrix3x4.rotation_z(math.inf)


# ===== place_step =====


def test_identity_placement_returns_identical_bytes() -> None:
    data = make_box_step()
    out, warnings = place_step(data, _model())
    assert out == data
    assert warnings == []
    assert transform_step(data, Matrix3x4.identity()) == data


def test_pure_translation() -> None:
    data = make_box_step(center=(0.5, 0.25), zmin=0.1)
    out, warnings = place_step(data, _model(offset=(1.0, -2.0), bottom_z=0.3))
    assert warnings == []
    box = step_bbox(out)
    assert box.center_xy == pytest.approx((1.0, -2.0), abs=1e-9)
    assert box.size == pytest.approx((1.6, 0.8, 0.5), abs=1e-9)
    assert box.min_z == pytest.approx(0.3, abs=1e-9)


@pytest.mark.parametrize("rz", [0.0, 90.0, 180.0, -90.0, 270.0, 45.0, 123.4])
def test_rotation_about_z_every_vertex(rz: float) -> None:
    centre = (0.3, -0.2)
    data = make_box_step(center=centre, zmin=-0.85)
    offset = (1.0, 2.0)
    out, _ = place_step(data, _model(offset=offset, bottom_z=0.2, rz=rz))
    before = _vertices(data)
    after = _vertices(out)
    assert before.keys() == after.keys()
    for vid, (x, y, z) in before.items():
        rx, ry = _rotate_z(x - centre[0], y - centre[1], rz)
        _assert_vec(after[vid], (rx + offset[0], ry + offset[1], z + 0.85 + 0.2))


def test_hand_computed_vertices_for_rotation_90() -> None:
    # Box 1.6 x 0.8 x 0.5 centred at (0.3, -0.2), zmin -0.85; rz 90, offset (1, 2), bottom 0.2.
    # (x, y) - c = (0.8, 0.4) -> Rz(90) = (-0.4, 0.8) -> + offset = (0.6, 2.8).
    # (x, y) - c = (-0.8, -0.4) -> (0.4, -0.8) -> (1.4, 1.2). z: -0.85 -> 0.2 and -0.35 -> 0.7.
    data = make_box_step(center=(0.3, -0.2), zmin=-0.85)
    out, _ = place_step(data, _model(offset=(1.0, 2.0), bottom_z=0.2, rz=90.0))
    placed = list(_vertices(out).values())
    for expected in [(0.6, 2.8, 0.2), (1.4, 1.2, 0.7), (0.6, 2.8, 0.7), (1.4, 1.2, 0.2)]:
        assert any(p == pytest.approx(expected, abs=1e-9) for p in placed), expected


@pytest.mark.parametrize(
    ("rz", "size_xy"),
    [(0.0, (1.6, 0.8)), (90.0, (0.8, 1.6)), (180.0, (1.6, 0.8)), (-90.0, (0.8, 1.6))],
)
def test_bbox_after_placement(rz: float, size_xy: tuple[float, float]) -> None:
    data = make_box_step(center=(0.7, 0.1), zmin=-0.85)
    out, _ = place_step(data, _model(offset=(-2.0, 3.0), bottom_z=0.4, rz=rz))
    box = step_bbox(out)
    assert box.center_xy == pytest.approx((-2.0, 3.0), abs=1e-9)
    assert box.size_xy == pytest.approx(size_xy, abs=1e-9)
    assert box.min_z == pytest.approx(0.4, abs=1e-9)
    assert box.max_z == pytest.approx(0.9, abs=1e-9)


def test_z_lift_rule() -> None:
    data = make_box_step(zmin=-0.85)
    out, _ = place_step(data, _model(bottom_z=-0.85))
    assert step_bbox(out).min_z == pytest.approx(-0.85, abs=1e-9)
    out, _ = place_step(data, _model(bottom_z=0.1))
    box = step_bbox(out)
    assert (box.min_z, box.max_z) == pytest.approx((0.1, 0.6), abs=1e-9)


def test_only_coordinate_numbers_change() -> None:
    data = make_box_step(center=(0.3, -0.2), zmin=-0.85)
    out, _ = place_step(data, _model(offset=(1.0, 2.0), bottom_z=0.2, rz=90.0))
    before, after = _lines(data), _lines(out)
    assert len(before) == len(after)
    changed = 0
    for old, new in zip(before, after, strict=True):
        if old == new:
            continue
        m_old, m_new = _COORD_LINE.match(old), _COORD_LINE.match(new)
        assert m_old is not None
        assert m_new is not None
        assert m_old.group(1, 2, 3) == m_new.group(1, 2, 3)  # id, kind, name
        # Everything except the digits is identical.
        assert re.sub(_NUMBER_BYTES, b"N", old) == re.sub(_NUMBER_BYTES, b"N", new)
        changed += 1
    assert changed > 0
    assert out.count(NON_ASCII_BYTES) == data.count(NON_ASCII_BYTES) == 3
    assert out.count(b"\r\n") == data.count(b"\r\n")
    assert b"\n" not in out.replace(b"\r\n", b"")


def test_two_dimensional_entities_are_untouched() -> None:
    data = make_box_step(center=(0.3, -0.2), zmin=-0.85)
    out, _ = place_step(data, _model(offset=(1.0, 2.0), bottom_z=0.2, rz=90.0))
    for expected in (PARAM_POINT_2D, PARAM_DIRECTION_2D):
        needle = f"( {expected[0]}, {expected[1]} )".encode()
        assert data.count(needle) == 1
        assert out.count(needle) == 1
        line = next(ln for ln in _lines(data) if needle in ln)
        assert line in _lines(out)


def test_directions_rotate_but_do_not_translate() -> None:
    data = make_box_step()
    out, _ = place_step(data, _model(offset=(10.0, 20.0), bottom_z=5.0, rz=90.0))
    before = {i: v for i, (k, v) in _coords(data).items() if k == b"DIRECTION" and len(v) == 3}
    after = {i: v for i, (k, v) in _coords(out).items() if k == b"DIRECTION" and len(v) == 3}
    assert before.keys() == after.keys()
    assert len(before) == 6
    for ident, (x, y, z) in before.items():
        _assert_vec(after[ident], (-y, x, z))  # Rz(90) only, no 10/20/5 anywhere
        assert math.dist((0, 0, 0), after[ident]) == pytest.approx(1.0, abs=1e-12)


def test_origin_point_that_is_not_a_vertex_is_transformed_too() -> None:
    # Every 3D CARTESIAN_POINT moves, not only vertices: the origin point is at (0, 0, 0) before.
    data = make_box_step()
    out, _ = place_step(data, _model(offset=(1.0, 2.0), bottom_z=0.25))
    before = {i: v for i, (k, v) in _coords(data).items() if k == b"CARTESIAN_POINT"}
    after = {i: v for i, (k, v) in _coords(out).items() if k == b"CARTESIAN_POINT"}
    origin = next(i for i, v in before.items() if v == (0.0, 0.0, 0.0))
    _assert_vec(after[origin], (1.0, 2.0, 0.25))  # centre (0, 0) and zmin 0 for the default box


def test_inverse_transform_reproduces_original() -> None:
    data = make_box_step(center=(0.3, -0.2), zmin=-0.85)
    rng = random.Random(11)
    for _ in range(5):
        m = (
            Matrix3x4.translation(*(rng.uniform(-50, 50) for _ in range(3)))
            @ Matrix3x4.rotation_z(rng.uniform(0, 360))
            @ Matrix3x4.rotation_x(rng.uniform(0, 360))
        )
        forward = transform_step(data, m)
        assert forward != data
        back = transform_step(forward, m.inverse())
        original = _coords(data)
        restored = _coords(back)
        assert original.keys() == restored.keys()
        for ident, (kind, values) in original.items():
            assert restored[ident][0] == kind
            assert restored[ident][1] == pytest.approx(values, abs=1e-6)


def test_transformed_numbers_are_valid_step_reals() -> None:
    data = make_box_step(center=(0.3, -0.2), zmin=-0.85)
    rng = random.Random(5)
    matrices = [
        Matrix3x4.translation(1e22, 1e-22, -3e-9) @ Matrix3x4.rotation_z(37.0),
        Matrix3x4.translation(1e-20, -1e-17, 0.0) @ Matrix3x4.rotation_x(1e-7),
        Matrix3x4.translation(123456789.123456789, 0.1, 0.30000000000000004),
    ]
    for _ in range(8):
        matrices.append(
            Matrix3x4.translation(*(rng.uniform(-1e3, 1e3) for _ in range(3)))
            @ Matrix3x4.rotation_z(rng.uniform(-720, 720))
            @ Matrix3x4.rotation_x(rng.uniform(-720, 720))
        )
    for m in matrices:
        out = transform_step(data, m)
        checked = 0
        for line in _lines(out):
            match = _COORD_LINE.match(line)
            if match is None:
                continue
            for token in match.group(4).decode("ascii").split(","):
                assert _STEP_REAL.fullmatch(token.strip()), token
                assert math.isfinite(float(token))
                checked += 1
        assert checked > 100
        assert read_step(out).vertices == 8


def test_precision_is_at_least_nine_significant_digits() -> None:
    data = make_box_step(size=(1.23456789012, 0.8, 0.5), center=(0.0, 0.0), zmin=0.0)
    out = transform_step(data, Matrix3x4.translation(0.1234567890123, 0.0, 0.0))
    xs = sorted(v[0] for v in _vertices(out).values())
    assert xs[0] == pytest.approx(0.1234567890123 - 1.23456789012 / 2, abs=1e-12)
    assert xs[-1] == pytest.approx(0.1234567890123 + 1.23456789012 / 2, abs=1e-12)


def test_unchanged_components_keep_their_original_digits() -> None:
    data = make_box_step()
    out = transform_step(data, Matrix3x4.translation(1.0, 0.0, 0.0))

    def number_text(blob: bytes) -> dict[bytes, list[bytes]]:
        found: dict[bytes, list[bytes]] = {}
        for line in _lines(blob):
            m = _COORD_LINE.match(line)
            if m:
                found[m.group(1)] = m.group(4).split(b",")
        return found

    old, new = number_text(data), number_text(out)
    assert old.keys() == new.keys()
    for ident, parts in old.items():
        if len(parts) == 3:
            assert new[ident][1:] == parts[1:]  # y and z are not modified, digits stay as written


# ===== warnings =====


def test_size_mismatch_warns() -> None:
    data = make_box_step()
    assert place_step(data, _model(native=(1.6, 0.8)))[1] == []
    assert place_step(data, _model(native=(1.64, 0.76)))[1] == []  # within 0.05
    warnings = place_step(data, _model(native=(1.7, 0.8)))[1]
    assert len(warnings) == 1
    assert "1.600" in warnings[0]
    assert "1.700" in warnings[0]
    assert len(place_step(data, _model(native=(1.6, 0.9)))[1]) == 1


def test_native_size_is_compared_unrotated() -> None:
    data = make_box_step()
    assert place_step(data, _model(rz=90.0, native=(1.6, 0.8)))[1] == []
    assert len(place_step(data, _model(rz=90.0, native=(0.8, 1.6)))[1]) == 1


def test_unknown_native_size_is_skipped() -> None:
    assert place_step(make_box_step(), _model(native=(0.0, 0.0)))[1] == []


def test_rx_ry_warn_and_are_ignored() -> None:
    data = make_box_step()
    out_ignored, warnings = place_step(data, _model(rotation=(10.0, -5.0, 90.0), offset=(1.0, 1.0)))
    assert len(warnings) == 1
    assert "rx=10" in warnings[0]
    assert "ry=-5" in warnings[0]
    out_plain, none = place_step(data, _model(rz=90.0, offset=(1.0, 1.0)))
    assert none == []
    assert out_ignored == out_plain


def test_non_finite_placement_is_refused() -> None:
    with pytest.raises(ModelError, match="non-finite"):
        place_step(make_box_step(), _model(offset=(math.nan, 0.0)))
    with pytest.raises(ModelError, match="non-finite"):
        place_step(make_box_step(), _model(bottom_z=math.inf))


# ===== refusals and odd layouts =====


def test_assembly_is_refused_everywhere() -> None:
    data = make_assembly_step()
    with pytest.raises(ModelError, match="assembly"):
        place_step(data, _model())
    with pytest.raises(ModelError, match="assembly"):
        transform_step(data, Matrix3x4.rotation_x(90))
    with pytest.raises(ModelError, match="assembly"):
        transform_step(data, Matrix3x4.identity())  # even a no-op is refused, no silent success


def test_wrapped_and_spaced_coordinate_lists() -> None:
    data = make_box_step()
    point = re.search(rb"#(\d+) = CARTESIAN_POINT \( 'NONE',  \( ([^)]*) \) \) ;", data)
    assert point is not None
    x, y, z = point.group(2).split(b", ")
    wrapped_entity = (
        b"#%s = CARTESIAN_POINT\r\n  (  'NONE' ,\r\n    (\r\n      %s ,\r\n      %s\r\n"
        b"      ,%s\r\n    )\r\n  )\r\n;" % (point.group(1), x, y, z)
    )
    wrapped = data.replace(point.group(0), wrapped_entity)
    assert wrapped != data
    out = transform_step(wrapped, Matrix3x4.translation(1.0, 2.0, 3.0))
    ident = int(point.group(1))
    m = re.search(rb"#%d = CARTESIAN_POINT.*?;" % ident, out, flags=re.S)
    assert m is not None
    numbers = [float(v) for v in re.findall(_NUMBER_BYTES, m.group(0).split(b"'NONE'")[1])]
    assert numbers == pytest.approx([float(x) + 1.0, float(y) + 2.0, float(z) + 3.0], abs=1e-12)
    # Layout (line breaks, spaces) is preserved, only the digits differ.
    assert re.sub(_NUMBER_BYTES, b"N", m.group(0)) == re.sub(_NUMBER_BYTES, b"N", wrapped_entity)
    assert step_bbox(out).size == pytest.approx((1.6, 0.8, 0.5), abs=1e-9)


def test_quote_escapes_in_names_are_handled() -> None:
    data = make_box_step().replace(b"PRODUCT ( 'BOX', 'BOX'", b"PRODUCT ( 'it''s a ''box''', 'BOX'")
    assert b"it''s a ''box'''" in data
    out, _ = place_step(data, _model(offset=(1.0, 0.0)))
    assert b"it''s a ''box'''" in out
    assert step_bbox(out).center_xy == pytest.approx((1.0, 0.0), abs=1e-9)


def test_unsupported_coordinate_layout_is_refused() -> None:
    complex_point = (
        b"#9999 =( GEOMETRIC_REPRESENTATION_ITEM ( ) CARTESIAN_POINT ( 'NONE', "
        b"( 1.0, 2.0, 3.0 ) ) POINT ( ) );\r\n"
    )
    data = make_box_step().replace(b"ENDSEC;\r\nEND-", complex_point + b"ENDSEC;\r\nEND-")
    assert read_step(data).vertices == 8  # still a valid file
    with pytest.raises(ModelError, match="unsupported layout"):
        transform_step(data, Matrix3x4.translation(1.0, 0.0, 0.0))


def test_four_coordinate_point_is_refused() -> None:
    odd = b"#9999 = CARTESIAN_POINT ( 'NONE',  ( 1.0, 2.0, 3.0, 4.0 ) ) ;\r\n"
    data = make_box_step().replace(b"ENDSEC;\r\nEND-", odd + b"ENDSEC;\r\nEND-")
    with pytest.raises(ModelError, match="unsupported layout"):
        transform_step(data, Matrix3x4.rotation_x(90))


def test_unbalanced_quote_cannot_cause_a_silent_partial_transform() -> None:
    data = make_box_step().replace(b"PRODUCT ( 'BOX', 'BOX'", b"PRODUCT ( 'BOX', 'BO'X'")
    with pytest.raises(ModelError, match=r"quoted strings or comments"):
        transform_step(data, Matrix3x4.translation(1.0, 0.0, 0.0))


def test_coordinate_text_inside_a_string_is_never_rewritten() -> None:
    fake = b"x #5 = CARTESIAN_POINT ( ''NONE'', ( 1.0, 2.0, 3.0 ) ) ; y"
    data = make_box_step().replace(b"PRODUCT ( 'BOX', 'BOX'", b"PRODUCT ( 'BOX', '" + fake + b"'")
    # The plain entity count notices the look-alike and the file is refused instead of guessed.
    with pytest.raises(ModelError, match=r"quoted strings or comments"):
        transform_step(data, Matrix3x4.translation(1.0, 0.0, 0.0))


# ===== real EasyEDA files (local only) =====

REAL_DIR = Path(__file__).parent / "fixtures" / "real"

# Bounding boxes measured on the matching OBJ meshes (min_x, max_x, min_y, max_y, min_z, max_z).
REAL_BBOX = {
    "C25804": (-0.41, 0.40, -0.81, 0.81, -0.01, 0.44),  # 0603 resistor
    "C2040": (-3.51, 3.51, -3.51, 3.51, -0.02, 0.85),  # QFN-56
    "C165948": (-4.47, 4.47, -5.30, 2.60, -0.85, 3.251),  # USB-C
}
# Research placements: (offset, bottom_z, rz) from the EasyEDA footprints.
REAL_PLACEMENT = {
    "C25804": ((0.0, 0.0), 0.0, 90.0),
    "C2040": ((0.0, 0.0), 0.0, 0.0),
    "C165948": ((0.0, -1.1), -0.85, 180.0),
}


def _real(part: str) -> bytes:
    path = REAL_DIR / f"{part}.step"
    if not path.is_file():
        pytest.skip(f"{path.name} not in tests/fixtures/real")
    return path.read_bytes()


def _native_size(part: str) -> tuple[float, float]:
    x0, x1, y0, y1, _, _ = REAL_BBOX[part]
    return (x1 - x0, y1 - y0)


@pytest.mark.real
@pytest.mark.parametrize("part", sorted(REAL_BBOX))
def test_real_read_step(part: str) -> None:
    data = _real(part)
    info = read_step(data)
    assert info.units_mm
    assert info.schema == "AUTOMOTIVE_DESIGN"
    assert info.solids >= 1
    assert info.vertices > 100
    assert not info.has_assembly_transforms
    assert info.size_bytes == len(data)


@pytest.mark.real
@pytest.mark.parametrize("part", sorted(REAL_BBOX))
def test_real_bbox_matches_obj(part: str) -> None:
    box = step_bbox(_real(part))
    expected = REAL_BBOX[part]
    actual = (box.min_x, box.max_x, box.min_y, box.max_y, box.min_z, box.max_z)
    assert actual == pytest.approx(expected, abs=0.02)


@pytest.mark.real
@pytest.mark.parametrize("part", sorted(REAL_BBOX))
def test_real_placement(part: str) -> None:
    data = _real(part)
    offset, bottom_z, rz = REAL_PLACEMENT[part]
    native = _native_size(part)
    t0 = time.perf_counter()
    out, warnings = place_step(data, _model(offset=offset, bottom_z=bottom_z, rz=rz, native=native))
    elapsed = time.perf_counter() - t0
    assert elapsed < 3.0
    assert warnings == []

    before = step_bbox(data)
    after = step_bbox(out)
    assert after.center_xy == pytest.approx(offset, abs=1e-6)
    assert after.min_z == pytest.approx(bottom_z, abs=1e-6)
    assert after.max_z - after.min_z == pytest.approx(before.size[2], abs=1e-6)
    swapped = rz % 180 == 90
    expected_size = before.size_xy[::-1] if swapped else before.size_xy
    assert after.size_xy == pytest.approx(expected_size, abs=1e-6)

    # Same entities in the same order; only coordinate digits differ.
    entity = re.compile(rb"(?m)^#(\d+)\s*=")
    assert [m.group(1) for m in entity.finditer(data)] == [m.group(1) for m in entity.finditer(out)]
    a, b = _lines(data), _lines(out)
    assert len(a) == len(b)
    for old, new in zip(a, b, strict=True):
        if old != new:
            m_old, m_new = _COORD_LINE.match(old), _COORD_LINE.match(new)
            assert m_old is not None
            assert m_new is not None
            assert m_old.group(1, 2, 3) == m_new.group(1, 2, 3)
    assert data.count(b"\r\n") == out.count(b"\r\n")
    assert read_step(out).vertices == read_step(data).vertices


@pytest.mark.real
def test_real_c25804_placed_extent() -> None:
    data = _real("C25804")
    out, _ = place_step(data, _model(rz=90.0, native=(0.81, 1.62)))
    box = step_bbox(out)
    assert box.size_xy == pytest.approx((1.62, 0.81), abs=0.02)
    assert (box.min_z, box.max_z) == pytest.approx((0.0, 0.45), abs=0.02)


@pytest.mark.real
@pytest.mark.parametrize("part", sorted(REAL_BBOX))
def test_real_non_ascii_bytes_survive(part: str) -> None:
    data = _real(part)
    out, _ = place_step(data, _model(offset=(1.0, 2.0), rz=90.0, native=_native_size(part)))
    high = re.compile(rb"[\x80-\xff]+")
    assert high.findall(data)  # the research files do contain raw GBK bytes
    assert high.findall(data) == high.findall(out)


@pytest.mark.real
def test_real_round_trip_and_timing() -> None:
    data = _real("C2040")
    m = (
        Matrix3x4.translation(3.0, -4.0, 5.0)
        @ Matrix3x4.rotation_z(33.0)
        @ Matrix3x4.rotation_x(17.0)
    )
    t0 = time.perf_counter()
    forward = transform_step(data, m)
    assert time.perf_counter() - t0 < 3.0
    back = transform_step(forward, m.inverse())
    original, restored = _coords(data), _coords(back)
    assert original.keys() == restored.keys()
    for ident, (_, values) in original.items():
        assert restored[ident][1] == pytest.approx(values, abs=1e-6)
