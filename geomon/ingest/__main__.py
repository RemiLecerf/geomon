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

from ..aoi import resolve_aoi, save_aoi
from ..config import all_sites, settings, use_site
from ..db import connect
from .lock import ingest_lock, lock_group
from . import admin, agera5, dem, farm, insee, openmeteo, osm, rpg, sentinel2, sim2, soil

STEPS = {
    "admin": admin.run,
    "farm": farm.run,                   # the farm's plots (GEOMON_FARM_FILE) as an analysis area
    "rpg": rpg.run,
    "osm": osm.run,
    "dem": dem.run,
    "soil": soil.run,
    "insee": insee.run,
    "weather": openmeteo.run_history,   # Open-Meteo: Météo-France archived forecasts, ERA5-Land
    "sim2": sim2.run,                   # Météo-France SAFRAN-ISBA daily reanalysis
    "agera5": agera5.run,               # Copernicus AgERA5 (needs GEOMON_CDS_API_KEY)
    "sentinel2": sentinel2.run,         # Sentinel-2 L2A via openEO (needs sentinel2-login once)
    "sentinel2-login": sentinel2.login, # interactive: log in to the Copernicus Data Space
    "sentinel2-stats": sentinel2.recompute,  # recompute index statistics from the stored images
    "sentinel2-parcels": sentinel2.recompute_parcels,  # recompute the RPG parcel series (after an RPG reload)
    "forecast": openmeteo.run_forecast, # latest forecast (run it daily)
}


# Interactive steps, never part of "all"
MANUAL_STEPS = {"sentinel2-login", "sentinel2-stats", "sentinel2-parcels"}


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
