"""Crypto Thesis Integrity pillar.

Score the "regime narrative" health of a crypto asset on 0-100.

Equities Thesis Integrity blends Alpha Vantage news sentiment with
analyst-rotation signals. The crypto version reuses two market-
structure signals plus a placeholder for narrative sentiment:

* **Funding rate normalcy** (50%) -- the perpetual-swap funding rate
  is a leading indicator of leverage build-up. A near-zero rate is
  "healthy" (score 100). Persistently positive (>0.05%/8h) means euphoric
  longs are paying carry (score drops). Persistently negative means
  panic shorts (also drops). Symmetric around zero.
* **Long/Short ratio normalcy** (30%) -- similar shape: a ratio near
  1.0 is healthy; values >1.5 (over-long) or <0.7 (over-short) drop
  the score.
* **Narrative sentiment** (20%) -- placeholder in V1. The crypto
  dashboard's news/sentiment fields aren't wired into a single
  per-asset sentiment number, so this defaults to neutral 50.0 with a
  data_quality flag. Future Phase 2 can plug AV NEWS_SENTIMENT for
  crypto tickers (CRYPTO:BTC) here.

When funding-rate / L-S-ratio data isn't passed in (the V1 default --
the V1 dashboard reads funding rate per-coin but we don't yet
persist a per-asset value the runner can read), the pillar drops those
components and falls back to the narrative sentiment, which itself
collapses to neutral. The whole pillar then returns 50.0 with a
``thesis_unavailable``-style data_quality flag.

All math is pure.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


# Sub-component weights (sum to 1.0).
FUNDING_WEIGHT = 0.50
LONG_SHORT_WEIGHT = 0.30
SENTIMENT_WEIGHT = 0.20

# Funding rate is reported as percent per 8h. Normal: |r| < 0.01%.
# Euphoric: r > 0.05%. Panic: r < -0.05%. Score is symmetric around 0.
_FUNDING_HEALTHY_THRESHOLD = 0.01
_FUNDING_EXTREME_THRESHOLD = 0.10

# Long/short ratio: 1.0 is balanced.
_LS_HEALTHY = 1.0
_LS_EXTREME_HIGH = 1.8
_LS_EXTREME_LOW = 0.55  # ~ 1/1.8

_NEUTRAL = 50.0


def _funding_score(funding_rate_pct_8h: Optional[float]) -> Optional[float]:
    """Map a funding rate (percent per 8h) to a 0-100 health score.

    * |r| <= healthy_threshold -> 100 (no excess leverage either side)
    * |r| >= extreme_threshold -> 0
    * In between: linear interpolation.

    Sign is symmetric -- the pillar penalises persistent over-longs and
    persistent over-shorts equally; both indicate fragile positioning.
    """
    if funding_rate_pct_8h is None:
        return None
    try:
        r = float(funding_rate_pct_8h)
    except (TypeError, ValueError):
        return None
    if r != r:  # NaN
        return None
    mag = abs(r)
    if mag <= _FUNDING_HEALTHY_THRESHOLD:
        return 100.0
    if mag >= _FUNDING_EXTREME_THRESHOLD:
        return 0.0
    # Linear between thresholds.
    span = _FUNDING_EXTREME_THRESHOLD - _FUNDING_HEALTHY_THRESHOLD
    return float(100.0 * (1.0 - (mag - _FUNDING_HEALTHY_THRESHOLD) / span))


def _long_short_score(long_short_ratio: Optional[float]) -> Optional[float]:
    """Map a long/short ratio to a 0-100 health score (1.0 = best)."""
    if long_short_ratio is None:
        return None
    try:
        r = float(long_short_ratio)
    except (TypeError, ValueError):
        return None
    if r != r or r <= 0:
        return None
    if r >= _LS_EXTREME_HIGH:
        return 0.0
    if r <= _LS_EXTREME_LOW:
        return 0.0
    # Convert to symmetric distance from 1.0 in log-ratio space so 1.5
    # and 0.667 (its reciprocal) score equally.
    import math
    log_r = math.log(r)
    # log(1.8) ~= 0.588, log(0.555) ~= -0.588
    extreme_log = math.log(_LS_EXTREME_HIGH)
    health = 1.0 - (abs(log_r) / extreme_log)
    return float(max(0.0, min(1.0, health)) * 100.0)


def _renormalize(scores: Dict[str, Optional[float]],
                 weights: Dict[str, float]) -> Dict[str, float]:
    available = [k for k, v in scores.items() if v is not None]
    if not available:
        return {k: 0.0 for k in weights}
    denom = sum(weights[k] for k in available) or 1.0
    return {
        k: (float(weights[k]) / denom if k in available else 0.0)
        for k in weights
    }


def compute_crypto_thesis(
    symbol: str,
    inputs: Dict[str, Any],
) -> Dict[str, Any]:
    """Compute the Crypto Thesis Integrity sub-score.

    Reads (optional) fields from ``inputs``:

    * ``funding_rate_pct_8h``: average perpetual-swap funding rate, %
      per 8h. Sign and magnitude both matter.
    * ``long_short_ratio``: top-trader L/S ratio (1.0 = balanced).
    * ``narrative_sentiment``: free-form -1..+1 narrative sentiment
      score. Not currently wired in V1; left as a Phase 2 hook.

    All three are optional -- missing values collapse to None and the
    pillar renormalizes around what's present. With nothing present,
    the score is the neutral 50.0 midpoint.
    """
    sym = (symbol or "").upper().strip() or "UNKNOWN"

    funding_score = _funding_score(inputs.get("funding_rate_pct_8h"))
    ls_score = _long_short_score(inputs.get("long_short_ratio"))

    # Narrative sentiment. Optional -1..+1 -> 0..100.
    sentiment_raw = inputs.get("narrative_sentiment")
    sentiment_score: Optional[float]
    try:
        s_f: Optional[float] = (
            float(sentiment_raw) if sentiment_raw is not None else None
        )
        if s_f is not None and s_f != s_f:
            s_f = None
    except (TypeError, ValueError):
        s_f = None
    if s_f is None:
        sentiment_score = None
    else:
        sentiment_score = float(50.0 + 50.0 * max(-1.0, min(1.0, s_f)))

    documented_weights = {
        "funding": FUNDING_WEIGHT,
        "ls": LONG_SHORT_WEIGHT,
        "sentiment": SENTIMENT_WEIGHT,
    }
    component_scores = {
        "funding": funding_score,
        "ls": ls_score,
        "sentiment": sentiment_score,
    }
    eff = _renormalize(component_scores, documented_weights)

    if all(v == 0.0 for v in eff.values()):
        sub_score = _NEUTRAL
    else:
        sub_score = sum(
            eff[k] * (component_scores[k] if component_scores[k] is not None else 0.0)
            for k in documented_weights
        )
    sub_score = round(float(sub_score), 1)

    variable_detail = {
        "funding_rate_pct_8h": inputs.get("funding_rate_pct_8h"),
        "funding_subscore": funding_score,
        "long_short_ratio": inputs.get("long_short_ratio"),
        "ls_subscore": ls_score,
        "narrative_sentiment": s_f,
        "sentiment_subscore": sentiment_score,
    }

    return {
        "ticker": sym,
        "sub_score": sub_score,
        "components": variable_detail,
        "variable_detail": variable_detail,
        "weights": documented_weights,
        "effective_weights": eff,
        "data_quality": {
            "has_funding": funding_score is not None,
            "has_long_short": ls_score is not None,
            "has_sentiment": sentiment_score is not None,
        },
    }
