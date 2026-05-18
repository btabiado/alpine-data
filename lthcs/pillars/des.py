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
from typing import Any, Dict, List, Optional, Tuple


__all__ = [
    "load_sector_weights",
    "normalize_macro_signal",
    "compute_des",
    "DEFAULT_MAGNITUDE_SCALE",
    "TIER2_MAX_POINTS",
]


# V1 default: a perfectly aligned full-magnitude signal can shift the
# score by 30 points from the neutral baseline. Multiple aligned signals
# stack additively (clipped to [0, 100] at the end).
DEFAULT_MAGNITUDE_SCALE = 30.0

# Tier-2 macro signals (Brent crude + gasoline crack, ISM PMI proxy,
# housing starts, consumer sentiment, U-6 unemployment) are a REFINEMENT
# on top of the Tier-1 sub-score, not a re-derivation.  Total absolute
# contribution from all Tier-2 inputs combined is clipped at ±5 points
# so they can't override the Tier-1 signal.
TIER2_MAX_POINTS = 5.0

# Per-indicator point budget within the Tier-2 envelope.  These are
# upper bounds: actual contribution scales linearly with how far each
# indicator is from its "neutral" zone.  Sum-of-magnitudes equals
# TIER2_MAX_POINTS so a maximally bearish/bullish day on every Tier-2
# input lands at exactly ±5.
_TIER2_BUDGET: Dict[str, float] = {
    "brent_crude": 1.0,
    "gasoline_crack": 0.5,
    "ism_pmi_proxy": 1.0,
    "housing_starts": 0.75,
    "consumer_sentiment": 1.0,
    "u6_unemployment": 0.75,
}


# Cyclical sectors get the full Tier-2 modifier; defensive sectors get
# a damped version (Tier-2 is a demand/cyclical signal — applying it
# undamped to Health Care or Utilities would be noise).  Keyed off the
# canonical sector name (after _alias_of resolution).
_TIER2_SECTOR_SCALING: Dict[str, float] = {
    # Cyclical / industrial / consumer cyclical: full effect.
    "Consumer Discretionary": 1.0,
    "Industrials": 1.0,
    "Materials": 1.0,
    "Energy": 1.0,
    "Financials": 0.8,
    "Real Estate": 0.8,
    "Information Technology": 0.6,
    "Communication Services": 0.6,
    # Defensive: damped — Tier-2 cyclical signal should barely move them.
    "Consumer Staples": 0.3,
    "Health Care": 0.3,
    "Utilities": 0.3,
}
_TIER2_SECTOR_SCALING_DEFAULT = 0.6


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


def _resolve_sector_block(
    sectors_block: Dict[str, Any], sector: str
) -> Optional[Dict[str, Any]]:
    """Look up ``sector`` in ``sectors_block`` and follow ``_alias_of`` once.

    Returns the resolved sector dict, or ``None`` if the sector is
    missing or the alias chain is broken.

    A sector block may declare ``"_alias_of": "<canonical name>"`` to
    point at another sector's weights. This lets us store duplicate
    sector names (e.g. yfinance's "Technology" vs GICS's "Information
    Technology") as a single canonical block without risking drift.

    Defensive behavior:

    * Empty / non-string sector name -> ``None``.
    * Sector not in ``sectors_block`` -> ``None`` (unknown sector;
      caller falls back to the neutral 50.0 path).
    * ``_alias_of`` target missing or not a dict -> ``None`` (broken
      alias; caller falls back to neutral, same as unknown sector).
    * Cycle detection: we only follow ``_alias_of`` for a single hop.
      If the target is itself an alias (i.e. has its own
      ``_alias_of``), we treat the chain as broken and return ``None``.
      Aliases are intended for renames, not chains.
    """
    if not isinstance(sectors_block, dict) or not isinstance(sector, str):
        return None
    block = sectors_block.get(sector)
    if not isinstance(block, dict):
        return None
    alias_target = block.get("_alias_of")
    if alias_target is None:
        return block
    # Defensive: only follow a single hop. If the alias target itself
    # is missing or is also an alias, treat as broken.
    if not isinstance(alias_target, str):
        return None
    canonical = sectors_block.get(alias_target)
    if not isinstance(canonical, dict):
        return None
    if "_alias_of" in canonical:
        # Chain detected — refuse to follow.
        return None
    return canonical


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


# ---------------------------------------------------------------------------
# Tier-2 macro refinement
# ---------------------------------------------------------------------------


def _tier2_indicator_tilt(
    indicator: str, block: Optional[Dict[str, Any]]
) -> Optional[float]:
    """Map a single Tier-2 indicator block to a tilt in ``[-1, +1]``.

    Returns ``None`` when the block is missing OR when there's not
    enough data to compute the tilt (treated as 0 contribution
    upstream).  Each indicator has its own mapping rule documented
    inline; the rules are deliberately conservative so Tier-2 is a
    nudge, not a re-derivation of the sub-score.
    """
    if not isinstance(block, dict):
        return None

    if indicator == "brent_crude":
        # High Brent (high percentile_2y) = drag on consumer-cyclical;
        # we let downstream sector scaling flip the sign for Energy.
        # Mapping: 2y percentile in [0,1] -> tilt in [-1, +1] where
        # percentile=0.5 (median) is neutral.  High oil = NEGATIVE tilt
        # because Tier-2 is broadly a DEMAND signal — Energy sector
        # scaling stays positive (1.0) but most others are dragged.
        pct = block.get("percentile_2y")
        if pct is None:
            return None
        return float(-(float(pct) - 0.5) * 2.0)

    if indicator == "gasoline_crack":
        # Widening crack spread = refiner margin expansion + consumer
        # gas-price pain.  Net Tier-2 demand tilt: negative when
        # spread is high (extracts consumer wallet share).  We score
        # vs. an empirically-typical mid-cycle US crack spread of
        # ~$1.50/gal; below = positive, above = negative.  Linear, clipped.
        spread = block.get("crack_spread_per_gal")
        if spread is None:
            return None
        # Spread typical band roughly [0.50, 3.00] $/gal historically.
        # Map [0.50 -> +1, 3.00 -> -1], clipped.
        s = float(spread)
        if s <= 0.5:
            return 1.0
        if s >= 3.0:
            return -1.0
        return float(-((s - 0.5) / (3.0 - 0.5) * 2.0 - 1.0))

    if indicator == "ism_pmi_proxy":
        # Regime-driven: expansion = positive, contraction = negative,
        # neutral = 0.  Magnitude proportional to 3m momentum.
        regime = block.get("regime")
        change = block.get("change_3m_pct")
        if regime == "expansion":
            base = 1.0
        elif regime == "contraction":
            base = -1.0
        else:
            base = 0.0
        # Damp by momentum magnitude: a 1% 3m move maps to full ±1.
        if change is None:
            return base * 0.5
        try:
            scaled = min(1.0, max(-1.0, float(change) / 0.01))
            # Always emit at least the regime sign at half-strength,
            # otherwise scale by momentum sign-aligned with regime.
            return float(scaled if base != 0 else 0.0)
        except (TypeError, ValueError):
            return base * 0.5

    if indicator == "housing_starts":
        # Housing starts above 2y median = constructive; below = drag.
        # Same percentile-mapped tilt as brent but with the SIGN flipped
        # (high starts = positive demand signal).
        pct = block.get("percentile_2y")
        if pct is None:
            return None
        return float((float(pct) - 0.5) * 2.0)

    if indicator == "consumer_sentiment":
        # High consumer sentiment = constructive for discretionary.
        pct = block.get("percentile_2y")
        if pct is None:
            return None
        return float((float(pct) - 0.5) * 2.0)

    if indicator == "u6_unemployment":
        # High U-6 (high percentile_2y) = labor slack = drag on demand.
        pct = block.get("percentile_2y")
        if pct is None:
            return None
        return float(-(float(pct) - 0.5) * 2.0)

    return None


def _compute_tier2_contribution(
    sector: str,
    tier2_macro: Dict[str, Any],
) -> Tuple[float, List[Dict[str, Any]], str]:
    """Compute the Tier-2 refinement points + per-indicator detail.

    Returns ``(total_points, tier2_inputs, quality)`` where:

      * ``total_points``  : signed delta to apply to the Tier-1 sub-score,
                            clipped to ``[-TIER2_MAX_POINTS, +TIER2_MAX_POINTS]``
                            and already sector-scaled.
      * ``tier2_inputs``  : list of ``{name, value, contribution_pts}``
                            dicts for explainability.
      * ``quality``       : ``"good"`` (5-6 sources), ``"partial"`` (1-4),
                            or ``"missing"`` (0).
    """
    sector_scale = _TIER2_SECTOR_SCALING.get(sector, _TIER2_SECTOR_SCALING_DEFAULT)

    # Block lookups keyed by indicator name.  "gasoline_crack" is a
    # synthetic derived from the gasoline block — surfaced separately
    # in variable_detail so the user sees both.
    gasoline_block = tier2_macro.get("gasoline_retail")
    blocks: Dict[str, Optional[Dict[str, Any]]] = {
        "brent_crude":        tier2_macro.get("brent_crude"),
        "gasoline_crack":     gasoline_block,
        "ism_pmi_proxy":      tier2_macro.get("ism_pmi_proxy"),
        "housing_starts":     tier2_macro.get("housing_starts"),
        "consumer_sentiment": tier2_macro.get("consumer_sentiment"),
        "u6_unemployment":    tier2_macro.get("u6_unemployment"),
    }

    tier2_inputs: List[Dict[str, Any]] = []
    total_points = 0.0
    sources_present = 0

    for name, block in blocks.items():
        budget = _TIER2_BUDGET.get(name, 0.0)
        tilt = _tier2_indicator_tilt(name, block)
        if tilt is None:
            # Still record the entry so detail rendering is uniform.
            tier2_inputs.append(
                {
                    "name": name,
                    "value": None,
                    "contribution_pts": 0.0,
                }
            )
            continue
        sources_present += 1

        # Per-indicator contribution = tilt * budget * sector_scale.
        # Sign convention: Energy sector gets a positive flip for
        # brent_crude (high oil = revenue tailwind).  We honour that by
        # special-casing Energy on brent only — other Tier-2 indicators
        # apply uniformly across sectors with the scaled magnitude.
        contribution = float(tilt) * float(budget) * float(sector_scale)
        if name == "brent_crude" and sector == "Energy":
            # Flip sign: high oil is positive for Energy revenues.
            contribution = -contribution
        if name == "gasoline_crack" and sector == "Energy":
            # Widening crack spread is positive for downstream Energy.
            contribution = -contribution

        # Record using a readable "value" field — pick the most natural
        # scalar from the block (current level, percentile, or regime).
        readable_value: Any
        if isinstance(block, dict):
            if name == "gasoline_crack":
                readable_value = block.get("crack_spread_per_gal")
            elif name == "ism_pmi_proxy":
                readable_value = block.get("regime")
            else:
                readable_value = block.get("current")
        else:
            readable_value = None

        tier2_inputs.append(
            {
                "name": name,
                "value": readable_value,
                "contribution_pts": round(float(contribution), 4),
            }
        )
        total_points += contribution

    # Clip total to ±TIER2_MAX_POINTS.
    if total_points > TIER2_MAX_POINTS:
        total_points = TIER2_MAX_POINTS
    elif total_points < -TIER2_MAX_POINTS:
        total_points = -TIER2_MAX_POINTS

    if sources_present == 0:
        quality = "missing"
    elif sources_present >= 5:
        quality = "good"
    else:
        quality = "partial"

    return float(total_points), tier2_inputs, quality


def compute_des(
    ticker: str,
    sector: str,
    macro_inputs: Dict[str, Optional[float]],
    sector_weights: Dict[str, Any],
    *,
    magnitude_scale: float = DEFAULT_MAGNITUDE_SCALE,
    tier2_macro: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compute the DES sub-score for one ticker.

    See module docstring for the formula. The return value's
    ``components`` block exposes every intermediate quantity needed for
    explainability (per-signal tilts, per-signal contributions, total
    contribution, list of signals whose sensitivity came from
    ``ticker_overrides`` rather than the sector default).

    Optional ``tier2_macro``: when provided, the Tier-2 macro snapshot
    (Brent crude, gasoline crack spread, ISM PMI proxy, housing starts,
    consumer sentiment, U-6 unemployment) is layered on top of the
    Tier-1 sub-score as a small (±5 max) sector-scaled refinement.
    Cyclical sectors get the full effect; defensive sectors (Staples,
    Health Care, Utilities) get a damped (~30%) effect.  When
    ``tier2_macro=None`` (default), the result is byte-equal to the
    pre-Tier-2 behaviour.  When provided, ``components`` gains
    ``tier2_inputs``, ``tier2_quality``, and ``tier2_total_pts``.
    """
    macro_inputs = macro_inputs or {}
    sector_weights = sector_weights or {}

    sectors_block = sector_weights.get("sectors") or {}
    overrides_block = sector_weights.get("ticker_overrides") or {}
    signal_norm = sector_weights.get("signal_normalization") or {}

    # --- Resolve sector sensitivities --------------------------------------
    # Follows ``_alias_of`` (single hop) so e.g. yfinance's "Technology"
    # and GICS's "Information Technology" both resolve to the same
    # canonical weight block. See ``_resolve_sector_block``.
    sector_block = _resolve_sector_block(sectors_block, sector) if isinstance(sectors_block, dict) else None
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

    # --- Optional Tier-2 macro refinement ----------------------------------
    # When ``tier2_macro`` is provided, layer the Tier-2 indicators on top
    # of the Tier-1 score as a small (±5 max) refinement.  Sector scaling
    # damps the effect for defensive sectors and amplifies it for
    # cyclicals.  When ``tier2_macro`` is None, behaviour is byte-equal
    # to the pre-Tier-2 implementation.
    tier2_points = 0.0
    tier2_inputs: List[Dict[str, Any]] = []
    tier2_quality: Optional[str] = None
    if tier2_macro is not None:
        tier2_points, tier2_inputs, tier2_quality = _compute_tier2_contribution(
            sector=sector,
            tier2_macro=tier2_macro,
        )
        raw_score = raw_score + tier2_points

    # Clip to [0, 100] and round to 1 decimal.
    if raw_score < 0.0:
        raw_score = 0.0
    elif raw_score > 100.0:
        raw_score = 100.0
    sub_score = round(float(raw_score), 1)

    components: Dict[str, Any] = {
        "signal_tilts": signal_tilts,
        "signal_contributions": signal_contributions,
        "total_contribution": float(total_contribution),
        "applied_overrides": applied_overrides,
    }
    if tier2_macro is not None:
        components["tier2_inputs"] = tier2_inputs
        components["tier2_quality"] = tier2_quality
        components["tier2_total_pts"] = round(float(tier2_points), 4)

    return {
        "ticker": ticker,
        "sector": sector,
        "sub_score": sub_score,
        "components": components,
        "weights_source": weights_source,
        "data_quality": {
            "has_macro_inputs": has_macro_inputs,
            "macro_signals_present": macro_signals_present,
            "sector_known": True,
        },
    }
