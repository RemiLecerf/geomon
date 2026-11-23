"""Field-level (zonal) aggregation of rasters: the exact share of each grid cell inside each field.

One method for every raster, whatever its resolution against the fields':

- a field over many small cells (Sentinel-2, 10 m) takes all the cells inside it, and the cells its
  outline crosses for the part of them inside it;
- a field smaller than one cell (MODIS 250 m, soil moisture 1 km) takes the value of the cell or
  cells under it, each weighted by the share of the field it holds.

The shares are a sparse matrix W (fields x cells, the fraction of each cell's area inside the
field), computed once per grid and field layer with shapely (GEOS) and cached on disk; a date's
values are then aggregated for every field at once by matrix products. Cells without a value
(clouds, the sea) are left out: ``valid`` is the share of the field's area with a value. Being
linear, the sums of several grids (a site split in parts) simply add.

Outlines: a field is followed on its outline shrunk by ``inner_m`` (10 m for Sentinel-2: the pixels
mixing it with a hedge, a road or the next field left out) when at least ``min_cells`` cells remain
inside, else on its whole outline (``edge``: its values mix in its borders).

Exact to the precision of GEOS: the same fractions as exactextract (to 5e-8, which crashes on some
RPG outlines in its version for Python 3.14) and as a geopandas overlay (1e-9). See the README,
"Field-level aggregation".
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy import sparse

from .config import settings

log = logging.getLogger(__name__)

_MEMORY: dict[tuple, "Coverage"] = {}
MAX_MEMORY = 48                     # coverages kept in memory (a few MB each at 10 m)


@dataclass(frozen=True)
class Grid:
    """A north-up regular grid: CRS, shape (rows, columns) and corner transform (a, b, c, d, e, f)."""
    crs: str
    shape: tuple[int, int]
    transform: tuple[float, ...]

    @classmethod
    def of(cls, ds, crs: str | None = None) -> "Grid":
        """The grid of a dataset with 1-D ``x``/``y`` cell centres (its ``crs`` attribute unless given)."""
        x, y = ds["x"].values, ds["y"].values
        rx, ry = float(x[1] - x[0]), float(y[1] - y[0])
        return cls(crs or ds.attrs["crs"], (len(y), len(x)),
                   (rx, 0.0, float(x[0]) - rx / 2, 0.0, ry, float(y[0]) - ry / 2))

    @property
    def key(self) -> str:
        return hashlib.sha1(repr((self.crs, self.shape, tuple(round(v, 6) for v in self.transform))).encode()).hexdigest()[:16]

    @property
    def cells(self) -> int:
        return self.shape[0] * self.shape[1]


@dataclass(frozen=True)
class Layer:
    """Fields: ids and outlines (WKB, WGS84). ``key`` changes with any id or outline: ``version``
    when given (computed where the outlines are kept, without reading them), else their hash."""
    name: str
    ids: tuple[str, ...]
    wkb: tuple[bytes, ...] = field(repr=False)
    version: str | None = None

    @property
    def key(self) -> str:
        if self.version:
            return self.version
        h = hashlib.sha1(self.name.encode())
        for i, g in zip(self.ids, self.wkb):
            h.update(i.encode())
            h.update(g)
        return h.hexdigest()[:16]

    def geoms(self) -> np.ndarray:
        import shapely

        return shapely.from_wkb(np.array(self.wkb, dtype=object))

    def __len__(self) -> int:
        return len(self.ids)


@dataclass
class Coverage:
    """The cells under each field of a layer on one grid."""
    weights: sparse.csr_matrix          # (fields, cells of the grid): share of each cell inside the field
    edge: np.ndarray                    # fields followed on their whole outline (too small once shrunk)

    def __post_init__(self):
        self.cols = np.unique(self.weights.indices)                 # the cells under some field
        self.w = self.weights[:, self.cols].tocsr()
        self.cells = np.asarray(self.weights.sum(axis=1)).ravel()   # each field's area, in cells

    def sums(self, values: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Weighted sums of ``values`` (bands x cells of the grid, NaN without value): for each field
        and band, sum of w.v, sum of w.v² and sum of w over the cells with a value."""
        v = np.asarray(values, dtype="float64").reshape(values.shape[0], -1)[:, self.cols]
        ok = np.isfinite(v)
        v = np.where(ok, v, 0.0)
        return self.w @ v.T, self.w @ (v * v).T, self.w @ ok.T.astype("float64")


def finish(num: np.ndarray, sq: np.ndarray, den: np.ndarray, total: np.ndarray):
    """Mean, standard deviation and valid share (of ``total``, the fields' area) from summed sums."""
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = np.where(den > 0, num / den, np.nan)
        std = np.sqrt(np.maximum(np.where(den > 0, sq / den, np.nan) - mean * mean, 0.0))
        valid = np.where(total[:, None] > 0, den / total[:, None], 0.0)
    return mean, std, np.clip(valid, 0.0, 1.0)


def coverage_matrix(geoms: np.ndarray, grid: Grid) -> sparse.csr_matrix:
    """(fields x cells) share of each cell's area inside each field (outlines in the grid's CRS).
    Cells wholly inside a field count 1 without any intersection; only those its outline crosses
    are intersected."""
    import shapely

    geoms = np.asarray(geoms, dtype=object)
    ny, nx = grid.shape
    rx, _, x0, _, ery, y0 = grid.transform
    ry = -ery
    if rx <= 0 or ry <= 0:
        raise ValueError("Only north-up grids with increasing x are supported")
    if not len(geoms):
        return sparse.csr_matrix((0, ny * nx))
    empty = shapely.is_empty(geoms) | shapely.is_missing(geoms)
    b = np.where(empty[:, None], np.nan, shapely.bounds(np.where(empty, None, geoms)))
    b = np.nan_to_num(b, nan=0.0)
    c0 = np.clip(np.floor((b[:, 0] - x0) / rx), 0, nx).astype("int64")
    c1 = np.clip(np.ceil((b[:, 2] - x0) / rx), 0, nx).astype("int64")
    r0 = np.clip(np.floor((y0 - b[:, 3]) / ry), 0, ny).astype("int64")
    r1 = np.clip(np.ceil((y0 - b[:, 1]) / ry), 0, ny).astype("int64")
    nc = np.where(empty, 0, np.maximum(c1 - c0, 0))
    nr = np.where(empty, 0, np.maximum(r1 - r0, 0))
    counts = nc * nr
    total = int(counts.sum())
    if not total:
        return sparse.csr_matrix((len(geoms), ny * nx))
    g = np.repeat(np.arange(len(geoms)), counts)
    k = np.arange(total) - np.repeat(np.cumsum(counts) - counts, counts)      # position in each window
    width = np.repeat(np.maximum(nc, 1), counts)
    col = np.repeat(c0, counts) + k % width
    row = np.repeat(r0, counts) + k // width
    boxes = shapely.box(x0 + col * rx, y0 - (row + 1) * ry, x0 + (col + 1) * rx, y0 - row * ry)
    shapely.prepare(geoms[~empty])
    inside = shapely.contains_properly(geoms[g], boxes)
    touch = ~inside & shapely.intersects(geoms[g], boxes)
    w = inside.astype("float64")
    w[touch] = shapely.area(shapely.intersection(geoms[g[touch]], boxes[touch])) / (rx * ry)
    keep = w > 1e-9
    return sparse.csr_matrix((w[keep], (g[keep], row[keep] * nx + col[keep])), shape=(len(geoms), ny * nx))


def _to_grid(geoms: np.ndarray, crs: str) -> np.ndarray:
    import shapely
    from pyproj import CRS, Transformer

    if CRS.from_user_input(crs) == CRS.from_epsg(4326):
        return geoms
    t = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    return shapely.transform(geoms, lambda xy: np.column_stack(t.transform(xy[:, 0], xy[:, 1])))


def _cache_path(layer: Layer, grid: Grid, inner_m: float, min_cells: float) -> Path:
    return settings.data_dir / "cache" / "coverage" / f"{layer.name}_{layer.key}_{grid.key}_{inner_m:g}_{min_cells:g}.npz"


def coverage(layer: Layer, grid: Grid, inner_m: float = 0.0, min_cells: float = 0.0) -> Coverage:
    """The coverage of a layer's fields on a grid: on their outline shrunk by ``inner_m`` (metres,
    a projected grid) where at least ``min_cells`` cells remain, else on the whole outline. Cached
    in memory and on disk (data/cache/coverage), per layer version and grid."""
    import shapely

    key = (layer.key, grid.key, inner_m, min_cells)
    hit = _MEMORY.get(key)
    if hit is not None:
        return hit
    path = _cache_path(layer, grid, inner_m, min_cells)
    if path.exists():
        with np.load(path) as z:
            cov = Coverage(sparse.csr_matrix((z["data"].astype("float64"), z["indices"], z["indptr"]),
                                             shape=tuple(z["shape"])), z["edge"])
    else:
        full = _to_grid(layer.geoms(), grid.crs)
        edge = np.zeros(len(layer), dtype=bool)
        if inner_m:
            inner = shapely.buffer(full, -inner_m)
            # Too small once shrunk: decided on the area (not on one grid's cells), so that a field
            # over two parts of a site keeps the same outline on both
            rx, ry = grid.transform[0], -grid.transform[4]
            edge = shapely.area(inner) / (rx * ry) < max(min_cells, 1e-9)
            w = coverage_matrix(np.where(edge, None, inner), grid)
            if edge.any():
                rows = np.flatnonzero(edge)
                expand = sparse.csr_matrix((np.ones(len(rows)), (rows, np.arange(len(rows)))),
                                           shape=(len(layer), len(rows)))
                w = (w + expand @ coverage_matrix(full[rows], grid)).tocsr()
        else:
            w = coverage_matrix(full, grid)
        w.sum_duplicates()
        w.eliminate_zeros()
        cov = Coverage(w, edge)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp.npz")
        np.savez_compressed(tmp, data=w.data.astype("float32"), indices=w.indices.astype("int32"),
                            indptr=w.indptr.astype("int64"), shape=np.array(w.shape), edge=edge)
        tmp.replace(path)
        for stale in path.parent.glob(f"{layer.name}_*_{grid.key}_{inner_m:g}_{min_cells:g}.npz"):
            if stale != path:                         # an earlier version of the layer on this grid
                stale.unlink(missing_ok=True)
        log.info("Coverage of %s (%d fields) on a %d x %d grid: %d cells, %d fields on their whole outline",
                 layer.name, len(layer), *grid.shape, len(cov.cols), int(edge.sum()))
    if len(_MEMORY) >= MAX_MEMORY:
        _MEMORY.clear()
    _MEMORY[key] = cov
    return cov


def clear_cache(layer_name: str | None = None) -> None:
    """Forgets the coverages in memory; on disk those of a layer (any version) when named."""
    _MEMORY.clear()
    if layer_name:
        for p in (settings.data_dir / "cache" / "coverage").glob(f"{layer_name}_*.npz"):
            p.unlink(missing_ok=True)
