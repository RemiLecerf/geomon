"""Digital elevation model: IGN RGE ALTI (WMS-R raw elevation), stored as Zarr.

The Géoplateforme WMS-R serves float32 elevations as GeoTIFF
(layer ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES). The AOI extent is requested
in tiles at GEOMON_DEM_RES_M in Lambert-93, mosaicked, and stored with slope
and aspect.
"""

from __future__ import annotations

import logging

import numpy as np
import xarray as xr
from rasterio.io import MemoryFile

from ..aoi import AOI
from ..config import settings
from ..http import IGN_WMS_R, session
from .rasters import Grid, write_zarr

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


def run(aoi: AOI) -> dict:
    grid = Grid(*aoi.bbox_l93, res=settings.dem_res_m)
    z = np.full((grid.height, grid.width), np.nan, dtype="float32")
    step = MAX_TILE_PX
    n = 0
    for r0 in range(0, grid.height, step):
        for c0 in range(0, grid.width, step):
            h = min(step, grid.height - r0)
            w = min(step, grid.width - c0)
            tx0 = grid.x0 + c0 * grid.res
            ty1 = grid.y1 - r0 * grid.res
            z[r0:r0 + h, c0:c0 + w] = _fetch_tile(tx0, ty1 - h * grid.res, tx0 + w * grid.res, ty1, w, h)
            n += 1
    log.info("DEM: %d tile(s), %d x %d px at %.0f m", n, grid.width, grid.height, grid.res)

    slope, aspect = _slope_aspect(z, grid.res)
    coords = {"y": ("y", grid.y), "x": ("x", grid.x)}
    ds = xr.Dataset(
        {
            "elevation": (("y", "x"), z, {"units": "m", "long_name": "Elevation (NGF-IGN69)"}),
            "slope": (("y", "x"), slope, {"units": "degree", "long_name": "Slope"}),
            "aspect": (("y", "x"), aspect, {"units": "degree",
                                             "long_name": "Aspect (clockwise from north)"}),
        },
        coords=coords,
        attrs={"source": f"IGN RGE ALTI via Géoplateforme WMS-R ({LAYER})",
               "aoi": aoi.code},
    )
    ds["x"].attrs.update(units="m", standard_name="projection_x_coordinate")
    ds["y"].attrs.update(units="m", standard_name="projection_y_coordinate")
    ds = ds.chunk({"y": 512, "x": 512})
    write_zarr(ds, "dem", grid, ds.attrs["source"], "Elevation, slope and aspect")
    valid = np.isfinite(z)
    return {
        "shape": [grid.height, grid.width],
        "res_m": grid.res,
        "min_m": float(np.nanmin(z)) if valid.any() else None,
        "max_m": float(np.nanmax(z)) if valid.any() else None,
        "valid_fraction": float(valid.mean()),
    }
