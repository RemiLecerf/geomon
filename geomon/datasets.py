"""The datasets, and how each one is followed for a site (chosen on the page's Data tab).

Modes:

- ``update``: the archive is kept and updated regularly by the scheduler (the default of the
  datasets the scheduler followed before; the static datasets are then refreshed every
  ``refresh_days``);
- ``frozen``: what is stored is kept, nothing is downloaded any more. "Update now" on the page still
  runs one update: with nothing stored yet, it downloads the archive once;
- ``live``: only the last ``keep_hours`` are kept, updated regularly (the datasets that offer it:
  the radar rain).

The page also asks for actions (update now, delete the stored data); they go to
meta.dataset_request and the scheduler runs them (the web server never runs a download).
"""

from __future__ import annotations

import os
import queue
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .config import settings
from .db import connect

MODES = {
    "update": "Update regularly",
    "frozen": "Frozen: keep, no updates",
    "live": "Live: recent hours only",
}
CATEGORIES = {
    "context": "Context and static data",
    "soil": "Terrain, soils and groundwater",
    "statistics": "Statistics",
    "weather": "Weather",
    "satellite": "Satellite images",
}


@dataclass(frozen=True)
class Every:
    """Checked once the period has passed since the last success."""
    minutes: int = 0
    hours: int = 0
    days: int = 0


@dataclass(frozen=True)
class Daily:
    """Checked once a day after this time (UTC)."""
    hour: int
    minute: int = 0


@dataclass(frozen=True)
class NewRun:
    """Checked at each new run of the forecast models (their metadata, every 15 minutes)."""


@dataclass(frozen=True)
class NewScene:
    """Checked at each newer acquisition of a STAC collection over each site (every 3 hours)."""
    collection: str


@dataclass(frozen=True)
class Dataset:
    code: str
    label: str
    category: str
    describe: str
    steps: tuple[str, ...]                     # ingestion steps of an update
    schedule: Every | Daily | NewRun | NewScene | None = None   # when the scheduler checks for updates
    job: str | None = None                     # the job's name when not the code (the scheduler's state key)
    modes: tuple[str, ...] = ("update", "frozen")
    default: str = "update"
    refresh_days: int | None = None            # static data: refresh period in update mode
    keep_hours: int = 24                       # live mode default
    stores: tuple[str, ...] = ()               # data paths (globs) of a site, relative to the data dir
    delete_sql: tuple[str, ...] = ()           # SQL removing a site's rows (%(site)s)
    shared: bool = False                       # data shared with other sites: not deleted per site
    requires: tuple[str, ...] = ()             # what a download needs (REQUIREMENTS), e.g. ("meteofrance",)

    def scheduled_job(self) -> str | None:
        if self.schedule:
            return self.job or self.code
        return f"refresh-{self.code}" if self.refresh_days else None


_S2 = "ARRAY['ndvi','ndre','ndwi','ndmi']"
_S1 = ("ARRAY['vv_asc','vh_asc','vh_vv_asc','rvi_asc','vv_desc','vh_desc','vh_vv_desc','rvi_desc']")

DATASETS: dict[str, Dataset] = {d.code: d for d in (
    # Context and static data
    Dataset("admin", "Administrative units", "context",
            "Communes, EPCI, départements, regions (IGN ADMIN EXPRESS, COG). Shared by the sites.",
            ("admin",), default="frozen", refresh_days=90, shared=True),
    Dataset("rpg", "Agricultural parcels (RPG)", "context",
            "RPG parcels and crops since 2015, farm îlots 2007-2014 (IGN). Shared by the sites. "
            "After an update, recompute the parcel and MODIS crop curves.",
            ("rpg",), default="frozen", refresh_days=30, shared=True),
    Dataset("osm", "OpenStreetMap", "context", "Roads, buildings, land use, water, places (Overpass).",
            ("osm",), default="frozen", refresh_days=30,
            delete_sql=("DELETE FROM ref.osm_feature WHERE site = %(site)s",)),
    Dataset("farm", "The farm's plots", "context", "The PAC declaration file (GEOMON_FARM_FILE).",
            ("farm",), default="frozen", refresh_days=1,
            delete_sql=("DELETE FROM ref.farm_plot WHERE site = %(site)s",
                        "DELETE FROM meta.aoi WHERE site = %(site)s AND kind = 'farm'"), requires=("farm",)),
    # Terrain and soils
    Dataset("dem", "Elevation (RGE ALTI)", "soil", "IGN elevation, slope, aspect, hillshade at 5 m.",
            ("dem",), modes=("frozen",), default="frozen", stores=("zarr/sites/{site}/dem*.zarr",)),
    Dataset("soil", "Soils (SoilGrids 2.0)", "soil", "ISRIC soil properties by depth, 250 m.",
            ("soil",), modes=("frozen",), default="frozen", stores=("zarr/sites/{site}/soil.zarr",)),
    Dataset("soil_inrae", "Soils of France (INRAE, 90 m)", "soil",
            "Texture, coarse fragments, water retention and available water capacity by depth (Gis Sol digital "
            "soil maps, French pedotransfer functions from SOLHYDRO). About 20 MB read per layer.",
            ("soil-inrae",), modes=("frozen",), default="frozen", stores=("zarr/sites/{site}/soil_inrae.zarr",)),
    Dataset("soil_maps", "Soil and geology maps", "soil",
            "Dominant soils (Gis Sol, 1:250 000), geology (BRGM, 1:50 000), clay shrink-swell exposure (BRGM).",
            ("soil-maps",), modes=("frozen",), default="frozen",
            delete_sql=("DELETE FROM ref.soil_feature WHERE site = %(site)s",)),
    Dataset("groundwater", "Groundwater levels (Hub'Eau)", "soil",
            "Daily water-table depth of the nearest active piezometers (ADES network, within 25 km).",
            ("groundwater",),
            delete_sql=("DELETE FROM ref.gw_site WHERE site = %(site)s",), schedule=Daily(7, 0)),
    # Statistics
    Dataset("insee", "INSEE statistics", "statistics", "Population and local key figures. Shared by the sites.",
            ("insee",), default="frozen", refresh_days=30, shared=True),
    # Weather
    Dataset("forecast", "Forecast (Météo-France, ECMWF)", "weather",
            "AROME, ARPEGE and ECMWF forecasts through Open-Meteo, each new run.",
            ("forecast",), stores=("zarr/sites/{site}/weather/om_forecast.zarr",), schedule=NewRun()),
    Dataset("weather", "Météo-France archive", "weather",
            "Archived Météo-France forecasts through Open-Meteo (hourly history), daily.",
            ("weather",), job="history", stores=("zarr/sites/{site}/weather/om_mf_hist*.zarr",
                                                 "raw/openmeteo/*/{site}"), schedule=Daily(2, 0)),
    Dataset("radar", "Rain radar, Europe (EUMETNET OPERA)", "weather",
            "All the European radars (Météo-France's included), 1 km every 5 minutes, the site and 100 km "
            "around. No account. The last 24 hours are fetched at once.",
            ("radar",), modes=("live", "update", "frozen"), default="live", keep_hours=24,
            stores=("zarr/sites/{site}/radar_opera.zarr",), schedule=Every(minutes=5)),
    Dataset("radar_mf", "Rain radar, France 500 m (Météo-France)", "weather",
            "Météo-France's rain mosaic, 500 m every 5 minutes, the site and 50 km around (needs a "
            "Météo-France API key). The API only serves the latest image, of all of France (2 MB every 5 "
            "minutes, about 575 MB a day): frozen by default, the OPERA radar already holding Météo-France's "
            "radars at 1 km. In live mode the hours are kept as they come.",
            ("radar-mf",), job="radar-mf", modes=("live", "update", "frozen"), default="frozen", keep_hours=24,
            stores=("zarr/sites/{site}/radar_mf.zarr",), requires=("meteofrance",), schedule=Every(minutes=5)),
    Dataset("nowcast", "Nowcast, next 6 hours (Météo-France AROME-PI)", "weather",
            "Rain every 15 minutes, temperature and gusts every hour for the next 6 hours, 0.01°, the site and "
            "50 km around; each new hourly run replaces the previous one. Shown after the radar images. Needs "
            "a Météo-France API key subscribed to AROME-PI.",
            ("arome-pi",), stores=("zarr/sites/{site}/nowcast_aromepi.zarr",), requires=("meteofrance",), schedule=Every(minutes=15)),
    Dataset("mf_obs", "Observations, last days (Météo-France stations)", "weather",
            "Hourly observations of the stations within 40 km (temperature, rain, wind and gusts, humidity, "
            "pressure...), refreshed every 30 minutes. Live mode keeps the last hours; update mode keeps them all.",
            ("mf-obs",), modes=("live", "update", "frozen"), default="live", keep_hours=120,
            stores=("zarr/sites/{site}/weather/mf_obs.zarr",), requires=("meteofrance",), schedule=Every(minutes=30)),
    Dataset("vigilance", "Vigilance (Météo-France)", "weather",
            "Weather warnings of the site's départements for today and tomorrow, with the bulletins. Shared by the sites.",
            ("vigilance",), shared=True, stores=("raw/meteofrance_alerts/vigilance*.json",), requires=("meteofrance",), schedule=Every(minutes=15)),
    Dataset("meteo_forets", "Météo des forêts (fire danger)", "weather",
            "Forest fire danger of the site's départements for tomorrow and the day after, published during the "
            "fire season. Shared by the sites.",
            ("meteo-forets",), shared=True, stores=("raw/meteofrance_alerts/meteo_forets_*.json",), requires=("meteofrance",), schedule=Every(hours=3)),
    Dataset("arome", "Forecast maps, AROME 1.3 km (Météo-France)", "weather",
            "Hourly rain, temperature and gusts for 51 hours over the site and 30 km around, each new run "
            "(every 3 hours) replacing the previous one.",
            ("arome",), stores=("zarr/sites/{site}/arome_maps.zarr",), requires=("meteofrance",), schedule=Every(minutes=30)),
    Dataset("ensemble", "Ensemble probabilities, PE-AROME (Météo-France)", "weather",
            "Probabilities of rain (next two 24-hour windows) and of frost (next mornings) over the site, "
            "from the 25 members of each run (every 6 hours).",
            ("pe-arome",), stores=("zarr/sites/{site}/ensemble_pearome.json",), requires=("meteofrance",), schedule=Every(minutes=30)),
    Dataset("sea", "Waves and storm surge (Météo-France)", "weather",
            "For a coastal site: wave height, period and direction (MFWAM) and the storm surge (HYCOM2D) at "
            "the nearest sea cells, about 2 days ahead.",
            ("sea",), stores=("zarr/sites/{site}/sea.zarr", "zarr/sites/{site}/sea_status.json"), requires=("meteofrance", "coast"), schedule=Every(hours=3)),
    Dataset("sim2", "SIM2 (SAFRAN-ISBA)", "weather", "Météo-France daily reanalysis, 8 km.",
            ("sim2",), stores=("zarr/sites/{site}/weather/sim2.zarr",), schedule=Every(hours=6)),
    Dataset("mf_stations", "Météo-France stations (measured)", "weather",
            "Daily measurements of the stations within 40 km (rain, temperatures, wind, humidity, radiation, "
            "ETP, snow), since 1950. No account.",
            ("mf-stations",), stores=("zarr/sites/{site}/weather/mf_stations.zarr",), schedule=Daily(8, 0)),
    Dataset("comephore", "COMEPHORE rainfall reanalysis", "weather",
            "Hourly rain at 1 km from radar and rain gauges since 1997, summed by day. About 50 GB of archives "
            "streamed over several days for the whole record. No account.",
            ("comephore",), stores=("zarr/sites/{site}/weather/comephore.zarr",), schedule=Daily(3, 0)),
    Dataset("mf_etp", "Météo-France ETP (grid)", "weather",
            "Daily Penman-Monteith ETP on a 0.125° grid since 1970. No account.",
            ("mf-etp",), stores=("zarr/sites/{site}/weather/mf_etp.zarr",), schedule=Daily(8, 30)),
    Dataset("agera5", "AgERA5", "weather", "Copernicus agro-meteorological daily data, 0.1°.",
            ("agera5",), stores=("zarr/sites/{site}/weather/agera5.zarr", "raw/agera5/{site}"), requires=("cds",), schedule=Every(hours=6)),
    Dataset("era5land", "ERA5-Land", "weather", "Copernicus hourly reanalysis, 0.1°.",
            ("era5land",), stores=("zarr/sites/{site}/weather/era5land*.zarr", "raw/era5land/{site}"), requires=("cds_era5land",), schedule=Every(hours=6)),
    Dataset("era5", "ERA5", "weather", "Copernicus hourly reanalysis, 0.25°.",
            ("era5",), stores=("zarr/sites/{site}/weather/era5.zarr", "zarr/sites/{site}/weather/era5_daily.zarr",
                                            "raw/era5/{site}"), requires=("cds",), schedule=Every(hours=6)),
    # Satellite
    Dataset("sentinel2", "Sentinel-2 L2A", "satellite", "Optical images, 10 m, since 2015 (openEO).",
            ("sentinel2",), stores=("zarr/sentinel2/{site}", "raw/sentinel2/{site}"),
            delete_sql=(f"DELETE FROM eo.index_stats WHERE site = %(site)s AND index = ANY({_S2})",
                        "DELETE FROM eo.parcel_stats WHERE site_id = meta.site_key_of(%(site)s)",
                        "DELETE FROM eo.parcel_dates WHERE site = %(site)s",
                        "DELETE FROM eo.parcel_season WHERE site = %(site)s",
                        f"DELETE FROM eo.group_season WHERE site = %(site)s AND index = ANY({_S2})"), requires=("openeo",), schedule=NewScene("sentinel-2-l2a")),
    Dataset("sentinel1", "Sentinel-1 GRD", "satellite", "Radar backscatter, 10 m, since 2023 (openEO).",
            ("sentinel1",), stores=("zarr/sentinel1/{site}", "raw/sentinel1/{site}"),
            delete_sql=(f"DELETE FROM eo.index_stats WHERE site = %(site)s AND index = ANY({_S1})",), requires=("openeo",), schedule=NewScene("sentinel-1-grd")),
    Dataset("modis", "MODIS NDVI", "satellite", "Terra and Aqua 16-day NDVI, 250 m, since 2000.",
            ("modis",), stores=("zarr/modis/{site}",),
            delete_sql=("DELETE FROM eo.index_stats WHERE site = %(site)s AND index = 'modis_ndvi'",
                        "DELETE FROM eo.group_season WHERE site = %(site)s AND index = 'modis_ndvi'"), schedule=Every(hours=12)),
)}

ACTIVE = ("update", "live")                    # modes the scheduler updates


def by_job(job: str) -> list[Dataset]:
    return [d for d in DATASETS.values() if d.scheduled_job() == job]


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------
_POLICIES: dict[str, tuple[float, dict]] = {}
POLICIES_TTL_S = 30             # the scheduler asks for every job and site each minute


def policies(site: str) -> dict[str, dict]:
    """Mode and kept hours of every dataset for a site (the defaults where none was chosen), read
    again after POLICIES_TTL_S (a change on the page reaches the scheduler within that time)."""
    hit = _POLICIES.get(site)
    if hit is None or time.monotonic() - hit[0] > POLICIES_TTL_S:
        hit = _POLICIES[site] = (time.monotonic(), _read_policies(site))
    return hit[1]


def _read_policies(site: str) -> dict[str, dict]:
    with connect() as conn:
        rows = {r["dataset"]: r for r in conn.execute(
            "SELECT dataset, mode, keep_hours, updated_at FROM meta.dataset_policy WHERE site = %s", (site,)).fetchall()}
    out = {}
    for code, d in DATASETS.items():
        r = rows.get(code)
        mode = r["mode"] if r and r["mode"] in d.modes else d.default
        out[code] = {"mode": mode, "keep_hours": (r and r["keep_hours"]) or d.keep_hours,
                     "chosen": r is not None, "updated_at": r and r["updated_at"]}
    return out


def policy(site: str, code: str) -> dict:
    return policies(site)[code]


def set_policy(site: str, code: str, mode: str, keep_hours: int | None = None) -> dict:
    d = DATASETS[code]
    if mode not in d.modes:
        raise ValueError(f"{code}: mode {mode} not offered ({', '.join(d.modes)})")
    if keep_hours is not None and not 1 <= keep_hours <= 24 * 31:
        raise ValueError("keep_hours: 1 hour to 31 days")
    with connect() as conn:
        conn.execute(
            """INSERT INTO meta.dataset_policy (site, dataset, mode, keep_hours) VALUES (%s, %s, %s, %s)
               ON CONFLICT (site, dataset) DO UPDATE SET mode = EXCLUDED.mode,
                   keep_hours = COALESCE(EXCLUDED.keep_hours, meta.dataset_policy.keep_hours), updated_at = now()""",
            (site, code, mode, keep_hours))
    _POLICIES.pop(site, None)
    return policy(site, code)


def scheduled(site: str, job: str) -> bool:
    """Whether the scheduler job updates this site: one of its datasets is in an active mode and has
    what it needs (a key, a login: without it the step would only log a skip, every 5 minutes for
    the radar)."""
    ds = by_job(job)
    if not ds:
        return True                            # a job of no catalogued dataset
    pol = policies(site)
    return any(pol[d.code]["mode"] in ACTIVE and not needs(d.code, site) for d in ds)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def paths(site: str, code: str) -> list[Path]:
    root = settings.data_dir
    out = []
    for pattern in DATASETS[code].stores:
        out += sorted(root.glob(pattern.format(site=site)))
    return out


def _du(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    total = 0
    for dirpath, _, files in os.walk(p):
        for f in files:
            try:
                total += os.stat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total


_SIZES: dict[tuple[str, str], tuple[float, int]] = {}
_SIZES_LOCK = threading.Lock()
_QUEUE: "queue.Queue[tuple[str, str]]" = queue.Queue()
_QUEUED: set[tuple[str, str]] = set()
_WORKER: threading.Thread | None = None
SIZE_TTL_S = 1800


def _measure(key: tuple[str, str]) -> int:
    n = sum(_du(p) for p in paths(*key))
    with _SIZES_LOCK:
        _SIZES[key] = (time.monotonic(), n)
    return n


def _size_worker() -> None:
    while True:
        key = _QUEUE.get()
        try:
            _measure(key)
        except Exception:
            pass
        finally:
            with _SIZES_LOCK:
                _QUEUED.discard(key)


def size_bytes(site: str, code: str, fresh: bool = False) -> int | None:
    """Disk size of a dataset's stores for a site. Walking large stores takes minutes on a Windows
    bind mount (one stat per Zarr chunk): the size is measured by a background thread and the last
    measure returned at once (None until the first one); ``fresh`` measures now."""
    global _WORKER
    key = (site, code)
    if fresh:
        return _measure(key)
    with _SIZES_LOCK:
        hit = _SIZES.get(key)
        if (hit is None or time.monotonic() - hit[0] > SIZE_TTL_S) and key not in _QUEUED:
            _QUEUED.add(key)
            _QUEUE.put(key)
            if _WORKER is None or not _WORKER.is_alive():
                _WORKER = threading.Thread(target=_size_worker, name="dataset-sizes", daemon=True)
                _WORKER.start()
    return hit[1] if hit else None


def delete(site: str, code: str) -> dict:
    """Removes a dataset's stored data for a site (not the data shared with other sites)."""
    d = DATASETS[code]
    if d.shared:
        raise ValueError(f"{d.label} is shared by the sites: it is not deleted per site")
    removed = []
    for p in paths(site, code):
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)
        removed.append(str(p.relative_to(settings.data_dir)))
    rows = 0
    with connect() as conn:
        for sql in d.delete_sql:
            rows += conn.execute(sql, {"site": site}).rowcount or 0
    with _SIZES_LOCK:
        _SIZES.pop((site, code), None)
    _clear_caches(code)
    return {"removed": removed, "rows": rows}


def _clear_caches(code: str) -> None:
    """Cached store handles of the processes (the web API) after a deletion."""
    try:
        if code == "sentinel2":
            from . import sentinel2
            sentinel2.clear_cache()
        elif code == "sentinel1":
            from . import sentinel1
            sentinel1.clear_cache()
        elif code == "modis":
            from . import modis
            modis.clear_cache()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Requests from the page, run by the scheduler
# ---------------------------------------------------------------------------
def request(site: str, code: str, action: str) -> int:
    if action not in ("update", "delete"):
        raise ValueError(action)
    if action == "delete" and DATASETS[code].shared:
        raise ValueError(f"{DATASETS[code].label} is shared by the sites: it is not deleted per site")
    with connect() as conn:
        dup = conn.execute(
            """SELECT id FROM meta.dataset_request WHERE site = %s AND dataset = %s AND action = %s
               AND status IN ('pending', 'running')""", (site, code, action)).fetchone()
        if dup:
            return dup["id"]
        return conn.execute(
            "INSERT INTO meta.dataset_request (site, dataset, action) VALUES (%s, %s, %s) RETURNING id",
            (site, code, action)).fetchone()["id"]


def next_request() -> dict | None:
    """Takes the oldest pending request (marked running)."""
    with connect() as conn:
        return conn.execute(
            """UPDATE meta.dataset_request SET status = 'running', started_at = now()
               WHERE id = (SELECT id FROM meta.dataset_request WHERE status = 'pending'
                           ORDER BY requested_at LIMIT 1 FOR UPDATE SKIP LOCKED)
               RETURNING id, site, dataset, action""").fetchone()


def finish_request(rid: int, ok: bool, message: str = "") -> None:
    with connect() as conn:
        conn.execute("UPDATE meta.dataset_request SET status = %s, finished_at = now(), message = %s WHERE id = %s",
                     ("done" if ok else "failed", message[:2000], rid))


def reset_stale_requests() -> None:
    """Requests left running by a stopped scheduler go back to the queue."""
    with connect() as conn:
        conn.execute("UPDATE meta.dataset_request SET status = 'pending', started_at = NULL WHERE status = 'running'")


# ---------------------------------------------------------------------------
# Status for the page
# ---------------------------------------------------------------------------
def _meteofrance(code: str, site: str) -> str | None:
    from . import meteofrance
    return None if meteofrance.configured() else (
        "Météo-France credentials (GEOMON_METEOFRANCE_APPLICATION_ID, free at portail-api.meteofrance.fr)")


def _cds(code: str, site: str) -> str | None:
    return None if settings.cds_api_key else "a Copernicus CDS key (GEOMON_CDS_API_KEY)"


def _cds_era5land(code: str, site: str) -> str | None:
    """ERA5-Land comes from the CDS unless GEOMON_ERA5_SOURCE says Open-Meteo."""
    return _cds(code, site) if settings.era5_source == "cds" else None


def _openeo(code: str, site: str) -> str | None:
    from openeo.rest.auth.config import RefreshTokenStore
    if os.environ.get("OPENEO_AUTH_METHOD") or RefreshTokenStore().path.exists():
        return None
    return "a Copernicus Data Space login (docker compose run --rm ingest sentinel2-login)"


def _coast(code: str, site: str) -> str | None:
    """A site found inland by the sea step."""
    from .mf_models import SEA_MAX_KM, coastal

    if coastal(site) is False:
        return (f"a site on the coast (no sea cell within {SEA_MAX_KM} km; after a change of the site's extent, "
                "delete this dataset's data to check again)")
    return None


REQUIREMENTS = {
    "meteofrance": _meteofrance,
    "cds": _cds,
    "cds_era5land": _cds_era5land,
    "openeo": _openeo,
    "farm": lambda code, site: None if settings.farm_file else "a farm file (GEOMON_FARM_FILE)",
    "coast": _coast,
}


def needs(code: str, site: str | None = None) -> str | None:
    """What a dataset needs before it can be downloaded for a site (default: the current one), if missing."""
    for r in DATASETS[code].requires:
        need = REQUIREMENTS[r](code, site or settings.site_code)
        if need:
            return need
    return None


def of_step(step: str) -> Dataset | None:
    """The dataset an ingestion step updates."""
    return next((d for d in DATASETS.values() if step in d.steps), None)


def missing(code: str, what: str) -> str:
    """Why an API has nothing to show for a dataset yet (the 404 message)."""
    need = needs(code)
    return f"No {what} yet: " + (f"needs {need}" if need else
                                  "the scheduler fetches it when the dataset is in update or live mode (Data tab)")


def status(site: str) -> list[dict]:
    pol = policies(site)
    with connect() as conn:
        last = {}
        for r in conn.execute(
                """SELECT DISTINCT ON (step, status = 'ok') step, status, finished_at, message
                   FROM meta.ingest_log WHERE site = %s ORDER BY step, status = 'ok', finished_at DESC""",
                (site,)).fetchall():
            last.setdefault(r["step"], {})["ok" if r["status"] == "ok" else "error"] = r
        reqs = conn.execute(
            """SELECT DISTINCT ON (dataset, action) id, dataset, action, status, requested_at, finished_at, message
               FROM meta.dataset_request WHERE site = %s ORDER BY dataset, action, requested_at DESC""",
            (site,)).fetchall()
    out = []
    for code, d in DATASETS.items():
        ok = [last[s]["ok"]["finished_at"] for s in d.steps if "ok" in last.get(s, {})]
        err = [last[s]["error"] for s in d.steps if "error" in last.get(s, {})]
        last_ok = max(ok) if ok else None
        last_err = max(err, key=lambda r: r["finished_at"]) if err else None
        out.append({
            "code": code, "label": d.label, "category": d.category, "describe": d.describe,
            "modes": [{"mode": m, "label": MODES[m]} for m in d.modes], **pol[code],
            "refresh_days": d.refresh_days, "scheduled": d.scheduled_job() is not None,
            "shared": d.shared, "deletable": not d.shared and bool(d.stores or d.delete_sql),
            "last_ok": last_ok,
            "last_error": None if not last_err or (last_ok and last_err["finished_at"] < last_ok)
            else {"at": last_err["finished_at"], "message": (last_err["message"] or "")[:300]},
            "size_bytes": size_bytes(site, code) if d.stores else None,
            "size_pending": bool(d.stores) and (site, code) in _QUEUED,
            "requests": [{k: r[k] for k in ("id", "action", "status", "requested_at", "finished_at", "message")}
                         for r in reqs if r["dataset"] == code],
            "needs": needs(code, site)})
    return out

