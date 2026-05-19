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
    _load_volatility_modifier_config,
    _parse_trigger_expression,
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

    def test_85_is_elite(self, weights_config):
        # Post-2026-05-18 calibration: elite.min lowered from 90 to 85
        # so the band is reachable given current pillar ceilings.
        assert assign_band(85.0, weights_config["score_bands"]) == "elite"

    def test_82_is_high_confidence(self, weights_config):
        # High confidence band is 80..84 after the 2026-05-18 recalibration.
        assert assign_band(82.0, weights_config["score_bands"]) == "high_confidence"

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


# --- Theoretical-max ceiling (band-calibration sanity) ---------------------

class TestTheoreticalMax:
    """Confirms the elite band is mathematically reachable.

    Backfill validation (2026-05-18) found zero Elite-band rows across 90
    backfilled days. Root cause: empirical pillar ceilings (Thesis stuck at
    50.0 placeholder for 88/90 dates, DES capped near 75 by sector adj) put
    the realistic composite ceiling at ~88 even though the math allows 100.
    These tests pin the theoretical max so we'd catch a future change that
    accidentally lowers it below the elite threshold.
    """

    def test_perfect_pillars_no_modifiers_hits_100(self, weights_config):
        # With all 5 pillars at 100 and zero modifiers, every maturity
        # profile must reach 100.0 — weights sum to 1.0 by construction.
        subs = {name: 100.0 for name in PILLAR_ORDER}
        for profile in weights_config["profiles"].keys():
            r = compute_lthcs_score(
                ticker="MAX",
                sector="Tech",
                maturity_stage=profile,
                pillar_subscores=subs,
                weights_config=weights_config,
            )
            assert r["lthcs_score"] == 100.0, (
                f"theoretical max for profile {profile!r} = {r['lthcs_score']}, "
                "expected 100.0 — pillar weights may not sum to 1.0"
            )
            assert r["band"] == "elite", (
                f"profile {profile!r} composite=100.0 but band={r['band']!r}; "
                "elite threshold may have crept above 100"
            )

    def test_perfect_pillars_with_best_case_modifiers_caps_at_100(
        self, weights_config
    ):
        # Best-case modifiers: macro_adj = +2.0 (10Y plunge), vol_mod = 0.0
        # (not in top decile), sector_adj override = 0.0. All-pillars-100
        # would raise 100 -> 102 raw but composite must cap at 100.
        subs = {name: 100.0 for name in PILLAR_ORDER}
        r = compute_lthcs_score(
            ticker="MAX",
            sector="Tech",
            maturity_stage="standard_compounder",
            pillar_subscores=subs,
            weights_config=weights_config,
            ten_y_30d_change_bp=-50.0,  # +2.0 macro tailwind
            ticker_volatility=1.0,
            universe_volatilities=[10.0, 20.0, 30.0, 40.0, 50.0,
                                   60.0, 70.0, 80.0, 90.0, 100.0],
            sector_adjustment_override=0.0,
        )
        assert r["modifiers"]["macro_adj"] == 2.0
        assert r["modifiers"]["volatility_mod"] == 0.0
        assert r["lthcs_score"] == 100.0
        assert r["band"] == "elite"

    def test_elite_band_threshold_is_reachable(self, weights_config):
        # Regression guard: the configured elite.min must be reachable from
        # plausibly-achievable pillar subscores. With the current backfill
        # showing Thesis p99=70 and DES p99=69.4, a ticker that hits
        # adoption=100, institutional=99, financial=99, thesis=70, des=69
        # should reach Elite under any reasonable calibration. If this
        # test fails, the elite threshold has been raised above the
        # empirical ceiling and Elite will be unreachable in practice.
        empirical_top_quintile = {
            "adoption_momentum": 100.0,
            "institutional_confidence": 99.0,
            "financial_evolution": 99.0,
            "thesis_integrity": 70.0,
            "des": 69.0,
        }
        r = compute_lthcs_score(
            ticker="REAL_TOP",
            sector="Energy",
            maturity_stage="standard_compounder",
            pillar_subscores=empirical_top_quintile,
            weights_config=weights_config,
        )
        elite_min = float(
            weights_config["score_bands"]["elite"]["min"]
        )
        assert r["lthcs_score"] >= elite_min, (
            f"empirical top-quintile pillars yield composite "
            f"{r['lthcs_score']} but elite.min={elite_min} — band "
            "unreachable; either lower elite.min or unblock the "
            "stuck pillars (Thesis/DES)."
        )
        assert r["band"] == "elite"


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

    def test_crypto_thesis_flag_renormalizes(self, weights_config):
        """crypto_thesis_unavailable → same thesis pillar drop as the equity flag
        (per docs/lthcs-crypto-pillar-adapter-spec.md §9)."""
        subs = self._subs()
        r = compute_lthcs_score(
            ticker="BTC", sector="Crypto",
            maturity_stage="standard_compounder",
            pillar_subscores=subs,
            weights_config=weights_config,
            data_quality_flags=["crypto_thesis_unavailable"],
        )
        # Same arithmetic as test_thesis_flag_renormalizes: thesis weight 0.20
        # is redistributed proportionally across the remaining 4 pillars.
        assert r["lthcs_score"] == pytest.approx(87.5)
        assert r["dropped_pillars"] == ["thesis_integrity"]
        assert r["effective_weights"][3] == 0.0
        assert sum(r["effective_weights"]) == pytest.approx(1.0)
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


# --- Trigger-expression parser ---------------------------------------------

class TestParseTriggerExpression:
    """Verify the modifier trigger-string parser."""

    def test_strict_gt(self):
        assert _parse_trigger_expression(
            "trailing_30d_volatility_percentile > 90"
        ) == ("trailing_30d_volatility_percentile", ">", 90.0)

    def test_gte(self):
        assert _parse_trigger_expression("x >= 5") == ("x", ">=", 5.0)

    def test_lt(self):
        assert _parse_trigger_expression("x < 10") == ("x", "<", 10.0)

    def test_lte(self):
        assert _parse_trigger_expression("x <= 10.5") == ("x", "<=", 10.5)

    def test_whitespace_tolerant(self):
        assert _parse_trigger_expression("  metric  >   42  ") == (
            "metric", ">", 42.0,
        )

    def test_empty_returns_none(self):
        assert _parse_trigger_expression("") is None

    def test_none_returns_none(self):
        assert _parse_trigger_expression(None) is None  # type: ignore[arg-type]

    def test_no_operator_returns_none(self):
        assert _parse_trigger_expression("just_a_metric") is None

    def test_non_numeric_threshold_returns_none(self):
        assert _parse_trigger_expression("metric > banana") is None

    def test_empty_metric_returns_none(self):
        assert _parse_trigger_expression(" > 5") is None

    def test_unsupported_operator_returns_none(self):
        # '==' is not a supported comparator.
        assert _parse_trigger_expression("x == 5") is None


# --- Volatility modifier config loader -------------------------------------

class TestLoadVolatilityModifierConfig:
    """Verify the loader pulls config from weights.json and falls back
    gracefully when the block is missing or malformed."""

    def test_loads_from_weights_json(self, weights_config):
        # Real on-disk weights.json matches the documented defaults.
        assert _load_volatility_modifier_config(weights_config) == (90.0, ">", -3.0)

    def test_custom_threshold(self):
        cfg = {
            "modifiers": {
                "volatility_modifier": {
                    "trigger": "trailing_30d_volatility_percentile > 80",
                    "magnitude": -3.0,
                    "applies_to": "all_tickers",
                },
            },
        }
        assert _load_volatility_modifier_config(cfg) == (80.0, ">", -3.0)

    def test_custom_magnitude(self):
        cfg = {
            "modifiers": {
                "volatility_modifier": {
                    "trigger": "trailing_30d_volatility_percentile > 90",
                    "magnitude": -5.0,
                    "applies_to": "all_tickers",
                },
            },
        }
        assert _load_volatility_modifier_config(cfg) == (90.0, ">", -5.0)

    def test_missing_modifiers_block_falls_back(self):
        assert _load_volatility_modifier_config({}) == (90.0, ">", -3.0)

    def test_missing_volatility_block_falls_back(self):
        assert _load_volatility_modifier_config({"modifiers": {}}) == (
            90.0, ">", -3.0,
        )

    def test_malformed_trigger_falls_back(self):
        cfg = {
            "modifiers": {
                "volatility_modifier": {
                    "trigger": "this is not parseable",
                    "magnitude": -3.0,
                    "applies_to": "all_tickers",
                },
            },
        }
        assert _load_volatility_modifier_config(cfg) == (90.0, ">", -3.0)

    def test_unknown_metric_falls_back(self):
        cfg = {
            "modifiers": {
                "volatility_modifier": {
                    "trigger": "unrelated_metric > 50",
                    "magnitude": -3.0,
                    "applies_to": "all_tickers",
                },
            },
        }
        assert _load_volatility_modifier_config(cfg) == (90.0, ">", -3.0)

    def test_malformed_magnitude_falls_back(self):
        cfg = {
            "modifiers": {
                "volatility_modifier": {
                    "trigger": "trailing_30d_volatility_percentile > 90",
                    "magnitude": "very negative",
                    "applies_to": "all_tickers",
                },
            },
        }
        assert _load_volatility_modifier_config(cfg) == (90.0, ">", -3.0)

    def test_unsupported_applies_to_falls_back(self):
        cfg = {
            "modifiers": {
                "volatility_modifier": {
                    "trigger": "trailing_30d_volatility_percentile > 50",
                    "magnitude": -10.0,
                    "applies_to": "tech_only",
                },
            },
        }
        # Unsupported applies_to -> we fall back to defaults rather than
        # quietly apply a config that we can't faithfully honour.
        assert _load_volatility_modifier_config(cfg) == (90.0, ">", -3.0)

    def test_gte_operator_supported(self):
        cfg = {
            "modifiers": {
                "volatility_modifier": {
                    "trigger": "trailing_30d_volatility_percentile >= 90",
                    "magnitude": -3.0,
                    "applies_to": "all_tickers",
                },
            },
        }
        assert _load_volatility_modifier_config(cfg) == (90.0, ">=", -3.0)


# --- Modifier wiring into compute_volatility_modifier ----------------------

class TestVolatilityModifierFromConfig:
    """End-to-end: configuration in weights.json drives the modifier."""

    _UNIVERSE_1_TO_100 = [float(x) for x in range(1, 101)]

    def _cfg(self, trigger, magnitude, applies_to="all_tickers"):
        return {
            "modifiers": {
                "volatility_modifier": {
                    "trigger": trigger,
                    "magnitude": magnitude,
                    "applies_to": applies_to,
                },
            },
        }

    def test_default_from_real_weights_json(self, weights_config):
        """The on-disk weights.json drives the modifier with the canonical
        90th-percentile / -3.0 contract."""
        # universe 1..100; p90 = 1 + (100-1)*0.9 = 90.1
        assert compute_volatility_modifier(
            91.0, self._UNIVERSE_1_TO_100, weights_config=weights_config,
        ) == -3.0
        # Below p90 -> no penalty.
        assert compute_volatility_modifier(
            50.0, self._UNIVERSE_1_TO_100, weights_config=weights_config,
        ) == 0.0

    def test_boundary_at_exact_p90_strict_does_not_fire(self):
        # Construct a universe whose p90 is exactly 90.0.
        universe = [0.0] + [90.0] * 99
        cfg = self._cfg("trailing_30d_volatility_percentile > 90", -3.0)
        assert compute_volatility_modifier(
            90.0, universe, weights_config=cfg,
        ) == 0.0

    def test_just_above_p90_fires(self):
        universe = [0.0] + [90.0] * 99
        cfg = self._cfg("trailing_30d_volatility_percentile > 90", -3.0)
        assert compute_volatility_modifier(
            90.01, universe, weights_config=cfg,
        ) == -3.0

    def test_custom_magnitude_applied(self):
        cfg = self._cfg("trailing_30d_volatility_percentile > 90", -5.0)
        # universe 1..100; p90=90.1, ticker 99 > 90.1.
        assert compute_volatility_modifier(
            99.0, self._UNIVERSE_1_TO_100, weights_config=cfg,
        ) == -5.0

    def test_custom_threshold_applied(self):
        # Threshold = 80th percentile. universe 1..100; p80 = 1 + (100-1)*0.8 = 80.2
        cfg = self._cfg("trailing_30d_volatility_percentile > 80", -3.0)
        # ticker 81 > 80.2 -> fires.
        assert compute_volatility_modifier(
            81.0, self._UNIVERSE_1_TO_100, weights_config=cfg,
        ) == -3.0
        # ticker 80 < 80.2 -> does not fire.
        assert compute_volatility_modifier(
            80.0, self._UNIVERSE_1_TO_100, weights_config=cfg,
        ) == 0.0

    def test_missing_block_falls_back_to_defaults(self):
        # No modifiers block at all.
        cfg = {}
        # Universe 1..100; default p90=90.1 with -3.0 magnitude.
        assert compute_volatility_modifier(
            95.0, self._UNIVERSE_1_TO_100, weights_config=cfg,
        ) == -3.0

    def test_malformed_trigger_falls_back_to_defaults(self, caplog):
        cfg = self._cfg("garbage expression", -3.0)
        with caplog.at_level("WARNING", logger="lthcs.score"):
            result = compute_volatility_modifier(
                95.0, self._UNIVERSE_1_TO_100, weights_config=cfg,
            )
        assert result == -3.0
        # Warning emitted at least once.
        assert any("malformed" in rec.message for rec in caplog.records)

    def test_weights_config_none_uses_defaults(self):
        # Passing weights_config=None should be safe and use defaults.
        assert compute_volatility_modifier(
            95.0, self._UNIVERSE_1_TO_100, weights_config=None,
        ) == -3.0

    def test_compute_lthcs_score_uses_config(self, weights_config, neutral_subscores):
        """Smoke test that compute_lthcs_score passes the config through."""
        # Use real weights.json -- volatility modifier should fire at -3.
        r = compute_lthcs_score(
            ticker="X", sector="Y",
            maturity_stage="standard_compounder",
            pillar_subscores=neutral_subscores,
            weights_config=weights_config,
            ticker_volatility=95.0,
            universe_volatilities=self._UNIVERSE_1_TO_100,
        )
        assert r["modifiers"]["volatility_mod"] == -3.0
        assert r["lthcs_score"] == 47.0

    def test_compute_lthcs_score_respects_overridden_magnitude(self, neutral_subscores):
        # Build a weights_config with a -7 magnitude override.
        cfg = {
            "profiles": {"standard_compounder": [0.25, 0.20, 0.15, 0.20, 0.20]},
            "score_bands": {
                "review": {"min": 0, "max": 49},
                "weakening": {"min": 50, "max": 59},
            },
            "modifiers": {
                "volatility_modifier": {
                    "trigger": "trailing_30d_volatility_percentile > 90",
                    "magnitude": -7.0,
                    "applies_to": "all_tickers",
                },
            },
        }
        r = compute_lthcs_score(
            ticker="X", sector="Y",
            maturity_stage="standard_compounder",
            pillar_subscores=neutral_subscores,
            weights_config=cfg,
            ticker_volatility=95.0,
            universe_volatilities=self._UNIVERSE_1_TO_100,
        )
        assert r["modifiers"]["volatility_mod"] == -7.0
        assert r["lthcs_score"] == 43.0
