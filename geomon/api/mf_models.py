"""Météo-France forecast models: AROME maps (rain, temperature, gusts, hour by hour), PE-AROME
probabilities and, for a coastal site, waves and storm surge (see geomon.mf_models)."""

from __future__ import annotations

import json
from functools import lru_cache

import numpy as np
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from .. import datasets, mf_models as m, radar
from ..arrays import finite_list
from ..config import settings
from .sentinel2 import grid_png, grid_rgba_png, legend

router = APIRouter(prefix="/api/mf", tags=["meteofrance"])

AROME_VARS = {
    "rain": {"label": "Rain (mm/h)", "units": "mm/h"},
    "t2m": {"label": "Temperature at 2 m (°C)", "units": "°C", "cmap": "RdYlBu_r"},
    "gust": {"label": "Gusts at 10 m (km/h)", "units": "km/h", "cmap": "YlOrRd", "scale": 3.6},
}


def _arome():
    ds = m.open_zarr(m.arome_path(settings.site_code))
    if ds is None:
        raise HTTPException(404, datasets.missing("arome", "AROME forecast"))
    return ds


@lru_cache(maxsize=16)
def _range(site: str, run: str, var: str) -> tuple[float, float]:
    """Fixed colour range of a variable over the whole run (so the animation compares hours)."""
    a = m.open_zarr(m.arome_path(site))[var].values * AROME_VARS[var].get("scale", 1.0)
    if var == "t2m":
        lo, hi = np.floor(np.nanpercentile(a, 1)), np.ceil(np.nanpercentile(a, 99))
        return float(lo), float(max(hi, lo + 4))
    return 0.0, float(max(40.0, np.ceil(np.nanpercentile(a, 99.5) / 10) * 10))


@router.get("/arome/info")
def arome_info() -> dict:
    ds = m.open_zarr(m.arome_path(settings.site_code))
    out = {"available": ds is not None, "needs": datasets.needs("arome"), "attribution": m.AROME_ATTRIBUTION,
           "variables": [{"name": k, "label": v["label"]} for k, v in AROME_VARS.items()]}
    if ds is None:
        return out
    return out | {"run": ds.attrs["run"][:16], "updated": ds.attrs.get("updated"),
                  "times": [str(t)[:16] for t in ds["time"].values]}


@router.get("/arome/frames")
def arome_frames(var: str = "rain") -> dict:
    if var not in AROME_VARS:
        raise HTTPException(404, f"Unknown variable {var}")
    ds = _arome()
    site, run = settings.site_code, ds.attrs["run"][:16]
    coords = _arome_frame(site, run, var, str(ds["time"].values[0])[:16])[1]
    out = {"var": var, "label": AROME_VARS[var]["label"], "run": run, "attribution": m.AROME_ATTRIBUTION,
           "margin_km": m.AROME_MARGIN_KM,
           "coordinates": coords,
           "frames": [{"time": str(t)[:16], "url": f"/api/mf/arome/frame.png?var={var}&time={str(t)[:16]}&run={run}&site={site}"}
                      for t in ds["time"].values]}
    if var == "rain":
        out["intensity"] = radar.intensity_legend()
    else:
        vmin, vmax = _range(site, run, var)
        out |= {"vmin": vmin, "vmax": vmax, "legend": legend(vmin, vmax, AROME_VARS[var]["cmap"])}
    return out


@lru_cache(maxsize=256)
def _arome_frame(site: str, run: str, var: str, time: str) -> tuple[bytes, list]:
    ds = m.open_zarr(m.arome_path(site))
    i = radar.time_index(ds["time"].values, time)
    if i is None:
        raise HTTPException(404, f"No AROME hour at {time}")
    a = ds[var].values[i] * AROME_VARS[var].get("scale", 1.0)
    if var == "rain":
        return grid_rgba_png(a, ds["x"].values, ds["y"].values, ds.attrs["crs"], radar.intensity_rgba)
    vmin, vmax = _range(site, run, var)
    return grid_png(a.astype("float32"), ds["x"].values, ds["y"].values, ds.attrs["crs"], vmin, vmax, AROME_VARS[var]["cmap"])


@router.get("/arome/frame.png")
def arome_frame_png(var: str, time: str) -> Response:     # the URL's run= only makes it change with each run
    ds = _arome()
    png = _arome_frame(settings.site_code, ds.attrs["run"][:16], var, time[:16])[0]
    return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=3600"})


@router.get("/ensemble")
def ensemble() -> dict:
    """PE-AROME: probabilities of rain in the next two 24-hour windows and of frost on the next
    mornings over the site (area mean, and the wettest / coldest cell)."""
    e = m.open_ensemble(settings.site_code)
    if e is None:
        raise HTTPException(404, datasets.missing("ensemble", "PE-AROME ensemble"))
    return e


@router.get("/sea")
def sea() -> dict:
    """Waves and storm surge at the sea cells nearest the site (coastal sites only)."""
    if m.coastal(settings.site_code) is False:
        return {"available": False, "coastal": False, "max_km": m.SEA_MAX_KM}
    ds = m.open_zarr(m.sea_path(settings.site_code))
    if ds is None:
        return {"available": False, "coastal": None, "needs": datasets.needs("sea")}
    clean = finite_list
    return {"available": True, "coastal": True, "max_km": m.SEA_MAX_KM, "source": ds.attrs.get("source"), "updated": ds.attrs.get("updated"),
            "waves_run": ds.attrs.get("waves_run", "")[:16], "surge_run": ds.attrs.get("surge_run", "")[:16],
            "wave_cells": json.loads(ds.attrs.get("wave_cells", "[]")), "surge_cells": json.loads(ds.attrs.get("surge_cells", "[]")),
            "waves": {"times": [str(t)[:16] for t in ds["time"].values],
                      **{k: clean(ds[k].values) for k in m.WAVES}},
            "surge": {"times": [str(t)[:16] for t in ds["time_s"].values], "values": clean(ds["surge"].values)},
            "units": {k: v[1] for k, v in m.WAVES.items()} | {"surge": "m"}}
