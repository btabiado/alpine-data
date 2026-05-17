"""Institutional Confidence pillar.

Combines three signals into a 0-100 sub-score per ticker:

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
* **SEC Form 4 insider conviction** (90-day open-market window) --
  an additive points-based adjustment to the base subscore, gated on
  the conviction-regime and cluster-buying flags produced by
  :mod:`lthcs.sources.sec_form4`. Raw conviction scores saturate fast
  and dollar flow is heavily biased toward megacap sales, so the
  Form 4 agent recommends gating on |conviction_score| >= 0.2 or
  ``cluster_buying == True`` and treating the *regime label* as the
  primary signal. The adjustment is asymmetric (favor buying over
  selling) and capped to ``[-5, +10]`` to keep one noisy quarter from
  swinging the pillar more than the underlying momentum.

The two base components (momentum / 13F) are combined with a fixed
70/30 weight per ``PHASE_1_BUILD_SPEC.md`` Section 5 -- 13F is
quarterly and slow, momentum is the live signal so it carries the
larger share. When the 13F component is missing (the V1 default),
weights are **renormalized** so momentum carries the full 100%
rather than being diluted toward the neutral 50.0 midpoint --
otherwise V1 scores would be artificially compressed toward the
middle of the scale. The Form 4 adjustment is applied *after* the
weighted base subscore is computed; missing insider data zeros the
adjustment (no penalty for absent data — momentum still drives the
score).

**V1 simplification note.** The spec says momentum is ranked "vs S&P
500 universe". V1 ranks against the 75-ticker LTHCS universe instead
-- close enough in spirit and avoids a second fetch path / second
rate-limit budget against Yahoo. The pipeline caller pre-fetches
``get_momentum_pct`` for every LTHCS ticker and passes the resulting
``peer_momentums`` map into this pillar.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

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

# --- Insider (Form 4) adjustment constants ---------------------------------
#
# Per the sec_form4 source agent's recommendation: |conviction_score| > 0.5
# is "strong", |conviction_score| < 0.15 is noise. Raw dollar flow is biased
# heavily toward selling for any megacap (every CEO/CFO sells ~$5M+/quarter
# under 10b5-1 plans), so the *regime label* and the cluster-buying flag
# carry more signal than the raw score. The adjustment is asymmetric: buying
# signals get more weight than selling signals because the megacap default is
# "heavy_selling" by construction.
_INSIDER_STRONG_THRESHOLD = 0.5
_INSIDER_MILD_THRESHOLD = 0.2

_INSIDER_PTS_CLUSTER_BUYING = 8.0  # strongest single signal
_INSIDER_PTS_STRONG_BUYING = 5.0
_INSIDER_PTS_MILD_BUYING = 3.0
_INSIDER_PTS_MILD_SELLING = -1.0
_INSIDER_PTS_HEAVY_SELLING = -3.0  # asymmetric vs strong buying

_INSIDER_PTS_CEO_CFO_BUYING = 2.0
_INSIDER_PTS_CEO_CFO_SELLING = -1.0

# Final adjustment is capped to [-5, +10] -- asymmetric, again favoring
# buying signals over selling signals because of the megacap-selling bias.
_INSIDER_ADJ_FLOOR = -5.0
_INSIDER_ADJ_CEIL = 10.0


# --- Holdings (13F) adjustment constants -----------------------------------
#
# The sec_13f source returns a per-ticker dict with ``conviction_signal``
# (accumulating / steady / distributing / mixed) and a ``signal_score`` in
# [-1, +1] derived from (net_buyers - net_sellers) / manager_count across
# the 20 tracked institutional managers. The adjustment mirrors the
# insider asymmetric design but with smaller magnitudes (institutional
# rebalancing is a lower-conviction signal than direct insider activity):
#
# accumulating + signal_score >  +0.5  -> +5
# accumulating + +0.3 <= score <= +0.5 -> +3
# steady / mixed                       -> 0
# distributing + -0.5 <= score <= -0.3 -> -2
# distributing + signal_score <  -0.5  -> -3
# data_quality == "sparse" (no signal) -> 0
#
# Missing or no-holdings ticker -> 0 (no penalty).
_HOLDINGS_STRONG_THRESHOLD = 0.5
_HOLDINGS_MILD_THRESHOLD = 0.3

_HOLDINGS_PTS_STRONG_ACCUMULATING = 5.0
_HOLDINGS_PTS_MILD_ACCUMULATING = 3.0
_HOLDINGS_PTS_MILD_DISTRIBUTING = -2.0
_HOLDINGS_PTS_STRONG_DISTRIBUTING = -3.0

# COMBINED insider + holdings adjustment cap. The insider-alone cap was
# [-5, +10]; we expand to [-7, +12] to allow the additive holdings signal
# to push through without compressing the insider signal away.
_COMBINED_ADJ_FLOOR = -7.0
_COMBINED_ADJ_CEIL = 12.0


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


# --- Insider (Form 4) adjustment -------------------------------------------

def _apply_insider_adjustment(
    base_subscore: float,
    insider_data: Optional[Dict[str, Any]],
) -> Tuple[float, Dict[str, Any]]:
    """Apply a points-based insider-conviction adjustment to ``base_subscore``.

    Returns ``(adjusted_subscore, component_detail)`` where ``component_detail``
    is the dict surfaced under ``components.insider`` for the snapshot's
    variable_detail row.

    Adjustment rules (additive on top of base, capped to ``[-5, +10]``):

    =====================================================================  ========
    Signal                                                                  Points
    =====================================================================  ========
    ``cluster_buying == True``                                              +8
    ``conviction_score >= +0.5``  (strong_buying regime)                    +5
    ``conviction_score >= +0.2``  (mild_buying regime)                      +3
    ``conviction_score <= -0.2``  (mild_selling)                            -1
    ``conviction_score <= -0.5``  (heavy_selling -- megacap default)        -3
    ``ceo_cfo_action == "buying"``  (overlay, additive)                     +2
    ``ceo_cfo_action == "selling"`` (overlay, additive)                     -1
    =====================================================================  ========

    Missing insider data zeros the adjustment (no penalty for absent data).
    Each conviction bracket is mutually exclusive; CEO/CFO is an overlay
    that stacks on top. Result is clamped to ``[-5, +10]``.
    """
    detail: Dict[str, Any] = {
        "regime": None,
        "conviction_score": None,
        "cluster_buying": None,
        "ceo_cfo_action": None,
        "adjustment_pts": 0.0,
    }
    if not insider_data:
        return float(base_subscore), detail

    # Extract raw fields, defending against bad types.
    regime = insider_data.get("regime")
    cluster_buying = bool(insider_data.get("cluster_buying"))
    ceo_cfo_action = insider_data.get("ceo_cfo_action")

    raw_conv = insider_data.get("conviction_score")
    try:
        conv: Optional[float] = (
            None if raw_conv is None else float(raw_conv)
        )
    except (TypeError, ValueError):
        conv = None
    if conv is not None and conv != conv:  # NaN
        conv = None

    detail["regime"] = regime
    detail["conviction_score"] = conv
    detail["cluster_buying"] = cluster_buying
    detail["ceo_cfo_action"] = ceo_cfo_action

    adj = 0.0

    # Mutually-exclusive conviction bracket. cluster_buying takes priority --
    # it is the strongest single signal in the module per the source agent.
    if cluster_buying:
        adj += _INSIDER_PTS_CLUSTER_BUYING
    elif conv is not None:
        if conv >= _INSIDER_STRONG_THRESHOLD:
            adj += _INSIDER_PTS_STRONG_BUYING
        elif conv >= _INSIDER_MILD_THRESHOLD:
            adj += _INSIDER_PTS_MILD_BUYING
        elif conv <= -_INSIDER_STRONG_THRESHOLD:
            adj += _INSIDER_PTS_HEAVY_SELLING
        elif conv <= -_INSIDER_MILD_THRESHOLD:
            adj += _INSIDER_PTS_MILD_SELLING
        # else: noise band -- no contribution.

    # CEO/CFO overlay -- additive on top of the conviction bracket.
    if ceo_cfo_action == "buying":
        adj += _INSIDER_PTS_CEO_CFO_BUYING
    elif ceo_cfo_action == "selling":
        adj += _INSIDER_PTS_CEO_CFO_SELLING

    # Asymmetric cap.
    if adj > _INSIDER_ADJ_CEIL:
        adj = _INSIDER_ADJ_CEIL
    elif adj < _INSIDER_ADJ_FLOOR:
        adj = _INSIDER_ADJ_FLOOR

    detail["adjustment_pts"] = float(adj)
    return float(base_subscore) + float(adj), detail


# --- Holdings (13F) adjustment ---------------------------------------------

def _apply_holdings_adjustment(
    holdings_data: Optional[Dict[str, Any]],
) -> Tuple[float, Dict[str, Any]]:
    """Compute the points contribution from the 13F-holdings signal.

    Returns ``(adjustment_pts, component_detail)``. The detail dict is
    surfaced under ``components.holdings`` for the snapshot's
    variable_detail row consumption.

    Rules (see module-level constants for thresholds):

    ============================================  ========
    Signal                                         Points
    ============================================  ========
    accumulating + signal_score >  +0.5            +5
    accumulating + +0.3 <= signal_score <= +0.5    +3
    steady / mixed / no data                       0
    distributing + -0.5 <= signal_score <= -0.3    -2
    distributing + signal_score <  -0.5            -3
    ============================================  ========

    Sparse data_quality (manager_count < 5) zeros the adjustment so
    obscure tickers don't get a synthetic boost / penalty from a noisy
    one-or-two manager signal.
    """
    detail: Dict[str, Any] = {
        "conviction_signal": None,
        "signal_score": None,
        "manager_count": None,
        "data_quality": None,
        "share_change_pct": None,
        "net_buyers": None,
        "net_sellers": None,
        "adjustment_pts": 0.0,
    }
    if not holdings_data:
        return 0.0, detail

    signal_label = holdings_data.get("conviction_signal")
    raw_score = holdings_data.get("signal_score")
    try:
        score: Optional[float] = (
            None if raw_score is None else float(raw_score)
        )
    except (TypeError, ValueError):
        score = None
    if score is not None and score != score:  # NaN
        score = None

    manager_count_raw = holdings_data.get("manager_count")
    try:
        manager_count: Optional[int] = (
            None if manager_count_raw is None else int(manager_count_raw)
        )
    except (TypeError, ValueError):
        manager_count = None

    data_quality = holdings_data.get("data_quality")

    qoq = holdings_data.get("quarter_over_quarter") or {}
    detail["conviction_signal"] = signal_label
    detail["signal_score"] = score
    detail["manager_count"] = manager_count
    detail["data_quality"] = data_quality
    detail["share_change_pct"] = qoq.get("share_change_pct")
    detail["net_buyers"] = qoq.get("net_buyers")
    detail["net_sellers"] = qoq.get("net_sellers")

    # No adjustment for sparse coverage — the signal is too noisy.
    if data_quality == "sparse":
        return 0.0, detail
    # No adjustment if we don't have a score at all.
    if score is None:
        return 0.0, detail

    adj = 0.0
    if signal_label == "accumulating":
        if score > _HOLDINGS_STRONG_THRESHOLD:
            adj = _HOLDINGS_PTS_STRONG_ACCUMULATING
        elif score >= _HOLDINGS_MILD_THRESHOLD:
            adj = _HOLDINGS_PTS_MILD_ACCUMULATING
    elif signal_label == "distributing":
        if score < -_HOLDINGS_STRONG_THRESHOLD:
            adj = _HOLDINGS_PTS_STRONG_DISTRIBUTING
        elif score <= -_HOLDINGS_MILD_THRESHOLD:
            adj = _HOLDINGS_PTS_MILD_DISTRIBUTING
    # steady / mixed / None / other -> 0

    detail["adjustment_pts"] = float(adj)
    return float(adj), detail


# --- Public API: pillar entry point ----------------------------------------

def compute_institutional(
    ticker: str,
    momentum_pct: Optional[float],
    peer_momentums: Dict[str, Optional[float]],
    *,
    inst_holdings_change_qoq: Optional[float] = None,
    insider_data: Optional[Dict[str, Any]] = None,
    holdings_data: Optional[Dict[str, Any]] = None,
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
    insider_data:
        Optional per-ticker SEC Form 4 insider dict from
        :mod:`lthcs.sources.sec_form4` (keys: ``regime``,
        ``conviction_score``, ``cluster_buying``, ``ceo_cfo_action``).
        When present, drives an asymmetric points adjustment on top of
        the base weighted subscore (see :func:`_apply_insider_adjustment`).
        When ``None`` or empty, no adjustment is applied -- the momentum
        subscore is still real, so don't mark the pillar as stubbed.
    holdings_data:
        Optional per-ticker SEC 13F institutional-holdings dict from
        :mod:`lthcs.sources.sec_13f` (keys: ``conviction_signal``,
        ``signal_score``, ``manager_count``, ``data_quality``,
        ``quarter_over_quarter``). When present, drives an additive
        adjustment on top of the insider adjustment; the COMBINED
        adjustment (insider + holdings) is capped to ``[-7, +12]``.
        Sparse data quality (manager_count < 5) zeros the adjustment.
    """
    has_momentum = momentum_pct is not None
    has_inst = inst_holdings_change_qoq is not None
    has_insider = bool(insider_data)
    has_holdings = bool(holdings_data)

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

    base_sub_score = eff_momentum_w * momentum_subscore + eff_inst_w * inst_subscore

    # Compute the insider + holdings raw adjustments separately, then
    # combine + clamp to the [-7, +12] outer cap. We retain each
    # component's UNCLAMPED contribution in the detail dicts so the
    # variable_detail UI can show "+5 (insider) + +5 (holdings) = +10
    # combined" rather than just the post-cap total.
    _, insider_detail = _apply_insider_adjustment(0.0, insider_data)
    insider_adj_pts = float(insider_detail.get("adjustment_pts") or 0.0)

    holdings_adj_pts, holdings_detail = _apply_holdings_adjustment(holdings_data)

    combined_adj = insider_adj_pts + holdings_adj_pts
    if combined_adj > _COMBINED_ADJ_CEIL:
        combined_adj = _COMBINED_ADJ_CEIL
    elif combined_adj < _COMBINED_ADJ_FLOOR:
        combined_adj = _COMBINED_ADJ_FLOOR

    adjusted_sub_score = base_sub_score + combined_adj
    sub_score = round(float(adjusted_sub_score), 1)

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
            "base_sub_score": round(float(base_sub_score), 1),
            "insider": insider_detail,
            "holdings": holdings_detail,
            "combined_adjustment_pts": float(combined_adj),
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
            "has_insider": has_insider,
            "has_holdings": has_holdings,
        },
    }
