"""Area of interest: one or more French communes, resolved from IGN ADMIN EXPRESS.

The communes are analysed one by one and as a group. Data is downloaded once,
for the buffered extent of the group (``bbox_*``); ``geom_wgs84`` is the union
of the commune outlines.

``code`` names the AOI in file names and caches: the INSEE code for a single
commune, otherwise ``GEOMON_SITE_CODE`` (default ``grp-<codes>``).
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field

from pyproj import Transformer
from shapely.geometry import MultiPolygon, box, mapping, shape
from shapely.ops import transform, unary_union

from .config import settings
from .db import connect
from .http import wfs_features

log = logging.getLogger(__name__)

_to_l93 = Transformer.from_crs("EPSG:4326", "EPSG:2154", always_xy=True).transform
_to_wgs = Transformer.from_crs("EPSG:2154", "EPSG:4326", always_xy=True).transform


@dataclass
class Commune:
    code: str
    name: str
    props: dict
    geom_wgs84: dict

    @property
    def epci(self) -> str | None:
        return (self.props.get("codes_siren_des_epci") or "").split("/")[0] or None

    @property
    def arrondissement(self) -> str | None:
        return self.props.get("code_insee_de_l_arrondissement")

    @property
    def departement(self) -> str:
        return self.props["code_insee_du_departement"]

    @property
    def region(self) -> str:
        return self.props["code_insee_de_la_region"]


@dataclass
class AOI:
    code: str
    name: str
    communes: list[Commune]
    geom_wgs84: dict              # GeoJSON MultiPolygon: union of the communes
    bbox_l93: tuple[float, float, float, float]    # buffered, snapped
    bbox_wgs84: tuple[float, float, float, float]  # buffered
    props: dict = field(default_factory=dict)      # properties of the first commune

    @property
    def codes(self) -> list[str]:
        return [c.code for c in self.communes]

    @property
    def is_group(self) -> bool:
        return len(self.communes) > 1

    # Parent units: every distinct one over the communes (first commune first)
    def _distinct(self, attr: str) -> list[str]:
        out: list[str] = []
        for c in self.communes:
            v = getattr(c, attr)
            if v and v not in out:
                out.append(v)
        return out

    @property
    def epcis(self) -> list[str]:
        return self._distinct("epci")

    @property
    def arrondissements(self) -> list[str]:
        return self._distinct("arrondissement")

    @property
    def departements(self) -> list[str]:
        return self._distinct("departement")

    @property
    def regions(self) -> list[str]:
        return self._distinct("region")

    # Single-value accessors kept for code that only needs the reference commune's parents
    @property
    def epci(self) -> str | None:
        return self.communes[0].epci

    @property
    def arrondissement(self) -> str | None:
        return self.communes[0].arrondissement

    @property
    def departement(self) -> str:
        return self.communes[0].departement

    @property
    def region(self) -> str:
        return self.communes[0].region


def _snap(bounds: tuple[float, float, float, float], step: float) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = bounds
    return (
        math.floor(x0 / step) * step,
        math.floor(y0 / step) * step,
        math.ceil(x1 / step) * step,
        math.ceil(y1 / step) * step,
    )


_UNIT_LAYERS = {"DEP": ("departement", "code_insee"), "REG": ("region", "code_insee"),
                "ARR": ("arrondissement", "code_insee"), "EPCI": ("epci", "code_siren")}


def _unit_communes(unit: str) -> tuple[list[dict], str]:
    """Every commune of an administrative unit (sorted by INSEE code), and the unit's name."""
    from .config import UNIT_LEVELS

    level, _, code = unit.partition(":")
    if not code.isalnum():
        raise ValueError(f"Bad unit code {code!r}")
    attr = UNIT_LEVELS[level]
    # A commune may belong to several EPCI ("200068856/200071835"): match the code within the list
    cql = f"{attr} LIKE '%{code}%'" if level == "EPCI" else f"{attr}='{code}'"
    feats = wfs_features(f"ADMINEXPRESS-COG.{settings.cog_vintage}:commune", cql=cql)
    if level == "EPCI":
        feats = [f for f in feats if code in (f["properties"].get(attr) or "").split("/")]
    if not feats:
        raise RuntimeError(f"No commune in {unit} ({settings.cog_vintage} geography)")
    layer, key = _UNIT_LAYERS[level]
    unit_feats = wfs_features(f"ADMINEXPRESS-COG.{settings.cog_vintage}:{layer}", cql=f"{key}='{code}'")
    name = unit_feats[0]["properties"]["nom_officiel"] if unit_feats else unit
    return sorted(feats, key=lambda f: f["properties"]["code_insee"]), name


def resolve_aoi(codes: list[str] | tuple[str, ...] | None = None, buffer_m: float | None = None) -> AOI:
    """The current site's AOI: its communes (or every commune of its administrative unit)."""
    buffer_m = settings.aoi_buffer_m if buffer_m is None else buffer_m
    layer = f"ADMINEXPRESS-COG.{settings.cog_vintage}:commune"
    unit_name = None
    if codes is None and settings.unit:
        feats_list, unit_name = _unit_communes(settings.unit)
        feats = {f["properties"]["code_insee"]: f for f in feats_list}
        codes = list(feats)
    else:
        codes = list(codes or settings.communes)
        quoted = ",".join(f"'{c}'" for c in codes)
        feats = {f["properties"]["code_insee"]: f for f in wfs_features(layer, cql=f"code_insee IN ({quoted})")}
    missing = [c for c in codes if c not in feats]
    if missing:
        raise RuntimeError(f"Commune(s) {', '.join(missing)} not found in {layer}")
    communes = [Commune(c, feats[c]["properties"]["nom_officiel"], feats[c]["properties"], feats[c]["geometry"])
                for c in codes]
    union = unary_union([shape(c.geom_wgs84) for c in communes])
    if union.geom_type == "Polygon":
        union = MultiPolygon([union])
    l93 = transform(_to_l93, union)
    # Snap to the coarsest grid used (soil) so all rasters share aligned edges
    bbox_l93 = _snap(l93.buffer(buffer_m).bounds, settings.soil_res_m)
    bbox_wgs84 = transform(_to_wgs, box(*bbox_l93)).bounds
    code = settings.site_code           # the commune code for a single commune of the environment
    name = settings.site_name or unit_name or ", ".join(c.name for c in communes)
    aoi = AOI(code=code, name=name, communes=communes, geom_wgs84=mapping(union),
              bbox_l93=bbox_l93, bbox_wgs84=bbox_wgs84, props=communes[0].props)
    log.info("AOI %s %s (%d commune%s), bbox L93 %s", aoi.code, aoi.name, len(communes),
             "s" if len(communes) > 1 else "", aoi.bbox_l93)
    return aoi


def save_aoi(aoi: AOI) -> None:
    """Store the communes and the group in meta.aoi (one row each), replacing the site's previous AOI.

    Rows: kind 'commune' for each commune, and kind 'site' for the AOI itself (the
    group, or the single commune again), which carries the download extent.
    """
    rows = [(c.code, c.name, "commune", c.geom_wgs84, [c.code], i) for i, c in enumerate(aoi.communes)]
    rows.append((aoi.code, aoi.name, "site", aoi.geom_wgs84, aoi.codes, -1))
    from .migrate import migrate

    migrate()
    with connect() as conn:
        # Rows of this site only (other sites stay); the farm area stays too
        conn.execute("DELETE FROM meta.aoi WHERE site = %s AND kind IN ('site', 'commune')", (aoi.code,))
        for code, name, kind, geom, members, pos in rows:
            conn.execute(
                """
                INSERT INTO meta.aoi (code, name, kind, site, members, position, buffer_m, geom, bbox, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s,
                        ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)),
                        ST_Transform(ST_MakeEnvelope(%s, %s, %s, %s, 2154), 4326),
                        now())
                """,
                (code, name, kind, aoi.code, members, pos, settings.aoi_buffer_m, json.dumps(geom), *aoi.bbox_l93),
            )

