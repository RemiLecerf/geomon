import numpy as np
import pytest
import xarray as xr

from geomon import datasets, radar
from geomon.ingest import radar as ing


def test_catalogue_is_consistent():
    from geomon.ingest.__main__ import STEPS
    from geomon.scheduler import JOBS

    jobs = {j.name: j for j in JOBS}
    for d in datasets.DATASETS.values():
        assert d.category in datasets.CATEGORIES and d.default in d.modes
        assert set(d.modes) <= set(datasets.MODES)
        assert all(s in STEPS for s in d.steps), d.code
        if d.scheduled_job():
            assert d.scheduled_job() in jobs, d.code          # every followed dataset has its job
        assert not (d.shared and d.delete_sql)                 # shared data is never deleted per site
    assert datasets.by_job("radar")[0].code == "radar" and datasets.DATASETS["radar"].default == "live"
    assert datasets.by_job("radar-mf")[0].code == "radar_mf" and datasets.needs("radar") is None
    assert [d.code for d in datasets.by_job("refresh-rpg")] == ["rpg"]


def test_paths_are_those_of_the_site():
    tmp_path = datasets.settings.data_dir          # a scratch folder in the tests (conftest.py)
    for p in ("zarr/sentinel1/a/asc/2024.zarr", "zarr/sentinel1/b/asc/2024.zarr", "zarr/sites/a/weather/era5land.zarr",
              "zarr/sites/a/weather/era5land_daily.zarr", "zarr/sites/a/weather/era5.zarr"):
        (tmp_path / p).mkdir(parents=True)
        (tmp_path / p / "zarr.json").write_text("{}")
    assert [p.name for p in datasets.paths("a", "sentinel1")] == ["a"]
    assert [p.name for p in datasets.paths("a", "era5land")] == ["era5land.zarr", "era5land_daily.zarr"]
    assert datasets.size_bytes("a", "sentinel1", fresh=True) == 2


def _image(t, value, nx=4, ny=3):
    return xr.Dataset({"rain": (("time", "y", "x"), np.full((1, ny, nx), value, "uint16"))},
                      coords={"time": [np.datetime64(t, "ns")], "y": np.arange(ny)[::-1] * 500.0,
                              "x": np.arange(nx) * 500.0}, attrs={"crs": "EPSG:3857"})


def test_live_mode_keeps_the_last_hours():
    ds = None
    for k in range(30):                                        # 2.5 hours of images
        ds = ing.merge(ds, _image(np.datetime64("2026-11-20T10:00") + np.timedelta64(5 * k, "m"), k), keep_hours=2)
    assert ds.sizes["time"] == 24 and str(ds["time"].values[0])[:16] == "2026-11-20T10:30"
    again = ing.merge(ds, _image("2026-11-20T12:25", 7), keep_hours=None)   # the same image again: replaced
    assert again.sizes["time"] == 24 and int(again["rain"].values[-1, 0, 0]) == 7
    moved = ing.merge(ds, _image("2026-11-20T12:30", 1, nx=5), keep_hours=2)  # another grid: restarted
    assert moved.sizes["time"] == 1


def test_accumulation_counts_missing_values_as_dry():
    imgs = [_image(np.datetime64("2026-11-20T10:00") + np.timedelta64(5 * k, "m"), 100) for k in range(12)]
    ds = xr.concat(imgs, dim="time")
    ds["rain"].values[3] = radar.NODATA
    total, cov = radar.accumulation(ds, 1)
    assert total[0, 0] == pytest.approx(11 * 1.0) and cov["images"] == 12 and cov["coverage"] == 1.0
    total, cov = radar.accumulation(ds, 3)
    assert cov["expected"] == 36 and cov["coverage"] == pytest.approx(12 / 36)


def test_odim_mosaic_is_read_and_georeferenced(tmp_path):
    h5py = pytest.importorskip("h5py")
    from pyproj import Transformer

    proj = "+proj=stere +lat_0=90 +lon_0=0 +lat_ts=45 +ellps=WGS84 +x_0=0 +y_0=0"
    to = Transformer.from_crs("EPSG:4326", proj, always_xy=True)
    x0, y0 = to.transform(-1.5, 48.7)
    inv = Transformer.from_crs(proj, "EPSG:4326", always_xy=True)
    ul_lon, ul_lat = inv.transform(x0, y0)
    raw = np.zeros((200, 300), "uint16")
    raw[10, 20] = 150                                          # 1.5 mm
    raw[0, 0] = 65535
    path = tmp_path / "m.h5"
    with h5py.File(path, "w") as h:
        h.create_group("what").attrs.update({"date": b"20261120", "time": b"101500", "object": b"COMP"})
        h.create_group("where").attrs.update({"projdef": proj.encode(), "xsize": 300, "ysize": 200, "xscale": 500.0,
                                             "yscale": 500.0, "UL_lon": ul_lon, "UL_lat": ul_lat})
        d = h.create_group("dataset1")
        d.create_group("what").attrs.update({"enddate": b"20261120", "endtime": b"101500"})
        g = d.create_group("data1")
        g.create_dataset("data", data=raw)
        g.create_group("what").attrs.update({"quantity": b"ACRR", "gain": 1.0, "offset": 0.0, "nodata": 65535})
        q = d.create_group("data2")
        q.create_dataset("data", data=np.full((200, 300), 80, "uint8"))
        q.create_group("what").attrs.update({"quantity": b"QIND"})
    m = ing.read_mosaic(path)
    assert m["time"] == np.datetime64("2026-11-20T10:15", "ns")
    assert m["rain"][10, 20] == 150 and m["rain"][0, 0] == radar.NODATA
    assert m["x"][20] == pytest.approx(x0 + 20.5 * 500) and m["y"][10] == pytest.approx(y0 - 10.5 * 500)


def test_reflectivity_to_rain_marshall_palmer():
    dbz = np.array([np.nan, -9.999e6, 0.0, 23.0, 40.0, 70.0], "float32")
    out = radar.dbz_to_stored(dbz)
    assert out[0] == 0 and out[1] == radar.NODATA and out[2] == 0          # dry, no radar, below 7 dBZ
    rate = lambda z: (10 ** (z / 10) / 200) ** (1 / 1.6)
    assert radar.rate_mmh(out[3:4])[0] == pytest.approx(rate(23.0), abs=0.13)  # about 1 mm/h
    assert radar.rate_mmh(out[4:5])[0] == pytest.approx(rate(40.0), rel=0.01)  # about 11.5 mm/h
    assert radar.rate_mmh(out[5:6])[0] == pytest.approx(rate(radar.MAX_DBZ), rel=0.01)   # capped


def test_intensity_colours_leave_dry_cells_transparent():
    rgba = radar.intensity_rgba(np.array([[np.nan, 0.0, 0.2, 100.0]], "float32"))
    assert rgba[0, 0, 3] == 0 and rgba[0, 1, 3] == 0 and rgba[0, 2, 3] > 0
    assert tuple(rgba[0, 3, :3]) == tuple(int(radar.INTENSITY[-1][1][k:k + 2], 16) for k in (1, 3, 5))


def test_opera_keys_are_parsed():
    m = ing._KEY.search("2026/11/20/OPERA/COMP/OPERA@20261120T1510@0@DBZH.tiff")
    assert m and m.group(1) == "20261120T1510"
    assert ing._KEY.search("2026/11/20/OPERA/COMP/OPERA@20261120T1500@0@RATE.tiff") is None


def test_groundwater_spikes_and_zero_depths_are_dropped():
    import numpy as np

    from geomon.api.soils import clean_levels

    depth = 20 + 0.3 * np.sin(np.arange(200) / 30)
    depth[50] = 0.0              # a day recorded at ground level
    depth[120] = 5.0             # a spike
    depth[150:] -= 1.5           # a real rise (kept)
    good = clean_levels(depth)
    assert not good[50] and not good[120] and good[149:].all() and good.sum() == 198
