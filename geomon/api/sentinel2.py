"""Sentinel-2 endpoints: catalogue, index time series over an area, index images per date."""

from __future__ import annotations

import io
from functools import lru_cache

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from matplotlib import colormaps
from PIL import Image
from pyproj import Transformer
from rasterio.transform import from_origin
from rasterio.warp import Resampling, calculate_default_transform, reproject

from .. import parcels as pc
from .. import sentinel2 as s2
from ..config import settings
from ..db import connect

router = APIRouter(prefix="/api/s2", tags=["sentinel-2"])

_merc_to_wgs = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)


def _site() -> str:
    with connect() as conn:
        row = conn.execute("SELECT code FROM meta.aoi WHERE kind = 'site' AND code = %s",
                           (settings.site_code,)).fetchone()
    if not row:
        raise HTTPException(503, "No AOI yet")
    return row["code"]


@router.get("/info")
def info() -> dict:
    """What is stored: years, dates, bands, indices that can be computed."""
    site = _site()
    years = s2.years(site)
    if not years:
        return {"site": site, "available": False, "years": [], "indices": [],
                "hint": "run: docker compose run --rm ingest sentinel2-login, then ingest sentinel2"}
    bands, dates, attrs = set(), set(), {}
    for part in s2.parts(site):
        for y in s2.years(site, part):
            ds = s2.open_year(site, y, part)
            bands |= {v for v in ds.data_vars if v in s2.BANDS}
            dates |= {str(t)[:10] for t in ds["time"].values}
            attrs = dict(ds.attrs)
    res = float(abs(ds["x"].values[1] - ds["x"].values[0]))
    return {
        "site": site, "available": True, "years": years, "dates": len(dates), "first": min(dates),
        "last": max(dates), "parts": [p or "all" for p in s2.parts(site)],
        "bands": [{"name": b, "description": s2.BANDS[b][0], "native_resolution_m": s2.BANDS[b][1]}
                  for b in s2.BANDS if b in bands],
        "indices": [{"name": i.name, "long_name": i.long_name, "bands": [i.a, i.b], "vmin": i.vmin, "vmax": i.vmax}
                    for i in s2.INDICES.values() if i.name in s2.available_indices(bands)],
        "missing_indices": [{"name": i.name, "bands": [i.a, i.b]} for i in s2.INDICES.values()
                            if i.name not in s2.available_indices(bands)],
        "crs": attrs.get("crs"), "resolution_m": res, "collection": s2.COLLECTION,
        "max_cloud_cover": attrs.get("max_cloud_cover"), "min_clear_fraction": attrs.get("min_clear_fraction"),
        "clear_classes": {k: s2.SCL_CLASSES[k] for k in s2.CLEAR_CLASSES},
        "max_clear_red": s2.MAX_CLEAR_RED,
        "updated": attrs.get("updated"),
    }


@router.get("/series")
def series(index: str = "ndvi", area: str | None = None, min_clear: float = 0.9) -> dict:
    """Per-date statistics of an index over an area (clear pixels only).

    ``min_clear``: dates where less than this fraction of the area is clear are left out.
    """
    if index not in s2.INDICES:
        raise HTTPException(404, f"Unknown index {index}")
    site = _site()
    area = area or site
    with connect() as conn:
        exists = conn.execute("SELECT to_regclass('eo.index_stats') AS t").fetchone()["t"]
        rows = conn.execute(
            """SELECT date, mean, median, p10, p90, std, n_clear, n_area, clear_fraction
               FROM eo.index_stats WHERE site = %s AND area = %s AND index = %s AND clear_fraction >= %s
               ORDER BY date""",
            (site, area, index, min_clear),
        ).fetchall() if exists else []
    return {"site": site, "area": area, "index": index, "long_name": s2.INDICES[index].long_name,
            "min_clear": min_clear,
            "points": [{"date": r["date"].isoformat(), **{k: (None if r[k] is None else round(float(r[k]), 4))
                        for k in ("mean", "median", "p10", "p90", "std", "clear_fraction")},
                        "n_clear": r["n_clear"]} for r in rows]}


@lru_cache(maxsize=48)
def _render(site: str, date: str, index: str, masked: bool, part: str = "") -> tuple[bytes, list, float, float, str]:
    try:
        ds = s2.scene(site, date, part)
    except KeyError as exc:
        raise HTTPException(404, f"No Sentinel-2 scene on {date}") from exc
    try:
        arr = s2.index_array(ds, index, masked=masked)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    ix = s2.INDICES[index]
    x, y = ds["x"].values, ds["y"].values
    res = float(abs(x[1] - x[0]))
    crs = s2.crs_of(ds)
    src_transform = from_origin(float(x[0] - res / 2), float(y[0] + res / 2), res, res)
    h, w = arr.shape
    left, top = x[0] - res / 2, y[0] + res / 2
    dst_transform, dw, dh = calculate_default_transform(crs, "EPSG:3857", w, h, left=left, bottom=top - h * res,
                                                        right=left + w * res, top=top)
    dst = np.full((dh, dw), np.nan, dtype="float32")
    reproject(arr, dst, src_transform=src_transform, src_crs=crs, src_nodata=np.nan,
              dst_transform=dst_transform, dst_crs="EPSG:3857", dst_nodata=np.nan, resampling=Resampling.nearest)
    cmap = colormaps[ix.colormap]
    norm = np.clip((dst - ix.vmin) / (ix.vmax - ix.vmin), 0, 1)
    rgba = (cmap(np.nan_to_num(norm)) * 255).astype("uint8")
    rgba[..., 3] = np.where(np.isfinite(dst), 230, 0)
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, format="PNG", optimize=True)
    x0, y1 = dst_transform.c, dst_transform.f
    x1, y0 = x0 + dw * dst_transform.a, y1 + dh * dst_transform.e
    coords = [list(_merc_to_wgs.transform(cx, cy)) for cx, cy in ((x0, y1), (x1, y1), (x1, y0), (x0, y0))]
    return buf.getvalue(), coords, ix.vmin, ix.vmax, ix.colormap


@router.get("/{index}/info")
def image_info(index: str, date: str, masked: bool = True) -> dict:
    if index not in s2.INDICES:
        raise HTTPException(404, f"Unknown index {index}")
    site = _site()
    images = []
    for part in s2.parts(site):            # one image per part that has the date
        try:
            _, coords, vmin, vmax, cmap_name = _render(site, date, index, masked, part)
        except HTTPException:
            continue
        images.append({"part": part, "coordinates": coords,
                       "url": f"/api/s2/{index}.png?date={date}&masked={str(masked).lower()}&part={part}&site={site}"})
    if not images:
        raise HTTPException(404, f"No Sentinel-2 scene on {date}")
    cmap = colormaps[cmap_name]
    return {"date": date, "index": index, "long_name": s2.INDICES[index].long_name,
            "coordinates": images[0]["coordinates"], "images": images,
            "vmin": vmin, "vmax": vmax, "masked": masked,
            "legend": [{"value": vmin + f * (vmax - vmin),
                        "color": "#%02x%02x%02x" % tuple(int(c * 255) for c in cmap(f)[:3])}
                       for f in np.linspace(0, 1, 6)]}


@router.get("/{index}.png")
def image_png(index: str, date: str, masked: bool = True, part: str = "") -> Response:
    if index not in s2.INDICES:
        raise HTTPException(404, f"Unknown index {index}")
    png = _render(_site(), date, index, masked, part)[0]
    return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=86400"})


# ---------------------------------------------------------------------------
# RPG parcels and crop groups
# ---------------------------------------------------------------------------
def _crop_labels(conn) -> tuple[dict, dict]:
    """(year, crop code) -> label, and group code -> label (latest year that has it)."""
    rows = conn.execute("SELECT year, code_culture, label, code_group, label_group FROM ref.rpg_crop_code "
                        "ORDER BY year").fetchall()
    crops = {(r["year"], r["code_culture"]): r["label"] for r in rows}
    groups = {}
    for r in rows:
        if r["code_group"] and r["label_group"]:
            groups[r["code_group"]] = r["label_group"]
    return crops, groups


def _curves(index: str):
    if index not in s2.INDICES:
        raise HTTPException(404, f"Unknown index {index}")
    site = _site()
    df, mat = pc.curves(site, index, pc.stamp(site))
    if not len(df):
        raise HTTPException(404, "No parcel series yet: run docker compose run --rm ingest sentinel2-parcels")
    return site, df, mat


def _season_info(season: int) -> dict:
    return {"season": season, "start": str(pc.season_start(season)), "end": str(pc.season_end(season)),
            "jan1_day": pc.JAN1, "days": pc.SEASON_DAYS}


def _in_area(df, area: str):
    return df["areas"].apply(lambda a: area in a)


@router.get("/crops")
def crops(index: str = "ndvi", area: str | None = None, season: int | None = None) -> dict:
    """Seasons with parcel curves, and the crop groups of an area in a season (parcels, area)."""
    site, df, _ = _curves(index)
    area = area or site
    with connect() as conn:
        _, glabels = _crop_labels(conn)
        years_rpg = pc.rpg_years(conn)
    seasons = sorted(int(s) for s in df["season"].unique())
    season = season or max(s for s in seasons if s <= max(years_rpg))
    known = df[_in_area(df, area) & (df["crop_status"] != "unknown")]
    cur = known[known["season"] == season]
    groups = []
    for code in known["code_group"].dropna().unique():
        g = cur[cur["code_group"] == code]
        groups.append({"code": code, "label": glabels.get(code, code), "parcels": int(len(g)),
                       "ha": round(float(g["surf_ha"].fillna(0).sum()), 1),
                       "seasons": int(known.loc[known["code_group"] == code, "season"].nunique()),
                       "status": None if g.empty else ("declared" if (g["crop_status"] == "declared").all()
                                                       else "assumed")})
    groups.sort(key=lambda g: (-g["ha"], -g["seasons"]))
    unknown = int((_in_area(df, area) & (df["season"] == season) & (df["crop_status"] == "unknown")).sum())
    return {"site": site, "area": area, "index": index, "seasons": seasons, "rpg_years": years_rpg,
            "latest_rpg": max(years_rpg), **_season_info(season), "groups": groups, "parcels_unknown_crop": unknown,
            "permanent_groups": sorted(pc.PERMANENT_GROUPS, key=int), "min_clear": settings.s2_parcel_min_clear}


@router.get("/crop")
def crop(group: str, season: int, index: str = "ndvi", area: str | None = None, weight: str = "pixels",
         ref_area: str | None = None) -> dict:
    """Curve of a crop group in an area for a season (mean of its parcels' daily curves), the
    observations behind it, and the 3-year, 5-year and long-term references of the seasons before.

    ``weight``: ``pixels`` (each parcel weighted by its interior pixels, the area measured) or
    ``parcel`` (one parcel, one vote). ``ref_area``: area whose earlier seasons give the references
    (default: the same area), e.g. the farm's plots compared with the whole group.
    """
    if weight not in ("pixels", "parcel"):
        raise HTTPException(422, "weight must be pixels or parcel")
    site, df, mat = _curves(index)
    area = area or site
    by_season = pc.group_curves(site, index, area, group, weighted=weight == "pixels")
    ref_area = ref_area or area
    by_ref = by_season if ref_area == area else pc.group_curves(site, index, ref_area, group,
                                                                 weighted=weight == "pixels")
    with connect() as conn:
        _, glabels = _crop_labels(conn)
        years_rpg = pc.rpg_years(conn)
    means = {s: g["mean"] for s, g in by_ref.items()}
    refs = pc.references(means, season)
    out = {"site": site, "area": area, "index": index, "group": group, "label": glabels.get(group, group),
           **_season_info(season), "min_clear": settings.s2_parcel_min_clear,
           "smoothing": settings.s2_parcel_smoothing, "weight": weight, "ref_area": ref_area,
           "seasons": {str(s): {"parcels": g["parcels"], "ha": round(g["ha"], 1), "status": g["status"]}
                       for s, g in sorted(by_season.items())},
           "refs": {k: {"seasons": r["seasons"], "mean": pc.nan_list(r["mean"]),
                        **({"p10": pc.nan_list(r["p10"]), "p90": pc.nan_list(r["p90"])} if "p10" in r else {})}
                    for k, r in refs.items()},
           "current": None, "observations": [], "comparison": None}
    if season > max(years_rpg) and group not in pc.PERMANENT_GROUPS:
        out["note"] = (f"RPG {season} is not published yet: the {season} crops are unknown, "
                       "except permanent ones (taken from the latest RPG).")
    cur = by_season.get(season)
    if cur is None:
        return out
    out["current"] = {"mean": pc.nan_list(cur["mean"]), "q25": pc.nan_list(cur["q25"]),
                      "q75": pc.nan_list(cur["q75"]), "count": [int(c) for c in cur["count"]],
                      "parcels": cur["parcels"], "ha": round(cur["ha"], 1), "status": cur["status"]}
    out["comparison"] = pc.compare(cur["mean"], refs, means, season)
    # Observations: per date, mean of the group's clear parcels
    sel = df[(df["season"] == season) & (df["code_group"] == group) & (df["crop_status"] != "unknown")
             & _in_area(df, area)]
    gy = pc.geometry_year(season, years_rpg)
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT date, {"sum(mean * n_pixels) / sum(n_pixels)" if weight == "pixels" else "avg(mean)"} AS mean,
                      count(*) AS n
               FROM eo.parcel_stats
               WHERE site = %s AND index = %s AND rpg_year = %s AND date BETWEEN %s AND %s
                 AND n_clear >= %s * n_pixels AND parcel_id = ANY(%s)
               GROUP BY date ORDER BY date""",
            (site, index, gy, pc.season_start(season), pc.season_end(season), settings.s2_parcel_min_clear,
             list(sel["parcel_id"])),
        ).fetchall()
    need = max(1, min(3, round(0.3 * len(sel))))
    out["observations"] = [{"date": str(r["date"]), "day": pc.day_of_season(r["date"], season),
                            "mean": round(float(r["mean"]), 3), "parcels": r["n"]}
                           for r in rows if r["n"] >= need]
    return out


@router.get("/parcel")
def parcel(lon: float, lat: float, index: str = "ndvi", season: int | None = None) -> dict:
    """History of the parcel at a point: for each season, the RPG parcel there, its crop and curve
    metrics; for the selected season, its curve, observations and outline, and the mean of its own
    earlier seasons with the same crop group."""
    import json

    site, df, mat = _curves(index)
    with connect() as conn:
        crops_l, glabels = _crop_labels(conn)
        years_rpg = pc.rpg_years(conn)
        found = {r["year"]: r for r in conn.execute(
            """SELECT DISTINCT ON (year) year, parcel_id, code_cultu, code_group, surf_ha, bio,
                      ST_AsGeoJSON(geom, 6) AS g
               FROM ref.rpg_parcel WHERE ST_Intersects(geom, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
               ORDER BY year, surf_ha DESC NULLS LAST""", (lon, lat)).fetchall()}
    if not found:
        raise HTTPException(404, "No RPG parcel at this point")
    history, rows = [], {}
    for s in sorted(int(x) for x in df["season"].unique()):
        gy = pc.geometry_year(s, years_rpg)
        f = found.get(gy)
        if f is None:
            history.append({"season": s, "rpg_year": gy, "status": "none"})
            continue
        status = "declared" if s == gy else ("assumed" if f["code_group"] in pc.PERMANENT_GROUPS else "unknown")
        known = status != "unknown"
        e = {"season": s, "rpg_year": gy, "parcel_id": f["parcel_id"], "status": status,
             "code_cultu": f["code_cultu"] if known else None,
             "crop": crops_l.get((gy, f["code_cultu"]), f["code_cultu"]) if known else None,
             "code_group": f["code_group"] if known else None,
             "group_label": glabels.get(f["code_group"]) if known else None,
             "last_known_group": f["code_group"], "surf_ha": f["surf_ha"], "bio": f["bio"]}
        hit = df.index[(df["season"] == s) & (df["parcel_id"] == f["parcel_id"]) & (df["rpg_year"] == gy)]
        if len(hit):
            r = df.loc[hit[0]]
            rows[s] = int(hit[0])
            e |= {"n_obs": int(r["n_obs"]), "peak": None if pd.isna(r["peak"]) else round(float(r["peak"]), 3),
                  "peak_date": None if pd.isna(r["peak_date"]) else str(r["peak_date"]),
                  "areas": sorted(r["areas"])}
        history.append(e)
    if not rows:
        raise HTTPException(404, "No clear observation of this parcel")
    import datetime as dt

    this_year = dt.date.today().year      # default: the season harvested this year
    season = season if season in rows else (this_year if this_year in rows else max(rows))
    sel = next(e for e in history if e["season"] == season)
    gy = sel["rpg_year"]
    with connect() as conn:
        obs = conn.execute(
            """SELECT date, mean, std, n_clear, n_pixels FROM eo.parcel_stats
               WHERE site = %s AND index = %s AND rpg_year = %s AND parcel_id = %s AND date BETWEEN %s AND %s
               ORDER BY date""",
            (site, index, gy, sel["parcel_id"], pc.season_start(season), pc.season_end(season))).fetchall()
    group = sel["code_group"] or sel["last_known_group"]
    by_season = {e["season"]: e for e in history}
    own = {s: mat[i] for s, i in rows.items() if s < season and by_season[s].get("code_group") == group}
    out = {"site": site, "index": index, "point": [lon, lat], **_season_info(season),
           "min_clear": settings.s2_parcel_min_clear, "history": history, "parcel": sel,
           "reference_group": group, "reference_group_label": glabels.get(group, group),
           "geometry": json.loads(found[gy]["g"]), "curve": pc.nan_list(mat[rows[season]]),
           "observations": [{"date": str(o["date"]), "day": pc.day_of_season(o["date"], season),
                             "mean": round(float(o["mean"]), 3), "std": round(float(o["std"] or 0), 3),
                             "clear_fraction": round(o["n_clear"] / o["n_pixels"], 3),
                             "used": o["n_clear"] >= settings.s2_parcel_min_clear * o["n_pixels"]} for o in obs],
           "own": None}
    if len(own) >= 2:
        with pc._quiet():
            mean = np.nanmean(np.stack(list(own.values())), axis=0)
        out["own"] = {"seasons": sorted(own), "mean": pc.nan_list(mean)}
    return out


def clear_cache() -> None:
    _render.cache_clear()
    s2.clear_cache()
    pc.clear_cache()
