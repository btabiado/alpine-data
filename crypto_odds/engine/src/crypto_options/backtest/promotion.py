"""Apply the predeclared promotion rule (config backtest.promotion) to the one-time holdout scores.

Written and committed before the holdout was opened. For each (cohort, asset, horizon) and each challenger, all four
conditions must hold on the holdout:
  1. mean Brier below both climatology and the strongest development-period P baseline for that cell
     (same holdout rows: both are scored inside the challenger's comparison)
  2. block-bootstrap 95% CI of the Brier difference vs climatology entirely below 0, and seed-stable
  3. reliability (count-weighted mean |observed - forecast| over the reliability bins) no worse than climatology's
     by more than 0.02
  4. holdout coverage at least 90% of climatology's
Otherwise the simpler baseline is retained.

Clarifications made before the opening, after an independent pre-flight review (no holdout result seen):
  - coverage (4) is measured on the cell's testable targets only;
  - Chronos's price representation is scored on all rows and must also pass on its in-band subset;
  - the strongest development baseline is judged against climatology only in (1), since it cannot beat itself;
  - cells with no testable holdout targets are listed explicitly as untestable.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

HOME = {"logit_A": "Stage 4", "rf_A": "Stage 4", "xgb_A": "Stage 4", "xgb_AD": "Stage 4 + DVOL",
        "chronos_bolt": "Stage 5", "chronos_vol": "Stage 5"}
ALSO_REQUIRED = {"chronos_bolt": "Stage 5 in-band"}   # must pass in its in-band subset too (pre-opening clarification)
COLUMNS = ["cohort", "asset", "h", "model", "comparison", "testable", "strongest_dev_baseline", "baseline_fallback",
           "holdout_brier", "climatology_brier", "baseline_brier", "bss_vs_climatology", "bss_ci_lo", "bss_ci_hi",
           "reliability_error", "climatology_reliability_error", "coverage", "climatology_coverage",
           "c1_beats_clim_and_baseline", "c2_ci_below_zero_seed_stable", "c3_reliability_ok", "c4_coverage_ok",
           "promoted", "approx_independent_origins", "note"]
P_BASELINES = ["lognormal_rv90", "lognormal_ewma", "lognormal_ewma_drift", "fhs_ewma", "garch_t"]
RELIABILITY_TOL = 0.02
COVERAGE_MIN = 0.90


def home_comparison(model: str) -> str:
    return HOME.get(model, "P models")


def strongest_dev_baseline(L: pd.DataFrame, cohort: str, asset: str, h: int) -> str:
    d = L[(L["split"] == "dev") & (L["comparison"] == "P models") & (L["cohort"] == cohort) & (L["asset"] == asset)
          & (L["h"] == h) & L["model"].isin(P_BASELINES)]
    if d.empty or d["bss_vs_climatology"].max() <= 0:
        return "climatology"
    return d.loc[d["bss_vs_climatology"].idxmax(), "model"]


def _reliability_error(R: pd.DataFrame, comparison: str, cohort: str, asset: str, h: int, model: str) -> float:
    r = R[(R["split"] == "holdout") & (R["comparison"] == comparison) & (R["cohort"] == cohort) & (R["asset"] == asset)
          & (R["h"] == h) & (R["model"] == model)]
    if r.empty:
        return np.nan
    return float(np.average((r["observed"] - r["mean_forecast"]).abs(), weights=r["n"]))


def _coverage(C: pd.DataFrame, testable: set, cohort, asset, h, model) -> float:
    """Coverage on the cell's testable targets only (pre-opening clarification, config promotion.coverage_basis):
    a model abstaining on a target nobody is scored on is not penalised; abstaining on a scored target is."""
    if C.empty:
        return np.nan
    c = C[(C["cohort"] == cohort) & (C["asset"] == asset) & (C["h"] == h) & (C["model"] == model) & C["target"].isin(testable)]
    return float(np.average(c["covered"], weights=c["rows"])) if len(c) and c["rows"].sum() > 0 else np.nan


def _conditions(L, R, C, S, cell, comp, model, best, cohort, asset, h):
    x = cell[cell["comparison"] == comp]
    if model not in set(x["model"]) or "climatology" not in set(x["model"]):
        return None
    m, clim = x[x["model"] == model].iloc[0], x[x["model"] == "climatology"].iloc[0]
    fallback = best not in set(x["model"])
    b = clim if fallback else x[x["model"] == best].iloc[0]
    # the strongest dev baseline cannot beat itself: it is judged against climatology only
    c1 = m["mean_brier"] < clim["mean_brier"] and (model == best or m["mean_brier"] < b["mean_brier"])
    c2 = bool(m["dbrier_ci_hi"] < 0 and m.get("verdict_seed_stable", True))
    rel_m = _reliability_error(R, comp, cohort, asset, h, model)
    rel_c = _reliability_error(R, comp, cohort, asset, h, "climatology")
    c3 = bool(np.isfinite(rel_m) and np.isfinite(rel_c) and rel_m <= rel_c + RELIABILITY_TOL)
    st = S[(S["split"] == "holdout") & (S["comparison"] == comp) & (S["cohort"] == cohort) & (S["asset"] == asset)
           & (S["h"] == h) & (S["model"] == "climatology") & S["testable"]]
    tt = set(st["target"])
    cov_m, cov_c = _coverage(C, tt, cohort, asset, h, model), _coverage(C, tt, cohort, asset, h, "climatology")
    c4 = bool(np.isfinite(cov_m) and np.isfinite(cov_c) and cov_c > 0 and cov_m >= COVERAGE_MIN * cov_c)
    return {"comparison": comp, "baseline_fallback": fallback, "holdout_brier": m["mean_brier"],
            "climatology_brier": clim["mean_brier"], "baseline_brier": b["mean_brier"],
            "bss_vs_climatology": m["bss_vs_climatology"], "bss_ci_lo": m["bss_ci_lo"], "bss_ci_hi": m["bss_ci_hi"],
            "reliability_error": rel_m, "climatology_reliability_error": rel_c, "coverage": cov_m,
            "climatology_coverage": cov_c, "c1_beats_clim_and_baseline": bool(c1), "c2_ci_below_zero_seed_stable": c2,
            "c3_reliability_ok": c3, "c4_coverage_ok": c4, "approx_independent_origins": m["approx_independent_origins"]}


def decide(rdir: Path) -> pd.DataFrame:
    rdir = Path(rdir)
    L = pd.read_csv(rdir / "leaderboard.csv")
    R = pd.read_csv(rdir / "reliability.csv")
    S = pd.read_csv(rdir / "scores_by_target.csv")
    C = pd.read_csv(rdir / "coverage_holdout.csv") if (rdir / "coverage_holdout.csv").exists() else pd.DataFrame()
    H = L[L["split"] == "holdout"]
    rows = []
    # every holdout cell that exists in the data, testable or not, so nothing drops out silently
    cells = S[S["split"] == "holdout"][["cohort", "asset", "h"]].drop_duplicates() if "split" in S else H[["cohort", "asset", "h"]].drop_duplicates()
    for r in cells.itertuples():
        cohort, asset, h = r.cohort, r.asset, int(r.h)
        cell = H[(H["cohort"] == cohort) & (H["asset"] == asset) & (H["h"] == h)]
        best = strongest_dev_baseline(L, cohort, asset, h)
        if cell.empty:
            rows.append({"cohort": cohort, "asset": asset, "h": h, "model": "(all)", "testable": False, "promoted": False,
                         "strongest_dev_baseline": best,
                         "note": f"untestable: fewer than the required independent event / non-event windows on the holdout"})
            continue
        for model in sorted(set(cell["model"]) - {"climatology", "dvol_lognormal_Q"}):
            res = _conditions(L, R, C, S, cell, home_comparison(model), model, best, cohort, asset, h)
            if res is None:
                continue
            note = ""
            if model in ALSO_REQUIRED:
                extra = _conditions(L, R, C, S, cell, ALSO_REQUIRED[model], model, best, cohort, asset, h)
                ok_extra = bool(extra and all(extra[k] for k in ("c1_beats_clim_and_baseline", "c2_ci_below_zero_seed_stable",
                                                                 "c3_reliability_ok", "c4_coverage_ok")))
                note = f"must also pass in {ALSO_REQUIRED[model]}: {'yes' if ok_extra else 'no'}"
            else:
                ok_extra = True
            promoted = all(res[k] for k in ("c1_beats_clim_and_baseline", "c2_ci_below_zero_seed_stable",
                                            "c3_reliability_ok", "c4_coverage_ok")) and ok_extra
            rows.append({"cohort": cohort, "asset": asset, "h": h, "model": model, "testable": True,
                         "strongest_dev_baseline": best, **res, "promoted": bool(promoted), "note": note})
    out = pd.DataFrame(rows, columns=COLUMNS)
    out.to_csv(rdir / "promotion.csv", index=False)
    return out
