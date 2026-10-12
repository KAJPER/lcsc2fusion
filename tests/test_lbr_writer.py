"""Tests for lcsc2fusion.lbr_writer.writer: model to .lbr XML, with hand-built parts."""

from __future__ import annotations

import math
import os
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

import pytest

from lcsc2fusion import constants
from lcsc2fusion.constants import ATTRIBUTION, EAGLE_LAYERS, Layer
from lcsc2fusion.errors import ConversionError, LintError
from lcsc2fusion.lbr_writer.lint import lint_library
from lcsc2fusion.lbr_writer.writer import (
    build_library,
    format_number,
    sanitize_name,
    write_library,
)
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

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"


# ----- hand-built parts -----


def make_pad(
    name: str,
    x: float = 0.0,
    y: float = 0.0,
    *,
    number: str | None = None,
    width: float = 0.8,
    height: float = 0.95,
    shape: PadShape = PadShape.RECT,
    **kwargs: object,
) -> Pad:
    return Pad(
        name=name,
        number=number if number is not None else name,
        x=x,
        y=y,
        width=width,
        height=height,
        shape=shape,
        **kwargs,  # type: ignore[arg-type]
    )


def make_pin(
    name: str,
    number: str | None = None,
    x: float = 0.0,
    y: float = 0.0,
    angle: int = 0,
    direction: PinDirection = PinDirection.PAS,
    **kwargs: object,
) -> Pin:
    return Pin(
        name=name,
        number=number if number is not None else name,
        x=x,
        y=y,
        length=PinLength.SHORT,
        angle=angle,
        direction=direction,
        **kwargs,  # type: ignore[arg-type]
    )


def box(x: float, y: float, layer: Layer, width: float) -> tuple[Wire, ...]:
    corners = [(-x, -y), (x, -y), (x, y), (-x, y)]
    return tuple(Wire(*corners[i], *corners[(i + 1) % 4], width, layer) for i in range(4))


def resistor_package(name: str = "0603") -> Package:
    return Package(
        name=name,
        description="Chip resistor 0603",
        pads=(make_pad("1", -0.8), make_pad("2", 0.8)),
        graphics=(
            Wire(-0.2, 0.6, 0.2, 0.6, constants.SILK_WIDTH_MM, Layer.T_PLACE),
            Wire(-0.2, -0.6, 0.2, -0.6, constants.SILK_WIDTH_MM, Layer.T_PLACE),
            *box(0.8, 0.4, Layer.T_DOCU, constants.DOCU_WIDTH_MM),
            Label(-1.27, 1.1, ">NAME", 0.8, Layer.T_NAMES),
            Label(-1.27, -1.9, ">VALUE", 0.8, Layer.T_VALUES),
        ),
    )


def resistor_symbol() -> Symbol:
    return Symbol(
        name="R",
        pins=(make_pin("1", x=-5.08, angle=0), make_pin("2", x=5.08, angle=180)),
        graphics=(
            *box(2.54, 1.016, Layer.SYMBOLS, constants.SYMBOL_WIRE_WIDTH_MM),
            Label(-2.54, 2.54, ">NAME", 1.778, Layer.NAMES),
            Label(-2.54, -3.81, ">VALUE", 1.778, Layer.VALUES),
        ),
    )


def make_resistor(
    lcsc_id: str = "C25804", mpn: str = "0603WAF1002T5E", package: Package | None = None
) -> Part:
    return Part(
        lcsc_id=lcsc_id,
        mpn=mpn,
        manufacturer="UNI-ROYAL",
        description="10k 1% 0603 chip resistor",
        prefix="R?",
        symbols=(resistor_symbol(),),
        package=package if package is not None else resistor_package(),
        datasheet="https://example.com/r.pdf",
    )


def ic_package() -> Package:
    positions = [
        *((-1.5, y) for y in (0.75, 0.25, -0.25, -0.75)),
        *((1.5, y) for y in (-0.75, -0.25, 0.25, 0.75)),
    ]
    smd_pads = [
        make_pad(str(n), x, y, width=0.8, height=0.3) for n, (x, y) in enumerate(positions, start=1)
    ]
    thermal = [
        make_pad("9", 0.0, 0.6, number="9", width=1.7, height=1.0),
        make_pad("9_2", 0.0, -0.6, number="9", width=1.7, height=1.0),
    ]
    slot = make_pad(
        "10",
        0.0,
        -3.0,
        shape=PadShape.OVAL,
        width=2.6,
        height=1.2,
        drill=0.6,
        slot_length=1.8,
        slot_angle=0.0,
    )
    return Package(
        name="qfn-8-ep 3x3",
        description="Synthetic 8 pin QFN with thermal pad and a slotted shield pad",
        pads=(*smd_pads, *thermal, slot),
        holes=(Hole(3.0, 3.0, 1.0),),
        graphics=(
            Wire(-1.6, 1.6, 1.6, 1.6, constants.SILK_WIDTH_MM, Layer.T_PLACE),
            Wire(1.6, -1.6, -1.6, -1.6, constants.SILK_WIDTH_MM, Layer.T_PLACE, -90.0),
            Circle(-2.0, 1.0, 0.1, 0.0, Layer.T_PLACE),
            Rectangle(-1.5, -1.5, 1.5, 1.5, Layer.T_DOCU),
            Polygon(
                (Point(-1.5, 1.5), Point(-1.0, 1.5), Point(-1.5, 1.0)),
                0.0,
                Layer.T_DOCU,
            ),
            Label(0.0, 2.2, ">NAME", 0.8, Layer.T_NAMES, rotation=90.0, align=TextAlign.CENTER),
            Label(0.0, -4.0, ">VALUE", 0.8, Layer.T_VALUES),
        ),
    )


def ic_symbol() -> Symbol:
    pins = (
        make_pin("VIN", "1", -10.16, 5.08, 0, PinDirection.PWR),
        make_pin("EN", "2", -10.16, 2.54, 0, PinDirection.IN, inverted=True),
        make_pin("CLK", "3", -10.16, 0.0, 0, PinDirection.IN, clock=True),
        make_pin("RST", "4", -10.16, -2.54, 0, PinDirection.IN, inverted=True, clock=True),
        make_pin("OUT", "5", 10.16, 5.08, 180, PinDirection.OUT),
        make_pin("FB", "6", 10.16, 2.54, 180, PinDirection.IO),
        make_pin("NC", "7", 10.16, 0.0, 180, PinDirection.NC),
        make_pin("VCC", "8", 10.16, -2.54, 180, PinDirection.SUP),
        make_pin("EP", "9", 0.0, -10.16, 90, PinDirection.PAS),
        make_pin("SH", "10", 0.0, 10.16, 270, PinDirection.PAS),
    )
    return Symbol(
        name="IC",
        pins=pins,
        graphics=(
            *box(7.62, 7.62, Layer.SYMBOLS, constants.SYMBOL_WIRE_WIDTH_MM),
            Label(-7.62, 8.89, ">NAME", 1.778, Layer.NAMES),
            Label(-7.62, -10.16, ">VALUE", 1.778, Layer.VALUES),
        ),
    )


def make_ic() -> Part:
    return Part(
        lcsc_id="C000001",
        mpn="TPS-TEST1",
        manufacturer="Test Mfr & Sons",
        description="Step-down converter <3A & 2.5V to 5.5V",
        prefix="U?",
        symbols=(ic_symbol(),),
        package=ic_package(),
        datasheet="https://example.com/ds.pdf?a=1&b=2",
        attributes=(("Package", "QFN-8"), ("manufacturer", "dup"), ("Supply Voltage", "5V")),
    )


def parse(text: str) -> ET.Element:
    return ET.fromstring(text)


def find_one(root: ET.Element, path: str) -> ET.Element:
    element = root.find(path)
    assert element is not None, f"{path} not found"
    return element


# ----- structure -----


def test_header_and_doctype() -> None:
    text = build_library([make_resistor()])
    lines = text.split("\n")
    assert lines[0] == '<?xml version="1.0" encoding="utf-8"?>'
    assert lines[1] == '<!DOCTYPE eagle SYSTEM "eagle.dtd">'
    assert lines[2] == f'<eagle version="{constants.EAGLE_VERSION}">'
    assert text.endswith("</eagle>\n")
    assert "\r" not in text


def test_output_parses_and_has_dtd_order() -> None:
    root = parse(build_library([make_resistor()]))
    assert root.tag == "eagle"
    drawing = find_one(root, "drawing")
    assert [c.tag for c in drawing] == ["settings", "grid", "layers", "library"]
    settings = find_one(drawing, "settings")
    assert [dict(s.attrib) for s in settings] == [
        {"alwaysvectorfont": "no"},
        {"verticaltext": "up"},
    ]
    library = find_one(drawing, "library")
    assert [c.tag for c in library] == ["description", "packages", "symbols", "devicesets"]


def test_all_46_layers_are_declared() -> None:
    root = parse(build_library([make_resistor()]))
    layers = root.findall("./drawing/layers/layer")
    assert len(layers) == 46
    assert len(EAGLE_LAYERS) == 46
    for element, expected in zip(layers, EAGLE_LAYERS, strict=True):
        assert element.attrib == {
            "number": str(expected.number),
            "name": expected.name,
            "color": str(expected.color),
            "fill": str(expected.fill),
            "visible": "yes" if expected.visible else "no",
            "active": "yes",
        }


def test_generated_libraries_lint_clean() -> None:
    assert lint_library(build_library([make_resistor()])) == []
    assert lint_library(build_library([make_ic()])) == []
    assert lint_library(build_library([make_resistor(), make_ic()])) == []


def test_output_is_deterministic() -> None:
    parts = [make_resistor(), make_ic()]
    assert build_library(parts) == build_library(list(parts))


def test_library_description_has_attribution_and_ids() -> None:
    root = parse(build_library([make_resistor(), make_ic()], description="My <test> lib"))
    text = find_one(root, "./drawing/library/description").text or ""
    assert ATTRIBUTION in text
    assert "C25804" in text
    assert "C000001" in text
    assert "My &lt;test&gt; lib" in text  # HTML escaped inside the XML text


def test_referential_integrity() -> None:
    root = parse(build_library([make_resistor(), make_ic()]))
    packages = {p.get("name"): p for p in root.findall(".//package")}
    symbols = {s.get("name"): s for s in root.findall(".//symbols/symbol")}
    for deviceset in root.findall(".//deviceset"):
        gates = {g.get("name"): g.get("symbol") for g in deviceset.findall("./gates/gate")}
        assert gates
        for symbol_name in gates.values():
            assert symbol_name in symbols
        for device in deviceset.findall("./devices/device"):
            package = packages[device.get("package")]
            pad_names = {e.get("name") for e in package if e.tag in ("pad", "smd")}
            for connect in device.findall("./connects/connect"):
                symbol = symbols[gates[connect.get("gate")]]
                assert connect.get("pin") in {p.get("name") for p in symbol.findall("pin")}
                for pad in (connect.get("pad") or "").split():
                    assert pad in pad_names


# ----- names -----


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0603WAF1002T5E", "0603WAF1002T5E"),
        ("tl072 cdt", "TL072_CDT"),
        ("A;B/C", "A_B_C"),
        ("a-b.c+d_e", "A-B.C+D_E"),
        ("  x  ", "X"),
        ("TPS63020(DSJR)[x]", "TPS63020_DSJR__X_"),
        (chr(0xE9) + "t" + chr(0xE9), "_T_"),
        ("", "FALLBACK"),
        ("   ", "FALLBACK"),
    ],
)
def test_sanitize_name(raw: str, expected: str) -> None:
    assert sanitize_name(raw, "fallback") == expected


def test_sanitize_name_without_any_usable_text_raises() -> None:
    with pytest.raises(ConversionError):
        sanitize_name("", "")


def test_names_are_sanitised_in_output() -> None:
    package = replace(resistor_package(), name="Chip R 0603;(a)")
    part = replace(make_resistor(mpn="tl072 cdt;x/y"), package=package)
    root = parse(build_library([part]))
    assert find_one(root, ".//deviceset").get("name") == "TL072_CDT_X_Y"
    assert find_one(root, ".//symbols/symbol").get("name") == "TL072_CDT_X_Y"
    assert find_one(root, ".//package").get("name") == "CHIP_R_0603__A_"
    assert find_one(root, ".//device").get("package") == "CHIP_R_0603__A_"


def test_deviceset_name_falls_back_to_lcsc_id() -> None:
    root = parse(build_library([make_resistor(mpn="")]))
    assert find_one(root, ".//deviceset").get("name") == "C25804"


def test_names_are_unique_case_insensitively() -> None:
    parts = [
        make_resistor("C1", "ab-1"),
        make_resistor("C2", "AB-1"),
        make_resistor("C3", "Ab-1"),
    ]
    root = parse(build_library(parts))
    names = [d.get("name") for d in root.findall(".//deviceset")]
    assert names == ["AB-1", "AB-1_2", "AB-1_3"]
    symbols = [s.get("name") for s in root.findall(".//symbols/symbol")]
    assert symbols == names
    gates = [g.get("symbol") for g in root.findall(".//gate")]
    assert gates == names


def test_symbol_name_clash_with_multi_unit_naming_is_resolved() -> None:
    """Deviceset X_1 (single unit) and deviceset X (two units) would both want symbol X_1."""
    two_units = replace(make_resistor("C1", "X"), symbols=(resistor_symbol(), resistor_symbol()))
    single = make_resistor("C2", "X_1")
    root = parse(build_library([two_units, single]))
    names = [s.get("name") for s in root.findall(".//symbols/symbol")]
    assert len(names) == len({n.upper() for n in names if n}) == 3
    for gate in root.findall(".//gate"):
        assert gate.get("symbol") in names


# ----- packages -----


def test_identical_packages_are_shared() -> None:
    root = parse(build_library([make_resistor("C1", "R-A"), make_resistor("C2", "R-B")]))
    assert [p.get("name") for p in root.findall(".//packages/package")] == ["0603"]
    assert [d.get("package") for d in root.findall(".//device")] == ["0603", "0603"]


def test_same_package_name_with_different_content_gets_suffix() -> None:
    bigger = replace(
        resistor_package(), pads=(make_pad("1", -1.0, width=1.0), make_pad("2", 1.0, width=1.0))
    )
    third = replace(resistor_package(), description="Another variant")
    parts = [
        make_resistor("C1", "R-A"),
        make_resistor("C2", "R-B", bigger),
        make_resistor("C3", "R-C", resistor_package()),
        make_resistor("C4", "R-D", third),
        make_resistor("C5", "R-E", bigger),
    ]
    root = parse(build_library(parts))
    assert [p.get("name") for p in root.findall(".//packages/package")] == [
        "0603",
        "0603_2",
        "0603_3",
    ]
    assert [d.get("package") for d in root.findall(".//device")] == [
        "0603",
        "0603_2",
        "0603",
        "0603_3",
        "0603_2",
    ]


# ----- pads -----


def only_pad(pad: Pad, tag: str) -> ET.Element:
    package = Package(name="T", description="", pads=(pad,))
    part = replace(
        make_resistor(),
        symbols=(Symbol("S", (make_pin("1", pad.number),)),),
        package=package,
    )
    return find_one(parse(build_library([part])), f".//package/{tag}")


def test_smd_rect_pad() -> None:
    smd = only_pad(make_pad("1", 1.5, -2.25, width=0.8, height=0.95), "smd")
    assert smd.attrib == {
        "name": "1",
        "x": "1.5",
        "y": "-2.25",
        "dx": "0.8",
        "dy": "0.95",
        "layer": "1",
        "roundness": "0",
    }


@pytest.mark.parametrize(
    ("shape", "roundness"),
    [(PadShape.RECT, "0"), (PadShape.OVAL, "100"), (PadShape.ROUND, "100")],
)
def test_smd_roundness(shape: PadShape, roundness: str) -> None:
    assert only_pad(make_pad("1", shape=shape), "smd").get("roundness") == roundness


@pytest.mark.parametrize(
    ("rotation", "rot"),
    [(0.0, None), (90.0, "R90"), (-90.0, "R270"), (45.5, "R45.5"), (360.0, None)],
)
def test_smd_rotation(rotation: float, rot: str | None) -> None:
    assert only_pad(make_pad("1", rotation=rotation), "smd").get("rot") == rot


def test_smd_on_bottom_layer() -> None:
    assert only_pad(make_pad("1", layer=Layer.BOTTOM), "smd").get("layer") == "16"


def test_smd_on_other_layer_is_rejected() -> None:
    with pytest.raises(ConversionError, match="layer"):
        only_pad(make_pad("1", layer=Layer.T_PLACE), "smd")


def test_polygon_smd_pad_becomes_bounding_box() -> None:
    outline = (Point(0, 0), Point(2, 0), Point(2.5, 1), Point(0, 1))
    pad = make_pad("1", 9.0, 9.0, shape=PadShape.POLYGON, rotation=30.0, outline=outline)
    smd = only_pad(pad, "smd")
    assert (smd.get("x"), smd.get("y"), smd.get("dx"), smd.get("dy")) == ("1.25", "0.5", "2.5", "1")
    assert smd.get("roundness") == "0"
    assert smd.get("rot") is None


@pytest.mark.parametrize(
    ("shape", "width", "height", "eagle_shape", "diameter", "rot"),
    [
        (PadShape.ROUND, 1.6, 1.4, "round", "1.6", None),
        (PadShape.RECT, 1.5, 2.0, "square", "2", None),
        (PadShape.OVAL, 2.4, 1.2, "long", "1.2", None),
        (PadShape.OVAL, 1.2, 2.4, "long", "1.2", "R90"),
        (PadShape.POLYGON, 1.0, 1.0, "square", "1", None),
    ],
)
def test_through_hole_pad_shapes(
    shape: PadShape,
    width: float,
    height: float,
    eagle_shape: str,
    diameter: str,
    rot: str | None,
) -> None:
    pad = only_pad(
        make_pad("1", 2.0, 3.0, width=width, height=height, shape=shape, drill=0.9), "pad"
    )
    assert pad.get("shape") == eagle_shape
    assert pad.get("diameter") == diameter
    assert pad.get("drill") == "0.9"
    assert (pad.get("x"), pad.get("y")) == ("2", "3")
    assert pad.get("rot") == rot
    assert "first" not in pad.attrib


def test_vertical_oval_pad_adds_to_existing_rotation() -> None:
    pad = only_pad(
        make_pad("1", width=1.2, height=2.4, shape=PadShape.OVAL, drill=0.8, rotation=90.0),
        "pad",
    )
    assert pad.get("rot") == "R180"


def test_through_hole_without_slot_has_no_milling_wires() -> None:
    package = Package("T", "", (make_pad("1", drill=0.8, shape=PadShape.ROUND),))
    part = replace(make_resistor(), symbols=(Symbol("S", (make_pin("1"),)),), package=package)
    root = parse(build_library([part]))
    assert root.findall(".//package/wire") == []


def test_slot_pad_generates_milling_outline_when_native_slots_are_off() -> None:
    pad = make_pad(
        "1",
        10.0,
        5.0,
        shape=PadShape.OVAL,
        width=2.0,
        height=1.5,
        drill=1.0,
        slot_length=3.0,
        slot_angle=0.0,
    )
    package = Package("T", "", (pad,))
    part = replace(make_resistor(), symbols=(Symbol("S", (make_pin("1"),)),), package=package)
    root = parse(build_library([part], native_slots=False))
    assert root.get("version") == constants.EAGLE_VERSION
    assert find_one(root, ".//package/pad").get("drill") == "1"
    assert find_one(root, ".//package/pad").get("shape") != "slot"
    wires = root.findall(".//package/wire")
    assert [w.get("layer") for w in wires] == ["46"] * 4
    assert {w.get("width") for w in wires} == {"0.02"}
    assert [
        (w.get("x1"), w.get("y1"), w.get("x2"), w.get("y2"), w.get("curve")) for w in wires
    ] == [
        ("9", "4.5", "11", "4.5", None),
        ("11", "4.5", "11", "5.5", "180"),
        ("11", "5.5", "9", "5.5", None),
        ("9", "5.5", "9", "4.5", "180"),
    ]


def test_slot_pad_outline_follows_the_slot_angle() -> None:
    pad = make_pad(
        "1",
        10.0,
        5.0,
        shape=PadShape.OVAL,
        width=1.5,
        height=2.0,
        drill=1.0,
        slot_length=3.0,
        slot_angle=90.0,
    )
    package = Package("T", "", (pad,))
    part = replace(make_resistor(), symbols=(Symbol("S", (make_pin("1"),)),), package=package)
    wires = parse(build_library([part], native_slots=False)).findall(".//package/wire")
    assert len(wires) == 4
    endpoints = {(w.get("x1"), w.get("y1")) for w in wires} | {
        (w.get("x2"), w.get("y2")) for w in wires
    }
    assert endpoints == {("10.5", "4"), ("10.5", "6"), ("9.5", "6"), ("9.5", "4")}
    assert [w.get("curve") for w in wires] == [None, "180", None, "180"]


def test_non_plated_hole() -> None:
    package = Package("T", "", (make_pad("1"),), holes=(Hole(1.5, -2.0, 1.1),))
    part = replace(make_resistor(), symbols=(Symbol("S", (make_pin("1"),)),), package=package)
    hole = find_one(parse(build_library([part])), ".//package/hole")
    assert hole.attrib == {"x": "1.5", "y": "-2", "drill": "1.1"}


def test_package_description_is_html_escaped_text() -> None:
    package = replace(resistor_package(), description="a < b & c")
    root = parse(build_library([replace(make_resistor(), package=package)]))
    assert find_one(root, ".//package/description").text == "a &lt; b &amp; c"


# ----- graphics -----


def graphics_of(*graphics: object) -> ET.Element:
    package = Package("T", "", (make_pad("1"),), graphics=graphics)  # type: ignore[arg-type]
    part = replace(make_resistor(), symbols=(Symbol("S", (make_pin("1"),)),), package=package)
    return find_one(parse(build_library([part])), ".//package")


def test_wire_curve_only_when_nonzero() -> None:
    package = graphics_of(
        Wire(0, 0, 1, 1, 0.1, Layer.T_PLACE),
        Wire(0, 0, 1, 1, 0.1, Layer.T_PLACE, -90.0),
    )
    straight, arc = package.findall("wire")
    assert straight.attrib == {
        "x1": "0",
        "y1": "0",
        "x2": "1",
        "y2": "1",
        "width": "0.1",
        "layer": "21",
    }
    assert arc.get("curve") == "-90"


def test_wire_curve_out_of_range() -> None:
    assert graphics_of(Wire(0, 0, 1, 1, 0.1, Layer.T_PLACE, 359.95)).find("wire") is not None
    with pytest.raises(ConversionError, match="arc"):
        graphics_of(Wire(0, 0, 1, 1, 0.1, Layer.T_PLACE, 360.0))


def test_circle_and_rectangle() -> None:
    package = graphics_of(
        Circle(1, 2, 0.5, 0.0, Layer.T_PLACE),
        Rectangle(0, 0, 1, 2, Layer.T_DOCU),
        Rectangle(0, 0, 1, 2, Layer.T_DOCU, rotation=90.0),
    )
    circle = find_one(package, "circle")
    assert circle.attrib == {"x": "1", "y": "2", "radius": "0.5", "width": "0", "layer": "21"}
    plain, rotated = package.findall("rectangle")
    assert "rot" not in plain.attrib
    assert rotated.get("rot") == "R90"


def test_polygon_width_is_clamped_and_vertices_written() -> None:
    package = graphics_of(
        Polygon((Point(0, 0), Point(1, 0), Point(1, 1)), 0.0, Layer.T_DOCU),
        Polygon((Point(0, 0), Point(1, 0), Point(1, 1)), 0.2, Layer.T_DOCU),
    )
    clamped, kept = package.findall("polygon")
    assert clamped.get("width") == format_number(constants.MIN_POLYGON_WIDTH_MM)
    assert kept.get("width") == "0.2"
    assert [(v.get("x"), v.get("y")) for v in clamped.findall("vertex")] == [
        ("0", "0"),
        ("1", "0"),
        ("1", "1"),
    ]


def test_polygon_with_too_few_vertices_is_rejected() -> None:
    with pytest.raises(ConversionError, match="vertices"):
        graphics_of(Polygon((Point(0, 0), Point(1, 0)), 0.1, Layer.T_DOCU))


def test_text_attributes_and_escaping() -> None:
    package = graphics_of(
        Label(1, 2, ">NAME", 0.8, Layer.T_NAMES),
        Label(1, 2, "a<b & c>d", 1.0, Layer.T_PLACE, rotation=-90.0, align=TextAlign.TOP_CENTER),
        Label(1, 2, "   ", 1.0, Layer.T_PLACE),
    )
    plain, special = package.findall("text")  # blank labels are dropped
    assert plain.attrib == {
        "x": "1",
        "y": "2",
        "size": "0.8",
        "layer": "25",
        "font": "vector",
        "ratio": "12",
    }
    assert plain.text == ">NAME"
    assert special.get("rot") == "R270"
    assert special.get("align") == "top-center"
    assert special.text == "a<b & c>d"


def test_text_is_escaped_in_the_raw_xml() -> None:
    text = build_library([make_resistor()])
    assert "<text " in text
    assert ">&gt;NAME</text>" in text


# ----- symbols -----


def pins_of(symbol: Symbol) -> list[ET.Element]:
    part = replace(make_resistor(), symbols=(symbol,))
    # the resistor package has pads 1 and 2, so use pin numbers from the symbol directly
    package = Package("T", "", tuple(make_pad(p.number, number=p.number) for p in symbol.pins))
    part = replace(part, package=package)
    return parse(build_library([part])).findall(".//symbols/symbol/pin")


def test_two_pin_passive_symbol_hides_names_and_is_swappable() -> None:
    pins = pins_of(Symbol("S", (make_pin("1"), make_pin("2", angle=180))))
    assert [p.get("visible") for p in pins] == ["off", "off"]
    assert [p.get("swaplevel") for p in pins] == ["1", "1"]
    assert pins[0].get("rot") is None
    assert pins[1].get("rot") == "R180"


def test_two_pin_symbol_not_swappable_when_not_passive() -> None:
    pins = pins_of(Symbol("S", (make_pin("1", direction=PinDirection.IN), make_pin("2"))))
    assert [p.get("visible") for p in pins] == ["off", "off"]
    assert all("swaplevel" not in p.attrib for p in pins)


def test_multi_pin_symbol_shows_names_and_never_swaps() -> None:
    pins = pins_of(Symbol("S", (make_pin("1"), make_pin("2"), make_pin("3"))))
    assert {p.get("visible") for p in pins} == {"both"}
    assert all("swaplevel" not in p.attrib for p in pins)


def test_pin_attributes() -> None:
    pins = pins_of(
        Symbol(
            "S",
            (
                make_pin("1", x=-10.16, y=2.54, direction=PinDirection.IN),
                make_pin("2", angle=90, direction=PinDirection.OUT),
                make_pin("3", angle=270),
            ),
        )
    )
    assert pins[0].attrib == {
        "name": "1",
        "x": "-10.16",
        "y": "2.54",
        "visible": "both",
        "length": "short",
        "direction": "in",
    }
    assert pins[1].get("rot") == "R90"
    assert pins[1].get("direction") == "out"
    assert pins[2].get("rot") == "R270"


def test_pin_function_values_and_no_function_none() -> None:
    symbol = Symbol(
        "S",
        (
            make_pin("1"),
            make_pin("2", inverted=True),
            make_pin("3", clock=True),
            make_pin("4", inverted=True, clock=True),
        ),
    )
    pins = pins_of(symbol)
    assert [p.get("function") for p in pins] == [None, "dot", "clk", "dotclk"]
    text = build_library([make_resistor(), make_ic()])
    assert 'function="none"' not in text
    assert "function=" in text


def test_pin_angle_must_be_a_right_angle() -> None:
    with pytest.raises(ConversionError, match="angle"):
        pins_of(Symbol("S", (make_pin("1", angle=45),)))


def test_pin_names_lose_whitespace_and_semicolons_but_keep_case_and_symbols() -> None:
    pins = pins_of(Symbol("S", (make_pin("!RESET;x y", "1"), make_pin("GND@1", "2"))))
    assert [p.get("name") for p in pins] == ["!RESET_x_y", "GND@1"]


# ----- device set -----


@pytest.mark.parametrize(
    ("prefix", "expected", "uservalue"),
    [
        ("R?", "R", "yes"),
        ("C?", "C", "yes"),
        ("L", "L", "yes"),
        ("U?", "U", None),
        ("LED?", "LED", None),
        ("", "U", None),
        ("?", "U", None),
        ("Q ?", "Q", None),
    ],
)
def test_deviceset_prefix(prefix: str, expected: str, uservalue: str | None) -> None:
    deviceset = find_one(
        parse(build_library([replace(make_resistor(), prefix=prefix)])), ".//deviceset"
    )
    assert deviceset.get("prefix") == expected
    assert deviceset.get("uservalue") == uservalue


def test_technology_attributes() -> None:
    root = parse(build_library([make_ic()]))
    technology = find_one(root, ".//technology")
    assert technology.get("name") == ""
    attributes = [(a.get("name"), a.get("value"), a.get("constant")) for a in technology]
    assert attributes == [
        ("MPN", "TPS-TEST1", "no"),
        ("MANUFACTURER", "Test Mfr & Sons", "no"),
        ("LCSC_PART", "C000001", "no"),
        ("DATASHEET", "https://example.com/ds.pdf?a=1&b=2", "no"),
        ("PACKAGE", "QFN-8", "no"),
        ("SUPPLY_VOLTAGE", "5V", "no"),
        ("SOURCE", ATTRIBUTION, "no"),
    ]


def test_empty_attributes_are_omitted() -> None:
    part = replace(make_resistor(), datasheet="", manufacturer="")
    names = [a.get("name") for a in parse(build_library([part])).findall(".//attribute")]
    assert names == ["MPN", "LCSC_PART", "SOURCE"]


def test_deviceset_description_has_mpn_description_and_attribution() -> None:
    root = parse(build_library([make_ic()]))
    text = find_one(root, ".//deviceset/description").text or ""
    assert "TPS-TEST1" in text
    assert "Step-down converter &lt;3A &amp; 2.5V to 5.5V" in text
    assert ATTRIBUTION in text


def test_device_and_deviceset_structure() -> None:
    deviceset = find_one(parse(build_library([make_resistor()])), ".//deviceset")
    assert [c.tag for c in deviceset] == ["description", "gates", "devices"]
    device = find_one(deviceset, "./devices/device")
    assert device.attrib == {"name": "", "package": "0603"}
    assert [c.tag for c in device] == ["connects", "technologies"]
    assert len(deviceset.findall("./devices/device")) == 1


def test_duplicate_pad_numbers_produce_a_pad_list() -> None:
    package = Package(
        "T",
        "",
        (
            make_pad("1", 0, 0),
            make_pad("41", 1, 0, number="41"),
            make_pad("41_2", 2, 0, number="41"),
            make_pad("41_3", 3, 0, number="41"),
        ),
    )
    symbol = Symbol("S", (make_pin("A", "1"), make_pin("GND", "41")))
    root = parse(build_library([replace(make_resistor(), symbols=(symbol,), package=package)]))
    connects = [(c.get("pin"), c.get("pad")) for c in root.findall(".//connect")]
    assert connects == [("A", "1"), ("GND", "41 41_2 41_3")]


def test_ic_thermal_pad_is_connected_to_both_pads() -> None:
    root = parse(build_library([make_ic()]))
    connects = {c.get("pin"): c.get("pad") for c in root.findall(".//connect")}
    assert connects["EP"] == "9 9_2"
    assert connects["SH"] == "10"
    assert len(connects) == 10


def test_multi_unit_part_gets_one_symbol_and_gate_per_unit() -> None:
    unit_a = Symbol("A", (make_pin("IN1", "1"), make_pin("OUT1", "2"), make_pin("VM", "3")))
    unit_b = Symbol("B", (make_pin("IN2", "5"), make_pin("OUT2", "6"), make_pin("VP", "7")))
    unit_c = Symbol("C", (make_pin("P", "8"),))
    package = Package("SO-8", "", tuple(make_pad(str(n), n) for n in (1, 2, 3, 5, 6, 7, 8)))
    part = Part(
        lcsc_id="C6961",
        mpn="TL072CDT",
        manufacturer="ST",
        description="dual op amp",
        prefix="U?",
        symbols=(unit_a, unit_b, unit_c),
        package=package,
    )
    root = parse(build_library([part]))
    assert [s.get("name") for s in root.findall(".//symbols/symbol")] == [
        "TL072CDT_1",
        "TL072CDT_2",
        "TL072CDT_3",
    ]
    gates = root.findall(".//gate")
    assert [
        (g.get("name"), g.get("symbol"), g.get("x"), g.get("y"), g.get("addlevel")) for g in gates
    ] == [
        ("G$1", "TL072CDT_1", "0", "0", "next"),
        ("G$2", "TL072CDT_2", "0", "-25.4", "next"),
        ("G$3", "TL072CDT_3", "0", "-50.8", "next"),
    ]
    connects = [(c.get("gate"), c.get("pin"), c.get("pad")) for c in root.findall(".//connect")]
    assert connects == [
        ("G$1", "IN1", "1"),
        ("G$1", "OUT1", "2"),
        ("G$1", "VM", "3"),
        ("G$2", "IN2", "5"),
        ("G$2", "OUT2", "6"),
        ("G$2", "VP", "7"),
        ("G$3", "P", "8"),
    ]
    assert lint_library(build_library([part])) == []


def test_single_unit_gate_has_no_addlevel() -> None:
    gate = find_one(parse(build_library([make_resistor()])), ".//gate")
    assert gate.attrib == {"name": "G$1", "symbol": "0603WAF1002T5E", "x": "0", "y": "0"}


# ----- errors -----


def test_empty_part_list_is_rejected() -> None:
    with pytest.raises(ConversionError, match="No parts"):
        build_library([])


def test_pin_without_pad_is_rejected_and_named() -> None:
    symbol = Symbol("S", (make_pin("1"), make_pin("VBUS", "99")))
    with pytest.raises(ConversionError, match="VBUS"):
        build_library([replace(make_resistor(), symbols=(symbol,))])


def test_part_without_symbols_is_rejected() -> None:
    with pytest.raises(ConversionError, match="no symbols"):
        build_library([replace(make_resistor(), symbols=())])


def test_part_without_any_name_is_rejected() -> None:
    with pytest.raises(ConversionError):
        build_library([make_resistor(lcsc_id="", mpn="")])


def test_nonpositive_pad_size_is_rejected() -> None:
    with pytest.raises(ConversionError, match="size"):
        only_pad(make_pad("1", width=0.0), "smd")


def test_lint_errors_in_generated_output_raise_lint_error() -> None:
    package = Package("T", "", (make_pad("1", 0), make_pad("1", 1, number="2")))
    symbol = Symbol("S", (make_pin("A", "1"), make_pin("B", "2")))
    with pytest.raises(LintError, match="DUP_PAD"):
        build_library([replace(make_resistor(), symbols=(symbol,), package=package)])


# ----- number formatting -----


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0"),
        (0.0, "0"),
        (-0.0, "0"),
        (1.0, "1"),
        (-1.0, "-1"),
        (100.0, "100"),
        (10.0, "10"),
        (2.54, "2.54"),
        (0.1 + 0.2, "0.3"),
        (1e-9, "0"),
        (-1e-9, "0"),
        (1.0000004, "1"),
        (123456.789012345, "123456.789012"),
        (1e20, "100000000000000000000"),
        (-25.4 * 3, "-76.2"),
        (0.000001, "0.000001"),
        (math.pi, "3.141593"),
        (0.15, "0.15"),
    ],
)
def test_format_number(value: float, expected: str) -> None:
    result = format_number(value)
    assert result == expected
    assert "e" not in result.lower()
    assert result != "-0"


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_format_number_rejects_non_finite(value: float) -> None:
    with pytest.raises(ConversionError):
        format_number(value)


# ----- output files -----


def test_write_library(tmp_path: Path) -> None:
    description = "Resistors " + chr(0x3A9) + " and caps"
    target = tmp_path / "nested" / "out.lbr"
    result = write_library(target, [make_resistor()], description=description)
    assert result == target
    data = target.read_bytes()
    assert b"\r" not in data
    assert data.startswith(b'<?xml version="1.0" encoding="utf-8"?>\n')
    text = data.decode("utf-8")
    assert text == build_library([make_resistor()], description=description)
    assert chr(0x3A9) in text
    assert lint_library(text) == []


def test_write_library_accepts_str_path(tmp_path: Path) -> None:
    assert write_library(str(tmp_path / "x.lbr"), [make_resistor()]).exists()


def test_control_characters_are_stripped() -> None:
    part = replace(
        make_resistor(), description="a" + chr(0) + "b" + chr(11) + "c", manufacturer="m" + chr(1)
    )
    text = build_library([part])
    root = parse(text)  # must still be well formed
    assert "abc" in (find_one(root, ".//deviceset/description").text or "")


# ----- golden files -----


def check_golden(name: str, text: str) -> None:
    golden = GOLDEN_DIR / name
    if os.environ.get("UPDATE_GOLDEN") == "1":
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(text, encoding="utf-8", newline="\n")
    assert golden.exists(), f"missing golden file {golden}, run with UPDATE_GOLDEN=1"
    assert text == golden.read_text(encoding="utf-8")


def test_golden_resistor() -> None:
    check_golden(
        "writer_resistor.lbr", build_library([make_resistor()], description="Golden: resistor")
    )


def test_golden_ic_with_thermal_and_slot_pad() -> None:
    check_golden("writer_ic.lbr", build_library([make_ic()], description="Golden: IC"))


def test_golden_ic_with_milled_slot() -> None:
    text = build_library([make_ic()], description="Golden: IC", native_slots=False)
    check_golden("writer_ic_milling.lbr", text)


def _slot_part() -> Part:
    pad = make_pad(
        "1",
        10.0,
        5.0,
        shape=PadShape.OVAL,
        width=2.0,
        height=1.5,
        drill=0.7,
        slot_length=1.2,
        slot_angle=90.0,
    )
    package = Package("T", "", (pad,))
    return replace(make_resistor(), symbols=(Symbol("S", (make_pin("1"),)),), package=package)


def test_native_slot_pad_uses_fusion_slot_syntax_and_version_9_7() -> None:
    root = parse(build_library([_slot_part()]))
    assert root.get("version") == constants.EAGLE_VERSION_SLOTS
    pad = find_one(root, ".//package/pad")
    assert pad.get("shape") == "slot"
    assert pad.get("drill") == "0.7"
    assert pad.get("slotLength") == "1.2"
    assert pad.get("rot") == "R90"
    assert root.findall(".//package/wire") == []


def test_native_slot_output_lints_clean() -> None:
    issues = lint_library(build_library([_slot_part()]))
    assert [i for i in issues if i.severity != "info"] == []


def test_library_without_slots_keeps_the_verified_version() -> None:
    assert parse(build_library([make_resistor()])).get("version") == constants.EAGLE_VERSION
