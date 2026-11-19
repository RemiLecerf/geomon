"""Small numpy helpers shared by the analysis modules."""

from __future__ import annotations

import warnings
from contextlib import contextmanager


@contextmanager
def quiet():
    """Silences numpy's warnings about all-NaN slices and empty means (the results stay NaN)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        yield
