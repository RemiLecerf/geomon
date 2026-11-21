import json
import shutil
import subprocess

import pytest

from geomon.vector import VectorError, field, read_features

pytestmark = pytest.mark.skipif(shutil.which("ogr2ogr") is None, reason="needs GDAL's ogr2ogr")

# A field near Dragey (WGS84)
RING = [[-1.48, 48.70], [-1.47, 48.70], [-1.47, 48.71], [-1.48, 48.71], [-1.48, 48.70]]


def _geojson(tmp_path):
    p = tmp_path / "fields.geojson"
    p.write_text(json.dumps({"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": {"CODE_CULTU": "LUZ", "plot": "A"},
        "geometry": {"type": "Polygon", "coordinates": [RING]}}]}))
    return p


def _convert(src, dst, *args):
    subprocess.run(["ogr2ogr", *args, str(dst), str(src)], check=True, capture_output=True)
    return dst


def _first_corner(feats):
    g = feats[0]["geometry"]
    ring = g["coordinates"][0] if g["type"] == "Polygon" else g["coordinates"][0][0]
    return sorted(ring)[0]


def test_any_format_and_projection_comes_back_in_wgs84(tmp_path):
    src = _geojson(tmp_path)
    files = [src,
             _convert(src, tmp_path / "ntf.shp", "-t_srs", "EPSG:27572"),       # NTF Lambert II étendu (another datum)
             _convert(src, tmp_path / "l93.gpkg", "-t_srs", "EPSG:2154", "-nln", "fields")]
    for f in files:
        feats = read_features(f)
        x, y = _first_corner(feats)
        assert x == pytest.approx(-1.48, abs=2e-5) and y == pytest.approx(48.70, abs=2e-5), f.name
        assert field(feats[0]["properties"], "code_cultu") == "LUZ"


def test_a_file_without_projection_needs_one(tmp_path):
    shp = _convert(_geojson(tmp_path), tmp_path / "l93.shp", "-t_srs", "EPSG:2154")
    (tmp_path / "l93.prj").unlink()
    with pytest.raises(VectorError, match="no projection"):
        read_features(shp)
    x, y = _first_corner(read_features(shp, crs="EPSG:2154"))
    assert x == pytest.approx(-1.48, abs=2e-5) and y == pytest.approx(48.70, abs=2e-5)


def test_layer_choice(tmp_path):
    gpkg = _convert(_geojson(tmp_path), tmp_path / "f.gpkg", "-nln", "fields")
    assert read_features(gpkg, layer="fields")
    with pytest.raises(VectorError, match="no layer"):
        read_features(gpkg, layer="other")
