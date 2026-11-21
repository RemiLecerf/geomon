import datetime as dt
import json
import threading

import pytest

from geomon import config, scheduler as sch

UTC = dt.timezone.utc


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    p = tmp_path / "state" / "scheduler.json"
    monkeypatch.setattr(sch, "_state_path", lambda: p)
    return p


@pytest.fixture
def two_sites(monkeypatch):
    sites = [config.Site("a", "A", ("38259",)), config.Site("b", "B", ("38244",))]
    monkeypatch.setattr(sch, "all_sites", lambda: sites)
    monkeypatch.setattr(config, "all_sites", lambda: sites)
    return sites


def test_every_and_daily_triggers():
    now = dt.datetime(2026, 11, 19, 3, 0, tzinfo=UTC)
    every6 = sch.every(dt.timedelta(hours=6))
    assert every6({}, now)
    assert not every6({"last_ok": (now - dt.timedelta(hours=5)).isoformat()}, now)
    at2 = sch.daily(2)
    assert at2({"last_ok": "2026-11-18T02:30:00+00:00"}, now)          # today's 02:00 slot not done
    assert not at2({"last_ok": "2026-11-19T02:01:00+00:00"}, now)
    assert not at2({"last_ok": "2026-11-18T02:30:00+00:00"}, now.replace(hour=1))   # before the slot


def test_run_job_updates_every_site_then_moves_the_trigger(state_file, two_sites, monkeypatch):
    ran = []

    def fake_run_site(job, code, sst):
        ran.append(code)
        ok = code == "a"
        with sch._state_lock:
            sst["last_ok" if ok else "retry_at"] = sch._iso(sch._now())
        return ok

    monkeypatch.setattr(sch, "_run_site", fake_run_site)
    job = sch.Job("x", ["step"], "test", lambda st, now: st.setdefault("pending_marker", "m1") == "m1")
    state = {"jobs": {}, "sites": {}}
    sch.run_job(job, state)
    assert ran == ["a", "b"]
    assert state["jobs"]["x"]["marker"] == "m1" and "pending_marker" not in state["jobs"]["x"]
    saved = json.loads(state_file.read_text())
    assert saved["sites"]["b"]["x"]["retry_at"] and saved["sites"]["a"]["x"]["last_ok"]


def test_failed_site_is_retried_alone(state_file, two_sites, monkeypatch):
    ran = []
    monkeypatch.setattr(sch, "_run_site", lambda job, code, sst: ran.append(code) or True)
    past = (sch._now() - dt.timedelta(minutes=1)).isoformat()
    state = {"jobs": {"x": {"checked": sch._iso(sch._now())}},
             "sites": {"a": {"x": {"last_ok": past}}, "b": {"x": {"last_ok": past, "retry_at": past}}}}
    job = sch.Job("x", ["step"], "test", lambda st, now: True, check_every=dt.timedelta(hours=1))
    sch.run_job(job, state)
    assert ran == ["b"]


def test_state_is_saved_while_other_threads_change_it(state_file):
    """Every change holds the lock, so the state is never serialised half-changed."""
    state = {"jobs": {}, "sites": {}}
    stop = threading.Event()

    def churn(n):
        k = 0
        while not stop.is_set():
            with sch._state_lock:
                state["sites"].setdefault(f"s{n}", {})[f"job{k % 50}"] = {"k": k}
                state["sites"][f"s{n}"].pop(f"job{(k + 25) % 50}", None)
            k += 1

    threads = [threading.Thread(target=churn, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    try:
        for _ in range(200):
            sch.save_state(state)
            json.loads(state_file.read_text())
    finally:
        stop.set()
        for t in threads:
            t.join()


def test_unchanged_state_is_not_rewritten(state_file):
    sch.save_state({"jobs": {}, "sites": {}})
    before = state_file.stat().st_mtime_ns
    sch.save_state({"jobs": {}, "sites": {}})
    assert state_file.stat().st_mtime_ns == before


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def _item(t, cloud):
    return {"properties": {"datetime": t, "eo:cloud_cover": cloud}}


def test_newest_scene_sorted_search(monkeypatch):
    calls = []

    class S:
        def get(self, url, params=None, timeout=None):
            calls.append(params)
            return _Resp(200, {"features": [_item("2026-11-14T10:42:21Z", 17.6)]})

    monkeypatch.setattr(sch, "session", lambda: S())
    assert sch.newest_scene("1,2,3,4", "2026-10-20T00:00:00Z") == "2026-11-14T10:42:21Z"
    assert calls[0]["sortby"] == "-properties.datetime" and calls[0]["limit"] == 1
    assert calls[0]["filter"] == "eo:cloud_cover<=80"


def test_newest_sentinel1_acquisition_has_no_cloud_filter(monkeypatch):
    calls = []

    class S:
        def get(self, url, params=None, timeout=None):
            calls.append((url, params))
            return _Resp(200, {"features": [_item("2026-11-19T17:31:42Z", None)]})

    monkeypatch.setattr(sch, "session", lambda: S())
    assert sch.newest_scene("1,2,3,4", "2026-10-20T00:00:00Z", "sentinel-1-grd") == "2026-11-19T17:31:42Z"
    url, params = calls[0]
    assert "sentinel-1-grd" in url and "filter" not in params and params["sortby"] == "-properties.datetime"


def test_newest_scene_pages_through_without_sort_support(monkeypatch):
    pages = {None: {"features": [_item("2026-11-01T10:00:00Z", 10), _item("2026-11-17T10:00:00Z", 95)],
                    "links": [{"rel": "next", "href": "page2"}]},
             "page2": {"features": [_item("2026-11-09T10:00:00Z", 50)], "links": []}}

    class S:
        def get(self, url, params=None, timeout=None):
            if params and "sortby" in params:
                return _Resp(400, {"code": "unsupported"})
            return _Resp(200, pages[None if url == sch.STAC_ITEMS else url])

    monkeypatch.setattr(sch, "session", lambda: S())
    # The 17 November scene is too cloudy: the newest usable one is on page 2
    assert sch.newest_scene("1,2,3,4", "2026-10-20T00:00:00Z") == "2026-11-09T10:00:00Z"
