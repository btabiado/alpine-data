"""
Rules-based "insights" engine.

Scans the dashboard payload and emits human-readable notable facts about
the latest state vs recent history. Deterministic, no API key, no LLM.

Each insight is:
    {
        "kind": "etf" | "signal" | "trend" | "anomaly" | "milestone",
        "asset": "btc" | "eth" | "link" | "global",
        "severity": "info" | "good" | "bad" | "alert",
        "headline": "Short bold sentence",
        "detail":   "1-2 sentence elaboration (optional)",
    }
"""

from __future__ import annotations

from typing import Any
from datetime import datetime, timedelta


# ----- helpers -----

def _safe(x, default=None):
    return x if x is not None else default


def _last(rows, n=1, key="flow"):
    if not rows:
        return None
    if n == 1:
        return rows[-1].get(key)
    return [r.get(key) for r in rows[-n:]]


def _streak(values, sign: str) -> int:
    """Length of trailing run of values with the given sign ('pos' or 'neg')."""
    if not values:
        return 0
    n = 0
    for v in reversed(values):
        if v is None:
            break
        if sign == "pos" and v > 0:
            n += 1
        elif sign == "neg" and v < 0:
            n += 1
        else:
            break
    return n


def _zscore(values, window=30):
    if not values or len(values) < window + 1:
        return None
    import statistics
    sample = [v for v in values[-window - 1:-1] if v is not None]
    if len(sample) < 5:
        return None
    mu = statistics.mean(sample)
    sd = statistics.pstdev(sample) or 1e-9
    return (values[-1] - mu) / sd


def _largest_in_window(values, window=30) -> bool:
    if not values or len(values) < 2:
        return False
    tail = values[-window:]
    return values[-1] == max(tail)


def _smallest_in_window(values, window=30) -> bool:
    if not values or len(values) < 2:
        return False
    tail = values[-window:]
    return values[-1] == min(tail)


def _fmt_usd(n) -> str:
    if n is None:
        return "?"
    sign = "-" if n < 0 else ""
    a = abs(n)
    if a >= 1000:
        return f"{sign}${a/1000:.2f}B"
    if a >= 1:
        return f"{sign}${a:.1f}M"
    return f"{sign}${a*1000:.0f}K"


# ----- per-domain insight generators -----

def _etf_insights(payload: dict, asset: str) -> list[dict]:
    out: list[dict] = []
    a = payload.get(asset) or {}
    daily = a.get("daily") or []
    if not daily:
        return out
    stats = a.get("stats") or {}
    flows = [d["flow"] for d in daily]
    last_date = daily[-1]["date"]
    last_flow = daily[-1]["flow"]

    # 1. Last day directional flow
    if last_flow != 0:
        cls = "good" if last_flow > 0 else "bad"
        out.append({
            "kind": "etf",
            "asset": asset,
            "severity": cls,
            "headline": f"{asset.upper()} ETF {'inflow' if last_flow > 0 else 'outflow'} of {_fmt_usd(abs(last_flow))} on {last_date}",
            "detail": None,
        })

    # 2. Streak
    pos = _streak(flows, "pos")
    neg = _streak(flows, "neg")
    if pos >= 5:
        out.append({
            "kind": "trend", "asset": asset, "severity": "good",
            "headline": f"{asset.upper()} ETF on {pos}-day inflow streak",
            "detail": f"Sum over the streak: {_fmt_usd(sum(flows[-pos:]))}",
        })
    elif neg >= 5:
        out.append({
            "kind": "trend", "asset": asset, "severity": "bad",
            "headline": f"{asset.upper()} ETF on {neg}-day outflow streak",
            "detail": f"Sum over the streak: {_fmt_usd(sum(flows[-neg:]))}",
        })

    # 3. Largest day in last 30 / 90
    if abs(last_flow) >= 1 and _largest_in_window([abs(f) for f in flows], 30):
        out.append({
            "kind": "milestone", "asset": asset,
            "severity": "good" if last_flow > 0 else "bad",
            "headline": f"{asset.upper()}'s biggest single-day move in 30+ days",
            "detail": f"{_fmt_usd(last_flow)} vs prior 30-day max",
        })

    # 4. Z-score anomaly vs 30d
    z = _zscore(flows, 30)
    if z is not None and abs(z) >= 2:
        cls = "good" if z > 0 else "bad"
        out.append({
            "kind": "anomaly", "asset": asset, "severity": cls,
            "headline": f"{asset.upper()} ETF flow {z:+.1f}σ vs 30-day mean",
            "detail": f"That's a {'large positive' if z > 0 else 'large negative'} outlier.",
        })

    # 5. Cumulative milestones
    all_time = stats.get("all_time")
    if all_time is not None:
        for thresh in (10_000, 25_000, 50_000, 75_000, 100_000):
            if all_time >= thresh:
                # crossed if previous cumulative was below
                if len(daily) >= 2 and daily[-2].get("cumulative", 0) < thresh <= daily[-1].get("cumulative", 0):
                    out.append({
                        "kind": "milestone", "asset": asset, "severity": "good",
                        "headline": f"{asset.upper()} ETF crossed ${thresh/1000:.0f}B cumulative inflows",
                        "detail": f"As of {last_date}",
                    })

    # 6. 7-day and 30-day rollups
    sum7 = sum(flows[-7:])
    sum30 = sum(flows[-30:])
    if abs(sum7) >= 500:
        out.append({
            "kind": "etf", "asset": asset,
            "severity": "good" if sum7 > 0 else "bad",
            "headline": f"{asset.upper()} ETF last 7d: {_fmt_usd(sum7)}",
            "detail": f"30d cumulative: {_fmt_usd(sum30)}",
        })

    # 7. Top-fund driver for the day (if per-fund data exists)
    by_fund_daily = a.get("by_fund_daily") or {}
    if by_fund_daily:
        last_per_fund = []
        for fund, series in by_fund_daily.items():
            if series and series[-1].get("date") == last_date:
                last_per_fund.append((fund, series[-1].get("flow") or 0))
        if last_per_fund:
            last_per_fund.sort(key=lambda x: abs(x[1]), reverse=True)
            top = last_per_fund[0]
            if abs(top[1]) >= 10:
                out.append({
                    "kind": "etf", "asset": asset,
                    "severity": "good" if top[1] > 0 else "bad",
                    "headline": f"{asset.upper()} top mover today: {top[0]} {_fmt_usd(top[1])}",
                    "detail": None,
                })
    return out


def _signal_insights(payload: dict) -> list[dict]:
    out: list[dict] = []
    sigs = payload.get("signals") or {}
    for asset, sig in sigs.items():
        if not sig:
            continue
        label = sig.get("label", "")
        score = sig.get("score", 0)
        if label in ("STRONG BUY", "STRONG SELL"):
            out.append({
                "kind": "signal", "asset": asset,
                "severity": "good" if "BUY" in label else "alert",
                "headline": f"{asset.upper()} composite signal: {label} (score {score:+d})",
                "detail": "Driven by " + ", ".join(c["name"] for c in (sig.get("components") or [])[:3] if c.get("contribution", 0) != 0),
            })
        # Detect direction flips in history (last 2 days)
        hist = sig.get("history") or []
        if len(hist) >= 2:
            prev, last = hist[-2]["score"], hist[-1]["score"]
            if prev <= 0 < last:
                out.append({
                    "kind": "signal", "asset": asset, "severity": "good",
                    "headline": f"{asset.upper()} signal flipped positive ({prev:+d} → {last:+d})",
                    "detail": None,
                })
            elif prev >= 0 > last:
                out.append({
                    "kind": "signal", "asset": asset, "severity": "bad",
                    "headline": f"{asset.upper()} signal flipped negative ({prev:+d} → {last:+d})",
                    "detail": None,
                })
    return out


def _market_insights(payload: dict) -> list[dict]:
    out: list[dict] = []
    market = payload.get("market") or {}
    fng = market.get("fear_greed") or []
    if fng:
        last = fng[-1]
        v = last.get("value")
        if v is not None:
            if v <= 25:
                out.append({"kind":"anomaly","asset":"global","severity":"good",
                    "headline": f"Fear & Greed at {v} — extreme fear (contrarian buy zone)",
                    "detail": last.get("label","")})
            elif v >= 75:
                out.append({"kind":"anomaly","asset":"global","severity":"alert",
                    "headline": f"Fear & Greed at {v} — extreme greed (contrarian caution)",
                    "detail": last.get("label","")})

    # Funding flips
    for asset in ("btc", "eth", "link"):
        a = (market.get(asset) or {})
        funding = a.get("funding") or []
        if len(funding) >= 2:
            last = funding[-1].get("rate", 0)
            prev = funding[-2].get("rate", 0)
            if prev > 0 and last < 0:
                out.append({"kind":"trend","asset":asset,"severity":"good",
                    "headline": f"{asset.upper()} funding flipped negative ({last*100:.4f}%)",
                    "detail": "Bearish positioning — contrarian setup."})
            elif prev < 0 and last > 0:
                out.append({"kind":"trend","asset":asset,"severity":"info",
                    "headline": f"{asset.upper()} funding flipped positive ({last*100:.4f}%)",
                    "detail": None})
        # DVOL crush / spike
        dvol = a.get("dvol") or []
        if len(dvol) >= 31:
            vals = [r["dvol"] for r in dvol if r.get("dvol") is not None]
            z = _zscore(vals, 30)
            if z is not None:
                if z <= -1.5:
                    out.append({"kind":"anomaly","asset":asset,"severity":"good",
                        "headline": f"{asset.upper()} DVOL crushed ({z:+.1f}σ vs 30d mean)",
                        "detail": "Implied vol historically low — long-vol setup."})
                elif z >= 1.5:
                    out.append({"kind":"anomaly","asset":asset,"severity":"alert",
                        "headline": f"{asset.upper()} DVOL spike ({z:+.1f}σ vs 30d mean)",
                        "detail": "Implied vol elevated — caution."})

    # ETH/BTC ratio extremes
    ethbtc = market.get("ethbtc") or []
    if len(ethbtc) >= 60:
        vals = [r["value"] for r in ethbtc]
        last = vals[-1]
        m6 = min(vals[-180:]) if len(vals) >= 180 else min(vals)
        x6 = max(vals[-180:]) if len(vals) >= 180 else max(vals)
        if last <= m6 * 1.005:
            out.append({"kind":"anomaly","asset":"global","severity":"info",
                "headline": f"ETH/BTC at ~6-month low ({last:.5f})",
                "detail": None})
        elif last >= x6 * 0.995:
            out.append({"kind":"anomaly","asset":"global","severity":"info",
                "headline": f"ETH/BTC at ~6-month high ({last:.5f})",
                "detail": None})
    return out


def build_insights(payload: dict, limit: int = 12) -> list[dict]:
    """Top-level entry. Returns up to `limit` insights, prioritised."""
    out: list[dict] = []
    out += _etf_insights(payload, "btc")
    out += _etf_insights(payload, "eth")
    out += _signal_insights(payload)
    out += _market_insights(payload)

    # Prioritise: milestones + anomalies first, then ETF, then trends, then signals, then info
    rank = {
        ("milestone", "good"): 1, ("milestone", "bad"): 1, ("milestone", "alert"): 1,
        ("anomaly", "good"): 2,   ("anomaly", "alert"): 2, ("anomaly", "bad"): 2,
        ("etf", "good"): 3,        ("etf", "bad"): 3,
        ("signal", "good"): 4,     ("signal", "bad"): 4, ("signal", "alert"): 4,
        ("trend", "good"): 5,      ("trend", "bad"): 5,
    }
    out.sort(key=lambda r: rank.get((r["kind"], r["severity"]), 9))
    return out[:limit]
