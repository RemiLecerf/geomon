"""Nowcast (AROME-PI, the next 6 hours): rain frames for the radar animation, and rain, temperature
and gusts over an area (see geomon.nowcast)."""

from __future__ import annotations

from functools import lru_cache

import numpy as np
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from .. import datasets, nowcast as nc, radar
from .. import sentinel2 as s2
from ..arrays import finite_round as _r
from ..arrays import quiet
from ..config import settings
from .sentinel2 import grid_rgba_png

router = APIRouter(prefix="/api/nowcast", tags=["nowcast"])


def _store():
    ds = nc.open_store(settings.site_code)
    if ds is None:
        raise HTTPException(404, datasets.missing(nc.DATASET, "nowcast"))
    return ds


@router.get("/info")
def info() -> dict:
    pol = datasets.policy(settings.site_code, nc.DATASET)
    out = {"label": "Météo-France AROME-PI nowcast, next 6 hours", "attribution": nc.ATTRIBUTION,
           "mode": pol["mode"], "needs": datasets.needs(nc.DATASET), "available": False}
    ds = nc.open_store(settings.site_code)
    if ds is None:
        return out
    t = ds["time"].values
    return out | {"available": True, "run": ds.attrs["run"][:16], "first": str(t[0])[:16], "last": str(t[-1])[:16],
                  "updated": ds.attrs.get("updated"), "step_min": nc.STEP_MIN}


@router.get("/frames")
def frames(after: str | None = None) -> dict:
    """The rain frames of the run (after ``after``, e.g. the latest radar image), with their corners."""
    ds = _store()
    t = ds["time"].values
    sel = np.arange(t.size) if not after else np.nonzero(t > np.datetime64(after[:16], "m"))[0]
    site = settings.site_code
    coords = _frame(site, ds.attrs["run"], str(t[-1])[:16])[1]
    return {"run": ds.attrs["run"][:16], "attribution": nc.ATTRIBUTION, "coordinates": coords,
            "frames": [{"time": str(t[i])[:16], "forecast": True,
                        "url": f"/api/nowcast/frame.png?time={str(t[i])[:16]}&run={ds.attrs['run'][:16]}&site={site}"}
                       for i in sel]}


@lru_cache(maxsize=64)
def _frame(site: str, run: str, time: str) -> tuple[bytes, list]:
    ds = nc.open_store(site)
    i = radar.time_index(ds["time"].values, time)
    if i is None:
        raise HTTPException(404, f"No nowcast frame at {time}")
    rate = ds["rain"].values[i] * 60 / nc.STEP_MIN          # mm/h, the radar's scale
    return grid_rgba_png(rate, ds["x"].values, ds["y"].values, ds.attrs["crs"], radar.intensity_rgba)


@router.get("/frame.png")
def frame_png(time: str) -> Response:          # the URL's run= only makes it change with each run
    ds = _store()
    png = _frame(settings.site_code, ds.attrs["run"], time[:16])[0]
    return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=3600"})


@router.get("/series")
def series(area: str | None = None) -> dict:
    """Over an area, for the next 6 hours: rain in each 15 minutes (mean, wettest cell, share of the
    area where it rains), and each hour the temperature (mean, min, max) and the maximum gust."""
    ds = _store()
    site = settings.site_code
    area = area or site
    idx = s2.site_zones(ds, site).get(area)
    if idx is None:
        raise HTTPException(404, f"Unknown area {area}")
    rain = ds["rain"].values.reshape(ds.sizes["time"], -1)[:, idx]
    t2m = ds["t2m"].values.reshape(ds.sizes["time_t"], -1)[:, idx]
    gust = ds["gust"].values.reshape(ds.sizes["time_g"], -1)[:, idx]
    with quiet():
        rain_pts = [{"time": str(t)[:16], "mean": _r(np.nanmean(m)), "max": _r(np.nanmax(m)),
                     "wet_share": _r(np.mean(m >= 0.05), 3)} for t, m in zip(ds["time"].values, rain)]
        temp_pts = [{"time": str(t)[:16], "mean": _r(np.nanmean(m), 1), "min": _r(np.nanmin(m), 1), "max": _r(np.nanmax(m), 1)}
                    for t, m in zip(ds["time_t"].values, t2m)]
        gust_pts = [{"time": str(t)[:16], "mean": _r(np.nanmean(m) * 3.6, 0), "max": _r(np.nanmax(m) * 3.6, 0)}
                    for t, m in zip(ds["time_g"].values, gust)]
        total = np.nansum(rain, axis=0)
    return {"site": site, "area": area, "run": ds.attrs["run"][:16], "cells": int(idx.size), "attribution": nc.ATTRIBUTION,
            "rain": rain_pts, "temperature": temp_pts, "gust_kmh": gust_pts,
            "total": {"mean_mm": _r(total.mean(), 1), "max_mm": _r(total.max(), 1)}}
