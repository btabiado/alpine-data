"""Tests for ``lthcs.adaptive_weights``.

These tests build synthetic per-(date, ticker) panels of pillar scores
and forward returns where the ground-truth signal is known, then verify
the ridge tuner recovers it. They never touch ``data/lthcs/``.
"""

from __future__ import annotations

import math
from typing import Dict, List

import numpy as np
import pandas as pd
import pytest

from lthcs import adaptive_weights, backtest
from lthcs.adaptive_weights import (
    DEFAULT_PRIOR,
    PILLAR_NAMES,
    _ridge_closed_form,
    _project_to_simplex,
    _recommendation,
    tune_weights,
    walk_forward_tune,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_panel(
    n_dates: int = 60,
    n_tickers: int = 12,
    signal_pillar: str = "adoption_momentum",
    signal_strength: float = 1.0,
    seed: int = 7,
) -> Dict[str, pd.DataFrame]:
    """Build a synthetic (score_history, pillar_histories, forward_returns)
    where one pillar carries true cross-sectional signal.

    Returns dict with keys ``score_history``, ``pillar_histories``,
    ``forward_returns``.
    """
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2025-01-02", periods=n_dates, freq="B")
    tickers = [f"T{i:02d}" for i in range(n_tickers)]

    # Each pillar: random N(50, 10) per (date, ticker).
    pillar_histories: Dict[str, pd.DataFrame] = {}
    for p in PILLAR_NAMES:
        arr = 50.0 + 10.0 * rng.standard_normal((n_dates, n_tickers))
        pillar_histories[p] = pd.DataFrame(arr, index=dates, columns=tickers)

    # Forward returns = signal_strength * (centered signal_pillar) + noise.
    sig = pillar_histories[signal_pillar].sub(
        pillar_histories[signal_pillar].mean(axis=1), axis=0
    )
    noise = rng.standard_normal((n_dates, n_tickers))
    fwd = signal_strength * sig.values * 0.001 + 0.002 * noise
    forward_returns = pd.DataFrame(fwd, index=dates, columns=tickers)

    # Composite score = simple equal-weight average (just a sanity column).
    score_history = sum(pillar_histories[p] for p in PILLAR_NAMES) / 5.0

    return {
        "score_history": score_history,
        "pillar_histories": pillar_histories,
        "forward_returns": forward_returns,
    }


# ---------------------------------------------------------------------------
# Core math primitives
# ---------------------------------------------------------------------------

def test_simplex_projection_clips_negatives_and_renormalizes():
    w = np.array([-0.1, 0.3, 0.4, 0.5, -0.05])
    out = _project_to_simplex(w)
    assert (out >= 0).all()
    assert math.isclose(out.sum(), 1.0, abs_tol=1e-9)
    # Zeros for the clipped slots.
    assert out[0] == 0.0
    assert out[4] == 0.0


def test_simplex_projection_degenerate_falls_back_to_equal_weight():
    w = np.array([-1.0, -1.0, -1.0, -1.0, -1.0])
    out = _project_to_simplex(w)
    assert math.isclose(out.sum(), 1.0, abs_tol=1e-9)
    assert np.allclose(out, [0.2] * 5)


def test_ridge_closed_form_pulls_toward_prior_when_alpha_large():
    # Build a tiny problem where OLS would put all weight on column 0.
    X = np.array([[1.0, 0.0], [2.0, 0.0], [3.0, 0.0]])
    y = np.array([1.0, 2.0, 3.0])
    prior = np.array([0.5, 0.5])

    w_small = _ridge_closed_form(X, y, prior=prior, alpha=0.01)
    w_huge = _ridge_closed_form(X, y, prior=prior, alpha=1e6)

    # With small alpha → close to OLS [1, 0]; with huge alpha → close to prior.
    assert w_small[0] > 0.5
    assert abs(w_huge[0] - 0.5) < 1e-2
    assert abs(w_huge[1] - 0.5) < 1e-2


def test_ridge_zero_alpha_is_ols():
    X = np.array([[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]])
    y = np.array([2.0, 3.0, 5.0])
    prior = np.array([0.0, 0.0])
    w = _ridge_closed_form(X, y, prior=prior, alpha=0.0)
    assert np.allclose(w, [2.0, 3.0], atol=1e-9)


# ---------------------------------------------------------------------------
# tune_weights — recovery on synthetic data
# ---------------------------------------------------------------------------

def test_tune_recovers_signal_pillar_above_prior():
    """Pillar 'adoption_momentum' drives forward returns → its weight
    should rise above the equal-weight prior."""
    panel = _make_panel(signal_pillar="adoption_momentum", signal_strength=4.0)
    out = tune_weights(
        score_history=panel["score_history"],
        pillar_histories=panel["pillar_histories"],
        forward_returns=panel["forward_returns"],
        ridge_alpha=0.5,
    )
    assert out["fit_method"] == "ridge_regression"
    weights = out["weights"]
    # Sum to 1, all non-negative.
    assert math.isclose(sum(weights.values()), 1.0, abs_tol=1e-9)
    for v in weights.values():
        assert v >= 0
    # adoption_momentum should be meaningfully above prior 0.20.
    assert weights["adoption_momentum"] > 0.20
    # And it should be the largest weight.
    top = max(weights, key=weights.get)
    assert top == "adoption_momentum"


def test_tune_recovers_different_signal_pillar():
    panel = _make_panel(signal_pillar="financial_evolution", signal_strength=3.0)
    out = tune_weights(
        score_history=panel["score_history"],
        pillar_histories=panel["pillar_histories"],
        forward_returns=panel["forward_returns"],
        ridge_alpha=0.5,
    )
    top = max(out["weights"], key=out["weights"].get)
    assert top == "financial_evolution"


def test_tune_with_high_ridge_alpha_stays_near_prior():
    panel = _make_panel(signal_pillar="adoption_momentum", signal_strength=4.0)
    out_low = tune_weights(
        score_history=panel["score_history"],
        pillar_histories=panel["pillar_histories"],
        forward_returns=panel["forward_returns"],
        ridge_alpha=0.01,
    )
    out_high = tune_weights(
        score_history=panel["score_history"],
        pillar_histories=panel["pillar_histories"],
        forward_returns=panel["forward_returns"],
        ridge_alpha=1e6,
    )
    # High-alpha solution should be much closer to equal-weight prior.
    diff_low = abs(out_low["weights"]["adoption_momentum"] - 0.20)
    diff_high = abs(out_high["weights"]["adoption_momentum"] - 0.20)
    assert diff_high < diff_low
    # And the high-alpha output should hug the equal-weight prior.
    for v in out_high["weights"].values():
        assert abs(v - 0.20) < 0.05


def test_tune_empty_history_returns_prior():
    out = tune_weights(
        score_history=pd.DataFrame(),
        pillar_histories={p: pd.DataFrame() for p in PILLAR_NAMES},
        forward_returns=pd.DataFrame(),
        ridge_alpha=0.5,
    )
    assert out["fit_method"] == "prior_fallback"
    assert out["n_obs"] == 0
    assert math.isclose(sum(out["weights"].values()), 1.0, abs_tol=1e-9)
    for p, v in out["weights"].items():
        assert abs(v - 0.20) < 1e-9


def test_tune_simplex_projection_no_negatives():
    panel = _make_panel(signal_pillar="thesis_integrity", signal_strength=5.0)
    out = tune_weights(
        score_history=panel["score_history"],
        pillar_histories=panel["pillar_histories"],
        forward_returns=panel["forward_returns"],
        ridge_alpha=0.001,  # very weak regularization → could push some weights negative
    )
    for v in out["weights"].values():
        assert v >= 0.0
    assert math.isclose(sum(out["weights"].values()), 1.0, abs_tol=1e-9)


def test_tune_universe_subset_filtering():
    panel = _make_panel(n_tickers=12, signal_pillar="des", signal_strength=3.0)
    subset = ["T00", "T01", "T02", "T03"]
    out = tune_weights(
        score_history=panel["score_history"],
        pillar_histories=panel["pillar_histories"],
        forward_returns=panel["forward_returns"],
        ridge_alpha=0.5,
        universe_subset=subset,
    )
    # 60 dates × 4 tickers ≈ 240 row max; centering drops a tiny bit but
    # should be well under the full 720.
    assert out["n_obs"] <= 60 * len(subset)
    assert out["n_obs"] >= 60 * len(subset) // 2
    # universe_subset echoed in output for auditability.
    assert out["universe_subset"] == subset


def test_tune_ic_matches_backtest_engine_ground_truth():
    """The reported in_sample_ic must equal what backtest.attribute_returns
    reports for the equivalent weighted composite — single source of truth."""
    panel = _make_panel(signal_pillar="adoption_momentum", signal_strength=3.0)
    out = tune_weights(
        score_history=panel["score_history"],
        pillar_histories=panel["pillar_histories"],
        forward_returns=panel["forward_returns"],
        ridge_alpha=0.5,
    )
    # Build composite from the same weights and feed to attribute_returns.
    w = out["weights"]
    composite = sum(
        panel["pillar_histories"][p] * w[p] for p in PILLAR_NAMES
    )
    attribution = backtest.attribute_returns(
        score_history=composite,
        pillar_histories={},
        forward_returns=panel["forward_returns"],
    )
    composite_row = attribution[attribution["pillar"] == "composite"].iloc[0]
    assert abs(out["in_sample_ic"] - composite_row["ic_mean"]) < 1e-6


# ---------------------------------------------------------------------------
# walk_forward_tune
# ---------------------------------------------------------------------------

def test_walk_forward_train_test_split_dates():
    panel = _make_panel(n_dates=50)
    result = walk_forward_tune(
        score_history=panel["score_history"],
        pillar_histories=panel["pillar_histories"],
        forward_returns=panel["forward_returns"],
        train_fraction=0.6,
        ridge_alpha=0.5,
    )
    # First 30 dates train, last 20 test.
    assert result["train_dates"][0] == panel["score_history"].index[0].strftime("%Y-%m-%d")
    assert result["test_dates"][1] == panel["score_history"].index[-1].strftime("%Y-%m-%d")
    # Train end strictly before test start.
    assert result["train_dates"][1] < result["test_dates"][0]


def test_walk_forward_overfit_gap_positive_when_signal_exists():
    """With real cross-sectional signal in-sample, train IC typically
    exceeds test IC (overfit gap >= 0). It can be very small or even
    slightly negative on small samples, but not wildly negative."""
    panel = _make_panel(signal_pillar="adoption_momentum", signal_strength=3.0,
                        n_dates=80, n_tickers=15)
    result = walk_forward_tune(
        score_history=panel["score_history"],
        pillar_histories=panel["pillar_histories"],
        forward_returns=panel["forward_returns"],
        train_fraction=0.6,
        ridge_alpha=0.5,
    )
    # Both ICs should be positive (signal is real).
    assert result["train_ic"] > 0
    assert result["test_ic"] > 0
    # train_ic + a small tolerance >= test_ic typically.
    assert result["train_ic"] >= result["test_ic"] - 0.02


def test_walk_forward_empty_history_rejects():
    result = walk_forward_tune(
        score_history=pd.DataFrame(),
        pillar_histories={p: pd.DataFrame() for p in PILLAR_NAMES},
        forward_returns=pd.DataFrame(),
        train_fraction=0.6,
        ridge_alpha=0.5,
    )
    assert result["recommendation"] == "reject"
    assert result["n_train_obs"] == 0
    assert result["n_test_obs"] == 0


def test_walk_forward_invalid_train_fraction():
    panel = _make_panel(n_dates=20)
    with pytest.raises(ValueError):
        walk_forward_tune(
            score_history=panel["score_history"],
            pillar_histories=panel["pillar_histories"],
            forward_returns=panel["forward_returns"],
            train_fraction=1.5,
        )


# ---------------------------------------------------------------------------
# Recommendation thresholds
# ---------------------------------------------------------------------------

def test_recommendation_ship():
    rec, reason = _recommendation(test_ic=0.08, overfit_gap=0.02)
    assert rec == "ship"
    assert "test_ic" in reason


def test_recommendation_hold_overfit():
    rec, reason = _recommendation(test_ic=0.06, overfit_gap=0.08)
    assert rec == "hold"
    assert "overfit" in reason.lower()


def test_recommendation_reject_weak_signal():
    rec, reason = _recommendation(test_ic=0.02, overfit_gap=0.01)
    assert rec == "reject"
    assert "weak" in reason.lower() or "0.040" in reason or "0.04" in reason


def test_recommendation_boundary_at_min_test_ic():
    # test_ic exactly equal to threshold → reject (uses strict '>').
    rec, _ = _recommendation(test_ic=adaptive_weights.SHIP_MIN_TEST_IC, overfit_gap=0.01)
    assert rec == "reject"


def test_recommendation_boundary_at_overfit_gap():
    # overfit_gap exactly equal to threshold → hold (uses strict '<').
    rec, _ = _recommendation(test_ic=0.08, overfit_gap=adaptive_weights.SHIP_MAX_OVERFIT_GAP)
    assert rec == "hold"


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------

def test_tune_output_schema():
    panel = _make_panel()
    out = tune_weights(
        score_history=panel["score_history"],
        pillar_histories=panel["pillar_histories"],
        forward_returns=panel["forward_returns"],
    )
    for key in (
        "weights", "prior_weights", "ridge_alpha", "horizon_days",
        "n_obs", "in_sample_ic", "fit_method", "trained_at",
    ):
        assert key in out, "missing key %r" % key
    # weights dict has exactly the 5 pillars.
    assert set(out["weights"].keys()) == set(PILLAR_NAMES)


def test_walk_forward_output_schema():
    panel = _make_panel()
    result = walk_forward_tune(
        score_history=panel["score_history"],
        pillar_histories=panel["pillar_histories"],
        forward_returns=panel["forward_returns"],
    )
    for key in (
        "train_weights", "train_ic", "test_ic", "overfit_gap",
        "train_dates", "test_dates", "n_train_obs", "n_test_obs",
        "recommendation", "recommendation_reason",
        "ridge_alpha", "horizon_days", "train_fraction",
    ):
        assert key in result, "missing key %r" % key
    assert result["recommendation"] in {"ship", "hold", "reject"}
    assert set(result["train_weights"].keys()) == set(PILLAR_NAMES)


# ---------------------------------------------------------------------------
# Custom prior
# ---------------------------------------------------------------------------

def test_tune_custom_prior():
    panel = _make_panel(signal_pillar="institutional_confidence", signal_strength=0.5)
    # Custom prior heavily weights institutional_confidence.
    custom_prior = [0.05, 0.80, 0.05, 0.05, 0.05]
    out = tune_weights(
        score_history=panel["score_history"],
        pillar_histories=panel["pillar_histories"],
        forward_returns=panel["forward_returns"],
        ridge_alpha=10.0,  # high enough to dominate weak data signal
        prior_weights=custom_prior,
    )
    # institutional_confidence should be by far the largest weight.
    assert out["weights"]["institutional_confidence"] > 0.5
    # Prior echoed in output.
    assert abs(sum(out["prior_weights"]) - 1.0) < 1e-9


def test_tune_invalid_prior_length():
    panel = _make_panel()
    with pytest.raises(ValueError):
        tune_weights(
            score_history=panel["score_history"],
            pillar_histories=panel["pillar_histories"],
            forward_returns=panel["forward_returns"],
            prior_weights=[0.5, 0.5],  # wrong length
        )


# ---------------------------------------------------------------------------
# Integration: adaptive_overrides flag in score.py
# ---------------------------------------------------------------------------

def test_score_respects_adaptive_overrides_when_enabled():
    """Wiring check: when adaptive_overrides.enabled=true in the
    weights_config dict, compute_lthcs_score uses the override weights."""
    from lthcs.score import compute_lthcs_score

    weights_config = {
        "profiles": {
            "mature_compounder": [0.20, 0.20, 0.20, 0.20, 0.20],
        },
        "score_bands": {
            "review": {"min": 0, "max": 49},
            "weakening": {"min": 50, "max": 59},
            "monitor": {"min": 60, "max": 69},
            "constructive": {"min": 70, "max": 79},
            "high_confidence": {"min": 80, "max": 89},
            "elite": {"min": 90, "max": 100},
        },
        "adaptive_overrides": {
            "enabled": True,
            "weights": {
                "adoption_momentum": 1.0,
                "institutional_confidence": 0.0,
                "financial_evolution": 0.0,
                "thesis_integrity": 0.0,
                "des": 0.0,
            },
        },
    }
    # Adoption = 100, others = 0. With overrides ON, weighted_sum should be 100.
    subs = {
        "adoption_momentum": 100.0,
        "institutional_confidence": 0.0,
        "financial_evolution": 0.0,
        "thesis_integrity": 0.0,
        "des": 0.0,
    }
    out = compute_lthcs_score(
        ticker="TEST", sector="Tech", maturity_stage="mature_compounder",
        pillar_subscores=subs, weights_config=weights_config,
    )
    # Should be 100 (clamped) → 'elite' band.
    assert out["lthcs_score"] == 100.0
    # effective_weights reflects the override.
    assert out["effective_weights"][0] == pytest.approx(1.0)


def test_score_ignores_adaptive_overrides_when_disabled():
    """Default behavior: adaptive_overrides.enabled=False → curated
    profile weights used unchanged."""
    from lthcs.score import compute_lthcs_score

    weights_config = {
        "profiles": {
            "mature_compounder": [0.20, 0.20, 0.20, 0.20, 0.20],
        },
        "score_bands": {
            "review": {"min": 0, "max": 49},
            "weakening": {"min": 50, "max": 59},
            "monitor": {"min": 60, "max": 69},
            "constructive": {"min": 70, "max": 79},
            "high_confidence": {"min": 80, "max": 89},
            "elite": {"min": 90, "max": 100},
        },
        "adaptive_overrides": {
            "enabled": False,
            "weights": {p: 0.0 for p in PILLAR_NAMES} | {"adoption_momentum": 1.0},
        },
    }
    subs = {p: 50.0 for p in PILLAR_NAMES}
    out = compute_lthcs_score(
        ticker="TEST", sector="Tech", maturity_stage="mature_compounder",
        pillar_subscores=subs, weights_config=weights_config,
    )
    # 50 * (0.2*5) = 50; with overrides disabled it should be exactly 50.
    assert out["lthcs_score"] == 50.0


def test_score_malformed_adaptive_overrides_falls_back():
    """If adaptive_overrides.weights is missing a pillar, fall back
    to curated weights gracefully (no crash)."""
    from lthcs.score import compute_lthcs_score

    weights_config = {
        "profiles": {
            "mature_compounder": [0.20, 0.20, 0.20, 0.20, 0.20],
        },
        "score_bands": {
            "review": {"min": 0, "max": 49},
            "weakening": {"min": 50, "max": 59},
            "monitor": {"min": 60, "max": 69},
            "constructive": {"min": 70, "max": 79},
            "high_confidence": {"min": 80, "max": 89},
            "elite": {"min": 90, "max": 100},
        },
        "adaptive_overrides": {
            "enabled": True,
            "weights": {"adoption_momentum": 1.0},  # missing other 4 pillars
        },
    }
    subs = {p: 50.0 for p in PILLAR_NAMES}
    out = compute_lthcs_score(
        ticker="TEST", sector="Tech", maturity_stage="mature_compounder",
        pillar_subscores=subs, weights_config=weights_config,
    )
    # Falls back to curated → 50.
    assert out["lthcs_score"] == 50.0
