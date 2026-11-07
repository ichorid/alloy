"""Column-width units: widths are stored in points."""

_POINTS_PER = {"pt": 1.0, "in": 72.27, "cm": 28.3465, "px": 0.75}


def to_points(value: float, unit: str) -> float:
    """Convert a width in `unit` to points."""
    return value * _POINTS_PER[unit]
