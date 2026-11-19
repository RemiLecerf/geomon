import datetime as dt
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from geomon import weather as w
from geomon.ingest import era5


def test_et0_fao56_example_19():
    # FAO-56, example 19 (hourly ET0, N'Diaye, Senegal, 16°N, 8 m above sea level)
    p = 101.2
    # 14:00-15:00: T 38 °C, RH 52 %, u2 3.3 m/s, Rn 1.749 MJ m-2 h-1, G 0.175 -> 0.63 mm/h
    ea = 0.52 * era5.saturation_vp(38.0)
    assert era5.et0_fao56_hourly(38.0, ea, 3.3, 1.749, 0.175, p) == pytest.approx(0.63, abs=0.01)
    # 02:00-03:00: T 28 °C, RH 90 %, u2 1.9 m/s, Rn -0.100, G -0.050 -> 0.00 mm/h
    ea = 0.90 * era5.saturation_vp(28.0)
    assert era5.et0_fao56_hourly(28.0, ea, 1.9, -0.100, -0.050, p) == pytest.approx(0.0, abs=0.01)


def test_et0_from_reanalysis_fields_is_plausible():
    # A sunny summer noon hour: 3.4 MJ/m² of solar, 1.3 MJ/m² of long-wave radiation
    noon = era5.et0_from_reanalysis(np.array(28.0), np.array(12.0), np.array(3.0), np.array(3.4e6),
                                    np.array(1.3e6), np.array(98000.0))
    night = era5.et0_from_reanalysis(np.array(15.0), np.array(13.0), np.array(1.0), np.array(0.0),
                                     np.array(1.2e6), np.array(98000.0))
    assert 0.5 < noon < 1.0
    assert 0.0 <= night < 0.05


def _aoi(geom_box):
    x0, y0, x1, y1 = geom_box
    ring = [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]
    return SimpleNamespace(bbox_wgs84=(x0 - 0.02, y0 - 0.02, x1 + 0.02, y1 + 0.02),
                           geom_wgs84={"type": "Polygon", "coordinates": [ring]})


def test_cells_touching_the_communes():
    pts = era5.cells(_aoi((4.96, 45.36, 5.14, 45.44)), 0.1)
    assert sorted(pts) == [(5.0, 45.4), (5.1, 45.4)]
    assert all(round(lo / 0.25, 6).is_integer() for lo, _ in era5.cells(_aoi((4.96, 45.36, 5.14, 45.44)), 0.25))


def test_request_boxes_stay_within_the_largest_area():
    pts = [(round(4.0 + 0.1 * i, 4), 45.0) for i in range(25)]          # 2.4° wide
    groups = era5.boxes(pts, 0.1, 1.0)
    assert sum(len(p) for _, p in groups) == 25
    for (n, wst, s, e), sel in groups:
        assert e - wst <= 1.0 + 1e-9 and n - s <= 1.0 + 1e-9
        assert all(wst < lo < e for lo, _ in sel)                       # centres inside, half a cell from the edge
    # One cell: a box of one cell around it, not a zero-width box
    assert era5.boxes([(5.0, 45.5)], 0.25, 2.0) == [([45.625, 4.875, 45.375, 5.125], [(5.0, 45.5)])]
    assert len(era5.boxes([(5.0, 45.0), (5.1, 45.1)], 0.1, 1.0)) == 1


def _download(times, lats, lons, **fields):
    shape = (len(times), len(lats), len(lons))
    return xr.Dataset({k: (("valid_time", "latitude", "longitude"), np.full(shape, v, "float32"))
                       for k, v in fields.items()},
                      coords={"valid_time": times, "latitude": lats, "longitude": lons, "expver": "0001"})


def test_download_to_store_variables():
    times = pd.date_range("2025-06-10", periods=48, freq="h")
    # Two files, as the CDS zips them: variables split by group
    files = [_download(times, [45.5, 45.4], [5.0, 5.1], t2m=293.15, d2m=283.15, sp=98000.0, tp=0.0005),
             _download(times, [45.5, 45.4], [5.0, 5.1], ssrd=1.8e6, strd=1.2e6, u10=3.0, v10=4.0, stl1=290.15,
                       swvl1=0.3, slhf=-1.225e6, sshf=-3.6e5, tcc=0.25)]
    raw = era5._raw_points(files, [(5.1, 45.4), (5.0, 45.5)], 0.1)
    assert dict(raw.sizes) == {"time": 48, "point": 2}
    ds = era5._dataset(raw, [(5.1, 45.4), (5.0, 45.5)])
    first = {v: float(ds[v].values[0, 0]) for v in ds.data_vars}
    assert first["temperature_2m"] == pytest.approx(20.0)
    assert first["precipitation"] == pytest.approx(0.5)                 # m -> mm
    assert first["shortwave_radiation"] == pytest.approx(500.0)         # J/m² over the hour -> W/m²
    assert first["wind_speed_10m"] == pytest.approx(5.0)
    assert first["surface_pressure"] == pytest.approx(980.0)
    assert first["soil_temperature_0_to_7cm"] == pytest.approx(17.0)
    assert first["soil_moisture_0_to_7cm"] == pytest.approx(0.3)
    assert first["evapotranspiration"] == pytest.approx(0.5)            # -1.225 MJ/m² / 2.45 MJ/kg
    assert first["sensible_heat_flux"] == pytest.approx(100.0)          # upwards positive
    assert first["cloud_cover"] == pytest.approx(25.0)
    assert 50 < first["relative_humidity_2m"] < 55                      # 10 °C dew point at 20 °C
    assert first["et0_fao_evapotranspiration"] > 0
    assert ds["lon"].values.tolist() == [5.1, 5.0]

    # The daily aggregation of the weather module reads them like the Open-Meteo products
    frame = pd.DataFrame({v: ds[v].values[:, 0] for v in ds.data_vars}, index=pd.DatetimeIndex(ds.time.values))
    daily = w.hourly_to_daily(frame, ["t_mean", "precip", "radiation", "evap", "sensible_heat", "soil_m_0_7"])
    assert daily.loc["2025-06-10", "precip"] == pytest.approx(11.0)    # 22 of its local hours (from 00 UTC)
    day = daily.loc["2025-06-11"]                                       # 22:00 UTC the day before to 22:00 UTC
    assert day.precip == pytest.approx(12.0) and day.evap == pytest.approx(12.0)
    assert day.radiation == pytest.approx(500 * 86400 / 1e6)
    assert day.sensible_heat == pytest.approx(100 * 86400 / 1e6)
    assert day.soil_m_0_7 == pytest.approx(0.3)


def test_yearly_requests():
    assert era5._years(dt.date(2022, 3, 1), dt.date(2024, 2, 10)) == [
        (dt.date(2022, 3, 1), dt.date(2022, 12, 31)), (dt.date(2023, 1, 1), dt.date(2023, 12, 31)),
        (dt.date(2024, 1, 1), dt.date(2024, 2, 10))]


def test_area_mean_of_a_variable_the_product_lacks(monkeypatch):
    """A series without the variable is reported unavailable, not an error (ERA5-Land has no evap)."""
    empty = {"available": True, "table": pd.DataFrame(), "forecast_from": None, "updated": None}
    monkeypatch.setattr(w, "base_product", lambda series: "era5land")
    monkeypatch.setattr(w, "area_weights", lambda name, geom: [(0, 0.5), (1, 0.5)])
    monkeypatch.setattr(w, "_series_point", lambda *a: empty)
    monkeypatch.setattr(w, "open_product", lambda name: xr.Dataset(coords={"lon": ("point", [5.0, 5.1]),
                                                                           "lat": ("point", [45.4, 45.4])}))
    r = w._series_area.__wrapped__("era5land", "{}", ("evap",), (1.0, None), "s")
    assert r["available"] and "evap" not in r["table"]


def test_series_follow_the_source():
    assert w.SERIES["era5land"]["history"] == "era5land"                # GEOMON_ERA5_SOURCE=cds by default
    assert w.SERIES["era5"]["history"] == "era5"
    assert w.cell_step("era5") == 0.25 and w.cell_step("era5land") == 0.1
