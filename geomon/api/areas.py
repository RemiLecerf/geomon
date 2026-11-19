"""The analysis areas of the current site (``meta.aoi``): the group, its communes, the farm's plots."""

from __future__ import annotations

import json

from fastapi import HTTPException

from ..config import settings
from ..db import connect


def site_row(conn) -> dict:
    """The current site: code, name, members (INSEE codes), download extent (GeoJSON)."""
    row = conn.execute(
        "SELECT code, name, members, ST_AsGeoJSON(bbox) AS bbox FROM meta.aoi WHERE kind = 'site' AND code = %s",
        (settings.site_code,),
    ).fetchone()
    if not row:
        raise HTTPException(503, "No AOI yet: run the ingestion (docker compose run --rm ingest)")
    return row


def area(conn, code: str | None) -> dict:
    """An analysis area: the site (group) by default, one of its communes, or ``farm``.

    Returns code, name, kind ('site', 'commune' or 'farm'), members (INSEE codes), area in ha,
    is_group and the outline (``geometry``, GeoJSON).
    """
    site = site_row(conn)
    if code in (None, "", site["code"]):
        kind, code = "site", site["code"]
    elif code in site["members"]:
        kind = "commune"
    elif code == "farm":
        kind = "farm"
    else:
        raise HTTPException(404, f"Area {code} is not part of the AOI ({', '.join(site['members'])})")
    row = conn.execute(
        """SELECT code, name, kind, members, ST_AsGeoJSON(geom) AS geojson,
                  ST_Area(geom::geography) / 1e4 AS area_ha
           FROM meta.aoi WHERE kind = %s AND code = %s AND site = %s""",
        (kind, code, site["code"]),
    ).fetchone()
    if not row:
        raise HTTPException(404, f"Area {code} is not loaded (run the ingestion)")
    row["geometry"] = json.loads(row.pop("geojson"))
    row["is_group"] = kind == "site" and len(row["members"]) > 1
    return row


def get_area(code: str | None) -> dict:
    with connect() as conn:
        return area(conn, code)
