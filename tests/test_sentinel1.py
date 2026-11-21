import numpy as np
import pytest
import xarray as xr

from geomon import sentinel1 as s1


def test_db_encoding_round_trip():
    linear = xr.DataArray(np.array([0.1, 0.01, 0.0, np.nan, 1.0]))
    stored = s1.encode_db(linear).values
    assert stored.dtype == np.int16
    assert stored.tolist()[:2] == [-1000, -2000] and stored[2] == s1.NODATA and stored[3] == s1.NODATA
    assert s1.decode_db(stored).tolist()[:2] == pytest.approx([-10.0, -20.0])
    assert np.isnan(s1.decode_db(stored)[2]) and s1.decode_db(stored)[4] == 0.0


def _date(vv_db, vh_db):
    enc = lambda a: (np.asarray(a, "float32") / s1.SCALE).round().astype("int16")
    return xr.Dataset({"VV": (("y", "x"), enc(vv_db)), "VH": (("y", "x"), enc(vh_db))})


def test_measures():
    d = _date([[-10.0, -10.0]], [[-16.0, -20.0]])
    assert s1.measure_array(d, "vh_vv").tolist()[0] == pytest.approx([-6.0, -10.0])
    rvi = s1.measure_array(d, "rvi")[0, 0]
    assert rvi == pytest.approx(4 * 10 ** -1.6 / (10 ** -1.0 + 10 ** -1.6), rel=1e-4)


def test_area_means_are_taken_in_linear_power():
    d = _date([[-10.0, -20.0, -15.0, -15.0]], [[-20.0, -20.0, -20.0, -20.0]])
    d["VV"].values[0, 3] = s1.NODATA                              # one pixel without data
    zones = {"a": np.arange(4, dtype="int32")}
    rows = {r["index"]: r for r in s1.area_rows([(d, zones)], {"a": 4}, "asc")}
    vv = rows["vv_asc"]
    expected = 10 * np.log10(np.mean([10 ** -1.0, 10 ** -2.0, 10 ** -1.5]))
    assert vv["mean"] == pytest.approx(expected, abs=1e-4)          # not the mean of the dB values (-15)
    assert vv["median"] == pytest.approx(-15.0)
    assert vv["n_valid"] == 3 and vv["clear_fraction"] == pytest.approx(0.75)
    assert rows["vh_vv_asc"]["mean"] == pytest.approx(rows["vh_asc"]["mean"] - vv["mean"], abs=1e-4)
    assert set(rows) == {"vv_asc", "vh_asc", "vh_vv_asc", "rvi_asc"}


def test_store_paths():
    assert s1.store_path("site", 2024, "asc").parts[-3:] == ("site", "asc", "2024.zarr")
    assert s1.store_path("site", 2024, "desc", "p2").parts[-4:] == ("site", "p2", "desc", "2024.zarr")
