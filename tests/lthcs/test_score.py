"""Tests for the LTHCS final-score combiner (``lthcs.score``)."""

from __future__ import annotations

import json
import os
from typing import Any, Dict

import pytest

from lthcs.score import (
    PILLAR_ORDER,
    assign_band,
    compute_drift,
    compute_lthcs_score,
    compute_macro_adjustment,
    compute_volatility_modifier,
    get_maturity_weights,
)


# --- Fixtures ---------------------------------------------------------------

REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
)
WEIGHTS_PATH = os.path.join(REPO_ROOT, "data", "lthcs", "weights.json")


@pytest.fixture(scope="module")
def weights_config() -> Dict[str, Any]:
    with open(WEIGHTS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def neutral_subscores() -> Dict[str, float]:
    return {name: 50.0 for name in PILLAR_ORDER}


# --- get_maturity_weights --------------------------------------------------

class TestGetMaturityWeights:
    def test_standard_compounder(self, weights_config):
        assert get_maturity_weights("standard_compounder", weights_config) == [
            0.25, 0.20, 0.15, 0.20, 0.20,
        ]

    def test_pre_profit_growth(self, weights_config):
        assert get_maturity_weights("pre_profit_growth", weights_config) == [
            0.30, 0.20, 0.15, 0.20, 0.15,
        ]

    def test_recovery_stabilization(self, weights_config):
        assert get_maturity_weights("recovery_stabilization", weights_config) == [
            0.15, 0.15, 0.35, 0.20, 0.15,
        ]

    def test_unknown_raises(self, weights_config):
        with pytest.raises(ValueError):
            get_maturity_weights("not_a_real_stage", weights_config)

    def test_all_profiles_sum_to_one(self, weights_config):
        for stage, vec in weights_config["profiles"].items():
            total = sum(vec)
            assert abs(total - 1.0) < 1e-9, "profile %s sums to %r" % (stage, total)


# --- compute_macro_adjustment ----------------------------------------------

class TestComputeMacroAdjustment:
    def test_above_25bp_negative(self):
        assert compute_macro_adjustment(26.0) == -2.0

    def test_below_minus_25bp_positive(self):
        assert compute_macro_adjustment(-26.0) == 2.0

    def test_at_24bp_neutral(self):
        assert compute_macro_adjustment(24.0) == 0.0

    def test_at_minus_24bp_neutral(self):
        assert compute_macro_adjustment(-24.0) == 0.0

    def test_none_neutral(self):
        assert compute_macro_adjustment(None) == 0.0

    def test_exactly_25_strict(self):
        # Strict ">" means exactly 25 does NOT trigger.
        assert compute_macro_adjustment(25.0) == 0.0

    def test_exactly_minus_25_strict(self):
        assert compute_macro_adjustment(-25.0) == 0.0

    def test_nan_neutral(self):
        assert compute_macro_adjustment(float("nan")) == 0.0


# --- compute_volatility_modifier -------------------------------------------

class TestComputeVolatilityModifier:
    def test_above_90th_percentile_returns_negative(self):
        # universe 1..10; p90 = 9.1. ticker at 10 -> -3.0.
        universe = [float(x) for x in range(1, 11)]
        assert compute_volatility_modifier(10.0, universe) == -3.0

    def test_at_median_returns_zero(self):
        universe = [float(x) for x in range(1, 11)]
        assert compute_volatility_modifier(5.5, universe) == 0.0

    def test_none_ticker_returns_zero(self):
        universe = [float(x) for x in range(1, 11)]
        assert compute_volatility_modifier(None, universe) == 0.0

    def test_empty_universe_returns_zero(self):
        assert compute_volatility_modifier(0.5, []) == 0.0

    def test_at_exact_p90_strict(self):
        # universe p90 = 9.1; ticker exactly 9.1 -> 0.0 (strict >).
        universe = [float(x) for x in range(1, 11)]
        assert compute_volatility_modifier(9.1, universe) == 0.0


# --- assign_band -----------------------------------------------------------

class TestAssignBand:
    def test_92_is_elite(self, weights_config):
        assert assign_band(92.0, weights_config["score_bands"]) == "elite"

    def test_85_is_high_confidence(self, weights_config):
        assert assign_band(85.0, weights_config["score_bands"]) == "high_confidence"

    def test_49_is_review(self, weights_config):
        assert assign_band(49.0, weights_config["score_bands"]) == "review"

    def test_50_is_weakening_boundary(self, weights_config):
        assert assign_band(50.0, weights_config["score_bands"]) == "weakening"

    def test_100_is_elite(self, weights_config):
        assert assign_band(100.0, weights_config["score_bands"]) == "elite"

    def test_0_is_review(self, weights_config):
        assert assign_band(0.0, weights_config["score_bands"]) == "review"


# --- compute_drift ---------------------------------------------------------

class TestComputeDrift:
    def test_basic(self):
        out = compute_drift(80.0, {"1d": 78.0, "7d": 75.0, "30d": 70.0, "90d": 60.0})
        assert out["drift_1d"] == 2.0
        assert out["drift_7d"] == 5.0
        assert out["drift_30d"] == 10.0
        assert out["drift_90d"] == 20.0

    def test_none_prior_yields_zero(self):
        out = compute_drift(80.0, {"1d": 78.0, "30d": None})
        assert out["drift_1d"] == 2.0
        assert out["drift_30d"] == 0.0
        # Missing windows also default to 0.0.
        assert out["drift_7d"] == 0.0
        assert out["drift_90d"] == 0.0

    def test_empty_priors(self):
        out = compute_drift(80.0, {})
        assert out == {"drift_1d": 0.0, "drift_7d": 0.0, "drift_30d": 0.0, "drift_90d": 0.0}

    def test_negative_drift(self):
        out = compute_drift(70.0, {"7d": 75.0})
        assert out["drift_7d"] == -5.0


# --- compute_lthcs_score ---------------------------------------------------

class TestComputeLthcsScore:
    def test_all_neutral(self, weights_config, neutral_subscores):
        result = compute_lthcs_score(
            ticker="TEST",
            sector="Technology",
            maturity_stage="standard_compounder",
            pillar_subscores=neutral_subscores,
            weights_config=weights_config,
        )
        assert result["lthcs_score"] == 50.0
        assert result["band"] == "weakening"
        assert result["modifiers"] == {
            "macro_adj": 0.0,
            "sector_adj": 0.0,
            "volatility_mod": 0.0,
        }
        assert result["confidence_level"] == "high"
        assert result["data_quality_flags"] == []

    def test_aapl_like(self, weights_config):
        # 48.3 / 68.1 / 66.4 / 62.4 / 39.1 against [0.25,0.20,0.15,0.20,0.20]
        # Manual: 12.075 + 13.62 + 9.96 + 12.48 + 7.82 = 55.955 -> rounds to 56.0
        subs = {
            "adoption_momentum": 48.3,
            "institutional_confidence": 68.1,
            "financial_evolution": 66.4,
            "thesis_integrity": 62.4,
            "des": 39.1,
        }
        result = compute_lthcs_score(
            ticker="AAPL",
            sector="Technology",
            maturity_stage="standard_compounder",
            pillar_subscores=subs,
            weights_config=weights_config,
        )
        assert result["lthcs_score"] == 56.0
        # Should land in weakening (50-59).
        assert result["band"] == "weakening"
        assert result["weights_used"] == [0.25, 0.20, 0.15, 0.20, 0.20]
        # weighted_components in PILLAR_ORDER.
        expected = [
            0.25 * 48.3, 0.20 * 68.1, 0.15 * 66.4, 0.20 * 62.4, 0.20 * 39.1,
        ]
        for got, exp in zip(result["weighted_components"], expected):
            assert abs(got - exp) < 1e-9

    def test_lcid_like(self, weights_config):
        # 80.0 / 0.0 / 55.0 / 50.0 / 53.6 against [0.30,0.20,0.15,0.20,0.15]
        # Manual: 24.0 + 0 + 8.25 + 10.0 + 8.04 = 50.29 -> rounds to 50.3
        subs = {
            "adoption_momentum": 80.0,
            "institutional_confidence": 0.0,
            "financial_evolution": 55.0,
            "thesis_integrity": 50.0,
            "des": 53.6,
        }
        result = compute_lthcs_score(
            ticker="LCID",
            sector="Consumer Cyclical",
            maturity_stage="pre_profit_growth",
            pillar_subscores=subs,
            weights_config=weights_config,
        )
        assert result["lthcs_score"] == 50.3
        assert result["band"] == "weakening"
        assert result["maturity_stage"] == "pre_profit_growth"

    def test_intc_like(self, weights_config):
        # 25.8 / 100 / 30.3 / 50 / 39.1 against [0.15,0.15,0.35,0.20,0.15]
        # Manual: 3.87 + 15.0 + 10.605 + 10.0 + 5.865 = 45.34 -> rounds to 45.3
        subs = {
            "adoption_momentum": 25.8,
            "institutional_confidence": 100.0,
            "financial_evolution": 30.3,
            "thesis_integrity": 50.0,
            "des": 39.1,
        }
        result = compute_lthcs_score(
            ticker="INTC",
            sector="Technology",
            maturity_stage="recovery_stabilization",
            pillar_subscores=subs,
            weights_config=weights_config,
        )
        assert result["lthcs_score"] == 45.3
        assert result["band"] == "review"

    def test_cap_at_100(self, weights_config):
        subs = {name: 100.0 for name in PILLAR_ORDER}
        # Add a big macro tailwind to push past 100; should cap.
        result = compute_lthcs_score(
            ticker="HOT",
            sector="Tech",
            maturity_stage="standard_compounder",
            pillar_subscores=subs,
            weights_config=weights_config,
            ten_y_30d_change_bp=-30.0,  # +2.0 macro tailwind
        )
        assert result["lthcs_score"] == 100.0
        assert result["band"] == "elite"

    def test_cap_at_0(self, weights_config):
        subs = {name: 0.0 for name in PILLAR_ORDER}
        # Headwind macro + vol penalty would push negative; should cap.
        result = compute_lthcs_score(
            ticker="DEAD",
            sector="Tech",
            maturity_stage="standard_compounder",
            pillar_subscores=subs,
            weights_config=weights_config,
            ten_y_30d_change_bp=30.0,  # -2.0
            ticker_volatility=2.0,
            universe_volatilities=[0.1, 0.2, 0.3, 0.4, 0.5],  # ticker well above p90
        )
        assert result["lthcs_score"] == 0.0
        assert result["band"] == "review"

    def test_weights_used_order(self, weights_config, neutral_subscores):
        result = compute_lthcs_score(
            ticker="TEST",
            sector="Tech",
            maturity_stage="pre_profit_growth",
            pillar_subscores=neutral_subscores,
            weights_config=weights_config,
        )
        assert result["weights_used"] == [0.30, 0.20, 0.15, 0.20, 0.15]

    def test_weighted_components_match(self, weights_config):
        subs = {
            "adoption_momentum": 60.0,
            "institutional_confidence": 70.0,
            "financial_evolution": 80.0,
            "thesis_integrity": 40.0,
            "des": 30.0,
        }
        result = compute_lthcs_score(
            ticker="X",
            sector="Y",
            maturity_stage="standard_compounder",
            pillar_subscores=subs,
            weights_config=weights_config,
        )
        weights = [0.25, 0.20, 0.15, 0.20, 0.20]
        expected = [w * subs[name] for w, name in zip(weights, PILLAR_ORDER)]
        for got, exp in zip(result["weighted_components"], expected):
            assert abs(got - exp) < 1e-9

    def test_modifiers_propagated(self, weights_config, neutral_subscores):
        result = compute_lthcs_score(
            ticker="X",
            sector="Y",
            maturity_stage="standard_compounder",
            pillar_subscores=neutral_subscores,
            weights_config=weights_config,
            ten_y_30d_change_bp=30.0,         # -2.0
            ticker_volatility=0.9,
            universe_volatilities=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
            sector_adjustment_override=1.5,
        )
        # 50 + (-2) + 1.5 + (vol mod). ticker 0.9 vs p90=0.91 -> 0; double-check.
        # Universe sorted 0.1..1.0; p90 = 0.1 + (1.0-0.1)*0.9 wait this uses interp.
        # rank = 0.9*(10-1) = 8.1, lo=8 hi=9 -> 0.9 + (1.0-0.9)*0.1 = 0.91
        # ticker 0.9 < 0.91 -> 0.0 mod.
        assert result["modifiers"]["macro_adj"] == -2.0
        assert result["modifiers"]["sector_adj"] == 1.5
        assert result["modifiers"]["volatility_mod"] == 0.0
        assert result["lthcs_score"] == 49.5
        assert result["band"] == "review"

    def test_drift_included(self, weights_config, neutral_subscores):
        result = compute_lthcs_score(
            ticker="X",
            sector="Y",
            maturity_stage="standard_compounder",
            pillar_subscores=neutral_subscores,
            weights_config=weights_config,
            prior_scores={"1d": 48.0, "7d": None, "30d": 45.0, "90d": 40.0},
        )
        assert result["drift_1d"] == 2.0
        assert result["drift_7d"] == 0.0
        assert result["drift_30d"] == 5.0
        assert result["drift_90d"] == 10.0

    def test_confidence_level_high_no_flags(self, weights_config, neutral_subscores):
        r = compute_lthcs_score(
            ticker="X", sector="Y",
            maturity_stage="standard_compounder",
            pillar_subscores=neutral_subscores,
            weights_config=weights_config,
            data_quality_flags=[],
        )
        assert r["confidence_level"] == "high"

    def test_confidence_level_medium_two_flags(self, weights_config, neutral_subscores):
        r = compute_lthcs_score(
            ticker="X", sector="Y",
            maturity_stage="standard_compounder",
            pillar_subscores=neutral_subscores,
            weights_config=weights_config,
            data_quality_flags=["no_xbrl", "stale_earnings"],
        )
        assert r["confidence_level"] == "medium"

    def test_confidence_level_low_three_flags(self, weights_config, neutral_subscores):
        r = compute_lthcs_score(
            ticker="X", sector="Y",
            maturity_stage="standard_compounder",
            pillar_subscores=neutral_subscores,
            weights_config=weights_config,
            data_quality_flags=["no_xbrl", "stale_earnings", "thesis_unavailable"],
        )
        assert r["confidence_level"] == "low"

    def test_data_quality_flags_propagated(self, weights_config, neutral_subscores):
        flags = ["no_xbrl"]
        r = compute_lthcs_score(
            ticker="X", sector="Y",
            maturity_stage="standard_compounder",
            pillar_subscores=neutral_subscores,
            weights_config=weights_config,
            data_quality_flags=flags,
        )
        assert r["data_quality_flags"] == ["no_xbrl"]
        # Returned list should be a copy, not the same identity.
        assert r["data_quality_flags"] is not flags

    def test_missing_pillar_raises(self, weights_config):
        with pytest.raises(ValueError):
            compute_lthcs_score(
                ticker="X", sector="Y",
                maturity_stage="standard_compounder",
                pillar_subscores={"adoption_momentum": 50.0},  # missing 4
                weights_config=weights_config,
            )

    def test_unknown_maturity_raises(self, weights_config, neutral_subscores):
        with pytest.raises(ValueError):
            compute_lthcs_score(
                ticker="X", sector="Y",
                maturity_stage="bogus_stage",
                pillar_subscores=neutral_subscores,
                weights_config=weights_config,
            )

    def test_snapshot_row_keys_match_spec(self, weights_config, neutral_subscores):
        r = compute_lthcs_score(
            ticker="X", sector="Y",
            maturity_stage="standard_compounder",
            pillar_subscores=neutral_subscores,
            weights_config=weights_config,
        )
        expected_keys = {
            "ticker", "lthcs_score", "band",
            "drift_1d", "drift_7d", "drift_30d", "drift_90d",
            "confidence_level", "data_quality_flags",
            "subscores", "modifiers", "maturity_stage",
            "weights_used", "weighted_components", "sector",
        }
        assert expected_keys.issubset(set(r.keys()))
        assert set(r["subscores"].keys()) == set(PILLAR_ORDER)
        assert set(r["modifiers"].keys()) == {"macro_adj", "sector_adj", "volatility_mod"}

    def test_volatility_penalty_lowers_score(self, weights_config, neutral_subscores):
        r = compute_lthcs_score(
            ticker="X", sector="Y",
            maturity_stage="standard_compounder",
            pillar_subscores=neutral_subscores,
            weights_config=weights_config,
            ticker_volatility=10.0,
            universe_volatilities=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
        )
        assert r["modifiers"]["volatility_mod"] == -3.0
        assert r["lthcs_score"] == 47.0


# --- Composite renormalization for stubbed pillars -------------------------

class TestRenormalizeOnStubbedThesis:
    """Verify the composite drops a stubbed pillar instead of letting it
    dilute the score with a neutral 50.

    The pipeline emits a 'thesis_unavailable' data_quality_flag when AV
    sentiment isn't fresh for that ticker yet (rotation ramping). Without
    composite-level renormalization, every such ticker mechanically caps
    at ~78 even with strong real signals on all other pillars.
    """

    def _subs(self, **overrides):
        # Start from a strong-conviction tech ticker.
        base = {
            "adoption_momentum": 100.0,
            "institutional_confidence": 100.0,
            "financial_evolution": 100.0,
            "thesis_integrity": 50.0,  # stubbed neutral placeholder
            "des": 50.0,
        }
        base.update(overrides)
        return base

    def test_no_flag_no_renorm(self, weights_config):
        """Documented 5-pillar formula when no stub flag is present."""
        subs = self._subs()
        r = compute_lthcs_score(
            ticker="X", sector="Technology",
            maturity_stage="standard_compounder",
            pillar_subscores=subs,
            weights_config=weights_config,
            data_quality_flags=[],
        )
        # 0.25*100 + 0.20*100 + 0.15*100 + 0.20*50 + 0.20*50 = 80
        assert r["lthcs_score"] == pytest.approx(80.0)
        assert r["effective_weights"] == r["weights_used"]
        assert r["dropped_pillars"] == []

    def test_thesis_flag_renormalizes(self, weights_config):
        """thesis_unavailable → thesis weight redistributed proportionally."""
        subs = self._subs()
        r = compute_lthcs_score(
            ticker="X", sector="Technology",
            maturity_stage="standard_compounder",
            pillar_subscores=subs,
            weights_config=weights_config,
            data_quality_flags=["thesis_unavailable"],
        )
        # Documented weights: [0.25, 0.20, 0.15, 0.20, 0.20]. Drop index 3
        # (thesis = 0.20). Remaining sum = 0.80. Renormalized:
        #   adoption: 0.25 / 0.80 = 0.3125
        #   inst:     0.20 / 0.80 = 0.250
        #   fin:      0.15 / 0.80 = 0.1875
        #   thesis:   0.00
        #   des:      0.20 / 0.80 = 0.250
        # Composite = 0.3125*100 + 0.25*100 + 0.1875*100 + 0*50 + 0.25*50
        #           = 31.25 + 25 + 18.75 + 0 + 12.5 = 87.5
        assert r["lthcs_score"] == pytest.approx(87.5)
        assert r["dropped_pillars"] == ["thesis_integrity"]
        assert r["effective_weights"][3] == 0.0
        assert sum(r["effective_weights"]) == pytest.approx(1.0)
        # Documented weights remain intact so the audit trail is preserved.
        assert r["weights_used"] == [0.25, 0.20, 0.15, 0.20, 0.20]

    def test_other_flags_dont_trigger_renorm(self, weights_config):
        """Only flags in _FLAGS_TO_DROPPED_PILLAR drop a pillar."""
        subs = self._subs()
        r = compute_lthcs_score(
            ticker="X", sector="Y",
            maturity_stage="standard_compounder",
            pillar_subscores=subs,
            weights_config=weights_config,
            data_quality_flags=["trends_unavailable", "volatility_unavailable"],
        )
        # No renorm — these flags don't drop a whole pillar.
        assert r["lthcs_score"] == pytest.approx(80.0)
        assert r["dropped_pillars"] == []
