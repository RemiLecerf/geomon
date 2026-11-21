"""Radar rain (see geomon.radar): EUMETNET OPERA (step ``radar``) and Météo-France (``radar-mf``).

OPERA: the composites are listed in the 24-hour rolling cache (S3, anonymous; the first of
GEOMON_OPERA_BUCKETS holding the most recent image is used), and every image of the kept hours not
stored yet is read: only the blocks of the Cloud-Optimised GeoTIFF holding the site's window. The
first run fills the last 24 hours (or the kept hours) at once.

Météo-France: the API only serves the latest image of all of France (about 2 MB): it is downloaded
once per 5 minutes for all the sites (data/raw/radar), each site keeps its window. The file's
layout (ODIM HDF5 groups what / where / datasetN) is not documented by the API: the rain grid and
its georeferencing are looked up from the attributes, and the layout found is logged once.
Authentication and rate limits: see geomon.meteofrance.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import xarray as xr

from .. import datasets, meteofrance
from .. import radar
from ..aoi import AOI
from ..config import settings
from ..http import session
from ..zarrio import write_atomic, write_bytes_atomic
from . import sentinel2 as ing2

log = logging.getLogger(__name__)

READERS = 8
GDAL_ENV = dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tiff,.tif",
                GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES", GDAL_HTTP_MAX_RETRY="3", GDAL_HTTP_RETRY_DELAY="2",
                VSI_CACHE="FALSE")


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
def merge(old: xr.Dataset | None, new: xr.Dataset, keep_hours: int | None) -> xr.Dataset:
    """The stored images plus the new ones (on another grid: the old ones dropped), the oldest
    beyond ``keep_hours`` before the latest removed (None: all kept)."""
    if old is not None and (old.sizes["x"] != new.sizes["x"] or old.sizes["y"] != new.sizes["y"]
                            or not np.allclose(old["x"].values, new["x"].values)
                            or not np.allclose(old["y"].values, new["y"].values)):
        old = None
    if old is not None:
        old = old[["rain"]]
        ds = xr.concat([old.drop_sel(time=new["time"].values, errors="ignore"), new[["rain"]]], dim="time")
    else:
        ds = new[["rain"]]
    ds = ds.sortby("time")
    if keep_hours:
        ds = ds.sel(time=ds["time"] > ds["time"].values.max() - np.timedelta64(keep_hours, "h"))
    ds.attrs = dict(new.attrs)
    return ds


def _write(ds: xr.Dataset, source: str, site: str) -> None:
    ds.attrs["updated"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    ny, nx = ds.sizes["y"], ds.sizes["x"]
    write_atomic(ds.chunk({"time": 12, "y": -1, "x": -1}), radar.store_path(source, site),
                 encoding={"rain": {"chunks": (12, ny, nx)}})


def _window_bounds(aoi: AOI, crs, margin_m: float) -> tuple[float, float, float, float]:
    from pyproj import Transformer
    from shapely.geometry import shape
    from shapely.ops import transform

    to = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform
    return transform(to, shape(ing2._extent(aoi)).segmentize(0.01)).buffer(margin_m).bounds


def _keep(site: str, dataset: str) -> tuple[dict, int | None]:
    pol = datasets.policy(site, dataset)
    return pol, (pol["keep_hours"] if pol["mode"] == "live" else None)


def _summary(ds: xr.Dataset, added: int) -> dict:
    latest = radar.rain_mm(ds["rain"].values[-1])
    return {"latest": str(ds["time"].values[-1])[:16], "added": added, "images": int(ds.sizes["time"]),
            "grid": [ds.sizes["y"], ds.sizes["x"]], "raining_share_now": round(float((latest > 0).mean()), 3)}


# ---------------------------------------------------------------------------
# EUMETNET OPERA
# ---------------------------------------------------------------------------
_KEY = re.compile(r"OPERA@(\d{8}T\d{4})@0@DBZH\.tiff$")


def _list_day(bucket: str, day: dt.date) -> list[tuple[np.datetime64, str]]:
    """(time, URL) of the reflectivity composites of a day in a bucket."""
    out, token = [], None
    prefix = f"{day:%Y/%m/%d}/OPERA/COMP/"
    for _ in range(20):
        params = {"list-type": 2, "prefix": prefix, "max-keys": 1000}
        if token:
            params["continuation-token"] = token
        r = session().get(f"{bucket}/", params=params, timeout=60)
        r.raise_for_status()
        for key in re.findall(r"<Key>([^<]+)</Key>", r.text):
            m = _KEY.search(key)
            if m:
                t = np.datetime64(dt.datetime.strptime(m.group(1), "%Y%m%dT%H%M"), "ns")
                out.append((t, f"{bucket}/{key}"))
        nxt = re.search(r"<NextContinuationToken>([^<]+)</NextContinuationToken>", r.text)
        if "<IsTruncated>true</IsTruncated>" not in r.text or not nxt:
            break
        token = nxt.group(1)
    return out


def opera_images(hours: float) -> list[tuple[np.datetime64, str]]:
    """The reflectivity composites of the last ``hours`` (at most the 24 h of the cache), from the
    bucket holding the most recent one."""
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    days = sorted({(now - dt.timedelta(hours=h)).date() for h in (0, min(hours, 24))})
    best: list = []
    for bucket in settings.opera_buckets:
        try:
            items = [it for d in days for it in _list_day(bucket, d)]
        except Exception as exc:
            log.warning("OPERA cache %s not listed: %s", bucket, exc)
            continue
        if items and (not best or max(items)[0] > max(best)[0]):
            best = items
    since = np.datetime64(now - dt.timedelta(hours=hours), "ns")
    return sorted(it for it in best if it[0] > since)


def _read_opera(url: str, bounds) -> tuple[np.ndarray, np.ndarray, np.ndarray, str]:
    import rasterio
    from rasterio.windows import from_bounds

    with rasterio.Env(**GDAL_ENV), rasterio.open(f"/vsicurl/{url}") as src:
        win = from_bounds(*bounds, transform=src.transform).round_offsets().round_lengths()
        win = win.intersection(rasterio.windows.Window(0, 0, src.width, src.height))
        dbz = src.read(1, window=win)
        t = src.window_transform(win)
        x = t.c + (np.arange(dbz.shape[1]) + 0.5) * t.a
        y = t.f + (np.arange(dbz.shape[0]) + 0.5) * t.e
        return radar.dbz_to_stored(dbz), x, y, src.crs.to_wkt()


_OPERA_CRS = "+proj=laea +lat_0=55 +lon_0=10 +x_0=1950000 +y_0=-2100000 +ellps=WGS84 +units=m +no_defs"


def run_opera(aoi: AOI) -> dict:
    src = radar.SOURCES["opera"]
    pol, keep = _keep(aoi.code, src.dataset)
    old = radar.open_store("opera", aoi.code)
    have = set() if old is None else set(old["time"].values)
    images = [it for it in opera_images(keep or 24) if it[0] not in have]
    if not images:
        return {"added": 0, "images": 0 if old is None else int(old.sizes["time"])}
    bounds = _window_bounds(aoi, _OPERA_CRS, src.margin_m)
    frames, failed = [], 0
    with ThreadPoolExecutor(max_workers=READERS) as pool:
        futures = {t: pool.submit(_read_opera, url, bounds) for t, url in images}
        for t, fut in futures.items():
            try:
                frames.append((t, *fut.result()))
            except Exception as exc:
                failed += 1
                log.warning("OPERA %s not read: %s", str(t)[:16], exc)
    if not frames:
        raise RuntimeError(f"no OPERA image read ({failed} failed)")
    _, _, x, y, crs = frames[0]
    new = xr.Dataset({"rain": (("time", "y", "x"), np.stack([f[1] for f in frames]))},
                     coords={"time": [f[0] for f in frames], "y": y, "x": x},
                     attrs={"crs": crs, "source": src.attribution, "units": "1/100 mm in 5 minutes",
                            "nodata": radar.NODATA, "step_min": radar.STEP_MIN})
    ds = merge(old, new, keep)
    _write(ds, "opera", aoi.code)
    log.info("OPERA %s: %d images added (%d failed)", aoi.code, len(frames), failed)
    return _summary(ds, len(frames)) | {"failed": failed}


# ---------------------------------------------------------------------------
# Météo-France
# ---------------------------------------------------------------------------
PRODUCT = meteofrance.PUBLIC + "/public/DPRadar/v1/mosaiques/METROPOLE/observations/LAME_D_EAU/produit"
REUSE_S = 240                 # a download serves the sites updated within 4 minutes

_lock = threading.Lock()
_logged_layout = False


def latest_file():
    """The latest Météo-France image (downloaded at most once per REUSE_S seconds)."""
    path = settings.raw_dir / "radar" / "latest.h5"
    with _lock:
        if path.exists() and time.time() - path.stat().st_mtime < REUSE_S:
            return path
        r = meteofrance.get(PRODUCT, {"maille": 500})
        r.raise_for_status()
        content = r.content
        if content[:2] == b"\x1f\x8b":                  # gzipped
            import gzip
            content = gzip.decompress(content)
        write_bytes_atomic(path, content)
        log.info("Météo-France radar: latest image downloaded (%.1f MB)", len(r.content) / 1e6)
        return path


def _attrs(h, path: str) -> dict:
    if path not in h:
        return {}
    out = {}
    for k, v in h[path].attrs.items():
        if isinstance(v, bytes):
            v = v.decode()
        elif isinstance(v, np.ndarray) and v.size == 1:
            v = v.item()
            v = v.decode() if isinstance(v, bytes) else v
        out[k] = v
    return out


def _layout(h) -> list[tuple[str, tuple, str, dict]]:
    """Every 2-D array of the file: (path, shape, dtype, attributes of the data group's 'what')."""
    found = []

    def visit(name, obj):
        if hasattr(obj, "shape") and len(obj.shape) == 2 and min(obj.shape) > 100:
            group = name.rsplit("/", 1)[0] if "/" in name else ""
            found.append((name, obj.shape, str(obj.dtype), _attrs(h, f"{group}/what" if group else "what")))
    h.visititems(visit)
    return found


def _time(what: dict, fallback: dict) -> np.datetime64:
    for d in (what, fallback):
        date, hhmm = d.get("enddate") or d.get("date"), d.get("endtime") or d.get("time")
        if date and hhmm:
            return np.datetime64(dt.datetime.strptime(f"{date}{str(hhmm)[:4]}", "%Y%m%d%H%M"), "ns")
    raise ValueError("no date in the radar file")


def read_mosaic(path) -> dict:
    """The rain grid of a Météo-France radar file: values in 1/100 mm (65535 no data), projection,
    cell-centre coordinates and time."""
    import h5py
    from pyproj import CRS, Transformer

    global _logged_layout
    with h5py.File(path, "r") as h:
        arrays = _layout(h)
        if not _logged_layout:
            log.info("Météo-France radar file layout: %s; root where: %s",
                     [(p, s, t, w.get("quantity")) for p, s, t, w in arrays], _attrs(h, "where"))
            _logged_layout = True
        rain = next((a for a in arrays if str(a[3].get("quantity", "")).upper() in ("ACRR", "RATE", "LAME_D_EAU")), None)
        rain = rain or next((a for a in arrays if a[2] == "uint16"), None) or (arrays[0] if arrays else None)
        if rain is None:
            raise ValueError("no 2-D array in the radar file")
        raw = h[rain[0]][()]
        what = rain[3]
        group = rain[0].rsplit("/", 2)[0]
        where = _attrs(h, "where") or _attrs(h, f"{group}/where")
        t = _time(_attrs(h, f"{group}/what"), _attrs(h, "what"))
    gain, offset = float(what.get("gain", 1.0) or 1.0), float(what.get("offset", 0.0) or 0.0)
    nodata = what.get("nodata", radar.NODATA)
    if gain != 1.0 or offset != 0.0:                  # stored as hundredths of mm
        mm = raw.astype("float64") * gain + offset
        out = np.where(raw == nodata, radar.NODATA, np.clip(np.round(mm / radar.SCALE), 0, 65534)).astype("uint16")
    else:
        out = np.where(raw == nodata, radar.NODATA, raw).astype("uint16")
    projdef = where.get("projdef")
    if not projdef:
        raise ValueError(f"no projection in the radar file (where: {where})")
    crs = CRS.from_proj4(projdef) if projdef.strip().startswith("+") else CRS.from_user_input(projdef)
    ny, nx = out.shape
    xs, ys = float(where["xscale"]), float(where["yscale"])
    to = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    x0, y0 = to.transform(float(where["UL_lon"]), float(where["UL_lat"]))   # upper-left corner
    x = x0 + (np.arange(nx) + 0.5) * xs
    y = y0 - (np.arange(ny) + 0.5) * ys
    return {"rain": out, "crs": crs.to_wkt(), "x": x, "y": y, "time": t}


def site_window(m: dict, aoi: AOI, margin_m: float) -> xr.Dataset:
    """The site's window (outline plus ``margin_m``) of a mosaic, as a one-image Dataset."""
    x0, y0, x1, y1 = _window_bounds(aoi, m["crs"], margin_m)
    cols = np.nonzero((m["x"] >= x0) & (m["x"] <= x1))[0]
    rows = np.nonzero((m["y"] >= y0) & (m["y"] <= y1))[0]
    if not cols.size or not rows.size:
        raise ValueError("the site is outside the radar mosaic")
    r, c = slice(rows[0], rows[-1] + 1), slice(cols[0], cols[-1] + 1)
    return xr.Dataset({"rain": (("time", "y", "x"), m["rain"][None, r, c])},
                      coords={"time": [m["time"]], "y": m["y"][r], "x": m["x"][c]}, attrs={"crs": m["crs"]})


def run_mf(aoi: AOI) -> dict:
    src = radar.SOURCES["mf"]
    _, keep = _keep(aoi.code, src.dataset)
    m = read_mosaic(latest_file())
    new = site_window(m, aoi, src.margin_m)
    new.attrs |= {"source": src.attribution, "units": "1/100 mm in 5 minutes", "nodata": radar.NODATA,
                  "step_min": radar.STEP_MIN}
    ds = merge(radar.open_store("mf", aoi.code), new, keep)
    _write(ds, "mf", aoi.code)
    return _summary(ds, 1)
