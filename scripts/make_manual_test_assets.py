"""Build the files used by docs/MANUAL_TEST.md.

Writes docs/manual_test/synthetic_all.lbr (every synthetic fixture in one library, so a single
import in Fusion exercises arcs, oblique pad rotation, slots, polygon pads, duplicate pad numbers
and multi-unit symbols) and two small libraries that differ only in how slots are written.
Also renders every footprint and symbol into out/manual_test/ to compare with what Fusion shows.
The renders are not committed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lcsc2fusion.lbr_writer import write_library  # noqa: E402
from lcsc2fusion.parser.part import parse_part  # noqa: E402
from lcsc2fusion.render import render_part  # noqa: E402

SYNTHETIC = ROOT / "tests" / "fixtures" / "synthetic"
LIBRARY = ROOT / "docs" / "manual_test" / "synthetic_all.lbr"
SLOTS_NATIVE = ROOT / "docs" / "manual_test" / "slots_native.lbr"
SLOTS_MILLING = ROOT / "docs" / "manual_test" / "slots_milling.lbr"
RENDERS = ROOT / "out" / "manual_test"


def main() -> int:
    parts = [
        parse_part(json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(SYNTHETIC.glob("*.json"))
        if not path.name.endswith(".expected.json")
    ]
    write_library(LIBRARY, parts, description="lcsc2fusion synthetic manual test library")
    print(f"Wrote {LIBRARY} ({len(parts)} parts)")
    # The same USB-C part twice: slots as Fusion style slot pads (format 9.7.0) and as a drilled
    # hole plus a Milling layer outline (format 9.6.2). Import both and see which one Fusion takes.
    usb = [part for part in parts if part.mpn == "SYN-USBC-16P"]
    write_library(SLOTS_NATIVE, usb, description="slots as native slot pads", native_slots=True)
    write_library(SLOTS_MILLING, usb, description="slots as milling outline", native_slots=False)
    print(f"Wrote {SLOTS_NATIVE} and {SLOTS_MILLING}")
    RENDERS.mkdir(parents=True, exist_ok=True)
    count = 0
    for part in parts:
        count += len(render_part(part, RENDERS))
    print(f"Wrote {count} renders to {RENDERS}")
    for part in parts:
        print(f"  {part.lcsc_id}  {part.mpn}  ({part.package.name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
