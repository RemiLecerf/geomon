"""Idempotent schema changes, run by the ingestion and when the web API starts.

Several sites in one database: the tables written per site carry a ``site``
column. Rows written before (one site per database) are given the site of the
database's existing AOI.
"""

from __future__ import annotations

from .db import connect

_SQL = """
ALTER TABLE meta.aoi ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'commune';
ALTER TABLE meta.aoi ADD COLUMN IF NOT EXISTS site text;
ALTER TABLE meta.aoi ADD COLUMN IF NOT EXISTS members text[];
ALTER TABLE meta.aoi ADD COLUMN IF NOT EXISTS position int;
ALTER TABLE meta.aoi DROP CONSTRAINT IF EXISTS aoi_pkey;
DROP INDEX IF EXISTS meta.aoi_kind_code_idx;
CREATE UNIQUE INDEX IF NOT EXISTS aoi_site_kind_code_idx ON meta.aoi (site, kind, code);

ALTER TABLE ref.osm_feature ADD COLUMN IF NOT EXISTS site text;
CREATE INDEX IF NOT EXISTS osm_feature_site_layer_idx ON ref.osm_feature (site, layer);
ALTER TABLE meta.ingest_log ADD COLUMN IF NOT EXISTS site text;
CREATE INDEX IF NOT EXISTS rpg_parcel_year_id_idx ON ref.rpg_parcel (year, parcel_id);
ALTER TABLE IF EXISTS ref.farm_plot ADD COLUMN IF NOT EXISTS site text;
"""

_BACKFILL = """
UPDATE {table} SET site = (SELECT code FROM meta.aoi WHERE kind = 'site' ORDER BY updated_at LIMIT 1)
WHERE site IS NULL
"""

_CHECK = """
SELECT (SELECT count(*) FROM information_schema.columns
        WHERE (table_schema, table_name, column_name) IN
              (('meta', 'aoi', 'site'), ('ref', 'osm_feature', 'site'), ('meta', 'ingest_log', 'site'))) = 3
   AND to_regclass('meta.aoi_site_kind_code_idx') IS NOT NULL
   AND to_regclass('ref.rpg_parcel_year_id_idx') IS NOT NULL
   AND (to_regclass('ref.farm_plot') IS NULL OR EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'ref' AND table_name = 'farm_plot' AND column_name = 'site')) AS ok
"""

_done = False


def migrate(force: bool = False) -> None:
    """Applies the schema changes once. Schema changes take exclusive locks: they are only run when
    something is missing, and give up after 10 s rather than queue every query behind them."""
    global _done
    if _done and not force:
        return
    with connect() as conn:
        if not conn.execute("SELECT to_regclass('meta.aoi') AS t").fetchone()["t"]:
            return
        if conn.execute(_CHECK).fetchone()["ok"]:
            _done = True
            return
        conn.execute("SET lock_timeout = '10s'")
        conn.execute(_SQL)
        for table in ("ref.osm_feature", "meta.ingest_log"):
            conn.execute(_BACKFILL.format(table=table))
        if conn.execute("SELECT to_regclass('ref.farm_plot') AS t").fetchone()["t"]:
            conn.execute(_BACKFILL.format(table="ref.farm_plot"))
    _done = True
