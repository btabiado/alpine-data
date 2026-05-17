"""Tests for lthcs.narratives_llm.

These tests never make a real Anthropic API call. The SDK client is
substituted with a small stand-in object (or monkeypatched at import).
We test:

* Prompt construction (system + user content, cache_control placement)
* Fallback path when ANTHROPIC_API_KEY is absent
* Fallback path when the SDK call raises
* Happy path returning the LLM text + usage telemetry
* The universe batch helper handles concurrency + per-ticker fallback
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from lthcs import narratives_llm


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _snapshot_row(
    *,
    ticker: str = "AAPL",
    score: float = 58.7,
    band: str = "weakening",
    subscores=None,
) -> Dict[str, Any]:
    if subscores is None:
        subscores = {
            "adoption_momentum": 50.0,
            "institutional_confidence": 68.5,
            "financial_evolution": 67.5,
            "thesis_integrity": 62.4,
            "des": 44.9,
        }
    return {
        "ticker": ticker,
        "lthcs_score": score,
        "band": band,
        "drift_1d": 0.0,
        "drift_30d": 0.0,
        "confidence_level": "high",
        "subscores": dict(subscores),
        "sector": "Technology",
    }


def _variable_detail_rows(ticker: str = "AAPL") -> List[Dict[str, Any]]:
    return [
        {
            "ticker": ticker,
            "pillar": "institutional_confidence",
            "sub_score": 68.5,
            "components": {
                "insider": {
                    "regime": "heavy_selling",
                    "conviction_score": -1.0,
                },
                "holdings": {
                    "conviction_signal": "mixed",
                    "manager_count": 10,
                },
            },
            "data_quality": {"has_insider": True, "has_holdings": True},
        },
    ]


def _insider() -> Dict[str, Any]:
    return {
        "as_of": "2026-05-17",
        "conviction_score": -1.0,
        "cluster_buying": False,
        "ceo_cfo_action": "neutral",
        "buy_count": 0,
        "net_dollar_value": -71189722.31,
        "raw_transactions": [
            {
                "code": "S",
                "date": "2026-05-06",
                "insider": "LEVINSON ARTHUR D",
                "planned_10b5_1": False,
                "price": 284.57,
                "role": "Director",
                "shares": 149527.0,
                "value": 42550898.39,
            },
            {
                "code": "S",
                "date": "2026-04-23",
                "insider": "Parekh Kevan",
                "planned_10b5_1": True,
                "price": 275.0,
                "role": "CFO",
                "shares": 1534.0,
                "value": 421850.0,
            },
        ],
    }


def _holdings() -> Dict[str, Any]:
    return {
        "as_of": "2026-05-17",
        "conviction_signal": "mixed",
        "signal_score": -0.1,
        "manager_count": 10,
        "latest_quarter": "2026-Q1",
        "quarter_over_quarter": {
            "share_change_pct": 4.88,
            "net_buyers": 4,
            "net_sellers": 5,
        },
        "top_holders": [
            {"manager": "BlackRock", "rank": 1, "value_bn": 291.03},
            {"manager": "State Street", "rank": 2, "value_bn": 153.016},
            {"manager": "Goldman Sachs", "rank": 3, "value_bn": 31.19},
        ],
    }


def _macro() -> Dict[str, Any]:
    return {
        "as_of": "2026-05-17",
        "hy_oas": {"current": 2.76},
        "ig_oas": {"current": 0.76},
        "yield_curve_2s10s": {"current": 0.5, "inverted": False},
        "regime_flags": {"curve_inverted": False, "dollar_strong": False, "hy_stress": False},
    }


class _FakeUsage:
    def __init__(self, input_tokens=1247, output_tokens=198, cache_read=1100, cache_create=0):
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
    def __init__(self, text: str = "Apple continues to weaken in the LTHCS framework.", raise_exc=None):
        self.text = text
        self.calls: List[Dict[str, Any]] = []
        self.raise_exc = raise_exc

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_exc is not None:
            raise self.raise_exc
        return _FakeResponse(self.text)


class _FakeClient:
    def __init__(self, text: str = "Apple continues to weaken in the LTHCS framework.", raise_exc=None):
        self.messages = _FakeMessages(text=text, raise_exc=raise_exc)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def test_system_blocks_include_framework_and_macro():
    blocks = narratives_llm.build_system_blocks(macro_breadth=_macro())
    assert len(blocks) == 2
    assert "LTHCS" in blocks[0]["text"]
    assert "Adoption Momentum" in blocks[0]["text"]
    # Both blocks carry cache_control -- the whole prefix is cached.
    for b in blocks:
        assert b.get("cache_control") == {"type": "ephemeral"}
    assert "macro overlay" in blocks[1]["text"].lower()
    assert "2.76" in blocks[1]["text"]


def test_system_blocks_skip_macro_when_absent():
    blocks = narratives_llm.build_system_blocks(macro_breadth=None)
    assert len(blocks) == 1
    assert "LTHCS" in blocks[0]["text"]


def test_user_message_contains_ticker_and_scores():
    msg = narratives_llm.build_user_message(
        ticker="AAPL",
        snapshot_row=_snapshot_row(),
        variable_detail_rows=_variable_detail_rows(),
        insider_data=_insider(),
        holdings_data=_holdings(),
    )
    assert "AAPL" in msg
    assert "58.7" in msg or "58.7," in msg or "\"lthcs_score\": 58.7" in msg
    # Sub-score names are JSON-serialized as snake_case keys.
    assert "institutional_confidence" in msg
    assert "LEVINSON" in msg  # top open-market transaction surfaces
    assert "BlackRock" in msg
    # The binding+supporting pillar hint is included in payload.
    assert "binding_and_supporting" in msg


def test_user_message_handles_missing_insider_and_holdings():
    msg = narratives_llm.build_user_message(
        ticker="ZZZ",
        snapshot_row=_snapshot_row(ticker="ZZZ"),
        variable_detail_rows=[],
        insider_data=None,
        holdings_data=None,
    )
    payload_start = msg.find("{")
    payload = json.loads(msg[payload_start:])
    assert payload["insider"]["available"] is False
    assert payload["holdings"]["available"] is False


# ---------------------------------------------------------------------------
# Fallback paths
# ---------------------------------------------------------------------------


def test_missing_api_key_falls_back_to_template(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = narratives_llm.generate_llm_narrative(
        ticker="AAPL",
        snapshot_row=_snapshot_row(),
        variable_detail_rows=_variable_detail_rows(),
        insider_data=_insider(),
        holdings_data=_holdings(),
        macro_breadth=_macro(),
    )
    assert out["fallback"] is True
    assert out["fallback_reason"] == "missing_api_key"
    assert out["ticker"] == "AAPL"
    assert isinstance(out["narrative"], str)
    assert len(out["narrative"]) > 20
    assert out["input_tokens"] == 0
    assert out["output_tokens"] == 0


def test_api_error_falls_back_to_template():
    client = _FakeClient(raise_exc=RuntimeError("network down"))
    out = narratives_llm.generate_llm_narrative(
        ticker="AAPL",
        snapshot_row=_snapshot_row(),
        variable_detail_rows=_variable_detail_rows(),
        client=client,
    )
    assert out["fallback"] is True
    assert "api_error" in out["fallback_reason"]
    assert "AAPL" in out["narrative"]


def test_empty_response_falls_back_to_template():
    client = _FakeClient(text="")
    out = narratives_llm.generate_llm_narrative(
        ticker="AAPL",
        snapshot_row=_snapshot_row(),
        variable_detail_rows=_variable_detail_rows(),
        client=client,
    )
    assert out["fallback"] is True
    assert out["fallback_reason"] == "empty_response"


# ---------------------------------------------------------------------------
# Happy path + cache_control plumbing
# ---------------------------------------------------------------------------


def test_happy_path_returns_text_and_token_counts():
    text = "Apple continues to weaken: institutional confidence (68.5) anchors, DES (44.9) drags."
    client = _FakeClient(text=text)
    out = narratives_llm.generate_llm_narrative(
        ticker="AAPL",
        snapshot_row=_snapshot_row(),
        variable_detail_rows=_variable_detail_rows(),
        insider_data=_insider(),
        holdings_data=_holdings(),
        macro_breadth=_macro(),
        client=client,
    )
    assert out["fallback"] is False
    assert out["narrative"] == text
    assert out["model"] == narratives_llm.DEFAULT_MODEL
    assert out["input_tokens"] == 1247
    assert out["output_tokens"] == 198
    assert out["cached_input_tokens"] == 1100
    assert out["ticker"] == "AAPL"
    assert out["generated_at"].endswith("Z")


def test_cache_control_placed_on_system_blocks():
    client = _FakeClient()
    narratives_llm.generate_llm_narrative(
        ticker="AAPL",
        snapshot_row=_snapshot_row(),
        variable_detail_rows=_variable_detail_rows(),
        macro_breadth=_macro(),
        client=client,
    )
    assert len(client.messages.calls) == 1
    call = client.messages.calls[0]
    system_blocks = call["system"]
    assert isinstance(system_blocks, list)
    # Every system block carries an ephemeral cache_control marker.
    for blk in system_blocks:
        assert blk.get("cache_control") == {"type": "ephemeral"}
    # User message is a plain string (not cached).
    msgs = call["messages"]
    assert msgs[0]["role"] == "user"
    assert isinstance(msgs[0]["content"], str)


def test_use_cache_false_strips_cache_control():
    client = _FakeClient()
    narratives_llm.generate_llm_narrative(
        ticker="AAPL",
        snapshot_row=_snapshot_row(),
        variable_detail_rows=_variable_detail_rows(),
        macro_breadth=_macro(),
        client=client,
        use_cache=False,
    )
    call = client.messages.calls[0]
    for blk in call["system"]:
        assert "cache_control" not in blk


def test_custom_model_propagates_to_api_call():
    client = _FakeClient()
    narratives_llm.generate_llm_narrative(
        ticker="AAPL",
        snapshot_row=_snapshot_row(),
        variable_detail_rows=_variable_detail_rows(),
        client=client,
        model="claude-opus-4-5",
    )
    assert client.messages.calls[0]["model"] == "claude-opus-4-5"


# ---------------------------------------------------------------------------
# Universe batch helper
# ---------------------------------------------------------------------------


def test_universe_helper_returns_one_entry_per_ticker():
    rows = [_snapshot_row(ticker="AAPL"), _snapshot_row(ticker="MSFT"), _snapshot_row(ticker="NVDA")]
    var_detail = {t: _variable_detail_rows(t) for t in ("AAPL", "MSFT", "NVDA")}
    client = _FakeClient(text="Narrative body.")
    out = narratives_llm.generate_universe_narratives(
        snapshot_rows=rows,
        variable_detail_by_ticker=var_detail,
        insider_by_ticker={"AAPL": _insider()},
        holdings_by_ticker={"AAPL": _holdings()},
        macro_breadth=_macro(),
        client=client,
        max_concurrency=2,
    )
    assert set(out.keys()) == {"AAPL", "MSFT", "NVDA"}
    for ticker, narr in out.items():
        assert narr["ticker"] == ticker
        assert narr["fallback"] is False
        assert narr["narrative"] == "Narrative body."


def test_universe_helper_falls_back_when_no_client(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    rows = [_snapshot_row(ticker="AAPL"), _snapshot_row(ticker="MSFT")]
    var_detail = {t: _variable_detail_rows(t) for t in ("AAPL", "MSFT")}
    out = narratives_llm.generate_universe_narratives(
        snapshot_rows=rows,
        variable_detail_by_ticker=var_detail,
    )
    assert set(out.keys()) == {"AAPL", "MSFT"}
    for narr in out.values():
        assert narr["fallback"] is True
        assert narr["fallback_reason"] == "missing_api_key"


def test_universe_helper_handles_empty_input():
    out = narratives_llm.generate_universe_narratives(
        snapshot_rows=[], variable_detail_by_ticker={}
    )
    assert out == {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_classify_insider_regime_bands():
    assert (
        narratives_llm._classify_insider_regime({"conviction_score": -1.0, "cluster_buying": False})
        == "heavy_selling"
    )
    assert (
        narratives_llm._classify_insider_regime({"conviction_score": -0.4, "cluster_buying": False})
        == "net_selling"
    )
    assert (
        narratives_llm._classify_insider_regime({"conviction_score": 0.0, "cluster_buying": False})
        == "balanced"
    )
    assert (
        narratives_llm._classify_insider_regime({"conviction_score": 0.4, "cluster_buying": False})
        == "net_buying"
    )
    assert (
        narratives_llm._classify_insider_regime({"conviction_score": 1.0, "cluster_buying": False})
        == "heavy_buying"
    )
    assert (
        narratives_llm._classify_insider_regime({"conviction_score": 0.0, "cluster_buying": True})
        == "cluster_buying"
    )


def test_binding_and_supporting_picks_extremes():
    subs = {
        "adoption_momentum": 50.0,
        "institutional_confidence": 68.5,
        "financial_evolution": 67.5,
        "thesis_integrity": 62.4,
        "des": 44.9,
    }
    out = narratives_llm._binding_and_supporting(subs)
    assert out["supporting_pillar"] == "Institutional Confidence"
    assert out["supporting_score"] == 68.5
    assert out["binding_pillar"] == "Demand Environment"
    assert out["binding_score"] == 44.9


def test_summarize_insider_filters_to_open_market_top_transactions():
    summary = narratives_llm._summarize_insider(_insider())
    assert summary["available"] is True
    txs = summary["top_open_market_transactions"]
    # Only the Levinson (open market) transaction qualifies; Parekh is 10b5-1.
    assert len(txs) == 1
    assert txs[0]["insider"] == "LEVINSON ARTHUR D"
    assert txs[0]["planned_10b5_1"] is False


def test_summarize_holdings_keeps_top_3():
    summary = narratives_llm._summarize_holdings(_holdings())
    assert summary["available"] is True
    assert summary["conviction_signal"] == "mixed"
    assert [h["manager"] for h in summary["top_3_holders"]] == [
        "BlackRock",
        "State Street",
        "Goldman Sachs",
    ]
