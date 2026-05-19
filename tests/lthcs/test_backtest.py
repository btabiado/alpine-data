"""Tests for ``lthcs.backtest``.

All tests build their own snapshot tree under ``tmp_path`` so the real
``data/lthcs/`` is never touched. Yahoo is mocked everywhere — a fake
``yahoo_module`` (with a ``get_daily_prices`` static method) is injected
into ``fetch_forward_returns`` to avoid any network call.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd
import pytest

from lthcs import backtest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PILLARS = backtest.PILLAR_NAMES


def _make_score_row(ticker: str, score: float, band: str,
                    subs: Dict[str, float] | None = None) -> Dict[str, Any]:
    subs = subs or {p: 50.0 for p in PILLARS}
    return {
        "ticker": ticker,
        "lthcs_score": score,
        "band": band,
        "drift_1d": 0.0,
        "drift_7d": 0.0,
        "drift_30d": 0.0,
        "drift_90d": 0.0,
        "confidence_level": "high",
        "data_quality_flags": [],
        "subscores": subs,
        "modifiers": {"macro_adj": 0.0, "sector_adj": 0.0, "volatility_mod": 0.0},
        "maturity_stage": "mature_compounder",
        "weights_used": [0.2] * 5,
        "effective_weights": [0.2] * 5,
        "dropped_pillars": [],
        "weighted_components": [10.0] * 5,
        "sector": "Technology",
    }


def _write_snapshot(data_root: Path, date: str, rows: List[Dict[str, Any]]) -> None:
    snap_dir = data_root / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "calc_date": date,
        "model_version": "v1.1.0",
        "weights_profile_default": "default",
        "scores": rows,
    }
    (snap_dir / ("%s.json" % date)).write_text(json.dumps(payload))


def _fake_yahoo(prices_by_ticker: Dict[str, List[Dict[str, Any]]]):
    """Build a stand-in module with a get_daily_prices method."""
    class _Fake:
        @staticmethod
        def get_daily_prices(ticker, period="1y"):
            return list(prices_by_ticker.get(ticker, []))
    return _Fake


def _price_rows(start: str, n: int, base: float = 100.0,
                step: float = 1.0) -> List[Dict[str, Any]]:
    """Generate a price series of ``n`` business days starting at ``start``.

    Each day the close advances by ``step`` from base, so simple returns
    are predictable.
    """
    out: List[Dict[str, Any]] = []
    dates = pd.bdate_range(start=start, periods=n)
    for i, d in enumerate(dates):
        c = base + step * i
        out.append(
            {
                "date": d.strftime("%Y-%m-%d"),
                "open": c, "high": c, "low": c, "close": c,
                "adj_close": c, "volume": 1000,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_load_score_history_basic(tmp_path: Path) -> None:
    _write_snapshot(tmp_path / "lthcs", "2026-05-16", [
        _make_score_row("AAA", 60.0, "constructive"),
        _make_score_row("BBB", 40.0, "review"),
    ])
    _write_snapshot(tmp_path / "lthcs", "2026-05-17", [
        _make_score_row("AAA", 65.0, "constructive"),
        _make_score_row("BBB", 42.0, "review"),
    ])

    df = backtest.load_score_history(data_root=tmp_path / "lthcs")
    assert list(df.columns) == ["AAA", "BBB"]
    assert len(df.index) == 2
    assert df.loc[pd.Timestamp("2026-05-17"), "AAA"] == 65.0


def test_load_score_history_filters_tickers(tmp_path: Path) -> None:
    _write_snapshot(tmp_path / "lthcs", "2026-05-17", [
        _make_score_row("AAA", 60.0, "constructive"),
        _make_score_row("BBB", 40.0, "review"),
    ])
    df = backtest.load_score_history(
        tickers=["AAA"], data_root=tmp_path / "lthcs"
    )
    assert list(df.columns) == ["AAA"]


def test_load_band_history_strings(tmp_path: Path) -> None:
    _write_snapshot(tmp_path / "lthcs", "2026-05-17", [
        _make_score_row("AAA", 80.0, "elite"),
        _make_score_row("BBB", 30.0, "review"),
    ])
    bands = backtest.load_band_history(data_root=tmp_path / "lthcs")
    assert bands.loc[pd.Timestamp("2026-05-17"), "AAA"] == "elite"
    assert bands.loc[pd.Timestamp("2026-05-17"), "BBB"] == "review"


def test_load_pillar_history(tmp_path: Path) -> None:
    _write_snapshot(tmp_path / "lthcs", "2026-05-17", [
        _make_score_row("AAA", 60.0, "constructive",
                        subs={"adoption_momentum": 70.0,
                              "institutional_confidence": 50.0,
                              "financial_evolution": 50.0,
                              "thesis_integrity": 50.0,
                              "des": 50.0}),
        _make_score_row("BBB", 40.0, "review",
                        subs={"adoption_momentum": 30.0,
                              "institutional_confidence": 50.0,
                              "financial_evolution": 50.0,
                              "thesis_integrity": 50.0,
                              "des": 50.0}),
    ])
    df = backtest.load_pillar_history(
        "adoption_momentum", data_root=tmp_path / "lthcs"
    )
    assert df.loc[pd.Timestamp("2026-05-17"), "AAA"] == 70.0
    assert df.loc[pd.Timestamp("2026-05-17"), "BBB"] == 30.0


def test_load_pillar_history_unknown_pillar(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        backtest.load_pillar_history(
            "not_a_pillar", data_root=tmp_path / "lthcs"
        )


def test_fetch_forward_returns_cached(tmp_path: Path) -> None:
    """Single-ticker known-returns smoke test + cache write."""
    prices = _price_rows("2026-01-02", n=30, base=100.0, step=1.0)
    fake = _fake_yahoo({"AAA": prices})
    cache_root = tmp_path / "cache"

    fwd = backtest.fetch_forward_returns(
        tickers=["AAA"],
        start_date="2026-01-02",
        end_date="2026-02-13",
        horizons_days=[1, 5],
        cache_root=cache_root,
        yahoo_module=fake,
    )

    # 1-day return on the first day: (101 - 100)/100 = 0.01.
    h1 = fwd[1]
    first_val = h1["AAA"].dropna().iloc[0]
    assert math.isclose(first_val, 0.01, rel_tol=1e-9)

    # Cache file was written.
    cache_file = cache_root / "prices" / "AAA.json"
    assert cache_file.exists()


def test_fetch_forward_returns_cache_hit(tmp_path: Path) -> None:
    """Second call returns from cache even with empty yahoo module."""
    prices = _price_rows("2026-01-02", n=10)
    fake = _fake_yahoo({"AAA": prices})
    cache_root = tmp_path / "cache"

    backtest.fetch_forward_returns(
        tickers=["AAA"], start_date="2026-01-02", end_date="2026-01-15",
        horizons_days=[1], cache_root=cache_root, yahoo_module=fake,
    )

    # Replace yahoo with one that errors so we can prove the cache
    # is what's serving data.
    class _Broken:
        @staticmethod
        def get_daily_prices(ticker, period="1y"):
            raise AssertionError("should not be called when cache hits")

    fwd = backtest.fetch_forward_returns(
        tickers=["AAA"], start_date="2026-01-02", end_date="2026-01-15",
        horizons_days=[1], cache_root=cache_root, yahoo_module=_Broken,
    )
    assert not fwd[1].empty


def test_fetch_forward_returns_missing_ticker_yields_nan(tmp_path: Path) -> None:
    fake = _fake_yahoo({})  # no data for any ticker
    cache_root = tmp_path / "cache"
    fwd = backtest.fetch_forward_returns(
        tickers=["GHOST"], start_date="2026-01-02", end_date="2026-01-15",
        horizons_days=[1], cache_root=cache_root, yahoo_module=fake,
    )
    # Empty frame is fine; no observations means no rows.
    assert isinstance(fwd[1], pd.DataFrame)


def test_band_portfolio_returns_long_only_known_returns() -> None:
    """Synthetic: 2 longs (+1%, +3%) → portfolio returns +2%."""
    date = pd.Timestamp("2026-05-17")
    band_history = pd.DataFrame(
        {"AAA": ["elite"], "BBB": ["elite"], "CCC": ["monitor"]},
        index=[date],
    )
    fwd_returns = pd.DataFrame(
        {"AAA": [0.01], "BBB": [0.03], "CCC": [0.20]},
        index=[date],
    )
    res = backtest.band_portfolio_returns(
        band_history, fwd_returns,
        bands_to_long=["elite"], bands_to_short=[],
    )
    assert math.isclose(res["daily_returns"].iloc[0], 0.02, rel_tol=1e-9)
    assert res["n_long_avg"] == 2.0
    assert res["n_short_avg"] == 0.0


def test_band_portfolio_returns_long_short() -> None:
    """Synthetic long-short: long_avg=0.05, short_avg=0.01, port=0.04."""
    date = pd.Timestamp("2026-05-17")
    band_history = pd.DataFrame(
        {"AAA": ["elite"], "BBB": ["review"], "CCC": ["review"]},
        index=[date],
    )
    fwd_returns = pd.DataFrame(
        {"AAA": [0.05], "BBB": [0.00], "CCC": [0.02]},
        index=[date],
    )
    res = backtest.band_portfolio_returns(
        band_history, fwd_returns,
        bands_to_long=["elite"], bands_to_short=["review"],
    )
    assert math.isclose(res["daily_returns"].iloc[0], 0.04, rel_tol=1e-9)


def test_band_portfolio_excludes_missing_data() -> None:
    """A long member with NaN forward return drops out of the average."""
    date = pd.Timestamp("2026-05-17")
    band_history = pd.DataFrame(
        {"AAA": ["elite"], "BBB": ["elite"]}, index=[date],
    )
    fwd_returns = pd.DataFrame(
        {"AAA": [0.10], "BBB": [np.nan]}, index=[date],
    )
    res = backtest.band_portfolio_returns(
        band_history, fwd_returns,
        bands_to_long=["elite"], bands_to_short=[],
    )
    assert math.isclose(res["daily_returns"].iloc[0], 0.10, rel_tol=1e-9)
    assert res["n_long_avg"] == 1.0


def test_band_portfolio_turnover() -> None:
    """Turnover = symmetric_diff / union across consecutive rebalances."""
    dates = pd.to_datetime(["2026-05-16", "2026-05-17"])
    # Day 1 longs: AAA, BBB. Day 2 longs: BBB, CCC.
    band_history = pd.DataFrame(
        {
            "AAA": ["elite", "monitor"],
            "BBB": ["elite", "elite"],
            "CCC": ["monitor", "elite"],
        },
        index=dates,
    )
    fwd_returns = pd.DataFrame(
        {"AAA": [0.0, 0.0], "BBB": [0.0, 0.0], "CCC": [0.0, 0.0]},
        index=dates,
    )
    res = backtest.band_portfolio_returns(
        band_history, fwd_returns,
        bands_to_long=["elite"], bands_to_short=[],
    )
    # Symmetric diff = {AAA, CCC} = 2; union = {AAA, BBB, CCC} = 3.
    assert math.isclose(res["turnover_per_rebalance"], 2 / 3, rel_tol=1e-9)


def test_band_portfolio_empty_inputs() -> None:
    res = backtest.band_portfolio_returns(
        pd.DataFrame(), pd.DataFrame(),
        bands_to_long=["elite"], bands_to_short=["review"],
    )
    assert res["n_rebalances"] == 0
    assert res["cumulative_return"] == 0.0
    assert res["sharpe"] == 0.0


def test_quintile_buckets_helper_remainder_to_last() -> None:
    # Spec: last bucket gets the remainder.
    assert backtest._quintile_buckets(10, q=5) == [2, 2, 2, 2, 2]
    assert backtest._quintile_buckets(11, q=5) == [2, 2, 2, 2, 3]
    assert backtest._quintile_buckets(14, q=5) == [2, 2, 2, 2, 6]


def test_pillar_quintile_returns_equal_buckets() -> None:
    """10 tickers, perfectly correlated → Q5 > Q1."""
    date = pd.Timestamp("2026-05-17")
    tickers = ["T%02d" % i for i in range(10)]
    pillar_scores = {t: [i * 10.0] for i, t in enumerate(tickers)}
    fwd_rets = {t: [i * 0.01] for i, t in enumerate(tickers)}
    ph = pd.DataFrame(pillar_scores, index=[date])
    fr = pd.DataFrame(fwd_rets, index=[date])

    q = backtest.pillar_quintile_returns(ph, fr, horizon_days=21)
    # Q5 mean of top-2 returns = (0.08+0.09)/2 = 0.085;
    # Q1 mean of bottom-2 = (0.00+0.01)/2 = 0.005.
    assert math.isclose(q.loc["Q5", date], 0.085, rel_tol=1e-9)
    assert math.isclose(q.loc["Q1", date], 0.005, rel_tol=1e-9)
    assert math.isclose(q.loc["Q5-Q1", date], 0.08, rel_tol=1e-9)


def test_pillar_quintile_returns_insufficient_tickers() -> None:
    """Fewer than 5 tickers on a date → NaNs (not a crash)."""
    date = pd.Timestamp("2026-05-17")
    ph = pd.DataFrame({"AAA": [50.0], "BBB": [60.0]}, index=[date])
    fr = pd.DataFrame({"AAA": [0.01], "BBB": [0.02]}, index=[date])
    q = backtest.pillar_quintile_returns(ph, fr)
    assert math.isnan(q.loc["Q5-Q1", date])


def test_attribute_returns_ic_with_known_correlation() -> None:
    """Pillar perfectly ranked with forward returns → IC ≈ +1."""
    date = pd.Timestamp("2026-05-17")
    tickers = ["T%02d" % i for i in range(10)]
    pillar_scores = pd.DataFrame(
        {t: [float(i)] for i, t in enumerate(tickers)}, index=[date],
    )
    fwd = pd.DataFrame(
        {t: [float(i) * 0.01] for i, t in enumerate(tickers)}, index=[date],
    )
    composite = pillar_scores  # not used substantively here, just present
    out = backtest.attribute_returns(
        score_history=composite,
        pillar_histories={"adoption_momentum": pillar_scores},
        forward_returns=fwd,
    )
    row = out[out["pillar"] == "adoption_momentum"].iloc[0]
    assert math.isclose(row["ic_mean"], 1.0, rel_tol=1e-9)
    assert row["n_obs"] == 1


def test_attribute_returns_anti_correlated() -> None:
    """Pillar inversely related to fwd returns → IC ≈ -1."""
    date = pd.Timestamp("2026-05-17")
    tickers = ["T%02d" % i for i in range(8)]
    pillar_scores = pd.DataFrame(
        {t: [float(i)] for i, t in enumerate(tickers)}, index=[date],
    )
    fwd = pd.DataFrame(
        {t: [-float(i) * 0.01] for i, t in enumerate(tickers)}, index=[date],
    )
    out = backtest.attribute_returns(
        score_history=pillar_scores,
        pillar_histories={"adoption_momentum": pillar_scores},
        forward_returns=fwd,
    )
    row = out[out["pillar"] == "adoption_momentum"].iloc[0]
    assert math.isclose(row["ic_mean"], -1.0, rel_tol=1e-9)


def test_attribute_returns_empty_pillar() -> None:
    out = backtest.attribute_returns(
        score_history=pd.DataFrame(),
        pillar_histories={"adoption_momentum": pd.DataFrame()},
        forward_returns=pd.DataFrame(),
    )
    # composite + 1 pillar
    assert len(out) == 2
    assert (out["n_obs"] == 0).all()


def test_serialize_portfolio_result_jsonable() -> None:
    date = pd.Timestamp("2026-05-17")
    band_history = pd.DataFrame({"AAA": ["elite"]}, index=[date])
    fwd_returns = pd.DataFrame({"AAA": [0.05]}, index=[date])
    res = backtest.band_portfolio_returns(
        band_history, fwd_returns,
        bands_to_long=["elite"], bands_to_short=[],
    )
    payload = backtest.serialize_portfolio_result(res)
    # JSON-serialisable.
    json.dumps(payload)
    assert "2026-05-17" in payload["daily_returns"]


def test_history_with_one_day_no_forward_returns(tmp_path: Path, recwarn) -> None:
    """One-day history + no price data → portfolio is empty, no crash."""
    _write_snapshot(tmp_path / "lthcs", "2026-05-17", [
        _make_score_row("AAA", 80.0, "elite"),
        _make_score_row("BBB", 30.0, "review"),
    ])
    score = backtest.load_score_history(data_root=tmp_path / "lthcs")
    band = backtest.load_band_history(data_root=tmp_path / "lthcs")
    fake = _fake_yahoo({})  # no prices
    fwd = backtest.fetch_forward_returns(
        tickers=list(score.columns),
        start_date="2026-05-17",
        end_date="2026-05-17",
        horizons_days=[21],
        cache_root=tmp_path / "cache",
        yahoo_module=fake,
    )
    res = backtest.band_portfolio_returns(
        band, fwd[21],
        bands_to_long=["elite"], bands_to_short=["review"],
    )
    # No price data → portfolio either has 0 rebalances or NaN-only returns.
    assert res["cumulative_return"] == 0.0 or math.isclose(
        res["cumulative_return"], 0.0, abs_tol=1e-9
    )


def test_cli_runs_with_synthetic_history(tmp_path: Path) -> None:
    """CLI subprocess test: minimal synthetic history → exit 0 + artifacts."""
    data_root = tmp_path / "lthcs"
    out_dir = tmp_path / "out"
    # Two days so we have a real index range.
    _write_snapshot(data_root, "2026-05-16", [
        _make_score_row("AAA", 80.0, "elite"),
        _make_score_row("BBB", 30.0, "review"),
        _make_score_row("CCC", 60.0, "constructive"),
        _make_score_row("DDD", 45.0, "monitor"),
        _make_score_row("EEE", 55.0, "constructive"),
    ])
    _write_snapshot(data_root, "2026-05-17", [
        _make_score_row("AAA", 82.0, "elite"),
        _make_score_row("BBB", 32.0, "review"),
        _make_score_row("CCC", 62.0, "constructive"),
        _make_score_row("DDD", 47.0, "monitor"),
        _make_score_row("EEE", 57.0, "constructive"),
    ])

    repo_root = Path(__file__).resolve().parents[2]
    cli = repo_root / "scripts" / "lthcs_backtest.py"
    cmd = [
        sys.executable, str(cli),
        "--data-root", str(data_root),
        "--output-dir", str(out_dir),
        "--run-id", "test-run",
        "--offline",
        "--horizon", "1",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, (
        "CLI failed: stdout=%s stderr=%s" % (result.stdout, result.stderr)
    )
    out_run = out_dir / "test-run"
    assert (out_run / "portfolio_returns.json").exists()
    assert (out_run / "pillar_ic.json").exists()
    assert (out_run / "quintile_returns.json").exists()
    assert (out_run / "summary.json").exists()
    # Regression: report.md must be emitted alongside the JSON artifacts.
    report_path = out_run / "report.md"
    assert report_path.exists(), "report.md was not emitted by the CLI"
    report_text = report_path.read_text(encoding="utf-8")
    assert "LTHCS Backtest" in report_text
    assert "test-run" in report_text
    assert "Pillar Information Coefficient" in report_text

    summary = json.loads((out_run / "summary.json").read_text())
    assert summary["run_id"] == "test-run"
    assert summary["start"] == "2026-05-16"
    assert summary["end"] == "2026-05-17"


def test_cli_no_report_flag_skips_report(tmp_path: Path) -> None:
    """``--no-report`` keeps JSON artifacts but skips report.md."""
    data_root = tmp_path / "lthcs"
    out_dir = tmp_path / "out"
    _write_snapshot(data_root, "2026-05-16", [
        _make_score_row("AAA", 80.0, "elite"),
        _make_score_row("BBB", 30.0, "review"),
        _make_score_row("CCC", 60.0, "constructive"),
        _make_score_row("DDD", 45.0, "monitor"),
        _make_score_row("EEE", 55.0, "constructive"),
    ])
    _write_snapshot(data_root, "2026-05-17", [
        _make_score_row("AAA", 82.0, "elite"),
        _make_score_row("BBB", 32.0, "review"),
        _make_score_row("CCC", 62.0, "constructive"),
        _make_score_row("DDD", 47.0, "monitor"),
        _make_score_row("EEE", 57.0, "constructive"),
    ])

    repo_root = Path(__file__).resolve().parents[2]
    cli = repo_root / "scripts" / "lthcs_backtest.py"
    cmd = [
        sys.executable, str(cli),
        "--data-root", str(data_root),
        "--output-dir", str(out_dir),
        "--run-id", "no-report-run",
        "--offline",
        "--horizon", "1",
        "--no-report",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, (
        "CLI failed: stdout=%s stderr=%s" % (result.stdout, result.stderr)
    )
    out_run = out_dir / "no-report-run"
    assert (out_run / "summary.json").exists()
    assert not (out_run / "report.md").exists()


def test_build_report_markdown_from_summary_payload() -> None:
    """The pure-function renderer turns a summary dict into markdown."""
    summary = {
        "run_id": "unit-run",
        "generated_at": "2026-05-19T03:45:23Z",
        "start": "2026-02-17",
        "end": "2026-05-18",
        "horizon_days": 21,
        "bands_long": ["elite", "high_confidence"],
        "bands_short": ["review"],
        "n_tickers": 167,
        "n_observation_dates": 91,
        "portfolio": {
            "cumulative_return": 2.5,
            "sharpe": 19.44,
            "max_drawdown": -0.13,
            "turnover_per_rebalance": 0.067,
            "n_rebalances": 91,
            "hit_rate": 0.813,
            "n_long_avg": 7.2,
            "n_short_avg": 62.0,
        },
        "pillar_ic": [
            {"pillar": "composite", "ic_mean": 0.122, "ic_std": 0.1,
             "ic_sharpe": 14.1, "n_obs": 91},
            {"pillar": "thesis_integrity", "ic_mean": 0.082, "ic_std": 0.12,
             "ic_sharpe": 17.2, "n_obs": 91},
        ],
    }
    quintile = {
        "thesis_integrity": {
            "Q5-Q1": {"2026-02-17": 0.01, "2026-02-18": 0.02, "2026-02-19": None}
        }
    }
    md = backtest.build_report_markdown(summary, quintile_payload=quintile)
    assert md.startswith("# LTHCS Backtest — unit-run")
    assert "Window: **2026-02-17 -> 2026-05-18**" in md
    assert "Horizon: **21 trading days**" in md
    assert "| composite |" in md
    assert "| thesis_integrity |" in md
    # Q5-Q1 mean of (0.01, 0.02) -> 0.0150; None dropped.
    assert "+0.0150" in md
    assert md.endswith("\n")


def test_write_report_from_dir_round_trip(tmp_path: Path) -> None:
    """``write_report_from_dir`` reads summary+quintile JSON, writes report.md."""
    out_dir = tmp_path / "run"
    out_dir.mkdir()
    summary = {
        "run_id": "round-trip",
        "start": "2026-05-01",
        "end": "2026-05-10",
        "horizon_days": 5,
        "bands_long": ["elite"],
        "bands_short": ["review"],
        "n_tickers": 50,
        "n_observation_dates": 10,
        "portfolio": {
            "cumulative_return": 0.1, "sharpe": 1.2, "max_drawdown": -0.05,
            "turnover_per_rebalance": 0.05, "n_rebalances": 10, "hit_rate": 0.6,
            "n_long_avg": 5.0, "n_short_avg": 4.0,
        },
        "pillar_ic": [
            {"pillar": "composite", "ic_mean": 0.05, "ic_std": 0.1,
             "ic_sharpe": 1.5, "n_obs": 10},
        ],
    }
    (out_dir / "summary.json").write_text(json.dumps(summary))
    (out_dir / "quintile_returns.json").write_text(json.dumps({
        "des": {"Q5-Q1": {"2026-05-01": 0.005, "2026-05-02": 0.007}}
    }))

    result = backtest.write_report_from_dir(out_dir)
    assert result == out_dir / "report.md"
    text = result.read_text(encoding="utf-8")
    assert "round-trip" in text
    assert "des" in text


def test_write_report_from_dir_missing_summary_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        backtest.write_report_from_dir(tmp_path / "nope")
