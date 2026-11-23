"""Per-field series of a raster product (see geomon.zonal): one Zarr store per site, product and
field layer, ``data/zarr/sites/<site>/fields/<product>/<layer>.zarr`` (layers: ``rpg<year>``, the
RPG parcels of a year, and ``farm``, the farm's fields).

Dimensions (time, field); for each variable ``v`` (an index, a band...):

- ``v_mean``, ``v_std`` (float32): over the cells with a value, weighted by their share in the field;
- ``v_valid`` (uint8): the share of the field's area with a value, in % (clouds, shadows and missing
  data left out); NOT_DONE (255) where the date has not been aggregated for this variable yet.

Field coordinates: ``field`` (id), ``cells`` (the field's area in the product's cells, on the
outline used), ``edge`` (followed on its whole outline: too small once shrunk). Attributes:
``layer``, ``layer_key`` (the layer's version: a store of another version is rebuilt), ``product``.

A store is small (a season of Sentinel-2 over 2,500 parcels: about 1 MB for one index) and rewritten
whole at each update (atomically); readers open it lazily, in chunks of FIELD_CHUNK fields.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

from .config import settings
from .zarrio import open_cached, write_atomic

log = logging.getLogger(__name__)

NOT_DONE = 255
FIELD_CHUNK = 256


def product_dir(site: str, product: str) -> Path:
    return settings.site_dir(site) / "fields" / product


def path(site: str, product: str, layer: str) -> Path:
    return product_dir(site, product) / f"{layer}.zarr"


def layers(site: str, product: str) -> list[str]:
    d = product_dir(site, product)
    return sorted(p.stem for p in d.glob("*.zarr") if not p.name.endswith(".tmp.zarr")) if d.exists() else []


def open_store(site: str, product: str, layer: str, load: bool = False) -> xr.Dataset | None:
    return open_cached(path(site, product, layer), load=load)


def done(ds: xr.Dataset | None, var: str) -> set:
    """The dates aggregated for a variable (as numpy datetime64[D])."""
    if ds is None or f"{var}_valid" not in ds:
        return set()
    v = ds[f"{var}_valid"].values
    t = ds["time"].values.astype("datetime64[D]")
    return set(t[(v != NOT_DONE).any(axis=1)]) if v.size else set()


def merge(site: str, product: str, layer, cells: np.ndarray, edge: np.ndarray, times, data: dict) -> xr.Dataset:
    """Adds aggregated dates to a layer's store (replacing those already there).

    ``layer``: the geomon.zonal.Layer; ``times``: the dates; ``data``: variable -> (mean, std,
    valid), each (fields, dates), valid as a share 0..1. A store of another version of the layer is
    replaced."""
    p = path(site, product, layer.name)
    old = open_cached(p, load=True)
    if old is not None and old.attrs.get("layer_key") != layer.key:
        log.info("Fields %s/%s: new version of the layer, store rebuilt", product, layer.name)
        old = None
    times = pd.DatetimeIndex(times).normalize()
    all_times = times if old is None else pd.DatetimeIndex(old["time"].values).union(times)
    pos_new = all_times.get_indexer(times)
    names = sorted(set(data) | ({v[:-6] for v in old.data_vars if v.endswith("_valid")} if old is not None else set()))
    out = {}
    for var in names:
        mean = np.full((len(all_times), len(layer)), np.nan, "float32")
        std = np.full_like(mean, np.nan)
        valid = np.full(mean.shape, NOT_DONE, "uint8")
        if old is not None and f"{var}_valid" in old:
            pos_old = all_times.get_indexer(pd.DatetimeIndex(old["time"].values))
            mean[pos_old], std[pos_old] = old[f"{var}_mean"].values, old[f"{var}_std"].values
            valid[pos_old] = old[f"{var}_valid"].values
        if var in data:
            m, s, v = data[var]
            mean[pos_new], std[pos_new] = m.T, s.T
            valid[pos_new] = np.round(100 * v.T).astype("uint8")
        out[f"{var}_mean"] = (("time", "field"), mean, {"long_name": f"{var}, mean over the field"})
        out[f"{var}_std"] = (("time", "field"), std, {"long_name": f"{var}, standard deviation over the field"})
        out[f"{var}_valid"] = (("time", "field"), valid,
                               {"long_name": f"share of the field's area with a {var} value", "units": "%",
                                "flag_values": NOT_DONE, "flag_meanings": "not_aggregated"})
    ds = xr.Dataset(out, coords={"time": all_times.values, "field": np.array(layer.ids, dtype=str),
                                 "cells": ("field", np.asarray(cells, "float32")), "edge": ("field", np.asarray(edge, bool))},
                    attrs={"product": product, "layer": layer.name, "layer_key": layer.key})
    _write(ds, p)
    return ds


def _write(ds: xr.Dataset, p: Path) -> None:
    chunks = {"time": max(1, len(ds["time"])), "field": min(FIELD_CHUNK, max(1, ds.sizes["field"]))}
    enc = {v: {"chunks": tuple(chunks[d] for d in ds[v].dims)} for v in ds.data_vars}
    write_atomic(ds, p, encoding=enc)


def keep_dates(site: str, product: str, layer: str, keep) -> int:
    """Keeps only the dates for which ``keep(date)`` (a datetime.date) is true; removes the store when
    none remains. Returns the number of dates removed."""
    p = path(site, product, layer)
    ds = open_cached(p, load=False)                   # the dates only: loaded when it is rewritten
    if ds is None:
        return 0
    days = pd.DatetimeIndex(ds["time"].values)
    mask = np.array([bool(keep(d.date())) for d in days], dtype=bool)
    if mask.all():
        return 0
    if not mask.any():
        remove(site, product, layer)
    else:
        _write(ds.isel(time=np.flatnonzero(mask)).load(), p)
    return int((~mask).sum())


def remove(site: str, product: str, layer: str | None = None) -> None:
    """A layer's store, or all the stores of a product (``layer`` None)."""
    target = path(site, product, layer) if layer else product_dir(site, product)
    if target.exists():
        shutil.rmtree(target)                      # open_cached sees the store gone (no zarr.json)


def series(site: str, product: str, layer: str, field_id: str, var: str, d0=None, d1=None) -> pd.DataFrame:
    """One field's dates aggregated for a variable: date, mean, std, valid (0..1)."""
    ds = open_store(site, product, layer)
    if ds is None or f"{var}_valid" not in ds or field_id not in ds.indexes["field"]:
        return pd.DataFrame(columns=["date", "mean", "std", "valid"])
    one = ds[[f"{var}_mean", f"{var}_std", f"{var}_valid"]].sel(field=field_id)
    if d0 is not None or d1 is not None:
        one = one.sel(time=slice(np.datetime64(d0) if d0 else None, np.datetime64(d1) if d1 else None))
    one = one.load()
    valid = one[f"{var}_valid"].values
    ok = valid != NOT_DONE
    return pd.DataFrame({"date": pd.DatetimeIndex(one["time"].values[ok]).date, "mean": one[f"{var}_mean"].values[ok],
                         "std": one[f"{var}_std"].values[ok], "valid": valid[ok] / 100.0})


def table(site: str, product: str, layer: str, var: str, d0=None, d1=None, ids=None):
    """(dates, field ids, mean (dates x fields), valid 0..1 (NaN where not aggregated), cells) of a
    layer over a period, for some fields (all when ``ids`` is None); None without a store."""
    ds = open_store(site, product, layer)
    if ds is None or f"{var}_valid" not in ds:
        return None
    sub = ds[[f"{var}_mean", f"{var}_valid", "cells"]]
    if d0 is not None or d1 is not None:
        sub = sub.sel(time=slice(np.datetime64(d0) if d0 else None, np.datetime64(d1) if d1 else None))
    if ids is not None:
        index = ds.indexes["field"]
        pos = index.get_indexer(pd.Index([str(i) for i in ids]))
        sub = sub.isel(field=pos[pos >= 0])
    sub = sub.load()
    valid = sub[f"{var}_valid"].values.astype("float32")
    valid[valid == NOT_DONE] = np.nan
    return (pd.DatetimeIndex(sub["time"].values).date, sub["field"].values.astype(str), sub[f"{var}_mean"].values,
            valid / 100.0, sub["cells"].values)
