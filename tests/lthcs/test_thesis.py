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
