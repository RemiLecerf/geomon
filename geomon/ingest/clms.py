"""Steps ``clms-soil``, ``clms-dmp`` and ``clms-vpp``: Copernicus Land Monitoring Service products
through openEO (see geomon.clms). Each reads only what is missing since the end of its store; the
long periods go through batch jobs, the updates through small synchronous requests."""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr
from shapely.geometry import shape

from .. import clms, parcels
from ..aoi import AOI
from ..config import settings
from ..db import connect
from ..weather import PRODUCTS, open_product, write_product
from ..zarrio import write_atomic
from . import sentinel2 as ing2

log = logging.getLogger(__name__)

OVERLAP_DAYS = 5               # late products: the last days are read again
BATCH_DAYS = 1461              # the record is read four years per job (the updates: a few days)


def _raw(aoi: AOI) -> Path:
    d = settings.raw_dir / "clms" / aoi.code
    d.mkdir(parents=True, exist_ok=True)
    return d


def _box(aoi: AOI, margin: float) -> dict:
    x0, y0, x1, y1 = aoi.bbox_wgs84
    return {"west": x0 - margin, "south": y0 - margin, "east": x1 + margin, "north": y1 + margin, "crs": "EPSG:4326"}


def _snap(v, step: float) -> np.ndarray:
    """Coordinates on a grid of ``step`` (cell centres on a multiple of half a step): the versions of
    a product and its batch jobs differ in the last digits, which xarray would align as other cells."""
    return np.round(np.asarray(v, dtype="float64") / step * 2) / 2 * step


def _fetch(conn, collection: str, box: dict, start: dt.date, end: dt.date, bands, dest: Path, label: str,
           step: float):
    """A period of a collection over a box, read as (time, y, x) on its grid of ``step`` degrees, without
    CF decoding; None when no product falls in it. The download is kept until the caller has written its store (``_done``), so
    that a failure in a later period does not cost the jobs already run again."""
    cube = conn.load_collection(collection, spatial_extent=box, bands=list(bands) if bands else None,
                                temporal_extent=[start.isoformat(), (end + dt.timedelta(days=1)).isoformat()])
    if not ing2.download(cube, dest, label, (end - start).days > ing2.SYNC_MAX_DAYS, f"geomon {label}"):
        return None
    with xr.open_dataset(dest, mask_and_scale=False) as f:
        ds = f.load()
    ds = ds.rename({"t": "time"}) if "t" in ds.dims else ds
    if "time" not in ds.dims or not ds.sizes["time"]:
        return None
    crs = ing2.nc_crs(ds)
    ds = ds.drop_vars([v for v in ds.data_vars if v in ("crs", "spatial_ref")])
    ds["time"] = pd.DatetimeIndex(ds["time"].values).normalize()
    ds = ds.assign_coords(x=_snap(ds["x"], step), y=_snap(ds["y"], step))
    ds.attrs = {"crs": crs}
    return ds


def _published_until(aoi: AOI, collection: str, since: dt.date) -> dt.date | None:
    """Day of the collection's newest product over the site since ``since``, from the CDSE STAC
    catalogue (free; its ids are the openEO ones in lower case, ``_cog``): the CLMS products come
    out weeks late, and an openEO job over a period without product fails after costing credits.
    None when there is none; today when the catalogue cannot tell."""
    from ..scheduler import newest_scene        # the scheduler imports the steps

    x0, y0, x1, y1 = aoi.bbox_wgs84
    try:
        t = newest_scene(f"{x0},{y0},{x1},{y1}", f"{since.isoformat()}T00:00:00Z", f"{collection.lower()}_cog")
    except Exception as exc:
        log.warning("  STAC catalogue of %s unavailable (%s): reading up to today", collection, exc)
        return dt.date.today()
    return None if t is None else dt.date.fromisoformat(t[:10])


def _done(aoi: AOI, prefix: str) -> None:
    """Removes a product's downloads once its store is written."""
    for f in _raw(aoi).glob(f"{prefix}_*.nc"):
        ing2._remove_download(f)


def _newer(published: dt.date | None, end: dt.date | None) -> bool:
    """A product newer than a store ending on ``end``: a catalogue item is dated up to a day after its
    data (and a 10-day one by its first day)."""
    return published is not None and (end is None or published > end + dt.timedelta(days=1))


def _periods(start: dt.date, end: dt.date, days: int) -> list[tuple[dt.date, dt.date]]:
    out = []
    while start <= end:
        out.append((start, min(end, start + dt.timedelta(days=days - 1))))
        start += dt.timedelta(days=days)
    return out


# ---------------------------------------------------------------------------
# Soil moisture: weather products clms_ssm and clms_swi
# ---------------------------------------------------------------------------
def _to_points(ds: xr.Dataset, bands) -> tuple[dict, np.ndarray, np.ndarray]:
    """(time, y, x) bands -> {band: (time, point)} and the points' longitudes and latitudes."""
    lon, lat = np.meshgrid(ds["x"].values, ds["y"].values)
    data = {b: ds[b].values.reshape(ds.sizes["time"], -1).astype("float32") for b in bands if b in ds}
    return data, lon.ravel(), lat.ravel()


def _soil_product(aoi: AOI, conn, name: str, pieces, bands, long_names: dict) -> dict:
    """One product (clms_ssm or clms_swi): the days missing since the end of its store, read from
    the collection versions covering them (``pieces``: (collection, first day, last day or None))."""
    old = open_product(name)
    box = _box(aoi, clms.CELL_1KM)
    today = dt.date.today()
    end = None if old is None else pd.Timestamp(old["time"].values[-1]).date()
    since = pieces[0][1] if end is None else max(pieces[0][1], end - dt.timedelta(days=OVERLAP_DAYS))
    frames = []
    for collection, first, last in pieces:
        a, b = max(since, first), min(today, last or today)
        if a <= b:              # read again from ``since`` only when a day after the store's end is out
            published = _published_until(aoi, collection, a)
            b = min(b, published) if _newer(published, end) else a - dt.timedelta(days=1)    # else no period
        for p0, p1 in _periods(a, b, BATCH_DAYS):
            ds = _fetch(conn, collection, box, p0, p1, bands, _raw(aoi) / f"{name}_{p0}.nc", f"{aoi.code} {name} {p0}..{p1}",
                        clms.CELL_1KM)
            if ds is not None:
                frames.append(ds)
    if not frames:
        _done(aoi, name)
        return {"added": 0, "end": None if end is None else str(end)}
    new = xr.concat(frames, dim="time").sortby("time")
    new = new.isel(time=~new.get_index("time").duplicated(keep="last"))
    data, lon, lat = _to_points(new, bands)
    times = new["time"].values
    if old is not None:
        times, data, lon, lat = _merge_points(old, times, data, lon, lat)
    ds = xr.Dataset({b: (("time", "point"), v, {"units": "percent", "long_name": long_names[b]}) for b, v in data.items()},
                    coords={"time": times, "point": np.arange(len(lon)), "lon": ("point", lon), "lat": ("point", lat)},
                    attrs={"product": name, "source": PRODUCTS[name].source, "aoi": aoi.code, "grid_step": clms.CELL_1KM,
                           "start": str(times[0])[:10], "end": str(times[-1])[:10],
                           "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                           "license": "Copernicus Land Monitoring Service (free, full and open)"})
    write_product(ds, name)
    _done(aoi, name)
    return {"added": int(len(new["time"])), "end": str(times[-1])[:10], "cells": int(len(lon))}


def _merge_points(old: xr.Dataset, times, data: dict, lon, lat):
    """The store's days not read again, then the new ones, on the union of their cells (matched on the
    grid: a store's point repeated by an earlier misalignment is merged back into one)."""
    olon, olat = _snap(old["lon"].values, clms.CELL_1KM), _snap(old["lat"].values, clms.CELL_1KM)
    cells: dict = {}
    for key in [*zip(olon, olat), *zip(lon, lat)]:
        cells.setdefault(key, len(cells))
    ocol = np.array([cells[k] for k in zip(olon, olat)], dtype=np.intp)
    ncol = np.array([cells[k] for k in zip(lon, lat)], dtype=np.intp)
    keep = ~np.isin(old["time"].values, times)
    out_times = np.concatenate([old["time"].values[keep], times])
    order = np.argsort(out_times, kind="stable")
    out = {}
    for b, v in data.items():
        a = np.full((len(out_times), len(cells)), np.nan, dtype="float32")
        if b in old:
            ov = old[b].values[keep]
            for j, c in enumerate(ocol):                # repeated cells: the values of each
                a[: len(ov), c] = np.where(np.isfinite(ov[:, j]), ov[:, j], a[: len(ov), c])
        a[len(out_times) - len(times):, ncol] = v
        out[b] = a[order]
    key = np.array(list(cells), dtype="float64")
    return out_times[order], out, key[:, 0], key[:, 1]


def run_soil(aoi: AOI) -> dict:
    conn = ing2._connection()
    ssm = _soil_product(aoi, conn, "clms_ssm", [(clms.SSM, clms.SSM_START, None)], ["ssm"],
                        {"ssm": "Surface soil moisture (Sentinel-1, top few cm)"})
    swi = _soil_product(aoi, conn, "clms_swi", list(clms.SWI), list(clms.SWI_BANDS),
                        {b: f"Soil Water Index T={int(b[3:])}" for b in clms.SWI_BANDS})
    return {"ssm": ssm, "swi": swi}


# ---------------------------------------------------------------------------
# Dry matter productivity
# ---------------------------------------------------------------------------
def run_dmp(aoi: AOI) -> dict:
    conn = ing2._connection()
    old = clms.open_dmp(aoi.code)
    today = dt.date.today()
    end = None if old is None else pd.Timestamp(old["time"].values[-1]).date()
    since = clms.DMP_START if end is None else end - dt.timedelta(days=20)
    published = _published_until(aoi, clms.DMP, since)
    frames = []
    for p0, p1 in _periods(since, min(today, published), BATCH_DAYS) if _newer(published, end) else []:
        ds = _fetch(conn, clms.DMP, _box(aoi, 0.006), p0, p1, ["dmp"], _raw(aoi) / f"dmp_{p0}.nc", f"{aoi.code} DMP {p0}..{p1}",
                    clms.CELL_300M)
        if ds is not None:
            frames.append(ds[["dmp"]].astype("float32"))
    if not frames:
        _done(aoi, "dmp")
        return {"added": 0, "last": None if end is None else str(end)}
    new = xr.concat(frames, dim="time").sortby("time")
    new = new.isel(time=~new.get_index("time").duplicated(keep="last"))
    if old is not None and old.sizes["x"] == new.sizes["x"] and old.sizes["y"] == new.sizes["y"]:
        old = old[["dmp"]].assign_coords(x=_snap(old["x"], clms.CELL_300M), y=_snap(old["y"], clms.CELL_300M))
        new = xr.concat([old.drop_sel(time=new["time"].values, errors="ignore"), new], dim="time").sortby("time")
    new["dmp"].attrs = {"units": "kg/ha/day", "long_name": "Dry matter productivity"}
    new.attrs = {"crs": "EPSG:4326", "source": f"{clms.ATTRIBUTION}, {clms.DMP}",
                 "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    write_atomic(new, clms.dmp_path(aoi.code))
    _done(aoi, "dmp")
    return {"dekads": int(new.sizes["time"]), "last": str(new["time"].values[-1])[:10]}


# ---------------------------------------------------------------------------
# Vegetation phenology and productivity, per parcel
# ---------------------------------------------------------------------------
def _vpp_rows(site: str, ds: xr.Dataset, year: int) -> list[tuple]:
    """Per parcel (that year's RPG parcels, and the farm's fields in its land-cover years): median
    start, peak and end dates (decoded), length, PPI peak and amplitude, productivity, quality."""
    dates = clms.decode_dates(ds["SOSD"].values, ds["MAXD"].values, ds["EOSD"].values, ds["LENGTH"].values, year)
    other = {}
    for b in ("LENGTH", "MAXV", "AMPL", "SPROD", "TPROD", "QFLAG"):
        a = ds[b].values.ravel().astype("float64")
        a[(a <= 0) | (a >= 65535)] = np.nan
        other[b] = a * clms.VPP_SCALE.get(b, 1.0)
    layers = [parcels.geometry_year(year, parcels.rpg_years())]
    if year in parcels.farm_years(site):
        layers.append(parcels.FARM_LAYER)
    base = dt.date(year, 1, 1)
    rows = []
    for gy in [g for g in layers if g is not None]:
        lab, ids, npix = parcels._labels(site, gy, ds)
        lab = lab.ravel()
        order = np.argsort(lab, kind="stable")
        bounds = np.searchsorted(lab[order], np.arange(len(ids) + 2))
        for k, pid in enumerate(ids, start=1):
            idx = order[bounds[k]:bounds[k + 1]]
            if idx.size < 5:
                continue
            with np.errstate(all="ignore"):
                n_dated = int(np.isfinite(dates[0, idx]).sum())
                d = [base + dt.timedelta(days=int(round(float(np.nanmedian(dates[j, idx])))))
                     if n_dated >= max(5, clms.VPP_MIN_DATED * idx.size) else None for j in range(3)]
                vals = {b: float(np.nanmedian(other[b][idx])) if np.isfinite(other[b][idx]).any() else None for b in other}
            if all(v is None for v in vals.values()):
                continue
            rows.append((site, year, gy, pid, *d, vals["LENGTH"], vals["MAXV"], vals["AMPL"], vals["SPROD"], vals["TPROD"],
                         vals["QFLAG"], int(idx.size), n_dated))
    return rows


def _vpp_last_year(conn) -> int:
    """Last HR-VPP season published: the end of the openEO collection's extent (free to read; the
    collection is not in the Data Space's STAC catalogue), else last year."""
    try:
        end = conn.describe_collection(clms.VPP)["extent"]["temporal"]["interval"][0][1]
    except Exception as exc:
        log.warning("  HR-VPP extent unknown (%s)", exc)
        end = None
    return int(end[:4]) if end else dt.date.today().year - 1


def run_vpp(aoi: AOI) -> dict:
    """The years published and not stored yet (one batch job per part for all of them)."""
    with connect() as conn:
        done = {r["year"] for r in conn.execute("SELECT DISTINCT year FROM eo.parcel_vpp WHERE site = %s", (aoi.code,))}
    years = [y for y in range(clms.VPP_FIRST_YEAR, dt.date.today().year) if y not in done]
    if not years:
        return {"years": sorted(done), "added": 0}
    conn = ing2._connection()
    last = _vpp_last_year(conn)
    years = [y for y in years if y <= last]
    if not years:                       # a season comes out the following year: no job until then
        return {"years": sorted(done), "added": 0, "published_until": last}
    rows, found = [], set()
    for part in ing2.split_parts(aoi):
        geom = ing2._extent(part)
        x0, y0, x1, y1 = shape(geom).bounds
        cube = conn.load_collection(clms.VPP, bands=list(clms.VPP_BANDS),
                                    spatial_extent={"west": x0, "south": y0, "east": x1, "north": y1, "crs": "EPSG:4326"},
                                    temporal_extent=[f"{years[0]}-01-01", f"{years[-1]}-12-31"]).mask_polygon(geom)
        dest = _raw(aoi) / f"vpp_{part.part or 'site'}_{years[0]}_{years[-1]}.nc"
        if not ing2.download(cube, dest, f"HR-VPP {years[0]}-{years[-1]}", True, f"geomon {aoi.code} HR-VPP"):
            continue
        ds = xr.open_dataset(dest, mask_and_scale=False)
        ds = ds.rename({"t": "time"}) if "t" in ds.dims else ds
        crs = ing2.nc_crs(ds)
        for i, t in enumerate(ds["time"].values):
            year = pd.Timestamp(t).year
            one = ds.isel(time=i).load()
            one.attrs = {"crs": crs}
            rows += _vpp_rows(aoi.code, one, year)
            found.add(year)
        ds.close()
        ing2._remove_download(dest)
    parcels.clear_labels()
    with connect() as conn:
        conn.execute("DELETE FROM eo.parcel_vpp WHERE site = %s AND year = ANY(%s)", (aoi.code, sorted(found)))
        with conn.cursor() as cur, cur.copy(
                "COPY eo.parcel_vpp (site, year, rpg_year, parcel_id, sos, peak, eos, length_days, ppi_max, ppi_amplitude, "
                "season_productivity, total_productivity, qflag, n_pixels, n_dated) FROM STDIN") as cp:
            for r in rows:
                cp.write_row(r)
    return {"years": sorted(found), "parcels": len(rows)}
