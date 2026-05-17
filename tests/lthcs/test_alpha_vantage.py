"""Tests for lthcs.sources.alpha_vantage.

All tests mock ``requests.get`` so no live HTTP is performed. The module
level cache singleton is redirected to ``tmp_path`` via ``monkeypatch``
so each test starts with an empty cache. The token bucket is replaced
with one configured for the specific test's needs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from lthcs.sources import alpha_vantage as av
from lthcs.sources._cache import FileCache
from lthcs.sources._ratelimit import TokenBucket


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FileCache:
    """Point the module-level cache at a fresh tmp dir for every test."""
    fresh = FileCache("alpha_vantage", root=tmp_path)
    monkeypatch.setattr(av, "_cache", fresh)
    return fresh


@pytest.fixture(autouse=True)
def generous_bucket(monkeypatch: pytest.MonkeyPatch) -> TokenBucket:
    """Replace the daily bucket with a generously-sized one by default.

    Tests that specifically exercise the rate limit override this with
    their own bucket.
    """
    bucket = TokenBucket(capacity=1_000_000, refill_rate=1_000_000)
    monkeypatch.setattr(av, "_bucket", bucket)
    return bucket


@pytest.fixture(autouse=True)
def api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default to a valid API key. Tests that remove it delete it explicitly."""
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "test-key")


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


def _news_payload(
    feed: List[Dict[str, Any]], items: Optional[str] = None
) -> Dict[str, Any]:
    return {
        "items": items if items is not None else str(len(feed)),
        "feed": feed,
    }


def _article(
    title: str,
    ticker_entries: List[Dict[str, str]],
    overall_label: str = "Neutral",
    overall_score: float = 0.0,
) -> Dict[str, Any]:
    return {
        "title": title,
        "url": f"https://example.com/{title.replace(' ', '-')}",
        "time_published": "20260516T120000",
        "overall_sentiment_score": overall_score,
        "overall_sentiment_label": overall_label,
        "ticker_sentiment": ticker_entries,
    }


# ---------------------------------------------------------------------------
# get_news_sentiment — HTTP / params
# ---------------------------------------------------------------------------


def test_get_news_sentiment_hits_correct_url_and_params() -> None:
    payload = _news_payload([])
    mock_get = MagicMock(return_value=_mock_response(payload))

    with patch("lthcs.sources.alpha_vantage.requests.get", mock_get):
        result = av.get_news_sentiment(["AAPL", "MSFT"], limit=10)

    assert result == payload
    assert mock_get.call_count == 1
    call_args = mock_get.call_args
    assert call_args.args[0] == "https://www.alphavantage.co/query"
    params = call_args.kwargs["params"]
    assert params["function"] == "NEWS_SENTIMENT"
    assert params["tickers"] == "AAPL,MSFT"
    assert params["limit"] == "10"
    assert params["apikey"] == "test-key"
    # ``timeout`` should be set so we never hang forever.
    assert "timeout" in call_args.kwargs


def test_get_news_sentiment_passes_topics_when_supplied() -> None:
    payload = _news_payload([])
    mock_get = MagicMock(return_value=_mock_response(payload))

    with patch("lthcs.sources.alpha_vantage.requests.get", mock_get):
        av.get_news_sentiment(
            ["AAPL"], topics=["technology", "earnings"], limit=5
        )

    params = mock_get.call_args.kwargs["params"]
    assert params["topics"] == "technology,earnings"


def test_get_news_sentiment_omits_topics_when_none() -> None:
    payload = _news_payload([])
    mock_get = MagicMock(return_value=_mock_response(payload))

    with patch("lthcs.sources.alpha_vantage.requests.get", mock_get):
        av.get_news_sentiment(["AAPL"])

    assert "topics" not in mock_get.call_args.kwargs["params"]


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------


def test_cache_key_is_ticker_order_insensitive() -> None:
    payload = _news_payload([_article("a", [])])
    mock_get = MagicMock(return_value=_mock_response(payload))

    with patch("lthcs.sources.alpha_vantage.requests.get", mock_get):
        first = av.get_news_sentiment(["AAPL", "MSFT"])
        second = av.get_news_sentiment(["MSFT", "AAPL"])

    assert first == second
    # The reordered call must hit the cache rather than re-fetch.
    assert mock_get.call_count == 1


def test_cache_hit_avoids_second_http_call() -> None:
    payload = _news_payload([_article("a", [])])
    mock_get = MagicMock(return_value=_mock_response(payload))

    with patch("lthcs.sources.alpha_vantage.requests.get", mock_get):
        a = av.get_news_sentiment(["AAPL", "MSFT"])
        b = av.get_news_sentiment(["AAPL", "MSFT"])

    assert a == b
    assert mock_get.call_count == 1


def test_cache_miss_after_delete_refetches(isolated_cache: FileCache) -> None:
    payload = _news_payload([_article("a", [])])
    mock_get = MagicMock(return_value=_mock_response(payload))

    with patch("lthcs.sources.alpha_vantage.requests.get", mock_get):
        av.get_news_sentiment(["AAPL"])
        assert mock_get.call_count == 1
        # Wipe the cache and refetch.
        isolated_cache.clear()
        av.get_news_sentiment(["AAPL"])
        assert mock_get.call_count == 2


def test_different_limit_cached_separately() -> None:
    payload = _news_payload([])
    mock_get = MagicMock(return_value=_mock_response(payload))

    with patch("lthcs.sources.alpha_vantage.requests.get", mock_get):
        av.get_news_sentiment(["AAPL"], limit=10)
        av.get_news_sentiment(["AAPL"], limit=50)

    assert mock_get.call_count == 2


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------


def test_token_bucket_exhaustion_raises_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Real-shape bucket: 25 capacity, slow refill so we never refill mid-test.
    bucket = TokenBucket(capacity=25, refill_rate=25 / 86400.0)
    monkeypatch.setattr(av, "_bucket", bucket)

    payload = _news_payload([])
    # 25 unique requests so each one misses the cache.
    mock_get = MagicMock(return_value=_mock_response(payload))

    with patch("lthcs.sources.alpha_vantage.requests.get", mock_get):
        for i in range(25):
            av.get_news_sentiment([f"TKR{i}"])
        # The 26th must raise.
        with pytest.raises(av.RateLimitExhausted):
            av.get_news_sentiment(["TKR_OVERFLOW"])

    assert mock_get.call_count == 25


# ---------------------------------------------------------------------------
# parse_ticker_sentiment — pure function
# ---------------------------------------------------------------------------


def test_parse_ticker_sentiment_mixed_tickers() -> None:
    feed = [
        _article(
            "Apple beats earnings",
            [
                {
                    "ticker": "AAPL",
                    "relevance_score": "0.80",
                    "ticker_sentiment_score": "0.30",
                    "ticker_sentiment_label": "Bullish",
                },
                {
                    "ticker": "MSFT",
                    "relevance_score": "0.20",
                    "ticker_sentiment_score": "0.05",
                    "ticker_sentiment_label": "Neutral",
                },
            ],
        ),
        _article(
            "Apple supplier struggles",
            [
                {
                    "ticker": "AAPL",
                    "relevance_score": "0.40",
                    "ticker_sentiment_score": "-0.10",
                    "ticker_sentiment_label": "Somewhat-Bearish",
                },
            ],
        ),
        _article(
            "Microsoft cloud growth",
            [
                {
                    "ticker": "MSFT",
                    "relevance_score": "0.90",
                    "ticker_sentiment_score": "0.45",
                    "ticker_sentiment_label": "Bullish",
                },
            ],
        ),
    ]
    payload = _news_payload(feed)

    aapl = av.parse_ticker_sentiment(payload, "AAPL")
    assert aapl["ticker"] == "AAPL"
    assert aapl["article_count"] == 2
    assert aapl["mean_sentiment_score"] == pytest.approx((0.30 + -0.10) / 2)
    assert aapl["mean_relevance_score"] == pytest.approx((0.80 + 0.40) / 2)
    assert aapl["label_counts"]["Bullish"] == 1
    assert aapl["label_counts"]["Somewhat-Bearish"] == 1
    assert aapl["label_counts"]["Neutral"] == 0
    # All five canonical labels should be present in the histogram.
    assert set(aapl["label_counts"].keys()) == {
        "Bearish",
        "Somewhat-Bearish",
        "Neutral",
        "Somewhat-Bullish",
        "Bullish",
    }

    msft = av.parse_ticker_sentiment(payload, "MSFT")
    assert msft["article_count"] == 2
    assert msft["mean_sentiment_score"] == pytest.approx((0.05 + 0.45) / 2)
    assert msft["label_counts"]["Bullish"] == 1
    assert msft["label_counts"]["Neutral"] == 1


def test_parse_ticker_sentiment_unmentioned_ticker() -> None:
    feed = [
        _article(
            "Apple beats earnings",
            [
                {
                    "ticker": "AAPL",
                    "relevance_score": "0.80",
                    "ticker_sentiment_score": "0.30",
                    "ticker_sentiment_label": "Bullish",
                }
            ],
        ),
    ]
    payload = _news_payload(feed)

    result = av.parse_ticker_sentiment(payload, "NVDA")
    assert result["ticker"] == "NVDA"
    assert result["article_count"] == 0
    assert result["mean_sentiment_score"] is None
    assert result["mean_relevance_score"] is None
    assert result["label_counts"] == {
        "Bearish": 0,
        "Somewhat-Bearish": 0,
        "Neutral": 0,
        "Somewhat-Bullish": 0,
        "Bullish": 0,
    }


def test_parse_ticker_sentiment_empty_response() -> None:
    result = av.parse_ticker_sentiment({"items": "0", "feed": []}, "AAPL")
    assert result["article_count"] == 0
    assert result["mean_sentiment_score"] is None
    assert result["mean_relevance_score"] is None
    assert sum(result["label_counts"].values()) == 0


def test_parse_ticker_sentiment_is_case_insensitive() -> None:
    feed = [
        _article(
            "Apple",
            [
                {
                    "ticker": "AAPL",
                    "relevance_score": "1.0",
                    "ticker_sentiment_score": "0.5",
                    "ticker_sentiment_label": "Bullish",
                }
            ],
        ),
    ]
    payload = _news_payload(feed)
    result = av.parse_ticker_sentiment(payload, "aapl")
    assert result["ticker"] == "AAPL"
    assert result["article_count"] == 1


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_av_note_response_raises_error() -> None:
    throttle = {
        "Note": (
            "Thank you for using Alpha Vantage! Our standard API rate limit "
            "is 25 requests per day."
        )
    }
    mock_get = MagicMock(return_value=_mock_response(throttle))

    with patch("lthcs.sources.alpha_vantage.requests.get", mock_get):
        with pytest.raises(av.AlphaVantageError):
            av.get_news_sentiment(["AAPL"])


def test_av_information_response_raises_error() -> None:
    throttle = {"Information": "Premium endpoint. Subscribe to access."}
    mock_get = MagicMock(return_value=_mock_response(throttle))

    with patch("lthcs.sources.alpha_vantage.requests.get", mock_get):
        with pytest.raises(av.AlphaVantageError):
            av.get_news_sentiment(["AAPL"])


def test_non_200_raises_error() -> None:
    resp = _mock_response({}, status_code=500, text="Server error")
    mock_get = MagicMock(return_value=resp)

    with patch("lthcs.sources.alpha_vantage.requests.get", mock_get):
        with pytest.raises(av.AlphaVantageError):
            av.get_news_sentiment(["AAPL"])


def test_missing_api_key_raises_at_first_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
    mock_get = MagicMock(return_value=_mock_response(_news_payload([])))

    with patch("lthcs.sources.alpha_vantage.requests.get", mock_get):
        with pytest.raises(RuntimeError, match="ALPHA_VANTAGE_API_KEY"):
            av.get_news_sentiment(["AAPL"])
    # And ``requests.get`` must not have been called.
    assert mock_get.call_count == 0


# ---------------------------------------------------------------------------
# get_daily_prices (fallback)
# ---------------------------------------------------------------------------


def test_get_daily_prices_parses_payload() -> None:
    payload = {
        "Meta Data": {"2. Symbol": "AAPL"},
        "Time Series (Daily)": {
            "2026-05-14": {
                "1. open": "180.0",
                "2. high": "182.5",
                "3. low": "179.5",
                "4. close": "181.0",
                "5. volume": "50000000",
            },
            "2026-05-15": {
                "1. open": "181.0",
                "2. high": "183.0",
                "3. low": "180.0",
                "4. close": "182.5",
                "5. volume": "48000000",
            },
        },
    }
    mock_get = MagicMock(return_value=_mock_response(payload))

    with patch("lthcs.sources.alpha_vantage.requests.get", mock_get):
        rows = av.get_daily_prices("AAPL")

    assert len(rows) == 2
    # Sorted ascending by date.
    assert rows[0]["date"] == "2026-05-14"
    assert rows[1]["date"] == "2026-05-15"
    assert rows[0] == {
        "date": "2026-05-14",
        "open": 180.0,
        "high": 182.5,
        "low": 179.5,
        "close": 181.0,
        "volume": 50000000,
    }

    params = mock_get.call_args.kwargs["params"]
    assert params["symbol"] == "AAPL"
    assert params["outputsize"] == "compact"
    assert params["apikey"] == "test-key"


def test_get_daily_prices_caches_result() -> None:
    payload = {
        "Time Series (Daily)": {
            "2026-05-15": {
                "1. open": "1.0",
                "2. high": "2.0",
                "3. low": "0.5",
                "4. close": "1.5",
                "5. volume": "100",
            }
        }
    }
    mock_get = MagicMock(return_value=_mock_response(payload))

    with patch("lthcs.sources.alpha_vantage.requests.get", mock_get):
        av.get_daily_prices("AAPL")
        av.get_daily_prices("AAPL")

    assert mock_get.call_count == 1
