"""Tests for lbr_writer.package_fingerprint / package_element_fingerprint."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

import pytest

from lcsc2fusion.constants import Layer
from lcsc2fusion.errors import ConversionError
from lcsc2fusion.lbr_writer import (
    build_library,
    package_element_fingerprint,
    package_fingerprint,
)
from lcsc2fusion.model import Label, Package, Pad, PadShape, Part, Rectangle, Wire
from lcsc2fusion.parser.part import parse_part

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"
FIXTURE_NAMES = sorted(
    p.name.removesuffix(".json")
    for p in SYNTH.glob("*.json")
    if not p.name.endswith(".expected.json")
)

# Pinned on purpose: the add-in stores fingerprints on disk, so changing how the writer formats a
# package invalidates every stored value. If this test fails, that is what changed.
PINNED_FINGERPRINT = "db7fa0010bcddfc07feac7f29fc354e38c9c1777167cd95c33fe2cf95cfd42a7"


def two_pad_package(name: str = "R0603", description: str = "chip resistor") -> Package:
    return Package(
        name=name,
        description=description,
        pads=(
            Pad("1", "1", -0.75, 0.0, 0.8, 0.95, PadShape.RECT),
            Pad("2", "2", 0.75, 0.0, 0.8, 0.95, PadShape.RECT),
        ),
        graphics=(
            Wire(-0.4, -0.45, 0.4, -0.45, 0.127, Layer.T_PLACE),
            Label(0.0, 1.2, "R", 0.8, Layer.T_NAMES),
        ),
    )


def load_part(name: str) -> Part:
    return parse_part(json.loads((SYNTH / f"{name}.json").read_text(encoding="utf-8")))


def package_elements(text: str) -> dict[str, ET.Element]:
    root = ET.fromstring(text)
    return {p.get("name", ""): p for p in root.iter("package")}


def test_fingerprint_is_a_sha256_hex_digest() -> None:
    value = package_fingerprint(two_pad_package())
    assert len(value) == 64
    assert value == value.lower()
    int(value, 16)


def test_fingerprint_is_stable_and_pinned() -> None:
    assert package_fingerprint(two_pad_package()) == package_fingerprint(two_pad_package())
    assert package_fingerprint(two_pad_package()) == PINNED_FINGERPRINT


def test_fingerprint_ignores_the_name() -> None:
    base = package_fingerprint(two_pad_package("R0603"))
    assert package_fingerprint(two_pad_package("SOMETHING_ELSE")) == base
    assert package_fingerprint(two_pad_package("")) == base


def test_fingerprint_ignores_the_description() -> None:
    base = package_fingerprint(two_pad_package(description="chip resistor"))
    assert package_fingerprint(two_pad_package(description="")) == base
    assert package_fingerprint(two_pad_package(description="other <b>text</b>")) == base


def pad_moved(package: Package) -> Package:
    first, second = package.pads
    return replace(package, pads=(replace(first, x=first.x - 0.01), second))


def pad_resized(package: Package) -> Package:
    first, second = package.pads
    return replace(package, pads=(first, replace(second, width=0.9)))


def pad_renamed(package: Package) -> Package:
    first, second = package.pads
    return replace(package, pads=(replace(first, name="A"), second))


def pads_swapped(package: Package) -> Package:
    return replace(package, pads=tuple(reversed(package.pads)))


def graphic_added(package: Package) -> Package:
    extra = Rectangle(-1.0, -1.0, 1.0, 1.0, Layer.T_DOCU)
    return replace(package, graphics=(*package.graphics, extra))


def label_changed(package: Package) -> Package:
    wire, label = package.graphics
    assert isinstance(label, Label)
    return replace(package, graphics=(wire, replace(label, text="X")))


def layer_changed(package: Package) -> Package:
    wire, label = package.graphics
    assert isinstance(wire, Wire)
    return replace(package, graphics=(replace(wire, layer=Layer.T_DOCU), label))


@pytest.mark.parametrize(
    "change",
    [
        pad_moved,
        pad_resized,
        pad_renamed,
        pads_swapped,
        graphic_added,
        label_changed,
        layer_changed,
    ],
    ids=lambda f: f.__name__,
)
def test_fingerprint_reacts_to_geometry(change: object) -> None:
    assert callable(change)
    base = two_pad_package()
    assert package_fingerprint(change(base)) != package_fingerprint(base)


def test_fingerprint_of_a_package_the_writer_rejects_raises() -> None:
    broken = Package("BAD", "", pads=(Pad("1", "1", 0.0, 0.0, 0.0, 0.5, PadShape.RECT),))
    with pytest.raises(ConversionError):
        package_fingerprint(broken)


def test_native_slots_flag_is_part_of_the_value() -> None:
    slotted = Package(
        "SLOT",
        "",
        pads=(
            Pad(
                "1",
                "1",
                0.0,
                0.0,
                2.0,
                1.2,
                PadShape.OVAL,
                drill=0.7,
                slot_length=1.6,
                slot_angle=90.0,
            ),
        ),
    )
    assert package_fingerprint(slotted) != package_fingerprint(slotted, native_slots=False)


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fingerprint_equals_the_one_read_back_from_a_written_library(name: str) -> None:
    """The add-in compares a model fingerprint with one computed from the file: they must agree."""
    part = load_part(name)
    elements = package_elements(build_library([part]))
    (written,) = elements.values()
    assert package_element_fingerprint(written) == package_fingerprint(part.package)


def test_read_back_value_ignores_layout_whitespace() -> None:
    compact = ET.fromstring(
        '<package name="A"><description>x</description>'
        '<smd name="1" x="0" y="0" dx="1" dy="1" layer="1" roundness="0"/></package>'
    )
    indented = ET.fromstring(
        '<package name="B">\n  <description>y</description>\n'
        '  <smd name="1" x="0" y="0" dx="1" dy="1" layer="1" roundness="0"/>\n</package>\n'
    )
    assert package_element_fingerprint(compact) == package_element_fingerprint(indented)


def test_read_back_value_keeps_text_content_and_ignores_attribute_order() -> None:
    one = ET.fromstring('<package name="A"><text x="0" y="1">R</text></package>')
    other_text = ET.fromstring('<package name="A"><text x="0" y="1">C</text></package>')
    reordered = ET.fromstring('<package name="Z"><text y="1" x="0">R</text></package>')
    assert package_element_fingerprint(one) != package_element_fingerprint(other_text)
    assert package_element_fingerprint(one) == package_element_fingerprint(reordered)


def test_same_geometry_under_two_names_shares_the_fingerprint_in_one_library() -> None:
    soic = load_part("soic8")
    twin = replace(
        soic,
        lcsc_id="C9100001",
        mpn="TWIN",
        package=replace(soic.package, name="OTHER-NAME"),
    )
    elements = package_elements(build_library([soic, twin]))
    assert len(elements) == 2
    prints = {package_element_fingerprint(e) for e in elements.values()}
    assert len(prints) == 1


def test_distinct_fixture_packages_have_distinct_fingerprints() -> None:
    by_name: dict[str, set[str]] = {}
    for name in FIXTURE_NAMES:
        package = load_part(name).package
        by_name.setdefault(package.name, set()).add(package_fingerprint(package))
    # the box3d fixtures deliberately share one footprint, every other name is its own shape
    assert all(len(values) == 1 for values in by_name.values())
    flat = [next(iter(values)) for values in by_name.values()]
    assert len(flat) == len(set(flat))
