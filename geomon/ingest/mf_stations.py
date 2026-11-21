"""Measured daily weather of the Météo-France stations near the site (product ``mf_stations``).

Source: "Données climatologiques de base - quotidiennes" (meteo.data.gouv.fr, Licence Ouverte, no
account): one pair of files per département and period, the history (1950 to last year) and the
current years (updated daily). The stations of the site's départements within RADIUS_KM of its centre
and still measuring are kept (up to MAX_STATIONS); the weather API takes each variable, each day, from
the nearest station that measured it.

Days are Météo-France's: precipitation from 06 UTC on the day to 06 UTC the next day; temperatures
and the other variables over the civil day (see the ``day`` attributes).
"""

from __future__ import annotations

import datetime as dt
import logging
import re

import numpy as np
import pandas as pd
import xarray as xr

from ..aoi import AOI
from ..config import settings
from ..http import cached_download, datagouv_resources
from ..weather import PRODUCTS, distance_km, write_product

log = logging.getLogger(__name__)

DATASET = "6569b51ae64326786e4e8e1a"          # data.gouv.fr: Données climatologiques de base - quotidiennes
RADIUS_KM = 40
MAX_STATIONS = 8
HISTORY_RECHECK_DAYS = 30

# File column -> (daily variable, units, scale, day definition)
COLUMNS = {
    "RR": ("precip", "mm", 1.0, "06 UTC to 06 UTC the next day"),
    "TN": ("t_min", "degC", 1.0, "civil day"), "TX": ("t_max", "degC", 1.0, "civil day"),
    "TM": ("t_mean", "degC", 1.0, "mean of the hourly values"),
    "FFM": ("wind_mean", "m/s", 1.0, "mean of the 10-minute winds at 10 m"),
    "FXI": ("gust_max", "m/s", 1.0, "maximum instantaneous wind at 10 m"),
    "UM": ("rh_mean", "percent", 1.0, "mean of the hourly values"),
    "UN": ("rh_min", "percent", 1.0, "minimum"), "UX": ("rh_max", "percent", 1.0, "maximum"),
    "TSVM": ("vp_mean", "hPa", 1.0, "mean vapour pressure"),
    "GLOT": ("radiation", "MJ/m**2", 0.01, "global radiation (J/cm2 / 100)"),
    "ETPMON": ("et0", "mm", 1.0, "Penman-Monteith ETP of the station"),
    "NEIGETOTX": ("snow_depth", "m", 0.01, "maximum snow depth (cm / 100)"),
}


def _files(dep: str, resources: list[dict]) -> dict[str, str]:
    """{"history": url, "latest": url, "history_other": url, "latest_other": url} of a département,
    among the dataset's resources."""
    out = {}
    for res in resources:
        url = res["url"]
        m = re.search(rf"/Q_{dep}_(previous|latest)-[0-9-]+_(RR-T-Vent|autres-parametres)\.csv\.gz$", url)
        if m:
            out[("history" if m.group(1) == "previous" else "latest") + ("" if m.group(2) == "RR-T-Vent" else "_other")] = url
    return out


META = ("NUM_POSTE", "NOM_USUEL", "LAT", "LON", "ALTI", "AAAAMMJJ")


def _read_csv(url: str, max_age_days: float, keep: set[str] | None = None) -> pd.DataFrame:
    """A daily file (downloaded when older than ``max_age_days`` and changed), its rows of the
    stations ``keep``. The rows of a station set are kept beside the file until it changes: the
    history files (1950 to last year, large) are then parsed once a month, not daily."""
    import hashlib

    path, changed = cached_download(url, settings.raw_dir / "mf_stations" / url.rsplit("/", 1)[-1], max_age_days)
    rows = None
    if keep is not None:
        rows = path.with_name(f"{path.name}.{hashlib.sha1(' '.join(sorted(keep)).encode()).hexdigest()[:12]}.pkl")
        if not changed and rows.exists() and rows.stat().st_mtime >= path.stat().st_mtime:
            return pd.read_pickle(rows)
    df = pd.read_csv(path, sep=";", dtype={"NUM_POSTE": str, "AAAAMMJJ": str}, low_memory=False,
                     usecols=lambda c: c in META or c in COLUMNS)
    if keep is not None:
        df = df[df.NUM_POSTE.isin(keep)]
        df.to_pickle(rows)
    return df


def run(aoi: AOI) -> dict:
    lon0, lat0 = aoi.centre_wgs84
    resources = datagouv_resources(DATASET)
    frames = []
    for dep in aoi.departements:
        files = _files(dep, resources)
        if "latest" not in files:
            log.warning("Stations: no daily files for département %s", dep)
            continue
        latest = _read_csv(files["latest"], 0.5)
        # Stations near the site still measuring
        st = latest.groupby("NUM_POSTE").agg(name=("NOM_USUEL", "first"), lon=("LON", "first"), lat=("LAT", "first"),
                                             alt=("ALTI", "first"), last=("AAAAMMJJ", "max")).reset_index()
        st["km"] = distance_km(lon0, lat0, st.lon.to_numpy(dtype="float64"), st.lat.to_numpy(dtype="float64"))
        recent = (dt.date.today() - dt.timedelta(days=60)).strftime("%Y%m%d")
        st = st[(st.km <= RADIUS_KM) & (st["last"] >= recent)]
        if st.empty:
            continue
        keep = set(st.NUM_POSTE)
        parts = [latest[latest.NUM_POSTE.isin(keep)]]
        for key, age in (("history", HISTORY_RECHECK_DAYS), ("latest_other", 0.5), ("history_other", HISTORY_RECHECK_DAYS)):
            if key in files:
                parts.append(_read_csv(files[key], age, keep))
        frames.append((st, parts))
    if not frames:
        raise RuntimeError(f"No Météo-France station measuring within {RADIUS_KM} km")
    stations = pd.concat([st for st, _ in frames]).sort_values("km").head(MAX_STATIONS).reset_index(drop=True)
    keep = set(stations.NUM_POSTE)
    # RR-T-Vent and the other parameters: one row per station and day
    main = pd.concat([p for _, parts in frames for p in parts if "RR" in p.columns])
    other = [p for _, parts in frames for p in parts if "RR" not in p.columns]
    df = main[main.NUM_POSTE.isin(keep)].drop_duplicates(["NUM_POSTE", "AAAAMMJJ"], keep="first")
    if other:
        o = pd.concat(other)
        o = o[o.NUM_POSTE.isin(keep)].drop_duplicates(["NUM_POSTE", "AAAAMMJJ"], keep="first")
        cols = ["NUM_POSTE", "AAAAMMJJ"] + [c for c in COLUMNS if c in o.columns and c not in df.columns]
        df = df.merge(o[cols], on=["NUM_POSTE", "AAAAMMJJ"], how="left")
    df["date"] = pd.to_datetime(df["AAAAMMJJ"], format="%Y%m%d")
    df = df[df["date"] >= pd.Timestamp(min(settings.weather_start, settings.sim2_start))]
    times = np.sort(df["date"].unique())
    ti = np.searchsorted(times, df["date"].values)
    pi = pd.Index(stations.NUM_POSTE).get_indexer(df.NUM_POSTE)
    data = {}
    for col, (name, units, scale, day) in COLUMNS.items():
        if col not in df.columns:
            continue
        arr = np.full((len(times), len(stations)), np.nan, dtype="float32")
        arr[ti, pi] = pd.to_numeric(df[col], errors="coerce").to_numpy(dtype="float64") * scale
        if np.isfinite(arr).any():
            data[name] = (("time", "point"), arr, {"units": units, "mf_code": col, "day": day})
    ds = xr.Dataset(data, coords={
        "time": times, "point": np.arange(len(stations)),
        "lon": ("point", stations.lon.to_numpy(dtype="float64")), "lat": ("point", stations.lat.to_numpy(dtype="float64")),
        "elevation": ("point", pd.to_numeric(stations.alt, errors="coerce").to_numpy(dtype="float64")),
        "station": ("point", stations.NUM_POSTE.to_numpy(dtype=str)),
        "station_name": ("point", stations.name.to_numpy(dtype=str)),
        "distance_km": ("point", stations.km.round(1).to_numpy(dtype="float64"))},
        attrs={"product": "mf_stations", "source": PRODUCTS["mf_stations"].source, "aoi": aoi.code,
               "start": str(times[0])[:10], "end": str(times[-1])[:10],
               "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
               "license": "Licence Ouverte Etalab 2.0, Météo-France"})
    write_product(ds, "mf_stations")
    return {"stations": [f"{r.name} ({r.km:.1f} km)" for r in stations.itertuples()], "days": len(times),
            "end": str(times[-1])[:10], "variables": sorted(data)}
