"""Steps ``vigilance`` and ``meteo-forets``: Météo-France's national warning files (see geomon.alerts)."""

from __future__ import annotations

from .. import alerts
from ..aoi import AOI


def run_vigilance(aoi: AOI) -> dict:
    info = alerts.fetch_vigilance()
    v = alerts.vigilance_for(aoi.departements) or {}
    return info | {"departements": {d["code"]: [p["max_color"]["name"] for p in d["periods"]] for d in v.get("departements", [])}}


def run_forets(aoi: AOI) -> dict:
    return alerts.fetch_forets(aoi.departements)
