"""Météo-France, now: observations of the last hours at the nearest stations, the Vigilance and
Météo des forêts for the site's départements."""

from __future__ import annotations

from functools import lru_cache

import numpy as np
from fastapi import APIRouter, HTTPException

from .. import alerts, datasets
from ..aoi import departement_of
from ..arrays import finite_list, nan_agg
from ..config import settings
from ..db import connect
from ..ingest import mf_obs

router = APIRouter(prefix="/api/mf", tags=["meteofrance"])


@lru_cache(maxsize=16)
def _departements(site: str) -> tuple[str, ...]:
    with connect() as conn:
        rows = conn.execute("SELECT code FROM meta.aoi WHERE site = %s AND kind = 'commune'", (site,)).fetchall()
    return tuple(sorted({departement_of(r["code"]) for r in rows}))


@router.get("/obs")
def observations(hours: int = 48) -> dict:
    """The hourly observations of the stations near the site over the last ``hours`` (UTC times):
    per station, the series and a summary of the last 24 hours."""
    ds = mf_obs.open_store(settings.site_code)
    pol = datasets.policy(settings.site_code, mf_obs.DATASET)
    if ds is None:
        raise HTTPException(404, datasets.missing(mf_obs.DATASET, "observation"))
    t = ds["time"].values
    sel = t > t.max() - np.timedelta64(int(hours), "h")
    last24 = t > t.max() - np.timedelta64(24, "h")
    stations = []
    for i in range(ds.sizes["point"]):
        series = {v: finite_list(ds[v].values[sel, i]) for v in ds.data_vars}
        series = {v: s for v, s in series.items() if any(x is not None for x in s)}
        latest = {}
        for v in ds.data_vars:
            col = ds[v].values[:, i]
            ok = np.nonzero(np.isfinite(col))[0]
            if ok.size:
                latest[v] = {"value": round(float(col[ok[-1]]), 2), "time": str(t[ok[-1]])[:16]}
        def day(v, i=i):           # the last 24 hours of a variable at this station
            return ds[v].values[last24, i] if v in ds else np.array([np.nan])

        d24 = {"precip": nan_agg(np.nansum, day("precip")), "precip_hours": int(np.isfinite(day("precip")).sum()),
               "t_min": nan_agg(np.nanmin, day("t_min") if "t_min" in ds else day("t")),
               "t_max": nan_agg(np.nanmax, day("t_max") if "t_max" in ds else day("t")),
               "gust_max": nan_agg(np.nanmax, day("gust"))}
        stations.append({"id": str(ds["station"].values[i]), "name": str(ds["station_name"].values[i]),
                         "distance_km": float(ds["distance_km"].values[i]),
                         "lon": float(ds["lon"].values[i]), "lat": float(ds["lat"].values[i]),
                         "latest": latest, "last24h": d24, "series": series})
    return {"site": settings.site_code, "mode": pol["mode"], "keep_hours": pol["keep_hours"], "radius_km": mf_obs.RADIUS_KM,
            "updated": ds.attrs.get("updated"), "source": ds.attrs.get("source"),
            "times": [str(x)[:16] for x in t[sel]],
            "units": {v: ds[v].attrs.get("units") for v in ds.data_vars}, "stations": stations}


@router.get("/vigilance")
def vigilance() -> dict:
    """The Vigilance of the site's départements (today and tomorrow) and Météo des forêts."""
    deps = list(_departements(settings.site_code))
    v = alerts.vigilance_for(deps)
    return {"departements": deps, "vigilance": v,
            "vigilance_needs": None if v else (datasets.needs("vigilance") or "the scheduler fetches it every 15 minutes"),
            "forets": alerts.forets_for(deps),
            "phenomena": alerts.PHENOMENA,
            "fire_levels": [{"level": k, "name": n, "hex": h} for k, (n, h) in alerts.FIRE_LEVELS.items()]}
