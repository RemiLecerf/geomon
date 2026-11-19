import datetime as dt

import numpy as np
import pytest

from geomon import parcels as pc


def test_seasons_overlap_in_autumn():
    assert pc.seasons_of(dt.date(2024, 10, 15)) == [2024, 2025]
    assert pc.seasons_of(dt.date(2025, 3, 1)) == [2025]
    assert pc.day_of_season(dt.date(2025, 1, 1), 2025) == pc.JAN1
    assert (pc.season_end(2024) - pc.season_start(2024)).days == pc.SEASON_DAYS - 1   # leap year


def test_geometry_year():
    years = [2015, 2016, 2024, 2025]
    assert pc.geometry_year(2024, years) == 2024
    assert pc.geometry_year(2026, years) == 2025          # RPG not published yet: the latest outlines
    assert pc.geometry_year(2020, years) is None          # a missing past year is not replaced
    assert pc.geometry_year(2025, []) is None


def test_interpolation_leaves_long_gaps_empty():
    t = np.array([10, 20, 30, 40, 200, 210])
    y = np.array([0.2, 0.4, 0.6, 0.8, 0.5, 0.5])
    z = pc.interpolate_season(t, y)
    assert z[15] == pytest.approx(0.3)
    assert np.isnan(z[5]) and np.isnan(z[100]) and np.isnan(z[300])
    assert pc.interpolate_season(t[:3], y[:3]) is None            # fewer than MIN_OBS observations


def test_whittaker_keeps_straight_lines():
    t = np.arange(0, 400, 10)
    y = 0.1 + 0.001 * t
    z = pc.whittaker(t, y, np.ones(len(t)))
    assert np.allclose(z[t], y, atol=1e-6)


def test_smoothing_follows_the_upper_envelope():
    t = np.arange(0, 300, 8)
    y = np.full(len(t), 0.7)
    k = len(t) // 2
    y[k] = 0.2                                     # a cloudy date, far below the others
    z = pc.smooth_season(t, y)
    assert z[t[k]] > 0.6                           # the low value is down-weighted
    assert np.isnan(z[350])                        # after the last observation


def test_references_and_comparison():
    n = pc.SEASON_DAYS
    prior = {s: np.full(n, v) for s, v in ((2020, 0.4), (2021, 0.5), (2022, 0.6), (2023, 0.5), (2024, 0.5))}
    refs = pc.references(prior, 2025)
    assert refs["3y"]["seasons"] == [2022, 2023, 2024]
    assert refs["3y"]["mean"][200] == pytest.approx(0.5333, abs=1e-3)
    assert refs["5y"]["mean"][200] == pytest.approx(0.5)
    current = np.full(n, np.nan)
    current[: pc.JAN1 + 60] = 0.55
    cmp = pc.compare(current, refs, prior, 2025)
    assert cmp["day"] == pc.JAN1 + 59
    assert cmp["refs"]["5y"]["difference"] == pytest.approx(0.05)
    assert cmp["rank"] == {"below": 4, "seasons": 5}


def test_group_curve_weights_and_minimum_count():
    mat = np.full((2, pc.SEASON_DAYS), np.nan)
    mat[:, 0] = [1.0, 3.0]
    mat[0, 1] = 1.0
    weighted, plain, q25, q75, cnt = pc._group_curve(mat, np.array([3.0, 1.0]))
    assert weighted[0] == pytest.approx(1.5)        # (3 x 1 + 1 x 3) / 4
    assert plain[0] == pytest.approx(2.0)
    assert np.isnan(weighted[1]) and np.isnan(plain[1])   # one parcel only
    assert cnt[0] == 2


def test_crop_status():
    assert pc.crop_status(2024, 2024, "1") == "declared"
    assert pc.crop_status(2026, 2025, "18") == "assumed"       # permanent grassland, RPG 2026 not out yet
    assert pc.crop_status(2026, 2025, "1") == "unknown"
