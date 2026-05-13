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
from datetime import datetime, timezone
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


def cryptocompare(syms: tuple = ("BTC", "ETH", "LINK")) -> dict:
    """CryptoCompare CCCAGG cross-exchange aggregate price + 24h volume.

    Free tier, no key needed for these public endpoints.
    """
    fsyms = ",".join(syms)
    j = _get("https://min-api.cryptocompare.com/data/pricemultifull",
             {"fsyms": fsyms, "tsyms": "USD"})
    out: dict[str, Any] = {}
    if not j or "RAW" not in j:
        return {"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    for sym in syms:
        raw = (j.get("RAW") or {}).get(sym, {}).get("USD")
        if not raw:
            continue
        out[sym] = {
            "price": float(raw.get("PRICE", 0)),
            "vol_24h_usd": float(raw.get("TOTALVOLUME24HTO", 0)),
            "top_tier_vol_24h_usd": float(raw.get("TOPTIERVOLUME24HOURTO", 0)),
            "mktcap_usd": float(raw.get("MKTCAP", 0)),
            "change_pct_24h": float(raw.get("CHANGEPCT24HOUR", 0)),
            "high_24h": float(raw.get("HIGH24HOUR", 0)),
            "low_24h": float(raw.get("LOW24HOUR", 0)),
        }
    out["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return out


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
    print("  CoinGecko BTC/ETH/LINK market_chart...")
    btc_mkt = coingecko_market("bitcoin")
    eth_mkt = coingecko_market("ethereum")
    link_mkt = coingecko_market("chainlink")
    print("  CoinGecko global...")
    glob = coingecko_global()
    print("  OKX funding BTC/ETH/LINK...")
    okx_fund_btc = okx_funding("BTC-USDT-SWAP")
    okx_fund_eth = okx_funding("ETH-USDT-SWAP")
    okx_fund_link = okx_funding("LINK-USDT-SWAP")
    print("  OKX open interest BTC/ETH/LINK...")
    okx_oi_btc = okx_open_interest("BTC")
    okx_oi_eth = okx_open_interest("ETH")
    okx_oi_link = okx_open_interest("LINK")
    print("  OKX long/short BTC/ETH/LINK...")
    okx_ls_btc = okx_long_short("BTC")
    okx_ls_eth = okx_long_short("ETH")
    okx_ls_link = okx_long_short("LINK")
    print("  Deribit DVOL BTC/ETH (LINK not supported)...")
    dvol_btc = deribit_dvol("BTC")
    dvol_eth = deribit_dvol("ETH")
    print("  DeFiLlama (stablecoin mcap, DEX vol, fees)...")
    llama = defillama()
    print("  CryptoCompare (cross-exchange CCCAGG)...")
    cc = cryptocompare()
    print("  Etherscan v2 (ETH gas oracle)...")
    gas = etherscan_gas()
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
        "global": glob,
        "defillama": llama,
        "cryptocompare": cc,
        "eth_gas": gas,
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
        "fear_greed": fng,
        "ethbtc": ethbtc,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def fetch_whale() -> dict:
    print("Fetching whale-activity proxies (BTC on-chain)...")
    btc = whale_proxies_btc()
    return {
        "btc": btc,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": "Free on-chain proxies. Real whale-flow metrics (Glassnode etc.) need a paid API.",
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
