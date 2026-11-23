"""Database schema: created and kept up to date from ``schema.sql``.

``db/init`` only creates the extensions, when the PostgreSQL cluster is first started: the
tables come from here, run by the ingestion, the scheduler and the web API when they start. The
database records the version of the schema it holds (a comment on the ``meta`` schema), and the
script only runs when that version is older than ``SCHEMA_VERSION``: schema changes take
exclusive locks, so they are not repeated at every start. Bump ``SCHEMA_VERSION`` after changing
``schema.sql``.
"""

from __future__ import annotations

from pathlib import Path

from .db import connect

SCHEMA_VERSION = 12
_SCHEMA_SQL = Path(__file__).with_name("schema.sql")
_MARK = "geomon schema "

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
        conn.execute(f"COMMENT ON SCHEMA meta IS '{_MARK}{SCHEMA_VERSION}'")
    _done = True
