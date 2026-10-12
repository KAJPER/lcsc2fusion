"""Internal data model. Always millimetres, Y axis up, angles in degrees counter-clockwise.

The parser is the only place that converts from EasyEDA units/axes (see parser/units.py).
Layers are Eagle layer numbers (constants.Layer); the EasyEDA to Eagle mapping lives in constants.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

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
class Model3D:
    """Placement of an EasyEDA 3D model relative to the package, in millimetres, Z up.

    The STEP file is not centred on the package origin. A consumer applies, to a model point p
    with `c` the XY centre of the model bounding box and `zmin` its lowest Z:
    `q_xy = Rz(rotation_deg[2]) * (p_xy - c) + offset_mm` and `q_z = p_z - zmin + bottom_z_mm`.
    The parser is the only place that converts from EasyEDA units (see parser/model3d.py).
    """

    uuid: str
    name: str
    offset_mm: tuple[float, float]  # where the bbox centre goes (Y up, package origin = 0,0)
    bottom_z_mm: float  # world Z of the lowest point of the model
    rotation_deg: tuple[float, float, float]  # (rx, ry, rz), only rz is applied
    native_size_mm: tuple[float, float]  # unrotated X and Y size of the model
    placement_source: str  # "c_origin" or "outline_centre" (when c_origin is stale)


@dataclass(frozen=True)
class ModelFiles:
    """Files written next to a library for one part when 3D output is requested."""

    lcsc_id: str
    step_path: Path  # raw STEP, byte for byte as downloaded
    placed_step_path: Path  # same model transformed into the package frame
    info_path: Path  # <id>.3d.json with placement and attribution
    warnings: tuple[str, ...] = ()


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
    model3d: Model3D | None = None


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
