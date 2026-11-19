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

# Steps writing the same data share a lock. Each dataset updated by the scheduler (and the farm
# step) has its own, so that a long download of one (Sentinel-2 history, Open-Meteo archive
# waiting for its daily quota) does not hold up the others. Locks are per site as well.
LOCK_IDS = {"main": 7_361_001, "sentinel2": 7_361_002, "farm": 7_361_003, "forecast": 7_361_004,
            "weather": 7_361_005, "sim2": 7_361_006, "agera5": 7_361_007}


def lock_group(step: str) -> str:
    if step.startswith("sentinel2"):
        return "sentinel2"
    return step if step in LOCK_IDS else "main"


@contextmanager
def ingest_lock(group: str = "main", wait_s: float = 12 * 3600) -> Iterator[None]:
    """One step at a time per group and per site: two sites are updated independently."""
    LOCK_ID = LOCK_IDS[group]
    site_key = zlib.crc32(settings.site_code.encode()) & 0x7FFFFFFF
    with connect(autocommit=True) as conn:
        deadline = time.monotonic() + wait_s
        announced = False
        while not conn.execute("SELECT pg_try_advisory_lock(%s, %s) AS ok", (LOCK_ID, site_key)).fetchone()["ok"]:
            if not announced:
                log.info("Another %s step is running for %s: waiting for it to finish", group, settings.site_code)
                announced = True
            if time.monotonic() > deadline:
                raise TimeoutError("Another ingestion step has been running for too long")
            time.sleep(10)
        try:
            yield
        finally:
            try:
                conn.execute("SELECT pg_advisory_unlock(%s, %s)", (LOCK_ID, site_key))
            except Exception as exc:  # database restarted: the lock went with the connection
                log.warning("Could not release the ingestion lock (%s); it was released with the connection", exc)
