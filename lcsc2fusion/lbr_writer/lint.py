"""Fusion-compatibility linter for Eagle .lbr text (docs/RESEARCH.md 2.6, decision D-004).

The official eagle.dtd rejects real files saved by Fusion, so it is not usable as a gate. This
linter works on ANY .lbr text, including files exported by Fusion: it does not require our own
layer set (only that every layer in use is declared) and it ignores elements it does not know.
It never raises on bad input, every problem becomes a `LintIssue`.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

Severity = Literal["error", "warning"]


@dataclass(frozen=True)
class LintIssue:
    severity: Severity
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.severity.upper()} [{self.code}] {self.message}"


def format_issues(issues: Sequence[LintIssue]) -> str:
    """One issue per line, in the given order."""
    return "\n".join(str(issue) for issue in issues)


# Order of the direct children of <library> in the DTD.
_LIBRARY_ORDER = ("description", "packages", "packages3d", "symbols", "devicesets")

_PIN_VISIBLE = frozenset({"off", "pad", "pin", "both"})
_PIN_LENGTH = frozenset({"point", "short", "middle", "long"})
_PIN_DIRECTION = frozenset({"nc", "in", "out", "io", "oc", "pwr", "pas", "hiz", "sup"})
# "none" is valid per DTD but the Fusion loader rejects it, so it gets its own error.
_PIN_FUNCTION = frozenset({"none", "dot", "clk", "dotclk"})
_PAD_SHAPE = frozenset({"square", "round", "octagon", "long", "offset"})
_TEXT_ALIGN = frozenset(
    {
        "bottom-left",
        "bottom-center",
        "bottom-right",
        "center-left",
        "center",
        "center-right",
        "top-left",
        "top-center",
        "top-right",
    }
)

_ROT_RE = re.compile(r"[SM]{0,2}R[0-9]+(\.[0-9]+)?")
_PIN_ROTATIONS = frozenset({0.0, 90.0, 180.0, 270.0})

# Elements whose `layer` attribute must point to a layer declared in <layers>.
_LAYER_USERS = frozenset({"wire", "circle", "rectangle", "polygon", "text", "smd"})
# Drawable/placeable children of <package> and <symbol> that get the common checks.
_DRAWABLES = frozenset({"wire", "circle", "rectangle", "polygon", "text", "hole", "pad", "smd"})

_NUMERIC_ATTRS: dict[str, tuple[str, ...]] = {
    "wire": ("x1", "y1", "x2", "y2", "width", "curve"),
    "circle": ("x", "y", "radius", "width"),
    "rectangle": ("x1", "y1", "x2", "y2"),
    "polygon": ("width",),
    "vertex": ("x", "y", "curve"),
    "text": ("x", "y", "size", "ratio"),
    "hole": ("x", "y", "drill"),
    "pad": ("x", "y", "drill", "diameter"),
    "smd": ("x", "y", "dx", "dy", "roundness"),
    "pin": ("x", "y"),
    "gate": ("x", "y"),
}

_REQUIRED_ATTRS: dict[str, tuple[str, ...]] = {
    "layer": ("number", "name", "color", "fill"),
    "package": ("name",),
    "symbol": ("name",),
    "deviceset": ("name",),
    "gate": ("name", "symbol", "x", "y"),
    "pad": ("name", "x", "y", "drill"),
    "smd": ("name", "x", "y", "dx", "dy", "layer"),
    "pin": ("name", "x", "y"),
    "wire": ("x1", "y1", "x2", "y2", "width", "layer"),
    "circle": ("x", "y", "radius", "width", "layer"),
    "rectangle": ("x1", "y1", "x2", "y2", "layer"),
    "polygon": ("width", "layer"),
    "vertex": ("x", "y"),
    "text": ("x", "y", "size", "layer"),
    "hole": ("x", "y", "drill"),
    "connect": ("gate", "pin", "pad"),
    "technology": ("name",),
    "attribute": ("name",),
}

# Elements whose `name` must not be empty.
_NON_EMPTY_NAME = frozenset({"package", "symbol", "deviceset", "gate", "pad", "smd", "pin"})


def _sort_key(issue: LintIssue) -> tuple[int, str, str]:
    return (0 if issue.severity == "error" else 1, issue.code, issue.message)


def lint_library(xml_text: str) -> list[LintIssue]:
    """Lint .lbr text. Issues are sorted deterministically: errors first, then code, message."""
    try:
        root = ET.fromstring(xml_text.lstrip("\ufeff"))
    except (ET.ParseError, ValueError) as exc:
        return [LintIssue("error", "XML_MALFORMED", f"Malformed XML: {exc}")]
    linter = _Linter()
    linter.check_root(root)
    return sorted(set(linter.issues), key=_sort_key)


class _Linter:
    def __init__(self) -> None:
        self.issues: list[LintIssue] = []
        self._declared_layers: set[int] = set()
        self._package_pads: dict[str, set[str]] = {}
        self._symbol_pins: dict[str, set[str]] = {}

    # ----- reporting helpers -----

    def _error(self, code: str, message: str) -> None:
        self.issues.append(LintIssue("error", code, message))

    def _warn(self, code: str, message: str) -> None:
        self.issues.append(LintIssue("warning", code, message))

    def _duplicates(self, code: str, scope: str, names: Iterable[str]) -> None:
        """Report names that repeat, compared case-insensitively."""
        seen: set[str] = set()
        for name in names:
            key = name.casefold()
            if key in seen:
                self._error(code, f"{scope}: name '{name}' is used more than once")
            seen.add(key)

    def _number(self, el: ET.Element, attr: str, ctx: str) -> float | None:
        raw = el.get(attr)
        if raw is None:
            return None
        try:
            value = float(raw)
        except ValueError:
            value = math.nan
        if not math.isfinite(value):
            self._error("BAD_NUMBER", f"{ctx}: attribute {attr}='{raw}' is not a finite number")
            return None
        return value

    def _enum(
        self, el: ET.Element, attr: str, allowed: frozenset[str], code: str, ctx: str
    ) -> None:
        value = el.get(attr)
        if value is not None and value not in allowed:
            self._error(code, f"{ctx}: {attr}='{value}' is not one of {', '.join(sorted(allowed))}")

    # ----- structure -----

    def check_root(self, root: ET.Element) -> None:
        if root.tag != "eagle":
            self._error("STRUCT_ROOT", f"Root element is <{root.tag}>, expected <eagle>")
            return
        drawing = root.find("drawing")
        if drawing is None:
            self._error("STRUCT_NO_DRAWING", "<eagle> has no <drawing> element")
            return
        layers = drawing.find("layers")
        if layers is None:
            self._error("STRUCT_NO_LAYERS", "<drawing> has no <layers> element")
        else:
            self._check_layers(layers)
        library = drawing.find("library")
        if library is None:
            self._error("STRUCT_NO_LIBRARY", "<drawing> has no <library> element")
            return
        self._check_library(library)

    def _check_layers(self, layers: ET.Element) -> None:
        numbers: list[str] = []
        for layer in layers.findall("layer"):
            self._check_required(layer, "layers")
            number = layer.get("number")
            if number is None:
                continue
            try:
                self._declared_layers.add(int(number))
            except ValueError:
                self._error("BAD_NUMBER", f"layers: layer number '{number}' is not an integer")
                continue
            numbers.append(number)
        self._duplicates("DUP_LAYER", "layers", numbers)

    def _check_library(self, library: ET.Element) -> None:
        last_index = -1
        for child in library:
            if child.tag not in _LIBRARY_ORDER:
                self._warn("STRUCT_UNKNOWN", f"library: unknown child element <{child.tag}>")
                continue
            index = _LIBRARY_ORDER.index(child.tag)
            if index <= last_index:
                self._error(
                    "STRUCT_ORDER",
                    f"library: <{child.tag}> is out of order or repeated, "
                    f"expected order is {', '.join(_LIBRARY_ORDER)}",
                )
            last_index = max(last_index, index)

        packages = library.find("packages")
        if packages is not None:
            self._check_packages(packages)
        symbols = library.find("symbols")
        if symbols is not None:
            self._check_symbols(symbols)
        devicesets = library.find("devicesets")
        if devicesets is not None:
            self._check_devicesets(devicesets)

    # ----- common element checks -----

    def _check_required(self, el: ET.Element, ctx: str) -> None:
        for attr in _REQUIRED_ATTRS.get(el.tag, ()):
            if attr not in el.attrib:
                self._error("MISSING_ATTR", f"{ctx}: <{el.tag}> is missing attribute '{attr}'")
        if el.tag in _NON_EMPTY_NAME and "name" in el.attrib and not el.get("name", "").strip():
            self._error("EMPTY_NAME", f"{ctx}: <{el.tag}> has an empty name")

    def _check_rot(self, el: ET.Element, ctx: str) -> None:
        rot = el.get("rot")
        if rot is None:
            return
        if _ROT_RE.fullmatch(rot) is None:
            self._error("BAD_ROT", f"{ctx}: rot='{rot}' does not match [SM]R<degrees>")
            return
        if el.tag == "pin":
            angle = float(rot[1:]) if rot.startswith("R") else None
            if angle not in _PIN_ROTATIONS:
                self._error("PIN_ROT", f"{ctx}: pin rot='{rot}' must be R0, R90, R180 or R270")

    def _check_layer_use(self, el: ET.Element, ctx: str) -> None:
        raw = el.get("layer")
        if raw is None:
            return
        try:
            layer = int(raw)
        except ValueError:
            self._error("BAD_NUMBER", f"{ctx}: layer='{raw}' is not an integer")
            return
        if layer not in self._declared_layers:
            self._error(
                "LAYER_UNDECLARED",
                f"{ctx}: <{el.tag}> uses layer {layer} which is not declared in <layers>",
            )

    def _check_common(self, el: ET.Element, ctx: str) -> None:
        self._check_required(el, ctx)
        values = {attr: self._number(el, attr, ctx) for attr in _NUMERIC_ATTRS.get(el.tag, ())}
        self._check_rot(el, ctx)
        if el.tag in _LAYER_USERS:
            self._check_layer_use(el, ctx)
        width = values.get("width")
        if el.tag in ("wire", "circle") and width is not None and width < 0:
            self._error("NEGATIVE_WIDTH", f"{ctx}: <{el.tag}> has negative width {width:g}")

    def _check_drawable(self, el: ET.Element, ctx: str) -> None:
        self._check_common(el, ctx)
        if el.tag == "polygon":
            self._check_polygon(el, ctx)
        elif el.tag == "text":
            self._enum(el, "align", _TEXT_ALIGN, "ENUM_TEXT_ALIGN", ctx)
        elif el.tag == "pad":
            self._enum(el, "shape", _PAD_SHAPE, "ENUM_PAD_SHAPE", ctx)

    def _check_polygon(self, polygon: ET.Element, ctx: str) -> None:
        vertices = polygon.findall("vertex")
        for vertex in vertices:
            self._check_common(vertex, ctx)
        if len(vertices) < 3:
            self._error(
                "POLYGON_VERTICES",
                f"{ctx}: <polygon> has {len(vertices)} vertices, need at least 3",
            )
        width = self._number(polygon, "width", ctx)
        if width is not None and width < 0:
            self._error("NEGATIVE_WIDTH", f"{ctx}: <polygon> has negative width {width:g}")
        elif width == 0:
            self._warn("POLYGON_ZERO_WIDTH", f"{ctx}: <polygon> has width 0")

    # ----- packages -----

    def _check_packages(self, packages: ET.Element) -> None:
        package_elements = packages.findall("package")
        self._duplicates(
            "DUP_PACKAGE",
            "packages",
            (p.get("name", "") for p in package_elements if "name" in p.attrib),
        )
        for package in package_elements:
            self._check_package(package)

    def _check_package(self, package: ET.Element) -> None:
        self._check_required(package, "packages")
        name = package.get("name", "")
        ctx = f"package '{name}'"
        pad_names: list[str] = []
        for child in package:
            if child.tag not in _DRAWABLES:
                continue
            self._check_drawable(child, ctx)
            if child.tag in ("pad", "smd") and "name" in child.attrib:
                pad_name = child.get("name", "")
                pad_names.append(pad_name)
                self._check_ref_name(
                    pad_name,
                    f"{ctx}: pad",
                )
        self._duplicates("DUP_PAD", ctx, pad_names)
        if not pad_names:
            self._warn("PACKAGE_NO_PADS", f"{ctx}: has no pads")
        if "name" in package.attrib:
            self._package_pads[name] = set(pad_names)

    def _check_ref_name(self, name: str, ctx: str) -> None:
        """Pad and pin names must not contain whitespace or semicolons."""
        if any(ch.isspace() or ch == ";" for ch in name):
            self._error("BAD_NAME", f"{ctx} name '{name}' contains whitespace or ';'")

    # ----- symbols -----

    def _check_symbols(self, symbols: ET.Element) -> None:
        symbol_elements = symbols.findall("symbol")
        self._duplicates(
            "DUP_SYMBOL",
            "symbols",
            (s.get("name", "") for s in symbol_elements if "name" in s.attrib),
        )
        for symbol in symbol_elements:
            self._check_symbol(symbol)

    def _check_symbol(self, symbol: ET.Element) -> None:
        self._check_required(symbol, "symbols")
        name = symbol.get("name", "")
        ctx = f"symbol '{name}'"
        pin_names: list[str] = []
        for child in symbol:
            if child.tag == "pin":
                self._check_pin(child, ctx)
                if "name" in child.attrib:
                    pin_names.append(child.get("name", ""))
            elif child.tag in _DRAWABLES:
                self._check_drawable(child, ctx)
        self._duplicates("DUP_PIN", ctx, pin_names)
        if "name" in symbol.attrib:
            self._symbol_pins[name] = set(pin_names)

    def _check_pin(self, pin: ET.Element, ctx: str) -> None:
        self._check_common(pin, ctx)
        pin_ctx = f"{ctx}: pin '{pin.get('name', '')}'"
        self._check_ref_name(pin.get("name", ""), f"{ctx}: pin")
        self._enum(pin, "visible", _PIN_VISIBLE, "ENUM_PIN_VISIBLE", pin_ctx)
        self._enum(pin, "length", _PIN_LENGTH, "ENUM_PIN_LENGTH", pin_ctx)
        self._enum(pin, "direction", _PIN_DIRECTION, "ENUM_PIN_DIRECTION", pin_ctx)
        self._enum(pin, "function", _PIN_FUNCTION, "ENUM_PIN_FUNCTION", pin_ctx)
        if pin.get("function") == "none":
            self._error(
                "PIN_FUNCTION_NONE",
                f"{pin_ctx}: function='none' is rejected by the Fusion loader, omit the attribute",
            )

    # ----- device sets -----

    def _check_devicesets(self, devicesets: ET.Element) -> None:
        elements = devicesets.findall("deviceset")
        self._duplicates(
            "DUP_DEVICESET",
            "devicesets",
            (d.get("name", "") for d in elements if "name" in d.attrib),
        )
        for deviceset in elements:
            self._check_deviceset(deviceset)

    def _check_deviceset(self, deviceset: ET.Element) -> None:
        self._check_required(deviceset, "devicesets")
        ctx = f"deviceset '{deviceset.get('name', '')}'"

        gate_symbols: dict[str, str] = {}
        gates = deviceset.find("gates")
        gate_elements = gates.findall("gate") if gates is not None else []
        if not gate_elements:
            self._error("DEVICESET_NO_GATES", f"{ctx}: has no gates")
        for gate in gate_elements:
            self._check_common(gate, ctx)
            gate_name = gate.get("name")
            symbol_name = gate.get("symbol")
            if gate_name is not None and symbol_name is not None:
                gate_symbols[gate_name] = symbol_name
            if symbol_name is not None and symbol_name not in self._symbol_pins:
                self._error(
                    "GATE_SYMBOL_MISSING",
                    f"{ctx}: gate '{gate_name}' refers to unknown symbol '{symbol_name}'",
                )
        self._duplicates(
            "DUP_GATE", ctx, (g.get("name", "") for g in gate_elements if "name" in g.attrib)
        )

        devices = deviceset.find("devices")
        device_elements = devices.findall("device") if devices is not None else []
        if not device_elements:
            self._error("DEVICESET_NO_DEVICES", f"{ctx}: has no devices")
        self._duplicates("DUP_DEVICE", ctx, (d.get("name", "") for d in device_elements))
        for device in device_elements:
            self._check_device(device, ctx, gate_symbols)

    def _check_device(self, device: ET.Element, ds_ctx: str, gate_symbols: dict[str, str]) -> None:
        ctx = f"{ds_ctx} device '{device.get('name', '')}'"
        package_name = device.get("package")
        package_pads: set[str] = set()
        if package_name is not None:
            if package_name in self._package_pads:
                package_pads = self._package_pads[package_name]
            else:
                self._error(
                    "DEVICE_PACKAGE_MISSING", f"{ctx}: refers to unknown package '{package_name}'"
                )

        connected: set[str] = set()
        connects = device.find("connects")
        for connect in connects.findall("connect") if connects is not None else []:
            self._check_required(connect, ctx)
            gate = connect.get("gate")
            pin = connect.get("pin")
            pad_attr = connect.get("pad")
            where = f"{ctx} connect gate='{gate}' pin='{pin}'"
            symbol_name = None
            if gate is not None:
                symbol_name = gate_symbols.get(gate)
                if symbol_name is None:
                    self._error("CONNECT_GATE", f"{where}: gate does not exist in the deviceset")
            if pin is not None and symbol_name is not None:
                pins = self._symbol_pins.get(symbol_name)
                if pins is not None and pin not in pins:
                    self._error(
                        "CONNECT_PIN", f"{where}: symbol '{symbol_name}' has no pin '{pin}'"
                    )
            if pad_attr is not None:
                pad_list = pad_attr.split()
                if not pad_list:
                    self._error("CONNECT_PAD", f"{where}: pad list is empty")
                for pad in pad_list:
                    if package_name is None:
                        self._error(
                            "CONNECT_PAD", f"{where}: pad '{pad}' but device has no package"
                        )
                    elif package_name in self._package_pads and pad not in package_pads:
                        self._error(
                            "CONNECT_PAD",
                            f"{where}: package '{package_name}' has no pad '{pad}'",
                        )
                    connected.add(pad)

        if package_name is not None and not connected:
            self._warn("DEVICE_NO_CONNECTS", f"{ctx}: no pads are connected to any pin")

        technologies = device.find("technologies")
        for technology in technologies.findall("technology") if technologies is not None else []:
            self._check_required(technology, ctx)
            for attribute in technology.findall("attribute"):
                self._check_required(attribute, ctx)
