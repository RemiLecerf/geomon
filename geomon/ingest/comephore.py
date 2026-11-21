"""COMEPHORE: Météo-France's hourly rainfall reanalysis at 1 km, radar merged with rain gauges, since
1997 (product ``comephore``, daily sums over UTC days).

Source: "Réanalyses Coméphore" (meteo.data.gouv.fr, Licence Ouverte, no account): one tar archive per
month (50 to 250 MB), holding for every hour three GeoTIFFs of all of France (1536 x 1536 km, polar
stereographic): RR (rain of the hour, 1/10 mm), its error and a quality code. Each image is a single
compressed block, so a window cannot be read alone: the archive is streamed once (one request) and
only the site's cells of the RR images are kept, summed over UTC days (a month's archive then holds
whole days). Nothing of the archive is stored.

The record (about 50 GB of archives for 1997-2026) is filled month by month, the most recent first,
within GEOMON_COMEPHORE_BUDGET_MIN minutes per run: the daily scheduled run carries on where the
previous one stopped. Archives are published about two months after their month. The store has every
day from GEOMON_COMEPHORE_START to the newest month published (NaN until read); each month read is
written into its place, so the record is never rewritten.

An archive is streamed once for all the sites: a store records its window (rows and columns of the
national image), and the month is also written for the other sites that follow COMEPHORE and lack it
(the comephore lock is global: the sites take turns).
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import tarfile
import time

import numpy as np
import pandas as pd
import xarray as xr

from ..aoi import AOI
from ..config import settings
from ..http import datagouv_resources, session
from ..weather import PRODUCTS
from ..zarrio import read_attrs, update_attrs, write_atomic

log = logging.getLogger(__name__)

DATASET = "669e23a7ce052a9e8521b75e"          # data.gouv.fr: Réanalyses Coméphore
NODATA = 65535
SCALE = 0.1                                    # mm per stored unit
MARGIN_M = 1000


def months_available() -> dict[str, str]:
    """{"YYYYMM": archive URL} of the published months."""
    out = {}
    for res in datagouv_resources(DATASET):
        m = re.search(r"H_COMEPHORE_(\d{6})\.tar$", res["url"])
        if m:
            out[m.group(1)] = res["url"]
    return dict(sorted(out.items()))


class _Window:
    """A site's cells on the COMEPHORE grid: rows and columns of the national image, from the site's
    box at the first image read, or as recorded in its store; their coordinates (``setup``)."""

    def __init__(self, bbox=None, rows=None, cols=None):
        self.bbox = bbox
        self.rows = slice(*rows) if rows else None
        self.cols = slice(*cols) if cols else None
        self.x = None

    def setup(self, src) -> None:
        from pyproj import Transformer
        from shapely.geometry import box
        from shapely.ops import transform

        t = src.transform
        if self.rows is None:
            to = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True).transform
            x0, y0, x1, y1 = transform(to, box(*self.bbox).segmentize(0.01)).buffer(MARGIN_M).bounds
            c0, c1 = int((x0 - t.c) // t.a), int((x1 - t.c) // t.a) + 1
            r0, r1 = int((y1 - t.f) // t.e), int((y0 - t.f) // t.e) + 1
            self.rows = slice(max(r0, 0), min(r1, src.height))
            self.cols = slice(max(c0, 0), min(c1, src.width))
        cols, rows = np.meshgrid(np.arange(self.cols.start, self.cols.stop), np.arange(self.rows.start, self.rows.stop))
        self.x = (t.c + (cols + 0.5) * t.a).ravel()
        self.y = (t.f + (rows + 0.5) * t.e).ravel()
        self.cell_m = float(abs(t.a))
        self.crs = src.crs.to_wkt()
        lon, lat = Transformer.from_crs(src.crs, "EPSG:4326", always_xy=True).transform(self.x, self.y)
        self.lon, self.lat = np.asarray(lon), np.asarray(lat)

    def slices(self) -> dict:
        return {"rows": [self.rows.start, self.rows.stop], "cols": [self.cols.start, self.cols.stop]}


def _month(url: str, windows: list[_Window]) -> list[tuple[np.ndarray, np.ndarray]]:
    """Daily rain (mm, UTC days) of each window's cells for one month: [(days, values[day, cell])]."""
    import rasterio

    sums: list[dict] = [{} for _ in windows]
    counts: dict[np.datetime64, int] = {}
    with session().get(url, stream=True, timeout=1800) as r:
        r.raise_for_status()
        r.raw.decode_content = True
        with tarfile.open(fileobj=r.raw, mode="r|") as tar:
            for member in tar:
                m = re.search(r"(\d{10})_RR\.gtif$", member.name)
                if not m or not member.isfile():
                    continue
                with rasterio.MemoryFile(tar.extractfile(member).read()) as mf, mf.open() as src:
                    for w in windows:
                        if w.x is None:
                            w.setup(src)
                    image = src.read(1)
                # The hour ending at HH UTC belongs to the UTC day of its start
                end = dt.datetime.strptime(m.group(1), "%Y%m%d%H")
                day = np.datetime64((end - dt.timedelta(hours=1)).date())
                counts[day] = counts.get(day, 0) + 1
                for w, s in zip(windows, sums):
                    a = image[w.rows, w.cols].astype("float32").ravel()
                    a[a == NODATA] = np.nan
                    s[day] = s.get(day, 0) + np.nan_to_num(a) * SCALE
    days = sorted(counts)
    out = []
    for s in sums:
        vals = np.stack([np.where(counts[d] >= 22, s[d], np.nan) for d in days]) if days else np.zeros((0, 0))
        out.append((np.array(days, dtype="datetime64[ns]"), vals.astype("float32")))
    return out


CHUNK_DAYS = 31


def day_axis(first_month: str, last_month: str) -> np.ndarray:
    """Every UTC day from the first day of ``first_month`` to the last of ``last_month`` (YYYYMM)."""
    end = (pd.Timestamp(f"{last_month}01") + pd.offsets.MonthEnd(0))
    return pd.date_range(f"{first_month}01", end, freq="D").values.astype("datetime64[ns]")


def store_of(site: str):
    return settings.site_dir(site) / "weather" / "comephore.zarr"


def _empty(days: np.ndarray, n: int) -> xr.Dataset:
    """NaN rain on ``days`` for ``n`` cells (lazily: the empty chunks are not written)."""
    import dask.array as da

    return xr.Dataset({"precip": (("time", "point"), da.full((len(days), n), np.nan, dtype="float32", chunks=(CHUNK_DAYS, n)),
                                  {"units": "mm", "day": "00 UTC to 24 UTC",
                                   "long_name": "Precipitation (COMEPHORE, radar and rain gauges)"})},
                      coords={"time": days})


def _prepare(path, site: str, win: _Window, axis: np.ndarray) -> None:
    """The store on the full day axis: created (empty) when missing, extended when newer months were
    published."""
    n = len(win.x)
    if not (path / "zarr.json").exists():
        ds = _empty(axis, n).assign_coords(
            point=np.arange(n), lon=("point", win.lon.astype("float64")), lat=("point", win.lat.astype("float64")),
            x_proj=("point", win.x.astype("float64")), y_proj=("point", win.y.astype("float64")))
        ds.attrs = {"product": "comephore", "source": PRODUCTS["comephore"].source, "aoi": site, "crs": win.crs,
                    "cell_m": win.cell_m, "months": "", **win.slices(), "start": str(axis[0])[:10],
                    "end": str(axis[-1])[:10], "license": "Licence Ouverte Etalab 2.0, Météo-France"}
        write_atomic(ds, path, encoding={"precip": {"chunks": (CHUNK_DAYS, n)}})
        return
    stored = xr.open_zarr(path, consolidated=True)
    if stored.sizes["point"] != n:
        raise RuntimeError(f"COMEPHORE {site}: the site's grid changed; delete the comephore store to start again")
    last = stored["time"].values[-1]
    if axis[-1] > last:
        _empty(axis[axis > last], n).to_zarr(path, append_dim="time", consolidated=True)


def _write_month(path, ym: str, days: np.ndarray, vals: np.ndarray) -> None:
    """A month's days written into their place on the store's axis."""
    inside = pd.DatetimeIndex(days).strftime("%Y%m") == ym      # the hour before the month belongs to the previous one
    days, vals = days[inside], vals[inside]
    if not len(days):
        return
    axis = xr.open_zarr(path, consolidated=True)["time"].values
    i = np.searchsorted(axis, days)
    if i[-1] - i[0] + 1 != len(i) or not np.array_equal(axis[i], days):
        raise RuntimeError(f"COMEPHORE {ym}: days not on the store's axis")
    xr.Dataset({"precip": (("time", "point"), vals)}).to_zarr(
        path, region={"time": slice(int(i[0]), int(i[-1]) + 1)}, mode="r+", consolidated=False)


def _other_sites(site: str) -> list[dict]:
    """The other sites that follow COMEPHORE and whose store records its window."""
    from .. import datasets
    from ..config import all_sites

    out = []
    for s in all_sites():
        attrs = read_attrs(store_of(s.code)) if s.code != site else None
        if attrs and attrs.get("rows") and datasets.policy(s.code, "comephore")["mode"] in datasets.ACTIVE:
            out.append({"site": s.code, "path": store_of(s.code), "win": _Window(rows=attrs["rows"], cols=attrs["cols"]),
                        "done": set(attrs.get("months", "").split()), "added": []})
    return out


def run(aoi: AOI) -> dict:
    """Streams the months not stored yet, the most recent first, within the time budget; each month
    is written into its place on the store's day axis (the record is never rewritten), for this site
    and the other sites lacking it."""
    budget = settings.comephore_budget_min * 60
    start = settings.comephore_start.strftime("%Y%m")
    avail = {k: v for k, v in months_available().items() if k >= start}
    path = store_of(aoi.code)
    attrs = read_attrs(path) or {}
    done = set(attrs.get("months", "").split())
    todo = sorted((k for k in avail if k not in done), reverse=True)     # the most recent first
    if not todo:
        return {"months": len(done), "added": 0, "remaining": 0}
    axis = day_axis(start, max(avail))
    win = _Window(aoi.bbox_wgs84, attrs.get("rows"), attrs.get("cols"))
    others = _other_sites(aoi.code)
    t0 = time.monotonic()
    added = []
    for ym in todo:
        if time.monotonic() - t0 > budget:
            break
        targets = [o for o in others if ym not in o["done"]]
        results = _month(avail[ym], [win] + [o["win"] for o in targets])
        if not added:                      # the windows are set by the first image: the store now
            _prepare(path, aoi.code, win, axis)
        for o in targets:
            if not o["added"]:
                _prepare(o["path"], o["site"], o["win"], axis)
        for (days, vals), p in zip(results, [path] + [o["path"] for o in targets]):
            _write_month(p, ym, days, vals)
        for o in targets:
            o["added"].append(ym)
        added.append(ym)
        log.info("COMEPHORE %s %s: %d days, %.1f mm on average (also for %d other site(s))", aoi.code, ym,
                 len(results[0][0]), float(np.nanmean(np.nansum(results[0][1], axis=0))) if results[0][1].size else 0.0,
                 len(targets))
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    months = sorted(done | set(added))
    if added:
        update_attrs(path, months=" ".join(months), end=str(axis[-1])[:10], updated=now, **win.slices())
    for o in others:
        if o["added"]:
            update_attrs(o["path"], months=" ".join(sorted(o["done"] | set(o["added"]))), end=str(axis[-1])[:10], updated=now)
    return {"months": len(months), "added": len(added), "remaining": len(todo) - len(added),
            "first": months[0] if months else None, "last": months[-1] if months else None,
            "also_for": {o["site"]: len(o["added"]) for o in others if o["added"]}}
