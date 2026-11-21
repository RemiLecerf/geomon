"""Locks, sites, atomic store writes, tiles."""

import json

import numpy as np
import pytest
import xarray as xr

from geomon import config, zarrio
from geomon.api import rasters, tiles
from geomon.ingest.lock import lock_group, lock_key


def test_lock_groups():
    assert lock_group("sentinel2-parcels") == "sentinel2"
    assert lock_group("rpg") == "main" and lock_group("osm") == "osm"
    # One OSM run at a time over every site; other groups are per site
    assert lock_key("osm", "a") == lock_key("osm", "b")
    assert lock_key("main", "a") != lock_key("main", "b")
    assert all(0 <= k < 2 ** 31 for k in lock_key("main", "a"))


def test_sites_file(tmp_path, monkeypatch):
    p = tmp_path / "sites.json"
    p.write_text(json.dumps([{"code": "x", "communes": ["38259", 38244]}, {"code": "y", "name": "Y", "communes": ["50167"]}]))
    monkeypatch.setattr(config, "sites_path", lambda: p)
    config._SITES_CACHE.clear()
    sites = config.all_sites()
    assert [s.code for s in sites] == ["x", "y"] and sites[0].communes == ("38259", "38244")
    assert sites[0].name == "x"                                  # default name: the code
    assert config.current_site().code == "x"                     # the first one by default
    with config.use_site("y") as s:
        assert s.name == "Y" and config.settings.site_code == "y"
    with pytest.raises(KeyError):
        config.get_site("z")
    p.write_text(json.dumps([{"code": "x", "communes": ["1"]}, {"code": "x", "communes": ["2"]}]))
    import os
    os.utime(p, (1, 1))                                          # another mtime: the file is read again
    with pytest.raises(ValueError, match="duplicate"):
        config.all_sites()
    config._SITES_CACHE.clear()


def test_netcdf_backend_is_installed(tmp_path):
    """openEO (Sentinel), the CDS (ERA5, AgERA5) deliver NetCDF, read with h5netcdf (needs h5py)."""
    path = tmp_path / "x.nc"
    xr.Dataset({"v": ("t", np.arange(3.0))}).to_netcdf(path, engine="h5netcdf")
    with xr.open_dataset(path, engine="h5netcdf") as ds:
        assert ds["v"].values.tolist() == [0.0, 1.0, 2.0]


def test_write_atomic_replaces_a_store(tmp_path):
    path = tmp_path / "w" / "p.zarr"
    zarrio.write_atomic(xr.Dataset({"v": ("t", np.arange(3.0))}), path)
    zarrio.write_atomic(xr.Dataset({"v": ("t", np.arange(5.0))}), path)
    assert xr.open_zarr(path)["v"].values.tolist() == [0, 1, 2, 3, 4]
    assert sorted(p.name for p in path.parent.iterdir()) == ["p.zarr"]      # no temporary store left


def test_tile_coordinates_are_checked():
    from fastapi import HTTPException

    for z, x, y in ((-1, 0, 0), (3, 8, 0), (3, 0, -1), (23, 0, 0)):
        with pytest.raises(HTTPException):
            tiles.tile("rpg", z, x, y)
    with pytest.raises(HTTPException):
        tiles.tile("nothing", 10, 0, 0)


def test_raster_tile_resampling():
    from rasterio.enums import Resampling

    assert rasters._resampling(False, 5, 100) == Resampling.average          # zoomed out
    assert rasters._resampling(False, 5, 4) == Resampling.bilinear
    assert rasters._resampling(False, 250, 200) == Resampling.nearest        # coarse soil cells stay crisp
    assert rasters._resampling(True, 250, 5000) == Resampling.nearest
    assert rasters._lat_of(0.0) == pytest.approx(0.0)
    assert rasters._lat_of(5_621_521.0) == pytest.approx(45.0, abs=1e-3)


def test_raster_level_bounds():
    lv = rasters.Level(4, 20.0, 1000.0, 9000.0, 50, 40)
    assert lv.bounds == (1000.0, 8200.0, 2000.0, 9000.0)
