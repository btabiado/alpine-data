"""Adoption Momentum pillar.

Combines two signals into a 0-100 sub-score per ticker:

* **Revenue growth YoY** (from SEC EDGAR XBRL company facts via
  :mod:`lthcs.sources.sec_edgar`) -- scored as the peer-relative
  percentile of the focal ticker's growth within the universe.
* **Search interest acceleration** (Google Trends via :mod:`pytrends`) --
  the regression slope of the trailing 90 days of daily interest values
  mapped onto 0-100.

The two components are combined with a fixed 60/40 weight (revenue /
trends) per ``PHASE_1_BUILD_SPEC.md`` Section 5. When a component is
missing (e.g. SEC has no usable revenue history, or Google Trends is
empty / blocked), the missing component falls back to the neutral 50.0
midpoint so the other component still contributes.

The live Google Trends fetcher is module-private wrt the test suite
(tests always mock it). It uses a polite token bucket (1 req / 10 s,
burst of 5) and a 24-hour file cache, since Google rate-limits
aggressively and trend signals don't move meaningfully intra-day.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from lthcs.normalize import (
    bounded_linear,
    peer_relative_percentile,
    slope,
)
from lthcs.peer_groups import (
    STRATEGY_MATURITY_ONLY,
    get_peer_cohort_with_strategy,
)
from lthcs.sources._cache import FileCache
from lthcs.sources._ratelimit import TokenBucket

# Trends-data renorm weights (when callers pass ``trends_data`` from the
# weekly-batch cache rather than the legacy ``interest_series``). The
# legacy path keeps its documented 60/40 mix; the new path defers more
# heavily to revenue (the higher-fidelity signal) and only lets trends
# contribute 30%. Stale trends data drops further to 15%.
REVENUE_WEIGHT_WITH_TRENDS_DICT = 0.70
TRENDS_WEIGHT_WITH_TRENDS_DICT = 0.30
REVENUE_WEIGHT_WITH_STALE_TRENDS = 0.85
TRENDS_WEIGHT_WITH_STALE_TRENDS = 0.15

# ``pytrends`` is only needed by the live fetcher; tests patch
# ``adoption.TrendReq`` directly, so the import is at module top so the
# patch target exists even when the lib raises at runtime.
try:  # pragma: no cover - trivial import shim
    from pytrends.request import TrendReq
except Exception:  # pragma: no cover
    TrendReq = None  # type: ignore[assignment,misc]


# --- Constants ---------------------------------------------------------------

# Spec: V1 combines revenue and trends 60/40.
REVENUE_WEIGHT = 0.60
TRENDS_WEIGHT = 0.40

# Sanity bounds for YoY revenue growth. A real number outside
# [-100%, +1000%] almost certainly reflects bad XBRL data (one-time
# corporate actions, restatements, currency switches, etc.) rather
# than genuine business momentum, so we drop the signal.
_GROWTH_MIN = -1.0
_GROWTH_MAX = 10.0

# Trend-slope bounds for ``bounded_linear``. Google Trends emits
# integers in [0, 100]; a slope of +/-0.5 per day over 90 days
# corresponds to ~+/-45 points of movement across the window, which is
# a very large swing. These bounds are V1 heuristics.
_TRENDS_SLOPE_LOW = -0.5
_TRENDS_SLOPE_HIGH = 0.5

# Google Trends has no published rate limit, but Google blocks
# aggressively. Be polite: 1 req / 10s with a burst of 5.
_TRENDS_BUCKET_CAPACITY = 5
_TRENDS_BUCKET_REFILL = 0.1

# Cache trend pulls for 24h -- daily granularity doesn't justify hitting
# Google more often than that.
_TRENDS_CACHE_TTL_SECONDS = 24 * 60 * 60


def _is_valid_growth(value: Any) -> bool:
    """Numeric, non-NaN check used when filtering peer growth candidates."""
    if value is None:
        return False
    try:
        f = float(value)
    except (TypeError, ValueError):
        return False
    return f == f  # NaN check


# --- Module state -----------------------------------------------------------

def _cache_root() -> Path:
    return Path(os.environ.get("LTHCS_CACHE_DIR", ".cache/lthcs"))


_cache = FileCache("google_trends", root=_cache_root())
_bucket = TokenBucket(
    capacity=_TRENDS_BUCKET_CAPACITY,
    refill_rate=_TRENDS_BUCKET_REFILL,
)


# --- Revenue helpers --------------------------------------------------------

# A fact's ``form`` (10-K vs 10-Q) describes the FILING, not the period; both
# annual and quarterly facts can appear in a 10-K. We discriminate annual vs
# quarterly by the period DURATION (end_date - start_date in days).
_ANNUAL_MIN_DAYS = 340  # leap years, fiscal year quirks
_ANNUAL_MAX_DAYS = 380
_QUARTER_MIN_DAYS = 80
_QUARTER_MAX_DAYS = 100

# Tolerance for matching "same fiscal quarter, prior year" when picking the
# prior-year annual / quarter-end. Calendars drift a bit week-over-week.
_YOY_END_DATE_TOLERANCE_DAYS = 21


def _parse_date(s: Any) -> Optional["date"]:
    from datetime import date as _date

    if not s:
        return None
    try:
        # XBRL dates are ISO ``YYYY-MM-DD``.
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


def _annual_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return only the annual-period facts, sorted desc by end_date.

    Annual is identified by period duration ≈ 365 days. Falls back to the
    legacy ``form='10-K' AND fp='FY'`` filter when start_date is missing
    (older test fixtures) -- but real SEC data always has start_date.
    """
    annuals: List[Dict[str, Any]] = []
    for r in rows:
        days = _period_days(r)
        if days is not None:
            if _ANNUAL_MIN_DAYS <= days <= _ANNUAL_MAX_DAYS:
                annuals.append(r)
            continue
        # Legacy fallback for fixtures with no start_date.
        if r.get("form") == "10-K" and str(r.get("fp", "")).upper() == "FY":
            annuals.append(r)
    annuals.sort(key=lambda r: str(r.get("end_date", "")), reverse=True)
    return annuals


def _quarterly_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return only the quarterly facts (~90-day period), sorted desc by end_date."""
    quarters: List[Dict[str, Any]] = []
    for r in rows:
        days = _period_days(r)
        if days is not None:
            if _QUARTER_MIN_DAYS <= days <= _QUARTER_MAX_DAYS:
                quarters.append(r)
            continue
        # Legacy fallback for fixtures with no start_date.
        if r.get("form") == "10-Q" and str(r.get("fp", "")).upper() in {"Q1", "Q2", "Q3", "Q4"}:
            quarters.append(r)
    quarters.sort(key=lambda r: str(r.get("end_date", "")), reverse=True)
    return quarters


def _find_prior_year_match(
    target_end: "date", candidates: List[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """Find the candidate whose end_date is closest to one year before ``target_end``."""
    from datetime import timedelta

    want = target_end - timedelta(days=365)
    best: Optional[Dict[str, Any]] = None
    best_delta: Optional[int] = None
    for c in candidates:
        c_end = _parse_date(c.get("end_date"))
        if c_end is None:
            continue
        delta = abs((c_end - want).days)
        if delta > _YOY_END_DATE_TOLERANCE_DAYS:
            continue
        if best_delta is None or delta < best_delta:
            best = c
            best_delta = delta
    return best


def _safe_float(x: Any) -> Optional[float]:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    # Reject NaN/inf -- they poison any downstream arithmetic.
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _growth_from_pair(recent: float, prior: float) -> Optional[float]:
    """Return (recent-prior)/prior, or None if prior is non-positive."""
    if prior <= 0:
        return None
    g = (recent - prior) / prior
    if g < _GROWTH_MIN or g > _GROWTH_MAX:
        return None
    return float(g)


# --- Public API: revenue growth --------------------------------------------

def compute_revenue_growth_yoy(revenue_rows: List[Dict[str, Any]]) -> Optional[float]:
    """Compute the most recent year-over-year revenue growth.

    Strategy:
    1. Prefer two consecutive annual 10-K / FY filings -- compute
       ``(recent - prior) / prior`` directly.
    2. Fall back to a TTM-vs-prior-TTM comparison if at least 8 quarterly
       10-Q rows are available.
    3. Otherwise return ``None``.

    Sanity bound: returns ``None`` for growth values outside
    ``[-1.0, 10.0]`` (drops bad XBRL data masquerading as a 1000%+ swing).

    Parameters
    ----------
    revenue_rows:
        Output of :func:`lthcs.sources.sec_edgar.get_revenue_history`
        -- a list of ``{end_date, value, form, fy, fp}`` dicts.
    """
    if not revenue_rows:
        return None

    # --- Path 1: most recent annual + prior-year annual ---------------------
    annuals = _annual_rows(revenue_rows)
    if len(annuals) >= 2:
        recent = annuals[0]
        recent_end = _parse_date(recent.get("end_date"))
        recent_val = _safe_float(recent.get("value"))
        if recent_end is not None and recent_val is not None:
            prior = _find_prior_year_match(recent_end, annuals[1:])
            if prior is not None:
                prior_val = _safe_float(prior.get("value"))
                if prior_val is not None:
                    g = _growth_from_pair(recent_val, prior_val)
                    if g is not None:
                        return g
            # Fall through to quarterly path if no clean YoY annual pair
            # was found (e.g., most recent annual is the only annual entry).

    # --- Path 2: TTM (last 4 quarters) vs prior TTM -------------------------
    quarters = _quarterly_rows(revenue_rows)
    if len(quarters) >= 8:
        recent_vals: List[float] = []
        for r in quarters[:4]:
            v = _safe_float(r.get("value"))
            if v is None:
                break
            recent_vals.append(v)
        prior_vals: List[float] = []
        for r in quarters[4:8]:
            v = _safe_float(r.get("value"))
            if v is None:
                break
            prior_vals.append(v)
        if len(recent_vals) == 4 and len(prior_vals) == 4:
            recent_ttm = sum(recent_vals)
            prior_ttm = sum(prior_vals)
            return _growth_from_pair(recent_ttm, prior_ttm)

    return None


# --- Public API: trends slope ----------------------------------------------

def compute_search_interest_slope(interest_series: List[float]) -> Optional[float]:
    """Thin wrapper around :func:`lthcs.normalize.slope`.

    Returns the regression slope (interest points per day) of a list of
    daily Google Trends values. ``None`` if fewer than 2 valid points
    are available.
    """
    if not interest_series:
        return None
    return slope(interest_series)


# --- Public API: live fetcher (mocked in tests) ----------------------------

def fetch_google_trends_interest(ticker: str, days: int = 90) -> List[float]:
    """Pull daily Google Trends interest-over-time for ``ticker``.

    Returns the daily interest series (oldest first, most recent last)
    over the trailing ``days`` window. Any error -- network, rate-limit,
    empty payload, missing column -- yields an empty list rather than
    propagating, so callers can treat the absence of trends data as a
    soft signal.

    Cached per ``(ticker, days)`` for 24h; gated by a polite token
    bucket of 1 request / 10s, burst 5.

    .. note::
       This function is not exercised by the test suite. The pillar's
       compute path takes ``interest_series`` as a parameter, so tests
       mock at the call-site boundary.
    """
    if not ticker:
        return []
    norm = ticker.strip().upper()
    if not norm:
        return []

    cache_key = "{}/{}d".format(norm, int(days))
    hit = _cache.get(cache_key)
    if hit is not None and isinstance(hit.value, list):
        return [float(x) for x in hit.value]

    if TrendReq is None:  # pragma: no cover - import shim
        return []

    # Be polite. If we can't get a token within 30s, skip rather than
    # blocking the whole pipeline.
    if not _bucket.acquire(timeout=30.0):
        return []

    try:
        pytrends = TrendReq(hl="en-US", tz=0)
        timeframe = "today {}-d".format(int(days))
        pytrends.build_payload([norm], timeframe=timeframe)
        df = pytrends.interest_over_time()
    except Exception:
        return []

    if df is None:
        return []
    # ``df`` is a pandas DataFrame keyed by date with a column per
    # keyword plus an ``isPartial`` flag. Guard against the column
    # missing (Google sometimes returns an empty frame for low-volume
    # queries).
    try:
        if df.empty:
            return []
    except Exception:
        return []
    if norm not in df.columns:
        return []

    try:
        series = [float(v) for v in df[norm].tolist()]
    except (TypeError, ValueError):
        return []

    _cache.set(cache_key, series, ttl_seconds=_TRENDS_CACHE_TTL_SECONDS)
    return series


# --- Public API: pillar entry point ----------------------------------------

def compute_adoption(
    ticker: str,
    revenue_rows: List[Dict[str, Any]],
    interest_series: List[float],
    peer_growths: Dict[str, Optional[float]],
    *,
    trends_data: Optional[Dict[str, Any]] = None,
    universe_trends_data: Optional[Dict[str, Dict[str, Any]]] = None,
    peer_groups_config: Optional[Dict[str, Any]] = None,
    universe: Optional[Any] = None,
) -> Dict[str, Any]:
    """Compute the Adoption Momentum sub-score for one ticker.

    Combines:

    * Revenue growth percentile within the peer universe (60% weight).
    * Google Trends interest slope mapped to 0-100 (40% weight).

    When ``trends_data`` is supplied (from the weekly-batch
    ``lthcs.sources.google_trends`` cache) it takes precedence over
    ``interest_series``. In that path the pillar reweights to
    revenue=0.70 / trends=0.30 (or 0.85 / 0.15 if the snapshot is
    stale), and the trends sub-score is computed as a percentile of the
    focal ticker's ``acceleration_4w_pct`` within the universe (passed
    via ``universe_trends_data``) — cohort-relative because absolute
    Trends % swings have huge per-ticker variance.

    Either component falling back to its neutral 50.0 midpoint is
    flagged in the returned ``data_quality`` dict so downstream
    aggregation can apply confidence haircuts.
    """
    growth = compute_revenue_growth_yoy(revenue_rows or [])

    # Build the peer distribution. Two modes:
    #
    # * Legacy (peer_groups_config is None): every peer's growth except the
    #   focal ticker's own value. This is the current behaviour — the
    #   pipeline has already restricted ``peer_growths`` to the focal's
    #   maturity-stage bucket in Stage 4.
    # * Compound-key (peer_groups_config + universe both provided): restrict
    #   the percentile distribution to the cohort that shares both
    #   maturity_stage AND sector_group with the focal. Safety valve falls
    #   back to sector_group_only -> maturity_only -> universe when the
    #   strict cohort is too thin (see lthcs.peer_groups.get_peer_cohort).
    valid_candidates = [
        sym for sym, g in (peer_growths or {}).items()
        if g is not None
        and _is_valid_growth(g)
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
            # NaN check.
            if f != f:
                continue
            peer_values.append(f)

    if growth is None:
        revenue_subscore = 50.0
    else:
        revenue_subscore = peer_relative_percentile(
            growth, peer_values, include_self=False
        )

    # --- Trends path: prefer the new weekly-batch ``trends_data`` dict when
    # supplied (Phase 2). Falls back to the legacy ``interest_series`` slope
    # otherwise so existing pipeline call sites (lthcs_daily.py passes []) keep
    # the renorm-to-revenue behaviour exactly as before.
    trends_subscore = 50.0
    trends_slope: Optional[float] = None
    trends_component: Optional[Dict[str, Any]] = None
    has_trends = False
    trends_quality_stale = False

    if isinstance(trends_data, dict) and trends_data:
        # New Phase 2 path: pre-computed acceleration block from the weekly
        # google_trends snapshot. Quality drives the renorm weight ladder.
        quality = trends_data.get("data_quality")
        acc_4w = trends_data.get("acceleration_4w_pct")
        if quality in ("good", "partial", "stale") and acc_4w is not None:
            has_trends = True
            trends_quality_stale = (quality == "stale")
            # Cohort-relative scoring: rank focal's 4w acceleration within
            # the universe. Absolute % swings have huge ticker-by-ticker
            # variance (NVDA can spike +50% in a week while KO is dead flat),
            # so a fixed bounded_linear mapping would compress everyone to
            # the middle. Percentile-rank handles the distribution shape.
            peer_acc: List[float] = []
            if isinstance(universe_trends_data, dict):
                for sym, blk in universe_trends_data.items():
                    if sym == ticker:
                        continue
                    if not isinstance(blk, dict):
                        continue
                    v = blk.get("acceleration_4w_pct")
                    if v is None:
                        continue
                    try:
                        f = float(v)
                    except (TypeError, ValueError):
                        continue
                    if f != f:  # NaN
                        continue
                    peer_acc.append(f)
            if peer_acc:
                trends_subscore = peer_relative_percentile(
                    float(acc_4w), peer_acc, include_self=False
                )
            else:
                # No cohort distribution available — fall back to mapping the
                # raw signal_score (already tanh-compressed to [-1, +1]) onto
                # [0, 100] via a straight linear remap.
                ss = trends_data.get("signal_score")
                try:
                    ss_f = float(ss) if ss is not None else 0.0
                except (TypeError, ValueError):
                    ss_f = 0.0
                trends_subscore = float(50.0 + 50.0 * max(-1.0, min(1.0, ss_f)))
            trends_component = {
                "trend_week": trends_data.get("trend_week"),
                "regime": trends_data.get("regime"),
                "acceleration_4w_pct": acc_4w,
                "acceleration_12w_pct": trends_data.get("acceleration_12w_pct"),
                "signal_score": trends_data.get("signal_score"),
                "quality": quality,
            }
    else:
        # Legacy path: raw daily ``interest_series`` -> slope -> bounded map.
        trends_slope = compute_search_interest_slope(interest_series or [])
        if trends_slope is None:
            trends_subscore = 50.0
        else:
            has_trends = True
            trends_subscore = bounded_linear(
                trends_slope, _TRENDS_SLOPE_LOW, _TRENDS_SLOPE_HIGH
            )

    # Renormalize when a sub-component is the V1 stub (data not available).
    # Mirrors the Institutional pillar's 13F-stub handling: when Trends data
    # isn't available (and it isn't in V1 for 168 tickers — pytrends rate-
    # limits aggressively), reweight so Revenue carries 100% of the pillar
    # rather than diluting toward the neutral-50 placeholder.
    has_revenue = growth is not None
    if has_trends and trends_component is not None:
        # Phase 2 trends_data path: tighter revenue tilt, with a further
        # haircut for stale snapshots.
        if trends_quality_stale:
            effective_weights = (
                REVENUE_WEIGHT_WITH_STALE_TRENDS,
                TRENDS_WEIGHT_WITH_STALE_TRENDS,
            )
        else:
            effective_weights = (
                REVENUE_WEIGHT_WITH_TRENDS_DICT,
                TRENDS_WEIGHT_WITH_TRENDS_DICT,
            )
    elif has_trends:
        # Legacy interest_series path: documented 60/40.
        effective_weights = (REVENUE_WEIGHT, TRENDS_WEIGHT)
    elif has_revenue:
        effective_weights = (1.0, 0.0)  # Revenue carries the pillar alone
    else:
        # Neither signal available — keep the documented 60/40 contract so
        # the result is exactly 50.0 (both components are the neutral
        # midpoint anyway).
        effective_weights = (REVENUE_WEIGHT, TRENDS_WEIGHT)

    sub_score = (
        effective_weights[0] * revenue_subscore
        + effective_weights[1] * trends_subscore
    )
    sub_score = round(float(sub_score), 1)

    variable_detail: Dict[str, Any] = {
        "revenue_growth_yoy": growth,
        "revenue_subscore": float(revenue_subscore),
        "trends_slope": trends_slope,
        "trends_subscore": float(trends_subscore),
        "peer_cohort_strategy": peer_cohort_strategy,
    }
    if peer_cohort_size is not None:
        variable_detail["peer_cohort_size"] = int(peer_cohort_size)
    if trends_component is not None:
        variable_detail["trends"] = trends_component

    return {
        "ticker": ticker,
        "sub_score": sub_score,
        "components": variable_detail,
        "variable_detail": variable_detail,
        "weights": {"revenue": REVENUE_WEIGHT, "trends": TRENDS_WEIGHT},
        "effective_weights": {
            "revenue": float(effective_weights[0]),
            "trends": float(effective_weights[1]),
        },
        "data_quality": {
            "has_revenue": has_revenue,
            "has_trends": has_trends,
        },
    }
