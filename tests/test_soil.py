import numpy as np
import pytest

from geomon.ingest.soil import USDA_CLASSES, saxton_rawls, usda_texture


@pytest.mark.parametrize("sand,silt,clay,expected", [
    (92, 5, 3, "sand"),
    (82, 12, 6, "loamy sand"),
    (65, 25, 10, "sandy loam"),
    (40, 40, 20, "loam"),
    (20, 65, 15, "silt loam"),
    (5, 88, 7, "silt"),
    (60, 10, 30, "sandy clay loam"),
    (30, 36, 34, "clay loam"),
    (10, 56, 34, "silty clay loam"),
    (50, 5, 45, "sandy clay"),
    (5, 50, 45, "silty clay"),
    (20, 20, 60, "clay"),
])
def test_usda_texture(sand, silt, clay, expected):
    k = usda_texture(np.array([sand], float), np.array([silt], float), np.array([clay], float))[0]
    assert USDA_CLASSES[k] == expected


def test_texture_undefined_without_data():
    assert usda_texture(np.array([np.nan]), np.array([40.0]), np.array([20.0]))[0] == -1


def test_texture_uses_fractions_of_the_total():
    # SoilGrids fractions do not always add up to 100: they are rescaled
    assert USDA_CLASSES[usda_texture(np.array([36.0]), np.array([36.0]), np.array([18.0]))[0]] == "loam"


def test_saxton_rawls_loam():
    # Saxton & Rawls (2006), table 3: loam (40 % sand, 20 % clay, 2.5 % organic matter):
    # wilting point 12 %, field capacity 26 %, saturation 46 %... within a few points
    wp, fc, sat, ksat = saxton_rawls(np.array([40.0]), np.array([20.0]), np.array([2.5 / 1.724 * 10]))
    assert wp[0] == pytest.approx(0.12, abs=0.02)
    assert fc[0] == pytest.approx(0.26, abs=0.02)
    assert sat[0] == pytest.approx(0.46, abs=0.03)
    assert 5 < ksat[0] < 30                       # mm/h


def test_saxton_rawls_orders_textures():
    sand, clay, soc = np.array([88.0, 40.0, 20.0]), np.array([5.0, 20.0, 50.0]), np.full(3, 10.0)
    wp, fc, sat, ksat = saxton_rawls(sand, clay, soc)
    assert np.all(wp < fc) and np.all(fc < sat)
    assert wp[0] < wp[1] < wp[2]                   # more clay holds more water at wilting point
    assert ksat[0] > ksat[1] > ksat[2]             # and drains more slowly
