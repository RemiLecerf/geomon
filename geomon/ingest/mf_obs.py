"""Observations of the last hours at the Météo-France stations near the site (step ``mf-obs``,
dataset ``mf_obs``).

Sources (portail-api.meteofrance.fr, the application subscribed to them): "Données Publiques
Observation", the latest hour of a station (about 0.5 KB), for the regular updates; "Données
Publiques Paquet Observation", the hourly observations of all a département's stations over about
the last five days (about 1.2 MB), to start, after a gap and daily to choose the stations again
(validity times in UTC). The stations within RADIUS_KM of the site's
centre are kept (up to MAX_STATIONS), their names from the API's station list (cached a month).

Stored in ``data/zarr/sites/<site>/weather/mf_obs.zarr`` (time, point): new hours are merged with
the stored ones; in live mode (the default) only the last ``keep_hours`` are kept, in update mode
the record grows.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time

import numpy as np
import pandas as pd
import xarray as xr

from .. import datasets, meteofrance as mf
from ..aoi import AOI
from ..config import settings
from ..weather import distance_km
from ..zarrio import open_cached, write_atomic, write_bytes_atomic

log = logging.getLogger(__name__)

DATASET = "mf_obs"
RADIUS_KM = 40
MAX_STATIONS = 8
REUSE_S = 600                 # a département's answer serves the sites updated within 10 minutes
GAP_H = 3                     # a longer gap is filled from the département's package

# API field -> (variable, units, conversion)
FIELDS = {
    "t": ("t", "degC", lambda v: v - 273.15),
    "tn": ("t_min", "degC", lambda v: v - 273.15),
    "tx": ("t_max", "degC", lambda v: v - 273.15),
    "td": ("t_dew", "degC", lambda v: v - 273.15),
    "u": ("rh", "percent", None),
    "rr1": ("precip", "mm", None),
    "ff": ("wind", "m/s", None),
    "dd": ("wind_dir", "degree", None),
    "raf": ("gust", "m/s", None),
    "pmer": ("pmsl", "hPa", lambda v: v / 100),
    "vv": ("visibility", "m", None),
    "n": ("cloud", "percent", None),
    "sss": ("snow_depth", "m", None),
    "ray_glo01": ("radiation", "J/m**2", None),
    "insolh": ("sunshine", "min", None),
}


def store_path(site: str | None = None):
    return settings.site_dir(site) / "weather" / "mf_obs.zarr"


def open_store(site: str | None = None) -> xr.Dataset | None:
    return open_cached(store_path(site))


def station_names() -> dict[str, str]:
    """{station id: usual name} from the API's station list (cached for a month)."""
    path = settings.raw_dir / "mf_obs" / "stations.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or dt.datetime.now().timestamp() - path.stat().st_mtime > 30 * 86400:
        r = mf.get(f"{mf.PUBLIC}/public/DPPaquetObs/v2/liste-stations")
        r.raise_for_status()
        write_bytes_atomic(path, r.content)
    df = pd.read_csv(path, sep=";", dtype=str)
    return dict(zip(df.iloc[:, 0], df["Nom_usuel"])) if "Nom_usuel" in df else {}


def to_frame(records: list[dict]) -> pd.DataFrame:
    """API records -> one row per station and hour, converted variables."""
    df = pd.DataFrame.from_records(records)
    if df.empty:
        return df
    df["time"] = pd.to_datetime(df["validity_time"]).dt.tz_localize(None)
    out = df[["geo_id_insee", "lat", "lon", "time"]].rename(columns={"geo_id_insee": "station"})
    for field, (name, _units, conv) in FIELDS.items():
        if field in df:
            v = pd.to_numeric(df[field], errors="coerce")
            out[name] = conv(v) if conv else v
    return out


def nearest(df: pd.DataFrame, lon0: float, lat0: float, radius_km: float = RADIUS_KM,
            n: int = MAX_STATIONS) -> pd.DataFrame:
    """The stations of the records within ``radius_km`` of (lon0, lat0), nearest first."""
    st = df.groupby("station").agg(lat=("lat", "first"), lon=("lon", "first")).reset_index()
    st["km"] = distance_km(lon0, lat0, st.lon.to_numpy(), st.lat.to_numpy())
    return st[st.km <= radius_km].sort_values("km").head(n).reset_index(drop=True)


def merge(old: xr.Dataset | None, new: xr.Dataset, keep_hours: int | None) -> xr.Dataset:
    """Stored hours plus new ones (a station set that changed drops the old hours); a newer value of
    an hour replaces the stored one."""
    if old is not None and list(old["station"].values) == list(new["station"].values):
        station_coords = [c for c in new.coords if c not in ("time", "point")]
        data = [d.drop_vars(station_coords, errors="ignore")
                for d in (old.drop_sel(time=new["time"].values, errors="ignore"), new)]
        ds = xr.concat(data, dim="time", join="outer").sortby("time").assign_coords({c: new[c] for c in station_coords})
        ds.attrs = dict(new.attrs)
    else:
        ds = new
    if keep_hours:
        ds = ds.sel(time=ds["time"] > ds["time"].values.max() - np.timedelta64(keep_hours, "h"))
    return ds


def _departement(dep: str) -> list[dict]:
    """The hourly observations of a département's stations (the API's answer reused for REUSE_S)."""
    path = settings.raw_dir / "mf_obs" / f"horaire_{dep}.json"
    if not path.exists() or time.time() - path.stat().st_mtime > REUSE_S:
        r = mf.get(f"{mf.PUBLIC}/public/DPPaquetObs/v2/paquet/horaire",
                   {"id-departement": int(dep) if dep.isdigit() else dep, "format": "json"})
        r.raise_for_status()
        write_bytes_atomic(path, r.content)
    return json.loads(path.read_text(encoding="utf-8"))


def _latest(stations: list[str]) -> pd.DataFrame:
    """The latest hourly observation of each station (one small request each)."""
    records = []
    for code in stations:
        r = mf.get(f"{mf.PUBLIC}/public/DPObs/v2/station/horaire", {"id_station": code, "format": "json"})
        r.raise_for_status()
        records += r.json()
    return to_frame(records)


def run(aoi: AOI) -> dict:
    """The stations' latest hour, one small request per station (about 0.5 KB); the département's
    package (about 1.2 MB, five days of all its stations) only to start, to fill a gap of more than
    GAP_H hours, and once a day to look again for the nearest stations."""
    pol = datasets.policy(aoi.code, DATASET)
    keep = pol["keep_hours"] if pol["mode"] == "live" else None
    old = open_store(aoi.code)
    now = dt.datetime.now(dt.timezone.utc)
    package_at = dt.datetime.fromisoformat(old.attrs["package_at"]) if old is not None and "package_at" in old.attrs else None
    recent = old is not None and np.datetime64(now.replace(tzinfo=None)) - old["time"].values.max() <= np.timedelta64(GAP_H, "h")
    if recent and package_at and now - package_at < dt.timedelta(hours=24):
        st = pd.DataFrame({"station": old["station"].values.astype(str), "lon": old["lon"].values, "lat": old["lat"].values,
                           "km": old["distance_km"].values})
        names = dict(zip(st.station, old["station_name"].values.astype(str)))
        df = _latest(list(st.station))
    else:
        lon0, lat0 = aoi.centre_wgs84
        frames = [to_frame(_departement(dep)) for dep in aoi.departements]
        df = pd.concat(frames) if frames else pd.DataFrame()
        if df.empty:
            raise RuntimeError("No observation returned for the site's départements")
        st = nearest(df, lon0, lat0)
        if st.empty:
            raise RuntimeError(f"No Météo-France station observing within {RADIUS_KM} km")
        names = station_names()
        package_at = now
    df = df[df.station.isin(st.station)].drop_duplicates(["station", "time"], keep="last")
    times = np.sort(df["time"].unique())
    ti = np.searchsorted(times, df["time"].values)
    pi = pd.Index(st.station).get_indexer(df.station)
    data = {}
    for field, (name, units, _) in FIELDS.items():
        if name not in df:
            continue
        arr = np.full((len(times), len(st)), np.nan, dtype="float32")
        arr[ti, pi] = df[name].to_numpy(dtype="float64")
        if np.isfinite(arr).any():
            data[name] = (("time", "point"), arr, {"units": units, "mf_field": field})
    new = xr.Dataset(data, coords={
        "time": times, "point": np.arange(len(st)),
        "station": ("point", st.station.to_numpy(dtype=str)),
        "station_name": ("point", np.array([names.get(s, s) for s in st.station], dtype=str)),
        "lon": ("point", st.lon.to_numpy(dtype="float64")), "lat": ("point", st.lat.to_numpy(dtype="float64")),
        "distance_km": ("point", np.round(st.km.to_numpy(dtype="float64"), 1))},
        attrs={"source": "Météo-France, Données Publiques Observation (hourly)", "aoi": aoi.code,
               "package_at": package_at.isoformat(timespec="seconds"),
               "updated": now.isoformat(timespec="seconds")})
    if old is not None:
        missing = [v for v in new.data_vars if v not in old]
        old = old.assign({v: (("time", "point"), np.full((old.sizes["time"], old.sizes["point"]), np.nan, "float32"))
                          for v in missing})
        old = old[[v for v in old.data_vars if v in new.data_vars]]
    ds = merge(old, new, keep)
    write_atomic(ds, store_path(aoi.code))
    return {"stations": [f"{n} ({k:.1f} km)" for n, k in zip(new["station_name"].values, st.km)],
            "hours": int(ds.sizes["time"]), "latest": str(ds["time"].values.max())[:16],
            "source": "package" if package_at == now else "latest hour per station"}
