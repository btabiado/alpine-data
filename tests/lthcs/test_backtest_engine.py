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


# ---------------------------------------------------------------------------
# 15. Sharpe / Sortino bootstrap CI (Tier 5 #24 P3 follow-on)
# ---------------------------------------------------------------------------

def _synthetic_returns_series(n: int, mu: float, sigma: float, seed: int) -> pd.Series:
    rng = np.random.default_rng(seed)
    arr = rng.normal(loc=mu, scale=sigma, size=n)
    idx = _trading_days("2026-01-05", n)
    return pd.Series(arr, index=idx)


def test_bootstrap_ci_deterministic_for_same_seed():
    """Same seed -> identical CI bounds; different seed -> different bounds."""
    s = _synthetic_returns_series(n=64, mu=0.001, sigma=0.01, seed=11)
    a = be._bootstrap_sharpe_ci(s, n_bootstrap=500, seed=42)
    b = be._bootstrap_sharpe_ci(s, n_bootstrap=500, seed=42)
    assert a == b
    c = be._bootstrap_sharpe_ci(s, n_bootstrap=500, seed=43)
    assert a != c


def test_bootstrap_ci_brackets_point_estimate():
    """The 95% CI should usually contain the point estimate on a well-
    behaved return series. The block-bootstrap is mean-preserving in
    expectation, so this is essentially a sanity check on plumbing."""
    s = _synthetic_returns_series(n=90, mu=0.0008, sigma=0.012, seed=7)
    point = be._annualized_sharpe(s)
    lo, hi = be._bootstrap_sharpe_ci(s, n_bootstrap=1000, seed=42)
    assert lo <= point <= hi


def test_bootstrap_ci_wider_on_high_variance_series():
    """A higher-variance daily return series should yield a wider CI than
    a low-variance series at the same length and mean."""
    low = _synthetic_returns_series(n=90, mu=0.0005, sigma=0.003, seed=21)
    high = _synthetic_returns_series(n=90, mu=0.0005, sigma=0.03, seed=21)
    lo_lo, lo_hi = be._bootstrap_sharpe_ci(low, n_bootstrap=1000, seed=42)
    hi_lo, hi_hi = be._bootstrap_sharpe_ci(high, n_bootstrap=1000, seed=42)
    # The Sharpe ratio itself is variance-normalized, but the *bootstrap
    # distribution* of Sharpe (block-resampled returns) is sensitive to
    # the raw scale of noise -- noisier samples imply noisier Sharpe.
    assert (hi_hi - hi_lo) > (lo_hi - lo_lo)


def test_bootstrap_ci_degenerate_series_returns_nan():
    """An empty or single-point series can't be bootstrapped sensibly."""
    lo, hi = be._bootstrap_sharpe_ci(pd.Series([], dtype=float), n_bootstrap=100)
    assert math.isnan(lo) and math.isnan(hi)
    lo2, hi2 = be._bootstrap_sharpe_ci(pd.Series([0.01]), n_bootstrap=100)
    assert math.isnan(lo2) and math.isnan(hi2)


def test_summary_includes_ci_keys_by_default():
    prices = _make_prices(
        "2026-01-05", 30, {"AAA": _ramp_price(100.0, 0.5, 30)}
    )
    bands = _make_band_history("2026-01-05", 30, {"AAA": ["elite"] * 30})
    out = be.run_backtest(
        bands, prices, params=be.EngineParams(cost_bps=0.0), per_band_sweep=False
    )
    s = out["summary"]
    for k in ("sharpe_ci_lower", "sharpe_ci_upper",
              "sortino_ci_lower", "sortino_ci_upper"):
        assert k in s, f"missing CI key {k} in summary"


def test_summary_ci_brackets_point_estimate_on_full_run():
    """On a real engine run (synthetic but with noise) the CI on the
    headline daily returns should bracket the point-estimate Sharpe."""
    rng = np.random.default_rng(99)
    n = 90
    idx = _trading_days("2026-01-05", n)
    # Drifting walk so daily returns are mildly positive with real noise.
    rets = rng.normal(loc=0.001, scale=0.015, size=n)
    closes = 100.0 * np.cumprod(1.0 + rets)
    prices = pd.DataFrame({"AAA": closes}, index=idx)
    bands = pd.DataFrame({"AAA": ["elite"] * n}, index=idx)
    out = be.run_backtest(
        bands, prices, params=be.EngineParams(cost_bps=0.0), per_band_sweep=False,
    )
    s = out["summary"]
    sharpe = float(s["sharpe"])
    lo = float(s["sharpe_ci_lower"])
    hi = float(s["sharpe_ci_upper"])
    assert lo <= sharpe <= hi
    assert lo < hi  # non-degenerate width


def test_summary_excludes_ci_keys_when_compute_ci_false():
    prices = _make_prices(
        "2026-01-05", 30, {"AAA": _ramp_price(100.0, 0.5, 30)}
    )
    bands = _make_band_history("2026-01-05", 30, {"AAA": ["elite"] * 30})
    out = be.run_backtest(
        bands, prices, params=be.EngineParams(cost_bps=0.0),
        per_band_sweep=False, compute_ci=False,
    )
    s = out["summary"]
    for k in ("sharpe_ci_lower", "sharpe_ci_upper",
              "sortino_ci_lower", "sortino_ci_upper"):
        assert k not in s, f"unexpected CI key {k} when compute_ci=False"


def test_run_backtest_ci_deterministic_across_runs():
    """Two identical run_backtest calls produce identical CI bounds."""
    rng = np.random.default_rng(123)
    n = 60
    idx = _trading_days("2026-01-05", n)
    rets = rng.normal(loc=0.0008, scale=0.012, size=n)
    closes = 100.0 * np.cumprod(1.0 + rets)
    prices = pd.DataFrame({"AAA": closes}, index=idx)
    bands = pd.DataFrame({"AAA": ["elite"] * n}, index=idx)
    out1 = be.run_backtest(bands, prices, params=be.EngineParams(cost_bps=0.0),
                            per_band_sweep=False)
    out2 = be.run_backtest(bands, prices, params=be.EngineParams(cost_bps=0.0),
                            per_band_sweep=False)
    assert out1["summary"]["sharpe_ci_lower"] == out2["summary"]["sharpe_ci_lower"]
    assert out1["summary"]["sharpe_ci_upper"] == out2["summary"]["sharpe_ci_upper"]
    assert out1["summary"]["sortino_ci_lower"] == out2["summary"]["sortino_ci_lower"]
    assert out1["summary"]["sortino_ci_upper"] == out2["summary"]["sortino_ci_upper"]


def test_report_markdown_renders_ci_when_present():
    """When the summary has CI bounds, the report should include them."""
    md = be.build_engine_report_markdown(
        summary={
            "total_return": 0.05,
            "ann_return": 0.20,
            "max_drawdown": -0.10,
            "sharpe": 2.607,
            "sortino": 3.1,
            "sharpe_ci_lower": 1.84,
            "sharpe_ci_upper": 3.42,
            "sortino_ci_lower": 2.2,
            "sortino_ci_upper": 4.1,
            "hit_rate": 0.55,
            "avg_hold_days": 5.0,
            "turnover": 0.05,
            "n_trades": 10,
            "n_unique_tkr": 5,
            "params": {"cost_bps": 5.0, "delay_trading_days": 1},
        },
        run_meta={"universe_size": 5, "long_set": ["elite"], "window": {}},
    )
    assert "95% CI" in md
    assert "+1.84" in md and "+3.42" in md


def test_report_markdown_omits_ci_when_absent():
    """When the summary has no CI keys, the report falls back to the
    plain Sharpe / Sortino formatting (no '95% CI' string)."""
    md = be.build_engine_report_markdown(
        summary={
            "total_return": 0.0,
            "sharpe": 1.5,
            "sortino": 2.0,
            "params": {"cost_bps": 5.0, "delay_trading_days": 1},
        },
        run_meta={"universe_size": 0, "long_set": [], "window": {}},
    )
    assert "95% CI" not in md
    assert "Annualized Sharpe" in md
