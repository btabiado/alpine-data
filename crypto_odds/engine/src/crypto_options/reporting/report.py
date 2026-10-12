"""Markdown run report (brief s.10): what the market prices, what history supports, what is unknown."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd


def pct(p):
    if p is None or not np.isfinite(p):
        return "—"
    if p < 0.01:
        return "<1%"
    if p > 0.99:
        return ">99%"
    return f"{p:.0%}"


def band(lo, hi):
    if not (np.isfinite(lo) and np.isfinite(hi)):
        return "—"
    return f"{pct(lo)}–{pct(hi)}"


def money(x):
    if not np.isfinite(x):
        return "—"
    return f"${x:,.0f}" if x >= 100 else f"${x:,.2f}"


def _md_table(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    lines = ["| " + " | ".join(str(c) for c in cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for _, r in df.iterrows():
        lines.append("| " + " | ".join(str(r[c]) for c in cols) + " |")
    return "\n".join(lines)


def _short_status(status: str, support: str) -> str:
    if not status.startswith("unavailable"):
        return status
    if "outside accepted strike range" in status:
        return f"unavailable: outside quoted strikes ({support})"
    if "fewer than" in status:
        return "unavailable: too few quoted strikes around target"
    if "apart" in status:
        return "unavailable: gap between quoted strikes too wide"
    return status


def _asset_table(res, asset, family):
    r = res[(res["asset"] == asset) & (res["family"] == family)]
    fc = r[r["model"] == "full_chain_constrained"].drop_duplicates(["expiry_utc", "target_label"])
    bm = r[r["model"] == "black76_constant_atm_iv"].drop_duplicates(["expiry_utc", "target_label"])
    bm = bm.set_index(["expiry_utc", "target_label"])["probability"]
    hz = (r.groupby("expiry_utc")["horizon"].apply(lambda s: " / ".join(dict.fromkeys(s)))
          if len(r) else pd.Series(dtype=str))
    rows = []
    for _, x in fc.sort_values(["expiry_utc", "K"]).iterrows():
        key = (x["expiry_utc"], x["target_label"])
        status = _short_status(x["target_status"], x.get("strike_support", ""))
        b76 = pct(bm.get(key, np.nan))
        if not np.isfinite(x["probability"]) and b76 != "—":
            b76 += " †"
        rows.append({
            "Expiry (08:00 UTC)": f"{x['expiry_utc']:%Y-%m-%d} ({x['days']:.0f}d)",
            "Horizon": hz.get(x["expiry_utc"], ""),
            "Target": x["target_label"], "K": money(x["K"]), "Event": x["event"].replace("S_T", "S<sub>T</sub>"),
            "Q full-chain": pct(x["probability"]),
            "Quote-feasible range": band(x["lower"], x["upper"]),
            "Q Black-76 (ATM IV)": b76,
            "Quality": status,
            "Validation": "unvalidated Q; history n/a",
        })
    return pd.DataFrame(rows)


def write_report(rdir: Path) -> str:
    rdir = Path(rdir)
    man = json.loads((rdir / "manifest.json").read_text())
    res = pd.read_parquet(rdir / "results.parquet")
    diag = pd.read_csv(rdir / "fit_diagnostics.csv", parse_dates=["expiry_utc"]) if (rdir / "fit_diagnostics.csv").exists() else pd.DataFrame()
    asof = pd.Timestamp(man["as_of_utc"])
    ny = asof.tz_convert(man["config"]["display_timezone"])
    L = []
    L.append(f"# Crypto options: market-implied terminal probabilities\n")
    L.append(f"**As of:** {asof:%Y-%m-%d %H:%M} UTC ({ny:%Y-%m-%d %H:%M} {ny.tzname()}) · **Run:** `{man['run_id']}` · "
             f"**Venue:** Deribit public API · **Source revision:** `{man['source_revision']}` · "
             f"**Code hash:** `{man.get('code_sha256', '')[:12]}`\n")
    L.append("> **What these numbers are.** Every probability below is **Q, the risk-neutral probability implied by "
             "option prices** on the event *settlement price at expiry is above (or below) K*. It is what the market "
             "prices, including any risk premium. It is **not** a validated real-world forecast; historical "
             "calibration (Stage 3) has not run yet. The range column shows every probability consistent with the "
             "accepted bid/ask quotes and no-arbitrage. That is quote uncertainty, not a statistical confidence "
             "interval. \"—\" means unavailable, never 0%.\n")

    # coverage
    L.append("## Coverage\n")
    cov = []
    for c in man["chains"]:
        cov.append({"Asset": c["asset"], "Chain": c.get("family") or "—", "Role": c.get("role") or "—",
                    "Status": c["status"], "Accepted OTM quotes": c.get("n_accepted_otm", "—"),
                    "Note": (c.get("error") or "")[:110]})
    L.append(_md_table(pd.DataFrame(cov)) + "\n")
    dp = [d for d in man.get("depeg_check", []) if d.get("deviation") is not None]
    if dp:
        L.append("USD vs USDC index check (linear options settle in USDC): " + "; ".join(
            f"{d['asset']} {d['deviation']:+.3%}{' **FLAG**' if d['flag'] else ''}" for d in dp
            if d.get("deviation") is not None and np.isfinite(d["deviation"])) + ".\n")

    # horizons
    hz = res.dropna(subset=["horizon"]).drop_duplicates(["asset", "family", "horizon"])
    if len(hz):
        h0 = hz[hz["role"] == "primary"].drop_duplicates("horizon")
        L.append("## Horizons\n")
        L.append("Expiries are the actual listed contracts. The 30/60/90-day horizons use a strict ±7-day rule. "
                 "When no listed expiry qualifies, that horizon is reported missing. The nearest listed contract "
                 "is still shown, under its real date and days remaining, and is never relabeled as that horizon.\n")
        L.append(_md_table(pd.DataFrame({
            "Horizon": h0["horizon"], "Listed expiry used": [f"{e:%Y-%m-%d} ({d:.0f}d)" for e, d in zip(h0["expiry_utc"], h0["days"])],
            "Status": h0["horizon_status"]})) + "\n")

    # per-asset tables
    L.append("## Results by asset\n")
    for asset in man["config"]["assets"]:
        sub = res[res["asset"] == asset]
        prim = sub[sub["role"] == "primary"]
        if prim["probability"].notna().sum() == 0 and prim["family"].fillna("").eq("").all():
            why = prim["target_status"].iat[0].removeprefix("unavailable: ") if len(prim) else ""
            L.append(f"### {asset}\n\n**Unavailable.** {why}\n")
            continue
        fam = prim["family"].iat[0]
        spot = prim["spot_index"].dropna()
        L.append(f"### {asset} ({fam} options)\n")
        if len(spot):
            L.append(f"Settlement-index spot {money(spot.iat[0])}. Proportional targets are relative to that.\n")
        L.append(_md_table(_asset_table(res, asset, fam)))
        L.append("\nEvery row: probability type **Q** (risk-neutral, market-implied); models are the constrained "
                 "full-chain curve and the Black-76 constant-ATM-vol benchmark; the range is the model-free "
                 "quote-feasible range of the full-chain probability (bid/ask + no-arbitrage), not a confidence "
                 "interval; historical sample size n/a; validation status unvalidated. results.csv carries these "
                 "fields per row.\n\n† Full-chain value unavailable at this target (see Quality). The Black-76 "
                 "number is a single-volatility extrapolation that the quotes do not support. Reference only.\n")
        for p in sorted((rdir / "charts").glob(f"{asset}_{fam}_*.png")):
            L.append(f"![{p.stem}](charts/{p.name})")
        L.append("")

    # cross-check
    cc = res[res["role"] == "cross_check"]
    if len(cc):
        L.append("## Cross-check: inverse vs linear chains (same expiry, same target)\n")
        rows = []
        for (asset, fam), g in cc[cc["model"] == "full_chain_constrained"].groupby(["asset", "family"]):
            prim = res[(res["asset"] == asset) & (res["role"] == "primary") & (res["model"] == "full_chain_constrained")]
            m = g.merge(prim, on=["expiry_utc", "target_label"], suffixes=("_lin", "_inv")).drop_duplicates(["expiry_utc", "target_label"])
            m = m[m["probability_lin"].notna() & m["probability_inv"].notna()]
            if len(m):
                d = (m["probability_lin"] - m["probability_inv"]).abs()
                rows.append({"Asset": asset, "Comparisons": len(m), "Median |Δ|": f"{d.median():.1%}",
                             "Max |Δ|": f"{d.max():.1%}"})
        if rows:
            L.append(_md_table(pd.DataFrame(rows)) + "\n")
            L.append("Linear chains carry under 2% of inverse open interest. They are reported only to check the "
                     "inverse-premium conversion; the headline uses inverse.\n")

    sp = rdir / "sensitivity_spread_threshold.csv"
    if sp.exists():
        sens = pd.read_csv(sp, parse_dates=["expiry_utc"])
        if len(sens):
            b, a = sens["base_threshold"].iat[0], sens["alt_threshold"].iat[0]
            both = sens[sens["change"] == "both available"]
            new = sens[sens["change"] == "newly available"]
            L.append(f"## Sensitivity to the spread threshold ({b:.0%} vs {a:.0%})\n")
            L.append(f"Relaxing the maximum relative spread from {b:.0%} to {a:.0%} admits more wing quotes. "
                     f"Among the {len(both)} targets available both ways, the median change is "
                     f"{both['abs_diff'].median():.1%} and the largest is {both['abs_diff'].max():.1%}. "
                     f"{len(new)} targets become available only at the looser threshold. Those are wide-spread "
                     f"wing quotes, so treat them as exploratory:\n")
            if len(new):
                L.append(_md_table(pd.DataFrame({
                    "Chain": new["asset"] + " " + new["family"], "Expiry": new["expiry_utc"].dt.strftime("%Y-%m-%d"),
                    "Target": new["target_label"], f"Q at {a:.0%} threshold": new["probability_alt"].map(pct)})) + "\n")
            big = both.sort_values("abs_diff", ascending=False).head(5)
            if len(big) and big["abs_diff"].iat[0] >= 0.01:
                L.append("Largest moves among targets available both ways: " + "; ".join(
                    f"{r.asset} {r.expiry_utc:%Y-%m-%d} {r.target_label} {pct(r.probability_base)}→{pct(r.probability_alt)}"
                    for r in big.itertuples() if r.abs_diff >= 0.01) + ".\n")

    # diagnostics
    if len(diag):
        L.append("## Curve diagnostics\n")
        d = diag.copy()
        L.append(_md_table(pd.DataFrame({
            "Chain": d["asset"] + " " + d["family"], "Expiry": d["expiry_utc"].dt.strftime("%Y-%m-%d"),
            "Status": d["status"], "Quotes": d["n_quotes"], "ATM IV": d["atm_iv"].map(lambda v: f"{v:.1%}"),
            "Fit RMS (half-spreads)": d["rms_half_spreads"].map(lambda v: f"{v:.2f}"),
            "Max error": d["max_half_spreads"].map(lambda v: f"{v:.2f}"),
            "Accepted strikes": [f"{money(a)}–{money(b)}" for a, b in zip(d["strike_lo"], d["strike_hi"])],
            "Uncovered mass below/above": [f"{a:.1%} / {b:.1%}" for a, b in zip(d["uncovered_mass_below"], d["uncovered_mass_above"])],
        })) + "\n")
        L.append("Fit errors are in half-spreads: under 1 means the fitted price sits inside that quote's bid/ask. "
                 "Uncovered mass lies outside the accepted strikes. It is reported, not redistributed.\n")
    if (rdir / "parity_check.csv").exists():
        pc = pd.read_csv(rdir / "parity_check.csv")
        if len(pc):
            g = pc.groupby(["asset", "family"])["violation"].agg(["sum", "count"]).reset_index()
            L.append("Put-call parity on the forward (strikes with two-sided calls and puts): " + "; ".join(
                f"{r.asset} {r.family} {int(r['sum'])}/{int(r['count'])} outside combined spread" for _, r in g.iterrows()) + ".\n")

    # what is known / unknown
    L.append("## What this shows and what it does not\n")
    L.append("- **What the market prices:** the tables above, for listed expiries only, within quoted strike support.\n"
             "- **What historical evidence supports:** nothing yet. Historical option quotes are paid-only, so these Q "
             "values cannot be scored against outcomes until the snapshot collector has run long enough for "
             "expiries to settle, or an archive is licensed (research/STAGE1_COVERAGE.md).\n"
             "- **What remains unknown:** real-world (P) probabilities, how Q compares with realized outcomes, and "
             "anything outside the accepted strikes. LINK has no listed options, so it gets no market-implied curve.\n"
             "- Deribit gives no genuine last-quote timestamp, so quote age is *unknown* for every quote.\n")
    L.append(f"\nManifest: `manifest.json` (config hash `{man['config_sha256'][:12]}`, "
             f"{len(man['raw_inputs_sha256'])} raw inputs with SHA-256).\n")
    out = rdir / "report.md"
    out.write_text("\n".join(L))
    return str(out)
