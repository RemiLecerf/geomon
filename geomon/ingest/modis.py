"""MODIS NDVI 250 m from the Microsoft Planetary Computer (see geomon.modis).

No openEO back-end serves MODIS, so the transfer is kept small on the client side instead:

- the STAC search asks only for the fields used (id, first day, tile, two asset links): about
  27 KB per site and year instead of 460 KB;
- each GeoTIFF is a Cloud-Optimised GeoTIFF read by HTTP range requests: its header, then the
  internal block of 512 x 512 cells (about 120 km) holding the site, never the whole 4800 x 4800
  tile (37 MB for NDVI). About 0.45 MB of NDVI and 0.03 MB of pixel reliability per composite
  for a site within one block;
- only NDVI and pixel reliability are read (not EVI, the reflectances or the day of year).

The record since GEOMON_MODIS_START (2000-02-18: the first Terra composite) takes about 0.6 GB
of transfer per block of 120 km and a few minutes; afterwards a run reads the composites published
since (about 1 MB a week). Composites are published up to four weeks after their first day: a year
is searched again until 45 days after its end.

Files are read through short-lived shared access tokens of the Planetary Computer (no account),
renewed before they expire.
"""

from __future__ import annotations

import collections
import datetime as dt
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import numpy as np
import xarray as xr

from .. import modis, modis_crops
from .. import sentinel2 as s2
from ..aoi import AOI, site_areas
from ..config import settings
from ..db import connect
from ..http import session
from ..zarrio import read_attrs, update_attrs, write_atomic
from . import sentinel2 as ing2

log = logging.getLogger(__name__)

PC_STAC = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
PC_TOKEN = "https://planetarycomputer.microsoft.com/api/sas/v1/token/{account}/{container}"
PUBLISH_LAG_DAYS = 45
GAP_YEARS = 3                     # composites missing from the catalogue are searched again over these years
# First composite of each platform (Terra, Aqua) and first day of year of its 16-day cycle
FIRST_COMPOSITE = {0: (dt.date(2000, 2, 18), 1), 1: (dt.date(2002, 7, 4), 9)}
READERS = 8                       # files read at once
TIME_CHUNK = 8                    # composites per Zarr chunk

# Range reads only: no directory listing (sidecar files), consecutive ranges merged, retries
GDAL_ENV = dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
                GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES", GDAL_HTTP_MAX_RETRY="4", GDAL_HTTP_RETRY_DELAY="3",
                VSI_CACHE="FALSE")


# ---------------------------------------------------------------------------
# Planetary Computer: catalogue and tokens
# ---------------------------------------------------------------------------
class _Tokens:
    """Shared access tokens per storage container, renewed 10 minutes before they expire."""

    def __init__(self):
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str], tuple[str, dt.datetime]] = {}

    def sign(self, href: str) -> str:
        u = urlparse(href)
        key = (u.netloc.split(".")[0], u.path.split("/")[1])
        with self._lock:
            token, expiry = self._cache.get(key, (None, None))
            if token is None or expiry - dt.datetime.now(dt.timezone.utc) < dt.timedelta(minutes=10):
                r = session().get(PC_TOKEN.format(account=key[0], container=key[1]), timeout=60)
                r.raise_for_status()
                d = r.json()
                token = d["token"]
                expiry = dt.datetime.fromisoformat(d["msft:expiry"].replace("Z", "+00:00"))
                self._cache[key] = (token, expiry)
        return f"{href}?{token}"


_TOKENS = _Tokens()


def search(bbox: list[float], start: dt.date, end: dt.date) -> list[dict]:
    """The composites whose 16 days overlap [start, end] over ``bbox`` (WGS84), as
    {date, platform, h, v, ndvi, reliability} (hrefs not signed)."""
    body = {"collections": [modis.COLLECTION], "bbox": bbox, "limit": 1000,
            "datetime": f"{start}T00:00:00Z/{end}T23:59:59Z",
            "fields": {"include": ["id", "properties.start_datetime", "properties.modis:horizontal-tile",
                                   "properties.modis:vertical-tile", f"assets.{modis.NDVI_ASSET}.href",
                                   f"assets.{modis.RELIABILITY_ASSET}.href"],
                       "exclude": ["links", "geometry", "bbox"]}}
    url, method, out = PC_STAC, "POST", []
    for _ in range(100):
        r = session().post(url, json=body, timeout=120) if method == "POST" else session().get(url, timeout=120)
        r.raise_for_status()
        page = r.json()
        for f in page.get("features", []):
            p = f["properties"]
            out.append({"date": dt.date.fromisoformat(p["start_datetime"][:10]),
                        "platform": modis.PLATFORMS[f["id"][:3]],
                        "h": int(p["modis:horizontal-tile"]), "v": int(p["modis:vertical-tile"]),
                        "ndvi": f["assets"][modis.NDVI_ASSET]["href"],
                        "reliability": f["assets"][modis.RELIABILITY_ASSET]["href"]})
        nxt = next((link for link in page.get("links", []) if link.get("rel") == "next"), None)
        if not nxt:
            break
        url, method = nxt["href"], nxt.get("method", "GET").upper()
        if nxt.get("body"):
            body = body | nxt["body"] if nxt.get("merge") else nxt["body"]
    return out


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
class Grid:
    """The site's window of the global MODIS grid, and the cells within its outline (plus margin)."""

    def __init__(self, aoi: AOI):
        from pyproj import Transformer
        from rasterio.features import geometry_mask
        from rasterio.transform import from_origin
        from shapely.geometry import mapping, shape
        from shapely.ops import transform

        extent = shape(ing2._extent(aoi))
        self.bbox = [round(v, 5) for v in extent.bounds]
        to_sinu = Transformer.from_crs("EPSG:4326", modis.CRS, always_xy=True).transform
        g = transform(to_sinu, extent.segmentize(0.005))         # the sinusoidal grid curves meridians
        self.window = modis.window_of(g.bounds)
        self.x, self.y = modis.cell_centres(self.window)
        aff = from_origin(self.x[0] - modis.PIXEL / 2, self.y[0] + modis.PIXEL / 2, modis.PIXEL, modis.PIXEL)
        self.inside = geometry_mask([mapping(g)], out_shape=(len(self.y), len(self.x)), transform=aff,
                                    invert=True, all_touched=True)

    @property
    def shape(self) -> tuple[int, int]:
        return len(self.y), len(self.x)


def _read_window(href: str, h: int, win: tuple[int, int, int, int]) -> np.ndarray:
    import rasterio
    from rasterio.windows import Window

    row_off, col_off, height, width = win
    with rasterio.Env(**GDAL_ENV), rasterio.open(_TOKENS.sign(href)) as src:
        west = modis.X0 + h * modis.TILE * modis.PIXEL
        if abs(src.transform.c - west) > 1 or abs(src.transform.a - modis.PIXEL) > 1e-6:
            raise ValueError(f"unexpected grid in {href.rsplit('/', 1)[-1]}: {src.transform}")
        return src.read(1, window=Window(col_off, row_off, width, height))


def _read_composite(grid: Grid, tiles: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    """NDVI and reliability of one composite over the site (its tiles merged)."""
    ndvi = np.full(grid.shape, modis.NDVI_NODATA, dtype="int16")
    rel = np.full(grid.shape, modis.REL_NODATA, dtype="int8")
    for t in tiles:
        sl = modis.tile_slices(grid.window, t["h"], t["v"])
        if sl is None:
            continue
        win, (rows, cols) = sl
        ndvi[rows, cols] = _read_window(t["ndvi"], t["h"], win)
        rel[rows, cols] = _read_window(t["reliability"], t["h"], win)
    ndvi[~grid.inside] = modis.NDVI_NODATA
    rel[~grid.inside] = modis.REL_NODATA
    return ndvi, rel


def _composites(items: list[dict]) -> dict[tuple[dt.date, int], list[dict]]:
    by_key: dict[tuple[dt.date, int], list[dict]] = collections.defaultdict(list)
    for it in items:
        by_key[(it["date"], it["platform"])].append(it)
    return dict(sorted(by_key.items()))


def _dataset(grid: Grid, keys: list[tuple[dt.date, int]], arrays: list[tuple[np.ndarray, np.ndarray]]) -> xr.Dataset:
    return xr.Dataset(
        {"ndvi": (("time", "y", "x"), np.stack([a for a, _ in arrays])),
         "reliability": (("time", "y", "x"), np.stack([b for _, b in arrays]))},
        coords={"time": np.array([np.datetime64(d, "ns") for d, _ in keys]),
                "platform": ("time", np.array([p for _, p in keys], dtype="int8")),
                "y": grid.y, "x": grid.x})


def _write(ds: xr.Dataset, path, attrs: dict) -> None:
    ds = ds.sortby("time")
    ds.attrs = attrs
    ds["ndvi"].attrs = {"scale_factor_note": "NDVI x 10000", "nodata": modis.NDVI_NODATA}
    ds["reliability"].attrs = {"flag_values": "-1 0 1 2 3",
                               "flag_meanings": "no_data good marginal snow_or_ice cloudy"}
    ds["platform"].attrs = {"flag_values": "0 1", "flag_meanings": "terra aqua"}
    ny, nx = ds.sizes["y"], ds.sizes["x"]
    chunks = {"time": TIME_CHUNK, "y": -1, "x": -1}
    write_atomic(ds.chunk(chunks), path,
                 encoding={v: {"chunks": (TIME_CHUNK, ny, nx)} for v in ("ndvi", "reliability")})


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------
def expected(year: int, today: dt.date) -> set[tuple[dt.date, int]]:
    """The composites (first day, platform) of a year that should be published by ``today``: each
    platform's 16-day cycle restarts on 1 January (Terra on day 1, Aqua on day 9)."""
    out = set()
    for platform, (first, doy0) in FIRST_COMPOSITE.items():
        for doy in range(doy0, 367, 16):
            d = dt.date(year, 1, 1) + dt.timedelta(days=doy - 1)
            if (d.year == year and d >= max(first, settings.modis_start)
                    and d + dt.timedelta(days=16 + PUBLISH_LAG_DAYS) <= today):
                out.add((d, platform))
    return out


def _year_task(grid: Grid, site: str, year: int, today: dt.date) -> dict | None:
    """What to search for a year: None when complete; else its search window and stored dataset."""
    y0, y1 = max(dt.date(year, 1, 1), settings.modis_start), min(dt.date(year, 12, 31), today)
    path = modis.store_path(site, year)
    if not (path / "zarr.json").exists():
        return {"year": year, "start": y0, "end": y1, "stored": None}
    attrs = read_attrs(path)
    if attrs.get("window") != list(grid.window):
        log.info("MODIS %s %d: the site's extent changed, year fetched again", site, year)
        return {"year": year, "start": y0, "end": y1, "stored": None}
    searched = dt.date.fromisoformat(attrs["searched_until"])
    if searched >= dt.date(year, 12, 31) + dt.timedelta(days=PUBLISH_LAG_DAYS):
        # A complete year: only the composites missing from the catalogue, in the last years (a
        # catalogue can fill a gap later: July to mid-November 2025 was missing in November 2026)
        if year <= today.year - GAP_YEARS:
            return None
        ds = modis.open_year(site, year)
        have = {(np.datetime64(t, "D").item(), int(p)) for t, p in zip(ds["time"].values, ds["platform"].values)}
        missing = sorted(expected(year, today) - have)
        if not missing:
            return None
        return {"year": year, "start": missing[0][0], "stored": path, "gaps": len(missing),
                "end": min(y1, missing[-1][0] + dt.timedelta(days=15))}
    start = max(y0, searched - dt.timedelta(days=PUBLISH_LAG_DAYS))
    return {"year": year, "start": start, "end": y1, "stored": path}


def _fetch_year(grid: Grid, site: str, task: dict, today: dt.date, pool: ThreadPoolExecutor) -> dict:
    year, path = task["year"], task["stored"]
    items = [it for it in search(grid.bbox, task["start"], task["end"])
             if it["date"].year == year and it["date"] >= settings.modis_start]
    old = xr.open_zarr(path, consolidated=True).load() if path else None
    have = set() if old is None else {(np.datetime64(t, "D").item(), int(p))
                                      for t, p in zip(old["time"].values, old["platform"].values)}
    todo = {k: v for k, v in _composites(items).items() if k not in have}
    futures = {k: pool.submit(_read_composite, grid, tiles) for k, tiles in todo.items()}
    keys, arrays, failed = [], [], []
    for k, fut in futures.items():
        try:
            arrays.append(fut.result())
            keys.append(k)
        except Exception as exc:
            log.error("MODIS %s %s %s failed: %s", site, modis.PLATFORM_NAMES[k[1]], k[0], exc)
            failed.append(k[0])
    # searched again from the first failure on the next run
    searched = min(failed) - dt.timedelta(days=1) if failed else today
    if keys:
        new = _dataset(grid, keys, arrays)
        ds = new if old is None else xr.concat([old, new], dim="time")
        attrs = {"site": site, "collection": modis.COLLECTION, "source": "Microsoft Planetary Computer",
                 "crs": modis.CRS, "window": list(grid.window), "scale": modis.SCALE,
                 "searched_until": str(searched),
                 "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
        _write(ds, modis.store_path(site, year), attrs)
        modis.clear_cache()
    elif path is not None:
        update_attrs(path, searched_until=str(searched),
                        updated=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
        modis.clear_cache()
    if keys or failed:
        log.info("MODIS %s %d: %d composites added%s", site, year, len(keys),
                 f", {len(failed)} failed" if failed else "")
    return {"year": year, "added": [k[0] for k in keys], "failed": [str(d) for d in failed]}


def _store_stats(site: str, dates, areas: dict[str, dict], communes: set[str]) -> int:
    """Statistics of the given composites over every area, upserted into eo.index_stats."""
    days = sorted({np.datetime64(d, "D") for d in dates})
    by_year: dict[int, list] = collections.defaultdict(list)
    for d in days:
        by_year[int(str(d)[:4])].append(d)
    zones, n = None, 0
    for year, ds_days in sorted(by_year.items()):
        ds = modis.open_year(site, year)
        if ds is None:
            continue
        if zones is None:
            zones = s2.area_zones(ds, areas, communes)
        times = ds["time"].values.astype("datetime64[D]")
        loaded = ds.load()
        rows = []
        for d in ds_days:
            hit = np.nonzero(times == d)[0]
            if hit.size:
                rows += [r | {"site": site, "date": str(d)} for r in modis.area_rows(loaded.isel(time=int(hit[0])), zones)]
        ing2.upsert_index_stats(rows)
        n += len(rows)
    if n:
        log.info("MODIS %s: statistics of %d composites", site, len(days))
    return n


def run(aoi: AOI) -> dict:
    grid = Grid(aoi)
    today = dt.date.today()
    tasks = [t for y in range(settings.modis_start.year, today.year + 1)
             if (t := _year_task(grid, aoi.code, y, today))]
    log.info("MODIS %s: grid of %d x %d cells, tiles %s; %d years to search", aoi.code, grid.shape[1],
             grid.shape[0], ", ".join(f"h{h:02d}v{v:02d}" for h, v in modis.tiles_of(grid.window)), len(tasks))
    results, errors = [], []
    with ThreadPoolExecutor(max_workers=READERS) as pool:
        for t in tasks:
            try:
                r = _fetch_year(grid, aoi.code, t, today, pool)
                results.append(r)
                errors += [f"MODIS {t['year']}: composite {d} failed" for d in r["failed"]]
            except Exception as exc:
                log.error("MODIS %s %d failed: %s", aoi.code, t["year"], exc)
                errors.append(f"MODIS {t['year']}: {exc}")
    added = [d for r in results for d in r["added"]]
    if errors and not added:
        raise RuntimeError(errors[0])
    rows = _store_stats(aoi.code, added, *site_areas(aoi.code)) if added else 0
    try:
        crops = modis_crops.sync(aoi.code, added)
    except Exception as exc:
        log.error("MODIS %s crop-group curves failed: %s", aoi.code, exc)
        errors.append(f"crop groups: {exc}")
        crops = {}
    return {"crop_groups": crops, "start": str(settings.modis_start), "grid": list(grid.shape),
            "tiles": [f"h{h:02d}v{v:02d}" for h, v in modis.tiles_of(grid.window)],
            "added_composites": len(added),
            "composites": sum(int(modis.open_year(aoi.code, y).sizes["time"]) for y in modis.years(aoi.code)),
            "stat_rows": rows, "errors": errors}


def recompute_crops(aoi: AOI) -> dict:
    """Recomputes the crop-group curves of every season, and the crop shares of the cells (after an
    RPG reload or a change of GEOMON_MODIS_PURITY)."""
    return modis_crops.compute(aoi.code, rebuild_purity=True)


def recompute(aoi: AOI) -> dict:
    """Recomputes every MODIS statistic from the stored composites (no download)."""
    areas, communes = site_areas(aoi.code)
    with connect() as conn:
        conn.execute("DELETE FROM eo.index_stats WHERE site = %s AND index = %s", (aoi.code, modis.STAT_NAME))
    dates = [t for y in modis.years(aoi.code) for t in modis.open_year(aoi.code, y)["time"].values]
    return {"rows": _store_stats(aoi.code, dates, areas, communes)}
