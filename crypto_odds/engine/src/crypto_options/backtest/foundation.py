"""Stage 5: turn Chronos-Bolt quantile forecasts of h-day log returns into event probabilities (brief s.7).

Chronos-Bolt returns quantiles at 10%, 20%, ..., 90%. Inside [q10, q90] the CDF is interpolated linearly between
quantiles. Outside it there is no model information, so a normal-shaped tail is assumed: centred on q50, with sigma
implied by the q10-q90 spread, and rescaled to join the 10% / 90% points continuously. Rows whose threshold falls
outside [q10, q90] are flagged (`in_band = False`) and are also reported separately, because their probability rests
on that tail assumption rather than on the model. Far-tail probabilities are never built from central quantiles alone
without this disclosure (brief s.7).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

QCOLS = ["q10", "q20", "q30", "q40", "q50", "q60", "q70", "q80", "q90"]
LEVELS = np.array([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9])
Z90 = norm.ppf(0.9)


def cdf_from_quantiles(q: np.ndarray, c: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """q: (n, 9) quantiles of the log return; c: (n,) thresholds. Returns (F(c), in_band)."""
    q = np.maximum.accumulate(np.asarray(q, float), axis=1)      # enforce non-decreasing quantiles
    c = np.asarray(c, float)
    lo, hi, mid = q[:, 0], q[:, -1], q[:, 4]
    sigma = np.maximum((hi - lo) / (2 * Z90), 1e-9)
    F = np.empty(len(c))
    inside = (c >= lo) & (c <= hi)
    for i in np.flatnonzero(inside):
        F[i] = np.interp(c[i], q[i], LEVELS)
    below = c < lo
    z = (c - mid) / sigma
    F[below] = 0.1 * norm.cdf(z[below]) / norm.cdf((lo[below] - mid[below]) / sigma[below])
    above = c > hi
    F[above] = 1 - 0.1 * norm.sf(z[above]) / norm.sf((hi[above] - mid[above]) / sigma[above])
    return np.clip(F, 0.0, 1.0), inside


def chronos_dir(cfg: dict) -> Path:
    return Path(cfg["paths"]["history"]).parent / "chronos"


def load_chronos(cfg: dict, cohort: str, asset: str, variant: str = "") -> pd.DataFrame | None:
    p = chronos_dir(cfg) / f"{cohort}_{asset}{'_' + variant if variant else ''}.parquet"
    return pd.read_parquet(p) if p.exists() else None


def check_final_ready(cfg: dict, final: bool):
    """Before the one-time holdout run, Chronos forecasts must have been regenerated with --final, or Chronos would
    silently drop out of the only contamination-free test."""
    man = chronos_dir(cfg) / "manifest.json"
    if not man.exists():
        if any(chronos_dir(cfg).glob("*.parquet")):
            raise RuntimeError("Chronos forecasts present without manifest.json; regenerate with scripts/chronos_forecast.py")
        return
    import json
    m = json.loads(man.read_text())
    if final and not m.get("final"):
        raise RuntimeError("Chronos forecasts were generated for development origins only. Run "
                           "`envs/chronos/bin/python scripts/chronos_forecast.py --final` before opening the holdout.")
    if not final and m.get("final"):
        raise RuntimeError("Chronos forecasts include holdout origins (--final); regenerate without --final for "
                           "development runs.")


def _aligned(events: pd.DataFrame, chronos: pd.DataFrame) -> pd.DataFrame:
    m = events.merge(chronos, on=["origin_idx", "h"], how="inner", suffixes=("", "_ch"))
    if "origin_date_ch" in m and len(m):
        bad = pd.to_datetime(m["origin_date"], utc=True) != pd.to_datetime(m["origin_date_ch"], utc=True)
        if bad.any():
            raise RuntimeError(f"Chronos forecasts misaligned with the panel on {int(bad.sum())} rows")
    return m


def chronos_vol_probs(vol: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    """Volatility representation: Chronos's h-day variance in a zero-drift lognormal (same mapping as the baselines)."""
    from .models import lognormal_prob
    m = _aligned(events[["origin_idx", "origin_date", "h", "target", "event"]], vol)
    if m.empty:
        return pd.DataFrame(columns=["origin_idx", "h", "target", "chronos_vol"])
    up = (m["event"] == "S_T > K").to_numpy()
    V = m["var_h"].to_numpy()
    p_up = lognormal_prob(V, -0.5 * V, m["target"].to_numpy(), True)
    p_dn = lognormal_prob(V, -0.5 * V, m["target"].to_numpy(), False)
    return pd.DataFrame({"origin_idx": m["origin_idx"], "h": m["h"], "target": m["target"],
                         "chronos_vol": np.where(up, p_up, p_dn)})


def quantile_scores(chronos: pd.DataFrame, realized: pd.DataFrame) -> pd.DataFrame:
    """Pinball loss over the 9 quantiles and 10-90% interval coverage/width for Chronos (price representation) and for
    a zero-drift lognormal with EWMA variance (realized: origin_idx, h, R = realized log return, V = EWMA h-day var)."""
    m = chronos.merge(realized, on=["origin_idx", "h"], how="inner").dropna(subset=["R", "V"])
    if m.empty:
        return pd.DataFrame()
    Q = np.maximum.accumulate(m[QCOLS].to_numpy(), axis=1)
    R = m["R"].to_numpy()[:, None]
    B = (-0.5 * m["V"].to_numpy())[:, None] + np.sqrt(m["V"].to_numpy())[:, None] * norm.ppf(LEVELS)[None, :]

    def pin(q):
        d = R - q
        return np.mean(np.maximum(LEVELS * d, (LEVELS - 1) * d), axis=1)
    return pd.DataFrame({"origin_idx": m["origin_idx"], "origin_date": m["origin_date"], "h": m["h"],
                         "pinball_chronos": pin(Q), "pinball_lognormal_ewma": pin(B),
                         "cover80_chronos": ((R[:, 0] >= Q[:, 0]) & (R[:, 0] <= Q[:, -1])).astype(float),
                         "cover80_lognormal_ewma": ((R[:, 0] >= B[:, 0]) & (R[:, 0] <= B[:, -1])).astype(float),
                         "width80_chronos": Q[:, -1] - Q[:, 0], "width80_lognormal_ewma": B[:, -1] - B[:, 0],
                         "median_bias_chronos": Q[:, 4] - R[:, 0]})


def chronos_probs(chronos: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    """events: rows with origin_idx, origin_date, h, target, event. Returns origin_idx, h, target, chronos_bolt,
    chronos_in_band."""
    m = _aligned(events[["origin_idx", "origin_date", "h", "target", "event"]], chronos)
    if m.empty:
        return pd.DataFrame(columns=["origin_idx", "h", "target", "chronos_bolt", "chronos_in_band"])
    F, inside = cdf_from_quantiles(m[QCOLS].to_numpy(), np.log1p(m["target"].to_numpy()))
    p = np.where(m["event"] == "S_T > K", 1 - F, F)
    return pd.DataFrame({"origin_idx": m["origin_idx"], "h": m["h"], "target": m["target"],
                         "chronos_bolt": p, "chronos_in_band": inside})
