"""Soil properties: ISRIC SoilGrids 2.0 (250 m, 6 layers down to 2 m), stored as Zarr.

SoilGrids is read through its public VRTs with GDAL (/vsicurl): only the
tiles covering the AOI are fetched (Specifications §1, AOI only). Values are
converted from SoilGrids' integer storage units to physical units.

Derived hydraulic properties use the Saxton & Rawls (2006) pedotransfer
functions (texture + organic matter). They are estimates and are labelled
as such. Coarse fragments reduce the available water of each layer.
"""

from __future__ import annotations

import logging

import numpy as np
import rasterio
import xarray as xr
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT

from ..aoi import AOI
from ..config import settings
from .rasters import Grid, write_zarr

log = logging.getLogger(__name__)

BASE = "/vsicurl/https://files.isric.org/soilgrids/latest/data"

DEPTHS = [(0, 5), (5, 15), (15, 30), (30, 60), (60, 100), (100, 200)]   # cm

# name: (divisor to physical unit, unit, long name)
PROPERTIES: dict[str, tuple[float, str, str]] = {
    "clay":     (10.0,  "percent",      "Clay content (mass fraction of fine earth)"),
    "sand":     (10.0,  "percent",      "Sand content (mass fraction of fine earth)"),
    "silt":     (10.0,  "percent",      "Silt content (mass fraction of fine earth)"),
    "bdod":     (100.0, "g/cm**3",      "Bulk density of the fine earth"),
    "soc":      (10.0,  "g/kg",         "Soil organic carbon content"),
    "cfvo":     (10.0,  "percent",      "Coarse fragments (volumetric)"),
    "phh2o":    (10.0,  "dimensionless", "pH in water"),
    "nitrogen": (100.0, "g/kg",         "Total nitrogen"),
    "cec":      (10.0,  "cmol/kg",      "Cation exchange capacity at pH 7 (cmol(c)/kg)"),
}

USDA_CLASSES = [
    "sand", "loamy sand", "sandy loam", "loam", "silt loam", "silt",
    "sandy clay loam", "clay loam", "silty clay loam", "sandy clay", "silty clay", "clay",
]

GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "GDAL_HTTP_MAX_RETRY": "5",
    "GDAL_HTTP_RETRY_DELAY": "3",
    "VSI_CACHE": "TRUE",
    "CPL_VSIL_CURL_CACHE_SIZE": str(256 << 20),
}


def _read(prop: str, depth: tuple[int, int], grid: Grid) -> np.ndarray:
    url = f"{BASE}/{prop}/{prop}_{depth[0]}-{depth[1]}cm_mean.vrt"
    with rasterio.open(url) as src, WarpedVRT(
        src, crs=grid.crs, transform=grid.transform, width=grid.width, height=grid.height,
        resampling=Resampling.nearest, src_nodata=src.nodata if src.nodata is not None else -32768,
        nodata=-32768,
    ) as vrt:
        raw = vrt.read(1).astype("float32")
    raw[raw == -32768] = np.nan
    return raw


def usda_texture(sand: np.ndarray, silt: np.ndarray, clay: np.ndarray) -> np.ndarray:
    """USDA texture class index (0..11, see USDA_CLASSES); -1 where undefined."""
    total = sand + silt + clay
    with np.errstate(invalid="ignore", divide="ignore"):
        s, si, c = (100 * a / total for a in (sand, silt, clay))
    conds = [
        si + 1.5 * c < 15,
        (si + 1.5 * c >= 15) & (si + 2 * c < 30),
        ((c >= 7) & (c < 20) & (s > 52) & (si + 2 * c >= 30)) | ((c < 7) & (si < 50) & (si + 2 * c >= 30)),
        (c >= 7) & (c < 27) & (si >= 28) & (si < 50) & (s <= 52),
        ((si >= 50) & (c >= 12) & (c < 27)) | ((si >= 50) & (si < 80) & (c < 12)),
        (si >= 80) & (c < 12),
        (c >= 20) & (c < 35) & (si < 28) & (s > 45),
        (c >= 27) & (c < 40) & (s > 20) & (s <= 45),
        (c >= 27) & (c < 40) & (s <= 20),
        (c >= 35) & (s > 45),
        (c >= 40) & (si >= 40),
        (c >= 40) & (s <= 45) & (si < 40),
    ]
    out = np.select(conds, np.arange(len(conds)), default=-1).astype("int8")
    out[~np.isfinite(total)] = -1
    return out


def saxton_rawls(sand_pct, clay_pct, soc_gkg):
    """Saxton & Rawls (2006) water retention and Ksat.

    Returns (theta_wp, theta_fc, theta_sat) in m3/m3 and ksat in mm/h.
    """
    S = sand_pct / 100.0
    C = clay_pct / 100.0
    OM = np.clip(soc_gkg / 10.0 * 1.724, 0, 8)          # organic matter, % mass (eq. valid <= 8 %)

    t1500t = -0.024 * S + 0.487 * C + 0.006 * OM + 0.005 * S * OM - 0.013 * C * OM + 0.068 * S * C + 0.031
    t1500 = t1500t + (0.14 * t1500t - 0.02)
    t33t = -0.251 * S + 0.195 * C + 0.011 * OM + 0.006 * S * OM - 0.027 * C * OM + 0.452 * S * C + 0.299
    t33 = t33t + (1.283 * t33t ** 2 - 0.374 * t33t - 0.015)
    ts33t = 0.278 * S + 0.034 * C + 0.022 * OM - 0.018 * S * OM - 0.027 * C * OM - 0.584 * S * C + 0.078
    ts33 = ts33t + (0.636 * ts33t - 0.107)
    ts = t33 + ts33 - 0.097 * S + 0.043

    t1500 = np.clip(t1500, 0.005, 0.6)
    t33 = np.clip(t33, t1500 + 0.005, 0.7)
    ts = np.maximum(ts, t33 + 0.01)
    with np.errstate(invalid="ignore", divide="ignore"):
        B = (np.log(1500.0) - np.log(33.0)) / (np.log(t33) - np.log(t1500))
        lam = 1.0 / B
        ksat = 1930.0 * (ts - t33) ** (3.0 - lam)
    return t1500, t33, ts, ksat


def run(aoi: AOI) -> dict:
    grid = Grid(*aoi.bbox_l93, res=settings.soil_res_m)
    nd = len(DEPTHS)
    data: dict[str, np.ndarray] = {}
    with rasterio.Env(**GDAL_ENV):
        for prop, (div, _unit, _name) in PROPERTIES.items():
            layers = []
            for d in DEPTHS:
                layers.append(_read(prop, d, grid) / div)
            data[prop] = np.stack(layers)
            log.info("SoilGrids %s: done", prop)

    wp, fc, sat, ksat = saxton_rawls(data["sand"], data["clay"], data["soc"])
    thickness_mm = np.array([(b - t) * 10.0 for t, b in DEPTHS], dtype="float32")[:, None, None]
    fine_earth = 1.0 - np.nan_to_num(data["cfvo"]) / 100.0
    awc_layer = (fc - wp) * thickness_mm * fine_earth
    awc_total = np.where(np.isfinite(awc_layer).all(axis=0), np.nansum(awc_layer, axis=0), np.nan)
    texture = usda_texture(data["sand"], data["silt"], data["clay"])

    dims3 = ("depth", "y", "x")
    variables = {
        p: (dims3, data[p].astype("float32"), {"units": u, "long_name": n})
        for p, (_d, u, n) in PROPERTIES.items()
    }
    pedo = "estimated with Saxton & Rawls (2006)"
    variables |= {
        "theta_wp": (dims3, wp.astype("float32"),
                     {"units": "m**3/m**3", "long_name": f"Water content at wilting point (1500 kPa), {pedo}"}),
        "theta_fc": (dims3, fc.astype("float32"),
                     {"units": "m**3/m**3", "long_name": f"Water content at field capacity (33 kPa), {pedo}"}),
        "theta_sat": (dims3, sat.astype("float32"),
                      {"units": "m**3/m**3", "long_name": f"Water content at saturation, {pedo}"}),
        "ksat": (dims3, ksat.astype("float32"),
                 {"units": "mm/h", "long_name": f"Saturated hydraulic conductivity, {pedo}"}),
        "awc": (dims3, awc_layer.astype("float32"),
                {"units": "mm", "long_name": f"Available water capacity of the layer, {pedo}"}),
        "texture_usda": (dims3, texture,
                         {"units": "1", "long_name": "USDA texture class",
                          "flag_values": list(range(len(USDA_CLASSES))),
                          "flag_meanings": " ".join(c.replace(" ", "_") for c in USDA_CLASSES)}),
        "awc_total": (("y", "x"), awc_total.astype("float32"),
                      {"units": "mm", "long_name": f"Available water capacity, 0-200 cm, {pedo}"}),
    }
    ds = xr.Dataset(
        variables,
        coords={
            "depth": ("depth", [f"{t}-{b}cm" for t, b in DEPTHS]),
            "depth_top": ("depth", np.array([t for t, _ in DEPTHS], dtype="float32"), {"units": "cm"}),
            "depth_bottom": ("depth", np.array([b for _, b in DEPTHS], dtype="float32"), {"units": "cm"}),
            "y": ("y", grid.y, {"units": "m"}),
            "x": ("x", grid.x, {"units": "m"}),
        },
        attrs={"source": "ISRIC SoilGrids 2.0 (mean predictions), https://soilgrids.org",
               "aoi": aoi.code, "nd_layers": nd},
    )
    write_zarr(ds, "soil", grid, ds.attrs["source"], "Soil properties by layer, 0-200 cm")
    return {
        "shape": [nd, grid.height, grid.width],
        "res_m": grid.res,
        "awc_total_mm_mean": float(np.nanmean(awc_total)) if np.isfinite(awc_total).any() else None,
    }
