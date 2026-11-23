"""Sentinel-1 GRD backscatter through openEO, Copernicus Data Space (see geomon.sentinel1).

From GEOMON_S1_START (2023-01-01), for each part of the site (see
geomon.ingest.sentinel2.split_parts) and each orbit direction: one batch job per
year, then short synchronous requests for the days published since. The relative
orbit of each date comes from the STAC catalogue. Statistics of VV, VH, VH/VV and
RVI over every area go to eo.index_stats (vv_asc, vh_desc...).

Uses the openEO login of the Sentinel-2 step (sentinel2-login).
"""

from __future__ import annotations

import collections
import datetime as dt
import logging

import numpy as np
import pandas as pd
import xarray as xr

from .. import sentinel1 as s1
from .. import sentinel2 as s2
from ..aoi import AOI, site_areas
from ..zarrio import read_attrs, update_attrs
from ..config import settings
from ..db import connect
from ..http import session
from . import sentinel2 as ing2
from .sentinel2 import (OVERLAP_DAYS, SYNC_MAX_DAYS, TIME_BLOCK, Part, download, nc_crs, split_parts)

log = logging.getLogger(__name__)

STAC_ITEMS = "https://stac.dataspace.copernicus.eu/v1/collections/sentinel-1-grd/items"
# Only dual-polarisation VV + VH products: some acquisitions are HH + HV (Sentinel-1C in April 2025 over
# the Manche, during its commissioning), without a VV band; one of them fails a whole year's job
# ("sar_backscatter: No tiff for band VV")
POLARISATION = "VV&VH"


def _raw(part: Part):
    d = settings.raw_dir / "sentinel1" / part.code
    return d / part.part if part.part else d


def _year_bounds(year: int) -> tuple[dt.date, dt.date]:
    return max(dt.date(year, 1, 1), settings.s1_start), min(dt.date(year, 12, 31), dt.date.today())


def _cube(conn, part: Part, direction: str, start: dt.date, end: dt.date):
    """VV and VH backscatter of one orbit direction over the part's outline (plus margin)."""
    from shapely.geometry import shape

    geom = ing2._extent(part)
    x0, y0, x1, y1 = shape(geom).bounds
    want = s1.DIRECTIONS[direction]
    cube = conn.load_collection(
        s1.COLLECTION,
        spatial_extent={"west": x0, "south": y0, "east": x1, "north": y1, "crs": "EPSG:4326"},
        temporal_extent=[start.isoformat(), (end + dt.timedelta(days=1)).isoformat()],   # end exclusive
        bands=["VV", "VH"],
        properties={"sat:orbit_state": lambda v: v == want, "polarisation": lambda v: v == POLARISATION},
    )
    return cube.sar_backscatter(coefficient=s1.COEFFICIENT).mask_polygon(geom)


def _read(path) -> xr.Dataset | None:
    """openEO NetCDF (linear backscatter) -> Dataset (time, y, x) of VV and VH in dB x 100, lazily."""
    ds = xr.open_dataset(path, engine="h5netcdf", chunks={"t": TIME_BLOCK})
    ds = ds.rename({k: v for k, v in {"t": "time"}.items() if k in ds.dims})
    if "time" not in ds.dims or ds.sizes["time"] == 0:
        return None
    out = {b: (("time", "y", "x"), s1.encode_db(ds[b]).data) for b in ("VV", "VH") if b in ds}
    times = pd.DatetimeIndex(ds["time"].values).normalize()
    res = xr.Dataset(out, coords={"time": times.values, "y": ds["y"].values, "x": ds["x"].values})
    res = res.sortby("time")
    res = res.isel(time=~res.get_index("time").duplicated(keep="last"))
    res.attrs["crs"] = nc_crs(ds)
    return res


def _orbits(geom_wgs84: dict, start: dt.date, end: dt.date, direction: str) -> dict:
    """Relative orbit of each day's acquisition of one direction over the outline (STAC catalogue)."""
    from shapely.geometry import shape

    x0, y0, x1, y1 = shape(geom_wgs84).bounds
    want = s1.DIRECTIONS[direction]
    by_day: dict[np.datetime64, collections.Counter] = collections.defaultdict(collections.Counter)
    url, params = STAC_ITEMS, {"bbox": f"{x0},{y0},{x1},{y1}", "limit": 1000,
                               "datetime": f"{start}T00:00:00Z/{end}T23:59:59Z"}
    for _ in range(100):
        r = session().get(url, params=params, timeout=120)
        r.raise_for_status()
        page = r.json()
        for f in page.get("features", []):
            p = f["properties"]
            if (p.get("sat:orbit_state") == want and p.get("sat:relative_orbit") is not None
                    and "VV" in (p.get("sar:polarizations") or ["VV"])):
                by_day[np.datetime64(p["datetime"][:10])][int(p["sat:relative_orbit"])] += 1
        url = next((link["href"] for link in page.get("links", []) if link.get("rel") == "next"), None)
        if not url:
            break
        params = None
    return {d: c.most_common(1)[0][0] for d, c in by_day.items()}


def _with_orbits(ds: xr.Dataset, part: Part, direction: str, start: dt.date, end: dt.date) -> xr.Dataset:
    orbits = _orbits(part.geom_wgs84, start, end, direction)
    ro = [orbits.get(np.datetime64(t, "D"), -1) for t in ds["time"].values]
    return ds.assign_coords(relative_orbit=("time", np.array(ro, dtype="int16")))


def _new_year(conn, part: Part, direction: str, year: int) -> dict:
    """One year without store (one part, one direction): one job, written as a new store."""
    start, end = _year_bounds(year)
    out = {"direction": direction, "year": year, "dates": []}
    if end < start:
        return out
    label = f"{part.label} {direction} {year}"
    nc = _raw(part) / f"{year}_{direction}.nc"
    if not download(_cube(conn, part, direction, start, end), nc, label, (end - start).days > SYNC_MAX_DAYS,
                    f"geomon {part.label} Sentinel-1 {direction} {year}"):
        return out
    ds = _read(nc)
    if ds is None:
        ing2._remove_download(nc)
        return out
    ds = _with_orbits(ds, part, direction, start, end)
    ds.attrs |= {"site": part.code, "part": part.part, "direction": direction, "collection": s1.COLLECTION,
                 "coefficient": s1.COEFFICIENT, "backend": settings.openeo_url, "units": "dB x 100",
                 "scale": s1.SCALE, "nodata": s1.NODATA, "requested_from": str(start), "requested_until": str(end),
                 "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    ing2._write_new(ds, s1.store_path(part.code, year, direction, part.part))
    s1.clear_cache()
    ing2._remove_download(nc)
    out["dates"] = list(ds["time"].values)
    log.info("Sentinel-1 %s: %d dates", label, len(out["dates"]))
    return out


def _update(conn, part: Part, direction: str, year: int) -> dict:
    """The days published since the last run of a stored year (one part, one direction)."""
    path = s1.store_path(part.code, year, direction, part.part)
    attrs = read_attrs(path)
    req_from = dt.date.fromisoformat(attrs["requested_from"])
    req_until = dt.date.fromisoformat(attrs["requested_until"])
    y_end = _year_bounds(year)[1]
    out = {"direction": direction, "year": year, "dates": []}
    if req_until >= y_end:
        return out
    start = max(req_from, req_until - dt.timedelta(days=OVERLAP_DAYS))
    label = f"{part.label} {direction} {year} update"
    nc = _raw(part) / f"{year}_{direction}_update.nc"
    if download(_cube(conn, part, direction, start, y_end), nc, label, (y_end - start).days > SYNC_MAX_DAYS,
                f"geomon {part.label} Sentinel-1 {direction} {year} update"):
        new = _read(nc)
        ds = xr.open_zarr(path, consolidated=True)
        if new is not None:
            have = set(ds["time"].values.astype("datetime64[D]"))
            fresh = [i for i, t in enumerate(new["time"].values.astype("datetime64[D]")) if t not in have]
            new = new.isel(time=fresh)
        if new is not None and new.sizes["time"]:
            new = new.reindex(y=ds["y"].values, x=ds["x"].values, method="nearest", tolerance=1.0,
                              fill_value=s1.NODATA)
            new = _with_orbits(new, part, direction, start, y_end)
            out["dates"] = list(new["time"].values)
            if new["time"].values.min() > ds["time"].values.max():
                new.attrs = dict(ds.attrs)       # an append writes the store's attributes: keep them
                ing2._per_date(new).to_zarr(path, append_dim="time", consolidated=True)
            else:                               # a late acquisition between stored dates: rewrite the year
                merged = xr.concat([ds, new], dim="time").sortby("time")
                merged.attrs = dict(ds.attrs)
                ing2._write_new(merged, path)
    ing2._remove_download(nc)
    update_attrs(path, requested_until=str(y_end),
                    updated=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    s1.clear_cache()
    return out


def _store_stats(site: str, direction: str, dates, areas: dict[str, dict], communes: set[str]) -> int:
    """Statistics of every measure of the given dates over every area (all the parts having the
    date), upserted into eo.index_stats."""
    days = sorted({np.datetime64(d, "D") for d in dates})
    if not days:
        return 0
    plist = [p for p in s1.parts(site) if s1.years(site, direction, p)]
    zones: dict[tuple, dict] = {}

    def zones_of(part: str, ds: xr.Dataset) -> dict:
        key = (part, s2.grid_key(ds))
        if key not in zones:
            zones[key] = s2.area_zones(ds, areas, communes)
        return zones[key]

    n_area = {code: 0 for code in areas}
    for part in plist:
        ds = s1.open_year(site, s1.years(site, direction, part)[0], direction, part)
        for code, z in zones_of(part, ds).items():
            n_area[code] += int(z.size)
    n = 0
    by_year: dict[int, list] = collections.defaultdict(list)
    for d in days:
        by_year[int(str(d)[:4])].append(d)
    for year, ds_days in by_year.items():
        stores = {p: s1.open_year(site, year, direction, p) for p in plist}
        stores = {k: v for k, v in stores.items() if v is not None}
        times = {k: v["time"].values.astype("datetime64[D]") for k, v in stores.items()}
        rows = []
        for d in ds_days:
            pieces = []
            for part, ds in stores.items():
                hit = np.nonzero(times[part] == d)[0]
                if hit.size:
                    pieces.append((ds.isel(time=int(hit[0])).load(), zones_of(part, ds)))
            if pieces:
                rows += [r | {"site": site, "date": str(d)} for r in s1.area_rows(pieces, n_area, direction)]
        ing2.upsert_index_stats(rows)
        n += len(rows)
        log.info("Sentinel-1 %s %s %d: statistics of %d dates", site, direction, year, len(ds_days))
    return n


def run(aoi: AOI) -> dict:
    if not ing2._has_login():
        log.warning("No openEO login yet: Sentinel-1 skipped. Run once: docker compose run --rm ingest sentinel2-login")
        return {"skipped": "no openEO login (run the sentinel2-login step)"}
    conn = ing2._connection()
    areas, communes = site_areas(aoi.code)
    parts_def = split_parts(aoi)
    first = settings.s1_start.year
    tasks = []
    for q in parts_def:
        for d in s1.DIRECTIONS:
            stored = set(s1.years(aoi.code, d, q.part))
            tasks += [(_new_year, (conn, q, d, y), f"{q.label} {d} {y}")
                      for y in range(first, dt.date.today().year + 1) if y not in stored]
            tasks += [(_update, (conn, q, d, y), f"{q.label} {d} {y}") for y in sorted(stored)
                      if dt.date.fromisoformat(read_attrs(s1.store_path(aoi.code, y, d, q.part))["requested_until"])
                      < _year_bounds(y)[1]]
    results, errors = ing2._parallel(tasks, "Sentinel-1")
    added = {d: [t for r in results if r["direction"] == d for t in r["dates"]] for d in s1.DIRECTIONS}
    if errors and not any(added.values()):
        raise RuntimeError(errors[0])
    rows = sum(_store_stats(aoi.code, d, added[d], areas, communes) for d in s1.DIRECTIONS)
    return {"start": str(settings.s1_start), "parts": [q.part for q in parts_def if q.part],
            "added_dates": {d: len(v) for d, v in added.items()},
            "dates": {d: sum(int(s1.open_year(aoi.code, y, d, p).sizes["time"]) for p in s1.parts(aoi.code)
                             for y in s1.years(aoi.code, d, p)) for d in s1.DIRECTIONS},
            "stat_rows": rows, "errors": errors}


def reencode(aoi: AOI) -> dict:
    """Step ``sentinel1-reencode``: the stored images rewritten with the current encoding (no download)."""
    root = s1.site_dir(aoi.code)
    paths = sorted(root.glob("**/*.zarr")) if root.exists() else []
    r = ing2.reencode_stores(paths, None)
    return {"stores": len(paths), "mb_before": r["mb_before"], "mb_after": r["mb_after"]}


def recompute(aoi: AOI) -> dict:
    """Recomputes every Sentinel-1 statistic from the stored images (no download)."""
    areas, communes = site_areas(aoi.code)
    names = [s1.stat_name(m, d) for m in s1.MEASURES for d in s1.DIRECTIONS]
    with connect() as conn:
        conn.execute("DELETE FROM eo.index_stats WHERE site = %s AND index = ANY(%s)", (aoi.code, names))
    rows = 0
    for d in s1.DIRECTIONS:
        dates = [t for p in s1.parts(aoi.code) for y in s1.years(aoi.code, d, p)
                 for t in s1.open_year(aoi.code, y, d, p)["time"].values]
        rows += _store_stats(aoi.code, d, dates, areas, communes)
    return {"rows": rows}
