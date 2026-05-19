"""Phase 2 Thesis-pillar wiring tests for the crypto LTHCS pillar.

These tests pin down the wiring contract between the
``CryptoDataAdapter`` (which surfaces funding-rate + L/S ratio from
``fetch_market.py`` via ``data/market.json``) and the
``compute_crypto_thesis`` pillar function.

The math defined in ``crypto_thesis.py`` (normalcy / symmetric mapping)
is already covered by ``test_crypto_pillars.py``. The cases here focus
on:

* directional polarity diagnostic (signed -100..+100 trend signal)
* graceful degradation when the adapter has no perp data
* Phase 2 inputs (``funding_rate_30d_mean_pct_8h``,
  ``long_short_ratio_30d_mean``) flowing through to ``variable_detail``
* end-to-end shape of an adapter-driven call (mocked, no HTTP).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from lthcs.pillars.crypto_thesis import compute_crypto_thesis
from lthcs.sources.crypto_data import (
    CryptoDataAdapter,
    funding_rate_metrics,
    load_market_payload,
    long_short_ratio_metrics,
)


# ---------------------------------------------------------------------------
# Pillar: polarity diagnostic + extreme behaviour
# ---------------------------------------------------------------------------


class TestCryptoThesisPolarity:
    def test_positive_funding_is_bearish_polarity(self) -> None:
        # Crowded longs paying carry -> bearish lean. Both score (low,
        # because |r| is at the extreme) and polarity (negative).
        out = compute_crypto_thesis(
            "BTC", {"funding_rate_pct_8h": 0.05, "long_short_ratio": 1.0}
        )
        vd = out["variable_detail"]
        assert vd["funding_polarity"] is not None
        assert vd["funding_polarity"] < 0.0
        # Spec extreme threshold is 0.10% -- at 0.05% polarity sits
        # roughly at the midpoint of the linear ramp (~-44).
        assert -60.0 < vd["funding_polarity"] < -30.0

    def test_negative_funding_is_bullish_polarity(self) -> None:
        # Panic shorts paying carry -> bullish capitulation lean.
        out = compute_crypto_thesis(
            "BTC", {"funding_rate_pct_8h": -0.05, "long_short_ratio": 1.0}
        )
        vd = out["variable_detail"]
        assert vd["funding_polarity"] is not None
        assert vd["funding_polarity"] > 0.0
        assert 30.0 < vd["funding_polarity"] < 60.0

    def test_extreme_positive_funding_saturates_bearish(self) -> None:
        # |r| at +0.10% -> -100 polarity (saturated bearish).
        out = compute_crypto_thesis(
            "BTC", {"funding_rate_pct_8h": 0.10, "long_short_ratio": 1.0}
        )
        assert out["variable_detail"]["funding_polarity"] == pytest.approx(-100.0)
        # Sub-score at the extreme: funding -> 0, L/S -> 100. The
        # pillar penalty pulls sub_score below the healthy band.
        assert out["sub_score"] < 50.0

    def test_extreme_negative_funding_saturates_bullish(self) -> None:
        out = compute_crypto_thesis(
            "BTC", {"funding_rate_pct_8h": -0.10, "long_short_ratio": 1.0}
        )
        assert out["variable_detail"]["funding_polarity"] == pytest.approx(100.0)

    def test_healthy_funding_polarity_is_zero(self) -> None:
        out = compute_crypto_thesis(
            "BTC", {"funding_rate_pct_8h": 0.005, "long_short_ratio": 1.0}
        )
        assert out["variable_detail"]["funding_polarity"] == pytest.approx(0.0)

    def test_long_short_polarity_crowded_long(self) -> None:
        out = compute_crypto_thesis(
            "BTC", {"funding_rate_pct_8h": 0.0, "long_short_ratio": 1.5}
        )
        # Crowded long -> bearish polarity.
        assert out["variable_detail"]["ls_polarity"] is not None
        assert out["variable_detail"]["ls_polarity"] < 0.0

    def test_long_short_polarity_crowded_short(self) -> None:
        out = compute_crypto_thesis(
            "BTC", {"funding_rate_pct_8h": 0.0, "long_short_ratio": 0.7}
        )
        # Crowded short -> bullish polarity.
        assert out["variable_detail"]["ls_polarity"] is not None
        assert out["variable_detail"]["ls_polarity"] > 0.0

    def test_polarity_none_when_input_missing(self) -> None:
        out = compute_crypto_thesis("ETH", {})
        assert out["variable_detail"]["funding_polarity"] is None
        assert out["variable_detail"]["ls_polarity"] is None


# ---------------------------------------------------------------------------
# Pillar: 30d-mean trend context flows through to variable_detail
# ---------------------------------------------------------------------------


class TestCryptoThesisTrendContext:
    def test_30d_means_surface_in_variable_detail(self) -> None:
        out = compute_crypto_thesis(
            "BTC",
            {
                "funding_rate_pct_8h": 0.0,
                "funding_rate_30d_mean_pct_8h": 0.012,
                "long_short_ratio": 1.0,
                "long_short_ratio_30d_mean": 1.05,
            },
        )
        vd = out["variable_detail"]
        assert vd["funding_rate_30d_mean_pct_8h"] == pytest.approx(0.012)
        assert vd["long_short_ratio_30d_mean"] == pytest.approx(1.05)
        # Trend context is diagnostic only; it must NOT pull the
        # sub-score away from the healthy-band score driven by the
        # latest values.
        assert out["sub_score"] == pytest.approx(100.0)

    def test_invalid_30d_mean_coerces_to_none(self) -> None:
        out = compute_crypto_thesis(
            "BTC",
            {
                "funding_rate_pct_8h": 0.0,
                "funding_rate_30d_mean_pct_8h": "garbage",
                "long_short_ratio": 1.0,
                "long_short_ratio_30d_mean": -0.5,  # invalid ratio
            },
        )
        vd = out["variable_detail"]
        assert vd["funding_rate_30d_mean_pct_8h"] is None
        assert vd["long_short_ratio_30d_mean"] is None


# ---------------------------------------------------------------------------
# Adapter: funding_rate_metrics + long_short_ratio_metrics
# ---------------------------------------------------------------------------


def _mk_market_payload(
    btc_funding: List[float] | None = None,
    btc_ls: List[float] | None = None,
    eth_funding: List[float] | None = None,
) -> Dict[str, Any]:
    """Build a minimal market.json payload for tests.

    Funding rates are passed as **decimals** to match the
    ``fetch_market.py:okx_funding`` contract (the adapter scales to %).
    """
    payload: Dict[str, Any] = {}
    if btc_funding is not None:
        payload.setdefault("btc", {})["funding"] = [
            {"date": "d%d" % i, "rate": r} for i, r in enumerate(btc_funding)
        ]
    if btc_ls is not None:
        payload.setdefault("btc", {})["long_short_ratio"] = [
            {"date": "d%d" % i, "ratio": r} for i, r in enumerate(btc_ls)
        ]
    if eth_funding is not None:
        payload.setdefault("eth", {})["funding"] = [
            {"date": "d%d" % i, "rate": r} for i, r in enumerate(eth_funding)
        ]
    return payload


class TestFundingRateMetrics:
    def test_latest_pct_converts_decimal_to_percent(self) -> None:
        # 0.0005 / 8h = 0.05% / 8h.
        payload = _mk_market_payload(btc_funding=[0.0005])
        out = funding_rate_metrics(payload, "BTC")
        assert out["latest_pct_8h"] == pytest.approx(0.05)
        assert out["mean_30d_pct_8h"] == pytest.approx(0.05)

    def test_mean_30d_uses_trailing_window(self) -> None:
        # 35 entries: last 30 = 0.001 each. First 5 should be ignored.
        rates = [0.01] * 5 + [0.0001] * 30
        payload = _mk_market_payload(btc_funding=rates)
        out = funding_rate_metrics(payload, "BTC")
        assert out["latest_pct_8h"] == pytest.approx(0.01)
        # Mean across last 30 only -> 0.01% per 8h.
        assert out["mean_30d_pct_8h"] == pytest.approx(0.01)

    def test_empty_series_returns_none(self) -> None:
        payload = _mk_market_payload(btc_funding=[])
        out = funding_rate_metrics(payload, "BTC")
        assert out == {"latest_pct_8h": None, "mean_30d_pct_8h": None}

    def test_missing_block_returns_none(self) -> None:
        out = funding_rate_metrics({}, "BTC")
        assert out == {"latest_pct_8h": None, "mean_30d_pct_8h": None}

    def test_unsupported_symbol_returns_none(self) -> None:
        # SOL has no OKX coverage in V1.
        payload = _mk_market_payload(btc_funding=[0.0001])
        out = funding_rate_metrics(payload, "SOL")
        assert out == {"latest_pct_8h": None, "mean_30d_pct_8h": None}

    def test_malformed_rows_ignored(self) -> None:
        payload = {
            "btc": {
                "funding": [
                    {"date": "a", "rate": "bad"},
                    {"date": "b"},  # missing rate
                    None,  # bad row
                    {"date": "c", "rate": 0.0001},
                ]
            }
        }
        out = funding_rate_metrics(payload, "BTC")
        assert out["latest_pct_8h"] == pytest.approx(0.01)


class TestLongShortRatioMetrics:
    def test_latest_and_mean(self) -> None:
        payload = _mk_market_payload(btc_ls=[0.9, 1.0, 1.1])
        out = long_short_ratio_metrics(payload, "BTC")
        assert out["latest"] == pytest.approx(1.1)
        assert out["mean_30d"] == pytest.approx(1.0)

    def test_non_positive_ratios_dropped(self) -> None:
        payload = _mk_market_payload(btc_ls=[-1.0, 0.0, 1.05])
        out = long_short_ratio_metrics(payload, "BTC")
        assert out["latest"] == pytest.approx(1.05)
        assert out["mean_30d"] == pytest.approx(1.05)

    def test_all_bad_returns_none(self) -> None:
        payload = _mk_market_payload(btc_ls=[-1.0, 0.0])
        out = long_short_ratio_metrics(payload, "BTC")
        assert out == {"latest": None, "mean_30d": None}

    def test_unsupported_symbol_returns_none(self) -> None:
        payload = _mk_market_payload(btc_ls=[1.0])
        out = long_short_ratio_metrics(payload, "SOL")
        assert out == {"latest": None, "mean_30d": None}


# ---------------------------------------------------------------------------
# Adapter: market_payload loader + inputs_for() wiring
# ---------------------------------------------------------------------------


class TestAdapterMarketWiring:
    def test_load_market_payload_missing_returns_empty(self, tmp_path: Path) -> None:
        # No file under tmp_path -> empty dict.
        out = load_market_payload(tmp_path / "market.json")
        assert out == {}

    def test_load_market_payload_reads_valid(self, tmp_path: Path) -> None:
        path = tmp_path / "market.json"
        path.write_text(json.dumps({"btc": {"funding": [{"date": "d", "rate": 0.0001}]}}))
        out = load_market_payload(path)
        assert out["btc"]["funding"][0]["rate"] == pytest.approx(0.0001)

    def test_inputs_for_btc_wires_funding_and_ls(self, tmp_path: Path) -> None:
        # Write a market.json under tmp_path so the adapter picks it up.
        market = _mk_market_payload(
            btc_funding=[0.0001] * 30,  # 0.01% per 8h
            btc_ls=[1.0] * 30,
        )
        (tmp_path / "market.json").write_text(json.dumps(market))

        adapter = CryptoDataAdapter(data_dir=tmp_path, offline=True)
        inp = adapter.inputs_for("BTC")
        # Keys present and populated.
        assert inp["funding_rate_pct_8h"] == pytest.approx(0.01)
        assert inp["funding_rate_30d_mean_pct_8h"] == pytest.approx(0.01)
        assert inp["long_short_ratio"] == pytest.approx(1.0)
        assert inp["long_short_ratio_30d_mean"] == pytest.approx(1.0)

    def test_inputs_for_sol_funding_is_none(self, tmp_path: Path) -> None:
        # SOL has no perp coverage in V1 -> adapter returns None and the
        # Thesis pillar degrades gracefully.
        market = _mk_market_payload(btc_funding=[0.0001] * 5)
        (tmp_path / "market.json").write_text(json.dumps(market))

        adapter = CryptoDataAdapter(data_dir=tmp_path, offline=True)
        inp = adapter.inputs_for("SOL")
        assert inp["funding_rate_pct_8h"] is None
        assert inp["long_short_ratio"] is None
        # Pillar collapses to neutral when no inputs are present.
        out = compute_crypto_thesis("SOL", inp)
        assert out["sub_score"] == 50.0

    def test_inputs_for_no_market_file_is_none(self, tmp_path: Path) -> None:
        # market.json absent -> adapter returns None for perp fields.
        adapter = CryptoDataAdapter(data_dir=tmp_path, offline=True)
        inp = adapter.inputs_for("BTC")
        assert inp["funding_rate_pct_8h"] is None
        assert inp["long_short_ratio"] is None

    def test_inputs_for_drives_pillar_end_to_end(self, tmp_path: Path) -> None:
        # Extreme positive funding (over-leveraged longs) + crowded L/S.
        market = _mk_market_payload(
            btc_funding=[0.001] * 30,  # 0.1% per 8h -> extreme
            btc_ls=[2.0] * 30,         # >= 1.8 extreme
        )
        (tmp_path / "market.json").write_text(json.dumps(market))

        adapter = CryptoDataAdapter(data_dir=tmp_path, offline=True)
        inp = adapter.inputs_for("BTC")
        out = compute_crypto_thesis("BTC", inp)
        # Both inputs at the extreme -> funding score 0, L/S score 0,
        # narrative absent -> aggregate 0.0.
        assert out["sub_score"] == 0.0
        # Polarity remains diagnostic and is signed (-100 = bearish).
        assert out["variable_detail"]["funding_polarity"] == pytest.approx(-100.0)
        assert out["variable_detail"]["ls_polarity"] == pytest.approx(-100.0)
