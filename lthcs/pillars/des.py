"""Demand Environment Score (DES) pillar.

Combines macro inputs (CPI, Fed Funds, 10Y yield, 30-day 10Y change, U-3
unemployment, WTI oil) with sector-specific sensitivities to produce a
0-100 sub-score per ticker.

Each macro signal is first mapped to a tilt in ``[-1, +1]`` via the
``signal_normalization`` bounds in ``sector_des_weights.json`` (linear
from ``low`` -> -1 to ``high`` -> +1, clipped). The tilt is then
multiplied by the sector's sensitivity for that signal (also in
``[-1, +1]``), which gives a per-signal contribution. The contributions
are summed and scaled by ``magnitude_scale`` points (default 30.0) from
the neutral baseline of 50.0, then clipped to ``[0, 100]``.

Ticker-level overrides may replace a sector's sensitivity on a
per-signal basis (i.e. an override does not have to be all-or-nothing).
This is the standard escape-hatch for industry-vs-sector mismatches
(e.g. EV automakers under Consumer Discretionary inherit a negative oil
tilt by default; the override flips that for TSLA / LCID).

The function is pure -- no I/O -- and never raises on missing data. A
missing macro input contributes 0 (neutral) tilt. An unknown sector
returns a flat 50.0 sub-score with ``data_quality.sector_known=False``
so downstream aggregation can apply a confidence haircut.

Tests for this module never touch the network or load the real config:
synthetic ``sector_weights`` dicts are passed in directly as fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional


__all__ = [
    "load_sector_weights",
    "normalize_macro_signal",
    "compute_des",
    "DEFAULT_MAGNITUDE_SCALE",
]


# V1 default: a perfectly aligned full-magnitude signal can shift the
# score by 30 points from the neutral baseline. Multiple aligned signals
# stack additively (clipped to [0, 100] at the end).
DEFAULT_MAGNITUDE_SCALE = 30.0


# Default repo-relative path to the config. Resolves to:
#   <repo>/data/lthcs/sector_des_weights.json
# (this file lives at <repo>/lthcs/pillars/des.py)
_DEFAULT_WEIGHTS_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "data"
    / "lthcs"
    / "sector_des_weights.json"
)


def load_sector_weights(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load ``sector_des_weights.json`` from the default location or ``path``.

    Returns the parsed JSON dict as-is. The caller passes the dict to
    :func:`compute_des`.

    Default path resolves relative to the repo root:
    ``data/lthcs/sector_des_weights.json``.
    """
    target = Path(path) if path is not None else _DEFAULT_WEIGHTS_PATH
    with open(target, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _safe_float(x: Any) -> Optional[float]:
    """Coerce ``x`` to a finite Python float, or return None."""
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    # NaN / inf -> None (treat as missing).
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def normalize_macro_signal(
    signal_name: str,
    raw_value: Optional[float],
    signal_normalization: Dict[str, Any],
) -> float:
    """Map a raw macro value to a tilt in ``[-1, +1]``.

    Linear map between ``signal_normalization[signal_name]['low']``
    (-> -1) and ``signal_normalization[signal_name]['high']`` (-> +1),
    clipped at the bounds.

    ``None`` raw value or an unknown signal name returns ``0.0``
    (neutral).
    """
    value = _safe_float(raw_value)
    if value is None:
        return 0.0

    bounds = signal_normalization.get(signal_name) if signal_normalization else None
    if not isinstance(bounds, dict):
        return 0.0

    low = _safe_float(bounds.get("low"))
    high = _safe_float(bounds.get("high"))
    if low is None or high is None or low >= high:
        return 0.0

    if value <= low:
        return -1.0
    if value >= high:
        return 1.0
    # Linear: low -> -1, high -> +1, midpoint -> 0.
    span = high - low
    return float((value - low) / span * 2.0 - 1.0)


def _sector_sensitivities(sector_block: Dict[str, Any]) -> Dict[str, float]:
    """Extract numeric per-signal sensitivities from a sector dict.

    Keys beginning with ``_`` (``_alias_of``, ``_comment``, ``_note``,
    ...) are metadata and are silently skipped. Non-numeric values are
    also skipped.
    """
    out: Dict[str, float] = {}
    if not isinstance(sector_block, dict):
        return out
    for k, v in sector_block.items():
        if not isinstance(k, str) or k.startswith("_"):
            continue
        f = _safe_float(v)
        if f is None:
            continue
        out[k] = f
    return out


def _ticker_override_sensitivities(
    ticker_block: Dict[str, Any],
) -> Dict[str, float]:
    """Same as :func:`_sector_sensitivities` but for the ticker_overrides block."""
    return _sector_sensitivities(ticker_block)


def compute_des(
    ticker: str,
    sector: str,
    macro_inputs: Dict[str, Optional[float]],
    sector_weights: Dict[str, Any],
    *,
    magnitude_scale: float = DEFAULT_MAGNITUDE_SCALE,
) -> Dict[str, Any]:
    """Compute the DES sub-score for one ticker.

    See module docstring for the formula. The return value's
    ``components`` block exposes every intermediate quantity needed for
    explainability (per-signal tilts, per-signal contributions, total
    contribution, list of signals whose sensitivity came from
    ``ticker_overrides`` rather than the sector default).
    """
    macro_inputs = macro_inputs or {}
    sector_weights = sector_weights or {}

    sectors_block = sector_weights.get("sectors") or {}
    overrides_block = sector_weights.get("ticker_overrides") or {}
    signal_norm = sector_weights.get("signal_normalization") or {}

    # --- Resolve sector sensitivities --------------------------------------
    sector_block = sectors_block.get(sector) if isinstance(sectors_block, dict) else None
    sector_known = isinstance(sector_block, dict)

    # Count non-None macro inputs for data-quality reporting (independent
    # of whether we'll actually use them).
    macro_signals_present = sum(
        1 for v in macro_inputs.values() if _safe_float(v) is not None
    )
    has_macro_inputs = macro_signals_present > 0

    if not sector_known:
        return {
            "ticker": ticker,
            "sector": sector,
            "sub_score": 50.0,
            "components": {
                "signal_tilts": {},
                "signal_contributions": {},
                "total_contribution": 0.0,
                "applied_overrides": [],
            },
            "weights_source": "sector_missing",
            "data_quality": {
                "has_macro_inputs": has_macro_inputs,
                "macro_signals_present": macro_signals_present,
                "sector_known": False,
            },
        }

    base_sensitivities = _sector_sensitivities(sector_block)

    # --- Apply ticker overrides on a per-signal basis ----------------------
    ticker_block = overrides_block.get(ticker) if isinstance(overrides_block, dict) else None
    applied_overrides: List[str] = []
    sensitivities: Dict[str, float] = dict(base_sensitivities)
    if isinstance(ticker_block, dict):
        for sig, override_val in _ticker_override_sensitivities(ticker_block).items():
            sensitivities[sig] = override_val
            applied_overrides.append(sig)
        applied_overrides.sort()

    weights_source = (
        "ticker_overrides_partial" if applied_overrides else "sector"
    )

    # --- Compute per-signal tilts and contributions ------------------------
    signal_tilts: Dict[str, float] = {}
    signal_contributions: Dict[str, float] = {}
    total_contribution = 0.0

    for sig, sensitivity in sensitivities.items():
        raw = macro_inputs.get(sig)
        tilt = normalize_macro_signal(sig, raw, signal_norm)
        signal_tilts[sig] = float(tilt)
        contribution = float(sensitivity) * float(tilt)
        signal_contributions[sig] = float(contribution)
        total_contribution += contribution

    raw_score = 50.0 + total_contribution * float(magnitude_scale)
    # Clip to [0, 100] and round to 1 decimal.
    if raw_score < 0.0:
        raw_score = 0.0
    elif raw_score > 100.0:
        raw_score = 100.0
    sub_score = round(float(raw_score), 1)

    return {
        "ticker": ticker,
        "sector": sector,
        "sub_score": sub_score,
        "components": {
            "signal_tilts": signal_tilts,
            "signal_contributions": signal_contributions,
            "total_contribution": float(total_contribution),
            "applied_overrides": applied_overrides,
        },
        "weights_source": weights_source,
        "data_quality": {
            "has_macro_inputs": has_macro_inputs,
            "macro_signals_present": macro_signals_present,
            "sector_known": True,
        },
    }
