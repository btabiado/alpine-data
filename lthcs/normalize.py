"""Normalization helpers — pure-math functions for turning raw inputs into 0–100 sub-scores.

This module is the shared math layer for the LTHCS pillar scorers. Every public
function is a pure function: no I/O, no globals, no module-level state beyond
typing imports and the small constants below.

Three primary normalization strategies are supported, matching the LTHCS spec
(PHASE_1_BUILD_SPEC.md §5, §10, §11 Week 3):

* **Peer-relative percentile** — rank a value inside a peer distribution and
  return the rank in [0, 100]. Used most heavily; works regardless of the
  metric's scale or units.
* **Z-score normalization** — compute a z-score against a distribution, then
  clip/linearly map it onto [0, 100] via :func:`z_to_0_100`.
* **Bounded linear map** — for metrics where domain knowledge gives us sensible
  hard bounds (e.g., gross margin in [0, 1]); map linearly onto [0, 100] and
  clip outside the bounds. Supports inversion when "lower is better".

**Neutral-fallback convention.** When inputs are missing or undefined (empty
distribution, NaN value where a scalar 0–100 is expected, etc.), these helpers
return ``50.0`` — the midpoint of the 0–100 scale. This is a deliberate V1
policy: with free-tier APIs, partial data is the norm, and "we don't know,
treat as average" downstream-composes better than propagating NaN through every
pillar. Functions that produce raw statistical quantities (``z_score``,
``percentile_rank``, ``slope``) propagate NaN/None for genuinely-missing
*input* values, but the 0–100 mapping functions (``z_to_0_100``,
``bounded_linear``) collapse NaN to 50.0.

All returns are coerced to native Python ``float`` (not ``numpy.float64``) so
the values serialize cleanly via the standard library ``json`` module.
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence

import numpy as np

__all__ = [
    "percentile_rank",
    "z_score",
    "z_to_0_100",
    "bounded_linear",
    "peer_relative_percentile",
    "slope",
]


# --- Internal helpers -------------------------------------------------------


def _is_nan(x: float) -> bool:
    """Return True if x is NaN. Safe for ints, floats, numpy scalars."""
    try:
        return math.isnan(float(x))
    except (TypeError, ValueError):
        return False


def _clean(distribution: Sequence[float]) -> List[float]:
    """Return a list of float values from ``distribution`` with NaNs removed."""
    out: List[float] = []
    for v in distribution:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isnan(f):
            continue
        out.append(f)
    return out


# --- Public API -------------------------------------------------------------


def percentile_rank(value: float, distribution: Sequence[float]) -> float:
    """Return the percentile (0–100) of ``value`` within ``distribution``.

    Uses the standard "half-equal" percentile-rank convention:

        pct = (count_below + 0.5 * count_equal) / N * 100

    so ties don't artificially compress the distribution.

    Edge cases:
    * Empty distribution -> 50.0 (neutral fallback).
    * NaNs in ``distribution`` are silently ignored.
    * If ``value`` is NaN, return NaN (caller decides what to do).
    """
    if _is_nan(value):
        return float("nan")

    cleaned = _clean(distribution)
    n = len(cleaned)
    if n == 0:
        return 50.0

    v = float(value)
    count_below = sum(1 for x in cleaned if x < v)
    count_equal = sum(1 for x in cleaned if x == v)
    pct = (count_below + 0.5 * count_equal) / n * 100.0
    return float(pct)


def z_score(value: float, distribution: Sequence[float]) -> float:
    """Standard z-score: ``(value - mean) / stdev`` with sample stdev (``ddof=1``).

    Edge cases:
    * Fewer than 2 valid (non-NaN) values in ``distribution`` -> 0.0.
    * Stdev of 0 (all values equal) -> 0.0 (not inf/nan).
    * NaNs in ``distribution`` are silently ignored.
    * If ``value`` is NaN, return NaN.
    """
    if _is_nan(value):
        return float("nan")

    cleaned = _clean(distribution)
    if len(cleaned) < 2:
        return 0.0

    arr = np.asarray(cleaned, dtype=float)
    mean = float(np.mean(arr))
    # Sample stdev (ddof=1) — matches the convention used downstream when the
    # peer "universe" is itself a sample, not the population.
    stdev = float(np.std(arr, ddof=1))
    if stdev == 0.0:
        return 0.0

    return float((float(value) - mean) / stdev)


def z_to_0_100(z: float, clip: float = 3.0) -> float:
    """Linearly map a z-score in ``[-clip, clip]`` onto ``[0, 100]``, clipped.

    * ``z = 0``  -> 50.0
    * ``z = clip``  -> 100.0
    * ``z = -clip`` -> 0.0
    * Outside ``[-clip, clip]`` the result is clipped to 0 or 100.
    * NaN input -> 50.0 (neutral fallback).

    Raises:
        ValueError: if ``clip <= 0``.
    """
    if clip <= 0:
        raise ValueError(f"clip must be positive, got {clip}")

    if _is_nan(z):
        return 50.0

    zf = float(z)
    if zf >= clip:
        return 100.0
    if zf <= -clip:
        return 0.0
    # Linear interpolation: z=-clip -> 0, z=+clip -> 100.
    return float((zf + clip) / (2.0 * clip) * 100.0)


def bounded_linear(
    value: float,
    low: float,
    high: float,
    *,
    invert: bool = False,
) -> float:
    """Linearly map ``value`` from ``[low, high]`` onto ``[0, 100]``, clipped.

    If ``invert=True``, the mapping is flipped: ``high`` -> 0 and ``low`` -> 100.
    Useful for metrics where "lower is better" (e.g., debt ratios, churn).

    Edge cases:
    * NaN input -> 50.0 (neutral fallback).
    * Value outside ``[low, high]`` is clipped to the respective bound.

    Raises:
        ValueError: if ``low >= high``.
    """
    if low >= high:
        raise ValueError(f"low ({low}) must be strictly less than high ({high})")

    if _is_nan(value):
        return 50.0

    v = float(value)
    if v <= low:
        score = 0.0
    elif v >= high:
        score = 100.0
    else:
        score = (v - low) / (high - low) * 100.0

    if invert:
        score = 100.0 - score
    return float(score)


def peer_relative_percentile(
    value: float,
    peers: Sequence[float],
    *,
    include_self: bool = False,
) -> float:
    """Percentile rank of ``value`` against a peer set.

    Convenience wrapper around :func:`percentile_rank`.

    Parameters
    ----------
    value:
        The focal value.
    peers:
        The peer distribution. If ``include_self`` is False (default), ``peers``
        is treated as "everyone else" — the typical use case where a caller has
        already separated the focal entity from its peers and is asking "where
        do I rank among the others?".
    include_self:
        If True, append ``value`` to ``peers`` before percentile-ranking. Use
        this when ``peers`` already represents the full universe and you want
        the value's rank inside it.

    Notes
    -----
    NaN handling and the empty-distribution fallback follow
    :func:`percentile_rank`.
    """
    if include_self and not _is_nan(value):
        combined: List[float] = list(peers)
        combined.append(float(value))
        return percentile_rank(value, combined)
    return percentile_rank(value, peers)


def slope(series: Sequence[float]) -> Optional[float]:
    """Simple-linear-regression slope (``dy/dx``) over a 1-indexed x-axis.

    Returns the slope of the best-fit line through ``(1, series[0]),
    (2, series[1]), ...``, ignoring NaN entries (the corresponding x-values are
    dropped too, so gaps don't bias the fit).

    Returns:
        The slope as a Python ``float``, or ``None`` if fewer than 2 valid
        (non-NaN) points are available.

    Useful for trend-velocity signals such as Google Trends search-interest
    acceleration or rolling-revenue growth-rate momentum.
    """
    xs: List[float] = []
    ys: List[float] = []
    for i, v in enumerate(series, start=1):
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isnan(f):
            continue
        xs.append(float(i))
        ys.append(f)

    if len(ys) < 2:
        return None

    # Guard against a degenerate x-axis (shouldn't happen with 1-indexed ints,
    # but cheap insurance against future callers passing custom x's).
    if len(set(xs)) < 2:
        return None

    coeffs = np.polyfit(np.asarray(xs, dtype=float), np.asarray(ys, dtype=float), 1)
    return float(coeffs[0])
