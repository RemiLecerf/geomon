"""Writing Zarr stores that other processes (the web API) may be reading."""

from __future__ import annotations

import logging
import shutil
import time
import warnings
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import xarray as xr

log = logging.getLogger(__name__)

# Zarr 3 warns at every write that consolidated metadata is not in the Zarr 3 specification yet;
# the stores are consolidated on purpose (one read to open them)
warnings.filterwarnings("ignore", message="Consolidated metadata is currently not part", category=UserWarning)


@contextmanager
def staged(path: Path) -> Iterator[Path]:
    """A new store written beside ``path`` (yielded), then swapped in for the old one when the
    block ends without error: readers see the old store or the new one, never a half-written one.
    For stores written piece by piece (see ``geomon.ingest.dem``).

    On Windows bind mounts a directory rename can fail for a moment after the old store is
    removed: it is retried, then the new store is copied instead.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.zarr")
    if tmp.exists():
        shutil.rmtree(tmp)
    yield tmp
    for attempt in range(5):
        try:
            if path.exists():
                shutil.rmtree(path)
            tmp.rename(path)
            return
        except OSError as exc:
            log.debug("Swap of %s failed (%s), retrying", path.name, exc)
            time.sleep(1 + attempt)
    shutil.rmtree(path, ignore_errors=True)
    shutil.copytree(tmp, path)
    shutil.rmtree(tmp, ignore_errors=True)


def write_atomic(ds: xr.Dataset, path: Path, **to_zarr_kwargs) -> None:
    """Writes ``ds`` as a new store and swaps it in for the old one (see :func:`staged`)."""
    with staged(path) as tmp:
        ds.to_zarr(tmp, mode="w", consolidated=True, **to_zarr_kwargs)
