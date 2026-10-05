"""
Free, no-API-key market and whale-activity fetchers.

Sources (all free; CoinGecko takes an optional Demo key):
  CoinGecko       price, 24h volume, market cap, daily closes for the top 50
  Coinbase / Kraken / Binance.US  keyless daily-candle fallbacks for the top 50
  Google News RSS headlines for Alpine Data's own keyword sentiment
  GitHub REST     repository stats for the Research tab
  OKX             funding rate, open interest, long/short ratio
  Deribit         DVOL (implied volatility index)
  Alternative.me  Fear & Greed Index
  blockchain.info BTC on-chain whale proxies

Output: data/market.json and data/whale.json, consumed by app.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import requests

UA = "Mozilla/5.0 (compatible; etf-flow-dashboard/1.0)"
H = {"User-Agent": UA}

# CoinGecko's free tier is free but REGISTERED. Keyless callers get ~30 req/min;
# a Demo key raises that roughly 10x. fetch_trading sweeps the top 50 coins on
# top of everything else in this file, so keyless runs 429 routinely — and a 429
# returns an empty list, which is exactly what drives the stale-keep path in
# `stale_keep_markets_top`. That is how the front-page BTC price sat frozen at
# its 2026-08-06 value for 16 days.
#
# The secret was already plumbed into CI (lthcs-crypto-daily.yml) but NOTHING
# read it — no Python file in the repo referenced COINGECKO_API_KEY at all, so
# every request still went out unauthenticated. Keyless remains a supported
# mode: an unset secret degrades to the old behaviour rather than breaking.
COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY", "").strip()

# Demo and Pro are different hosts AND different header names; sending the wrong
# pair is a 401, so key off the host we are actually calling.
_COINGECKO_KEY_HEADERS = {
    "api.coingecko.com": "x-cg-demo-api-key",
    "pro-api.coingecko.com": "x-cg-pro-api-key",
}


def _headers_for(url: str) -> dict:
    """Request headers for `url`, adding the CoinGecko key only for CoinGecko.

    `_get` is the shared helper for ~45 different upstreams. Putting the key in
    the module-level `H` would ship it to every one of them, so it is attached
    per-host here instead — a credential must never ride along to a host that
    did not issue it.
    """
    if not COINGECKO_API_KEY:
        return H
    header = _COINGECKO_KEY_HEADERS.get((urlsplit(url).hostname or "").lower())
    return {**H, header: COINGECKO_API_KEY} if header else H
ROOT = Path(__file__).parent
CACHE = ROOT / "data"
CACHE.mkdir(exist_ok=True)


# ----- helpers ---------------------------------------------------------------

def _get(url: str, params: dict | None = None, timeout: int = 25) -> dict | list | None:
    try:
        r = requests.get(url, params=params, headers=_headers_for(url), timeout=timeout)
        if r.status_code != 200:
            print(f"  [skip] {url} -> {r.status_code}", file=sys.stderr)
            return None
        return r.json()
    except Exception as e:
        print(f"  [skip] {url} -> {e}", file=sys.stderr)
        return None


def _get_status(url: str, params: dict | None = None, headers: dict | None = None,
                timeout: int = 25) -> tuple[int | None, Any]:
    """Like `_get`, but keeps the HTTP status: ``(status, parsed_json_or_None)``.

    `_get` collapses every failure to None, which is fine for a section that
    only needs "data or nothing". A section that has to TELL the reader why it
    is empty (a paywall, a missing key) needs the status, so it uses this.
    ``status`` is None when no HTTP response arrived at all. Only the bare URL
    and the exception's type are logged: a requests exception message can
    carry the full query string, and a header-borne key must stay unlogged.
    """
    try:
        r = requests.get(url, params=params, headers=headers or _headers_for(url),
                         timeout=timeout)
    except Exception as e:
        print(f"  [skip] {url} -> {type(e).__name__}", file=sys.stderr)
        return None, None
    if r.status_code != 200:
        print(f"  [skip] {url} -> {r.status_code}", file=sys.stderr)
        return r.status_code, None
    try:
        return 200, r.json()
    except ValueError:
        print(f"  [skip] {url} -> 200 with a non-JSON body", file=sys.stderr)
        return 200, None


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
        "price": _one_per_day(j.get("prices", [])),
        "volume": _one_per_day(j.get("total_volumes", [])),
        "market_cap": _one_per_day(j.get("market_caps", [])),
    }


def _one_per_day(points) -> list[dict]:
    """[[ms, value], ...] -> one {date, value} per UTC day, the LAST sample
    of each day winning.

    market_chart's daily series ends with an extra "now" sample, so today's
    date appeared twice (00:00 and the fetch instant) in price / volume /
    market_cap and in the ETH/BTC ratio derived from them, which charted two
    points for one day. The latest sample is the more current reading, and
    keeping it leaves every series' final value exactly what it was.
    """
    by_day: dict[str, float] = {}
    for p in points or []:
        if isinstance(p, (list, tuple)) and len(p) >= 2 and p[0] is not None:
            by_day[_ts(p[0])] = p[1]
    return [{"date": d, "value": v} for d, v in sorted(by_day.items())]


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


# A perp row only describes the market if the instrument is actually trading
# and its quote is recent. On 2026-10-04 every Coinbase International PERP was
# PAUSED (131) or DELISTED (133) with quotes frozen at 2026-09-03 / 2026-10-01,
# yet those frozen predicted-funding values kept feeding the Overview
# "Crypto Market Sentiment" composite as if live.
CB_INTL_MAX_QUOTE_AGE_H = 24
_CB_INTL_LAST_STATUS: dict = {}


def _parse_iso_utc(ts: Any) -> datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def coinbase_intl_perpetuals(now: datetime | None = None) -> list[dict]:
    """Coinbase International Exchange — funding rate + mark price + open
    interest for every PERP that is TRADING with a quote < 24h old. Public
    endpoint, no auth.

    Works from US IPs (Binance's /fapi endpoint returns 451 from US, this
    one returns 200). Use case: cross-exchange perpetual positioning view
    next to OKX funding. The funding rate field returned is
    `predicted_funding` from the quote object, which is the rate that will
    settle at the next funding interval — i.e. forward-looking funding,
    most useful for spotting crowded positioning right now.

    Instruments whose ``trading_state`` is not TRADING (PAUSED/DELISTED) or
    whose quote is older than ``CB_INTL_MAX_QUOTE_AGE_H`` are dropped; the
    counts and the reason land in ``_CB_INTL_LAST_STATUS`` (published as
    ``market.coinbase_intl_perps_status``) so an empty table says why.

    Returns rows sorted by funding_rate descending (most crowded long first).
    Empty list on any failure.
    """
    now = now or datetime.now(timezone.utc)
    checked_at = now.isoformat(timespec="seconds")
    _CB_INTL_LAST_STATUS.clear()
    j = _get("https://api.international.coinbase.com/api/v1/instruments")
    if not j or not isinstance(j, list):
        _CB_INTL_LAST_STATUS.update({"available": False, "checked_at": checked_at,
                                     "reason": "instruments endpoint unreachable"})
        return []
    out: list[dict] = []
    total = 0
    states: dict[str, int] = {}
    stale_quotes = 0
    newest_quote: str | None = None
    for it in j:
        if it.get("type") != "PERP":
            continue
        sym_full = it.get("symbol") or ""
        sym = sym_full.replace("-PERP", "")
        if not sym:
            continue
        total += 1
        quote = it.get("quote") or {}
        # Coinbase stamps each quote object with its own ISO-8601 UTC
        # `timestamp` (same object that carries predicted_funding), which
        # is when THE QUOTE was produced — not when we called. That is the
        # honest per-row observation time; `market.fetched_at` is not, and
        # for a stale-kept payload it would be actively wrong. Falls back
        # to the instrument-level timestamp, then to None so a missing
        # upstream timestamp renders as "unavailable" instead of "now".
        q_ts = quote.get("timestamp") or it.get("timestamp")
        q_ts = q_ts if isinstance(q_ts, str) and q_ts else None
        if q_ts and (newest_quote is None or q_ts > newest_quote):
            newest_quote = q_ts
        state = it.get("trading_state")
        if state is not None and state != "TRADING":
            states[str(state)] = states.get(str(state), 0) + 1
            continue
        q_dt = _parse_iso_utc(q_ts)
        if q_dt is None or (now - q_dt) > timedelta(hours=CB_INTL_MAX_QUOTE_AGE_H):
            stale_quotes += 1
            continue
        try:
            out.append({
                "symbol":         sym,
                "funding_rate":   float(quote.get("predicted_funding") or 0),
                "mark_price":     float(quote.get("mark_price") or 0),
                "index_price":    float(quote.get("index_price") or 0),
                "open_interest_base": float(it.get("open_interest") or 0),
                "volume_24h":     float(it.get("qty_24hr") or 0),
                "notional_24h":   float(it.get("notional_24hr") or 0),
                # `as_of` is the YYYY-MM-DD every other row type in this
                # payload uses; `as_of_ts` keeps the full precision that
                # actually matters for an 8-hourly funding rate.
                "as_of":          (q_ts[:10] if q_ts else None),
                "as_of_ts":       q_ts,
            })
        except (ValueError, TypeError):
            continue
    out.sort(key=lambda r: r["funding_rate"], reverse=True)
    status = {"available": bool(out), "checked_at": checked_at,
              "perps_total": total, "trading_fresh": len(out),
              "excluded_by_state": states, "excluded_stale_quote": stale_quotes,
              "max_quote_age_hours": CB_INTL_MAX_QUOTE_AGE_H,
              "newest_quote": newest_quote}
    if not out:
        bits = [f"{n} {st}" for st, n in sorted(states.items())]
        if stale_quotes:
            bits.append(f"{stale_quotes} with quotes older than {CB_INTL_MAX_QUOTE_AGE_H}h")
        status["reason"] = (f"no Coinbase International perp is trading with a fresh quote "
                            f"({', '.join(bits) or 'no PERP instruments'}"
                            + (f"; newest quote {newest_quote}" if newest_quote else "") + ")")
    _CB_INTL_LAST_STATUS.update(status)
    return out


def perp_funding_summary(funding_by_symbol: dict, now: datetime | None = None,
                         max_age_days: int = 2) -> dict:
    """Perp-funding input for the Overview sentiment composite, from OKX.

    ``funding_by_symbol`` maps a symbol to ``okx_funding`` output (daily mean
    of OKX's per-settlement rates, oldest->newest). Each symbol contributes its
    newest row if that row is within ``max_age_days``; ``as_of`` is the OLDEST
    contributing date (a composite is only as fresh as its oldest input).
    Replaces the Coinbase International perps, which stopped trading."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=max_age_days)).strftime("%Y-%m-%d")
    rows, excluded = [], []
    for sym, series in (funding_by_symbol or {}).items():
        last = next((r for r in reversed(series or [])
                     if isinstance(r, dict) and r.get("rate") is not None
                     and isinstance(r.get("date"), str)), None)
        if last is None:
            excluded.append({"symbol": sym, "reason": "no funding rows"})
            continue
        if last["date"][:10] < cutoff:
            excluded.append({"symbol": sym, "reason": f"newest row {last['date'][:10]} is stale"})
            continue
        rows.append({"symbol": sym, "rate": float(last["rate"]), "as_of": last["date"][:10]})
    out = {
        "source": "OKX USDT-margined perpetual swaps (daily mean of settlement funding rates)",
        "available": bool(rows),
        "rows": rows,
        "excluded": excluded,
        "avg_rate": (sum(r["rate"] for r in rows) / len(rows)) if rows else None,
        "as_of": min(r["as_of"] for r in rows) if rows else None,
    }
    if not rows:
        out["reason"] = "no OKX funding row within %d days" % max_age_days
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
    #
    # `as_of` IS read: it is the only honest observation date the top-50
    # tail carries. CoinGecko stamps every /coins/markets row with
    # `last_updated` (ISO-8601 UTC, e.g. "2026-08-02T16:49:31.736Z") — the
    # moment CG itself last repriced that coin, NOT the moment we called
    # them. signals.compute_signal_simple copies it onto every
    # signals_top20 entry so a freshness stamp can report the age of the
    # DATA. Never substitute a local clock here: when the whole list is
    # stale-kept (see `_fetch_trading_async`) these rows are copied forward
    # verbatim and their as_of must stay frozen at the original
    # observation, which is exactly what makes the frozen-chart bug
    # visible instead of invisible.
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
            # None (not today's date) when CG omits it — an explicit
            # "unavailable" beats a fabricated stamp.
            "as_of": (str(c.get("last_updated") or "")[:10]) or None,
        })
    return out


# How long a carried-forward markets_top row may keep being served. Matches the
# 7d bound already used for poc_top; a crypto price older than a week is not a
# price, and the hourly cron means 7d == ~168 consecutive failed fetches.
MARKETS_TOP_STALE_MAX_DAYS = 7


def stale_keep_markets_top() -> list[dict]:
    """Previous ``markets_top`` list, flagged, for when CoinGecko returns [].

    CoinGecko 429 (rate-limit wipe) returns an empty list. The semaphore +
    0.6s gap helps, but a fresh-cache 429 from upstream contention is still
    possible — preserve the last good list instead of clobbering cache.

    Every carried-forward row gains ``stale: True`` and keeps its ORIGINAL
    ``as_of`` (see `coingecko_top_markets`). Both matter, and for different
    reasons: the frozen ``as_of`` is what lets the stamp age visibly, and
    the flag is what lets the UI disclose "N of M served from cache"
    instead of printing one confident date over a cache-served list. There
    is deliberately no clock in this function.

    BOUNDED, for the same reason ``poc_top`` is (see ``stale_keep_poc_top``).
    market.json is never committed — it is restored from the Actions cache on
    every run — so an unbounded carry-forward re-serves the same prices
    indefinitely. That is not hypothetical: BTC sat at its 2026-08-06 price
    for 16 days while the page looked live, because this function had no
    expiry and every row was copied forward on each failed fetch.

    Past ``MARKETS_TOP_STALE_MAX_DAYS`` a row is dropped rather than re-served.
    A coin missing from the table is visible; a confidently-rendered stale
    price is not.

    Returns ``[]`` when there is nothing to carry forward.
    """
    path = CACHE / "market.json"
    if not path.exists():
        return []
    try:
        prev = json.loads(path.read_text()).get("markets_top") or []
    except Exception as e:
        print(f"  [stale-keep] failed to read previous markets_top: {e}", file=sys.stderr)
        return []

    now = datetime.now(timezone.utc)

    def _age_days(row: dict) -> "float | None":
        """Age of a row's ORIGINAL observation, from its frozen ``as_of``.

        ``as_of`` is pinned when the row is first fetched and is never advanced
        by a carry-forward, so it keeps ageing across runs. A row with no
        ``as_of`` (CoinGecko omitted ``last_updated``) has no provable
        observation date and is treated as expired — an unprovable price is
        exactly what should not be re-served.
        """
        iso = row.get("as_of")
        if not isinstance(iso, str) or not iso.strip():
            return None
        try:
            d = datetime.strptime(iso.strip()[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        return (now - d).total_seconds() / 86400.0

    kept: list[dict] = []
    expired = 0
    for r in prev:
        if not isinstance(r, dict):
            continue
        age = _age_days(r)
        if age is None or age > MARKETS_TOP_STALE_MAX_DAYS:
            expired += 1
            continue
        kept.append({**r, "stale": True, "stale_age_days": round(age, 2)})

    if kept:
        print(f"  [stale-keep] markets_top empty from API; kept {len(kept)} from previous fetch")
    if expired:
        print(f"  [stale-keep] dropped {expired} markets_top row"
              f"{'' if expired == 1 else 's'} with no as_of or older than "
              f"{MARKETS_TOP_STALE_MAX_DAYS}d - refusing to re-serve them as live",
              file=sys.stderr)
    return kept


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


def _pct_change(now_v, then_v):
    """Percent change now vs then, or None when either side is unusable."""
    try:
        now_f, then_f = float(now_v), float(then_v)
    except (TypeError, ValueError):
        return None
    if then_f == 0:
        return None
    return (now_f / then_f - 1.0) * 100.0


def _chain_changes_from_history(points: Any) -> dict:
    """1d/7d/30d % change from a ``/v2/historicalChainTvl/{chain}`` body.

    Compares the newest daily point with the point exactly 1/7/30 UTC days
    earlier (matched by day, not by list offset, so a gap in the series yields
    None instead of a change over the wrong span)."""
    by_day: dict[int, float] = {}
    for p in points or []:
        try:
            by_day[int(p.get("date")) // 86400] = float(p.get("tvl"))
        except (TypeError, ValueError, AttributeError):
            continue
    if not by_day:
        return {}
    last = max(by_day)
    cur = by_day[last]
    return {
        "change_1d_pct": _pct_change(cur, by_day.get(last - 1)),
        "change_7d_pct": _pct_change(cur, by_day.get(last - 7)),
        "change_1m_pct": _pct_change(cur, by_day.get(last - 30)),
        "change_as_of": datetime.fromtimestamp(last * 86400, tz=timezone.utc).strftime("%Y-%m-%d"),
    }


def defillama_chains(top: int = 20) -> list[dict]:
    """TVL across all blockchain ecosystems (Ethereum, Solana, etc.).

    DeFiLlama's free ``/v2/chains`` stopped returning ``change_1d/7d/1m``
    (verified 2026-10-04: only name/tvl/tokenSymbol/gecko_id/cmcId/chainId),
    which left every chain's change null and starved the TVL-momentum
    composite. When the snapshot lacks them, the changes are derived per
    top-N chain from ``/v2/historicalChainTvl/{chain}``; a chain whose
    history cannot be fetched keeps null (never a guessed 0)."""
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
    chains = chains[:top]
    for c in chains:
        if all(c.get(k) is not None for k in ("change_1d_pct", "change_7d_pct", "change_1m_pct")):
            continue
        if not c.get("name"):
            continue
        hist = _get(f"https://api.llama.fi/v2/historicalChainTvl/{c['name']}")
        derived = _chain_changes_from_history(hist if isinstance(hist, list) else [])
        if not derived:
            continue
        for k in ("change_1d_pct", "change_7d_pct", "change_1m_pct"):
            if c.get(k) is None:
                c[k] = derived.get(k)
        c["change_source"] = "historicalChainTvl"
        c["change_as_of"] = derived.get("change_as_of")
    return chains


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


def _protocols2_enrichment(j: Any) -> tuple[dict, dict]:
    """(by_name, parent_by_id) from DeFiLlama ``/lite/protocols2``.

    That endpoint still carries ``tvlPrevMonth`` per protocol and a
    ``parentProtocols`` list with each parent token's ``mcap``."""
    if not isinstance(j, dict):
        return {}, {}
    by_name = {p.get("name"): p for p in (j.get("protocols") or [])
               if isinstance(p, dict) and p.get("name")}
    parents = {p.get("id"): p for p in (j.get("parentProtocols") or [])
               if isinstance(p, dict) and p.get("id")}
    return by_name, parents


def defillama_protocols(top: int = 25) -> list[dict]:
    """Top DeFi protocols by TVL with 1d/7d/1m changes.

    ``/protocols`` no longer returns ``change_1m`` (null on 25/25 rows) and
    reports ``mcap`` only for protocols that own a CoinGecko id, so child
    protocols (Aave V3, Morpho Blue, ...) read null. Both are filled from
    ``/lite/protocols2``: ``change_1m`` from its ``tvl``/``tvlPrevMonth`` and
    ``mcap`` from the parent protocol's token (tagged ``mcap_source``). Rows
    that still have no value (CEX entries carry no 30d baseline; tokenless
    entries have no market cap) say why in ``unavailable``."""
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
            "_parent": p.get("parentProtocol"),
        })
    out.sort(key=lambda x: x.get("tvl_usd") or 0, reverse=True)
    out = out[:top]
    by_name: dict = {}
    parents: dict = {}
    if any(r.get("change_1m_pct") is None or r.get("mcap_usd") is None for r in out):
        by_name, parents = _protocols2_enrichment(_get("https://api.llama.fi/lite/protocols2"))
    for r in out:
        parent_id = r.pop("_parent", None)
        lite = by_name.get(r.get("name")) or {}
        missing: dict = {}
        if r.get("change_1m_pct") is None:
            r["change_1m_pct"] = _pct_change(lite.get("tvl"), lite.get("tvlPrevMonth"))
            if r["change_1m_pct"] is None:
                missing["change_1m_pct"] = (
                    "no 30d TVL baseline published for CEX entries"
                    if (r.get("category") == "CEX") else "no 30d TVL baseline from DeFiLlama")
        if r.get("mcap_usd") is None:
            parent = parents.get(parent_id) if parent_id else None
            if parent and parent.get("mcap") is not None:
                r["mcap_usd"] = parent.get("mcap")
                r["mcap_source"] = f"parent token ({parent.get('symbol') or parent.get('name')})"
            elif not r.get("symbol") or r.get("symbol") == "-":
                missing["mcap_usd"] = "no token"
            else:
                missing["mcap_usd"] = "market cap not reported by DeFiLlama"
        if missing:
            r["unavailable"] = missing
    return out


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


DEFILLAMA_BRIDGES_URL = "https://bridges.llama.fi/bridges"


def _bridges_rows(j: Any) -> list[dict]:
    """Top-10 bridges by 24h volume from a DeFiLlama ``/bridges`` body."""
    if not isinstance(j, dict):
        return []
    bridges = j.get("bridges") or []
    if not isinstance(bridges, list):
        return []
    bridges = [b for b in bridges if isinstance(b, dict)]
    bridges.sort(key=lambda b: (b.get("lastDailyVolume") or 0), reverse=True)
    return [
        {
            "name": b.get("displayName") or b.get("name"),
            "daily_volume_usd": b.get("lastDailyVolume"),
            "weekly_volume_usd": b.get("lastWeeklyVolume"),
            "monthly_volume_usd": b.get("lastMonthlyVolume"),
            "chains": b.get("chains") or [],
        }
        for b in bridges[:10]
    ]


def _bridges_unavailable_reason(status: int | None) -> str:
    if status == 402:
        return ("DeFiLlama moved its bridges API to the paid Pro plan "
                "(HTTP 402 Payment Required)")
    if status is None:
        return "DeFiLlama bridges API did not respond"
    if status == 200:
        return "DeFiLlama bridges API returned no bridges"
    return f"DeFiLlama bridges API returned HTTP {status}"


def defillama_bridges(now: datetime | None = None) -> dict:
    """Cross-chain bridge volume snapshot, or an explicit "unavailable" record.

    The legacy ``api.llama.fi/bridges`` route 404s; the API moved to
    ``bridges.llama.fi``. Since 2026-10 that host answers every bridges route
    with HTTP 402 "Upgrade to the paid API plan", and DeFiLlama's API docs now
    list all bridges endpoints as Pro-only (``pro-api.llama.fi/{key}/bridges/…``,
    paid subscription). There is no keyless equivalent to fall back to:
    ``/overview/bridge-aggregators`` is aggregator routing volume and protocol
    TVL is not volume, so substituting either would put a different metric
    under this card's title.

    So instead of an empty list the UI silently hides, a failed fetch returns
    ``available: False`` with the reason, the HTTP status and when it was
    checked, and the card says so. ``top_bridges`` stays a list in every case
    (payload schema unchanged); there is no stale-keep, so an empty list here
    contributes no date to ``defi_provenance``. Never raises.
    """
    checked_at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    status: int | None = None
    rows: list[dict] = []
    try:
        status, j = _get_status(DEFILLAMA_BRIDGES_URL)
        if status == 200:
            rows = _bridges_rows(j)
    except Exception as e:  # parse surprises must not take down fetch_trading
        print(f"  [defillama_bridges] {type(e).__name__}", file=sys.stderr)
    if rows:
        return {"top_bridges": rows, "available": True, "checked_at": checked_at}
    return {
        "top_bridges": [],
        "available": False,
        "http_status": status,
        "reason": _bridges_unavailable_reason(status),
        "checked_at": checked_at,
    }


def _series_last_date(rows: list | None) -> str | None:
    """Newest ``YYYY-MM-DD`` in a ``[{date: ...}, ...]`` series, or None."""
    if not isinstance(rows, list) or not rows:
        return None
    best: str | None = None
    for r in rows:
        if not isinstance(r, dict):
            continue
        d = r.get("date")
        if not isinstance(d, str) or len(d) < 10:
            continue
        d = d[:10]
        try:
            datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            continue
        if best is None or d > best:
            best = d
    return best


def defi_provenance(chains: list | None, protocols: list | None,
                    yields_stablecoin: list | None, bridges: dict | None,
                    tvl_history: dict | None,
                    observed_at: datetime | None = None) -> dict:
    """Derive an honest observation date for the DeFi subtree.

    The DeFi block is a composite of five independently-fetched inputs, so
    per rule "a composite is only as fresh as its oldest input" the
    returned ``as_of`` is the MINIMUM of the contributing dates — never the
    newest, never an average.

    Where each date comes from:

      * ``tvl_history`` — DeFiLlama's own daily timestamps. A real
        observation date; used as-is (per chain, then min-ed).
      * ``chains`` / ``protocols`` / ``yields_stablecoin`` / ``bridges`` —
        DeFiLlama serves these as *current* snapshots with no upstream
        timestamp of any kind, so for a snapshot that came back populated
        the observation instant genuinely IS the fetch instant. That is the
        one case where the clock is the right answer, and it is safe here
        for a specific reason: none of these four has a stale-keep path, so
        an input that fails this run arrives EMPTY and contributes no date
        at all rather than a cached payload wearing a fresh stamp.
      * An input that is empty contributes nothing. If every input is
        empty the result is ``{"as_of": None, ...}`` and the UI is expected
        to render an explicit unavailable state.

    ``observed_at`` is injectable so tests can pin the clock.

    Returns ``{"as_of", "observed_at", "sources"}`` where ``sources`` maps
    each input to its own date (or None) so a UI can name the laggard
    rather than just showing the min.
    """
    now = observed_at or datetime.now(timezone.utc)
    snapshot_date = now.strftime("%Y-%m-%d")

    sources: dict[str, Any] = {
        "chains":            snapshot_date if chains else None,
        "protocols":         snapshot_date if protocols else None,
        "yields_stablecoin": snapshot_date if yields_stablecoin else None,
        "bridges":           snapshot_date if (bridges or {}).get("top_bridges") else None,
    }
    tvl_dates: dict[str, str | None] = {}
    for name, rows in (tvl_history or {}).items():
        tvl_dates[name] = _series_last_date(rows)
    sources["tvl_history"] = tvl_dates

    candidates = [d for d in sources.values() if isinstance(d, str)]
    candidates += [d for d in tvl_dates.values() if isinstance(d, str)]
    return {
        "as_of": min(candidates) if candidates else None,
        # Wall-clock of the fetch. Named so nobody mistakes it for a data
        # date: it exists for debugging "when did this run last", and must
        # NOT be used as a freshness stamp.
        "observed_at": now.isoformat(timespec="seconds"),
        "sources": sources,
    }


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


def _sec_headers() -> dict:
    """SEC fair-access headers. Prefer the operator-supplied SEC_USER_AGENT
    secret (a real contact, as SEC asks) and fall back to the generic UA."""
    ua = (os.environ.get("SEC_USER_AGENT") or "").strip() or SEC_UA
    return {"User-Agent": ua, "Accept": "application/json"}


def _sec_get(url: str, params: dict | None = None, timeout: int = 20):
    """SEC-flavored requests.get that always uses the polite UA. Returns
    the parsed JSON (or text for non-JSON endpoints) or None on failure.
    Honors EDGAR's preferred 10 req/sec ceiling implicitly by being called
    serially in the fetcher with a small sleep between calls."""
    try:
        r = requests.get(url, params=params, headers=_sec_headers(), timeout=timeout)
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
    # Live EDGAR schema nests the date: <dateOfFirstSale><value>YYYY-MM-DD
    # </value></dateOfFirstSale>, or <yetToOccur>true</yetToOccur> when no
    # sale has happened. Older/flat docs carry the date as direct text.
    first_sale = _ftext("dateOfFirstSale/value") or _ftext("dateOfFirstSale")
    if not first_sale and _ftext("dateOfFirstSale/yetToOccur").lower() == "true":
        first_sale = "yet to occur"
    out["date_of_first_sale"] = first_sale
    try:
        # Live schema: <federalExemptionsExclusions><item>06b</item>...;
        # some docs use <exemption> leaves instead.
        ex_nodes = (root.findall(".//federalExemptionsExclusions/item")
                    or root.findall(".//exemption"))
        out["exemptions"] = [
            (n.text or "").strip() for n in ex_nodes if n.text and n.text.strip()
        ]
    except Exception:
        out["exemptions"] = []
    return out


# Server-side EDGAR full-text query. The unfiltered Form D firehose is
# >=10,000 filings per 60 days and EDGAR caps paging at 10,000, so scanning
# "the first 100 hits" (the old approach) only ever saw ONE filing day. Asking
# EDGAR for the AI terms instead returns ~200 hits for 60 days, which we page
# through completely; the issuer-name matcher below then keeps only names
# that are themselves AI-adjacent (a full-text hit can come from a related
# person's name or an address).
_SEC_FTS_QUERY = " OR ".join(
    f'"{kw}"' if (" " in kw or "." in kw) else kw
    for kw in ("ai", "artificial intelligence", "machine learning", "neural",
               "deep learning", "gpt", "llm", "agentic", "robotics",
               "autonomous", "openai", "anthropic", "inference")
)
_SEC_FTS_PAGE = 100          # EDGAR FTS page size (fixed upstream)
_SEC_FTS_MAX_PAGES = 20      # hard stop: 2,000 hits; a real 60d window is ~2-3 pages
_SEC_MIN_GAP_S = 0.15        # ~6 req/s, under SEC's 10 req/s fair-access ceiling

# Coverage of the most recent sweep, published next to the rows so the UI can
# say "N of M" instead of implying the list is the whole window.
_SEC_FORM_D_LAST_COVERAGE: dict = {}


def _fetch_sec_form_d_filings_impl(
    days: int = 60,
    max_results: int = 20,
    enrich_details: bool = True,
) -> list[dict]:
    """Pull recent Form D filings from EDGAR, filter to AI-adjacent issuers,
    optionally enrich each with offering-amount fields from primary_doc.xml.

    Steps:
      1. Full-text search ``forms=D`` over ``[today-days, today]`` with the AI
         terms as a server-side OR query, paging with ``from=`` until every
         hit is read (sleeping between pages for SEC's 10 req/s limit).
      2. Keep hits whose issuer display_name passes ``_ai_keyword_hit``,
         de-duplicated by accession, newest first. Exemptions come straight
         from the hit's ``items`` (e.g. 06B, 3C.7).
      3. Take the top `max_results`. If `enrich_details` is True, fetch
         each filing's primary_doc.xml for offering amounts and date of
         first sale.

    Returns a list of dicts ready for the AI tab renderer.
    """
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    base = {
        "q": _SEC_FTS_QUERY,
        "forms": "D",
        "dateRange": "custom",
        "startdt": start.isoformat(),
        "enddt": end.isoformat(),
    }
    hits: list = []
    total = None
    pages = 0
    for page in range(_SEC_FTS_MAX_PAGES):
        params = dict(base)
        if page:
            params["from"] = page * _SEC_FTS_PAGE
            time.sleep(_SEC_MIN_GAP_S)
        j = _sec_get("https://efts.sec.gov/LATEST/search-index", params=params)
        if not isinstance(j, dict):
            if page == 0:
                return []
            break  # keep what earlier pages returned
        pages += 1
        h = (j.get("hits") or {})
        if total is None:
            t = h.get("total")
            total = t.get("value") if isinstance(t, dict) else t
        batch = h.get("hits") or []
        hits.extend(batch)
        if len(batch) < _SEC_FTS_PAGE:
            break
        if isinstance(total, int) and len(hits) >= total:
            break

    rows: list[dict] = []
    seen: set = set()
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
        adsh = src.get("adsh") or (raw_id.split(":", 1)[0] if raw_id else "")
        if adsh and adsh in seen:
            continue
        seen.add(adsh)
        ciks = src.get("ciks") or []
        cik = ciks[0] if ciks else ""
        file_date = src.get("file_date") or ""
        items = [str(x) for x in (src.get("items") or []) if x]
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
            # The search hit already carries the claimed exemptions; the XML
            # pass overwrites them only if it finds its own list.
            "exemptions": items,
        })

    rows.sort(key=lambda r: r.get("filed_date") or "", reverse=True)
    matched = len(rows)
    rows = rows[:max_results]
    _SEC_FORM_D_LAST_COVERAGE.clear()
    _SEC_FORM_D_LAST_COVERAGE.update({
        "window_days": days,
        "window": [start.isoformat(), end.isoformat()],
        "fts_hits_total": total,
        "fts_hits_scanned": len(hits),
        "pages": pages,
        "complete": isinstance(total, int) and len(hits) >= total,
        "ai_matches": matched,
        "shown": len(rows),
    })

    if enrich_details and rows:
        for row in rows:
            if not row.get("cik") or not row.get("accession"):
                continue
            url = _sec_primary_doc_url(row["cik"], row["accession"])
            xml_text = _sec_get(url)
            # Be polite — sleep between filing fetches (~6 req/sec
            # ceiling, well under SEC's 10 req/sec limit).
            time.sleep(_SEC_MIN_GAP_S)
            if not isinstance(xml_text, str):
                continue
            parsed = _parse_form_d_xml(xml_text)
            # Only overwrite with values the XML actually carried, so the
            # exemptions taken from the search hit survive an XML without them.
            row.update({k: v for k, v in parsed.items()
                        if v not in (None, "", [])})

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
    _SEC_FORM_D_LAST_COVERAGE.clear()
    form_d = fetch_sec_form_d_filings(60, 50, True)
    form_d_cov = dict(_SEC_FORM_D_LAST_COVERAGE) or {"window_days": 60, "stale_fallback": True}
    print(f"    -> {len(form_d)} Form D filings (AI-adjacent; "
          f"{form_d_cov.get('ai_matches', '?')} matched in window)")
    return {
        "yc_companies": yc.get("yc_companies", []),
        "yc_total_ai_count": yc.get("yc_total_ai_count", 0),
        "recent_funding_news": hn_news,
        "form_d_filings": form_d,
        "form_d_coverage": form_d_cov,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


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
    # A few cache keys embed upstream API symbols/ids (e.g. CoinGecko 'symbol'/'id'),
    # so strip path separators + traversal before joining (CodeQL py/path-injection).
    safe = "".join(c if (c.isalnum() or c in "_.-") else "_" for c in Path(str(funcname)).name)
    # Normalise and confirm the result is still directly inside the stale dir.
    # Unreachable with the character filter above; kept as an explicit
    # containment check so the guarantee doesn't rest on the filter alone.
    base = os.path.normpath(_STALE_DIR)
    full = os.path.normpath(os.path.join(base, f"{safe}.json"))
    if not full.startswith(base + os.sep):
        raise ValueError(f"unsafe stale-cache key: {funcname!r}")
    return Path(full)


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


def _stale_flags(src) -> dict:
    """Re-surface a stale-fallback tag from a fetcher result onto a payload block.

    `coingecko_market()` returns a cache-served dict already tagged
    `{"stale": True, "stale_age_sec": N}` by `_stale_load`. But `fetch_trading`
    rebuilds each per-asset block key by key (`"price": btc_mkt["price"]`, ...),
    which dropped the tag before it ever reached the browser.

    That mattered: the dashboard's freshness strip counts entries flagged
    `stale` to render "N of M cached" (see `freshness()` in app.py, rule 4).
    With the tag lost in transit, crypto prices could only ever report zero
    cached — so a BTC price served from cache for 16 days displayed with no
    cache disclosure at all, next to a date that looked current.

    Returns `{}` for anything not flagged, so it is safe to `**`-splat
    unconditionally.
    """
    if not isinstance(src, dict) or src.get("stale") is not True:
        return {}
    out: dict = {"stale": True}
    age = src.get("stale_age_sec")
    if isinstance(age, int):
        out["stale_age_sec"] = age
    return out


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
    ua = ("alpine-data/1.0 (+https://github.com/btabiado/alpine-data) "
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


# ----- community + developer stats (CoinGecko profile + GitHub REST) ----------
#
# This card used to read CryptoCompare /data/social/coin/latest (Twitter,
# Reddit, GitHub). That endpoint needs a paid CoinDesk Data key since 2026-10.
# CoinGecko's /coins/{id} was the planned replacement, but CoinGecko stopped
# returning its `community_data` and `developer_data` objects on 2026-08-28
# (the query params are still accepted and ignored — see
# docs.coingecko.com/reference/coins-id). So the card carries only what a free
# source really publishes:
#
#   CoinGecko /coins/{id}  watchlist_portfolio_users and the up/down vote split
#                          (CoinGecko's own users' votes, labelled as such)
#   GitHub REST /repos     stars, forks, watchers, open issues + PRs, last push,
#                          commits to the default branch in the last 30 days
#
# Twitter/X followers and Reddit activity have no free source here any more.
# They are NOT emitted (never zero-filled); the payload lists them under
# `unavailable` with the reason, and Reddit subscriber counts stay on the
# Reddit cards (Reddit's own API). Refreshed once per UTC day — these are slow
# counters — which keeps the CoinGecko spend at 4 calls/day.

COMMUNITY_DEV_COINS: tuple[tuple[str, str, str, str], ...] = (
    # (asset key, CoinGecko id, display name, primary GitHub repository)
    ("btc",  "bitcoin",   "Bitcoin",   "bitcoin/bitcoin"),
    ("eth",  "ethereum",  "Ethereum",  "ethereum/go-ethereum"),
    ("link", "chainlink", "Chainlink", "smartcontractkit/chainlink"),
    ("ltc",  "litecoin",  "Litecoin",  "litecoin-project/litecoin"),
)
GITHUB_API = "https://api.github.com"
COMMUNITY_DEV_UNAVAILABLE = {
    "twitter_followers": ("CoinGecko stopped returning community_data on 2026-08-28 and "
                          "X/Twitter has no free API, so follower counts are not shown."),
    "reddit_subscribers": ("No longer in CoinGecko's response (community_data removed "
                           "2026-08-28). Subscriber counts are on the Reddit cards, from "
                           "Reddit's own API."),
    "reddit_active_users": "Same as reddit_subscribers.",
    "github_open_pulls": ("GitHub's repository endpoint counts open issues and pull requests "
                          "together; shown combined as github_open_issues_and_prs."),
}
COMMUNITY_DEV_SOURCES = {
    "coingecko": "CoinGecko /coins/{id}: watchlist_portfolio_users, sentiment_votes_up/down_percentage",
    "github": "GitHub REST /repos/{owner}/{repo} and /commits?since=<30 days ago>",
}
_COMMUNITY_DEV_CACHE_KEY = "community_dev_stats"


def _github_headers(url: str) -> dict:
    """GitHub REST headers. The Actions GITHUB_TOKEN (if mapped) rides along
    ONLY to api.github.com, never to another host — same rule as the CoinGecko
    key in `_headers_for`."""
    h = {**H, "Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28"}
    tok = os.environ.get("GITHUB_TOKEN", "").strip()
    if tok and (urlsplit(url).hostname or "").lower() == "api.github.com":
        h["Authorization"] = f"Bearer {tok}"
    return h


def _github_last_page(link_header: str | None) -> int | None:
    """The page number of rel="last" in a GitHub ``Link`` header, or None."""
    for part in (link_header or "").split(","):
        if 'rel="last"' in part:
            m = re.search(r"[?&]page=(\d+)", part)
            if m:
                return int(m.group(1))
    return None


def github_repo_stats(repo: str, now: datetime | None = None) -> dict | None:
    """Stars / forks / watchers / open issues+PRs / last push for `repo`, plus
    the number of commits on its default branch in the last 30 days (counted
    from the Link header of a per_page=1 listing: one request, exact count)."""
    if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        return None
    url = f"{GITHUB_API}/repos/{repo}"
    status, j = _get_status(url, headers=_github_headers(url), timeout=15)
    if status != 200 or not isinstance(j, dict):
        return None
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    commits_30d = None
    curl = f"{url}/commits"
    try:
        r = requests.get(curl, params={"since": since, "per_page": "1"},
                         headers=_github_headers(curl), timeout=15)
        if r.status_code == 200:
            last = _github_last_page(r.headers.get("Link"))
            body = r.json()
            commits_30d = last if last is not None else (len(body) if isinstance(body, list) else None)
        else:
            print(f"  [github] {repo} commits -> {r.status_code}", file=sys.stderr)
    except Exception as e:
        print(f"  [github] {repo} commits: {type(e).__name__}", file=sys.stderr)
    return {
        "github_repo": repo,
        "github_stars": j.get("stargazers_count"),
        "github_forks": j.get("forks_count"),
        "github_watchers": j.get("subscribers_count"),
        "github_open_issues_and_prs": j.get("open_issues_count"),
        "github_pushed_at": j.get("pushed_at"),
        "github_commits_30d": commits_30d,
        "github_commits_since": since[:10],
    }


def coingecko_coin_profile(coin_id: str) -> tuple[int | None, dict | None]:
    """The community fields CoinGecko still publishes for a coin (one call)."""
    if not isinstance(coin_id, str) or not _CG_ID_RE.match(coin_id):
        return None, None
    status, j = _get_status(
        f"https://api.coingecko.com/api/v3/coins/{coin_id}",
        {"localization": "false", "tickers": "false", "market_data": "false",
         "community_data": "false", "developer_data": "false", "sparkline": "false"},
        timeout=20,
    )
    if status != 200 or not isinstance(j, dict):
        return status, None
    prof = {
        "coingecko_watchlist_users": j.get("watchlist_portfolio_users"),
        "coingecko_votes_up_pct": j.get("sentiment_votes_up_percentage"),
        "coingecko_votes_down_pct": j.get("sentiment_votes_down_percentage"),
        "coingecko_last_updated": j.get("last_updated"),
    }
    if all(v is None for k, v in prof.items() if k != "coingecko_last_updated"):
        return status, None
    return status, prof


def community_dev_stats(now: datetime | None = None, *, pace_s: float = 1.0) -> dict:
    """Per-coin community + developer stats for BTC/ETH/LINK/LTC (the Research
    tab card). Once per UTC day: a result already observed today is reused
    as-is (its ``fetched_at`` is the real time it was fetched)."""
    now = now or datetime.now(timezone.utc)
    today = _utc_today(now).isoformat()
    cached = _stale_read_raw(_COMMUNITY_DEV_CACHE_KEY)
    if isinstance(cached, dict) and cached.get("available") and cached.get("observed_date") == today:
        return {**cached, "coingecko_calls": 0, "cache": "today"}
    coins: dict[str, dict] = {}
    cg_calls = 0
    for key, cg_id, name, repo in COMMUNITY_DEV_COINS:
        row: dict[str, Any] = {"name": name, "coingecko_id": cg_id}
        _status, prof = coingecko_coin_profile(cg_id)
        cg_calls += 1
        gh = github_repo_stats(repo, now)
        if prof:
            row.update(prof)
        if gh:
            row.update(gh)
        if prof or gh:
            row["sources"] = [lbl for lbl, ok in (("CoinGecko", prof), ("GitHub", gh)) if ok]
            coins[key] = row
        if pace_s:
            time.sleep(pace_s)
    out = {
        "available": bool(coins),
        "coins": coins,
        "sources": COMMUNITY_DEV_SOURCES,
        "unavailable": COMMUNITY_DEV_UNAVAILABLE,
        "observed_date": today,
        "coingecko_calls": cg_calls,
        "fetched_at": now.isoformat(timespec="seconds"),
    }
    if coins:
        _stale_save(_COMMUNITY_DEV_CACHE_KEY, out)
    return out


# ----- headline sentiment, computed by Alpine Data ----------------------------
#
# Replaces CryptoCompare's data-api news list and its POSITIVE/NEGATIVE/NEUTRAL
# labels (paid key since 2026-10). The headlines now come from Google News RSS
# search (free, keyless) and the sentiment is OUR OWN, computed here by a small
# committed keyword rule — not CoinDesk's/CryptoCompare's labels, not a paid
# API, not an LLM. The rule, the word lists and the source ship with every
# payload under `method`, and every count says how many headlines it is out of.

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
HEADLINE_WINDOW_DAYS = 7
GOOGLE_NEWS_SAMPLE_CAP = 100   # Google News RSS returns at most this many items

# Generic noise filtered out of the keyword-cloud aggregation.
_HEADLINE_STOPWORDS = {
    "crypto", "cryptocurrency", "cryptocurrencies", "blockchain", "market",
    "markets", "news", "price", "prices", "trading", "trader", "traders", "coin",
    "coins", "token", "tokens", "update", "report", "analysis", "the", "and",
    "for", "with", "from", "into", "after", "amid", "over", "this", "that",
    "what", "why", "how", "will", "could", "would", "can", "may", "says", "said",
    "its", "are", "was", "were", "has", "have", "had", "not", "but", "you",
    "your", "new", "now", "today", "week", "more", "than", "out", "about", "who",
    "here", "just", "top", "all", "his", "her", "they", "their", "our", "per",
    "via", "vs", "off", "under", "near", "next", "first", "year", "years", "day",
    "days", "usd", "price-prediction", "prediction", "predictions", "million",
    "billion", "january", "february", "march", "april", "june", "july", "august",
    "september", "october", "november", "december", "monday", "tuesday",
    "wednesday", "thursday", "friday", "saturday", "sunday",
}
# Per-coin aliases also dropped (don't show "bitcoin" as a tag on the BTC card)
_COIN_ALIASES = {
    "BTC":  {"btc", "bitcoin", "xbt"},
    "ETH":  {"eth", "ethereum", "ether"},
    "LINK": {"link", "chainlink"},
    "LTC":  {"ltc", "litecoin"},
}


def _strip_publisher_suffix(title: str, source: str) -> str:
    """Google News titles end in " - <Publisher>"; scoring the publisher name
    would make e.g. every "Yahoo Finance" headline hit the keyword "fine"."""
    t = (title or "").strip()
    src = (source or "").strip()
    if src and t.endswith(" - " + src):
        return t[: -(len(src) + 3)].rstrip()
    return t


def google_news_rss(query: str, *, timeout: int = 15) -> tuple[int | None, list[dict]]:
    """``(http_status, items)`` for a Google News RSS search. Items:
    ``{title, url, source, source_url, ts, date}``, publisher suffix stripped
    from the title. No key, no cookies."""
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    try:
        r = requests.get(GOOGLE_NEWS_RSS, params={"q": query, "hl": "en-US", "gl": "US",
                                                  "ceid": "US:en"},
                         headers=H, timeout=timeout)
    except Exception as e:
        print(f"  [google-news] {query!r}: {type(e).__name__}", file=sys.stderr)
        return None, []
    if r.status_code != 200:
        print(f"  [google-news] {query!r} -> {r.status_code}", file=sys.stderr)
        return r.status_code, []
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError:
        print(f"  [google-news] {query!r}: unparseable RSS", file=sys.stderr)
        return 200, []
    items: list[dict] = []
    for it in root.findall(".//item"):
        src_el = it.find("source")
        source = (src_el.text or "").strip() if src_el is not None else ""
        title = _strip_publisher_suffix(it.findtext("title") or "", source)
        link = (it.findtext("link") or "").strip()
        if not title or not link:
            continue
        ts = None
        date_str = ""
        try:
            dt = parsedate_to_datetime((it.findtext("pubDate") or "").strip())
            if dt is not None:
                dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
                ts = int(dt.timestamp())
                date_str = dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError, IndexError):
            pass
        items.append({
            "title": title[:240],
            "url": link,
            "source": source or "Google News",
            "source_url": (src_el.get("url") if src_el is not None else "") or "",
            "ts": ts,
            "date": date_str,
        })
    return 200, items


def _coin_news_query(name: str) -> str:
    """Google News query for one coin: the name in quotes, the word crypto to
    keep "Chainlink" from meaning fencing, last HEADLINE_WINDOW_DAYS days."""
    return f'"{name}" crypto when:{HEADLINE_WINDOW_DAYS}d'


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (t or "").lower())[:60]


def _headline_keywords(scored: list[tuple[dict, str]], drop: set[str],
                       top_n: int = 10) -> list[dict]:
    """Most frequent headline words (once per headline), with the mean of our
    own sentiment labels on the headlines that contain them (+1/-1/0)."""
    val = {"POSITIVE": 1, "NEGATIVE": -1, "NEUTRAL": 0}
    counts: dict[str, int] = {}
    skew: dict[str, int] = {}
    stop = _HEADLINE_STOPWORDS | {d.lower() for d in drop}
    for it, label in scored:
        for kw in set(re.findall(r"[a-z][a-z0-9-]{2,}", (it.get("title") or "").lower())):
            if kw in stop or kw.strip("-") in stop:
                continue
            counts[kw] = counts.get(kw, 0) + 1
            skew[kw] = skew.get(kw, 0) + val.get(label, 0)
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n]
    return [{"kw": kw, "count": n, "sentiment_skew": round(skew[kw] / n, 3)}
            for kw, n in ranked]


def summarize_headlines(items: list[dict], *, aliases: set[str] | None = None,
                        now: datetime | None = None,
                        window_days: int = HEADLINE_WINDOW_DAYS,
                        top_articles: int = 5) -> dict:
    """Score each headline with `_score_news_item_sentiment` (title only — RSS
    descriptions from Google News are just a link) and aggregate: counts,
    percentages, net score, the most recent headlines, a keyword cloud and a
    per-day count for the last `window_days` UTC days. Every number counts
    headlines in THIS sample; nothing is extrapolated."""
    now = now or datetime.now(timezone.utc)
    scored = [(it, _score_news_item_sentiment({"title": it.get("title")})) for it in items or []]
    pos = sum(1 for _, s in scored if s == "POSITIVE")
    neg = sum(1 for _, s in scored if s == "NEGATIVE")
    neu = len(scored) - pos - neg
    total = len(scored)
    newest = sorted(scored, key=lambda x: x[0].get("ts") or 0, reverse=True)
    today = _utc_today(now)
    buckets: dict[str, dict[str, int]] = {}
    for it, s in scored:
        ts = it.get("ts")
        if not isinstance(ts, (int, float)):
            continue
        d = datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
        b = buckets.setdefault(d, {"pos": 0, "neg": 0, "neu": 0})
        b[{"POSITIVE": "pos", "NEGATIVE": "neg"}.get(s, "neu")] += 1
    trend = []
    for i in range(window_days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        b = buckets.get(d, {"pos": 0, "neg": 0, "neu": 0})
        trend.append({"date": d, **b, "net": b["pos"] - b["neg"]})
    return {
        "article_count": total,
        "sample_capped": total >= GOOGLE_NEWS_SAMPLE_CAP,
        "positive": pos,
        "negative": neg,
        "neutral": neu,
        "positive_pct": (pos / total * 100) if total else None,
        "negative_pct": (neg / total * 100) if total else None,
        "neutral_pct": (neu / total * 100) if total else None,
        "net_score": pos - neg,
        "top_articles": [{
            "title": it.get("title"),
            "url": it.get("url"),
            "sentiment": s,
            "source": it.get("source"),
            "published_on": it.get("ts"),
            "date": it.get("date"),
        } for it, s in newest[:top_articles]],
        "top_keywords": _headline_keywords(scored, aliases or set()),
        "trend_7d": trend,
    }


def headline_sentiment(now: datetime | None = None, *, pace_s: float = 0.3) -> dict:
    """Research-tab per-coin headline sentiment for BTC/ETH/LINK/LTC (the
    "deep coverage" block in the news modal). One Google News query per coin."""
    now = now or datetime.now(timezone.utc)
    coins: dict[str, dict] = {}
    for key, _cg_id, name, _repo in COMMUNITY_DEV_COINS:
        q = _coin_news_query(name)
        _status, items = google_news_rss(q)
        if items:
            coins[key] = {"category": key.upper(), "query": q,
                          **summarize_headlines(items, aliases=_COIN_ALIASES.get(key.upper()),
                                                now=now)}
        if pace_s:
            time.sleep(pace_s)
    return {
        "available": bool(coins),
        "coins": coins,
        "method": HEADLINE_SENTIMENT_METHOD,
        "source": "Google News RSS search",
        "fetched_at": now.isoformat(timespec="seconds"),
    }


# --- Per-coin headline scoring for the Research-tab Top-25 card ---------------
#
# The RSS pipeline (`crypto_news_rss` + frontend `groupNewsBySymbol`) only
# matches ~14 of the top-25 coins because the 5 publisher feeds rarely name the
# long-tail coins. One Google News search per top-25 coin, scored server-side
# with the SAME rule the frontend applies to the publisher feeds, lifts coverage
# without shipping ~2,500 raw headlines to the browser. Headlines that are also
# in the publisher-feed corpus are dropped here, because the frontend ADDS
# these counts to its own and would otherwise count them twice.
#
# Keeping the keyword lists and the matching rule IDENTICAL across Python and
# JS is the whole point (tests/test_crypto_free_sources.py checks it): the
# merged counts must agree with what the JS would have produced on the same
# headlines.
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
    "hack", "hacked", "exploit", "exploited", "lawsuit", "sued", "sec", "fine",
    "crash", "plunge", "plunges", "dump", "dumps", "tumbles", "tumble", "sinks",
    "sink", "slide", "slides", "falls", "fall", "loses", "loss", "losses",
    "fraud", "investigation", "probe", "ban", "banned", "banning", "breach",
    "leak", "leaked", "outage", "down", "bearish", "liquidation", "liquidated",
    "rejected", "rejection", "denied", "sell-off", "selloff", "crashes",
    "crackdown", "sanction", "sanctioned", "rug", "scam", "theft", "stolen",
    "delisting", "delisted", "outflows", "outflow", "warning", "warns",
)


def _kw_pattern(words) -> re.Pattern:
    """Whole-word / whole-phrase matcher for a keyword list. Substring matching
    used to score "against" as a gain, "finance" as a fine and "path" as an
    all-time high."""
    alts = sorted({w.strip().lower() for w in words if w.strip()}, key=len, reverse=True)
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(a) for a in alts) + r")(?![a-z0-9])")


_NEWS_POS_RE = _kw_pattern(_NEWS_POS_KEYWORDS_PER_COIN)
_NEWS_NEG_RE = _kw_pattern(_NEWS_NEG_KEYWORDS_PER_COIN)

HEADLINE_SENTIMENT_METHOD = {
    "name": "Alpine Data headline keyword score",
    "computed_by": "Alpine Data, from headline text only",
    "source": ("Google News RSS search (news.google.com/rss/search), query "
               f"'\"<coin name>\" crypto when:{HEADLINE_WINDOW_DAYS}d'"),
    "rule": ("A headline is POSITIVE if it contains at least one positive keyword and no "
             "negative keyword, NEGATIVE if the reverse, otherwise NEUTRAL (both or "
             "neither). Whole words/phrases only, case-insensitive. net = positive - negative."),
    "lexicon": {"positive": list(_NEWS_POS_KEYWORDS_PER_COIN),
                "negative": list(_NEWS_NEG_KEYWORDS_PER_COIN)},
    "window": (f"last {HEADLINE_WINDOW_DAYS} days; Google News returns at most "
               f"{GOOGLE_NEWS_SAMPLE_CAP} headlines per query, so counts describe a sample, "
               "not every article published"),
    "not": "Not CoinDesk or CryptoCompare labels, not a paid API, not an LLM.",
}


def _score_news_item_sentiment(item: dict) -> str:
    """Port of the JS `scoreNewsItemSentiment` in app.py / v2/app.py. POSITIVE
    iff >= 1 positive keyword and 0 negative keywords, NEGATIVE iff the
    reverse, otherwise NEUTRAL. Whole-word matching on lower-cased title+body.
    Keep the lists and the rule in sync with the JS `_NEWS_POS_KEYWORDS` /
    `_NEWS_NEG_KEYWORDS`.
    """
    title = (item.get("title") or "") if isinstance(item, dict) else ""
    body = (item.get("body") or "") if isinstance(item, dict) else ""
    text = (str(title) + " " + str(body)).lower()
    has_pos = bool(_NEWS_POS_RE.search(text))
    has_neg = bool(_NEWS_NEG_RE.search(text))
    if has_pos and not has_neg:
        return "POSITIVE"
    if has_neg and not has_pos:
        return "NEGATIVE"
    return "NEUTRAL"


def _fetch_headline_sentiment_by_coin_impl(
    coins: list[dict],
    *,
    exclude_titles: set[str] | None = None,
    sleep_between: float = 0.3,
    now: datetime | None = None,
) -> dict:
    """One Google News search per coin, scored with `_score_news_item_sentiment`.

    `exclude_titles` holds normalized titles already in the publisher-feed
    corpus (`market.news`), which the frontend scores itself; those are dropped
    so the merged counts never count one headline twice.

    Returns `{available, coins: {SYMBOL: {symbol, name, total, positive,
    negative, neutral, net_score, recent: [...5], article_count,
    excluded_duplicates, query}}, method, source, fetched_at}`. Coins with no
    headlines are omitted so the frontend can check `if (rows[sym])`.
    """
    now = now or datetime.now(timezone.utc)
    exclude = exclude_titles or set()
    out: dict[str, dict] = {}
    for c in coins or []:
        if not isinstance(c, dict):
            continue
        sym = (c.get("symbol") or "").upper().strip()
        name = (c.get("name") or "").strip()
        if not sym or not name:
            continue
        q = _coin_news_query(name)
        try:
            _status, items = google_news_rss(q)
        except Exception as e:
            print(f"  [headline-sentiment] {sym}: {type(e).__name__}", file=sys.stderr)
            items = []
        seen: set[str] = set()
        kept: list[dict] = []
        dupes = 0
        for it in items:
            k = _norm_title(it.get("title"))
            if not k or k in seen:
                continue
            seen.add(k)
            if k in exclude:
                dupes += 1
                continue
            kept.append(it)
        if kept:
            pos = neg = neu = 0
            labelled = []
            for it in kept:
                label = _score_news_item_sentiment({"title": it.get("title")})
                pos += label == "POSITIVE"
                neg += label == "NEGATIVE"
                neu += label == "NEUTRAL"
                labelled.append((it, label))
            labelled.sort(key=lambda x: x[0].get("ts") or 0, reverse=True)
            out[sym] = {
                "symbol": sym,
                "name": name,
                "total": pos + neg + neu,
                "positive": pos,
                "negative": neg,
                "neutral": neu,
                "net_score": pos - neg,
                "recent": [{
                    "title": it.get("title") or "",
                    "url": it.get("url") or "",
                    "source": it.get("source") or "",
                    "date": it.get("date") or "",
                    "ts": it.get("ts"),
                    "sentiment": label,
                    "body": "",
                } for it, label in labelled[:5]],
                "article_count": len(kept),
                "excluded_duplicates": dupes,
                "query": q,
            }
        if sleep_between:
            time.sleep(sleep_between)
    return {
        "available": bool(out),
        "coins": out,
        "method": HEADLINE_SENTIMENT_METHOD,
        "source": "Google News RSS search",
        "fetched_at": now.isoformat(timespec="seconds"),
    }


def fetch_headline_sentiment_by_coin(markets_top: list[dict], top_n: int = 25,
                                     news: list[dict] | None = None) -> dict:
    """Stale-fallback wrapper. Slices `markets_top` to the top-N coins (list
    order is market-cap order) and scores each coin's Google News headlines.
    `news` is the publisher-feed corpus, used only to drop duplicates. On total
    failure falls back to the last successful run (tagged stale)."""
    coins = (markets_top or [])[:top_n]
    exclude = {_norm_title(n.get("title")) for n in news or [] if isinstance(n, dict)}
    exclude.discard("")
    cache_key = "fetch_headline_sentiment_by_coin"
    try:
        out = _fetch_headline_sentiment_by_coin_impl(coins, exclude_titles=exclude)
    except Exception as e:
        print(f"  [fetch_headline_sentiment_by_coin] fatal {type(e).__name__}", file=sys.stderr)
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
        "method": HEADLINE_SENTIMENT_METHOD,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _santiment_prev() -> dict | None:
    """The previous run's ``social.santiment`` node from data/market.json."""
    try:
        prev = json.loads((CACHE / "market.json").read_text())
        node = (prev.get("social") or {}).get("santiment")
        return node if isinstance(node, dict) else None
    except Exception as e:
        print(f"  [stale-keep] santiment cache unreadable: {type(e).__name__}", file=sys.stderr)
        return None


def santiment_gate(prev: dict | None, now: datetime) -> str | None:
    """``None`` = attempt a Santiment fetch now; else the stale_reason to keep the cache.

    At most ONE attempt per UTC day, made by the first run of that day.

    This used to be ``now.hour == 0``: fetch only on the 00:xx UTC run. But
    GitHub runs the "hourly" pages cron only ~4-6 times a day at drifting
    times, so on most days no run landed in hour 0 and the panel sat on a
    2-3 day old snapshot (live on 2026-10-04: fetched 2026-10-02T00:39Z).
    Keying on "already attempted today" keeps the same budget (one sweep of
    ~28 calls a day, inside the keyless 1,000 calls/month cap) without
    depending on which hours GitHub happens to schedule. A failed attempt
    also counts, so a Santiment outage cannot burn the quota run after run.
    """
    if not isinstance(prev, dict):
        return None
    last = prev.get("attempted_at")
    if last is None and prev.get("coins"):
        last = prev.get("fetched_at")  # nodes written before attempted_at existed
    if isinstance(last, str) and last[:10] == now.strftime("%Y-%m-%d"):
        return "daily_gate_attempted_today"
    return None


def santiment_metrics(now: datetime | None = None) -> dict:
    """Santiment GraphQL — free-tier metrics for 4 coins. No API key.

    Fetches at most once per UTC day (see ``santiment_gate``); other runs
    serve the previous snapshot marked ``stale``. ``fetched_at`` is when the
    served coins were fetched, ``attempted_at`` when a fetch was last tried.
    A failed attempt keeps the previous coins (stale, ``stale_reason:
    fetch_failed``) rather than blanking the panel; every series carries its
    own dates, which the cards and insights.py read as the data date.
    """
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds")
    prev = _santiment_prev()
    gate = santiment_gate(prev, now)
    if gate:
        if prev and prev.get("coins"):
            return {**prev, "stale": True, "stale_reason": gate}
        return {"available": False, "reason": gate, "coins": {},
                "attempted_at": (prev or {}).get("attempted_at"),
                "fetched_at": now_iso}
    coins = _santiment_fetch_coins(now)
    if coins:
        return {"available": True, "coins": coins,
                "fetched_at": now_iso, "attempted_at": now_iso}
    if prev and prev.get("coins"):
        return {**prev, "stale": True, "stale_reason": "fetch_failed",
                "attempted_at": now_iso}
    return {"available": False, "reason": "fetch_failed", "coins": {},
            "fetched_at": now_iso, "attempted_at": now_iso}


def _santiment_fetch_coins(now: datetime) -> dict:
    """One sweep of the Santiment free tier: ``{sym: {metric series...}}``.

    The free tier has a sliding window restriction (now-12mo to now-30d)
    for MOST metrics, but a handful work with recent data (lag=0): DAA,
    dev_activity, active_addresses_24h, dev_contributors. The rest
    (network_growth, mvrv_usd, exchange flows) need a ~35d lag query.

    Budget: 4 slugs x 6 metrics + 2 slugs x 2 BTC/ETH-only = 28 calls.
    Empty dict when nothing came back.
    """
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
    return {sym: data for sym, data in out.items()
            if any(k for k in data if k not in ("slug",))}


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


# ----- free daily close / volume / market-cap series --------------------------
#
# The top-50 POC sweep (and the server-side "look up any crypto" path) used to
# read CryptoCompare histoday. Since 2026-10 the whole CoinDesk/CryptoCompare
# API answers keyless requests with 401 and this deployment has no paid key, so
# the series now come from free sources, in this order:
#
#   1. CoinGecko /coins/{id}/market_chart?interval=daily — aggregate USD volume
#      across every venue CoinGecko tracks (the closest free match to
#      CryptoCompare's CCCAGG `volumeto`), plus market cap, which the Alpine
#      Large-Cap Crypto Index needs.
#   2. Coinbase Exchange daily candles   (<SYM>-USD)
#   3. Kraken OHLC                        (<SYM>USD)
#   4. Binance.US klines                  (<SYM>USD, then <SYM>USDT)
#
# 2-4 are keyless and are used only when CoinGecko fails for that coin. Their
# volume is ONE exchange's volume, not the market's, so the series says so
# (`volume_basis: "exchange"`) and every POC entry carries its `source`.
# (api.binance.com is not used: it answers 451 to US hosts, GitHub runners
# included.)
#
# DATES. Every series is a close series of COMPLETE UTC days, labelled with
# the day the close belongs to. CoinGecko's daily points are stamped 00:00 UTC
# and carry the close of the day that just ended — checked 2026-10-05: CG
# 2026-10-05T00:00 SOL 121.52 vs Coinbase's 2026-10-04 candle close 121.57 —
# so they are labelled with the PREVIOUS day. The trailing "now" sample and
# each exchange's still-open candle are partial days and are dropped. The
# newest bar is therefore yesterday's close, never a moving intraday read.
#
# BUDGET (CoinGecko Demo plan: 30 calls/min, ~10k calls/month). A complete
# series cannot change until the next UTC midnight, so each coin's series is
# cached in data/.stale/ (restored between CI runs) and CoinGecko is asked at
# most COINGECKO_MAX_ATTEMPTS_PER_COIN_PER_DAY times per coin per UTC day —
# one call, plus one retry if the first answer was a failure that fell back to
# an exchange. Calls are paced COINGECKO_SWEEP_PACE_S apart, capped per run,
# and a run that sees three 429s in a row stops calling CoinGecko and uses the
# exchanges. Top-50 sweep: <= 50 calls/day normally, <= 100 worst case.

CG_MARKET_CHART_URL = "https://api.coingecko.com/api/v3/coins/{id}/market_chart"
CG_SEARCH_URL = "https://api.coingecko.com/api/v3/search"
COINBASE_CANDLES_URL = "https://api.exchange.coinbase.com/products/{sym}-USD/candles"
KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"
BINANCE_US_KLINES_URL = "https://api.binance.us/api/v3/klines"

DAILY_SERIES_SOURCE_LABELS = {
    "coingecko": "CoinGecko market_chart: aggregate USD volume across the venues CoinGecko tracks",
    "coinbase": "Coinbase Exchange daily candles: Coinbase-only volume (USD volume = base volume x close)",
    "kraken": "Kraken OHLC: Kraken-only volume (USD volume = base volume x VWAP)",
    "binance_us": "Binance.US klines: Binance.US-only quote volume (USD or USDT pair)",
}
DAILY_SERIES_FALLBACKS = ("coinbase", "kraken", "binance_us")
# A series served from cache after every live source failed is only honest
# for so long. Same bound as the poc_top carry-forward below.
DAILY_SERIES_STALE_MAX_DAYS = 7
COINGECKO_SWEEP_PACE_S = 2.5        # 24 calls/min, under the Demo plan's 30
COINGECKO_SWEEP_MAX_CALLS = 60      # hard per-run cap for the sweep
COINGECKO_MAX_ATTEMPTS_PER_COIN_PER_DAY = 2
_CG_LEDGER_KEY = "coingecko_sweep_ledger"

_CG_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
_TICKER_RE = re.compile(r"^[A-Z0-9]{1,12}$")


def _utc_today(now: datetime | None = None):
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()


def _stale_read_raw(funcname: str):
    """The cached value for `funcname` exactly as saved — no stale tags, no
    log line. For caches that are FRESH by construction (a complete-day
    series fetched earlier today), where `_stale_load`'s "stale" tag would be
    a false statement."""
    try:
        p = _stale_path(funcname)
        if not p.exists():
            return None
        return json.loads(p.read_text()).get("value")
    except Exception:
        return None


class CoinGeckoBudget:
    """Per-run CoinGecko spend for a sweep: a hard call cap, a pause after each
    call (Demo plan: 30 calls/min) and a breaker that stops calling after three
    consecutive 429s, so a throttled run falls back to the keyless exchanges
    instead of hammering a limit it has already hit."""

    def __init__(self, max_calls: int = COINGECKO_SWEEP_MAX_CALLS,
                 pace_s: float = COINGECKO_SWEEP_PACE_S):
        self.max_calls = max_calls
        self.pace_s = pace_s
        self.calls = 0
        self.consecutive_429 = 0
        self.statuses: dict[str, int] = {}

    def allow(self) -> bool:
        return self.calls < self.max_calls and self.consecutive_429 < 3

    def record(self, status: int | None) -> None:
        self.calls += 1
        k = str(status)
        self.statuses[k] = self.statuses.get(k, 0) + 1
        self.consecutive_429 = self.consecutive_429 + 1 if status == 429 else 0
        if self.pace_s:
            time.sleep(self.pace_s)

    def summary(self) -> dict:
        return {"calls": self.calls, "max_calls": self.max_calls,
                "statuses": dict(self.statuses),
                "breaker_tripped": self.consecutive_429 >= 3}


def _series_from_maps(price_by: dict, vol_by: dict, cap_by: dict | None = None) -> dict:
    dates = sorted(d for d, v in price_by.items() if isinstance(v, (int, float)) and v > 0)
    return {
        "price": [{"date": d, "value": price_by[d]} for d in dates],
        "volume": [{"date": d, "value": vol_by[d]} for d in dates
                   if isinstance(vol_by.get(d), (int, float))],
        "market_cap": [{"date": d, "value": cap_by[d]} for d in dates
                       if cap_by and isinstance(cap_by.get(d), (int, float)) and cap_by[d] > 0],
    }


def _trim_series(s: dict, days: int) -> dict:
    keep = set(sorted({p["date"] for p in s.get("price") or []})[-days:])
    return {k: [p for p in (s.get(k) or []) if p.get("date") in keep]
            for k in ("price", "volume", "market_cap")}


def _cg_daily_points(points, today) -> dict:
    """``[[ms, value], ...]`` from market_chart -> {close-day ISO: value}.

    Only 00:00:00 UTC samples are kept and each is labelled with the day it
    closes (the previous UTC day). Anything else is an intraday sample (the
    trailing "now" point) and is a partial day, so it is dropped."""
    out: dict[str, float] = {}
    for p in points or []:
        if not (isinstance(p, (list, tuple)) and len(p) >= 2):
            continue
        try:
            dt = datetime.fromtimestamp(float(p[0]) / 1000, tz=timezone.utc)
            val = float(p[1])
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        if (dt.hour, dt.minute, dt.second) != (0, 0, 0):
            continue
        day = (dt - timedelta(days=1)).date()
        if day >= today:
            continue
        out[day.isoformat()] = val
    return out


def coingecko_daily_series(coin_id: str, days: int = 180,
                           today=None) -> tuple[int | None, dict | None]:
    """``(http_status, series_or_None)`` from CoinGecko market_chart.

    Series: ``{price, volume, market_cap}`` lists of ``{date, value}``, complete
    UTC days only. The Demo key rides along via `_headers_for` (CoinGecko
    hosts only)."""
    if not isinstance(coin_id, str) or not _CG_ID_RE.match(coin_id):
        return None, None
    today = today or _utc_today()
    status, j = _get_status(
        CG_MARKET_CHART_URL.format(id=coin_id),
        {"vs_currency": "usd", "days": str(int(days)), "interval": "daily"},
        timeout=20,
    )
    if status != 200 or not isinstance(j, dict):
        return status, None
    price = _cg_daily_points(j.get("prices"), today)
    if not price:
        return status, None
    s = _series_from_maps(price, _cg_daily_points(j.get("total_volumes"), today),
                          _cg_daily_points(j.get("market_caps"), today))
    return status, _trim_series(s, days)


def coinbase_daily_series(symbol: str, days: int = 180, today=None) -> dict | None:
    """Coinbase Exchange daily candles ``[time, low, high, open, close, volume]``.
    Volume is in the base asset, so USD volume = volume x close (an
    approximation of the day's quote volume)."""
    sym = (symbol or "").upper()
    if not _TICKER_RE.match(sym):
        return None
    today = today or _utc_today()
    status, j = _get_status(COINBASE_CANDLES_URL.format(sym=sym),
                            {"granularity": "86400"}, timeout=15)
    if status != 200 or not isinstance(j, list):
        return None
    price, vol = {}, {}
    for r in j:
        try:
            day = datetime.fromtimestamp(int(r[0]), tz=timezone.utc).date()
            close, base_vol = float(r[4]), float(r[5])
        except (TypeError, ValueError, IndexError, OverflowError, OSError):
            continue
        if day >= today or close <= 0:
            continue
        price[day.isoformat()] = close
        vol[day.isoformat()] = base_vol * close
    return _trim_series(_series_from_maps(price, vol), days) if price else None


def kraken_daily_series(symbol: str, days: int = 180, today=None) -> dict | None:
    """Kraken OHLC ``[time, open, high, low, close, vwap, volume, count]``.
    USD volume = base volume x VWAP (close when VWAP is 0, i.e. no trades)."""
    sym = (symbol or "").upper()
    if not _TICKER_RE.match(sym):
        return None
    today = today or _utc_today()
    since = int(datetime(today.year, today.month, today.day, tzinfo=timezone.utc).timestamp()
                - (days + 2) * 86400)
    status, j = _get_status(KRAKEN_OHLC_URL, {"pair": f"{sym}USD", "interval": "1440",
                                              "since": str(since)}, timeout=15)
    if status != 200 or not isinstance(j, dict) or j.get("error"):
        return None
    result = j.get("result") or {}
    rows = next((v for k, v in result.items() if k != "last" and isinstance(v, list)), None)
    if not rows:
        return None
    price, vol = {}, {}
    for r in rows:
        try:
            day = datetime.fromtimestamp(int(r[0]), tz=timezone.utc).date()
            close, vwap, base_vol = float(r[4]), float(r[5]), float(r[6])
        except (TypeError, ValueError, IndexError, OverflowError, OSError):
            continue
        if day >= today or close <= 0:
            continue
        price[day.isoformat()] = close
        vol[day.isoformat()] = base_vol * (vwap if vwap > 0 else close)
    return _trim_series(_series_from_maps(price, vol), days) if price else None


def binance_us_daily_series(symbol: str, days: int = 180, today=None) -> dict | None:
    """Binance.US klines; index 7 is the quote-asset volume (USD or USDT)."""
    sym = (symbol or "").upper()
    if not _TICKER_RE.match(sym):
        return None
    today = today or _utc_today()
    rows = None
    for quote in ("USD", "USDT"):
        status, j = _get_status(BINANCE_US_KLINES_URL,
                                {"symbol": f"{sym}{quote}", "interval": "1d",
                                 "limit": str(min(int(days) + 1, 1000))}, timeout=15)
        if status == 200 and isinstance(j, list) and j:
            rows = j
            break
    if not rows:
        return None
    price, vol = {}, {}
    for r in rows:
        try:
            day = datetime.fromtimestamp(int(r[0]) / 1000, tz=timezone.utc).date()
            close, quote_vol = float(r[4]), float(r[7])
        except (TypeError, ValueError, IndexError, OverflowError, OSError):
            continue
        if day >= today or close <= 0:
            continue
        price[day.isoformat()] = close
        vol[day.isoformat()] = quote_vol
    return _trim_series(_series_from_maps(price, vol), days) if price else None


_DAILY_FALLBACK_FETCHERS = {
    "coinbase": lambda sym, days, today: coinbase_daily_series(sym, days, today),
    "kraken": lambda sym, days, today: kraken_daily_series(sym, days, today),
    "binance_us": lambda sym, days, today: binance_us_daily_series(sym, days, today),
}


def _series_last_common_date(s: dict | None) -> str | None:
    if not isinstance(s, dict):
        return None
    pd = {p.get("date") for p in s.get("price") or [] if p.get("date")}
    vd = {p.get("date") for p in s.get("volume") or [] if p.get("date")}
    common = pd & vd if vd else pd
    return max(common) if common else None


def crypto_daily_series(coin_id: str | None, symbol: str, days: int = 180, *,
                        today=None, budget: CoinGeckoBudget | None = None,
                        allow_coingecko: bool = True) -> dict:
    """Complete-day close/volume(/market cap) series for one coin, from the
    first free source that answers (CoinGecko, then Coinbase, Kraken,
    Binance.US). Never raises.

    Returns ``{price, volume, market_cap, source, volume_basis, as_of,
    coingecko_calls, attempts[, cache][, stale]}``. ``market_cap`` is only
    filled from CoinGecko. ``cache: "today"`` means the series was fetched
    earlier this UTC day and is still complete (nothing newer exists until the
    next UTC midnight). ``stale: True`` means every live source failed and an
    older cached series (at most DAILY_SERIES_STALE_MAX_DAYS old) is served;
    its ``as_of`` stays the day it was observed.
    """
    sym = (symbol or "").upper()
    today = today or _utc_today()
    yesterday = (today - timedelta(days=1)).isoformat()
    budget = budget or CoinGeckoBudget(max_calls=1, pace_s=0)
    key = f"daily_series_{coin_id or sym}"
    cached = _stale_read_raw(key)
    cached = cached if isinstance(cached, dict) and cached.get("price") else None
    cached_fresh = bool(cached and (cached.get("as_of") or "") >= yesterday)

    if cached_fresh and cached.get("source") == "coingecko":
        return {**cached, "cache": "today", "coingecko_calls": 0, "attempts": []}

    attempts: list[str] = []
    calls = 0
    series = None
    if coin_id and allow_coingecko and budget.allow():
        status, s = coingecko_daily_series(coin_id, days, today)
        budget.record(status)
        calls = 1
        attempts.append(f"coingecko:{status}")
        if s:
            series = {**s, "source": "coingecko", "volume_basis": "aggregate"}
    elif coin_id:
        attempts.append("coingecko:skipped (budget)")

    if series is None and cached_fresh:
        # A fallback series fetched earlier today is complete; CoinGecko just
        # failed again (or was not asked), so keep it rather than re-pulling
        # the exchanges for the same days.
        return {**cached, "cache": "today", "coingecko_calls": calls,
                "attempts": attempts}

    if series is None:
        for name in DAILY_SERIES_FALLBACKS:
            try:
                s = _DAILY_FALLBACK_FETCHERS[name](sym, days, today)
            except Exception as e:  # one bad venue must not end the chain
                print(f"  [daily-series] {sym} {name}: {type(e).__name__}", file=sys.stderr)
                s = None
            attempts.append(f"{name}:{'ok' if s else 'none'}")
            if s:
                series = {**s, "market_cap": [], "source": name, "volume_basis": "exchange"}
                break

    if series is not None:
        series["as_of"] = _series_last_common_date(series)
        series["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _stale_save(key, series)
        return {**series, "coingecko_calls": calls, "attempts": attempts}

    if cached and cached.get("as_of"):
        try:
            age = (today - datetime.strptime(cached["as_of"], "%Y-%m-%d").date()).days
        except ValueError:
            age = None
        if age is not None and age <= DAILY_SERIES_STALE_MAX_DAYS:
            print(f"  [daily-series] {sym}: every source failed ({', '.join(attempts)}); "
                  f"serving cache observed {cached['as_of']}", file=sys.stderr)
            return {**cached, "stale": True, "coingecko_calls": calls, "attempts": attempts}
    print(f"  [daily-series] {sym}: no source answered ({', '.join(attempts)})", file=sys.stderr)
    return {"price": [], "volume": [], "market_cap": [], "source": None,
            "volume_basis": None, "as_of": None, "coingecko_calls": calls,
            "attempts": attempts}


def fetch_top_daily_series(top_markets: list[dict], n: int = 50, days: int = 180, *,
                           today=None, budget: CoinGeckoBudget | None = None) -> dict:
    """Daily series for the top `n` coins, with the run's source/budget record.

    Returns ``{"series": {coin_id: series}, "meta": {...}}``. ``meta`` is what
    ships as ``market.poc_top_meta``: per-source counts, how many came from
    today's cache, which are stale or missing, and the CoinGecko calls this
    run actually made.
    """
    today = today or _utc_today()
    budget = budget or CoinGeckoBudget()
    ledger = _stale_read_raw(_CG_LEDGER_KEY)
    if not isinstance(ledger, dict) or ledger.get("date") != today.isoformat():
        ledger = {"date": today.isoformat(), "attempts": {}}
    attempts = ledger.setdefault("attempts", {})
    series: dict[str, dict] = {}
    by_source: dict[str, int] = {}
    from_cache = 0
    stale: list[str] = []
    missing: list[str] = []
    for c in (top_markets or [])[:n]:
        coin_id = c.get("id") if isinstance(c, dict) else None
        symbol = ((c.get("symbol") if isinstance(c, dict) else "") or "").upper()
        if not coin_id or not symbol:
            continue
        allow = int(attempts.get(coin_id, 0)) < COINGECKO_MAX_ATTEMPTS_PER_COIN_PER_DAY
        s = crypto_daily_series(coin_id, symbol, days, today=today, budget=budget,
                                allow_coingecko=allow)
        if s.get("coingecko_calls"):
            attempts[coin_id] = int(attempts.get(coin_id, 0)) + int(s["coingecko_calls"])
        series[coin_id] = s
        src = s.get("source") or "none"
        by_source[src] = by_source.get(src, 0) + 1
        if s.get("cache") == "today":
            from_cache += 1
        if s.get("stale"):
            stale.append(symbol)
        if not s.get("price"):
            missing.append(symbol)
    _stale_save(_CG_LEDGER_KEY, ledger)
    meta = {
        "requested": min(n, len(top_markets or [])),
        "by_source": by_source,
        "served_from_today_cache": from_cache,
        "stale": stale,
        "missing": missing,
        "coingecko_this_run": budget.summary(),
        "coingecko_calls_today": sum(int(v) for v in attempts.values()),
        "coingecko_max_per_coin_per_day": COINGECKO_MAX_ATTEMPTS_PER_COIN_PER_DAY,
        "date_basis": ("complete UTC days only; the newest bar is the previous "
                       "UTC day's close"),
        "source_labels": DAILY_SERIES_SOURCE_LABELS,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return {"series": series, "meta": meta}


def resolve_coingecko_id(symbol: str, markets_top: list[dict] | None = None) -> tuple[str | None, str | None]:
    """``(coin_id, name)`` for a ticker: the cached top-N list first, then
    CoinGecko /search (one call). Ties go to the best market-cap rank, so
    "BTC" is Bitcoin and not a token that borrowed its ticker."""
    sym = (symbol or "").upper()
    if not _TICKER_RE.match(sym):
        return None, None
    for c in markets_top or []:
        if isinstance(c, dict) and str(c.get("symbol") or "").upper() == sym and c.get("id"):
            return c["id"], c.get("name")
    status, j = _get_status(CG_SEARCH_URL, {"query": sym}, timeout=15)
    if status != 200 or not isinstance(j, dict):
        return None, None
    hits = [c for c in (j.get("coins") or [])
            if isinstance(c, dict) and str(c.get("symbol") or "").upper() == sym
            and isinstance(c.get("id"), str) and _CG_ID_RE.match(c["id"])]
    if not hits:
        return None, None
    hits.sort(key=lambda c: (c.get("market_cap_rank") is None, c.get("market_cap_rank") or 0))
    return hits[0]["id"], hits[0].get("name")


def crypto_daily_market_by_symbol(symbol: str, days: int = 180,
                                  markets_top: list[dict] | None = None) -> dict:
    """Server-side "look up any crypto": resolve the ticker to a CoinGecko id
    (cached list first) and return its daily series, falling back to the
    exchanges by ticker when CoinGecko has nothing. Adds ``coin_id`` and
    ``name`` when resolved."""
    coin_id, name = resolve_coingecko_id(symbol, markets_top)
    s = crypto_daily_series(coin_id, symbol, days)
    return {**s, "coin_id": coin_id, "name": name}


def poc_entry_as_of(entry: dict | None) -> str | None:
    """Observation date (``YYYY-MM-DD``) of one ``poc_top`` entry.

    Prefers the explicit ``as_of`` written by `compute_poc_top_markets`;
    falls back to the last ``signal_history`` date so entries written by an
    older build (restored from the Actions cache, which is never committed)
    still report a real age instead of ``None``.

    Deliberately has no clock in it. A carried-forward entry keeps whatever
    date it was originally observed on, so `stale: true` rows age visibly
    rather than inheriting the freshness of the coins around them.
    """
    def _iso(v) -> str | None:
        if not isinstance(v, str) or len(v) < 10:
            return None
        try:
            datetime.strptime(v[:10], "%Y-%m-%d")
        except ValueError:
            return None
        return v[:10]

    if not isinstance(entry, dict):
        return None
    hist = entry.get("signal_history")
    last = hist[-1] if isinstance(hist, list) and hist else None
    return _iso(entry.get("as_of")) or _iso(
        last.get("date") if isinstance(last, dict) else None)


def compute_poc_top_markets(top_markets: list[dict], n: int = 25,
                             days: int = 180,
                             series_by_id: dict[str, dict] | None = None) -> list[dict]:
    """Multi-timeframe POC + migration + naked POCs + rolling signal score for
    the top `n` coins by market cap. Feeds the "Top 50 POC" table and the
    top-50 signal-breadth chart (and, via data/composites, the
    poc_signal_breadth history).

    Price/volume come from `crypto_daily_series` (CoinGecko market_chart, then
    Coinbase -> Kraken -> Binance.US), normally pre-fetched by
    `fetch_top_daily_series` and passed in as `series_by_id` so the Alpine
    index can reuse the same pulls. When `series_by_id` is None it is fetched
    here. This used to call CryptoCompare histoday, and before that this
    comment claimed Binance klines, which it never was — anyone budgeting
    quota from the comment was budgeting the wrong API.

    Each entry carries ``source`` (which API the series came from) and
    ``volume_basis`` ("aggregate" for CoinGecko's cross-venue volume,
    "exchange" for a single exchange's volume), because a POC built from one
    venue's volume is a different measurement from one built from the market's.

    Returns up to `n` entries with the schema the dashboard expects:
        {coin_id, symbol, name, image, current_price, as_of, source,
         volume_basis, poc: {d30, d90, d180, migration, naked,
         migration_series}, signal_history[, stale]}
    """
    if not top_markets:
        return []
    out: list[dict] = []
    coins = top_markets[:n]
    if series_by_id is None:
        series_by_id = fetch_top_daily_series(coins, n=n, days=days)["series"]
    LOOKBACKS = (("d30", 30, 60), ("d90", 90, 80), ("d180", 180, 100))
    # Build a stale-keep map from the previous market.json so any coin we fail
    # to fetch this run keeps its last good POC entry.
    #
    # BOUNDED, deliberately. This used to carry an entry forward forever, and
    # because market.json is never committed — it is restored from the Actions
    # cache on every run — a coin that stopped fetching was re-served from that
    # cache indefinitely. The top-50 signal-breadth chart sat frozen at
    # 2026-06-09 for eight weeks looking completely current, because the UI
    # renders signal_history and never checks the `stale` flag set below.
    #
    # Riding out a transient blip is the point of stale-keep; pretending a
    # two-month outage is live data is not. Past STALE_KEEP_MAX_DAYS we drop
    # the coin so it disappears from the chart — a visible gap beats a
    # confident lie, and the chart's own "last 90 days" window then shrinks
    # honestly instead of flat-lining.
    STALE_KEEP_MAX_DAYS = 7

    def _entry_age_days(entry: dict) -> float | None:
        iso = poc_entry_as_of(entry)
        if not iso:
            return None
        try:
            d = datetime.strptime(iso, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        return (datetime.now(timezone.utc) - d).total_seconds() / 86400.0

    stale_map: dict[str, dict] = {}
    try:
        prev = json.loads((CACHE / "market.json").read_text())
        expired = 0
        for e in (prev.get("poc_top") or []):
            cid = e.get("coin_id")
            if not cid:
                continue
            age = _entry_age_days(e)
            if age is not None and age > STALE_KEEP_MAX_DAYS:
                expired += 1
                continue
            stale_map[cid] = e
        if expired:
            print(f"  [stale-keep] dropped {expired} poc_top entr"
                  f"{'y' if expired == 1 else 'ies'} older than "
                  f"{STALE_KEEP_MAX_DAYS}d — refusing to re-serve them as live",
                  file=sys.stderr)
    except Exception as e:
        print(f"  [stale-keep] poc_top stale map suppressed: {type(e).__name__}", file=sys.stderr)

    def _carry_forward(coin_id: str) -> dict:
        """Copy the previous entry forward, flagged and dated honestly.

        `as_of` is pinned to the date the carried-forward data was
        ORIGINALLY observed (backfilled from signal_history for entries
        written before as_of existed). It must never advance here — a
        re-served entry that inherits today's date is precisely the lie
        that let the breadth chart sit frozen at 2026-06-09 behind a
        fresh-looking page.
        """
        stale = dict(stale_map[coin_id])
        stale["stale"] = True
        stale["as_of"] = poc_entry_as_of(stale)
        return stale

    for c in coins:
        coin_id = c.get("id")
        symbol = (c.get("symbol") or "").upper()
        if not coin_id or not symbol:
            continue
        m = series_by_id.get(coin_id) or {}
        prices = m.get("price") or []
        volumes = m.get("volume") or []
        if not prices or not volumes:
            if coin_id in stale_map:
                out.append(_carry_forward(coin_id))
            continue
        tfs = {k: point_of_control(prices, volumes, lookback_days=lb, bins=b)
               for k, lb, b in LOOKBACKS}
        if not any(tfs.values()):
            if coin_id in stale_map:
                out.append(_carry_forward(coin_id))
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
            "symbol":        symbol,
            "name":          c.get("name"),
            "image":         c.get("image"),
            "current_price": c.get("price_usd"),
            # Last date present in BOTH the price and volume series — the
            # newest complete daily bar the POC/score were computed from,
            # labelled by the UTC day it closes. Not a clock reading.
            "as_of":         common[-1] if common else None,
            "source":        m.get("source"),
            "volume_basis":  m.get("volume_basis"),
            "poc": {
                **tfs,
                "migration":        compute_poc_migration(tfs.get("d30"), tfs.get("d90")),
                "naked":            naked_pocs(prices, volumes, lookback_days=180),
                "migration_series": poc_migration_series(prices, volumes),
            },
            "signal_history": signal_history,
        }
        # `crypto_daily_series` serves a bounded (<= 7 day) cached series when
        # every live source failed, and tags it. Surface that here too, so the
        # UI's "N of M served from cache" count covers BOTH stale paths and not
        # just the copy-forward one above. The `as_of` above is already the
        # cached series' own last date, so it stays honest either way.
        if m.get("stale"):
            entry["stale"] = True
        out.append(entry)
    return out


# ----- Alpine Large-Cap Crypto Index (replaces the CoinDesk CADLI chart) ------
#
# The Futures tab used to chart CoinDesk's CADLI BTC reference price. CADLI is
# CoinDesk's proprietary index and its API needs a paid key since 2026-10, so
# it cannot be shown, and it cannot be reproduced either. In its place this is
# Alpine Data's OWN index, built only from CoinGecko daily closes and market
# caps that the top-50 POC sweep already pulls (zero extra API calls). It is
# never labelled CADLI and never presented as CoinDesk's.

ALPINE_INDEX_NAME = "Alpine Large-Cap Crypto Index"
ALPINE_INDEX_CODE = "ALCI-10"
ALPINE_INDEX_CONSTITUENTS = 10
ALPINE_INDEX_WINDOW_DAYS = 90
ALPINE_INDEX_BASE_LEVEL = 100.0
ALPINE_PEG_BAND = 0.03
ALPINE_STABLECOIN_IDS = frozenset({
    "tether", "usd-coin", "dai", "usds", "ethena-usde", "usd1-wlfi",
    "first-digital-usd", "paypal-usd", "true-usd", "frax", "ripple-usd",
    "global-dollar", "falcon-finance", "usual-usd", "binance-usd", "gemini-dollar",
    "paxos-standard", "liquity-usd", "crvusd", "gho", "susds", "ethena-staked-usde",
    "eurc", "stasis-eurs", "agora-dollar", "sky-dollar", "usdd",
})
ALPINE_NON_CRYPTO_IDS = frozenset({
    # tokenized real-world assets: gold, loans, money-market / T-bill funds
    "tether-gold", "pax-gold", "figure-heloc", "hashnote-usyc",
    "ondo-us-dollar-yield", "blackrock-usd-institutional-digital-liquidity-fund",
    "franklin-onchain-u-s-government-money-fund", "ondo-short-term-us-government-bond-fund",
})
ALPINE_DERIVATIVE_IDS = frozenset({
    # wrapped / staked / bridged versions of another coin (double counting)
    "wrapped-bitcoin", "staked-ether", "wrapped-steth", "weth", "coinbase-wrapped-btc",
    "wrapped-eeth", "ether-fi-staked-eth", "rocket-pool-eth", "mantle-staked-ether",
    "wrapped-beacon-eth", "lombard-staked-btc", "solv-btc", "jito-staked-sol",
    "binance-staked-sol", "kelp-dao-restaked-eth", "renzo-restaked-eth",
    "binance-peg-weth", "wbnb", "wrapped-solana", "bitcoin-avalanche-bridged-btc-b",
    "binance-bitcoin", "cbeth", "coinbase-wrapped-staked-eth", "msol", "jupiter-staked-sol",
})
_ALPINE_DERIVATIVE_NAME_RE = re.compile(r"\b(wrapped|bridged|staked|restaked)\b", re.I)
ALPINE_INDEX_METHOD = {
    "summary": ("Market-cap-weighted price index of the 10 largest eligible coins, "
                "rebased to 100 at the first day of the 90-day window shown."),
    "universe": ("The 50 largest coins by market cap on CoinGecko at fetch time "
                 "(the list the POC table uses)."),
    "eligibility": ("Excludes stablecoins (a named list, plus any coin whose every close "
                    "in the window is within ±3% of $1), tokenized real-world assets "
                    "(gold, loans, money-market funds) and wrapped, staked or bridged "
                    "versions of other coins. A coin also needs CoinGecko market-cap "
                    "history in this run; one served by an exchange fallback is left "
                    "out and listed under excluded."),
    "selection": ("The 10 largest eligible coins by CoinGecko market cap at the base "
                  "date, re-selected at the first daily close of each calendar month. "
                  "Every change is listed under constituent_changes."),
    "weighting": ("Each day's index return is the average of the constituents' price "
                  "returns weighted by their previous-day CoinGecko market caps. No "
                  "weight cap, so Bitcoin dominates."),
    "data": ("CoinGecko /coins/{id}/market_chart daily closes and market caps (00:00 UTC "
             "samples, labelled with the UTC day they close). Complete days only. A day "
             "on which any constituent has no price is not computed and is listed under "
             "gaps; the next computed day chains from the last computed one. Nothing is "
             "interpolated or filled."),
    "caveats": ("Survivorship: only coins in today's top 50 can be constituents at any "
                "point in the window, so a coin that was large earlier and has since "
                "dropped out is missing. Market caps are CoinGecko's estimates. This is "
                "Alpine Data's own index, not CoinDesk's CADLI or any licensed benchmark."),
}


def _alpine_exclusion(coin: dict, s: dict | None, window: set[str]) -> str | None:
    """Why `coin` cannot be an index constituent this run, or None."""
    cid = str(coin.get("id") or "")
    name = str(coin.get("name") or "")
    if cid in ALPINE_STABLECOIN_IDS:
        return "stablecoin"
    if cid in ALPINE_NON_CRYPTO_IDS:
        return "tokenized real-world asset (gold, loans, funds)"
    if cid in ALPINE_DERIVATIVE_IDS or _ALPINE_DERIVATIVE_NAME_RE.search(name):
        return "wrapped, staked or bridged version of another coin"
    if not isinstance(s, dict) or s.get("source") != "coingecko" or not s.get("market_cap"):
        return "no CoinGecko market-cap history in this run"
    closes = [p.get("value") for p in s.get("price") or []
              if p.get("date") in window and isinstance(p.get("value"), (int, float))]
    if closes and all(abs(v - 1.0) <= ALPINE_PEG_BAND for v in closes):
        return "stablecoin (every close in the window within ±3% of $1)"
    return None


def compute_alpine_index(top_markets: list[dict], series_by_id: dict[str, dict], *,
                         n: int = ALPINE_INDEX_CONSTITUENTS,
                         window_days: int = ALPINE_INDEX_WINDOW_DAYS,
                         now: datetime | None = None) -> dict:
    """Alpine Large-Cap Crypto Index over the last `window_days` complete days.

    See ALPINE_INDEX_METHOD for the rules; every one of them is applied here
    and nothing else is. Returns ``{available, name, code, series: [{date,
    value}], base_date, base_level, as_of, constituents, initial_constituents,
    constituent_changes, gaps, excluded, method, ...}``; ``available: False``
    with a ``reason`` when there is not enough CoinGecko history to compute it.
    Pure: no network.
    """
    now = now or datetime.now(timezone.utc)
    today = _utc_today(now)
    base = {
        "name": ALPINE_INDEX_NAME,
        "code": ALPINE_INDEX_CODE,
        "computed_by": "Alpine Data",
        "replaces": ("CoinDesk CADLI BTC reference chart. CADLI is CoinDesk's proprietary "
                     "index and its API needs a paid CoinDesk key since 2026-10; this is a "
                     "different, independently computed index."),
        "source": "CoinGecko /coins/{id}/market_chart (daily close price and market cap)",
        "method": ALPINE_INDEX_METHOD,
        "constituent_count": n,
        "window_days": window_days,
        "computed_at": now.isoformat(timespec="seconds"),
    }
    end = today - timedelta(days=1)
    window = [(end - timedelta(days=i)).isoformat() for i in range(window_days - 1, -1, -1)]
    wset = set(window)
    price: dict[str, dict[str, float]] = {}
    cap: dict[str, dict[str, float]] = {}
    label: dict[str, tuple[str, str]] = {}
    excluded: dict[str, list[str]] = {}
    for c in top_markets or []:
        if not isinstance(c, dict) or not c.get("id"):
            continue
        cid = c["id"]
        sym = str(c.get("symbol") or "").upper()
        s = (series_by_id or {}).get(cid)
        why = _alpine_exclusion(c, s, wset)
        if why:
            excluded.setdefault(why, []).append(sym or cid)
            continue
        price[cid] = {p["date"]: float(p["value"]) for p in s.get("price") or []
                      if p.get("date") in wset and isinstance(p.get("value"), (int, float))
                      and p["value"] > 0}
        cap[cid] = {p["date"]: float(p["value"]) for p in s.get("market_cap") or []
                    if p.get("date") in wset and isinstance(p.get("value"), (int, float))
                    and p["value"] > 0}
        label[cid] = (sym or cid, str(c.get("name") or cid))
    base["excluded"] = excluded
    base["eligible_count"] = len(price)

    def _select(d: str) -> list[str] | None:
        ranked = sorted((cid for cid in price if cap[cid].get(d) and price[cid].get(d)),
                        key=lambda cid: -cap[cid][d])
        return ranked[:n] if len(ranked) >= n else None

    def _syms(ids) -> list[str]:
        return [label[i][0] for i in ids]

    if len(price) < n:
        return {**base, "available": False, "series": [],
                "reason": (f"Only {len(price)} eligible coins had CoinGecko market-cap history "
                           f"in this run; the index needs {n}. Not computed rather than "
                           f"computed from fewer constituents.")}
    base_date = next((d for d in window if _select(d)), None)
    if base_date is None:
        return {**base, "available": False, "series": [],
                "reason": (f"No day in the {window_days}-day window had prices and market caps "
                           f"for {n} eligible coins.")}
    members = _select(base_date)
    initial = list(members)
    sel_month = base_date[:7]
    level = ALPINE_INDEX_BASE_LEVEL
    series = [{"date": base_date, "value": round(level, 4)}]
    prev = base_date
    changes: list[dict] = []
    gaps: list[dict] = []
    for d in window[window.index(base_date) + 1:]:
        missing = [label[c][0] for c in members if not price[c].get(d)]
        if missing or not all(price[c].get(prev) and cap[c].get(prev) for c in members):
            gaps.append({"date": d, "missing": missing or ["previous-day market cap"]})
            continue
        tot = sum(cap[c][prev] for c in members)
        level *= sum(cap[c][prev] / tot * (price[c][d] / price[c][prev]) for c in members)
        series.append({"date": d, "value": round(level, 4)})
        prev = d
        if d[:7] != sel_month:
            new = _select(d)
            if new:
                if set(new) != set(members):
                    changes.append({"date": d,
                                    "added": _syms(c for c in new if c not in members),
                                    "removed": _syms(c for c in members if c not in new)})
                members = new
                sel_month = d[:7]
    tot = sum(cap[c][prev] for c in members if cap[c].get(prev)) or 0.0
    constituents = sorted((
        {"coin_id": c, "symbol": label[c][0], "name": label[c][1],
         "market_cap_usd": cap[c].get(prev),
         "weight_pct": round(cap[c][prev] / tot * 100, 2) if tot and cap[c].get(prev) else None}
        for c in members), key=lambda r: -(r["market_cap_usd"] or 0))
    return {
        **base,
        "available": len(series) >= 2,
        **({} if len(series) >= 2 else {
            "reason": "Fewer than two computable days in the window."}),
        "series": series,
        "base_date": base_date,
        "base_level": ALPINE_INDEX_BASE_LEVEL,
        "as_of": prev,
        "change_pct": round(level / ALPINE_INDEX_BASE_LEVEL * 100 - 100, 2),
        "constituents": constituents,
        "initial_constituents": _syms(initial),
        "constituent_changes": changes,
        "gaps": gaps,
    }


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


# --- whale-sentiment provenance ---------------------------------------------
# The two whale composites below used to stamp themselves with
# ``whale["fetched_at"][:10]`` — the wall clock at fetch time. That advances
# on every run whether or not a single on-chain number moved, and the whale
# tree is stale-kept in pieces (bitinfocharts falls back to the previous
# distribution, glassnode/etherscan fall back to data/.stale/*.json), so a
# completely failed refresh still came out wearing today's date.
#
# The underlying proxy series all carry REAL observation dates
# (blockchain.info charts, bitinfocharts cohort rows, Coin Metrics, the
# synthesized Etherscan blocks/day series). Those dates are frozen by
# construction: carry a payload forward and its dates come with it.
#
# So: each contributing component reports the observation date of the series
# it was computed from, and the composite stamps the OLDEST of them — a
# composite is only as fresh as its stalest input. The fetch clock survives
# under the unambiguous name ``fetched_at`` and is never the headline stamp.

# Marker field: only the fixed shape emits it. Consumers (v2 whaleFreshness,
# scripts/snapshot_composites.py) gate on its presence before trusting
# ``as_of``, because a cached sidecar from an older build carries the
# poisoned value in a field that looks identical.
WHALE_AS_OF_BASIS = "oldest contributing on-chain series"


def _obs_date_of_series(series) -> str | None:
    """Newest date in a ``[{date, value}, ...]`` series that carries a value.

    That is the last day the upstream actually published a number — the
    honest observation date of the series. Scans backwards so a trailing
    null-valued or undated point cannot blank the answer. ``None`` when the
    series has no usable date at all (never a substituted clock read).
    """
    if not isinstance(series, list):
        return None
    for row in reversed(series):
        if not isinstance(row, dict) or row.get("value") is None:
            continue
        d = row.get("date")
        if isinstance(d, str) and len(d) >= 10:
            return d[:10]
    return None


def _obs_date_of_row(row) -> str | None:
    """Observation date of a single dated row (e.g. a bitinfocharts cohort)."""
    if not isinstance(row, dict):
        return None
    d = row.get("date")
    return d[:10] if isinstance(d, str) and len(d) >= 10 else None


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
    obs_dates: list[str] = []
    undated = 0

    def add(name: str, value: str, c: int, explanation: str,
            obs_date: str | None = None):
        nonlocal undated
        comps.append({"name": name, "value": value,
                      "contribution": int(c), "explanation": explanation,
                      "as_of": obs_date})
        if obs_date:
            obs_dates.append(obs_date)
        else:
            undated += 1

    # 1) Whale supply 30d Δ — ±20 saturates at ±1%
    sup_now = _whale_supply(dist[-1])
    sup_30 = _whale_supply(dist[-31])
    if sup_30:
        sup_delta = (sup_now - sup_30) / sup_30 * 100
        c = _clamp(sup_delta / 1.0 * 20, -20, 20)
        add("Whale supply Δ30d", f"{sup_delta:+.2f}%", c,
            "whales accumulating" if c > 0 else "whales distributing" if c < 0 else "flat",
            _obs_date_of_row(dist[-1]))

    # 2) Hash rate vs 30d mean — ±20 saturates at ±10%
    hr = _pct_vs_mean30("hash_rate")
    if hr is not None:
        c = _clamp(hr / 10 * 20, -20, 20)
        add("Hash rate vs 30d", f"{hr:+.1f}%", c,
            "miner confidence rising" if c > 0 else "miners capitulating" if c < 0 else "flat",
            _obs_date_of_series(btc.get("hash_rate")))

    # 3) Miner revenue vs 30d mean — ±15 saturates at ±15%
    mr = _pct_vs_mean30("miners_revenue_usd")
    if mr is not None:
        c = _clamp(mr / 15 * 15, -15, 15)
        add("Miner revenue vs 30d", f"{mr:+.1f}%", c,
            "miners under pressure" if c < 0 else "miner income healthy" if c > 0 else "flat",
            _obs_date_of_series(btc.get("miners_revenue_usd")))

    # 4) Avg tx USD z-score(30d) — ±15 saturates at ±2σ
    az = _z30("avg_tx_usd")
    if az is not None:
        c = _clamp(az / 2 * 15, -15, 15)
        add("Avg tx USD z30", f"{az:.2f}σ", c,
            "larger-ticket flow (whale-shaped)" if c > 0 else "smaller-ticket flow",
            _obs_date_of_series(btc.get("avg_tx_usd")))

    # 5) Output volume BTC z-score(30d) — large-tx proxy
    oz = _z30("output_volume_btc")
    if oz is not None:
        c = _clamp(oz / 2 * 15, -15, 15)
        add("Output vol z30", f"{oz:.2f}σ", c,
            "on-chain BTC movement spike" if c > 0 else "quiet on-chain",
            _obs_date_of_series(btc.get("output_volume_btc")))

    # 6) Active addresses vs 30d mean — ±15 saturates at ±15%
    aa = _pct_vs_mean30("active_addresses")
    if aa is not None:
        c = _clamp(aa / 15 * 15, -15, 15)
        add("Active addr vs 30d", f"{aa:+.1f}%", c,
            "broad usage uptick" if c > 0 else "usage softening",
            _obs_date_of_series(btc.get("active_addresses")))

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
        # OLDEST contributing observation date, never the fetch clock. None
        # when not one component could be dated — consumers must render an
        # explicit "unavailable" rather than substituting today.
        "as_of": min(obs_dates) if obs_dates else None,
        "as_of_basis": WHALE_AS_OF_BASIS,
        "dated_inputs": len(obs_dates),
        "undated_inputs": undated,
        # Wall clock at fetch time. Debug/provenance only — a stale-kept
        # whale tree advances this while every date above stays frozen,
        # which is exactly why it may never be the headline stamp.
        "fetched_at": whale.get("fetched_at"),
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
    obs_dates: list[str] = []
    undated = 0

    def add(name: str, value: str, c: int, explanation: str,
            obs_date: str | None = None):
        nonlocal undated
        comps.append({
            "name": name, "value": value,
            "contribution": int(c), "explanation": explanation,
            "as_of": obs_date,
        })
        if obs_date:
            obs_dates.append(obs_date)
        else:
            undated += 1

    # 1) Active addresses z-score(30d) — ±25 saturates at ±2σ
    aa_z, aa_now = _z30(cm.get("AdrActCnt") or [])
    if aa_z is not None:
        c = _clamp(aa_z / 2 * 25, -25, 25)
        add("Active addr z30", f"{aa_z:.2f}σ", c,
            "demand picking up" if c > 0 else "demand softening" if c < 0 else "flat",
            _obs_date_of_series(cm.get("AdrActCnt")))

    # 2) Tx count z-score(30d) — ±25 saturates at ±2σ
    tx_z, tx_now = _z30(cm.get("TxCnt") or [])
    if tx_z is not None:
        c = _clamp(tx_z / 2 * 25, -25, 25)
        add("Tx count z30", f"{tx_z:.2f}σ", c,
            "network activity rising" if c > 0 else "network quieter",
            _obs_date_of_series(cm.get("TxCnt")))

    # 3) Transfer volume USD z-score(30d) — Coin Metrics paid metric, may be
    #    absent on the community tier (the fetcher silently drops it). Still
    #    try both the canonical and friendly keys.
    vol_series = cm.get("TxTfrValAdjUSD") or cm.get("transfer_volume_usd") or []
    vol_z, vol_now = _z30(vol_series)
    if vol_z is not None:
        c = _clamp(vol_z / 2 * 25, -25, 25)
        add("Transfer vol USD z30", f"{vol_z:.2f}σ", c,
            "economic throughput rising" if c > 0 else "economic throughput cooling",
            _obs_date_of_series(vol_series))

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
            "demand saturating slots" if c > 0 else "slots underused" if c < 0 else "at target",
            _obs_date_of_series(eds_series))

    if not comps:
        return {
            "available": False,
            "score": 0,
            "label": "NO DATA",
            "components": [],
            # Nothing contributed, so there is nothing to date. Explicitly
            # null — the fetch clock here was the original lie.
            "as_of": None,
            "as_of_basis": WHALE_AS_OF_BASIS,
            "dated_inputs": 0,
            "undated_inputs": 0,
            "fetched_at": whale.get("fetched_at"),
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
        # Oldest contributing observation date (rule: a composite is only as
        # fresh as its stalest input). None when nothing could be dated.
        "as_of": min(obs_dates) if obs_dates else None,
        "as_of_basis": WHALE_AS_OF_BASIS,
        "dated_inputs": len(obs_dates),
        "undated_inputs": undated,
        # Fetch clock — provenance only, never the headline stamp.
        "fetched_at": whale.get("fetched_at"),
        "disclaimer": ("Proxy composite from free Coin Metrics community + "
                       "Etherscan daily series. Directional indicator, not a "
                       "trading signal. ETH-specific whale cohorts (≥10K ETH "
                       "addresses) require a paid feed."),
    }


async def _fetch_social_async() -> dict:
    """Concurrent implementation of ``fetch_social``. Runs the 4 independent
    sub-fetchers in parallel via ``asyncio.gather`` + ``asyncio.to_thread``.
    They hit different hosts (reddit.com, CoinGecko + GitHub, Google News,
    api.santiment.net), so there's no shared rate-limit contention — wall time
    collapses to max(durations). community_dev_stats makes at most 4 CoinGecko
    calls per UTC day (it reuses today's result), paced 1 s apart."""
    print("    [social] reddit + community/dev + headline sentiment + santiment in parallel...")

    def _reddit() -> dict:
        try:
            return reddit_crypto_stats()
        except Exception as e:
            print(f"  [reddit] fatal: {e}", file=sys.stderr)
            return {"available": False, "reason": "fetch_error", "subreddits": {}}

    def _community() -> dict:
        try:
            return community_dev_stats()
        except Exception as e:
            print(f"  [community-dev] fatal: {type(e).__name__}", file=sys.stderr)
            return {"available": False, "reason": "fetch_error", "coins": {}}

    def _headlines() -> dict:
        try:
            return headline_sentiment()
        except Exception as e:
            print(f"  [headline-sentiment] fatal: {type(e).__name__}", file=sys.stderr)
            return {"available": False, "reason": "fetch_error", "coins": {}}

    def _san() -> dict:
        try:
            return santiment_metrics()
        except Exception as e:
            print(f"  [santiment] fatal: {e}", file=sys.stderr)
            return {"available": False, "reason": "fetch_error", "coins": {}}

    reddit, community, headlines, san = await asyncio.gather(
        asyncio.to_thread(_reddit),
        asyncio.to_thread(_community),
        asyncio.to_thread(_headlines),
        asyncio.to_thread(_san),
    )

    # Apply stale fallbacks post-gather (these are cheap local disk reads,
    # so doing them sequentially after the network gather is fine).
    if not reddit.get("available"):
        prev = _social_stale_fallback("reddit", {})
        if isinstance(prev, dict) and prev.get("subreddits"):
            reddit = {**prev, "stale": True}
    if not community.get("available"):
        prev = _social_stale_fallback("community_dev", {})
        if isinstance(prev, dict) and prev.get("coins"):
            community = {**prev, "stale": True}
    if not headlines.get("available"):
        prev = _social_stale_fallback("headline_sentiment", {})
        if isinstance(prev, dict) and prev.get("coins"):
            headlines = {**prev, "stale": True}

    return {
        "available": any(s.get("available") for s in (reddit, community, headlines, san)),
        "reddit": reddit,
        "community_dev": community,
        "headline_sentiment": headlines,
        "santiment": san,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def fetch_social() -> dict:
    """Consolidated 'Research' tab payload. Composes free social + dev +
    on-chain + news signals from 4 independent free sources, each handled
    separately so partial failures degrade gracefully:

      reddit             — subscribers + active users + top 24h posts (often
                           blocked on cloud IPs with HTTP 403; works locally)
      community_dev      — per-coin CoinGecko watchlist/vote counts + GitHub
                           repo stats (once per UTC day; see community_dev_stats)
      headline_sentiment — per-coin Google News headlines scored by Alpine
                           Data's own keyword rule (see HEADLINE_SENTIMENT_METHOD)
      santiment          — DAA + dev-activity (one fetch per UTC day; see santiment_gate)

    CryptoCompare social + news (the old `cryptocompare` / `cc_news` keys) were
    removed in 2026-10: the CoinDesk/CryptoCompare API needs a paid key.
    LunarCrush was removed earlier — their v4 API is gated behind the Builder
    plan (~$240/mo); no free endpoints exist.

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
    # Blockchair's average_transaction_{fee,value}_24h are WEI strings, like
    # every other ETH amount on this endpoint. They used to be passed through
    # raw under *_eth_* names, so the Whale tab printed a fee of
    # "88120340117212.000000 ETH" (and V2's .toFixed() on the string threw).
    avg_tx_fee_eth = _wei_to_eth(d.get("average_transaction_fee_24h"))
    avg_tx_val_eth = _wei_to_eth(d.get("average_transaction_value_24h"))
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
        if txs_24h in (None, "") or avg_tx_val_eth is None or mkt_px in (None, ""):
            transfer_volume_24h_usd = None
        else:
            transfer_volume_24h_usd = float(txs_24h) * float(avg_tx_val_eth) * float(mkt_px)
    except (TypeError, ValueError):
        transfer_volume_24h_usd = None

    return {
        "blocks_24h": d.get("blocks_24h"),
        "transactions_24h": txs_24h,
        "avg_tx_fee_eth_24h": avg_tx_fee_eth,
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


# Window the "Recent ETH whale transactions" feed is labelled with. The
# fetcher, the stale-cache replay and the browser all enforce it, so a row
# outside it can never be shown under a "last 24h" label.
ETH_LARGE_TX_WINDOW_HOURS = 24
# Cache key for the stale replay. The previous key
# ("blockchair_eth_large_transactions") holds rows from an unfiltered
# all-time `s=value(desc)` scan (2015-2022 transfers) and is deleted on sight.
_ETH_LARGE_TX_CACHE_KEY = "blockchair_eth_large_transactions_24h"
_ETH_LARGE_TX_LEGACY_CACHE_KEY = "blockchair_eth_large_transactions"


def _blockchair_time_utc(value) -> datetime | None:
    """Parse a Blockchair row time ("YYYY-MM-DD HH:MM:SS", UTC) or an ISO
    string. Returns an aware UTC datetime, or None when unparseable."""
    if not isinstance(value, str) or not value.strip():
        return None
    txt = value.strip().replace("T", " ").replace("Z", "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(txt[:19], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _eth_large_tx_within_window(rows, now: datetime | None = None,
                                hours: int = ETH_LARGE_TX_WINDOW_HOURS) -> list[dict]:
    """Keep only rows whose `time` falls inside the trailing `hours` window
    ending at `now` (UTC). Rows with a missing/unparseable time are dropped:
    an undated row cannot honestly be called recent."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    # Small allowance for clock skew between Blockchair and the runner.
    ceiling = now + timedelta(minutes=10)
    out: list[dict] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        t = _blockchair_time_utc(r.get("time"))
        if t is None or t < cutoff or t > ceiling:
            continue
        out.append(r)
    return out


def _blockchair_eth_large_transactions_impl(
    min_value_usd: float = 1_000_000.0, limit: int = 10,
    now: datetime | None = None,
) -> list[dict] | None:
    """Live fetch of large ETH transactions over the last 24h via Blockchair.

    Blockchair's `q=` filter takes an absolute datetime range; the relative
    form `time(24h)..` is rejected with HTTP 400 "Wrong filtering expression".
    So the query is `time(<now-24h UTC>..),value_usd(<min>..)`, sorted by USD
    value. There is deliberately NO unfiltered fallback: the old
    `s=value(desc)` retry returned the all-time largest transfers (2015-2022)
    and published them under a "last 24h" label.

    Returns at most `limit` rows (hash, value_eth, value_usd, time, fee_eth)
    sorted by USD value descending, re-filtered to the window and threshold
    client-side. Returns ``[]`` when the query succeeded but nothing
    qualified, and ``None`` when the request failed.
    """
    now = now or datetime.now(timezone.utc)
    min_usd = int(max(0, float(min_value_usd or 0)))
    since = (now - timedelta(hours=ETH_LARGE_TX_WINDOW_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    base = "https://api.blockchair.com/ethereum/transactions"

    j = _get(base, {
        "q": f"time({since}..),value_usd({min_usd}..)",
        "s": "value_usd(desc)",
        "limit": str(max(limit, 10)),
    })
    if not j or not isinstance(j, dict):
        return None
    rows = j.get("data")
    if rows is None:
        return None
    if not isinstance(rows, list):
        return None

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
        if value_usd < min_usd:
            continue
        out.append({
            "hash":      h,
            "value_eth": value_eth,
            "value_usd": value_usd,
            "time":      r.get("time"),
            "fee_eth":   fee_eth,
        })

    out = _eth_large_tx_within_window(out, now)
    out.sort(key=lambda r: r.get("value_usd") or 0, reverse=True)
    return out[:limit]


def blockchair_eth_large_transactions_with_status(
    min_value_usd: float = 1_000_000.0, limit: int = 10,
    now: datetime | None = None,
) -> dict:
    """Fetch the last-24h large-tx feed and say where the rows came from.

    Returns ``{"rows": [...], "status": {...}}`` where status.source is
    "live" (query succeeded, possibly with zero qualifying rows),
    "stale-cache" (query failed; replaying the last good fetch, trimmed to
    rows still inside the 24h window) or "unavailable" (query failed and no
    cached row is still recent). Never raises.
    """
    now = now or datetime.now(timezone.utc)
    # The legacy cache file was filled by the unfiltered all-time scan; never
    # replay it.
    try:
        _stale_path(_ETH_LARGE_TX_LEGACY_CACHE_KEY).unlink(missing_ok=True)
    except OSError:
        # Best-effort cleanup only: the legacy key is never read again, so a
        # file we couldn't delete is harmless and must not stop the fetch.
        pass
    status = {
        "window_hours": ETH_LARGE_TX_WINDOW_HOURS,
        "min_value_usd": float(min_value_usd or 0),
        "as_of": now.isoformat(timespec="seconds"),
    }
    try:
        out = _blockchair_eth_large_transactions_impl(min_value_usd, limit, now=now)
    except Exception as e:
        print(f"  [blockchair_eth_large_transactions] fatal: {e}", file=sys.stderr)
        out = None
    if isinstance(out, list):
        if out:
            _stale_save(_ETH_LARGE_TX_CACHE_KEY, out)
        return {"rows": out, "status": {**status, "source": "live"}}
    cached = _stale_load(_ETH_LARGE_TX_CACHE_KEY)
    recent = _eth_large_tx_within_window(cached if isinstance(cached, list) else [], now)
    if recent:
        return {"rows": recent[:limit], "status": {**status, "source": "stale-cache"}}
    return {"rows": [], "status": {**status, "source": "unavailable"}}


def blockchair_eth_large_transactions(
    min_value_usd: float = 1_000_000.0, limit: int = 10,
    now: datetime | None = None,
) -> list[dict]:
    """Rows-only wrapper around `blockchair_eth_large_transactions_with_status`.

    Every row returned is inside the trailing 24h window, whether it came from
    the live query or the stale cache; an empty list means nothing recent is
    known. Never raises.
    """
    return blockchair_eth_large_transactions_with_status(min_value_usd, limit, now=now)["rows"]


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
        # Retail money-market fund assets (weekly, NSA, $B) — the "cash on the
        # sidelines" confirmer for the Money Flow Index. WRMFNS is active; the
        # SA/institutional variants (WRMFSL/WIMFSL/IMFSL) were discontinued 2021.
        "retail_mmf":   "WRMFNS",
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

    Returns a list of {date, open, high, low, close, volume} for each valid
    daily bar. Yahoo includes None values for missing days; those are filtered
    out. open/high/low fall back to close when Yahoo omits them so downstream
    money-flow math (MFI/CMF, which need H/L) never sees None. Empty list on
    failure.
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
        opens = quote.get("open") or []
        highs = quote.get("high") or []
        lows = quote.get("low") or []
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
        c = float(c)
        o = opens[i] if i < len(opens) else None
        h = highs[i] if i < len(highs) else None
        l = lows[i] if i < len(lows) else None
        out.append({
            "date":   datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d"),
            "open":   float(o) if o is not None else c,
            "high":   float(h) if h is not None else c,
            "low":    float(l) if l is not None else c,
            "close":  c,
            "volume": int(v) if v is not None else 0,
        })
    return out


# Index ETFs that proxy the 3 major US equity indices for the Money Flow Index.
MFX_ETFS = ("SPY", "QQQ", "DIA")


def build_money_flow_payload() -> dict:
    """Assemble + score the Money Flow Index (MFX).

    Combines four legs into a -100..+100 composite (per-index + market-wide):
      * per-index ETF flow  — ΔSharesOutstanding × NAV for SPY/QQQ/DIA
        (``fetch_equity_etf_flows``; warms up over ~1 trading day)
      * per-index buy/sell  — MFI + Chaikin Money Flow on each ETF's OHLCV
      * market-wide cash     — ICI weekly money-market-fund WoW change (inverted)
      * market-wide MF flow  — ICI weekly equity mutual-fund net flow

    Returns ``money_flow.build_money_flow_index`` output plus a ``sources`` block
    (raw ICI + ETF-flow data for the UI's stacked-flow bar / staleness chips).
    Never raises — any failed leg degrades that component to neutral so the
    gauge (and the build) survive.
    """
    import money_flow as _mf

    # --- per-index buy/sell (MFI / CMF) from OHLCV ---------------------------
    legs: dict[str, dict] = {}
    for tk in MFX_ETFS:
        try:
            bars = yahoo_chart_history(tk, "6mo")
        except Exception:
            bars = []
        mfi_hist: list[float] = []
        if bars:
            # rolling MFI(14): one value per day once enough bars exist —
            # the trailing distribution the composite z-scores against.
            for k in range(15, len(bars) + 1):
                v = _mf.mfi(bars[:k], 14)
                if v is not None:
                    mfi_hist.append(round(v, 4))
        last = bars[-1] if bars else None
        legs[tk] = {
            "etf_flow": None,
            "etf_flow_hist": [],
            "mfi": _mf.mfi(bars, 14) if bars else None,
            "mfi_hist": mfi_hist,
            "cmf": _mf.cmf(bars, 20) if bars else None,
            "dollar_volume": (last["close"] * last["volume"]) if last else 0.0,
            # Observation date of the MFI/CMF half of this leg: the newest BAR,
            # not the moment yahoo_chart_history() ran. Widened below to the
            # older of (bar, ETF-flow row) once the flow leg lands.
            "as_of": (last or {}).get("date"),
        }

    # --- per-index ETF flow (ΔSO × NAV) -------------------------------------
    etf_block: dict = {}
    try:
        import fetch_equity_etf_flows as _eef
        etf_block = _eef.main(write=True) or {}
        for tk in MFX_ETFS:
            t = (etf_block.get("tickers") or {}).get(tk) or {}
            hist = [h for h in (t.get("history") or [])
                    if h.get("net_flow_musd") is not None]
            legs[tk]["etf_flow"] = t.get("net_flow_musd")
            legs[tk]["etf_flow_hist"] = [h["net_flow_musd"] for h in hist]
            # The flow leg's own data date is the newest history ROW's date.
            # Deliberately NOT etf_block["as_of"] — fetch_equity_etf_flows sets
            # that from _today(), i.e. when the fetcher ran, which is precisely
            # the clock read this whole change exists to stop propagating.
            flow_day = max((h.get("date") or "" for h in hist), default="")
            # A leg fuses two components, so it is only as fresh as the OLDER of
            # them (same min-not-max rule the composite applies one level up).
            days = [d for d in (legs[tk].get("as_of"), flow_day or None) if d]
            legs[tk]["as_of"] = min(days) if days else None
    except Exception as e:
        print(f"  [money_flow] equity ETF flows failed: {e}", file=sys.stderr)

    # --- market-wide ICI MMF + mutual-fund flows ----------------------------
    mmf_block: dict = {"weekly": []}
    mf_flows_block: dict = {"weekly": []}
    try:
        import fetch_money_flows as _fmf
        mf = _fmf.fetch_all(write=True) or {}
        mmf_block = mf.get("mmf") or {"weekly": []}
        mf_flows_block = mf.get("mf_flows") or {"weekly": []}
    except Exception as e:
        print(f"  [money_flow] ICI flows failed: {e}", file=sys.stderr)

    # Keep each weekly row's own date next to its value. The ICI blocks carry a
    # top-level `as_of` too, but fetch_money_flows sets it with _now_iso() —
    # when the download happened, not what week the data describes. ICI
    # publishes weekly with a multi-day lag, so those differ by design and the
    # row date is the only honest one.
    mmf_rows = [w for w in (mmf_block.get("weekly") or []) if w.get("total") is not None]
    mmf_totals = [w["total"] for w in mmf_rows]
    mmf_wow_hist = [round(mmf_totals[i] - mmf_totals[i - 1], 2) for i in range(1, len(mmf_totals))]
    # A week-over-week CHANGE observes the newer of the two weeks it spans.
    mmf_as_of = mmf_rows[-1].get("date") if len(mmf_rows) >= 2 else None

    ici_rows = [w for w in (mf_flows_block.get("weekly") or [])
                if w.get("total_equity") is not None]
    ici_eq_hist = [w["total_equity"] for w in ici_rows]
    ici_as_of = ici_rows[-1].get("date") if ici_rows else None

    market: dict = dict(legs)
    market["ici_equity_flow"] = ici_eq_hist[-1] if ici_eq_hist else None
    market["ici_equity_flow_hist"] = ici_eq_hist
    market["ici_as_of"] = ici_as_of
    market["mmf_wow_change"] = mmf_wow_hist[-1] if mmf_wow_hist else None
    market["mmf_wow_change_hist"] = mmf_wow_hist
    market["mmf_as_of"] = mmf_as_of
    # No market-wide `as_of` override is set on purpose: there is no single
    # observation for a four-leg composite, so build_money_flow_index() derives
    # it as the OLDEST contributing leg (or None). Setting one here would be
    # this function asserting a date it does not have.

    try:
        mfx = _mf.build_money_flow_index({"market": market})
    except Exception as e:
        print(f"  [money_flow] composite failed: {e}", file=sys.stderr)
        # as_of stays explicitly None: a gauge built from nothing has no
        # observation date, and "today" would be a fabricated one.
        mfx = {"as_of": None,
               "as_of_inputs": {"resolved_from": f"composite failed: {e}",
                                "legs": {}, "undated_contributors": []},
               "headline": {"score": 0, "label": "Neutral", "components": []},
               "per_index": []}

    mfx["sources"] = {
        "mmf": mmf_block,
        "mf_flows": mf_flows_block,
        "equity_etf_flows": etf_block,
    }
    return mfx


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
                # Money-flow indicators off the SAME bars (no extra fetch) — feed
                # the Stock Flows tab reliably (avoids a second Yahoo fan-out that
                # gets IP-throttled). hist already carries OHLC after the
                # yahoo_chart_history widening.
                try:
                    import money_flow as _mf
                    mfi_v = _mf.mfi(hist, 14)
                    cmf_v = _mf.cmf(hist, 20)
                except Exception:
                    mfi_v = cmf_v = None
                # Observation date for this row = the date of the last
                # daily bar the score was actually computed from (Yahoo
                # trading-day calendar). NOT the time we ran: on a weekend
                # or a market holiday every row here is legitimately 1-3
                # days old, and a same-day stamp would hide that. Also the
                # only thing that stops a hist-fetch failure elsewhere in
                # the sweep from looking current. None when the history is
                # empty (compute_stock_signal returns a zero score with no
                # rolling history) — an explicit unavailable, not a guess.
                _hist = sig.get("history") or []
                as_of = (_hist[-1].get("date")
                         if isinstance(_hist[-1], dict) else None) if _hist else None
                return {
                    "symbol":     sym,
                    "name":       m["name"],
                    "last_price": m["last_price"],
                    "change_pct": m["change_pct"],
                    "volume":     m["volume"],
                    "score":      sig["score"],
                    "label":      sig["label"],
                    "as_of":      as_of,
                    "components": sig["components"],
                    "history":    sig["history"],
                    "poc":        poc,
                    "mfi":        mfi_v,
                    "cmf":        cmf_v,
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
# practice protects against the per-IP burst limiter). The top-50 daily-series
# sweep (fetch_top_daily_series) runs in Batch 2, after these, with its own
# 2.5 s pacing and per-run cap (CoinGeckoBudget). Other APIs
# (Coinbase, Kraken, DeFiLlama, mempool.space, etc.) tolerate much higher
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
    concurrency cap. Used for all non-CG sources (OKX, DeFiLlama,
    mempool.space, Coinbase, Yahoo, FRED, Google News, etc.) and for the
    top-50 daily-series sweep, which paces its own CoinGecko calls."""
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
        yahoo_idx, stocks_signals, fng,                # yahoo × 2, F&G
        money_flow_block,                              # Money Flow Index (MFX)
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
        _timed("yahoo_indices",           _bg_call(yahoo_indices)),
        _timed("fetch_stocks_signals",    _bg_call(fetch_stocks_signals, 50)),
        _timed("fear_greed",              _bg_call(fear_greed)),
        _timed("build_money_flow",        _bg_call(build_money_flow_payload)),
    )
    if fred.get("available"):
        print("  FRED macro (DXY/SPX/Gold/10Y/M2) available")
    print(f"    -> {len(ai_curated.get('top_funded_companies', []))} companies, "
          f"{len(ai_curated.get('investment_kpis', []))} inv KPIs, "
          f"{len(ai_curated.get('whitepaper_kpis', []))} wp KPIs")

    # ---- Stock Flows sidecar (piggybacks on the stocks_signals fetch above) --
    # Reliable path: score the most-active index members from the MFI/CMF already
    # computed during the (working) stocks_signals fetch — no extra Yahoo calls,
    # so it isn't subject to the IP throttling that zeroed the standalone fetch.
    # Last-good preserving: a thin/empty result never clobbers a populated sidecar.
    try:
        import fetch_stock_money_flow as _sf
        _sfx = _sf.build_from_signals(stocks_signals, write=True)
        print(f"  Stock Flows: scored {_sfx.get('scored_count', 0)} most-active index members")
    except Exception as e:
        print(f"  [stock-flows] sidecar build failed: {e}", file=sys.stderr)

    # ---- Stale-keep for top_markets (was inline in the sequential version) --
    top_markets = top_markets_raw or stale_keep_markets_top()

    # ---- Batch 2: depends on top_markets ------------------------------------
    # fetch_top_daily_series pulls a complete-day close/volume/market-cap
    # series for each of the top 50 (CoinGecko market_chart, then Coinbase ->
    # Kraken -> Binance.US). It is cached per coin per UTC day, so only the
    # first run of a day spends CoinGecko calls (<= 50, paced 2.5 s apart);
    # every later run that day is served from data/.stale/. The POC table, the
    # signal-breadth chart AND the Alpine Large-Cap Crypto Index are all
    # computed from those same series — the index costs no extra call.
    # fetch_headline_sentiment_by_coin runs alongside: 25 Google News RSS
    # queries (no CoinGecko), deduplicated against the publisher feeds.
    print("  Batch 2: top-50 daily series + per-coin headlines (depend on top_markets)...")
    top_series, headline_by_coin = await asyncio.gather(
        _timed(
            "fetch_top_daily_series",
            _bg_call(fetch_top_daily_series, top_markets, 50, 180),
        ),
        _timed(
            "fetch_headline_sentiment_by_coin",
            _bg_call(fetch_headline_sentiment_by_coin, top_markets, 25, news),
        ),
    )
    series_by_id = (top_series or {}).get("series") or {}
    poc_top = compute_poc_top_markets(top_markets, 50, 180, series_by_id=series_by_id)
    try:
        alpine_index = compute_alpine_index(top_markets, series_by_id)
    except Exception as e:  # a method bug must not take down fetch_trading
        print(f"  [alpine-index] {type(e).__name__}: {e}", file=sys.stderr)
        alpine_index = {"available": False, "name": ALPINE_INDEX_NAME,
                        "reason": f"index computation failed ({type(e).__name__})",
                        "series": []}

    # ETH/BTC ratio from prices
    btc_p = {p["date"]: p["value"] for p in btc_mkt["price"]}
    ethbtc = []
    for p in eth_mkt["price"]:
        b = btc_p.get(p["date"])
        if b and b > 0:
            ethbtc.append({"date": p["date"], "value": p["value"] / b})

    print(f"  [timing] fetch_trading total: {time.monotonic() - t_total:.2f}s")

    # DeFi subtree + its provenance. `data-defi.json` is written as a lazy
    # sidecar by the frontend builders, which have no access to the fetch
    # that produced it — without this the sidecar carries no derivable date
    # at all and any stamp on it would have to be build time. See
    # `defi_provenance` for why the snapshot inputs may use the fetch
    # instant and the historical series may not.
    tvl_history = {
        "Ethereum": tvl_eth,
        "Solana": tvl_sol,
        "Arbitrum": tvl_arb,
        "Base": tvl_base,
    }
    defi_block = {
        "chains": chains,
        "protocols": protocols,
        "yields_stablecoin": yields_top,
        "bridges": bridges,
        "tvl_history": tvl_history,
        **defi_provenance(chains, protocols, yields_top, bridges, tvl_history),
    }

    return {
        "btc": {
            "price": btc_mkt["price"],
            "volume": btc_mkt["volume"],
            "market_cap": btc_mkt["market_cap"],
            "funding": okx_fund_btc,
            "open_interest_usd": okx_oi_btc,
            "long_short_ratio": okx_ls_btc,
            "dvol": dvol_btc,
            **_stale_flags(btc_mkt),
        },
        "eth": {
            "price": eth_mkt["price"],
            "volume": eth_mkt["volume"],
            "market_cap": eth_mkt["market_cap"],
            "funding": okx_fund_eth,
            "open_interest_usd": okx_oi_eth,
            "long_short_ratio": okx_ls_eth,
            "dvol": dvol_eth,
            **_stale_flags(eth_mkt),
        },
        "link": {
            "price": link_mkt["price"],
            "volume": link_mkt["volume"],
            "market_cap": link_mkt["market_cap"],
            "funding": okx_fund_link,
            "open_interest_usd": okx_oi_link,
            "long_short_ratio": okx_ls_link,
            "dvol": [],
            **_stale_flags(link_mkt),
        },
        "ltc": {
            "price": ltc_mkt["price"],
            "volume": ltc_mkt["volume"],
            "market_cap": ltc_mkt["market_cap"],
            "funding": okx_fund_ltc,
            "open_interest_usd": okx_oi_ltc,
            "long_short_ratio": okx_ls_ltc,
            "dvol": [],
            **_stale_flags(ltc_mkt),
        },
        "global": glob,
        "coinbase": cb_spot,
        "coinbase_intl_perps": cb_intl,
        "coinbase_intl_perps_status": dict(_CB_INTL_LAST_STATUS),
        # Overview sentiment's perp-funding input (OKX; Coinbase Intl perps
        # are paused/delisted). See perp_funding_summary.
        "perp_funding": perp_funding_summary({
            "BTC": okx_fund_btc, "ETH": okx_fund_eth,
            "LINK": okx_fund_link, "LTC": okx_fund_ltc,
        }),
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
        # Which source each top-50 series came from, today-cache hits, stale /
        # missing coins and the CoinGecko calls this run made. See
        # fetch_top_daily_series.
        "poc_top_meta": (top_series or {}).get("meta") or {},
        "defi": defi_block,
        "news": news,
        # Per-coin headline sentiment (top-25 by mcap): Google News RSS
        # headlines scored by Alpine Data's own keyword rule, minus any
        # headline already in `news`. Keyed by uppercase symbol. Frontend
        # `groupNewsBySymbol` adds these counts to its RSS counts so coins that
        # aren't named in the 5 publisher feeds still get scored. See
        # `fetch_headline_sentiment_by_coin` for the shape.
        "news_sentiment_by_coin": headline_by_coin,
        "ai_news": ai_news,
        "ai_funding": ai_funding,
        "ai_curated": ai_curated,
        # Alpine Large-Cap Crypto Index: Alpine Data's own cap-weighted index
        # of the 10 largest eligible coins, from CoinGecko closes + market caps.
        # It took the Futures-tab slot of the CoinDesk CADLI chart (CADLI's API
        # needs a paid key since 2026-10) and is never labelled as CADLI.
        "alpine_index": alpine_index,
        "yahoo_indices": yahoo_idx,
        "stocks_signals": stocks_signals,
        "money_flow": money_flow_block,
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
        _timed("blockchair_eth_large_transactions", _bg_call(blockchair_eth_large_transactions_with_status, 1_000_000)),
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
            "large_transactions": (eth_large_txs or {}).get("rows") or [],
            "large_transactions_status": (eth_large_txs or {}).get("status") or {},
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
