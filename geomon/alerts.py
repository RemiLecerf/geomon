"""Météo-France warnings for the site's départements: the weather Vigilance (today and tomorrow, per
phenomenon, and the bulletins' texts) and Météo des forêts (forest fire danger, tomorrow and the day
after, during the fire season).

Both are national files fetched by the steps ``vigilance`` and ``meteo-forets`` into ``data/raw``
(shared by the sites: a fetch serves every site for a few minutes) and read here for a site.

Vigilance colours: 1 green, 2 yellow, 3 orange, 4 red. A coastal département has a second domain,
its code followed by "10", for the waves-submersion phenomenon.

Météo des forêts is published only during the fire season (roughly June to September, longer in the
south): out of season the API answers 404 and the page says so. Its JSON layout is not documented:
the levels are looked up by field names (département, day, level).
"""

from __future__ import annotations

import datetime as dt
import json
import time

from . import meteofrance as mf
from .config import settings
from .zarrio import read_json, write_bytes_atomic

VIGILANCE = "/public/DPVigilance/v1/cartevigilance/encours"
TEXTS = "/public/DPVigilance/v1/textesvigilance/encours"
FORETS = "/public/DPMeteoForets/v1/carte/departement/encours"
REUSE_S = 600

PHENOMENA = {"1": "Wind", "2": "Rain and flooding", "3": "Thunderstorms", "4": "River floods", "5": "Snow and ice",
             "6": "Heatwave", "7": "Extreme cold", "8": "Avalanches", "9": "Waves and submersion"}
COLORS = {1: ("green", "#31aa35"), 2: ("yellow", "#f9e000"), 3: ("orange", "#f7a401"), 4: ("red", "#e1000f")}
# Météo des forêts danger levels (the page's legend)
FIRE_LEVELS = {1: ("weak", "#9fd981"), 2: ("moderate", "#f9e000"), 3: ("high", "#f7a401"), 4: ("very high", "#e1000f"),
               5: ("exceptional", "#7d1c8e")}


def _dir():
    d = settings.raw_dir / "meteofrance_alerts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _fresh(path, max_age_s: float) -> bool:
    return path.exists() and time.time() - path.stat().st_mtime < max_age_s


# ---------------------------------------------------------------------------
# Fetch (ingestion steps)
# ---------------------------------------------------------------------------
def fetch_vigilance() -> dict:
    """The map (about 210 KB) when older than REUSE_S, and the bulletins' texts only when the map
    changed (its update time)."""
    carte, texts, marker = _dir() / "vigilance.json", _dir() / "vigilance_texts.json", _dir() / "vigilance_texts.for"
    if not _fresh(carte, REUSE_S):
        r = mf.get(mf.PUBLIC + VIGILANCE)
        r.raise_for_status()
        write_bytes_atomic(carte, r.content)
    v = read_json(carte)["product"]
    updated = str(v.get("update_time"))
    if not texts.exists() or not marker.exists() or marker.read_text(encoding="utf-8") != updated:
        r = mf.get(mf.PUBLIC + TEXTS)
        r.raise_for_status()
        write_bytes_atomic(texts, r.content)
        write_bytes_atomic(marker, updated.encode())
    return {"updated": updated, "max_color_france": v.get("global_max_color_id")}


def _forets_record(dep: str) -> dict:
    """The fire danger answer of a département (fetched when older than REUSE_S): {"fetched",
    "status" (HTTP), "data"}. Out of the fire season the API answers 404."""
    p = _dir() / f"meteo_forets_{dep}.json"
    if _fresh(p, REUSE_S):
        return read_json(p)
    r = mf.get(mf.PUBLIC + FORETS, {"format": "json", "echeance": "J1J2", "id-departement": dep})
    if r.status_code not in (200, 404):
        r.raise_for_status()
    try:
        body = r.json()
    except ValueError:
        body = r.text[:500]
    rec = {"fetched": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "status": r.status_code, "data": body}
    write_bytes_atomic(p, json.dumps(rec, ensure_ascii=False).encode())
    return rec


def _published(rec: dict) -> bool:
    return rec["status"] == 200 and bool(rec["data"])


def fetch_forets(departements: list[str]) -> dict:
    return {dep: "published" if _published(_forets_record(dep)) else "none (out of season)" for dep in departements}


# ---------------------------------------------------------------------------
# Read for a site
# ---------------------------------------------------------------------------
def _color(cid) -> dict:
    cid = int(cid or 1)
    name, hexa = COLORS.get(cid, COLORS[1])
    return {"id": cid, "name": name, "hex": hexa}


def vigilance_for(departements: list[str]) -> dict | None:
    v = read_json(_dir() / "vigilance.json")
    if v is None:
        return None
    v = v["product"]
    want = {d: d for d in departements} | {f"{d}10": d for d in departements}
    deps: dict[str, dict] = {d: {"code": d, "coastal": False, "periods": []} for d in departements}
    for period in v.get("periods", []):
        ech = period.get("echeance")
        per_dep: dict[str, dict] = {}
        for dom in period["timelaps"]["domain_ids"]:
            d = want.get(dom["domain_id"])
            if d is None:
                continue
            if dom["domain_id"] != d:
                deps[d]["coastal"] = True
            e = per_dep.setdefault(d, {"echeance": ech, "begin": period.get("begin_validity_time"),
                                       "end": period.get("end_validity_time"), "max_color": 1, "phenomena": []})
            e["max_color"] = max(e["max_color"], int(dom.get("max_color_id") or 1))
            for ph in dom.get("phenomenon_items", []):
                c = int(ph.get("phenomenon_max_color_id") or 1)
                e["phenomena"].append({"id": ph["phenomenon_id"], "name": PHENOMENA.get(ph["phenomenon_id"], ph["phenomenon_id"]),
                                       "color": _color(c),
                                       "times": [{"begin": t["begin_time"], "end": t["end_time"], "color": int(t["color_id"])}
                                                 for t in ph.get("timelaps_items", []) if int(t.get("color_id") or 1) > 1]})
        for d, e in per_dep.items():
            e["phenomena"].sort(key=lambda x: (-x["color"]["id"], x["name"]))
            e["max_color"] = _color(e["max_color"])
            deps[d]["periods"].append(e)
    texts = _texts(departements)
    for d in deps.values():
        d["texts"] = texts.get(d["code"], [])
    return {"updated": v.get("update_time"), "france_max_color": _color(v.get("global_max_color_id")),
            "comment": _comment(v),
            "departements": list(deps.values())}


def _comment(v: dict) -> list[str]:
    """The map's comment (today's period): ``text_items`` is one object or a list of them."""
    out = []
    for p in v.get("periods", [])[:1]:
        items = p.get("text_items") or []
        for it in items if isinstance(items, list) else [items]:
            if isinstance(it, dict):
                out += [t for t in it.get("text", []) if t]
    return out


def _texts(departements: list[str]) -> dict[str, list[dict]]:
    """Bulletin paragraphs of each département (and its coastal domain), when there are any."""
    t = read_json(_dir() / "vigilance_texts.json")
    if t is None:
        return {}
    t = t["product"]
    out: dict[str, list[dict]] = {}
    for bloc in t.get("text_bloc_items", []):
        dom = str(bloc.get("domain_id"))
        dep = dom[:-2] if dom.endswith("10") and dom[:-2] in departements else dom
        if dep not in departements:
            continue
        for item in bloc.get("bloc_items", []):
            for ti in item.get("text_items", []):
                for term in ti.get("term_items", []):
                    paras = []
                    for sub in term.get("subdivision_text", []):
                        body = " ".join(x for x in sub.get("text", []) if x)
                        if body:
                            paras.append({"title": sub.get("bold_text") or "", "text": body})
                    if paras:
                        out.setdefault(dep, []).append({"hazard": ti.get("hazard_name"), "term": term.get("term_names"),
                                                        "risk": term.get("risk_name"), "paragraphs": paras})
    return out


def fire_levels(data) -> list[dict]:
    """[{"departement", "day", "level"}] from Météo des forêts' JSON, whatever its exact layout: the
    records are the dicts holding a département and a level."""
    found: list[dict] = []

    def walk(x):
        if isinstance(x, dict):
            keys = {k.lower().replace("_", "").replace("-", ""): k for k in x}
            dep = next((x[keys[k]] for k in keys if k in ("iddepartement", "departement", "codedepartement", "dep", "numdep")), None)
            lvl = next((x[keys[k]] for k in keys if "niveau" in k or "danger" in k or k in ("level", "indice")), None)
            day = next((x[keys[k]] for k in keys if k in ("echeance", "jour", "date", "day", "dateecheance")), None)
            if dep is not None and lvl is not None and not isinstance(lvl, (dict, list)):
                found.append({"departement": str(dep), "day": day, "level": lvl})
            for v in x.values():
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(data)
    return found


def forets_for(departements: list[str]) -> list[dict]:
    out = []
    for dep in departements:
        rec = read_json(_dir() / f"meteo_forets_{dep}.json")
        if rec is None:
            out.append({"departement": dep, "status": "not fetched"})
            continue
        levels = fire_levels(rec["data"]) if _published(rec) else []
        for lv in levels:
            try:
                lv["name"], lv["hex"] = FIRE_LEVELS.get(int(float(lv["level"])), (str(lv["level"]), "#999999"))
            except (TypeError, ValueError):
                lv["name"], lv["hex"] = str(lv["level"]), "#999999"
        # Published but in a layout fire_levels does not know: the raw answer is shown
        out.append({"departement": dep, "fetched": rec["fetched"], "status": "published" if _published(rec) else "none",
                    "levels": levels, "raw": rec["data"] if _published(rec) and not levels else None})
    return out
