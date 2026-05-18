"""Yahoo Finance events source for LTHCS Thesis Integrity.

Wraps three ``yfinance`` endpoints that are NOT covered by ``yahoo.py``:

    * ``Ticker.earnings_dates``        — past + future earnings dates with
                                         EPS estimate, actual, and surprise%.
    * ``Ticker.recommendations``       — analyst upgrade/downgrade history.
    * ``Ticker.recommendations_summary`` — aggregated buy/hold/sell snapshot.

Each of these is event-driven signal that complements the price/momentum
data already pulled via ``yahoo.py``. Earnings beats/misses and analyst
rating moves feed two new Thesis pillars; see
``summarize_earnings_for_thesis`` and
``summarize_analyst_actions_for_thesis``.

This module is intentionally separate from ``yahoo.py`` so the prices /
volatility / momentum path stays focused. The two modules share neither
cache namespace nor rate-limit bucket — earnings data is touched once
per ticker per day, where prices may be requested many times.

Public functions:
    * ``get_earnings_dates(ticker, limit=4)``
    * ``get_analyst_actions(ticker, days=90)``
    * ``get_recommendation_summary(ticker)``
    * ``summarize_earnings_for_thesis(earnings)``
    * ``summarize_analyst_actions_for_thesis(actions)``

All upstream calls go through:
    * a 24h ``FileCache`` per endpoint, and
    * a polite ``TokenBucket(capacity=5, refill_rate=0.5)`` (1 req / 2 sec).

Caches are populated on first hit and used for downstream Thesis-pillar
work. Any yfinance exception, missing column, or unknown ticker yields
an empty list / dict — callers should treat that as a neutral signal.
"""

from __future__ import annotations

import datetime as _dt
import math
from typing import Any, Dict, List, Optional

import pandas as _pd
import yfinance as yf

from lthcs.sources._cache import FileCache
from lthcs.sources._ratelimit import TokenBucket

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# 24 hours. Earnings dates and recommendations don't change intraday.
_CACHE_TTL_SECONDS = 24 * 60 * 60

# Five sentiment labels Alpha Vantage uses on ``ticker_sentiment_label``.
# We emit them so downstream Thesis code can mix Yahoo earnings/analyst
# signal with AV news signal without a separate adapter.
_SENTIMENT_LABELS = (
    "Bearish",
    "Somewhat-Bearish",
    "Neutral",
    "Somewhat-Bullish",
    "Bullish",
)

# Surprise-pct -> (sentiment_score, label) thresholds. See
# ``summarize_earnings_for_thesis`` for the policy rationale.
_SURPRISE_THRESHOLDS = (
    # (lower_bound_inclusive, score, label)
    (10.0, 0.7, "Bullish"),            # >= +10%
    (3.0, 0.4, "Somewhat-Bullish"),    # +3% .. +10%
    (-3.0, 0.0, "Neutral"),            # -3% .. +3%
    (-10.0, -0.4, "Somewhat-Bearish"), # -10% .. -3%
)
# Anything below -10% gets the strong-miss bucket.
_STRONG_MISS = (-0.7, "Bearish")

# Action text -> direction sign. Be defensive: case-insensitive partial
# match on the verb. Many feeds use plural ("Upgrades") or third-person
# singular ("Upgrade"); we match on the stem.
ACTION_DIRECTION: Dict[str, float] = {
    "upgrade": +1.0,
    "initiate": +0.5,
    "maintain": 0.0,
    "reiterate": 0.0,
    "reinstate": 0.0,
    "downgrade": -1.0,
    "resume": 0.0,
}

# Rough grade -> numeric scale for from/to delta detection.
GRADE_SCORE: Dict[str, float] = {
    "strong buy": 1.0,
    "buy": 0.75,
    "outperform": 0.6,
    "overweight": 0.6,
    "accumulate": 0.5,
    "hold": 0.0,
    "neutral": 0.0,
    "market perform": 0.0,
    "equal weight": 0.0,
    "reduce": -0.5,
    "underperform": -0.6,
    "underweight": -0.6,
    "sell": -0.75,
    "strong sell": -1.0,
}

# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------

_EARNINGS_CACHE = FileCache("yahoo_earnings")
_RECO_CACHE = FileCache("yahoo_reco")

# Yahoo via yfinance is fragile — be polite: 1 req / 2 sec with a burst of 5.
_BUCKET = TokenBucket(capacity=5, refill_rate=0.5)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _today_iso() -> str:
    return _dt.date.today().isoformat()


def _today_date() -> _dt.date:
    return _dt.date.today()


def _parse_as_of(as_of: Optional[str]) -> Optional[_dt.date]:
    """Coerce an optional ``as_of`` string to a date.

    Returns ``None`` on invalid input — callers treat that the same as
    ``as_of=None`` (i.e. fall through to today).
    """
    if as_of is None:
        return None
    if not isinstance(as_of, str) or not as_of.strip():
        return None
    try:
        return _dt.date.fromisoformat(as_of.strip())
    except ValueError:
        return None


def _coerce_float(raw: Any) -> Optional[float]:
    """Best-effort float coercion. NaN / None / non-numeric -> None."""
    if raw is None:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if math.isnan(val) or math.isinf(val):
        return None
    return val


def _coerce_int(raw: Any, default: int = 0) -> int:
    if raw is None:
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default
    if math.isnan(val) or math.isinf(val):
        return default
    return int(val)


def _coerce_str(raw: Any) -> Optional[str]:
    """Coerce to a trimmed string. None / NaN / empty -> None."""
    if raw is None:
        return None
    # pandas NaN floats survive isinstance(..., float)
    if isinstance(raw, float) and math.isnan(raw):
        return None
    s = str(raw).strip()
    if not s or s.lower() == "nan":
        return None
    return s


def _coerce_iso_date(raw: Any) -> Optional[str]:
    """Best-effort ISO YYYY-MM-DD extraction from common pandas date types."""
    if raw is None:
        return None
    # pandas / numpy timestamps expose strftime/.date().
    if hasattr(raw, "strftime"):
        try:
            return raw.strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            pass
    if hasattr(raw, "date"):
        try:
            d = raw.date()
            if isinstance(d, _dt.date):
                return d.isoformat()
        except (TypeError, ValueError):
            pass
    if isinstance(raw, _dt.datetime):
        return raw.date().isoformat()
    if isinstance(raw, _dt.date):
        return raw.isoformat()
    s = _coerce_str(raw)
    if not s:
        return None
    # Strip trailing Z (3.9 fromisoformat doesn't accept it).
    cleaned = s.replace("Z", "+00:00")
    try:
        return _dt.datetime.fromisoformat(cleaned).date().isoformat()
    except ValueError:
        pass
    # Plain YYYY-MM-DD prefix.
    try:
        return _dt.date.fromisoformat(s[:10]).isoformat()
    except ValueError:
        return None


def _column(df: "_pd.DataFrame", names: List[str]) -> Optional[str]:
    """Find the first matching column name in ``df`` (case-insensitive).

    yfinance uses slightly different column names across versions
    (e.g. ``Reported EPS`` vs ``EPS Actual``). We accept any of a list.
    Returns the actual column name, or None if no match.
    """
    if df is None:
        return None
    cols = {str(c).strip().lower(): c for c in df.columns}
    for name in names:
        actual = cols.get(name.lower())
        if actual is not None:
            return actual
    return None


def _empty_label_counts() -> Dict[str, int]:
    return {label: 0 for label in _SENTIMENT_LABELS}


def _grade_score(grade: Optional[str]) -> Optional[float]:
    if grade is None:
        return None
    return GRADE_SCORE.get(grade.strip().lower())


def _action_direction(action: Optional[str]) -> float:
    """Map a free-form action string to a direction sign.

    Case-insensitive partial match against the ``ACTION_DIRECTION`` stems.
    Unknown actions return 0.0 (treated as a hold/neutral).
    """
    if action is None:
        return 0.0
    a = action.strip().lower()
    if not a:
        return 0.0
    for stem, direction in ACTION_DIRECTION.items():
        if stem in a:
            return direction
    return 0.0


# ---------------------------------------------------------------------------
# get_earnings_dates
# ---------------------------------------------------------------------------


def _parse_earnings_df(
    df: "_pd.DataFrame",
    ticker: str,
    limit: int,
    as_of_date: Optional[_dt.date] = None,
) -> List[Dict[str, Any]]:
    """Normalize a yfinance ``earnings_dates`` DataFrame into our shape.

    Defensive: any missing column or unparseable row is skipped.

    When ``as_of_date`` is supplied:
      * rows with ``date > as_of_date`` are filtered out entirely
      * the ``is_future`` flag is computed against ``as_of_date`` rather
        than today (so a date that was "future" on ``as_of_date`` but is
        now in the past is still labelled future).
    """
    if df is None or len(df) == 0:
        return []

    est_col = _column(df, ["EPS Estimate", "Earnings Estimate", "Estimate"])
    act_col = _column(df, ["Reported EPS", "EPS Actual", "Actual"])
    surprise_col = _column(df, ["Surprise(%)", "Surprise %", "% Surprise"])

    today = as_of_date if as_of_date is not None else _today_date()
    rows: List[Dict[str, Any]] = []
    for idx, row in df.iterrows():
        date_iso = _coerce_iso_date(idx)
        if date_iso is None:
            continue

        est = _coerce_float(row[est_col]) if est_col is not None else None
        act = _coerce_float(row[act_col]) if act_col is not None else None

        # Prefer the explicit surprise column if present; otherwise compute.
        surprise_pct: Optional[float] = None
        if surprise_col is not None:
            surprise_pct = _coerce_float(row[surprise_col])
        if surprise_pct is None and est is not None and act is not None:
            denom = abs(est)
            if denom > 0:
                surprise_pct = (act - est) / denom * 100.0

        try:
            date_obj = _dt.date.fromisoformat(date_iso)
        except ValueError:
            continue

        if as_of_date is not None and date_obj > as_of_date:
            # Historical mode: drop anything after the as-of cutoff.
            continue

        is_future = date_obj > today

        rows.append(
            {
                "ticker": ticker.upper(),
                "date": date_iso,
                "eps_estimate": est,
                "eps_actual": act,
                "surprise_pct": surprise_pct,
                "is_future": is_future,
            }
        )

    # Newest first by date string (ISO YYYY-MM-DD sorts lexicographically).
    rows.sort(key=lambda r: r["date"], reverse=True)
    if limit > 0:
        rows = rows[:limit]
    return rows


def get_earnings_dates(
    ticker: str,
    limit: int = 4,
    as_of: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return recent + upcoming earnings dates for ``ticker``.

    Each entry has shape::

        {
          "ticker", "date" (ISO),
          "eps_estimate", "eps_actual", "surprise_pct" (or None),
          "is_future" (bool),
        }

    Cached 24h per ``(ticker, limit, as_of)``. Returns ``[]`` on failure
    or unknown ticker — never raises.

    When ``as_of`` is supplied (ISO ``YYYY-MM-DD``) only earnings dates
    ``<= as_of`` are returned, and the last ``limit`` of those.
    """
    if not ticker or not str(ticker).strip():
        return []
    as_of_date = _parse_as_of(as_of)
    cache_suffix = f"/asof/{as_of_date.isoformat()}" if as_of_date else ""
    cache_key = f"{ticker.upper()}/earnings_dates/{int(limit)}{cache_suffix}"
    hit = _EARNINGS_CACHE.get(cache_key)
    if hit is not None:
        return [dict(row) for row in (hit.value or [])]

    # Rate limit — block briefly rather than skip; this path runs at most
    # once per ticker per 24h so the wait is acceptable.
    _BUCKET.acquire()

    try:
        t = yf.Ticker(ticker)
        df = t.earnings_dates
    except Exception:
        return []

    rows = _parse_earnings_df(df, ticker, limit, as_of_date=as_of_date)
    _EARNINGS_CACHE.set(cache_key, rows, ttl_seconds=_CACHE_TTL_SECONDS)
    return rows


# ---------------------------------------------------------------------------
# get_analyst_actions
# ---------------------------------------------------------------------------


def _parse_recommendations_df(
    df: "_pd.DataFrame",
    ticker: str,
    days: int,
    as_of_date: Optional[_dt.date] = None,
) -> List[Dict[str, Any]]:
    """Normalize a yfinance ``recommendations`` DataFrame.

    yfinance has used both an indexed date and a ``Date`` column over the
    years. We accept either: prefer the index when it looks date-shaped,
    fall back to a Date column.

    When ``as_of_date`` is provided the window is ``[as_of - days, as_of]``
    instead of ``[today - days, today]``.
    """
    if df is None or len(df) == 0:
        return []

    firm_col = _column(df, ["Firm", "Analyst"])
    action_col = _column(df, ["Action"])
    from_col = _column(df, ["From Grade", "FromGrade", "From"])
    to_col = _column(df, ["To Grade", "ToGrade", "To Grade"])
    date_col = _column(df, ["Date"])

    anchor = as_of_date if as_of_date is not None else _today_date()
    cutoff = anchor - _dt.timedelta(days=max(int(days), 0))

    rows: List[Dict[str, Any]] = []
    for idx, row in df.iterrows():
        # Prefer the index for the date — that's what current yfinance returns.
        date_iso = _coerce_iso_date(idx)
        if date_iso is None and date_col is not None:
            date_iso = _coerce_iso_date(row[date_col])
        if date_iso is None:
            continue
        try:
            date_obj = _dt.date.fromisoformat(date_iso)
        except ValueError:
            continue
        if date_obj < cutoff:
            continue
        if as_of_date is not None and date_obj > as_of_date:
            # Historical mode: drop actions after the as-of cutoff.
            continue

        firm = _coerce_str(row[firm_col]) if firm_col is not None else None
        if firm is None:
            firm = ""
        action_raw = _coerce_str(row[action_col]) if action_col is not None else None
        action_text = action_raw or ""
        from_grade = _coerce_str(row[from_col]) if from_col is not None else None
        to_grade = _coerce_str(row[to_col]) if to_col is not None else None

        direction = _action_direction(action_text)
        # Detect rating-bracket moves: a from/to delta can override or
        # augment the action verb (e.g. "Maintains" from hold to buy is
        # still effectively an upgrade signal).
        f_score = _grade_score(from_grade)
        t_score = _grade_score(to_grade)
        if direction == 0.0 and f_score is not None and t_score is not None:
            delta = t_score - f_score
            if delta > 0:
                direction = +1.0 if delta >= 0.5 else +0.5
            elif delta < 0:
                direction = -1.0 if delta <= -0.5 else -0.5

        rows.append(
            {
                "ticker": ticker.upper(),
                "date": date_iso,
                "firm": firm,
                "action": action_text,
                "from_grade": from_grade,
                "to_grade": to_grade,
                "direction": direction,
            }
        )

    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows


def get_analyst_actions(
    ticker: str,
    days: int = 90,
    as_of: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return analyst upgrade/downgrade history for the last ``days`` days.

    Each entry::

        {
          "ticker", "date" (ISO),
          "firm", "action",
          "from_grade", "to_grade",
          "direction" (+1 / +0.5 / 0 / -0.5 / -1),
        }

    Cached 24h per (ticker, days, as_of). Returns ``[]`` on any yfinance
    failure.

    When ``as_of`` is supplied the window is ``[as_of - days, as_of]``
    instead of ``[today - days, today]``.
    """
    if not ticker or not str(ticker).strip():
        return []
    as_of_date = _parse_as_of(as_of)
    cache_suffix = f"/asof/{as_of_date.isoformat()}" if as_of_date else ""
    cache_key = f"{ticker.upper()}/analyst_actions/{int(days)}{cache_suffix}"
    hit = _RECO_CACHE.get(cache_key)
    if hit is not None:
        return [dict(row) for row in (hit.value or [])]

    _BUCKET.acquire()

    try:
        t = yf.Ticker(ticker)
        df = t.recommendations
    except Exception:
        return []

    rows = _parse_recommendations_df(df, ticker, days, as_of_date=as_of_date)
    _RECO_CACHE.set(cache_key, rows, ttl_seconds=_CACHE_TTL_SECONDS)
    return rows


# ---------------------------------------------------------------------------
# get_recommendation_summary
# ---------------------------------------------------------------------------


def _parse_summary_df(df: "_pd.DataFrame", ticker: str) -> Dict[str, Any]:
    """Normalize a yfinance ``recommendations_summary`` DataFrame.

    Current yfinance returns a DataFrame with one row per month (most
    recent first) and columns ``strongBuy``, ``buy``, ``hold``, ``sell``,
    ``strongSell``, ``period`` (e.g. "0m", "-1m", "-2m", ...). Older
    versions used title-case "Strong Buy" / "Buy" / etc. with a
    DateTimeIndex.

    We pick the most recent row (period == "0m" if present, else first
    row) and compute the consensus score::

        score = (1*strong_buy + 0.5*buy + 0*hold - 0.5*sell - 1*strong_sell)
                / total_analysts

    so the score is in [-1, +1].
    """
    if df is None or len(df) == 0:
        return {}

    strong_buy_col = _column(df, ["strongBuy", "Strong Buy", "strong_buy"])
    buy_col = _column(df, ["buy", "Buy"])
    hold_col = _column(df, ["hold", "Hold"])
    sell_col = _column(df, ["sell", "Sell"])
    strong_sell_col = _column(df, ["strongSell", "Strong Sell", "strong_sell"])
    period_col = _column(df, ["period", "Period"])

    # Pick the row for the most recent month. yfinance encodes periods as
    # "0m", "-1m", "-2m", "-3m" where "0m" is current. If the period
    # column is present pick "0m"; else use the first row.
    target_row = None
    if period_col is not None:
        for _, row in df.iterrows():
            if _coerce_str(row[period_col]) == "0m":
                target_row = row
                break
    if target_row is None:
        target_row = df.iloc[0]

    strong_buy = _coerce_int(target_row[strong_buy_col]) if strong_buy_col else 0
    buy = _coerce_int(target_row[buy_col]) if buy_col else 0
    hold = _coerce_int(target_row[hold_col]) if hold_col else 0
    sell = _coerce_int(target_row[sell_col]) if sell_col else 0
    strong_sell = (
        _coerce_int(target_row[strong_sell_col]) if strong_sell_col else 0
    )

    total = strong_buy + buy + hold + sell + strong_sell
    if total > 0:
        score = (
            1.0 * strong_buy
            + 0.5 * buy
            + 0.0 * hold
            + -0.5 * sell
            + -1.0 * strong_sell
        ) / total
        # Clamp guard (shouldn't be needed mathematically; defensive).
        if score > 1.0:
            score = 1.0
        elif score < -1.0:
            score = -1.0
        consensus_score: Optional[float] = score
    else:
        consensus_score = None

    # Month label. yfinance current shape: period "0m" plus today's month.
    # Older shape used the DataFrame index as the date. Pick whichever is
    # available.
    month: Optional[str] = None
    if period_col is not None:
        # period is relative; encode as the current month.
        month = _today_date().strftime("%Y-%m")
    else:
        # Try the index of the picked row.
        try:
            idx_val = target_row.name
            iso = _coerce_iso_date(idx_val)
            if iso is not None:
                month = iso[:7]
        except AttributeError:
            month = None

    return {
        "ticker": ticker.upper(),
        "month": month,
        "strong_buy": strong_buy,
        "buy": buy,
        "hold": hold,
        "sell": sell,
        "strong_sell": strong_sell,
        "total_analysts": total,
        "consensus_score": consensus_score,
    }


def get_recommendation_summary(ticker: str) -> Dict[str, Any]:
    """Return the aggregated buy/hold/sell snapshot for ``ticker``.

    Output keys: ``ticker``, ``month`` (YYYY-MM), ``strong_buy``,
    ``buy``, ``hold``, ``sell``, ``strong_sell``, ``total_analysts``,
    ``consensus_score`` (in [-1, +1] or ``None`` when no analysts).

    Cached 24h. Returns ``{}`` on any yfinance failure.
    """
    if not ticker or not str(ticker).strip():
        return {}
    cache_key = f"{ticker.upper()}/reco_summary"
    hit = _RECO_CACHE.get(cache_key)
    if hit is not None:
        return dict(hit.value or {})

    _BUCKET.acquire()

    try:
        t = yf.Ticker(ticker)
        df = t.recommendations_summary
    except Exception:
        return {}

    parsed = _parse_summary_df(df, ticker)
    _RECO_CACHE.set(cache_key, parsed, ttl_seconds=_CACHE_TTL_SECONDS)
    return parsed


# ---------------------------------------------------------------------------
# Thesis-pillar adapters
# ---------------------------------------------------------------------------


def _classify_surprise(surprise_pct: float) -> Dict[str, Any]:
    """Map a surprise% into (sentiment_score, label) per the spec table."""
    # Anything below the lowest threshold (-10%) is a strong miss.
    if surprise_pct < _SURPRISE_THRESHOLDS[-1][0]:
        score, label = _STRONG_MISS
        return {"score": score, "label": label}
    # Walk the table from strongest to weakest threshold.
    for bound, score, label in _SURPRISE_THRESHOLDS:
        if surprise_pct >= bound:
            return {"score": score, "label": label}
    # Defensive: shouldn't reach here, but fall back to strong miss.
    score, label = _STRONG_MISS
    return {"score": score, "label": label}


def summarize_earnings_for_thesis(earnings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Convert ``get_earnings_dates`` output to a Thesis-pillar payload.

    Strategy: look at the most-recent COMPLETED earnings event
    (``is_future=False``). Surprise classifier:

        surprise_pct >= +10%      ->  +0.7   "strong beat"   (Bullish)
        surprise_pct +3% to +10%  ->  +0.4   "beat"          (Somewhat-Bullish)
        surprise_pct -3% to +3%   ->   0.0   "in-line"       (Neutral)
        surprise_pct -10% to -3%  ->  -0.4   "miss"          (Somewhat-Bearish)
        surprise_pct <= -10%      ->  -0.7   "strong miss"   (Bearish)

    Output shape mirrors ``alpha_vantage.parse_ticker_sentiment`` so
    ``compute_thesis_from_stored_sentiment`` can consume it without a
    branch. ``article_count`` is 1 when a completed report exists, else 0.
    """
    label_counts = _empty_label_counts()
    ticker = ""
    last_completed: Optional[Dict[str, Any]] = None

    # Newest-first input — pick the first non-future entry with a numeric
    # surprise.
    for row in earnings or []:
        if not isinstance(row, dict):
            continue
        if not ticker:
            ticker = str(row.get("ticker") or "").upper()
        if row.get("is_future"):
            continue
        if row.get("surprise_pct") is None:
            continue
        last_completed = row
        break

    if last_completed is None:
        return {
            "ticker": ticker,
            "article_count": 0,
            "mean_sentiment_score": None,
            "mean_relevance_score": None,
            "label_counts": label_counts,
            "source": "yahoo_earnings",
            "last_scored": _today_iso(),
            "surprise_pct": None,
            "earnings_date": None,
        }

    surprise = float(last_completed["surprise_pct"])
    classified = _classify_surprise(surprise)
    label_counts[classified["label"]] = 1

    return {
        "ticker": ticker,
        "article_count": 1,
        "mean_sentiment_score": classified["score"],
        "mean_relevance_score": 1.0,
        "label_counts": label_counts,
        "source": "yahoo_earnings",
        "last_scored": _today_iso(),
        "surprise_pct": surprise,
        "earnings_date": last_completed.get("date"),
    }


def _action_label(direction: float) -> str:
    """Bucket a single action's direction into one of the 5 labels."""
    if direction >= 0.75:
        return "Bullish"
    if direction >= 0.25:
        return "Somewhat-Bullish"
    if direction > -0.25:
        return "Neutral"
    if direction > -0.75:
        return "Somewhat-Bearish"
    return "Bearish"


def _recency_weight(date_iso: str, today: _dt.date, window_days: int) -> float:
    """Linear-decay weight: 1.0 today -> 0.1 at window_days ago.

    A 90-day-old upgrade still counts a little; a 1-day-old one counts
    nearly fully.
    """
    try:
        d = _dt.date.fromisoformat(date_iso)
    except (ValueError, TypeError):
        return 0.1
    age = max(0, (today - d).days)
    if window_days <= 0:
        return 1.0
    frac = min(1.0, age / float(window_days))
    return 1.0 - 0.9 * frac  # 1.0 at age=0, 0.1 at age=window


def summarize_analyst_actions_for_thesis(
    actions: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Convert ``get_analyst_actions`` to a Thesis-pillar payload.

    Aggregation: weighted average of ``direction`` over recent actions,
    where recent actions weigh more. Also augment the per-action direction
    with the from/to grade delta when both are present.

    Returns the same shape as ``summarize_earnings_for_thesis``, with
    ``source='yahoo_analyst'``. ``mean_sentiment_score`` is the weighted
    direction (already in [-1, +1] roughly). ``label_counts`` records
    how many actions fell into each sentiment bucket.
    """
    label_counts = _empty_label_counts()
    ticker = ""

    if not actions:
        return {
            "ticker": ticker,
            "article_count": 0,
            "mean_sentiment_score": None,
            "mean_relevance_score": None,
            "label_counts": label_counts,
            "source": "yahoo_analyst",
            "last_scored": _today_iso(),
        }

    # Determine the time window from the oldest action present, capped at
    # 90 days. The caller picks the window via ``get_analyst_actions(days=)``.
    today = _today_date()
    oldest_days = 0
    for row in actions:
        try:
            d = _dt.date.fromisoformat(str(row.get("date") or ""))
            oldest_days = max(oldest_days, (today - d).days)
        except (ValueError, TypeError):
            continue
    window_days = max(oldest_days, 30)  # at least a 30-day window for the decay

    total_weight = 0.0
    weighted_sum = 0.0
    counted = 0
    for row in actions:
        if not isinstance(row, dict):
            continue
        if not ticker:
            ticker = str(row.get("ticker") or "").upper()
        direction = _coerce_float(row.get("direction"))
        if direction is None:
            direction = 0.0

        # Augment direction with from/to delta if available.
        f_score = _grade_score(row.get("from_grade"))
        t_score = _grade_score(row.get("to_grade"))
        if f_score is not None and t_score is not None:
            delta = t_score - f_score
            # Scale delta into [-1, +1] roughly. delta range is about [-2, +2].
            grade_signal = max(-1.0, min(1.0, delta))
            # Weighted blend: 60% action verb, 40% grade delta. If the
            # action verb produced 0 (Maintains), grade delta dominates.
            if direction == 0.0:
                direction = grade_signal
            else:
                direction = 0.6 * direction + 0.4 * grade_signal

        date_iso = str(row.get("date") or "")
        w = _recency_weight(date_iso, today, window_days)
        weighted_sum += direction * w
        total_weight += w
        counted += 1

        label_counts[_action_label(direction)] += 1

    mean_score: Optional[float] = (
        weighted_sum / total_weight if total_weight > 0 else None
    )
    if mean_score is not None:
        # Clamp into [-1, +1] (action_direction values are already there).
        if mean_score > 1.0:
            mean_score = 1.0
        elif mean_score < -1.0:
            mean_score = -1.0

    return {
        "ticker": ticker,
        "article_count": counted,
        "mean_sentiment_score": mean_score,
        "mean_relevance_score": 1.0 if counted > 0 else None,
        "label_counts": label_counts,
        "source": "yahoo_analyst",
        "last_scored": _today_iso(),
    }


__all__ = [
    "ACTION_DIRECTION",
    "GRADE_SCORE",
    "get_earnings_dates",
    "get_analyst_actions",
    "get_recommendation_summary",
    "summarize_earnings_for_thesis",
    "summarize_analyst_actions_for_thesis",
]
