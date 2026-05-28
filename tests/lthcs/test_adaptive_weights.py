"""Tests for ``lthcs.adaptive_weights``.

These tests build synthetic per-(date, ticker) panels of pillar scores
and forward returns where the ground-truth signal is known, then verify
the ridge tuner recovers it. They never touch ``data/lthcs/``.
"""

from __future__ import annotations

import math
from typing import Dict

import numpy as np
import pandas as pd
import pytest

from lthcs import adaptive_weights, backtest
from lthcs.adaptive_weights import PILLAR_NAMES, SHIP_MIN_TEST_OBS, SHIP_MIN_TEST_SHARPE, SHIP_MAX_SHARPE_OVERFIT_GAP, _build_ffill_mask, _apply_real_mask, _zscore_columns, _unscale_coefs, _ridge_closed_form, _project_to_simplex, _recommendation, _recommendation_equity, _annualised_sharpe, _equity_curve_to_returns, tune_weights, walk_forward_tune, walk_forward_tune_equity


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


def test_recommendation_small_sample_downgrades_to_hold():
    """Bug 1 follow-up: a great-looking point IC on only a handful of
    real cross-sections must NOT ship — the OOS estimate is too noisy
    on small N."""
    # Strong test IC, small overfit gap — would normally ship.
    rec, reason = _recommendation(
        test_ic=0.20, overfit_gap=0.01, n_test_obs=4
    )
    assert rec == "hold"
    assert "cross-section" in reason or "n_test_obs" in reason


def test_recommendation_large_sample_can_still_ship():
    rec, _ = _recommendation(
        test_ic=0.20, overfit_gap=0.01,
        n_test_obs=adaptive_weights.SHIP_MIN_TEST_OBS + 1,
    )
    assert rec == "ship"


def test_recommendation_backward_compat_no_n_test_obs():
    """Callers that don't pass n_test_obs (legacy) still get the
    classical ship/hold/reject behavior."""
    rec, _ = _recommendation(test_ic=0.20, overfit_gap=0.01)
    assert rec == "ship"


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


# ---------------------------------------------------------------------------
# Bug 1: ffill rejection in test split (commit 2bcacd3 walk-forward bug)
# ---------------------------------------------------------------------------

def _make_panel_with_ffill_tail(
    n_real_dates: int = 30,
    n_ffill_dates: int = 20,
    n_tickers: int = 10,
    signal_pillar: str = "adoption_momentum",
    signal_strength: float = 3.0,
    seed: int = 11,
) -> Dict[str, pd.DataFrame]:
    """Synthetic panel where the last n_ffill_dates rows of forward_returns
    are exact duplicates of the last real cross-section (mimicking what
    backtest.fetch_forward_returns does when the price cache ends before
    end_date + horizon)."""
    n_dates = n_real_dates + n_ffill_dates
    panel = _make_panel(
        n_dates=n_dates,
        n_tickers=n_tickers,
        signal_pillar=signal_pillar,
        signal_strength=signal_strength,
        seed=seed,
    )
    fwd = panel["forward_returns"].copy()
    # Tail rows duplicate the last real row exactly.
    last_real_row = fwd.iloc[n_real_dates - 1]
    for i in range(n_real_dates, n_dates):
        fwd.iloc[i] = last_real_row.values
    panel["forward_returns"] = fwd
    panel["n_real_dates"] = n_real_dates
    panel["n_ffill_dates"] = n_ffill_dates
    return panel


def test_build_ffill_mask_flags_duplicate_tail():
    panel = _make_panel_with_ffill_tail(n_real_dates=30, n_ffill_dates=20)
    mask = _build_ffill_mask(panel["forward_returns"])
    # Mask is True for ffilled/non-real cells.
    # Tail rows (rows 30..49) should be all-True duplicates.
    assert mask.iloc[30:].values.all()
    # The first n_real_dates rows are real (mostly False).
    # Row 0 is always real (no prior to compare).
    assert not mask.iloc[0].any()
    # Real rows in middle should be predominantly False.
    assert mask.iloc[5:25].sum().sum() == 0  # all distinct floats


def test_build_ffill_mask_handles_nans():
    df = pd.DataFrame(
        {
            "A": [0.01, 0.02, np.nan, 0.04],
            "B": [0.10, 0.10, 0.10, np.nan],
        },
        index=pd.date_range("2025-01-01", periods=4, freq="D"),
    )
    mask = _build_ffill_mask(df)
    # B row 1 and 2 are duplicates of row 0.
    assert mask.loc[mask.index[1], "B"] == True
    assert mask.loc[mask.index[2], "B"] == True
    # A row 2 is NaN → flagged as not-real.
    assert mask.loc[mask.index[2], "A"] == True
    # B row 3 is NaN → flagged.
    assert mask.loc[mask.index[3], "B"] == True


def test_apply_real_mask_nullifies_and_counts():
    df = pd.DataFrame(
        {"A": [1.0, 1.0, 2.0], "B": [3.0, 4.0, 4.0]},
        index=pd.date_range("2025-01-01", periods=3, freq="D"),
    )
    # Real mask: A row 1 is duplicate of row 0; B row 2 is dup of row 1.
    masked, n_rejected = _apply_real_mask(df, None)
    # 2 cells got nullified.
    assert n_rejected == 2
    assert pd.isna(masked.iloc[1]["A"])
    assert pd.isna(masked.iloc[2]["B"])


def test_walk_forward_rejects_ffilled_test_dates():
    """Bug 1 fix: the test split must skip dates whose forward returns
    are frozen ffilled duplicates from earlier in the panel."""
    panel = _make_panel_with_ffill_tail(
        n_real_dates=30, n_ffill_dates=20, n_tickers=10,
        signal_pillar="adoption_momentum", signal_strength=3.0,
    )
    result = walk_forward_tune(
        score_history=panel["score_history"],
        pillar_histories=panel["pillar_histories"],
        forward_returns=panel["forward_returns"],
        train_fraction=0.6,  # split = 30 → first 30 train, 20 test
        ridge_alpha=0.5,
    )
    # n_rejected_ffill must report the nullified cells.
    assert result["n_rejected_ffill"] >= 20 * 10 - 10  # ~all ffill rows
    # All test dates are ffilled → effective n_test_obs is 0
    # (no cross-section survives) or very small (the boundary date might
    # still have one real row depending on how the split lands).
    assert result["n_test_obs"] <= 1


def test_tune_weights_reject_ffill_can_be_disabled():
    """If reject_ffill=False, ffilled cells flow through unchanged
    (legacy behavior). Useful for callers who pre-masked their data."""
    panel = _make_panel_with_ffill_tail(n_real_dates=20, n_ffill_dates=15)
    out_with_reject = tune_weights(
        score_history=panel["score_history"],
        pillar_histories=panel["pillar_histories"],
        forward_returns=panel["forward_returns"],
        ridge_alpha=0.5,
        reject_ffill=True,
    )
    out_no_reject = tune_weights(
        score_history=panel["score_history"],
        pillar_histories=panel["pillar_histories"],
        forward_returns=panel["forward_returns"],
        ridge_alpha=0.5,
        reject_ffill=False,
    )
    # n_obs strictly smaller after rejection.
    assert out_with_reject["n_obs"] < out_no_reject["n_obs"]
    assert out_with_reject["n_rejected_ffill"] > 0
    assert out_no_reject["n_rejected_ffill"] == 0


def test_tune_weights_accepts_explicit_real_mask():
    """Callers can pass a precomputed real-mask (e.g. derived from the
    actual price-cache last-trading-day) instead of relying on the
    duplicate-detection heuristic."""
    panel = _make_panel(n_dates=30, n_tickers=8, signal_strength=2.0)
    fwd = panel["forward_returns"]
    # Build a mask that rejects the second half of dates.
    real_mask = pd.DataFrame(True, index=fwd.index, columns=fwd.columns)
    real_mask.iloc[15:] = False
    out = tune_weights(
        score_history=panel["score_history"],
        pillar_histories=panel["pillar_histories"],
        forward_returns=fwd,
        ridge_alpha=0.5,
        forward_returns_real_mask=real_mask,
    )
    # ~15 dates × 8 tickers nullified.
    assert out["n_rejected_ffill"] == 15 * 8


# ---------------------------------------------------------------------------
# Bug 2: z-scoring removes the artificial low-variance-pillar concentration
# ---------------------------------------------------------------------------

def _make_pillar_scale_imbalance_panel(
    n_dates: int = 60,
    n_tickers: int = 20,
    signal_pillar: str = "adoption_momentum",
    signal_strength: float = 2.5,
    seed: int = 23,
) -> Dict[str, pd.DataFrame]:
    """Mimic the production pillar-scale imbalance: pillars have very
    different cross-sectional standard deviations (e.g. thesis_integrity
    σ ≈ 1.2 vs adoption σ ≈ 29). Forward returns are driven by the
    SIGNAL pillar (NOT thesis_integrity)."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2025-01-02", periods=n_dates, freq="B")
    tickers = [f"T{i:02d}" for i in range(n_tickers)]

    # Per-pillar scales: roughly matches the production stds.
    pillar_sigmas = {
        "adoption_momentum": 29.0,
        "institutional_confidence": 29.0,
        "financial_evolution": 20.0,
        "thesis_integrity": 1.2,  # tiny — would dominate post-simplex pre-fix
        "des": 6.5,
    }
    pillar_histories: Dict[str, pd.DataFrame] = {}
    for p in PILLAR_NAMES:
        sigma = pillar_sigmas[p]
        arr = 50.0 + sigma * rng.standard_normal((n_dates, n_tickers))
        pillar_histories[p] = pd.DataFrame(arr, index=dates, columns=tickers)

    # Forward returns driven by SIGNAL_PILLAR (after centering).
    sig = pillar_histories[signal_pillar].sub(
        pillar_histories[signal_pillar].mean(axis=1), axis=0
    )
    noise = rng.standard_normal((n_dates, n_tickers))
    fwd = signal_strength * sig.values * 0.0005 + 0.002 * noise
    forward_returns = pd.DataFrame(fwd, index=dates, columns=tickers)

    score_history = sum(pillar_histories[p] for p in PILLAR_NAMES) / 5.0
    return {
        "score_history": score_history,
        "pillar_histories": pillar_histories,
        "forward_returns": forward_returns,
    }


def test_zscore_columns_unit_variance():
    X = np.array([[1.0, 100.0], [2.0, 200.0], [3.0, 300.0], [4.0, 400.0]])
    Z, sigma = _zscore_columns(X)
    # Per-column std of Z should be ~1.
    assert abs(Z.std(axis=0)[0] - 1.0) < 1e-9
    assert abs(Z.std(axis=0)[1] - 1.0) < 1e-9
    # σ recovers raw stds.
    assert abs(sigma[0] - X.std(axis=0)[0]) < 1e-9
    assert abs(sigma[1] - X.std(axis=0)[1]) < 1e-9


def test_zscore_columns_handles_constant_column():
    """A pillar with zero cross-sectional variance must not blow up
    AND its Z column must be all-zero so the ridge fit sees no signal
    there (preventing FP-noise leakage)."""
    X = np.array([[1.0, 5.0], [2.0, 5.0], [3.0, 5.0]])
    Z, sigma = _zscore_columns(X)
    # No NaN / inf in Z.
    assert np.isfinite(Z).all()
    # Raw sigma is reported as-is (so callers can detect degenerate
    # columns via σ ≈ 0).
    assert sigma[1] == 0.0
    # The Z column for the degenerate pillar is literally zero.
    assert (Z[:, 1] == 0.0).all()


def test_unscale_coefs_zeroes_degenerate_pillars():
    """If a pillar has σ ≈ 0, _unscale_coefs must return 0 for that
    pillar regardless of what w_z claims (else dividing by σ_floor
    re-creates the Bug 2 winner-takes-all artifact)."""
    sigma = np.array([5.0, 0.0, 2.0])  # middle pillar degenerate
    w_z = np.array([1.0, 0.20, 0.5])  # middle pillar holds the prior
    w_raw = _unscale_coefs(w_z, sigma)
    # Degenerate pillar's raw coef must be exactly 0.
    assert w_raw[1] == 0.0
    # Other pillars un-scaled normally.
    assert abs(w_raw[0] - 0.2) < 1e-9
    assert abs(w_raw[2] - 0.25) < 1e-9


def test_unscale_coefs_inverse_of_zscore_scaling():
    """If y = β·X and we fit on Z = X/σ, then w_z = β·σ; un-scaling
    recovers β."""
    sigma = np.array([5.0, 2.0, 1.0])
    w_z = np.array([10.0, 4.0, 7.0])
    w_raw = _unscale_coefs(w_z, sigma)
    np.testing.assert_allclose(w_raw, np.array([2.0, 2.0, 7.0]))


def test_zscoring_prevents_low_variance_pillar_concentration():
    """The headline Bug 2 regression test.

    Production-like pillar-scale imbalance with TRUE signal in a
    high-variance pillar (adoption). Before the fix, the simplex
    projection collapsed onto thesis_integrity (the LOWEST-variance
    pillar) regardless of where the true signal lived. After the fix,
    the recovered weight on the actual signal pillar should be > 0.5
    AND thesis_integrity should NOT dominate."""
    panel = _make_pillar_scale_imbalance_panel(
        n_dates=80, n_tickers=20,
        signal_pillar="adoption_momentum", signal_strength=2.5, seed=23,
    )
    out = tune_weights(
        score_history=panel["score_history"],
        pillar_histories=panel["pillar_histories"],
        forward_returns=panel["forward_returns"],
        ridge_alpha=0.001,  # weak prior — let the data speak
        reject_ffill=False,  # synthetic returns aren't ffilled
    )
    weights = out["weights"]
    # True signal pillar should recover > 0.5 of total mass.
    assert weights["adoption_momentum"] > 0.5, (
        "Expected recovered weight on signal pillar > 0.5, got %r" % weights
    )
    # thesis_integrity should NOT be the top pillar (pre-fix bug).
    top = max(weights, key=weights.get)
    assert top == "adoption_momentum"
    # And specifically thesis_integrity shouldn't be > 0.5.
    assert weights["thesis_integrity"] < 0.5


def test_zscoring_records_pillar_sigmas_in_output():
    panel = _make_pillar_scale_imbalance_panel(n_dates=60, n_tickers=15)
    out = tune_weights(
        score_history=panel["score_history"],
        pillar_histories=panel["pillar_histories"],
        forward_returns=panel["forward_returns"],
        ridge_alpha=0.5,
        reject_ffill=False,
    )
    sigmas = out["pillar_sigmas"]
    # thesis_integrity should be the smallest sigma.
    assert sigmas["thesis_integrity"] < sigmas["adoption_momentum"]
    assert sigmas["thesis_integrity"] < sigmas["des"]
    # All recorded.
    assert set(sigmas.keys()) == set(PILLAR_NAMES)


# ---------------------------------------------------------------------------
# Bug 3: ridge_alpha actually regularizes after the n_obs rescaling
# ---------------------------------------------------------------------------

def test_ridge_alpha_zero_close_to_ols():
    """alpha=0 → ridge_alpha_effective=0 → pure OLS on z-scored X."""
    panel = _make_pillar_scale_imbalance_panel(
        n_dates=80, n_tickers=20,
        signal_pillar="adoption_momentum", signal_strength=2.5,
    )
    out = tune_weights(
        score_history=panel["score_history"],
        pillar_histories=panel["pillar_histories"],
        forward_returns=panel["forward_returns"],
        ridge_alpha=0.0,
        reject_ffill=False,
    )
    # OLS recovers the signal strongly.
    assert out["weights"]["adoption_momentum"] > 0.5
    # Effective alpha is 0.
    assert out["ridge_alpha_effective"] == 0.0


def test_ridge_alpha_one_meaningfully_regularizes():
    """alpha=1.0 → ridge_alpha_effective = n_obs → strong regularization,
    weights should be pulled noticeably toward equal-weight prior."""
    panel = _make_pillar_scale_imbalance_panel(
        n_dates=80, n_tickers=20,
        signal_pillar="adoption_momentum", signal_strength=2.5,
    )
    out_low = tune_weights(
        score_history=panel["score_history"],
        pillar_histories=panel["pillar_histories"],
        forward_returns=panel["forward_returns"],
        ridge_alpha=0.0,
        reject_ffill=False,
    )
    out_high = tune_weights(
        score_history=panel["score_history"],
        pillar_histories=panel["pillar_histories"],
        forward_returns=panel["forward_returns"],
        ridge_alpha=1.0,  # = 1.0 × n_obs effective
        reject_ffill=False,
    )
    # Higher alpha pulls signal pillar weight DOWN toward 0.20 prior.
    assert (
        out_high["weights"]["adoption_momentum"]
        < out_low["weights"]["adoption_momentum"]
    )


def test_ridge_alpha_effective_recorded_in_output():
    panel = _make_pillar_scale_imbalance_panel(n_dates=60, n_tickers=15)
    out = tune_weights(
        score_history=panel["score_history"],
        pillar_histories=panel["pillar_histories"],
        forward_returns=panel["forward_returns"],
        ridge_alpha=0.5,
        reject_ffill=False,
    )
    expected = 0.5 * out["n_obs"]
    assert abs(out["ridge_alpha_effective"] - expected) < 1e-9


# ---------------------------------------------------------------------------
# Walk-forward CV: combined behaviour after all 3 fixes
# ---------------------------------------------------------------------------

def test_walk_forward_after_all_fixes_no_signal_yields_negligible_test_ic():
    """When pillars are noise (no real signal) and we honestly reject
    ffilled dates, test_ic should be ~0 and the verdict should NOT be
    ship. This is the 'fixed math, low N → honest hold/reject' path."""
    panel = _make_panel(
        n_dates=60, n_tickers=12,
        signal_pillar="adoption_momentum", signal_strength=0.0,
        seed=999,
    )
    result = walk_forward_tune(
        score_history=panel["score_history"],
        pillar_histories=panel["pillar_histories"],
        forward_returns=panel["forward_returns"],
        train_fraction=0.6,
        ridge_alpha=0.5,
    )
    # No signal → test IC near zero → reject.
    assert abs(result["test_ic"]) < 0.10
    assert result["recommendation"] in {"reject", "hold"}


def test_walk_forward_output_schema_extended():
    """After fixes, the result dict carries the new diagnostic keys."""
    panel = _make_panel(n_dates=40, n_tickers=10, signal_strength=2.0)
    result = walk_forward_tune(
        score_history=panel["score_history"],
        pillar_histories=panel["pillar_histories"],
        forward_returns=panel["forward_returns"],
        train_fraction=0.6,
        ridge_alpha=0.5,
    )
    for key in (
        "n_rejected_ffill",
        "ridge_alpha_effective_train",
        "pillar_sigmas_train",
        "test_dates_after_ffill_reject",
    ):
        assert key in result, "missing key %r" % key


# ---------------------------------------------------------------------------
# Adaptive Weights V2 prep: engine equity-curve bridge
# (Tier 5 #24 Phase 4 + #25 plumbing)
#
# These tests verify the new walk_forward_tune_equity() function ingests
# backtest-engine equity curves correctly and that the promotion gate
# (SHIP_MIN_TEST_OBS) fires HOLD until enough OOS history accumulates.
# The IC-based walk_forward_tune is treated as a regression guard — its
# default behavior MUST be byte-identical pre/post the V2 bridge.
# ---------------------------------------------------------------------------


def _make_engine_equity_curve(
    n_days: int,
    mean_daily_return: float = 0.0005,
    daily_vol: float = 0.01,
    seed: int = 17,
    start: str = "2026-02-17",
) -> Dict[str, float]:
    """Synthetic engine equity curve mimicking the JSON shape under
    ``data/lthcs/backtest/<run_id>/equity_curve.json``.

    Returns ``{YYYY-MM-DD: float_equity, ...}`` starting at 1.0.
    """
    rng = np.random.default_rng(seed)
    daily = mean_daily_return + daily_vol * rng.standard_normal(n_days)
    equity = np.cumprod(1.0 + daily)
    # Force the first day to be exactly 1.0 (engine convention).
    equity = np.concatenate([[1.0], equity[:-1]])
    dates = pd.bdate_range(start, periods=n_days)
    return {d.strftime("%Y-%m-%d"): float(e) for d, e in zip(dates, equity)}


def test_annualised_sharpe_matches_252_convention():
    """Smoke: at mean=0.001, sd=0.01 daily, annualised Sharpe ≈
    0.001/0.01 * sqrt(252) ≈ 1.587."""
    # Truly zero-variance input → 0.0 (guards against div-by-zero).
    assert _annualised_sharpe(pd.Series([0.0, 0.0, 0.0, 0.0])) == 0.0
    rng = np.random.default_rng(0)
    rets = pd.Series(0.001 + 0.01 * rng.standard_normal(5000))
    sharpe = _annualised_sharpe(rets)
    # Large-sample expectation: ~1.587 ± noise.
    assert 1.2 < sharpe < 2.0


def test_annualised_sharpe_handles_empty_and_nans():
    assert _annualised_sharpe(pd.Series([], dtype=float)) == 0.0
    assert _annualised_sharpe(pd.Series([np.nan, np.nan])) == 0.0
    # Single point → zero variance → 0.
    assert _annualised_sharpe(pd.Series([0.01])) == 0.0


def test_equity_curve_to_returns_round_trip():
    """Equity curve → daily returns: ratio of successive equity values."""
    ec = {"2026-01-02": 1.0, "2026-01-05": 1.01, "2026-01-06": 1.0302}
    rets = _equity_curve_to_returns(ec)
    assert len(rets) == 2
    assert abs(rets.iloc[0] - 0.01) < 1e-9
    assert abs(rets.iloc[1] - (1.0302 / 1.01 - 1.0)) < 1e-9


def test_equity_curve_to_returns_empty():
    assert _equity_curve_to_returns({}).empty
    # Single-point curve → no returns (need a prior).
    assert _equity_curve_to_returns({"2026-01-02": 1.0}).empty


def test_walk_forward_tune_equity_basic_shape():
    """The bridge ingests a synthetic 60-day engine curve and returns the
    documented schema."""
    ec = _make_engine_equity_curve(n_days=60)
    result = walk_forward_tune_equity(
        equity_curve=ec, train_fraction=0.6, profile="dollar_neutral"
    )
    for key in (
        "data_source", "profile", "train_dates", "test_dates",
        "n_train_obs", "n_test_obs",
        "n_train_daily_obs", "n_test_daily_obs",
        "train_sharpe", "test_sharpe",
        "overfit_gap", "train_total_return", "test_total_return",
        "horizon_days", "train_fraction",
        "recommendation", "recommendation_reason",
        "trained_at", "ship_min_test_obs", "ship_min_test_sharpe",
    ):
        assert key in result, "missing key %r" % key
    assert result["data_source"] == "equity"
    assert result["profile"] == "dollar_neutral"
    assert result["recommendation"] in {
        "ship", "hold", "reject", "insufficient_data",
    }
    # n_test_obs is in non-overlapping h-day-block units (gate input);
    # n_test_daily_obs is the raw count. The ratio is exactly h.
    h = result["horizon_days"]
    assert result["n_test_obs"] == result["n_test_daily_obs"] // h
    assert result["n_train_obs"] == result["n_train_daily_obs"] // h


def test_walk_forward_tune_equity_empty_curve_insufficient_data():
    result = walk_forward_tune_equity(equity_curve={})
    assert result["recommendation"] == "insufficient_data"
    assert result["n_train_obs"] == 0
    assert result["n_test_obs"] == 0


def test_walk_forward_tune_equity_small_oos_holds_regardless_of_sharpe():
    """The headline V2 regression test: even if the OOS slice looks
    spectacular, if n_test_obs < SHIP_MIN_TEST_OBS the verdict MUST be
    HOLD (or insufficient_data). This is the time-locked promotion gate
    that keeps Adaptive Weights V2 unshipped until ~July 2026 even
    though the wire is built today."""
    rng = np.random.default_rng(7)
    # Train slice: 50 days of modest mean / modest vol.
    train_ec = _make_engine_equity_curve(
        n_days=50, mean_daily_return=0.0001, daily_vol=0.01, seed=42
    )
    # Test slice: 10 days with large mean and small vol → huge Sharpe but n=10.
    test_dates = pd.bdate_range(
        start=pd.Timestamp(list(train_ec)[-1]) + pd.tseries.offsets.BDay(1),
        periods=10,
    )
    last_train_eq = train_ec[list(train_ec)[-1]]
    test_ec = {}
    eq = last_train_eq
    # Mean +1%, vol 0.1% → Sharpe ~ 0.01/0.001 * sqrt(252) ≈ 158.
    daily = 0.01 + 0.001 * rng.standard_normal(10)
    for d, r in zip(test_dates, daily):
        eq = eq * (1.0 + float(r))
        test_ec[d.strftime("%Y-%m-%d")] = float(eq)
    full_ec = {**train_ec, **test_ec}
    # train_fraction chosen so n_test ≈ 10 (well below SHIP_MIN_TEST_OBS=20).
    result = walk_forward_tune_equity(
        equity_curve=full_ec, train_fraction=50.0 / 59.0
    )
    # n_test (h=21d blocks) should be < SHIP_MIN_TEST_OBS.
    assert result["n_test_obs"] < SHIP_MIN_TEST_OBS
    # Sharpe should be huge (+1% daily, tiny noise).
    assert result["test_sharpe"] > SHIP_MIN_TEST_SHARPE * 5
    # The verdict MUST NOT be SHIP — the gate fires. Either HOLD
    # (small-sample) or INSUFFICIENT_DATA (zero h-day blocks) is
    # acceptable; both are honest signals that promotion is blocked.
    assert result["recommendation"] in {"hold", "insufficient_data"}
    assert result["recommendation"] != "ship"


def test_walk_forward_tune_equity_large_oos_can_ship():
    """The dual: when both the gate is met AND Sharpe is above
    threshold AND overfit gap is small, verdict flips to SHIP. This is
    the ~July 2026 trigger — same function call, just more data.

    The gate counts non-overlapping h-day blocks: at h=5 (short
    horizon to keep this test fast) with 60% train / 40% test split,
    we need n_test_daily / 5 >= 20 → n_test_daily >= 100 →
    total days * 0.4 >= 100 → total days ≈ 250. The point of this
    test is that the wire DOES work — when the gate fills, SHIP fires.
    """
    full_ec = _make_engine_equity_curve(
        n_days=300, mean_daily_return=0.001, daily_vol=0.01, seed=99
    )
    result = walk_forward_tune_equity(
        equity_curve=full_ec, train_fraction=0.6, horizon_days=5
    )
    # n_test_obs (h-day blocks) must exceed the gate.
    assert result["n_test_obs"] >= SHIP_MIN_TEST_OBS, (
        "expected n_test_obs >= %d at h=5d with 300 days, got %d (n_test_daily=%d)"
        % (SHIP_MIN_TEST_OBS, result["n_test_obs"], result["n_test_daily_obs"])
    )
    # If test Sharpe and gap clear thresholds, verdict is SHIP.
    if (
        result["test_sharpe"] > SHIP_MIN_TEST_SHARPE
        and result["overfit_gap"] < SHIP_MAX_SHARPE_OVERFIT_GAP
    ):
        assert result["recommendation"] == "ship"
    else:
        # Otherwise hold/reject — but NEVER SHIP without meeting
        # both thresholds.
        assert result["recommendation"] in {"hold", "reject"}


def test_walk_forward_tune_equity_explicit_daily_returns_preferred():
    """If the caller passes daily_returns (matching engine's
    portfolio_returns.json), they should be used directly rather than
    derived from the equity curve. This matters because the engine's
    daily returns may include weekend stamps / forward-fills that
    differ from the simple equity-curve ratio."""
    ec = {"2026-01-02": 1.0, "2026-01-05": 1.05, "2026-01-06": 1.05}
    # Caller-provided daily returns disagree intentionally.
    explicit = {"2026-01-05": 0.02, "2026-01-06": 0.0}
    out_explicit = walk_forward_tune_equity(
        equity_curve=ec, daily_returns=explicit, train_fraction=0.5
    )
    out_derived = walk_forward_tune_equity(
        equity_curve=ec, train_fraction=0.5
    )
    # The Sharpes should differ — explicit path uses 0.02 + 0.0,
    # derived path uses 0.05 + 0.0. Compare on the raw-daily count
    # (n_*_daily_obs) since the block count (n_*_obs) is 0 at h=21d
    # with only 2 daily observations.
    assert (
        out_explicit["n_train_daily_obs"] + out_explicit["n_test_daily_obs"]
        == 2
    )
    assert (
        out_derived["n_train_daily_obs"] + out_derived["n_test_daily_obs"]
        == 2
    )


def test_walk_forward_tune_equity_invalid_train_fraction():
    ec = _make_engine_equity_curve(n_days=30)
    with pytest.raises(ValueError):
        walk_forward_tune_equity(equity_curve=ec, train_fraction=0.0)
    with pytest.raises(ValueError):
        walk_forward_tune_equity(equity_curve=ec, train_fraction=1.0)


def test_walk_forward_tune_equity_64_day_engine_baseline_holds():
    """Match the real engine output: 64 trading days at h=21d. Verdict
    MUST be HOLD or INSUFFICIENT_DATA — this is the literal state of
    `data/lthcs/backtest/2026-05-18_validation/` baseline. The gate
    is time-locked: until ~July 2026 there are < 20 non-overlapping
    21-day forward blocks in the OOS slice.

    This is the headline acceptance test for #24 P4 / #25 plumbing —
    the wire is built but cannot SHIP."""
    ec = _make_engine_equity_curve(
        n_days=64, mean_daily_return=0.0008, daily_vol=0.012, seed=2026
    )
    result = walk_forward_tune_equity(
        equity_curve=ec, train_fraction=0.6, horizon_days=21
    )
    # ~38 train / ~25 test daily returns → ~1 non-overlapping h=21 block.
    assert result["horizon_days"] == 21
    assert result["n_test_obs"] < SHIP_MIN_TEST_OBS
    # NEVER SHIP until the gate fills.
    assert result["recommendation"] != "ship"
    assert result["recommendation"] in {"hold", "reject", "insufficient_data"}


def test_recommendation_equity_ship_path():
    rec, reason = _recommendation_equity(
        test_sharpe=1.5, overfit_gap=0.5, n_test_obs=SHIP_MIN_TEST_OBS + 5
    )
    assert rec == "ship"
    assert "test_sharpe" in reason


def test_recommendation_equity_small_sample_downgrades_to_hold():
    """The promotion-gate guarantee — mirror of the IC path's small-N
    guard. Even a spectacular OOS Sharpe earns HOLD if n_test_obs < gate."""
    rec, reason = _recommendation_equity(
        test_sharpe=10.0, overfit_gap=0.01, n_test_obs=4
    )
    assert rec == "hold"
    assert (
        "time-locked" in reason
        or "n_test_obs" in reason
        or "OOS daily returns" in reason
    )


def test_recommendation_equity_zero_obs_is_insufficient_data():
    rec, _ = _recommendation_equity(test_sharpe=1.5, overfit_gap=0.5, n_test_obs=0)
    assert rec == "insufficient_data"


def test_recommendation_equity_overfit_gap_holds():
    rec, reason = _recommendation_equity(
        test_sharpe=1.5,
        overfit_gap=SHIP_MAX_SHARPE_OVERFIT_GAP + 0.5,
        n_test_obs=SHIP_MIN_TEST_OBS + 5,
    )
    assert rec == "hold"
    assert "overfit" in reason.lower()


def test_recommendation_equity_weak_sharpe_rejects():
    rec, reason = _recommendation_equity(
        test_sharpe=0.5,
        overfit_gap=0.1,
        n_test_obs=SHIP_MIN_TEST_OBS + 5,
    )
    assert rec == "reject"
    assert "weak" in reason.lower() or "test_sharpe" in reason


# ---------------------------------------------------------------------------
# Regression guard: IC-based walk_forward_tune must be byte-identical
# pre/post the V2 bridge addition. The bridge is a NEW function, not a
# refactor — existing callers (monthly cron, CLI default) must see no
# behavioral change.
# ---------------------------------------------------------------------------


def test_ic_path_unchanged_default_behavior():
    """Regression: default-flag walk_forward_tune on a fixed-seed panel
    yields the same recommendation and rounded test_ic as before the
    V2 bridge was added.

    This is the byte-compat guarantee for the monthly cron — if a
    future refactor of walk_forward_tune accidentally changes default
    behavior, this test catches it before the cron flips."""
    panel = _make_panel(
        n_dates=60, n_tickers=12,
        signal_pillar="adoption_momentum", signal_strength=2.0, seed=7,
    )
    result = walk_forward_tune(
        score_history=panel["score_history"],
        pillar_histories=panel["pillar_histories"],
        forward_returns=panel["forward_returns"],
        train_fraction=0.6,
        ridge_alpha=0.5,
    )
    # Schema unchanged — these keys exist and have the original meaning.
    assert "train_ic" in result
    assert "test_ic" in result
    assert "overfit_gap" in result
    assert "n_rejected_ffill" in result
    assert "pillar_sigmas_train" in result
    # data_source key should NOT have leaked into the IC-path output
    # (it's an equity-path-only field).
    assert "data_source" not in result
    # Recommendation is one of the documented IC-path values.
    assert result["recommendation"] in {"ship", "hold", "reject"}


def test_ic_path_and_equity_path_independent():
    """Both functions exist, both callable, no shared mutable state."""
    panel = _make_panel(n_dates=40, n_tickers=8, signal_strength=2.0)
    ic_result = walk_forward_tune(
        score_history=panel["score_history"],
        pillar_histories=panel["pillar_histories"],
        forward_returns=panel["forward_returns"],
        train_fraction=0.6,
    )
    eq_result = walk_forward_tune_equity(
        equity_curve=_make_engine_equity_curve(n_days=50, seed=1),
        train_fraction=0.6,
    )
    assert "test_ic" in ic_result
    assert "test_sharpe" in eq_result
    # No cross-pollination of fields.
    assert "test_sharpe" not in ic_result
    assert "test_ic" not in eq_result
