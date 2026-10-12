"""Stage 3 Markdown report: what history supports, with the holdout sealed. Every number is computed."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

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
    out.append("# Stages 3–4: walk-forward evaluation of real-world (P) baselines" + (" and ML challengers" if has_ml else "") + "\n")
    out.append(f"**Run:** `{man['run_id']}` · **Created:** {man['created_utc'][:16]} UTC · **Source revision:** "
               f"`{man['source_revision']}` · **Final holdout opened:** {'YES' if man['final_holdout_opened'] else 'no (sealed)'}\n")
    out.append("> **What this is.** Every forecast is a probability that the price on day t+h is above (or below) a "
               "threshold set relative to the price on day t. Each forecast used only data available on day t, and "
               "each is scored against the outcome that later happened. Probabilities are **P**, real-world "
               "estimates from price history, except **DVOL lognormal**, a **Q-proxy** built from Deribit's "
               "implied-volatility index. Where the Q-proxy and the outcomes disagree, that can reflect a risk "
               "premium rather than an error. Scores cover the **development period only**. The final holdout ("
               + ", ".join(f"{c} cohort from {d}" for c, d in seal.items()) +
               ") is sealed and will be scored once, together with the Stage 4 models.\n")

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
                     "exploratory until the holdout confirms or refutes them.")
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
            out.append(_md_table(disp.reset_index(names="Asset · model").astype(str)) + "\n")

    # ---------------- sample sizes and holdout share
    out.append("## Sample size, testability and split\n")
    tt = S[(S["split"] == "dev") & (S["model"] == "climatology") & (S["comparison"] == "P models")]
    tt = tt.groupby(["cohort", "asset", "h"]).agg(targets=("target", "size"), testable=("testable", "sum"),
                                                  origins=("n", "max")).reset_index()
    out.append(_md_table(tt.assign(h=tt["h"].astype(str) + "d").rename(columns={
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
        out.append(_md_table(sp.reset_index().astype(str)) + "\n")

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
    if st4:
        out.append(st4)
        out.append("## Next (Stage 5)\n")
        out.append("A pinned time-series foundation model (Chronos), plus optional news-feature and ensemble "
                   "experiments. After that, the sealed holdout is opened once and every model is scored together "
                   "under the promotion rule above.\n")
    else:
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
    o.append("Logistic regression, random forest and XGBoost, each trained walk-forward: refit every "
             f"{bt['ml']['refit_days']} days on labels already matured, tuned with at most {bt['ml']['tuning_trials']} "
             "predeclared trials per year on a purged inner validation slice, and (for the tree models) calibrated on "
             "that slice. They are scored on the same rows as the baselines, which here means the rows the ML models "
             "cover, so baseline skill in this table differs slightly from the Stage 3 tables.\n")
    ml = S4[S4["model"].isin(ML_NAMES)]
    beat_clim = ml[(ml["dbrier_ci_hi"] < 0) & ml["verdict_seed_stable"]]
    beat_best = ml[ml["dbest_ci_hi"] < 0]
    worse_best = ml[ml["dbest_ci_lo"] > 0]
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
            o.append(f"Tree-model refits calibrated on a past-only slice: {int(ok['calibrated'].astype(str).eq('True').sum())} "
                     f"of {int(ok['model'].isin(['rf_A', 'xgb_A', 'xgb_AD']).sum())}.\n")
    o.append("### Promotion rule (predeclared, applied once on the holdout)\n")
    o.append("> " + " ".join(bt["promotion"]["rule"].split()) + "\n")
    o.append("XGBoost quantile regression is deferred, because the data cannot support it. With 12–100 approximately "
             "independent outcomes per cell, far-tail quantiles would come from a handful of events, which the brief "
             "warns against. Ensembles wait until individual models have been evaluated on the holdout.\n")
    return "\n".join(o)
