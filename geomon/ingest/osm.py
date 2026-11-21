"""OpenStreetMap features for the AOI.

Two sources, selected with GEOMON_OSM_SOURCE:

* ``overpass`` (default): one Overpass API query restricted to the AOI extent
  and to the tags classified below. The response (a few MB) is kept under
  data/raw/osm as a local archive (Specifications §1) and reused while younger
  than GEOMON_OSM_MAX_AGE_DAYS. Overpass asks clients to send a descriptive
  User-Agent and to avoid repeated or parallel queries: one query per run.
* ``geofabrik`` (for later, larger areas): a regional .osm.pbf extract
  (Rhône-Alpes is 533 MB), downloaded once and clipped to the AOI.

Either way ogr2ogr loads the data into staging tables, and SQL classifies the
features into ref.osm_feature.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
import subprocess
import time
from pathlib import Path

from ..aoi import AOI
from ..config import settings
from ..db import connect
from ..http import cached_download, is_fresh, session

log = logging.getLogger(__name__)


# Tags kept, matching _CLASSIFY_SQL. Relations are limited to multipolygons;
# "(._;>;)" adds the member ways and nodes needed to build the geometries.
_OVERPASS_QUERY = """[out:xml][timeout:{timeout}][bbox:{s},{w},{n},{e}];
(
  way[highway]; way[railway]; way[waterway];
  way[building]; rel[building][type=multipolygon];
  way[landuse]; rel[landuse][type=multipolygon];
  way[natural]; rel[natural][type=multipolygon];
  way[leisure]; rel[leisure][type=multipolygon];
  way[water];   rel[water][type=multipolygon];
  node[place]; node[amenity]; node[shop]; node[tourism]; node[historic];
);
(._;>;);
out body;
"""


def _overpass(aoi: AOI, dest: Path, max_age_days: int) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if is_fresh(dest, max_age_days):
        log.info("Overpass response %s is less than %d days old: reusing it", dest.name, max_age_days)
        return dest
    w, s_, e, n = aoi.bbox_wgs84
    query = _OVERPASS_QUERY.format(
        timeout=settings.overpass_timeout_s, s=f"{s_:.6f}", w=f"{w:.6f}", n=f"{n:.6f}", e=f"{e:.6f}"
    )
    errors = []
    for url in settings.overpass_urls:
        log.info("Querying Overpass %s", url)
        t0 = time.time()
        try:
            r = session().post(url, data={"data": query}, timeout=settings.overpass_timeout_s + 60)
            r.raise_for_status()
        except Exception as exc:  # try the next endpoint
            log.warning("Overpass %s failed: %s", url, exc)
            errors.append(f"{url}: {exc}")
            continue
        body = r.content
        # Overpass reports a query timeout or memory error as HTTP 200 with a <remark>
        remark = re.search(rb"<remark>(.*?)</remark>", body[-4096:], re.S)
        if remark and b"error" in remark.group(1).lower():
            msg = remark.group(1).decode(errors="replace").strip()
            log.warning("Overpass %s returned an error: %s", url, msg)
            errors.append(f"{url}: {msg}")
            continue
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(body)
        os.replace(tmp, dest)
        # Date of the OSM data, not of the download
        base = re.search(rb'osm_base="([^"]+)"', body[:2048])
        if base:
            ts = dt.datetime.fromisoformat(base.group(1).decode().replace("Z", "+00:00")).timestamp()
            os.utime(dest, (ts, ts))
        log.info("Overpass: %.1f MB in %.0f s", len(body) / 1e6, time.time() - t0)
        return dest
    raise RuntimeError("All Overpass endpoints failed: " + "; ".join(errors))


_CLASSIFY_SQL = """
WITH src AS (
    SELECT 'lines' AS src, (j->>'osm_id') AS osm_id,
           jsonb_strip_nulls(j) || COALESCE(hstore_to_jsonb(other_tags), '{}') AS tags, geom
      FROM (SELECT to_jsonb(l) - '{geom,other_tags,ogc_fid,z_order,osm_id}'::text[]
                   || jsonb_build_object('osm_id', 'w' || l.osm_id) AS j,
                   l.other_tags, l.geom FROM staging.lines l) s
    UNION ALL
    SELECT 'multipolygons', (j->>'osm_id'),
           jsonb_strip_nulls(j) || COALESCE(hstore_to_jsonb(other_tags), '{}'), geom
      FROM (SELECT to_jsonb(m) - '{geom,other_tags,ogc_fid,osm_id,osm_way_id}'::text[]
                   || jsonb_build_object('osm_id',
                        CASE WHEN m.osm_id IS NOT NULL THEN 'r' || m.osm_id ELSE 'w' || m.osm_way_id END) AS j,
                   m.other_tags, m.geom FROM staging.multipolygons m) s
    UNION ALL
    SELECT 'points', (j->>'osm_id'),
           jsonb_strip_nulls(j) || COALESCE(hstore_to_jsonb(other_tags), '{}'), geom
      FROM (SELECT to_jsonb(p) - '{geom,other_tags,ogc_fid,osm_id}'::text[]
                   || jsonb_build_object('osm_id', 'n' || p.osm_id) AS j,
                   p.other_tags, p.geom FROM staging.points p) s
),
classified AS (
    SELECT osm_id, tags - 'osm_id' AS tags, geom,
           CASE
             WHEN src = 'lines' AND tags ? 'highway'  THEN 'roads'
             WHEN src = 'lines' AND tags ? 'railway'  THEN 'railways'
             WHEN src = 'lines' AND tags ? 'waterway' THEN 'waterways'
             WHEN src = 'multipolygons' AND tags ? 'building' THEN 'buildings'
             WHEN src = 'multipolygons' AND (tags->>'natural' IN ('water', 'wetland')
                                             OR tags ? 'water'
                                             OR tags->>'landuse' IN ('reservoir', 'basin')) THEN 'water'
             WHEN src = 'multipolygons' AND (tags ? 'landuse' OR tags ? 'natural' OR tags ? 'leisure') THEN 'landuse'
             WHEN src = 'points' AND (tags ? 'place' OR tags ? 'amenity' OR tags ? 'shop'
                                      OR tags ? 'tourism' OR tags ? 'historic') THEN 'pois'
           END AS layer
      FROM src
)
INSERT INTO ref.osm_feature (site, osm_id, layer, kind, name, tags, snapshot, geom)
SELECT %(aoi)s, c.osm_id, c.layer,
       COALESCE(c.tags->>'highway', c.tags->>'railway', c.tags->>'waterway', c.tags->>'building',
                c.tags->>'water', c.tags->>'landuse', c.tags->>'natural', c.tags->>'leisure',
                c.tags->>'place', c.tags->>'amenity', c.tags->>'shop', c.tags->>'tourism',
                c.tags->>'historic'),
       c.tags->>'name', c.tags, %(snapshot)s, ST_MakeValid(c.geom)
  FROM classified c, meta.aoi a
 WHERE c.layer IS NOT NULL AND c.geom IS NOT NULL AND a.code = %(aoi)s AND a.kind = 'site'
   AND c.geom && a.bbox          -- bounding-box test only: no GEOS call on raw OSM geometries
"""


def _drop_staging() -> None:
    with connect() as conn:
        for t in ("lines", "multipolygons", "points"):
            conn.execute(f"DROP TABLE IF EXISTS staging.{t}")


def run(aoi: AOI) -> dict:
    raw = settings.raw_dir / "osm"
    if settings.osm_source == "geofabrik":
        url = settings.osm_pbf_url
        src, _ = cached_download(url, raw / Path(url).name, settings.osm_max_age_days, server_time=True)
    elif settings.osm_source == "overpass":
        src = _overpass(aoi, raw / f"overpass_{aoi.code}.osm", settings.osm_max_age_days)
    else:
        raise ValueError(f"GEOMON_OSM_SOURCE must be overpass or geofabrik, not {settings.osm_source!r}")
    snapshot = dt.date.fromtimestamp(src.stat().st_mtime)

    x0, y0, x1, y1 = aoi.bbox_wgs84
    cmd = [
        "ogr2ogr", "-f", "PostgreSQL", settings.ogr_pg, str(src),
        "lines", "multipolygons", "points",
        "-spat", str(x0), str(y0), str(x1), str(y1),
        "-overwrite",
        "-lco", "SCHEMA=staging", "-lco", "GEOMETRY_NAME=geom",
        "-lco", "COLUMN_TYPES=other_tags=hstore", "-lco", "SPATIAL_INDEX=NONE",
        "--config", "PG_USE_COPY", "YES",
        "--config", "OSM_MAX_TMPFILE_SIZE", "1024",
        # Drop broken polygons (unclosed rings) instead of loading invalid geometries
        "--config", "OGR_GEOMETRY_ACCEPT_UNCLOSED_RING", "NO",
    ]
    # ogr2ogr's -overwrite does not see tables in a non-default schema: drop them first
    _drop_staging()
    log.info("Loading %s into staging with ogr2ogr", src.name)
    t0 = time.time()
    subprocess.run(cmd, check=True)
    log.info("ogr2ogr done in %.0f s", time.time() - t0)

    with connect() as conn:
        conn.execute("DELETE FROM ref.osm_feature WHERE site = %s", (aoi.code,))
        conn.execute(_CLASSIFY_SQL, {"snapshot": snapshot, "aoi": aoi.code})
        rows = conn.execute(
            "SELECT layer, count(*) AS n FROM ref.osm_feature WHERE site = %s GROUP BY layer ORDER BY layer",
            (aoi.code,),
        ).fetchall()
    _drop_staging()
    counts = {r["layer"]: r["n"] for r in rows}
    log.info("OSM features (snapshot %s): %s", snapshot, counts)
    return {"source": settings.osm_source, "snapshot": snapshot.isoformat(), **counts}
