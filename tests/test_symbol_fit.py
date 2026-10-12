"""Tests for the symbol body auto fit (parser/symbol_fit.py)."""

from __future__ import annotations

import copy
import json
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from lcsc2fusion.constants import GRID_MM, SYMBOL_WIRE_WIDTH_MM, Layer
from lcsc2fusion.model import (
    Circle,
    Graphic,
    Label,
    Pin,
    PinDirection,
    PinLength,
    Symbol,
    TextAlign,
    Wire,
)
from lcsc2fusion.parser.part import parse_part
from lcsc2fusion.parser.symbol import build_name_value_labels
from lcsc2fusion.parser.symbol_fit import (
    _MAX_PINS_WITHOUT_NAMES,
    PIN_NAME_CAP_HEIGHT_MM,
    PIN_NAME_CHAR_ADVANCE_MM,
    PIN_NAME_GAP_MM,
    PIN_NAME_INSET_MM,
    _find_body,
    _split_sides,
    display_name,
    fit_symbol,
    name_length_mm,
)

FIXTURES = Path(__file__).parent / "fixtures"
SYNTH_FILES = sorted(
    p for p in (FIXTURES / "synthetic").glob("*.json") if not p.name.endswith(".expected.json")
)
REAL_DIR = FIXTURES / "real"
REAL_FILES = sorted(REAL_DIR.glob("*.json")) if REAL_DIR.is_dir() else []
TOL = 1e-6

LEFT, BOTTOM, RIGHT, TOP = 0, 90, 180, 270  # pin angles (direction of the pin line)


# ---------------------------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------------------------


def pin(name: str, x: float, y: float, angle: int, number: str | None = None) -> Pin:
    return Pin(
        name=name,
        number=number or name,
        x=x,
        y=y,
        length=PinLength.SHORT,
        angle=angle,
        direction=PinDirection.IO,
    )


def rect(left: float, bottom: float, right: float, top: float) -> list[Graphic]:
    w, layer = SYMBOL_WIRE_WIDTH_MM, Layer.SYMBOLS
    return [
        Wire(left, top, right, top, w, layer),
        Wire(right, top, right, bottom, w, layer),
        Wire(right, bottom, left, bottom, w, layer),
        Wire(left, bottom, left, top, w, layer),
    ]


def make_symbol(
    box: tuple[float, float, float, float],
    pins: Sequence[Pin],
    extra: Sequence[Graphic] = (),
) -> Symbol:
    """Rectangular symbol (left, bottom, right, top) with the parser's label placement."""
    left, bottom, right, top = box
    body = [*rect(left, bottom, right, top), *extra]
    return Symbol("T", tuple(pins), (*body, *build_name_value_labels(body, tuple(pins))))


def box_of(symbol: Symbol) -> tuple[float, float, float, float]:
    body = _find_body(symbol)
    assert body is not None
    return body.left, body.bottom, body.right, body.top


def only_note(notes: list[str]) -> str:
    assert len(notes) == 1
    return notes[0]


# Pin name model, spelled out from the calibration constants (not from the implementation).


def text(chars: int) -> float:
    """Drawn length of a name with ``chars`` displayed characters."""
    return chars * PIN_NAME_CHAR_ADVANCE_MM


def pair_need(chars_a: int, chars_b: int) -> float:
    """Body size needed by two names that start at opposite edges on the same row or column."""
    return text(chars_a) + text(chars_b) + 2.0 * PIN_NAME_INSET_MM + PIN_NAME_GAP_MM


def grid_up(deficit: float) -> float:
    """Whole grid steps (in mm) that cover ``deficit``, 0 when nothing is missing."""
    return max(0.0, math.ceil(deficit / GRID_MM - 1e-9)) * GRID_MM


# ---------------------------------------------------------------------------------------------
# Name text model
# ---------------------------------------------------------------------------------------------


def test_displayed_name_drops_duplicate_suffix_and_overbar_marks() -> None:
    assert display_name("GND@1") == "GND"
    assert display_name("!RESET") == "RESET"
    assert display_name("A!B!C@12") == "ABC"
    assert display_name("IO4") == "IO4"


def test_name_length_counts_displayed_characters() -> None:
    assert name_length_mm("GND@1") == pytest.approx(text(3))
    assert name_length_mm("!RESET") == pytest.approx(text(5))
    assert name_length_mm("GND@1") == name_length_mm("GND@2") == name_length_mm("GND")


def test_calibration_is_at_least_the_measured_values() -> None:
    assert PIN_NAME_CHAR_ADVANCE_MM >= 1.23
    assert PIN_NAME_CAP_HEIGHT_MM >= 1.3
    assert PIN_NAME_INSET_MM > 0.0
    assert PIN_NAME_GAP_MM > 0.0


# ---------------------------------------------------------------------------------------------
# Width
# ---------------------------------------------------------------------------------------------


def test_left_and_right_names_make_the_body_grow_symmetrically_in_grid_steps() -> None:
    sym = make_symbol(
        (-2.54, -3.81, 2.54, 3.81),
        [
            pin("ENABLE", -5.08, 1.27, LEFT),
            pin("OUTPUT", 5.08, 1.27, RIGHT),
            pin("GND", -5.08, -1.27, LEFT),
        ],
    )
    fitted, notes = fit_symbol(sym)
    growth = grid_up((pair_need(6, 6) - 5.08) / 2.0)  # per edge
    assert growth > 0.0
    assert box_of(fitted) == pytest.approx((-2.54 - growth, -3.81, 2.54 + growth, 3.81))
    enable, output, gnd = fitted.pins
    assert enable.x == pytest.approx(-5.08 - growth)
    assert gnd.x == pytest.approx(-5.08 - growth)
    assert output.x == pytest.approx(5.08 + growth)
    assert [p.y for p in fitted.pins] == [1.27, 1.27, -1.27]
    # Pins still touch the body: same distance from their edge as before.
    assert enable.x - box_of(fitted)[0] == pytest.approx(sym.pins[0].x - box_of(sym)[0])
    assert output.x - box_of(fitted)[2] == pytest.approx(sym.pins[1].x - box_of(sym)[2])
    assert (
        only_note(notes)
        == f"Symbol T: body enlarged by {2 * growth:.2f} x 0.00 mm to fit pin names"
    )


def test_one_sided_pins_still_grow_both_edges() -> None:
    sym = make_symbol(
        (-2.54, -2.54, 2.54, 2.54),
        [
            pin("SDA_LONG", -5.08, 0.0, LEFT),
            pin("SCL_LONG", -5.08, -2.54, LEFT),
            pin("X", -5.08, 2.54, LEFT),
        ],
    )
    fitted, notes = fit_symbol(sym)
    # The longest name starts INSET from its edge and has to end inside the body.
    growth = grid_up((PIN_NAME_INSET_MM + text(8) - 5.08) / 2.0)
    assert growth > 0.0
    assert box_of(fitted) == pytest.approx((-2.54 - growth, -2.54, 2.54 + growth, 2.54))
    assert [p.x for p in fitted.pins] == pytest.approx([-5.08 - growth] * 3)
    assert f"{2 * growth:.2f} x 0.00" in only_note(notes)


def test_names_on_different_rows_do_not_need_a_wider_body() -> None:
    # Long names on opposite sides but on rows far apart cannot meet.
    sym = make_symbol(
        (-5.08, -5.08, 5.08, 5.08),
        [
            pin("ABCDE", -7.62, 2.54, LEFT),
            pin("ABCDE", 7.62, -2.54, RIGHT),
            pin("X", -7.62, 0.0, LEFT),
        ],
    )
    assert PIN_NAME_INSET_MM + text(5) <= 10.16  # each name fits alone
    assert pair_need(5, 5) > 10.16  # but would not fit on the same row
    assert fit_symbol(sym)[0] is sym


def test_duplicate_suffix_and_overbar_do_not_count_as_characters() -> None:
    def grown(left_name: str, right_name: str) -> tuple[float, float, float, float]:
        sym = make_symbol(
            (-2.54, -2.54, 2.54, 2.54),
            [
                pin(left_name, -5.08, 0.0, LEFT),
                pin(right_name, 5.08, 0.0, RIGHT),
                pin("N", -5.08, -2.54, LEFT),
            ],
        )
        return box_of(fit_symbol(sym)[0])

    assert grown("GND@1", "GND@2") == pytest.approx(grown("GND", "GND"))
    assert grown("!RESET", "!RESET") == pytest.approx(grown("RESET", "RESET"))
    assert grown("!RESET", "!RESET") != pytest.approx(grown("!RESET1", "!RESET1"))


def test_gap_and_insets_between_facing_names_are_counted() -> None:
    # The text of "ABC" and "ABCD" plus the insets would fit, the gap between them makes the
    # difference: a body narrower than the full need by half a gap must grow, a wider one not.
    need = pair_need(3, 4)
    assert need - PIN_NAME_GAP_MM < need - PIN_NAME_GAP_MM / 2 < need

    def build(width: float) -> Symbol:
        half = width / 2.0
        return make_symbol(
            (-half, -2.54, half, 2.54),
            [
                pin("ABC", -half - 2.54, 0.0, LEFT),
                pin("ABCD", half + 2.54, 0.0, RIGHT),
                pin("N", -half - 2.54, -2.54, LEFT),
            ],
        )

    tight_half = (need - PIN_NAME_GAP_MM / 2) / 2.0
    fitted, _ = fit_symbol(build(2 * tight_half))
    assert box_of(fitted) == pytest.approx(
        (-tight_half - GRID_MM, -2.54, tight_half + GRID_MM, 2.54)
    )
    roomy = build(need + 0.05)
    assert fit_symbol(roomy)[0] is roomy


def test_already_fitting_symbol_is_returned_unchanged() -> None:
    half = pair_need(1, 1) / 2.0 + 2.0
    sym = make_symbol(
        (-half, -5.08, half, 5.08),
        [
            pin("A", -half - 2.54, 0.0, LEFT),
            pin("B", half + 2.54, 0.0, RIGHT),
            pin("N", -half - 2.54, -2.54, LEFT),
        ],
    )
    fitted, notes = fit_symbol(sym)
    assert fitted is sym
    assert notes == []


def test_exact_fit_does_not_grow_because_of_float_noise() -> None:
    half = pair_need(1, 1) / 2.0
    sym = make_symbol(
        (-half, -2.54, half, 2.54),
        [
            pin("A", -half - 2.54, 0.0, LEFT),
            pin("B", half + 2.54, 0.0, RIGHT),
            pin("N", -half - 2.54, -2.54, LEFT),
        ],
    )
    assert fit_symbol(sym)[0] is sym


def test_symbols_with_two_pins_or_less_are_never_touched() -> None:
    """The writer hides the pin names of such symbols (visible="off"): nothing to fit."""
    pins = [pin("VERY_LONG_NAME", -5.08, 0.0, LEFT), pin("VERY_LONG_NAME2", 5.08, 0.0, RIGHT)]
    sym = make_symbol((-2.54, -2.54, 2.54, 2.54), pins)
    assert fit_symbol(sym) == (sym, [])
    assert fit_symbol(make_symbol((-2.54, -2.54, 2.54, 2.54), pins[:1]))[1] == []
    three = make_symbol((-2.54, -2.54, 2.54, 2.54), [*pins, pin("N", -5.08, -2.54, LEFT)])
    assert fit_symbol(three)[0] != three  # a third pin makes the very same body grow


def test_synthetic_soic8_from_the_user_screenshot_grows() -> None:
    """VDD and NC overlapped in Fusion on this 10.16 mm body; the calibration must catch it."""
    doc = json.loads((FIXTURES / "synthetic" / "soic8.json").read_text(encoding="utf-8"))
    plain = parse_part(doc, fit_symbols=False).symbols[0]
    fitted = parse_part(doc).symbols[0]
    assert box_of(plain)[2] - box_of(plain)[0] == pytest.approx(10.16)
    need = pair_need(3, 2)  # VDD and NC share the top row
    assert need > 10.16
    growth = grid_up((need - 10.16) / 2.0)
    assert box_of(fitted)[2] - box_of(fitted)[0] == pytest.approx(10.16 + 2 * growth)


# ---------------------------------------------------------------------------------------------
# Height (vertical names against side rows)
# ---------------------------------------------------------------------------------------------


def test_top_and_bottom_names_clear_the_side_rows_they_cross() -> None:
    sym = make_symbol(
        (-7.62, -5.08, 7.62, 5.08),
        [
            pin("IN", -10.16, 2.54, LEFT),
            pin("IN2", -10.16, -2.54, LEFT),
            pin("VCC", -5.08, 7.62, TOP),
            pin("GND", -5.08, -7.62, BOTTOM),
        ],
    )
    fitted, notes = fit_symbol(sym)
    left, bottom, right, top = box_of(fitted)
    assert (left, right) == pytest.approx((-7.62, 7.62))
    # VCC runs down from the top edge and has to end above the cap height of the row at y = 2.54
    # (IN), GND likewise below the row at y = -2.54 (IN2). The two names also have to fit into the
    # body against each other (same column).
    row_need = 2.54 + text(3) + PIN_NAME_INSET_MM + PIN_NAME_CAP_HEIGHT_MM / 2 + PIN_NAME_GAP_MM
    floor_need = (pair_need(3, 3) - 10.16) / 2.0
    growth = max(grid_up(row_need - 5.08), grid_up(floor_need))
    assert growth > 0.0
    assert (top, bottom) == pytest.approx((5.08 + growth, -5.08 - growth))
    top_pin, bottom_pin = fitted.pins[2], fitted.pins[3]
    assert (top_pin.x, top_pin.y) == pytest.approx((-5.08, 7.62 + growth))
    assert (bottom_pin.x, bottom_pin.y) == pytest.approx((-5.08, -7.62 - growth))
    # The vertical name ends above the cap height of the row it crosses, with the gap.
    text_end = top - PIN_NAME_INSET_MM - name_length_mm("VCC")
    assert text_end >= 2.54 + PIN_NAME_CAP_HEIGHT_MM / 2 + PIN_NAME_GAP_MM - TOL
    assert f"0.00 x {2 * growth:.2f}" in only_note(notes)
    # Side pins did not move.
    assert fitted.pins[0] == sym.pins[0]
    assert fitted.pins[1] == sym.pins[1]


def test_top_and_bottom_grow_independently() -> None:
    sym = make_symbol(
        (-7.62, -5.08, 7.62, 5.08),
        [
            pin("IN", -10.16, 2.54, LEFT),
            pin("OUT", -10.16, -2.54, LEFT),
            pin("VCC", -5.08, 7.62, TOP),
        ],
    )
    fitted, _ = fit_symbol(sym)
    _, bottom, _, top = box_of(fitted)
    row_need = 2.54 + text(3) + PIN_NAME_INSET_MM + PIN_NAME_CAP_HEIGHT_MM / 2 + PIN_NAME_GAP_MM
    assert top == pytest.approx(5.08 + grid_up(row_need - 5.08))
    assert top > 5.08
    assert bottom == pytest.approx(-5.08)  # no bottom pins, the bottom edge stays


def test_vertical_name_far_from_side_names_does_not_grow_the_body() -> None:
    # The column of GND is far away from the short side names, so nothing crosses it.
    sym = make_symbol(
        (-8.89, -7.62, 8.89, 5.08),
        [
            pin("SW", -11.43, 2.54, LEFT),
            pin("BST", 11.43, 2.54, RIGHT),
            pin("GND", 0.0, 7.62, TOP),
        ],
    )
    fitted, notes = fit_symbol(sym)
    assert fitted is sym
    assert notes == []


def test_columns_and_edge_positions_of_vertical_pins_are_kept() -> None:
    sym = make_symbol(
        (-7.62, -5.08, 7.62, 5.08),
        [
            pin("LONG_LEFT_NAME", -10.16, 0.0, LEFT),
            pin("A", -3.81, 7.62, TOP),
            pin("B", 1.27, 7.62, TOP),
            pin("C", 3.81, -7.62, BOTTOM),
        ],
    )
    fitted, _ = fit_symbol(sym)
    assert fitted != sym
    for before, after in zip(sym.pins, fitted.pins, strict=True):
        if before.angle in (TOP, BOTTOM):
            assert after.x == before.x
    left, bottom, right, top = box_of(fitted)
    for p in fitted.pins:
        if p.angle == TOP:
            assert p.y - top == pytest.approx(7.62 - 5.08)
            assert left < p.x < right
        elif p.angle == BOTTOM:
            assert bottom - p.y == pytest.approx(7.62 - 5.08)
            assert left < p.x < right


def test_vertical_names_in_the_same_column_must_fit_against_each_other() -> None:
    sym = make_symbol(
        (-5.08, -2.54, 5.08, 2.54),
        [
            pin("VERYLONGTOP", -2.54, 5.08, TOP),
            pin("VERYLONGBOT", -2.54, -5.08, BOTTOM),
            pin("MID", 2.54, 5.08, TOP),
        ],
    )
    fitted, _ = fit_symbol(sym)
    _, bottom, _, top = box_of(fitted)
    growth = grid_up((pair_need(11, 11) - 5.08) / 2.0)
    assert growth > 0.0
    assert (top, bottom) == pytest.approx((2.54 + growth, -2.54 - growth))  # symmetric growth
    assert top - bottom >= pair_need(11, 11) - TOL
    assert top - bottom < pair_need(11, 11) + 2 * GRID_MM  # whole grid steps, not more than needed


def test_vertical_names_in_different_columns_only_have_to_fit_alone() -> None:
    sym = make_symbol(
        (-5.08, -2.54, 5.08, 2.54),
        [
            pin("VERYLONGTOP", -2.54, 5.08, TOP),
            pin("VERYLONGBOT", 2.54, -5.08, BOTTOM),
            pin("MID", 0.0, 5.08, TOP),
        ],
    )
    fitted, _ = fit_symbol(sym)
    _, bottom, _, top = box_of(fitted)
    assert PIN_NAME_INSET_MM + text(11) <= top - bottom
    assert top - bottom < pair_need(11, 11)  # not sized for the two names against each other
    growth = grid_up((PIN_NAME_INSET_MM + text(11) - 5.08) / 2.0)
    assert (top, bottom) == pytest.approx((2.54 + growth, -2.54 - growth))


# ---------------------------------------------------------------------------------------------
# Scope: what is not touched
# ---------------------------------------------------------------------------------------------


def long_pins(x: float = 5.08) -> list[Pin]:
    """Three pins whose names are far too long for a 5.08 mm body (the positive control)."""
    return [
        pin("LONG_NAME_LEFT", -x, 0.0, LEFT),
        pin("LONG_NAME_RIGHT", x, 0.0, RIGHT),
        pin("EXTRA", -x, -2.54, LEFT),
    ]


def test_the_pins_used_in_the_scope_tests_do_grow_a_plain_rectangle() -> None:
    sym = make_symbol((-2.54, -2.54, 2.54, 2.54), long_pins())
    assert fit_symbol(sym)[0] != sym


def test_triangle_body_is_left_alone() -> None:
    w, layer = SYMBOL_WIRE_WIDTH_MM, Layer.SYMBOLS
    triangle: list[Graphic] = [
        Wire(-5.08, 5.08, -5.08, -5.08, w, layer),
        Wire(-5.08, -5.08, 5.08, 0.0, w, layer),
        Wire(5.08, 0.0, -5.08, 5.08, w, layer),
    ]
    pins = tuple(long_pins(7.62))
    sym = Symbol("T", pins, (*triangle, *build_name_value_labels(triangle, pins)))
    assert fit_symbol(sym) == (sym, [])


def test_arcs_without_a_rectangle_are_left_alone() -> None:
    w, layer = SYMBOL_WIRE_WIDTH_MM, Layer.SYMBOLS
    arcs: list[Graphic] = [
        Wire(-2.54, 0.0, 2.54, 0.0, w, layer, 180.0),
        Wire(2.54, 0.0, -2.54, 0.0, w, layer, 180.0),
    ]
    pins = tuple(long_pins())
    sym = Symbol("T", pins, (*arcs, *build_name_value_labels(arcs, pins)))
    assert fit_symbol(sym) == (sym, [])


def test_rectangle_with_a_curved_edge_is_not_a_plain_body() -> None:
    graphics = rect(-2.54, -2.54, 2.54, 2.54)
    graphics[0] = Wire(-2.54, 2.54, 2.54, 2.54, SYMBOL_WIRE_WIDTH_MM, Layer.SYMBOLS, 30.0)
    pins = tuple(long_pins())
    sym = Symbol("T", pins, (*graphics, *build_name_value_labels(graphics, pins)))
    assert fit_symbol(sym)[0] is sym


def test_graphics_outside_the_rectangle_mean_no_plain_body() -> None:
    far = Circle(20.0, 0.0, 1.0, SYMBOL_WIRE_WIDTH_MM, Layer.SYMBOLS)
    sym = make_symbol((-2.54, -2.54, 2.54, 2.54), long_pins(), extra=[far])
    assert fit_symbol(sym)[0] is sym


def test_wires_on_another_layer_do_not_make_a_body() -> None:
    graphics: list[Graphic] = [
        Wire(w.x1, w.y1, w.x2, w.y2, w.width, Layer.DOCUMENT)
        for w in rect(-2.54, -2.54, 2.54, 2.54)
        if isinstance(w, Wire)
    ]
    sym = Symbol("T", tuple(long_pins()), tuple(graphics))
    assert fit_symbol(sym)[0] is sym


def test_pin_that_does_not_touch_the_box_leaves_the_symbol_alone() -> None:
    pins = long_pins()
    pins[0] = pin("LONG_NAME_LEFT", -5.08, 20.0, LEFT)
    sym = make_symbol((-2.54, -2.54, 2.54, 2.54), pins)
    assert fit_symbol(sym)[0] is sym


def test_pin_with_a_diagonal_angle_leaves_the_symbol_alone() -> None:
    pins = long_pins()
    pins[0] = pin("LONG_NAME_LEFT", -5.08, 0.0, 45)
    sym = make_symbol((-2.54, -2.54, 2.54, 2.54), pins)
    assert fit_symbol(sym)[0] is sym


def test_inner_marks_stay_and_inner_wires_that_end_on_an_edge_follow_it() -> None:
    w, layer = SYMBOL_WIRE_WIDTH_MM, Layer.SYMBOLS
    inner_left = Wire(-5.08, -2.54, -2.54, -2.54, w, layer)  # starts on the left edge
    inner_right = Wire(2.54, 2.54, 5.08, 2.54, w, layer)  # ends on the right edge
    floating = Wire(-1.27, -1.27, 1.27, -1.27, w, layer)
    dot = Circle(0.0, 0.0, 0.5, 0.0, layer)
    sym = make_symbol(
        (-5.08, -5.08, 5.08, 5.08),
        [
            pin("OSC1", -7.62, -2.54, LEFT),
            pin("OSC2", 7.62, 2.54, RIGHT),
            pin("GND1", -7.62, 2.54, LEFT),
            pin("GND2", 7.62, -2.54, RIGHT),
        ],
        extra=[inner_left, inner_right, floating, dot],
    )
    fitted, _ = fit_symbol(sym)
    growth = grid_up((pair_need(4, 4) - 10.16) / 2.0)
    assert growth > 0.0
    assert box_of(fitted) == pytest.approx((-5.08 - growth, -5.08, 5.08 + growth, 5.08))
    assert Wire(-5.08 - growth, -2.54, -2.54, -2.54, w, layer) in fitted.graphics
    assert Wire(2.54, 2.54, 5.08 + growth, 2.54, w, layer) in fitted.graphics
    assert floating in fitted.graphics
    assert dot in fitted.graphics


# ---------------------------------------------------------------------------------------------
# Idempotence, labels, grid
# ---------------------------------------------------------------------------------------------


def _samples() -> list[Symbol]:
    return [
        make_symbol(
            (-2.54, -3.81, 2.54, 3.81),
            [
                pin("ENABLE", -5.08, 1.27, LEFT),
                pin("OUTPUT", 5.08, 1.27, RIGHT),
                pin("GND", -5.08, -1.27, LEFT),
            ],
        ),
        make_symbol(
            (-7.62, -5.08, 7.62, 5.08),
            [
                pin("IN", -10.16, 2.54, LEFT),
                pin("IN2", -10.16, -2.54, LEFT),
                pin("VCC", -5.08, 7.62, TOP),
                pin("GND", -5.08, -7.62, BOTTOM),
            ],
        ),
        make_symbol(
            (-5.08, -2.54, 5.08, 2.54),
            [
                pin("VERYLONGTOP", -2.54, 5.08, TOP),
                pin("VERYLONGBOT", -2.54, -5.08, BOTTOM),
                pin("MID", 2.54, 5.08, TOP),
            ],
        ),
    ]


@pytest.mark.parametrize("index", range(3))
def test_fitting_is_idempotent(index: int) -> None:
    sym = _samples()[index]
    once, notes = fit_symbol(sym)
    assert once != sym
    assert len(notes) == 1
    twice, notes_again = fit_symbol(once)
    assert twice is once
    assert notes_again == []


@pytest.mark.parametrize("index", range(3))
def test_labels_follow_the_new_body(index: int) -> None:
    sym = _samples()[index]
    fitted, _ = fit_symbol(sym)
    left, bottom, _, top = box_of(fitted)
    found = [g for g in fitted.graphics if isinstance(g, Label)]
    name = next(g for g in found if g.text == ">NAME")
    value = next(g for g in found if g.text == ">VALUE")
    assert len(found) == 2
    pin_top = max([top, *(p.y for p in fitted.pins)])
    pin_bottom = min([bottom, *(p.y for p in fitted.pins)])
    assert name.x == pytest.approx(left)
    assert name.y > pin_top
    assert value.y < pin_bottom
    assert value.align == TextAlign.TOP_LEFT
    assert name.layer == Layer.NAMES
    assert value.layer == Layer.VALUES


def test_label_recompute_reproduces_the_parser_labels() -> None:
    """The parser and symbol_fit share build_name_value_labels, so their labels agree."""
    doc = json.loads((FIXTURES / "synthetic" / "soic8.json").read_text(encoding="utf-8"))
    sym = parse_part(doc, fit_symbols=False).symbols[0]
    graphics = [g for g in sym.graphics if not isinstance(g, Label)]
    assert build_name_value_labels(graphics, sym.pins) == [
        g for g in sym.graphics if isinstance(g, Label)
    ]


def test_other_labels_are_kept() -> None:
    sym = _samples()[0]
    custom = Label(0.0, 0.0, "hello", 1.0, Layer.INFO)
    sym = Symbol(sym.name, sym.pins, (*sym.graphics, custom))
    fitted, _ = fit_symbol(sym)
    assert fitted != sym
    assert custom in fitted.graphics


def test_body_wires_keep_width_layer_and_are_a_closed_rectangle() -> None:
    fitted, _ = fit_symbol(_samples()[0])
    wires = [g for g in fitted.graphics if isinstance(g, Wire)]
    assert len(wires) == 4
    assert {(w.width, w.layer, w.curve) for w in wires} == {
        (SYMBOL_WIRE_WIDTH_MM, Layer.SYMBOLS, 0.0)
    }
    ends = {(w.x1, w.y1) for w in wires} | {(w.x2, w.y2) for w in wires}
    assert len(ends) == 4


def test_pins_on_the_grid_stay_on_it_and_off_grid_pins_keep_their_offset() -> None:
    sym = make_symbol(
        (-3.81, -5.08, 3.81, 5.08),
        [
            pin("ENABLE", -6.35, 2.54, LEFT),  # on the 2.54 grid in y, x = -6.35 (odd 1.27)
            pin("OUTPUT", 6.35, -1.27, RIGHT),  # off grid in y
            pin("TOPNAME", -1.27, 7.62, TOP),
            pin("BOTTOMNAME", 2.54, -7.62, BOTTOM),
        ],
    )
    fitted, _ = fit_symbol(sym)
    assert fitted != sym
    for before, after in zip(sym.pins, fitted.pins, strict=True):
        for old, new in ((before.x, after.x), (before.y, after.y)):
            steps = (new - old) / GRID_MM
            assert abs(steps - round(steps)) < TOL
    on_grid = make_symbol(
        (-2.54, -2.54, 2.54, 2.54),
        [
            pin("ENABLE", -5.08, 2.54, LEFT),
            pin("OUTPUT", 5.08, 2.54, RIGHT),
            pin("GND", -5.08, 0.0, LEFT),
        ],
    )
    fitted_grid, _ = fit_symbol(on_grid)
    assert fitted_grid != on_grid
    for p in fitted_grid.pins:
        assert abs(p.x / GRID_MM - round(p.x / GRID_MM)) < TOL
        assert abs(p.y / GRID_MM - round(p.y / GRID_MM)) < TOL


# ---------------------------------------------------------------------------------------------
# parse_part integration (multi gate parts, flag)
# ---------------------------------------------------------------------------------------------


def _ee_pin(x: float, y: float, rot: int, number: str, name: str) -> str:
    dx, dy = {0: (-1, 0), 90: (0, 1), 180: (1, 0), 270: (0, -1)}[rot]
    path = f"M {x:g} {y:g} h {dx * 10:g}" if dx else f"M {x:g} {y:g} v {dy * 10:g}"
    return (
        f"P~show~0~{number}~{x:g}~{y:g}~{rot}~gge1~0^^{x:g}~{y:g}^^{path}~#880000"
        f"^^0~{x + 3:g}~{y + 3:g}~0~{name}~start~~~#0000FF"
        f"^^1~{x + 1:g}~{y - 1:g}~0~{number}~end~~~#0000FF^^0~{x + 8:g}~{y:g}^^0~M 0 0"
    )


def _unit(shapes: list[str], half_width: int = 10) -> dict[str, Any]:
    """One unit with a rectangle body of 2 * half_width x 20 EasyEDA units centred on 0."""
    body = f"R~{-half_width}~-10~~~{2 * half_width}~20~#880000~1~0~none~gge2~0~"
    return {
        "dataStr": {
            "head": {"x": 0, "y": 0, "c_para": {}},
            "BBox": {"x": -half_width, "y": -10, "width": 2 * half_width, "height": 20},
            "shape": [body, *shapes],
        }
    }


def _multi_gate_doc() -> dict[str, Any]:
    doc = json.loads((FIXTURES / "synthetic" / "opamp2.json").read_text(encoding="utf-8"))
    doc = copy.deepcopy(doc)
    result = doc.get("result", doc)
    result["dataStr"]["shape"] = []
    result["dataStr"]["BBox"] = {"x": 0, "y": 0, "width": 0, "height": 0}
    result["subparts"] = [
        # Needs a wider body: ENABLE and OUTPUT share a row in a 5.08 mm wide box.
        _unit(
            [
                _ee_pin(-20, 0, 180, "1", "ENABLE"),
                _ee_pin(20, 0, 0, "2", "OUTPUT"),
                _ee_pin(-20, 5, 180, "3", "NC"),
            ]
        ),
        # Already fits (10.16 mm wide for one character names).
        _unit(
            [
                _ee_pin(-30, 0, 180, "4", "A"),
                _ee_pin(30, 0, 0, "5", "B"),
                _ee_pin(-30, 5, 180, "6", "C"),
            ],
            half_width=20,
        ),
        # Needs a taller body: the vertical SUPPLY name runs down into the INPUT row.
        _unit(
            [
                _ee_pin(-20, 0, 180, "7", "INPUT"),
                _ee_pin(-20, 5, 180, "8", "IN2"),
                _ee_pin(0, -20, 90, "9", "SUPPLY"),
            ]
        ),
    ]
    return doc


def test_every_unit_of_a_multi_gate_part_is_fitted_independently() -> None:
    doc = _multi_gate_doc()
    plain = parse_part(doc, fit_symbols=False)
    fitted = parse_part(doc)
    assert len(plain.symbols) == len(fitted.symbols) == 3
    assert [s.name for s in plain.symbols] == [s.name for s in fitted.symbols]
    first, second, third = fitted.symbols
    assert box_of(plain.symbols[0]) == pytest.approx((-2.54, -2.54, 2.54, 2.54))
    growth = grid_up((pair_need(6, 6) - 5.08) / 2.0)
    assert growth > 0.0
    assert box_of(first) == pytest.approx((-2.54 - growth, -2.54, 2.54 + growth, 2.54))
    assert second == plain.symbols[1]
    assert box_of(plain.symbols[2]) == pytest.approx((-2.54, -2.54, 2.54, 2.54))
    assert box_of(third)[3] > box_of(plain.symbols[2])[3]  # taller
    notes = [w for w in fitted.warnings if "body enlarged" in w]
    assert len(notes) == 2
    assert notes[0].startswith(f"Symbol {first.name}:")
    assert notes[1].startswith(f"Symbol {third.name}:")
    assert not any("body enlarged" in w for w in plain.warnings)
    assert set(plain.warnings) <= set(fitted.warnings)


def test_fit_flag_off_keeps_the_source_geometry() -> None:
    doc = _multi_gate_doc()
    plain = parse_part(doc, fit_symbols=False)
    assert box_of(plain.symbols[0]) == pytest.approx((-2.54, -2.54, 2.54, 2.54))
    assert parse_part(doc, fit_symbols=True) != plain
    assert parse_part(doc) == parse_part(doc, fit_symbols=True)


# ---------------------------------------------------------------------------------------------
# Property check over all fixtures
# ---------------------------------------------------------------------------------------------

Box = tuple[float, float, float, float]  # x1, y1, x2, y2


def _name_boxes(symbol: Symbol, body: tuple[float, float, float, float]) -> list[tuple[int, Box]]:
    """Text boxes of the pin names as Eagle draws them, tagged with the pin angle."""
    left, bottom, right, top = body
    half = PIN_NAME_CAP_HEIGHT_MM / 2.0
    boxes: list[tuple[int, Box]] = []
    for p in symbol.pins:
        length = name_length_mm(p.name)
        if p.angle == LEFT:
            x = left + PIN_NAME_INSET_MM
            boxes.append((p.angle, (x, p.y - half, x + length, p.y + half)))
        elif p.angle == RIGHT:
            x = right - PIN_NAME_INSET_MM
            boxes.append((p.angle, (x - length, p.y - half, x, p.y + half)))
        elif p.angle == TOP:
            y = top - PIN_NAME_INSET_MM
            boxes.append((p.angle, (p.x - half, y - length, p.x + half, y)))
        else:
            y = bottom + PIN_NAME_INSET_MM
            boxes.append((p.angle, (p.x - half, y, p.x + half, y + length)))
    return boxes


def _closer_than(a: Box, b: Box, clearance: float) -> bool:
    """True if the boxes are closer than ``clearance`` in both axes (touch or overlap)."""
    dx = max(a[0] - b[2], b[0] - a[2])
    dy = max(a[1] - b[3], b[1] - a[3])
    return dx < clearance - TOL and dy < clearance - TOL


def _edge_distances(symbol: Symbol, body: Box) -> list[float]:
    left, bottom, right, top = body
    out: list[float] = []
    for p in symbol.pins:
        if p.angle == LEFT:
            out.append(left - p.x)
        elif p.angle == RIGHT:
            out.append(p.x - right)
        elif p.angle == TOP:
            out.append(p.y - top)
        else:
            out.append(bottom - p.y)
    return out


def _part_paths() -> list[Path]:
    return [*SYNTH_FILES, *REAL_FILES]


@pytest.mark.parametrize("path", _part_paths(), ids=lambda p: p.stem)
def test_fitted_names_do_not_collide_and_pins_keep_touching(path: Path) -> None:
    doc = json.loads(path.read_text(encoding="utf-8"))
    plain = parse_part(doc, fit_symbols=False)
    fitted = parse_part(doc)
    assert len(plain.symbols) == len(fitted.symbols)
    for before, after in zip(plain.symbols, fitted.symbols, strict=True):
        body_before = _find_body(before)
        if (
            len(before.pins) <= _MAX_PINS_WITHOUT_NAMES
            or body_before is None
            or _split_sides(before.pins, body_before) is None
        ):
            # Names hidden, not a plain box symbol or pins that do not touch it: must not change.
            assert after == before, f"{path.stem} {before.name}: must not change"
            continue
        old_box = box_of(before)
        new_box = box_of(after)
        assert new_box[0] <= old_box[0] + TOL and new_box[2] >= old_box[2] - TOL
        assert new_box[1] <= old_box[1] + TOL and new_box[3] >= old_box[3] - TOL
        # Every pin keeps its distance from its edge (it still touches the body).
        assert _edge_distances(after, new_box) == pytest.approx(
            _edge_distances(before, old_box), abs=TOL
        ), f"{path.stem} {before.name}"
        boxes = _name_boxes(after, new_box)
        for side, box in boxes:  # every name ends inside the body
            assert new_box[0] - TOL <= box[0] and box[2] <= new_box[2] + TOL, (path.stem, side)
            assert new_box[1] - TOL <= box[1] and box[3] <= new_box[3] + TOL, (path.stem, side)
        for i, (side_a, a) in enumerate(boxes):
            for side_b, b in boxes[i + 1 :]:
                if side_a == side_b:
                    continue
                assert not _closer_than(a, b, PIN_NAME_GAP_MM), (
                    f"{path.stem} {after.name}: names of sides {side_a} and {side_b} collide"
                )
        # Names of pins on different edges stay apart (checked above); fitting twice is a no-op.
        assert fit_symbol(after) == (after, [])


@pytest.mark.skipif(not REAL_FILES, reason="no real fixtures in tests/fixtures/real")
def test_real_symbols_change_rarely() -> None:
    changed = 0
    total = 0
    for path in REAL_FILES:
        doc = json.loads(path.read_text(encoding="utf-8"))
        plain = parse_part(doc, fit_symbols=False)
        fitted = parse_part(doc)
        for before, after in zip(plain.symbols, fitted.symbols, strict=True):
            total += 1
            changed += before != after
    assert changed <= total // 4
