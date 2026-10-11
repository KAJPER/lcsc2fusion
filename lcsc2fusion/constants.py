"""Single home of units, layer tables and EasyEDA to Eagle mappings. No magic numbers elsewhere."""

from __future__ import annotations

from enum import IntEnum
from typing import NamedTuple

# EasyEDA coordinates are in 10 mil units: 1 unit = 10 * 0.0254 mm.
EE_UNIT_MM = 0.254
# Symbol pins sit on a 5 unit grid (1.27 mm) in EasyEDA.
EE_SYMBOL_GRID = 5.0

EAGLE_VERSION = "9.6.2"
GRID_MM = 2.54

SOURCE_NAME = "JLCEDA/EasyEDA Official Library"
SOURCE_LINKS = ("https://lceda.cn/", "https://easyeda.com")
ATTRIBUTION = (
    f"Source: {SOURCE_NAME} (https://lceda.cn/ , https://easyeda.com). "
    "Copyright of the library data belongs to JLCEDA/EasyEDA."
)


class Layer(IntEnum):
    """Eagle/Fusion layer numbers used by the generated library."""

    TOP = 1
    BOTTOM = 16
    PADS = 17
    VIAS = 18
    UNROUTED = 19
    DIMENSION = 20
    T_PLACE = 21
    B_PLACE = 22
    T_ORIGINS = 23
    B_ORIGINS = 24
    T_NAMES = 25
    B_NAMES = 26
    T_VALUES = 27
    B_VALUES = 28
    T_STOP = 29
    B_STOP = 30
    T_CREAM = 31
    B_CREAM = 32
    T_FINISH = 33
    B_FINISH = 34
    T_GLUE = 35
    B_GLUE = 36
    T_TEST = 37
    B_TEST = 38
    T_KEEPOUT = 39
    B_KEEPOUT = 40
    T_RESTRICT = 41
    B_RESTRICT = 42
    V_RESTRICT = 43
    DRILLS = 44
    HOLES = 45
    MILLING = 46
    MEASURES = 47
    DOCUMENT = 48
    REFERENCE = 49
    T_DOCU = 51
    B_DOCU = 52
    MODULES = 90
    NETS = 91
    BUSSES = 92
    PINS = 93
    SYMBOLS = 94
    NAMES = 95
    VALUES = 96
    INFO = 97
    GUIDE = 98


class LayerDef(NamedTuple):
    number: int
    name: str
    color: int
    fill: int
    visible: bool


def _ld(number: Layer, name: str, color: int, fill: int, visible: bool = True) -> LayerDef:
    return LayerDef(int(number), name, color, fill, visible)


# Full 46 layer set as written by Fusion/SnapEDA (see docs/RESEARCH.md 2.2).
EAGLE_LAYERS: tuple[LayerDef, ...] = (
    _ld(Layer.TOP, "Top", 4, 1),
    _ld(Layer.BOTTOM, "Bottom", 1, 1),
    _ld(Layer.PADS, "Pads", 2, 1),
    _ld(Layer.VIAS, "Vias", 2, 1),
    _ld(Layer.UNROUTED, "Unrouted", 6, 1),
    _ld(Layer.DIMENSION, "Dimension", 15, 1),
    _ld(Layer.T_PLACE, "tPlace", 7, 1),
    _ld(Layer.B_PLACE, "bPlace", 7, 1),
    _ld(Layer.T_ORIGINS, "tOrigins", 15, 1),
    _ld(Layer.B_ORIGINS, "bOrigins", 15, 1),
    _ld(Layer.T_NAMES, "tNames", 7, 1),
    _ld(Layer.B_NAMES, "bNames", 7, 1),
    _ld(Layer.T_VALUES, "tValues", 7, 1),
    _ld(Layer.B_VALUES, "bValues", 7, 1),
    _ld(Layer.T_STOP, "tStop", 7, 3),
    _ld(Layer.B_STOP, "bStop", 7, 6, False),
    _ld(Layer.T_CREAM, "tCream", 7, 4),
    _ld(Layer.B_CREAM, "bCream", 7, 5, False),
    _ld(Layer.T_FINISH, "tFinish", 6, 3, False),
    _ld(Layer.B_FINISH, "bFinish", 6, 6, False),
    _ld(Layer.T_GLUE, "tGlue", 7, 4, False),
    _ld(Layer.B_GLUE, "bGlue", 7, 5, False),
    _ld(Layer.T_TEST, "tTest", 7, 1, False),
    _ld(Layer.B_TEST, "bTest", 7, 1, False),
    _ld(Layer.T_KEEPOUT, "tKeepout", 4, 11),
    _ld(Layer.B_KEEPOUT, "bKeepout", 1, 11),
    _ld(Layer.T_RESTRICT, "tRestrict", 4, 10),
    _ld(Layer.B_RESTRICT, "bRestrict", 1, 10),
    _ld(Layer.V_RESTRICT, "vRestrict", 2, 10),
    _ld(Layer.DRILLS, "Drills", 7, 1, False),
    _ld(Layer.HOLES, "Holes", 7, 1, False),
    _ld(Layer.MILLING, "Milling", 3, 1, False),
    _ld(Layer.MEASURES, "Measures", 7, 1, False),
    _ld(Layer.DOCUMENT, "Document", 7, 1),
    _ld(Layer.REFERENCE, "Reference", 7, 1),
    _ld(Layer.T_DOCU, "tDocu", 7, 1),
    _ld(Layer.B_DOCU, "bDocu", 7, 1),
    _ld(Layer.MODULES, "Modules", 5, 1),
    _ld(Layer.NETS, "Nets", 2, 1),
    _ld(Layer.BUSSES, "Busses", 1, 1),
    _ld(Layer.PINS, "Pins", 2, 1),
    _ld(Layer.SYMBOLS, "Symbols", 4, 1),
    _ld(Layer.NAMES, "Names", 7, 1),
    _ld(Layer.VALUES, "Values", 7, 1),
    _ld(Layer.INFO, "Info", 7, 1),
    _ld(Layer.GUIDE, "Guide", 6, 1),
)

DECLARED_LAYER_NUMBERS: frozenset[int] = frozenset(ld.number for ld in EAGLE_LAYERS)

# EasyEDA footprint layer id -> Eagle layer. None means "not drawn" (mask/paste come from
# the stop/cream attributes of pads). Layer 11 (Multi-Layer) is handled by the pad logic.
EE_FOOTPRINT_LAYER_TO_EAGLE: dict[int, Layer | None] = {
    1: Layer.TOP,
    2: Layer.BOTTOM,
    3: Layer.T_PLACE,
    4: Layer.B_PLACE,
    5: None,
    6: None,
    7: None,
    8: None,
    10: Layer.DIMENSION,
    12: Layer.T_DOCU,
    13: Layer.T_DOCU,
    14: Layer.B_DOCU,
    15: Layer.T_DOCU,
    99: Layer.T_DOCU,
    100: Layer.T_DOCU,
    101: Layer.T_PLACE,
}
EE_LAYER_MULTI = 11

# EasyEDA pin electrical type code -> Eagle pin direction value.
EE_PIN_TYPE_TO_DIRECTION: dict[int, str] = {0: "pas", 1: "in", 2: "out", 3: "io", 4: "pwr"}

# Eagle pin length keywords in mm.
PIN_LENGTH_POINT_MM = 0.0
PIN_LENGTH_SHORT_MM = 2.54
PIN_LENGTH_MIDDLE_MM = 5.08
PIN_LENGTH_LONG_MM = 7.62

# Drawing defaults (mm).
SYMBOL_WIRE_WIDTH_MM = 0.254
SILK_WIDTH_MM = 0.15
DOCU_WIDTH_MM = 0.1
COURTYARD_WIDTH_MM = 0.05
COURTYARD_MARGIN_MM = 0.25
LABEL_SIZE_PACKAGE_MM = 0.8
LABEL_SIZE_SYMBOL_MM = 1.778
MIN_POLYGON_WIDTH_MM = 0.01
