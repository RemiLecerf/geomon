"""Météo-France sources: station files, COMEPHORE archives (synthetic) and the WCS client helpers."""

import io
import tarfile
from types import SimpleNamespace

import numpy as np

from geomon import meteofrance as mf
from geomon.ingest import comephore, mf_stations


class _Response:
    def __init__(self, payload=None, raw=None):
        self._payload, self.raw, self.status_code = payload, raw, 200

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def test_station_files_of_a_departement(monkeypatch):
    base = "https://object.files.data.gouv.fr/meteofrance/data/synchro_ftp/BASE/QUOT/"
    names = ["Q_50_previous-1950-2024_RR-T-Vent.csv.gz", "Q_50_latest-2025-2026_RR-T-Vent.csv.gz",
             "Q_50_previous-1950-2024_autres-parametres.csv.gz", "Q_50_latest-2025-2026_autres-parametres.csv.gz",
             "Q_500_latest-2025-2026_RR-T-Vent.csv.gz", "Q_35_latest-2025-2026_RR-T-Vent.csv.gz", "Q_descriptif_champs_RR-T-Vent.csv"]
    files = mf_stations._files("50", [{"url": base + n} for n in names])
    assert files == {"history": base + names[0], "latest": base + names[1],
                     "history_other": base + names[2], "latest_other": base + names[3]}


def _tar_of_hours(hours: dict[str, np.ndarray]) -> bytes:
    """A COMEPHORE-like month archive: one RR GeoTIFF per hour (and an ERR file to skip)."""
    import rasterio
    from rasterio.transform import from_origin

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for stamp, a in hours.items():
            for kind, data in (("RR", a), ("ERR", np.zeros_like(a))):
                with rasterio.MemoryFile() as m:
                    with m.open(driver="GTiff", width=a.shape[1], height=a.shape[0], count=1, dtype="uint16",
                                crs="EPSG:2154", transform=from_origin(0, 6_400_000, 1000, 1000), nodata=65535) as dst:
                        dst.write(data.astype("uint16"), 1)
                    content = m.read()
                info = tarfile.TarInfo(f"{stamp[:4]}/{stamp}_{kind}.gtif")
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def test_comephore_month_sums_utc_days(monkeypatch):
    # Day 1: 24 hours of 0.5 mm (the hour ending 00 UTC on day 2 belongs to day 1); day 2: only 3 hours
    hours = {}
    for h in range(1, 25):
        end = f"2026060{1 + h // 24}{h % 24:02d}"
        hours[end] = np.full((3, 3), 5)
    hours["2026060201"] = np.full((3, 3), 10)
    hours["2026060202"] = np.full((3, 3), 10)
    hours["2026060203"] = np.array([[65535, 10, 10], [10, 10, 10], [10, 10, 10]])
    data = _tar_of_hours(hours)
    monkeypatch.setattr(comephore, "session",
                        lambda: SimpleNamespace(get=lambda *a, **k: _Response(raw=SimpleNamespace(read=io.BytesIO(data).read))))

    win = comephore._Window(rows=(0, 2), cols=(1, 3))     # 4 cells, as recorded in a store
    other = comephore._Window(rows=(2, 3), cols=(0, 3))   # another site's 3 cells, from the same read
    (days, vals), (_, vals2) = comephore._month("https://example/H_COMEPHORE_202606.tar", [win, other])
    assert vals2.shape == (2, 3) and np.allclose(vals2[0], 12.0)
    assert [str(d)[:10] for d in days] == ["2026-06-01", "2026-06-02"]
    assert vals.shape == (2, 4)
    assert np.allclose(vals[0], 12.0)                    # 24 x 0.5 mm
    assert np.isnan(vals[1]).all()                       # 3 hours only: not a whole day


def test_wcs_run_time_and_latest(monkeypatch):
    ids = ("TOTAL_PRECIPITATION__GROUND_OR_WATER_SURFACE___2026-11-20T17.00.00Z_PT15M",
           "TOTAL_PRECIPITATION__GROUND_OR_WATER_SURFACE___2026-11-20T18.00.00Z_PT15M",
           "TOTAL_PRECIPITATION__GROUND_OR_WATER_SURFACE___2026-11-20T18.00.00Z_PT1H",
           "TEMPERATURE__SPECIFIC_HEIGHT_LEVEL_ABOVE_GROUND___2026-11-20T18.00.00Z")
    w = mf.WCS("/public/aromepi/1.0/wcs/MF-NWP-HIGHRES-AROMEPI-001-FRANCE-WCS")
    monkeypatch.setattr(w, "coverages", lambda: list(ids))
    assert w.latest("TOTAL_PRECIPITATION__GROUND_OR_WATER_SURFACE", "_PT15M") == ids[1]
    assert w.latest("TEMPERATURE__SPECIFIC_HEIGHT_LEVEL_ABOVE_GROUND") == ids[3]
    assert w.latest("WIND_SPEED_GUST__SPECIFIC_HEIGHT_LEVEL_ABOVE_GROUND") is None
    assert w.run_time(ids[1]) == np.datetime64("2026-11-20T18:00:00")


def test_nowcast_box_and_latest_complete_run():
    from rasterio.transform import from_origin

    from geomon import nowcast as nc

    x0, y0, x1, y1 = mf.bbox((5.0, 45.4, 5.1, 45.5), margin_km=11.057)
    assert (y0, y1) == (45.3, 45.6) and x0 < 4.86 and x1 > 5.24
    assert all(abs(v * 100 - round(v * 100)) < 1e-9 for v in (x0, y0, x1, y1))
    x, y = mf.centres({"transform": from_origin(4.0, 46.0, 0.01, 0.01), "width": 3, "height": 2})
    assert list(x) == [4.005, 4.015, 4.025] and list(y) == [45.995, 45.985]

    full, partial = (f"{nc.RAIN}___2026-11-20T18.00.00Z_PT15M", f"{nc.RAIN}___2026-11-20T19.00.00Z_PT15M")

    w = mf.WCS(nc.WCS_PATH)
    w.coverages = lambda: [full, partial, f"{nc.RAIN}___2026-11-20T19.00.00Z_PT1H"]
    described = []
    w.offsets = lambda c: described.append(c) or list(range(900, (25 if c == full else 15) * 900, 900))
    cov, offsets = w.latest_complete(nc.RAIN, "_PT15M", n=nc.STEPS)
    assert cov == full and len(offsets) == 24 and offsets[-1] == 21600
    # The stored run reached first: nothing newer, and it is not described again
    described.clear()
    assert w.latest_complete(nc.RAIN, "_PT15M", n=nc.STEPS, stop_at="2026-11-20T18.00.00Z") == (None, [])
    assert described == [partial]


def test_vigilance_for_a_departement_and_its_coast(monkeypatch, tmp_path):
    import json

    from geomon import alerts

    monkeypatch.setattr(alerts, "_dir", lambda: tmp_path)
    period = lambda ech, items: {"echeance": ech, "begin_validity_time": "b", "end_validity_time": "e",
                                 "text_items": {"title": "Commentaire carte", "text": ["Crues sur la Lys."]},
                                 "timelaps": {"domain_ids": items}}
    dom = lambda d, ph: {"domain_id": d, "max_color_id": max(c for _, c in ph),
                         "phenomenon_items": [{"phenomenon_id": p, "phenomenon_max_color_id": c,
                                               "timelaps_items": [{"begin_time": "b", "end_time": "e", "color_id": c}]} for p, c in ph]}
    (tmp_path / "vigilance.json").write_text(json.dumps({"product": {"update_time": "2026-11-20T17:00:30Z", "global_max_color_id": 3,
        "periods": [period("J", [dom("50", [("2", 2), ("1", 1)]), dom("5010", [("9", 1)]), dom("38", [("8", 3)])]),
                    period("J1", [dom("50", [("1", 1)]), dom("5010", [("9", 3)])])]}}), encoding="utf-8")
    v = alerts.vigilance_for(["50"])
    d = v["departements"][0]
    assert d["code"] == "50" and d["coastal"]
    today, tomorrow = d["periods"]
    assert today["max_color"]["name"] == "yellow" and today["phenomena"][0]["name"] == "Rain and flooding"
    assert tomorrow["max_color"]["name"] == "orange" and tomorrow["phenomena"][0]["name"] == "Waves and submersion"
    assert v["comment"] == ["Crues sur la Lys."]


def test_fire_levels_found_whatever_the_layout():
    from geomon import alerts

    data = {"departements": [{"id_departement": "13", "echeance": "J1", "niveau": 3},
                             {"id_departement": "13", "echeance": "J2", "niveau": 2}]}
    assert alerts.fire_levels(data) == [{"departement": "13", "day": "J1", "level": 3},
                                        {"departement": "13", "day": "J2", "level": 2}]
    assert alerts.fire_levels({"detail": "no matching blob"}) == []


def test_observations_frame_stations_and_merge():
    from geomon.ingest import mf_obs

    recs = [{"geo_id_insee": s, "lat": lat, "lon": lon, "validity_time": f"2026-11-20T{h:02d}:00:00Z", "t": 283.15 + h,
             "rr1": 0.2, "raf": 5.0, "pmer": 101300.0}
            for s, lat, lon in (("A", 48.70, -1.45), ("B", 48.75, -1.40), ("FAR", 49.60, -1.30)) for h in (18, 19)]
    df = mf_obs.to_frame(recs)
    assert abs(df["t"].iloc[0] - 28.0) < 1e-6 and abs(df["pmsl"].iloc[0] - 1013.0) < 1e-6
    st = mf_obs.nearest(df, -1.45, 48.70)
    assert list(st.station) == ["A", "B"] and st.km.iloc[0] == 0
    import xarray as xr

    t = np.array(["2026-11-20T18:00", "2026-11-20T19:00"], dtype="datetime64[ns]")
    mk = lambda times, v: xr.Dataset({"t": (("time", "point"), np.full((len(times), 1), v, "float32"))},
                                      coords={"time": times, "station": ("point", ["A"]), "distance_km": ("point", [v])})
    old = mk(t, 1.0)
    new = mk(np.array(["2026-11-20T19:00", "2026-11-20T20:00"], dtype="datetime64[ns]"), 2.0)
    ds = mf_obs.merge(old, new, keep_hours=2)
    assert [str(x)[11:16] for x in ds["time"].values] == ["19:00", "20:00"] and float(ds["t"][0, 0]) == 2.0
    assert ds["distance_km"].dims == ("point",) and float(ds["distance_km"][0]) == 2.0     # the new station coordinates


def test_ensemble_probabilities_dawn_and_sea_cells():
    from geomon import mf_models as m
    from geomon.ingest import mf_models as ing

    p = m.probabilities([0.0, 2.0, 6.0, 12.0, np.nan], (1, 5, 10))
    assert p["members"] == 4 and p["probability"] == {"1": 0.75, "5": 0.5, "10": 0.25}
    f = m.probabilities([-3.0, -1.0, 1.0, 2.0], (0, -2), below=True)
    assert f["probability"] == {"0": 0.5, "-2": 0.25}
    # Run 15 UTC on 20 November (UTC+1): mornings of the 21st and 22nd, hours ending 04-08 local
    mornings = ing.dawn_mornings(np.datetime64("2026-11-20T15:00"), list(range(3600, 51 * 3600 + 1, 3600)))
    assert [d for d, _ in mornings] == ["2026-11-21", "2026-11-22"]
    assert mornings[0][1] == [h * 3600 for h in (12, 13, 14, 15, 16)]
    v = np.array([[np.nan, 1.0], [np.nan, np.nan]])
    cells = m.nearest_sea_cells(v, np.array([-1.5, -1.6]), np.array([48.7, 48.6]), -1.45, 48.7)
    assert cells and cells[0][:2] == (0, 1)
    assert m.nearest_sea_cells(v, np.array([-1.5, -1.6]), np.array([48.7, 48.6]), 5.0, 45.0) == []


def test_pearome_site_cut_from_the_shared_read():
    from rasterio.transform import from_origin

    from geomon.ingest import mf_models as ing

    a = np.arange(20, dtype="float32").reshape(4, 5)                 # 0.025° cells from (5.0, 46.0)
    prof = {"transform": from_origin(5.0, 46.0, 0.025, 0.025), "width": 5, "height": 4}
    cut = ing._cut(a, prof, (5.03, 45.93, 5.07, 45.97))            # centres 5.0375, 5.0625 / 45.9625, 45.9375
    assert cut.tolist() == [[6.0, 7.0], [11.0, 12.0]]


def test_comephore_months_written_in_place(monkeypatch):
    from geomon.config import settings
    from geomon.ingest import comephore as c
    from geomon.weather import clear_cache, open_product

    months = {"202605": "u5", "202606": "u6"}
    monkeypatch.setattr(c, "months_available", lambda: dict(months))
    monkeypatch.setattr(c, "settings", SimpleNamespace(comephore_start=__import__("datetime").date(2026, 5, 1),
                                                       comephore_budget_min=20, site_dir=settings.site_dir))

    def fake_month(url, windows):
        win = windows[0]
        win.x, win.y, win.lon, win.lat = np.array([1.0, 2.0]), np.array([1.0, 1.0]), np.array([5.0, 5.01]), np.array([45.0, 45.0])
        win.crs, win.cell_m, win.rows, win.cols = "EPSG:2154", 1000.0, slice(0, 1), slice(0, 2)
        ym = [k for k, v in months.items() if v == url][0]
        days = c.day_axis(ym, ym)
        days = np.concatenate([days[:1] - np.timedelta64(1, "D"), days])        # the hour before the month
        return [(days, np.full((len(days), 2), float(ym[-1]), dtype="float32"))]

    monkeypatch.setattr(c, "_month", fake_month)
    monkeypatch.setattr(c, "_other_sites", lambda site: [])
    aoi = SimpleNamespace(code=settings.site_code, bbox_wgs84=(5.0, 45.0, 5.1, 45.1))
    r = c.run(aoi)
    assert r["added"] == 2 and r["remaining"] == 0
    months["202607"] = "u7"                       # a new month published: the axis grows
    assert c.run(aoi)["added"] == 1
    clear_cache()
    ds = open_product("comephore")
    assert str(ds["time"].values[0])[:10] == "2026-05-01" and str(ds["time"].values[-1])[:10] == "2026-07-31"
    v = ds["precip"].sel(time=["2026-05-31", "2026-06-01", "2026-06-30", "2026-07-01"]).values[:, 0]
    assert v.tolist() == [5.0, 6.0, 6.0, 7.0]     # each month in its place, not overwritten by the next one's first hour
    assert ds.attrs["months"] == "202605 202606 202607" and ds.attrs["rows"] == [0, 1]


def test_pearome_groups_only_nearby_sites():
    from geomon.ingest import mf_models as ing

    boxes = {"a": (5.0, 45.0, 5.2, 45.2), "b": (5.15, 45.05, 5.35, 45.25), "far": (-1.6, 48.6, -1.4, 48.8)}
    assert ing.group_box(boxes, "a") == (5.0, 45.0, 5.35, 45.25)          # neighbours share one box
    assert ing.group_box(boxes, "far") == boxes["far"]                    # far away: its own box
