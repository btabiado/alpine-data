"""Normalized quote table and quality rules (brief s.3, s.4).

Price normalization. Every option price is expressed as an undiscounted fraction of its
matching forward F, so calls and puts across inverse and linear families share one scale:

    linear (USDC per 1 unit):        x_norm = premium_usdc / F
    inverse (coin per 1 coin):       x_norm = premium_coin
        because Deribit marks inverse options as premium_coin = Black76_undiscounted(F, K) / F
        (research/stage1_deribit.md, Note 5); the coin payoff max(S-K,0)/S is worth max(S-K,0) USD.

Deribit applies no discounting (interest_rate = 0 on every option), so D = 1 is recorded with
its source. In normalized units the forward is 1 and the strike is k = K / F.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd

from ..pricing.black76 import SECONDS_PER_YEAR


def _tick(inst: dict, price: float) -> float:
    tick = inst.get("tick_size")
    for step in inst.get("tick_size_steps") or []:
        if price is not None and np.isfinite(price) and price > step["above_price"]:
            tick = step["tick_size"]
    return tick


def _num(x):
    return float(x) if x is not None else np.nan


def normalize_chain(snap, venue: str = "deribit") -> pd.DataFrame:
    inst_by_name = {i["instrument_name"]: i for i in snap.instruments}
    rows = []
    for b in snap.summaries:
        inst = inst_by_name[b["instrument_name"]]
        F = _num(b.get("underlying_price"))
        bid, ask, mark = _num(b.get("bid_price")), _num(b.get("ask_price")), _num(b.get("mark_price"))
        bid = np.nan if bid == 0 else bid
        ask = np.nan if ask == 0 else ask
        mid = (bid + ask) / 2 if np.isfinite(bid) and np.isfinite(ask) else np.nan
        tick = _tick(inst, mid if np.isfinite(mid) else mark)
        scale = 1.0 if snap.family == "inverse" else (1.0 / F if F and F > 0 else np.nan)
        expiry = pd.Timestamp(inst["expiration_timestamp"], unit="ms", tz="UTC")
        retrieved = pd.Timestamp(snap.retrieved_at)
        rows.append({
            "venue": venue, "instrument": b["instrument_name"], "asset": snap.asset,
            "family": snap.family, "role": snap.role,
            "quote_currency": inst.get("quote_currency"), "settlement_currency": inst.get("settlement_currency"),
            "multiplier": inst.get("contract_size"),
            "payoff_type": "inverse_coin_settled" if snap.family == "inverse" else "linear_cash_settled",
            "exercise": "european",
            "quote_ts": pd.NaT,  # Deribit supplies no genuine last-quote-update time
            "response_ts": pd.Timestamp(b["creation_timestamp"], unit="ms", tz="UTC"),
            "retrieval_ts": retrieved, "expiry_ts": expiry,
            "T": (expiry - retrieved).total_seconds() / SECONDS_PER_YEAR,
            "strike": float(inst["strike"]), "cp": "C" if inst["option_type"] == "call" else "P",
            "bid": bid, "ask": ask, "mid": mid, "mark": mark, "mark_iv": _num(b.get("mark_iv")) / 100.0,
            "index_price": _num(b.get("estimated_delivery_price")), "index_source": inst.get("price_index"),
            "forward": F, "forward_source": b.get("underlying_index"),
            "discount_factor": 1.0, "discount_source": "deribit_convention_interest_rate_0",
            "open_interest": _num(b.get("open_interest")), "oi_units": inst.get("base_currency"),
            "volume_24h": _num(b.get("volume")), "volume_units": f"{inst.get('base_currency')} (24h)",
            "tick": tick, "tick_norm": tick * scale,
            "bid_n": bid * scale, "ask_n": ask * scale, "mid_n": mid * scale, "mark_n": mark * scale,
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["k"] = df["strike"] / df["forward"]
    return df


def apply_quality(df: pd.DataFrame, qcfg: dict) -> pd.DataFrame:
    """Add flags, an exclusion reason (first hard failure) and `accepted` for the primary fit."""
    if df.empty:
        return df.assign(flags=[], excluded_reason=[], accepted=[], otm=[])
    df = df.copy()
    flags = [[] for _ in range(len(df))]
    reason = [None] * len(df)

    def hard(mask, why):
        for i in np.flatnonzero(mask):
            if reason[i] is None:
                reason[i] = why

    def soft(mask, why):
        for i in np.flatnonzero(mask):
            flags[i].append(why)

    vals = df[["bid", "ask", "mark", "forward"]].to_numpy(dtype=float)
    hard(df["T"].to_numpy() <= 0, "expired_or_nonpositive_time")
    hard(~np.isfinite(df["forward"].to_numpy()) | (df["forward"].to_numpy() <= 0), "nonpositive_forward")
    hard(np.any(vals[:, :2] < 0, axis=1), "negative_price")
    two_sided = np.isfinite(df["bid"].to_numpy()) & np.isfinite(df["ask"].to_numpy())
    hard(two_sided & (df["bid"].to_numpy() > df["ask"].to_numpy()), "bid_above_ask")
    hard(~two_sided, "one_sided_or_no_quote")

    k = df["k"].to_numpy()
    is_call = (df["cp"] == "C").to_numpy()
    mid_n = df["mid_n"].to_numpy()
    lower = np.where(is_call, np.maximum(1 - k, 0), np.maximum(k - 1, 0))
    upper = np.where(is_call, 1.0, k)
    hard(two_sided & ((mid_n <= lower) | (mid_n >= upper)), "outside_no_arbitrage_bounds")

    spread = (df["ask"] - df["bid"]).to_numpy()
    rel = spread / df["mid"].to_numpy()
    df["rel_spread"] = rel
    wide = two_sided & (rel > qcfg["max_relative_spread"])
    tick_ok = spread <= qcfg["tiny_premium_max_ticks"] * df["tick"].to_numpy() + 1e-12
    hard(wide & ~tick_ok, "relative_spread_above_max")
    soft(wide & tick_ok, "wide_relative_spread_within_tick_rule")
    soft(df["quote_ts"].isna().to_numpy(), "quote_age_unknown")
    has_ts = df["quote_ts"].notna().to_numpy()
    if has_ts.any():  # only genuine last-update timestamps are aged; response times never are
        age = (df["retrieval_ts"] - df["quote_ts"]).dt.total_seconds().to_numpy()
        hard(has_ts & (age > qcfg["max_quote_age_s"]), "stale_quote")

    df["otm"] = np.where(is_call, k >= 1.0, k < 1.0)
    df["flags"] = [";".join(f) for f in flags]
    df["excluded_reason"] = reason
    df["accepted"] = [r is None for r in reason] & df["otm"].to_numpy()
    return df


def parity_check(df: pd.DataFrame) -> pd.DataFrame:
    """Normalized put-call parity C - P - (1 - k) against combined half-spreads, per strike."""
    ok = df[df["excluded_reason"].isna()]
    piv = ok.pivot_table(index=["expiry_ts", "strike", "k"], columns="cp",
                         values=["mid_n", "bid_n", "ask_n"], aggfunc="first")
    if piv.empty or ("mid_n", "C") not in piv.columns or ("mid_n", "P") not in piv.columns:
        return pd.DataFrame(columns=["expiry_ts", "strike", "k", "gap", "tolerance", "violation"])
    piv = piv.dropna()
    k = piv.index.get_level_values("k").to_numpy()
    gap = piv[("mid_n", "C")] - piv[("mid_n", "P")] - (1 - k)
    tol = (piv[("ask_n", "C")] - piv[("bid_n", "C")] + piv[("ask_n", "P")] - piv[("bid_n", "P")]) / 2
    out = pd.DataFrame({"gap": gap.to_numpy(), "tolerance": tol.to_numpy()}, index=piv.index).reset_index()
    out["violation"] = out["gap"].abs() > out["tolerance"]
    return out


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
