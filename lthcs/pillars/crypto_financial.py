"""Crypto Financial Evolution pillar.

Three signals combined into a 0-100 sub-score per crypto asset:

* **Network revenue Δ30d** (40%) -- miners' revenue for BTC, transaction
  fees for ETH/SOL (proxied via tx_volume_usd when explicit fee data
  isn't in the whale.json payload). The free-tier proxy is the
  ``miners_revenue_usd`` field that ``fetch_market.py`` already pulls
  from blockchain.info.
* **Realized cap growth** (30%) -- proxy: 30d % change in market cap.
  Realized cap is a paid Glassnode metric, so we use the public
  market-cap series from CoinGecko as a substitute. This conflates
  realized cap with the implicit "willing to pay" capital, but the
  trend direction is similar in V1.
* **Stock-to-flow stability (BTC) / supply growth stability (ETH/SOL)**
  (30%) -- the V1 free proxy is the asset's monthly supply inflation
  rate. BTC has a fixed +0.83%/year emission and a score near 100;
  ETH after the merge has near-zero net issuance and also scores high.
  SOL has higher inflation (~5-7%) and scores lower.

The pillar function always returns a usable score: when a component is
missing, weights are proportionally renormalized.

All math is pure.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from lthcs.normalize import bounded_linear
from lthcs.sources.crypto_data import pct_change_30d


# Sub-component weights (sum to 1.0).
REVENUE_WEIGHT = 0.40
REALIZED_CAP_WEIGHT = 0.30
SUPPLY_STABILITY_WEIGHT = 0.30

# Network revenue Δ30d: -40% to +40% saturates the score.
_REVENUE_LOW = -40.0
_REVENUE_HIGH = 40.0

# Realized cap proxy (30d market cap change): -30% to +30%.
_REALIZED_LOW = -30.0
_REALIZED_HIGH = 30.0

# Supply inflation: lower is better. Bounds in percent per year.
# +0%: peak score (no dilution). +10%: floor.
_SUPPLY_LOW = 0.0
_SUPPLY_HIGH = 10.0

_NEUTRAL = 50.0


# V1 default annual supply-inflation estimates for each asset. These are
# slow-moving (BTC halves every 4 years, ETH stays near zero post-merge,
# SOL has a deterministic disinflationary schedule). Override per-asset
# by passing ``supply_inflation_pct_yr`` in the inputs dict.
# Tier 5 #27 Phase 5 extends this to the 10-asset universe. Numbers are
# steady-state estimates published by each chain's tokenomics docs;
# refreshed annually rather than fetched live (the signal is too slow
# to justify a daily API call).
_DEFAULT_SUPPLY_INFLATION = {
    "BTC": 0.83,   # ~0.83% / year post-2024 halving
    "ETH": 0.10,   # near zero net issuance post-merge (varies)
    "SOL": 5.5,    # ~5.5% currently, disinflating to 1.5% over time
    "ADA": 1.5,    # tail emission ~1.5%/yr, capped at 45B
    "AVAX": 4.5,   # ~4.5%/yr, deflationary burns offset some
    "DOT": 7.5,    # ~7.5%/yr inflation (NPoS rewards)
    "LINK": 0.0,   # fixed 1B max supply, no inflation
    "POL": 2.0,    # 2%/yr by Polygon 2.0 tokenomics (1% staking + 1% community)
    "XRP": 0.0,    # fixed 100B supply, no new issuance (escrow releases offset by burn)
    "DOGE": 3.5,   # ~5B/yr fixed -> ~3.5%/yr at current supply (~145B)
}


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


def compute_crypto_financial(
    symbol: str,
    inputs: Dict[str, Any],
) -> Dict[str, Any]:
    """Compute the Crypto Financial Evolution sub-score.

    See module docstring for the component breakdown. ``inputs`` is the
    per-asset dict from
    :meth:`lthcs.sources.crypto_data.CryptoDataAdapter.inputs_for`.
    """
    sym = (symbol or "").upper().strip() or "UNKNOWN"

    # --- Network revenue Δ30d -------------------------------------------
    # BTC: miners_revenue_usd_series. ETH/SOL: tx_volume_usd_series as
    # proxy (fee revenue scales with on-chain volume; without explicit
    # gas-fee data this is the cleanest free proxy).
    if sym == "BTC":
        rev_pct = pct_change_30d(inputs.get("miners_revenue_usd_series") or [])
        revenue_label = "miners_revenue_pct_30d"
    else:
        rev_pct = pct_change_30d(inputs.get("tx_volume_usd_series") or [])
        revenue_label = "tx_volume_proxy_pct_30d"
    revenue_score = (
        float(bounded_linear(rev_pct, _REVENUE_LOW, _REVENUE_HIGH))
        if rev_pct is not None else None
    )

    # --- Realized cap proxy (30d market cap pct change) -----------------
    # CoinGecko gives us 30d price change; that approximates market-cap
    # change to first order (supply moves slowly).
    market = inputs.get("market") or {}
    cap_change_pct = market.get("price_change_pct_30d")
    try:
        cap_change_f: Optional[float] = (
            float(cap_change_pct) if cap_change_pct is not None else None
        )
        if cap_change_f is not None and cap_change_f != cap_change_f:
            cap_change_f = None
    except (TypeError, ValueError):
        cap_change_f = None
    realized_score = (
        float(bounded_linear(cap_change_f, _REALIZED_LOW, _REALIZED_HIGH))
        if cap_change_f is not None else None
    )

    # --- Supply inflation (lower is better) -----------------------------
    inflation_pct = inputs.get("supply_inflation_pct_yr")
    if inflation_pct is None:
        inflation_pct = _DEFAULT_SUPPLY_INFLATION.get(sym)
    try:
        inflation_f: Optional[float] = (
            float(inflation_pct) if inflation_pct is not None else None
        )
        if inflation_f is not None and inflation_f != inflation_f:
            inflation_f = None
    except (TypeError, ValueError):
        inflation_f = None
    supply_score = (
        float(bounded_linear(inflation_f, _SUPPLY_LOW, _SUPPLY_HIGH, invert=True))
        if inflation_f is not None else None
    )

    documented_weights = {
        "revenue": REVENUE_WEIGHT,
        "realized": REALIZED_CAP_WEIGHT,
        "supply": SUPPLY_STABILITY_WEIGHT,
    }
    component_scores = {
        "revenue": revenue_score,
        "realized": realized_score,
        "supply": supply_score,
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
        revenue_label: rev_pct,
        "revenue_subscore": revenue_score,
        "market_cap_proxy_pct_30d": cap_change_f,
        "realized_subscore": realized_score,
        "supply_inflation_pct_yr": inflation_f,
        "supply_subscore": supply_score,
    }

    return {
        "ticker": sym,
        "sub_score": sub_score,
        "components": variable_detail,
        "variable_detail": variable_detail,
        "weights": documented_weights,
        "effective_weights": eff,
        "data_quality": {
            "has_revenue": revenue_score is not None,
            "has_realized_cap": realized_score is not None,
            "has_supply_inflation": supply_score is not None,
        },
    }
