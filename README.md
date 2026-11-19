# Geomon — first product

This first version handles static data only, for one area of interest: the commune of
**Montseveroux** (Isère; INSEE code 38259, postal code 38122). You explore it on an interactive
web map. See [Specifications.md](Specifications.md) for the full project.

| Data | Source | Storage |
|---|---|---|
| Administrative hierarchy: commune (+ neighbours), EPCI, arrondissement, département, région, France | IGN ADMIN EXPRESS COG 2026 (Géoplateforme WFS) | PostGIS `ref.admin_unit`, with validity dates |
| Agricultural parcels, RPG 2015–2025 (every year: crop history of each parcel) | IGN Géoplateforme WFS (`RPG.LATEST` for the latest year, checked at download; `RPG.<year>` before) | PostGIS `ref.rpg_parcel`, `ref.rpg_crop_code` |
| OpenStreetMap: roads, buildings, land use, water, waterways, railways, POIs | Overpass API, one query over the AOI extent (Geofabrik extracts optional, for later) | PostGIS `ref.osm_feature`. Raw response kept in `data/raw/osm` |
| Elevation model, 5 m, plus slope and aspect | IGN RGE ALTI (Géoplateforme WMS-R) | Zarr `data/zarr/dem.zarr` |
| Soil, 6 layers down to 2 m | ISRIC SoilGrids 2.0 (250 m) | Zarr `data/zarr/soil.zarr` |
| Derived soil hydraulics: water content at field capacity, wilting point and saturation, saturated conductivity (Ksat), available water capacity (AWC), USDA texture class | Saxton & Rawls (2006) pedotransfer functions | Zarr `data/zarr/soil.zarr` |
| Satellite, Sentinel-2 L2A (Sen2Cor atmospheric correction), since 2015-06-27, 10 m: bands B04 and B08 (NDVI) plus the scene classification (SCL); more bands on demand (B05 for NDRE, …) | Copernicus Data Space, through openEO (free account) | Zarr `data/zarr/sentinel2/<site>/<year>.zarr`; per-date index statistics per area in `eo.index_stats`, per RPG parcel in `eo.parcel_stats`, smoothed parcel-season curves in `eo.parcel_season` |
| Weather, hourly: Météo-France AROME/ARPEGE archived forecasts (from 2022-11-13) and the latest forecast (AROME/ARPEGE to ~4 days, then ECMWF IFS to 15 days) | Open-Meteo (free, non-commercial use) | Zarr `data/zarr/weather/om_mf_hist.zarr`, `om_forecast.zarr` |
| Reanalysis, hourly: ERA5-Land 0.1° (soil temperature and moisture, snow), ERA5 0.25° for rain, wind, radiation, ET0 | Open-Meteo (Copernicus) | Zarr `data/zarr/weather/om_era5land.zarr` |
| Agrometeorological indicators, daily, since 1979: AgERA5, 0.1° (ERA5 aggregated to local days and corrected to the 0.1° topography), about 10 days behind | Copernicus CDS (free account, CC-BY 4.0) | Zarr `data/zarr/weather/agera5.zarr`. Download of the AOI box in `data/raw/agera5` |
| Reanalysis, daily: SAFRAN-ISBA (SIM2), 8 km, since 1990 (for the 1991–2020 normals), with ETP, soil wetness index, drainage, snow | Météo-France open data (meteo.data.gouv.fr) | Zarr `data/zarr/weather/sim2.zarr`. Only the AOI cells are kept, in `data/raw/sim2` |
| Descriptive statistics: population, households, dwellings, employment, income | INSEE Melodi API, for the commune, EPCI, département, région and France | PostgreSQL `stats.*`, with units and geography vintage |

All data lives on the host, under `./data`. The containers hold only code (Specifications §3.1).

## Run it

Requirements: Docker Desktop (or Docker Engine with Compose v2) and an internet connection.
Run the commands from the project folder (PowerShell, a shell or the VS Code terminal), with Docker running.

```bash
docker compose build
docker compose up -d                       # PostGIS + web app + weather scheduler
docker compose run --rm ingest all         # download and load all data (first weather history: ~2 h)
```

Open **http://localhost:8088**.

To re-run one step:

```bash
docker compose run --rm ingest rpg         # steps: admin rpg osm dem soil insee weather sim2 agera5 sentinel2 forecast (or all)
curl -X POST http://localhost:8088/api/reload   # after re-running dem or soil, reload the rasters
```

Everyday commands:

```bash
docker compose ps                          # what is running
docker compose stop                        # stop everything (data stays in ./data)
docker compose down                        # stop and remove the containers (data stays in ./data)
docker compose build && docker compose up -d   # after changing the code or .env: rebuild, restart what changed
docker compose logs -f api                 # follow a service's log (api, scheduler, db); Ctrl+C to quit
docker compose restart scheduler
docker ps                                  # find a stuck manual run (geomon-ingest-run-...)
docker stop <container name>               # and stop it
```

The services restart on their own when Docker starts. For the weather to keep updating after a
reboot, turn on "Start Docker Desktop when you sign in" in Docker Desktop's settings (General).

### Weather updates

Weather updates are automatic: the `scheduler` service (started by `docker compose up -d`) fetches each
source when it publishes, and only downloads what is new:

| Job | Published | Checked | Step |
|---|---|---|---|
| Forecast | AROME every 3 h, ARPEGE and ECMWF IFS every 6 h | every 15 min, from Open-Meteo's model run times; at most one download every 3 h (`GEOMON_FORECAST_MIN_HOURS`) | `forecast` |
| Météo-France archive, ERA5-Land / ERA5 | daily | daily at 02:00 UTC | `weather` |
| SIM2 | daily ("latest" file) | every 6 h | `sim2` |
| AgERA5 | daily, ~10 days behind | every 6 h | `agera5` |
| Sentinel-2 | each scene, ~5 h after acquisition | every 3 h, from the Copernicus STAC catalogue (no openEO credits) | `sentinel2` |

One scheduler serves every site: each job checks its source once (one look at the forecast models' run
times, one daily slot...) and, when there is something new, updates every site in turn; only the
Sentinel-2 search is made per site, for its extent. SIM2's national files are downloaded once and keep
the cells of all the sites. Each job has its own thread and lock, so a job waiting for a download (the
Open-Meteo daily quota, the CDS queue) does not hold up the others.

A site whose update failed is retried alone after 30 minutes; a site added to `sites.json` is brought up
to date at once; a job missed while the computer was off runs at the next start.
`docker compose exec scheduler python -m geomon.scheduler --status` shows the last runs; `docker compose logs -f
scheduler` follows them. Steps can still be run by hand (`ingest forecast`, `ingest weather sim2 agera5`): a
database lock makes a manual run wait for a scheduled one, so they never write the same data at once.

AgERA5 needs a free Copernicus account: put the personal access token
(https://cds.climate.copernicus.eu/profile) in `GEOMON_CDS_API_KEY` in `.env`, and accept the
licence once on the dataset's Download tab. Without a token the step is skipped. The CDS queues
requests: the first run sends a single request (all variables, the whole history since 1979, AOI box), which can wait
from minutes to an hour in the queue. Later runs only ask for the days published since (plus the last 10
again) and append them to the store; nothing is requested when no new day has been published.

The Météo-France archive at Open-Meteo is slow (1–3 min per month for all points): the first run takes
about 2 hours. Fetched months are kept in `data/raw/openmeteo`, so an interrupted run resumes. Later runs
only ask for the days since the last run, plus the last 3 again (`GEOMON_WEATHER_OVERLAP_DAYS`, as the
archives fill in late), rather than the whole current month (Météo-France) or year (ERA5-Land): for
ERA5-Land this divides the daily quota used by about 20. ERA5-Land is first published as preliminary
data (ERA5T), replaced by the final release 2–3 months later: once a week the last 92 days are fetched
again so that these corrections reach the store (`GEOMON_ERA5_REVISION_DAYS`,
`GEOMON_ERA5_REVISION_EVERY_DAYS`; about 300 quota calls for both sites). The store keeps what is already assembled:
only the new days are parsed and appended, and nothing is written when nothing changed.

Open-Meteo's free quota (10,000 calls a day) counts each grid point of a request as one call, weighted by
the number of variables and days. Only the points whose cell touches one of the site's communes are
requested (34 for Dragey-Ronthon rather than 224 over its box, 11 for Montseveroux). When the quota is
reached, the step stops at once (completed months are kept) and the scheduler tries again every 30
minutes; the quota is restored the next day.

The first SIM2 run streams the national files from 1990 (about 4.4 GB, 40 s per year, once); only
the sites' cells are kept (a few MB). Past years are checked against the server once a month; the
store is rebuilt only when one of its files changed (usually the daily "latest" file).

### Sentinel-2

Sentinel-2 L2A comes from the Copernicus Data Space Ecosystem through **openEO**, so only the AOI extent,
the requested bands and the scenes below the cloud threshold are transferred. A free account is needed;
log in once (open the printed link and confirm the code):

```bash
docker compose run --rm ingest sentinel2-login     # the refresh token is kept in data/state/xdg
docker compose run --rm ingest sentinel2           # history since 2015 (a few batch jobs), then updates
```

- **Cloud mask:** a pixel counts as clear when Sen2Cor's SCL classifies it as vegetation, not vegetated
  or water (classes 4, 5, 6; "unclassified" is left out, it is mostly haze and cloud edges) and its red
  reflectance is below 0.25. The red test catches fog and flat low cloud, which Sen2Cor often classifies
  as bare soil (on 2024-10-30 and 2025-10-30 it marked 70–80 % of the AOI clear under fog). After changing
  the mask, `docker compose run --rm ingest sentinel2-stats` recomputes the statistics without downloading.
- **Bands:** `GEOMON_S2_BANDS=B04,B08` (NDVI) by default, always with SCL for the cloud mask. Add bands at
  any time, e.g. `B04,B08,B05` for NDRE, or `B03`, `B11` for NDWI and NDMI: the next run fetches them for
  every stored date, in one job per few years, and computes the new indices.
- **Jobs:** the history is fetched in batch jobs of `GEOMON_S2_YEARS_PER_JOB` years (default 4), two at a
  time; updates are small synchronous requests. Scenes whose tile cloud cover exceeds
  `GEOMON_S2_MAX_CLOUD` (80 %) are not requested; dates with less than 5 % of the AOI clear are not stored.
- **Parts:** when a site's communes form separate clusters more than 5 km apart (Dragey-Ronthon and
  Beauficel, 35 km east), each cluster has its own grid and stores (`<site>/p1`, `<site>/p2`): only the
  communes and their surroundings are downloaded and processed, not the land between them. Area
  statistics gather the pixels of all the parts, and the map shows one image per part.
- **Storage:** one Zarr store per year, native 10 m UTM grid (20 m bands resampled to 10 m by the
  back-end), reflectance as integers (× 0.0001). Index statistics per date and area (mean, median,
  10th/90th percentiles, clear fraction) are stored in PostgreSQL, so time series load instantly.
- openEO batch jobs use credits of the Copernicus Data Space free monthly allowance; an AOI of a few
  communes needs few.

#### Parcels and crop groups

Index series are also kept per RPG parcel, which removes the mixing of land covers of the area means:

1. **Parcel-date statistics** (`eo.parcel_stats`, the full history): for every stored date, the mean and
   standard deviation of each index over the parcel's interior (outline shrunk by 10 m, so that pixels
   shared with a hedge, a road or the next field are left out), clear pixels only. Each season uses the
   parcels of its own RPG year: outlines and identifiers change every year.
2. **Parcel-season curves** (`eo.parcel_season`): a daily curve per parcel and season, from the dates on
   which at least `GEOMON_S2_PARCEL_MIN_CLEAR` (80 %) of the parcel is clear. By default
   (`GEOMON_S2_PARCEL_SMOOTHING=none`) it is a linear interpolation between these dates. With
   `whittaker`, it is smoothed with a weighted Whittaker smoother (λ = 100, chosen by leave-one-out:
   unseen dates are predicted within 0.02 NDVI, median) whose observations far below the curve get a low
   weight (residual cloud, haze and shadow lower the index), so the curve follows the upper envelope.
   Gaps of more than 60 days stay empty. The season peak (1 January–31 August) and its date are stored
   too. After changing the method, run `ingest sentinel2-parcels`.
3. **References** (computed on request): the curve of a crop group in an area is the mean, day by day, weighted by each parcel's interior pixels
   (the area measured; "one parcel, one vote" can be chosen in the view),
   of its parcels' curves; the 3-year, 5-year and long-term references are the means of the group curves
   of the seasons before the one compared, and the long-term 10th–90th percentile of these seasons gives
   the normal range.

A **season** Y is the crop of RPG year Y, followed from 1 September Y-1 (autumn sowing) to 31 December Y
(last harvests); a date of September to December belongs to two seasons. The RPG of the current year is
published about a year later: until then the latest RPG outlines are used, with its crop for permanent
crops only (permanent grassland, moorland, orchards, vines, nut trees, olive groves). Other parcels can
still be followed one by one, against the references of their last known crop.

The parcel series are updated by every `sentinel2` run. After loading RPG years (`ingest rpg`, every
year since `GEOMON_RPG_FIRST_YEAR=2015`), recompute them without downloading anything (about 2 minutes):

```bash
docker compose run --rm ingest sentinel2-parcels
```

### Sites

One server can follow several sites (groups of communes), for example Montseveroux in Isère and
Dragey-Ronthon in the Manche. List them in `data/sites.json` (start from `sites.example.json`:
`cp sites.example.json data/sites.json`, then edit it); the first one is shown by default. Each site has
a `code` (letters, digits and dashes: it names its folders), a `name`, its `communes` (INSEE codes) and,
optionally, a `farm_file` and `farm_name` (see "The farm's plots"; leave them out without a plots file):

```json
[
  {"code": "montseveroux-3", "name": "Montseveroux et voisines", "communes": ["38259", "38244", "38066"]},
  {"code": "dragey-ronthon-6", "name": "Dragey-Ronthon et voisines",
   "communes": ["50167", "50565", "50199", "50496", "50066", "50040"],
   "farm_file": "farm/dragey-ronthon-6/parcellaire_pac_wgs84.geojson", "farm_name": "My plots (PAC 2025)"}
]
```

- **Web page:** a *Site* menu in the header switches between them (the choice is kept in a cookie;
  `http://localhost:8088/?site=dragey-ronthon-6` opens a site directly).
- **Ingestion:** every step runs for every site, one after the other, unless `--site` is given:
  `docker compose run --rm ingest --site dragey-ronthon-6 rpg sentinel2`. A new site needs one
  `ingest --site <code> all`.
- **Scheduler:** one for all the sites; each source is checked once and updates every site (see
  "Weather updates"). A new site is brought up to date by the scheduler at its next start.
- **Storage:** rasters and weather stores in `data/zarr/sites/<code>/`, Sentinel-2 in
  `data/zarr/sentinel2/<code>/`; database rows carry the site code. Without `sites.json`, the single site
  of `GEOMON_COMMUNES` is used as before.

### The farm's plots

`farm_file` in `sites.json` (or `GEOMON_FARM_FILE` for a single site; optional) points to a GeoJSON of
the farm's own plots, in WGS84, relative to the data directory: for example a PAC declaration export (`CAMPAGNE`, `NUMERO`, `ILOT_REF`, `COMMUNE`,
`SURF_TOT` are read when present). The `farm` step (part of `all`) loads them into `ref.farm_plot`, and
their union becomes an analysis area, "My plots", next to the communes and the group: land summary,
weather, Sentinel-2 series, and crop groups of the RPG parcels lying in the plots. In the crop-group view,
the plots' current season can be compared with the references of the whole group or with the plots' own
earlier seasons. The plots are drawn on the map; their communes must be in `GEOMON_COMMUNES`.

```bash
docker compose run --rm ingest farm                # after changing the file
docker compose run --rm ingest sentinel2-stats     # Sentinel-2 area series of the new area
```

### Communes and groups of communes

The AOI is one commune or a group of communes, set in `.env` (copy `.env.example`):

```bash
GEOMON_COMMUNES=38259,38244,38066          # INSEE codes: Montseveroux, Monsteroux-Milieu, Chalon
GEOMON_SITE_CODE=montseveroux-3            # short code, used in file names (default grp-<codes>)
GEOMON_SITE_NAME=Montseveroux et voisines  # display name (default: the commune names)
```

then run `docker compose run --rm ingest all` and `docker compose up -d` (the scheduler picks up the
new AOI). Data is downloaded once, for the buffered extent of the whole group, so neighbouring communes
share their weather cells, rasters and parcels. Each commune and the group are then analysed separately:

- **Crops, terrain and soil:** clipped to the outline of the commune or the group.
- **INSEE statistics:** stored per commune; the group's values are sums of its communes, for counts
  only (population, dwellings, households). Rates, means and medians cannot be summed and are shown
  as "not summable".
- **Weather:** averaged over the area, each grid cell weighted by the part of it inside the area.

The data tables hold one AOI at a time (except the administrative units): changing `GEOMON_COMMUNES`
replaces the data of the previous AOI. Caches of earlier AOIs stay under `data/raw/*/<code>` and can be
deleted.

## The web page

The **Area** selector under the title (shown for a group) chooses the group or one of its communes.
Every tab follows it; the selected area is outlined in red on the map.

- **Layers:** base maps (Plan IGN, IGN orthophotos, OSM) and vector layers you can toggle. RPG
  parcels are coloured by crop group, and the legend gives crop areas inside the selected area. Raster
  overlays: elevation, hillshade, slope, aspect, and every soil property by depth, with a legend in
  physical units.
- **Statistics:** a land summary of the selected area (surface, declared crops, elevation, slope,
  available water capacity, topsoil texture, clay, sand, organic carbon and pH), then the administrative
  hierarchy, the municipal population series, and key indicators (totals, plus breakdowns by age, sex,
  dwelling type, …) for the group, each commune or any parent unit. A dataset explorer shows the full
  tables with labelled dimensions.
- **Weather:** daily, weekly or monthly series of any variable, averaged over the selected area (or at
  a point clicked on the map), comparing
  Météo-France, ERA5-Land, AgERA5 and SIM2: the AgERA5 set (temperature min/mean/max, precipitation, radiation,
  humidity at 06–18 h, vapour pressure, wind, snow), plus ET0, soil moisture and temperature, and the SIM2
  soil wetness index and drainage. The forecast is shaded; a table gives the mean difference and
  correlation with SIM2; a second table gives the daily forecast. Click the map for a point; the
  grid cells of each source can be shown. A second view compares a year (from 1 January, 1 September
  or 1 October) with the **1991–2020 normal** (WMO reference period) of the same source, SIM2 (updated
  daily) or AgERA5: daily values against the normal with three bands (min–max, 10th–90th percentile,
  interquartile range) for averaged variables, totals since the start of the year for precipitation,
  ET0 and other sums. The observations continue with the Météo-France forecast (and, for AgERA5,
  the Météo-France archive over the days it is behind), shaded; the chart shows the whole year or the
  last 90 days and the forecast. A table gives the last 7 days, the last 30 days, the year so far, the
  forecast days and the year with the forecast (difference, share of normal, quantiles, rank among
  the 30 reference years).
- **Sentinel-2:** time series of an index (NDVI, and NDRE, NDWI or NDMI when their bands are stored)
  averaged over the selected area, clear pixels only, with the spread of its pixels; filter by year and
  by the share of the area that must be clear. Click a date to show the index image on the map, with or
  without the cloud mask. Two other views: **crop group** (mean curve of the group's parcels in the
  area for a season, the middle half of the parcels, and the 3-year, 5-year and long-term means with the
  long-term range; a table compares the latest value, the peak and the mean since 1 January) and **one
  parcel** (click it on the map: its curve and clear dates for a season against its crop group and its own
  earlier seasons with the same crop, and its crop history since 2015).
- **Point:** click anywhere to get elevation, slope, aspect, the RPG parcel (crop, area, organic
  status) and the full soil profile with estimated available water capacity.

## API

| Endpoint | Content |
|---|---|
| `GET /api/aoi` | Area of interest: its communes, the group, the analysis areas, the parent units |
| `GET /api/area.geojson?area=`, `GET /api/area/summary?area=` | Outline of an area; its land summary (terrain, soil, crops) |
| `GET /api/admin.geojson?levels=COM,EPCI` | Administrative units |
| `GET /api/rpg.geojson`, `GET /api/rpg/summary?area=` | RPG parcels, crop areas in an area |
| `GET /api/farm.geojson` | The farm's plots, with the latest RPG crop of each |
| `GET /api/osm/{roads,buildings,landuse,water,waterways,railways,pois}.geojson` | OSM layers |
| `GET /api/rasters` | Raster catalogue: variables, units, depths |
| `GET /api/raster/{dem,soil}/{var}.png?depth=i`, `…/info` | Rendered overlay and its legend |
| `GET /api/point?lon=&lat=` | Every layer at a point |
| `GET /api/stats/datasets`, `summary`, `series`, `table` (`level=GRP&code=<site>` for the group) | Statistics |
| `GET /api/weather/products` | Weather products, periods, variables and units |
| `GET /api/weather/daily?area=&var=t_max&series=mf,era5land,agera5,sim2&step=day` | Daily, weekly or monthly series over an area (or `lon=&lat=` for a point) |
| `GET /api/weather/normals?area=&var=precip&series=sim2&start=2026-01-01` | One year against the 1991–2020 normal of the same source (`sim2` or `agera5`), continued by the Météo-France archive and forecast (`forecast=false` to leave them out) |
| `GET /api/weather/forecast?area=` | Daily summary of the latest forecast over an area (or at a point) |
| `GET /api/weather/cells.geojson?product=sim2` | Grid cells of a product |
| `GET /api/s2/info` | Sentinel-2 stores: dates, bands, indices available |
| `GET /api/s2/series?index=ndvi&area=&min_clear=0.5` | Index statistics per date over an area |
| `GET /api/s2/{index}.png?date=&masked=true`, `…/info` | Index image of a date and its legend |
| `GET /api/s2/crops?index=ndvi&area=&season=` | Seasons with parcel curves, crop groups of an area in a season |
| `GET /api/s2/crop?group=1&season=2025&index=ndvi&area=` | Crop group curve, observations, 3-year / 5-year / long-term references |
| `GET /api/s2/parcel?lon=&lat=&season=&index=ndvi` | Parcel at a point: crop history, curve and observations of a season |
| `GET /api/units/convert?value=15&from=degC&to=K` | Unit conversion (Specifications §3.5) |
| `GET /api/status` | Last ingestion result of each step |

Interactive documentation: http://localhost:8088/docs. The database is reachable from the host at
`localhost:5433` (user and password `geomon`), e.g. from QGIS.

## Layout

```
docker-compose.yml          db (PostGIS 18), api (web app), ingest (one-shot data loading)
docker/geomon/Dockerfile    Debian testing + GDAL + Python geospatial stack
db/init/01_schema.sql       schemas ref, stats, meta, staging
geomon/
  config.py                 settings from environment variables
  aoi.py                    area of interest: one commune or a group (outlines, buffered extent)
  units.py                  unit conversion helper (pint)
  weather.py                weather products: loading, AgERA5-style daily aggregation
  scheduler.py              keeps the weather and Sentinel-2 up to date (compose service "scheduler")
  sentinel2.py              Sentinel-2 stores, cloud mask (SCL), spectral indices, area statistics
  parcels.py                index series per RPG parcel, smoothed season curves, crop-group references
  ingest/                   one module per source: admin, rpg, osm, dem, soil, insee, openmeteo, sim2, agera5, sentinel2
  api/                      FastAPI app, raster rendering and point sampling
web/                        static page (MapLibre GL JS, no build step)
```

## Known limitations

- The image is the slim "first product" variant. Rust, CUDA and the ML libraries (Specifications
  §2.4) will come in a heavier image variant.
- SoilGrids publishes no water-retention layers. Field capacity, wilting point, Ksat and AWC are
  therefore pedotransfer estimates, and AWC is reduced by the coarse fragment fraction. Bedrock
  depth and groundwater depth are not covered yet (BRGM to come).
- Only the COG 2026 boundaries are loaded. INSEE publishes some datasets on another geography
  vintage (e.g. 2025 for historical populations). The vintage is stored with each value, but the
  boundaries of earlier vintages still need loading for exact spatio-temporal joins.
- INSEE income data (Filosofi) is confidential for communes this small. The page says so.
- Weather: the Météo-France history is made of archived forecasts, not a reanalysis. Against SIM2 it
  matches closely for temperature but is drier for rain; bias correction is still to do. The original
  Station data and radar rainfall (COMEPHORE) are not connected yet.
- Eurostat is not connected yet (Specifications §7.2, step 5).
- OpenStreetMap comes from the public Overpass API: one query per run, reused for 30 days. If the
  server is busy, retry later or add mirrors in `GEOMON_OVERPASS_URLS`. For larger areas, set
  `GEOMON_OSM_SOURCE=geofabrik`: this downloads a regional extract (533 MB for Rhône-Alpes) and
  clips it locally.
- Web map tiles come from IGN and OpenStreetMap servers. OSM's tile server is for light use only.
