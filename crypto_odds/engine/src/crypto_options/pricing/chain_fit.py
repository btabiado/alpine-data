"""Constrained full-chain fit of normalized call prices and the implied terminal distribution.

Model (brief s.6, "Full-chain construction"). Work in forward-normalized units: forward = 1,
strike k = K / F, undiscounted call price c(k) = C / (D F). Then

    Q(S_T > K) = -dc/dk (right derivative)      f_Q(k) = d2c/dk2   (density of S_T / F)

c is piecewise linear on a node set x_0 < ... < x_n that contains every quoted strike, parameterized by
    c0        value at x_0                          bounded: max(1 - x_0, 0) <= c0 <= 1
    s0        slope on the first segment            bounded: -1 <= s0 <= 0
    a_1..     slope increments at interior nodes    bounded: a_j >= 0  (convexity)
so the call function is convex and slope-bounded by construction. Linear constraints add
    last slope <= 0, c(x_n) >= 0                    (with the bounds above, intrinsic <= c <= 1 everywhere:
                                                      c - intrinsic attains its minimum at an endpoint)
    put(x_0) <= x_0 * Q(S_T <= x_0)                 (a positive put price needs mass below x_0)
    convex-extension limits from ask-only quotes    (an ask is an upper bound on price)

Fit: exact convex QP (Clarabel) minimizing squared quote errors in half-spread units plus a density
roughness penalty; the penalty is the smoothest one whose fit passes through every bid/ask.
Bounds: two LPs (HiGHS) over every curve with nodes at all quoted strikes and the target that
satisfies the shape constraints and every bid/ask. That is the model-free range of the right derivative
at K given the quotes, not a confidence interval. Mass outside [x_0, x_n] is reported, never renormalized.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import clarabel
import numpy as np
import scipy.sparse as sp
from scipy.optimize import linprog

from . import black76


@dataclass
class ChainFit:
    status: str                       # usable | degraded | unavailable
    reasons: list = field(default_factory=list)
    T: float = np.nan
    grid: np.ndarray | None = None    # nodes, normalized strikes
    c: np.ndarray | None = None       # fitted call prices at nodes
    slopes: np.ndarray | None = None  # per segment
    lam: float = np.nan
    rms_spread_units: float = np.nan
    max_spread_units: float = np.nan
    frac_outside_quotes: float = np.nan
    tail_below: float = np.nan        # Q(S_T <= x_0)
    tail_above: float = np.nan        # Q(S_T > x_n)
    n_quotes: int = 0
    k_obs: np.ndarray | None = None
    lp_feasible: bool | None = None
    alt_fits: dict = field(default_factory=dict)  # lambda -> slopes on `grid`, smoothing sensitivity
    _quotes: tuple | None = None      # (k, bid, ask, ask_only_k, ask_only_ask) for the bounds LP

    def survival(self, k):
        """Point estimate of Q(S_T > k*F): segment slopes interpolated between segment midpoints."""
        return _interp_survival(self.grid, self.slopes, k)

    def density(self):
        """(node, density) at interior nodes, density of S_T / F."""
        h = np.diff(self.grid)
        return self.grid[1:-1], np.diff(self.slopes) / (0.5 * (h[:-1] + h[1:]))

    def fitted_iv(self):
        ivs = []
        for x, cx in zip(self.grid[1:-1], self.c[1:-1]):
            ivs.append(black76.implied_vol(cx, 1.0, x, self.T, 1.0, True) if x >= 1
                       else black76.implied_vol(cx - (1 - x), 1.0, x, self.T, 1.0, False))
        return self.grid[1:-1], np.array(ivs)


def _interp_survival(grid, slopes, k):
    mids = 0.5 * (grid[:-1] + grid[1:])
    return np.interp(k, mids, -slopes, left=np.nan, right=np.nan)


# ---- linear structure -------------------------------------------------------------------

def _design(grid):
    """c_nodes = M @ theta and slopes = S @ theta for theta = [c0, s0, a_1..a_{n-1}]."""
    n = len(grid) - 1
    M = np.zeros((n + 1, n + 1))
    M[:, 0] = 1.0
    M[:, 1] = grid - grid[0]
    M[:, 2:] = np.maximum(grid[:, None] - grid[None, 1:n], 0.0)
    S = np.zeros((n, n + 1))
    S[:, 1] = 1.0
    S[:, 2:] = np.tril(np.ones((n, n - 1)), -1)  # slope_j = s0 + a_1 + ... + a_j
    return M, S


def _constraints(grid, M, S, ext):
    """Rows G, g with G @ theta >= g, plus variable bounds (lo, hi)."""
    n = len(grid) - 1
    x0, xn = grid[0], grid[-1]
    rows, rhs = [-S[-1], M[-1]], [0.0, 0.0]          # last slope <= 0 ; c(x_n) >= 0
    e = np.zeros(n + 1)
    e[0], e[1] = -1.0, x0                            # c0 - x0*s0 <= 1  <=>  put(x0) <= x0 (1 + s0)
    rows.append(e); rhs.append(-1.0)
    for kq, ask in ext:                              # convex extension beyond the node range
        if kq > xn:    # c(kq) >= c(xn) + s_last (kq - xn), and c(kq) <= ask
            rows.append(-(M[-1] + (kq - xn) * S[-1])); rhs.append(-ask)
        elif kq < x0:  # c(kq) >= c0 - s0 (x0 - kq), and c(kq) <= ask
            r = -M[0].copy(); r[1] += (x0 - kq); rows.append(r); rhs.append(-ask)
    lo = np.r_[max(1 - x0, 0.0), -1.0, np.zeros(n - 1)]
    hi = np.r_[1.0, 0.0, np.full(n - 1, np.inf)]
    return np.array(rows), np.array(rhs), lo, hi


def _node_grid(k_quotes, extra, n_fill):
    """Uniform fill nodes plus every quoted strike; fill nodes too close to a strike are dropped."""
    k_all = np.unique(np.concatenate([k_quotes, extra]))
    lo, hi = k_all[0], k_all[-1]
    fill = np.linspace(lo, hi, n_fill + 1)
    h = (hi - lo) / n_fill
    keep = np.min(np.abs(fill[:, None] - k_all[None, :]), axis=1) > 0.25 * h
    return np.unique(np.concatenate([k_all, fill[keep]]))


def _roughness(grid):
    """Rows R: R @ theta = density first differences / spacing, weighted by sqrt(spacing)."""
    n = len(grid) - 1
    h = np.diff(grid)
    w = 0.5 * (h[:-1] + h[1:])                       # node j (1..n-1): density = a_j / w[j-1]
    R = np.zeros((max(n - 2, 0), n + 1))
    for j in range(n - 2):
        dx = grid[j + 2] - grid[j + 1]
        R[j, 2 + j] = -1.0 / (w[j] * dx)
        R[j, 3 + j] = 1.0 / (w[j + 1] * dx)
        R[j] *= np.sqrt(dx)
    return R


def _qp(X, y, G, g, lo, hi):
    """min ||X theta - y||^2 s.t. G theta >= g, lo <= theta <= hi. Exact convex QP; None unless solved."""
    p = X.shape[1]
    scale = max(len(y), 1)
    P = sp.csc_matrix(np.triu(2.0 * X.T @ X / scale))
    q = -2.0 * X.T @ y / scale
    fin = np.isfinite(hi)
    A = sp.csc_matrix(np.vstack([-G, -np.eye(p), np.eye(p)[fin]]))
    b = np.concatenate([-g, -lo, hi[fin]])
    st = clarabel.DefaultSettings()
    st.verbose = False
    st.max_iter = 300
    sol = clarabel.DefaultSolver(P, q, A, b, [clarabel.NonnegativeConeT(A.shape[0])], st).solve()
    if str(sol.status) != "Solved":
        return None
    theta = np.array(sol.x)
    if np.min(G @ theta - g) < -1e-7 or np.any(theta < lo - 1e-7) or np.any(theta > hi + 1e-7):
        return None
    return theta


# ---- fit ---------------------------------------------------------------------------------

def fit_chain(k, bid, ask, T, cfg_fit: dict, cfg_quality: dict, tick_norm=None,
              ask_only_k=None, ask_only_ask=None) -> ChainFit:
    """Fit one expiry. k, bid, ask: accepted OTM quotes as normalized call prices.
    ask_only_k / ask_only_ask: one-sided OTM quotes (call-price units), used only as upper limits."""
    k, bid, ask = (np.asarray(v, dtype=float) for v in (k, bid, ask))
    order = np.argsort(k)
    k, bid, ask = k[order], bid[order], ask[order]
    tick = np.asarray(tick_norm, dtype=float)[order] if tick_norm is not None else np.full(len(k), 2e-6)
    ak = np.asarray(ask_only_k if ask_only_k is not None else [], dtype=float)
    aa = np.asarray(ask_only_ask if ask_only_ask is not None else [], dtype=float)
    fit = ChainFit(status="unavailable", T=T, n_quotes=len(k), k_obs=k)

    n_below, n_above = int(np.sum(k < 1)), int(np.sum(k >= 1))
    if len(np.unique(k)) < cfg_quality["min_strikes"]:
        fit.reasons.append(f"only {len(np.unique(k))} accepted strikes (< {cfg_quality['min_strikes']})")
    if min(n_below, n_above) < cfg_quality["min_strikes_each_side"]:
        fit.reasons.append(f"{n_below} accepted strikes below / {n_above} above forward "
                           f"(< {cfg_quality['min_strikes_each_side']} each side)")
    if fit.reasons:
        return fit

    mid = 0.5 * (bid + ask)
    half = np.maximum(0.5 * (ask - bid), 0.5 * tick)
    inside = (ak > k[0]) & (ak < k[-1])
    grid = _node_grid(k, ak[inside], cfg_fit["grid_points"])
    M, S = _design(grid)
    A = M[np.searchsorted(grid, k)]
    G, g, lo, hi = _constraints(grid, M, S, list(zip(ak[~inside], aa[~inside])))
    if inside.any():  # c(k) <= ask at interior ask-only strikes
        G = np.vstack([G, -M[np.searchsorted(grid, ak[inside])]])
        g = np.concatenate([g, -aa[inside]])
    R = _roughness(grid)
    Aw, yw = A / half[:, None], mid / half
    tol = 1.0 + cfg_fit.get("quote_tolerance", 0.02)

    def solve(lam):
        return _qp(np.vstack([Aw, np.sqrt(lam) * R]), np.concatenate([yw, np.zeros(len(R))]), G, g, lo, hi)

    fits = {}
    for lam in sorted(cfg_fit["lambda_grid"]):
        theta = solve(lam)
        if theta is None:
            continue
        resid = (A @ theta - mid) / half
        fits[lam] = (theta, float(np.sqrt(np.mean(resid**2))), resid)
    fit.lp_feasible = _feasible(A, bid, ask, G, g, lo, hi)
    if not fits:
        fit.status = "degraded"
        fit.reasons.append("constrained QP did not solve at any smoothing level")
        return fit

    within_all = [lam for lam, f in fits.items() if np.max(np.abs(f[2])) <= tol]
    within_rms = [lam for lam, f in fits.items() if f[1] <= cfg_fit["target_rms_spread_units"]]
    chosen = max(within_all) if within_all else (max(within_rms) if within_rms else min(fits))
    theta, rms, resid = fits[chosen]
    fit.grid, fit.lam = grid, chosen
    fit.c, fit.slopes = M @ theta, S @ theta
    fit.rms_spread_units = rms
    fit.max_spread_units = float(np.max(np.abs(resid)))
    fit.frac_outside_quotes = float(np.mean(np.abs(resid) > tol))
    put0 = fit.c[0] - (1 - grid[0])
    fit.tail_below = float(max(1 + fit.slopes[0], put0 / grid[0]))
    fit.tail_above = float(-fit.slopes[-1])
    fac = cfg_fit["sensitivity_lambda_factor"]
    for lam in (chosen * fac, chosen / fac):
        th = solve(lam)
        if th is not None:
            fit.alt_fits[lam] = S @ th
    fit._quotes = (k, bid, ask, ak, aa)

    fit.status = "usable"
    if not fit.lp_feasible:
        fit.status = "degraded"
        fit.reasons.append("no convex, decreasing call curve passes through every accepted bid/ask")
    elif not within_all:
        fit.status = "degraded"
        fit.reasons.append(f"smooth fit leaves {fit.frac_outside_quotes:.0%} of quotes outside bid/ask "
                           f"(RMS {rms:.2f} half-spreads)")
    return fit


def _feasible(A, bid, ask, G, g, lo, hi):
    res = linprog(np.zeros(A.shape[1]), A_ub=np.vstack([A, -A, -G]), b_ub=np.concatenate([ask, -bid, -g]),
                  bounds=list(zip(lo, np.where(np.isinf(hi), None, hi))), method="highs")
    return res.status == 0


def quote_feasible_bounds(fit: ChainFit, k_target: float):
    """Model-free (lower, upper) of Q(S_T > K) = -c'(K+) over every shape-valid curve within all quotes.

    Nodes are the quoted strikes plus the target and a point just to its right, so every extremal
    piecewise-linear curve is representable and the range is not narrowed by any fitting grid."""
    if fit._quotes is None or not fit.lp_feasible:
        return np.nan, np.nan
    k, bid, ask, ak, aa = fit._quotes
    if not (k[0] < k_target < k[-1]):
        return np.nan, np.nan
    inside = (ak > k[0]) & (ak < k[-1])
    # nodes at the target and just right of it: the segment [K, K+eps] carries the right derivative and
    # may take any slope convexity allows (a single node at K would force a chord to the next strike)
    nodes = np.unique(np.concatenate([k, ak[inside]]))
    nxt = nodes[nodes > k_target]
    eps = min(1e-7, 0.5 * (nxt[0] - k_target)) if len(nxt) else 1e-7
    grid = np.unique(np.concatenate([nodes, [k_target, k_target + eps]]))
    M, S = _design(grid)
    G, g, lo, hi = _constraints(grid, M, S, list(zip(ak[~inside], aa[~inside])))
    if inside.any():
        G = np.vstack([G, -M[np.searchsorted(grid, ak[inside])]])
        g = np.concatenate([g, -aa[inside]])
    A = M[np.searchsorted(grid, k)]
    A_ub = np.vstack([A, -A, -G])
    b_ub = np.concatenate([ask, -bid, -g])
    bnds = list(zip(lo, np.where(np.isinf(hi), None, hi)))
    row = S[int(np.searchsorted(grid, k_target))]   # segment starting at the target: right derivative
    r_min = linprog(row, A_ub=A_ub, b_ub=b_ub, bounds=bnds, method="highs")    # min slope -> max Q
    r_max = linprog(-row, A_ub=A_ub, b_ub=b_ub, bounds=bnds, method="highs")   # max slope -> min Q
    if r_min.status != 0 or r_max.status != 0:
        return np.nan, np.nan
    return float(np.clip(r_max.fun, 0, 1)), float(np.clip(-r_min.fun, 0, 1))


def atm_iv(k, mid_call, T):
    """Constant-ATM benchmark vol: implied vols at the two strikes bracketing the forward, interpolated."""
    k, mid_call = np.asarray(k), np.asarray(mid_call)
    below, above = np.flatnonzero(k < 1), np.flatnonzero(k >= 1)
    if not len(below) or not len(above):
        return np.nan
    pts = []
    for i in (below[-1], above[0]):
        if k[i] >= 1:
            pts.append((k[i], black76.implied_vol(mid_call[i], 1.0, k[i], T, 1.0, True)))
        else:
            pts.append((k[i], black76.implied_vol(mid_call[i] - (1 - k[i]), 1.0, k[i], T, 1.0, False)))
    (k1, v1), (k2, v2) = pts
    return float(np.interp(1.0, [k1, k2], [v1, v2]))


def target_support(fit: ChainFit, k_target: float, cfg_quality: dict):
    """Is the target inside reliable strike support? Returns (ok, reason)."""
    if fit.status == "unavailable" or fit.grid is None:
        return False, "curve unavailable"
    k = np.unique(fit.k_obs)
    if not (k[0] < k_target < k[-1]):
        return False, f"outside accepted strike range ({k[0]:.3f}-{k[-1]:.3f} x F)"
    below, above = k[k <= k_target], k[k >= k_target]
    need = cfg_quality["min_strikes_each_side_of_target"]
    if len(below) < need or len(above) < need:
        return False, f"fewer than {need} accepted strikes on each side of target"
    gap = above[0] - below[-1]
    if gap > cfg_quality["max_target_gap_frac"]:
        return False, f"bracketing accepted strikes {gap:.0%} of forward apart"
    return True, ""
