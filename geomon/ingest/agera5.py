"""AgERA5 daily agrometeorological indicators from the Copernicus CDS (Specifications §4.2).

AgERA5 is ERA5 aggregated to local days and corrected to a 0.1° topography,
1979 to about 10 days ago. Licence CC-BY 4.0.

It is read from the time-series edition of the dataset
(``sis-agrometeorological-indicators-timeseries``), which accepts several
variables, a date range and an area in one request: one request brings every
variable for the whole period over the AOI box: the full history since 1979
(GEOMON_AGERA5_START) is about 5 MB for a commune. The gridded
edition would need one request per variable and year (over 100), and the CDS
only accepts a few queued requests per user and dataset.

Access needs a free Copernicus account: set ``GEOMON_CDS_API_KEY`` to the
personal access token (https://cds.climate.copernicus.eu/profile) and accept the
dataset licence once on its "Download" tab. Without a key the step is skipped.
The downloads are kept under data/raw/agera5. The first run requests the whole
history; later runs only the days published since (plus the last 10 again),
appended to the store. The step does nothing when the CDS has published no new
day since.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pint
import xarray as xr

from ..aoi import AOI
from ..config import settings
from ..http import session
from ..units import registry
from ..weather import PRODUCTS, store_path
from .openmeteo import _write

log = logging.getLogger(__name__)

DATASET = "sis-agrometeorological-indicators-timeseries"
HALF_CELL = 0.05


@dataclass(frozen=True)
class Field:
    request: str              # CDS variable name
    name: str                 # geomon daily variable
    units: str                # target unit
    default_units: str        # assumed if the file carries no units attribute
    aliases: tuple[str, ...]  # names the variable may carry in the file (AgERA5 file names)


FIELDS = [
    Field("2m_temperature_24_hour_mean", "t_mean", "degC", "K", ("Temperature_Air_2m_Mean_24h",)),
    Field("2m_temperature_24_hour_minimum", "t_min", "degC", "K", ("Temperature_Air_2m_Min_24h",)),
    Field("2m_temperature_24_hour_maximum", "t_max", "degC", "K", ("Temperature_Air_2m_Max_24h",)),
    Field("precipitation_flux", "precip", "mm", "mm d-1", ("Precipitation_Flux",)),
    Field("solar_radiation_flux", "radiation", "MJ/m**2", "J m-2 d-1", ("Solar_Radiation_Flux",)),
    Field("reference_evapotranspiration", "et0", "mm", "mm d-1",
          ("ReferenceET_PenmanMonteith_FAO56", "Reference_Evapotranspiration", "ET0", "Reference_Evapotranspiration_PM_FAO56")),
    *[Field(f"2m_relative_humidity_at_{h}_00", f"rh_{h}", "percent", "%", (f"Relative_Humidity_2m_{h}h",))
      for h in ("06", "09", "12", "15", "18")],
    Field("derived_2m_relative_humidity_24_hour_minimum", "rh_min", "percent", "%",
          ("Derived_Relative_Humidity_2m_Min_24h", "Relative_Humidity_2m_Min_24h", "Relative_Humidity_2m_Min", "Relative_Humidity_2m_Derived_Min_24h")),
    Field("derived_2m_relative_humidity_24_hour_maximum", "rh_max", "percent", "%",
          ("Derived_Relative_Humidity_2m_Max_24h", "Relative_Humidity_2m_Max_24h", "Relative_Humidity_2m_Max", "Relative_Humidity_2m_Derived_Max_24h")),
    Field("vapour_pressure_24_hour_mean", "vp_mean", "hPa", "hPa", ("Vapour_Pressure_Mean_24h", "Vapour_Pressure_Mean")),
    Field("vapour_pressure_deficit_at_maximum_temperature", "vpd_tmax", "kPa", "hPa",
          ("Vapour_Pressure_Deficit_at_Maximum_Temperature", "VPD_at_Tmax", "Vapour_Pressure_Deficit_Max")),
    Field("10m_wind_speed_24_hour_mean", "wind_mean", "m/s", "m s-1", ("Wind_Speed_10m_Mean_24h", "Wind_Speed_10m_Mean")),
    Field("cloud_cover_24_hour_mean", "cloud_cover", "percent", "1", ("Cloud_Cover_Mean_24h", "Cloud_Cover_Mean")),
    Field("snow_thickness_24_hour_mean", "snow_depth", "m", "cm", ("Snow_Thickness_Mean_24h", "Snow_Thickness_Mean")),
    Field("snow_thickness_lwe_24_hour_mean", "swe", "mm", "cm", ("Snow_Thickness_LWE_Mean_24h", "Snow_Thickness_LWE_Mean")),
]


# ---------------------------------------------------------------------------
# Units: CF strings ("J m-2 d-1") -> pint, daily fluxes -> daily totals
# ---------------------------------------------------------------------------
def _cf_to_pint(units: str) -> str:
    units = units.strip()
    if units in ("", "1", "-", "dimensionless", "fraction", "0-1", "(0-1)"):
        return "dimensionless"
    if units == "%":
        return "percent"
    parts = []
    for tok in units.replace("/", " / ").split():
        if tok == "/":
            parts.append("/")
            continue
        m = re.fullmatch(r"([A-Za-z°]+)(-?\d+)?", tok)
        if not m:
            return units          # leave it to pint
        sym, exp = m.group(1), m.group(2)
        sym = {"d": "day", "day": "day", "s": "second", "h": "hour", "°C": "degC"}.get(sym, sym)
        parts.append(f"{sym}**{exp}" if exp else sym)
    out = ""
    for p in parts:
        out += p if (not out or p == "/" or out.endswith("/")) else f" * {p}"
    return out.replace("/", " / ")


def _convert(values: np.ndarray, src: str, dst: str) -> np.ndarray:
    ureg = registry()
    q = ureg.Quantity(values.astype("float64"), _cf_to_pint(src))
    for cand in (q, q * ureg.day):          # a daily flux times one day is the daily total
        for ctx in ((), ("water",)):
            try:
                return np.asarray(cand.to(dst, *ctx).magnitude)
            except pint.DimensionalityError:
                continue
    raise ValueError(f"Cannot convert {src!r} to {dst!r}")


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------
def _available_until() -> dt.date:
    """Last date published: the end of the date range offered by the request form."""
    s = session()
    r = s.get(f"{settings.cds_api_url}/catalogue/v1/collections/{DATASET}", timeout=60)
    r.raise_for_status()
    coll = r.json()
    form_url = next(l["href"] for l in coll["links"] if l.get("rel") == "form")
    form = s.get(form_url, timeout=60).json()
    for w in form:
        if w.get("name") == "date" and w.get("details", {}).get("maxEnd"):
            return dt.date.fromisoformat(w["details"]["maxEnd"][:10])
    return dt.date.fromisoformat(coll["extent"]["temporal"]["interval"][0][1][:10])


def _area(aoi: AOI) -> list[float]:
    """[N, W, S, E] covering every 0.1° cell that touches the AOI extent."""
    x0, y0, x1, y1 = aoi.bbox_wgs84
    return [round(y1 + HALF_CELL, 2), round(x0 - HALF_CELL, 2), round(y0 - HALF_CELL, 2), round(x1 + HALF_CELL, 2)]


def _request(area: list[float], start: dt.date, end: dt.date) -> dict:
    return {"variable": [f.request for f in FIELDS], "area": area,
            "date": [f"{start.isoformat()}/{end.isoformat()}"], "data_format": "netcdf"}


def _retrieve(request: dict, target: Path) -> None:
    import cdsapi  # imported here: only this step needs it

    client = cdsapi.Client(url=settings.cds_api_url, key=settings.cds_api_key, quiet=True, progress=False,
                           retry_max=3, sleep_max=60)
    try:
        client.retrieve(DATASET, request, str(target))
    except Exception as exc:
        msg = str(exc).lower()
        if "licen" in msg:
            raise RuntimeError(
                "The AgERA5 licence is not accepted for this CDS account: open "
                f"https://cds.climate.copernicus.eu/datasets/{DATASET}?tab=download and accept it") from exc
        if "queued requests" in msg:
            raise RuntimeError(
                "The CDS already has the maximum number of queued requests for this dataset on this account. "
                "Wait for them to finish (or cancel them at https://cds.climate.copernicus.eu/requests) "
                "and run the step again") from exc
        raise


# ---------------------------------------------------------------------------
# Reading the download
# ---------------------------------------------------------------------------
_DIM_ALIASES = {"time": ("time", "valid_time", "date", "day"),
                "lat": ("lat", "latitude", "y"), "lon": ("lon", "longitude", "x")}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _open_files(path: Path, tmp: str) -> list[xr.Dataset]:
    files = [path]
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            z.extractall(tmp)
        files = sorted(p for p in Path(tmp).rglob("*") if p.suffix in (".nc", ".nc4", ".netcdf"))
    out = []
    for p in files:
        ds = xr.open_dataset(p, engine="h5netcdf").load()
        ren = {}
        for std, names in _DIM_ALIASES.items():
            for n in names:
                if n in ds.variables and n != std and std not in ds.variables:
                    ren[n] = std
                    break
        out.append(ds.rename(ren))
    return out


def _match(ds_vars: dict[str, xr.DataArray]) -> dict[str, xr.DataArray]:
    """File variables -> geomon names, by CDS request name, AgERA5 file name or long name."""
    found = {}
    for f in FIELDS:
        keys = {_norm(f.request), *(_norm(a) for a in f.aliases)}
        for name, da in ds_vars.items():
            labels = {_norm(name), _norm(str(da.attrs.get("long_name", ""))), _norm(str(da.attrs.get("standard_name", "")))}
            if keys & labels:
                found[f.name] = da
                break
    return found


def _read(path: Path) -> tuple[dict[str, xr.DataArray], list[str]]:
    """Download -> {geomon name: DataArray (time, lat, lon)}, plus the unmatched file variables."""
    with tempfile.TemporaryDirectory() as tmp:
        datasets = _open_files(path, tmp)
        arrays: dict[str, xr.DataArray] = {}
        for ds in datasets:
            for v in ds.data_vars:
                da = ds[v]
                if "time" in da.dims and {"lat", "lon"} <= set(da.dims):
                    arrays[v] = da.transpose("time", "lat", "lon").drop_vars(
                        [c for c in da.coords if c not in ("time", "lat", "lon")])
    found = _match(arrays)
    used = {id(a) for a in found.values()}
    unmatched = [f"{n} ({a.attrs.get('long_name', '')}, {a.attrs.get('units', '')})"
                 for n, a in arrays.items() if id(a) not in used]
    return found, unmatched


# ---------------------------------------------------------------------------
# Step
# ---------------------------------------------------------------------------
# Days requested again before the end of the store (late corrections of the last days published)
OVERLAP_DAYS = 10


def _dataset(found: dict[str, xr.DataArray], aoi: AOI, area: list[float]) -> xr.Dataset:
    """Variables read from a download -> store layout (time, point), in geomon units."""
    ref = next(iter(found.values()))
    lats, lons = ref["lat"].values, ref["lon"].values
    glat, glon = np.meshgrid(lats, lons, indexing="ij")
    times = np.unique(np.concatenate([pd.DatetimeIndex(a["time"].values).normalize().values for a in found.values()]))
    data = {}
    for f in FIELDS:
        if f.name not in found:
            continue
        a = found[f.name]
        a = a.assign_coords(time=pd.DatetimeIndex(a["time"].values).normalize())
        a = a[~a.get_index("time").duplicated(keep="last")].reindex(time=times, lat=lats, lon=lons, method="nearest",
                                                                     tolerance=1e-3)
        src = str(a.attrs.get("units") or f.default_units)
        values = _convert(a.values.reshape(len(times), -1), src, f.units)
        data[f.name] = (("time", "point"), values.astype("float32"),
                        {"units": f.units, "source_units": src, "cds_variable": f.request,
                         "long_name": str(a.attrs.get("long_name", f.request))})
    return xr.Dataset(
        data,
        coords={"time": times, "point": np.arange(glat.size),
                "lon": ("point", glon.ravel().astype("float64")),
                "lat": ("point", glat.ravel().astype("float64"))},
        attrs={
            "product": "agera5", "source": PRODUCTS["agera5"].source, "aoi": aoi.code,
            "dataset": DATASET, "area": json.dumps(area),
            "grid": "0.1° regular lat/lon; lon/lat are cell centres",
            "day": "local solar day (AgERA5 aggregates hourly ERA5 to the local time zone)",
            "license": "CC-BY 4.0, Copernicus Climate Change Service (C3S)",
        },
    )


def _download(area: list[float], start: dt.date, until: dt.date, folder: Path) -> Path:
    """One CDS request for every variable from ``start`` to ``until``, kept in ``folder``."""
    request = _request(area, start, until)
    raw = folder / f"agera5_{start}_{until}.nc"
    log.info("AgERA5 %s to %s, area %s, %d variables: one CDS request (it may wait in the CDS queue)",
             start, until, area, len(FIELDS))
    part = raw.with_suffix(".part")
    _retrieve(request, part)
    part.replace(raw)
    (folder / "request.json").write_text(json.dumps(request, indent=1))
    return raw


def _read_checked(raw: Path) -> tuple[dict[str, xr.DataArray], list[str]]:
    found, unmatched = _read(raw)
    missing = [f.name for f in FIELDS if f.name not in found]
    if missing:
        msg = f"AgERA5: variables not recognised in the download: {', '.join(missing)}. File variables left: {unmatched}"
        if len(missing) == len(FIELDS):
            raise RuntimeError(msg)
        log.warning(msg)
    return found, missing


def _stored(area: list[float], start: dt.date) -> xr.Dataset | None:
    """The current store, when it covers the same area from the same start (not loaded)."""
    path = store_path("agera5")
    if not (path / "zarr.json").exists():
        return None
    ds = xr.open_zarr(path, consolidated=True, chunks=None)
    if ds.attrs.get("area") != json.dumps(area) or ds.attrs.get("start", "9999") > start.isoformat():
        ds.close()
        return None
    return ds


def run(aoi: AOI) -> dict:
    """Brings the store up to the last day published. The first run downloads the whole history
    (one request); later runs only the days since the end of the store, plus the last
    ``OVERLAP_DAYS`` again. Nothing is done when no new day was published."""
    if not settings.cds_api_key:
        log.warning("GEOMON_CDS_API_KEY is not set: AgERA5 skipped (free Copernicus account needed)")
        return {"skipped": "GEOMON_CDS_API_KEY not set"}
    start, until, area = max(settings.agera5_start, dt.date(1979, 1, 1)), _available_until(), _area(aoi)
    folder = settings.raw_dir / "agera5" / aoi.code
    folder.mkdir(parents=True, exist_ok=True)
    store = _stored(area, start)
    end = pd.Timestamp(store.time.values[-1]).date() if store is not None else None
    if end is not None and end >= until:
        store.close()
        log.info("AgERA5 unchanged: published until %s, already stored", until)
        return {"unchanged": True, "end": end.isoformat()}

    if end is None:                                    # whole history
        raw = _download(area, start, until, folder)
        for old in folder.glob("agera5_*.nc"):
            if old != raw:
                old.unlink()
        found, missing = _read_checked(raw)
        ds = _dataset(found, aoi, area)
        added = ds.sizes["time"]
    else:                                              # the new days, appended to the store
        first = end - dt.timedelta(days=OVERLAP_DAYS)
        raw = _download(area, first, until, folder)
        found, missing = _read_checked(raw)
        new = _dataset(found, aoi, area)
        with store:
            same_grid = (store.sizes["point"] == new.sizes["point"]
                         and np.allclose(store["lon"].values, new["lon"].values)
                         and np.allclose(store["lat"].values, new["lat"].values))
            kept = store.sel(time=store.time < new.time.values[0]).load() if same_grid else None
        if kept is None:
            raise RuntimeError("AgERA5: the grid of the new days differs from the store; delete "
                               f"{store_path('agera5')} to download the whole history again")
        added = int((new.time.values > np.datetime64(end)).sum())
        ds = xr.concat([kept, new], dim="time", data_vars="all", coords="minimal", compat="override", join="outer")
        ds.attrs = new.attrs
    ds.attrs |= {"start": str(ds.time.values[0])[:10], "end": str(ds.time.values[-1])[:10],
                 "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    _write(ds, "agera5")
    return {"cells": int(ds.sizes["point"]), "start": ds.attrs["start"], "end": ds.attrs["end"],
            "new_days": added, "variables": len(ds.data_vars), "missing": missing}
