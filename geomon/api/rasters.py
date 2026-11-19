"""Raster access for the web map: Zarr -> reprojected PNG overlays and point queries."""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from functools import lru_cache

import matplotlib
import numpy as np
import xarray as xr
from matplotlib import colormaps
from PIL import Image
from pyproj import Transformer
from rasterio.transform import from_origin
from rasterio.warp import Resampling, calculate_default_transform, reproject, transform_bounds

from ..config import settings
from ..ingest.soil import USDA_CLASSES

matplotlib.use("Agg")

DATASETS = {"dem": "dem.zarr", "soil": "soil.zarr"}

# Display styles: colormap, and whether the variable is categorical
STYLES = {
    "elevation": "terrain",
    "hillshade": "gray",
    "slope": "magma_r",
    "aspect": "twilight",
    "clay": "YlOrBr",
    "sand": "YlOrBr",
    "silt": "YlOrBr",
    "bdod": "cividis",
    "soc": "Greens",
    "cfvo": "Greys",
    "phh2o": "RdYlBu",
    "nitrogen": "Purples",
    "cec": "PuBuGn",
    "theta_wp": "Blues",
    "theta_fc": "Blues",
    "theta_sat": "Blues",
    "ksat": "viridis",
    "awc": "Blues",
    "awc_total": "Blues",
}

TEXTURE_COLORS = [
    "#f2e3a0", "#e8cf7c", "#d9b45b", "#a6845a", "#c9b38a", "#d9cfb3",
    "#b97a4c", "#8f5a3c", "#9a7d6a", "#c2603a", "#7d6a7a", "#6b3f2e",
]

_wgs_to_l93 = Transformer.from_crs("EPSG:4326", "EPSG:2154", always_xy=True)
_merc_to_wgs = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)


# Read by window: a département's 5 m grid (over a billion cells) does not fit in memory. The soil
# grid (250 m) is small and loaded whole.
LAZY = {"dem"}
# Whole-grid reads (colour scale, area summary, single image) use the finest level below this size
MAX_CELLS = 40_000_000
MAX_IMAGE_PX = 4096


def _stamp(name: str) -> float | None:
    """Modification time of a dataset of the current site (None when absent): the caches below
    are keyed by it, so a re-ingested dataset is picked up without a reload."""
    z = settings.site_zarr_dir / DATASETS[name] / "zarr.json"
    return z.stat().st_mtime if z.exists() else None


def _level_path(name: str, k: int):
    base = settings.site_zarr_dir / DATASETS[name]
    return base if k == 1 else base.with_name(f"{base.stem}_ov{k}.zarr")


def open_dataset(name: str, k: int = 1) -> xr.Dataset:
    """A raster dataset (dem, soil) of the current site: full resolution (k=1) or an overview
    (k times coarser, written by the ingestion). Lazy for the DEM, loaded for the soil."""
    stamp = _stamp(name)
    if stamp is None:
        raise FileNotFoundError(f"{settings.site_zarr_dir / DATASETS[name]} not found: "
                                f"run the '{name}' ingestion step")
    return _open_dataset(settings.site_code, name, k, stamp)


@lru_cache(maxsize=16)
def _open_dataset(site: str, name: str, k: int, stamp: float) -> xr.Dataset:
    ds = xr.open_zarr(_level_path(name, k), consolidated=True)
    return ds if name in LAZY else ds.load()


@dataclass(frozen=True)
class Level:
    """Grid of one level of a dataset (Lambert-93)."""
    k: int
    res: float
    x0: float          # left edge
    y1: float          # top edge
    width: int
    height: int

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return self.x0, self.y1 - self.height * self.res, self.x0 + self.width * self.res, self.y1


@lru_cache(maxsize=64)
def _level_info(site: str, name: str, k: int, stamp: float | None) -> Level:
    ds = open_dataset(name, k)
    x, y = ds.x.values, ds.y.values
    res = float(abs(x[1] - x[0]))
    return Level(k, res, float(x[0] - res / 2), float(y[0] + res / 2), len(x), len(y))


def levels(name: str) -> list[Level]:
    """Full resolution first, then the overviews stored with it (coarser)."""
    site, stamp = settings.site_code, _stamp(name)
    ks = [1] + [k for k in open_dataset(name).attrs.get("overviews", [])
                if (_level_path(name, k) / "zarr.json").exists()]
    return [_level_info(site, name, k, stamp) for k in ks]


def _finest(name: str, fits) -> Level:
    """The finest level satisfying ``fits``, else the coarsest."""
    ls = levels(name)
    return next((lv for lv in ls if fits(lv)), ls[-1])


def catalogue() -> list[dict]:
    out = []
    for name in DATASETS:
        try:
            ds = open_dataset(name)
        except FileNotFoundError:
            continue
        variables = []
        for v, da in ds.data_vars.items():
            if v == "spatial_ref":
                continue
            variables.append({
                "name": v,
                "long_name": da.attrs.get("long_name", v),
                "units": da.attrs.get("units", ""),
                "has_depth": "depth" in da.dims,
                "categorical": v == "texture_usda",
            })
        if name == "dem":
            variables.append({"name": "hillshade", "long_name": "Hillshade", "units": "",
                              "has_depth": False, "categorical": False})
        order = list(STYLES) + ["texture_usda"]
        variables.sort(key=lambda v: order.index(v["name"]) if v["name"] in order else len(order))
        out.append({
            "name": name,
            "source": ds.attrs.get("source"),
            "depths": [str(d) for d in ds["depth"].values] if "depth" in ds.coords else [],
            "variables": variables,
        })
    return out


def _hillshade(z: np.ndarray, res: float, azimuth=315.0, altitude=45.0) -> np.ndarray:
    dzdy, dzdx = np.gradient(z, res)
    slope = np.arctan(np.hypot(dzdx, dzdy))
    aspect = np.arctan2(-dzdx, dzdy)
    az, alt = np.radians(azimuth), np.radians(altitude)
    hs = np.sin(alt) * np.cos(slope) + np.cos(alt) * np.sin(slope) * np.cos(az - aspect)
    return np.clip(hs, 0, 1).astype("float32")


def _window(name: str, lv: Level, var: str, depth: int, bounds, margin: int = 0):
    """The cells of a level covering ``bounds`` (Lambert-93) plus ``margin`` cells, read alone:
    (array, its affine transform), or None outside the grid. The hillshade is computed from the
    window's elevation (the margin keeps its edges out of a tile)."""
    xmin, ymin, xmax, ymax = bounds
    j0 = max(0, int(np.floor((xmin - lv.x0) / lv.res)) - margin)
    j1 = min(lv.width, int(np.ceil((xmax - lv.x0) / lv.res)) + margin)
    i0 = max(0, int(np.floor((lv.y1 - ymax) / lv.res)) - margin)
    i1 = min(lv.height, int(np.ceil((lv.y1 - ymin) / lv.res)) + margin)
    if j1 <= j0 or i1 <= i0:
        return None
    ds = open_dataset(name, lv.k)
    cells = {"y": slice(i0, i1), "x": slice(j0, j1)}
    if var == "hillshade" and name == "dem":
        arr = _hillshade(ds["elevation"].isel(cells).values.astype("float32"), lv.res)
    else:
        da = ds[var]
        if "depth" in da.dims:
            da = da.isel(depth=depth)
        arr = da.isel(cells).values.astype("float32", copy=False)
    return arr, from_origin(lv.x0 + j0 * lv.res, lv.y1 - i0 * lv.res, lv.res, lv.res)


@dataclass(frozen=True)
class Style:
    """Colour scale of a variable: the same for every depth, and for the image and its tiles."""
    vmin: float | None
    vmax: float | None
    units: str
    long_name: str
    colormap: str
    legend: list


@lru_cache(maxsize=64)
def _style(site: str, name: str, var: str, stamp: float | None) -> Style:
    ds = open_dataset(name)
    attrs = ds[var].attrs if var in ds else {}
    units, long_name = attrs.get("units", ""), attrs.get("long_name", var)
    if var == "texture_usda":
        legend = [{"value": i, "label": USDA_CLASSES[i], "color": col} for i, col in enumerate(TEXTURE_COLORS)]
        return Style(None, None, units, long_name, "texture", legend)
    if var == "hillshade":
        vmin, vmax = 0.0, 1.0
    elif var == "aspect":
        vmin, vmax = 0.0, 360.0
    else:
        lv = _finest(name, lambda lv: lv.width * lv.height <= MAX_CELLS)
        full = open_dataset(name, lv.k)[var].values
        finite = full[np.isfinite(full)]
        vmin, vmax = (float(v) for v in np.percentile(finite, [2, 98])) if finite.size else (0.0, 1.0)
        if vmin == vmax:
            vmax = vmin + 1e-6
    cmap_name = STYLES.get(var, "viridis")
    cmap = colormaps[cmap_name]
    legend = [{"value": vmin + f * (vmax - vmin), "color": "#%02x%02x%02x" % tuple(int(c * 255) for c in cmap(f)[:3])}
              for f in np.linspace(0, 1, 6)]
    return Style(vmin, vmax, units, long_name, cmap_name, legend)


def _png(dst: np.ndarray, style: Style, var: str) -> bytes:
    """Reprojected values -> PNG (transparent where there is no value)."""
    valid = np.isfinite(dst)
    if style.colormap == "texture":
        rgba = np.zeros((*dst.shape, 4), dtype="uint8")
        for i, col in enumerate(TEXTURE_COLORS):
            m = valid & (dst == i)
            if m.any():
                rgba[m, :3] = tuple(int(col[k:k + 2], 16) for k in (1, 3, 5))
                rgba[m, 3] = 210
    else:
        norm = np.clip((dst - style.vmin) / (style.vmax - style.vmin), 0, 1)
        rgba = (colormaps[style.colormap](np.nan_to_num(norm)) * 255).astype("uint8")
        rgba[..., 3] = np.where(valid, 255 if var == "hillshade" else 215, 0)
    buf = io.BytesIO()
    # Default zlib level: optimize=True took 90 s for a 5 m DEM of 10,000 x 3,000 px, for files
    # only 25 % smaller. The hillshade is grey: grey + alpha halves the data to encode.
    img = Image.fromarray(rgba[..., [0, 3]], "LA") if var == "hillshade" else Image.fromarray(rgba, "RGBA")
    img.save(buf, format="PNG")
    return buf.getvalue()


def _resampling(categorical: bool, src_res: float, dst_ground_res: float) -> Resampling:
    if categorical:
        return Resampling.nearest
    if dst_ground_res > 2 * src_res:
        return Resampling.average             # zoomed out: average the cells of each pixel
    return Resampling.nearest if src_res >= 100 else Resampling.bilinear


def _warp(arr: np.ndarray, src_transform, var: str, dst: np.ndarray, dst_transform, resampling) -> np.ndarray:
    reproject(arr, dst, src_transform=src_transform, src_crs="EPSG:2154",
              src_nodata=-1 if var == "texture_usda" else np.nan,
              dst_transform=dst_transform, dst_crs="EPSG:3857", dst_nodata=np.nan, resampling=resampling)
    return dst


def render(name: str, var: str, depth: int = 0) -> bytes:
    """The whole variable as one image (``…/{var}.png``; the map uses the tiles), from the finest
    level at most MAX_IMAGE_PX wide and high."""
    return _render(settings.site_code, name, var, depth, _stamp(name))


def _mercator_extent(lv: Level):
    return calculate_default_transform("EPSG:2154", "EPSG:3857", lv.width, lv.height, *lv.bounds)


def _corners(dst_transform, dw: int, dh: int) -> list[list[float]]:
    x0, y1 = dst_transform.c, dst_transform.f
    x1, y0 = x0 + dw * dst_transform.a, y1 + dh * dst_transform.e
    return [list(_merc_to_wgs.transform(cx, cy)) for cx, cy in ((x0, y1), (x1, y1), (x1, y0), (x0, y0))]


@lru_cache(maxsize=16)
def _render(site: str, name: str, var: str, depth: int, stamp: float | None) -> bytes:
    lv = _finest(name, lambda lv: max(lv.width, lv.height) <= MAX_IMAGE_PX)
    arr, src_transform = _window(name, lv, var, depth, lv.bounds)
    style = _style(site, name, var, stamp)
    dst_transform, dw, dh = _mercator_extent(lv)
    dst = _warp(arr, src_transform, var, np.full((dh, dw), np.nan, dtype="float32"), dst_transform,
                _resampling(var == "texture_usda", lv.res, lv.res))
    return _png(dst, style, var)


# ---------------------------------------------------------------------------
# XYZ tiles (Web Mercator, 256 px): the map loads the tiles in view, at the resolution of the zoom
# ---------------------------------------------------------------------------
TILE_PX = 256
WORLD_M = 2 * np.pi * 6378137.0          # width of the Web Mercator world


def _lat_of(merc_y: float) -> float:
    return float(np.degrees(2 * np.arctan(np.exp(merc_y / 6378137.0)) - np.pi / 2))


def info(name: str, var: str, depth: int = 0) -> dict:
    """Colour scale, legend, extent and tile URL of a variable, without reading its cells."""
    site, stamp = settings.site_code, _stamp(name)
    lv, style = levels(name)[0], _style(site, name, var, stamp)
    if var not in open_dataset(name) and var != "hillshade":
        raise KeyError(var)
    dst_transform, dw, dh = _mercator_extent(lv)
    w, s, e, n = transform_bounds("EPSG:2154", "EPSG:4326", *lv.bounds)
    # Deepest zoom worth computing: a tile pixel about the size of a cell (overzoomed beyond)
    ground = WORLD_M * np.cos(np.radians((s + n) / 2)) / TILE_PX
    maxzoom = int(min(18, max(0, np.ceil(np.log2(ground / lv.res)))))
    return {"coordinates": _corners(dst_transform, dw, dh), "bounds": [w, s, e, n], "maxzoom": maxzoom,
            "tiles": f"/api/raster/{name}/{var}/tiles/{{z}}/{{x}}/{{y}}.png?depth={depth}&v={stamp:.0f}&site={site}",
            "vmin": style.vmin, "vmax": style.vmax, "units": style.units, "long_name": style.long_name,
            "colormap": style.colormap, "legend": style.legend}


@lru_cache(maxsize=1)
def _empty_tile() -> bytes:
    buf = io.BytesIO()
    Image.new("LA", (TILE_PX, TILE_PX)).save(buf, format="PNG")
    return buf.getvalue()


def tile(name: str, var: str, depth: int, z: int, x: int, y: int) -> bytes:
    site, stamp = settings.site_code, _stamp(name)
    return _tile(site, name, var, depth, stamp, z, x, y)


@lru_cache(maxsize=512)
def _tile(site: str, name: str, var: str, depth: int, stamp: float | None, z: int, x: int, y: int) -> bytes:
    size = WORLD_M / 2 ** z
    x0, y1 = -WORLD_M / 2 + x * size, WORLD_M / 2 - y * size
    ls = levels(name)
    left, bottom, right, top = transform_bounds("EPSG:2154", "EPSG:3857", *ls[0].bounds)
    if x0 > right or x0 + size < left or y1 < bottom or y1 - size > top:
        return _empty_tile()
    res = size / TILE_PX
    ground = res * np.cos(np.radians(_lat_of(y1 - size / 2)))
    # The coarsest level with at least 2 x 2 cells per tile pixel (each pixel still averages several
    # cells): a tile reads at most ~2,000 cells a side, whatever the size of the dataset
    lv = max((lv for lv in ls if lv.res <= ground / 2), key=lambda lv: lv.res, default=ls[0])
    win = _window(name, lv, var, depth, transform_bounds("EPSG:3857", "EPSG:2154", x0, y1 - size, x0 + size, y1),
                  margin=2)
    if win is None:
        return _empty_tile()
    arr, src_transform = win
    dst = _warp(arr, src_transform, var, np.full((TILE_PX, TILE_PX), np.nan, dtype="float32"),
                from_origin(x0, y1, res, res), _resampling(var == "texture_usda", lv.res, ground))
    if not np.isfinite(dst).any():
        return _empty_tile()
    return _png(dst, _style(site, name, var, stamp), var)


def sample(lon: float, lat: float) -> dict:
    """Values of every raster variable at a point (full resolution, one chunk read per variable)."""
    xq, yq = _wgs_to_l93.transform(lon, lat)
    out: dict = {}
    for name in DATASETS:
        try:
            ds = open_dataset(name)
        except FileNotFoundError:
            continue
        lv = levels(name)[0]
        left, bottom, right, top = lv.bounds
        if not (left <= xq <= right and bottom <= yq <= top):
            continue
        p = ds.sel(x=xq, y=yq, method="nearest")
        values = {}
        for v, da in p.data_vars.items():
            if v == "spatial_ref":
                continue
            vals = da.values
            entry = {"units": ds[v].attrs.get("units", ""), "long_name": ds[v].attrs.get("long_name", v)}
            if "depth" in da.dims:
                entry["values"] = [None if not np.isfinite(float(x)) else float(x) for x in vals]
            else:
                f = float(vals)
                entry["value"] = f if np.isfinite(f) else None
            if v == "texture_usda":
                entry["labels"] = [USDA_CLASSES[int(x)] if 0 <= int(x) < len(USDA_CLASSES) else None
                                   for x in vals]
            values[v] = entry
        out[name] = {
            "depths": [str(d) for d in ds["depth"].values] if "depth" in ds.coords else None,
            "variables": values,
            "resolution_m": lv.res,
        }
    return out


# ---------------------------------------------------------------------------
# Zonal statistics over an area (commune or group)
# ---------------------------------------------------------------------------
_SKIP_ZONAL = {"spatial_ref", "aspect"}      # aspect is circular: a mean is meaningless


def _mask(ds: xr.Dataset, geom_l93) -> np.ndarray:
    """Cells whose centre falls inside the geometry (Lambert-93)."""
    from rasterio.features import geometry_mask
    from rasterio.transform import Affine

    x, y = ds.x.values, ds.y.values
    res_x, res_y = float(x[1] - x[0]), float(y[1] - y[0])
    transform = Affine(res_x, 0, x[0] - res_x / 2, 0, res_y, y[0] - res_y / 2)
    return geometry_mask([geom_l93], out_shape=(len(y), len(x)), transform=transform, invert=True)


def zonal(geom_wgs84: dict) -> dict:
    """Mean, min and max of every raster variable inside an area; dominant texture by depth.
    From the finest level of at most MAX_CELLS cells (full resolution for a few communes; an
    overview for a département). Cached until the datasets change (the result is shared: do not
    modify it)."""
    return _zonal(settings.site_code, json.dumps(geom_wgs84, sort_keys=True),
                  tuple(_stamp(n) for n in DATASETS))


@lru_cache(maxsize=32)
def _zonal(site: str, geom_json: str, stamps: tuple) -> dict:
    from shapely.geometry import mapping, shape
    from shapely.ops import transform as shp_transform

    geom_l93 = mapping(shp_transform(_wgs_to_l93.transform, shape(json.loads(geom_json))))
    out: dict = {}
    for name in DATASETS:
        try:
            lv = _finest(name, lambda lv: lv.width * lv.height <= MAX_CELLS)
        except FileNotFoundError:
            continue
        ds = open_dataset(name, lv.k)
        inside = _mask(ds, geom_l93)
        if not inside.any():
            continue
        variables = {}
        for v, da in ds.data_vars.items():
            if v in _SKIP_ZONAL:
                continue
            entry = {"units": da.attrs.get("units", ""), "long_name": da.attrs.get("long_name", v)}
            if v == "texture_usda":
                dom = []
                for k in range(da.sizes["depth"]):
                    vals = da.isel(depth=k).values[inside]
                    vals = vals[np.isfinite(vals)].astype(int)
                    vals = vals[(vals >= 0) & (vals < len(USDA_CLASSES))]   # -1: no data
                    dom.append(USDA_CLASSES[np.bincount(vals).argmax()] if vals.size else None)
                entry["dominant"] = dom
            elif "depth" in da.dims:
                arr = da.values[:, inside]
                entry["mean"] = [float(np.nanmean(a)) if np.isfinite(a).any() else None for a in arr]
            else:
                a = da.values[inside]
                a = a[np.isfinite(a)]
                if a.size:
                    entry |= {"mean": float(a.mean()), "min": float(a.min()), "max": float(a.max()),
                              "p10": float(np.percentile(a, 10)), "p90": float(np.percentile(a, 90))}
            variables[v] = entry
        out[name] = {"resolution_m": lv.res, "cells": int(inside.sum()),
                     "depths": [str(d) for d in ds["depth"].values] if "depth" in ds.coords else None,
                     "variables": variables}
    return out
