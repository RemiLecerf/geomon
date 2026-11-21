"""MODIS NDVI at 250 m (MOD13Q1 / MYD13Q1, collection 6.1): storage, values, area statistics.

Source: the 16-day vegetation index composites of Terra (MOD13Q1, since 2000-02-18) and Aqua
(MYD13Q1, since 2002-07-04), as Cloud-Optimised GeoTIFFs on the Microsoft Planetary Computer
(STAC collection modis-13Q1-061). The two satellites' composites are 8 days apart, which gives a
composite every 8 days.

Only the site's pixels cross the network: the GeoTIFFs are read by HTTP range requests, one
internal block of 512 x 512 pixels (about 120 km) per file, about 0.5 MB per composite for NDVI and
pixel reliability. The composite day-of-year band is not read.

Storage: one Zarr store per year, ``data/zarr/modis/<site>/<year>.zarr``, on the MODIS
sinusoidal grid (231.66 m cells, snapped to the global grid, tiles merged when a site straddles
two), cut to the rectangle around the site. Variables ``ndvi`` (int16, x 0.0001, -3000 = no data)
and ``reliability`` (int8: 0 good, 1 marginal, 2 snow or ice, 3 cloudy, -1 no data), outside the
site's outline (plus margin) set to no data. Each composite is dated by its first day (the date
in the product name, A<year><day>) and carries its platform (0 Terra, 1 Aqua).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np
import xarray as xr

from .config import settings

COLLECTION = "modis-13Q1-061"
NDVI_ASSET = "250m_16_days_NDVI"
RELIABILITY_ASSET = "250m_16_days_pixel_reliability"
STAT_NAME = "modis_ndvi"          # in eo.index_stats

# The MODIS sinusoidal grid (spherical earth), tiles of 4800 x 4800 cells
CRS = "+proj=sinu +lon_0=0 +x_0=0 +y_0=0 +R=6371007.181 +units=m +no_defs"
PIXEL = 231.65635826395834
X0 = -20015109.355798             # west edge of tile h00
Y0 = 10007554.677899              # north edge of tile v00
TILE = 4800

SCALE = 0.0001
NDVI_NODATA = -3000
REL_NODATA = -1
CLEAR = (0, 1)                    # reliability: good, marginal
PLATFORMS = {"MOD": 0, "MYD": 1}  # product prefix -> platform code
PLATFORM_NAMES = {0: "terra", 1: "aqua"}

VMIN, VMAX, COLORMAP = -0.1, 0.9, "RdYlGn"     # as Sentinel-2 NDVI


# ---------------------------------------------------------------------------
# Grid
# ---------------------------------------------------------------------------
def window_of(bounds: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
    """Global cell window (row0, row1, col0, col1), end exclusive, covering sinusoidal bounds."""
    x0, y0, x1, y1 = bounds
    return (int(np.floor((Y0 - y1) / PIXEL)), int(np.ceil((Y0 - y0) / PIXEL)),
            int(np.floor((x0 - X0) / PIXEL)), int(np.ceil((x1 - X0) / PIXEL)))


def tiles_of(window: tuple[int, int, int, int]) -> list[tuple[int, int]]:
    """(h, v) of the tiles a global window touches."""
    r0, r1, c0, c1 = window
    return [(h, v) for v in range(r0 // TILE, (r1 - 1) // TILE + 1) for h in range(c0 // TILE, (c1 - 1) // TILE + 1)]


def tile_slices(window: tuple[int, int, int, int], h: int, v: int):
    """For one tile: the window to read in the tile ((row_off, col_off, height, width)) and where it
    goes in the site array (row slice, col slice); None when the tile misses the window."""
    r0, r1, c0, c1 = window
    tr0, tc0 = v * TILE, h * TILE
    a0, a1 = max(r0, tr0), min(r1, tr0 + TILE)
    b0, b1 = max(c0, tc0), min(c1, tc0 + TILE)
    if a0 >= a1 or b0 >= b1:
        return None
    return (a0 - tr0, b0 - tc0, a1 - a0, b1 - b0), (slice(a0 - r0, a1 - r0), slice(b0 - c0, b1 - c0))


def cell_centres(window: tuple[int, int, int, int]) -> tuple[np.ndarray, np.ndarray]:
    """x (west to east) and y (north to south) of the cell centres of a global window."""
    r0, r1, c0, c1 = window
    return X0 + (np.arange(c0, c1) + 0.5) * PIXEL, Y0 - (np.arange(r0, r1) + 0.5) * PIXEL


# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------
def site_dir(site: str) -> Path:
    return settings.zarr_dir / "modis" / site


def store_path(site: str, year: int) -> Path:
    return site_dir(site) / f"{year}.zarr"


def years(site: str) -> list[int]:
    d = site_dir(site)
    return sorted(int(p.stem) for p in d.glob("*.zarr") if p.stem.isdigit() and (p / "zarr.json").exists()) \
        if d.exists() else []


@lru_cache(maxsize=64)
def _open(path: str, mtime: float) -> xr.Dataset:
    return xr.open_zarr(path, consolidated=True)


def open_year(site: str, year: int) -> xr.Dataset | None:
    p = store_path(site, year)
    z = p / "zarr.json"
    return _open(str(p), z.stat().st_mtime) if z.exists() else None


def clear_cache() -> None:
    _open.cache_clear()


def scene(site: str, date: str) -> xr.Dataset:
    """One composite, loaded (KeyError when absent)."""
    ds = open_year(site, int(date[:4]))
    if ds is None:
        raise KeyError(date)
    hits = np.nonzero(ds["time"].values.astype("datetime64[D]") == np.datetime64(date[:10]))[0]
    if not hits.size:
        raise KeyError(date)
    return ds.isel(time=int(hits[0])).load()


# ---------------------------------------------------------------------------
# Values and statistics
# ---------------------------------------------------------------------------
def ndvi_array(ds_date: xr.Dataset, masked: bool = True) -> np.ndarray:
    """NDVI of one composite (2-D float32), NaN without data and, when ``masked``, where the pixel
    is not reliable (snow, ice, cloud)."""
    raw = ds_date["ndvi"].values
    out = raw.astype("float32") * SCALE
    bad = raw == NDVI_NODATA
    if masked:
        bad |= ~np.isin(ds_date["reliability"].values, CLEAR)
    out[bad] = np.nan
    return out


def area_rows(ds_date: xr.Dataset, zones: dict[str, np.ndarray]) -> list[dict]:
    """NDVI statistics of one composite over every area, over its reliable cells (rows of
    eo.index_stats)."""
    raw = ds_date["ndvi"].values.ravel()
    rel = ds_date["reliability"].values.ravel()
    valid = raw != NDVI_NODATA
    clear = valid & np.isin(rel, CLEAR)
    values = raw.astype("float32") * SCALE
    rows = []
    for code, idx in zones.items():
        total = int(idx.size)
        v = values[idx][clear[idx]]
        row = {"area": code, "index": STAT_NAME, "n_area": total, "n_valid": int(valid[idx].sum()),
               "n_clear": int(v.size), "clear_fraction": v.size / total if total else 0.0,
               "mean": None, "median": None, "p10": None, "p90": None, "std": None}
        if v.size:
            p10, p50, p90 = np.percentile(v, [10, 50, 90])
            row |= {"mean": float(v.mean()), "median": float(p50), "p10": float(p10), "p90": float(p90),
                    "std": float(v.std())}
        rows.append(row)
    return rows
