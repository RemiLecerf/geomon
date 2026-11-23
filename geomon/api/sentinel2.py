"""Sentinel-2 endpoints: catalogue, index time series over an area, index images per date."""

from __future__ import annotations

import io
from functools import lru_cache

import numpy as np
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
from .areas import site_row

router = APIRouter(prefix="/api/s2", tags=["sentinel-2"])

_merc_to_wgs = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)


def _site() -> str:
    with connect() as conn:
        return site_row(conn)["code"]


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
        rows = conn.execute(
            """SELECT date, mean, median, p10, p90, std, n_clear, n_area, clear_fraction
               FROM eo.index_stats WHERE site = %s AND area = %s AND index = %s AND clear_fraction >= %s
               ORDER BY date""",
            (site, area, index, min_clear),
        ).fetchall()
    return {"site": site, "area": area, "index": index, "long_name": s2.INDICES[index].long_name,
            "min_clear": min_clear,
            "points": [{"date": r["date"].isoformat(), **{k: (None if r[k] is None else round(float(r[k]), 4))
                        for k in ("mean", "median", "p10", "p90", "std", "clear_fraction")},
                        "n_clear": r["n_clear"]} for r in rows]}


def _render(site: str, date: str, index: str, masked: bool, part: str = "") -> tuple[bytes, list, float, float, str]:
    """An index image of one date and part (cached until its year's store changes)."""
    z = s2.store_path(site, int(date[:4]), part) / "zarr.json"
    return _render_cached(site, date, index, masked, part, z.stat().st_mtime if z.exists() else None)


@lru_cache(maxsize=48)
def _render_cached(site: str, date: str, index: str, masked: bool, part: str,
                   stamp: float | None) -> tuple[bytes, list, float, float, str]:
    try:
        ds = s2.scene(site, date, part)
    except KeyError as exc:
        raise HTTPException(404, f"No Sentinel-2 scene on {date}") from exc
    try:
        arr = s2.index_array(ds, index, masked=masked)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    ix = s2.INDICES[index]
    png, coords = grid_png(arr, ds["x"].values, ds["y"].values, s2.crs_of(ds), ix.vmin, ix.vmax, ix.colormap)
    return png, coords, ix.vmin, ix.vmax, ix.colormap


def grid_png(arr: np.ndarray, x: np.ndarray, y: np.ndarray, crs: str, vmin: float, vmax: float,
             colormap: str) -> tuple[bytes, list]:
    """A 2-D array on a north-up grid (cell centres x, y) as a Web Mercator PNG for the map, and
    the lon/lat of its corners (TL, TR, BR, BL). Transparent where NaN. Also used for Sentinel-1."""
    cmap = colormaps[colormap]

    def to_rgba(dst: np.ndarray) -> np.ndarray:
        norm = np.clip((dst - vmin) / (vmax - vmin), 0, 1)
        rgba = (cmap(np.nan_to_num(norm)) * 255).astype("uint8")
        rgba[..., 3] = np.where(np.isfinite(dst), 230, 0)
        return rgba

    return grid_rgba_png(arr, x, y, crs, to_rgba)


def grid_rgba_png(arr: np.ndarray, x: np.ndarray, y: np.ndarray, crs: str, to_rgba) -> tuple[bytes, list]:
    """As :func:`grid_png`, with the colours given by ``to_rgba`` (reprojected values, NaN outside
    or without data -> RGBA uint8 image)."""
    res = float(abs(x[1] - x[0]))
    src_transform = from_origin(float(x[0] - res / 2), float(y[0] + res / 2), res, res)
    h, w = arr.shape
    left, top = x[0] - res / 2, y[0] + res / 2
    dst_transform, dw, dh = calculate_default_transform(crs, "EPSG:3857", w, h, left=left, bottom=top - h * res,
                                                        right=left + w * res, top=top)
    dst = np.full((dh, dw), np.nan, dtype="float32")
    reproject(arr.astype("float32"), dst, src_transform=src_transform, src_crs=crs, src_nodata=np.nan,
              dst_transform=dst_transform, dst_crs="EPSG:3857", dst_nodata=np.nan, resampling=Resampling.nearest)
    buf = io.BytesIO()
    Image.fromarray(to_rgba(dst), "RGBA").save(buf, format="PNG")      # optimize=True: several times slower
    x0, y1 = dst_transform.c, dst_transform.f
    x1, y0 = x0 + dw * dst_transform.a, y1 + dh * dst_transform.e
    coords = [list(_merc_to_wgs.transform(cx, cy)) for cx, cy in ((x0, y1), (x1, y1), (x1, y0), (x0, y0))]
    return buf.getvalue(), coords


def legend(vmin: float, vmax: float, colormap: str) -> list[dict]:
    cmap = colormaps[colormap]
    return [{"value": vmin + f * (vmax - vmin), "color": "#%02x%02x%02x" % tuple(int(c * 255) for c in cmap(f)[:3])}
            for f in np.linspace(0, 1, 6)]


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
    return {"date": date, "index": index, "long_name": s2.INDICES[index].long_name,
            "coordinates": images[0]["coordinates"], "images": images,
            "vmin": vmin, "vmax": vmax, "masked": masked, "legend": legend(vmin, vmax, cmap_name)}


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
    rows = conn.execute("SELECT year, code_culture, label FROM ref.rpg_crop_code").fetchall()
    crops = {(r["year"], r["code_culture"]): r["label"] for r in rows}
    # A group's most frequent label in each year (a code list may give a group two labels)
    groups = {r["code_group"]: r["label"] for r in conn.execute(
        """SELECT year, code_group, mode() WITHIN GROUP (ORDER BY label_group) AS label FROM ref.rpg_crop_code
           WHERE code_group IS NOT NULL AND label_group IS NOT NULL GROUP BY 1, 2 ORDER BY 1""").fetchall()}
    # Groups of the RPG 1.0 îlots only (seeds, industrial set-aside, arboriculture...)
    for r in conn.execute("SELECT code_group, min(group_label) AS label FROM ref.rpg_ilot "
                          "WHERE code_group IS NOT NULL GROUP BY 1").fetchall():
        groups.setdefault(r["code_group"], (r["label"] or r["code_group"]).capitalize())
    return crops, groups


def _check_index(index: str) -> None:
    if index not in s2.INDICES:
        raise HTTPException(404, f"Unknown index {index}")


def _season_info(season: int) -> dict:
    return {"season": season, "start": str(pc.season_start(season)), "end": str(pc.season_end(season)),
            "jan1_day": pc.JAN1, "days": pc.SEASON_DAYS}


_NO_SERIES = "No parcel series yet: run docker compose run --rm ingest sentinel2-parcels"


def crops_payload(site: str, index: str, area: str, season: int | None, no_series: str) -> dict:
    """Seasons with crop-group curves, and the crop groups of an area in a season (also for MODIS)."""
    with connect() as conn:
        _, glabels = _crop_labels(conn)
        years_rpg = pc.rpg_years(conn)
        seasons = [r["season"] for r in conn.execute(
            "SELECT DISTINCT season FROM eo.group_season WHERE site = %s AND index = %s AND area = %s ORDER BY 1",
            (site, index, area)).fetchall()]
        rows = conn.execute(
            "SELECT season, code_group, parcels, ha, status FROM eo.group_season WHERE site = %s AND index = %s "
            "AND area = %s", (site, index, area)).fetchall()
    if not seasons:
        raise HTTPException(404, no_series)
    # Default: the latest season whose RPG is published (else the latest season)
    season = season or max((s for s in seasons if s <= max(years_rpg)), default=seasons[-1])
    known = [r for r in rows if r["code_group"]]
    groups = []
    for code in sorted({r["code_group"] for r in known}):
        cur = next((r for r in known if r["code_group"] == code and r["season"] == season), None)
        groups.append({"code": code, "label": glabels.get(code, code), "parcels": cur["parcels"] if cur else 0,
                       "ha": round(cur["ha"], 1) if cur else 0.0,
                       "seasons": sum(1 for r in known if r["code_group"] == code),
                       "status": cur["status"] if cur else None})
    groups.sort(key=lambda g: (-g["ha"], -g["seasons"], g["code"]))
    unknown = sum(r["parcels"] for r in rows if not r["code_group"] and r["season"] == season)
    return {"site": site, "area": area, "index": index, "seasons": seasons, "rpg_years": years_rpg,
            "latest_rpg": max(years_rpg), **_season_info(season), "groups": groups, "parcels_unknown_crop": unknown,
            "permanent_groups": sorted(pc.PERMANENT_GROUPS, key=int), "min_clear": settings.s2_parcel_min_clear}


def crop_payload(site: str, index: str, group: str, season: int, area: str, weight: str,
                 ref_area: str | None) -> tuple[dict, dict | None]:
    """Curve of a crop group in an area for a season and its references (also for MODIS), and the
    season's group entry (None when the group has no curve this season)."""
    if weight not in ("pixels", "parcel"):
        raise HTTPException(422, "weight must be pixels or parcel")
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
    cur = by_season.get(season)
    if season > max(years_rpg) and group not in pc.PERMANENT_GROUPS and not (cur and cur["status"] == "farm"):
        out["note"] = (f"RPG {season} is not published yet: the {season} crops are unknown, "
                       "except permanent ones (taken from the latest RPG) and the farm's plots kept unchanged.")
    if cur is None:
        return out, None
    out["current"] = {"mean": pc.nan_list(cur["mean"]), "q25": pc.nan_list(cur["q25"]),
                      "q75": pc.nan_list(cur["q75"]), "count": [int(c) for c in cur["count"]],
                      "parcels": cur["parcels"], "ha": round(cur["ha"], 1), "status": cur["status"]}
    out["comparison"] = pc.compare(cur["mean"], refs, means, season)
    return out, cur


@router.get("/crops")
def crops(index: str = "ndvi", area: str | None = None, season: int | None = None) -> dict:
    """Seasons with parcel curves, and the crop groups of an area in a season (parcels, area)."""
    _check_index(index)
    site = _site()
    return crops_payload(site, index, area or site, season, _NO_SERIES)


@router.get("/crop")
def crop(group: str, season: int, index: str = "ndvi", area: str | None = None, weight: str = "pixels",
         ref_area: str | None = None) -> dict:
    """Curve of a crop group in an area for a season (mean of its parcels' daily curves), the
    observations behind it, and the 3-year, 5-year and long-term references of the seasons before.

    ``weight``: ``pixels`` (each parcel weighted by its interior pixels, the area measured) or
    ``parcel`` (one parcel, one vote). ``ref_area``: area whose earlier seasons give the references
    (default: the same area), e.g. the farm's plots compared with the whole group.
    """
    _check_index(index)
    site = _site()
    area = area or site
    out, cur = crop_payload(site, index, group, season, area, weight, ref_area)
    if cur is None:
        return out
    # Observations: per date, mean of the group's clear parcels (those with a curve this season)
    years_rpg = pc.rpg_years()
    gy = pc.geometry_year(season, years_rpg)
    with connect() as conn:
        members = conn.execute(
            """SELECT c.rpg_year, c.parcel_id
               FROM eo.parcel_season c
               JOIN eo.parcel_info i ON i.site = c.site AND i.rpg_year = c.rpg_year AND i.parcel_id = c.parcel_id
               LEFT JOIN eo.farm_crop fc ON fc.site = c.site AND fc.season = c.season AND fc.rpg_year = c.rpg_year
                                        AND fc.parcel_id = c.parcel_id
               WHERE c.site = %(site)s AND c.season = %(season)s AND c.index = %(index)s
                 AND c.rpg_year IN (%(gy)s, %(farm_layer)s) AND %(area)s = ANY(i.areas)
                 AND (%(area)s <> 'farm' OR fc.parcel_id IS NOT NULL OR NOT %(farm_lc)s)
                 AND CASE WHEN fc.parcel_id IS NOT NULL THEN fc.code_group = %(group)s      -- the farm's crop
                          ELSE i.code_group = %(group)s AND (i.rpg_year = %(season)s OR i.code_group = ANY(%(permanent)s)) END""",
            {"site": site, "index": index, "gy": gy, "season": season, "group": group, "area": area,
             "permanent": sorted(pc.PERMANENT_GROUPS), "farm_lc": bool(pc.farm_years(site)),
             "farm_layer": pc.FARM_LAYER},
        ).fetchall()
    by_layer: dict[int, list[str]] = {}
    for r in members:
        by_layer.setdefault(r["rpg_year"], []).append(r["parcel_id"])
    rows = pc.group_observations(site, index, by_layer, pc.season_start(season), pc.season_end(season),
                                 weighted=weight == "pixels")
    need = max(1, min(3, round(0.3 * cur["parcels"])))
    out["observations"] = [{"date": str(d), "day": pc.day_of_season(d, season), "mean": round(float(m), 3), "parcels": n}
                           for d, m, n in rows if n >= need]
    return out


@router.get("/parcel")
def parcel(lon: float, lat: float, index: str = "ndvi", season: int | None = None) -> dict:
    """History of the parcel at a point: for each season, the RPG parcel there (or the farm's field, in
    the seasons of its land cover), its crop and curve metrics; for the selected season, its curve,
    observations and outline, and the mean of its own earlier seasons with the same crop group."""
    import datetime as dt
    import json

    _check_index(index)
    site = _site()
    with connect() as conn:
        crops_l, glabels = _crop_labels(conn)
        years_rpg = pc.rpg_years(conn)
        all_seasons = [r["season"] for r in conn.execute(
            "SELECT DISTINCT season FROM eo.parcel_season WHERE site = %s AND index = %s ORDER BY 1",
            (site, index)).fetchall()]
        found = {r["year"]: r for r in conn.execute(
            """SELECT DISTINCT ON (year) year, parcel_id, code_cultu, code_group, surf_ha, bio,
                      ST_AsGeoJSON(geom, 6) AS g
               FROM ref.rpg_parcel WHERE ST_Intersects(geom, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
               ORDER BY year, surf_ha DESC NULLS LAST""", (lon, lat)).fetchall()}
        if not all_seasons:
            raise HTTPException(404, _NO_SERIES)
        # The farm's own fields at the point, year by year (their own series)
        farm = {}
        for r in conn.execute(
                """SELECT year, field_id, plot, code_group, code_cultu, crop_label, surf_ha, ST_AsGeoJSON(geom, 6) AS g
                   FROM ref.farm_landcover
                   WHERE site = %s AND code_group IS NOT NULL
                     AND ST_Intersects(geom, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
                   ORDER BY year, surf_ha DESC NULLS LAST""", (site, lon, lat)).fetchall():
            farm.setdefault(r["year"], r)
        if not found and not farm:
            raise HTTPException(404, "No RPG parcel at this point")
        # The curve of each season: the farm's field of the season, else that season's RPG parcel
        wanted = []
        for s in all_seasons:
            gy = pc.geometry_year(s, years_rpg)
            if s in farm:
                wanted.append((s, farm[s]["field_id"], pc.FARM_LAYER))
            elif gy in found:
                wanted.append((s, found[gy]["parcel_id"], gy))
        stored = {r["season"]: r for r in conn.execute(
            """SELECT c.season, c.n_obs, c.peak, c.peak_date, c.curve, i.areas
               FROM eo.parcel_season c
               LEFT JOIN eo.parcel_info i ON i.site = c.site AND i.rpg_year = c.rpg_year AND i.parcel_id = c.parcel_id
               WHERE c.site = %s AND c.index = %s
                 AND (c.season, c.parcel_id, c.rpg_year) IN (SELECT * FROM unnest(%s::int[], %s::text[], %s::int[]))""",
            (site, index, [w[0] for w in wanted], [w[1] for w in wanted], [w[2] for w in wanted])).fetchall()}
    history, curves = [], {}
    for s in all_seasons:
        gy = pc.geometry_year(s, years_rpg)
        f = found.get(gy)
        lc = farm.get(s)
        if f is None and lc is None:
            history.append({"season": s, "rpg_year": gy, "status": "none"})
            continue
        f = f or {"parcel_id": None, "code_group": None, "code_cultu": None, "surf_ha": None, "bio": None}
        group, cultu, status = pc.effective_crop(s, gy, f["code_group"], f["code_cultu"],
                                                 (lc["code_group"], lc["code_cultu"]) if lc else None)
        known = status != "unknown"
        if status == "farm":
            crop = lc["crop_label"] or crops_l.get((max(years_rpg), cultu)) or glabels.get(group, group)
        else:
            crop = crops_l.get((gy, cultu), cultu) if known else None
        e = {"season": s, "rpg_year": gy, "parcel_id": f["parcel_id"], "status": status,
             "code_cultu": cultu if known else None, "crop": crop,
             "code_group": group if known else None,
             "group_label": glabels.get(group) if known else None,
             "rpg_crop": crops_l.get((gy, f["code_cultu"]), f["code_cultu"])
             if status == "farm" and s == gy and f["code_cultu"] and f["code_cultu"] != cultu else None,
             "last_known_group": f["code_group"] or group, "surf_ha": f["surf_ha"], "bio": f["bio"]}
        if lc is not None:                 # the farm's field: its own outline and series
            e |= {"rpg_year": pc.FARM_LAYER, "parcel_id": lc["field_id"], "field": lc["plot"], "unit": "field",
                  "surf_ha": round(float(lc["surf_ha"]), 2) if lc["surf_ha"] is not None else None}
        r = stored.get(s)
        if r is not None:
            curves[s] = np.frombuffer(r["curve"], dtype="<f4")
            e |= {"n_obs": int(r["n_obs"]), "peak": None if r["peak"] is None else round(float(r["peak"]), 3),
                  "peak_date": None if r["peak_date"] is None else str(r["peak_date"]),
                  "areas": sorted(r["areas"] or [])}
        history.append(e)
    if not curves:
        raise HTTPException(404, "No clear observation of this parcel")
    # Before the parcels (2015), the RPG îlot at the point and its majority crop group
    with connect() as conn:
        ilots = conn.execute(
            """SELECT DISTINCT ON (year) year, ilot_id, code_group, surf_ha, group_ha FROM ref.rpg_ilot
               WHERE ST_Intersects(geom, ST_SetSRID(ST_MakePoint(%s, %s), 4326)) AND year < %s
               ORDER BY year, surf_ha DESC NULLS LAST""", (lon, lat, min(years_rpg))).fetchall()
    history = [{"season": r["year"], "rpg_year": r["year"], "status": "ilots", "parcel_id": r["ilot_id"],
                "code_group": r["code_group"], "group_label": glabels.get(r["code_group"], r["code_group"]),
                "crop": glabels.get(r["code_group"], r["code_group"]), "surf_ha": r["surf_ha"],
                "share": None if not r["group_ha"] or not r["surf_ha"] else round(min(r["group_ha"] / r["surf_ha"], 1), 2)}
               for r in ilots] + history
    this_year = dt.date.today().year      # default: the season harvested this year
    season = season if season in curves else (this_year if this_year in curves else max(curves))
    sel = next(e for e in history if e["season"] == season)
    gy = sel["rpg_year"]
    obs = pc.field_observations(site, index, gy, sel["parcel_id"], pc.season_start(season), pc.season_end(season))
    obs = obs[obs["valid"] >= pc.STORE_MIN_CLEAR].to_dict("records")         # half clear or more: shown
    group = sel["code_group"] or sel["last_known_group"]
    by_season = {e["season"]: e for e in history}
    own = {s: c for s, c in curves.items() if s < season and by_season[s].get("code_group") == group}
    out = {"site": site, "index": index, "point": [lon, lat], **_season_info(season),
           "min_clear": settings.s2_parcel_min_clear, "history": history, "parcel": sel,
           "reference_group": group, "reference_group_label": glabels.get(group, group),
           "geometry": json.loads(farm[season]["g"] if sel.get("unit") == "field" else found[gy]["g"]),
           "curve": pc.nan_list(curves[season]),
           "observations": [{"date": str(o["date"]), "day": pc.day_of_season(o["date"], season),
                             "mean": round(float(o["mean"]), 3), "std": round(float(o["std"] or 0), 3),
                             "clear_fraction": round(float(o["valid"]), 3),
                             "used": bool(o["valid"] >= settings.s2_parcel_min_clear)} for o in obs],
           "own": None}
    if len(own) >= 2:
        with pc._quiet():
            mean = np.nanmean(np.stack(list(own.values())), axis=0)
        out["own"] = {"seasons": sorted(own), "mean": pc.nan_list(mean)}
    from .clms import parcel_vpp

    vpp = parcel_vpp(site, season, gy, sel["parcel_id"])
    if vpp:                       # Copernicus HR-VPP season of the parcel, on the chart's day axis
        for k in ("sos", "peak", "eos"):
            vpp[f"{k}_day"] = pc.day_of_season(dt.date.fromisoformat(vpp[k]), season) if vpp.get(k) else None
    out["vpp"] = vpp
    return out
