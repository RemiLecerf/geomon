"""Météo-France public APIs (portail-api.meteofrance.fr): authentication, rate limits and the WCS
client of the forecast models (AROME, AROME-PI, PE-AROME, waves).

Every API of the portal is free with an account; each one must be subscribed by the application.
Authentication, the first one set:

- GEOMON_METEOFRANCE_APPLICATION_ID (preferred): the application's OAuth2 credentials (base64 of
  ``consumer_key:consumer_secret``, as the portal shows them); a token is fetched and renewed before
  its hour runs out;
- GEOMON_METEOFRANCE_API_KEY: an API key. It is a JWT listing the APIs the application was subscribed
  to *when it was generated* (subscribe first, then generate it). With many APIs it grows past 7 KB,
  and the model servers (WCS) then drop the requests: use the OAuth2 credentials instead.

An API not subscribed answers 403 ``Resource forbidden`` (raised as NotSubscribed). Requests are
spaced to stay under PER_MINUTE per API, and each opens its own connection: the portal's gateway
drops kept-alive connections without answering.

The model APIs are OGC WCS 2.0: coverages named ``PARAMETER___<run time>[_<accumulation>]``, read
one forecast time at a time over a longitude/latitude box, as GRIB2.
"""

from __future__ import annotations

import re
import threading
import time
from functools import lru_cache
from pathlib import Path

import numpy as np
import requests
from requests.adapters import HTTPAdapter

from .config import settings
from .http import _Retry
from .zarrio import write_bytes_atomic

PUBLIC = "https://public-api.meteofrance.fr"
TOKEN_URL = "https://portail-api.meteofrance.fr/token"
PER_MINUTE = 50                 # the portal's limit, per API
FORMAT = "application/wmo-grib"


class NotSubscribed(RuntimeError):
    """The application is not subscribed to this API on the portal."""


_token: dict = {}
_token_lock = threading.Lock()
_rate_lock = threading.Lock()
_last: dict[str, float] = {}
_ANSWERS: dict[tuple, tuple[float, object]] = {}
ANSWER_TTL_S = 300          # a service's coverage list and forecast times, shared by the sites of a pass


def _remembered(key: tuple, fetch):
    """``fetch()``, or its answer of the last ANSWER_TTL_S seconds."""
    hit = _ANSWERS.get(key)
    if hit is None or time.monotonic() - hit[0] > ANSWER_TTL_S:
        if len(_ANSWERS) > 256:
            _ANSWERS.clear()
        hit = _ANSWERS[key] = (time.monotonic(), fetch())
    return hit[1]


def configured() -> bool:
    return bool(settings.meteofrance_api_key or settings.meteofrance_application_id)


@lru_cache(maxsize=1)
def _session() -> requests.Session:
    """Retries (connection errors, 429 with its Retry-After, 5xx) as geomon.http, no kept-alive
    connection."""
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=_Retry(total=5, backoff_factor=2.0, status_forcelist=(429, 500, 502, 503, 504),
                                                       allowed_methods=("GET", "POST"))))
    s.headers.update({"User-Agent": settings.user_agent, "Connection": "close"})
    return s


def headers() -> dict:
    app = settings.meteofrance_application_id
    if not app:
        return {"apikey": settings.meteofrance_api_key}
    if app.count(".") == 2:
        raise RuntimeError("GEOMON_METEOFRANCE_APPLICATION_ID holds an access token (valid one hour), not the "
                           "application's credentials: give base64 of consumer_key:consumer_secret")
    with _token_lock:
        if not _token or _token["expires"] - time.time() < 120:
            r = _session().post(TOKEN_URL, data={"grant_type": "client_credentials"}, timeout=60,
                                headers={"Authorization": f"Basic {app}"})
            r.raise_for_status()
            d = r.json()
            _token.update(value=d["access_token"], expires=time.time() + int(d.get("expires_in", 3600)))
        return {"Authorization": f"Bearer {_token['value']}"}


def get(url: str, params=None, timeout: int = 120) -> requests.Response:
    """A GET on a Météo-France API, spaced to stay under its rate limit (per API path)."""
    api = "/".join(url.split("/")[3:6])
    gap = 60.0 / PER_MINUTE * 1.1
    with _rate_lock:
        wait = _last.get(api, 0) + gap - time.monotonic()
        _last[api] = max(time.monotonic(), _last.get(api, 0) + gap)
    if wait > 0:
        time.sleep(wait)
    r = _session().get(url, params=params, timeout=timeout, headers=headers())
    if r.status_code == 401 and settings.meteofrance_application_id:     # a token revoked early: once more
        _token.clear()
        r = _session().get(url, params=params, timeout=timeout, headers=headers())
    if r.status_code == 403 and "900908" in r.text:
        raise NotSubscribed(f"Not subscribed to {api.split('/')[1]} on portail-api.meteofrance.fr")
    return r


# ---------------------------------------------------------------------------
# Grids
# ---------------------------------------------------------------------------
def bbox(extent_wgs84: tuple[float, float, float, float], margin_km: float) -> tuple[float, float, float, float]:
    """A box plus ``margin_km``, in degrees, rounded outwards to 0.01°."""
    x0, y0, x1, y1 = extent_wgs84
    dy = margin_km / 110.57
    dx = margin_km / (111.32 * max(0.2, np.cos(np.radians((y0 + y1) / 2))))
    return (float(np.floor((x0 - dx) * 100) / 100), float(np.floor((y0 - dy) * 100) / 100),
            float(np.ceil((x1 + dx) * 100) / 100), float(np.ceil((y1 + dy) * 100) / 100))


def centres(profile: dict) -> tuple[np.ndarray, np.ndarray]:
    """Cell-centre longitudes and latitudes of a GeoTIFF read by WCS.read."""
    t = profile["transform"]
    x = t.c + (np.arange(profile["width"]) + 0.5) * t.a
    y = t.f + (np.arange(profile["height"]) + 0.5) * t.e
    return np.round(x, 6), np.round(y, 6)


# ---------------------------------------------------------------------------
# WCS
# ---------------------------------------------------------------------------
class WCS:
    """A WCS service of a Météo-France model, e.g.
    ``WCS("/public/aromepi/1.0/wcs/MF-NWP-HIGHRES-AROMEPI-001-FRANCE-WCS")``."""

    def __init__(self, path: str):
        self.base = PUBLIC + path

    def coverages(self) -> list[str]:
        def fetch():
            r = get(f"{self.base}/GetCapabilities", {"service": "WCS", "version": "2.0.1", "language": "fre"})
            r.raise_for_status()
            return re.findall(r"<wcs:CoverageId>([^<]+)</wcs:CoverageId>", r.text)
        return _remembered(("capabilities", self.base), fetch)

    def runs(self, parameter: str, suffix: str = "") -> list[str]:
        """The coverages of a parameter, newest run first (``suffix``: an accumulation such as
        ``_PT15M``; without one, the instantaneous coverage)."""
        def match(c: str) -> bool:
            if not c.startswith(parameter + "___"):
                return False
            return c.endswith(suffix) if suffix else not re.search(r"_P\d|_PT\d", c.split("___")[1][20:])
        return sorted(filter(match, self.coverages()), reverse=True)

    def latest(self, parameter: str, suffix: str = "") -> str | None:
        ids = self.runs(parameter, suffix)
        return ids[0] if ids else None

    def latest_complete(self, parameter: str, suffix: str = "", n: int = 1, runs: int = 3,
                        stop_at: str | None = None) -> tuple[str | None, list[int]] | None:
        """The newest coverage of a parameter whose run is complete (at least ``n`` forecast times,
        a run being published progressively), among the ``runs`` latest, and its forecast times.
        ``stop_at``: the run id already stored: reached first, it gives (None, []) (nothing newer)
        without describing it. None when no run is complete."""
        for c in self.runs(parameter, suffix)[:runs]:
            if stop_at and self.run_id(c) == stop_at:
                return None, []
            offsets = self.offsets(c)
            if len(offsets) >= n:
                return c, offsets
        return None

    @staticmethod
    def run_id(coverage: str) -> str:
        return coverage.split("___")[1][:20]

    @classmethod
    def run_time(cls, coverage: str) -> np.datetime64:
        return np.datetime64(cls.run_id(coverage).replace(".", ":").rstrip("Z"), "s")

    def offsets(self, coverage: str) -> list[int]:
        """Forecast times of a coverage, in seconds after its run."""
        def fetch():
            r = get(f"{self.base}/DescribeCoverage", {"service": "WCS", "version": "2.0.1", "coverageID": coverage})
            r.raise_for_status()
            for axis in re.findall(r"<gmlrgrid:GeneralGridAxis>(.*?)</gmlrgrid:GeneralGridAxis>", r.text, re.S):
                if "<gmlrgrid:gridAxesSpanned>time</gmlrgrid:gridAxesSpanned>" in axis:
                    c = re.search(r"<gmlrgrid:coefficients>([^<]*)</gmlrgrid:coefficients>", axis)
                    return [int(float(v)) for v in c.group(1).split()] if c else []
            return []
        return _remembered(("offsets", self.base, coverage), fetch)

    def read(self, coverage: str, box: tuple[float, float, float, float], offset_s: int | None = None,
             height: int | None = None):
        """One forecast time of a coverage over (lon0, lat0, lon1, lat1): (array, rasterio profile).

        Read as GRIB2: the same values as the service's GeoTIFF (float64, uncompressed) in 6 to 24 %
        of the bytes; GDAL gives temperatures in °C as the GeoTIFF does.

        Answers are kept on disk for READ_KEEP_H hours, keyed by the request: a coverage id holds
        its run, so the same request always gets the same answer, and another site or process (or
        a run started again) reading it costs no request."""
        import rasterio
        from rasterio.transform import from_bounds

        params = [("service", "WCS"), ("version", "2.0.1"), ("coverageid", coverage), ("format", FORMAT),
                  ("subset", f"lat({box[1]},{box[3]})"), ("subset", f"long({box[0]},{box[2]})")]
        if height is not None:
            params.append(("subset", f"height({height})"))
        if offset_s is not None:
            params.append(("subset", f"time({offset_s})"))
        path = _read_path(self.base, params)
        if path.exists():
            content = path.read_bytes()
        else:
            r = get(f"{self.base}/GetCoverage", params)
            if r.status_code != 200:
                raise RuntimeError(f"{coverage} at {offset_s} s: HTTP {r.status_code} {r.text[:200]}")
            content = r.content
            write_bytes_atomic(path, content)
        with rasterio.MemoryFile(content) as mf, mf.open() as src:
            a = src.read(1).astype("float32")
            if src.nodata is not None:
                a[a == np.float32(src.nodata)] = np.nan
            t = src.transform
            if abs(t.a) > 1 or abs(t.e) > 1:      # a broken geotransform (HYCOM2D): the cells fill the box asked
                t = from_bounds(box[0], box[1], box[2], box[3], src.width, src.height)
            return a, {"transform": t, "crs": src.crs.to_wkt(), "width": src.width, "height": src.height}

    def read_stack(self, coverage: str, box, offsets: list[int], height: int | None = None,
                   shape: tuple[int, int] | None = None):
        """Several forecast times of a coverage, stacked: (array[time, y, x], profile of the first)."""
        arrays, profile = [], None
        for o in offsets:
            a, p = self.read(coverage, box, o, height=height)
            if shape is not None and a.shape != shape:
                raise RuntimeError(f"{coverage}: grid {a.shape} differs from {shape}")
            arrays.append(a)
            profile = profile or p
        return np.stack(arrays), profile



READ_KEEP_H = 24
_pruned: dict = {"at": 0.0}


def _read_path(base: str, params: list) -> Path:
    """Where the answer of a GetCoverage request is kept (older answers pruned hourly)."""
    import hashlib

    d = settings.raw_dir / "meteofrance_wcs"
    if time.monotonic() - _pruned["at"] > 3600 and d.exists():
        _pruned["at"] = time.monotonic()
        limit = time.time() - READ_KEEP_H * 3600
        for f in d.iterdir():
            if f.stat().st_mtime < limit:
                f.unlink(missing_ok=True)
    return d / (hashlib.sha1(repr((base, params)).encode()).hexdigest() + ".grb2")
