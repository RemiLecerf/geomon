import datetime as dt
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
    assert [d.code for d in datasets.by_job("refresh-osm")] == ["osm"]
    assert [d.code for d in datasets.by_job("rpg")] == ["rpg"] and datasets.DATASETS["rpg"].steps == ("rpg-new",)


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


def test_hrvpp_dates_recovered_from_truncated_codes():
    from geomon import clms

    code = lambda d: ((d.year % 100) * 1000 + d.timetuple().tm_yday) % 256       # noqa: E731
    s, p, e = dt.date(2023, 3, 13), dt.date(2023, 4, 13), dt.date(2023, 6, 6)          # decoded from real pixels
    assert clms.season_dates(code(s), code(p), code(e), (e - s).days, 2023) == (s, p, e)
    # the length one day short, as the product often gives it
    s, p, e = dt.date(2023, 3, 17), dt.date(2023, 4, 30), dt.date(2023, 10, 14)
    assert clms.season_dates(code(s), code(p), code(e), (e - s).days - 1, 2023) == (s, p, e)
    out = clms.decode_dates(np.array([code(s), 0]), np.array([code(p), 0]), np.array([code(e), 0]),
                            np.array([(e - s).days - 1, 0]), 2023)
    assert out[:, 0].tolist() == [(s - dt.date(2023, 1, 1)).days, (p - dt.date(2023, 1, 1)).days, (e - dt.date(2023, 1, 1)).days]
    assert np.isnan(out[:, 1]).all()                                               # no data


def test_dekads():
    from geomon import clms

    assert [clms.dekad(dt.date(2024, 1, d)) for d in (1, 10, 11, 21, 31)] == [0, 0, 1, 2, 2]
    assert clms.dekad(dt.date(2024, 12, 25)) == 35
    assert clms.dekad_days(dt.date(2024, 2, 21)) == 9 and clms.dekad_days(dt.date(2023, 1, 21)) == 11


def test_soil_points_merged_by_cell():
    """A store whose cells were repeated (the same cell with coordinates differing in the last digits,
    from two product versions) is merged back on the grid, and the new days are added."""
    from geomon import clms
    from geomon.ingest.clms import _merge_points, _snap

    step = clms.CELL_1KM
    lon = _snap([4.5 + 0.5 * step, 4.5 + 1.5 * step], step)
    lat = _snap([45.0 + 0.5 * step] * 2, step)
    t = np.array(["2025-07-10", "2025-07-11", "2025-07-12"], dtype="datetime64[ns]")
    nan = np.nan
    v = np.array([[10, 20, nan, nan], [11, 21, nan, nan], [nan, nan, 12, 22]], dtype="float32")
    old = xr.Dataset({"swi002": (("time", "point"), v)},
                     coords={"time": t, "point": np.arange(4), "lon": ("point", np.r_[lon, lon + 1e-9]),
                             "lat": ("point", np.r_[lat, lat - 1e-9])})
    new_t = np.array(["2025-07-12", "2025-07-13"], dtype="datetime64[ns]")
    times, data, mlon, mlat = _merge_points(old, new_t, {"swi002": np.array([[13, 23], [14, 24]], "float32")}, lon, lat)
    assert len(mlon) == 2 and np.allclose(mlon, lon) and np.allclose(mlat, lat)
    assert list(times) == list(t[:2]) + list(new_t)
    assert data["swi002"].tolist() == [[10, 20], [11, 21], [13, 23], [14, 24]]


def test_clms_reads_only_newer_products():
    from geomon.ingest.clms import _newer

    d = dt.date
    assert _newer(d(2026, 9, 15), None) and not _newer(None, d(2026, 9, 14))
    assert not _newer(d(2026, 9, 15), d(2026, 9, 14))          # the item of the store's last day
    assert _newer(d(2026, 9, 16), d(2026, 9, 14))
    assert not _newer(d(2026, 9, 1), d(2026, 9, 1)) and _newer(d(2026, 9, 11), d(2026, 9, 1))   # dekads


def test_field_values_weight_cells_by_area():
    """A field over two cells takes their values weighted by its area in each; a cell without value
    is left out while the others cover at least half of the field."""
    import pandas as pd
    import shapely

    from geomon import clms

    step = 0.01
    lon, lat = np.array([0.005, 0.015, 0.005]), np.array([0.005, 0.005, 0.015])   # three cells
    field = shapely.box(0.0025, 0.002, 0.0125, 0.004)        # 3/4 in the first cell, 1/4 in the second
    small = shapely.box(0.001, 0.011, 0.002, 0.012)          # inside the third cell
    w = clms.cell_weights([field, small], lon, lat, step)
    assert np.allclose(w.toarray(), [[0.75, 0.25, 0], [0, 0, 1]])
    times = pd.DatetimeIndex(["2025-01-01", "2025-01-02"])
    values = np.array([[10.0, 30.0, 5.0], [10.0, np.nan, np.nan]])
    out = clms.field_values(w, times, values, pd.DatetimeIndex(["2025-01-01", "2025-01-02", "2025-01-03"]))
    assert np.allclose(out[0, :2], [15.0, 10.0]) and np.isnan(out[0, 2])
    assert out[1, 0] == 5.0 and np.isnan(out[1, 1])


def test_dmp_cumulative_fills_inner_gaps():
    import pandas as pd

    from geomon import clms

    dates = pd.DatetimeIndex(["2025-01-01", "2025-01-11", "2025-01-21"])
    cum = clms.dmp_cumulative(np.array([10.0, np.nan, 30.0]), dates)[0]
    assert np.allclose(cum, [0.1, 0.3, 0.63])          # 10 x 10 d, 20 x 10 d, 30 x 11 d (kg -> t)


def test_rpg_years_from_the_wfs_layers(monkeypatch):
    """The RPG years published and their layer: RPG.LATEST holds the newest until it gets its own."""
    import dataclasses

    from geomon import http
    from geomon.ingest import rpg

    def caps(years, latest):
        layers = "".join(f"<Name>RPG.{y}:parcelles_graphiques</Name><Title>RPG {y}</Title>" for y in years)
        return layers + f"<Name>RPG.LATEST:parcelles_graphiques</Name><Title>Registre parcellaire graphique (RPG) {latest} : Parcelles graphiques</Title>"

    monkeypatch.setattr(rpg, "settings", dataclasses.replace(rpg.settings, rpg_year=0))
    for text, want in ((caps([2023, 2024], 2025), {2023: "RPG.2023", 2024: "RPG.2024", 2025: "RPG.LATEST"}),
                       (caps([2023, 2024, 2025], 2026), {2023: "RPG.2023", 2024: "RPG.2024", 2025: "RPG.2025", 2026: "RPG.LATEST"})):
        monkeypatch.setattr(http, "_wfs_capabilities", lambda t=text: t)
        monkeypatch.setattr(rpg, "_wfs_capabilities", lambda t=text: t)
        assert rpg.published() == want
    monkeypatch.setattr(rpg, "settings", dataclasses.replace(rpg.settings, rpg_year=2025))   # GEOMON_RPG_YEAR
    assert max(rpg.published()) == 2025


def test_new_rpg_year_recomputes_the_seasons_that_borrowed_outlines(monkeypatch):
    from geomon import parcels as pc

    monkeypatch.setattr(pc, "current_seasons", lambda today=None: [2026, 2027])
    before, after = list(range(2015, 2026)), list(range(2015, 2027))
    assert pc.changed_seasons(before, after) == [2026, 2027]
    # Statistics of the 2025 outlines: still used by season 2025 up to its padding (mid-February 2026)
    pairs = [(2025, dt.date(2025, 10, 1)), (2025, dt.date(2026, 2, 1)), (2025, dt.date(2026, 6, 1)),
             (2026, dt.date(2026, 6, 1)), (pc.FARM_LAYER, dt.date(2026, 6, 1))]
    assert pc.unused_layers(pairs, after) == [(2025, dt.date(2026, 6, 1))]
    assert pc.unused_layers(pairs, before) == [(2026, dt.date(2026, 6, 1))]
