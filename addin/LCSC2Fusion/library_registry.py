"""Registry of what the accumulating "LCSC2Fusion Library" already holds (no `adsk` import).

The add-in keeps ONE Electronics library in the user's Fusion project and merges every converted
part into it. Merging with the Eagle `COPY` command replaces anything of the same name, so before
a part is merged we must know which footprints (packages) the library has and what they look
like. This module keeps that knowledge in a small JSON file and offers three tools on top of it:

* `StateStore`: load and save the registry (`default_state_path()` is the location).
* `make_part_transform`: a hook for `lcsc2fusion.service.convert_parts(part_transform=...)` that
  renames a footprint whose name is taken by a DIFFERENT footprint in the library, so the .lbr is
  written with a clean name and `COPY` can never overwrite someone else's geometry.
* `inspect_part_lbr`: reads a written part .lbr and reports the deviceset name (needed by the
  merge command) and the package names with their fingerprints.

Fingerprints come from `lcsc2fusion.lbr_writer` (`package_fingerprint` for a model package,
`package_element_fingerprint` for a `<package>` element read from a file). Both ignore the package
name and description, so equal geometry means equal fingerprint whatever the name is.

Package names are kept in the form the writer produces (upper case, see `sanitize_name`) and are
compared case-insensitively, like Eagle does.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from lcsc2fusion.errors import ConversionError, LcscError
from lcsc2fusion.lbr_writer.writer import (
    package_element_fingerprint,
    package_fingerprint,
    sanitize_name,
)
from lcsc2fusion.model import Part

STATE_VERSION = 1
STATE_FILE_NAME = "state.json"
STATE_ENV_VAR = "LCSC2FUSION_STATE"  # full path of the state file, overrides the default
_APP_DIR_NAME = "LCSC2Fusion"

# Renamed packages get this many leading fingerprint characters as a suffix. If that name is taken
# by yet another shape, more characters are used, up to the whole fingerprint.
SUFFIX_MIN_CHARS = 6


class PartLbrError(LcscError):
    """A part .lbr is missing or not what the writer produces. The message is user readable."""


# ----- state ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class PartRecord:
    """One part merged into the library. `added_at` is seconds since the epoch."""

    lcsc_id: str
    deviceset: str
    package: str
    fingerprint: str
    added_at: float


@dataclass(frozen=True)
class LibraryState:
    """What is known about the library. Treat the dicts as read only: use the `with_*` methods.

    `library_file_id` and `library_name` identify the Fusion document (None before the first
    part). `parts` maps the normalised LCSC id to its record, `packages` maps every package name
    in the library to its fingerprint.
    """

    library_file_id: str | None = None
    library_name: str | None = None
    parts: dict[str, PartRecord] = field(default_factory=dict)
    packages: dict[str, str] = field(default_factory=dict)

    def has_part(self, lcsc_id: str) -> bool:
        return normalize_id(lcsc_id) in self.parts

    def with_library(self, file_id: str, name: str) -> LibraryState:
        return dataclasses.replace(self, library_file_id=file_id, library_name=name)

    def with_part(self, record: PartRecord, packages: Mapping[str, str]) -> LibraryState:
        """Copy with `record` added and the package fingerprints merged in."""
        parts = {**self.parts, normalize_id(record.lcsc_id): record}
        return dataclasses.replace(self, parts=parts, packages={**self.packages, **packages})

    def without_contents(self) -> LibraryState:
        """Copy that keeps nothing: used when the library it described no longer exists."""
        return LibraryState()


def normalize_id(lcsc_id: str) -> str:
    return lcsc_id.strip().upper()


def default_state_path(
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: Path | None = None,
) -> Path:
    """`%LOCALAPPDATA%\\LCSC2Fusion\\state.json` on Windows, the usual per-OS place elsewhere.

    The arguments exist so tests do not touch the real environment. `LCSC2FUSION_STATE` (a full
    file path) overrides everything.
    """
    env = os.environ if environ is None else environ
    override = env.get(STATE_ENV_VAR)
    if override:
        return Path(override)
    system = sys.platform if platform is None else platform
    home_dir = Path.home() if home is None else home
    if system.startswith("win"):
        local = env.get("LOCALAPPDATA")
        base = Path(local) if local else home_dir / "AppData" / "Local"
        return base / _APP_DIR_NAME / STATE_FILE_NAME
    if system == "darwin":
        return home_dir / "Library" / "Application Support" / _APP_DIR_NAME / STATE_FILE_NAME
    state_home = env.get("XDG_STATE_HOME")
    base = Path(state_home) if state_home else home_dir / ".local" / "state"
    return base / _APP_DIR_NAME.lower() / STATE_FILE_NAME


class StateStore:
    """The state file. `load` never raises, `save` is atomic and raises OSError on failure.

    A missing file is an empty state. A file that cannot be read or does not look like a state
    file also gives an empty state, and `warnings` (valid until the next `load`) says why, so the
    caller can tell the user. Single records that are malformed are dropped with a warning and the
    rest is kept.
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._warnings: tuple[str, ...] = ()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def warnings(self) -> tuple[str, ...]:
        """Problems found by the last `load` (empty when the file was fine or absent)."""
        return self._warnings

    def load(self) -> LibraryState:
        self._warnings = ()
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return LibraryState()
        except (OSError, UnicodeDecodeError) as exc:
            return self._unusable(f"cannot be read ({exc})")
        try:
            raw = json.loads(text)
        except ValueError as exc:
            return self._unusable(f"is not valid JSON ({exc})")
        problems: list[str] = []
        state = _state_from_json(raw, problems)
        if state is None:
            return self._unusable(problems[0] if problems else "has an unexpected structure")
        self._warnings = tuple(f"{self._path}: {problem}" for problem in problems)
        return state

    def save(self, state: LibraryState) -> None:
        """Write the whole state atomically (temporary file in the same folder, then replace)."""
        text = json.dumps(_state_to_json(state), indent=2, sort_keys=True, ensure_ascii=True)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(dir=self._path.parent, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp_name)
            raise

    def _unusable(self, reason: str) -> LibraryState:
        self._warnings = (
            f"The library registry {self._path} {reason}; starting with an empty registry.",
        )
        return LibraryState()


def forget_library(store: StateStore) -> None:
    """Start over: forget the stored library id, name and parts. Raises OSError if unwritable.

    The Fusion document is not touched. The next part looks for a file called like the library
    again: if the user deleted or renamed the document a new library is created, otherwise the
    existing one is found and the part is merged into it without the registry knowing its content.
    """
    store.save(LibraryState())


def _state_to_json(state: LibraryState) -> dict[str, object]:
    return {
        "version": STATE_VERSION,
        "library_file_id": state.library_file_id,
        "library_name": state.library_name,
        "parts": {key: dataclasses.asdict(record) for key, record in sorted(state.parts.items())},
        "packages": dict(sorted(state.packages.items())),
    }


def _state_from_json(raw: object, problems: list[str]) -> LibraryState | None:
    """The state in `raw`, or None (and the reason in `problems`) when it is not a state file."""
    if not isinstance(raw, dict):
        problems.append("does not contain a JSON object")
        return None
    version = raw.get("version")
    if version != STATE_VERSION:
        problems.append(f"has version {version!r}, this add-in understands {STATE_VERSION}")
        return None
    file_id = _optional_str(raw.get("library_file_id"))
    name = _optional_str(raw.get("library_name"))
    parts: dict[str, PartRecord] = {}
    raw_parts = raw.get("parts")
    for key, value in (raw_parts if isinstance(raw_parts, dict) else {}).items():
        record = _record_from_json(value)
        if record is None:
            problems.append(f"dropped the malformed record for {key!r}")
        else:
            parts[normalize_id(record.lcsc_id)] = record
    packages: dict[str, str] = {}
    raw_packages = raw.get("packages")
    for key, value in (raw_packages if isinstance(raw_packages, dict) else {}).items():
        if isinstance(key, str) and isinstance(value, str) and key and value:
            packages[key] = value
        else:
            problems.append(f"dropped the malformed package entry {key!r}")
    return LibraryState(file_id, name, parts, packages)


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _record_from_json(value: object) -> PartRecord | None:
    if not isinstance(value, dict):
        return None
    lcsc_id, deviceset = value.get("lcsc_id"), value.get("deviceset")
    package, fingerprint = value.get("package"), value.get("fingerprint")
    added_at = value.get("added_at")
    if not (
        isinstance(lcsc_id, str)
        and lcsc_id
        and isinstance(deviceset, str)
        and isinstance(package, str)
        and isinstance(fingerprint, str)
        and isinstance(added_at, int | float)
        and not isinstance(added_at, bool)
    ):
        return None
    return PartRecord(lcsc_id, deviceset, package, fingerprint, float(added_at))


# ----- package names --------------------------------------------------------------------------


def resolve_package_name(state: LibraryState, name: str, fingerprint: str) -> str:
    """The name under which a package with `fingerprint` may enter the library.

    `name` itself when no package of that name is in the library or the one there has the same
    fingerprint (it is the same footprint, `COPY` will just rewrite identical data). Otherwise
    `<name>_<first 6 fingerprint characters, upper case>`, with more characters when even that is
    taken by a different shape. The suffix is upper case because the writer upper-cases names.
    """
    known = {existing.upper(): value for existing, value in state.packages.items()}

    def usable(candidate: str) -> bool:
        return known.get(candidate.upper(), fingerprint) == fingerprint

    if usable(name):
        return name
    for length in range(min(SUFFIX_MIN_CHARS, len(fingerprint)), len(fingerprint) + 1):
        candidate = f"{name}_{fingerprint[:length].upper()}"
        if usable(candidate):
            return candidate
    counter = 2  # only reachable with fingerprints that are not hashes
    while not usable(f"{name}_{fingerprint.upper()}_{counter}"):
        counter += 1
    return f"{name}_{fingerprint.upper()}_{counter}"


def make_part_transform(state: LibraryState) -> Callable[[Part], Part]:
    """Hook for `convert_parts(part_transform=...)`: renames clashing footprints, nothing else.

    A package that is not in the library, or is there with identical geometry, keeps its name (the
    very same `Part` object is returned). One that clashes gets the name `resolve_package_name`
    chooses. Parts converted by the same transform also see each other's names, so two different
    footprints of one batch do not end up under one name. A package the writer cannot write is
    left alone: the writer reports that with the proper context.
    """
    taken: dict[str, str] = {}  # names handed out by this transform

    def transform(part: Part) -> Part:
        try:
            fingerprint = package_fingerprint(part.package)
        except ConversionError:
            return part
        base = sanitize_name(part.package.name, "PACKAGE")
        view = dataclasses.replace(state, packages={**state.packages, **taken})
        resolved = resolve_package_name(view, base, fingerprint)
        taken[resolved] = fingerprint
        if resolved == base:
            return part
        return dataclasses.replace(part, package=dataclasses.replace(part.package, name=resolved))

    return transform


# ----- reading a written part library ---------------------------------------------------------


@dataclass(frozen=True)
class PartLbrInfo:
    """What the merge needs to know about a part .lbr.

    `deviceset` is the name of the first `<deviceset>` (the Eagle `COPY` command takes it),
    `packages` maps each package name in the file to its fingerprint (see the module docstring),
    `symbols` lists the symbol names.
    """

    deviceset: str
    packages: dict[str, str]
    symbols: tuple[str, ...]


def inspect_part_lbr(path: Path | str) -> PartLbrInfo:
    """Read a .lbr written by `write_library`. Raises PartLbrError when it is not usable."""
    source = Path(path)
    try:
        root = ET.parse(source).getroot()
    except FileNotFoundError as exc:
        raise PartLbrError(f"The part library file does not exist: {source}") from exc
    except (OSError, ET.ParseError) as exc:
        raise PartLbrError(f"The part library {source.name} cannot be read: {exc}") from exc
    library = root.find("./drawing/library")
    if library is None:
        raise PartLbrError(f"{source.name} is not an Eagle library (no <library> element)")
    deviceset = library.find("./devicesets/deviceset")
    deviceset_name = "" if deviceset is None else deviceset.get("name", "")
    if not deviceset_name:
        raise PartLbrError(f"{source.name} has no named <deviceset>")
    packages: dict[str, str] = {}
    for package in library.findall("./packages/package"):
        package_name = package.get("name", "")
        if not package_name:
            raise PartLbrError(f"{source.name} has a <package> without a name")
        packages[package_name] = package_element_fingerprint(package)
    symbols = tuple(symbol.get("name", "") for symbol in library.findall("./symbols/symbol"))
    return PartLbrInfo(deviceset=deviceset_name, packages=packages, symbols=symbols)
