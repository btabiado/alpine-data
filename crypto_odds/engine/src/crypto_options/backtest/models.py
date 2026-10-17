"""Stage 3 baseline forecasters (brief s.6 "required for backtests", s.8).

Each model maps origin i to P(event) using only prices with info_time <= info_time[i] and labels matured
by then (panel.Series.matured_through). Nothing is tuned: every setting is predeclared in config.yaml
under `backtest`, so no hyper-parameter can leak from evaluation data.

Probability types:
  P (real-world, empirically estimated): climatology, lognormal_rv90, lognormal_ewma, lognormal_ewma_drift,
      fhs_ewma, garch_t
  Q-proxy (market-implied): dvol_lognormal_Q. Deribit DVOL as a single lognormal vol. It is a measurement of
      implied volatility, not the smile-consistent curve, and its miscalibration can reflect risk premia.

Log-price drift conventions: "zero drift" means a zero expected *price* change (martingale), i.e.
ln(S_T/S_t) ~ N(-V/2, V). "drift" variants estimate the mean daily log return from the trailing window.
"""
from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
from scipy.stats import norm

from ..pandas_compat import merge_asof_utc
from .panel import Series

P_MODELS = ["climatology", "lognormal_rv90", "lognormal_ewma", "lognormal_ewma_drift", "fhs_ewma", "garch_t"]
Q_MODELS = ["dvol_lognormal_Q"]
PROB_TYPE = {**{m: "P" for m in P_MODELS}, **{m: "Q-proxy" for m in Q_MODELS}}


def _warm(s: Series, cfg) -> np.ndarray:
    return (s.dates - s.dates[0]) >= pd.Timedelta(days=cfg["min_history_days"])


def ewma_var(r: np.ndarray, lam: float, init_n: int = 30) -> np.ndarray:
    """Daily EWMA variance known at each day's close (includes that day's return); NaN returns skip updates."""
    v = np.full(len(r), np.nan)
    first = np.flatnonzero(np.isfinite(r))
    if len(first) < init_n:
        return v
    seed_idx = first[:init_n]
    cur = float(np.mean(r[seed_idx] ** 2))
    for i in range(seed_idx[-1], len(r)):
        if np.isfinite(r[i]) and i > seed_idx[-1]:
            cur = lam * cur + (1 - lam) * r[i] ** 2
        v[i] = cur
    return v


def lognormal_prob(V, mu_total, p, up: bool):
    """P(ln(S_T/S_t) > ln(1+p)) (or <) with ln-return ~ N(mu_total, V)."""
    z = (np.log1p(p) - mu_total) / np.sqrt(V)
    return norm.sf(z) if up else norm.cdf(z)


def _past_frequency(flags: np.ndarray, valid: np.ndarray, J: np.ndarray, min_n: int):
    """Jeffreys-smoothed frequency of flags[j] over valid j <= J[i]."""
    k = np.concatenate([[0.0], np.cumsum(np.where(valid, flags, 0.0))])
    n = np.concatenate([[0.0], np.cumsum(valid.astype(float))])
    kk, nn = k[J + 1], n[J + 1]
    p = (kk + 0.5) / (nn + 1.0)
    return np.where(nn >= min_n, p, np.nan), nn


def _sum_expected_var(om: float, pers: float, nxt: float, h: int) -> float:
    """sum_{k=0}^{h-1} E[sigma^2_{t+1+k}] with E[s_{k+1}] = om + pers * E[s_k], s_0 = nxt (exact for any pers,
    including the integrated boundary pers = 1, where it equals h*nxt + om*h*(h-1)/2)."""
    if abs(1.0 - pers) < 1e-12:
        return h * nxt + om * h * (h - 1) / 2.0
    k = np.arange(h)
    lr = om / (1.0 - pers)
    return float(np.sum(lr + pers**k * (nxt - lr)))


def garch_hday_variance(r: np.ndarray, warm: np.ndarray, hs: list[int], refit_days: int,
                        log: list | None = None) -> dict:
    """Zero-mean GARCH(1,1)-t on daily % returns, refit on an expanding window every `refit_days` origins.

    Between refits the parameters are frozen and the variance recursion runs on returns observed so far.
    Returns {h: sum of expected daily variances over the next h days} in log-return units."""
    from arch import arch_model

    n = len(r)
    out = {h: np.full(n, np.nan) for h in hs}
    start = np.flatnonzero(warm)
    if not len(start):
        return out
    x = r * 100.0
    refits = range(start[0], n, refit_days)
    for R in refits:
        hist = x[: R + 1]
        hist = hist[np.isfinite(hist)]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            res = arch_model(hist, mean="Zero", vol="GARCH", p=1, q=1, dist="t").fit(disp="off")
        om, al, be = (float(res.params[k]) for k in ("omega", "alpha[1]", "beta[1]"))
        pers = al + be
        if log is not None:
            log.append({"refit_idx": int(R), "omega": om, "alpha": al, "beta": be, "persistence": pers,
                        "integrated": bool(pers > 1 - 1e-6)})
        sig2 = float(res.conditional_volatility[-1] ** 2)       # variance of day R's return
        last_eps2 = float(hist[-1] ** 2)
        for i in range(R, min(R + refit_days, n)):
            if i > R:
                e2 = x[i] ** 2 if np.isfinite(x[i]) else sig2
                sig2 = om + al * last_eps2 + be * sig2          # variance for day i, given info through i-1
                last_eps2 = e2
            nxt = om + al * last_eps2 + be * sig2               # variance for day i+1, info through i
            for h in hs:
                out[h][i] = _sum_expected_var(om, pers, nxt, h) / 1e4
    return out


def forecast(s: Series, hs: list[int], targets: list[float], cfg: dict, dvol: pd.DataFrame | None = None) -> pd.DataFrame:
    bt = cfg
    r = s.log_returns()
    warm = _warm(s, bt)
    n = len(s)
    v_ewma = ewma_var(r, bt["ewma_lambda"])
    rv = pd.Series(r).rolling(bt["rv_window"], min_periods=int(0.8 * bt["rv_window"])).var().to_numpy()
    mu = pd.Series(r).rolling(bt["drift_window"], min_periods=int(0.8 * bt["drift_window"])).mean().to_numpy()
    garch_log: list = []
    garch = garch_hday_variance(r, warm, hs, bt["garch_refit_days"], garch_log)
    dv = None
    if dvol is not None and len(dvol):
        o = pd.DataFrame({"t": s.info_time, "i": np.arange(n)})
        d = dvol.sort_values("available_time")[["available_time", "dvol"]].rename(columns={"available_time": "t"})
        m = merge_asof_utc(o, d, on="t", direction="backward", tolerance=pd.Timedelta(days=3))
        dv = m.sort_values("i")["dvol"].to_numpy() / 100.0

    rows = []
    for h in hs:
        J = s.matured_through(h)
        jo = s.outcome_index(h)
        has = jo >= 0
        strict = has.copy()
        if s.excluded_outcome_from is not None:
            strict &= ~(s.dates + pd.Timedelta(days=h) >= s.excluded_outcome_from)
        R_h = np.where(has, np.log(s.price[np.maximum(jo, 0)] / s.price), np.nan)
        sd = np.sqrt(v_ewma)
        z = R_h / (sd * np.sqrt(h))
        zvalid = strict & np.isfinite(z)
        for p in targets:
            up = p > 0
            lp = np.log1p(p)
            y_hist = np.where(up, R_h > lp, R_h < lp).astype(float)
            preds = {}
            clim, _ = _past_frequency(y_hist, strict, J, bt["min_history_days"])
            preds["climatology"] = clim
            preds["lognormal_rv90"] = lognormal_prob(rv * h, -0.5 * rv * h, p, up)
            preds["lognormal_ewma"] = lognormal_prob(v_ewma * h, -0.5 * v_ewma * h, p, up)
            preds["lognormal_ewma_drift"] = lognormal_prob(v_ewma * h, mu * h, p, up)
            # filtered historical simulation: past standardized h-day returns, rescaled by today's EWMA vol
            thr = lp / (sd * np.sqrt(h))
            fhs = np.full(n, np.nan)
            zz = np.where(zvalid, z, np.nan)
            for a in range(0, n, 512):
                b = min(a + 512, n)
                Ji = J[a:b]
                mask = (np.arange(n)[None, :] <= Ji[:, None]) & zvalid[None, :]
                hit = (zz[None, :] > thr[a:b, None]) if up else (zz[None, :] < thr[a:b, None])
                k = np.sum(hit & mask, axis=1)
                cnt = np.sum(mask, axis=1)
                fhs[a:b] = np.where(cnt >= bt["min_history_days"], (k + 0.5) / (cnt + 1.0), np.nan)
            preds["fhs_ewma"] = fhs
            Vg = garch[h]
            preds["garch_t"] = lognormal_prob(Vg, -0.5 * Vg, p, up)
            if dv is not None:
                Vq = dv**2 * h / 365.0
                preds["dvol_lognormal_Q"] = lognormal_prob(Vq, -0.5 * Vq, p, up)
            for m, pr in preds.items():
                pr = np.where(warm, pr, np.nan)
                rows.append(pd.DataFrame({"origin_idx": np.arange(n), "h": h, "target": p, "model": m, "p": pr}))
    out = pd.concat(rows, ignore_index=True)
    out.attrs["garch_refits"] = [{**g, "refit_date": str(s.dates[g["refit_idx"]].date())} for g in garch_log]
    return out
