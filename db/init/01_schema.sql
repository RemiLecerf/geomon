-- Geomon database schema (first product).
-- Runs once, when the PostgreSQL cluster is initialised.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS hstore;   -- used by ogr2ogr for OSM other_tags

CREATE SCHEMA IF NOT EXISTS ref;      -- reference vectors: admin units, RPG, OSM
CREATE SCHEMA IF NOT EXISTS stats;    -- statistics (INSEE, later Eurostat...)
CREATE SCHEMA IF NOT EXISTS meta;     -- catalogue of layers, ingestion log
CREATE SCHEMA IF NOT EXISTS staging;  -- scratch tables used during ingestion

-- ---------------------------------------------------------------------------
-- Administrative units with temporal validity (Specifications §3.4)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ref.admin_unit (
    id           bigserial PRIMARY KEY,
    level        text NOT NULL,          -- COM, EPCI, ARR, DEP, REG, FRANCE
    code         text NOT NULL,          -- INSEE / SIREN code
    name         text NOT NULL,
    parent_level text,
    parent_code  text,
    vintage      int  NOT NULL,          -- COG vintage (geography year)
    valid_from   date NOT NULL,
    valid_to     date,                   -- NULL = still valid
    source       text NOT NULL,
    props        jsonb NOT NULL DEFAULT '{}'::jsonb,
    geom         geometry(MultiPolygon, 4326),
    UNIQUE (level, code, vintage)
);
CREATE INDEX IF NOT EXISTS admin_unit_geom_idx ON ref.admin_unit USING gist (geom);
CREATE INDEX IF NOT EXISTS admin_unit_validity_idx ON ref.admin_unit (level, code, valid_from, valid_to);

-- Area of interest: its communes (kind 'commune') and the group itself (kind 'site')
CREATE TABLE IF NOT EXISTS meta.aoi (
    code       text NOT NULL,             -- INSEE commune code, or the site code
    name       text NOT NULL,
    kind       text NOT NULL DEFAULT 'commune',
    site       text,                      -- site the row belongs to
    members    text[],                    -- INSEE codes of the communes
    position   int,                       -- order of the communes; -1 for the site
    buffer_m   double precision NOT NULL,
    geom       geometry(MultiPolygon, 4326) NOT NULL,   -- outline (union for the site)
    bbox       geometry(Polygon, 4326) NOT NULL,        -- buffered download extent of the site
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS aoi_kind_code_idx ON meta.aoi (kind, code);

-- ---------------------------------------------------------------------------
-- RPG (Registre parcellaire graphique)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ref.rpg_crop_code (
    year          int  NOT NULL,
    code_culture  text NOT NULL,
    label         text,
    code_group    text,
    label_group   text,
    PRIMARY KEY (year, code_culture)
);

CREATE TABLE IF NOT EXISTS ref.rpg_parcel (
    id          bigserial PRIMARY KEY,
    year        int  NOT NULL,
    parcel_id   text,
    code_cultu  text,
    code_group  text,
    cat_cult_p  text,
    surf_ha     double precision,        -- declared area, unit: ha
    bio         boolean,
    props       jsonb NOT NULL DEFAULT '{}'::jsonb,
    geom        geometry(MultiPolygon, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS rpg_parcel_geom_idx ON ref.rpg_parcel USING gist (geom);
CREATE INDEX IF NOT EXISTS rpg_parcel_year_idx ON ref.rpg_parcel (year);
CREATE INDEX IF NOT EXISTS rpg_parcel_year_id_idx ON ref.rpg_parcel (year, parcel_id);

-- ---------------------------------------------------------------------------
-- OpenStreetMap features (clipped to the AOI extent)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ref.osm_feature (
    id        bigserial PRIMARY KEY,
    osm_id    text,
    layer     text NOT NULL,            -- roads, railways, waterways, buildings, landuse, water, pois
    kind      text,                     -- e.g. highway=primary -> 'primary'
    name      text,
    tags      jsonb NOT NULL DEFAULT '{}'::jsonb,
    snapshot  date,                     -- date of the OSM extract
    geom      geometry(Geometry, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS osm_feature_geom_idx ON ref.osm_feature USING gist (geom);
CREATE INDEX IF NOT EXISTS osm_feature_layer_idx ON ref.osm_feature (layer);

-- ---------------------------------------------------------------------------
-- Statistics (Specifications §3.4)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stats.dataset (
    id          text PRIMARY KEY,         -- e.g. DS_RP_POPULATION_PRINC
    source      text NOT NULL,            -- INSEE, EUROSTAT...
    title_fr    text,
    title_en    text,
    url         text,
    fetched_at  timestamptz NOT NULL DEFAULT now()
);

-- Labels of dimension codes (code lists), per dataset
CREATE TABLE IF NOT EXISTS stats.code_label (
    dataset_id  text NOT NULL REFERENCES stats.dataset(id) ON DELETE CASCADE,
    concept     text NOT NULL,            -- dimension name, e.g. AGE
    concept_fr  text,
    concept_en  text,
    code        text NOT NULL,
    label_fr    text,
    label_en    text,
    PRIMARY KEY (dataset_id, concept, code)
);

CREATE TABLE IF NOT EXISTS stats.observation (
    id           bigserial PRIMARY KEY,
    dataset_id   text NOT NULL REFERENCES stats.dataset(id) ON DELETE CASCADE,
    geo_level    text NOT NULL,           -- COM, EPCI, DEP, REG, FRANCE
    geo_code     text NOT NULL,
    geo_vintage  int,                     -- geography vintage the value refers to
    time_period  text NOT NULL,
    measure      text,                    -- main measure code (e.g. POP, DWELLINGS)
    dims         jsonb NOT NULL,          -- remaining dimensions
    value        double precision,        -- NULL when confidential / missing
    unit         text NOT NULL,           -- unit (pint-compatible where possible)
    unit_mult    int NOT NULL DEFAULT 0,  -- power of ten multiplier from the source
    status       text,                    -- OBS_STATUS
    conf_status  text,                    -- CONF_STATUS
    fetched_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS observation_lookup_idx
    ON stats.observation (dataset_id, geo_level, geo_code, time_period);
CREATE INDEX IF NOT EXISTS observation_dims_idx ON stats.observation USING gin (dims);

-- Statistics joined to the geometry valid for their geography vintage
CREATE OR REPLACE VIEW stats.observation_geo AS
SELECT o.*, a.name AS geo_name, a.geom
FROM stats.observation o
JOIN ref.admin_unit a
  ON a.level = o.geo_level
 AND a.code  = o.geo_code
 AND make_date(COALESCE(o.geo_vintage, a.vintage), 1, 1) >= a.valid_from
 AND (a.valid_to IS NULL OR make_date(COALESCE(o.geo_vintage, a.vintage), 1, 1) < a.valid_to);

-- ---------------------------------------------------------------------------
-- Catalogue of raster layers stored as Zarr on the host
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta.raster_dataset (
    name        text PRIMARY KEY,         -- dem, soil
    path        text NOT NULL,            -- path under GEOMON_DATA_DIR
    crs         text NOT NULL,
    resolution  double precision NOT NULL,
    source      text NOT NULL,
    description text,
    attrs       jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS meta.ingest_log (
    id          bigserial PRIMARY KEY,
    step        text NOT NULL,
    started_at  timestamptz NOT NULL,
    finished_at timestamptz,
    status      text NOT NULL,
    message     text,
    details     jsonb NOT NULL DEFAULT '{}'::jsonb
);

-- ---------------------------------------------------------------------------
-- Earth observation: per-date index statistics over the AOI areas (Sentinel-2)
-- The images themselves are Zarr stores under data/zarr/sentinel2/<site>/<year>.zarr
-- ---------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS eo;
CREATE TABLE IF NOT EXISTS eo.index_stats (
    site            text NOT NULL,
    area            text NOT NULL,        -- site code or INSEE commune code
    date            date NOT NULL,
    index           text NOT NULL,        -- ndvi, ndre, ...
    mean            real, median real, p10 real, p90 real, std real,
    n_area          int,                  -- pixels in the area
    n_valid         int,                  -- pixels with data
    n_clear         int,                  -- clear pixels (SCL 4, 5, 6 and red reflectance < 0.25)
    clear_fraction  real,
    PRIMARY KEY (site, area, index, date)
);
