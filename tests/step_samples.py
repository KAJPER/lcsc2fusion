"""Hand-written minimal STEP files for tests (no EasyEDA data, safe to commit).

The files imitate the vocabulary and layout of the SolidWorks 2020 AP214 files that EasyEDA
serves: CRLF line endings, entity order shuffled with forward references, `#n = NAME ( ... ) ;`
spacing, 19 digit coordinates, complex entities for the units, raw non-ASCII (GBK like) bytes
inside quoted names, colour styling chain, and a pair of 2D parameter space entities.

Import as `from step_samples import make_box_step` (the tests directory is on sys.path).
"""

from __future__ import annotations

import random

# Raw bytes of the kind GBK encoded Chinese names produce. They must survive any transform.
NON_ASCII_BYTES = b"\xb5\xc4\xbe\xb5\xcf\xf2"
# The millimetre LENGTH_UNIT entity body, exactly as written in the sample (tests replace it).
LENGTH_UNIT_BODY = "( LENGTH_UNIT ( ) NAMED_UNIT ( * ) SI_UNIT ( .MILLI., .METRE. ) )"
# The 2D entities that must never be transformed (parameter space of a PCURVE).
PARAM_POINT_2D = (0.25, 0.5)
PARAM_DIRECTION_2D = (0.6, 0.8)

_HEADER = (
    "ISO-10303-21;\r\n"
    "HEADER;\r\n"
    "FILE_DESCRIPTION (( 'STEP AP214' ),\r\n"
    "    '1' );\r\n"
    "FILE_NAME ('BOX.STEP',\r\n"
    "    '2023-02-10T06:10:09',\r\n"
    "    ( 'PC' ),\r\n"
    "    ( 'Microsoft' ),\r\n"
    "    'SwSTEP 2.0',\r\n"
    "    'SolidWorks 2020',\r\n"
    "    '' );\r\n"
    "FILE_SCHEMA (( 'AUTOMOTIVE_DESIGN' ));\r\n"
    "ENDSEC;\r\n"
    "\r\n"
    "DATA;\r\n"
)
_FOOTER = "ENDSEC;\r\nEND-ISO-10303-21;\r\n"

Corner = tuple[int, int, int]
Xyz = tuple[float, float, float]


def _num(value: float) -> str:
    """SolidWorks style number with 19 decimals."""
    return f"{value + 0.0:.19f}"


def _point(xyz: Xyz) -> str:
    return f"CARTESIAN_POINT ( 'NONE',  ( {_num(xyz[0])}, {_num(xyz[1])}, {_num(xyz[2])} ) )"


class _Entities:
    """Allocates ids and collects entity lines in definition order."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def add(self, body: str) -> int:
        number = len(self.lines) + 1
        self.lines.append(f"#{number} = {body} ;")
        return number

    def add_complex(self, body: str) -> int:
        number = len(self.lines) + 1
        self.lines.append(f"#{number} =( {body} );")
        return number


def _preamble(e: _Entities) -> tuple[int, dict[tuple[int, int], int]]:
    """Units, representation context and the six axis directions. Returns (context, directions).

    Directions are keyed by (axis, sign). +x carries SolidWorks style noise (E-16 terms and a
    negative zero) so that number formats are exercised.
    """
    length = e.add_complex("LENGTH_UNIT ( ) NAMED_UNIT ( * ) SI_UNIT ( .MILLI., .METRE. )")
    angle = e.add_complex("NAMED_UNIT ( * ) PLANE_ANGLE_UNIT ( ) SI_UNIT ( $, .RADIAN. )")
    solid_angle = e.add_complex("NAMED_UNIT ( * ) SI_UNIT ( $, .STERADIAN. ) SOLID_ANGLE_UNIT ( )")
    uncertainty = e.add(
        f"UNCERTAINTY_MEASURE_WITH_UNIT (LENGTH_MEASURE( 1.000000000000000082E-05 ), #{length}, "
        "'distance_accuracy_value', 'NONE')"
    )
    context = e.add_complex(
        f"GEOMETRIC_REPRESENTATION_CONTEXT ( 3 ) GLOBAL_UNCERTAINTY_ASSIGNED_CONTEXT "
        f"( ( #{uncertainty} ) ) GLOBAL_UNIT_ASSIGNED_CONTEXT ( ( #{length}, #{angle}, "
        f"#{solid_angle} ) ) REPRESENTATION_CONTEXT ( 'NONE', 'WORKASPACE' )"
    )
    unit_values = {
        (0, 1): "1.000000000000000000, 1.694065894508600925E-16, -0.0000000000000000000",
        (0, -1): "-1.000000000000000000, 0.0000000000000000000, 0.0000000000000000000",
        (1, 1): "0.0000000000000000000, 1.000000000000000000, 0.0000000000000000000",
        (1, -1): "0.0000000000000000000, -1.000000000000000000, 0.0000000000000000000",
        (2, 1): "0.0000000000000000000, 0.0000000000000000000, 1.000000000000000000",
        (2, -1): "0.0000000000000000000, 0.0000000000000000000, -1.000000000000000000",
    }
    directions = {
        key: e.add(f"DIRECTION ( 'NONE',  ( {values} ) )") for key, values in unit_values.items()
    }
    return context, directions


def _product(e: _Entities, name: str, brep: int) -> tuple[int, int, int]:
    """Product structure. Returns (product definition, its formation, its context)."""
    app_context = e.add("APPLICATION_CONTEXT ( 'automotive_design' )")
    e.add(
        "APPLICATION_PROTOCOL_DEFINITION ( 'draft international standard', 'automotive_design', "
        f"1998, #{app_context} )"
    )
    product_context = e.add(f"PRODUCT_CONTEXT ( 'NONE', #{app_context}, 'mechanical' )")
    product = e.add(f"PRODUCT ( 'BOX', 'BOX', '', ( #{product_context} ) )")
    e.add(f"PRODUCT_RELATED_PRODUCT_CATEGORY ( 'part', '', ( #{product} ) )")
    formation = e.add(
        f"PRODUCT_DEFINITION_FORMATION_WITH_SPECIFIED_SOURCE ( '{name}', '', #{product}, "
        ".NOT_KNOWN. )"
    )
    definition_context = e.add(
        f"PRODUCT_DEFINITION_CONTEXT ( 'detailed design', #{app_context}, 'design' )"
    )
    definition = e.add(f"PRODUCT_DEFINITION ( '{name}', '', #{formation}, #{definition_context} )")
    definition_shape = e.add(f"PRODUCT_DEFINITION_SHAPE ( 'NONE', 'NONE',  #{definition} )")
    e.add(f"SHAPE_DEFINITION_REPRESENTATION ( #{definition_shape}, #{brep} )")
    return definition, formation, definition_context


def _document(e: _Entities) -> bytes:
    # SolidWorks writes entities in arbitrary order, so references point forwards as well.
    lines = list(e.lines)
    random.Random(20230210).shuffle(lines)
    return (_HEADER + "\r\n".join(lines) + "\r\n" + _FOOTER).encode("latin-1")


def _ids(numbers: list[int]) -> str:
    return ", ".join(f"#{n}" for n in numbers)


def _build(
    size: tuple[float, float, float],
    center: tuple[float, float],
    zmin: float,
    non_ascii_name: bool,
    assembly: bool,
) -> bytes:
    sx, sy, sz = size
    lo = (center[0] - sx / 2.0, center[1] - sy / 2.0, zmin)
    hi = (center[0] + sx / 2.0, center[1] + sy / 2.0, zmin + sz)

    def corner_xyz(c: Corner) -> Xyz:
        return (hi[0] if c[0] else lo[0], hi[1] if c[1] else lo[1], hi[2] if c[2] else lo[2])

    e = _Entities()
    name = "\xb5\xc4\xbe\xb5\xcf\xf2" if non_ascii_name else "body"
    context, directions = _preamble(e)

    # Eight vertices, each with its own CARTESIAN_POINT.
    vertices: dict[Corner, int] = {}
    for idx in range(8):
        corner = (idx & 1, (idx >> 1) & 1, (idx >> 2) & 1)
        pt = e.add(_point(corner_xyz(corner)))
        vertices[corner] = e.add(f"VERTEX_POINT ( 'NONE', #{pt} )")

    # Twelve edges: LINE through a start point with a VECTOR along +axis, then EDGE_CURVE.
    edges: dict[tuple[int, tuple[int, int]], int] = {}
    for axis in range(3):
        others = [a for a in range(3) if a != axis]
        for b in (0, 1):
            for c in (0, 1):
                start_bits = [0, 0, 0]
                start_bits[others[0]], start_bits[others[1]] = b, c
                end_bits = list(start_bits)
                end_bits[axis] = 1
                start: Corner = (start_bits[0], start_bits[1], start_bits[2])
                end: Corner = (end_bits[0], end_bits[1], end_bits[2])
                origin = e.add(_point(corner_xyz(start)))
                vector = e.add(f"VECTOR ( 'NONE', #{directions[(axis, 1)]}, 1000.000000000000000 )")
                line = e.add(f"LINE ( 'NONE', #{origin}, #{vector} )")
                edges[(axis, (b, c))] = e.add(
                    f"EDGE_CURVE ( 'NONE', #{vertices[start]}, #{vertices[end]}, #{line}, .T. )"
                )

    # Six planar faces. Loops run counter-clockwise seen from outside the solid.
    faces: list[int] = []
    planes: list[int] = []
    for axis in range(3):
        u, v = (axis + 1) % 3, (axis + 2) % 3
        for side in (0, 1):
            ring: list[Corner] = []
            for bu, bv in ((0, 0), (1, 0), (1, 1), (0, 1)):
                bits = [0, 0, 0]
                bits[axis], bits[u], bits[v] = side, bu, bv
                ring.append((bits[0], bits[1], bits[2]))
            if side == 0:
                ring.reverse()
            oriented: list[int] = []
            for i, p in enumerate(ring):
                q = ring[(i + 1) % 4]
                e_axis = next(a for a in range(3) if p[a] != q[a])
                rest = tuple(p[a] for a in range(3) if a != e_axis)
                sense = ".T." if q[e_axis] > p[e_axis] else ".F."
                oriented.append(
                    e.add(
                        f"ORIENTED_EDGE ( 'NONE', *, *, #{edges[(e_axis, (rest[0], rest[1]))]}, "
                        f"{sense} )"
                    )
                )
            loop = e.add(f"EDGE_LOOP ( 'NONE', ( {_ids(oriented)} ) )")
            bound = e.add(f"FACE_OUTER_BOUND ( 'NONE', #{loop}, .T. )")
            loc = e.add(_point(corner_xyz(ring[0])))
            placement = e.add(
                f"AXIS2_PLACEMENT_3D ( 'NONE', #{loc}, #{directions[(axis, 1 if side else -1)]}, "
                f"#{directions[(u, 1)]} )"
            )
            plane = e.add(f"PLANE ( 'NONE',  #{placement} )")
            planes.append(plane)
            faces.append(e.add(f"ADVANCED_FACE ( 'NONE', ( #{bound} ), #{plane}, .T. )"))

    shell = e.add(f"CLOSED_SHELL ( 'NONE', ( {_ids(faces)} ) )")
    solid = e.add(f"MANIFOLD_SOLID_BREP ( '{name}', #{shell} )")

    # A 3D axis at the global origin: a CARTESIAN_POINT that is not a vertex (must not be in bbox).
    origin_pt = e.add(_point((0.0, 0.0, 0.0)))
    origin_axis = e.add(
        f"AXIS2_PLACEMENT_3D ( 'NONE', #{origin_pt}, #{directions[(2, 1)]}, #{directions[(0, 1)]} )"
    )
    brep = e.add(
        f"ADVANCED_BREP_SHAPE_REPRESENTATION ( 'BOX', ( #{solid}, #{origin_axis} ), #{context} )"
    )

    # Colour styling chain on the first face, like the per face colours of the real files.
    rgb = e.add("COLOUR_RGB ( '',0.7, 0.2, 0.1 )")
    fill_colour = e.add(f"FILL_AREA_STYLE_COLOUR ( '', #{rgb} )")
    fill = e.add(f"FILL_AREA_STYLE ('', ( #{fill_colour} ) )")
    surface_fill = e.add(f"SURFACE_STYLE_FILL_AREA ( #{fill} )")
    side_style = e.add(f"SURFACE_SIDE_STYLE ('', ( #{surface_fill} ) )")
    usage = e.add(f"SURFACE_STYLE_USAGE ( .BOTH. , #{side_style} )")
    presentation = e.add(f"PRESENTATION_STYLE_ASSIGNMENT (( #{usage} ) )")
    e.add(f"STYLED_ITEM ( 'NONE', ( #{presentation} ), #{faces[0]} )")

    # 2D parameter space: a PCURVE whose definitional representation uses 2D points/directions.
    pt2 = e.add(f"CARTESIAN_POINT ( 'NONE',  ( {PARAM_POINT_2D[0]}, {PARAM_POINT_2D[1]} ) )")
    dir2 = e.add(f"DIRECTION ( 'NONE',  ( {PARAM_DIRECTION_2D[0]}, {PARAM_DIRECTION_2D[1]} ) )")
    vec2 = e.add(f"VECTOR ( 'NONE', #{dir2}, 1.000000000000000000 )")
    line2 = e.add(f"LINE ( 'NONE', #{pt2}, #{vec2} )")
    param_context = e.add_complex(
        "GEOMETRIC_REPRESENTATION_CONTEXT ( 2 ) PARAMETRIC_REPRESENTATION_CONTEXT ( ) "
        "REPRESENTATION_CONTEXT ( '2D SPACE', '' )"
    )
    def_rep = e.add(f"DEFINITIONAL_REPRESENTATION ( 'NONE', ( #{line2} ), #{param_context} )")
    e.add(f"PCURVE ( 'NONE', #{planes[0]}, #{def_rep} )")

    definition, formation, definition_context = _product(e, name, brep)
    if assembly:
        child = e.add(f"PRODUCT_DEFINITION ( 'child', '', #{formation}, #{definition_context} )")
        e.add(
            f"NEXT_ASSEMBLY_USAGE_OCCURRENCE ( 'NAUO1', 'child', '', #{definition}, #{child}, $ )"
        )
        e.add(f"ITEM_DEFINED_TRANSFORMATION ( 'NONE', '', #{origin_axis}, #{origin_axis} )")
    return _document(e)


def _build_cylinder(
    radius: float,
    height: float,
    center: tuple[float, float],
    zmin: float,
    split_rims: bool,
) -> bytes:
    cx, cy = center
    z_of = {0: zmin, 1: zmin + height}  # rim 0 is the bottom, rim 1 the top
    e = _Entities()
    context, directions = _preamble(e)

    def placement(z: float, axis_sign: int) -> int:
        loc = e.add(_point((cx, cy, z)))
        return e.add(
            f"AXIS2_PLACEMENT_3D ( 'NONE', #{loc}, #{directions[(2, axis_sign)]}, "
            f"#{directions[(0, 1)]} )"
        )

    # Vertices sit at the seams of each rim: angle 0 and, when split, angle pi.
    angles = (0, 1) if split_rims else (0,)
    vertex: dict[tuple[int, int], int] = {}
    for rim in (0, 1):
        for k in angles:
            x = cx + radius if k == 0 else cx - radius
            vertex[(rim, k)] = e.add(
                f"VERTEX_POINT ( 'NONE', #{e.add(_point((x, cy, z_of[rim])))} )"
            )

    # Rim arcs run counter-clockwise (about +z) along the CIRCLE: 0 to pi, then pi to 2 pi.
    arc: dict[tuple[int, int], int] = {}
    for rim in (0, 1):
        circle = e.add(f"CIRCLE ( 'NONE', #{placement(z_of[rim], 1)}, {_num(radius)} )")
        for k in angles:
            end = vertex[(rim, (k + 1) % len(angles))]
            arc[(rim, k)] = e.add(
                f"EDGE_CURVE ( 'NONE', #{vertex[(rim, k)]}, #{end}, #{circle}, .T. )"
            )
    seam: dict[int, int] = {}
    for k in angles:
        x = cx + radius if k == 0 else cx - radius
        origin = e.add(_point((x, cy, z_of[0])))
        vector = e.add(f"VECTOR ( 'NONE', #{directions[(2, 1)]}, 1000.000000000000000 )")
        line = e.add(f"LINE ( 'NONE', #{origin}, #{vector} )")
        seam[k] = e.add(
            f"EDGE_CURVE ( 'NONE', #{vertex[(0, k)]}, #{vertex[(1, k)]}, #{line}, .T. )"
        )

    # Loops as (edge, forward) pairs, derived so that every edge is used twice, in opposite senses.
    top = [(arc[(1, k)], True) for k in angles]
    if split_rims:
        bottom = [(arc[(0, 1)], False), (arc[(0, 0)], False)]
        side = [
            (arc[(1, 1)], False),
            (seam[1], False),
            (arc[(0, 1)], True),
            (seam[0], True),
            (seam[0], False),
            (arc[(0, 0)], True),
            (seam[1], True),
            (arc[(1, 0)], False),
        ]
    else:
        bottom = [(arc[(0, 0)], False)]
        side = [(arc[(1, 0)], False), (seam[0], False), (arc[(0, 0)], True), (seam[0], True)]

    def face(loop: list[tuple[int, bool]], surface: int) -> int:
        oriented = [
            e.add(f"ORIENTED_EDGE ( 'NONE', *, *, #{edge}, {'.T.' if forward else '.F.'} )")
            for edge, forward in loop
        ]
        edge_loop = e.add(f"EDGE_LOOP ( 'NONE', ( {_ids(oriented)} ) )")
        bound = e.add(f"FACE_OUTER_BOUND ( 'NONE', #{edge_loop}, .T. )")
        return e.add(f"ADVANCED_FACE ( 'NONE', ( #{bound} ), #{surface}, .T. )")

    faces = [
        face(top, e.add(f"PLANE ( 'NONE',  #{placement(z_of[1], 1)} )")),
        face(bottom, e.add(f"PLANE ( 'NONE',  #{placement(z_of[0], -1)} )")),
        face(
            side,
            e.add(f"CYLINDRICAL_SURFACE ( 'NONE', #{placement(z_of[0], 1)}, {_num(radius)} )"),
        ),
    ]
    shell = e.add(f"CLOSED_SHELL ( 'NONE', ( {_ids(faces)} ) )")
    solid = e.add(f"MANIFOLD_SOLID_BREP ( 'cylinder', #{shell} )")
    origin_pt = e.add(_point((0.0, 0.0, 0.0)))
    origin_axis = e.add(
        f"AXIS2_PLACEMENT_3D ( 'NONE', #{origin_pt}, #{directions[(2, 1)]}, #{directions[(0, 1)]} )"
    )
    brep = e.add(
        f"ADVANCED_BREP_SHAPE_REPRESENTATION ( 'CYL', ( #{solid}, #{origin_axis} ), #{context} )"
    )
    _product(e, "cylinder", brep)
    return _document(e)


def make_box_step(
    size: tuple[float, float, float] = (1.6, 0.8, 0.5),
    center: tuple[float, float] = (0.0, 0.0),
    zmin: float = 0.0,
    non_ascii_name: bool = True,
) -> bytes:
    """A well formed AP214 style B-rep box with the given size, XY centre and lowest Z (mm)."""
    return _build(size, center, zmin, non_ascii_name, assembly=False)


def make_assembly_step() -> bytes:
    """The same box plus NEXT_ASSEMBLY_USAGE_OCCURRENCE and ITEM_DEFINED_TRANSFORMATION."""
    return _build((1.6, 0.8, 0.5), (0.0, 0.0), 0.0, True, assembly=True)


def make_cylinder_step(
    radius: float = 2.5,
    height: float = 5.0,
    center: tuple[float, float] = (0.0, 0.0),
    zmin: float = 0.0,
    split_rims: bool = True,
) -> bytes:
    """A round body (axis along +Z) like a radial capacitor, written the way SolidWorks does it.

    The only vertices lie on the seams of the two rims: with `split_rims` each rim is two half
    circle arcs (vertices at angle 0 and pi), otherwise one full circle with a single vertex.
    A bounding box built from vertices alone is therefore flat in Y; the circle arcs are needed.
    """
    return _build_cylinder(radius, height, center, zmin, split_rims)
