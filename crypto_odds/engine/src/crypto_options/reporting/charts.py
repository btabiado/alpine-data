"""Static PNG charts (brief s.10): accepted quotes + fitted smile, terminal density, exceedance vs targets.

Colors: dataviz reference palette (light mode), slots 1-3 only (validated all-pairs).
Identity never relies on color alone: legends on every multi-series panel, dashed benchmark.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from ..pricing import black76  # noqa: E402

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
GRID = "#e4e3df"
S1, S2, S3 = "#2a78d6", "#eb6834", "#1baf7a"   # full-chain, Black-76 benchmark, third series
QUOTE = "#8a8984"

plt.rcParams.update({
    "font.family": "Arial", "font.size": 9, "axes.edgecolor": GRID, "axes.labelcolor": INK2,
    "xtick.color": INK2, "ytick.color": INK2, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6,
    "axes.spines.top": False, "axes.spines.right": False, "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE, "legend.frameon": False, "axes.titlesize": 10, "axes.titlecolor": INK,
    "axes.titleweight": "bold", "axes.titlelocation": "left",
})


def _fmt_price(x):
    return f"{x:,.0f}" if x >= 100 else f"{x:,.2f}"


def _quote_ivs(q, T):
    out = []
    for _, r in q.iterrows():
        call = r["cp"] == "C"
        lo = black76.implied_vol(r["bid_n"], 1.0, r["k"], T, 1.0, call)
        hi = black76.implied_vol(r["ask_n"], 1.0, r["k"], T, 1.0, call)
        out.append((r["strike"], lo, hi))
    return np.array(out, dtype=float).reshape(-1, 3)


def expiry_figure(asset, family, expiry, f, res_rows, path):
    fit, q, F, T = f["fit"], f["quotes"], f["F"], f["T"]
    fig, axes = plt.subplots(1, 3, figsize=(13.5, 4.0), constrained_layout=True)
    days = T * 365
    fig.suptitle(f"{asset} {family} options · expiry {expiry:%Y-%m-%d} 08:00 UTC ({days:.0f} days) · "
                 f"forward {_fmt_price(F)} · curve {fit.status}", x=0.01, ha="left", color=INK, fontsize=11)

    # 1) accepted quotes and fitted smile
    ax = axes[0]
    iv = _quote_ivs(q, T)
    if len(iv):
        ax.vlines(iv[:, 0], iv[:, 1] * 100, iv[:, 2] * 100, color=QUOTE, lw=2.0, label="accepted bid–ask (IV)")
    if fit.grid is not None:
        xk, fiv = fit.fitted_iv()
        ax.plot(xk * F, fiv * 100, color=S1, lw=2, label="full-chain fit")
    if np.isfinite(f["atm_iv"]):
        ax.axhline(f["atm_iv"] * 100, color=S2, lw=1.5, ls="--", label=f"ATM IV {f['atm_iv']:.0%} (Black-76)")
    ax.axvline(F, color=INK2, lw=0.8, ls=":")
    ax.set_title("Accepted quotes and fitted smile")
    ax.set_xlabel("strike (USD)"); ax.set_ylabel("implied volatility (%)")
    ax.legend(loc="upper center", fontsize=8)

    # 2) terminal density
    ax = axes[1]
    if fit.grid is not None:
        xk, dens = fit.density()
        unit = 1000.0 if F >= 100 else 1.0
        ax.fill_between(xk * F, dens / F * unit, color=S1, alpha=0.18, lw=0)
        ax.plot(xk * F, dens / F * unit, color=S1, lw=2, label="full-chain density")
        kk = np.linspace(xk[0], xk[-1], 300)
        sd = f["atm_iv"] * np.sqrt(T)
        if np.isfinite(sd):
            ln = np.exp(-(np.log(kk) + 0.5 * sd * sd) ** 2 / (2 * sd * sd)) / (kk * sd * np.sqrt(2 * np.pi))
            ax.plot(kk * F, ln / F * unit, color=S2, lw=1.5, ls="--", label="lognormal at ATM IV")
        ax.text(0.02, 0.97, f"uncovered mass below {_fmt_price(xk[0] * F)}: {fit.tail_below:.1%}\n"
                f"uncovered mass above {_fmt_price(xk[-1] * F)}: {fit.tail_above:.1%}",
                transform=ax.transAxes, va="top", fontsize=8, color=INK2)
        ax.legend(loc="upper right", fontsize=8)
    ax.set_title("Risk-neutral terminal density (Q)")
    ax.set_xlabel("price at expiry (USD)")
    ax.set_ylabel("probability per $1,000" if F >= 100 else "probability per $1")
    ax.set_ylim(bottom=0)

    # 3) exceedance curve vs targets
    ax = axes[2]
    if fit.grid is not None:
        mids = 0.5 * (fit.grid[:-1] + fit.grid[1:])
        ax.plot(mids * F, -fit.slopes * 100, color=S1, lw=2, label="full-chain Q(S_T > K)")
        kk = np.linspace(mids[0], mids[-1], 300)
        if np.isfinite(f["atm_iv"]):
            ax.plot(kk * F, black76.exceedance_probability(1.0, kk, T, f["atm_iv"]) * 100, color=S2, lw=1.5,
                    ls="--", label="Black-76 constant ATM IV")
    up = res_rows[(res_rows["model"] == "full_chain_constrained") & (res_rows["event"] == "S_T > K")]
    absolute = up["target_label"].str.startswith("$")
    up = up[absolute] if absolute.any() else up   # chart one target family; the table carries both
    t = up[up["probability"].notna()].drop_duplicates("K")
    if len(t):
        yerr = np.vstack([(t["probability"] - t["lower"]).clip(lower=0), (t["upper"] - t["probability"]).clip(lower=0)]) * 100
        ax.errorbar(t["K"], t["probability"] * 100, yerr=yerr, fmt="o", ms=6, color=S1, mfc=SURFACE, mew=2,
                    elinewidth=1.2, capsize=3, label="targets (quote-feasible bounds)", zorder=5)
        for i, (_, r) in enumerate(t.sort_values("K").iterrows()):
            p = r["probability"]
            ax.annotate(f"{r['target_label']}  {'<1%' if p < 0.01 else f'{p:.0%}'}", (r["K"], p * 100),
                        textcoords="offset points", xytext=(6, 6 + 12 * (i % 2)), fontsize=7.5, color=INK2)
    un = up[up["probability"].isna()].drop_duplicates("K")
    if len(un):
        ax.text(0.98, 0.97, "unavailable (outside support):\n" + ", ".join(un["target_label"]),
                transform=ax.transAxes, ha="right", va="top", fontsize=8, color=INK2)
    ax.set_title("Exceedance probability vs target (Q, market-implied)")
    ax.set_xlabel("target strike K (USD)"); ax.set_ylabel("Q(S_T > K), %")
    ax.set_ylim(0, 100)
    ax.legend(loc="center right", fontsize=8)
    fig.savefig(path, dpi=150)
    plt.close(fig)


def overview_figure(asset, family, fits, path):
    fig, ax = plt.subplots(figsize=(7.5, 4.2), constrained_layout=True)
    colors = [S1, S2, S3]
    for c, (expiry, f) in zip(colors, sorted(fits.items())):
        fit = f["fit"]
        if fit.grid is None:
            continue
        mids = 0.5 * (fit.grid[:-1] + fit.grid[1:])
        ax.plot(mids * f["F"], -fit.slopes * 100, color=c, lw=2,
                label=f"{expiry:%Y-%m-%d} ({f['T'] * 365:.0f}d)")
    spot = next(iter(fits.values()))["spot"]
    ax.axvline(spot, color=INK2, lw=0.8, ls=":")
    ax.text(spot, 3, f" spot {_fmt_price(spot)}", fontsize=8, color=INK2, va="bottom")
    ax.set_title(f"{asset} {family}: market-implied Q(S_T > K) by expiry (fitted support only)")
    ax.set_xlabel("strike K (USD)"); ax.set_ylabel("Q(S_T > K), %")
    ax.set_ylim(0, 100)
    ax.legend(title="expiry", fontsize=8, title_fontsize=8)
    fig.savefig(path, dpi=150)
    plt.close(fig)


def make_all(all_fits, results: pd.DataFrame, rdir: Path, cfg) -> list[str]:
    cdir = rdir / "charts"
    cdir.mkdir(exist_ok=True)
    files = []
    for (asset, family), fits in all_fits.items():
        role = results.loc[(results["asset"] == asset) & (results["family"] == family), "role"]
        if len(role) and role.iat[0] != "primary":
            continue
        p = cdir / f"{asset}_{family}_overview.png"
        overview_figure(asset, family, fits, p)
        files.append(str(p.relative_to(rdir)))
        for expiry, f in fits.items():
            rr = results[(results["asset"] == asset) & (results["family"] == family)
                         & (results["expiry_utc"] == expiry)]
            p = cdir / f"{asset}_{family}_{expiry:%Y%m%d}.png"
            expiry_figure(asset, family, expiry, f, rr, p)
            files.append(str(p.relative_to(rdir)))
    return files
