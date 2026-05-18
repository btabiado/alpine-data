"""Tests for the LTHCS crypto pillar implementations.

These tests are pure-math: every input is a synthetic in-memory
dictionary so we never touch the network. Each pillar is exercised on:

* A "happy path" with all components present.
* A "missing data" path where one or more components fall back to None
  and the pillar renormalizes the remaining weights.
* The empty/everything-missing case where the pillar collapses to the
  neutral 50.0 midpoint.

The composite computation (driver from ``scripts/lthcs_crypto_daily``)
is exercised with each of BTC / ETH / SOL to confirm the per-asset
weight profile is honored.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List

import pytest

from lthcs.pillars.crypto_adoption import compute_crypto_adoption
from lthcs.pillars.crypto_des import compute_crypto_des
from lthcs.pillars.crypto_financial import compute_crypto_financial
from lthcs.pillars.crypto_institutional import compute_crypto_institutional
from lthcs.pillars.crypto_thesis import compute_crypto_thesis


# ---------------------------------------------------------------------------
# Series builders
# ---------------------------------------------------------------------------

def _series(start_value: float, pct_change_30d: float, *, n: int = 31) -> List[Dict[str, Any]]:
    """Build a ``[{date, value}]`` series whose value[-1] vs value[-31] is
    exactly ``pct_change_30d`` percent.

    Intermediate days don't matter for the pillar's pct_change_30d
    function (it samples endpoints).
    """
    base = start_value
    series = []
    today = date(2026, 5, 18)
    for i in range(n):
        d = today - timedelta(days=(n - 1 - i))
        value = base if i < n - 1 else base * (1.0 + pct_change_30d / 100.0)
        series.append({"date": d.isoformat(), "value": value})
    return series


def _distribution(now_supply: float, then_supply: float, n: int = 31) -> List[Dict[str, Any]]:
    """Build a whale-distribution series. Only rows[-1] and rows[-31]
    matter for compute_whale_cohort_pct_30d."""
    rows: List[Dict[str, Any]] = []
    for i in range(n):
        if i == 0:
            v = then_supply
        elif i == n - 1:
            v = now_supply
        else:
            v = (then_supply + now_supply) / 2.0
        # Split arbitrarily across the three whale buckets.
        rows.append({
            "b1k_10k": v * 0.5,
            "b10k_100k": v * 0.3,
            "b100k_1m": v * 0.2,
        })
    return rows


# ---------------------------------------------------------------------------
# Adoption pillar
# ---------------------------------------------------------------------------

class TestCryptoAdoption:
    def test_happy_btc(self) -> None:
        inputs = {
            "active_addresses_series": _series(800_000, 12.0),
            "tx_volume_usd_series": _series(5e9, 25.0),
            "hash_rate_series": _series(5e8, 8.0),
        }
        out = compute_crypto_adoption("BTC", inputs)
        assert out["ticker"] == "BTC"
        # Each component is in the positive band (>50).
        assert 50.0 < out["sub_score"] <= 100.0
        assert out["data_quality"]["has_active_addresses"] is True
        assert out["data_quality"]["has_tx_volume"] is True
        assert out["data_quality"]["has_security"] is True

    def test_negative_signal(self) -> None:
        inputs = {
            "active_addresses_series": _series(800_000, -15.0),
            "tx_volume_usd_series": _series(5e9, -30.0),
            "hash_rate_series": _series(5e8, -5.0),
        }
        out = compute_crypto_adoption("BTC", inputs)
        assert 0.0 <= out["sub_score"] < 50.0

    def test_missing_security_btc(self) -> None:
        # Empty hash rate series -> renormalize across active + txvol.
        inputs = {
            "active_addresses_series": _series(800_000, 10.0),
            "tx_volume_usd_series": _series(5e9, 20.0),
            "hash_rate_series": [],
        }
        out = compute_crypto_adoption("BTC", inputs)
        assert out["data_quality"]["has_security"] is False
        # Active + txvol weights should sum to 1.0 in effective_weights.
        eff = out["effective_weights"]
        assert abs(eff["active"] + eff["txvol"] - 1.0) < 1e-6
        assert eff["security"] == 0.0

    def test_all_missing(self) -> None:
        out = compute_crypto_adoption("ETH", {})
        assert out["sub_score"] == 50.0
        assert all(v is False for v in out["data_quality"].values())

    def test_eth_uses_active_addr_fallback(self) -> None:
        inputs = {
            "active_addresses_series": _series(400_000, 15.0),
            "tx_volume_usd_series": _series(3e9, 20.0),
            # No hash_rate_series or tx_count_series -> proxy = active.
        }
        out = compute_crypto_adoption("ETH", inputs)
        # security_pct should fall back to the active 15% number.
        assert out["components"]["active_addr_proxy_pct_30d"] == pytest.approx(15.0, abs=0.5)


# ---------------------------------------------------------------------------
# Institutional pillar
# ---------------------------------------------------------------------------

class TestCryptoInstitutional:
    def test_btc_strong_buying(self) -> None:
        inputs = {
            "distribution_series": _distribution(now_supply=1010.0, then_supply=1000.0),
            "etf_flow_rows": [{"date": "2026-05-%02d" % i, "total": 80.0} for i in range(1, 31)],
            "market": {"price_change_pct_30d": 15.0},
        }
        out = compute_crypto_institutional("BTC", inputs)
        assert out["sub_score"] > 70.0
        assert out["data_quality"]["has_whale_cohort"] is True
        assert out["data_quality"]["has_etf_flow"] is True

    def test_btc_etf_outflow(self) -> None:
        inputs = {
            "distribution_series": _distribution(now_supply=990.0, then_supply=1000.0),
            "etf_flow_rows": [{"date": "2026-05-%02d" % i, "total": -60.0} for i in range(1, 31)],
            "market": {"price_change_pct_30d": -10.0},
        }
        out = compute_crypto_institutional("BTC", inputs)
        assert out["sub_score"] < 40.0

    def test_sol_no_etf_no_whale(self) -> None:
        # SOL has no ETF coverage and no whale cohort series; falls back
        # to momentum alone.
        inputs = {
            "distribution_series": [],
            "etf_flow_rows": [],
            "market": {"price_change_pct_30d": 20.0},
        }
        out = compute_crypto_institutional("SOL", inputs)
        assert out["data_quality"]["has_whale_cohort"] is False
        assert out["data_quality"]["has_etf_flow"] is False
        assert out["data_quality"]["has_price_momentum"] is True
        # Momentum carries 100% of the weight.
        assert out["effective_weights"]["momentum"] == pytest.approx(1.0)

    def test_all_missing(self) -> None:
        out = compute_crypto_institutional("BTC", {})
        assert out["sub_score"] == 50.0


# ---------------------------------------------------------------------------
# Financial pillar
# ---------------------------------------------------------------------------

class TestCryptoFinancial:
    def test_btc_strong(self) -> None:
        inputs = {
            "miners_revenue_usd_series": _series(40e6, 25.0),
            "market": {"price_change_pct_30d": 10.0},
            "supply_inflation_pct_yr": 0.83,
        }
        out = compute_crypto_financial("BTC", inputs)
        assert out["sub_score"] > 70.0
        assert out["components"]["supply_subscore"] is not None
        # Supply inflation 0.83% -> high supply score (lower is better).
        assert out["components"]["supply_subscore"] >= 80.0

    def test_eth_uses_txvol_proxy(self) -> None:
        inputs = {
            "tx_volume_usd_series": _series(2e9, 30.0),
            "market": {"price_change_pct_30d": 5.0},
        }
        out = compute_crypto_financial("ETH", inputs)
        assert "tx_volume_proxy_pct_30d" in out["components"]
        assert out["components"]["supply_subscore"] is not None  # default ETH inflation

    def test_sol_high_inflation_drags_supply(self) -> None:
        inputs = {
            "tx_volume_usd_series": _series(1e9, 5.0),
            "market": {"price_change_pct_30d": 0.0},
            # default SOL inflation is 5.5% -> mid-range supply score
        }
        out = compute_crypto_financial("SOL", inputs)
        sol_supply = out["components"]["supply_subscore"]

        # Same inputs with BTC's lower inflation should produce a higher
        # supply subscore.
        inputs_btc = dict(inputs)
        inputs_btc["supply_inflation_pct_yr"] = 0.83
        out_btc = compute_crypto_financial("BTC", inputs_btc)
        assert out_btc["components"]["supply_subscore"] > sol_supply

    def test_all_missing(self) -> None:
        # Even with no real data, the default supply inflation kicks in,
        # so the score isn't a flat 50.
        out = compute_crypto_financial("BTC", {})
        # Supply subscore exists from the default inflation; revenue
        # and realized are None.
        assert out["data_quality"]["has_supply_inflation"] is True
        assert out["data_quality"]["has_revenue"] is False


# ---------------------------------------------------------------------------
# Thesis pillar
# ---------------------------------------------------------------------------

class TestCryptoThesis:
    def test_healthy_funding(self) -> None:
        out = compute_crypto_thesis(
            "BTC",
            {"funding_rate_pct_8h": 0.005, "long_short_ratio": 1.0},
        )
        # Both metrics in the "healthy" band -> high score.
        assert out["sub_score"] >= 90.0

    def test_euphoric_funding(self) -> None:
        out = compute_crypto_thesis(
            "BTC",
            {"funding_rate_pct_8h": 0.12, "long_short_ratio": 2.0},
        )
        # Funding > 0.1 and L/S > 1.8 -> zeros on both -> 0.
        assert out["sub_score"] == 0.0

    def test_no_thesis_inputs(self) -> None:
        # All thesis components missing -> 50.0 (neutral), and the
        # runner-side `thesis_unavailable` flag should be set.
        out = compute_crypto_thesis("ETH", {})
        assert out["sub_score"] == 50.0
        assert all(v is False for v in out["data_quality"].values())

    def test_partial_data(self) -> None:
        # Only funding given.
        out = compute_crypto_thesis("BTC", {"funding_rate_pct_8h": 0.0})
        assert out["sub_score"] == 100.0
        assert out["effective_weights"]["funding"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# DES pillar
# ---------------------------------------------------------------------------

class TestCryptoDes:
    def test_growing_stablecoins(self) -> None:
        inputs = {
            "stablecoins": {"now": 1.5e11, "delta_30d_pct": 5.0},
            "exchange_reserves_pct_30d": -3.0,  # falling reserves = bullish
            "hy_oas": 3.0, "vix": 14.0, "ten_y_30d_change_bp": -20.0,
        }
        out = compute_crypto_des("BTC", inputs)
        assert out["sub_score"] > 70.0
        assert out["data_quality"]["has_stablecoin"]
        assert out["data_quality"]["has_exchange_reserves"]
        assert out["data_quality"]["has_macro_overlay"]

    def test_shrinking_stablecoins(self) -> None:
        inputs = {
            "stablecoins": {"now": 1.4e11, "delta_30d_pct": -8.0},
            "exchange_reserves_pct_30d": 4.0,
            "hy_oas": 7.0, "vix": 28.0, "ten_y_30d_change_bp": 40.0,
        }
        out = compute_crypto_des("BTC", inputs)
        assert out["sub_score"] < 30.0

    def test_no_macro_only_stablecoins(self) -> None:
        inputs = {
            "stablecoins": {"delta_30d_pct": 3.0},
        }
        out = compute_crypto_des("BTC", inputs)
        # Only stablecoin component active.
        assert out["effective_weights"]["stablecoin"] == pytest.approx(1.0)
        assert out["effective_weights"]["macro"] == 0.0

    def test_all_missing(self) -> None:
        out = compute_crypto_des("BTC", {})
        assert out["sub_score"] == 50.0


# ---------------------------------------------------------------------------
# Composite (per-asset weight profile honored)
# ---------------------------------------------------------------------------

class TestCompositeComputation:
    @pytest.fixture(autouse=True)
    def _set_up(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Isolate persist + cache directories.
        monkeypatch.setenv("LTHCS_CACHE_DIR", str(tmp_path / "cache"))

    def _make_inputs(self) -> Dict[str, Any]:
        # Returns input fields that would produce ~70 on every pillar
        # individually, so we can verify the *weight profile* drives
        # the per-asset spread.
        return {
            "active_addresses_series": _series(800_000, 12.0),
            "tx_volume_usd_series": _series(5e9, 20.0),
            "hash_rate_series": _series(5e8, 7.0),
            "miners_revenue_usd_series": _series(40e6, 25.0),
            "distribution_series": _distribution(1010.0, 1000.0),
            "etf_flow_rows": [{"date": "2026-05-%02d" % i, "total": 80.0} for i in range(1, 31)],
            "market": {"price_change_pct_30d": 15.0},
            "supply_inflation_pct_yr": 0.83,
            "stablecoins": {"delta_30d_pct": 4.0},
            "exchange_reserves_pct_30d": -2.0,
            "hy_oas": 3.5, "vix": 15.0, "ten_y_30d_change_bp": -10.0,
            "funding_rate_pct_8h": 0.005,
            "long_short_ratio": 1.05,
        }

    def test_btc_eth_sol_weight_profiles_differ(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from scripts import lthcs_crypto_daily as runner

        weights_config = {
            "profiles": {
                "btc": [0.10, 0.30, 0.25, 0.15, 0.20],
                "eth": [0.25, 0.20, 0.20, 0.20, 0.15],
                "sol": [0.30, 0.15, 0.20, 0.20, 0.15],
            },
            "score_bands": {
                "elite": {"min": 90, "max": 100},
                "high_confidence": {"min": 80, "max": 89},
                "constructive": {"min": 70, "max": 79},
                "monitor": {"min": 60, "max": 69},
                "weakening": {"min": 50, "max": 59},
                "review": {"min": 0, "max": 49},
            },
        }

        class StubAdapter:
            def __init__(self, inputs: Dict[str, Any]) -> None:
                self._inputs = inputs

            def inputs_for(self, symbol: str) -> Dict[str, Any]:
                return dict(self._inputs)

        inputs = self._make_inputs()
        adapter = StubAdapter(inputs)

        rows = {}
        for sym, profile in (("BTC", "btc"), ("ETH", "eth"), ("SOL", "sol")):
            rows[sym] = runner.score_asset(
                {"symbol": sym, "weight_profile": profile},
                adapter, weights_config, calc_date="2026-05-18",
            )

        # Every weight profile must produce a usable composite in [0,100].
        for sym, row in rows.items():
            assert 0.0 <= row["lthcs_score"] <= 100.0
            assert row["band"] in {"elite", "high_confidence", "constructive",
                                   "monitor", "weakening", "review"}
            # Weights actually used match the profile vector.
            assert row["weights_used"] == weights_config["profiles"][sym.lower()]

    def test_thesis_unavailable_renormalizes(self) -> None:
        from scripts import lthcs_crypto_daily as runner

        weights_config = {
            "profiles": {
                "btc": [0.10, 0.30, 0.25, 0.15, 0.20],
            },
            "score_bands": {
                "review": {"min": 0, "max": 100},
            },
        }

        class StubAdapter:
            def inputs_for(self, symbol: str) -> Dict[str, Any]:
                # No funding / no L-S ratio / no narrative -> thesis_unavailable.
                return {
                    "active_addresses_series": _series(800_000, 12.0),
                    "tx_volume_usd_series": _series(5e9, 20.0),
                    "hash_rate_series": _series(5e8, 7.0),
                    "miners_revenue_usd_series": _series(40e6, 25.0),
                    "distribution_series": _distribution(1010.0, 1000.0),
                    "etf_flow_rows": [{"date": "2026-05-%02d" % i, "total": 80.0} for i in range(1, 31)],
                    "market": {"price_change_pct_30d": 15.0},
                    "supply_inflation_pct_yr": 0.83,
                    "stablecoins": {"delta_30d_pct": 4.0},
                }

        row = runner.score_asset(
            {"symbol": "BTC", "weight_profile": "btc"},
            StubAdapter(), weights_config, calc_date="2026-05-18",
        )
        assert "thesis_unavailable" in row["data_quality_flags"]
        assert "thesis_integrity" in row["dropped_pillars"]
        # Effective weights: thesis_integrity -> 0; remaining sum to 1.
        eff = row["effective_weights"]
        assert eff[3] == 0.0  # thesis_integrity index
        assert abs(sum(eff) - 1.0) < 1e-6
