"""Command line entry point for data acquisition.

    python -m geomon.ingest all                        # every step, for every site
    python -m geomon.ingest rpg soil                   # selected steps, every site
    python -m geomon.ingest --site dragey-ronthon-6 all  # one site (repeat --site for several)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
import traceback

from .. import datasets
from ..aoi import resolve_aoi, save_aoi
from ..config import all_sites, settings, use_site
from ..db import connect
from .lock import ingest_lock, lock_group
from . import (admin, agera5, alerts, clms, comephore, dem, era5, farm, groundwater, insee, mf_etp, mf_models, mf_obs, mf_stations, modis, nowcast, openmeteo,
               osm, radar, rpg, sentinel1, sentinel2, sim2, soil, soil_inrae, soil_maps)

STEPS = {
    "admin": admin.run,
    "farm": farm.run,                   # the farm's plots (GEOMON_FARM_FILE) as an analysis area
    "rpg": rpg.run,                       # every RPG year again (a correction of the published RPG)
    "rpg-new": rpg.run_new,               # the RPG years newly published, then the seasons recomputed
    "osm": osm.run,
    "dem": dem.run,
    "soil": soil.run,
    "soil-inrae": soil_inrae.run,       # INRAE / Gis Sol soil properties and water reserve, 90 m (France)
    "soil-maps": soil_maps.run,         # dominant soils (Gis Sol RRP), geology 1:50 000 and clay exposure (BRGM)
    "groundwater": groundwater.run,     # groundwater levels of the nearest piezometers (Hub'Eau)
    "insee": insee.run,
    "weather": openmeteo.run_history,   # Open-Meteo: Météo-France archived forecasts (+ ERA5-Land if GEOMON_ERA5_SOURCE=openmeteo)
    "era5land": era5.run_land,          # Copernicus CDS ERA5-Land hourly, 0.1° (needs GEOMON_CDS_API_KEY)
    "era5": era5.run_era5,              # Copernicus CDS ERA5 hourly, 0.25° (needs GEOMON_CDS_API_KEY)
    "sim2": sim2.run,                   # Météo-France SAFRAN-ISBA daily reanalysis
    "mf-stations": mf_stations.run,     # measured daily weather of the nearest Météo-France stations (no account)
    "mf-etp": mf_etp.run,               # Météo-France daily Penman-Monteith ETP, 0.125° grid (no account)
    "comephore": comephore.run,         # COMEPHORE hourly rainfall reanalysis, 1 km, since 1997 (no account; long)
    "agera5": agera5.run,               # Copernicus AgERA5 (needs GEOMON_CDS_API_KEY)
    "sentinel2": sentinel2.run,         # Sentinel-2 L2A via openEO (needs sentinel2-login once)
    "sentinel2-login": sentinel2.login, # interactive: log in to the Copernicus Data Space
    "sentinel2-stats": sentinel2.recompute,  # recompute index statistics from the stored images
    "sentinel2-parcels": sentinel2.recompute_parcels,  # recompute the RPG parcel series (after an RPG reload)
    "sentinel2-groups": sentinel2.recompute_groups,  # recompute the crop-group curves only (after an RPG change)
    "sentinel2-reencode": sentinel2.reencode,  # rewrite the stored images (encoding, dates below GEOMON_S2_MIN_CLEAR)
    "sentinel1": sentinel1.run,         # Sentinel-1 GRD backscatter via openEO (same login), from GEOMON_S1_START
    "sentinel1-stats": sentinel1.recompute,  # recompute the Sentinel-1 statistics from the stored images
    "sentinel1-reencode": sentinel1.reencode,  # rewrite the stored images with the current encoding
    "modis": modis.run,                 # MODIS NDVI 250 m (Planetary Computer, no account), from GEOMON_MODIS_START
    "modis-stats": modis.recompute,     # recompute the MODIS statistics from the stored composites
    "modis-crops": modis.recompute_crops,  # recompute the MODIS crop-group curves (after an RPG reload)
    "radar": radar.run_opera,           # rain radar, EUMETNET OPERA composites (no account), the kept hours
    "radar-mf": radar.run_mf,           # rain radar, Météo-France latest 5-minute image (key)
    "arome-pi": nowcast.run,            # nowcast, the next 6 hours: Météo-France AROME-PI (key)
    "mf-obs": mf_obs.run,               # observations of the last days, nearest Météo-France stations (key)
    "vigilance": alerts.run_vigilance,  # Météo-France Vigilance of the site's départements (key)
    "meteo-forets": alerts.run_forets,  # Météo des forêts, fire danger (key; fire season only)
    "arome": mf_models.run_arome,       # AROME 1.3 km maps, 51 hours (key)
    "pe-arome": mf_models.run_pearome,  # PE-AROME ensemble probabilities of rain and frost (key)
    "sea": mf_models.run_sea,           # waves (MFWAM) and storm surge (HYCOM2D), coastal sites (key)
    "clms-soil": clms.run_soil,         # soil moisture: Sentinel-1 SSM and the Soil Water Index, 1 km (openEO)
    "clms-dmp": clms.run_dmp,           # dry matter productivity, 300 m, 10-daily (openEO)
    "clms-vpp": clms.run_vpp,           # HR-VPP phenology and productivity per parcel, yearly (openEO)
    "forecast": openmeteo.run_forecast, # latest forecast (run it daily)
}


# Interactive steps, never part of "all"
MANUAL_STEPS = {"sentinel2-login", "sentinel2-stats", "sentinel2-parcels", "sentinel2-groups", "sentinel1-stats", "modis-stats", "modis-crops",
                "sentinel2-reencode", "sentinel1-reencode"}


def _log_step(step: str, started: dt.datetime, status: str, message: str, details: dict) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO meta.ingest_log (site, step, started_at, finished_at, status, message, details)
               VALUES (%s, %s, %s, now(), %s, %s, %s::jsonb)""",
            (settings.site_code, step, started, status, message, json.dumps(details, default=str)),
        )


class Failure:
    """Result of a failed step: false, with the time to retry at when the step gave one."""

    def __init__(self, retry_at: dt.datetime | None = None):
        self.retry_at = retry_at

    def __bool__(self) -> bool:
        return False


def run_step(step: str, aoi) -> bool | Failure:
    """Run one step, record it in meta.ingest_log. Returns True on success, else a Failure."""
    log = logging.getLogger("geomon.ingest")
    started = dt.datetime.now(dt.timezone.utc)
    log.info("=== %s · %s ===", settings.site_code, step)
    try:
        d = datasets.of_step(step)
        need = datasets.needs(d.code) if d else None
        if need:
            details = {"skipped": f"needs {need}"}
        else:
            with ingest_lock(lock_group(step)):
                details = STEPS[step](aoi) or {}
        _log_step(step, started, "ok", "", details)
        log.info("%s: ok %s", step, json.dumps(details, default=str))
        return True
    except Exception as exc:
        log.error("%s failed: %s", step, exc)
        traceback.print_exc()
        try:
            _log_step(step, started, "error", str(exc), {})
        except Exception:  # the database may be the problem
            log.exception("Could not record the failure in meta.ingest_log")
        return Failure(getattr(exc, "retry_at", None))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="geomon.ingest")
    parser.add_argument("steps", nargs="*", default=["all"], help=f"all or any of {', '.join(STEPS)}")
    parser.add_argument("--site", action="append", help="site code (sites.json); default: every site")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    log = logging.getLogger("geomon.ingest")

    steps = [s for s in STEPS if s not in MANUAL_STEPS] if "all" in args.steps else args.steps
    unknown = [s for s in steps if s not in STEPS]
    if unknown:
        parser.error(f"unknown step(s): {', '.join(unknown)}")

    codes = args.site or [s.code for s in all_sites()]
    failed = []
    for code in codes:
        with use_site(code):
            aoi = resolve_aoi()
            save_aoi(aoi)
            failed += [f"{code}: {step}" for step in steps if not run_step(step, aoi)]
    if failed:
        log.error("Failed steps: %s", ", ".join(failed))
        return 1
    log.info("All steps completed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
