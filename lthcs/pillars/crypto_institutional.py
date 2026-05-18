"""Crypto Institutional Confidence pillar.

Three signals combined into a 0-100 sub-score:

* **Whale cohort accumulation Δ30d** (50%, BTC only) -- the change in
  aggregate balance held by addresses with >=1k BTC (the standard
  "whale" cohort). Sourced from the existing ``whale.json`` distribution
  buckets. ETH/SOL have no equivalent free-tier cohort series, so they
  zero out this component and the rest are renormalized.
* **ETF net inflows / outflows (trailing 30d)** (30%) -- sum of daily
  net flows across all spot ETFs for the asset. BTC + ETH have full
  Farside CSV coverage in the repo; SOL has no ETF product so this
  component also drops.
* **Price momentum (30d)** (20%) -- the CoinGecko-reported 30-day
  percent return. A weak proxy for "institutional flow demand" but
  reliable and free. Functions as a fallback when the on-chain whale
  cohort and ETF flow data are both unavailable.

The pillar always returns a usable sub_score: when any combination of
components is missing it proportionally renormalizes the available
weights. If every component is missing, the score collapses to the
neutral 50.0 midpoint.

All math is pure.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from lthcs.normalize import bounded_linear
from lthcs.sources.crypto_data import compute_etf_flow_30d


# Sub-component weights (sum to 1.0).
WHALE_COHORT_WEIGHT = 0.50
ETF_FLOW_WEIGHT = 0.30
MOMENTUM_WEIGHT = 0.20

# Whale cohort Δ30d: -1% to +1% maps to 0-100 (matches the V1
# Whale Sentiment Index saturation point for "whale supply Δ30d").
_WHALE_LOW_PCT = -1.0
_WHALE_HIGH_PCT = 1.0

# ETF flow: -$3B to +$3B (USD millions: -3000 to +3000) trailing 30d.
# BTC has had +/-$2.5B months historically, so 3B is a reasonable saturation.
_ETF_LOW_USDM = -3000.0
_ETF_HIGH_USDM = 3000.0

# 30d price momentum: -30% to +30% saturates the score.
_MOM_LOW = -30.0
_MOM_HIGH = 30.0

_NEUTRAL = 50.0

# Whale-cohort buckets used by fetch_market.py's BTC distribution series.
# Sum across these for "whale supply" (addresses holding >= 1K BTC).
_WHALE_BUCKETS = ("b1k_10k", "b10k_100k", "b100k_1m")


def _whale_supply(row: Dict[str, Any]) -> Optional[float]:
    """Sum the >=1K BTC cohort balances for a distribution row."""
    if not isinstance(row, dict):
        return None
    total = 0.0
    any_present = False
    for k in _WHALE_BUCKETS:
        v = row.get(k)
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        total += f
        any_present = True
    if not any_present:
        return None
    return total


def compute_whale_cohort_pct_30d(
    distribution: List[Dict[str, Any]],
) -> Optional[float]:
    """Return the Δ30d percent change in BTC whale-cohort supply.

    Needs at least 31 distribution rows. Returns None otherwise (the
    pillar will renormalize away the missing component).
    """
    if not distribution or len(distribution) < 31:
        return None
    now = _whale_supply(distribution[-1])
    then = _whale_supply(distribution[-31])
    if now is None or then is None or then <= 0:
        return None
    return float((now - then) / then * 100.0)


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


def compute_crypto_institutional(
    symbol: str,
    inputs: Dict[str, Any],
) -> Dict[str, Any]:
    """Compute the Crypto Institutional Confidence sub-score.

    See module docstring for the component breakdown.
    """
    sym = (symbol or "").upper().strip() or "UNKNOWN"

    # --- Whale cohort (BTC only) ----------------------------------------
    whale_pct = None
    if sym == "BTC":
        whale_pct = compute_whale_cohort_pct_30d(inputs.get("distribution_series") or [])
    whale_score = (
        float(bounded_linear(whale_pct, _WHALE_LOW_PCT, _WHALE_HIGH_PCT))
        if whale_pct is not None else None
    )

    # --- ETF flows -----------------------------------------------------
    etf_30d = compute_etf_flow_30d(inputs.get("etf_flow_rows") or [])
    etf_score = (
        float(bounded_linear(etf_30d, _ETF_LOW_USDM, _ETF_HIGH_USDM))
        if etf_30d is not None else None
    )

    # --- Price momentum ------------------------------------------------
    market = inputs.get("market") or {}
    mom_pct = market.get("price_change_pct_30d")
    try:
        mom_pct_f: Optional[float] = float(mom_pct) if mom_pct is not None else None
        if mom_pct_f is not None and (mom_pct_f != mom_pct_f):  # NaN
            mom_pct_f = None
    except (TypeError, ValueError):
        mom_pct_f = None
    mom_score = (
        float(bounded_linear(mom_pct_f, _MOM_LOW, _MOM_HIGH))
        if mom_pct_f is not None else None
    )

    documented_weights = {
        "whale": WHALE_COHORT_WEIGHT,
        "etf": ETF_FLOW_WEIGHT,
        "momentum": MOMENTUM_WEIGHT,
    }
    component_scores = {
        "whale": whale_score,
        "etf": etf_score,
        "momentum": mom_score,
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
        "whale_cohort_pct_30d": whale_pct,
        "whale_subscore": whale_score,
        "etf_flow_30d_usd_m": etf_30d,
        "etf_subscore": etf_score,
        "price_change_pct_30d": mom_pct_f,
        "momentum_subscore": mom_score,
    }

    return {
        "ticker": sym,
        "sub_score": sub_score,
        "components": variable_detail,
        "variable_detail": variable_detail,
        "weights": documented_weights,
        "effective_weights": eff,
        "data_quality": {
            "has_whale_cohort": whale_score is not None,
            "has_etf_flow": etf_score is not None,
            "has_price_momentum": mom_score is not None,
        },
    }
