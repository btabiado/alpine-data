"""Institutional Confidence pillar.

Combines two signals into a 0-100 sub-score per ticker:

* **Trailing 90-day price momentum** (Yahoo Finance via
  :mod:`lthcs.sources.yahoo`) -- scored as the peer-relative percentile
  of the focal ticker's 90-day return within the universe.
* **13F institutional ownership change** (SEC EDGAR Form 13F) --
  quarter-over-quarter delta in institutional float ownership. **V1
  stubs this**: the SEC's 13F endpoints require aggregating across
  thousands of institutional filers per ticker, which is out of scope
  for the Phase 1 build. The pillar function accepts an optional
  ``inst_holdings_change_qoq`` parameter so a future Phase 2 wire-in is
  a single-line change at the caller.

The two components are combined with a fixed 70/30 weight
(momentum / 13F) per ``PHASE_1_BUILD_SPEC.md`` Section 5 -- 13F is
quarterly and slow, momentum is the live signal so it carries the
larger share. When the 13F component is missing (the V1 default),
weights are **renormalized** so momentum carries the full 100%
rather than being diluted toward the neutral 50.0 midpoint --
otherwise V1 scores would be artificially compressed toward the
middle of the scale.

**V1 simplification note.** The spec says momentum is ranked "vs S&P
500 universe". V1 ranks against the 75-ticker LTHCS universe instead
-- close enough in spirit and avoids a second fetch path / second
rate-limit budget against Yahoo. The pipeline caller pre-fetches
``get_momentum_pct`` for every LTHCS ticker and passes the resulting
``peer_momentums`` map into this pillar.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from lthcs.normalize import (
    bounded_linear,
    peer_relative_percentile,
)


# --- Constants --------------------------------------------------------------

# Spec: V1 combines momentum and 13F 70/30. Momentum gets the larger
# share because it updates daily; 13F only refreshes quarterly.
MOMENTUM_WEIGHT = 0.70
INST_HOLDINGS_WEIGHT = 0.30

# Bounds for mapping 13F QoQ ownership change onto 0-100 via
# ``bounded_linear``. The suggested input scale is "percent of float"
# (e.g. 0.05 == +5pp of institutional float QoQ). +/-5pp swings are
# already very large at the institutional-ownership level, so these
# bounds give a useful spread without being trivially saturated.
_INST_CHANGE_LOW = -0.05
_INST_CHANGE_HIGH = 0.05

# Neutral midpoint of the 0-100 scale. Used when a component is missing.
_NEUTRAL = 50.0


# --- Public API: momentum sub-score ----------------------------------------

def compute_momentum_subscore(
    ticker: str,
    momentum_pct: Optional[float],
    peer_momentums: Dict[str, Optional[float]],
) -> float:
    """Return the 0-100 sub-score for the momentum component.

    Parameters
    ----------
    ticker:
        Focal ticker. Excluded from the peer distribution so a value
        does not rank itself.
    momentum_pct:
        Trailing-90d return as a decimal (e.g. ``0.12`` == +12%).
        ``None`` -> returns 50.0 (neutral).
    peer_momentums:
        ``{peer_ticker -> momentum_or_None}``. May include the focal
        ticker; it will be filtered out. ``None`` values are filtered
        out (peer has no usable momentum data).
    """
    if momentum_pct is None:
        return _NEUTRAL

    try:
        focal = float(momentum_pct)
    except (TypeError, ValueError):
        return _NEUTRAL
    # NaN check.
    if focal != focal:
        return _NEUTRAL

    peer_values: List[float] = []
    for sym, m in (peer_momentums or {}).items():
        if sym == ticker:
            continue
        if m is None:
            continue
        try:
            f = float(m)
        except (TypeError, ValueError):
            continue
        if f != f:  # NaN
            continue
        peer_values.append(f)

    return float(peer_relative_percentile(focal, peer_values, include_self=False))


# --- Public API: 13F sub-score ---------------------------------------------

def compute_inst_holdings_subscore(
    change_qoq: Optional[float],
) -> float:
    """Return the 0-100 sub-score for the 13F-ownership-change component.

    Parameters
    ----------
    change_qoq:
        QoQ change in institutional float ownership as a decimal
        (e.g. ``0.05`` == +5 percentage points). ``None`` -> returns
        50.0 (neutral placeholder; V1 always passes None).
    """
    if change_qoq is None:
        return _NEUTRAL
    try:
        v = float(change_qoq)
    except (TypeError, ValueError):
        return _NEUTRAL
    if v != v:  # NaN
        return _NEUTRAL
    return float(bounded_linear(v, _INST_CHANGE_LOW, _INST_CHANGE_HIGH))


# --- Public API: pillar entry point ----------------------------------------

def compute_institutional(
    ticker: str,
    momentum_pct: Optional[float],
    peer_momentums: Dict[str, Optional[float]],
    *,
    inst_holdings_change_qoq: Optional[float] = None,
) -> Dict[str, Any]:
    """Compute the Institutional Confidence sub-score for one ticker.

    Combines:

    * Momentum percentile within the peer universe (70% weight).
    * 13F institutional-ownership-change mapped to 0-100 (30% weight).

    When the 13F component is missing (the V1 default), the weights
    are renormalized so momentum carries the full 100%. The 13F
    component is still surfaced in ``components`` as the neutral
    placeholder 50.0, but its effective weight is 0.

    Parameters
    ----------
    ticker:
        Subject ticker.
    momentum_pct:
        Trailing-90d return as a decimal (e.g. ``0.12`` == +12%), or
        ``None`` if Yahoo lookup failed for this ticker.
    peer_momentums:
        ``{peer_ticker -> momentum_or_None}`` INCLUDING this ticker
        (the focal will be filtered out of its own ranking).
    inst_holdings_change_qoq:
        V1 always ``None``. Phase 2 will pass a real value (suggested
        scale: percent of float, e.g. ``0.05`` == +5%).
    """
    has_momentum = momentum_pct is not None
    has_inst = inst_holdings_change_qoq is not None

    momentum_subscore = compute_momentum_subscore(
        ticker, momentum_pct, peer_momentums or {}
    )
    inst_subscore = compute_inst_holdings_subscore(inst_holdings_change_qoq)

    # Weight handling: when 13F is missing, renormalize so momentum
    # carries the full 100%. This keeps V1 scores from being
    # artificially compressed toward 50.
    if has_inst:
        eff_momentum_w = MOMENTUM_WEIGHT
        eff_inst_w = INST_HOLDINGS_WEIGHT
    else:
        eff_momentum_w = 1.0
        eff_inst_w = 0.0

    sub_score = eff_momentum_w * momentum_subscore + eff_inst_w * inst_subscore
    sub_score = round(float(sub_score), 1)

    return {
        "ticker": ticker,
        "sub_score": sub_score,
        "components": {
            "momentum_pct_90d": (
                float(momentum_pct) if has_momentum else None
            ),
            "momentum_subscore": float(momentum_subscore),
            "inst_holdings_change_qoq": (
                float(inst_holdings_change_qoq) if has_inst else None
            ),
            "inst_holdings_subscore": float(inst_subscore),
        },
        "weights": {
            "momentum": MOMENTUM_WEIGHT,
            "inst_holdings": INST_HOLDINGS_WEIGHT,
        },
        "effective_weights": {
            "momentum": float(eff_momentum_w),
            "inst_holdings": float(eff_inst_w),
        },
        "data_quality": {
            "has_momentum": has_momentum,
            "has_inst_holdings": has_inst,
        },
    }
