"""Compact JSON for the public web page (Alpine site): market odds from an analyze run, findings from a backtest run.

Only derived probabilities and summary statistics are exported; raw option quotes are not redistributed.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from ..pricing import black76

UNAVAILABLE = None  # JSON null: the page renders "unavailable", never 0%


def _num(v, nd=6):
    return None if v is None or not np.isfinite(v) else round(float(v), nd)


def export_odds(run_dir: Path) -> dict:
    run_dir = Path(run_dir)
    man = json.loads((run_dir / "manifest.json").read_text())
    res = pd.read_parquet(run_dir / "results.parquet")
    curves = pd.read_parquet(run_dir / "curves.parquet") if (run_dir / "curves.parquet").exists() else pd.DataFrame()
    diag = pd.read_csv(run_dir / "fit_diagnostics.csv", parse_dates=["expiry_utc"])
    asof = pd.Timestamp(man["as_of_utc"])
    out = {"as_of_utc": asof.isoformat(), "as_of_new_york": asof.tz_convert("America/New_York").isoformat(),
           "source": "Deribit public API (option chains); probabilities derived, quotes not redistributed",
           "run_id": man["run_id"], "engine_revision": man["source_revision"],
           "probability_type": "Q: risk-neutral, implied by option prices. Not a forecast.", "assets": []}
    for asset in man["config"]["assets"]:
        r = res[(res["asset"] == asset)]
        prim = r[r["role"] == "primary"] if "role" in r else r
        fam = prim["family"].dropna()
        fam = fam[fam != ""]
        if prim["probability"].notna().sum() == 0 and fam.empty:
            out["assets"].append({"asset": asset, "status": "unavailable",
                                  "reason": str(prim["target_status"].iat[0]).removeprefix("unavailable: ") if len(prim) else "no data"})
            continue
        fam = fam.iat[0]
        a = {"asset": asset, "status": "ok", "family": fam, "spot": _num(prim["spot_index"].dropna().iat[0], 4),
             "expiries": []}
        for exp, g in prim.dropna(subset=["expiry_utc"]).groupby("expiry_utc"):
            d = diag[(diag["asset"] == asset) & (diag["family"] == fam) & (diag["expiry_utc"] == pd.Timestamp(exp))]
            if d.empty:
                continue
            d = d.iloc[0]
            F, T, iv = float(d["forward"]), float(d["days"]) / 365.0, float(d["atm_iv"])
            c = curves[(curves["asset"] == asset) & (curves["family"] == fam) & (curves["expiry_utc"] == exp)] if len(curves) else curves
            curve = []
            if len(c):
                c = c.sort_values("strike")
                step = max(1, len(c) // 90)
                curve = [[_num(k, 4), _num(q, 5)] for k, q in zip(c["strike"].iloc[::step], c["survival_Q"].iloc[::step])]
            bench = []
            if np.isfinite(iv) and curve:
                ks = np.linspace(curve[0][0], curve[-1][0], 60)
                bench = [[_num(k, 4), _num(float(black76.exceedance_probability(F, k, T, iv)), 5)] for k in ks]
            fc = g[g["model"] == "full_chain_constrained"].drop_duplicates("target_label")
            bm = g[g["model"] == "black76_constant_atm_iv"].drop_duplicates("target_label").set_index("target_label")["probability"]
            targets = []
            for t in fc.sort_values("K").itertuples():
                st = str(t.target_status)
                targets.append({"label": t.target_label, "K": _num(t.K, 4), "event": t.event,
                                "q": _num(t.probability, 5), "lo": _num(t.lower, 5), "hi": _num(t.upper, 5),
                                "black76": _num(bm.get(t.target_label, np.nan), 5),
                                "status": "usable" if st == "usable" else ("degraded" if st == "degraded" else "unavailable"),
                                "note": "" if st in ("usable", "degraded") else st.removeprefix("unavailable: ")})
            a["expiries"].append({
                "expiry_utc": pd.Timestamp(exp).isoformat(), "days": _num(d["days"], 1),
                "horizon": " / ".join(dict.fromkeys(g["horizon"].dropna())), "curve_status": d["status"],
                "atm_iv": _num(iv, 4), "forward": _num(F, 4),
                "support": [_num(d["strike_lo"], 4), _num(d["strike_hi"], 4)],
                "uncovered_below": _num(d["uncovered_mass_below"], 4), "uncovered_above": _num(d["uncovered_mass_above"], 4),
                "curve": curve, "black76_curve": bench, "targets": targets})
        out["assets"].append(a)
    return out


def export_findings(backtest_dir: Path) -> dict:
    """Plain-language Stage 3-5 summary: best baseline, DVOL Q-proxy, best ML and best Chronos variant per cell
    (development period). Cells without Chronos forecasts (90d) carry no best_fm keys."""
    backtest_dir = Path(backtest_dir)
    man = json.loads((backtest_dir / "manifest.json").read_text())
    L = pd.read_csv(backtest_dir / "leaderboard.csv")
    L = L[L["split"] == "dev"]
    rows = []
    for (cohort, asset, h), g in L.groupby(["cohort", "asset", "h"]):
        P = g[(g["comparison"] == "P models") & (g["model"] != "climatology")]
        if P.empty:
            continue
        b = P.loc[P["bss_vs_climatology"].idxmax()]
        row = {"cohort": cohort, "asset": asset, "h": int(h), "indep_outcomes": _num(b["approx_independent_origins"], 0),
               "best_baseline": b["model"], "best_baseline_bss": _num(b["bss_vs_climatology"], 4),
               "best_baseline_ci": [_num(b["bss_ci_lo"], 4), _num(b["bss_ci_hi"], 4)],
               "best_baseline_reliably_better": bool(b["dbrier_ci_hi"] < 0 and b.get("verdict_seed_stable", True))}
        M = g[(g["comparison"] == "Stage 4") & g["model"].isin(["logit_A", "rf_A", "xgb_A"])]
        if len(M):
            m = M.loc[M["bss_vs_climatology"].idxmax()]
            row.update({"best_ml": m["model"], "best_ml_bss": _num(m["bss_vs_climatology"], 4),
                        "best_ml_ci": [_num(m["bss_ci_lo"], 4), _num(m["bss_ci_hi"], 4)],
                        "best_ml_reliably_better": bool(m["dbrier_ci_hi"] < 0 and m.get("verdict_seed_stable", True))})
        Q = g[(g["comparison"] == "P + Q-proxy") & (g["model"] == "dvol_lognormal_Q")]
        if len(Q):
            q = Q.iloc[0]
            row.update({"dvol_q_bss": _num(q["bss_vs_climatology"], 4), "dvol_q_ci": [_num(q["bss_ci_lo"], 4), _num(q["bss_ci_hi"], 4)]})
        # Stage 5 foundation model: each variant on its primary comparison (vol on all rows, price on in-band rows)
        F = g[((g["comparison"] == "Stage 5") & (g["model"] == "chronos_vol"))
              | ((g["comparison"] == "Stage 5 in-band") & (g["model"] == "chronos_bolt"))]
        F = F[F["bss_vs_climatology"].notna()]
        if len(F):
            fm = F.loc[F["bss_vs_climatology"].idxmax()]
            row.update({"best_fm": fm["model"], "best_fm_bss": _num(fm["bss_vs_climatology"], 4),
                        "best_fm_ci": [_num(fm["bss_ci_lo"], 4), _num(fm["bss_ci_hi"], 4)],
                        "best_fm_reliably_better": bool(fm["dbrier_ci_hi"] < 0 and fm.get("verdict_seed_stable", True)),
                        "best_fm_comparison": fm["comparison"]})
        rows.append(row)
    return {"run_id": man["run_id"], "engine_revision": man["source_revision"], "holdout_seal": man["holdout_seal"],
            "holdout_opened": man["final_holdout_opened"], "metric": "Brier skill vs past event frequency (climatology); "
            "95% block-bootstrap interval; development period only", "cells": rows}


# ---------------------------------------------------------------------------------------------------------------------
# Model scorecard (/crypto-scorecard/): full leaderboard, calibration and, once the holdout is opened, the promotion verdict

SCORECARD_MODELS = {   # key: (page label, family, probability type)
    "climatology": ("Past frequency", "baseline", "P"),
    "lognormal_rv90": ("Lognormal, 90-day vol", "history", "P"),
    "lognormal_ewma": ("Lognormal, EWMA vol", "history", "P"),
    "lognormal_ewma_drift": ("Lognormal, EWMA vol + drift", "history", "P"),
    "fhs_ewma": ("Filtered historical simulation", "history", "P"),
    "garch_t": ("GARCH(1,1)-t", "history", "P"),
    "dvol_lognormal_Q": ("DVOL implied vol", "market", "Q-proxy"),
    "logit_A": ("Logistic regression", "ML", "P"),
    "rf_A": ("Random forest", "ML", "P"),
    "xgb_A": ("XGBoost", "ML", "P"),
    "xgb_AD": ("XGBoost + DVOL features", "ML", "P"),
    "chronos_bolt": ("Chronos (price)", "foundation", "P"),
    "chronos_vol": ("Chronos (volatility)", "foundation", "P"),
}
_LB_COLS = [("split", None), ("comparison", None), ("cohort", None), ("asset", None), ("h", "int"), ("model", None),
            ("mean_brier", 5), ("bss_vs_climatology", 4), ("bss_ci_lo", 4), ("bss_ci_hi", 4), ("dbrier_ci_lo", 5),
            ("dbrier_ci_hi", 5), ("verdict_seed_stable", "bool"), ("approx_independent_origins", 1), ("origins", 0),
            ("targets_scored", 0), ("common_start", None), ("common_end", None), ("mean_logloss", 4),
            ("best_baseline", None), ("dbrier_vs_best_baseline", 5), ("dbest_ci_lo", 5), ("dbest_ci_hi", 5)]
_PROMO_COLS = [("cohort", None), ("asset", None), ("h", "int"), ("model", None), ("comparison", None), ("testable", "bool"),
               ("strongest_dev_baseline", None), ("holdout_brier", 5), ("climatology_brier", 5), ("baseline_brier", 5),
               ("bss_vs_climatology", 4), ("bss_ci_lo", 4), ("bss_ci_hi", 4), ("reliability_error", 4),
               ("climatology_reliability_error", 4), ("coverage", 3), ("climatology_coverage", 3),
               ("c1_beats_clim_and_baseline", "bool"), ("c2_ci_below_zero_seed_stable", "bool"), ("c3_reliability_ok", "bool"),
               ("c4_coverage_ok", "bool"), ("promoted", "bool"), ("approx_independent_origins", 1), ("note", None)]


def _cell(v, kind):
    if v is None or (isinstance(v, float) and not np.isfinite(v)) or (not isinstance(v, str) and pd.isna(v)):
        return None
    if kind is None:
        return str(v)
    if kind == "int":
        return int(v)
    if kind == "bool":
        return bool(v) if not isinstance(v, str) else v.strip().lower() == "true"
    return round(float(v), kind) if kind else int(round(float(v)))


def _table(df: pd.DataFrame, spec) -> dict:
    cols = [c for c, _ in spec]
    rows = [[_cell(r.get(c), k) for c, k in spec] for r in df.to_dict("records")]
    return {"columns": cols, "rows": rows}


def scorecard_comparison(model: str) -> str:
    """The comparison each model is naturally scored on: its promotion home (backtest.promotion.HOME), with the DVOL
    Q-proxy on 'P + Q-proxy' and every history baseline on 'P models'."""
    from ..backtest.promotion import HOME
    return "P + Q-proxy" if model == "dvol_lognormal_Q" else HOME.get(model, "P models")


def export_scorecard(run_dir: Path) -> dict:
    """Compact JSON for the model scorecard page. Works for a development run and for the final run (holdout rows in
    leaderboard.csv / reliability.csv plus promotion.csv).

    Leaderboard: every row on each model's natural comparison, plus climatology on each of those and Chronos
    (price) on its in-band subset (also required for its promotion). Reliability: the same model/comparison pairs,
    nested as {"split|cohort|asset|h|comparison|model": [[bin, mean_forecast, observed, n, lo, hi], ...]}."""
    run_dir = Path(run_dir)
    man = json.loads((run_dir / "manifest.json").read_text())
    bt = man.get("config_backtest", {})
    L = pd.read_csv(run_dir / "leaderboard.csv")
    nat = L["model"].map(scorecard_comparison)
    used = set(nat) | {"Stage 5 in-band"}
    keep = (L["comparison"] == nat) | ((L["model"] == "climatology") & L["comparison"].isin(used)) \
        | ((L["model"] == "chronos_bolt") & (L["comparison"] == "Stage 5 in-band"))
    L = L[keep].sort_values(["split", "cohort", "asset", "h", "comparison", "model"])
    pairs = set(zip(L["comparison"], L["model"]))
    R = pd.read_csv(run_dir / "reliability.csv")
    R = R[[(c, m) in pairs for c, m in zip(R["comparison"], R["model"])]]
    bins = list(dict.fromkeys(R["bin"]))
    edges = bt.get("reliability_bins")
    if not edges or len(edges) != len(bins) + 1:
        edges = None
    rel = {}
    for key, g in R.groupby(["split", "cohort", "asset", "h", "comparison", "model"], sort=True):
        rel["|".join(str(k) for k in key)] = [[bins.index(r["bin"]), _cell(r["mean_forecast"], 3), _cell(r["observed"], 3),
                                               int(r["n"]), _cell(r["lo"], 3), _cell(r["hi"], 3)]
                                              for r in g.to_dict("records")]
    pp = run_dir / "promotion.csv"
    promo = None
    if pp.exists() and pp.stat().st_size > 0:
        P = pd.read_csv(pp)
        promo = _table(P, _PROMO_COLS)
    present = list(dict.fromkeys(L["model"]))
    models = {m: {"label": SCORECARD_MODELS.get(m, (m, "other", "P"))[0], "family": SCORECARD_MODELS.get(m, (m, "other", "P"))[1],
                  "probability": SCORECARD_MODELS.get(m, (m, "other", "P"))[2], "comparison": scorecard_comparison(m)}
              for m in [*SCORECARD_MODELS, *[m for m in present if m not in SCORECARD_MODELS]] if m in present}
    prom = bt.get("promotion", {})
    return {
        "meta": {"run_id": man["run_id"], "created_utc": str(man.get("created_utc", ""))[:19],
                 "engine_revision": man.get("source_revision"), "holdout_seal": man.get("holdout_seal", {}),
                 "holdout_opened": bool(man.get("final_holdout_opened", False)),
                 "splits": sorted(L["split"].unique().tolist(), key=lambda s: (s != "dev", s)),
                 "metric": "Brier skill vs past event frequency (climatology): 1 - Brier / Brier of climatology on the "
                           "same rows; 95% moving-block bootstrap interval",
                 "bootstrap_reps": bt.get("bootstrap_reps"), "verdict_seeds": bt.get("verdict_seeds"),
                 "min_independent_events": bt.get("min_independent_events"),
                 "promotion_rule": " ".join(str(prom.get("rule", "")).split()) or None,
                 "promotion_clarifications": " ".join(str(prom.get("clarifications", "")).split()) or None,
                 "reliability_bins": edges, "reliability_bin_labels": bins},
        "models": models,
        "leaderboard": _table(L, _LB_COLS),
        "reliability": rel,
        "promotion": promo,
    }


if __name__ == "__main__":   # python -m crypto_options.reporting.web_export scorecard <run_dir> <out.json>
    import sys
    if len(sys.argv) != 4 or sys.argv[1] != "scorecard":
        sys.exit("usage: python -m crypto_options.reporting.web_export scorecard <backtest_run_dir> <out.json>")
    _sc = export_scorecard(Path(sys.argv[2]))
    Path(sys.argv[3]).write_text(json.dumps(_sc, separators=(",", ":"), allow_nan=False))
    print(f"wrote {sys.argv[3]}: {Path(sys.argv[3]).stat().st_size / 1024:.0f} KB, splits {_sc['meta']['splits']}, "
          f"promotion {'yes' if _sc['promotion'] else 'no'}")
