"""The ONLY place where EasyEDA units and axes are converted to the internal model.

EasyEDA: 1 unit = 10 mil = 0.254 mm, Y axis points down (SVG convention).
Internal model: millimetres, Y axis up (Eagle).
"""

from __future__ import annotations

from lcsc2fusion.constants import EE_UNIT_MM
from lcsc2fusion.model import Point

# Output precision: 0.1 micron. EasyEDA stores 4 decimals of a 10 mil unit (0.025 micron),
# so this only removes float noise such as 0.850011 for a 0.85 mm design value.
MM_DECIMALS = 4


def ee_to_mm(value: float) -> float:
    """Convert a length (no axis, no origin) from EasyEDA units to millimetres."""
    return value * EE_UNIT_MM


def ee_rotation_to_ccw(degrees: float) -> float:
    """Convert an EasyEDA rotation angle to a counter-clockwise (Y up) angle in [0, 360)."""
    # Verified on real pad outlines (tests/test_units.py): positive EasyEDA rotation is
    # counter-clockwise on screen, and a screen rotation stays counter-clockwise after the
    # Y flip, so the angle is unchanged (only normalised to [0, 360)).
    return degrees % 360.0


class Transform:
    """Maps absolute EasyEDA coordinates to millimetres relative to an origin, flipping Y."""

    def __init__(self, origin_x: float, origin_y: float) -> None:
        self.origin_x = origin_x
        self.origin_y = origin_y

    def point(self, x: float, y: float) -> Point:
        return Point(
            round((x - self.origin_x) * EE_UNIT_MM, MM_DECIMALS),
            round(-(y - self.origin_y) * EE_UNIT_MM, MM_DECIMALS),
        )

    def length(self, value: float) -> float:
        return round(value * EE_UNIT_MM, MM_DECIMALS)
