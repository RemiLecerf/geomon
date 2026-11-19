"""Weather products: catalogue, loading and daily aggregation (Specifications §4.2).

Every product is a Zarr store under ``data/zarr/weather/<name>.zarr`` with
dimensions ``(time, point)``. ``time`` is UTC; each point carries ``lon``,
``lat`` and the model or grid elevation. Variables keep their units in the
``units`` attribute (pint syntax, see :mod:`geomon.units`).

Hourly values follow Open-Meteo conventions: accumulations (precipitation,
ET0, snowfall) and radiation refer to the hour ending at the timestamp;
other variables are instantaneous.

Daily aggregates reproduce the AgERA5 variable set where the inputs allow it,
over the local (Europe/Paris) civil day. SIM2 is already daily: its values are
mapped onto the same names, keeping Météo-France's day definitions (some
accumulations run 06 UTC to 06 UTC; see the ``day`` attribute).
"""

from __future__ import annotations

import functools
import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

from .config import settings

LOCAL_TZ = "Europe/Paris"


@dataclass(frozen=True)
class Product:
    name: str
    label: str
    source: str
    step: str                  # "hour" or "day"
    resolution: str
    forecast: bool = False


PRODUCTS: dict[str, Product] = {
    p.name: p
    for p in (
        Product("om_mf_hist", "Météo-France AROME/ARPEGE, archived forecasts",
                "Open-Meteo Historical Forecast API, model meteofrance_seamless", "hour", "1.5–11 km"),
        Product("om_forecast", "Forecast: Météo-France AROME/ARPEGE, then ECMWF IFS",
                "Open-Meteo Forecast API, models meteofrance_seamless and ecmwf_ifs", "hour", "1.5–9 km",
                forecast=True),
        Product("om_era5land", "ERA5-Land reanalysis (ERA5 for rain, wind, radiation, ET0)",
                "Open-Meteo Historical Weather API, models era5_land then era5 (Copernicus ECMWF)", "hour",
                "0.1° (~9 km); ERA5 0.25°"),
        Product("agera5", "AgERA5 agrometeorological indicators (ERA5, daily, 0.1°)",
                "Copernicus CDS sis-agrometeorological-indicators-timeseries", "day", "0.1° (~9 km)"),
        Product("sim2", "SAFRAN-ISBA (SIM2) reanalysis",
                "Météo-France, Données changement climatique - SIM quotidienne", "day", "8 km"),
    )
}

# Display series: one history product, optionally continued by a forecast product
SERIES = {
    "mf": {"label": "Météo-France AROME/ARPEGE + forecast", "history": "om_mf_hist", "forecast": "om_forecast"},
    "era5land": {"label": "ERA5-Land / ERA5 (reanalysis)", "history": "om_era5land", "forecast": None},
    "agera5": {"label": "AgERA5 (Copernicus)", "history": "agera5", "forecast": None},
    "sim2": {"label": "SIM2 / SAFRAN (reanalysis)", "history": "sim2", "forecast": None},
}


def store_path(name: str) -> Path:
    return settings.site_zarr_dir / "weather" / f"{name}.zarr"


@lru_cache(maxsize=16)
def _open_cached(path: str, mtime: float) -> xr.Dataset:
    return xr.open_zarr(path, consolidated=True).load()


def open_product(name: str) -> xr.Dataset | None:
    path = store_path(name)
    zmeta = path / "zarr.json"
    if not zmeta.exists():
        return None
    return _open_cached(str(path), zmeta.stat().st_mtime)


def clear_cache() -> None:
    _open_cached.cache_clear()
    _series_point.cache_clear()
    _series_area.cache_clear()
    _area_weights.cache_clear()


# ---------------------------------------------------------------------------
# Points
# ---------------------------------------------------------------------------
def distance_km(lon0: float, lat0: float, lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
    kx = 111.32 * math.cos(math.radians(lat0))
    return np.hypot((np.asarray(lon) - lon0) * kx, (np.asarray(lat) - lat0) * 110.57)


def nearest_point(ds: xr.Dataset, lon: float, lat: float) -> tuple[int, float]:
    d = distance_km(lon, lat, ds["lon"].values, ds["lat"].values)
    i = int(np.nanargmin(d))
    return i, float(d[i])


# ---------------------------------------------------------------------------
# Daily aggregation
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DailyVar:
    name: str
    long_name: str
    units: str
    how: str              # aggregation over the day, or how a sum is combined over weeks/months
    agera5: str | None = None


DAILY_VARS: list[DailyVar] = [
    DailyVar("t_mean", "Air temperature 2 m, daily mean", "degC", "mean", "Temperature-Air-2m-Mean-24h"),
    DailyVar("t_min", "Air temperature 2 m, daily minimum", "degC", "mean", "Temperature-Air-2m-Min-24h"),
    DailyVar("t_max", "Air temperature 2 m, daily maximum", "degC", "mean", "Temperature-Air-2m-Max-24h"),
    DailyVar("precip", "Precipitation (rain + snow, water equivalent)", "mm", "sum", "Precipitation-Flux"),
    DailyVar("rain", "Rainfall (liquid)", "mm", "sum"),
    DailyVar("snowfall", "Snowfall (snow depth)", "cm", "sum"),
    DailyVar("snowfall_we", "Snowfall (water equivalent)", "mm", "sum"),
    DailyVar("radiation", "Global solar radiation", "MJ/m**2", "sum", "Solar-Radiation-Flux"),
    DailyVar("longwave_down", "Downward long-wave radiation", "MJ/m**2", "sum"),
    DailyVar("et0", "Reference evapotranspiration (FAO-56 Penman-Monteith)", "mm", "sum", "Reference-Evapotranspiration"),
    DailyVar("rh_mean", "Relative humidity 2 m, daily mean", "percent", "mean"),
    DailyVar("rh_06", "Relative humidity 2 m at 06 h local time", "percent", "mean", "Relative-Humidity-2m-06h"),
    DailyVar("rh_09", "Relative humidity 2 m at 09 h local time", "percent", "mean", "Relative-Humidity-2m-09h"),
    DailyVar("rh_12", "Relative humidity 2 m at 12 h local time", "percent", "mean", "Relative-Humidity-2m-12h"),
    DailyVar("rh_15", "Relative humidity 2 m at 15 h local time", "percent", "mean", "Relative-Humidity-2m-15h"),
    DailyVar("rh_18", "Relative humidity 2 m at 18 h local time", "percent", "mean", "Relative-Humidity-2m-18h"),
    DailyVar("rh_min", "Relative humidity 2 m, daily minimum", "percent", "mean", "Relative-Humidity-2m-Min"),
    DailyVar("rh_max", "Relative humidity 2 m, daily maximum", "percent", "mean", "Relative-Humidity-2m-Max"),
    DailyVar("vp_mean", "Vapour pressure, daily mean", "hPa", "mean", "Vapour-Pressure-Mean"),
    DailyVar("vpd_mean", "Vapour pressure deficit, daily mean", "kPa", "mean"),
    DailyVar("vpd_tmax", "Vapour pressure deficit at the daily maximum temperature", "kPa", "mean",
             "Vapour-Pressure-Deficit-at-Maximum-Temperature"),
    DailyVar("q_mean", "Specific humidity, daily mean", "g/kg", "mean"),
    DailyVar("wind_mean", "Wind speed 10 m, daily mean", "m/s", "mean", "Wind-Speed-10m-Mean"),
    DailyVar("cloud_cover", "Cloud cover, daily mean", "percent", "mean", "Cloud-Cover-Mean"),
    DailyVar("snow_depth", "Snow depth, daily mean", "m", "mean", "Snow-Thickness-Mean"),
    DailyVar("swe", "Snow water equivalent, daily mean", "mm", "mean"),
    DailyVar("soil_t_0_7", "Soil temperature 0–7 cm", "degC", "mean"),
    DailyVar("soil_t_7_28", "Soil temperature 7–28 cm", "degC", "mean"),
    DailyVar("soil_t_28_100", "Soil temperature 28–100 cm", "degC", "mean"),
    DailyVar("soil_t_100_255", "Soil temperature 100–255 cm", "degC", "mean"),
    DailyVar("soil_m_0_7", "Soil moisture 0–7 cm", "m**3/m**3", "mean"),
    DailyVar("soil_m_7_28", "Soil moisture 7–28 cm", "m**3/m**3", "mean"),
    DailyVar("soil_m_28_100", "Soil moisture 28–100 cm", "m**3/m**3", "mean"),
    DailyVar("soil_m_100_255", "Soil moisture 100–255 cm", "m**3/m**3", "mean"),
    DailyVar("swi", "Soil wetness index (SIM2)", "dimensionless", "mean"),
    DailyVar("wg_root", "Liquid water content of the root zone at 06 UTC (SIM2)", "m**3/m**3", "mean"),
    DailyVar("evap", "Actual evapotranspiration (SIM2)", "mm", "sum"),
    DailyVar("drainage", "Drainage (SIM2)", "mm", "sum"),
    DailyVar("runoff", "Runoff (SIM2)", "mm", "sum"),
    DailyVar("effective_rain", "Effective rainfall (SIM2)", "mm", "sum"),
]
DAILY = {v.name: v for v in DAILY_VARS}

# Hourly source variable -> daily variables, with the reduction used
_HOURLY_RULES: list[tuple[str, str, str]] = [
    ("temperature_2m", "t_mean", "mean"),
    ("temperature_2m", "t_min", "min"),
    ("temperature_2m", "t_max", "max"),
    ("precipitation", "precip", "sum"),
    ("rain", "rain", "sum"),
    ("snowfall", "snowfall", "sum"),
    ("shortwave_radiation", "radiation", "energy"),
    ("et0_fao_evapotranspiration", "et0", "sum"),
    ("relative_humidity_2m", "rh_mean", "mean"),
    ("relative_humidity_2m", "rh_06", "h06"),
    ("relative_humidity_2m", "rh_09", "h09"),
    ("relative_humidity_2m", "rh_12", "h12"),
    ("relative_humidity_2m", "rh_15", "h15"),
    ("relative_humidity_2m", "rh_18", "h18"),
    ("dew_point_2m", "vp_mean", "vapour_pressure"),
    ("vapour_pressure_deficit", "vpd_mean", "mean"),
    ("wind_speed_10m", "wind_mean", "mean"),
    ("cloud_cover", "cloud_cover", "mean"),
    ("snow_depth", "snow_depth", "mean"),
    ("soil_temperature_0_to_7cm", "soil_t_0_7", "mean"),
    ("soil_temperature_7_to_28cm", "soil_t_7_28", "mean"),
    ("soil_temperature_28_to_100cm", "soil_t_28_100", "mean"),
    ("soil_temperature_100_to_255cm", "soil_t_100_255", "mean"),
    ("soil_moisture_0_to_7cm", "soil_m_0_7", "mean"),
    ("soil_moisture_7_to_28cm", "soil_m_7_28", "mean"),
    ("soil_moisture_28_to_100cm", "soil_m_28_100", "mean"),
    ("soil_moisture_100_to_255cm", "soil_m_100_255", "mean"),
]

# A day is kept when at least this many hourly values are present (DST days have 23 or 25 hours)
MIN_HOURS = 20


def daily_variables(name: str, ds: xr.Dataset | None = None) -> list[str]:
    """Daily variables a product can provide."""
    ds = ds if ds is not None else open_product(name)
    if ds is None:
        return []
    if PRODUCTS[name].step == "day":
        return [v.name for v in DAILY_VARS if v.name in ds.data_vars]
    present = {dst for src, dst, _ in _HOURLY_RULES if src in ds.data_vars}
    return [v.name for v in DAILY_VARS if v.name in present]


def _local_index(times: np.ndarray) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(times).tz_localize("UTC").tz_convert(LOCAL_TZ)


def hourly_to_daily(frame: pd.DataFrame, variables: list[str] | None = None) -> pd.DataFrame:
    """Aggregate an hourly table (UTC index, Open-Meteo variable names) to local days."""
    if frame.empty:
        return pd.DataFrame()
    local = _local_index(frame.index.values)
    day = pd.Index(local.date, name="date")
    hour = local.hour
    out: dict[str, pd.Series] = {}
    for src, dst, how in _HOURLY_RULES:
        if src not in frame or (variables is not None and dst not in variables):
            continue
        s = pd.Series(frame[src].values, index=day)
        g = s.groupby(level=0)
        n = g.count()
        if how in ("mean", "min", "max"):
            v = getattr(g, how)()
        elif how == "sum":
            v = g.sum(min_count=1)
        elif how == "energy":          # W/m² hourly means -> MJ/m² per day
            v = g.sum(min_count=1) * 3600 / 1e6
        elif how == "vapour_pressure":  # dew point (°C) -> actual vapour pressure (hPa), Magnus formula
            e = 6.112 * np.exp(17.67 * s / (s + 243.5))
            v = e.groupby(level=0).mean()
        elif how.startswith("h"):
            sel = pd.Series(np.where(hour == int(how[1:]), s.values, np.nan), index=day)
            out[dst] = sel.groupby(level=0).mean()
            continue
        else:
            raise ValueError(how)
        out[dst] = v.where(n >= MIN_HOURS)
    df = pd.DataFrame(out)
    df.index = pd.to_datetime(df.index)
    return df


def point_frame(name: str, point: int) -> pd.DataFrame:
    """All variables of one point of a product as a table indexed by time (UTC or date)."""
    ds = open_product(name)
    sub = ds.isel(point=point)
    cols = {v: sub[v].values for v in sub.data_vars if sub[v].dims == ("time",)}
    return pd.DataFrame(cols, index=pd.DatetimeIndex(sub["time"].values))


def series_daily(series: str, lon: float, lat: float, variables: list[str]) -> dict:
    """Daily values of a display series at the point nearest to (lon, lat).

    For a series with a forecast, the archived history is continued by the
    latest forecast run. Returns the table and metadata (point, forecast start).
    """
    name = base_product(series)
    if name is None:
        return {"available": False}
    i, dist = nearest_point(open_product(name), lon, lat)
    return _point_series(series, name, i, dist, list(variables))


def _point_series(series: str, name: str, i: int, dist: float, variables: list[str]) -> dict:
    """Daily values of a series at point ``i`` of its base product ``name``."""
    spec = SERIES[series]
    base = open_product(name)
    info = {
        "available": True,
        "point": {"lon": float(base["lon"][i]), "lat": float(base["lat"][i]),
                  "elevation": float(base["elevation"][i]) if "elevation" in base else None,
                  "distance_km": dist},
        "forecast_from": None,
        "updated": base.attrs.get("updated"),
    }
    if PRODUCTS[name].step == "day":
        frame = point_frame(name, i)
        daily = frame[[v for v in variables if v in frame]]
    else:
        # Archive (when downloaded), continued by the latest forecast
        hist = open_product(spec["history"])
        if hist is None:
            frame = pd.DataFrame()
        else:
            h = i if name == spec["history"] else nearest_point(hist, info["point"]["lon"], info["point"]["lat"])[0]
            frame = point_frame(spec["history"], h)
        fc_name = spec["forecast"]
        fc = open_product(fc_name) if fc_name else None
        if fc is not None:
            j, _ = nearest_point(fc, info["point"]["lon"], info["point"]["lat"])
            ff = point_frame(fc_name, j)
            start = ff.index.min()
            frame = pd.concat([frame[frame.index < start], ff]) if len(frame) else ff
            info["forecast_from"] = str(_local_index(np.array([start.to_datetime64()]))[0].date())
            info["forecast_run"] = fc.attrs.get("run")
        daily = hourly_to_daily(frame, variables)
    info["table"] = daily.dropna(how="all")
    return info


# ---------------------------------------------------------------------------
# Grid cells and area means
# ---------------------------------------------------------------------------
_TRANSFORMERS = None


def _transformers():
    # Built together and published at once: requests run in parallel threads
    global _TRANSFORMERS
    if _TRANSFORMERS is None:
        from pyproj import Transformer

        _TRANSFORMERS = (Transformer.from_crs("EPSG:27572", "EPSG:4326", always_xy=True).transform,
                         Transformer.from_crs("EPSG:4326", "EPSG:2154", always_xy=True).transform)
    return _TRANSFORMERS


def cell_step(name: str) -> float | None:
    """Cell size in degrees of the lat/lon products (None for SIM2, on its own grid)."""
    return {"om_era5land": 0.1, "agera5": 0.1, "sim2": None}.get(name, settings.weather_grid_deg)


def cell_polygons(name: str) -> list:
    """The grid cell of each point of a product, as shapely polygons in WGS84."""
    from shapely.geometry import Polygon

    ds = open_product(name)
    if ds is None:
        return []
    l2e_to_wgs, _ = _transformers()
    out = []
    for i in range(ds.sizes["point"]):
        if name == "sim2":
            x, y, h = float(ds["x_l2e"][i]), float(ds["y_l2e"][i]), 4000.0
            ring = [l2e_to_wgs(a, b) for a, b in ((x - h, y - h), (x + h, y - h), (x + h, y + h), (x - h, y + h))]
        else:
            # Open-Meteo cells are centred on the requested grid point; AgERA5 on its own centres
            cx = "req_lon" if "req_lon" in ds.coords else "lon"
            cy = "req_lat" if "req_lat" in ds.coords else "lat"
            lon, lat, h = float(ds[cx][i]), float(ds[cy][i]), cell_step(name) / 2
            ring = [(lon - h, lat - h), (lon + h, lat - h), (lon + h, lat + h), (lon - h, lat + h)]
        out.append(Polygon(ring))
    return out


def area_weights(name: str, geom_wgs84: dict) -> list[tuple[int, float]]:
    """Points of a product covering an area, weighted by the area of their cell inside it.

    Areas are computed in Lambert-93. When no cell intersects the area (outside
    the downloaded grid), the point nearest to the area is used alone. Cached until
    the product changes.
    """
    return _area_weights(name, json.dumps(geom_wgs84, sort_keys=True), _stamp(name), settings.site_code)


@lru_cache(maxsize=128)
def _area_weights(name: str, geom_json: str, stamp: tuple, site: str) -> list[tuple[int, float]]:
    from shapely.geometry import shape
    from shapely.ops import transform

    geom_wgs84 = json.loads(geom_json)

    _, wgs_to_l93 = _transformers()
    area = transform(wgs_to_l93, shape(geom_wgs84))
    weights = []
    for i, cell in enumerate(cell_polygons(name)):
        inter = transform(wgs_to_l93, cell).intersection(area).area
        if inter > 0:
            weights.append((i, inter))
    if not weights:
        ds = open_product(name)
        c = shape(geom_wgs84).representative_point()
        weights = [(nearest_point(ds, c.x, c.y)[0], 1.0)]
    # Drop slivers (cells touching the area edge by less than 0.2 % of it)
    total = sum(w for _, w in weights)
    weights = [(i, w) for i, w in weights if w / total >= 0.002] or weights
    total = sum(w for _, w in weights)
    return [(i, w / total) for i, w in weights]


def _stamp(*names: str | None) -> tuple:
    out = []
    for n in names:
        z = store_path(n) / "zarr.json" if n else None
        out.append(z.stat().st_mtime if z is not None and z.exists() else None)
    return tuple(out)


def base_product(series: str) -> str | None:
    """The product a series' points come from: its history, or its forecast while the
    history has not been downloaded yet."""
    spec = SERIES[series]
    if open_product(spec["history"]) is not None:
        return spec["history"]
    if spec["forecast"] and open_product(spec["forecast"]) is not None:
        return spec["forecast"]
    return None


@lru_cache(maxsize=512)
def _series_point(series: str, name: str, i: int, variables: tuple[str, ...], stamp: tuple, site: str = "") -> dict:
    return _point_series(series, name, i, 0.0, list(variables))


def series_daily_area(series: str, geom_wgs84: dict, variables: list[str]) -> dict:
    """Daily values of a display series averaged over an area (weighted by cell overlap).

    A day is kept when cells holding at least half of the weight have a value;
    the mean is taken over the cells that have one. Cached until the products change
    (the result is shared: do not modify it).
    """
    spec = SERIES[series]
    stamp = _stamp(spec["history"], spec["forecast"])
    return _series_area(series, json.dumps(geom_wgs84, sort_keys=True), tuple(variables), stamp, settings.site_code)


@lru_cache(maxsize=64)
def _series_area(series: str, geom_json: str, variables: tuple[str, ...], stamp: tuple, site: str) -> dict:
    name = base_product(series)
    if name is None:
        return {"available": False}
    weights = area_weights(name, json.loads(geom_json))
    parts, info = [], None
    for i, w in weights:
        r = _series_point(series, name, i, variables, stamp, site)
        if r.get("available"):
            info = info or r
            parts.append((r["table"], w))
    if not parts:
        return {"available": False}
    index = functools.reduce(pd.Index.union, [t.index for t, _ in parts])
    w = np.array([wt for _, wt in parts])[:, None]
    out = {}
    for v in variables:
        vals = np.vstack([t[v].reindex(index).to_numpy(dtype="float64") if v in t else np.full(len(index), np.nan)
                          for t, _ in parts])
        ok = np.isfinite(vals)
        den = (ok * w).sum(axis=0)
        if den.max() > 0:
            with np.errstate(invalid="ignore", divide="ignore"):
                out[v] = np.where(den >= 0.5, np.nansum(vals * w, axis=0) / den, np.nan)
    table = pd.DataFrame(out, index=pd.DatetimeIndex(index)).dropna(how="all")
    base = open_product(name)
    cells = [{"lon": float(base["lon"][i]), "lat": float(base["lat"][i]), "weight": round(wt, 4)}
             for i, wt in weights]
    return {"available": True, "point": None, "cells": cells, "table": table,
            "forecast_from": info.get("forecast_from"), "forecast_run": info.get("forecast_run"),
            "updated": info.get("updated")}


def resample(df: pd.DataFrame, step: str) -> pd.DataFrame:
    """Daily table -> weekly or monthly (sums for accumulations, means otherwise)."""
    if step == "day" or df.empty:
        return df
    rule = {"week": "W-MON", "month": "MS"}[step]
    agg = {c: ("sum" if DAILY[c].how == "sum" else "mean") for c in df.columns}
    counts = df.resample(rule, label="left", closed="left").count()
    out = df.resample(rule, label="left", closed="left").agg(agg)
    # Sums over incomplete periods would be misleading
    full = {"week": 7, "month": 28}[step]
    for c, how in agg.items():
        if how == "sum":
            out[c] = out[c].where(counts[c] >= full)
    return out
