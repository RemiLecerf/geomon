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
import logging
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

from .config import settings

log = logging.getLogger(__name__)

LOCAL_TZ = "Europe/Paris"


@dataclass(frozen=True)
class Product:
    name: str
    label: str
    source: str
    step: str                  # "hour" or "day"
    resolution: str
    forecast: bool = False
    kind: str = "grid"         # "grid" (cells weighted over an area) or "stations" (measured points)


PRODUCTS: dict[str, Product] = {
    p.name: p
    for p in (
        Product("om_mf_hist", "Météo-France AROME/ARPEGE, archived forecasts",
                "Open-Meteo Historical Forecast API, model meteofrance_seamless", "hour", "1.5–11 km"),
        Product("om_forecast", "Forecast: Météo-France AROME/ARPEGE, then ECMWF IFS",
                "Open-Meteo Forecast API, models meteofrance_seamless and ecmwf_ifs", "hour", "1.5–9 km",
                forecast=True),
        Product("era5land", "ERA5-Land reanalysis (hourly, 0.1°)",
                "Copernicus CDS reanalysis-era5-land-timeseries (ECMWF)", "hour", "0.1° (~9 km)"),
        Product("era5", "ERA5 reanalysis (hourly, 0.25°)",
                "Copernicus CDS reanalysis-era5-single-levels-timeseries (ECMWF)", "hour", "0.25° (~28 km)"),
        # Former source of ERA5-Land, kept as an option (GEOMON_ERA5_SOURCE=openmeteo)
        Product("om_era5land", "ERA5-Land via Open-Meteo (ERA5 for rain, wind, radiation, ET0)",
                "Open-Meteo Historical Weather API, models era5_land then era5 (Copernicus ECMWF)", "hour",
                "0.1° (~9 km); ERA5 0.25°"),
        Product("agera5", "AgERA5 agrometeorological indicators (ERA5, daily, 0.1°)",
                "Copernicus CDS sis-agrometeorological-indicators-timeseries", "day", "0.1° (~9 km)"),
        Product("sim2", "SAFRAN-ISBA (SIM2) reanalysis",
                "Météo-France, Données changement climatique - SIM quotidienne", "day", "8 km"),
        # Measured: the stations nearest to the site, each variable from the nearest station measuring it
        Product("mf_stations", "Météo-France stations (measured)",
                "Météo-France, Données climatologiques de base - quotidiennes", "day", "stations", kind="stations"),
        Product("comephore", "COMEPHORE rainfall reanalysis (radar and rain gauges)",
                "Météo-France, Réanalyses Coméphore (hourly, summed over UTC days)", "day", "1 km"),
        Product("mf_etp", "Météo-France Penman-Monteith ETP (grid)",
                "Météo-France, Données ETP spatialisées quotidiennes", "day", "0.125° (~12 km)"),
    )
}

# Display series: one history product, optionally continued by a forecast product
_OPENMETEO_ERA5 = settings.era5_source == "openmeteo"
SERIES = {
    "mf": {"label": "Météo-France AROME/ARPEGE + forecast", "history": "om_mf_hist", "forecast": "om_forecast"},
    "era5land": {"label": "ERA5-Land / ERA5 via Open-Meteo (reanalysis)" if _OPENMETEO_ERA5
                 else "ERA5-Land (reanalysis, 0.1°)",
                 "history": "om_era5land" if _OPENMETEO_ERA5 else "era5land", "forecast": None},
    "era5": {"label": "ERA5 (reanalysis, 0.25°)", "history": "era5", "forecast": None},
    "agera5": {"label": "AgERA5 (Copernicus)", "history": "agera5", "forecast": None},
    "sim2": {"label": "SIM2 / SAFRAN (reanalysis)", "history": "sim2", "forecast": None},
    "stations": {"label": "Météo-France stations (measured)", "history": "mf_stations", "forecast": None},
    "comephore": {"label": "COMEPHORE (radar + rain gauges, 1 km)", "history": "comephore", "forecast": None},
    "mf_etp": {"label": "Météo-France ETP (grid, 12 km)", "history": "mf_etp", "forecast": None},
}
STATIONS_MAX_KM = 40.0          # stations farther than this from the area are not used


def store_path(name: str) -> Path:
    return settings.site_zarr_dir / "weather" / f"{name}.zarr"


def _hourly_history(name: str) -> bool:
    """An hourly archive (not a forecast): read lazily, with a daily store beside it."""
    p = PRODUCTS.get(name)
    return p is not None and p.step == "hour" and not p.forecast


def daily_name(name: str) -> str:
    """The daily store of an hourly archive (local days, every daily variable, every point)."""
    return f"{name}_daily"


def _mtime(name: str) -> float | None:
    z = store_path(name) / "zarr.json"
    return z.stat().st_mtime if z.exists() else None


def open_product(name: str) -> xr.Dataset | None:
    """A product of the current site. Hourly archives are opened lazily (a département's hourly
    store does not fit in memory: the daily values come from :func:`open_daily`); daily products
    and forecasts are loaded."""
    from .zarrio import open_cached

    return open_cached(store_path(name), load=not _hourly_history(name))


def open_daily(name: str) -> xr.Dataset | None:
    """Daily values of a product, loaded: the product itself when daily; for an hourly archive its
    daily store, or the daily values computed from the hourly store when that is missing or older
    (an archive written before daily stores existed)."""
    if not _hourly_history(name):
        return open_product(name)
    hourly, daily = _mtime(name), _mtime(daily_name(name))
    if hourly is None:
        return None
    if daily is not None and daily >= hourly:
        from .zarrio import open_cached

        return open_cached(store_path(daily_name(name)))
    return _daily_fallback(str(store_path(name)), hourly)


@lru_cache(maxsize=4)
def _daily_fallback(path: str, mtime: float) -> xr.Dataset:
    log.warning("No daily store for %s: daily values computed from the hourly store", path)
    with xr.open_zarr(path, consolidated=True) as ds:
        return daily_dataset(ds.load())


def clear_cache() -> None:
    for f in (_daily_fallback, _series_point, _series_area, _area_weights):
        f.cache_clear()


def write_product(ds: xr.Dataset, name: str) -> None:
    """Replaces the store of a product of the current site (atomically: the web API may be reading
    it); for an hourly archive, its daily store too."""
    from .zarrio import write_atomic

    write_atomic(ds, store_path(name))
    log.info("Wrote %s: %d points, %s to %s, %d variables", name, ds.sizes["point"],
             str(ds.time.values[0])[:16], str(ds.time.values[-1])[:16], len(ds.data_vars))
    if _hourly_history(name):
        daily = daily_dataset(ds)
        write_atomic(daily, store_path(daily_name(name)))
        log.info("Wrote %s: %d days, %d variables", daily_name(name), daily.sizes["time"], len(daily.data_vars))
    clear_cache()


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
    DailyVar("sensible_heat", "Sensible heat flux from the surface (ERA5)", "MJ/m**2", "sum"),
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
    DailyVar("wind_mean_100m", "Wind speed 100 m, daily mean (ERA5)", "m/s", "mean"),
    DailyVar("gust_max", "Wind gust 10 m, daily maximum (ERA5)", "m/s", "mean"),
    DailyVar("cloud_cover", "Cloud cover, daily mean", "percent", "mean", "Cloud-Cover-Mean"),
    DailyVar("blh_max", "Boundary-layer height, daily maximum (ERA5)", "m", "mean"),
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
    DailyVar("evap", "Actual evapotranspiration (SIM2; ERA5 from the latent heat flux)", "mm", "sum"),
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
    ("snowfall_water_equivalent", "snowfall_we", "sum"),
    ("shortwave_radiation", "radiation", "energy"),
    ("longwave_radiation", "longwave_down", "energy"),
    ("sensible_heat_flux", "sensible_heat", "energy"),
    ("evapotranspiration", "evap", "sum"),
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
    ("wind_speed_100m", "wind_mean_100m", "mean"),
    ("wind_gusts_10m", "gust_max", "max"),
    ("cloud_cover", "cloud_cover", "mean"),
    ("boundary_layer_height", "blh_max", "max"),
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


def daily_matrix(times: np.ndarray, data: dict[str, np.ndarray]) -> tuple[pd.DatetimeIndex, dict[str, np.ndarray]]:
    """:func:`hourly_to_daily` for every point at once: ``data`` holds (time, point) arrays of the
    hourly variables (UTC times); returns the local days and the (day, point) daily arrays."""
    local = _local_index(times)
    codes, days = pd.factorize(local.tz_localize(None).normalize(), sort=True)
    hour = np.asarray(local.hour)
    out: dict[str, np.ndarray] = {}
    for src, dst, how in _HOURLY_RULES:
        if src not in data:
            continue
        a = np.asarray(data[src], dtype="float64")
        g = pd.DataFrame(a).groupby(codes)
        n = g.count()
        if how in ("mean", "min", "max"):
            v = getattr(g, how)()
        elif how == "sum":
            v = g.sum(min_count=1)
        elif how == "energy":
            v = g.sum(min_count=1) * 3600 / 1e6
        elif how == "vapour_pressure":
            v = pd.DataFrame(6.112 * np.exp(17.67 * a / (a + 243.5))).groupby(codes).mean()
        elif how.startswith("h"):
            sel = np.where((hour == int(how[1:]))[:, None], a, np.nan)
            out[dst] = pd.DataFrame(sel).groupby(codes).mean().to_numpy()
            continue
        else:
            raise ValueError(how)
        out[dst] = v.where(n >= MIN_HOURS).to_numpy()
    return pd.DatetimeIndex(days), out


def daily_dataset(ds: xr.Dataset) -> xr.Dataset:
    """The daily store of an hourly product: every daily variable it provides, at every point."""
    data = {v: ds[v].values for v in ds.data_vars if ds[v].dims == ("time", "point")}
    days, out = daily_matrix(ds["time"].values, data)
    coords = {k: c for k, c in ds.coords.items() if k != "time"}
    return xr.Dataset({k: (("time", "point"), a, {"units": DAILY[k].units}) for k, a in out.items()},
                      coords={"time": days.values, **coords},
                      attrs=dict(ds.attrs) | {"daily": f"local days ({LOCAL_TZ}) of the hourly product"})


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
    if PRODUCTS[name].kind == "stations":
        return _stations_series(series, name, lon, lat, list(variables))
    i, dist = nearest_point(open_product(name), lon, lat)
    return _point_series(series, name, i, dist, list(variables))


def _stations_series(series: str, name: str, lon: float, lat: float, variables: list[str]) -> dict:
    """Measured daily values near (lon, lat): each variable, each day, from the nearest station that
    measured it (within STATIONS_MAX_KM): the nearest station often lacks humidity or radiation."""
    ds = open_product(name)
    d = distance_km(lon, lat, ds["lon"].values, ds["lat"].values)
    order = [int(i) for i in np.argsort(d) if d[i] <= STATIONS_MAX_KM]
    if not order:
        return {"available": False}
    table, used = None, {}
    for i in order:
        t = _point_series(series, name, i, float(d[i]), variables)["table"]
        gained = t.notna().sum() if table is None else (t.notna() & table.reindex_like(t).isna()).sum()
        table = t if table is None else table.combine_first(t)
        if gained.sum() > 0:
            used[str(ds["station"].values[i])] = {"name": str(ds["station_name"].values[i]), "distance_km": round(float(d[i]), 1),
                                                    "variables": [v for v in table.columns if gained.get(v, 0) > 0]}
    first = order[0]
    return {"available": True, "table": table.reindex(columns=[v for v in variables if v in table.columns]),
            "point": {"lon": float(ds["lon"][first]), "lat": float(ds["lat"][first]),
                      "elevation": float(ds["elevation"][first]) if "elevation" in ds else None,
                      "distance_km": round(float(d[first]), 1)},
            "stations": used, "forecast_from": None, "updated": ds.attrs.get("updated")}


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
        # Archive (its daily store, when downloaded), continued by the latest forecast
        hist_name = spec["history"]
        hist = open_product(hist_name)
        daily, h = pd.DataFrame(index=pd.DatetimeIndex([])), None
        if hist is not None:
            h = i if name == hist_name else nearest_point(hist, info["point"]["lon"], info["point"]["lat"])[0]
            dd = open_daily(hist_name).isel(point=h)
            daily = pd.DataFrame({v: dd[v].values for v in variables if v in dd.data_vars},
                                 index=pd.DatetimeIndex(dd["time"].values))
        fc_name = spec["forecast"]
        fc = open_product(fc_name) if fc_name else None
        if fc is not None:
            j, _ = nearest_point(fc, info["point"]["lon"], info["point"]["lat"])
            ff = point_frame(fc_name, j)
            start = ff.index.min()
            first_day = _local_index(np.array([start.to_datetime64()]))[0].date()
            # From the forecast's first local day: that day's archive hours before the forecast, then the forecast
            hours = ff
            if hist is not None:
                day0 = pd.Timestamp(first_day).tz_localize(LOCAL_TZ).tz_convert("UTC").tz_localize(None)
                seg = hist.isel(point=h).sel(time=slice(day0, start - pd.Timedelta(1, "ns"))).load()
                if seg.sizes["time"]:
                    before = pd.DataFrame({v: seg[v].values for v in seg.data_vars if seg[v].dims == ("time",)},
                                          index=pd.DatetimeIndex(seg["time"].values))
                    hours = pd.concat([before, ff])
            daily = pd.concat([daily[daily.index < pd.Timestamp(first_day)], hourly_to_daily(hours, variables)])
            info["forecast_from"] = str(first_day)
            info["forecast_run"] = fc.attrs.get("run")
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
    fixed = {"om_era5land": 0.1, "era5land": 0.1, "era5": 0.25, "agera5": 0.1, "sim2": None, "mf_etp": 0.125,
             "comephore": None, "mf_stations": None}
    if name in fixed:
        return fixed[name]
    # Météo-France products: the step the site's points were requested on (coarser for a large site)
    ds = open_product(name)
    step = ds.attrs.get("grid_step") if ds is not None else None
    return float(step) if step else settings.weather_grid_deg


def cell_polygons(name: str) -> list:
    """The grid cell of each point of a product, as shapely polygons in WGS84."""
    from shapely.geometry import Polygon

    ds = open_product(name)
    if ds is None:
        return []
    l2e_to_wgs, _ = _transformers()

    def square(x, y, h, to=None):
        ring = ((x - h, y - h), (x + h, y - h), (x + h, y + h), (x - h, y + h))
        return Polygon([to(a, b) for a, b in ring] if to else ring)

    if "x_proj" in ds.coords:              # a projected grid (COMEPHORE): square cells in its projection
        from pyproj import Transformer

        to_wgs = Transformer.from_crs(ds.attrs["crs"], "EPSG:4326", always_xy=True).transform
        h = float(ds.attrs.get("cell_m", 1000.0)) / 2
        return [square(x, y, h, to_wgs) for x, y in zip(ds["x_proj"].values, ds["y_proj"].values)]
    if PRODUCTS[name].kind == "stations":  # a station: a small square around it (map display only)
        return [square(x, y, 0.005) for x, y in zip(ds["lon"].values, ds["lat"].values)]
    if name == "sim2":
        return [square(x, y, 4000.0, l2e_to_wgs) for x, y in zip(ds["x_l2e"].values, ds["y_l2e"].values)]
    # Open-Meteo cells are centred on the requested grid point; AgERA5 on its own centres
    cx = "req_lon" if "req_lon" in ds.coords else "lon"
    cy = "req_lat" if "req_lat" in ds.coords else "lat"
    h = cell_step(name) / 2
    return [square(x, y, h) for x, y in zip(ds[cx].values, ds[cy].values)]


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
    stamp = _stamp(spec["history"], spec["forecast"], daily_name(spec["history"]))
    return _series_area(series, json.dumps(geom_wgs84, sort_keys=True), tuple(variables), stamp, settings.site_code)


@lru_cache(maxsize=64)
def _series_area(series: str, geom_json: str, variables: tuple[str, ...], stamp: tuple, site: str) -> dict:
    name = base_product(series)
    if name is None:
        return {"available": False}
    if PRODUCTS[name].kind == "stations":   # measured: the nearest stations to the area, not a grid mean
        from shapely.geometry import shape

        c = shape(json.loads(geom_json)).representative_point()
        r = _stations_series(series, name, c.x, c.y, list(variables))
        return r | {"cells": []} if r.get("available") else r
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
        if den.size and den.max() > 0:              # empty: the product does not provide the variable
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
