"""Groundwater levels near the site: Hub'Eau, API "niveaux des nappes" (ADES network), no account.

The piezometers within RADIUS_KM of the site's centre that are still measured (a measure in the last
year) are followed, the MAX_STATIONS nearest. Their whole daily record is fetched once, then the days
published since (incremental). Stored in ref.gw_station, eo.gw_level (depth of the water below
ground and level in m NGF) and ref.gw_site (the stations of each site and their distance).

A piezometer measures one aquifer at its place: a station 20 km away may sit in another aquifer than
the site's soils (the distance and the aquifer are shown with each station).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math

from ..aoi import AOI
from ..db import connect
from ..http import session

log = logging.getLogger(__name__)

API = "https://hubeau.eaufrance.fr/api/v1/niveaux_nappes"
RADIUS_KM = 25
MAX_STATIONS = 6


def _centre(aoi: AOI) -> tuple[float, float]:
    x0, y0, x1, y1 = aoi.bbox_wgs84
    return (x0 + x1) / 2, (y0 + y1) / 2


def _km(lon, lat, lon0, lat0) -> float:
    return 111.2 * math.hypot((lon - lon0) * math.cos(math.radians(lat0)), lat - lat0)


def stations(aoi: AOI) -> list[dict]:
    """The followed stations: active, within RADIUS_KM, the nearest first."""
    lon0, lat0 = _centre(aoi)
    dlat = RADIUS_KM / 111.2
    dlon = dlat / math.cos(math.radians(lat0))
    r = session().get(f"{API}/stations", timeout=120, params={
        "bbox": f"{lon0 - dlon},{lat0 - dlat},{lon0 + dlon},{lat0 + dlat}", "size": 500, "format": "json"})
    r.raise_for_status()
    year_ago = (dt.date.today() - dt.timedelta(days=365)).isoformat()
    found = []
    for s in r.json().get("data", []):
        if not s.get("date_fin_mesure") or s["date_fin_mesure"] < year_ago or s.get("x") is None:
            continue
        d = _km(s["x"], s["y"], lon0, lat0)
        if d <= RADIUS_KM:
            found.append(s | {"distance_km": round(d, 1)})
    return sorted(found, key=lambda s: s["distance_km"])[:MAX_STATIONS]


def _levels(code_bss: str, since: str | None) -> list[dict]:
    params = {"code_bss": code_bss, "size": 20000, "sort": "asc", "format": "json",
              "fields": "date_mesure,profondeur_nappe,niveau_nappe_eau"}
    if since:
        params["date_debut_mesure"] = since
    out, url = [], f"{API}/chroniques"
    for _ in range(50):
        r = session().get(url, params=params, timeout=300)
        r.raise_for_status()
        d = r.json()
        out += d.get("data", [])
        url, params = d.get("next"), None
        if not url:
            break
    return out


def run(aoi: AOI) -> dict:
    found = stations(aoi)
    added = {}
    with connect() as conn:
        conn.execute("DELETE FROM ref.gw_site WHERE site = %s", (aoi.code,))
    for s in found:
        code = s["code_bss"]
        aquifers = "; ".join(s.get("noms_masse_eau_edl") or []) or None      # groundwater bodies
        with connect() as conn:
            conn.execute(
                """INSERT INTO ref.gw_station (code_bss, name, commune, lon, lat, altitude_m, depth_m, aquifers,
                                               first_date, last_date, props)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                   ON CONFLICT (code_bss) DO UPDATE SET name = EXCLUDED.name, commune = EXCLUDED.commune,
                       lon = EXCLUDED.lon, lat = EXCLUDED.lat, altitude_m = EXCLUDED.altitude_m, depth_m = EXCLUDED.depth_m,
                       aquifers = EXCLUDED.aquifers, first_date = EXCLUDED.first_date, last_date = EXCLUDED.last_date,
                       props = EXCLUDED.props""",
                (code, s.get("libelle_pe") or s.get("nom_commune"), s.get("nom_commune"), s["x"], s["y"],
                 s.get("altitude_station"), s.get("profondeur_investigation"), aquifers,
                 s.get("date_debut_mesure"), s.get("date_fin_mesure"),
                 json.dumps({k: v for k, v in s.items() if k != "geometry"}, default=str)))
            conn.execute("INSERT INTO ref.gw_site (site, code_bss, distance_km) VALUES (%s, %s, %s)",
                         (aoi.code, code, s["distance_km"]))
            last = conn.execute("SELECT max(date) AS d FROM eo.gw_level WHERE code_bss = %s", (code,)).fetchone()["d"]
        since = (last - dt.timedelta(days=7)).isoformat() if last else None      # late validations
        rows = [(code, x["date_mesure"], x.get("profondeur_nappe"), x.get("niveau_nappe_eau"))
                for x in _levels(code, since) if x.get("date_mesure")]
        with connect() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO eo.gw_level (code_bss, date, depth_m, level_ngf) VALUES (%s, %s, %s, %s)
                       ON CONFLICT (code_bss, date) DO UPDATE SET depth_m = EXCLUDED.depth_m,
                           level_ngf = EXCLUDED.level_ngf""", rows)
        added[code] = len(rows)
        log.info("Groundwater %s (%s, %.1f km): %d daily levels", code, s.get("nom_commune"), s["distance_km"], len(rows))
    return {"stations": len(found), "levels": added, "radius_km": RADIUS_KM}
