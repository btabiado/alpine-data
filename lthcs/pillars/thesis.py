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

from typing import Any, Dict

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
