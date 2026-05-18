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
from typing import Any, Dict

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
