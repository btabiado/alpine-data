"""Financial Evolution pillar.

Combines three SEC-EDGAR-derived signals into a single 0-100 sub-score
per ticker (PHASE_1_BUILD_SPEC.md Section 5):

* **Revenue growth YoY** (40%) -- delegates the YoY math to
  :func:`lthcs.pillars.adoption.compute_revenue_growth_yoy` so the
  Financial pillar shares one source of truth with Adoption. Mapped to
  0-100 via peer-relative percentile against the universe of peer
  growths supplied by the caller.
* **Gross margin trajectory** (30%) -- pair quarterly revenue and
  gross-profit rows by ``(start_date, end_date)``, compute the
  ``gp / rev`` margin per matched quarter, and take the OLS slope of
  the trailing four quarterly margins. Slope is mapped onto 0-100 via
  :func:`lthcs.normalize.bounded_linear` with bounds ``+/- 0.05`` (a
  5-percentage-point swing per quarter is V1's "extreme" anchor).
* **OCF positivity** (30%) -- trailing-4-quarter OCF margin
  (``TTM OCF / TTM revenue``) mapped onto 0-100 via
  :func:`lthcs.normalize.bounded_linear` with bounds ``[-0.10, +0.30]``
  (i.e., +30% OCF margin saturates to 100, -10% to 0).

When any component lacks the data to compute (no usable revenue YoY,
fewer than 4 matched quarterly margins, or can't form trailing-4
revenue/OCF sums) it falls back to the neutral 50.0 midpoint and the
return value's ``data_quality`` dict flags the gap so downstream
aggregation can apply a confidence haircut.

All math is pure -- no I/O. Tests for this module never touch the
network: SEC EDGAR rows are passed in directly as fixtures.
"""

from __future__ import annotations

from datetime import date as _date
from typing import Any, Dict, List, Optional, Tuple

from lthcs.normalize import bounded_linear, peer_relative_percentile, slope
from lthcs.pillars.adoption import compute_revenue_growth_yoy


# --- Constants ---------------------------------------------------------------

# Spec: 40/30/30 weighting of revenue growth / margin trajectory / OCF.
REVENUE_WEIGHT = 0.40
MARGIN_WEIGHT = 0.30
OCF_WEIGHT = 0.30

# Quarterly period detection. Mirrors the constants in
# ``lthcs.pillars.adoption`` -- copied (not imported) so this module
# doesn't depend on private names.
_QUARTER_MIN_DAYS = 80
_QUARTER_MAX_DAYS = 100

# Trailing-four-quarter trends: how many matched quarters we require
# before computing a slope, and how many we use.
_TRAILING_QUARTERS = 4

# Heuristic bounds for the margin-trend slope (margin units per quarter).
# A 5 percentage-point swing per quarter is a very large move; well
# inside this range, the bounded_linear mapping gives a roughly linear
# response with 50.0 representing "flat".
_MARGIN_SLOPE_LOW = -0.05
_MARGIN_SLOPE_HIGH = 0.05

# OCF margin bounds: -10% saturates the floor, +30% saturates the
# ceiling. These are V1 heuristics, chosen to give cash-rich software /
# consumer-staples businesses room near 100 while still penalising
# negative-OCF names hard.
_OCF_MARGIN_LOW = -0.10
_OCF_MARGIN_HIGH = 0.30


# --- Internal helpers -------------------------------------------------------

def _parse_date(s: Any) -> Optional[_date]:
    if not s:
        return None
    try:
        return _date.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None


def _period_days(row: Dict[str, Any]) -> Optional[int]:
    start = _parse_date(row.get("start_date"))
    end = _parse_date(row.get("end_date"))
    if start is None or end is None:
        return None
    delta = (end - start).days
    return delta if delta > 0 else None


def _safe_float(x: Any) -> Optional[float]:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _quarterly_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return only the quarterly rows (~90-day period), sorted desc by end_date.

    Rows without a usable ``start_date`` (the legacy-fixture case) cannot
    be classified by duration and are silently dropped: quarterly
    filtering is meaningless without it, and the caller already has a
    legacy-fixture fallback path elsewhere (annual-only revenue YoY in
    :func:`lthcs.pillars.adoption.compute_revenue_growth_yoy`).
    """
    quarters: List[Dict[str, Any]] = []
    for r in rows or []:
        days = _period_days(r)
        if days is None:
            continue
        if _QUARTER_MIN_DAYS <= days <= _QUARTER_MAX_DAYS:
            quarters.append(r)
    quarters.sort(key=lambda r: str(r.get("end_date", "")), reverse=True)
    return quarters


# --- Public API: gross margin history --------------------------------------

def compute_gross_margin_history(
    revenue_rows: List[Dict[str, Any]],
    gross_profit_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Pair revenue + gross-profit rows by ``(start_date, end_date)``.

    For each matched period compute ``gross_profit / revenue`` and emit::

        {"start_date": str, "end_date": str, "margin": float,
         "revenue": float, "gross_profit": float}

    Sorted by ``end_date`` descending. Only matched pairs are returned --
    periods present on one side but missing on the other are silently
    skipped. No quarterly-vs-annual filter is applied here; the caller
    decides which durations matter (Financial uses the quarterly subset
    for trend slope; an annual caller could use the same helper for an
    annual margin history).

    Rows where either value is non-numeric, zero, or negative revenue
    are dropped (a non-positive revenue can't produce a meaningful
    margin and almost always indicates bad XBRL data).
    """
    rev_by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for r in revenue_rows or []:
        start = r.get("start_date")
        end = r.get("end_date")
        if start is None or end is None:
            continue
        rev_by_key[(str(start), str(end))] = r

    gp_by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for r in gross_profit_rows or []:
        start = r.get("start_date")
        end = r.get("end_date")
        if start is None or end is None:
            continue
        gp_by_key[(str(start), str(end))] = r

    out: List[Dict[str, Any]] = []
    for key, rev_row in rev_by_key.items():
        gp_row = gp_by_key.get(key)
        if gp_row is None:
            continue
        rev_val = _safe_float(rev_row.get("value"))
        gp_val = _safe_float(gp_row.get("value"))
        if rev_val is None or gp_val is None:
            continue
        if rev_val <= 0:
            continue
        margin = gp_val / rev_val
        out.append(
            {
                "start_date": key[0],
                "end_date": key[1],
                "margin": float(margin),
                "revenue": float(rev_val),
                "gross_profit": float(gp_val),
            }
        )

    out.sort(key=lambda r: r["end_date"], reverse=True)
    return out


# --- Public API: margin-trend sub-score -------------------------------------

def compute_margin_trend_subscore(
    revenue_rows: List[Dict[str, Any]],
    gross_profit_rows: List[Dict[str, Any]],
) -> float:
    """Trailing-4-quarter gross-margin trend mapped onto 0-100.

    Steps:
        1. Build the matched margin history (see
           :func:`compute_gross_margin_history`).
        2. Restrict to quarterly periods only (~90-day duration).
        3. Take the most recent 4 quarterly margins.
        4. Compute the OLS slope of those margins (margin units per
           quarter).
        5. Map via ``bounded_linear(slope, -0.05, 0.05)``.

    Returns ``50.0`` -- the neutral midpoint -- if fewer than 4 matched
    quarterly margins are available, so an unscorable signal can't
    distort the parent sub-score.
    """
    history = compute_gross_margin_history(revenue_rows, gross_profit_rows)
    if not history:
        return 50.0

    # Filter to quarterly periods.
    quarterly: List[Dict[str, Any]] = []
    for h in history:
        # Reconstruct duration from the matched record's own dates.
        s = _parse_date(h.get("start_date"))
        e = _parse_date(h.get("end_date"))
        if s is None or e is None:
            continue
        days = (e - s).days
        if _QUARTER_MIN_DAYS <= days <= _QUARTER_MAX_DAYS:
            quarterly.append(h)

    if len(quarterly) < _TRAILING_QUARTERS:
        return 50.0

    # ``history`` is desc-sorted; take the most recent 4, then reverse to
    # chronological so the slope's sign matches "improving over time".
    recent_desc = quarterly[:_TRAILING_QUARTERS]
    chrono = list(reversed(recent_desc))
    margins = [float(r["margin"]) for r in chrono]

    margin_slope = slope(margins)
    if margin_slope is None:
        return 50.0

    return float(bounded_linear(margin_slope, _MARGIN_SLOPE_LOW, _MARGIN_SLOPE_HIGH))


# --- Public API: OCF sub-score ---------------------------------------------

def _trailing_quarterly_sum(rows: List[Dict[str, Any]]) -> Optional[float]:
    """Sum the value of the most recent 4 quarterly rows.

    Returns ``None`` if fewer than 4 quarterly rows are present or any of
    the 4 has a non-numeric value.
    """
    quarters = _quarterly_rows(rows)
    if len(quarters) < _TRAILING_QUARTERS:
        return None
    total = 0.0
    for r in quarters[:_TRAILING_QUARTERS]:
        v = _safe_float(r.get("value"))
        if v is None:
            return None
        total += v
    return total


def compute_ocf_subscore(
    revenue_rows: List[Dict[str, Any]],
    ocf_rows: List[Dict[str, Any]],
) -> float:
    """Trailing-4-quarter OCF margin mapped onto 0-100.

    ``ttm_ocf_margin = (sum of trailing 4 quarterly OCFs) / (sum of
    trailing 4 quarterly revenues)``, then mapped via
    ``bounded_linear(ratio, -0.10, 0.30)``.

    Returns ``50.0`` if either trailing-4 sum can't be formed (fewer
    than 4 quarters, non-numeric values, or non-positive trailing
    revenue).
    """
    ttm_rev = _trailing_quarterly_sum(revenue_rows)
    ttm_ocf = _trailing_quarterly_sum(ocf_rows)
    if ttm_rev is None or ttm_ocf is None:
        return 50.0
    if ttm_rev <= 0:
        return 50.0
    ratio = ttm_ocf / ttm_rev
    return float(bounded_linear(ratio, _OCF_MARGIN_LOW, _OCF_MARGIN_HIGH))


# --- Public API: pillar entry point ----------------------------------------

def _ttm_ocf_margin(
    revenue_rows: List[Dict[str, Any]],
    ocf_rows: List[Dict[str, Any]],
) -> Optional[float]:
    """Helper used purely for explainability in ``components``."""
    ttm_rev = _trailing_quarterly_sum(revenue_rows)
    ttm_ocf = _trailing_quarterly_sum(ocf_rows)
    if ttm_rev is None or ttm_ocf is None or ttm_rev <= 0:
        return None
    return float(ttm_ocf / ttm_rev)


def _margin_trend_slope(
    revenue_rows: List[Dict[str, Any]],
    gross_profit_rows: List[Dict[str, Any]],
) -> Optional[float]:
    """Helper used purely for explainability in ``components``."""
    history = compute_gross_margin_history(revenue_rows, gross_profit_rows)
    quarterly: List[Dict[str, Any]] = []
    for h in history:
        s = _parse_date(h.get("start_date"))
        e = _parse_date(h.get("end_date"))
        if s is None or e is None:
            continue
        days = (e - s).days
        if _QUARTER_MIN_DAYS <= days <= _QUARTER_MAX_DAYS:
            quarterly.append(h)
    if len(quarterly) < _TRAILING_QUARTERS:
        return None
    chrono = list(reversed(quarterly[:_TRAILING_QUARTERS]))
    margins = [float(r["margin"]) for r in chrono]
    return slope(margins)


def compute_financial(
    ticker: str,
    revenue_rows: List[Dict[str, Any]],
    gross_profit_rows: List[Dict[str, Any]],
    ocf_rows: List[Dict[str, Any]],
    peer_growths: Dict[str, Optional[float]],
) -> Dict[str, Any]:
    """Compute the Financial Evolution sub-score for one ticker.

    See module docstring for the component definitions and weighting.

    Parameters
    ----------
    ticker:
        Symbol of the focal entity. Used to exclude the focal's own
        growth from the peer percentile distribution.
    revenue_rows / gross_profit_rows / ocf_rows:
        SEC EDGAR period-dicts (see ``lthcs.sources.sec_edgar``).
    peer_growths:
        ``{symbol: yoy_growth or None}`` for the universe (including the
        focal). The focal's own entry is filtered out before
        percentile-ranking.

    Returns a dict with keys ``ticker``, ``sub_score``, ``components``,
    ``weights``, ``data_quality`` -- see the module docstring / the
    ``Required public API`` block in the spec for the exact schema.
    """
    revenue_rows = revenue_rows or []
    gross_profit_rows = gross_profit_rows or []
    ocf_rows = ocf_rows or []

    # --- Revenue subscore ---------------------------------------------------
    growth = compute_revenue_growth_yoy(revenue_rows)

    peer_values: List[float] = []
    for sym, g in (peer_growths or {}).items():
        if sym == ticker:
            continue
        if g is None:
            continue
        try:
            f = float(g)
        except (TypeError, ValueError):
            continue
        if f != f:  # NaN
            continue
        peer_values.append(f)

    if growth is None:
        revenue_subscore = 50.0
    else:
        revenue_subscore = float(
            peer_relative_percentile(growth, peer_values, include_self=False)
        )

    # --- Margin subscore ----------------------------------------------------
    margin_subscore = compute_margin_trend_subscore(revenue_rows, gross_profit_rows)
    margin_slope_value = _margin_trend_slope(revenue_rows, gross_profit_rows)

    # --- OCF subscore -------------------------------------------------------
    ocf_subscore = compute_ocf_subscore(revenue_rows, ocf_rows)
    ttm_ocf_margin = _ttm_ocf_margin(revenue_rows, ocf_rows)

    # --- Combine ------------------------------------------------------------
    # Renormalize away sub-components that have no real data. Banks (JPM,
    # BAC, GS, etc.) don't report GrossProfit or NetCashProvidedByOperating-
    # Activities in the standard us-gaap XBRL concepts — they use bank-
    # specific concepts (NetInterestIncome, ProvisionForCreditLosses) which
    # this V1 pillar doesn't extract. Without renorm, banks get
    # margin_subscore=50 (30%) + ocf_subscore=50 (30%) baked in at neutral
    # despite having strong revenue percentiles, mechanically capping
    # banks at Weakening band. Same pattern as Adoption Trends-stub renorm.
    has_revenue = growth is not None
    has_margin = margin_slope_value is not None
    has_ocf = ttm_ocf_margin is not None

    pairs = [
        (REVENUE_WEIGHT, revenue_subscore, has_revenue),
        (MARGIN_WEIGHT, margin_subscore, has_margin),
        (OCF_WEIGHT, ocf_subscore, has_ocf),
    ]
    real_pairs = [(w, s) for w, s, ok in pairs if ok]
    if real_pairs:
        real_sum = sum(w for w, _ in real_pairs)
        sub_score = sum((w / real_sum) * s for w, s in real_pairs)
        effective_weights = {
            "revenue": REVENUE_WEIGHT / real_sum if has_revenue else 0.0,
            "margin":  MARGIN_WEIGHT / real_sum if has_margin else 0.0,
            "ocf":     OCF_WEIGHT / real_sum if has_ocf else 0.0,
        }
    else:
        # No real sub-component at all — keep documented weights, all
        # components fall through neutral 50, result is exactly 50.
        sub_score = (
            REVENUE_WEIGHT * revenue_subscore
            + MARGIN_WEIGHT * margin_subscore
            + OCF_WEIGHT * ocf_subscore
        )
        effective_weights = {
            "revenue": REVENUE_WEIGHT,
            "margin": MARGIN_WEIGHT,
            "ocf": OCF_WEIGHT,
        }
    sub_score = round(float(sub_score), 1)

    return {
        "ticker": ticker,
        "sub_score": sub_score,
        "components": {
            "revenue_growth_yoy": growth,
            "revenue_subscore": float(revenue_subscore),
            "margin_subscore": float(margin_subscore),
            "ocf_subscore": float(ocf_subscore),
            "ttm_ocf_margin": ttm_ocf_margin,
            "margin_trend_slope": margin_slope_value,
        },
        "weights": {
            "revenue": REVENUE_WEIGHT,
            "margin": MARGIN_WEIGHT,
            "ocf": OCF_WEIGHT,
        },
        "effective_weights": effective_weights,
        "data_quality": {
            "has_revenue": has_revenue,
            "has_margin": has_margin,
            "has_ocf": has_ocf,
        },
    }
