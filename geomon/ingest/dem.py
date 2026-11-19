"""Digital elevation model: IGN RGE ALTI (WMS-R raw elevation), stored as Zarr.

The Géoplateforme WMS-R serves float32 elevations as GeoTIFF
(layer ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES). The AOI extent is requested
at GEOMON_DEM_RES_M in Lambert-93 and stored with slope and aspect.

The grid is processed in strips of STRIP rows (one row more on each side for the
slope), each written into the store as it comes: a département at 5 m (over a
billion cells) never sits in memory. Overviews (``dem_ov4``, ``dem_ov16``,
``dem_ov64``: 20, 80 and 320 m) are written in the same pass, so that the web
app draws large areas and summarises them without reading the full grid:
elevation and slope are block means, the aspect a circular mean.
"""

from __future__ import annotations

import logging

import numpy as np
import xarray as xr
from rasterio.io import MemoryFile

from ..aoi import AOI
from ..arrays import quiet
from ..config import settings
from ..http import IGN_WMS_R, session
from ..zarrio import staged
from .rasters import Grid, register

log = logging.getLogger(__name__)

LAYER = "ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES"
MIN_ELEVATION_M = -20.0
MAX_TILE_PX = 2000


def _fetch_tile(x0: float, y0: float, x1: float, y1: float, w: int, h: int) -> np.ndarray:
    params = {
        "SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetMap",
        "LAYERS": LAYER, "STYLES": "", "CRS": "EPSG:2154",
        "BBOX": f"{x0},{y0},{x1},{y1}", "WIDTH": w, "HEIGHT": h,
        "FORMAT": "image/geotiff",
    }
    # The WMS rejects percent-encoded ',' and ':' in BBOX/CRS: send them literally
    query = "&".join(f"{k}={v}" for k, v in params.items())
    r = session().get(f"{IGN_WMS_R}?{query}", timeout=300)
    r.raise_for_status()
    if "tiff" not in r.headers.get("Content-Type", ""):
        raise RuntimeError(f"Unexpected WMS response: {r.headers.get('Content-Type')} {r.text[:300]}")
    with MemoryFile(r.content) as mem, mem.open() as src:
        arr = src.read(1).astype("float32")
        if src.nodata is not None:
            arr[arr == src.nodata] = np.nan
    # IGN no-data is -99999, but the WMS resamples it into the neighbouring pixels at the edge of the
    # sea (-993 m, -150 m...). Mainland France goes no lower than about -10 m: anything below -20 m is
    # no-data, and so is a pixel below sea level that touches no-data (a smaller share of -99999).
    arr[arr < MIN_ELEVATION_M] = np.nan
    bad = ~np.isfinite(arr)
    ring = np.zeros_like(bad)
    ring[1:] |= bad[:-1]
    ring[:-1] |= bad[1:]
    ring[:, 1:] |= bad[:, :-1]
    ring[:, :-1] |= bad[:, 1:]
    arr[ring & (arr < 0)] = np.nan
    return arr


def _slope_aspect(z: np.ndarray, res: float) -> tuple[np.ndarray, np.ndarray]:
    dzdy, dzdx = np.gradient(z, res)          # rows go north -> south
    slope = np.degrees(np.arctan(np.hypot(dzdx, dzdy)))
    # Aspect: direction the slope faces, clockwise from north
    aspect = (np.degrees(np.arctan2(-dzdx, dzdy)) + 360.0) % 360.0
    return slope.astype("float32"), aspect.astype("float32")


STRIP = 1536                 # rows per strip: a multiple of the chunk and of the coarsest overview
CHUNK = 512
OVERVIEWS = (4, 16, 64)      # overview factors (20, 80, 320 m at 5 m)
VARIABLES = {
    "elevation": {"units": "m", "long_name": "Elevation (NGF-IGN69)"},
    "slope": {"units": "degree", "long_name": "Slope"},
    "aspect": {"units": "degree", "long_name": "Aspect (clockwise from north)"},
}


def _fetch_rows(grid: Grid, a0: int, a1: int) -> tuple[np.ndarray, int]:
    """Elevation of grid rows a0..a1 (every column), in WMS tiles of at most MAX_TILE_PX."""
    h = a1 - a0
    z = np.full((h, grid.width), np.nan, dtype="float32")
    ty1 = grid.y1 - a0 * grid.res
    n = 0
    for c0 in range(0, grid.width, MAX_TILE_PX):
        w = min(MAX_TILE_PX, grid.width - c0)
        tx0 = grid.x0 + c0 * grid.res
        z[:, c0:c0 + w] = _fetch_tile(tx0, ty1 - h * grid.res, tx0 + w * grid.res, ty1, w, h)
        n += 1
    return z, n


def _blocks(a: np.ndarray, k: int, rows: int, cols: int) -> np.ndarray:
    """Mean of k x k blocks (NaN ignored) over the first rows x cols blocks."""
    with quiet():
        return np.nanmean(a[:rows * k, :cols * k].reshape(rows, k, cols, k), axis=(1, 3)).astype("float32")


def _circular_blocks(deg: np.ndarray, k: int, rows: int, cols: int) -> np.ndarray:
    r = np.radians(deg)
    s, c = _blocks(np.sin(r), k, rows, cols), _blocks(np.cos(r), k, rows, cols)
    return ((np.degrees(np.arctan2(s, c)) + 360.0) % 360.0).astype("float32")


def _template(grid: Grid, k: int, attrs: dict) -> xr.Dataset:
    """An empty store layout (chunked, nothing computed) for the full grid (k=1) or an overview."""
    import dask.array as da

    h, w = grid.height // k if k > 1 else grid.height, grid.width // k if k > 1 else grid.width
    res = grid.res * k
    x = grid.x0 + res * (np.arange(w) + 0.5)
    y = grid.y1 - res * (np.arange(h) + 0.5)
    ds = xr.Dataset({v: (("y", "x"), da.full((h, w), np.nan, dtype="float32", chunks=(CHUNK, CHUNK)), a)
                     for v, a in VARIABLES.items()},
                    coords={"y": ("y", y, {"units": "m", "standard_name": "projection_y_coordinate"}),
                            "x": ("x", x, {"units": "m", "standard_name": "projection_x_coordinate"})},
                    attrs=attrs | {"resolution_m": res, "overview": k})
    from rasterio.transform import from_origin

    return ds.rio.write_crs(grid.crs).rio.write_transform(from_origin(grid.x0, grid.y1, res, res))


def run(aoi: AOI) -> dict:
    grid = Grid(*aoi.bbox_l93, res=settings.dem_res_m)
    h, w = grid.height, grid.width
    source = f"IGN RGE ALTI via Géoplateforme WMS-R ({LAYER})"
    attrs = {"source": source, "aoi": aoi.code, "overviews": list(OVERVIEWS)}
    base = settings.site_zarr_dir
    stores = {1: base / "dem.zarr", **{k: base / f"dem_ov{k}.zarr" for k in OVERVIEWS}}
    lo, hi, valid, tiles = np.inf, -np.inf, 0, 0
    with staged(stores[1]) as tmp, staged(stores[4]) as t4, staged(stores[16]) as t16, staged(stores[64]) as t64:
        tmps = {1: tmp, 4: t4, 16: t16, 64: t64}
        for k, t in tmps.items():
            _template(grid, k, attrs).to_zarr(t, mode="w", compute=False, consolidated=True)
        for r0 in range(0, h, STRIP):
            r1 = min(h, r0 + STRIP)
            a0, a1 = max(0, r0 - 1), min(h, r1 + 1)          # one more row each side, for the slope
            z, n = _fetch_rows(grid, a0, a1)
            tiles += n
            slope, aspect = _slope_aspect(z, grid.res)
            cut = slice(r0 - a0, r0 - a0 + (r1 - r0))
            z, slope, aspect = z[cut], slope[cut], aspect[cut]
            region = {"y": slice(r0, r1), "x": slice(0, w)}
            xr.Dataset({"elevation": (("y", "x"), z), "slope": (("y", "x"), slope),
                        "aspect": (("y", "x"), aspect)}).to_zarr(tmp, region=region)
            for k in OVERVIEWS:
                k0, k1 = r0 // k, min(h // k, r1 // k)
                if k1 <= k0:
                    continue
                rows, cols = k1 - k0, w // k
                xr.Dataset({"elevation": (("y", "x"), _blocks(z, k, rows, cols)),
                            "slope": (("y", "x"), _blocks(slope, k, rows, cols)),
                            "aspect": (("y", "x"), _circular_blocks(aspect, k, rows, cols))}
                           ).to_zarr(tmps[k], region={"y": slice(k0, k1), "x": slice(0, cols)})
            ok = np.isfinite(z)
            if ok.any():
                lo, hi = min(lo, float(z[ok].min())), max(hi, float(z[ok].max()))
            valid += int(ok.sum())
            log.info("DEM: rows %d-%d of %d (%d tile(s) so far)", r0, r1, h, tiles)
    log.info("DEM: %d tile(s), %d x %d px at %.0f m, overviews x%s", tiles, w, h, grid.res,
             ", x".join(map(str, OVERVIEWS)))
    register(_template(grid, 1, attrs), "dem", stores[1], grid, source, "Elevation, slope and aspect",
             {"overviews": list(OVERVIEWS)})
    return {
        "shape": [h, w],
        "res_m": grid.res,
        "overviews": list(OVERVIEWS),
        "min_m": lo if valid else None,
        "max_m": hi if valid else None,
        "valid_fraction": valid / (h * w),
    }
