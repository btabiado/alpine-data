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

Bank code path
--------------

Banks (JPM, BAC, GS, WFC, ...) don't report ``GrossProfit`` or
``NetCashProvidedByOperatingActivities`` under the standard us-gaap
XBRL concepts -- they have a different financial-services concept
family (NetInterestIncome, ProvisionForCreditLosses,
NoninterestIncome). For tickers in the strict-bank allowlist
(:data:`BANK_TICKERS`) the pillar swaps the three sub-components:

* **Net Interest Income growth YoY** (40%) -- the bank's "revenue line"
  is interest income, not the us-gaap ``Revenues`` total. Computed the
  same way as the standard revenue YoY but off the NII series, and
  ranked against the same peer growth distribution the caller supplies.
* **Provision-for-credit-losses / total revenue ratio** (30%) -- the
  bank's "cost of revenue" pressure. Lower is better. Total revenue
  here means ``NII + Noninterest Income`` (the standard bank revenue
  decomposition); the us-gaap ``Revenues`` concept is unreliable for
  banks. Mapped onto 0-100 with ``invert=True`` and bounds
  ``[0.05, 0.30]`` -- a PCL/Rev ratio of 5% or below saturates to 100
  ("benign credit cycle"), 30% or above saturates to 0
  ("crisis-era loan-loss accrual").
* **Noninterest-income / total revenue ratio** (30%) -- the bank's
  revenue diversification. Higher is better (less rate-cycle
  dependent). Mapped onto 0-100 with bounds ``[0.20, 0.60]`` -- a
  20%-or-below ratio means a deposit-and-lend monoline; 60%-or-above
  means a diversified universal bank like JPM.

All math is pure -- no I/O. Tests for this module never touch the
network: SEC EDGAR rows are passed in directly as fixtures.
"""

from __future__ import annotations

from datetime import date as _date
from typing import Any, Dict, List, Optional, Tuple

from lthcs.normalize import bounded_linear, peer_relative_percentile, slope
from lthcs.peer_groups import (
    STRATEGY_MATURITY_ONLY,
    get_peer_cohort_with_strategy,
)
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

# --- Gross-margin fallback chain (P3 audit fix-up, May 2026) ----------------
#
# The Financial pillar walks a four-step fallback chain when ``GrossProfit``
# is not filed (services-heavy / utility / some legacy filers). Each step
# yields the same margin-history row schema as
# :func:`compute_gross_margin_history`; consumers see which step produced
# the slope via the ``margin_source`` tag surfaced in the pillar
# ``components`` dict.
#
# Order (canonical → loose):
#   1. ``gross_profit``       — XBRL ``GrossProfit`` (preferred).
#   2. ``sales_revenue_gross`` — pair ``SalesRevenueGross`` with the same
#      cost-of-revenue series; semantically equivalent to (1) for legacy
#      filers that report under the pre-ASC 606 sales concept.
#   3. ``revenue_minus_cost`` — compute gross profit as
#      ``Revenues - CostOfRevenue``; semantically equivalent to (1) for
#      filers that report the two components but not the rolled-up
#      ``GrossProfit`` concept.
#   4. ``operating_income``   — operating income / revenue. Different
#      metric (nets out OpEx), but indicates margin profile; tagged as
#      a loose fallback so consumers can discount.
#   5. ``none``               — no fallback produced ≥ 4 matched quarters.
#      Sub-score returns the neutral 50.0 midpoint as it does today.
MARGIN_SOURCE_GROSS_PROFIT = "gross_profit"
MARGIN_SOURCE_SALES_REVENUE_GROSS = "sales_revenue_gross"
MARGIN_SOURCE_REVENUE_MINUS_COST = "revenue_minus_cost"
MARGIN_SOURCE_OPERATING_INCOME = "operating_income"
MARGIN_SOURCE_NONE = "none"

# Tuple of every non-"none" source, in fallback-chain order. Exported so
# tests can iterate without hard-coding strings.
MARGIN_SOURCES_ORDER = (
    MARGIN_SOURCE_GROSS_PROFIT,
    MARGIN_SOURCE_SALES_REVENUE_GROSS,
    MARGIN_SOURCE_REVENUE_MINUS_COST,
    MARGIN_SOURCE_OPERATING_INCOME,
)

# OCF margin bounds: -10% saturates the floor, +30% saturates the
# ceiling. These are V1 heuristics, chosen to give cash-rich software /
# consumer-staples businesses room near 100 while still penalising
# negative-OCF names hard.
_OCF_MARGIN_LOW = -0.10
_OCF_MARGIN_HIGH = 0.30


def _is_valid_growth_value(value: Any) -> bool:
    """Numeric, non-NaN check used when filtering peer growth candidates."""
    if value is None:
        return False
    try:
        f = float(value)
    except (TypeError, ValueError):
        return False
    return f == f  # NaN check

# --- Bank-specific constants ------------------------------------------------
#
# Allowlist of tickers routed through the bank code path / participating
# in the bank percentile cohort. Restricted to financials whose XBRL
# decomposition (NII / PCL / Noninterest) makes the bank revenue model
# meaningful; adjacent financials that don't fit (insurance, payments,
# conglomerate) stay on the standard path so they don't get spurious
# neutral scores from absent bank concepts.
#
# May 2026 post-audit expansion (LTHCS audit 2026-05-18): the original
# 8-ticker list (the strict universal / commercial / investment-bank set)
# was too thin to give statistical power to the percentile rank --
# only 7 of those were in the active S&P universe (TFC isn't), so a
# 6-peer distribution made the credit / diversification ranks degenerate
# at the tails. We expanded to 11 by adding three Financials sector
# members whose business model overlaps the bank concept family enough
# that they belong in the cohort:
#
#   BK    -- Bank of New York Mellon, a custody / trust bank. Files NII
#            + Noninterest concepts.
#   COF   -- Capital One, consumer finance with a real loan book. PCL
#            dynamics differ from the universal banks but it's still the
#            correct concept family. Reports NII / PCL / Noninterest.
#   SCHW  -- Charles Schwab, brokerage / banking hybrid. Files NII +
#            Noninterest under the bank concept family.
#   BLK   -- BlackRock, asset manager. Doesn't file NII but is a peer
#            in the Financials sector. Cohort math handles it gracefully:
#            empty NII rows -> excluded from NII-growth ranking, but it
#            still benchmarks against the cohort for revenue growth.
#
# Notable exclusions and why (still skipped):
#   V/MA/AXP/PYPL -- payment networks, not banks.
#   BRK.B -- conglomerate.
#   PRU/MET/TRV/AIG/AFL/ALL -- insurance, different model.
BANK_TICKERS = frozenset({
    "JPM",
    "BAC",
    "WFC",
    "C",
    "GS",
    "MS",
    "USB",
    "TFC",
    # Audit-driven cohort expansion (May 2026):
    "BK",
    "COF",
    "SCHW",
    "BLK",
})

# Bank PCL / total-revenue ratio bounds. ``invert=True`` (lower is
# better). 5% is a benign-credit-cycle low water mark; 30% is the kind
# of accrual we last saw in 2008-09 / Covid-era CECL provisioning.
_BANK_PCL_RATIO_LOW = 0.05
_BANK_PCL_RATIO_HIGH = 0.30

# Bank Noninterest income / total revenue ratio bounds. 20% means a
# nearly-pure deposit-and-lend franchise; 60% means a diversified
# universal-bank revenue mix (JPM/GS/MS sit near or above this).
_BANK_NONINT_RATIO_LOW = 0.20
_BANK_NONINT_RATIO_HIGH = 0.60


def is_bank_ticker(ticker: Optional[str], sector: Optional[str] = None) -> bool:
    """True if ``ticker`` should route through the bank code path.

    Strict allowlist check (``ticker in BANK_TICKERS``). The optional
    ``sector`` arg is accepted for forward-compat / caller clarity but
    isn't required to flip the routing -- the allowlist is the source
    of truth in V1 because XBRL-industry inference is unreliable.
    Callers may still gate on ``sector == "Financials"`` for an extra
    sanity check; passing ``sector=None`` is fine.
    """
    if not ticker:
        return False
    return str(ticker).strip().upper() in BANK_TICKERS


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


# --- Gross-margin fallback chain helpers ------------------------------------

def _synthesize_gross_profit_rows(
    revenue_rows: List[Dict[str, Any]],
    cost_of_revenue_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Build synthetic gross-profit rows as ``Revenue - CostOfRevenue``.

    Pairs ``revenue_rows`` and ``cost_of_revenue_rows`` by
    ``(start_date, end_date)`` and emits a row mimicking the SEC EDGAR
    row schema with ``value = revenue - cost`` and
    ``concept = "_RevenueMinusCostOfRevenue"``. Only matched periods
    with numeric, non-negative resulting gross profit are kept --
    a negative synthetic GP almost always indicates bad XBRL data
    (cost line larger than total revenue would imply the filer reported
    them on different definitions).

    Returns rows sorted by ``end_date`` descending. Empty list if no
    period matches.
    """
    rev_by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for r in revenue_rows or []:
        start = r.get("start_date")
        end = r.get("end_date")
        if start is None or end is None:
            continue
        rev_by_key[(str(start), str(end))] = r

    cost_by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for r in cost_of_revenue_rows or []:
        start = r.get("start_date")
        end = r.get("end_date")
        if start is None or end is None:
            continue
        cost_by_key[(str(start), str(end))] = r

    out: List[Dict[str, Any]] = []
    for key, rev_row in rev_by_key.items():
        cost_row = cost_by_key.get(key)
        if cost_row is None:
            continue
        rev_val = _safe_float(rev_row.get("value"))
        cost_val = _safe_float(cost_row.get("value"))
        if rev_val is None or cost_val is None:
            continue
        gp_val = rev_val - cost_val
        if gp_val < 0:
            # Synthetic GP shouldn't go negative for a healthy filer;
            # drop the period rather than feed garbage to the slope.
            continue
        out.append(
            {
                "start_date": key[0],
                "end_date": key[1],
                "value": float(gp_val),
                "form": rev_row.get("form"),
                "fy": rev_row.get("fy"),
                "fp": rev_row.get("fp"),
                "concept": "_RevenueMinusCostOfRevenue",
            }
        )

    out.sort(key=lambda r: str(r.get("end_date", "")), reverse=True)
    return out


def _quarterly_margin_pairs(
    revenue_rows: List[Dict[str, Any]],
    margin_numerator_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Return matched gross-margin rows restricted to quarterly periods.

    Helper for the fallback resolver: a candidate is "acceptable" only if
    it produces at least ``_TRAILING_QUARTERS`` matched quarterly rows.
    """
    history = compute_gross_margin_history(revenue_rows, margin_numerator_rows)
    quarterly: List[Dict[str, Any]] = []
    for h in history:
        s = _parse_date(h.get("start_date"))
        e = _parse_date(h.get("end_date"))
        if s is None or e is None:
            continue
        days = (e - s).days
        if _QUARTER_MIN_DAYS <= days <= _QUARTER_MAX_DAYS:
            quarterly.append(h)
    return quarterly


def resolve_gross_margin_source(
    revenue_rows: List[Dict[str, Any]],
    gross_profit_rows: List[Dict[str, Any]],
    sales_revenue_gross_rows: Optional[List[Dict[str, Any]]] = None,
    cost_of_revenue_rows: Optional[List[Dict[str, Any]]] = None,
    operating_income_rows: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[List[Dict[str, Any]], str]:
    """Walk the gross-margin fallback chain and return ``(rows, source)``.

    Each fallback is considered "acceptable" if it produces at least
    ``_TRAILING_QUARTERS`` (4) matched quarterly margin rows -- the
    minimum the slope computation needs. The first acceptable fallback
    wins. When none of the four sources clears the bar, returns
    ``([], "none")`` and the caller falls back to the neutral 50.0
    midpoint (preserving current behavior).

    The returned ``rows`` are the "gross profit"-shaped rows (or the
    synthetic ``Revenue - CostOfRevenue`` equivalent) suitable for
    feeding back into :func:`compute_gross_margin_history` or
    :func:`compute_margin_trend_subscore` along with the same
    ``revenue_rows``. The returned ``source`` is one of the
    ``MARGIN_SOURCE_*`` constants.

    Fallback order:
        1. ``MARGIN_SOURCE_GROSS_PROFIT``        — canonical
        2. ``MARGIN_SOURCE_SALES_REVENUE_GROSS`` — legacy revenue + cost
        3. ``MARGIN_SOURCE_REVENUE_MINUS_COST``  — modern revenue + cost
        4. ``MARGIN_SOURCE_OPERATING_INCOME``    — loose proxy
        5. ``MARGIN_SOURCE_NONE``                — give up, neutral 50.
    """
    revenue_rows = revenue_rows or []
    gross_profit_rows = gross_profit_rows or []
    sales_revenue_gross_rows = sales_revenue_gross_rows or []
    cost_of_revenue_rows = cost_of_revenue_rows or []
    operating_income_rows = operating_income_rows or []

    # 1) GrossProfit (canonical).
    quarterly = _quarterly_margin_pairs(revenue_rows, gross_profit_rows)
    if len(quarterly) >= _TRAILING_QUARTERS:
        return gross_profit_rows, MARGIN_SOURCE_GROSS_PROFIT

    # 2) SalesRevenueGross paired with CostOfRevenue (synthesised GP from
    #    legacy revenue + cost; same semantics as canonical GP).
    if sales_revenue_gross_rows and cost_of_revenue_rows:
        synthetic_legacy = _synthesize_gross_profit_rows(
            sales_revenue_gross_rows, cost_of_revenue_rows
        )
        # Restrict the matched-margin history against the LEGACY revenue
        # series so the trend is internally consistent.
        quarterly_legacy = _quarterly_margin_pairs(
            sales_revenue_gross_rows, synthetic_legacy
        )
        if len(quarterly_legacy) >= _TRAILING_QUARTERS:
            # NB: the rows returned here are still the synthetic GP rows;
            # the caller must pair them with the LEGACY revenue series.
            # We surface that via the second return value (the caller in
            # ``compute_financial`` knows to swap in legacy revenue when
            # source == sales_revenue_gross).
            return synthetic_legacy, MARGIN_SOURCE_SALES_REVENUE_GROSS

    # 3) Modern Revenues - CostOfRevenue.
    if cost_of_revenue_rows:
        synthetic_modern = _synthesize_gross_profit_rows(
            revenue_rows, cost_of_revenue_rows
        )
        quarterly_modern = _quarterly_margin_pairs(revenue_rows, synthetic_modern)
        if len(quarterly_modern) >= _TRAILING_QUARTERS:
            return synthetic_modern, MARGIN_SOURCE_REVENUE_MINUS_COST

    # 4) OperatingIncomeLoss — loose proxy (operating margin, not gross).
    quarterly_op = _quarterly_margin_pairs(revenue_rows, operating_income_rows)
    if len(quarterly_op) >= _TRAILING_QUARTERS:
        return operating_income_rows, MARGIN_SOURCE_OPERATING_INCOME

    # 5) Nothing acceptable — caller falls back to neutral 50.
    return [], MARGIN_SOURCE_NONE


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


# --- Bank sub-component helpers --------------------------------------------

def _ttm_quarterly_sum(rows: List[Dict[str, Any]]) -> Optional[float]:
    """Sum the most recent 4 quarterly rows. Returns None on bad data.

    Exposed separately from :func:`_trailing_quarterly_sum` only so
    bank-path call sites read clearly at the use site; the body is the
    same.
    """
    return _trailing_quarterly_sum(rows)


def compute_bank_pcl_ratio_subscore(
    nii_rows: List[Dict[str, Any]],
    noninterest_rows: List[Dict[str, Any]],
    pcl_rows: List[Dict[str, Any]],
) -> float:
    """Bank PCL-to-total-revenue ratio mapped onto 0-100 (lower is better).

    ``total_revenue = TTM NII + TTM Noninterest Income`` -- the standard
    bank revenue decomposition. ``ratio = TTM PCL / total_revenue``,
    inverted onto [0, 100] via ``bounded_linear`` with bounds
    ``[0.05, 0.30]``: 5% or below saturates to 100 (benign cycle),
    30% or above to 0 (crisis-era accrual).

    Returns ``50.0`` when any trailing-4 sum can't be formed (fewer than
    4 quarterly rows on any input, or non-positive total revenue).
    """
    ttm_nii = _trailing_quarterly_sum(nii_rows)
    ttm_nint = _trailing_quarterly_sum(noninterest_rows)
    ttm_pcl = _trailing_quarterly_sum(pcl_rows)
    if ttm_nii is None or ttm_nint is None or ttm_pcl is None:
        return 50.0
    total_rev = ttm_nii + ttm_nint
    if total_rev <= 0:
        return 50.0
    ratio = ttm_pcl / total_rev
    return float(bounded_linear(
        ratio, _BANK_PCL_RATIO_LOW, _BANK_PCL_RATIO_HIGH, invert=True
    ))


def compute_bank_noninterest_ratio_subscore(
    nii_rows: List[Dict[str, Any]],
    noninterest_rows: List[Dict[str, Any]],
) -> float:
    """Noninterest-income share of total bank revenue mapped onto 0-100.

    ``ratio = TTM Noninterest / (TTM NII + TTM Noninterest)``, mapped
    via ``bounded_linear`` with bounds ``[0.20, 0.60]``. Higher is
    better (more revenue diversification).

    Returns ``50.0`` when either trailing-4 sum can't be formed or the
    denominator is non-positive.
    """
    ttm_nii = _trailing_quarterly_sum(nii_rows)
    ttm_nint = _trailing_quarterly_sum(noninterest_rows)
    if ttm_nii is None or ttm_nint is None:
        return 50.0
    total_rev = ttm_nii + ttm_nint
    if total_rev <= 0:
        return 50.0
    ratio = ttm_nint / total_rev
    return float(bounded_linear(
        ratio, _BANK_NONINT_RATIO_LOW, _BANK_NONINT_RATIO_HIGH
    ))


def _bank_pcl_ratio_value(
    nii_rows: List[Dict[str, Any]],
    noninterest_rows: List[Dict[str, Any]],
    pcl_rows: List[Dict[str, Any]],
) -> Optional[float]:
    """Raw PCL/total-revenue ratio for explainability. None on bad data."""
    ttm_nii = _trailing_quarterly_sum(nii_rows)
    ttm_nint = _trailing_quarterly_sum(noninterest_rows)
    ttm_pcl = _trailing_quarterly_sum(pcl_rows)
    if ttm_nii is None or ttm_nint is None or ttm_pcl is None:
        return None
    total_rev = ttm_nii + ttm_nint
    if total_rev <= 0:
        return None
    return float(ttm_pcl / total_rev)


def _bank_noninterest_ratio_value(
    nii_rows: List[Dict[str, Any]],
    noninterest_rows: List[Dict[str, Any]],
) -> Optional[float]:
    """Raw Noninterest / total revenue ratio for explainability. None on bad data."""
    ttm_nii = _trailing_quarterly_sum(nii_rows)
    ttm_nint = _trailing_quarterly_sum(noninterest_rows)
    if ttm_nii is None or ttm_nint is None:
        return None
    total_rev = ttm_nii + ttm_nint
    if total_rev <= 0:
        return None
    return float(ttm_nint / total_rev)


# Bank-cohort weights (apply when bank cohort data is provided so we can
# rank the focal vs other banks instead of mapping its absolute ratio onto
# bounded thresholds). NII is the primary top-line signal at 50%; revenue
# % rank is a secondary 20% (the focal's universe-wide revenue growth %
# rank, but re-computed within the bank cohort); credit (PCL/NII) is 20%
# and diversification (noninterest mix) is 10%.
_BANK_NII_WEIGHT = 0.50
_BANK_REVENUE_WEIGHT = 0.20
_BANK_CREDIT_WEIGHT = 0.20
_BANK_DIVERSIFICATION_WEIGHT = 0.10


def _pcl_to_nii_ratio(
    nii_rows: List[Dict[str, Any]],
    pcl_rows: List[Dict[str, Any]],
) -> Optional[float]:
    """Raw PCL/NII ratio (lower is better) for cohort percentile ranking.

    Uses TTM PCL / TTM NII (not PCL / total revenue) -- the user-facing
    spec is the bank credit-quality cycle indicator, which is canonically
    expressed against NII (the loan-book's revenue line) rather than
    against the broader top-line.

    Returns ``None`` when either TTM sum is unavailable or NII is non-positive.
    """
    ttm_nii = _trailing_quarterly_sum(nii_rows)
    ttm_pcl = _trailing_quarterly_sum(pcl_rows)
    if ttm_nii is None or ttm_pcl is None:
        return None
    if ttm_nii <= 0:
        return None
    return float(ttm_pcl / ttm_nii)


def _noninterest_mix_ratio(
    nii_rows: List[Dict[str, Any]],
    noninterest_rows: List[Dict[str, Any]],
) -> Optional[float]:
    """Noninterest / (NII + Noninterest) for cohort percentile ranking."""
    return _bank_noninterest_ratio_value(nii_rows, noninterest_rows)


def _filter_peer_growths_to_cohort(
    ticker: str,
    peer_growths: Dict[str, Optional[float]],
    cohort: frozenset,
) -> List[float]:
    """Extract numeric peer growths for tickers in ``cohort``, excl focal."""
    cohort_upper = {c.upper() for c in cohort}
    peer_values: List[float] = []
    focal_upper = (ticker or "").strip().upper()
    for sym, g in (peer_growths or {}).items():
        if not sym:
            continue
        sym_upper = str(sym).strip().upper()
        if sym_upper == focal_upper:
            continue
        if sym_upper not in cohort_upper:
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
    return peer_values


def _cohort_nii_growths(
    ticker: str,
    bank_cohort_nii_rows: Optional[Dict[str, List[Dict[str, Any]]]],
) -> List[float]:
    """NII growth YoY for every bank in the cohort (excl focal) with usable data."""
    if not bank_cohort_nii_rows:
        return []
    focal_upper = (ticker or "").strip().upper()
    out: List[float] = []
    for sym, rows in bank_cohort_nii_rows.items():
        if not sym:
            continue
        if str(sym).strip().upper() == focal_upper:
            continue
        if str(sym).strip().upper() not in {b.upper() for b in BANK_TICKERS}:
            continue
        g = compute_revenue_growth_yoy(rows or [])
        if g is None:
            continue
        try:
            f = float(g)
        except (TypeError, ValueError):
            continue
        if f != f:  # NaN
            continue
        out.append(f)
    return out


def _cohort_pcl_nii_ratios(
    ticker: str,
    bank_cohort_nii_rows: Optional[Dict[str, List[Dict[str, Any]]]],
    bank_cohort_pcl_rows: Optional[Dict[str, List[Dict[str, Any]]]],
) -> List[float]:
    """PCL/NII ratio for every bank in the cohort (excl focal) with usable data."""
    if not bank_cohort_nii_rows or not bank_cohort_pcl_rows:
        return []
    focal_upper = (ticker or "").strip().upper()
    bank_upper = {b.upper() for b in BANK_TICKERS}
    out: List[float] = []
    for sym, nii in bank_cohort_nii_rows.items():
        if not sym:
            continue
        sym_upper = str(sym).strip().upper()
        if sym_upper == focal_upper or sym_upper not in bank_upper:
            continue
        pcl = (bank_cohort_pcl_rows or {}).get(sym) or []
        r = _pcl_to_nii_ratio(nii or [], pcl)
        if r is None:
            continue
        out.append(r)
    return out


def _cohort_noninterest_mixes(
    ticker: str,
    bank_cohort_nii_rows: Optional[Dict[str, List[Dict[str, Any]]]],
    bank_cohort_noninterest_rows: Optional[Dict[str, List[Dict[str, Any]]]],
) -> List[float]:
    """Noninterest / total revenue for every bank in cohort (excl focal)."""
    if not bank_cohort_nii_rows or not bank_cohort_noninterest_rows:
        return []
    focal_upper = (ticker or "").strip().upper()
    bank_upper = {b.upper() for b in BANK_TICKERS}
    out: List[float] = []
    for sym, nii in bank_cohort_nii_rows.items():
        if not sym:
            continue
        sym_upper = str(sym).strip().upper()
        if sym_upper == focal_upper or sym_upper not in bank_upper:
            continue
        nint = (bank_cohort_noninterest_rows or {}).get(sym) or []
        r = _noninterest_mix_ratio(nii or [], nint)
        if r is None:
            continue
        out.append(r)
    return out


def _compute_bank_financial(
    ticker: str,
    nii_rows: List[Dict[str, Any]],
    noninterest_rows: List[Dict[str, Any]],
    pcl_rows: List[Dict[str, Any]],
    peer_growths: Dict[str, Optional[float]],
    *,
    bank_cohort_nii_rows: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    bank_cohort_noninterest_rows: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    bank_cohort_pcl_rows: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """Bank-sector Financial Evolution path.

    Two operating modes:

    * **Cohort mode** (when ``bank_cohort_nii_rows`` is provided): score
      the focal bank by percentile-ranking it against other banks on
      NII growth (50%), revenue growth (20%), PCL/NII (20%, inverted),
      and noninterest mix (10%). This is the post-audit path that fixes
      the Tier-3 #15 issue where JPM's +2-3% growth was being ranked
      against NVDA's +65%.
    * **Legacy / no-cohort mode** (when no bank cohort dicts are passed):
      fall back to the original 40/30/30 NII-growth / PCL-ratio /
      noninterest-ratio shape that uses absolute bounded thresholds.
      This preserves backward compatibility for callers that haven't
      plumbed the cohort dicts through yet.

    Renormalizes weights away from sub-components whose underlying
    inputs are unavailable, so a bank with only NII history still scores
    cleanly off its growth percentile.
    """
    nii_rows = nii_rows or []
    noninterest_rows = noninterest_rows or []
    pcl_rows = pcl_rows or []

    # Decide whether to use cohort mode. We need at least the NII cohort
    # dict to do anything useful -- if it's missing, fall back to the
    # legacy 40/30/30 absolute-threshold shape.
    use_cohort = bool(bank_cohort_nii_rows)

    # NII growth YoY (re-use the revenue-growth helper -- it's purely
    # period-arithmetic on whatever quarterly / annual series it gets).
    nii_growth = compute_revenue_growth_yoy(nii_rows)
    has_nii = nii_growth is not None

    # Focal's revenue growth from the supplied peer_growths dict (it was
    # populated for every ticker including banks in Stage 4).
    rev_growth_val: Optional[float] = None
    raw_focal_rev = (peer_growths or {}).get(ticker)
    if raw_focal_rev is None:
        # Try case-insensitive lookup (defensive)
        focal_upper = (ticker or "").strip().upper()
        for sym, g in (peer_growths or {}).items():
            if str(sym).strip().upper() == focal_upper:
                raw_focal_rev = g
                break
    if raw_focal_rev is not None:
        try:
            f = float(raw_focal_rev)
            if f == f:  # not NaN
                rev_growth_val = f
        except (TypeError, ValueError):
            pass
    has_rev = rev_growth_val is not None

    if use_cohort:
        # --- Cohort-relative path -----------------------------------------

        # NII subscore: rank focal NII growth against other banks' NII growths.
        cohort_nii_growths = _cohort_nii_growths(ticker, bank_cohort_nii_rows)
        if has_nii and cohort_nii_growths:
            nii_subscore = float(
                peer_relative_percentile(
                    nii_growth, cohort_nii_growths, include_self=False
                )
            )
            has_nii_subscore = True
        elif has_nii:
            # Cohort too thin -- single bank against itself. Neutral.
            nii_subscore = 50.0
            has_nii_subscore = False
        else:
            nii_subscore = 50.0
            has_nii_subscore = False

        # Revenue subscore: rank focal revenue growth against other banks'.
        bank_cohort_rev_growths = _filter_peer_growths_to_cohort(
            ticker, peer_growths or {}, BANK_TICKERS
        )
        if has_rev and bank_cohort_rev_growths:
            revenue_subscore = float(
                peer_relative_percentile(
                    rev_growth_val, bank_cohort_rev_growths, include_self=False
                )
            )
            has_revenue_subscore = True
        else:
            revenue_subscore = 50.0
            has_revenue_subscore = False

        # Credit subscore: rank focal PCL/NII ratio against cohort, inverted.
        focal_pcl_nii = _pcl_to_nii_ratio(nii_rows, pcl_rows)
        cohort_pcl_nii_ratios = _cohort_pcl_nii_ratios(
            ticker, bank_cohort_nii_rows, bank_cohort_pcl_rows
        )
        if focal_pcl_nii is not None and cohort_pcl_nii_ratios:
            raw_pct = float(
                peer_relative_percentile(
                    focal_pcl_nii, cohort_pcl_nii_ratios, include_self=False
                )
            )
            credit_subscore = 100.0 - raw_pct
            has_credit = True
        else:
            credit_subscore = 50.0
            has_credit = False

        # Diversification subscore: rank focal noninterest mix against cohort.
        focal_nint_mix = _noninterest_mix_ratio(nii_rows, noninterest_rows)
        cohort_nint_mixes = _cohort_noninterest_mixes(
            ticker, bank_cohort_nii_rows, bank_cohort_noninterest_rows
        )
        if focal_nint_mix is not None and cohort_nint_mixes:
            diversification_subscore = float(
                peer_relative_percentile(
                    focal_nint_mix, cohort_nint_mixes, include_self=False
                )
            )
            has_diversification = True
        else:
            diversification_subscore = 50.0
            has_diversification = False

        # Combine 50/20/20/10, renormalize when any component lacks data.
        pairs = [
            (_BANK_NII_WEIGHT, nii_subscore, has_nii_subscore),
            (_BANK_REVENUE_WEIGHT, revenue_subscore, has_revenue_subscore),
            (_BANK_CREDIT_WEIGHT, credit_subscore, has_credit),
            (_BANK_DIVERSIFICATION_WEIGHT, diversification_subscore, has_diversification),
        ]
        real_pairs = [(w, s) for w, s, ok in pairs if ok]
        if real_pairs:
            real_sum = sum(w for w, _ in real_pairs)
            sub_score = sum((w / real_sum) * s for w, s in real_pairs)
            effective_weights = {
                "nii": _BANK_NII_WEIGHT / real_sum if has_nii_subscore else 0.0,
                "revenue": _BANK_REVENUE_WEIGHT / real_sum if has_revenue_subscore else 0.0,
                "credit": _BANK_CREDIT_WEIGHT / real_sum if has_credit else 0.0,
                "diversification": _BANK_DIVERSIFICATION_WEIGHT / real_sum if has_diversification else 0.0,
            }
        else:
            # All components missing -> exact 50.
            sub_score = 50.0
            effective_weights = {
                "nii": _BANK_NII_WEIGHT,
                "revenue": _BANK_REVENUE_WEIGHT,
                "credit": _BANK_CREDIT_WEIGHT,
                "diversification": _BANK_DIVERSIFICATION_WEIGHT,
            }
        sub_score = round(float(sub_score), 1)

        # Legacy explainability fields -- keep the same shape downstream
        # consumers expect, mapping bank components into the legacy slots.
        # "margin" slot continues to surface credit; "ocf" slot continues
        # to surface diversification.
        return {
            "ticker": ticker,
            "sub_score": sub_score,
            "components": {
                "revenue_growth_yoy": nii_growth,
                "revenue_subscore": float(revenue_subscore),
                "margin_subscore": float(credit_subscore),
                "ocf_subscore": float(diversification_subscore),
                "nii_growth_yoy": nii_growth,
                "nii_subscore": float(nii_subscore),
                "credit_subscore": float(credit_subscore),
                "diversification_subscore": float(diversification_subscore),
                "pcl_to_nii_ratio": focal_pcl_nii,
                "pcl_to_revenue_ratio": _bank_pcl_ratio_value(
                    nii_rows, noninterest_rows, pcl_rows
                ),
                "noninterest_to_revenue_ratio": focal_nint_mix,
                "ttm_ocf_margin": None,
                "margin_trend_slope": None,
                # Banks score off the NII/PCL/Noninterest concept family
                # rather than the gross-margin chain. Tag the source as
                # ``"bank"`` so consumers can route narratives correctly
                # instead of treating it as a missing-margin neutral.
                "margin_source": "bank",
            },
            "weights": {
                "nii": _BANK_NII_WEIGHT,
                "revenue": _BANK_REVENUE_WEIGHT,
                "credit": _BANK_CREDIT_WEIGHT,
                "diversification": _BANK_DIVERSIFICATION_WEIGHT,
            },
            "effective_weights": effective_weights,
            "data_quality": {
                # Legacy keys (kept for back-compat with downstream consumers).
                "has_revenue": has_nii_subscore,
                "has_margin": has_credit,
                "has_ocf": has_diversification,
                # New bank-path-specific quality flags.
                "is_bank_cohort": True,
                "has_nii": has_nii_subscore,
                "has_pcl": has_credit,
                "has_noninterest": has_diversification,
                "has_rev_pct": has_revenue_subscore,
            },
            "sector_path": "bank",
        }

    # --- Legacy / no-cohort path: 40/30/30 absolute thresholds -------------
    #
    # Backward-compat: callers that haven't passed cohort dicts (existing
    # unit tests, older callers) still get the original behavior.
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

    if nii_growth is None:
        revenue_subscore = 50.0
    else:
        revenue_subscore = float(
            peer_relative_percentile(nii_growth, peer_values, include_self=False)
        )

    pcl_subscore = compute_bank_pcl_ratio_subscore(
        nii_rows, noninterest_rows, pcl_rows
    )
    pcl_ratio_value = _bank_pcl_ratio_value(
        nii_rows, noninterest_rows, pcl_rows
    )

    nint_subscore = compute_bank_noninterest_ratio_subscore(
        nii_rows, noninterest_rows
    )
    nint_ratio_value = _bank_noninterest_ratio_value(
        nii_rows, noninterest_rows
    )

    has_revenue = nii_growth is not None
    has_pcl = pcl_ratio_value is not None
    has_nint = nint_ratio_value is not None

    pairs = [
        (REVENUE_WEIGHT, revenue_subscore, has_revenue),
        (MARGIN_WEIGHT, pcl_subscore, has_pcl),
        (OCF_WEIGHT, nint_subscore, has_nint),
    ]
    real_pairs = [(w, s) for w, s, ok in pairs if ok]
    if real_pairs:
        real_sum = sum(w for w, _ in real_pairs)
        sub_score = sum((w / real_sum) * s for w, s in real_pairs)
        effective_weights = {
            "revenue": REVENUE_WEIGHT / real_sum if has_revenue else 0.0,
            "margin":  MARGIN_WEIGHT / real_sum if has_pcl else 0.0,
            "ocf":     OCF_WEIGHT / real_sum if has_nint else 0.0,
        }
    else:
        sub_score = (
            REVENUE_WEIGHT * revenue_subscore
            + MARGIN_WEIGHT * pcl_subscore
            + OCF_WEIGHT * nint_subscore
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
            # Bank-specific raw values surface alongside the standard
            # component keys so downstream consumers and the narrative
            # generator can pick them up by either name.
            "revenue_growth_yoy": nii_growth,
            "revenue_subscore": float(revenue_subscore),
            "margin_subscore": float(pcl_subscore),
            "ocf_subscore": float(nint_subscore),
            "nii_growth_yoy": nii_growth,
            "pcl_to_revenue_ratio": pcl_ratio_value,
            "noninterest_to_revenue_ratio": nint_ratio_value,
            # Standard explainability keys -- bank path doesn't compute these.
            "ttm_ocf_margin": None,
            "margin_trend_slope": None,
            "margin_source": "bank",
        },
        "weights": {
            "revenue": REVENUE_WEIGHT,
            "margin": MARGIN_WEIGHT,
            "ocf": OCF_WEIGHT,
        },
        "effective_weights": effective_weights,
        "data_quality": {
            # Bank path uses NII as its revenue line, PCL ratio in the
            # margin slot, and Noninterest ratio in the OCF slot.
            "has_revenue": has_revenue,
            "has_margin": has_pcl,
            "has_ocf": has_nint,
            "is_bank_cohort": False,
            "has_nii": has_revenue,
            "has_pcl": has_pcl,
            "has_noninterest": has_nint,
        },
        "sector_path": "bank",
    }


def compute_financial(
    ticker: str,
    revenue_rows: List[Dict[str, Any]],
    gross_profit_rows: List[Dict[str, Any]],
    ocf_rows: List[Dict[str, Any]],
    peer_growths: Dict[str, Optional[float]],
    *,
    sector: Optional[str] = None,
    nii_rows: Optional[List[Dict[str, Any]]] = None,
    noninterest_rows: Optional[List[Dict[str, Any]]] = None,
    pcl_rows: Optional[List[Dict[str, Any]]] = None,
    bank_cohort_nii_rows: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    bank_cohort_noninterest_rows: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    bank_cohort_pcl_rows: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    sales_revenue_gross_rows: Optional[List[Dict[str, Any]]] = None,
    cost_of_revenue_rows: Optional[List[Dict[str, Any]]] = None,
    operating_income_rows: Optional[List[Dict[str, Any]]] = None,
    peer_groups_config: Optional[Dict[str, Any]] = None,
    universe: Optional[Any] = None,
) -> Dict[str, Any]:
    """Compute the Financial Evolution sub-score for one ticker.

    See module docstring for the component definitions and weighting.

    Parameters
    ----------
    ticker:
        Symbol of the focal entity. Used to exclude the focal's own
        growth from the peer percentile distribution AND to route
        strict-bank tickers (see :data:`BANK_TICKERS`) through the
        bank code path when bank inputs are supplied.
    revenue_rows / gross_profit_rows / ocf_rows:
        SEC EDGAR period-dicts (see ``lthcs.sources.sec_edgar``).
    peer_growths:
        ``{symbol: yoy_growth or None}`` for the universe (including the
        focal). The focal's own entry is filtered out before
        percentile-ranking. The bank path re-uses the same peer
        distribution -- NII growth and revenue growth are roughly
        comparable in magnitude across the universe, and banks live in
        the same maturity-stage cohort as their peers.
    sector:
        Optional GICS sector string. Accepted for forward-compat;
        routing is allowlist-driven (``BANK_TICKERS``), not
        sector-driven, because XBRL-industry inference is unreliable.
        Pass it through if you have it -- it's stamped onto the result
        for traceability.
    nii_rows / noninterest_rows / pcl_rows:
        Bank-specific quarterly series. When ``ticker`` is in
        :data:`BANK_TICKERS` and at least ``nii_rows`` is non-empty the
        bank code path runs and ``revenue_rows`` / ``gross_profit_rows``
        / ``ocf_rows`` are ignored. If a strict-bank ticker arrives
        without bank inputs we fall back to the standard path
        (preserving existing behavior, but the result will be data-
        renormed away from margin / OCF the way it already is today).

    Returns a dict with keys ``ticker``, ``sub_score``, ``components``,
    ``weights``, ``data_quality`` -- see the module docstring / the
    ``Required public API`` block in the spec for the exact schema. The
    bank path additionally surfaces ``sector_path == "bank"`` and the
    raw bank ratios in ``components`` (``nii_growth_yoy``,
    ``pcl_to_revenue_ratio``, ``noninterest_to_revenue_ratio``).
    """
    revenue_rows = revenue_rows or []
    gross_profit_rows = gross_profit_rows or []
    ocf_rows = ocf_rows or []
    nii_rows = nii_rows or []
    noninterest_rows = noninterest_rows or []
    pcl_rows = pcl_rows or []

    # Bank routing: strict-allowlist ticker AND at least an NII series
    # to score off. If a caller hasn't fetched bank inputs (e.g. legacy
    # pipeline code) the bank path silently falls through and the
    # standard path runs with its existing data-quality renorm.
    if is_bank_ticker(ticker, sector) and nii_rows:
        return _compute_bank_financial(
            ticker,
            nii_rows,
            noninterest_rows,
            pcl_rows,
            peer_growths,
            bank_cohort_nii_rows=bank_cohort_nii_rows,
            bank_cohort_noninterest_rows=bank_cohort_noninterest_rows,
            bank_cohort_pcl_rows=bank_cohort_pcl_rows,
        )

    # --- Revenue subscore ---------------------------------------------------
    growth = compute_revenue_growth_yoy(revenue_rows)

    # When peer_groups_config + universe are provided, restrict the
    # percentile distribution to the compound (maturity_stage, sector_group)
    # cohort. Safety valve falls back to sector_group_only -> maturity_only
    # -> universe when the strict cohort is too thin. When the kwargs are
    # absent, preserve current behaviour (whatever cohort peer_growths
    # already represents — typically the maturity-stage bucket built by
    # lthcs_daily.py Stage 4).
    valid_candidates = [
        sym for sym, g in (peer_growths or {}).items()
        if g is not None
        and _is_valid_growth_value(g)
    ]

    peer_cohort_size: Optional[int] = None
    peer_cohort_strategy: str = STRATEGY_MATURITY_ONLY
    if peer_groups_config and universe is not None:
        cohort, peer_cohort_strategy = get_peer_cohort_with_strategy(
            ticker,
            universe,
            peer_groups_config,
            candidate_tickers=valid_candidates,
        )
        cohort_set = set(cohort)
        peer_values: List[float] = []
        for sym, g in (peer_growths or {}).items():
            if sym == ticker:
                continue
            if sym not in cohort_set:
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
        peer_cohort_size = len(peer_values) + (1 if ticker in cohort_set else 0)
    else:
        peer_values = []
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
    #
    # Walk the gross-margin fallback chain (P3 audit fix-up, May 2026).
    # When ``GrossProfit`` is missing we successively try
    # ``SalesRevenueGross``, ``Revenues - CostOfRevenue``, and
    # ``OperatingIncomeLoss`` before falling back to the neutral 50.0.
    # The chosen source is surfaced in ``components.margin_source`` so
    # downstream consumers (variable_detail / narratives) can disclose
    # which proxy produced the slope.
    effective_margin_numerator_rows, margin_source = resolve_gross_margin_source(
        revenue_rows,
        gross_profit_rows,
        sales_revenue_gross_rows=sales_revenue_gross_rows,
        cost_of_revenue_rows=cost_of_revenue_rows,
        operating_income_rows=operating_income_rows,
    )
    # The legacy ``SalesRevenueGross`` fallback pairs the synthetic GP
    # rows with the LEGACY revenue series (not the modern Revenues
    # concept) so the trend is internally consistent. All other sources
    # pair with the standard revenue_rows.
    if margin_source == MARGIN_SOURCE_SALES_REVENUE_GROSS:
        margin_revenue_rows: List[Dict[str, Any]] = sales_revenue_gross_rows or []
    else:
        margin_revenue_rows = revenue_rows

    margin_subscore = compute_margin_trend_subscore(
        margin_revenue_rows, effective_margin_numerator_rows
    )
    margin_slope_value = _margin_trend_slope(
        margin_revenue_rows, effective_margin_numerator_rows
    )

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

    components: Dict[str, Any] = {
        "revenue_growth_yoy": growth,
        "revenue_subscore": float(revenue_subscore),
        "margin_subscore": float(margin_subscore),
        "ocf_subscore": float(ocf_subscore),
        "ttm_ocf_margin": ttm_ocf_margin,
        "margin_trend_slope": margin_slope_value,
        "margin_source": margin_source,
        "peer_cohort_strategy": peer_cohort_strategy,
    }
    if peer_cohort_size is not None:
        components["peer_cohort_size"] = int(peer_cohort_size)

    return {
        "ticker": ticker,
        "sub_score": sub_score,
        "components": components,
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
        "sector_path": "standard",
    }
