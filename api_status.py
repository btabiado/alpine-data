#!/usr/bin/env python3
"""Live reachability probe for the crypto dashboard's upstream data-source APIs.

This is the *liveness* counterpart to ``scripts/build_health_status.py``: that
script classifies cached ``data/`` files by mtime (is the data fresh?), whereas
this one actually hits each upstream endpoint and reports whether it is
reachable *right now* (is the API up?).

Two ways it's used:
  1. Imported by ``server.py`` — ``get_status(ttl=…)`` returns a TTL-cached
     snapshot that backs the ``/api/status`` endpoint and the ``/status`` page.
  2. Run as a CLI — ``python api_status.py`` writes
     ``data/health/api_status.json`` so the static GitHub-Pages mirror
     (``health/apis.html``) has a snapshot to fall back on when there's no
     live server to probe through.

Pure stdlib (urllib + concurrent.futures) so it stays cheap in CI and adds no
dependency to the server process.

Note on environments with locked-down egress (e.g. Claude Code on the web,
where only github.com is allowlisted): every target will come back "down" or
"blocked". That reflects the *probe host's* network policy, not the APIs — run
it somewhere with open outbound to get a true picture.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
OUT_PATH = REPO_ROOT / "data" / "health" / "api_status.json"

_UA = "Mozilla/5.0 (alpine-data api-status probe)"
_SSL_CTX = ssl.create_default_context()
DEFAULT_TIMEOUT = 8.0

# Canonical list of the data sources the dashboard depends on. Each probe URL
# is a real, cheap, keyless endpoint on that host (a ping / single-row query)
# so a 2xx genuinely means "this API path is serving". ``key_env`` names the
# environment variable that unlocks the source — when set, a 401/403 is
# reported as "auth_required" (endpoint live, just gated) rather than down, and
# the snapshot records whether the key is actually configured.
#
# Fields: label, category (≈ dashboard tab/role), url, key_env (None = keyless)
TARGETS: list[dict] = [
    # ---- price / market cap ----
    {"label": "CoinGecko",            "category": "Price/MktCap",  "url": "https://api.coingecko.com/api/v3/ping",                                                          "key_env": None},
    {"label": "CryptoCompare CCCAGG", "category": "Price/MktCap",  "url": "https://min-api.cryptocompare.com/data/price?fsym=BTC&tsyms=USD",                                "key_env": "CRYPTOCOMPARE_API_KEY"},
    {"label": "CryptoCompare data-api","category": "Research",     "url": "https://data-api.cryptocompare.com/asset/v1/top/list?page=1&page_size=1",                       "key_env": "CRYPTOCOMPARE_API_KEY"},
    {"label": "GeckoTerminal",        "category": "Price/MktCap",  "url": "https://api.geckoterminal.com/api/v2/networks",                                                  "key_env": None},
    # ---- exchange / derivatives ----
    {"label": "Coinbase Exchange",    "category": "Spot",          "url": "https://api.exchange.coinbase.com/products/BTC-USD/ticker",                                      "key_env": None},
    {"label": "Coinbase Intl (perps)","category": "Futures",       "url": "https://api.international.coinbase.com/api/v1/instruments",                                      "key_env": None},
    {"label": "CoinDesk CADLI",       "category": "Futures",       "url": "https://data-api.coindesk.com/index/cc/v1/latest/tick?market=cadli&instruments=BTC-USD",         "key_env": None},
    {"label": "OKX",                  "category": "Futures",       "url": "https://www.okx.com/api/v5/public/funding-rate?instId=BTC-USD-SWAP",                             "key_env": None},
    {"label": "Deribit",              "category": "Futures",       "url": "https://www.deribit.com/api/v2/public/get_index_price?index_name=btc_usd",                       "key_env": None},
    {"label": "Alternative.me F&G",   "category": "Sentiment",     "url": "https://api.alternative.me/fng/?limit=1",                                                        "key_env": None},
    # ---- on-chain / whale ----
    {"label": "mempool.space",        "category": "Whale",         "url": "https://mempool.space/api/v1/fees/recommended",                                                  "key_env": None},
    {"label": "blockchain.info",      "category": "Whale",         "url": "https://api.blockchain.info/stats",                                                              "key_env": None},
    {"label": "Blockchair",           "category": "Whale",         "url": "https://api.blockchair.com/bitcoin/stats",                                                       "key_env": None},
    {"label": "Etherscan v2",         "category": "Whale",         "url": "https://api.etherscan.io/v2/api?chainid=1&module=stats&action=ethprice",                         "key_env": "ETHERSCAN_API_KEY"},
    {"label": "CoinMetrics",          "category": "Whale",         "url": "https://community-api.coinmetrics.io/v4/catalog/assets?assets=btc",                              "key_env": "COINMETRICS_API_KEY"},
    {"label": "Glassnode",            "category": "Whale",         "url": "https://api.glassnode.com/v1/metrics/market/price_usd_close",                                     "key_env": "GLASSNODE_API_KEY"},
    # ---- defi ----
    {"label": "DeFiLlama TVL",        "category": "DeFi",          "url": "https://api.llama.fi/v2/chains",                                                                 "key_env": None},
    {"label": "DeFiLlama yields",     "category": "DeFi",          "url": "https://yields.llama.fi/pools",                                                                  "key_env": None},
    # ---- equities / macro ----
    {"label": "Yahoo Finance",        "category": "Stocks",        "url": "https://query1.finance.yahoo.com/v8/finance/chart/BTC-USD?range=1d&interval=1d",                 "key_env": None},
    {"label": "FRED",                 "category": "Macro",         "url": "https://api.stlouisfed.org/fred/releases",                                                       "key_env": "FRED_API_KEY"},
    # ---- ETF flows ----
    {"label": "Farside",              "category": "ETF Flows",     "url": "https://farside.co.uk/btc/",                                                                     "key_env": None},
    {"label": "SoSoValue",            "category": "ETF Flows",     "url": "https://api.sosovalue.com/openapi/v1/etf/historicalInflowChart",                                 "key_env": "SOSOVALUE_API_KEY"},
    # ---- news / social / research ----
    {"label": "Reddit",               "category": "Research",      "url": "https://www.reddit.com/r/CryptoCurrency/about.json",                                            "key_env": None},
    {"label": "Santiment",            "category": "Research",      "url": "https://api.santiment.net/graphql",                                                             "key_env": "SANTIMENT_API_KEY"},
    {"label": "SEC EDGAR",            "category": "AI News",       "url": "https://efts.sec.gov/LATEST/search-index?q=ai",                                                 "key_env": None},
]


def _verdict(status: int | None, needs_key: bool) -> str:
    """Map an HTTP status (or None for connection failure) to a verdict.

    up            — 2xx/3xx, the endpoint served.
    auth_required — 401/403 on a key-gated source: endpoint is live, key gates it.
    rate_limited  — 429: live but throttling us right now.
    blocked       — 401/403 on a keyless source (geo-block, WAF, or egress proxy).
    degraded      — other 4xx/5xx: reachable but erroring.
    down          — no HTTP response at all (DNS / TCP / TLS / timeout).
    """
    if status is None:
        return "down"
    if 200 <= status < 400:
        return "up"
    if status == 429:
        return "rate_limited"
    if status in (401, 403):
        return "auth_required" if needs_key else "blocked"
    return "degraded"


def _probe_one(target: dict, timeout: float) -> dict:
    url = target["url"]
    key_env = target.get("key_env")
    needs_key = bool(key_env)
    t0 = time.monotonic()
    status: int | None = None
    note = ""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA}, method="GET")
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
            status = r.status
            # Drain a little so keep-alive sockets close cleanly; ignore body.
            r.read(1)
    except urllib.error.HTTPError as e:
        status = e.code
        note = (e.reason or "")[:80]
    except Exception as e:  # URLError, timeout, ssl, etc.
        note = f"{type(e).__name__}: {e}"[:120]
    latency_ms = int((time.monotonic() - t0) * 1000)
    verdict = _verdict(status, needs_key)
    return {
        "label": target["label"],
        "category": target["category"],
        "host": url.split("/")[2],
        "status": status,
        "latency_ms": latency_ms,
        "verdict": verdict,
        # An auth_required source counts as reachable for the up/down summary.
        "reachable": verdict in ("up", "auth_required", "rate_limited"),
        "needs_key": needs_key,
        "key_present": bool(os.environ.get(key_env)) if key_env else None,
        "note": note,
    }


def probe_all(timeout: float = DEFAULT_TIMEOUT, max_workers: int = 12) -> dict:
    """Probe every target in parallel. Returns a JSON-ready snapshot dict."""
    with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
        sources = list(ex.map(lambda t: _probe_one(t, timeout), TARGETS))
    sources.sort(key=lambda s: (s["category"], s["label"]))

    def count(*verdicts: str) -> int:
        return sum(1 for s in sources if s["verdict"] in verdicts)

    summary = {
        "total": len(sources),
        "up": count("up"),
        "auth_required": count("auth_required"),
        "rate_limited": count("rate_limited"),
        "degraded": count("degraded"),
        "blocked": count("blocked"),
        "down": count("down"),
        "reachable": sum(1 for s in sources if s["reachable"]),
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "timeout_s": timeout,
        "summary": summary,
        "sources": sources,
    }


# ---- TTL cache for the server endpoint ----------------------------------
# Probing 25 hosts on every page load would be slow and rude to upstreams, so
# server.py reuses one snapshot for `ttl` seconds. A lock serializes the
# refresh so a burst of concurrent requests triggers at most one probe sweep.
_cache: dict = {"snapshot": None, "at": 0.0}
_cache_lock = threading.Lock()


def get_status(ttl: float = 60.0, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Return a cached snapshot, re-probing only if older than ``ttl`` seconds."""
    now = time.monotonic()
    snap = _cache["snapshot"]
    if snap is not None and (now - _cache["at"]) < ttl:
        return snap
    with _cache_lock:
        # Double-check: another thread may have refreshed while we waited.
        now = time.monotonic()
        snap = _cache["snapshot"]
        if snap is not None and (now - _cache["at"]) < ttl:
            return snap
        snap = probe_all(timeout=timeout)
        snap["cached"] = False
        _cache["snapshot"] = snap
        _cache["at"] = now
        return snap


def main() -> int:
    snapshot = probe_all()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(snapshot, indent=2))
    s = snapshot["summary"]
    rel = OUT_PATH.relative_to(REPO_ROOT)
    print(
        f"wrote {rel} ({s['total']} sources: {s['up']} up, "
        f"{s['auth_required']} auth-gated, {s['rate_limited']} rate-limited, "
        f"{s['degraded']} degraded, {s['blocked']} blocked, {s['down']} down)"
    )
    # Print a compact table to stdout for CI logs / manual runs.
    for src in snapshot["sources"]:
        st = src["status"] if src["status"] is not None else "—"
        key = ""
        if src["needs_key"]:
            key = " [key set]" if src["key_present"] else " [no key]"
        print(f"  {src['verdict']:<13} {str(st):>4} {src['latency_ms']:>5}ms  "
              f"{src['label']}{key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
