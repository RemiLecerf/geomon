"""Weather endpoints: product catalogue, grid cells, daily series at a point or over an area."""

from __future__ import annotations

import json
import math

import pandas as pd
from fastapi import APIRouter, HTTPException

from .. import normals as nm
from .. import weather as w
from ..config import settings
from .areas import get_area as _area

router = APIRouter(prefix="/api/weather", tags=["weather"])


def _area_info(region: dict | None) -> dict | None:
    return {k: region[k] for k in ("code", "name", "kind")} if region else None


def _series(key: str, lon: float | None, lat: float | None, region: dict | None, variables: list[str]) -> dict:
    """A display series at a point, or averaged over ``region`` when given."""
    return (w.series_daily(key, lon, lat, variables) if region is None
            else w.series_daily_area(key, region["geometry"], variables))


def _clean(values) -> list:
    return [None if v is None or (isinstance(v, float) and math.isnan(v)) else round(float(v), 3)
            for v in values]


@router.get("/products")
def products() -> dict:
    out = []
    for name, p in w.PRODUCTS.items():
        ds = w.open_product(name)
        item = {"name": name, "label": p.label, "source": p.source, "step": p.step,
                "resolution": p.resolution, "forecast": p.forecast, "available": ds is not None}
        if ds is not None:
            item |= {
                "start": str(ds.time.values[0])[:16], "end": str(ds.time.values[-1])[:16],
                "points": int(ds.sizes["point"]), "updated": ds.attrs.get("updated"),
                "run": ds.attrs.get("run"),
                "hourly_variables": [
                    {"name": v, "units": ds[v].attrs.get("units"), "time_reference": ds[v].attrs.get("time_reference")}
                    for v in ds.data_vars] if p.step == "hour" else None,
                "daily_variables": w.daily_variables(name, ds),
            }
        out.append(item)
    series = []
    for key, s in w.SERIES.items():
        names = [s["history"]] + ([s["forecast"]] if s["forecast"] else [])
        vars_ = set()
        for n in names:
            vars_ |= set(w.daily_variables(n))
        base = w.base_product(key)
        hist = w.open_product(s["history"])
        series.append({"key": key, "label": s["label"], "history": s["history"], "forecast": s["forecast"],
                       "available": base is not None, "product": base,
                       "start": str(hist.time.values[0])[:10] if hist is not None else None,
                       "variables": [v for v in w.DAILY if v in vars_]})
    return {
        "products": out,
        "series": series,
        "variables": [{"name": v.name, "long_name": v.long_name, "units": v.units, "how": v.how,
                       "agera5": v.agera5} for v in w.DAILY_VARS],
        "local_time_zone": w.LOCAL_TZ,
    }


@router.get("/cells.geojson")
def cells(product: str) -> dict:
    """Grid cells of a product, as polygons, for the map."""
    if product not in w.PRODUCTS:
        raise HTTPException(404, f"Unknown product {product}")
    ds = w.open_product(product)
    if ds is None:
        return {"type": "FeatureCollection", "features": []}
    feats = []
    for i, poly in enumerate(w.cell_polygons(product)):
        ring = [list(map(float, p)) for p in poly.exterior.coords]
        feats.append({"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]},
                      "properties": {"product": product, "point": i,
                                     "lon": float(ds["lon"][i]), "lat": float(ds["lat"][i])}})
    return {"type": "FeatureCollection", "features": feats}


@router.get("/daily")
def daily(var: str, lon: float | None = None, lat: float | None = None, area: str | None = None,
          series: str = "mf,era5land,era5,agera5,sim2",
          start: str | None = None, end: str | None = None, step: str = "day") -> dict:
    """Daily (or weekly/monthly) values of one variable, for several series.

    At a point (``lon``, ``lat``: the nearest grid point of each product), or
    averaged over an area (``area``: a commune code or the group code; the
    group when neither is given), each grid cell weighted by its overlap.

    ``series``: comma-separated keys among mf (Météo-France history + forecast),
    era5land, era5, agera5 and sim2. Weekly and monthly values are sums for accumulations and
    means otherwise; incomplete periods of sums are left empty.
    """
    if var not in w.DAILY:
        raise HTTPException(404, f"Unknown variable {var}")
    if step not in ("day", "week", "month"):
        raise HTTPException(400, "step must be day, week or month")
    meta = w.DAILY[var]
    region = None if lon is not None and lat is not None else _area(area)
    out = []
    for key in [s for s in series.split(",") if s]:
        if key not in w.SERIES:
            raise HTTPException(404, f"Unknown series {key}")
        r = _series(key, lon, lat, region, [var])
        item = {"key": key, "label": w.SERIES[key]["label"], "available": r.get("available", False)}
        if not item["available"]:
            out.append(item)
            continue
        table: pd.DataFrame = r["table"]
        if var not in table:
            item["available"] = False
            out.append(item)
            continue
        s = table[[var]]
        if start:
            s = s[s.index >= pd.Timestamp(start)]
        if end:
            s = s[s.index <= pd.Timestamp(end)]
        s = w.resample(s, step)[var]
        item |= {
            "point": r["point"], "cells": r.get("cells"),
            "forecast_from": r["forecast_from"], "forecast_run": r.get("forecast_run"),
            "updated": r["updated"],
            "dates": [d.strftime("%Y-%m-%d") for d in s.index],
            "values": _clean(s.values),
        }
        out.append(item)
    return {"variable": {"name": meta.name, "long_name": meta.long_name, "units": meta.units,
                         "how": meta.how, "agera5": meta.agera5},
            "step": step, "lon": lon, "lat": lat,
            "area": _area_info(region), "series": out}


# Normals of the series for the current (site, place, variable, start day): they do not depend
# on the year compared. Kept until the products change.
_REFS: dict[tuple, dict] = {}


@router.get("/normals")
def normals(var: str, series: str = "sim2", lon: float | None = None, lat: float | None = None,
            area: str | None = None, start: str | None = None, forecast: bool = True) -> dict:
    """One year of a variable against its 1991-2020 normal (WMO reference period).

    The normal comes from the same series; it needs one covering the reference period (SIM2,
    kept from 1990; AgERA5, from 1979). ``start``: first day of the year compared (default
    1 January of the current year). Averaged variables are compared day by day; summed ones as
    totals since ``start``. See :mod:`geomon.normals`.

    ``forecast``: continue the observations with the Météo-France series (archived forecasts
    up to today, which bridges a source that lags behind, then the latest forecast: AROME/ARPEGE,
    then ECMWF IFS to 15 days). It is a different model: its bias against the source of the
    normal is not corrected.
    """
    if var not in w.DAILY:
        raise HTTPException(404, f"Unknown variable {var}")
    if series not in w.SERIES:
        raise HTTPException(404, f"Unknown series {series}")
    meta = w.DAILY[var]
    today = pd.Timestamp.now(tz=w.LOCAL_TZ).date()
    try:
        first = pd.Timestamp(start).date() if start else today.replace(month=1, day=1)
    except ValueError:
        raise HTTPException(400, "start must be a date (YYYY-MM-DD)")
    region = None if lon is not None and lat is not None else _area(area)
    r = _series(series, lon, lat, region, [var])
    base = {"variable": {"name": meta.name, "long_name": meta.long_name, "units": meta.units, "how": meta.how},
            "series": {"key": series, "label": w.SERIES[series]["label"], "updated": r.get("updated")},
            "point": r.get("point"), "cells": len(r.get("cells") or []) or None, "area": _area_info(region)}
    if not r.get("available") or var not in r["table"]:
        return base | {"available": False, "reason": f"{w.SERIES[series]['label']} does not provide this variable here"}
    obs = r["table"][var]

    spec = w.SERIES[series]
    key = (settings.site_code, series, region["code"] if region else (lon, lat), var,
           first.strftime("%m-%d"), nm.period_days(first), w._stamp(spec["history"], spec["forecast"]))
    if key not in _REFS:
        if len(_REFS) > 64:
            _REFS.clear()
        _REFS[key] = nm.reference(obs, meta.how, first)

    ahead, ahead_from, fc_info = None, None, {}
    if forecast and first <= today < nm.period_start(first, first.year + 1):
        f = _series("mf", lon, lat, region, [var])
        if f.get("available") and var in f["table"]:
            ahead = f["table"][var]
            ahead_from = pd.Timestamp(f["forecast_from"]).date() if f.get("forecast_from") else None
            fc_info = {"label": w.SERIES["mf"]["label"], "run": f.get("forecast_run"), "forecast_from": f.get("forecast_from")}
    res = nm.compare(obs, meta.how, first, ahead, ahead_from, ref=_REFS[key])
    if not res["available"]:
        return base | res
    for part in (res, res.get("ahead")):
        for k in ("daily", "cumulative"):
            if part and k in part:
                part[k] = {n: _clean(v) for n, v in part[k].items()} if isinstance(part[k], dict) else _clean(part[k])
    if "ahead" in res:
        res["ahead"] |= fc_info
    res["summary"] = [{k: (round(v, 3) if isinstance(v, float) else v) for k, v in row.items()} for row in res["summary"]]
    return base | res


@router.get("/forecast")
def forecast(lon: float | None = None, lat: float | None = None, area: str | None = None) -> dict:
    """Daily summary of the latest forecast (local days), at a point or averaged over an area."""
    fc = w.open_product("om_forecast")
    if fc is None:
        raise HTTPException(404, "No forecast yet: run the forecast ingestion step")
    wanted = ["t_min", "t_max", "precip", "et0", "radiation", "wind_mean", "rh_mean", "soil_m_0_7"]
    region = None
    if lon is not None and lat is not None:
        i, dist = w.nearest_point(fc, lon, lat)
        d = w.hourly_to_daily(w.point_frame("om_forecast", i), wanted).dropna(how="all")
        point = {"lon": float(fc["lon"][i]), "lat": float(fc["lat"][i]), "distance_km": dist,
                 "elevation": float(fc["elevation"][i])}
    else:
        region = _area(area)
        weights = w.area_weights("om_forecast", region["geometry"])
        parts = [(w.hourly_to_daily(w.point_frame("om_forecast", i), wanted), wt) for i, wt in weights]
        d = sum(t * wt for t, wt in parts).dropna(how="all")
        point = {"cells": len(weights),
                 "elevation": float(sum(float(fc["elevation"][i]) * wt for i, wt in weights))}
    today = pd.Timestamp.now(tz=w.LOCAL_TZ).normalize().tz_localize(None)
    d = d[d.index >= today]
    return {
        "run": fc.attrs.get("run"), "models": json.loads(fc.attrs.get("models", "{}")),
        "point": point,
        "area": _area_info(region),
        "units": {v: w.DAILY[v].units for v in wanted},
        "days": [{"date": idx.strftime("%Y-%m-%d"), **{c: _clean([row[c]])[0] for c in d.columns}}
                 for idx, row in d.iterrows()],
    }


@router.post("/reload")
def reload() -> dict:
    w.clear_cache()
    return {"reloaded": True}

