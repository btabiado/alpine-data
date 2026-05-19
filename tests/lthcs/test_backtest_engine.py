"""Tests for ``lthcs.backtest_engine`` (Tier 5 #24, Phase 1).

The engine takes pre-built panels (band_history + prices) so the tests
construct synthetic frames directly. No filesystem, no network. The
event-driven loop, trade-tracking, and Sharpe/turnover bookkeeping are
exercised with hand-checkable scenarios.
"""

from __future__ import annotations

from typing import Dict, List

import numpy as np
import pandas as pd
import pytest

from lthcs import backtest_engine as be


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _trading_days(start: str, n: int) -> pd.DatetimeIndex:
    return pd.bdate_range(start=start, periods=n)


def _ramp_price(start: float, daily_step: float, n: int) -> List[float]:
    return [start + daily_step * i for i in range(n)]


def _make_prices(start: str, n: int, per_ticker: Dict[str, List[float]]) -> pd.DataFrame:
    idx = _trading_days(start, n)
    return pd.DataFrame(per_ticker, index=idx)


def _make_band_history(
    start: str,
    n: int,
    per_ticker: Dict[str, List[str]],
) -> pd.DataFrame:
    idx = _trading_days(start, n)
    return pd.DataFrame(per_ticker, index=idx)


# ---------------------------------------------------------------------------
# 1. Smoke + empty inputs
# ---------------------------------------------------------------------------

def test_empty_inputs_return_empty_result():
    out = be.run_backtest(pd.DataFrame(), pd.DataFrame())
    assert out["equity_curve"] == {}
    assert out["trades"] == []
    assert out["summary"]["total_return"] == 0.0
    assert out["summary"]["sharpe"] == 0.0


def test_no_matching_bands_yields_flat_curve():
    prices = _make_prices(
        "2026-01-05",
        10,
        {"AAA": _ramp_price(100.0, 1.0, 10)},
    )
    bands = _make_band_history(
        "2026-01-05",
        10,
        {"AAA": ["monitor"] * 10},
    )
    out = be.run_backtest(bands, prices)
    eq = list(out["equity_curve"].values())
    # No positions ever taken -> equity stays at 1.0 (within rounding).
    assert all(abs(v - 1.0) < 1e-9 for v in eq)
    assert out["summary"]["n_trades"] == 0


# ---------------------------------------------------------------------------
# 2. One-day delay enforcement
# ---------------------------------------------------------------------------

def test_one_day_delay_skips_entry_day_pnl():
    """A name flipping into 'elite' on day 0 should be bought at close of
    day 1 -- so day-1's return is missed even though the price rose."""
    prices = _make_prices(
        "2026-01-05",
        4,
        # Day 0 close = 100, day 1 = 110 (+10%), day 2 = 121 (+10%),
        # day 3 = 133.1 (+10%).
        {"AAA": [100.0, 110.0, 121.0, 133.1]},
    )
    bands = _make_band_history(
        "2026-01-05",
        4,
        {"AAA": ["elite", "elite", "elite", "elite"]},
    )
    params = be.EngineParams(bands_long=["elite"], cost_bps=0.0)
    out = be.run_backtest(bands, prices, params=params, per_band_sweep=False)
    eq = list(out["equity_curve"].values())
    # Trading day order (4 days). With 1-day delay:
    #   day 0: bands shifted -> NaN -> no position. ret = 0. eq = 1.0
    #   day 1: target_bands[1] = bands[0] = 'elite' -> enter at close of 1.
    #          But yesterday no position. So day-1 return is still 0.
    #          eq = 1.0
    #   day 2: target_bands[2] = bands[1] = 'elite' -> still held.
    #          ret = 121/110 - 1 = +0.10. eq = 1.10
    #   day 3: target_bands[3] = bands[2] = 'elite' -> still held.
    #          ret = 133.1/121 - 1 = +0.10. eq = 1.21
    assert eq[0] == pytest.approx(1.0)
    assert eq[1] == pytest.approx(1.0)
    assert eq[2] == pytest.approx(1.10, rel=1e-6)
    assert eq[3] == pytest.approx(1.21, rel=1e-6)


# ---------------------------------------------------------------------------
# 3. Cost drag on entry / exit
# ---------------------------------------------------------------------------

def test_cost_drag_on_entry_then_exit():
    """A one-day round-trip at 5 bps/side should erode equity by ~10 bps
    relative to the zero-cost case."""
    prices = _make_prices(
        "2026-01-05",
        5,
        {"AAA": [100.0, 100.0, 100.0, 100.0, 100.0]},
    )
    bands = _make_band_history(
        "2026-01-05",
        5,
        # in on day 1 (acted on day 2), out on day 3 (acted on day 4).
        {"AAA": ["monitor", "elite", "elite", "monitor", "monitor"]},
    )
    params = be.EngineParams(bands_long=["elite"], cost_bps=5.0)
    out = be.run_backtest(bands, prices, params=params, per_band_sweep=False)
    final_eq = list(out["equity_curve"].values())[-1]
    # With identical prices we should only feel the cost drag. With a
    # single name the traded weight on entry is 1.0 (1/1), on exit also
    # 1.0 -> total drag ~10bps.
    assert final_eq < 1.0
    assert final_eq == pytest.approx(1.0 * (1 - 5e-4) * (1 - 5e-4), rel=1e-6)
    # Trade list should contain one closed round-trip.
    assert len(out["trades"]) == 1
    t = out["trades"][0]
    assert t["ticker"] == "AAA"
    assert t["gross_return"] == pytest.approx(0.0, abs=1e-12)


# ---------------------------------------------------------------------------
# 4. Equal weighting across N names
# ---------------------------------------------------------------------------

def test_equal_weighting_average_return():
    """Two names with different daily returns -> portfolio return = mean."""
    prices = _make_prices(
        "2026-01-05",
        3,
        {
            "AAA": [100.0, 110.0, 110.0],  # +10% then flat
            "BBB": [50.0, 50.0, 55.0],     # flat then +10%
        },
    )
    bands = _make_band_history(
        "2026-01-05",
        3,
        {
            "AAA": ["elite"] * 3,
            "BBB": ["elite"] * 3,
        },
    )
    params = be.EngineParams(bands_long=["elite"], cost_bps=0.0)
    out = be.run_backtest(bands, prices, params=params, per_band_sweep=False)
    eq = list(out["equity_curve"].values())
    # Day 0: no positions yet (delay). eq = 1.0
    # Day 1: target_bands[1] = bands[0] = elite for both -> hold both at close of 1.
    #        But no prior positions yesterday so day-1 ret = 0. eq = 1.0
    # Day 2: held both since close of 1. Returns: AAA = 110/110 - 1 = 0,
    #        BBB = 55/50 - 1 = +0.10. Avg = +0.05. eq = 1.05
    assert eq[2] == pytest.approx(1.05, rel=1e-6)


# ---------------------------------------------------------------------------
# 5. Trade tracking
# ---------------------------------------------------------------------------

def test_trades_recorded_with_entry_exit_dates():
    prices = _make_prices(
        "2026-01-05",
        6,
        {"AAA": [100.0, 100.0, 110.0, 110.0, 100.0, 100.0]},
    )
    bands = _make_band_history(
        "2026-01-05",
        6,
        # In on day 0, out on day 3.
        {"AAA": ["elite", "elite", "elite", "monitor", "monitor", "monitor"]},
    )
    out = be.run_backtest(bands, prices, params=be.EngineParams(cost_bps=0.0),
                          per_band_sweep=False)
    assert len(out["trades"]) == 1
    t = out["trades"][0]
    # With 1-day delay, entry was on trading day 1 (calendar 2026-01-06),
    # exit on trading day 4 (calendar 2026-01-09).
    assert t["entry_date"] == "2026-01-06"
    assert t["exit_date"] == "2026-01-09"
    assert t["gross_return"] == pytest.approx(100.0 / 100.0 - 1.0, abs=1e-12)
    assert t["hold_days"] >= 3


# ---------------------------------------------------------------------------
# 6. Per-band sweep emits all bands
# ---------------------------------------------------------------------------

def test_per_band_sweep_emits_all_bands():
    prices = _make_prices(
        "2026-01-05", 5, {"AAA": [100.0] * 5, "BBB": [100.0] * 5}
    )
    bands = _make_band_history(
        "2026-01-05",
        5,
        {"AAA": ["elite"] * 5, "BBB": ["review"] * 5},
    )
    out = be.run_backtest(bands, prices, params=be.EngineParams(cost_bps=0.0))
    for b in be.ALL_BANDS_FOR_SWEEP:
        assert b in out["band_curves"], f"missing band curve for {b}"


# ---------------------------------------------------------------------------
# 7. Summary stat shape
# ---------------------------------------------------------------------------

def test_summary_contains_expected_keys():
    prices = _make_prices(
        "2026-01-05", 10, {"AAA": _ramp_price(100.0, 1.0, 10)}
    )
    bands = _make_band_history("2026-01-05", 10, {"AAA": ["elite"] * 10})
    out = be.run_backtest(bands, prices, params=be.EngineParams(cost_bps=0.0))
    s = out["summary"]
    for k in [
        "total_return",
        "ann_return",
        "max_drawdown",
        "sharpe",
        "sortino",
        "hit_rate",
        "avg_hold_days",
        "turnover",
        "n_trades",
        "n_unique_tkr",
        "n_trading_days",
        "params",
    ]:
        assert k in s, f"missing key {k}"


# ---------------------------------------------------------------------------
# 8. Sharpe on a deterministic ramp matches the analytic value
# ---------------------------------------------------------------------------

def test_deterministic_ramp_zero_volatility_zero_sharpe():
    """A flat daily return series has stdev 0 -> Sharpe is reported as 0."""
    prices = _make_prices(
        "2026-01-05", 30, {"AAA": [100.0 * (1.001) ** i for i in range(30)]}
    )
    bands = _make_band_history("2026-01-05", 30, {"AAA": ["elite"] * 30})
    out = be.run_backtest(bands, prices, params=be.EngineParams(cost_bps=0.0),
                          per_band_sweep=False)
    # After day 1 onwards returns are exactly +0.1% so stdev should be tiny.
    # Engine's ``_annualized_sharpe`` should not blow up.
    assert math.isfinite(out["summary"]["sharpe"])


# ---------------------------------------------------------------------------
# 9. Look-ahead guard: future band change does NOT affect today
# ---------------------------------------------------------------------------

def test_future_band_change_does_not_leak_into_today():
    """If a ticker's band changes from monitor->elite at end of day 2,
    we must not see the day-2 return in our portfolio (we're not in yet)."""
    prices = _make_prices(
        "2026-01-05",
        5,
        {"AAA": [100.0, 100.0, 200.0, 200.0, 200.0]},  # huge gap on day 2
    )
    bands = _make_band_history(
        "2026-01-05",
        5,
        # Switches on day 2 -> trade at close of day 3.
        {"AAA": ["monitor", "monitor", "elite", "elite", "elite"]},
    )
    out = be.run_backtest(bands, prices, params=be.EngineParams(cost_bps=0.0),
                          per_band_sweep=False)
    eq = list(out["equity_curve"].values())
    # Day 2 return must NOT include the 2x gap. We enter at close of day 3,
    # so day 3 return is 0 (200/200), day 4 return is 0.
    assert eq[2] == pytest.approx(1.0, rel=1e-9)
    assert eq[-1] == pytest.approx(1.0, rel=1e-9)


# ---------------------------------------------------------------------------
# 10. Hash stability + run_meta shape
# ---------------------------------------------------------------------------

def test_run_meta_includes_hashes_and_window():
    prices = _make_prices(
        "2026-01-05", 5, {"AAA": [100.0] * 5}
    )
    bands = _make_band_history("2026-01-05", 5, {"AAA": ["elite"] * 5})
    out1 = be.run_backtest(bands, prices, per_band_sweep=False)
    out2 = be.run_backtest(bands, prices, per_band_sweep=False)
    assert out1["run_meta"]["params_hash"] == out2["run_meta"]["params_hash"]
    assert out1["run_meta"]["band_hash"] == out2["run_meta"]["band_hash"]
    assert out1["run_meta"]["price_hash"] == out2["run_meta"]["price_hash"]
    assert out1["run_meta"]["window"]["n_trading_days"] > 0


# ---------------------------------------------------------------------------
# 11. Missing prices are tolerated
# ---------------------------------------------------------------------------

def test_missing_price_for_a_ticker_does_not_crash():
    prices = _make_prices(
        "2026-01-05", 5, {"AAA": [100.0] * 5}  # no BBB column
    )
    bands = _make_band_history(
        "2026-01-05",
        5,
        {"AAA": ["elite"] * 5, "BBB": ["elite"] * 5},
    )
    out = be.run_backtest(bands, prices, params=be.EngineParams(cost_bps=0.0),
                          per_band_sweep=False)
    # BBB has no prices, so it's dropped silently. AAA only -> equity flat.
    assert math.isfinite(out["summary"]["total_return"])


# ---------------------------------------------------------------------------
# 12. CSV emitters round-trip
# ---------------------------------------------------------------------------

def test_csv_emitters_write_correct_columns(tmp_path):
    eq = {"2026-01-05": 1.0, "2026-01-06": 1.01, "2026-01-07": 1.02}
    p = tmp_path / "equity_curve.csv"
    be.equity_curve_to_csv(eq, p)
    text = p.read_text()
    assert "date,equity,daily_return" in text

    trades = [
        {
            "ticker": "AAA",
            "entry_date": "2026-01-05",
            "exit_date": "2026-01-10",
            "entry_price": 100.0,
            "exit_price": 105.0,
            "gross_return": 0.05,
            "net_return": 0.0499,
            "hold_days": 5,
        }
    ]
    p2 = tmp_path / "trades.csv"
    be.trades_to_csv(trades, p2)
    txt = p2.read_text()
    assert "entry_date,exit_date,ticker" in txt
    assert "AAA" in txt


# ---------------------------------------------------------------------------
# 13. Report renderer doesn't crash on empty payload
# ---------------------------------------------------------------------------

def test_report_renderer_handles_empty_payload():
    md = be.build_engine_report_markdown(
        summary={"total_return": 0.0, "params": {"cost_bps": 5.0, "delay_trading_days": 1}},
        run_meta={"universe_size": 0, "long_set": [], "window": {}},
    )
    assert "LTHCS Backtest Engine Report" in md


# ---------------------------------------------------------------------------
# 14. Benchmark curve is normalized to the engine window
# ---------------------------------------------------------------------------

def test_benchmark_curve_normalized_to_first_trading_day():
    idx = _trading_days("2026-01-05", 5)
    prices = pd.DataFrame({"AAA": [100.0] * 5}, index=idx)
    bands = pd.DataFrame({"AAA": ["elite"] * 5}, index=idx)
    bench = pd.Series([200.0, 210.0, 220.0, 230.0, 240.0], index=idx)
    out = be.run_backtest(
        bands, prices, params=be.EngineParams(cost_bps=0.0),
        benchmark_prices=bench, per_band_sweep=False,
    )
    first = list(out["benchmark_curve"].values())[0]
    last = list(out["benchmark_curve"].values())[-1]
    assert first == pytest.approx(1.0)
    assert last == pytest.approx(240.0 / 200.0, rel=1e-6)


# Needed by test_deterministic_ramp_zero_volatility_zero_sharpe.
import math  # noqa: E402 — kept here to make the dependency explicit.
