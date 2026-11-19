"""Raster access for the web map: Zarr -> reprojected PNG overlays and point queries."""

from __future__ import annotations

import io
from dataclasses import dataclass
from functools import lru_cache

import matplotlib
import numpy as np
import xarray as xr
from matplotlib import colormaps
from PIL import Image
from pyproj import Transformer
from rasterio.transform import from_origin
from rasterio.warp import Resampling, calculate_default_transform, reproject

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


def open_dataset(name: str) -> xr.Dataset:
    """A raster dataset (dem, soil) of the current site."""
    return _open_dataset(settings.site_code, name)


@lru_cache(maxsize=8)
def _open_dataset(site: str, name: str) -> xr.Dataset:
    path = settings.site_zarr_dir / DATASETS[name]
    if not path.exists():
        raise FileNotFoundError(f"{path} not found: run the '{name}' ingestion step")
    return xr.open_zarr(path, consolidated=True).load()


def clear_cache() -> None:
    _open_dataset.cache_clear()
    _render.cache_clear()


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


def _array(name: str, var: str, depth: int) -> tuple[np.ndarray, xr.Dataset]:
    ds = open_dataset(name)
    if var == "hillshade" and name == "dem":
        res = float(abs(ds.x.values[1] - ds.x.values[0]))
        return _hillshade(ds["elevation"].values, res), ds
    da = ds[var]
    if "depth" in da.dims:
        da = da.isel(depth=depth)
    return da.values.astype("float32"), ds


@dataclass
class Rendered:
    png: bytes
    coordinates: list[list[float]]   # TL, TR, BR, BL in lon/lat
    vmin: float | None
    vmax: float | None
    units: str
    long_name: str
    colormap: str
    legend: list[dict]


def render(name: str, var: str, depth: int = 0) -> Rendered:
    return _render(settings.site_code, name, var, depth)


@lru_cache(maxsize=64)
def _render(site: str, name: str, var: str, depth: int = 0) -> Rendered:
    arr, ds = _array(name, var, depth)
    x, y = ds.x.values, ds.y.values
    res = float(abs(x[1] - x[0]))
    src_transform = from_origin(float(x[0] - res / 2), float(y[0] + res / 2), res, res)
    h, w = arr.shape
    left, top = x[0] - res / 2, y[0] + res / 2
    right, bottom = left + w * res, top - h * res

    dst_transform, dw, dh = calculate_default_transform(
        "EPSG:2154", "EPSG:3857", w, h, left=left, bottom=bottom, right=right, top=top
    )
    dst = np.full((dh, dw), np.nan, dtype="float32")
    categorical = var == "texture_usda"
    reproject(
        arr if not categorical else arr.astype("float32"),
        dst,
        src_transform=src_transform, src_crs="EPSG:2154", src_nodata=np.nan if not categorical else -1,
        dst_transform=dst_transform, dst_crs="EPSG:3857", dst_nodata=np.nan,
        resampling=Resampling.nearest if categorical or res >= 100 else Resampling.bilinear,
    )

    da_attrs = ds[var].attrs if var in ds else {}
    units = da_attrs.get("units", "")
    long_name = da_attrs.get("long_name", var)
    rgba = np.zeros((dh, dw, 4), dtype="uint8")
    valid = np.isfinite(dst)

    if categorical:
        legend = []
        for i, col in enumerate(TEXTURE_COLORS):
            m = valid & (dst == i)
            if m.any():
                rgb = tuple(int(col[k:k + 2], 16) for k in (1, 3, 5))
                rgba[m, :3] = rgb
                rgba[m, 3] = 210
            legend.append({"value": i, "label": USDA_CLASSES[i], "color": col})
        vmin = vmax = None
        cmap_name = "texture"
    else:
        # Same colour scale for every depth of a variable
        full = ds[var].values if var in ds else arr
        finite = full[np.isfinite(full)]
        if var == "hillshade":
            vmin, vmax = 0.0, 1.0
        elif var == "aspect":
            vmin, vmax = 0.0, 360.0
        elif finite.size:
            vmin, vmax = (float(v) for v in np.percentile(finite, [2, 98]))
            if vmin == vmax:
                vmax = vmin + 1e-6
        else:
            vmin, vmax = 0.0, 1.0
        cmap_name = STYLES.get(var, "viridis")
        cmap = colormaps[cmap_name]
        norm = np.clip((dst - vmin) / (vmax - vmin), 0, 1)
        rgba = (cmap(np.nan_to_num(norm)) * 255).astype("uint8")
        rgba[..., 3] = np.where(valid, 255 if var == "hillshade" else 215, 0)
        legend = [
            {"value": vmin + f * (vmax - vmin),
             "color": "#%02x%02x%02x" % tuple(int(c * 255) for c in cmap(f)[:3])}
            for f in np.linspace(0, 1, 6)
        ]

    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, format="PNG", optimize=True)

    x0, y1 = dst_transform.c, dst_transform.f
    x1, y0 = x0 + dw * dst_transform.a, y1 + dh * dst_transform.e
    corners = [(x0, y1), (x1, y1), (x1, y0), (x0, y0)]
    coordinates = [list(_merc_to_wgs.transform(cx, cy)) for cx, cy in corners]
    return Rendered(buf.getvalue(), coordinates, vmin, vmax, units, long_name, cmap_name, legend)


def sample(lon: float, lat: float) -> dict:
    """Values of every raster variable at a point."""
    xq, yq = _wgs_to_l93.transform(lon, lat)
    out: dict = {}
    for name in DATASETS:
        try:
            ds = open_dataset(name)
        except FileNotFoundError:
            continue
        res = float(abs(ds.x.values[1] - ds.x.values[0]))
        if not (ds.x.min() - res / 2 <= xq <= ds.x.max() + res / 2
                and ds.y.min() - res / 2 <= yq <= ds.y.max() + res / 2):
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
            "resolution_m": res,
        }
    return out


# ---------------------------------------------------------------------------
# Zonal statistics over an area (commune or group)
# ---------------------------------------------------------------------------
_SKIP_ZONAL = {"spatial_ref", "aspect"}      # aspect is circular: a mean is meaningless


def _mask(ds: xr.Dataset, geom_l93) -> np.ndarray:
    """Cells whose centre falls inside the geometry (Lambert-93)."""
    from rasterio.features import geometry_mask

    x, y = ds.x.values, ds.y.values
    res_x, res_y = float(x[1] - x[0]), float(y[1] - y[0])
    from rasterio.transform import Affine
    transform = Affine(res_x, 0, x[0] - res_x / 2, 0, res_y, y[0] - res_y / 2)
    return geometry_mask([geom_l93], out_shape=(len(y), len(x)), transform=transform, invert=True)


def zonal(geom_wgs84: dict) -> dict:
    """Mean, min and max of every raster variable inside an area; dominant texture by depth."""
    from shapely.geometry import mapping, shape
    from shapely.ops import transform as shp_transform

    geom_l93 = mapping(shp_transform(_wgs_to_l93.transform, shape(geom_wgs84)))
    out: dict = {}
    for name in DATASETS:
        try:
            ds = open_dataset(name)
        except FileNotFoundError:
            continue
        inside = _mask(ds, geom_l93)
        if not inside.any():
            continue
        res = float(abs(ds.x.values[1] - ds.x.values[0]))
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
        out[name] = {"resolution_m": res, "cells": int(inside.sum()),
                     "depths": [str(d) for d in ds["depth"].values] if "depth" in ds.coords else None,
                     "variables": variables}
    return out
