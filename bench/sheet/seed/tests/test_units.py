import pytest

from sheet.units import to_points


def test_points_are_points():
    assert to_points(10, "pt") == 10


def test_an_inch_is_72_points():
    assert to_points(1, "in") == pytest.approx(72)
