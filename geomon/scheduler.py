"""Keeps the weather data up to date (Specifications §4.2, §6.5).

    python -m geomon.scheduler            # runs until stopped (compose service "scheduler")
    python -m geomon.scheduler --status   # last runs and next checks

Each source is fetched when it publishes, not more often:

| Job       | Step(s)  | Published                                     | Checked                  |
|-----------|----------|-----------------------------------------------|--------------------------|
| forecast  | forecast | AROME every 3 h, ARPEGE and ECMWF every 6 h   | every 15 min (run times) |
| history   | weather  | Météo-France archive and ERA5: daily          | daily, 02:00 UTC         |
| sim2      | sim2     | Météo-France "latest" file: daily             | every 6 h                |
| agera5    | agera5   | Copernicus CDS: daily, ~10 days behind        | every 6 h                |
| sentinel2 | sentinel2| L2A scenes, ~5 h after acquisition           | every 3 h (STAC catalogue)|

The forecast job reads Open-Meteo's model metadata (a few hundred bytes per
model) and only downloads a forecast when one of the models has a new run.
The sentinel2 job searches the Copernicus Data Space STAC catalogue (no openEO
credits) and only runs openEO when a new scene below the cloud threshold covers
the AOI.
The sim2 and agera5 steps themselves skip the download when nothing changed.

One scheduler serves every site of sites.json: each dataset (job) has one
trigger, checked once for all sites (one look at the forecast models' metadata,
one daily slot...), and when it fires the dataset is updated for every site in
turn. Sentinel-2 is the exception: the STAC search is made for each site's
extent. Each job runs in its own thread, so a job waiting for a download (the
Open-Meteo daily quota, the CDS queue) does not hold up the others. A site whose
update failed is retried alone after 30 minutes; a site added to sites.json is
brought up to date at once.

The state (per job: last check, last run seen; per site and job: last run, last
success, retry time) is kept in data/state/scheduler.json, so a restart
does not repeat what was already done; a job whose daily time passed while the
scheduler was stopped runs at start.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import signal
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .aoi import resolve_aoi, save_aoi
from .config import all_sites, settings, use_site
from .http import session
from .ingest.__main__ import run_step

log = logging.getLogger("geomon.scheduler")

UTC = dt.timezone.utc
RETRY = dt.timedelta(minutes=30)
TICK_S = 60

# Models behind the forecast product (Open-Meteo metadata names)
FORECAST_MODELS = ["meteofrance_arome_france_hd", "meteofrance_arome_france0025",
                   "meteofrance_arpege_europe", "ecmwf_ifs"]


def _now() -> dt.datetime:
    return dt.datetime.now(UTC)


def _iso(t: dt.datetime | None) -> str | None:
    return t.isoformat(timespec="seconds") if t else None


def _parse(s: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(s) if s else None


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------
@dataclass
class Job:
    name: str
    steps: list[str]
    describe: str
    due: Callable[[dict, dt.datetime], bool]          # (job state, now) -> run now?
    check_every: dt.timedelta = dt.timedelta(minutes=1)
    per_site: bool = False                             # trigger checked for each site (Sentinel-2 extent)


def every(period: dt.timedelta) -> Callable[[dict, dt.datetime], bool]:
    def due(st: dict, now: dt.datetime) -> bool:
        last = _parse(st.get("last_ok"))
        return last is None or now - last >= period
    return due


def daily(hour: int, minute: int = 0) -> Callable[[dict, dt.datetime], bool]:
    def due(st: dict, now: dt.datetime) -> bool:
        slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now < slot:
            slot -= dt.timedelta(days=1)
        last = _parse(st.get("last_ok"))
        return last is None or last < slot
    return due


def latest_forecast_run() -> str | None:
    """Most recent availability time over the forecast models, from Open-Meteo metadata."""
    times = []
    for m in FORECAST_MODELS:
        try:
            r = session().get(f"https://api.open-meteo.com/data/{m}/static/meta.json", timeout=30)
            r.raise_for_status()
            t = r.json().get("last_run_availability_time")
            if t:
                times.append(int(t))
        except Exception as exc:
            log.debug("No metadata for %s: %s", m, exc)
    return dt.datetime.fromtimestamp(max(times), UTC).isoformat(timespec="seconds") if times else None


def new_forecast_run(st: dict, now: dt.datetime) -> bool:
    """A model has a new run and the minimum interval since the last download has passed (each
    download counts once per grid point at Open-Meteo). Without metadata, the interval decides."""
    last = _parse(st.get("last_ok"))
    if last is not None and now - last < dt.timedelta(hours=settings.forecast_min_hours):
        return False
    marker = latest_forecast_run()
    st["pending_marker"] = marker
    return last is None or marker is None or marker != st.get("marker")


def latest_s2_scene() -> str | None:
    """Acquisition time of the newest L2A scene over the AOI below the cloud threshold (STAC search)."""
    from .db import connect

    with connect() as conn:
        row = conn.execute("""SELECT ST_XMin(bbox) x0, ST_YMin(bbox) y0, ST_XMax(bbox) x1, ST_YMax(bbox) y1
                              FROM meta.aoi WHERE kind = 'site' AND code = %s""", (settings.site_code,)).fetchone()
    if not row:
        return None
    since = (_now() - dt.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = session().get(
        "https://stac.dataspace.copernicus.eu/v1/collections/sentinel-2-l2a/items",
        params={"bbox": f"{row['x0']},{row['y0']},{row['x1']},{row['y1']}", "datetime": f"{since}/..",
                "limit": 100},
        timeout=60,
    )
    r.raise_for_status()
    times = [f["properties"]["datetime"] for f in r.json().get("features", [])
             if (f["properties"].get("eo:cloud_cover") or 0) <= settings.s2_max_cloud]
    return max(times) if times else None


def new_s2_scene(st: dict, now: dt.datetime) -> bool:
    marker = latest_s2_scene()
    st["pending_marker"] = marker
    if _parse(st.get("last_ok")) is None:
        return True
    return marker is not None and marker != st.get("marker")


JOBS = [
    Job("forecast", ["forecast"], "new AROME/ARPEGE/ECMWF run (checked every 15 min)",
        new_forecast_run, dt.timedelta(minutes=15)),
    Job("history", ["weather"], "Météo-France archive and ERA5, daily at 02:00 UTC", daily(2, 0)),
    Job("sim2", ["sim2"], "SIM2 latest file, every 6 h", every(dt.timedelta(hours=6))),
    Job("agera5", ["agera5"], "AgERA5, every 6 h", every(dt.timedelta(hours=6))),
    Job("sentinel2", ["sentinel2"], "new Sentinel-2 scene (STAC catalogue checked every 3 h for each site)",
        new_s2_scene, dt.timedelta(hours=3), per_site=True),
]


# ---------------------------------------------------------------------------
# State: {"jobs": {job: trigger state}, "sites": {site: {job: run state}}}
# ---------------------------------------------------------------------------
def _state_path():
    return settings.data_dir / "state" / "scheduler.json"


_state_lock = threading.Lock()
_TRIGGER_KEYS = ("checked", "marker", "pending_marker", "last_ok", "last_run")


def load_state() -> dict:
    """The state, converted from the formats of earlier versions (one site; one thread per site)."""
    p = _state_path()
    raw = json.loads(p.read_text()) if p.exists() else {}
    if "jobs" in raw or "sites" in raw:
        raw.setdefault("jobs", {})
        raw.setdefault("sites", {})
        return raw
    names = {j.name for j in JOBS}
    by_site = {all_sites()[0].code: raw} if any(k in names for k in raw) else raw
    state = {"jobs": {}, "sites": {}}
    for code, jobs in by_site.items():
        state["sites"][code] = {n: {k: v for k, v in st.items() if k != "pending_marker"}
                                for n, st in jobs.items() if isinstance(st, dict)}
        for n, st in jobs.items():                 # trigger state: from the first site that has one
            if isinstance(st, dict) and n not in state["jobs"]:
                state["jobs"][n] = {k: st[k] for k in _TRIGGER_KEYS if k in st and k != "pending_marker"}
    return state


def save_state(state: dict) -> None:
    with _state_lock:
        p = _state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(f".{threading.get_ident()}.tmp")
        for _ in range(10):        # another job's thread may be changing its part: retry
            try:
                text = json.dumps(state, indent=1)
                break
            except RuntimeError:
                time.sleep(0.05)
        else:
            return
        tmp.write_text(text)
        tmp.replace(p)


# ---------------------------------------------------------------------------
# Loop
# ---------------------------------------------------------------------------
_stop = False


def _on_signal(signum, frame) -> None:
    global _stop
    log.info("Stopping after the current job")
    _stop = True


def _sleep(seconds: float) -> None:
    end = time.monotonic() + seconds
    while not _stop and time.monotonic() < end:
        time.sleep(min(5, end - time.monotonic()))


_aois: dict[str, tuple[float, object]] = {}
_aoi_lock = threading.Lock()


def _aoi(code: str):
    """The AOI of the current site (resolved from the IGN services, refreshed daily)."""
    with _aoi_lock:
        t0, aoi = _aois.get(code, (0.0, None))
        if aoi is None or time.monotonic() - t0 > 86400:
            try:
                aoi = resolve_aoi()
                save_aoi(aoi)
                _aois[code] = (time.monotonic(), aoi)
            except Exception as exc:
                if aoi is None:
                    raise
                log.warning("%s: AOI not refreshed (%s), keeping the previous one", code, exc)
        return aoi


def _run_site(job: Job, code: str, sst: dict) -> bool:
    """Runs a job's steps for one site; records the outcome in its site state."""
    with use_site(code):
        now = _now()
        sst["last_run"] = _iso(now)
        retry_at = None
        try:
            aoi = _aoi(code)
            ok = True
            for step in job.steps:
                result = run_step(step, aoi)
                if not result:
                    ok, retry_at = False, result.retry_at
                    break
        except Exception as exc:
            log.error("%s %s: %s", code, job.name, exc)
            ok = False
    if ok:
        sst["last_ok"] = _iso(_now())
        sst.pop("retry_at", None)
        sst.pop("error", None)
    else:
        sst["retry_at"] = _iso(max(retry_at, _now() + RETRY) if retry_at else _now() + RETRY)
        sst["error"] = _iso(_now())
        log.warning("Job %s failed for %s: retry at %s", job.name, code, sst["retry_at"])
    return ok


def run_job(job: Job, state: dict) -> None:
    """Checks a job's trigger once and updates the sites that need it."""
    js = state["jobs"].setdefault(job.name, {})
    now = _now()
    sites = [s.code for s in all_sites()]
    site_states = {c: state["sites"].setdefault(c, {}).setdefault(job.name, {}) for c in sites}

    # Sites to update: all when the trigger fires; else new sites, and failed ones whose retry time came
    todo: list[str] = []
    checked = _parse(js.get("checked"))
    if not checked or now - checked >= job.check_every:
        js["checked"] = _iso(now)
        if job.per_site:
            for c in sites:
                with use_site(c):
                    try:
                        if job.due(site_states[c], now):
                            todo.append(c)
                    except Exception as exc:
                        log.warning("%s %s: could not check (%s)", c, job.name, exc)
        else:
            try:
                if job.due(js, now):
                    todo = list(sites)
            except Exception as exc:
                log.warning("%s: could not check (%s)", job.name, exc)
    for c in sites:
        sst = site_states[c]
        retry = _parse(sst.get("retry_at"))
        if c not in todo and ((retry and now >= retry) or (not sst.get("last_ok") and not retry)):
            todo.append(c)
    if not todo:
        js.pop("pending_marker", None)
        save_state(state)
        return

    log.info("Job %s: %s · %s", job.name, job.describe, ", ".join(todo))
    results = {}
    for c in todo:
        if _stop:
            return
        results[c] = _run_site(job, c, site_states[c])
        if job.per_site and results[c] and "pending_marker" in site_states[c]:
            site_states[c]["marker"] = site_states[c].pop("pending_marker")
        save_state(state)
    if not job.per_site and set(todo) == set(sites):
        # The trigger moves on after a full run; a site that failed is retried alone (retry_at)
        js["last_run"] = _iso(now)
        js["last_ok"] = _iso(_now())
        if "pending_marker" in js:
            js["marker"] = js.pop("pending_marker")
    save_state(state)


def job_loop(job: Job, state: dict, once: bool) -> None:
    while not _stop:
        try:
            run_job(job, state)
        except Exception:
            log.exception("Job %s: scheduler error", job.name)
        if once:
            break
        _sleep(TICK_S)


def status() -> None:
    state = load_state()
    for job in JOBS:
        js = state["jobs"].get(job.name, {})
        print(f"{job.name:9s} {job.describe}")
        for k in ("checked", "last_ok", "marker"):
            if js.get(k):
                print(f"          {k:9s} {js[k]}")
        for site in all_sites():
            st = state["sites"].get(site.code, {}).get(job.name, {})
            line = " · ".join(f"{k} {st[k]}" for k in ("last_ok", "retry_at", "marker") if st.get(k))
            print(f"          {site.code}: {line or 'never run'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="geomon.scheduler")
    parser.add_argument("--status", action="store_true", help="print the last runs and exit")
    parser.add_argument("--once", action="store_true", help="run the due jobs once and exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    if args.status:
        status()
        return 0
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    log.info("Scheduler started: %s", "; ".join(f"{j.name} ({j.describe})" for j in JOBS))
    log.info("Sites: %s", ", ".join(s.code for s in all_sites()))
    state = load_state()
    save_state(state)
    threads = [threading.Thread(target=job_loop, args=(job, state, args.once), name=job.name, daemon=True)
               for job in JOBS]
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads):
        for t in threads:
            t.join(timeout=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
