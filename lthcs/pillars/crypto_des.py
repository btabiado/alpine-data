"""Crypto Demand Environment Score (DES) pillar.

Two signal layers combined into a 0-100 sub-score per crypto asset:

* **Stablecoin supply Δ30d** (50%) -- aggregate stablecoin market cap
  change over 30 days. Growing stablecoin supply = dry powder waiting
  to be deployed into crypto-risk = positive demand environment.
  Shrinking supply = capital exiting the system. Source: DeFiLlama
  ``/stablecoins`` (free, no key).
* **Exchange reserves Δ30d** (20%) -- BTC reserves on exchanges. Falling
  reserves = supply moving to cold storage (holders accumulating) =
  positive DES. Rising reserves = supply moving to exchanges for sale.
  V1 stubs this: a free per-exchange total isn't reliably available,
  so the pillar accepts the value as an optional input
  (``exchange_reserves_pct_30d``). When absent, the weight redistributes.
* **Macro overlay (broad-market regime)** (30%) -- crypto trades like
  a risk asset; we tilt the score by the same broad-market signals
  the equity DES uses but with crypto-appropriate sensitivities.
  Inputs (all optional): ``hy_oas`` (HY credit spread, %), ``vix``
  (VIX index), ``ten_y_30d_change_bp`` (already used in the equity
  macro modifier). Each is mapped to a -1..+1 tilt and summed with a
  small magnitude (capped to +/-25 points off the neutral 50).

The pillar always returns a usable score; missing components are
renormalized away. With ALL components missing the score is the
neutral 50.0 midpoint.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from lthcs.normalize import bounded_linear


# Sub-component weights (sum to 1.0).
STABLECOIN_WEIGHT = 0.50
EXCHANGE_RESERVES_WEIGHT = 0.20
MACRO_OVERLAY_WEIGHT = 0.30

# Stablecoin supply Δ30d: -10% to +10%.
_STABLE_LOW = -10.0
_STABLE_HIGH = 10.0

# Exchange reserves Δ30d: positive (rising) is BAD (supply moving to
# sell). invert=True so falling reserves -> high score.
_RESERVES_LOW = -10.0
_RESERVES_HIGH = 10.0

# Macro overlay: each signal contributes a per-signal tilt in [-1, +1].
# The composite tilt is multiplied by MAGNITUDE_SCALE to shift the score
# from the neutral 50.0 midpoint. The result is clamped to [0, 100].
MACRO_MAGNITUDE_SCALE = 25.0

# Per-signal bounds for the [low -> -1, high -> +1] linear map.
# Calibrated for "tighter conditions" -> negative tilt for crypto.
_HY_OAS_LOW = 2.0    # tight credit -> +1 tilt (good for crypto)
_HY_OAS_HIGH = 8.0   # stressed credit -> -1 tilt
_VIX_LOW = 12.0      # calm market -> +1 tilt
_VIX_HIGH = 35.0     # stressed market -> -1 tilt
_10Y_30D_LOW = -50.0  # falling yields -> +1 tilt
_10Y_30D_HIGH = 50.0  # rising yields -> -1 tilt

_NEUTRAL = 50.0


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


def _signal_tilt(value: Optional[float], low: float, high: float) -> Optional[float]:
    """Map a raw value onto [-1, +1] via a linear `low -> +1`, `high -> -1`
    interpretation (i.e. higher = WORSE for crypto)."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v:
        return None
    if low >= high:
        return None
    if v <= low:
        tilt = 1.0
    elif v >= high:
        tilt = -1.0
    else:
        tilt = 1.0 - 2.0 * (v - low) / (high - low)
    return float(tilt)


def _macro_overlay_score(inputs: Dict[str, Any]) -> Optional[float]:
    """Compute the macro overlay component (or None when fully missing).

    Inputs (all optional):
        hy_oas (percent), vix, ten_y_30d_change_bp.
    """
    hy_tilt = _signal_tilt(inputs.get("hy_oas"), _HY_OAS_LOW, _HY_OAS_HIGH)
    vix_tilt = _signal_tilt(inputs.get("vix"), _VIX_LOW, _VIX_HIGH)
    ten_y_tilt = _signal_tilt(
        inputs.get("ten_y_30d_change_bp"), _10Y_30D_LOW, _10Y_30D_HIGH
    )
    tilts = [t for t in (hy_tilt, vix_tilt, ten_y_tilt) if t is not None]
    if not tilts:
        return None
    avg_tilt = sum(tilts) / len(tilts)
    score = 50.0 + MACRO_MAGNITUDE_SCALE * avg_tilt
    return float(max(0.0, min(100.0, score)))


def compute_crypto_des(
    symbol: str,
    inputs: Dict[str, Any],
) -> Dict[str, Any]:
    """Compute the Crypto DES sub-score.

    Reads from ``inputs``:

    * ``stablecoins``: ``{"now": float, "delta_30d_pct": float|None}``
      (populated by ``CryptoDataAdapter.stablecoins``).
    * ``exchange_reserves_pct_30d`` (optional): % change in exchange
      reserves over 30d. None -> component drops.
    * ``hy_oas``, ``vix``, ``ten_y_30d_change_bp``: optional macro
      overlay inputs. Any present contribute to the macro-overlay
      component.
    """
    sym = (symbol or "").upper().strip() or "UNKNOWN"

    stable = inputs.get("stablecoins") or {}
    stable_delta = stable.get("delta_30d_pct")
    try:
        stable_f: Optional[float] = (
            float(stable_delta) if stable_delta is not None else None
        )
        if stable_f is not None and stable_f != stable_f:
            stable_f = None
    except (TypeError, ValueError):
        stable_f = None
    stable_score = (
        float(bounded_linear(stable_f, _STABLE_LOW, _STABLE_HIGH))
        if stable_f is not None else None
    )

    reserves_pct = inputs.get("exchange_reserves_pct_30d")
    try:
        reserves_f: Optional[float] = (
            float(reserves_pct) if reserves_pct is not None else None
        )
        if reserves_f is not None and reserves_f != reserves_f:
            reserves_f = None
    except (TypeError, ValueError):
        reserves_f = None
    reserves_score = (
        float(bounded_linear(reserves_f, _RESERVES_LOW, _RESERVES_HIGH, invert=True))
        if reserves_f is not None else None
    )

    macro_score = _macro_overlay_score(inputs)

    documented_weights = {
        "stablecoin": STABLECOIN_WEIGHT,
        "reserves": EXCHANGE_RESERVES_WEIGHT,
        "macro": MACRO_OVERLAY_WEIGHT,
    }
    component_scores = {
        "stablecoin": stable_score,
        "reserves": reserves_score,
        "macro": macro_score,
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
        "stablecoin_delta_30d_pct": stable_f,
        "stablecoin_subscore": stable_score,
        "exchange_reserves_pct_30d": reserves_f,
        "reserves_subscore": reserves_score,
        "macro_overlay_subscore": macro_score,
        "macro_inputs": {
            "hy_oas": inputs.get("hy_oas"),
            "vix": inputs.get("vix"),
            "ten_y_30d_change_bp": inputs.get("ten_y_30d_change_bp"),
        },
    }

    return {
        "ticker": sym,
        "sub_score": sub_score,
        "components": variable_detail,
        "variable_detail": variable_detail,
        "weights": documented_weights,
        "effective_weights": eff,
        "data_quality": {
            "has_stablecoin": stable_score is not None,
            "has_exchange_reserves": reserves_score is not None,
            "has_macro_overlay": macro_score is not None,
        },
    }
