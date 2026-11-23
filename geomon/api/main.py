"""Geomon web API and web page.

Run: uvicorn geomon.api.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import hashlib
import json
import logging
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .. import __version__, units
from .. import parcels as pc
from ..config import _current, all_sites, settings
from ..db import connect
from ..migrate import migrate
from . import clms, datasets, mf_live, mf_models, modis, nowcast, radar, rasters, sentinel1, sentinel2, soils, tiles, weather
from .areas import area as _area
from .areas import site_row as _site

log = logging.getLogger(__name__)

SITE_COOKIE = "geomon_site"
_schema_ready = False


def _ensure_schema() -> None:
    """Creates or upgrades the database schema; retried at the next request when it fails
    (database not reachable yet)."""
    global _schema_ready
    try:
        migrate()
        _schema_ready = True
    except Exception as exc:
        log.warning("Database schema not checked yet: %s", exc)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    await run_in_threadpool(_ensure_schema)
    yield


app = FastAPI(title="Geomon", version=__version__, lifespan=_lifespan)
# GeoJSON layers are large (RPG parcels, OSM buildings: tens of MB) and compress about 5-fold
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
app.include_router(weather.router)
app.include_router(sentinel2.router)
app.include_router(sentinel1.router)
app.include_router(modis.router)
app.include_router(radar.router)
app.include_router(nowcast.router)
app.include_router(clms.router)
app.include_router(mf_live.router)
app.include_router(mf_models.router)
app.include_router(datasets.router)
app.include_router(soils.router)
app.include_router(tiles.router)


@app.middleware("http")
async def _site_of_request(request: Request, call_next):
    """The site of a request: ``?site=`` (remembered in a cookie), else the cookie, else the first site."""
    if not _schema_ready:
        await run_in_threadpool(_ensure_schema)
    codes = [s.code for s in all_sites()]
    asked = request.query_params.get("site")
    code = asked if asked in codes else request.cookies.get(SITE_COOKIE)
    token = _current.set(code if code in codes else None)
    try:
        response = await call_next(request)
    finally:
        _current.reset(token)
    if asked in codes:
        response.set_cookie(SITE_COOKIE, asked, max_age=365 * 86400, samesite="lax")
    # The page itself (index.html, app.js, style.css): revalidated at every load (ETag), so that an
    # update shows at once instead of after the browser's heuristic cache delay
    if not request.url.path.startswith("/api/") and "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/api/sites")
def sites() -> dict:
    """The sites this server follows, and the current one (cookie)."""
    with connect() as conn:
        rows = {r["code"]: r for r in conn.execute(
            """SELECT code, name, members, ST_Area(geom::geography) / 1e4 AS area_ha, updated_at
               FROM meta.aoi WHERE kind = 'site'""").fetchall()}
    return {"current": settings.site_code,
            "sites": [{"code": s.code, "name": (rows.get(s.code) or {}).get("name") or s.name or s.code,
                       "unit": s.unit or None,
                       "communes": list(s.communes) or list((rows.get(s.code) or {}).get("members") or []),
                       "loaded": s.code in rows,
                       "area_ha": (rows.get(s.code) or {}).get("area_ha"), "farm": bool(s.farm_file)}
                      for s in all_sites()]}

WEB_DIR = Path(__file__).resolve().parents[2] / "web"

# Simplification tolerance (degrees) per admin level, for display only
SIMPLIFY = {"COM": 0.00002, "EPCI": 0.0001, "ARR": 0.0005, "DEP": 0.001, "REG": 0.002}
LEVEL_ORDER = ["COM", "EPCI", "ARR", "DEP", "REG", "FRANCE"]
LEVEL_NAMES = {"COM": "Commune", "EPCI": "Intercommunalité (EPCI)", "ARR": "Arrondissement",
               "DEP": "Département", "REG": "Région", "FRANCE": "Pays"}
OSM_LAYERS = tiles.OSM_LAYERS


def _feature_collection(rows: list[dict]) -> JSONResponse:
    feats = [
        {"type": "Feature", "geometry": json.loads(r.pop("geojson")), "properties": r}
        for r in rows
        if r.get("geojson")
    ]
    return JSONResponse({"type": "FeatureCollection", "features": feats})


# ---------------------------------------------------------------------------
# AOI and administrative hierarchy
# ---------------------------------------------------------------------------
@app.get("/api/aoi")
def aoi() -> dict:
    """The AOI: its communes, the group, and the parent units of the communes."""
    with connect() as conn:
        site = _site(conn)
        bbox = json.loads(site["bbox"])["coordinates"][0]
        xs, ys = [p[0] for p in bbox], [p[1] for p in bbox]
        communes = conn.execute(
            """SELECT a.code, a.name, u.props->>'code_postal' AS postal_code, u.props,
                      ST_Area(a.geom::geography) / 1e4 AS area_ha,
                      ST_X(ST_PointOnSurface(a.geom)) AS lon, ST_Y(ST_PointOnSurface(a.geom)) AS lat,
                      ST_XMin(a.geom) AS x0, ST_YMin(a.geom) AS y0, ST_XMax(a.geom) AS x1, ST_YMax(a.geom) AS y1
               FROM meta.aoi a
               LEFT JOIN ref.admin_unit u ON u.level = 'COM' AND u.code = a.code AND u.valid_to IS NULL
               WHERE a.kind = 'commune' AND a.site = %s ORDER BY a.position""", (site["code"],)
        ).fetchall()
        group = conn.execute(
            """SELECT ST_Area(geom::geography) / 1e4 AS area_ha,
                      ST_X(ST_PointOnSurface(geom)) AS lon, ST_Y(ST_PointOnSurface(geom)) AS lat
               FROM meta.aoi WHERE kind = 'site' AND code = %s""",
            (site["code"],),
        ).fetchone()
        # Parent units of every commune (distinct, in hierarchy order)
        wanted: dict[str, list[str]] = {lv: [] for lv in LEVEL_ORDER}
        for c in communes:
            p = c["props"] or {}
            for lv, v in (("EPCI", (p.get("codes_siren_des_epci") or "").split("/")[0]),
                          ("ARR", p.get("code_insee_de_l_arrondissement")),
                          ("DEP", p.get("code_insee_du_departement")),
                          ("REG", p.get("code_insee_de_la_region"))):
                if v and v not in wanted[lv]:
                    wanted[lv].append(v)
        wanted["COM"] = [c["code"] for c in communes]
        wanted["FRANCE"] = ["F"]
        # One query for every unit (a département site has hundreds of communes)
        pairs = [(lv, code) for lv in LEVEL_ORDER for code in wanted[lv]]
        found = {(r["level"], r["code"]): r for r in conn.execute(
            """SELECT level, code, name, vintage, valid_from, valid_to, source FROM ref.admin_unit
               WHERE (level, code) IN (SELECT * FROM unnest(%s::text[], %s::text[])) AND valid_to IS NULL""",
            ([lv for lv, _ in pairs], [c for _, c in pairs])).fetchall()}
        hierarchy = []
        for key in pairs:
            if key in found:
                hierarchy.append(found[key] | {"level_name": LEVEL_NAMES[key[0]]})
    is_group = len(communes) > 1
    areas = ([{"code": site["code"], "kind": "site", "name": site["name"], "members": site["members"],
               "area_ha": group["area_ha"]}] if is_group else [])
    areas += [{"code": c["code"], "kind": "commune", "name": c["name"], "members": [c["code"]],
               "area_ha": c["area_ha"], "postal_code": c["postal_code"]} for c in communes]
    farm = conn_farm()
    if farm:
        areas.append({"code": farm["code"], "kind": "farm", "name": farm["name"], "members": farm["members"],
                      "area_ha": farm["area_ha"], "plots": farm["plots"], "declared_ha": farm["declared_ha"]})
    first = communes[0] if communes else None
    return {
        "code": site["code"],
        "name": site["name"],
        "is_group": is_group,
        "members": site["members"],
        "bbox": [min(xs), min(ys), max(xs), max(ys)],
        "center": [group["lon"], group["lat"]] if group else None,
        "area_ha": group["area_ha"] if group else None,
        "postal_code": first["postal_code"] if first and not is_group else None,
        "unit": settings.unit or None,
        "communes": [{"code": c["code"], "name": c["name"], "postal_code": c["postal_code"],
                      "epci": ((c["props"] or {}).get("codes_siren_des_epci") or "").split("/")[0] or None,
                      "area_ha": c["area_ha"], "center": [c["lon"], c["lat"]],
                      "bbox": [c["x0"], c["y0"], c["x1"], c["y1"]]} for c in communes],
        "areas": areas,
        "hierarchy": hierarchy,
        "farm": farm,
    }


def conn_farm() -> dict | None:
    """The farm area (GEOMON_FARM_FILE), if loaded."""
    with connect() as conn:
        row = conn.execute(
            """SELECT a.code, a.name, a.members, ST_Area(a.geom::geography) / 1e4 AS area_ha,
                      ST_XMin(a.geom) AS x0, ST_YMin(a.geom) AS y0, ST_XMax(a.geom) AS x1, ST_YMax(a.geom) AS y1,
                      (SELECT count(*) FROM ref.farm_plot WHERE site = %(s)s) AS plots,
                      (SELECT sum(surf_ha) FROM ref.farm_plot WHERE site = %(s)s) AS declared_ha,
                      (SELECT max(campaign) FROM ref.farm_plot WHERE site = %(s)s) AS campaign
               FROM meta.aoi a WHERE a.kind = 'farm' AND a.site = %(s)s LIMIT 1""", {"s": settings.site_code}).fetchone()
    if not row:
        return None
    row["bbox"] = [row.pop("x0"), row.pop("y0"), row.pop("x1"), row.pop("y1")]
    return row


@app.get("/api/farm.geojson")
def farm_geojson() -> JSONResponse:
    """The farm's plots, with the RPG crop of the latest year covering most of each plot."""
    if not conn_farm():
        return JSONResponse({"type": "FeatureCollection", "features": []})
    with connect() as conn:
        rows = conn.execute(
            """SELECT f.id, f.campaign, f.number, f.island, f.commune, f.surf_ha,
                      ST_Area(f.geom::geography) / 1e4 AS area_ha, ST_AsGeoJSON(f.geom, 6) AS geojson,
                      c.label AS crop, c.label_group AS crop_group, r.code_group
               FROM ref.farm_plot f
               LEFT JOIN LATERAL (
                   SELECT p.year, p.code_cultu, p.code_group FROM ref.rpg_parcel p
                   WHERE p.year = (SELECT max(year) FROM ref.rpg_parcel) AND ST_Intersects(p.geom, f.geom)
                   ORDER BY ST_Area(ST_Intersection(p.geom, f.geom)) DESC LIMIT 1) r ON true
               LEFT JOIN ref.rpg_crop_code c ON c.year = r.year AND c.code_culture = r.code_cultu
               WHERE f.site = %s
               ORDER BY f.id""", (settings.site_code,)).fetchall()
    return JSONResponse({"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": json.loads(r.pop("geojson")), "properties": r} for r in rows]})


@app.get("/api/area.geojson")
def area_geojson(area: str | None = None) -> JSONResponse:
    """Outline of an analysis area (the group by default)."""
    with connect() as conn:
        a = _area(conn, area)
    return JSONResponse({"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": a["geometry"],
         "properties": {"code": a["code"], "name": a["name"], "kind": a["kind"], "members": a["members"]}}]})


@app.get("/api/area/summary")
def area_summary(area: str | None = None, year: int | None = None) -> dict:
    """Land summary of an area: surface, terrain and soil averages, declared crops."""
    with connect() as conn:
        a = _area(conn, area)
    return {
        "area": {k: a[k] for k in ("code", "name", "kind", "members", "area_ha", "is_group")},
        "rasters": rasters.zonal(a["geometry"]),
        "rpg": rpg_summary(year=year, area=a["code"]),
        "units": {"area": "ha"},
    }


@app.get("/api/admin.geojson")
def admin_geojson(levels: str = "COM,EPCI") -> JSONResponse:
    wanted = [lv for lv in levels.split(",") if lv in SIMPLIFY]
    with connect() as conn:
        members = _site(conn)["members"]
        rows = conn.execute(
            """SELECT level, code, name, vintage, (level = 'COM' AND code = ANY(%(members)s)) AS is_aoi,
                      ST_AsGeoJSON(ST_SimplifyPreserveTopology(geom,
                          CASE level WHEN 'COM' THEN %(com)s WHEN 'EPCI' THEN %(epci)s
                                     WHEN 'ARR' THEN %(arr)s WHEN 'DEP' THEN %(dep)s ELSE %(reg)s END), 6) AS geojson
               FROM ref.admin_unit
               WHERE level = ANY(%(levels)s) AND valid_to IS NULL AND geom IS NOT NULL""",
            {"members": members, "levels": wanted, "com": SIMPLIFY["COM"], "epci": SIMPLIFY["EPCI"],
             "arr": SIMPLIFY["ARR"], "dep": SIMPLIFY["DEP"], "reg": SIMPLIFY["REG"]},
        ).fetchall()
    return _feature_collection(rows)


# ---------------------------------------------------------------------------
# RPG
# ---------------------------------------------------------------------------
@app.get("/api/rpg.geojson")
def rpg_geojson(year: int | None = None) -> JSONResponse:
    """Parcels of the download extent; ``in_commune``: the parcel touches one of the AOI communes."""
    year = year or pc.latest_rpg_year() or settings.rpg_first_year
    with connect() as conn:
        rows = conn.execute(
            """SELECT p.id, p.year, p.parcel_id, p.code_cultu, c.label AS crop,
                      p.code_group, c.label_group AS crop_group, p.cat_cult_p, p.surf_ha, p.bio,
                      ST_Intersects(p.geom, a.geom) AS in_commune,
                      ST_AsGeoJSON(p.geom, 6) AS geojson
               FROM ref.rpg_parcel p
               LEFT JOIN ref.rpg_crop_code c ON c.year = p.year AND c.code_culture = p.code_cultu
               CROSS JOIN (SELECT geom, bbox FROM meta.aoi WHERE kind = 'site' AND code = %s) a
               WHERE p.year = %s AND p.geom && a.bbox""",
            (settings.site_code, year),
        ).fetchall()
    return _feature_collection(rows)


@app.get("/api/rpg/years")
def rpg_years() -> dict:
    """RPG years loaded over the site: parcels (2015-) and îlots with their majority crop (2007-2014)."""
    with connect() as conn:
        bbox = "(SELECT bbox FROM meta.aoi WHERE kind = 'site' AND code = %s)"
        parcels = [r["year"] for r in conn.execute(
            f"SELECT DISTINCT year FROM ref.rpg_parcel WHERE geom && {bbox} ORDER BY 1", (settings.site_code,)).fetchall()]
        ilots = [r["year"] for r in conn.execute(
            f"SELECT DISTINCT year FROM ref.rpg_ilot WHERE geom && {bbox} ORDER BY 1", (settings.site_code,)).fetchall()]
    return {"parcels": parcels, "ilots": [y for y in ilots if y not in parcels], "latest": parcels[-1] if parcels else None}


@app.get("/api/rpg/summary")
def rpg_summary(year: int | None = None, area: str | None = None) -> dict:
    """Crop areas inside an area (parcels clipped to the commune or group outline). Before 2015, the
    areas of the îlots by their majority crop group (``ilots``: true)."""
    year = year or pc.latest_rpg_year() or settings.rpg_first_year
    if year < settings.rpg_first_year:
        return _ilot_summary(year, area)
    with connect() as conn:
        a = _area(conn, area)
        rows = conn.execute(
            """SELECT p.code_group, COALESCE(c.label_group, p.code_group) AS crop_group,
                      count(*) AS parcels,
                      -- only the parcels crossing the outline are clipped (3x faster for a group)
                      sum(ST_Area(CASE WHEN ST_CoveredBy(p.geom, a.geom) THEN p.geom
                                       ELSE ST_Intersection(p.geom, a.geom) END::geography)) / 1e4 AS area_ha
               FROM ref.rpg_parcel p
               JOIN meta.aoi a ON a.kind = %s AND a.code = %s AND a.site = %s AND ST_Intersects(p.geom, a.geom)
               -- One label per group: a code list may give a group two labels (PME in group 25
               -- since 2023), which would count its parcels twice
               LEFT JOIN (SELECT year, code_group, mode() WITHIN GROUP (ORDER BY label_group) AS label_group
                          FROM ref.rpg_crop_code GROUP BY year, code_group) c
                      ON c.year = p.year AND c.code_group = p.code_group
               WHERE p.year = %s
               GROUP BY 1, 2 ORDER BY area_ha DESC""",
            (a["kind"], a["code"], settings.site_code, year),
        ).fetchall()
    total = sum(r["area_ha"] or 0 for r in rows)
    return {"year": year, "area": {"code": a["code"], "name": a["name"], "kind": a["kind"]},
            "units": {"area": "ha"}, "total_ha": total, "groups": rows, "ilots": False}


def _ilot_summary(year: int, area: str | None) -> dict:
    with connect() as conn:
        a = _area(conn, area)
        labels = {r["code_group"]: r["label"] for r in conn.execute(
            """SELECT code_group, mode() WITHIN GROUP (ORDER BY label_group) AS label FROM ref.rpg_crop_code
               WHERE code_group IS NOT NULL GROUP BY 1""").fetchall()}
        rows = conn.execute(
            """SELECT i.code_group, min(i.group_label) AS ilot_label, count(*) AS parcels,
                      sum(ST_Area(CASE WHEN ST_CoveredBy(i.geom, a.geom) THEN i.geom
                                       ELSE ST_Intersection(i.geom, a.geom) END::geography)) / 1e4 AS area_ha
               FROM ref.rpg_ilot i
               JOIN meta.aoi a ON a.kind = %s AND a.code = %s AND a.site = %s AND ST_Intersects(i.geom, a.geom)
               WHERE i.year = %s GROUP BY 1 ORDER BY area_ha DESC""",
            (a["kind"], a["code"], settings.site_code, year)).fetchall()
    groups = [{"code_group": r["code_group"], "parcels": r["parcels"], "area_ha": r["area_ha"],
               "crop_group": labels.get(r["code_group"]) or (r["ilot_label"] or r["code_group"] or "").capitalize()}
              for r in rows]
    return {"year": year, "area": {"code": a["code"], "name": a["name"], "kind": a["kind"]},
            "units": {"area": "ha"}, "total_ha": sum(g["area_ha"] or 0 for g in groups), "groups": groups,
            "ilots": True}


# ---------------------------------------------------------------------------
# OSM
# ---------------------------------------------------------------------------
@app.get("/api/osm/{layer}.geojson")
def osm_geojson(layer: str) -> JSONResponse:
    if layer not in OSM_LAYERS:
        raise HTTPException(404, f"Unknown OSM layer {layer}")
    with connect() as conn:
        rows = conn.execute(
            """SELECT osm_id, layer, kind, name, snapshot, ST_AsGeoJSON(geom, 6) AS geojson
               FROM ref.osm_feature WHERE layer = %s AND site = %s""",
            (layer, settings.site_code),
        ).fetchall()
    for r in rows:
        r["snapshot"] = r["snapshot"].isoformat() if r["snapshot"] else None
    return _feature_collection(rows)


# ---------------------------------------------------------------------------
# Rasters
# ---------------------------------------------------------------------------
@app.get("/api/rasters")
def raster_catalogue() -> list[dict]:
    return rasters.catalogue()


def _raster(fn, name: str, var: str, *args):
    if name not in rasters.DATASETS:
        raise HTTPException(404, f"Unknown raster dataset {name}")
    try:
        return fn(name, var, *args)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (KeyError, IndexError) as exc:
        raise HTTPException(404, f"Unknown variable or depth {var} {args[:1]}") from exc


@app.get("/api/raster/{name}/{var}/info")
def raster_info(name: str, var: str, depth: int = 0) -> dict:
    """Legend, extent and tile URL template of a raster variable."""
    return _raster(rasters.info, name, var, depth)


@app.get("/api/raster/{name}/{var}/tiles/{z}/{x}/{y}.png")
def raster_tile(name: str, var: str, z: int, x: int, y: int, depth: int = 0) -> Response:
    if not (0 <= z <= 22 and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
        raise HTTPException(404, f"No tile {z}/{x}/{y}")
    png = _raster(rasters.tile, name, var, depth, z, x, y)
    # The URL carries the dataset's version (v=): a tile never changes
    return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=86400"})


@app.get("/api/raster/{name}/{var}.png")
def raster_png(name: str, var: str, depth: int = 0) -> Response:
    """The whole variable as one image (large for a 5 m DEM: the map uses the tiles)."""
    png = _raster(rasters.render, name, var, depth)
    return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=3600"})


# ---------------------------------------------------------------------------
# Point query
# ---------------------------------------------------------------------------
@app.get("/api/point")
def point(lon: float, lat: float) -> dict:
    out: dict = {"lon": lon, "lat": lat, "rasters": rasters.sample(lon, lat)}
    with connect() as conn:
        out["parcel"] = conn.execute(
            """SELECT p.parcel_id, p.year, p.code_cultu, c.label AS crop, c.label_group AS crop_group,
                      p.surf_ha, p.bio
               FROM ref.rpg_parcel p
               LEFT JOIN ref.rpg_crop_code c ON c.year = p.year AND c.code_culture = p.code_cultu
               WHERE ST_Intersects(p.geom, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
               ORDER BY p.year DESC LIMIT 1""",
            (lon, lat),
        ).fetchone()
        out["commune"] = conn.execute(
            """SELECT code, name FROM ref.admin_unit
               WHERE level = 'COM' AND valid_to IS NULL
                 AND ST_Intersects(geom, ST_SetSRID(ST_MakePoint(%s, %s), 4326)) LIMIT 1""",
            (lon, lat),
        ).fetchone()
    return out


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
# Units whose values can be summed over communes (counts). Rates, means, medians and
# amounts per person cannot: they are left empty for a group.
ADDITIVE_UNITS = {"person", "dwelling", "household", "count", "establishment"}


def _measure_concept(dims: dict) -> str | None:
    return next((k for k in dims if k.endswith("_MEASURE")), None)


def _is_total(dims: dict) -> bool:
    return all(v == "_T" for k, v in dims.items() if not k.endswith("_MEASURE"))


def _labels(conn, dataset: str) -> dict[tuple[str, str], str]:
    rows = conn.execute(
        "SELECT concept, code, COALESCE(label_fr, label_en, code) AS label FROM stats.code_label WHERE dataset_id = %s",
        (dataset,),
    ).fetchall()
    return {(r["concept"], r["code"]): r["label"] for r in rows}


def _concept_labels(conn, dataset: str) -> dict[str, str]:
    return {r["concept"]: r["label"] for r in conn.execute(
        "SELECT DISTINCT concept, COALESCE(concept_fr, concept) AS label FROM stats.code_label WHERE dataset_id = %s",
        (dataset,)).fetchall()}


def _observations(conn, dataset: str, level: str, code: str) -> list[dict]:
    """Observations of an area, values already scaled by their unit multiplier.

    ``level`` GRP: the AOI group. Its values are sums over the communes, for
    additive units only and when every commune has a value; otherwise ``value``
    is None and ``not_additive``, ``incomplete`` or ``conf_status`` says why.
    """
    cols = """time_period, measure, dims, value * power(10, unit_mult) AS value, unit, status,
              conf_status, geo_vintage, geo_code"""
    if level != "GRP":
        return conn.execute(
            f"""SELECT {cols} FROM stats.observation
                WHERE dataset_id = %s AND geo_level = %s AND geo_code = %s""",
            (dataset, level, code),
        ).fetchall()
    members = _area(conn, code)["members"]
    rows = conn.execute(
        f"""SELECT {cols} FROM stats.observation
            WHERE dataset_id = %s AND geo_level = 'COM' AND geo_code = ANY(%s)""",
        (dataset, members),
    ).fetchall()
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["time_period"], r["measure"], json.dumps(r["dims"], sort_keys=True)), []).append(r)
    out = []
    for (period, measure, _), rs in groups.items():
        first = rs[0]
        additive = first["unit"] in ADDITIVE_UNITS
        complete = len({r["geo_code"] for r in rs}) == len(members)
        confidential = any(r["value"] is None or r["conf_status"] not in (None, "", "F") for r in rs)
        value = sum(r["value"] for r in rs) if additive and complete and not confidential else None
        out.append({"time_period": period, "measure": measure, "dims": first["dims"], "value": value,
                    "unit": first["unit"], "status": None,
                    "conf_status": "C" if confidential and additive and complete else None,
                    "geo_vintage": first["geo_vintage"], "not_additive": not additive,
                    "incomplete": additive and not complete})
    return out


def _latest_period(rows: list[dict]) -> str | None:
    periods = [r["time_period"] for r in rows if r["value"] is not None] or [r["time_period"] for r in rows]
    return max(periods) if periods else None


@app.get("/api/stats/datasets")
def stats_datasets() -> list[dict]:
    with connect() as conn:
        return conn.execute(
            """SELECT d.id, d.source, d.title_fr, d.title_en, d.url, d.fetched_at,
                      count(o.id) AS observations,
                      min(o.time_period) AS first_period, max(o.time_period) AS last_period
               FROM stats.dataset d LEFT JOIN stats.observation o ON o.dataset_id = d.id
               GROUP BY d.id ORDER BY d.id"""
        ).fetchall()


MAX_BREAKDOWN_ROWS = 25


def _default_geo(conn, level: str, code: str | None) -> tuple[str, str]:
    if code:
        return level, code
    site = _site(conn)
    return ("GRP", site["code"]) if len(site["members"]) > 1 else ("COM", site["members"][0])


def _item(r: dict, label: str) -> dict:
    return {
        "measure": r["measure"], "label": label, "value": r["value"], "unit": r["unit"],
        "confidential": r["value"] is None and not r.get("not_additive") and not r.get("incomplete"),
        "not_additive": bool(r.get("not_additive")), "incomplete": bool(r.get("incomplete")),
    }


@app.get("/api/stats/summary")
def stats_summary(level: str = "COM", code: str | None = None) -> list[dict]:
    """Key indicators for the latest period of each dataset.

    ``indicators``: totals (every breakdown dimension = '_T').
    ``breakdowns``: one table per dimension, when only that dimension is broken down.
    ``level`` GRP sums the AOI communes (counts only).
    """
    from ..ingest.insee import DATASETS as ORDER

    out = []
    with connect() as conn:
        level, code = _default_geo(conn, level, code)
        datasets = conn.execute("SELECT id, title_fr FROM stats.dataset").fetchall()
        datasets.sort(key=lambda d: ORDER.index(d["id"]) if d["id"] in ORDER else len(ORDER))
        for d in datasets:
            allrows = _observations(conn, d["id"], level, code)
            latest = _latest_period(allrows)
            rows = [r for r in allrows if r["time_period"] == latest]
            if not rows:
                continue
            labels = _labels(conn, d["id"])
            concept_labels = _concept_labels(conn, d["id"])
            totals, breakdowns = [], {}
            for r in rows:
                mconcept = _measure_concept(r["dims"])
                broken = [k for k, v in r["dims"].items() if not k.endswith("_MEASURE") and v != "_T"]
                item = _item(r, labels.get((mconcept, r["measure"]), r["measure"]) if mconcept else r["measure"])
                if not broken:
                    totals.append(item)
                elif len(broken) == 1:
                    k = broken[0]
                    b = breakdowns.setdefault((r["measure"], k), {
                        "measure_label": item["label"], "concept": k,
                        "concept_label": concept_labels.get(k, k), "rows": []})
                    b["rows"].append(item | {"label": labels.get((k, r["dims"][k]), r["dims"][k])})
            tables = [b for b in breakdowns.values()
                      if len(b["rows"]) <= MAX_BREAKDOWN_ROWS and any(x["value"] is not None for x in b["rows"])]
            for b in tables:
                b["rows"].sort(key=lambda x: str(x["label"]))
            out.append({"dataset": d["id"], "title": d["title_fr"], "time_period": latest,
                        "geo_vintage": rows[0]["geo_vintage"], "level": level, "code": code,
                        "indicators": sorted(totals, key=lambda x: str(x["label"])),
                        "breakdowns": tables})
    return out


@app.get("/api/stats/series")
def stats_series(dataset: str = "DS_POPULATIONS_HISTORIQUES", measure: str | None = None,
                 level: str = "COM", code: str | None = None) -> dict:
    with connect() as conn:
        level, code = _default_geo(conn, level, code)
        rows = [r for r in _observations(conn, dataset, level, code)
                if _is_total(r["dims"]) and (measure is None or r["measure"] == measure)]
        labels = _labels(conn, dataset)
    rows.sort(key=lambda r: r["time_period"])
    series: dict[str, dict] = {}
    for r in rows:
        s = series.setdefault(r["measure"] or "", {
            "measure": r["measure"],
            "label": next((lab for (c, k), lab in labels.items() if k == r["measure"] and c.endswith("_MEASURE")),
                          r["measure"]),
            "unit": r["unit"], "points": []})
        s["points"].append({"time": r["time_period"], "value": r["value"]})
    return {"dataset": dataset, "level": level, "code": code, "series": list(series.values())}


@app.get("/api/stats/table")
def stats_table(dataset: str, level: str = "COM", code: str | None = None,
                time: str | None = None) -> dict:
    """All observations of a dataset for one area and period, with labelled dimensions."""
    with connect() as conn:
        level, code = _default_geo(conn, level, code)
        allrows = _observations(conn, dataset, level, code)
        periods = sorted({r["time_period"] for r in allrows})
        time = time or _latest_period(allrows)
        rows = [r for r in allrows if r["time_period"] == time]
        labels = _labels(conn, dataset)
        concepts = _concept_labels(conn, dataset)
    # Keep only dimensions that vary in this table
    keys = sorted({k for r in rows for k in r["dims"]})
    varying = [k for k in keys if len({r["dims"].get(k) for r in rows}) > 1]
    table = []
    for r in rows:
        table.append({
            "dims": {k: labels.get((k, r["dims"].get(k)), r["dims"].get(k)) for k in varying},
            "value": r["value"], "unit": r["unit"],
            "not_additive": bool(r.get("not_additive")), "incomplete": bool(r.get("incomplete")),
        })
    table.sort(key=lambda x: [str(x["dims"].get(k)) for k in varying])
    return {"dataset": dataset, "level": level, "code": code, "time_period": time, "periods": periods,
            "columns": [{"key": k, "label": concepts.get(k, k)} for k in varying], "rows": table}


# ---------------------------------------------------------------------------
# Units and housekeeping
# ---------------------------------------------------------------------------
@app.get("/api/units/convert")
def convert_units(value: float, from_unit: str = Query(alias="from"), to_unit: str = Query(alias="to")) -> dict:
    try:
        return {"value": value, "from": from_unit, "to": to_unit,
                "result": units.convert(value, from_unit, to_unit)}
    except Exception as exc:
        raise HTTPException(400, f"Cannot convert {from_unit} to {to_unit}: {exc}") from exc


@app.get("/api/status")
def status() -> dict:
    with connect() as conn:
        log = conn.execute(
            """SELECT DISTINCT ON (step) step, status, finished_at, message, details
               FROM meta.ingest_log WHERE site = %s ORDER BY step, finished_at DESC""", (settings.site_code,)
        ).fetchall()
    return {"version": __version__, "aoi": settings.site_code, "unit": settings.unit or None,
            "communes": list(settings.communes),
            "ingestion": log}


@lru_cache(maxsize=4)
def _index_html(mtimes: tuple) -> str:
    """index.html with versioned links to its script and style sheet (app.js?v=<hash>): after an
    update, the browser cannot run a cached script with the new page."""
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    for name in ("app.js", "style.css"):
        v = hashlib.sha1((WEB_DIR / name).read_bytes()).hexdigest()[:10]
        html = html.replace(f'"{name}"', f'"{name}?v={v}"')
    return html


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
def index() -> HTMLResponse:
    mtimes = tuple((WEB_DIR / f).stat().st_mtime for f in ("index.html", "app.js", "style.css"))
    return HTMLResponse(_index_html(mtimes), headers={"Cache-Control": "no-cache"})


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
