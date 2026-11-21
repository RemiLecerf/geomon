"""Soil and geology maps of the site (ref.soil_feature), three layers:

- ``soils``: the dominant soils of metropolitan France, harmonised map of the Référentiels Régionaux
  Pédologiques at 1:250 000 (INRAE / Gis Sol), IGN Géoplateforme WFS
  ``INRAE_CARTE-SOLS:sols_dominants_france_metropolitaine``: soil mapping units (UCS) with their
  description and dominant soil (Référentiel Pédologique) and its share.
- ``geology``: the harmonised 1:50 000 geological map (BRGM BD Charm-50), one archive per département
  on InfoTerre; only the site's extent is read from the zip archive (HTTP range requests).
- ``clay``: the exposure to clay shrink-swell (BRGM, ``ms:ALEARG_REALISE`` WFS of geoservices.brgm.fr;
  its ``id`` is the exposure level, 1 low, 2 medium, 3 high, as Géorisques's API gives it; outside
  the polygons the ground is a priori not clayey).
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess

from ..aoi import AOI
from ..db import connect
from ..http import session, wfs_features

log = logging.getLogger(__name__)

SOILS_LAYER = "INRAE_CARTE-SOLS:sols_dominants_france_metropolitaine"
GEOLOGY_ZIP = "https://infoterre.brgm.fr/telechargements/BDCharm50/GEO050K_HARM_{dep}.zip"
BRGM_RISKS_WFS = "https://geoservices.brgm.fr/risques"
CLAY_LEVELS = {"1": ("Low", "#f6d55c"), "2": ("Medium", "#ed8b3d"), "3": ("High", "#c8323a")}

# Dominant soils (Référentiel Pédologique references): colours close to the usual soil map legends
SOIL_COLORS = {
    "BRUNISOL": "#c8a165", "ALOCRISOL": "#b07c4f", "LUVISOL": "#d9a86c", "NEOLUVISOL": "#e0b77e",
    "PLANOSOL": "#9fa7c7", "REDOXISOL": "#7fa7c9", "REDUCTISOL": "#4f7fb0", "PODZOSOL": "#9a8fb5",
    "CALCOSOL": "#e9dfa3", "CALCISOL": "#f0e6b5", "RENDOSOL": "#f3eccb", "RENDISOL": "#efe4b9",
    "COLLUVIOSOL": "#c9b68f", "FLUVIOSOL": "#a9cfc4", "THALASSOSOL": "#8fc3c9", "ARENOSOL": "#f2e3a0",
    "RANKOSOL": "#8c8c6e", "LITHOSOL": "#a8a8a0", "REGOSOL": "#c2c2b4", "PEYROSOL": "#b5ab94",
    "HISTOSOL": "#5b4a3a", "VERTISOL": "#6b5a4e", "ANDOSOL": "#4d4d4d", "FERSIALSOL": "#c1694f",
    "ORGANOSOL": "#7a6a4f", "VERACRISOL": "#bf9a6a", "SALISOL": "#d8d0e4", "SODISOL": "#cfc6dd",
    "ANTHROPOSOL": "#bdbdbd", "PELOSOL": "#9e7b62", "LEPTISMECTISOL": "#8d7a6c",
}


def _hash_color(text: str) -> str:
    h = hashlib.md5(text.encode()).digest()
    return "#%02x%02x%02x" % (90 + h[0] % 140, 90 + h[1] % 140, 90 + h[2] % 140)


def _cmyk(c, m, y, k) -> str:
    """BRGM's CMYK percentages -> #rrggbb."""
    c, m, y, k = (min(max(float(v or 0), 0), 100) / 100 for v in (c, m, y, k))
    return "#%02x%02x%02x" % tuple(round(255 * (1 - v) * (1 - k)) for v in (c, m, y))


def _soils(aoi: AOI) -> list[tuple]:
    rows = []
    for f in wfs_features(SOILS_LAYER, bbox_wgs84=aoi.bbox_wgs84):
        p = f["properties"]
        ref = (p.get("nom_sol_dominant") or "").strip().upper()
        share = p.get("pourcent_sol_dominant")
        label = f"{ref.capitalize()}{f' ({share} %)' if share is not None else ''}" if ref else "Not described"
        rows.append(("soils", ref or None, label, p.get("nom_ucs"), SOIL_COLORS.get(ref) or _hash_color(ref or "?"),
                     p, f["geometry"]))
    return rows


def _geology(aoi: AOI) -> list[tuple]:
    rows = []
    x0, y0, x1, y1 = aoi.bbox_l93
    for dep in aoi.departements:
        d = f"{int(dep):03d}" if dep.isdigit() else dep.upper()      # 2A / 2B: Corsica
        src = f"/vsizip//vsicurl/{GEOLOGY_ZIP.format(dep=d)}/GEO050K_HARM_{d}_S_FGEOL_2154.shp"
        out = subprocess.run(
            ["ogr2ogr", "-f", "GeoJSON", "/vsistdout/", src, "-spat", str(x0), str(y0), str(x1), str(y1),
             "-t_srs", "EPSG:4326", "-select", "NOTATION,DESCR,CODE,CODE_LEG,C_FOND,M_FOND,J_FOND,N_FOND"],
            capture_output=True)
        if out.returncode:
            log.warning("Geology %s not read: %s", d, out.stderr.decode(errors="replace")[:300])
            continue
        for f in json.loads(out.stdout).get("features", []):
            p = f["properties"]
            # The notation is written for a BRGM symbol font (ã, ¡...): the description is the label
            rows.append(("geology", str(p.get("CODE_LEG") or p.get("CODE") or ""), p.get("DESCR"), p.get("NOTATION"),
                         _cmyk(p.get("C_FOND"), p.get("M_FOND"), p.get("J_FOND"), p.get("N_FOND")), p, f["geometry"]))
    return rows


def _clay(aoi: AOI) -> list[tuple]:
    x0, y0, x1, y1 = aoi.bbox_wgs84
    params = {"SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature", "TYPENAMES": "ms:ALEARG_REALISE",
              "BBOX": f"{y0},{x0},{y1},{x1},urn:ogc:def:crs:EPSG::4326", "OUTPUTFORMAT": "geojson",
              "PROPERTYNAME": "id,id_zone_rga,surf_m2", "COUNT": 10000}
    r = session().get(BRGM_RISKS_WFS, params=params, timeout=300)
    r.raise_for_status()
    rows = []
    for f in r.json().get("features", []):
        level = str(f["properties"].get("id"))
        label, color = CLAY_LEVELS.get(level, (f"Level {level}", "#999999"))
        rows.append(("clay", level, label, None, color, f["properties"], f["geometry"]))
    return rows


def run(aoi: AOI) -> dict:
    out, errors = {}, []
    for layer, fn in (("soils", _soils), ("geology", _geology), ("clay", _clay)):
        try:
            rows = fn(aoi)
        except Exception as exc:
            log.error("Soil maps: %s not loaded: %s", layer, exc)
            errors.append(f"{layer}: {exc}")
            continue
        with connect() as conn:
            conn.execute("DELETE FROM ref.soil_feature WHERE site = %s AND layer = %s", (aoi.code, layer))
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO ref.soil_feature (site, layer, code, label, detail, color, props, geom)
                       VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, ST_Multi(ST_CollectionExtract(
                               ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)), 3)))""",
                    [(aoi.code, la, code, label, detail, color, json.dumps(p, default=str), json.dumps(g))
                     for la, code, label, detail, color, p, g in rows if g])
        out[layer] = len(rows)
        log.info("Soil maps %s: %d polygons", layer, len(rows))
    if errors and not out:
        raise RuntimeError("; ".join(errors))
    return {"polygons": out, "errors": errors}
