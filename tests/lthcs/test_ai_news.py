"""Tests for lthcs.sources.ai_news.

All HTTP is mocked. The module-level caches are redirected to ``tmp_path``
via ``monkeypatch`` so every test starts with a clean cache. The HN and
RSS token buckets are replaced with generously-sized buckets by default;
specific tests that exercise rate limiting install their own.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from lthcs.sources import ai_news as ain
from lthcs.sources._cache import FileCache
from lthcs.sources._ratelimit import TokenBucket


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_caches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the module-level caches at fresh tmp dirs for every test."""
    monkeypatch.setattr(ain, "_HN_CACHE", FileCache("hn_news", root=tmp_path))
    monkeypatch.setattr(ain, "_RSS_CACHE", FileCache("ai_rss", root=tmp_path))


@pytest.fixture(autouse=True)
def generous_buckets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default to buckets that never block. Per-test overrides allowed."""
    monkeypatch.setattr(
        ain, "_HN_BUCKET", TokenBucket(capacity=1_000_000, refill_rate=1_000_000)
    )
    monkeypatch.setattr(
        ain, "_RSS_BUCKET", TokenBucket(capacity=1_000_000, refill_rate=1_000_000)
    )


def _mock_response(
    *,
    json_payload: Any = None,
    text: Optional[str] = None,
    status_code: int = 200,
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    if json_payload is not None:
        resp.json.return_value = json_payload
    else:
        resp.json.side_effect = ValueError("no JSON")
    resp.text = text if text is not None else ""
    return resp


def _hn_hit(
    title: str,
    *,
    points: int = 100,
    num_comments: int = 25,
    url: str = "",
    object_id: str = "abc123",
    created_at: str = "2026-05-15T12:34:56.000Z",
    created_at_i: int = 1778848496,
) -> Dict[str, Any]:
    return {
        "title": title,
        "url": url,
        "objectID": object_id,
        "points": points,
        "num_comments": num_comments,
        "created_at": created_at,
        "created_at_i": created_at_i,
    }


def _hn_payload(hits: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"hits": hits, "nbHits": len(hits)}


def _rss_xml(items: List[Dict[str, str]]) -> str:
    item_xml = "".join(
        (
            "<item>"
            f"<title>{i.get('title', '')}</title>"
            f"<link>{i.get('link', '')}</link>"
            f"<description>{i.get('description', '')}</description>"
            f"<pubDate>{i.get('pubDate', '')}</pubDate>"
            "</item>"
        )
        for i in items
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel>'
        "<title>Test Feed</title>"
        "<link>https://example.com/</link>"
        "<description>test</description>"
        f"{item_xml}"
        "</channel></rss>"
    )


# ---------------------------------------------------------------------------
# fetch_hn_mentions
# ---------------------------------------------------------------------------


def test_fetch_hn_mentions_parses_hits() -> None:
    payload = _hn_payload(
        [
            _hn_hit(
                "NVIDIA earnings beat",
                points=234,
                num_comments=89,
                url="https://example.com/nvda",
                object_id="11111",
            ),
            _hn_hit(
                "Nvidia ships new GPU",
                points=42,
                num_comments=10,
                url="",  # exercise the news.ycombinator.com fallback
                object_id="22222",
            ),
        ]
    )
    with patch.object(ain.requests, "get", return_value=_mock_response(json_payload=payload)) as mg:
        out = ain.fetch_hn_mentions("NVIDIA", days=14)

    assert mg.call_count == 1
    # Hit the Algolia endpoint, story tag, day cutoff in params.
    args, kwargs = mg.call_args
    assert args[0] == "https://hn.algolia.com/api/v1/search"
    params = kwargs["params"]
    assert params["query"] == "NVIDIA"
    assert params["tags"] == "story"
    assert params["numericFilters"].startswith("created_at_i>")

    assert len(out) == 2
    first = out[0]
    assert first["title"] == "NVIDIA earnings beat"
    assert first["url"] == "https://example.com/nvda"
    assert first["points"] == 234
    assert first["num_comments"] == 89
    assert first["source"] == "HN"
    assert first["time_published"] == "2026-05-15"
    # Second hit got the HN fallback URL.
    assert out[1]["url"] == "https://news.ycombinator.com/item?id=22222"


def test_fetch_hn_mentions_caches_between_calls() -> None:
    payload = _hn_payload([_hn_hit("Microsoft Azure update")])
    with patch.object(ain.requests, "get", return_value=_mock_response(json_payload=payload)) as mg:
        first = ain.fetch_hn_mentions("Microsoft", days=30)
        second = ain.fetch_hn_mentions("Microsoft", days=30)
    assert mg.call_count == 1
    assert first == second
    assert first[0]["title"] == "Microsoft Azure update"


def test_fetch_hn_mentions_empty_query_skips_http() -> None:
    with patch.object(ain.requests, "get") as mg:
        assert ain.fetch_hn_mentions("", days=30) == []
        assert ain.fetch_hn_mentions("   ", days=30) == []
    mg.assert_not_called()


def test_fetch_hn_mentions_non_200_returns_empty() -> None:
    with patch.object(
        ain.requests, "get", return_value=_mock_response(status_code=503, text="oops")
    ):
        assert ain.fetch_hn_mentions("NVIDIA") == []


def test_fetch_hn_mentions_request_exception_returns_empty() -> None:
    with patch.object(
        ain.requests, "get", side_effect=ain.requests.RequestException("boom")
    ):
        assert ain.fetch_hn_mentions("NVIDIA") == []


def test_fetch_hn_mentions_drops_titleless_hits() -> None:
    payload = _hn_payload(
        [
            _hn_hit("", points=10, object_id="x1"),
            _hn_hit("NVIDIA partners with X", points=15, object_id="x2"),
        ]
    )
    with patch.object(ain.requests, "get", return_value=_mock_response(json_payload=payload)):
        out = ain.fetch_hn_mentions("NVIDIA")
    assert len(out) == 1
    assert out[0]["title"] == "NVIDIA partners with X"


def test_fetch_hn_mentions_rate_limit_skips_http(monkeypatch: pytest.MonkeyPatch) -> None:
    # Empty bucket, no refill — try_acquire is False immediately.
    monkeypatch.setattr(
        ain, "_HN_BUCKET", TokenBucket(capacity=1, refill_rate=0.0)
    )
    # Drain the single token.
    ain._HN_BUCKET.try_acquire()
    with patch.object(ain.requests, "get") as mg:
        assert ain.fetch_hn_mentions("NVIDIA") == []
    mg.assert_not_called()


# ---------------------------------------------------------------------------
# fetch_techcrunch_feed / fetch_venturebeat_feed
# ---------------------------------------------------------------------------


def test_fetch_techcrunch_feed_parses_items() -> None:
    xml = _rss_xml(
        [
            {
                "title": "OpenAI announces new model",
                "link": "https://techcrunch.com/openai",
                "description": "<p>The model is faster.</p>",
                "pubDate": "Sat, 16 May 2026 09:00:00 +0000",
            },
            {
                "title": "NVIDIA hits new high",
                "link": "https://techcrunch.com/nvda",
                "description": "Earnings beat.",
                "pubDate": "Fri, 15 May 2026 12:00:00 +0000",
            },
        ]
    )
    with patch.object(
        ain.requests, "get", return_value=_mock_response(text=xml)
    ) as mg:
        out = ain.fetch_techcrunch_feed()

    mg.assert_called_once()
    assert mg.call_args[0][0] == "https://techcrunch.com/feed/"
    assert len(out) == 2
    assert out[0]["title"] == "OpenAI announces new model"
    assert out[0]["url"] == "https://techcrunch.com/openai"
    assert out[0]["summary"] == "The model is faster."
    assert out[0]["time_published"] == "2026-05-16"
    assert out[0]["source"] == "TechCrunch"


def test_fetch_techcrunch_feed_caches() -> None:
    xml = _rss_xml([{"title": "T", "link": "x", "description": "d", "pubDate": ""}])
    with patch.object(
        ain.requests, "get", return_value=_mock_response(text=xml)
    ) as mg:
        a = ain.fetch_techcrunch_feed()
        b = ain.fetch_techcrunch_feed()
    assert mg.call_count == 1
    assert a == b


def test_fetch_venturebeat_feed_uses_vb_url() -> None:
    xml = _rss_xml(
        [{"title": "VB story", "link": "https://vb/x", "description": "", "pubDate": ""}]
    )
    with patch.object(
        ain.requests, "get", return_value=_mock_response(text=xml)
    ) as mg:
        out = ain.fetch_venturebeat_feed()
    assert mg.call_args[0][0] == "https://venturebeat.com/feed/"
    assert out[0]["source"] == "VentureBeat"


def test_rss_malformed_xml_returns_empty() -> None:
    with patch.object(
        ain.requests, "get", return_value=_mock_response(text="<this is not> xml")
    ):
        assert ain.fetch_techcrunch_feed() == []


def test_rss_empty_text_returns_empty() -> None:
    with patch.object(ain.requests, "get", return_value=_mock_response(text="")):
        assert ain.fetch_techcrunch_feed() == []


def test_rss_non_200_returns_empty() -> None:
    with patch.object(
        ain.requests, "get", return_value=_mock_response(status_code=500, text="<x/>")
    ):
        assert ain.fetch_venturebeat_feed() == []


def test_rss_skips_titleless_items() -> None:
    xml = _rss_xml(
        [
            {"title": "", "link": "x", "description": "", "pubDate": ""},
            {"title": "Real story", "link": "y", "description": "", "pubDate": ""},
        ]
    )
    with patch.object(ain.requests, "get", return_value=_mock_response(text=xml)):
        out = ain.fetch_techcrunch_feed()
    assert len(out) == 1
    assert out[0]["title"] == "Real story"


def test_rss_rate_limited_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ain, "_RSS_BUCKET", TokenBucket(capacity=1, refill_rate=0.0))
    ain._RSS_BUCKET.try_acquire()  # drain
    with patch.object(ain.requests, "get") as mg:
        assert ain.fetch_techcrunch_feed() == []
    mg.assert_not_called()


# ---------------------------------------------------------------------------
# aggregate_ai_news
# ---------------------------------------------------------------------------


def _stub_get_factory(
    hn_payloads_by_query: Dict[str, Dict[str, Any]],
    tc_xml: str,
    vb_xml: str,
) -> Any:
    """Return a callable usable as ``requests.get`` side_effect."""

    def _stub(url: str, **kwargs: Any) -> MagicMock:
        if url == ain._HN_URL:
            q = (kwargs.get("params") or {}).get("query", "")
            payload = hn_payloads_by_query.get(q, _hn_payload([]))
            return _mock_response(json_payload=payload)
        if url == ain._TECHCRUNCH_URL:
            return _mock_response(text=tc_xml)
        if url == ain._VENTUREBEAT_URL:
            return _mock_response(text=vb_xml)
        return _mock_response(status_code=404, text="not found")

    return _stub


def test_aggregate_ai_news_combines_hn_and_rss() -> None:
    hn_payloads = {
        "NVIDIA": _hn_payload(
            [
                _hn_hit("NVIDIA earnings beat", points=234, num_comments=89, object_id="n1"),
                _hn_hit("Nvidia ships GPU",     points=100, num_comments=40, object_id="n2"),
            ]
        ),
        # Nvidia/NVIDIA share keywords list; we dedupe by URL+title.
        "Nvidia": _hn_payload(
            [
                _hn_hit("Nvidia ships GPU",     points=100, num_comments=40, object_id="n2"),
            ]
        ),
        "Microsoft": _hn_payload(
            [
                _hn_hit("Microsoft AI launch", points=80, num_comments=15, object_id="m1"),
            ]
        ),
        "Azure":   _hn_payload([]),
        "Copilot": _hn_payload([]),
    }
    tc_xml = _rss_xml(
        [
            {
                "title": "TechCrunch: NVIDIA partners with X",
                "link": "https://tc/nvda",
                "description": "Nvidia did a thing.",
                "pubDate": "Fri, 15 May 2026 12:00:00 +0000",
            },
            {
                "title": "Unrelated story",
                "link": "https://tc/x",
                "description": "Nothing relevant here.",
                "pubDate": "Thu, 14 May 2026 12:00:00 +0000",
            },
        ]
    )
    vb_xml = _rss_xml(
        [
            {
                "title": "VentureBeat: Microsoft Copilot update",
                "link": "https://vb/ms",
                "description": "Microsoft pushed an update.",
                "pubDate": "Sat, 16 May 2026 12:00:00 +0000",
            },
        ]
    )

    stub = _stub_get_factory(hn_payloads, tc_xml, vb_xml)
    with patch.object(ain.requests, "get", side_effect=stub):
        out = ain.aggregate_ai_news(["NVDA", "MSFT"], days=30)

    assert set(out.keys()) == {"NVDA", "MSFT"}

    nvda = out["NVDA"]
    # Two unique HN stories after dedupe.
    assert nvda["hn_mention_count"] == 2
    assert nvda["hn_total_points"] == 234 + 100
    assert nvda["hn_total_comments"] == 89 + 40
    # One RSS hit ("partners with X"); the unrelated story was filtered out.
    assert nvda["rss_mention_count"] == 1
    assert nvda["total_mentions"] == 3
    assert nvda["first_seen"] is not None
    assert nvda["last_seen"] is not None
    assert nvda["first_seen"] <= nvda["last_seen"]
    assert len(nvda["sample_titles"]) <= 3
    # Highest-points HN title ranks first.
    assert nvda["sample_titles"][0] == "NVIDIA earnings beat"

    msft = out["MSFT"]
    assert msft["hn_mention_count"] == 1
    assert msft["rss_mention_count"] == 1   # VB Copilot story matches "Copilot"
    assert msft["total_mentions"] == 2


def test_aggregate_ai_news_unknown_ticker_returns_empty() -> None:
    # JNJ is not in TICKER_KEYWORDS — no HN search, no RSS filter applied.
    with patch.object(ain.requests, "get") as mg:
        # The RSS feeds *are* still fetched (shared pool), so allow that.
        mg.return_value = _mock_response(text=_rss_xml([]))
        out = ain.aggregate_ai_news(["JNJ"])
    assert "JNJ" in out
    jnj = out["JNJ"]
    assert jnj["ticker"] == "JNJ"
    assert jnj["hn_mention_count"] == 0
    assert jnj["rss_mention_count"] == 0
    assert jnj["total_mentions"] == 0
    assert jnj["sample_titles"] == []
    assert jnj["first_seen"] is None
    assert jnj["last_seen"] is None


def test_aggregate_ai_news_empty_input() -> None:
    with patch.object(ain.requests, "get") as mg:
        assert ain.aggregate_ai_news([]) == {}
    mg.assert_not_called()


def test_aggregate_ai_news_rss_pool_shared_across_tickers() -> None:
    """RSS feeds should be pulled exactly once per call, regardless of ticker count."""
    hn_payloads: Dict[str, Dict[str, Any]] = {}  # empty HN responses
    tc_xml = _rss_xml([])
    vb_xml = _rss_xml([])
    stub = _stub_get_factory(hn_payloads, tc_xml, vb_xml)
    with patch.object(ain.requests, "get", side_effect=stub) as mg:
        ain.aggregate_ai_news(["NVDA", "MSFT", "AMD", "META"], days=30)
    # Count RSS URL hits.
    rss_calls = [c for c in mg.call_args_list if c.args and c.args[0] in
                 (ain._TECHCRUNCH_URL, ain._VENTUREBEAT_URL)]
    assert len(rss_calls) == 2  # one TC, one VB


# ---------------------------------------------------------------------------
# compute_thesis_signal_from_news
# ---------------------------------------------------------------------------


def test_thesis_signal_zero_mentions() -> None:
    sig = ain.compute_thesis_signal_from_news(ain._empty_aggregate("AAPL"))
    assert sig["ticker"] == "AAPL"
    assert sig["article_count"] == 0
    assert sig["mean_sentiment_score"] is None
    assert sig["mean_relevance_score"] is None
    # Label counts present but zeroed.
    assert sig["label_counts"] == {
        "Bearish": 0, "Somewhat-Bearish": 0, "Neutral": 0,
        "Somewhat-Bullish": 0, "Bullish": 0,
    }
    assert sig["source"] == "ai_news_aggregate"
    assert sig["last_scored"] == ain._today_iso()


def test_thesis_signal_low_engagement_neutral() -> None:
    agg = {
        "ticker": "NVDA",
        "hn_mention_count": 1,
        "hn_total_points": 5,
        "hn_total_comments": 1,
        "rss_mention_count": 1,
        "total_mentions": 2,
        "sample_titles": ["t1", "t2"],
        "first_seen": "2026-05-10",
        "last_seen": "2026-05-15",
    }
    sig = ain.compute_thesis_signal_from_news(agg)
    # 1-2 mentions => neutral 0.0, not None
    assert sig["article_count"] == 2
    assert sig["mean_sentiment_score"] == pytest.approx(0.15)
    assert sig["label_counts"]["Neutral"] == 2


def test_thesis_signal_high_engagement_positive() -> None:
    # 5 mentions, total points 400 => avg 80 >= threshold 50.
    agg = {
        "ticker": "NVDA",
        "hn_mention_count": 5,
        "hn_total_points": 400,
        "hn_total_comments": 50,
        "rss_mention_count": 0,
        "total_mentions": 5,
        "sample_titles": [],
        "first_seen": "2026-05-01",
        "last_seen": "2026-05-15",
    }
    sig = ain.compute_thesis_signal_from_news(agg)
    assert sig["article_count"] == 5
    assert sig["mean_sentiment_score"] == pytest.approx(0.60)
    assert sig["mean_relevance_score"] == 0.5
    assert sig["label_counts"]["Neutral"] == 5


def test_thesis_signal_3_mentions_low_engagement() -> None:
    # 3 mentions but average points 10 and comments 5 — below thresholds.
    # Bumped 2026-05-17: 3+ mentions with LOW engagement gets +0.25
    # (in the news cycle but not viral).
    agg = {
        "ticker": "MSFT",
        "hn_mention_count": 3,
        "hn_total_points": 30,
        "hn_total_comments": 15,
        "rss_mention_count": 0,
        "total_mentions": 3,
        "sample_titles": [],
        "first_seen": None,
        "last_seen": None,
    }
    sig = ain.compute_thesis_signal_from_news(agg)
    assert sig["mean_sentiment_score"] == pytest.approx(0.35)


def test_thesis_signal_engagement_via_comments_only() -> None:
    # Points low but comments high: should still trigger positive.
    agg = {
        "ticker": "META",
        "hn_mention_count": 4,
        "hn_total_points": 20,         # avg 5 — below points threshold
        "hn_total_comments": 200,      # avg 50 — above comments threshold (30)
        "rss_mention_count": 0,
        "total_mentions": 4,
        "sample_titles": [],
        "first_seen": None,
        "last_seen": None,
    }
    sig = ain.compute_thesis_signal_from_news(agg)
    assert sig["mean_sentiment_score"] == pytest.approx(0.60)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_to_iso_date_handles_known_formats() -> None:
    assert ain._to_iso_date("2026-05-15T12:34:56.000Z") == "2026-05-15"
    assert ain._to_iso_date("Sat, 16 May 2026 09:00:00 +0000") == "2026-05-16"
    assert ain._to_iso_date(1778848496) == "2026-05-15"
    assert ain._to_iso_date("") is None
    assert ain._to_iso_date(None) is None
    assert ain._to_iso_date("not a date") is None


def test_strip_html_handles_common_entities() -> None:
    raw = "<p>Hello&nbsp;world &amp; friends &#39;quoted&#39;</p>"
    assert ain._strip_html(raw) == "Hello world & friends 'quoted'"


def test_ticker_keywords_includes_core_cohort() -> None:
    # Sanity-check the canonical AI cohort is present.
    for t in ("NVDA", "MSFT", "GOOGL", "META", "AMD", "AVGO", "ORCL"):
        assert t in ain.TICKER_KEYWORDS
        assert ain.TICKER_KEYWORDS[t], f"{t} has empty keyword list"
