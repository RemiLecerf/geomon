"""Radar rain: where it rains and has been raining over the last hours, from two sources.

- ``opera`` (default, no account): the EUMETNET OPERA pan-European composite (CIRRUS maximum
  reflectivity, 1 km, every 5 minutes, all the national radars including Météo-France's), CC BY 4.0.
  The files are Cloud-Optimised GeoTIFFs in a 24-hour rolling cache (S3, anonymous): only the
  blocks holding the site's window are read, and the last 24 hours can be fetched at once.
  Reflectivity is turned into rain with the Marshall-Palmer relation Z = 200 R^1.6 (OPERA's own
  rain-rate product uses it too), capped at 56 dBZ (hail would give absurd rates).
- ``mf``: Météo-France's 500 m rain mosaic ("lame d'eau", API Ciblée Radar, free API key), every
  5 minutes, in hundredths of a millimetre. The API only serves the latest image: the hours are
  kept as they come.

Storage, the same for both: ``data/zarr/sites/<site>/radar_<source>.zarr``, the site's window
(plus ``margin_m``, to see the rain coming) of each image: ``rain`` (uint16, 1/100 mm in the
image's 5 minutes, 65535 no data), dated by the end of the 5 minutes. In live mode only the last
``keep_hours`` are kept (see geomon.datasets).

Accumulations follow Météo-France's advice: over more than 15 minutes, missing values count as 0
and the coverage (images present / expected) is reported.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import xarray as xr

from .config import settings
from .zarrio import open_cached

NODATA = 65535
SCALE = 0.01                  # mm per stored unit
STEP_MIN = 5
VMAX = {1: 5.0, 3: 10.0, 6: 20.0, 12: 30.0, 24: 40.0}     # display maximum (mm) per accumulation
COLORMAP = "YlGnBu"
MAX_DBZ = 56.0                # Marshall-Palmer cap (about 100 mm/h)

# Rain intensity classes for the maps and animations (mm/h, lower bounds), the usual radar scale
INTENSITY = [(0.1, "#a6d8f5"), (0.5, "#5fb0ea"), (1.0, "#2479cf"), (2.0, "#1aa64b"), (4.0, "#9ccc2b"),
             (8.0, "#f4d22b"), (16.0, "#f28c1e"), (32.0, "#de2f1f"), (64.0, "#a6127f")]


@dataclass(frozen=True)
class Source:
    code: str
    dataset: str              # geomon.datasets code
    label: str
    resolution_m: int
    margin_m: int
    attribution: str


SOURCES = {s.code: s for s in (
    Source("opera", "radar", "Europe, EUMETNET OPERA (all national radars), 1 km, every 5 minutes", 1000, 100_000,
           "EUMETNET OPERA, CIRRUS maximum reflectivity composite (CC BY 4.0); rain from Marshall-Palmer"),
    Source("mf", "radar_mf", "France, Météo-France, 500 m, every 5 minutes", 500, 50_000,
           "Météo-France, API Ciblée Radar, lame d'eau 500 m"),
)}


def store_path(source: str, site: str | None = None) -> Path:
    return settings.zarr_dir / "sites" / (site or settings.site_code) / f"radar_{source}.zarr"


def open_store(source: str, site: str | None = None) -> xr.Dataset | None:
    return open_cached(store_path(source, site))


def dbz_to_stored(dbz: np.ndarray) -> np.ndarray:
    """Reflectivity (dBZ; NaN: nothing detected, i.e. dry; <= -1e5: no radar) -> rain in 1/100 mm
    in 5 minutes (uint16), Marshall-Palmer Z = 200 R^1.6."""
    nodata = dbz <= -1e5
    d = np.where(np.isfinite(dbz) & ~nodata, np.minimum(dbz, MAX_DBZ), -99.0)
    rate = (10 ** (d / 10) / 200.0) ** (1 / 1.6)                     # mm/h
    rate[rate < 0.05] = 0.0                                           # below about 7 dBZ: no rain
    out = np.round(rate / 12 / SCALE).clip(0, 65534).astype("uint16")
    out[nodata] = NODATA
    return out


def rain_mm(raw: np.ndarray) -> np.ndarray:
    """Stored values -> mm in 5 minutes (float32), missing values as 0 (Météo-France's advice for
    cumulating more than 15 minutes)."""
    out = raw.astype("float32") * SCALE
    out[raw == NODATA] = 0.0
    return out


def rate_mmh(raw: np.ndarray) -> np.ndarray:
    """Stored values -> rain intensity in mm/h (float32), NaN without data."""
    out = raw.astype("float32") * SCALE * 60 / STEP_MIN
    out[raw == NODATA] = np.nan
    return out


def accumulation(ds: xr.Dataset, hours: float, end: np.datetime64 | None = None) -> tuple[np.ndarray, dict]:
    """Rain (mm) over the last ``hours`` before ``end`` (default: the latest image), and its
    coverage: images present / expected."""
    times = ds["time"].values
    end = times.max() if end is None else end
    start = end - np.timedelta64(int(hours * 60), "m")
    sel = np.nonzero((times > start) & (times <= end))[0]
    expected = int(hours * 60 / STEP_MIN)
    total = rain_mm(ds["rain"].values[sel]).sum(axis=0) if sel.size else np.zeros(ds["rain"].shape[1:], "float32")
    return total, {"images": int(sel.size), "expected": expected, "coverage": min(1.0, sel.size / expected),
                   "start": str(start)[:16], "end": str(end)[:16]}


def intensity_rgba(rate: np.ndarray, alpha: int = 225) -> np.ndarray:
    """Rain intensity (mm/h, NaN outside) -> RGBA with the INTENSITY classes, transparent when dry
    or without data."""
    rgba = np.zeros(rate.shape + (4,), "uint8")
    r = np.nan_to_num(rate, nan=-1.0)
    for lo, color in INTENSITY:
        m = r >= lo
        rgba[m] = [int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16), alpha]
    return rgba


def intensity_legend() -> list[dict]:
    return [{"min_mm_h": lo, "color": c} for lo, c in INTENSITY]


def time_index(times: np.ndarray, time: str) -> int | None:
    """Index of the image at ``time`` (to the minute), None when there is none."""
    hit = np.nonzero(times.astype("datetime64[m]") == np.datetime64(time[:16], "m"))[0]
    return int(hit[0]) if hit.size else None
