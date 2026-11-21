"""Météo-France forecast models read through their WCS (see geomon.meteofrance), each kept for a site
as its latest complete run only:

- ``arome``: AROME at 1.3 km (0.01° grid), every 3 hours, to 51 hours: rain, temperature at 2 m and
  gusts, hourly for the first day then every 3 hours, over the site and ``AROME_MARGIN_KM`` around,
  as maps (``arome_maps.zarr``: ``rain`` rate (mm/h, the hour before ``time``, then the mean of the 3
  hours before), ``t2m`` (°C) and ``gust`` (maximum over the hour, m/s));
- ``pearome``: the PE-AROME ensemble (25 members, 2.5 km), every 6 hours: for the two 24-hour
  windows after the run, each member's rain and dawn minimum temperature over the site, hence
  probabilities (``ensemble_pearome.json``);
- ``sea``: for a coastal site, the MFWAM waves (0.025°) and the HYCOM2D storm surge (Atlantic,
  0.04°) at the sea cells nearest the site (``sea.zarr``).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import xarray as xr

from .config import settings
from .zarrio import open_cached, read_json

AROME_WCS = "/public/arome/1.0/wcs/MF-NWP-HIGHRES-AROME-001-FRANCE-WCS"
AROME_RAIN = ("TOTAL_PRECIPITATION__GROUND_OR_WATER_SURFACE", "_PT1H")
AROME_T2M = "TEMPERATURE__SPECIFIC_HEIGHT_LEVEL_ABOVE_GROUND"
AROME_GUST = "WIND_SPEED_GUST__SPECIFIC_HEIGHT_LEVEL_ABOVE_GROUND"
AROME_HOURS = 51
AROME_HOURLY_H = 24             # hourly maps for the first day, then every 3 hours
AROME_MARGIN_KM = 30
AROME_ATTRIBUTION = "Météo-France, AROME 1.3 km (0.01°)"

PEAROME_WCS = "/public/pearome/1.0/wcs/MF-NWP-HIGHRES-PEARO{member:03d}-0025-FRANCE-WCS"
PEAROME_MEMBERS = 25
PEAROME_RAIN = ("TOTAL_PRECIPITATION__GROUND_OR_WATER_SURFACE", "_P1D")      # the 24 hours before each time
PEAROME_TMIN = "MINIMUM_TEMPERATURE__SPECIFIC_HEIGHT_LEVEL_ABOVE_GROUND"      # over the hour before each time
DAWN_LOCAL_HOURS = (4, 5, 6, 7, 8)          # hours read for the night's minimum (local time, end of each hour)
RAIN_THRESHOLDS = (1, 5, 10, 20)            # mm in 24 hours
FROST_THRESHOLDS = (0, -2, -5)              # °C
PEAROME_ATTRIBUTION = "Météo-France, PE-AROME ensemble (25 members, 2.5 km)"

MFWAM_WCS = "/public/wavesmodels/1.0/wcs/MF-NWP-HIGHRES-MFWAM-0025-FRANCE-WCS"
HYCOM_WCS = "/public/wavesmodels/1.0/wcs/MF-NWP-HIGHRES-HYCOM2DARO-0041-ATL-WCS"
WAVES = {"hs": ("SIGNIFICANT_HEIGHT_OF_COMBINED_WIND_WAVES_AND_SWELL__WATER_SURFACE", "m", "Significant wave height"),
         "tp": ("PEAK_WAVE_PERIOD__WATER_SURFACE", "s", "Peak period"),
         "dir": ("DIRECTION_OF_COMBINED_WIND_WAVES_AND_SWELL__WATER_SURFACE", "degree", "Direction the waves come from")}
SURGE = "EXTRA_TROPICAL_STORM_SURGE__WATER_SURFACE"
SEA_MAX_KM = 30                 # a site with no sea cell this close is not coastal
WAVES_STEP_H = 3
SEA_ATTRIBUTION = "Météo-France, MFWAM waves (0.025°) and HYCOM2D storm surge (Atlantic)"


def arome_path(site=None) -> Path:
    return settings.site_dir(site) / "arome_maps.zarr"


def ensemble_path(site=None) -> Path:
    return settings.site_dir(site) / "ensemble_pearome.json"


def sea_path(site=None) -> Path:
    return settings.site_dir(site) / "sea.zarr"


def sea_status_path(site=None) -> Path:
    return settings.site_dir(site) / "sea_status.json"


def coastal(site=None) -> bool | None:
    """Whether the sea step found sea near the site (None: not checked yet)."""
    return (read_json(sea_status_path(site)) or {}).get("coastal")


def open_zarr(path: Path) -> xr.Dataset | None:
    return open_cached(path)


def open_ensemble(site=None) -> dict | None:
    return read_json(ensemble_path(site))


def probabilities(values: np.ndarray, thresholds, below: bool = False) -> dict:
    """Share of the members (NaN ignored) at or above (``below``: at or below) each threshold, and
    the 10th, 50th and 90th percentiles."""
    v = np.asarray(values, dtype="float64")
    v = v[np.isfinite(v)]
    if not v.size:
        return {"members": 0}
    p = {str(t): round(float(((v <= t) if below else (v >= t)).mean()), 3) for t in thresholds}
    q = np.percentile(v, [10, 50, 90])
    return {"members": int(v.size), "probability": p, "p10": round(float(q[0]), 1), "p50": round(float(q[1]), 1),
            "p90": round(float(q[2]), 1), "min": round(float(v.min()), 1), "max": round(float(v.max()), 1)}


def nearest_sea_cells(values: np.ndarray, lon: np.ndarray, lat: np.ndarray, lon0: float, lat0: float,
                      max_km: float = SEA_MAX_KM, n: int = 4) -> list[tuple[int, int, float]]:
    """(row, col, km) of the ``n`` cells with data nearest to (lon0, lat0), within ``max_km``."""
    from .weather import distance_km

    lo, la = np.meshgrid(lon, lat)
    km = distance_km(lon0, lat0, lo, la)
    km[~np.isfinite(values)] = np.inf
    order = np.argsort(km, axis=None)[:n]
    rows, cols = np.unravel_index(order, km.shape)
    return [(int(r), int(c), round(float(km[r, c]), 1)) for r, c in zip(rows, cols) if km[r, c] <= max_km]
