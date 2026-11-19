import numpy as np
import pandas as pd
import pytest

from geomon import weather as w
from geomon.ingest import agera5, openmeteo
from geomon.units import convert


def _hourly(start, hours, **cols):
    idx = pd.date_range(start, periods=hours, freq="h")      # UTC, naive
    return pd.DataFrame({k: (v(np.arange(hours)) if callable(v) else v) for k, v in cols.items()}, index=idx)


def test_daily_aggregation_over_local_days():
    # 2025-06-10 00:00 Paris = 2025-06-09 22:00 UTC
    f = _hourly("2025-06-09 22:00", 48, temperature_2m=lambda h: h % 24, precipitation=0.5,
                shortwave_radiation=100.0)
    d = w.hourly_to_daily(f, ["t_min", "t_max", "precip", "radiation"])
    day = d.loc["2025-06-10"]
    assert (day.t_min, day.t_max) == (0, 23)
    assert day.precip == pytest.approx(12.0)
    assert day.radiation == pytest.approx(100 * 24 * 3600 / 1e6)        # W/m² over a day -> MJ/m²


def test_daylight_saving_day_is_kept_and_short_days_dropped():
    # 2025-03-30 has 23 local hours; the series ends 10 hours into 2025-03-31
    f = _hourly("2025-03-29 23:00", 23 + 10, temperature_2m=5.0)
    d = w.hourly_to_daily(f, ["t_mean"])
    assert d.loc["2025-03-30", "t_mean"] == 5.0
    assert np.isnan(d.loc["2025-03-31", "t_mean"])                     # fewer than MIN_HOURS hours


def test_relative_humidity_at_local_hours():
    f = _hourly("2025-06-09 22:00", 24, relative_humidity_2m=lambda h: h)
    d = w.hourly_to_daily(f, ["rh_06", "rh_12"])
    assert d.loc["2025-06-10", "rh_06"] == 6 and d.loc["2025-06-10", "rh_12"] == 12


def test_daily_matrix_equals_the_daily_aggregation_of_each_point():
    """The daily stores (all points at once) hold what hourly_to_daily gives point by point."""
    rng = np.random.default_rng(1)
    times = pd.date_range("2025-10-20", "2025-11-03", freq="h")          # across the October DST change
    vars_ = ["temperature_2m", "precipitation", "shortwave_radiation", "relative_humidity_2m", "dew_point_2m",
             "wind_speed_10m"]
    data = {v: rng.normal(10, 5, (len(times), 3)) for v in vars_}
    data["precipitation"][rng.random((len(times), 3)) < 0.3] = np.nan          # gaps: some days below MIN_HOURS
    days, out = w.daily_matrix(times.values, data)
    for p in range(3):
        ref = w.hourly_to_daily(pd.DataFrame({v: data[v][:, p] for v in vars_}, index=times))
        assert list(ref.index) == list(days)
        for col in ref.columns:
            np.testing.assert_allclose(out[col][:, p], ref[col].to_numpy(), rtol=1e-12, equal_nan=True, err_msg=col)


def test_resample_sums_only_complete_periods():
    idx = pd.date_range("2025-06-02", "2025-06-18", freq="D")           # Monday 2 June -> Wednesday 18
    df = pd.DataFrame({"precip": 1.0, "t_mean": 10.0}, index=idx)
    out = w.resample(df, "week")
    assert out.loc["2025-06-02", "precip"] == 7.0
    assert np.isnan(out.loc["2025-06-16", "precip"])                    # 3 days only
    assert out.loc["2025-06-16", "t_mean"] == 10.0                      # means are kept


def test_units():
    assert convert(15.0, "degC", "K") == pytest.approx(288.15)
    assert convert(3.2, "g/kg", "percent") == pytest.approx(0.32)
    assert convert(1.0, "kg/m**2/s", "mm/day") == pytest.approx(86400.0)
    with pytest.raises(Exception):
        convert(1.0, "person", "m")


def test_agera5_units():
    assert agera5._cf_to_pint("J m-2 d-1") == "J * m**-2 * day**-1"
    assert agera5._convert(np.array([8.64e6]), "J m-2 d-1", "MJ/m**2")[0] == pytest.approx(8.64)
    assert agera5._convert(np.array([273.15]), "K", "degC")[0] == pytest.approx(0.0)
    assert agera5._convert(np.array([3.0]), "mm d-1", "mm")[0] == pytest.approx(3.0)
    assert agera5._convert(np.array([5.0]), "cm", "m")[0] == pytest.approx(0.05)


def test_openmeteo_chunks():
    import datetime as dt

    chunks = list(openmeteo._months(dt.date(2024, 11, 13), dt.date(2025, 2, 10), 1))
    assert chunks[0] == (dt.date(2024, 11, 13), dt.date(2024, 11, 30))
    assert chunks[-1] == (dt.date(2025, 2, 1), dt.date(2025, 2, 10))
    years = list(openmeteo._months(dt.date(2022, 1, 1), dt.date(2024, 6, 1), 12))
    assert years == [(dt.date(2022, 1, 1), dt.date(2022, 12, 31)), (dt.date(2023, 1, 1), dt.date(2023, 12, 31)),
                     (dt.date(2024, 1, 1), dt.date(2024, 6, 1))]


def test_openmeteo_merge_and_reuse():
    import datetime as dt

    t = [int(pd.Timestamp(f"2025-01-0{d}").timestamp()) for d in (1, 2, 3)]
    old = [{"hourly": {"time": t, "x": [1, 2, 3]}}]
    new = [{"hourly": {"time": t[1:] + [t[2] + 86400], "x": [20, 30, 40]}}]
    merged = openmeteo._merge(old, new, dt.date(2025, 1, 2))
    assert merged[0]["hourly"]["x"] == [1, 20, 30, 40]

    sig = {"points": [[1, 2], [3, 4]], "variables": ["x"], "extra": {}}
    stored = {"signature": sig, "responses": ["a", "b"]}
    assert openmeteo._reuse(stored, sig) == ["a", "b"]
    assert openmeteo._reuse(stored, sig | {"points": [[3, 4]]}) == ["b"]           # a subset of the points
    assert openmeteo._reuse(stored, sig | {"points": [[5, 6]]}) is None
    assert openmeteo._reuse(stored, sig | {"variables": ["y"]}) is None


def test_openmeteo_requests_are_batched(monkeypatch):
    import datetime as dt

    calls = []

    def fake_get(kind, params, weight):
        lats = params["latitude"].split(",")
        calls.append((len(lats), weight))
        return [{"latitude": float(la)} for la in lats]

    monkeypatch.setattr(openmeteo, "_get", fake_get)
    assert openmeteo.settings.openmeteo_batch_points == 100          # default
    points = [(0.1 * i, 45.0 + 0.001 * i) for i in range(250)]
    blocks = openmeteo._fetch("historical", points, ["temperature_2m"], {"models": "meteofrance_seamless"},
                              dt.date(2025, 1, 1), dt.date(2025, 1, 31))
    assert [n for n, _ in calls] == [100, 100, 50]
    assert [b["latitude"] for b in blocks] == [round(p[1], 4) for p in points]          # in order


def test_weather_grid_respects_the_point_budget(monkeypatch):
    from types import SimpleNamespace

    # About the extent of the Manche (a département): 1.2° x 1.3°
    ring = [[-1.95, 48.45], [-0.75, 48.45], [-0.75, 49.73], [-1.95, 49.73], [-1.95, 48.45]]
    aoi = SimpleNamespace(bbox_wgs84=(-1.95, 48.45, -0.75, 49.73), geom_wgs84={"type": "Polygon", "coordinates": [ring]})
    step, points = openmeteo.weather_grid(aoi)
    assert len(points) <= openmeteo.settings.weather_max_points
    assert step / openmeteo.settings.weather_grid_deg in (2, 4, 8, 16)
    small = SimpleNamespace(bbox_wgs84=(4.95, 45.40, 5.05, 45.46),
                            geom_wgs84={"type": "Polygon", "coordinates": [[[4.95, 45.4], [5.05, 45.4], [5.05, 45.46], [4.95, 45.46], [4.95, 45.4]]]})
    assert openmeteo.weather_grid(small)[0] == openmeteo.settings.weather_grid_deg      # a small site keeps 2.5 km


def test_openmeteo_weight():
    assert openmeteo._weight(10, 14) == 1.0
    assert openmeteo._weight(20, 28, 3) == 12.0


def test_pacer_does_not_wait_under_budget():
    import time

    p = openmeteo._Pacer(100)
    t = time.monotonic()
    for _ in range(10):
        p.wait(5)
    assert time.monotonic() - t < 0.5 and len(p.calls) == 10
