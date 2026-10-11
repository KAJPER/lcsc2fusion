"""SVG path parsing and arc math for EasyEDA shapes.

EasyEDA stores arcs and free-form outlines as SVG path data in its own coordinate system
(Y axis down, EasyEDA units). Nothing here converts units or flips axes; callers do that
with parser.units.Transform. Angles in this module follow SVG conventions (Y down), except
`arc_curve_deg` which returns the Eagle `curve` value (Y up, counter-clockwise positive).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from lcsc2fusion.errors import ParseError

_ARG_COUNT = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "Q": 4, "A": 7, "Z": 0}


@dataclass(frozen=True)
class MoveTo:
    x: float
    y: float


@dataclass(frozen=True)
class LineTo:
    x: float
    y: float


@dataclass(frozen=True)
class CubicTo:
    x1: float
    y1: float
    x2: float
    y2: float
    x: float
    y: float


@dataclass(frozen=True)
class QuadTo:
    x1: float
    y1: float
    x: float
    y: float


@dataclass(frozen=True)
class ArcTo:
    rx: float
    ry: float
    x_axis_rotation: float
    large_arc: bool
    sweep: bool
    x: float
    y: float


@dataclass(frozen=True)
class ClosePath:
    pass


Segment = MoveTo | LineTo | CubicTo | QuadTo | ArcTo | ClosePath


class _Scanner:
    """Character scanner for SVG path data (handles compact forms like '1.5.5' and '011')."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def skip_separators(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos] in " \t\r\n,":
            self.pos += 1

    def at_end(self) -> bool:
        self.skip_separators()
        return self.pos >= len(self.text)

    def peek(self) -> str:
        self.skip_separators()
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def read_command(self) -> str:
        self.skip_separators()
        ch = self.text[self.pos]
        self.pos += 1
        return ch

    def read_number(self) -> float:
        self.skip_separators()
        start = self.pos
        text = self.text
        n = len(text)
        if self.pos < n and text[self.pos] in "+-":
            self.pos += 1
        digits = 0
        while self.pos < n and text[self.pos].isdigit():
            self.pos += 1
            digits += 1
        if self.pos < n and text[self.pos] == ".":
            self.pos += 1
            while self.pos < n and text[self.pos].isdigit():
                self.pos += 1
                digits += 1
        if digits == 0:
            raise ParseError(f"Bad number in SVG path at {start}: {text[start : start + 20]!r}")
        if self.pos < n and text[self.pos] in "eE":
            probe = self.pos + 1
            if probe < n and text[probe] in "+-":
                probe += 1
            if probe < n and text[probe].isdigit():
                while probe < n and text[probe].isdigit():
                    probe += 1
                self.pos = probe
        return float(text[start : self.pos])

    def read_flag(self) -> bool:
        self.skip_separators()
        if self.pos >= len(self.text) or self.text[self.pos] not in "01":
            raise ParseError(f"Bad arc flag in SVG path at {self.pos}")
        flag = self.text[self.pos] == "1"
        self.pos += 1
        return flag


def parse_path(data: str) -> list[Segment]:
    """Parse SVG path data into absolute segments (M L H V C Q A Z, upper and lower case).

    Smooth curves (S, T) are not produced by EasyEDA and raise ParseError.
    """
    scan = _Scanner(data)
    segments: list[Segment] = []
    cx = cy = 0.0  # current point
    sx = sy = 0.0  # subpath start
    cmd = ""
    while not scan.at_end():
        ch = scan.peek()
        if ch.isalpha():
            cmd = scan.read_command()
            if cmd.upper() not in _ARG_COUNT:
                raise ParseError(f"Unsupported SVG path command {cmd!r}")
        elif not cmd:
            raise ParseError("SVG path data must start with a command")
        upper = cmd.upper()
        rel = cmd.islower()
        if upper == "Z":
            segments.append(ClosePath())
            cx, cy = sx, sy
            cmd = ""
            continue
        ox, oy = (cx, cy) if rel else (0.0, 0.0)
        if upper == "M":
            x, y = scan.read_number() + ox, scan.read_number() + oy
            segments.append(MoveTo(x, y))
            cx, cy = sx, sy = x, y
            cmd = "l" if rel else "L"  # extra pairs after M are implicit lineto
        elif upper == "L":
            x, y = scan.read_number() + ox, scan.read_number() + oy
            segments.append(LineTo(x, y))
            cx, cy = x, y
        elif upper == "H":
            x = scan.read_number() + (cx if rel else 0.0)
            segments.append(LineTo(x, cy))
            cx = x
        elif upper == "V":
            y = scan.read_number() + (cy if rel else 0.0)
            segments.append(LineTo(cx, y))
            cy = y
        elif upper == "C":
            v = [scan.read_number() for _ in range(6)]
            segments.append(
                CubicTo(v[0] + ox, v[1] + oy, v[2] + ox, v[3] + oy, v[4] + ox, v[5] + oy)
            )
            cx, cy = v[4] + ox, v[5] + oy
        elif upper == "Q":
            v = [scan.read_number() for _ in range(4)]
            segments.append(QuadTo(v[0] + ox, v[1] + oy, v[2] + ox, v[3] + oy))
            cx, cy = v[2] + ox, v[3] + oy
        else:  # "A"
            rx, ry, rot = scan.read_number(), scan.read_number(), scan.read_number()
            large, sweep = scan.read_flag(), scan.read_flag()
            x, y = scan.read_number() + ox, scan.read_number() + oy
            segments.append(ArcTo(rx, ry, rot, large, sweep, x, y))
            cx, cy = x, y
    return segments


@dataclass(frozen=True)
class ArcCenter:
    """Centre parameterisation of an SVG arc (angles in radians, SVG Y-down frame)."""

    cx: float
    cy: float
    rx: float
    ry: float
    phi: float
    theta1: float
    delta: float  # signed sweep; positive when the SVG sweep flag is set


def arc_to_center(
    x1: float,
    y1: float,
    arc: ArcTo,
) -> ArcCenter | None:
    """Convert an endpoint arc to centre form (W3C SVG implementation notes F.6.5).

    Returns None for degenerate arcs (zero radius or identical endpoints). Radii that are
    too small are scaled up (F.6.6).
    """
    x2, y2 = arc.x, arc.y
    rx, ry = abs(arc.rx), abs(arc.ry)
    if rx == 0.0 or ry == 0.0 or (x1 == x2 and y1 == y2):
        return None
    phi = math.radians(arc.x_axis_rotation % 360.0)
    cos_phi, sin_phi = math.cos(phi), math.sin(phi)
    dx2, dy2 = (x1 - x2) / 2.0, (y1 - y2) / 2.0
    x1p = cos_phi * dx2 + sin_phi * dy2
    y1p = -sin_phi * dx2 + cos_phi * dy2
    lam = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry)
    if lam > 1.0:
        scale = math.sqrt(lam)
        rx, ry = rx * scale, ry * scale
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    coef = math.sqrt(max(0.0, num / den)) if den else 0.0
    if arc.large_arc == arc.sweep:
        coef = -coef
    cxp = coef * (rx * y1p / ry)
    cyp = coef * (-ry * x1p / rx)
    cx = cos_phi * cxp - sin_phi * cyp + (x1 + x2) / 2.0
    cy = sin_phi * cxp + cos_phi * cyp + (y1 + y2) / 2.0
    ux, uy = (x1p - cxp) / rx, (y1p - cyp) / ry
    vx, vy = (-x1p - cxp) / rx, (-y1p - cyp) / ry
    theta1 = math.atan2(uy, ux)
    delta = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
    if arc.sweep and delta < 0:
        delta += 2.0 * math.pi
    elif not arc.sweep and delta > 0:
        delta -= 2.0 * math.pi
    return ArcCenter(cx, cy, rx, ry, phi, theta1, delta)


def arc_curve_deg(x1: float, y1: float, arc: ArcTo) -> float:
    """Eagle `curve` value for an SVG arc given in EasyEDA (Y down) coordinates.

    The arc is treated as circular (EasyEDA footprint arcs are). Eagle measures the signed sweep
    from the start to the end point, positive counter-clockwise with Y up. The SVG sweep flag
    means clockwise on the Y-down screen, which stays clockwise after the Y flip, so
    sweep=1 gives a negative curve. Returns 0.0 for degenerate arcs.
    """
    center = arc_to_center(x1, y1, arc)
    if center is None:
        return 0.0
    return -math.degrees(center.delta)


def flatten_arc(
    x1: float, y1: float, arc: ArcTo, step_deg: float = 15.0
) -> list[tuple[float, float]]:
    """Polyline approximation of an arc (excluding the start point, including the end point)."""
    center = arc_to_center(x1, y1, arc)
    if center is None:
        return [(arc.x, arc.y)]
    steps = max(1, math.ceil(abs(math.degrees(center.delta)) / step_deg))
    cos_phi, sin_phi = math.cos(center.phi), math.sin(center.phi)
    points: list[tuple[float, float]] = []
    for i in range(1, steps + 1):
        angle = center.theta1 + center.delta * i / steps
        ex, ey = center.rx * math.cos(angle), center.ry * math.sin(angle)
        points.append(
            (
                cos_phi * ex - sin_phi * ey + center.cx,
                sin_phi * ex + cos_phi * ey + center.cy,
            )
        )
    points[-1] = (arc.x, arc.y)
    return points


@dataclass(frozen=True)
class SubPath:
    points: tuple[tuple[float, float], ...]
    closed: bool


def flatten_path(
    segments: list[Segment], arc_step_deg: float = 15.0, curve_steps: int = 8
) -> list[SubPath]:
    """Convert segments into polylines (arcs and Bezier curves are approximated)."""
    result: list[SubPath] = []
    current: list[tuple[float, float]] = []

    def flush(closed: bool) -> None:
        nonlocal current
        if len(current) > 1 or (current and closed):
            result.append(SubPath(tuple(current), closed))
        current = []

    for seg in segments:
        if isinstance(seg, MoveTo):
            flush(False)
            current = [(seg.x, seg.y)]
        elif isinstance(seg, ClosePath):
            flush(True)
        else:
            if not current:
                raise ParseError("SVG path draws before any moveto")
            px, py = current[-1]
            if isinstance(seg, LineTo):
                current.append((seg.x, seg.y))
            elif isinstance(seg, ArcTo):
                current.extend(flatten_arc(px, py, seg, arc_step_deg))
            elif isinstance(seg, CubicTo):
                for i in range(1, curve_steps + 1):
                    t = i / curve_steps
                    u = 1.0 - t
                    b0, b1, b2, b3 = u**3, 3 * u * u * t, 3 * u * t * t, t**3
                    current.append(
                        (
                            b0 * px + b1 * seg.x1 + b2 * seg.x2 + b3 * seg.x,
                            b0 * py + b1 * seg.y1 + b2 * seg.y2 + b3 * seg.y,
                        )
                    )
            else:  # QuadTo
                for i in range(1, curve_steps + 1):
                    t = i / curve_steps
                    u = 1.0 - t
                    current.append(
                        (
                            u * u * px + 2 * u * t * seg.x1 + t * t * seg.x,
                            u * u * py + 2 * u * t * seg.y1 + t * t * seg.y,
                        )
                    )
    flush(False)
    return result
