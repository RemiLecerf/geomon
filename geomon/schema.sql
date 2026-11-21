-- Geomon database schema: the single definition of every table.
--
-- Applied by geomon.migrate (ingestion, scheduler, web API) when the database's schema version
-- is older than migrate.SCHEMA_VERSION. Every statement is idempotent: a change is made by editing
-- the definitions below (and adding what an existing database needs, as under "Removed"), then
-- bumping SCHEMA_VERSION.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS hstore;   -- used by ogr2ogr for OSM other_tags

CREATE SCHEMA IF NOT EXISTS ref;      -- reference vectors: admin units, RPG, OSM, farm plots
CREATE SCHEMA IF NOT EXISTS stats;    -- statistics (INSEE, later Eurostat...)
CREATE SCHEMA IF NOT EXISTS meta;     -- areas of interest, raster catalogue, ingestion log
CREATE SCHEMA IF NOT EXISTS eo;       -- Earth observation statistics (Sentinel-2)
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

-- ---------------------------------------------------------------------------
-- Areas of each site: its communes (kind 'commune'), the group itself (kind 'site'), the
-- farm's plots (kind 'farm')
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta.aoi (
    code       text NOT NULL,             -- INSEE commune code, the site code, or 'farm'
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
CREATE UNIQUE INDEX IF NOT EXISTS aoi_site_kind_code_idx ON meta.aoi (site, kind, code);

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

-- RPG 2007-2014 (RPG 1.0): only the farms' îlots (blocks of contiguous parcels of one farm), each
-- with its majority crop group, no parcel or crop
CREATE TABLE IF NOT EXISTS ref.rpg_ilot (
    id           bigserial PRIMARY KEY,
    year         int  NOT NULL,
    ilot_id      text,
    code_group   text,                    -- majority crop group (the codes of the parcels' groups)
    group_label  text,
    surf_ha      double precision,        -- graphic area of the îlot, ha
    group_ha     double precision,        -- area of the majority group in the îlot (2010-2014 only), ha
    commune      text,
    geom         geometry(MultiPolygon, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS rpg_ilot_geom_idx ON ref.rpg_ilot USING gist (geom);
CREATE INDEX IF NOT EXISTS rpg_ilot_year_id_idx ON ref.rpg_ilot (year, ilot_id);

-- ---------------------------------------------------------------------------
-- The farm's own plots (a PAC declaration export), per site
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ref.farm_plot (
    id         serial PRIMARY KEY,
    site       text,                  -- site the plots belong to
    campaign   int,                   -- PAC campaign (CAMPAGNE)
    number     text,                  -- plot or island number (NUMERO)
    island     text,                  -- reference island (ILOT_REF)
    commune    text,                  -- INSEE code declared (COMMUNE)
    surf_ha    double precision,      -- declared area (SURF_TOT), ha
    props      jsonb NOT NULL DEFAULT '{}'::jsonb,
    geom       geometry(MultiPolygon, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS farm_plot_geom_idx ON ref.farm_plot USING gist (geom);

-- ---------------------------------------------------------------------------
-- OpenStreetMap features (clipped to the AOI extent), per site
-- ---------------------------------------------------------------------------
-- The farm's land cover, one file per year (sites.json farm_landcover; the same file may serve several
-- years): its plots or fields with their crop (RPG codes)
CREATE TABLE IF NOT EXISTS ref.farm_landcover (
    id          bigserial PRIMARY KEY,
    site        text NOT NULL,
    year        int  NOT NULL,
    plot        text,
    code_cultu  text,
    code_group  text,
    crop_label  text,
    surf_ha     double precision,
    source      text,                 -- the file
    props       jsonb NOT NULL DEFAULT '{}'::jsonb,
    geom        geometry(MultiPolygon, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS farm_landcover_geom_idx ON ref.farm_landcover USING gist (geom);
CREATE INDEX IF NOT EXISTS farm_landcover_site_year_idx ON ref.farm_landcover (site, year);
-- A field's id: 'F' and a hash of its outline (the same field in several years' files has one id, and
-- one series); its Sentinel-2 statistics and curves are those of eo.parcel_* with rpg_year 0
ALTER TABLE ref.farm_landcover ADD COLUMN IF NOT EXISTS field_id text;

-- Soil and geology maps of a site (geomon.ingest.soil_maps): layer soils (Gis Sol dominant soils,
-- RRP 1:250 000), geology (BRGM 1:50 000 harmonised), clay (BRGM clay shrink-swell exposure)
CREATE TABLE IF NOT EXISTS ref.soil_feature (
    id      bigserial PRIMARY KEY,
    site    text NOT NULL,
    layer   text NOT NULL,
    code    text,                     -- class code (soil reference, geological notation, exposure level)
    label   text,                     -- class label
    detail  text,                     -- longer description (soil unit, formation)
    color   text,                     -- display colour (#rrggbb)
    props   jsonb NOT NULL DEFAULT '{}'::jsonb,
    geom    geometry(MultiPolygon, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS soil_feature_geom_idx ON ref.soil_feature USING gist (geom);
CREATE INDEX IF NOT EXISTS soil_feature_site_layer_idx ON ref.soil_feature (site, layer);

-- Groundwater levels (Hub'Eau, piezometers of the ADES network): stations, daily levels, and the
-- stations followed for each site
CREATE TABLE IF NOT EXISTS ref.gw_station (
    code_bss    text PRIMARY KEY,
    name        text,
    commune     text,
    lon         double precision,
    lat         double precision,
    altitude_m  double precision,     -- ground level (NGF)
    depth_m     double precision,     -- depth of the borehole
    aquifers    text,                 -- groundwater bodies (masses d'eau)
    first_date  date,
    last_date   date,
    props       jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE TABLE IF NOT EXISTS eo.gw_level (
    code_bss   text NOT NULL,
    date       date NOT NULL,
    depth_m    real,                  -- depth of the water below ground
    level_ngf  real,                  -- water level (m NGF)
    PRIMARY KEY (code_bss, date)
);
CREATE TABLE IF NOT EXISTS ref.gw_site (
    site         text NOT NULL,
    code_bss     text NOT NULL,
    distance_km  double precision,
    PRIMARY KEY (site, code_bss)
);

CREATE TABLE IF NOT EXISTS ref.osm_feature (
    id        bigserial PRIMARY KEY,
    site      text,
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
CREATE INDEX IF NOT EXISTS osm_feature_site_layer_idx ON ref.osm_feature (site, layer);

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
-- Datasets: how each one is followed for a site (chosen on the page's Data tab, see
-- geomon.datasets), and the actions asked from the page, run by the scheduler
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta.dataset_policy (
    site        text NOT NULL,
    dataset     text NOT NULL,
    mode        text NOT NULL,            -- update, frozen or live
    keep_hours  int,                      -- live: the hours kept
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site, dataset)
);
CREATE TABLE IF NOT EXISTS meta.dataset_request (
    id            bigserial PRIMARY KEY,
    site          text NOT NULL,
    dataset       text NOT NULL,
    action        text NOT NULL,          -- update or delete
    requested_at  timestamptz NOT NULL DEFAULT now(),
    started_at    timestamptz,
    finished_at   timestamptz,
    status        text NOT NULL DEFAULT 'pending',   -- pending, running, done, failed
    message       text
);
CREATE INDEX IF NOT EXISTS dataset_request_pending_idx ON meta.dataset_request (status, requested_at);

-- ---------------------------------------------------------------------------
-- Ingestion log
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta.ingest_log (
    id          bigserial PRIMARY KEY,
    site        text,
    step        text NOT NULL,
    started_at  timestamptz NOT NULL,
    finished_at timestamptz,
    status      text NOT NULL,
    message     text,
    details     jsonb NOT NULL DEFAULT '{}'::jsonb
);

-- ---------------------------------------------------------------------------
-- Sentinel-2 (the images are Zarr stores under data/zarr/sentinel2/<site>/[<part>/]<year>.zarr)
-- ---------------------------------------------------------------------------
-- Per-date index statistics over the areas of a site
CREATE TABLE IF NOT EXISTS eo.index_stats (
    site            text NOT NULL,
    area            text NOT NULL,        -- site code, INSEE commune code or 'farm'
    date            date NOT NULL,
    index           text NOT NULL,        -- ndvi, ndre, ...
    mean            real, median real, p10 real, p90 real, std real,
    n_area          int,                  -- pixels in the area
    n_valid         int,                  -- pixels with data
    n_clear         int,                  -- clear pixels (SCL 4, 5, 6 and red reflectance < 0.25)
    clear_fraction  real,
    PRIMARY KEY (site, area, index, date)
);

-- Sites as small integers in the largest tables (a site's name repeated in every row and in the key
-- took 17 % of eo.parcel_stats). meta.site_id gives a site its id (adding it), meta.site_key_of only
-- reads it (stable: computed once per query, the primary key usable).
CREATE TABLE IF NOT EXISTS meta.site_key (
    id    smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    site  text NOT NULL UNIQUE
);
CREATE OR REPLACE FUNCTION meta.site_id(s text) RETURNS smallint LANGUAGE sql AS $$
    WITH added AS (INSERT INTO meta.site_key (site) VALUES (s) ON CONFLICT (site) DO NOTHING RETURNING id)
    SELECT id FROM added UNION ALL SELECT id FROM meta.site_key WHERE site = s LIMIT 1
$$;
CREATE OR REPLACE FUNCTION meta.site_key_of(s text) RETURNS smallint LANGUAGE sql STABLE AS $$
    SELECT id FROM meta.site_key WHERE site = s
$$;

-- An eo.parcel_stats keyed by the site's name (before version 10): moved to the site key
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'eo' AND table_name = 'parcel_stats' AND column_name = 'site') THEN
        INSERT INTO meta.site_key (site) SELECT DISTINCT site FROM eo.parcel_stats ON CONFLICT DO NOTHING;
        CREATE TABLE eo.parcel_stats_v10 (
            site_id smallint NOT NULL, rpg_year int NOT NULL, parcel_id text NOT NULL, index text NOT NULL,
            date date NOT NULL, mean real, std real, n_pixels int, n_clear int);
        INSERT INTO eo.parcel_stats_v10
            SELECT k.id, p.rpg_year, p.parcel_id, p.index, p.date, p.mean, p.std, p.n_pixels, p.n_clear
            FROM eo.parcel_stats p JOIN meta.site_key k ON k.site = p.site;
        DROP TABLE eo.parcel_stats;
        ALTER TABLE eo.parcel_stats_v10 RENAME TO parcel_stats;
        ALTER TABLE eo.parcel_stats ADD PRIMARY KEY (site_id, rpg_year, index, parcel_id, date);
        ANALYZE eo.parcel_stats;
    END IF;
END $$;

-- Per-date index statistics over each RPG parcel's interior (see geomon.parcels)
CREATE TABLE IF NOT EXISTS eo.parcel_stats (
    site_id    smallint NOT NULL,      -- meta.site_key
    rpg_year   int  NOT NULL,          -- RPG year of the parcel outline
    parcel_id  text NOT NULL,          -- RPG id_parcel (unique within a year only)
    index      text NOT NULL,
    date       date NOT NULL,
    mean       real,
    std        real,
    n_pixels   int,                    -- interior pixels (outline shrunk by 10 m)
    n_clear    int,                    -- clear interior pixels with a valid index
    PRIMARY KEY (site_id, rpg_year, index, parcel_id, date)
);

-- Parcel-dates processed (also when no parcel was clear)
CREATE TABLE IF NOT EXISTS eo.parcel_dates (
    site text NOT NULL, rpg_year int NOT NULL, index text NOT NULL, date date NOT NULL,
    PRIMARY KEY (site, rpg_year, index, date)
);

-- Daily curve of each parcel and season
CREATE TABLE IF NOT EXISTS eo.parcel_season (
    site        text NOT NULL,
    season      int  NOT NULL,         -- RPG year of the crop (1 Sept season-1 .. 31 Dec season)
    index       text NOT NULL,
    parcel_id   text NOT NULL,
    rpg_year    int  NOT NULL,         -- outline used (the season's RPG, or the latest one)
    n_obs       int,
    first_obs   date,
    last_obs    date,
    peak        real,
    peak_date   date,
    curve       bytea,                 -- float32 little-endian, SEASON_DAYS values, NaN when empty
    n_pixels    int,                   -- interior pixels of the parcel (weight in the group means)
    computed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site, season, index, parcel_id)
);

-- Each parcel of a site, per RPG year: its crop and the areas it lies in (its point on surface),
-- for the crop-group curves below (version 2)
CREATE TABLE IF NOT EXISTS eo.parcel_info (
    site        text NOT NULL,
    rpg_year    int  NOT NULL,
    parcel_id   text NOT NULL,
    code_group  text,
    code_cultu  text,
    surf_ha     double precision,
    bio         boolean,
    areas       text[] NOT NULL,       -- codes of the site's areas containing the parcel
    PRIMARY KEY (site, rpg_year, parcel_id)
);
ALTER TABLE eo.parcel_info DROP COLUMN IF EXISTS farm_group;    -- replaced by eo.farm_crop
ALTER TABLE eo.parcel_info DROP COLUMN IF EXISTS farm_cultu;

-- The crop the farm declared (ref.farm_landcover) for the RPG parcels of a season lying in its plots
-- (the parcel's point on surface in a plot of the season's year): used instead of the RPG's
CREATE TABLE IF NOT EXISTS eo.farm_crop (
    site        text NOT NULL,
    season      int  NOT NULL,
    rpg_year    int  NOT NULL,
    parcel_id   text NOT NULL,
    code_group  text,
    code_cultu  text,
    PRIMARY KEY (site, season, rpg_year, parcel_id)
);
CREATE INDEX IF NOT EXISTS parcel_info_areas_idx ON eo.parcel_info USING gin (areas);

-- Crop-group curves per area and season, computed from the parcel curves at ingestion, so that the
-- web app reads a few rows instead of every parcel's curve. code_group '' holds the count of the
-- parcels whose crop is not known yet (no curve).
CREATE TABLE IF NOT EXISTS eo.group_season (
    site         text NOT NULL,
    index        text NOT NULL,
    area         text NOT NULL,
    code_group   text NOT NULL,
    season       int  NOT NULL,
    parcels      int,
    ha           double precision,
    status       text,                 -- declared, or assumed (permanent crop, RPG not published yet)
    mean_pixels  bytea,                -- float32 x SEASON_DAYS: parcels weighted by their interior pixels
    mean_parcel  bytea,                -- one parcel, one vote
    q25          bytea,
    q75          bytea,
    count        bytea,                -- int16 x SEASON_DAYS: parcels with a value
    computed_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site, index, area, code_group, season)
);

-- ---------------------------------------------------------------------------
-- Removed
-- ---------------------------------------------------------------------------
-- version 3: the raster catalogue, written by the ingestion but read by nothing
DROP TABLE IF EXISTS meta.raster_dataset;
