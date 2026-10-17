"""Stage 3 Markdown report: what history supports, with the holdout sealed. Every number is computed."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from ..pandas_compat import as_py_str
from ..reporting.report import _md_table
from .charts import LABEL



def _f(v, d=3):
    return "—" if v is None or not np.isfinite(v) else f"{v:+.{d}f}"


def _ci(lo, hi):
    return f"[{lo:+.3f}, {hi:+.3f}]" if np.isfinite(lo) and np.isfinite(hi) else "—"


def verdict(r) -> str:
    if r.model == "climatology":
        return "reference"
    v = "better" if r.dbrier_ci_hi < 0 else ("worse" if r.dbrier_ci_lo > 0 else "not distinguishable")
    if not r.verdict_seed_stable:
        v += " (seed-sensitive)"
    return v


def _leader_table(x: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for r in x.sort_values(["asset", "h", "bss_vs_climatology"], ascending=[True, True, False]).itertuples():
        rows.append({
            "Asset": r.asset, "h": f"{r.h}d", "Model": LABEL[r.model], "Type": "Q-proxy" if r.model.endswith("_Q") else "P",
            "Brier skill": _f(r.bss_vs_climatology), "95% CI": _ci(r.bss_ci_lo, r.bss_ci_hi) if r.model != "climatology" else "—",
            "Verdict vs past freq.": verdict(r),
            "Non-overlap ΔBrier range": "—" if r.model == "climatology" else
            f"{r.nonoverlap_dbrier_min:+.4f} … {r.nonoverlap_dbrier_max:+.4f} ({r.nonoverlap_share_better:.0%} of offsets better)",
            "Log loss": f"{r.mean_logloss:.3f}", "Targets": int(r.targets_scored), "≈ indep.": f"{r.approx_independent_origins:.0f}",
            "Period": f"{r.common_start} → {r.common_end}"})
    return pd.DataFrame(rows)


def write(rdir: Path) -> str:
    rdir = Path(rdir)
    man = json.loads((rdir / "manifest.json").read_text())
    L = pd.read_csv(rdir / "leaderboard.csv")
    S = pd.read_csv(rdir / "scores_by_target.csv")
    G = pd.read_csv(rdir / "data_gaps.csv")
    C = pd.read_csv(rdir / "coverage.csv")
    Y = pd.read_csv(rdir / "stability_by_year.csv")
    N = pd.read_csv(rdir / "split_counts.csv")
    bt = man["config_backtest"]
    dev = L[L["split"] == "dev"]
    P = dev[dev["comparison"] == "P models"]
    PQ = dev[dev["comparison"] == "P + Q-proxy"]
    seal = man["holdout_seal"]

    out = []
    has_ml = (L["comparison"] == "Stage 4").any()
    has_fm = (L["comparison"] == "Stage 5").any()
    out.append("# Stages 3–" + ("5" if has_fm else "4" if has_ml else "3") + ": walk-forward evaluation of real-world (P) baselines"
               + (", ML challengers" if has_ml else "") + (" and a time-series foundation model" if has_fm else "") + "\n")
    out.append(f"**Run:** `{man['run_id']}` · **Created:** {man['created_utc'][:16]} UTC · **Source revision:** "
               f"`{man['source_revision']}` · **Final holdout opened:** {'YES' if man['final_holdout_opened'] else 'no (sealed)'}\n")
    out.append("> **What this is.** Every forecast is a probability that the price on day t+h is above (or below) a "
               "threshold set relative to the price on day t. Each forecast used only data available on day t, and "
               "each is scored against the outcome that later happened. Probabilities are **P**, real-world "
               "estimates from price history, except **DVOL lognormal**, a **Q-proxy** built from Deribit's "
               "implied-volatility index. Where the Q-proxy and the outcomes disagree, that can reflect a risk "
               "premium rather than an error. "
               + ("The final holdout section comes first; all later sections cover the **development period only**.\n"
                  if man.get("final_holdout_opened") else
                  "Scores cover the **development period only**. The final holdout ("
                  + ", ".join(f"{c} cohort from {d}" for c, d in seal.items()) +
                  ") is sealed and will be scored once, together with every other model.\n"))

    if man.get("final_holdout_opened") and (rdir / "promotion.csv").exists():
        out.append(holdout_section(rdir, L, man, bt))

    # ---------------- bottom line (primary = P models, full history)
    out.append("## Bottom line\n")
    lines = []
    for cohort in sorted(P["cohort"].unique()):
        x = P[(P["cohort"] == cohort) & (P["model"] != "climatology")]
        all_cells = S[(S["cohort"] == cohort) & (S["split"] == "dev") & (S["comparison"] == "P models")][["asset", "h"]].drop_duplicates()
        scored = x[["asset", "h"]].drop_duplicates()
        missing = all_cells.merge(scored, how="left", indicator=True).query("_merge == 'left_only'")
        wins = x[(x["dbrier_ci_hi"] < 0) & x["verdict_seed_stable"]]
        losses = x[(x["dbrier_ci_lo"] > 0) & x["verdict_seed_stable"]]
        unstable = x[~x["verdict_seed_stable"]]
        best = x.loc[x.groupby(["asset", "h"])["bss_vs_climatology"].idxmax()]
        txt = (f"- **{cohort.capitalize()} cohort, full history:** {len(scored)} asset × horizon cells have testable "
               f"targets")
        if len(missing):
            txt += f" ({', '.join(f'{r.asset} {r.h}d' for r in missing.itertuples())} have none)"
        txt += (f". In {wins[['asset', 'h']].drop_duplicates().shape[0]} of them, at least one model beats past "
                f"frequency with a 95% interval excluding zero")
        if len(wins):
            txt += " (" + "; ".join(sorted({f"{r.asset} {r.h}d {LABEL[r.model]}" for r in wins.itertuples()})) + ")"
        txt += (f". {len(losses)} model × cell results are reliably worse than past frequency"
                + (f"; {len(unstable)} verdicts are seed-sensitive and not counted" if len(unstable) else "")
                + ". The best model per "
                f"cell ranges from {best['bss_vs_climatology'].min():+.1%} to {best['bss_vs_climatology'].max():+.1%} "
                "Brier skill.")
        lines.append(txt)
    if len(PQ):
        q = PQ[PQ["model"] == "dvol_lognormal_Q"]
        lines.append(f"- **DVOL Q-proxy (BTC and ETH, from 2021):** Brier skill vs past frequency from "
                     f"{q['bss_vs_climatology'].min():+.1%} to {q['bss_vs_climatology'].max():+.1%} across "
                     f"{len(q)} cohort × asset × horizon cells; "
                     f"{int((q['dbrier_ci_hi'] < 0).sum())} reliably better, {int((q['dbrier_ci_lo'] > 0).sum())} reliably worse.")
    fragile = P[(P["model"] != "climatology") & (P["dbrier_ci_hi"] < 0) & P["verdict_seed_stable"]
                & (P["approx_independent_origins"] < 20)]
    if len(fragile):
        lines.append(f"- **Caution:** {len(fragile)} of the reliably-better results rest on fewer than 20 approximately "
                     "independent outcomes (" + ", ".join(sorted({f'{r.cohort} {r.asset} {r.h}d' for r in fragile.itertuples()}))
                     + "). With that few outcomes, a block bootstrap can understate uncertainty. Treat them as "
                     + ("exploratory; see the holdout section." if man.get("final_holdout_opened") else
                        "exploratory until the holdout confirms or refutes them."))
    lines.append("- **Read this as:** with a few years of overlapping 30–90-day outcomes, history does not reliably "
                 "beat simply using past event frequency for these thresholds. Wide intervals are the finding, not a "
                 "defect. No model is promoted. Stage 4 has to beat the strongest of these on the same rows.")
    out.append("\n".join(lines) + "\n")

    # ---------------- leaderboards
    for cohort in sorted(P["cohort"].unique()):
        out.append(f"## Leaderboard: {cohort} cohort, P baselines, full history\n")
        src = G[G["cohort"] == cohort].set_index("asset")["source"].to_dict()
        out.append("Outcome series: " + "; ".join(f"{a} `{s}`" for a, s in src.items()) + ".\n")
        out.append(_md_table(_leader_table(P[P["cohort"] == cohort])) + "\n")
        out.append(f"![leaderboard P](charts/leaderboard_{cohort}_P.png)\n")
        q = PQ[PQ["cohort"] == cohort]
        if len(q):
            out.append(f"### {cohort.capitalize()} cohort with the DVOL Q-proxy (BTC and ETH, DVOL period only)\n")
            out.append("This is a separate comparison on the shorter window where DVOL exists. Skill here is not "
                       "comparable to the full-history table above, because the rows differ.\n")
            out.append(_md_table(_leader_table(q)) + "\n")
            out.append(f"![leaderboard P+Q](charts/leaderboard_{cohort}_PQ.png)\n")

    out.append("**How to read these tables.**\n\n"
               "- **Brier skill** = 1 − Brier(model) / Brier(past frequency), on identical rows, averaged over the "
               "testable targets.\n"
               f"- **Testable target:** at least {bt['min_independent_events']} non-overlapping h-day windows contain "
               "events and at least that many contain non-events.\n"
               f"- **Intervals:** circular block bootstrap over origin dates, block length h, {bt['bootstrap_reps']} "
               f"replicates, seed {bt['seed']}. Targets and models are resampled jointly, so model differences are "
               "paired.\n"
               f"- **\"Seed-sensitive\":** the verdict changes when the bootstrap is rerun with "
               f"{bt.get('verdict_seeds', 5)} different seeds. Treat it as not established.\n"
               "- **Non-overlap range:** the same comparison recomputed on every h-th origin only, once per phase "
               "offset. It shows how much the answer depends on which non-overlapping subsample is used.\n"
               f"- **Log loss:** probabilities clipped to [{bt['logloss_clip']}, 1 − {bt['logloss_clip']}].\n"
               "- **\"≈ indep.\":** origins ÷ h.\n")

    # ---------------- calibration
    out.append("## Calibration (reliability, all targets pooled)\n")
    out.append("Charts use the P + Q-proxy comparison where DVOL exists (BTC and ETH), so the Q-proxy panel is "
               "visible, and the full-history P comparison elsewhere. Numbers by each point are forecasts in that "
               "bin. Bars are 95% block-bootstrap intervals.\n")
    for p in sorted((rdir / "charts").glob("reliability_*.png")):
        out.append(f"![{p.stem}](charts/{p.name})")
    out.append("")

    # ---------------- drift sensitivity (P models)
    pv = P[P["model"].isin(["lognormal_ewma", "lognormal_ewma_drift"])].pivot_table(
        index=["cohort", "asset", "h"], columns="model", values="bss_vs_climatology")
    if len(pv):
        worse = int((pv["lognormal_ewma_drift"] < pv["lognormal_ewma"]).sum())
        out.append("## Drift sensitivity (zero vs. estimated)\n")
        out.append(f"Adding a trailing {bt['drift_window']}-day drift estimate to the EWMA lognormal model lowered "
                   f"skill in {worse} of {len(pv)} asset × horizon cells (full-history P comparison).\n")

    # ---------------- GARCH disclosure
    gi = man.get("garch_refits_integrated", {})
    if gi:
        out.append("## GARCH estimates at the integrated boundary\n")
        out.append("The fitted GARCH(1,1)-t often sits at α + β ≈ 1, where volatility shocks never decay. At that "
                   "boundary the h-day variance is h·σ²ₜ₊₁ + ω·h(h−1)/2, which grows faster than linearly. "
                   "Refits at the boundary, by asset and cohort: " + "; ".join(f"{k} {v}" for k, v in gi.items())
                   + ". Details are in `garch_refits.csv`.\n")

    # ---------------- stability
    y = Y[(Y["split"] == "dev") & (Y["h"] == 30)] if len(Y) else Y
    if len(y):
        out.append("## Regime stability (30-day, testable targets, Brier skill by origin year)\n")
        for cohort in sorted(y["cohort"].unique()):
            yc = y[y["cohort"] == cohort]
            pv = yc.pivot_table(index=["asset", "model"], columns="year", values="bss_vs_climatology")
            flips = int(((pv > 0).any(axis=1) & (pv < 0).any(axis=1)).sum())
            out.append(f"**{cohort.capitalize()} cohort:** in {flips} of {len(pv)} asset × model rows, skill is "
                       "positive in some years and negative in others.\n")
            disp = pv.round(2).astype(object).where(pv.notna(), "—")
            disp.index = [f"{a} · {LABEL[m]}" for a, m in disp.index]
            out.append(_md_table(as_py_str(disp.reset_index(names="Asset · model"))) + "\n")

    # ---------------- sample sizes and holdout share
    out.append("## Sample size, testability and split\n")
    tt = S[(S["split"] == "dev") & (S["model"] == "climatology") & (S["comparison"] == "P models")]
    tt = tt.groupby(["cohort", "asset", "h"]).agg(targets=("target", "size"), testable=("testable", "sum"),
                                                  origins=("n", "max")).reset_index()
    out.append(_md_table(tt.assign(h=as_py_str(tt["h"]) + "d").rename(columns={
        "cohort": "Cohort", "asset": "Asset", "targets": "Targets", "testable": "Testable", "origins": "Dev origins"})) + "\n")
    untest = S[(S["split"] == "dev") & (S["model"] == "climatology") & (S["comparison"] == "P models") & ~S["testable"]]
    rare = untest["target"].isin([0.75, 1.0, -0.5]).sum()
    out.append(f"{len(untest)} asset × horizon × target combinations are untestable ({rare} of them are the rare "
               "tails +75%, +100% and −50%). They are reported, not scored, and their per-target intervals are left "
               "blank.\n")
    sp = N[N["h"] == 30].pivot_table(index=["cohort", "asset"], columns="split", values="forecastable_origins",
                                     aggfunc="sum").fillna(0)
    if "holdout" in sp:
        sp["holdout share of forecastable origins"] = (sp["holdout"] / sp.sum(axis=1)).map(lambda v: f"{v:.0%}")
        out.append("Split of forecastable 30-day origins. The holdout is the last 20% of the shortest asset's "
                   "label-eligible timeline, with one date per cohort so pooled comparisons stay aligned. That is "
                   "why its share differs by asset.\n")
        out.append(_md_table(as_py_str(sp.reset_index())) + "\n")

    # ---------------- coverage and cost
    out.append("## Coverage and cost\n")
    cov = C[C["dev_rows"] > 0].assign(cell=lambda d: d["covered"].map(lambda v: f"{v:.0%}") + " from " + d["first_forecast"])
    out.append(_md_table(cov.pivot_table(index=["cohort", "asset"], columns="model", values="cell", aggfunc="first")
                         .reset_index().fillna("—")) + "\n")
    out.append("Coverage is the share of development rows with a forecast, after 365 days of warm-up. DVOL exists "
               "for BTC and ETH only, from 2021-03-24. "
               f"Inference cost for the whole run: {man['costs']['wall_seconds']} s wall, "
               f"{man['costs']['cpu_seconds']} s CPU on this Mac, $0 data cost.\n")

    # ---------------- data gaps
    out.append("## Data-gap report\n")
    out.append(_md_table(G.rename(columns={"asset": "Asset", "cohort": "Cohort", "source": "Source", "start": "Start",
                                           "end": "End", "days": "Days", "gaps": "Gaps", "gap_list": "Gap dates",
                                           "methodology_break": "Outcomes excluded from"}).fillna("")) + "\n")
    out.append("- Settlement cohort: Deribit delivery prices, the 30-minute TWAP ending 08:00 UTC, which are the "
               "values options settle on. BTC and ETH use the daily era only (from 2020-02); the earlier "
               "weekly-only records are dropped. LINK outcomes on or after 2026-08-01 are excluded because "
               "Deribit moved option-less assets to a different index around then.\n"
               "- Spot cohort: venue daily closes at 00:00 UTC (Coinbase; Bitstamp for XRP, the only continuous "
               "free USD series). It is a proxy series, reported separately and never mixed with settlement "
               "outcomes.\n"
               "- Not available: historical option chains (paid only); XRP and LINK implied-vol indexes; LINK "
               "options of any kind.\n")

    # ---------------- integrity
    out.append("## Leakage and integrity checks\n")
    out.append("- **Truncation invariance:** for every model, forecasts at an origin are identical, to the bit, "
               "when all data after that origin is deleted (`tests/test_backtest.py`).\n"
               "- **Future-dated data rejected:** corrupting prices after an origin leaves its forecast unchanged, "
               "and labels are used only once matured (outcome date ≤ origin date).\n"
               "- **No tuning:** every model setting is predeclared in `config.yaml` (`backtest:`). GARCH is refit "
               "every 30 days on past returns only, and its multi-day variance is checked against `arch`'s own "
               "forecast in the tests.\n"
               "- **Purging:** development origins whose outcome date falls on or after the holdout start are "
               "dropped.\n"
               "- **Sealed holdout:** the start dates live in `config/holdout_seal.json`, which is committed to git, "
               "and a missing seal cannot be silently re-derived. Before the final evaluation, holdout-origin and "
               "purged rows are not written anywhere: not to `forecasts.parquet`, the CSVs or the charts. Opening "
               "the holdout requires `--open-final-holdout OPEN-ONCE`, is logged in `config/holdout_ledger.json`, "
               "and is refused a second time.\n"
               f"- **Reproducibility:** seed {man['seed']}; the manifest records code hash "
               f"`{man['code_sha256'][:12]}`, the SHA-256 of every input series and every output.\n")
    st4 = stage4_section(rdir, dev, man, bt)
    st5 = stage5_section(rdir, dev, man, bt)
    if st4:
        out.append(st4)
    if st5:
        out.append(st5)
    if st5 and not man.get("final_holdout_opened"):
        out.append("## Next: the one-time holdout evaluation\n")
        out.append("Every model is scored once on the sealed holdout under the predeclared promotion rule. Nothing "
                   "is revised after seeing it. The holdout is entirely after Chronos-Bolt's weights were frozen, so it "
                   "is also the clean test of the foundation model.\n")
    elif st4 and not st5:
        out.append("## Next (Stage 5)\n")
        out.append("A pinned time-series foundation model (Chronos), plus optional news-feature and ensemble "
                   "experiments. After that, the sealed holdout is opened once and every model is scored together "
                   "under the promotion rule above.\n")
    elif not st4 and not st5:
        out.append("## Next (Stage 4)\n")
        out.append("Logistic regression, XGBoost and random forest on lagged features, trained with nested "
                   "chronological validation inside the development period.\n")
    path = rdir / "report.md"
    path.write_text("\n".join(out))
    return str(path)


ML_NAMES = ["logit_A", "rf_A", "xgb_A", "xgb_AD"]


def stage4_section(rdir: Path, dev: pd.DataFrame, man: dict, bt: dict) -> str:
    S4 = dev[dev["comparison"] == "Stage 4"]
    if S4.empty:
        return ""
    o = ["## Stage 4: ML challengers (development period)\n"]
    o.append(f"ML {bt['ml'].get('version', 'v1')}. Logistic regression, random forest and XGBoost, each trained walk-forward: refit every "
             f"{bt['ml']['refit_days']} days on labels already matured, tuned with at most {bt['ml']['tuning_trials']} "
             "predeclared trials per year on a purged inner validation slice, and calibrated on that slice with shrink-only "
             "Platt scaling, which can pull forecasts toward the base rate but never sharpen them. Each target needs its "
             "own minimum count of events and non-events before it is forecast. They are scored on the same rows as the baselines, which here means the rows the ML models "
             "cover, so baseline skill in this table differs slightly from the Stage 3 tables.\n")
    ml = S4[S4["model"].isin(ML_NAMES)]
    beat_clim = ml[(ml["dbrier_ci_hi"] < 0) & ml["verdict_seed_stable"]]
    stable = ml["dbest_seed_stable"] if "dbest_seed_stable" in ml else True
    beat_best = ml[(ml["dbest_ci_hi"] < 0) & stable]
    worse_best = ml[(ml["dbest_ci_lo"] > 0) & stable]
    point_best = ml[ml["dbrier_vs_best_baseline"] < 0]
    cells = S4[["cohort", "asset", "h"]].drop_duplicates()
    o.append(f"- **Cells scored:** {len(cells)} cohort × asset × horizon cells with ML coverage.\n"
             f"- **ML beats past frequency** (95% interval below zero, seed-stable): {len(beat_clim)} model × cell results"
             + (" (" + "; ".join(sorted({f'{r.cohort} {r.asset} {r.h}d {LABEL[r.model]}' for r in beat_clim.itertuples()})) + ")" if len(beat_clim) else "")
             + ".\n"
             f"- **ML beats the strongest baseline in that cell** (paired 95% interval below zero): {len(beat_best)}"
             + (" (" + "; ".join(sorted({f'{r.cohort} {r.asset} {r.h}d {LABEL[r.model]} vs {LABEL[r.best_baseline]}' for r in beat_best.itertuples()})) + ")" if len(beat_best) else "")
             + f". Reliably worse than the strongest baseline: {len(worse_best)}. Better on the point estimate only: "
             f"{len(point_best)} of {len(ml)}.\n"
             "- The strongest baseline is picked on these same development rows, which flatters the baseline "
             "slightly. That makes \"beats the strongest baseline\" a conservative test.\n")
    rows = []
    for r in ml.sort_values(["cohort", "asset", "h", "model"]).itertuples():
        rows.append({"Cohort": r.cohort, "Asset": r.asset, "h": f"{r.h}d", "Model": LABEL[r.model],
                     "Brier skill vs past freq.": _f(r.bss_vs_climatology), "95% CI": _ci(r.bss_ci_lo, r.bss_ci_hi),
                     "Strongest baseline": LABEL[r.best_baseline],
                     "ΔBrier vs it": f"{r.dbrier_vs_best_baseline:+.4f}", "95% CI ": _ci(r.dbest_ci_lo, r.dbest_ci_hi),
                     "Log loss": f"{r.mean_logloss:.3f}", "≈ indep.": f"{r.approx_independent_origins:.0f}",
                     "Period": f"{r.common_start} → {r.common_end}"})
    o.append(_md_table(pd.DataFrame(rows)) + "\n")
    for tag in ("ML", "MLD"):
        for p in sorted((rdir / "charts").glob(f"leaderboard_*_{tag}.png")):
            o.append(f"![{p.stem}](charts/{p.name})")
    o.append("")
    # sharpness diagnostic: how widely each model's forecasts move, vs how often the event actually varies
    fp = rdir / "forecasts.parquet"
    if fp.exists():
        f = pd.read_parquet(fp)
        f = f[f["y"].notna()]
        cols = [c for c in ["climatology", "lognormal_ewma", "logit_A", "rf_A", "xgb_A"] if c in f]
        f = f.dropna(subset=cols)
        sd = f.groupby(["cohort", "asset", "h", "target"])[cols].std().median()
        corr = {c: f.groupby(["cohort", "asset", "h", "target"]).apply(
            lambda g: g[c].corr(g["y"]) if g[c].std() > 0 else np.nan, include_groups=False).median() for c in cols}
        o.append("### Why the ML models lose: overconfidence\n")
        o.append("The table shows the median over cells and targets of (a) the spread of each model's forecasts across "
                 "origin dates and (b) their correlation with the outcome. The ML forecasts swing far more than past "
                 "frequency does, but the swings carry almost no information about outcomes. Brier scores punish "
                 "that.\n")
        o.append(_md_table(pd.DataFrame({"Model": [LABEL[c] for c in cols],
                                         "Forecast spread (median s.d.)": [f"{sd[c]:.3f}" for c in cols],
                                         "Correlation with outcome (median)": [f"{corr[c]:+.3f}" for c in cols]})) + "\n")
        o.append("With overlapping daily labels and only about 12–100 independent outcomes per cell, flexible models "
                 "fit regime patterns (trend, distance from highs) that later reverse. Tuning on a recent, equally "
                 "autocorrelated validation slice cannot detect that. Under the predeclared rule, the simpler "
                 "baseline stays. Further ML variants would need to be predeclared before the holdout is opened, "
                 "and must not be tuned until one wins.\n")
    # DVOL feature ablation
    D = dev[dev["comparison"] == "Stage 4 + DVOL"]
    if len(D):
        a = D[D["model"] == "xgb_A"].set_index(["cohort", "asset", "h"])["mean_brier"]
        b = D[D["model"] == "xgb_AD"].set_index(["cohort", "asset", "h"])["mean_brier"]
        j = pd.concat([a.rename("A"), b.rename("AD")], axis=1).dropna()
        if len(j):
            o.append("### Feature ablation: does adding DVOL features help XGBoost?\n")
            o.append(f"On identical rows (BTC and ETH, DVOL period), adding the DVOL feature group lowered mean Brier in "
                     f"{int((j['AD'] < j['A']).sum())} of {len(j)} cells. The median change is "
                     f"{(j['AD'] - j['A']).median():+.4f} Brier, against a typical Brier of {j['A'].median():.3f}.\n")
    # gates and tuning
    lp = rdir / "ml_refits.csv"
    if lp.exists():
        lg = pd.read_csv(lp)
        g = lg.groupby(["model", "status"]).size().unstack(fill_value=0)
        o.append("### Training gates and tuning\n")
        o.append("Refit events by status, across all cells (a gate failure means fewer than "
                 f"{bt['ml']['min_distinct_outcome_dates']} distinct matured outcome dates, or fewer than "
                 f"{bt['ml']['min_class_dates']} distinct event or non-event dates, so no forecast is made):\n")
        o.append(_md_table(g.reset_index()) + "\n")
        ok = lg[lg["status"] == "ok"]
        if len(ok) and "calibrated" in ok:
            cal = as_py_str(ok["calibrated"]).eq("True")
            o.append(f"ML refits calibrated on a past-only slice: {int(cal.sum())} of {len(ok)}. The rest had too few events "
                     "in the slice and were left uncalibrated. "
                     + (f"Median fitted calibration slope: {ok['cal_slope'].median():.2f}. The bound is 1, which means no "
                        "shrinkage: the slice next to the training data shares its regime and does not reveal the "
                        "overconfidence that appears out of sample. " if "cal_slope" in ok else ""))
        if "params" in ok:
            tun = ok[ok["model"] == "logit_A"].drop_duplicates(["asset", "cohort", "h", "side", "tuned_at"])
            hit = as_py_str(tun["params"]).str.contains("'C': 0.0001").sum()
            o.append(f"Logistic tuning events that chose the smallest C offered (1e-4): {int(hit)} of {len(tun)}. The data "
                     "keep asking for more regularization. The grid was fixed before this run and is not widened after "
                     "seeing results.\n")
    C = pd.read_csv(rdir / "coverage.csv") if (rdir / "coverage.csv").exists() else pd.DataFrame()
    if len(C):
        cv = C.pivot_table(index=["cohort", "asset"], columns="model", values="covered")
        mlc = [m for m in ML_NAMES if m in cv]
        if "climatology" in cv and mlc:
            ratio = cv[mlc].div(cv["climatology"], axis=0)
            low = (ratio < 0.9).sum().sum()
            o.append("### Coverage\n")
            o.append(f"Before an ML model may forecast a target, the class gates require enough matured events and "
                     f"non-events for that target. That cuts ML coverage: in {int(low)} of {ratio.size} model × "
                     "cohort × asset combinations, ML covers under 90% of the rows past frequency covers (median ratio "
                     f"{ratio.stack().median():.2f}). Promotion condition (4) therefore fails in those cells whatever "
                     "the skill.\n")
            o.append(_md_table(as_py_str(ratio.round(2).reset_index())) + "\n")
    o.append("### Promotion rule (predeclared, applied once on the holdout)\n")
    o.append("> " + " ".join(bt["promotion"]["rule"].split()) + "\n")
    o.append("XGBoost quantile regression is deferred, because the data cannot support it. With 12–100 approximately "
             "independent outcomes per cell, far-tail quantiles would come from a handful of events, which the brief "
             "warns against. Ensembles wait until individual models have been evaluated on the holdout.\n")
    return "\n".join(o)


def stage5_section(rdir: Path, dev: pd.DataFrame, man: dict, bt: dict) -> str:
    S5 = dev[dev["comparison"].str.startswith("Stage 5")]
    if S5.empty:
        return ""
    fm = man.get("foundation_model") or {}
    fcfg = bt.get("foundation", {})
    o = ["## Stage 5: time-series foundation model (development period)\n"]
    o.append(_md_table(pd.DataFrame([
        {"Item": "Checkpoint", "Value": f"`{fm.get('model', fcfg.get('model'))}` @ `{str(fm.get('revision', fcfg.get('revision')))[:10]}` "
                                         f"(weights dated {fm.get('weights_date', fcfg.get('weights_date'))}, {fm.get('license', 'apache-2.0')})"},
        {"Item": "Input", "Value": f"univariate, no covariates, last {fm.get('context_length', 512)} daily values up to the origin: "
                                  "log price (price representation) or log 30-day realized vol (volatility representation)"},
        {"Item": "Output", "Value": "price: quantiles 10%…90% of the h-day log return; volatility: median realized-vol path → "
                                   "h-day variance. Horizons " + ", ".join(f"{h}d" for h in fm.get('horizons', [30, 60]))
                                   + " (90d not run: beyond the native 64-step horizon)"},
        {"Item": "Event probability", "Value": "price: linear interpolation of the quantile function inside 10–90%, with a normal tail "
                                              "fitted to the 10–90% spread outside it (disclosed; rows flagged). Volatility: zero-drift "
                                              "lognormal, as for the baselines"},
        {"Item": "Runtime / deps", "Value": f"CPU, {fm.get('threads', 4)} threads; torch {fm.get('versions', {}).get('torch', '')}, "
                                           f"transformers {fm.get('versions', {}).get('transformers', '')}, numpy {fm.get('versions', {}).get('numpy', '')}; "
                                           f"{sum(v['seconds'] for v in fm.get('timing', {}).values()):.0f} s for "
                                           f"{sum(v['origins'] for v in fm.get('timing', {}).values()):,} origin forecasts"},
        {"Item": "Contamination", "Value": "The pretraining corpus is not fully documented, so overlap with crypto prices before "
                                          f"{fcfg.get('weights_date')} cannot be ruled out. The post-release table below uses only origins after "
                                          "that date. The sealed holdout is entirely post-release, so it is the clean test."},
    ])) + "\n")
    fp = rdir / "forecasts.parquet"
    if fp.exists():
        f = pd.read_parquet(fp, columns=["cohort", "asset", "h", "target", "y", "climatology", "chronos_bolt", "chronos_in_band"])
        f = f[f["y"].notna() & f["chronos_bolt"].notna()]
        if len(f):
            share = f["chronos_in_band"].mean()
            g = f.groupby(["cohort", "asset", "h", "target"])
            sd = g[["climatology", "chronos_bolt"]].std().median()
            corr = g.apply(lambda d: d["chronos_bolt"].corr(d["y"]) if d["chronos_bolt"].std() > 0 else np.nan, include_groups=False).median()
            o.append(f"Targets inside Chronos's own 10–90% band: {share:.0%} of scored rows; the rest rely on the tail assumption. "
                     f"Median forecast spread: {sd['chronos_bolt']:.3f} vs {sd['climatology']:.3f} for past frequency; median "
                     f"correlation with the outcome {corr:+.3f}.\n")
    sections = (
        ("Stage 5 in-band", "chronos_bolt", "Price representation, rows where the target lies inside Chronos's 10–90% band (no tail assumption)",
         "Conditioned on Chronos's own forecast, which is known at the origin, so there is no leakage. These rows need no tail "
         "assumption. They are a selected subset, so baseline skill here differs from the full-history tables."),
        ("Stage 5", "chronos_bolt", "Price representation, all rows (tail rows use the normal-tail assumption)",
         "Rows outside the 10–90% band rely on the disclosed tail rule, not on the model. For holdout promotion the price "
         "representation must pass on all rows and on the in-band subset."),
        ("Stage 5", "chronos_vol", "Volatility representation, all rows",
         "Chronos forecasts the 30-day realized volatility path, which goes into the same zero-drift lognormal as the "
         "baselines. No tail assumption and no drift, so it isolates whether Chronos forecasts volatility better than "
         "EWMA, GARCH or realized-vol rules."),
        ("Stage 5 post-release", "chronos_bolt", f"Origins on or after {fcfg.get('weights_date')} (no possible pretraining overlap)",
         "Few independent outcomes; read as exploratory."))
    for comp, model, title, note in sections:
        x = S5[S5["comparison"] == comp]
        ch = x[x["model"] == model] if len(x) else x
        if ch.empty:
            St = pd.read_csv(rdir / "scores_by_target.csv")
            st = St[(St["comparison"] == comp) & (St["split"] == "dev") & (St["model"] == model)]
            if len(st):
                o.append(f"### {title}\n")
                o.append(f"No testable targets. This window has only {int(st['n'].max())} development origins per cell at most, "
                         f"and every target falls below the {bt['min_independent_events']}-independent-window minimum, so "
                         "nothing is scored. The sealed holdout, entirely after the weights date, is where Chronos gets its "
                         "contamination-free test.\n")
            continue
        o.append(f"### {title}\n")
        o.append(note + "\n")
        better = ch[(ch["dbrier_ci_hi"] < 0) & ch["verdict_seed_stable"]]
        worse = ch[(ch["dbrier_ci_lo"] > 0) & ch["verdict_seed_stable"]]
        bb = ch[(ch["dbest_ci_hi"] < 0) & ch.get("dbest_seed_stable", True)]
        wb = ch[(ch["dbest_ci_lo"] > 0) & ch.get("dbest_seed_stable", True)]
        o.append(f"Beats past frequency in {len(better)} of {len(ch)} cells; reliably worse in {len(worse)}; beats the "
                 f"strongest history model in {len(bb)}; reliably worse than it in {len(wb)}"
                 + (" (" + ", ".join(f"{r.cohort} {r.asset} {r.h}d" for r in wb.itertuples()) + ")" if len(wb) else "") + ".\n")
        if len(better):
            o.append(f"With {len(ch)} cells tested, about {0.025 * len(ch):.1f} interval-excludes-zero 'wins' are expected by chance "
                     "alone, before any multiple-comparison correction. The win(s) above "
                     + ("rest on few independent outcomes and " if (better["approx_independent_origins"] < 20).any() else "")
                     + "are exploratory.\n")
        if model == "chronos_vol" and (rdir / "vol_calibration.csv").exists():
            vc = pd.read_csv(rdir / "vol_calibration.csv")
            if len(vc):
                o.append(f"Variance calibration: Chronos's h-day variance averages {vc['ratio'].min():.2f}–{vc['ratio'].max():.2f}× the "
                         f"realized mean squared return ({int((vc['ratio'] < 1).sum())} of {len(vc)} cells below 1). Squaring a median "
                         "path biases variance low.\n")
        rows = [{"Cohort": r.cohort, "Asset": r.asset, "h": f"{r.h}d", "Skill vs past freq.": _f(r.bss_vs_climatology),
                 "95% CI": _ci(r.bss_ci_lo, r.bss_ci_hi), "Verdict": verdict(r),
                 "Strongest baseline": LABEL.get(r.best_baseline, r.best_baseline), "ΔBrier vs it": f"{r.dbrier_vs_best_baseline:+.4f}",
                 "95% CI ": _ci(r.dbest_ci_lo, r.dbest_ci_hi), "≈ indep.": f"{r.approx_independent_origins:.0f}",
                 "Period": f"{r.common_start} → {r.common_end}"} for r in ch.sort_values(["cohort", "asset", "h"]).itertuples()]
        o.append(_md_table(pd.DataFrame(rows)) + "\n")
    qp = rdir / "quantile_scores.csv"
    if qp.exists():
        q = pd.read_csv(qp)
        q = q[q["split"] == "dev"]
        if len(q):
            o.append("### Quantile forecasts: pinball loss and 80% interval (price representation)\n")
            o.append("Chronos's nine quantiles of the h-day log return are compared with those of a zero-drift lognormal "
                     "using EWMA variance, on identical origins. A negative ΔPinball favours Chronos; the CI is a block "
                     "bootstrap with block h. A positive median bias means Chronos's median sits above the realized return.\n")
            better_q = int((q["dpinball_ci_hi"] < 0).sum())
            worse_q = int((q["dpinball_ci_lo"] > 0).sum())
            o.append(f"Chronos has lower pinball loss with a CI below zero in {better_q} of {len(q)} cells, higher in {worse_q}. "
                     f"Its median sits above the outcome in {int((q['median_bias_chronos'] > 0).sum())} of {len(q)} cells (mean "
                     f"bias {q['median_bias_chronos'].mean():+.3f} log points, with {int((q['median_bias_chronos'] < 0).sum())} cells negative). "
                     "The comparison model has zero drift, so this measures Chronos's own drift relative to outcomes.\n")
            o.append(_md_table(pd.DataFrame({
                "Cohort": q["cohort"], "Asset": q["asset"], "h": as_py_str(q["h"]) + "d",
                "Pinball Chronos": q["pinball_chronos"].map(lambda v: f"{v:.4f}"),
                "Pinball lognormal": q["pinball_lognormal_ewma"].map(lambda v: f"{v:.4f}"),
                "ΔPinball [95% CI]": [f"{a:+.4f} [{b:+.4f}, {c:+.4f}]" for a, b, c in zip(q["dpinball"], q["dpinball_ci_lo"], q["dpinball_ci_hi"])],
                "80% coverage Chronos / lognormal": [f"{a:.0%} / {b:.0%}" for a, b in zip(q["cover80_chronos"], q["cover80_lognormal_ewma"])],
                "80% width Chronos / lognormal": [f"{a:.3f} / {b:.3f}" for a, b in zip(q["width80_chronos"], q["width80_lognormal_ewma"])],
                "Median bias": q["median_bias_chronos"].map(lambda v: f"{v:+.3f}")})) + "\n")
    sk = fcfg.get("skipped", {})
    if sk:
        o.append("### Experiments not run, and why\n")
        o.append("\n".join(f"- **{k.replace('_', ' ')}**: {v}" for k, v in sk.items()) + "\n")
    return "\n".join(o)


def holdout_section(rdir: Path, L: pd.DataFrame, man: dict, bt: dict) -> str:
    """Final, one-time holdout results under the predeclared promotion rule."""
    import json as _json
    pp = rdir / "promotion.csv"
    pr = pd.read_csv(pp) if pp.exists() and pp.stat().st_size > 0 else pd.DataFrame()
    o = ["## Final holdout evaluation (scored once)\n"]
    o.append("The holdout was sealed before any model was built and opened once, for every model together. Holdout "
             f"periods: {', '.join(f'{c} from {d}' for c, d in man['holdout_seal'].items())}. No design choice, tuning step "
             "or rule was made with these outcomes in view. Walk-forward models (ML, GARCH) do keep refitting through the "
             "holdout, but only on labels that had matured by each refit date, as they would in live use. The promotion "
             "rule was predeclared in `config.yaml`. The clarifications below were recorded before the opening, and "
             "`promotion.py` applies the rule mechanically.\n")
    o.append("> " + " ".join(bt["promotion"]["rule"].split()) + "\n")
    if bt["promotion"].get("clarifications"):
        o.append("> Clarifications: " + " ".join(bt["promotion"]["clarifications"].split()) + "\n")
    from . import evaluate as _E
    led = _E.LEDGER
    if led.exists():
        att = _json.loads(led.read_text())
        o.append("Ledger of holdout attempts: " + "; ".join(
            f"opened {a['opened_utc'][:16]} → {'completed ' + a['completed_utc'][:16] if a.get('completed_utc') else 'not completed'}"
            f" (code {str(a.get('code_sha256', ''))[:8]})" for a in att) + ".\n")
    if pr.empty:
        o.append("**No holdout cell could be scored.** The holdout is too short for any target to meet the testability "
                 "minimum. No model is promoted, and past event frequency is retained.\n")
        return "\n".join(o)
    untestable = pr[~pr["testable"].astype(bool)]
    tested = pr[pr["testable"].astype(bool)]
    cells_t = tested[["cohort", "asset", "h"]].drop_duplicates()
    if len(untestable):
        o.append(f"**Untestable on the holdout ({len(untestable)} cells):** "
                 + ", ".join(f"{r.cohort} {r.asset} {r.h}d" for r in untestable.itertuples())
                 + f". These have too few independent outcome windows (minimum {bt['min_independent_events']}) in the "
                 "holdout period, so no model can be promoted or rejected there.\n")
    promoted = tested[tested["promoted"].astype(bool)]
    retained = []
    for r in cells_t.itertuples():
        g = tested[(tested["cohort"] == r.cohort) & (tested["asset"] == r.asset) & (tested["h"] == r.h)]
        best = g["strongest_dev_baseline"].iat[0]
        bp = g[(g["model"] == best) & g["promoted"].astype(bool)]
        retained.append({"cell": f"{r.cohort} {r.asset} {r.h}d", "retained": best if len(bp) else "climatology"})
    if promoted.empty:
        o.append(f"**Result: no model is promoted in any of the {len(cells_t)} testable cells.** Past event frequency "
                 "(climatology) is retained: no demonstrated improvement.\n")
    else:
        o.append(f"**Result: {len(promoted)} promotion(s) across {len(cells_t)} testable cells**: " + "; ".join(
            f"{r.cohort} {r.asset} {r.h}d {LABEL.get(r.model, r.model)} (skill {r.bss_vs_climatology:+.1%}, "
            f"CI [{r.bss_ci_lo:+.1%}, {r.bss_ci_hi:+.1%}], ≈{r.approx_independent_origins:.0f} indep.)" for r in promoted.itertuples())
            + ". Retained model per testable cell: " + "; ".join(f"{x['cell']}: {LABEL.get(x['retained'], x['retained'])}" for x in retained) + ".\n")
    cnt = tested.groupby("model")[["c1_beats_clim_and_baseline", "c2_ci_below_zero_seed_stable", "c3_reliability_ok",
                                   "c4_coverage_ok", "promoted"]].sum().astype(int)
    cnt["cells"] = tested.groupby("model").size()
    cnt.index = [LABEL.get(m, m) for m in cnt.index]
    o.append("Conditions met per model, counted over testable cells:\n")
    o.append(_md_table(cnt.reset_index(names="Model").rename(columns={
        "c1_beats_clim_and_baseline": "(1) beats past freq. and best dev baseline",
        "c2_ci_below_zero_seed_stable": "(2) CI below 0", "c3_reliability_ok": "(3) reliability ok",
        "c4_coverage_ok": "(4) coverage ok", "promoted": "Promoted", "cells": "Cells"})) + "\n")
    rows = []
    for (c, a, h), g in tested.groupby(["cohort", "asset", "h"]):
        b = g.loc[g["bss_vs_climatology"].idxmax()]
        rows.append({"Cohort": c, "Asset": a, "h": f"{h}d", "Best holdout model": LABEL.get(b["model"], b["model"]),
                     "Its skill": _f(b["bss_vs_climatology"]), "95% CI": _ci(b["bss_ci_lo"], b["bss_ci_hi"]),
                     "Strongest dev baseline": LABEL.get(b["strongest_dev_baseline"], b["strongest_dev_baseline"]),
                     "≈ indep.": f"{b['approx_independent_origins']:.0f}", "Promoted": "yes" if g["promoted"].astype(bool).any() else "no"})
    o.append(_md_table(pd.DataFrame(rows)) + "\n")
    disc = rdir / "DISCLOSURES.md"
    if disc.exists():
        o.append("### Disclosures\n")
        o.append(disc.read_text() + "\n")
    H = L[L["split"] == "holdout"]
    q = H[(H["comparison"] == "P + Q-proxy") & (H["model"] == "dvol_lognormal_Q")]
    if len(q):
        o.append("DVOL Q-proxy on the holdout (market-implied, not eligible for P promotion): Brier skill "
                 + ", ".join(f"{r.cohort} {r.asset} {r.h}d {r.bss_vs_climatology:+.1%}" for r in q.itertuples()) + ".\n")
    return "\n".join(o)
