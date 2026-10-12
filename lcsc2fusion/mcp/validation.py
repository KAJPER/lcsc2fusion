"""Strict validation of tool arguments and of the output directory.

Everything a model sends is untrusted text. Each check here either returns a clean value of the
right type or raises `InvalidArguments`, which the server turns into a JSON-RPC "invalid params"
error. Nothing is coerced: a string is not a number and a number is not a bool.
"""

from __future__ import annotations

import math
import os
import re
import tempfile
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any

from lcsc2fusion.errors import LcscError

MAX_RAW_TEXT_CHARS = 1000  # hard cap on any string before it is looked at
MAX_QUERY_CHARS = 100
MAX_OUT_DIR_CHARS = 1024
MAX_ID_INPUT_CHARS = 64

_LCSC_ID_RE = re.compile(r"C[0-9]{1,12}")
_UNC_PREFIXES = ("\\\\", "//")


class InvalidArguments(LcscError):
    """The tool arguments break the input schema. The message tells the model how to fix them."""


class OutDirError(LcscError):
    """The output directory cannot be created or written to (an environment problem)."""


def reject_unknown(arguments: Mapping[str, Any], allowed: Collection[str]) -> None:
    unknown = sorted(set(arguments) - set(allowed))
    if unknown:
        raise InvalidArguments(
            f"Unknown argument(s): {', '.join(unknown)}. Allowed: {', '.join(sorted(allowed))}."
        )


def require(arguments: Mapping[str, Any], name: str) -> Any:
    if name not in arguments:
        raise InvalidArguments(f"Missing required argument '{name}'.")
    return arguments[name]


def as_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise InvalidArguments(f"'{name}' must be true or false.")
    return value


def as_int(value: object, name: str, low: int, high: int) -> int:
    """An integer in [low, high]. A float with an integral value (5.0) counts as 5, like JSON."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise InvalidArguments(f"'{name}' must be an integer between {low} and {high}.")
    if isinstance(value, float) and not (math.isfinite(value) and value.is_integer()):
        raise InvalidArguments(f"'{name}' must be an integer between {low} and {high}.")
    number = int(value)
    if not low <= number <= high:
        raise InvalidArguments(f"'{name}' must be between {low} and {high}, got {number}.")
    return number


def as_number(value: object, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise InvalidArguments(f"'{name}' must be a number between {low:g} and {high:g}.")
    number = float(value)
    if not low <= number <= high:
        raise InvalidArguments(f"'{name}' must be between {low:g} and {high:g}, got {number:g}.")
    return number


def as_text(value: object, name: str) -> str:
    """A string without NUL bytes that can be encoded as UTF-8 (no lone surrogates)."""
    if not isinstance(value, str):
        raise InvalidArguments(f"'{name}' must be a string.")
    if len(value) > MAX_RAW_TEXT_CHARS:
        raise InvalidArguments(f"'{name}' is too long.")
    if "\x00" in value:
        raise InvalidArguments(f"'{name}' must not contain NUL characters.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidArguments(f"'{name}' is not valid Unicode text.") from None
    return value


def as_query(value: object, name: str = "query") -> str:
    """Search text: whitespace collapsed, 1 to 100 characters, no control characters."""
    text = " ".join(as_text(value, name).split())
    if not text:
        raise InvalidArguments(f"'{name}' must not be empty.")
    if len(text) > MAX_QUERY_CHARS:
        raise InvalidArguments(
            f"'{name}' is too long ({len(text)} characters, max {MAX_QUERY_CHARS})."
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise InvalidArguments(f"'{name}' must not contain control characters.")
    return text


def as_lcsc_id(value: object, name: str = "lcsc_id") -> str:
    """Canonical LCSC number: the letter C and 1 to 12 digits, upper case (c2040 gives C2040)."""
    if not isinstance(value, str) or len(value) > MAX_ID_INPUT_CHARS:
        raise InvalidArguments(f"'{name}' must be an LCSC part number like C2040.")
    candidate = value.strip().upper()
    if _LCSC_ID_RE.fullmatch(candidate) is None:
        raise InvalidArguments(
            f"'{name}' must be an LCSC part number: the letter C followed by 1 to 12 digits, "
            "for example C2040. Use lcsc_search to find the number of a part."
        )
    return candidate


# Output directory


def resolve_out_dir(raw: object, default: Path) -> Path:
    """The directory to write to, as an absolute normalised path. Nothing is created yet.

    `raw` must be a non-empty absolute path or start with `~` (null is not accepted; the caller
    leaves the argument out to get `default`, which only appears in the error message here).
    UNC and device paths (two leading slashes or backslashes) are refused, because writing to a
    network share can leak the Windows login to that host, and so are relative paths, whose
    base would be the working directory of the server.
    """
    text = as_text(raw, "out_dir").strip()
    if not text:
        raise InvalidArguments("'out_dir' must not be empty. Leave it out to use the default.")
    if len(text) > MAX_OUT_DIR_CHARS:
        raise InvalidArguments("'out_dir' is too long.")
    if text.startswith(_UNC_PREFIXES):
        raise InvalidArguments(
            "'out_dir' must be a local path, network and device paths are refused."
        )
    try:
        path = Path(text).expanduser()
    except RuntimeError:
        raise InvalidArguments(
            "'out_dir' starts with ~ but the home directory is unknown."
        ) from None
    if not path.is_absolute():
        raise InvalidArguments(
            f"'out_dir' must be an absolute path (or start with ~). Leave it out to use {default}."
        )
    return Path(os.path.normpath(path))


def prepare_out_dir(path: Path) -> Path:
    """Create `path` if needed and prove that a file can be written there."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        if path.is_file():
            raise OutDirError(f"'{path}' is a file, not a directory.") from None
        raise OutDirError(f"Cannot create the directory '{path}': {_reason(exc)}.") from None
    try:
        with tempfile.TemporaryFile(dir=path):
            pass
    except OSError as exc:
        raise OutDirError(f"Cannot write to the directory '{path}': {_reason(exc)}.") from None
    return path


def _reason(exc: OSError) -> str:
    return exc.strerror or type(exc).__name__
