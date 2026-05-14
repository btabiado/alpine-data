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


def _is_fresh(date_str: str | None, max_age_days: int = 14) -> bool:
    """True if `date_str` (YYYY-MM-DD) is within `max_age_days` of today.

    Returns False on parse failure or missing input so callers fail closed and
    avoid emitting insights from clearly stale data.
    """
    if not date_str:
        return False
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return False
    return (datetime.utcnow() - d) <= timedelta(days=max_age_days)


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
    # Freshness guard: if the latest ETF row is more than 14 days old, do not
    # emit "fresh" insights — but keep cumulative milestones, which describe
    # the cumulative total (still meaningful even if the last row is stale).
    fresh = _is_fresh(last_date, max_age_days=14)

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

    # 8. ETF flow pace: latest 7d vs 90d-rolling 7d average. Helps fill the
    #    ETF tab on quiet days when no single rule above triggers.
    if fresh and len(flows) >= 90:
        sum7_recent = sum(flows[-7:])
        # 90-day rolling 7-day windows (exclude the current window so we
        # compare against history, not against the window we're flagging).
        history = flows[-90:-7] if len(flows) >= 97 else flows[:-7]
        if len(history) >= 14:
            # Average absolute 7d sum across overlapping windows.
            windows = [sum(history[i:i+7]) for i in range(len(history) - 6)]
            avg_abs = sum(abs(w) for w in windows) / max(len(windows), 1)
            if avg_abs > 0 and abs(sum7_recent) >= 2.0 * avg_abs and abs(sum7_recent) >= 100:
                cls = "good" if sum7_recent > 0 else "bad"
                out.append({
                    "kind": "anomaly", "asset": asset, "severity": cls,
                    "headline": f"{asset.upper()} ETF flow pace 2x the 90-day average ({_fmt_usd(sum7_recent)} last 7d)",
                    "detail": f"90-day rolling 7d avg: {_fmt_usd(avg_abs)}",
                })

    # 9. Top-fund concentration: when a single fund drives ≥60% of today's
    # net flow and the net flow is meaningful (≥$100M abs).
    if fresh and by_fund_daily and abs(last_flow) >= 100:
        last_per_fund_signed = []
        for fund, series in by_fund_daily.items():
            if series and series[-1].get("date") == last_date:
                last_per_fund_signed.append((fund, series[-1].get("flow") or 0))
        if last_per_fund_signed:
            # Find fund with largest |flow|.
            top_abs = max(last_per_fund_signed, key=lambda x: abs(x[1]))
            if last_flow != 0 and abs(top_abs[1]) / abs(last_flow) >= 0.6:
                pct = abs(top_abs[1]) / abs(last_flow) * 100.0
                out.append({
                    "kind": "etf", "asset": asset,
                    "severity": "good" if last_flow > 0 else "bad",
                    "headline": f"{asset.upper()} ETF: {top_abs[0]} drove {pct:.0f}% of today's net flow",
                    "detail": f"{top_abs[0]} {_fmt_usd(top_abs[1])} vs net {_fmt_usd(last_flow)}.",
                })

    # 10. Outflow leader: the single fund bleeding the worst today (≤ −$25M).
    # Skip the not-noteworthy case where net flow is positive AND the worst
    # fund is only mildly negative (between -25 and 0).
    if fresh and by_fund_daily:
        last_per_fund_signed = []
        for fund, series in by_fund_daily.items():
            if series and series[-1].get("date") == last_date:
                last_per_fund_signed.append((fund, series[-1].get("flow") or 0))
        if last_per_fund_signed:
            worst = min(last_per_fund_signed, key=lambda x: x[1])
            worst_flow = worst[1]
            # Threshold: ≤ -25; if net flow positive AND worst > -25, skip.
            if worst_flow <= -25 and not (last_flow > 0 and worst_flow > -25):
                out.append({
                    "kind": "etf", "asset": asset, "severity": "bad",
                    "headline": f"{asset.upper()} ETF: {worst[0]} leads outflows at {_fmt_usd(worst_flow)}",
                    "detail": f"Net flow today: {_fmt_usd(last_flow)}.",
                })

    # 11. New all-time cumulative high: today's cumulative > max of all prior
    # cumulative values. Reports even on stale data (it's a milestone).
    cumulative_series = [d.get("cumulative") for d in daily if d.get("cumulative") is not None]
    if len(cumulative_series) >= 2:
        last_cum = cumulative_series[-1]
        prior_max = max(cumulative_series[:-1])
        if last_cum is not None and last_cum > prior_max:
            out.append({
                "kind": "milestone", "asset": asset, "severity": "good",
                "headline": f"{asset.upper()} ETF cumulative hits new all-time high: {_fmt_usd(last_cum)}",
                "detail": f"Prior peak: {_fmt_usd(prior_max)} as of {last_date}.",
            })

    # 12. Month-over-month aggregate: last 30 days vs prior 30 days. Trigger
    # when same-sign and ≥2× in abs terms, with the recent window ≥$1B.
    if fresh and len(flows) >= 60:
        curr_30 = sum(flows[-30:])
        prev_30 = sum(flows[-60:-30])
        same_sign = (curr_30 > 0 and prev_30 > 0) or (curr_30 < 0 and prev_30 < 0)
        if same_sign and abs(curr_30) >= 1000 and abs(prev_30) > 0 and abs(curr_30) >= 2 * abs(prev_30):
            direction = "accelerated" if curr_30 > 0 else "deepened"
            sev = "good" if curr_30 > 0 else "bad"
            out.append({
                "kind": "trend", "asset": asset, "severity": sev,
                "headline": f"{asset.upper()} ETF: month-over-month flows {direction} sharply ({_fmt_usd(prev_30)} → {_fmt_usd(curr_30)})",
                "detail": "30d vs prior 30d net flows.",
            })

    # 13. Flow vs price divergence: 7d flow strongly diverges from 7d price.
    # Threshold: |7d flow| ≥ $300M and 7d price change ≥5% in the opposite
    # direction. Pulls price series from payload["market"][asset]["price"].
    if fresh and len(flows) >= 7:
        price_rows = (((payload.get("market") or {}).get(asset) or {}).get("price")) or []
        # Need at least 8 points to compute a 7-day change.
        if len(price_rows) >= 8:
            sum7_flow = sum(flows[-7:])
            p_now = price_rows[-1].get("value")
            p_then = price_rows[-8].get("value")
            if p_now and p_then and p_then > 0 and abs(sum7_flow) >= 300:
                price_pct = (p_now / p_then - 1) * 100.0
                # Positive flow + price down ≥5%, OR negative flow + price up ≥5%.
                if (sum7_flow >= 300 and price_pct <= -5) or (sum7_flow <= -300 and price_pct >= 5):
                    flow_dir = "inflows" if sum7_flow > 0 else "outflows"
                    price_dir = "fell" if price_pct < 0 else "rose"
                    out.append({
                        "kind": "anomaly", "asset": asset, "severity": "alert",
                        "headline": f"{asset.upper()} ETF flow vs price divergence: {flow_dir} {_fmt_usd(abs(sum7_flow))} 7d while price {price_dir} {price_pct:+.1f}%",
                        "detail": "Flows and spot are pointing in opposite directions — sentiment dislocation.",
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

        # Near-extreme signal score (|60| ≤ |score| < |75|). The STRONG
        # threshold is |75|; this fills the Signals tab on days where no asset
        # has tipped over but momentum is building.
        if label not in ("STRONG BUY", "STRONG SELL") and isinstance(score, (int, float)):
            mag = abs(int(score))
            if 60 <= mag < 75:
                direction = "STRONG BUY" if score > 0 else "STRONG SELL"
                out.append({
                    "kind": "signal", "asset": asset,
                    "severity": "good" if score > 0 else "alert",
                    "headline": f"{asset.upper()} signal near {direction} zone (score {score:+d})",
                    "detail": f"Within 15 pts of the {direction} threshold — watch for follow-through.",
                })

    # Signal component standout: of all assets with a non-trivial score, pick
    # the one with the largest |score| and call out its top-magnitude
    # contributing component. This always produces something on quiet days
    # provided any signal data exists.
    candidates = []
    for asset, sig in sigs.items():
        if not sig:
            continue
        score = sig.get("score")
        comps = sig.get("components") or []
        if score is None or not comps:
            continue
        if abs(score) < 20:
            continue
        # Find the highest |contribution| component.
        top_comp = max(
            (c for c in comps if c.get("contribution") not in (None, 0)),
            key=lambda c: abs(c.get("contribution") or 0),
            default=None,
        )
        if top_comp is None:
            continue
        candidates.append((abs(score), asset, score, top_comp))
    if candidates:
        candidates.sort(reverse=True)
        _, asset, score, top_comp = candidates[0]
        contrib = top_comp.get("contribution") or 0
        name = top_comp.get("name") or "component"
        out.append({
            "kind": "signal", "asset": asset,
            "severity": "good" if contrib > 0 else "bad",
            "headline": f"{asset.upper()} signal driven by {name} contribution {contrib:+d}",
            "detail": f"Largest single driver of the {score:+d} composite score.",
        })
    return out


def _whale_insights(payload: dict) -> list[dict]:
    """BTC on-chain whale-tab rules. Operates on payload['whale']['btc'].

    Each rule guards for empty/missing series. Freshness is not strictly
    enforced here because blockchain.info is occasionally a day or two
    behind — the trends still describe the most recent state we have.
    """
    out: list[dict] = []
    btc = (payload.get("whale") or {}).get("btc") or {}

    def _vals(series_name: str) -> list[float]:
        rows = btc.get(series_name) or []
        return [r.get("value") for r in rows if r.get("value") is not None]

    addrs = _vals("active_addresses")
    tx_count = _vals("tx_count")
    tx_vol = _vals("tx_volume_usd")
    avg_tx = _vals("avg_tx_usd")
    miners = _vals("miners_revenue_usd")

    addrs_high = _largest_in_window(addrs, 30) if len(addrs) >= 2 else False
    addrs_low = _smallest_in_window(addrs, 30) if len(addrs) >= 2 else False
    txc_high = _largest_in_window(tx_count, 30) if len(tx_count) >= 2 else False
    txc_low = _smallest_in_window(tx_count, 30) if len(tx_count) >= 2 else False
    vol_low = _smallest_in_window(tx_vol, 30) if len(tx_vol) >= 2 else False

    # 1. Active addresses 30d high
    if addrs_high and addrs:
        last = addrs[-1]
        out.append({
            "kind": "milestone", "asset": "btc", "severity": "good",
            "headline": f"BTC active addresses at 30-day high ({last:,.0f})",
            "detail": "Network participation surging.",
        })

    # 2. Active addresses 30d low
    if addrs_low and addrs:
        last = addrs[-1]
        out.append({
            "kind": "anomaly", "asset": "btc", "severity": "bad",
            "headline": f"BTC active addresses at 30-day low ({last:,.0f}) — network engagement weak",
            "detail": None,
        })

    # 3. Average transaction size spike (≥1.5σ above 30d mean) — whale proxy.
    if len(avg_tx) >= 31:
        z = _zscore(avg_tx, 30)
        if z is not None and z >= 1.5:
            out.append({
                "kind": "anomaly", "asset": "btc", "severity": "info",
                "headline": f"BTC avg transaction size {z:+.1f}σ vs 30d — big-money on-chain",
                "detail": f"Latest avg tx: {_fmt_usd((avg_tx[-1] or 0) / 1e6)}.",
            })

    # 4. Miner revenue spike (≥2σ above 30d mean).
    if len(miners) >= 31:
        z = _zscore(miners, 30)
        if z is not None and z >= 2:
            last = miners[-1] or 0
            out.append({
                "kind": "anomaly", "asset": "btc", "severity": "good",
                "headline": f"BTC miner revenue {z:+.1f}σ vs 30d ({_fmt_usd(last / 1e6)}M today)",
                "detail": "Miners earning more — supports network security and may ease sell-pressure.",
            })

    # 5. Combined network momentum: tx_count AND active_addresses both at 30d highs.
    if txc_high and addrs_high:
        out.append({
            "kind": "milestone", "asset": "btc", "severity": "good",
            "headline": "BTC network momentum strong: both tx count and active addresses at 30-day highs",
            "detail": "Broad-based on-chain engagement.",
        })

    # 6. On-chain quiet day: tx_count + active_addresses + tx_volume all at 30d lows.
    if txc_low and addrs_low and vol_low:
        out.append({
            "kind": "trend", "asset": "btc", "severity": "info",
            "headline": "BTC on-chain unusually quiet: tx count, active addresses, and volume all at 30-day lows",
            "detail": "Network engagement low — could indicate consolidation or holiday lull.",
        })

    # 7. Network velocity spike: tx_volume_usd / active_addresses (per day)
    # ≥1.5σ above its 30d mean — outsized USD movement per active address.
    vol_rows = btc.get("tx_volume_usd") or []
    addr_rows = btc.get("active_addresses") or []
    # Align by date so we don't divide mismatched offsets if one series is
    # shorter than the other.
    addr_by_date = {r.get("date"): r.get("value") for r in addr_rows if r.get("date") is not None}
    velocity: list[float] = []
    for r in vol_rows:
        v = r.get("value")
        a = addr_by_date.get(r.get("date"))
        if v is None or a is None or a == 0:
            continue
        velocity.append(v / a)
    if len(velocity) >= 31:
        z = _zscore(velocity, 30)
        if z is not None and z >= 1.5:
            out.append({
                "kind": "anomaly", "asset": "btc", "severity": "info",
                "headline": f"BTC network velocity {z:+.1f}σ vs 30d — outsized USD per active address",
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

    # ETH gas oracle (Etherscan v2)
    gas = market.get("eth_gas") or {}
    base = gas.get("base_fee_gwei")
    if base is not None:
        if base >= 50:
            out.append({"kind":"anomaly","asset":"eth","severity":"alert",
                "headline": f"ETH gas spike: base fee {base:.0f} gwei",
                "detail": f"Fast: {gas.get('fast_gwei','?')} gwei. Network is congested."})
        elif base <= 1:
            out.append({"kind":"trend","asset":"eth","severity":"info",
                "headline": f"ETH gas near zero ({base:.2f} gwei)",
                "detail": "Quiet mainnet — cheap to transact, but low activity."})

    # BTC mempool fees (mempool.space)
    mp = market.get("mempool") or {}
    fees = mp.get("fees_sat_vb") or {}
    fastest = fees.get("fastestFee")
    if fastest is not None:
        if fastest >= 100:
            out.append({"kind":"anomaly","asset":"btc","severity":"alert",
                "headline": f"BTC mempool congested: {fastest} sat/vB fastest fee",
                "detail": "Heavy on-chain demand."})
        elif fastest <= 2:
            out.append({"kind":"trend","asset":"btc","severity":"info",
                "headline": f"BTC mempool quiet ({fastest} sat/vB)",
                "detail": None})

    # BTC hashrate trend
    hr = mp.get("hashrate_daily_eh") or []
    if len(hr) >= 30:
        last = hr[-1].get("value")
        prior = [r.get("value") for r in hr[-30:] if r.get("value")]
        if last and prior and last == max(prior):
            out.append({"kind":"milestone","asset":"btc","severity":"good",
                "headline": f"BTC hashrate at 30-day high ({last:.0f} EH/s)",
                "detail": "Miners committing more compute — bullish security."})

    # Stablecoin supply 7d delta (DeFiLlama) — proxy for "dry powder" coming in/out
    llama = market.get("defillama") or {}
    delta = llama.get("stablecoin_7d_change_usd")
    if delta is not None:
        d_b = delta / 1e9  # billions
        if abs(d_b) >= 0.5:
            out.append({"kind":"trend","asset":"global",
                "severity": "good" if d_b > 0 else "bad",
                "headline": f"Stablecoin supply {'+' if d_b > 0 else ''}${d_b:.2f}B over the last 7d",
                "detail": f"Total stablecoin mcap: ${(llama.get('stablecoin_mcap_usd') or 0)/1e9:.1f}B. Rising stablecoins = buying power building up."})
    # DeFi DEX volume snapshot
    dex24 = llama.get("dex_volume_24h_usd")
    fees24 = llama.get("fees_24h_usd")
    if dex24 and dex24 >= 5e9:
        out.append({"kind":"info","asset":"global","severity":"info",
            "headline": f"DEX 24h volume: ${dex24/1e9:.2f}B  ·  protocol fees: ${(fees24 or 0)/1e6:.1f}M",
            "detail": None})

    # Trending coin (CoinGecko search) — retail attention proxy
    trending = market.get("trending") or []
    if trending:
        top = trending[0]
        sym = (top.get("symbol") or "").upper()
        name = top.get("name") or ""
        rank = top.get("rank")
        # Filter out BTC/ETH (we already cover them) and anything in top-10 (not really "trending")
        if sym and sym not in ("BTC", "ETH") and (rank is None or rank > 10):
            out.append({"kind": "trend", "asset": "global", "severity": "info",
                "headline": f"{sym} ({name}) is trending #1 on CoinGecko",
                "detail": (f"Current market cap rank: {rank}" if rank else "Retail search interest spike — watch for follow-through.")})

    # DEX hot pool (GeckoTerminal) — top trending pool by volume with big move
    gt = market.get("geckoterminal") or {}
    gt_trending = gt.get("trending_pools") or []
    for pool in gt_trending[:5]:  # only top-5 by volume considered
        vol = pool.get("volume_24h_usd") or 0
        ch = pool.get("change_24h_pct")
        if vol >= 50_000_000 and ch is not None and ch >= 30:
            out.append({
                "kind": "trend", "asset": "global", "severity": "info",
                "headline": f"DEX hot pool: {pool.get('name','?')} on {pool.get('network','?')} {ch:+.0f}% with {_fmt_usd(vol/1e6)}M volume",
                "detail": f"DEX: {pool.get('dex','?')} · 24h tx: {pool.get('transactions_24h',0):,}",
            })
            break  # one such insight is enough

    # BTC difficulty adjustment — miner economics
    diff_adj = (market.get("mempool_extra") or {}).get("difficulty_adjustment") or {}
    change_pct = diff_adj.get("difficulty_change_pct")
    days = (diff_adj.get("remaining_time_ms") or 0) / 86400000.0
    if change_pct is not None and 0 < days < 5:
        if abs(change_pct) >= 4:
            sev = "alert" if change_pct > 0 else "good"
            direction = "harder" if change_pct > 0 else "easier"
            out.append({"kind": "milestone", "asset": "btc", "severity": sev,
                "headline": f"BTC difficulty retarget in ~{days:.1f} days: {change_pct:+.1f}% ({direction} for miners)",
                "detail": ("Miners likely under pressure — watch for distribution." if change_pct > 0 else "Miners get a break — less sell-side pressure.")})

    # Mining pool centralization risk
    pools = (market.get("mempool_extra") or {}).get("pools") or {}
    top2 = pools.get("top2_concentration_pct")
    if top2 is not None and top2 >= 55:
        out.append({"kind": "anomaly", "asset": "btc", "severity": "alert",
            "headline": f"BTC mining concentration high: top 2 pools = {top2:.1f}% of blocks",
            "detail": "Theoretical 51% attack risk if both colluded."})

    # DeFi TVL by chain - flag big movers
    chains = ((market.get("defi") or {}).get("chains")) or []
    for c in chains[:10]:
        change_1d = c.get("change_1d_pct")
        if change_1d is not None and abs(change_1d) >= 4:
            sev = "good" if change_1d > 0 else "bad"
            out.append({"kind": "trend", "asset": "global", "severity": sev,
                "headline": f"{c.get('name')} TVL {'+'if change_1d>0 else ''}{change_1d:.1f}% today (${(c.get('tvl_usd') or 0)/1e9:.1f}B)",
                "detail": f"Top-10 chain by TVL — significant 1d move."})
            break  # one chain insight at most

    # Latest news headline (top story summary)
    news = market.get("news") or []
    if news and news[0].get("title"):
        out.append({"kind": "info", "asset": "global", "severity": "info",
            "headline": f"📰 {news[0]['source']}: {news[0]['title'][:100]}",
            "detail": None})

    # FRED macro overlay (DXY / SP500 / Gold / 10Y) — only if user supplied a key
    fred = market.get("fred") or {}
    if fred.get("available"):
        # DXY 1d change ≥1%
        dxy = fred.get("dxy") or []
        if len(dxy) >= 2:
            last = dxy[-1].get("value")
            prev = dxy[-2].get("value")
            if last and prev:
                ch = (last - prev) / prev * 100.0
                if abs(ch) >= 1.0:
                    out.append({
                        "kind": "trend", "asset": "global",
                        "severity": "bad" if ch > 0 else "good",
                        "headline": f"DXY {ch:+.1f}% today — typically inverse to risk assets including crypto",
                        "detail": f"Broad dollar index at {last:.2f}.",
                    })

        # 10Y Treasury yield thresholds
        tnx = fred.get("treasury_10y") or []
        if len(tnx) >= 2:
            last_y = tnx[-1].get("value")
            prev_y = tnx[-2].get("value")
            if last_y is not None and prev_y is not None:
                for thresh in (4.5, 5.0):
                    crossed_up = prev_y < thresh <= last_y
                    crossed_dn = prev_y >= thresh > last_y
                    if crossed_up or crossed_dn:
                        out.append({
                            "kind": "milestone", "asset": "global",
                            "severity": "alert" if crossed_up else "info",
                            "headline": f"10Y Treasury yield {'crossed above' if crossed_up else 'fell below'} {thresh:.1f}% ({last_y:.2f}%)",
                            "detail": "Higher yields pressure risk assets; lower yields ease conditions.",
                        })
                        break

        # Gold new 30-day high
        gold = fred.get("gold") or []
        if len(gold) >= 2:
            vals = [r.get("value") for r in gold if r.get("value") is not None]
            if vals:
                tail = vals[-30:] if len(vals) >= 30 else vals
                if vals[-1] == max(tail) and len(tail) >= 5:
                    out.append({
                        "kind": "milestone", "asset": "global", "severity": "info",
                        "headline": f"Gold at 30-day high (${vals[-1]:,.2f}/oz)",
                        "detail": "Often a hedge/risk-off signal — monitor BTC correlation.",
                    })

        # S&P 500 1d drop ≥2%
        spx = fred.get("sp500") or []
        if len(spx) >= 2:
            last_s = spx[-1].get("value")
            prev_s = spx[-2].get("value")
            if last_s and prev_s and prev_s > 0:
                ch = (last_s - prev_s) / prev_s * 100.0
                if ch <= -2.0:
                    out.append({
                        "kind": "anomaly", "asset": "global", "severity": "bad",
                        "headline": f"S&P 500 {ch:.1f}% today — risk-off may pressure crypto",
                        "detail": f"Index at {last_s:,.0f}.",
                    })

    # ----- Markets tab: top-25 movers + BTC dominance + total market cap -----
    # These fire more frequently than the FRED macro rules so the Markets
    # insights bar isn't empty on calm macro days.

    markets_top = market.get("markets_top") or []

    # Top-25 24h gainer (≥5%): surface the leader so user knows which top-cap
    # name is hot today. Skip stables (symbols ending in USD).
    if markets_top:
        gainers_24h = [c for c in markets_top
                       if (c.get("change_24h_pct") is not None
                           and c.get("change_24h_pct") >= 5
                           and not (c.get("symbol") or "").upper().endswith("USD"))]
        if gainers_24h:
            top = max(gainers_24h, key=lambda c: c.get("change_24h_pct") or 0)
            out.append({
                "kind": "trend", "asset": "global", "severity": "good",
                "headline": f"Top-25 24h gainer: {top.get('symbol','?')} {top.get('change_24h_pct'):+.1f}% (rank #{top.get('rank','?')})",
                "detail": (top.get("name") or "") + f" — {_fmt_usd((top.get('market_cap_usd') or 0)/1e6)} mcap.",
            })

        # Top-25 24h loser (≤-5%): same but for biggest drop
        losers_24h = [c for c in markets_top
                      if (c.get("change_24h_pct") is not None
                          and c.get("change_24h_pct") <= -5
                          and not (c.get("symbol") or "").upper().endswith("USD"))]
        if losers_24h:
            worst = min(losers_24h, key=lambda c: c.get("change_24h_pct") or 0)
            out.append({
                "kind": "trend", "asset": "global", "severity": "bad",
                "headline": f"Top-25 24h loser: {worst.get('symbol','?')} {worst.get('change_24h_pct'):+.1f}% (rank #{worst.get('rank','?')})",
                "detail": (worst.get("name") or "") + f" — {_fmt_usd((worst.get('market_cap_usd') or 0)/1e6)} mcap.",
            })

        # Top-25 7d gainer (≥15%)
        gainers_7d = [c for c in markets_top
                      if (c.get("change_7d_pct") is not None
                          and c.get("change_7d_pct") >= 15
                          and not (c.get("symbol") or "").upper().endswith("USD"))]
        if gainers_7d:
            top = max(gainers_7d, key=lambda c: c.get("change_7d_pct") or 0)
            out.append({
                "kind": "trend", "asset": "global", "severity": "good",
                "headline": f"Top-25 7d momentum: {top.get('symbol','?')} {top.get('change_7d_pct'):+.1f}% week (rank #{top.get('rank','?')})",
                "detail": (top.get("name") or "") + " — sustained breakout.",
            })

        # Top-25 7d loser (≤-15%)
        losers_7d = [c for c in markets_top
                     if (c.get("change_7d_pct") is not None
                         and c.get("change_7d_pct") <= -15
                         and not (c.get("symbol") or "").upper().endswith("USD"))]
        if losers_7d:
            worst = min(losers_7d, key=lambda c: c.get("change_7d_pct") or 0)
            out.append({
                "kind": "trend", "asset": "global", "severity": "bad",
                "headline": f"Top-25 7d laggard: {worst.get('symbol','?')} {worst.get('change_7d_pct'):+.1f}% week (rank #{worst.get('rank','?')})",
                "detail": (worst.get("name") or "") + " — sustained drawdown.",
            })

    # BTC dominance threshold crossings (50% / 55% / 60% / 65%)
    g = market.get("global") or {}
    btc_d = g.get("btc_dominance")
    if btc_d is not None:
        # We don't track yesterday's dominance, so just flag standout regimes.
        if btc_d >= 60:
            out.append({
                "kind": "milestone", "asset": "global", "severity": "info",
                "headline": f"BTC dominance high: {btc_d:.1f}% — alt season unlikely",
                "detail": "Capital concentrated in BTC vs alts.",
            })
        elif btc_d <= 45:
            out.append({
                "kind": "milestone", "asset": "global", "severity": "info",
                "headline": f"BTC dominance low: {btc_d:.1f}% — alt rotation in play",
                "detail": "Capital diversifying out of BTC.",
            })

    # Total crypto market cap milestones ($3T / $4T / $5T)
    total_mcap = g.get("total_market_cap_usd")
    if total_mcap is not None and total_mcap > 0:
        t = total_mcap / 1e12
        for thresh in (5.0, 4.0, 3.0):
            if t >= thresh:
                # Only emit the highest threshold crossed (avoid stacking)
                out.append({
                    "kind": "milestone", "asset": "global", "severity": "good",
                    "headline": f"Total crypto market cap above ${thresh:.0f}T (now ${t:.2f}T)",
                    "detail": "Asset class scale milestone.",
                })
                break

    # ----- Whale tab: BTC on-chain transfer volume + active-address swings -----
    whale = (payload.get("whale") or {}).get("btc") or {}

    # Whale tx volume spike (≥2σ above 30d mean)
    tx_vol = whale.get("tx_volume_usd") or []
    if len(tx_vol) >= 31:
        last_row = tx_vol[-1] or {}
        if _is_fresh(last_row.get("date"), max_age_days=14):
            vals = [r.get("value") for r in tx_vol if r.get("value") is not None]
            z = _zscore(vals, 30)
            if z is not None and z >= 2.0:
                last_v = vals[-1]
                out.append({
                    "kind": "anomaly", "asset": "btc", "severity": "alert",
                    "headline": f"BTC on-chain transfer volume spike: Whale tx volume {z:+.1f}σ vs 30d mean",
                    "detail": f"Latest day: {_fmt_usd(last_v / 1e6)} (USD). Heavy on-chain movement.",
                })

    # Active addresses anomaly (≥1.5σ either direction)
    addrs = whale.get("active_addresses") or []
    if len(addrs) >= 31:
        last_row = addrs[-1] or {}
        if _is_fresh(last_row.get("date"), max_age_days=14):
            vals = [r.get("value") for r in addrs if r.get("value") is not None]
            z = _zscore(vals, 30)
            if z is not None and abs(z) >= 1.5:
                sev = "good" if z > 0 else "bad"
                last_v = vals[-1]
                out.append({
                    "kind": "anomaly", "asset": "btc", "severity": sev,
                    "headline": f"BTC active addresses {z:+.1f}σ vs 30d",
                    "detail": f"Latest day: ~{int(last_v):,} addresses. "
                              f"{'Surging' if z > 0 else 'Falling'} network participation.",
                })

    # ----- Markets tab: traditional indices intraday moves -----
    yi = market.get("yahoo_indices") or {}

    def _series_1d_change(idx_dict: dict | None) -> tuple[float | None, str | None]:
        if not idx_dict:
            return None, None
        series = idx_dict.get("series_90d") or []
        if len(series) < 2:
            return None, None
        last = series[-1].get("value")
        prev = series[-2].get("value")
        if not last or not prev:
            return None, None
        return (last / prev - 1) * 100.0, idx_dict.get("latest_date") or series[-1].get("date")

    # NASDAQ 1d move
    ch_nas, date_nas = _series_1d_change(yi.get("nasdaq"))
    if ch_nas is not None and abs(ch_nas) >= 1.5 and _is_fresh(date_nas, max_age_days=7):
        out.append({
            "kind": "trend", "asset": "global",
            "severity": "good" if ch_nas > 0 else "bad",
            "headline": f"NASDAQ {ch_nas:+.2f}% on the day",
            "detail": f"Risk-on tech tape — typically correlated with crypto majors.",
        })

    # Dow Jones 1d move
    ch_dji, date_dji = _series_1d_change(yi.get("dow"))
    if ch_dji is not None and abs(ch_dji) >= 1.5 and _is_fresh(date_dji, max_age_days=7):
        out.append({
            "kind": "trend", "asset": "global",
            "severity": "good" if ch_dji > 0 else "bad",
            "headline": f"Dow Jones {ch_dji:+.2f}% on the day",
            "detail": "Broad US large-cap industrial bellwether.",
        })

    # VIX threshold crossings (20 = calm↔fear, 30 = fear↔panic)
    vix = yi.get("vix") or {}
    vix_series = vix.get("series_90d") or []
    if len(vix_series) >= 2:
        last_v = vix_series[-1].get("value")
        prev_v = vix_series[-2].get("value")
        last_d = vix.get("latest_date") or vix_series[-1].get("date")
        if last_v is not None and prev_v is not None and _is_fresh(last_d, max_age_days=7):
            for thresh, calm_label, fear_label in (
                (20.0, "calm", "fear"),
                (30.0, "fear", "panic"),
            ):
                crossed_up = prev_v < thresh <= last_v
                crossed_dn = prev_v >= thresh > last_v
                if crossed_up:
                    out.append({
                        "kind": "milestone", "asset": "global", "severity": "alert",
                        "headline": f"VIX crossed above {thresh:.0f} ({last_v:.1f}) — {calm_label}→{fear_label}",
                        "detail": "Volatility regime shift higher; risk assets typically wobble.",
                    })
                    break
                if crossed_dn:
                    out.append({
                        "kind": "milestone", "asset": "global", "severity": "good",
                        "headline": f"VIX fell below {thresh:.0f} ({last_v:.1f}) — {fear_label}→{calm_label}",
                        "detail": "Volatility regime cooling; supportive for risk-on.",
                    })
                    break

    # ----- Trading tab: Open Interest and Long/Short crowding -----
    for asset in ("btc", "eth", "link"):
        a = market.get(asset) or {}

        # Open interest surge (≥1.5σ above 30d mean). The cached series uses
        # `oi_usd` keyed rows; tolerate the prompt-spec `oi` field too.
        oi_rows = a.get("open_interest_usd") or a.get("open_interest") or []
        if len(oi_rows) >= 31:
            last_row = oi_rows[-1] or {}
            if _is_fresh(last_row.get("date"), max_age_days=14):
                vals = [
                    (r.get("oi_usd") if r.get("oi_usd") is not None else r.get("oi"))
                    for r in oi_rows
                ]
                vals = [v for v in vals if v is not None]
                z = _zscore(vals, 30) if len(vals) >= 31 else None
                if z is not None and z >= 1.5:
                    out.append({
                        "kind": "anomaly", "asset": asset, "severity": "alert",
                        "headline": f"{asset.upper()} open interest {z:+.1f}σ above 30d mean",
                        "detail": f"Latest OI: {_fmt_usd((vals[-1] or 0) / 1e6)} — leverage building, watch for squeezes.",
                    })

        # Long/Short ratio extremes
        ls_rows = a.get("long_short_ratio") or a.get("long_short") or []
        if ls_rows:
            last_row = ls_rows[-1] or {}
            ratio = last_row.get("ratio")
            if ratio is not None and _is_fresh(last_row.get("date"), max_age_days=14):
                if ratio >= 2.5:
                    out.append({
                        "kind": "anomaly", "asset": asset, "severity": "alert",
                        "headline": f"{asset.upper()} L/S ratio crowded long ({ratio:.2f})",
                        "detail": "Contrarian: heavy long positioning often precedes long-squeezes.",
                    })
                elif ratio <= 0.7:
                    out.append({
                        "kind": "anomaly", "asset": asset, "severity": "good",
                        "headline": f"{asset.upper()} L/S ratio crowded short ({ratio:.2f})",
                        "detail": "Contrarian: heavy short positioning often precedes short-squeezes.",
                    })

    # ----- DeFi tab extras: second-place chain mover + protocol mover -----
    # `chains[:10]` mover loop above breaks on the FIRST big mover. Catch a
    # second-place mover here so the DeFi tab gets two chain insights when
    # multiple chains are moving.
    chains_extra = ((market.get("defi") or {}).get("chains")) or []
    if chains_extra:
        # Re-scan, skip the index that emitted above (first |change_1d|≥4).
        first_emitter = None
        for idx, c in enumerate(chains_extra[:10]):
            ch = c.get("change_1d_pct")
            if ch is not None and abs(ch) >= 4:
                first_emitter = idx
                break
        if first_emitter is not None:
            for idx, c in enumerate(chains_extra[:10]):
                if idx == first_emitter:
                    continue
                ch = c.get("change_1d_pct")
                if ch is not None and abs(ch) >= 4:
                    sev = "good" if ch > 0 else "bad"
                    out.append({
                        "kind": "trend", "asset": "global", "severity": sev,
                        "headline": f"{c.get('name')} TVL {'+'if ch>0 else ''}{ch:.1f}% today (${(c.get('tvl_usd') or 0)/1e9:.1f}B)",
                        "detail": "Second-place 1d chain mover in the top-10 by TVL.",
                    })
                    break

    # Top DeFi protocol mover (≥10% 1d change)
    protocols = ((market.get("defi") or {}).get("protocols")) or []
    if protocols:
        # Sort by |change_1d_pct| descending, ignoring None.
        ranked = sorted(
            (p for p in protocols if p.get("change_1d_pct") is not None),
            key=lambda p: abs(p.get("change_1d_pct") or 0),
            reverse=True,
        )
        if ranked:
            top = ranked[0]
            ch = top.get("change_1d_pct") or 0
            if abs(ch) >= 10:
                sev = "good" if ch > 0 else "bad"
                tvl_b = (top.get("tvl_usd") or 0) / 1e9
                out.append({
                    "kind": "trend", "asset": "global", "severity": sev,
                    "headline": f"{top.get('name')} TVL {'+' if ch > 0 else ''}{ch:.1f}% today (${tvl_b:.2f}B)",
                    "detail": f"Largest 1d protocol mover by % change. Category: {top.get('category') or '—'}.",
                })

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


# ----- per-tab classification -----
#
# Every insight is tagged with the dashboard tab it belongs to so the
# Insights bar can filter to "just the things relevant to what I'm looking
# at right now." The Overview tab is intentionally NOT in the mapping —
# Overview shows its own curated "Top insights" card from the global list,
# and the per-tab Insights bar is hidden when Overview is active.

VALID_TABS = {"etf", "signals", "trading", "markets", "defi", "whale"}


def _market_insight_tab(insight: dict) -> str:
    """Classify a market-generator insight to a single dashboard tab.

    Headlines are stable strings emitted by `_market_insights`; the matching
    is intentionally explicit so test_insights can catch drift if someone
    changes a headline without updating the mapping.
    """
    raw = insight.get("headline") or ""
    h = raw.lower()
    # Trading desk: sentiment, funding, IV, BTC↔ETH crosses, OI, L/S
    if "fear & greed" in h: return "trading"
    if "funding flipped" in h: return "trading"
    if "dvol" in h: return "trading"
    if "eth/btc" in h: return "trading"
    if "open interest" in h: return "trading"
    if "l/s ratio" in h: return "trading"
    # DeFi: gas, stablecoin supply, DEX volume, chain/protocol TVL
    if "gas" in h: return "defi"
    if "stablecoin supply" in h: return "defi"
    if "dex 24h" in h: return "defi"
    if "tvl" in h: return "defi"
    # Whale / on-chain: mempool, hashrate, difficulty, mining concentration,
    # on-chain transfer volume, active-address swings
    if "mempool" in h: return "whale"
    if "hashrate" in h: return "whale"
    if "difficulty retarget" in h: return "whale"
    if "mining concentration" in h: return "whale"
    if "whale tx volume" in h: return "whale"
    if "on-chain transfer volume" in h: return "whale"
    if "active addresses" in h: return "whale"
    # Markets / macro: traditional indices, news, trending tickers, source divergence
    if "dxy" in h: return "markets"
    if "10y treasury" in h: return "markets"
    if "gold at" in h: return "markets"
    if "s&p 500" in h: return "markets"
    if "nasdaq" in h: return "markets"
    if "dow jones" in h: return "markets"
    if "vix " in h or h.startswith("vix"): return "markets"
    if "📰" in raw: return "markets"
    if "trending #1" in h: return "markets"
    if "dex hot pool" in h: return "markets"
    # Top-25 movers + dominance + total mcap milestones
    if "top-25" in h: return "markets"
    if "btc dominance" in h: return "markets"
    if "total crypto market cap" in h: return "markets"
    # Default: anything else macro-flavoured lands in Markets.
    return "markets"


def build_insights(payload: dict, limit: int = 12) -> list[dict]:
    """Top-level entry. Returns up to `limit` insights, prioritised."""
    etf_btc = _etf_insights(payload, "btc")
    etf_eth = _etf_insights(payload, "eth")
    sigs = _signal_insights(payload)
    whales = _whale_insights(payload)
    mkts = _market_insights(payload)

    # Tag with the tab each insight belongs to.
    for i in etf_btc + etf_eth:
        i.setdefault("tab", "etf")
    for i in sigs:
        i.setdefault("tab", "signals")
    for i in whales:
        i.setdefault("tab", "whale")
    for i in mkts:
        i.setdefault("tab", _market_insight_tab(i))

    out = etf_btc + etf_eth + sigs + whales + mkts

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
