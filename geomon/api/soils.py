"""Soil and geology maps (legends, what lies at a point) and groundwater levels (Hub'Eau)."""

from __future__ import annotations

import datetime as dt

import numpy as np
from fastapi import APIRouter, HTTPException

from ..config import settings
from ..db import connect

router = APIRouter(tags=["soils"])

MAPS = {
    "soils": {"title": "Dominant soils", "source": "INRAE / Gis Sol, Référentiels Régionaux Pédologiques 1:250 000 (harmonised map)"},
    "geology": {"title": "Geology 1:50 000", "source": "BRGM, BD Charm-50 harmonised geological map"},
    "clay": {"title": "Clay shrink-swell exposure", "source": "BRGM / Géorisques; elsewhere: a priori not clayey"},
}


@router.get("/api/soil/maps")
def soil_maps() -> dict:
    """The maps of the site and the classes present in its outline (area in ha, the largest first)."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT s.layer, s.code, s.label, s.color, min(s.detail) AS detail,
                      sum(ST_Area(ST_Intersection(s.geom, a.geom)::geography)) / 1e4 AS ha
               FROM ref.soil_feature s JOIN meta.aoi a ON a.kind = 'site' AND a.code = %s AND ST_Intersects(s.geom, a.geom)
               WHERE s.site = %s GROUP BY 1, 2, 3, 4 ORDER BY 1, ha DESC""",
            (settings.site_code, settings.site_code)).fetchall()
    out = []
    for layer, meta in MAPS.items():
        cls = [{"code": r["code"], "label": r["label"], "color": r["color"], "ha": round(r["ha"] or 0, 1),
                "detail": None}
               for r in rows if r["layer"] == layer]
        if layer == "soils":            # one entry per dominant soil (the units differ by their share)
            merged: dict[str, dict] = {}
            for c in cls:
                key = c["code"] or c["label"]
                e = merged.setdefault(key, {**c, "label": (c["code"] or c["label"]).capitalize(), "ha": 0.0})
                e["ha"] = round(e["ha"] + c["ha"], 1)
            cls = sorted(merged.values(), key=lambda c: -c["ha"])
        out.append({"layer": layer, **meta, "available": bool(cls), "classes": cls})
    return {"site": settings.site_code, "maps": out}


@router.get("/api/soil/at")
def soil_at(lon: float, lat: float) -> dict:
    """The soil unit, the geological formation and the clay exposure at a point."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT layer, code, label, detail, color, props FROM ref.soil_feature
               WHERE site = %s AND ST_Intersects(geom, ST_SetSRID(ST_MakePoint(%s, %s), 4326))""",
            (settings.site_code, lon, lat)).fetchall()
        loaded = {r["layer"] for r in conn.execute(
            "SELECT DISTINCT layer FROM ref.soil_feature WHERE site = %s", (settings.site_code,)).fetchall()}
    out = {}
    for r in rows:
        e = {"code": r["code"], "label": r["label"], "detail": r["detail"], "color": r["color"]}
        if r["layer"] == "soils":
            e["share"] = r["props"].get("pourcent_sol_dominant")
            e["unit"] = r["props"].get("no_ucs")
        out[r["layer"]] = e
    if "clay" in loaded and "clay" not in out:
        out["clay"] = {"code": "0", "label": "A priori not clayey", "detail": None, "color": None}
    return {"point": [lon, lat], **out}


# ---------------------------------------------------------------------------
# Groundwater
# ---------------------------------------------------------------------------
@router.get("/api/groundwater/stations")
def gw_stations() -> dict:
    """The piezometers followed for the site, the nearest first, with their latest level."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT s.code_bss, s.name, s.commune, s.lon, s.lat, s.altitude_m, s.depth_m, s.aquifers, g.distance_km,
                      l.date AS last_date, l.depth_m AS last_depth, l.level_ngf AS last_level,
                      (SELECT min(date) FROM eo.gw_level WHERE code_bss = s.code_bss) AS first_date,
                      (SELECT count(*) FROM eo.gw_level WHERE code_bss = s.code_bss) AS n
               FROM ref.gw_site g JOIN ref.gw_station s USING (code_bss)
               LEFT JOIN LATERAL (SELECT date, depth_m, level_ngf FROM eo.gw_level WHERE code_bss = s.code_bss
                                  ORDER BY date DESC LIMIT 1) l ON true
               WHERE g.site = %s ORDER BY g.distance_km""", (settings.site_code,)).fetchall()
    return {"site": settings.site_code, "source": "Hub'Eau, niveaux des nappes (ADES)",
            "stations": [{k: (v.isoformat() if isinstance(v, dt.date) else v) for k, v in r.items()} for r in rows]}


def clean_levels(depth: np.ndarray) -> np.ndarray:
    """The plausible daily depths: not at or above the ground (0 m: a recording error) and not a spike,
    i.e. within max(2 m, 6 median absolute deviations) of the median of the 31 days around."""
    import pandas as pd

    s = pd.Series(depth)
    med = s.rolling(31, center=True, min_periods=5).median()
    mad = (s - med).abs().rolling(31, center=True, min_periods=5).median()
    spike = (s - med).abs() > np.maximum(2.0, 6 * mad.fillna(0))
    return ((s > 0) & ~spike.fillna(False)).to_numpy()


@router.get("/api/groundwater/series")
def gw_series(code_bss: str, years: float = 2) -> dict:
    """Daily water-table depth of a station over the last ``years`` (``0``: the whole record), and its
    normal for each day of the year over the seasons before the last 12 months (10th, 50th, 90th
    percentiles, minimum, maximum): where the water stands against the usual."""
    with connect() as conn:
        rows = conn.execute("SELECT date, depth_m FROM eo.gw_level WHERE code_bss = %s AND depth_m IS NOT NULL ORDER BY date",
                            (code_bss,)).fetchall()
        st = conn.execute("SELECT name, commune, altitude_m, aquifers FROM ref.gw_station WHERE code_bss = %s",
                          (code_bss,)).fetchone()
    if not rows:
        raise HTTPException(404, f"No level for station {code_bss}")
    dates = np.array([r["date"] for r in rows], dtype="datetime64[D]")
    depth = np.array([r["depth_m"] for r in rows], dtype="float64")
    good = clean_levels(depth)
    removed = int((~good).sum())
    dates, depth = dates[good], depth[good]
    last = dates.max()
    ref = dates <= last - np.timedelta64(365, "D")
    doy = np.array([d.astype(object).timetuple().tm_yday for d in dates])
    normal = {}
    if ref.sum() >= 365 * 3:
        for k in ("p10", "p50", "p90", "min", "max"):
            normal[k] = [None] * 366
        for day in range(1, 367):
            win = ref & (np.abs(((doy - day + 183) % 366) - 183) <= 7)          # +/- 7 days
            v = depth[win]
            if v.size >= 20:
                p10, p50, p90 = np.percentile(v, [10, 50, 90])
                for k, x in (("p10", p10), ("p50", p50), ("p90", p90), ("min", v.min()), ("max", v.max())):
                    normal[k][day - 1] = round(float(x), 3)
    sel = dates >= (last - np.timedelta64(int(years * 365.25), "D")) if years else np.ones(dates.size, bool)
    current = None
    if normal and normal["p50"][doy[-1] - 1] is not None:
        win = ref & (np.abs(((doy - doy[-1] + 183) % 366) - 183) <= 7)
        current = {"date": str(last), "depth_m": round(float(depth[-1]), 3),
                   "median_m": normal["p50"][doy[-1] - 1],
                   # share of the past days of the same season when the water was deeper than now
                   "higher_than_pct": round(100 * float((depth[win] > depth[-1]).mean()), 0),
                   "years": int(len({d.astype(object).year for d in dates[ref]}))}
    return {"code_bss": code_bss, "station": st, "first": str(dates.min()), "last": str(last), "removed": removed,
            "points": [[str(d), round(float(v), 3)] for d, v in zip(dates[sel], depth[sel])],
            "normal": normal or None, "current": current}
