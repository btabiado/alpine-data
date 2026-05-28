"""Tests for the LTHCS MCP server's data-access layer.

The tests target :mod:`lthcs_mcp.data` directly so they run without the
``mcp`` SDK installed. The FastMCP wrappers in :mod:`lthcs_mcp.server` are
thin and merely forward validated args to these functions.

Most tests build a synthetic ``data/lthcs/`` tree in a tmp_path so the suite
is hermetic. A couple of smoke tests hit the real repo data to catch any
schema drift.
"""

from __future__ import annotations

import json
import os

import pytest

from lthcs_mcp import data as ldata


REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
)
REAL_DATA_ROOT = os.path.join(REPO_ROOT, "data", "lthcs")


# --- Fixtures --------------------------------------------------------------


@pytest.fixture
def fake_root(tmp_path) -> str:
    """Build a minimal but realistic ``data/lthcs`` tree under tmp_path."""
    root = tmp_path / "lthcs"
    (root / "snapshots").mkdir(parents=True)
    (root / "variable_detail").mkdir()
    (root / "insider").mkdir()
    (root / "holdings").mkdir()
    (root / "macro").mkdir()
    (root / "index").mkdir()
    (root / "history" / "by_ticker").mkdir(parents=True)

    snap = {
        "calc_date": "2026-05-17",
        "model_version": "v1.1.0",
        "scores": [
            {
                "ticker": "AAPL",
                "lthcs_score": 58.7,
                "band": "weakening",
                "drift_1d": 0.1,
                "drift_7d": 0.2,
                "drift_30d": 0.5,
                "drift_90d": 1.0,
                "confidence_level": "high",
                "data_quality_flags": [],
                "subscores": {
                    "adoption_momentum": 50.0,
                    "institutional_confidence": 68.5,
                    "financial_evolution": 67.5,
                    "thesis_integrity": 62.4,
                    "des": 44.9,
                },
                "modifiers": {"macro_adj": 0.0, "sector_adj": 0.0, "volatility_mod": 0.0},
                "maturity_stage": "mature_compounder",
                "sector": "Technology",
            },
            {
                "ticker": "MSFT",
                "lthcs_score": 72.0,
                "band": "constructive",
                "drift_1d": 0.0,
                "drift_7d": 0.0,
                "drift_30d": 0.0,
                "drift_90d": 0.0,
                "confidence_level": "high",
                "data_quality_flags": [],
                "subscores": {
                    "adoption_momentum": 60.0,
                    "institutional_confidence": 75.0,
                    "financial_evolution": 80.0,
                    "thesis_integrity": 70.0,
                    "des": 55.0,
                },
                "modifiers": {"macro_adj": 0.0, "sector_adj": 0.0, "volatility_mod": 0.0},
                "maturity_stage": "mature_compounder",
                "sector": "Technology",
            },
            {
                "ticker": "ZZZ",
                "lthcs_score": 30.0,
                "band": "review",
                "drift_1d": 0.0,
                "drift_7d": 0.0,
                "drift_30d": 0.0,
                "drift_90d": 0.0,
                "confidence_level": "low",
                "data_quality_flags": ["partial"],
                "subscores": {
                    "adoption_momentum": 30.0,
                    "institutional_confidence": 30.0,
                    "financial_evolution": 30.0,
                    "thesis_integrity": 30.0,
                    "des": 30.0,
                },
                "modifiers": {},
                "maturity_stage": "recovery_candidate",
                "sector": "Industrials",
            },
        ],
    }
    (root / "snapshots" / "2026-05-17.json").write_text(json.dumps(snap))

    var_detail = {
        "calc_date": "2026-05-17",
        "model_version": "v1.1.0",
        "variables": [
            {
                "ticker": "AAPL",
                "pillar": "adoption_momentum",
                "sub_score": 50.0,
                "components": {"revenue_subscore": 50.0},
                "data_quality": {"has_revenue": True},
            },
            {
                "ticker": "AAPL",
                "pillar": "thesis_integrity",
                "sub_score": 62.4,
                "components": {"article_count": 50},
                "data_quality": {"has_sentiment": True},
            },
        ],
    }
    (root / "variable_detail" / "2026-05-17.json").write_text(json.dumps(var_detail))

    insider = {
        "AAPL": {
            "as_of": "2026-05-17",
            "cluster_buying": False,
            "ceo_cfo_action": "neutral",
            "conviction_score": -1.0,
            "buy_count": 0,
            "net_dollar_value": -1000.0,
        },
        "BUY1": {
            "as_of": "2026-05-17",
            "cluster_buying": True,
            "ceo_cfo_action": "buying",
            "conviction_score": 0.8,
            "buy_count": 5,
            "net_dollar_value": 50000.0,
        },
        "BUY2": {
            "as_of": "2026-05-17",
            "cluster_buying": True,
            "ceo_cfo_action": "buying",
            "conviction_score": 0.9,
            "buy_count": 3,
            "net_dollar_value": 20000.0,
        },
    }
    (root / "insider" / "2026-05-17.json").write_text(json.dumps(insider))

    holdings = {
        "AAPL": {
            "as_of": "2026-05-17",
            "conviction_signal": "mixed",
            "signal_score": -0.1,
            "manager_count": 10,
            "data_quality": "good",
            "latest_quarter": "2026-Q1",
            "quarter_over_quarter": {"net_buyers": 4, "net_sellers": 5},
            "top_holders": [{"manager": "BlackRock", "rank": 1, "shares_mm": 1.0, "value_bn": 1.0}],
        }
    }
    (root / "holdings" / "2026-05-17.json").write_text(json.dumps(holdings))

    (root / "macro" / "breadth_2026-05-17.json").write_text(
        json.dumps({"as_of": "2026-05-17", "broad_dollar": {"current": 118.0}})
    )
    (root / "macro" / "sector_strength_2026-05-17.json").write_text(
        json.dumps({"as_of": "2026-05-17", "sectors": {"XLK": {"rank_1m": 1}}})
    )
    (root / "macro" / "breadth_sentiment_2026-05-17.json").write_text(
        json.dumps({"as_of": "2026-05-17", "aaii": {"bullish_pct": 39.3}})
    )

    (root / "index" / "2026-05-17.json").write_text(
        json.dumps(
            {
                "as_of": "2026-05-17",
                "score": -15,
                "label": "LTHCS NEUTRAL",
                "band_key": "monitor",
                "color": "#F0A861",
                "components": [{"name": "x", "value": 1, "delta": 0, "read": "ok"}],
                "note": "test",
            }
        )
    )

    # Per-ticker history files
    (root / "history" / "by_ticker" / "AAPL.json").write_text(
        json.dumps(
            {
                "ticker": "AAPL",
                "model_version": "v1.1.0",
                "history": [
                    {"date": "2026-05-17", "score": 58.7, "band": "weakening"},
                    {"date": "2026-05-16", "score": 56.0, "band": "weakening"},
                    {"date": "2026-04-17", "score": 70.0, "band": "constructive"},
                ],
            }
        )
    )
    (root / "history" / "by_ticker" / "MSFT.json").write_text(
        json.dumps(
            {
                "ticker": "MSFT",
                "model_version": "v1.1.0",
                "history": [
                    {"date": "2026-05-17", "score": 72.0, "band": "constructive"},
                    {"date": "2026-04-17", "score": 50.0, "band": "weakening"},
                ],
            }
        )
    )

    (root / "universe.json").write_text(
        json.dumps(
            {
                "version": "test",
                "tickers": [
                    {
                        "ticker": "AAPL",
                        "name": "Apple Inc.",
                        "sector": "Technology",
                        "maturity_stage": "mature_compounder",
                    },
                    {
                        "ticker": "MSFT",
                        "name": "Microsoft Corporation",
                        "sector": "Technology",
                        "maturity_stage": "mature_compounder",
                    },
                    {
                        "ticker": "ZZZ",
                        "name": "Zilch Holdings",
                        "sector": "Industrials",
                        "maturity_stage": "recovery_candidate",
                    },
                ],
            }
        )
    )

    # Crypto snapshot for get_crypto_universe tests.
    (root / "snapshots_crypto").mkdir()
    crypto_snap = {
        "calc_date": "2026-05-16",
        "model_version": "v1.1.0",
        "asset_class": "crypto",
        "scores": [
            {
                "ticker": "BTC",
                "lthcs_score": 64.5,
                "band": "monitor",
                "drift_7d": 0.3,
                "drift_30d": 1.2,
                "confidence_level": "medium",
                "subscores": {
                    "adoption_momentum": 39.0,
                    "institutional_confidence": 81.7,
                    "financial_evolution": 63.2,
                    "thesis_integrity": 50.0,
                    "des": 52.9,
                },
                "dropped_pillars": ["thesis_integrity"],
                "maturity_stage": "btc",
                "data_quality_flags": ["thesis_unavailable"],
            },
            {
                "ticker": "ETH",
                "lthcs_score": 58.0,
                "band": "monitor",
                "drift_7d": -0.2,
                "drift_30d": -1.0,
                "confidence_level": "medium",
                "subscores": {
                    "adoption_momentum": 45.0,
                    "institutional_confidence": 60.0,
                    "financial_evolution": 70.0,
                    "thesis_integrity": 50.0,
                    "des": 55.0,
                },
                "dropped_pillars": [],
                "maturity_stage": "eth",
                "data_quality_flags": [],
            },
        ],
    }
    (root / "snapshots_crypto" / "2026-05-16.json").write_text(
        json.dumps(crypto_snap)
    )

    return str(root)


# --- get_ticker_score ------------------------------------------------------


def test_get_ticker_score_happy_path(fake_root: str) -> None:
    out = ldata.get_ticker_score("AAPL", data_root=fake_root)
    assert out["ticker"] == "AAPL"
    assert out["score"] == 58.7
    assert out["band"] == "weakening"
    assert out["drift"]["30d"] == 0.5
    assert set(out["subscores"].keys()) == {
        "adoption_momentum",
        "institutional_confidence",
        "financial_evolution",
        "thesis_integrity",
        "des",
    }
    assert out["sector"] == "Technology"


def test_get_ticker_score_case_insensitive(fake_root: str) -> None:
    out = ldata.get_ticker_score("aapl", data_root=fake_root)
    assert out["ticker"] == "AAPL"


def test_get_ticker_score_unknown_ticker(fake_root: str) -> None:
    out = ldata.get_ticker_score("NOPE", data_root=fake_root)
    assert "error" in out
    assert "NOPE" in out["error"]


def test_get_ticker_score_missing_date_file(fake_root: str) -> None:
    out = ldata.get_ticker_score("AAPL", date="2025-01-01", data_root=fake_root)
    assert "error" in out
    assert "data not available" in out["error"]


def test_get_ticker_score_invalid_date(fake_root: str) -> None:
    out = ldata.get_ticker_score("AAPL", date="not-a-date", data_root=fake_root)
    assert "error" in out
    assert "YYYY-MM-DD" in out["error"]


def test_get_ticker_score_future_date_rejected(fake_root: str) -> None:
    out = ldata.get_ticker_score("AAPL", date="2099-01-01", data_root=fake_root)
    assert "error" in out
    assert "future" in out["error"]


# --- get_universe_distribution --------------------------------------------


def test_get_universe_distribution(fake_root: str) -> None:
    out = ldata.get_universe_distribution(data_root=fake_root)
    assert out["total_tickers"] == 3
    assert out["bands"]["weakening"] == 1
    assert out["bands"]["constructive"] == 1
    assert out["bands"]["review"] == 1
    assert out["bands"]["elite"] == 0
    assert set(out["bands"].keys()) == {
        "elite",
        "high_confidence",
        "constructive",
        "monitor",
        "weakening",
        "review",
    }


# --- get_composite_index --------------------------------------------------


def test_get_composite_index(fake_root: str) -> None:
    out = ldata.get_composite_index(data_root=fake_root)
    assert out["score"] == -15
    assert out["label"] == "LTHCS NEUTRAL"
    assert out["band_key"] == "monitor"
    assert isinstance(out["components"], list)


# --- get_top_movers -------------------------------------------------------


def test_get_top_movers_gainers(fake_root: str) -> None:
    out = ldata.get_top_movers(direction="gainers", limit=5, period_days=30, data_root=fake_root)
    assert out["direction"] == "gainers"
    # MSFT went 50 -> 72 over the window — should be the top gainer
    assert out["movers"][0]["ticker"] == "MSFT"
    assert out["movers"][0]["delta"] > 0


def test_get_top_movers_decliners(fake_root: str) -> None:
    out = ldata.get_top_movers(direction="decliners", limit=5, period_days=30, data_root=fake_root)
    # AAPL went 70 -> 58.7 — should appear among decliners with negative delta
    assert out["movers"][0]["delta"] < 0
    tickers = [m["ticker"] for m in out["movers"]]
    assert "AAPL" in tickers


def test_get_top_movers_bad_direction(fake_root: str) -> None:
    out = ldata.get_top_movers(direction="sideways", data_root=fake_root)
    assert "error" in out


# --- get_insider_signals --------------------------------------------------


def test_get_insider_signals_by_ticker(fake_root: str) -> None:
    out = ldata.get_insider_signals(ticker="AAPL", data_root=fake_root)
    assert out["ticker"] == "AAPL"
    assert out["signals"]["conviction_score"] == -1.0


def test_get_insider_signals_cluster_buying(fake_root: str) -> None:
    out = ldata.get_insider_signals(regime="cluster_buying", data_root=fake_root)
    assert out["count"] == 2
    tickers = {m["ticker"] for m in out["tickers"]}
    assert tickers == {"BUY1", "BUY2"}


def test_get_insider_signals_requires_arg(fake_root: str) -> None:
    out = ldata.get_insider_signals(data_root=fake_root)
    assert "error" in out


def test_get_insider_signals_invalid_regime(fake_root: str) -> None:
    out = ldata.get_insider_signals(regime="bogus", data_root=fake_root)
    assert "error" in out


# --- get_holdings ---------------------------------------------------------


def test_get_holdings(fake_root: str) -> None:
    out = ldata.get_holdings("AAPL", data_root=fake_root)
    assert out["ticker"] == "AAPL"
    assert out["conviction_signal"] == "mixed"
    assert out["manager_count"] == 10
    assert out["top_holders"][0]["manager"] == "BlackRock"


def test_get_holdings_missing(fake_root: str) -> None:
    out = ldata.get_holdings("NOPE", data_root=fake_root)
    assert "error" in out


# --- get_pillar_breakdown -------------------------------------------------


def test_get_pillar_breakdown(fake_root: str) -> None:
    out = ldata.get_pillar_breakdown("AAPL", data_root=fake_root)
    assert out["ticker"] == "AAPL"
    pillars = {p["pillar"] for p in out["pillars"]}
    assert "adoption_momentum" in pillars
    assert "thesis_integrity" in pillars


# --- get_history ----------------------------------------------------------


def test_get_history_limits(fake_root: str) -> None:
    out = ldata.get_history("AAPL", days=2, data_root=fake_root)
    assert out["count"] == 2
    assert out["history"][0]["date"] == "2026-05-17"  # newest first


def test_get_history_default_30(fake_root: str) -> None:
    out = ldata.get_history("AAPL", data_root=fake_root)
    # File has 3 entries; days=30 returns all of them
    assert out["count"] == 3


def test_get_history_unknown_ticker(fake_root: str) -> None:
    out = ldata.get_history("NOPE", data_root=fake_root)
    assert "error" in out


def test_get_history_invalid_days(fake_root: str) -> None:
    out = ldata.get_history("AAPL", days=0, data_root=fake_root)
    assert "error" in out


# --- get_macro_regime -----------------------------------------------------


def test_get_macro_regime(fake_root: str) -> None:
    out = ldata.get_macro_regime(data_root=fake_root)
    assert set(out["available"]) == {"breadth", "sector_strength", "breadth_sentiment"}
    assert out["breadth"]["broad_dollar"]["current"] == 118.0
    assert out["breadth_sentiment"]["aaii"]["bullish_pct"] == 39.3


def test_get_macro_regime_missing_date(fake_root: str) -> None:
    out = ldata.get_macro_regime(date="2020-01-01", data_root=fake_root)
    assert "error" in out


# --- search_tickers -------------------------------------------------------


def test_search_tickers_by_symbol(fake_root: str) -> None:
    out = ldata.search_tickers("AAPL", data_root=fake_root)
    assert out["count"] >= 1
    assert out["matches"][0]["ticker"] == "AAPL"
    assert out["matches"][0]["score"] == 58.7


def test_search_tickers_case_insensitive(fake_root: str) -> None:
    out = ldata.search_tickers("apple", data_root=fake_root)
    assert out["count"] == 1
    assert out["matches"][0]["ticker"] == "AAPL"


def test_search_tickers_substring_company_name(fake_root: str) -> None:
    out = ldata.search_tickers("microsoft", data_root=fake_root)
    assert out["count"] == 1
    assert out["matches"][0]["ticker"] == "MSFT"


def test_search_tickers_limit(fake_root: str) -> None:
    # Query "z" matches AmaZon-style — but in our fake universe matches MSFT
    # (contains 'z' in nothing... actually only ZZZ). Use a query that yields
    # 2 hits via name substring and verify limit clamps.
    out = ldata.search_tickers("e", limit=2, data_root=fake_root)
    assert out["count"] <= 2


def test_search_tickers_empty_query() -> None:
    out = ldata.search_tickers("")
    assert "error" in out


# --- Smoke tests on real data ---------------------------------------------


@pytest.mark.skipif(
    not os.path.exists(os.path.join(REAL_DATA_ROOT, "snapshots", "2026-05-17.json")),
    reason="real LTHCS snapshot not present",
)
def test_real_data_ticker_score() -> None:
    out = ldata.get_ticker_score("AAPL", date="2026-05-17")
    assert "error" not in out
    assert out["ticker"] == "AAPL"
    assert isinstance(out["score"], (int, float))
    assert set(out["subscores"].keys()) == {
        "adoption_momentum",
        "institutional_confidence",
        "financial_evolution",
        "thesis_integrity",
        "des",
    }


@pytest.mark.skipif(
    not os.path.exists(os.path.join(REAL_DATA_ROOT, "index", "2026-05-17.json")),
    reason="real LTHCS index not present",
)
def test_real_data_composite_index() -> None:
    out = ldata.get_composite_index(date="2026-05-17")
    assert "error" not in out
    assert "components" in out
    assert isinstance(out["components"], list)


# --- get_dragging_pillar --------------------------------------------------


def test_get_dragging_pillar_weakening(fake_root: str) -> None:
    # AAPL is in fake_root with band='weakening'; lowest subscore is des=44.9
    out = ldata.get_dragging_pillar("AAPL", data_root=fake_root)
    assert out["ticker"] == "AAPL"
    assert out["band"] == "weakening"
    assert out["dragging_pillar"] == "des"
    assert out["sub_score"] == 44.9
    assert "lowest" in out["rationale"]


def test_get_dragging_pillar_review(fake_root: str) -> None:
    # ZZZ is in band='review' with all subscores equal at 30.0 — first pillar
    # in _PILLAR_ORDER wins on equal scores when weights are absent (all 0).
    out = ldata.get_dragging_pillar("ZZZ", data_root=fake_root)
    assert out["band"] == "review"
    assert out["dragging_pillar"] == "adoption_momentum"
    assert out["sub_score"] == 30.0


def test_get_dragging_pillar_skipped_for_buy_band(fake_root: str) -> None:
    # MSFT is in band='constructive' — Buy bucket, no drag to surface.
    out = ldata.get_dragging_pillar("MSFT", data_root=fake_root)
    assert out["ticker"] == "MSFT"
    assert out["band"] == "constructive"
    assert out["dragging_pillar"] is None
    assert out["sub_score"] is None
    assert "Buy or Hold" in out["rationale"]


def test_get_dragging_pillar_unknown_ticker(fake_root: str) -> None:
    out = ldata.get_dragging_pillar("NOPE", data_root=fake_root)
    assert "error" in out
    assert "NOPE" in out["error"]


def test_get_dragging_pillar_case_insensitive(fake_root: str) -> None:
    out = ldata.get_dragging_pillar("aapl", data_root=fake_root)
    assert out["ticker"] == "AAPL"
    assert out["dragging_pillar"] == "des"


def test_get_dragging_pillar_tie_break_by_weight(tmp_path) -> None:
    # Two pillars tied at lowest sub-score; weights_used breaks the tie.
    root = tmp_path / "lthcs"
    (root / "snapshots").mkdir(parents=True)
    snap = {
        "calc_date": "2026-05-17",
        "scores": [
            {
                "ticker": "TIE",
                "lthcs_score": 40.0,
                "band": "weakening",
                "subscores": {
                    "adoption_momentum": 30.0,
                    "institutional_confidence": 60.0,
                    "financial_evolution": 60.0,
                    "thesis_integrity": 60.0,
                    "des": 30.0,
                },
                # adoption_momentum (idx 0) weight=0.10, des (idx 4) weight=0.35
                # — des should win the tie.
                "weights_used": [0.10, 0.15, 0.20, 0.20, 0.35],
            }
        ],
    }
    (root / "snapshots" / "2026-05-17.json").write_text(json.dumps(snap))
    out = ldata.get_dragging_pillar("TIE", data_root=str(root))
    assert out["dragging_pillar"] == "des"


# --- list_band ------------------------------------------------------------


def test_list_band_returns_tickers_in_band(fake_root: str) -> None:
    out = ldata.list_band(band="weakening", data_root=fake_root)
    assert out["band"] == "weakening"
    assert out["total_in_band"] == 1
    assert out["count"] == 1
    assert out["tickers"][0]["ticker"] == "AAPL"
    assert out["tickers"][0]["score"] == 58.7
    assert out["tickers"][0]["sector"] == "Technology"


def test_list_band_sorted_by_composite_desc(tmp_path) -> None:
    # Build a snapshot with 3 tickers all in the same band; check sort.
    root = tmp_path / "lthcs"
    (root / "snapshots").mkdir(parents=True)
    snap = {
        "calc_date": "2026-05-17",
        "scores": [
            {"ticker": "A", "lthcs_score": 50.0, "band": "constructive"},
            {"ticker": "B", "lthcs_score": 80.0, "band": "constructive"},
            {"ticker": "C", "lthcs_score": 65.0, "band": "constructive"},
        ],
    }
    (root / "snapshots" / "2026-05-17.json").write_text(json.dumps(snap))
    out = ldata.list_band(band="constructive", data_root=str(root))
    assert [t["ticker"] for t in out["tickers"]] == ["B", "C", "A"]


def test_list_band_respects_limit(tmp_path) -> None:
    root = tmp_path / "lthcs"
    (root / "snapshots").mkdir(parents=True)
    snap = {
        "calc_date": "2026-05-17",
        "scores": [
            {"ticker": f"T{i}", "lthcs_score": float(i), "band": "monitor"}
            for i in range(25)
        ],
    }
    (root / "snapshots" / "2026-05-17.json").write_text(json.dumps(snap))
    out = ldata.list_band(band="monitor", limit=5, data_root=str(root))
    assert out["total_in_band"] == 25
    assert out["count"] == 5
    assert out["tickers"][0]["ticker"] == "T24"  # highest score


def test_list_band_invalid_band(fake_root: str) -> None:
    out = ldata.list_band(band="nonsense", data_root=fake_root)
    assert "error" in out
    assert "band must be" in out["error"]


def test_list_band_case_insensitive(fake_root: str) -> None:
    out = ldata.list_band(band="WEAKENING", data_root=fake_root)
    assert out["band"] == "weakening"
    assert out["count"] == 1


def test_list_band_empty_band(fake_root: str) -> None:
    # No 'elite' tickers in fake_root.
    out = ldata.list_band(band="elite", data_root=fake_root)
    assert out["total_in_band"] == 0
    assert out["tickers"] == []


def test_list_band_invalid_limit(fake_root: str) -> None:
    out = ldata.list_band(band="weakening", limit=0, data_root=fake_root)
    assert "error" in out


# --- get_pillar_attribution -----------------------------------------------


def test_get_pillar_attribution_returns_sub_score_and_evidence(
    fake_root: str,
) -> None:
    out = ldata.get_pillar_attribution(
        ticker="AAPL", pillar="adoption_momentum", data_root=fake_root
    )
    assert out["ticker"] == "AAPL"
    assert out["pillar"] == "adoption_momentum"
    assert out["sub_score"] == 50.0
    assert len(out["evidence"]) == 1
    assert out["evidence"][0]["components"] == {"revenue_subscore": 50.0}


def test_get_pillar_attribution_invalid_pillar(fake_root: str) -> None:
    out = ldata.get_pillar_attribution(
        ticker="AAPL", pillar="bogus", data_root=fake_root
    )
    assert "error" in out
    assert "pillar must be" in out["error"]


def test_get_pillar_attribution_unknown_ticker(fake_root: str) -> None:
    out = ldata.get_pillar_attribution(
        ticker="NOPE", pillar="adoption_momentum", data_root=fake_root
    )
    assert "error" in out


def test_get_pillar_attribution_case_insensitive(fake_root: str) -> None:
    out = ldata.get_pillar_attribution(
        ticker="aapl", pillar="ADOPTION_MOMENTUM", data_root=fake_root
    )
    assert out["ticker"] == "AAPL"
    assert out["pillar"] == "adoption_momentum"


def test_get_pillar_attribution_missing_evidence_but_canonical(tmp_path) -> None:
    # Snapshot has subscore but variable_detail file has no row for that pillar.
    root = tmp_path / "lthcs"
    (root / "snapshots").mkdir(parents=True)
    (root / "variable_detail").mkdir(parents=True)
    snap = {
        "calc_date": "2026-05-17",
        "scores": [
            {
                "ticker": "X",
                "lthcs_score": 50.0,
                "band": "monitor",
                "subscores": {"des": 42.0},
            }
        ],
    }
    (root / "snapshots" / "2026-05-17.json").write_text(json.dumps(snap))
    (root / "variable_detail" / "2026-05-17.json").write_text(
        json.dumps({"variables": []})
    )
    out = ldata.get_pillar_attribution(
        ticker="X", pillar="des", data_root=str(root)
    )
    assert out["sub_score"] == 42.0
    assert out["evidence"] == []


# --- get_recent_movers ----------------------------------------------------


def test_get_recent_movers_up(fake_root: str) -> None:
    # AAPL has drift_7d=0.2, MSFT=0.0, ZZZ=0.0 — AAPL should top the list.
    out = ldata.get_recent_movers(direction="up", limit=5, data_root=fake_root)
    assert out["direction"] == "up"
    assert out["count"] == 3
    assert out["movers"][0]["ticker"] == "AAPL"
    assert out["movers"][0]["drift_7d"] == 0.2


def test_get_recent_movers_down(tmp_path) -> None:
    root = tmp_path / "lthcs"
    (root / "snapshots").mkdir(parents=True)
    snap = {
        "calc_date": "2026-05-17",
        "scores": [
            {"ticker": "UP", "lthcs_score": 60.0, "band": "monitor", "drift_7d": 1.5},
            {"ticker": "DOWN", "lthcs_score": 40.0, "band": "weakening", "drift_7d": -2.0},
            {"ticker": "FLAT", "lthcs_score": 50.0, "band": "monitor", "drift_7d": 0.0},
        ],
    }
    (root / "snapshots" / "2026-05-17.json").write_text(json.dumps(snap))
    out = ldata.get_recent_movers(direction="down", limit=2, data_root=str(root))
    assert out["movers"][0]["ticker"] == "DOWN"
    assert out["movers"][0]["drift_7d"] == -2.0


def test_get_recent_movers_invalid_direction(fake_root: str) -> None:
    out = ldata.get_recent_movers(direction="sideways", data_root=fake_root)
    assert "error" in out


def test_get_recent_movers_respects_limit(fake_root: str) -> None:
    out = ldata.get_recent_movers(direction="up", limit=2, data_root=fake_root)
    assert out["count"] == 2
    assert out["limit"] == 2


def test_get_recent_movers_invalid_limit(fake_root: str) -> None:
    out = ldata.get_recent_movers(direction="up", limit=0, data_root=fake_root)
    assert "error" in out


# --- get_crypto_universe --------------------------------------------------


def test_get_crypto_universe_returns_scores(fake_root: str) -> None:
    out = ldata.get_crypto_universe(data_root=fake_root)
    assert out["asset_class"] == "crypto"
    assert out["count"] == 2
    tickers = [t["ticker"] for t in out["tickers"]]
    assert "BTC" in tickers
    assert "ETH" in tickers
    # Sorted by score desc — BTC (64.5) before ETH (58.0)
    assert out["tickers"][0]["ticker"] == "BTC"
    assert out["tickers"][0]["score"] == 64.5
    assert out["tickers"][0]["dropped_pillars"] == ["thesis_integrity"]


def test_get_crypto_universe_no_data(tmp_path) -> None:
    root = tmp_path / "lthcs"
    root.mkdir()
    out = ldata.get_crypto_universe(data_root=str(root))
    assert "error" in out
    assert "snapshots_crypto" in out["error"]


def test_get_crypto_universe_explicit_date(fake_root: str) -> None:
    out = ldata.get_crypto_universe(date="2026-05-16", data_root=fake_root)
    assert out["date"] == "2026-05-16"
    assert out["count"] == 2


def test_get_crypto_universe_missing_date_file(fake_root: str) -> None:
    out = ldata.get_crypto_universe(date="2020-01-01", data_root=fake_root)
    assert "error" in out


def test_get_crypto_universe_invalid_date(fake_root: str) -> None:
    out = ldata.get_crypto_universe(date="not-a-date", data_root=fake_root)
    assert "error" in out


def test_get_dragging_pillar_fallback_to_variable_detail(tmp_path) -> None:
    # Snapshot lacks subscores; fallback averages variable_detail rows.
    root = tmp_path / "lthcs"
    (root / "snapshots").mkdir(parents=True)
    (root / "variable_detail").mkdir(parents=True)
    snap = {
        "calc_date": "2026-05-17",
        "scores": [
            {
                "ticker": "FALL",
                "lthcs_score": 40.0,
                "band": "review",
                # no subscores key — forces fallback
                "weights_used": [0.2, 0.2, 0.2, 0.2, 0.2],
            }
        ],
    }
    (root / "snapshots" / "2026-05-17.json").write_text(json.dumps(snap))
    vd = {
        "variables": [
            {"ticker": "FALL", "pillar": "adoption_momentum", "sub_score": 70.0},
            {"ticker": "FALL", "pillar": "institutional_confidence", "sub_score": 25.0},
            {"ticker": "FALL", "pillar": "financial_evolution", "sub_score": 50.0},
            {"ticker": "FALL", "pillar": "thesis_integrity", "sub_score": 50.0},
            {"ticker": "FALL", "pillar": "des", "sub_score": 50.0},
        ]
    }
    (root / "variable_detail" / "2026-05-17.json").write_text(json.dumps(vd))
    out = ldata.get_dragging_pillar("FALL", data_root=str(root))
    assert out["dragging_pillar"] == "institutional_confidence"
    assert out["sub_score"] == 25.0
