"""Copernicus Climate Data Store (CDS): what is published, requests, reading the downloads.

Needs a free Copernicus account: GEOMON_CDS_API_KEY is the personal access token
(https://cds.climate.copernicus.eu/profile); each dataset's licence is accepted once on its
"Download" tab. Requests wait in the CDS queue (minutes, sometimes an hour).
"""

from __future__ import annotations

import datetime as dt
import tempfile
import zipfile
from pathlib import Path

import xarray as xr

from ..config import settings
from ..http import session


def available_until(dataset: str) -> dt.date:
    """Last date published: the end of the date range offered by the dataset's request form."""
    s = session()
    r = s.get(f"{settings.cds_api_url}/catalogue/v1/collections/{dataset}", timeout=60)
    r.raise_for_status()
    coll = r.json()
    form_url = next(link["href"] for link in coll["links"] if link.get("rel") == "form")
    form = s.get(form_url, timeout=60).json()
    for w in form:
        if w.get("name") == "date" and w.get("details", {}).get("maxEnd"):
            return dt.date.fromisoformat(w["details"]["maxEnd"][:10])
    return dt.date.fromisoformat(coll["extent"]["temporal"]["interval"][0][1][:10])


def retrieve(dataset: str, request: dict, target: Path) -> None:
    """One request, downloaded to ``target`` (written under a temporary name, then renamed)."""
    import cdsapi  # imported here: only the CDS steps need it

    client = cdsapi.Client(url=settings.cds_api_url, key=settings.cds_api_key, quiet=True, progress=False,
                           retry_max=3, sleep_max=60)
    part = target.with_name(target.name + ".part")
    try:
        client.retrieve(dataset, request, str(part))
    except Exception as exc:
        msg = str(exc).lower()
        if "licen" in msg:
            raise RuntimeError(
                f"The licence of {dataset} is not accepted for this CDS account: open "
                f"https://cds.climate.copernicus.eu/datasets/{dataset}?tab=download and accept it") from exc
        if "queued requests" in msg:
            raise RuntimeError(
                "The CDS already has the maximum number of queued requests for this dataset on this account. "
                "Wait for them to finish (or cancel them at https://cds.climate.copernicus.eu/requests) "
                "and run the step again") from exc
        raise
    part.replace(target)


def open_download(path: Path) -> list[xr.Dataset]:
    """A download (one NetCDF file, or a zip of several) as loaded datasets."""
    if not zipfile.is_zipfile(path):
        return [xr.open_dataset(path, engine="h5netcdf").load()]
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(path) as z:
            z.extractall(tmp)
        for p in sorted(Path(tmp).rglob("*")):
            if p.suffix in (".nc", ".nc4", ".netcdf"):
                with xr.open_dataset(p, engine="h5netcdf") as ds:
                    out.append(ds.load())
    return out
