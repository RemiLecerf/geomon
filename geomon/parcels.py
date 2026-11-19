"""Sentinel-2 index series per RPG parcel, smoothed seasonal curves, crop-group references.

Three levels, each derived from the previous one:

1. **Parcel-date statistics** (``eo.parcel_stats``, the history): for every stored
   Sentinel-2 date, the mean and standard deviation of each index over the
   interior of each RPG parcel (outline shrunk by 10 m, so that pixels mixing
   the parcel with a hedge, a road or the next field are left out), clear pixels
   only. A parcel-date is kept when at least half of its interior is clear; the
   series use it when GEOMON_S2_PARCEL_MIN_CLEAR (80 %) is.

2. **Parcel-season curves** (``eo.parcel_season``): per parcel and season, a
   daily curve. GEOMON_S2_PARCEL_SMOOTHING chooses how:
   - ``none`` (default): linear interpolation between the clear observations;
   - ``whittaker``: weighted Whittaker smoother (Eilers 2003), whose weights are
     lowered for observations far below the curve (residual cloud, haze and
     shadow lower the index), i.e. an upper envelope (Chen et al. 2004).
   Gaps of more than 60 days between observations are left empty.

3. **References** (computed on request): the curve of a crop group in an area
   for a season is the mean of its parcels' curves weighted by their interior
   pixels (the area measured), day by day; the 3-year,
   5-year and long-term references are the means of the group curves of the
   seasons before the one compared.

Season: the crop declared in RPG year Y is followed from 1 September Y-1 (autumn
sowing of winter crops) to 31 December Y (harvest of maize, sunflower). A date
of September to December thus belongs to two seasons. Day 0 is 1 September;
1 January Y is day 122.

Parcels change outline and identifier every year, so a season uses the parcels
of its own RPG year. The RPG of the current year is published a year later:
until then the latest RPG outlines are used, and its crops only for permanent
crops (permanent grassland, orchards, vines...); other crops of the season are
unknown.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from functools import lru_cache

import numpy as np
import pandas as pd
import xarray as xr

from . import sentinel2 as s2
from .arrays import quiet as _quiet
from .config import settings
from .db import connect

log = logging.getLogger(__name__)

SEASON_DAYS = 488            # 1 Sept Y-1 .. 31 Dec Y (leap year)
JAN1 = 122                   # day index of 1 January Y
EDGE_BUFFER_M = 10           # outline shrunk by one pixel
MIN_PIXELS = 4               # parcels with fewer interior pixels are ignored
STORE_MIN_CLEAR = 0.5        # parcel-dates stored when at least half of the interior is clear
MIN_OBS = 4                  # observations needed for a season curve
MAX_HALF_GAP = 30            # days farther than this from an observation stay empty
# The curve of a season is smoothed over the season extended by PAD days on both sides, then cut:
# its first and last days are then as well constrained as the others.
PAD = 45
LAMBDA = 100                 # Whittaker smoothing (daily grid, 2nd differences); chosen by leave-one-out
# RPG crop groups that stay from one year to the next (assumed for seasons without RPG yet)
PERMANENT_GROUPS = {"17", "18", "20", "21", "22", "23"}
# Peak of the season's crop: 1 January to 31 August (later, the next crop may have been sown)
PEAK_DAYS = (JAN1, JAN1 + 243)

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS eo;
CREATE TABLE IF NOT EXISTS eo.parcel_stats (
    site       text NOT NULL,
    rpg_year   int  NOT NULL,          -- RPG year of the parcel outline
    parcel_id  text NOT NULL,          -- RPG id_parcel (unique within a year only)
    index      text NOT NULL,
    date       date NOT NULL,
    mean       real,
    std        real,
    n_pixels   int,                    -- interior pixels (outline shrunk by 10 m)
    n_clear    int,                    -- clear interior pixels with a valid index
    PRIMARY KEY (site, rpg_year, index, parcel_id, date)
);
CREATE TABLE IF NOT EXISTS eo.parcel_dates (     -- parcel-dates processed (also when no parcel was clear)
    site text NOT NULL, rpg_year int NOT NULL, index text NOT NULL, date date NOT NULL,
    PRIMARY KEY (site, rpg_year, index, date)
);
CREATE TABLE IF NOT EXISTS eo.parcel_season (
    site        text NOT NULL,
    season      int  NOT NULL,         -- RPG year of the crop (1 Sept season-1 .. 31 Dec season)
    index       text NOT NULL,
    parcel_id   text NOT NULL,
    rpg_year    int  NOT NULL,         -- outline used (the season's RPG, or the latest one)
    n_obs       int,
    first_obs   date,
    last_obs    date,
    peak        real,
    peak_date   date,
    curve       bytea,                 -- float32 little-endian, SEASON_DAYS values, NaN when empty
    n_pixels    int,                   -- interior pixels of the parcel (weight in the group means)
    computed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site, season, index, parcel_id)
);
ALTER TABLE eo.parcel_season ADD COLUMN IF NOT EXISTS n_pixels int;
"""


# ---------------------------------------------------------------------------
# Seasons
# ---------------------------------------------------------------------------
def season_start(season: int) -> dt.date:
    return dt.date(season - 1, 9, 1)


def season_end(season: int) -> dt.date:
    return dt.date(season, 12, 31)


def seasons_of(d: dt.date, pad: int = 0) -> list[int]:
    """Seasons whose period (extended by ``pad`` days on both sides) contains the date."""
    p = dt.timedelta(days=pad)
    return [s for s in range(d.year - 1, d.year + 3) if season_start(s) - p <= d <= season_end(s) + p]


def day_of_season(d: dt.date, season: int) -> int:
    return (d - season_start(season)).days


def rpg_years(conn=None) -> list[int]:
    def q(c):
        return [r["year"] for r in c.execute("SELECT DISTINCT year FROM ref.rpg_parcel ORDER BY 1").fetchall()]
    if conn is not None:
        return q(conn)
    with connect() as c:
        return q(c)


def geometry_year(season: int, years: list[int]) -> int | None:
    """RPG year whose outlines are used for a season."""
    if not years:
        return None
    if season in years:
        return season
    return max(years) if season > max(years) else None


def current_seasons(today: dt.date | None = None) -> list[int]:
    return seasons_of(today or dt.date.today())


# ---------------------------------------------------------------------------
# Parcel rasters on the store grid
# ---------------------------------------------------------------------------
_LABELS: dict[tuple, tuple[np.ndarray, list[str], np.ndarray]] = {}


def _labels(site: str, rpg_year: int, ds: xr.Dataset) -> tuple[np.ndarray, list[str], np.ndarray]:
    """Parcel interiors rasterized on the store grid: label image (0 = none, k = ids[k-1]), ids,
    interior pixel count per label."""
    from pyproj import CRS
    from rasterio.features import rasterize
    from rasterio.transform import Affine
    from shapely import wkb

    x, y = ds["x"].values, ds["y"].values
    key = (site, rpg_year, float(x[0]), float(y[0]), len(x), len(y), ds.attrs["crs"])
    if key in _LABELS:
        return _LABELS[key]
    epsg = CRS.from_user_input(ds.attrs["crs"]).to_epsg()
    with connect() as conn:
        rows = conn.execute(
            """SELECT DISTINCT ON (p.parcel_id) p.parcel_id,
                      ST_AsBinary(ST_Buffer(ST_Transform(p.geom, %s), %s)) AS g
               FROM ref.rpg_parcel p
               JOIN meta.aoi a ON a.kind = 'site' AND a.code = %s AND ST_Intersects(ST_PointOnSurface(p.geom), a.geom)
               WHERE p.year = %s ORDER BY p.parcel_id, p.surf_ha DESC NULLS LAST""",
            (epsg, -EDGE_BUFFER_M, site, rpg_year),
        ).fetchall()
    ids, shapes = [], []
    for r in rows:
        g = wkb.loads(bytes(r["g"]))
        if g.is_empty:
            continue
        ids.append(r["parcel_id"])
        shapes.append((g, len(ids)))
    rx, ry = float(x[1] - x[0]), float(y[1] - y[0])
    aff = Affine(rx, 0, x[0] - rx / 2, 0, ry, y[0] - ry / 2)
    lab = rasterize(shapes, out_shape=(len(y), len(x)), transform=aff, fill=0, dtype="int32") if shapes \
        else np.zeros((len(y), len(x)), dtype="int32")
    npix = np.bincount(lab.ravel(), minlength=len(ids) + 1)
    _LABELS[key] = (lab, ids, npix)
    return _LABELS[key]


def clear_labels() -> None:
    _LABELS.clear()


# ---------------------------------------------------------------------------
# 1. Parcel-date statistics
# ---------------------------------------------------------------------------
def _date_rows(site: str, one: xr.Dataset, d: dt.date, todo: dict[int, list[str]]) -> list[tuple]:
    """Statistics of one loaded date for the (RPG year -> indices) pairs to do."""
    rows = []
    cache: dict[str, np.ndarray] = {}
    for gy, indices in todo.items():
        lab, ids, npix = _labels(site, gy, one)
        if not ids:
            continue
        for name in indices:
            v = cache.get(name)
            if v is None:
                v = cache[name] = s2.index_array(one, name)
            ok = np.isfinite(v) & (lab > 0)
            lb, val = lab[ok], v[ok].astype("float64")
            n = np.bincount(lb, minlength=len(ids) + 1)
            sm = np.bincount(lb, weights=val, minlength=len(ids) + 1)
            sq = np.bincount(lb, weights=val * val, minlength=len(ids) + 1)
            keep = np.nonzero((npix >= MIN_PIXELS) & (n >= STORE_MIN_CLEAR * npix) & (n > 0))[0]
            for k in keep[keep > 0]:
                mean = sm[k] / n[k]
                std = float(np.sqrt(max(sq[k] / n[k] - mean * mean, 0.0)))
                rows.append((site, gy, ids[k - 1], name, d, float(mean), std, int(npix[k]), int(n[k])))
    return rows


def _write_stats(site: str, rows: list[tuple], done: list[tuple]) -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.executemany("DELETE FROM eo.parcel_stats WHERE site = %s AND rpg_year = %s AND index = %s AND date = %s",
                            [(site, gy, ix, d) for gy, ix, d in done])
            with cur.copy("COPY eo.parcel_stats (site, rpg_year, parcel_id, index, date, mean, std, n_pixels, n_clear) "
                          "FROM STDIN") as cp:
                for r in rows:
                    cp.write_row(r)
            cur.executemany("INSERT INTO eo.parcel_dates (site, rpg_year, index, date) VALUES (%s, %s, %s, %s) "
                            "ON CONFLICT DO NOTHING", [(site, gy, ix, d) for gy, ix, d in done])


def _missing(site: str, years_rpg: list[int]) -> dict[dt.date, dict[int, list[str]]]:
    """Stored dates whose parcel statistics are missing, per RPG year and index."""
    with connect() as conn:
        have = {(r["rpg_year"], r["index"], r["date"]) for r in conn.execute(
            "SELECT rpg_year, index, date FROM eo.parcel_dates WHERE site = %s", (site,)).fetchall()}
    out: dict[dt.date, dict[int, list[str]]] = {}
    stores = [(part, y) for part in s2.parts(site) for y in s2.years(site, part)]
    for part, y in stores:
        ds = s2.open_year(site, y, part)
        indices = s2.available_indices(ds.data_vars)
        for t in ds["time"].values.astype("datetime64[D]").astype(object):
            gys = {geometry_year(s, years_rpg) for s in seasons_of(t, PAD)} - {None}
            for gy in gys:
                todo = [ix for ix in indices if (gy, ix, t) not in have]
                if todo:
                    out.setdefault(t, {}).setdefault(gy, [])
                    out[t][gy] = sorted(set(out[t][gy]) | set(todo))
    return out


def store_missing_stats(site: str) -> set[int]:
    """Computes the parcel statistics of every stored date not done yet; returns the seasons touched."""
    years_rpg = rpg_years()
    if not years_rpg:
        log.info("Parcels: no RPG loaded, skipped")
        return set()
    missing = _missing(site, years_rpg)
    if not missing:
        return set()
    log.info("Parcels: statistics of %d dates to compute", len(missing))
    touched: set[int] = set()
    by_year: dict[int, list[dt.date]] = {}
    for d in sorted(missing):
        by_year.setdefault(d.year, []).append(d)
    n_rows = 0
    for y, dates in by_year.items():
        stores = {part: s2.open_year(site, y, part) for part in s2.parts(site)}
        stores = {k: v for k, v in stores.items() if v is not None}
        times = {k: v["time"].values.astype("datetime64[D]") for k, v in stores.items()}
        rows, done = [], []
        for d in dates:
            for part, ds in stores.items():         # the parcels of each part are on its own grid
                hit = np.nonzero(times[part] == np.datetime64(d, "D"))[0]
                if hit.size:
                    rows += _date_rows(site, ds.isel(time=int(hit[0])).load(), d, missing[d])
            done += [(gy, ix, d) for gy, ixs in missing[d].items() for ix in ixs]
            touched |= {s for s in seasons_of(d, PAD) if geometry_year(s, years_rpg) in missing[d]}
        _write_stats(site, rows, done)
        n_rows += len(rows)
        log.info("Parcels %d: %d dates, %d parcel-dates", y, len(dates), len(rows))
    log.info("Parcels: %d parcel-dates stored", n_rows)
    return touched


# ---------------------------------------------------------------------------
# 2. Season curves
# ---------------------------------------------------------------------------
def _penalty(n: int) -> np.ndarray:
    """Upper banded form of D'D (second differences), for scipy.linalg.solveh_banded."""
    ab = np.zeros((3, n))
    main = np.full(n, 6.0)
    main[[0, -1]] = 1.0
    main[[1, -2]] = 5.0
    sup1 = np.full(n - 1, -4.0)
    sup1[[0, -1]] = -2.0
    ab[2] = main
    ab[1, 1:] = sup1
    ab[0, 2:] = 1.0
    return ab


_PEN = {}


def whittaker(t: np.ndarray, y: np.ndarray, w: np.ndarray, lam: float = LAMBDA, n: int = SEASON_DAYS) -> np.ndarray:
    """Weighted Whittaker smoother on a daily grid of n days; t = day indices of the observations."""
    from scipy.linalg import solveh_banded

    W = np.zeros(n)
    WY = np.zeros(n)
    np.add.at(W, t, w)
    np.add.at(WY, t, w * y)
    if n not in _PEN:
        _PEN[n] = _penalty(n)
    ab = lam * _PEN[n]
    ab[2] += W
    return solveh_banded(ab, WY)


def interpolate_season(t: np.ndarray, y: np.ndarray, n: int = SEASON_DAYS) -> np.ndarray | None:
    """Daily curve (n days): linear interpolation between the observations on days t (sorted);
    NaN before the first, after the last, and inside gaps longer than 2 x MAX_HALF_GAP days."""
    if len(t) < MIN_OBS:
        return None
    days = np.arange(n)
    z = np.interp(days, t, y)
    z[(days < t[0]) | (days > t[-1])] = np.nan
    for a, b in zip(t[:-1], t[1:]):
        if b - a > 2 * MAX_HALF_GAP:
            z[a + 1:b] = np.nan
    return z.astype("float32")


def season_curve(t: np.ndarray, y: np.ndarray, n: int = SEASON_DAYS) -> np.ndarray | None:
    """Daily curve of a parcel, with the method of GEOMON_S2_PARCEL_SMOOTHING."""
    if settings.s2_parcel_smoothing == "whittaker":
        return smooth_season(t, y, n=n)
    return interpolate_season(t, y, n)


def smooth_season(t: np.ndarray, y: np.ndarray, lam: float = LAMBDA, n: int = SEASON_DAYS) -> np.ndarray | None:
    """Daily curve (n days) from observations on days t, upper envelope, NaN far from observations."""
    if len(t) < MIN_OBS:
        return None
    w = np.ones(len(t))
    for _ in range(3):
        z = whittaker(t, y, w, lam, n)
        r = y - z[t]
        s = 1.4826 * np.median(np.abs(r)) + 0.01
        w = np.where(r < -2 * s, 0.1, 1.0)     # far below the curve: probably residual cloud or haze
    z = whittaker(t, y, w, lam, n)
    days = np.arange(n)
    pos = np.searchsorted(t, days)
    left = np.abs(days - t[np.clip(pos - 1, 0, len(t) - 1)])
    right = np.abs(t[np.clip(pos, 0, len(t) - 1)] - days)
    near = np.minimum(left, right)
    z[(near > MAX_HALF_GAP) | (days < t[0]) | (days > t[-1])] = np.nan
    return z.astype("float32")


def compute_seasons(site: str, seasons: set[int] | None = None) -> int:
    """(Re)computes the curves of the given seasons (all when None)."""
    years_rpg = rpg_years()
    if not years_rpg:
        return 0
    with connect() as conn:
        conn.execute(SCHEMA)
        if seasons is None:
            first = conn.execute("SELECT min(date) AS d FROM eo.parcel_dates WHERE site = %s", (site,)).fetchone()["d"]
            if first is None:
                return 0
            seasons = set(range(first.year, max(current_seasons()) + 1))
    n = 0
    for season in sorted(seasons):
        gy = geometry_year(season, years_rpg)
        if gy is None:
            continue
        with connect() as conn:
            obs = pd.DataFrame(conn.execute(
                """SELECT index, parcel_id, date, mean, n_pixels FROM eo.parcel_stats
                   WHERE site = %s AND rpg_year = %s AND date BETWEEN %s AND %s AND n_clear >= %s * n_pixels
                   ORDER BY index, parcel_id, date""",
                (site, gy, season_start(season) - dt.timedelta(PAD), season_end(season) + dt.timedelta(PAD),
                 settings.s2_parcel_min_clear),
            ).fetchall())
        rows = []
        if len(obs):
            obs["t"] = [(d - season_start(season)).days + PAD for d in obs["date"]]
            for (ix, pid), g in obs.groupby(["index", "parcel_id"], sort=False):
                t, y = g["t"].to_numpy(), g["mean"].to_numpy(dtype="float64")
                inside = t[(t >= PAD) & (t < PAD + SEASON_DAYS)] - PAD
                if len(inside) < MIN_OBS:
                    continue
                z = season_curve(t, y, n=SEASON_DAYS + 2 * PAD)
                if z is None:
                    continue
                z = z[PAD:PAD + SEASON_DAYS]
                z[np.arange(SEASON_DAYS) > (season_end(season) - season_start(season)).days] = np.nan  # 31 Dec
                if not np.isfinite(z).any():
                    continue
                main = z[PEAK_DAYS[0]:PEAK_DAYS[1]]
                k = PEAK_DAYS[0] + int(np.nanargmax(main)) if np.isfinite(main).any() else None  # none yet
                rows.append((site, season, ix, pid, gy, len(inside), season_start(season) + dt.timedelta(int(inside[0])),
                             season_start(season) + dt.timedelta(int(inside[-1])), None if k is None else float(z[k]),
                             None if k is None else season_start(season) + dt.timedelta(k),
                             z.astype("<f4").tobytes(), int(g["n_pixels"].max())))
        with connect() as conn:
            conn.execute("DELETE FROM eo.parcel_season WHERE site = %s AND season = %s", (site, season))
            with conn.cursor() as cur:
                with cur.copy("COPY eo.parcel_season (site, season, index, parcel_id, rpg_year, n_obs, first_obs, "
                              "last_obs, peak, peak_date, curve, n_pixels) FROM STDIN") as cp:
                    for r in rows:
                        cp.write_row(r)
        n += len(rows)
        log.info("Parcels: season %d (RPG %d outlines), %d parcel curves", season, gy, len(rows))
    return n


def sync(site: str, full: bool = False) -> dict:
    """Parcel statistics of new dates (or of every date when ``full``), then the curves of the seasons touched.

    Seasons whose RPG outlines changed (a new RPG year loaded) are recomputed as well.
    """
    with connect() as conn:
        conn.execute(SCHEMA)
        if full:
            for t in ("parcel_stats", "parcel_dates", "parcel_season"):
                conn.execute(f"DELETE FROM eo.{t} WHERE site = %s", (site,))
    clear_labels()
    touched = store_missing_stats(site)
    years_rpg = rpg_years()
    with connect() as conn:
        stale = {r["season"] for r in conn.execute(
            "SELECT DISTINCT season, rpg_year FROM eo.parcel_season WHERE site = %s", (site,)).fetchall()
            if geometry_year(r["season"], years_rpg) != r["rpg_year"]}
        if stale:
            conn.execute("DELETE FROM eo.parcel_season WHERE site = %s AND season = ANY(%s)", (site, list(stale)))
    seasons = None if full else touched | stale
    n = compute_seasons(site, seasons) if (full or seasons) else 0
    return {"seasons": sorted(seasons) if seasons is not None else "all", "curves": n}


# ---------------------------------------------------------------------------
# 3. Reading: curves, group references
# ---------------------------------------------------------------------------
def stamp(site: str) -> str:
    with connect() as conn:
        exists = conn.execute("SELECT to_regclass('eo.parcel_season') AS t").fetchone()["t"]
        if not exists:
            return ""
        r = conn.execute("SELECT max(computed_at) AS m, count(*) AS n FROM eo.parcel_season WHERE site = %s",
                         (site,)).fetchone()
    return f"{r['m']}/{r['n']}"


@lru_cache(maxsize=4)
def curves(site: str, index: str, _stamp: str) -> tuple[pd.DataFrame, np.ndarray]:
    """Every parcel curve of an index: table (one row per parcel and season, with its crop and areas)
    and the matrix of curves (rows in the same order)."""
    import shapely
    from shapely.geometry import shape

    with connect() as conn:
        # One indexed lookup per parcel (year, id), with a point inside it; no spatial join in SQL,
        # whose plan proved fragile on large sites
        rows = conn.execute(
            """SELECT c.season, c.parcel_id, c.rpg_year, c.n_obs, c.n_pixels, c.first_obs, c.last_obs, c.peak,
                      c.peak_date, c.curve, p.code_group, p.code_cultu, p.surf_ha, p.bio, p.px, p.py
               FROM eo.parcel_season c
               JOIN LATERAL (SELECT q.code_group, q.code_cultu, q.surf_ha, q.bio,
                                    ST_X(ST_PointOnSurface(q.geom)) AS px, ST_Y(ST_PointOnSurface(q.geom)) AS py
                             FROM ref.rpg_parcel q WHERE q.year = c.rpg_year AND q.parcel_id = c.parcel_id
                             ORDER BY q.surf_ha DESC NULLS LAST LIMIT 1) p ON true
               WHERE c.site = %s AND c.index = %s
               ORDER BY c.season, c.parcel_id""",
            (site, index),
        ).fetchall()
        areas = conn.execute("SELECT code, ST_AsGeoJSON(geom) AS g FROM meta.aoi WHERE site = %s",
                             (site,)).fetchall()
    # Areas of each parcel: those containing its point (vectorised)
    if rows:
        xs = np.array([r["px"] for r in rows], dtype="float64")
        ys = np.array([r["py"] for r in rows], dtype="float64")
        inside = {a["code"]: shapely.contains_xy(shape(json.loads(a["g"])), xs, ys) for a in areas}
        for i, r in enumerate(rows):
            r["areas"] = [code for code, m in inside.items() if m[i]]
    mat = np.stack([np.frombuffer(r["curve"], dtype="<f4") for r in rows]) if rows \
        else np.zeros((0, SEASON_DAYS), dtype="float32")
    df = pd.DataFrame([{k: v for k, v in r.items() if k not in ("curve", "px", "py")} for r in rows])
    if len(df):
        df["crop_status"] = np.where(df["season"] == df["rpg_year"], "declared",
                                     np.where(df["code_group"].isin(PERMANENT_GROUPS), "assumed", "unknown"))
        df["areas"] = df["areas"].apply(lambda a: set(a or []))
    return df, mat


def clear_cache() -> None:
    curves.cache_clear()
    clear_labels()


def _mean_curve(mat: np.ndarray, min_n: int, w: np.ndarray | None = None
                ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Mean (weighted by w when given), 25th and 75th percentiles across rows, day by day, where at
    least min_n rows have a value."""
    if not len(mat):
        nan = np.full(SEASON_DAYS, np.nan)
        return nan, nan, nan, np.zeros(SEASON_DAYS, int)
    cnt = np.isfinite(mat).sum(axis=0)
    with np.errstate(all="ignore"), _quiet():
        if w is None:
            med = np.nanmean(mat, axis=0)
        else:
            ok = np.isfinite(mat)
            ww = np.where(ok, w[:, None], 0.0)
            med = (np.where(ok, mat, 0.0) * ww).sum(axis=0) / ww.sum(axis=0)
        q25, q75 = np.nanpercentile(mat, [25, 75], axis=0)
    bad = cnt < min_n
    for a in (med, q25, q75):
        a[bad] = np.nan
    return med, q25, q75, cnt


def group_curves(site: str, index: str, area: str, group: str, weighted: bool = True) -> dict[int, dict]:
    """Per season: the group curve in an area (mean of its parcels' curves, weighted by their interior
    pixels unless ``weighted`` is False), with parcel counts."""
    df, mat = curves(site, index, stamp(site))
    if not len(df):
        return {}
    sel = (df["code_group"] == group) & (df["crop_status"] != "unknown") & df["areas"].apply(lambda a: area in a)
    out = {}
    for season, idx in df[sel].groupby("season").groups.items():
        rows = np.asarray(list(idx))
        sub = mat[rows]
        w = df.loc[rows, "n_pixels"].fillna(1).to_numpy(dtype="float64") if weighted else None
        mean, q25, q75, cnt = _mean_curve(sub, min_n=min(3, len(rows)), w=w)
        out[int(season)] = {"mean": mean, "q25": q25, "q75": q75, "count": cnt, "parcels": len(rows),
                            "ha": float(df.loc[rows, "surf_ha"].fillna(0).sum()),
                            "status": "declared" if (df.loc[rows, "crop_status"] == "declared").all() else "assumed"}
    return out


def references(curves_by_season: dict[int, np.ndarray], season: int) -> dict[str, dict]:
    """3-year, 5-year and long-term means of the curves of the seasons before ``season``."""
    prior = sorted(s for s in curves_by_season if s < season)
    out = {}
    for key, span, min_n in (("3y", 3, 2), ("5y", 5, 3), ("lt", None, 3)):
        used = [s for s in prior if span is None or s >= season - span]
        if len(used) < min_n:
            continue
        m = np.stack([curves_by_season[s] for s in used])
        cnt = np.isfinite(m).sum(axis=0)
        with _quiet():
            mean = np.nanmean(m, axis=0)
            ref = {"seasons": used, "mean": np.where(cnt >= min_n, mean, np.nan)}
            if key == "lt":
                p10, p90 = np.nanpercentile(m, [10, 90], axis=0)
                ok = cnt >= max(min_n, 5)
                ref |= {"p10": np.where(ok, p10, np.nan), "p90": np.where(ok, p90, np.nan)}
        out[key] = ref
    return out


def compare(current: np.ndarray, refs: dict[str, dict], prior: dict[int, np.ndarray], season: int) -> dict | None:
    """Current season against the references, on its last day with a value and since 1 January."""
    days = np.nonzero(np.isfinite(current))[0]
    if not len(days):
        return None
    d = int(days[-1])
    out = {"day": d, "date": str(season_start(season) + dt.timedelta(d)), "value": float(current[d]), "refs": {}}
    jan = np.arange(JAN1, d + 1)
    for k, r in refs.items():
        v = r["mean"][d]
        row = {"value": None if np.isnan(v) else float(v),
               "difference": None if np.isnan(v) else float(current[d] - v)}
        if len(jan):
            both = jan[np.isfinite(current[jan]) & np.isfinite(r["mean"][jan])]
            if len(both) >= 15:
                row["mean_since_jan1"] = float(r["mean"][both].mean())
                row["current_mean_since_jan1"] = float(current[both].mean())
        out["refs"][k] = row
    past = [s for s in prior if s < season and np.isfinite(prior[s][d])]
    if past:
        out["rank"] = {"below": int(sum(prior[s][d] < current[d] for s in past)), "seasons": len(past)}
    return out


def nan_list(a: np.ndarray, nd: int = 3) -> list:
    return [None if not np.isfinite(v) else round(float(v), nd) for v in a]
