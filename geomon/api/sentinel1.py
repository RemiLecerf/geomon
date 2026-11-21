"""Sentinel-1 endpoints: what is stored, backscatter series over an area, images per date."""

from __future__ import annotations

import collections
from functools import lru_cache

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from .. import sentinel1 as s1
from ..config import settings
from ..db import connect
from .areas import site_row
from .sentinel2 import grid_png, legend

router = APIRouter(prefix="/api/s1", tags=["sentinel-1"])


def _site() -> str:
    with connect() as conn:
        return site_row(conn)["code"]


def _check(measure: str, direction: str) -> None:
    if measure not in s1.MEASURES:
        raise HTTPException(404, f"Unknown measure {measure} ({', '.join(s1.MEASURES)})")
    if direction not in s1.DIRECTIONS:
        raise HTTPException(404, f"Unknown direction {direction} (asc, desc)")


def _orbit_of_dates(site: str, direction: str) -> dict[str, int]:
    """Relative orbit of each stored date of a direction (from the stores' coordinates)."""
    out = {}
    for p in s1.parts(site):
        for y in s1.years(site, direction, p):
            ds = s1.open_year(site, y, direction, p)
            for t, o in zip(ds["time"].values, ds["relative_orbit"].values):
                out[str(t)[:10]] = int(o)
    return out


@router.get("/info")
def info() -> dict:
    """What is stored, per orbit direction: years, dates, relative orbits."""
    site = _site()
    directions = {}
    for d, label in s1.DIRECTIONS.items():
        orbits = _orbit_of_dates(site, d)
        directions[d] = {"label": label, "years": s1.years(site, d), "dates": len(orbits),
                         "first": min(orbits) if orbits else None, "last": max(orbits) if orbits else None,
                         "orbits": [{"orbit": o, "dates": n} for o, n in sorted(collections.Counter(orbits.values()).items())]}
    return {"site": site, "available": any(v["dates"] for v in directions.values()), "start": str(settings.s1_start),
            "collection": s1.COLLECTION, "coefficient": s1.COEFFICIENT, "resolution_m": 10,
            "directions": directions,
            "measures": [{"name": m.name, "long_name": m.long_name, "units": m.units, "vmin": m.vmin, "vmax": m.vmax}
                         for m in s1.MEASURES.values()],
            "note": "sigma0 on the ellipsoid, not terrain-flattened: compare dates of the same relative orbit",
            "hint": None if any(v["dates"] for v in directions.values())
            else "run: docker compose run --rm ingest sentinel1 (uses the sentinel2-login)"}


@router.get("/series")
def series(measure: str = "vv", direction: str = "asc", area: str | None = None) -> dict:
    """Per-date statistics of a measure over an area, with the relative orbit of each date. The
    mean is taken in linear power (expressed in dB); median and percentiles are the pixels'."""
    _check(measure, direction)
    site = _site()
    area = area or site
    with connect() as conn:
        rows = conn.execute(
            """SELECT date, mean, median, p10, p90, std, clear_fraction FROM eo.index_stats
               WHERE site = %s AND area = %s AND index = %s ORDER BY date""",
            (site, area, s1.stat_name(measure, direction))).fetchall()
    orbits = _orbit_of_dates(site, direction)
    m = s1.MEASURES[measure]
    return {"site": site, "area": area, "measure": measure, "direction": direction, "long_name": m.long_name,
            "units": m.units,
            "points": [{"date": r["date"].isoformat(), "orbit": orbits.get(r["date"].isoformat()),
                        **{k: (None if r[k] is None else round(float(r[k]), 4))
                           for k in ("mean", "median", "p10", "p90", "std", "clear_fraction")}}
                       for r in rows if r["mean"] is not None]}


def _render(site: str, date: str, direction: str, measure: str, part: str) -> tuple[bytes, list]:
    z = s1.store_path(site, int(date[:4]), direction, part) / "zarr.json"
    return _render_cached(site, date, direction, measure, part, z.stat().st_mtime if z.exists() else None)


@lru_cache(maxsize=48)
def _render_cached(site: str, date: str, direction: str, measure: str, part: str,
                   stamp: float | None) -> tuple[bytes, list]:
    try:
        ds = s1.scene(site, date, direction, part)
    except KeyError as exc:
        raise HTTPException(404, f"No Sentinel-1 {direction} acquisition on {date}") from exc
    m = s1.MEASURES[measure]
    return grid_png(s1.measure_array(ds, measure), ds["x"].values, ds["y"].values, ds.attrs["crs"],
                    m.vmin, m.vmax, m.colormap)


@router.get("/{measure}/info")
def image_info(measure: str, date: str, direction: str = "asc") -> dict:
    """The images of a date (one per part of the site), with their legend."""
    _check(measure, direction)
    site = _site()
    images = []
    for part in s1.parts(site):
        try:
            _, coords = _render(site, date, direction, measure, part)
        except HTTPException:
            continue
        images.append({"part": part, "coordinates": coords,
                       "url": f"/api/s1/{measure}.png?date={date}&direction={direction}&part={part}&site={site}"})
    if not images:
        raise HTTPException(404, f"No Sentinel-1 {direction} acquisition on {date}")
    m = s1.MEASURES[measure]
    orbit = _orbit_of_dates(site, direction).get(date[:10])
    return {"date": date, "direction": direction, "orbit": orbit, "measure": measure, "long_name": m.long_name,
            "units": m.units, "images": images, "vmin": m.vmin, "vmax": m.vmax,
            "legend": legend(m.vmin, m.vmax, m.colormap)}


@router.get("/{measure}.png")
def image_png(measure: str, date: str, direction: str = "asc", part: str = "") -> Response:
    _check(measure, direction)
    png = _render(_site(), date, direction, measure, part)[0]
    return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=86400"})
