"""Daily weather against the 1991-2020 climate normal (WMO reference period).

The normal of a series is computed from that same series (SIM2 against SIM2,
AgERA5 against AgERA5), so that differences between products do not show up
as anomalies.

A comparison covers one year from a chosen start (1 January, or the start of
an agricultural year). Each reference year contributes the same period: with
a 1 January start the years 1991 to 2020; with an autumn start the periods
whose middle falls in 1991-2020 (September 1990 - August 1991 to September
2019 - August 2020). Days are matched by their offset from the start.

* Averaged variables (temperature, humidity, soil moisture...): for each day,
  the values of the reference years within a 31-day window centred on that
  day (30 years x 31 days); the normal is their mean, the bands their
  minimum-maximum, 10th-90th and 25th-75th percentiles (the range of a single
  day at that time of year).
* Summed variables (precipitation, ET0, drainage...): the total since the
  start of the period; the normal is the mean of the reference years' totals
  on the same day, the bands their minimum-maximum, 10th-90th and 25th-75th
  percentiles. Missing days in
  a reference year are filled with that day's normal.

A reference year is used when at least 90 % of its days have a value; the
normal is given when at least 24 years (80 % of 30, as WMO asks) are usable.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from .arrays import quiet as _quiet

REF_FIRST, REF_LAST = 1991, 2020
WINDOW = 15                 # half-width of the daily window, in days
MIN_COVERAGE = 0.9
MIN_YEARS = 24


def period_start(start: dt.date, year: int) -> dt.date:
    """The same start day in another year (29 February becomes 28 February)."""
    try:
        return start.replace(year=year)
    except ValueError:
        return start.replace(year=year, day=28)


def period_days(start: dt.date) -> int:
    return (period_start(start, start.year + 1) - start).days


def reference_years(start: dt.date) -> list[int]:
    """Start years of the reference periods: those whose middle falls in 1991-2020."""
    half = dt.timedelta(days=182)
    return [y for y in range(REF_FIRST - 1, REF_LAST + 1)
            if REF_FIRST <= (period_start(start, y) + half).year <= REF_LAST]


def _daily(s: pd.Series) -> pd.Series:
    """A daily series without NaN, one value per day (the last one)."""
    s = s.dropna()
    s.index = pd.DatetimeIndex(s.index).normalize()
    return s[~s.index.duplicated(keep="last")]


def _values(s: pd.Series, first: dt.date, n: int) -> np.ndarray:
    idx = pd.date_range(pd.Timestamp(first), periods=n, freq="D")
    return s.reindex(idx).to_numpy(dtype="float64")


QUANTILES = {"p10": 10, "p25": 25, "p75": 75, "p90": 90}


def _bands(x: np.ndarray, axis: int) -> dict[str, np.ndarray]:
    """Mean ("normal"), min, max and the quantiles of ``QUANTILES`` along ``axis`` (NaN ignored)."""
    with _quiet():
        out = {"normal": np.nanmean(x, axis=axis), "min": np.nanmin(x, axis=axis), "max": np.nanmax(x, axis=axis)}
        qs = np.nanpercentile(x, list(QUANTILES.values()), axis=axis)
    return out | dict(zip(QUANTILES, qs))


def _window_stats(m: np.ndarray, n: int) -> dict[str, np.ndarray]:
    """Mean, min, max and quantiles of each day over the years and a centred window (``m``:
    years x (n + 2 WINDOW) days, the window margins included)."""
    w = np.lib.stride_tricks.sliding_window_view(m, 2 * WINDOW + 1, axis=1)   # years, n, window
    pooled = w.transpose(1, 0, 2).reshape(n, -1)
    ok = np.isfinite(pooled).sum(axis=1) >= pooled.shape[1] // 2
    return {k: np.where(ok, v, np.nan) for k, v in _bands(pooled, 1).items()}


def reference(s: pd.Series, how: str, start: dt.date) -> dict:
    """The 1991-2020 normal of a daily series for a year starting on ``start``. It depends only
    on the month and day of the start and on the length of the year, so it can be cached and
    reused for every year compared. ``available`` is False with a ``reason`` when the series
    does not cover enough of the reference period."""
    s = _daily(s)
    n = period_days(start)
    years = reference_years(start)
    rows, used = [], []
    for y in years:
        v = _values(s, period_start(start, y) - dt.timedelta(days=WINDOW), n + 2 * WINDOW)
        if np.isfinite(v[WINDOW:WINDOW + n]).mean() >= MIN_COVERAGE:
            rows.append(v)
            used.append(y)
    if len(used) < MIN_YEARS:
        first = s.index.min().date() if len(s) else None
        return {"available": False, "years_used": used,
                "reason": f"{len(used)} usable reference years out of {len(years)} (at least {MIN_YEARS} needed); "
                          f"the series starts {first}" if first else "no data"}
    m = np.vstack(rows)
    daily = _window_stats(m, n)
    ref = m[:, WINDOW:WINDOW + n]                       # years x days, the period only
    out = {"available": True, "years": used, "daily": daily}
    if how == "sum":
        ref = np.where(np.isfinite(ref), ref, daily["normal"])   # missing days: the normal of the day
        out["cumulative"] = _bands(np.cumsum(ref, axis=1), 0)
    out["ref"] = ref
    return out


def compare(s: pd.Series, how: str, start: dt.date, ahead: pd.Series | None = None,
            ahead_from: dt.date | None = None, ref: dict | None = None) -> dict:
    """Compares one year of a daily series, from ``start``, with its 1991-2020 normal.

    ``how``: "sum" (compared as totals since the start) or "mean". ``ahead``: another daily
    series continuing the observations after their last day (the Météo-France archive, then
    the forecast from ``ahead_from``). Its lists start on the last observed day, so that its
    line joins the observations, and totals carry on from the last observed total. ``ref``:
    the result of :func:`reference` for this start, when already computed.
    Returns lists over the days of the period (None where unknown) and a summary.
    """
    ref = ref if ref is not None else reference(s, how, start)
    if not ref["available"]:
        return ref
    n = period_days(start)
    cur = _values(_daily(s), start, n)
    known = np.flatnonzero(np.isfinite(cur))
    last = int(known[-1]) if known.size else -1
    dates = [(start + dt.timedelta(days=k)).isoformat() for k in range(n)]
    out = {"available": True, "how": how, "start": start.isoformat(), "days": n, "dates": dates,
           "reference": {"first": REF_FIRST, "last": REF_LAST, "years": ref["years"], "window_days": 2 * WINDOW + 1},
           "last": dates[last] if last >= 0 else None, "last_day": last,
           "daily": {"current": cur, **ref["daily"]}}
    if how == "sum":
        cur_cum = np.where(np.arange(n) <= last, np.cumsum(np.nan_to_num(cur)), np.nan)
        out["cumulative"] = {"current": cur_cum, **ref["cumulative"]}
        out["missing_days"] = int((~np.isfinite(cur[:last + 1])).sum())
    out["summary"] = _summary(cur, ref["ref"], last, how) if last >= 0 else []

    # Continuation after the last observed day: archive (bridging a lagging source), then forecast
    nxt = _values(_daily(ahead), start, n) if ahead is not None and 0 <= last < n - 1 else None
    if nxt is not None and np.isfinite(nxt[last + 1]):
        gaps = np.flatnonzero(~np.isfinite(nxt[last + 1:]))
        end = last + int(gaps[0]) if gaps.size else n - 1                # contiguous run only
        fut = np.full(n, np.nan)
        fut[last + 1:end + 1] = nxt[last + 1:end + 1]
        fut[last] = cur[last]                                            # the junction
        fc0 = (ahead_from - start).days if ahead_from else None
        out["ahead"] = {"daily": fut, "first_day": last + 1, "last_day": end,
                        "forecast_day": fc0 if fc0 is not None and last < fc0 <= end else None}
        if how == "sum":
            cum = np.full(n, np.nan)
            cum[last:end + 1] = cur_cum[last] + np.concatenate([[0.0], np.cumsum(fut[last + 1:end + 1])])
            out["ahead"]["cumulative"] = cum
        both = np.where(np.isfinite(cur), cur, fut)
        out["summary"] += _summary(both, ref["ref"], end, how, periods=(("ahead", end - last), ("start_ahead", None)))
    if how == "mean" and last >= 0:
        past = slice(0, last + 1)
        c, lo, hi = cur[past], ref["daily"]["p10"][past], ref["daily"]["p90"][past]
        ok = np.isfinite(c) & np.isfinite(lo) & np.isfinite(hi)
        out["days_above_p90"] = int((c[ok] > hi[ok]).sum())
        out["days_below_p10"] = int((c[ok] < lo[ok]).sum())
        out["days_compared"] = int(ok.sum())
    return out


def _summary(cur: np.ndarray, ref: np.ndarray, last: int, how: str,
             periods: tuple = (("7d", 7), ("30d", 30), ("start", None))) -> list[dict]:
    """Totals (sums) or means (other variables) over the days ending on ``last`` (by default
    the last 7 and 30 days and since the start), against the same days of each reference year."""
    reduce = np.nansum if how == "sum" else np.nanmean
    out = []
    for key, days in periods:
        a = 0 if days is None else max(0, last + 1 - days)
        sel = slice(a, last + 1)
        c = cur[sel]
        if np.isfinite(c).mean() < 0.8:
            continue
        with _quiet():
            cv = float(reduce(c))
            rv = reduce(ref[:, sel], axis=1)
        rv = rv[np.isfinite(rv)]
        stats = {k: float(v) for k, v in _bands(rv, 0).items()}
        row = {"period": key, "from_day": a, "to_day": last, "days": last + 1 - a,
               "current": cv, **stats, "diff": cv - stats["normal"],
               "below": int((rv < cv).sum()), "years": int(rv.size)}
        if how == "sum" and stats["normal"] > 0:
            row["ratio"] = cv / stats["normal"]
        out.append(row)
    return out
