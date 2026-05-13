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

    # CryptoCompare price-divergence sanity check (vs CoinGecko)
    cc = market.get("cryptocompare") or {}
    for asset in ("btc","eth","link"):
        cg_last_rows = (((market.get(asset) or {}).get("price")) or [])
        cg_last = cg_last_rows[-1].get("value") if cg_last_rows else None
        cc_price = (cc.get(asset.upper()) or {}).get("price")
        if cg_last and cc_price and cg_last > 0:
            div = abs(cc_price - cg_last) / cg_last
            if div >= 0.005:  # >0.5%
                out.append({"kind":"anomaly","asset":asset,"severity":"info",
                    "headline": f"{asset.upper()} price divergence: CoinGecko ${cg_last:,.0f} vs CryptoCompare ${cc_price:,.0f}",
                    "detail": f"{div*100:.2f}% spread between data sources."})

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
    # Trading desk: sentiment, funding, IV, BTC↔ETH crosses
    if "fear & greed" in h: return "trading"
    if "funding flipped" in h: return "trading"
    if "dvol" in h: return "trading"
    if "eth/btc" in h: return "trading"
    # DeFi: gas, stablecoin supply, DEX volume, chain TVL
    if "gas" in h: return "defi"
    if "stablecoin supply" in h: return "defi"
    if "dex 24h" in h: return "defi"
    if "tvl" in h: return "defi"
    # Whale / on-chain: mempool, hashrate, difficulty, mining concentration
    if "mempool" in h: return "whale"
    if "hashrate" in h: return "whale"
    if "difficulty retarget" in h: return "whale"
    if "mining concentration" in h: return "whale"
    # Markets / macro: traditional indices, news, trending tickers, source divergence
    if "dxy" in h: return "markets"
    if "10y treasury" in h: return "markets"
    if "gold at" in h: return "markets"
    if "s&p 500" in h: return "markets"
    if "📰" in raw: return "markets"
    if "trending #1" in h: return "markets"
    if "price divergence" in h: return "markets"
    # Default: anything else macro-flavoured lands in Markets.
    return "markets"


def build_insights(payload: dict, limit: int = 12) -> list[dict]:
    """Top-level entry. Returns up to `limit` insights, prioritised."""
    etf_btc = _etf_insights(payload, "btc")
    etf_eth = _etf_insights(payload, "eth")
    sigs = _signal_insights(payload)
    mkts = _market_insights(payload)

    # Tag with the tab each insight belongs to.
    for i in etf_btc + etf_eth:
        i.setdefault("tab", "etf")
    for i in sigs:
        i.setdefault("tab", "signals")
    for i in mkts:
        i.setdefault("tab", _market_insight_tab(i))

    out = etf_btc + etf_eth + sigs + mkts

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
