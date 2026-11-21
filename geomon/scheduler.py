"""Keeps the weather data up to date (Specifications §4.2, §6.5).

    python -m geomon.scheduler            # runs until stopped (compose service "scheduler")
    python -m geomon.scheduler --status   # last runs and next checks

Each source is fetched when it publishes, not more often: every dataset of geomon.datasets with a
``schedule`` is checked on it (see :func:`job_of`: a period, a daily time, a new forecast run or a
new satellite acquisition), and its steps read only what is new. ``--status`` lists the jobs
with their last runs.

Each site follows the mode chosen for each dataset on the page's Data tab (geomon.datasets): a
job only updates the sites where one of its datasets is in "update" or "live" mode; the static
datasets ("refresh-*" jobs) are refreshed only in "update" mode (by default they are frozen). The
actions asked from the page (update now, delete the stored data) are run by a thread of the
scheduler, one at a time.

The forecast job reads Open-Meteo's model metadata (a few hundred bytes per
model) and only downloads a forecast when one of the models has a new run.
The sentinel2 job searches the Copernicus Data Space STAC catalogue (no openEO
credits) and only runs openEO when a new scene below the cloud threshold covers
the AOI.
The sim2, agera5, era5land and era5 steps themselves skip the download when nothing changed.

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

from . import datasets
from .aoi import resolve_aoi, save_aoi
from .config import all_sites, settings, use_site
from .http import session
from .ingest.__main__ import run_step
from .ingest.lock import ingest_lock, lock_group

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


STAC = "https://stac.dataspace.copernicus.eu/v1/collections/{}/items"
STAC_ITEMS = STAC.format("sentinel-2-l2a")


def newest_scene(bbox: str, since: str, collection: str = "sentinel-2-l2a",
                 max_cloud: float | None = None) -> str | None:
    """Acquisition time of the newest scene of a STAC collection over ``bbox`` since ``since``;
    for Sentinel-2, whose tile cloud cover is at most GEOMON_S2_MAX_CLOUD (``max_cloud``; None:
    no cloud filter, as for the radar of Sentinel-1).

    The catalogue sorts and filters: one item is transferred. A server without the sort or filter
    extensions (HTTP 4xx) is paged through instead, every item read."""
    if collection == "sentinel-2-l2a" and max_cloud is None:
        max_cloud = settings.s2_max_cloud
    items = STAC.format(collection)

    def wanted(f: dict) -> bool:
        return max_cloud is None or (f["properties"].get("eo:cloud_cover") or 0) <= max_cloud

    params = {"bbox": bbox, "datetime": f"{since}/.."}
    query = {"limit": 1, "sortby": "-properties.datetime"}
    if max_cloud is not None:
        query |= {"filter": f"eo:cloud_cover<={max_cloud:g}", "filter-lang": "cql2-text"}
    r = session().get(items, timeout=60, params=params | query)
    if r.status_code < 400:
        feats = [f for f in r.json().get("features", []) if wanted(f)]
        return feats[0]["properties"]["datetime"] if feats else None
    if r.status_code >= 500:
        r.raise_for_status()
    log.debug("STAC search without sort/filter (%s): paging through the items", r.status_code)
    times: list[str] = []
    url, page_query = items, params | {"limit": 100}
    for _ in range(50):                    # at most 5,000 items
        r = session().get(url, params=page_query, timeout=60)
        r.raise_for_status()
        page = r.json()
        times += [f["properties"]["datetime"] for f in page.get("features", []) if wanted(f)]
        url = next((link["href"] for link in page.get("links", []) if link.get("rel") == "next"), None)
        if not url:
            break
        page_query = None                  # the next link carries the query
    return max(times) if times else None


def _site_bbox() -> str | None:
    from .db import connect

    with connect() as conn:
        row = conn.execute("""SELECT ST_XMin(bbox) x0, ST_YMin(bbox) y0, ST_XMax(bbox) x1, ST_YMax(bbox) y1
                              FROM meta.aoi WHERE kind = 'site' AND code = %s""", (settings.site_code,)).fetchone()
    return f"{row['x0']},{row['y0']},{row['x1']},{row['y1']}" if row else None


def _new_scene(collection: str):
    """Trigger: a newer acquisition of the collection over the current site (last 30 days)."""
    def due(st: dict, now: dt.datetime) -> bool:
        bbox = _site_bbox()
        since = (_now() - dt.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        marker = newest_scene(bbox, since, collection) if bbox else None
        st["pending_marker"] = marker
        if _parse(st.get("last_ok")) is None:
            return True
        return marker is not None and marker != st.get("marker")
    return due




def job_of(d: datasets.Dataset) -> Job:
    """The scheduler job of a dataset, from its ``schedule``; its steps decide themselves whether
    there is anything new to read (a new image, run or day)."""
    sc, check_every, per_site = d.schedule, dt.timedelta(minutes=1), False
    if isinstance(sc, datasets.Every):
        period = dt.timedelta(minutes=sc.minutes, hours=sc.hours, days=sc.days)
        text, due = f"every {_human(period)}", every(period)
    elif isinstance(sc, datasets.Daily):
        text, due = f"daily at {sc.hour:02d}:{sc.minute:02d} UTC", daily(sc.hour, sc.minute)
    elif isinstance(sc, datasets.NewRun):
        text, due, check_every = "each new model run (checked every 15 min)", new_forecast_run, dt.timedelta(minutes=15)
    elif isinstance(sc, datasets.NewScene):
        text, due, check_every, per_site = ("each new acquisition (STAC catalogue checked every 3 h for each site)",
                                            _new_scene(sc.collection), dt.timedelta(hours=3), True)
    else:
        raise ValueError(f"{d.code}: unknown schedule {sc!r}")
    return Job(d.scheduled_job(), list(d.steps), f"{d.label}, {text}", due, check_every, per_site)


def _human(period: dt.timedelta) -> str:
    m = int(period.total_seconds() // 60)
    return f"{m // 1440} days" if m % 1440 == 0 else f"{m // 60} h" if m % 60 == 0 else f"{m} min"


JOBS = [job_of(d) for d in datasets.DATASETS.values() if d.schedule] + [
    # Static datasets: refreshed only for the sites where they are in "update" mode
    Job(d.scheduled_job(), list(d.steps), f"{d.label}, every {d.refresh_days} days (update mode)",
        every(dt.timedelta(days=d.refresh_days)), dt.timedelta(minutes=10))
    for d in datasets.DATASETS.values() if d.refresh_days and not d.schedule
]


# ---------------------------------------------------------------------------
# State: {"jobs": {job: trigger state}, "sites": {site: {job: run state}}}
# ---------------------------------------------------------------------------
def _state_path():
    return settings.data_dir / "state" / "scheduler.json"


# The state is shared by the job threads: every change to it, and its serialisation, holds this
# lock (never during a download or a trigger check, which work on a copy).
_state_lock = threading.RLock()


def load_state() -> dict:
    p = _state_path()
    state = json.loads(p.read_text()) if p.exists() else {}
    state.setdefault("jobs", {})
    state.setdefault("sites", {})
    return state


def save_state(state: dict) -> None:
    with _state_lock:
        text = json.dumps(state, indent=1)
        p = _state_path()
        if p.exists() and p.read_text() == text:
            return                 # unchanged: most ticks of most jobs
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(f".{threading.get_ident()}.tmp")
        tmp.write_text(text)
        tmp.replace(p)


def _check(job: "Job", st: dict, now: dt.datetime) -> bool:
    """A job's trigger, evaluated on a copy of its state (it may read the network), merged back."""
    with _state_lock:
        work = dict(st)
    due = job.due(work, now)
    with _state_lock:
        st.update(work)
    return due


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
        with _state_lock:
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
    with _state_lock:
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
    now = _now()
    sites = []
    for s in all_sites():
        try:
            if datasets.scheduled(s.code, job.name):     # the modes chosen on the page's Data tab
                sites.append(s.code)
        except Exception as exc:
            log.warning("%s %s: dataset modes not read (%s)", s.code, job.name, exc)
    if not sites:
        return
    with _state_lock:
        js = state["jobs"].setdefault(job.name, {})
        site_states = {c: state["sites"].setdefault(c, {}).setdefault(job.name, {}) for c in sites}

    # Sites to update: all when the trigger fires; else new sites, and failed ones whose retry time came
    todo: list[str] = []
    checked = _parse(js.get("checked"))
    if not checked or now - checked >= job.check_every:
        with _state_lock:
            js["checked"] = _iso(now)
        if job.per_site:
            for c in sites:
                with use_site(c):
                    try:
                        if _check(job, site_states[c], now):
                            todo.append(c)
                    except Exception as exc:
                        log.warning("%s %s: could not check (%s)", c, job.name, exc)
        else:
            try:
                if _check(job, js, now):
                    todo = list(sites)
            except Exception as exc:
                log.warning("%s: could not check (%s)", job.name, exc)
    for c in sites:
        sst = site_states[c]
        retry = _parse(sst.get("retry_at"))
        if c not in todo and ((retry and now >= retry) or (not sst.get("last_ok") and not retry)):
            todo.append(c)
    if not todo:
        with _state_lock:
            js.pop("pending_marker", None)
        save_state(state)
        return

    log.info("Job %s: %s · %s", job.name, job.describe, ", ".join(todo))
    for c in todo:
        if _stop:
            return
        ok = _run_site(job, c, site_states[c])
        with _state_lock:
            if job.per_site and ok and "pending_marker" in site_states[c]:
                site_states[c]["marker"] = site_states[c].pop("pending_marker")
        save_state(state)
    if not job.per_site and set(todo) == set(sites):
        # The trigger moves on after a full run; a site that failed is retried alone (retry_at)
        with _state_lock:
            js["last_run"] = _iso(now)
            js["last_ok"] = _iso(_now())
            if "pending_marker" in js:
                js["marker"] = js.pop("pending_marker")
    save_state(state)


# ---------------------------------------------------------------------------
# Actions asked from the page (meta.dataset_request)
# ---------------------------------------------------------------------------
def run_request(req: dict) -> tuple[bool, str]:
    d = datasets.DATASETS.get(req["dataset"])
    if d is None:
        return False, f"unknown dataset {req['dataset']}"
    with use_site(req["site"]):
        if req["action"] == "delete":
            with ingest_lock(lock_group(d.steps[0])):
                r = datasets.delete(req["site"], d.code)
            return True, f"{len(r['removed'])} stores and {r['rows']} rows removed"
        aoi = _aoi(req["site"])
        failed = [step for step in d.steps if not run_step(step, aoi)]
        return (not failed, "failed: " + ", ".join(failed) if failed else "updated")


def request_loop(once: bool) -> None:
    try:
        datasets.reset_stale_requests()
    except Exception as exc:
        log.warning("Dataset requests not reset: %s", exc)
    while not _stop:
        try:
            req = datasets.next_request()
        except Exception as exc:
            log.warning("Dataset requests not read: %s", exc)
            req = None
        if req is None:
            if once:
                return
            _sleep(10)
            continue
        log.info("Request %s: %s %s for %s", req["id"], req["action"], req["dataset"], req["site"])
        try:
            ok, message = run_request(req)
        except Exception as exc:
            log.exception("Request %s failed", req["id"])
            ok, message = False, str(exc)
        datasets.finish_request(req["id"], ok, message)


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
    from .migrate import migrate
    migrate()                     # the dataset modes are read at the first tick, before any AOI is saved
    threads = [threading.Thread(target=job_loop, args=(job, state, args.once), name=job.name, daemon=True)
               for job in JOBS]
    threads.append(threading.Thread(target=request_loop, args=(args.once,), name="requests", daemon=True))
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads):
        for t in threads:
            t.join(timeout=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
