"""Tests for LTC-specific behavior — Deribit doesn't quote LTC DVOL,
signal still computes from the remaining components."""
from __future__ import annotations

import pandas as pd

import signals


def _build_ltc_payload(n_days: int = 260, with_dvol: bool = False):
    """Build a synthetic payload with ltc market data and optional DVOL."""
    dates = pd.date_range("2023-01-01", periods=n_days, freq="D")
    # Slight oscillation around an upward trend so RSI is well-defined
    # (a purely monotonic series gives 0 down-moves, leaving RSI undefined).
    import math
    prices = [80.0 + i * 0.1 + math.sin(i / 5.0) * 2.0 for i in range(n_days)]

    price_rows = [
        {"date": d.strftime("%Y-%m-%d"), "value": float(p)}
        for d, p in zip(dates, prices)
    ]
    funding_rows = [{"date": d.strftime("%Y-%m-%d"), "rate": 0.0} for d in dates]
    fng_rows = [
        {"date": d.strftime("%Y-%m-%d"), "value": 50, "label": "Neutral"} for d in dates
    ]
    dvol_rows = (
        [{"date": d.strftime("%Y-%m-%d"), "dvol": 50.0} for d in dates] if with_dvol else []
    )

    return {
        "market": {
            "ltc": {
                "price": price_rows,
                "funding": funding_rows,
                "dvol": dvol_rows,
            },
            "btc": {"price": price_rows, "funding": funding_rows, "dvol": dvol_rows},
            "eth": {"price": price_rows, "funding": funding_rows, "dvol": dvol_rows},
            "fear_greed": fng_rows,
        },
        # No 'ltc' key at top level — LTC has no ETF flows
        "btc": {"daily": []},
        "eth": {"daily": []},
    }


def test_signals_compute_ltc_with_no_dvol():
    """LTC signal should compute without a DVOL component when dvol is empty
    (Deribit doesn't quote LTC vol)."""
    payload = _build_ltc_payload(n_days=260, with_dvol=False)
    out = signals.compute_signal("ltc", payload)
    assert out is not None
    assert isinstance(out["score"], int)
    assert -100 <= out["score"] <= 100
    assert out["label"] in {"STRONG BUY", "BUY", "HOLD", "SELL", "STRONG SELL"}
    # Components must not include any DVOL-related entry — Deribit doesn't quote LTC.
    component_names = " ".join(c["name"].lower() for c in out["components"])
    assert "dvol" not in component_names
    # The core technical components (SMA50/SMA200, RSI, MACD) must all be present
    # given a 260-day uptrending price series.
    assert any("sma50" in c["name"].lower() for c in out["components"])
    assert any("sma200" in c["name"].lower() for c in out["components"])
    assert any("rsi" in c["name"].lower() for c in out["components"])
    assert any("macd" in c["name"].lower() for c in out["components"])
    # History should still be present
    assert isinstance(out["history"], list)
    assert len(out["history"]) > 0


def test_signals_compute_all_returns_ltc_key():
    """compute_all(payload) must return a dict that includes the 'ltc' key
    alongside btc/eth/link."""
    payload = _build_ltc_payload(n_days=260, with_dvol=False)
    out = signals.compute_all(payload)
    assert isinstance(out, dict)
    assert "ltc" in out
    # The ltc entry should be a non-None signal dict given valid synthetic data
    assert out["ltc"] is not None
    assert "score" in out["ltc"]
    # Existing assets still present
    assert "btc" in out
    assert "eth" in out
    assert "link" in out
