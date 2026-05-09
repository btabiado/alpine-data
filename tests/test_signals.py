"""Tests for signals.py — compute_signal scoring + bounds."""
from __future__ import annotations

import math

import pandas as pd
import pytest

import signals


def _build_payload(
    n_days: int = 260,
    asset: str = "btc",
    price_pattern: str = "rising",
    funding_rate: float = 0.0,
    fng_value: int = 50,
    etf_flow: float = 0.0,
):
    """Build a synthetic payload that signals.compute_signal can consume."""
    dates = pd.date_range("2023-01-01", periods=n_days, freq="D")

    if price_pattern == "rising":
        # Strong steady uptrend so price > SMA50 and > SMA200; MACD positive
        prices = [10000 + i * 50 for i in range(n_days)]
    elif price_pattern == "falling":
        prices = [60000 - i * 50 for i in range(n_days)]
    elif price_pattern == "flat":
        prices = [30000.0] * n_days
    else:
        raise ValueError(price_pattern)

    price_rows = [{"date": d.strftime("%Y-%m-%d"), "value": float(p)}
                  for d, p in zip(dates, prices)]
    funding_rows = [{"date": d.strftime("%Y-%m-%d"), "rate": funding_rate} for d in dates]
    dvol_rows = [{"date": d.strftime("%Y-%m-%d"), "dvol": 50.0} for d in dates]
    fng_rows = [{"date": d.strftime("%Y-%m-%d"), "value": fng_value, "label": "Neutral"}
                for d in dates]
    etf_daily = [{"date": d.strftime("%Y-%m-%d"), "flow": etf_flow, "cumulative": etf_flow * (i + 1)}
                 for i, d in enumerate(dates)]

    return {
        "market": {
            asset: {
                "price": price_rows,
                "funding": funding_rows,
                "dvol": dvol_rows,
            },
            "fear_greed": fng_rows,
        },
        asset: {"daily": etf_daily},
    }


def test_compute_signal_returns_required_keys():
    payload = _build_payload(n_days=260)
    out = signals.compute_signal("btc", payload)
    assert out is not None
    for k in ("score", "label", "components", "as_of", "price", "history", "disclaimer"):
        assert k in out
    assert isinstance(out["score"], int)
    assert isinstance(out["label"], str)
    assert isinstance(out["components"], list)
    assert len(out["components"]) > 0
    assert isinstance(out["history"], list)
    # Each component has required keys
    for c in out["components"]:
        assert {"name", "value", "contribution", "explanation"} <= set(c.keys())


def test_compute_signal_returns_none_when_too_little_data():
    # Less than 30 days → returns None
    payload = _build_payload(n_days=20)
    assert signals.compute_signal("btc", payload) is None


def test_compute_signal_returns_none_when_empty_payload():
    assert signals.compute_signal("btc", {}) is None
    assert signals.compute_signal("btc", {"market": {}}) is None
    assert signals.compute_signal("btc", {"market": {"btc": {}}}) is None


def test_compute_signal_score_bounded():
    # Try several patterns and ensure score stays within ±100
    for pattern in ("rising", "falling", "flat"):
        for funding in (-0.001, 0.0, 0.001):
            for fng in (10, 50, 90):
                payload = _build_payload(
                    n_days=260,
                    price_pattern=pattern,
                    funding_rate=funding,
                    fng_value=fng,
                    etf_flow=10.0,
                )
                out = signals.compute_signal("btc", payload)
                assert out is not None
                assert -100 <= out["score"] <= 100, (
                    f"score {out['score']} out of bounds for pattern={pattern}"
                )


def test_compute_signal_history_scores_all_bounded():
    payload = _build_payload(n_days=260, price_pattern="rising")
    out = signals.compute_signal("btc", payload)
    assert out is not None
    for h in out["history"]:
        assert -100 <= h["score"] <= 100
        assert "date" in h and "price" in h


def test_label_classification_at_boundaries():
    # _label is the canonical mapping
    assert signals._label(50) == "STRONG BUY"
    assert signals._label(75) == "STRONG BUY"
    assert signals._label(20) == "BUY"
    assert signals._label(49) == "BUY"
    assert signals._label(0) == "HOLD"
    assert signals._label(19) == "HOLD"
    assert signals._label(-19) == "HOLD"
    assert signals._label(-20) == "SELL"
    assert signals._label(-49) == "SELL"
    assert signals._label(-50) == "STRONG SELL"
    assert signals._label(-100) == "STRONG SELL"


def test_compute_all_returns_btc_and_eth_keys():
    payload = _build_payload(n_days=260, asset="btc")
    out = signals.compute_all(payload)
    assert "btc" in out and "eth" in out
    assert out["btc"] is not None
    # eth has no data → None
    assert out["eth"] is None


def test_strong_uptrend_produces_positive_score():
    """Steady uptrend, neutral funding/fng/etf — should be positive (SMA + MACD bullish)."""
    payload = _build_payload(
        n_days=260, price_pattern="rising",
        funding_rate=0.0, fng_value=50, etf_flow=0.0,
    )
    out = signals.compute_signal("btc", payload)
    assert out is not None
    assert out["score"] > 0
    assert out["label"] in ("BUY", "STRONG BUY", "HOLD")


def test_strong_downtrend_produces_negative_score():
    payload = _build_payload(
        n_days=260, price_pattern="falling",
        funding_rate=0.0, fng_value=50, etf_flow=0.0,
    )
    out = signals.compute_signal("btc", payload)
    assert out is not None
    assert out["score"] < 0
    assert out["label"] in ("SELL", "STRONG SELL", "HOLD")
