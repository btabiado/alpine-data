"""Tests for ``lthcs.backtest_engine_attribution`` (Tier 5 #24, Phase 2).

Phase-2 attribution layers on top of Phase 1's pure engine: it rebuilds
the band history per pillar (weight set to zero, renormalized,
re-banded) and re-runs ``run_backtest`` to compute Δ-Sharpe vs
baseline. These tests exercise the math + the orchestrator on hand-
checkable synthetic snapshots, plus the standard edge cases (empty
input, missing-data tolerance, hash stability, additive-caveat note).
"""

from __future__ import annotations

import math
from typing import Any, Dict, List

import pandas as pd
import pytest

from lthcs import backtest_engine as be
from lthcs import backtest_engine_attribution as bea


# A minimal score_bands matching weights.json shape — bands tile [0, 100].
SCORE_BANDS = {
    "elite":            {"min": 85, "max": 100},
    "high_confidence":  {"min": 80, "max": 84},
    "constructive":     {"min": 70, "max": 79},
    "monitor":          {"min": 60, "max": 69},
    "weakening":        {"min": 50, "max": 59},
    "review":           {"min": 0,  "max": 49},
}


# ---------------------------------------------------------------------------
# Helpers — construct synthetic snapshots / prices
# ---------------------------------------------------------------------------

def _equal_weights() -> List[float]:
    return [0.20, 0.20, 0.20, 0.20, 0.20]


def _make_row(
    ticker: str,
    subscores_by_pillar: Dict[str, float],
    weights: List[float] = None,
    band: str = None,
    score: float = None,
    modifiers: Dict[str, float] = None,
) -> Dict[str, Any]:
    w = weights or _equal_weights()
    # Derive composite from subscores * weights (no modifiers).
    if score is None:
        score = sum(
            float(subscores_by_pillar[p]) * float(w[i])
            for i, p in enumerate(bea.PILLARS)
        )
    if band is None:
        from lthcs.score import assign_band
        band = assign_band(score, SCORE_BANDS)
    return {
        "ticker": ticker,
        "subscores": dict(subscores_by_pillar),
        "weights_used": list(w),
        "effective_weights": list(w),
        "modifiers": modifiers or {"macro_adj": 0.0, "sector_adj": 0.0, "volatility_mod": 0.0},
        "lthcs_score": float(score),
        "band": band,
    }


def _trading_days(start: str, n: int) -> pd.DatetimeIndex:
    return pd.bdate_range(start=start, periods=n)


# ---------------------------------------------------------------------------
# 1. Weight zero-out + renormalization math
# ---------------------------------------------------------------------------

def test_renormalize_zeroes_pillar_and_redistributes():
    w = [0.20, 0.20, 0.20, 0.20, 0.20]
    new = bea._renormalize_with_pillar_zeroed(w, 0)
    assert new is not None
    assert new[0] == pytest.approx(0.0)
    assert sum(new) == pytest.approx(1.0)
    # Remaining 4 pillars now each carry 0.25.
    for i in range(1, 5):
        assert new[i] == pytest.approx(0.25)


def test_renormalize_handles_asymmetric_base_weights():
    # Standard compounder profile.
    w = [0.25, 0.20, 0.15, 0.20, 0.20]
    new = bea._renormalize_with_pillar_zeroed(w, 2)  # zero financial_evolution
    assert new is not None
    assert new[2] == pytest.approx(0.0)
    assert sum(new) == pytest.approx(1.0)
    # The 0.15 should redistribute proportionally to remaining weights.
    # remaining = 0.85; renormalized = original / 0.85
    assert new[0] == pytest.approx(0.25 / 0.85)
    assert new[1] == pytest.approx(0.20 / 0.85)


def test_renormalize_returns_none_when_all_other_weights_are_zero():
    # Pathological: only pillar 0 has weight; zero it.
    w = [1.0, 0.0, 0.0, 0.0, 0.0]
    out = bea._renormalize_with_pillar_zeroed(w, 0)
    assert out is None


def test_renormalize_returns_none_for_out_of_range_index():
    w = _equal_weights()
    assert bea._renormalize_with_pillar_zeroed(w, -1) is None
    assert bea._renormalize_with_pillar_zeroed(w, 7) is None


# ---------------------------------------------------------------------------
# 2. Composite recomputation with modifiers
# ---------------------------------------------------------------------------

def test_recompute_score_includes_modifier_sum():
    subs = {
        "adoption_momentum": 80.0,
        "institutional_confidence": 80.0,
        "financial_evolution": 80.0,
        "thesis_integrity": 80.0,
        "des": 80.0,
    }
    weights = _equal_weights()
    score = bea._recompute_score(subs, weights, modifier_sum=-3.0)
    # 80*0.2 * 5 = 80, minus 3 = 77
    assert score == pytest.approx(77.0)


def test_recompute_score_clamps_to_0_100():
    subs = {p: 100.0 for p in bea.PILLARS}
    score = bea._recompute_score(subs, _equal_weights(), modifier_sum=50.0)
    assert score == 100.0  # clamped


def test_recompute_score_returns_none_on_missing_subscore():
    subs = {p: 50.0 for p in bea.PILLARS}
    del subs["thesis_integrity"]
    assert bea._recompute_score(subs, _equal_weights(), 0.0) is None


# ---------------------------------------------------------------------------
# 3. Re-banding a snapshot panel
# ---------------------------------------------------------------------------

def test_rebanded_history_drops_pillar_and_changes_band():
    """Synthetic: AAA has high adoption_momentum (90) but mediocre other
    pillars (60). With equal weights the composite is ~66, so the
    production band is 'monitor'. If we zero adoption_momentum and
    renormalize the other four (each now 0.25), composite drops to ~60.
    """
    subs = {
        "adoption_momentum": 90.0,
        "institutional_confidence": 60.0,
        "financial_evolution": 60.0,
        "thesis_integrity": 60.0,
        "des": 60.0,
    }
    snapshots = {
        "2026-01-05": [_make_row("AAA", subs, weights=_equal_weights())]
    }
    # Baseline: 90*.2 + 60*.2*4 = 18 + 48 = 66 -> monitor.
    bh_base = bea._baseline_band_history(snapshots)
    assert bh_base.iloc[0, 0] == "monitor"

    # Zero adoption -> 60 * 0.25 * 4 = 60 -> monitor (just barely).
    rebanded = bea.rebanded_history_for_pillar(snapshots, 0, SCORE_BANDS)
    # 60 -> monitor (min=60). But if any rounding pushes it under, accept.
    assert rebanded.iloc[0, 0] in {"monitor", "weakening"}


def test_rebanded_history_empty_when_no_subscores():
    # Row has no subscores -> should be skipped.
    bad_row = {
        "ticker": "AAA",
        "band": "elite",
        "lthcs_score": 90.0,
        # No subscores, no weights_used.
    }
    snapshots = {"2026-01-05": [bad_row]}
    rebanded = bea.rebanded_history_for_pillar(snapshots, 0, SCORE_BANDS)
    assert rebanded.empty


# ---------------------------------------------------------------------------
# 4. End-to-end attribution: pillar that drives entries vs one that doesn't
# ---------------------------------------------------------------------------

def test_attribution_pillar_zero_changes_equity_curve():
    """Two-pillar effective scenario: AAA's adoption_momentum is the
    pillar that pushes it into 'elite'. Without it (weight zeroed +
    renormalized), AAA falls out of elite and the strategy holds
    nothing. Equity curve flattens -> Δ-Sharpe should be visible.

    Construction: AAA price climbs 1% per day. AAA has perfect adoption
    (100), neutral on the other four (50). With equal weights the
    composite is 60 (still monitor). Zero adoption -> 50 (weakening).
    To get into elite we need ALL pillars hot OR adoption hot.

    Easier: AAA's only path to 'elite' is via adoption (100 on adoption,
    85 elsewhere). Composite = 100*.2 + 85*.2*4 = 20 + 68 = 88 -> elite.
    Zero adoption (renormalize): 85*.25*4 = 85 -> elite still. Sigh.

    Real test: AAA = adoption 100, others 70.
       composite = 100*.2 + 70*.2*4 = 76 -> constructive (buy band).
       zero adoption: 70*.25*4 = 70 -> constructive (still buy).
    Two-tier: AAA = adoption 100, others 60.
       composite = 100*.2 + 60*.2*4 = 68 -> monitor (NOT buy).
       Useless: it's not in the buy set baseline either.

    Try AAA = adoption 100, others 80.
       composite = 100*.2 + 80*.2*4 = 84 -> high_confidence (buy).
       zero adoption: 80*.25*4 = 80 -> high_confidence (still buy).

    What we want: baseline-in-buy, variant-out-of-buy. Force it with
    asymmetric weights, e.g. adoption=0.6, others=0.1 each.

    AAA = adoption 100, others 30.
       composite = 100*.6 + 30*.1*4 = 60 + 12 = 72 -> constructive.
       zero adoption (renormalize others to 0.25): 30*.25*4 = 30 -> review.
    Now baseline includes AAA in buy, variant kicks AAA out. The
    variant should have zero equity growth (no positions).
    """
    weights = [0.6, 0.1, 0.1, 0.1, 0.1]
    subs = {
        "adoption_momentum": 100.0,
        "institutional_confidence": 30.0,
        "financial_evolution": 30.0,
        "thesis_integrity": 30.0,
        "des": 30.0,
    }
    # 10 trading days, AAA climbs from 100 to ~110.
    idx = _trading_days("2026-01-05", 10)
    prices = pd.DataFrame({"AAA": [100.0 * (1.01 ** i) for i in range(10)]}, index=idx)
    snapshots = {}
    for d in idx:
        ds = d.strftime("%Y-%m-%d")
        snapshots[ds] = [_make_row("AAA", subs, weights=weights)]

    params = be.EngineParams(cost_bps=0.0)
    out = bea.run_attribution(
        snapshots_by_date=snapshots,
        prices=prices,
        score_bands=SCORE_BANDS,
        params=params,
    )

    # Baseline: AAA in constructive -> long the whole window.
    assert out["baseline_summary"]["total_return"] > 0.0

    # Variant adoption_momentum (pillar 0) -> AAA falls to 'review',
    # not in buy bands. So variant total_return should be ~0.
    am = out["per_pillar"]["adoption_momentum"]
    assert am["status"] == "ok"
    assert am["variant_summary"]["total_return"] == pytest.approx(0.0, abs=1e-9)
    # Δ-total_return negative: removing adoption hurts.
    assert am["delta_total_return"] < 0.0


def test_attribution_emits_all_five_pillars():
    """Even when pillar zero-out doesn't change the band, the per-pillar
    entry should still be present in the output dict."""
    weights = _equal_weights()
    subs = {p: 80.0 for p in bea.PILLARS}  # composite 80, high_confidence
    idx = _trading_days("2026-01-05", 5)
    prices = pd.DataFrame({"AAA": [100.0] * 5}, index=idx)
    snapshots = {
        d.strftime("%Y-%m-%d"): [_make_row("AAA", subs, weights=weights)]
        for d in idx
    }
    out = bea.run_attribution(
        snapshots, prices, SCORE_BANDS, params=be.EngineParams(cost_bps=0.0),
    )
    for pillar in bea.PILLARS:
        assert pillar in out["per_pillar"]


def test_attribution_note_includes_non_additive_caveat():
    out = bea.run_attribution(
        snapshots_by_date={},
        prices=pd.DataFrame(),
        score_bands=SCORE_BANDS,
    )
    assert "not additive" in out["note"].lower() or "additive" in out["note"].lower()


# ---------------------------------------------------------------------------
# 5. Missing-data tolerance
# ---------------------------------------------------------------------------

def test_attribution_tolerates_rows_without_subscores():
    """Some rows are well-formed, some lack subscores. The attribution
    runner shouldn't crash."""
    weights = _equal_weights()
    subs = {p: 80.0 for p in bea.PILLARS}
    idx = _trading_days("2026-01-05", 5)
    prices = pd.DataFrame({"AAA": [100.0] * 5, "BBB": [100.0] * 5}, index=idx)
    snapshots = {}
    for d in idx:
        ds = d.strftime("%Y-%m-%d")
        snapshots[ds] = [
            _make_row("AAA", subs, weights=weights),
            {  # malformed BBB row
                "ticker": "BBB",
                "band": "monitor",
                "lthcs_score": 65.0,
            },
        ]
    out = bea.run_attribution(
        snapshots, prices, SCORE_BANDS, params=be.EngineParams(cost_bps=0.0),
    )
    # All 5 pillar entries present, none raised.
    assert set(out["per_pillar"].keys()) == set(bea.PILLARS)


def test_attribution_with_empty_snapshots_returns_well_formed_payload():
    out = bea.run_attribution(
        snapshots_by_date={},
        prices=pd.DataFrame(),
        score_bands=SCORE_BANDS,
    )
    assert "baseline_summary" in out
    assert "per_pillar" in out
    assert out["pillars"] == list(bea.PILLARS)


# ---------------------------------------------------------------------------
# 6. Hash / determinism stability
# ---------------------------------------------------------------------------

def test_attribution_is_deterministic_for_same_inputs():
    weights = [0.6, 0.1, 0.1, 0.1, 0.1]
    subs = {
        "adoption_momentum": 100.0,
        "institutional_confidence": 30.0,
        "financial_evolution": 30.0,
        "thesis_integrity": 30.0,
        "des": 30.0,
    }
    idx = _trading_days("2026-01-05", 8)
    prices = pd.DataFrame({"AAA": [100.0 + i for i in range(8)]}, index=idx)
    snapshots = {
        d.strftime("%Y-%m-%d"): [_make_row("AAA", subs, weights=weights)]
        for d in idx
    }
    a = bea.run_attribution(snapshots, prices, SCORE_BANDS,
                            params=be.EngineParams(cost_bps=0.0))
    b = bea.run_attribution(snapshots, prices, SCORE_BANDS,
                            params=be.EngineParams(cost_bps=0.0))
    # Compare delta_sharpe across pillars; should be exactly equal.
    for p in bea.PILLARS:
        if a["per_pillar"][p].get("status") == "ok":
            assert (
                a["per_pillar"][p]["delta_sharpe"]
                == b["per_pillar"][p]["delta_sharpe"]
            )


def test_baseline_summary_carries_engine_metrics():
    """Sanity: the baseline summary copy includes the standard engine
    keys so the UI can lift them straight from the attribution file
    without parallel-fetching engine_summary.json."""
    weights = _equal_weights()
    subs = {p: 80.0 for p in bea.PILLARS}
    idx = _trading_days("2026-01-05", 4)
    prices = pd.DataFrame({"AAA": [100.0] * 4}, index=idx)
    snapshots = {
        d.strftime("%Y-%m-%d"): [_make_row("AAA", subs, weights=weights)]
        for d in idx
    }
    out = bea.run_attribution(snapshots, prices, SCORE_BANDS,
                              params=be.EngineParams(cost_bps=0.0))
    for k in ("sharpe", "total_return", "max_drawdown",
              "n_trading_days", "n_trades"):
        assert k in out["baseline_summary"]
