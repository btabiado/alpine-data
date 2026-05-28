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
   y is also date-centered for symmetry (intercept = 0).

3. **Z-score X by pillar.** After centering, divide each pillar column
   by its global panel standard deviation. This puts all pillars on a
   comparable scale before the ridge fit — otherwise pillars with much
   larger raw spreads (adoption σ ≈ 29) dominate the loss and the
   simplex projection on tiny coefs becomes a sign-of-noise lottery
   (the thesis-integrity-99% artifact from the pre-fix walk-forward
   report). See ``_zscore_columns`` and ``_unscale_coefs``.

4. **Closed-form ridge.** Minimise

       ||y - Z·w_z||² + α_eff · ||w_z - w_prior||²

   solved as ``w_z = (ZᵀZ + α_eff·I)⁻¹ · (Zᵀy + α_eff·w_prior)``. The
   effective alpha is ``α_eff = user_alpha · n_obs`` — i.e. the
   user-facing ``ridge_alpha`` argument is a unit-free fraction in
   ``[0, ~1]`` that multiplies the sample count internally. After
   z-scoring, ``ZᵀZ`` diagonals scale as ``n_obs`` (each column has
   unit variance), so this parameterization gives a knob whose default
   ``0.5`` is genuinely "halfway between OLS and prior" — the pre-fix
   ``α ∈ [0.1, 5.0]`` was effectively zero relative to ``XᵀX`` on
   un-scaled pillar columns (entries ~10⁶).

5. **Transform back.** The fitted ``w_z`` is in z-scored units.
   ``compute_lthcs_score`` consumes raw 0–100 sub-scores, so we
   transform back to raw-pillar coefficients via ``w_raw = w_z / σ_p``
   for each pillar ``p``. Then **simplex projection**: clip negatives
   to 0 and renormalize so weights sum to 1.0.

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
# ``ridge_alpha`` is now a unit-free fraction in [0, ~1] that multiplies
# ``n_obs`` internally (see module docstring step 4). The default 0.5
# corresponds to ~half-strength regularization toward the equal-weight
# prior. BREAKING CHANGE from the pre-fix module where ridge_alpha was
# the literal scalar passed to the normal equation — see the migration
# note in :func:`tune_weights`.
DEFAULT_RIDGE_ALPHA = 0.5
DEFAULT_HORIZON = 21
DEFAULT_TRAIN_FRACTION = 0.6

# Ship-gate thresholds for the walk-forward recommendation.
SHIP_MIN_TEST_IC = 0.04
SHIP_MAX_OVERFIT_GAP = 0.04
# Minimum number of REAL out-of-sample cross-sections (after Bug 1
# ffill rejection) before a ship verdict is even considered. With fewer
# than this many independent test dates, the IC estimate is too noisy
# to trust regardless of point value. 20 trading-day cross-sections is
# roughly one calendar month of independent forward windows at h=21d.
SHIP_MIN_TEST_OBS = 20

# Ship-gate thresholds for the equity-curve walk-forward bridge
# (Adaptive Weights V2, Tier 5 #24 P4 / #25). The IC-based path scores
# a candidate weight vector by mean cross-sectional Spearman IC against
# forward returns; the equity-curve path scores it by the annualised
# Sharpe ratio of the resulting *daily-return* time series emitted by
# the backtest engine. Sharpe is the apples-to-apples gate because the
# engine output is already a P&L curve, not a cross-section.
#
# Threshold rationale: SHIP_MIN_TEST_SHARPE = 1.0 corresponds to
# roughly a Sharpe ≥ 1 OOS — a low bar by hedge-fund standards but a
# real signal in a long-only or long/short equity strategy at daily
# rebalance. The promotion gate is intentionally Sharpe-based (not
# total-return) so it isn't fooled by a single big-tail day. As with
# IC, SHIP_MIN_TEST_OBS (number of trading-day OOS daily returns) is
# the binding constraint until ~July 2026 — see
# `data/lthcs/adaptive_weights/2026-05-18_walk_forward_after_fixes.md`.
SHIP_MIN_TEST_SHARPE = 1.0
SHIP_MAX_SHARPE_OVERFIT_GAP = 1.5

# Floor for per-column standard deviation when z-scoring. Pillars with
# essentially no cross-sectional variance (e.g. all tickers stuck at the
# same thesis_integrity sub-score for the whole window) would otherwise
# explode the un-scaling step. With this floor, a near-constant pillar
# gets coefficient ~0 in raw-unit space, which is the honest answer.
_STD_FLOOR = 1e-6


# ---------------------------------------------------------------------------
# Helpers — forward-return ffill detection (Bug 1)
# ---------------------------------------------------------------------------

def _build_ffill_mask(forward_returns: pd.DataFrame) -> pd.DataFrame:
    """Return a per-cell boolean mask marking entries that look ffilled.

    ``backtest.fetch_forward_returns`` forward-fills the most recent
    real return onto subsequent calendar days (to support weekend/
    holiday score stamps), AND onto the tail dates where ``close[t+h]``
    is not yet observed because the price cache ends earlier than
    ``end_date + h``. The latter is the bug we care about: it makes
    many obs-dates inherit the same frozen return cross-section, which
    inflates ``test_ic`` in walk-forward CV.

    Heuristic: per ticker column, a cell is ffilled iff its value
    equals the immediately-preceding indexed value AND that preceding
    value is not NaN. The very first row per column is always real
    (no prior to compare). NaN cells are flagged as "not real" (they
    have no value at all).

    Floating-point forward returns essentially never produce two
    consecutive exactly-equal values from independent real prices, so
    this catches both the tail ffill and the weekend ffill cleanly.

    Returns a boolean DataFrame the same shape as ``forward_returns``,
    where ``True`` means "this cell looks like an ffilled duplicate /
    NaN (NOT a real new forward return)".
    """
    if forward_returns is None or forward_returns.empty:
        return pd.DataFrame()
    sorted_fr = forward_returns.sort_index()
    prev = sorted_fr.shift(1)
    # Duplicate iff value equals prior AND prior is not NaN.
    dup = (sorted_fr == prev) & prev.notna()
    # NaN cells are also "not real".
    nan_mask = sorted_fr.isna()
    return (dup | nan_mask).reindex(forward_returns.index)


def _apply_real_mask(
    forward_returns: pd.DataFrame,
    real_mask: Optional[pd.DataFrame],
) -> Tuple[pd.DataFrame, int]:
    """Replace ffilled / non-real cells with NaN so downstream code drops them.

    Returns ``(masked_frame, n_rejected_cells)`` where ``n_rejected_cells``
    counts how many previously-non-NaN cells were nullified.
    """
    if forward_returns is None or forward_returns.empty:
        return forward_returns, 0
    if real_mask is None:
        real_mask = ~_build_ffill_mask(forward_returns)
    # Align mask shape; missing entries treated as "not real".
    aligned = real_mask.reindex(
        index=forward_returns.index, columns=forward_returns.columns
    ).fillna(False).astype(bool)
    not_real = ~aligned
    # Count cells we're about to nullify that previously held a number.
    rejected = int((not_real & forward_returns.notna()).sum().sum())
    masked = forward_returns.where(aligned)
    return masked, rejected


# ---------------------------------------------------------------------------
# Helpers — z-scoring (Bug 2)
# ---------------------------------------------------------------------------

def _zscore_columns(X: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Z-score each column of X by its panel standard deviation.

    X is assumed to be already date-centered (column means ≈ 0 within
    each date, but not necessarily exactly zero across the full panel
    after stacking — that's fine; we treat the post-stacked column mean
    as our "panel mean" and divide by the post-stacked column std).

    Returns ``(Z, sigma)`` where ``Z = (X - μ_col) / σ_col`` and
    ``sigma`` is the *raw* per-column std (no floor applied — callers
    that need to detect degenerate columns can inspect σ directly).
    For the division step we substitute the floor internally so the
    z-scored output is finite; for degenerate columns ``Z`` is forced
    to zero so they contribute nothing to the ridge fit. The mean is
    *recorded* but not separately returned because we reconstruct
    raw-unit coefs via ``w_raw = w_z / σ`` — the mean cancels in
    cross-sectional ranking (compute_lthcs_score is rank-monotonic in
    any affine transform of a pillar).
    """
    if X.size == 0:
        return X, np.ones(X.shape[1], dtype=float) if X.ndim == 2 else np.array([])
    mu = X.mean(axis=0, keepdims=True)
    sigma = X.std(axis=0, ddof=0)
    degenerate = sigma < _STD_FLOOR
    sigma_for_div = np.where(degenerate, 1.0, sigma)
    Z = (X - mu) / sigma_for_div
    # Force degenerate columns to literal zero in Z so the ridge fit
    # sees no signal there (any tiny FP noise from a "constant" pillar
    # would otherwise leak into w_z).
    if degenerate.any():
        Z[:, degenerate] = 0.0
    return Z, sigma


def _unscale_coefs(w_z: np.ndarray, sigma: np.ndarray) -> np.ndarray:
    """Transform z-scored coefficients back to raw-pillar units.

    Math: if the fitted model is ``y ≈ Z @ w_z`` and ``Z = (X - μ) / σ``,
    then ``y ≈ ((X - μ) / σ) @ w_z = X @ (w_z / σ) - constant``. The
    constant ``μ @ (w_z / σ)`` is absorbed by the date-wise centering
    of y (intercept ≈ 0), so the production-ready raw-unit coefficient
    is simply ``w_raw_i = w_z_i / σ_i``.

    Why this matters: ``compute_lthcs_score`` takes raw 0–100 sub-scores
    and computes ``Σ w_p · subscore_p``. If we shipped ``w_z`` directly,
    a pillar with σ=29 (adoption) would be penalised relative to a
    pillar with σ=1.2 (thesis) — exactly the original bug. Dividing by
    σ in the un-scaling step restores the right relative magnitudes
    so the simplex projection acts on comparable numbers.

    Degenerate-column guard: if a pillar has near-zero cross-sectional
    variance over the panel (σ_i below ``_STD_FLOOR``), it can't carry
    cross-sectional signal regardless of what the ridge fit returns
    (which in that case is dominated by the prior). Dividing the prior
    by σ_floor would blow up into a giant raw-unit coefficient and
    re-create the Bug 2 winner-take-all artifact in reverse. Instead
    we set ``w_raw_i = 0`` for such pillars — the honest reading is
    "this pillar has no usable variance on this window, so it earns
    no data-driven weight". The simplex projection then renormalizes
    the remaining pillars; if every pillar is degenerate, the projector
    falls back to equal-weight.
    """
    sigma_arr = np.asarray(sigma, dtype=float)
    w_z_arr = np.asarray(w_z, dtype=float)
    # Below-floor pillars get zero raw-unit coefficient.
    degenerate = sigma_arr < _STD_FLOOR
    sigma_safe = np.where(degenerate, 1.0, sigma_arr)
    w_raw = w_z_arr / sigma_safe
    w_raw = np.where(degenerate, 0.0, w_raw)
    return w_raw


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

    NOTE: This function returns *date-centered raw-unit* X. The
    z-scoring (Bug 2 fix) happens one level up in :func:`tune_weights`
    so the per-column std can be captured and used to un-scale the
    fitted coefficients back to raw units.
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

    NOTE: This primitive is the *math kernel*; ``alpha`` here is the
    raw scalar fed to the normal equation. The user-facing
    ``ridge_alpha`` argument in :func:`tune_weights` is rescaled by
    ``n_obs`` before being passed in here (see Bug 3 fix in the
    module docstring).
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
    forward_returns_real_mask: Optional[pd.DataFrame] = None,
    reject_ffill: bool = True,
) -> Dict[str, Any]:
    """Solve for pillar weights that best explain forward returns.

    See module docstring for the algorithm. Output schema::

        {
          "weights": {pillar_name: float, ...},
          "prior_weights": [...],
          "ridge_alpha": float,             # the user-facing value
          "ridge_alpha_effective": float,   # user_alpha * n_obs
          "horizon_days": int,
          "n_obs": int,
          "n_rejected_ffill": int,          # cells nulled by Bug 1 fix
          "pillar_sigmas": {pillar: float, ...},  # per-column std on Z
          "in_sample_ic": float,
          "fit_method": "ridge_regression",
          "trained_at": "<iso8601 utc>",
          "universe_subset": [...] | None,
        }

    Output weights sum to 1.0 and are non-negative.

    **Breaking change vs the pre-fix module (commit 2bcacd3)**:
    ``ridge_alpha`` is now a unit-free fraction in ``[0, ~1]`` that
    multiplies ``n_obs`` internally. Pre-fix, the same name held the
    raw scalar fed to the normal equation. The new default ``0.5``
    gives roughly half-strength regularization toward the prior on
    z-scored features; pre-fix ``0.5`` was effectively zero against
    raw-unit pillar columns. Callers passing very large historical
    values (e.g. ``ridge_alpha=1e6``) will continue to hug the prior
    as before — the rescaling only makes small values *more* effective,
    so there is no silent behavior change at the extremes.

    Bug 1 fix: if ``reject_ffill=True`` (default), any forward-return
    cell that appears to be a forward-filled duplicate (or a NaN) is
    nullified before assembling the design matrix. The count is
    returned as ``n_rejected_ffill``. Callers who have already
    pre-masked their ``forward_returns`` (or who know the cache is
    fully real through ``end_date + horizon``) can disable this by
    passing ``reject_ffill=False``.
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

    # --- Bug 1: reject ffilled forward-return cells ---
    n_rejected = 0
    fr_use = forward_returns
    if reject_ffill and forward_returns is not None and not forward_returns.empty:
        real_mask: Optional[pd.DataFrame] = forward_returns_real_mask
        fr_use, n_rejected = _apply_real_mask(forward_returns, real_mask)

    X, y, n_obs = _assemble_design_matrix(
        score_history=score_history,
        pillar_histories=pillar_histories or {},
        forward_returns=fr_use if fr_use is not None else pd.DataFrame(),
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
            "ridge_alpha_effective": 0.0,
            "horizon_days": int(horizon_days),
            "n_obs": 0,
            "n_rejected_ffill": int(n_rejected),
            "pillar_sigmas": {p: 0.0 for p in PILLAR_NAMES},
            "in_sample_ic": 0.0,
            "fit_method": "prior_fallback",
            "trained_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "universe_subset": universe_list,
        }

    # --- Bug 2: z-score the design matrix ---
    Z, sigma = _zscore_columns(X)

    # --- Bug 3: rescale alpha by n_obs ---
    alpha_effective = float(ridge_alpha) * float(n_obs)

    # Fit on z-scored X, then transform back to raw-unit coefs.
    w_z = _ridge_closed_form(Z, y, prior=prior_arr, alpha=alpha_effective)
    w_raw = _unscale_coefs(w_z, sigma)
    w = _project_to_simplex(w_raw)

    # IC computation runs on the post-rejection forward_returns (so the
    # in-sample IC is honest about which dates have real returns), but
    # against the raw pillar histories (compute_lthcs_score consumes raw).
    ic_mean, _ = _composite_ic(
        weights=w,
        pillar_histories=pillar_histories or {},
        forward_returns=fr_use if fr_use is not None else pd.DataFrame(),
        universe_subset=universe_list,
    )

    weights_dict = {p: float(w[i]) for i, p in enumerate(PILLAR_NAMES)}
    sigma_dict = {p: float(sigma[i]) for i, p in enumerate(PILLAR_NAMES)}
    return {
        "weights": weights_dict,
        "prior_weights": [float(x) for x in prior_arr],
        "ridge_alpha": float(ridge_alpha),
        "ridge_alpha_effective": float(alpha_effective),
        "horizon_days": int(horizon_days),
        "n_obs": int(n_obs),
        "n_rejected_ffill": int(n_rejected),
        "pillar_sigmas": sigma_dict,
        "in_sample_ic": float(ic_mean),
        "fit_method": "ridge_regression",
        "trained_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "universe_subset": universe_list,
    }


def _recommendation(
    test_ic: float,
    overfit_gap: float,
    n_test_obs: Optional[int] = None,
) -> Tuple[str, str]:
    """Apply the ship/hold/reject thresholds for walk-forward CV.

    Small-sample guard: if ``n_test_obs`` is provided and is below
    ``SHIP_MIN_TEST_OBS``, the verdict is at best ``hold`` regardless
    of the point IC value — the OOS IC estimate on a handful of
    cross-sections is too noisy to support a SHIP recommendation, even
    when the point estimate is large. Set ``n_test_obs=None`` (default)
    to skip this guard for backward compatibility with callers that
    don't pass the count.
    """
    # Small-sample guard: a great-looking test IC on N<SHIP_MIN_TEST_OBS
    # real cross-sections doesn't earn a SHIP. Downgrade to HOLD if the
    # IC otherwise passes the threshold, else REJECT.
    small_sample = (
        n_test_obs is not None and 0 < n_test_obs < SHIP_MIN_TEST_OBS
    )
    if test_ic > SHIP_MIN_TEST_IC and overfit_gap < SHIP_MAX_OVERFIT_GAP and not small_sample:
        return (
            "ship",
            "test_ic=%.4f > %.3f and overfit_gap=%.4f < %.3f"
            % (test_ic, SHIP_MIN_TEST_IC, overfit_gap, SHIP_MAX_OVERFIT_GAP),
        )
    if test_ic > SHIP_MIN_TEST_IC and small_sample:
        return (
            "hold",
            "test_ic=%.4f passes but only n_test_obs=%d real cross-sections "
            "(<%d). OOS estimate is too noisy on this sample size to ship; "
            "collect more history first."
            % (test_ic, int(n_test_obs), SHIP_MIN_TEST_OBS),
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
    forward_returns_real_mask: Optional[pd.DataFrame] = None,
    reject_ffill: bool = True,
) -> Dict[str, Any]:
    """Walk-forward cross-validation: fit on first ``train_fraction``,
    measure IC on the held-out tail.

    Bug 1 fix: the test split rejects any (date, ticker) cell whose
    forward-return value looks ffilled — i.e. a frozen duplicate from
    earlier dates (because the price cache hasn't yet observed
    ``close[date + horizon]``). The count of nullified cells is
    returned as ``n_rejected_ffill`` (sum across train + test splits).
    Truncating test_dates to only dates with at least 2 surviving
    tickers happens automatically inside ``_assemble_design_matrix``
    and ``_composite_ic`` — a date with all-NaN forward returns has
    no IC and is simply skipped.

    Output schema::

        {
          "train_weights": {pillar: w, ...},
          "train_ic": float,
          "test_ic": float,
          "overfit_gap": float,                # train_ic - test_ic
          "train_dates": (start, end),
          "test_dates": (start, end),
          "test_dates_after_ffill_reject": (start, end),  # actually-used
          "n_train_obs": int,
          "n_test_obs": int,                   # cross-sections with real IC
          "n_rejected_ffill": int,             # train + test combined
          "ridge_alpha": float,
          "ridge_alpha_effective_train": float,
          "horizon_days": int,
          "train_fraction": float,
          "prior_weights": [...],
          "pillar_sigmas_train": {pillar: float, ...},
          "recommendation": "ship"|"hold"|"reject",
          "recommendation_reason": str,
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
            "test_dates_after_ffill_reject": (None, None),
            "n_train_obs": 0,
            "n_test_obs": 0,
            "n_rejected_ffill": 0,
            "recommendation": "reject",
            "recommendation_reason": "empty score history",
            "ridge_alpha": float(ridge_alpha),
            "ridge_alpha_effective_train": 0.0,
            "horizon_days": int(horizon_days),
            "train_fraction": float(train_fraction),
            "prior_weights": [float(x) for x in prior_list],
            "pillar_sigmas_train": {p: 0.0 for p in PILLAR_NAMES},
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

    # Bug 1: build the ffill mask on the FULL forward_returns frame
    # (so a row that's ffilled because of a weekend rolls forward from
    # the most recent prior real value, even if that prior date sits in
    # the train slice). Then split.
    if forward_returns is not None and not forward_returns.empty:
        if forward_returns_real_mask is not None:
            full_real_mask = forward_returns_real_mask
        elif reject_ffill:
            full_real_mask = ~_build_ffill_mask(forward_returns)
        else:
            full_real_mask = pd.DataFrame(
                True, index=forward_returns.index, columns=forward_returns.columns
            )
    else:
        full_real_mask = None

    train_fwd = _slice(forward_returns, train_dates)
    test_fwd = _slice(forward_returns, test_dates)
    train_mask = _slice(full_real_mask, train_dates) if full_real_mask is not None else None
    test_mask = _slice(full_real_mask, test_dates) if full_real_mask is not None else None

    # Train. Pass the precomputed mask so the count is accurate per-split.
    train_result = tune_weights(
        score_history=train_score,
        pillar_histories=train_pillars,
        forward_returns=train_fwd,
        horizon_days=horizon_days,
        ridge_alpha=ridge_alpha,
        prior_weights=prior_list,
        universe_subset=universe_list,
        forward_returns_real_mask=train_mask,
        reject_ffill=reject_ffill,
    )
    train_w_arr = np.array(
        [train_result["weights"][p] for p in PILLAR_NAMES], dtype=float
    )

    # Out-of-sample IC: same trained weights, evaluated on the test slice
    # AFTER ffill rejection (Bug 1 fix). We mask the test forward-returns
    # frame ourselves so we can also report the actually-used test date
    # range for transparency.
    n_rejected_test = 0
    if reject_ffill and test_fwd is not None and not test_fwd.empty:
        test_fwd_masked, n_rejected_test = _apply_real_mask(test_fwd, test_mask)
    else:
        test_fwd_masked = test_fwd if test_fwd is not None else pd.DataFrame()

    # Real test dates: those with at least one non-NaN forward return.
    if test_fwd_masked is None or test_fwd_masked.empty:
        real_test_dates: List = []
    else:
        rt = test_fwd_masked.dropna(how="all")
        real_test_dates = list(rt.sort_index().index)

    test_ic, n_test_obs = _composite_ic(
        weights=train_w_arr,
        pillar_histories=test_pillars,
        forward_returns=test_fwd_masked,
        universe_subset=universe_list,
    )

    train_ic = float(train_result["in_sample_ic"])
    overfit_gap = float(train_ic - test_ic)
    rec, reason = _recommendation(
        float(test_ic), overfit_gap, n_test_obs=int(n_test_obs)
    )

    def _fmt(d) -> Optional[str]:
        if d is None:
            return None
        try:
            return d.strftime("%Y-%m-%d")
        except AttributeError:
            return str(d)

    n_rejected_total = int(train_result.get("n_rejected_ffill", 0)) + int(n_rejected_test)

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
        "test_dates_after_ffill_reject": (
            _fmt(real_test_dates[0]) if real_test_dates else None,
            _fmt(real_test_dates[-1]) if real_test_dates else None,
        ),
        "n_train_obs": int(train_result["n_obs"]),
        "n_test_obs": int(n_test_obs),
        "n_rejected_ffill": n_rejected_total,
        "recommendation": rec,
        "recommendation_reason": reason,
        "ridge_alpha": float(ridge_alpha),
        "ridge_alpha_effective_train": float(train_result.get("ridge_alpha_effective", 0.0)),
        "horizon_days": int(horizon_days),
        "train_fraction": float(train_fraction),
        "prior_weights": train_result["prior_weights"],
        "pillar_sigmas_train": train_result.get("pillar_sigmas", {p: 0.0 for p in PILLAR_NAMES}),
        "trained_at": now_iso,
    }


# ---------------------------------------------------------------------------
# Engine bridge — Adaptive Weights V2 prep (Tier 5 #24 P4 + #25)
#
# The IC-based walk_forward_tune above scores candidate pillar weights by
# mean cross-sectional Spearman IC of the weighted *composite score* vs
# forward returns. The backtest engine (`lthcs/backtest_engine.py`,
# Phases 1-3) already emits per-day equity curves and daily returns for
# each profile. The bridge below ingests those equity curves and scores a
# candidate weight vector by the annualised Sharpe ratio of the OOS slice.
#
# This is the *plumbing* for Adaptive Weights V2. The promotion gate
# (`SHIP_MIN_TEST_OBS = 20`) remains time-locked — until ~July 2026 the
# engine has fewer trading days than the gate requires, so the verdict
# will be HOLD regardless of how good the Sharpe looks. When the gate
# fills, the same function will start emitting SHIP automatically; no
# code change is needed at flip time, only the data accumulation.
#
# Design notes:
#   * `walk_forward_tune` (IC path) is byte-identical pre/post — the
#     bridge is a *new* function, not a refactor. Default behavior of
#     all existing callers (CLI, monthly cron, tests) is unchanged.
#   * The bridge accepts engine artifacts in the exact JSON shapes
#     written under `data/lthcs/backtest/<run_id>/`:
#       - `equity_curve.json`: {"YYYY-MM-DD": float, ...}
#       - `daily_returns.json` (optional): same shape, daily simple
#         returns. If not provided, the bridge derives daily returns
#         from successive equity-curve ratios.
#       - The portfolio_returns.json shape (with `horizon_Nd` sub-blocks)
#         is also accepted via `equity_curve_from_returns_block()`.
#   * Sharpe is annualised with 252 trading-day convention to match
#     `lthcs/backtest_engine.py:portfolio["horizon_Nd"]["sharpe_annualised"]`.
#   * The bridge does NOT re-fit weights from equity curves — the weight
#     vector is taken as-given (e.g. from the IC-based tuner, or curated)
#     and the equity-curve path scores its OOS P&L. Re-fitting weights
#     against equity curves is a future iteration; today the goal is the
#     SHIP-gate wire so the verdict flips automatically at ~July 2026.
# ---------------------------------------------------------------------------

_TRADING_DAYS_PER_YEAR = 252


def _equity_curve_to_returns(equity_curve: Dict[str, float]) -> pd.Series:
    """Convert an engine equity-curve dict to a daily simple-return Series.

    ``equity_curve`` is the JSON shape written by the backtest engine to
    ``data/lthcs/backtest/<run_id>/equity_curve.json`` (or any per-profile
    subdir): ``{"YYYY-MM-DD": float_equity, ...}``. The first date's
    return is dropped (no prior to ratio against); subsequent returns
    are ``equity[t] / equity[t-1] - 1``.
    """
    if not equity_curve:
        return pd.Series(dtype=float)
    s = pd.Series(equity_curve, dtype=float)
    s.index = pd.to_datetime(s.index)
    s = s.sort_index()
    rets = s.pct_change().dropna()
    return rets


def _annualised_sharpe(daily_returns: pd.Series) -> float:
    """Annualised Sharpe at 252 trading-day convention.

    Matches the formula used by ``lthcs/backtest_engine.py`` for the
    ``portfolio["horizon_Nd"]["sharpe_annualised"]`` field — single
    source of truth for the engine→bridge handshake. Returns 0.0 on an
    empty or zero-variance input rather than raising, so callers can
    treat "no signal" as a numeric 0 without try/except.
    """
    if daily_returns is None or daily_returns.empty:
        return 0.0
    arr = np.asarray(daily_returns.values, dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return 0.0
    sd = float(np.std(arr, ddof=0))
    if sd <= 0 or not np.isfinite(sd):
        return 0.0
    mean = float(np.mean(arr))
    return mean / sd * float(np.sqrt(_TRADING_DAYS_PER_YEAR))


def _recommendation_equity(
    test_sharpe: float,
    overfit_gap: float,
    n_test_obs: Optional[int] = None,
) -> Tuple[str, str]:
    """Apply the equity-curve ship/hold/reject thresholds.

    Mirrors ``_recommendation`` but on Sharpe (engine's primary
    portfolio statistic) instead of IC. The small-sample guard is the
    SAME constant — ``SHIP_MIN_TEST_OBS`` — and ``n_test_obs`` here is
    the count of **non-overlapping h-day forward blocks** in the OOS
    slice, NOT daily return observations. This matches the IC-path's
    intent ("20 trading-day cross-sections is roughly one calendar
    month of independent forward windows at h=21d"); without the
    non-overlap reduction the 64-day engine baseline would already
    spuriously SHIP at h=21d because 25 overlapping daily returns
    pass the daily-obs gate trivially.

    Returns ``(verdict, reason)`` where verdict is one of
    ``"ship" | "hold" | "reject" | "insufficient_data"``.

    * ``"insufficient_data"`` is a strict superset of the old
      small-sample HOLD — when *no* test returns at all exist (e.g.
      train_fraction too high) the verdict reflects that explicitly
      rather than getting blended into a HOLD-on-noise.
    """
    if n_test_obs is not None and n_test_obs <= 0:
        return (
            "insufficient_data",
            "n_test_obs=%d — no out-of-sample trading-day returns to score; "
            "engine slice too short or train_fraction too high."
            % (int(n_test_obs),),
        )
    # n_test_obs <= 0 already returned "insufficient_data" above, so when
    # not None it's strictly positive — only the upper bound needs testing.
    small_sample = (
        n_test_obs is not None and n_test_obs < SHIP_MIN_TEST_OBS
    )
    if (
        test_sharpe > SHIP_MIN_TEST_SHARPE
        and overfit_gap < SHIP_MAX_SHARPE_OVERFIT_GAP
        and not small_sample
    ):
        return (
            "ship",
            "test_sharpe=%.4f > %.3f and overfit_gap=%.4f < %.3f"
            % (
                test_sharpe,
                SHIP_MIN_TEST_SHARPE,
                overfit_gap,
                SHIP_MAX_SHARPE_OVERFIT_GAP,
            ),
        )
    if small_sample:
        return (
            "hold",
            "test_sharpe=%.4f%s but only n_test_obs=%d non-overlapping "
            "h-day forward blocks (<%d). Engine OOS slice is too short to "
            "ship; promotion gate remains time-locked until ~July 2026."
            % (
                test_sharpe,
                " passes ship threshold" if test_sharpe > SHIP_MIN_TEST_SHARPE else "",
                int(n_test_obs),
                SHIP_MIN_TEST_OBS,
            ),
        )
    if test_sharpe > SHIP_MIN_TEST_SHARPE:
        return (
            "hold",
            "test_sharpe=%.4f passes but overfit_gap=%.4f >= %.3f — signal "
            "is real but the fit may be overfit; collect more history "
            "before shipping."
            % (test_sharpe, overfit_gap, SHIP_MAX_SHARPE_OVERFIT_GAP),
        )
    return (
        "reject",
        "test_sharpe=%.4f <= %.3f — out-of-sample equity-curve signal too "
        "weak; keep curated weights."
        % (test_sharpe, SHIP_MIN_TEST_SHARPE),
    )


def walk_forward_tune_equity(
    equity_curve: Dict[str, float],
    daily_returns: Optional[Dict[str, float]] = None,
    train_fraction: float = DEFAULT_TRAIN_FRACTION,
    horizon_days: int = DEFAULT_HORIZON,
    profile: Optional[str] = None,
    weights: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """Equity-curve walk-forward CV — Adaptive Weights V2 bridge.

    Ingest a backtest-engine equity curve (and optionally a daily-return
    series — derived from the curve if not provided), split into
    train/test on the engine's time index, and score the OOS slice by
    annualised Sharpe. Returns the same verdict-shaped dict as
    :func:`walk_forward_tune` but with equity-curve diagnostics.

    The function does NOT fit pillar weights from equity curves; the
    intended use is:
      1. The IC-based ``tune_weights`` produces candidate weights from
         the score panel (already wired).
      2. The backtest engine runs those weights through the strategy
         and writes an equity curve.
      3. This bridge scores that curve against a SHIP gate.

    For today, ``weights`` is informational metadata only — recorded
    in the output payload for audit but not used to recompute returns.
    Future iteration may re-fit weights against equity curves.

    Output schema::

        {
          "data_source": "equity",
          "profile": str | None,
          "weights": {...} | None,
          "train_dates": (start, end),
          "test_dates":  (start, end),
          "n_train_daily_obs": int,    # daily returns, transparency
          "n_test_daily_obs":  int,    # daily returns, transparency
          "n_train_obs":       int,    # non-overlapping h-day blocks (gate input)
          "n_test_obs":        int,    # non-overlapping h-day blocks (gate input)
          "train_sharpe": float,
          "test_sharpe":  float,
          "overfit_gap":  float,         # train - test (Sharpe units)
          "train_total_return": float,
          "test_total_return":  float,
          "horizon_days": int,
          "train_fraction": float,
          "recommendation": "ship"|"hold"|"reject"|"insufficient_data",
          "recommendation_reason": str,
          "trained_at": iso8601,
          "ship_min_test_obs": int,      # echoed for transparency
          "ship_min_test_sharpe": float,
        }

    Promotion-gate contract: the verdict is "ship" iff
    ``n_test_obs >= SHIP_MIN_TEST_OBS`` AND
    ``test_sharpe > SHIP_MIN_TEST_SHARPE`` AND
    ``overfit_gap < SHIP_MAX_SHARPE_OVERFIT_GAP``. Until the engine has
    accumulated enough live trading-day history (~July 2026), the
    first condition fails and the verdict is "hold". Operationally
    this means: ship-by-time, not ship-by-code-flag.

    NOTE on the ``n_test_obs`` unit: at horizon ``h`` (e.g. 21d), one
    independent OOS observation is one *non-overlapping* h-day forward
    window. ``n_test_obs = floor(n_test_daily_obs / h)`` accordingly.
    With 64 engine days, 60/40 split, h=21d this is ~25/21 = 1 — well
    below the gate, so the verdict is HOLD by design. When the engine
    has rolled forward to ~250+ daily OOS observations at h=21d
    (n_test_obs ≥ 20 non-overlapping blocks), the SAME function call
    will start returning SHIP automatically.
    """
    if not 0.0 < train_fraction < 1.0:
        raise ValueError(
            "train_fraction must be in (0, 1), got %r" % (train_fraction,)
        )

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Empty input → insufficient_data verdict, no crash.
    if not equity_curve:
        return {
            "data_source": "equity",
            "profile": profile,
            "weights": dict(weights) if weights else None,
            "train_dates": (None, None),
            "test_dates": (None, None),
            "n_train_daily_obs": 0,
            "n_test_daily_obs": 0,
            "n_train_obs": 0,
            "n_test_obs": 0,
            "train_sharpe": 0.0,
            "test_sharpe": 0.0,
            "overfit_gap": 0.0,
            "train_total_return": 0.0,
            "test_total_return": 0.0,
            "horizon_days": int(horizon_days),
            "train_fraction": float(train_fraction),
            "recommendation": "insufficient_data",
            "recommendation_reason": "empty equity_curve",
            "trained_at": now_iso,
            "ship_min_test_obs": int(SHIP_MIN_TEST_OBS),
            "ship_min_test_sharpe": float(SHIP_MIN_TEST_SHARPE),
        }

    # Build daily-return series. Prefer caller-provided (matches engine
    # exactly); else derive from equity curve.
    if daily_returns:
        s = pd.Series(daily_returns, dtype=float)
        s.index = pd.to_datetime(s.index)
        rets = s.sort_index().dropna()
    else:
        rets = _equity_curve_to_returns(equity_curve)

    if rets.empty:
        return {
            "data_source": "equity",
            "profile": profile,
            "weights": dict(weights) if weights else None,
            "train_dates": (None, None),
            "test_dates": (None, None),
            "n_train_daily_obs": 0,
            "n_test_daily_obs": 0,
            "n_train_obs": 0,
            "n_test_obs": 0,
            "train_sharpe": 0.0,
            "test_sharpe": 0.0,
            "overfit_gap": 0.0,
            "train_total_return": 0.0,
            "test_total_return": 0.0,
            "horizon_days": int(horizon_days),
            "train_fraction": float(train_fraction),
            "recommendation": "insufficient_data",
            "recommendation_reason": (
                "no usable daily returns derived from equity_curve "
                "(need at least 2 consecutive non-NaN equity points)"
            ),
            "trained_at": now_iso,
            "ship_min_test_obs": int(SHIP_MIN_TEST_OBS),
            "ship_min_test_sharpe": float(SHIP_MIN_TEST_SHARPE),
        }

    n_total = len(rets)
    split = int(round(train_fraction * n_total))
    split = max(1, min(split, n_total - 1)) if n_total >= 2 else n_total
    train_rets = rets.iloc[:split]
    test_rets = rets.iloc[split:]

    train_sharpe = _annualised_sharpe(train_rets)
    test_sharpe = _annualised_sharpe(test_rets)
    overfit_gap = float(train_sharpe - test_sharpe)
    n_train_daily = int(len(train_rets))
    n_test_daily = int(len(test_rets))

    # The gate counts NON-OVERLAPPING h-day forward blocks, not daily
    # returns. At h=21d with 25 daily OOS obs, that's only 1 truly
    # independent block — well below SHIP_MIN_TEST_OBS=20. This is the
    # binding constraint until ~July 2026.
    h = max(1, int(horizon_days))
    n_train = n_train_daily // h
    n_test = n_test_daily // h

    def _cum(r: pd.Series) -> float:
        if r is None or r.empty:
            return 0.0
        return float((1.0 + r).prod() - 1.0)

    def _fmt(d) -> Optional[str]:
        if d is None:
            return None
        try:
            return d.strftime("%Y-%m-%d")
        except AttributeError:
            return str(d)

    rec, reason = _recommendation_equity(
        test_sharpe=test_sharpe,
        overfit_gap=overfit_gap,
        n_test_obs=n_test,
    )

    return {
        "data_source": "equity",
        "profile": profile,
        "weights": dict(weights) if weights else None,
        "train_dates": (
            _fmt(train_rets.index[0]) if n_train_daily else None,
            _fmt(train_rets.index[-1]) if n_train_daily else None,
        ),
        "test_dates": (
            _fmt(test_rets.index[0]) if n_test_daily else None,
            _fmt(test_rets.index[-1]) if n_test_daily else None,
        ),
        "n_train_daily_obs": n_train_daily,
        "n_test_daily_obs": n_test_daily,
        "n_train_obs": n_train,
        "n_test_obs": n_test,
        "train_sharpe": float(train_sharpe),
        "test_sharpe": float(test_sharpe),
        "overfit_gap": overfit_gap,
        "train_total_return": _cum(train_rets),
        "test_total_return": _cum(test_rets),
        "horizon_days": int(horizon_days),
        "train_fraction": float(train_fraction),
        "recommendation": rec,
        "recommendation_reason": reason,
        "trained_at": now_iso,
        "ship_min_test_obs": int(SHIP_MIN_TEST_OBS),
        "ship_min_test_sharpe": float(SHIP_MIN_TEST_SHARPE),
    }


__all__ = [
    "PILLAR_NAMES",
    "DEFAULT_PRIOR",
    "DEFAULT_RIDGE_ALPHA",
    "DEFAULT_HORIZON",
    "DEFAULT_TRAIN_FRACTION",
    "SHIP_MIN_TEST_IC",
    "SHIP_MAX_OVERFIT_GAP",
    "SHIP_MIN_TEST_OBS",
    "SHIP_MIN_TEST_SHARPE",
    "SHIP_MAX_SHARPE_OVERFIT_GAP",
    "tune_weights",
    "walk_forward_tune",
    "walk_forward_tune_equity",
]
