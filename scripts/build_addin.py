"""Build the Fusion add-in package (stdlib only).

Produces `dist/LCSC2Fusion/` (the add-in folder plus a copy of the `lcsc2fusion/` core package,
D-009) and `dist/LCSC2Fusion.zip` with that folder as the zip root. The zip is deterministic:
sorted members, fixed timestamps and permissions.

    python scripts/build_addin.py [--dist DIR]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

ADDIN_NAME = "LCSC2Fusion"
CORE_NAME = "lcsc2fusion"
ROOT = Path(__file__).resolve().parents[1]

EXCLUDED_DIRS = frozenset(
    {"__pycache__", "tests", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".git"}
)
EXCLUDED_DIR_SUFFIXES = (".egg-info",)
EXCLUDED_FILE_SUFFIXES = (".pyc", ".pyo")
EXCLUDED_FILE_NAMES = frozenset({".DS_Store", "Thumbs.db"})

REQUIRED_MANIFEST_KEYS = (
    "autodeskProduct",
    "type",
    "author",
    "description",
    "version",
    "runOnStartup",
    "supportedOS",
    "editEnabled",
)
ZIP_TIMESTAMP = (2020, 1, 1, 0, 0, 0)
ZIP_FILE_MODE = 0o644 << 16
ZIP_CREATOR_UNIX = 3

INSTALL_HINT = "\n".join(
    [
        r"Install: extract the zip into %APPDATA%\Autodesk\FusionAddins\ so that",
        r"  %APPDATA%\Autodesk\FusionAddins\LCSC2Fusion\LCSC2Fusion.manifest exists.",
        "  Then in Fusion open Utilities > Add-Ins > Scripts and Add-Ins (Shift+S), Add-Ins tab,",
        "  and run LCSC2Fusion. Alternative: the green plus there, then pick the extracted folder.",
    ]
)


class BuildError(Exception):
    """The add-in sources are inconsistent. The message says what to fix."""


@dataclass(frozen=True)
class BuildResult:
    folder: Path
    zip_path: Path
    files: tuple[str, ...]  # posix paths relative to the add-in folder


def validate_addin(addin_dir: Path) -> dict[str, object]:
    """Check folder, .py and .manifest names match and the manifest is sane."""
    name = addin_dir.name
    script = addin_dir / f"{name}.py"
    manifest_path = addin_dir / f"{name}.manifest"
    for required in (script, manifest_path):
        if not required.is_file():
            raise BuildError(
                f"Missing {required.name} in {addin_dir}: "
                "folder, .py and .manifest names must match."
            )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise BuildError(f"{manifest_path.name} is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise BuildError(f"{manifest_path.name} must contain a JSON object.")
    missing = [key for key in REQUIRED_MANIFEST_KEYS if key not in manifest]
    if missing:
        raise BuildError(f"{manifest_path.name} lacks required keys: {', '.join(missing)}")
    if manifest["type"] != "addin":
        raise BuildError(f"{manifest_path.name}: type must be 'addin', got {manifest['type']!r}.")
    return manifest


def _is_excluded(relative: Path) -> bool:
    parts = relative.parts
    if any(part in EXCLUDED_DIRS or part.endswith(EXCLUDED_DIR_SUFFIXES) for part in parts[:-1]):
        return True
    name = parts[-1]
    return name in EXCLUDED_FILE_NAMES or name.endswith(EXCLUDED_FILE_SUFFIXES)


def collect_files(source: Path) -> list[Path]:
    """Files below `source` (relative paths, sorted), without caches, bytecode and tests."""
    found = [
        path.relative_to(source)
        for path in source.rglob("*")
        if path.is_file() and not _is_excluded(path.relative_to(source))
    ]
    return sorted(found, key=lambda p: p.as_posix())


def _copy_tree(source: Path, target: Path) -> None:
    for relative in collect_files(source):
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, destination)


def _write_zip(folder: Path, zip_path: Path) -> None:
    members = collect_files(folder)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative in members:
            info = zipfile.ZipInfo(f"{folder.name}/{relative.as_posix()}", ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = ZIP_FILE_MODE
            info.create_system = ZIP_CREATOR_UNIX
            archive.writestr(info, (folder / relative).read_bytes())


def build(root: Path = ROOT, dist: Path | None = None) -> BuildResult:
    """Build the add-in folder and zip under `dist` (default `<root>/dist`)."""
    dist = dist if dist is not None else root / "dist"
    addin_src = root / "addin" / ADDIN_NAME
    core_src = root / CORE_NAME
    if not addin_src.is_dir():
        raise BuildError(f"Add-in sources not found: {addin_src}")
    if not (core_src / "__init__.py").is_file():
        raise BuildError(f"Core package not found: {core_src}")
    validate_addin(addin_src)

    folder = dist / ADDIN_NAME
    zip_path = dist / f"{ADDIN_NAME}.zip"
    if folder.exists():
        shutil.rmtree(folder)
    zip_path.unlink(missing_ok=True)
    folder.mkdir(parents=True)

    _copy_tree(addin_src, folder)
    _copy_tree(core_src, folder / CORE_NAME)
    license_file = root / "LICENSE"
    if license_file.is_file():
        shutil.copyfile(license_file, folder / "LICENSE")

    _write_zip(folder, zip_path)
    files = tuple(path.as_posix() for path in collect_files(folder))
    return BuildResult(folder=folder, zip_path=zip_path, files=files)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the LCSC2Fusion Fusion add-in package.")
    parser.add_argument("--dist", type=Path, default=None, help="output directory (default: dist/)")
    args = parser.parse_args(argv)
    try:
        result = build(dist=args.dist)
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    size_kib = result.zip_path.stat().st_size / 1024
    print(f"Built {result.folder} ({len(result.files)} files)")
    print(f"Built {result.zip_path} ({size_kib:.1f} KiB)")
    print(INSTALL_HINT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
