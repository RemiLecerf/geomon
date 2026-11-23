"""Copernicus Land Monitoring Service products: dry matter productivity per area; soil moisture and
productivity per field (a parcel's series and statistics, every field's as CSV; see geomon.clms).
The soil moisture of the areas is served by the weather API, HR-VPP with the parcel view."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Response

from .. import clms, datasets
from .. import parcels as pc
from .. import sentinel2 as s2
from ..arrays import finite_list, finite_round, quiet
from ..config import settings
from ..db import connect

router = APIRouter(prefix="/api/clms", tags=["clms"])


@router.get("/dmp")
def dmp(area: str | None = None, year: int | None = None) -> dict:
    """Dry matter productivity over an area, every 10 days (kg DM/ha/day, mean of its 300 m cells), for
    ``year`` (default: the latest) against the other years (mean, minimum, maximum), and accumulated
    since 1 January (t DM/ha)."""
    site = settings.site_code
    ds = clms.open_dmp(site)
    if ds is None:
        raise HTTPException(404, datasets.missing("clms_dmp", "dry matter productivity"))
    idx = s2.site_zones(ds, site).get(area or site)
    if idx is None or not idx.size:
        raise HTTPException(404, f"No 300 m cell in area {area}")
    with quiet():
        values = np.nanmean(ds["dmp"].values.reshape(ds.sizes["time"], -1)[:, idx], axis=1)
    dates = [pd.Timestamp(t).date() for t in ds["time"].values]
    years = sorted({d.year for d in dates})
    year = year if year in years else years[-1]
    table = {y: np.full(36, np.nan) for y in years}
    for d, v in zip(dates, values):
        table[d.year][clms.dekad(d)] = v
    days = np.array([clms.dekad_days(dt.date(2001, m, d)) for m in range(1, 13) for d in (1, 11, 21)], dtype="float64")
    # a dekad missing between two others counts as their mean, not as no growth
    cumulative = {y: np.cumsum(np.nan_to_num(pd.Series(a).interpolate(limit_area="inside").to_numpy()) * days) / 1000
                  for y, a in table.items()}
    others = [y for y in years if y != year and np.isfinite(table[y]).sum() >= 30]       # complete years only
    with quiet():
        ref = np.stack([table[y] for y in others]) if others else np.full((1, 36), np.nan)
        cref = np.stack([cumulative[y] for y in others]) if others else np.full((1, 36), np.nan)
        out_ref = {"mean": finite_list(np.nanmean(ref, axis=0), 1), "min": finite_list(np.nanmin(ref, axis=0), 1),
                   "max": finite_list(np.nanmax(ref, axis=0), 1)}
        out_cum = {"mean": finite_list(np.nanmean(cref, axis=0), 2), "min": finite_list(np.nanmin(cref, axis=0), 2),
                   "max": finite_list(np.nanmax(cref, axis=0), 2)}
    last = max(i for i in range(36) if np.isfinite(table[year][i])) if np.isfinite(table[year]).any() else -1
    cum = finite_list(cumulative[year], 2)
    return {"site": site, "area": area or site, "year": year, "years": years, "reference_years": others,
            "cells": int(idx.size), "units": "kg DM/ha/day", "cumulative_units": "t DM/ha",
            "dekads": [f"{m:02d}-{d:02d}" for m in range(1, 13) for d in (1, 11, 21)],
            "values": finite_list(table[year], 1), "reference": out_ref,
            "cumulative": [v if i <= last else None for i, v in enumerate(cum)], "cumulative_reference": out_cum,
            "source": ds.attrs.get("source"), "updated": ds.attrs.get("updated")}


def parcel_vpp(site: str, season: int, rpg_year: int, parcel_id: str) -> dict | None:
    """The HR-VPP season of a parcel (dates, length, PPI peak, productivity), or None."""
    with connect() as conn:
        r = conn.execute("""SELECT * FROM eo.parcel_vpp WHERE site = %s AND year = %s AND rpg_year = %s AND parcel_id = %s""",
                         (site, season, rpg_year, parcel_id)).fetchone()
    if r is None:
        return None
    return {k: (str(v) if isinstance(v, dt.date) else round(float(v), 3) if isinstance(v, float) else v)
            for k, v in r.items() if k not in ("site", "year", "rpg_year", "parcel_id")}


# ---------------------------------------------------------------------------
# Soil moisture and productivity per field (see geomon.clms: the cells under the field, weighted)
# ---------------------------------------------------------------------------
def _period(season: int, start: str | None, end: str | None) -> tuple[dt.date, dt.date]:
    """The period of the statistics: the season's (1 September of the year before to 31 December)
    unless given."""
    try:
        p0 = dt.date.fromisoformat(start) if start else pc.season_start(season)
        p1 = dt.date.fromisoformat(end) if end else pc.season_end(season)
    except ValueError as exc:
        raise HTTPException(400, f"Dates as YYYY-MM-DD: {exc}") from exc
    if p1 < p0 or (p1 - p0).days > 800:
        raise HTTPException(400, "The period must end after it starts, and last at most 800 days")
    return p0, p1


def _fields(site: str, season: int, rpg_year: int | None = None, parcel_id: str | None = None) -> list[dict]:
    """The fields of a season with their outline (shapely): the RPG parcels of its outlines' year
    in the site and the farm's fields of its land cover; or the one given by ``rpg_year`` (0: the
    farm's field) and ``parcel_id``."""
    from shapely import wkb

    from .sentinel2 import _crop_labels

    with connect() as conn:
        crops, groups = _crop_labels(conn)
        gy = pc.geometry_year(season, pc.rpg_years(conn))
        rows = []
        if parcel_id is None or rpg_year != pc.FARM_LAYER:
            rows += conn.execute(
                """SELECT DISTINCT ON (p.parcel_id) p.year AS rpg_year, p.parcel_id, NULL AS field, p.code_cultu,
                          p.code_group, NULL AS crop_label, p.surf_ha, ST_AsBinary(p.geom) AS g
                   FROM ref.rpg_parcel p
                   JOIN meta.aoi a ON a.kind = 'site' AND a.code = %(site)s
                    AND ST_Intersects(ST_PointOnSurface(p.geom), a.geom)
                   WHERE p.year = %(year)s AND (%(id)s::text IS NULL OR p.parcel_id = %(id)s)
                   ORDER BY p.parcel_id, p.surf_ha DESC NULLS LAST""",
                {"site": site, "year": rpg_year if parcel_id else gy, "id": parcel_id}).fetchall()
        if parcel_id is None or rpg_year == pc.FARM_LAYER:
            rows += conn.execute(
                """SELECT DISTINCT ON (field_id) 0 AS rpg_year, field_id AS parcel_id, plot AS field, code_cultu,
                          code_group, crop_label, surf_ha, ST_AsBinary(geom) AS g
                   FROM ref.farm_landcover
                   WHERE site = %(site)s AND field_id IS NOT NULL
                     AND (%(id)s::text IS NULL AND year = %(season)s OR field_id = %(id)s)
                   ORDER BY field_id, year = %(season)s DESC, year DESC""",
                {"site": site, "season": season, "id": parcel_id}).fetchall()
    latest = max((y for y, _ in crops), default=0)          # the farm's codes: the latest labels
    out = []
    for r in rows:
        year = r["rpg_year"] or latest
        out.append({**{k: r[k] for k in ("rpg_year", "parcel_id", "field", "code_cultu", "code_group", "surf_ha")},
                    "crop": r["crop_label"] or crops.get((year, r["code_cultu"])) or groups.get(r["code_group"]),
                    "geom": wkb.loads(bytes(r["g"]))})
    return out


def _stats_of(s: dict, i: int) -> dict:
    """One field's statistics (see geomon.clms.field_stats), rounded."""
    digits = 1
    o = {"mean": finite_round(s["mean"][i], digits), "min": finite_round(s["min"][i], digits),
         "min_date": s["min_date"][i] and str(s["min_date"][i]), "max": finite_round(s["max"][i], digits),
         "max_date": s["max_date"][i] and str(s["max_date"][i]), "n": int(s["n"][i]),
         "reference": finite_round(s["reference"][i], digits),
         "difference": finite_round(s["mean"][i] - s["reference"][i], digits),
         "last": _last(s), "reference_years": s["reference_years"]}
    if "total" in s:
        t, r = s["total"][i], s["reference_total"][i]
        o |= {"total": finite_round(t, 2), "reference_total": finite_round(r, 2),
              "difference_pct": finite_round(100 * (t / r - 1), 0) if np.isfinite(r) and r > 0 else None}
    return o


def _last(s: dict) -> str | None:
    return str(s["dates"][s["published"] - 1].date()) if s["published"] else None


@router.get("/parcel")
def parcel(season: int, rpg_year: int, parcel_id: str, start: str | None = None, end: str | None = None) -> dict:
    """Soil moisture (surface, and the Soil Water Index at five depths) and dry matter productivity
    of a field over a period (default: its season): its series, the mean, lowest and highest of the
    same period in the earlier years, and statistics (see geomon.clms.field_stats). ``rpg_year``
    0: the farm's field ``parcel_id``."""
    site = settings.site_code
    p0, p1 = _period(season, start, end)
    fields = _fields(site, season, rpg_year, parcel_id)
    if not fields:
        raise HTTPException(404, f"No parcel {parcel_id} in RPG {rpg_year}")
    geoms, weights = [fields[0]["geom"]], {}
    out = {"site": site, "season": season, "start": str(p0), "end": str(p1), "rpg_year": rpg_year,
           "parcel_id": parcel_id, "min_share": clms.MIN_SHARE, "soil": None, "dmp": None}
    soil = {v: s for v in clms.SOIL_VARS if (s := clms.field_stats(v, geoms, p0, p1, weights, profiles=True))}
    if soil:
        first = next(iter(soil.values()))
        out["soil"] = {"times": [str(d.date()) for d in first["dates"]], "last": _last(first),
                       "cells": int(first["cells"][0]), "covered": finite_round(first["covered"][0], 2),
                       "reference_years": first["reference_years"], "units": "percent",
                       "series": {v: {"values": finite_list(s["profile"], 1),
                                      "reference": finite_list(s["profile_reference"]["mean"], 1)} for v, s in soil.items()},
                       "stats": {v: _stats_of(s, 0) for v, s in soil.items()}}
    d = clms.field_stats("dmp", geoms, p0, p1, weights, profiles=True)
    if d:
        ref = d["profile_reference"]
        out["dmp"] = {"times": [str(t.date()) for t in d["dates"]], "last": _last(d), "cells": int(d["cells"][0]),
                      "covered": finite_round(d["covered"][0], 2), "reference_years": d["reference_years"],
                      "units": "kg DM/ha/day", "cumulative_units": "t DM/ha",
                      "values": finite_list(d["profile"], 1),
                      "reference": {k: finite_list(ref[k], 1) for k in ("mean", "min", "max")},
                      "cumulative": finite_list(d["cumulative"], 2),
                      "cumulative_reference": finite_list(d["cumulative_reference"], 2), "stats": _stats_of(d, 0)}
    if out["soil"] is None and out["dmp"] is None:
        raise HTTPException(404, datasets.missing("clms_soil", "soil moisture and productivity"))
    return out


@router.get("/parcels.csv")
def parcels_csv(season: int, start: str | None = None, end: str | None = None) -> Response:
    """The same statistics for every field of a season (its RPG parcels and the farm's fields), one
    row each, as CSV."""
    import csv
    import io

    site = settings.site_code
    p0, p1 = _period(season, start, end)
    fields = _fields(site, season)
    if not fields:
        raise HTTPException(404, f"No parcel for the season {season}")
    geoms, weights = [f["geom"] for f in fields], {}
    stats = {v: s for v in clms.FIELD_VARS if (s := clms.field_stats(v, geoms, p0, p1, weights))}
    if not stats:
        raise HTTPException(404, datasets.missing("clms_soil", "soil moisture and productivity"))
    soil = next((s for v, s in stats.items() if v != "dmp"), None)
    head = ["season", "period_start", "period_end", "rpg_year", "parcel_id", "field", "crop", "code_cultu",
            "code_group", "surf_ha"]
    if soil:
        head += ["soil_last_date", "cells_1km"]
    if "dmp" in stats:
        head += ["dmp_last_date", "cells_300m"]
    cols = {"mean": "mean", "min": "min", "min_date": "min_date", "max": "max", "max_date": "max_date",
            "reference": "other_years", "difference": "difference"}
    for v in stats:
        head += [f"{v}_{c}" for c in cols.values()]
    if "dmp" in stats:
        head += ["dmp_total_t_ha", "dmp_total_other_years_t_ha", "dmp_total_difference_pct"]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(head)
    for i, f in enumerate(fields):
        row = [season, p0, p1, f["rpg_year"], f["parcel_id"], f["field"] or "", f["crop"] or "", f["code_cultu"] or "",
               f["code_group"] or "", finite_round(f["surf_ha"], 2) if f["surf_ha"] is not None else ""]
        if soil:
            row += [_last(soil), int(soil["cells"][i])]
        if "dmp" in stats:
            row += [_last(stats["dmp"]), int(stats["dmp"]["cells"][i])]
        for s in stats.values():
            o = _stats_of(s, i)
            row += ["" if o[c] is None else o[c] for c in cols]
        if "dmp" in stats:
            o = _stats_of(stats["dmp"], i)
            row += ["" if o[c] is None else o[c] for c in ("total", "reference_total", "difference_pct")]
        w.writerow(row)
    name = f"{site}_soil_productivity_{season}.csv"
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
