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
    """Plain-language Stage 3-4 summary: best baseline, DVOL Q-proxy and best ML per cell (development period)."""
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
        rows.append(row)
    return {"run_id": man["run_id"], "engine_revision": man["source_revision"], "holdout_seal": man["holdout_seal"],
            "holdout_opened": man["final_holdout_opened"], "metric": "Brier skill vs past event frequency (climatology); "
            "95% block-bootstrap interval; development period only", "cells": rows}
