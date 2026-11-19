"""The farm's own plots (a PAC declaration exported as GeoJSON) as an analysis area.

GEOMON_FARM_FILE: GeoJSON of the plots (WGS84 / CRS84), absolute or relative to
the data directory, e.g. ``farm/parcellaire_pac_wgs84.geojson``. Each feature is a
plot or an island (îlot) of the declaration; the attributes of the PAC export
(CAMPAGNE, PACAGE, NUMERO, ILOT_REF, COMMUNE, SURF_TOT) are kept when present.

The plots are stored in ``ref.farm_plot``, and their union as an area of kind
``farm`` in ``meta.aoi``: everything computed per area (land summary, weather,
Sentinel-2 series, crop groups of the RPG parcels lying in the plots) is then
available for the farm too. Without GEOMON_FARM_FILE the step does nothing.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..aoi import AOI
from ..config import settings
from ..db import connect

log = logging.getLogger(__name__)

FARM_CODE = "farm"


def farm_path() -> Path | None:
    if not settings.farm_file:
        return None
    p = Path(settings.farm_file)
    return p if p.is_absolute() else settings.data_dir / p


def run(aoi: AOI) -> dict:
    path = farm_path()
    if path is None:
        with connect() as conn:            # no farm (any more) for this site
            conn.execute("DELETE FROM meta.aoi WHERE site = %s AND kind = 'farm'", (aoi.code,))
        return {"skipped": "no farm file for this site"}
    if not path.exists():
        raise FileNotFoundError(f"Farm plots file {path} not found (farm_file in sites.json, or GEOMON_FARM_FILE); "
                                "add the file or remove the setting")
    data = json.loads(path.read_text(encoding="utf-8"))
    feats = [f for f in data.get("features", []) if f.get("geometry")]
    if not feats:
        raise ValueError(f"{path}: no feature with a geometry")

    def get(p, *names):
        return next((p[n] for n in names if p.get(n) not in (None, "")), None)

    with connect() as conn:
        conn.execute("DELETE FROM ref.farm_plot WHERE site = %s", (aoi.code,))
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO ref.farm_plot (site, campaign, number, island, commune, surf_ha, props, geom)
                   VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb,
                           ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)), 3)))""",
                [(aoi.code, get(p, "CAMPAGNE", "campagne"), str(get(p, "NUMERO", "numero", "num_ilot") or i + 1),
                  get(p, "ILOT_REF", "ilot_ref"), get(p, "COMMUNE", "commune"),
                  get(p, "SURF_TOT", "surf_tot", "surface"), json.dumps(p), json.dumps(f["geometry"]))
                 for i, f in enumerate(feats) for p in [f.get("properties") or {}]],
            )
        r = conn.execute(
            """SELECT count(*) AS n, sum(surf_ha) AS ha, max(campaign) AS campaign,
                      array_agg(DISTINCT commune) FILTER (WHERE commune IS NOT NULL) AS communes,
                      count(*) FILTER (WHERE NOT ST_Intersects(f.geom, a.geom)) AS outside
               FROM ref.farm_plot f, (SELECT geom FROM meta.aoi WHERE kind = 'site' AND code = %s) a
               WHERE f.site = %s""",
            (aoi.code, aoi.code)).fetchone()
        name = settings.farm_name or (f"My plots (PAC {r['campaign']})" if r["campaign"] else "My plots")
        conn.execute("DELETE FROM meta.aoi WHERE site = %s AND kind = 'farm'", (aoi.code,))
        conn.execute(
            """INSERT INTO meta.aoi (code, name, kind, site, members, position, buffer_m, geom, bbox, updated_at)
               SELECT %s, %s, 'farm', %s, %s, 1000, %s, ST_Multi(ST_Union(geom)),
                      (SELECT bbox FROM meta.aoi WHERE kind = 'site' AND code = %s), now()
               FROM ref.farm_plot WHERE site = %s""",
            (FARM_CODE, name, aoi.code, sorted(r["communes"] or []), settings.aoi_buffer_m, aoi.code, aoi.code))
    if r["outside"]:
        log.warning("Farm: %d plot(s) outside the AOI communes (GEOMON_COMMUNES): no data there", r["outside"])
    log.info("Farm: %d plots, %.1f ha declared, communes %s", r["n"], r["ha"] or 0, ", ".join(r["communes"] or []))
    return {"plots": r["n"], "ha": r["ha"], "campaign": r["campaign"], "outside_aoi": r["outside"]}
