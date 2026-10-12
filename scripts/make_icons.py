"""Draw the LCSC2Fusion icon and write every raster asset (dev tool, needs Pillow).

The add-in runtime and the build stay stdlib only: the generated files are committed and the
build just copies them. Run this script again only when the design changes.

    python scripts/make_icons.py [--palette-dir DIR] [--resources-dir DIR]

Outputs (the defaults point into addin/LCSC2Fusion):

    palette/favicon.ico    multi image ICO, PNG compressed entries 16, 24, 32, 48, 64, 128, 256.
                           The tab, title bar and taskbar icon of the browser window.
    palette/icon-192.png   web app manifest icon and apple-touch-icon.
    palette/icon-512.png   web app manifest icon.
    resources/16x16.png    Fusion toolbar button icons (same names as before).
    resources/32x32.png
    resources/64x64.png

Design source: addin/LCSC2Fusion/AddInIcon.svg (128 unit viewBox). The numbers of the SVG live in
`FULL` below, ONE definition for every size from 48 px up, and a test compares them with the
SVG. Below 48 px the SVG is not drawn as is: pins 1 to 2 px wide land between pixels and blur,
so 16, 24 and 32 px use hand snapped layouts on the pixel grid. They keep the colours, the
tile, the white chip with its pin 1 dot and the gold pins on four sides, but use bigger chips,
fewer pins and whole pixel edges (16 px: two 1 px pins per side, 24 and 32 px: three 2 px pins).

Rendering: every size is drawn at 8x and shrunk, so the edges are antialiased and nothing depends
on font or system rasterisers. The SVG geometry is shrunk with LANCZOS. The pixel snapped sizes
use BOX (exact area coverage) instead: their lines are 1 to 2 px wide and sit on pixel boundaries,
where the negative lobes of LANCZOS would add halos and smear the pins into their neighbours.
The output is stable: fixed PNG parameters, no text chunks or timestamps, ICO written by hand
with `struct`. No maskable variant is made: the
app only runs as a desktop browser window (Windows and macOS), where `purpose: "maskable"` is
never used, and an extra file would add nothing there.
"""

from __future__ import annotations

import argparse
import io
import struct
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
ADDIN_DIR = ROOT / "addin" / "LCSC2Fusion"
DEFAULT_PALETTE_DIR = ADDIN_DIR / "palette"
DEFAULT_RESOURCES_DIR = ADDIN_DIR / "resources"

SUPERSAMPLE = 8
PNG_COMPRESS_LEVEL = 9

# Colours of AddInIcon.svg (also the accent of palette/style.css).
BLUE = "#1f5ea8"
WHITE = "#ffffff"
GOLD = "#ffc400"

ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
RESOURCE_SIZES = (16, 32, 64)
MANIFEST_SIZES = (192, 512)
ALL_SIZES = tuple(sorted({*ICO_SIZES, *RESOURCE_SIZES, *MANIFEST_SIZES}))


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float


@dataclass(frozen=True)
class Dot:
    cx: float
    cy: float
    r: float


@dataclass(frozen=True)
class Layout:
    """One drawing of the icon on a square grid of `grid` units (rounded tile, chip, pins)."""

    grid: float
    tile: Rect
    tile_radius: float
    chip: Rect
    chip_radius: float
    dot: Dot
    pins: tuple[Rect, ...]


def _pins(chip: Rect, length: float, thickness: float, starts: Sequence[float]) -> tuple[Rect, ...]:
    """Pins on the four sides of a square chip: left, right, top, bottom, in that order.

    `starts` are the offsets of the pins along a side (the chip is square, so one list serves
    both axes). A pin touches the chip edge and reaches `length` outwards.
    """
    left = [Rect(chip.x - length, s, length, thickness) for s in starts]
    right = [Rect(chip.x + chip.w, s, length, thickness) for s in starts]
    top = [Rect(s, chip.y - length, thickness, length) for s in starts]
    bottom = [Rect(s, chip.y + chip.h, thickness, length) for s in starts]
    return (*left, *right, *top, *bottom)


_FULL_CHIP = Rect(35, 35, 58, 58)

# The numbers of AddInIcon.svg, in the order of its elements (tests/test_addin_icons.py checks).
FULL = Layout(
    grid=128,
    tile=Rect(2.5, 2.5, 123, 123),
    tile_radius=28,
    chip=_FULL_CHIP,
    chip_radius=6,
    dot=Dot(46, 46, 5),
    pins=_pins(_FULL_CHIP, length=18, thickness=9, starts=(40, 59.5, 79)),
)

_CHIP_16 = Rect(4, 4, 8, 8)
_CHIP_24 = Rect(7, 7, 10, 10)
_CHIP_32 = Rect(9, 9, 14, 14)

# Pixel snapped drawings, the grid equals the pixel size so every edge sits on a pixel boundary.
SMALL_LAYOUTS: dict[int, Layout] = {
    16: Layout(
        grid=16,
        tile=Rect(0, 0, 16, 16),
        tile_radius=3.5,
        chip=_CHIP_16,
        chip_radius=0.75,
        dot=Dot(5.5, 5.5, 0.6),
        pins=_pins(_CHIP_16, length=2, thickness=1, starts=(5, 10)),
    ),
    24: Layout(
        grid=24,
        tile=Rect(0, 0, 24, 24),
        tile_radius=5,
        chip=_CHIP_24,
        chip_radius=1,
        dot=Dot(9, 9, 1),
        pins=_pins(_CHIP_24, length=3, thickness=2, starts=(8, 11, 14)),
    ),
    32: Layout(
        grid=32,
        tile=Rect(0, 0, 32, 32),
        tile_radius=7,
        chip=_CHIP_32,
        chip_radius=1.5,
        dot=Dot(12, 12, 1.25),
        pins=_pins(_CHIP_32, length=5, thickness=2, starts=(10, 15, 20)),
    ),
}


def layout_for(size: int) -> Layout:
    """The hand snapped drawing for the tiny sizes, the SVG geometry for everything else."""
    return SMALL_LAYOUTS.get(size, FULL)


# Rendering ----------------------------------------------------------------------------------


def _box(rect: Rect, scale: float) -> tuple[int, int, int, int]:
    """Inclusive pixel box of `rect` on a canvas where one unit is `scale` pixels."""
    x0 = round(rect.x * scale)
    y0 = round(rect.y * scale)
    x1 = round((rect.x + rect.w) * scale) - 1
    y1 = round((rect.y + rect.h) * scale) - 1
    return x0, y0, x1, y1


def render_icon(size: int) -> Image.Image:
    """The icon as an RGBA image of `size` x `size` pixels (transparent outside the tile)."""
    from PIL import Image, ImageDraw

    layout = layout_for(size)
    big = size * SUPERSAMPLE
    scale = big / layout.grid
    canvas = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(_box(layout.tile, scale), radius=layout.tile_radius * scale, fill=BLUE)
    for pin in layout.pins:
        draw.rectangle(_box(pin, scale), fill=GOLD)
    draw.rounded_rectangle(_box(layout.chip, scale), radius=layout.chip_radius * scale, fill=WHITE)
    dot = layout.dot
    draw.ellipse(_box(Rect(dot.cx - dot.r, dot.cy - dot.r, 2 * dot.r, 2 * dot.r), scale), fill=BLUE)
    resample = Image.Resampling.BOX if size in SMALL_LAYOUTS else Image.Resampling.LANCZOS
    return canvas.resize((size, size), resample)


def png_bytes(size: int) -> bytes:
    """PNG file content of the icon: fixed parameters and no metadata, so it is reproducible."""
    buffer = io.BytesIO()
    render_icon(size).save(buffer, format="PNG", compress_level=PNG_COMPRESS_LEVEL, optimize=False)
    return buffer.getvalue()


def build_ico(entries: Sequence[tuple[int, bytes]]) -> bytes:
    """ICO file: header, directory, then the PNG blobs. 256 is stored as 0 in the directory."""
    header = struct.pack("<HHH", 0, 1, len(entries))
    offset = len(header) + 16 * len(entries)
    directory = b""
    blobs = b""
    for size, png in entries:
        dimension = 0 if size >= 256 else size
        directory += struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(png), offset)
        blobs += png
        offset += len(png)
    return header + directory + blobs


def generate(palette_dir: Path, resources_dir: Path) -> list[Path]:
    """Write every asset and return the paths. Directories are created when missing."""
    palette_dir.mkdir(parents=True, exist_ok=True)
    resources_dir.mkdir(parents=True, exist_ok=True)
    cache: dict[int, bytes] = {size: png_bytes(size) for size in ALL_SIZES}
    written: list[Path] = []

    def write(path: Path, content: bytes) -> None:
        path.write_bytes(content)
        written.append(path)

    write(palette_dir / "favicon.ico", build_ico([(size, cache[size]) for size in ICO_SIZES]))
    for size in MANIFEST_SIZES:
        write(palette_dir / f"icon-{size}.png", cache[size])
    for size in RESOURCE_SIZES:
        write(resources_dir / f"{size}x{size}.png", cache[size])
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the LCSC2Fusion icon files.")
    parser.add_argument("--palette-dir", type=Path, default=DEFAULT_PALETTE_DIR)
    parser.add_argument("--resources-dir", type=Path, default=DEFAULT_RESOURCES_DIR)
    args = parser.parse_args(argv)
    try:
        written = generate(args.palette_dir, args.resources_dir)
    except ImportError:
        print("error: Pillow is required (a dev tool only): pip install pillow", file=sys.stderr)
        return 2
    for path in written:
        print(f"wrote {path} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
