"""Tests for the Fusion-compatibility linter (lcsc2fusion.lbr_writer.lint)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lcsc2fusion.lbr_writer.lint import LintIssue, format_issues, lint_library

VALID = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE eagle SYSTEM "eagle.dtd">
<eagle version="9.6.2">
<drawing>
<settings><setting alwaysvectorfont="no"/><setting verticaltext="up"/></settings>
<grid distance="0.1" unitdist="inch" unit="inch" style="lines" multiple="1" display="no" altdistance="0.01" altunitdist="inch" altunit="inch"/>
<layers>
<layer number="1" name="Top" color="4" fill="1" visible="yes" active="yes"/>
<layer number="16" name="Bottom" color="1" fill="1" visible="yes" active="yes"/>
<layer number="21" name="tPlace" color="7" fill="1" visible="yes" active="yes"/>
<layer number="94" name="Symbols" color="4" fill="1" visible="yes" active="yes"/>
</layers>
<library>
<description>test</description>
<packages>
<package name="P1">
<wire x1="0" y1="0" x2="1" y2="1" width="0.1" layer="21"/>
<circle x="0" y="0" radius="1" width="0.1" layer="21"/>
<rectangle x1="0" y1="0" x2="1" y2="1" layer="21" rot="R90"/>
<polygon width="0.1" layer="21"><vertex x="0" y="0"/><vertex x="1" y="0"/><vertex x="1" y="1"/></polygon>
<text x="0" y="0" size="1" layer="21" font="vector" align="center">&gt;NAME</text>
<smd name="1" x="0" y="0" dx="1" dy="1" layer="1" roundness="50" rot="SMR45.5"/>
<pad name="2" x="3" y="0" drill="0.8" diameter="1.6" shape="long" rot="R90"/>
<hole x="5" y="5" drill="1"/>
</package>
</packages>
<symbols>
<symbol name="S1">
<wire x1="-2.54" y1="-1" x2="2.54" y2="-1" width="0.254" layer="94"/>
<pin name="A" x="-5.08" y="0" visible="off" length="short" direction="pas" swaplevel="1"/>
<pin name="B" x="5.08" y="0" visible="off" length="short" direction="pas" function="dot" swaplevel="1" rot="R180"/>
</symbol>
</symbols>
<devicesets>
<deviceset name="D1" prefix="R" uservalue="yes">
<description>d</description>
<gates><gate name="G$1" symbol="S1" x="0" y="0"/></gates>
<devices>
<device name="" package="P1">
<connects><connect gate="G$1" pin="A" pad="1"/><connect gate="G$1" pin="B" pad="2"/></connects>
<technologies><technology name=""><attribute name="MPN" value="X" constant="no"/></technology></technologies>
</device>
</devices>
</deviceset>
</devicesets>
</library>
</drawing>
</eagle>
"""  # noqa: E501


def mutate(old: str, new: str, *, text: str = VALID, count: int = 1) -> str:
    assert old in text, f"test bug: {old!r} not in the base library"
    return text.replace(old, new, count)


def issues_with(text: str, code: str) -> list[LintIssue]:
    return [i for i in lint_library(text) if i.code == code]


def assert_error(text: str, code: str) -> None:
    found = issues_with(text, code)
    assert found, f"expected {code}, got: {format_issues(lint_library(text))}"
    assert all(i.severity == "error" for i in found)


def assert_warning(text: str, code: str) -> None:
    found = issues_with(text, code)
    assert found, f"expected {code}, got: {format_issues(lint_library(text))}"
    assert all(i.severity == "warning" for i in found)


# ----- positive -----


def test_minimal_valid_library_has_no_issues() -> None:
    assert lint_library(VALID) == []


def test_bom_is_tolerated() -> None:
    assert lint_library(chr(0xFEFF) + VALID) == []


def test_foreign_attributes_and_elements_are_tolerated() -> None:
    """Files saved by newer Fusion builds carry extras the DTD does not know."""
    text = mutate(
        '<layer number="1" name="Top" color="4" fill="1" visible="yes" active="yes"/>',
        '<layer number="1" name="Top" color="4" fill="1" visible="yes" active="yes"'
        ' blackcolor="0" whitecolor="0"/>',
    )
    text = mutate("</library>", "</library><schematic/>", text=text)
    text = mutate(
        "</eagle>",
        '<compatibility><note version="8.2" severity="warning">x</note></compatibility></eagle>',
        text=text,
    )
    assert lint_library(text) == []


def test_packages3d_between_packages_and_symbols_is_valid() -> None:
    assert lint_library(mutate("<symbols>", "<packages3d/><symbols>")) == []


def test_symbol_only_deviceset_without_package_is_valid() -> None:
    """Power symbols have a device without package and without connects."""
    text = mutate('<device name="" package="P1">', '<device name="">')
    text = mutate(
        '<connects><connect gate="G$1" pin="A" pad="1"/><connect gate="G$1" pin="B" pad="2"/>'
        "</connects>",
        "",
        text=text,
    )
    assert lint_library(text) == []


def test_pad_list_in_connect_is_valid() -> None:
    extra = '<pad name="1_2" x="9" y="9" drill="1" shape="round"/>'
    text = mutate('<pad name="2" ', extra + '<pad name="2" ')
    text = mutate('pin="A" pad="1"', 'pin="A" pad="1 1_2"', text=text)
    assert lint_library(text) == []


# ----- structure -----


def test_malformed_xml() -> None:
    assert_error(VALID[: len(VALID) // 2], "XML_MALFORMED")
    assert_error("not xml at all", "XML_MALFORMED")
    assert_error("", "XML_MALFORMED")


def test_wrong_root() -> None:
    assert_error("<library/>", "STRUCT_ROOT")


def test_missing_drawing() -> None:
    assert_error("<eagle/>", "STRUCT_NO_DRAWING")


def test_missing_layers() -> None:
    text = mutate("<layers>", "<nolayers>")
    text = mutate("</layers>", "</nolayers>", text=text)
    assert_error(text, "STRUCT_NO_LAYERS")


def test_missing_library() -> None:
    assert_error("<eagle><drawing><layers/></drawing></eagle>", "STRUCT_NO_LIBRARY")


def test_library_child_order() -> None:
    text = mutate("<packages>", "<symbols/><packages>")
    assert_error(text, "STRUCT_ORDER")
    text = mutate("</packages>", "</packages><packages/>")
    assert_error(text, "STRUCT_ORDER")
    text = mutate("<description>test</description>", "")
    text = mutate("</devicesets>", "</devicesets><description>late</description>", text=text)
    assert_error(text, "STRUCT_ORDER")


def test_unknown_library_child_is_only_a_warning() -> None:
    assert_warning(mutate("</library>", "<extras/></library>"), "STRUCT_UNKNOWN")


def test_fusion_style_layer_attributes_are_valid() -> None:
    fusion_layer = (
        '<layer number="1" name="Top" blackcolor="0xccc90d0c" whitecolor="0xccc90d0c" '
        'coloredcolor="0xccc90d0c" blackhighlight="0xccc90000" whitehighlight="0xccc90000" '
        'coloredhighlight="0xccc90000" fill="1" visible="yes" active="yes"/>'
    )
    text = mutate(
        '<layer number="1" name="Top" color="4" fill="1" visible="yes" active="yes"/>',
        fusion_layer,
    )
    assert lint_library(text) == []


def test_legacy_layer_without_color_is_an_error() -> None:
    text = mutate('<layer number="1" name="Top" color="4" fill="1"', '<layer number="1" name="Top"')
    assert_error(text, "MISSING_ATTR")


def test_fusion_atts_section_between_symbols_and_devicesets_is_valid() -> None:
    assert lint_library(mutate("<devicesets>", "<atts/><devicesets>")) == []


def test_slot_pad_is_valid_in_format_9_7() -> None:
    text = mutate(
        'shape="long" rot="R90"',
        'shape="slot" slotLength="2.5" rot="R90"',
    ).replace('<eagle version="9.6.2">', '<eagle version="9.7.0">')
    assert issues_with(text, "ENUM_PAD_SHAPE") == []
    assert issues_with(text, "SLOT_VERSION") == []


def test_slot_pad_in_an_older_format_is_a_warning() -> None:
    text = mutate(
        'shape="long" rot="R90"',
        'shape="slot" slotLength="2.5" rot="R90"',
    )
    assert_warning(text, "SLOT_VERSION")


def test_slot_pad_without_slot_length_is_an_error() -> None:
    text = mutate(
        'shape="long" rot="R90"',
        'shape="slot" rot="R90"',
    ).replace('<eagle version="9.6.2">', '<eagle version="9.7.0">')
    assert_error(text, "MISSING_ATTR")


# ----- duplicates -----


def test_duplicate_package_names_case_insensitive() -> None:
    text = mutate(
        "</packages>",
        '<package name="p1"><smd name="1" x="0" y="0" dx="1" dy="1" layer="1"/></package>'
        "</packages>",
    )
    assert_error(text, "DUP_PACKAGE")


def test_duplicate_symbol_names() -> None:
    text = mutate("</symbols>", '<symbol name="s1"/></symbols>')
    assert_error(text, "DUP_SYMBOL")


def test_duplicate_deviceset_names() -> None:
    dup = (
        '<deviceset name="d1"><gates><gate name="G$1" symbol="S1" x="0" y="0"/></gates>'
        '<devices><device name=""/></devices></deviceset>'
    )
    assert_error(mutate("</devicesets>", dup + "</devicesets>"), "DUP_DEVICESET")


def test_duplicate_device_names() -> None:
    text = mutate("</devices>", '<device name=""/></devices>')
    assert_error(text, "DUP_DEVICE")


def test_duplicate_pad_names_across_pad_and_smd() -> None:
    assert_error(mutate('<pad name="2"', '<pad name="1"'), "DUP_PAD")


def test_duplicate_pad_names_case_insensitive() -> None:
    text = mutate('<smd name="1"', '<smd name="a"')
    text = mutate('<pad name="2"', '<pad name="A"', text=text)
    assert_error(text, "DUP_PAD")


def test_duplicate_pin_names() -> None:
    assert_error(mutate('<pin name="B"', '<pin name="a"'), "DUP_PIN")


def test_duplicate_gate_names() -> None:
    text = mutate("</gates>", '<gate name="g$1" symbol="S1" x="0" y="25"/></gates>')
    assert_error(text, "DUP_GATE")


def test_duplicate_layer_numbers() -> None:
    text = mutate("</layers>", '<layer number="21" name="again" color="7" fill="1"/></layers>')
    assert_error(text, "DUP_LAYER")


# ----- names -----


@pytest.mark.parametrize("bad", ["1 A", "1;A", "A\tB"])
def test_pad_names_with_whitespace_or_semicolon(bad: str) -> None:
    assert_error(mutate('<pad name="2"', f'<pad name="{bad}"'), "BAD_NAME")


@pytest.mark.parametrize("bad", ["A B", "A;B"])
def test_pin_names_with_whitespace_or_semicolon(bad: str) -> None:
    assert_error(mutate('<pin name="A"', f'<pin name="{bad}"'), "BAD_NAME")


def test_empty_names() -> None:
    assert_error(mutate('<package name="P1">', '<package name="">'), "EMPTY_NAME")
    assert_error(mutate('<pin name="A"', '<pin name=""'), "EMPTY_NAME")


# ----- references -----


def test_connect_unknown_gate() -> None:
    assert_error(mutate('gate="G$1" pin="A"', 'gate="G$9" pin="A"'), "CONNECT_GATE")


def test_connect_unknown_pin() -> None:
    assert_error(mutate('pin="A" pad="1"', 'pin="Z" pad="1"'), "CONNECT_PIN")


def test_connect_unknown_pad() -> None:
    assert_error(mutate('pin="A" pad="1"', 'pin="A" pad="7"'), "CONNECT_PAD")


def test_connect_pad_list_with_one_unknown_pad() -> None:
    assert_error(mutate('pin="A" pad="1"', 'pin="A" pad="1 7"'), "CONNECT_PAD")


def test_connect_empty_pad_list() -> None:
    assert_error(mutate('pin="A" pad="1"', 'pin="A" pad=""'), "CONNECT_PAD")


def test_connect_pad_is_case_sensitive_reference() -> None:
    assert_error(mutate('pin="A" pad="1"', 'pin="A" pad="X"'), "CONNECT_PAD")


def test_connect_with_pad_but_device_without_package() -> None:
    assert_error(mutate('<device name="" package="P1">', '<device name="">'), "CONNECT_PAD")


def test_device_unknown_package() -> None:
    assert_error(mutate('package="P1"', 'package="NOPE"'), "DEVICE_PACKAGE_MISSING")


def test_gate_unknown_symbol() -> None:
    assert_error(mutate('symbol="S1"', 'symbol="NOPE"'), "GATE_SYMBOL_MISSING")


# ----- enums -----


@pytest.mark.parametrize(
    ("old", "new", "code"),
    [
        ('direction="pas"', 'direction="bogus"', "ENUM_PIN_DIRECTION"),
        ('length="short"', 'length="huge"', "ENUM_PIN_LENGTH"),
        ('visible="off"', 'visible="maybe"', "ENUM_PIN_VISIBLE"),
        ('function="dot"', 'function="zigzag"', "ENUM_PIN_FUNCTION"),
        ('shape="long"', 'shape="hexagon"', "ENUM_PAD_SHAPE"),
        ('align="center"', 'align="middle"', "ENUM_TEXT_ALIGN"),
    ],
)
def test_enum_values(old: str, new: str, code: str) -> None:
    assert_error(mutate(old, new), code)


def test_all_valid_enum_values_pass() -> None:
    for direction in ("nc", "in", "out", "io", "oc", "pwr", "pas", "hiz", "sup"):
        assert lint_library(mutate('direction="pas"', f'direction="{direction}"')) == []
    for shape in ("square", "round", "octagon", "long", "offset"):
        assert lint_library(mutate('shape="long"', f'shape="{shape}"')) == []
    for function in ("dot", "clk", "dotclk"):
        assert lint_library(mutate('function="dot"', f'function="{function}"')) == []


def test_pin_function_none_is_an_error() -> None:
    text = mutate('function="dot"', 'function="none"')
    assert_error(text, "PIN_FUNCTION_NONE")
    # none is allowed by the DTD, so it must not also be reported as a bad enum
    assert issues_with(text, "ENUM_PIN_FUNCTION") == []


# ----- rotation -----


@pytest.mark.parametrize("bad", ["X90", "90", "R", "R-90", "R90deg", "RR90", "SSSR0", "r90"])
def test_bad_rot_strings(bad: str) -> None:
    assert_error(mutate('rot="R90"/>\n<hole', f'rot="{bad}"/>\n<hole'), "BAD_ROT")


@pytest.mark.parametrize("good", ["R0", "R90", "R45.5", "MR90", "SR270", "SMR12.25"])
def test_good_rot_strings(good: str) -> None:
    assert lint_library(mutate('rot="R90"/>\n<hole', f'rot="{good}"/>\n<hole')) == []


@pytest.mark.parametrize("bad", ["R45", "R360", "MR90", "SR0", "R90.5"])
def test_pin_rot_must_be_a_right_angle(bad: str) -> None:
    assert_error(mutate('rot="R180"', f'rot="{bad}"'), "PIN_ROT")


@pytest.mark.parametrize("good", ["R0", "R90", "R180", "R270", "R90.0"])
def test_pin_rot_right_angles_pass(good: str) -> None:
    assert lint_library(mutate('rot="R180"', f'rot="{good}"')) == []


# ----- geometry -----


def test_polygon_with_fewer_than_three_vertices() -> None:
    text = mutate('<vertex x="1" y="1"/>', "")
    assert_error(text, "POLYGON_VERTICES")


def test_polygon_zero_width_is_a_warning() -> None:
    text = mutate('<polygon width="0.1"', '<polygon width="0"')
    assert_warning(text, "POLYGON_ZERO_WIDTH")
    assert [i for i in lint_library(text) if i.severity == "error"] == []


def test_negative_widths() -> None:
    assert_error(mutate('<polygon width="0.1"', '<polygon width="-0.1"'), "NEGATIVE_WIDTH")
    assert_error(mutate('x2="1" y2="1" width="0.1"', 'x2="1" y2="1" width="-1"'), "NEGATIVE_WIDTH")
    assert_error(mutate('radius="1" width="0.1"', 'radius="1" width="-1"'), "NEGATIVE_WIDTH")


def test_circle_with_zero_width_is_valid() -> None:
    assert lint_library(mutate('radius="1" width="0.1"', 'radius="1" width="0"')) == []


def test_non_numeric_values() -> None:
    assert_error(mutate('dx="1" dy="1"', 'dx="wide" dy="1"'), "BAD_NUMBER")
    assert_error(mutate('x="3" y="0" drill', 'x="nan" y="0" drill'), "BAD_NUMBER")
    assert_error(mutate('<layer number="21"', '<layer number="twenty"'), "BAD_NUMBER")


def test_missing_required_attributes() -> None:
    assert_error(
        mutate(
            '<smd name="1" x="0" y="0" dx="1" dy="1" layer="1"',
            '<smd name="1" x="0" y="0" dx="1" layer="1"',
        ),
        "MISSING_ATTR",
    )
    assert_error(mutate('<hole x="5" y="5" drill="1"/>', '<hole x="5" y="5"/>'), "MISSING_ATTR")
    assert_error(
        mutate('<connect gate="G$1" pin="A" pad="1"/>', '<connect gate="G$1" pin="A"/>'),
        "MISSING_ATTR",
    )


# ----- layers -----


@pytest.mark.parametrize(
    "old",
    [
        '<wire x1="0" y1="0" x2="1" y2="1" width="0.1" layer="21"',
        '<circle x="0" y="0" radius="1" width="0.1" layer="21"',
        '<rectangle x1="0" y1="0" x2="1" y2="1" layer="21"',
        '<polygon width="0.1" layer="21"',
        '<text x="0" y="0" size="1" layer="21"',
        '<smd name="1" x="0" y="0" dx="1" dy="1" layer="1"',
    ],
)
def test_undeclared_layer_in_use(old: str) -> None:
    new = old.rsplit('layer="', 1)[0] + 'layer="99"'
    assert_error(mutate(old, new), "LAYER_UNDECLARED")


def test_layer_used_in_symbol_must_be_declared() -> None:
    assert_error(mutate('width="0.254" layer="94"', 'width="0.254" layer="95"'), "LAYER_UNDECLARED")


def test_unused_layers_need_not_be_declared() -> None:
    """A tolerant lint: the set of declared layers is the file's own business."""
    assert (
        lint_library(
            mutate(
                '<layer number="16" name="Bottom" color="1" fill="1" visible="yes" active="yes"/>',
                "",
            )
        )
        == []
    )


# ----- device sets -----


def test_deviceset_without_gates() -> None:
    text = mutate('<gates><gate name="G$1" symbol="S1" x="0" y="0"/></gates>', "")
    assert_error(text, "DEVICESET_NO_GATES")


def test_deviceset_without_devices() -> None:
    start = VALID.index("<devices>")
    end = VALID.index("</devices>") + len("</devices>")
    assert_error(VALID[:start] + VALID[end:], "DEVICESET_NO_DEVICES")


def test_deviceset_with_empty_devices_element() -> None:
    start = VALID.index("<devices>")
    end = VALID.index("</devices>") + len("</devices>")
    assert_error(VALID[:start] + "<devices/>" + VALID[end:], "DEVICESET_NO_DEVICES")


def test_device_without_connected_pads_is_a_warning() -> None:
    text = mutate(
        '<connects><connect gate="G$1" pin="A" pad="1"/>'
        '<connect gate="G$1" pin="B" pad="2"/></connects>',
        "<connects/>",
    )
    assert_warning(text, "DEVICE_NO_CONNECTS")


def test_package_without_pads_is_a_warning() -> None:
    start = VALID.index('<smd name="1"')
    end = VALID.index('<hole x="5"')
    text = VALID[:start] + VALID[end:]
    assert_warning(text, "PACKAGE_NO_PADS")


# ----- reporting -----


def test_issues_are_sorted_errors_first_and_stable() -> None:
    text = mutate('<polygon width="0.1"', '<polygon width="0"')
    text = mutate('gate="G$1" pin="A"', 'gate="G$9" pin="A"', text=text)
    text = mutate("</packages>", '<package name="EMPTY"/></packages>', text=text)
    issues = lint_library(text)
    severities = [i.severity for i in issues]
    assert severities == sorted(severities, key=lambda s: 0 if s == "error" else 1)
    assert "warning" in severities
    assert "error" in severities
    assert issues == lint_library(text)
    errors = [i for i in issues if i.severity == "error"]
    assert errors == sorted(errors, key=lambda i: (i.code, i.message))


def test_issue_is_frozen_and_printable() -> None:
    issue = LintIssue("error", "X", "boom")
    assert str(issue) == "ERROR [X] boom"
    with pytest.raises(AttributeError):
        issue.code = "Y"  # type: ignore[misc]
    assert (
        format_issues([issue, LintIssue("warning", "Z", "hmm")])
        == "ERROR [X] boom\nWARNING [Z] hmm"
    )


def test_gate0_sample_lints_clean() -> None:
    sample = Path(__file__).resolve().parent.parent / "docs" / "gate0_resistor.lbr"
    if not sample.exists():
        pytest.skip("docs/gate0_resistor.lbr has not been generated")
    assert lint_library(sample.read_text(encoding="utf-8")) == []
