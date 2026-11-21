"""Steps ``arome``, ``pe-arome`` and ``sea``: the latest complete run of Météo-France's AROME,
PE-AROME and waves/surge models for the site (see geomon.mf_models).

Each run is read one forecast time and parameter per request, under the API's 50 requests a minute:
AROME 99 requests (hourly for a day, then 3-hourly; about 2.5 minutes per site), PE-AROME about 300 (25 members x 12, about 7
minutes, once for all the sites: see _member_read), waves and surge about 70 (1.5 minutes). Nothing is read when the newest complete run is the
one stored.
"""

from __future__ import annotations

import datetime as dt
import json
import logging

import numpy as np
import pandas as pd
import xarray as xr

from .. import mf_models as m
from ..aoi import AOI
from ..arrays import finite_list
from ..meteofrance import WCS, bbox, centres
from ..weather import LOCAL_TZ
from ..zarrio import read_attrs, write_atomic, write_bytes_atomic

log = logging.getLogger(__name__)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# AROME maps
# ---------------------------------------------------------------------------
def run_arome(aoi: AOI) -> dict:
    w = WCS(m.AROME_WCS)
    box = bbox(aoi.bbox_wgs84, m.AROME_MARGIN_KM)
    stored = read_attrs(m.arome_path(aoi.code)) or {}
    found = w.latest_complete(*m.AROME_RAIN, n=m.AROME_HOURS,
                              stop_at=stored.get("run_id") if stored.get("bbox") == list(box) else None)
    if found is None:
        return {"skipped": "no complete AROME run published"}
    rain_cov, offsets = found
    if rain_cov is None:
        return {"run": stored["run"][:16], "added": 0}
    # Hourly for the first day, then every 3 hours (the rain then from the 3-hour totals)
    hourly = [o for o in offsets if 0 < o <= m.AROME_HOURLY_H * 3600]
    later = [o for o in offsets if m.AROME_HOURLY_H * 3600 < o <= m.AROME_HOURS * 3600 and o % 10800 == 0]
    offsets = hourly + later
    run_id, run_time = w.run_id(rain_cov), w.run_time(rain_cov)
    rain, prof = w.read_stack(rain_cov, box, hourly)
    if later:
        rain3, _ = w.read_stack(f"{m.AROME_RAIN[0]}___{run_id}_PT3H", box, later, shape=rain.shape[1:])
        rain = np.concatenate([rain, rain3 / 3])        # mm/h: the mean rate of the 3 hours
    t2m, _ = w.read_stack(f"{m.AROME_T2M}___{run_id}", box, offsets, 2, rain.shape[1:])
    gust, _ = w.read_stack(f"{m.AROME_GUST}___{run_id}", box, offsets, 10, rain.shape[1:])
    x, y = centres(prof)
    ds = xr.Dataset(
        {"rain": (("time", "y", "x"), np.clip(rain, 0, None).astype("float32"),
                  {"units": "mm/h", "long_name": "Rain and snow rate: over the hour before each time for the first "
                                                 f"{m.AROME_HOURLY_H} hours, then the mean of the 3 hours before"}),
         "t2m": (("time", "y", "x"), t2m.astype("float32"), {"units": "degC", "long_name": "Temperature at 2 m"}),
         "gust": (("time", "y", "x"), gust.astype("float32"), {"units": "m/s", "long_name": "Gust at 10 m (the hour before each time)"})},
        coords={"time": run_time + np.array(offsets, dtype="timedelta64[s]"), "y": y, "x": x},
        attrs={"crs": "EPSG:4326", "run": str(run_time), "run_id": run_id, "bbox": list(box),
               "source": m.AROME_ATTRIBUTION, "updated": _now()})
    write_atomic(ds, m.arome_path(aoi.code))
    return {"run": str(run_time)[:16], "added": 1, "hours": len(offsets), "grid": [ds.sizes["y"], ds.sizes["x"]]}


# ---------------------------------------------------------------------------
# PE-AROME probabilities
# ---------------------------------------------------------------------------
def dawn_mornings(run_time: np.datetime64, offsets: list[int]) -> list[tuple[str, list[int]]]:
    """The local mornings whose dawn hours (DAWN_LOCAL_HOURS, ends of hours) are all forecast:
    [(local date, [offsets])]."""
    t0 = pd.Timestamp(run_time).tz_localize("UTC")
    by_day: dict[str, list[int]] = {}
    for o in sorted(set(offsets)):
        loc = (t0 + pd.Timedelta(seconds=o)).tz_convert(LOCAL_TZ)
        if loc.hour in m.DAWN_LOCAL_HOURS:
            by_day.setdefault(loc.strftime("%Y-%m-%d"), []).append(o)
    return [(d, o) for d, o in by_day.items() if len(o) == len(m.DAWN_LOCAL_HOURS)]


# The members are read over a box covering the nearby sites, the answers kept on disk by the WCS
# client (geomon.meteofrance.WCS.read): the sites after the first (the ensemble lock is global, so
# they take turns) find them there, and each takes its cells. Sites far apart are not grouped: a
# box over both would bring mostly cells no site needs.
SHARE_RATIO = 2.0               # grouped when the box over them is at most this times their own boxes


def group_box(boxes: dict[str, tuple], site: str) -> tuple[float, float, float, float]:
    """The box over the group of nearby sites holding ``site`` (sites merged while the box over two
    groups is at most SHARE_RATIO times the sum of theirs)."""
    def area(b):
        return (b[2] - b[0]) * (b[3] - b[1])

    def union(a, b):
        return min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])

    groups = [({code}, b, area(b)) for code, b in boxes.items()]       # (sites, box, sum of their areas)
    merged = True
    while merged:
        merged = False
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                u = union(groups[i][1], groups[j][1])
                if area(u) <= SHARE_RATIO * (groups[i][2] + groups[j][2]):
                    groups[i] = (groups[i][0] | groups[j][0], u, groups[i][2] + groups[j][2])
                    del groups[j]
                    merged = True
                    break
            if merged:
                break
    return next(g[1] for g in groups if site in g[0])


def _sites_box(site: str, box) -> tuple[float, float, float, float]:
    """The box read for ``site``: over its group of nearby configured sites (0.01° rounded, a few km
    around), or its own box when it is not in meta.aoi yet."""
    from ..config import all_sites
    from ..db import connect

    with connect() as conn:
        rows = conn.execute("""SELECT site, ST_XMin(e) x0, ST_YMin(e) y0, ST_XMax(e) x1, ST_YMax(e) y1
                               FROM (SELECT site, ST_Extent(geom) e FROM meta.aoi WHERE kind = 'site' AND site = ANY(%s)
                                     GROUP BY site) b""", ([s.code for s in all_sites()],)).fetchall()
    boxes = {r["site"]: (r["x0"], r["y0"], r["x1"], r["y1"]) for r in rows}
    return bbox(group_box(boxes, site) if site in boxes else box, 5)


def _cut(a: np.ndarray, profile: dict, box) -> np.ndarray:
    """The cells of a read whose centres fall in ``box`` (widened by half a cell)."""
    x, y = centres(profile)
    hx, hy = abs(profile["transform"].a) / 2, abs(profile["transform"].e) / 2
    cols = (x >= box[0] - hx) & (x <= box[2] + hx)
    rows = (y >= box[1] - hy) & (y <= box[3] + hy)
    return a[..., rows, :][..., cols]


def _member_read(k: int, coverage: str, offsets: list[int], height: int | None, box, union) -> np.ndarray:
    """Forecast times of a member's coverage over the site ``box``, read over ``union``: (time, y, x)."""
    stack, profile = WCS(m.PEAROME_WCS.format(member=k)).read_stack(coverage, union, offsets, height)
    return _cut(stack, profile, box)


def run_pearome(aoi: AOI) -> dict:
    w0 = WCS(m.PEAROME_WCS.format(member=0))
    stored = m.open_ensemble(aoi.code) or {}
    found = w0.latest_complete(*m.PEAROME_RAIN, n=28, stop_at=stored.get("run_id"))
    if found is None:
        return {"skipped": "no complete PE-AROME run published"}
    rain_cov, rain_offsets = found
    if rain_cov is None:
        return {"run": stored["run"][:16], "added": 0}
    run_id, run_time = w0.run_id(rain_cov), w0.run_time(rain_cov)
    windows = [o for o in (86400, 172800) if o in set(rain_offsets)]
    mornings = dawn_mornings(run_time, w0.offsets(f"{m.PEAROME_TMIN}___{run_id}"))
    box = aoi.bbox_wgs84
    union = _sites_box(aoi.code, box)
    rain = np.full((m.PEAROME_MEMBERS, len(windows)), np.nan)
    rain_max = np.full_like(rain, np.nan)
    tmin = np.full((m.PEAROME_MEMBERS, len(mornings)), np.nan)
    tmin_cold = np.full_like(tmin, np.nan)
    failed = []
    for k in range(m.PEAROME_MEMBERS):
        try:
            for j, a in enumerate(_member_read(k, f"{m.PEAROME_RAIN[0]}___{run_id}{m.PEAROME_RAIN[1]}", windows, None, box, union)):
                rain[k, j], rain_max[k, j] = np.nanmean(a), np.nanmax(a)
            for j, (_, offs) in enumerate(mornings):
                hours = _member_read(k, f"{m.PEAROME_TMIN}___{run_id}", offs, 2, box, union)
                tmin[k, j] = np.nanmin(np.nanmean(hours.reshape(len(offs), -1), axis=1))
                tmin_cold[k, j] = np.nanmin(hours)
        except Exception as e:             # a member missing: the probabilities use the others
            failed.append(k)
            log.warning("PE-AROME member %03d: %s", k, e)
    t0 = pd.Timestamp(run_time).tz_localize("UTC")
    out = {"run": str(run_time), "run_id": run_id, "updated": _now(), "source": m.PEAROME_ATTRIBUTION, "box": list(box),
           "members": m.PEAROME_MEMBERS - len(failed), "members_failed": failed,
           "rain": [{"start": str(t0 + pd.Timedelta(seconds=o - 86400))[:16], "end": str(t0 + pd.Timedelta(seconds=o))[:16],
                     "area_mean": m.probabilities(rain[:, j], m.RAIN_THRESHOLDS),
                     "wettest_cell": m.probabilities(rain_max[:, j], m.RAIN_THRESHOLDS),
                     "members": finite_list(rain[:, j], 1)}
                    for j, o in enumerate(windows)],
           "frost": [{"morning": d, "hours_local": list(m.DAWN_LOCAL_HOURS),
                      "area_mean": m.probabilities(tmin[:, j], m.FROST_THRESHOLDS, below=True),
                      "coldest_cell": m.probabilities(tmin_cold[:, j], m.FROST_THRESHOLDS, below=True),
                      "members": finite_list(tmin[:, j], 1)}
                     for j, (d, _) in enumerate(mornings)],
           "rain_thresholds_mm": list(m.RAIN_THRESHOLDS), "frost_thresholds_c": list(m.FROST_THRESHOLDS)}
    write_bytes_atomic(m.ensemble_path(aoi.code), json.dumps(out).encode())
    return {"run": str(run_time)[:16], "added": 1, "members": out["members"],
            "rain_p1mm": [r["area_mean"].get("probability", {}).get("1") for r in out["rain"]],
            "frost_p0": [f["area_mean"].get("probability", {}).get("0") for f in out["frost"]]}


# ---------------------------------------------------------------------------
# Waves and storm surge
# ---------------------------------------------------------------------------
def _sea_read(w: WCS, coverage: str, box, offset: int, lon0: float, lat0: float) -> tuple[float, list]:
    """The mean over the sea cells nearest to (lon0, lat0) of one forecast time, and those cells."""
    a, prof = w.read(coverage, box, offset)
    x, y = centres(prof)
    cells = m.nearest_sea_cells(a, x, y, lon0, lat0)
    if not cells:
        return np.nan, []
    return float(np.nanmean([a[r, c] for r, c, _ in cells])), [(float(x[c]), float(y[r]), km) for r, c, km in cells]


def _around(cells: list, pad: float) -> tuple[float, float, float, float]:
    lons, lats = [c[0] for c in cells], [c[1] for c in cells]
    return min(lons) - pad, min(lats) - pad, max(lons) + pad, max(lats) + pad


def _series(w: WCS, coverage: str, wide, offsets: list[int], lon0: float, lat0: float, pad: float):
    """Values of a coverage at the sea cells nearest the site, for each forecast time: the cells
    are found on a wide box at the first time, the later times read a small box around them."""
    first, cells = _sea_read(w, coverage, wide, offsets[0], lon0, lat0)
    if not cells:
        return [], []
    small = _around(cells, pad)
    return [first] + [_sea_read(w, coverage, small, o, lon0, lat0)[0] for o in offsets[1:]], cells


def _status(aoi: AOI, coastal: bool) -> None:
    write_bytes_atomic(m.sea_status_path(aoi.code), json.dumps({"coastal": coastal, "checked": _now()}).encode())


def run_sea(aoi: AOI) -> dict:
    """An inland site is recorded as such (sea_status.json): the dataset then needs "a site on the
    coast" (geomon.datasets) and is not run again."""
    lon0, lat0 = aoi.centre_wgs84
    wide = bbox(aoi.bbox_wgs84, m.SEA_MAX_KM + 10)
    waves = WCS(m.MFWAM_WCS)
    stored = read_attrs(m.sea_path(aoi.code)) or {}
    found = waves.latest_complete(m.WAVES["hs"][0], n=40, stop_at=stored.get("waves_run_id"))
    if found is None:
        return {"skipped": "no complete MFWAM run published"}
    hs_cov, offsets = found
    if hs_cov is None:
        return {"run": stored["waves_run"][:16], "added": 0}
    run_id, run_time = waves.run_id(hs_cov), waves.run_time(hs_cov)
    w_off = [o for o in offsets if o % (m.WAVES_STEP_H * 3600) == 0]
    hs, cells = _series(waves, hs_cov, wide, w_off, lon0, lat0, 0.03)
    if not cells:
        _status(aoi, False)
        return {"skipped": f"no sea cell within {m.SEA_MAX_KM} km: not a coastal site"}
    small = _around(cells, 0.03)
    data = {"hs": hs} | {k: [_sea_read(waves, f"{param}___{run_id}", small, o, lon0, lat0)[0] for o in w_off]
                         for k, (param, _, _) in m.WAVES.items() if k != "hs"}
    surge_w = WCS(m.HYCOM_WCS)
    s_found = surge_w.latest_complete(m.SURGE, n=48)
    surge, s_times, s_cells, s_run = [], [], [], None
    if s_found:
        s_cov, s_off = s_found
        s_run = surge_w.run_time(s_cov)
        surge, s_cells = _series(surge_w, s_cov, wide, s_off, lon0, lat0, 0.05)
        s_times = [s_run + np.timedelta64(o, "s") for o in s_off] if s_cells else []
    ds = xr.Dataset(
        {k: (("time",), np.array(data[k], dtype="float32"), {"units": u, "long_name": label}) for k, (_, u, label) in m.WAVES.items()}
        | {"surge": (("time_s",), np.array(surge, dtype="float32"), {"units": "m", "long_name": "Storm surge (sea level above the tide)"})},
        coords={"time": run_time + np.array(w_off, dtype="timedelta64[s]"),
                "time_s": np.array(s_times, dtype="datetime64[ns]")},
        attrs={"waves_run": str(run_time), "waves_run_id": run_id, "surge_run": str(s_run) if s_run is not None else "",
               "source": m.SEA_ATTRIBUTION, "wave_cells": json.dumps(cells), "surge_cells": json.dumps(s_cells),
               "updated": _now()})
    write_atomic(ds, m.sea_path(aoi.code))
    _status(aoi, True)
    return {"run": str(run_time)[:16], "added": 1, "wave_cell_km": cells[0][2], "hs_max": round(float(np.nanmax(hs)), 2),
            "surge_max": round(float(np.nanmax(surge)), 2) if len(surge) else None}
