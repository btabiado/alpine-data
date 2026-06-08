"""Hermetic tests for fetch_stock_money_flow.py (no network).

Covers the pure scoring/labelling/scope helpers. The MFI/CMF math itself is
tested in test_money_flow.py; here we verify the blend, band labels, contract
shape, index-scope filtering, and the "unscoreable -> None" path.
"""
from __future__ import annotations

import fetch_stock_money_flow as sf


def _bars(rows):
    return [
        {"date": f"2026-01-{i + 1:02d}", "open": o, "high": h, "low": l, "close": c, "volume": v}
        for i, (o, h, l, c, v) in enumerate(rows)
    ]


def test_band_label_boundaries():
    assert sf._band_label(-60) == "Heavy Outflow"
    assert sf._band_label(-59.9) == "Outflow"
    assert sf._band_label(-30) == "Outflow"
    assert sf._band_label(-29.9) == "Neutral"
    assert sf._band_label(0) == "Neutral"
    assert sf._band_label(29.9) == "Neutral"
    assert sf._band_label(30) == "Inflow"
    assert sf._band_label(59.9) == "Inflow"
    assert sf._band_label(60) == "Heavy Inflow"


def test_in_scope_and_scope_indices():
    dow = {"ticker": "JPM", "index_membership": ["DJIA", "S&P 500", "S&P 100"]}
    out = {"ticker": "XYZ", "index_membership": ["Russell 2000"]}
    assert sf._in_scope(dow) is True
    assert sf._in_scope(out) is False
    idx = sf._scope_indices(dow)
    assert "DJIA" in idx and "S&P 500" in idx
    # S&P 100 is carried through (per the contract) even though it's not a display bucket
    assert "S&P 100" in idx


def test_score_stock_uptrend_positive():
    rec = {"ticker": "UP", "name": "Up Co", "index_membership": ["NASDAQ-100", "S&P 500"], "sector": "Tech"}
    # strictly rising typical price + high closes => strong accumulation
    rows = [(i, i + 1, i - 1, i + 0.9, 1000) for i in range(1, 30)]
    out = sf._score_stock(rec, _bars(rows))
    assert out is not None
    assert out["symbol"] == "UP"
    assert -100.0 <= out["score"] <= 100.0
    assert out["score"] > 0
    assert out["label"] in {"Inflow", "Heavy Inflow", "Neutral"}
    # contract fields present
    assert set(out.keys()) == {"symbol", "name", "score", "label", "mfi", "cmf", "indices", "sector"}
    assert out["indices"] == ["NASDAQ-100", "S&P 500"]


def test_score_stock_downtrend_negative():
    rec = {"ticker": "DN", "name": "Down Co", "index_membership": ["DJIA"], "sector": "Energy"}
    rows = [(i, i + 1, i - 1, i - 0.9, 1000) for i in range(40, 1, -1)]  # strictly falling
    out = sf._score_stock(rec, _bars(rows))
    assert out is not None and out["score"] < 0


def test_score_stock_blend_formula():
    # When both MFI and CMF exist, score == clip(round(0.6*((mfi-50)*2)+0.4*(cmf*200),1))
    rec = {"ticker": "BL", "name": "Blend", "index_membership": ["S&P 500"], "sector": "X"}
    rows = [(10, 12, 8, 11, 1000) for _ in range(30)]
    out = sf._score_stock(rec, _bars(rows))
    if out and out["mfi"] is not None and out["cmf"] is not None:
        expected = round(0.6 * ((out["mfi"] - 50.0) * 2.0) + 0.4 * (out["cmf"] * 200.0), 1)
        expected = max(-100.0, min(100.0, expected))
        # allow tiny rounding drift from the 2dp/4dp stored mfi/cmf
        assert abs(out["score"] - expected) <= 0.5


def test_score_stock_insufficient_is_none():
    rec = {"ticker": "NA", "name": "NoData", "index_membership": ["DJIA"], "sector": "X"}
    assert sf._score_stock(rec, _bars([(10, 11, 9, 10, 100)])) is None
    assert sf._score_stock(rec, []) is None
