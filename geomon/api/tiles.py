"""Vector tiles (Mapbox Vector Tiles, made by PostGIS) of the large map layers: RPG parcels and
OpenStreetMap. The map downloads the tiles in view only, instead of whole GeoJSON layers of tens
of MB. The GeoJSON endpoints stay, for other clients (QGIS...)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from ..config import settings
from ..db import connect

router = APIRouter(prefix="/api/tiles", tags=["tiles"])

OSM_LAYERS = {"roads", "railways", "waterways", "buildings", "landuse", "water", "pois"}
MVT = "application/vnd.mapbox-vector-tile"
EXTENT, BUFFER = 4096, 64

# Tile envelope (Web Mercator) and the same with the buffer, in WGS84, for the index search
_TILE = f"""t AS (SELECT ST_TileEnvelope(%(z)s, %(x)s, %(y)s) AS env,
                         ST_Transform(ST_TileEnvelope(%(z)s, %(x)s, %(y)s, margin => {BUFFER / EXTENT}), 4326) AS box)"""
_GEOM = f"ST_AsMVTGeom(ST_Transform({{g}}, 3857), t.env, {EXTENT}, {BUFFER}, true) AS geom"

_RPG_SQL = f"""
WITH {_TILE},
a AS (SELECT geom, bbox FROM meta.aoi WHERE kind = 'site' AND code = %(site)s),
f AS (
    SELECT p.id, p.year, p.parcel_id, p.code_cultu, c.label AS crop, p.code_group, c.label_group AS crop_group,
           p.cat_cult_p, p.surf_ha, p.bio, ST_Intersects(p.geom, a.geom) AS in_commune,
           {_GEOM.format(g="p.geom")}
    FROM ref.rpg_parcel p CROSS JOIN t CROSS JOIN a
    LEFT JOIN ref.rpg_crop_code c ON c.year = p.year AND c.code_culture = p.code_cultu
    WHERE p.year = %(year)s AND p.geom && t.box AND p.geom && a.bbox)
SELECT ST_AsMVT(f.*, 'rpg', {EXTENT}, 'geom') AS mvt FROM f WHERE geom IS NOT NULL
"""

_OSM_SQL = f"""
WITH {_TILE},
f AS (
    SELECT o.osm_id, o.kind, o.name, {_GEOM.format(g="o.geom")}
    FROM ref.osm_feature o CROSS JOIN t
    WHERE o.site = %(site)s AND o.layer = %(layer)s AND o.geom && t.box)
SELECT ST_AsMVT(f.*, %(layer)s, {EXTENT}, 'geom') AS mvt FROM f WHERE geom IS NOT NULL
"""


def _check(z: int, x: int, y: int) -> None:
    if not (0 <= z <= 22 and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
        raise HTTPException(404, f"No tile {z}/{x}/{y}")


def tile(layer: str, z: int, x: int, y: int, year: int | None = None) -> bytes:
    """One tile of a layer of the current site (empty bytes when it holds no feature)."""
    _check(z, x, y)
    params = {"z": z, "x": x, "y": y, "site": settings.site_code}
    if layer == "rpg":
        sql, params["year"] = _RPG_SQL, year or settings.rpg_year
    elif layer in OSM_LAYERS:
        sql, params["layer"] = _OSM_SQL, layer
    else:
        raise HTTPException(404, f"Unknown tile layer {layer}")
    with connect() as conn:
        row = conn.execute(sql, params).fetchone()
    return bytes(row["mvt"] or b"")


@router.get("/{layer}/{z}/{x}/{y}.pbf")
def vector_tile(layer: str, z: int, x: int, y: int, year: int | None = None) -> Response:
    """A vector tile: ``rpg`` (parcels of ``year``, default the latest RPG) or an OSM layer."""
    return Response(tile(layer, z, x, y, year), media_type=MVT, headers={"Cache-Control": "max-age=600"})
