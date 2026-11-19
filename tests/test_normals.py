import datetime as dt

import numpy as np
import pandas as pd
import pytest

from geomon import normals as nm


def test_period_start_moves_29_february():
    assert nm.period_start(dt.date(2024, 2, 29), 2025) == dt.date(2025, 2, 28)
    assert nm.period_start(dt.date(2024, 9, 1), 1990) == dt.date(1990, 9, 1)


def test_period_days():
    assert nm.period_days(dt.date(2023, 1, 1)) == 365
    assert nm.period_days(dt.date(2024, 1, 1)) == 366
    assert nm.period_days(dt.date(2023, 9, 1)) == 366      # contains 29 February 2024


def test_reference_years_are_30_whose_middle_is_in_1991_2020():
    assert nm.reference_years(dt.date(2026, 1, 1)) == list(range(1991, 2021))
    # A September start: September 1990 - August 1991 ... September 2019 - August 2020
    assert nm.reference_years(dt.date(2025, 9, 1)) == list(range(1990, 2020))


def _daily(start, end, f):
    idx = pd.date_range(start, end, freq="D")
    return pd.Series([f(d) for d in idx], index=idx)


def _seasonal(d):
    return 10 + 8 * np.sin(2 * np.pi * (d.dayofyear - 110) / 365.25)


def test_mean_normal_of_a_repeating_climate():
    s = _daily("1989-01-01", "2026-06-30", _seasonal)
    start = dt.date(2026, 1, 1)
    ref = nm.reference(s, "mean", start)
    assert ref["available"] and len(ref["years"]) == 30
    expected = np.array([_seasonal(d) for d in pd.date_range("2026-01-01", periods=365)])
    # The normal averages a 31-day window: close to the seasonal curve, slightly flattened at its extremes
    assert np.nanmax(np.abs(ref["daily"]["normal"] - expected)) < 0.15
    out = nm.compare(s, "mean", start, ref=ref)
    assert out["last"] == "2026-06-30"
    assert all(abs(r["diff"]) < 0.15 for r in out["summary"])


def test_sum_normal_is_the_mean_cumulative_total():
    s = _daily("1990-01-01", "2026-03-31", lambda d: 2.0)
    out = nm.compare(s, "sum", dt.date(2026, 1, 1))
    cum = out["cumulative"]
    assert cum["normal"][0] == pytest.approx(2.0)
    assert cum["normal"][99] == pytest.approx(200.0)
    assert cum["current"][89] == pytest.approx(180.0)          # 1 January - 31 March: 90 days
    start_row = next(r for r in out["summary"] if r["period"] == "start")
    assert start_row["ratio"] == pytest.approx(1.0)


def test_continuation_joins_the_last_observation():
    s = _daily("1990-01-01", "2026-03-31", lambda d: 1.0)
    ahead = _daily("2026-03-20", "2026-04-10", lambda d: 3.0)
    out = nm.compare(s, "sum", dt.date(2026, 1, 1), ahead=ahead, ahead_from=dt.date(2026, 4, 2))
    a = out["ahead"]
    last = out["last_day"]
    assert a["first_day"] == last + 1 and a["last_day"] == last + 10
    assert a["cumulative"][last] == pytest.approx(out["cumulative"]["current"][last])
    assert a["cumulative"][last + 10] == pytest.approx(out["cumulative"]["current"][last] + 30.0)
    assert a["forecast_day"] == (dt.date(2026, 4, 2) - dt.date(2026, 1, 1)).days


def test_short_record_has_no_normal():
    s = _daily("2005-01-01", "2026-03-31", lambda d: 1.0)
    ref = nm.reference(s, "mean", dt.date(2026, 1, 1))
    assert not ref["available"] and "at least 24" in ref["reason"]
