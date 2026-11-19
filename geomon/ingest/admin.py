"""Administrative hierarchy from IGN ADMIN EXPRESS (COG vintage), with temporal validity.

Hierarchy stored for the AOI:
    COM (+ neighbouring communes) -> ARR -> DEP -> REG -> FRANCE
    EPCI (intercommunalité) is stored alongside; it does not nest in ARR.
"""

from __future__ import annotations

import json
import logging

from ..aoi import AOI
from ..config import settings
from ..db import connect
from ..http import wfs_features

log = logging.getLogger(__name__)

SOURCE = "IGN ADMIN EXPRESS COG"


def _layer(name: str) -> str:
    return f"ADMINEXPRESS-COG.{settings.cog_vintage}:{name}"


def _row(level, code, name, parent_level, parent_code, props, geometry):
    return {
        "level": level,
        "code": code,
        "name": name,
        "parent_level": parent_level,
        "parent_code": parent_code,
        "props": props,
        "geom": json.dumps(geometry) if geometry else None,
    }


def run(aoi: AOI) -> dict:
    rows: list[dict] = []

    # Communes: the AOI communes and their neighbours within the download extent
    communes = wfs_features(_layer("commune"), bbox_wgs84=aoi.bbox_wgs84)
    have = {f["properties"]["code_insee"] for f in communes}
    for code in aoi.codes:
        if code not in have:
            communes += wfs_features(_layer("commune"), cql=f"code_insee='{code}'")
    for f in communes:
        p = f["properties"]
        rows.append(
            _row("COM", p["code_insee"], p["nom_officiel"], "ARR",
                 p.get("code_insee_de_l_arrondissement"), p, f["geometry"])
        )

    for epci in aoi.epcis:
        for f in wfs_features(_layer("epci"), cql=f"code_siren='{epci}'"):
            p = f["properties"]
            deps = (p.get("codes_insee_des_departements_membres") or "").split("/")
            rows.append(
                _row("EPCI", p["code_siren"], p["nom_officiel"],
                     "DEP" if len(deps) == 1 else None, deps[0] if len(deps) == 1 else None,
                     p, f["geometry"])
            )

    for arr in aoi.arrondissements:
        for f in wfs_features(_layer("arrondissement"), cql=f"code_insee='{arr}'"):
            p = f["properties"]
            rows.append(_row("ARR", p["code_insee"], p["nom_officiel"], "DEP",
                             p["code_insee_du_departement"], p, f["geometry"]))

    for f in (f for dep in aoi.departements for f in wfs_features(_layer("departement"), cql=f"code_insee='{dep}'")):
        p = f["properties"]
        rows.append(_row("DEP", p["code_insee"], p["nom_officiel"], "REG",
                         p["code_insee_de_la_region"], p, f["geometry"]))

    for f in (f for reg in aoi.regions for f in wfs_features(_layer("region"), cql=f"code_insee='{reg}'")):
        p = f["properties"]
        rows.append(_row("REG", p["code_insee"], p["nom_officiel"], "FRANCE", "F", p, f["geometry"]))

    rows.append(_row("FRANCE", "F", "France", None, None, {}, None))

    valid_from = f"{settings.cog_vintage}-01-01"
    with connect() as conn:
        for r in rows:
            conn.execute(
                """
                INSERT INTO ref.admin_unit
                    (level, code, name, parent_level, parent_code, vintage,
                     valid_from, valid_to, source, props, geom)
                VALUES (%(level)s, %(code)s, %(name)s, %(parent_level)s, %(parent_code)s,
                        %(vintage)s, %(valid_from)s, NULL, %(source)s, %(props)s::jsonb,
                        CASE WHEN %(geom)s::text IS NULL THEN NULL
                             ELSE ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%(geom)s::text), 4326)) END)
                ON CONFLICT (level, code, vintage) DO UPDATE SET
                    name = EXCLUDED.name, parent_level = EXCLUDED.parent_level,
                    parent_code = EXCLUDED.parent_code, props = EXCLUDED.props,
                    geom = EXCLUDED.geom, source = EXCLUDED.source
                """,
                {**r, "props": json.dumps(r["props"]), "vintage": settings.cog_vintage,
                 "valid_from": valid_from, "source": SOURCE},
            )
        # Close the validity of older vintages of the same units
        conn.execute(
            """
            UPDATE ref.admin_unit old
               SET valid_to = new.valid_from
              FROM ref.admin_unit new
             WHERE old.level = new.level AND old.code = new.code
               AND old.vintage < new.vintage
               AND (old.valid_to IS NULL OR old.valid_to > new.valid_from)
            """
        )
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["level"]] = counts.get(r["level"], 0) + 1
    log.info("Admin units stored: %s", counts)
    return counts
