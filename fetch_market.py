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


def binance_futures_snapshot() -> list[dict]:
    """Funding rate + mark price across all Binance USDT-margined perpetuals.
    Single snapshot (not historical). Free, no auth."""
    j = _get("https://fapi.binance.com/fapi/v1/premiumIndex")
    if not j or not isinstance(j, list):
        return []
    out = []
    for r in j:
        sym = r.get("symbol") or ""
        if not sym.endswith("USDT"):  # ignore USDC/BUSD/etc
            continue
        try:
            out.append({
                "symbol": sym.replace("USDT", ""),
                "funding_rate": float(r.get("lastFundingRate") or 0),
                "mark_price": float(r.get("markPrice") or 0),
                "index_price": float(r.get("indexPrice") or 0),
                "next_funding_time_ms": int(r.get("nextFundingTime") or 0),
            })
        except (ValueError, TypeError):
            continue
    out.sort(key=lambda r: r["funding_rate"], reverse=True)
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


def lunarcrush_snapshot() -> dict:
    """Social-sentiment snapshot for top coins. Env-gated by LUNARCRUSH_API_KEY.

    Free tier is 10 req/min. We make one call but the IP may be shared
    (rate-limit can fire spuriously on the first call after startup).
    Retry once after a 3-second wait on a 429 response."""
    import os
    key = os.environ.get("LUNARCRUSH_API_KEY")
    if not key:
        return {"available": False, "reason": "no LUNARCRUSH_API_KEY in env"}
    url = "https://lunarcrush.com/api4/public/coins/list/v1"
    headers = {"Authorization": f"Bearer {key}", "User-Agent": UA}
    r = None
    for attempt in range(2):
        try:
            r = requests.get(url, headers=headers, timeout=20)
        except Exception as e:
            print(f"  [lunarcrush] error: {e}", file=sys.stderr)
            return {"available": False, "reason": "error"}
        if r.status_code == 200:
            break
        if r.status_code == 429 and attempt == 0:
            print(f"  [lunarcrush] HTTP 429 — retrying after backoff…", file=sys.stderr)
            time.sleep(3)
            continue
        print(f"  [lunarcrush] HTTP {r.status_code}", file=sys.stderr)
        return {"available": False, "reason": f"http_{r.status_code}"}
    try:
        j = r.json() or {}
        data = (j.get("data") if isinstance(j, dict) else j) or []
    except Exception as e:
        print(f"  [lunarcrush] parse: {e}", file=sys.stderr)
        return {"available": False, "reason": "parse_error"}
    out = []
    for c in data[:50]:
        if not isinstance(c, dict):
            continue
        out.append({
            "symbol": c.get("symbol"),
            "name": c.get("name"),
            "galaxy_score": c.get("galaxy_score"),
            "alt_rank": c.get("alt_rank"),
            "social_volume_24h": c.get("interactions_24h") or c.get("social_volume_24h"),
            "sentiment": c.get("sentiment"),
            "social_dominance": c.get("social_dominance"),
            "percent_change_24h": c.get("percent_change_24h"),
            "market_cap_rank": c.get("market_cap_rank"),
        })
    return {
        "available": bool(out),
        "coins": out,
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
    print("Fetching trading data...")
    print("  CoinGecko BTC/ETH/LINK/LTC market_chart...")
    btc_mkt = coingecko_market("bitcoin")
    eth_mkt = coingecko_market("ethereum")
    link_mkt = coingecko_market("chainlink")
    ltc_mkt = coingecko_market("litecoin")
    print("  CoinGecko global...")
    glob = coingecko_global()
    print("  Coinbase Exchange spot (BTC/ETH/LINK/LTC)...")
    cb_spot = coinbase_spot()
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
    print("  Binance Futures snapshot (all USDT perpetuals)...")
    binance_fut = binance_futures_snapshot()
    print("  GeckoTerminal DEX pools (trending + new)...")
    gt_pools = geckoterminal_pools()
    print("  LunarCrush social sentiment (env-gated by LUNARCRUSH_API_KEY)...")
    lunar = lunarcrush_snapshot()
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
    top_markets = coingecko_top_markets(25)
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
        "defillama": llama,
        "binance_futures": binance_fut,
        "geckoterminal": gt_pools,
        "lunarcrush": lunar,
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
