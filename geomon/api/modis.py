"""MODIS NDVI endpoints: what is stored, NDVI series over an area, crop-group curves, images per composite."""

from __future__ import annotations

import collections
from functools import lru_cache

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from .. import modis, modis_crops
from ..config import settings
from ..db import connect
from .areas import site_row
from .sentinel2 import crop_payload, crops_payload, grid_png, legend

router = APIRouter(prefix="/api/modis", tags=["modis"])


def _site() -> str:
    with connect() as conn:
        return site_row(conn)["code"]


def _platforms(site: str) -> dict[str, str]:
    """Platform (terra, aqua) of each stored composite, by first day."""
    out = {}
    for y in modis.years(site):
        ds = modis.open_year(site, y)
        for t, p in zip(ds["time"].values, ds["platform"].values):
            out[str(t)[:10]] = modis.PLATFORM_NAMES[int(p)]
    return out


@router.get("/info")
def info() -> dict:
    site = _site()
    platforms = _platforms(site)
    dates = sorted(platforms)
    return {"site": site, "available": bool(dates), "collection": modis.COLLECTION, "start": str(settings.modis_start),
            "resolution_m": round(modis.PIXEL, 2), "years": modis.years(site), "dates": len(dates),
            "first": dates[0] if dates else None, "last": dates[-1] if dates else None,
            "platforms": dict(collections.Counter(platforms.values())),
            "vmin": modis.VMIN, "vmax": modis.VMAX,
            "hint": None if dates else "run: docker compose run --rm ingest modis (no account needed)"}


@router.get("/series")
def series(area: str | None = None, min_clear: float = 0.9) -> dict:
    """Per-composite NDVI statistics over an area (reliable cells only), with the platform of each
    composite; composites where less than ``min_clear`` of the area is reliable are left out."""
    site = _site()
    area = area or site
    with connect() as conn:
        rows = conn.execute(
            """SELECT date, mean, median, p10, p90, std, n_clear, n_area, clear_fraction FROM eo.index_stats
               WHERE site = %s AND area = %s AND index = %s AND clear_fraction >= %s ORDER BY date""",
            (site, area, modis.STAT_NAME, min_clear)).fetchall()
    platforms = _platforms(site)
    return {"site": site, "area": area, "index": "ndvi", "min_clear": min_clear,
            "n_area": rows[0]["n_area"] if rows else None,
            "points": [{"date": r["date"].isoformat(), "platform": platforms.get(r["date"].isoformat()),
                        **{k: (None if r[k] is None else round(float(r[k]), 4))
                           for k in ("mean", "median", "p10", "p90", "std", "clear_fraction")}}
                       for r in rows if r["mean"] is not None]}


_NO_CROPS = "No MODIS crop-group curves yet: run docker compose run --rm ingest modis-crops"


@router.get("/crops")
def crops(area: str | None = None, season: int | None = None) -> dict:
    """Seasons with crop-group curves, and the crop groups of an area in a season (``parcels``: the
    pure 250 m cells of the group, ``ha``: the group's area in them)."""
    site = _site()
    out = crops_payload(site, modis.STAT_NAME, area or site, season, _NO_CROPS)
    return out | {"unit": "cells", "purity": settings.modis_purity, "cell_ha": round(modis_crops.CELL_HA, 2),
                  "ilot_years": modis_crops.rpg_years()[1]}


@router.get("/crop")
def crop(group: str, season: int, area: str | None = None, weight: str = "pixels", ref_area: str | None = None) -> dict:
    """Curve of a crop group in an area for a season: mean of the daily curves of its pure cells
    (covered at least GEOMON_MODIS_PURITY by the group), weighted by the group's share of each cell
    (``weight=pixels``) or not (``parcel``); the composites behind it; the 3-year, 5-year and
    long-term references of the seasons before (since 2000 for permanent crops)."""
    site = _site()
    area = area or site
    out, cur = crop_payload(site, modis.STAT_NAME, group, season, area, weight, ref_area)
    out |= {"unit": "cells", "purity": settings.modis_purity, "cell_ha": round(modis_crops.CELL_HA, 2)}
    if cur is not None:
        out["observations"] = modis_crops.observations(site, season, group, area, weighted=weight == "pixels")
    return out


def _render(site: str, date: str, masked: bool) -> tuple[bytes, list]:
    z = modis.store_path(site, int(date[:4])) / "zarr.json"
    return _render_cached(site, date, masked, z.stat().st_mtime if z.exists() else None)


@lru_cache(maxsize=48)
def _render_cached(site: str, date: str, masked: bool, stamp: float | None) -> tuple[bytes, list]:
    try:
        ds = modis.scene(site, date)
    except KeyError as exc:
        raise HTTPException(404, f"No MODIS composite starting on {date}") from exc
    return grid_png(modis.ndvi_array(ds, masked), ds["x"].values, ds["y"].values, ds.attrs["crs"],
                    modis.VMIN, modis.VMAX, modis.COLORMAP)


@router.get("/ndvi/info")
def image_info(date: str, masked: bool = True) -> dict:
    site = _site()
    _, coords = _render(site, date, masked)
    q = f"date={date}&masked={str(masked).lower()}&site={site}"
    return {"date": date, "platform": _platforms(site).get(date[:10]), "masked": masked,
            "images": [{"part": "", "coordinates": coords, "url": f"/api/modis/ndvi.png?{q}"}],
            "vmin": modis.VMIN, "vmax": modis.VMAX, "legend": legend(modis.VMIN, modis.VMAX, modis.COLORMAP)}


@router.get("/ndvi.png")
def image_png(date: str, masked: bool = True) -> Response:
    png = _render(_site(), date, masked)[0]
    return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=86400"})
