"""HTTP session with retries, and small OGC helpers."""

from __future__ import annotations

import email.utils
import logging
import os
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import requests
from requests.adapters import HTTPAdapter
from urllib3.exceptions import MaxRetryError, ResponseError
from urllib3.util.retry import Retry

from .config import settings

log = logging.getLogger(__name__)

IGN_WFS = "https://data.geopf.fr/wfs/ows"
IGN_WMS_R = "https://data.geopf.fr/wms-r/wms"

# Longest Retry-After a request waits for. Beyond it (a daily quota: "come back tomorrow") the
# request fails at once instead of sleeping for hours while its step holds an ingestion lock.
MAX_RETRY_AFTER_S = 120


class _Retry(Retry):
    def increment(self, method=None, url=None, response=None, error=None, _pool=None, _stacktrace=None):
        after = response.headers.get("Retry-After") if response is not None else None
        if after and self.parse_retry_after(after) > MAX_RETRY_AFTER_S:
            raise MaxRetryError(_pool, url, ResponseError(f"{response.status}, Retry-After {after} s"))
        return super().increment(method, url, response, error, _pool, _stacktrace)


@lru_cache(maxsize=1)
def session() -> requests.Session:
    s = requests.Session()
    retry = _Retry(
        total=5,
        backoff_factor=2.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST", "HEAD"),
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.headers["User-Agent"] = settings.user_agent
    return s


def get_json(url: str, params: dict[str, Any] | None = None, timeout: int = 120, attempts: int = 4) -> Any:
    """GET a JSON document. A 200 answer whose body is not JSON (an empty page during a
    service hiccup, as IGN sometimes returns) is retried like a server error."""
    import time

    for attempt in range(attempts):
        r = session().get(url, params=params, timeout=timeout)
        r.raise_for_status()
        try:
            return r.json()
        except ValueError:
            if attempt == attempts - 1:
                raise RuntimeError(f"{url}: not a JSON answer ({len(r.content)} bytes): {r.text[:200]!r}")
            wait = 5 * 3 ** attempt
            log.warning("%s: not a JSON answer (%d bytes), retrying in %d s", url, len(r.content), wait)
            time.sleep(wait)


def wfs_features(
    typename: str,
    *,
    cql: str | None = None,
    bbox_wgs84: tuple[float, float, float, float] | None = None,
    page_size: int = 5000,
) -> list[dict]:
    """Fetch all features of an IGN Géoplateforme WFS layer as GeoJSON (EPSG:4326).

    ``bbox_wgs84`` is (min_lon, min_lat, max_lon, max_lat). GeoServer does not
    accept CQL_FILTER and BBOX together, so the bbox is turned into a CQL BBOX
    filter when both are given.
    """
    params: dict[str, Any] = {
        "SERVICE": "WFS",
        "VERSION": "2.0.0",
        "REQUEST": "GetFeature",
        "TYPENAMES": typename,
        "OUTPUTFORMAT": "application/json",
        "SRSNAME": "EPSG:4326",
        "COUNT": page_size,
    }
    if cql:
        params["CQL_FILTER"] = cql
    elif bbox_wgs84:
        x0, y0, x1, y1 = bbox_wgs84
        # WFS 2.0 + EPSG URN => latitude/longitude axis order
        params["BBOX"] = f"{y0},{x0},{y1},{x1},urn:ogc:def:crs:EPSG::4326"

    features: list[dict] = []
    start = 0
    while True:
        params["STARTINDEX"] = start
        data = get_json(IGN_WFS, params, timeout=300)
        batch = data.get("features", [])
        features.extend(batch)
        log.debug("%s: %d features (start=%d)", typename, len(batch), start)
        if len(batch) < page_size:
            break
        start += page_size
    return features


@lru_cache(maxsize=1)
def _wfs_capabilities() -> str:
    r = session().get(
        IGN_WFS,
        params={"SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetCapabilities"},
        timeout=300,
    )
    r.raise_for_status()
    return r.text


def wfs_layer_title(typename: str) -> str | None:
    """Return the human title of a WFS layer from the capabilities document."""
    text = _wfs_capabilities()
    i = text.find(f"<Name>{typename}</Name>")
    if i < 0:
        return None
    m = re.search(r"<Title>(.*?)</Title>", text[i : i + 3000], re.S)
    return m.group(1).strip() if m else None


def chunks(items: list, size: int) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def datagouv_resources(dataset_id: str) -> list[dict]:
    """The resources (files) of a data.gouv.fr dataset: [{"url", "title", ...}]."""
    return get_json(f"https://www.data.gouv.fr/api/1/datasets/{dataset_id}/")["resources"]


def is_fresh(path: Path, max_age_days: float) -> bool:
    """A local copy younger than ``max_age_days``."""
    return path.exists() and (time.time() - path.stat().st_mtime) / 86400 <= max_age_days


def cached_download(url: str, dest: Path, max_age_days: float, server_time: bool = False,
                    missing_ok: bool = False) -> tuple[Path | None, bool]:
    """``url`` saved as ``dest`` (streamed, renamed into place when complete): (path, changed).

    A copy younger than ``max_age_days`` is used as it is. An older one is checked with a
    conditional request (If-Modified-Since): when the server's file has not changed, the copy is
    kept (and dated now, unless ``server_time``). ``server_time``: the copy is dated by the
    server's Last-Modified, so its age is the data's age (an OSM extract). ``missing_ok``: a 404
    gives (None, False)."""
    if is_fresh(dest, max_age_days):
        return dest, False
    dest.parent.mkdir(parents=True, exist_ok=True)
    headers = {"If-Modified-Since": email.utils.formatdate(dest.stat().st_mtime, usegmt=True)} if dest.exists() else {}
    tmp = dest.with_name(dest.name + ".part")
    with session().get(url, stream=True, timeout=600, headers=headers) as r:
        if r.status_code == 304:
            if not server_time:
                os.utime(dest, None)
            return dest, False
        if missing_ok and r.status_code == 404:
            return None, False
        r.raise_for_status()
        log.info("Downloading %s", url)
        total = int(r.headers.get("Content-Length", 0))
        done, next_report = 0, 0.0
        with open(tmp, "wb") as fh:
            for block in r.iter_content(chunk_size=4 << 20):
                fh.write(block)
                done += len(block)
                if total > 50e6 and done / total >= next_report:
                    log.info("  %3.0f%% of %.0f MB", 100 * done / total, total / 1e6)
                    next_report += 0.1
        last_modified = r.headers.get("Last-Modified")
    os.replace(tmp, dest)
    if server_time and last_modified:
        ts = email.utils.parsedate_to_datetime(last_modified).timestamp()
        os.utime(dest, (ts, ts))
    return dest, True
