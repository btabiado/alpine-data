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

import asyncio
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

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

def _coingecko_market_impl(asset_id: str, days: int = 365) -> dict:
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


def coingecko_market(asset_id: str, days: int = 365) -> dict:
    """Stale-fallback wrapper around `_coingecko_market_impl`.

    CG free tier rate-limits aggressively (~30 req/min) and frequently 429s
    during top-N sweeps. If the price array comes back empty we fall back
    to the cached prior result for this exact ``asset_id`` so the section
    isn't blanked by a single transient rate-limit hit.
    """
    cache_key = f"coingecko_market_{asset_id}"
    try:
        out = _coingecko_market_impl(asset_id, days)
    except Exception as e:
        print(f"  [coingecko_market] {asset_id}: fatal {e}", file=sys.stderr)
        out = None
    # Empty price array == failed fetch; everything else == success.
    if isinstance(out, dict) and out.get("price"):
        _stale_save(cache_key, out)
        return out
    cached = _stale_load(cache_key)
    if cached is not None:
        return cached
    return out if isinstance(out, dict) else {"price": [], "volume": [], "market_cap": []}


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


def _coinbase_spot_impl() -> dict:
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


def coinbase_spot() -> dict:
    """Stale-fallback wrapper around `_coinbase_spot_impl`.

    If all four assets (btc/eth/link/ltc) come back empty (e.g., Coinbase
    Exchange API outage or transient block), serve the last good payload
    from `data/.stale/coinbase_spot.json` tagged with stale metadata.
    """
    try:
        out = _coinbase_spot_impl()
    except Exception as e:
        print(f"  [coinbase_spot] fatal: {e}", file=sys.stderr)
        out = None
    # Success means at least one of the four expected symbols populated.
    expected = ("btc", "eth", "link", "ltc")
    if isinstance(out, dict) and any(out.get(s) for s in expected):
        _stale_save("coinbase_spot", out)
        return out
    cached = _stale_load("coinbase_spot")
    if cached is not None:
        return cached
    return out if isinstance(out, dict) else {
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
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


def coingecko_top_markets(per_page: int = 50) -> list[dict]:
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
    # Fields kept here are the union of what every consumer reads:
    #   - signals.compute_signal_simple (price_usd, market_cap_usd,
    #     volume_24h_usd, change_24h_pct, change_7d_pct, change_30d_pct,
    #     sparkline_7d, symbol, name, rank, image)
    #   - compute_poc_top_markets (id, symbol, name, image, price_usd)
    #   - insights.build_insights (symbol, name, rank, market_cap_usd,
    #     change_24h_pct, change_7d_pct)
    #   - tests/test_stocks_breadth.py (symbol; sparkline_7d as breadth source)
    # Five fields previously emitted but never read — high_24h_usd,
    # low_24h_usd, change_1h_pct, ath_usd, ath_change_pct — are dropped to
    # shrink the inlined market.json blob in the rendered dashboard.
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
            "change_24h_pct": c.get("price_change_percentage_24h_in_currency"),
            "change_7d_pct": c.get("price_change_percentage_7d_in_currency"),
            "change_30d_pct": c.get("price_change_percentage_30d_in_currency"),
            "sparkline_7d": (c.get("sparkline_in_7d") or {}).get("price", []),
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

    The legacy `api.llama.fi/bridges` route now 404s. DeFiLlama moved the
    bridges API onto its own subdomain at `bridges.llama.fi/bridges`. If
    that also fails (rare auth/quota cases), gracefully return an empty
    list — never raise.
    """
    out: dict[str, Any] = {"top_bridges": []}
    j = _get("https://bridges.llama.fi/bridges")
    if not j or not isinstance(j, dict):
        return out
    bridges = (j.get("bridges") or [])
    if not isinstance(bridges, list):
        return out
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


def crypto_news_rss(limit: int = 120) -> list[dict]:
    """Latest crypto headlines via free RSS feeds (CoinDesk, Decrypt, Cointelegraph).

    NB: ``limit`` is the post-dedupe total cap. The per-feed cap is raised
    from 8 → 30 below so the Research-tab "Top-25 news sentiment" card has a
    wider corpus to match against — with only ~25 items the long tail of
    alt-coins scored zero mentions even after alias expansion. Each feed
    still bounds itself to 30 to avoid one chatty source crowding out the
    others. The Research card only renders the top-25 coins so it's
    insensitive to the absolute corpus size; the win is broader coin
    coverage, not more headlines per row.
    """
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
            items = root.findall(".//item")[:30]
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
                except Exception as e:
                    print(f"  [news] pubdate parse suppressed: {type(e).__name__}", file=sys.stderr)
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


# ----- AI news (RSS + keyword sentiment) -------------------------------------

# Keyword lists scoped at module level so they're trivially testable and the
# per-item scoring loop doesn't rebuild them. Lowercase form only; the scorer
# lowercases the title/body before matching.
_AI_NEWS_POSITIVE_KEYWORDS = (
    "breakthrough", "launches", "raises", "wins", "advance", "milestone",
    "best", "leading", "growth", "valuation", "funding round", "series",
    "deal", "partnership", "outperform", "open-source",
)
_AI_NEWS_NEGATIVE_KEYWORDS = (
    "lawsuit", "fired", "layoff", "warns", "risk", "regulate", "ban",
    "concern", "fear", "fail", "down", "loss", "fraud", "investigation",
    "outage", "leaked", "hack", "breach", "harm", "decline", "delay",
    "criticism", "deepfake", "misinformation",
)


def ai_news_rss(per_feed_limit: int = 15) -> list[dict]:
    """Latest AI/ML headlines via free RSS feeds (TechCrunch AI, The Verge AI,
    VentureBeat AI, MIT Technology Review AI, Anthropic, OpenAI, Ars Technica).
    Sorted newest first, deduped by title, capped at 60 items total.

    Mirrors `crypto_news_rss()` — same field schema:
        {title, url, source, source_name, body, ts, date}
    Each per-feed fetch is wrapped so one bad XML response doesn't take the
    whole batch down.
    """
    import xml.etree.ElementTree as ET
    feeds = [
        ("TechCrunch AI",     "https://techcrunch.com/category/artificial-intelligence/feed/"),
        ("The Verge AI",      "https://www.theverge.com/ai-artificial-intelligence/rss/index.xml"),
        ("VentureBeat AI",    "https://venturebeat.com/category/ai/feed/"),
        ("MIT Tech Review",   "https://www.technologyreview.com/topic/artificial-intelligence/feed"),
        ("Anthropic",         "https://www.anthropic.com/news/feed.xml"),
        ("OpenAI",            "https://openai.com/news/rss.xml"),
        ("Ars Technica",      "https://feeds.arstechnica.com/arstechnica/index/"),
    ]
    out: list[dict] = []
    for source_name, url in feeds:
        try:
            r = requests.get(url, headers=H, timeout=15)
            if r.status_code != 200:
                print(f"  [ai-news] {source_name} -> {r.status_code}", file=sys.stderr)
                continue
            # Strip BOM / XML namespace prefixes can show up but ET handles
            # them fine — only catch malformed XML here.
            try:
                root = ET.fromstring(r.text)
            except ET.ParseError as e:
                print(f"  [ai-news] {source_name} parse: {e}", file=sys.stderr)
                continue
            # RSS 2.0 uses <item>; Atom uses <entry>. Try both.
            items = root.findall(".//item")
            is_atom = False
            if not items:
                # Atom: namespace is http://www.w3.org/2005/Atom — use a
                # wildcard local-name match so we don't have to hard-code it.
                items = root.findall(".//{http://www.w3.org/2005/Atom}entry")
                is_atom = bool(items)
            items = items[:per_feed_limit]
            for it in items:
                try:
                    if is_atom:
                        title = (it.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
                        # Atom links are <link href="..."/>
                        link_el = it.find("{http://www.w3.org/2005/Atom}link")
                        link = (link_el.get("href") if link_el is not None else "") or ""
                        pub = (it.findtext("{http://www.w3.org/2005/Atom}updated")
                               or it.findtext("{http://www.w3.org/2005/Atom}published")
                               or "").strip()
                        desc = (it.findtext("{http://www.w3.org/2005/Atom}summary")
                                or it.findtext("{http://www.w3.org/2005/Atom}content")
                                or "").strip()
                    else:
                        title = (it.findtext("title") or "").strip()
                        link = (it.findtext("link") or "").strip()
                        pub = (it.findtext("pubDate") or "").strip()
                        desc = (it.findtext("description") or "").strip()
                    desc = re.sub(r"<[^>]+>", "", desc)[:280]
                    ts = None
                    date_str = pub
                    # Try RFC822 (RSS pubDate) then ISO 8601 (Atom updated).
                    try:
                        from email.utils import parsedate_to_datetime
                        dt = parsedate_to_datetime(pub)
                        if dt:
                            ts = int(dt.timestamp())
                            date_str = dt.strftime("%Y-%m-%d %H:%M")
                    except Exception as e:
                        print(f"  [news] RFC822 pubdate parse suppressed: {type(e).__name__}", file=sys.stderr)
                    if ts is None and pub:
                        try:
                            # Atom often has e.g. 2026-05-15T12:34:56Z
                            dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                            ts = int(dt.timestamp())
                            date_str = dt.strftime("%Y-%m-%d %H:%M")
                        except Exception as e:
                            print(f"  [news] ISO8601 pubdate parse suppressed: {type(e).__name__}", file=sys.stderr)
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
                    # Per-entry failure — skip and keep parsing the rest.
                    print(f"  [ai-news] {source_name} entry: {e}", file=sys.stderr)
                    continue
        except Exception as e:
            print(f"  [ai-news] {source_name} failed: {e}", file=sys.stderr)
            continue
    out.sort(key=lambda x: x.get("ts") or 0, reverse=True)
    seen: set[str] = set()
    deduped: list[dict] = []
    for n in out:
        k = (n["title"][:50]).lower()
        if k in seen:
            continue
        seen.add(k)
        deduped.append(n)
        if len(deduped) >= 60:
            break
    return deduped


def compute_ai_sentiment(items: list[dict]) -> dict:
    """Keyword-based POSITIVE/NEGATIVE/NEUTRAL tagging for AI news items.

    Each item is scored against title + body (body only if non-empty). An item
    is POSITIVE if it has any positive keyword and no negative keywords,
    NEGATIVE if it has any negative keyword and no positive keywords, and
    NEUTRAL if both/neither are present.

    Returns aggregate counts plus the item list with a `sentiment` field
    attached to each row. Caller pops `items` out of the result if it wants
    summary-only stats.
    """
    pos = neg = neu = 0
    enriched: list[dict] = []
    for it in items or []:
        title = (it.get("title") or "")
        body = (it.get("body") or "")
        text = (title + " " + body if body.strip() else title).lower()
        has_pos = any(kw in text for kw in _AI_NEWS_POSITIVE_KEYWORDS)
        has_neg = any(kw in text for kw in _AI_NEWS_NEGATIVE_KEYWORDS)
        if has_pos and not has_neg:
            label = "POSITIVE"
            pos += 1
        elif has_neg and not has_pos:
            label = "NEGATIVE"
            neg += 1
        else:
            label = "NEUTRAL"
            neu += 1
        row = dict(it)
        row["sentiment"] = label
        enriched.append(row)
    total = pos + neg + neu
    net_score = pos - neg
    # Overall label thresholds: if net_score dominates, tag it; else NEUTRAL.
    # Picking a small absolute floor (>=2 net items) keeps a single article
    # from swinging the dashboard summary.
    if total == 0:
        overall = "NEUTRAL"
    elif net_score >= 2 and pos > neg:
        overall = "POSITIVE"
    elif net_score <= -2 and neg > pos:
        overall = "NEGATIVE"
    else:
        overall = "NEUTRAL"
    return {
        "positive": pos,
        "negative": neg,
        "neutral": neu,
        "total": total,
        "net_score": net_score,
        "sentiment_label": overall,
        "items": enriched,
    }


def _fetch_ai_news_impl() -> dict:
    items = ai_news_rss()
    sent = compute_ai_sentiment(items)
    return {
        "available": bool(items),
        "items": sent.pop("items"),
        "summary": sent,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def fetch_ai_news() -> dict:
    """Stale-fallback wrapper around `_fetch_ai_news_impl`.

    The publisher feeds (especially MIT TR + VentureBeat) periodically 503
    or block our UA — when every feed fails we get an empty `items` list,
    which would blank the AI News tab. Fall back to the last successful
    fetch in that case.
    """
    try:
        out = _fetch_ai_news_impl()
    except Exception as e:
        print(f"  [fetch_ai_news] fatal {e}", file=sys.stderr)
        out = None
    if isinstance(out, dict) and out.get("available") and out.get("items"):
        _stale_save("fetch_ai_news", out)
        return out
    cached = _stale_load("fetch_ai_news")
    if cached is not None:
        return cached
    return out if isinstance(out, dict) else {
        "available": False,
        "items": [],
        "summary": {"positive": 0, "negative": 0, "neutral": 0,
                    "total": 0, "net_score": 0, "sentiment_label": "NEUTRAL"},
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ----- AI funding & curated data --------------------------------------------

_AI_FUNDING_KEYWORDS = (
    "ai", "a.i.", "artificial intelligence", "machine learning", " ml ",
    "openai", "anthropic", "mistral", "cohere", "perplexity", "xai",
    "llm", "gpt", "claude", "gemini", "model", "foundation model",
    "generative", "neural", "deep learning", "agent", "agents",
    "robot", "robotics", "humanoid", "autonom", "self-driving",
    "chip", "silicon", "inference", "training compute",
)


def load_ai_curated() -> dict:
    """Read the curated AI snapshot from data/ai_curated.json.

    Returns an empty dict with the expected top-level keys if the file is
    missing or malformed so downstream consumers can rely on the shape.

    After loading, the ``top_funded_companies`` rows are enriched with
    Wikipedia infobox data (founded year, employee count, HQ, industry) via
    :mod:`wiki_enrich`. Curated values always win — Wikipedia only fills
    gaps. The enrichment is defensive: if Wikipedia is unreachable, the
    parser fails, or anything else goes wrong, the raw curated snapshot is
    returned unchanged.
    """
    path = ROOT / "data" / "ai_curated.json"
    empty = {
        "top_funded_companies": [],
        "investment_kpis": [],
        "whitepaper_kpis": [],
        "compiled_at": None,
        "sources_index": [],
    }
    try:
        if not path.exists():
            print(f"  [ai-curated] {path} missing, returning empty shell", file=sys.stderr)
            return empty
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            return empty
        # Fill in any missing top-level keys so callers can index safely.
        for k, v in empty.items():
            data.setdefault(k, v)
    except Exception as e:
        print(f"  [ai-curated] load failed: {e}", file=sys.stderr)
        return empty
    # Wikipedia enrichment — isolated so a bad parse can never break the build.
    try:
        import wiki_enrich
        data = wiki_enrich.enrich_ai_curated(data)
    except Exception as e:
        print(f"  [ai-curated] wiki enrichment skipped: {e}", file=sys.stderr)
    return data


def _fetch_yc_ai_companies_impl(limit: int = 200) -> dict:
    """Pull YC's AI-tagged company list from the free yc-oss mirror."""
    url = "https://yc-oss.github.io/api/tags/artificial-intelligence.json"
    try:
        r = requests.get(url, headers=H, timeout=20)
        if r.status_code != 200:
            print(f"  [yc-ai] {url} -> {r.status_code}", file=sys.stderr)
            return {"yc_companies": [], "yc_total_ai_count": 0}
        rows = r.json()
    except Exception as e:
        print(f"  [yc-ai] failed: {e}", file=sys.stderr)
        return {"yc_companies": [], "yc_total_ai_count": 0}
    if not isinstance(rows, list):
        return {"yc_companies": [], "yc_total_ai_count": 0}
    total = len(rows)
    out: list[dict] = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        out.append({
            "name": row.get("name"),
            "slug": row.get("slug"),
            "batch": row.get("batch"),
            "status": row.get("status"),
            "one_liner": row.get("one_liner") or row.get("subtitle") or "",
            "tags": row.get("tags") or [],
        })
    return {"yc_companies": out, "yc_total_ai_count": total}


def fetch_yc_ai_companies(limit: int = 200) -> dict:
    """Stale-fallback wrapper around `_fetch_yc_ai_companies_impl`."""
    try:
        out = _fetch_yc_ai_companies_impl(limit)
    except Exception as e:
        print(f"  [fetch_yc_ai_companies] fatal {e}", file=sys.stderr)
        out = None
    if isinstance(out, dict) and out.get("yc_companies"):
        _stale_save("fetch_yc_ai_companies", out)
        return out
    cached = _stale_load("fetch_yc_ai_companies")
    if cached is not None:
        return cached
    return out if isinstance(out, dict) else {
        "yc_companies": [], "yc_total_ai_count": 0,
    }


def _fetch_ai_funding_news_hn_impl(days: int = 30, max_items: int = 40) -> list[dict]:
    """Pull recent 'raises Series' Hacker News stories filtered for AI relevance."""
    epoch_cutoff = int(time.time()) - days * 86400
    url = "https://hn.algolia.com/api/v1/search"
    params = {
        "query": "raises Series",
        "tags": "story",
        "numericFilters": f"created_at_i>{epoch_cutoff}",
        "hitsPerPage": 100,
    }
    try:
        r = requests.get(url, params=params, headers=H, timeout=20)
        if r.status_code != 200:
            print(f"  [hn-funding] -> {r.status_code}", file=sys.stderr)
            return []
        j = r.json()
    except Exception as e:
        print(f"  [hn-funding] failed: {e}", file=sys.stderr)
        return []
    hits = (j or {}).get("hits") or []
    out: list[dict] = []
    for h in hits:
        title = (h.get("title") or "").strip()
        if not title:
            continue
        low = title.lower()
        if not any(kw in low for kw in _AI_FUNDING_KEYWORDS):
            continue
        url_ = (h.get("url") or "").strip()
        if not url_:
            obj_id = h.get("objectID")
            if not obj_id:
                continue
            url_ = f"https://news.ycombinator.com/item?id={obj_id}"
        created_i = h.get("created_at_i")
        date_str = ""
        if created_i:
            try:
                date_str = datetime.fromtimestamp(int(created_i), tz=timezone.utc).strftime("%Y-%m-%d")
            except Exception:
                date_str = ""
        out.append({
            "title": title,
            "url": url_,
            "source": "HN",
            "date": date_str,
        })
        if len(out) >= max_items:
            break
    return out


def fetch_ai_funding_news_hn(days: int = 30, max_items: int = 40) -> list[dict]:
    """Stale-fallback wrapper around the HN funding-news fetcher."""
    try:
        out = _fetch_ai_funding_news_hn_impl(days, max_items)
    except Exception as e:
        print(f"  [fetch_ai_funding_news_hn] fatal {e}", file=sys.stderr)
        out = None
    if isinstance(out, list) and out:
        _stale_save("fetch_ai_funding_news_hn", out)
        return out
    cached = _stale_load("fetch_ai_funding_news_hn")
    if isinstance(cached, list):
        return cached
    return out if isinstance(out, list) else []


# --- SEC EDGAR Form D (private placement filings) ---------------------------
#
# Form D is filed within 15 days of a Rule 506(b) / 506(c) private placement
# and discloses issuer, date of first sale, total offering amount, amount
# sold, and exemption claimed. EDGAR's full-text search at efts.sec.gov is
# keyless JSON but requires a polite User-Agent per SEC fair access rules
# (https://www.sec.gov/os/accessing-edgar-data) — without one EDGAR 403s.
#
# Approach: pull recent Form D filings via the search-index endpoint (one
# request, paginated), filter to AI-adjacent issuer names client-side, then
# for the top N matches optionally fetch the primary XML document to extract
# the offering-amount fields. Per-filing fetches are rate-limited (sleep
# between requests) to stay well under SEC's 10 req/sec/IP limit.

# Polite SEC User-Agent — SEC asks for "Sample Company Name AdminContact@..."
# style. Without this header EDGAR replies 403 Forbidden. Keep it generic so
# we're not impersonating anyone; the dashboard isn't a registered entity.
SEC_UA = "etf-flow-dashboard/1.0 (open-source dashboard; contact@etf-flow-dashboard.local)"
SEC_HEADERS = {"User-Agent": SEC_UA, "Accept": "application/json"}

# Keywords used to flag AI-adjacent Form D filings by issuer name. Kept
# narrower than _AI_FUNDING_KEYWORDS because issuer names are short and
# generic terms like "ai" produce too many false positives without word
# boundaries; the matcher below does word-boundary checks.
_SEC_AI_KEYWORDS = (
    "ai", "a.i.", "artificial intelligence", "machine learning",
    "neural", "deep learning", "gpt", "llm", "agents", "agentic",
    "robotic", "robotics", "autonomous", "intelligence",
    "openai", "anthropic", "mistral", "cohere", "perplexity",
    "inference", "model", "vision", "speech",
)


def _ai_keyword_hit(name: str) -> bool:
    """Word-boundary keyword check for issuer names. Returns True if any
    keyword in `_SEC_AI_KEYWORDS` appears as a whole word (or substring for
    multi-word phrases). Defensive against empty / non-string input."""
    if not isinstance(name, str) or not name:
        return False
    low = name.lower()
    for kw in _SEC_AI_KEYWORDS:
        if " " in kw or "." in kw:
            # multi-word / acronym: substring match is safer (whole-word
            # regex can choke on punctuation in company names like "A.I.").
            if kw in low:
                return True
        else:
            # single-word keyword: require a word boundary so "ai" doesn't
            # match every "main", "rain", "captain" in the filings.
            if re.search(rf"\b{re.escape(kw)}\b", low):
                return True
    return False


def _sec_get(url: str, params: dict | None = None, timeout: int = 20):
    """SEC-flavored requests.get that always uses the polite UA. Returns
    the parsed JSON (or text for non-JSON endpoints) or None on failure.
    Honors EDGAR's preferred 10 req/sec ceiling implicitly by being called
    serially in the fetcher with a small sleep between calls."""
    try:
        r = requests.get(url, params=params, headers=SEC_HEADERS, timeout=timeout)
        if r.status_code != 200:
            print(f"  [sec] {url} -> {r.status_code}", file=sys.stderr)
            return None
        ct = (r.headers.get("Content-Type") or "").lower()
        if "json" in ct:
            return r.json()
        return r.text
    except Exception as e:
        print(f"  [sec] {url} -> {e}", file=sys.stderr)
        return None


def _sec_accession_url(cik: str, adsh: str) -> str:
    """Build the public EDGAR filing-index URL for an accession number.
    `adsh` arrives with dashes (0001234567-25-000123); the archive path uses
    the no-dash form for the folder and the original form for the .index."""
    cik_int = str(cik).lstrip("0") or "0"
    nodash = adsh.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{nodash}/{adsh}-index.htm"


def _sec_primary_doc_url(cik: str, adsh: str) -> str:
    """The structured XML version of the Form D filing — has the offering-
    amount fields we want. Path mirrors `_sec_accession_url`."""
    cik_int = str(cik).lstrip("0") or "0"
    nodash = adsh.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{nodash}/primary_doc.xml"


def _parse_form_d_xml(xml_text: str) -> dict:
    """Extract the four headline Form D fields from primary_doc.xml.

    Returns a dict with keys: ``total_offering_amount`` (float or None),
    ``total_amount_sold`` (float or None), ``date_of_first_sale`` (ISO date
    string or empty), ``exemptions`` (list of exemption strings). Tolerant
    of missing elements — Form D has many optional fields."""
    out: dict = {
        "total_offering_amount": None,
        "total_amount_sold": None,
        "date_of_first_sale": "",
        "exemptions": [],
    }
    if not xml_text or not isinstance(xml_text, str):
        return out
    try:
        import xml.etree.ElementTree as ET
        # primary_doc.xml uses no default namespace at the leaf-text level
        # for the fields we care about, but some have eis: prefixes. The
        # simplest cross-version-tolerant approach: strip namespaces.
        cleaned = re.sub(r'\sxmlns(:\w+)?="[^"]+"', "", xml_text, count=0)
        cleaned = re.sub(r"<(/?)\w+:", r"<\1", cleaned)
        root = ET.fromstring(cleaned)
    except Exception as e:
        print(f"  [sec] xml parse fail: {e}", file=sys.stderr)
        return out

    def _ftext(path: str) -> str:
        el = root.find(f".//{path}")
        return (el.text or "").strip() if el is not None and el.text else ""

    def _ffloat(path: str):
        s = _ftext(path)
        if not s:
            return None
        try:
            return float(s)
        except (TypeError, ValueError):
            return None

    out["total_offering_amount"] = _ffloat("totalOfferingAmount")
    out["total_amount_sold"]     = _ffloat("totalAmountSold")
    out["date_of_first_sale"]    = _ftext("dateOfFirstSale")
    try:
        ex_nodes = root.findall(".//exemption")
        out["exemptions"] = [
            (n.text or "").strip() for n in ex_nodes if n.text and n.text.strip()
        ]
    except Exception:
        out["exemptions"] = []
    return out


def _fetch_sec_form_d_filings_impl(
    days: int = 60,
    max_results: int = 20,
    enrich_details: bool = True,
) -> list[dict]:
    """Pull recent Form D filings from EDGAR, filter to AI-adjacent issuers,
    optionally enrich each with offering-amount fields from primary_doc.xml.

    Steps:
      1. One full-text search request: `forms=D&dateRange=custom&startdt=...`
         returns up to 100 hits in chronological order (newest first).
      2. Filter hits by AI keywords in the issuer display_name.
      3. Take the top `max_results`. If `enrich_details` is True, fetch
         each filing's primary_doc.xml (with a 0.15s gap between requests
         to stay below SEC's 10 req/sec ceiling).

    Returns a list of dicts ready for the AI tab renderer.
    """
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    params = {
        "q": "",
        "forms": "D",
        "dateRange": "custom",
        "startdt": start.isoformat(),
        "enddt": end.isoformat(),
    }
    j = _sec_get("https://efts.sec.gov/LATEST/search-index", params=params)
    if not isinstance(j, dict):
        return []
    hits = (((j.get("hits") or {}).get("hits")) or [])
    rows: list[dict] = []
    for h in hits:
        src = h.get("_source") or {}
        names = src.get("display_names") or []
        # display_names entries look like "Issuer Name  (CIK 0001234567)
        # (Filer)". Strip the trailing parens for the matcher and the
        # rendered name.
        primary = names[0] if names else ""
        # Get the bare name without the (CIK ...) suffix.
        clean_name = primary.split("(CIK")[0].strip() if primary else ""
        if not clean_name:
            continue
        if not _ai_keyword_hit(clean_name):
            continue
        # adsh on full-text search hits arrives as the bare _id, like
        # "0001234567-25-000123:primary_doc.xml" — the part before the colon
        # is the accession number.
        raw_id = h.get("_id") or ""
        adsh = raw_id.split(":", 1)[0] if raw_id else ""
        ciks = src.get("ciks") or []
        cik = ciks[0] if ciks else ""
        file_date = src.get("file_date") or ""
        rows.append({
            "issuer": clean_name,
            "cik": cik,
            "accession": adsh,
            "filing_url": _sec_accession_url(cik, adsh) if cik and adsh else "",
            "filed_date": file_date,
            "form": src.get("form") or "D",
            # Filled in by the enrichment pass below (or left as defaults).
            "total_offering_amount": None,
            "total_amount_sold": None,
            "date_of_first_sale": "",
            "exemptions": [],
        })
        if len(rows) >= max_results:
            break

    if enrich_details and rows:
        for row in rows:
            if not row.get("cik") or not row.get("accession"):
                continue
            url = _sec_primary_doc_url(row["cik"], row["accession"])
            xml_text = _sec_get(url)
            # Be polite — sleep 0.15s between filing fetches (~6 req/sec
            # ceiling, well under SEC's 10 req/sec limit).
            time.sleep(0.15)
            if not isinstance(xml_text, str):
                continue
            parsed = _parse_form_d_xml(xml_text)
            row.update(parsed)

    return rows


def fetch_sec_form_d_filings(
    days: int = 60,
    max_results: int = 20,
    enrich_details: bool = True,
) -> list[dict]:
    """Stale-fallback wrapper around `_fetch_sec_form_d_filings_impl`.

    EDGAR will 403 if the User-Agent is missing or transiently slow during
    business hours; preserve the prior good result so the AI tab never goes
    blank on a single failed sweep."""
    try:
        out = _fetch_sec_form_d_filings_impl(days, max_results, enrich_details)
    except Exception as e:
        print(f"  [fetch_sec_form_d_filings] fatal {e}", file=sys.stderr)
        out = None
    if isinstance(out, list) and out:
        _stale_save("fetch_sec_form_d_filings", out)
        return out
    cached = _stale_load("fetch_sec_form_d_filings")
    if isinstance(cached, list):
        return cached
    return out if isinstance(out, list) else []


def fetch_ai_funding() -> dict:
    """Orchestrator: pull live YC AI directory + HN funding news + SEC Form
    D AI filings + load curated snapshot. Stored on `market.ai_funding`.
    """
    print("  AI funding: YC AI directory (yc-oss)...")
    yc = fetch_yc_ai_companies(200)
    print(f"    -> {len(yc.get('yc_companies', []))} YC companies "
          f"(total tagged: {yc.get('yc_total_ai_count', 0)})")
    print("  AI funding: HN 'raises Series' (filtered for AI)...")
    hn_news = fetch_ai_funding_news_hn(30, 40)
    print(f"    -> {len(hn_news)} HN funding stories")
    print("  AI funding: SEC EDGAR Form D (AI issuers, last 60d)...")
    form_d = fetch_sec_form_d_filings(60, 20, True)
    print(f"    -> {len(form_d)} Form D filings (AI-adjacent)")
    return {
        "yc_companies": yc.get("yc_companies", []),
        "yc_total_ai_count": yc.get("yc_total_ai_count", 0),
        "recent_funding_news": hn_news,
        "form_d_filings": form_d,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


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


def mempool_whale_transactions(btc_price_usd: float | None,
                                threshold_usd: float = 1_000_000,
                                n_blocks: int = 1,
                                max_per_block: int = 200) -> list[dict]:
    """Scan the latest confirmed BTC block(s) for txs with any single vout
    above `threshold_usd`. Returns top 20 by USD value.

    mempool.space caveats:
      - txs in a block come ordered by mining priority (fee/byte), not value,
        so a low-fee whale tx late in the block can fall outside max_per_block.
        The default 200 covers ~90% of typical ~3k-tx blocks.
      - Coinbase txs are filtered — they aren't whale movements.
    """
    if not btc_price_usd or btc_price_usd <= 0:
        return []
    threshold_sats = int((threshold_usd / btc_price_usd) * 1e8)
    blocks = _get("https://mempool.space/api/v1/blocks")
    if not blocks or not isinstance(blocks, list):
        return []
    out: list[dict] = []
    for blk in blocks[:n_blocks]:
        bhash = blk.get("id")
        if not bhash:
            continue
        for start in range(0, max_per_block, 25):
            txs = _get(f"https://mempool.space/api/block/{bhash}/txs/{start}")
            if not txs or not isinstance(txs, list):
                break
            for tx in txs:
                if not isinstance(tx, dict):
                    continue
                vins = tx.get("vin") or []
                if vins and vins[0].get("is_coinbase"):
                    continue
                vouts = tx.get("vout") or []
                max_sats = max((vo.get("value") or 0 for vo in vouts), default=0)
                if max_sats >= threshold_sats:
                    out.append({
                        "txid": tx.get("txid"),
                        "value_btc": round(max_sats / 1e8, 4),
                        "value_usd": round(max_sats / 1e8 * btc_price_usd, 0),
                        "block_height": blk.get("height"),
                        "block_time": blk.get("timestamp"),
                    })
            if len(txs) < 25:
                break
    out.sort(key=lambda x: x["value_usd"], reverse=True)
    return out[:20]


def _mempool_space_impl() -> dict:
    """Live mempool.space fetch — fees, hashrate, tip. See `mempool_space`."""
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


def mempool_space() -> dict:
    """mempool.space: BTC mempool fees, 3y hashrate series, current tip height.

    Wraps `_mempool_space_impl` with a stale-fallback. If the live fetch
    returns nothing meaningful (only `fetched_at`), serve the last good
    payload from `data/.stale/mempool_space.json` tagged with
    ``{"stale": True, "stale_age_sec": N}``.
    """
    try:
        out = _mempool_space_impl()
    except Exception as e:
        print(f"  [mempool_space] fatal: {e}", file=sys.stderr)
        out = None
    if not _is_empty_result(out):
        _stale_save("mempool_space", out)
        return out
    cached = _stale_load("mempool_space")
    return cached if cached is not None else (out or {})


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
    except Exception as e:
        print(f"  [stale-keep] social.{key} suppressed: {type(e).__name__}", file=sys.stderr)
    return default


# ----- generic stale-fallback for flaky source fetchers ----------------------

_STALE_DIR = CACHE / ".stale"


def _stale_path(funcname: str) -> Path:
    return _STALE_DIR / f"{funcname}.json"


def _stale_save(funcname: str, value) -> None:
    """Persist a successful fetcher return for later stale-fallback use.
    Silently no-ops on disk errors — never let cache writes break a fetch."""
    try:
        _STALE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "saved_at": int(time.time()),
            "value": value,
        }
        _stale_path(funcname).write_text(json.dumps(payload))
    except Exception as e:
        print(f"  [stale-save] {funcname}: {e}", file=sys.stderr)


def _stale_load(funcname: str):
    """Load the last successful return for `funcname`, tagged with stale
    metadata. Returns ``None`` if no cache exists or it's unreadable.

    For dict return values the tags `{"stale": True, "stale_age_sec": N}`
    are merged in. For non-dict types the raw value is returned untagged
    so callers can decide how to surface staleness."""
    try:
        p = _stale_path(funcname)
        if not p.exists():
            return None
        payload = json.loads(p.read_text())
        saved_at = int(payload.get("saved_at") or 0)
        age = max(0, int(time.time()) - saved_at) if saved_at else 0
        value = payload.get("value")
        if isinstance(value, dict):
            value = dict(value)  # shallow copy so we don't mutate cache
            value["stale"] = True
            value["stale_age_sec"] = age
        print(f"  [stale-load] {funcname}: serving cached value (age {age}s)", file=sys.stderr)
        return value
    except Exception as e:
        print(f"  [stale-load] {funcname}: {e}", file=sys.stderr)
        return None


def _is_empty_result(value) -> bool:
    """Heuristic for 'fetcher returned nothing useful'. Dicts that only carry
    timestamp/availability flags count as empty so we'd rather serve stale."""
    if value is None:
        return True
    if isinstance(value, (list, tuple, set, str)):
        return len(value) == 0
    if isinstance(value, dict):
        meaningful = {k: v for k, v in value.items()
                      if k not in ("fetched_at", "available", "reason")}
        if not meaningful:
            return True
        # All-empty sub-collections also count as empty.
        return all(
            (v is None) or (isinstance(v, (list, dict, str)) and len(v) == 0)
            for v in meaningful.values()
        )
    return False


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
    api_key = os.environ.get("CRYPTOCOMPARE_API_KEY", "")
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


# --- Per-coin CC news scoring for the Research-tab Top-25 card -------------
#
# The RSS pipeline (`crypto_news_rss` + frontend `groupNewsBySymbol`) only
# matches ~14 of the top-25 coins because the 5 RSS feeds we pull rarely name
# the long-tail alt-coins (FIGR_HELOC, WBT, USDS, LEO, XMR, TON, XLM, DAI,
# LTC, USD1, etc.). CryptoCompare's data-api news endpoint accepts a
# `categories=<COIN>` param and returns up to 50 articles tagged TO that
# coin, regardless of which publisher wrote them. Fanning out 25 calls (one
# per top-25 symbol) and aggregating per-coin sentiment server-side lifts
# coverage substantially and avoids shipping ~1,250 raw articles to the
# browser.
#
# We deliberately re-port the frontend keyword lists rather than reuse
# `_AI_NEWS_POSITIVE_KEYWORDS` — the JS `_NEWS_POS_KEYWORDS` /
# `_NEWS_NEG_KEYWORDS` are tuned for crypto (rally/surge/inflows vs
# crash/dump/liquidation) not AI launches/funding. Keeping the two lists
# IDENTICAL across Python and JS is the whole point: the merged counts must
# agree with whatever the JS would have produced if it scored the same items.
_NEWS_POS_KEYWORDS_PER_COIN = (
    "rally", "surge", "soars", "soar", "jumps", "jump", "gains", "gain",
    "breakout", "breakthrough", "launches", "launch", "partnership", "adopts",
    "adoption", "approves", "approved", "approval", "wins", "win", "milestone",
    "record", "all-time high", "ath", "bullish", "upgrade", "upgraded",
    "beats", "inflows", "inflow", "buys", "accumulate", "accumulation",
    "recovery", "rebounds", "rebound", "outperform", "green", "institutional",
    "etf approval",
)
_NEWS_NEG_KEYWORDS_PER_COIN = (
    "hack", "hacked", "exploit", "exploited", "lawsuit", "sued", "sec ", "fine",
    "crash", "plunge", "plunges", "dump", "dumps", "tumbles", "tumble", "sinks",
    "sink", "slide", "slides", "falls", "fall", "loses", "loss", "losses",
    "fraud", "investigation", "probe", "ban", "banned", "banning", "breach",
    "leak", "leaked", "outage", "down", "bearish", "liquidation", "liquidated",
    "rejected", "rejection", "denied", "sell-off", "selloff", "crashes",
    "crackdown", "sanction", "sanctioned", "rug", "scam", "theft", "stolen",
    "delisting", "delisted", "outflows", "outflow", "warning", "warns",
)


def _score_news_item_sentiment(item: dict) -> str:
    """Port of the JS `scoreNewsItemSentiment` in app.py. POSITIVE iff ≥1
    positive keyword hit and 0 negative hits, NEGATIVE iff the reverse,
    otherwise NEUTRAL. Lower-cases title+body before substring-matching.
    Keep keyword lists in sync with the JS `_NEWS_POS_KEYWORDS` /
    `_NEWS_NEG_KEYWORDS` constants in app.py.
    """
    title = (item.get("title") or "") if isinstance(item, dict) else ""
    body = (item.get("body") or "") if isinstance(item, dict) else ""
    text = (str(title) + " " + str(body)).lower()
    has_pos = any(kw in text for kw in _NEWS_POS_KEYWORDS_PER_COIN)
    has_neg = any(kw in text for kw in _NEWS_NEG_KEYWORDS_PER_COIN)
    if has_pos and not has_neg:
        return "POSITIVE"
    if has_neg and not has_pos:
        return "NEGATIVE"
    return "NEUTRAL"


def _fetch_cc_per_coin_news_impl(
    coins: list[dict],
    *,
    per_coin_limit: int = 50,
    sleep_between: float = 0.06,
) -> dict:
    """Hit CryptoCompare's `/news/v1/article/list?categories=<SYM>` for each
    coin and aggregate per-coin sentiment counts using the same keyword
    scorer the frontend uses for RSS items. Designed to fan out to ~25 calls
    (top-25 by mcap) — well within CC's free-tier rate limit (~50 calls/sec).

    The endpoint is keyless for low-volume use, but we pass `Authorization`
    if `CRYPTOCOMPARE_API_KEY` is set so we benefit from the higher quota.

    Returns `{symbol_upper: {symbol, name, total, positive, negative,
    neutral, net_score, recent: [{title, url, source, date, sentiment, ts}],
    article_count}}`. Symbols with zero matched articles are omitted so the
    frontend can cheaply check `if (cc[sym])`.
    """
    import os
    api_key = os.environ.get("CRYPTOCOMPARE_API_KEY", "").strip()
    headers = dict(H)
    if api_key:
        headers["Authorization"] = f"Apikey {api_key}"
    out: dict[str, dict] = {}
    # CC doesn't tag every CG-listed coin. Categories the endpoint rejects
    # come back as HTTP 400 "Category ... does not exist" — we log + skip.
    for c in coins or []:
        if not isinstance(c, dict):
            continue
        sym = (c.get("symbol") or "").upper().strip()
        name = c.get("name") or ""
        if not sym:
            continue
        category = sym
        try:
            params = {"lang": "EN", "categories": category, "limit": per_coin_limit}
            r = requests.get(
                "https://data-api.cryptocompare.com/news/v1/article/list",
                params=params, headers=headers, timeout=15,
            )
            if r.status_code != 200:
                # 400 = unknown category (e.g. FIGR_HELOC, USDS, CC, USD1),
                # 401/429 = auth/rate-limit. All non-fatal per-coin.
                print(f"  [cc-per-coin-news] {sym} -> {r.status_code}", file=sys.stderr)
                continue
            body = r.json() or {}
            arts = body.get("Data") or []
            if not arts:
                continue
            pos = neg = neu = 0
            recent: list[dict] = []
            for a in arts:
                if not isinstance(a, dict):
                    continue
                # CC payload field names differ from our RSS schema; remap so
                # the scorer (which reads .title/.body) works directly.
                item = {
                    "title": (a.get("TITLE") or "")[:240],
                    "body":  (a.get("BODY")  or "")[:280],
                }
                label = _score_news_item_sentiment(item)
                if   label == "POSITIVE": pos += 1
                elif label == "NEGATIVE": neg += 1
                else:                     neu += 1
                if len(recent) < 5:
                    src = ((a.get("SOURCE_DATA") or {}).get("NAME")) or a.get("SOURCE_ID") or ""
                    ts = a.get("PUBLISHED_ON")
                    date_str = ""
                    if isinstance(ts, (int, float)) and ts > 0:
                        date_str = datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d %H:%M")
                    recent.append({
                        "title": item["title"],
                        "url": a.get("URL") or "",
                        "source": src,
                        "date": date_str,
                        "ts": int(ts) if isinstance(ts, (int, float)) else None,
                        "sentiment": label,
                        "body": item["body"],
                    })
            total = pos + neg + neu
            if total == 0:
                continue
            out[sym] = {
                "symbol": sym,
                "name": name,
                "total": total,
                "positive": pos,
                "negative": neg,
                "neutral": neu,
                "net_score": pos - neg,
                "recent": recent,
                "article_count": len(arts),
            }
        except Exception as e:
            print(f"  [cc-per-coin-news] {sym} error: {e}", file=sys.stderr)
        if sleep_between:
            time.sleep(sleep_between)
    return {
        "available": bool(out),
        "coins": out,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def fetch_cc_per_coin_news(markets_top: list[dict], top_n: int = 25) -> dict:
    """Stale-fallback wrapper. Slices `markets_top` to the top-N coins (by
    list order — markets_top is already mcap-sorted) and asks CC for per-
    coin sentiment. On total failure (no key + 4xx storm, or network down)
    falls back to the last successful run so the frontend still gets counts.
    """
    coins = (markets_top or [])[:top_n]
    cache_key = "fetch_cc_per_coin_news"
    try:
        out = _fetch_cc_per_coin_news_impl(coins)
    except Exception as e:
        print(f"  [fetch_cc_per_coin_news] fatal {e}", file=sys.stderr)
        out = None
    if isinstance(out, dict) and out.get("available") and out.get("coins"):
        _stale_save(cache_key, out)
        return out
    cached = _stale_load(cache_key)
    if cached is not None:
        return cached
    return out if isinstance(out, dict) else {
        "available": False,
        "coins": {},
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
        except Exception as e:
            print(f"  [stale-keep] santiment daily-gate suppressed: {type(e).__name__}", file=sys.stderr)
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
                        "migration_series": poc_migration_series(prices, volumes),
                        "naked": naked_pocs(prices, volumes, lookback_days=180)}
    return out


def _cryptocompare_market_impl(symbol: str, days: int = 180) -> dict:
    """Daily OHLCV from CryptoCompare histoday. No key required for basic
    usage; honors CRYPTOCOMPARE_API_KEY env var if set. Same shape as
    coingecko_market — {price, volume} where each is [{date, value}]."""
    sym = (symbol or "").upper()
    if not sym or len(sym) > 12:
        return {"price": [], "volume": []}
    days = max(1, min(days, 2000))
    import os
    headers = dict(H)
    key = os.environ.get("CRYPTOCOMPARE_API_KEY", "").strip()
    if key:
        headers["authorization"] = f"Apikey {key}"
    try:
        r = requests.get(
            "https://min-api.cryptocompare.com/data/v2/histoday",
            params={"fsym": sym, "tsym": "USD", "limit": str(days)},
            timeout=15, headers=headers,
        )
        if r.status_code != 200:
            return {"price": [], "volume": []}
        j = r.json()
    except Exception:
        return {"price": [], "volume": []}
    if not isinstance(j, dict) or j.get("Response") != "Success":
        return {"price": [], "volume": []}
    rows = (j.get("Data") or {}).get("Data") or []
    prices, volumes = [], []
    for row in rows:
        try:
            t = int(row.get("time"))
            close = float(row.get("close"))
            volto = float(row.get("volumeto") or 0)  # USD volume
            if close <= 0:
                continue
            date = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")
            prices.append({"date": date, "value": close})
            volumes.append({"date": date, "value": volto})
        except (ValueError, TypeError, KeyError):
            continue
    return {"price": prices, "volume": volumes}


def cryptocompare_market(symbol: str, days: int = 180) -> dict:
    """Stale-fallback wrapper around `_cryptocompare_market_impl`.

    CryptoCompare's free tier can rate-limit or briefly 5xx during top-N
    sweeps. If the price array comes back empty we fall back to the cached
    prior result for this exact ``symbol`` so the section isn't blanked by
    a single transient failure. Mirrors the `coingecko_market` pattern.
    """
    sym = (symbol or "").upper()
    cache_key = f"cryptocompare_market_{sym}"
    try:
        out = _cryptocompare_market_impl(symbol, days)
    except Exception as e:
        print(f"  [cryptocompare_market] {sym}: fatal {e}", file=sys.stderr)
        out = None
    # Empty price array == failed fetch; everything else == success.
    if isinstance(out, dict) and out.get("price"):
        _stale_save(cache_key, out)
        return out
    cached = _stale_load(cache_key)
    if cached is not None:
        return cached
    return out if isinstance(out, dict) else {"price": [], "volume": []}


def compute_poc_top_markets(top_markets: list[dict], n: int = 25,
                             days: int = 180) -> list[dict]:
    """Fetch market_chart and compute multi-timeframe POC + migration + naked
    POCs for the top `n` coins by market cap. Used by the "Top 25 POC" UI.

    Calls `coingecko_market(coin_id, days)` per coin with CG_PACE spacing so
    we don't trip CoinGecko's free-tier rate limit (~30 calls/min). Skips
    coins whose price/volume series come back empty or whose POC compute
    yields nothing usable.

    Returns up to `n` entries with the schema the dashboard expects:
        {coin_id, symbol, name, image, current_price,
         poc: {d30, d90, d180, migration, naked, migration_series}}
    """
    if not top_markets:
        return []
    # Binance public klines API has ~1200 req/min limits with no key, so this
    # comfortably covers a top-25 sweep. CoinGecko's 30/min free tier was
    # hitting 429 on most coins given the rest of fetch_trading already burns
    # ~10 calls. Stablecoins and unlisted-on-Binance coins fall back to the
    # stale cache from the previous run.
    out: list[dict] = []
    coins = top_markets[:n]
    LOOKBACKS = (("d30", 30, 60), ("d90", 90, 80), ("d180", 180, 100))
    # Build a stale-keep map from the previous market.json so any coin we
    # fail to fetch this run keeps its last good POC entry.
    stale_map: dict[str, dict] = {}
    try:
        prev = json.loads((CACHE / "market.json").read_text())
        for e in (prev.get("poc_top") or []):
            cid = e.get("coin_id")
            if cid:
                stale_map[cid] = e
    except Exception as e:
        print(f"  [stale-keep] poc_top stale map suppressed: {type(e).__name__}", file=sys.stderr)
    for c in coins:
        coin_id = c.get("id")
        symbol = (c.get("symbol") or "").upper()
        if not coin_id or not symbol:
            continue
        m = cryptocompare_market(symbol, days=days)
        prices = (m or {}).get("price") or []
        volumes = (m or {}).get("volume") or []
        if not prices or not volumes:
            if coin_id in stale_map:
                stale = dict(stale_map[coin_id])
                stale["stale"] = True
                out.append(stale)
            continue
        tfs = {k: point_of_control(prices, volumes, lookback_days=lb, bins=b)
               for k, lb, b in LOOKBACKS}
        if not any(tfs.values()):
            if coin_id in stale_map:
                stale = dict(stale_map[coin_id])
                stale["stale"] = True
                out.append(stale)
            continue
        # Build a date-aligned closes/volumes pair so we can compute the
        # same rolling -100..+100 score the stocks breadth chart uses.
        # Intersection on date matches the convention in poc_migration_series
        # and naked_pocs above.
        p_by = {p.get("date"): p.get("value") for p in prices
                if p.get("date") and p.get("value") is not None}
        v_by = {v.get("date"): v.get("value") for v in volumes
                if v.get("date") and v.get("value") is not None}
        common = sorted(set(p_by) & set(v_by))
        aligned_closes = [float(p_by[d]) for d in common]
        aligned_vols   = [float(v_by[d]) for d in common]
        signal_history = _signal_history_from_prices(
            aligned_closes, aligned_vols, common, days=90,
        )
        entry = {
            "coin_id":       coin_id,
            "symbol":        (c.get("symbol") or "").upper(),
            "name":          c.get("name"),
            "image":         c.get("image"),
            "current_price": c.get("price_usd"),
            "poc": {
                **tfs,
                "migration":        compute_poc_migration(tfs.get("d30"), tfs.get("d90")),
                "naked":            naked_pocs(prices, volumes, lookback_days=180),
                "migration_series": poc_migration_series(prices, volumes),
            },
            "signal_history": signal_history,
        }
        out.append(entry)
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


def poc_migration_series(price_series: list[dict], volume_series: list[dict],
                          lookback_days: int = 90, window_days: int = 30,
                          bins: int = 60) -> list[dict]:
    """Rolling 30d POC computed for each day across the last 90 days, so the
    UI can sparkline how the value-area centroid has migrated over time.
    Returns [{date, poc}] sorted ascending."""
    if not price_series or not volume_series:
        return []
    p_by = {p.get("date"): p.get("value") for p in price_series if p.get("date") and p.get("value")}
    v_by = {v.get("date"): v.get("value") for v in volume_series if v.get("date") and v.get("value")}
    common = sorted(set(p_by) & set(v_by))
    if len(common) < window_days + 1:
        return []
    out: list[dict] = []
    start_idx = max(window_days - 1, len(common) - lookback_days)
    for i in range(start_idx, len(common)):
        ws_p = [p_by[d] for d in common[i - window_days + 1:i + 1]]
        ws_v = [v_by[d] for d in common[i - window_days + 1:i + 1]]
        lo, hi = min(ws_p), max(ws_p)
        if hi <= lo:
            continue
        step = (hi - lo) / bins
        buckets = [0.0] * bins
        for p, v in zip(ws_p, ws_v):
            idx = min(int((p - lo) / step), bins - 1)
            buckets[idx] += v
        poc_idx = max(range(bins), key=lambda k: buckets[k])
        out.append({"date": common[i], "poc": round(lo + (poc_idx + 0.5) * step, 2)})
    return out


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


def compute_whale_sentiment(whale: dict) -> dict | None:
    """Composite ±100 whale-sentiment score from existing BTC on-chain
    proxies (no new API calls). Six components, drawing on Glassnode-style
    framing translated to free data:

      ±20  Whale supply Δ30d (bitinfocharts cohorts, ≥1K BTC addresses)
      ±20  Hash rate vs 30d mean (miner confidence proxy)
      ±15  Miner revenue vs 30d mean (selling-pressure inverse)
      ±15  Avg tx USD z-score(30d) (larger-ticket flow = whale-shaped)
      ±15  Output volume BTC z-score(30d) (large-tx proxy, no `large_tx` field)
      ±15  Active addresses vs 30d mean (broad usage breadth)

    Returns same shape as signals.compute_signal (score, label,
    components, as_of, disclaimer) so the existing UI patterns work.
    Returns None if data is too thin to compute.
    """
    if not isinstance(whale, dict):
        return None
    btc = whale.get("btc") or {}
    dist = (whale.get("distribution") or {}).get("buckets") or []
    if len(dist) < 31:
        return None

    def _last_values(series: list[dict], n: int) -> list[float]:
        vals = [r.get("value") for r in (series or []) if isinstance(r, dict) and r.get("value") is not None]
        return vals[-n:]

    def _mean(arr: list[float]) -> float:
        return sum(arr) / len(arr) if arr else 0.0

    def _std(arr: list[float]) -> float:
        if not arr:
            return 1.0
        m = _mean(arr)
        var = _mean([(x - m) ** 2 for x in arr])
        return (var ** 0.5) or 1.0

    def _pct_vs_mean30(name: str) -> float | None:
        v = _last_values(btc.get(name) or [], 31)
        if len(v) < 31:
            return None
        history, today = v[:-1], v[-1]
        m = _mean(history)
        return ((today - m) / m * 100) if m else None

    def _z30(name: str) -> float | None:
        v = _last_values(btc.get(name) or [], 31)
        if len(v) < 31:
            return None
        history, today = v[:-1], v[-1]
        m = _mean(history)
        s = _std(history)
        return (today - m) / s if s else None

    def _clamp(x: float, lo: float, hi: float) -> int:
        return int(max(lo, min(hi, round(x))))

    def _whale_supply(row: dict) -> float:
        return (row.get("b1k_10k", 0) + row.get("b10k_100k", 0) + row.get("b100k_1m", 0))

    comps: list[dict] = []
    def add(name: str, value: str, c: int, explanation: str):
        comps.append({"name": name, "value": value,
                      "contribution": int(c), "explanation": explanation})

    # 1) Whale supply 30d Δ — ±20 saturates at ±1%
    sup_now = _whale_supply(dist[-1])
    sup_30 = _whale_supply(dist[-31])
    if sup_30:
        sup_delta = (sup_now - sup_30) / sup_30 * 100
        c = _clamp(sup_delta / 1.0 * 20, -20, 20)
        add("Whale supply Δ30d", f"{sup_delta:+.2f}%", c,
            "whales accumulating" if c > 0 else "whales distributing" if c < 0 else "flat")

    # 2) Hash rate vs 30d mean — ±20 saturates at ±10%
    hr = _pct_vs_mean30("hash_rate")
    if hr is not None:
        c = _clamp(hr / 10 * 20, -20, 20)
        add("Hash rate vs 30d", f"{hr:+.1f}%", c,
            "miner confidence rising" if c > 0 else "miners capitulating" if c < 0 else "flat")

    # 3) Miner revenue vs 30d mean — ±15 saturates at ±15%
    mr = _pct_vs_mean30("miners_revenue_usd")
    if mr is not None:
        c = _clamp(mr / 15 * 15, -15, 15)
        add("Miner revenue vs 30d", f"{mr:+.1f}%", c,
            "miners under pressure" if c < 0 else "miner income healthy" if c > 0 else "flat")

    # 4) Avg tx USD z-score(30d) — ±15 saturates at ±2σ
    az = _z30("avg_tx_usd")
    if az is not None:
        c = _clamp(az / 2 * 15, -15, 15)
        add("Avg tx USD z30", f"{az:.2f}σ", c,
            "larger-ticket flow (whale-shaped)" if c > 0 else "smaller-ticket flow")

    # 5) Output volume BTC z-score(30d) — large-tx proxy
    oz = _z30("output_volume_btc")
    if oz is not None:
        c = _clamp(oz / 2 * 15, -15, 15)
        add("Output vol z30", f"{oz:.2f}σ", c,
            "on-chain BTC movement spike" if c > 0 else "quiet on-chain")

    # 6) Active addresses vs 30d mean — ±15 saturates at ±15%
    aa = _pct_vs_mean30("active_addresses")
    if aa is not None:
        c = _clamp(aa / 15 * 15, -15, 15)
        add("Active addr vs 30d", f"{aa:+.1f}%", c,
            "broad usage uptick" if c > 0 else "usage softening")

    if not comps:
        return None

    score = max(-100, min(100, sum(x["contribution"] for x in comps)))
    if   score >=  50: label = "STRONG WHALE BUY"
    elif score >=  20: label = "WHALE ACCUMULATION"
    elif score >  -20: label = "NEUTRAL"
    elif score >  -50: label = "WHALE DISTRIBUTION"
    else:              label = "STRONG WHALE DUMP"

    return {
        "score": int(score),
        "label": label,
        "components": comps,
        "as_of": (whale.get("fetched_at") or "")[:10],
        "disclaimer": ("Proxy composite from free blockchain.info + bitinfocharts "
                       "cohorts. Not a Glassnode metric — directional indicator, "
                       "not a trading signal."),
    }


def compute_whale_sentiment_eth(whale: dict) -> dict | None:
    """ETH parallel of ``compute_whale_sentiment`` — composite ±100 whale-
    sentiment score from existing ETH on-chain proxies (no new API calls).

    Components (each saturates at ±2σ over a 30-day baseline):

      ±25  Active addresses z-score(30d)        (Coin Metrics AdrActCnt)
      ±25  Transactions per day z-score(30d)    (Coin Metrics TxCnt)
      ±25  Transfer volume USD z-score(30d)     (Coin Metrics TxTfrValAdjUSD;
                                                  community-tier may omit)
      ±25  Blocks per day vs 7200 (post-Merge)  (Etherscan daily series)

    Output dict shape is identical to ``compute_whale_sentiment`` so the
    same renderer pattern can be reused. Returns ``None`` (or an empty-
    state marker) when data is too thin to compute any component.
    """
    if not isinstance(whale, dict):
        return None
    eth = whale.get("eth") or {}
    cm = eth.get("coin_metrics") or {}
    eds = eth.get("etherscan_daily") or {}

    def _last_values(series: list[dict], n: int) -> list[float]:
        vals = [
            r.get("value") for r in (series or [])
            if isinstance(r, dict) and r.get("value") is not None
        ]
        return vals[-n:]

    def _mean(arr: list[float]) -> float:
        return sum(arr) / len(arr) if arr else 0.0

    def _std(arr: list[float]) -> float:
        if not arr:
            return 1.0
        m = _mean(arr)
        var = _mean([(x - m) ** 2 for x in arr])
        return (var ** 0.5) or 1.0

    def _z30(series: list[dict]) -> tuple[float | None, float | None]:
        v = _last_values(series, 31)
        if len(v) < 31:
            return None, None
        history, today = v[:-1], v[-1]
        m = _mean(history)
        s = _std(history)
        if not s:
            return None, today
        return (today - m) / s, today

    def _clamp(x: float, lo: float, hi: float) -> int:
        return int(max(lo, min(hi, round(x))))

    comps: list[dict] = []

    def add(name: str, value: str, c: int, explanation: str):
        comps.append({
            "name": name, "value": value,
            "contribution": int(c), "explanation": explanation,
        })

    # 1) Active addresses z-score(30d) — ±25 saturates at ±2σ
    aa_z, aa_now = _z30(cm.get("AdrActCnt") or [])
    if aa_z is not None:
        c = _clamp(aa_z / 2 * 25, -25, 25)
        add("Active addr z30", f"{aa_z:.2f}σ", c,
            "demand picking up" if c > 0 else "demand softening" if c < 0 else "flat")

    # 2) Tx count z-score(30d) — ±25 saturates at ±2σ
    tx_z, tx_now = _z30(cm.get("TxCnt") or [])
    if tx_z is not None:
        c = _clamp(tx_z / 2 * 25, -25, 25)
        add("Tx count z30", f"{tx_z:.2f}σ", c,
            "network activity rising" if c > 0 else "network quieter")

    # 3) Transfer volume USD z-score(30d) — Coin Metrics paid metric, may be
    #    absent on the community tier (the fetcher silently drops it). Still
    #    try both the canonical and friendly keys.
    vol_series = cm.get("TxTfrValAdjUSD") or cm.get("transfer_volume_usd") or []
    vol_z, vol_now = _z30(vol_series)
    if vol_z is not None:
        c = _clamp(vol_z / 2 * 25, -25, 25)
        add("Transfer vol USD z30", f"{vol_z:.2f}σ", c,
            "economic throughput rising" if c > 0 else "economic throughput cooling")

    # 4) Blocks per day vs the post-Merge 7,200 target — well above = network
    #    saturated by demand, well below = soft demand or proposer issues.
    eds_series = eds.get("series") if isinstance(eds, dict) else None
    bp_vals = _last_values(eds_series or [], 7)
    if bp_vals:
        bp_avg = _mean(bp_vals)
        TARGET = 7200.0
        bp_pct = (bp_avg - TARGET) / TARGET * 100
        # ±25 saturates at ±2% deviation from target (blocks/day is tight)
        c = _clamp(bp_pct / 2 * 25, -25, 25)
        add("Blocks/day vs 7200", f"{bp_pct:+.2f}%", c,
            "demand saturating slots" if c > 0 else "slots underused" if c < 0 else "at target")

    if not comps:
        return {
            "available": False,
            "score": 0,
            "label": "NO DATA",
            "components": [],
            "as_of": (whale.get("fetched_at") or "")[:10],
            "disclaimer": "Not enough ETH on-chain data to compute sentiment yet.",
        }

    score = max(-100, min(100, sum(x["contribution"] for x in comps)))
    if   score >=  50: label = "STRONG WHALE BUY"
    elif score >=  20: label = "WHALE ACCUMULATION"
    elif score >  -20: label = "NEUTRAL"
    elif score >  -50: label = "WHALE DISTRIBUTION"
    else:              label = "STRONG WHALE DUMP"

    return {
        "available": True,
        "score": int(score),
        "label": label,
        "components": comps,
        "as_of": (whale.get("fetched_at") or "")[:10],
        "disclaimer": ("Proxy composite from free Coin Metrics community + "
                       "Etherscan daily series. Directional indicator, not a "
                       "trading signal. ETH-specific whale cohorts (≥10K ETH "
                       "addresses) require a paid feed."),
    }


async def _fetch_social_async() -> dict:
    """Concurrent implementation of ``fetch_social``. Schema-identical to
    the sequential version; just runs the 4 independent sub-fetchers in
    parallel via ``asyncio.gather`` + ``asyncio.to_thread``. They hit 4
    different domains (reddit.com, min-api.cryptocompare.com,
    data-api.cryptocompare.com, api.santiment.net), so there's no shared
    rate-limit contention — wall time collapses to max(durations)."""
    print("    [social] reddit + cryptocompare + cc-news + santiment in parallel...")

    def _reddit() -> dict:
        try:
            return reddit_crypto_stats()
        except Exception as e:
            print(f"  [reddit] fatal: {e}", file=sys.stderr)
            return {"available": False, "reason": "fetch_error", "subreddits": {}}

    def _cc() -> dict:
        try:
            return cryptocompare_social_stats()
        except Exception as e:
            print(f"  [cryptocompare] fatal: {e}", file=sys.stderr)
            return {"available": False, "reason": "fetch_error", "coins": {}}

    def _ccnews() -> dict:
        try:
            return cryptocompare_news_sentiment()
        except Exception as e:
            print(f"  [cc-news] fatal: {e}", file=sys.stderr)
            return {"available": False, "reason": "fetch_error", "coins": {}}

    def _san() -> dict:
        try:
            return santiment_metrics()
        except Exception as e:
            print(f"  [santiment] fatal: {e}", file=sys.stderr)
            return {"available": False, "reason": "fetch_error", "coins": {}}

    reddit, cc, cc_news, san = await asyncio.gather(
        asyncio.to_thread(_reddit),
        asyncio.to_thread(_cc),
        asyncio.to_thread(_ccnews),
        asyncio.to_thread(_san),
    )

    # Apply stale fallbacks post-gather (these are cheap local disk reads,
    # so doing them sequentially after the network gather is fine).
    if not reddit.get("available"):
        prev = _social_stale_fallback("reddit", {})
        if isinstance(prev, dict) and prev.get("subreddits"):
            reddit = {**prev, "stale": True}
    if not cc.get("available"):
        prev = _social_stale_fallback("cryptocompare", {})
        if isinstance(prev, dict) and prev.get("coins"):
            cc = {**prev, "stale": True}
    if not cc_news.get("available"):
        prev = _social_stale_fallback("cc_news", {})
        if isinstance(prev, dict) and prev.get("coins"):
            cc_news = {**prev, "stale": True}

    return {
        "available": any(s.get("available") for s in (reddit, cc, cc_news, san)),
        "reddit": reddit,
        "cryptocompare": cc,
        "cc_news": cc_news,
        "santiment": san,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


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

    The 4 sub-fetchers run concurrently via ``_fetch_social_async``. This
    public function stays sync so callers in ``fetch_all`` /
    ``_fetch_trading_async`` (where it's wrapped by ``_bg_call``) don't
    need changes. ``asyncio.run`` is safe here because ``_bg_call`` invokes
    us from a worker thread, which has no existing event loop."""
    t0 = time.monotonic()
    try:
        out = asyncio.run(_fetch_social_async())
        return out
    finally:
        print(f"  [timing] fetch_social: {time.monotonic() - t0:.2f}s")


def _coin_metrics_headers() -> dict:
    """Auth header for Coin Metrics. If COINMETRICS_API_KEY is set in env,
    return their documented `Authorization: Api-Key <key>` header. If unset,
    fall back to the keyless community-API tier — most of the basic metrics
    the dashboard needs (AdrActCnt, TxCnt, SplyCur, PriceUSD) are available
    keyless. A few advanced metrics (transfer volume USD) are paid-only."""
    import os
    key = os.environ.get("COINMETRICS_API_KEY", "").strip()
    if not key:
        return {}
    return {"Authorization": f"Api-Key {key}"}


def _coin_metrics_get(url: str, params: dict) -> dict | list | None:
    """Internal Coin Metrics fetch that merges auth headers with the default
    UA. Mirrors `_get` semantics — None on any failure, no raise."""
    try:
        headers = dict(H)
        headers.update(_coin_metrics_headers())
        r = requests.get(url, params=params, headers=headers, timeout=25)
        if r.status_code != 200:
            print(f"  [skip] {url} -> {r.status_code}", file=sys.stderr)
            return None
        return r.json()
    except Exception as e:
        print(f"  [skip] {url} -> {e}", file=sys.stderr)
        return None


def _coin_metrics_btc_eth_metrics_impl() -> dict:
    """Coin Metrics Community API — free network metrics for BTC + ETH.
    Tier 1 free only; metrics outside free tier return 403 and skip.

    Honors ``COINMETRICS_API_KEY`` env var (sent as ``Authorization:
    Api-Key <value>``). Falls back to keyless if unset."""
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
        j = _coin_metrics_get("https://community-api.coinmetrics.io/v4/timeseries/asset-metrics", params)
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


def coin_metrics_btc_eth_metrics() -> dict:
    """Stale-fallback wrapper around `_coin_metrics_btc_eth_metrics_impl`.

    The free Community API 403s without an API key and rate-limits even
    with one. When both btc and eth series come back empty we serve the
    last good payload from `data/.stale/coin_metrics_btc_eth_metrics.json`.
    """
    try:
        out = _coin_metrics_btc_eth_metrics_impl()
    except Exception as e:
        print(f"  [coin_metrics_btc_eth_metrics] fatal: {e}", file=sys.stderr)
        out = None
    # Success = at least one of btc/eth populated with any metric series.
    def _has_data(d):
        if not isinstance(d, dict):
            return False
        btc = d.get("btc") or {}
        eth = d.get("eth") or {}
        return bool(btc) or bool(eth)

    if _has_data(out):
        _stale_save("coin_metrics_btc_eth_metrics", out)
        return out
    cached = _stale_load("coin_metrics_btc_eth_metrics")
    if cached is not None:
        return cached
    return out if isinstance(out, dict) else {
        "btc": {}, "eth": {},
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def coin_metrics_eth_whale_metrics() -> dict:
    """Coin Metrics ETH-only daily series for the Whale tab — active
    addresses, tx count, supply. Keyless community tier is enough.

    Note: ``TxTfrValAdjUSD`` (USD transfer volume) was originally in the
    metrics list but it's a paid metric on the community-api tier — and
    Coin Metrics' API rejects the entire batch with HTTP 403 if even one
    requested metric is paid, which silently nuked all four series. Now
    we only request the three free ones. The UI's transfer-volume KPI
    sources from a different feed (Blockchair / Etherscan).

    Honors ``COINMETRICS_API_KEY`` env var (sent as
    ``Authorization: Api-Key <value>``) for users on a paid plan;
    keyless works for the free tier."""
    metrics = ["AdrActCnt", "TxCnt", "SplyCur"]
    since = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%dT00:00:00")
    params = {
        "assets": "eth",
        "metrics": ",".join(metrics),
        "start_time": since,
        "page_size": "1000",
        "frequency": "1d",
    }
    j = _coin_metrics_get("https://community-api.coinmetrics.io/v4/timeseries/asset-metrics", params)
    if not j or not isinstance(j, dict):
        return {}
    rows = j.get("data") or []
    out: dict[str, list[dict]] = {m: [] for m in metrics}
    for r in rows:
        d = (r.get("time") or "")[:10]
        if not d:
            continue
        for m in metrics:
            v = r.get(m)
            if v is None:
                continue
            try:
                out[m].append({"date": d, "value": float(v)})
            except (ValueError, TypeError):
                continue
    populated = {m: ser for m, ser in out.items() if ser}
    if not populated:
        return {}
    populated["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return populated


def blockchair_eth_stats() -> dict:
    """Blockchair ETH stats — 24h tx counts, largest transaction of the day,
    EIP-1559 burn, ERC-20/ERC-721 token activity, supply. No API key needed;
    free tier is 30 req/min.

    Returns a flat dict that maps cleanly into whale.eth.* — empty dict if the
    request fails so callers can render an empty-state.
    """
    j = _get("https://api.blockchair.com/ethereum/stats")
    if not j or not isinstance(j, dict):
        return {}
    d = j.get("data") or {}
    if not d:
        return {}
    largest = d.get("largest_transaction_24h") or {}

    # Blockchair returns wei amounts as strings (the values exceed JS safe-int
    # range, so they have to). Wrap conversions to tolerate string-or-None.
    def _wei_to_eth(v):
        if v in (None, "", 0, "0"):
            return None
        try:
            return float(v) / 1e18
        except (ValueError, TypeError):
            return None

    layer_2 = d.get("layer_2") or {}
    erc20  = (layer_2.get("erc_20")  if isinstance(layer_2, dict) else None) or {}
    erc721 = (layer_2.get("erc_721") if isinstance(layer_2, dict) else None) or {}

    txs_24h = d.get("transactions_24h")
    avg_tx_val_eth = d.get("average_transaction_value_24h")
    mkt_px = d.get("market_price_usd")

    # Honest on-chain 24h transfer volume in USD: txs * avg-value-per-tx * price.
    # Coin Metrics' TxTfrValAdjUSD is paid-only, so we derive an equivalent from
    # the three free Blockchair fields above. None if any input is missing.
    # Upgrade path: if ETHERSCAN_API_KEY is set, the stats?module=stats&action=
    # ethdailytx endpoint can back a historical series via daily tx count *
    # daily avg-value * daily price. Skipped for now — gating on a key adds
    # setup friction and this single live value already replaces the misleading
    # CoinGecko trading-volume KPI.
    try:
        if txs_24h in (None, "") or avg_tx_val_eth in (None, "") or mkt_px in (None, ""):
            transfer_volume_24h_usd = None
        else:
            transfer_volume_24h_usd = float(txs_24h) * float(avg_tx_val_eth) * float(mkt_px)
    except (TypeError, ValueError):
        transfer_volume_24h_usd = None

    return {
        "blocks_24h": d.get("blocks_24h"),
        "transactions_24h": txs_24h,
        "avg_tx_fee_eth_24h": d.get("average_transaction_fee_24h"),
        "avg_tx_value_eth_24h": avg_tx_val_eth,
        "transfer_volume_24h_usd": transfer_volume_24h_usd,
        "supply_eth": _wei_to_eth(d.get("circulation_approximate")),
        "burned_eth_total": _wei_to_eth(d.get("burned")),
        "burned_eth_24h": _wei_to_eth(d.get("burned_24h")),
        "inflation_eth_24h": _wei_to_eth(d.get("inflation_24h")) or 0.0,
        "erc20_transactions_24h": erc20.get("transactions_24h"),
        "erc721_transactions_24h": erc721.get("transactions_24h"),
        "market_price_usd": mkt_px,
        "largest_tx_24h": {
            "hash": largest.get("hash"),
            "value_usd": largest.get("value_usd"),
        } if largest else None,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# Native decimal lookup for Blockchair multichain stats. Blockchair returns
# circulation/supply in the chain's smallest unit (satoshis for the BTC-derived
# chains, wei for ETH). LTC/BCH/DOGE all inherit Bitcoin's 1e8 base unit.
_BLOCKCHAIR_NATIVE_DECIMALS = {
    "bitcoin":      8,
    "litecoin":     8,
    "bitcoin-cash": 8,
    "dogecoin":     8,
    "ethereum":    18,
}

_BLOCKCHAIR_SYMBOLS = {
    "bitcoin":      "BTC",
    "litecoin":     "LTC",
    "bitcoin-cash": "BCH",
    "dogecoin":     "DOGE",
    "ethereum":     "ETH",
}

_BLOCKCHAIR_NAMES = {
    "bitcoin":      "Bitcoin",
    "litecoin":     "Litecoin",
    "bitcoin-cash": "Bitcoin Cash",
    "dogecoin":     "Dogecoin",
    "ethereum":     "Ethereum",
}


def _blockchair_eth_large_transactions_impl(
    min_value_usd: float = 1_000_000.0, limit: int = 10
) -> list[dict]:
    """Live fetch of large ETH transactions over the last 24h via Blockchair.

    Blockchair's tx search uses a `q=` query language; we ask for txs in the
    last 24h with value_usd above the threshold. If the query rejects the
    `value_usd(...)` filter we fall back to a plain top-N-by-value scan.

    Each row carries hash, native ETH value, USD value, ISO time, and fee
    in ETH. Returns at most `limit` rows sorted by USD value descending.
    """
    min_usd = int(max(0, float(min_value_usd or 0)))
    base = "https://api.blockchair.com/ethereum/transactions"

    j = _get(base, {
        "q": f"time(24h)..,value_usd({min_usd}..)",
        "s": "value_usd(desc)",
        "limit": str(max(limit, 10)),
    })

    # Fall back to plain top-N-by-value if the query language was rejected
    # (Blockchair returns 4xx for malformed `q=`; _get logs and returns None).
    if not j or not isinstance(j, dict) or not j.get("data"):
        j = _get(base, {"limit": str(max(limit, 10)), "s": "value(desc)"})

    if not j or not isinstance(j, dict):
        return []
    rows = j.get("data") or []
    if not isinstance(rows, list):
        return []

    out: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        h = r.get("hash")
        if not h:
            continue
        try:
            value_eth = float(r.get("value") or 0) / 1e18
        except (TypeError, ValueError):
            value_eth = 0.0
        try:
            value_usd = float(r.get("value_usd") or 0)
        except (TypeError, ValueError):
            value_usd = 0.0
        try:
            fee_eth = float(r.get("fee") or 0) / 1e18
        except (TypeError, ValueError):
            fee_eth = 0.0
        out.append({
            "hash":      h,
            "value_eth": value_eth,
            "value_usd": value_usd,
            "time":      r.get("time"),
            "fee_eth":   fee_eth,
        })

    out.sort(key=lambda r: r.get("value_usd") or 0, reverse=True)
    return out[:limit]


def blockchair_eth_large_transactions(
    min_value_usd: float = 1_000_000.0, limit: int = 10
) -> list[dict]:
    """Stale-fallback wrapper around `_blockchair_eth_large_transactions_impl`.

    On empty result or fetch failure, serves the last good payload from
    `data/.stale/blockchair_eth_large_transactions.json`. Returns an empty
    list if no cache exists either — never raises.
    """
    cache_key = "blockchair_eth_large_transactions"
    try:
        out = _blockchair_eth_large_transactions_impl(min_value_usd, limit)
    except Exception as e:
        print(f"  [blockchair_eth_large_transactions] fatal: {e}", file=sys.stderr)
        out = None
    if isinstance(out, list) and len(out) > 0:
        _stale_save(cache_key, out)
        return out
    cached = _stale_load(cache_key)
    if cached is not None:
        return cached if isinstance(cached, list) else []
    return out if isinstance(out, list) else []


def blockchair_chain_stats(chain_slug: str) -> dict:
    """Blockchair `/stats` endpoint generalized over chain slug.

    Supports `bitcoin`, `litecoin`, `bitcoin-cash`, `dogecoin`, `ethereum`.
    Returns a flat dict with blocks_24h, transactions_24h, largest_tx_24h,
    supply (in native units via `_BLOCKCHAIR_NATIVE_DECIMALS`), and
    market_price_usd. Empty dict on failure so callers can render an
    empty-state.
    """
    slug = (chain_slug or "").strip().lower()
    if slug not in _BLOCKCHAIR_NATIVE_DECIMALS:
        return {}
    j = _get(f"https://api.blockchair.com/{slug}/stats")
    if not j or not isinstance(j, dict):
        return {}
    d = j.get("data") or {}
    if not d:
        return {}
    decimals = _BLOCKCHAIR_NATIVE_DECIMALS[slug]
    divisor = float(10 ** decimals)

    def _to_native(v):
        if v in (None, "", 0, "0"):
            return None
        try:
            return float(v) / divisor
        except (ValueError, TypeError):
            return None

    largest = d.get("largest_transaction_24h") or {}
    largest_out = None
    if isinstance(largest, dict) and largest.get("hash"):
        largest_out = {
            "hash":      largest.get("hash"),
            "value_usd": largest.get("value_usd"),
        }

    return {
        "symbol":            _BLOCKCHAIR_SYMBOLS.get(slug, slug.upper()),
        "name":              _BLOCKCHAIR_NAMES.get(slug, slug.title()),
        "blocks_24h":        d.get("blocks_24h"),
        "transactions_24h":  d.get("transactions_24h"),
        "largest_tx_24h":    largest_out,
        "supply":            _to_native(d.get("circulation_approximate")
                                        or d.get("circulation")),
        "market_price_usd":  d.get("market_price_usd"),
        "_native_decimals":  decimals,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _fetch_multichain_whale_stats_impl() -> dict:
    """Live fetch of Blockchair stats for LTC/BCH/DOGE. Paced at 0.3s/req to
    respect Blockchair's free-tier 30-req/min cap. Returns dict keyed by
    chain slug — missing chains map to empty dicts, never raises."""
    out: dict[str, dict] = {}
    for slug in ("litecoin", "bitcoin-cash", "dogecoin"):
        try:
            out[slug] = blockchair_chain_stats(slug) or {}
        except Exception as e:
            print(f"  [multichain_whale_stats] {slug}: {e}", file=sys.stderr)
            out[slug] = {}
        time.sleep(0.3)
    return out


def fetch_multichain_whale_stats() -> dict:
    """Stale-fallback wrapper around `_fetch_multichain_whale_stats_impl`.

    Treats the multichain dict as empty if *every* chain returned an empty
    payload; in that case the last good cache is served. Returns `{}` if no
    cache exists either.
    """
    cache_key = "multichain_whale_stats"
    try:
        out = _fetch_multichain_whale_stats_impl()
    except Exception as e:
        print(f"  [fetch_multichain_whale_stats] fatal: {e}", file=sys.stderr)
        out = None
    if isinstance(out, dict) and any(
        isinstance(v, dict) and v for v in out.values()
    ):
        _stale_save(cache_key, out)
        return out
    cached = _stale_load(cache_key)
    if cached is not None:
        return cached if isinstance(cached, dict) else {}
    return out if isinstance(out, dict) else {}


def _etherscan_gas_impl() -> dict:
    """Live etherscan gas oracle fetch. See `etherscan_gas` wrapper."""
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
    except (TypeError, ValueError) as e:
        print(f"  [gas] etherscan gwei parse suppressed: {type(e).__name__}", file=sys.stderr)
    return out


def etherscan_gas() -> dict:
    """Etherscan v2 gas oracle — ETH mainnet base fee + safe/propose/fast.

    Works without an API key but rate-limited to 1 req/5sec — frequent
    callers get HTTP 429. On rate-limit (or any non-200) the live fetch
    returns a payload with only `fetched_at` and no gwei fields; this
    wrapper detects that empty case and serves the last good response
    from `data/.stale/etherscan_gas.json` tagged with stale metadata.
    """
    try:
        out = _etherscan_gas_impl()
    except Exception as e:
        print(f"  [etherscan_gas] fatal: {e}", file=sys.stderr)
        out = None
    if not _is_empty_result(out):
        _stale_save("etherscan_gas", out)
        return out
    cached = _stale_load("etherscan_gas")
    return cached if cached is not None else (out or {})


# ----- Etherscan daily ETH on-chain series ----------------------------------

def _etherscan_eth_daily_impl(days: int = 90) -> dict:
    """Live Etherscan daily-series fetch. See ``etherscan_eth_daily`` wrapper.

    Free-tier compromise: Etherscan's purpose-built daily-stats endpoints
    (``stats?action=dailytx`` / ``dailyavggasprice`` / ``dailynewaddress``
    / ``ethdailytxnfee`` / ``dailynetutilization``) are gated behind their
    Pro plan. To stay on the free tier and still produce a 90-day daily
    on-chain throughput series, we synthesize one from a free endpoint:

      1. For each of the last N+1 UTC midnights, call
         ``module=block&action=getblocknobytime&closest=before`` to find
         the block number mined at (or just before) that timestamp.
      2. The number of blocks mined in a 24h window is the delta between
         consecutive checkpoints. That delta is a clean proxy for daily
         network throughput / capacity utilization (post-Merge ETH
         targets a 12s slot so a steady ~7,200 blocks/day = full
         saturation; dips correlate with missed slots and demand drops).

    That's ~91 calls per daily refresh — well within the 5-req/sec and
    100k/day free-tier ceilings. The endpoint takes one ``apikey`` param
    when provided.
    """
    import os

    api_key = os.environ.get("ETHERSCAN_API_KEY", "").strip()
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not api_key:
        return {"available": False, "reason": "no ETHERSCAN_API_KEY in env"}

    # Build the list of midnight-UTC timestamps for the last ``days`` days,
    # plus one extra at "now" so the most recent bucket has a delta.
    # E.g. for days=90 → 91 timestamps → 90 daily deltas.
    now = datetime.now(timezone.utc)
    today_midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    checkpoints: list[tuple[str, int]] = []  # (YYYY-MM-DD label, unix timestamp)
    for i in range(days, -1, -1):
        d = today_midnight - timedelta(days=i)
        checkpoints.append((d.strftime("%Y-%m-%d"), int(d.timestamp())))

    block_numbers: dict[str, int | None] = {}
    fail_count = 0
    for label, ts in checkpoints:
        j = _get(
            "https://api.etherscan.io/v2/api",
            {
                "chainid": "1",
                "module": "block",
                "action": "getblocknobytime",
                "timestamp": str(ts),
                "closest": "before",
                "apikey": api_key,
            },
        )
        if not isinstance(j, dict) or j.get("status") != "1":
            block_numbers[label] = None
            fail_count += 1
            # Etherscan returns Max rate limit reached as status=0; bail
            # early if everything is failing rather than burn 90 calls.
            if fail_count >= 5 and not any(v for v in block_numbers.values()):
                print("  [etherscan_eth_daily] aborting: 5 consecutive failures",
                      file=sys.stderr)
                break
            continue
        try:
            block_numbers[label] = int(j.get("result"))
        except (TypeError, ValueError):
            block_numbers[label] = None
            fail_count += 1

    # Convert to a (date, blocks_in_24h) series. Blocks-per-day is the
    # delta from one checkpoint to the next; we attribute that delta to
    # the *starting* date (i.e. blocks mined from day D 00:00 UTC to
    # day D+1 00:00 UTC are tagged with date D).
    series: list[dict] = []
    labels = [c[0] for c in checkpoints]
    for i in range(len(labels) - 1):
        d0, d1 = labels[i], labels[i + 1]
        b0, b1 = block_numbers.get(d0), block_numbers.get(d1)
        if b0 is None or b1 is None:
            continue
        delta = b1 - b0
        # Sanity guard: a healthy 24h window is ~6.5k–7.5k blocks. Drop
        # absurd values (negative, zero, > 20k) which would only occur if
        # the API returned wildly wrong block numbers.
        if delta <= 0 or delta > 20_000:
            continue
        series.append({"date": d0, "value": delta})

    available = bool(series)
    out: dict = {
        "available": available,
        "metric": "blocks_per_day",
        "description": (
            "Ethereum mainnet blocks mined per UTC day. Synthesized from "
            "Etherscan's free block?action=getblocknobytime endpoint by "
            "diffing midnight-UTC checkpoint block numbers. Higher = more "
            "network throughput; ~7,200/day saturates the 12s slot target."
        ),
        "series": series,
        "fetched_at": fetched_at,
    }
    if fail_count:
        out["fail_count"] = fail_count
    return out


def etherscan_eth_daily(days: int = 90) -> dict:
    """Etherscan ETH on-chain 90-day daily series — env-gated by
    ``ETHERSCAN_API_KEY``.

    Mirrors the Glassnode / FRED no-key contract: when the env var is
    absent, returns ``{"available": False, "reason": "no ETHERSCAN_API_KEY in env"}``
    and never touches the network. The dashboard renders an inline
    "Add ETHERSCAN_API_KEY to light up" hint in place of the chart.

    Stale-fallback: when the key *is* set but every call failed (rate
    limit, invalid key, network error), serves the last successful
    payload from ``data/.stale/etherscan_eth_daily.json`` tagged with
    stale metadata. The no-key branch never triggers stale.
    """
    import os
    key_set = bool(os.environ.get("ETHERSCAN_API_KEY", "").strip())
    try:
        out = _etherscan_eth_daily_impl(days=days)
    except Exception as e:
        print(f"  [etherscan_eth_daily] fatal: {e}", file=sys.stderr)
        out = None

    # No-key branch is intentional; never serve stale for that.
    if (
        isinstance(out, dict)
        and out.get("available") is False
        and out.get("reason") == "no ETHERSCAN_API_KEY in env"
    ):
        return out

    # Key was set and the call succeeded with at least one data point.
    if isinstance(out, dict) and out.get("available"):
        _stale_save("etherscan_eth_daily", out)
        return out

    # Key was set but everything failed → try stale.
    if key_set:
        cached = _stale_load("etherscan_eth_daily")
        if cached is not None:
            return cached
    return out if isinstance(out, dict) else {
        "available": False,
        "reason": "fetch failed",
        "series": [],
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _fetch_fred_impl() -> dict:
    """Live FRED fetch. See `fetch_fred` for the public wrapper that adds
    stale-fallback when the key is set but the API errors."""
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

    Stale-fallback: when ``FRED_API_KEY`` is set but every series comes back
    empty (key revoked, FRED outage, network), serve the last good response
    from ``data/.stale/fetch_fred.json`` tagged
    ``{"stale": True, "stale_age_sec": N}``. The no-key branch (returns
    ``available: False``) is an intentional opt-out and never triggers stale.
    """
    import os
    key_set = bool(os.environ.get("FRED_API_KEY", "").strip())
    try:
        out = _fetch_fred_impl()
    except Exception as e:
        print(f"  [fetch_fred] fatal: {e}", file=sys.stderr)
        out = None

    # No-key branch is intentional; treat as valid response, no stale.
    if isinstance(out, dict) and out.get("available") is False:
        return out

    # Key set: assess whether any series populated. We treat 'no series at
    # all' as failure worthy of stale-fallback.
    def _all_series_empty(d):
        if not isinstance(d, dict):
            return True
        series_keys = ("dxy", "sp500", "gold", "treasury_10y", "m2")
        return all(not (d.get(k) or [])  for k in series_keys)

    if key_set and (out is None or _all_series_empty(out)):
        cached = _stale_load("fetch_fred")
        if cached is not None:
            return cached
        return out if isinstance(out, dict) else {
            "available": False,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    if isinstance(out, dict):
        _stale_save("fetch_fred", out)
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


# ----- Yahoo most-active stocks + signal scoring ----------------------------

def yahoo_most_active(limit: int = 50) -> list[dict]:
    """Yahoo Finance most-active US stocks predefined screener.

    Returns a list of dicts with symbol, name, last_price, change_pct, volume.
    No auth required. Some Yahoo endpoints require a cookie/crumb; this one
    is publicly accessible. Returns [] on any failure.
    """
    j = _get(
        "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved",
        {"count": str(limit), "scrIds": "most_actives",
         "lang": "en-US", "region": "US"},
    )
    if not j or not isinstance(j, dict):
        return []
    try:
        quotes = ((j.get("finance") or {}).get("result") or [{}])[0].get("quotes") or []
    except (IndexError, AttributeError, TypeError):
        return []
    out: list[dict] = []
    for q in quotes[:limit]:
        sym = q.get("symbol")
        if not sym:
            continue
        name = q.get("shortName") or q.get("longName") or sym
        try:
            last_price = float(q.get("regularMarketPrice") or 0)
            change_pct = float(q.get("regularMarketChangePercent") or 0)
            volume = int(q.get("regularMarketVolume") or 0)
        except (ValueError, TypeError):
            continue
        out.append({
            "symbol":     sym,
            "name":       name,
            "last_price": last_price,
            "change_pct": change_pct,
            "volume":     volume,
        })
    return out


def yahoo_chart_history(symbol: str, range_: str = "6mo") -> list[dict]:
    """Daily OHLCV history for a single ticker via Yahoo's chart API.

    Returns a list of {date, close, volume} for each valid daily bar.
    Yahoo includes None values for missing days; those are filtered out.
    Empty list on failure.
    """
    j = _get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        {"range": range_, "interval": "1d"},
    )
    if not j or not isinstance(j, dict):
        return []
    try:
        result = (j.get("chart") or {}).get("result", [])[0]
        timestamps = result.get("timestamp") or []
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []
    except (IndexError, AttributeError, TypeError):
        return []
    out: list[dict] = []
    for i, ts in enumerate(timestamps):
        if ts is None:
            continue
        c = closes[i] if i < len(closes) else None
        v = volumes[i] if i < len(volumes) else None
        if c is None:
            continue
        out.append({
            "date":   datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d"),
            "close":  float(c),
            "volume": int(v) if v is not None else 0,
        })
    return out


def _score_components_from_series(closes: list[float], volumes: list[float]) -> tuple[list[dict], int]:
    """Compute the 6 signal components for the last day given full series.

    Returns (components_list, raw_total_score). The caller normalizes/labels.
    Each component is {"name", "value", "score"}.
    """
    n = len(closes)
    components: list[dict] = []

    last_close = closes[-1] if n else 0.0

    # 1) Above 50d SMA
    if n >= 50:
        sma_50 = sum(closes[-50:]) / 50.0
        above_50 = 1 if last_close > sma_50 else 0
        score_50 = 10 if above_50 else -10
        components.append({"name": "Above 50d SMA", "value": above_50, "score": score_50})
    else:
        sma_50 = sum(closes) / n if n else 0.0
        above_50 = 1 if last_close > sma_50 else 0
        score_50 = 5 if above_50 else -5
        components.append({"name": "Above 50d SMA", "value": above_50, "score": score_50})

    # 2) RSI(14)
    if n >= 15:
        gains = 0.0
        losses = 0.0
        for i in range(n - 14, n):
            change = closes[i] - closes[i - 1]
            if change > 0:
                gains += change
            else:
                losses += -change
        avg_gain = gains / 14.0
        avg_loss = losses / 14.0
        if avg_loss == 0:
            rsi = 100.0 if avg_gain > 0 else 50.0
        else:
            rs = avg_gain / avg_loss
            rsi = 100.0 - (100.0 / (1.0 + rs))
        if rsi > 70:
            score_rsi = -10
        elif rsi < 30:
            score_rsi = 10
        else:
            # middle linear: 50 -> 0, 30 -> +10, 70 -> -10
            score_rsi = int(round((50.0 - rsi) / 20.0 * 10.0))
        components.append({"name": "RSI(14)", "value": round(rsi, 2), "score": score_rsi})
    else:
        components.append({"name": "RSI(14)", "value": None, "score": 0})

    # 3) MACD(12,26) signal line crossover
    def _ema(vals: list[float], period: int) -> list[float]:
        if not vals:
            return []
        k = 2.0 / (period + 1.0)
        ema_vals = [vals[0]]
        for v in vals[1:]:
            ema_vals.append(v * k + ema_vals[-1] * (1.0 - k))
        return ema_vals

    if n >= 35:
        ema_12 = _ema(closes, 12)
        ema_26 = _ema(closes, 26)
        macd_line = [a - b for a, b in zip(ema_12, ema_26)]
        # signal line is EMA(9) of macd_line
        signal_line = _ema(macd_line[-(len(macd_line)):], 9) if macd_line else []
        if signal_line:
            macd_val = macd_line[-1]
            sig_val = signal_line[-1]
            diff = macd_val - sig_val
            score_macd = 10 if diff > 0 else -10
            components.append({"name": "MACD signal", "value": round(diff, 3), "score": score_macd})
        else:
            components.append({"name": "MACD signal", "value": None, "score": 0})
    else:
        components.append({"name": "MACD signal", "value": None, "score": 0})

    # 4) 5d momentum
    if n >= 6:
        prev_5 = closes[-6]
        if prev_5 != 0:
            mom_pct = (last_close / prev_5 - 1.0) * 100.0
        else:
            mom_pct = 0.0
        score_mom = int(round(max(-10.0, min(10.0, mom_pct))))
        components.append({"name": "5d momentum", "value": round(mom_pct, 2), "score": score_mom})
    else:
        components.append({"name": "5d momentum", "value": None, "score": 0})

    # 5) Volume z-score (last day vs 30d mean/std)
    if len(volumes) >= 31:
        recent = volumes[-31:-1]  # 30-day baseline excluding today
        mean_v = sum(recent) / 30.0
        var_v = sum((v - mean_v) ** 2 for v in recent) / 30.0
        std_v = var_v ** 0.5
        if std_v > 0:
            z = (volumes[-1] - mean_v) / std_v
        else:
            z = 0.0
        score_vol = 5 if z > 1.5 else 0
        components.append({"name": "Volume z-score", "value": round(z, 2), "score": score_vol})
    else:
        components.append({"name": "Volume z-score", "value": None, "score": 0})

    # 6) 50/200 cross
    if n >= 200:
        sma_50_x = sum(closes[-50:]) / 50.0
        sma_200_x = sum(closes[-200:]) / 200.0
        if sma_50_x > sma_200_x:
            components.append({"name": "50/200 cross", "value": "above", "score": 10})
        else:
            components.append({"name": "50/200 cross", "value": "below", "score": -10})
    else:
        # Partial: use what's available
        if n >= 50:
            sma_50_x = sum(closes[-50:]) / 50.0
            sma_long = sum(closes) / n
            label = "above" if sma_50_x > sma_long else "below"
            score = 5 if sma_50_x > sma_long else -5
            components.append({"name": "50/200 cross", "value": label, "score": score})
        else:
            components.append({"name": "50/200 cross", "value": "n/a", "score": 0})

    raw_total = sum(c["score"] for c in components)
    return components, raw_total


def _signal_history_from_prices(closes: list[float], volumes: list[float],
                                 dates: list[str], days: int = 90) -> list[dict]:
    """Rolling -100..+100 signal score for the last `days` aligned closes.

    Uses the same component algorithm as `compute_stock_signal` so the
    crypto breadth chart in the UI can share the stocks rendering path.
    For each as-of point `t` in the trailing window we recompute components
    against `closes[:t+1]` / `volumes[:t+1]` and emit `{date, score}`.
    The component function gracefully returns zero-scored components when
    history is short, so early entries are naturally muted rather than
    raising.

    Inputs are pre-aligned: `closes[i]`, `volumes[i]`, `dates[i]` describe
    the same trading day. Returns entries oldest -> newest.
    """
    n = min(len(closes), len(volumes), len(dates))
    if n == 0:
        return []
    window = min(days, n)
    start = n - window
    out: list[dict] = []
    for i in range(start, n):
        sub_c = closes[: i + 1]
        sub_v = volumes[: i + 1]
        _comps, raw_total = _score_components_from_series(sub_c, sub_v)
        score = int(round(max(-100.0, min(100.0, (raw_total or 0) * 1.8))))
        out.append({"date": dates[i], "score": score})
    return out


def _label_from_score(score: int) -> str:
    if score >= 50:
        return "STRONG BUY"
    if score >= 20:
        return "BUY"
    if score > -20:
        return "HOLD"
    if score > -50:
        return "SELL"
    return "STRONG SELL"


def compute_stock_signal(history: list[dict]) -> dict:
    """Compute a signal score, label, components, and a 90d rolling history
    from daily OHLC history. `history` is the list returned by
    `yahoo_chart_history` — each item has {date, close, volume}.

    Returns:
        {
            "score": int,            # -100..+100
            "label": str,
            "components": [...],
            "history": [{date, score}, ...]   # last 90d, oldest first
        }
    """
    if not history:
        return {"score": 0, "label": "HOLD", "components": [], "history": []}

    closes = [float(h["close"]) for h in history]
    volumes = [float(h.get("volume") or 0) for h in history]
    dates = [h["date"] for h in history]

    components, raw_total = _score_components_from_series(closes, volumes)
    # Raw range roughly ±55; normalize to ±100 by ~1.8x then clip.
    final_score = int(round(max(-100.0, min(100.0, raw_total * 1.8))))
    label = _label_from_score(final_score)

    # Rolling 90d history: compute signal at each day for the last 90
    rolling: list[dict] = []
    n = len(closes)
    window = min(90, n)
    start = n - window
    for i in range(start, n):
        sub_closes = closes[: i + 1]
        sub_vols = volumes[: i + 1]
        _comps, sub_raw = _score_components_from_series(sub_closes, sub_vols)
        sub_score = int(round(max(-100.0, min(100.0, sub_raw * 1.8))))
        rolling.append({"date": dates[i], "score": sub_score})

    return {
        "score":      final_score,
        "label":      label,
        "components": components,
        "history":    rolling,
    }


def compute_stock_poc(history: list[dict]) -> dict | None:
    """Volume-profile POC for a single stock, computed from the same daily
    OHLCV history `compute_stock_signal` consumes.

    Returns the same shape `compute_poc_top_markets` emits for each crypto
    entry — d30/d90/d180 timeframes plus migration / migration_series /
    naked POCs — so the frontend can render `pocCompactCardHtml(stock)`
    without a schema fork. Returns None when the history is too short for
    a meaningful 30d window (the inner `point_of_control` already guards
    `>= 10` overlapping days; we additionally require ≥30 daily bars so
    the d30 timeframe carries weight).

    Daily bars from Yahoo are trading-days only (~21/month, ~125 in 6mo).
    The same lookback labels (`d30`, `d90`, `d180`) therefore cover ~6
    calendar weeks / ~4½ calendar months / the full 6mo window respectively
    — close enough to crypto semantics for the UI's purposes and the
    label names stay consistent.
    """
    if not history or len(history) < 30:
        return None
    price_series = [{"date": h["date"], "value": float(h["close"])}
                    for h in history if h.get("date") and h.get("close") is not None]
    volume_series = [{"date": h["date"], "value": float(h.get("volume") or 0)}
                     for h in history if h.get("date") and h.get("close") is not None]
    LOOKBACKS = (("d30", 30, 60), ("d90", 90, 80), ("d180", 180, 100))
    tfs = {k: point_of_control(price_series, volume_series,
                                lookback_days=lb, bins=b)
           for k, lb, b in LOOKBACKS}
    if not any(tfs.values()):
        return None
    return {
        **tfs,
        "migration":        compute_poc_migration(tfs.get("d30"), tfs.get("d90")),
        "migration_series": poc_migration_series(price_series, volume_series),
        "naked":            naked_pocs(price_series, volume_series, lookback_days=180),
    }


async def _fetch_stocks_signals_async(limit: int = 50) -> list[dict]:
    """Concurrent implementation of ``fetch_stocks_signals``. Schema-
    identical to the sequential version. Yahoo's chart endpoint tolerates
    ~200/hr per IP with generous burst behavior; an 8-permit semaphore
    holds total in-flight chart calls to 8 (replacing the previous 0.3s
    serial pacing, which capped throughput at ~3 req/s). Order of the
    returned list matches ``yahoo_most_active``'s order so downstream
    consumers see the same shape as before."""
    movers = yahoo_most_active(limit)
    if not movers:
        return []

    # Local semaphore — independent of the trading-fetch generic semaphore
    # so a parallel ``fetch_all`` doesn't have these compete with other
    # generic fetchers for the same 10 permits.
    sem = asyncio.Semaphore(8)

    async def _one(m: dict) -> dict | None:
        async with sem:
            def _work() -> dict | None:
                sym = m["symbol"]
                hist = yahoo_chart_history(sym, "6mo")
                sig = compute_stock_signal(hist)
                # POC reuses the same daily OHLCV — no extra fetch. Returns
                # None for tickers with <30d of bars (recent IPOs, etc.); the
                # frontend falls back to the empty-state card in that case.
                poc = compute_stock_poc(hist)
                return {
                    "symbol":     sym,
                    "name":       m["name"],
                    "last_price": m["last_price"],
                    "change_pct": m["change_pct"],
                    "volume":     m["volume"],
                    "score":      sig["score"],
                    "label":      sig["label"],
                    "components": sig["components"],
                    "history":    sig["history"],
                    "poc":        poc,
                }
            try:
                return await asyncio.to_thread(_work)
            except Exception as e:
                print(f"  [stocks] {m.get('symbol')}: {e}", file=sys.stderr)
                return None

    results = await asyncio.gather(*[_one(m) for m in movers])
    return [r for r in results if r is not None]


def fetch_stocks_signals(limit: int = 50) -> list[dict]:
    """Pull the top-N most-active US stocks and compute a signal for each.

    The 50-symbol Yahoo chart fan-out runs concurrently via
    ``_fetch_stocks_signals_async`` with an 8-permit semaphore for natural
    rate-throttling (replacing the previous 0.3s per-call serial sleep).
    Yahoo's chart endpoint allows ~200/hr per IP, so 8-way concurrency is
    well under the burst limit. Returns [] if the screener call fails.

    Public API is sync so the existing ``_bg_call(fetch_stocks_signals,
    50)`` site in ``_fetch_trading_async`` doesn't need changes;
    ``asyncio.run`` is safe because ``_bg_call`` invokes us from a worker
    thread without an event loop attached."""
    t0 = time.monotonic()
    try:
        out = asyncio.run(_fetch_stocks_signals_async(limit))
        print(f"  [timing] fetch_stocks_signals: {time.monotonic() - t0:.2f}s · "
              f"{len(out)} stocks succeeded")
        return out
    except Exception:
        print(f"  [timing] fetch_stocks_signals: {time.monotonic() - t0:.2f}s · failed")
        raise


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
    # ``limit`` matches the longest dashboard range button (3y = 1095d) — any
    # data older than that is unreachable from the UI's range selector and
    # just bloats the inlined payload. The alternative.me ``?limit=`` query
    # is silently ignored (the API returns the full history back to 2018
    # regardless), so we also slice client-side after parsing.
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
    if limit and limit > 0 and len(out) > limit:
        out = out[-limit:]
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

# Concurrency configuration. CoinGecko's free tier is ~30 req/min, so we
# serialize CG calls through a 1-permit semaphore AND enforce a 0.6s gap
# between successive calls (100/min headroom mathematically; 0.6s in
# practice protects against the per-IP burst limiter). Other APIs
# (CryptoCompare, DeFiLlama, mempool.space, etc.) tolerate much higher
# concurrency — we cap them at 10 simultaneously to avoid local socket
# exhaustion and being mistaken for a scraper.
CG_PACE = 0.6
CG_CONCURRENCY = 1
GENERIC_CONCURRENCY = 10


async def _cg_call(fn: Callable, *args: Any, **kwargs: Any) -> Any:
    """Run a synchronous CoinGecko fetcher in a thread, serialized via the
    CG semaphore and followed by the CG_PACE gap. The semaphore is held
    across the gap so two CG calls can never overlap, no matter how the
    event loop schedules other tasks."""
    async with _cg_semaphore:
        result = await asyncio.to_thread(fn, *args, **kwargs)
        await asyncio.sleep(CG_PACE)
        return result


async def _bg_call(fn: Callable, *args: Any, **kwargs: Any) -> Any:
    """Run an arbitrary synchronous fetcher in a thread under the generic
    concurrency cap. Used for all non-CG sources (CryptoCompare, OKX,
    DeFiLlama, mempool.space, Coinbase, Yahoo, FRED, etc.)."""
    async with _generic_semaphore:
        return await asyncio.to_thread(fn, *args, **kwargs)


async def _timed(label: str, coro: Awaitable[Any]) -> Any:
    """Await ``coro`` and log wall-clock duration. Survives exceptions —
    timing line is emitted even on failure so a hung fetcher is visible."""
    t0 = time.monotonic()
    try:
        return await coro
    finally:
        print(f"  [timing] {label}: {time.monotonic() - t0:.2f}s")


# Lazily-created event-loop-bound semaphores. We construct them fresh per
# asyncio.run() invocation to avoid the "attached to different loop" error
# that bites long-lived module-scope asyncio primitives. See _fetch_trading_async.
_cg_semaphore: asyncio.Semaphore  # set in _fetch_trading_async / _fetch_whale_async
_generic_semaphore: asyncio.Semaphore


async def _fetch_trading_async() -> dict:
    """Concurrent implementation of ``fetch_trading``. Schema-identical to
    the previous sequential version; just runs the independent fetchers in
    parallel and serializes CoinGecko calls behind a rate-limited
    semaphore."""
    global _cg_semaphore, _generic_semaphore
    _cg_semaphore = asyncio.Semaphore(CG_CONCURRENCY)
    _generic_semaphore = asyncio.Semaphore(GENERIC_CONCURRENCY)

    print("Fetching trading data...")
    t_total = time.monotonic()

    # ---- Batch 1: all independent fetchers run concurrently -----------------
    # CoinGecko calls share a 1-permit semaphore so they execute in series
    # internally even though they're scheduled in parallel here.
    print("  Batch 1: scheduling all independent fetchers in parallel...")
    (
        btc_mkt, eth_mkt, link_mkt, ltc_mkt,           # CG market_chart × 4
        glob,                                          # CG /global
        top_markets_raw,                               # CG /coins/markets
        trending,                                      # CG /search/trending
        cb_spot, cb_intl,                              # Coinbase × 2
        okx_fund_btc, okx_fund_eth, okx_fund_link, okx_fund_ltc,  # OKX funding × 4
        okx_oi_btc, okx_oi_eth, okx_oi_link, okx_oi_ltc,          # OKX OI × 4
        okx_ls_btc, okx_ls_eth, okx_ls_link, okx_ls_ltc,          # OKX L/S × 4
        dvol_btc, dvol_eth,                            # Deribit × 2
        llama, gt_pools, social,                       # DeFiLlama, GeckoTerm, social
        gas, fred, mp,                                 # Etherscan, FRED, mempool
        diff_adj, lightning, pools,                    # mempool extras × 3
        chains, protocols, yields_top, bridges,        # DeFiLlama tabular × 4
        tvl_eth, tvl_sol, tvl_arb, tvl_base,           # DeFiLlama historical × 4
        news, ai_news, ai_funding, ai_curated,         # news × 4
        cadli, yahoo_idx, stocks_signals, fng,         # cadli, yahoo × 2, F&G
    ) = await asyncio.gather(
        _timed("coingecko_market(btc)",   _cg_call(coingecko_market, "bitcoin")),
        _timed("coingecko_market(eth)",   _cg_call(coingecko_market, "ethereum")),
        _timed("coingecko_market(link)",  _cg_call(coingecko_market, "chainlink")),
        _timed("coingecko_market(ltc)",   _cg_call(coingecko_market, "litecoin")),
        _timed("coingecko_global",        _cg_call(coingecko_global)),
        _timed("coingecko_top_markets",   _cg_call(coingecko_top_markets, 50)),
        _timed("coingecko_trending",      _cg_call(coingecko_trending)),
        _timed("coinbase_spot",           _bg_call(coinbase_spot)),
        _timed("coinbase_intl_perpetuals", _bg_call(coinbase_intl_perpetuals)),
        _timed("okx_funding(btc)",        _bg_call(okx_funding, "BTC-USDT-SWAP")),
        _timed("okx_funding(eth)",        _bg_call(okx_funding, "ETH-USDT-SWAP")),
        _timed("okx_funding(link)",       _bg_call(okx_funding, "LINK-USDT-SWAP")),
        _timed("okx_funding(ltc)",        _bg_call(okx_funding, "LTC-USDT-SWAP")),
        _timed("okx_open_interest(btc)",  _bg_call(okx_open_interest, "BTC")),
        _timed("okx_open_interest(eth)",  _bg_call(okx_open_interest, "ETH")),
        _timed("okx_open_interest(link)", _bg_call(okx_open_interest, "LINK")),
        _timed("okx_open_interest(ltc)",  _bg_call(okx_open_interest, "LTC")),
        _timed("okx_long_short(btc)",     _bg_call(okx_long_short, "BTC")),
        _timed("okx_long_short(eth)",     _bg_call(okx_long_short, "ETH")),
        _timed("okx_long_short(link)",    _bg_call(okx_long_short, "LINK")),
        _timed("okx_long_short(ltc)",     _bg_call(okx_long_short, "LTC")),
        _timed("deribit_dvol(btc)",       _bg_call(deribit_dvol, "BTC")),
        _timed("deribit_dvol(eth)",       _bg_call(deribit_dvol, "ETH")),
        _timed("defillama",               _bg_call(defillama)),
        _timed("geckoterminal_pools",     _bg_call(geckoterminal_pools)),
        _timed("fetch_social",            _bg_call(fetch_social)),
        _timed("etherscan_gas",           _bg_call(etherscan_gas)),
        _timed("fetch_fred",              _bg_call(fetch_fred)),
        _timed("mempool_space",           _bg_call(mempool_space)),
        _timed("mempool_diff_adj",        _bg_call(mempool_difficulty_adjustment)),
        _timed("mempool_lightning",       _bg_call(mempool_lightning_stats)),
        _timed("mempool_mining_pools",    _bg_call(mempool_mining_pools)),
        _timed("defillama_chains",        _bg_call(defillama_chains, 20)),
        _timed("defillama_protocols",     _bg_call(defillama_protocols, 25)),
        _timed("defillama_yields",        _bg_call(defillama_yields_stablecoin_top, 20)),
        _timed("defillama_bridges",       _bg_call(defillama_bridges)),
        _timed("defillama_tvl(eth)",      _bg_call(defillama_historical_tvl, "Ethereum")),
        _timed("defillama_tvl(sol)",      _bg_call(defillama_historical_tvl, "Solana")),
        _timed("defillama_tvl(arb)",      _bg_call(defillama_historical_tvl, "Arbitrum")),
        _timed("defillama_tvl(base)",     _bg_call(defillama_historical_tvl, "Base")),
        _timed("crypto_news_rss",         _bg_call(crypto_news_rss, 120)),
        _timed("fetch_ai_news",           _bg_call(fetch_ai_news)),
        _timed("fetch_ai_funding",        _bg_call(fetch_ai_funding)),
        _timed("load_ai_curated",         _bg_call(load_ai_curated)),
        _timed("coindesk_cadli_ohlc",     _bg_call(coindesk_cadli_ohlc, 90)),
        _timed("yahoo_indices",           _bg_call(yahoo_indices)),
        _timed("fetch_stocks_signals",    _bg_call(fetch_stocks_signals, 50)),
        _timed("fear_greed",              _bg_call(fear_greed)),
    )
    if fred.get("available"):
        print("  FRED macro (DXY/SPX/Gold/10Y/M2) available")
    print(f"    -> {len(ai_curated.get('top_funded_companies', []))} companies, "
          f"{len(ai_curated.get('investment_kpis', []))} inv KPIs, "
          f"{len(ai_curated.get('whitepaper_kpis', []))} wp KPIs")

    # ---- Stale-keep for top_markets (was inline in the sequential version) --
    top_markets = top_markets_raw
    if not top_markets and (CACHE / "market.json").exists():
        # CoinGecko 429 (rate-limit wipe) returns []. The semaphore + 0.6s
        # gap helps, but a fresh-cache 429 from upstream contention is still
        # possible — preserve last good list instead of clobbering cache.
        try:
            prev = json.loads((CACHE / "market.json").read_text()).get("markets_top") or []
            if prev:
                top_markets = prev
                print(f"  [stale-keep] markets_top empty from API; kept {len(prev)} from previous fetch")
        except Exception as e:
            print(f"  [stale-keep] failed to read previous markets_top: {e}", file=sys.stderr)

    # ---- Batch 2: depends on top_markets ------------------------------------
    # compute_poc_top_markets fans out 50 cryptocompare_market calls. We
    # already keep those serialized inside the function (it loops), but
    # CryptoCompare tolerates parallelism — wrap the whole call in a single
    # thread so it can run alongside any straggling Batch 1 work. (Most of
    # Batch 1 will be done by now since CG-bound tasks dominate.)
    # fetch_cc_per_coin_news fans out 25 CC news calls — also depends on
    # top_markets (to pick which symbols to score). Same parallelism story
    # as compute_poc_top_markets; the two run side-by-side here.
    print("  Batch 2: compute_poc_top_markets + cc_per_coin_news (depends on top_markets)...")
    poc_top, cc_per_coin_news = await asyncio.gather(
        _timed(
            "compute_poc_top_markets",
            _bg_call(compute_poc_top_markets, top_markets, 50),
        ),
        _timed(
            "fetch_cc_per_coin_news",
            _bg_call(fetch_cc_per_coin_news, top_markets, 25),
        ),
    )

    # ETH/BTC ratio from prices
    btc_p = {p["date"]: p["value"] for p in btc_mkt["price"]}
    ethbtc = []
    for p in eth_mkt["price"]:
        b = btc_p.get(p["date"])
        if b and b > 0:
            ethbtc.append({"date": p["date"], "value": p["value"] / b})

    print(f"  [timing] fetch_trading total: {time.monotonic() - t_total:.2f}s")

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
        "poc_top": poc_top,
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
        # Per-coin CC news sentiment (top-25 by mcap). Keyed by uppercase
        # symbol. Frontend `groupNewsBySymbol` merges these counts on top of
        # RSS counts so coins that aren't named in the 5 RSS feeds still get
        # scored. See `fetch_cc_per_coin_news` for the shape.
        "news_sentiment_by_coin": cc_per_coin_news,
        "ai_news": ai_news,
        "ai_funding": ai_funding,
        "ai_curated": ai_curated,
        "cadli_btc": cadli,
        "yahoo_indices": yahoo_idx,
        "stocks_signals": stocks_signals,
        "fear_greed": fng,
        "ethbtc": ethbtc,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def fetch_trading() -> dict:
    """Synchronous public entrypoint. Drives the concurrent async
    implementation via ``asyncio.run`` so callers (and the test suite) see
    the same blocking signature they always have. The returned dict is
    schema-identical to the pre-concurrency version."""
    return asyncio.run(_fetch_trading_async())


def _glassnode_btc_whale_metrics_impl() -> dict:
    """Live Glassnode whale-cohort fetch. See `glassnode_btc_whale_metrics`
    for the wrapper that adds stale-fallback when the key is set but
    requests fail."""
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

    Stale-fallback: when the key is set but every metric returned an error
    (tier_status has no "ok"), serve the last good response from
    `data/.stale/glassnode_btc_whale_metrics.json`. The no-key branch is
    intentional and never triggers stale.
    """
    import os
    key_set = bool(os.environ.get("GLASSNODE_API_KEY"))
    try:
        out = _glassnode_btc_whale_metrics_impl()
    except Exception as e:
        print(f"  [glassnode] fatal: {e}", file=sys.stderr)
        out = None

    # No-key branch is intentional; never serve stale for that.
    if (
        isinstance(out, dict)
        and out.get("available") is False
        and out.get("reason") == "no GLASSNODE_API_KEY in env"
    ):
        return out

    # Key was set but every metric failed → try stale.
    if key_set and (out is None or not (isinstance(out, dict) and out.get("available"))):
        cached = _stale_load("glassnode_btc_whale_metrics")
        if cached is not None:
            return cached
        return out if isinstance(out, dict) else {
            "available": False,
            "reason": "fetch_failed",
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    if isinstance(out, dict) and out.get("available"):
        _stale_save("glassnode_btc_whale_metrics", out)
    return out if isinstance(out, dict) else {}


def _bitinfocharts_cached_distribution() -> dict:
    """Return the previous `distribution` dict from data/whale.json, if any.
    Used as the last-line fallback when the live scrape parses zero rows
    (page restructure, anti-bot block, etc.) so the dashboard doesn't blow
    away a known-good payload."""
    try:
        prev = json.loads((CACHE / "whale.json").read_text())
        d = (prev or {}).get("distribution") or {}
        if isinstance(d, dict) and d.get("buckets"):
            print("  [stale-keep] whale.distribution kept from previous fetch",
                  file=sys.stderr)
            return d
    except Exception as e:
        print(f"  [stale-keep] whale.distribution suppressed: {type(e).__name__}", file=sys.stderr)
    return {}


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

    Defensive guards:
      * Each captured cell is type-guarded before `.strip()` — the regex can
        in theory hand back non-string match groups under exotic re flags;
        only strings are stripped, others are skipped.
      * If 0 rows are parsed (network, anti-bot 403, HTML restructure) the
        previous `distribution` from `data/whale.json` is reused if present
        instead of returning empty and clobbering the cached payload.
    """
    try:
        r = requests.get(
            "https://bitinfocharts.com/bitcoin-distribution-history.html",
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X)"},
            timeout=30,
        )
        if r.status_code != 200 or not r.text:
            print(f"  [skip] bitinfocharts -> {r.status_code}", file=sys.stderr)
            return _bitinfocharts_cached_distribution()
    except Exception as e:
        print(f"  [skip] bitinfocharts -> {e}", file=sys.stderr)
        return _bitinfocharts_cached_distribution()

    pattern = re.compile(
        r'\[new Date\("(\d{4}/\d{1,2}/\d{1,2})"\)((?:,\s*-?\d+(?:\.\d+)?|,\s*null)+)\]'
    )
    rows = []
    for m in pattern.finditer(r.text):
        # Pad single-digit month / day to 2 chars
        try:
            d = datetime.strptime(m.group(1), "%Y/%m/%d").strftime("%Y-%m-%d")
        except ValueError:
            continue
        # Type-guard each split fragment: only call .strip() on strings.
        raw_cells = m.group(2).strip(",").split(",")
        vals: list[str] = []
        for v in raw_cells:
            if isinstance(v, str):
                vals.append(v.strip())
            else:
                # Non-str token — skip the whole row defensively.
                vals = []
                break
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
        return _bitinfocharts_cached_distribution()
    return {
        "labels": ["0-0.1", "0.1-1", "1-10", "10-100",
                   "100-1K", "1K-10K", "10K-100K", "100K-1M"],
        "buckets": rows,
        "source": "bitinfocharts.com",
        "note": "BTC supply held per address-balance cohort. ≥1,000 BTC = whale.",
    }


async def _fetch_whale_async(btc_price_usd: float | None = None) -> dict:
    """Concurrent implementation of ``fetch_whale``. All sources here are
    independent of each other (no per-call dependencies), so we fan them
    all out in a single asyncio.gather batch under the generic concurrency
    cap. No CoinGecko calls live in the whale tree."""
    global _cg_semaphore, _generic_semaphore
    # Recreate semaphores bound to *this* event loop. _cg_semaphore goes
    # unused here but is initialized so _bg_call / _cg_call are safe to
    # invoke from anywhere a future refactor might add them.
    _cg_semaphore = asyncio.Semaphore(CG_CONCURRENCY)
    _generic_semaphore = asyncio.Semaphore(GENERIC_CONCURRENCY)

    print("Fetching whale-activity proxies (BTC on-chain)...")
    t_total = time.monotonic()

    async def _whale_tx() -> list:
        if not btc_price_usd:
            return []
        return await _bg_call(mempool_whale_transactions, btc_price_usd)

    (
        btc, distribution, glassnode, whale_txs,
        eth_bc, eth_large_txs, eth_cm, eth_etherscan, multichain,
    ) = await asyncio.gather(
        _timed("whale_proxies_btc",                _bg_call(whale_proxies_btc)),
        _timed("bitinfocharts_btc_distribution",   _bg_call(bitinfocharts_btc_distribution)),
        _timed("glassnode_btc_whale_metrics",      _bg_call(glassnode_btc_whale_metrics)),
        _timed("mempool_whale_transactions",       _whale_tx()),
        _timed("blockchair_eth_stats",             _bg_call(blockchair_eth_stats)),
        _timed("blockchair_eth_large_transactions", _bg_call(blockchair_eth_large_transactions, 1_000_000)),
        _timed("coin_metrics_eth_whale_metrics",   _bg_call(coin_metrics_eth_whale_metrics)),
        _timed("etherscan_eth_daily",              _bg_call(etherscan_eth_daily)),
        _timed("fetch_multichain_whale_stats",     _bg_call(fetch_multichain_whale_stats)),
    )
    print(f"  [timing] fetch_whale total: {time.monotonic() - t_total:.2f}s")
    return {
        "btc": btc,
        "distribution": distribution,
        "glassnode": glassnode,
        "whale_transactions": whale_txs,
        "eth": {
            "blockchair": eth_bc,
            "coin_metrics": eth_cm,
            "large_transactions": eth_large_txs,
            "etherscan_daily": eth_etherscan,
        },
        "multichain": multichain,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": ("Free: blockchain.info + bitinfocharts cohorts + mempool.space "
                 "tx scan. ETH side via Blockchair + Coin Metrics. "
                 "Multichain (LTC/BCH/DOGE) via Blockchair /stats. "
                 "Glassnode auto-activates when GLASSNODE_API_KEY is set. "
                 "Etherscan 90d blocks-per-day series activates when "
                 "ETHERSCAN_API_KEY is set."),
    }


def fetch_whale(btc_price_usd: float | None = None) -> dict:
    """Synchronous public entrypoint for the whale tree. Drives the async
    implementation via ``asyncio.run``. Schema unchanged from the previous
    sequential version."""
    return asyncio.run(_fetch_whale_async(btc_price_usd))


def fetch_all() -> None:
    trading = fetch_trading()
    (CACHE / "market.json").write_text(json.dumps(trading))
    print(f"  wrote {CACHE/'market.json'}")
    # Pull latest BTC price from the trading dict so the whale-tx scan can
    # threshold by USD value instead of a hardcoded BTC amount.
    btc_prices = ((trading or {}).get("btc") or {}).get("price") or []
    btc_price = btc_prices[-1]["value"] if btc_prices else None
    whale = fetch_whale(btc_price_usd=btc_price)
    (CACHE / "whale.json").write_text(json.dumps(whale))
    print(f"  wrote {CACHE/'whale.json'}")


if __name__ == "__main__":
    fetch_all()
