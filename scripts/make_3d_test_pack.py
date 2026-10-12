"""Build the 3D test pack for Gate 3 of docs/MANUAL_TEST.md.

For every part this writes the library, the raw STEP, the STEP placed into the package frame and a
third variant, `<id>.placed_yup.step`, which is the placed model turned about the X axis so that
what was Z (up) becomes Y (up). Fusion's package editor may expect a Y up document (SnapEDA and
other tools had to turn the imported model by 90 degrees about X), so the Gate 3 test attaches the
three variants one by one and notes which one sits correctly on the footprint. The variant is only
for that experiment and disappears once the result is known (D-021).

Usage: python scripts/make_3d_test_pack.py [C25804 C2040 ...] [-o out/3d_test]
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lcsc2fusion.constants import ATTRIBUTION  # noqa: E402
from lcsc2fusion.errors import LcscError  # noqa: E402
from lcsc2fusion.service import ConversionResult, convert_parts  # noqa: E402
from lcsc2fusion.step import rotate_x  # noqa: E402

DEFAULT_IDS = ("C25804", "C2040", "C165948")
DEFAULT_OUTPUT = REPO_ROOT / "out" / "3d_test"
YUP_SUFFIX = ".placed_yup.step"
# Rotating by -90 degrees about X maps (x, y, z) to (x, z, -y): the old up axis Z becomes Y.
YUP_ROTATION_DEG = -90.0


def write_yup_variants(result: ConversionResult) -> list[Path]:
    """Write `<id>.placed_yup.step` next to every placed STEP of `result`."""
    written: list[Path] = []
    for model in result.models:
        target = model.placed_step_path.with_name(f"{model.lcsc_id}{YUP_SUFFIX}")
        target.write_bytes(rotate_x(model.placed_step_path.read_bytes(), YUP_ROTATION_DEG))
        written.append(target)
    return written


def build_pack(ids: Sequence[str], out_dir: Path) -> tuple[list[Path], list[str]]:
    """Convert each part to its own library with 3D files; returns (files, warnings)."""
    files: list[Path] = []
    warnings: list[str] = []
    for lcsc_id in ids:
        result = convert_parts([lcsc_id], out_dir, with_3d=True)
        files.append(result.path)
        for model in result.models:
            files.extend((model.step_path, model.placed_step_path, model.info_path))
        files.extend(write_yup_variants(result))
        warnings.extend(result.warnings)
    return files, warnings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the Gate 3 3D test pack.")
    parser.add_argument("ids", nargs="*", default=list(DEFAULT_IDS), help="LCSC part numbers")
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    ids = [part_id.upper() for part_id in args.ids]
    try:
        files, warnings = build_pack(ids, args.output)
    except LcscError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for path in files:
        print(f"Wrote {path}")
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print()
    print("Gate 3, for each part in turn (see docs/MANUAL_TEST.md):")
    print("  1. Open <id>.lbr in Fusion (File > Open > Open from my computer) and save it.")
    print(
        "  2. Content Manager > Footprints > right click the footprint > Attach Existing 3D Model"
    )
    print("     > Select from my computer... and pick a STEP file, name the package, OK.")
    print("  3. Note where the model sits, then repeat with the other variants:")
    print("     <id>.placed.step, <id>.placed_yup.step, <id>.step (raw).")
    print()
    print(ATTRIBUTION)
    print("3D models: personal use only, do not redistribute the STEP files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
