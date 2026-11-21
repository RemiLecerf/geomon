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


_OPEN: dict[tuple, tuple[float, xr.Dataset]] = {}


def open_cached(path: Path, load: bool = True) -> xr.Dataset | None:
    """A store, loaded in memory (``load``) or opened lazily, opened again when it was rewritten
    (one copy per path), or None when there is none."""
    key = (str(path), load)
    z = Path(path) / "zarr.json"
    if not z.exists():
        _OPEN.pop(key, None)
        return None
    mtime = z.stat().st_mtime
    hit = _OPEN.get(key)
    if hit is None or hit[0] != mtime:
        ds = xr.open_zarr(path, consolidated=True)
        hit = _OPEN[key] = (mtime, ds.load() if load else ds)
    return hit[1]


def read_attrs(path: Path) -> dict | None:
    """A store's attributes, from its metadata file alone (None when there is no store)."""
    import json

    z = Path(path) / "zarr.json"
    return json.loads(z.read_text()).get("attributes", {}) if z.exists() else None


def update_attrs(path: Path, **attrs) -> None:
    """Sets attributes of a store and consolidates its metadata (which also dates the store, so
    the readers' caches see the change)."""
    import zarr

    g = zarr.open_group(str(path), mode="a")
    g.attrs.update(attrs)
    zarr.consolidate_metadata(str(path))


_JSON: dict[str, tuple[float, object]] = {}


def read_json(path: Path):
    """A JSON file, parsed once per version (mtime), or None when there is none."""
    path = Path(path)
    if not path.exists():
        return None
    mtime = path.stat().st_mtime
    hit = _JSON.get(str(path))
    if hit is None or hit[0] != mtime:
        import json
        hit = _JSON[str(path)] = (mtime, json.loads(path.read_text(encoding="utf-8")))
    return hit[1]


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """A file written beside ``path`` then renamed over it: readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)


def write_atomic(ds: xr.Dataset, path: Path, **to_zarr_kwargs) -> None:
    """Writes ``ds`` as a new store and swaps it in for the old one (see :func:`staged`)."""
    with staged(path) as tmp:
        ds.to_zarr(tmp, mode="w", consolidated=True, **to_zarr_kwargs)
