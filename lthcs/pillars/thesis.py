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


# --- Finnhub recommendation-trends base path -------------------------------
#
# Analyst recommendations are the PRIMARY Thesis signal as of the dead-pillar
# fix (May 2026). Backtest validation showed thesis_integrity was constant
# 50 on 88 of 90 backfilled dates because the Alpha Vantage rotation has no
# historical archive and the supplement cascade was gated by --skip-thesis
# (always passed by the backfill orchestrator). Finnhub /stock/recommendation
# returns ~12 months of monthly buy/hold/sell buckets with as_of support, so
# we can produce real directional signal across the full backfill window.
#
# Live runs still benefit from the richer AV rotation when available:
# lthcs_daily.py Stage 4 reads this Finnhub path first, then falls back to
# stored AV-sourced sentiment via compute_thesis_from_stored_sentiment when
# Finnhub has no coverage.


# Minimum number of analysts covering a ticker before we trust the consensus.
# Below this, a single boutique broker's call would whipsaw the score.
_MIN_ANALYSTS_FOR_THESIS = 3


def compute_thesis_from_finnhub_recommendation(
    ticker: str,
    reco_signal: Optional[Dict[str, Any]],
    *,
    today: Optional[str] = None,
) -> Dict[str, Any]:
    """Compute Thesis Integrity sub-score from a Finnhub recommendation signal.

    Consumes the dict produced by
    :func:`lthcs.sources.finnhub.parse_recommendation_signal` -- a
    monthly snapshot of analyst buy/hold/sell counts with a derived
    ``consensus_score`` in ``[-1, +1]``.

    Composition math mirrors the AV-rotation path so downstream scoring
    can't tell which source produced a given sub_score::

        raw = bounded_linear(consensus_score, -1.0, +1.0)         # 50 if missing
        confidence = min(total_analysts, 3) / 3                   # in [0, 1]
        sub_score = 50.0 + (raw - 50.0) * confidence
        sub_score = round(sub_score, 1)

    Parameters
    ----------
    ticker:
        Subject ticker. Used for the output ``ticker`` field when the
        recommendation signal doesn't carry one of its own.
    reco_signal:
        Parsed Finnhub recommendation signal dict. ``None`` (or an
        empty / unsourced dict with no consensus) collapses to neutral
        50.0 with ``has_sentiment=False``.
    today:
        ISO date stored in the ``data_quality.last_scored`` field for
        downstream UI display. Defaults to real today.

    Returns
    -------
    dict
        Same shape as :func:`compute_thesis_from_stored_sentiment` so
        Stage 4 can swap sources without altering the snapshot schema.
    """
    today_iso = _today_iso(today)
    out_ticker = ticker.upper() if isinstance(ticker, str) else ticker

    # --- Missing / empty signal --> neutral with stale flag ---------------
    consensus = None
    total_analysts = 0
    buy_n = 0
    hold_n = 0
    sell_n = 0
    latest_month: Optional[str] = None
    if isinstance(reco_signal, dict):
        consensus = reco_signal.get("consensus_score")
        total_analysts = int(reco_signal.get("total_analysts") or 0)
        buy_n = int(reco_signal.get("buy_count") or 0)
        hold_n = int(reco_signal.get("hold_count") or 0)
        sell_n = int(reco_signal.get("sell_count") or 0)
        latest_month = reco_signal.get("latest_month")
        if reco_signal.get("ticker"):
            out_ticker = reco_signal["ticker"]

    insufficient = (
        consensus is None
        or total_analysts < _MIN_ANALYSTS_FOR_THESIS
    )

    label_counts = {
        "Bearish": int(sell_n),
        "Somewhat-Bearish": 0,
        "Neutral": int(hold_n),
        "Somewhat-Bullish": 0,
        "Bullish": int(buy_n),
    }

    if insufficient:
        return {
            "ticker": out_ticker,
            "sub_score": _NEUTRAL,
            "components": {
                "article_count": int(total_analysts),
                "mean_sentiment_score": (
                    float(consensus) if consensus is not None else None
                ),
                "mean_relevance_score": None,
                "label_counts": label_counts,
                "sentiment_subscore_raw": _NEUTRAL,
                "confidence_blend": 0.0,
            },
            "data_quality": {
                "has_sentiment": False,
                "article_count_sufficient": False,
                "is_stale": True,
                "days_since_scored": None,
                "last_scored": latest_month,
                "source": "finnhub_recommendation",
            },
        }

    # --- Fresh signal: same math as compute_thesis ------------------------
    raw_sentiment_sub = float(
        bounded_linear(float(consensus), _SENTIMENT_LOW, _SENTIMENT_HIGH)
    )

    # Analyst-coverage confidence: caps at full at 3+ covering analysts.
    capped = min(total_analysts, _MIN_ARTICLES_FOR_FULL_CONFIDENCE)
    confidence = capped / float(_MIN_ARTICLES_FOR_FULL_CONFIDENCE)

    sub_score = _NEUTRAL + (raw_sentiment_sub - _NEUTRAL) * confidence
    sub_score = round(float(sub_score), 1)

    return {
        "ticker": out_ticker,
        "sub_score": sub_score,
        "components": {
            "article_count": int(total_analysts),
            "mean_sentiment_score": float(consensus),
            "mean_relevance_score": 1.0,
            "label_counts": label_counts,
            "sentiment_subscore_raw": raw_sentiment_sub,
            "confidence_blend": float(confidence),
        },
        "data_quality": {
            "has_sentiment": True,
            "article_count_sufficient": total_analysts
            >= _MIN_ARTICLES_FOR_FULL_CONFIDENCE,
            "is_stale": False,
            "days_since_scored": 0,
            "last_scored": today_iso,
            "source": "finnhub_recommendation",
        },
    }


# --- Event-driven refinement (8-K + Yahoo earnings) ------------------------
#
# The Finnhub recommendation base is the PRIMARY Thesis signal (analyst
# consensus, real direction, ~12 months of history). 8-K material events
# and Yahoo earnings surprises are REFINEMENT signals: they nudge the base
# when a discrete, high-information event landed, but they do NOT replace
# the analyst consensus. Rationale: a single restatement (Item 4.02) might
# warrant a -10pt nudge on a ticker the Street still loves; an earnings
# beat by +12% deserves a small +pt bump. But we want analysts to remain
# the anchor — they're the cross-sectional differentiator.
#
# This implements Pattern C from docs/news-feeds-earnings-events.md §5.

# Weight of the events refinement when blending with the Finnhub base.
# 0.0 = base only (no refinement); 1.0 = events only (replace base).
# 0.25 leaves the base dominant while letting strong events move the
# needle ~5-10 points on extreme cases.
_EVENTS_REFINEMENT_WEIGHT = 0.25

# Sub-weights inside the events score: 8-K = 50%, Yahoo earnings = 50%.
# Both are present infrequently, so when only one fires it carries the
# full events_score by itself.
_SEC_8K_WEIGHT = 0.5
_YAHOO_EARNINGS_WEIGHT = 0.5


def _extract_event_score(signal: Optional[Dict[str, Any]]) -> Optional[float]:
    """Pull a [-1, +1] sentiment score from a refinement signal dict.

    Accepts the output shape of:
      * ``lthcs.sources.sec_8k.event_signal_for_ticker``
      * ``lthcs.sources.yahoo_events.summarize_earnings_for_thesis``
      * ``lthcs.sources.yahoo_events.summarize_analyst_actions_for_thesis``

    Returns ``None`` when:
      * the signal is None / empty / not a dict
      * ``mean_sentiment_score`` is missing or None
      * ``article_count`` is 0 (no underlying events)
      * ``mean_sentiment_score`` is 0.0 — a neutral signal is treated as
        absent rather than as "pull toward 50". This is critical for
        cross-sectional stdev: most 8-K filings are direction=0 (Item
        7.01 Reg-FD disclosures, Item 9.01 financial-statements exhibits,
        etc.) and treating them as a "pull to 50" force would flatten
        the distribution toward the midpoint and undo the analyst-
        consensus signal the base captures.

    The score is clamped to [-1, +1] defensively in case a future source
    emits something out of range.
    """
    if not isinstance(signal, dict):
        return None
    count = signal.get("article_count")
    try:
        if int(count or 0) <= 0:
            return None
    except (TypeError, ValueError):
        return None
    raw = signal.get("mean_sentiment_score")
    if raw is None:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if val == 0.0:
        # See docstring: neutral signals are treated as absent so the
        # refinement does not flatten the cross-sectional distribution.
        return None
    if val > 1.0:
        val = 1.0
    elif val < -1.0:
        val = -1.0
    return val


def _blend_events_score(
    sec_8k_score: Optional[float],
    yahoo_earnings_score: Optional[float],
) -> Optional[float]:
    """Weighted blend of refinement scores in [-1, +1].

    Both inputs are optional. If both are None, returns None
    (no refinement). If only one is present, it carries 100% weight
    (we don't dilute a real signal with absent ones). If both are
    present, blend 50/50.
    """
    have_sec = sec_8k_score is not None
    have_yh = yahoo_earnings_score is not None
    if not have_sec and not have_yh:
        return None
    if have_sec and not have_yh:
        return float(sec_8k_score)
    if have_yh and not have_sec:
        return float(yahoo_earnings_score)
    return (
        _SEC_8K_WEIGHT * float(sec_8k_score)
        + _YAHOO_EARNINGS_WEIGHT * float(yahoo_earnings_score)
    )


def compute_thesis_with_refinement(
    ticker: str,
    reco_signal: Optional[Dict[str, Any]],
    *,
    sec_8k_signal: Optional[Dict[str, Any]] = None,
    yahoo_earnings_signal: Optional[Dict[str, Any]] = None,
    stored_sentiment: Optional[Dict[str, Any]] = None,
    today: Optional[str] = None,
    events_weight: float = _EVENTS_REFINEMENT_WEIGHT,
) -> Dict[str, Any]:
    """Compute the Thesis Integrity sub-score with event-driven refinement.

    Cascade:
      1. BASE: Finnhub recommendation consensus (via
         ``compute_thesis_from_finnhub_recommendation``) if usable;
         else stored sentiment fallback via
         ``compute_thesis_from_stored_sentiment``; else neutral 50.
      2. REFINEMENT: blend ``sec_8k_signal`` and ``yahoo_earnings_signal``
         into a single events_score in [-1, +1], map to 0-100, and blend
         with the base using ``events_weight``::

             sub_score = base * (1 - w) + events_subscore * w

    The refinement is intentionally a SMALL adjustment by default (w=0.25)
    so analyst consensus remains the anchor. The base path also still runs
    when no events are present — refinement is purely additive.

    Components surfaces:
      * ``has_sec_8k`` (bool) — whether the 8-K refinement fired
      * ``has_yahoo_earnings`` (bool) — whether the Yahoo earnings
        refinement fired
      * ``sec_8k_score`` (float in [-1, +1] or None) — raw 8-K sentiment
      * ``yahoo_earnings_score`` (float in [-1, +1] or None) — raw
        Yahoo earnings sentiment
      * ``events_score_raw`` (float in [0, 100] or None) — events score
        mapped to 0-100 (matches ``sentiment_subscore_raw`` scale)
      * ``events_weight`` (float) — the refinement weight used
      * ``base_sub_score`` (float in [0, 100]) — what the sub_score would
        have been pre-refinement

    Parameters
    ----------
    ticker:
        Subject ticker.
    reco_signal:
        Finnhub recommendation signal dict (see
        ``compute_thesis_from_finnhub_recommendation``). Pass ``None``
        when Finnhub has no coverage; the function falls back through
        ``stored_sentiment`` or to a neutral base.
    sec_8k_signal:
        Output of ``sec_8k.event_signal_for_ticker``. Refinement only
        fires when ``article_count > 0`` and ``mean_sentiment_score``
        is a real float.
    yahoo_earnings_signal:
        Output of ``yahoo_events.summarize_earnings_for_thesis``.
        Same gates as 8-K.
    stored_sentiment:
        Fallback AV-rotation sentiment dict consumed by
        ``compute_thesis_from_stored_sentiment``. Only used when
        Finnhub doesn't produce a usable base.
    today:
        ISO date for ``last_scored``. Defaults to today.
    events_weight:
        Blend weight for the events refinement (in [0, 1]). Default
        0.25 keeps analyst consensus dominant; tests pass 0.0 to
        verify the base path is preserved verbatim.

    Returns
    -------
    dict
        Same top-level shape as ``compute_thesis_from_finnhub_recommendation``,
        but with extra component fields documenting the refinement.

    Notes
    -----
    * The base path is preserved exactly when both refinement signals
      are missing — this function reduces to the prior Finnhub-or-AV
      cascade with extra ``has_sec_8k=False`` / ``has_yahoo_earnings=False``
      markers.
    * Refinement never moves a sub_score outside [0, 100]: the
      events_subscore is itself in [0, 100], and a weighted average of
      two values in [0, 100] stays in [0, 100].
    """
    # --- Step 1: compute the base sub_score ------------------------------
    base_result: Dict[str, Any]
    used_finnhub_base = False
    if reco_signal is not None:
        consensus = reco_signal.get("consensus_score")
        total = int(reco_signal.get("total_analysts") or 0)
        if consensus is not None and total >= _MIN_ANALYSTS_FOR_THESIS:
            base_result = compute_thesis_from_finnhub_recommendation(
                ticker, reco_signal, today=today
            )
            used_finnhub_base = True
        else:
            base_result = compute_thesis_from_finnhub_recommendation(
                ticker, reco_signal, today=today
            )
    elif stored_sentiment is not None:
        base_result = compute_thesis_from_stored_sentiment(
            ticker, stored_sentiment, today=today
        )
    else:
        base_result = compute_thesis_from_finnhub_recommendation(
            ticker, None, today=today
        )

    base_sub_score = float(base_result.get("sub_score", _NEUTRAL))

    # --- Step 2: extract refinement scores -------------------------------
    sec_8k_score = _extract_event_score(sec_8k_signal)
    yahoo_score = _extract_event_score(yahoo_earnings_signal)
    has_sec_8k = sec_8k_score is not None
    has_yahoo = yahoo_score is not None

    # --- Step 3: blend events into a single score ------------------------
    events_score = _blend_events_score(sec_8k_score, yahoo_score)

    if events_score is None:
        # No refinement applies — return the base with marker fields set
        # so downstream variable_detail rows are uniform in shape.
        components = dict(base_result.get("components") or {})
        components["has_sec_8k"] = False
        components["has_yahoo_earnings"] = False
        components["sec_8k_score"] = None
        components["yahoo_earnings_score"] = None
        components["events_score_raw"] = None
        components["events_weight"] = float(events_weight)
        components["base_sub_score"] = base_sub_score
        out = dict(base_result)
        out["components"] = components
        return out

    # Map events_score from [-1, +1] to [0, 100] using the same
    # bounded_linear the base path uses for analyst consensus.
    events_subscore = float(
        bounded_linear(float(events_score), _SENTIMENT_LOW, _SENTIMENT_HIGH)
    )

    # Clamp the weight defensively. A negative or >1 weight here would
    # produce a nonsensical sub_score; failing closed (clamp) is gentler
    # than raising in production.
    w = float(events_weight)
    if w < 0.0:
        w = 0.0
    elif w > 1.0:
        w = 1.0

    refined = base_sub_score * (1.0 - w) + events_subscore * w
    # Defensive clamp (mathematically already in [0, 100] given both inputs).
    if refined > 100.0:
        refined = 100.0
    elif refined < 0.0:
        refined = 0.0
    refined = round(refined, 1)

    components = dict(base_result.get("components") or {})
    components["has_sec_8k"] = bool(has_sec_8k)
    components["has_yahoo_earnings"] = bool(has_yahoo)
    components["sec_8k_score"] = (
        float(sec_8k_score) if sec_8k_score is not None else None
    )
    components["yahoo_earnings_score"] = (
        float(yahoo_score) if yahoo_score is not None else None
    )
    components["events_score_raw"] = round(events_subscore, 2)
    components["events_weight"] = w
    components["base_sub_score"] = base_sub_score

    data_quality = dict(base_result.get("data_quality") or {})
    # A refinement always means we have *some* signal — even if the base
    # was neutral/stale, an 8-K event is a real data point. Don't flip
    # has_sentiment on the basis of refinement alone, but do mark the
    # refinement source.
    data_quality["has_events_refinement"] = True
    # Track which event sources contributed for the audit trail.
    refinement_sources: list = []
    if has_sec_8k:
        refinement_sources.append("sec_8k")
    if has_yahoo:
        refinement_sources.append("yahoo_earnings")
    data_quality["events_refinement_sources"] = refinement_sources

    return {
        "ticker": base_result.get("ticker", ticker.upper() if isinstance(ticker, str) else ticker),
        "sub_score": refined,
        "components": components,
        "data_quality": data_quality,
    }

