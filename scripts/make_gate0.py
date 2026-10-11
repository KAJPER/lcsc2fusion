"""Generate docs/gate0_resistor.lbr, the hand-built 0603 resistor for Gate 0 (RESEARCH.md 7).

Import the result into Fusion Electronics by hand (File > Open > Open from my computer, then
Save) and check that symbol, footprint and device show up in the Place panel.

Usage: python scripts/make_gate0.py [output.lbr]
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lcsc2fusion import constants  # noqa: E402
from lcsc2fusion.constants import Layer  # noqa: E402
from lcsc2fusion.lbr_writer.lint import format_issues, lint_library  # noqa: E402
from lcsc2fusion.lbr_writer.writer import write_library  # noqa: E402
from lcsc2fusion.model import (  # noqa: E402
    Graphic,
    Label,
    Package,
    Pad,
    PadShape,
    Part,
    Pin,
    PinDirection,
    PinLength,
    Symbol,
    Wire,
)

DEFAULT_OUTPUT = REPO_ROOT / "docs" / "gate0_resistor.lbr"

PAD_X_MM = 0.8
PAD_WIDTH_MM = 0.8
PAD_HEIGHT_MM = 0.95
BODY_HALF_LENGTH_MM = 0.8
BODY_HALF_WIDTH_MM = 0.4
SILK_Y_MM = 0.6
SILK_HALF_LENGTH_MM = 0.2
SYMBOL_HALF_LENGTH_MM = 2.54
SYMBOL_HALF_WIDTH_MM = 1.016
SYMBOL_PIN_X_MM = 5.08


def box(x: float, y: float, layer: Layer, width: float) -> list[Graphic]:
    """Closed rectangle centred on the origin, made of four wires."""
    corners = [(-x, -y), (x, -y), (x, y), (-x, y)]
    return [Wire(*corners[i], *corners[(i + 1) % 4], width, layer) for i in range(len(corners))]


def build_resistor() -> Part:
    pads = tuple(
        Pad(
            name=str(number),
            number=str(number),
            x=x,
            y=0.0,
            width=PAD_WIDTH_MM,
            height=PAD_HEIGHT_MM,
            shape=PadShape.RECT,
        )
        for number, x in ((1, -PAD_X_MM), (2, PAD_X_MM))
    )
    courtyard_x = PAD_X_MM + PAD_WIDTH_MM / 2 + constants.COURTYARD_MARGIN_MM
    courtyard_y = PAD_HEIGHT_MM / 2 + constants.COURTYARD_MARGIN_MM
    package_graphics: list[Graphic] = [
        *(
            Wire(
                -SILK_HALF_LENGTH_MM,
                y,
                SILK_HALF_LENGTH_MM,
                y,
                constants.SILK_WIDTH_MM,
                Layer.T_PLACE,
            )
            for y in (-SILK_Y_MM, SILK_Y_MM)
        ),
        *box(BODY_HALF_LENGTH_MM, BODY_HALF_WIDTH_MM, Layer.T_DOCU, constants.DOCU_WIDTH_MM),
        *box(courtyard_x, courtyard_y, Layer.T_KEEPOUT, constants.COURTYARD_WIDTH_MM),
        Label(-1.27, 1.1, ">NAME", constants.LABEL_SIZE_PACKAGE_MM, Layer.T_NAMES),
        Label(-1.27, -1.9, ">VALUE", constants.LABEL_SIZE_PACKAGE_MM, Layer.T_VALUES),
    ]
    package = Package(
        name="0603",
        description="Chip resistor 0603 (1608 metric)",
        pads=pads,
        graphics=tuple(package_graphics),
    )

    symbol_graphics: list[Graphic] = [
        *box(
            SYMBOL_HALF_LENGTH_MM,
            SYMBOL_HALF_WIDTH_MM,
            Layer.SYMBOLS,
            constants.SYMBOL_WIRE_WIDTH_MM,
        ),
        Label(-2.54, 2.54, ">NAME", constants.LABEL_SIZE_SYMBOL_MM, Layer.NAMES),
        Label(-2.54, -3.81, ">VALUE", constants.LABEL_SIZE_SYMBOL_MM, Layer.VALUES),
    ]
    pins = (
        Pin("1", "1", -SYMBOL_PIN_X_MM, 0.0, PinLength.SHORT, 0, PinDirection.PAS),
        Pin("2", "2", SYMBOL_PIN_X_MM, 0.0, PinLength.SHORT, 180, PinDirection.PAS),
    )
    symbol = Symbol(name="R", pins=pins, graphics=tuple(symbol_graphics))

    return Part(
        lcsc_id="C25804",
        mpn="0603WAF1002T5E",
        manufacturer="UNI-ROYAL",
        description="10k 1% 0603 thick film chip resistor (Gate 0 hand-built sample)",
        prefix="R?",
        symbols=(symbol,),
        package=package,
        attributes=(("RESISTANCE", "10k"),),
    )


def main(argv: list[str]) -> int:
    output = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUTPUT
    path = write_library(output, [build_resistor()], description="Gate 0 sample: 0603 resistor")
    print(f"Wrote {path}")
    issues = lint_library(path.read_text(encoding="utf-8"))
    if issues:
        print(format_issues(issues))
    else:
        print("Lint: OK, no issues")
    return 1 if any(issue.severity == "error" for issue in issues) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
