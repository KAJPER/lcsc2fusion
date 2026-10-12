"""Assemble a full Part (symbols + package + metadata) from an EasyEDA component document."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lcsc2fusion.errors import ParseError, PartNotFoundError
from lcsc2fusion.model import Part
from lcsc2fusion.parser.footprint import parse_footprint
from lcsc2fusion.parser.model3d import parse_model3d
from lcsc2fusion.parser.symbol import parse_symbol_units, symbol_prefix
from lcsc2fusion.parser.symbol_fit import fit_symbol


def _mapping(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ParseError(f"Expected an object for {what}, got {type(value).__name__}")
    return value


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def parse_part(doc: Mapping[str, Any], *, fit_symbols: bool = True) -> Part:
    """Parse the JSON returned by the EasyEDA component endpoint into a Part.

    Accepts the top level response (`{"success": ..., "result": {...}}`) or the bare `result`.
    With `fit_symbols` (default) every symbol body is enlarged where needed so the pin names
    Eagle draws inside it do not overlap (see parser/symbol_fit.py); pass False to keep the
    source geometry exactly.
    """
    doc = _mapping(doc, "component document")
    if doc.get("success") is False:
        raise PartNotFoundError(str(doc.get("message") or "Component not found"))
    result = _mapping(doc.get("result", doc), "result")

    data_str = result.get("dataStr")
    if not isinstance(data_str, Mapping):
        raise ParseError("Component has no symbol data (dataStr)")
    package_detail = result.get("packageDetail")
    if not isinstance(package_detail, Mapping):
        raise ParseError("Component has no footprint data (packageDetail)")

    head = data_str.get("head")
    c_para: Mapping[str, Any] = {}
    if isinstance(head, Mapping) and isinstance(head.get("c_para"), Mapping):
        c_para = head["c_para"]

    lcsc = result.get("lcsc")
    lcsc_id = _text(lcsc.get("number")) if isinstance(lcsc, Mapping) else ""
    lcsc_id = lcsc_id or _text(c_para.get("Supplier Part"))

    mpn = _text(c_para.get("Manufacturer Part")) or _text(result.get("title")) or lcsc_id
    manufacturer = _text(c_para.get("Manufacturer"))
    description = _text(result.get("description"))
    base_name = mpn or lcsc_id or "PART"

    symbols, symbol_warnings = parse_symbol_units(result, base_name=base_name)
    if fit_symbols:
        fitted = [fit_symbol(symbol) for symbol in symbols]
        symbols = [symbol for symbol, _ in fitted]
        symbol_warnings = symbol_warnings + [note for _, notes in fitted for note in notes]
    package, package_warnings = parse_footprint(package_detail)
    model3d, model_warnings = parse_model3d(package_detail)

    attributes: list[tuple[str, str]] = []
    for key, name in (
        ("package", "LCSC_PACKAGE"),
        ("Value", "LCSC_VALUE"),
        ("JLCPCB Part Class", "JLCPCB_CLASS"),
    ):
        value = _text(c_para.get(key))
        if value:
            attributes.append((name, value))

    return Part(
        lcsc_id=lcsc_id,
        mpn=mpn,
        manufacturer=manufacturer,
        description=description,
        prefix=symbol_prefix(result),
        symbols=tuple(symbols),
        package=package,
        datasheet=_text(c_para.get("link")),
        source_uuid=_text(result.get("uuid")),
        attributes=tuple(attributes),
        warnings=tuple(symbol_warnings + package_warnings + model_warnings),
        model3d=model3d,
    )
