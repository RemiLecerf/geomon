"""Helpers shared by raster ingestion: target grid, Zarr writing, catalogue."""

from __future__ import annotations

import json
import logging
import math
import shutil
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rioxarray  # noqa: F401  (registers the .rio accessor)
import xarray as xr
from rasterio.transform import Affine, from_origin

from ..config import settings
from ..db import connect

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Grid:
    """A north-up grid in the working CRS (Lambert-93)."""

    x0: float
    y0: float
    x1: float
    y1: float
    res: float
    crs: str = settings.work_crs

    @property
    def width(self) -> int:
        return int(math.ceil((self.x1 - self.x0) / self.res))

    @property
    def height(self) -> int:
        return int(math.ceil((self.y1 - self.y0) / self.res))

    @property
    def transform(self) -> Affine:
        return from_origin(self.x0, self.y1, self.res, self.res)

    @property
    def x(self) -> np.ndarray:
        return self.x0 + self.res * (np.arange(self.width) + 0.5)

    @property
    def y(self) -> np.ndarray:
        return self.y1 - self.res * (np.arange(self.height) + 0.5)


def write_zarr(ds: xr.Dataset, name: str, grid: Grid, source: str, description: str) -> Path:
    ds = ds.rio.write_crs(grid.crs)
    ds = ds.rio.write_transform(grid.transform)
    path = settings.site_zarr_dir / f"{name}.zarr"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.rmtree(path)
    ds.to_zarr(path, mode="w", consolidated=True)
    log.info("Wrote %s (%s)", path, ", ".join(ds.data_vars))

    variables = {
        v: {k: ds[v].attrs.get(k) for k in ("units", "long_name") if k in ds[v].attrs}
        | {"dims": list(ds[v].dims)}
        for v in ds.data_vars
        if v != "spatial_ref"
    }
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO meta.raster_dataset (name, path, crs, resolution, source, description, attrs, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, now())
            ON CONFLICT (name) DO UPDATE SET path = EXCLUDED.path, crs = EXCLUDED.crs,
                resolution = EXCLUDED.resolution, source = EXCLUDED.source,
                description = EXCLUDED.description, attrs = EXCLUDED.attrs, updated_at = now()
            """,
            (name, str(path.relative_to(settings.data_dir)), grid.crs, grid.res, source, description,
             json.dumps({"variables": variables, "bbox": [grid.x0, grid.y0, grid.x1, grid.y1]})),
        )
    return path
