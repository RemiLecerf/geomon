"""Runtime configuration, read from environment variables (see docker-compose.yml).

Sites: one server can follow several sites (groups of communes), listed in
``<data dir>/sites.json`` (GEOMON_SITES_FILE)::

    [{"code": "montseveroux-3", "name": "Montseveroux et voisines",
      "communes": ["38259", "38244", "38066"]},
     {"code": "dragey-ronthon-6", "name": "Dragey-Ronthon et voisines",
      "communes": ["50167", "50565"], "farm_file": "farm/dragey/plots.geojson",
      "farm_name": "My plots (PAC 2025)"}]

Without the file, the single site of GEOMON_COMMUNES / GEOMON_SITE_CODE /
GEOMON_SITE_NAME / GEOMON_FARM_FILE is used. Site-dependent settings
(``settings.communes``, ``site_code``, ``farm_file``, ``site_zarr_dir``...) follow
the *current site*, set with ``use_site(code)`` (ingestion and scheduler) or per
request (web API).
"""

from __future__ import annotations

import contextlib
import contextvars
import datetime as dt
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    pg_host: str = field(default_factory=lambda: _env("POSTGRES_HOST", "localhost"))
    pg_port: int = field(default_factory=lambda: int(_env("POSTGRES_PORT", "5432")))
    pg_db: str = field(default_factory=lambda: _env("POSTGRES_DB", "geomon"))
    pg_user: str = field(default_factory=lambda: _env("POSTGRES_USER", "geomon"))
    pg_password: str = field(default_factory=lambda: _env("POSTGRES_PASSWORD", "geomon"))

    data_dir: Path = field(default_factory=lambda: Path(_env("GEOMON_DATA_DIR", "./data")))

    # Area of interest: one or more INSEE commune codes, comma-separated (Montseveroux = 38259).
    # Data is downloaded once for their joint extent; each commune and the group can be analysed.
    communes_env: tuple[str, ...] = field(default_factory=lambda: tuple(
        c.strip() for c in _env("GEOMON_COMMUNES", "38259").split(",") if c.strip()))
    # Code and name of the group (used in file names and the web page). Default: the commune code
    # for a single commune, otherwise derived from the codes.
    site_code_env: str = field(default_factory=lambda: _env("GEOMON_SITE_CODE", ""))
    site_name_env: str = field(default_factory=lambda: _env("GEOMON_SITE_NAME", ""))
    # Or every commune of an administrative unit, e.g. DEP:50 (a département, NUTS-3), EPCI:<SIREN>,
    # ARR:<code>, REG:28; it replaces GEOMON_COMMUNES
    unit_env: str = field(default_factory=lambda: _env("GEOMON_UNIT", "").upper())
    aoi_buffer_m: float = field(default_factory=lambda: float(_env("GEOMON_AOI_BUFFER_M", "1000")))
    # The farm's own plots (GeoJSON, e.g. a PAC export), relative to the data directory; optional
    farm_file_env: str = field(default_factory=lambda: _env("GEOMON_FARM_FILE", ""))
    farm_name_env: str = field(default_factory=lambda: _env("GEOMON_FARM_NAME", ""))
    sites_file: str = field(default_factory=lambda: _env("GEOMON_SITES_FILE", "sites.json"))

    # Source vintages
    cog_vintage: int = field(default_factory=lambda: int(_env("GEOMON_COG_VINTAGE", "2026")))
    rpg_layer: str = field(default_factory=lambda: _env("GEOMON_RPG_LAYER", "RPG.LATEST"))
    rpg_year: int = field(default_factory=lambda: int(_env("GEOMON_RPG_YEAR", "2025")))
    # Earlier RPG years are loaded too (layers RPG.<year>), for the crop history of each parcel
    rpg_first_year: int = field(default_factory=lambda: int(_env("GEOMON_RPG_FIRST_YEAR", "2015")))

    # Raster resolutions (metres, Lambert-93)
    dem_res_m: float = field(default_factory=lambda: float(_env("GEOMON_DEM_RES_M", "5")))
    soil_res_m: float = field(default_factory=lambda: float(_env("GEOMON_SOIL_RES_M", "250")))

    # OpenStreetMap: "overpass" (AOI only, default) or "geofabrik" (regional extract, for later)
    osm_source: str = field(default_factory=lambda: _env("GEOMON_OSM_SOURCE", "overpass").lower())
    overpass_urls: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            u.strip()
            for u in _env("GEOMON_OVERPASS_URLS", "https://overpass-api.de/api/interpreter").split(",")
            if u.strip()
        )
    )
    overpass_timeout_s: int = field(default_factory=lambda: int(_env("GEOMON_OVERPASS_TIMEOUT_S", "180")))
    osm_pbf_url: str = field(
        default_factory=lambda: _env(
            "GEOMON_OSM_PBF_URL",
            "https://download.geofabrik.de/europe/france/rhone-alpes-latest.osm.pbf",
        )
    )
    osm_max_age_days: int = field(default_factory=lambda: int(_env("GEOMON_OSM_MAX_AGE_DAYS", "30")))

    # Weather (Specifications §4.2)
    weather_start: dt.date = field(
        default_factory=lambda: dt.date.fromisoformat(_env("GEOMON_WEATHER_START", "2022-01-01"))
    )
    weather_grid_deg: float = field(default_factory=lambda: float(_env("GEOMON_WEATHER_GRID_DEG", "0.025")))
    # Shortest time between two forecast downloads, in hours (new model runs come every 1-3 h)
    forecast_min_hours: float = field(default_factory=lambda: float(_env("GEOMON_FORECAST_MIN_HOURS", "3")))
    # SIM2 is kept from this date: the WMO reference period 1991-2020 (and the agricultural
    # years starting in autumn 1990) for the daily normals
    sim2_start: dt.date = field(
        default_factory=lambda: dt.date.fromisoformat(_env("GEOMON_SIM2_START", "1990-01-01"))
    )
    # ERA5-Land / ERA5: preliminary data (ERA5T) is replaced by the final release 2-3 months later;
    # the last GEOMON_ERA5_REVISION_DAYS are fetched again every GEOMON_ERA5_REVISION_EVERY_DAYS
    era5_revision_days: int = field(default_factory=lambda: int(_env("GEOMON_ERA5_REVISION_DAYS", "92")))
    era5_revision_every_days: int = field(default_factory=lambda: int(_env("GEOMON_ERA5_REVISION_EVERY_DAYS", "7")))
    weather_overlap_days: int = field(default_factory=lambda: int(_env("GEOMON_WEATHER_OVERLAP_DAYS", "3")))
    # ERA5-Land and ERA5: "cds" (Copernicus CDS time-series datasets: steps era5land and era5) or
    # "openmeteo" (the former source: ERA5-Land completed with ERA5 in one product, om_era5land,
    # fetched by the weather step within the Open-Meteo daily quota)
    era5_source: str = field(default_factory=lambda: _env("GEOMON_ERA5_SOURCE", "cds").lower())
    # ERA5 (0.25°) is kept from this date (default GEOMON_WEATHER_START; 1990-01-01 gives the
    # 1991-2020 normals). ERA5-Land starts at GEOMON_WEATHER_START.
    era5_start: dt.date = field(default_factory=lambda: dt.date.fromisoformat(
        _env("GEOMON_ERA5_START", _env("GEOMON_WEATHER_START", "2022-01-01"))))
    openmeteo_api_key: str = field(default_factory=lambda: _env("GEOMON_OPENMETEO_API_KEY", ""))
    openmeteo_calls_per_minute: float = field(
        default_factory=lambda: float(_env("GEOMON_OPENMETEO_CALLS_PER_MINUTE", "300"))
    )
    # Points per Open-Meteo request (the coordinates go in the URL)
    openmeteo_batch_points: int = field(default_factory=lambda: int(_env("GEOMON_OPENMETEO_BATCH_POINTS", "100")))
    # Largest number of Météo-France grid points per site: beyond it the grid step doubles (0.025°,
    # 0.05°, 0.1°...). Each point counts in Open-Meteo's daily quota (a forecast ~5 calls per point)
    weather_max_points: int = field(default_factory=lambda: int(_env("GEOMON_WEATHER_MAX_POINTS", "150")))
    # Copernicus CDS (AgERA5): personal access token from https://cds.climate.copernicus.eu/profile
    cds_api_key: str = field(default_factory=lambda: _env("GEOMON_CDS_API_KEY", _env("CDSAPI_KEY", "")))
    # AgERA5 starts in 1979; its whole history is one small request
    agera5_start: dt.date = field(
        default_factory=lambda: dt.date.fromisoformat(_env("GEOMON_AGERA5_START", "1979-01-01"))
    )
    cds_api_url: str = field(default_factory=lambda: _env("GEOMON_CDS_API_URL", "https://cds.climate.copernicus.eu/api"))
    # Sentinel-2 L2A through openEO (Copernicus Data Space Ecosystem)
    openeo_url: str = field(default_factory=lambda: _env("GEOMON_OPENEO_URL", "https://openeo.dataspace.copernicus.eu"))
    s2_start: dt.date = field(default_factory=lambda: dt.date.fromisoformat(_env("GEOMON_S2_START", "2015-06-27")))
    # Spectral bands kept besides SCL. B04 + B08 give NDVI; add B05 for NDRE, or any other band.
    s2_bands: tuple[str, ...] = field(default_factory=lambda: tuple(
        b.strip().upper() for b in _env("GEOMON_S2_BANDS", "B04,B08").split(",") if b.strip()))
    s2_max_cloud: float = field(default_factory=lambda: float(_env("GEOMON_S2_MAX_CLOUD", "80")))
    s2_min_clear: float = field(default_factory=lambda: float(_env("GEOMON_S2_MIN_CLEAR", "0.05")))
    s2_parallel_jobs: int = field(default_factory=lambda: int(_env("GEOMON_S2_PARALLEL_JOBS", "2")))
    s2_years_per_job: int = field(default_factory=lambda: int(_env("GEOMON_S2_YEARS_PER_JOB", "4")))
    # Parcel time series: a parcel-date is used when at least this share of the parcel is clear
    s2_parcel_min_clear: float = field(default_factory=lambda: float(_env("GEOMON_S2_PARCEL_MIN_CLEAR", "0.8")))
    # Daily curve of a parcel: "none" (linear interpolation of the clear dates) or "whittaker"
    s2_parcel_smoothing: str = field(default_factory=lambda: _env("GEOMON_S2_PARCEL_SMOOTHING", "none").lower())
    sim2_base_url: str = field(
        default_factory=lambda: _env("GEOMON_SIM2_BASE_URL", "https://meteofrance.s3.sbg.io.cloud.ovh.net/data/REF_CC/SIM")
    )

    user_agent: str = field(default_factory=lambda: _env("GEOMON_USER_AGENT", "geomon/0.1 (geospatial monitoring prototype; python-requests)"))

    # Working CRS for rasters (Lambert-93)
    work_crs: str = "EPSG:2154"

    @property
    def dsn(self) -> str:
        return (
            f"host={self.pg_host} port={self.pg_port} dbname={self.pg_db} "
            f"user={self.pg_user} password={self.pg_password}"
        )

    @property
    def ogr_pg(self) -> str:
        """Connection string for GDAL/OGR's PostgreSQL driver."""
        return f"PG:{self.dsn}"

    # -- the current site --------------------------------------------------------
    @property
    def site(self) -> "Site":
        return current_site()

    @property
    def communes(self) -> tuple[str, ...]:
        return self.site.communes

    @property
    def unit(self) -> str:
        return self.site.unit

    @property
    def site_code(self) -> str:
        return self.site.code

    @property
    def site_name(self) -> str:
        return self.site.name

    @property
    def farm_file(self) -> str:
        return self.site.farm_file

    @property
    def farm_name(self) -> str:
        return self.site.farm_name

    @property
    def zarr_dir(self) -> Path:
        return self.data_dir / "zarr"

    @property
    def site_zarr_dir(self) -> Path:
        """Rasters and weather stores of the current site."""
        return self.zarr_dir / "sites" / self.site_code

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"


settings = Settings()


# ---------------------------------------------------------------------------
# Sites
# ---------------------------------------------------------------------------
# Administrative units a site can cover whole (``unit``), and the attribute of the ADMIN EXPRESS
# commune layer that names the unit of each commune
UNIT_LEVELS = {"DEP": "code_insee_du_departement", "REG": "code_insee_de_la_region",
               "ARR": "code_insee_de_l_arrondissement", "EPCI": "codes_siren_des_epci"}


@dataclass(frozen=True)
class Site:
    code: str
    name: str
    communes: tuple[str, ...]          # INSEE codes; empty for a site defined by ``unit``
    farm_file: str = ""
    farm_name: str = ""
    unit: str = ""                     # "DEP:50": every commune of an administrative unit

    def __post_init__(self):
        if self.unit:
            level, _, code = self.unit.partition(":")
            if level not in UNIT_LEVELS or not code:
                raise ValueError(f"Site {self.code}: unit must be LEVEL:code with LEVEL in "
                                 f"{', '.join(UNIT_LEVELS)}, not {self.unit!r}")
        elif not self.communes:
            raise ValueError(f"Site {self.code}: give its communes or an administrative unit (unit)")


_SITES_CACHE: dict = {}


def _env_site() -> Site:
    if settings.unit_env:
        code = settings.site_code_env or settings.unit_env.lower().replace(":", "-")
        return Site(code, settings.site_name_env, (), settings.farm_file_env, settings.farm_name_env,
                    settings.unit_env)
    c = settings.communes_env
    code = settings.site_code_env or (c[0] if len(c) == 1 else "grp-" + "-".join(c))
    return Site(code, settings.site_name_env, c, settings.farm_file_env, settings.farm_name_env)


def sites_path() -> Path:
    p = Path(settings.sites_file)
    return p if p.is_absolute() else settings.data_dir / p


def all_sites() -> list[Site]:
    """The configured sites (sites.json, re-read when it changes), or the single site of the environment."""
    p = sites_path()
    if not p.exists():
        return [_env_site()]
    mtime = p.stat().st_mtime
    if _SITES_CACHE.get("mtime") != mtime:
        raw = json.loads(p.read_text(encoding="utf-8"))
        sites = [Site(code=s["code"], name=s.get("name") or ("" if s.get("unit") else s["code"]),
                      communes=tuple(str(c) for c in s.get("communes", ())),
                      farm_file=s.get("farm_file", ""), farm_name=s.get("farm_name", ""),
                      unit=str(s.get("unit", "")).upper()) for s in raw]
        if not sites:
            raise ValueError(f"{p}: no site")
        if len({s.code for s in sites}) != len(sites):
            raise ValueError(f"{p}: duplicate site codes")
        _SITES_CACHE.update(mtime=mtime, sites=sites)
    return _SITES_CACHE["sites"]


def get_site(code: str) -> Site:
    for s in all_sites():
        if s.code == code:
            return s
    raise KeyError(f"Unknown site {code!r} (sites: {', '.join(s.code for s in all_sites())})")


_current: contextvars.ContextVar[str | None] = contextvars.ContextVar("geomon_site", default=None)


def current_site() -> Site:
    code = _current.get()
    sites = all_sites()
    if code is None:
        return sites[0]
    return get_site(code)


def set_site(code: str | None) -> contextvars.Token:
    if code is not None:
        get_site(code)            # validate
    return _current.set(code)


@contextlib.contextmanager
def use_site(code: str | None) -> Iterator[Site]:
    token = set_site(code)
    try:
        yield current_site()
    finally:
        _current.reset(token)
