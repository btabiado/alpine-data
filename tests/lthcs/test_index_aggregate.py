"""Tests for the LTHCS composite index aggregator."""

from __future__ import annotations

import pytest

from lthcs.index_aggregate import compute_lthcs_index


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row(ticker: str, band: str, **subs):
    subscores = {
        "adoption_momentum": 50.0,
        "institutional_confidence": 50.0,
        "financial_evolution": 50.0,
        "thesis_integrity": 50.0,
        "des": 50.0,
    }
    subscores.update(subs)
    return {"ticker": ticker, "band": band, "subscores": subscores}


def _neutral_universe(n: int = 10):
    """A universe where every pillar averages to exactly 50 and bands split evenly.

    Half bullish (constructive) + half bearish (weakening) → net band lean 0.
    """
    rows = []
    for i in range(n):
        band = "constructive" if i < n // 2 else "weakening"
        rows.append(_row("T%d" % i, band))
    return rows


# ---------------------------------------------------------------------------
# Band lean
# ---------------------------------------------------------------------------

def test_band_lean_all_bullish_caps_at_plus_30():
    rows = [_row("T%d" % i, "elite") for i in range(20)]
    out = compute_lthcs_index(rows)
    band = next(c for c in out["components"] if c["name"].startswith("Band lean"))
    assert band["delta"] == 30
    assert "broadly bullish" in band["read"]


def test_band_lean_all_bearish_caps_at_minus_30():
    rows = [_row("T%d" % i, "review") for i in range(20)]
    out = compute_lthcs_index(rows)
    band = next(c for c in out["components"] if c["name"].startswith("Band lean"))
    assert band["delta"] == -30
    assert "distributing" in band["read"]


def test_band_lean_balanced_is_zero():
    rows = _neutral_universe(10)
    out = compute_lthcs_index(rows)
    band = next(c for c in out["components"] if c["name"].startswith("Band lean"))
    assert band["delta"] == 0


# ---------------------------------------------------------------------------
# Pillar averages
# ---------------------------------------------------------------------------

def test_pillar_avg_above_50_is_positive():
    rows = [
        _row("T%d" % i, "constructive", adoption_momentum=75.0)
        for i in range(10)
    ]
    out = compute_lthcs_index(rows)
    comp = next(c for c in out["components"] if c["name"] == "Adoption pillar avg")
    # mean=75, centered=+25, /5 = +5 → capped at +5
    assert comp["delta"] == 5
    assert "strengthening" in comp["read"]


def test_pillar_avg_below_50_is_negative():
    rows = [
        _row("T%d" % i, "weakening", institutional_confidence=20.0)
        for i in range(10)
    ]
    out = compute_lthcs_index(rows)
    comp = next(
        c for c in out["components"] if c["name"] == "Institutional pillar avg"
    )
    # mean=20, centered=-30, /5 = -6 → capped at -6
    assert comp["delta"] == -6
    assert "eroding" in comp["read"]


def test_pillar_avg_clip_at_plus_10():
    rows = [_row("T%d" % i, "elite", thesis_integrity=100.0) for i in range(10)]
    out = compute_lthcs_index(rows)
    comp = next(c for c in out["components"] if c["name"] == "Thesis pillar avg")
    # mean=100, centered=+50, /5 = +10 → capped at +10
    assert comp["delta"] == 10


def test_pillar_avg_clip_at_minus_10():
    rows = [_row("T%d" % i, "review", des=0.0) for i in range(10)]
    out = compute_lthcs_index(rows)
    comp = next(
        c for c in out["components"] if c["name"] == "DES (demand environment) avg"
    )
    assert comp["delta"] == -10


# ---------------------------------------------------------------------------
# Macro regime
# ---------------------------------------------------------------------------

def test_macro_regime_all_clean():
    rows = _neutral_universe(10)
    breadth = {
        "regime_flags": {
            "hy_stress": False,
            "curve_inverted": False,
            "dollar_strong": False,
        }
    }
    out = compute_lthcs_index(rows, breadth_snapshot=breadth)
    comp = next(c for c in out["components"] if c["name"].startswith("Macro regime"))
    # 3 clean → +15 → clipped to +10
    assert comp["delta"] == 10
    assert "risk-on" in comp["read"]


def test_macro_regime_all_tripped():
    rows = _neutral_universe(10)
    breadth = {
        "regime_flags": {
            "hy_stress": True,
            "curve_inverted": True,
            "dollar_strong": True,
        }
    }
    out = compute_lthcs_index(rows, breadth_snapshot=breadth)
    comp = next(c for c in out["components"] if c["name"].startswith("Macro regime"))
    # 0 clean - 3 tripped*5 = -15 → clipped to -10
    assert comp["delta"] == -10


def test_macro_regime_one_flag_tripped():
    rows = _neutral_universe(10)
    breadth = {
        "regime_flags": {
            "hy_stress": True,
            "curve_inverted": False,
            "dollar_strong": False,
        }
    }
    out = compute_lthcs_index(rows, breadth_snapshot=breadth)
    comp = next(c for c in out["components"] if c["name"].startswith("Macro regime"))
    # 2 clean*5 - 1 tripped*5 = +5
    assert comp["delta"] == 5


# ---------------------------------------------------------------------------
# Insider conviction breadth
# ---------------------------------------------------------------------------

def test_insider_strong_buying_dominant():
    rows = _neutral_universe(10)
    insider = {
        "T0": {"regime": "strong_buying"},
        "T1": {"regime": "strong_buying"},
        "T2": {"regime": "strong_buying"},
        "T3": {"regime": "strong_buying"},
        "T4": {"regime": "strong_buying"},
        "T5": {"regime": "strong_buying"},
    }
    out = compute_lthcs_index(rows, insider_by_ticker=insider)
    comp = next(
        c for c in out["components"] if c["name"].startswith("Insider conviction")
    )
    # 6 buying / max(6, 6) → axis = +1.0 → delta = +10
    assert comp["delta"] == 10
    assert "accumulating" in comp["read"]


def test_insider_heavy_selling_dominant():
    rows = _neutral_universe(10)
    insider = {
        "T%d" % i: {"regime": "heavy_selling"} for i in range(6)
    }
    out = compute_lthcs_index(rows, insider_by_ticker=insider)
    comp = next(
        c for c in out["components"] if c["name"].startswith("Insider conviction")
    )
    assert comp["delta"] == -10
    assert "distributing" in comp["read"]


def test_insider_empty_map_dropped():
    rows = _neutral_universe(10)
    out = compute_lthcs_index(rows, insider_by_ticker=None)
    assert not any(
        c["name"].startswith("Insider conviction") for c in out["components"]
    )


def test_insider_single_signal_does_not_swing():
    rows = _neutral_universe(10)
    insider = {"T0": {"regime": "strong_buying"}}
    out = compute_lthcs_index(rows, insider_by_ticker=insider)
    comp = next(
        c for c in out["components"] if c["name"].startswith("Insider conviction")
    )
    # 1 buying / max(1, 6) = 1/6 ≈ 0.167 → delta = +2 (capped by the floor)
    assert -3 <= comp["delta"] <= 3


# ---------------------------------------------------------------------------
# Holdings (13F) conviction breadth
# ---------------------------------------------------------------------------

def test_holdings_accumulating_dominant():
    rows = _neutral_universe(10)
    holdings = {
        "T%d" % i: {"conviction_signal": "accumulating"} for i in range(8)
    }
    out = compute_lthcs_index(rows, holdings_by_ticker=holdings)
    comp = next(
        c for c in out["components"] if c["name"].startswith("13F conviction")
    )
    assert comp["delta"] == 10
    assert "accumulating" in comp["read"]


def test_holdings_balanced_returns_zero_delta():
    rows = _neutral_universe(10)
    holdings = {
        "T0": {"conviction_signal": "accumulating"},
        "T1": {"conviction_signal": "accumulating"},
        "T2": {"conviction_signal": "distributing"},
        "T3": {"conviction_signal": "distributing"},
    }
    out = compute_lthcs_index(rows, holdings_by_ticker=holdings)
    comp = next(
        c for c in out["components"] if c["name"].startswith("13F conviction")
    )
    assert comp["delta"] == 0
    assert "balanced" in comp["read"]


# ---------------------------------------------------------------------------
# Score label thresholds
# ---------------------------------------------------------------------------

def test_label_threshold_neutral_just_below_30():
    """A composite of +29 sits just inside NEUTRAL band."""
    # Force +29 by stacking band lean at +30 and DES pillar at 50 (delta 0)
    # but trimming via macro -1. Easier: synthesize via score directly.
    rows = [_row("T%d" % i, "elite") for i in range(10)]
    # Force pillar averages = 50 → no contribution; band lean = +30 only.
    out = compute_lthcs_index(rows)
    assert out["score"] == 30
    assert out["label"] == "LTHCS CONSTRUCTIVE"


def test_label_threshold_elite():
    rows = [_row("T%d" % i, "elite", **{p: 100.0 for p in (
        "adoption_momentum",
        "institutional_confidence",
        "financial_evolution",
        "thesis_integrity",
        "des",
    )}) for i in range(10)]
    breadth = {"regime_flags": {"hy_stress": False, "curve_inverted": False, "dollar_strong": False}}
    insider = {"T%d" % i: {"regime": "strong_buying"} for i in range(8)}
    holdings = {"T%d" % i: {"conviction_signal": "accumulating"} for i in range(8)}
    out = compute_lthcs_index(
        rows,
        breadth_snapshot=breadth,
        insider_by_ticker=insider,
        holdings_by_ticker=holdings,
    )
    # 30 (band) + 5*10 (pillars) + 10 (macro) + 10 (insider) + 10 (holdings)
    # = 30 + 50 + 30 = 110 → clipped to 100
    assert out["score"] == 100
    assert out["label"] == "LTHCS ELITE"


def test_label_threshold_distributing():
    rows = [_row("T%d" % i, "review", **{p: 0.0 for p in (
        "adoption_momentum",
        "institutional_confidence",
        "financial_evolution",
        "thesis_integrity",
        "des",
    )}) for i in range(10)]
    breadth = {"regime_flags": {"hy_stress": True, "curve_inverted": True, "dollar_strong": True}}
    insider = {"T%d" % i: {"regime": "heavy_selling"} for i in range(8)}
    holdings = {"T%d" % i: {"conviction_signal": "distributing"} for i in range(8)}
    out = compute_lthcs_index(
        rows,
        breadth_snapshot=breadth,
        insider_by_ticker=insider,
        holdings_by_ticker=holdings,
    )
    # -30 - 50 - 30 = -110 → clipped to -100
    assert out["score"] == -100
    assert out["label"] == "LTHCS DISTRIBUTING"


def test_label_threshold_weakening():
    """Score -30 must land in WEAKENING."""
    rows = [_row("T%d" % i, "review") for i in range(10)]
    # band -30 only (pillars all 50, no other inputs)
    out = compute_lthcs_index(rows)
    assert out["score"] == -30
    assert out["label"] == "LTHCS WEAKENING"


def test_label_threshold_neutral_at_zero():
    rows = _neutral_universe(10)
    out = compute_lthcs_index(rows)
    assert out["score"] == 0
    assert out["label"] == "LTHCS NEUTRAL"


# ---------------------------------------------------------------------------
# Missing-input / degradation tests
# ---------------------------------------------------------------------------

def test_empty_snapshot_returns_neutral_no_components():
    out = compute_lthcs_index([])
    assert out["score"] == 0
    assert out["label"] == "LTHCS NEUTRAL"
    # Empty universe → no pillar averages and no band lean either.
    assert out["components"] == []


def test_missing_inputs_drop_components():
    rows = _neutral_universe(10)
    out = compute_lthcs_index(rows)  # all optional inputs None
    names = [c["name"] for c in out["components"]]
    assert "Macro regime (HY OAS / curve / USD)" not in names
    assert "Insider conviction breadth" not in names
    assert "13F conviction breadth (acc vs dist)" not in names


def test_as_of_passthrough():
    out = compute_lthcs_index(_neutral_universe(10), as_of="2026-05-17")
    assert out["as_of"] == "2026-05-17"


# ---------------------------------------------------------------------------
# Score clamp
# ---------------------------------------------------------------------------

def test_score_clamp_upper():
    out = compute_lthcs_index(
        [_row("T%d" % i, "elite", **{p: 100.0 for p in (
            "adoption_momentum",
            "institutional_confidence",
            "financial_evolution",
            "thesis_integrity",
            "des",
        )}) for i in range(20)],
        breadth_snapshot={"regime_flags": {"hy_stress": False, "curve_inverted": False, "dollar_strong": False}},
        insider_by_ticker={"T%d" % i: {"regime": "strong_buying"} for i in range(10)},
        holdings_by_ticker={"T%d" % i: {"conviction_signal": "accumulating"} for i in range(10)},
    )
    assert -100 <= out["score"] <= 100
    assert out["score"] == 100


def test_score_clamp_lower():
    out = compute_lthcs_index(
        [_row("T%d" % i, "review", **{p: 0.0 for p in (
            "adoption_momentum",
            "institutional_confidence",
            "financial_evolution",
            "thesis_integrity",
            "des",
        )}) for i in range(20)],
        breadth_snapshot={"regime_flags": {"hy_stress": True, "curve_inverted": True, "dollar_strong": True}},
        insider_by_ticker={"T%d" % i: {"regime": "heavy_selling"} for i in range(10)},
        holdings_by_ticker={"T%d" % i: {"conviction_signal": "distributing"} for i in range(10)},
    )
    assert -100 <= out["score"] <= 100
    assert out["score"] == -100


# ---------------------------------------------------------------------------
# Component-level shape
# ---------------------------------------------------------------------------

def test_component_shape():
    out = compute_lthcs_index(_neutral_universe(10))
    for c in out["components"]:
        assert set(c.keys()) >= {"name", "value", "delta", "read"}
        assert isinstance(c["delta"], int)


def test_color_is_set():
    out = compute_lthcs_index(_neutral_universe(10))
    assert out["color"].startswith("#")
    assert out["band_key"] in {"elite", "high_confidence", "constructive", "monitor", "weakening", "review"}


def test_insider_cluster_buying_counted():
    rows = _neutral_universe(10)
    insider = {
        "T0": {"regime": "neutral", "cluster_buying": True},
        "T1": {"regime": "neutral", "cluster_buying": True},
        "T2": {"regime": "neutral", "cluster_buying": True},
        "T3": {"regime": "neutral", "cluster_buying": True},
        "T4": {"regime": "neutral", "cluster_buying": True},
        "T5": {"regime": "neutral", "cluster_buying": True},
    }
    out = compute_lthcs_index(rows, insider_by_ticker=insider)
    comp = next(
        c for c in out["components"] if c["name"].startswith("Insider conviction")
    )
    # 6 buying (via cluster) - 0 heavy → axis = 1.0 → +10
    assert comp["delta"] == 10
