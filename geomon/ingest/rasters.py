"""Helpers shared by raster ingestion: target grid, Zarr writing."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rioxarray  # noqa: F401  (registers the .rio accessor)
import xarray as xr
from rasterio.transform import Affine, from_origin

from ..config import settings
from ..zarrio import write_atomic

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


def write_zarr(ds: xr.Dataset, name: str, grid: Grid) -> Path:
    ds = ds.rio.write_crs(grid.crs)
    ds = ds.rio.write_transform(grid.transform)
    path = settings.site_zarr_dir / f"{name}.zarr"
    write_atomic(ds, path)
    log.info("Wrote %s (%s)", path, ", ".join(ds.data_vars))
    return path

