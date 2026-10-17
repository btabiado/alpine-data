"""Stage 4 features, all known at the origin's information time (brief s.7).

Group A (price history of the cohort series itself):
  log returns over 1, 7, 30, 90, 180, 365 days; realized vol over 7, 30, 90 days; EWMA vol; vol ratio 30/90;
  distance from the trailing 365-day high and low; share of up days over 30 days.
Group D (DVOL, BTC/ETH only): DVOL level, DVOL minus 30-day realized vol (annualized), 30-day DVOL change,
  DVOL staleness in days, missing flag.
Row-level target features are added per (origin, target): c = ln(1+p) and c / (EWMA vol * sqrt(h)).
Missing values are left as NaN and handled inside each model's pipeline (median imputation learned on the
training window only; XGBoost handles NaN natively).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .models import ewma_var
from .panel import Series

GROUP_A = ["ret_1", "ret_7", "ret_30", "ret_90", "ret_180", "ret_365", "rv_7", "rv_30", "rv_90", "ewma_vol",
           "vol_ratio_30_90", "dist_high_365", "dist_low_365", "up_share_30"]
GROUP_D = ["dvol", "dvol_minus_rv30", "dvol_chg_30", "dvol_age_days", "dvol_missing"]
ROW = ["c", "c_std"]


def origin_features(s: Series, ewma_lambda: float, dvol: pd.DataFrame | None = None) -> pd.DataFrame:
    n = len(s)
    lp = pd.Series(np.log(s.price))
    r = pd.Series(s.log_returns())
    by_date = pd.Series(np.log(s.price), index=s.dates)
    f = pd.DataFrame(index=np.arange(n))
    for k in (1, 7, 30, 90, 180, 365):  # exact calendar lag; NaN if that day is missing
        lag = by_date.reindex(s.dates - pd.Timedelta(days=k)).to_numpy()
        f[f"ret_{k}"] = lp.to_numpy() - lag
    for w in (7, 30, 90):
        f[f"rv_{w}"] = r.rolling(w, min_periods=int(0.8 * w)).std().to_numpy() * np.sqrt(365)
    f["ewma_vol"] = np.sqrt(ewma_var(r.to_numpy(), ewma_lambda)) * np.sqrt(365)
    f["vol_ratio_30_90"] = f["rv_30"] / f["rv_90"]
    f["dist_high_365"] = lp - lp.rolling(365, min_periods=300).max()
    f["dist_low_365"] = lp - lp.rolling(365, min_periods=300).min()
    f["up_share_30"] = (r > 0).astype(float).where(r.notna()).rolling(30, min_periods=24).mean()
    if dvol is not None and len(dvol):
        o = pd.DataFrame({"t": s.info_time, "i": np.arange(n)})
        d = dvol.sort_values("available_time")[["available_time", "dvol"]].rename(columns={"available_time": "t"})
        d["t_obs"] = d["t"]
        m = pd.merge_asof(o.sort_values("t"), d, on="t", direction="backward").sort_values("i")
        dv = m["dvol"].to_numpy() / 100.0
        age = (m["t"] - m["t_obs"]).dt.total_seconds().to_numpy() / 86400
        stale = ~(age <= 3)
        dv = np.where(stale, np.nan, dv)
        f["dvol"] = dv
        f["dvol_minus_rv30"] = dv - f["rv_30"]
        dser = pd.Series(dv, index=s.dates)
        f["dvol_chg_30"] = dv - dser.reindex(s.dates - pd.Timedelta(days=30)).to_numpy()
        f["dvol_age_days"] = np.where(np.isfinite(age), age, np.nan)
        f["dvol_missing"] = stale.astype(float)
    return f


def row_features(f: pd.DataFrame, origin_idx: np.ndarray, target: np.ndarray, h: int) -> pd.DataFrame:
    x = f.iloc[origin_idx].reset_index(drop=True)
    c = np.log1p(target)
    x["c"] = c
    x["c_std"] = c / (x["ewma_vol"].to_numpy() / np.sqrt(365) * np.sqrt(h))
    return x
