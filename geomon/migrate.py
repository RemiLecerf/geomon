"""Database schema: created and upgraded from ``schema.sql``, by the ingestion, the scheduler
and the web API.

The database records the version of the schema it holds (a comment on the ``meta`` schema).
The script runs only when that version is older than ``SCHEMA_VERSION``: schema changes take
exclusive locks, so they are not repeated at every start. Bump ``SCHEMA_VERSION`` after
changing ``schema.sql``.

Several sites in one database: the tables written per site carry a ``site`` column. Rows
written before (one site per database) are given the site of the database's existing AOI.
"""

from __future__ import annotations

from pathlib import Path

from .db import connect

SCHEMA_VERSION = 2
_SCHEMA_SQL = Path(__file__).with_name("schema.sql")
_MARK = "geomon schema "

_BACKFILL = """
UPDATE {table} SET site = (SELECT code FROM meta.aoi WHERE kind = 'site' ORDER BY updated_at LIMIT 1)
WHERE site IS NULL
"""

_done = False


def _version(conn) -> int:
    d = conn.execute(
        "SELECT obj_description(to_regnamespace('meta'), 'pg_namespace') AS d").fetchone()["d"] or ""
    return int(d[len(_MARK):]) if d.startswith(_MARK) and d[len(_MARK):].isdigit() else 0


def migrate(force: bool = False) -> None:
    """Brings the schema up to date, once per process. Gives up after 10 s waiting for a lock
    rather than queue every query behind it."""
    global _done
    if _done and not force:
        return
    with connect() as conn:
        if _version(conn) >= SCHEMA_VERSION and not force:
            _done = True
            return
        conn.execute("SET lock_timeout = '10s'")
        conn.execute(_SCHEMA_SQL.read_text(encoding="utf-8"))
        for table in ("ref.osm_feature", "meta.ingest_log", "ref.farm_plot"):
            conn.execute(_BACKFILL.format(table=table))
        conn.execute(f"COMMENT ON SCHEMA meta IS '{_MARK}{SCHEMA_VERSION}'")
    _done = True
