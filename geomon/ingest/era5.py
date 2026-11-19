"""ERA5-Land and ERA5 hourly reanalyses from the Copernicus CDS (Specifications §4.2).

Two products, stored as Zarr ``(time, point)`` beside the other weather products:

* ``era5land``: ERA5-Land, 0.1° (~9 km), land surface re-run of ECMWF's land model forced by
  ERA5: temperature, humidity, pressure, rain, snowfall, radiation, wind, and what ERA5-Land
  is for, the snow and the four soil layers (temperature and moisture, down to 2.89 m).
  Dataset ``reanalysis-era5-land-timeseries``.
* ``era5``: ERA5, 0.25° (~28 km), the atmospheric reanalysis (1940 onwards): the same core
  variables plus cloud cover, boundary-layer height, gusts, 100 m wind and the surface heat
  fluxes, from which the actual evapotranspiration is derived. Dataset
  ``reanalysis-era5-single-levels-timeseries`` (the time-series edition of
  ``reanalysis-era5-single-levels``: one request per period for an area, instead of one per
  month and variable group; it has no soil or snow variables, which ERA5-Land provides).

Variables are stored under the hourly names of the Open-Meteo products (``temperature_2m``,
``precipitation``...), in the same units and with the same conventions (accumulations over the
hour ending at the timestamp, UTC), so that :mod:`geomon.weather` aggregates every product the
same way. Derived: relative humidity and vapour pressure deficit (from the dew point), wind
speed (from its components), reference evapotranspiration ET0 (FAO-56 hourly Penman-Monteith,
:func:`et0_from_reanalysis`).

Only the grid cells touching the site's communes are kept. The first run downloads the history
(from GEOMON_WEATHER_START, or GEOMON_ERA5_START for ERA5) in yearly requests, kept under
data/raw/<product>/<site> so that an interrupted run resumes; later runs ask for the days
published since (plus the last GEOMON_WEATHER_OVERLAP_DAYS) and append them. ERA5 and ERA5-Land
are first published as preliminary data (ERA5T), replaced 2-3 months later: every
GEOMON_ERA5_REVISION_EVERY_DAYS the last GEOMON_ERA5_REVISION_DAYS are fetched again. The CDS
publishes these time series about 10 days behind real time.

Used when GEOMON_ERA5_SOURCE=cds (default); with ``openmeteo`` the steps do nothing and the
weather step fetches ``om_era5land`` from Open-Meteo instead.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr
from shapely.geometry import box, shape

from ..aoi import AOI
from ..config import settings
from ..weather import PRODUCTS, store_path, write_product
from . import cds

log = logging.getLogger(__name__)

_SOIL = [f"soil_{k}_level_{i}" for k in ("temperature", "volumetric_water") for i in (1, 2, 3, 4)]


@dataclass(frozen=True)
class Spec:
    name: str                   # product (store) name
    dataset: str                # CDS dataset
    step: float                 # grid spacing, degrees
    max_box: float              # largest area of one request, degrees
    variables: tuple[str, ...]  # CDS request names

    @property
    def start(self) -> dt.date:
        return settings.era5_start if self.name == "era5" else settings.weather_start


LAND = Spec("era5land", "reanalysis-era5-land-timeseries", 0.1, 1.0, (
    "2m_temperature", "2m_dewpoint_temperature", "surface_pressure", "total_precipitation", "snowfall",
    "surface_solar_radiation_downwards", "surface_thermal_radiation_downwards",
    "10m_u_component_of_wind", "10m_v_component_of_wind", "snow_depth",
    "soil_temperature_level_1", "soil_temperature_level_2", "soil_temperature_level_3", "soil_temperature_level_4",
    "volumetric_soil_water_level_1", "volumetric_soil_water_level_2", "volumetric_soil_water_level_3",
    "volumetric_soil_water_level_4"))

ERA5 = Spec("era5", "reanalysis-era5-single-levels-timeseries", 0.25, 2.0, (
    "2m_temperature", "2m_dewpoint_temperature", "surface_pressure", "total_precipitation",
    "surface_solar_radiation_downwards", "surface_thermal_radiation_downwards",
    "10m_u_component_of_wind", "10m_v_component_of_wind", "10m_wind_gust_since_previous_post_processing",
    "100m_u_component_of_wind", "100m_v_component_of_wind", "total_cloud_cover", "boundary_layer_height",
    "surface_latent_heat_flux", "surface_sensible_heat_flux"))


# ---------------------------------------------------------------------------
# Physics: humidity and reference evapotranspiration (FAO-56)
# ---------------------------------------------------------------------------
SIGMA_H = 2.043e-10          # Stefan-Boltzmann constant, MJ m-2 h-1 K-4 (FAO-56)
EMISSIVITY = 0.98            # of the ground, for the long-wave balance
LATENT_HEAT = 2.45e6         # J kg-1: 1 mm of water evaporated takes 2.45 MJ m-2 (FAO-56)


def saturation_vp(t: np.ndarray) -> np.ndarray:
    """Saturation vapour pressure (kPa) at temperature t (°C), FAO-56 eq. 11."""
    return 0.6108 * np.exp(17.27 * t / (t + 237.3))


def et0_fao56_hourly(t, ea, u2, rn, g, p_kpa):
    """FAO-56 hourly Penman-Monteith (eq. 53), mm per hour; negative values (dew) set to 0.

    t: air temperature (°C), ea: actual vapour pressure (kPa), u2: wind speed at 2 m (m/s),
    rn and g: net radiation and soil heat flux (MJ m-2 h-1), p_kpa: air pressure.
    """
    es = saturation_vp(t)
    delta = 4098 * es / (t + 237.3) ** 2
    gamma = 0.000665 * p_kpa
    et0 = (0.408 * delta * (rn - g) + gamma * 37 / (t + 273) * u2 * (es - ea)) / (delta + gamma * (1 + 0.34 * u2))
    return np.maximum(et0, 0.0)


def et0_from_reanalysis(t, td, wind10, ssrd, strd, sp):
    """ET0 (mm per hour) from reanalysis fields: t, td (°C), 10 m wind (m/s), downward short- and
    long-wave radiation over the hour (J m-2), surface pressure (Pa).

    The net radiation uses the reanalysis' own downward long-wave radiation, rather than FAO's
    estimate from cloudiness: Rn = 0.77 Rs + ε (L↓ - σT⁴), albedo 0.23 of the reference grass.
    """
    ea = saturation_vp(td)
    u2 = wind10 * 4.87 / np.log(67.8 * 10 - 5.42)            # FAO-56 eq. 47, 10 m -> 2 m
    rs = ssrd / 1e6
    rn = 0.77 * rs + EMISSIVITY * (strd / 1e6 - SIGMA_H * (t + 273.16) ** 4)
    g = np.where(rs > 0, 0.1, 0.5) * rn                       # FAO-56 eq. 45-46: day, night
    return et0_fao56_hourly(t, ea, u2, rn, g, sp / 1000)


# ---------------------------------------------------------------------------
# File variables (ECMWF short names, SI units) -> store variables (Open-Meteo names and units)
# ---------------------------------------------------------------------------
_SUM = "sum over the hour ending at the timestamp"
_MEAN = "mean over the hour ending at the timestamp"
_INST = "instantaneous"
_LAYERS = {1: "0_to_7cm", 2: "7_to_28cm", 3: "28_to_100cm", 4: "100_to_255cm"}
_LAYER_CM = {1: "0-7 cm", 2: "7-28 cm", 3: "28-100 cm", 4: "100-289 cm"}


def to_store(raw: xr.Dataset) -> dict[str, tuple[np.ndarray, dict]]:
    """Store variables computed from the raw fields present: {name: (array, attrs)}."""
    def get(k):
        return raw[k].values.astype("float64") if k in raw else None

    out: dict[str, tuple[np.ndarray, dict]] = {}

    def put(name, values, units, ref, cds_name, long_name=None):
        out[name] = (values.astype("float32"),
                     {"units": units, "time_reference": ref, "cds_variable": cds_name,
                      "long_name": long_name or name.replace("_", " ")})

    t, td, sp = get("t2m"), get("d2m"), get("sp")
    if t is not None:
        t = t - 273.15
        put("temperature_2m", t, "degC", _INST, "2m_temperature")
    if td is not None:
        td = td - 273.15
        put("dew_point_2m", td, "degC", _INST, "2m_dewpoint_temperature")
    if t is not None and td is not None:
        es, ea = saturation_vp(t), saturation_vp(td)
        put("relative_humidity_2m", np.clip(100 * ea / es, 0, 100), "percent", _INST, "from 2m temperature and dew point")
        put("vapour_pressure_deficit", np.maximum(es - ea, 0), "kPa", _INST, "from 2m temperature and dew point")
    if sp is not None:
        put("surface_pressure", sp / 100, "hPa", _INST, "surface_pressure")
    if (tp := get("tp")) is not None:
        put("precipitation", np.maximum(tp, 0) * 1000, "mm", _SUM, "total_precipitation")
    if (sf := get("sf")) is not None:
        put("snowfall_water_equivalent", np.maximum(sf, 0) * 1000, "mm", _SUM, "snowfall")
    ssrd, strd = get("ssrd"), get("strd")
    if ssrd is not None:
        put("shortwave_radiation", np.maximum(ssrd, 0) / 3600, "W/m**2", _MEAN, "surface_solar_radiation_downwards")
    if strd is not None:
        put("longwave_radiation", strd / 3600, "W/m**2", _MEAN, "surface_thermal_radiation_downwards")
    u, v = get("u10"), get("v10")
    wind = np.hypot(u, v) if u is not None and v is not None else None
    if wind is not None:
        put("wind_speed_10m", wind, "m/s", _INST, "10m u and v components of wind")
    if (u100 := get("u100")) is not None and (v100 := get("v100")) is not None:
        put("wind_speed_100m", np.hypot(u100, v100), "m/s", _INST, "100m u and v components of wind")
    if (fg := get("fg10")) is not None:
        put("wind_gusts_10m", fg, "m/s", "maximum over the hour ending at the timestamp",
            "10m_wind_gust_since_previous_post_processing")
    if (tcc := get("tcc")) is not None:
        put("cloud_cover", tcc * 100, "percent", _INST, "total_cloud_cover")
    if (blh := get("blh")) is not None:
        put("boundary_layer_height", blh, "m", _INST, "boundary_layer_height")
    if (sde := get("sde")) is not None:
        put("snow_depth", sde, "m", _INST, "snow_depth")
    # ECMWF fluxes are positive downwards: evaporation and the sensible heat lost by the surface are negative
    if (slhf := get("slhf")) is not None:
        put("evapotranspiration", -slhf / LATENT_HEAT, "mm", _SUM, "surface_latent_heat_flux",
            "actual evapotranspiration, from the latent heat flux")
    if (sshf := get("sshf")) is not None:
        put("sensible_heat_flux", -sshf / 3600, "W/m**2", _MEAN, "surface_sensible_heat_flux",
            "sensible heat flux from the surface to the air (upwards positive)")
    for i in (1, 2, 3, 4):
        if (st := get(f"stl{i}")) is not None:
            put(f"soil_temperature_{_LAYERS[i]}", st - 273.15, "degC", _INST, f"soil_temperature_level_{i}",
                f"soil temperature, layer {i} ({_LAYER_CM[i]})")
        if (sw := get(f"swvl{i}")) is not None:
            put(f"soil_moisture_{_LAYERS[i]}", sw, "m**3/m**3", _INST, f"volumetric_soil_water_level_{i}",
                f"volumetric soil water, layer {i} ({_LAYER_CM[i]})")
    if all(x is not None for x in (t, td, wind, ssrd, strd, sp)):
        put("et0_fao_evapotranspiration", et0_from_reanalysis(t, td, wind, ssrd, strd, sp), "mm", _SUM,
            "computed", "reference evapotranspiration ET0 (FAO-56 hourly Penman-Monteith)")
    return out


# ---------------------------------------------------------------------------
# Grid cells and requests
# ---------------------------------------------------------------------------
def _centres(a: float, b: float, step: float) -> list[float]:
    """Centres (multiples of step) of the cells overlapping [a, b]."""
    k0 = math.ceil((a - step / 2) / step - 1e-9)
    k1 = math.floor((b + step / 2) / step + 1e-9)
    return [round(k * step, 4) for k in range(k0, k1 + 1)]


def cells(aoi: AOI, step: float) -> list[tuple[float, float]]:
    """Centres (lon, lat) of the grid cells touching the site's communes (of the whole extent
    when none does)."""
    x0, y0, x1, y1 = aoi.bbox_wgs84
    pts = [(lo, la) for la in _centres(y0, y1, step) for lo in _centres(x0, x1, step)]
    geom, h = shape(aoi.geom_wgs84), step / 2
    inside = [p for p in pts if box(p[0] - h, p[1] - h, p[0] + h, p[1] + h).intersects(geom)]
    return inside or pts


def boxes(points: list[tuple[float, float]], step: float, max_box: float) -> list[tuple[list[float], list]]:
    """The points in request areas of at most max_box degrees: [([N, W, S, E], points)].

    An area reaches half a cell beyond the outer centres: the CDS grid carries float noise
    (45.400000000001), and a centre lying exactly on the edge of an area may be left out.
    """
    span = math.floor(max_box / step + 1e-9) * step - step       # centre to centre: + 2 half cells = max_box
    h = step / 2
    lons, lats = sorted({p[0] for p in points}), sorted({p[1] for p in points})

    def runs(values):
        out, i = [], 0
        while i < len(values):
            j = i
            while j + 1 < len(values) and values[j + 1] - values[i] <= span + 1e-9:
                j += 1
            out.append((values[i], values[j]))
            i = j + 1
        return out

    out = []
    for w, e in runs(lons):
        for s, n in runs(lats):
            sel = [p for p in points if w - 1e-9 <= p[0] <= e + 1e-9 and s - 1e-9 <= p[1] <= n + 1e-9]
            if sel:
                out.append(([round(n + h, 4), round(w - h, 4), round(s - h, 4), round(e + h, 4)], sel))
    return out


def _raw_points(datasets: list[xr.Dataset], points: list, step: float) -> xr.Dataset:
    """The fields of a download at the given cell centres: (time, point)."""
    parts = []
    for d in datasets:
        if "valid_time" in d.dims:
            d = d.rename({"valid_time": "time"})
        keep = [v for v in d.data_vars if set(d[v].dims) == {"time", "latitude", "longitude"}]
        parts.append(d[keep].drop_vars([c for c in d.coords if c not in ("time", "latitude", "longitude")]))
    merged = xr.merge(parts, compat="override", join="outer")
    lon = xr.DataArray([p[0] for p in points], dims="point")
    lat = xr.DataArray([p[1] for p in points], dims="point")
    sel = merged.sel(longitude=lon, latitude=lat, method="nearest", tolerance=step / 4)
    return sel.drop_vars(["latitude", "longitude"]).transpose("time", "point")


def _fetch(spec: Spec, groups: list, first: dt.date, last: dt.date, folder: Path) -> tuple[xr.Dataset, list[Path]]:
    """The raw fields of a period at every point (one request per area); downloads kept in folder."""
    folder.mkdir(parents=True, exist_ok=True)
    parts, files = [], []
    for area, pts in groups:
        key = hashlib.sha1(json.dumps([spec.dataset, list(spec.variables), area]).encode()).hexdigest()[:8]
        path = folder / f"{spec.name}_{key}_{first}_{last}.zip"
        if not path.exists():
            log.info("%s %s to %s, area %s, %d variables: one CDS request (it may wait in the CDS queue)",
                     spec.name, first, last, area, len(spec.variables))
            cds.retrieve(spec.dataset, {"variable": list(spec.variables), "area": area,
                                        "date": [f"{first}/{last}"], "data_format": "netcdf"}, path)
        parts.append(_raw_points(cds.open_download(path), pts, spec.step))
        files.append(path)
    return xr.concat(parts, "point"), files


def _dataset(raw: xr.Dataset, points: list) -> xr.Dataset:
    raw = raw.sortby("time")
    raw = raw.isel(time=~raw.get_index("time").duplicated(keep="last"))
    data = {k: (("time", "point"), a, attrs) for k, (a, attrs) in to_store(raw).items()}
    return xr.Dataset(data, coords={"time": raw["time"].values, "point": np.arange(len(points)),
                                    "lon": ("point", np.array([p[0] for p in points], "float64")),
                                    "lat": ("point", np.array([p[1] for p in points], "float64"))})


def _years(start: dt.date, end: dt.date) -> list[tuple[dt.date, dt.date]]:
    return [(max(start, dt.date(y, 1, 1)), min(end, dt.date(y, 12, 31))) for y in range(start.year, end.year + 1)]


def _stored(name: str, signature: str) -> xr.Dataset | None:
    """The current store when it was built from the same request (not loaded)."""
    path = store_path(name)
    if not (path / "zarr.json").exists():
        return None
    ds = xr.open_zarr(path, consolidated=True, chunks=None)
    if ds.attrs.get("signature") != signature:
        ds.close()
        return None
    return ds


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------
def update(spec: Spec, aoi: AOI) -> dict:
    """Brings a product up to the last day published (see the module documentation)."""
    if settings.era5_source != "cds":
        return {"skipped": f"GEOMON_ERA5_SOURCE={settings.era5_source}: fetched from Open-Meteo by the weather step"}
    if not settings.cds_api_key:
        log.warning("GEOMON_CDS_API_KEY is not set: %s skipped (free Copernicus account needed)", spec.name)
        return {"skipped": "GEOMON_CDS_API_KEY not set"}
    points = cells(aoi, spec.step)
    groups = boxes(points, spec.step, spec.max_box)
    start, until = spec.start, cds.available_until(spec.dataset)
    today = dt.datetime.now(dt.timezone.utc).date()
    signature = hashlib.sha1(json.dumps({"dataset": spec.dataset, "variables": list(spec.variables),
                                         "points": points, "start": str(start)}).encode()).hexdigest()[:16]
    folder = settings.raw_dir / spec.name / aoi.code
    store = _stored(spec.name, signature)

    if store is not None:                              # the days published since, appended
        last_hour = store.time.values[-1]
        end = pd.Timestamp(last_hour).date()
        revised = store.attrs.get("revised")
        revise = revised is None or (today - dt.date.fromisoformat(revised)).days >= settings.era5_revision_every_days
        back = settings.era5_revision_days if revise else settings.weather_overlap_days
        first = max(start, min(end, until) - dt.timedelta(days=back))
        if until <= end and not revise:
            store.close()
            log.info("%s unchanged: published until %s, already stored", spec.name, until)
            return {"unchanged": True, "end": end.isoformat()}
        if revise:
            log.info("%s: fetching the last %d days again (ERA5T replaced by the final release)", spec.name, back)
        raw, files = _fetch(spec, groups, first, until, folder / "updates")
        new = _dataset(raw, points)
        with store:
            kept = store.sel(time=store.time < np.datetime64(first)).load()
            attrs = dict(store.attrs)
        ds = xr.concat([kept, new], dim="time", data_vars="all", coords="minimal", compat="override", join="outer")
        added = int((new.time.values > last_hour).sum())
        attrs["revised"] = today.isoformat() if revise else attrs.get("revised")
    else:                                              # the whole history, one request per year
        chunks = _years(start, until)
        log.info("%s: history %s to %s, %d cells, %d request(s) per year", spec.name, start, until,
                 len(points), len(groups))
        raws, files = [], []
        for a, b in chunks:
            r, f = _fetch(spec, groups, a, b, folder)
            raws.append(r)
            files += f
        ds = _dataset(xr.concat(raws, "time"), points)
        added = int(ds.sizes["time"])
        attrs = {"revised": today.isoformat()}
        for p in folder.glob("*.zip"):                 # downloads of earlier requests (other period or cells)
            if p not in files:
                p.unlink()
        files = []                                     # the yearly downloads are kept (a rebuild reuses them)

    ds.attrs = attrs | {
        "product": spec.name, "source": PRODUCTS[spec.name].source, "dataset": spec.dataset, "aoi": aoi.code,
        "signature": signature, "start": str(ds.time.values[0])[:10], "end": str(ds.time.values[-1])[:10],
        "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "time_zone": "UTC",
        "grid": f"{spec.step}° regular lat/lon; lon/lat are cell centres",
        "license": "CC-BY 4.0, Copernicus Climate Change Service (C3S), ECMWF",
    }
    write_product(ds, spec.name)
    for f in files:                                    # update downloads: now in the store
        f.unlink(missing_ok=True)
    return {"cells": len(points), "requests": len(groups), "start": ds.attrs["start"], "end": ds.attrs["end"],
            "new_hours": added, "variables": len(ds.data_vars)}


def run_land(aoi: AOI) -> dict:
    return update(LAND, aoi)


def run_era5(aoi: AOI) -> dict:
    return update(ERA5, aoi)
