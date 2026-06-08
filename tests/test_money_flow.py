"""Tests for the Money Flow Index (money_flow.py).

Covers the buy/sell math (MFI/CMF/OBV), the composite assembly, and the
SPEC guardrails: MMF enters INVERTED, missing legs degrade to neutral
(never crash), and per-index legs never read the market-wide ICI/MMF legs
(no double-counting).
"""
from __future__ import annotations

import money_flow as mf


def _bars(rows):
    """rows: list of (o,h,l,c,v) -> list of bar dicts."""
    return [
        {"date": f"2026-01-{i+1:02d}", "open": o, "high": h, "low": l, "close": c, "volume": v}
        for i, (o, h, l, c, v) in enumerate(rows)
    ]


# --------------------------------------------------------------------------- MFI
def test_mfi_all_up_is_100():
    bars = _bars([(i, i + 1, i - 1, i, 1000) for i in range(1, 20)])  # typical price strictly rising
    assert mf.mfi(bars, 14) == 100.0


def test_mfi_all_down_is_0():
    bars = _bars([(i, i + 1, i - 1, i, 1000) for i in range(20, 1, -1)])  # strictly falling
    assert mf.mfi(bars, 14) == 0.0


def test_mfi_insufficient_data_is_none():
    assert mf.mfi(_bars([(10, 11, 9, 10, 100)]), 14) is None
    assert mf.mfi([], 14) is None


def test_mfi_in_bounds():
    import random
    rng = random.Random(7)
    bars = _bars([(c := rng.uniform(90, 110), c + 1, c - 1, c, rng.randint(1, 9) * 1000) for _ in range(40)])
    v = mf.mfi(bars, 14)
    assert v is None or 0.0 <= v <= 100.0


# --------------------------------------------------------------------------- CMF
def test_cmf_flat_range_guarded():
    # high == low on every bar -> the (c-l)-(h-c) multiplier is guarded to 0,
    # so CMF is 0.0 (not a divide-by-zero crash).
    bars = _bars([(10, 10, 10, 10, 500) for _ in range(25)])
    assert mf.cmf(bars, 20) == 0.0


def test_cmf_in_range():
    bars = _bars([(10, 12, 8, 11, 1000) for _ in range(25)])
    v = mf.cmf(bars, 20)
    assert v is None or -1.0 <= v <= 1.0


# --------------------------------------------------------------------------- OBV
def test_obv_exact():
    bars = _bars([(10, 10, 10, 10, 5), (12, 12, 12, 12, 3), (11, 11, 11, 11, 4), (11, 11, 11, 11, 9)])
    assert mf.obv(bars) == [0.0, 3.0, -1.0, -1.0]


def test_obv_empty():
    assert mf.obv([]) == []


# ------------------------------------------------------------ composite assembly
def _leg(etf_flow, etf_hist, mfi_val, mfi_hist, cmf_val=0.1, dv=1e9):
    return {
        "etf_flow": etf_flow, "etf_flow_hist": etf_hist,
        "mfi": mfi_val, "mfi_hist": mfi_hist,
        "cmf": cmf_val, "dollar_volume": dv,
    }


def test_empty_payload_is_neutral_not_crash():
    out = mf.build_money_flow_index(None)
    assert out["headline"]["label"] == "Neutral"
    assert out["headline"]["score"] == 0 or abs(out["headline"]["score"]) < 1e-9
    # still emits the 3 per-index slots
    assert len(out["per_index"]) == 3
    assert {p["index"] for p in out["per_index"]} == {"Dow", "S&P 500", "Nasdaq"}


def test_missing_leg_degrades():
    market = {
        "SPY": _leg(5e8, [1e8, 2e8, 3e8], 60.0, [50.0, 55.0, 60.0]),
        "DIA": _leg(2e8, [1e8, 1.5e8, 2e8], 65.0, [50.0, 55.0, 65.0]),
        # QQQ intentionally absent
    }
    out = mf.build_money_flow_index({"market": market})
    qqq = next(p for p in out["per_index"] if p["etf"] == "QQQ")
    assert qqq["mfi"] is None  # absent leg -> neutral
    # headline still computes from the present legs, no crash
    assert isinstance(out["headline"]["score"], (int, float))


def test_mmf_enters_inverted():
    # A money-market WoW change far ABOVE its trailing history (cash building up)
    # must push the money-market component NEGATIVE (risk-off).
    market = {
        "mmf_wow_change": 200.0,
        "mmf_wow_change_hist": [0.0, 1.0, -1.0, 2.0, 0.5, -0.5, 1.0, 0.0],
        "ici_equity_flow": None, "ici_equity_flow_hist": [],
    }
    out = mf.build_money_flow_index({"market": market})
    mmf_comp = next((c for c in out["headline"]["components"] if "Money-market" in c["name"]), None)
    assert mmf_comp is not None
    assert mmf_comp["contribution"] < 0, "rising MMF cash must pull the gauge toward outflow"


def test_ici_inflow_positive():
    market = {
        "ici_equity_flow": 50.0,
        "ici_equity_flow_hist": [-10.0, -8.0, -12.0, -9.0, -11.0, -10.0, -8.0, -9.0],
        "mmf_wow_change": None, "mmf_wow_change_hist": [],
    }
    out = mf.build_money_flow_index({"market": market})
    ici = next((c for c in out["headline"]["components"] if "mutual-fund" in c["name"].lower()), None)
    assert ici is not None and ici["contribution"] > 0


def test_no_double_counting_scope():
    # Per-index sub-scores must depend ONLY on per-index legs, not on the
    # market-wide ICI/MMF legs. Flipping the market-wide legs must not move
    # any per-index score.
    base = {
        "SPY": _leg(5e8, [1e8, 2e8, 3e8], 60.0, [50.0, 55.0, 60.0]),
        "QQQ": _leg(-3e8, [3e8, 1e8, -3e8], 40.0, [60.0, 50.0, 40.0]),
        "DIA": _leg(2e8, [1e8, 1.5e8, 2e8], 65.0, [50.0, 55.0, 65.0]),
    }
    out1 = mf.build_money_flow_index({"market": dict(base)})
    out2 = mf.build_money_flow_index({"market": {**base, "mmf_wow_change": 999.0,
                                                 "mmf_wow_change_hist": [0, 1, 2, 3, 4, 5],
                                                 "ici_equity_flow": 999.0,
                                                 "ici_equity_flow_hist": [0, 1, 2, 3, 4, 5]}})
    s1 = {p["etf"]: p["score"] for p in out1["per_index"]}
    s2 = {p["etf"]: p["score"] for p in out2["per_index"]}
    assert s1 == s2, "per-index sub-scores must not be affected by market-wide legs"


def test_headline_shape_and_bounds():
    market = {
        "SPY": _leg(5e8, [1e8, 2e8, 3e8], 60.0, [50.0, 55.0, 60.0]),
        "QQQ": _leg(-3e8, [3e8, 1e8, -3e8], 40.0, [60.0, 50.0, 40.0]),
        "DIA": _leg(2e8, [1e8, 1.5e8, 2e8], 65.0, [50.0, 55.0, 65.0]),
        "ici_equity_flow": 10.0, "ici_equity_flow_hist": [-5, -6, -4, -5, -6],
        "mmf_wow_change": -20.0, "mmf_wow_change_hist": [0, 1, 2, 3, 4],
        "as_of": "2026-06-08",
    }
    out = mf.build_money_flow_index({"market": market})
    h = out["headline"]
    assert -100.0 <= h["score"] <= 100.0
    assert h["label"] in {"Heavy Outflow", "Outflow", "Neutral", "Inflow", "Heavy Inflow"}
    assert out["as_of"] == "2026-06-08"
    for p in out["per_index"]:
        assert -100.0 <= p["score"] <= 100.0
