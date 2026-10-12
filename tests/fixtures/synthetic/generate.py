#!/usr/bin/env python3
"""Deterministic generator of SYNTHETIC EasyEDA component fixtures.

Run `python tests/fixtures/synthetic/generate.py` to (re)write every
`<name>.json` and `<name>.expected.json` next to this file. Use `--check` to
only compare the in-memory output with the files on disk (exit code 1 on drift).

Why this exists (DECISIONS D-013): real EasyEDA library data is copyrighted and the
repository is public, so the committed fixtures are made from small parametric
builders. They follow the field layout of the real endpoint
`https://easyeda.com/api/components/{uuid}` (see docs/RESEARCH.md 1.5 to 1.11) but
contain no EasyEDA geometry, names, uuids or license text.

Each part is described once in millimetres (Y up, relative to the footprint or
symbol origin). The same parameters feed two independent outputs:

* the EasyEDA document (units of 0.254 mm, Y down, absolute canvas coordinates),
* the `<name>.expected.json` sidecar (millimetres, Y up, relative to the origin).

The sidecar is computed from the emitted EasyEDA numbers with the documented
formulas only (x_mm = (x - ox) * 0.254, y_mm = -(y - oy) * 0.254), never through a
parser.

Conventions reproduced from the real files:

* PAD has 20 fields. `points` holds absolute outline vertices that already include
  the rotation (RECT, POLYGON), or the two axis end points (OVAL), or nothing
  (ELLIPSE). RECT outlines were checked against all RECT pads with rotation 0, 90,
  180 and 270 of the local real samples: base list (-a,-b) (a,-b) (a,b) (-a,b) in
  the Y down frame, rotated counter-clockwise on screen with
  x' = x cos t + y sin t, y' = -x sin t + y cos t. Older editor versions list the
  same vertices starting one corner earlier (`Pad.vertex_shift = 0`).
* Pin rotation 0 is the right edge and the pin line extends to the LEFT of the
  connection point; the internal model angle is (ee_rotation + 180) % 360.
* Symbol origin: BBox centre snapped to the 5 unit grid. Every BBox written here is
  symmetric around head.x/head.y so that rule and "head.x/y" agree.
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

OUT_DIR = Path(__file__).resolve().parent
UNIT = 0.254  # millimetres per EasyEDA unit (10 mil)
LICENSE_TEXT = "Synthetic test fixture, not EasyEDA data"
EDITOR_VERSION = "6.5.51"
FIXED_TIME = 1700000000
EXAMPLE_HOST = "//example.invalid/synthetic"

LAYER_NAMES = {1: "top", 2: "bottom", 11: "thru"}
PIN_DIRECTIONS = {"1": "in", "2": "out", "3": "io", "4": "pwr", "0": "pas", "": "pas"}
ArcStyle = str  # "spaced" or "compact"


# ----------------------------------------------------------------------------
# Number helpers
# ----------------------------------------------------------------------------


def r4(v: float) -> float:
    """Round to 4 decimals (EasyEDA precision), never returning -0.0."""
    return round(v, 4) + 0.0


def r6(v: float) -> float:
    """Round to 6 decimals for the expected sidecar, never returning -0.0."""
    return round(v, 6) + 0.0


def fmt(v: float) -> str:
    """Format like EasyEDA: up to 4 decimals, no trailing zeros, no exponent."""
    s = f"{round(v, 4) + 0.0:.4f}"
    s = s.rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def md5hex(*parts: str) -> str:
    return hashlib.md5(":".join(parts).encode("utf-8")).hexdigest()


def cos_sin(deg: float) -> tuple[float, float]:
    """cos and sin in degrees, exact for multiples of 90."""
    d = deg % 360
    exact = {0: (1.0, 0.0), 90: (0.0, 1.0), 180: (-1.0, 0.0), 270: (0.0, -1.0)}
    if d in exact:
        return exact[int(d)]
    t = math.radians(d)
    return math.cos(t), math.sin(t)


def rotate_screen(x: float, y: float, deg: float) -> tuple[float, float]:
    """Rotate counter-clockwise as seen on screen. Input and output use the Y down frame."""
    c, s = cos_sin(deg)
    return x * c + y * s, -x * s + y * c


def nint(v: float) -> int | float:
    """Integral floats become ints so the JSON reads like the real files (x: 20, not 20.0)."""
    return int(v) if float(v).is_integer() else v


def snap5(v: float) -> float:
    q = v / 5.0
    if abs(q - round(q)) > 0.25:
        raise ValueError(f"symbol centre {v} is too far from the 5 unit grid")
    return 5.0 * round(q)


# ----------------------------------------------------------------------------
# Coordinate frame
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Frame:
    """Origin in EasyEDA absolute coordinates. Converts between mm (Y up) and EE (Y down)."""

    ox: float
    oy: float

    def ee(self, x_mm: float, y_mm: float) -> tuple[float, float]:
        return r4(self.ox + x_mm / UNIT), r4(self.oy - y_mm / UNIT)

    def mm(self, x_ee: float, y_ee: float) -> tuple[float, float]:
        return r6((x_ee - self.ox) * UNIT), r6(-(y_ee - self.oy) * UNIT)

    def mm_list(self, pts: Sequence[tuple[float, float]]) -> list[list[float]]:
        return [list(self.mm(x, y)) for x, y in pts]


def _poly_path(*pts: tuple[float, float]) -> str:
    """SVG polyline path in the spaced spelling used by pin clock marks."""
    return "M " + " L ".join(f"{fmt(x)} {fmt(y)}" for x, y in pts)


def pts_str(pts: Sequence[tuple[float, float]]) -> str:
    return " ".join(f"{fmt(x)} {fmt(y)}" for x, y in pts)


# ----------------------------------------------------------------------------
# Footprint builder
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Pad:
    """One footprint pad, described in mm (Y up, relative to the footprint origin)."""

    number: str
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0  # size before rotation, like fields 4 and 5 of the PAD string
    h: float = 0.0
    shape: str = "RECT"  # RECT, OVAL, ELLIPSE, POLYGON
    layer: int = 1  # 1 top SMD, 2 bottom SMD, 11 through hole
    rot: float = 0.0
    drill: float = 0.0  # hole diameter in mm, 0 for SMD
    slot: float = 0.0  # slot length end to end in mm, 0 for a round hole
    plated: bool = True
    poly: tuple[tuple[float, float], ...] = ()  # POLYGON vertices, absolute mm
    vertex_shift: int = 1  # RECT vertex order variant, see module docstring


def _extent_of(
    pts: Sequence[tuple[float, float]], pad_r: float = 0.0
) -> tuple[float, float, float, float]:
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs) - pad_r, min(ys) - pad_r, max(xs) + pad_r, max(ys) + pad_r


class FootprintBuilder:
    """Collects PAD/TRACK/... shape strings and the matching expected values."""

    def __init__(
        self,
        origin: tuple[float, float],
        *,
        tail: tuple[str, str] = ("0", "0.2"),
        zero_len: str = "0",
    ) -> None:
        self.frame = Frame(origin[0], origin[1])
        self.tail = tail  # PAD fields 17 and 18 (meaning unverified)
        self.zero_len = zero_len  # PAD field 13 for pads without slot
        self.shapes: list[str] = []
        self.pads: list[dict[str, Any]] = []
        self.holes: list[dict[str, Any]] = []
        self.tracks: list[dict[str, Any]] = []
        self.circles: list[dict[str, Any]] = []
        self.arcs: list[dict[str, Any]] = []
        self.rects: list[dict[str, Any]] = []
        self.regions: list[dict[str, Any]] = []
        self.texts: list[dict[str, Any]] = []
        self.counts: dict[str, int] = {}
        self._n = 0
        self._ee_pts: list[tuple[float, float]] = []
        self._pad_ext: list[tuple[float, float, float, float]] = []
        self.model3d: dict[str, Any] | None = None  # expected 3D placement, see add_svgnode

    # -- internals ---------------------------------------------------------

    def _id(self) -> str:
        self._n += 1
        return f"gge{1000 + self._n}"

    def _emit(self, tag: str, text: str) -> None:
        self.shapes.append(text)
        self.counts[tag] = self.counts.get(tag, 0) + 1

    # -- pads and holes ------------------------------------------------------

    def add_pad(self, p: Pad) -> None:
        fr = self.frame
        rot = p.rot % 360
        outline: list[tuple[float, float]] = []
        if p.shape == "POLYGON":
            outline = [fr.ee(x, y) for x, y in p.poly]
            x0, y0, x1, y1 = _extent_of(outline)
            cx, cy = r4((x0 + x1) / 2), r4((y0 + y1) / 2)
            w, h = r4(x1 - x0), r4(y1 - y0)
        else:
            cx, cy = fr.ee(p.x, p.y)
            w, h = r4(p.w / UNIT), r4(p.h / UNIT)
        if p.shape == "RECT":
            a, b = w / 2, h / 2
            base = [(-a, -b), (a, -b), (a, b), (-a, b)]
            if p.vertex_shift == 0:
                base = [base[3], *base[:3]]
            outline = [
                (r4(cx + rx), r4(cy + ry)) for rx, ry in (rotate_screen(x, y, rot) for x, y in base)
            ]
        elif p.shape == "OVAL":
            d = abs(w - h) / 2
            local = [(-d, 0.0), (d, 0.0)] if w >= h else [(0.0, -d), (0.0, d)]
            outline = [
                (r4(cx + rx), r4(cy + ry))
                for rx, ry in (rotate_screen(x, y, rot) for x, y in local)
            ]
        hole_r = r4(p.drill / 2 / UNIT) if p.drill else 0.0
        hole_len = r4(p.slot / UNIT) if p.slot else 0.0
        slot_axis: list[tuple[float, float]] = []
        if p.slot:
            if h < w:
                raise ValueError("slot pads must be taller than wide in this generator")
            half = hole_len / 2 - hole_r
            local_axis = [(0.0, half), (0.0, -half)]
            slot_axis = [
                (r4(cx + rx), r4(cy + ry))
                for rx, ry in (rotate_screen(x, y, rot) for x, y in local_axis)
            ]
        # Extent of the copper shape in EE units (Y down).
        if p.shape in ("RECT", "POLYGON"):
            ex = _extent_of(outline)
        elif p.shape == "OVAL":
            ex = _extent_of(outline, min(w, h) / 2)
        else:
            c, s = cos_sin(rot)
            hx = math.hypot(w / 2 * c, h / 2 * s)
            hy = math.hypot(w / 2 * s, h / 2 * c)
            ex = (cx - hx, cy - hy, cx + hx, cy + hy)
        self._ee_pts.extend([(ex[0], ex[1]), (ex[2], ex[3])])
        # Millimetre extent, Y up: min y comes from the larger EE y.
        mm_lo = fr.mm(ex[0], ex[3])
        mm_hi = fr.mm(ex[2], ex[1])
        self._pad_ext.append((mm_lo[0], mm_lo[1], mm_hi[0], mm_hi[1]))

        fields = [
            "PAD",
            p.shape,
            fmt(cx),
            fmt(cy),
            fmt(w),
            fmt(h),
            str(p.layer),
            "",
            p.number,
            fmt(hole_r) if hole_r else "0",
            pts_str(outline),
            fmt(rot),
            self._id(),
            fmt(hole_len) if hole_len else self.zero_len,
            pts_str(slot_axis),
            "Y" if p.plated else "N",
            "0",
            self.tail[0],
            self.tail[1],
            f"{fmt(cx)},{fmt(cy)}",
        ]
        self._emit("PAD", "~".join(fields))
        px, py = fr.mm(cx, cy)
        self.pads.append(
            {
                "number": p.number,
                "x_mm": px,
                "y_mm": py,
                "width_mm": r6(w * UNIT),
                "height_mm": r6(h * UNIT),
                "shape": p.shape,
                "drill_mm": r6(2 * hole_r * UNIT),
                "rotation_deg": float(rot),
                "layer": LAYER_NAMES[p.layer],
                "slot_length_mm": r6(hole_len * UNIT),
                "plated": p.plated,
                "points_mm": fr.mm_list(outline),
                "slot_axis_mm": fr.mm_list(slot_axis),
                "extent_mm": [r6(v) for v in self._pad_ext[-1]],
            }
        )

    def add_hole(self, x: float, y: float, drill: float) -> None:
        cx, cy = self.frame.ee(x, y)
        radius = r4(drill / 2 / UNIT)
        self._ee_pts.extend([(cx - radius, cy - radius), (cx + radius, cy + radius)])
        self._emit("HOLE", "~".join(["HOLE", fmt(cx), fmt(cy), fmt(radius), self._id(), "0"]))
        mx, my = self.frame.mm(cx, cy)
        self.holes.append({"x_mm": mx, "y_mm": my, "drill_mm": r6(2 * radius * UNIT)})

    # -- silkscreen and documentation shapes ---------------------------------

    def add_track(
        self, pts: Sequence[tuple[float, float]], sw: float = 0.6, layer: int = 3
    ) -> None:
        ee = [self.frame.ee(x, y) for x, y in pts]
        self._ee_pts.extend(ee)
        self._emit(
            "TRACK",
            "~".join(["TRACK", fmt(sw), str(layer), "", pts_str(ee), self._id(), "0"]),
        )
        self.tracks.append(
            {
                "layer": layer,
                "width_mm": r6(sw * UNIT),
                "points_mm": self.frame.mm_list(ee),
            }
        )

    def add_circle(self, x: float, y: float, r: float, sw: float, layer: int) -> None:
        cx, cy = self.frame.ee(x, y)
        radius = r4(r / UNIT)
        self._ee_pts.extend([(cx - radius, cy - radius), (cx + radius, cy + radius)])
        fields = [
            "CIRCLE",
            fmt(cx),
            fmt(cy),
            fmt(radius),
            fmt(sw),
            str(layer),
            self._id(),
            "0",
            "",
            "",
        ]
        self._emit("CIRCLE", "~".join(fields))
        mx, my = self.frame.mm(cx, cy)
        self.circles.append(
            {
                "layer": layer,
                "x_mm": mx,
                "y_mm": my,
                "radius_mm": r6(radius * UNIT),
                "width_mm": r6(sw * UNIT),
            }
        )

    def add_arc(
        self,
        x: float,
        y: float,
        r: float,
        start_deg: float,
        sweep_deg: float,
        sw: float = 0.6,
        layer: int = 3,
        style: ArcStyle = "spaced",
    ) -> None:
        """Arc given by centre and radius in mm. Angles: counter-clockwise positive, Y up."""
        fr = self.frame
        c0, s0 = cos_sin(start_deg)
        c1, s1 = cos_sin(start_deg + sweep_deg)
        start = fr.ee(x + r * c0, y + r * s0)
        end = fr.ee(x + r * c1, y + r * s1)
        r_ee = r4(r / UNIT)
        large = 1 if abs(sweep_deg) > 180 else 0
        sweep_flag = 1 if sweep_deg < 0 else 0  # SVG sweep 1 means clockwise on screen
        if style == "compact":
            path = (
                f"M{fmt(start[0])},{fmt(start[1])} A{fmt(r_ee)},{fmt(r_ee)} 0 {large} {sweep_flag} "
                f"{fmt(end[0])},{fmt(end[1])}"
            )
        else:
            path = (
                f"M {fmt(start[0])} {fmt(start[1])} A {fmt(r_ee)} {fmt(r_ee)} 0 {large} "
                f"{sweep_flag} {fmt(end[0])} {fmt(end[1])}"
            )
        cx_ee, cy_ee = fr.ee(x, y)
        self._ee_pts.extend([(cx_ee - r_ee, cy_ee - r_ee), (cx_ee + r_ee, cy_ee + r_ee)])
        self._emit(
            "ARC",
            "~".join(["ARC", fmt(sw), str(layer), "", path, "", self._id(), "0"]),
        )
        self.arcs.append(
            {
                "layer": layer,
                "width_mm": r6(sw * UNIT),
                "cx_mm": r6(x),
                "cy_mm": r6(y),
                "radius_mm": r6(r_ee * UNIT),
                "start_deg": float(start_deg % 360),
                "sweep_deg": float(sweep_deg),
                "large_arc": large,
                "svg_sweep_flag": sweep_flag,
                "start_mm": list(fr.mm(*start)),
                "end_mm": list(fr.mm(*end)),
            }
        )

    def add_rect(
        self, x_min: float, y_max: float, w: float, h: float, layer: int, sw: float
    ) -> None:
        """RECT shape. EasyEDA stores the upper left corner (x_min, y_max in Y up terms)."""
        fr = self.frame
        x, y = fr.ee(x_min, y_max)
        w_ee, h_ee = r4(w / UNIT), r4(h / UNIT)
        self._ee_pts.extend([(x, y), (x + w_ee, y + h_ee)])
        fields = [
            "RECT",
            fmt(x),
            fmt(y),
            fmt(w_ee),
            fmt(h_ee),
            str(layer),
            self._id(),
            "0",
            fmt(sw),
            "none",
            "",
            "",
            "",
        ]
        self._emit("RECT", "~".join(fields))
        self.rects.append(
            {
                "layer": layer,
                "points_mm": fr.mm_list(
                    [(x, y), (x + w_ee, y), (x + w_ee, y + h_ee), (x, y + h_ee)]
                ),
                "width_mm": r6(sw * UNIT),
            }
        )

    def add_region(
        self,
        pts: Sequence[tuple[float, float]],
        layer: int,
        kind: str = "solid",
        style: str = "spaced",
    ) -> None:
        ee = [self.frame.ee(x, y) for x, y in pts]
        self._ee_pts.extend(ee)
        if style == "compact":
            path = "M" + "L".join(f"{fmt(x)},{fmt(y)}" for x, y in ee) + "Z"
        else:
            path = "M " + " L ".join(f"{fmt(x)} {fmt(y)}" for x, y in ee) + " Z "
        fields = ["SOLIDREGION", str(layer), "", path, kind, self._id(), "", "", "", "0"]
        self._emit("SOLIDREGION", "~".join(fields))
        self.regions.append(
            {"layer": layer, "kind": kind, "points_mm": self.frame.mm_list(ee)},
        )

    def add_text(self, kind: str, x: float, y: float, layer: int, text: str) -> None:
        """TEXT shape. No real sample was available, layout follows the EasyEDA Std docs."""
        cx, cy = self.frame.ee(x, y)
        self._ee_pts.append((cx, cy))
        glyph = f"M {fmt(cx)} {fmt(cy)} L {fmt(cx + 1)} {fmt(cy)}"
        fields = [
            "TEXT",
            kind,
            fmt(cx),
            fmt(cy),
            "0.6",
            "0",
            "",
            str(layer),
            "",
            "4.5",
            text,
            glyph,
            "",
            self._id(),
            "0",
        ]
        self._emit("TEXT", "~".join(fields))
        mx, my = self.frame.mm(cx, cy)
        self.texts.append(
            {
                "type": kind,
                "x_mm": mx,
                "y_mm": my,
                "layer": layer,
                "text": text,
                "rotation_deg": 0.0,
            }
        )

    def add_svgnode(
        self,
        title: str,
        *,
        native_mm: tuple[float, float] = (1.6, 0.8),
        centre_mm: tuple[float, float] = (0.0, 0.0),
        z_mm: float = 0.0,
        rotation_deg: tuple[float, float, float] = (0.0, 0.0, 0.0),
        stale_shift_ee: tuple[float, float] | None = None,
    ) -> None:
        """3D model node (SVGNODE). Contains no `~`, so it survives a plain split.

        The model is described in mm (Y up, relative to the footprint origin): `native_mm` is the
        unrotated X/Y size (c_width, c_height), `centre_mm` the true centre of the placed model,
        `z_mm` the height of its lowest point (sign kept) and `rotation_deg` the (rx, ry, rz)
        triple. The outline is the placed bounding rectangle, so rz must be a multiple of 90 and
        the X/Y sizes swap for 90 and 270. `stale_shift_ee` moves `c_origin` away from the true
        centre by that many EasyEDA units (x, y in the Y down frame), like the outdated c_origin
        of 4 of the 23 real samples. The outline stays correct.

        The expected placement (self.model3d) is computed from the emitted numbers with the
        documented rules only: offset = (x - head.x) * 0.254, -(y - head.y) * 0.254; the outline
        bounding box centre replaces c_origin when it is more than 0.1 mm away.
        """
        if self.model3d is not None:
            raise ValueError("only one SVGNODE per footprint")
        fr = self.frame
        rz = rotation_deg[2] % 360
        if rz not in (0, 90, 180, 270):
            raise ValueError("the placed outline is a rectangle, rz must be a multiple of 90")
        w_ee, h_ee = r4(native_mm[0] / UNIT), r4(native_mm[1] / UNIT)
        placed_w, placed_h = (h_ee, w_ee) if rz in (90, 270) else (w_ee, h_ee)
        cx, cy = fr.ee(*centre_mm)
        outline = [
            (r4(cx - placed_w / 2), r4(cy - placed_h / 2)),
            (r4(cx + placed_w / 2), r4(cy - placed_h / 2)),
            (r4(cx + placed_w / 2), r4(cy + placed_h / 2)),
            (r4(cx - placed_w / 2), r4(cy + placed_h / 2)),
        ]
        origin = (cx, cy)
        if stale_shift_ee is not None:
            origin = (r4(cx + stale_shift_ee[0]), r4(cy + stale_shift_ee[1]))
        z_ee = r4(z_mm / UNIT)
        uuid = md5hex("syn", "3d", title)
        node = {
            "gId": "g1_outline",
            "nodeName": "g",
            "nodeType": 1,
            "layerid": "19",
            "attrs": {
                "c_width": fmt(w_ee),
                "c_height": fmt(h_ee),
                "c_rotation": ",".join(fmt(v) for v in rotation_deg),
                "z": fmt(z_ee),
                "c_origin": f"{fmt(origin[0])},{fmt(origin[1])}",
                "uuid": uuid,
                "c_etype": "outline3D",
                "id": "g1_outline",
                "title": title,
                "layerid": "19",
                "transform": "scale(1) translate(0, 0)",
            },
            "childNodes": [
                {
                    "gId": "g1_outline_line0",
                    "nodeName": "polyline",
                    "nodeType": 1,
                    "attrs": {
                        "fill": "none",
                        "id": "g1_outline_line0",
                        "c_shapetype": "line",
                        "points": pts_str(outline),
                    },
                }
            ],
        }
        self._emit("SVGNODE", "SVGNODE~" + json.dumps(node, separators=(",", ":")))
        # Expected values, from the emitted numbers (the emitted strings are 4 decimal rounded).
        xs, ys = [p[0] for p in outline], [p[1] for p in outline]
        outline_centre = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
        distance_mm = math.hypot(origin[0] - outline_centre[0], origin[1] - outline_centre[1]) * UNIT
        stale = distance_mm > 0.1
        if stale != (stale_shift_ee is not None):
            raise ValueError("stale_shift_ee must move c_origin by more than 0.1 mm, or be None")
        placed = outline_centre if stale else origin
        self.model3d = {
            "uuid": uuid,
            "name": title,
            "offset_mm": list(fr.mm(*placed)),
            "bottom_z_mm": r6(z_ee * UNIT),
            "rotation_deg": [float(fmt(v)) for v in rotation_deg],
            "native_size_mm": [r6(w_ee * UNIT), r6(h_ee * UNIT)],
            "placement_source": "outline_centre" if stale else "c_origin",
            "stale_origin": stale,
            "c_origin_mm": list(fr.mm(*origin)),
            "outline_centre_mm": list(fr.mm(*outline_centre)),
            "outline_size_mm": [r6(placed_w * UNIT), r6(placed_h * UNIT)],
        }

    # -- results -----------------------------------------------------------------

    def bbox(self) -> dict[str, float]:
        xs = [p[0] for p in self._ee_pts]
        ys = [p[1] for p in self._ee_pts]
        x0 = math.floor(min(xs) * 10) / 10
        y0 = math.floor(min(ys) * 10) / 10
        x1 = math.ceil(max(xs) * 10) / 10
        y1 = math.ceil(max(ys) * 10) / 10
        return {"x": r4(x0), "y": r4(y0), "width": r4(x1 - x0), "height": r4(y1 - y0)}

    def pad_bbox_mm(self) -> dict[str, float]:
        lo_x = min(e[0] for e in self._pad_ext)
        lo_y = min(e[1] for e in self._pad_ext)
        hi_x = max(e[2] for e in self._pad_ext)
        hi_y = max(e[3] for e in self._pad_ext)
        return {"min_x": r6(lo_x), "min_y": r6(lo_y), "max_x": r6(hi_x), "max_y": r6(hi_y)}


# ----------------------------------------------------------------------------
# Symbol builder
# ----------------------------------------------------------------------------


@dataclass
class SymbolUnit:
    shapes: list[str]
    origin: tuple[int | float, int | float]
    bbox: dict[str, int | float]
    pins: list[dict[str, Any]]
    arcs: list[dict[str, Any]]
    counts: dict[str, int]


class SymbolBuilder:
    """Builds symbol shape strings in local coordinates (Y down, EE units) plus an offset."""

    def __init__(
        self,
        off: tuple[float, float],
        *,
        font: tuple[str, str] = ("", ""),
        path_style: str = "spaced",
    ) -> None:
        self.off = off
        self.font = font
        self.path_style = path_style
        self.shapes: list[str] = []
        self.counts: dict[str, int] = {}
        self._pins: list[dict[str, Any]] = []
        self._arcs: list[dict[str, Any]] = []
        self._pts: list[tuple[float, float]] = []
        self._n = 0

    def _id(self) -> str:
        self._n += 1
        return f"gge{self._n * 3 - 1}"

    def _abs(self, x: float, y: float) -> tuple[float, float]:
        return x + self.off[0], y + self.off[1]

    def _emit(self, tag: str, text: str) -> None:
        self.shapes.append(text)
        self.counts[tag] = self.counts.get(tag, 0) + 1

    def _track(self, *pts: tuple[float, float]) -> None:
        self._pts.extend(pts)

    # -- body shapes ---------------------------------------------------------

    def rect(self, x: float, y: float, w: float, h: float, variant: str = "a") -> None:
        ax, ay = self._abs(x, y)
        self._track((ax, ay), (ax + w, ay + h))
        if variant == "a":
            fields = ["R", fmt(ax), fmt(ay), "", "", fmt(w), fmt(h), "#880000", "1", "0", "none"]
            fields += [self._id(), "0"]
        else:
            fields = ["R", fmt(ax), fmt(ay), "2", "2", fmt(w), fmt(h), "#880000", "1", "0", "none"]
            fields += [self._id(), "0", ""]
        self._emit("R", "~".join(fields))

    def circle(self, cx: float, cy: float, r: float, fill: str = "none") -> None:
        ax, ay = self._abs(cx, cy)
        self._track((ax - r, ay - r), (ax + r, ay + r))
        fields = ["C", fmt(ax), fmt(ay), fmt(r), "#880000", "1", "0", fill, self._id(), "0"]
        self._emit("C", "~".join(fields))

    def ellipse(self, cx: float, cy: float, rx: float, ry: float, fill: str = "#880000") -> None:
        ax, ay = self._abs(cx, cy)
        self._track((ax - rx, ay - ry), (ax + rx, ay + ry))
        fields = [
            "E",
            fmt(ax),
            fmt(ay),
            fmt(rx),
            fmt(ry),
            "#880000",
            "1",
            "0",
            fill,
            self._id(),
            "0",
        ]
        self._emit("E", "~".join(fields))

    def _poly(self, tag: str, pts: Sequence[tuple[float, float]], fill: str, color: str) -> None:
        ab = [self._abs(x, y) for x, y in pts]
        self._track(*ab)
        fields = [tag, pts_str(ab), color, "1", "0", fill, self._id(), "0"]
        self._emit(tag, "~".join(fields))

    def line(self, *pts: tuple[float, float], color: str = "#880000") -> None:
        self._poly("PL", pts, "none", color)

    def polygon(self, *pts: tuple[float, float], color: str = "#880000") -> None:
        self._poly("PG", pts, color, color)

    def filled_path(self, *pts: tuple[float, float]) -> None:
        ab = [self._abs(x, y) for x, y in pts]
        self._track(*ab)
        path = "M " + " L ".join(f"{fmt(x)} {fmt(y)}" for x, y in ab) + " Z "
        fields = ["PT", path, "#880000", "1", "0", "#880000", self._id(), "0", ""]
        self._emit("PT", "~".join(fields))

    def arc(self, cx: float, cy: float, r: float, start_deg: float, sweep_deg: float) -> None:
        """Arc with centre in local EE coordinates, angles counter-clockwise on screen."""
        ax, ay = self._abs(cx, cy)
        c0, s0 = cos_sin(start_deg)
        c1, s1 = cos_sin(start_deg + sweep_deg)
        sx, sy = r4(ax + r * c0), r4(ay - r * s0)
        ex, ey = r4(ax + r * c1), r4(ay - r * s1)
        large = 1 if abs(sweep_deg) > 180 else 0
        sweep_flag = 1 if sweep_deg < 0 else 0
        path = (
            f"M {fmt(sx)} {fmt(sy)} A {fmt(r)} {fmt(r)} 0 {large} {sweep_flag} {fmt(ex)} {fmt(ey)}"
        )
        self._track((ax - r, ay - r), (ax + r, ay + r))
        fields = ["A", path, "", "#880000", "1", "0", "none", self._id(), "0"]
        self._emit("A", "~".join(fields))
        self._arcs.append(
            {
                "abs_center": (ax, ay),
                "radius_ee": r,
                "start_deg": float(start_deg % 360),
                "sweep_deg": float(sweep_deg),
                "abs_start": (sx, sy),
                "abs_end": (ex, ey),
                "large_arc": large,
                "svg_sweep_flag": sweep_flag,
            }
        )

    def text(self, x: float, y: float, text: str) -> None:
        ax, ay = self._abs(x, y)
        fields = [
            "T",
            "L",
            fmt(ax),
            fmt(ay),
            "0",
            "#0000FF",
            "Tahoma",
            "11.5pt",
            "0.1",
            "",
            "middle",
            "comment",
            text,
            "1",
            "middle",
            self._id(),
            "0",
            "pinpart",
        ]
        self._emit("T", "~".join(fields))

    # -- pins ----------------------------------------------------------------

    def pin(
        self,
        number: str,
        name: str,
        x: float,
        y: float,
        rot: int,
        ptype: str = "0",
        *,
        length: float = 10,
        dot: bool = False,
        clk: bool = False,
        spice: str | None = None,
        show_name: bool = True,
        show_num: bool = True,
        empty_rot: bool = False,
        line_color: str = "#880000",
        text_color: str = "#0000FF",
    ) -> None:
        """Add a pin. (x, y) is the connection point in local coordinates."""
        x0, y0 = self._abs(x, y)
        ln = length
        if rot == 0:
            be = (x0 - ln, y0)
            run, axis = -ln, "h"
            clk_path = _poly_path((be[0], y0 - 3), (be[0] - 3, y0), (be[0], y0 + 3))
            dot_c = (be[0] + 3, be[1])
            name_t = (be[0] - 5, y0 + 3, 0, "end")
            num_t = (x0 - 5, y0 - 1, 0, "start")
        elif rot == 180:
            be = (x0 + ln, y0)
            run, axis = ln, "h"
            clk_path = _poly_path((be[0], y0 + 3), (be[0] + 3, y0), (be[0], y0 - 3))
            dot_c = (be[0] - 3, be[1])
            name_t = (be[0] + 5, y0 + 3, 0, "start")
            num_t = (x0 + 5, y0 - 1, 0, "end")
        elif rot == 90:
            be = (x0, y0 + ln)
            run, axis = ln, "v"
            clk_path = _poly_path((x0 - 3, be[1]), (x0, be[1] + 3), (x0 + 3, be[1]))
            dot_c = (be[0], be[1] - 3)
            name_t = (x0 + 3, be[1] + 3, 270, "end")
            num_t = (x0 - 1, y0 + ln + 4, 270, "start")
        elif rot == 270:
            be = (x0, y0 - ln)
            run, axis = -ln, "v"
            clk_path = _poly_path((x0 + 3, be[1]), (x0, be[1] - 3), (x0 - 3, be[1]))
            dot_c = (be[0], be[1] + 3)
            name_t = (x0 + 3, be[1] - 3, 270, "start")
            num_t = (x0 - 1, y0 - ln - 4, 270, "end")
        else:
            raise ValueError(f"unsupported pin rotation {rot}")
        if self.path_style == "compact":
            path = f"M{fmt(x0)},{fmt(y0)}{axis}{fmt(run)}"
        else:
            path = f"M {fmt(x0)} {fmt(y0)} {axis} {fmt(run)}"
        self._track((x0, y0), be)
        font, size = self.font
        sec0 = "~".join(
            [
                "P",
                "show",
                ptype,
                spice if spice is not None else number,
                fmt(x0),
                fmt(y0),
                "" if (empty_rot and rot == 0) else str(rot),
                self._id(),
                "0",
            ]
        )
        sec1 = f"{fmt(x0)}~{fmt(y0)}"
        sec2 = f"{path}~{line_color}"

        def label(show: bool, pos: tuple[float, float, int, str], text: str) -> str:
            fields = [
                "1" if show else "0",
                fmt(pos[0]),
                fmt(pos[1]),
                str(pos[2]),
                text,
                pos[3],
                font,
                size,
                text_color,
            ]
            return "~".join(fields)

        sec3 = label(show_name, name_t, name)
        sec4 = label(show_num, num_t, number)
        sec5 = f"{'1' if dot else '0'}~{fmt(dot_c[0])}~{fmt(dot_c[1])}"
        sec6 = f"{'1' if clk else '0'}~{clk_path}"
        self._emit("P", "^^".join([sec0, sec1, sec2, sec3, sec4, sec5, sec6]))
        self._pins.append(
            {
                "number": number,
                "name": name,
                "abs": (x0, y0),
                "rot": rot,
                "ptype": ptype,
                "length": ln,
                "inverted": dot,
                "clock": clk,
            }
        )

    # -- result --------------------------------------------------------------

    def finish(self) -> SymbolUnit:
        xs = [p[0] for p in self._pts]
        ys = [p[1] for p in self._pts]
        ox = snap5((min(xs) + max(xs)) / 2)
        oy = snap5((min(ys) + max(ys)) / 2)
        hw = math.ceil((max(ox - min(xs), max(xs) - ox) + 1.0) * 2) / 2
        hh = math.ceil((max(oy - min(ys), max(ys) - oy) + 1.0) * 2) / 2
        bbox = {
            "x": nint(ox - hw),
            "y": nint(oy - hh),
            "width": nint(2 * hw),
            "height": nint(2 * hh),
        }
        fr = Frame(ox, oy)
        pins = []
        for p in self._pins:
            px, py = fr.mm(*p["abs"])
            pins.append(
                {
                    "number": p["number"],
                    "name": p["name"],
                    "x_mm": px,
                    "y_mm": py,
                    "angle_deg": (p["rot"] + 180) % 360,
                    "direction": PIN_DIRECTIONS[p["ptype"]],
                    "length_mm": r6(p["length"] * UNIT),
                    "inverted": p["inverted"],
                    "clock": p["clock"],
                }
            )
        arcs = []
        for a in self._arcs:
            arcs.append(
                {
                    "cx_mm": fr.mm(*a["abs_center"])[0],
                    "cy_mm": fr.mm(*a["abs_center"])[1],
                    "radius_mm": r6(a["radius_ee"] * UNIT),
                    "start_deg": a["start_deg"],
                    "sweep_deg": a["sweep_deg"],
                    "large_arc": a["large_arc"],
                    "svg_sweep_flag": a["svg_sweep_flag"],
                    "start_mm": list(fr.mm(*a["abs_start"])),
                    "end_mm": list(fr.mm(*a["abs_end"])),
                }
            )
        return SymbolUnit(self.shapes, (nint(ox), nint(oy)), bbox, pins, arcs, dict(self.counts))


def side_pins(
    sb: SymbolBuilder,
    defs: Sequence[tuple[Any, ...]],
    *,
    x: float,
    y: float,
    rot: int,
    step: float,
    horizontal_run: bool = False,
    length: float = 10,
    **kwargs: Any,
) -> None:
    """Place pins in a column (default) or a row. defs: (number, name, ptype[, flags dict])."""
    for i, d in enumerate(defs):
        number, name, ptype = d[0], d[1], d[2]
        flags: dict[str, Any] = d[3] if len(d) > 3 else {}
        px = x + i * step if horizontal_run else x
        py = y if horizontal_run else y + i * step
        sb.pin(number, name, px, py, rot, ptype, length=length, **{**kwargs, **flags})


# ----------------------------------------------------------------------------
# Document assembly
# ----------------------------------------------------------------------------


@dataclass
class Part:
    name: str
    index: int
    mpn: str
    prefix: str
    package: str
    description: str
    tags: list[str]
    manufacturer: str
    fp: FootprintBuilder
    units: list[SymbolBuilder]
    value: str | None = None
    fp_pre: str = "U?"
    layer101: str = "ComponentMarkingLayer"
    main_origin: tuple[float, float] = (0.0, 0.0)  # only used for multi-unit parts
    smt: bool = True
    part_class: str = "Basic Part"
    notes: list[str] = field(default_factory=list)

    @property
    def lcsc_id(self) -> str:
        return f"C{9000000 + self.index}"


def layers_table(layer101: str) -> list[str]:
    base = [
        (1, "TopLayer", "#FF0000", "true", "true", "true", ""),
        (2, "BottomLayer", "#0000FF", "true", "false", "true", ""),
        (3, "TopSilkLayer", "#FFCC00", "true", "false", "true", ""),
        (4, "BottomSilkLayer", "#66CC33", "true", "false", "true", ""),
        (5, "TopPasteMaskLayer", "#808080", "true", "false", "true", ""),
        (6, "BottomPasteMaskLayer", "#800000", "true", "false", "true", ""),
        (7, "TopSolderMaskLayer", "#800080", "true", "false", "true", "0.3"),
        (8, "BottomSolderMaskLayer", "#AA00FF", "true", "false", "true", "0.3"),
        (9, "Ratlines", "#6464FF", "true", "false", "true", ""),
        (10, "BoardOutLine", "#FF00FF", "true", "false", "true", ""),
        (11, "Multi-Layer", "#FFFFFF", "true", "false", "true", "0.5"),
        (12, "Document", "#FFFFFF", "true", "false", "true", ""),
        (13, "TopAssembly", "#33CC99", "true", "false", "true", ""),
        (14, "BottomAssembly", "#5555FF", "true", "false", "true", ""),
        (15, "Mechanical", "#F022F0", "true", "false", "true", ""),
        (19, "3DModel", "#66CCFF", "true", "false", "true", ""),
    ]
    rows = ["~".join(str(f) for f in row) for row in base]
    for i in range(1, 33):
        # Inner layers: derived colours, not copied from any EasyEDA sample.
        color = f"#{(i * 37) % 200 + 40:02X}{(i * 91) % 200 + 40:02X}{(i * 53) % 200 + 40:02X}"
        rows.append(f"{20 + i}~Inner{i}~{color}~false~false~false~~")
    rows.append("99~ComponentShapeLayer~#00CCCC~true~false~true~")
    rows.append("100~LeadShapeLayer~#CC9999~true~false~true~")
    rows.append(f"101~{layer101}~#66FFCC~true~false~true~")
    rows.append("Hole~Hole~#222222~false~false~true~")
    rows.append("DRCError~DRCError~#FAD609~false~false~true~")
    return rows


OBJECTS = [
    "All~true~false",
    "Component~true~true",
    "Prefix~true~true",
    "Name~true~false",
    "Track~true~true",
    "Pad~true~true",
    "Via~true~true",
    "Hole~true~true",
    "Copper_Area~true~true",
    "Circle~true~true",
    "Arc~true~true",
    "Solid_Region~true~true",
    "Text~true~true",
    "Image~true~true",
    "Rect~true~true",
    "Dimension~true~true",
    "Protractor~true~true",
]


def _owner() -> dict[str, Any]:
    return {
        "uuid": md5hex("syn", "owner"),
        "username": "synthetic",
        "nickname": "Synthetic",
        "avatar": "",
    }


def _unit_expected_pins(unit: SymbolUnit, index: int) -> list[dict[str, Any]]:
    return [{**p, "unit": index} for p in unit.pins]


def render(part: Part) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (document, expected) for one part."""
    sym_uuid = md5hex("syn", part.name, "symbol")
    fp_uuid = md5hex("syn", part.name, "footprint")
    units = [u.finish() for u in part.units]
    multi = len(units) > 1
    lcsc = part.lcsc_id
    num = int(lcsc[1:])

    def sym_cpara(pre: str, subpart_no: str | None) -> dict[str, Any]:
        c: dict[str, Any] = {"pre": pre, "name": part.mpn, "package": part.package}
        if part.value is not None:
            c["nameAlias"] = "Value"
        c["Supplier"] = "LCSC"
        c["Manufacturer"] = part.manufacturer
        c["Manufacturer Part"] = part.mpn
        c["Supplier Part"] = lcsc
        if part.value is not None:
            c["Value"] = part.value
        c["JLCPCB Part Class"] = part.part_class
        if subpart_no is not None:
            c["subpart_no"] = subpart_no
        return c

    def sym_head(
        x: float, y: float, c_para: dict[str, Any], doc_type: str, uuid: str
    ) -> dict[str, Any]:
        return {
            "docType": doc_type,
            "editorVersion": EDITOR_VERSION,
            "c_para": c_para,
            "x": x,
            "y": y,
            "puuid": fp_uuid,
            "uuid": uuid,
            "utime": FIXED_TIME,
            "importFlag": 0,
            "c_spiceCmd": None,
            "hasIdFlag": True,
            "license": LICENSE_TEXT,
        }

    def sym_canvas(x: float, y: float) -> str:
        return f"CA~1000~1000~#FFFFFF~yes~#CCCCCC~5~1000~1000~line~5~pixel~5~{fmt(x)}~{fmt(y)}"

    pre = part.prefix + "?"
    subparts: list[dict[str, Any]] = []
    if multi:
        ox, oy = part.main_origin
        sym_data: dict[str, Any] = {
            "head": sym_head(ox, oy, sym_cpara(pre, "1"), "2", sym_uuid),
            "canvas": sym_canvas(ox, oy),
            "shape": [],
            "BBox": {"x": 0, "y": 0, "width": 0, "height": 0},
            "colors": [],
        }
        for k, u in enumerate(units, start=1):
            sub_uuid = md5hex("syn", part.name, "unit", str(k))
            subparts.append(
                {
                    "uuid": sub_uuid,
                    "title": f"{part.mpn}.{k}",
                    "dataStr": {
                        "head": sym_head(
                            u.origin[0], u.origin[1], sym_cpara(f"{pre}.{k}", str(k)), "6", sub_uuid
                        ),
                        "canvas": sym_canvas(*u.origin),
                        "shape": u.shapes,
                        "BBox": u.bbox,
                        "colors": [],
                    },
                    "updateTime": FIXED_TIME,
                    "description": "",
                    "datastrid": md5hex("syn", part.name, "unit-data", str(k)),
                    "thumb": f"{EXAMPLE_HOST}/{sub_uuid}.png",
                }
            )
    else:
        u = units[0]
        sym_data = {
            "head": sym_head(u.origin[0], u.origin[1], sym_cpara(pre, None), "2", sym_uuid),
            "canvas": sym_canvas(*u.origin),
            "shape": u.shapes,
            "BBox": u.bbox,
            "colors": [],
        }

    fp = part.fp
    fp_head = {
        "docType": "4",
        "editorVersion": EDITOR_VERSION,
        "c_para": {
            "package": part.package,
            "pre": part.fp_pre,
            "Contributor": "synthetic",
            "3DModel": f"SYN-{part.name}-3D",
            "link": f"https://example.invalid/synthetic/{part.name}",
        },
        "hasIdFlag": True,
        "x": fp.frame.ox,
        "y": fp.frame.oy,
        "utime": FIXED_TIME,
        "uuid": fp_uuid,
        "importFlag": 0,
        "transformList": "",
        "newgId": True,
        "uuid_3d": md5hex("syn", part.name, "3d"),
        "license": LICENSE_TEXT,
    }
    fp_canvas = (
        "CA~1000~1000~#000000~yes~#FFFFFF~10~1000~1000~line~0.03937~mm~1~45~visible~0.5~"
        f"{fmt(fp.frame.ox)}~{fmt(fp.frame.oy)}~0~none"
    )
    package_detail = {
        "uuid": fp_uuid,
        "title": part.package,
        "docType": 4,
        "updateTime": FIXED_TIME,
        "owner": _owner(),
        "datastrid": md5hex("syn", part.name, "fp-data"),
        "writable": False,
        "dataStr": {
            "head": fp_head,
            "canvas": fp_canvas,
            "shape": fp.shapes,
            "layers": layers_table(part.layer101),
            "objects": OBJECTS,
            "BBox": fp.bbox(),
            "netColors": [],
        },
    }

    result: dict[str, Any] = {
        "uuid": sym_uuid,
        "title": part.mpn,
        "description": part.description,
        "docType": 2,
        "type": 3,
        "thumb": f"{EXAMPLE_HOST}/{sym_uuid}.png",
        "szlcsc": {
            "id": num,
            "number": lcsc,
            "step": 1,
            "min": 1,
            "price": 0.01,
            "stock": 0,
            "url": f"https://example.invalid/synthetic/szlcsc/{lcsc}",
            "image": "",
        },
        "lcsc": {
            "id": num,
            "number": lcsc,
            "step": 1,
            "min": 1,
            "price": 0.01,
            "stock": 1000,
            "url": f"https://example.invalid/synthetic/lcsc/{lcsc}",
        },
        "owner": _owner(),
        "tags": part.tags,
        "updateTime": FIXED_TIME,
        "updated_at": "2026-01-01 00:00:00",
        "dataStr": sym_data,
        "verify": True,
        "SMT": part.smt,
        "datastrid": md5hex("syn", part.name, "sym-data"),
        "jlcOnSale": 1,
    }
    if multi:
        result["subparts"] = subparts
    result["writable"] = False
    result["isFavorite"] = False
    result["packageDetail"] = package_detail
    document = {"success": True, "code": 0, "result": result}

    all_pins: list[dict[str, Any]] = []
    unit_records: list[dict[str, Any]] = []
    for k, u in enumerate(units, start=1):
        all_pins.extend(_unit_expected_pins(u, k))
        unit_records.append(
            {
                "unit": k,
                "title": f"{part.mpn}.{k}" if multi else part.mpn,
                "origin_ee": list(u.origin),
                "pin_count": len(u.pins),
                "arcs": u.arcs,
                "shape_counts": u.counts,
            }
        )
    numbers: list[str] = []
    for p in fp.pads:
        if p["number"] not in numbers:
            numbers.append(p["number"])
    expected = {
        "name": part.name,
        "synthetic": True,
        "lcsc_id": lcsc,
        "mpn": part.mpn,
        "prefix": part.prefix,
        "package_name": part.package,
        "description": part.description,
        "manufacturer": part.manufacturer,
        "value": part.value,
        "unit_count": len(units),
        "footprint_origin_ee": [fp.frame.ox, fp.frame.oy],
        "pins": all_pins,
        "symbol_units": unit_records,
        "pad_count": len(fp.pads),
        "unique_pad_numbers": len(numbers),
        "pad_numbers": numbers,
        "pads": fp.pads,
        "holes": fp.holes,
        "bbox_mm": fp.pad_bbox_mm(),
        "footprint_shapes": {
            "counts": fp.counts,
            "tracks": fp.tracks,
            "circles": fp.circles,
            "arcs": fp.arcs,
            "rects": fp.rects,
            "solid_regions": fp.regions,
            "texts": fp.texts,
        },
        "arc_tolerance_mm": 0.001,
        "notes": part.notes,
    }
    if fp.model3d is not None:
        expected["model3d"] = fp.model3d
    return document, expected


# ----------------------------------------------------------------------------
# Part definitions
# ----------------------------------------------------------------------------


def part_r0603() -> Part:
    fp = FootprintBuilder((4000, 3000), tail=("-393.7008", "0.2000"))
    fp.add_pad(Pad("1", -0.85, 0, 0.9, 1.0, vertex_shift=0))
    fp.add_pad(Pad("2", 0.85, 0, 0.9, 1.0, vertex_shift=0))
    fp.add_track([(-0.3, 0.8), (0.3, 0.8)])
    fp.add_track([(-0.3, -0.8), (0.3, -0.8)])
    fp.add_track([(-1.0, 0.85), (-1.55, 0.85), (-1.55, -0.85), (-1.0, -0.85)])
    fp.add_track([(1.0, 0.85), (1.55, 0.85), (1.55, -0.85), (1.0, -0.85)])
    fp.add_region([(-0.8, 0.4), (-0.8, -0.4), (0.8, -0.4), (0.8, 0.4)], 99)
    fp.add_svgnode("SYN-R0603-3D", native_mm=(1.6, 0.8))
    sb = SymbolBuilder((20, 0))
    sb.rect(-10, -4, 20, 8, "a")
    sb.pin("1", "1", -20, 0, 180, "0", show_name=False)
    sb.pin("2", "2", 20, 0, 0, "", show_name=False)
    return Part(
        "r0603",
        1,
        "SYN-R0603-10K",
        "R",
        "SYN-R0603",
        "Synthetic 10kΩ ±1% 0603 resistor",
        ["Resistors"],
        "SYN-Maker(示例)",
        fp,
        [sb],
        value="10kΩ",
        fp_pre="R?",
        layer101="ComponentPolarityLayer",
        notes=["pad vertex order variant: older editor (vertex_shift 0)"],
    )


def part_cap0603_arc() -> Part:
    fp = FootprintBuilder((4000, 3000), tail=("0", "0.2000"), zero_len="0.0000")
    fp.add_pad(Pad("1", -0.85, 0, 0.95, 1.0))
    fp.add_pad(Pad("2", 0.85, 0, 0.95, 1.0))
    half_w, half_h, rad = 1.5, 0.85, 0.3
    fp.add_track([(-half_w + rad, half_h), (half_w - rad, half_h)], sw=1)
    fp.add_track([(-half_w + rad, -half_h), (half_w - rad, -half_h)], sw=1)
    fp.add_track([(-half_w, half_h - rad), (-half_w, -half_h + rad)], sw=1)
    fp.add_track([(half_w, -half_h + rad), (half_w, half_h - rad)], sw=1)
    fp.add_arc(half_w - rad, half_h - rad, rad, 90, -90, sw=1)
    fp.add_arc(-half_w + rad, half_h - rad, rad, 180, -90, sw=1, style="compact")
    fp.add_arc(-half_w + rad, -half_h + rad, rad, 270, -90, sw=1)
    fp.add_arc(half_w - rad, -half_h + rad, rad, 0, -90, sw=1, style="compact")
    # Polarity mark: large arc, counter-clockwise (SVG large=1, sweep=0).
    fp.add_arc(-2.0, 0.0, 0.15, 90, 270, sw=0.4)
    sb = SymbolBuilder((0, 0))
    sb.line((-2, 8), (-2, -8), color="#A00000")
    sb.line((2, -8), (2, 8), color="#A00000")
    sb.line((-10, 0), (-2, 0), color="#A00000")
    sb.line((10, 0), (2, 0), color="#A00000")
    sb.pin("1", "1", -20, 0, 180, "0")
    sb.pin("2", "2", 20, 0, 0, "0")
    return Part(
        "cap0603_arc",
        2,
        "SYN-C0603-100NF",
        "C",
        "SYN-C0603",
        "Synthetic 100nF 50V 0603 capacitor",
        ["Capacitors"],
        "SYN-Maker",
        fp,
        [sb],
        value="100nF",
        fp_pre="C?",
        layer101="ComponentPolarityLayer",
        notes=["4 corner arcs (clockwise), 1 large counter-clockwise arc, both SVG path spellings"],
    )


def part_led0603() -> Part:
    fp = FootprintBuilder((4000.8828, 3003.1398), tail=("0", "0.2"))
    fp.add_pad(Pad("1", -0.8, 0, 0.9, 1.0))
    fp.add_pad(Pad("2", 0.8, 0, 0.9, 1.0))
    fp.add_circle(-1.55, 0.2, 0.1, 0.2, 101)
    fp.add_circle(-1.7, -0.5, 0.15, 0.1, 3)
    fp.add_track([(-0.3, 0.85), (0.3, 0.85)], sw=1)
    fp.add_track([(-0.3, -0.85), (0.3, -0.85)], sw=1)
    fp.add_region([(-0.9, 0.5), (-0.9, -0.5), (-0.5, 0.0)], 12, style="compact")
    fp.add_region([(-0.8, 0.4), (0.8, 0.4), (0.8, -0.4), (-0.8, -0.4)], 99)
    fp.add_region([(0.2, 0.2), (0.6, 0.2), (0.6, -0.2), (0.2, -0.2)], 100, kind="cutout")
    fp.add_text("P", 0.0, -1.6, 3, "LED?")
    sb = SymbolBuilder((0, -5))
    sb.circle(0, 0, 14)
    sb.filled_path((5, -7), (5, 7), (-6, 0))
    sb.line((-6, -7), (-6, 7))
    sb.line((-10, 0), (-6, 0))
    sb.line((5, 0), (10, 0))
    sb.line((3, -6), (9, -12))
    sb.line((8, -6), (14, -12))
    sb.pin("1", "K", -20, 0, 180, "0")
    sb.pin("2", "A", 20, 0, 0, "0")
    return Part(
        "led0603",
        3,
        "SYN-LED0603-R",
        "LED",
        "SYN-LED0603",
        "Synthetic red LED 0603",
        ["Light Emitting Diodes (LED)"],
        "SYN-Maker",
        fp,
        [sb],
        fp_pre="U?",
        layer101="ComponentPolarityLayer",
        notes=[
            "symbol uses C, PT and PL shapes; footprint has TEXT (layout from docs, no real sample)"
        ],
    )


def part_sot23() -> Part:
    fp = FootprintBuilder((4000, 3000), tail=("0", "0.2000"), zero_len="0.0000")
    fp.add_pad(Pad("1", -0.95, -1.0, 0.8, 1.0, vertex_shift=1))
    fp.add_pad(Pad("2", 0.95, -1.0, 0.8, 1.0))
    fp.add_pad(Pad("3", 0.0, 1.0, 0.8, 1.0))
    fp.add_track([(-1.5, 0.3), (-1.5, 0.45), (-0.5, 0.45)])
    fp.add_track([(1.5, 0.3), (1.5, 0.45), (0.5, 0.45)])
    fp.add_track([(-1.5, -0.3), (-1.5, 0.3)])
    fp.add_track([(1.5, -0.3), (1.5, 0.3)])
    fp.add_circle(-1.9, -1.6, 0.12, 0.2, 101)
    fp.add_region([(-1.4, 0.65), (1.4, 0.65), (1.4, -0.65), (-1.4, -0.65)], 99)
    sb = SymbolBuilder((400, 300))
    sb.pin("1", "B", -20, 0, 180, "1")
    sb.pin("3", "C", 10, -20, 90, "3")
    sb.pin("2", "E", 10, 20, 270, "0")
    sb.line((-10, 0), (-2, 0))
    sb.line((-2, 3), (10, 10))
    sb.line((-2, -9), (-2, 9))
    sb.line((-2, -3), (10, -10))
    sb.polygon((10, 10), (6, 5), (4, 9))
    return Part(
        "sot23",
        4,
        "SYN-MMBT-NPN",
        "Q",
        "SYN-SOT-23-3",
        "Synthetic NPN transistor SOT-23",
        ["Transistors"],
        "SYN-Maker",
        fp,
        [sb],
        fp_pre="U?",
        notes=[
            "symbol with PG arrow head, pins listed B, C, E while numbers are 1, 3, 2",
            "hole_length written as 0.0000",
        ],
    )


def part_soic8() -> Part:
    fp = FootprintBuilder((4000, 3000), tail=("0", "0.2000"), zero_len="0.0000")
    pitch = 1.27
    for i in range(4):
        fp.add_pad(Pad(str(i + 1), (i - 1.5) * pitch, -2.7, 0.6, 1.6, shape="OVAL"))
    for i in range(4):
        fp.add_pad(Pad(str(8 - i), (i - 1.5) * pitch, 2.7, 0.6, 1.6, shape="OVAL"))
    fp.add_track([(-2.45, 1.95), (2.45, 1.95)], sw=1)
    fp.add_track([(-2.45, -1.95), (2.45, -1.95)], sw=1)
    fp.add_track([(-2.45, 1.95), (-2.45, 0.5)], sw=1)
    fp.add_track([(-2.45, -0.5), (-2.45, -1.95)], sw=1)
    fp.add_track([(2.45, -1.95), (2.45, 1.95)], sw=1)
    fp.add_arc(-2.45, 0.0, 0.5, 90, -180, sw=1, style="compact")
    fp.add_circle(-2.9, -3.7, 0.12, 0.2, 101)
    fp.add_region([(-2.4, 1.9), (2.4, 1.9), (2.4, -1.9), (-2.4, -1.9)], 99)
    fp.add_region([(-2.4, 1.9), (-1.8, 1.9), (-1.8, 1.5), (-2.4, 1.5)], 100, kind="cutout")
    sb = SymbolBuilder((400, 300), font=("Tahoma", "5.8pt"))
    sb.rect(-20, -25, 40, 50, "b")
    left = [
        ("1", "VDD", "4"),
        ("2", "CLK", "1", {"clk": True}),
        ("3", "RST", "1", {"dot": True}),
        ("4", "GND", "4"),
    ]
    right = [
        ("8", "NC", ""),
        ("7", "IO", "3"),
        ("6", "DO", "2"),
        ("5", "DI", "1", {"dot": True, "clk": True}),
    ]
    side_pins(sb, left, x=-30, y=-15, rot=180, step=10)
    side_pins(sb, right, x=30, y=-15, rot=0, step=10)
    return Part(
        "soic8",
        5,
        "SYN-SOIC8-LOGIC",
        "U",
        "SYN-SOIC-8",
        "Synthetic logic IC SOIC-8",
        ["Logic ICs"],
        "SYN-Maker",
        fp,
        [sb],
        notes=["pin 3 inverted, pin 2 clock, pin 5 inverted and clock, pin 8 empty type"],
    )


def part_qfn16_ep() -> Part:
    pitch, ctr = 0.5, 1.45
    # Origin deliberately off the exposed pad centre.
    fp = FootprintBuilder((4012.5, 2994.25), tail=("0", "0.1969"))
    pl, ps = 0.85, 0.3
    for i in range(4):  # left side, top to bottom, pins 1 to 4
        fp.add_pad(Pad(str(1 + i), -ctr, (1.5 - i) * pitch, pl, ps))
    for i in range(4):  # bottom side, left to right, pins 5 to 8
        fp.add_pad(Pad(str(5 + i), (i - 1.5) * pitch, -ctr, pl, ps, rot=90))
    for i in range(4):  # right side, bottom to top, pins 9 to 12
        fp.add_pad(Pad(str(9 + i), ctr, (i - 1.5) * pitch, pl, ps, rot=180))
    for i in range(4):  # top side, right to left, pins 13 to 16
        fp.add_pad(Pad(str(13 + i), (1.5 - i) * pitch, ctr, pl, ps, rot=270))
    fp.add_pad(Pad("17", 0.0, 0.0, 1.7, 1.7))
    for sx in (-1, 1):
        for sy in (-1, 1):
            fp.add_track([(sx * 1.7, sy * 1.2), (sx * 1.7, sy * 1.7), (sx * 1.2, sy * 1.7)], sw=1)
    fp.add_circle(-2.2, 2.2, 0.12, 0.2, 101)
    fp.add_region([(-1.5, 1.5), (1.5, 1.5), (1.5, -1.5), (-1.5, -1.5)], 99)
    sb = SymbolBuilder((400, 300), font=("Tahoma", "5.8pt"))
    sb.rect(-30, -30, 60, 60, "a")
    left = [
        ("1", "VDD", "4"),
        ("2", "IN1", "1"),
        ("3", "IN2", "1", {"dot": True}),
        ("4", "GND", "4"),
    ]
    side_pins(sb, left, x=-40, y=-15, rot=180, step=10)
    for number, name, x in (
        ("5", "OUT1", -20),
        ("6", "OUT2", -10),
        ("17", "GND", 0),
        ("7", "OUT3", 10),
        ("8", "OUT4", 20),
    ):
        sb.pin(number, name, x, 40, 270, "4" if number == "17" else "2")
    right = [(str(n), f"IO{n}", "3") for n in (12, 11, 10, 9)]
    side_pins(sb, right, x=40, y=-15, rot=0, step=10)
    top = [
        ("16", "VDD", "4"),
        ("15", "IO15", "3"),
        ("14", "IO14", "3"),
        ("13", "CLK", "1", {"clk": True}),
    ]
    side_pins(sb, top, x=-15, y=-40, rot=90, step=10, horizontal_run=True)
    return Part(
        "qfn16_ep",
        6,
        "SYN-QFN16-EP",
        "U",
        "SYN-QFN-16-EP",
        "Synthetic QFN-16 with exposed pad",
        ["Microcontrollers"],
        "SYN-Maker",
        fp,
        [sb],
        notes=[
            "exposed pad 17 is a RECT and shares its number with symbol pin 17",
            "side pads use rotations 0, 90, 180, 270 with non-square sizes",
            "footprint origin is off the exposed pad centre",
            "pin names VDD and GND appear twice",
        ],
    )


def part_tqfp32() -> Part:
    fp = FootprintBuilder((4000, 3000), tail=("0", "0.2"))
    pitch, ctr, pl, ps = 0.8, 4.3, 1.5, 0.55
    for i in range(8):  # left, top to bottom
        fp.add_pad(Pad(str(1 + i), -ctr, (3.5 - i) * pitch, pl, ps))
    for i in range(8):  # bottom, left to right
        fp.add_pad(Pad(str(9 + i), (i - 3.5) * pitch, -ctr, pl, ps, rot=90))
    for i in range(8):  # right, bottom to top
        fp.add_pad(Pad(str(17 + i), ctr, (i - 3.5) * pitch, pl, ps, rot=180))
    for i in range(8):  # top, right to left
        fp.add_pad(Pad(str(25 + i), (3.5 - i) * pitch, ctr, pl, ps, rot=270))
    for sx in (-1, 1):
        for sy in (-1, 1):
            fp.add_track([(sx * 3.6, sy * 2.6), (sx * 3.6, sy * 3.6), (sx * 2.6, sy * 3.6)], sw=1)
    fp.add_circle(-3.9, 3.9, 0.2, 0.2, 101)
    fp.add_region([(-3.5, 3.5), (3.5, 3.5), (3.5, -3.5), (-3.5, -3.5)], 99)
    names: dict[int, tuple[str, str, dict[str, Any]]] = {
        4: ("VCC", "4", {}),
        5: ("GND", "4", {}),
        7: ("XTAL1", "1", {}),
        8: ("XTAL2", "2", {}),
        12: ("SCK", "1", {"clk": True}),
        20: ("AVCC", "4", {}),
        21: ("GND", "4", {}),
        29: ("RESET", "1", {"dot": True}),
    }
    sb = SymbolBuilder((400, 300), font=("Tahoma", "5.8pt"))
    sb.rect(-50, -50, 100, 100, "b")
    counter = 0

    def put(number: int, x: float, y: float, rot: int) -> None:
        nonlocal counter
        counter += 1
        name, ptype, flags = names.get(number, (f"P{number}", "3", {}))
        # spice_no is the emission index here, so it differs from the real pin number.
        sb.pin(str(number), name, x, y, rot, ptype, spice=str(counter), **flags)

    for i in range(8):  # bottom side first, to keep shape order different from numbering
        put(9 + i, -35 + 10 * i, 60, 270)
    for i in range(8):
        put(17 + i, 60, 35 - 10 * i, 0)
    for i in range(8):
        put(25 + i, 35 - 10 * i, -60, 90)
    for i in range(8):
        put(1 + i, -60, -35 + 10 * i, 180)
    return Part(
        "tqfp32",
        7,
        "SYN-TQFP32-MCU",
        "U",
        "SYN-TQFP-32",
        "Synthetic 8-bit MCU TQFP-32",
        ["Microcontrollers"],
        "SYN-Maker",
        fp,
        [sb],
        notes=[
            "symbol pin spice_no is the emission index, the real number is in section 4",
            "pin shape order differs from numeric order",
            "duplicate pin names GND (pins 5 and 21)",
        ],
    )


def part_header1x6() -> Part:
    fp = FootprintBuilder((4000, 3000), tail=("0", "0.2"))
    for i in range(6):
        x = (i - 2.5) * 2.54
        if i == 0:
            fp.add_pad(Pad("1", x, 0, 1.8, 1.8, shape="RECT", layer=11, drill=1.0))
        else:
            rot = 90.0 if i == 2 else 0.0
            fp.add_pad(
                Pad(str(i + 1), x, 0, 1.8, 1.8, shape="ELLIPSE", layer=11, drill=1.0, rot=rot)
            )
    fp.add_track([(-7.62, -1.27), (7.62, -1.27), (7.62, 1.27), (-7.62, 1.27), (-7.62, -1.27)], sw=1)
    fp.add_track([(-6.35, 1.8), (-5.6, 1.8)], sw=1)
    fp.add_region([(-7.5, 1.1), (7.5, 1.1), (7.5, -1.1), (-7.5, -1.1)], 99)
    sb = SymbolBuilder((400, 300), path_style="compact")
    sb.rect(-10, -30, 20, 60, "b")
    sb.ellipse(-5, -25, 1.5, 1.5)
    for i in range(6):
        sb.pin(str(i + 1), str(i + 1), -20, -25 + 10 * i, 180, "0")
    return Part(
        "header1x6",
        8,
        "SYN-HDR-1X6",
        "H",
        "SYN-HDR-TH-1X6-P2.54",
        "Synthetic 1x6 pin header 2.54mm",
        ["Pin Headers"],
        "SYN-Maker",
        fp,
        [sb],
        smt=False,
        part_class="Extended Part",
        notes=[
            "through hole pads on layer 11: RECT pad 1, ELLIPSE pads, pad 3 has rotation 90",
            "symbol pin paths use the compact spelling M x,y h10",
        ],
    )


def part_usbc_like() -> Part:
    fp = FootprintBuilder((3999.5, 3003.25), tail=("0", "0.2"))
    row_y = 1.2
    power_off = [(-0.3, -0.55), (0.3, -0.55), (0.3, 0.35), (0.1, 0.55), (-0.1, 0.55), (-0.3, 0.35)]
    layout = [
        ("A1B12", -3.3, "P"),
        ("A4B9", -2.5, "P"),
        ("B8", -1.75, "S"),
        ("A5", -1.25, "S"),
        ("B7", -0.75, "S"),
        ("A6", -0.25, "S"),
        ("A7", 0.25, "S"),
        ("B6", 0.75, "S"),
        ("A8", 1.25, "S"),
        ("B5", 1.75, "S"),
        ("B4A9", 2.5, "P"),
        ("B1A12", 3.3, "P"),
    ]
    for number, x, kind in layout:
        if kind == "S":
            fp.add_pad(Pad(number, x, row_y, 0.3, 1.1))
            continue
        offsets = list(power_off)
        if number == "A1B12":
            offsets.insert(4, offsets[3])  # real polygons repeat consecutive vertices
        poly = tuple((x + dx, row_y + dy) for dx, dy in offsets)
        fp.add_pad(Pad(number, shape="POLYGON", poly=poly))
    for number, x, y, h, slot in (
        ("S1", -4.3, 0.1, 2.0, 1.6),
        ("S2", 4.3, 0.1, 2.0, 1.6),
        ("S3", -4.3, -3.2, 1.8, 1.2),
        ("S4", 4.3, -3.2, 1.8, 1.2),
    ):
        fp.add_pad(Pad(number, x, y, 1.0, h, shape="OVAL", layer=11, drill=0.6, slot=slot))
    fp.add_hole(-2.9, -1.2, 1.0)
    fp.add_hole(2.9, -1.2, 1.0)
    fp.add_track([(-4.0, -4.6), (4.0, -4.6), (4.0, -4.1)], sw=1)
    fp.add_track([(-4.0, -4.1), (-4.0, -4.6)], sw=1)
    fp.add_track([(-3.5, 2.6), (3.5, 2.6)], sw=1)
    fp.add_circle(-3.9, 2.2, 0.15, 0.2, 101)
    fp.add_region([(-4.2, 2.4), (4.2, 2.4), (4.2, -4.4), (-4.2, -4.4)], 99)
    sb = SymbolBuilder((530, 315))
    sb.rect(-25, -65, 50, 130, "a")
    left = [
        ("A1B12", "GND", "4"),
        ("A4B9", "VBUS", "4"),
        ("B8", "SBU2", "3"),
        ("A5", "CC1", "3"),
        ("B7", "DN2", "3"),
        ("A6", "DP1", "3"),
        ("A7", "DN1", "3"),
        ("B6", "DP2", "3"),
        ("A8", "SBU1", "3"),
        ("B5", "CC2", "3"),
        ("B4A9", "VBUS", "4"),
        ("B1A12", "GND", "4"),
    ]
    side_pins(sb, left, x=-35, y=-55, rot=180, step=10)
    shield = [(f"S{n}", "SHIELD", "0") for n in range(1, 5)]
    side_pins(sb, shield, x=35, y=-15, rot=0, step=10, empty_rot=True)
    return Part(
        "usbc_like",
        9,
        "SYN-USBC-16P",
        "USBC",
        "SYN-USB-C-SMD-16P",
        "Synthetic USB Type-C receptacle, 16 pin",
        ["USB Connectors"],
        "SYN-Maker",
        fp,
        [sb],
        part_class="Extended Part",
        notes=[
            "alphanumeric pad numbers",
            "4 POLYGON SMD pads, the first one repeats a vertex",
            "4 slotted OVAL through hole pads (hole_length and slot_outline)",
            "2 non-plated HOLE shapes",
            "shield pins use an empty pin rotation field (means 0)",
            "pin names VBUS, GND, SHIELD repeat",
        ],
    )


def part_module_dup() -> Part:
    fp = FootprintBuilder((3987.5, 3012.125), tail=("0", "0.2"))
    pad_list: list[Pad] = []
    for i in range(8):  # left, top to bottom, pads 1 to 8
        pad_list.append(Pad(str(1 + i), -7.2, (3.5 - i) * 1.5, 1.8, 0.9))
    for i in range(8):  # bottom, left to right, pads 9 to 16
        pad_list.append(Pad(str(9 + i), (i - 3.5) * 1.5, -6.45, 1.8, 0.9, rot=90))
    for i in range(8):  # right, bottom to top, pads 17 to 24
        pad_list.append(Pad(str(17 + i), 7.2, (i - 3.5) * 1.5, 1.8, 0.9, rot=180))
    ground = [
        Pad("25", -1.5, 1.5, 2.2, 2.2),
        Pad("25", 1.5, 1.5, 2.2, 2.2, rot=90),
        Pad("25", -1.5, -1.5, 2.2, 2.2),
        Pad("25", 1.5, -1.5, 2.2, 2.2, rot=90),
    ]
    # Interleave the duplicates with the numbered pads.
    order = [*pad_list[:3], ground[0], *pad_list[3:10], ground[1], *pad_list[10:20]]
    order += [ground[2], *pad_list[20:], ground[3]]
    for pad in order:
        fp.add_pad(pad)
    fp.add_track([(-7.5, 7.5), (7.5, 7.5), (7.5, 6.5)], sw=1)
    fp.add_track([(-7.5, 6.5), (-7.5, 7.5)], sw=1)
    fp.add_track([(-7.5, -7.5), (7.5, -7.5)], sw=1)
    fp.add_region([(-7.5, 7.5), (7.5, 7.5), (7.5, -7.5), (-7.5, -7.5)], 99)
    sb = SymbolBuilder((440, 275), font=("Tahoma", "5.8pt"))
    sb.rect(-40, -65, 80, 130, "b")
    sb.ellipse(-35, -60, 1.5, 1.5)
    sb.text(0, -70, "SYN-MODULE")
    sb.text(0, 80, "Synthetic")
    left_names: dict[int, tuple[str, str, dict[str, Any]]] = {
        1: ("EN", "1", {"dot": True}),
        2: ("VDD", "4", {}),
        3: ("GND", "4", {}),
    }
    for i in range(12):
        n = 1 + i
        name, ptype, flags = left_names.get(n, (f"IO{n}", "3", {}))
        sb.pin(str(n), name, -50, -55 + 10 * i, 180, ptype, **flags)
    for i in range(12):
        n = 13 + i
        sb.pin(str(n), f"IO{n}", 50, -55 + 10 * i, 0, "3")
    sb.pin("25", "GND", 0, 75, 270, "4")
    return Part(
        "module_dup",
        10,
        "SYN-MODULE-WIFI",
        "U",
        "SYN-MODULE-SMD-15X15",
        "Synthetic wireless module with split ground pad",
        ["Modules"],
        "SYN-Maker",
        fp,
        [sb],
        part_class="Extended Part",
        notes=[
            "ground pad is split in 4 pads that all carry number 25, interleaved in the shape list",
            "28 pads, 25 unique pad numbers, 25 symbol pins",
            "two of the duplicate pads have rotation 90",
            "symbol has T text shapes",
        ],
    )


def part_xtal4() -> Part:
    fp = FootprintBuilder((4000, 3000.0005), tail=("0", "0.2000"), zero_len="0.0000")
    fp.add_pad(Pad("1", -1.1, -0.95, 1.4, 1.15, vertex_shift=0))
    fp.add_pad(Pad("2", 1.1, -0.95, 1.4, 1.15, vertex_shift=0))
    fp.add_pad(Pad("3", 1.1, 0.95, 1.4, 1.15, vertex_shift=0))
    fp.add_pad(Pad("4", -1.1, 0.95, 1.4, 1.15, vertex_shift=0))
    fp.add_track([(-1.7, 1.75), (1.7, 1.75), (1.7, 1.6)])
    fp.add_track([(-1.7, -1.75), (1.7, -1.75), (1.7, -1.6)])
    fp.add_track([(-1.7, 1.75), (-1.7, -1.75)])
    fp.add_circle(-2.2, -1.5, 0.12, 0.2, 101)
    fp.add_region([(-1.6, 1.25), (1.6, 1.25), (1.6, -1.25), (-1.6, -1.25)], 99)
    sb = SymbolBuilder((50, 35))
    sb.rect(-20, -20, 40, 40, "b")
    sb.line((-5, 8), (-5, -8), color="#880011")
    sb.line((5, 8), (5, -8), color="#880011")
    sb.line((-3, 8), (-3, -8), (3, -8), (3, 8), (-3, 8), color="#880011")
    sb.line((-20, 0), (-5, 0), color="#880011")
    sb.line((5, 0), (20, 0), color="#880011")
    sb.pin("1", "OSC1", -30, 0, 180, "0")
    sb.pin("3", "OSC2", 30, 0, 0, "0")
    sb.pin("2", "GND", -10, 30, 270, "0", text_color="#000000")
    sb.pin("4", "GND", 10, 30, 270, "0", text_color="#000000")
    return Part(
        "xtal4",
        11,
        "SYN-XTAL-3225-16M",
        "X",
        "SYN-XTAL-SMD-4P-3.2X2.5",
        "Synthetic 16MHz crystal, 4 pad",
        ["Crystals"],
        "SYN-Maker",
        fp,
        [sb],
        value="16MHz",
        fp_pre="X?",
        notes=["two symbol pins called GND (pads 2 and 4)", "footprint origin y has 4 decimals"],
    )


def part_inductor() -> Part:
    fp = FootprintBuilder((4000, 3000), tail=("0", "0.2"))
    fp.add_pad(Pad("1", -1.6, 0, 1.2, 1.8))
    fp.add_pad(Pad("2", 1.6, 0, 1.2, 1.8))
    fp.add_rect(-0.8, 0.8, 1.6, 1.6, 3, 0.4)
    fp.add_rect(-2.6, 1.2, 5.2, 2.4, 12, 0.2)
    fp.add_track([(-0.8, 1.2), (0.8, 1.2)], sw=1)
    fp.add_track([(-0.8, -1.2), (0.8, -1.2)], sw=1)
    fp.add_region([(-2.2, 1.0), (2.2, 1.0), (2.2, -1.0), (-2.2, -1.0)], 99, style="compact")
    sb = SymbolBuilder((0, 0))
    for k in range(4):
        sb.arc(-15 + 10 * k, 0, 5, 180, -180)
    sb.pin("1", "1", -30, 0, 180, "0")
    sb.pin("2", "2", 30, 0, 0, "0")
    return Part(
        "inductor",
        12,
        "SYN-IND-10UH",
        "L",
        "SYN-L1008",
        "Synthetic 10uH inductor",
        ["Inductors"],
        "SYN-Maker",
        fp,
        [sb],
        value="10uH",
        fp_pre="L?",
        notes=[
            "symbol drawn with 4 A arcs (semicircles); footprint has RECT shapes on layers 3 and 12"
        ],
    )


def part_tact4() -> Part:
    fp = FootprintBuilder((4000, 3000), tail=("0", "0.2"))
    fp.add_pad(Pad("1", -3.1, 2.0, 1.2, 1.9))
    fp.add_pad(Pad("2", 3.1, 2.0, 1.2, 1.9))
    # Pads 3 and 4 give the same copper through a non-square size plus rotation 90.
    fp.add_pad(Pad("3", -3.1, -2.0, 1.9, 1.2, rot=90))
    fp.add_pad(Pad("4", 3.1, -2.0, 1.9, 1.2, rot=90))
    octagon = [
        (-1.4, 2.6),
        (1.4, 2.6),
        (2.6, 1.4),
        (2.6, -1.4),
        (1.4, -2.6),
        (-1.4, -2.6),
        (-2.6, -1.4),
        (-2.6, 1.4),
        (-1.4, 2.6),
    ]
    fp.add_track(octagon, sw=1)
    fp.add_circle(0, 0, 1.6, 1, 3)
    fp.add_circle(-3.6, 3.2, 0.12, 0.2, 101)
    fp.add_region([(-2.5, 2.5), (2.5, 2.5), (2.5, -2.5), (-2.5, -2.5)], 99)
    sb = SymbolBuilder((400, 300))
    sb.line((-10, 15), (10, 15))
    sb.line((-10, -15), (10, -15))
    sb.ellipse(0, -8, 1.25, 1.25, fill="none")
    sb.ellipse(0, 8, 1.25, 1.25, fill="none")
    sb.line((0, -9), (0, -15))
    sb.line((0, 9), (0, 15))
    sb.line((-2, -9), (-5, 5))
    sb.pin("1", "A", -20, -15, 180, "0")
    sb.pin("2", "B", 20, -15, 0, "0")
    sb.pin("3", "C", -20, 15, 180, "0")
    sb.pin("4", "D", 20, 15, 0, "0")
    return Part(
        "tact4",
        13,
        "SYN-SW-TACT-4P",
        "SW",
        "SYN-SW-SMD-4P-5.1X5.1",
        "Synthetic tactile switch, 4 pad",
        ["Switches"],
        "SYN-Maker",
        fp,
        [sb],
        fp_pre="U?",
        notes=["pads 3 and 4 are 1.9 x 1.2 with rotation 90, copper equals pads 1 and 2"],
    )


def part_opamp2() -> Part:
    fp = FootprintBuilder((4000, 3000), tail=("0", "0.2000"))
    pitch = 0.65
    for i in range(4):
        fp.add_pad(Pad(str(1 + i), (i - 1.5) * pitch, -2.9, 0.4, 1.5))
    for i in range(4):
        fp.add_pad(Pad(str(8 - i), (i - 1.5) * pitch, 2.9, 0.4, 1.5))
    fp.add_track([(-1.7, 1.6), (1.7, 1.6)], sw=1)
    fp.add_track([(-1.7, -1.6), (1.7, -1.6)], sw=1)
    fp.add_circle(-2.2, -3.6, 0.1, 0.2, 101)
    fp.add_region([(-1.6, 1.5), (1.6, 1.5), (1.6, -1.5), (-1.6, -1.5)], 99)

    def amp(
        off: tuple[float, float],
        inv: tuple[str, str],
        non: tuple[str, str],
        out: str,
        flip: bool,
    ) -> SymbolBuilder:
        sb = SymbolBuilder(off)
        sb.line((-20, -25), (20, 0), (-20, 25), (-20, -25))
        sb.line((-16, -10), (-10, -10))
        sb.line((-16, 10), (-10, 10))
        sb.line((-13, 7), (-13, 13))
        top, bottom = (non, inv) if flip else (inv, non)
        sb.pin(top[0], top[1], -40, -10, 180, "1", length=20)
        sb.pin(bottom[0], bottom[1], -40, 10, 180, "1", length=20)
        sb.pin(out, "OUT", 40, 0, 0, "2", length=20)
        return sb

    unit1 = amp((100, 120), ("2", "IN-"), ("3", "IN+"), "1", False)
    unit2 = amp((100, 200), ("6", "IN-"), ("5", "IN+"), "7", True)
    power = SymbolBuilder((180, 160))
    power.rect(-10, -15, 20, 30, "a")
    power.pin("8", "V+", 0, -25, 90, "4")
    power.pin("4", "V-", 0, 25, 270, "4")
    return Part(
        "opamp2",
        14,
        "SYN-OPAMP2",
        "U",
        "SYN-TSSOP-8",
        "Synthetic dual op-amp, 2 amplifier units plus power unit",
        ["Operational Amplifiers"],
        "SYN-Maker",
        fp,
        [unit1, unit2, power],
        main_origin=(60, 40),
        notes=[
            "multi-unit: main shape list empty and BBox zero, units in subparts[]",
            "main head.x/y differs from the first sub-part on purpose",
            "unit 2 lists IN+ above IN- (pins 5 and 6), unit 1 the opposite",
            "unit pin length is 20 units",
        ],
    )


def part_padstress() -> Part:
    fp = FootprintBuilder((4000, 3000), tail=("0", "0.2"))
    fp.add_pad(Pad("1", -2.5, 2.0, 1.6, 0.8, rot=30))
    fp.add_pad(Pad("2", 0.0, 2.0, 1.6, 0.8, rot=90))
    fp.add_pad(Pad("3", 2.5, 2.0, 2.0, 0.8, shape="OVAL", rot=45))
    fp.add_pad(Pad("4", -2.5, 0.0, 1.4, 0.7, rot=270, layer=2))
    fp.add_pad(Pad("5", 0.0, 0.0, 1.2, 1.2, shape="ELLIPSE"))
    fp.add_pad(Pad("6", 2.5, 0.0, 1.8, 1.8, shape="ELLIPSE", layer=11, drill=0.8, plated=False))
    fp.add_pad(Pad("7", -2.5, -2.0, 1.8, 1.4, layer=11, drill=0.9, rot=15))
    fp.add_pad(Pad("8", 0.0, -2.0, 1.0, 2.0, shape="OVAL", rot=90))
    fp.add_track([(-4.0, 3.2), (4.0, 3.2), (4.0, -3.2), (-4.0, -3.2), (-4.0, 3.2)], sw=1)
    sb = SymbolBuilder((400, 300))
    sb.rect(-10, -40, 20, 80, "a")
    for i in range(8):
        sb.pin(str(i + 1), f"P{i + 1}", -20, -35 + 10 * i, 180, "0")
    return Part(
        "padstress",
        15,
        "SYN-PADSTRESS",
        "J",
        "SYN-PADSTRESS-8",
        "Synthetic pad shape stress test",
        ["Test Parts"],
        "SYN-Maker",
        fp,
        [sb],
        smt=False,
        notes=[
            "pad 1: RECT rotated 30 degrees, outline vertices include the rotation",
            "pad 2: RECT rotation 90, pad 4: RECT rotation 270 on the bottom layer",
            "pad 3: OVAL rotated 45 degrees, pad 8: OVAL rotated 90 degrees",
            "pad 5: round SMD ELLIPSE (no real sample, empty points assumed)",
            "pad 6: non-plated ELLIPSE with hole, pad 7: through hole RECT rotated 15 degrees",
        ],
    )


def _box3d_part(
    name: str,
    index: int,
    origin: tuple[float, float],
    description: str,
    notes: list[str],
    **model: Any,
) -> Part:
    """Two pad part on the Y axis with a 3D model node. `model` goes to add_svgnode."""
    mpn = f"SYN-{name.upper().replace('_', '-')}"
    fp = FootprintBuilder(origin, tail=("0", "0.2000"), zero_len="0.0000")
    fp.add_pad(Pad("1", 0.0, -1.0, 1.4, 0.9))
    fp.add_pad(Pad("2", 0.0, 1.0, 1.4, 0.9))
    fp.add_track([(-1.0, -0.3), (-1.0, 0.3)])
    fp.add_track([(1.0, -0.3), (1.0, 0.3)])
    fp.add_region([(-0.8, 1.6), (0.8, 1.6), (0.8, -1.6), (-0.8, -1.6)], 99)
    fp.add_svgnode(f"{mpn}-3D", **model)
    sb = SymbolBuilder((20, 0))
    sb.rect(-10, -4, 20, 8, "a")
    sb.pin("1", "1", -20, 0, 180, "0", show_name=False)
    sb.pin("2", "2", 20, 0, 0, "", show_name=False)
    return Part(
        name,
        index,
        mpn,
        "J",
        "SYN-BOX3D-1X2",
        description,
        ["Test Parts"],
        "SYN-Maker",
        fp,
        [sb],
        fp_pre="J?",
        notes=notes,
    )


def part_box3d_rot90() -> Part:
    return _box3d_part(
        "box3d_rot90",
        16,
        (4000, 3000),
        "Synthetic 3D model rotated by 90 degrees, off centre",
        ["3D model: rz 90 swaps the outline sizes, centre (0.508, -0.762) mm, z 0"],
        native_mm=(3.2, 1.6),
        centre_mm=(0.508, -0.762),
        rotation_deg=(0.0, 0.0, 90.0),
    )


def part_box3d_z() -> Part:
    return _box3d_part(
        "box3d_z",
        17,
        (3987.5, 3012.25),
        "Synthetic 3D model sunk into the board",
        ["3D model: z is -3.6 units (-0.9144 mm), sign kept, no Y flip; origin in another corner"],
        native_mm=(4.0, 2.0),
        centre_mm=(-0.254, 1.016),
        z_mm=-0.9144,
    )


def part_box3d_stale() -> Part:
    return _box3d_part(
        "box3d_stale",
        18,
        (4012.125, 2994.5),
        "Synthetic 3D model with an outdated c_origin",
        [
            "3D model: c_origin is 6.5 and -2 units away from the real centre, outline is right",
            "rz 180 and z +2 units (0.508 mm) on purpose",
        ],
        native_mm=(3.2, 2.5),
        centre_mm=(-0.508, 0.254),
        z_mm=0.508,
        rotation_deg=(0.0, 0.0, 180.0),
        stale_shift_ee=(6.5, -2.0),
    )


PARTS: list[Callable[[], Part]] = [
    part_r0603,
    part_cap0603_arc,
    part_led0603,
    part_sot23,
    part_soic8,
    part_qfn16_ep,
    part_tqfp32,
    part_header1x6,
    part_usbc_like,
    part_module_dup,
    part_xtal4,
    part_inductor,
    part_tact4,
    part_opamp2,
    part_padstress,
    part_box3d_rot90,
    part_box3d_z,
    part_box3d_stale,
]


# ----------------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------------


def to_json_bytes(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def build_all() -> dict[str, bytes]:
    """Return {file name: content} for every generated file, in memory."""
    out: dict[str, bytes] = {}
    for factory in PARTS:
        part = factory()
        document, expected = render(part)
        out[f"{part.name}.json"] = to_json_bytes(document)
        out[f"{part.name}.expected.json"] = to_json_bytes(expected)
    return out


def main(argv: Sequence[str]) -> int:
    files = build_all()
    if "--check" in argv:
        stale = [
            name
            for name, data in files.items()
            if not (OUT_DIR / name).exists() or (OUT_DIR / name).read_bytes() != data
        ]
        for name in stale:
            print(f"out of date: {name}")
        return 1 if stale else 0
    for name, data in files.items():
        (OUT_DIR / name).write_bytes(data)
    print(f"wrote {len(files)} files to {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
