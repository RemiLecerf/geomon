# Geomon — Specifications

## 1. Purpose

Geomon is a containerised geospatial platform for monitoring land. It acquires, stores, analyses and monitors Earth observation data, weather, soil properties, statistics and reference data. Users manage it through a web interface.

**Main goal:** simulate crop and vegetation growth during the season with a custom, modular crop model (section 6). Satellite images, model outputs and field observations constrain the model.

Guiding principles:

- **Free and open data first.**
- **Local, incremental archive.** Store every acquired dataset locally. Update it when a new revision or new data is published. Keep preprocessing to a minimum.
- **Data lives outside the container.** Containers hold code and tools only. Data is stored on the host, or in the cloud (see 3.1).
- **Fetch only the area of interest (AOI).** Never download full scenes or global products when a subset is available.
- **Standard interfaces.** Prefer openEO, OGC services, STAC and other standard geospatial APIs.
- **Explicit units.** Every quantity carries its unit, and values can be converted between units (see 3.5).

---

## 2. Runtime environment

### 2.1 Container

| Item | Requirement |
|---|---|
| Packaging | Docker image(s), orchestrated with Docker Compose |
| Base image | Debian **testing** (preferred) or Debian **stable** |
| GPU | Optional CUDA support (e.g. a separate image variant or build flag) |
| Data | Not stored in the image or the container's writable layer. Mounted from the host or accessed from object storage (see 3.1) |

### 2.2 Languages and toolchains

- **Rust**: latest stable toolchain.
- **Python**: latest release.
- **GNU compilers**: gcc, g++, gfortran.
- **C/C++ libraries** may be used where relevant.

**Language policy:** use Rust instead of Python when there is a clear trade-off in its favour (performance, memory safety, concurrency, deployment). Otherwise Python is acceptable.

### 2.3 Geospatial stack

- GDAL
- PostgreSQL + PostGIS
- Python: Dask, xarray, xvec, geopandas, openEO client, and the other geospatial packages the platform needs (e.g. rasterio, rioxarray, shapely, pyproj, zarr, pystac-client, healpy/xdggs)

### 2.4 Machine learning

**General-purpose libraries**

- scikit-learn
- CatBoost
- Keras
- TensorFlow
- PyTorch

**Image segmentation and computer vision**

- Convolutional neural networks (CNNs)
- U-Net and its variants
- Encoder–decoder models, including the DeepLab family (DeepLabv3, DeepLabv3+)
- Vision transformers (ViT, SegFormer, Swin, etc.)
- Supporting libraries, for example segmentation-models-pytorch, timm and TorchGeo

### 2.5 User interface

- Written in **JavaScript** and accessed from a **web browser**.
- A **GIS-style** interactive map is preferred, so users can explore and query data layers directly.
- A **Jupyter server** is a desirable addition for advanced analysis.
- The interface covers three functions:
  - **Analysis**: exploring and processing data.
  - **Monitoring**: tracking indicators over time and the state of acquisitions.
  - **Management**: data sources, AOIs, jobs, storage and updates.

---

## 3. Data storage

### 3.1 Storage location

| Data | Location |
|---|---|
| Rasters / images (Zarr) | **Host filesystem** (bind mount) by default. **S3-compatible object storage** optional, preferred for images when running in the cloud |
| PostgreSQL / PostGIS data directory | **Host filesystem** (bind mount or host-backed volume) |
| GeoParquet files | Host filesystem, or S3 |
| Configuration, credentials | Host files / environment, never baked into the image |

Requirements:

- Removing or rebuilding a container must not cause data loss.
- Storage paths and S3 endpoints are configurable, so the same code runs locally or in the cloud.

### 3.2 Formats

| Data type | Format | Alternative |
|---|---|---|
| Raster / gridded | **Zarr** | — |
| Vector | **PostGIS** | **GeoParquet**, where it is more efficient (large, mostly read-only or analytical datasets) |
| Statistics | **PostgreSQL** (see 3.4) | — |

### 3.3 Spatial reference and grids

- **HEALPix** grids are an option. Any other suitable gridded reference system or projection can be used instead.
- Users must be able to **change the projection**.

### 3.4 Statistics database

Statistics from Eurostat, INSEE, Comtrade and similar sources are fetched through their APIs and stored in **PostgreSQL**, next to the PostGIS spatial units.

Requirements:

- **Metadata:** keep the source dataset's metadata and dimensions: source, dataset ID, indicator, time, geography, unit, and flags such as provisional or estimated values.
- **Revisions:** track dataset revisions and vintages.
- **Temporal validity of spatial units:** administrative units change over time (commune mergers, NUTS versions, COG vintages). Each spatial unit has a validity period (`valid_from`, `valid_to`).
- **Spatio-temporal joins:** a statistic is joined to the geometry that was valid at the statistic's reference date. A 2015 value is shown on the 2015 boundaries, not today's.
- **Correspondence tables** between versions (e.g. NUTS 2016 → 2021, commune mergers) so that series can be compared across boundary changes.

### 3.5 Units

- Every variable, in every store (Zarr attributes, PostGIS/PostgreSQL columns, GeoParquet metadata), declares its **unit** explicitly, in a standard notation (UCUM or CF conventions).
- A **unit conversion helper** converts a variable from one unit to another in a single call, e.g. K → °C, kg/m²/s → mm/day, ha → km². It should be usable from both Python and Rust (e.g. built on pint in Python).
- Statistics keep the unit and multiplier from the source (e.g. "thousand persons", "EUR million").

### 3.6 Distribution and updates

- Storage is **distributed**.
- Every acquired dataset is stored locally.
- An update pulls only new data and new revisions.

---

## 4. Data sources

### 4.1 Satellite / Earth observation

- **Providers:** Copernicus (Sentinel missions), ESA, EUMETSAT, CNES, NASA, and others.
- **Products:** Copernicus Land products, ESA products and EUMETSAT products.
- **Soil moisture:** collect the available products from Copernicus and other projects.
- Free data takes priority.

**Sentinel-2 (implemented).** L2A surface reflectance (Sen2Cor) from the Copernicus Data Space Ecosystem openEO back-end (`SENTINEL2_L2A`), since the start of the mission (2015-06-27):

- **Only what is needed:** the AOI extent; bands B04 and B08 (NDVI) plus the scene classification SCL (cloud mask) by default; a pixel is clear when SCL is vegetation, not vegetated or water and its red reflectance is below 0.25 (Sen2Cor classifies fog over land as bare soil); scenes with tile cloud cover above a threshold (80 %) are not requested; dates with almost no clear pixel over the AOI are not stored. More bands are added on demand (B05 for NDRE, B03, B11, …) and fetched for every stored date.
- **Few jobs:** the history is fetched in batch jobs of several years (5.1); updates are small synchronous requests, triggered by a search of the STAC catalogue for new scenes (free, no credits).
- **Storage:** Zarr, one store per year, native 10 m UTM grid, integer reflectance (scale 0.0001; the baseline 04.00 offset is removed by the back-end). Indices are computed from the stored bands; per-date statistics over each analysis area (commune, group) are kept in PostgreSQL for fast time series.
- **Per parcel:** statistics per RPG parcel (interior, clear pixels) for every date, with each season's own RPG (every year since 2015); a daily curve per parcel and season (linear interpolation of the clear dates; Whittaker smoothing as an option); crop-group curves (mean of the parcels, weighted by their interior area) and 3-year, 5-year and long-term references from the earlier seasons, to compare the current season with.

### 4.2 Weather

**Main source: Open-Meteo.** For France, the best balance between resolution, a 3–5 year history and forecasts comes from combining several products:

| Product | Open-Meteo API, model | Resolution | Period | Role |
|---|---|---|---|---|
| Météo-France archived forecasts | Historical Forecast API, `meteofrance_seamless` | 1.5–11 km, hourly | from 2022-11-13 | Main history: same model family as the forecast, so no break between past and future |
| Forecast | Forecast API, `meteofrance_seamless` then `ecmwf_ifs` | AROME 1.5–2.5 km to day 2, ARPEGE 11 km to day 4, ECMWF IFS 9 km to day 15 | latest run | Daily model runs (6.5) |
| ERA5-Land, ERA5 | Historical Weather API, `era5_land` then `era5` | 0.1°; ERA5 0.25° | from 1950 / 1940, 5-day delay | Homogeneous reference; soil temperature and moisture (4 layers to 2.55 m) and snow depth, which the Météo-France models lack at Open-Meteo |
| Past forecast runs | Previous Runs / Single Runs API | as the forecast | | Replay and evaluate in-season revisions (6.5) |

What Open-Meteo's Météo-France archive contains, by period (checked November 2026):

| Variables | Before 2024-01-02 | From 2024-01-02 |
|---|---|---|
| Temperature, humidity, wind | AROME HD 1.5 km | AROME HD 1.5 km |
| Precipitation | ARPEGE 11 km | AROME HD 1.5 km |
| Radiation, ET0, pressure, cloud cover, snowfall | ARPEGE 11 km | AROME 2.5 km |

- **Archived forecasts are not a reanalysis.** The archive joins the first hours of successive runs. It carries the model's biases (notably on rain) and its version changes. Validate it against SIM2 and stations, and correct biases (e.g. quantile mapping) before driving the crop model.
- **Name the models in every request.** `meteofrance_seamless` is the one exception: Open-Meteo's merge of Météo-France models only, whose composition is documented above. Where several models are requested, the first one is used where it has values and the next ones fill gaps; the model of each variable is stored with the data.
- **Licence:** the free API is for non-commercial use only, with rate limits (600 calls/min, 5,000/h, 10,000/day; a call is weighted by days and variables). Commercial use needs a subscription (API key) or a **self-hosted** Open-Meteo instance.
- **Point-based queries:** the API returns data for coordinates. For an area, query the centres of a regular grid over the AOI (AROME 2.5 km grid; 0.1° for ERA5-Land), keeping the cells that touch one of the communes. All points go in one request, one month at a time (one year for ERA5-Land). Fetched periods are kept as raw JSON; an update only asks for the days since the last one, plus a few days of overlap, and appends them to the store. ERA5-Land's preliminary data (ERA5T) is replaced by the final release 2–3 months later: the last 92 days are fetched again every week. Each point counts as one call against the quota: forecasts are downloaded at most every 3 hours, and a step that reaches the daily limit stops at once and is retried later.
- **Downscaling:** Open-Meteo corrects temperature to the elevation of the requested point (90 m DEM); the elevation used is stored with each point.

**AgERA5 variables are essential.** AgERA5 (Copernicus CDS, daily, 0.1°) is derived from ERA5. All its variables are reproduced from Open-Meteo hourly data: daily mean, min, max, day-time and night-time temperature; dew point; precipitation; rain and solid precipitation duration fractions; solar radiation; vapour pressure; 10 m wind; relative humidity at 06, 09, 12, 15 and 18 h; cloud cover; snow thickness. The **original AgERA5** is downloaded from the CDS for the AOI box as a reference, to check the derived variables, and as a fallback. It comes from the time-series edition (`sis-agrometeorological-indicators-timeseries`), where one request brings all variables for the whole history since 1979 (`GEOMON_AGERA5_START`); the gridded edition caps a request at 400 fields (one variable, about one year) and the CDS rejects more than a few queued requests per user and dataset. A free Copernicus account (personal access token) is needed and the CC-BY licence must be accepted once. Data is published about 10 days behind. AgERA5 also provides daily minimum and maximum relative humidity and the vapour pressure deficit at maximum temperature.

**Météo-France open data**

- **SIM2 (SAFRAN-ISBA) daily reanalysis**, 8 km, since 1958 (meteo.data.gouv.fr). It is the French reference for validating and bias-correcting the Open-Meteo products and for calibrating the soil water balance: it adds ETP, a soil wetness index, root-zone water content, drainage, runoff and snow water equivalent. No forecast. Some accumulations run from 06 UTC to 06 UTC. The national files (~135 MB per year) are read as a stream; only the AOI cells are kept.
- **Climate normals:** daily weather is compared with the **1991–2020 normal** (WMO reference period), computed from the same source as the observations (SIM2, kept from 1990, or AgERA5, from 1979) so that the bias between products does not show as an anomaly. Averaged variables: mean, min–max, 10th–90th and 25th–75th percentiles of the reference years' values within a 31-day window centred on each day. Summed variables: totals since the start of the period (1 January, 1 September or 1 October), mean, min–max and the same percentiles of the reference years' totals on the same day. The observations are continued by the Météo-France series (archived forecasts over the days a lagging source has not reached yet, then the latest forecast to 15 days); its bias against the source of the normal is not corrected. A reference year is used when 90 % of its days have values, and the normal needs 24 of the 30 years (WMO's 80 % rule).
- **Station observations**, for validation and as local measurements for data assimilation (6.6). Daily and hourly station files are published per département on meteo.data.gouv.fr (no key); the Météo-France API portal serves the same data with a free key (tokens valid one hour, a per-minute request cap).
- **Later:** COMEPHORE / ANTILOPE radar rainfall (1 km, gauge-adjusted), the best rainfall estimate at field scale.

**Other sources:** ECMWF, Copernicus CDS (ERA5-Land, e.g. for snow density), when needed.

- **Archive:** keep a historical weather archive and update it on a regular schedule.
- **Forecasts:** retrieve forecasts on a regular schedule. They feed the daily model runs (6.5). Each source is polled at the rate it publishes: the forecast at each new model run (AROME every 3 h, ARPEGE and ECMWF every 6 h, detected from Open-Meteo's model metadata), the Météo-France archive and ERA5 daily, SIM2 and AgERA5 every 6 h (downloaded only when changed). Every forecast run is archived.
- **Time step:** **hourly** data is stored and used to drive the model. Daily variables (AgERA5 style) are derived from the hourly data.
- **Scope (for now):** surface-level variables only.

**Variables required by the crop model:**

| Variable | Notes |
|---|---|
| Air temperature | Mean, min and max (daily), or hourly values |
| Rainfall / precipitation | |
| Solar radiation | Incoming shortwave radiation |
| Potential / reference evapotranspiration | Open-Meteo provides FAO-56 ET0. Otherwise compute it (FAO-56 Penman–Monteith) |
| Humidity | Relative humidity and/or vapour pressure |
| Wind speed | At 2 m or 10 m (state the height) |
| Snow depth | Open-Meteo |
| Snow density | Probably not in Open-Meteo or AgERA5. Derive it from snow depth and snow water equivalent, or take it from ERA5-Land (CDS) |
| Soil temperature / moisture (optional) | Open-Meteo provides modelled values at several depths. They complement 4.3 |

### 4.3 Soil surface conditions

- **Soil temperature:** any source is welcome, including high-resolution but partial ones covering only the soil surface. For example, land surface temperature from Sentinel-3 SLSTR, MODIS, Landsat thermal bands, or the soil temperature layers in ERA5-Land.
- **Saturated soils:** information on waterlogged or saturated soils and flooded areas. For example, surface soil moisture from Sentinel-1 or SMAP, Copernicus water and wetness products, and flood maps.

### 4.4 Soil properties

- **Primary candidate:** SoilGrids.
- **Other sources:** BRGM, or any other provider of the required properties.
- **Depth:** down to **2 m**, by layers. SoilGrids layers are the default: 0–5, 5–15, 15–30, 30–60, 60–100 and 100–200 cm.
- **Required properties:**
  - Soil texture
  - Available water capacity
  - Hydraulic conductivity
  - Groundwater depth
  - Bedrock depth and type

### 4.5 Statistics

- **Sources:** Eurostat, INSEE, Comtrade, and similar.
- Provide a **catalogue** of the available INSEE and Eurostat datasets.

### 4.6 Reference and vector data

- **OpenStreetMap**: general reference data. For a small area, one Overpass API query covering
  the area's extent and the tags needed (a few MB). For later, larger areas: Geofabrik regional
  extracts (.osm.pbf), clipped locally. For France these follow the pre-2016 regions; Rhône-Alpes
  is 533 MB. The extract can be chosen automatically from Geofabrik's index of polygons.
- **IGN** (France): required, e.g. administrative boundaries (ADMIN EXPRESS), BD TOPO, orthophotos, and the parcel register (RPG) for crops.

---

## 5. Acquisition rules

### 5.1 Satellite imagery through openEO

- Satellite images are acquired through **openEO**.
- Download and store **only the AOI**.
- Use openEO as much as possible, while **keeping credit consumption low**.
- For historical archives, **use as few jobs as possible**, for example by batching long time ranges and several bands into one job.

### 5.2 Sources without subsetting

When a source cannot serve only the needed chunks or AOI:

1. Raise a **warning**.
2. Estimate the data volume to download per **week, month and year**.

### 5.3 Service preference

Use services in this order:

1. openEO
2. OGC services (WMS/WCS/WFS, OGC API – Features/Coverages/Processes)
3. STAC catalogues
4. Other provider-specific APIs

---

## 6. Crop model

### 6.1 Overview

Geomon uses its **own crop model**, not an existing one such as WOFOST or STICS. It is a set of independent, interchangeable components that simulate the soil–plant–atmosphere system for **any kind of vegetation**: field crops (e.g. potato), meadows and grassland, forage crops, and trees.

The model has two main parts:

- **Soil:** water content and water movement down to 2 m (6.3.1).
- **Plant:** above-ground and below-ground development and growth (6.3.2).

Observations of many kinds refine the model continuously: remote sensing, other models' outputs, local measurements and reported events (6.6).

### 6.2 Component architecture

**Principles**

- **Independent components.** Each process (phenology, root growth, soil water, photosynthesis, a specific disease, etc.) is a separate component. It can be developed, tested, calibrated and replaced on its own.
- **Standard interface.** Every component declares the same items in the same way:
  - **Inputs:** driving variables with units (3.5), e.g. temperature or rainfall.
  - **States:** the variables it maintains, e.g. development stage or layer water content.
  - **Outputs:** the variables it exposes to other components.
  - **Parameters:** with default values, units, valid ranges and uncertainty, by plant type or variety.
  - **Spatial and temporal resolution** it runs at (6.4).
  - **Standard methods**, for example `initialise`, `step` (advance one time step), `get_state` / `set_state` (used for restarts and data assimilation), and `calibrate`.
- **Extensibility.** New components can be added later without changing the existing ones, for example a model for one plant disease, a pest, or a management practice such as irrigation or mowing.
- **Coupling.** A scheduler links the components through their declared inputs and outputs. It resolves the order in which they run and passes variables between them, converting units where needed.
- **Plant-generic.** Each plant type (crop, meadow, forage, tree) or variety is described by a **parameter set**, not by separate code.

### 6.3 Processes

#### 6.3.1 Soil

- **Soil water content** down to **2 m**, in several layers. The SoilGrids depth layers are the default (4.4).
- Water balance terms: infiltration, runoff, drainage between layers, capillary rise, root water uptake, soil evaporation.
- **Saturated / waterlogged conditions**, using the saturated soil information from 4.3.
- **Snow cover** (depth, density, melt), from the snow inputs in 4.2.
- **Soil temperature** (at least at the surface) when data is available (4.3).

#### 6.3.2 Plant

- **Phenology:** development stages, from sowing or bud break to maturity or senescence.
- **Roots:** root depth and how roots are distributed across the soil layers.
- **Above-ground development:** leaves (including leaf area), stems and organs (e.g. tubers, grains, fruits).
- **Photosynthesis** and biomass production.
- **Water use:** transpiration, evapotranspiration and **water stress**.
- **Pests and diseases:** pressure from pests and diseases and the damage they cause, as add-on components (6.2).

### 6.4 Spatial resolution

- **Each component runs at the resolution of its inputs.** If weather comes on a 25 km grid and no other input adds spatial variability to a process, that process is computed on the 25 km grid.
- The result is then **transferred to finer units** (soil map units, satellite pixels, parcels) where other inputs or the plant type do vary.
- This avoids repeating identical calculations at fine resolution. Each output keeps a record of the resolution it was actually computed at.

### 6.5 Temporal resolution and runs

- **Time step:** an **hourly** step is preferred. A daily step is the minimum.
- **Daily operation:** the model runs every day, driven by the weather archive and then by the **weather forecast** for the rest of the season.
- **In-season revisions:** a simulation is re-run from the relevant date when new or revised data arrives, e.g. revised weather, a new satellite image, or a field observation.
- **Model versions:** the model itself will be improved over time. Every simulation records the model version, component versions, parameter sets and input data versions, so results are reproducible and comparable.

### 6.6 Data assimilation and recalibration

**Observation sources**

| Source | Examples | Typical use |
|---|---|---|
| Optical remote sensing | Sentinel-2, Landsat: LAI, fAPAR, NDVI, biophysical products | Green biomass, leaf area, phenology |
| Radar | Sentinel-1 backscatter and coherence | Biomass and structure, surface soil moisture, cloud-independent phenology |
| Other models' outputs | Soil moisture products, reanalyses | Soil water states |
| Local measurements | Sensors, field sampling | Any state variable |
| Reported events | Field observations, news (e.g. sowing dates, harvest, frost, hail, disease outbreaks) | Events, dates, qualitative constraints |

Requirements:

- Each observation has a **location, date, variable, unit, source and uncertainty**. Reported events and observations from news are flagged as **qualitative / lower confidence**.
- Observations are only used once they have been **validated** (quality control, outlier detection).
- **State update:** observations correct the model states during the season (data assimilation through `set_state`).
- **Recalibration:** a method refits **model parameters** once enough valid observations are available, per plant type, variety or region. Calibrated parameter sets are versioned (6.5).
- Remote sensing data is used to improve both the **states** and **all parameters** of the model.

### 6.7 First draft scope

The first version of the model covers only:

1. **Phenology**
2. **Root growth**
3. **Above-ground green biomass**

It needs the soil water component, at least in a simple form, to compute water stress. Photosynthesis in detail, organs, pests and diseases come in later versions.

---

## 7. First use case: Montseveroux (Isère, France)

The project starts with one small, concrete AOI to validate the architecture end to end.

### 7.1 Area of interest

| Item | Value |
|---|---|
| Municipality | **Montseveroux** |
| Postal code | **38122** |
| INSEE commune code | *To be confirmed.* Postal and INSEE codes differ, and all joins use the INSEE code |
| Département | Isère (38) |
| Région | Auvergne-Rhône-Alpes |

**Groups of communes.** The AOI can be a group of communes (`GEOMON_COMMUNES`), tested with Montseveroux (38259), Monsteroux-Milieu (38244) and Chalon (38066). Data is downloaded once for the buffered extent of the group. Each commune and the group are analysis areas: crops, terrain and soil are clipped to their outline; weather is averaged over the grid cells, weighted by their overlap with the area; INSEE values of the group are sums of the communes for counts only (rates, means and medians are not summable).

### 7.2 Scope

1. **Docker environment:** a first working version of the image(s) from section 2, with data on the host (3.1) and PostgreSQL/PostGIS running.
2. **First JavaScript interface:** a GIS-style map centred on Montseveroux, displaying the contextual layers below.
3. **Contextual data:**
   - **OpenStreetMap:** base map and features (roads, buildings, land use, water), from Overpass.
   - **IGN:** administrative boundaries, orthophotos and topographic layers.
   - **INSEE:** high-level descriptors of the commune (population, households, housing, employment, area, density, etc.).
4. **Administrative hierarchy:** build the full chain of units containing the commune, with temporal validity (3.4):
   - Commune → EPCI (intercommunalité) → arrondissement → département → région → France
   - Mapped onto the European **LAU → NUTS 3 → NUTS 2 → NUTS 1 → country** hierarchy
5. **Eurostat join:** attach basic Eurostat indicators (population, area, agriculture, land use, etc.) at the **finest scale Eurostat publishes** for each indicator. That is LAU where available, otherwise NUTS 3 (Isère), then higher levels.

### 7.3 Out of scope for this first step

- Satellite acquisition and the crop growth model. These come in later iterations, on the same AOI. (Soil and weather were added in the second iteration: SoilGrids, Open-Meteo, SIM2, AgERA5; then Sentinel-2 L2A through openEO.)

### 7.4 Done when

- The containers start from a clean checkout. All data persists on the host across container rebuilds.
- The web map shows Montseveroux with the OSM and IGN layers.
- Clicking the commune, or any level of its hierarchy, shows the INSEE and Eurostat indicators with their units, reference year and source.
- The hierarchy and the statistics are stored in PostgreSQL/PostGIS with validity periods.

---

## 8. Open questions

- **Base image:** Debian testing gives newer packages but is less stable. Confirm which one is the default.
- **CUDA:** a separate image or a runtime option? Which CUDA version?
- **Object storage:** which S3 provider in the cloud? Use MinIO locally, to test with the same S3 API?
- **Default grid:** HEALPix or a conventional projected CRS (e.g. Lambert-93 / EPSG:2154 for France)? At what resolution?
- **AOI definition:** how are AOIs created and managed (UI drawing, file upload, administrative units)?
- **Update frequency** for each source: weather archive, forecasts, satellite data, statistics.
- **Authentication and credentials** for the providers (Copernicus, CDS, openEO back-ends, IGN, INSEE).
- **JavaScript UI framework** and mapping library (e.g. MapLibre GL JS, OpenLayers).
- **Open-Meteo access:** use the public API (non-commercial), a paid subscription, or self-host it? This depends on how the project will be used commercially and how large the areas get.
- **Météo-France:** AROME/ARPEGE (Open-Meteo) and SIM2 are now the defaults over France. Which bias correction against SIM2 and stations? Should Météo-France's own API (AROME GRIB, PE-AROME ensemble) be used for uncertainty?
- **Assimilation method:** e.g. ensemble Kalman filter, particle filter, or variational methods. Which one for the state updates and for recalibration?
- **Component language:** Rust for the model core (performance, a hourly step over many grid cells), with Python bindings for calibration and analysis?
- **Transferring results to finer scales:** how outputs move from coarse to fine units (6.4): direct assignment, interpolation, or statistical downscaling.
- **Reported events from news:** collected manually, or extracted automatically (e.g. by text mining)?
