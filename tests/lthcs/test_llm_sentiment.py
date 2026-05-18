"""Tests for lthcs.sources.llm_sentiment.

These tests never make a real Anthropic API call. The SDK client is
substituted with a small stand-in object (mirrors test_narratives_llm.py).
Coverage:

* Prompt construction (system block carries cache_control; user message
  carries ticker + news items; payload trimmed and ranked by HN points)
* Happy path: valid JSON response -> normalized output dict
* Fallback paths: missing API key, API error, malformed JSON, empty
  response, empty news input
* Score clamping to [-1, +1]
* Label/score consistency enforced after the fact
* Concurrency on the universe helper
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, List

import pytest

from lthcs.sources import llm_sentiment


# ---------------------------------------------------------------------------
# Fake Anthropic client (matches the SDK surface we touch)
# ---------------------------------------------------------------------------


class _FakeUsage:
    def __init__(
        self,
        input_tokens: int = 1240,
        output_tokens: int = 180,
        cache_read: int = 1100,
        cache_create: int = 0,
    ):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cache_read
        self.cache_creation_input_tokens = cache_create


class _FakeBlock:
    def __init__(self, text: str):
        self.text = text
        self.type = "text"


class _FakeResponse:
    def __init__(self, text: str, usage: _FakeUsage = None):
        self.content = [_FakeBlock(text)]
        self.usage = usage or _FakeUsage()


class _FakeMessages:
    def __init__(self, text: str = "{}", raise_exc=None, delay: float = 0.0):
        self.text = text
        self.calls: List[Dict[str, Any]] = []
        self.raise_exc = raise_exc
        self.delay = delay
        self.lock = threading.Lock()
        self.concurrency_seen = 0
        self._in_flight = 0
        self._in_flight_lock = threading.Lock()

    def create(self, **kwargs):
        with self._in_flight_lock:
            self._in_flight += 1
            self.concurrency_seen = max(self.concurrency_seen, self._in_flight)
        try:
            if self.delay:
                time.sleep(self.delay)
            with self.lock:
                self.calls.append(kwargs)
            if self.raise_exc is not None:
                raise self.raise_exc
            # Allow per-call response override via a "text_fn" kwarg attached
            # to self for richer behavior.
            text = self._resolve_text(kwargs)
            return _FakeResponse(text)
        finally:
            with self._in_flight_lock:
                self._in_flight -= 1

    def _resolve_text(self, kwargs: Dict[str, Any]) -> str:
        # If self.text is a callable, invoke per call (gives per-ticker
        # responses). Otherwise return the static text.
        text = self.text
        if callable(text):
            return text(kwargs)
        return text


class _FakeClient:
    def __init__(self, text="{}", raise_exc=None, delay: float = 0.0):
        self.messages = _FakeMessages(text=text, raise_exc=raise_exc, delay=delay)


def _good_json(score: float = 0.62) -> str:
    return json.dumps(
        {
            "mean_sentiment_score": score,
            "label": "bullish",
            "polarity_confidence": 0.85,
            "key_drivers": [
                "Q4 earnings beat",
                "data-center revenue +35%",
                "AI-cluster demand",
            ],
            "key_risks": [
                "competition from custom silicon",
                "consumer-GPU price pressure",
            ],
            "rationale": "Earnings beat plus AI-cluster traction.",
        }
    )


def _nvda_news() -> List[Dict[str, Any]]:
    return [
        {
            "title": "Nvidia Q4 earnings beat, data-center +35% YoY",
            "summary": "Strong AI cluster demand drives the upside.",
            "source": "HN",
            "points": 250,
            "num_comments": 80,
            "time_published": "2026-05-15",
        },
        {
            "title": "Custom silicon competition heats up: Microsoft, Google",
            "summary": "Hyperscalers building in-house accelerators.",
            "source": "TechCrunch",
            "time_published": "2026-05-14",
        },
        {
            "title": "Consumer GPU pricing softens into Q1",
            "summary": "RTX 50-series channel inventory builds.",
            "source": "VentureBeat",
            "time_published": "2026-05-13",
        },
    ]


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def test_system_blocks_carry_cache_control():
    blocks = llm_sentiment.build_system_blocks()
    assert len(blocks) == 1
    assert blocks[0]["type"] == "text"
    assert "LTHCS" in blocks[0]["text"]
    assert "sentiment scale" in blocks[0]["text"].lower()
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}


def test_user_message_contains_ticker_and_news_items():
    msg = llm_sentiment.build_user_message("nvda", _nvda_news())
    assert "NVDA" in msg  # ticker is upper-cased
    assert "data-center" in msg
    assert "custom silicon" in msg.lower()
    # Should also include source labels.
    assert "HN" in msg


def test_user_message_truncates_to_max_news_items():
    items = [
        {
            "title": f"Story {i}",
            "summary": "x",
            "source": "HN",
            "points": 100 - i,  # so ranking is stable: story 0 -> top
            "time_published": "2026-05-15",
        }
        for i in range(15)
    ]
    msg = llm_sentiment.build_user_message("AAPL", items, max_news_items=3)
    # Extract JSON payload.
    payload = json.loads(msg[msg.index("{"):])
    assert payload["item_count"] == 3
    # Highest-points items should win.
    titles = [it["title"] for it in payload["news_items"]]
    assert titles == ["Story 0", "Story 1", "Story 2"]


def test_user_message_drops_titleless_items():
    items = [
        {"title": "Real headline", "summary": "x", "source": "HN"},
        {"title": "", "summary": "no title", "source": "HN"},
        {"summary": "missing title", "source": "RSS"},
    ]
    msg = llm_sentiment.build_user_message("AAPL", items)
    payload = json.loads(msg[msg.index("{"):])
    assert payload["item_count"] == 1
    assert payload["news_items"][0]["title"] == "Real headline"


# ---------------------------------------------------------------------------
# Happy path + cache_control plumbing
# ---------------------------------------------------------------------------


def test_happy_path_returns_normalized_dict():
    client = _FakeClient(text=_good_json(score=0.62))
    out = llm_sentiment.compute_llm_sentiment(
        ticker="NVDA",
        news_items=_nvda_news(),
        client=client,
    )
    assert out["fallback"] is False
    assert out["fallback_reason"] is None
    assert out["ticker"] == "NVDA"
    assert out["model"] == llm_sentiment.DEFAULT_MODEL
    assert out["mean_sentiment_score"] == 0.62
    assert out["label"] == "bullish"
    assert out["polarity_confidence"] == 0.85
    assert "Q4 earnings beat" in out["key_drivers"]
    assert "competition from custom silicon" in out["key_risks"]
    assert out["raw_classification"] is not None
    assert out["input_tokens"] == 1240
    assert out["output_tokens"] == 180
    assert out["cached_input_tokens"] == 1100
    assert out["generated_at"].endswith("Z")
    assert out["article_count"] == 3


def test_cache_control_placed_on_system_block_in_api_call():
    client = _FakeClient(text=_good_json())
    llm_sentiment.compute_llm_sentiment(
        ticker="NVDA",
        news_items=_nvda_news(),
        client=client,
    )
    assert len(client.messages.calls) == 1
    call = client.messages.calls[0]
    system_blocks = call["system"]
    assert isinstance(system_blocks, list)
    for blk in system_blocks:
        assert blk.get("cache_control") == {"type": "ephemeral"}
    # User message is plain string.
    msgs = call["messages"]
    assert msgs[0]["role"] == "user"
    assert isinstance(msgs[0]["content"], str)


def test_use_cache_false_strips_cache_control():
    client = _FakeClient(text=_good_json())
    llm_sentiment.compute_llm_sentiment(
        ticker="NVDA",
        news_items=_nvda_news(),
        client=client,
        use_cache=False,
    )
    call = client.messages.calls[0]
    for blk in call["system"]:
        assert "cache_control" not in blk


def test_custom_model_propagates_to_api_call():
    client = _FakeClient(text=_good_json())
    llm_sentiment.compute_llm_sentiment(
        ticker="NVDA",
        news_items=_nvda_news(),
        client=client,
        model="claude-opus-4-5",
    )
    assert client.messages.calls[0]["model"] == "claude-opus-4-5"


# ---------------------------------------------------------------------------
# Fallback paths
# ---------------------------------------------------------------------------


def test_missing_api_key_falls_back_to_engagement_heuristic(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = llm_sentiment.compute_llm_sentiment(
        ticker="NVDA",
        news_items=_nvda_news(),
    )
    assert out["fallback"] is True
    assert out["fallback_reason"] == "missing_api_key"
    assert out["ticker"] == "NVDA"
    # Heuristic returns a real score; with 3 mentions and HN points >= 80
    # average, we should be in the high-engagement tier.
    assert out["mean_sentiment_score"] is not None
    assert out["label"] in llm_sentiment.VALID_LABELS


def test_api_error_falls_back_to_engagement_heuristic():
    client = _FakeClient(raise_exc=RuntimeError("network down"))
    out = llm_sentiment.compute_llm_sentiment(
        ticker="NVDA",
        news_items=_nvda_news(),
        client=client,
    )
    assert out["fallback"] is True
    assert "api_error" in out["fallback_reason"]
    assert out["ticker"] == "NVDA"


def test_empty_response_falls_back():
    client = _FakeClient(text="")
    out = llm_sentiment.compute_llm_sentiment(
        ticker="NVDA",
        news_items=_nvda_news(),
        client=client,
    )
    assert out["fallback"] is True
    assert out["fallback_reason"] == "empty_response"


def test_malformed_json_falls_back():
    client = _FakeClient(text="this is not JSON at all, just prose")
    out = llm_sentiment.compute_llm_sentiment(
        ticker="NVDA",
        news_items=_nvda_news(),
        client=client,
    )
    assert out["fallback"] is True
    assert out["fallback_reason"] == "json_parse_error"


def test_partial_json_missing_score_falls_back():
    client = _FakeClient(text=json.dumps({"label": "bullish"}))
    out = llm_sentiment.compute_llm_sentiment(
        ticker="NVDA",
        news_items=_nvda_news(),
        client=client,
    )
    assert out["fallback"] is True
    assert out["fallback_reason"] == "json_parse_error"


def test_empty_news_returns_neutral_no_news():
    out = llm_sentiment.compute_llm_sentiment(
        ticker="ZZZ",
        news_items=[],
    )
    assert out["fallback"] is True
    assert out["fallback_reason"] == "no_news"
    assert out["mean_sentiment_score"] is None
    assert out["label"] == "neutral"


# ---------------------------------------------------------------------------
# Score clamping + label consistency
# ---------------------------------------------------------------------------


def test_score_clamped_to_unit_range():
    client = _FakeClient(
        text=json.dumps({"mean_sentiment_score": 2.7, "label": "extreme_bullish"})
    )
    out = llm_sentiment.compute_llm_sentiment(
        ticker="NVDA",
        news_items=_nvda_news(),
        client=client,
    )
    assert out["fallback"] is False
    assert out["mean_sentiment_score"] == 1.0
    assert out["label"] == "extreme_bullish"


def test_score_clamped_below_negative_one():
    client = _FakeClient(
        text=json.dumps({"mean_sentiment_score": -3.5, "label": "extreme_bearish"})
    )
    out = llm_sentiment.compute_llm_sentiment(
        ticker="NVDA",
        news_items=_nvda_news(),
        client=client,
    )
    assert out["fallback"] is False
    assert out["mean_sentiment_score"] == -1.0
    assert out["label"] == "extreme_bearish"


def test_label_corrected_when_inconsistent_with_score():
    # Model returns bearish score but mislabels bullish; we trust the score.
    client = _FakeClient(
        text=json.dumps({"mean_sentiment_score": -0.5, "label": "bullish"})
    )
    out = llm_sentiment.compute_llm_sentiment(
        ticker="NVDA",
        news_items=_nvda_news(),
        client=client,
    )
    assert out["fallback"] is False
    assert out["mean_sentiment_score"] == -0.5
    assert out["label"] == "bearish"


def test_label_from_score_bands():
    assert llm_sentiment._label_from_score(-0.9) == "extreme_bearish"
    assert llm_sentiment._label_from_score(-0.5) == "bearish"
    assert llm_sentiment._label_from_score(-0.2) == "neutral"
    assert llm_sentiment._label_from_score(0.0) == "neutral"
    assert llm_sentiment._label_from_score(0.2) == "neutral"
    assert llm_sentiment._label_from_score(0.5) == "bullish"
    assert llm_sentiment._label_from_score(0.9) == "extreme_bullish"
    # Edge: exactly +1.0 and -1.0
    assert llm_sentiment._label_from_score(1.0) == "extreme_bullish"
    assert llm_sentiment._label_from_score(-1.0) == "extreme_bearish"


# ---------------------------------------------------------------------------
# Universe batch helper
# ---------------------------------------------------------------------------


def test_universe_helper_returns_one_entry_per_ticker():
    news = {
        "NVDA": _nvda_news(),
        "AAPL": [
            {
                "title": "Apple Q2 in-line, services growth steady",
                "summary": "Mixed quarter.",
                "source": "TechCrunch",
                "time_published": "2026-05-12",
            }
        ],
        "MSFT": [
            {
                "title": "Azure AI revenue up sharply",
                "summary": "Cloud accelerates.",
                "source": "HN",
                "points": 120,
                "num_comments": 40,
                "time_published": "2026-05-11",
            }
        ],
    }
    client = _FakeClient(text=_good_json(score=0.5))
    out = llm_sentiment.compute_universe_llm_sentiment(
        news_by_ticker=news,
        client=client,
        max_concurrency=2,
    )
    assert set(out.keys()) == {"NVDA", "AAPL", "MSFT"}
    for ticker, sig in out.items():
        assert sig["ticker"] == ticker
        assert sig["fallback"] is False
        assert sig["mean_sentiment_score"] == 0.5
        assert sig["label"] == "bullish"


def test_universe_helper_falls_back_when_no_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    news = {
        "NVDA": _nvda_news(),
        "AAPL": _nvda_news(),
    }
    out = llm_sentiment.compute_universe_llm_sentiment(news_by_ticker=news)
    assert set(out.keys()) == {"NVDA", "AAPL"}
    for sig in out.values():
        assert sig["fallback"] is True
        assert sig["fallback_reason"] == "missing_api_key"


def test_universe_helper_handles_empty_input():
    out = llm_sentiment.compute_universe_llm_sentiment(news_by_ticker={})
    assert out == {}


def test_universe_helper_respects_max_concurrency():
    news = {f"T{i}": _nvda_news() for i in range(8)}
    client = _FakeClient(text=_good_json(), delay=0.05)
    out = llm_sentiment.compute_universe_llm_sentiment(
        news_by_ticker=news,
        client=client,
        max_concurrency=3,
    )
    assert len(out) == 8
    # Should never see more than the cap in flight concurrently.
    assert client.messages.concurrency_seen <= 3
    # And it should actually have parallelized -- saw at least 2 in flight.
    assert client.messages.concurrency_seen >= 2


# ---------------------------------------------------------------------------
# JSON envelope parsing edge cases
# ---------------------------------------------------------------------------


def test_json_envelope_handles_markdown_fence():
    fenced = "```json\n" + _good_json() + "\n```"
    client = _FakeClient(text=fenced)
    out = llm_sentiment.compute_llm_sentiment(
        ticker="NVDA",
        news_items=_nvda_news(),
        client=client,
    )
    assert out["fallback"] is False
    assert out["mean_sentiment_score"] == 0.62


def test_json_envelope_handles_leading_prose():
    text = "Here is the classification:\n\n" + _good_json()
    client = _FakeClient(text=text)
    out = llm_sentiment.compute_llm_sentiment(
        ticker="NVDA",
        news_items=_nvda_news(),
        client=client,
    )
    assert out["fallback"] is False
    assert out["mean_sentiment_score"] == 0.62
