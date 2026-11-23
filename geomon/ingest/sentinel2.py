"""Sentinel-2 L2A through openEO, Copernicus Data Space Ecosystem (Specifications §4.1, §5.1).

Only what is needed crosses the network: the AOI extent, the configured bands
(B04 and B08 by default, for NDVI) and the scene classification (SCL, for the
cloud mask), for scenes whose tile cloud cover is at most GEOMON_S2_MAX_CLOUD.
The back-end delivers L2A reflectance (Sen2Cor atmospheric correction) on the
native 10 m UTM grid; 20 m bands are resampled to 10 m (nearest neighbour).

History is fetched in few openEO batch jobs (Specifications §5.1), up to
GEOMON_S2_YEARS_PER_JOB years each, GEOMON_S2_PARALLEL_JOBS at a time, and stored
as one Zarr store per year. Afterwards a run only fetches the days published
since the last one (synchronous request), and bands added to GEOMON_S2_BANDS are fetched for the
dates already stored. Dates whose clear fraction over the AOI is below
GEOMON_S2_MIN_CLEAR are not stored.

Login: a free Copernicus Data Space account. Run once, interactively:

    docker compose run --rm ingest sentinel2-login

The refresh token is kept under data/state/xdg; scheduled runs use it silently.
Client credentials can be used instead (openEO client variables
OPENEO_AUTH_METHOD=client_credentials, OPENEO_AUTH_CLIENT_ID, ...).
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

from .. import fields, parcels
from .. import sentinel2 as s2
from ..aoi import AOI, site_areas
from ..config import settings
from ..db import connect
from ..zarrio import read_attrs, update_attrs, write_atomic

log = logging.getLogger(__name__)

SYNC_MAX_DAYS = 45          # longer periods go through batch jobs
TIME_BLOCK = 8              # dates read at once from an openEO result (memory: 8 x the AOI grid)
OVERLAP_DAYS = 3            # scenes can be published a little after later ones
CHUNK = 512                 # pixels of a stored chunk (each date of a band is one shard of them)
MAX_RUNS = 6                # more separate runs of clear dates: one request over the period instead


# ---------------------------------------------------------------------------
# openEO connection
# ---------------------------------------------------------------------------
def _connection(interactive: bool = False):
    import openeo

    from ..http import session

    # Our session retries dropped connections (the identity server sometimes closes them while polling)
    conn = openeo.connect(settings.openeo_url, session=session())
    if interactive or os.environ.get("OPENEO_AUTH_METHOD"):
        # Stored refresh token, client credentials, or device code flow (30 min to confirm the link)
        conn.authenticate_oidc(max_poll_time=1800)
    else:
        try:
            conn.authenticate_oidc_refresh_token()
        except Exception as exc:
            raise RuntimeError(
                "No valid openEO login for the Copernicus Data Space. Run once: "
                "docker compose run --rm ingest sentinel2-login") from exc
    return conn


def login(aoi: AOI | None = None) -> dict:
    """Interactive login (device code flow): open the printed URL and enter the code."""
    conn = _connection(interactive=True)
    me = conn.describe_account()
    log.info("Logged in to %s as %s", settings.openeo_url, me.get("name") or me.get("user_id"))
    return {"user": me.get("name") or me.get("user_id"), "backend": settings.openeo_url}


MARGIN_M = 250             # margin around the commune outlines


def _extent(aoi: AOI) -> dict:
    """Outline of the AOI communes plus a margin (GeoJSON, WGS84): what is requested from openEO."""
    from pyproj import Transformer
    from shapely.geometry import mapping, shape
    from shapely.ops import transform

    to_l93 = Transformer.from_crs("EPSG:4326", "EPSG:2154", always_xy=True).transform
    to_wgs = Transformer.from_crs("EPSG:2154", "EPSG:4326", always_xy=True).transform
    g = transform(to_l93, shape(aoi.geom_wgs84)).buffer(MARGIN_M).simplify(10)
    return mapping(transform(to_wgs, g))


def _cube(conn, aoi: AOI, bands: list[str], start: dt.date, end: dt.date):
    """Bands over the AOI outline (plus margin); pixels outside it are masked server-side, so they
    compress to almost nothing in the result."""
    from shapely.geometry import shape

    geom = _extent(aoi)
    x0, y0, x1, y1 = shape(geom).bounds
    cube = conn.load_collection(
        s2.COLLECTION,
        spatial_extent={"west": x0, "south": y0, "east": x1, "north": y1, "crs": "EPSG:4326"},
        temporal_extent=[start.isoformat(), (end + dt.timedelta(days=1)).isoformat()],   # end exclusive
        bands=bands,
        max_cloud_cover=settings.s2_max_cloud,
    )
    return cube.mask_polygon(geom)


def download(cube, dest: Path, label: str, batch: bool, title: str) -> bool:
    """Runs an openEO process graph into a NetCDF file (a complete earlier download is reused):
    a batch job for long periods, else a synchronous request. False when no scene matches."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    done = _complete_marker(dest)
    if dest.exists() and done.exists():
        log.info("  %s: reusing the complete download %s", label, dest.name)
        return True
    dest.unlink(missing_ok=True)
    t0 = time.time()
    try:
        if batch:
            job = cube.execute_batch(outputfile=str(dest), out_format="netCDF", title=title, max_poll_interval=60)
            try:                                       # credits used, as the back-end reports them
                meta = job.describe()
                log.info("  %s: openEO job %s, costs %s, usage %s", label, job.job_id, meta.get("costs"),
                         meta.get("usage"))
            except Exception:
                pass
        else:
            cube.download(str(dest), format="netCDF")
    except Exception as exc:
        text = str(exc)
        if getattr(exc, "job", None) is not None:        # a failed batch job: the reason is in its logs
            try:
                text += " ".join(str(e.get("message")) for e in exc.job.logs(level="error"))
            except Exception:
                pass
        if "NoDataAvailable" in text or "no data" in text.lower():
            log.info("  %s: no scene", label)
            return False
        raise
    log.info("  %s: %.1f MB in %.0f s", label, dest.stat().st_size / 1e6, time.time() - t0)
    done.touch()
    return True


def clear_dates(scl: xr.Dataset, mask: np.ndarray) -> dict:
    """{day: clear fraction over the site} from the scene classification alone (no brightness test)."""
    n = max(int(mask.sum()), 1)
    out = {}
    for i in range(0, scl.sizes["time"], TIME_BLOCK):
        block = np.asarray(scl["SCL"].isel(time=slice(i, i + TIME_BLOCK)).values)
        fractions = (s2.clear_mask(block, None) & mask).sum(axis=(1, 2)) / n
        for t, f in zip(scl["time"].values[i:i + TIME_BLOCK], fractions):
            out[np.datetime64(t, "D")] = float(f)
    return out


def runs_of(dates, keep: set) -> list[tuple]:
    """The runs of consecutive kept dates in the observed dates: [(first, last)]."""
    runs, cur = [], []
    for d in sorted(dates):
        if d in keep:
            cur.append(d)
        elif cur:
            runs.append((cur[0], cur[-1]))
            cur = []
    if cur:
        runs.append((cur[0], cur[-1]))
    return runs


def _fetch_clear(conn, aoi, bands: list[str], start: dt.date, end: dt.date, dest: Path,
                 have=frozenset()) -> xr.Dataset | None:
    """Two passes, to pay only for useful dates: the scene classification at 20 m (about 1/12 of the
    cost of the bands) gives each date's clear fraction over the site; the bands are then requested
    for the dates clear enough (GEOMON_S2_MIN_CLEAR) and not stored yet (``have``), one request per
    run of consecutive dates (one over the period when there would be more than MAX_RUNS). The
    back-end cannot select dates by label, hence the runs."""
    scl_nc = dest.with_name(f"{dest.stem}_scl.nc")
    cube = _cube(conn, aoi, ["SCL"], start, end).resample_spatial(resolution=20, method="near")
    if not download(cube, scl_nc, f"{start}..{end} SCL 20 m", (end - start).days > SYNC_MAX_DAYS,
                    f"geomon {aoi.code} Sentinel-2 clear dates {start}..{end}"):
        return None
    scl = _read(scl_nc)
    fractions = clear_dates(scl, s2.area_masks(scl, {aoi.code: aoi.geom_wgs84})[aoi.code]) if scl is not None else {}
    _remove_download(scl_nc)
    have = {np.datetime64(d, "D") for d in have}
    keep = {d for d, f in fractions.items() if f >= settings.s2_min_clear and d not in have}
    log.info("  %s..%s: %d dates, %d clear enough (>= %.0f %%) and new", start, end, len(fractions), len(keep),
             100 * settings.s2_min_clear)
    if not keep:
        return None
    runs = runs_of(fractions, keep)
    if len(runs) > MAX_RUNS:
        runs = [(min(keep), max(keep))]
    parts = []
    for i, (a, b) in enumerate(runs):
        nc = dest if len(runs) == 1 else dest.with_name(f"{dest.stem}_{i}.nc")
        ds = _fetch(conn, aoi, bands, a.astype(object), b.astype(object), nc)
        if ds is not None:
            parts.append(ds)
    if not parts:
        return None
    out = xr.concat(parts, dim="time").sortby("time") if len(parts) > 1 else parts[0]
    out.attrs = dict(parts[0].attrs)
    return out


def _fetch(conn, aoi: AOI, bands: list[str], start: dt.date, end: dt.date, dest: Path) -> xr.Dataset | None:
    """Download a period as NetCDF and read it. None when no scene matches."""
    label = f"{start}..{end} {','.join(bands)}"
    cube = _cube(conn, aoi, bands, start, end)
    if not download(cube, dest, label, (end - start).days > SYNC_MAX_DAYS,
                    f"geomon {aoi.code} Sentinel-2 {label}"):
        return None
    return _read(dest)


def nc_crs(ds: xr.Dataset) -> str:
    """The CRS of an openEO NetCDF result (EPSG code when it has one, else WKT)."""
    from pyproj import CRS

    for v in ds.variables:
        wkt = ds[v].attrs.get("crs_wkt") or ds[v].attrs.get("spatial_ref")
        if wkt:
            crs = CRS.from_wkt(wkt)
            return f"EPSG:{crs.to_epsg()}" if crs.to_epsg() else crs.to_wkt()
    raise RuntimeError("no CRS found in the openEO result")


def _complete_marker(nc: Path) -> Path:
    return nc.with_name(nc.name + ".complete")


def _remove_download(nc: Path) -> None:
    """A download, and the runs and clear-date pass of a two-pass request (``<name>_*.nc``)."""
    for f in [nc, *nc.parent.glob(f"{nc.stem}_*.nc")]:
        f.unlink(missing_ok=True)
        _complete_marker(f).unlink(missing_ok=True)
        _complete_marker(f).unlink(missing_ok=True)


def _read(path: Path) -> xr.Dataset | None:
    """openEO NetCDF -> Dataset (time, y, x) with integer bands and its CRS in attrs."""
    # Lazy (dask), TIME_BLOCK dates at a time: a few years of a large AOI do not fit in memory
    ds = xr.open_dataset(path, engine="h5netcdf", mask_and_scale=False, chunks={"t": TIME_BLOCK})
    ds = ds.rename({k: v for k, v in {"t": "time"}.items() if k in ds.dims})
    if "time" not in ds.dims or ds.sizes["time"] == 0:
        return None
    crs = nc_crs(ds)
    ds = ds.drop_vars([v for v in ds.data_vars if v not in s2.BANDS and v != "SCL"])
    out = {}
    for v in ds.data_vars:
        a = ds[v]
        if np.issubdtype(a.dtype, np.floating):
            a = a.fillna(0.0)
        out[v] = (("time", "y", "x"), a.astype("uint8" if v == "SCL" else "int16").data)
    times = pd.DatetimeIndex(ds["time"].values).normalize()
    res = xr.Dataset(out, coords={"time": times.values, "y": ds["y"].values, "x": ds["x"].values})
    res = res.sortby("time")
    res = res.isel(time=~res.get_index("time").duplicated(keep="last"))
    res.attrs["crs"] = crs
    return res


# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------
def _encoding(ds: xr.Dataset) -> dict:
    """Each date of a band in one file (a shard) of 512 x 512 chunks: a map reads the shard, a pixel's
    time series only its chunk of each date; byte-shuffled zstd (18 % smaller than plain zstd)."""
    from zarr.codecs import BloscCodec

    ny, nx = ds.sizes["y"], ds.sizes["x"]
    pad = lambda n: -(-n // CHUNK) * CHUNK          # noqa: E731
    comp = [BloscCodec(cname="zstd", clevel=5, shuffle="shuffle")]
    return {v: {"chunks": (1, CHUNK, CHUNK), "shards": (1, pad(ny), pad(nx)), "compressors": comp} for v in ds.data_vars}


def _per_date(ds: xr.Dataset) -> xr.Dataset:
    """Dask blocks matching the Zarr chunks (one date, whole grid), as Zarr requires."""
    return ds.chunk({"time": 1, "y": -1, "x": -1})


def _write_new(ds: xr.Dataset, path: Path) -> None:
    write_atomic(_per_date(ds), path, encoding=_encoding(ds))


def _fractions(ds: xr.Dataset, site_mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Valid and clear fractions of the AOI per date, TIME_BLOCK dates at a time."""
    n = site_mask.sum()
    valid, clear = [], []
    for i in range(0, ds.sizes["time"], TIME_BLOCK):
        part = ds.isel(time=slice(i, i + TIME_BLOCK))
        scl = np.asarray(part["SCL"].values)
        red = np.asarray(part["B04"].values) if "B04" in part else None
        valid.append(((scl > 0) & site_mask).sum(axis=(1, 2)) / n)
        clear.append((s2.clear_mask(scl, red) & site_mask).sum(axis=(1, 2)) / n)
    if not valid:
        return np.zeros(0), np.zeros(0)
    return np.concatenate(valid), np.concatenate(clear)


def _keep_clear(ds: xr.Dataset, site_mask: np.ndarray) -> xr.Dataset:
    valid, clear = _fractions(ds, site_mask)
    keep = clear >= settings.s2_min_clear
    out = ds.isel(time=np.nonzero(keep)[0])
    out = out.assign_coords(valid_fraction=("time", valid[keep].astype("float32")),
                            clear_fraction=("time", clear[keep].astype("float32")))
    log.info("    %d of %d dates kept (clear fraction over the AOI >= %.0f %%)",
             int(keep.sum()), len(keep), 100 * settings.s2_min_clear)
    return out




# ---------------------------------------------------------------------------
# Parts: clusters of communes more than PART_GAP_M apart get their own grid
# ---------------------------------------------------------------------------
PART_GAP_M = 5000


@dataclass(frozen=True)
class Part:
    code: str            # site code
    part: str            # "" (site in one block) or p1, p2...
    geom_wgs84: dict     # outline of the part's communes

    @property
    def label(self) -> str:
        return f"{self.code}/{self.part}" if self.part else self.code


def split_parts(aoi: AOI) -> list[Part]:
    """The site in one part, or one part per cluster of communes (by decreasing area)."""
    from pyproj import Transformer
    from shapely.geometry import mapping, shape
    from shapely.ops import transform, unary_union

    to_l93 = Transformer.from_crs("EPSG:4326", "EPSG:2154", always_xy=True).transform
    to_wgs = Transformer.from_crs("EPSG:2154", "EPSG:4326", always_xy=True).transform
    g = transform(to_l93, shape(aoi.geom_wgs84))
    polys = list(getattr(g, "geoms", [g]))
    merged = unary_union([q.buffer(PART_GAP_M / 2) for q in polys])
    clusters = list(getattr(merged, "geoms", [merged]))
    if len(clusters) == 1:
        return [Part(aoi.code, "", aoi.geom_wgs84)]
    groups = sorted((unary_union([q for q in polys if q.intersects(c)]) for c in clusters), key=lambda x: -x.area)
    return [Part(aoi.code, f"p{i + 1}", mapping(transform(to_wgs, gr))) for i, gr in enumerate(groups)]


def _raw(part: Part) -> Path:
    d = settings.raw_dir / "sentinel2" / part.code
    return d / part.part if part.part else d


def _store(part: Part, year: int) -> Path:
    return s2.store_path(part.code, year, part.part)


def _part_bounds(part: Part, crs: str) -> tuple[float, float, float, float]:
    from pyproj import Transformer
    from shapely.geometry import shape
    from shapely.ops import transform

    to = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform
    return transform(to, shape(_extent(part))).bounds


def _migrate_to_parts(site: str, parts_def: list[Part]) -> list:
    """Stores of a site kept as one grid while its communes form several parts: cropped into the
    parts' grids (no download). Returns the dates written, for the statistics."""
    years = s2.years(site, "")
    if not years or len(parts_def) < 2:
        return []
    log.info("Sentinel-2 %s: splitting the stores of %d years into %d parts", site, len(years), len(parts_def))
    written = []
    for y in years:
        src = s2.store_path(site, y, "")
        ds = xr.open_zarr(src, consolidated=True)
        attrs = dict(ds.attrs)
        for part in parts_def:
            x0, y0, x1, y1 = _part_bounds(part, attrs["crs"])
            sub = ds.sel(x=slice(x0 - 10, x1 + 10), y=slice(y1 + 10, y0 - 10))
            sub = sub.drop_vars([c for c in ("clear_fraction", "valid_fraction") if c in sub.coords])
            sub.attrs = attrs
            mask = s2.area_masks(sub, {part.code: part.geom_wgs84})[part.code]
            sub = _keep_clear(sub, mask)
            if sub.sizes["time"] == 0:
                continue
            sub.attrs = attrs | {"part": part.part}
            _write_new(sub, _store(part, y))
            written += list(sub["time"].values)
        s2.clear_cache()
        shutil.rmtree(src)
        log.info("Sentinel-2 %s %d: split into %s", site, y, ", ".join(q.part for q in parts_def))
    return written


# ---------------------------------------------------------------------------
# Statistics per area, over every part
# ---------------------------------------------------------------------------
def _store_stats(site: str, dates, areas: dict[str, dict], communes: set[str]) -> int:
    """Index statistics of the given dates over every area (pixels of all the parts that have the
    date), upserted into eo.index_stats."""
    days = sorted({np.datetime64(d, "D") for d in dates})
    if not days:
        return 0
    plist = s2.parts(site)
    zones: dict[tuple, dict] = {}

    def zones_of(part: str, ds: xr.Dataset) -> dict:
        """Pixels of every area on a part's grid (flat indices; the communes from one label image)."""
        key = (part, s2.grid_key(ds))
        if key not in zones:
            zones[key] = s2.area_zones(ds, areas, communes)
        return zones[key]

    n_area = {code: 0 for code in areas}         # pixels of each area over all the parts
    for part in plist:
        ds = s2.open_year(site, s2.years(site, part)[0], part)
        for code, z in zones_of(part, ds).items():
            n_area[code] += int(z.size)
    n = 0
    by_year: dict[int, list] = {}
    for d in days:
        by_year.setdefault(int(str(d)[:4]), []).append(d)
    for year, ds_days in by_year.items():
        stores = {part: s2.open_year(site, year, part) for part in plist}
        stores = {k: v for k, v in stores.items() if v is not None}
        times = {k: v["time"].values.astype("datetime64[D]") for k, v in stores.items()}
        rows = []
        for d in ds_days:
            pieces = []
            for part, ds in stores.items():
                hit = np.nonzero(times[part] == d)[0]
                if hit.size:
                    pieces.append((ds.isel(time=int(hit[0])).load(), zones_of(part, ds)))
            if not pieces:
                continue
            indices = sorted(set.intersection(*(set(s2.available_indices(p.data_vars)) for p, _ in pieces)))
            for r in s2.area_stats_parts(pieces, n_area, indices):
                rows.append(r | {"site": site, "date": str(d)})
        upsert_index_stats(rows)
        n += len(rows)
        log.info("Sentinel-2 %s %d: statistics of %d dates", site, year, len(ds_days))
    return n


def upsert_index_stats(rows: list[dict]) -> None:
    """Per-date statistics over the areas (Sentinel-2 indices, Sentinel-1 measures) into eo.index_stats."""
    if not rows:
        return
    with connect() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO eo.index_stats (site, area, date, index, mean, median, p10, p90, std,
                                               n_area, n_valid, n_clear, clear_fraction)
                   VALUES (%(site)s, %(area)s, %(date)s, %(index)s, %(mean)s, %(median)s, %(p10)s, %(p90)s,
                           %(std)s, %(n_area)s, %(n_valid)s, %(n_clear)s, %(clear_fraction)s)
                   ON CONFLICT (site, area, index, date) DO UPDATE SET
                       mean = EXCLUDED.mean, median = EXCLUDED.median, p10 = EXCLUDED.p10, p90 = EXCLUDED.p90,
                       std = EXCLUDED.std, n_area = EXCLUDED.n_area, n_valid = EXCLUDED.n_valid,
                       n_clear = EXCLUDED.n_clear, clear_fraction = EXCLUDED.clear_fraction""",
                rows,
            )


def _year_bounds(year: int) -> tuple[dt.date, dt.date]:
    return max(dt.date(year, 1, 1), settings.s2_start), min(dt.date(year, 12, 31), dt.date.today())


def _groups(years: list[int], size: int) -> list[list[int]]:
    """Consecutive years in groups of at most ``size`` (one openEO job each)."""
    out: list[list[int]] = []
    for y in years:
        if out and y == out[-1][-1] + 1 and len(out[-1]) < size:
            out[-1].append(y)
        else:
            out.append([y])
    return out


def _split_years(ds: xr.Dataset) -> dict[int, xr.Dataset]:
    years = pd.DatetimeIndex(ds["time"].values).year
    return {int(y): ds.isel(time=np.nonzero(years == y)[0]) for y in sorted(set(years))}


def _new_years(conn, aoi: Part, group: list[int], wanted: list[str]) -> list[dict]:
    """One job for a group of years without store (one part): split by year, keep clear dates, write."""
    start, end = _year_bounds(group[0])[0], _year_bounds(group[-1])[1]
    label = f"{group[0]}" if len(group) == 1 else f"{group[0]}-{group[-1]}"
    log.info("Sentinel-2 %s %s: %s to %s, bands %s (one job)", aoi.label, label, start, end, ",".join(wanted))
    nc = _raw(aoi) / f"{label}.nc"
    new = _fetch_clear(conn, aoi, wanted, start, end, nc)
    out = []
    by_year = _split_years(new) if new is not None else {}
    mask = s2.area_masks(new, {aoi.code: aoi.geom_wgs84})[aoi.code] if new is not None else None
    for year in group:
        y_start, y_end = _year_bounds(year)
        part = by_year.get(year)
        if part is not None:
            part = _keep_clear(part, mask)
        if part is None or part.sizes["time"] == 0:
            log.info("Sentinel-2 %s %d: no usable scene", aoi.label, year)
            out.append({"year": year, "dates": 0, "added_dates": 0, "added_bands": [], "stat_dates": []})
            continue
        part.attrs = dict(new.attrs) | {
            "site": aoi.code, "part": aoi.part, "collection": s2.COLLECTION, "backend": settings.openeo_url,
            "requested_from": str(y_start), "requested_until": str(y_end),
            "max_cloud_cover": settings.s2_max_cloud, "min_clear_fraction": settings.s2_min_clear,
            "scale": s2.SCALE, "clear_classes": list(s2.CLEAR_CLASSES), "max_clear_red": s2.MAX_CLEAR_RED,
            "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
        _write_new(part, _store(aoi, year))
        s2.clear_cache()
        n = int(part.sizes["time"])
        out.append({"year": year, "dates": n, "added_dates": n, "added_bands": [],
                    "stat_dates": list(part["time"].values)})
    _remove_download(nc)
    return out


def _add_bands(conn, aoi: Part, group: list[int], missing: list[str]) -> list[dict]:
    """One job fetching bands added to the configuration for the dates already stored (one part)."""
    stores = {y: xr.open_zarr(_store(aoi, y), consolidated=True) for y in group}
    attrs = {y: read_attrs(_store(aoi, y)) for y in group}
    start = min(dt.date.fromisoformat(a["requested_from"]) for a in attrs.values())
    end = max(dt.date.fromisoformat(a["requested_until"]) for a in attrs.values())
    label = f"{group[0]}" if len(group) == 1 else f"{group[0]}-{group[-1]}"
    log.info("Sentinel-2 %s %s: adding band(s) %s (one job)", aoi.label, label, ",".join(missing))
    nc = _raw(aoi) / f"{label}_{'_'.join(missing)}.nc"
    new = _fetch(conn, aoi, missing, start, end, nc)
    out = []
    for y in group:
        ds = stores[y]
        path = _store(aoi, y)
        if new is not None:
            part = new.reindex(time=ds["time"].values, fill_value=0)
            part = part.reindex(y=ds["y"].values, x=ds["x"].values, method="nearest", tolerance=1.0, fill_value=0)
            part = part.drop_vars([c for c in part.coords if c not in ("time", "y", "x")])
            part.attrs = {}
            _per_date(part).to_zarr(path, mode="a", consolidated=True, encoding=_encoding(part))
        s2.clear_cache()
        ds = xr.open_zarr(path, consolidated=True)
        out.append({"year": y, "dates": int(ds.sizes["time"]), "added_dates": 0, "added_bands": missing,
                    "stat_dates": list(ds["time"].values)})          # new indices for every date
    _remove_download(nc)
    return out


def _update(conn, aoi: Part, year: int) -> dict:
    """Days published since the last run of a year (one part; synchronous request when short)."""
    path = _store(aoi, year)
    attrs = read_attrs(path)
    ds = xr.open_zarr(path, consolidated=True)
    req_from = dt.date.fromisoformat(attrs["requested_from"])
    req_until = dt.date.fromisoformat(attrs["requested_until"])
    y_end = _year_bounds(year)[1]
    summary = {"year": year, "added_dates": 0, "added_bands": [], "stat_dates": []}
    if req_until >= y_end:
        return summary | {"dates": int(ds.sizes["time"])}
    start = max(req_from, req_until - dt.timedelta(days=OVERLAP_DAYS))
    bands = [b for b in s2.BANDS if b in ds.data_vars] + ["SCL"]
    nc = _raw(aoi) / f"{year}_update.nc"
    new = _fetch_clear(conn, aoi, bands, start, y_end, nc, have=ds["time"].values)
    added = []
    if new is not None:
        have = set(ds["time"].values.astype("datetime64[D]"))
        fresh = [i for i, t in enumerate(new["time"].values.astype("datetime64[D]")) if t not in have]
        new = new.isel(time=fresh)
        if new.sizes["time"]:
            new = new.reindex(y=ds["y"].values, x=ds["x"].values, method="nearest", tolerance=1.0, fill_value=0)
            mask = s2.area_masks(new.assign_attrs(crs=ds.attrs["crs"]), {aoi.code: aoi.geom_wgs84})[aoi.code]
            new = _keep_clear(new, mask)
        if new.sizes["time"]:
            added = list(new["time"].values)
            if new["time"].values.min() > ds["time"].values.max():
                new.attrs = dict(ds.attrs)       # an append writes the store's attributes: keep them
                _per_date(new).to_zarr(path, append_dim="time", consolidated=True)
            else:   # a late scene falls between stored dates: rewrite the year
                merged = xr.concat([ds, new], dim="time").sortby("time")   # lazy: written to a temporary store first
                merged.attrs = dict(ds.attrs)
                _write_new(merged, path)
    _remove_download(nc)
    update_attrs(path, requested_until=str(y_end),
               updated=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    s2.clear_cache()
    ds = xr.open_zarr(path, consolidated=True)
    return summary | {"added_dates": len(added), "dates": int(ds.sizes["time"]), "stat_dates": added}


def recompute(aoi: AOI) -> dict:
    """Recompute the clear fractions and every index statistic from the stored images (no download).

    Run it after a change of the cloud mask or of the index list.
    """
    import zarr

    areas, communes = site_areas(aoi.code)
    parts_def = split_parts(aoi)
    _migrate_to_parts(aoi.code, parts_def)
    with connect() as db:
        db.execute("DELETE FROM eo.index_stats WHERE site = %s", (aoi.code,))
    dates = []
    for part in parts_def:
        for y in s2.years(aoi.code, part.part):
            path = _store(part, y)
            ds = xr.open_zarr(path, consolidated=True)
            mask = s2.area_masks(ds, {aoi.code: part.geom_wgs84})[aoi.code]
            valid, clear = _fractions(ds, mask)
            g = zarr.open_group(str(path), mode="a")
            g["clear_fraction"][:] = clear.astype("float32")
            g["valid_fraction"][:] = valid.astype("float32")
            update_attrs(path, clear_classes=list(s2.CLEAR_CLASSES), max_clear_red=s2.MAX_CLEAR_RED)
            dates += list(ds["time"].values)
    s2.clear_cache()
    n = _store_stats(aoi.code, dates, areas, communes)
    return {"years": len(s2.years(aoi.code)), "parts": [q.part for q in parts_def], "rows": n,
            "parcels": parcels.sync(aoi.code, full=True)}


def reencode_stores(paths, min_clear: float | None) -> dict:
    """Stores rewritten with the current encoding (_encoding); with ``min_clear``, the dates whose
    clear fraction over the site is below it are left out. Returns the dates removed."""
    removed, before, after = [], 0, 0

    def size(p):
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())

    for path in paths:
        ds = xr.open_zarr(path, consolidated=True)
        before += size(path)
        if min_clear is not None and "clear_fraction" in ds.coords:
            low = ds["clear_fraction"].values < min_clear
            removed += list(ds["time"].values[low])
            ds = ds.isel(time=np.nonzero(~low)[0])
        attrs = dict(ds.attrs) | ({"min_clear_fraction": min_clear} if min_clear is not None else {})
        for v in ds.variables.values():
            v.encoding = {}
        if ds.sizes["time"]:
            ds.attrs = attrs
            _write_new(ds, path)
        else:
            shutil.rmtree(path)
        after += size(path) if path.exists() else 0
        s2.clear_cache()
    return {"removed": removed, "mb_before": round(before / 1e6, 1), "mb_after": round(after / 1e6, 1)}


def reencode(aoi: AOI) -> dict:
    """Step ``sentinel2-reencode``: the stored images rewritten with the current encoding, without
    the dates below GEOMON_S2_MIN_CLEAR (no download); their statistics removed (the parcel stores of
    geomon.fields, eo.index_stats) and the curves recomputed."""
    paths = sorted(s2.site_dir(aoi.code).glob("**/*.zarr")) if s2.site_dir(aoi.code).exists() else []
    r = reencode_stores(paths, settings.s2_min_clear)
    days = sorted({str(np.datetime64(d, "D")) for d in r["removed"]})
    if days:
        gone = set(days)
        for name in fields.layers(aoi.code, parcels.PRODUCT):
            fields.keep_dates(aoi.code, parcels.PRODUCT, name, lambda d: str(d) not in gone)
        with connect() as conn:
            conn.execute("DELETE FROM eo.index_stats WHERE site = %s AND index = ANY(%s) AND date = ANY(%s::date[])",
                         (aoi.code, list(s2.INDICES), days))
        seasons = sorted({x for d in days for x in parcels.seasons_of(dt.date.fromisoformat(d))})
        parcels.compute_seasons(aoi.code, set(seasons))
        parcels.compute_groups(aoi.code, set(seasons))
    return {"stores": len(paths), "dates_removed": len(days), "mb_before": r["mb_before"], "mb_after": r["mb_after"]}


def recompute_groups(aoi: AOI) -> dict:
    """Recomputes the crop-group curves from the stored parcel curves (after a change of the RPG crops;
    the farm step does it itself when the farm's land cover changed; a minute)."""
    return {"group_curves": parcels.compute_groups(aoi.code)}


def recompute_parcels(aoi: AOI) -> dict:
    """Recompute the parcel statistics and curves from the stored images (no download)."""
    return parcels.sync(aoi.code, full=True)


def _has_login() -> bool:
    from openeo.rest.auth.config import RefreshTokenStore

    return bool(os.environ.get("OPENEO_AUTH_METHOD")) or RefreshTokenStore().path.exists()


def _parallel(tasks: list[tuple], what: str) -> tuple[list[dict], list[str]]:
    results, errors = [], []
    with ThreadPoolExecutor(max_workers=max(1, settings.s2_parallel_jobs)) as pool:
        futures = {pool.submit(fn, *args): label for fn, args, label in tasks}
        for fut in as_completed(futures):
            label = futures[fut]
            try:
                r = fut.result()
                results.extend(r if isinstance(r, list) else [r])
            except Exception as exc:
                log.error("%s %s failed: %s", what, label, exc)
                errors.append(f"{what} {label}: {exc}")
    return results, errors


def run(aoi: AOI) -> dict:
    wanted = s2.wanted_bands()
    if not _has_login():
        log.warning("No openEO login yet: Sentinel-2 skipped. Run once: docker compose run --rm ingest sentinel2-login")
        return {"skipped": "no openEO login (run the sentinel2-login step)"}
    conn = _connection()
    areas, communes = site_areas(aoi.code)
    parts_def = split_parts(aoi)
    if len(parts_def) > 1:
        log.info("Sentinel-2 %s: %d parts (%s)", aoi.code, len(parts_def),
                 ", ".join(f"{q.part}" for q in parts_def))
    migrated = _migrate_to_parts(aoi.code, parts_def)
    years = list(range(settings.s2_start.year, dt.date.today().year + 1))
    size = max(1, settings.s2_years_per_job)
    results, errors = [], []
    stored = {q.part: set(s2.years(aoi.code, q.part)) for q in parts_def}

    # 1. History: years without store, a few years per job, for each part
    tasks = [(_new_years, (conn, q, g, wanted), f"{q.label} {g[0]}-{g[-1]}")
             for q in parts_def for g in _groups([y for y in years if y not in stored[q.part]], size)]
    r, e = _parallel(tasks, "Sentinel-2 history")
    results += r
    errors += e

    # 2. Bands added to the configuration, for the years already stored (grouped by missing bands)
    tasks = []
    for q in parts_def:
        by_missing: dict[tuple, list[int]] = {}
        for y in sorted(stored[q.part]):
            have = set(xr.open_zarr(_store(q, y), consolidated=True).data_vars)
            missing = tuple(b for b in wanted if b not in have)
            if missing:
                by_missing.setdefault(missing, []).append(y)
        tasks += [(_add_bands, (conn, q, g, list(m)), f"{q.label} {g[0]}-{g[-1]} {','.join(m)}")
                  for m, ys in by_missing.items() for g in _groups(ys, size)]
    r, e = _parallel(tasks, "Sentinel-2 bands")
    results += r
    errors += e

    # 3. Days published since the last run (in practice the current year)
    tasks = [(_update, (conn, q, y), f"{q.label} {y}") for q in parts_def for y in sorted(stored[q.part])
             if dt.date.fromisoformat(read_attrs(_store(q, y))["requested_until"]) < _year_bounds(y)[1]]
    r, e = _parallel(tasks, "Sentinel-2 update")
    results += r
    errors += e

    if errors and not results and not migrated:
        raise RuntimeError(errors[0])
    # 4. Statistics per area of the new dates, over all the parts
    _store_stats(aoi.code, migrated + [d for r in results for d in r.get("stat_dates", [])], areas, communes)
    # 5. RPG parcel series of the new dates (and of seasons whose RPG changed)
    try:
        parcel_summary = parcels.sync(aoi.code)
    except Exception as exc:
        log.error("Sentinel-2 parcel series failed: %s", exc)
        errors.append(f"parcels: {exc}")
        parcel_summary = {}
    return {"bands": wanted, "parts": [q.part for q in parts_def if q.part],
            "years": len(s2.years(aoi.code)),
            "dates": sum(int(xr.open_zarr(s2.store_path(aoi.code, y, q), consolidated=True).sizes["time"])
                         for q in s2.parts(aoi.code) for y in s2.years(aoi.code, q)),
            "added_dates": sum(r["added_dates"] for r in results),
            "added_bands": sorted({b for r in results for b in r["added_bands"]}), "parcels": parcel_summary,
            "errors": errors}
