"""Tests for the SVGNODE (3D model placement) parser.

Inline tests use hand-written nodes with the footprint origin at (100, 200) EasyEDA units, so
10 units are exactly 2.54 mm and a point 10 units above the origin (smaller y) is +2.54 mm in Y.
Real fixtures (local only) and the synthetic fixtures check the same rules on whole documents.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from lcsc2fusion.model import Model3D
from lcsc2fusion.parser.model3d import (
    MODEL_WARNING_PREFIX,
    PLACEMENT_C_ORIGIN,
    PLACEMENT_OUTLINE_CENTRE,
    STALE_ORIGIN_TOLERANCE_MM,
    parse_model3d,
)
from lcsc2fusion.parser.part import parse_part

FIXTURES = Path(__file__).parent / "fixtures"
REAL_DIR = FIXTURES / "real"
SYNTHETIC_DIR = FIXTURES / "synthetic"
REAL_FILES = sorted(REAL_DIR.glob("*.json")) if REAL_DIR.is_dir() else []
real_only = pytest.mark.skipif(not REAL_FILES, reason="no real fixtures in tests/fixtures/real")

ORIGIN = (100.0, 200.0)
UUID = "0123456789abcdef0123456789abcdef"
OTHER_UUID = "fedcba9876543210fedcba9876543210"
TOLERANCE = 1e-4  # the parser rounds millimetres to 4 decimals
_MISSING: Any = object()  # marks an attribute or key that is left out of the node


def box_points(cx: float, cy: float, width: float, height: float) -> str:
    """Closed rectangle outline in canvas units, 'x y x y ...'."""
    x0, x1, y0, y1 = cx - width / 2, cx + width / 2, cy - height / 2, cy + height / 2
    return f"{x0} {y0} {x1} {y0} {x1} {y1} {x0} {y1} {x0} {y0}"


def node(
    *,
    uuid: Any = UUID,
    title: Any = "TEST-3D",
    c_origin: Any = "100,200",
    z: Any = "0",
    c_rotation: Any = "0,0,0",
    c_width: Any = "10",
    c_height: Any = "5",
    points: str | None = None,
    children: Any = "default",
) -> dict[str, Any]:
    """An SVGNODE JSON object. The default outline is a 10 x 5 rectangle centred on the origin."""
    attrs: dict[str, Any] = {
        "c_width": c_width,
        "c_height": c_height,
        "c_rotation": c_rotation,
        "z": z,
        "c_origin": c_origin,
        "uuid": uuid,
        "c_etype": "outline3D",
        "id": "g1_outline",
        "title": title,
        "layerid": "19",
        "transform": "scale(1) translate(0, 0)",
    }
    for key in [k for k, v in attrs.items() if v is _MISSING]:
        del attrs[key]
    if children == "default":
        text = box_points(*ORIGIN, 10, 5) if points is None else points
        children = [{"gId": "l0", "nodeName": "polyline", "attrs": {"points": text}}]
    out: dict[str, Any] = {"gId": "g1_outline", "nodeName": "g", "attrs": attrs}
    if children is not _MISSING:
        out["childNodes"] = children
    return out


def shape(**kwargs: Any) -> str:
    return "SVGNODE~" + json.dumps(node(**kwargs), separators=(",", ":"))


def detail(shapes: list[Any], **head: Any) -> dict[str, Any]:
    return {
        "title": "TEST-FP",
        "dataStr": {
            "head": {"x": ORIGIN[0], "y": ORIGIN[1], "c_para": {"package": "TEST-FP"}, **head},
            "shape": shapes,
        },
    }


def parse(*shapes: str, **head: Any) -> tuple[Model3D | None, list[str]]:
    return parse_model3d(detail([*shapes], **head))


def parse_ok(**kwargs: Any) -> tuple[Model3D, list[str]]:
    model, warnings = parse(shape(**kwargs))
    assert model is not None, warnings
    return model, warnings


# ============================================================
# Plain cases
# ============================================================


def test_plain_node() -> None:
    model, warnings = parse_ok(c_origin="110,190", points=box_points(110, 190, 10, 5))
    assert warnings == []
    assert model.uuid == UUID
    assert model.name == "TEST-3D"
    # (110 - 100) * 0.254 = 2.54, Y flipped: -(190 - 200) * 0.254 = +2.54
    assert model.offset_mm == pytest.approx((2.54, 2.54), abs=TOLERANCE)
    assert model.bottom_z_mm == 0.0
    assert model.rotation_deg == (0.0, 0.0, 0.0)
    assert model.native_size_mm == pytest.approx((2.54, 1.27), abs=TOLERANCE)
    assert model.placement_source == PLACEMENT_C_ORIGIN


def test_origin_at_the_package_origin_is_exactly_zero() -> None:
    model, _ = parse_ok()
    assert model.offset_mm == (0.0, 0.0)
    assert all(math.copysign(1.0, v) == 1.0 for v in model.offset_mm), "no negative zero"


def test_offset_flips_y_only() -> None:
    model, _ = parse_ok(c_origin="95,203", points=box_points(95, 203, 4, 4))
    assert model.offset_mm == pytest.approx((-1.27, -0.762), abs=TOLERANCE)


@pytest.mark.parametrize("rz", [90, 180, 270, -90, 45.5, 450])
def test_rotation_is_kept_as_given(rz: float) -> None:
    model, warnings = parse_ok(c_rotation=f"0,0,{rz}")
    assert model.rotation_deg == (0.0, 0.0, float(rz))
    assert warnings == []


def test_rotation_90_with_swapped_outline() -> None:
    """Native 10 x 5 turned by 90 degrees has a 5 x 10 outline: placement still from c_origin."""
    model, warnings = parse_ok(c_rotation="0,0,90", points=box_points(100, 200, 5, 10))
    assert model.rotation_deg == (0.0, 0.0, 90.0)
    assert model.native_size_mm == pytest.approx((2.54, 1.27), abs=TOLERANCE)  # unrotated
    assert model.placement_source == PLACEMENT_C_ORIGIN
    assert warnings == []


def test_rotation_180() -> None:
    model, warnings = parse_ok(c_rotation="0,0,180")
    assert model.rotation_deg == (0.0, 0.0, 180.0)
    assert warnings == []


@pytest.mark.parametrize(
    ("z", "mm"),
    [("0", 0.0), ("-3.6", -0.9144), ("2", 0.508), ("-13.7795", -3.5), (" 1.5 ", 0.381)],
)
def test_bottom_z_keeps_its_sign_and_is_not_flipped(z: str, mm: float) -> None:
    model, _ = parse_ok(z=z)
    assert model.bottom_z_mm == pytest.approx(mm, abs=1e-3)


def test_missing_z_is_zero() -> None:
    model, warnings = parse_ok(z=_MISSING)
    assert model.bottom_z_mm == 0.0
    assert warnings == []


def test_missing_rotation_is_zero() -> None:
    model, _ = parse_ok(c_rotation=_MISSING)
    assert model.rotation_deg == (0.0, 0.0, 0.0)


def test_numbers_as_strings_and_as_json_numbers_agree() -> None:
    as_text, _ = parse_ok(z="-3.6", c_width="12.5", c_height="6.25")
    as_number, _ = parse_ok(z=-3.6, c_width=12.5, c_height=6.25)
    assert as_text == as_number
    assert as_text.native_size_mm == pytest.approx((3.175, 1.5875), abs=TOLERANCE)


def test_integer_json_numbers() -> None:
    model, _ = parse_ok(z=-4, c_width=10, c_height=5)
    assert model.bottom_z_mm == pytest.approx(-1.016, abs=TOLERANCE)
    assert model.native_size_mm == pytest.approx((2.54, 1.27), abs=TOLERANCE)


@pytest.mark.parametrize(
    "c_origin", ["110,190", " 110 , 190 ", "110 190", "110, 190", "\t110,190\n"]
)
def test_c_origin_with_spaces(c_origin: str) -> None:
    model, warnings = parse_ok(c_origin=c_origin, points=box_points(110, 190, 10, 5))
    assert model.offset_mm == pytest.approx((2.54, 2.54), abs=TOLERANCE)
    assert model.placement_source == PLACEMENT_C_ORIGIN
    assert warnings == []


def test_title_may_contain_a_tilde_and_survives_the_shape_split() -> None:
    model, _ = parse_ok(title="A~B~C")
    assert model.name == "A~B~C"


def test_missing_title_gives_an_empty_name() -> None:
    model, _ = parse_ok(title=_MISSING)
    assert model.name == ""


def test_data_str_given_as_json_text() -> None:
    package = detail([shape()])
    package["dataStr"] = json.dumps(package["dataStr"])
    model, warnings = parse_model3d(package)
    assert model is not None
    assert warnings == []


def test_head_origin_missing_is_taken_as_zero() -> None:
    package = detail([shape(c_origin="10,20", points=box_points(10, 20, 4, 4))])
    del package["dataStr"]["head"]["x"]
    del package["dataStr"]["head"]["y"]
    model, warnings = parse_model3d(package)
    assert model is not None
    assert model.offset_mm == pytest.approx((2.54, -5.08), abs=TOLERANCE)
    assert warnings == []  # the footprint parser reports the missing origin


def test_head_origin_given_as_text() -> None:
    model, _ = parse(shape(c_origin="110,190", points=box_points(110, 190, 4, 4)), x="100", y="200")
    assert model is not None
    assert model.offset_mm == pytest.approx((2.54, 2.54), abs=TOLERANCE)


# ============================================================
# Stale c_origin and the outline
# ============================================================


def test_stale_c_origin_falls_back_to_the_outline_centre() -> None:
    model, warnings = parse_ok(c_origin="400,-120", points=box_points(110, 190, 10, 5))
    assert model.placement_source == PLACEMENT_OUTLINE_CENTRE
    assert model.offset_mm == pytest.approx((2.54, 2.54), abs=TOLERANCE)
    distance = math.hypot(400 - 110, -120 - 190) * 0.254
    assert len(warnings) == 1
    assert "stale" in warnings[0]
    assert f"{distance:.2f}" in warnings[0]


@pytest.mark.parametrize(
    ("shift_units", "source"),
    [
        (0.0, PLACEMENT_C_ORIGIN),
        (0.35, PLACEMENT_C_ORIGIN),  # 0.089 mm, inside the 0.1 mm tolerance
        (0.45, PLACEMENT_OUTLINE_CENTRE),  # 0.114 mm
        (-0.45, PLACEMENT_OUTLINE_CENTRE),
        (20.0, PLACEMENT_OUTLINE_CENTRE),
    ],
)
def test_stale_threshold(shift_units: float, source: str) -> None:
    assert STALE_ORIGIN_TOLERANCE_MM == 0.1
    model, warnings = parse_ok(
        c_origin=f"{110 + shift_units},190", points=box_points(110, 190, 10, 5)
    )
    assert model.placement_source == source
    assert len(warnings) == (1 if source == PLACEMENT_OUTLINE_CENTRE else 0)
    if source == PLACEMENT_C_ORIGIN:
        assert model.offset_mm[0] == pytest.approx((10 + shift_units) * 0.254, abs=TOLERANCE)
    else:
        assert model.offset_mm == pytest.approx((2.54, 2.54), abs=TOLERANCE)


def test_c_origin_wins_over_a_matching_outline_centre_to_within_the_tolerance() -> None:
    """An asymmetric outline whose centre differs by 0.025 mm keeps c_origin."""
    model, warnings = parse_ok(c_origin="100,200", points=box_points(100.1, 200, 10, 5))
    assert model.placement_source == PLACEMENT_C_ORIGIN
    assert model.offset_mm == (0.0, 0.0)
    assert warnings == []


def test_outline_is_the_bounding_box_of_all_polylines() -> None:
    """A second polyline outside the first moves the bounding box centre."""
    children = [
        {"nodeName": "polyline", "attrs": {"points": box_points(100, 200, 10, 5)}},
        {"nodeName": "polyline", "attrs": {"points": "105 200 115 200 115 202"}},
    ]
    # Union x: 95..115 -> centre 105, y: 197.5..202.5 -> centre 200
    model, warnings = parse_ok(c_origin="105,200", children=children)
    assert model.placement_source == PLACEMENT_C_ORIGIN
    assert warnings == []
    stale, stale_warnings = parse_ok(c_origin="100,200", children=children)
    assert stale.placement_source == PLACEMENT_OUTLINE_CENTRE
    assert stale.offset_mm == pytest.approx((1.27, 0.0), abs=TOLERANCE)
    assert len(stale_warnings) == 1


def test_outline_points_may_use_commas() -> None:
    model, warnings = parse_ok(
        c_origin="400,0", points="95,197.5 105,197.5 105,202.5 95,202.5", children="default"
    )
    assert model.placement_source == PLACEMENT_OUTLINE_CENTRE
    assert model.offset_mm == (0.0, 0.0)
    assert len(warnings) == 1


@pytest.mark.parametrize(
    "children",
    [_MISSING, [], None, [{"nodeName": "g"}], [{"attrs": {}}], [{"attrs": {"points": ""}}], ["x"]],
    ids=["missing", "empty", "null", "no-attrs", "no-points", "empty-points", "not-an-object"],
)
def test_no_outline_keeps_c_origin(children: Any) -> None:
    model, warnings = parse_ok(c_origin="110,190", children=children)
    assert model.placement_source == PLACEMENT_C_ORIGIN
    assert model.offset_mm == pytest.approx((2.54, 2.54), abs=TOLERANCE)
    assert warnings == []


@pytest.mark.parametrize(
    "children",
    [
        "not a list",
        [{"attrs": {"points": "1 2 3"}}],
        [{"attrs": {"points": "1 2 x 4"}}],
        [{"attrs": {"points": 5}}],
        [{"attrs": {"points": "1 2 3 4e9"}}],
        [{"attrs": {"points": "1 2 nan 4"}}],
    ],
    ids=["children-not-a-list", "odd", "not-a-number", "not-a-string", "absurd", "nan"],
)
def test_malformed_outline_is_ignored_with_a_warning(children: Any) -> None:
    model, warnings = parse_ok(c_origin="110,190", children=children)
    assert model.placement_source == PLACEMENT_C_ORIGIN
    assert model.offset_mm == pytest.approx((2.54, 2.54), abs=TOLERANCE)
    assert len(warnings) == 1
    assert "outline ignored" in warnings[0]


@pytest.mark.parametrize(
    "c_origin", [_MISSING, None, "", "110", "1,2,3", "a,b", "5e6,0", "nan,0", 7]
)
def test_unusable_c_origin_uses_the_outline_centre(c_origin: Any) -> None:
    model, warnings = parse_ok(c_origin=c_origin, points=box_points(110, 190, 10, 5))
    assert model.placement_source == PLACEMENT_OUTLINE_CENTRE
    assert model.offset_mm == pytest.approx((2.54, 2.54), abs=TOLERANCE)
    assert len(warnings) == 1
    assert "c_origin" in warnings[0]


@pytest.mark.parametrize("c_origin", [_MISSING, "a,b", "5e6,0"])
def test_unusable_c_origin_without_outline_drops_the_model(c_origin: Any) -> None:
    model, warnings = parse(shape(c_origin=c_origin, children=_MISSING))
    assert model is None
    assert len(warnings) == 1
    assert "c_origin" in warnings[0]


# ============================================================
# Rotation about X and Y
# ============================================================


def test_rotation_about_x_or_y_is_kept_with_a_warning() -> None:
    model, warnings = parse_ok(c_rotation="10,-20,90")
    assert model.rotation_deg == (10.0, -20.0, 90.0)
    assert len(warnings) == 1
    assert "only the rotation about Z" in warnings[0]


@pytest.mark.parametrize("c_rotation", ["90,0,0", "0,90,0", "0.5,0,0"])
def test_each_nonzero_axis_warns_once(c_rotation: str) -> None:
    _, warnings = parse_ok(c_rotation=c_rotation)
    assert len(warnings) == 1


@pytest.mark.parametrize("c_rotation", ["360,0,0", "0,-360,90", "0,720,0"])
def test_whole_turns_about_x_and_y_do_not_warn(c_rotation: str) -> None:
    _, warnings = parse_ok(c_rotation=c_rotation)
    assert warnings == []


# ============================================================
# Absent and malformed nodes never fail the part
# ============================================================


def test_no_svgnode_gives_none_without_warning() -> None:
    assert parse("TRACK~1~3~~0 0 10 10~gge1~0", "PAD~RECT~100~200~4~4~1~~1") == (None, [])
    assert parse() == (None, [])


def test_non_string_and_similar_tags_are_not_svgnodes() -> None:
    package = detail([5, None, "SVGNODEX~{}", "svgnode~{}", "", "TEXT~x"])
    assert parse_model3d(package) == (None, [])


def test_two_svgnodes_use_the_first_with_one_warning() -> None:
    model, warnings = parse(shape(uuid=UUID), shape(uuid=OTHER_UUID))
    assert model is not None
    assert model.uuid == UUID
    assert len(warnings) == 1
    assert "2 SVGNODE" in warnings[0]


def test_every_kind_of_warning_carries_the_stable_prefix() -> None:
    """The service filters Part.warnings on this prefix, so no message may lack it."""
    cases = [
        parse(shape(uuid=UUID), shape(uuid=OTHER_UUID)),  # several nodes
        parse("SVGNODE~{broken"),  # bad node
        parse(shape(c_rotation="10,20,90")),  # rx/ry
        parse(shape(c_origin="400,-120", points=box_points(110, 190, 10, 5))),  # stale origin
        parse(shape(c_origin=_MISSING, points=box_points(110, 190, 10, 5))),  # no origin
        parse(shape(c_origin="110,190", children=[{"attrs": {"points": "1 2 3"}}])),  # bad outline
    ]
    for _, warnings in cases:
        assert warnings
        assert all(w.startswith(MODEL_WARNING_PREFIX) for w in warnings)
    assert MODEL_WARNING_PREFIX == "3D model: "


def test_when_the_first_of_two_nodes_is_broken_the_model_is_dropped() -> None:
    model, warnings = parse("SVGNODE~{broken", shape(uuid=OTHER_UUID))
    assert model is None
    assert len(warnings) == 2


@pytest.mark.parametrize(
    "uuid",
    [
        "",
        "   ",
        "XYZ",
        UUID.upper(),
        UUID[:-1],
        UUID + "0",
        "0123456789abcdef0123456789abcdeg",
        "01234567-89ab-cdef-0123-456789abcdef",
        None,
        123,
        ["a"],
        _MISSING,
    ],
)
def test_invalid_uuid(uuid: Any) -> None:
    model, warnings = parse(shape(uuid=uuid))
    assert model is None
    assert len(warnings) == 1
    assert "uuid" in warnings[0]


def test_uuid_whitespace_is_stripped() -> None:
    model, _ = parse_ok(uuid=f"  {UUID}\n")
    assert model is not None
    assert model.uuid == UUID


@pytest.mark.parametrize(
    "raw",
    [
        "SVGNODE",
        "SVGNODE~",
        "SVGNODE~{not json",
        "SVGNODE~[1, 2]",
        'SVGNODE~"text"',
        "SVGNODE~null",
        "SVGNODE~42",
        "SVGNODE~{}",
        'SVGNODE~{"attrs": []}',
        'SVGNODE~{"attrs": null}',
        'SVGNODE~{"attrs": {"uuid": "' + "9" * 5000 + '"}}',
        "SVGNODE~" + "[" * 200000,
        'SVGNODE~{"attrs": {"z": ' + "9" * 5000 + "}}",
    ],
    ids=lambda raw: raw[:24],
)
def test_bad_json_gives_one_warning(raw: str) -> None:
    model, warnings = parse(raw)
    assert model is None
    assert len(warnings) == 1
    assert warnings[0].startswith(f"{MODEL_WARNING_PREFIX}ignored")


@pytest.mark.parametrize(
    "field",
    ["z", "c_width", "c_height", "c_rotation"],
)
@pytest.mark.parametrize(
    "bad",
    ["abc", "nan", "inf", "-inf", "1e999", "1e7", "-1e7", True, [], {}, "1,2"],
)
def test_invalid_numbers_drop_the_model(field: str, bad: Any) -> None:
    model, warnings = parse(shape(**{field: bad}))
    assert model is None
    assert len(warnings) == 1
    assert field in warnings[0] or "number" in warnings[0] or "range" in warnings[0]


@pytest.mark.parametrize("c_rotation", ["0,0", "0,0,0,0", "a,b,c", "0,0,", "90"])
def test_wrong_rotation_shape_drops_the_model(c_rotation: str) -> None:
    model, warnings = parse(shape(c_rotation=c_rotation))
    assert model is None
    assert len(warnings) == 1
    assert "c_rotation" in warnings[0]


@pytest.mark.parametrize("field", ["c_width", "c_height"])
def test_missing_or_negative_size_drops_the_model(field: str) -> None:
    assert parse(shape(**{field: _MISSING}))[0] is None
    assert parse(shape(**{field: "-1"}))[0] is None
    assert parse(shape(**{field: ""}))[0] is None


def test_size_limit_is_inclusive() -> None:
    model, _ = parse_ok(z="1000000", c_width="1000000")
    assert model.bottom_z_mm == pytest.approx(254000.0)
    assert parse(shape(z="1000001"))[0] is None


def test_absurd_head_origin_or_garbage_head_drops_the_model() -> None:
    assert parse(shape(), x="abc")[0] is None
    assert parse(shape(), y="1e9")[0] is None


@pytest.mark.parametrize(
    "package",
    [
        {},
        {"dataStr": None},
        {"dataStr": 5},
        {"dataStr": "{not json"},
        {"dataStr": "[1]"},
        {"dataStr": {"head": {"x": 0, "y": 0}}},
        {"dataStr": {"head": {"x": 0, "y": 0}, "shape": "SVGNODE~{}"}},
        {"dataStr": {"shape": [shape()]}},
        {"dataStr": {"head": [], "shape": [shape()]}},
    ],
)
def test_malformed_footprint_structure_gives_a_warning_not_an_exception(
    package: dict[str, Any],
) -> None:
    model, warnings = parse_model3d(package)
    assert model is None
    assert len(warnings) == 1


def test_never_raises_on_hostile_attribute_values() -> None:
    junk: list[Any] = [None, True, False, 0, -1, 1e30, math.nan, math.inf, "", " ", "x", "nan"]
    junk += ["1e999", [], {}, [1, 2], {"a": 1}, "0,0,0", "1 2", 2**70, -(2**70)]
    for field in ("uuid", "title", "c_origin", "z", "c_rotation", "c_width", "c_height"):
        for value in junk:
            result = parse(shape(**{field: value}))
            _check_result(result)
    for value in junk:
        _check_result(parse(shape(children=value)))
        _check_result(parse(shape(children=[{"attrs": {"points": value}}])))
        _check_result(parse(shape(children=[{"attrs": value}])))
        _check_result(parse(shape(children=[value])))
        _check_result(parse(shape(), x=value, y=value))
        assert parse_model3d(detail([value])) == (None, [])  # not a shape string at all


def _check_result(result: tuple[Model3D | None, list[str]]) -> None:
    model, warnings = result
    assert all(isinstance(w, str) and w for w in warnings)
    assert all(w.startswith(MODEL_WARNING_PREFIX) for w in warnings)
    if model is None:
        assert warnings, "a dropped node must explain itself"
        return
    numbers = [*model.offset_mm, model.bottom_z_mm, *model.rotation_deg, *model.native_size_mm]
    assert all(math.isfinite(v) for v in numbers)
    assert model.placement_source in (PLACEMENT_C_ORIGIN, PLACEMENT_OUTLINE_CENTRE)


# ============================================================
# parse_part wiring
# ============================================================


def synthetic_documents() -> list[Path]:
    return sorted(p for p in SYNTHETIC_DIR.glob("*.json") if not p.name.endswith(".expected.json"))


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def with_model3d() -> list[Path]:
    return [
        p for p in synthetic_documents() if "model3d" in load_json(p.with_suffix(".expected.json"))
    ]


def without_model3d() -> list[Path]:
    return [
        p
        for p in synthetic_documents()
        if "model3d" not in load_json(p.with_suffix(".expected.json"))
    ]


def test_synthetic_fixture_sets_are_not_empty() -> None:
    assert len(with_model3d()) >= 4
    assert len(without_model3d()) >= 10


@pytest.mark.parametrize("path", with_model3d(), ids=lambda p: p.stem)
def test_synthetic_model3d_matches_expected(path: Path) -> None:
    doc = load_json(path)
    want = load_json(path.with_suffix(".expected.json"))["model3d"]
    package = doc["result"]["packageDetail"]
    model, warnings = parse_model3d(package)
    assert model is not None
    assert model.uuid == want["uuid"]
    assert model.name == want["name"]
    assert model.offset_mm == pytest.approx(want["offset_mm"], abs=1e-3)
    assert model.bottom_z_mm == pytest.approx(want["bottom_z_mm"], abs=1e-3)
    assert model.rotation_deg == tuple(want["rotation_deg"])
    assert model.native_size_mm == pytest.approx(want["native_size_mm"], abs=1e-3)
    assert model.placement_source == want["placement_source"]
    assert [("stale" in w) for w in warnings] == ([True] if want["stale_origin"] else [])
    # The same through the whole part: model and warnings arrive on the Part.
    part = parse_part(doc)
    assert part.model3d == model
    assert [w for w in part.warnings if w.startswith("3D model")] == warnings


@pytest.mark.parametrize("path", without_model3d(), ids=lambda p: p.stem)
def test_synthetic_fixture_without_node_has_no_model(path: Path) -> None:
    doc = load_json(path)
    assert parse_model3d(doc["result"]["packageDetail"]) == (None, [])
    part = parse_part(doc)
    assert part.model3d is None
    assert not [w for w in part.warnings if "3D" in w]


def test_synthetic_rotation_and_z_cases_are_covered() -> None:
    models = {
        p.stem: parse_model3d(load_json(p)["result"]["packageDetail"])[0] for p in with_model3d()
    }
    assert models["box3d_rot90"] is not None
    assert models["box3d_rot90"].rotation_deg[2] == 90.0
    assert models["box3d_z"] is not None
    assert models["box3d_z"].bottom_z_mm == pytest.approx(-0.9144, abs=TOLERANCE)
    assert models["box3d_stale"] is not None
    assert models["box3d_stale"].placement_source == PLACEMENT_OUTLINE_CENTRE


def test_parse_part_without_svgnode_keeps_model_none_and_adds_no_warning() -> None:
    doc = load_json(SYNTHETIC_DIR / "soic8.json")
    shapes = doc["result"]["packageDetail"]["dataStr"]["shape"]
    assert not any(s.startswith("SVGNODE") for s in shapes)
    part = parse_part(doc)
    assert part.model3d is None


def test_parse_part_survives_a_broken_node() -> None:
    doc = load_json(SYNTHETIC_DIR / "r0603.json")
    shapes = doc["result"]["packageDetail"]["dataStr"]["shape"]
    shapes[:] = [s if not s.startswith("SVGNODE") else "SVGNODE~{broken" for s in shapes]
    part = parse_part(doc)
    assert part.model3d is None
    assert len(part.package.pads) == 2
    assert [w for w in part.warnings if w.startswith(f"{MODEL_WARNING_PREFIX}ignored")]


def test_parse_part_also_works_on_the_bare_result_object() -> None:
    doc = load_json(SYNTHETIC_DIR / "box3d_rot90.json")
    part = parse_part(doc["result"])
    assert part.model3d is not None
    assert part.model3d.rotation_deg[2] == 90.0


# ============================================================
# Real EasyEDA responses (local only)
# ============================================================

STALE_REAL = {"C1035", "C14663", "C6186", "C20526"}


def load_real(stem: str) -> dict[str, Any]:
    return load_json(REAL_DIR / f"{stem}.json")


def real_model(stem: str) -> tuple[Model3D | None, list[str]]:
    return parse_model3d(load_real(stem)["result"]["packageDetail"])


@real_only
@pytest.mark.real
@pytest.mark.parametrize("path", REAL_FILES, ids=lambda p: p.stem)
def test_real_part_has_a_model_with_a_valid_uuid(path: Path) -> None:
    result = load_json(path)["result"]
    model, warnings = parse_model3d(result["packageDetail"])
    if path.stem == "C476261":
        assert (model, warnings) == (None, [])
        return
    assert model is not None
    assert len(model.uuid) == 32
    assert all(c in "0123456789abcdef" for c in model.uuid)
    assert model.uuid != result["packageDetail"]["dataStr"]["head"].get("uuid_3d")
    assert model.name
    assert all(math.isfinite(v) for v in (*model.offset_mm, model.bottom_z_mm))
    assert all(v > 0 for v in model.native_size_mm)
    assert model.rotation_deg[:2] == (0.0, 0.0), "rx and ry are zero in all real samples"
    assert model.rotation_deg[2] in (0.0, 90.0, 180.0)
    # No real part sits further than a few centimetres from its footprint origin.
    assert math.hypot(*model.offset_mm) < 10.0


@real_only
@pytest.mark.real
@pytest.mark.parametrize("path", REAL_FILES, ids=lambda p: p.stem)
def test_real_part_placement_source_and_warnings(path: Path) -> None:
    model, warnings = parse_model3d(load_json(path)["result"]["packageDetail"])
    if path.stem == "C476261":
        assert model is None
        return
    assert model is not None
    if path.stem in STALE_REAL:
        assert model.placement_source == PLACEMENT_OUTLINE_CENTRE
        assert len(warnings) == 1
        assert "stale" in warnings[0]
    else:
        assert model.placement_source == PLACEMENT_C_ORIGIN
        assert warnings == []


@real_only
@pytest.mark.real
def test_real_stale_parts_are_the_documented_distances() -> None:
    """Distance of the stale c_origin from the model: 1.74, 0.51, 4.86 mm and over a metre."""
    expected = {"C1035": 1.74, "C14663": 0.51, "C6186": 4.86, "C20526": 1205.58}
    for stem, mm in expected.items():
        _, warnings = real_model(stem)
        assert f"{mm:.2f} mm" in warnings[0], (stem, warnings)


@real_only
@pytest.mark.real
@pytest.mark.parametrize(
    ("stem", "offset", "z", "rz", "size"),
    [
        ("C165948", (0.0, -1.1), -0.85, 180.0, (8.94, 7.9)),
        ("C46749", (-0.043, 0.0), -3.5, 0.0, None),
        ("C25804", (0.0, 0.0), 0.0, 90.0, None),
        ("C2040", (0.0, 0.0), 0.0, 0.0, None),
    ],
)
def test_real_known_measured_values(
    stem: str,
    offset: tuple[float, float],
    z: float,
    rz: float,
    size: tuple[float, float] | None,
) -> None:
    if not (REAL_DIR / f"{stem}.json").exists():
        pytest.skip(f"{stem} not available locally")
    model, _ = real_model(stem)
    assert model is not None
    assert model.offset_mm == pytest.approx(offset, abs=0.01)
    assert model.bottom_z_mm == pytest.approx(z, abs=0.01)
    assert model.rotation_deg[2] == rz
    if size is not None:
        assert model.native_size_mm == pytest.approx(size, abs=0.01)


@real_only
@pytest.mark.real
@pytest.mark.parametrize("path", REAL_FILES, ids=lambda p: p.stem)
def test_real_parse_part_carries_the_model(path: Path) -> None:
    doc = load_json(path)
    part = parse_part(doc)
    model, warnings = parse_model3d(doc["result"]["packageDetail"])
    assert part.model3d == model
    assert [w for w in part.warnings if w.startswith("3D model")] == warnings
