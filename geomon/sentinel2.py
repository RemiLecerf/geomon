"""Sentinel-2 L2A: storage, cloud masking, spectral indices (Specifications §4.1).

Storage: one Zarr store per year, ``data/zarr/sentinel2/<site>/<year>.zarr``,
dimensions ``(time, y, x)`` on the native Sentinel-2 grid (UTM, 10 m; 20 m
bands are delivered resampled to 10 m by the back-end, nearest neighbour).

Parts: when the communes of a site form separate clusters (more than 5 km
apart), each cluster has its own grid and stores, ``<site>/<part>/<year>.zarr``
(parts p1, p2... by decreasing area), so that the empty land between them is
neither downloaded nor processed. A site in one block has a single part, ``""``,
stored directly under ``<site>/``.

Variables: the reflectance bands kept (int16 digital numbers; reflectance =
DN x 0.0001, the offset of processing baseline 04.00 is already removed by the
back-end) and ``SCL``, the Sen2Cor scene classification used as cloud mask.
Each date carries ``clear_fraction`` and ``valid_fraction`` over the AOI.

Cloud mask (``clear_mask``): SCL classes 4, 5, 6, and red reflectance below 0.25,
which removes the fog and low cloud that Sen2Cor classifies as bare soil.

Indices are computed from the stored bands; adding one only needs its formula
here (and its bands in GEOMON_S2_BANDS).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
import xarray as xr

from .config import settings

COLLECTION = "SENTINEL2_L2A"
SCALE = 0.0001

# Bands of the collection (name -> description, native resolution in m)
BANDS = {
    "B01": ("Coastal aerosol, 443 nm", 60), "B02": ("Blue, 492 nm", 10), "B03": ("Green, 560 nm", 10),
    "B04": ("Red, 665 nm", 10), "B05": ("Red edge 1, 704 nm", 20), "B06": ("Red edge 2, 741 nm", 20),
    "B07": ("Red edge 3, 783 nm", 20), "B08": ("NIR, 833 nm", 10), "B8A": ("Narrow NIR, 865 nm", 20),
    "B09": ("Water vapour, 945 nm", 60), "B11": ("SWIR 1, 1614 nm", 20), "B12": ("SWIR 2, 2202 nm", 20),
}

# Sen2Cor scene classification
SCL_CLASSES = {
    0: "No data", 1: "Saturated or defective", 2: "Dark area pixels", 3: "Cloud shadows", 4: "Vegetation",
    5: "Not vegetated", 6: "Water", 7: "Unclassified", 8: "Cloud, medium probability",
    9: "Cloud, high probability", 10: "Thin cirrus", 11: "Snow or ice",
}
# Pixels used for indices: vegetation, not vegetated, water. "Unclassified" (7) is left out: it is
# mostly haze and cloud edges.
CLEAR_CLASSES = (4, 5, 6)
# Sen2Cor classifies fog and flat low cloud over land as "not vegetated" (class 5); they are far
# brighter in the red than any land surface here. On clear dates 99.9 % of clear pixels have a
# red reflectance below 0.265 (2022-2025, Montseveroux).
MAX_CLEAR_RED = 0.25


@dataclass(frozen=True)
class Index:
    name: str
    long_name: str
    a: str           # normalized difference (a - b) / (a + b)
    b: str
    vmin: float      # display range
    vmax: float
    colormap: str


INDICES = {
    i.name: i for i in (
        Index("ndvi", "NDVI, normalized difference vegetation index (B08, B04)", "B08", "B04", -0.1, 0.9, "RdYlGn"),
        Index("ndre", "NDRE, normalized difference red edge index (B08, B05)", "B08", "B05", -0.1, 0.6, "RdYlGn"),
        Index("ndwi", "NDWI, normalized difference water index (B03, B08)", "B03", "B08", -0.8, 0.4, "BrBG"),
        Index("ndmi", "NDMI, normalized difference moisture index (B08, B11)", "B08", "B11", -0.4, 0.6, "BrBG"),
    )
}


def wanted_bands() -> list[str]:
    """Spectral bands to keep (configuration), in collection order, plus SCL."""
    bands = {"B04", "B08", *settings.s2_bands}
    unknown = bands - set(BANDS)
    if unknown:
        raise ValueError(f"Unknown Sentinel-2 band(s) in GEOMON_S2_BANDS: {', '.join(sorted(unknown))}")
    return [b for b in BANDS if b in bands] + ["SCL"]


def available_indices(bands) -> list[str]:
    return [i.name for i in INDICES.values() if i.a in bands and i.b in bands]


# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------
def site_dir(site: str) -> Path:
    return settings.zarr_dir / "sentinel2" / site


def part_dir(site: str, part: str = "") -> Path:
    return site_dir(site) / part if part else site_dir(site)


def store_path(site: str, year: int, part: str = "") -> Path:
    return part_dir(site, part) / f"{year}.zarr"


def _years_in(d: Path) -> list[int]:
    return sorted(int(p.stem) for p in d.glob("*.zarr") if p.stem.isdigit() and (p / "zarr.json").exists()) \
        if d.exists() else []


def parts(site: str) -> list[str]:
    """The parts with stores: "" (a site in one block), or p1, p2..."""
    d = site_dir(site)
    if not d.exists():
        return []
    out = [""] if _years_in(d) else []
    out += sorted(p.name for p in d.iterdir() if p.is_dir() and not p.name.endswith(".zarr") and _years_in(p))
    return out


def years(site: str, part: str | None = None) -> list[int]:
    """Years with a store (in one part, or in any part when ``part`` is None)."""
    if part is not None:
        return _years_in(part_dir(site, part))
    return sorted({y for p in parts(site) for y in _years_in(part_dir(site, p))})


@lru_cache(maxsize=32)
def _open(path: str, mtime: float) -> xr.Dataset:
    return xr.open_zarr(path, consolidated=True)


def open_year(site: str, year: int, part: str = "") -> xr.Dataset | None:
    p = store_path(site, year, part)
    z = p / "zarr.json"
    return _open(str(p), z.stat().st_mtime) if z.exists() else None


def clear_cache() -> None:
    _open.cache_clear()


def scenes(site: str, date: str) -> list[tuple[str, xr.Dataset]]:
    """One date in every part that has it: [(part, dataset loaded)]."""
    out = []
    for p in parts(site):
        try:
            out.append((p, scene(site, date, p)))
        except KeyError:
            pass
    return out


def scene(site: str, date: str, part: str = "") -> xr.Dataset:
    """One date (all bands) of one part, loaded."""
    d = dt.date.fromisoformat(date[:10])
    ds = open_year(site, d.year, part)
    if ds is None:
        raise KeyError(date)
    t = np.datetime64(d)
    times = ds["time"].values.astype("datetime64[D]")
    hits = np.nonzero(times == t)[0]
    if not hits.size:
        raise KeyError(date)
    return ds.isel(time=int(hits[0])).load()


def crs_of(ds: xr.Dataset) -> str:
    return ds.attrs["crs"]


# ---------------------------------------------------------------------------
# Masks and indices
# ---------------------------------------------------------------------------
def clear_mask(scl: np.ndarray, red: np.ndarray | None = None) -> np.ndarray:
    """Clear pixels: SCL vegetation, not vegetated or water, and (when B04 is given) not too bright
    in the red, which catches the fog and low cloud that Sen2Cor misses."""
    clear = np.isin(scl, CLEAR_CLASSES)
    if red is not None:
        clear &= red < MAX_CLEAR_RED / SCALE
    return clear


def clear_mask_ds(ds: xr.Dataset) -> np.ndarray:
    return clear_mask(ds["SCL"].values, ds["B04"].values if "B04" in ds else None)


def index_array(ds: xr.Dataset, name: str, masked: bool = True) -> np.ndarray:
    """Index of one date (2-D), NaN where cloudy or without data."""
    ix = INDICES[name]
    if ix.a not in ds or ix.b not in ds:
        raise KeyError(f"{name} needs bands {ix.a} and {ix.b}: add them to GEOMON_S2_BANDS")
    a = ds[ix.a].values.astype("float32")
    b = ds[ix.b].values.astype("float32")
    with np.errstate(divide="ignore", invalid="ignore"):
        out = (a - b) / (a + b)
    bad = (a <= 0) | (b <= 0) | ~np.isfinite(out)
    if masked:
        bad |= ~clear_mask_ds(ds)
    out[bad] = np.nan
    return out


def _grid(ds: xr.Dataset):
    """Shape and affine transform (pixel corners) of a store's grid."""
    from rasterio.transform import Affine

    x, y = ds["x"].values, ds["y"].values
    rx, ry = float(x[1] - x[0]), float(y[1] - y[0])
    return (len(y), len(x)), Affine(rx, 0, x[0] - rx / 2, 0, ry, y[0] - ry / 2)


def _to_grid_crs(ds: xr.Dataset):
    from pyproj import Transformer

    return Transformer.from_crs("EPSG:4326", crs_of(ds), always_xy=True).transform


def area_masks(ds: xr.Dataset, areas: dict[str, dict]) -> dict[str, np.ndarray]:
    """Pixels whose centre falls in each area (GeoJSON in WGS84), on the store grid: one boolean
    image per area. For many areas (the communes of a département), see :func:`area_zones`."""
    from rasterio.features import geometry_mask
    from shapely.geometry import mapping, shape
    from shapely.ops import transform

    out_shape, aff = _grid(ds)
    to_grid = _to_grid_crs(ds)
    return {code: geometry_mask([mapping(transform(to_grid, shape(g)))], out_shape=out_shape,
                                transform=aff, invert=True)
            for code, g in areas.items()}


def area_zones(ds: xr.Dataset, areas: dict[str, dict], partition: set[str] = frozenset()) -> dict[str, np.ndarray]:
    """Pixels whose centre falls in each area, as flat indices into the store grid (int32, sorted).

    The areas of ``partition`` (the communes, which do not overlap) are drawn into one label image
    and split by label: memory grows with the site's pixels, not with the number of communes
    (one boolean image per commune of a département would take tens of GB). The other areas (the
    site, the farm) get one mask each.
    """
    from rasterio.features import rasterize
    from shapely.geometry import mapping, shape
    from shapely.ops import transform

    out_shape, aff = _grid(ds)
    to_grid = _to_grid_crs(ds)
    zones: dict[str, np.ndarray] = {}
    labelled = [c for c in areas if c in partition]
    if labelled:
        lab = rasterize([(mapping(transform(to_grid, shape(areas[c]))), k + 1) for k, c in enumerate(labelled)],
                        out_shape=out_shape, transform=aff, fill=0, dtype="int32").ravel()
        order = np.argsort(lab, kind="stable").astype("int32")       # pixels grouped by label, ascending
        bounds = np.searchsorted(lab[order], np.arange(len(labelled) + 2))
        for k, c in enumerate(labelled):
            zones[c] = order[bounds[k + 1]:bounds[k + 2]]
    for c, m in area_masks(ds, {c: g for c, g in areas.items() if c not in partition}).items():
        zones[c] = np.flatnonzero(m.ravel()).astype("int32")
    return zones


def area_stats(ds_date: xr.Dataset, masks: dict[str, np.ndarray], indices: list[str]) -> list[dict]:
    """Statistics of each index over each area for one (loaded) date (areas as boolean images)."""
    zones = {c: np.flatnonzero(m.ravel()).astype("int32") for c, m in masks.items()}
    return area_stats_parts([(ds_date, zones)], {c: int(z.size) for c, z in zones.items()}, indices)


def area_stats_parts(pieces: list[tuple[xr.Dataset, dict[str, np.ndarray]]], n_area: dict[str, int],
                     indices: list[str]) -> list[dict]:
    """Statistics of each index over each area for one date seen in one or more parts.

    ``pieces``: (loaded date of a part, area pixels on that part's grid as flat indices, see
    :func:`area_zones`) for the parts that have the date; ``n_area``: pixels of each area over all
    the parts (the clear fraction is relative to it).
    """
    prepared = []
    for ds_date, zones in pieces:
        scl = ds_date["SCL"].values.ravel()
        clear = clear_mask_ds(ds_date).ravel()
        values = {name: index_array(ds_date, name).ravel() for name in indices}     # once per part and date
        prepared.append((scl, clear, values, zones))
    rows = []
    for code, total in n_area.items():
        n_valid = n_clear = 0
        vals = {name: [] for name in indices}
        for scl, clear, values, zones in prepared:
            idx = zones.get(code)
            if idx is None or not idx.size:
                continue
            n_valid += int((scl[idx] > 0).sum())
            n_clear += int(clear[idx].sum())
            for name in indices:
                vals[name].append(values[name][idx])
        for name in indices:
            v = np.concatenate(vals[name]) if vals[name] else np.zeros(0, dtype="float32")
            v = v[np.isfinite(v)]
            row = {"area": code, "index": name, "n_area": total, "n_valid": n_valid, "n_clear": n_clear,
                   "clear_fraction": n_clear / total if total else 0.0,
                   "mean": None, "median": None, "p10": None, "p90": None, "std": None}
            if v.size:
                p10, p50, p90 = np.percentile(v, [10, 50, 90])
                row |= {"mean": float(v.mean()), "median": float(p50), "p10": float(p10), "p90": float(p90),
                        "std": float(v.std())}
            rows.append(row)
    return rows
