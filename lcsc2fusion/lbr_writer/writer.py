"""Internal model to Eagle .lbr XML (docs/RESEARCH.md section 2, decisions D-004, D-005, D-008).

Output is deterministic: stable element order, fixed number formatting, no timestamps. Runtime
uses only xml.etree.ElementTree. The generated text is linted before it is returned.
"""

from __future__ import annotations

import html
import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from pathlib import Path

from lcsc2fusion.constants import (
    ATTRIBUTION,
    EAGLE_LAYERS,
    EAGLE_VERSION,
    EAGLE_VERSION_SLOTS,
    MIN_POLYGON_WIDTH_MM,
    Layer,
)
from lcsc2fusion.errors import ConversionError, LintError
from lcsc2fusion.lbr_writer.lint import format_issues, lint_library
from lcsc2fusion.model import (
    Circle,
    Graphic,
    Hole,
    Label,
    Package,
    Pad,
    PadShape,
    Part,
    Pin,
    PinDirection,
    Point,
    Polygon,
    Rectangle,
    Symbol,
    TextAlign,
    Wire,
)

__all__ = [
    "build_library",
    "format_number",
    "sanitize_name",
    "write_library",
]

XML_DECLARATION = '<?xml version="1.0" encoding="utf-8"?>'
DOCTYPE = '<!DOCTYPE eagle SYSTEM "eagle.dtd">'

MAX_DECIMALS = 6
# Eagle limits the sweep of an arc to this many degrees (full circles use <circle>).
MAX_CURVE_DEG = 359.9
SMD_ROUNDNESS_FULL = 100
# Vector font stroke ratio in percent (convention from docs/RESEARCH.md 2.5).
TEXT_RATIO = 12
# Milling outline of slotted holes.
SLOT_OUTLINE_WIDTH_MM = 0.02
SLOT_MIN_EXTRA_LENGTH_MM = 1e-6
# Vertical distance between gates of a multi-unit device so that they do not overlap.
GATE_SPACING_MM = 25.4
DEFAULT_PREFIX = "U"
USERVALUE_PREFIXES = frozenset({"R", "C", "L"})
TECHNOLOGY_ATTRIBUTE_SOURCE = "SOURCE"

_NAME_INVALID_CHARS = re.compile(r"[^A-Z0-9_\-.+]")
_REF_NAME_INVALID_CHARS = re.compile(r"[\s;]")
_ATTR_NAME_INVALID_CHARS = re.compile(r"[^A-Z0-9_\-]")
_XML_INVALID_CHARS = re.compile(r"[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")

_PAD_SHAPE_TO_EAGLE: dict[PadShape, str] = {
    PadShape.ROUND: "round",
    PadShape.RECT: "square",
    PadShape.OVAL: "long",
    PadShape.POLYGON: "square",
}


# ----- text and number helpers -----


def format_number(value: float) -> str:
    """At most 6 decimals, trailing zeros stripped, no exponent, never "-0"."""
    if not math.isfinite(value):
        raise ConversionError(f"Cannot write non-finite number {value!r} to a library")
    text = f"{value:.{MAX_DECIMALS}f}".rstrip("0").rstrip(".")
    if text in ("", "-", "-0"):
        return "0"
    return text


def _xml_text(value: str) -> str:
    """Drop characters that are not allowed in XML 1.0."""
    return _XML_INVALID_CHARS.sub("", value)


def _attr_text(value: str) -> str:
    """Attribute value: XML safe, whitespace collapsed to single spaces."""
    return " ".join(_xml_text(value).split())


def _html_text(value: str) -> str:
    """Escape for use inside an Eagle <description>, whose content is HTML."""
    return html.escape(_attr_text(value), quote=False)


def sanitize_name(raw: str, fallback: str = "") -> str:
    """Upper-case; characters other than A-Z 0-9 _ - . + become `_`. Uses `fallback` if empty."""
    for candidate in (raw, fallback):
        name = _NAME_INVALID_CHARS.sub("_", _attr_text(candidate).upper())
        if name:
            return name
    raise ConversionError(f"Cannot derive a library name from {raw!r} (fallback {fallback!r})")


def _clean_ref_name(raw: str, what: str, ctx: str) -> str:
    """Pad and pin names: keep them as is, but no whitespace or `;` (the Fusion loader rejects)."""
    name = _REF_NAME_INVALID_CHARS.sub("_", _xml_text(raw).strip())
    if not name:
        raise ConversionError(f"{ctx}: {what} has an empty name")
    return name


def _attribute_name(raw: str) -> str:
    name = _ATTR_NAME_INVALID_CHARS.sub("_", _attr_text(raw).upper())
    return "_" + name[1:] if name.startswith("-") else name


def _rot(angle: float) -> str | None:
    """`R<deg>` normalised to [0, 360), or None for a zero rotation."""
    if not math.isfinite(angle):
        raise ConversionError(f"Cannot write non-finite rotation {angle!r}")
    normalized = round(angle % 360.0, MAX_DECIMALS)
    if normalized == 0.0 or normalized >= 360.0:
        return None
    return f"R{format_number(normalized)}"


def _description_html(title: str, *paragraphs: str) -> str:
    chunks = [f"<b>{_html_text(title)}</b>"] if title.strip() else []
    chunks.extend(f"<p>{_html_text(p)}</p>" for p in paragraphs if p.strip())
    return "\n".join(chunks)


class _NameRegistry:
    """Hands out names that are unique case-insensitively, appending `_2`, `_3`, ... on clashes."""

    def __init__(self) -> None:
        self._taken: set[str] = set()

    def claim(self, base: str) -> str:
        candidate = base
        counter = 1
        while candidate.upper() in self._taken:
            counter += 1
            candidate = f"{base}_{counter}"
        self._taken.add(candidate.upper())
        return candidate


# ----- graphics -----


def _n(value: float) -> str:
    return format_number(value)


def _add_graphic(parent: ET.Element, graphic: Graphic, ctx: str) -> None:
    if isinstance(graphic, Wire):
        attrs = {
            "x1": _n(graphic.x1),
            "y1": _n(graphic.y1),
            "x2": _n(graphic.x2),
            "y2": _n(graphic.y2),
            "width": _n(graphic.width),
            "layer": str(int(graphic.layer)),
        }
        if graphic.curve != 0.0:
            if not math.isfinite(graphic.curve) or abs(graphic.curve) >= 360.0:
                raise ConversionError(
                    f"{ctx}: arc sweep {graphic.curve!r} is out of range, use a circle instead"
                )
            curve = max(-MAX_CURVE_DEG, min(MAX_CURVE_DEG, graphic.curve))
            attrs["curve"] = _n(curve)
        ET.SubElement(parent, "wire", attrs)
    elif isinstance(graphic, Circle):
        ET.SubElement(
            parent,
            "circle",
            {
                "x": _n(graphic.x),
                "y": _n(graphic.y),
                "radius": _n(graphic.radius),
                "width": _n(graphic.width),
                "layer": str(int(graphic.layer)),
            },
        )
    elif isinstance(graphic, Rectangle):
        attrs = {
            "x1": _n(graphic.x1),
            "y1": _n(graphic.y1),
            "x2": _n(graphic.x2),
            "y2": _n(graphic.y2),
            "layer": str(int(graphic.layer)),
        }
        rot = _rot(graphic.rotation)
        if rot is not None:
            attrs["rot"] = rot
        ET.SubElement(parent, "rectangle", attrs)
    elif isinstance(graphic, Polygon):
        if len(graphic.points) < 3:
            raise ConversionError(
                f"{ctx}: polygon has {len(graphic.points)} vertices, at least 3 are required"
            )
        polygon = ET.SubElement(
            parent,
            "polygon",
            {
                "width": _n(max(graphic.width, MIN_POLYGON_WIDTH_MM)),
                "layer": str(int(graphic.layer)),
            },
        )
        for point in graphic.points:
            ET.SubElement(polygon, "vertex", {"x": _n(point.x), "y": _n(point.y)})
    elif isinstance(graphic, Label):
        _add_label(parent, graphic)
    else:
        raise ConversionError(f"{ctx}: unsupported graphic {type(graphic).__name__}")


def _add_label(parent: ET.Element, label: Label) -> None:
    if not label.text.strip():
        return
    attrs = {
        "x": _n(label.x),
        "y": _n(label.y),
        "size": _n(label.size),
        "layer": str(int(label.layer)),
        "font": "vector",
        "ratio": str(TEXT_RATIO),
    }
    rot = _rot(label.rotation)
    if rot is not None:
        attrs["rot"] = rot
    if label.align is not TextAlign.BOTTOM_LEFT:
        attrs["align"] = label.align.value
    text = ET.SubElement(parent, "text", attrs)
    text.text = _xml_text(label.text)


# ----- package -----


def _bbox(points: Sequence[Point]) -> tuple[float, float, float, float]:
    xs = [p.x for p in points]
    ys = [p.y for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _add_smd(parent: ET.Element, pad: Pad, name: str, ctx: str) -> None:
    if pad.layer not in (Layer.TOP, Layer.BOTTOM):
        raise ConversionError(f"{ctx}: SMD pad '{name}' is on layer {int(pad.layer)}, not 1 or 16")
    x, y, width, height, rotation = pad.x, pad.y, pad.width, pad.height, pad.rotation
    roundness = 0
    if pad.shape is PadShape.POLYGON and pad.outline:
        # Eagle has no polygon pads, approximate with the bounding box (known limitation).
        x1, y1, x2, y2 = _bbox(pad.outline)
        x, y, width, height, rotation = (x1 + x2) / 2, (y1 + y2) / 2, x2 - x1, y2 - y1, 0.0
    elif pad.shape in (PadShape.OVAL, PadShape.ROUND):
        roundness = SMD_ROUNDNESS_FULL
    if width <= 0 or height <= 0:
        raise ConversionError(f"{ctx}: SMD pad '{name}' has a non-positive size")
    attrs = {
        "name": name,
        "x": _n(x),
        "y": _n(y),
        "dx": _n(width),
        "dy": _n(height),
        "layer": str(int(pad.layer)),
        "roundness": str(roundness),
    }
    rot = _rot(rotation)
    if rot is not None:
        attrs["rot"] = rot
    ET.SubElement(parent, "smd", attrs)


def _is_slot(pad: Pad) -> bool:
    return pad.slot_length - pad.drill > SLOT_MIN_EXTRA_LENGTH_MM


def _add_through_pad(
    parent: ET.Element, pad: Pad, name: str, ctx: str, *, native_slot: bool = False
) -> None:
    width, height, rotation = pad.width, pad.height, pad.rotation
    if pad.shape is PadShape.POLYGON and pad.outline:
        x1, y1, x2, y2 = _bbox(pad.outline)
        width, height, rotation = x2 - x1, y2 - y1, 0.0
    if width <= 0 or height <= 0:
        raise ConversionError(f"{ctx}: pad '{name}' has a non-positive size")
    shape = _PAD_SHAPE_TO_EAGLE[pad.shape]
    if shape == "long":
        diameter = min(width, height)
        if height > width:
            # A long pad is drawn along X at R0, so a vertical oval needs a quarter turn.
            rotation += 90.0
    else:
        diameter = max(width, height)
    attrs = {
        "name": name,
        "x": _n(pad.x),
        "y": _n(pad.y),
        "drill": _n(pad.drill),
        "diameter": _n(diameter),
        "shape": shape,
    }
    if native_slot and _is_slot(pad):
        # Fusion's own format: drill is the slot width, slotLength the total length, rot the
        # direction of the long axis (R0 = along X).
        attrs["shape"] = "slot"
        attrs["slotLength"] = _n(pad.slot_length)
        attrs["diameter"] = _n(max(min(width, height), pad.drill))
        rotation = pad.slot_angle
    rot = _rot(rotation)
    if rot is not None:
        attrs["rot"] = rot
    ET.SubElement(parent, "pad", attrs)


def _slot_outline(pad: Pad) -> list[Wire]:
    """Milling outline (layer 46) of a slot: two straight and two semicircle wires, CCW loop."""
    angle = math.radians(pad.slot_angle)
    ux, uy = math.cos(angle), math.sin(angle)
    nx, ny = -uy, ux
    radius = pad.drill / 2.0
    half = (pad.slot_length - pad.drill) / 2.0
    c1 = (pad.x - half * ux, pad.y - half * uy)
    c2 = (pad.x + half * ux, pad.y + half * uy)

    def edge(center: tuple[float, float], side: float) -> tuple[float, float]:
        return center[0] + side * radius * nx, center[1] + side * radius * ny

    def wire(p1: tuple[float, float], p2: tuple[float, float], curve: float = 0.0) -> Wire:
        return Wire(p1[0], p1[1], p2[0], p2[1], SLOT_OUTLINE_WIDTH_MM, Layer.MILLING, curve)

    return [
        wire(edge(c1, -1.0), edge(c2, -1.0)),
        wire(edge(c2, -1.0), edge(c2, 1.0), 180.0),
        wire(edge(c2, 1.0), edge(c1, 1.0)),
        wire(edge(c1, 1.0), edge(c1, -1.0), 180.0),
    ]


def _add_hole(parent: ET.Element, hole: Hole, ctx: str) -> None:
    if hole.drill <= 0:
        raise ConversionError(f"{ctx}: hole has a non-positive drill")
    ET.SubElement(parent, "hole", {"x": _n(hole.x), "y": _n(hole.y), "drill": _n(hole.drill)})


def _build_package(
    package: Package, name: str, ctx: str, *, native_slots: bool = True
) -> ET.Element:
    element = ET.Element("package", {"name": name})
    if package.description.strip():
        ET.SubElement(element, "description").text = _html_text(package.description)
    slots: list[Wire] = []
    for pad in package.pads:
        pad_name = _clean_ref_name(pad.name, "pad", ctx)
        if pad.is_smd:
            _add_smd(element, pad, pad_name, ctx)
            continue
        _add_through_pad(element, pad, pad_name, ctx, native_slot=native_slots)
        if not native_slots and _is_slot(pad):
            slots.extend(_slot_outline(pad))
    for wire in slots:
        _add_graphic(element, wire, ctx)
    for hole in package.holes:
        _add_hole(element, hole, ctx)
    for graphic in package.graphics:
        _add_graphic(element, graphic, ctx)
    return element


# ----- symbol -----


def _pin_function(pin: Pin) -> str | None:
    if pin.inverted and pin.clock:
        return "dotclk"
    if pin.inverted:
        return "dot"
    if pin.clock:
        return "clk"
    return None


def _build_symbol(symbol: Symbol, name: str, ctx: str) -> ET.Element:
    element = ET.Element("symbol", {"name": name})
    for graphic in symbol.graphics:
        _add_graphic(element, graphic, ctx)
    visible = "off" if len(symbol.pins) <= 2 else "both"
    swappable = len(symbol.pins) == 2 and all(p.direction is PinDirection.PAS for p in symbol.pins)
    for pin in symbol.pins:
        pin_name = _clean_ref_name(pin.name, "pin", ctx)
        if pin.angle % 360 not in (0, 90, 180, 270):
            raise ConversionError(f"{ctx}: pin '{pin_name}' angle {pin.angle} is not 0/90/180/270")
        attrs = {
            "name": pin_name,
            "x": _n(pin.x),
            "y": _n(pin.y),
            "visible": visible,
            "length": pin.length.value,
            "direction": pin.direction.value,
        }
        function = _pin_function(pin)
        if function is not None:
            attrs["function"] = function
        if swappable:
            attrs["swaplevel"] = "1"
        rot = _rot(float(pin.angle))
        if rot is not None:
            attrs["rot"] = rot
        ET.SubElement(element, "pin", attrs)
    return element


# ----- device set -----


def _build_connects(
    part: Part, gate_names: Sequence[str], package: Package, ctx: str
) -> ET.Element:
    pads_by_number: dict[str, list[str]] = {}
    for pad in package.pads:
        pads_by_number.setdefault(pad.number, []).append(_clean_ref_name(pad.name, "pad", ctx))
    connects = ET.Element("connects")
    for gate_name, symbol in zip(gate_names, part.symbols, strict=True):
        for pin in symbol.pins:
            pad_names = pads_by_number.get(pin.number)
            if not pad_names:
                raise ConversionError(
                    f"{ctx}: pin '{pin.name}' (number '{pin.number}') has no pad with that "
                    f"number in package '{package.name}'"
                )
            ET.SubElement(
                connects,
                "connect",
                {
                    "gate": gate_name,
                    "pin": _clean_ref_name(pin.name, "pin", ctx),
                    "pad": " ".join(pad_names),
                },
            )
    return connects


def _technology_attributes(part: Part) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = [
        ("MPN", part.mpn),
        ("MANUFACTURER", part.manufacturer),
        ("LCSC_PART", part.lcsc_id),
        ("DATASHEET", part.datasheet),
    ]
    candidates.extend(part.attributes)
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_name, raw_value in [*candidates, (TECHNOLOGY_ATTRIBUTE_SOURCE, ATTRIBUTION)]:
        name = _attribute_name(raw_name)
        value = _attr_text(raw_value)
        # Skip empty values and repeated names (first one wins, e.g. a parser attribute that
        # duplicates MANUFACTURER).
        if not name or not value or name in seen:
            continue
        seen.add(name)
        result.append((name, value))
    return result


def _build_deviceset(
    part: Part, name: str, symbol_names: Sequence[str], package_name: str, ctx: str
) -> ET.Element:
    prefix = _REF_NAME_INVALID_CHARS.sub("_", _attr_text(part.prefix).rstrip("?").strip())
    prefix = prefix or DEFAULT_PREFIX
    attrs = {"name": name, "prefix": prefix}
    if prefix.upper() in USERVALUE_PREFIXES:
        attrs["uservalue"] = "yes"
    deviceset = ET.Element("deviceset", attrs)

    paragraphs = [part.description, ATTRIBUTION]
    ET.SubElement(deviceset, "description").text = _description_html(part.mpn or name, *paragraphs)

    multi_unit = len(symbol_names) > 1
    gate_names = [f"G${index}" for index in range(1, len(symbol_names) + 1)]
    gates = ET.SubElement(deviceset, "gates")
    for index, (gate_name, symbol_name) in enumerate(zip(gate_names, symbol_names, strict=True)):
        gate_attrs = {
            "name": gate_name,
            "symbol": symbol_name,
            "x": "0",
            "y": _n(-GATE_SPACING_MM * index),
        }
        if multi_unit:
            gate_attrs["addlevel"] = "next"
        ET.SubElement(gates, "gate", gate_attrs)

    devices = ET.SubElement(deviceset, "devices")
    device = ET.SubElement(devices, "device", {"name": "", "package": package_name})
    device.append(_build_connects(part, gate_names, part.package, ctx))
    technologies = ET.SubElement(device, "technologies")
    technology = ET.SubElement(technologies, "technology", {"name": ""})
    for attr_name, attr_value in _technology_attributes(part):
        ET.SubElement(
            technology, "attribute", {"name": attr_name, "value": attr_value, "constant": "no"}
        )
    return deviceset


# ----- library -----


def _build_layers() -> ET.Element:
    layers = ET.Element("layers")
    for layer in EAGLE_LAYERS:
        ET.SubElement(
            layers,
            "layer",
            {
                "number": str(layer.number),
                "name": layer.name,
                "color": str(layer.color),
                "fill": str(layer.fill),
                "visible": "yes" if layer.visible else "no",
                "active": "yes",
            },
        )
    return layers


def _build_grid() -> ET.Element:
    return ET.Element(
        "grid",
        {
            "distance": "0.1",
            "unitdist": "inch",
            "unit": "inch",
            "style": "lines",
            "multiple": "1",
            "display": "no",
            "altdistance": "0.01",
            "altunitdist": "inch",
            "altunit": "inch",
        },
    )


def _part_context(part: Part) -> str:
    return f"part {part.lcsc_id or part.mpn or '?'}"


def build_library(
    parts: Sequence[Part], *, description: str = "", native_slots: bool = True
) -> str:
    """Build the complete .lbr text for `parts`.

    Slotted holes are written as Fusion style slot pads (`shape="slot"`, format version 9.7.0)
    unless `native_slots` is False, which draws the slot outline on the Milling layer instead.

    Raises ConversionError when a part cannot be represented and LintError when the generated
    library has error-severity lint issues.
    """
    if not parts:
        raise ConversionError("No parts to write: an empty library would be useless")

    packages = ET.Element("packages")
    symbols = ET.Element("symbols")
    devicesets = ET.Element("devicesets")
    package_names = _NameRegistry()
    symbol_names = _NameRegistry()
    deviceset_names = _NameRegistry()
    package_by_content: dict[tuple[str, str], str] = {}

    for part in parts:
        ctx = _part_context(part)
        if not part.symbols:
            raise ConversionError(f"{ctx}: part has no symbols")

        deviceset_name = deviceset_names.claim(sanitize_name(part.mpn, part.lcsc_id))

        # Identical packages (same name and content) are written once and shared.
        base = sanitize_name(part.package.name, "PACKAGE")
        draft = _build_package(part.package, "", ctx, native_slots=native_slots)
        key = (base, ET.tostring(draft, encoding="unicode"))
        package_name = package_by_content.get(key)
        if package_name is None:
            package_name = package_names.claim(base)
            package_by_content[key] = package_name
            draft.set("name", package_name)
            packages.append(draft)

        unit_names: list[str] = []
        for index, symbol in enumerate(part.symbols, start=1):
            base_symbol = deviceset_name if len(part.symbols) == 1 else f"{deviceset_name}_{index}"
            unit_name = symbol_names.claim(base_symbol)
            unit_names.append(unit_name)
            symbols.append(_build_symbol(symbol, unit_name, f"{ctx}, symbol {index}"))
        devicesets.append(_build_deviceset(part, deviceset_name, unit_names, package_name, ctx))

    library = ET.Element("library")
    ET.SubElement(library, "description").text = _library_description(parts, description)
    library.extend([packages, symbols, devicesets])

    uses_slots = native_slots and any(
        _is_slot(pad) and not pad.is_smd for part in parts for pad in part.package.pads
    )
    version = EAGLE_VERSION_SLOTS if uses_slots else EAGLE_VERSION
    eagle = ET.Element("eagle", {"version": version})
    drawing = ET.SubElement(eagle, "drawing")
    settings = ET.SubElement(drawing, "settings")
    ET.SubElement(settings, "setting", {"alwaysvectorfont": "no"})
    ET.SubElement(settings, "setting", {"verticaltext": "up"})
    drawing.append(_build_grid())
    drawing.append(_build_layers())
    drawing.append(library)

    ET.indent(eagle, space="  ")
    body = ET.tostring(eagle, encoding="unicode").replace(" />", "/>")
    text = f"{XML_DECLARATION}\n{DOCTYPE}\n{body}\n"

    issues = [issue for issue in lint_library(text) if issue.severity == "error"]
    if issues:
        raise LintError(f"Generated library failed lint:\n{format_issues(issues)}")
    return text


def _library_description(parts: Sequence[Part], description: str) -> str:
    ids = list(dict.fromkeys(p.lcsc_id for p in parts if p.lcsc_id))
    paragraphs = [description] if description.strip() else []
    if ids:
        paragraphs.append("LCSC parts: " + ", ".join(ids))
    paragraphs.append(ATTRIBUTION)
    return _description_html("lcsc2fusion library", *paragraphs)


def write_library(
    path: str | Path,
    parts: Sequence[Part],
    description: str = "",
    *,
    native_slots: bool = True,
) -> Path:
    """Write the library as UTF-8 with LF newlines. Creates parent directories."""
    text = build_library(parts, description=description, native_slots=native_slots)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")
    return target
