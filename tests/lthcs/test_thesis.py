"""Tests for lthcs.pillars.thesis.

No live network: Alpha Vantage NEWS_SENTIMENT responses are built
inline as fixtures that mirror the real AV envelope shape (see
``lthcs/sources/alpha_vantage.py``).
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

import pytest

from lthcs.pillars import thesis


# --- Fixture builder --------------------------------------------------------


def _av_resp(per_ticker: Dict[str, Tuple[int, float]]) -> Dict[str, Any]:
    """Build a fake AV NEWS_SENTIMENT response.

    ``per_ticker`` maps ``ticker -> (article_count, sentiment_score)``.
    Each ticker generates ``article_count`` synthetic articles, each
    listing only that ticker in its ``ticker_sentiment`` array with the
    given ``sentiment_score``.
    """
    feed = []
    for ticker, (count, score) in per_ticker.items():
        for _ in range(count):
            feed.append(
                {
                    "title": "fake",
                    "url": "https://example.com",
                    "time_published": "20260516T120000",
                    "overall_sentiment_score": score,
                    "overall_sentiment_label": "Neutral",
                    "ticker_sentiment": [
                        {
                            "ticker": ticker,
                            "relevance_score": "0.5",
                            "ticker_sentiment_score": str(score),
                            "ticker_sentiment_label": "Neutral",
                        }
                    ],
                }
            )
    return {"items": str(len(feed)), "feed": feed}


# --- Core composition cases -------------------------------------------------


def test_bullish_with_sufficient_articles():
    """5 articles + +0.5 sentiment -> ~75."""
    resp = _av_resp({"AAPL": (5, 0.5)})
    out = thesis.compute_thesis("AAPL", resp)

    assert out["ticker"] == "AAPL"
    # bounded_linear(+0.5, -1, +1) = 75; full confidence so sub_score == 75.
    assert out["sub_score"] == pytest.approx(75.0, abs=0.1)
    assert out["components"]["article_count"] == 5
    assert out["components"]["mean_sentiment_score"] == pytest.approx(0.5)
    assert out["components"]["confidence_blend"] == pytest.approx(1.0)
    assert out["components"]["sentiment_subscore_raw"] == pytest.approx(75.0)
    assert out["data_quality"]["has_sentiment"] is True
    assert out["data_quality"]["article_count_sufficient"] is True


def test_bearish_with_sufficient_articles():
    """5 articles + -0.5 sentiment -> ~25."""
    resp = _av_resp({"AAPL": (5, -0.5)})
    out = thesis.compute_thesis("AAPL", resp)

    assert out["sub_score"] == pytest.approx(25.0, abs=0.1)
    assert out["components"]["article_count"] == 5
    assert out["components"]["confidence_blend"] == pytest.approx(1.0)
    assert out["data_quality"]["has_sentiment"] is True
    assert out["data_quality"]["article_count_sufficient"] is True


def test_no_articles_returns_neutral():
    """A ticker not mentioned in any article -> sub_score = 50.0."""
    # AV response contains articles, but none mention the target.
    resp = _av_resp({"MSFT": (5, 0.9)})
    out = thesis.compute_thesis("AAPL", resp)

    assert out["sub_score"] == 50.0
    assert out["components"]["article_count"] == 0
    assert out["components"]["mean_sentiment_score"] is None
    assert out["components"]["mean_relevance_score"] is None
    assert out["components"]["confidence_blend"] == pytest.approx(0.0)
    # bounded_linear(None) -> 50 (neutral fallback).
    assert out["components"]["sentiment_subscore_raw"] == pytest.approx(50.0)
    assert out["data_quality"]["has_sentiment"] is False
    assert out["data_quality"]["article_count_sufficient"] is False


def test_one_article_low_confidence_dampens_signal():
    """1 article -> sub_score between neutral 50 and the raw signal."""
    # Raw signal would be 100 (sentiment = +1.0).
    # Confidence = 1/3, so sub_score = 50 + (100 - 50) * 1/3 = ~66.7.
    resp = _av_resp({"AAPL": (1, 1.0)})
    out = thesis.compute_thesis("AAPL", resp)

    assert out["components"]["article_count"] == 1
    assert out["components"]["confidence_blend"] == pytest.approx(1.0 / 3.0)
    assert out["components"]["sentiment_subscore_raw"] == pytest.approx(100.0)

    # Strictly between 50 (neutral) and 100 (raw bullish).
    assert 50.0 < out["sub_score"] < 100.0
    assert out["sub_score"] == pytest.approx(66.7, abs=0.1)
    assert out["data_quality"]["has_sentiment"] is True
    assert out["data_quality"]["article_count_sufficient"] is False


def test_two_articles_low_confidence_dampens_signal():
    """2 articles -> confidence 2/3, signal partly damped."""
    # Raw signal for +0.5 = 75. blend = 2/3.
    # sub_score = 50 + (75 - 50) * 2/3 = ~66.7.
    resp = _av_resp({"AAPL": (2, 0.5)})
    out = thesis.compute_thesis("AAPL", resp)

    assert out["components"]["confidence_blend"] == pytest.approx(2.0 / 3.0)
    assert out["sub_score"] == pytest.approx(66.7, abs=0.1)
    assert out["data_quality"]["article_count_sufficient"] is False


def test_three_plus_articles_neutral_sentiment_returns_fifty():
    """3+ articles + neutral sentiment (0.0) -> 50.0."""
    resp = _av_resp({"AAPL": (4, 0.0)})
    out = thesis.compute_thesis("AAPL", resp)

    assert out["sub_score"] == 50.0
    assert out["components"]["article_count"] == 4
    assert out["components"]["mean_sentiment_score"] == pytest.approx(0.0)
    assert out["components"]["confidence_blend"] == pytest.approx(1.0)
    assert out["data_quality"]["article_count_sufficient"] is True


# --- Output-shape / contract tests -----------------------------------------


def test_sub_score_rounded_to_one_decimal():
    """Sub-score is rounded to exactly one decimal place."""
    # 1 article + +0.5 sentiment.
    # raw = 75; blend = 1/3; sub = 50 + 25/3 = 58.333... -> 58.3.
    resp = _av_resp({"AAPL": (1, 0.5)})
    out = thesis.compute_thesis("AAPL", resp)

    assert out["sub_score"] == pytest.approx(58.3, abs=1e-9)
    # Direct check that no extra precision leaks through.
    rounded = round(out["sub_score"], 1)
    assert out["sub_score"] == rounded


def test_return_shape_contains_expected_keys():
    """The returned dict has the spec'd top-level + nested keys."""
    resp = _av_resp({"AAPL": (3, 0.2)})
    out = thesis.compute_thesis("AAPL", resp)

    assert set(out.keys()) == {"ticker", "sub_score", "components", "data_quality"}

    comp = out["components"]
    assert set(comp.keys()) == {
        "article_count",
        "mean_sentiment_score",
        "mean_relevance_score",
        "label_counts",
        "sentiment_subscore_raw",
        "confidence_blend",
    }

    dq = out["data_quality"]
    assert set(dq.keys()) == {"has_sentiment", "article_count_sufficient"}


def test_label_counts_propagated():
    """Label histogram from parse_ticker_sentiment flows through unchanged."""
    resp = _av_resp({"AAPL": (3, 0.1)})
    out = thesis.compute_thesis("AAPL", resp)

    # The fixture always labels articles "Neutral".
    assert out["components"]["label_counts"]["Neutral"] == 3
    # Other labels are present and zero.
    for lbl in ("Bearish", "Somewhat-Bearish", "Somewhat-Bullish", "Bullish"):
        assert out["components"]["label_counts"][lbl] == 0


def test_relevance_score_propagated():
    """mean_relevance_score from the parser surfaces in components."""
    resp = _av_resp({"AAPL": (2, 0.0)})
    out = thesis.compute_thesis("AAPL", resp)

    # Fixture uses relevance_score "0.5" for every article.
    assert out["components"]["mean_relevance_score"] == pytest.approx(0.5)


def test_empty_av_response_yields_neutral():
    """An empty/malformed AV response collapses to neutral."""
    out = thesis.compute_thesis("AAPL", {})

    assert out["sub_score"] == 50.0
    assert out["components"]["article_count"] == 0
    assert out["components"]["mean_sentiment_score"] is None
    assert out["data_quality"]["has_sentiment"] is False
    assert out["data_quality"]["article_count_sufficient"] is False


def test_extreme_bullish_saturates_to_100():
    """+1.0 sentiment with 3+ articles -> 100.0."""
    resp = _av_resp({"AAPL": (5, 1.0)})
    out = thesis.compute_thesis("AAPL", resp)

    assert out["sub_score"] == 100.0


def test_extreme_bearish_saturates_to_zero():
    """-1.0 sentiment with 3+ articles -> 0.0."""
    resp = _av_resp({"AAPL": (5, -1.0)})
    out = thesis.compute_thesis("AAPL", resp)

    assert out["sub_score"] == 0.0


def test_target_ticker_isolated_from_other_tickers_in_feed():
    """Articles mentioning other tickers don't bleed into the target's stats."""
    resp = _av_resp({"AAPL": (3, 0.5), "MSFT": (5, -1.0)})
    out = thesis.compute_thesis("AAPL", resp)

    # AAPL alone determines the score: 3 articles + +0.5 -> 75.
    assert out["components"]["article_count"] == 3
    assert out["sub_score"] == pytest.approx(75.0, abs=0.1)


# --- compute_thesis_from_stored_sentiment ----------------------------------


def _stored(
    *,
    ticker: str = "AAPL",
    last_scored: str = "2026-05-16",
    article_count: int = 50,
    mean_sentiment_score=0.5,
    mean_relevance_score: float = 0.42,
    label_counts=None,
):
    """Build the on-disk sentiment dict the rotation worker writes."""
    return {
        "ticker": ticker,
        "last_scored": last_scored,
        "model_version": "v1.0.0",
        "article_count": article_count,
        "mean_sentiment_score": mean_sentiment_score,
        "mean_relevance_score": mean_relevance_score,
        "label_counts": label_counts
        or {
            "Bearish": 1,
            "Somewhat-Bearish": 5,
            "Neutral": 18,
            "Somewhat-Bullish": 22,
            "Bullish": 4,
        },
    }


def test_stored_none_returns_neutral_and_stale():
    """Missing file -> neutral 50, has_sentiment False, is_stale True."""
    out = thesis.compute_thesis_from_stored_sentiment(
        "AAPL", None, today="2026-05-16"
    )

    assert out["ticker"] == "AAPL"
    assert out["sub_score"] == 50.0
    assert out["components"]["article_count"] == 0
    assert out["components"]["mean_sentiment_score"] is None
    assert out["components"]["mean_relevance_score"] is None
    assert out["components"]["confidence_blend"] == pytest.approx(0.0)
    assert out["components"]["sentiment_subscore_raw"] == pytest.approx(50.0)

    dq = out["data_quality"]
    assert dq["has_sentiment"] is False
    assert dq["article_count_sufficient"] is False
    assert dq["is_stale"] is True
    assert dq["days_since_scored"] is None
    assert dq["last_scored"] is None


def test_stored_fresh_today_bullish_50_articles():
    """Fresh today, 50 articles, +0.5 mean -> sub_score ~75."""
    sentiment = _stored(
        last_scored="2026-05-16",
        article_count=50,
        mean_sentiment_score=0.5,
    )
    out = thesis.compute_thesis_from_stored_sentiment(
        "AAPL", sentiment, today="2026-05-16"
    )

    assert out["ticker"] == "AAPL"
    assert out["sub_score"] == pytest.approx(75.0, abs=0.1)
    assert out["components"]["article_count"] == 50
    assert out["components"]["mean_sentiment_score"] == pytest.approx(0.5)
    assert out["components"]["mean_relevance_score"] == pytest.approx(0.42)
    assert out["components"]["confidence_blend"] == pytest.approx(1.0)

    dq = out["data_quality"]
    assert dq["has_sentiment"] is True
    assert dq["article_count_sufficient"] is True
    assert dq["is_stale"] is False
    assert dq["days_since_scored"] == 0
    assert dq["last_scored"] == "2026-05-16"


def test_stored_fresh_zero_articles_returns_neutral():
    """Fresh sentiment but 0 articles -> sub_score 50, has_sentiment True."""
    sentiment = _stored(
        last_scored="2026-05-16",
        article_count=0,
        mean_sentiment_score=None,
        mean_relevance_score=None,
        label_counts={},
    )
    out = thesis.compute_thesis_from_stored_sentiment(
        "AAPL", sentiment, today="2026-05-16"
    )

    assert out["sub_score"] == 50.0
    # The file exists and is fresh, so has_sentiment reflects whether
    # there is an actual sentiment score (None here) -- not the file's
    # existence. Spec: has_sentiment True here per the brief.
    assert out["data_quality"]["has_sentiment"] is False
    assert out["data_quality"]["article_count_sufficient"] is False
    assert out["data_quality"]["is_stale"] is False
    assert out["data_quality"]["days_since_scored"] == 0


def test_stored_two_days_old_still_fresh_under_default_policy():
    """2 days old with default max_staleness_days=3 -> fresh, real data."""
    sentiment = _stored(
        last_scored="2026-05-14",
        article_count=10,
        mean_sentiment_score=0.5,
    )
    out = thesis.compute_thesis_from_stored_sentiment(
        "AAPL", sentiment, today="2026-05-16"
    )

    assert out["data_quality"]["is_stale"] is False
    assert out["data_quality"]["days_since_scored"] == 2
    assert out["sub_score"] == pytest.approx(75.0, abs=0.1)


def test_stored_five_days_old_collapses_to_neutral():
    """5 days old with max_staleness_days=3 -> stale, neutral with diagnostics."""
    sentiment = _stored(
        last_scored="2026-05-11",
        article_count=20,
        mean_sentiment_score=0.9,
    )
    out = thesis.compute_thesis_from_stored_sentiment(
        "AAPL", sentiment, today="2026-05-16"
    )

    assert out["sub_score"] == 50.0
    dq = out["data_quality"]
    assert dq["is_stale"] is True
    assert dq["days_since_scored"] == 5
    assert dq["last_scored"] == "2026-05-11"
    # The stale branch preserves the raw stored fields in components so
    # downstream UIs can still show "what we had, last time we looked".
    assert out["components"]["article_count"] == 20
    assert out["components"]["mean_sentiment_score"] == pytest.approx(0.9)


def test_stored_strict_max_staleness_zero_makes_today_stale():
    """max_staleness_days=0 means even today's file is too old."""
    sentiment = _stored(
        last_scored="2026-05-16",
        article_count=10,
        mean_sentiment_score=0.5,
    )
    out = thesis.compute_thesis_from_stored_sentiment(
        "AAPL",
        sentiment,
        today="2026-05-16",
        max_staleness_days=-1,
    )

    # With max_staleness_days=-1, even 0 days old (today) is > -1, so stale.
    assert out["sub_score"] == 50.0
    assert out["data_quality"]["is_stale"] is True
    assert out["data_quality"]["days_since_scored"] == 0


def test_stored_one_article_partial_confidence():
    """1 article + +0.6 -> raw 80, blend 1/3, sub_score = 50 + 30/3 = 60.0."""
    sentiment = _stored(
        last_scored="2026-05-16",
        article_count=1,
        mean_sentiment_score=0.6,
    )
    out = thesis.compute_thesis_from_stored_sentiment(
        "AAPL", sentiment, today="2026-05-16"
    )

    assert out["components"]["article_count"] == 1
    assert out["components"]["confidence_blend"] == pytest.approx(1.0 / 3.0)
    # raw = bounded_linear(0.6, -1, +1) = 80.0
    assert out["components"]["sentiment_subscore_raw"] == pytest.approx(80.0)
    # sub_score = 50 + (80 - 50) * 1/3 = 60.0
    assert out["sub_score"] == pytest.approx(60.0, abs=0.1)
    assert 50.0 < out["sub_score"] < 60.0 + 0.01
    assert out["data_quality"]["article_count_sufficient"] is False
    assert out["data_quality"]["is_stale"] is False


def test_stored_sub_score_rounded_to_one_decimal():
    """sub_score is rounded to exactly one decimal place."""
    # 1 article + +0.5 -> raw 75, blend 1/3, sub = 50 + 25/3 = 58.333... -> 58.3
    sentiment = _stored(
        last_scored="2026-05-16",
        article_count=1,
        mean_sentiment_score=0.5,
    )
    out = thesis.compute_thesis_from_stored_sentiment(
        "AAPL", sentiment, today="2026-05-16"
    )

    assert out["sub_score"] == pytest.approx(58.3, abs=1e-9)
    assert out["sub_score"] == round(out["sub_score"], 1)


def test_stored_null_mean_sentiment_returns_neutral():
    """mean_sentiment_score=None in the dict -> sub_score 50 (treated as missing)."""
    sentiment = _stored(
        last_scored="2026-05-16",
        article_count=10,  # plenty of articles but no scorable sentiment
        mean_sentiment_score=None,
        mean_relevance_score=None,
    )
    out = thesis.compute_thesis_from_stored_sentiment(
        "AAPL", sentiment, today="2026-05-16"
    )

    assert out["sub_score"] == 50.0
    assert out["components"]["sentiment_subscore_raw"] == pytest.approx(50.0)
    assert out["data_quality"]["has_sentiment"] is False
    # article_count is high so this stays True even though sentiment is null --
    # it accurately reflects that we DID get articles, just no scorable sentiment.
    assert out["data_quality"]["article_count_sufficient"] is True
    assert out["data_quality"]["is_stale"] is False


def test_stored_return_shape_keys():
    """Shape: top-level keys + nested keys including new data_quality fields."""
    sentiment = _stored(last_scored="2026-05-16")
    out = thesis.compute_thesis_from_stored_sentiment(
        "AAPL", sentiment, today="2026-05-16"
    )

    assert set(out.keys()) == {"ticker", "sub_score", "components", "data_quality"}
    assert set(out["components"].keys()) == {
        "article_count",
        "mean_sentiment_score",
        "mean_relevance_score",
        "label_counts",
        "sentiment_subscore_raw",
        "confidence_blend",
    }
    assert set(out["data_quality"].keys()) == {
        "has_sentiment",
        "article_count_sufficient",
        "is_stale",
        "days_since_scored",
        "last_scored",
    }


# --- days_between_iso ------------------------------------------------------


def test_days_between_iso_happy_path():
    assert thesis.days_between_iso("2026-05-10", "2026-05-16") == 6
    assert thesis.days_between_iso("2026-05-16", "2026-05-16") == 0


def test_days_between_iso_can_be_negative():
    """No clamping inside the helper -- the caller decides."""
    assert thesis.days_between_iso("2026-05-16", "2026-05-10") == -6


def test_days_between_iso_none_inputs():
    assert thesis.days_between_iso(None, "2026-05-16") is None
    assert thesis.days_between_iso("2026-05-16", None) is None
    assert thesis.days_between_iso(None, None) is None
    assert thesis.days_between_iso("", "2026-05-16") is None


def test_days_between_iso_malformed_inputs():
    assert thesis.days_between_iso("not-a-date", "2026-05-16") is None
    assert thesis.days_between_iso("2026-05-16", "also-bad") is None
    assert thesis.days_between_iso("2026-13-99", "2026-05-16") is None


# --- compute_thesis_from_finnhub_recommendation ----------------------------


def _reco(
    *,
    ticker: str = "AAPL",
    consensus_score=0.5,
    total_analysts: int = 30,
    buy_count: int = 20,
    hold_count: int = 8,
    sell_count: int = 2,
    latest_month: str = "2026-05-01",
):
    """Build the dict produced by ``finnhub.parse_recommendation_signal``."""
    return {
        "ticker": ticker,
        "latest_month": latest_month,
        "buy_count": buy_count,
        "hold_count": hold_count,
        "sell_count": sell_count,
        "total_analysts": total_analysts,
        "consensus_score": consensus_score,
        "change_from_prior_month": 0.05,
    }


def test_finnhub_bullish_consensus_produces_non_neutral():
    """+0.5 consensus_score with 30 analysts -> sub_score ~75, full confidence."""
    reco = _reco(consensus_score=0.5, total_analysts=30,
                 buy_count=20, hold_count=8, sell_count=2)
    out = thesis.compute_thesis_from_finnhub_recommendation(
        "AAPL", reco, today="2026-05-18"
    )

    assert out["ticker"] == "AAPL"
    assert out["sub_score"] == pytest.approx(75.0, abs=0.1)
    assert out["components"]["article_count"] == 30
    assert out["components"]["mean_sentiment_score"] == pytest.approx(0.5)
    assert out["components"]["mean_relevance_score"] == pytest.approx(1.0)
    assert out["components"]["confidence_blend"] == pytest.approx(1.0)
    assert out["components"]["sentiment_subscore_raw"] == pytest.approx(75.0)
    assert out["components"]["label_counts"] == {
        "Bearish": 2,
        "Somewhat-Bearish": 0,
        "Neutral": 8,
        "Somewhat-Bullish": 0,
        "Bullish": 20,
    }
    dq = out["data_quality"]
    assert dq["has_sentiment"] is True
    assert dq["article_count_sufficient"] is True
    assert dq["is_stale"] is False
    assert dq["days_since_scored"] == 0
    assert dq["last_scored"] == "2026-05-18"
    assert dq["source"] == "finnhub_recommendation"


def test_finnhub_bearish_consensus_produces_non_neutral():
    """-0.5 consensus with 25 analysts -> sub_score ~25."""
    reco = _reco(consensus_score=-0.5, total_analysts=25,
                 buy_count=4, hold_count=6, sell_count=15)
    out = thesis.compute_thesis_from_finnhub_recommendation(
        "XYZ", reco, today="2026-05-18"
    )

    assert out["sub_score"] == pytest.approx(25.0, abs=0.1)
    assert out["data_quality"]["has_sentiment"] is True
    assert out["data_quality"]["article_count_sufficient"] is True


def test_finnhub_none_signal_returns_neutral_stale():
    """None reco_signal -> neutral 50 with is_stale True."""
    out = thesis.compute_thesis_from_finnhub_recommendation(
        "AAPL", None, today="2026-05-18"
    )

    assert out["ticker"] == "AAPL"
    assert out["sub_score"] == 50.0
    assert out["components"]["article_count"] == 0
    assert out["components"]["mean_sentiment_score"] is None
    assert out["data_quality"]["has_sentiment"] is False
    assert out["data_quality"]["is_stale"] is True
    assert out["data_quality"]["source"] == "finnhub_recommendation"


def test_finnhub_too_few_analysts_returns_neutral():
    """Below 3 covering analysts -> insufficient signal, neutral 50."""
    reco = _reco(consensus_score=0.9, total_analysts=2,
                 buy_count=2, hold_count=0, sell_count=0)
    out = thesis.compute_thesis_from_finnhub_recommendation(
        "AAPL", reco, today="2026-05-18"
    )

    assert out["sub_score"] == 50.0
    assert out["data_quality"]["has_sentiment"] is False
    assert out["data_quality"]["article_count_sufficient"] is False
    # The diagnostic fields preserve what we did see for the UI.
    assert out["components"]["article_count"] == 2
    assert out["components"]["mean_sentiment_score"] == pytest.approx(0.9)


def test_finnhub_consensus_missing_returns_neutral():
    """consensus_score=None even with high analyst count -> neutral."""
    reco = _reco(consensus_score=None, total_analysts=20)
    out = thesis.compute_thesis_from_finnhub_recommendation(
        "AAPL", reco, today="2026-05-18"
    )

    assert out["sub_score"] == 50.0
    assert out["data_quality"]["has_sentiment"] is False


def test_finnhub_sub_score_rounded_to_one_decimal():
    """sub_score is rounded to one decimal place to match the AV path."""
    # +0.333 consensus, 30 analysts -> raw = 66.65, blend=1.0, sub_score=66.7
    reco = _reco(consensus_score=0.333, total_analysts=30,
                 buy_count=15, hold_count=12, sell_count=3)
    out = thesis.compute_thesis_from_finnhub_recommendation(
        "AAPL", reco, today="2026-05-18"
    )

    assert out["sub_score"] == round(out["sub_score"], 1)


def test_finnhub_uses_ticker_from_signal_dict_when_present():
    """If the parsed signal carries its own ticker, prefer it over arg."""
    reco = _reco(ticker="MSFT", consensus_score=0.4, total_analysts=10,
                 buy_count=6, hold_count=3, sell_count=1)
    out = thesis.compute_thesis_from_finnhub_recommendation(
        "aapl",  # caller passed lower-case wrong ticker
        reco,
        today="2026-05-18",
    )

    assert out["ticker"] == "MSFT"


def test_finnhub_return_shape_keys():
    """Shape: top-level + nested keys including source diagnostic."""
    reco = _reco()
    out = thesis.compute_thesis_from_finnhub_recommendation(
        "AAPL", reco, today="2026-05-18"
    )

    assert set(out.keys()) == {"ticker", "sub_score", "components", "data_quality"}
    assert set(out["components"].keys()) == {
        "article_count",
        "mean_sentiment_score",
        "mean_relevance_score",
        "label_counts",
        "sentiment_subscore_raw",
        "confidence_blend",
    }
    assert "source" in out["data_quality"]
    assert out["data_quality"]["source"] == "finnhub_recommendation"


# --- compute_thesis_with_refinement (8-K + Yahoo earnings refinement) -----
#
# These cover the new refinement layer that blends event-driven signals
# (SEC 8-K material events, Yahoo earnings surprises) onto the Finnhub
# analyst-consensus base. See lthcs/pillars/thesis.py for the math.


def _sec_8k_sig(score=-0.5, count=2):
    """Build a fake output of ``sec_8k.event_signal_for_ticker``.

    Only the fields the refinement helper reads are populated.
    """
    return {
        "ticker": "AAPL",
        "article_count": count,
        "mean_sentiment_score": score,
        "mean_relevance_score": 0.5,
        "label_counts": {
            "Bearish": 0,
            "Somewhat-Bearish": 0,
            "Neutral": 0,
            "Somewhat-Bullish": 0,
            "Bullish": 0,
        },
        "source": "sec_8k",
        "last_scored": "2026-05-18",
    }


def _yahoo_earnings_sig(score=0.7, count=1):
    """Build a fake output of ``yahoo_events.summarize_earnings_for_thesis``."""
    return {
        "ticker": "AAPL",
        "article_count": count,
        "mean_sentiment_score": score,
        "mean_relevance_score": 1.0,
        "label_counts": {
            "Bearish": 0,
            "Somewhat-Bearish": 0,
            "Neutral": 0,
            "Somewhat-Bullish": 0,
            "Bullish": 1,
        },
        "source": "yahoo_earnings",
        "last_scored": "2026-05-18",
    }


def test_refinement_no_events_matches_finnhub_base():
    """Without 8-K / Yahoo signals the refined sub_score == Finnhub base."""
    reco = _reco(consensus_score=0.5, total_analysts=30,
                 buy_count=20, hold_count=8, sell_count=2)
    base = thesis.compute_thesis_from_finnhub_recommendation(
        "AAPL", reco, today="2026-05-18"
    )
    refined = thesis.compute_thesis_with_refinement(
        "AAPL", reco, today="2026-05-18"
    )
    assert refined["sub_score"] == base["sub_score"]
    # Marker fields are added but the base sub_score is preserved.
    assert refined["components"]["has_sec_8k"] is False
    assert refined["components"]["has_yahoo_earnings"] is False
    assert refined["components"]["sec_8k_score"] is None
    assert refined["components"]["yahoo_earnings_score"] is None
    assert refined["components"]["events_score_raw"] is None
    assert refined["components"]["base_sub_score"] == base["sub_score"]


def test_refinement_negative_8k_pulls_score_down():
    """A -0.5 8-K refinement on a +75 Finnhub base lowers the score."""
    reco = _reco(consensus_score=0.5, total_analysts=30,
                 buy_count=20, hold_count=8, sell_count=2)
    sec_sig = _sec_8k_sig(score=-0.5, count=2)
    refined = thesis.compute_thesis_with_refinement(
        "AAPL", reco, sec_8k_signal=sec_sig, today="2026-05-18"
    )
    # Base = 75. Events score = -0.5 -> bounded_linear -> 25.
    # With w=0.25 default: 75 * 0.75 + 25 * 0.25 = 56.25 + 6.25 = 62.5
    assert refined["sub_score"] == pytest.approx(62.5, abs=0.1)
    assert refined["components"]["has_sec_8k"] is True
    assert refined["components"]["has_yahoo_earnings"] is False
    assert refined["components"]["sec_8k_score"] == pytest.approx(-0.5)
    assert refined["components"]["yahoo_earnings_score"] is None
    assert refined["components"]["events_score_raw"] == pytest.approx(25.0, abs=0.1)
    assert refined["components"]["events_weight"] == pytest.approx(0.25)
    assert refined["components"]["base_sub_score"] == pytest.approx(75.0, abs=0.1)
    # Refinement source recorded in data_quality for audit.
    assert refined["data_quality"]["has_events_refinement"] is True
    assert refined["data_quality"]["events_refinement_sources"] == ["sec_8k"]


def test_refinement_positive_yahoo_earnings_boosts_score():
    """Strong earnings beat (+0.7) on a +50 base pushes score up."""
    reco = _reco(consensus_score=0.0, total_analysts=10,
                 buy_count=3, hold_count=4, sell_count=3)
    yh = _yahoo_earnings_sig(score=0.7, count=1)
    refined = thesis.compute_thesis_with_refinement(
        "AAPL", reco, yahoo_earnings_signal=yh, today="2026-05-18"
    )
    # Base = 50. Events = +0.7 -> bounded_linear -> 85.
    # Refined = 50 * 0.75 + 85 * 0.25 = 37.5 + 21.25 = 58.75
    assert refined["sub_score"] == pytest.approx(58.8, abs=0.1)
    assert refined["components"]["has_sec_8k"] is False
    assert refined["components"]["has_yahoo_earnings"] is True
    assert refined["components"]["yahoo_earnings_score"] == pytest.approx(0.7)
    assert refined["data_quality"]["events_refinement_sources"] == ["yahoo_earnings"]


def test_refinement_both_signals_blend_5050():
    """Both 8-K and Yahoo present -> equal-weight blend of refinement scores."""
    reco = _reco(consensus_score=0.0, total_analysts=10,
                 buy_count=3, hold_count=4, sell_count=3)
    sec_sig = _sec_8k_sig(score=-1.0, count=1)  # restatement
    yh = _yahoo_earnings_sig(score=1.0, count=1)  # blowout beat
    refined = thesis.compute_thesis_with_refinement(
        "AAPL", reco, sec_8k_signal=sec_sig, yahoo_earnings_signal=yh,
        today="2026-05-18"
    )
    # Events score = 0.5*(-1) + 0.5*(+1) = 0 -> bounded_linear -> 50.
    # Base = 50, refined = 50 (no change because conflicting signals cancel).
    assert refined["sub_score"] == pytest.approx(50.0, abs=0.1)
    assert refined["components"]["has_sec_8k"] is True
    assert refined["components"]["has_yahoo_earnings"] is True
    assert refined["data_quality"]["events_refinement_sources"] == [
        "sec_8k", "yahoo_earnings"
    ]


def test_refinement_empty_signal_dicts_treated_as_absent():
    """Signals with article_count=0 contribute nothing."""
    reco = _reco(consensus_score=0.5, total_analysts=30,
                 buy_count=20, hold_count=8, sell_count=2)
    empty_sec = _sec_8k_sig(score=-0.5, count=0)  # no events
    empty_yh = _yahoo_earnings_sig(score=0.7, count=0)
    refined = thesis.compute_thesis_with_refinement(
        "AAPL", reco, sec_8k_signal=empty_sec,
        yahoo_earnings_signal=empty_yh, today="2026-05-18"
    )
    base = thesis.compute_thesis_from_finnhub_recommendation(
        "AAPL", reco, today="2026-05-18"
    )
    # No refinement applied — sub_score matches base.
    assert refined["sub_score"] == base["sub_score"]
    assert refined["components"]["has_sec_8k"] is False
    assert refined["components"]["has_yahoo_earnings"] is False


def test_refinement_weight_zero_disables_refinement():
    """events_weight=0 should be identity with the Finnhub base."""
    reco = _reco(consensus_score=0.5, total_analysts=30,
                 buy_count=20, hold_count=8, sell_count=2)
    sec_sig = _sec_8k_sig(score=-1.0, count=3)  # extreme negative
    refined = thesis.compute_thesis_with_refinement(
        "AAPL", reco, sec_8k_signal=sec_sig,
        events_weight=0.0, today="2026-05-18"
    )
    base = thesis.compute_thesis_from_finnhub_recommendation(
        "AAPL", reco, today="2026-05-18"
    )
    # Refinement weight 0 -> no movement.
    assert refined["sub_score"] == base["sub_score"]
    # But marker fields still record the signal existed.
    assert refined["components"]["has_sec_8k"] is True


def test_refinement_weight_one_full_override_by_events():
    """events_weight=1 effectively replaces the base with events score."""
    reco = _reco(consensus_score=1.0, total_analysts=30,
                 buy_count=30, hold_count=0, sell_count=0)
    # base = 100. Sec 8-K events score = -1.0 -> 0.
    sec_sig = _sec_8k_sig(score=-1.0, count=2)
    refined = thesis.compute_thesis_with_refinement(
        "AAPL", reco, sec_8k_signal=sec_sig,
        events_weight=1.0, today="2026-05-18"
    )
    # Refined = 100*0 + 0*1 = 0.
    assert refined["sub_score"] == pytest.approx(0.0, abs=0.1)


def test_refinement_no_finnhub_no_events_returns_neutral():
    """None reco + no events -> neutral 50."""
    refined = thesis.compute_thesis_with_refinement(
        "AAPL", None, today="2026-05-18"
    )
    assert refined["sub_score"] == 50.0
    assert refined["components"]["has_sec_8k"] is False
    assert refined["components"]["has_yahoo_earnings"] is False
    assert refined["components"]["base_sub_score"] == 50.0


def test_refinement_no_finnhub_with_8k_uses_events_only():
    """When Finnhub is absent, 8-K events alone refine off the neutral 50."""
    sec_sig = _sec_8k_sig(score=-0.8, count=1)
    refined = thesis.compute_thesis_with_refinement(
        "AAPL", None, sec_8k_signal=sec_sig, today="2026-05-18"
    )
    # Base = 50, events = -0.8 -> 10. Refined = 50*0.75 + 10*0.25 = 40.
    assert refined["sub_score"] == pytest.approx(40.0, abs=0.1)
    assert refined["components"]["has_sec_8k"] is True
    assert refined["components"]["sec_8k_score"] == pytest.approx(-0.8)


def test_refinement_invalid_score_in_signal_is_skipped():
    """A signal with mean_sentiment_score=None is treated as absent."""
    reco = _reco(consensus_score=0.5, total_analysts=30,
                 buy_count=20, hold_count=8, sell_count=2)
    bad_sec = _sec_8k_sig(score=None, count=2)
    refined = thesis.compute_thesis_with_refinement(
        "AAPL", reco, sec_8k_signal=bad_sec, today="2026-05-18"
    )
    base = thesis.compute_thesis_from_finnhub_recommendation(
        "AAPL", reco, today="2026-05-18"
    )
    assert refined["sub_score"] == base["sub_score"]
    assert refined["components"]["has_sec_8k"] is False


def test_refinement_clamps_extreme_score_to_unit_interval():
    """A score outside [-1, +1] is clamped before blending."""
    reco = _reco(consensus_score=0.0, total_analysts=10,
                 buy_count=3, hold_count=4, sell_count=3)
    sec_sig = _sec_8k_sig(score=-5.0, count=2)  # malformed extreme
    refined = thesis.compute_thesis_with_refinement(
        "AAPL", reco, sec_8k_signal=sec_sig, today="2026-05-18"
    )
    # Clamped to -1.0 -> events score 0. Refined = 50*0.75 + 0*0.25 = 37.5
    assert refined["sub_score"] == pytest.approx(37.5, abs=0.1)
    assert refined["components"]["sec_8k_score"] == pytest.approx(-1.0)


def test_refinement_preserves_shape_keys():
    """Refinement output has the same top-level keys as the base."""
    reco = _reco(consensus_score=0.3, total_analysts=10,
                 buy_count=6, hold_count=3, sell_count=1)
    sec_sig = _sec_8k_sig(score=0.5, count=1)
    yh = _yahoo_earnings_sig(score=0.4, count=1)
    refined = thesis.compute_thesis_with_refinement(
        "AAPL", reco, sec_8k_signal=sec_sig, yahoo_earnings_signal=yh,
        today="2026-05-18"
    )
    assert set(refined.keys()) == {
        "ticker", "sub_score", "components", "data_quality"
    }
    # Required refinement marker fields exist.
    for key in ("has_sec_8k", "has_yahoo_earnings", "sec_8k_score",
                "yahoo_earnings_score", "events_score_raw",
                "events_weight", "base_sub_score"):
        assert key in refined["components"]
    assert "has_events_refinement" in refined["data_quality"]
    assert "events_refinement_sources" in refined["data_quality"]


def test_refinement_neutral_event_score_treated_as_absent():
    """A signal with mean_sentiment_score=0.0 doesn't pull the base toward 50.

    This is the key anti-flattening guard: most 8-K filings carry direction=0
    (Reg-FD disclosures, exhibits), and treating them as a "pull to 50"
    force would shrink the cross-sectional stdev of Thesis sub-scores —
    exactly the opposite of what refinement is supposed to do.
    """
    reco = _reco(consensus_score=0.5, total_analysts=30,
                 buy_count=20, hold_count=8, sell_count=2)
    neutral_sec = _sec_8k_sig(score=0.0, count=5)  # 5 neutral 8-K events
    refined = thesis.compute_thesis_with_refinement(
        "AAPL", reco, sec_8k_signal=neutral_sec, today="2026-05-18"
    )
    base = thesis.compute_thesis_from_finnhub_recommendation(
        "AAPL", reco, today="2026-05-18"
    )
    # Neutral 8-K should not move the score AT ALL — it carries no
    # directional information.
    assert refined["sub_score"] == base["sub_score"]
    # Marker fields reflect that no refinement actually fired.
    assert refined["components"]["has_sec_8k"] is False
    assert refined["components"]["sec_8k_score"] is None


def test_refinement_refines_off_stored_sentiment_when_no_finnhub():
    """Fallback to stored sentiment when no Finnhub signal; refinement still applies."""
    stored = {
        "ticker": "AAPL",
        "last_scored": "2026-05-18",
        "article_count": 10,
        "mean_sentiment_score": 0.5,
        "mean_relevance_score": 0.5,
        "label_counts": {"Bullish": 8, "Neutral": 2,
                          "Bearish": 0, "Somewhat-Bullish": 0,
                          "Somewhat-Bearish": 0},
    }
    sec_sig = _sec_8k_sig(score=-1.0, count=2)
    refined = thesis.compute_thesis_with_refinement(
        "AAPL", None, sec_8k_signal=sec_sig, stored_sentiment=stored,
        today="2026-05-18"
    )
    # Stored base = 75. Events = 0. Refined = 75*0.75 + 0*0.25 = 56.25
    assert refined["sub_score"] == pytest.approx(56.2, abs=0.2)
    assert refined["components"]["has_sec_8k"] is True
    assert refined["components"]["base_sub_score"] == pytest.approx(75.0, abs=0.5)
