"""Internal data model. Always millimetres, Y axis up, angles in degrees counter-clockwise.

The parser is the only place that converts from EasyEDA units/axes (see parser/units.py).
Layers are Eagle layer numbers (constants.Layer); the EasyEDA to Eagle mapping lives in constants.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from lcsc2fusion.constants import Layer


class PadShape(StrEnum):
    RECT = "rect"
    OVAL = "oval"
    ROUND = "round"
    POLYGON = "polygon"


class PinDirection(StrEnum):
    """Eagle pin direction values."""

    NC = "nc"
    IN = "in"
    OUT = "out"
    IO = "io"
    OC = "oc"
    PWR = "pwr"
    PAS = "pas"
    HIZ = "hiz"
    SUP = "sup"


class PinLength(StrEnum):
    POINT = "point"
    SHORT = "short"
    MIDDLE = "middle"
    LONG = "long"


class TextAlign(StrEnum):
    BOTTOM_LEFT = "bottom-left"
    BOTTOM_CENTER = "bottom-center"
    BOTTOM_RIGHT = "bottom-right"
    CENTER_LEFT = "center-left"
    CENTER = "center"
    CENTER_RIGHT = "center-right"
    TOP_LEFT = "top-left"
    TOP_CENTER = "top-center"
    TOP_RIGHT = "top-right"


@dataclass(frozen=True)
class Point:
    x: float
    y: float


@dataclass(frozen=True)
class Wire:
    """Straight line, or an arc when curve != 0 (signed sweep angle from p1 to p2, CCW positive)."""

    x1: float
    y1: float
    x2: float
    y2: float
    width: float
    layer: Layer
    curve: float = 0.0


@dataclass(frozen=True)
class Circle:
    """Ring when width > 0, filled disc when width == 0."""

    x: float
    y: float
    radius: float
    width: float
    layer: Layer


@dataclass(frozen=True)
class Rectangle:
    """Always filled."""

    x1: float
    y1: float
    x2: float
    y2: float
    layer: Layer
    rotation: float = 0.0


@dataclass(frozen=True)
class Polygon:
    points: tuple[Point, ...]
    width: float
    layer: Layer


@dataclass(frozen=True)
class Label:
    x: float
    y: float
    text: str
    size: float
    layer: Layer
    rotation: float = 0.0
    align: TextAlign = TextAlign.BOTTOM_LEFT


Graphic = Wire | Circle | Rectangle | Polygon | Label


@dataclass(frozen=True)
class Pad:
    """A package pad. `number` is the EasyEDA pad number (may repeat), `name` is unique."""

    name: str
    number: str
    x: float
    y: float
    width: float
    height: float
    shape: PadShape
    rotation: float = 0.0
    layer: Layer = Layer.TOP
    drill: float = 0.0  # drill diameter, 0 means SMD
    slot_length: float = 0.0  # > drill for slotted holes (total length along slot_angle)
    slot_angle: float = 0.0
    plated: bool = True
    outline: tuple[Point, ...] = ()  # POLYGON pads, package coordinates

    @property
    def is_smd(self) -> bool:
        return self.drill <= 0.0


@dataclass(frozen=True)
class Hole:
    """Non-plated mounting hole."""

    x: float
    y: float
    drill: float


@dataclass(frozen=True)
class Package:
    name: str
    description: str
    pads: tuple[Pad, ...]
    holes: tuple[Hole, ...] = ()
    graphics: tuple[Graphic, ...] = ()


@dataclass(frozen=True)
class Pin:
    """Symbol pin. (x, y) is the connection point, `angle` the direction the pin line extends
    from it towards the symbol body (0 = +x, 90 = +y, ...). `name` is unique within the symbol,
    `number` equals the number of the footprint pad(s) it connects to."""

    name: str
    number: str
    x: float
    y: float
    length: PinLength
    angle: int
    direction: PinDirection
    inverted: bool = False
    clock: bool = False


@dataclass(frozen=True)
class Symbol:
    name: str
    pins: tuple[Pin, ...]
    graphics: tuple[Graphic, ...] = ()


@dataclass(frozen=True)
class Part:
    """One LCSC part: one symbol per gate (multi-unit parts have several) and one package."""

    lcsc_id: str
    mpn: str
    manufacturer: str
    description: str
    prefix: str
    symbols: tuple[Symbol, ...]
    package: Package
    datasheet: str = ""
    source_uuid: str = ""
    attributes: tuple[tuple[str, str], ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class SearchResult:
    lcsc_id: str
    mpn: str
    manufacturer: str
    package: str
    description: str
    datasheet_url: str = ""
    image_url: str = ""
    library_type: str = ""  # "Basic" / "Extended" when known
    stock: int | None = None
