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
# Tables: eo.parcel_stats, eo.parcel_dates, eo.parcel_season (geomon/schema.sql)


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
    if seasons is None:
        with connect() as conn:
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
    if full:
        with connect() as conn:
            for t in ("parcel_stats", "parcel_dates", "parcel_season", "parcel_info", "group_season"):
                conn.execute(f"DELETE FROM eo.{t} WHERE site = %s", (site,))
    clear_labels()
    try:
        touched = store_missing_stats(site)
    finally:
        clear_labels()      # parcel rasters of every RPG year: not kept by the long-running scheduler
    years_rpg = rpg_years()
    with connect() as conn:
        stale = {r["season"] for r in conn.execute(
            "SELECT DISTINCT season, rpg_year FROM eo.parcel_season WHERE site = %s", (site,)).fetchall()
            if geometry_year(r["season"], years_rpg) != r["rpg_year"]}
        if stale:
            conn.execute("DELETE FROM eo.parcel_season WHERE site = %s AND season = ANY(%s)", (site, list(stale)))
    seasons = None if full else touched | stale
    n = compute_seasons(site, seasons) if (full or seasons) else 0
    # Crop-group curves of the seasons recomputed (all of them when none was computed yet)
    with connect() as conn:
        backfill = (conn.execute("SELECT 1 FROM eo.parcel_season WHERE site = %s LIMIT 1", (site,)).fetchone()
                    and not conn.execute("SELECT 1 FROM eo.group_season WHERE site = %s LIMIT 1", (site,)).fetchone())
    groups = compute_groups(site, None if (full or backfill) else seasons) if (full or seasons or backfill) else 0
    return {"seasons": sorted(seasons) if seasons is not None else "all", "curves": n, "group_curves": groups}


# ---------------------------------------------------------------------------
# 3. Crop-group curves per area, computed at ingestion (eo.parcel_info, eo.group_season)
# ---------------------------------------------------------------------------
def crop_status(season: int, rpg_year: int, code_group: str | None) -> str:
    """declared (the season's own RPG), assumed (a permanent crop taken from the latest RPG) or unknown."""
    if season == rpg_year:
        return "declared"
    return "assumed" if code_group in PERMANENT_GROUPS else "unknown"


def refresh_parcel_info(site: str, years) -> int:
    """eo.parcel_info for the given RPG years: each parcel whose point on surface lies in one of the
    site's areas, with its crop and those areas (point-in-polygon in Python, vectorised: a spatial
    join in SQL proved fragile on large sites)."""
    import shapely
    from shapely.geometry import shape

    with connect() as conn:
        areas = {r["code"]: shape(json.loads(r["g"])) for r in conn.execute(
            "SELECT code, ST_AsGeoJSON(geom) AS g FROM meta.aoi WHERE site = %s", (site,)).fetchall()}
    for g in areas.values():
        shapely.prepare(g)
    n = 0
    for gy in sorted(years):
        with connect() as conn:
            rows = conn.execute(
                """SELECT DISTINCT ON (p.parcel_id) p.parcel_id, p.code_group, p.code_cultu, p.surf_ha, p.bio,
                          ST_X(ST_PointOnSurface(p.geom)) AS px, ST_Y(ST_PointOnSurface(p.geom)) AS py
                   FROM ref.rpg_parcel p JOIN meta.aoi s ON s.kind = 'site' AND s.code = %s AND p.geom && s.bbox
                   WHERE p.year = %s ORDER BY p.parcel_id, p.surf_ha DESC NULLS LAST""", (site, gy)).fetchall()
            xs = np.array([r["px"] for r in rows], dtype="float64")
            ys = np.array([r["py"] for r in rows], dtype="float64")
            inside = {code: shapely.contains_xy(g, xs, ys) for code, g in areas.items()} if rows else {}
            conn.execute("DELETE FROM eo.parcel_info WHERE site = %s AND rpg_year = %s", (site, gy))
            with conn.cursor() as cur:
                with cur.copy("COPY eo.parcel_info (site, rpg_year, parcel_id, code_group, code_cultu, surf_ha, bio, "
                              "areas) FROM STDIN") as cp:
                    for i, r in enumerate(rows):
                        codes = [code for code, m in inside.items() if m[i]]
                        if codes:
                            cp.write_row((site, gy, r["parcel_id"], r["code_group"], r["code_cultu"], r["surf_ha"],
                                          r["bio"], codes))
                            n += 1
    return n


def _group_curve(mat: np.ndarray, w: np.ndarray) -> tuple[np.ndarray, ...]:
    """Mean weighted by w, plain mean, 25th and 75th percentiles and count, day by day, where at
    least min(3, rows) rows have a value."""
    min_n = min(3, len(mat))
    cnt = np.isfinite(mat).sum(axis=0)
    with np.errstate(all="ignore"), _quiet():
        ok = np.isfinite(mat)
        ww = np.where(ok, w[:, None], 0.0)
        weighted = (np.where(ok, mat, 0.0) * ww).sum(axis=0) / ww.sum(axis=0)
        plain = np.nanmean(mat, axis=0)
        q25, q75 = np.nanpercentile(mat, [25, 75], axis=0)
    bad = cnt < min_n
    for a in (weighted, plain, q25, q75):
        a[bad] = np.nan
    return weighted, plain, q25, q75, cnt


def _f32(a: np.ndarray) -> bytes:
    return np.asarray(a, dtype="<f4").tobytes()


def compute_groups(site: str, seasons=None) -> int:
    """(Re)computes the crop-group curves of every area of the site for the given seasons (all when
    None): mean of the parcels' curves, weighted by their interior pixels and one parcel one vote,
    the middle half of the parcels, counts. Parcels of an unknown crop are only counted."""
    years_rpg = rpg_years()
    with connect() as conn:
        if seasons is None:
            seasons = {r["season"] for r in conn.execute(
                "SELECT DISTINCT season FROM eo.parcel_season WHERE site = %s", (site,)).fetchall()}
    seasons = sorted(seasons)
    refresh_parcel_info(site, {geometry_year(s, years_rpg) for s in seasons} - {None})
    n = 0
    for season in seasons:
        with connect() as conn:
            rows = conn.execute(
                """SELECT c.index, c.rpg_year, c.n_pixels, c.curve, i.code_group, i.surf_ha, i.areas
                   FROM eo.parcel_season c
                   JOIN eo.parcel_info i ON i.site = c.site AND i.rpg_year = c.rpg_year AND i.parcel_id = c.parcel_id
                   WHERE c.site = %s AND c.season = %s ORDER BY c.index, c.parcel_id""", (site, season)).fetchall()
        out = []
        for ix in sorted({r["index"] for r in rows}):
            rs = [r for r in rows if r["index"] == ix]
            mat = np.stack([np.frombuffer(r["curve"], dtype="<f4") for r in rs]).astype("float64")
            status = np.array([crop_status(season, r["rpg_year"], r["code_group"]) for r in rs])
            group = np.array([r["code_group"] or "" for r in rs])
            ha = np.array([r["surf_ha"] or 0.0 for r in rs])
            w = np.array([r["n_pixels"] or 1 for r in rs], dtype="float64")
            members: dict[str, list[int]] = {}
            for k, r in enumerate(rs):
                for code in r["areas"]:
                    members.setdefault(code, []).append(k)
            for area, idx in members.items():
                idx = np.asarray(idx)
                unknown = idx[status[idx] == "unknown"]
                if unknown.size:
                    out.append((site, ix, area, "", season, int(unknown.size), float(ha[unknown].sum()), "unknown",
                                None, None, None, None, None))
                known = idx[status[idx] != "unknown"]
                for g in sorted(set(group[known]) - {""}):
                    sel = known[group[known] == g]
                    weighted, plain, q25, q75, cnt = _group_curve(mat[sel], w[sel])
                    st = "declared" if (status[sel] == "declared").all() else "assumed"
                    out.append((site, ix, area, g, season, int(sel.size), float(ha[sel].sum()), st, _f32(weighted),
                                _f32(plain), _f32(q25), _f32(q75), cnt.astype("<i2").tobytes()))
        with connect() as conn:
            conn.execute("DELETE FROM eo.group_season WHERE site = %s AND season = %s", (site, season))
            with conn.cursor() as cur:
                with cur.copy("COPY eo.group_season (site, index, area, code_group, season, parcels, ha, status, "
                              "mean_pixels, mean_parcel, q25, q75, count) FROM STDIN") as cp:
                    for r in out:
                        cp.write_row(r)
        n += len(out)
        log.info("Parcels: season %d, %d crop-group curves", season, len(out))
    return n


def _curve(b: bytes | None) -> np.ndarray:
    return np.frombuffer(b, dtype="<f4").astype("float64") if b is not None else np.full(SEASON_DAYS, np.nan)


def group_curves(site: str, index: str, area: str, group: str, weighted: bool = True) -> dict[int, dict]:
    """Per season: the group curve in an area (mean of its parcels' curves, weighted by their interior
    pixels unless ``weighted`` is False), with parcel counts (from eo.group_season)."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT season, parcels, ha, status, mean_pixels, mean_parcel, q25, q75, count FROM eo.group_season
               WHERE site = %s AND index = %s AND area = %s AND code_group = %s ORDER BY season""",
            (site, index, area, group)).fetchall()
    return {r["season"]: {"mean": _curve(r["mean_pixels" if weighted else "mean_parcel"]), "q25": _curve(r["q25"]),
                          "q75": _curve(r["q75"]), "count": np.frombuffer(r["count"], dtype="<i2").astype(int),
                          "parcels": r["parcels"], "ha": r["ha"], "status": r["status"]}
            for r in rows}


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
