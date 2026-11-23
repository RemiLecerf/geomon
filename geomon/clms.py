"""Copernicus Land Monitoring Service (CLMS) products, read through openEO on the Copernicus Data
Space (the Sentinel-2 login; credits as for Sentinel-2):

- soil moisture, as weather products (geomon.weather): ``clms_ssm``, the surface soil moisture
  from Sentinel-1 (1 km, daily over the passes, since October 2014), and ``clms_swi``, the Soil
  Water Index at five depths of infiltration (T = 2, 10, 20, 40 and 100 days: from the surface
  layer to the root zone; 1 km, daily, version 1 to July 2025 then version 2). Both in % of
  saturation, one point per 1/112° cell;
- dry matter productivity ``dmp`` (DMP, kg of dry matter per hectare and day, 300 m, every 10
  days, since 2014): ``data/zarr/sites/<site>/dmp.zarr``, read per area by geomon.api.clms;
- vegetation phenology and productivity HR-VPP (10 m, yearly since 2017, the first season of each
  year): per parcel (eo.parcel_vpp), its start, peak and end dates, length, peak and amplitude of
  the Plant Phenology Index and the season's productivity.

HR-VPP dates: the files hold them as YYDOY (23095 is day 95 of 2023), but the openEO back-end casts
them to the 8 bits its catalogue declares, so they arrive modulo 256. They are recovered from the
season length (intact) and the peak (its season is the one of the product's year): the start
whose code matches, plus the length (or one day more), must give the end's code, with a peak date
of the year in between. About 83 % of pixels have a single solution; the others are left out.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from .arrays import quiet
from .config import settings
from .zarrio import open_cached

SSM = "CLMS_SSM_EUROPE_1KM_DAILY_V1"
SSM_START = dt.date(2014, 10, 4)
# Soil Water Index: version 1 to 11 July 2025, version 2 after
SWI = (("CLMS_SWI_EUROPE_1KM_DAILY_V1", dt.date(2014, 12, 31), dt.date(2025, 7, 11)),
       ("CLMS_SWI_EUROPE_1KM_DAILY_V2", dt.date(2025, 7, 12), None))
SWI_BANDS = ("swi002", "swi010", "swi020", "swi040", "swi100")
CELL_1KM = 1 / 112

DMP = "CLMS_DMP_GLOBAL_300M_10DAILY_V2"
DMP_START = dt.date(2014, 1, 1)
CELL_300M = 1 / 336

VPP = "COPERNICUS_VEGETATION_PHENOLOGY_PRODUCTIVITY_10M_SEASON1"
VPP_FIRST_YEAR = 2017
VPP_BANDS = ("SOSD", "MAXD", "EOSD", "LENGTH", "MAXV", "AMPL", "SPROD", "TPROD", "QFLAG")
VPP_SCALE = {"MAXV": 1e-4, "AMPL": 1e-4, "SPROD": 0.02, "TPROD": 0.02}
VPP_MIN_DATED = 0.3             # a parcel's dates need this share of its pixels decoded

ATTRIBUTION = "Copernicus Land Monitoring Service (CDSE openEO)"


def dmp_path(site: str | None = None) -> Path:
    return settings.site_dir(site) / "dmp.zarr"


def open_dmp(site: str | None = None):
    return open_cached(dmp_path(site))


def dekad(d: dt.date) -> int:
    """Index of a 10-day period in its year (0 to 35)."""
    return (d.month - 1) * 3 + min(2, (d.day - 1) // 10)


def dekad_days(d: dt.date) -> int:
    """Days of the 10-day period starting on ``d`` (the third of a month runs to its end)."""
    if d.day < 21:
        return 10
    nxt = dt.date(d.year + d.month // 12, d.month % 12 + 1, 1)
    return (nxt - d).days


# ---------------------------------------------------------------------------
# HR-VPP dates
# ---------------------------------------------------------------------------
def _code(day: dt.date) -> int:
    """YYDOY of a date, as the openEO back-end delivers it (modulo 256)."""
    return ((day.year % 100) * 1000 + day.timetuple().tm_yday) % 256


@lru_cache(maxsize=8)
def _days_by_code(year: int) -> tuple[dict, dict]:
    """{code: [dates]} of the year before and the year (season starts), and of the year (peaks)."""
    starts, peaks = {}, {}
    for k in range((dt.date(year + 1, 1, 1) - dt.date(year - 1, 1, 1)).days):
        day = dt.date(year - 1, 1, 1) + dt.timedelta(k)
        starts.setdefault(_code(day), []).append(day)
        if day.year == year:
            peaks.setdefault(_code(day), []).append(day)
    return starts, peaks


def season_dates(sosd: int, maxd: int, eosd: int, length: int, year: int) -> tuple[dt.date, dt.date, dt.date] | None:
    """(start, peak, end) of a season of ``year`` from its truncated codes and length, or None when
    there is no single solution."""
    starts, peaks = _days_by_code(year)
    found = []
    for s in starts.get(sosd, []):
        for extra in (0, 1):
            e = s + dt.timedelta(length + extra)
            if _code(e) != eosd:
                continue
            p = [d for d in peaks.get(maxd, []) if s <= d <= e]
            if p:
                found.append((s, p[0], e))
    return found[0] if len(found) == 1 else None


def decode_dates(sosd, maxd, eosd, length, year: int) -> np.ndarray:
    """Days after 1 January of ``year`` of the start, peak and end of each pixel's season:
    float array (3, n), NaN where undecided or without data."""
    stack = np.stack([np.asarray(a, dtype="int64").ravel() for a in (sosd, maxd, eosd, length)], axis=1)
    out = np.full((3, stack.shape[0]), np.nan)
    ok = np.all((stack > 0) & (stack < 65535), axis=1) & np.all(stack[:, :3] < 256, axis=1)
    if not ok.any():
        return out
    uniq, inverse = np.unique(stack[ok], axis=0, return_inverse=True)
    base = dt.date(year, 1, 1)
    values = np.full((len(uniq), 3), np.nan)
    for i, (s, p, e, n) in enumerate(uniq):
        r = season_dates(int(s), int(p), int(e), int(n), year)
        if r:
            values[i] = [(d - base).days for d in r]
    out[:, ok] = values[inverse.ravel()].T
    return out


# ---------------------------------------------------------------------------
# Values per field: the cells under a parcel, weighted by their share of its area
# ---------------------------------------------------------------------------
# The soil moisture cells (1 km) and the productivity cells (300 m) are larger than most parcels: a
# parcel's value is the mean of the cells it overlaps, each weighted by the share of the parcel's
# area inside it. Cells without a value (the sea) are left out while the others cover at least
# MIN_SHARE of the parcel.
SOIL_VARS = ("ssm", *SWI_BANDS)
FIELD_VARS = (*SOIL_VARS, "dmp")
MIN_SHARE = 0.5


@dataclass
class Grid:
    """A product as cells: centres, size (degrees), dates, and a reader of some cells' values."""
    var: str
    lon: np.ndarray
    lat: np.ndarray
    step: float
    times: pd.DatetimeIndex
    read: Callable[[np.ndarray], np.ndarray]            # cell indices -> (time, cell) values

    @property
    def key(self) -> tuple:
        return (self.step, len(self.lon), float(self.lon[0]), float(self.lat[0]))


def grid(var: str) -> Grid | None:
    """The cells of a soil moisture variable (``ssm``, ``swi002``...) or of ``dmp`` for the current
    site; None when its store is missing."""
    if var == "dmp":
        ds = open_dmp()
        if ds is None:
            return None
        lon, lat = np.meshgrid(ds["x"].values, ds["y"].values)
        flat = ds["dmp"].values.reshape(ds.sizes["time"], -1)
        return Grid(var, lon.ravel(), lat.ravel(), CELL_300M, pd.DatetimeIndex(ds["time"].values).normalize(),
                    lambda cols: flat[:, cols])
    from .weather import open_product

    ds = open_product("clms_ssm" if var == "ssm" else "clms_swi")
    if ds is None or var not in ds:
        return None
    return Grid(var, ds["lon"].values, ds["lat"].values, float(ds.attrs.get("grid_step", CELL_1KM)),
                pd.DatetimeIndex(ds["time"].values).normalize(), lambda cols: ds[var].isel(point=cols).values)


def cell_weights(geoms, lon: np.ndarray, lat: np.ndarray, step: float):
    """(fields x cells) sparse matrix: the share of each field's area (shapely geometries in
    degrees) inside each cell of ``step`` degrees centred on (``lon``, ``lat``)."""
    import shapely
    from scipy import sparse

    geoms = np.asarray(geoms, dtype=object)
    x0, y0 = float(lon.min()), float(lat.min())

    def index(v, origin):
        return np.floor((np.asarray(v) - origin) / step + 0.5).astype("int64")

    cells = {(i, j): k for k, (i, j) in enumerate(zip(index(lon, x0), index(lat, y0)))}
    rows, cols = [], []
    for g, (bx0, by0, bx1, by1) in enumerate(shapely.bounds(geoms)):
        for i in range(index(bx0, x0), index(bx1, x0) + 1):
            for j in range(index(by0, y0), index(by1, y0) + 1):
                k = cells.get((i, j))
                if k is not None:
                    rows.append(g)
                    cols.append(k)
    rows, cols = np.array(rows, dtype="int64"), np.array(cols, dtype="int64")
    w = np.zeros(0)
    if rows.size:
        h = step / 2
        boxes = shapely.box(lon[cols] - h, lat[cols] - h, lon[cols] + h, lat[cols] + h)
        w = shapely.area(shapely.intersection(geoms[rows], boxes)) / shapely.area(geoms[rows])
    keep = w > 1e-6
    return sparse.csr_matrix((w[keep], (rows[keep], cols[keep])), shape=(len(geoms), len(lon)))


def slots(var: str, start: dt.date, end: dt.date) -> pd.DatetimeIndex:
    """The dates of a variable over a period: every day, or the first day of each 10 days for DMP."""
    days = pd.date_range(start, end, freq="D")
    return days[days.day.isin((1, 11, 21))] if var == "dmp" else days


def field_values(weights, times: pd.DatetimeIndex, values: np.ndarray, dates: pd.DatetimeIndex) -> np.ndarray:
    """(fields x dates) values of the fields, from the ``values`` (store dates ``times`` x the cells
    of ``weights``' columns); NaN on dates not in the store."""
    pos = times.get_indexer(dates)
    have = pos >= 0
    out = np.full((weights.shape[0], len(dates)), np.nan)
    if have.any():
        v = values[pos[have]].astype("float64")
        ok = np.isfinite(v)
        num = weights @ np.where(ok, v, 0.0).T
        den = weights @ ok.T.astype("float64")
        with np.errstate(invalid="ignore", divide="ignore"):
            out[:, have] = np.where(den >= MIN_SHARE, num / den, np.nan)
    return out


def dmp_cumulative(series: np.ndarray, dates: pd.DatetimeIndex) -> np.ndarray:
    """(fields x dekads) DMP in kg/ha/day -> dry matter produced since the first dekad (t/ha); a
    missing dekad between two others counts as their mean."""
    filled = pd.DataFrame(np.atleast_2d(series)).interpolate(axis=1, limit_area="inside").to_numpy()
    days = np.array([dekad_days(d.date()) for d in dates], dtype="float64")
    return np.cumsum(np.nan_to_num(filled) * days, axis=1) / 1000


def field_stats(var: str, geoms, start: dt.date, end: dt.date, weights: dict | None = None,
                profiles: bool = False) -> dict | None:
    """Statistics of each field over a period, up to the last date published: mean, lowest and
    highest (and their dates), number of values, and the mean over the same period (to the same
    date) of the other years of the record (``reference``, ``reference_years``: their last year);
    for DMP, the dry matter produced (t/ha). With ``profiles``, the first field's series and the
    other years' mean, lowest and highest on each date.
    ``weights``: a cache of the fields' cell weights per grid. None when the store is missing."""
    g = grid(var)
    if g is None or not len(g.times):
        return None
    weights = {} if weights is None else weights
    if g.key not in weights:
        weights[g.key] = cell_weights(geoms, g.lon, g.lat, g.step)
    w = weights[g.key]
    cols = np.unique(w.indices)
    wc, values = w[:, cols], g.read(cols)
    dates = slots(var, start, end)
    n = int((dates <= g.times[-1]).sum())                # dates published so far
    cur = field_values(wc, g.times, values, dates)
    refs, years = [], []
    first, last = g.times[0].year - end.year - 1, g.times[-1].year - start.year + 1
    for k in [k for k in range(first, last + 1) if k]:   # the same period of the other years
        shifted = dates + pd.DateOffset(years=k)
        if shifted[0] >= g.times[0] and shifted[max(n, 1) - 1] <= g.times[-1]:
            refs.append(field_values(wc, g.times, values, shifted))
            years.append(end.year + k)
    head = cur[:, :max(n, 1)]                             # nothing published: one empty date
    finite = np.isfinite(head)
    count = finite.sum(axis=1)
    rows = np.arange(len(cur))
    imin = np.where(count > 0, np.argmin(np.where(finite, head, np.inf), axis=1), -1)
    imax = np.where(count > 0, np.argmax(np.where(finite, head, -np.inf), axis=1), -1)
    with quiet():
        out = {"var": var, "dates": dates, "published": n, "reference_years": years,
               "cells": np.diff(w.indptr), "covered": np.asarray(w.sum(axis=1)).ravel(), "n": count,
               "mean": np.nanmean(head, axis=1),
               "min": np.where(imin >= 0, head[rows, np.maximum(imin, 0)], np.nan),
               "min_date": [dates[i].date() if i >= 0 else None for i in imin],
               "max": np.where(imax >= 0, head[rows, np.maximum(imax, 0)], np.nan),
               "max_date": [dates[i].date() if i >= 0 else None for i in imax],
               "reference": (np.nanmean(np.stack([np.nanmean(r[:, :n], axis=1) for r in refs]), axis=0)
                             if refs and n else np.full(len(cur), np.nan))}
        if var == "dmp":
            total = lambda s: dmp_cumulative(s[:, :n], dates[:n])[:, -1] if n else np.zeros(len(s))  # noqa: E731
            out["total"] = np.where(count > 0, total(cur), np.nan)
            out["reference_total"] = (np.nanmean(np.stack([total(r) for r in refs]), axis=0)
                                      if refs else np.full(len(cur), np.nan))
        if profiles:
            stack = np.stack([r[0] for r in refs]) if refs else np.full((1, len(dates)), np.nan)
            out["profile"] = cur[0]
            out["profile_reference"] = {"mean": np.nanmean(stack, axis=0), "min": np.nanmin(stack, axis=0),
                                        "max": np.nanmax(stack, axis=0)}
            if var == "dmp":
                out["cumulative"] = np.where(np.arange(len(dates)) < n, dmp_cumulative(cur[0], dates)[0], np.nan)
                out["cumulative_reference"] = np.nanmean(dmp_cumulative(stack, dates), axis=0)
    return out
