"""INSEE descriptive statistics through the open Melodi API (no key needed).

For each dataset, observations are fetched for every level of the AOI's
hierarchy (commune, EPCI, département, région, France) and stored in
stats.observation with their unit and geography vintage. Code lists
(labels of dimension codes) are cached under data/raw/insee.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from ..aoi import AOI
from ..config import settings
from ..db import connect
from ..http import get_json

log = logging.getLogger(__name__)

MELODI = "https://api.insee.fr/melodi"

DATASETS = [
    "DS_POPULATIONS_HISTORIQUES",   # municipal population since 1968
    "DS_RP_POPULATION_PRINC",       # census: population by sex and age
    "DS_RP_MENAGES_PRINC",          # census: households
    "DS_RP_LOGEMENT_PRINC",         # census: dwellings
    "DS_RP_EMPLOI_LR_PRINC",        # census: labour force and unemployment
    "DS_FILOSOFI_CC",               # income and poverty (often confidential for small communes)
]

# Unit codes found in UNIT_MEASURE -> pint-compatible unit
UNIT_CODES = {
    "EUR_YR": "EUR/year",
    "EUR": "EUR",
    "PT": "percent",
    "PCT": "percent",
    "PS": "person",
    "Y": "year",
    "NB": "count",
}
# Measure code (…_MEASURE dimension) -> unit, when no UNIT_MEASURE is given
MEASURE_UNITS = {
    "POP": "person", "PMUN": "person", "PTOT": "person", "PCAP": "person",
    "DWELLINGS": "dwelling", "HOUSEHOLDS": "household", "HH": "household",
}

SKIP_DIMS = {"GEO", "TIME_PERIOD", "FREQ"}
CACHE_DAYS = 30


def _range(dataset: str) -> dict:
    cache = settings.raw_dir / "insee" / f"{dataset}_range.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.exists() and (time.time() - cache.stat().st_mtime) < CACHE_DAYS * 86400:
        return json.loads(cache.read_text(encoding="utf-8"))
    data = get_json(f"{MELODI}/range/{dataset}", timeout=300)
    # Drop the (very long) list of geographies before caching
    for c in data.get("range", []):
        if c.get("concept", {}).get("code") == "GEO":
            c["values"] = []
    cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def _observations(dataset: str, geo: str) -> list[dict]:
    out: list[dict] = []
    url: str | None = f"{MELODI}/data/{dataset}"
    params: dict | None = {"GEO": geo, "maxResult": 10000}
    while url:
        data = get_json(url, params, timeout=300)
        out.extend(data.get("observations", []))
        nxt = (data.get("paging") or {}).get("next")
        url, params = (nxt, None) if nxt and nxt != url else (None, None)
    return out


def _parse(dataset: str, obs: dict) -> dict:
    dims = dict(obs.get("dimensions", {}))
    attrs = obs.get("attributes", {}) or {}
    geo = dims.get("GEO", "")
    parts = geo.split("-")
    vintage = int(parts[0]) if parts and parts[0].isdigit() else None
    if vintage is not None:
        parts = parts[1:]
    level, code = parts[0], "-".join(parts[1:])

    measure_key = next((k for k in dims if k.endswith("_MEASURE") and k != "UNIT_MEASURE"), None)
    measure = dims.get(measure_key) if measure_key else None
    unit_code = dims.pop("UNIT_MEASURE", None) or attrs.get("UNIT_MEASURE")
    unit = UNIT_CODES.get(unit_code, unit_code) if unit_code else MEASURE_UNITS.get(measure or "", "count")

    measures = obs.get("measures", {}) or {}
    value = None
    for m in measures.values():
        if isinstance(m, dict) and m.get("value") is not None:
            value = float(m["value"])
            break

    return {
        "dataset_id": dataset,
        "geo_level": level,
        "geo_code": code,
        "geo_vintage": vintage,
        "time_period": dims.get("TIME_PERIOD", ""),
        "measure": measure,
        "dims": json.dumps({k: v for k, v in dims.items() if k not in SKIP_DIMS}),
        "value": value,
        "unit": unit,
        "unit_mult": int(attrs.get("UNIT_MULT") or 0),
        "status": attrs.get("OBS_STATUS"),
        "conf_status": attrs.get("CONF_STATUS"),
    }


def _store_labels(conn, dataset: str, rng: dict) -> None:
    label = rng.get("label") or {}
    conn.execute(
        """INSERT INTO stats.dataset (id, source, title_fr, title_en, url, fetched_at)
           VALUES (%s, 'INSEE', %s, %s, %s, now())
           ON CONFLICT (id) DO UPDATE SET title_fr = EXCLUDED.title_fr,
               title_en = EXCLUDED.title_en, url = EXCLUDED.url, fetched_at = now()""",
        (dataset, label.get("fr"), label.get("en"),
         f"https://catalogue-donnees.insee.fr/fr/catalogue/recherche/{dataset}"),
    )
    conn.execute("DELETE FROM stats.code_label WHERE dataset_id = %s", (dataset,))
    rows = []
    for c in rng.get("range", []):
        concept = c.get("concept", {})
        if concept.get("code") in (None, "GEO"):
            continue
        for v in c.get("values", []):
            lab = v.get("label") or {}
            rows.append((dataset, concept["code"], (concept.get("label") or {}).get("fr"),
                         (concept.get("label") or {}).get("en"), v.get("code"),
                         lab.get("fr"), lab.get("en")))
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO stats.code_label
                   (dataset_id, concept, concept_fr, concept_en, code, label_fr, label_en)
               VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
            rows,
        )


def geographies(aoi: AOI) -> list[str]:
    geos = [f"COM-{c}" for c in aoi.codes]
    geos += [f"EPCI-{e}" for e in aoi.epcis]
    geos += [f"DEP-{d}" for d in aoi.departements] + [f"REG-{r}" for r in aoi.regions] + ["FRANCE-F"]
    return geos


def run(aoi: AOI) -> dict:
    summary: dict[str, int] = {}
    geos = geographies(aoi)
    for ds in DATASETS:
        try:
            rng = _range(ds)
        except Exception as exc:  # dataset withdrawn or renamed
            log.warning("INSEE %s: code lists unavailable (%s), skipped", ds, exc)
            continue
        n = 0
        with connect() as conn:
            _store_labels(conn, ds, rng)
            for geo in geos:
                level, code = geo.split("-", 1)
                obs = _observations(ds, geo)
                rows = [_parse(ds, o) for o in obs]
                conn.execute(
                    "DELETE FROM stats.observation WHERE dataset_id = %s AND geo_level = %s AND geo_code = %s",
                    (ds, level, code),
                )
                with conn.cursor() as cur:
                    cur.executemany(
                        """INSERT INTO stats.observation
                               (dataset_id, geo_level, geo_code, geo_vintage, time_period, measure,
                                dims, value, unit, unit_mult, status, conf_status)
                           VALUES (%(dataset_id)s, %(geo_level)s, %(geo_code)s, %(geo_vintage)s,
                                   %(time_period)s, %(measure)s, %(dims)s::jsonb, %(value)s, %(unit)s,
                                   %(unit_mult)s, %(status)s, %(conf_status)s)""",
                        rows,
                    )
                n += len(rows)
        summary[ds] = n
        log.info("INSEE %s: %d observations", ds, n)
    return summary


def cache_dir() -> Path:
    return settings.raw_dir / "insee"
