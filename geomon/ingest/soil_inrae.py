"""French soil properties at 90 m (INRAE, Gis Sol), stored like SoilGrids for the same tools.

Two national datasets of digital soil mapping (GlobalSoilMap depths, Licence Ouverte 2.0), on the
Recherche Data Gouv repository (formerly data.inrae.fr):

- "Réservoir utile des sols de la France métropolitaine" (doi:10.15454/9IRARJ): available water capacity
  per horizon and over 0-200 cm (with its standard deviation), water content at field capacity and at
  wilting point. Computed with pedotransfer functions fitted on French measurements (SOLHYDRO),
  better suited to French soils than Saxton & Rawls on SoilGrids.
- "Propriétés de granulométrie ... et d'éléments grossiers ... au pas de 90 m" (doi:10.57745/N4E4NE):
  clay, silt, sand, coarse fragments.

The files are national GeoTIFFs (about 640 MB each, LZW, one-row strips): only the rows of the
site's extent are read, by HTTP range requests on the repository's signed links (about 20 MB and 2 s
per layer for a few communes, instead of 640 MB). Stored in ``data/zarr/sites/<site>/soil_inrae.zarr``
on a 90 m Lambert-93 grid, with the variable names of the SoilGrids store (clay, sand, silt, cfvo,
theta_fc, theta_wp, awc, awc_total, texture_usda) plus awc_total_sd.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import rasterio
import xarray as xr
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT

from ..aoi import AOI
from ..http import session
from .rasters import Grid, write_zarr
from .soil import DEPTHS, USDA_CLASSES, usda_texture

log = logging.getLogger(__name__)

API = "https://entrepot.recherche.data.gouv.fr/api"
RU_DOI = "doi:10.15454/9IRARJ"
TEXTURE_DOI = "doi:10.57745/N4E4NE"
RES_M = 90
READERS = 4
GDAL_ENV = {"CPL_VSIL_CURL_USE_HEAD": "NO", "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
            "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES", "GDAL_HTTP_MAX_RETRY": "4", "GDAL_HTTP_RETRY_DELAY": "3"}

# variable -> (dataset, file name with {d} for the depth "0_5"..., units, long name); None depth: one layer
_D = "{d}"
LAYERS = {
    "clay": (TEXTURE_DOI, f"argile.{_D}.tif", "percent", "Clay content (INRAE, 90 m)"),
    "silt": (TEXTURE_DOI, f"limon.{_D}.tif", "percent", "Silt content (INRAE, 90 m)"),
    "sand": (TEXTURE_DOI, f"sable.{_D}.tif", "percent", "Sand content (INRAE, 90 m)"),
    "cfvo": (TEXTURE_DOI, f"coarse_{_D}.preds.mean.tif", "percent", "Coarse fragments (INRAE, 90 m)"),
    "theta_fc": (RU_DOI, f"SMFC_{_D}.tif", "m**3/m**3", "Water content at field capacity (INRAE, French pedotransfer functions)"),
    "theta_wp": (RU_DOI, f"SMPWP_{_D}.tif", "m**3/m**3", "Water content at wilting point (INRAE, French pedotransfer functions)"),
    "awc": (RU_DOI, f"awc_mm_{_D}.tif", "mm", "Available water capacity of the layer (INRAE)"),
    "awc_total": (RU_DOI, "AWCr_mm_0_200.tif", "mm", "Available water capacity, 0-200 cm (INRAE réservoir utile)"),
    "awc_total_sd": (RU_DOI, "AWCr_mm_0_200.sd.tif", "mm", "Standard deviation of the available water capacity, 0-200 cm (INRAE)"),
}


def _files(doi: str) -> dict[str, int]:
    r = session().get(f"{API}/datasets/:persistentId/", params={"persistentId": doi}, timeout=120)
    r.raise_for_status()
    return {f["dataFile"]["filename"]: f["dataFile"]["id"] for f in r.json()["data"]["latestVersion"]["files"]}


def _signed(file_id: int) -> str:
    """The repository answers a download with a redirect to a signed S3 link (valid 4 hours)."""
    r = session().get(f"{API}/access/datafile/{file_id}", allow_redirects=False, timeout=120)
    if r.status_code not in (301, 302, 303, 307) or "Location" not in r.headers:
        raise RuntimeError(f"No download link for file {file_id} (HTTP {r.status_code})")
    return r.headers["Location"]


def _read(file_id: int, grid: Grid) -> np.ndarray:
    with rasterio.Env(**GDAL_ENV), rasterio.open(f"/vsicurl/{_signed(file_id)}") as src, WarpedVRT(
            src, crs=grid.crs, transform=grid.transform, width=grid.width, height=grid.height,
            resampling=Resampling.nearest, src_nodata=src.nodata, nodata=np.nan, dtype="float32") as vrt:
        a = vrt.read(1).astype("float32")
    a[(a < -1e30) | (a > 1e30)] = np.nan
    return a


def _as_percent(a: np.ndarray) -> np.ndarray:
    """Fractions in g/kg (0-1000) or as a share (0-1) -> percent."""
    v = a[np.isfinite(a)]
    if v.size and np.nanmax(v) > 100.5:
        return a / 10.0
    if v.size and np.nanmax(v) <= 1.0:
        return a * 100.0
    return a


def run(aoi: AOI) -> dict:
    grid = Grid(*aoi.bbox_l93, res=RES_M)
    files = {doi: _files(doi) for doi in (RU_DOI, TEXTURE_DOI)}
    jobs = []
    for var, (doi, pattern, _u, _n) in LAYERS.items():
        depths = [f"{t}_{b}" for t, b in DEPTHS] if _D in pattern else [None]
        for k, d in enumerate(depths):
            name = pattern.replace(_D, d) if d else pattern
            if name not in files[doi]:
                raise RuntimeError(f"{doi}: no file {name} (the dataset changed?)")
            jobs.append((var, k if d else None, files[doi][name]))
    with ThreadPoolExecutor(max_workers=READERS) as pool:
        arrays = list(pool.map(lambda j: _read(j[2], grid), jobs))
    data: dict[str, np.ndarray] = {}
    for (var, k, _), a in zip(jobs, arrays):
        if k is None:
            data[var] = a
        else:
            data.setdefault(var, np.full((len(DEPTHS), grid.height, grid.width), np.nan, "float32"))[k] = a
    for v in ("clay", "silt", "sand", "cfvo"):
        data[v] = _as_percent(data[v])
    texture = usda_texture(data["sand"], data["silt"], data["clay"])


    dims3 = ("depth", "y", "x")
    variables = {v: ((dims3 if data[v].ndim == 3 else ("y", "x")), data[v].astype("float32"), {"units": u, "long_name": n})
                 for v, (_doi, _p, u, n) in LAYERS.items()}
    variables["texture_usda"] = (dims3, texture, {
        "units": "1", "long_name": "USDA texture class (from the INRAE texture)",
        "flag_values": list(range(len(USDA_CLASSES))), "flag_meanings": " ".join(c.replace(" ", "_") for c in USDA_CLASSES)})
    ds = xr.Dataset(variables, coords={
        "depth": ("depth", [f"{t}-{b}cm" for t, b in DEPTHS]),
        "depth_top": ("depth", np.array([t for t, _ in DEPTHS], dtype="float32"), {"units": "cm"}),
        "depth_bottom": ("depth", np.array([b for _, b in DEPTHS], dtype="float32"), {"units": "cm"}),
        "y": ("y", grid.y, {"units": "m"}), "x": ("x", grid.x, {"units": "m"})},
        attrs={"source": "INRAE / Gis Sol, digital soil maps of metropolitan France at 90 m (doi:10.15454/9IRARJ, "
                         "doi:10.57745/N4E4NE), Licence Ouverte 2.0", "aoi": aoi.code, "nd_layers": len(DEPTHS)})
    write_zarr(ds, "soil_inrae", grid)
    total = data["awc_total"]
    return {"shape": [len(DEPTHS), grid.height, grid.width], "res_m": RES_M, "layers": len(jobs),
            "awc_total_mm_mean": float(np.nanmean(total)) if np.isfinite(total).any() else None}
