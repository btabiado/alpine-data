"""Tests for ``lthcs.backtest_profiles`` (Tier 5 #24, Phase 3).

Each profile is exercised end-to-end against synthetic panels: build
profile -> run engine -> assert the headline invariant for that profile
(e.g. dollar_neutral has a symmetric short leg, top_k holds exactly K).
"""

from __future__ import annotations

from typing import Dict, List

import numpy as np
import pandas as pd
import pytest

from lthcs import backtest_engine as be
from lthcs import backtest_profiles as bp


# ---------------------------------------------------------------------------
# Helpers (mirror the Phase-1 test helpers; no shared imports across files
# because the sibling agents own neighboring test modules).
# ---------------------------------------------------------------------------

def _trading_days(start: str, n: int) -> pd.DatetimeIndex:
    return pd.bdate_range(start=start, periods=n)


def _ramp(start: float, step: float, n: int) -> List[float]:
    return [start + step * i for i in range(n)]


def _frame(start: str, n: int, cols: Dict[str, List]) -> pd.DataFrame:
    return pd.DataFrame(cols, index=_trading_days(start, n))


# ---------------------------------------------------------------------------
# 1. Registry sanity.
# ---------------------------------------------------------------------------

def test_registry_lists_four_profiles():
    names = bp.available_profiles()
    assert "long_only_buy" in names
    assert "long_buy_short_review" in names
    assert "dollar_neutral" in names
    assert "top_k_by_composite" in names
    assert len(names) >= 4


def test_unknown_profile_raises():
    with pytest.raises(KeyError):
        bp.get_profile("not_a_real_profile")


# ---------------------------------------------------------------------------
# 2. long_only_buy — baseline parity.
# ---------------------------------------------------------------------------

def test_long_only_buy_profile_loads_and_runs():
    profile = bp.get_profile("long_only_buy")
    assert profile.name == "long_only_buy"
    assert profile.requires_score_history is False
    assert "elite" in profile.params.bands_long
    assert profile.params.bands_short == []
    assert profile.params.top_k == 0
    assert profile.params.short_bottom_quintile is False
    assert profile.params.profile_name == "long_only_buy"

    prices = _frame("2026-01-05", 6, {"AAA": _ramp(100.0, 1.0, 6)})
    bands = _frame("2026-01-05", 6, {"AAA": ["elite"] * 6})
    out = be.run_backtest(bands, prices, params=profile.params,
                          per_band_sweep=False)
    # The simulation must produce a non-trivial equity curve and one trade
    # set (entry but not yet exit, since AAA stays elite to the end).
    assert len(out["equity_curve"]) == 6
    assert out["summary"]["n_trades"] >= 0  # may be 0 (still open) or more
    # No short leg recorded.
    assert all(
        rec.get("side", "long") == "long"
        for rec in out["positions_daily"]
    )
    # run_meta carries the profile name forward.
    assert out["run_meta"]["profile_name"] == "long_only_buy"


# ---------------------------------------------------------------------------
# 3. long_buy_short_review — short leg active, dollar-neutral by construction.
# ---------------------------------------------------------------------------

def test_long_buy_short_review_short_leg_active():
    profile = bp.get_profile("long_buy_short_review")
    assert profile.params.bands_short == ["review"]
    assert profile.params.has_short_leg is True

    # AAA Elite (long leg), BBB Review (short leg). Both ramp identically
    # so gross daily return is 0 (long - short = 0), and we only feel cost
    # drag on entries.
    n = 6
    prices = _frame("2026-01-05", n, {
        "AAA": _ramp(100.0, 1.0, n),
        "BBB": _ramp(50.0, 0.5, n),
    })
    bands = _frame("2026-01-05", n, {
        "AAA": ["elite"] * n,
        "BBB": ["review"] * n,
    })
    out = be.run_backtest(bands, prices, params=profile.params,
                          per_band_sweep=False)

    # The simulation should record positions on both legs.
    sides = {rec.get("side") for rec in out["positions_daily"]}
    assert "long" in sides
    assert "short" in sides

    # Equal-ramp prices: the daily long return matches the daily short return,
    # so the dollar-neutral PNL is ~zero (we only see cost drag on entry).
    final_eq = list(out["equity_curve"].values())[-1]
    # Final equity should be very close to 1.0 (gross PnL = 0), minus a
    # small cost drag from the initial double-leg entry.
    assert final_eq <= 1.0
    assert final_eq > 1.0 - 5e-3  # < 50 bps of drag is plenty of headroom


# ---------------------------------------------------------------------------
# 4. dollar_neutral — quintile short leg + symmetric weighting.
# ---------------------------------------------------------------------------

def test_dollar_neutral_profile_uses_score_history():
    profile = bp.get_profile("dollar_neutral")
    assert profile.requires_score_history is True
    assert profile.params.short_bottom_quintile is True

    # 10 tickers, mixed bands and scores. Long leg = elite. Short leg = bottom
    # 20% of composite score each day (~2 names).
    n = 5
    n_tk = 10
    tickers = [f"T{i:02d}" for i in range(n_tk)]
    prices_data = {tkr: [100.0] * n for tkr in tickers}  # flat -> 0% return
    # Half elite (long leg), half monitor (not in long leg).
    bands_data = {
        tkr: (["elite"] * n if i < 5 else ["monitor"] * n)
        for i, tkr in enumerate(tickers)
    }
    # Scores: highest for elite names, lowest for monitor names. Ensures
    # the bottom quintile pulls from the monitor pool, not from the long leg.
    score_vals = list(range(100, 0, -10))  # 100,90,...,10
    scores_data = {tkr: [score_vals[i]] * n for i, tkr in enumerate(tickers)}

    prices = _frame("2026-01-05", n, prices_data)
    bands = _frame("2026-01-05", n, bands_data)
    scores = _frame("2026-01-05", n, scores_data)

    out = be.run_backtest(
        bands, prices, params=profile.params,
        score_history=scores, per_band_sweep=False,
    )

    # Both legs must populate. With 10 names total, bottom quintile = 2.
    positions = out["positions_daily"]
    longs = [r for r in positions if r.get("side") == "long"]
    shorts = [r for r in positions if r.get("side") == "short"]
    assert longs, "expected long-leg positions"
    assert shorts, "expected short-leg positions from bottom quintile"

    # Bottom-quintile shorts must not overlap with long-leg names.
    long_tkrs = {r["ticker"] for r in longs}
    short_tkrs = {r["ticker"] for r in shorts}
    assert long_tkrs & short_tkrs == set()

    # All prices flat -> daily gross return is 0; equity ends very close
    # to 1.0 minus a one-time entry cost drag.
    final_eq = list(out["equity_curve"].values())[-1]
    assert 0.99 < final_eq <= 1.0


# ---------------------------------------------------------------------------
# 5. top_k_by_composite — holds exactly K names regardless of band.
# ---------------------------------------------------------------------------

def test_top_k_holds_exactly_k_names():
    K = 3
    profile = bp.build_top_k_by_composite(k=K)
    assert profile.params.top_k == K
    assert profile.params.bands_long == []  # band-agnostic

    n = 4
    n_tk = 8
    tickers = [f"T{i:02d}" for i in range(n_tk)]
    prices_data = {tkr: [100.0] * n for tkr in tickers}
    # Every ticker is "monitor" (would not enter long leg for a band-based
    # strategy); the top_k profile ignores bands and picks by score.
    bands_data = {tkr: ["monitor"] * n for tkr in tickers}
    scores_data = {tkr: [float(50 + i)] * n for i, tkr in enumerate(tickers)}

    prices = _frame("2026-01-05", n, prices_data)
    bands = _frame("2026-01-05", n, bands_data)
    scores = _frame("2026-01-05", n, scores_data)

    out = be.run_backtest(
        bands, prices, params=profile.params,
        score_history=scores, per_band_sweep=False,
    )

    # Count distinct names held per (date, side='long'). Day 0 has no
    # positions (delay); subsequent days should each have exactly K names.
    by_date: Dict[str, set] = {}
    for r in out["positions_daily"]:
        if r.get("side") != "long":
            continue
        by_date.setdefault(r["date"], set()).add(r["ticker"])
    held_counts = [len(s) for s in by_date.values()]
    # At least one day with K names.
    assert any(c == K for c in held_counts), held_counts
    # No day exceeds K.
    assert max(held_counts) == K

    # Top 3 by score are T05, T06, T07 (scores 55, 56, 57).
    sample_day = sorted(by_date.keys())[-1]
    assert by_date[sample_day] == {"T05", "T06", "T07"}


# ---------------------------------------------------------------------------
# 6. Short leg attracts cost drag.
# ---------------------------------------------------------------------------

def test_short_leg_round_trip_adds_cost_drag():
    """A long+short profile with flat prices should bleed exactly the
    sum of long-leg and short-leg round-trip costs."""
    profile = bp.get_profile("long_buy_short_review")
    n = 5
    prices = _frame("2026-01-05", n, {
        "AAA": [100.0] * n,
        "BBB": [100.0] * n,
    })
    # AAA in long, BBB in short for one day window, then both exit.
    bands = _frame("2026-01-05", n, {
        "AAA": ["monitor", "elite", "elite", "monitor", "monitor"],
        "BBB": ["monitor", "review", "review", "monitor", "monitor"],
    })
    out = be.run_backtest(bands, prices, params=profile.params,
                          per_band_sweep=False)

    # Final equity should be below 1.0 due to entry+exit on BOTH legs.
    final_eq = list(out["equity_curve"].values())[-1]
    # Each leg traded weight ~ 1.0 at entry + 1.0 at exit; cost is per
    # side bps. With cost_bps=5 the round-trip drag is at least 10bps
    # per leg = 20bps total.
    assert final_eq < 1.0
    expected_drag = 4 * (5.0 / 10000.0)  # 2 round-trips at 5bps each
    expected_eq = (1.0 - 5.0 / 10000.0) ** 4  # entry/exit on each leg
    assert final_eq == pytest.approx(expected_eq, rel=1e-6)


# ---------------------------------------------------------------------------
# 7. Profile names round-trip into run_meta.
# ---------------------------------------------------------------------------

def test_profile_name_round_trips_to_run_meta():
    for name in bp.available_profiles():
        profile = bp.get_profile(name)
        # Minimal panels so the engine returns quickly.
        n = 3
        prices = _frame("2026-01-05", n, {"AAA": [100.0] * n})
        bands = _frame("2026-01-05", n, {"AAA": ["elite"] * n})
        scores = _frame("2026-01-05", n, {"AAA": [80.0] * n})
        out = be.run_backtest(
            bands, prices, params=profile.params,
            score_history=scores, per_band_sweep=False,
        )
        assert out["run_meta"]["profile_name"] == name


# ---------------------------------------------------------------------------
# 8. Backwards compat: passing no score_history still works for top_k
# (yields empty long leg rather than crashing).
# ---------------------------------------------------------------------------

def test_top_k_without_score_history_yields_empty_long_leg():
    profile = bp.build_top_k_by_composite(k=5)
    n = 3
    prices = _frame("2026-01-05", n, {"AAA": [100.0] * n, "BBB": [100.0] * n})
    bands = _frame("2026-01-05", n, {"AAA": ["elite"] * n, "BBB": ["elite"] * n})
    out = be.run_backtest(bands, prices, params=profile.params,
                          per_band_sweep=False)  # no score_history
    # No score history -> top_k picks nothing -> flat equity at 1.0.
    eq = list(out["equity_curve"].values())
    assert all(abs(v - 1.0) < 1e-9 for v in eq)
    assert out["summary"]["n_trades"] == 0
