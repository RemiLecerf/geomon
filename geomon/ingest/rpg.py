"""RPG parcels (Registre parcellaire graphique) from the IGN Géoplateforme WFS.

Every year from GEOMON_RPG_FIRST_YEAR (2015, the first year of the current
nomenclature and of Sentinel-2) to GEOMON_RPG_YEAR is loaded: the latest from
GEOMON_RPG_LAYER (RPG.LATEST), earlier ones from the layers RPG.<year>. The
crop history of a parcel and the crop-group NDVI references need them all.

Parcel identifiers (id_parcel) are only unique within one year: parcels are
renumbered and their outlines change from year to year.
"""

from __future__ import annotations

import json
import logging
import re

from ..aoi import AOI
from ..config import settings
from ..db import connect
from ..http import wfs_features, wfs_layer_title

log = logging.getLogger(__name__)


def _check_year(layer: str, year: int) -> None:
    """Make sure e.g. RPG.LATEST really holds the configured year."""
    title = wfs_layer_title(f"{layer}:parcelles_graphiques") or ""
    years = re.findall(r"\b(20\d\d)\b", title)
    if years and int(years[0]) != year:
        raise RuntimeError(
            f"{layer} holds RPG {years[0]} ('{title}'), not {year}. "
            "Set GEOMON_RPG_LAYER / GEOMON_RPG_YEAR accordingly."
        )
    log.info("RPG layer %s: %s", layer, title or "(title not found)")


def _layer(year: int) -> str:
    return settings.rpg_layer if year == settings.rpg_year else f"RPG.{year}"


def _codes(year: int) -> tuple[list[dict], int | None]:
    """Crop code list of a year. Layers before 2022 have none: the nearest later list is used
    (the nomenclature has been stable since 2015)."""
    for y in range(year, settings.rpg_year + 1):
        layer = _layer(y)
        if wfs_layer_title(f"{layer}:codes_cultures") is not None:
            return wfs_features(f"{layer}:codes_cultures"), y
    return [], None


def _code_row(year: int, p: dict) -> tuple:
    """The field names differ between years (code / code_culture, libelle_groupe / ...)."""
    def get(*names):
        return next((p[n] for n in names if p.get(n) not in (None, "")), None)
    return (year, get("code_culture", "code"), get("libelle_culture", "libelle"),
            get("code_groupe_culture", "code_groupe"), get("libelle_groupe_culture", "libelle_groupe"))


def _fill_groups(conn) -> None:
    """Some code lists (2024) have no crop group: take it from the nearest year that has it."""
    conn.execute(
        """UPDATE ref.rpg_crop_code c SET (code_group, label_group) = (
               SELECT o.code_group, o.label_group FROM ref.rpg_crop_code o
               WHERE o.code_culture = c.code_culture AND o.code_group IS NOT NULL
               ORDER BY abs(o.year - c.year), o.year DESC LIMIT 1)
           WHERE c.code_group IS NULL""")


def _load_year(aoi: AOI, year: int) -> dict:
    layer = _layer(year)
    if year == settings.rpg_year:
        _check_year(layer, year)
    elif wfs_layer_title(f"{layer}:parcelles_graphiques") is None:
        log.warning("RPG %d: no layer %s on the WFS, skipped", year, layer)
        return {"year": year, "parcels": 0, "skipped": True}
    codes, codes_year = _codes(year)
    parcels = wfs_features(f"{layer}:parcelles_graphiques", bbox_wgs84=aoi.bbox_wgs84)

    with connect() as conn:
        conn.execute("DELETE FROM ref.rpg_crop_code WHERE year = %s", (year,))
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO ref.rpg_crop_code (year, code_culture, label, code_group, label_group)
                   VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                [_code_row(year, f["properties"]) for f in codes],
            )

        # Parcels of this site's extent only: the other sites' parcels stay
        conn.execute(
            """DELETE FROM ref.rpg_parcel WHERE year = %s
               AND geom && (SELECT bbox FROM meta.aoi WHERE kind = 'site' AND code = %s)""",
            (year, aoi.code))
        with conn.cursor() as cur:
            have = {r["parcel_id"] for r in cur.execute(
                "SELECT parcel_id FROM ref.rpg_parcel WHERE year = %s", (year,)).fetchall()}
            parcels = [f for f in parcels
                       if str(f["properties"].get("num_parcel") or f["properties"].get("id_parcel") or f.get("id"))
                       not in have]       # already loaded with another site whose extent overlaps
            cur.executemany(
                """INSERT INTO ref.rpg_parcel
                       (year, parcel_id, code_cultu, code_group, cat_cult_p, surf_ha, bio, props, geom)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb,
                           ST_Multi(ST_CollectionExtract(
                               ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)), 3)))""",
                [
                    (
                        year,
                        str(p.get("num_parcel") or p.get("id_parcel") or f.get("id")),
                        p.get("code_cultu"),
                        p.get("code_group"),
                        p.get("cat_cult_p"),
                        p.get("surf_parc"),
                        p.get("bio"),
                        json.dumps(p),
                        json.dumps(f["geometry"]),
                    )
                    for f in parcels
                    if f.get("geometry")
                    for p in [f["properties"]]
                ],
            )
    note = "" if codes_year in (year, None) else f" (crop labels of {codes_year})"
    log.info("RPG %s: %d parcels, %d crop codes%s", year, len(parcels), len(codes), note)
    return {"year": year, "parcels": len(parcels), "crop_codes": len(codes), "crop_codes_year": codes_year}


def run(aoi: AOI) -> dict:
    years = list(range(min(settings.rpg_first_year, settings.rpg_year), settings.rpg_year + 1))
    out = [_load_year(aoi, y) for y in years]
    with connect() as conn:
        _fill_groups(conn)
    return {"years": [r["year"] for r in out if r.get("parcels")],
            "parcels": {r["year"]: r["parcels"] for r in out}}
