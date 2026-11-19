-- Runs once, when the PostgreSQL cluster is initialised (as the superuser).
--
-- Only the extensions are created here. The tables are defined in geomon/schema.sql and
-- created or upgraded by geomon.migrate when the ingestion, the scheduler or the web API starts.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS hstore;   -- used by ogr2ogr for OSM other_tags
