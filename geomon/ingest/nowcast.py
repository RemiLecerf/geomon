"""Nowcast step ``arome-pi``: the latest complete AROME-PI run over the site (see geomon.nowcast).

A run is published progressively over about 40 minutes: the newest run whose 15-minute rain covers
the whole 6 hours is used, and nothing is read when it is the one stored. Reading a run costs one
request per forecast time and parameter (24 for the rain, 7 for the temperature, 6 for the gusts),
spaced to stay under the API's 50 requests a minute: about 45 seconds per site.
"""

from __future__ import annotations

import datetime as dt
import logging

import numpy as np
import xarray as xr

from .. import nowcast as nc
from ..aoi import AOI
from ..meteofrance import WCS, bbox, centres
from ..zarrio import read_attrs, write_atomic

log = logging.getLogger(__name__)


def run(aoi: AOI) -> dict:
    w = WCS(nc.WCS_PATH)
    box = bbox(aoi.bbox_wgs84, nc.MARGIN_KM)
    stored = read_attrs(nc.store_path(aoi.code)) or {}
    found = w.latest_complete(nc.RAIN, "_PT15M", n=nc.STEPS,
                              stop_at=stored.get("run_id") if stored.get("bbox") == list(box) else None)
    if found is None:
        return {"skipped": "no complete AROME-PI run published"}
    rain_cov, offsets = found
    if rain_cov is None:
        return {"run": stored.get("run", "")[:16], "added": 0}
    offsets = offsets[:nc.STEPS]
    run_id, run_time = w.run_id(rain_cov), w.run_time(rain_cov)

    rain, prof = w.read_stack(rain_cov, box, offsets)
    rain = np.clip(rain, 0, None)
    hours = [h * 3600 for h in range(nc.HORIZON_H + 1)]
    t_cov, g_cov = f"{nc.T2M}___{run_id}", f"{nc.GUST}___{run_id}_PT1H"
    t_have, g_have = set(w.offsets(t_cov)), set(w.offsets(g_cov))
    t_off = [o for o in hours if o in t_have]
    g_off = [o for o in hours[1:] if o in g_have]
    t2m, _ = w.read_stack(t_cov, box, t_off, 2, rain.shape[1:])
    gust, _ = w.read_stack(g_cov, box, g_off, 10, rain.shape[1:])
    x, y = centres(prof)
    ds = xr.Dataset(
        {"rain": (("time", "y", "x"), rain.astype("float32"), {"units": "mm", "long_name": "Rain and snow (water equivalent) in 15 minutes"}),
         "t2m": (("time_t", "y", "x"), t2m.astype("float32"), {"units": "degC", "long_name": "Temperature at 2 m"}),
         "gust": (("time_g", "y", "x"), gust.astype("float32"), {"units": "m/s", "long_name": "Maximum gust at 10 m over the hour"})},
        coords={"time": run_time + np.array(offsets, dtype="timedelta64[s]"),
                "time_t": run_time + np.array(t_off, dtype="timedelta64[s]"),
                "time_g": run_time + np.array(g_off, dtype="timedelta64[s]"), "y": y, "x": x},
        attrs={"crs": "EPSG:4326", "run": str(run_time), "run_id": run_id, "bbox": list(box), "source": nc.ATTRIBUTION,
               "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")})
    write_atomic(ds, nc.store_path(aoi.code))
    rain_max = float(np.nanmax(rain)) if np.isfinite(rain).any() else 0.0
    log.info("AROME-PI %s run %s: %d x %d cells, rain max %.1f mm in 15 min", aoi.code, run_time, ds.sizes["y"],
             ds.sizes["x"], rain_max)
    return {"run": str(run_time)[:16], "added": 1, "grid": [ds.sizes["y"], ds.sizes["x"]],
            "rain_max_mm_15min": round(rain_max, 2)}
