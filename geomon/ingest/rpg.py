"""RPG parcels (Registre parcellaire graphique) from the IGN Géoplateforme WFS.

Every year from GEOMON_RPG_FIRST_YEAR (2015, the first year of the current
nomenclature and of Sentinel-2) to GEOMON_RPG_YEAR is loaded: the latest from
GEOMON_RPG_LAYER (RPG.LATEST), earlier ones from the layers RPG.<year>. The
crop history of a parcel and the crop-group NDVI references need them all.

Parcel identifiers (id_parcel) are only unique within one year: parcels are
renumbered and their outlines change from year to year.

Before 2015 (RPG 1.0, from GEOMON_RPG_ILOT_FIRST_YEAR, 2007) only the farms' îlots were
published: blocks of contiguous parcels of one farm, each with its majority crop group (and, from
2010, that group's area in the îlot), no crop per parcel. They go to ref.rpg_ilot. 2010-2014 come
from the WFS layers RPG.<year>:rpg_<year>, for the site's extent only; 2007-2009 are only published
as regional archives (7z, 100 to 300 MB per region and year, kept in data/raw/rpg for the other
sites of the region): the shapefile is extracted from it and cut to the site's extent.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path

from ..aoi import AOI
from ..config import settings
from ..db import connect
from ..http import cached_download, session, wfs_features, wfs_layer_title

log = logging.getLogger(__name__)


def _check_year(layer: str, year: int) -> None:
    """Make sure e.g. RPG.LATEST really holds the configured year."""
    title = wfs_layer_title(f"{layer}:parcelles_graphiques") or ""
    years = re.findall(r"\b(20\d\d)\b", title)
    if years and int(years[0]) != year:
        raise RuntimeError(
            f"{layer} holds RPG {years[0]} ('{title}'), not {year}. "
            "Set GEOMON_RPG_LAYER / GEOMON_RPG_YEAR accordingly."
        )
    log.info("RPG layer %s: %s", layer, title or "(title not found)")


def _layer(year: int) -> str:
    return settings.rpg_layer if year == settings.rpg_year else f"RPG.{year}"


def _codes(year: int) -> tuple[list[dict], int | None]:
    """Crop code list of a year. Layers before 2022 have none: the nearest later list is used
    (the nomenclature has been stable since 2015)."""
    for y in range(year, settings.rpg_year + 1):
        layer = _layer(y)
        if wfs_layer_title(f"{layer}:codes_cultures") is not None:
            return wfs_features(f"{layer}:codes_cultures"), y
    return [], None


def _code_row(year: int, p: dict) -> tuple:
    """The field names differ between years (code / code_culture, libelle_groupe / ...)."""
    def get(*names):
        return next((p[n] for n in names if p.get(n) not in (None, "")), None)
    return (year, get("code_culture", "code"), get("libelle_culture", "libelle"),
            get("code_groupe_culture", "code_groupe"), get("libelle_groupe_culture", "libelle_groupe"))


def _fill_groups(conn) -> None:
    """Some code lists (2024) have no crop group: take it from the nearest year that has it."""
    conn.execute(
        """UPDATE ref.rpg_crop_code c SET (code_group, label_group) = (
               SELECT o.code_group, o.label_group FROM ref.rpg_crop_code o
               WHERE o.code_culture = c.code_culture AND o.code_group IS NOT NULL
               ORDER BY abs(o.year - c.year), o.year DESC LIMIT 1)
           WHERE c.code_group IS NULL""")


def _load_year(aoi: AOI, year: int) -> dict:
    layer = _layer(year)
    if year == settings.rpg_year:
        _check_year(layer, year)
    elif wfs_layer_title(f"{layer}:parcelles_graphiques") is None:
        log.warning("RPG %d: no layer %s on the WFS, skipped", year, layer)
        return {"year": year, "parcels": 0, "skipped": True}
    codes, codes_year = _codes(year)
    parcels = wfs_features(f"{layer}:parcelles_graphiques", bbox_wgs84=aoi.bbox_wgs84)

    with connect() as conn:
        conn.execute("DELETE FROM ref.rpg_crop_code WHERE year = %s", (year,))
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO ref.rpg_crop_code (year, code_culture, label, code_group, label_group)
                   VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                [_code_row(year, f["properties"]) for f in codes],
            )

        # Parcels of this site's extent only: the other sites' parcels stay
        conn.execute(
            """DELETE FROM ref.rpg_parcel WHERE year = %s
               AND geom && (SELECT bbox FROM meta.aoi WHERE kind = 'site' AND code = %s)""",
            (year, aoi.code))
        with conn.cursor() as cur:
            have = {r["parcel_id"] for r in cur.execute(
                "SELECT parcel_id FROM ref.rpg_parcel WHERE year = %s", (year,)).fetchall()}
            parcels = [f for f in parcels
                       if str(f["properties"].get("num_parcel") or f["properties"].get("id_parcel") or f.get("id"))
                       not in have]       # already loaded with another site whose extent overlaps
            cur.executemany(
                """INSERT INTO ref.rpg_parcel
                       (year, parcel_id, code_cultu, code_group, cat_cult_p, surf_ha, bio, props, geom)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb,
                           ST_Multi(ST_CollectionExtract(
                               ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)), 3)))""",
                [
                    (
                        year,
                        str(p.get("num_parcel") or p.get("id_parcel") or f.get("id")),
                        p.get("code_cultu"),
                        p.get("code_group"),
                        p.get("cat_cult_p"),
                        p.get("surf_parc"),
                        p.get("bio"),
                        json.dumps(p),
                        json.dumps(f["geometry"]),
                    )
                    for f in parcels
                    if f.get("geometry")
                    for p in [f["properties"]]
                ],
            )
    note = "" if codes_year in (year, None) else f" (crop labels of {codes_year})"
    log.info("RPG %s: %d parcels, %d crop codes%s", year, len(parcels), len(codes), note)
    return {"year": year, "parcels": len(parcels), "crop_codes": len(codes), "crop_codes_year": codes_year}


# ---------------------------------------------------------------------------
# RPG 1.0 (2007-2014): îlots and their majority crop group
# ---------------------------------------------------------------------------
ILOT_LAST_YEAR = 2014
ILOT_WFS_FIRST_YEAR = 2010            # WFS layers RPG.2010:rpg_2010 ... RPG.2014:rpg_2014
DOWNLOADS = "https://data.geopf.fr/telechargement/resource/RPG"


def _group_code(v) -> str | None:
    """'01' -> '1': the groups of the parcels are written without leading zero."""
    v = str(v if v is not None else "").strip()
    return str(int(v)) if v.isdigit() else (v or None)


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ilots_wfs(aoi: AOI, year: int) -> list[dict]:
    feats = wfs_features(f"RPG.{year}:rpg_{year}", bbox_wgs84=aoi.bbox_wgs84)
    return [{"ilot_id": str(p.get("num_ilot")), "code_group": _group_code(p.get("code_cultu")),
             "group_label": p.get("nom_cultu"), "surf_ha": _num(p.get("surf_graph")),
             "group_ha": _num(p.get("surf_cultu")), "commune": p.get("commune"), "geometry": f["geometry"]}
            for f in feats if f.get("geometry") for p in [f["properties"]]]


def package_name(names: list[str], region: str, year: int) -> str | None:
    """The RPG 1.0 archive of a region (2016 region code) and year among the download service's
    resources, e.g. RPG_1-0__SHP_LAMB93_R28_2008-01-01 or RPG_1-0__SHP_LAMB93_R28-2011_2011-01-01."""
    pat = re.compile(rf"^RPG_1-0_+SHP_LAMB93_R{region}(?:-{year})?_{year}-\d\d-\d\d$")
    return next((n for n in sorted(names) if pat.match(n)), None)


def _resources() -> list[str]:
    """Names of the RPG 1.0 resources of the download service (Atom feed, paged)."""
    names, page = [], 1
    while True:
        r = session().get(DOWNLOADS, params={"limit": 50, "page": page}, timeout=120)
        r.raise_for_status()
        names += re.findall(r"<title>(RPG_1-0[^<]+)</title>", r.text)
        count = re.search(r'gpf_dl:pagecount="(\d+)"', r.text)
        if not count or page >= int(count.group(1)):
            return names
        page += 1


def _archive(name: str) -> Path:
    """The archive of a resource, downloaded into data/raw/rpg (removed once its îlots are loaded)."""
    dest = settings.raw_dir / "rpg" / f"{name}.7z"
    if dest.exists():
        return dest
    r = session().get(f"{DOWNLOADS}/{name}", timeout=120)
    r.raise_for_status()
    url = next(iter(re.findall(r'href="([^"]+\.7z)"', r.text)), None)
    if url is None:
        raise RuntimeError(f"No archive in the RPG resource {name}")
    return cached_download(url, dest, float("inf"))[0]


def _ilots_archive(aoi: AOI, archive: Path) -> list[dict]:
    """The îlots of the site's extent from a regional archive. The 7z archives are solid: the
    shapefile is copied out first (seconds); reading it inside the archive takes minutes."""
    root = f"/vsi7z/{archive}"
    listing = subprocess.run(["gdal", "vsi", "list", "-R", root], check=True, capture_output=True, text=True).stdout
    shp = next((ln.strip() for ln in listing.splitlines() if ln.strip().upper().endswith("ILOTS_ANONYMES.SHP")), None)
    if shp is None:
        raise RuntimeError(f"No ILOTS_ANONYMES shapefile in {archive.name}")
    with tempfile.TemporaryDirectory() as tmp:
        for ext in (".shp", ".shx", ".dbf", ".prj"):
            subprocess.run(["gdal", "vsi", "copy", f"{root}/{shp[:-4]}{ext}", f"{tmp}/ilots{ext}"],
                           check=True, capture_output=True)
        x0, y0, x1, y1 = aoi.bbox_wgs84
        out = subprocess.run(
            ["ogr2ogr", "-f", "GeoJSON", "/vsistdout/", f"{tmp}/ilots.shp", "-t_srs", "EPSG:4326",
             "-spat", str(x0), str(y0), str(x1), str(y1), "-spat_srs", "EPSG:4326",
             "--config", "SHAPE_ENCODING", "CP1252"], check=True, capture_output=True).stdout
    feats = json.loads(out).get("features", [])
    return [{"ilot_id": str(p.get("NUM_ILOT")), "code_group": _group_code(p.get("CODE_CULTU")),
             "group_label": p.get("NOM_CULTU"), "surf_ha": None, "group_ha": None, "commune": None,
             "geometry": f["geometry"]}
            for f in feats if f.get("geometry") for p in [f["properties"]]]


def _store_ilots(aoi: AOI, year: int, rows: list[dict]) -> int:
    with connect() as conn:
        conn.execute("""DELETE FROM ref.rpg_ilot WHERE year = %s
                        AND geom && (SELECT bbox FROM meta.aoi WHERE kind = 'site' AND code = %s)""", (year, aoi.code))
        have = {r["ilot_id"] for r in conn.execute("SELECT ilot_id FROM ref.rpg_ilot WHERE year = %s", (year,)).fetchall()}
        rows = [r for r in rows if r["ilot_id"] not in have]     # loaded with another site whose extent overlaps
        with conn.cursor() as cur:
            cur.executemany(
                """WITH g AS (SELECT ST_Multi(ST_CollectionExtract(ST_MakeValid(
                                         ST_SetSRID(ST_GeomFromGeoJSON(%(geometry)s), 4326)), 3)) AS geom)
                   INSERT INTO ref.rpg_ilot (year, ilot_id, code_group, group_label, surf_ha, group_ha, commune, geom)
                   SELECT %(year)s, %(ilot_id)s, %(code_group)s, %(group_label)s,
                          COALESCE(%(surf_ha)s, ST_Area(g.geom::geography) / 1e4), %(group_ha)s, %(commune)s, g.geom
                   FROM g""",
                [r | {"year": year, "geometry": json.dumps(r["geometry"])} for r in rows])
    return len(rows)


def _load_ilots(aoi: AOI, year: int, resources: list[str] | None) -> dict:
    if year >= ILOT_WFS_FIRST_YEAR and wfs_layer_title(f"RPG.{year}:rpg_{year}") is not None:
        rows, source = _ilots_wfs(aoi, year), "WFS"
    else:
        rows, names, archives = [], [], []
        for region in aoi.regions:
            name = package_name(resources or [], region, year)
            if name is None:
                log.warning("RPG %d: no archive for region %s", year, region)
                continue
            archives.append(_archive(name))
            rows += _ilots_archive(aoi, archives[-1])
            names.append(name)
        source = ", ".join(names) or "none"
    n = _store_ilots(aoi, year, rows)
    for a in archives:            # regional archives of 100-275 MB, loaded: downloaded again if ever needed
        a.unlink(missing_ok=True)
    log.info("RPG %d: %d îlots (%s)", year, n, source)
    return {"year": year, "ilots": n}


def ilot_years() -> list[int]:
    if not settings.rpg_ilot_first_year:
        return []
    return list(range(settings.rpg_ilot_first_year, min(ILOT_LAST_YEAR, settings.rpg_first_year - 1) + 1))


def run(aoi: AOI) -> dict:
    years_ilots = ilot_years()
    resources = _resources() if any(y < ILOT_WFS_FIRST_YEAR for y in years_ilots) else None
    out_ilots = [_load_ilots(aoi, y, resources) for y in years_ilots]
    years = list(range(min(settings.rpg_first_year, settings.rpg_year), settings.rpg_year + 1))
    out = [_load_year(aoi, y) for y in years]
    with connect() as conn:
        _fill_groups(conn)
    return {"years": [r["year"] for r in out if r.get("parcels")],
            "parcels": {r["year"]: r["parcels"] for r in out},
            "ilots": {r["year"]: r["ilots"] for r in out_ilots}}
