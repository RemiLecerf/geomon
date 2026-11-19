"""Hourly weather from Open-Meteo (Specifications §4.2).

Three products, stored as Zarr ``(time, point)`` under data/zarr/weather:

* ``om_mf_hist``: Météo-France archived forecasts (Historical Forecast API,
  ``meteofrance_seamless``). Open-Meteo merges AROME France HD (1.5 km:
  temperature, humidity, wind; precipitation from 2024), AROME France (2.5 km,
  complete from 2024-01-02) and ARPEGE Europe (11 km, archive from
  2022-11-13). Points follow the AROME 2.5 km grid over the AOI.
* ``om_era5land``: ERA5-Land reanalysis (0.1°), for soil temperature and
  moisture, snow depth, and as a homogeneous long-term reference. Open-Meteo
  does not serve ERA5-Land precipitation, snowfall, wind, radiation or ET0:
  those come from ERA5 (0.25°). The ``models`` attribute of each variable says
  which model provided it.
* ``om_forecast``: latest forecast. Météo-France (AROME then ARPEGE, ~4 days),
  continued by ECMWF IFS 9 km up to 15 days; soil and snow from IFS.

History is fetched month by month (ERA5-Land: year by year) for all points at once,
and kept as raw JSON: a re-run only asks for the days since the last one, plus the
last few again (see :func:`_history`). Requests are paced to stay within the free API limits
(600 calls/min, 5,000/h, 10,000/day; a call is weighted by days and
variables, and counts once per location). Free use is for non-commercial
purposes; set GEOMON_OPENMETEO_API_KEY to use a commercial subscription.

Only the grid points whose cell touches one of the site's communes are
requested (not the whole bounding box: for communes spread along a coast, most
of the box is sea or other communes). When the daily or hourly limit is
reached, the step stops at once with :class:`QuotaExceeded` (months already
fetched are kept) and the scheduler tries again later.
"""

from __future__ import annotations

import collections
import datetime as dt
import gzip
import hashlib
import json
import logging
import math
import re
import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import xarray as xr
from shapely.geometry import box, shape

from ..aoi import AOI
from ..config import settings
from ..http import session
from ..weather import PRODUCTS, store_path, write_product
from . import RetryLater

log = logging.getLogger(__name__)

MF_ARCHIVE_START = dt.date(2022, 11, 13)   # ARPEGE Europe / AROME HD archive start at Open-Meteo

MF_VARS = [
    "temperature_2m", "relative_humidity_2m", "dew_point_2m", "precipitation", "rain", "snowfall",
    "shortwave_radiation", "wind_speed_10m", "et0_fao_evapotranspiration", "surface_pressure",
    "cloud_cover", "vapour_pressure_deficit",
]
SOIL_VARS = [
    f"soil_{kind}_{layer}"
    for kind in ("temperature", "moisture")
    for layer in ("0_to_7cm", "7_to_28cm", "28_to_100cm", "100_to_255cm")
]
ERA5LAND_VARS = [
    "temperature_2m", "relative_humidity_2m", "dew_point_2m", "precipitation", "snowfall",
    "shortwave_radiation", "wind_speed_10m", "et0_fao_evapotranspiration", "snow_depth", *SOIL_VARS,
]
FORECAST_VARS = MF_VARS + ["snow_depth", *SOIL_VARS]

# Open-Meteo unit strings -> pint
UNITS = {"°C": "degC", "%": "percent", "mm": "mm", "cm": "cm", "m": "m", "W/m²": "W/m**2",
         "m/s": "m/s", "hPa": "hPa", "kPa": "kPa", "m³/m³": "m**3/m**3"}
ACCUMULATED = {"precipitation", "rain", "snowfall", "et0_fao_evapotranspiration"}
HOUR_MEAN = {"shortwave_radiation"}


# ---------------------------------------------------------------------------
# HTTP, with pacing
# ---------------------------------------------------------------------------
class QuotaExceeded(RetryLater):
    """Open-Meteo's request limit is reached: retried after the Retry-After it gave, else after
    the next UTC day (daily limit) or hour."""

    def __init__(self, reason: str):
        now = dt.datetime.now(dt.timezone.utc)
        after = re.search(r"Retry-After (\d+)", reason)
        if after:
            at = now + dt.timedelta(seconds=int(after.group(1)))
        elif "daily" in reason.lower():
            at = (now + dt.timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
        else:
            at = (now + dt.timedelta(hours=1)).replace(minute=5, second=0, microsecond=0)
        super().__init__(f"Open-Meteo request limit reached: {reason}", at)


def _base(kind: str) -> str:
    host = {"forecast": "api", "historical": "historical-forecast-api", "archive": "archive-api"}[kind]
    path = "/v1/archive" if kind == "archive" else "/v1/forecast"
    if settings.openmeteo_api_key:
        return f"https://customer-{host}.open-meteo.com{path}"
    return f"https://{host}.open-meteo.com{path}"


class _Pacer:
    """Keeps the weighted call count under a per-minute budget (shared by the scheduler's
    forecast and history threads)."""

    def __init__(self, per_minute: float):
        self.per_minute = per_minute
        self.calls: collections.deque[tuple[float, float]] = collections.deque()
        self.lock = threading.Lock()

    def wait(self, weight: float) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                while self.calls and now - self.calls[0][0] > 60:
                    self.calls.popleft()
                used = sum(w for _, w in self.calls)
                if used + weight <= self.per_minute or not self.calls:
                    self.calls.append((now, weight))
                    return
                pause = max(1.0, 60 - (now - self.calls[0][0]))
            time.sleep(pause)


_pacer = _Pacer(settings.openmeteo_calls_per_minute)


def _weight(n_vars: int, days: int, n_points: int = 1) -> float:
    return max(1.0, n_vars / 10) * max(1.0, days / 14) * n_points


def _get(kind: str, params: dict, weight: float) -> dict | list:
    if settings.openmeteo_api_key:
        params = params | {"apikey": settings.openmeteo_api_key}
    else:
        _pacer.wait(weight)
    # The session retries a 429 (backoff, or a short Retry-After) and gives up on a long one
    try:
        r = session().get(_base(kind), params=params, timeout=300)
    except requests.exceptions.RetryError as exc:
        if "429" in str(exc):
            raise QuotaExceeded(str(exc)) from exc
        raise
    if r.status_code >= 400:
        raise RuntimeError(f"Open-Meteo {kind} {r.status_code}: {r.text[:300]}")
    return r.json()


# ---------------------------------------------------------------------------
# Points
# ---------------------------------------------------------------------------
def grid_points(aoi: AOI, step: float) -> list[tuple[float, float]]:
    """Centres of a regular lon/lat grid (multiples of ``step``) whose cells touch the AOI's
    communes (cells of the bounding box away from every commune are left out)."""
    x0, y0, x1, y1 = aoi.bbox_wgs84
    lons = np.arange(math.floor((x0 + step / 2) / step), math.ceil((x1 - step / 2) / step) + 1) * step
    lats = np.arange(math.floor((y0 + step / 2) / step), math.ceil((y1 - step / 2) / step) + 1) * step
    pts = [(round(float(lo), 4), round(float(la), 4)) for la in lats for lo in lons]
    geom = shape(aoi.geom_wgs84)
    h = step / 2
    inside = [p for p in pts if box(p[0] - h, p[1] - h, p[0] + h, p[1] + h).intersects(geom)]
    return inside or pts


# ---------------------------------------------------------------------------
# Fetch and assemble
# ---------------------------------------------------------------------------
def _parse(resp: dict, variables: list[str], models: list[str] | None = None) -> tuple[pd.DataFrame, dict]:
    """One location's hourly block -> DataFrame (UTC index). With several models,
    values of the first model are used where present, then the next ones."""
    h = resp["hourly"]
    idx = pd.to_datetime(np.asarray(h["time"], dtype="int64"), unit="s")
    units = resp.get("hourly_units", {})
    cols, out_units, filled_by = {}, {}, {}
    for v in variables:
        if models and len(models) > 1:
            merged = None
            for m in models:
                key = f"{v}_{m}"
                if key not in h:
                    continue
                arr = np.asarray(h[key], dtype="float64")
                if merged is None:
                    merged = arr
                    filled_by[v] = [m] if (~np.isnan(arr)).any() else []
                else:
                    gap = np.isnan(merged) & ~np.isnan(arr)
                    if gap.any():
                        filled_by[v].append(m)
                    merged = np.where(np.isnan(merged), arr, merged)
                out_units[v] = units.get(key)
            if merged is not None:
                cols[v] = merged
        elif v in h:
            cols[v] = np.asarray(h[v], dtype="float64")
            out_units[v] = units.get(v)
    return pd.DataFrame(cols, index=idx), {"units": out_units, "models": filled_by}


def _fetch(kind: str, points: list[tuple[float, float]], variables: list[str], extra: dict,
           start: dt.date | None = None, end: dt.date | None = None) -> list[dict]:
    """All points, in requests of GEOMON_OPENMETEO_BATCH_POINTS (Open-Meteo accepts lists of
    coordinates, sent in the URL). Returns one block per point, in order."""
    base = {"hourly": ",".join(variables), "timezone": "GMT", "timeformat": "unixtime",
            "wind_speed_unit": "ms", "cell_selection": "nearest", **extra}
    n_models = len(str(extra.get("models", "")).split(","))
    if start is None:
        days = int(extra.get("forecast_days", 7))
    else:
        base |= {"start_date": start.isoformat(), "end_date": end.isoformat()}
        days = (end - start).days + 1
    size = max(1, settings.openmeteo_batch_points)
    out: list[dict] = []
    for k in range(0, len(points), size):
        batch = points[k:k + size]
        params = base | {"latitude": ",".join(f"{p[1]:.4f}" for p in batch),
                         "longitude": ",".join(f"{p[0]:.4f}" for p in batch)}
        resp = _get(kind, params, _weight(len(variables) * n_models, days, len(batch)))
        out += resp if isinstance(resp, list) else [resp]
    return out


def weather_grid(aoi: AOI) -> tuple[float, list[tuple[float, float]]]:
    """Step and points of the Météo-France grid of a site: GEOMON_WEATHER_GRID_DEG (the AROME 2.5 km
    grid), doubled until the site has at most GEOMON_WEATHER_MAX_POINTS points (a département would
    exceed Open-Meteo's free daily quota at 2.5 km; coarser, the points sample the model's cells)."""
    step = settings.weather_grid_deg
    points = grid_points(aoi, step)
    while len(points) > settings.weather_max_points:
        step *= 2
        points = grid_points(aoi, step)
    if step != settings.weather_grid_deg:
        log.info("Météo-France grid: %g° (%d points; %g° would exceed GEOMON_WEATHER_MAX_POINTS=%d)",
                 step, len(points), settings.weather_grid_deg, settings.weather_max_points)
    return step, points


def _months(start: dt.date, end: dt.date, months: int):
    t = start
    while t <= end:
        y, m = t.year + (t.month - 1 + months) // 12, (t.month - 1 + months) % 12 + 1
        t1 = min(end, dt.date(y, m, 1) - dt.timedelta(days=1))
        yield t, t1
        t = t1 + dt.timedelta(days=1)


def _to_dataset(frames: list[pd.DataFrame], meta: list[dict], points: list[tuple[float, float]],
                variables: list[str], descriptions: dict) -> xr.Dataset:
    index = frames[0].index
    for f in frames[1:]:
        index = index.union(f.index)
    data = {}
    for v in variables:
        # Skip variables the model does not provide (all values missing)
        if not any(v in f and f[v].notna().any() for f in frames):
            continue
        arr = np.full((len(index), len(frames)), np.nan, dtype="float32")
        for k, f in enumerate(frames):
            if v in f:
                arr[:, k] = f[v].reindex(index).values
        u = next((m["units"].get(v) for m in descriptions["meta"] if m["units"].get(v)), None)
        attrs = {"units": UNITS.get(u, u or ""), "long_name": v.replace("_", " ")}
        used = sorted({mod for m in descriptions["meta"] for mod in m.get("models", {}).get(v, [])})
        if used:
            attrs["models"] = ",".join(used)
        attrs["time_reference"] = ("sum over the hour ending at the timestamp" if v in ACCUMULATED
                                   else "mean over the hour ending at the timestamp" if v in HOUR_MEAN
                                   else "instantaneous")
        data[v] = (("time", "point"), arr, attrs)
    ds = xr.Dataset(
        data,
        coords={
            "time": index.values,
            "point": np.arange(len(frames)),
            "lon": ("point", np.array([m["lon"] for m in meta], dtype="float64")),
            "lat": ("point", np.array([m["lat"] for m in meta], dtype="float64")),
            "req_lon": ("point", np.array([p[0] for p in points])),
            "req_lat": ("point", np.array([p[1] for p in points])),
            "elevation": ("point", np.array([m["elevation"] for m in meta], dtype="float64"),
                          {"units": "m", "long_name": "elevation used by Open-Meteo for downscaling"}),
        },
    )
    return ds


def _reuse(stored: dict, signature: dict) -> list[dict] | None:
    """The responses of a cached chunk for the requested points: all of them when the request
    is the same, or the requested subset when the chunk was fetched for more points."""
    old = stored.get("signature", {})
    if old == signature:
        return stored["responses"]
    if old.get("variables") != signature["variables"] or old.get("extra") != signature["extra"]:
        return None
    where = {tuple(p): i for i, p in enumerate(old.get("points", []))}
    idx = [where.get(tuple(p)) for p in signature["points"]]
    if any(i is None for i in idx):
        return None
    return [stored["responses"][i] for i in idx]


def _load_chunk(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def _save_chunk(path: Path, signature: dict, responses: list[dict]) -> None:
    part = path.with_suffix(".part")
    with gzip.open(part, "wt", encoding="utf-8") as fh:
        json.dump({"signature": signature, "responses": responses}, fh)
    part.replace(path)


def _cached_chunks(cache: Path) -> dict[dt.date, tuple[Path, dt.date]]:
    """Cached chunks by first day: (file, last day fetched). Files are named <first>_<last>."""
    out = {}
    for p in cache.glob("*.json.gz"):
        try:
            a, b = p.name[:-len(".json.gz")].split("_")
            out[dt.date.fromisoformat(a)] = (p, dt.date.fromisoformat(b))
        except ValueError:
            continue
    return out


def _merge(old: list[dict], new: list[dict], cut: dt.date) -> list[dict]:
    """Per point: the hours of ``old`` before ``cut``, then those of ``new``."""
    cut_s = int(pd.Timestamp(cut).timestamp())
    out = []
    for o, n in zip(old, new):
        ho, hn = o["hourly"], n["hourly"]
        keep = int(np.searchsorted(np.asarray(ho["time"], dtype="int64"), cut_s))
        hourly = {}
        for key in set(ho) | set(hn):
            a = ho.get(key, [None] * len(ho["time"]))[:keep]
            b = hn.get(key, [None] * len(hn["time"]))
            hourly[key] = list(a) + list(b)
        out.append(n | {"hourly": hourly})
    return out


def _signature_id(signature: dict) -> str:
    return hashlib.sha1(json.dumps(signature, sort_keys=True).encode()).hexdigest()[:16]


def _stored(name: str, sig_id: str) -> xr.Dataset | None:
    """The current store of a product, when it was built from the same request (points,
    variables, models); not loaded."""
    path = store_path(name)
    if not (path / "zarr.json").exists():
        return None
    ds = xr.open_zarr(path, consolidated=True, chunks=None)       # lazy, without dask
    if ds.attrs.get("signature") != sig_id:
        ds.close()
        return None
    return ds


def _combine(old: xr.Dataset, new: xr.Dataset) -> xr.Dataset:
    """The hours of ``old`` (loaded, before those of ``new``) followed by ``new``; same points.
    The models of each variable are the union of both."""
    variables = list(dict.fromkeys([*old.data_vars, *new.data_vars]))
    data = {}
    for v in variables:
        a = old[v].values if v in old else np.full((old.sizes["time"], new.sizes["point"]), np.nan, "float32")
        b = new[v].values if v in new else np.full((new.sizes["time"], new.sizes["point"]), np.nan, "float32")
        attrs = dict((old[v] if v in old else new[v]).attrs) | dict(new[v].attrs if v in new else {})
        models = {m for d in (old, new) if v in d for m in str(d[v].attrs.get("models", "")).split(",") if m}
        if models:
            attrs["models"] = ",".join(sorted(models))
        data[v] = (("time", "point"), np.concatenate([a, b]).astype("float32"), attrs)
    coords = {k: c for k, c in new.coords.items() if k != "time"}
    return xr.Dataset(data, coords={"time": np.concatenate([old.time.values, new.time.values]), **coords})


def _revision_due(cache: Path, every_days: int, today: dt.date) -> bool:
    """Whether the periodic re-fetch of the revision window is due (marker file in the cache)."""
    marker = cache / "revised.json"
    if not marker.exists():
        return True
    last = dt.date.fromisoformat(json.loads(marker.read_text())["date"])
    return (today - last).days >= every_days


def _history(aoi: AOI, name: str, kind: str, points: list[tuple[float, float]], variables: list[str],
             extra: dict, start: dt.date, end: dt.date, months: int,
             revision_days: int = 0, revision_every_days: int = 7, grid_step: float | None = None) -> dict:
    """Fetch a history in chunks of ``months``, all points per request, and update its store.

    Chunks are kept under data/raw/openmeteo/<name>/<aoi> (compressed JSON), named by
    their first day and the last day fetched. A run only asks for the days not fetched yet,
    plus the last ``GEOMON_WEATHER_OVERLAP_DAYS`` (archives fill in with a delay), merged into
    the open chunk; an interrupted download resumes. The store keeps the hours already
    assembled: only the chunks fetched in the run are parsed and appended, and nothing is
    written when nothing changed.

    ``revision_days``: for a product whose provider replaces recent data later (ERA5-Land:
    preliminary data replaced by the final release 2-3 months on), the last ``revision_days``
    are fetched again every ``revision_every_days``, so that the corrections reach the store.
    """
    cache = settings.raw_dir / "openmeteo" / name / aoi.code
    cache.mkdir(parents=True, exist_ok=True)
    signature = {"points": [list(p) for p in points], "variables": variables, "extra": extra}
    sig_id = _signature_id(signature)
    today = dt.datetime.now(dt.timezone.utc).date()
    revise = revision_days > 0 and _revision_due(cache, revision_every_days, today)
    overlap = max(revision_days, settings.weather_overlap_days) if revise else settings.weather_overlap_days
    settled = today - dt.timedelta(days=overlap)       # days after it are fetched again
    if revise:
        log.info("  %s: fetching the last %d days again (provider revisions)", name, overlap)
    chunks = list(_months(start, end, months))
    cached = _cached_chunks(cache)
    store = _stored(name, sig_id)
    store_end = pd.Timestamp(store.time.values[-1]) if store is not None else None

    blocks: dict[int, list[dict]] = {}       # the chunks to parse: from the first one the store lacks
    fetched = 0
    for k, (t0, t1) in enumerate(chunks):
        target = cache / f"{t0:%Y-%m-%d}_{t1:%Y-%m-%d}.json.gz"
        path, have = cached.get(t0, (None, None))
        old = None
        if path is not None:
            written_today = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc).date() == today
            complete = have >= t1 and (have < settled or (written_today and not revise))
            if complete and not blocks and store_end is not None and store_end >= pd.Timestamp(t1) + pd.Timedelta(hours=23):
                continue                                  # already in the store: not even read
            stored = _load_chunk(path)
            old = _reuse(stored, signature)
            if old is not None and complete:
                if path != target or stored["signature"] != signature:
                    _save_chunk(target, signature, old)   # renamed, or compacted to the points in use
                blocks[k] = old
                continue
        # Fetch the days missing, and the last ones again (archives are revised for a few days)
        first = t0 if old is None else min(max(t0, min(have + dt.timedelta(days=1), settled)), t1)
        t = time.monotonic()
        data = _fetch(kind, points, variables, extra, first, t1)
        if old is not None and first > t0:
            data = _merge(old, data, first)
        _save_chunk(target, signature, data)
        fetched += 1
        blocks[k] = data
        log.info("  %s: %s to %s fetched in %.0f s (%d/%d)", name, first, t1, time.monotonic() - t, k + 1, len(chunks))
    # Files of earlier periods (a chunk renamed as it grew, or outside the period) are removed
    wanted = {f"{t0:%Y-%m-%d}_{t1:%Y-%m-%d}.json.gz" for t0, t1 in chunks}
    for p in cache.glob("*.json.gz"):
        if p.name not in wanted:
            p.unlink()
    if revise:
        (cache / "revised.json").write_text(json.dumps({"date": today.isoformat(), "days": overlap}))

    if not blocks:
        if store is not None:
            store.close()
        log.info("%s unchanged (up to %s)", name, str(store_end)[:16])
        return {"points": len(points), "start": start.isoformat(), "end": end.isoformat(),
                "chunks": len(chunks), "fetched_chunks": 0, "unchanged": True}

    models = str(extra.get("models", "")).split(",")
    models = models if len(models) > 1 else None
    first_k = min(blocks)
    frames, metas, unit_meta = [], [], []
    for i in range(len(points)):
        parts = []
        for k in sorted(blocks):
            f_, m = _parse(blocks[k][i], variables, models)
            parts.append(f_)
            unit_meta.append(m)
        frame = pd.concat(parts).sort_index()
        frames.append(frame[~frame.index.duplicated(keep="last")])
        last = blocks[max(blocks)][i]
        metas.append({"lon": last["longitude"], "lat": last["latitude"], "elevation": last.get("elevation", np.nan)})
    ds = _to_dataset(frames, metas, points, variables, {"meta": unit_meta})
    parsed = len(blocks)
    if store is not None:
        cut = np.datetime64(pd.Timestamp(chunks[first_k][0]))
        with store:
            kept = store.sel(time=store.time < cut).load()
        if kept.sizes["time"]:
            ds = _combine(kept, ds)
    ds.attrs = {
        "product": name, "source": PRODUCTS[name].source, "aoi": aoi.code, "variables": variables,
        "grid_step": grid_step,
        "start": start.isoformat(), "end": end.isoformat(), "signature": sig_id,
        "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "time_zone": "UTC", "license": "Open-Meteo data: CC BY 4.0",
    }
    write_product(ds, name)
    return {"points": len(points), "start": start.isoformat(), "end": end.isoformat(),
            "chunks": len(chunks), "fetched_chunks": fetched, "parsed_chunks": parsed, "hours": int(ds.sizes["time"])}


def run_history(aoi: AOI) -> dict:
    today = dt.datetime.now(dt.timezone.utc).date()
    start = settings.weather_start
    out = {}
    step, mf_points = weather_grid(aoi)
    out["om_mf_hist"] = _history(
        aoi, "om_mf_hist", "historical", mf_points, MF_VARS,
        {"models": "meteofrance_seamless"}, max(start, MF_ARCHIVE_START), today - dt.timedelta(days=1),
        months=1, grid_step=step)   # this archive answers slowly: one month for all points takes 1-3 min
    if settings.era5_source != "openmeteo":       # ERA5-Land and ERA5 come from the CDS (steps era5land, era5)
        return out
    # ERA5-Land is published with a ~5-day delay
    out["om_era5land"] = _history(
        aoi, "om_era5land", "archive", grid_points(aoi, 0.1), ERA5LAND_VARS,
        {"models": "era5_land,era5"}, start, today - dt.timedelta(days=6), months=12,
        revision_days=settings.era5_revision_days, revision_every_days=settings.era5_revision_every_days,
        grid_step=0.1)
    return out


def run_forecast(aoi: AOI) -> dict:
    """Latest forecast: Météo-France first, ECMWF IFS beyond its horizon."""
    models = ["meteofrance_seamless", "ecmwf_ifs"]
    step, points = weather_grid(aoi)
    run = dt.datetime.now(dt.timezone.utc)
    raw = _fetch("forecast", points, FORECAST_VARS, {"models": ",".join(models), "forecast_days": 16})
    frames, metas, unit_meta = [], [], []
    for r in raw:
        f, m = _parse(r, FORECAST_VARS, models)
        frames.append(f)
        unit_meta.append(m)
        metas.append({"lon": r["longitude"], "lat": r["latitude"], "elevation": r.get("elevation", np.nan)})
    ds = _to_dataset(frames, metas, points, FORECAST_VARS, {"meta": unit_meta})
    # Last hour with Météo-France temperature (the rest comes from ECMWF IFS)
    mf_end = None
    for r in raw:
        t = np.asarray(r["hourly"]["time"], dtype="int64")
        v = np.asarray(r["hourly"].get("temperature_2m_meteofrance_seamless", []), dtype="float64")
        if v.size and (~np.isnan(v)).any():
            mf_end = max(mf_end or 0, int(t[~np.isnan(v)][-1]))
    mf_until = pd.to_datetime(mf_end, unit="s").isoformat() if mf_end else None
    ds.attrs = {
        "product": "om_forecast", "source": PRODUCTS["om_forecast"].source, "aoi": aoi.code, "grid_step": step,
        "run": run.isoformat(timespec="minutes"), "updated": run.isoformat(timespec="seconds"),
        "models": json.dumps({"meteofrance_seamless": f"until {mf_until}", "ecmwf_ifs": "after, and soil/snow"}),
        "time_zone": "UTC", "license": "Open-Meteo data: CC BY 4.0",
    }
    write_product(ds, "om_forecast")
    # Keep every forecast run as a small raw archive, for later verification (Specifications §1)
    arch = settings.raw_dir / "openmeteo" / "forecast" / f"{aoi.code}_{run:%Y%m%dT%H%M}Z.json.gz"
    arch.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(arch, "wt", encoding="utf-8") as fh:
        json.dump({"run": run.isoformat(), "points": points, "responses": raw}, fh)
    return {"points": len(points), "run": ds.attrs["run"], "until": str(ds.time.values[-1])[:16],
            "meteofrance_until": mf_until}
