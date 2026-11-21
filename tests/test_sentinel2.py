import numpy as np
import pytest
import xarray as xr

from geomon import sentinel2 as s2


def _scene(b04, b08, scl):
    dims = ("y", "x")
    ds = xr.Dataset({"B04": (dims, np.array(b04, "int16")), "B08": (dims, np.array(b08, "int16")),
                     "SCL": (dims, np.array(scl, "uint8"))},
                    coords={"x": [500005.0, 500015.0], "y": [5000015.0, 5000005.0]})
    ds.attrs["crs"] = "EPSG:32631"
    return ds


def test_clear_mask_rejects_clouds_and_fog():
    scl = np.array([4, 5, 9, 5])
    red = np.array([500, 500, 500, 3000])          # the last one: bright "bare soil" = fog
    assert s2.clear_mask(scl, red).tolist() == [True, True, False, False]
    assert s2.clear_mask(scl).tolist() == [True, True, False, True]


def test_ndvi_masks_clouds():
    ds = _scene([[1000, 1000], [1000, 0]], [[3000, 3000], [3000, 3000]], [[4, 9], [4, 4]])
    v = s2.index_array(ds, "ndvi")
    assert v[0, 0] == pytest.approx(0.5)
    assert np.isnan(v[0, 1]) and np.isnan(v[1, 1])            # cloud; no red reflectance
    assert s2.index_array(ds, "ndvi", masked=False)[0, 1] == pytest.approx(0.5)
    with pytest.raises(KeyError):
        s2.index_array(ds, "ndre")                            # B05 not stored


def test_area_statistics_over_two_parts():
    a = _scene([[1000, 1000], [1000, 1000]], [[3000, 3000], [3000, 3000]], [[4, 4], [9, 9]])
    b = _scene([[1000, 1000], [1000, 1000]], [[1000, 1000], [1000, 1000]], [[4, 4], [4, 4]])
    full = np.arange(4, dtype="int32")                          # every pixel of the 2 x 2 grid
    rows = s2.area_stats_parts([(a, {"site": full}), (b, {"site": full})], {"site": 8}, ["ndvi"])
    (r,) = rows
    assert r["n_clear"] == 6 and r["clear_fraction"] == pytest.approx(0.75)
    assert r["mean"] == pytest.approx((2 * 0.5 + 4 * 0.0) / 6)


def test_zones_match_masks():
    """Communes drawn into one label image give the same pixels as one mask per commune."""
    x = 500005.0 + 10 * np.arange(60)
    y = 5000595.0 - 10 * np.arange(60)
    ds = xr.Dataset(coords={"x": x, "y": y}, attrs={"crs": "EPSG:32631"})
    from pyproj import Transformer

    to_wgs = Transformer.from_crs("EPSG:32631", "EPSG:4326", always_xy=True).transform

    def square(x0, y0, x1, y1):
        ring = [to_wgs(*p) for p in ((x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0))]
        return {"type": "Polygon", "coordinates": [ring]}

    areas = {"c1": square(500000, 5000000, 500300, 5000600), "c2": square(500300, 5000000, 500600, 5000600),
             "site": square(500000, 5000000, 500600, 5000600), "farm": square(500100, 5000100, 500400, 5000200)}
    zones = s2.area_zones(ds, areas, {"c1", "c2"})
    masks = s2.area_masks(ds, areas)
    for code in areas:
        assert zones[code].tolist() == np.flatnonzero(masks[code].ravel()).tolist(), code
    assert zones["c1"].size + zones["c2"].size == zones["site"].size


def test_wanted_bands_always_include_ndvi_and_scl():
    bands = s2.wanted_bands()
    assert {"B04", "B08"} <= set(bands) and bands[-1] == "SCL"
    assert s2.available_indices({"B04", "B08", "B05"}) == ["ndvi", "ndre"]


def test_two_pass_requests_only_runs_of_clear_new_dates():
    from geomon.ingest import sentinel2 as ing

    d = [np.datetime64(f"2026-10-{k:02d}") for k in (1, 2, 4, 14, 17, 19, 21, 27, 29, 31)]
    keep = {d[0], d[2], d[3], d[4], d[5], d[7], d[9]}                   # 02, 21 and 29 too cloudy
    assert ing.runs_of(d, keep) == [(d[0], d[0]), (d[2], d[5]), (d[7], d[7]), (d[9], d[9])]
    scl = xr.Dataset({"SCL": (("time", "y", "x"), np.array([[[4, 4], [9, 9]], [[8, 9], [9, 9]]], "uint8"))},
                     coords={"time": d[:2], "y": [1.0, 0.0], "x": [0.0, 1.0]})
    fr = ing.clear_dates(scl, np.ones((2, 2), bool))
    assert fr == {d[0]: 0.5, d[1]: 0.0}


def test_store_encoding_is_sharded_per_date():
    from geomon.ingest import sentinel2 as ing

    ds = xr.Dataset({"B04": (("time", "y", "x"), np.zeros((2, 700, 1100), "int16"))})
    enc = ing._encoding(ds)["B04"]
    assert enc["chunks"] == (1, 512, 512) and enc["shards"] == (1, 1024, 1536)
