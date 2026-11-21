"""Météo-France daily Penman-Monteith ETP on a 0.125° grid (product ``mf_etp``).

Source: "Données ETP spatialisées quotidiennes" (meteo.data.gouv.fr, Licence Ouverte, no account): one
CSV per year for all of France (longitude;latitude;date;ETP_grille, mm), the current year updated
daily. The grid nodes whose 0.125° cell meets the site are kept. Past years are downloaded once
(about 5 MB each; last year checked again monthly); only the years whose file changed are parsed
again, the others are kept from the stored product.
"""

from __future__ import annotations

import datetime as dt
import gzip

import numpy as np
import pandas as pd
import xarray as xr

from ..aoi import AOI
from ..config import settings
from ..http import cached_download
from ..weather import PRODUCTS, open_product, write_product

URL = "https://meteofrance.s3.sbg.io.cloud.ovh.net/data/REF_CC/ETP_QUOT/QUOT_ETPgrille_{year}.csv.gz"
FIRST_YEAR = 1970
STEP = 0.125


def _parse(path, box) -> pd.DataFrame:
    x0, y0, x1, y1 = box
    h = STEP / 2
    with gzip.open(path, "rt") as f:
        df = pd.read_csv(f, sep=";", dtype={"date": str})
    return df[(df.longitude + h >= x0) & (df.longitude - h <= x1) & (df.latitude + h >= y0) & (df.latitude - h <= y1)]


def run(aoi: AOI) -> dict:
    today = dt.date.today()
    start = max(FIRST_YEAR, min(settings.weather_start, settings.sim2_start).year)
    old = open_product("mf_etp")
    reusable = old is not None and old.attrs.get("bbox") == list(aoi.bbox_wgs84)
    kept_years = set(pd.DatetimeIndex(old["time"].values).year) - {today.year} if reusable else set()
    frames, keep = [], []
    for year in range(start, today.year + 1):
        # The current year changes daily, the previous one may still be corrected; older ones not
        age = 0.5 if year == today.year else 30 if year == today.year - 1 else float("inf")
        path, fresh = cached_download(URL.format(year=year), settings.raw_dir / "mf_etp" / f"QUOT_ETPgrille_{year}.csv.gz",
                                      age, missing_ok=True)
        if path is None:
            continue
        if year in kept_years and not fresh:
            keep.append(year)
        else:
            frames.append(_parse(path, aoi.bbox_wgs84))
    df = pd.concat(frames) if frames else pd.DataFrame(columns=["longitude", "latitude", "date", "ETP_grille"])
    if df.empty and not keep:
        raise RuntimeError("No ETP grid node over the site (the grid covers metropolitan France)")
    if keep:
        nodes = pd.DataFrame({"longitude": old["lon"].values, "latitude": old["lat"].values})
    else:
        nodes = df[["longitude", "latitude"]].drop_duplicates().sort_values(["latitude", "longitude"]).reset_index(drop=True)
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    times = np.sort(df["date"].unique())
    arr = np.full((len(times), len(nodes)), np.nan, dtype="float32")
    pi = pd.MultiIndex.from_frame(nodes).get_indexer(pd.MultiIndex.from_frame(df[["longitude", "latitude"]]))
    ok = pi >= 0
    arr[np.searchsorted(times, df["date"].values[ok]), pi[ok]] = df["ETP_grille"].to_numpy(dtype="float64")[ok]
    if keep:
        old_t = pd.DatetimeIndex(old["time"].values)
        sel = np.isin(old_t.year, keep)
        times = np.concatenate([old["time"].values[sel], times])
        arr = np.concatenate([old["et0"].values[sel], arr])
        order = np.argsort(times, kind="stable")
        times, arr = times[order], arr[order]
    ds = xr.Dataset({"et0": (("time", "point"), arr, {"units": "mm", "long_name": "Penman-Monteith ETP (Météo-France grid)"})},
                    coords={"time": times, "point": np.arange(len(nodes)),
                            "lon": ("point", nodes.longitude.to_numpy(dtype="float64")),
                            "lat": ("point", nodes.latitude.to_numpy(dtype="float64"))},
                    attrs={"product": "mf_etp", "source": PRODUCTS["mf_etp"].source, "aoi": aoi.code,
                           "bbox": list(aoi.bbox_wgs84), "start": str(times[0])[:10], "end": str(times[-1])[:10],
                           "grid_step": STEP, "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                           "license": "Licence Ouverte Etalab 2.0, Météo-France"})
    write_product(ds, "mf_etp")
    return {"nodes": len(nodes), "days": len(times), "end": str(times[-1])[:10], "years_parsed": len(frames),
            "years_kept": len(keep)}
