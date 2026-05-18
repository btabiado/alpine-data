"""LTHCS adaptive weights optimizer.

Solve for the 5-element pillar-weight vector that best explains
*cross-sectional* forward returns over a rolling window of LTHCS score
history. Uses ridge regression with L2 regularization toward an
equal-weight prior so the solution stays close to the curated
hand-tuned weights when the training sample is small.

This is a strategic capability, gated behind
``data/lthcs/weights.json::adaptive_overrides.enabled`` (default OFF).
Production keeps using ``get_maturity_weights`` until Bryan flips the
flag after a walk-forward CV report passes the ship gate.

Algorithm — :func:`tune_weights`:

1. For each ``(date, ticker)`` row, assemble:

       X_t = [adoption, institutional, financial, thesis, des]   (5-vector)
       y_t = forward_return at horizon h

   then stack across all rows into ``X (n × 5)`` and ``y (n,)``.

2. **Center X by date.** Subtract the date-wise universe mean so the
   regression is on cross-sectional spreads, not market-level drift.
   Re-add a global constant column? No — ``y`` is also centered, so the
   intercept is zero. (We mean-center y too for symmetry.)

3. **Closed-form ridge.** Minimise

       ||y - Xw||² + α · ||w - w_prior||²

   solved as ``w = (XᵀX + α·I)⁻¹ · (Xᵀy + α·w_prior)``. The default
   ``α = 0.5`` is mild; tune via cross-validation in
   :func:`walk_forward_tune`.

4. **Simplex projection.** Clip negatives to 0 and renormalize so the
   weights sum to 1.0. This keeps the output drop-in compatible with
   ``score.compute_lthcs_score``.

The module is pure Python + numpy/pandas (already on the dep list). It
imports :func:`lthcs.backtest._spearman_ic` for the diagnostic IC so the
adaptive optimizer's IC computation matches the backtest engine's
exactly (single source of truth).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from lthcs.backtest import PILLAR_NAMES, _spearman_ic


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_PRIOR = [0.20, 0.20, 0.20, 0.20, 0.20]
DEFAULT_RIDGE_ALPHA = 0.5
DEFAULT_HORIZON = 21
DEFAULT_TRAIN_FRACTION = 0.6

# Ship-gate thresholds for the walk-forward recommendation.
SHIP_MIN_TEST_IC = 0.04
SHIP_MAX_OVERFIT_GAP = 0.04


# ---------------------------------------------------------------------------
# Core math
# ---------------------------------------------------------------------------

def _assemble_design_matrix(
    score_history: pd.DataFrame,
    pillar_histories: Dict[str, pd.DataFrame],
    forward_returns: pd.DataFrame,
    universe_subset: Optional[List[str]] = None,
) -> Tuple[np.ndarray, np.ndarray, int]:
    """Stack per-(date, ticker) rows into a numeric ``(X, y)`` pair.

    X is centered *by date* so the optimizer learns cross-sectional
    weights, not market drift. y is centered by date too. Returns
    ``(X, y, n)`` where ``n`` is the number of usable rows.
    """
    if score_history is None or score_history.empty:
        return np.zeros((0, len(PILLAR_NAMES))), np.zeros((0,)), 0

    # Tickers we care about — intersect score columns with the optional
    # universe filter, then with forward_returns columns.
    cols = list(score_history.columns)
    if universe_subset is not None:
        wanted = {str(t) for t in universe_subset}
        cols = [c for c in cols if c in wanted]
    if forward_returns is not None and not forward_returns.empty:
        cols = [c for c in cols if c in forward_returns.columns]
    if not cols:
        return np.zeros((0, len(PILLAR_NAMES))), np.zeros((0,)), 0

    # Per-pillar wide frames, sliced to the same columns. Skip pillars
    # whose history is missing entirely → treat as constant 0 vector
    # (they contribute no signal but keep the X shape stable).
    pillar_frames: List[pd.DataFrame] = []
    for p in PILLAR_NAMES:
        ph = pillar_histories.get(p) if pillar_histories else None
        if ph is None or ph.empty:
            pillar_frames.append(pd.DataFrame(0.0, index=score_history.index, columns=cols))
            continue
        sliced = ph.reindex(index=score_history.index, columns=cols)
        pillar_frames.append(sliced)

    # Forward returns aligned the same way.
    fr = forward_returns.reindex(index=score_history.index, columns=cols)

    # Date-wise centering of each pillar (and y).
    rows_X: List[List[float]] = []
    rows_y: List[float] = []
    for date in score_history.index:
        # Per-date forward returns: drop NaNs to get the date's universe.
        y_row = fr.loc[date]
        valid = y_row.dropna().index
        if len(valid) < 2:
            # Need at least 2 names to center cross-sectionally.
            continue
        y_vec = y_row.loc[valid].astype(float)
        # Per-date pillar vectors; require all 5 pillars present for the
        # ticker to be included.
        per_ticker_x: Dict[str, List[float]] = {t: [] for t in valid}
        skipped: set = set()
        for pframe in pillar_frames:
            row = pframe.loc[date].reindex(valid)
            if row.isna().any():
                # mark tickers with NaN pillar as skipped
                missing = row.index[row.isna()].tolist()
                skipped.update(missing)
            for t in valid:
                v = row.get(t)
                if pd.isna(v):
                    per_ticker_x[t].append(np.nan)
                else:
                    per_ticker_x[t].append(float(v))
        keep = [t for t in valid if t not in skipped]
        if len(keep) < 2:
            continue
        x_mat = np.array([per_ticker_x[t] for t in keep], dtype=float)  # (k, 5)
        y_vec = y_vec.loc[keep].to_numpy(dtype=float)
        # Center by date.
        x_mat = x_mat - x_mat.mean(axis=0, keepdims=True)
        y_vec = y_vec - y_vec.mean()
        rows_X.extend(x_mat.tolist())
        rows_y.extend(y_vec.tolist())

    if not rows_X:
        return np.zeros((0, len(PILLAR_NAMES))), np.zeros((0,)), 0
    X = np.array(rows_X, dtype=float)
    y = np.array(rows_y, dtype=float)
    return X, y, X.shape[0]


def _ridge_closed_form(
    X: np.ndarray,
    y: np.ndarray,
    prior: np.ndarray,
    alpha: float,
) -> np.ndarray:
    """Closed-form ridge regression toward a non-zero prior.

    Solve ``min ||y - Xw||² + α · ||w - prior||²``. The normal-equation
    solution is ``(XᵀX + α·I)⁻¹ · (Xᵀy + α·prior)``.
    """
    n_features = X.shape[1] if X.size else len(prior)
    XtX = X.T @ X if X.size else np.zeros((n_features, n_features))
    Xty = X.T @ y if X.size else np.zeros((n_features,))
    A = XtX + float(alpha) * np.eye(n_features)
    b = Xty + float(alpha) * np.asarray(prior, dtype=float)
    try:
        w = np.linalg.solve(A, b)
    except np.linalg.LinAlgError:
        w = np.linalg.lstsq(A, b, rcond=None)[0]
    return w


def _project_to_simplex(w: np.ndarray) -> np.ndarray:
    """Clip negatives to 0 and renormalize to sum=1.

    If the entire vector clips to zero (degenerate), fall back to
    equal-weight. This keeps the output drop-in compatible with
    score.compute_lthcs_score which expects positive weights summing
    to 1.0.
    """
    clipped = np.clip(w, 0.0, None)
    total = clipped.sum()
    if total <= 0 or not np.isfinite(total):
        return np.full_like(w, 1.0 / len(w))
    return clipped / total


def _composite_ic(
    weights: np.ndarray,
    pillar_histories: Dict[str, pd.DataFrame],
    forward_returns: pd.DataFrame,
    universe_subset: Optional[List[str]] = None,
) -> Tuple[float, int]:
    """Compute mean cross-sectional Spearman IC of weighted composite.

    Builds a synthetic composite-score frame as
    ``Σ_p weight_p · pillar_history_p`` then delegates to
    :func:`lthcs.backtest._spearman_ic` per date (same engine as the
    backtest pillar IC report). Returns ``(mean_ic, n_dates_with_ic)``.
    """
    # Universe of tickers we'll evaluate on.
    cols_union: Optional[List[str]] = None
    for p in PILLAR_NAMES:
        ph = pillar_histories.get(p)
        if ph is None or ph.empty:
            continue
        if cols_union is None:
            cols_union = list(ph.columns)
        else:
            cols_union = [c for c in cols_union if c in ph.columns]
    if not cols_union:
        return 0.0, 0
    if universe_subset is not None:
        wanted = {str(t) for t in universe_subset}
        cols_union = [c for c in cols_union if c in wanted]
    if not cols_union:
        return 0.0, 0
    if forward_returns is None or forward_returns.empty:
        return 0.0, 0
    cols_union = [c for c in cols_union if c in forward_returns.columns]
    if not cols_union:
        return 0.0, 0

    # Build composite frame.
    composite = None
    for w_p, p in zip(weights, PILLAR_NAMES):
        ph = pillar_histories.get(p)
        if ph is None or ph.empty:
            continue
        contrib = ph.reindex(columns=cols_union).astype(float) * float(w_p)
        if composite is None:
            composite = contrib
        else:
            composite = composite.add(contrib, fill_value=0.0)
    if composite is None:
        return 0.0, 0

    # Per-date IC.
    fr = forward_returns.reindex(columns=cols_union)
    common = composite.index.intersection(fr.index)
    ics: List[float] = []
    for date in common:
        ic = _spearman_ic(composite.loc[date], fr.loc[date])
        if ic is not None:
            ics.append(ic)
    if not ics:
        return 0.0, 0
    return float(np.mean(ics)), len(ics)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def tune_weights(
    score_history: pd.DataFrame,
    pillar_histories: Dict[str, pd.DataFrame],
    forward_returns: pd.DataFrame,
    horizon_days: int = DEFAULT_HORIZON,
    ridge_alpha: float = DEFAULT_RIDGE_ALPHA,
    prior_weights: Optional[List[float]] = None,
    universe_subset: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Solve for pillar weights that best explain forward returns.

    See module docstring for the algorithm. Output schema::

        {
          "weights": {pillar_name: float, ...},
          "prior_weights": [...],
          "ridge_alpha": float,
          "horizon_days": int,
          "n_obs": int,
          "in_sample_ic": float,
          "fit_method": "ridge_regression",
          "trained_at": "<iso8601 utc>",
          "universe_subset": [...] | None,
        }

    Output weights sum to 1.0 and are non-negative.
    """
    prior_list = list(prior_weights) if prior_weights is not None else list(DEFAULT_PRIOR)
    if len(prior_list) != len(PILLAR_NAMES):
        raise ValueError(
            "prior_weights must be a %d-element list, got %d"
            % (len(PILLAR_NAMES), len(prior_list))
        )
    prior_arr = np.asarray(prior_list, dtype=float)
    # Normalize prior to sum=1 defensively (caller may pass un-normalized).
    if prior_arr.sum() > 0:
        prior_arr = prior_arr / prior_arr.sum()

    universe_list = list(universe_subset) if universe_subset is not None else None

    X, y, n_obs = _assemble_design_matrix(
        score_history=score_history,
        pillar_histories=pillar_histories or {},
        forward_returns=forward_returns,
        universe_subset=universe_list,
    )

    if n_obs == 0:
        # No usable data → return the prior unchanged, flagged so callers
        # can detect the empty-history case.
        weights_dict = {p: float(prior_arr[i]) for i, p in enumerate(PILLAR_NAMES)}
        return {
            "weights": weights_dict,
            "prior_weights": [float(x) for x in prior_arr],
            "ridge_alpha": float(ridge_alpha),
            "horizon_days": int(horizon_days),
            "n_obs": 0,
            "in_sample_ic": 0.0,
            "fit_method": "prior_fallback",
            "trained_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "universe_subset": universe_list,
        }

    raw_w = _ridge_closed_form(X, y, prior=prior_arr, alpha=float(ridge_alpha))
    w = _project_to_simplex(raw_w)

    ic_mean, _ = _composite_ic(
        weights=w,
        pillar_histories=pillar_histories or {},
        forward_returns=forward_returns,
        universe_subset=universe_list,
    )

    weights_dict = {p: float(w[i]) for i, p in enumerate(PILLAR_NAMES)}
    return {
        "weights": weights_dict,
        "prior_weights": [float(x) for x in prior_arr],
        "ridge_alpha": float(ridge_alpha),
        "horizon_days": int(horizon_days),
        "n_obs": int(n_obs),
        "in_sample_ic": float(ic_mean),
        "fit_method": "ridge_regression",
        "trained_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "universe_subset": universe_list,
    }


def _recommendation(test_ic: float, overfit_gap: float) -> Tuple[str, str]:
    """Apply the ship/hold/reject thresholds for walk-forward CV."""
    if test_ic > SHIP_MIN_TEST_IC and overfit_gap < SHIP_MAX_OVERFIT_GAP:
        return (
            "ship",
            "test_ic=%.4f > %.3f and overfit_gap=%.4f < %.3f"
            % (test_ic, SHIP_MIN_TEST_IC, overfit_gap, SHIP_MAX_OVERFIT_GAP),
        )
    if test_ic > SHIP_MIN_TEST_IC:
        return (
            "hold",
            "test_ic=%.4f passes but overfit_gap=%.4f >= %.3f — signal is real "
            "but the fit is overfit; collect more history before shipping."
            % (test_ic, overfit_gap, SHIP_MAX_OVERFIT_GAP),
        )
    return (
        "reject",
        "test_ic=%.4f <= %.3f — out-of-sample signal too weak; keep curated weights."
        % (test_ic, SHIP_MIN_TEST_IC),
    )


def walk_forward_tune(
    score_history: pd.DataFrame,
    pillar_histories: Dict[str, pd.DataFrame],
    forward_returns: pd.DataFrame,
    horizon_days: int = DEFAULT_HORIZON,
    train_fraction: float = DEFAULT_TRAIN_FRACTION,
    ridge_alpha: float = DEFAULT_RIDGE_ALPHA,
    prior_weights: Optional[List[float]] = None,
    universe_subset: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Walk-forward cross-validation: fit on first ``train_fraction``,
    measure IC on the held-out tail.

    Output schema::

        {
          "train_weights": {pillar: w, ...},
          "train_ic": float,
          "test_ic": float,
          "overfit_gap": float,                # train_ic - test_ic
          "train_dates": (start, end),
          "test_dates": (start, end),
          "n_train_obs": int,
          "n_test_obs": int,
          "recommendation": "ship"|"hold"|"reject",
          "recommendation_reason": str,
          "ridge_alpha": float,
          "horizon_days": int,
          "train_fraction": float,
          "prior_weights": [...],
          "trained_at": "<iso>",
        }

    The recommendation logic:
      * **ship**   if test_ic > 0.04 AND overfit_gap < 0.04
      * **hold**   if test_ic > 0.04 BUT overfit_gap >= 0.04
      * **reject** otherwise
    """
    if not 0.0 < train_fraction < 1.0:
        raise ValueError("train_fraction must be in (0, 1), got %r" % (train_fraction,))

    universe_list = list(universe_subset) if universe_subset is not None else None
    prior_list = list(prior_weights) if prior_weights is not None else list(DEFAULT_PRIOR)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if score_history is None or score_history.empty:
        empty_w = {p: float(v) for p, v in zip(PILLAR_NAMES, prior_list)}
        return {
            "train_weights": empty_w,
            "train_ic": 0.0,
            "test_ic": 0.0,
            "overfit_gap": 0.0,
            "train_dates": (None, None),
            "test_dates": (None, None),
            "n_train_obs": 0,
            "n_test_obs": 0,
            "recommendation": "reject",
            "recommendation_reason": "empty score history",
            "ridge_alpha": float(ridge_alpha),
            "horizon_days": int(horizon_days),
            "train_fraction": float(train_fraction),
            "prior_weights": [float(x) for x in prior_list],
            "trained_at": now_iso,
        }

    sorted_dates = list(score_history.sort_index().index)
    n_dates = len(sorted_dates)
    split = int(round(train_fraction * n_dates))
    # Guarantee at least one date in each split when possible.
    split = max(1, min(split, n_dates - 1)) if n_dates >= 2 else n_dates
    train_dates = sorted_dates[:split]
    test_dates = sorted_dates[split:]

    def _slice(frame: Optional[pd.DataFrame], dates: List) -> pd.DataFrame:
        if frame is None or frame.empty or not dates:
            return pd.DataFrame()
        return frame.loc[frame.index.isin(dates)].copy()

    train_score = _slice(score_history, train_dates)
    test_score = _slice(score_history, test_dates)
    train_pillars = {p: _slice(pillar_histories.get(p), train_dates) for p in PILLAR_NAMES}
    test_pillars = {p: _slice(pillar_histories.get(p), test_dates) for p in PILLAR_NAMES}
    train_fwd = _slice(forward_returns, train_dates)
    test_fwd = _slice(forward_returns, test_dates)

    # Train.
    train_result = tune_weights(
        score_history=train_score,
        pillar_histories=train_pillars,
        forward_returns=train_fwd,
        horizon_days=horizon_days,
        ridge_alpha=ridge_alpha,
        prior_weights=prior_list,
        universe_subset=universe_list,
    )
    train_w_arr = np.array(
        [train_result["weights"][p] for p in PILLAR_NAMES], dtype=float
    )

    # Out-of-sample IC: same trained weights, evaluated on the test slice.
    test_ic, n_test_obs = _composite_ic(
        weights=train_w_arr,
        pillar_histories=test_pillars,
        forward_returns=test_fwd,
        universe_subset=universe_list,
    )

    train_ic = float(train_result["in_sample_ic"])
    overfit_gap = float(train_ic - test_ic)
    rec, reason = _recommendation(float(test_ic), overfit_gap)

    def _fmt(d) -> Optional[str]:
        if d is None:
            return None
        try:
            return d.strftime("%Y-%m-%d")
        except AttributeError:
            return str(d)

    return {
        "train_weights": train_result["weights"],
        "train_ic": train_ic,
        "test_ic": float(test_ic),
        "overfit_gap": overfit_gap,
        "train_dates": (
            _fmt(train_dates[0]) if train_dates else None,
            _fmt(train_dates[-1]) if train_dates else None,
        ),
        "test_dates": (
            _fmt(test_dates[0]) if test_dates else None,
            _fmt(test_dates[-1]) if test_dates else None,
        ),
        "n_train_obs": int(train_result["n_obs"]),
        "n_test_obs": int(n_test_obs),
        "recommendation": rec,
        "recommendation_reason": reason,
        "ridge_alpha": float(ridge_alpha),
        "horizon_days": int(horizon_days),
        "train_fraction": float(train_fraction),
        "prior_weights": train_result["prior_weights"],
        "trained_at": now_iso,
    }


__all__ = [
    "PILLAR_NAMES",
    "DEFAULT_PRIOR",
    "DEFAULT_RIDGE_ALPHA",
    "DEFAULT_HORIZON",
    "DEFAULT_TRAIN_FRACTION",
    "SHIP_MIN_TEST_IC",
    "SHIP_MAX_OVERFIT_GAP",
    "tune_weights",
    "walk_forward_tune",
]
