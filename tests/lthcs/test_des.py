"""Tests for lthcs.pillars.des.

No live network: macro inputs and sector weights are passed inline as
fixtures. The real ``data/lthcs/sector_des_weights.json`` is loaded
only by the real-world sanity tests at the bottom (per the brief).
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from lthcs.pillars import des
from lthcs.pillars.des import (
    compute_des,
    load_sector_weights,
    normalize_macro_signal,
)


# --- Inline synthetic fixture (per brief) -----------------------------------

SAMPLE_WEIGHTS: Dict[str, Any] = {
    "signal_normalization": {
        "wti_oil_usd": {"low": 40, "high": 130, "neutral": 75},
        "fed_funds_pct": {"low": 0, "high": 6, "neutral": 2.5},
        "ten_y_yield_pct": {"low": 1, "high": 6, "neutral": 3.5},
    },
    "sectors": {
        "Energy": {
            "wti_oil_usd": 0.7,
            "fed_funds_pct": 0.0,
            "ten_y_yield_pct": -0.1,
        },
        "Financials": {
            "wti_oil_usd": 0.0,
            "fed_funds_pct": 0.4,
            "ten_y_yield_pct": 0.5,
        },
        "Consumer Discretionary": {
            "wti_oil_usd": -0.4,
            "fed_funds_pct": -0.3,
            "ten_y_yield_pct": -0.3,
        },
    },
    "ticker_overrides": {
        "TSLA": {"wti_oil_usd": 0.5, "_note": "EV oil-positive"},
        "LCID": {"wti_oil_usd": 0.6},
    },
}


# --- normalize_macro_signal -------------------------------------------------


class TestNormalizeMacroSignal:
    def test_low_maps_to_minus_one(self):
        tilt = normalize_macro_signal(
            "wti_oil_usd", 40.0, SAMPLE_WEIGHTS["signal_normalization"]
        )
        assert tilt == pytest.approx(-1.0)

    def test_high_maps_to_plus_one(self):
        tilt = normalize_macro_signal(
            "wti_oil_usd", 130.0, SAMPLE_WEIGHTS["signal_normalization"]
        )
        assert tilt == pytest.approx(1.0)

    def test_midpoint_maps_to_zero(self):
        # midpoint between low=40 and high=130 is 85; neutral=75 is offset.
        tilt = normalize_macro_signal(
            "wti_oil_usd", 85.0, SAMPLE_WEIGHTS["signal_normalization"]
        )
        assert tilt == pytest.approx(0.0)

    def test_neutral_roughly_zero(self):
        # neutral=75 is somewhat below the midpoint (85); tilt should be small.
        tilt = normalize_macro_signal(
            "wti_oil_usd", 75.0, SAMPLE_WEIGHTS["signal_normalization"]
        )
        # (75 - 40) / 90 * 2 - 1 = 70/90 - 1 = -2/9 ~= -0.222
        assert -0.3 < tilt < 0.0

    def test_below_low_clipped(self):
        tilt = normalize_macro_signal(
            "wti_oil_usd", 10.0, SAMPLE_WEIGHTS["signal_normalization"]
        )
        assert tilt == pytest.approx(-1.0)

    def test_above_high_clipped(self):
        tilt = normalize_macro_signal(
            "wti_oil_usd", 500.0, SAMPLE_WEIGHTS["signal_normalization"]
        )
        assert tilt == pytest.approx(1.0)

    def test_none_input_returns_zero(self):
        tilt = normalize_macro_signal(
            "wti_oil_usd", None, SAMPLE_WEIGHTS["signal_normalization"]
        )
        assert tilt == 0.0

    def test_unknown_signal_returns_zero(self):
        tilt = normalize_macro_signal(
            "some_unknown_signal", 42.0, SAMPLE_WEIGHTS["signal_normalization"]
        )
        assert tilt == 0.0

    def test_empty_signal_normalization_returns_zero(self):
        tilt = normalize_macro_signal("wti_oil_usd", 100.0, {})
        assert tilt == 0.0


# --- compute_des ------------------------------------------------------------


class TestComputeDes:
    def test_energy_high_oil_bullish(self):
        macro = {"wti_oil_usd": 120.0}
        result = compute_des("XOM", "Energy", macro, SAMPLE_WEIGHTS)
        assert result["sub_score"] > 50.0
        assert result["weights_source"] == "sector"
        assert result["components"]["applied_overrides"] == []
        assert result["data_quality"]["sector_known"] is True
        assert result["data_quality"]["macro_signals_present"] == 1

    def test_consumer_discretionary_high_oil_bearish(self):
        macro = {"wti_oil_usd": 120.0}
        result = compute_des(
            "AMZN", "Consumer Discretionary", macro, SAMPLE_WEIGHTS
        )
        assert result["sub_score"] < 50.0
        assert result["components"]["applied_overrides"] == []

    def test_tsla_override_flips_oil_tilt_positive(self):
        macro = {"wti_oil_usd": 120.0}
        result = compute_des(
            "TSLA", "Consumer Discretionary", macro, SAMPLE_WEIGHTS
        )
        # CD sector defaults give -0.4 on oil (bearish). TSLA override
        # replaces with +0.5 (bullish). With high oil tilt > 0, override
        # flips the contribution to positive on oil.
        assert result["sub_score"] > 50.0
        assert "wti_oil_usd" in result["components"]["applied_overrides"]
        assert result["weights_source"] == "ticker_overrides_partial"
        # Override should only have replaced the one signal; other CD
        # sensitivities (fed_funds, 10Y) remain at the sector defaults.
        contribs = result["components"]["signal_contributions"]
        # oil contribution should now be positive (sensitivity 0.5 *
        # high-oil tilt > 0).
        assert contribs["wti_oil_usd"] > 0.0

    def test_lcid_override_oil_bullish(self):
        macro = {"wti_oil_usd": 120.0}
        result = compute_des(
            "LCID", "Consumer Discretionary", macro, SAMPLE_WEIGHTS
        )
        assert result["sub_score"] > 50.0
        assert "wti_oil_usd" in result["components"]["applied_overrides"]

    def test_financials_high_10y_bullish(self):
        macro = {"ten_y_yield_pct": 5.5}
        result = compute_des(
            "JPM", "Financials", macro, SAMPLE_WEIGHTS
        )
        assert result["sub_score"] > 50.0
        assert result["components"]["signal_tilts"]["ten_y_yield_pct"] > 0.0

    def test_financials_rising_10y_uses_change_signal_or_neutral(self):
        # SAMPLE_WEIGHTS does NOT include ten_y_30d_change_bp in either
        # signal_normalization or Financials sector. Per spec: "if you've
        # wired that signal, > 50; if not, still neutral". With the
        # inline fixture we expect neutral (only the listed signals are
        # in the sector dict, and missing signals contribute 0).
        macro = {"ten_y_yield_pct": 3.5, "ten_y_30d_change_bp": 40.0}
        result = compute_des(
            "JPM", "Financials", macro, SAMPLE_WEIGHTS
        )
        # ten_y at 3.5 is the midpoint of [1, 6] -> tilt = 0.0; oil
        # missing -> 0; fed_funds missing -> 0. Net contribution ~0,
        # sub_score ~ 50.0.
        assert result["sub_score"] == pytest.approx(50.0, abs=1.0)

    def test_financials_rising_10y_via_real_weights(self):
        # The REAL config wires ten_y_30d_change_bp into Financials with
        # sensitivity 0.30 and signal_normalization low=-50/high=+50.
        # A 40bp rise (tilt ~ +0.8) on otherwise-neutral inputs should
        # push above 50.
        real = load_sector_weights()
        macro = {"ten_y_30d_change_bp": 40.0}
        result = compute_des("JPM", "Financials", macro, real)
        assert result["sub_score"] > 50.0

    def test_unknown_sector_returns_50_with_flag(self):
        macro = {"wti_oil_usd": 120.0}
        result = compute_des(
            "ZZZZ", "FakeSector", macro, SAMPLE_WEIGHTS
        )
        assert result["sub_score"] == 50.0
        assert result["data_quality"]["sector_known"] is False
        assert result["weights_source"] == "sector_missing"
        assert result["components"]["applied_overrides"] == []
        # macro_signals_present should still reflect what was provided.
        assert result["data_quality"]["macro_signals_present"] == 1

    def test_ticker_not_in_overrides_no_applied(self):
        macro = {"wti_oil_usd": 120.0}
        result = compute_des(
            "AAPL", "Consumer Discretionary", macro, SAMPLE_WEIGHTS
        )
        assert result["components"]["applied_overrides"] == []
        assert result["weights_source"] == "sector"

    def test_ticker_in_overrides_records_signal(self):
        macro = {"wti_oil_usd": 120.0}
        result = compute_des(
            "TSLA", "Consumer Discretionary", macro, SAMPLE_WEIGHTS
        )
        # Only wti_oil_usd was in the TSLA override block; _note is
        # metadata and must NOT appear.
        assert result["components"]["applied_overrides"] == ["wti_oil_usd"]

    def test_alias_of_and_metadata_keys_skipped(self):
        weights = {
            "signal_normalization": {
                "wti_oil_usd": {"low": 40, "high": 130, "neutral": 75},
            },
            "sectors": {
                "Technology": {
                    "_alias_of": "Information Technology",
                    "_comment": "some note",
                    "wti_oil_usd": 0.1,
                },
            },
            "ticker_overrides": {},
        }
        macro = {"wti_oil_usd": 130.0}
        result = compute_des("MSFT", "Technology", macro, weights)
        # Only wti_oil_usd should appear in contributions; _alias_of /
        # _comment are silently skipped.
        contribs = result["components"]["signal_contributions"]
        assert set(contribs.keys()) == {"wti_oil_usd"}
        # 0.1 sensitivity * +1.0 tilt * 30 magnitude = +3 from 50 -> 53.0
        assert result["sub_score"] == pytest.approx(53.0, abs=0.01)

    def test_sub_score_rounded_to_one_decimal(self):
        # Pick values that produce something with more decimals.
        macro = {"wti_oil_usd": 77.0}  # tilt = (77-40)/90*2-1 = -0.1778
        result = compute_des(
            "XOM", "Energy", macro, SAMPLE_WEIGHTS
        )
        score = result["sub_score"]
        # Round-trip check: float should equal its 1-decimal rounding.
        assert score == round(score, 1)

    def test_empty_macro_inputs_returns_50(self):
        result = compute_des("XOM", "Energy", {}, SAMPLE_WEIGHTS)
        assert result["sub_score"] == 50.0
        assert result["data_quality"]["macro_signals_present"] == 0
        assert result["data_quality"]["has_macro_inputs"] is False
        # Sector is known so the flag stays True.
        assert result["data_quality"]["sector_known"] is True

    def test_none_values_count_as_missing(self):
        macro = {"wti_oil_usd": None, "fed_funds_pct": None}
        result = compute_des("XOM", "Energy", macro, SAMPLE_WEIGHTS)
        assert result["data_quality"]["macro_signals_present"] == 0
        assert result["sub_score"] == 50.0

    def test_clipping_to_zero_to_hundred(self):
        # Build a degenerate weight that forces a huge contribution to
        # confirm we clip to [0, 100].
        weights = {
            "signal_normalization": {
                "x": {"low": 0, "high": 10, "neutral": 5},
            },
            "sectors": {"Hyper": {"x": 1.0}},
            "ticker_overrides": {},
        }
        # 1.0 sensitivity * +1.0 tilt * magnitude_scale=200 -> +200 from
        # 50 -> raw 250, clipped to 100.
        result = compute_des(
            "T", "Hyper", {"x": 100.0}, weights, magnitude_scale=200.0
        )
        assert result["sub_score"] == 100.0

        # Conversely, negative direction clips to 0.
        result_neg = compute_des(
            "T", "Hyper", {"x": -100.0}, weights, magnitude_scale=200.0
        )
        assert result_neg["sub_score"] == 0.0

    def test_components_shape(self):
        macro = {"wti_oil_usd": 120.0}
        result = compute_des("XOM", "Energy", macro, SAMPLE_WEIGHTS)
        assert "signal_tilts" in result["components"]
        assert "signal_contributions" in result["components"]
        assert "total_contribution" in result["components"]
        assert "applied_overrides" in result["components"]
        # All three Energy signals should show up in both maps.
        assert set(result["components"]["signal_tilts"].keys()) == {
            "wti_oil_usd",
            "fed_funds_pct",
            "ten_y_yield_pct",
        }


# --- load_sector_weights ----------------------------------------------------


class TestLoadSectorWeights:
    def test_default_path_loads_real_config(self):
        # Sanity: file exists and is well-formed.
        weights = load_sector_weights()
        assert "sectors" in weights
        assert "signal_normalization" in weights
        assert "Energy" in weights["sectors"]
        assert "Financials" in weights["sectors"]

    def test_explicit_path_overrides_default(self, tmp_path):
        import json as _json

        custom = {
            "signal_normalization": {
                "x": {"low": 0, "high": 1, "neutral": 0.5}
            },
            "sectors": {"X": {"x": 0.5}},
            "ticker_overrides": {},
        }
        p = tmp_path / "custom.json"
        p.write_text(_json.dumps(custom))
        loaded = load_sector_weights(p)
        assert loaded == custom


# --- Real-world sanity assertions -------------------------------------------


class TestRealWorldSpecAssertions:
    """Per brief: use the actual current macro state + real config to
    verify each named ticker scores above 55.

    Macro state (hardcoded per brief):
      CPI YoY     = 2.9%
      Fed Funds   = 3.64%
      10Y yield   = 4.47%
      WTI oil     = $105.78
      Unemp       = 4.0% (reasonable current value; not pinned by brief)
      10Y 30d chg = 0 bp (no recent move)
    """

    REAL_MACRO: Dict[str, float] = {
        "cpi_yoy_pct": 2.9,
        "fed_funds_pct": 3.64,
        "ten_y_yield_pct": 4.47,
        "ten_y_30d_change_bp": 0.0,
        "unemployment_pct": 4.0,
        "wti_oil_usd": 105.78,
    }

    @pytest.fixture(scope="class")
    def real_weights(self) -> Dict[str, Any]:
        return load_sector_weights()

    def test_energy_xom_above_55(self, real_weights):
        result = compute_des(
            "XOM", "Energy", self.REAL_MACRO, real_weights
        )
        assert result["sub_score"] > 55.0, result

    def test_financials_jpm_above_55(self, real_weights):
        result = compute_des(
            "JPM", "Financials", self.REAL_MACRO, real_weights
        )
        assert result["sub_score"] > 55.0, result

    def test_tsla_above_55_via_override(self, real_weights):
        result = compute_des(
            "TSLA", "Consumer Discretionary", self.REAL_MACRO, real_weights
        )
        assert result["sub_score"] > 55.0, result
        assert "wti_oil_usd" in result["components"]["applied_overrides"]

    def test_lcid_above_55_via_override(self, real_weights):
        result = compute_des(
            "LCID", "Consumer Discretionary", self.REAL_MACRO, real_weights
        )
        assert result["sub_score"] > 55.0, result
        assert "wti_oil_usd" in result["components"]["applied_overrides"]


# --- Expanded macro signals (real_10y_yield, VIX, M2 YoY) -------------------


SAMPLE_WEIGHTS_EXPANDED: Dict[str, Any] = {
    "signal_normalization": {
        "wti_oil_usd":         {"low": 40,    "high": 130,  "neutral": 75},
        "fed_funds_pct":       {"low": 0,     "high": 6,    "neutral": 2.5},
        "ten_y_yield_pct":     {"low": 1,     "high": 6,    "neutral": 3.5},
        "real_10y_yield_pct":  {"low": -1.0,  "high": 3.5,  "neutral": 1.5},
        "vix_index":           {"low": 10.0,  "high": 40.0, "neutral": 18.0},
        "m2_yoy_pct":          {"low": -2.0,  "high": 12.0, "neutral": 4.0},
    },
    "sectors": {
        "Technology": {
            "wti_oil_usd":         0.00,
            "ten_y_yield_pct":    -0.22,
            "real_10y_yield_pct": -0.20,
            "vix_index":          -0.10,
            "m2_yoy_pct":          0.15,
        },
        "Real Estate": {
            "wti_oil_usd":        -0.10,
            "ten_y_yield_pct":    -0.60,
            "real_10y_yield_pct": -0.30,
            "vix_index":          -0.10,
            "m2_yoy_pct":          0.10,
        },
        "Financials": {
            "wti_oil_usd":         0.00,
            "ten_y_yield_pct":     0.50,
            "real_10y_yield_pct":  0.10,
            "vix_index":          -0.05,
            "m2_yoy_pct":          0.05,
        },
    },
    "ticker_overrides": {},
}


class TestExpandedMacroSignals:
    """Regression coverage for the three Phase-1.5 macro additions
    (real_10y_yield_pct, vix_index, m2_yoy_pct)."""

    def test_fixture_extends_with_new_signals(self):
        norms = SAMPLE_WEIGHTS_EXPANDED["signal_normalization"]
        assert "real_10y_yield_pct" in norms
        assert "vix_index" in norms
        assert "m2_yoy_pct" in norms
        # Each Tech / Real Estate / Financials sector has all three.
        for sec in ("Technology", "Real Estate", "Financials"):
            block = SAMPLE_WEIGHTS_EXPANDED["sectors"][sec]
            assert "real_10y_yield_pct" in block
            assert "vix_index" in block
            assert "m2_yoy_pct" in block

    def test_tech_drops_on_high_real_yield_and_high_vix(self):
        # Real 10Y at 3.5 (high=+1 tilt), VIX at 40 (high=+1 tilt), and
        # ten_y_yield at 5.5 (tilt > 0). All three signals carry NEGATIVE
        # Tech sensitivities -> sub_score must drop below 50.
        macro = {
            "ten_y_yield_pct": 5.5,
            "real_10y_yield_pct": 3.5,
            "vix_index": 40.0,
            "m2_yoy_pct": 4.0,  # neutral
        }
        result = compute_des("MSFT", "Technology", macro, SAMPLE_WEIGHTS_EXPANDED)
        assert result["sub_score"] < 50.0, result
        contribs = result["components"]["signal_contributions"]
        assert contribs["real_10y_yield_pct"] < 0.0
        assert contribs["vix_index"] < 0.0

    def test_real_estate_more_sensitive_than_tech_to_falling_real_yield(self):
        # Real 10Y at -1.0 (low=-1 tilt). RE sensitivity is -0.30 -> the
        # contribution is +0.30. Tech sensitivity is -0.20 -> +0.20. So
        # holding everything else equal, RE should lift more than Tech.
        macro = {"real_10y_yield_pct": -1.0}
        re_result = compute_des(
            "AMT", "Real Estate", macro, SAMPLE_WEIGHTS_EXPANDED
        )
        tech_result = compute_des(
            "MSFT", "Technology", macro, SAMPLE_WEIGHTS_EXPANDED
        )
        # Both lift above 50; RE lifts more.
        assert re_result["sub_score"] > tech_result["sub_score"]
        assert re_result["sub_score"] > 50.0
        assert tech_result["sub_score"] > 50.0
        # Numerically: RE contrib = -0.30 * -1.0 = +0.30 (vs Tech +0.20).
        re_contrib = re_result["components"]["signal_contributions"][
            "real_10y_yield_pct"
        ]
        tech_contrib = tech_result["components"]["signal_contributions"][
            "real_10y_yield_pct"
        ]
        assert re_contrib == pytest.approx(0.30, abs=1e-6)
        assert tech_contrib == pytest.approx(0.20, abs=1e-6)

    def test_missing_new_signals_contribute_zero(self):
        # Backwards-compat: if real_10y/vix/m2 are absent (or None),
        # they must contribute 0 tilt and the score derives entirely
        # from the other signals.
        macro_with = {
            "ten_y_yield_pct": 3.5,  # neutral midpoint -> tilt 0
            "real_10y_yield_pct": None,
            "vix_index": None,
            "m2_yoy_pct": None,
        }
        macro_without = {"ten_y_yield_pct": 3.5}
        a = compute_des("MSFT", "Technology", macro_with, SAMPLE_WEIGHTS_EXPANDED)
        b = compute_des("MSFT", "Technology", macro_without, SAMPLE_WEIGHTS_EXPANDED)
        assert a["sub_score"] == b["sub_score"]
        # And both should be ~50 since the only present signal is at neutral.
        assert a["sub_score"] == pytest.approx(50.0, abs=0.1)
        # The new signals' per-signal contributions must each be 0.
        for sig in ("real_10y_yield_pct", "vix_index", "m2_yoy_pct"):
            assert a["components"]["signal_contributions"][sig] == 0.0

    def test_high_vix_pushes_financials_down(self):
        # Financials has vix sensitivity -0.05. With VIX=40 (tilt +1),
        # contribution = -0.05. Other signals neutral. Sub-score should
        # drop slightly below 50.
        macro = {"vix_index": 40.0}
        result = compute_des("JPM", "Financials", macro, SAMPLE_WEIGHTS_EXPANDED)
        assert result["sub_score"] < 50.0
        assert (
            result["components"]["signal_contributions"]["vix_index"]
            == pytest.approx(-0.05, abs=1e-6)
        )

    def test_m2_expansion_lifts_tech(self):
        # M2 at 12% YoY -> tilt +1. Tech sensitivity +0.15 -> contribution +0.15.
        macro = {"m2_yoy_pct": 12.0}
        result = compute_des("MSFT", "Technology", macro, SAMPLE_WEIGHTS_EXPANDED)
        assert result["sub_score"] > 50.0
        assert (
            result["components"]["signal_contributions"]["m2_yoy_pct"]
            == pytest.approx(0.15, abs=1e-6)
        )

    # --- Live-config (real sector_des_weights.json) assertions ---

    def test_real_config_has_new_normalization_entries(self):
        real = load_sector_weights()
        norms = real.get("signal_normalization", {})
        for sig in ("real_10y_yield_pct", "vix_index", "m2_yoy_pct"):
            assert sig in norms, sig
            b = norms[sig]
            assert "low" in b and "high" in b and "neutral" in b
            assert b["low"] < b["high"]

    def test_real_config_tech_real_estate_sensitivities(self):
        real = load_sector_weights()
        tech = real["sectors"]["Information Technology"]
        re_ = real["sectors"]["Real Estate"]
        # Real Estate must be more rate-sensitive than Tech on real yields.
        assert re_["real_10y_yield_pct"] < tech["real_10y_yield_pct"] <= 0
        # Both have negative VIX sensitivity.
        assert tech["vix_index"] < 0
        # Both benefit from M2 expansion.
        assert tech["m2_yoy_pct"] > 0
        assert re_["m2_yoy_pct"] > 0
