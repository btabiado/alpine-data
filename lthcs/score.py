"""LTHCS final-score combiner.

Combines the five pillar sub-scores into the composite LTHCS_v1 score per
``PHASE_1_BUILD_SPEC.md`` Section 5::

    LTHCS_v1 = (Adoption Momentum       x weight)
             + (Institutional Confidence x weight)
             + (Financial Evolution      x weight)
             + (Thesis Integrity         x weight)
             + (DES                      x weight)
             + Macro Adjustments
             + Sector Adjustments
             + Volatility Modifiers
             , capped to [0, 100]

Pillar weights come from ``data/lthcs/weights.json`` keyed by the
maturity stage of the focal ticker. Modifiers:

* **Macro adjustment** -- if the trailing 30-day change in the US 10Y
  Treasury yield exceeds +25bp, subtract 2.0 (higher rates pressure
  long-duration equities). If it's below -25bp, add 2.0. Otherwise 0.0.
* **Sector adjustment** -- V1 stub returning 0.0. A future Phase 2
  caller can supply a real value via ``sector_adjustment_override``
  without breaking the API.
* **Volatility modifier** -- if the focal ticker's trailing 30-day
  realised volatility is above the 90th percentile of the supplied
  universe distribution, subtract 3.0.

The composite is capped to ``[0, 100]`` and rounded to 1 decimal.
The corresponding band is looked up from ``weights_config["score_bands"]``.

All functions are pure (no I/O).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

_LOG = logging.getLogger(__name__)


# Order of pillars in the weights vector. Must match ``weights.json``.
# Maps data_quality_flags emitted by the daily pipeline to the pillar they
# render unavailable. When a flag is present in `data_quality_flags`, that
# pillar's documented weight is REDISTRIBUTED across the remaining pillars
# (proportional renormalization) rather than letting the pillar's neutral-50
# placeholder dilute the composite. Without this, a ticker with great real
# data on 4 pillars and a stubbed Thesis caps at ~78 (the neutral-50
# placeholder consumes 20% of the score regardless of conviction).
#
# Why "thesis_unavailable" but NOT "trends_unavailable":
# - thesis_unavailable means the ENTIRE Thesis pillar is stubbed → drop it.
# - trends_unavailable means a SUB-COMPONENT inside the Adoption pillar
#   is stubbed; the pillar already renormalizes that internally
#   (lthcs/pillars/adoption.py). Adoption's pillar-level score is real,
#   so we keep its weight.
_FLAGS_TO_DROPPED_PILLAR = {
    "thesis_unavailable": "thesis_integrity",
}

PILLAR_ORDER = (
    "adoption_momentum",
    "institutional_confidence",
    "financial_evolution",
    "thesis_integrity",
    "des",
)


# --- Constants --------------------------------------------------------------

_MACRO_THRESHOLD_BP = 25.0
_MACRO_MAGNITUDE = 2.0
_VOLATILITY_PERCENTILE = 90.0
_VOLATILITY_MAGNITUDE = -3.0

# Default volatility modifier configuration. Used as a fallback when
# ``weights.json`` lacks a ``modifiers.volatility_modifier`` block or that
# block is malformed. Keeping a code-level default ensures a typo in JSON
# cannot silently disable risk management.
_VOLATILITY_DEFAULT_METRIC = "trailing_30d_volatility_percentile"
_VOLATILITY_DEFAULT_OP = ">"

_DRIFT_WINDOWS = ("1d", "7d", "30d", "90d")

# Supported comparison operators for modifier trigger expressions.
_TRIGGER_OPS = {
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    ">":  lambda a, b: a > b,
    "<":  lambda a, b: a < b,
}


def _parse_trigger_expression(expr: str) -> Optional[Tuple[str, str, float]]:
    """Parse a trigger expression like ``"metric_name > 90"``.

    Returns ``(metric_name, operator, threshold)`` on success, or ``None``
    if the expression cannot be parsed. Supports ``>``, ``>=``, ``<``,
    ``<=``. The metric name may not contain whitespace.
    """
    if not isinstance(expr, str):
        return None
    s = expr.strip()
    if not s:
        return None
    # Check two-char operators first so '>=' isn't tokenized as '>'.
    for op in (">=", "<=", ">", "<"):
        idx = s.find(op)
        if idx <= 0:  # operator must come AFTER some metric text
            continue
        metric = s[:idx].strip()
        rhs = s[idx + len(op):].strip()
        if not metric or any(ch.isspace() for ch in metric):
            return None
        try:
            threshold = float(rhs)
        except (TypeError, ValueError):
            return None
        if threshold != threshold:  # NaN
            return None
        return (metric, op, threshold)
    return None


def _load_volatility_modifier_config(
    weights_config: Optional[Dict[str, Any]],
) -> Tuple[float, str, float]:
    """Load (percentile_threshold, operator, magnitude) for the vol modifier.

    Reads ``weights_config['modifiers']['volatility_modifier']`` and
    falls back to the documented defaults (percentile=90, op='>',
    magnitude=-3.0) when the block is absent, has unsupported
    ``applies_to``, or has a malformed trigger / magnitude.
    """
    defaults = (_VOLATILITY_PERCENTILE, _VOLATILITY_DEFAULT_OP, _VOLATILITY_MAGNITUDE)
    if not isinstance(weights_config, dict):
        return defaults
    modifiers = weights_config.get("modifiers")
    if not isinstance(modifiers, dict):
        return defaults
    spec = modifiers.get("volatility_modifier")
    if not isinstance(spec, dict):
        return defaults

    applies_to = spec.get("applies_to", "all_tickers")
    if applies_to != "all_tickers":
        _LOG.warning(
            "volatility_modifier.applies_to=%r is not supported; "
            "skipping modifier (falling back to defaults).",
            applies_to,
        )
        return defaults

    parsed = _parse_trigger_expression(spec.get("trigger", ""))
    if parsed is None:
        _LOG.warning(
            "volatility_modifier.trigger=%r is malformed; "
            "falling back to defaults (%s %s %s).",
            spec.get("trigger"),
            _VOLATILITY_DEFAULT_METRIC,
            _VOLATILITY_DEFAULT_OP,
            _VOLATILITY_PERCENTILE,
        )
        return defaults
    metric, op, threshold = parsed
    if metric != _VOLATILITY_DEFAULT_METRIC:
        _LOG.warning(
            "volatility_modifier.trigger metric=%r is not recognized "
            "(expected %r); falling back to defaults.",
            metric,
            _VOLATILITY_DEFAULT_METRIC,
        )
        return defaults
    if op not in _TRIGGER_OPS:
        _LOG.warning(
            "volatility_modifier.trigger operator=%r not supported; "
            "falling back to defaults.",
            op,
        )
        return defaults

    try:
        magnitude = float(spec["magnitude"])
    except (KeyError, TypeError, ValueError):
        _LOG.warning(
            "volatility_modifier.magnitude=%r is malformed; "
            "falling back to default %r.",
            spec.get("magnitude"),
            _VOLATILITY_MAGNITUDE,
        )
        return defaults
    if magnitude != magnitude:  # NaN
        _LOG.warning(
            "volatility_modifier.magnitude is NaN; falling back to default."
        )
        return defaults

    return (threshold, op, magnitude)


# --- Pillar weights ---------------------------------------------------------

def get_maturity_weights(maturity_stage: str, weights_config: Dict[str, Any]) -> List[float]:
    """Return the 5-element pillar-weight vector for a maturity stage.

    Vector order matches :data:`PILLAR_ORDER`. Raises ``ValueError`` if
    the stage is not present in ``weights_config['profiles']``.
    """
    profiles = (weights_config or {}).get("profiles") or {}
    if maturity_stage not in profiles:
        raise ValueError(
            "Unknown maturity_stage %r (known: %s)"
            % (maturity_stage, sorted(profiles.keys()))
        )
    vec = profiles[maturity_stage]
    if not isinstance(vec, (list, tuple)) or len(vec) != len(PILLAR_ORDER):
        raise ValueError(
            "weights profile for %r must be a %d-element list, got %r"
            % (maturity_stage, len(PILLAR_ORDER), vec)
        )
    return [float(x) for x in vec]


# --- Modifiers --------------------------------------------------------------

def compute_macro_adjustment(ten_y_30d_change_bp: Optional[float]) -> float:
    """Macro adjustment based on 30-day US 10Y Treasury change.

    Strictly greater than +25bp -> -2.0; strictly less than -25bp -> +2.0;
    otherwise (including exactly +/-25bp and ``None``) -> 0.0.
    """
    if ten_y_30d_change_bp is None:
        return 0.0
    try:
        bp = float(ten_y_30d_change_bp)
    except (TypeError, ValueError):
        return 0.0
    if bp != bp:  # NaN
        return 0.0
    if bp > _MACRO_THRESHOLD_BP:
        return -_MACRO_MAGNITUDE
    if bp < -_MACRO_THRESHOLD_BP:
        return _MACRO_MAGNITUDE
    return 0.0


def _percentile(values: List[float], pct: float) -> Optional[float]:
    """Linear-interpolation percentile (numpy-style), pure-Python.

    Returns ``None`` for an empty list.
    """
    cleaned: List[float] = []
    for v in values:
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f != f:  # NaN
            continue
        cleaned.append(f)
    if not cleaned:
        return None
    cleaned.sort()
    if len(cleaned) == 1:
        return cleaned[0]
    rank = (pct / 100.0) * (len(cleaned) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(cleaned) - 1)
    frac = rank - lo
    return cleaned[lo] + (cleaned[hi] - cleaned[lo]) * frac


def compute_volatility_modifier(
    ticker_volatility: Optional[float],
    universe_volatilities: List[float],
    weights_config: Optional[Dict[str, Any]] = None,
) -> float:
    """Return the volatility-modifier magnitude when the ticker's volatility
    crosses the configured universe percentile.

    The percentile threshold, comparison operator, and magnitude are read
    from ``weights_config['modifiers']['volatility_modifier']`` (parsed via
    :func:`_load_volatility_modifier_config`). If ``weights_config`` is
    omitted, ``None``, or the block is absent/malformed, the documented
    defaults (90th percentile, strict ``>``, ``-3.0``) are used.

    ``None`` ticker volatility or empty universe both return ``0.0``.
    """
    if ticker_volatility is None:
        return 0.0
    try:
        ticker_v = float(ticker_volatility)
    except (TypeError, ValueError):
        return 0.0
    if ticker_v != ticker_v:  # NaN
        return 0.0
    if not universe_volatilities:
        return 0.0

    percentile, op, magnitude = _load_volatility_modifier_config(weights_config)
    threshold = _percentile(list(universe_volatilities), percentile)
    if threshold is None:
        return 0.0
    cmp = _TRIGGER_OPS.get(op)
    if cmp is None:  # Defensive — config loader already filtered.
        cmp = _TRIGGER_OPS[_VOLATILITY_DEFAULT_OP]
    if cmp(ticker_v, threshold):
        return magnitude
    return 0.0


# --- Banding ----------------------------------------------------------------

def assign_band(score: float, score_bands: Dict[str, Dict[str, Any]]) -> str:
    """Return the band key covering ``score``.

    Bands in weights.json are INTEGER-bounded (e.g. constructive=70..79,
    high_confidence=80..89) but composite scores carry one decimal place,
    so fractional scores like 79.4 sit in a gap between bands and would
    otherwise be wrongly assigned. We treat each band as covering the
    half-open interval ``[min, max+1)`` so a score of 79.4 lands in
    constructive (since 70 ≤ 79.4 < 80), 89.99 lands in high_confidence,
    100.0 lands in elite. Equivalent to flooring ``score`` to its integer
    part before doing the inclusive lookup.

    Out-of-range inputs are clamped to [0, 100] before lookup.
    """
    import math

    try:
        s = float(score)
    except (TypeError, ValueError):
        s = 0.0
    if s != s:  # NaN
        s = 0.0
    s = max(0.0, min(100.0, s))
    floored = int(math.floor(s))
    # Special-case the top: a perfect 100.0 floors to 100, which fits
    # elite's max=100 inclusive check below — no extra handling needed.
    for name, spec in (score_bands or {}).items():
        try:
            lo = int(float(spec["min"]))
            hi = int(float(spec["max"]))
        except (KeyError, TypeError, ValueError):
            continue
        if lo <= floored <= hi:
            return name
    # Fallback: return the band whose min is closest below the score.
    # (Defensive — the standard config tiles [0,100] without gaps.)
    best_name: Optional[str] = None
    best_lo = float("-inf")
    for name, spec in (score_bands or {}).items():
        try:
            lo = float(spec["min"])
        except (KeyError, TypeError, ValueError):
            continue
        if lo <= s and lo > best_lo:
            best_lo = lo
            best_name = name
    return best_name or "review"


# --- Drift ------------------------------------------------------------------

def compute_drift(
    current_score: float,
    prior_scores: Dict[str, Optional[float]],
) -> Dict[str, float]:
    """Compute drift = current - prior for each window.

    Windows are ``1d``, ``7d``, ``30d``, ``90d``. Missing or ``None`` priors
    yield 0.0 for that window. Result keys are ``drift_<window>``.
    """
    try:
        cur = float(current_score)
    except (TypeError, ValueError):
        cur = 0.0
    if cur != cur:
        cur = 0.0
    priors = prior_scores or {}
    out: Dict[str, float] = {}
    for win in _DRIFT_WINDOWS:
        prev = priors.get(win)
        if prev is None:
            out["drift_" + win] = 0.0
            continue
        try:
            pv = float(prev)
        except (TypeError, ValueError):
            out["drift_" + win] = 0.0
            continue
        if pv != pv:
            out["drift_" + win] = 0.0
            continue
        out["drift_" + win] = round(cur - pv, 1)
    return out


# --- Confidence -------------------------------------------------------------

def _confidence_from_flags(data_quality_flags: Optional[List[str]]) -> str:
    """Map count of upstream data-quality flags to a confidence bucket.

    0 flags -> ``high``, 1-2 flags -> ``medium``, 3+ flags -> ``low``.
    This is the V1 proxy described in the spec: a pillar that fell back
    to neutral 50 due to missing data still emits a float, so flag count
    is the only signal the combiner can use.
    """
    n = len(data_quality_flags or [])
    if n == 0:
        return "high"
    if n <= 2:
        return "medium"
    return "low"


# --- Composite --------------------------------------------------------------

def compute_lthcs_score(
    ticker: str,
    sector: str,
    maturity_stage: str,
    pillar_subscores: Dict[str, float],
    weights_config: Dict[str, Any],
    *,
    ten_y_30d_change_bp: Optional[float] = None,
    ticker_volatility: Optional[float] = None,
    universe_volatilities: Optional[List[float]] = None,
    sector_adjustment_override: Optional[float] = None,
    prior_scores: Optional[Dict[str, Optional[float]]] = None,
    data_quality_flags: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Combine the 5 pillar sub-scores into a final LTHCS_v1 snapshot row.

    See module docstring + ``PHASE_1_BUILD_SPEC.md`` Section 7 for the
    full return-shape contract. All inputs except ``ticker``, ``sector``,
    ``maturity_stage``, ``pillar_subscores``, ``weights_config`` are
    optional / default to neutral.
    """
    if not isinstance(pillar_subscores, dict):
        raise TypeError("pillar_subscores must be a dict, got %r" % type(pillar_subscores).__name__)
    missing = [p for p in PILLAR_ORDER if p not in pillar_subscores]
    if missing:
        raise ValueError("pillar_subscores missing required keys: %s" % missing)

    documented_weights = get_maturity_weights(maturity_stage, weights_config)

    # Renormalize away stubbed pillars driven by data_quality_flags. A ticker
    # whose Thesis sentiment hasn't been scored yet (rotation hasn't reached
    # it on the AV free-tier budget) shouldn't carry a 50.0 placeholder at
    # full pillar weight — that mechanically caps composites at ~78.
    # Proportionally redistribute the dropped pillars' weight across the
    # remaining ones so they still sum to 1.0.
    flags_set = set(data_quality_flags or [])
    dropped_pillars = {
        _FLAGS_TO_DROPPED_PILLAR[f]
        for f in flags_set
        if f in _FLAGS_TO_DROPPED_PILLAR
    }
    effective_weights: List[float] = []
    if dropped_pillars and len(dropped_pillars) < len(PILLAR_ORDER):
        # Build the proportional renormalization.
        retained = [
            (w, n) for w, n in zip(documented_weights, PILLAR_ORDER)
            if n not in dropped_pillars
        ]
        retained_sum = sum(w for w, _ in retained) or 1.0
        for w, name in zip(documented_weights, PILLAR_ORDER):
            if name in dropped_pillars:
                effective_weights.append(0.0)
            else:
                effective_weights.append(float(w) / retained_sum)
    else:
        effective_weights = [float(w) for w in documented_weights]

    weighted_components: List[float] = []
    weighted_sum = 0.0
    for w, name in zip(effective_weights, PILLAR_ORDER):
        sub = float(pillar_subscores[name])
        contrib = w * sub
        weighted_components.append(float(contrib))
        weighted_sum += contrib

    macro_adj = compute_macro_adjustment(ten_y_30d_change_bp)
    sector_adj = (
        float(sector_adjustment_override) if sector_adjustment_override is not None else 0.0
    )
    vol_mod = compute_volatility_modifier(
        ticker_volatility,
        list(universe_volatilities or []),
        weights_config=weights_config,
    )

    raw = weighted_sum + macro_adj + sector_adj + vol_mod
    final = max(0.0, min(100.0, raw))
    final = round(float(final), 1)

    band = assign_band(final, weights_config.get("score_bands", {}))
    drift = compute_drift(final, prior_scores or {})

    confidence_level = _confidence_from_flags(data_quality_flags)

    subscores_out: Dict[str, float] = {}
    for name in PILLAR_ORDER:
        subscores_out[name] = float(pillar_subscores[name])

    return {
        "ticker": ticker,
        "lthcs_score": final,
        "band": band,
        "drift_1d": drift["drift_1d"],
        "drift_7d": drift["drift_7d"],
        "drift_30d": drift["drift_30d"],
        "drift_90d": drift["drift_90d"],
        "confidence_level": confidence_level,
        "data_quality_flags": list(data_quality_flags or []),
        "subscores": subscores_out,
        "modifiers": {
            "macro_adj": float(macro_adj),
            "sector_adj": float(sector_adj),
            "volatility_mod": float(vol_mod),
        },
        "maturity_stage": maturity_stage,
        "weights_used": [float(w) for w in documented_weights],
        "effective_weights": effective_weights,
        "dropped_pillars": sorted(dropped_pillars),
        "weighted_components": weighted_components,
        "sector": sector,
    }
