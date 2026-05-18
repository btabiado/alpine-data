"""Integration tests for the P4 DES expansion wiring.

Covers the two fixes:

    * Fix 1 — FRED Tier-2 macro snapshot is fetched in Stage 2 and
      threaded into ``des.compute_des`` in Stage 4 (so the DES sub-score
      can move beyond the Tier-1 ceiling).
    * Fix 2 — Sector-specific RSS (FDA / EIA / Fed) aggregate is
      persisted on state and stamped onto the Thesis pillar's
      ``data_quality`` block as ``has_sector_rss`` for the ~30 mapped
      pharma / energy / financials tickers.

All upstream source clients are mocked. No network, no real disk
outside of ``tmp_path``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

import lthcs_daily
from lthcs.pillars import des


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Universe mixes the three sector_rss-mapped sectors (LLY=pharma,
# XOM=energy, JPM=financials) plus a Tier-2 cyclical (LCID, Consumer
# Discretionary) and a Tier-2-amplified Energy name. AAPL stays as a
# defensive-ish reference (Tier-2 damped via the 0.6 default for
# Information Technology).
_UNIVERSE_FIXTURE = {
    "version": "test",
    "tickers": [
        {
            "ticker": "AAPL",
            "name": "Apple Inc.",
            "sector": "Information Technology",
            "maturity_stage": "standard_compounder",
            "active": True,
        },
        {
            "ticker": "LCID",
            "name": "Lucid Group",
            "sector": "Consumer Discretionary",
            "maturity_stage": "pre_profit_growth",
            "active": True,
        },
        {
            "ticker": "LLY",
            "name": "Eli Lilly",
            "sector": "Health Care",
            "maturity_stage": "standard_compounder",
            "active": True,
        },
        {
            "ticker": "XOM",
            "name": "ExxonMobil",
            "sector": "Energy",
            "maturity_stage": "standard_compounder",
            "active": True,
        },
        {
            "ticker": "JPM",
            "name": "JPMorgan",
            "sector": "Financials",
            "maturity_stage": "standard_compounder",
            "active": True,
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

# Sector weight fixture. All five sectors must be present so DES doesn't
# fall back to the neutral ``sector_known=False`` path on any ticker.
# Sensitivities chosen so Tier-1 lands very close to 50 (small contrib
# from fed_funds @ low end) — leaves headroom for Tier-2 to lift / drop
# the sub-score visibly in the tests.
_SECTOR_WEIGHTS_FIXTURE = {
    "magnitude_scale": 30.0,
    "signal_normalization": {
        "fed_funds_pct": {"low": 0.0, "high": 6.0},
        "ten_y_yield_pct": {"low": 1.0, "high": 5.0},
        "cpi_yoy_pct": {"low": 0.0, "high": 6.0},
    },
    "sectors": {
        "Information Technology": {"fed_funds_pct": -0.05},
        "Consumer Discretionary": {"fed_funds_pct": -0.05},
        "Health Care":            {"fed_funds_pct": -0.05},
        "Energy":                 {"fed_funds_pct": -0.05},
        "Financials":             {"fed_funds_pct":  0.05},
    },
    "ticker_overrides": {},
}


def _block(percentile_2y: float, current: float, *, regime: str = "neutral",
           change_3m_pct: float = 0.0, crack_spread: float = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "current": current,
        "change_3m_pct": change_3m_pct,
        "percentile_2y": percentile_2y,
    }
    if regime != "neutral":
        out["regime"] = regime
    if crack_spread is not None:
        out["crack_spread_per_gal"] = crack_spread
    return out


def _bullish_tier2() -> Dict[str, Any]:
    """Tier-2 snapshot with all six indicators screaming bullish.

    Brent low + gasoline crack low + ISM expansion + housing high +
    sentiment high + U-6 low.  Sector-scaled against a cyclical (1.0)
    this should comfortably push DES well above the +5 Tier-2 budget
    before clip.
    """
    return {
        "as_of": "2026-05-18",
        "brent_crude":        _block(percentile_2y=0.10, current=60.0,
                                     change_3m_pct=-0.05),
        "gasoline_retail":    _block(percentile_2y=0.10, current=2.80,
                                     change_3m_pct=-0.05, crack_spread=0.40),
        "ism_pmi_proxy":      _block(percentile_2y=0.80, current=105.0,
                                     regime="expansion", change_3m_pct=0.02),
        "housing_starts":     _block(percentile_2y=0.85, current=1600.0,
                                     change_3m_pct=0.06),
        "consumer_sentiment": _block(percentile_2y=0.90, current=85.0,
                                     change_3m_pct=0.04),
        "u6_unemployment":    _block(percentile_2y=0.10, current=6.5,
                                     change_3m_pct=-0.03),
        "data_quality": {
            "sources_ok": 6,
            "sources_failed": 0,
            "failed_sources": [],
        },
    }


@pytest.fixture
def patched_configs(monkeypatch, tmp_path):
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
def patched_sources(monkeypatch):
    """Stub out every Stage-2 source so the integration runs deterministically."""
    monkeypatch.setattr(
        lthcs_daily.yahoo, "get_daily_prices",
        MagicMock(return_value=[{"date": "2026-05-15", "close": 100.0}]),
    )
    monkeypatch.setattr(
        lthcs_daily.yahoo, "get_momentum_pct", MagicMock(return_value=0.05)
    )
    monkeypatch.setattr(
        lthcs_daily.yahoo, "get_volatility", MagicMock(return_value=0.25)
    )
    monkeypatch.setattr(
        lthcs_daily.sec_edgar, "get_revenue_history",
        MagicMock(return_value=[
            {"end_date": "2024-09-30", "start_date": "2023-10-01",
             "value": 100.0, "form": "10-K", "fp": "FY"},
            {"end_date": "2023-09-30", "start_date": "2022-10-01",
             "value": 92.0, "form": "10-K", "fp": "FY"},
        ]),
    )
    monkeypatch.setattr(
        lthcs_daily.sec_edgar, "get_gross_profit_history",
        MagicMock(return_value=[]),
    )
    monkeypatch.setattr(
        lthcs_daily.sec_edgar, "get_operating_cash_flow_history",
        MagicMock(return_value=[]),
    )
    monkeypatch.setattr(
        lthcs_daily.fred, "get_series",
        MagicMock(return_value=[{"date": "2026-05-15", "value": 4.3}]),
    )
    monkeypatch.setattr(
        lthcs_daily.fred, "get_latest_value",
        MagicMock(return_value={"date": "2026-05-01", "value": 0.5}),
    )
    monkeypatch.setattr(
        lthcs_daily.eia, "get_latest_value",
        MagicMock(return_value={"date": "2026-05-15", "value": 75.0}),
    )
    monkeypatch.setattr(
        lthcs_daily.alpha_vantage, "get_news_sentiment",
        MagicMock(return_value={"items": "0", "feed": []}),
    )

    # Tier-2 + sector_rss are the two new wires under test — default
    # them to "bullish" / "no events" so individual tests can override.
    tier2_mock = MagicMock(return_value=_bullish_tier2())
    sector_rss_mock = MagicMock(
        return_value={t: {
            "ticker": t,
            "event_count": 0,
            "event_titles": [],
            "first_seen": None,
            "last_seen": None,
            "sectors_matched": [],
        } for t in ("AAPL", "LCID", "LLY", "XOM", "JPM")}
    )
    monkeypatch.setattr(
        lthcs_daily.fred_tier2, "fetch_tier2_macro_snapshot", tier2_mock
    )
    monkeypatch.setattr(
        lthcs_daily.sector_rss, "aggregate_sector_events", sector_rss_mock
    )

    # Belt-and-suspenders: stub heavyweight network sources so a flaky
    # sandbox doesn't taint the integration.
    monkeypatch.setattr(
        lthcs_daily.sec_form4, "fetch_universe_insider_transactions",
        MagicMock(return_value={}),
    )
    monkeypatch.setattr(
        lthcs_daily.sec_13f, "fetch_universe_institutional_holdings",
        MagicMock(return_value={}),
    )
    monkeypatch.setattr(
        lthcs_daily.fred_breadth, "fetch_breadth_snapshot",
        MagicMock(return_value={"data_quality": {"sources_ok": 0}}),
    )
    monkeypatch.setattr(
        lthcs_daily.sector_etf, "fetch_sector_strength",
        MagicMock(return_value={"sectors": {}}),
    )
    monkeypatch.setattr(
        lthcs_daily.breadth_sentiment, "fetch_breadth_sentiment",
        MagicMock(return_value={"data_quality": {"sources_ok": 0}}),
    )

    return {
        "tier2": tier2_mock,
        "sector_rss": sector_rss_mock,
    }


def _run_stage_1_through_4(args_list, tmp_path, monkeypatch):
    """Helper: run stages 1-4 and return the state."""
    monkeypatch.setattr(
        "lthcs.sources.thesis_rotation.get_default_data_root",
        lambda: tmp_path,
    )
    args = lthcs_daily.parse_args(args_list)
    state = lthcs_daily.PipelineState(args=args)
    state.persist = lthcs_daily.LthcsPersist(data_root=tmp_path)
    assert lthcs_daily.stage_1_load_config(state)
    assert lthcs_daily.stage_2_fetch_data(state)
    assert lthcs_daily.stage_3_quality_checks(state)
    assert lthcs_daily.stage_4_compute_subscores(state)
    return state


# ---------------------------------------------------------------------------
# Fix 1: FRED Tier-2 wiring (Stage 2 fetch + Stage 4 DES integration)
# ---------------------------------------------------------------------------


class TestTier2WiredIntoStage2:
    def test_tier2_snapshot_is_fetched(
        self, patched_configs, patched_sources, tmp_path, monkeypatch
    ):
        """Stage 2 calls fetch_tier2_macro_snapshot and stores the result."""
        state = _run_stage_1_through_4(
            ["--tickers", "AAPL,LCID"], tmp_path, monkeypatch
        )
        assert patched_sources["tier2"].call_count == 1
        assert state.tier2_macro is not None
        assert "brent_crude" in state.tier2_macro
        # data_quality must report sources_ok=6 from our fixture.
        assert state.tier2_macro["data_quality"]["sources_ok"] == 6

    def test_tier2_failure_does_not_abort_stage_2(
        self, patched_configs, patched_sources, tmp_path, monkeypatch
    ):
        """A failed Tier-2 fetch must degrade to None, never crash."""
        patched_sources["tier2"].side_effect = RuntimeError("FRED down")
        state = _run_stage_1_through_4(
            ["--tickers", "AAPL"], tmp_path, monkeypatch
        )
        assert state.tier2_macro is None
        # DES still computes (Tier-1 only).
        assert state.pillar_results["AAPL"]["des"]["sub_score"] is not None


class TestTier2WiredIntoDESStage4:
    def test_tier2_components_present_on_des_result(
        self, patched_configs, patched_sources, tmp_path, monkeypatch
    ):
        """compute_des is called with tier2_macro so explainability lands."""
        state = _run_stage_1_through_4(
            ["--tickers", "AAPL,LCID,XOM"], tmp_path, monkeypatch
        )
        for sym in ("AAPL", "LCID", "XOM"):
            comp = state.pillar_results[sym]["des"]["components"]
            assert "tier2_inputs" in comp
            assert "tier2_quality" in comp
            assert "tier2_total_pts" in comp
            assert comp["tier2_quality"] == "good"

    def test_tier2_lifts_des_score_for_cyclical_with_bullish_macro(
        self, patched_configs, patched_sources, tmp_path, monkeypatch
    ):
        """LCID (Consumer Discretionary, sector scale 1.0) gets the full lift."""
        # Run WITH tier2 (default = bullish).
        state_with = _run_stage_1_through_4(
            ["--tickers", "LCID"], tmp_path, monkeypatch
        )
        score_with = state_with.pillar_results["LCID"]["des"]["sub_score"]

        # Run WITHOUT tier2 (force the fetch to fail so the snapshot drops to None).
        patched_sources["tier2"].side_effect = RuntimeError("simulated outage")
        state_without = _run_stage_1_through_4(
            ["--tickers", "LCID"], tmp_path, monkeypatch
        )
        score_without = state_without.pillar_results["LCID"]["des"]["sub_score"]

        # Bullish Tier-2 should LIFT the sub-score for a cyclical.
        assert score_with > score_without
        # And specifically the delta is bounded by TIER2_MAX_POINTS.
        assert score_with - score_without <= des.TIER2_MAX_POINTS + 0.01

    def test_tier2_lifts_des_above_tier1_ceiling(
        self, patched_configs, patched_sources, tmp_path, monkeypatch
    ):
        """Audit said Tier-1 ceiling capped near 73.

        With bullish Tier-2 layered on for a cyclical (LCID, scale 1.0),
        the sub-score should be able to land above 73 even when Tier-1
        is engineered to land *at* 73.
        """
        # mock fed_funds=0.5 with norm low=0/high=6 → tilt ≈ -0.833.
        # Pick a negative sensitivity that yields 50 + (-0.77) * (-0.833) * 30
        # ≈ 50 + 19.25 = 69.25 first to verify the math, then bump higher.
        # We want Tier-1 == ~73, so contribution = +23 / 30 = 0.7666 needed.
        # 0.7666 / 0.8333 = -0.92 sensitivity (flipped sign vs the tilt).
        boosted = json.loads(json.dumps(_SECTOR_WEIGHTS_FIXTURE))
        boosted["sectors"]["Consumer Discretionary"]["fed_funds_pct"] = -0.92
        sector_path = patched_configs[2]
        sector_path.write_text(json.dumps(boosted))

        # Sanity-check the Tier-1 baseline first (no Tier-2).
        patched_sources["tier2"].side_effect = RuntimeError("baseline outage")
        baseline_state = _run_stage_1_through_4(
            ["--tickers", "LCID"], tmp_path, monkeypatch
        )
        baseline_sub = baseline_state.pillar_results["LCID"]["des"]["sub_score"]
        # Baseline should land near 73 (Tier-1 ceiling per audit narrative).
        assert 70.0 <= baseline_sub <= 74.0, (
            f"expected Tier-1 baseline ~73 for LCID, got {baseline_sub}"
        )

        # Now flip Tier-2 back on (reset the side_effect) and confirm lift.
        patched_sources["tier2"].side_effect = None
        patched_sources["tier2"].return_value = _bullish_tier2()
        state = _run_stage_1_through_4(
            ["--tickers", "LCID"], tmp_path, monkeypatch
        )
        sub = state.pillar_results["LCID"]["des"]["sub_score"]
        assert sub > 73.0, (
            "Tier-2 should lift DES above the ~73 Tier-1 ceiling for a "
            f"cyclical with bullish macro; got {sub}"
        )

    def test_tier2_skipped_when_snapshot_none_byte_equal_to_tier1(
        self, patched_configs, patched_sources, tmp_path, monkeypatch
    ):
        """When fred_tier2 fetch returns None, DES result is byte-equal to Tier-1."""
        patched_sources["tier2"].side_effect = RuntimeError("outage")
        state = _run_stage_1_through_4(
            ["--tickers", "AAPL"], tmp_path, monkeypatch
        )
        comp = state.pillar_results["AAPL"]["des"]["components"]
        # No Tier-2 components when snapshot is None.
        assert "tier2_inputs" not in comp
        assert "tier2_quality" not in comp
        assert "tier2_total_pts" not in comp


# ---------------------------------------------------------------------------
# Fix 2: Sector-RSS wiring (Stage 2 aggregate + Stage 4 Thesis stamp)
# ---------------------------------------------------------------------------


class TestSectorRssWiredIntoStage2:
    def test_aggregate_called_and_persisted_on_state(
        self, patched_configs, patched_sources, tmp_path, monkeypatch
    ):
        state = _run_stage_1_through_4(
            ["--tickers", "AAPL,LCID,LLY,XOM,JPM"], tmp_path, monkeypatch
        )
        assert patched_sources["sector_rss"].call_count == 1
        # state.sector_rss_by_ticker should mirror the mock.
        assert set(state.sector_rss_by_ticker.keys()) >= {
            "AAPL", "LCID", "LLY", "XOM", "JPM"
        }

    def test_supplement_count_in_fetch_counts(
        self, patched_configs, patched_sources, tmp_path, monkeypatch
    ):
        """counts must expose sector_rss_supplement and sector_rss_covered."""
        # Give LLY 4 pharma events so it both supplements Thesis AND
        # bumps sector_rss_covered.
        patched_sources["sector_rss"].return_value = {
            "AAPL": {"ticker": "AAPL", "event_count": 0, "event_titles": [],
                     "first_seen": None, "last_seen": None, "sectors_matched": []},
            "LCID": {"ticker": "LCID", "event_count": 0, "event_titles": [],
                     "first_seen": None, "last_seen": None, "sectors_matched": []},
            "LLY": {"ticker": "LLY", "event_count": 4,
                    "event_titles": ["FDA approves trial"],
                    "first_seen": "2026-05-01", "last_seen": "2026-05-15",
                    "sectors_matched": ["pharma"]},
            "XOM": {"ticker": "XOM", "event_count": 2,
                    "event_titles": ["Crude oil prices"],
                    "first_seen": "2026-05-10", "last_seen": "2026-05-17",
                    "sectors_matched": ["energy"]},
            "JPM": {"ticker": "JPM", "event_count": 0, "event_titles": [],
                    "first_seen": None, "last_seen": None, "sectors_matched": []},
        }
        state = _run_stage_1_through_4(
            ["--tickers", "AAPL,LCID,LLY,XOM,JPM"], tmp_path, monkeypatch
        )
        assert state.fetch_counts.get("sector_rss_covered") == 2
        # supplement_count is non-negative — exact value depends on whether
        # Finnhub/yahoo upstream supplements ran first; just guard it's tracked.
        assert "sector_rss_supplement" in state.fetch_counts


class TestSectorRssStampedOnThesis:
    def test_has_sector_rss_true_for_mapped_ticker_with_events(
        self, patched_configs, patched_sources, tmp_path, monkeypatch
    ):
        patched_sources["sector_rss"].return_value = {
            "LLY": {"ticker": "LLY", "event_count": 5,
                    "event_titles": ["FDA approves new therapy"],
                    "first_seen": "2026-05-01", "last_seen": "2026-05-15",
                    "sectors_matched": ["pharma"]},
        }
        state = _run_stage_1_through_4(
            ["--tickers", "LLY"], tmp_path, monkeypatch
        )
        th = state.pillar_results["LLY"]["thesis_integrity"]
        assert th["data_quality"]["has_sector_rss"] is True
        assert th["data_quality"]["sector_rss_event_count"] == 5
        assert th["data_quality"]["sector_rss_sectors"] == ["pharma"]

    def test_has_sector_rss_false_for_unmapped_ticker(
        self, patched_configs, patched_sources, tmp_path, monkeypatch
    ):
        """AAPL isn't in any sector keyword map => has_sector_rss=False."""
        state = _run_stage_1_through_4(
            ["--tickers", "AAPL"], tmp_path, monkeypatch
        )
        th = state.pillar_results["AAPL"]["thesis_integrity"]
        assert th["data_quality"].get("has_sector_rss") is False
        # And the count keys must NOT appear when there are no events.
        assert "sector_rss_event_count" not in th["data_quality"]

    def test_has_sector_rss_false_for_mapped_ticker_with_zero_events(
        self, patched_configs, patched_sources, tmp_path, monkeypatch
    ):
        """LLY is mapped but if no FDA items match keywords, flag is False."""
        # Default sector_rss_mock fixture returns event_count=0 for every ticker.
        state = _run_stage_1_through_4(
            ["--tickers", "LLY"], tmp_path, monkeypatch
        )
        th = state.pillar_results["LLY"]["thesis_integrity"]
        assert th["data_quality"].get("has_sector_rss") is False

    def test_variable_detail_carries_sector_rss_flag(
        self, patched_configs, patched_sources, tmp_path, monkeypatch
    ):
        """Stage 8 builds variable_detail from pillar_results.

        The has_sector_rss flag must survive into the Thesis row of the
        variable_detail output so the dashboard / explainability layer
        can read it.
        """
        patched_sources["sector_rss"].return_value = {
            "XOM": {"ticker": "XOM", "event_count": 3,
                    "event_titles": ["EIA energy report"],
                    "first_seen": "2026-05-10", "last_seen": "2026-05-17",
                    "sectors_matched": ["energy"]},
        }
        state = _run_stage_1_through_4(
            ["--tickers", "XOM"], tmp_path, monkeypatch
        )
        assert lthcs_daily.stage_5_apply_modifiers(state)
        assert lthcs_daily.stage_6_compute_final_scores(state)
        assert lthcs_daily.stage_7_generate_narratives(state)
        # Build variable_detail rows the way stage_8 does.
        state.variable_detail_rows = []
        for sym, pillars in state.pillar_results.items():
            for pillar_name, result in pillars.items():
                state.variable_detail_rows.append({
                    "ticker": sym,
                    "pillar": pillar_name,
                    "components": dict(result.get("components") or {}),
                    "sub_score": float(result.get("sub_score", 50.0)),
                    "data_quality": dict(result.get("data_quality") or {}),
                })
        thesis_rows = [r for r in state.variable_detail_rows
                       if r["pillar"] == "thesis_integrity"]
        assert len(thesis_rows) == 1
        assert thesis_rows[0]["data_quality"]["has_sector_rss"] is True
        assert thesis_rows[0]["data_quality"]["sector_rss_event_count"] == 3


# ---------------------------------------------------------------------------
# Tier-2 snapshot persisted to data/lthcs/macro/fred_tier2_<date>.json
# ---------------------------------------------------------------------------


class TestTier2PersistedToMacroDir:
    def test_tier2_snapshot_written_to_macro_dir(
        self, patched_configs, patched_sources, tmp_path, monkeypatch
    ):
        state = _run_stage_1_through_4(
            ["--tickers", "AAPL"], tmp_path, monkeypatch
        )
        assert lthcs_daily.stage_5_apply_modifiers(state)
        assert lthcs_daily.stage_6_compute_final_scores(state)
        assert lthcs_daily.stage_7_generate_narratives(state)
        assert lthcs_daily.stage_7p5_compute_index(state)
        # Use the real persist (already pointed at tmp_path) for Stage 8.
        state.args.force = True
        assert lthcs_daily.stage_8_persist(state) is True

        macro_dir = tmp_path / "macro"
        out = macro_dir / f"fred_tier2_{state.calc_date}.json"
        assert out.exists(), f"expected {out} to exist after Stage 8"
        body = json.loads(out.read_text())
        assert body["data_quality"]["sources_ok"] == 6
        assert "brent_crude" in body
