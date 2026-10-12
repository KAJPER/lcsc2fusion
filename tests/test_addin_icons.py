"""Tests of the icon of the add-in's browser window: generator, committed assets, manifest, page.

Layers, all offline and without Fusion:

* scripts/make_icons.py (dev tool, needs Pillow): geometry equals AddInIcon.svg, the pixel snapped
  tiny layouts are symmetric, the output is reproducible and equals the committed files.
* the committed files: ICO structure, PNG sizes and alpha, the web app manifest, the `<link>` tags.
* the real HttpTransport serving the real palette folder.
* a real Chrome (or the bundled Chromium) driven by Playwright from whichever Python on the machine
  has it: the page's icon and manifest links load with the session cookie. Skipped without
  Playwright. Set LCSC_ICON_SHOTS to a folder to keep the screenshot of the page.
"""

from __future__ import annotations

import functools
import http.client
import importlib.util
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from types import ModuleType
from typing import Any

import http_transport as ht
import pytest

ROOT = Path(__file__).resolve().parents[1]
ADDIN_DIR = ROOT / "addin" / "LCSC2Fusion"
PALETTE_DIR = ADDIN_DIR / "palette"
RESOURCES_DIR = ADDIN_DIR / "resources"
SVG_PATH = ADDIN_DIR / "AddInIcon.svg"
SVG_NS = "{http://www.w3.org/2000/svg}"

PNG_SIGNATURE = bytes([0x89]) + b"PNG" + bytes([0x0D, 0x0A, 0x1A, 0x0A])
COLOR_TYPE_RGBA = 6
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]
ASSET_NAMES = {
    "favicon.ico": "image/x-icon",
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
    "manifest.webmanifest": "application/manifest+json",
}
PIXEL_TOLERANCE = 2  # per channel, between the committed files and a fresh render


@pytest.fixture(scope="module")
def make_icons() -> Iterator[ModuleType]:
    """scripts/ is not a package: load the generator by path."""
    spec = importlib.util.spec_from_file_location("make_icons", ROOT / "scripts" / "make_icons.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look the defining module up here
    spec.loader.exec_module(module)
    yield module
    del sys.modules[spec.name]


# Helpers ------------------------------------------------------------------------------------


def png_header(blob: bytes) -> tuple[int, int, int, int]:
    """(width, height, bit depth, colour type) from the IHDR chunk of a PNG."""
    assert blob.startswith(PNG_SIGNATURE)
    length, kind = struct.unpack(">I4s", blob[8:16])
    assert (length, kind) == (13, b"IHDR")
    width, height, depth, color_type = struct.unpack(">IIBB", blob[16:26])
    return width, height, depth, color_type


def png_chunk_types(blob: bytes) -> list[str]:
    types: list[str] = []
    position = len(PNG_SIGNATURE)
    while position < len(blob):
        length, kind = struct.unpack(">I4s", blob[position : position + 8])
        types.append(kind.decode("ascii"))
        position += 12 + length  # length, type, data, crc
    assert position == len(blob)
    return types


@dataclass(frozen=True)
class IcoEntry:
    width: int
    height: int
    planes: int
    bit_count: int
    size: int
    offset: int


def parse_ico(data: bytes) -> tuple[list[IcoEntry], list[bytes]]:
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind) == (0, 1)  # type 1 is an icon (2 would be a cursor)
    entries: list[IcoEntry] = []
    for index in range(count):
        raw = data[6 + 16 * index : 6 + 16 * (index + 1)]
        width, height, colors, zero, planes, bits, size, offset = struct.unpack("<BBBBHHII", raw)
        assert (colors, zero) == (0, 0)
        entries.append(IcoEntry(width, height, planes, bits, size, offset))
    blobs = [data[entry.offset : entry.offset + entry.size] for entry in entries]
    return entries, blobs


def css_variable(name: str, scheme: str) -> str:
    css = (PALETTE_DIR / "style.css").read_text(encoding="utf-8")
    marker = "@media (prefers-color-scheme: dark)"
    light, _, dark = css.partition(marker)
    source = dark if scheme == "dark" else light
    match = re.search(rf"--{re.escape(name)}:\s*(#[0-9a-fA-F]{{6}})\b", source)
    assert match is not None, f"--{name} ({scheme}) not found in style.css"
    return match.group(1).lower()


class _HeadCollector(HTMLParser):
    """Records <link> and <meta> start tags with their attributes."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, str | None]] = []
        self.metas: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "link":
            self.links.append(dict(attrs))
        elif tag == "meta":
            self.metas.append(dict(attrs))


def head_tags() -> _HeadCollector:
    collector = _HeadCollector()
    collector.feed((PALETTE_DIR / "index.html").read_text(encoding="utf-8"))
    return collector


def link_for(rel: str) -> list[dict[str, str | None]]:
    return [link for link in head_tags().links if link.get("rel") == rel]


# Geometry: one definition, equal to the SVG --------------------------------------------------


def test_script_geometry_matches_the_svg_numbers(make_icons: ModuleType) -> None:
    root = ET.parse(SVG_PATH).getroot()
    assert root.get("viewBox") == "0 0 128 128"
    full = make_icons.FULL
    assert full.grid == 128

    def rect_of(element: ET.Element) -> Any:
        return make_icons.Rect(
            float(element.attrib["x"]),
            float(element.attrib["y"]),
            float(element.attrib["width"]),
            float(element.attrib["height"]),
        )

    tile, chip = root.findall(f"{SVG_NS}rect")  # direct children only: the pins sit in <g>
    assert rect_of(tile) == full.tile
    assert float(tile.attrib["rx"]) == full.tile_radius
    assert tile.attrib["fill"].lower() == make_icons.BLUE
    assert rect_of(chip) == full.chip
    assert float(chip.attrib["rx"]) == full.chip_radius
    assert chip.attrib["fill"].lower() == make_icons.WHITE

    (circle,) = root.findall(f"{SVG_NS}circle")
    dot = make_icons.Dot(
        float(circle.attrib["cx"]), float(circle.attrib["cy"]), float(circle.attrib["r"])
    )
    assert dot == full.dot
    assert circle.attrib["fill"].lower() == make_icons.BLUE

    (group,) = root.findall(f"{SVG_NS}g")
    assert group.attrib["fill"].lower() == make_icons.GOLD
    assert [rect_of(pin) for pin in group.findall(f"{SVG_NS}rect")] == list(full.pins)


def test_blue_is_the_accent_of_the_page(make_icons: ModuleType) -> None:
    assert css_variable("accent", "light") == make_icons.BLUE


@pytest.mark.parametrize("size", [16, 24, 32])
def test_tiny_layouts_sit_on_the_pixel_grid_and_are_symmetric(
    make_icons: ModuleType, size: int
) -> None:
    layout = make_icons.layout_for(size)
    assert layout is make_icons.SMALL_LAYOUTS[size]
    assert layout.grid == size
    assert layout.tile == make_icons.Rect(0, 0, size, size)
    chip = layout.chip
    assert chip.w == chip.h
    assert 2 * chip.x + chip.w == size  # centred
    pins = set(layout.pins)
    assert len(pins) == len(layout.pins) == (8 if size == 16 else 12)

    def edges(rect: Any) -> list[float]:
        return [rect.x, rect.y, rect.x + rect.w, rect.y + rect.h]

    for rect in (chip, *layout.pins):
        assert all(float(value).is_integer() for value in edges(rect)), rect
        assert rect.x >= 0
        assert rect.y >= 0
        assert rect.x + rect.w <= size
        assert rect.y + rect.h <= size

    def mirrored(rect: Any) -> Any:
        return make_icons.Rect(size - rect.x - rect.w, rect.y, rect.w, rect.h)

    def transposed(rect: Any) -> Any:
        return make_icons.Rect(rect.y, rect.x, rect.h, rect.w)

    assert {mirrored(pin) for pin in pins} == pins  # left right symmetric
    assert {transposed(pin) for pin in pins} == pins  # the same pattern on all four sides
    flipped = {make_icons.Rect(p.x, size - p.y - p.h, p.w, p.h) for p in pins}
    assert flipped == pins  # top bottom symmetric

    left = [p for p in layout.pins if p.x + p.w == chip.x]
    assert len(left) == len(layout.pins) // 4
    assert all(p.w > p.h for p in left)  # pins are longer than they are wide
    for pin in layout.pins:
        touches_chip = (
            pin.x + pin.w == chip.x
            or pin.x == chip.x + chip.w
            or pin.y + pin.h == chip.y
            or pin.y == chip.y + chip.h
        )
        assert touches_chip, pin
    dot = layout.dot
    assert chip.x < dot.cx - dot.r
    assert dot.cx + dot.r < chip.x + chip.w


def test_other_sizes_use_the_svg_geometry(make_icons: ModuleType) -> None:
    for size in (48, 64, 128, 192, 256, 512):
        assert make_icons.layout_for(size) is make_icons.FULL
    assert len(make_icons.FULL.pins) == 12


# Generator output ------------------------------------------------------------------------------


def test_build_ico_writes_header_directory_and_blobs_in_order(make_icons: ModuleType) -> None:
    blobs = {16: b"a" * 10, 256: b"b" * 7, 48: b"c" * 3}
    data = make_icons.build_ico(list(blobs.items()))
    entries, parsed = parse_ico(data)
    assert [(e.width, e.height) for e in entries] == [(16, 16), (0, 0), (48, 48)]  # 256 is 0
    assert parsed == list(blobs.values())
    assert [e.offset for e in entries] == [54, 64, 71]
    assert all((e.planes, e.bit_count) == (1, 32) for e in entries)
    assert len(data) == 6 + 3 * 16 + 20


def test_generated_files_are_exactly_the_expected_set(
    make_icons: ModuleType, tmp_path: Path
) -> None:
    pytest.importorskip("PIL")
    written = make_icons.generate(tmp_path / "palette", tmp_path / "resources")
    assert sorted(path.relative_to(tmp_path).as_posix() for path in written) == [
        "palette/favicon.ico",
        "palette/icon-192.png",
        "palette/icon-512.png",
        "resources/16x16.png",
        "resources/32x32.png",
        "resources/64x64.png",
    ]


def test_generation_is_reproducible_byte_for_byte(make_icons: ModuleType, tmp_path: Path) -> None:
    pytest.importorskip("PIL")
    digests: list[dict[str, bytes]] = []
    for run in ("a", "b"):
        written = make_icons.generate(tmp_path / run / "palette", tmp_path / run / "resources")
        digests.append({p.relative_to(tmp_path / run).as_posix(): p.read_bytes() for p in written})
    assert digests[0] == digests[1]


def test_png_files_carry_no_timestamp_or_text_chunks(make_icons: ModuleType) -> None:
    pytest.importorskip("PIL")
    for size in (16, 32, 192):
        types = png_chunk_types(make_icons.png_bytes(size))
        assert types[0] == "IHDR"
        assert types[-1] == "IEND"
        assert set(types) <= {"IHDR", "IDAT", "IEND"}


def _same_pixels(first: bytes, second: bytes) -> bool:
    from PIL import Image, ImageChops

    left = Image.open(io.BytesIO(first)).convert("RGBA")
    right = Image.open(io.BytesIO(second)).convert("RGBA")
    if left.size != right.size:
        return False
    extrema = ImageChops.difference(left, right).getextrema()
    return all(high <= PIXEL_TOLERANCE for _, high in extrema)


def test_committed_files_equal_a_fresh_render(make_icons: ModuleType, tmp_path: Path) -> None:
    """Catches a design change that was not regenerated. Pixels are compared with a tiny
    tolerance, because zlib and Pillow versions may change the compressed bytes or a rounding."""
    pytest.importorskip("PIL")
    palette, resources = tmp_path / "palette", tmp_path / "resources"
    for fresh in make_icons.generate(palette, resources):
        committed = (PALETTE_DIR if fresh.parent == palette else RESOURCES_DIR) / fresh.name
        hint = f"{committed.name} is stale: run python scripts/make_icons.py"
        assert committed.is_file(), hint
        if committed.read_bytes() == fresh.read_bytes():
            continue
        if fresh.suffix == ".png":
            assert _same_pixels(committed.read_bytes(), fresh.read_bytes()), hint
            continue
        old_entries, old_blobs = parse_ico(committed.read_bytes())
        new_entries, new_blobs = parse_ico(fresh.read_bytes())
        assert [(e.width, e.height) for e in old_entries] == [
            (e.width, e.height) for e in new_entries
        ], hint
        for old, new in zip(old_blobs, new_blobs, strict=True):
            assert _same_pixels(old, new), hint


def test_main_reports_a_missing_pillow_as_an_error(
    make_icons: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def broken(palette_dir: Path, resources_dir: Path) -> list[Path]:
        raise ImportError("No module named PIL")

    monkeypatch.setattr(make_icons, "generate", broken)
    assert make_icons.main(["--palette-dir", str(tmp_path), "--resources-dir", str(tmp_path)]) == 2
    assert "Pillow is required" in capsys.readouterr().err


def test_main_writes_the_files_it_lists(
    make_icons: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest.importorskip("PIL")
    args = ["--palette-dir", str(tmp_path / "p"), "--resources-dir", str(tmp_path / "r")]
    assert make_icons.main(args) == 0
    out = capsys.readouterr().out
    assert out.count("wrote ") == 6
    assert (tmp_path / "p" / "favicon.ico").is_file()


# Committed assets --------------------------------------------------------------------------------


def test_favicon_ico_structure() -> None:
    data = (PALETTE_DIR / "favicon.ico").read_bytes()
    entries, blobs = parse_ico(data)
    assert len(entries) == len(ICO_SIZES)
    sizes = [256 if entry.width == 0 else entry.width for entry in entries]
    assert sizes == ICO_SIZES
    assert entries[-1].width == 0
    assert entries[-1].height == 0  # 256 is stored as 0
    offset = 6 + 16 * len(entries)
    for entry, blob, size in zip(entries, blobs, sizes, strict=True):
        assert (entry.planes, entry.bit_count) == (1, 32)
        assert entry.offset == offset  # blobs follow the directory without gaps
        assert entry.size == len(blob)
        offset += entry.size
        width, height, depth, color_type = png_header(blob)
        assert (width, height) == (size, size)
        assert (depth, color_type) == (8, COLOR_TYPE_RGBA)
    assert offset == len(data)


def test_favicon_ico_is_read_by_pillow_and_every_entry_decodes() -> None:
    image_module = pytest.importorskip("PIL.Image")
    path = PALETTE_DIR / "favicon.ico"
    with image_module.open(path) as icon:
        assert icon.format == "ICO"
        assert sorted(icon.info["sizes"]) == [(s, s) for s in ICO_SIZES]
    _, blobs = parse_ico(path.read_bytes())
    for blob, size in zip(blobs, ICO_SIZES, strict=True):
        with image_module.open(io.BytesIO(blob)) as entry:
            assert entry.format == "PNG"
            assert entry.size == (size, size)
            assert entry.mode == "RGBA"
            entry.load()


PNG_FILES = [
    (PALETTE_DIR / "icon-192.png", 192),
    (PALETTE_DIR / "icon-512.png", 512),
    (RESOURCES_DIR / "16x16.png", 16),
    (RESOURCES_DIR / "32x32.png", 32),
    (RESOURCES_DIR / "64x64.png", 64),
]
PNG_IDS = [f"{path.parent.name}/{path.name}" for path, _ in PNG_FILES]


@pytest.mark.parametrize(("path", "size"), PNG_FILES, ids=PNG_IDS)
def test_png_has_its_size_and_an_alpha_channel(path: Path, size: int) -> None:
    width, height, depth, color_type = png_header(path.read_bytes())
    assert (width, height) == (size, size)
    assert (depth, color_type) == (8, COLOR_TYPE_RGBA)


def _close(pixel: tuple[int, ...], colour: str, distance: int = 24) -> bool:
    wanted = [int(colour[i : i + 2], 16) for i in (1, 3, 5)]
    return pixel[3] == 255 and all(
        abs(a - b) <= distance for a, b in zip(pixel, wanted, strict=False)
    )


def _check_look(image: Any, make_icons: ModuleType) -> None:
    size = image.width
    assert image.getpixel((0, 0))[3] == 0  # the corners of the rounded tile are transparent
    assert image.getpixel((size - 1, size - 1))[3] == 0
    assert image.getpixel((size // 2, size // 2)) == (255, 255, 255, 255)  # the chip body
    raw = image.tobytes()
    pixels = [tuple(raw[i : i + 4]) for i in range(0, len(raw), 4)]
    assert any(_close(p, make_icons.BLUE) for p in pixels), "no tile blue"
    assert any(_close(p, make_icons.GOLD) for p in pixels), "no pin gold"
    opaque = sum(1 for p in pixels if p[3] == 255)
    assert opaque > 0.6 * size * size  # the tile fills most of the canvas


@pytest.mark.parametrize(("path", "size"), PNG_FILES, ids=PNG_IDS)
def test_png_shows_the_icon_on_a_transparent_background(
    make_icons: ModuleType, path: Path, size: int
) -> None:
    image_module = pytest.importorskip("PIL.Image")
    with image_module.open(path) as opened:
        _check_look(opened.convert("RGBA"), make_icons)


@pytest.mark.parametrize("size", ICO_SIZES)
def test_every_ico_entry_shows_the_icon(make_icons: ModuleType, size: int) -> None:
    image_module = pytest.importorskip("PIL.Image")
    _, blobs = parse_ico((PALETTE_DIR / "favicon.ico").read_bytes())
    with image_module.open(io.BytesIO(blobs[ICO_SIZES.index(size)])) as opened:
        _check_look(opened.convert("RGBA"), make_icons)


def test_resource_icons_are_the_ico_frames() -> None:
    # The toolbar buttons and the browser icon are drawn by the same code, so they must agree.
    _, blobs = parse_ico((PALETTE_DIR / "favicon.ico").read_bytes())
    for size in (16, 32, 64):
        assert (RESOURCES_DIR / f"{size}x{size}.png").read_bytes() == blobs[ICO_SIZES.index(size)]


# Manifest -------------------------------------------------------------------------------------


def manifest() -> dict[str, Any]:
    loaded = json.loads((PALETTE_DIR / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_manifest_describes_a_standalone_app_at_the_root() -> None:
    data = manifest()
    assert data["name"] == "LCSC2Fusion"
    assert data["short_name"] == "LCSC2Fusion"
    assert isinstance(data["description"], str)
    assert data["description"]
    assert data["start_url"] == "/"
    assert data["scope"] == "/"
    assert data["display"] == "standalone"


def test_manifest_colours_match_the_page() -> None:
    data = manifest()
    assert re.fullmatch(r"#[0-9a-f]{6}", data["background_color"])
    assert re.fullmatch(r"#[0-9a-f]{6}", data["theme_color"])
    assert data["background_color"] == css_variable("bg", "dark")
    assert data["theme_color"] == css_variable("accent", "light")


def test_manifest_has_no_absolute_urls_and_no_host() -> None:
    text = (PALETTE_DIR / "manifest.webmanifest").read_text(encoding="utf-8")
    for forbidden in ("://", "127.0.0.1", "localhost", "//"):
        assert forbidden not in text, forbidden
    for icon in manifest()["icons"]:
        assert not icon["src"].startswith("/")


def test_manifest_icons_exist_and_declare_their_real_size() -> None:
    icons = manifest()["icons"]
    assert [icon["sizes"] for icon in icons] == ["192x192", "512x512"]
    for icon in icons:
        assert icon["type"] == "image/png"
        assert "purpose" not in icon  # no maskable variant: the app only runs in desktop windows
        blob = (PALETTE_DIR / icon["src"]).read_bytes()
        width, height, _, _ = png_header(blob)
        assert icon["sizes"] == f"{width}x{height}"


# Page head ---------------------------------------------------------------------------------------


def test_head_links_the_icon_set() -> None:
    (icon,) = link_for("icon")
    assert icon["href"] == "favicon.ico"
    assert icon["sizes"] == " ".join(f"{s}x{s}" for s in ICO_SIZES)
    (touch,) = link_for("apple-touch-icon")
    assert touch["href"] == "icon-192.png"
    (linked_manifest,) = link_for("manifest")
    assert linked_manifest["href"] == "manifest.webmanifest"


def test_manifest_link_sends_credentials_because_the_server_needs_its_cookie() -> None:
    (linked_manifest,) = link_for("manifest")
    assert linked_manifest["crossorigin"] == "use-credentials"
    others = [link for link in head_tags().links if link.get("rel") != "manifest"]
    assert all("crossorigin" not in link for link in others)


def test_theme_colour_follows_the_page_background_in_both_schemes() -> None:
    metas = [m for m in head_tags().metas if m.get("name") == "theme-color"]
    by_media = {m["media"]: m["content"] for m in metas}
    assert by_media == {
        "(prefers-color-scheme: light)": css_variable("bg", "light"),
        "(prefers-color-scheme: dark)": css_variable("bg", "dark"),
    }


def test_every_local_reference_of_the_page_exists_and_is_served_by_the_transport() -> None:
    tags = head_tags()
    html = (PALETTE_DIR / "index.html").read_text(encoding="utf-8")
    references = set(re.findall(r'(?:src|href)="([^"]+)"', html))
    assert {link["href"] for link in tags.links} <= references
    for reference in references:
        assert "://" not in reference
        assert not reference.startswith("/")
        assert (PALETTE_DIR / reference).is_file(), reference
        assert reference in ht.STATIC_FILES, f"{reference} would be a 404 in the browser window"


def test_icon_assets_are_on_the_allow_list_with_the_right_types_and_magic_bytes() -> None:
    for name, content_type in ASSET_NAMES.items():
        assert ht.STATIC_FILES[name] == content_type
        assert (PALETTE_DIR / name).is_file()
    assert (PALETTE_DIR / "favicon.ico").read_bytes().startswith(bytes([0, 0, 1, 0]))
    for name in ("icon-192.png", "icon-512.png"):
        assert (PALETTE_DIR / name).read_bytes().startswith(PNG_SIGNATURE)


# The real transport serving the real palette ---------------------------------------------------


@pytest.fixture
def transport() -> Iterator[ht.HttpTransport]:
    server = ht.HttpTransport(ht.Hub(), lambda action, data: "OK", lambda line: None, PALETTE_DIR)
    server.start()
    yield server
    server.stop()


def _get(
    server: ht.HttpTransport, path: str, cookie: str | None = None
) -> tuple[int, dict[str, str], bytes]:
    port = int(server.url.rsplit(":", 1)[1])
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        headers = {"Cookie": cookie} if cookie else {}
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        body = response.read()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, body
    finally:
        connection.close()


def _login(server: ht.HttpTransport) -> str:
    status, headers, _ = _get(server, server.launch_url().removeprefix(server.url))
    assert status == 302
    return headers["set-cookie"].split(";")[0]


def test_transport_serves_the_committed_assets_with_type_and_nosniff(
    transport: ht.HttpTransport,
) -> None:
    cookie = _login(transport)
    for name, content_type in ASSET_NAMES.items():
        status, headers, body = _get(transport, "/" + name, cookie)
        assert status == 200, name
        assert headers["content-type"] == content_type
        assert headers["x-content-type-options"] == "nosniff"
        assert body == (PALETTE_DIR / name).read_bytes()
        assert headers["content-length"] == str(len(body))


def test_favicon_ico_is_the_real_icon_with_the_cookie_and_refused_without(
    transport: ht.HttpTransport,
) -> None:
    cookie = _login(transport)
    status, _, body = _get(transport, "/favicon.ico", cookie)
    assert status == 200
    assert body == (PALETTE_DIR / "favicon.ico").read_bytes()
    for name in ASSET_NAMES:
        refused, _, refused_body = _get(transport, "/" + name)
        assert refused == 403, name
        assert len(refused_body) < 64


def test_the_manifest_served_by_the_transport_is_valid_json(transport: ht.HttpTransport) -> None:
    cookie = _login(transport)
    _, _, body = _get(transport, "/manifest.webmanifest", cookie)
    assert json.loads(body) == manifest()


# Real browser ----------------------------------------------------------------------------------

DRIVER = r'''
"""Run by the test with a Python that has Playwright. Prints ONE JSON line."""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

launch_url, file_url, shot_path, sentinel = sys.argv[1:5]

PROBE = """
async () => {
  const grab = async (url, credentials) => {
    const response = await fetch(url, {credentials: credentials});
    const body = await response.arrayBuffer();
    return {
      status: response.status,
      type: response.headers.get('content-type'),
      nosniff: response.headers.get('x-content-type-options'),
      bytes: body.byteLength,
    };
  };
  const out = {};
  const icon = document.querySelector('link[rel="icon"]');
  out.icon_href = icon.getAttribute('href');
  out.icon = await grab(icon.href, 'same-origin');
  const link = document.querySelector('link[rel="manifest"]');
  out.manifest_crossorigin = link.crossOrigin;
  const mode = link.crossOrigin === 'use-credentials' ? 'include' : 'omit';
  out.manifest = await grab(link.href, mode);
  out.manifest_without_credentials = (await grab(link.href, 'omit')).status;
  const document_ = await (await fetch(link.href, {credentials: 'include'})).json();
  out.manifest_icons = [];
  for (const entry of document_.icons) {
    const url = new URL(entry.src, link.href).href;
    const loaded = await grab(url, 'same-origin');
    out.manifest_icons.push(Object.assign({src: entry.src, sizes: entry.sizes}, loaded));
  }
  return out;
}
"""


def launch(pw):
    errors = []
    for channel in ("chrome", None):
        try:
            browser = pw.chromium.launch(channel=channel) if channel else pw.chromium.launch()
            return browser, channel or "bundled", errors
        except Exception as exc:  # no such browser: try the next one
            errors.append(str(exc)[:200])
    return None, "", errors


def main():
    report = {}
    with sync_playwright() as pw:
        browser, flavor, errors = launch(pw)
        if browser is None:
            return {"skipped": "cannot start a browser: " + " | ".join(errors)}
        try:
            report["flavor"] = flavor
            context = browser.new_context(viewport={"width": 520, "height": 780})
            page = context.new_page()
            responses = []
            page_errors = []
            page.on(
                "response",
                lambda r: responses.append(
                    [r.request.resource_type, r.url.rsplit("/", 1)[-1], r.status]
                ),
            )
            page.on("pageerror", lambda e: page_errors.append(str(e)))
            page.goto(launch_url)
            report["title"] = page.title()
            report["probe"] = page.evaluate(PROBE)
            # The browser's own manifest fetch, made with the credentials mode of the <link>.
            cdp = context.new_cdp_session(page)
            app_manifest = cdp.send("Page.getAppManifest")
            report["app_manifest"] = {
                "url": app_manifest["url"],
                "errors": app_manifest["errors"],
                "name": json.loads(app_manifest["data"] or "{}").get("name"),
            }
            if flavor != "bundled":
                # A full browser also asks for the icon itself, without any script.
                for _ in range(100):
                    if Path(sentinel).exists():
                        break
                    time.sleep(0.1)
            page.screenshot(path=shot_path)
            report["responses"] = responses
            report["page_errors"] = page_errors

            plain = context.new_page()
            plain_errors = []
            plain.on("pageerror", lambda e: plain_errors.append(str(e)))
            plain.goto(file_url)
            for _ in range(100):
                if plain.locator("#mock-banner").is_visible():
                    break
                time.sleep(0.1)
            report["file_page"] = {
                "title": plain.title(),
                "mock_banner_visible": plain.locator("#mock-banner").is_visible(),
                "page_errors": plain_errors,
            }
        finally:
            browser.close()
    return report


print(json.dumps(main()))
'''


@functools.lru_cache(maxsize=1)
def python_with_playwright() -> str | None:
    """The project venv has no Playwright; look for any other interpreter that does."""
    candidates = [sys.executable]
    candidates += [found for name in ("python", "python3") if (found := shutil.which(name))]
    launcher = shutil.which("py")
    if launcher:
        try:
            listed = subprocess.run(
                [launcher, "-3", "-c", "import sys; print(sys.executable)"],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            listed = None
        if listed is not None and listed.returncode == 0 and listed.stdout.strip():
            candidates.append(listed.stdout.strip())
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(os.path.abspath(candidate))
        if key in seen:
            continue
        seen.add(key)
        try:
            probe = subprocess.run(
                [candidate, "-c", "import playwright.sync_api"],
                capture_output=True,
                check=False,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0:
            return candidate
    return None


@dataclass
class BrowserRun:
    report: dict[str, Any]
    served: list[str]  # allow-listed names the transport served (after every access check)
    logs: list[str]
    shot: Path


@pytest.fixture(scope="module")
def browser_run(tmp_path_factory: pytest.TempPathFactory) -> Iterator[BrowserRun]:
    python = python_with_playwright()
    if python is None:
        pytest.skip("Playwright is not installed in any Python on PATH")
    work = tmp_path_factory.mktemp("icon-browser")
    configured = os.environ.get("LCSC_ICON_SHOTS")
    shot_dir = Path(configured) if configured else work
    shot_dir.mkdir(parents=True, exist_ok=True)
    shot = shot_dir / "page_icons.png"
    sentinel = work / "favicon_served"
    driver = work / "icon_driver.py"
    driver.write_text(DRIVER, encoding="utf-8")

    served: list[str] = []
    logs: list[str] = []
    lock = threading.Lock()
    original = ht._Handler._static

    def spy(self: Any, name: str) -> None:
        with lock:
            served.append(name)
        if name == "favicon.ico":
            sentinel.write_text("seen", encoding="utf-8")
        original(self, name)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ht._Handler, "_static", spy)
        server = ht.HttpTransport(
            ht.Hub(), lambda action, data: "OK", logs.append, PALETTE_DIR, heartbeat_s=1.0
        )
        server.start()
        try:
            completed = subprocess.run(
                [
                    python,
                    str(driver),
                    server.launch_url(),
                    (PALETTE_DIR / "index.html").as_uri(),
                    str(shot),
                    str(sentinel),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                timeout=240,
            )
            assert completed.returncode == 0, completed.stderr[-3000:]
            report = json.loads(completed.stdout.strip().splitlines()[-1])
            if "skipped" in report:
                pytest.skip(report["skipped"])
            yield BrowserRun(report, list(served), list(logs), shot)
        finally:
            server.stop()


def test_browser_loads_the_page_title_and_the_icon_link(browser_run: BrowserRun) -> None:
    report = browser_run.report
    assert report["title"] == "LCSC2Fusion"
    assert report["page_errors"] == []
    assert report["probe"]["icon_href"] == "favicon.ico"
    icon = report["probe"]["icon"]
    assert icon["status"] == 200
    assert icon["type"] == "image/x-icon"
    assert icon["nosniff"] == "nosniff"
    assert icon["bytes"] == (PALETTE_DIR / "favicon.ico").stat().st_size


def test_browser_loads_the_manifest_with_credentials_and_not_without(
    browser_run: BrowserRun,
) -> None:
    probe = browser_run.report["probe"]
    assert probe["manifest_crossorigin"] == "use-credentials"
    assert probe["manifest"]["status"] == 200
    assert probe["manifest"]["type"] == "application/manifest+json"
    assert probe["manifest"]["nosniff"] == "nosniff"
    # Without the attribute a browser fetches the manifest anonymously and the session check
    # refuses it: that is why the <link> must say use-credentials.
    assert probe["manifest_without_credentials"] == 403


def test_browser_manifest_icons_load_and_declare_their_size(browser_run: BrowserRun) -> None:
    icons = browser_run.report["probe"]["manifest_icons"]
    assert [(i["src"], i["sizes"]) for i in icons] == [
        ("icon-192.png", "192x192"),
        ("icon-512.png", "512x512"),
    ]
    for icon in icons:
        assert icon["status"] == 200
        assert icon["type"] == "image/png"
        assert icon["bytes"] == (PALETTE_DIR / icon["src"]).stat().st_size


def test_browser_itself_fetches_and_accepts_the_manifest(browser_run: BrowserRun) -> None:
    report = browser_run.report
    assert report["app_manifest"]["url"].endswith("/manifest.webmanifest")
    assert report["app_manifest"]["errors"] == []
    assert report["app_manifest"]["name"] == "LCSC2Fusion"
    fetched = [r for r in report["responses"] if r[0] == "manifest"]
    assert fetched
    assert all(status == 200 for _, _, status in fetched)
    assert "manifest.webmanifest" in browser_run.served


def test_browser_requests_for_icons_are_never_refused(browser_run: BrowserRun) -> None:
    # The only refusal is the deliberate request without credentials made by the probe above.
    refusals = [line for line in browser_run.logs if line.startswith("refused")]
    assert refusals == ["refused 403: no valid session"]


def test_a_full_browser_asks_for_the_favicon_with_the_session_cookie(
    browser_run: BrowserRun,
) -> None:
    if browser_run.report["flavor"] == "bundled":
        pytest.skip("the headless shell never requests a favicon; needs an installed Chrome")
    assert "favicon.ico" in browser_run.served  # served means it passed the cookie check


def test_page_works_from_a_file_url_without_the_server(browser_run: BrowserRun) -> None:
    plain = browser_run.report["file_page"]
    assert plain["title"] == "LCSC2Fusion"
    assert plain["mock_banner_visible"] is True  # mock mode: no bridge, so the mock is used
    assert plain["page_errors"] == []


def test_browser_screenshot_was_taken(browser_run: BrowserRun) -> None:
    assert browser_run.shot.stat().st_size > 2000
