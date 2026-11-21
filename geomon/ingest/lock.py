"""One ingestion step at a time, across containers (scheduler and manual runs).

A PostgreSQL advisory lock is used rather than a file lock: it works across
containers and on Windows bind mounts, and is released if a process dies.
"""

from __future__ import annotations

import logging
import time
import zlib
from contextlib import contextmanager
from typing import Iterator

from ..config import settings
from ..db import connect

log = logging.getLogger(__name__)

# Steps writing the same data share a lock. Each dataset updated by the scheduler has its own, so
# that a long download of one (Sentinel-2 history, Open-Meteo archive waiting for its daily quota,
# the Météo-France station history) does not hold up the others; the static datasets share "main".
# Locks are per site as well, except those of GLOBAL_GROUPS. The groups below keep the keys they
# always had; the others' keys are derived from their names.
LOCK_IDS = {"main": 7_361_001, "sentinel2": 7_361_002, "farm": 7_361_003, "forecast": 7_361_004,
            "weather": 7_361_005, "sim2": 7_361_006, "agera5": 7_361_007, "osm": 7_361_008,
            "era5land": 7_361_009, "era5": 7_361_010, "sentinel1": 7_361_011,
            "modis": 7_361_012, "radar": 7_361_013, "groundwater": 7_361_014}
# Step families writing one store (sentinel2, sentinel2-stats, sentinel2-parcels...)
PREFIXES = ("sentinel2", "sentinel1", "modis")
# One run at a time over all the sites. osm: its ogr2ogr staging tables (staging.lines...) are
# shared, and Overpass asks clients not to send parallel queries.
# ensemble: its reads cover every site (geomon.ingest.mf_models), the sites take turns to share them.
# comephore: an archive read for one site is also written for the others.
GLOBAL_GROUPS = {"osm", "ensemble", "comephore"}


def lock_group(step: str) -> str:
    from .. import datasets

    prefix = next((p for p in PREFIXES if step.startswith(p)), None)
    if prefix:
        return prefix
    if step in LOCK_IDS:
        return step
    d = datasets.of_step(step)
    return d.code if d is not None and d.schedule else "main"


def lock_key(group: str, site: str) -> tuple[int, int]:
    """The two int4 keys of the PostgreSQL advisory lock of a group (and a site)."""
    gid = LOCK_IDS.get(group) or zlib.crc32(f"geomon-lock-{group}".encode()) & 0x7FFFFFFF
    return gid, 0 if group in GLOBAL_GROUPS else zlib.crc32(site.encode()) & 0x7FFFFFFF


@contextmanager
def ingest_lock(group: str = "main", wait_s: float = 12 * 3600) -> Iterator[None]:
    """One step at a time per group and per site (two sites are updated independently), or per
    group only for the GLOBAL_GROUPS."""
    key = lock_key(group, settings.site_code)
    holder = "any site" if group in GLOBAL_GROUPS else settings.site_code
    with connect(autocommit=True) as conn:
        deadline = time.monotonic() + wait_s
        announced = False
        while not conn.execute("SELECT pg_try_advisory_lock(%s, %s) AS ok", key).fetchone()["ok"]:
            if not announced:
                log.info("Another %s step is running for %s: waiting for it to finish", group, holder)
                announced = True
            if time.monotonic() > deadline:
                raise TimeoutError("Another ingestion step has been running for too long")
            time.sleep(10)
        try:
            yield
        finally:
            try:
                conn.execute("SELECT pg_advisory_unlock(%s, %s)", key)
            except Exception as exc:  # database restarted: the lock went with the connection
                log.warning("Could not release the ingestion lock (%s); it was released with the connection", exc)
