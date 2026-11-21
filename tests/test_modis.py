import datetime as dt

import numpy as np
import pytest
import xarray as xr

from geomon import modis
from geomon import sentinel2 as s2
from geomon.ingest import modis as ing


def test_window_and_tiles_follow_the_global_grid():
    # A point of Isère, in tile h18v04
    from pyproj import Transformer

    x, y = Transformer.from_crs("EPSG:4326", modis.CRS, always_xy=True).transform(5.0, 45.4)
    win = modis.window_of((x - 1000, y - 1000, x + 1000, y + 1000))
    assert modis.tiles_of(win) == [(18, 4)]
    xs, ys = modis.cell_centres(win)
    assert xs[0] - modis.PIXEL / 2 <= x - 1000 and xs[-1] + modis.PIXEL / 2 >= x + 1000
    assert ys[0] + modis.PIXEL / 2 >= y + 1000 and ys[-1] - modis.PIXEL / 2 <= y - 1000
    assert np.allclose(np.diff(xs), modis.PIXEL) and np.allclose(np.diff(ys), -modis.PIXEL)


def test_window_across_two_tiles_is_split():
    t = modis.TILE
    win = (4 * t + 10, 4 * t + 20, 18 * t - 3, 18 * t + 5)        # 3 cells in h17, 5 in h18
    assert modis.tiles_of(win) == [(17, 4), (18, 4)]
    (r, c, h, w), (rows, cols) = modis.tile_slices(win, 17, 4)
    assert (r, c, h, w) == (10, t - 3, 10, 3) and (cols.start, cols.stop) == (0, 3)
    (r, c, h, w), (rows, cols) = modis.tile_slices(win, 18, 4)
    assert (r, c, h, w) == (10, 0, 10, 5) and (cols.start, cols.stop) == (3, 8)
    assert modis.tile_slices(win, 19, 4) is None


def _composite(ndvi, rel):
    return xr.Dataset({"ndvi": (("y", "x"), np.asarray(ndvi, "int16")),
                       "reliability": (("y", "x"), np.asarray(rel, "int8"))})


def test_ndvi_masks_unreliable_cells():
    d = _composite([[5000, 8000, modis.NDVI_NODATA, 3000]], [[0, 3, -1, 1]])
    assert modis.ndvi_array(d, masked=False)[0, :2].tolist() == pytest.approx([0.5, 0.8])
    m = modis.ndvi_array(d)
    assert m[0, 0] == pytest.approx(0.5) and np.isnan(m[0, 1]) and np.isnan(m[0, 2]) and m[0, 3] == pytest.approx(0.3)


def test_area_rows_use_reliable_cells_only():
    d = _composite([[5000, 8000, modis.NDVI_NODATA, 3000]], [[0, 3, -1, 1]])
    (row,) = modis.area_rows(d, {"a": np.arange(4, dtype="int32")})
    assert row["index"] == modis.STAT_NAME
    assert row["n_area"] == 4 and row["n_valid"] == 3 and row["n_clear"] == 2
    assert row["clear_fraction"] == pytest.approx(0.5) and row["mean"] == pytest.approx(0.4)


def test_small_area_gets_the_cell_under_it():
    from pyproj import Transformer

    to = Transformer.from_crs("EPSG:4326", modis.CRS, always_xy=True)
    x, y = to.transform(5.0, 45.4)
    win = modis.window_of((x - 700, y - 700, x + 700, y + 700))
    xs, ys = modis.cell_centres(win)
    ds = xr.Dataset({"ndvi": (("y", "x"), np.zeros((len(ys), len(xs)), "int16"))}, coords={"x": xs, "y": ys},
                    attrs={"crs": modis.CRS})
    lon, lat = Transformer.from_crs(modis.CRS, "EPSG:4326", always_xy=True).transform(xs[2] + 50, ys[1] - 30)
    d = 0.0002                                         # about 20 m: holds no cell centre
    tiny = {"type": "Polygon", "coordinates": [[[lon - d, lat - d], [lon + d, lat - d], [lon + d, lat + d],
                                               [lon - d, lat + d], [lon - d, lat - d]]]}
    zones = s2.area_zones(ds, {"farm": tiny})
    assert zones["farm"].tolist() == [1 * len(xs) + 2]


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def _feature(pid, date, h=18, v=4):
    href = f"https://modiseuwest.blob.core.windows.net/modis-061-cogs/{pid}/{h}/{v:02d}/x/{pid}.A.h{h}v{v:02d}"
    return {"id": f"{pid}.A2026001.h{h}v{v:02d}.061.1", "properties": {
        "start_datetime": f"{date}T00:00:00.000000Z", "modis:horizontal-tile": h, "modis:vertical-tile": v},
        "assets": {modis.NDVI_ASSET: {"href": href + "_NDVI.tif"}, modis.RELIABILITY_ASSET: {"href": href + "_rel.tif"}}}


def test_search_asks_for_few_fields_and_follows_post_pages(monkeypatch):
    calls = []
    pages = [{"features": [_feature("MOD13Q1", "2026-01-01")],
              "links": [{"rel": "next", "href": ing.PC_STAC, "method": "POST", "body": {"token": "next:2"}, "merge": True}]},
             {"features": [_feature("MYD13Q1", "2026-01-09")], "links": []}]

    class Session:
        def post(self, url, json, timeout):
            calls.append(json)
            return _Resp(pages[len(calls) - 1])

    monkeypatch.setattr(ing, "session", lambda: Session())
    out = ing.search([4.9, 45.3, 5.1, 45.5], dt.date(2026, 1, 1), dt.date(2026, 12, 31))
    assert [(o["date"], o["platform"], o["h"], o["v"]) for o in out] == [
        (dt.date(2026, 1, 1), 0, 18, 4), (dt.date(2026, 1, 9), 1, 18, 4)]
    assert "fields" in calls[0] and "geometry" in calls[0]["fields"]["exclude"]
    assert calls[1]["token"] == "next:2" and calls[1]["collections"] == [modis.COLLECTION]     # merged body


def test_token_is_reused_then_renewed(monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    expiries = [now + dt.timedelta(minutes=5), now + dt.timedelta(hours=1)]
    got = []

    class Session:
        def get(self, url, timeout):
            got.append(url)
            return _Resp({"token": f"t{len(got)}", "msft:expiry": expiries[len(got) - 1].isoformat()})

    monkeypatch.setattr(ing, "session", lambda: Session())
    tokens = ing._Tokens()
    href = "https://modiseuwest.blob.core.windows.net/modis-061-cogs/MOD13Q1/a.tif"
    assert tokens.sign(href) == href + "?t1"
    assert tokens.sign(href) == href + "?t2"            # 5 minutes left: renewed
    assert tokens.sign(href) == href + "?t2"
    assert got[0].endswith("/token/modiseuwest/modis-061-cogs") and len(got) == 2


def test_composite_merges_tiles_and_masks_outside(monkeypatch):
    t = modis.TILE

    class G:
        window = (4 * t, 4 * t + 2, 18 * t - 2, 18 * t + 2)
        shape = (2, 4)
        inside = np.array([[True, True, True, False], [True, True, True, True]])

    def read(href, h, win):
        _, _, height, width = win
        value = h * 100 if "NDVI" in href else (0 if h == 17 else 1)
        return np.full((height, width), value, dtype="int16")

    monkeypatch.setattr(ing, "_read_window", read)
    tiles = [{"h": h, "v": 4, "ndvi": f"{h}_NDVI", "reliability": f"{h}_rel"} for h in (17, 18)]
    ndvi, rel = ing._read_composite(G, tiles)
    assert ndvi.tolist() == [[1700, 1700, 1800, modis.NDVI_NODATA], [1700, 1700, 1800, 1800]]
    assert rel.tolist() == [[0, 0, 1, modis.REL_NODATA], [0, 0, 1, 1]]
    assert ndvi.dtype == np.int16 and rel.dtype == np.int8


def test_years_are_searched_again_until_late_composites_are_published(tmp_path, monkeypatch):
    class G:
        window = (1, 2, 3, 4)

    monkeypatch.setattr(modis, "store_path", lambda site, year: tmp_path / f"{year}.zarr")
    today = dt.date(2026, 11, 20)
    assert ing._year_task(G, "s", 2026, today)["stored"] is None          # no store: the whole year
    (tmp_path / "2025.zarr").mkdir()
    (tmp_path / "2025.zarr" / "zarr.json").write_text("{}")
    attrs = {"window": [1, 2, 3, 4], "searched_until": "2026-01-20"}
    monkeypatch.setattr(ing, "read_attrs", lambda path: attrs)
    t = ing._year_task(G, "s", 2025, today)
    assert t["start"] == dt.date(2025, 12, 6) and t["end"] == dt.date(2025, 12, 31)
    attrs["searched_until"] = "2026-02-15"                                # 45 days after the year's end
    keys = sorted(ing.expected(2025, today))
    stored = xr.Dataset(coords={"time": [np.datetime64(d, "ns") for d, _ in keys],
                                "platform": ("time", np.array([p for _, p in keys], "int8"))})
    monkeypatch.setattr(modis, "open_year", lambda site, year: stored)
    assert ing._year_task(G, "s", 2025, today) is None                    # complete
    gaps = stored.isel(time=[k for k, (d, _) in enumerate(keys) if not dt.date(2025, 7, 1) <= d <= dt.date(2025, 11, 10)])
    monkeypatch.setattr(modis, "open_year", lambda site, year: gaps)
    t = ing._year_task(G, "s", 2025, today)                               # a gap in the catalogue: searched again
    assert t["start"] == dt.date(2025, 7, 4) and t["end"] == dt.date(2025, 11, 24) and t["gaps"] == 17
    attrs["window"] = [0, 2, 3, 4]                                         # the site changed
    assert ing._year_task(G, "s", 2025, today)["start"] == dt.date(2025, 1, 1)


def test_expected_composites_follow_each_platform_cycle():
    full = ing.expected(2024, dt.date(2026, 1, 1))
    assert len(full) == 46                                        # 23 Terra (day 1, 17...) + 23 Aqua (day 9, 25...)
    assert (dt.date(2024, 1, 1), 0) in full and (dt.date(2024, 1, 9), 1) in full and (dt.date(2024, 12, 26), 1) in full
    first = ing.expected(2000, dt.date(2026, 1, 1))
    assert min(first) == (dt.date(2000, 2, 18), 0) and all(p == 0 for _, p in first)     # Aqua from July 2002
    recent = ing.expected(2026, dt.date(2026, 4, 1))              # published about 2 months after their first day
    assert max(d for d, _ in recent) == dt.date(2026, 1, 25) and len(recent) == 4


def test_crop_seasons_before_the_first_rpg_assume_permanent_crops():
    from geomon import modis_crops as mc

    years = [2015, 2016, 2025]
    assert mc.geometry_year(2016, years) == 2016 and mc.geometry_year(2027, years) == 2025
    assert mc.geometry_year(2005, years) == 2015
    assert mc.crop_status(2005, 2015, "18") == "assumed" and mc.crop_status(2005, 2015, "2") == "unknown"
    assert mc.crop_status(2016, 2016, "2") == "declared"
    assert mc.crop_status(2012, 2012, "2", ilots=True) == "ilots"


def test_pure_cells_keep_cells_mostly_covered_by_one_group(monkeypatch):
    from geomon import modis_crops as mc

    pur = {"groups": np.array(["2", "18"]), "share": np.array([[[80, 40], [0, 75]], [[20, 60], [100, 0]]], "uint8")}
    pure = mc.pure_cells(pur, min_share=0.75)
    assert pure["2"][0].tolist() == [0, 3] and pure["2"][1].tolist() == pytest.approx([0.8, 0.75])
    assert pure["18"][0].tolist() == [2]


def test_cell_curves_interpolate_reliable_composites_over_the_season():
    from geomon import modis_crops as mc
    from geomon import parcels as pc

    days = np.arange(0, 480, 8)                                   # middle days of the composites
    values = np.full((len(days), 2), np.nan, "float32")
    values[:, 0] = 0.5 + 0.3 * np.sin(days / 480 * np.pi)         # a cell seen all season
    values[:3, 1] = 0.6                                           # a cell seen three times only
    curves = mc.cell_curves(days, values, np.array([0, 1]), 2026)
    assert set(curves) == {0}
    z = curves[0]
    assert len(z) == pc.SEASON_DAYS and z[16] == pytest.approx(values[2, 0], abs=1e-6)
    assert z[12] == pytest.approx((values[1, 0] + values[2, 0]) / 2, abs=1e-6)


def test_the_farm_crop_replaces_the_rpg_one():
    from geomon import parcels as pc

    # A parcel declared as wheat in the RPG, temporary grassland in the farm's land cover
    assert pc.effective_crop(2023, 2023, "1", "BTH", ("19", "PTR")) == ("19", "PTR", "farm")
    assert pc.effective_crop(2023, 2023, "1", "BTH", None) == ("1", "BTH", "declared")
    # Seasons without RPG yet: known where the farm says, unknown elsewhere (not a permanent crop)
    assert pc.effective_crop(2026, 2025, "19", "PTR", ("19", "PTR"))[2] == "farm"
    assert pc.effective_crop(2026, 2025, "19", "PTR", None)[2] == "unknown"
    assert pc.effective_crop(2026, 2025, "18", "PPH", None)[2] == "assumed"


def test_group_status_of_a_purity_variant():
    from geomon import modis_crops as mc

    assert mc._group_status({"declared"}) == "declared" and mc._group_status({"ilots"}) == "ilots"
    assert mc._group_status({"declared", "farm"}) == "declared" and mc._group_status({"farm"}) == "farm"
    assert mc._group_status({"assumed", "farm"}) == "assumed"


def test_the_farm_fields_replace_the_rpg_parcels_they_cover():
    from geomon import parcels as pc

    farm = {(pc.FARM_LAYER, "Fabc"): ("16", "LUZ"), (2025, "6024713"): (None, None)}
    assert pc.in_season(farm, pc.FARM_LAYER, "Fabc")              # the farm's field, declared this season
    assert not pc.in_season(farm, pc.FARM_LAYER, "Fother")        # a field of another year's file
    assert not pc.in_season(farm, 2025, "6024713")                # the RPG parcel it replaces
    assert pc.in_season(farm, 2025, "5944811")                    # a neighbour's RPG parcel
