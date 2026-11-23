"""Field-level aggregation (geomon.zonal) and its per-field stores (geomon.fields)."""

import numpy as np
import pandas as pd
import shapely
import xarray as xr

from geomon import fields, zonal


def _grid(nx=4, ny=3, size=10.0, x0=0.0, y0=30.0, crs="EPSG:2154"):
    """A north-up grid of ``size`` m cells whose top-left corner is (x0, y0)."""
    return zonal.Grid(crs, (ny, nx), (size, 0.0, x0, 0.0, -size, y0))


def _layer(name, geoms_l93):
    """A layer from Lambert-93 outlines (stored as WGS84, as the RPG)."""
    from pyproj import Transformer

    t = Transformer.from_crs("EPSG:2154", "EPSG:4326", always_xy=True)
    wgs = shapely.transform(np.array(geoms_l93, dtype=object), lambda xy: np.column_stack(t.transform(xy[:, 0], xy[:, 1])))
    return zonal.Layer(name, tuple(f"f{i}" for i in range(len(geoms_l93))), tuple(shapely.to_wkb(wgs)))


def test_grid_of_a_dataset():
    ds = xr.Dataset(coords={"x": [5.0, 15.0, 25.0], "y": [25.0, 15.0]}, attrs={"crs": "EPSG:2154"})
    g = zonal.Grid.of(ds)
    assert g.shape == (2, 3) and g.transform == (10.0, 0.0, 0.0, 0.0, -10.0, 30.0)


def test_coverage_fractions_fine_and_coarse():
    """A field over several cells takes each for its share inside; a field inside one cell takes that
    cell; the shares are those of the cells' areas."""
    grid = _grid()
    big = shapely.box(5, 5, 25, 25)            # 4 quarter cells at its corners, 4 half cells, 1 whole
    small = shapely.box(32, 22, 34, 24)        # inside the cell (row 0, column 3)
    w = zonal.coverage_matrix(np.array([big, small], dtype=object), grid).toarray().reshape(2, 3, 4)
    assert np.allclose(w[0], [[0.25, 0.5, 0.25, 0], [0.5, 1, 0.5, 0], [0.25, 0.5, 0.25, 0]])
    assert np.allclose(w[1], [[0, 0, 0, 0.04], [0, 0, 0, 0], [0, 0, 0, 0]])
    assert np.isclose(w[0].sum() * 100, big.area)


def test_shrunk_outline_and_fallback(tmp_path, monkeypatch):
    """Outlines shrunk by ``inner_m``; a field too small once shrunk is followed on its whole
    outline (``edge``). The coverage is cached on disk and found again."""
    from geomon.config import settings

    grid = _grid(nx=6, ny=6, y0=60.0)
    layer = _layer("test", [shapely.box(0, 0, 60, 60), shapely.box(0, 0, 15, 15)])
    a = zonal.coverage(layer, grid, inner_m=10, min_cells=4)
    assert list(a.edge) == [False, True]
    assert np.isclose(a.cells[0], 16, atol=1e-3)       # 40 m x 40 m left once shrunk: 16 cells
    assert np.isclose(a.cells[1], 2.25, atol=1e-3)     # its whole outline: 15 m x 15 m
    zonal.clear_cache()
    b = zonal.coverage(layer, grid, inner_m=10, min_cells=4)
    assert np.allclose(a.weights.toarray(), b.weights.toarray(), atol=1e-6) and list(b.edge) == [False, True]   # float32 on disk
    assert any(p.suffix == ".npz" for p in (settings.data_dir / "cache" / "coverage").iterdir())


def test_sums_skip_missing_values_and_add_over_parts():
    """Cells without a value are left out of the mean, and the valid share drops; the sums of two
    grids (a site in two parts) add up to those of one grid."""
    grid = _grid(nx=2, ny=1, y0=10.0)
    field = np.array([shapely.box(0, 0, 20, 10)], dtype=object)
    cov = zonal.Coverage(zonal.coverage_matrix(field, grid), np.zeros(1, bool))
    values = np.array([[0.2, np.nan], [0.2, 0.4]])            # two bands (dates or indices)
    mean, std, valid = zonal.finish(*cov.sums(values), cov.cells)
    assert np.allclose(mean[0], [0.2, 0.3]) and np.allclose(valid[0], [0.5, 1.0]) and np.allclose(std[0], [0, 0.1])
    left, right = _grid(nx=1, ny=1, y0=10.0), _grid(nx=1, ny=1, x0=10.0, y0=10.0)
    parts = [zonal.Coverage(zonal.coverage_matrix(field, g), np.zeros(1, bool)) for g in (left, right)]
    s = [p.sums(v[:, None]) for p, v in zip(parts, (values[:, :1], values[:, 1:]))]
    total = parts[0].cells + parts[1].cells
    mean2, _, valid2 = zonal.finish(*(a + b for a, b in zip(*s)), total)
    assert np.allclose(mean2, mean, equal_nan=True) and np.allclose(valid2, valid)


def test_field_store_merge_done_and_prune():
    layer = zonal.Layer("rpg2025", ("a", "b"), (b"1", b"2"))
    t1 = [np.datetime64("2025-05-01"), np.datetime64("2025-05-11")]
    m = np.array([[0.5, 0.6], [0.7, np.nan]])
    data = {"ndvi": (m, np.zeros_like(m), np.array([[1.0, 0.9], [0.8, 0.0]]))}
    fields.merge("s", "sentinel2", layer, np.array([10.0, 20.0]), np.array([False, True]), t1, data)
    ds = fields.open_store("s", "sentinel2", "rpg2025", load=True)
    assert fields.done(ds, "ndvi") == set(np.array(t1, dtype="datetime64[D]")) and fields.done(ds, "ndre") == set()
    # A new date and a new index: the earlier dates stay, not done for the new index
    fields.merge("s", "sentinel2", layer, np.array([10.0, 20.0]), np.array([False, True]), [np.datetime64("2025-05-21")],
                 {"ndvi": (np.array([[0.65], [0.75]]),) * 2 + (np.ones((2, 1)),),
                  "ndre": (np.array([[0.3], [0.4]]),) * 2 + (np.ones((2, 1)),)})
    ds = fields.open_store("s", "sentinel2", "rpg2025", load=True)
    assert ds.sizes["time"] == 3 and len(fields.done(ds, "ndre")) == 1 and len(fields.done(ds, "ndvi")) == 3
    s = fields.series("s", "sentinel2", "rpg2025", "a", "ndvi")
    assert np.allclose(s["mean"], [0.5, 0.6, 0.65]) and list(s["valid"]) == [1.0, 0.9, 1.0]
    dates, ids, mean, valid, cells = fields.table("s", "sentinel2", "rpg2025", "ndvi", ids=["b"])
    assert list(ids) == ["b"] and list(cells) == [20.0] and valid[1, 0] == 0.0
    # Another version of the layer: the store is rebuilt
    other = zonal.Layer("rpg2025", ("a",), (b"9",))
    fields.merge("s", "sentinel2", other, np.array([1.0]), np.array([False]), [np.datetime64("2025-06-01")],
                 {"ndvi": (np.ones((1, 1)),) * 3})
    assert fields.open_store("s", "sentinel2", "rpg2025", load=True).sizes == {"time": 1, "field": 1}
    assert fields.keep_dates("s", "sentinel2", "rpg2025", lambda d: d.month != 6) == 1
    assert fields.open_store("s", "sentinel2", "rpg2025") is None and fields.layers("s", "sentinel2") == []
