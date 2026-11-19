"""Daily SAFRAN-ISBA reanalysis (SIM2) from Météo-France open data (Specifications §4.2).

Source: "Données changement climatique - SIM quotidienne" (data.gouv.fr),
8 km grid in Lambert II étendu, daily since 1958. One national file per year
(~135 MB compressed) plus a ``latest`` file with the recent months, updated
daily. Kept from 1990 (``GEOMON_SIM2_START``): the 1991-2020 normals are
computed from it.

The national files are read as a stream and never stored: only the rows of the
grid cells touching the AOI are kept, under data/raw/sim2 (a few hundred kB).
A past year is downloaded once. The current year is only downloaded again when
the ``latest`` file no longer joins up with what is stored.
"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import io
import json
import logging
import shutil

import numpy as np
import pandas as pd
import xarray as xr
from pyproj import Transformer

from ..aoi import AOI
from ..config import all_sites, settings
from ..http import session
from ..weather import PRODUCTS, clear_cache, store_path

log = logging.getLogger(__name__)

HALF_CELL_M = 4000.0

# SIM2 column -> (geomon daily name, units, scale, day definition)
# Units from "SIM_QUOTIDIENNE_parametres" (Météo-France, 2026-03-31).
COLUMNS = {
    "T": ("t_mean", "degC", 1, "mean ]00UTC-00UTC]"),
    "TINF_H": ("t_min", "degC", 1, "minimum of the 24 hourly values ]18UTC-18UTC]"),
    "TSUP_H": ("t_max", "degC", 1, "maximum of the 24 hourly values ]06UTC-06UTC]"),
    "PRELIQ": ("rain", "mm", 1, "sum ]06UTC-06UTC]"),
    "PRENEI": ("snowfall_we", "mm", 1, "sum ]06UTC-06UTC]"),
    "SSI": ("radiation", "MJ/m**2", 0.01, "sum ]00UTC-00UTC], source unit J/cm2"),
    "DLI": ("longwave_down", "MJ/m**2", 0.01, "sum ]00UTC-00UTC], source unit J/cm2"),
    "ETP": ("et0", "mm", 1, "FAO-56 Penman-Monteith"),
    "EVAP": ("evap", "mm", 1, "sum ]06UTC-06UTC]"),
    "HU": ("rh_mean", "percent", 1, "mean ]00UTC-00UTC]"),
    "Q": ("q_mean", "g/kg", 1, "mean ]00UTC-00UTC]"),
    "FF": ("wind_mean", "m/s", 1, "mean ]00UTC-00UTC]"),
    "PE": ("effective_rain", "mm", 1, "sum ]06UTC-06UTC]"),
    "SWI": ("swi", "dimensionless", 1, "mean [06UTC-06UTC]; documented as %, values are a ratio"),
    "DRAINC": ("drainage", "mm", 1, "sum ]06UTC-06UTC]"),
    "RUNC": ("runoff", "mm", 1, "sum ]06UTC-06UTC]"),
    "RESR_NEIGE": ("swe", "mm", 1, "mean [06UTC-06UTC]"),
    "HTEURNEIGE": ("snow_depth", "m", 1, "mean [06UTC-06UTC]"),
    "WG_RACINE": ("wg_root", "m**3/m**3", 1, "at 06 UTC"),
}


def _cells(aoi: AOI) -> pd.DataFrame:
    """SAFRAN cells (LAMBX, LAMBY in hm) whose 8 km square touches the AOI extent."""
    url = f"{settings.sim2_base_url}/coordonnees_grille_safran_lambert-2-etendu.csv"
    cache = settings.raw_dir / "sim2" / "coordonnees_grille_safran_lambert-2-etendu.csv"
    cache.parent.mkdir(parents=True, exist_ok=True)
    if not cache.exists():
        r = session().get(url, timeout=120)
        r.raise_for_status()
        cache.write_bytes(r.content)
    grid = pd.read_csv(cache, sep=";", decimal=",")
    grid.columns = ["lambx", "lamby", "lat", "lon"]
    to_l2 = Transformer.from_crs("EPSG:4326", "EPSG:27572", always_xy=True).transform
    x0, y0, x1, y1 = aoi.bbox_wgs84
    xs, ys = zip(*(to_l2(x, y) for x, y in ((x0, y0), (x0, y1), (x1, y0), (x1, y1))))
    cx, cy = grid.lambx * 100.0, grid.lamby * 100.0
    sel = grid[(cx + HALF_CELL_M > min(xs)) & (cx - HALF_CELL_M < max(xs))
               & (cy + HALF_CELL_M > min(ys)) & (cy - HALF_CELL_M < max(ys))]
    return sel.sort_values(["lamby", "lambx"]).reset_index(drop=True)


def _extract(folder, name: str):
    """Local extract of a national file, and its metadata file."""
    dest = folder / name.replace(".csv.gz", ".csv")
    return dest, dest.with_suffix(".json")


def _covers(dest, meta_path, keys: set[str]) -> dict | None:
    """The metadata of a local extract holding (at least) the wanted cells, else None."""
    if not (dest.exists() and meta_path.exists()):
        return None
    cached = json.loads(meta_path.read_text())
    return cached if set(cached.get("cells", ())) >= keys else None


def _filter_remote(name: str, keys: set[str], dest, meta_path, recheck_days: int = 0) -> dict:
    """Stream a national csv.gz, keep the rows of the wanted cells. Skips unchanged files.

    ``recheck_days``: a local extract checked against the server less than this many days ago
    is used without asking the server (past years, which rarely change)."""
    cached = _covers(dest, meta_path, keys)
    now = dt.datetime.now(dt.timezone.utc)
    if cached and recheck_days:
        checked = dt.datetime.fromisoformat(cached.get("checked") or cached["fetched"])
        if now - checked < dt.timedelta(days=recheck_days):
            return cached
    url = f"{settings.sim2_base_url}/{name}"
    head = session().head(url, timeout=60)
    head.raise_for_status()
    remote = {"last_modified": head.headers.get("Last-Modified"), "size": head.headers.get("Content-Length")}
    if cached and cached.get("remote") == remote:
        cached["checked"] = now.isoformat(timespec="seconds")
        meta_path.write_text(json.dumps(cached, indent=1))
        log.debug("%s unchanged: using the local extract", name)
        return cached
    log.info("Streaming %s (%.0f MB) and keeping %d cells", name, int(remote["size"] or 0) / 1e6, len(keys))
    kept = 0
    tmp = dest.with_suffix(".part")
    with session().get(url, stream=True, timeout=600) as r:
        r.raise_for_status()
        r.raw.decode_content = False
        with gzip.GzipFile(fileobj=r.raw) as gz, open(tmp, "w", encoding="utf-8", newline="") as out:
            text = io.TextIOWrapper(gz, encoding="utf-8", newline="")
            out.write(text.readline())
            for line in text:
                cut = line.find(";", line.find(";") + 1)
                if line[:cut] in keys:
                    out.write(line)
                    kept += 1
    tmp.replace(dest)
    meta = {"remote": remote, "cells": sorted(keys), "rows": kept,
            "fetched": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    meta_path.write_text(json.dumps(meta, indent=1))
    log.info("  kept %d rows", kept)
    return meta


def _read(path) -> pd.DataFrame:
    df = pd.read_csv(path, sep=";", dtype={"DATE": str})
    df["date"] = pd.to_datetime(df["DATE"], format="%Y%m%d")
    return df


def run(aoi: AOI) -> dict:
    cells = _cells(aoi)
    if cells.empty:
        raise RuntimeError("No SAFRAN cell intersects the AOI (SIM2 covers metropolitan France only)")
    keys = {f"{a};{b}" for a, b in zip(cells.lambx, cells.lamby)}
    # One extract for every site: each national file is downloaded once, with the cells of all
    # the sites (each site records its cells; the extract is redone when it lacks some of them)
    site_dir = settings.raw_dir / "sim2" / aoi.code
    site_dir.mkdir(parents=True, exist_ok=True)
    (site_dir / "cells.json").write_text(json.dumps(sorted(keys)))
    for s in all_sites():
        f = settings.raw_dir / "sim2" / s.code / "cells.json"
        if s.code != aoi.code and f.exists():
            keys |= set(json.loads(f.read_text()))
    folder = settings.raw_dir / "sim2" / "extract"
    folder.mkdir(parents=True, exist_ok=True)
    today = dt.date.today()
    start = min(settings.sim2_start, settings.weather_start)

    used: dict = {}                                    # extract -> its metadata (what the store is built from)

    def extract(name: str, recheck_days: int = 0):
        dest, meta_path = _extract(folder, name)
        used[dest] = _filter_remote(name, keys, dest, meta_path, recheck_days)
        return dest

    latest_path = extract("QUOT_SIM2_latest.csv.gz")
    latest = _read(latest_path)
    latest_start = latest["date"].min()
    for year in range(start.year, today.year + 1):
        name = f"QUOT_SIM2_{year}.csv.gz"
        dest, meta_path = _extract(folder, name)
        if year == today.year and (cached := _covers(dest, meta_path, keys)):
            current = _read(dest)
            # The yearly file is refreshed monthly; the daily "latest" file covers the recent months.
            if not current.empty and current["date"].max() + pd.Timedelta(days=1) >= latest_start:
                used[dest] = cached
                continue
        if latest_start > pd.Timestamp(year, 1, 1):   # not entirely covered by the latest file
            try:
                # Past years are only checked against the server once a month
                extract(name, recheck_days=30 if year < today.year else 0)
            except Exception as exc:  # the current-year file may not exist yet in January
                if year == today.year:
                    log.warning("%s not available: %s", name, exc)
                else:
                    raise

    # Nothing to do when the store was built from the same files, for the same cells
    inputs = hashlib.sha1(json.dumps([sorted(f"{a};{b}" for a, b in zip(cells.lambx, cells.lamby)), start.isoformat(),
                                      [[p.name, m.get("remote"), m.get("rows")] for p, m in sorted(used.items())]],
                                     sort_keys=True).encode()).hexdigest()[:16]
    path = store_path("sim2")
    if (path / "zarr.json").exists():
        with xr.open_zarr(path, consolidated=True, chunks=None) as old:
            if old.attrs.get("inputs") == inputs:
                log.info("SIM2 unchanged (up to %s)", old.attrs.get("end"))
                return {"unchanged": True, "cells": len(cells), "end": old.attrs.get("end")}

    # The years in order, then the latest file: it wins on overlapping days
    frames = [_read(p) for p in used if p != latest_path] + [latest]
    df = pd.concat(frames, ignore_index=True)
    # This site's cells only (the extract holds every site's), then one row per cell and day
    point = pd.MultiIndex.from_arrays([cells.lambx, cells.lamby]).get_indexer(
        pd.MultiIndex.from_arrays([df.LAMBX, df.LAMBY]))
    df = df.assign(point=point)[(point >= 0) & (df["date"] >= pd.Timestamp(start)).values]
    df = df.drop_duplicates(subset=["point", "date"], keep="last")

    times = np.sort(df["date"].unique())
    ti = np.searchsorted(times, df["date"].values)
    data = {}
    for col, (name, units, scale, day) in COLUMNS.items():
        if col not in df:
            continue
        arr = np.full((len(times), len(cells)), np.nan, dtype="float32")
        arr[ti, df["point"].values] = df[col].astype("float64").values * scale
        data[name] = (("time", "point"), arr, {"units": units, "sim2_code": col, "day": day})
    # Total precipitation, comparable with the other products
    data["precip"] = (("time", "point"), data["rain"][1] + data["snowfall_we"][1],
                      {"units": "mm", "sim2_code": "PRELIQ+PRENEI", "day": "sum ]06UTC-06UTC]"})
    ds = xr.Dataset(
        data,
        coords={
            "time": times,
            "point": np.arange(len(cells)),
            "lon": ("point", cells.lon.values.astype("float64")),
            "lat": ("point", cells.lat.values.astype("float64")),
            "x_l2e": ("point", cells.lambx.values * 100.0, {"units": "m", "crs": "EPSG:27572"}),
            "y_l2e": ("point", cells.lamby.values * 100.0, {"units": "m", "crs": "EPSG:27572"}),
        },
        attrs={
            "product": "sim2", "source": PRODUCTS["sim2"].source, "aoi": aoi.code,
            "start": str(times[0])[:10], "end": str(times[-1])[:10],
            "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "inputs": inputs,
            "grid": "SAFRAN 8 km, Lambert II étendu (EPSG:27572); lon/lat are cell centres",
            "license": "Licence Ouverte Etalab 2.0, Météo-France",
        },
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.rmtree(path)
    ds.to_zarr(path, mode="w", consolidated=True)
    clear_cache()
    log.info("Wrote %s: %d cells, %s to %s", path.name, len(cells), ds.attrs["start"], ds.attrs["end"])
    return {"cells": len(cells), "start": ds.attrs["start"], "end": ds.attrs["end"], "days": len(times)}
