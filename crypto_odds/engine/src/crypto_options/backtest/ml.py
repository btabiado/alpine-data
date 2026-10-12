"""Stage 4 ML challengers: walk-forward, nested chronological tuning, past-only calibration (brief s.6-8).

One classifier per (asset, cohort, horizon, side), pooled over that side's targets, with the target entering as
features (c = ln(1+p), c in vol units). "up" side: event S_T > S_t(1+p), p > 0. "down" side: S_T < S_t(1+p), p < 0.

At each refit date R (every `refit_days` origins-days):
  pool       = rows whose outcome date <= R (labels matured by R; nothing later is visible)
  gate       = >= min_distinct_outcome_dates distinct outcome dates, and >= min_class_dates distinct dates with
               an event and with a non-event (brief s.8); otherwise no forecast (coverage gap, never 0%)
  tuning     = every `retune_days`: <= `tuning_trials` predeclared random trials scored by Brier on an inner
               validation slice (last `inner_val_fraction` of the pool), whose training part is purged so its
               labels mature before the slice starts
  calibrate  = all models: Platt scaling with slope restricted to [0, 1] (shrink-only), fitted on the inner-validation
               predictions of an inner-train model, and only if that slice has >= cal_min_class_dates distinct event and
               non-event dates
  target gate = each target needs >= min_class_dates distinct matured event and non-event dates, else no forecast
  final      = refit on the full pool with the chosen parameters; predict origins in [R, next R)
  monotone   = probabilities rearranged across targets per origin, so a farther target is never more likely
               (part of the evaluated pipeline)
All preprocessing (imputation, scaling) is fitted inside the training data via sklearn Pipelines.
"""
from __future__ import annotations

import warnings
import zlib

import numpy as np
import pandas as pd

from .features import GROUP_A, GROUP_D, ROW

SPACES = {
    "logit": {"C": [1e-4, 3e-4, 1e-3, 3e-3, 0.01, 0.03, 0.1, 0.3, 1.0]},
    "rf": {"max_depth": [3, 5, 8, None], "min_samples_leaf": [20, 50, 100, 200], "max_features": [0.3, 0.6, "sqrt"]},
    "xgb": {"n_estimators": [100, 200, 400], "max_depth": [2, 3, 4], "learning_rate": [0.03, 0.1],
            "min_child_weight": [5, 20, 50], "subsample": [0.7, 1.0], "colsample_bytree": [0.7, 1.0],
            "reg_lambda": [1.0, 10.0]},
}
ML_MODELS = {"logit_A": ("logit", "A"), "rf_A": ("rf", "A"), "xgb_A": ("xgb", "A"), "xgb_AD": ("xgb", "AD")}


def feature_cols(fs: str) -> list[str]:
    return GROUP_A + ROW + (GROUP_D if fs == "AD" else [])


def _make(kind: str, params: dict, cols: list[str], side: str, seed: int):
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    if kind == "logit":
        return make_pipeline(SimpleImputer(strategy="median"), StandardScaler(),
                             LogisticRegression(C=params["C"], max_iter=2000))
    if kind == "rf":
        from sklearn.ensemble import RandomForestClassifier
        return make_pipeline(SimpleImputer(strategy="median"),
                             RandomForestClassifier(n_estimators=150, max_samples=0.5, random_state=seed, n_jobs=1,
                                                    **params))
    import xgboost
    sign = -1 if side == "up" else 1   # up: P(R > c) falls as c rises; down: P(R < c) rises as c rises
    mono = tuple(sign if c in ("c", "c_std") else 0 for c in cols)
    return xgboost.XGBClassifier(objective="binary:logistic", tree_method="hist", n_jobs=1, random_state=seed,
                                 monotone_constraints=mono, eval_metric="logloss", **params)


def _fit_predict(kind, params, cols, side, seed, Xtr, ytr, Xte):
    m = _make(kind, params, cols, side, seed)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        m.fit(Xtr[cols], ytr)
        return m.predict_proba(Xte[cols])[:, 1]


def _logit(p):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


def shrink_calibrator(p_val, y_val):
    """Platt scaling restricted to slopes in [0, 1]: it can pull forecasts toward the slice's base rate (b < 1) but
    never sharpen them (b > 1) or reverse their ranking (b < 0). Exact constrained maximum likelihood."""
    from scipy.optimize import minimize
    z, y = _logit(np.asarray(p_val, float)), np.asarray(y_val, float)
    if len(np.unique(y)) < 2:
        return None

    def nll(v):
        a, b = v
        t = a + b * z
        return float(np.sum(np.logaddexp(0.0, t) - y * t))

    base = float(np.clip(y.mean(), 1e-4, 1 - 1e-4))
    r = minimize(nll, x0=[np.log(base / (1 - base)), 0.5], method="L-BFGS-B", bounds=[(None, None), (0.0, 1.0)])
    a, b = r.x
    return lambda p: 1.0 / (1.0 + np.exp(-(a + b * _logit(np.asarray(p, float))))), float(b)


def _trials(kind: str, n: int, rng) -> list[dict]:
    space = SPACES[kind]
    keys = sorted(space)
    combos, seen = [], set()
    total = int(np.prod([len(space[k]) for k in keys]))
    while len(combos) < min(n, total):
        c = {k: space[k][rng.integers(len(space[k]))] for k in keys}
        key = tuple(str(c[k]) for k in keys)
        if key not in seen:
            seen.add(key)
            combos.append(c)
    return combos


def rearrange(df: pd.DataFrame, side: str) -> pd.Series:
    """Monotone rearrangement across targets for each origin (Chernozhukov et al.): sort the probabilities."""
    out = df["p"].copy()
    for _, g in df.groupby("origin_idx"):
        order = g.sort_values("c").index
        vals = np.sort(g["p"].to_numpy())
        out.loc[order] = vals[::-1] if side == "up" else vals
    return out


def walk_forward(task: dict) -> pd.DataFrame:
    """task: rows (DataFrame with origin_idx, origin_date, outcome_date, target, y + feature columns), asset, cohort,
    h, side, model, cfg (backtest config), stop_date (no forecasts at/after; None in the final run)."""
    rows, cfg, h, side, name = task["rows"], task["cfg"], task["h"], task["side"], task["model"]
    kind, fs = ML_MODELS[name]
    cols = feature_cols(fs)
    if fs == "AD" and rows[GROUP_D].isna().all().all():
        return pd.DataFrame(columns=["origin_idx", "target", "model", "p"])
    ml = cfg["ml"]
    seed = cfg["seed"] + zlib.crc32(f"{task['asset']}|{task['cohort']}|{h}|{side}|{name}".encode()) % 100000
    rows = rows.sort_values(["origin_date", "target"]).reset_index(drop=True)
    lab = rows[rows["y"].notna()]
    dates = np.sort(rows["origin_date"].unique())
    first = pd.Timestamp(dates[0]) + pd.Timedelta(days=cfg["min_history_days"])
    stop = task["stop_date"]
    refits = []
    t = first
    last = pd.Timestamp(dates[-1])
    while t <= last and (stop is None or t < stop):
        refits.append(t)
        t += pd.Timedelta(days=ml["refit_days"])

    preds, params, tuned_at, log = [], None, None, []
    rng = np.random.default_rng(seed)
    for k, R in enumerate(refits):
        end = refits[k + 1] if k + 1 < len(refits) else (stop if stop is not None else last + pd.Timedelta(days=1))
        block = rows[(rows["origin_date"] >= R) & (rows["origin_date"] < end)]
        if block.empty:
            continue
        pool = lab[lab["outcome_date"] <= R]
        ev_dates = pool.loc[pool["y"] == 1, "outcome_date"].nunique()
        ne_dates = pool.loc[pool["y"] == 0, "outcome_date"].nunique()
        if (pool["outcome_date"].nunique() < ml["min_distinct_outcome_dates"]
                or min(ev_dates, ne_dates) < ml["min_class_dates"]):
            log.append({"refit": R, "status": "gate_failed", "events": ev_dates, "nonevents": ne_dates})
            continue
        span0, span1 = pool["origin_date"].min(), pool["origin_date"].max()
        val_start = span0 + (span1 - span0) * (1 - ml["inner_val_fraction"])
        inner_tr = pool[pool["outcome_date"] < val_start]          # purged: labels mature before the slice
        inner_va = pool[pool["origin_date"] >= val_start]
        ok_inner = bool(len(inner_tr) and len(inner_va) and inner_tr["y"].nunique() == 2 and inner_va["y"].nunique() == 2)
        if params is None or tuned_at is None or (R - tuned_at).days >= ml["retune_days"]:
            if ok_inner:
                best = None
                for trial in _trials(kind, ml["tuning_trials"], rng):
                    pv = _fit_predict(kind, trial, cols, side, seed, inner_tr, inner_tr["y"], inner_va)
                    b = float(np.mean((pv - inner_va["y"].to_numpy()) ** 2))
                    if best is None or b < best[0]:
                        best = (b, trial)
                params, tuned_at = best[1], R
            elif params is None:   # untuned fallback; tuned_at stays None so the next usable refit retunes
                params = _trials(kind, 1, np.random.default_rng(seed))[0]
        # per-target class gate (brief s.8): each target needs its own matured events and non-events
        tg = pool.groupby("target")
        ok_t = [t for t in block["target"].unique()
                if t in tg.groups
                and pool.loc[(pool["target"] == t) & (pool["y"] == 1), "outcome_date"].nunique() >= ml["min_class_dates"]
                and pool.loc[(pool["target"] == t) & (pool["y"] == 0), "outcome_date"].nunique() >= ml["min_class_dates"]]
        block = block[block["target"].isin(ok_t)]
        if block.empty:
            log.append({"refit": R, "status": "target_gate_failed", "events": ev_dates, "nonevents": ne_dates})
            continue
        cal, slope = None, np.nan
        va_ev = inner_va.loc[inner_va["y"] == 1, "outcome_date"].nunique() if ok_inner else 0
        va_ne = inner_va.loc[inner_va["y"] == 0, "outcome_date"].nunique() if ok_inner else 0
        if ok_inner and min(va_ev, va_ne) >= ml["cal_min_class_dates"]:
            got = shrink_calibrator(_fit_predict(kind, params, cols, side, seed, inner_tr, inner_tr["y"], inner_va),
                                    inner_va["y"].to_numpy())
            if got is not None:
                cal, slope = got
        p = _fit_predict(kind, params, cols, side, seed, pool, pool["y"], block)
        if cal is not None:
            p = cal(p)
        out = block[["origin_idx", "target", "c"]].copy()
        out["p"] = p
        out["p"] = rearrange(out, side)
        preds.append(out)
        log.append({"refit": R, "status": "ok", "pool_rows": len(pool), "events": ev_dates, "nonevents": ne_dates,
                    "params": params, "tuned_at": tuned_at, "calibrated": cal is not None, "cal_slope": slope,
                    "targets_gated_out": int(len(set(rows["target"].unique()) - set(ok_t)))})
    res = pd.concat(preds, ignore_index=True) if preds else pd.DataFrame(columns=["origin_idx", "target", "c", "p"])
    res = res.drop(columns=["c"]).assign(model=name)
    res.attrs["log"] = [{**e, "asset": task["asset"], "cohort": task["cohort"], "h": h, "side": side, "model": name}
                        for e in log]
    return res
