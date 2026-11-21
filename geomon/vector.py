"""Reading the vector files users bring (the farm's plots and land cover): GeoJSON, shapefile
(possibly zipped), GeoPackage, or anything else GDAL reads, in any projection and datum.

The file is converted by ``ogr2ogr`` to GeoJSON in WGS84 (EPSG:4326, longitude/latitude): PROJ applies
the datum shift (NTF, ED50...) with the best transformation available. A file without a declared
projection (a shapefile without .prj) needs ``crs``; a GeoJSON without one is WGS84 by definition.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


class VectorError(ValueError):
    pass


def _source(path: Path) -> str:
    s = str(path)
    if s.lower().endswith(".zip"):
        return f"/vsizip/{s}"
    return s


def layers(path: Path) -> list[tuple[str, str]]:
    """(name, geometry type) of the layers of a file."""
    out = subprocess.run(["ogrinfo", "-ro", "-q", _source(path)], capture_output=True, text=True)
    if out.returncode:
        raise VectorError(f"{path.name}: not readable ({out.stderr.strip()[:300]})")
    found = []
    for line in out.stdout.splitlines():
        m = re.match(r"\s*\d+:\s+(.+?)(?:\s+\((.+)\))?\s*$", line)
        if m:
            found.append((m.group(1), m.group(2) or ""))
    return found


def has_crs(path: Path, layer: str) -> bool:
    out = subprocess.run(["ogrinfo", "-ro", "-so", _source(path), layer], capture_output=True, text=True)
    text = out.stdout
    if path.suffix.lower() in (".geojson", ".json"):
        return True                                   # WGS84 when not stated (RFC 7946)
    return "Layer SRS WKT:\n(unknown)" not in text and ("PROJCRS" in text or "GEOGCRS" in text or "PROJCS" in text
                                                         or "GEOGCS" in text)


def read_features(path: Path, layer: str | None = None, crs: str | None = None) -> list[dict]:
    """The features of a polygon layer as GeoJSON dicts in WGS84. ``layer``: the layer to read (default:
    the first polygon layer); ``crs``: the projection when the file does not state one (e.g.
    "EPSG:2154"), or to override it."""
    if not path.exists():
        raise VectorError(f"{path} not found")
    names = layers(path)
    if not names:
        raise VectorError(f"{path.name}: no layer")
    if layer is None:
        polys = [n for n, t in names if "polygon" in t.lower()]
        layer = (polys or [n for n, _ in names])[0]
    elif layer not in [n for n, _ in names]:
        raise VectorError(f"{path.name}: no layer {layer!r} ({', '.join(n for n, _ in names)})")
    if crs is None and not has_crs(path, layer):
        raise VectorError(f"{path.name}: no projection stated (a shapefile without .prj?): give its crs, "
                          "e.g. \"crs\": \"EPSG:2154\" for Lambert-93")
    cmd = ["ogr2ogr", "-f", "GeoJSON", "/vsistdout/", _source(path), layer, "-t_srs", "EPSG:4326",
           "-lco", "RFC7946=YES", "-dim", "XY"]
    if crs:
        cmd += ["-s_srs", crs]
    out = subprocess.run(cmd, capture_output=True)
    if out.returncode:
        raise VectorError(f"{path.name}: {out.stderr.decode(errors='replace').strip()[:300]}")
    feats = [f for f in json.loads(out.stdout).get("features", []) if f.get("geometry")]
    if not feats:
        raise VectorError(f"{path.name}: no feature with a geometry in layer {layer}")
    return feats


def field(props: dict, *names: str):
    """The first non-empty value among attribute names, case-insensitive."""
    lower = {k.lower(): v for k, v in props.items()}
    for n in names:
        v = lower.get(n.lower())
        if v not in (None, ""):
            return v
    return None
