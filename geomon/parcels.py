"""Sentinel-2 index series per RPG parcel, smoothed seasonal curves, crop-group references.

Three levels, each derived from the previous one:

1. **Parcel-date statistics** (geomon.fields: one Zarr store per RPG year and one for the farm's
   fields, ``data/zarr/sites/<site>/fields/sentinel2/<layer>.zarr``): for every stored Sentinel-2
   date, the mean and standard deviation of each index over each parcel and the share of its area
   with a clear value, by geomon.zonal: every pixel weighted by its share inside the parcel's
   outline shrunk by 10 m (pixels mixing the parcel with a hedge, a road or the next field left
   out), or inside its whole outline when less than MIN_PIXELS pixels' area would remain (``edge``).
   The series use a date when GEOMON_S2_PARCEL_MIN_CLEAR (80 %) of the area is clear.

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

The farm's own land cover (one file per year, the site's ``farm_landcover``, ref.farm_landcover) is
used for its plots: in a season with a land-cover file, the RPG parcels whose point on surface lies in
one of the farm's plots take the farm's crop (status ``farm``, eo.farm_crop), instead of the RPG's
declaration or of an unknown crop (RPG not published yet).

Better still, the farm's fields have their own series: their outlines (shrunk by 10 m, as the RPG
parcels) are the parcels of a layer of their own, ``rpg_year`` 0 (FARM_LAYER): its store ``farm``
(geomon.fields), eo.parcel_season and parcel_info, with ids 'F' + a hash of the outline. In a season with a land-cover
file, its fields replace the RPG parcels lying in them (eo.farm_crop: the fields with their crop, and
the replaced RPG parcels without one), in every area: the communes and the group as the farm's own
("My plots"), which only has curves for those seasons, from its fields.
"""

from __future__ import annotations

import datetime as dt
import json
import logging

import numpy as np
import pandas as pd
import xarray as xr

from . import fields, zonal
from . import sentinel2 as s2
from .arrays import quiet as _quiet
from .config import settings
from .db import connect

log = logging.getLogger(__name__)

SEASON_DAYS = 488            # 1 Sept Y-1 .. 31 Dec Y (leap year)
JAN1 = 122                   # day index of 1 January Y
EDGE_BUFFER_M = 10           # outline shrunk by one pixel
MIN_PIXELS = 4               # parcels smaller than this many pixels' area are ignored; below it once
                             # shrunk, a parcel is followed on its whole outline
STORE_MIN_CLEAR = 0.5        # parcel-dates shown (not used) on the parcel's chart from half clear
PRODUCT = "sentinel2"        # its stores in geomon.fields
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
FARM_LAYER = 0               # rpg_year of the farm's own fields (ref.farm_landcover.field_id)
# Tables: eo.parcel_season, eo.parcel_info, eo.group_season (geomon/schema.sql); the parcel-date
# statistics are Zarr stores (geomon.fields)


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


_LATEST: list = [0.0, None]


def latest_rpg_year() -> int | None:
    """The latest RPG year loaded (cached a minute: every map tile asks for it)."""
    import time

    if time.monotonic() - _LATEST[0] > 60:
        years = rpg_years()
        _LATEST[:] = [time.monotonic(), years[-1] if years else None]
    return _LATEST[1]


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
        if rpg_year == FARM_LAYER:          # the farm's fields, every year's (one series per outline)
            rows = conn.execute(
                """SELECT DISTINCT ON (field_id) field_id AS parcel_id,
                          ST_AsBinary(ST_Buffer(ST_Transform(geom, %s), %s)) AS g
                   FROM ref.farm_landcover WHERE site = %s AND field_id IS NOT NULL ORDER BY field_id""",
                (epsg, -EDGE_BUFFER_M, site)).fetchall()
        else:
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
# 1. Parcel-date statistics (geomon.zonal, stored by geomon.fields)
# ---------------------------------------------------------------------------
def layer_name(rpg_year: int) -> str:
    return "farm" if rpg_year == FARM_LAYER else f"rpg{rpg_year}"


def layer_year(name: str) -> int:
    return FARM_LAYER if name == "farm" else int(name[3:])


def _layer_sql(rpg_year: int) -> str:
    """The fields of a layer (id, outline): an RPG year's parcels over the site (their point on surface
    in it), or the farm's fields (FARM_LAYER: every year's outlines, one series per outline)."""
    if rpg_year == FARM_LAYER:
        return """SELECT DISTINCT ON (field_id) field_id AS id, geom FROM ref.farm_landcover
                  WHERE site = %(site)s AND field_id IS NOT NULL ORDER BY field_id, year DESC"""
    return """SELECT DISTINCT ON (p.parcel_id) p.parcel_id AS id, p.geom FROM ref.rpg_parcel p
              JOIN meta.aoi a ON a.kind = 'site' AND a.code = %(site)s AND ST_Intersects(ST_PointOnSurface(p.geom), a.geom)
              WHERE p.year = %(year)s ORDER BY p.parcel_id, p.surf_ha DESC NULLS LAST"""


def layer_version(site: str, rpg_year: int) -> str:
    """A layer's version (its ids and outlines), computed in the database without reading them."""
    with connect() as conn:
        r = conn.execute(f"""SELECT md5(string_agg(id || ':' || md5(ST_AsBinary(geom)), ',' ORDER BY id)) AS v
                             FROM ({_layer_sql(rpg_year)}) f""", {"site": site, "year": rpg_year}).fetchone()
    return f"{layer_name(rpg_year)}-{(r['v'] or 'empty')[:16]}"


def field_layer(site: str, rpg_year: int) -> zonal.Layer:
    """The fields of a layer with their outlines (see :func:`_layer_sql`)."""
    with connect() as conn:
        rows = conn.execute(f"SELECT id, ST_AsBinary(geom) AS g FROM ({_layer_sql(rpg_year)}) f ORDER BY id",
                            {"site": site, "year": rpg_year}).fetchall()
    return zonal.Layer(layer_name(rpg_year), tuple(r["id"] for r in rows), tuple(bytes(r["g"]) for r in rows),
                       version=layer_version(site, rpg_year))


def _layers(site: str):
    """Memos of the site's layer versions and of its layers (outlines read when first needed)."""
    versions: dict[int, str] = {}
    memo: dict[int, zonal.Layer] = {}

    def version(rpg_year: int) -> str:
        if rpg_year not in versions:
            versions[rpg_year] = memo[rpg_year].key if rpg_year in memo else layer_version(site, rpg_year)
        return versions[rpg_year]

    def get(rpg_year: int) -> zonal.Layer:
        if rpg_year not in memo:
            memo[rpg_year] = field_layer(site, rpg_year)
        return memo[rpg_year]
    get.version = version
    return get


def _missing(site: str, years_rpg: list[int], layer_of) -> dict[dt.date, list[int]]:
    """Stored dates not aggregated yet for a layer that uses them (for one of its indices, or for a
    store of an older version of the layer): date -> RPG years (FARM_LAYER: the farm's fields)."""
    farm = set(farm_years(site))
    have: dict[int, dict[str, set]] = {}

    def done(gy: int) -> dict[str, set]:
        if gy not in have:
            ds = fields.open_store(site, PRODUCT, layer_name(gy))
            current = ds is not None and ds.attrs.get("layer_key") == layer_of.version(gy)
            have[gy] = {ix: fields.done(ds, ix) for ix in s2.INDICES} if current else {}
        return have[gy]

    out: dict[dt.date, list[int]] = {}
    for part in s2.parts(site):
        for y in s2.years(site, part):
            ds = s2.open_year(site, y, part)
            indices = s2.available_indices(ds.data_vars)
            for t in ds["time"].values.astype("datetime64[D]"):
                d = t.astype(object)
                gys = {geometry_year(s, years_rpg) for s in seasons_of(d, PAD)} - {None}
                if farm & set(seasons_of(d, PAD)):
                    gys.add(FARM_LAYER)
                for gy in gys:
                    if any(t not in done(gy).get(ix, ()) for ix in indices) and gy not in out.get(d, []):
                        out.setdefault(d, []).append(gy)
    return out


def store_missing_stats(site: str) -> set[int]:
    """Aggregates every stored date not done yet for the layers using it (all the indices of the
    date, every part of the site summed); returns the seasons touched."""
    years_rpg = rpg_years()
    if not years_rpg:
        log.info("Parcels: no RPG loaded, skipped")
        return set()
    layer_of = _layers(site)
    missing = _missing(site, years_rpg, layer_of)
    if not missing:
        return set()
    farm = set(farm_years(site))
    log.info("Parcels: %d dates to aggregate", len(missing))
    touched: set[int] = set()
    by_year: dict[int, list[dt.date]] = {}
    for d in sorted(missing):
        by_year.setdefault(d.year, []).append(d)
    for y, dates in by_year.items():
        stores = {part: s2.open_year(site, y, part) for part in s2.parts(site)}
        stores = {k: v for k, v in stores.items() if v is not None}
        times = {k: v["time"].values.astype("datetime64[D]") for k, v in stores.items()}
        grids = {k: zonal.Grid.of(v) for k, v in stores.items()}

        def cover(gy: int, part: str) -> zonal.Coverage:
            return zonal.coverage(layer_of(gy), grids[part], EDGE_BUFFER_M, MIN_PIXELS)

        sums: dict[int, dict[dt.date, list]] = {}      # RPG year -> date -> [num, sq, den] (fields x indices)
        indices_of: dict[dt.date, list[str]] = {}
        for d in dates:
            for part, ds in stores.items():               # each part on its own grid: the sums add
                hit = np.nonzero(times[part] == np.datetime64(d, "D"))[0]
                if not hit.size:
                    continue
                one = ds.isel(time=int(hit[0])).load()
                indices = indices_of.setdefault(d, s2.available_indices(one.data_vars))
                values = np.stack([s2.index_array(one, ix).ravel() for ix in indices])
                for gy in missing[d]:
                    r = cover(gy, part).sums(values)
                    acc = sums.setdefault(gy, {}).get(d)
                    sums[gy][d] = list(r) if acc is None else [a + b for a, b in zip(acc, r)]
            touched |= {s for s in seasons_of(d, PAD) if geometry_year(s, years_rpg) in missing[d]
                        or (FARM_LAYER in missing[d] and s in farm)}
        for gy, by_date in sums.items():
            layer = layer_of(gy)
            covers = [cover(gy, part) for part in stores]
            total = np.sum([c.cells for c in covers], axis=0)
            edge = np.any([c.edge for c in covers], axis=0)
            days = sorted(by_date)
            if any(indices_of[d] != indices_of[days[0]] for d in days):
                raise RuntimeError(f"Sentinel-2 {y}: the parts or dates of a year hold different bands")
            data = {}
            for k, ix in enumerate(indices_of[days[0]]):
                cols = [by_date[d] for d in days]
                num, sq, den = (np.stack([c[j][:, k] for c in cols], axis=1) for j in range(3))
                data[ix] = zonal.finish(num, sq, den, total)
            fields.merge(site, PRODUCT, layer, total, edge, [np.datetime64(d, "D") for d in days], data)
            log.info("Parcels %d, %s: %d dates aggregated over %d fields", y, layer.name, len(days), len(layer))
    return touched


def _season_observations(site: str, season: int, gy: int, farm_fields: list[str]) -> pd.DataFrame:
    """The clear observations of a season (with PAD days on both sides) on its layer and the farm's
    fields of its land cover: index, rpg_year, parcel_id, date, mean, n_pixels (the area followed)."""
    d0, d1 = season_start(season) - dt.timedelta(PAD), season_end(season) + dt.timedelta(PAD)
    frames = []
    for ry, ids in ((gy, None), (FARM_LAYER, farm_fields)):
        if ry == FARM_LAYER and not ids:
            continue
        for ix in s2.INDICES:
            tab = fields.table(site, PRODUCT, layer_name(ry), ix, d0, d1, ids)
            if tab is None:
                continue
            dates, fids, mean, valid, cells = tab
            with np.errstate(invalid="ignore"):
                ok = (valid >= settings.s2_parcel_min_clear) & np.isfinite(mean) & (cells >= MIN_PIXELS)[None, :]
            ti, fi = np.nonzero(ok)
            frames.append(pd.DataFrame({"index": ix, "rpg_year": ry, "parcel_id": fids[fi], "date": dates[ti],
                                        "mean": mean[ti, fi].astype("float64"), "n_pixels": cells[fi]}))
    if not frames:
        return pd.DataFrame(columns=["index", "rpg_year", "parcel_id", "date", "mean", "n_pixels"])
    return pd.concat(frames, ignore_index=True).sort_values(["index", "rpg_year", "parcel_id", "date"], kind="stable")


def field_observations(site: str, index: str, rpg_year: int, parcel_id: str, d0: dt.date, d1: dt.date) -> pd.DataFrame:
    """A parcel's (or the farm's field's, FARM_LAYER) dates over a period: date, mean, std, valid."""
    return fields.series(site, PRODUCT, layer_name(rpg_year), parcel_id, index, d0, d1)


def group_observations(site: str, index: str, members: dict[int, list[str]], d0: dt.date, d1: dt.date,
                       weighted: bool = True) -> list[tuple[dt.date, float, int]]:
    """Per date, the mean of the clear observations of some parcels (RPG year -> ids), weighted by
    their area (``weighted``) or one parcel one vote, and their number."""
    acc: dict[dt.date, list[float]] = {}
    for ry, ids in members.items():
        tab = fields.table(site, PRODUCT, layer_name(ry), index, d0, d1, ids) if ids else None
        if tab is None:
            continue
        dates, _, mean, valid, cells = tab
        with np.errstate(invalid="ignore"):
            ok = (valid >= settings.s2_parcel_min_clear) & np.isfinite(mean)
        w = np.where(ok, cells[None, :] if weighted else 1.0, 0.0)
        s, n, c = (np.where(ok, mean, 0.0) * w).sum(axis=1), w.sum(axis=1), ok.sum(axis=1)
        for d, a, b, k in zip(dates, s, n, c):
            if k:
                v = acc.setdefault(d, [0.0, 0.0, 0])
                v[0], v[1], v[2] = v[0] + a, v[1] + b, v[2] + int(k)
    return [(d, v[0] / v[1], v[2]) for d, v in sorted(acc.items()) if v[1] > 0]


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
        stored = s2.years(site)
        if not stored:
            return 0
        seasons = set(range(stored[0], max(current_seasons()) + 1))
    n = 0
    for season in sorted(seasons):
        gy = geometry_year(season, years_rpg)
        if gy is None:
            continue
        obs = _season_observations(site, season, gy, season_fields(site, season))
        rows = []
        if len(obs):
            obs["t"] = [(d - season_start(season)).days + PAD for d in obs["date"]]
            for (ix, ry, pid), g in obs.groupby(["index", "rpg_year", "parcel_id"], sort=False):
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
                rows.append((site, season, ix, pid, int(ry), len(inside), season_start(season) + dt.timedelta(int(inside[0])),
                             season_start(season) + dt.timedelta(int(inside[-1])), None if k is None else float(z[k]),
                             None if k is None else season_start(season) + dt.timedelta(k),
                             z.astype("<f4").tobytes(), int(round(float(g["n_pixels"].max())))))
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


def clear_site(conn, site: str, rpg_year: int | None = None) -> None:
    """A site's parcel statistics (stores), curves and parcel info, of one RPG layer or all."""
    fields.remove(site, PRODUCT, None if rpg_year is None else layer_name(rpg_year))
    layer = "" if rpg_year is None else " AND rpg_year = %s"
    args = (site,) if rpg_year is None else (site, rpg_year)
    for t in ("parcel_season", "parcel_info"):
        conn.execute(f"DELETE FROM eo.{t} WHERE site = %s{layer}", args)


def changed_seasons(before: list[int], after: list[int], first: int = 2015) -> list[int]:
    """The seasons (``first`` to the current ones) whose RPG outlines differ between two lists of
    RPG years loaded: a new year's own season, and the later ones that used the latest RPG."""
    return [s for s in range(first, max(current_seasons()) + 1) if geometry_year(s, before) != geometry_year(s, after)]


def unused_layers(pairs, years_rpg: list[int]) -> list[tuple[int, dt.date]]:
    """The (RPG year, date) of statistics that no season of their date uses (FARM_LAYER kept)."""
    return [(y, d) for y, d in pairs
            if y != FARM_LAYER and y not in {geometry_year(s, years_rpg) for s in seasons_of(d, PAD)}]


def prune_stats(site: str, years_rpg: list[int]) -> int:
    """Removes the dates of the RPG layers that no season of the date uses any more (the latest RPG's,
    used for a season until its own RPG was published; a year no longer loaded). Returns the number
    of layer-dates removed."""
    n = 0
    for name in fields.layers(site, PRODUCT):
        y = layer_year(name)
        if y == FARM_LAYER:
            continue
        n += fields.keep_dates(site, PRODUCT, name, lambda d, y=y: not unused_layers([(y, d)], years_rpg))
    if n:
        log.info("Parcels: %d layer-dates of outlines no longer used removed", n)
    return n


def sync(site: str, full: bool = False) -> dict:
    """Parcel statistics of new dates (or of every date when ``full``), then the curves of the seasons touched.

    Seasons whose RPG outlines changed (a new RPG year loaded) are recomputed as well.
    """
    if full:
        with connect() as conn:
            clear_site(conn, site)
            conn.execute("DELETE FROM eo.group_season WHERE site = %s AND index = ANY(%s)", (site, list(s2.INDICES)))
    clear_labels()
    try:
        touched = store_missing_stats(site)
    finally:
        clear_labels()      # parcel rasters of every RPG year: not kept by the long-running scheduler
    years_rpg = rpg_years()
    prune_stats(site, years_rpg)
    with connect() as conn:
        stale = {r["season"] for r in conn.execute(
            "SELECT DISTINCT season, rpg_year FROM eo.parcel_season WHERE site = %s", (site,)).fetchall()
            if r["rpg_year"] != FARM_LAYER and geometry_year(r["season"], years_rpg) != r["rpg_year"]}
        if stale:
            conn.execute("DELETE FROM eo.parcel_season WHERE site = %s AND season = ANY(%s)", (site, list(stale)))
    seasons = None if full else touched | stale
    n = compute_seasons(site, seasons) if (full or seasons) else 0
    # Crop-group curves of the seasons recomputed (all of them when none was computed yet)
    with connect() as conn:
        backfill = (conn.execute("SELECT 1 FROM eo.parcel_season WHERE site = %s LIMIT 1", (site,)).fetchone()
                    and not conn.execute("SELECT 1 FROM eo.group_season WHERE site = %s AND index = ANY(%s) LIMIT 1",
                                         (site, list(s2.INDICES))).fetchone())
    groups = compute_groups(site, None if (full or backfill) else seasons) if (full or seasons or backfill) else 0
    return {"seasons": sorted(seasons) if seasons is not None else "all", "curves": n, "group_curves": groups}


# ---------------------------------------------------------------------------
# 3. Crop-group curves per area, computed at ingestion (eo.parcel_info, eo.group_season)
# ---------------------------------------------------------------------------
def crop_status(season: int, rpg_year: int, code_group: str | None) -> str:
    """declared (the season's own RPG), assumed (a permanent crop taken from the latest RPG) or unknown
    (a parcel of the farm's plots may be ``farm`` instead, see :func:`effective_crop`)."""
    if season == rpg_year:
        return "declared"
    return "assumed" if code_group in PERMANENT_GROUPS else "unknown"


def farm_years(site: str) -> list[int]:
    """The years of the farm's land cover (ref.farm_landcover)."""
    with connect() as conn:
        return [r["year"] for r in conn.execute(
            "SELECT DISTINCT year FROM ref.farm_landcover WHERE site = %s ORDER BY 1", (site,)).fetchall()]


def effective_crop(season: int, rpg_year: int, code_group: str | None, code_cultu: str | None,
                   farm: tuple[str | None, str | None] | None = None) -> tuple[str | None, str | None, str]:
    """(group, crop, status) of a parcel in a season: the farm's crop when its land cover gives one
    for the parcel's place (``farm``: (group, crop); status ``farm``), else the RPG's (see
    :func:`crop_status`)."""
    if farm is not None and farm[0]:
        return farm[0], farm[1], "farm"
    return code_group, code_cultu, crop_status(season, rpg_year, code_group)


def season_fields(site: str, season: int) -> list[str]:
    """The ids of the farm's fields declared for a season (its land-cover year)."""
    with connect() as conn:
        return [r["field_id"] for r in conn.execute(
            "SELECT DISTINCT field_id FROM ref.farm_landcover WHERE site = %s AND year = %s AND field_id IS NOT NULL",
            (site, season)).fetchall()]


def refresh_field_info(site: str) -> int:
    """eo.parcel_info of the farm's fields (rpg_year FARM_LAYER): area and the site's areas holding them
    (their point on surface); their crop is per season, in eo.farm_crop."""
    with connect() as conn:
        conn.execute("DELETE FROM eo.parcel_info WHERE site = %s AND rpg_year = %s", (site, FARM_LAYER))
        return conn.execute(
            """INSERT INTO eo.parcel_info (site, rpg_year, parcel_id, code_group, code_cultu, surf_ha, bio, areas)
               SELECT %(site)s, %(farm)s, f.field_id, NULL, NULL, ST_Area(f.geom::geography) / 1e4, NULL,
                      array_agg(a.code ORDER BY a.code)
               FROM (SELECT DISTINCT ON (field_id) field_id, geom FROM ref.farm_landcover
                     WHERE site = %(site)s AND field_id IS NOT NULL ORDER BY field_id) f
               JOIN meta.aoi a ON a.site = %(site)s AND ST_Intersects(a.geom, ST_PointOnSurface(f.geom))
               GROUP BY f.field_id, f.geom""", {"site": site, "farm": FARM_LAYER}).rowcount or 0


def refresh_farm_crops(site: str, seasons) -> int:
    """eo.farm_crop for the given seasons with a land-cover file: its fields (rpg_year FARM_LAYER) with
    their crop, and the RPG parcels of the season (its RPG year's outlines) lying in them (point on
    surface), without crop: replaced by the fields."""
    years_rpg = rpg_years()
    have = set(farm_years(site))
    n = 0
    with connect() as conn:
        for season in sorted(seasons):
            conn.execute("DELETE FROM eo.farm_crop WHERE site = %s AND season = %s", (site, season))
            gy = geometry_year(season, years_rpg)
            if season not in have:
                continue
            n += conn.execute(
                """INSERT INTO eo.farm_crop (site, season, rpg_year, parcel_id, code_group, code_cultu)
                   SELECT DISTINCT ON (field_id) %(site)s, %(season)s, %(farm)s, field_id, code_group, code_cultu
                   FROM ref.farm_landcover WHERE site = %(site)s AND year = %(season)s AND field_id IS NOT NULL
                   ORDER BY field_id, code_group NULLS LAST""",
                {"site": site, "season": season, "farm": FARM_LAYER}).rowcount or 0
            if gy is None:
                continue
            conn.execute(
                """INSERT INTO eo.farm_crop (site, season, rpg_year, parcel_id, code_group, code_cultu)
                   SELECT DISTINCT ON (i.parcel_id) %(site)s, %(season)s, i.rpg_year, i.parcel_id, NULL, NULL
                   FROM eo.parcel_info i
                   JOIN ref.rpg_parcel p ON p.year = i.rpg_year AND p.parcel_id = i.parcel_id
                   JOIN ref.farm_landcover l ON l.site = %(site)s AND l.year = %(season)s
                        AND ST_Intersects(l.geom, ST_PointOnSurface(p.geom))
                   WHERE i.site = %(site)s AND i.rpg_year = %(gy)s
                   ORDER BY i.parcel_id
                   ON CONFLICT DO NOTHING""",
                {"site": site, "season": season, "gy": gy})
    return n


def in_season(farm: dict, rpg_year: int, parcel_id: str) -> bool:
    """Whether a parcel series counts in a season: the farm's fields declared for it (with a crop)
    replace the RPG parcels lying in them (``farm``: see :func:`farm_crops`)."""
    f = farm.get((rpg_year, parcel_id))
    if rpg_year == FARM_LAYER:
        return f is not None and f[0] is not None
    return f is None


def farm_crops(site: str, season: int) -> dict[tuple[int, str], tuple[str | None, str | None]]:
    """(RPG year, parcel id) -> (group, crop) declared by the farm for a season: its fields
    (FARM_LAYER) with their crop, the RPG parcels they replace with (None, None)."""
    with connect() as conn:
        return {(r["rpg_year"], r["parcel_id"]): (r["code_group"], r["code_cultu"]) for r in conn.execute(
            "SELECT rpg_year, parcel_id, code_group, code_cultu FROM eo.farm_crop WHERE site = %s AND season = %s",
            (site, season)).fetchall()}


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
    refresh_field_info(site)
    refresh_farm_crops(site, seasons)
    declared_years = set(farm_years(site))     # with land-cover files, the farm area only from them
    n = 0
    for season in seasons:
        farm = farm_crops(site, season)
        with connect() as conn:
            rows = conn.execute(
                """SELECT c.index, c.rpg_year, c.parcel_id, c.n_pixels, c.curve, i.code_group, i.code_cultu,
                          i.surf_ha, i.areas
                   FROM eo.parcel_season c
                   JOIN eo.parcel_info i ON i.site = c.site AND i.rpg_year = c.rpg_year AND i.parcel_id = c.parcel_id
                   WHERE c.site = %s AND c.season = %s ORDER BY c.index, c.parcel_id""", (site, season)).fetchall()
        rows = [r for r in rows if in_season(farm, r["rpg_year"], r["parcel_id"])]
        out = []
        for ix in sorted({r["index"] for r in rows}):
            rs = [r for r in rows if r["index"] == ix]
            mat = np.stack([np.frombuffer(r["curve"], dtype="<f4") for r in rs]).astype("float64")
            eff = [effective_crop(season, r["rpg_year"], r["code_group"], r["code_cultu"],
                                  farm.get((r["rpg_year"], r["parcel_id"]))) for r in rs]
            status = np.array([e[2] for e in eff])
            group = np.array([e[0] or "" for e in eff])
            ha = np.array([r["surf_ha"] or 0.0 for r in rs])
            w = np.array([r["n_pixels"] or 1 for r in rs], dtype="float64")
            members: dict[str, list[int]] = {}
            for k, r in enumerate(rs):
                for code in r["areas"]:
                    if code == "farm" and declared_years and (r["rpg_year"], r["parcel_id"]) not in farm:
                        continue            # the farm's plots: only what the farm declared for the season
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
                    st = ("declared" if (status[sel] == "declared").all()
                          else "farm" if np.isin(status[sel], ("declared", "farm")).all() else "assumed")
                    out.append((site, ix, area, g, season, int(sel.size), float(ha[sel].sum()), st, _f32(weighted),
                                _f32(plain), _f32(q25), _f32(q75), cnt.astype("<i2").tobytes()))
        with connect() as conn:
            conn.execute("DELETE FROM eo.group_season WHERE site = %s AND season = %s AND index = ANY(%s)",
                         (site, season, list(s2.INDICES)))
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
