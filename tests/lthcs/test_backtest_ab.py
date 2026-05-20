"""Tests for ``scripts/lthcs_backtest_ab.py`` (Phase 4 A/B view).

The module is laid out as a script under ``scripts/``, but its public
helpers (``run_ab``, ``build_delta_table``, ``overall_verdict``,
``rebanded_history_for_config``, ``_apply_diff``, ``_config_hash``) are
importable: ``scripts/lthcs_backtest_ab.py`` adds the repo root to
``sys.path`` at module load and ``conftest.py`` already adds
``scripts/`` to the path for the other lthcs scripts.
"""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd
import pytest

# Load the script as a module (it's under scripts/, not the lthcs package).
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "lthcs_backtest_ab.py"
spec = importlib.util.spec_from_file_location("lthcs_backtest_ab", SCRIPT_PATH)
ab = importlib.util.module_from_spec(spec)
sys.modules["lthcs_backtest_ab"] = ab
spec.loader.exec_module(ab)  # type: ignore[union-attr]

from lthcs import backtest_engine as be  # noqa: E402
from lthcs.score import PILLAR_ORDER  # noqa: E402


# Score bands matching the production weights.json shape — [0, 100] tiled.
SCORE_BANDS = {
    "elite":            {"min": 85, "max": 100},
    "high_confidence":  {"min": 80, "max": 84},
    "constructive":     {"min": 70, "max": 79},
    "monitor":          {"min": 60, "max": 69},
    "weakening":        {"min": 50, "max": 59},
    "review":           {"min": 0,  "max": 49},
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _equal_weights() -> List[float]:
    return [0.20, 0.20, 0.20, 0.20, 0.20]


def _make_row(
    ticker: str,
    subscores_by_pillar: Dict[str, float],
    weights: List[float] = None,
    maturity_stage: str = "mature_compounder",
    modifiers: Dict[str, float] = None,
) -> Dict[str, Any]:
    w = weights or _equal_weights()
    score = sum(
        float(subscores_by_pillar[p]) * float(w[i])
        for i, p in enumerate(PILLAR_ORDER)
    )
    return {
        "ticker": ticker,
        "subscores": dict(subscores_by_pillar),
        "weights_used": list(w),
        "effective_weights": list(w),
        "modifiers": modifiers or {"macro_adj": 0.0, "sector_adj": 0.0, "volatility_mod": 0.0},
        "lthcs_score": float(score),
        "maturity_stage": maturity_stage,
    }


def _trading_days(start: str, n: int) -> pd.DatetimeIndex:
    return pd.bdate_range(start=start, periods=n)


def _base_cfg(profile_weights: List[float]) -> Dict[str, Any]:
    return {
        "version": "test-1.0",
        "pillar_order": list(PILLAR_ORDER),
        "profiles": {"mature_compounder": list(profile_weights)},
        "score_bands": copy.deepcopy(SCORE_BANDS),
    }


# ---------------------------------------------------------------------------
# 1. Two synthetic configs produce two distinct equity curves
# ---------------------------------------------------------------------------

def test_two_configs_produce_distinct_equity_curves():
    """Force one ticker into the buy bands under A, but out under B.

    Construction:
      AAA = adoption 100, others 30.
      Profile A weights: adoption=0.60, others=0.10 each.
        composite = 100*0.60 + 30*0.10*4 = 72 -> constructive (BUY).
      Profile B weights: adoption=0.10, others=0.225 each.
        composite = 100*0.10 + 30*0.225*4 = 10 + 27 = 37 -> review (NOT buy).

    AAA rises 1%/day. A's equity curve compounds; B's stays at 1.0.
    """
    weights_a = [0.60, 0.10, 0.10, 0.10, 0.10]
    weights_b = [0.10, 0.225, 0.225, 0.225, 0.225]
    subs = {p: 30.0 for p in PILLAR_ORDER}
    subs["adoption_momentum"] = 100.0

    idx = _trading_days("2026-01-05", 10)
    prices = pd.DataFrame({"AAA": [100.0 * (1.01 ** i) for i in range(10)]}, index=idx)
    snapshots = {
        d.strftime("%Y-%m-%d"): [_make_row("AAA", subs, weights=_equal_weights())]
        for d in idx
    }

    cfg_a = _base_cfg(weights_a)
    cfg_b = _base_cfg(weights_b)

    out = ab.run_ab(
        config_a=cfg_a,
        config_b=cfg_b,
        snapshots_by_date=snapshots,
        prices=prices,
        params=be.EngineParams(cost_bps=0.0),
        label_a="A",
        label_b="B",
    )

    # A holds AAA the whole window -> positive total return.
    assert out["side_a"]["summary"]["total_return"] > 0.0
    # B never holds AAA -> equity flat at 1.0.
    assert out["side_b"]["summary"]["total_return"] == pytest.approx(0.0, abs=1e-9)

    # Equity curves should not coincide.
    eq_a = out["side_a"]["equity_curve"]
    eq_b = out["side_b"]["equity_curve"]
    assert len(eq_a) >= 2 and len(eq_b) >= 2
    last_a = eq_a[max(eq_a.keys())]
    last_b = eq_b[max(eq_b.keys())]
    assert abs(last_a - last_b) > 1e-6


# ---------------------------------------------------------------------------
# 2. Delta math: per-metric correctness
# ---------------------------------------------------------------------------

def test_delta_table_math_b_minus_a():
    summary_a = {
        "total_return": 0.10, "ann_return": 0.40, "sharpe": 1.0, "sortino": 1.2,
        "max_drawdown": -0.20, "hit_rate": 0.55, "turnover": 0.15,
        "avg_hold_days": 12.0, "n_trades": 30, "n_unique_tkr": 12,
    }
    summary_b = {
        "total_return": 0.15, "ann_return": 0.55, "sharpe": 1.5, "sortino": 1.8,
        "max_drawdown": -0.15, "hit_rate": 0.60, "turnover": 0.10,
        "avg_hold_days": 14.0, "n_trades": 33, "n_unique_tkr": 12,
    }
    rows = ab.build_delta_table(summary_a, summary_b)
    by_key = {r["key"]: r for r in rows}

    # delta = B - A
    assert by_key["total_return"]["delta"] == pytest.approx(0.05)
    assert by_key["sharpe"]["delta"] == pytest.approx(0.5)
    # pct = (B - A) / |A|
    assert by_key["sharpe"]["pct"] == pytest.approx(0.5)
    # max_drawdown: -0.15 - (-0.20) = +0.05 (less negative = better for higher-better default)
    assert by_key["max_drawdown"]["delta"] == pytest.approx(0.05)
    assert by_key["max_drawdown"]["winner"] == "b"
    # Turnover is lower_better; B (0.10) < A (0.15) -> B wins, delta = -0.05
    assert by_key["turnover"]["delta"] == pytest.approx(-0.05)
    assert by_key["turnover"]["winner"] == "b"


def test_overall_verdict_prefers_b_when_more_directional_metrics_win():
    summary_a = {"sharpe": 1.0, "total_return": 0.10, "ann_return": 0.20,
                 "max_drawdown": -0.20, "hit_rate": 0.50, "sortino": 1.0}
    summary_b = {"sharpe": 1.5, "total_return": 0.15, "ann_return": 0.30,
                 "max_drawdown": -0.15, "hit_rate": 0.55, "sortino": 1.4}
    rows = ab.build_delta_table(summary_a, summary_b)
    v = ab.overall_verdict(rows)
    assert v["winner"] == "b"
    # Weights: sharpe=2, plus total_return + ann_return + max_drawdown +
    # hit_rate + sortino at 1 each -> max possible 7.
    assert v["score_b"] == 7
    assert v["score_a"] == 0
    assert v["metrics_counted"] == 6


def test_verdict_tie_when_all_metrics_equal():
    summary = {"sharpe": 1.0, "total_return": 0.10, "ann_return": 0.20,
               "max_drawdown": -0.20, "hit_rate": 0.50, "sortino": 1.0,
               "turnover": 0.1, "avg_hold_days": 10, "n_trades": 20, "n_unique_tkr": 5}
    rows = ab.build_delta_table(summary, copy.deepcopy(summary))
    v = ab.overall_verdict(rows)
    assert v["winner"] == "tie"


# ---------------------------------------------------------------------------
# 3. Empty-config fallback
# ---------------------------------------------------------------------------

def test_rebanded_history_empty_when_snapshots_empty():
    cfg = _base_cfg(_equal_weights())
    out = ab.rebanded_history_for_config({}, cfg)
    assert out.empty


def test_rebanded_history_skips_rows_without_subscores():
    cfg = _base_cfg(_equal_weights())
    snapshots = {
        "2026-01-05": [
            # Well-formed row.
            _make_row("AAA", {p: 80.0 for p in PILLAR_ORDER}),
            # Bad row: no subscores.
            {"ticker": "BBB", "maturity_stage": "mature_compounder", "weights_used": _equal_weights()},
        ]
    }
    out = ab.rebanded_history_for_config(snapshots, cfg)
    assert "AAA" in out.columns
    assert "BBB" not in out.columns


def test_run_ab_handles_empty_snapshots_without_crashing():
    cfg = _base_cfg(_equal_weights())
    out = ab.run_ab(
        config_a=cfg,
        config_b=cfg,
        snapshots_by_date={},
        prices=pd.DataFrame(),
        params=be.EngineParams(),
    )
    assert out["side_a"]["run_meta"].get("empty") is True
    assert out["side_b"]["run_meta"].get("empty") is True
    # delta_table is still well-formed (all a/b None, winners 'unknown').
    assert len(out["delta_table"]) >= 1
    assert all(row["winner"] in ("unknown", "tie") for row in out["delta_table"])
    assert out["verdict"]["winner"] == "tie"


# ---------------------------------------------------------------------------
# 4. Hash stability — same config + same data = same hashes + same summaries
# ---------------------------------------------------------------------------

def test_config_hash_stable_across_invocations():
    cfg = _base_cfg([0.20, 0.20, 0.20, 0.20, 0.20])
    h1 = ab._config_hash(cfg)
    h2 = ab._config_hash(copy.deepcopy(cfg))
    assert h1 == h2


def test_config_hash_changes_when_profile_weight_changes():
    cfg_a = _base_cfg([0.20, 0.20, 0.20, 0.20, 0.20])
    cfg_b = _base_cfg([0.20, 0.20, 0.15, 0.25, 0.20])
    assert ab._config_hash(cfg_a) != ab._config_hash(cfg_b)


def test_config_hash_ignores_comment_changes():
    cfg_a = _base_cfg(_equal_weights())
    cfg_b = copy.deepcopy(cfg_a)
    cfg_b["description"] = "different prose"
    cfg_b["last_updated"] = "2099-01-01"
    assert ab._config_hash(cfg_a) == ab._config_hash(cfg_b)


def test_run_ab_deterministic_for_same_inputs():
    weights_a = [0.60, 0.10, 0.10, 0.10, 0.10]
    weights_b = [0.20, 0.20, 0.20, 0.20, 0.20]
    subs = {p: 30.0 for p in PILLAR_ORDER}
    subs["adoption_momentum"] = 100.0
    idx = _trading_days("2026-01-05", 8)
    prices = pd.DataFrame({"AAA": [100.0 + i for i in range(8)]}, index=idx)
    snapshots = {
        d.strftime("%Y-%m-%d"): [_make_row("AAA", subs, weights=_equal_weights())]
        for d in idx
    }

    cfg_a = _base_cfg(weights_a)
    cfg_b = _base_cfg(weights_b)

    out1 = ab.run_ab(
        config_a=cfg_a, config_b=cfg_b,
        snapshots_by_date=snapshots, prices=prices,
        params=be.EngineParams(cost_bps=0.0),
    )
    out2 = ab.run_ab(
        config_a=cfg_a, config_b=cfg_b,
        snapshots_by_date=snapshots, prices=prices,
        params=be.EngineParams(cost_bps=0.0),
    )
    # Hashes stable.
    assert out1["config_a_hash"] == out2["config_a_hash"]
    assert out1["config_b_hash"] == out2["config_b_hash"]
    # Total returns identical.
    assert out1["side_a"]["summary"]["total_return"] == out2["side_a"]["summary"]["total_return"]
    assert out1["side_b"]["summary"]["total_return"] == out2["side_b"]["summary"]["total_return"]
    # Equity curves identical.
    assert out1["side_a"]["equity_curve"] == out2["side_a"]["equity_curve"]
    assert out1["side_b"]["equity_curve"] == out2["side_b"]["equity_curve"]
    # Verdict stable.
    assert out1["verdict"] == out2["verdict"]


# ---------------------------------------------------------------------------
# 5. Diff overlay — apply_diff merges profile vector + score_bands
# ---------------------------------------------------------------------------

def test_apply_diff_overrides_profile_vector():
    base = _base_cfg([0.20, 0.20, 0.20, 0.20, 0.20])
    diff = {"profiles": {"mature_compounder": [0.20, 0.20, 0.15, 0.25, 0.20]}}
    out = ab._apply_diff(base, diff)
    assert out["profiles"]["mature_compounder"] == [0.20, 0.20, 0.15, 0.25, 0.20]
    # Base isn't mutated.
    assert base["profiles"]["mature_compounder"] == [0.20, 0.20, 0.20, 0.20, 0.20]


def test_apply_diff_adds_new_profile_without_clobbering_others():
    base = _base_cfg([0.20, 0.20, 0.20, 0.20, 0.20])
    base["profiles"]["other_stage"] = [0.30, 0.20, 0.20, 0.15, 0.15]
    diff = {"profiles": {"mature_compounder": [0.10, 0.20, 0.20, 0.30, 0.20]}}
    out = ab._apply_diff(base, diff)
    assert out["profiles"]["mature_compounder"] == [0.10, 0.20, 0.20, 0.30, 0.20]
    # Untouched profile preserved.
    assert out["profiles"]["other_stage"] == [0.30, 0.20, 0.20, 0.15, 0.15]


def test_apply_diff_merges_score_bands_per_band():
    base = _base_cfg(_equal_weights())
    diff = {"score_bands": {"constructive": {"min": 72, "max": 79}}}
    out = ab._apply_diff(base, diff)
    # The overridden band has new bounds.
    assert out["score_bands"]["constructive"]["min"] == 72
    assert out["score_bands"]["constructive"]["max"] == 79
    # The unmentioned band is untouched.
    assert out["score_bands"]["elite"]["min"] == 85


# ---------------------------------------------------------------------------
# 6. Profile-weight resolution — rebanded history uses cfg.profiles[maturity]
# ---------------------------------------------------------------------------

def test_rebanded_history_uses_cfg_profile_weights_not_row_weights_used():
    """The row has weights_used = equal, but the cfg says 'mature_compounder'
    should be [0.6, 0.1, 0.1, 0.1, 0.1]. The rebander must use the cfg vector.
    Verify by constructing a case where the two vectors band the row differently.

      Subscores: adoption 100, others 30.
      Row's weights_used (equal): composite = 100*.2 + 30*.2*4 = 44 -> review.
      Cfg's mature_compounder ([.6, .1, .1, .1, .1]): composite = 60 + 12 = 72 -> constructive.
    """
    subs = {p: 30.0 for p in PILLAR_ORDER}
    subs["adoption_momentum"] = 100.0
    snapshots = {
        "2026-01-05": [_make_row("AAA", subs, weights=_equal_weights(),
                                  maturity_stage="mature_compounder")]
    }
    cfg = _base_cfg([0.60, 0.10, 0.10, 0.10, 0.10])
    bh = ab.rebanded_history_for_config(snapshots, cfg)
    assert bh.iloc[0, 0] == "constructive"


def test_rebanded_history_falls_back_to_standard_compounder_for_unknown_stage():
    subs = {p: 80.0 for p in PILLAR_ORDER}  # composite 80 under equal weights
    snapshots = {
        "2026-01-05": [_make_row("AAA", subs, maturity_stage="totally_unknown_stage")]
    }
    cfg = {
        "pillar_order": list(PILLAR_ORDER),
        "profiles": {"standard_compounder": _equal_weights()},
        "score_bands": copy.deepcopy(SCORE_BANDS),
    }
    bh = ab.rebanded_history_for_config(snapshots, cfg)
    # 80 -> high_confidence under the standard_compounder fallback.
    assert bh.iloc[0, 0] == "high_confidence"
