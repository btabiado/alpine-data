"""Tests for the LTHCS daily pipeline runner (``lthcs_daily.py``).

All upstream source clients and the persistence layer are mocked. No
test touches the network, and no test writes to disk outside of
``tmp_path``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

import lthcs_daily


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_UNIVERSE_FIXTURE = {
    "version": "test",
    "tickers": [
        {
            "ticker": "AAPL",
            "name": "Apple Inc.",
            "sector": "Technology",
            "industry": "Consumer Electronics",
            "maturity_stage": "standard_compounder",
            "active": True,
        },
        {
            "ticker": "LCID",
            "name": "Lucid Group",
            "sector": "Consumer Discretionary",
            "industry": "Auto Manufacturers",
            "maturity_stage": "pre_profit_growth",
            "active": True,
        },
        {
            "ticker": "DEAD",
            "name": "Inactive Co",
            "sector": "Technology",
            "industry": "Software",
            "maturity_stage": "standard_compounder",
            "active": False,
        },
    ],
}

_WEIGHTS_FIXTURE = {
    "version": "test",
    "profiles": {
        "standard_compounder": [0.25, 0.20, 0.15, 0.20, 0.20],
        "pre_profit_growth": [0.30, 0.20, 0.15, 0.20, 0.15],
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

_SECTOR_WEIGHTS_FIXTURE = {
    "magnitude_scale": 30.0,
    "signal_normalization": {
        "fed_funds_pct": {"low": 0.0, "high": 6.0},
        "ten_y_yield_pct": {"low": 1.0, "high": 5.0},
    },
    "sectors": {
        "Technology": {"fed_funds_pct": -0.4, "ten_y_yield_pct": -0.3},
        "Consumer Discretionary": {"fed_funds_pct": -0.2, "ten_y_yield_pct": -0.2},
    },
    "ticker_overrides": {},
}


@pytest.fixture
def patched_configs(monkeypatch, tmp_path):
    """Redirect the config-file paths so stage_1 reads our fixtures."""
    universe_path = tmp_path / "universe.json"
    weights_path = tmp_path / "weights.json"
    sector_path = tmp_path / "sector_des_weights.json"
    universe_path.write_text(json.dumps(_UNIVERSE_FIXTURE))
    weights_path.write_text(json.dumps(_WEIGHTS_FIXTURE))
    sector_path.write_text(json.dumps(_SECTOR_WEIGHTS_FIXTURE))
    monkeypatch.setattr(lthcs_daily, "UNIVERSE_PATH", universe_path)
    monkeypatch.setattr(lthcs_daily, "WEIGHTS_PATH", weights_path)
    monkeypatch.setattr(lthcs_daily, "SECTOR_WEIGHTS_PATH", sector_path)
    return universe_path, weights_path, sector_path


@pytest.fixture
def fake_persist():
    """A MagicMock standing in for an :class:`LthcsPersist` instance.

    ``snapshot_exists`` defaults to False so the happy-path test doesn't
    trip the collision guard.
    """
    persist = MagicMock(name="LthcsPersist")
    persist.snapshot_exists.return_value = False
    persist.write_snapshot.return_value = Path("/tmp/snapshot.json")
    persist.write_variable_detail.return_value = Path("/tmp/var.json")
    persist.write_narratives.return_value = Path("/tmp/narr.json")
    persist.rebuild_history_for_all_tickers.return_value = 0
    persist.rebuild_index.return_value = Path("/tmp/index.json")
    return persist


@pytest.fixture
def patched_persist_class(monkeypatch, fake_persist):
    """Make ``LthcsPersist()`` return our fake regardless of constructor args."""
    monkeypatch.setattr(
        lthcs_daily, "LthcsPersist", MagicMock(return_value=fake_persist)
    )
    return fake_persist


@pytest.fixture
def patched_sources(monkeypatch):
    """Replace every upstream source call with a deterministic stub.

    Returns a dict of MagicMocks so individual tests can introspect the
    call counts (especially the AV one).
    """
    yahoo_prices_mock = MagicMock(return_value=[{"date": "2026-05-15", "close": 100.0}])
    yahoo_momentum_mock = MagicMock(return_value=0.08)
    yahoo_vol_mock = MagicMock(return_value=0.25)
    sec_rev_mock = MagicMock(
        return_value=[
            {"end_date": "2024-09-30", "start_date": "2023-10-01",
             "value": 100.0, "form": "10-K", "fp": "FY"},
            {"end_date": "2023-09-30", "start_date": "2022-10-01",
             "value": 92.0, "form": "10-K", "fp": "FY"},
        ]
    )
    sec_gp_mock = MagicMock(return_value=[])
    sec_ocf_mock = MagicMock(return_value=[])
    fred_series_mock = MagicMock(
        return_value=[
            {"date": "2026-04-15", "value": 4.3},
            {"date": "2026-05-15", "value": 4.4},
        ]
    )
    fred_latest_mock = MagicMock(return_value={"date": "2026-05-01", "value": 4.5})
    eia_latest_mock = MagicMock(return_value={"date": "2026-05-15", "value": 75.0})
    av_mock = MagicMock(return_value={"items": "0", "feed": []})

    monkeypatch.setattr(lthcs_daily.yahoo, "get_daily_prices", yahoo_prices_mock)
    monkeypatch.setattr(lthcs_daily.yahoo, "get_momentum_pct", yahoo_momentum_mock)
    monkeypatch.setattr(lthcs_daily.yahoo, "get_volatility", yahoo_vol_mock)
    monkeypatch.setattr(lthcs_daily.sec_edgar, "get_revenue_history", sec_rev_mock)
    monkeypatch.setattr(
        lthcs_daily.sec_edgar, "get_gross_profit_history", sec_gp_mock
    )
    monkeypatch.setattr(
        lthcs_daily.sec_edgar, "get_operating_cash_flow_history", sec_ocf_mock
    )
    monkeypatch.setattr(lthcs_daily.fred, "get_series", fred_series_mock)
    monkeypatch.setattr(lthcs_daily.fred, "get_latest_value", fred_latest_mock)
    monkeypatch.setattr(lthcs_daily.eia, "get_latest_value", eia_latest_mock)
    monkeypatch.setattr(lthcs_daily.alpha_vantage, "get_news_sentiment", av_mock)

    return {
        "yahoo_prices": yahoo_prices_mock,
        "yahoo_momentum": yahoo_momentum_mock,
        "yahoo_vol": yahoo_vol_mock,
        "sec_rev": sec_rev_mock,
        "sec_gp": sec_gp_mock,
        "sec_ocf": sec_ocf_mock,
        "fred_series": fred_series_mock,
        "fred_latest": fred_latest_mock,
        "eia_latest": eia_latest_mock,
        "av": av_mock,
    }


@pytest.fixture
def base_state(patched_configs, patched_persist_class, patched_sources):
    """Run stages 1+2+3+4+5+6 to produce a ready-for-Stage-7/8 state."""
    args = lthcs_daily.parse_args(["--tickers", "AAPL,LCID"])
    state = lthcs_daily.PipelineState(args=args)
    assert lthcs_daily.stage_1_load_config(state)
    assert lthcs_daily.stage_2_fetch_data(state)
    assert lthcs_daily.stage_3_quality_checks(state)
    assert lthcs_daily.stage_4_compute_subscores(state)
    assert lthcs_daily.stage_5_apply_modifiers(state)
    assert lthcs_daily.stage_6_compute_final_scores(state)
    return state


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------

class TestParseArgs:
    def test_defaults(self):
        args = lthcs_daily.parse_args([])
        assert args.tickers is None
        assert args.dry_run is False
        assert args.force is False
        assert args.skip_thesis is False
        assert args.verbose is False

    def test_tickers_csv(self):
        args = lthcs_daily.parse_args(["--tickers", "AAPL,LCID"])
        assert args.tickers == "AAPL,LCID"

    def test_dry_run(self):
        args = lthcs_daily.parse_args(["--dry-run"])
        assert args.dry_run is True

    def test_force(self):
        args = lthcs_daily.parse_args(["--force"])
        assert args.force is True

    def test_skip_thesis(self):
        args = lthcs_daily.parse_args(["--skip-thesis"])
        assert args.skip_thesis is True

    def test_combined_flags(self):
        args = lthcs_daily.parse_args(["--dry-run", "--force", "--skip-thesis", "--verbose"])
        assert (args.dry_run, args.force, args.skip_thesis, args.verbose) == (True, True, True, True)


# ---------------------------------------------------------------------------
# Stage 1 -- config load
# ---------------------------------------------------------------------------

class TestStage1:
    def test_loads_active_tickers_and_filters_inactive(self, patched_configs):
        args = lthcs_daily.parse_args([])
        state = lthcs_daily.PipelineState(args=args)
        assert lthcs_daily.stage_1_load_config(state) is True
        # DEAD is filtered (active=False); AAPL + LCID remain.
        assert set(state.active_tickers) == {"AAPL", "LCID"}
        assert state.calc_date  # populated
        assert state.persist is not None
        assert "standard_compounder" in state.weights_config["profiles"]

    def test_ticker_subset_filters_to_universe(self, patched_configs):
        args = lthcs_daily.parse_args(["--tickers", "AAPL,UNKNOWN"])
        state = lthcs_daily.PipelineState(args=args)
        assert lthcs_daily.stage_1_load_config(state) is True
        assert state.active_tickers == ["AAPL"]


# ---------------------------------------------------------------------------
# Stage 2 -- fetch
# ---------------------------------------------------------------------------

class TestStage2:
    def test_fetches_for_each_ticker(self, patched_configs, patched_sources, tmp_path, monkeypatch):
        # Isolate rotation state to tmp_path so the test doesn't write to the
        # real data/lthcs/ directory.
        monkeypatch.setattr(
            "lthcs.sources.thesis_rotation.get_default_data_root",
            lambda: tmp_path,
        )
        args = lthcs_daily.parse_args(["--tickers", "AAPL,LCID"])
        state = lthcs_daily.PipelineState(args=args)
        lthcs_daily.stage_1_load_config(state)
        # Point persist at tmp_path too so rotation inherits the right root.
        state.persist = lthcs_daily.LthcsPersist(data_root=tmp_path)
        assert lthcs_daily.stage_2_fetch_data(state) is True

        assert patched_sources["yahoo_momentum"].call_count == 2
        assert patched_sources["sec_rev"].call_count == 2
        # Rotation: one AV call per ticker selected (both AAPL and LCID
        # are never-scored on a fresh tmp_path state, so both get picked).
        assert patched_sources["av"].call_count == 2
        assert state.rotation is not None
        assert set(state.rotation_scored_today) == {"AAPL", "LCID"}
        # Macro inputs populated.
        assert "ten_y_yield_pct" in state.macro_inputs

    def test_skip_thesis_avoids_av_call(self, patched_configs, patched_sources, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "lthcs.sources.thesis_rotation.get_default_data_root",
            lambda: tmp_path,
        )
        args = lthcs_daily.parse_args(["--tickers", "AAPL,LCID", "--skip-thesis"])
        state = lthcs_daily.PipelineState(args=args)
        lthcs_daily.stage_1_load_config(state)
        state.persist = lthcs_daily.LthcsPersist(data_root=tmp_path)
        assert lthcs_daily.stage_2_fetch_data(state) is True
        assert patched_sources["av"].call_count == 0
        assert state.rotation_scored_today == []

    def test_source_exception_does_not_abort(self, patched_configs, patched_sources):
        # If yfinance momentum blows up, the stage still returns True.
        patched_sources["yahoo_momentum"].side_effect = RuntimeError("yahoo down")
        args = lthcs_daily.parse_args(["--tickers", "AAPL"])
        state = lthcs_daily.PipelineState(args=args)
        lthcs_daily.stage_1_load_config(state)
        assert lthcs_daily.stage_2_fetch_data(state) is True
        assert state.momentum_by_ticker.get("AAPL") is None


# ---------------------------------------------------------------------------
# Stage 3 -- quality
# ---------------------------------------------------------------------------

class TestStage3:
    def test_marks_tickers_with_data_as_sufficient(
        self, patched_configs, patched_sources
    ):
        args = lthcs_daily.parse_args(["--tickers", "AAPL,LCID"])
        state = lthcs_daily.PipelineState(args=args)
        lthcs_daily.stage_1_load_config(state)
        lthcs_daily.stage_2_fetch_data(state)
        assert lthcs_daily.stage_3_quality_checks(state) is True
        assert set(state.scored_tickers) == {"AAPL", "LCID"}

    def test_ticker_without_yahoo_or_sec_is_dropped(
        self, patched_configs, patched_sources
    ):
        patched_sources["yahoo_momentum"].return_value = None
        patched_sources["sec_rev"].return_value = []
        args = lthcs_daily.parse_args(["--tickers", "AAPL"])
        state = lthcs_daily.PipelineState(args=args)
        lthcs_daily.stage_1_load_config(state)
        lthcs_daily.stage_2_fetch_data(state)
        lthcs_daily.stage_3_quality_checks(state)
        assert "AAPL" not in state.scored_tickers
        assert "yahoo_unavailable" in state.data_quality_flags["AAPL"]
        assert "sec_unavailable" in state.data_quality_flags["AAPL"]


# ---------------------------------------------------------------------------
# Stage 4 -- compute sub-scores
# ---------------------------------------------------------------------------

class TestStage4:
    def test_produces_five_pillar_results_per_ticker(
        self, patched_configs, patched_sources
    ):
        args = lthcs_daily.parse_args(["--tickers", "AAPL,LCID"])
        state = lthcs_daily.PipelineState(args=args)
        lthcs_daily.stage_1_load_config(state)
        lthcs_daily.stage_2_fetch_data(state)
        lthcs_daily.stage_3_quality_checks(state)
        assert lthcs_daily.stage_4_compute_subscores(state) is True
        for sym in state.scored_tickers:
            pillars = state.pillar_results[sym]
            assert set(pillars.keys()) == {
                "adoption_momentum",
                "institutional_confidence",
                "financial_evolution",
                "thesis_integrity",
                "des",
            }

    def test_thesis_uses_finnhub_when_av_skipped(
        self, patched_configs, patched_sources, monkeypatch
    ):
        """With --skip-thesis (AV bypassed), Finnhub recommendation consensus
        should still produce a real Thesis sub-score. Before the dead-pillar
        fix (May 2026) this test asserted neutral 50 across the board; that
        was the bug -- the cascade was fully gated by --skip-thesis even
        though Finnhub has historical analyst data with no AV-style archive
        limitation."""
        # Mock Finnhub to return a bullish consensus for both tickers.
        reco_history = [
            {"period": "2026-05-01", "strong_buy": 10, "buy": 15,
             "hold": 5, "sell": 0, "strong_sell": 0},
            {"period": "2026-04-01", "strong_buy": 8, "buy": 14,
             "hold": 7, "sell": 1, "strong_sell": 0},
        ]
        monkeypatch.setattr(
            lthcs_daily.finnhub,
            "get_recommendation_trends",
            MagicMock(return_value=reco_history),
        )
        args = lthcs_daily.parse_args(["--tickers", "AAPL,LCID", "--skip-thesis"])
        state = lthcs_daily.PipelineState(args=args)
        lthcs_daily.stage_1_load_config(state)
        lthcs_daily.stage_2_fetch_data(state)
        lthcs_daily.stage_3_quality_checks(state)
        lthcs_daily.stage_4_compute_subscores(state)
        for sym in state.scored_tickers:
            th = state.pillar_results[sym]["thesis_integrity"]
            # Bullish 25/30 buy-weighted consensus -> real signal, not 50.
            assert th["sub_score"] > 55.0
            assert th["data_quality"]["has_sentiment"] is True

    def test_thesis_falls_back_to_neutral_when_finnhub_keyless_and_skip_thesis(
        self, patched_configs, patched_sources, monkeypatch
    ):
        """No Finnhub key + --skip-thesis -> graceful neutral fallback."""
        monkeypatch.setattr(
            lthcs_daily.finnhub,
            "get_recommendation_trends",
            MagicMock(side_effect=lthcs_daily.finnhub.FinnhubAPIKeyMissing(
                "FINNHUB_API_KEY missing in test"
            )),
        )
        args = lthcs_daily.parse_args(["--tickers", "AAPL,LCID", "--skip-thesis"])
        state = lthcs_daily.PipelineState(args=args)
        lthcs_daily.stage_1_load_config(state)
        lthcs_daily.stage_2_fetch_data(state)
        lthcs_daily.stage_3_quality_checks(state)
        lthcs_daily.stage_4_compute_subscores(state)
        for sym in state.scored_tickers:
            th = state.pillar_results[sym]["thesis_integrity"]
            assert th["sub_score"] == 50.0
            assert th["data_quality"]["has_sentiment"] is False


# ---------------------------------------------------------------------------
# Stage 5/6 -- modifiers + final scoring
# ---------------------------------------------------------------------------

class TestStage6:
    def test_emits_one_snapshot_per_scored_ticker(self, base_state):
        assert len(base_state.snapshot_rows) == len(base_state.scored_tickers)
        for row in base_state.snapshot_rows:
            assert 0.0 <= row["lthcs_score"] <= 100.0
            assert row["band"] in {
                "elite", "high_confidence", "constructive",
                "monitor", "weakening", "review",
            }
            assert "drift_1d" in row


# ---------------------------------------------------------------------------
# Stage 7 -- narratives
# ---------------------------------------------------------------------------

class TestStage7:
    def test_one_narrative_per_snapshot(self, base_state):
        assert lthcs_daily.stage_7_generate_narratives(base_state) is True
        assert len(base_state.narrative_rows) == len(base_state.snapshot_rows)
        for narr in base_state.narrative_rows:
            assert "todays_take" in narr
            assert "why_changed" in narr
            assert "why_not_to_sell" in narr
            assert "what_would_break" in narr


# ---------------------------------------------------------------------------
# Stage 8 -- persistence
# ---------------------------------------------------------------------------

class TestStage8:
    def test_dry_run_skips_all_persist_writes(self, base_state, patched_persist_class):
        # Mutate the args on the existing state to flip --dry-run on.
        base_state.args.dry_run = True
        lthcs_daily.stage_7_generate_narratives(base_state)
        assert lthcs_daily.stage_8_persist(base_state) is True
        patched_persist_class.write_snapshot.assert_not_called()
        patched_persist_class.write_variable_detail.assert_not_called()
        patched_persist_class.write_narratives.assert_not_called()
        patched_persist_class.rebuild_history_for_all_tickers.assert_not_called()
        patched_persist_class.rebuild_index.assert_not_called()

    def test_each_persist_method_called_exactly_once(
        self, base_state, patched_persist_class
    ):
        lthcs_daily.stage_7_generate_narratives(base_state)
        assert lthcs_daily.stage_8_persist(base_state) is True
        assert patched_persist_class.write_snapshot.call_count == 1
        assert patched_persist_class.write_variable_detail.call_count == 1
        assert patched_persist_class.write_narratives.call_count == 1
        assert patched_persist_class.rebuild_history_for_all_tickers.call_count == 1
        assert patched_persist_class.rebuild_index.call_count == 1

    def test_existing_snapshot_without_force_fails(
        self, base_state, patched_persist_class
    ):
        patched_persist_class.snapshot_exists.return_value = True
        lthcs_daily.stage_7_generate_narratives(base_state)
        ok = lthcs_daily.stage_8_persist(base_state)
        assert ok is False
        patched_persist_class.write_snapshot.assert_not_called()

    def test_force_overwrites_existing_snapshot(
        self, base_state, patched_persist_class
    ):
        patched_persist_class.snapshot_exists.return_value = True
        base_state.args.force = True
        lthcs_daily.stage_7_generate_narratives(base_state)
        assert lthcs_daily.stage_8_persist(base_state) is True
        # overwrite kwarg propagated as True.
        assert patched_persist_class.write_snapshot.call_args.kwargs.get("overwrite") is True

    def test_variable_detail_has_one_row_per_pillar_per_ticker(
        self, base_state, patched_persist_class
    ):
        lthcs_daily.stage_7_generate_narratives(base_state)
        lthcs_daily.stage_8_persist(base_state)
        # 2 tickers x 5 pillars = 10 rows.
        assert len(base_state.variable_detail_rows) == 2 * 5


# ---------------------------------------------------------------------------
# Full pipeline -- main()
# ---------------------------------------------------------------------------

class TestMain:
    def test_dry_run_end_to_end_exits_zero(
        self, patched_configs, patched_persist_class, patched_sources
    ):
        rc = lthcs_daily.main(["--tickers", "AAPL", "--dry-run"])
        assert rc == 0
        # Dry run -> no writes.
        patched_persist_class.write_snapshot.assert_not_called()

    def test_full_run_persists_and_exits_zero(
        self, patched_configs, patched_persist_class, patched_sources
    ):
        rc = lthcs_daily.main(["--tickers", "AAPL"])
        assert rc == 0
        assert patched_persist_class.write_snapshot.call_count == 1
        assert patched_persist_class.rebuild_index.call_count == 1

    def test_existing_snapshot_without_force_exits_two(
        self, patched_configs, patched_persist_class, patched_sources
    ):
        patched_persist_class.snapshot_exists.return_value = True
        rc = lthcs_daily.main(["--tickers", "AAPL"])
        assert rc == 2
        patched_persist_class.write_snapshot.assert_not_called()

    def test_skip_thesis_results_in_zero_av_calls_end_to_end(
        self, patched_configs, patched_persist_class, patched_sources
    ):
        rc = lthcs_daily.main(["--tickers", "AAPL", "--skip-thesis", "--dry-run"])
        assert rc == 0
        assert patched_sources["av"].call_count == 0
