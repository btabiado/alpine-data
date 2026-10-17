"""Stage 3 charts: held-out leaderboard with uncertainty, reliability with counts (brief s.10)."""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from ..reporting.charts import INK, INK2, S1, S2, S3, SURFACE  # noqa: E402  (shared rcParams/palette)

LABEL = {"climatology": "Past frequency (climatology)", "lognormal_rv90": "Lognormal, 90-day realized vol",
         "lognormal_ewma": "Lognormal, EWMA vol", "lognormal_ewma_drift": "Lognormal, EWMA vol + est. drift",
         "fhs_ewma": "Filtered historical simulation", "garch_t": "GARCH(1,1)-t",
         "dvol_lognormal_Q": "DVOL lognormal (Q-proxy)", "logit_A": "Logistic regression (ML)",
         "rf_A": "Random forest (ML)", "xgb_A": "XGBoost (ML)", "xgb_AD": "XGBoost + DVOL features (ML)",
         "chronos_bolt": "Chronos-Bolt, price representation", "chronos_vol": "Chronos-Bolt, volatility representation"}
ORDER = ["dvol_lognormal_Q", "garch_t", "fhs_ewma", "lognormal_ewma_drift", "lognormal_ewma", "lognormal_rv90"]
ML_ORDER = ["xgb_AD", "xgb_A", "rf_A", "logit_A"]


def leaderboard_figure(L: pd.DataFrame, cohort: str, comparison: str, path: Path):
    x = L[(L["cohort"] == cohort) & (L["split"] == "dev") & (L["comparison"] == comparison)]
    if x.empty:
        return False
    order = {"P + Q-proxy": ORDER, "P models": [m for m in ORDER if not m.endswith("_Q")],
             "Stage 4": [m for m in ORDER if not m.endswith("_Q")] + [m for m in ML_ORDER if m != "xgb_AD"],
             "Stage 4 + DVOL": ORDER + ML_ORDER}[comparison]
    assets = [a for a in ["BTC", "ETH", "XRP", "LINK"] if a in set(x["asset"])]
    hs = sorted(x["h"].unique())
    fig, axes = plt.subplots(len(hs), len(assets), figsize=(3.3 * len(assets), (0.25 * len(order) + 1.0) * len(hs) + 0.8),
                             sharex=True, squeeze=False, constrained_layout=True)
    lo_all = np.nanmin(x["bss_ci_lo"]) if len(x) else -0.3
    hi_all = np.nanmax(x["bss_ci_hi"]) if len(x) else 0.3
    for r, h in enumerate(hs):
        for c, a in enumerate(assets):
            ax = axes[r, c]
            g = x[(x["asset"] == a) & (x["h"] == h)].set_index("model")
            models = [m for m in order if m in g.index]
            ax.set_ylim(-0.6, len(order) - 0.4)
            ax.set_yticks(np.arange(len(order)))
            ax.set_yticklabels([LABEL[m] for m in order] if c == 0 else [""] * len(order), fontsize=7.5)
            if not models:
                ax.text(0.5, 0.5, "no testable targets", transform=ax.transAxes, ha="center", color=INK2, fontsize=8)
            else:
                yy = np.array([order.index(m) for m in models])  # fixed row per model in every panel
                bss = g.loc[models, "bss_vs_climatology"].to_numpy()
                lo = g.loc[models, "bss_ci_lo"].to_numpy()
                hi = g.loc[models, "bss_ci_hi"].to_numpy()
                sig = lo > 0
                col = [S2 if m == "dvol_lognormal_Q" else (S3 if m in ML_ORDER else S1) for m in models]
                ax.hlines(yy, lo, hi, color=col, lw=1.6)
                ax.scatter(bss, yy, s=28, c=[c_ if s_ else SURFACE for c_, s_ in zip(col, sig)], edgecolors=col,
                           linewidths=1.8, zorder=3)
                n_ind = g["approx_independent_origins"].iat[0]
                ax.set_title(f"≈{n_ind:.0f} independent outcomes", loc="right", fontsize=7, color=INK2,
                             fontweight="normal")
            ax.axvline(0, color=INK2, lw=0.9)
            ax.set_xlim(min(lo_all, -0.05) - 0.02, max(hi_all, 0.05) + 0.02)
            ax.grid(axis="y", visible=False)
            if r == 0:
                ax.set_title(a, fontsize=10, loc="left")
            if c == 0:
                ax.set_ylabel(f"{h}-day horizon", fontsize=9, color=INK)
            if r == len(hs) - 1:
                ax.set_xlabel("Brier skill vs past frequency (95% block-bootstrap CI)", fontsize=7.5)
    span = f"{x['common_start'].min()} → {x['common_end'].max()}"
    title = {"P models": "real-world (P) baselines, full history",
             "P + Q-proxy": "P baselines + DVOL Q-proxy, DVOL period only",
             "Stage 4": "ML challengers (green) vs P baselines, ML-covered rows",
             "Stage 4 + DVOL": "ML incl. DVOL features + Q-proxy, DVOL period"}[comparison]
    fig.suptitle(f"Walk-forward skill, {cohort} cohort: {title} ({span}; holdout sealed). "
                 f"Filled dot = reliably better than past frequency (CI above 0).", x=0.01, ha="left", fontsize=10.5, color=INK)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return True


def reliability_figure(R: pd.DataFrame, cohort: str, asset: str, h: int, path: Path):
    x = R[(R["cohort"] == cohort) & (R["asset"] == asset) & (R["h"] == h) & (R["split"] == "dev")]
    if x.empty:
        return False
    comp = "P + Q-proxy" if (x["comparison"] == "P + Q-proxy").any() else "P models"
    x = x[x["comparison"] == comp]
    models = [m for m in ["climatology"] + ORDER[::-1] if m in set(x["model"])]
    fig, axes = plt.subplots(1, len(models), figsize=(2.25 * len(models), 2.7), sharey=True, constrained_layout=True)
    for ax, m in zip(np.atleast_1d(axes), models):
        g = x[x["model"] == m]
        col = S2 if m == "dvol_lognormal_Q" else S1
        ax.plot([0, 1], [0, 1], color=INK2, lw=0.8, ls=":")
        ax.errorbar(g["mean_forecast"], g["observed"], yerr=[g["observed"] - g["lo"], g["hi"] - g["observed"]],
                    fmt="o", ms=4, color=col, elinewidth=1, capsize=2)
        for _, r in g.iterrows():
            ax.annotate(f"{int(r['n'])}", (r["mean_forecast"], r["observed"]), textcoords="offset points",
                        xytext=(3, -9), fontsize=6, color=INK2)
        ax.set_xlim(0, 1); ax.set_ylim(0, 1)
        ax.set_title(LABEL[m].replace(", ", ",\n", 1), fontsize=7.5)
        ax.set_xlabel("forecast", fontsize=7.5)
    np.atleast_1d(axes)[0].set_ylabel("observed frequency", fontsize=8)
    fig.suptitle(f"Reliability — {asset}, {cohort} cohort, {h}-day, all targets pooled (dev period; "
                 f"labels = forecasts per bin; bars = 95% block-bootstrap CI)", x=0.01, ha="left", fontsize=9, color=INK)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return True


def make_all(rdir: Path) -> list[str]:
    rdir = Path(rdir)
    cdir = rdir / "charts"
    cdir.mkdir(exist_ok=True)
    L = pd.read_csv(rdir / "leaderboard.csv")
    R = pd.read_csv(rdir / "reliability.csv")
    files = []
    for cohort in sorted(L["cohort"].unique()):
        for comp, tag in (("P models", "P"), ("P + Q-proxy", "PQ"), ("Stage 4", "ML"), ("Stage 4 + DVOL", "MLD")):
            p = cdir / f"leaderboard_{cohort}_{tag}.png"
            if leaderboard_figure(L, cohort, comp, p):
                files.append(p.name)
        for a in ["BTC", "ETH", "XRP", "LINK"]:
            for h in (30, 90):
                p = cdir / f"reliability_{cohort}_{a}_{h}d.png"
                if reliability_figure(R, cohort, a, h, p):
                    files.append(p.name)
    return files
