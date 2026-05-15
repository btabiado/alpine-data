"""
Free, no-API-key market and whale-activity fetchers.

Sources (all free, no auth required):
  CoinGecko       price, 24h volume, market cap
  OKX             funding rate, open interest, long/short ratio
  Deribit         DVOL (implied volatility index)
  Alternative.me  Fear & Greed Index
  blockchain.info BTC on-chain whale proxies

Output: data/market.json and data/whale.json, consumed by app.py.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

UA = "Mozilla/5.0 (compatible; etf-flow-dashboard/1.0)"
H = {"User-Agent": UA}
ROOT = Path(__file__).parent
CACHE = ROOT / "data"
CACHE.mkdir(exist_ok=True)


# ----- helpers ---------------------------------------------------------------

def _get(url: str, params: dict | None = None, timeout: int = 25) -> dict | list | None:
    try:
        r = requests.get(url, params=params, headers=H, timeout=timeout)
        if r.status_code != 200:
            print(f"  [skip] {url} -> {r.status_code}", file=sys.stderr)
            return None
        return r.json()
    except Exception as e:
        print(f"  [skip] {url} -> {e}", file=sys.stderr)
        return None


def _ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


# ----- trading ---------------------------------------------------------------

def coingecko_market(asset_id: str, days: int = 365) -> dict:
    """Daily price, market cap, total volume series. Free tier caps at 365 days."""
    j = _get(
        f"https://api.coingecko.com/api/v3/coins/{asset_id}/market_chart",
        {"vs_currency": "usd", "days": str(days)},
    )
    if not j:
        return {"price": [], "volume": [], "market_cap": []}
    return {
        "price": [{"date": _ts(p[0]), "value": p[1]} for p in j.get("prices", [])],
        "volume": [{"date": _ts(p[0]), "value": p[1]} for p in j.get("total_volumes", [])],
        "market_cap": [{"date": _ts(p[0]), "value": p[1]} for p in j.get("market_caps", [])],
    }


def coinbase_intl_perpetuals() -> list[dict]:
    """Coinbase International Exchange — funding rate + mark price + open
    interest for every PERP (~246 of them). Public endpoint, no auth.

    Works from US IPs (Binance's /fapi endpoint returns 451 from US, this
    one returns 200). Use case: cross-exchange perpetual positioning view
    next to OKX funding. The funding rate field returned is
    `predicted_funding` from the quote object, which is the rate that will
    settle at the next funding interval — i.e. forward-looking funding,
    most useful for spotting crowded positioning right now.

    Returns rows sorted by funding_rate descending (most crowded long first).
    Empty list on any failure.
    """
    j = _get("https://api.international.coinbase.com/api/v1/instruments")
    if not j or not isinstance(j, list):
        return []
    out: list[dict] = []
    for it in j:
        if it.get("type") != "PERP":
            continue
        sym_full = it.get("symbol") or ""
        sym = sym_full.replace("-PERP", "")
        if not sym:
            continue
        quote = it.get("quote") or {}
        try:
            out.append({
                "symbol":         sym,
                "funding_rate":   float(quote.get("predicted_funding") or 0),
                "mark_price":     float(quote.get("mark_price") or 0),
                "index_price":    float(quote.get("index_price") or 0),
                "open_interest_base": float(it.get("open_interest") or 0),
                "volume_24h":     float(it.get("qty_24hr") or 0),
                "notional_24h":   float(it.get("notional_24hr") or 0),
            })
        except (ValueError, TypeError):
            continue
    out.sort(key=lambda r: r["funding_rate"], reverse=True)
    return out


def coinbase_spot() -> dict:
    """Coinbase Exchange spot ticker + 24h stats for BTC/ETH/LINK/LTC.

    Public Exchange API endpoint (api.exchange.coinbase.com) — no auth, no
    key required, no rate-limit concerns for personal use. Adds a US-licensed
    exchange perspective alongside CoinGecko (which is a price aggregator,
    not an exchange) and OKX (offshore). Useful for:

      * Cross-exchange price-divergence sanity check (rule fires if Coinbase
        and CoinGecko diverge by ≥0.5%)
      * US-flavored bid/ask spread + 24h high/low/open in BTC-native units

    Returns:
        {
            "btc": {price_usd, bid, ask, volume_24h, open_24h, high_24h, low_24h, time},
            "eth": {...},
            "link": {...},
            "ltc": {...},
            "fetched_at": ISO,
        }
    """
    out: dict[str, Any] = {}
    products = [("BTC-USD", "btc"), ("ETH-USD", "eth"),
                ("LINK-USD", "link"), ("LTC-USD", "ltc")]
    for product, sym in products:
        ticker = _get(f"https://api.exchange.coinbase.com/products/{product}/ticker")
        stats = _get(f"https://api.exchange.coinbase.com/products/{product}/stats")
        if not ticker or not isinstance(ticker, dict):
            continue
        try:
            entry = {
                "price_usd":  float(ticker.get("price") or 0),
                "bid":        float(ticker.get("bid") or 0),
                "ask":        float(ticker.get("ask") or 0),
                "volume_24h": float(ticker.get("volume") or 0),  # base units (e.g. BTC)
                "time":       ticker.get("time"),
            }
            if isinstance(stats, dict):
                entry["open_24h"] = float(stats.get("open") or 0)
                entry["high_24h"] = float(stats.get("high") or 0)
                entry["low_24h"]  = float(stats.get("low") or 0)
                # Coinbase 24h change %: (last - open) / open
                if entry["open_24h"] > 0:
                    entry["change_24h_pct"] = (entry["price_usd"] / entry["open_24h"] - 1) * 100
            out[sym] = entry
        except (ValueError, TypeError) as e:
            print(f"  [coinbase] {product}: parse {e}", file=sys.stderr)
            continue
    out["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return out


def coingecko_global() -> dict:
    j = _get("https://api.coingecko.com/api/v3/global") or {}
    d = j.get("data", {})
    return {
        "btc_dominance": d.get("market_cap_percentage", {}).get("btc"),
        "eth_dominance": d.get("market_cap_percentage", {}).get("eth"),
        "total_market_cap_usd": d.get("total_market_cap", {}).get("usd"),
        "total_volume_usd": d.get("total_volume", {}).get("usd"),
        "active_cryptos": d.get("active_cryptocurrencies"),
    }


def okx_funding(inst: str, limit: int = 5000) -> list[dict]:
    """Funding rate history. Paginate backwards via 'after' (older records)."""
    out: list[dict] = []
    after = None
    seen = 0
    while seen < limit:
        params = {"instId": inst, "limit": "100"}
        if after is not None:
            params["after"] = str(after)
        j = _get("https://www.okx.com/api/v5/public/funding-rate-history", params)
        if not j or not j.get("data"):
            break
        rows = j["data"]
        if not rows:
            break
        for r in rows:
            out.append({"date": _ts(int(r["fundingTime"])), "rate": float(r["fundingRate"])})
        seen += len(rows)
        if len(rows) < 100:
            break
        after = int(rows[-1]["fundingTime"])
        time.sleep(0.12)
    out.sort(key=lambda r: r["date"])
    # Aggregate to daily mean (OKX has 3 funding settlements per day)
    by_day: dict[str, list[float]] = {}
    for r in out:
        by_day.setdefault(r["date"], []).append(r["rate"])
    return [{"date": d, "rate": sum(v) / len(v)} for d, v in sorted(by_day.items())]


def okx_open_interest(ccy: str) -> list[dict]:
    """USD-denominated open interest history (1d)."""
    j = _get(
        "https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume",
        {"ccy": ccy, "period": "1D"},
    )
    if not j or not j.get("data"):
        return []
    out = []
    for row in j["data"]:
        # row = [ts, oi_ccy, oi_usd]  (oi in CCY units and USD)
        out.append({"date": _ts(int(row[0])), "oi_usd": float(row[2])})
    out.sort(key=lambda r: r["date"])
    return out


def okx_long_short(ccy: str) -> list[dict]:
    j = _get(
        "https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio",
        {"ccy": ccy, "period": "1D"},
    )
    if not j or not j.get("data"):
        return []
    out = []
    for row in j["data"]:
        out.append({"date": _ts(int(row[0])), "ratio": float(row[1])})
    out.sort(key=lambda r: r["date"])
    return out


def deribit_dvol(currency: str, days: int = 1095) -> list[dict]:
    end = int(time.time() * 1000)
    start = end - days * 86400 * 1000
    j = _get(
        "https://www.deribit.com/api/v2/public/get_volatility_index_data",
        {
            "currency": currency,
            "start_timestamp": start,
            "end_timestamp": end,
            "resolution": "86400",
        },
    )
    if not j or "result" not in j:
        return []
    rows = j["result"].get("data", [])
    return [{"date": _ts(int(r[0])), "dvol": float(r[4])} for r in rows]


def coingecko_top_markets(per_page: int = 25) -> list[dict]:
    """Top N coins by market cap with price/vol/24h%/7d%/sparkline."""
    j = _get(
        "https://api.coingecko.com/api/v3/coins/markets",
        {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": str(per_page),
            "page": "1",
            "sparkline": "true",
            "price_change_percentage": "1h,24h,7d,30d",
        },
    )
    if not j or not isinstance(j, list):
        return []
    out = []
    for c in j:
        out.append({
            "rank": c.get("market_cap_rank"),
            "id": c.get("id"),
            "symbol": (c.get("symbol") or "").upper(),
            "name": c.get("name"),
            "image": c.get("image"),
            "price_usd": c.get("current_price"),
            "market_cap_usd": c.get("market_cap"),
            "volume_24h_usd": c.get("total_volume"),
            "high_24h_usd": c.get("high_24h"),
            "low_24h_usd": c.get("low_24h"),
            "change_1h_pct": c.get("price_change_percentage_1h_in_currency"),
            "change_24h_pct": c.get("price_change_percentage_24h_in_currency"),
            "change_7d_pct": c.get("price_change_percentage_7d_in_currency"),
            "change_30d_pct": c.get("price_change_percentage_30d_in_currency"),
            "sparkline_7d": (c.get("sparkline_in_7d") or {}).get("price", []),
            "ath_usd": c.get("ath"),
            "ath_change_pct": c.get("ath_change_percentage"),
        })
    return out


def coingecko_trending() -> list[dict]:
    """Top 7 trending coins on CoinGecko in the last 24h (search interest)."""
    j = _get("https://api.coingecko.com/api/v3/search/trending")
    if not j or not isinstance(j, dict):
        return []
    out = []
    for c in (j.get("coins") or []):
        item = c.get("item") or {}
        out.append({
            "rank": item.get("market_cap_rank"),
            "id": item.get("id"),
            "symbol": (item.get("symbol") or "").upper(),
            "name": item.get("name"),
            "thumb": item.get("thumb"),
            "score": item.get("score"),  # 0 = most trending
            "price_btc": item.get("price_btc"),
        })
    return out


def defillama_chains(top: int = 20) -> list[dict]:
    """TVL across all blockchain ecosystems (Ethereum, Solana, etc.)."""
    j = _get("https://api.llama.fi/v2/chains")
    if not j or not isinstance(j, list):
        return []
    chains = []
    for c in j:
        chains.append({
            "name": c.get("name"),
            "tvl_usd": c.get("tvl"),
            "change_1d_pct": c.get("change_1d"),
            "change_7d_pct": c.get("change_7d"),
            "change_1m_pct": c.get("change_1m"),
            "token_symbol": c.get("tokenSymbol"),
            "cmc_id": c.get("cmcId"),
        })
    chains.sort(key=lambda x: x.get("tvl_usd") or 0, reverse=True)
    return chains[:top]


def defillama_historical_tvl(chain: str = "Ethereum") -> list[dict]:
    """Daily TVL time series for a specific chain."""
    j = _get(f"https://api.llama.fi/v2/historicalChainTvl/{chain}")
    if not j or not isinstance(j, list):
        return []
    out = []
    for p in j:
        ts = p.get("date") or 0
        out.append({
            "date": datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d"),
            "tvl_usd": p.get("tvl"),
        })
    return out[-365:]  # keep last year


def defillama_protocols(top: int = 25) -> list[dict]:
    """Top DeFi protocols by TVL with 1d/7d/1m changes."""
    j = _get("https://api.llama.fi/protocols")
    if not j or not isinstance(j, list):
        return []
    out = []
    for p in j:
        out.append({
            "name": p.get("name"),
            "symbol": p.get("symbol"),
            "category": p.get("category"),
            "chains": p.get("chains") or [],
            "tvl_usd": p.get("tvl"),
            "change_1d_pct": p.get("change_1d"),
            "change_7d_pct": p.get("change_7d"),
            "change_1m_pct": p.get("change_1m"),
            "mcap_usd": p.get("mcap"),
            "url": p.get("url"),
        })
    out.sort(key=lambda x: x.get("tvl_usd") or 0, reverse=True)
    return out[:top]


def defillama_yields_stablecoin_top(top: int = 20) -> list[dict]:
    """Top stablecoin lending/yield pools across DeFi."""
    j = _get("https://yields.llama.fi/pools")
    if not j:
        return []
    data = (j.get("data") if isinstance(j, dict) else j) or []
    if not isinstance(data, list):
        return []
    stables = {"USDC", "USDT", "DAI", "FRAX", "LUSD", "USDD", "TUSD", "MIM", "PYUSD", "USDS", "USDE"}
    out = []
    for p in data:
        symbol = (p.get("symbol") or "").upper()
        if any(s in symbol for s in stables) and (p.get("tvlUsd") or 0) >= 5_000_000:
            out.append({
                "project": p.get("project"),
                "chain": p.get("chain"),
                "symbol": symbol,
                "tvl_usd": p.get("tvlUsd"),
                "apy_pct": p.get("apy"),
                "apy_base_pct": p.get("apyBase"),
                "apy_reward_pct": p.get("apyReward"),
                "stable": p.get("stablecoin"),
                "il_risk": p.get("ilRisk"),
            })
    out.sort(key=lambda x: x.get("tvl_usd") or 0, reverse=True)
    return out[:top]


def defillama_bridges() -> dict:
    """Cross-chain bridge daily volume snapshot.

    DeFiLlama's `bridges.llama.fi` host now requires payment (402). We
    fall back to the deprecated-but-still-public `/bridges` route under
    `api.llama.fi` if available; otherwise return empty.
    """
    j = _get("https://api.llama.fi/bridges")
    out: dict[str, Any] = {"top_bridges": []}
    if not j or not isinstance(j, dict):
        return out
    bridges = (j.get("bridges") or [])
    bridges = sorted(bridges, key=lambda b: (b.get("lastDailyVolume") or 0), reverse=True)
    out["top_bridges"] = [
        {
            "name": b.get("displayName") or b.get("name"),
            "daily_volume_usd": b.get("lastDailyVolume"),
            "weekly_volume_usd": b.get("lastWeeklyVolume"),
            "monthly_volume_usd": b.get("lastMonthlyVolume"),
            "chains": b.get("chains") or [],
        }
        for b in bridges[:10]
    ]
    return out


def crypto_news_rss(limit: int = 25) -> list[dict]:
    """Latest crypto headlines via free RSS feeds (CoinDesk, Decrypt, Cointelegraph)."""
    import xml.etree.ElementTree as ET
    feeds = [
        ("CoinDesk",       "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml"),
        ("Cointelegraph",  "https://cointelegraph.com/rss"),
        ("Decrypt",        "https://decrypt.co/feed"),
        ("The Block",      "https://www.theblock.co/rss.xml"),
        ("Bitcoin Magazine","https://bitcoinmagazine.com/feed"),
    ]
    out: list[dict] = []
    for source_name, url in feeds:
        try:
            r = requests.get(url, headers=H, timeout=15)
            if r.status_code != 200:
                continue
            root = ET.fromstring(r.text)
            items = root.findall(".//item")[:8]
            for it in items:
                title = (it.findtext("title") or "").strip()
                link = (it.findtext("link") or "").strip()
                pub = (it.findtext("pubDate") or "").strip()
                desc = (it.findtext("description") or "").strip()
                # Try to clean HTML from description (best-effort)
                desc = re.sub(r"<[^>]+>", "", desc)[:280]
                # Parse pub date
                ts = None
                date_str = pub
                try:
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(pub)
                    if dt:
                        ts = int(dt.timestamp())
                        date_str = dt.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    pass
                if title and link:
                    out.append({
                        "title": title,
                        "url": link,
                        "source": source_name,
                        "source_name": source_name,
                        "body": desc,
                        "ts": ts,
                        "date": date_str,
                    })
        except Exception as e:
            print(f"  [news] {source_name} failed: {e}", file=sys.stderr)
            continue
    # Sort newest first, dedupe by title prefix
    out.sort(key=lambda x: x.get("ts") or 0, reverse=True)
    seen: set[str] = set()
    deduped: list[dict] = []
    for n in out:
        k = (n["title"][:50]).lower()
        if k in seen:
            continue
        seen.add(k)
        deduped.append(n)
        if len(deduped) >= limit:
            break
    return deduped


# Module-level import needed for the RSS body regex
import re


def coindesk_cadli_ohlc(days: int = 90) -> list[dict]:
    """CoinDesk cadli BTC-USD daily OHLC — manipulation-resistant aggregate index."""
    j = _get(
        "https://data-api.coindesk.com/index/cc/v1/historical/days",
        {"market": "cadli", "instrument": "BTC-USD", "limit": str(days)},
    )
    if not j or not isinstance(j, dict):
        return []
    rows = (j.get("Data") or [])
    out = []
    for r in rows:
        ts = r.get("TIMESTAMP")
        if not ts:
            continue
        out.append({
            "date": datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d"),
            "open": r.get("OPEN"),
            "high": r.get("HIGH"),
            "low": r.get("LOW"),
            "close": r.get("CLOSE"),
            "volume": r.get("VOLUME"),
        })
    out.sort(key=lambda x: x["date"])
    return out


def mempool_difficulty_adjustment() -> dict:
    """BTC difficulty retarget countdown + estimate (mempool.space)."""
    j = _get("https://mempool.space/api/v1/difficulty-adjustment")
    if not j or not isinstance(j, dict):
        return {}
    return {
        "progress_pct": j.get("progressPercent"),
        "difficulty_change_pct": j.get("difficultyChange"),
        "estimated_retarget_date_unix": j.get("estimatedRetargetDate"),
        "remaining_blocks": j.get("remainingBlocks"),
        "remaining_time_ms": j.get("remainingTime"),
        "previous_retarget": j.get("previousRetarget"),
        "next_retarget_height": j.get("nextRetargetHeight"),
        "time_avg_ms": j.get("timeAvg"),
        "adjusted_time_avg_ms": j.get("adjustedTimeAvg"),
    }


def mempool_lightning_stats() -> dict:
    """Lightning Network capacity, channels, nodes."""
    j = _get("https://mempool.space/api/v1/lightning/statistics/latest")
    if not j or not isinstance(j, dict):
        return {}
    latest = j.get("latest") or {}
    return {
        "node_count": latest.get("node_count"),
        "channel_count": latest.get("channel_count"),
        "total_capacity_sat": latest.get("total_capacity"),
        "total_capacity_btc": (latest.get("total_capacity") or 0) / 1e8 if latest.get("total_capacity") else None,
        "tor_nodes": latest.get("tor_nodes"),
        "clearnet_nodes": latest.get("clearnet_nodes"),
        "unannounced_nodes": latest.get("unannounced_nodes"),
        "avg_capacity_btc": latest.get("avg_capacity") / 1e8 if latest.get("avg_capacity") else None,
        "avg_fee_rate": latest.get("avg_fee_rate"),
        "avg_base_fee_mtokens": latest.get("avg_base_fee_mtokens"),
    }


def mempool_mining_pools() -> dict:
    """BTC mining pool hashrate share (1-year window) — decentralization metric."""
    j = _get("https://mempool.space/api/v1/mining/pools/1y")
    if not j or not isinstance(j, dict):
        return {}
    pools = (j.get("pools") or [])
    total_blocks = sum(p.get("blockCount", 0) for p in pools) or 1
    out = []
    for p in pools[:15]:
        bc = p.get("blockCount", 0) or 0
        out.append({
            "name": p.get("name"),
            "blocks": bc,
            "share_pct": (bc / total_blocks) * 100.0,
            "rank": p.get("rank"),
            "empty_blocks": p.get("emptyBlocks"),
            "slug": p.get("slug"),
        })
    return {
        "pools": out,
        "total_blocks_window": total_blocks,
        "top2_concentration_pct": (out[0]["share_pct"] + out[1]["share_pct"]) if len(out) >= 2 else None,
    }


def mempool_space() -> dict:
    """mempool.space: BTC mempool fees, 3y hashrate series, current tip height."""
    out: dict[str, Any] = {}
    fees = _get("https://mempool.space/api/v1/fees/recommended")
    if fees:
        out["fees_sat_vb"] = fees  # {fastestFee, halfHourFee, hourFee, economyFee, minimumFee}
    tip = _get("https://mempool.space/api/blocks/tip/height")
    if tip is not None:
        out["tip_height"] = tip
    hr = _get("https://mempool.space/api/v1/mining/hashrate/3y")
    if hr and isinstance(hr, dict):
        series = hr.get("hashrates") or []
        # last 365d only to keep payload size reasonable
        out["hashrate_daily_eh"] = [
            {"date": datetime.fromtimestamp(int(p["timestamp"]), tz=timezone.utc).strftime("%Y-%m-%d"),
             "value": float(p.get("avgHashrate", 0)) / 1e18}  # convert H/s -> EH/s
            for p in series[-365:]
        ]
    out["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return out


def _parse_gt_pool(item: dict) -> dict | None:
    if not isinstance(item, dict):
        return None
    a = item.get("attributes") or {}
    r = item.get("relationships") or {}
    network = ((r.get("network") or {}).get("data") or {}).get("id") or ""
    dex = ((r.get("dex") or {}).get("data") or {}).get("id") or ""
    try:
        price = float(a.get("base_token_price_usd") or 0)
    except (TypeError, ValueError):
        price = 0.0
    try:
        vol = float((a.get("volume_usd") or {}).get("h24") or 0)
    except (TypeError, ValueError):
        vol = 0.0
    try:
        ch = float((a.get("price_change_percentage") or {}).get("h24") or 0)
    except (TypeError, ValueError):
        ch = 0.0
    tx_h24 = a.get("transactions", {}).get("h24") or {}
    try:
        txs = int(tx_h24.get("buys", 0)) + int(tx_h24.get("sells", 0))
    except (TypeError, ValueError):
        txs = 0
    return {
        "name": a.get("name") or "",
        "network": network,
        "dex": dex,
        "price_usd": price,
        "volume_24h_usd": vol,
        "change_24h_pct": ch,
        "transactions_24h": txs,
        "fdv_usd": a.get("fdv_usd"),
        "market_cap_usd": a.get("market_cap_usd"),
        "pool_address": a.get("address"),
    }


def geckoterminal_pools() -> dict:
    """DEX trending + new pools snapshot."""
    trending_j = _get("https://api.geckoterminal.com/api/v2/networks/trending_pools",
                      {"include": "base_token,quote_token"})
    new_j = _get("https://api.geckoterminal.com/api/v2/networks/new_pools", {"page": "1"})
    def to_rows(j):
        if not j or not isinstance(j, dict):
            return []
        data = j.get("data") or []
        out = [_parse_gt_pool(it) for it in data]
        return [r for r in out if r is not None]
    trending = to_rows(trending_j)[:20]
    new = to_rows(new_j)[:20]
    trending.sort(key=lambda r: r.get("volume_24h_usd") or 0, reverse=True)
    return {
        "trending_pools": trending,
        "new_pools": new,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _social_stale_fallback(key: str, default):
    """Restore previous good value for a `social` sub-key when the current
    fetch returns empty/None. Returns `default` if nothing usable found."""
    try:
        prev = json.loads((CACHE / "market.json").read_text()).get("social") or {}
        val = prev.get(key)
        if val:
            print(f"  [stale-keep] social.{key} kept from previous fetch", file=sys.stderr)
            return val
    except Exception:
        pass
    return default


def _reddit_rss_top_posts(sub: str, headers: dict) -> list[dict]:
    """Fallback for cloud-IP Reddit blocks: parse the public RSS feed
    instead of the JSON API. RSS feeds are sometimes less aggressively
    rate-limited / IP-filtered by Reddit's bot detection. Returns up to
    5 posts with title + permalink (no score/comment count via RSS)."""
    try:
        r = requests.get(f"https://www.reddit.com/r/{sub}/top/.rss?t=day",
                         headers=headers, timeout=15)
        if r.status_code != 200:
            print(f"  [reddit] /r/{sub}/top.rss -> {r.status_code}", file=sys.stderr)
            return []
        # Lightweight RSS parse — no extra deps. <entry> blocks contain
        # <title>...</title> and <link href="..."/>.
        import re as _re
        body = r.text
        entries = _re.findall(r"<entry>(.*?)</entry>", body, _re.S)
        out = []
        for e in entries[:5]:
            title_m = _re.search(r"<title[^>]*>(.*?)</title>", e, _re.S)
            link_m  = _re.search(r'<link[^>]*href="([^"]+)"', e)
            if not title_m:
                continue
            out.append({
                "title": _re.sub(r"<[^>]+>", "", title_m.group(1)).strip()[:120],
                "score": None,    # not available in RSS
                "comments": None,
                "url": link_m.group(1) if link_m else "",
            })
        return out
    except Exception as e:
        print(f"  [reddit] /r/{sub}/top.rss error: {e}", file=sys.stderr)
        return []


# Title-sentiment keyword lists. Plain word-match × upvote_ratio gives a
# decent buzz signal without an NLP dep. Tunable per market mood.
_BULL_KW = {"surge","rally","ath","breakout","moon","bullish","approved","approval",
            "soar","pump","green","record","milestone","adoption","upgrade","partnership"}
_BEAR_KW = {"crash","dump","ban","banned","hack","hacked","exploit","exploited",
            "bearish","sec","lawsuit","sue","sued","liquidation","rugpull","rug",
            "scam","plunge","drop","sell-off","selloff"}


def _title_sentiment(posts: list[dict]) -> dict:
    """(bull - bear) × upvote_ratio summed across titles. Returns
    {score, n, label} where label in {'bullish','bearish','neutral'}.
    Missing upvote_ratio (RSS posts) defaults to 0.85 (neutral confidence)."""
    import re as _re
    total, n = 0.0, 0
    for p in posts or []:
        title = (p.get("title") or "").lower()
        if not title:
            continue
        words = set(_re.findall(r"[a-z]+", title))
        bull = len(words & _BULL_KW)
        bear = len(words & _BEAR_KW)
        ratio = p.get("upvote_ratio")
        if ratio is None:
            ratio = 0.85
        total += (bull - bear) * float(ratio)
        n += 1
    if n == 0:
        return {"score": 0.0, "n": 0, "label": "neutral"}
    label = "bullish" if total >= 0.75 else ("bearish" if total <= -0.75 else "neutral")
    return {"score": round(total, 2), "n": n, "label": label}


# Module-level Reddit OAuth token cache so we re-auth once per fetch_all().
_REDDIT_TOKEN_CACHE: dict = {"token": None, "exp": 0.0}


def _reddit_oauth_token(ua: str) -> str | None:
    """Fetch (and cache for the process lifetime) a Reddit app-only bearer
    token via the client_credentials grant. Returns None on any failure so
    callers can fall back to anon. Requires REDDIT_CLIENT_ID + _SECRET in env."""
    import os
    cid = os.environ.get("REDDIT_CLIENT_ID")
    csec = os.environ.get("REDDIT_CLIENT_SECRET")
    if not cid or not csec:
        return None
    now = time.time()
    if _REDDIT_TOKEN_CACHE["token"] and _REDDIT_TOKEN_CACHE["exp"] - 60 > now:
        return _REDDIT_TOKEN_CACHE["token"]
    try:
        r = requests.post(
            "https://www.reddit.com/api/v1/access_token",
            auth=(cid, csec),
            data={"grant_type": "client_credentials"},
            headers={"User-Agent": ua},
            timeout=15,
        )
        if r.status_code != 200:
            print(f"  [reddit] oauth token -> {r.status_code}: {r.text[:160]}", file=sys.stderr)
            return None
        j = r.json() or {}
        tok = j.get("access_token")
        if not tok:
            return None
        _REDDIT_TOKEN_CACHE["token"] = tok
        _REDDIT_TOKEN_CACHE["exp"] = now + float(j.get("expires_in", 3600))
        return tok
    except Exception as e:
        print(f"  [reddit] oauth token error: {e}", file=sys.stderr)
        return None


def reddit_crypto_stats() -> dict:
    """Free Reddit data. Prefers OAuth (oauth.reddit.com) when
    REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET are set — bypasses most of
    Reddit's cloud-IP blocking and gives 100 req/min vs ~60 anon.

    For each of 9 subreddits, fetches: /about (subs + active users), /top
    (top 5 24h posts), /hot (top 3 climbing posts not yet in /top — the
    "trending now" signal). Aggregates per-sub title sentiment from the
    keyword lists above × upvote_ratio.

    Falls back to anon www.reddit.com JSON, then RSS for post titles, as
    defense in depth. Calls: 9 subs × 3 endpoints + 1 token = 28 paced at
    0.4s ≈ 11s overhead. Under Reddit's 100 req/min auth limit."""
    SUBS = [
        ("CryptoCurrency", "All crypto"),
        ("CryptoMarkets",  "Markets/TA"),
        ("Bitcoin",        "BTC"),
        ("ethereum",       "ETH"),
        ("solana",         "SOL"),
        ("cardano",        "ADA"),
        ("Chainlink",      "LINK"),
        ("litecoin",       "LTC"),
        ("defi",           "DeFi"),
    ]
    ua = ("btc-eth-etf-dashboard/1.0 (+https://github.com/btabiado/btc-eth-etf-dashboard) "
          "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 "
          "(KHTML, like Gecko) Version/18.0 Safari/605.1.15")
    token = _reddit_oauth_token(ua)
    if token:
        base = "https://oauth.reddit.com"
        headers = {"User-Agent": ua, "Authorization": f"bearer {token}",
                   "Accept": "application/json,text/xml,*/*;q=0.8"}
        print("  [reddit] using OAuth (oauth.reddit.com)", file=sys.stderr)
    else:
        base = "https://www.reddit.com"
        headers = {"User-Agent": ua,
                   "Accept": "application/json,text/xml,*/*;q=0.8"}
        print("  [reddit] no creds, using anon www.reddit.com", file=sys.stderr)

    out: dict[str, dict] = {}
    for sub, label in SUBS:
        meta = {"sub": sub, "label": label, "subscribers": None,
                "active_users": None, "top_posts": [], "trending": [],
                "sentiment": {"score": 0, "n": 0, "label": "neutral"},
                "ok": False}
        # About — subscriber + active-user counts
        try:
            r = requests.get(f"{base}/r/{sub}/about.json", headers=headers, timeout=15)
            if r.status_code == 200:
                d = (r.json() or {}).get("data") or {}
                meta["subscribers"] = d.get("subscribers")
                meta["active_users"] = d.get("active_user_count") or d.get("accounts_active")
                meta["description"] = (d.get("public_description") or "")[:120]
                meta["ok"] = True
            else:
                print(f"  [reddit] /r/{sub}/about -> {r.status_code}", file=sys.stderr)
        except Exception as e:
            print(f"  [reddit] /r/{sub}/about error: {e}", file=sys.stderr)
        time.sleep(0.4)
        # Top posts (last 24h) — JSON first, RSS fallback
        json_ok = False
        try:
            r = requests.get(f"{base}/r/{sub}/top.json?t=day&limit=5",
                             headers=headers, timeout=15)
            if r.status_code == 200:
                children = ((r.json() or {}).get("data") or {}).get("children") or []
                meta["top_posts"] = [{
                    "title": (c.get("data") or {}).get("title", "")[:120],
                    "score": (c.get("data") or {}).get("score"),
                    "comments": (c.get("data") or {}).get("num_comments"),
                    "upvote_ratio": (c.get("data") or {}).get("upvote_ratio"),
                    "url": "https://reddit.com" + ((c.get("data") or {}).get("permalink", "")),
                } for c in children if isinstance(c, dict)][:5]
                json_ok = True
            else:
                print(f"  [reddit] /r/{sub}/top -> {r.status_code} (will try RSS)", file=sys.stderr)
        except Exception as e:
            print(f"  [reddit] /r/{sub}/top error: {e} (will try RSS)", file=sys.stderr)
        if not json_ok:
            meta["top_posts"] = _reddit_rss_top_posts(sub, headers)
            if meta["top_posts"]:
                meta["ok"] = True
                meta["via_rss"] = True
        time.sleep(0.4)
        # Trending = hot ∖ top (climbing fast, not yet top of day). Non-critical.
        try:
            r = requests.get(f"{base}/r/{sub}/hot.json?limit=15",
                             headers=headers, timeout=15)
            if r.status_code == 200:
                hot_children = ((r.json() or {}).get("data") or {}).get("children") or []
                top_titles = {(p.get("title") or "") for p in meta["top_posts"]}
                trending = []
                for c in hot_children:
                    d = (c.get("data") or {}) if isinstance(c, dict) else {}
                    title = d.get("title") or ""
                    if not title or d.get("stickied") or title in top_titles:
                        continue
                    trending.append({
                        "title": title[:120],
                        "score": d.get("score"),
                        "comments": d.get("num_comments"),
                        "upvote_ratio": d.get("upvote_ratio"),
                        "url": "https://reddit.com" + (d.get("permalink") or ""),
                    })
                trending.sort(key=lambda p: p.get("score") or 0, reverse=True)
                meta["trending"] = trending[:3]
            else:
                print(f"  [reddit] /r/{sub}/hot -> {r.status_code}", file=sys.stderr)
        except Exception as e:
            print(f"  [reddit] /r/{sub}/hot error: {e}", file=sys.stderr)
        time.sleep(0.4)
        # Aggregate sentiment from top + trending titles
        meta["sentiment"] = _title_sentiment(
            (meta.get("top_posts") or []) + (meta.get("trending") or [])
        )
        out[sub.lower()] = meta
    return {
        "available": any(v.get("ok") for v in out.values()),
        "subreddits": out,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def cryptocompare_social_stats() -> dict:
    """Per-coin social + dev stats from CryptoCompare (now hosted under CoinDesk
    after the 2026 migration). Endpoint: /data/social/coin/latest?coinId=N.
    As of 2026 this requires an API key — set CRYPTOCOMPARE_API_KEY in env
    (free tier 100k calls/month; sign up at developers.coindesk.com).
    Without a key, the endpoint returns HTTP 200 with Response='Error' and
    empty Data; we skip cleanly and the UI renders "no data" rather than dashes.

    Coin IDs are CryptoCompare's internal numeric IDs (not symbols):
      BTC=1182, ETH=7605, LINK=46472, LTC=3808
    Returns Twitter followers, Reddit subscribers, GitHub stars/forks/PRs,
    code activity. Each call has its own try/except so partial failures
    don't kill the whole section."""
    import os
    # Accept either the canonical name or the legacy BTC_ETH_ETF_DASHBOARD
    # name that an earlier secret-add mishap created. Canonical wins when both
    # are set. Safe to drop the fallback once the legacy secret is renamed.
    api_key = (os.environ.get("CRYPTOCOMPARE_API_KEY") or
               os.environ.get("BTC_ETH_ETF_DASHBOARD") or "")
    # CryptoCompare internal coin IDs (NOT symbols). 46472 used to map to
    # LINK in older docs but now returns "Coin id is invalid"; the canonical
    # ID is 309621 (verified via /data/all/coinlist?fsym=LINK in 2026-05).
    COINS = {
        "btc":  {"id": 1182,   "name": "Bitcoin"},
        "eth":  {"id": 7605,   "name": "Ethereum"},
        "link": {"id": 309621, "name": "Chainlink"},
        "ltc":  {"id": 3808,   "name": "Litecoin"},
    }
    out: dict[str, dict] = {}
    for sym, meta in COINS.items():
        try:
            params = {"coinId": meta["id"]}
            # Auth via Authorization header is the documented v2 path; the
            # api_key query-string param is also accepted for backwards-compat.
            headers = dict(H)
            if api_key:
                headers["Authorization"] = f"Apikey {api_key}"
                params["api_key"] = api_key
            r = requests.get(
                "https://min-api.cryptocompare.com/data/social/coin/latest",
                params=params,
                headers=headers, timeout=15,
            )
            if r.status_code != 200:
                print(f"  [cryptocompare] {sym} -> {r.status_code}", file=sys.stderr)
                continue
            body = r.json() or {}
            # Without a key, the legacy endpoint returns HTTP 200 with
            # {"Response": "Error", "Message": "auth key required", "Data": {}}.
            # Skip cleanly so the UI shows "no data" cards instead of all-dash.
            if body.get("Response") == "Error" or not body.get("Data"):
                msg = (body.get("Message") or "")[:80]
                hint = "" if api_key else " (set CRYPTOCOMPARE_API_KEY)"
                print(f"  [cryptocompare] {sym} skipped: {msg}{hint}", file=sys.stderr)
                continue
            j = body["Data"]
            general = (j.get("General") or {})
            twitter = (j.get("Twitter") or {})
            reddit  = (j.get("Reddit") or {})
            repo    = (j.get("CodeRepository") or {}).get("List") or []
            # Aggregate across multiple repos if listed
            stars = forks = subs = pulls = issues = 0
            for r_ in repo:
                if not isinstance(r_, dict):
                    continue
                stars  += int(r_.get("stars") or 0)
                forks  += int(r_.get("forks") or 0)
                subs   += int(r_.get("subscribers") or 0)
                pulls  += int(r_.get("open_pull_issues") or 0)
                issues += int(r_.get("open_total_issues") or 0)
            out[sym] = {
                "name": meta["name"],
                "points": general.get("Points"),
                "twitter_followers": twitter.get("followers"),
                "twitter_statuses": twitter.get("statuses"),
                "reddit_subscribers": reddit.get("subscribers"),
                "reddit_active_users": reddit.get("active_users"),
                "reddit_posts_per_day": reddit.get("posts_per_day"),
                "reddit_comments_per_day": reddit.get("comments_per_day"),
                "github_stars": stars,
                "github_forks": forks,
                "github_subscribers": subs,
                "github_open_pulls": pulls,
                "github_open_issues": issues,
                "github_repo_count": len(repo),
            }
        except Exception as e:
            print(f"  [cryptocompare] {sym} error: {e}", file=sys.stderr)
        time.sleep(0.3)
    return {
        "available": bool(out),
        "coins": out,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# Generic noise filtered out of the keyword-cloud aggregation.
_CC_KW_STOP = {
    "crypto", "cryptocurrency", "cryptocurrencies", "blockchain",
    "market", "markets", "news", "price", "prices", "trading",
    "trader", "traders", "coin", "coins", "token", "tokens",
    "update", "report", "analysis",
}
# Per-coin aliases also dropped (e.g. don't show "bitcoin" as a tag on the BTC card)
_CC_CAT_ALIASES = {
    "BTC":  {"btc", "bitcoin", "xbt"},
    "ETH":  {"eth", "ethereum", "ether"},
    "LINK": {"link", "chainlink"},
    "LTC":  {"ltc", "litecoin"},
}


def _cc_aggregate_keywords(arts: list[dict], cat: str, top_n: int = 10) -> list[dict]:
    """Bucket KEYWORDS across articles, score by frequency × sentiment skew.
    sentiment_skew > 0 → keyword appears more in positive context; <0 negative."""
    drop = _CC_KW_STOP | _CC_CAT_ALIASES.get(cat, set())
    sent_val = {"POSITIVE": 1, "NEGATIVE": -1, "NEUTRAL": 0}
    counts: dict[str, int] = {}
    skew_sum: dict[str, int] = {}
    for a in arts:
        if not isinstance(a, dict):
            continue
        raw = a.get("KEYWORDS") or ""
        s = sent_val.get((a.get("SENTIMENT") or "").upper(), 0)
        seen: set[str] = set()
        for tok in str(raw).split(","):
            kw = tok.strip().lower()
            if not kw or len(kw) < 3 or kw.isdigit() or kw in drop:
                continue
            if kw in seen:
                continue
            seen.add(kw)
            counts[kw] = counts.get(kw, 0) + 1
            skew_sum[kw] = skew_sum.get(kw, 0) + s
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n]
    return [
        {"kw": kw, "count": n,
         "sentiment_skew": round(skew_sum[kw] / n, 3) if n else 0.0}
        for kw, n in ranked
    ]


def _cc_news_trend(category: str, days: int = 7, page_size: int = 50,
                   max_pages: int = 5) -> list[dict]:
    """Paginate cc data-api news backwards via to_ts, bucket by UTC date,
    return last `days` days of {date, pos, neg, neu, net}. Keyless."""
    cutoff = int(time.time()) - days * 86400
    buckets: dict[str, dict[str, int]] = {}
    to_ts = None
    for _page in range(max_pages):
        params = {"lang": "EN", "categories": category, "limit": page_size}
        if to_ts is not None:
            params["to_ts"] = to_ts
        try:
            r = requests.get(
                "https://data-api.cryptocompare.com/news/v1/article/list",
                params=params, headers=H, timeout=15,
            )
            if r.status_code != 200:
                print(f"  [cc-news-trend] {category} -> {r.status_code}", file=sys.stderr)
                break
            arts = (r.json() or {}).get("Data") or []
        except Exception as e:
            print(f"  [cc-news-trend] {category} error: {e}", file=sys.stderr)
            break
        if not arts:
            break
        oldest = None
        for a in arts:
            ts = a.get("PUBLISHED_ON") or 0
            if not ts:
                continue
            oldest = ts if oldest is None else min(oldest, ts)
            if ts < cutoff:
                continue
            d = datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
            b = buckets.setdefault(d, {"pos": 0, "neg": 0, "neu": 0})
            s = (a.get("SENTIMENT") or "").upper()
            if   s == "POSITIVE": b["pos"] += 1
            elif s == "NEGATIVE": b["neg"] += 1
            elif s == "NEUTRAL":  b["neu"] += 1
        if oldest is None or oldest < cutoff:
            break
        to_ts = oldest - 1
        time.sleep(0.3)
    today = datetime.now(timezone.utc).date()
    out = []
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        b = buckets.get(d, {"pos": 0, "neg": 0, "neu": 0})
        out.append({"date": d, "pos": b["pos"], "neg": b["neg"],
                    "neu": b["neu"], "net": b["pos"] - b["neg"]})
    return out


def _cc_trend_should_refresh() -> bool:
    """Trend is ~16 extra calls. Refresh only at the top of each hour (first
    5 minutes). Other minutes reuse the previous cached trend."""
    return datetime.now(timezone.utc).minute < 5


def _cc_trend_from_cache(sym: str) -> list[dict]:
    """Read previous trend_7d from data/market.json so non-refresh minutes
    can rehydrate without re-fetching."""
    try:
        prev = json.loads((CACHE / "market.json").read_text())
        return (((prev.get("social") or {}).get("cc_news") or {})
                .get("coins", {}).get(sym, {}).get("trend_7d")) or []
    except Exception:
        return []


def cryptocompare_news_sentiment() -> dict:
    """Per-coin news sentiment via CryptoCompare's keyless data-api endpoint.
    Returns sentiment counts, top 5 headlines, top-10 keyword cloud (with
    sentiment skew), and a 7-day daily sentiment trend (rate-limited to hourly).
    """
    CATS = {"btc": "BTC", "eth": "ETH", "link": "LINK", "ltc": "LTC"}
    refresh_trend = _cc_trend_should_refresh()
    out: dict[str, dict] = {}
    for sym, cat in CATS.items():
        try:
            r = requests.get(
                "https://data-api.cryptocompare.com/news/v1/article/list",
                params={"lang": "EN", "categories": cat, "limit": 50},
                headers=H, timeout=15,
            )
            if r.status_code != 200:
                print(f"  [cc-news] {cat} -> {r.status_code}", file=sys.stderr)
                continue
            body = r.json() or {}
            arts = body.get("Data") or []
            if not arts:
                continue
            pos = neg = neu = 0
            for a in arts:
                s = (a.get("SENTIMENT") or "").upper()
                if s == "POSITIVE":  pos += 1
                elif s == "NEGATIVE": neg += 1
                elif s == "NEUTRAL":  neu += 1
            top = []
            for a in arts[:5]:
                if not isinstance(a, dict):
                    continue
                top.append({
                    "title": (a.get("TITLE") or "")[:140],
                    "url": a.get("URL"),
                    "sentiment": (a.get("SENTIMENT") or "").upper(),
                    "source": ((a.get("SOURCE_DATA") or {}).get("NAME")) or a.get("SOURCE_ID"),
                    "published_on": a.get("PUBLISHED_ON"),
                    "upvotes": a.get("UPVOTES"),
                    "keywords_raw": a.get("KEYWORDS") or "",
                })
            total = pos + neg + neu
            # Keyword cloud + per-coin trend (cached unless hourly refresh window)
            top_keywords = _cc_aggregate_keywords(arts, cat, top_n=10)
            if refresh_trend:
                trend_7d = _cc_news_trend(cat, days=7)
            else:
                trend_7d = _cc_trend_from_cache(sym)
            out[sym] = {
                "category": cat,
                "article_count": len(arts),
                "positive": pos,
                "negative": neg,
                "neutral": neu,
                "positive_pct": (pos / total * 100) if total else None,
                "negative_pct": (neg / total * 100) if total else None,
                "neutral_pct":  (neu / total * 100) if total else None,
                "net_score": pos - neg,
                "top_articles": top,
                "top_keywords": top_keywords,
                "trend_7d": trend_7d,
            }
        except Exception as e:
            print(f"  [cc-news] {cat} error: {e}", file=sys.stderr)
        time.sleep(0.3)
    return {
        "available": bool(out),
        "coins": out,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def santiment_metrics() -> dict:
    """Santiment GraphQL — free-tier metrics for 4 coins. No API key.

    The free tier has a sliding window restriction (now-12mo to now-30d)
    for MOST metrics, but a handful work with recent data (lag=0): DAA,
    dev_activity, active_addresses_24h, dev_contributors. The rest
    (network_growth, mvrv_usd, exchange flows) need a ~35d lag query.

    Budget: 4 slugs × 7 metrics = 28 calls. Gated to fire only on the
    hourly run at UTC hour 0 (once/day). Other hours return prior good
    snapshot from cache (marked stale).
    """
    # Daily-only gate (stale-keep otherwise)
    now_hour = datetime.now(timezone.utc).hour
    if now_hour != 0:
        try:
            prev = json.loads((CACHE / "market.json").read_text())
            prev_san = ((prev.get("social") or {}).get("santiment")) or None
            if prev_san and prev_san.get("coins"):
                return {**prev_san, "stale": True, "stale_reason": f"daily_gate_hour_{now_hour}"}
        except Exception:
            pass
        return {"available": False, "reason": f"daily_gate_hour_{now_hour}",
                "coins": {},
                "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    SLUGS = {"btc": "bitcoin", "eth": "ethereum", "link": "chainlink", "ltc": "litecoin"}
    BTC_ETH_ONLY = {"btc", "eth"}
    # (metric_name, output_key, day_lag, slugs_supported)
    SANTIMENT_METRICS = [
        ("daily_active_addresses",         "daily_active_addresses",  0,  set(SLUGS)),
        ("dev_activity",                   "dev_activity",            0,  set(SLUGS)),
        ("active_addresses_24h",           "active_addresses_24h",    0,  set(SLUGS)),
        ("dev_activity_contributors_count","dev_contributors",        0,  set(SLUGS)),
        ("network_growth",                 "network_growth",          35, set(SLUGS)),
        ("mvrv_usd",                       "mvrv_usd",                35, set(SLUGS)),
        ("exchange_outflow",               "exchange_outflow",        35, BTC_ETH_ONLY),
        ("exchange_inflow",                "exchange_inflow",         35, BTC_ETH_ONLY),
    ]
    out: dict[str, dict] = {sym: {"slug": slug} for sym, slug in SLUGS.items()}
    now = datetime.now(timezone.utc)
    for metric, key, lag, slugs_ok in SANTIMENT_METRICS:
        # Build per-metric date window (recent vs lagged)
        to_dt = now - timedelta(days=lag) if lag else now
        from_dt = to_dt - timedelta(days=8)
        from_iso = from_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        to_iso   = to_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        for sym, slug in SLUGS.items():
            if slug not in {SLUGS[s] for s in slugs_ok}:
                continue
            q = ('query{getMetric(metric:"' + metric + '"){'
                 'timeseriesData(slug:"' + slug + '",from:"' + from_iso + '",to:"' + to_iso +
                 '",interval:"1d"){datetime value}}}')
            try:
                r = requests.post("https://api.santiment.net/graphql",
                                  json={"query": q}, headers=H, timeout=20)
                if r.status_code != 200:
                    print(f"  [santiment] {slug}/{metric} -> {r.status_code}", file=sys.stderr)
                    continue
                j = r.json() or {}
                ser = (((j.get("data") or {}).get("getMetric") or {}).get("timeseriesData")) or []
                points = [
                    {"date": (p.get("datetime") or "")[:10],
                     "value": p.get("value")}
                    for p in ser if isinstance(p, dict) and p.get("value") is not None
                ]
                if points:
                    out[sym][key] = points
                    # Also store a flat scalar summary the UI can use directly
                    latest = points[-1]
                    first = points[0]
                    delta_pct = ((latest["value"] - first["value"]) / first["value"] * 100) if first["value"] else None
                    out[sym][key + "_latest"] = latest["value"]
                    out[sym][key + "_delta_pct"] = delta_pct
                    out[sym][key + "_lag_days"] = lag
            except Exception as e:
                print(f"  [santiment] {slug}/{metric} error: {e}", file=sys.stderr)
            time.sleep(0.3)
    # Filter out slugs with no metrics populated
    out = {sym: data for sym, data in out.items()
           if any(k for k in data if k not in ("slug",))}
    return {
        "available": bool(out),
        "coins": out,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def point_of_control(price_series: list[dict], volume_series: list[dict],
                     lookback_days: int = 90, bins: int = 80) -> dict | None:
    """Volume profile Point of Control + Value Area for one asset.

    Algorithm:
      1. Align daily price + volume by date over the last `lookback_days`.
      2. Bin the price range into `bins` equal-width buckets.
      3. Each day's volume contributes to its closing-price bin.
      4. POC = price bin with highest cumulative volume.
      5. Value Area = smallest price band containing ~70% of total volume,
         expanded outward from POC by whichever neighbor (above/below) has
         more volume at each step.

    Returns {poc, val, vah, current, distance_pct, lookback, ...} or None
    if insufficient data."""
    if not price_series or not volume_series:
        return None
    p_by = {p.get("date"): p.get("value") for p in price_series if p.get("date") and p.get("value")}
    v_by = {v.get("date"): v.get("value") for v in volume_series if v.get("date") and v.get("value")}
    common = sorted(set(p_by) & set(v_by))
    if len(common) < 10:
        return None
    common = common[-lookback_days:]
    prices = [p_by[d] for d in common]
    volumes = [v_by[d] for d in common]
    lo = min(prices)
    hi = max(prices)
    if hi <= lo:
        return None
    step = (hi - lo) / bins
    buckets = [0.0] * bins
    for p, v in zip(prices, volumes):
        idx = min(int((p - lo) / step), bins - 1)
        buckets[idx] += v
    poc_idx = max(range(bins), key=lambda i: buckets[i])
    poc_price = lo + (poc_idx + 0.5) * step
    total_vol = sum(buckets)
    target = 0.70 * total_vol
    covered = buckets[poc_idx]
    lo_i = hi_i = poc_idx
    while covered < target and (lo_i > 0 or hi_i < bins - 1):
        below = buckets[lo_i - 1] if lo_i > 0 else -1
        above = buckets[hi_i + 1] if hi_i < bins - 1 else -1
        if below >= above and lo_i > 0:
            lo_i -= 1
            covered += buckets[lo_i]
        elif hi_i < bins - 1:
            hi_i += 1
            covered += buckets[hi_i]
        else:
            break
    val_price = lo + lo_i * step
    vah_price = lo + (hi_i + 1) * step
    current = prices[-1]
    # Expose buckets + step so the UI can render a horizontal volume profile
    # histogram inline with each POC card. List ordered low → high. Each entry
    # is {price: bin-center, volume: cumulative USD volume in that bin}.
    bucket_list = [
        {"price": lo + (i + 0.5) * step, "volume": buckets[i]}
        for i in range(bins)
    ]
    return {
        "poc": poc_price,
        "val": val_price,
        "vah": vah_price,
        "current": current,
        "distance_pct": (current - poc_price) / poc_price * 100 if poc_price else None,
        "in_value_area": val_price <= current <= vah_price,
        "lookback_days": len(common),
        "price_low": lo,
        "price_high": hi,
        "bin_count": bins,
        "step": step,
        "buckets": bucket_list,
        "total_volume_usd": total_vol,
    }


def compute_poc_all(market: dict) -> dict:
    """Compute multi-timeframe POC/VAH/VAL + migration + naked POCs per asset.
    Returns:
      {btc: {"d30":{...}, "d90":{...}, "d180":{...}, "d365":{...},
             "migration": {...}, "naked": [...]}, ...}
    Used by app.py during build to attach analytics to the payload."""
    out: dict[str, dict] = {}
    LOOKBACKS = (("d30", 30, 60), ("d90", 90, 80),
                 ("d180", 180, 100), ("d365", 365, 120))
    for sym in ("btc", "eth", "link", "ltc"):
        m = (market or {}).get(sym) or {}
        prices = m.get("price") or []
        volumes = m.get("volume") or []
        tfs = {k: point_of_control(prices, volumes, lookback_days=lb, bins=b)
               for k, lb, b in LOOKBACKS}
        if any(tfs.values()):
            out[sym] = {**tfs,
                        "migration": compute_poc_migration(tfs.get("d30"), tfs.get("d90")),
                        "naked": naked_pocs(prices, volumes, lookback_days=180)}
    return out


def compute_poc_migration(d30: dict | None, d90: dict | None) -> dict | None:
    """Compare 30d POC vs 90d POC to detect directional value migration.
    Positive delta = recent volume concentrating ABOVE the structural mean
    (bullish acceptance — "value is migrating up"). Negative = bearish
    acceptance. FLAT within ±1%.

    Returns None if either timeframe is missing or 90d POC is zero/invalid.
    `between_pocs` flags the transition-zone case where current price sits
    between 30d and 90d POC — often the most actionable read since structural
    support hasn't caught up to tactical volume formation."""
    if not d30 or not d90:
        return None
    p30, p90 = d30.get("poc"), d90.get("poc")
    if not p30 or not p90:
        return None
    delta = (p30 - p90) / p90 * 100
    a = abs(delta)
    direction = "FLAT" if a < 1 else ("UP" if delta > 0 else "DOWN")
    magnitude = "STRONG" if a >= 5 else ("MEDIUM" if a >= 2 else "WEAK")
    cur = d30.get("current") or d90.get("current")
    between = (cur is not None and min(p30, p90) <= cur <= max(p30, p90))
    if direction == "FLAT":
        explanation = f"Value stable (Δ {delta:+.2f}%) — 30d and 90d POCs aligned"
    else:
        word = "above" if direction == "UP" else "below"
        explanation = (f"Value migrating {direction} {delta:+.2f}% — "
                       f"short-term volume concentrating {word} structural mean")
    if between:
        explanation += " · price sits BETWEEN POCs (transition zone)"
    return {"delta_pct": round(delta, 2), "direction": direction,
            "magnitude": magnitude, "between_pocs": between,
            "explanation": explanation}


def naked_pocs(price_series: list[dict], volume_series: list[dict],
               lookback_days: int = 180, week_len: int = 7,
               skip_recent_weeks: int = 2, bins: int = 24,
               top_n: int = 5) -> list[dict]:
    """Find recent weekly POCs that price hasn't subsequently traded through.

    Market Profile theory: a POC that hasn't been retested acts as a magnet
    level — volume concentrated there but no later session has tested it.
    When price drifts back to a naked POC, expect a reaction.

    Touch approximation (daily close only, no OHLC): a POC is considered
    "touched" if a consecutive-close pair straddles it:
        min(close[t-1], close[t]) <= poc <= max(close[t-1], close[t])
    This misses intra-day wicks, so the function is biased toward MORE
    naked POCs than reality. Treat output as a candidate set."""
    if not price_series or not volume_series:
        return []
    p_by = {p.get("date"): p.get("value") for p in price_series if p.get("date") and p.get("value")}
    v_by = {v.get("date"): v.get("value") for v in volume_series if v.get("date") and v.get("value")}
    common = sorted(set(p_by) & set(v_by))
    if len(common) < week_len * (skip_recent_weeks + 3):
        return []
    common = common[-lookback_days:]
    prices = [p_by[d] for d in common]
    vols   = [v_by[d] for d in common]
    current = prices[-1]
    # Build weekly POCs newest-to-oldest
    weeks: list[dict] = []
    i = len(common)
    while i - week_len >= 0:
        seg_p, seg_v = prices[i-week_len:i], vols[i-week_len:i]
        lo, hi = min(seg_p), max(seg_p)
        if hi > lo:
            step = (hi - lo) / bins
            buckets = [0.0] * bins
            for p, v in zip(seg_p, seg_v):
                idx = min(int((p - lo) / step), bins - 1)
                buckets[idx] += v
            poc_idx = max(range(bins), key=lambda k: buckets[k])
            weeks.append({"week_start": common[i-week_len],
                          "end_idx": i-1,
                          "poc": lo + (poc_idx + 0.5) * step})
        i -= week_len
    # Skip the most recent N weeks (too fresh to have been tested)
    candidates = weeks[skip_recent_weeks:]
    naked = []
    last_idx = len(prices) - 1
    for w in candidates:
        poc, s = w["poc"], w["end_idx"]
        touched = False
        prev = prices[s]
        for t in range(s + 1, len(prices)):
            cur = prices[t]
            if min(prev, cur) <= poc <= max(prev, cur):
                touched = True
                break
            prev = cur
        if not touched:
            naked.append({
                "poc": round(poc, 2),
                "week_start": w["week_start"],
                "days_ago": last_idx - w["end_idx"],
                "distance_pct": round((current - poc) / poc * 100, 2) if poc else None,
            })
    naked.sort(key=lambda x: x["days_ago"])
    return naked[:top_n]


def fetch_social() -> dict:
    """Consolidated 'Research' tab payload. Composes free social + dev +
    on-chain + news signals from 4 independent free sources, each handled
    separately so partial failures degrade gracefully:

      reddit          — subscribers + active users + top 24h posts (often
                        blocked on cloud IPs with HTTP 403; works locally)
      cryptocompare   — per-coin Twitter/Reddit/GitHub social+dev stats
                        (requires CryptoCompare auth as of 2026; will skip
                        on missing key)
      cc_news         — per-coin news sentiment via the keyless data-api
                        (POSITIVE/NEGATIVE/NEUTRAL counts + top headlines)
      santiment       — DAA + dev-activity (daily-gated at hour=0 UTC)

    LunarCrush was removed — their v4 API is gated behind the Builder plan
    (~$240/mo); no free endpoints exist. See commit log for the decision.
    """
    # --- Reddit (no key, just User-Agent; cloud-IP blocked by Reddit) ---
    print("    reddit: subreddit stats + top 24h posts...")
    try:
        reddit = reddit_crypto_stats()
    except Exception as e:
        print(f"  [reddit] fatal: {e}", file=sys.stderr)
        reddit = {"available": False, "reason": "fetch_error", "subreddits": {}}
    if not reddit.get("available"):
        prev = _social_stale_fallback("reddit", {})
        if isinstance(prev, dict) and prev.get("subreddits"):
            reddit = {**prev, "stale": True}

    # --- CryptoCompare social/dev (legacy endpoint, now auth-gated) ---
    print("    cryptocompare: Twitter/Reddit/GitHub stats per coin...")
    try:
        cc = cryptocompare_social_stats()
    except Exception as e:
        print(f"  [cryptocompare] fatal: {e}", file=sys.stderr)
        cc = {"available": False, "reason": "fetch_error", "coins": {}}
    if not cc.get("available"):
        prev = _social_stale_fallback("cryptocompare", {})
        if isinstance(prev, dict) and prev.get("coins"):
            cc = {**prev, "stale": True}

    # --- CryptoCompare news sentiment (keyless data-api) ---
    print("    cc-news: per-coin sentiment + top headlines (no key)...")
    try:
        cc_news = cryptocompare_news_sentiment()
    except Exception as e:
        print(f"  [cc-news] fatal: {e}", file=sys.stderr)
        cc_news = {"available": False, "reason": "fetch_error", "coins": {}}
    if not cc_news.get("available"):
        prev = _social_stale_fallback("cc_news", {})
        if isinstance(prev, dict) and prev.get("coins"):
            cc_news = {**prev, "stale": True}

    # --- Santiment (no key for free tier; daily-gated for quota) ---
    print("    santiment: DAA + dev activity (daily-gated at 00:00 UTC)...")
    try:
        san = santiment_metrics()
    except Exception as e:
        print(f"  [santiment] fatal: {e}", file=sys.stderr)
        san = {"available": False, "reason": "fetch_error", "coins": {}}

    return {
        "available": any(s.get("available") for s in (reddit, cc, cc_news, san)),
        "reddit": reddit,
        "cryptocompare": cc,
        "cc_news": cc_news,
        "santiment": san,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def coin_metrics_btc_eth_metrics() -> dict:
    """Coin Metrics Community API — free network metrics for BTC + ETH.
    Tier 1 free only; metrics outside free tier return 403 and skip."""
    metrics = ["PriceUSD", "CapMrktCurUSD"]
    # Pull each asset+metric pair so we can gracefully degrade
    out: dict[str, dict[str, list[dict]]] = {"btc": {}, "eth": {}}
    import time as _time
    since = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%dT00:00:00")
    for asset in ("btc", "eth"):
        params = {
            "assets": asset,
            "metrics": ",".join(metrics),
            "start_time": since,
            "page_size": "1000",
            "frequency": "1d",
        }
        j = _get("https://community-api.coinmetrics.io/v4/timeseries/asset-metrics", params)
        if not j or not isinstance(j, dict):
            continue
        rows = j.get("data") or []
        for m in metrics:
            ser = []
            for r in rows:
                v = r.get(m)
                if v is None:
                    continue
                try:
                    ser.append({"date": (r.get("time") or "")[:10], "value": float(v)})
                except (ValueError, TypeError):
                    continue
            if ser:
                out[asset][m] = ser
    return {
        "btc": out["btc"],
        "eth": out["eth"],
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def etherscan_gas() -> dict:
    """Etherscan v2 gas oracle — ETH mainnet base fee + safe/propose/fast.

    Works without an API key but rate-limited to 1 req/5sec.
    """
    j = _get("https://api.etherscan.io/v2/api",
             {"chainid": "1", "module": "gastracker", "action": "gasoracle"})
    out: dict[str, Any] = {"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if not j or j.get("status") != "1":
        return out
    r = j.get("result") or {}
    try:
        out["safe_gwei"] = float(r.get("SafeGasPrice", 0))
        out["propose_gwei"] = float(r.get("ProposeGasPrice", 0))
        out["fast_gwei"] = float(r.get("FastGasPrice", 0))
        out["base_fee_gwei"] = float(r.get("suggestBaseFee", 0))
    except (TypeError, ValueError):
        pass
    return out


def fetch_fred() -> dict:
    """FRED (St. Louis Fed) macro overlay — DXY, S&P 500, gold, 10Y yield, M2.

    Free API; requires a self-service key in env var ``FRED_API_KEY``. If the
    key isn't set, return ``{"available": False, ...}`` and skip silently so
    the dashboard stays useful without a key.

    Series pulled (last 3 years):
        dxy           DTWEXBGS          Broad Dollar Index (daily, business)
        sp500         SP500             S&P 500 closing price (daily)
        gold          GOLDPMGBD228NLBM  London PM Gold Fixing (USD, daily) — switched
                                        from the retired AM Fix series.
        treasury_10y  DGS10             10-Year Treasury CMT (daily)
        m2            M2SL              M2 Money Stock (monthly)

    FRED encodes missing observations as ``"."`` — those are filtered out.
    """
    import os

    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    api_key = os.environ.get("FRED_API_KEY", "").strip()
    if not api_key:
        return {"available": False, "fetched_at": fetched_at}

    series_map = {
        "dxy":          "DTWEXBGS",
        "sp500":        "SP500",
        "gold":         "GOLDPMGBD228NLBM",
        "treasury_10y": "DGS10",
        "m2":           "M2SL",
    }
    start = (datetime.now(timezone.utc).date() - timedelta(days=1095)).isoformat()
    end = "2026-12-31"
    out: dict[str, Any] = {"available": True, "fetched_at": fetched_at}
    for friendly, series_id in series_map.items():
        j = _get(
            "https://api.stlouisfed.org/fred/series/observations",
            {
                "series_id": series_id,
                "api_key": api_key,
                "file_type": "json",
                "observation_start": start,
                "observation_end": end,
            },
        )
        rows: list[dict] = []
        if j and isinstance(j, dict):
            for obs in (j.get("observations") or []):
                date = obs.get("date")
                raw = obs.get("value")
                if not date or raw is None or raw == "." or raw == "":
                    continue
                try:
                    rows.append({"date": date, "value": float(raw)})
                except (TypeError, ValueError):
                    continue
        out[friendly] = rows

    # Fallback: FRED's London Gold fixings (AM and PM) were both discontinued
    # in 2017. If gold came back empty, pull from Yahoo Finance gold futures
    # (GC=F) — free, no key, ~2 years of daily closes.
    if not out.get("gold"):
        out["gold"] = _yahoo_gold(days=1095)
        if out["gold"]:
            out["gold_source"] = "yahoo:GC=F"

    return out


def yahoo_indices() -> dict:
    """Yahoo Finance public chart API — top US indices for the Overview tab.

    Free, no key, near-real-time. Returns latest close + 1d/5d/30d % change
    plus a 90-day sparkline series for each index.

    Tickers:
        ^DJI   Dow Jones Industrial Average
        ^GSPC  S&P 500
        ^IXIC  NASDAQ Composite
        ^VIX   CBOE Volatility Index (bonus — fear gauge)
    """
    indices = [
        ("dow",    "^DJI",  "Dow Jones Industrial Average"),
        ("sp500",  "^GSPC", "S&P 500"),
        ("nasdaq", "^IXIC", "NASDAQ Composite"),
        ("vix",    "^VIX",  "CBOE Volatility Index"),
    ]
    out: dict[str, Any] = {"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    for friendly, ticker, name in indices:
        j = _get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
            {"range": "3mo", "interval": "1d"},
        )
        if not j or not isinstance(j, dict):
            out[friendly] = None
            continue
        try:
            result = (j.get("chart") or {}).get("result", [])[0]
            meta = result.get("meta") or {}
            ts = result.get("timestamp") or []
            closes = ((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        except (IndexError, AttributeError, TypeError):
            out[friendly] = None
            continue
        # Filter out None closes
        series = [{"date": datetime.fromtimestamp(int(t), tz=timezone.utc).strftime("%Y-%m-%d"),
                   "value": float(c)}
                  for t, c in zip(ts, closes) if c is not None and t is not None]
        if not series:
            out[friendly] = None
            continue
        last = series[-1]["value"]
        # Compute % changes
        prev_1d = series[-2]["value"] if len(series) >= 2 else last
        prev_5d = series[-6]["value"] if len(series) >= 6 else series[0]["value"]
        prev_30d = series[-31]["value"] if len(series) >= 31 else series[0]["value"]
        out[friendly] = {
            "ticker": ticker,
            "name": name,
            "latest": last,
            "latest_date": series[-1]["date"],
            "change_1d_pct": (last / prev_1d - 1) * 100 if prev_1d else None,
            "change_5d_pct": (last / prev_5d - 1) * 100 if prev_5d else None,
            "change_30d_pct": (last / prev_30d - 1) * 100 if prev_30d else None,
            "previous_close": meta.get("chartPreviousClose"),
            "currency": meta.get("currency"),
            "exchange": meta.get("fullExchangeName"),
            "sparkline_90d": [p["value"] for p in series[-90:]],
            "series_90d": series[-90:],  # [{date, value}, ...] for downstream z-score etc.
        }
    return out


def _yahoo_gold(days: int = 1095) -> list[dict]:
    """Daily gold futures closes from Yahoo Finance public chart API."""
    range_str = "2y" if days <= 730 else "5y"
    j = _get(
        "https://query1.finance.yahoo.com/v8/finance/chart/GC=F",
        {"range": range_str, "interval": "1d"},
    )
    if not j or not isinstance(j, dict):
        return []
    try:
        result = (j.get("chart") or {}).get("result", [])[0]
        timestamps = result.get("timestamp") or []
        closes = ((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    except (IndexError, AttributeError, TypeError):
        return []
    rows = []
    for ts, close in zip(timestamps, closes):
        if close is None or ts is None:
            continue
        rows.append({
            "date": datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d"),
            "value": float(close),
        })
    return rows[-days:]


def defillama() -> dict:
    """DeFiLlama: stablecoin mcap & 7d delta, DEX 24h vol, fees 24h,
    plus a sanity-check price feed. No auth, no rate limit issues."""
    out: dict[str, Any] = {}
    prices = _get("https://coins.llama.fi/prices/current/"
                  "coingecko:bitcoin,coingecko:ethereum,coingecko:chainlink")
    if prices and "coins" in prices:
        out["prices"] = {v.get("symbol"): v.get("price") for v in prices["coins"].values() if v.get("symbol")}
    stables = _get("https://stablecoins.llama.fi/stablecoins?includePrices=false")
    if stables and stables.get("peggedAssets"):
        agg_now = agg_prev = 0.0
        for a in stables["peggedAssets"]:
            agg_now += (a.get("circulating") or {}).get("peggedUSD", 0) or 0
            agg_prev += (a.get("circulatingPrevWeek") or {}).get("peggedUSD", 0) or 0
        out["stablecoin_mcap_usd"] = agg_now
        out["stablecoin_7d_change_usd"] = agg_now - agg_prev
    dex = _get("https://api.llama.fi/overview/dexs?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true")
    fees = _get("https://api.llama.fi/overview/fees?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true")
    if dex: out["dex_volume_24h_usd"] = dex.get("total24h")
    if fees: out["fees_24h_usd"] = fees.get("total24h")
    out["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return out


def fear_greed(limit: int = 1095) -> list[dict]:
    j = _get(f"https://api.alternative.me/fng/?limit={limit}")
    if not j or "data" not in j:
        return []
    out = []
    for r in j["data"]:
        ts = int(r["timestamp"])
        out.append({
            "date": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d"),
            "value": int(r["value"]),
            "label": r.get("value_classification", ""),
        })
    out.sort(key=lambda r: r["date"])
    return out


# ----- whale proxies (BTC) ---------------------------------------------------

def blockchain_chart(name: str, span: str = "3years") -> list[dict]:
    j = _get(f"https://api.blockchain.info/charts/{name}", {"timespan": span, "format": "json"})
    if not j or "values" not in j:
        return []
    return [{"date": _ts(int(p["x"]) * 1000), "value": float(p["y"])} for p in j["values"]]


def whale_proxies_btc() -> dict:
    txvol_usd = blockchain_chart("estimated-transaction-volume-usd")
    txcnt = blockchain_chart("n-transactions")
    output_volume = blockchain_chart("output-volume")
    addresses = blockchain_chart("n-unique-addresses")
    hash_rate = blockchain_chart("hash-rate")
    miners_rev = blockchain_chart("miners-revenue")

    # Average transaction USD value = txvol_usd / txcnt (rising = whales moving more per tx)
    cnt_by_date = {r["date"]: r["value"] for r in txcnt}
    avg_tx_usd = []
    for r in txvol_usd:
        c = cnt_by_date.get(r["date"])
        if c and c > 0:
            avg_tx_usd.append({"date": r["date"], "value": r["value"] / c})

    return {
        "tx_volume_usd": txvol_usd,
        "tx_count": txcnt,
        "output_volume_btc": output_volume,
        "active_addresses": addresses,
        "hash_rate": hash_rate,
        "miners_revenue_usd": miners_rev,
        "avg_tx_usd": avg_tx_usd,
    }


# ----- main entrypoints ------------------------------------------------------

def fetch_trading() -> dict:
    # CoinGecko free tier is 30 calls/min. We make 7 calls back-to-back during
    # this function, which can trip the per-minute limit when combined with
    # any other recent activity (manual /api/refresh, /api/data polls, etc).
    # Sleep 600ms between CoinGecko calls so a single fetch_all() can't blow
    # the budget even on a cold start. Total: ~4s overhead — well worth it.
    CG_PACE = 0.6
    print("Fetching trading data...")
    print("  CoinGecko BTC/ETH/LINK/LTC market_chart...")
    btc_mkt = coingecko_market("bitcoin");   time.sleep(CG_PACE)
    eth_mkt = coingecko_market("ethereum");  time.sleep(CG_PACE)
    link_mkt = coingecko_market("chainlink"); time.sleep(CG_PACE)
    ltc_mkt = coingecko_market("litecoin");  time.sleep(CG_PACE)
    print("  CoinGecko global...")
    glob = coingecko_global();               time.sleep(CG_PACE)
    print("  Coinbase Exchange spot (BTC/ETH/LINK/LTC)...")
    cb_spot = coinbase_spot()
    print("  Coinbase International Exchange perpetuals snapshot...")
    cb_intl = coinbase_intl_perpetuals()
    print("  OKX funding BTC/ETH/LINK/LTC...")
    okx_fund_btc = okx_funding("BTC-USDT-SWAP")
    okx_fund_eth = okx_funding("ETH-USDT-SWAP")
    okx_fund_link = okx_funding("LINK-USDT-SWAP")
    okx_fund_ltc = okx_funding("LTC-USDT-SWAP")
    print("  OKX open interest BTC/ETH/LINK/LTC...")
    okx_oi_btc = okx_open_interest("BTC")
    okx_oi_eth = okx_open_interest("ETH")
    okx_oi_link = okx_open_interest("LINK")
    okx_oi_ltc = okx_open_interest("LTC")
    print("  OKX long/short BTC/ETH/LINK/LTC...")
    okx_ls_btc = okx_long_short("BTC")
    okx_ls_eth = okx_long_short("ETH")
    okx_ls_link = okx_long_short("LINK")
    okx_ls_ltc = okx_long_short("LTC")
    print("  Deribit DVOL BTC/ETH (LINK and LTC not supported)...")
    dvol_btc = deribit_dvol("BTC")
    dvol_eth = deribit_dvol("ETH")
    print("  DeFiLlama (stablecoin mcap, DEX vol, fees)...")
    llama = defillama()
    print("  GeckoTerminal DEX pools (trending + new)...")
    gt_pools = geckoterminal_pools()
    print("  Research tab social/sentiment sources (Reddit, CryptoCompare, Santiment)...")
    social = fetch_social()
    print("  Coin Metrics Community (BTC/ETH network metrics)...")
    cm = coin_metrics_btc_eth_metrics()
    print("  Etherscan v2 (ETH gas oracle)...")
    gas = etherscan_gas()
    fred = fetch_fred()
    if fred.get("available"):
        print("  FRED macro (DXY/SPX/Gold/10Y/M2)...")
    print("  mempool.space (BTC fees, hashrate, tip height)...")
    mp = mempool_space()
    print("  mempool difficulty adjustment + lightning + mining pools...")
    diff_adj = mempool_difficulty_adjustment()
    lightning = mempool_lightning_stats()
    pools = mempool_mining_pools()
    print("  CoinGecko top-25 markets + trending...")
    top_markets = coingecko_top_markets(25); time.sleep(CG_PACE)
    if not top_markets and (CACHE / "market.json").exists():
        # CoinGecko 429 (rate-limit wipe) returns []. Pacing in 2b396b8 helps
        # but doesn't fully eliminate races. Preserve the last good value
        # instead of overwriting cache with an empty list.
        try:
            prev = json.loads((CACHE / "market.json").read_text()).get("markets_top") or []
            if prev:
                top_markets = prev
                print(f"  [stale-keep] markets_top empty from API; kept {len(prev)} from previous fetch")
        except Exception as e:
            print(f"  [stale-keep] failed to read previous markets_top: {e}", file=sys.stderr)
    trending = coingecko_trending()
    print("  DeFiLlama: chains + protocols + yields + bridges + historical TVL...")
    chains = defillama_chains(20)
    protocols = defillama_protocols(25)
    yields_top = defillama_yields_stablecoin_top(20)
    bridges = defillama_bridges()
    tvl_eth = defillama_historical_tvl("Ethereum")
    tvl_sol = defillama_historical_tvl("Solana")
    tvl_arb = defillama_historical_tvl("Arbitrum")
    tvl_base = defillama_historical_tvl("Base")
    print("  Crypto news RSS (CoinDesk + Cointelegraph + Decrypt + Block + BTC Mag)...")
    news = crypto_news_rss(25)
    print("  CoinDesk cadli BTC-USD OHLC (90d)...")
    cadli = coindesk_cadli_ohlc(90)
    print("  Yahoo Finance indices (Dow / S&P / NASDAQ / VIX)...")
    yahoo_idx = yahoo_indices()
    print("  Fear & Greed...")
    fng = fear_greed()

    # ETH/BTC ratio from prices
    btc_p = {p["date"]: p["value"] for p in btc_mkt["price"]}
    ethbtc = []
    for p in eth_mkt["price"]:
        b = btc_p.get(p["date"])
        if b and b > 0:
            ethbtc.append({"date": p["date"], "value": p["value"] / b})

    return {
        "btc": {
            "price": btc_mkt["price"],
            "volume": btc_mkt["volume"],
            "market_cap": btc_mkt["market_cap"],
            "funding": okx_fund_btc,
            "open_interest_usd": okx_oi_btc,
            "long_short_ratio": okx_ls_btc,
            "dvol": dvol_btc,
        },
        "eth": {
            "price": eth_mkt["price"],
            "volume": eth_mkt["volume"],
            "market_cap": eth_mkt["market_cap"],
            "funding": okx_fund_eth,
            "open_interest_usd": okx_oi_eth,
            "long_short_ratio": okx_ls_eth,
            "dvol": dvol_eth,
        },
        "link": {
            "price": link_mkt["price"],
            "volume": link_mkt["volume"],
            "market_cap": link_mkt["market_cap"],
            "funding": okx_fund_link,
            "open_interest_usd": okx_oi_link,
            "long_short_ratio": okx_ls_link,
            "dvol": [],
        },
        "ltc": {
            "price": ltc_mkt["price"],
            "volume": ltc_mkt["volume"],
            "market_cap": ltc_mkt["market_cap"],
            "funding": okx_fund_ltc,
            "open_interest_usd": okx_oi_ltc,
            "long_short_ratio": okx_ls_ltc,
            "dvol": [],
        },
        "global": glob,
        "coinbase": cb_spot,
        "coinbase_intl_perps": cb_intl,
        "defillama": llama,
        "geckoterminal": gt_pools,
        "social": social,
        "coin_metrics": cm,
        "eth_gas": gas,
        "fred": fred,
        "mempool": mp,
        "mempool_extra": {
            "difficulty_adjustment": diff_adj,
            "lightning": lightning,
            "pools": pools,
        },
        "markets_top": top_markets,
        "trending": trending,
        "defi": {
            "chains": chains,
            "protocols": protocols,
            "yields_stablecoin": yields_top,
            "bridges": bridges,
            "tvl_history": {
                "Ethereum": tvl_eth,
                "Solana": tvl_sol,
                "Arbitrum": tvl_arb,
                "Base": tvl_base,
            },
        },
        "news": news,
        "cadli_btc": cadli,
        "yahoo_indices": yahoo_idx,
        "fear_greed": fng,
        "ethbtc": ethbtc,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def glassnode_btc_whale_metrics() -> dict:
    """Optional: pull true whale cohort metrics from Glassnode Studio.

    Env-gated by GLASSNODE_API_KEY. If unset (or 403 returned for higher-tier
    metrics), returns an empty dict and the dashboard falls back to free
    bitinfocharts data + the activity proxy chart.

    Each call is independent — partial tier access is fine; failures skip
    the missing series rather than aborting the whole batch.

    Metrics pulled (all daily, BTC asset):
      addresses/min_1k_count            — # of addresses with ≥1,000 BTC
      addresses/min_10k_count           — # of addresses with ≥10,000 BTC
      transactions/transfers_volume_sum — total transfer volume (BTC)
      transactions/transfers_to_exchanges_sum   — exchange inflow proxy
      transactions/transfers_from_exchanges_sum — exchange outflow proxy
      supply/profit_relative            — % supply in profit (regime context)

    Returns:
        {
            "available": bool,
            "tier_status": {metric_path: "ok" | "forbidden" | "error"},
            "series": {metric_path: [{"date": "YYYY-MM-DD", "value": float}, ...]},
            "fetched_at": ISO timestamp,
        }
    """
    import os
    key = os.environ.get("GLASSNODE_API_KEY")
    if not key:
        return {"available": False, "reason": "no GLASSNODE_API_KEY in env"}
    metrics = [
        "addresses/min_1k_count",
        "addresses/min_10k_count",
        "transactions/transfers_volume_sum",
        "transactions/transfers_to_exchanges_sum",
        "transactions/transfers_from_exchanges_sum",
        "supply/profit_relative",
    ]
    series_out: dict[str, list[dict]] = {}
    tier_status: dict[str, str] = {}
    # 90 days back is enough for the dashboard; bumps to 365 if user has tier.
    import time as _time
    since = int(_time.time()) - 90 * 86400
    for m in metrics:
        url = f"https://api.glassnode.com/v1/metrics/{m}"
        try:
            r = requests.get(
                url,
                params={"a": "BTC", "api_key": key, "i": "24h", "s": since},
                headers=H,
                timeout=20,
            )
            if r.status_code == 200:
                rows = r.json() or []
                series_out[m] = [
                    {"date": datetime.fromtimestamp(int(p["t"]), tz=timezone.utc).strftime("%Y-%m-%d"),
                     "value": p.get("v")}
                    for p in rows if isinstance(p, dict) and "t" in p
                ]
                tier_status[m] = "ok"
            elif r.status_code in (401, 402, 403):
                # 401 invalid key, 402/403 tier mismatch — skip gracefully
                tier_status[m] = "forbidden"
                print(f"  [glassnode] {m}: tier {r.status_code} (paid plan needed)", file=sys.stderr)
            else:
                tier_status[m] = f"http_{r.status_code}"
                print(f"  [glassnode] {m}: HTTP {r.status_code}", file=sys.stderr)
        except Exception as e:
            tier_status[m] = "error"
            print(f"  [glassnode] {m}: {e}", file=sys.stderr)
    return {
        "available": any(v == "ok" for v in tier_status.values()),
        "tier_status": tier_status,
        "series": series_out,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def bitinfocharts_btc_distribution() -> dict:
    """BTC supply held per address-balance cohort, daily, ~5 years back.

    Source: bitinfocharts.com/bitcoin-distribution-history.html — they publish
    the Dygraph data inline as `[new Date("YYYY/MM/DD"), v1, v2, ..., v8]`
    arrays. Each row is one day; the 8 values are supply (BTC) held by
    addresses in each balance band:

        0-0.1, 0.1-1, 1-10, 10-100, 100-1K, 1K-10K, 10K-100K, 100K-1M

    The last three columns (≥1,000 BTC) are the whale cohort.

    Returns:
        {
            "labels": [...],
            "buckets": [
                {"date": "YYYY-MM-DD",
                 "b0_01": ..., "b01_1": ..., "b1_10": ..., "b10_100": ...,
                 "b100_1k": ..., "b1k_10k": ..., "b10k_100k": ..., "b100k_1m": ...},
                ...
            ],
            "source": "bitinfocharts.com",
        }

    Returns an empty dict on any failure (network, parse, structure change).
    """
    import re
    try:
        r = requests.get(
            "https://bitinfocharts.com/bitcoin-distribution-history.html",
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X)"},
            timeout=30,
        )
        if r.status_code != 200 or not r.text:
            print(f"  [skip] bitinfocharts -> {r.status_code}", file=sys.stderr)
            return {}
    except Exception as e:
        print(f"  [skip] bitinfocharts -> {e}", file=sys.stderr)
        return {}

    pattern = re.compile(
        r'\[new Date\("(\d{4}/\d{1,2}/\d{1,2})"\)((?:,\s*-?\d+(?:\.\d+)?|,\s*null)+)\]'
    )
    rows = []
    for m in pattern.finditer(r.text):
        date_iso = m.group(1).replace("/", "-")
        # Pad single-digit month / day to 2 chars
        try:
            d = datetime.strptime(m.group(1), "%Y/%m/%d").strftime("%Y-%m-%d")
        except ValueError:
            continue
        vals = [v.strip() for v in m.group(2).strip(",").split(",")]
        if len(vals) != 8:
            continue
        try:
            parsed = [None if v == "null" else float(v) for v in vals]
        except ValueError:
            continue
        rows.append({
            "date": d,
            "b0_01":     parsed[0],
            "b01_1":     parsed[1],
            "b1_10":     parsed[2],
            "b10_100":   parsed[3],
            "b100_1k":   parsed[4],
            "b1k_10k":   parsed[5],
            "b10k_100k": parsed[6],
            "b100k_1m":  parsed[7],
        })
    if not rows:
        print("  [skip] bitinfocharts: no rows parsed", file=sys.stderr)
        return {}
    return {
        "labels": ["0-0.1", "0.1-1", "1-10", "10-100",
                   "100-1K", "1K-10K", "10K-100K", "100K-1M"],
        "buckets": rows,
        "source": "bitinfocharts.com",
        "note": "BTC supply held per address-balance cohort. ≥1,000 BTC = whale.",
    }


def fetch_whale() -> dict:
    print("Fetching whale-activity proxies (BTC on-chain)...")
    btc = whale_proxies_btc()
    print("  bitinfocharts BTC distribution history (cohorts)...")
    distribution = bitinfocharts_btc_distribution()
    print("  Glassnode (optional, env-gated by GLASSNODE_API_KEY)...")
    glassnode = glassnode_btc_whale_metrics()
    return {
        "btc": btc,
        "distribution": distribution,
        "glassnode": glassnode,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": ("Free: blockchain.info + bitinfocharts cohorts. "
                 "Glassnode auto-activates when GLASSNODE_API_KEY is set."),
    }


def fetch_all() -> None:
    trading = fetch_trading()
    (CACHE / "market.json").write_text(json.dumps(trading))
    print(f"  wrote {CACHE/'market.json'}")
    whale = fetch_whale()
    (CACHE / "whale.json").write_text(json.dumps(whale))
    print(f"  wrote {CACHE/'whale.json'}")


if __name__ == "__main__":
    fetch_all()
