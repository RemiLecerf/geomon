"""Unit handling (Specifications §3.5).

Every stored variable declares its unit. This module converts values between
units in one call, e.g.::

    >>> convert(15.0, "degC", "K")
    288.15
    >>> convert(3.2, "g/kg", "percent")        # 3.2 g/kg = 0.32 %
    0.32
    >>> convert(1.0, "kg/m**2/s", "mm/day")    # water flux, via water density
    86400.0
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import numpy as np
import pint

_EXTRA_DEFINITIONS = """
person = [population] = persons = inhabitant
dwelling = [dwelling] = dwellings
household = [household] = households
establishment = [establishment] = establishments
"""

# Aliases found in source metadata -> pint expressions
ALIASES: dict[str, str] = {
    "%": "percent",
    "pct": "percent",
    "PT": "percent",          # INSEE "point(s) de pourcentage" / percentage
    "EUR": "EUR",
    "EUR_YR": "EUR/year",
    "m": "meter",
    "deg": "degree",
    "1": "dimensionless",
    "mm/h": "millimeter/hour",
}


@lru_cache(maxsize=1)
def registry() -> pint.UnitRegistry:
    ureg = pint.UnitRegistry()
    for line in _EXTRA_DEFINITIONS.strip().splitlines():
        ureg.define(line.strip())
    ureg.define("EUR = [currency] = euro")
    # Water flux <-> depth: 1 kg m-2 of water = 1 mm (density 1000 kg m-3)
    ctx = pint.Context("water")
    ctx.add_transformation(
        "[mass] / [length] ** 2 / [time]",
        "[length] / [time]",
        lambda ureg, x: x / ureg.Quantity(1000, "kg/m**3"),
    )
    ctx.add_transformation(
        "[mass] / [length] ** 2",
        "[length]",
        lambda ureg, x: x / ureg.Quantity(1000, "kg/m**3"),
    )
    ureg.add_context(ctx)
    return ureg


def normalise(unit: str) -> str:
    return ALIASES.get(unit, unit)


def convert(value: Any, from_unit: str, to_unit: str) -> Any:
    """Convert a scalar or a NumPy array from one unit to another."""
    ureg = registry()
    q = ureg.Quantity(np.asarray(value, dtype=float), normalise(from_unit))
    try:
        out = q.to(normalise(to_unit))
    except pint.DimensionalityError:
        out = q.to(normalise(to_unit), "water")
    m = out.magnitude
    return float(m) if np.ndim(m) == 0 else m


def is_compatible(from_unit: str, to_unit: str) -> bool:
    try:
        convert(1.0, from_unit, to_unit)
        return True
    except Exception:
        return False
