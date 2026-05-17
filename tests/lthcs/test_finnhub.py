"""Tests for lthcs.sources.finnhub.

All HTTP is mocked; no live network calls. Each test starts with a fresh
on-disk cache (pointed at ``tmp_path`` via ``monkeypatch``) and a
generously-sized token bucket, so the rate-limiter never gets in the way
unless the specific test wants it to.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from lthcs.sources import finnhub as fh
from lthcs.sources._cache import FileCache
from lthcs.sources._ratelimit import TokenBucket


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Dict[str, FileCache]:
    """Redirect every module-level FileCache to a fresh tmp dir."""
    news = FileCache("finnhub_news", root=tmp_path)
    sent = FileCache("finnhub_sentiment", root=tmp_path)
    reco = FileCache("finnhub_recommendations", root=tmp_path)
    monkeypatch.setattr(fh, "_NEWS_CACHE", news)
    monkeypatch.setattr(fh, "_SENTIMENT_CACHE", sent)
    monkeypatch.setattr(fh, "_RECO_CACHE", reco)
    return {"news": news, "sentiment": sent, "reco": reco}


@pytest.fixture(autouse=True)
def generous_bucket(monkeypatch: pytest.MonkeyPatch) -> TokenBucket:
    """Replace the rate-limit bucket with a very generous one by default."""
    bucket = TokenBucket(capacity=1_000_000, refill_rate=1_000_000)
    monkeypatch.setattr(fh, "_BUCKET", bucket)
    return bucket


@pytest.fixture(autouse=True)
def api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default to a valid API key. Tests that remove it delete it explicitly."""
    monkeypatch.setenv("FINNHUB_API_KEY", "test-key")


def _mock_response(
    payload: Any, status_code: int = 200, text: Optional[str] = None
) -> MagicMock:
    """Build a MagicMock that quacks like a ``requests.Response``."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    if isinstance(payload, Exception):
        resp.json.side_effect = payload
    else:
        resp.json.return_value = payload
    resp.text = text if text is not None else str(payload)
    return resp


def _company_news_payload() -> List[Dict[str, Any]]:
    return [
        {
            "category": "company news",
            "datetime": 1747432800,
            "headline": "Apple unveils new iPhone",
            "id": 101,
            "image": "https://img.example.com/a.png",
            "related": "AAPL",
            "source": "Reuters",
            "summary": "Apple announced the iPhone 18 today.",
            "url": "https://example.com/a",
        },
        {
            "category": "company news",
            "datetime": 1747346400,
            "headline": "Apple beats Q2 earnings",
            "id": 102,
            "image": "",
            "related": "AAPL",
            "source": "Bloomberg",
            "summary": "Apple reported $97B in revenue.",
            "url": "https://example.com/b",
        },
    ]


def _sentiment_payload(
    *,
    articles: int = 30,
    bullish: float = 0.5,
    bearish: float = 0.2,
    buzz: float = 1.2,
    company_score: float = 0.65,
    sector_avg_score: float = 0.5,
    sector_avg_bullish: float = 0.55,
    weekly_average: float = 25.0,
) -> Dict[str, Any]:
    return {
        "buzz": {
            "articlesInLastWeek": articles,
            "buzz": buzz,
            "weeklyAverage": weekly_average,
        },
        "companyNewsScore": company_score,
        "sectorAverageBullishPercent": sector_avg_bullish,
        "sectorAverageNewsScore": sector_avg_score,
        "sentiment": {
            "bearishPercent": bearish,
            "bullishPercent": bullish,
        },
        "symbol": "AAPL",
    }


def _recommendation_payload() -> List[Dict[str, Any]]:
    # Newest first, AAPL-like profile sliding more bullish over 3 months.
    return [
        {
            "buy": 18,
            "hold": 4,
            "period": "2026-05-01",
            "sell": 0,
            "strongBuy": 12,
            "strongSell": 0,
            "symbol": "AAPL",
        },
        {
            "buy": 17,
            "hold": 5,
            "period": "2026-04-01",
            "sell": 1,
            "strongBuy": 10,
            "strongSell": 0,
            "symbol": "AAPL",
        },
        {
            "buy": 15,
            "hold": 6,
            "period": "2026-03-01",
            "sell": 2,
            "strongBuy": 8,
            "strongSell": 0,
            "symbol": "AAPL",
        },
    ]


# ---------------------------------------------------------------------------
# get_company_news
# ---------------------------------------------------------------------------


def test_get_company_news_parses_headline_list() -> None:
    payload = _company_news_payload()
    mock_get = MagicMock(return_value=_mock_response(payload))

    with patch("lthcs.sources.finnhub.requests.get", mock_get):
        result = fh.get_company_news("AAPL", days=30)

    assert mock_get.call_count == 1
    call_args = mock_get.call_args
    # URL is the company-news endpoint.
    assert call_args.args[0] == f"{fh._FINNHUB_BASE}/company-news"
    params = call_args.kwargs["params"]
    assert params["symbol"] == "AAPL"
    assert params["token"] == "test-key"
    assert "from" in params and "to" in params

    assert isinstance(result, list)
    assert len(result) == 2
    first = result[0]
    assert first["headline"] == "Apple unveils new iPhone"
    assert first["summary"] == "Apple announced the iPhone 18 today."
    assert first["source"] == "Reuters"
    assert first["url"] == "https://example.com/a"
    assert first["category"] == "company news"
    assert first["datetime"] == 1747432800


def test_get_company_news_none_or_empty_returns_empty() -> None:
    mock_get = MagicMock(return_value=_mock_response([]))

    with patch("lthcs.sources.finnhub.requests.get", mock_get):
        assert fh.get_company_news(None) == []
        assert fh.get_company_news("") == []

    # No HTTP call should have happened.
    assert mock_get.call_count == 0


def test_get_company_news_non_200_returns_empty() -> None:
    resp = _mock_response({}, status_code=500, text="Server error")
    mock_get = MagicMock(return_value=resp)

    with patch("lthcs.sources.finnhub.requests.get", mock_get):
        result = fh.get_company_news("AAPL")

    assert result == []


def test_get_company_news_network_error_returns_empty() -> None:
    mock_get = MagicMock(side_effect=ConnectionError("boom"))

    with patch("lthcs.sources.finnhub.requests.get", mock_get):
        result = fh.get_company_news("AAPL")

    assert result == []


def test_get_company_news_invalid_json_returns_empty() -> None:
    resp = _mock_response(ValueError("not json"))
    mock_get = MagicMock(return_value=resp)

    with patch("lthcs.sources.finnhub.requests.get", mock_get):
        result = fh.get_company_news("AAPL")

    assert result == []


def test_get_company_news_cache_hit_avoids_second_call() -> None:
    mock_get = MagicMock(return_value=_mock_response(_company_news_payload()))

    with patch("lthcs.sources.finnhub.requests.get", mock_get):
        a = fh.get_company_news("AAPL", days=30)
        b = fh.get_company_news("AAPL", days=30)

    assert a == b
    assert mock_get.call_count == 1


def test_get_company_news_cache_miss_after_clear_refetches(
    isolated_caches: Dict[str, FileCache],
) -> None:
    mock_get = MagicMock(return_value=_mock_response(_company_news_payload()))

    with patch("lthcs.sources.finnhub.requests.get", mock_get):
        fh.get_company_news("AAPL", days=30)
        assert mock_get.call_count == 1
        isolated_caches["news"].clear()
        fh.get_company_news("AAPL", days=30)
        assert mock_get.call_count == 2


# ---------------------------------------------------------------------------
# get_news_sentiment
# ---------------------------------------------------------------------------


def test_get_news_sentiment_returns_normalized_dict() -> None:
    payload = _sentiment_payload(
        articles=30,
        bullish=0.6,
        bearish=0.1,
        buzz=1.4,
        company_score=0.75,
        sector_avg_score=0.55,
        sector_avg_bullish=0.5,
        weekly_average=22.0,
    )
    mock_get = MagicMock(return_value=_mock_response(payload))

    with patch("lthcs.sources.finnhub.requests.get", mock_get):
        result = fh.get_news_sentiment("AAPL")

    assert mock_get.call_args.args[0] == f"{fh._FINNHUB_BASE}/news-sentiment"
    assert mock_get.call_args.kwargs["params"]["symbol"] == "AAPL"

    # All expected keys present.
    expected_keys = {
        "ticker",
        "article_count",
        "weekly_average",
        "buzz_score",
        "company_news_score",
        "sector_avg_news_score",
        "bullish_percent",
        "bearish_percent",
        "sector_avg_bullish",
    }
    assert set(result.keys()) == expected_keys

    assert result["ticker"] == "AAPL"
    assert result["article_count"] == 30
    assert result["bullish_percent"] == pytest.approx(0.6)
    assert result["bearish_percent"] == pytest.approx(0.1)
    assert result["buzz_score"] == pytest.approx(1.4)
    assert result["company_news_score"] == pytest.approx(0.75)
    assert result["sector_avg_news_score"] == pytest.approx(0.55)
    assert result["sector_avg_bullish"] == pytest.approx(0.5)
    assert result["weekly_average"] == pytest.approx(22.0)


def test_get_news_sentiment_none_returns_empty() -> None:
    mock_get = MagicMock(return_value=_mock_response({}))

    with patch("lthcs.sources.finnhub.requests.get", mock_get):
        assert fh.get_news_sentiment(None) == {}
        assert fh.get_news_sentiment("") == {}

    assert mock_get.call_count == 0


def test_get_news_sentiment_non_200_returns_empty() -> None:
    resp = _mock_response({}, status_code=502, text="bad gateway")
    mock_get = MagicMock(return_value=resp)

    with patch("lthcs.sources.finnhub.requests.get", mock_get):
        result = fh.get_news_sentiment("AAPL")

    assert result == {}


def test_get_news_sentiment_cache_hit_avoids_second_call() -> None:
    mock_get = MagicMock(return_value=_mock_response(_sentiment_payload()))

    with patch("lthcs.sources.finnhub.requests.get", mock_get):
        a = fh.get_news_sentiment("AAPL")
        b = fh.get_news_sentiment("AAPL")

    assert a == b
    assert mock_get.call_count == 1


# ---------------------------------------------------------------------------
# parse_thesis_signal — Finnhub sentiment -> Thesis pillar payload
# ---------------------------------------------------------------------------


def test_parse_thesis_signal_basic_derivation() -> None:
    sentiment = {
        "ticker": "AAPL",
        "article_count": 30,
        "weekly_average": 25.0,
        "buzz_score": 0.8,
        "company_news_score": 0.7,
        "sector_avg_news_score": 0.55,
        "bullish_percent": 0.6,
        "bearish_percent": 0.1,
        "sector_avg_bullish": 0.5,
    }
    out = fh.parse_thesis_signal(sentiment)

    assert out["ticker"] == "AAPL"
    assert out["article_count"] == 30
    # 0.6 - 0.1 = 0.5
    assert out["mean_sentiment_score"] == pytest.approx(0.5)
    # buzz_score clamped into [0, 1]
    assert out["mean_relevance_score"] == pytest.approx(0.8)
    assert out["source"] == "finnhub"
    assert "last_scored" in out and isinstance(out["last_scored"], str)
    # All five canonical label buckets are present.
    assert set(out["label_counts"].keys()) == {
        "Bearish",
        "Somewhat-Bearish",
        "Neutral",
        "Somewhat-Bullish",
        "Bullish",
    }
    # Label histogram sums to article_count.
    assert sum(out["label_counts"].values()) == 30
    # Bullish bucket should dominate (60% of 30 = 18).
    assert out["label_counts"]["Bullish"] == 18
    assert out["label_counts"]["Bearish"] == 3
    # Remainder lands in Neutral (30 - 18 - 3 = 9).
    assert out["label_counts"]["Neutral"] == 9


def test_parse_thesis_signal_zero_articles_means_none_scores() -> None:
    sentiment = {
        "ticker": "AAPL",
        "article_count": 0,
        "buzz_score": 0.0,
        "bullish_percent": 0.0,
        "bearish_percent": 0.0,
    }
    out = fh.parse_thesis_signal(sentiment)

    assert out["article_count"] == 0
    assert out["mean_sentiment_score"] is None
    assert out["mean_relevance_score"] is None
    assert sum(out["label_counts"].values()) == 0


def test_parse_thesis_signal_three_articles_50_20_split() -> None:
    """Exact case from the brief: 3 articles, 50% bullish, 20% bearish."""
    sentiment = {
        "ticker": "AAPL",
        "article_count": 3,
        "buzz_score": 1.0,
        "bullish_percent": 0.5,
        "bearish_percent": 0.2,
    }
    out = fh.parse_thesis_signal(sentiment)

    assert out["article_count"] == 3
    # 0.5 - 0.2 = 0.3
    assert out["mean_sentiment_score"] == pytest.approx(0.3)
    assert out["mean_relevance_score"] == pytest.approx(1.0)
    # 3 articles: 50% bullish -> 1.5 -> largest-remainder bumps to 2 or 1;
    # 20% bearish -> 0.6 -> 1; 30% neutral -> 0.9 -> 1. Sum must == 3.
    counts = out["label_counts"]
    assert sum(counts.values()) == 3
    assert counts["Bullish"] >= 1
    # Sanity: the largest bucket is Bullish (or tied with Neutral).
    assert counts["Bullish"] >= counts["Bearish"]


def test_parse_thesis_signal_clamps_negative_sentiment() -> None:
    sentiment = {
        "ticker": "MSFT",
        "article_count": 10,
        "buzz_score": 0.3,
        "bullish_percent": 0.1,
        "bearish_percent": 0.8,
    }
    out = fh.parse_thesis_signal(sentiment)

    assert out["mean_sentiment_score"] == pytest.approx(-0.7)
    assert sum(out["label_counts"].values()) == 10
    assert out["label_counts"]["Bearish"] == 8


def test_parse_thesis_signal_buzz_above_one_clamps_to_one() -> None:
    sentiment = {
        "ticker": "NVDA",
        "article_count": 5,
        "buzz_score": 3.5,  # well above 1.0; sector relative score
        "bullish_percent": 0.4,
        "bearish_percent": 0.1,
    }
    out = fh.parse_thesis_signal(sentiment)

    assert out["mean_relevance_score"] == pytest.approx(1.0)


def test_parse_thesis_signal_handles_empty_input() -> None:
    out = fh.parse_thesis_signal({})
    assert out["ticker"] == ""
    assert out["article_count"] == 0
    assert out["mean_sentiment_score"] is None
    assert out["mean_relevance_score"] is None
    assert out["source"] == "finnhub"


# ---------------------------------------------------------------------------
# get_recommendation_trends
# ---------------------------------------------------------------------------


def test_get_recommendation_trends_parses_list() -> None:
    payload = _recommendation_payload()
    mock_get = MagicMock(return_value=_mock_response(payload))

    with patch("lthcs.sources.finnhub.requests.get", mock_get):
        rows = fh.get_recommendation_trends("AAPL")

    assert mock_get.call_args.args[0] == (
        f"{fh._FINNHUB_BASE}/stock/recommendation"
    )
    assert mock_get.call_args.kwargs["params"]["symbol"] == "AAPL"

    assert len(rows) == 3
    # Newest first.
    assert rows[0]["period"] == "2026-05-01"
    assert rows[0]["strong_buy"] == 12
    assert rows[0]["buy"] == 18
    assert rows[0]["hold"] == 4
    assert rows[0]["sell"] == 0
    assert rows[0]["strong_sell"] == 0


def test_get_recommendation_trends_none_returns_empty() -> None:
    mock_get = MagicMock(return_value=_mock_response([]))

    with patch("lthcs.sources.finnhub.requests.get", mock_get):
        assert fh.get_recommendation_trends(None) == []

    assert mock_get.call_count == 0


def test_get_recommendation_trends_non_200_returns_empty() -> None:
    resp = _mock_response({}, status_code=404, text="not found")
    mock_get = MagicMock(return_value=resp)

    with patch("lthcs.sources.finnhub.requests.get", mock_get):
        assert fh.get_recommendation_trends("XYZ") == []


# ---------------------------------------------------------------------------
# parse_recommendation_signal
# ---------------------------------------------------------------------------


def test_parse_recommendation_signal_positive_consensus() -> None:
    """5 strong_buy / 3 buy / 2 hold / 0 sell / 0 strong_sell."""
    trends = [
        {
            "period": "2026-05-01",
            "strong_buy": 5,
            "buy": 3,
            "hold": 2,
            "sell": 0,
            "strong_sell": 0,
        }
    ]
    out = fh.parse_recommendation_signal(trends)

    assert out["latest_month"] == "2026-05-01"
    assert out["buy_count"] == 8
    assert out["hold_count"] == 2
    assert out["sell_count"] == 0
    assert out["total_analysts"] == 10
    # Weighted: (5*1 + 3*0.5 + 2*0 + 0*-0.5 + 0*-1) / 10 = 6.5 / 10 = 0.65
    assert out["consensus_score"] == pytest.approx(0.65)
    # Only one month -> no MoM delta.
    assert out["change_from_prior_month"] is None


def test_parse_recommendation_signal_mom_delta() -> None:
    trends = [
        {
            "period": "2026-05-01",
            "strong_buy": 5,
            "buy": 3,
            "hold": 2,
            "sell": 0,
            "strong_sell": 0,
        },  # consensus = 0.65
        {
            "period": "2026-04-01",
            "strong_buy": 2,
            "buy": 3,
            "hold": 4,
            "sell": 1,
            "strong_sell": 0,
        },  # weighted: (2 + 1.5 + 0 + -0.5 + 0)/10 = 0.30
    ]
    out = fh.parse_recommendation_signal(trends)

    assert out["consensus_score"] == pytest.approx(0.65)
    assert out["change_from_prior_month"] == pytest.approx(0.65 - 0.30)


def test_parse_recommendation_signal_empty_input() -> None:
    out = fh.parse_recommendation_signal([])
    assert out["latest_month"] is None
    assert out["total_analysts"] == 0
    assert out["consensus_score"] is None
    assert out["change_from_prior_month"] is None


def test_parse_recommendation_signal_aapl_like_shape() -> None:
    """Brief's worked example: 12 strong_buy, 18 buy, 4 hold, 0 sell, 0 strong_sell."""
    trends = [
        {
            "period": "2026-05-01",
            "strong_buy": 12,
            "buy": 18,
            "hold": 4,
            "sell": 0,
            "strong_sell": 0,
        }
    ]
    out = fh.parse_recommendation_signal(trends)

    assert out["buy_count"] == 30
    assert out["hold_count"] == 4
    assert out["sell_count"] == 0
    assert out["total_analysts"] == 34
    # (12 + 9 + 0 + 0 + 0)/34 = 21/34 ≈ 0.6176
    assert out["consensus_score"] == pytest.approx(21.0 / 34.0)


def test_parse_recommendation_signal_all_strong_sell_caps_at_negative_one() -> None:
    trends = [
        {
            "period": "2026-05-01",
            "strong_buy": 0,
            "buy": 0,
            "hold": 0,
            "sell": 0,
            "strong_sell": 8,
        }
    ]
    out = fh.parse_recommendation_signal(trends)
    assert out["consensus_score"] == pytest.approx(-1.0)


# ---------------------------------------------------------------------------
# Error handling: API key + rate limit
# ---------------------------------------------------------------------------


def test_missing_api_key_raises_at_first_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    mock_get = MagicMock(return_value=_mock_response([]))

    with patch("lthcs.sources.finnhub.requests.get", mock_get):
        with pytest.raises(fh.FinnhubAPIKeyMissing):
            fh.get_company_news("AAPL")
        with pytest.raises(fh.FinnhubAPIKeyMissing):
            fh.get_news_sentiment("AAPL")
        with pytest.raises(fh.FinnhubAPIKeyMissing):
            fh.get_recommendation_trends("AAPL")

    # No HTTP traffic should have been issued.
    assert mock_get.call_count == 0


def test_http_429_raises_rate_limit_error() -> None:
    resp = _mock_response({}, status_code=429, text="too many requests")
    mock_get = MagicMock(return_value=resp)

    with patch("lthcs.sources.finnhub.requests.get", mock_get):
        with pytest.raises(fh.FinnhubRateLimit):
            fh.get_company_news("AAPL")


def test_local_bucket_exhaustion_raises_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Tight bucket with no refill so we can deterministically blow through it.
    bucket = TokenBucket(capacity=1, refill_rate=0)
    monkeypatch.setattr(fh, "_BUCKET", bucket)

    payload = _company_news_payload()
    mock_get = MagicMock(return_value=_mock_response(payload))

    with patch("lthcs.sources.finnhub.requests.get", mock_get):
        # First request consumes the single token (with a unique cache key).
        fh.get_company_news("AAPL", days=30)
        # Second unique request can't get a token in 2s and must raise.
        with pytest.raises(fh.FinnhubRateLimit):
            fh.get_company_news("MSFT", days=30)


# ---------------------------------------------------------------------------
# Cache isolation between endpoints
# ---------------------------------------------------------------------------


def test_news_and_sentiment_caches_are_isolated(
    isolated_caches: Dict[str, FileCache],
) -> None:
    """A news fetch should not satisfy a sentiment fetch (or vice-versa).

    Same ticker, two endpoints -> two separate HTTP calls, two separate
    caches.
    """
    news_payload = _company_news_payload()
    sent_payload = _sentiment_payload()

    def _router(url: str, *args: Any, **kwargs: Any) -> MagicMock:
        if "company-news" in url:
            return _mock_response(news_payload)
        if "news-sentiment" in url:
            return _mock_response(sent_payload)
        if "recommendation" in url:
            return _mock_response(_recommendation_payload())
        return _mock_response({}, status_code=404)

    mock_get = MagicMock(side_effect=_router)

    with patch("lthcs.sources.finnhub.requests.get", mock_get):
        fh.get_company_news("AAPL", days=30)
        fh.get_news_sentiment("AAPL")
        # Each endpoint hit exactly once.
        assert mock_get.call_count == 2

        # Clear only the news cache. Sentiment should still be cached.
        isolated_caches["news"].clear()
        fh.get_news_sentiment("AAPL")  # cached, no new HTTP call
        assert mock_get.call_count == 2

        # News refetches.
        fh.get_company_news("AAPL", days=30)
        assert mock_get.call_count == 3
