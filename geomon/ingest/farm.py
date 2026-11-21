"""The farm: its plots as an analysis area, and its land cover year by year.

Two settings of the site in sites.json (or GEOMON_FARM_FILE for a single site):

- ``farm_file``: the plots, e.g. a PAC declaration export (the attributes CAMPAGNE, PACAGE, NUMERO,
  ILOT_REF, COMMUNE, SURF_TOT are kept when present). Their union is the area "My plots"
  (meta.aoi, kind ``farm``): everything computed per area (land summary, weather, Sentinel-2 series,
  crop groups) is then available for the farm. Without it, the plots of the latest land-cover year
  make the area.
- ``farm_landcover``: one file per year of the farm's land cover, its plots or fields with their crop:
  ``{"2025": "farm/x/landcover_2025.gpkg", "2026": "farm/x/landcover_2025.gpkg"}`` (a file may serve
  several years: the same crops). The crop is read from an RPG crop code (CODE_CULTU, CODE_CULTURE,
  CULTURE...; or ``crop_field``) and/or a crop group (CODE_GROUP...; or ``group_field``; else found from
  the crop code). Stored in ref.farm_landcover; for those years the RPG parcels lying in the plots
  take the farm's crop (eo.farm_crop, see geomon.parcels).

Every file may be a GeoJSON, a shapefile (or a zipped one), a GeoPackage... in any projection (see
geomon.vector). When the land cover changed, the crop-group curves (Sentinel-2, MODIS) are recomputed.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from ..aoi import AOI
from ..config import LandCoverFile, settings
from ..db import connect
from ..vector import field, read_features

log = logging.getLogger(__name__)

FARM_CODE = "farm"
CROP_FIELDS = ("CODE_CULTU", "CODE_CULTURE", "CODE_CULT", "CULTURE", "CULT", "CROP", "CROP_CODE")
GROUP_FIELDS = ("CODE_GROUP", "CODE_GROUPE", "CODE_GROUPE_CULTURE", "GROUPE", "CROP_GROUP")


def _path(p: str) -> Path:
    q = Path(p)
    return q if q.is_absolute() else settings.data_dir / q


def farm_path() -> Path | None:
    return _path(settings.farm_file) if settings.farm_file else None


def _geom_sql(param: str) -> str:
    return f"ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON({param}), 4326)), 3))"


def _load_plots(conn, site: str, path: Path) -> None:
    feats = read_features(path)
    conn.execute("DELETE FROM ref.farm_plot WHERE site = %s", (site,))
    with conn.cursor() as cur:
        cur.executemany(
            f"""INSERT INTO ref.farm_plot (site, campaign, number, island, commune, surf_ha, props, geom)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, {_geom_sql("%s")})""",
            [(site, field(p, "CAMPAGNE"), str(field(p, "NUMERO", "NUM_ILOT") or i + 1), field(p, "ILOT_REF"),
              field(p, "COMMUNE"), field(p, "SURF_TOT", "SURFACE"), json.dumps(p, default=str),
              json.dumps(f["geometry"]))
             for i, f in enumerate(feats) for p in [f.get("properties") or {}]])


def _crop_codes(conn) -> tuple[dict, dict]:
    """crop code -> (group, label) (latest year that has it), group code -> group label."""
    crops, groups = {}, {}
    for r in conn.execute("SELECT code_culture, code_group, label, label_group FROM ref.rpg_crop_code ORDER BY year").fetchall():
        crops[r["code_culture"]] = (r["code_group"], r["label"])
        if r["code_group"] and r["label_group"]:
            groups[r["code_group"]] = r["label_group"]
    return crops, groups


def _load_landcover(conn, site: str, files: tuple[LandCoverFile, ...]) -> dict:
    crops, groups = _crop_codes(conn)
    rows, unknown, per_year = [], set(), {}
    cache: dict[tuple, list] = {}
    for lc in files:
        key = (lc.path, lc.layer, lc.crs)
        if key not in cache:
            cache[key] = read_features(_path(lc.path), lc.layer, lc.crs)
        n = 0
        for i, f in enumerate(cache[key]):
            p = f.get("properties") or {}
            code = field(p, *((lc.crop_field,) if lc.crop_field else CROP_FIELDS))
            code = str(code).strip().upper() if code is not None else None
            group = field(p, *((lc.group_field,) if lc.group_field else GROUP_FIELDS))
            group = str(int(group)) if group is not None and str(group).strip().isdigit() else (group or None)
            if code and code not in crops:
                unknown.add(code)
            if group is None and code in crops:
                group = crops[code][0]
            label = crops[code][1] if code in crops else (groups.get(group) if group else None)
            rows.append((site, lc.year, str(field(p, "PLOT", "NUMERO", "NUM_PARCEL", "ID") or i + 1), code, group,
                         label, field(p, "SURF_HA", "SURF_PARC", "SURFACE"), lc.path,
                         json.dumps(p, default=str), json.dumps(f["geometry"])))
            n += 1
        per_year[lc.year] = n
    conn.execute("DELETE FROM ref.farm_landcover WHERE site = %s", (site,))
    with conn.cursor() as cur:
        cur.executemany(
            f"""INSERT INTO ref.farm_landcover (site, year, plot, code_cultu, code_group, crop_label, surf_ha, source, props, geom)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, {_geom_sql("%s")})""", rows)
    conn.execute("""UPDATE ref.farm_landcover SET surf_ha = COALESCE(surf_ha, ST_Area(geom::geography) / 1e4),
                           field_id = 'F' || left(md5(ST_AsBinary(geom)), 12)
                    WHERE site = %s""", (site,))
    if unknown:
        log.warning("Farm land cover: crop codes not in the RPG nomenclature: %s", ", ".join(sorted(unknown)))
    return {"years": per_year, "unknown_codes": sorted(unknown)}


def _fingerprint(conn, site: str) -> str:
    rows = conn.execute(
        """SELECT year, code_group, code_cultu, md5(ST_AsBinary(geom)) AS g FROM ref.farm_landcover
           WHERE site = %s ORDER BY year, g""", (site,)).fetchall()
    return hashlib.md5(json.dumps([list(r.values()) for r in rows]).encode()).hexdigest()


def run(aoi: AOI) -> dict:
    path, files = farm_path(), settings.farm_landcover
    if path is None and not files:
        with connect() as conn:            # no farm (any more) for this site
            conn.execute("DELETE FROM meta.aoi WHERE site = %s AND kind = 'farm'", (aoi.code,))
            conn.execute("DELETE FROM ref.farm_landcover WHERE site = %s", (aoi.code,))
        return {"skipped": "no farm file for this site"}
    with connect() as conn:
        before = _fingerprint(conn, aoi.code)
        landcover = _load_landcover(conn, aoi.code, files) if files else {}
        if not files:
            conn.execute("DELETE FROM ref.farm_landcover WHERE site = %s", (aoi.code,))
        if path is not None:
            _load_plots(conn, aoi.code, path)
            source = "SELECT geom FROM ref.farm_plot WHERE site = %(site)s"
        else:                              # the plots of the latest land-cover year
            source = """SELECT geom FROM ref.farm_landcover WHERE site = %(site)s
                        AND year = (SELECT max(year) FROM ref.farm_landcover WHERE site = %(site)s)"""
        r = conn.execute(
            f"""WITH f AS ({source})
                SELECT count(*) AS n, sum(ST_Area(f.geom::geography)) / 1e4 AS ha,
                       count(*) FILTER (WHERE NOT ST_Intersects(f.geom, a.geom)) AS outside
                FROM f, (SELECT geom FROM meta.aoi WHERE kind = 'site' AND code = %(site)s) a""",
            {"site": aoi.code}).fetchone()
        campaign = conn.execute("SELECT max(campaign) AS c FROM ref.farm_plot WHERE site = %s",
                                (aoi.code,)).fetchone()["c"] if path is not None else max(f.year for f in files)
        communes = [c["code"] for c in conn.execute(
            f"""SELECT DISTINCT a.code FROM meta.aoi a, ({source}) f
                WHERE a.site = %(site)s AND a.kind = 'commune' AND ST_Intersects(a.geom, f.geom) ORDER BY 1""",
            {"site": aoi.code}).fetchall()]
        name = settings.farm_name or (f"My plots ({campaign})" if campaign else "My plots")
        conn.execute("DELETE FROM meta.aoi WHERE site = %s AND kind = 'farm'", (aoi.code,))
        conn.execute(
            f"""INSERT INTO meta.aoi (code, name, kind, site, members, position, buffer_m, geom, bbox, updated_at)
                SELECT %(code)s, %(name)s, 'farm', %(site)s, %(members)s, 1000, %(buffer)s, ST_Multi(ST_Union(f.geom)),
                       (SELECT bbox FROM meta.aoi WHERE kind = 'site' AND code = %(site)s), now()
                FROM ({source}) f""",
            {"code": FARM_CODE, "name": name, "site": aoi.code, "members": communes, "buffer": settings.aoi_buffer_m})
        changed = _fingerprint(conn, aoi.code) != before
    if r["outside"]:
        log.warning("Farm: %d plot(s) outside the AOI communes: no data there", r["outside"])
    log.info("Farm: %d plots, %.1f ha; land cover %s", r["n"], r["ha"] or 0, landcover.get("years") or "none")
    out = {"plots": r["n"], "ha": round(r["ha"] or 0, 2), "landcover": landcover, "outside_aoi": r["outside"]}
    if changed:
        out["curves"] = recompute_curves(aoi.code)
    return out


def recompute_curves(site: str) -> dict:
    """After a change of the farm's land cover: the Sentinel-2 series of its fields (statistics on their
    outlines, from the stored images), the curves of the seasons and the crop groups; the MODIS ones."""
    from .. import modis, modis_crops, parcels

    out = {}
    try:
        with connect() as conn:            # the fields' series start again (outlines may have changed)
            parcels.clear_site(conn, site, parcels.FARM_LAYER)
            conn.execute("DELETE FROM eo.farm_crop WHERE site = %s", (site,))
        seasons = set(parcels.farm_years(site))
        parcels.clear_labels()
        try:
            parcels.store_missing_stats(site)
        finally:
            parcels.clear_labels()
        with connect() as conn:            # seasons that had land cover before but not now
            seasons |= {r["season"] for r in conn.execute(
                "SELECT DISTINCT season FROM eo.group_season WHERE site = %s AND area = 'farm'", (site,)).fetchall()}
        out["sentinel2_curves"] = parcels.compute_seasons(site, seasons)
        out["sentinel2_groups"] = parcels.compute_groups(site)
    except Exception as exc:
        log.error("Farm: Sentinel-2 field series not recomputed: %s", exc)
    if modis.years(site):
        try:
            out["modis"] = modis_crops.compute(site, rebuild_purity=True)
        except Exception as exc:
            log.error("Farm: MODIS crop groups not recomputed: %s", exc)
    return out
