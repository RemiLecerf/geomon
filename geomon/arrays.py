"""Small numpy helpers shared by the analysis modules."""

from __future__ import annotations

import warnings
from contextlib import contextmanager

import numpy as np


@contextmanager
def quiet():
    """Silences numpy's warnings about all-NaN slices and empty means (the results stay NaN)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        yield


def finite_round(v, digits: int = 2) -> float | None:
    """A number for JSON: rounded, None when NaN or infinite."""
    return round(float(v), digits) if v is not None and np.isfinite(v) else None


def finite_list(a, digits: int = 2) -> list:
    return [finite_round(v, digits) for v in np.asarray(a, dtype="float64").ravel()]


def nan_agg(fn, a, digits: int = 1) -> float | None:
    """``fn`` (np.nanmin, np.nanmax, np.nansum...) of the finite values of ``a``, None without any."""
    a = np.asarray(a, dtype="float64")
    return finite_round(fn(a), digits) if np.isfinite(a).any() else None
