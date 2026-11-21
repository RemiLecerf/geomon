# Geomon — first product

This first version handles static data only, for one area of interest: the commune of
**Montseveroux** (Isère; INSEE code 38259, postal code 38122). You explore it on an interactive
web map. See [Specifications.md](Specifications.md) for the full project.

| Data | Source | Storage |
|---|---|---|
| Administrative hierarchy: commune (+ neighbours), EPCI, arrondissement, département, région, France | IGN ADMIN EXPRESS COG 2026 (Géoplateforme WFS) | PostGIS `ref.admin_unit`, with validity dates |
| Agricultural parcels, RPG 2015–2025 (every year: crop history of each parcel) | IGN Géoplateforme WFS (`RPG.LATEST` for the latest year, checked at download; `RPG.<year>` before) | PostGIS `ref.rpg_parcel`, `ref.rpg_crop_code` |
| Farm îlots, RPG 1.0 2007–2014 (blocks of parcels of one farm, with their majority crop group only) | 2010–2014: IGN WFS `RPG.<year>:rpg_<year>`; 2007–2009: IGN download service, regional 7z archives (105 MB for Normandie, 285 MB for Auvergne-Rhône-Alpes per year), deleted once loaded | PostGIS `ref.rpg_ilot` |
| OpenStreetMap: roads, buildings, land use, water, waterways, railways, POIs | Overpass API, one query over the AOI extent (Geofabrik extracts optional, for later) | PostGIS `ref.osm_feature`. Raw response kept in `data/raw/osm` |
| Elevation model, 5 m, plus slope and aspect | IGN RGE ALTI (Géoplateforme WMS-R) | Zarr `data/zarr/sites/<site>/dem.zarr`, overviews `dem_ov4`, `dem_ov16`, `dem_ov64`: scaled integers (elevation to the cm, slope and aspect to 0.01°, byte-shuffled zstd), 60 % smaller than float32 |
| Soil, 6 layers down to 2 m | ISRIC SoilGrids 2.0 (250 m) | Zarr `data/zarr/sites/<site>/soil.zarr` |
| Soils of France, 6 layers: texture, coarse fragments, water at field capacity and wilting point, available water capacity (réserve utile) | INRAE / Gis Sol digital soil maps, 90 m (Recherche Data Gouv, doi:10.15454/9IRARJ and doi:10.57745/N4E4NE, Licence Ouverte) | Zarr `data/zarr/sites/<site>/soil_inrae.zarr` |
| Dominant soils (RRP 1:250 000), geology 1:50 000, clay shrink-swell exposure | IGN WFS `INRAE_CARTE-SOLS` (Gis Sol); BRGM BD Charm-50 (InfoTerre) and `ALEARG` WFS | PostGIS `ref.soil_feature` |
| Groundwater levels (nearest piezometers) | Hub'Eau API "niveaux des nappes" (ADES) | PostGIS `ref.gw_station`, `eo.gw_level` |
| Derived soil hydraulics: water content at field capacity, wilting point and saturation, saturated conductivity (Ksat), available water capacity (AWC), USDA texture class | Saxton & Rawls (2006) pedotransfer functions | Zarr `data/zarr/soil.zarr` |
| Satellite, Sentinel-2 L2A (Sen2Cor atmospheric correction), since 2015-06-27, 10 m: bands B04 and B08 (NDVI) plus the scene classification (SCL); more bands on demand (B05 for NDRE, …) | Copernicus Data Space, through openEO (free account) | Zarr `data/zarr/sentinel2/<site>/<year>.zarr`; per-date index statistics per area in `eo.index_stats`, per RPG parcel in `eo.parcel_stats`, smoothed parcel-season curves in `eo.parcel_season` |
| Weather, hourly: Météo-France AROME/ARPEGE archived forecasts (from 2022-11-13) and the latest forecast (AROME/ARPEGE to ~4 days, then ECMWF IFS to 15 days) | Open-Meteo (free, non-commercial use) | Zarr `data/zarr/weather/om_mf_hist.zarr`, `om_forecast.zarr` |
| Reanalysis, hourly: ERA5-Land 0.1° (core weather, snow, soil temperature and moisture in 4 layers) | Copernicus CDS, `reanalysis-era5-land-timeseries` (free account, CC-BY 4.0); Open-Meteo as an option | Zarr `data/zarr/sites/<site>/weather/era5land.zarr` |
| Reanalysis, hourly: ERA5 0.25° (core weather, plus cloud cover, boundary-layer height, gusts, 100 m wind, surface heat fluxes and the actual evapotranspiration derived from them) | Copernicus CDS, `reanalysis-era5-single-levels-timeseries` | Zarr `data/zarr/sites/<site>/weather/era5.zarr` |
| Agrometeorological indicators, daily, since 1979: AgERA5, 0.1° (ERA5 aggregated to local days and corrected to the 0.1° topography), about 10 days behind | Copernicus CDS (free account, CC-BY 4.0) | Zarr `data/zarr/weather/agera5.zarr`. Download of the AOI box in `data/raw/agera5` |
| Reanalysis, daily: SAFRAN-ISBA (SIM2), 8 km, since 1990 (for the 1991–2020 normals), with ETP, soil wetness index, drainage, snow | Météo-France open data (meteo.data.gouv.fr) | Zarr `data/zarr/weather/sim2.zarr`. Only the AOI cells are kept, in `data/raw/sim2` |
| Measured daily weather of the Météo-France stations within 40 km (up to 8, still measuring): rain, temperature, wind, gusts, humidity, and where measured radiation, ETP and snow depth, since 1990 | Météo-France "Données climatologiques de base - quotidiennes" (meteo.data.gouv.fr, Licence Ouverte, no account) | Zarr `data/zarr/sites/<site>/weather/mf_stations.zarr`; département files in `data/raw/mf_stations` |
| Rainfall reanalysis COMEPHORE: hourly, 1 km, radar merged with rain gauges, since 1997, about two months behind (kept as daily sums over UTC days) | Météo-France "Réanalyses Coméphore" (meteo.data.gouv.fr, Licence Ouverte, no account) | Zarr `data/zarr/sites/<site>/weather/comephore.zarr`: every day since `GEOMON_COMEPHORE_START`, each month written in its place as it is read, for every site lacking it (one stream per month for all the sites). The monthly archives (50–250 MB) are streamed, never stored |
| Daily Penman-Monteith ETP, 0.125° grid, since 1970 | Météo-France "Données ETP spatialisées quotidiennes" (meteo.data.gouv.fr, Licence Ouverte) | Zarr `data/zarr/sites/<site>/weather/mf_etp.zarr`; yearly files in `data/raw/mf_etp` |
| Observations of the last days, hourly, at the stations within 40 km | Météo-France API "Paquet Observation" (key) | Zarr `data/zarr/sites/<site>/weather/mf_obs.zarr` (live: last 120 hours) |
| Vigilance (today, tomorrow, bulletins) and Météo des forêts (fire danger, in season) of the site's départements | Météo-France APIs "Vigilance" and "Météo des forêts" (key) | `data/raw/meteofrance_alerts` (shared by the sites) |
| Forecast maps, AROME 1.3 km: rain, temperature, gusts for 51 hours (hourly for a day, then 3-hourly) | Météo-France API "AROME" (WCS, key) | Zarr `data/zarr/sites/<site>/arome_maps.zarr` (latest run) |
| Ensemble probabilities of rain and frost, PE-AROME (25 members) | Météo-France API "PE-AROME" (WCS, key) | `data/zarr/sites/<site>/ensemble_pearome.json` (latest run) |
| Waves (MFWAM) and storm surge (HYCOM2D), coastal sites | Météo-France API "WavesModels" (WCS, key) | Zarr `data/zarr/sites/<site>/sea.zarr` (latest run) |
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
docker compose run --rm ingest rpg         # steps: admin rpg osm dem soil insee weather era5land era5 sim2 mf-stations mf-etp comephore agera5 sentinel2 sentinel1 modis radar forecast (or all)
```

The web app picks up re-ingested data by itself (its caches follow the stores' modification times).
The `osm` step runs for one site at a time, even from several terminals (Overpass asks for no
parallel queries).

Unit tests (no database, network or data needed; they run on the working copy, without rebuilding):

```bash
docker compose run --rm test
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
| Météo-France archive | daily | daily at 02:00 UTC | `weather` |
| SIM2 | daily ("latest" file) | every 6 h | `sim2` |
| AgERA5 | daily, ~10 days behind | every 6 h | `agera5` |
| Météo-France stations | daily (the day before, in the morning) | daily at 08:00 UTC | `mf-stations` |
| Météo-France ETP grid | daily | daily at 08:30 UTC | `mf-etp` |
| COMEPHORE | monthly, ~2 months behind | daily at 03:00 UTC: new months, and the record filled backwards within `GEOMON_COMEPHORE_BUDGET_MIN` minutes (20 by default) per run | `comephore` |
| ERA5-Land, ERA5 | daily, ~10 days behind | every 6 h | `era5land`, `era5` |
| Sentinel-2 | each scene, ~5 h after acquisition | every 3 h, from the Copernicus STAC catalogue (no openEO credits) | `sentinel2` |
| Sentinel-1 | each acquisition, a few hours after | every 3 h, from the Copernicus STAC catalogue | `sentinel1` |
| MODIS NDVI | 16-day composites, 1 to 4 weeks after their end | every 12 h (the step searches the catalogue itself) | `modis` |
| Rain radar | EUMETNET OPERA composite every 5 min (~10 min behind); Météo-France latest image every 5 min | every 5 min | `radar`, `radar-mf` |
| Nowcast (AROME-PI) | a run every hour, complete ~40 min later | every 15 min (only a new complete run is read) | `arome-pi` |
| Station observations | hourly | every 30 min | `mf-obs` |
| Vigilance | 06:00 and 16:00 local, and on change | every 15 min | `vigilance` |
| Météo des forêts | daily, in the fire season | every 3 h | `meteo-forets` |
| AROME maps | a run every 3 h | every 30 min (only a new complete run is read: ~3.5 min per site) | `arome` |
| PE-AROME probabilities | a run every 6 h | every 30 min (only a new complete run is read: ~7 min per site) | `pe-arome` |
| Waves and surge | runs every 6 to 12 h | every 3 h (~2 min per coastal site) | `sea` |
| Static data | admin, RPG, OSM, INSEE, farm plots | every 1 to 90 days, only in "update" mode (frozen by default) | `admin`, `rpg`, … |

Each site follows the mode chosen for each dataset on the **Data** tab (below): a job only updates the
sites where its dataset is in "update" or "live" mode and has what it needs (a key, a login, a coast).
The jobs come from the dataset catalogue (`geomon/datasets.py`): a dataset with a `job` is checked on its
`schedule` ("every 6h", "daily 02:00", "forecast" for a new model run, "scene <collection>" for a new
satellite acquisition), and declares what it `requires`; adding a dataset means one entry there and its
ingestion step.

One scheduler serves every site: each job checks its source once (one look at the forecast models' run
times, one daily slot...) and, when there is something new, updates every site in turn; only the
Sentinel-2 search is made per site, for its extent. SIM2's national files are downloaded once and keep
the cells of all the sites. Each job has its own thread and lock, so a job waiting for a download (the
Open-Meteo daily quota, the CDS queue) does not hold up the others.

A site whose update failed is retried alone after 30 minutes; a site added to `sites.json` is brought up
to date at once; a job missed while the computer was off runs at the next start.
`docker compose exec scheduler python -m geomon.scheduler --status` shows the last runs; `docker compose logs -f
scheduler` follows them. Steps can still be run by hand (`ingest forecast`, `ingest weather era5land era5 sim2 agera5`): a
database lock makes a manual run wait for a scheduled one, so they never write the same data at once.

AgERA5, ERA5-Land and ERA5 need a free Copernicus account: put the personal access token
(https://cds.climate.copernicus.eu/profile) in `GEOMON_CDS_API_KEY` in `.env`, and accept the
licence once on each dataset's Download tab. Without a token the steps are skipped. For AgERA5 the CDS queues
requests: the first run sends a single request (all variables, the whole history since 1979, AOI box), which can wait
from minutes to an hour in the queue. Later runs only ask for the days published since (plus the last 10
again) and append them to the store; nothing is requested when no new day has been published.

ERA5-Land (0.1°) and ERA5 (0.25°) come from the CDS time-series editions, which serve an area and a
period in one request: the first run asks for one year per request (from `GEOMON_WEATHER_START`, or
`GEOMON_ERA5_START` for ERA5: `1990-01-01` gives ERA5 1991–2020 normals), kept in `data/raw/era5land` and
`data/raw/era5` so that an interrupted run resumes; later runs ask for the days published since, plus the
last 3 again. Only the cells touching the site's communes are kept. Both are first published as preliminary
data (ERA5T), replaced by the final release 2–3 months later: once a week the last 92 days are fetched
again (`GEOMON_ERA5_REVISION_DAYS`, `GEOMON_ERA5_REVISION_EVERY_DAYS`). Variables carry the names and units
of the Open-Meteo products; relative humidity, wind speed and ET0 (FAO-56 hourly Penman-Monteith, with the
reanalysis' own long-wave radiation) are computed, and ERA5's actual evapotranspiration comes from its latent
heat flux. ERA5-Land re-runs ERA5's land model at 0.1°: it is the one for soil and snow; ERA5 adds the
atmosphere (cloud cover, boundary layer, gusts, heat fluxes) and a record back to 1940.

`GEOMON_ERA5_SOURCE=openmeteo` brings back the former source: ERA5-Land completed with ERA5 in one product
(`om_era5land`), fetched by the `weather` step within Open-Meteo's daily quota (then no separate ERA5).

The Météo-France archive at Open-Meteo is slow (1–3 min per month for all points): the first run takes
about 2 hours. Fetched months are kept in `data/raw/openmeteo`, so an interrupted run resumes. Later runs
only ask for the days since the last run, plus the last 3 again (`GEOMON_WEATHER_OVERLAP_DAYS`, as the
archives fill in late). The store keeps what is already assembled: only the new days are parsed and
appended, and nothing is written when nothing changed.

Open-Meteo's free quota (10,000 calls a day) counts each grid point of a request as one call, weighted by
the number of variables and days. Only the points whose cell touches one of the site's communes are
requested (34 for Dragey-Ronthon rather than 224 over its box, 11 for Montseveroux). When the quota is
reached, the step stops at once (completed months are kept) and the scheduler tries again every 30
minutes; the quota is restored the next day.

The first SIM2 run streams the national files from 1990 (about 4.4 GB, 40 s per year, once); only
the sites' cells are kept (a few MB). Past years are checked against the server once a month; the
store is rebuilt only when one of its files changed (usually the daily "latest" file).

### Datasets: what is kept, what is updated

The page's **Data** tab lists every dataset of the selected site by category (context and static data,
terrain and soils, statistics, weather, satellite), with its stored size, its last update and what it
still needs (a key, a login). For each one, choose:

- **Update regularly**: the archive is kept and the scheduler updates it (the default of the datasets it
  followed before; the static ones are then refreshed every 1 to 90 days);
- **Frozen: keep, no updates**: what is stored stays, nothing more is downloaded (the default of the static
  datasets). **Update now** still runs one update, e.g. to download an archive once without following it;
- **Live: recent hours only**: only the last hours are kept (24 by default, 1 hour to 31 days), updated
  regularly; offered by the rain radar.

**Delete data** removes a dataset's stores and rows for the site (asked to confirm, then set to frozen so
that it does not come back). Data shared by the sites (administrative units, RPG, INSEE) is not deleted
per site. The modes are kept in `meta.dataset_policy`; the actions in `meta.dataset_request` are run by
the scheduler, one at a time, usually within a minute. Sizes are measured in the background (walking
the satellite stores takes minutes on a Windows bind mount). `docker compose run --rm ingest <step>` still
runs any step by hand, whatever the mode.

### Rain radar

Where it rains and has been raining, from two sources (two datasets on the Data tab, live mode by default,
the last 24 hours kept), plus a worldwide fallback:

- **Europe, EUMETNET OPERA** (default, no account): the pan-European composite of all the national radars,
  Météo-France's included (CIRRUS maximum reflectivity, 1 km, every 5 minutes, about 10 minutes behind),
  CC BY 4.0. The composites are Cloud-Optimised GeoTIFFs in a 24-hour rolling cache (S3, anonymous,
  `GEOMON_OPERA_BUCKETS`): only the blocks holding the site and 100 km around are read (0.4 s per image),
  and the first run fills the last 24 hours at once (287 images in 12 s; 3 to 5 MB stored per site).
  Rain comes from the reflectivity by Marshall-Palmer (Z = 200 R^1.6, capped at 56 dBZ), as OPERA's own
  rain products: over Dragey on 20 November 2026 the hourly totals were 3.1, 1.9 and 2.2 mm against 3.1,
  2.2 and 1.4 mm for OPERA's 1-hour accumulation.
- **France, Météo-France** (500 m, every 5 minutes; needs a free key): the API Ciblée Radar only serves the
  latest image of all of France (about 2 MB), so following it costs about 575 MB a day: the dataset is
  **frozen by default** (OPERA already holds Météo-France's radars, at 1 km). Set it to live on the Data tab to
  poll it every 5 minutes (the images are 5-minute images: polling less often would leave holes in the
  animation); the site and 50 km around are kept. On https://portail-api.meteofrance.fr create an application, subscribe
  it to the radar API, and put its API key in `.env` as `GEOMON_METEOFRANCE_API_KEY` (or its application
  ID as `GEOMON_METEOFRANCE_APPLICATION_ID`), then restart the scheduler.
- **World, RainViewer** (map only, nothing stored): free tiles, the last 2 hours every 10 minutes, but only
  up to zoom 7 (about 1 km): enough to see rain bands, coarse at the plot scale.

Windy and meteoblue offer radar maps only through paid APIs whose terms forbid storing them; MeteoSwiss's
open radar data covers Switzerland only.

On the **Layers** tab, *Rain radar* animates the last 1 to 24 hours (play, pause, a slider per image, the
usual intensity scale in mm/h, local times). The Weather tab's **Rain radar** view gives, for the selected
area and source, the rain of each 5-minute image, the totals of the last 1, 3, 6, 12 and 24 hours and the
accumulation map. Accumulations count missing images as dry, as Météo-France advises, and report the
coverage. Stored in `data/zarr/sites/<site>/radar_<opera|mf>.zarr` (rain in 1/100 mm per 5 minutes).
The archive since 1997 is Météo-France's COMEPHORE reanalysis (radar and rain gauges, 1 km), kept as daily
sums and compared with the other sources on the Weather tab.

**Nowcast, the next 6 hours** (dataset "Nowcast" on the Data tab, step `arome-pi`): Météo-France's AROME-PI
(prévision immédiate), run every hour at 1.3 km and served on a 0.01° grid. It needs the Météo-France key, its
application subscribed to the "AROME-PI" API on the portal. The scheduler checks every 15 minutes and reads
the newest *complete* run (a run is published progressively over about 40 minutes), over the site and 50 km
around: rain and snow (water equivalent) every 15 minutes, temperature at 2 m every hour, and the maximum
gust of each hour. That is 37 requests, about 1.5 minutes per site under the API's 50 requests a minute;
each run replaces the previous one (`data/zarr/sites/<site>/nowcast_aromepi.zarr`). The Layers tab's
animation continues after the latest radar image with these forecast frames, on the same intensity scale
and marked "forecast" (untick *Then the next 6 hours* to leave them out). The Weather tab's Rain radar view
adds, for the area, the rain of each 15 minutes and the temperature as a chart, and an hourly table with
the maximum gusts.

### Météo-France APIs: now, the next hours and the coast

With a Météo-France account (free), geomon follows several of Météo-France's APIs; subscribe the
application to each of them on https://portail-api.meteofrance.fr. **Authenticate with the application's
OAuth2 credentials** (`GEOMON_METEOFRANCE_APPLICATION_ID`, base64 of `consumer_key:consumer_secret`, the
value after `Authorization: Basic` in the portal's "generate a token" example): geomon fetches the hourly
token and renews it. An API key works too, but it is a JWT listing every subscribed API, fixed when it is
generated (subscribe first, then generate it): with about 20 APIs it exceeds 7 KB, and the model servers
(AROME, AROME-PI, PE-AROME, waves) then drop every request, while observations and Vigilance still answer.
An access token pasted instead of the credentials is refused with a message saying what to give. The model
services (WCS) are read as GRIB2: the same values as their GeoTIFF (uncompressed float64) in 6 to 24 % of the
bytes. The Vigilance texts are fetched only when the map's update time changes. The
gateway also drops kept-alive connections: each request opens its own (with geomon's usual retries).

- **Warnings banner** (under the site, every tab): the Vigilance of the site's départements for today and
  tomorrow, coloured, with the phenomena above green (the coastal domain, e.g. 5010 for the Manche's
  waves-submersion, is included) and the bulletins' texts; and **Météo des forêts**, the forest fire danger
  for tomorrow and the day after. It is published only during the fire season: out of season the API
  answers 404 and the banner says so (its JSON layout is undocumented: levels are found by field names).
- **Weather tab, "Observed, last days"**: the hourly observations of the stations within 40 km (each update
  asks every station for its latest hour, about 0.5 KB; the département's package, five days of all its
  stations in 1.2 MB, only to start, after a gap of more than 3 hours and daily to choose the stations again): temperature and dew point
  with the rain of each hour, wind and gusts, humidity, and a table of the latest values, rain over
  24 hours and the 24-hour extremes. Live mode keeps 120 hours; update mode keeps them all.
- **"Forecast maps, AROME 1.3 km"**: rain rate, temperature at 2 m or gusts for 51 hours, hourly for the first
  day then every 3 hours (the rain then as the mean rate of the 3 hours), over the site and 30 km around, as an
  animation (each variable on a fixed colour scale over the run).
- **"Probabilities of rain and frost (PE-AROME)"**: read once per run for nearby sites (each member over a box
  covering the sites whose own boxes take at least half of it, the answers kept a day in
  `data/raw/meteofrance_wcs`, then cut per site: the second site takes seconds; sites far apart read their own
  boxes, which costs requests but almost no bytes), from the 25 members of the latest run, the chance of
  at least 1, 5, 10 and 20 mm in each of the next two 24-hour windows, and of a minimum at or below 0,
  -2 and -5 °C on the next mornings (hourly minima between 03:00 and 08:00 local), for the site's
  mean and for its wettest or coldest 2.5 km cell, with the members' spread.
- **"Waves and storm surge"** (coastal sites: a site with no sea cell within 30 km is recorded as inland and the
  dataset then needs "a site on the coast"; the view is removed there): significant wave height,
  peak period and direction at the nearest MFWAM sea cells, every 3 hours, and the storm surge
  (HYCOM2D), hourly, about two days ahead. The surge is the sea level above the predicted tide: add it to
  the tide (not provided).

PIAF (the 5-minute nowcast) is listed by the portal under `/pro/`: its gateway closes the connection,
it is not used.

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
  `GEOMON_S2_MAX_CLOUD` (80 %) are not requested. Each request is made in two passes: the scene classification at
  20 m first (about 1/12 of the cost), then the bands only for the dates with at least `GEOMON_S2_MIN_CLEAR` (20 %)
  of the site clear and not stored yet. Below 20 %, dates cost as much and gave 2.4 % of the parcel values used by
  the curves (measured on 1 600 dates); `ingest sentinel2-reencode` removes such dates already stored, with their
  statistics. Images are stored as one shard per date and band of 512 x 512 chunks (byte-shuffled zstd).
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

In the **one parcel** view, the crop history goes back to 2007: before 2015 it gives the majority crop
group of the farm îlot at the point (and its share of the îlot from 2010).

The parcel series are updated by every `sentinel2` run. After loading RPG years (`ingest rpg`, every
year since `GEOMON_RPG_FIRST_YEAR=2015`), recompute them without downloading anything (about 2 minutes):

```bash
docker compose run --rm ingest sentinel2-parcels
```

### Sentinel-1

Sentinel-1 GRD radar backscatter (IW, VV and VH, 10 m) from `GEOMON_S1_START` (2023-01-01), through the
same openEO account as Sentinel-2. Radar sees through clouds: every acquisition is kept (60 a year per
relative orbit with Sentinel-1A alone, about twice as many since Sentinel-1C in 2025).

```bash
docker compose run --rm ingest sentinel1          # history (one batch job per year, part and orbit direction), then updates
docker compose run --rm ingest sentinel1-stats    # recompute the statistics from the stored images
```

- **Backscatter:** sigma0 on the ellipsoid, the only coefficient the Copernicus Data Space openEO back-end
  offers (terrain-flattened gamma0 is refused). On slopes the values depend on the viewing geometry:
  compare an acquisition with others of the same relative orbit (the web page draws one line per orbit).
- **Storage:** `data/zarr/sentinel1/<site>/[<part>/]<asc|desc>/<year>.zarr`, VV and VH in dB × 100 (int16),
  on the 10 m grid of the Sentinel-2 stores, with the relative orbit of each date. Ascending (evening)
  and descending (morning) passes are kept apart: the back-end delivers one image per day. About 1 MB
  per date at 10 m for a few communes. From 2023 to November 2026 the two sites took 2.1 GB and about
  135 openEO credits (4 to 9 credits per year, part and direction).
- **Statistics:** VV, VH, the VH/VV ratio and the radar vegetation index RVI = 4 VH / (VV + VH) per date
  and area (`eo.index_stats`, `vv_asc`, `rvi_desc`...), means taken in linear power.
- **Polarisation:** only VV + VH products are requested. A few acquisitions are HH + HV (Sentinel-1C in
  April 2025 over the Manche, during its commissioning); without a VV band they made a whole year's job fail.
- **Coherence** needs the phase of SLC products, which GRD does not keep. The catalogue has
  `sentinel-1-slc` and `sentinel-1-slc-burst`; openEO has neither them nor a coherence process: it would
  be a separate, local processing chain.
- The scheduler checks the STAC catalogue every 3 hours and updates a site when a new acquisition covers it.

### MODIS NDVI

The 250 m NDVI of MODIS (collection 6.1): the 16-day composites of Terra (MOD13Q1, since 18 February
2000) and Aqua (MYD13Q1, since July 2002), 8 days apart, from `GEOMON_MODIS_START` (2000-02-18). They come
from the Microsoft Planetary Computer, without an account. A coarser series than Sentinel-2, but with
fifteen more years and a composite every 8 days whatever the clouds.

```bash
docker compose run --rm ingest modis          # whole record the first time (a few minutes), then the new composites
docker compose run --rm ingest modis-stats    # recompute the statistics from the stored composites
docker compose run --rm ingest modis-crops    # recompute the crop-group curves (after an RPG reload or a GEOMON_MODIS_PURITY change)
```

- **Small transfers:** no openEO back-end serves MODIS. The files are Cloud-Optimised GeoTIFFs, read by
  HTTP range requests: their header, then only the internal block of 512 × 512 cells (about 120 km) that
  holds the site, never the 4800 × 4800 tile (37 MB for NDVI). Only NDVI and pixel reliability are read,
  about 0.5 MB per composite for a site within one block. The STAC search asks for the few fields
  it uses (27 KB per year instead of 460 KB). The record since 2000 takes about 0.6 GB of transfer per
  block, a few minutes (8 files read at once); afterwards about 1 MB a week. The shared access tokens
  of the Planetary Computer (valid one hour) are renewed as they expire.
- **Storage:** `data/zarr/modis/<site>/<year>.zarr` on the MODIS sinusoidal grid (231.66 m, snapped to the
  global grid, tiles merged when a site straddles two), cut to the site's rectangle, cells outside its
  outline (plus 250 m) empty. `ndvi` (int16, × 0.0001), `reliability` (int8: 0 good, 1 marginal, 2 snow
  or ice, 3 cloudy), and the platform of each composite. A composite is dated by its first day (the
  date in its name). A few hundred kB per site and year.
- **Statistics:** NDVI per composite and area over the reliable cells (good or marginal), in
  `eo.index_stats` as `modis_ndvi`. A cell is 5.4 ha: an area too small to hold a cell centre (a small
  farm) takes the cell under it.
- **Publication:** composites appear one to four weeks after their 16 days; a year is searched again
  until 45 days after its end. A composite that failed to download is fetched at the next run. Over the
  last three years, composites missing from the catalogue are searched again at each run (one small
  query): in November 2026 the Planetary Computer had none from July to mid-November 2025, and a few
  isolated ones are missing (18 February 2023, 12 August 2024...).
- **Crop groups:** NDVI curves per RPG crop group and season, against the 3-year, 5-year and long-term
  means, as for Sentinel-2. A cell (5.4 ha) usually holds several parcels: a group is followed on its
  "pure" cells, covered at least `GEOMON_MODIS_PURITY` (75 %) by the group (measured on 29 m sub-cells;
  neighbouring parcels of the same group count together). Each pure cell gets a daily curve per season
  from its reliable composites (placed at the middle of their 16 days); the group curve is their mean,
  weighted by the group's share of each cell or not. Stored in `eo.group_season` (index `modis_ndvi`,
  `parcels` = cells). Seasons use their own RPG: parcels since 2015, farm îlots from 2007 to 2014
  (status `ilots`); after the latest RPG and before 2007, only permanent crops (permanent grassland,
  moors, orchards, vines) are assumed, which gives permanent grassland a long-term reference since 2000,
  and the other groups one since 2007. An îlot counts for its majority group by the share of its area
  that group covers (given from 2010; for 2007–2009, the share of the 2010 îlot of the same group that
  overlaps it most, else the site's overall share, 78 % at Dragey): mixed îlots make no pure cells. Small
  areas have few or no pure cells: around Dragey, about 100 pure cells of permanent grassland, 50 of
  maize and 10 to 20 of wheat over the whole group with parcels (25 to 60 with the coarser îlots), but
  only one or two per group on the farm's plots. Recomputing all seasons takes about 2 minutes for
  Dragey. Run `ingest modis-crops` after loading RPG years.

### Soils

Besides SoilGrids (world, 250 m), French sources (all open, without account):

- **INRAE / Gis Sol, soils of France at 90 m** (`ingest soil-inrae`): the national digital soil maps of the
  Réservoir utile (available water capacity per horizon and over 0-200 cm with its standard deviation,
  water content at field capacity and at wilting point, from pedotransfer functions fitted on the French
  measurements of SOLHYDRO) and of the texture (clay, silt, sand, coarse fragments). The national GeoTIFFs
  (640 MB each, in one-row strips) are read by HTTP range requests on the repository's signed links: only
  the site's rows, about 20 MB and 2 s per layer, 44 layers in about a minute for Dragey (instead of 28 GB).
  Stored with the variable names of the SoilGrids store, so that every tool works with both. SOLHYDRO
  itself (the measurements) is not published: its use is through these maps.
- **Maps** (`ingest soil-maps`, in `ref.soil_feature`): the dominant soils of the harmonised Référentiels
  Régionaux Pédologiques at 1:250 000 (Gis Sol, IGN WFS `INRAE_CARTE-SOLS`: soil units, their description,
  dominant soil and its share); the BRGM harmonised geological map at 1:50 000 (BD Charm-50, read from the
  département's zip archive on InfoTerre by range requests: 3 s for Dragey instead of 22 MB); the BRGM clay
  shrink-swell exposure (`ALEARG_REALISE` WFS, whose `id` is the exposure level, as Géorisques's API gives
  it). The geological map image (WMS) cannot be queried at a point: the vector map is used.
- **Groundwater** (`ingest groundwater`, daily by the scheduler): Hub'Eau's daily levels of the active
  piezometers within 25 km (up to 6), their whole record the first time, then the new days. The page shows
  the depth of the water against the 10th-90th percentile and median of the same weeks of the earlier
  years, and where the latest level stands among them. Recording errors (a day at 0 m, spikes) are left
  out. At Montseveroux 6 piezometers within 8 to 14 km, recorded since 1977 to 2011; at Dragey the nearest
  4 are 12 to 17 km away, in other aquifers than the site's dunes and alluvium.

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

#### A whole administrative unit (a département: NUTS-3)

Instead of `communes`, a site can take every commune of an administrative unit: `"unit": "DEP:50"` (a
département), `EPCI:<SIREN code>`, `ARR:<code>` or `REG:<code>` (`GEOMON_UNIT` for a single site). The
communes are listed from IGN ADMIN EXPRESS at each ingestion, and the name defaults to the unit's:

```json
{"code": "manche", "unit": "DEP:50"}
```

What changes with the size of the site (a département is ~5,700 km² and ~360 communes, against tens of km²):

- **Web page:** the Area menu groups the communes by intercommunality; the hierarchy lists the parent units.
- **Sentinel-2:** the statistics of every commune come from one label image per part, not one mask each
  (0.5 GB instead of 20 GB for a département); about 7 s per date for 360 communes. The images themselves
  grow with the area: plan about 150 GB and the openEO credits for 11 years of a département.
- **Crop groups:** their curves per area and season are computed at ingestion (`eo.group_season`, from
  `eo.parcel_info`: each parcel's crop and areas); the web page reads a few rows.
- **Weather:** daily values are computed once per update into a daily store beside each hourly archive
  (`<product>_daily.zarr`); the web app reads it, and the hourly store only around the forecast start.
  The Météo-France grid doubles its step until the site has at most `GEOMON_WEATHER_MAX_POINTS` (150)
  points: a département gets about 0.1° (each point samples the AROME cell under it), which keeps a
  forecast near 800 of Open-Meteo's 10,000 daily calls. Requests carry at most
  `GEOMON_OPENMETEO_BATCH_POINTS` (100) points. The quota is shared by all the sites of a server.
- **Elevation:** the 5 m grid is downloaded and written in strips (a département has over a billion cells),
  stored as scaled integers (centimetres, hundredths of a degree),
  with overviews at 20, 80 and 320 m (`dem_ov4`, `dem_ov16`, `dem_ov64`). The map reads only the cells of
  each tile, from the overview matching the zoom; the land summary of a large area uses an overview.
- **Not changed yet:** INSEE statistics are fetched commune by commune (~2,000 requests for a département);
  OpenStreetMap for a département is beyond what Overpass accepts in one query: use
  `GEOMON_OSM_SOURCE=geofabrik`.

### The farm's plots and land cover

Two settings of a site in `sites.json` (`GEOMON_FARM_FILE` for a single site without `sites.json`):

- `farm_file`: the farm's plots, e.g. a PAC declaration export (`CAMPAGNE`, `NUMERO`, `ILOT_REF`,
  `COMMUNE`, `SURF_TOT` are kept when present). Their union becomes an analysis area, "My plots", next to
  the communes and the group: land summary, weather, satellite series, crop groups. Without it, the plots
  of the latest land-cover year make the area.
- `farm_landcover`: the farm's land cover, **one file per year**, its plots or fields with their crop:

  ```json
  "farm_landcover": {
    "2025": "farm/dragey-ronthon-6/landcover_2025.gpkg",
    "2026": {"path": "farm/dragey-ronthon-6/fields_2026.shp", "crs": "EPSG:27572", "crop_field": "CULT"}
  }
  ```

  A file may serve several years (the same crops). The crop is an RPG crop code (`CODE_CULTU`,
  `CODE_CULTURE`, `CULTURE`... or `crop_field`) and/or an RPG crop group (`CODE_GROUP`... or
  `group_field`; else found from the crop code). Options: `layer` (default: the first polygon layer),
  `crs` (for a file that does not state its projection, or to override it).

Every file may be a GeoJSON, a shapefile (zipped or not), a GeoPackage, or any other format GDAL reads,
in any projection and datum (Lambert-93, NTF Lambert II, UTM, ED50...): it is reprojected to WGS84 by
GDAL/PROJ (`geomon/vector.py`). Crop codes outside the RPG nomenclature are reported in the log.

The farm's fields have their own Sentinel-2 series: their outlines, shrunk by 10 m like the RPG parcels,
are a parcel layer of their own (`rpg_year` 0 in `eo.parcel_stats`, `parcel_season`, `parcel_info`; one
series per outline, shared by the years whose files repeat it). In each year with a land-cover file, the
farm's fields replace the RPG parcels lying in them (their centre in a field), with the farm's crops
(`eo.farm_crop`, status "your land cover"), in every area: the communes and the group as well as the
farm's own. The farm's own area ("My plots") only has crop-group curves (Sentinel-2 and MODIS) for those
years, from its fields: in the other years the RPG says nothing reliable about the farm's plots. The
parcel view shows the field at the clicked point (its own outline, crop and curve; the RPG's crop when
different) in those years, and the RPG parcel before. Fields with fewer than 4 interior pixels (about
0.1 ha once shrunk) have no series.

```bash
docker compose run --rm ingest farm      # after changing a file; when the land cover changed, recomputes the
                                         # fields' series from the stored images and the crop curves (about 4 min)
```

At Dragey the farm's 2025 crops (a GeoPackage in Lambert-93, made from the RPG 2025 parcels inside the
plots, as the farmer would declare them) serve 2023 to 2027: the farm has not changed its land cover
since 2023. Its 52 fields have series from July 2022 (323 images); the 1.06 ha lucerne field, which the
RPG drew inside larger parcels in 2023 and 2024, now has its own curve in those years.

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
  parcels are coloured by crop group, and the legend gives crop areas inside the selected area, for any
  RPG year: parcels since 2015, farm îlots by majority crop group from 2007 to 2014. The rain radar,
  animated over the last hours (see Rain radar). Terrain overlays: elevation, hillshade, slope, aspect.
- **Soils:** soil properties by depth from INRAE / Gis Sol (France, 90 m: texture, coarse fragments, water at
  field capacity and wilting point, available water capacity, from French pedotransfer functions) or
  SoilGrids 2.0 (world, 250 m: also organic carbon, bulk density, pH, CEC, nitrogen; water retention by
  Saxton & Rawls), on the map and as the profile of the selected area; the maps of the dominant soils, of
  the geology at 1:50 000 and of the clay shrink-swell exposure, with their legends; on a click, what lies
  at the point (soil unit and its description, geological formation, clay exposure) and its soil profile;
  the groundwater of the nearest piezometers against its usual level for the season (see Soils). Raster
  overlays: elevation, hillshade, slope, aspect, and every soil property by depth, with a legend in
  physical units.
- **Statistics:** a land summary of the selected area (surface, declared crops, elevation, slope,
  available water capacity, topsoil texture, clay, sand, organic carbon and pH), then the administrative
  hierarchy, the municipal population series, and key indicators (totals, plus breakdowns by age, sex,
  dwelling type, …) for the group, each commune or any parent unit. A dataset explorer shows the full
  tables with labelled dimensions.
- **Weather:** daily, weekly or monthly series of any variable, averaged over the selected area (or at
  a point clicked on the map), comparing
  Météo-France, ERA5-Land, ERA5, AgERA5, SIM2, the **measured** values of the nearest Météo-France stations
  (each variable, each day, from the nearest station measuring it; the stations used are named in the legend,
  with their distances as a tooltip), **COMEPHORE** rain (1 km, radar and rain gauges) and the Météo-France
  **ETP grid**: the AgERA5 set (temperature min/mean/max, precipitation, radiation,
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
- **Vegetation:** Sentinel-2 (10 m, since 2015) or MODIS (250 m, since 2000), chosen at the top of the tab.
  Sentinel-2: time series of an index (NDVI, and NDRE, NDWI or NDMI when their bands are stored)
  averaged over the selected area, clear pixels only, with the spread of its pixels; filter by year and
  by the share of the area that must be clear. Click a date to show the index image on the map, with or
  without the cloud mask. Two other views: **crop group** (mean curve of the group's parcels in the
  area for a season, the middle half of the parcels, and the 3-year, 5-year and long-term means with the
  long-term range; a table compares the latest value, the peak and the mean since 1 January) and **one
  parcel** (click it on the map: its curve and clear dates for a season against its crop group and its own
  earlier seasons with the same crop, and its crop history since 2015). MODIS: the NDVI over the area and
  the crop-group curves with their references (no parcel view: a cell is larger than most parcels).
- **Radar:** Sentinel-1 VV, VH, VH/VV and RVI backscatter over the area, per orbit direction, one line per
  relative orbit; click a date for its image.
- **Data:** how each dataset is followed for the site (update, frozen, live), its size and last update,
  "update now" and "delete data" (see Datasets above).
- **Point:** click anywhere to get elevation, slope, aspect, the RPG parcel (crop, area, organic
  status) and the full soil profile with estimated available water capacity.

## API

| Endpoint | Content |
|---|---|
| `GET /api/aoi` | Area of interest: its communes, the group, the analysis areas, the parent units |
| `GET /api/area.geojson?area=`, `GET /api/area/summary?area=` | Outline of an area; its land summary (terrain, soil, crops) |
| `GET /api/admin.geojson?levels=COM,EPCI` | Administrative units |
| `GET /api/rpg.geojson`, `GET /api/rpg/summary?area=&year=` | RPG parcels, crop areas in an area (before 2015: îlot areas by majority crop group) |
| `GET /api/rpg/years` | RPG years loaded: parcels, and îlots (2007–2014) |
| `GET /api/farm.geojson` | The farm's plots, with the latest RPG crop of each |
| `GET /api/osm/{roads,buildings,landuse,water,waterways,railways,pois}.geojson` | OSM layers |
| `GET /api/tiles/{rpg,roads,buildings,landuse,water,waterways,railways,pois}/{z}/{x}/{y}.pbf` | Vector tiles of the large layers (what the map uses) |
| `GET /api/rasters` | Raster catalogue: variables, units, depths |
| `GET /api/raster/{dem,soil}/{var}/info?depth=i` | Legend, extent and tile URL of an overlay |
| `GET /api/raster/{dem,soil}/{var}/tiles/{z}/{x}/{y}.png?depth=i` | Overlay tiles (what the map uses); `…/{var}.png`: the whole overlay as one image |
| `GET /api/point?lon=&lat=` | Every layer at a point |
| `GET /api/stats/datasets`, `summary`, `series`, `table` (`level=GRP&code=<site>` for the group) | Statistics |
| `GET /api/weather/products` | Weather products, periods, variables and units |
| `GET /api/weather/daily?area=&var=t_max&series=mf,era5land,era5,agera5,sim2,stations,comephore,mf_etp&step=day` | Daily, weekly or monthly series over an area (or `lon=&lat=` for a point); `stations` names the stations used |
| `GET /api/weather/normals?area=&var=precip&series=sim2&start=2026-01-01` | One year against the 1991–2020 normal of the same source (`sim2` or `agera5`), continued by the Météo-France archive and forecast (`forecast=false` to leave them out) |
| `GET /api/weather/forecast?area=` | Daily summary of the latest forecast over an area (or at a point) |
| `GET /api/weather/cells.geojson?product=sim2` | Grid cells of a product |
| `GET /api/nowcast/info`, `/api/nowcast/frames?after=`, `/api/nowcast/frame.png?time=` | AROME-PI nowcast: run, rain frames (mm/h, the radar scale) for the animation |
| `GET /api/mf/vigilance` | Vigilance and Météo des forêts of the site's départements |
| `GET /api/mf/obs?hours=48` | Hourly observations of the nearest stations, latest values and 24-hour summaries |
| `GET /api/mf/arome/info`, `/api/mf/arome/frames?var=rain`, `/api/mf/arome/frame.png?var=&time=` | AROME maps (`rain`, `t2m`, `gust`) |
| `GET /api/mf/ensemble` | PE-AROME probabilities of rain and frost |
| `GET /api/mf/sea` | Waves and storm surge (coastal sites) |
| `GET /api/nowcast/series?area=` | Next 6 hours over an area: rain per 15 minutes (mean, wettest cell, wet share), hourly temperature and maximum gust (km/h) |
| `GET /api/s2/info` | Sentinel-2 stores: dates, bands, indices available |
| `GET /api/s2/series?index=ndvi&area=&min_clear=0.5` | Index statistics per date over an area |
| `GET /api/s2/{index}.png?date=&masked=true`, `…/info` | Index image of a date and its legend |
| `GET /api/s2/crops?index=ndvi&area=&season=` | Seasons with parcel curves, crop groups of an area in a season |
| `GET /api/s2/crop?group=1&season=2025&index=ndvi&area=` | Crop group curve, observations, 3-year / 5-year / long-term references |
| `GET /api/s2/parcel?lon=&lat=&season=&index=ndvi` | Parcel at a point: crop history, curve and observations of a season |
| `GET /api/s1/info` | Sentinel-1 stores: dates and relative orbits per direction, measures |
| `GET /api/s1/series?measure=vv&direction=asc&area=` | Backscatter statistics per acquisition over an area, with its relative orbit |
| `GET /api/s1/{vv,vh,vh_vv,rvi}.png?date=&direction=`, `…/info` | Image of a date and its legend |
| `GET /api/modis/info` | MODIS NDVI stores: years, composites per platform |
| `GET /api/modis/series?area=&min_clear=0.9` | NDVI statistics per composite over an area (reliable cells), with its platform |
| `GET /api/modis/ndvi.png?date=&masked=true`, `…/info` | NDVI image of a composite and its legend |
| `GET /api/modis/crops?area=&season=` | Seasons with crop-group curves, crop groups of an area in a season (pure cells) |
| `GET /api/modis/crop?group=18&season=2026&area=&weight=pixels` | Crop group curve from MODIS, composites, 3-year / 5-year / long-term references |
| `GET /api/soil/maps`, `GET /api/soil/at?lon=&lat=`, `GET /api/tiles/{soils,geology,clay}/{z}/{x}/{y}.pbf` | Soil and geology maps: classes in the site, what lies at a point, vector tiles |
| `GET /api/groundwater/stations`, `GET /api/groundwater/series?code_bss=&years=2` | Piezometers near the site; daily depth of the water and its usual level for the season |
| `GET /api/units/convert?value=15&from=degC&to=K` | Unit conversion (Specifications §3.5) |
| `GET /api/status` | Last ingestion result of each step |
| `GET /api/datasets`, `PUT /api/datasets/{code}` (`{"mode": "frozen"}`), `POST /api/datasets/{code}/update`, `POST /api/datasets/{code}/delete?confirm={code}` | Dataset modes, sizes, last updates; actions run by the scheduler |
| `GET /api/radar/info`, `GET /api/radar/frames?source=opera&hours=3`, `GET /api/radar/frame.png?source=&time=` | Radar sources and stored images; animation frames (rain intensity) |
| `GET /api/radar/series?source=opera&area=`, `GET /api/radar/accum.png?source=&hours=3`, `…/accum/info` | Rain per image over an area and totals; rain of the last hours as a map image |

Interactive documentation: http://localhost:8088/docs. The database is reachable from the host at
`localhost:5433` (user and password `geomon`), e.g. from QGIS.

## Layout

```
docker-compose.yml          db (PostGIS 18), api (web app), ingest (one-shot data loading)
docker/geomon/Dockerfile    Debian testing + GDAL + Python geospatial stack
db/init/01_schema.sql       PostGIS and hstore extensions (run once, when the cluster is created)
geomon/
  schema.sql                every table (schemas ref, stats, meta, eo, staging)
  migrate.py                creates or upgrades the schema when a process starts (versioned)
  config.py                 settings from environment variables
  aoi.py                    area of interest: one commune or a group (outlines, buffered extent)
  units.py                  unit conversion helper (pint)
  zarrio.py                 atomic Zarr writes (the web app never reads a half-written store)
  weather.py                weather products: loading, AgERA5-style daily aggregation
  scheduler.py              keeps the weather and satellite data up to date (compose service "scheduler")
  sentinel2.py              Sentinel-2 stores, cloud mask (SCL), spectral indices, area statistics
  sentinel1.py              Sentinel-1 backscatter stores, measures, area statistics
  modis.py                  MODIS NDVI stores, sinusoidal grid, area statistics
  modis_crops.py            MODIS crop-group curves from pure cells
  datasets.py               dataset catalogue, modes per site (update, frozen, live), sizes, deletion
  radar.py                  Météo-France radar rain stores and accumulations
  parcels.py                index series per RPG parcel, smoothed season curves, crop-group references
  ingest/                   one module per source: admin, rpg, osm, dem, soil, insee, openmeteo, era5 (ERA5-Land and ERA5), sim2, agera5, mf_stations, mf_etp, comephore, sentinel2, sentinel1, modis
  api/                      FastAPI app, vector and raster tiles, point sampling
web/                        static page (MapLibre GL JS, no build step)
tests/                      unit tests (docker compose run --rm test)
```

## Known limitations

- The image is the slim "first product" variant. Rust, CUDA and the ML libraries (Specifications
  §2.4) will come in a heavier image variant.
- SoilGrids publishes no water-retention layers: with it, field capacity, wilting point, Ksat and AWC
  are Saxton & Rawls estimates (reduced by the coarse fragments), much higher than the INRAE map in
  France (284 against 124 mm at Dragey): prefer the INRAE source there. The depth to bedrock is not
  covered; groundwater only by the piezometers within 25 km, which may sit in another aquifer.
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
