"""Stage 2 analysis: expiry selection, per-expiry curve fits, target table (brief s.5, s.10)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .pricing import black76
from .pricing.chain_fit import atm_iv, fit_chain, quote_feasible_bounds, target_support

PROB_TYPE = "Q (risk-neutral, market-implied)"
VALIDATION = "unvalidated: current market-implied Q, not a real-world forecast"


def select_expiries(expiries, asof: pd.Timestamp, hcfg: dict) -> list[dict]:
    """Nearest listed expiry per horizon (strict tolerance decides scoring eligibility) + year-end."""
    exps = sorted({pd.Timestamp(e) for e in expiries if pd.Timestamp(e) > asof})
    if not exps:
        return []
    days = np.array([(e - asof).total_seconds() / 86400 for e in exps])
    picks = []
    for h in hcfg["days"]:
        i = int(np.argmin(np.abs(days - h)))
        within = abs(days[i] - h) <= hcfg["tolerance_days"]
        picks.append({"horizon": f"{h}d" if within else f"nearest to {h}d (not a {h}d result)",
                      "expiry": exps[i], "days": days[i], "within_tolerance": bool(within),
                      "horizon_status": "matched" if within else
                      f"{h}d missing: nearest listed expiry is {days[i]:.0f}d, outside ±{hcfg['tolerance_days']}d"})
    if hcfg.get("include_year_end"):
        ye = [e for e in exps if e.year == asof.year]
        if ye:
            e = ye[-1]
            picks.append({"horizon": "year-end", "expiry": e, "days": (e - asof).total_seconds() / 86400,
                          "within_tolerance": True, "horizon_status": f"last {asof.year} expiry"})
        else:
            picks.append({"horizon": "year-end", "expiry": None, "days": np.nan, "within_tolerance": False,
                          "horizon_status": f"missing (no remaining {asof.year} expiry)"})
    return picks


def call_equivalent_quotes(df_exp: pd.DataFrame) -> pd.DataFrame:
    """Accepted OTM quotes as normalized call prices (puts via parity c = p + 1 - k, D = 1)."""
    acc = df_exp[df_exp["accepted"]].copy()
    conv = np.where(acc["cp"] == "P", 1 - acc["k"], 0.0)
    acc["c_bid"] = acc["bid_n"] + conv
    acc["c_ask"] = acc["ask_n"] + conv
    return acc.sort_values("k").drop_duplicates("k")


def ask_only_quotes(df_exp: pd.DataFrame) -> pd.DataFrame:
    """OTM quotes with an ask but no bid: boundary information (price <= ask), never a midpoint."""
    a = df_exp[df_exp["otm"] & (df_exp["excluded_reason"] == "one_sided_or_no_quote")
               & df_exp["bid"].isna() & df_exp["ask"].notna()].copy()
    a["c_ask"] = a["ask_n"] + np.where(a["cp"] == "P", 1 - a["k"], 0.0)
    return a.sort_values("k").drop_duplicates("k")


def targets_for(asset: str, spot: float, tcfg: dict) -> list[dict]:
    out = []
    for K in tcfg["absolute"].get(asset, []):
        # absolute targets keep one event definition regardless of where spot is today
        out.append({"target_label": f"${K:,.2f}", "K": float(K), "event": "S_T > K"})
    for p in tcfg["proportional_up"]:
        out.append({"target_label": f"+{p:.0%}", "K": spot * (1 + p), "event": "S_T > K"})
    for p in tcfg["proportional_down"]:
        out.append({"target_label": f"{p:.0%}", "K": spot * (1 + p), "event": "S_T < K"})
    return out


def analyze_chain(df: pd.DataFrame, cfg: dict, asof: pd.Timestamp):
    """Fit selected expiries of one asset/family chain. Returns (fits, result rows)."""
    asset, family, role = df["asset"].iat[0], df["family"].iat[0], df["role"].iat[0]
    picks = select_expiries(df["expiry_ts"].unique(), asof, cfg["horizons"])
    fits, rows = {}, []
    by_expiry: dict = {}
    for p in picks:
        if p["expiry"] is not None:
            by_expiry.setdefault(p["expiry"], []).append(p)

    for expiry, plist in by_expiry.items():
        dfe = df[df["expiry_ts"] == expiry]
        F = float(dfe["forward"].median())
        spot = float(dfe["index_price"].median())
        T = float(dfe["T"].iat[0])
        q = call_equivalent_quotes(dfe)
        ao = ask_only_quotes(dfe)
        ao = ao[~ao["k"].isin(q["k"])]
        fit = fit_chain(q["k"].to_numpy(), q["c_bid"].to_numpy(), q["c_ask"].to_numpy(), T,
                        cfg["fit"], cfg["quality"], q["tick_norm"].to_numpy(),
                        ao["k"].to_numpy(), ao["c_ask"].to_numpy())
        sigma_atm = atm_iv(q["k"].to_numpy(), 0.5 * (q["c_bid"] + q["c_ask"]).to_numpy(), T) if len(q) else np.nan
        fits[expiry] = {"fit": fit, "quotes": q, "ask_only": ao, "F": F, "spot": spot, "T": T,
                        "atm_iv": sigma_atm, "chain": dfe}
        fmt = (lambda v: f"${v:,.0f}") if F >= 100 else (lambda v: f"${v:,.2f}")
        support = f"{fmt(q['strike'].min())}–{fmt(q['strike'].max())}" if len(q) else "none"
        for p in plist:
            for t in targets_for(asset, spot, cfg["targets"]):
                rows.extend(_target_rows(asset, family, role, p, expiry, F, spot, T, sigma_atm, fit, t,
                                         support, cfg["quality"]))
    return fits, rows


def _target_rows(asset, family, role, pick, expiry, F, spot, T, sigma_atm, fit, t, support, qcfg):
    k = t["K"] / F
    below = t["event"] == "S_T < K"
    base = {
        "asset": asset, "venue": "deribit", "family": family, "role": role,
        "spot_index": spot, "forward": F, "horizon": pick["horizon"], "horizon_status": pick["horizon_status"],
        "scoring_eligible": pick["within_tolerance"], "expiry_utc": expiry, "days": T * 365,
        "target_label": t["target_label"], "K": t["K"], "event": t["event"],
        "probability_type": PROB_TYPE, "strike_support": support, "curve_status": fit.status,
        "curve_reasons": "; ".join(fit.reasons), "historical_sample_size": "n/a (current market)",
        "validation_status": VALIDATION,
    }
    rows = []
    # Black-76 constant-ATM-vol benchmark
    if np.isfinite(sigma_atm):
        q = float(black76.exceedance_probability(1.0, k, T, sigma_atm))
        ok, why = target_support(fit, k, qcfg)
        rows.append({**base, "model": "black76_constant_atm_iv", "atm_iv": sigma_atm,
                     "probability": 1 - q if below else q, "uncertainty_kind": "none (single-vol benchmark)",
                     "lower": np.nan, "upper": np.nan, "target_status": "benchmark" if ok else
                     f"benchmark; target {why}"})
    # constrained full-chain curve
    ok, why = target_support(fit, k, qcfg)
    if fit.status == "unavailable":
        ok, why = False, "curve unavailable: " + "; ".join(fit.reasons)
    if ok:
        q = float(fit.survival(k))
        lo, hi = quote_feasible_bounds(fit, k)
        sens = [float(np.interp(k, 0.5 * (fit.grid[:-1] + fit.grid[1:]), -s)) for s in fit.alt_fits.values()]
        prob, lo2, hi2 = (1 - q, 1 - hi, 1 - lo) if below else (q, lo, hi)
        sl, sh = ((1 - max(sens), 1 - min(sens)) if below else (min(sens), max(sens))) if sens else (np.nan, np.nan)
        status = fit.status
        if np.isfinite(lo2) and not (lo2 - 1e-3 <= prob <= hi2 + 1e-3):
            status = "degraded"
            base = {**base, "curve_reasons": (base["curve_reasons"] + "; " if base["curve_reasons"] else "")
                    + "point estimate outside quote-feasible bounds"}
        rows.append({**base, "model": "full_chain_constrained", "atm_iv": sigma_atm, "probability": prob,
                     "uncertainty_kind": "quote-feasible bounds (bid/ask + no-arbitrage), not a confidence interval",
                     "lower": lo2, "upper": hi2, "smoothing_sensitivity_lo": sl, "smoothing_sensitivity_hi": sh,
                     "target_status": status})
    else:
        rows.append({**base, "model": "full_chain_constrained", "atm_iv": sigma_atm, "probability": np.nan,
                     "uncertainty_kind": "", "lower": np.nan, "upper": np.nan,
                     "target_status": f"unavailable: {why}"})
    return rows


def unavailable_asset_rows(asset: str, reason: str) -> list[dict]:
    return [{"asset": asset, "venue": "none", "family": "", "role": "primary", "model": "full_chain_constrained",
             "probability_type": PROB_TYPE, "probability": np.nan, "curve_status": "unavailable",
             "target_status": f"unavailable: {reason}", "validation_status": VALIDATION}]


def depeg_check(index_prices: dict, cfg: dict) -> list[dict]:
    out = []
    thr = cfg["venues"]["deribit"]["depeg_flag_threshold"]
    for asset, (usd, usdc) in cfg["venues"]["deribit"]["depeg_pairs"].items():
        a, b = index_prices.get(usd, {}).get("price"), index_prices.get(usdc, {}).get("price")
        if a and b:
            dev = b / a - 1
            out.append({"asset": asset, "usd_index": usd, "usdc_index": usdc, "usd": a, "usdc": b,
                        "deviation": dev, "flag": abs(dev) > thr,
                        "assumption": "USDC treated as USD for linear options; flagged if |deviation| > "
                                      f"{thr:.1%}"})
        else:
            out.append({"asset": asset, "usd_index": usd, "usdc_index": usdc, "deviation": np.nan,
                        "flag": None, "assumption": "index price unavailable"})
    return out
