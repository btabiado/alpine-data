"""Thesis Integrity pillar.

V1 derives a 0-100 sub-score per ticker from Alpha Vantage's NEWS_SENTIMENT
batched response (PHASE_1_BUILD_SPEC.md Section 5).

The pipeline calls
:func:`lthcs.sources.alpha_vantage.get_news_sentiment` ONCE per day with
the full universe of tickers and caches the result for 24h. Each AV
response holds up to ``limit`` (default 50) recently-published articles
spanning the trailing few weeks -- in V1 that snapshot IS the
"rolling 30-day" input, so no extra rolling logic lives here.

For each ticker we delegate the per-ticker extraction to
:func:`lthcs.sources.alpha_vantage.parse_ticker_sentiment` and then:

* Map the mean sentiment score from ``[-1, +1]`` onto ``[0, 100]`` via
  :func:`lthcs.normalize.bounded_linear`.
* Apply a **confidence blend** toward the neutral 50.0 midpoint when the
  ticker was mentioned in fewer than 3 articles. The blend factor is
  ``min(article_count, 3) / 3``, so 0 articles collapses to pure
  neutral, 1-2 articles produces a damped signal, and 3+ articles
  passes the raw sentiment-derived sub-score through unmodified.

The composition is::

    raw_sentiment_sub = bounded_linear(mean_sentiment_score, -1.0, +1.0)
    confidence = min(article_count, 3) / 3
    sub_score = 50.0 + (raw_sentiment_sub - 50.0) * confidence

All math is pure -- no I/O. Tests for this module never touch the
network: AV responses are passed in directly as inline fixtures.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, Optional

from lthcs.normalize import bounded_linear
from lthcs.sources.alpha_vantage import parse_ticker_sentiment


# --- Constants --------------------------------------------------------------

# Alpha Vantage's ``ticker_sentiment_score`` is documented in [-1, +1].
_SENTIMENT_LOW = -1.0
_SENTIMENT_HIGH = 1.0

# Minimum mentions before we let the raw sentiment signal pass through
# unscaled. Below this, we blend toward the neutral 50.0 midpoint to
# reflect low confidence in a tiny sample.
_MIN_ARTICLES_FOR_FULL_CONFIDENCE = 3

# The neutral midpoint we blend toward when confidence is low.
_NEUTRAL = 50.0


# --- Public API -------------------------------------------------------------

def compute_thesis(
    ticker: str,
    av_response: Dict[str, Any],
) -> Dict[str, Any]:
    """Compute the Thesis Integrity sub-score for one ticker.

    Parameters
    ----------
    ticker:
        Subject ticker. Casing must match the entries in AV's
        ``ticker_sentiment`` list (AV upper-cases these; the underlying
        parser also upper-cases the target, so callers don't need to
        normalise themselves).
    av_response:
        The full parsed JSON dict returned by
        :func:`lthcs.sources.alpha_vantage.get_news_sentiment`. The
        pillar invokes :func:`parse_ticker_sentiment` internally to
        extract this ticker's stats from the batched response.

    Returns
    -------
    dict
        ``{"ticker", "sub_score", "components", "data_quality"}`` --
        see the spec / module docstring for the exact schema.
    """
    summary = parse_ticker_sentiment(av_response or {}, ticker)

    article_count = int(summary.get("article_count") or 0)
    mean_sent = summary.get("mean_sentiment_score")
    mean_rel = summary.get("mean_relevance_score")
    label_counts = summary.get("label_counts") or {}

    # When the ticker isn't mentioned in any article, mean_sent is None
    # and the raw sub-score collapses to the neutral midpoint -- exactly
    # what we want when there's no signal to read.
    # (bounded_linear handles NaN but not None, so we short-circuit.)
    if mean_sent is None:
        raw_sentiment_sub = _NEUTRAL
    else:
        raw_sentiment_sub = float(
            bounded_linear(mean_sent, _SENTIMENT_LOW, _SENTIMENT_HIGH)
        )

    # Confidence blend in [0, 1]. 0 mentions -> 0, 3+ mentions -> 1.
    capped = min(article_count, _MIN_ARTICLES_FOR_FULL_CONFIDENCE)
    confidence = capped / float(_MIN_ARTICLES_FOR_FULL_CONFIDENCE)

    sub_score = _NEUTRAL + (raw_sentiment_sub - _NEUTRAL) * confidence
    sub_score = round(float(sub_score), 1)

    return {
        "ticker": summary.get("ticker", ticker.upper()),
        "sub_score": sub_score,
        "components": {
            "article_count": article_count,
            "mean_sentiment_score": mean_sent,
            "mean_relevance_score": mean_rel,
            "label_counts": label_counts,
            "sentiment_subscore_raw": raw_sentiment_sub,
            "confidence_blend": float(confidence),
        },
        "data_quality": {
            "has_sentiment": mean_sent is not None,
            "article_count_sufficient": article_count
            >= _MIN_ARTICLES_FOR_FULL_CONFIDENCE,
        },
    }


# --- Phase 2 (rotation) helpers --------------------------------------------


def days_between_iso(
    earlier: Optional[str],
    later: Optional[str],
) -> Optional[int]:
    """Return ``later - earlier`` in whole days, or ``None`` on bad input.

    Both arguments must be ISO ``YYYY-MM-DD`` strings. If either is
    missing, empty, or unparseable, the function returns ``None``
    rather than raising -- callers use this to compute staleness and
    can fall back to "unknown" semantics when the file lacks a date.

    The result may be negative if ``later`` is before ``earlier``;
    the staleness gate clamps with ``max(0, ...)`` so callers don't
    need to worry about a clock-skew sentiment file showing 0 days
    rather than -1.
    """
    if not earlier or not later:
        return None
    try:
        d1 = date.fromisoformat(str(earlier))
        d2 = date.fromisoformat(str(later))
    except (ValueError, TypeError):
        return None
    return (d2 - d1).days


def _today_iso(today: Optional[str]) -> str:
    """Return either the supplied ISO date or real today's ISO date."""
    if today:
        return today
    return datetime.utcnow().date().isoformat()


def compute_thesis_from_stored_sentiment(
    ticker: str,
    sentiment_dict: Optional[Dict[str, Any]],
    *,
    today: Optional[str] = None,
    max_staleness_days: int = 3,
) -> Dict[str, Any]:
    """Compute Thesis Integrity sub-score from an on-disk sentiment dict.

    See module docstring + ``compute_thesis`` for the original V1
    (in-memory AV response) variant. This Phase 2 variant consumes
    the per-ticker JSON written by the rotation worker
    (``ThesisRotation.read_sentiment``) and adds a staleness gate so
    sentiment files older than ``max_staleness_days`` collapse to
    neutral 50.0 -- with extra ``data_quality`` fields so the
    narrative/UI can explain WHY a ticker is sitting at neutral.

    Composition math is identical to the V1 function::

        raw = bounded_linear(mean_sentiment_score, -1.0, +1.0)   # 50 if missing
        confidence = min(article_count, 3) / 3                   # in [0, 1]
        sub_score = 50.0 + (raw - 50.0) * confidence
        sub_score = round(sub_score, 1)

    Parameters
    ----------
    ticker:
        Subject ticker. Used only for the output ``ticker`` field
        (the on-disk dict's own ``ticker`` is preferred when present
        so we don't silently rename whatever the rotation worker wrote).
    sentiment_dict:
        Parsed contents of ``data/lthcs/sentiment/<TICKER>.json`` or
        ``None`` if the file doesn't exist.
    today:
        ISO date for the staleness comparison. Defaults to real today.
        Tests should pass this explicitly to avoid wall-clock flakiness.
    max_staleness_days:
        Sentiment dated more than this many days before ``today`` is
        treated as missing (neutral 50.0). Setting this to ``0`` makes
        even today's data "stale by strict policy".
    """
    today_iso = _today_iso(today)

    # --- Missing-file branch ------------------------------------------------
    if sentiment_dict is None:
        return {
            "ticker": ticker.upper() if isinstance(ticker, str) else ticker,
            "sub_score": _NEUTRAL,
            "components": {
                "article_count": 0,
                "mean_sentiment_score": None,
                "mean_relevance_score": None,
                "label_counts": {},
                "sentiment_subscore_raw": _NEUTRAL,
                "confidence_blend": 0.0,
            },
            "data_quality": {
                "has_sentiment": False,
                "article_count_sufficient": False,
                "is_stale": True,
                "days_since_scored": None,
                "last_scored": None,
            },
        }

    # --- Pull fields out of the on-disk dict (tolerate missing keys) --------
    out_ticker = sentiment_dict.get("ticker") or (
        ticker.upper() if isinstance(ticker, str) else ticker
    )
    last_scored = sentiment_dict.get("last_scored")
    article_count = int(sentiment_dict.get("article_count") or 0)
    mean_sent = sentiment_dict.get("mean_sentiment_score")
    mean_rel = sentiment_dict.get("mean_relevance_score")
    label_counts = sentiment_dict.get("label_counts") or {}

    # Staleness check. None days => unknown => treat as stale (we can't
    # prove freshness without a date).
    raw_days = days_between_iso(last_scored, today_iso)
    days_since_scored = max(0, raw_days) if raw_days is not None else None
    is_stale = (
        days_since_scored is None
        or days_since_scored > max_staleness_days
    )

    # --- Stale branch: collapse to neutral but keep diagnostic fields ------
    if is_stale:
        return {
            "ticker": out_ticker,
            "sub_score": _NEUTRAL,
            "components": {
                "article_count": article_count,
                "mean_sentiment_score": mean_sent,
                "mean_relevance_score": mean_rel,
                "label_counts": label_counts,
                "sentiment_subscore_raw": _NEUTRAL,
                "confidence_blend": 0.0,
            },
            "data_quality": {
                # has_sentiment=False here even though we technically have a
                # stored value -- the scoring layer should see this the same
                # way it sees a missing file, per the spec.
                "has_sentiment": False,
                "article_count_sufficient": False,
                "is_stale": True,
                "days_since_scored": days_since_scored,
                "last_scored": last_scored,
            },
        }

    # --- Fresh branch: same math as compute_thesis -------------------------
    if mean_sent is None:
        raw_sentiment_sub = _NEUTRAL
    else:
        raw_sentiment_sub = float(
            bounded_linear(mean_sent, _SENTIMENT_LOW, _SENTIMENT_HIGH)
        )

    capped = min(article_count, _MIN_ARTICLES_FOR_FULL_CONFIDENCE)
    confidence = capped / float(_MIN_ARTICLES_FOR_FULL_CONFIDENCE)

    sub_score = _NEUTRAL + (raw_sentiment_sub - _NEUTRAL) * confidence
    sub_score = round(float(sub_score), 1)

    return {
        "ticker": out_ticker,
        "sub_score": sub_score,
        "components": {
            "article_count": article_count,
            "mean_sentiment_score": mean_sent,
            "mean_relevance_score": mean_rel,
            "label_counts": label_counts,
            "sentiment_subscore_raw": raw_sentiment_sub,
            "confidence_blend": float(confidence),
        },
        "data_quality": {
            "has_sentiment": mean_sent is not None,
            "article_count_sufficient": article_count
            >= _MIN_ARTICLES_FOR_FULL_CONFIDENCE,
            "is_stale": False,
            "days_since_scored": days_since_scored,
            "last_scored": last_scored,
        },
    }
