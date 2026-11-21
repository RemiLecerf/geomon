"""Nowcast: the next 6 hours from Météo-France's AROME-PI (prévision immédiate), run every hour at
1.3 km and served on a 0.01° grid (API "AROME-PI", portail-api.meteofrance.fr, free key, the
application subscribed to it).

Kept for a site (``data/zarr/sites/<site>/nowcast_aromepi.zarr``), the latest complete run only,
over the site and ``MARGIN_KM`` around (to see the rain coming, as the radar):

- ``rain`` (time, y, x): rain and snow (water equivalent) in each 15 minutes, mm, dated by the end
  of the 15 minutes;
- ``t2m`` (time_t, y, x): temperature at 2 m, °C, every hour from the run time;
- ``gust`` (time_g, y, x): maximum gust at 10 m over the hour ending at ``time_g``, m/s.

``x``/``y`` are longitudes/latitudes of the cell centres (EPSG:4326).
"""

from __future__ import annotations

from pathlib import Path

import xarray as xr

from .config import settings
from .zarrio import open_cached

WCS_PATH = "/public/aromepi/1.0/wcs/MF-NWP-HIGHRES-AROMEPI-001-FRANCE-WCS"
RAIN = "TOTAL_PRECIPITATION__GROUND_OR_WATER_SURFACE"           # with the suffix _PT15M
T2M = "TEMPERATURE__SPECIFIC_HEIGHT_LEVEL_ABOVE_GROUND"         # height 2 m
GUST = "WIND_SPEED_MAXIMUM_GUST__SPECIFIC_HEIGHT_LEVEL_ABOVE_GROUND"  # with the suffix _PT1H, height 10 m
STEP_MIN = 15
HORIZON_H = 6
STEPS = HORIZON_H * 60 // STEP_MIN
MARGIN_KM = 50
DATASET = "nowcast"
ATTRIBUTION = "Météo-France, AROME-PI (prévision immédiate), 0.01°"


def store_path(site: str | None = None) -> Path:
    return settings.site_dir(site) / "nowcast_aromepi.zarr"


def open_store(site: str | None = None) -> xr.Dataset | None:
    return open_cached(store_path(site))
