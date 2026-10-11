"""Tokenizer for EasyEDA shape strings ('KIND~f1~f2...', pins also use '^^' sections)."""

from __future__ import annotations

from lcsc2fusion.errors import ParseError


def split_shape(raw: str) -> tuple[str, list[str]]:
    """Split 'KIND~a~b' into ('KIND', ['a', 'b'])."""
    tag, sep, rest = raw.partition("~")
    if not tag:
        raise ParseError(f"Empty shape tag in {raw[:60]!r}")
    return tag, rest.split("~") if sep else []


def split_pin(raw: str) -> list[list[str]]:
    """Split a PIN shape into its '^^' sections, each section split on '~'.

    Section 0 starts with the 'P' tag, so its fields are [tag, show, type, ...].
    """
    return [section.split("~") for section in raw.split("^^")]


def to_float(text: str, default: float = 0.0) -> float:
    """Parse a numeric field; empty string means the default."""
    text = text.strip()
    if not text:
        return default
    try:
        return float(text)
    except ValueError as exc:
        raise ParseError(f"Not a number: {text!r}") from exc


def field(fields: list[str], index: int, default: str = "") -> str:
    """Safe field access: EasyEDA shapes frequently have trailing fields missing."""
    return fields[index] if 0 <= index < len(fields) else default
