"""MODIS NDVI per RPG crop group: seasonal curves of "pure" 250 m cells, crop-group references.

A MODIS cell (231.66 m, 5.4 ha) usually holds several parcels: a crop group is followed on the
cells it covers almost entirely. For each RPG year the share of every crop group in every cell is
measured (``purity``: the parcels rasterized on a grid 8 times finer, 29 m); a cell is used for a
group when the group covers at least GEOMON_MODIS_PURITY (75 %) of it. The neighbouring parcels
of the same group count together: a block of maize fields makes pure maize cells even when each
field is smaller than a cell.

Then, as for the Sentinel-2 parcels (geomon.parcels), with the same seasons (1 September Y-1 to
31 December Y), the same daily curves (interpolation between observations, or Whittaker smoothing
with GEOMON_S2_PARCEL_SMOOTHING) and the same references (3-year, 5-year, long-term):

1. each pure cell gets a daily curve per season from its reliable composites, each placed at the
   middle of its 16 days (day 8);
2. the curve of a group in an area is the mean of the curves of its pure cells there, weighted by
   the group's share of each cell (``mean_pixels``) or not (``mean_parcel``), with the middle half
   of the cells; stored in eo.group_season under the index ``modis_ndvi`` (``parcels``: the cells).

Seasons follow the RPG as for Sentinel-2: a season uses its own RPG; after the latest RPG, only
permanent crops (permanent grassland, moors, orchards, vines...) are assumed from it. From 2007 to
2014 the RPG only gives the farms' îlots and their majority crop group (ref.rpg_ilot, status
``ilots``): an îlot counts for its majority group by the share of its area that group covers
(2010-2014; 2007-2009 give no area: the share of the 2010 îlot of the same group overlapping
it most is used, else the site's overall share, see ``ILOT_SHARE``),
so that only cells of mostly mono-crop îlots are pure. MODIS goes back to 2000: before the first
RPG loaded, the permanent crops of the first RPG are assumed, which gives permanent grassland a
long-term reference since 2000.

In a season with a file of the farm's land cover (the site's ``farm_landcover``), the farm's plots
of that year replace the RPG outlines lying in them, with the farm's crops (status ``farm``).
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import numpy as np
import xarray as xr

from . import modis
from . import sentinel2 as s2
from . import parcels as pc
from .config import settings
from .db import connect

log = logging.getLogger(__name__)

STAT = modis.STAT_NAME            # index name in eo.group_season
SUPERSAMPLE = 8                   # purity measured on 8 x 8 sub-cells (29 m)
MID_DAY = 8                       # a composite is placed at the middle of its 16 days
CELL_HA = modis.PIXEL ** 2 / 10_000
# Share of an îlot covered by its majority group when the RPG does not give it (2007-2009): the
# share of the site's îlot area covered by their majority group in the nearest year that gives it
# (0.78 and 0.85 on the two sites in 2010: large îlots are the mixed ones), else this value
ILOT_SHARE = 0.9


def rpg_years() -> tuple[list[int], list[int]]:
    """Years of RPG parcels (2015-) and of RPG îlots (2007-2014)."""
    with connect() as conn:
        parcels = [r["year"] for r in conn.execute("SELECT DISTINCT year FROM ref.rpg_parcel ORDER BY 1").fetchall()]
        ilots = [r["year"] for r in conn.execute("SELECT DISTINCT year FROM ref.rpg_ilot ORDER BY 1").fetchall()]
    return parcels, [y for y in ilots if y not in parcels]


def geometry_year(season: int, years: list[int]) -> int | None:
    """RPG year of a season: its own, else the latest (seasons after it) or the first (before it)."""
    if not years:
        return None
    if season in years:
        return season
    return max(years) if season > max(years) else min(years)


def crop_status(season: int, rpg_year: int, code_group: str, ilots: bool = False) -> str:
    """declared (the season's RPG parcels), ilots (its RPG îlots' majority group), assumed (a
    permanent crop of the nearest RPG) or unknown."""
    if season == rpg_year:
        return "ilots" if ilots else "declared"
    return "assumed" if code_group in pc.PERMANENT_GROUPS else "unknown"


# ---------------------------------------------------------------------------
# Purity: share of each crop group in each cell, per RPG year
# ---------------------------------------------------------------------------
def _purity_path(site: str, year: int, own: bool = True, farm_year: int | None = None) -> Path:
    return modis.site_dir(site) / "rpg" / f"{year}{'' if own else '_x'}{f'_farm{farm_year}' if farm_year else ''}.npz"


# An RPG outline lying in a plot of the farm's land cover of a year (its point on surface)
_IN_FARM = """EXISTS (SELECT 1 FROM ref.farm_landcover f WHERE f.site = %(site)s AND f.year = %(farm_year)s
                       AND f.code_group IS NOT NULL AND ST_Intersects(f.geom, ST_PointOnSurface({g})))"""


def _rpg_shapes(site: str, year: int, ilots: bool, farm_year: int | None = None) -> list[dict]:
    """Outlines of an RPG year over the site, with their crop group, the share of each outline that
    group covers (1 for a parcel; for an îlot, its majority group's share) and whether it lies in the
    farm's plots."""
    with connect() as conn:
        if not ilots:
            return conn.execute(
                f"""SELECT p.code_group, 1.0 AS share, ST_AsBinary(p.geom) AS g, {_IN_FARM.format(g="p.geom")} AS in_farm
                    FROM ref.rpg_parcel p
                    JOIN meta.aoi s ON s.kind = 'site' AND s.code = %(site)s AND p.geom && s.bbox
                    WHERE p.year = %(year)s AND p.code_group IS NOT NULL""",
                {"site": site, "year": year, "farm_year": farm_year}).fetchall()
        known = conn.execute(
            """SELECT i.year, sum(LEAST(i.group_ha, i.surf_ha)) / sum(i.surf_ha) AS share
               FROM ref.rpg_ilot i JOIN meta.aoi s ON s.kind = 'site' AND s.code = %s AND i.geom && s.bbox
               WHERE i.group_ha IS NOT NULL AND i.surf_ha > 0 GROUP BY 1 ORDER BY abs(i.year - %s) LIMIT 1""",
            (site, year)).fetchone()
        default = float(known["share"]) if known else ILOT_SHARE
        # Without a share (2007-2009): that of the îlot of the nearest year with shares, same majority
        # group, overlapping it most (îlots change little); else the site's overall share
        return conn.execute(
            f"""SELECT i.code_group, COALESCE(
                          -- (LEAST ignores NULLs: LEAST(NULL, 1) is 1)
                          CASE WHEN i.group_ha IS NOT NULL AND i.surf_ha > 0 THEN LEAST(i.group_ha / i.surf_ha, 1) END,
                          (SELECT LEAST(j.group_ha / j.surf_ha, 1) FROM ref.rpg_ilot j
                           WHERE j.year = %(ref)s AND j.code_group = i.code_group AND j.geom && i.geom
                             AND j.group_ha IS NOT NULL AND j.surf_ha > 0
                             AND ST_Intersects(j.geom, i.geom)
                           ORDER BY ST_Area(ST_Intersection(j.geom, i.geom)) DESC LIMIT 1),
                          %(default)s) AS share,
                      ST_AsBinary(i.geom) AS g, {_IN_FARM.format(g="i.geom")} AS in_farm FROM ref.rpg_ilot i
               JOIN meta.aoi s ON s.kind = 'site' AND s.code = %(site)s AND i.geom && s.bbox
               WHERE i.year = %(year)s AND i.code_group IS NOT NULL""",
            {"ref": known["year"] if known else None, "default": default, "site": site, "year": year,
             "farm_year": farm_year}).fetchall()


def _variant_shapes(site: str, year: int, ilots: bool, own: bool, farm_year: int | None) -> list[dict]:
    """The outlines of an RPG year with the status of their crop for the seasons it serves: its own
    season (declared, ilots) or others (assumed for the permanent crops, unknown otherwise; the
    unknown ones are kept as group "" so that they lower the other groups' shares). With
    ``farm_year``, the farm's plots of that year replace the RPG outlines lying in them (status farm)."""
    rows = []
    for r in _rpg_shapes(site, year, ilots, farm_year):
        if farm_year and r["in_farm"]:
            continue
        status = ("ilots" if ilots else "declared") if own else (
            "assumed" if r["code_group"] in pc.PERMANENT_GROUPS else "unknown")
        rows.append({"code_group": r["code_group"] if status != "unknown" else "", "share": r["share"],
                     "g": r["g"], "status": status})
    if farm_year:
        with connect() as conn:
            farm = conn.execute(
                """SELECT code_group, ST_AsBinary(geom) AS g FROM ref.farm_landcover
                   WHERE site = %s AND year = %s AND code_group IS NOT NULL""", (site, farm_year)).fetchall()
        rows += [{"code_group": r["code_group"], "share": 1.0, "g": r["g"], "status": "farm"} for r in farm]
    return rows


def _group_status(statuses: set[str]) -> str:
    if statuses <= {"declared"} or statuses <= {"ilots"}:
        return statuses.pop() if statuses else "declared"
    if statuses <= {"declared", "ilots", "farm"}:
        return "farm" if statuses == {"farm"} else "declared"
    return "unknown" if statuses == {"unknown"} else "assumed"


def build_purity(site: str, year: int, ds: xr.Dataset, ilots: bool = False, own: bool = True,
                 farm_year: int | None = None) -> dict:
    """Share (percent, uint8) of each crop group in each cell of the store grid, for one RPG year
    (its parcels, or its îlots weighted by their majority group's share), with the status of each
    group for the seasons served (see :func:`_variant_shapes`)."""
    from pyproj import Transformer
    from rasterio.features import rasterize
    from rasterio.transform import from_origin
    from shapely import wkb
    from shapely.ops import transform

    x, y = ds["x"].values, ds["y"].values
    ny, nx, f = len(y), len(x), SUPERSAMPLE
    aff = from_origin(x[0] - modis.PIXEL / 2, y[0] + modis.PIXEL / 2, modis.PIXEL / f, modis.PIXEL / f)
    to = Transformer.from_crs("EPSG:4326", ds.attrs["crs"], always_xy=True).transform
    rows = _variant_shapes(site, year, ilots, own, farm_year)
    groups = sorted({r["code_group"] for r in rows}, key=lambda g: (len(g), g))
    statuses = [_group_status({r["status"] for r in rows if r["code_group"] == g}) if g else "unknown" for g in groups]
    label = {g: k + 1 for k, g in enumerate(groups)}
    shapes = [transform(to, wkb.loads(bytes(r["g"]))) for r in rows]
    shape_ = (ny * f, nx * f)
    sub = rasterize(zip(shapes, (label[r["code_group"]] for r in rows)), out_shape=shape_, transform=aff,
                    fill=0, dtype="uint8") if rows else np.zeros(shape_, "uint8")
    weight = rasterize(zip(shapes, (float(r["share"]) for r in rows)), out_shape=shape_, transform=aff,
                       fill=0, dtype="float32") if rows else np.zeros(shape_, "float32")
    blocks, wblocks = sub.reshape(ny, f, nx, f), weight.reshape(ny, f, nx, f)
    share = np.stack([np.round(100 * ((blocks == k + 1) * wblocks).mean(axis=(1, 3))) for k in range(len(groups))]
                     ).astype("uint8") if groups else np.zeros((0, ny, nx), "uint8")
    out = {"groups": np.array(groups), "statuses": np.array(statuses), "share": share, "x0": float(x[0]),
           "y0": float(y[0]), "shape": np.array([ny, nx])}
    p = _purity_path(site, year, own, farm_year)
    p.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(p, **out)
    return out


def purity(site: str, year: int, ds: xr.Dataset, rebuild: bool = False, ilots: bool = False,
           own: bool = True, farm_year: int | None = None) -> dict:
    """The purity of an RPG year for a kind of season (rebuilt when missing, asked for, on another
    grid, or written before the statuses were kept)."""
    p = _purity_path(site, year, own, farm_year)
    if p.exists() and not rebuild:
        with np.load(p) as z:
            d = {k: z[k] for k in z.files}
        if ("statuses" in d and abs(d["x0"] - ds["x"].values[0]) < 1 and abs(d["y0"] - ds["y"].values[0]) < 1
                and tuple(d["shape"]) == (ds.sizes["y"], ds.sizes["x"])):
            return d
    return build_purity(site, year, ds, ilots, own, farm_year)


def season_purity(site: str, season: int, ds: xr.Dataset, years: tuple[list[int], list[int]],
                  rebuild: bool = False) -> tuple[int, dict]:
    """The RPG year of a season and its purity."""
    parcel_years, ilot_years = years
    gy = geometry_year(season, sorted(parcel_years + ilot_years))
    farm_year = season if season in pc.farm_years(site) else None
    return gy, purity(site, gy, ds, rebuild=rebuild, ilots=gy in ilot_years, own=season == gy, farm_year=farm_year)


def pure_cells(pur: dict, min_share: float | None = None) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """Per group: the flat indices of its pure cells and its share of each (0-1); group "": cells
    mostly covered by crops not known for the season."""
    threshold = round(100 * (settings.modis_purity if min_share is None else min_share))
    out = {}
    for g, share in zip(pur["groups"], pur["share"]):
        flat = share.ravel()
        idx = np.flatnonzero(flat >= threshold).astype("int32")
        if idx.size:
            out[str(g)] = (idx, flat[idx].astype("float64") / 100)
    return out


# ---------------------------------------------------------------------------
# Composites of a season
# ---------------------------------------------------------------------------
def season_composites(site: str, season: int, pad: int = pc.PAD) -> tuple[list[str], np.ndarray, np.ndarray]:
    """The composites whose middle day falls in the season (extended by ``pad`` days): their first
    days, middle days (from the season's start, in days) and NDVI (time, cells), NaN where unreliable."""
    start = pc.season_start(season)
    lo, hi = start - dt.timedelta(days=pad), pc.season_end(season) + dt.timedelta(days=pad)
    dates, days, values = [], [], []
    for year in range(lo.year, hi.year + 1):
        ds = modis.open_year(site, year)
        if ds is None:
            continue
        times = ds["time"].values.astype("datetime64[D]")
        mids = times + np.timedelta64(MID_DAY, "D")
        sel = np.nonzero((mids >= np.datetime64(lo)) & (mids <= np.datetime64(hi)))[0]
        if not sel.size:
            continue
        part = ds.isel(time=sel).load()
        raw = part["ndvi"].values.reshape(len(sel), -1)
        rel = part["reliability"].values.reshape(len(sel), -1)
        v = raw.astype("float32") * modis.SCALE
        v[(raw == modis.NDVI_NODATA) | ~np.isin(rel, modis.CLEAR)] = np.nan
        values.append(v)
        dates += [str(t) for t in times[sel]]
        days += [int((m - np.datetime64(start)).astype(int)) for m in mids[sel]]
    if not values:
        return [], np.zeros(0, "int64"), np.zeros((0, 0), "float32")
    return dates, np.array(days), np.concatenate(values)


def cell_curves(days: np.ndarray, values: np.ndarray, cells: np.ndarray, season: int) -> dict[int, np.ndarray]:
    """Daily curve of each cell over the season (as for a parcel, see geomon.parcels.compute_seasons)."""
    n_full = pc.SEASON_DAYS + 2 * pc.PAD
    last = (pc.season_end(season) - pc.season_start(season)).days
    t_all = days + pc.PAD
    order = np.argsort(t_all, kind="stable")
    t_all, values = t_all[order], values[order]
    out = {}
    for c in cells:
        y = values[:, c]
        ok = np.isfinite(y)
        t, yy = t_all[ok], y[ok].astype("float64")
        if ((t >= pc.PAD) & (t < pc.PAD + pc.SEASON_DAYS)).sum() < pc.MIN_OBS:
            continue
        t, first = np.unique(t, return_index=True)        # two composites may share a middle day
        z = pc.season_curve(t, yy[first], n=n_full)
        if z is None:
            continue
        z = z[pc.PAD:pc.PAD + pc.SEASON_DAYS]
        z[np.arange(pc.SEASON_DAYS) > last] = np.nan
        if np.isfinite(z).any():
            out[int(c)] = z
    return out


# ---------------------------------------------------------------------------
# Group curves
# ---------------------------------------------------------------------------
def _template(site: str) -> xr.Dataset | None:
    ys = modis.years(site)
    return modis.open_year(site, ys[-1]) if ys else None


def compute(site: str, seasons=None, rebuild_purity: bool = False) -> dict:
    """(Re)computes the MODIS crop-group curves of every area of the site for the given seasons (all
    the seasons since GEOMON_MODIS_START when None)."""
    ds = _template(site)
    parcel_years, ilot_years = rpg_years()
    years_rpg = sorted(parcel_years + ilot_years)
    if ds is None or not years_rpg:
        return {"seasons": [], "group_curves": 0}
    if seasons is None:
        seasons = range(settings.modis_start.year, max(pc.current_seasons()) + 1)
    seasons = sorted(seasons)
    zones = s2.site_zones(ds, site)
    farm_years = set(pc.farm_years(site))
    rebuilt: set[Path] = set()
    n = 0
    for season in seasons:
        gy = geometry_year(season, years_rpg)
        key = _purity_path(site, gy, season == gy, season if season in farm_years else None)
        _, pur = season_purity(site, season, ds, (parcel_years, ilot_years),
                               rebuild=rebuild_purity and key not in rebuilt)
        rebuilt.add(key)
        status_of = dict(zip((str(g) for g in pur["groups"]), (str(t) for t in pur["statuses"])))
        pure = pure_cells(pur)
        _, days, values = season_composites(site, season)
        rows = []
        if len(days):
            curves = cell_curves(days, values, np.unique(np.concatenate([c for c, _ in pure.values()]))
                                 if pure else np.zeros(0, "int32"), season)
            for area, zone in zones.items():
                if area == "farm" and farm_years and season not in farm_years:
                    continue                # the farm's plots: only the years the farm declared
                unknown_cells, unknown_ha = 0, 0.0
                for g, (cells, share) in pure.items():
                    inside = np.isin(cells, zone)
                    status = status_of.get(g, "unknown")
                    if status == "unknown":
                        unknown_cells += int(inside.sum())
                        unknown_ha += float(share[inside].sum() * CELL_HA)
                        continue
                    keep = [k for k in np.flatnonzero(inside) if int(cells[k]) in curves]
                    if not keep:
                        continue
                    mat = np.stack([curves[int(cells[k])] for k in keep]).astype("float64")
                    w = share[keep]
                    weighted, plain, q25, q75, cnt = pc._group_curve(mat, w)
                    rows.append((site, STAT, area, g, season, len(keep), float(w.sum() * CELL_HA), status,
                                 pc._f32(weighted), pc._f32(plain), pc._f32(q25), pc._f32(q75),
                                 cnt.astype("<i2").tobytes()))
                if unknown_cells:
                    rows.append((site, STAT, area, "", season, unknown_cells, unknown_ha, "unknown",
                                 None, None, None, None, None))
        with connect() as conn:
            conn.execute("DELETE FROM eo.group_season WHERE site = %s AND index = %s AND season = %s",
                         (site, STAT, season))
            with conn.cursor() as cur:
                with cur.copy("COPY eo.group_season (site, index, area, code_group, season, parcels, ha, status, "
                              "mean_pixels, mean_parcel, q25, q75, count) FROM STDIN") as cp:
                    for r in rows:
                        cp.write_row(r)
        n += len(rows)
    log.info("MODIS %s: crop-group curves of %d seasons (%d curves)", site, len(seasons), n)
    return {"seasons": [seasons[0], seasons[-1]] if seasons else [], "group_curves": n}


def sync(site: str, dates) -> dict:
    """Curves of the seasons touched by new composites (all of them the first time)."""
    with connect() as conn:
        done = conn.execute("SELECT 1 FROM eo.group_season WHERE site = %s AND index = %s LIMIT 1",
                            (site, STAT)).fetchone()
    if not done:
        return compute(site)
    seasons = set()
    for d in dates:
        mid = dt.date.fromisoformat(str(d)[:10]) + dt.timedelta(days=MID_DAY)
        seasons |= set(pc.seasons_of(mid, pad=pc.PAD))
    return compute(site, seasons) if seasons else {"seasons": [], "group_curves": 0}


# ---------------------------------------------------------------------------
# Observations of a group (for the chart's dots)
# ---------------------------------------------------------------------------
def observations(site: str, season: int, group: str, area: str, weighted: bool = True) -> list[dict]:
    """Per composite of the season: the mean NDVI of the group's pure reliable cells in the area."""
    ds = _template(site)
    parcel_years, ilot_years = rpg_years()
    years_rpg = sorted(parcel_years + ilot_years)
    if ds is None or not years_rpg:
        return []
    _, pur = season_purity(site, season, ds, (parcel_years, ilot_years))
    status_of = dict(zip((str(g) for g in pur["groups"]), (str(t) for t in pur["statuses"])))
    if status_of.get(group, "unknown") == "unknown":
        return []
    pure = pure_cells(pur).get(group)
    if pure is None:
        return []
    zones = s2.site_zones(_template(site), site)
    cells, share = pure
    inside = np.isin(cells, zones.get(area, np.zeros(0, "int32")))
    cells, share = cells[inside], share[inside]
    if not cells.size:
        return []
    dates, days, values = season_composites(site, season, pad=0)
    out = []
    need = max(1, min(3, round(0.3 * cells.size)))
    for d, day, row in zip(dates, days, values):
        v = row[cells]
        ok = np.isfinite(v)
        if ok.sum() < need or not (0 <= day < pc.SEASON_DAYS):
            continue
        mean = float(np.average(v[ok], weights=share[ok])) if weighted else float(v[ok].mean())
        out.append({"date": d[:10], "day": int(day), "mean": round(mean, 3), "parcels": int(ok.sum())})
    return out


