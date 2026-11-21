"""Radar rain: where it rains and has been raining over the last hours (animation frames, rain
over an area, accumulations). ``source``: ``opera`` (EUMETNET, default) or ``mf`` (Météo-France)."""

from __future__ import annotations

from functools import lru_cache

import numpy as np
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from .. import datasets, radar
from .. import sentinel2 as s2
from ..config import settings
from .sentinel2 import grid_png, grid_rgba_png, legend

router = APIRouter(prefix="/api/radar", tags=["radar"])
HOURS = (1, 3, 6, 12, 24)
MAX_FRAMES = 96               # longer periods are thinned (24 h: one frame every 15 minutes)


def _source(source: str) -> radar.Source:
    if source not in radar.SOURCES:
        raise HTTPException(404, f"Unknown radar source {source} ({', '.join(radar.SOURCES)})")
    return radar.SOURCES[source]


def _store(source: str):
    src = _source(source)
    ds = radar.open_store(source, settings.site_code)
    if ds is None or not ds.sizes.get("time"):
        raise HTTPException(404, datasets.missing(src.dataset, f"{src.label} image"))
    return ds


def _stamp(source: str) -> float:
    z = radar.store_path(source, settings.site_code) / "zarr.json"
    return z.stat().st_mtime if z.exists() else 0.0


def _source_info(code: str) -> dict:
    src = radar.SOURCES[code]
    pol = datasets.policy(settings.site_code, src.dataset)
    out = {"source": code, "label": src.label, "attribution": src.attribution, "resolution_m": src.resolution_m,
           "dataset": src.dataset, "mode": pol["mode"], "keep_hours": pol["keep_hours"],
           "needs": datasets.needs(src.dataset), "available": False, "hours": []}
    ds = radar.open_store(code, settings.site_code)
    if ds is None or not ds.sizes.get("time"):
        return out
    t = ds["time"].values
    span_h = float((t.max() - t.min()) / np.timedelta64(1, "h")) + radar.STEP_MIN / 60
    return out | {"available": True, "images": int(t.size), "first": str(t.min())[:16], "last": str(t.max())[:16],
                  "span_hours": round(span_h, 2), "hours": [h for h in HOURS if h <= max(1, span_h + 0.01)] or [1]}


@router.get("/info")
def info(source: str | None = None) -> dict:
    """The radar sources of the site (or one): what is stored, mode, what is missing."""
    if source:
        _source(source)
        return _source_info(source)
    return {"site": settings.site_code, "sources": [_source_info(c) for c in radar.SOURCES],
            "intensity": radar.intensity_legend()}


@router.get("/series")
def series(area: str | None = None, source: str = "opera") -> dict:
    """Rain over an area, image by image (mm in 5 minutes: mean over the area, maximum, share of the
    area where it rains), and the totals of the last 1, 3, 6, 12 and 24 hours (mean over the area)."""
    ds = _store(source)
    site = settings.site_code
    area = area or site
    zones = s2.site_zones(ds, site)
    idx = zones.get(area)
    if idx is None or not idx.size:
        raise HTTPException(404, f"No radar cell in area {area}")
    raw = ds["rain"].values.reshape(ds.sizes["time"], -1)[:, idx]
    mm = radar.rain_mm(raw)
    valid = (raw != radar.NODATA).mean(axis=1)
    points = [{"time": str(t)[:16], "mean": round(float(m.mean()), 3), "max": round(float(m.max()), 2),
               "wet_share": round(float((m >= 0.01).mean()), 3), "valid": round(float(v), 3)}
              for t, m, v in zip(ds["time"].values, mm, valid)]
    end = ds["time"].values.max()
    totals = []
    for h in HOURS:
        sel = ds["time"].values > end - np.timedelta64(h, "h")
        tot = mm[sel].sum(axis=0) if sel.any() else np.zeros(idx.size)
        totals.append({"hours": h, "mean_mm": round(float(tot.mean()), 2), "max_mm": round(float(tot.max()), 2),
                       "images": int(sel.sum()), "expected": h * 60 // radar.STEP_MIN})
    return {"site": site, "area": area, "source": source, "cells": int(idx.size),
            "cell_km": radar.SOURCES[source].resolution_m / 1000, "points": points, "totals": totals}


# ---------------------------------------------------------------------------
# Animation frames: rain intensity of each image
# ---------------------------------------------------------------------------
@router.get("/frames")
def frames(source: str = "opera", hours: float = 3) -> dict:
    """The images of the last ``hours`` (at most MAX_FRAMES, evenly thinned) as map frames: one URL per
    image, the same corners for all."""
    ds = _store(source)
    t = ds["time"].values
    sel = np.nonzero(t > t.max() - np.timedelta64(int(hours * 60), "m"))[0]
    if sel.size > MAX_FRAMES:
        sel = sel[np.unique(np.round(np.linspace(0, sel.size - 1, MAX_FRAMES)).astype(int))]
    site = settings.site_code
    _, coords = _frame(site, source, str(t[sel[-1]])[:16])
    return {"source": source, "attribution": radar.SOURCES[source].attribution, "coordinates": coords,
            "frames": [{"time": str(t[i])[:16], "url": f"/api/radar/frame.png?source={source}&time={str(t[i])[:16]}&site={site}"}
                       for i in sel],
            "intensity": radar.intensity_legend()}


@lru_cache(maxsize=400)
def _frame_cached(site: str, source: str, time: str) -> tuple[bytes, list]:
    ds = radar.open_store(source, site)
    i = radar.time_index(ds["time"].values, time)
    if i is None:
        raise HTTPException(404, f"No radar image at {time}")
    rate = radar.rate_mmh(ds["rain"].values[i])
    return grid_rgba_png(rate, ds["x"].values, ds["y"].values, ds.attrs["crs"], radar.intensity_rgba)


_GRIDS: dict = {}


def _frame(site: str, source: str, time: str) -> tuple[bytes, list]:
    """A frame (cached: an image never changes once stored; the cache is cleared when the store's grid
    changes, e.g. after the site's extent changed)."""
    ds = radar.open_store(source, site)
    if ds is None:
        raise HTTPException(404, "No radar image")
    key = (ds.sizes["x"], ds.sizes["y"], float(ds["x"].values[0]), float(ds["y"].values[0]))
    if _GRIDS.get((site, source)) != key:
        _GRIDS[(site, source)] = key
        _frame_cached.cache_clear()
    return _frame_cached(site, source, time)


@router.get("/frame.png")
def frame_png(time: str, source: str = "opera") -> Response:
    _store(source)
    png = _frame(settings.site_code, source, time[:16])[0]
    return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=86400"})


# ---------------------------------------------------------------------------
# Accumulations
# ---------------------------------------------------------------------------
@lru_cache(maxsize=24)
def _render(site: str, source: str, hours: float, stamp: float) -> tuple[bytes, list, dict, float]:
    ds = radar.open_store(source, site)
    total, cov = radar.accumulation(ds, hours)
    vmax = radar.VMAX.get(int(hours), 20.0)
    shown = np.where(total >= 0.1, total, np.nan).astype("float32")
    png, coords = grid_png(shown, ds["x"].values, ds["y"].values, ds.attrs["crs"], 0.0, vmax, radar.COLORMAP)
    return png, coords, cov | {"max_mm": round(float(total.max()), 2),
                               "wet_share": round(float((total >= 0.1).mean()), 3)}, vmax


@router.get("/accum/info")
def accum_info(hours: float = 1, source: str = "opera") -> dict:
    """The rain of the last ``hours`` as an image for the map (transparent where dry), with its legend
    and coverage (images present / expected)."""
    _store(source)
    if not 5 / 60 <= hours <= 24 * 31:
        raise HTTPException(422, "hours: 5 minutes to 31 days")
    _, coords, cov, vmax = _render(settings.site_code, source, hours, _stamp(source))
    return {"hours": hours, "source": source, "coordinates": coords, **cov, "vmin": 0.0, "vmax": vmax, "units": "mm",
            "legend": legend(0.0, vmax, radar.COLORMAP),
            "url": f"/api/radar/accum.png?hours={hours}&source={source}&site={settings.site_code}&t={int(_stamp(source))}"}


@router.get("/accum.png")
def accum_png(hours: float = 1, source: str = "opera") -> Response:
    _store(source)
    png = _render(settings.site_code, source, hours, _stamp(source))[0]
    return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=300"})
