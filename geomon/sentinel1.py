"""Sentinel-1 GRD backscatter: storage, measures, area statistics.

Source: the Copernicus Data Space openEO collection SENTINEL1_GRD (IW, VV + VH),
calibrated by the back-end's ``sar_backscatter`` with the coefficient it offers,
sigma0 on the ellipsoid. It is not terrain-flattened: on slopes the backscatter
depends on the slope's orientation towards the satellite, so values are best
compared within one relative orbit (the same viewing geometry).

Storage: one Zarr store per orbit direction and year,
``data/zarr/sentinel1/<site>/[<part>/]<asc|desc>/<year>.zarr``, on the native
10 m UTM grid (the grid of the Sentinel-2 stores). The backend gives one image
per day: ascending (evening) and descending (morning) passes are requested
separately so that they are never blended. Variables VV and VH in dB x 100
(int16, -32768 where there is no data); each date carries its relative orbit
(``relative_orbit``, from the STAC catalogue).

GRD keeps the amplitude only: interferometric coherence needs the phase, i.e.
SLC products (STAC collections sentinel-1-slc, sentinel-1-slc-burst).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
import xarray as xr

from .arrays import quiet
from .config import settings

COLLECTION = "SENTINEL1_GRD"
COEFFICIENT = "sigma0-ellipsoid"
DIRECTIONS = {"asc": "ascending", "desc": "descending"}
SCALE = 0.01                  # dB per stored unit
NODATA = -32768


@dataclass(frozen=True)
class Measure:
    name: str
    long_name: str
    units: str
    vmin: float               # display range
    vmax: float
    colormap: str


MEASURES = {m.name: m for m in (
    Measure("vv", "VV backscatter (sigma0)", "dB", -25.0, 0.0, "gray"),
    Measure("vh", "VH backscatter (sigma0)", "dB", -32.0, -8.0, "gray"),
    Measure("vh_vv", "VH/VV cross ratio", "dB", -15.0, -3.0, "viridis"),
    Measure("rvi", "Radar vegetation index, 4 VH / (VV + VH)", "", 0.0, 1.0, "YlGn"),
)}


def stat_name(measure: str, direction: str) -> str:
    """Name of a measure in eo.index_stats, e.g. vv_asc."""
    return f"{measure}_{direction}"


# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------
def site_dir(site: str) -> Path:
    return settings.zarr_dir / "sentinel1" / site


def part_dir(site: str, part: str = "") -> Path:
    return site_dir(site) / part if part else site_dir(site)


def store_path(site: str, year: int, direction: str, part: str = "") -> Path:
    return part_dir(site, part) / direction / f"{year}.zarr"


def _years_in(d: Path) -> list[int]:
    return sorted(int(p.stem) for p in d.glob("*.zarr") if p.stem.isdigit() and (p / "zarr.json").exists()) \
        if d.exists() else []


def parts(site: str) -> list[str]:
    """The parts with stores: "" (a site in one block), or p1, p2..."""
    d = site_dir(site)
    if not d.exists():
        return []
    out = [""] if any(_years_in(d / k) for k in DIRECTIONS) else []
    out += sorted(p.name for p in d.iterdir()
                  if p.is_dir() and p.name not in DIRECTIONS and any(_years_in(p / k) for k in DIRECTIONS))
    return out


def years(site: str, direction: str, part: str | None = None) -> list[int]:
    """Years with a store, in one part or in any part when ``part`` is None."""
    if part is not None:
        return _years_in(part_dir(site, part) / direction)
    return sorted({y for p in parts(site) for y in _years_in(part_dir(site, p) / direction)})


@lru_cache(maxsize=32)
def _open(path: str, mtime: float) -> xr.Dataset:
    return xr.open_zarr(path, consolidated=True)


def open_year(site: str, year: int, direction: str, part: str = "") -> xr.Dataset | None:
    p = store_path(site, year, direction, part)
    z = p / "zarr.json"
    return _open(str(p), z.stat().st_mtime) if z.exists() else None


def clear_cache() -> None:
    _open.cache_clear()


def scene(site: str, date: str, direction: str, part: str = "") -> xr.Dataset:
    """One date of one part and direction, loaded (KeyError when absent)."""
    ds = open_year(site, int(date[:4]), direction, part)
    if ds is None:
        raise KeyError(date)
    hits = np.nonzero(ds["time"].values.astype("datetime64[D]") == np.datetime64(date[:10]))[0]
    if not hits.size:
        raise KeyError(date)
    return ds.isel(time=int(hits[0])).load()


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------
def encode_db(linear: xr.DataArray) -> xr.DataArray:
    """Linear backscatter -> dB x 100 as int16 (lazy with dask); no data where not positive."""
    with np.errstate(divide="ignore", invalid="ignore"):
        db = 10 * np.log10(linear.where(linear > 0))
    return (db / SCALE).round().clip(-32767, 32767).fillna(NODATA).astype("int16")


def decode_db(stored: np.ndarray) -> np.ndarray:
    """dB x 100 int16 -> dB float32, NaN where there is no data."""
    out = stored.astype("float32") * SCALE
    out[stored == NODATA] = np.nan
    return out


def measure_array(ds_date: xr.Dataset, name: str) -> np.ndarray:
    """A measure of one date (2-D): VV, VH, VH/VV in dB, RVI (linear)."""
    vv, vh = decode_db(ds_date["VV"].values), decode_db(ds_date["VH"].values)
    if name == "vv":
        return vv
    if name == "vh":
        return vh
    if name == "vh_vv":
        return vh - vv
    if name == "rvi":
        lvv, lvh = 10 ** (vv / 10), 10 ** (vh / 10)
        return (4 * lvh / (lvv + lvh)).astype("float32")
    raise KeyError(name)


def area_rows(pieces: list[tuple[xr.Dataset, dict[str, np.ndarray]]], n_area: dict[str, int],
              direction: str) -> list[dict]:
    """Statistics of every measure over every area for one date seen in one or more parts (as
    rows of eo.index_stats). ``pieces``: (loaded date of a part, area pixels as flat indices, see
    :func:`geomon.sentinel2.area_zones`).

    Means are taken in linear power (the mean of dB values would be biased by the speckle) and
    expressed in dB; the area's VH/VV and RVI come from its mean VV and VH. Medians and
    percentiles are those of the pixels' values.
    """
    prepared = [(decode_db(d["VV"].values).ravel(), decode_db(d["VH"].values).ravel(), z) for d, z in pieces]
    rows = []
    for code, total in n_area.items():
        vv = [a[z[code]] for a, _, z in prepared if code in z and z[code].size]
        vh = [b[z[code]] for _, b, z in prepared if code in z and z[code].size]
        vv = np.concatenate(vv) if vv else np.zeros(0, "float32")
        vh = np.concatenate(vh) if vh else np.zeros(0, "float32")
        ok = np.isfinite(vv) & np.isfinite(vh)
        vv, vh = vv[ok], vh[ok]
        n = int(ok.sum())
        base = {"area": code, "n_area": total, "n_valid": n, "n_clear": n,
                "clear_fraction": n / total if total else 0.0}
        lvv, lvh = 10 ** (vv / 10), 10 ** (vh / 10)
        per_pixel = {"vv": vv, "vh": vh, "vh_vv": vh - vv, "rvi": 4 * lvh / (lvv + lvh)}
        with quiet():
            mvv, mvh = float(lvv.mean()) if n else np.nan, float(lvh.mean()) if n else np.nan
        means = {"vv": 10 * np.log10(mvv), "vh": 10 * np.log10(mvh), "vh_vv": 10 * np.log10(mvh / mvv),
                 "rvi": 4 * mvh / (mvv + mvh)}
        for name, v in per_pixel.items():
            row = base | {"index": stat_name(name, direction), "mean": None, "median": None, "p10": None,
                          "p90": None, "std": None}
            if n:
                p10, p50, p90 = np.percentile(v, [10, 50, 90])
                row |= {"mean": float(means[name]), "median": float(p50), "p10": float(p10), "p90": float(p90),
                        "std": float(v.std())}
            rows.append(row)
    return rows
