#!/usr/bin/env python3
"""Live reachability probe for the dashboard's upstream data-source APIs.

Covers every domain the dashboard fetches: crypto/whale/DeFi/ETF, equities &
macro, plus the City, Aviation, Real-Estate, Metals, Supplies, UAP and Travel
tabs. (Some macro hosts — FRED, Yahoo — are shared across tabs and probed once.)

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

THE KEY-REPORTING CONTRACT
Each source that names a ``key_env`` reports a three-state ``key_state``:
``set`` (the secret reached this process), ``unset`` (the variable was handed to
us empty — the secret is not configured) and ``not_wired`` (the variable never
arrived at all — whatever launched us forgot to map it). The last state exists
because collapsing it into "no key" is what let this page report `key_present=
false` for nine correctly-configured secrets: the workflow simply never passed
them, and the page had no vocabulary to say so. Values are never read, printed
or stored — presence only.

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
DEFAULT_TIMEOUT = 12.0  # above the ~8-9s TLS-handshake window CI runners hit
PROBE_ATTEMPTS = 2       # 1 retry: gov/city hosts give transient timeouts from CI

# Canonical list of the data sources the dashboard depends on. Each probe URL
# is a real, cheap, keyless endpoint on that host (a ping / single-row query)
# so a 2xx genuinely means "this API path is serving". ``key_env`` names the
# environment variable that unlocks the source — when set, a 401/403 is
# reported as "auth_required" (endpoint live, just gated) rather than down, and
# the snapshot records whether the key is actually configured.
#
# *** A key_env named here is a PROMISE that the probe process receives it. ***
# Naming one that no workflow maps into the step's `env:` is the bug that made
# this page lie for months: Socrata/Census/BLS/AirNow/FBI-CDE/OpenSky/Reddit all
# reported "no key" on /health/apis.html even with the repository secret
# correctly set, because pages.yml handed the probe only five of the fourteen
# names this file used. ``key_state()`` below now tells those two situations
# apart, and tests/test_api_status_wiring.py fails the build if a key_env here
# is not mapped by the workflow step that runs this script.
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
    {"label": "bitinfocharts",        "category": "Whale",         "url": "https://bitinfocharts.com/bitcoin-distribution-history.html",                                     "key_env": None},
    # ---- defi ----
    {"label": "DeFiLlama TVL",        "category": "DeFi",          "url": "https://api.llama.fi/v2/chains",                                                                 "key_env": None},
    {"label": "DeFiLlama yields",     "category": "DeFi",          "url": "https://yields.llama.fi/pools",                                                                  "key_env": None},
    {"label": "DeFiLlama prices",     "category": "DeFi",          "url": "https://coins.llama.fi/prices/current/coingecko:bitcoin",                                        "key_env": None},
    {"label": "DeFiLlama stablecoins","category": "DeFi",          "url": "https://stablecoins.llama.fi/stablecoins?includePrices=false",                                   "key_env": None},
    {"label": "DeFiLlama bridges",    "category": "DeFi",          "url": "https://bridges.llama.fi/bridges",                                                               "key_env": None},
    # ---- equities / macro ----
    {"label": "Yahoo Finance",        "category": "Stocks",        "url": "https://query1.finance.yahoo.com/v8/finance/chart/BTC-USD?range=1d&interval=1d",                 "key_env": None},
    {"label": "FRED",                 "category": "Macro",         "url": "https://api.stlouisfed.org/fred/releases",                                                       "key_env": "FRED_API_KEY"},
    # ---- ETF flows ----
    # Farside blocks automated requests (Cloudflare 403); the dashboard
    # actually sources BTC ETF flows from a keyless GitHub mirror CSV (see
    # fetch_live.MIRROR_BTC_CSV), so probe that real data path instead.
    {"label": "Farside (ETF mirror)", "category": "ETF Flows",     "url": "https://raw.githubusercontent.com/canadiancode/btc-etf-flows/main/Bitcoin-ETF-Flow-Data/data/BTC_ETF_INFLOWS_OUTFLOWS.csv", "key_env": None},
    # SoSoValue dropped: api.sosovalue.com no longer resolves (DNS NXDOMAIN — the
    # API subdomain was decommissioned).
    #
    # CoinGlass dropped one step later, for a related reason. Its branch in
    # fetch_live.fetch_all() is only reached when COINGLASS_API_KEY is set, and
    # no workflow passes that variable to anything, so fetch_all always takes
    # the keyless GitHub-mirror path — the one probed directly above. Keeping a
    # probe (and a key_env) for a branch that never executes reported on a
    # source the dashboard does not use, and made COINGLASS_API_KEY look like
    # live plumbing on /health/apis.html.
    #
    # Both keys stay named in scripts/check_secrets_present.py, annotated
    # "(retired…)", deliberately: a user who has them set should be told they
    # do nothing, rather than told nothing at all.
    # ---- news / social / research ----
    # Reddit hard-blocks datacenter IPs on the keyless public API; the dashboard
    # reaches it via OAuth (REDDIT_CLIENT_ID/SECRET), so a 403 here means
    # "needs credentials from this host", i.e. auth_required rather than down.
    {"label": "Reddit",               "category": "Research",      "url": "https://www.reddit.com/r/CryptoCurrency/about.json",                                            "key_env": "REDDIT_CLIENT_ID", "headers": {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"}},
    # Santiment is KEYLESS here on purpose. SANTIMENT_API_KEY used to be named
    # as this target's key_env and existed nowhere else in the repo — not in a
    # workflow, not a repository secret, and fetch_market.santiment_metrics()
    # queries the free public GraphQL tier with no auth header at all. Its only
    # effect was printing "auth required" on /health/apis.html forever for a key
    # that unlocked nothing. Claim removed; the probe still reports whether the
    # endpoint the dashboard actually calls is serving.
    {"label": "Santiment",            "category": "Research",      "url": "https://api.santiment.net/graphql",                                                             "key_env": None},
    {"label": "SEC EDGAR",            "category": "AI News",       "url": "https://efts.sec.gov/LATEST/search-index?q=ai",                                                 "key_env": None, "headers": {"User-Agent": "BDT-Dashboards/1.0 (open-source dashboard; contact@bdt-dashboards.local)", "Accept": "application/json"}},
    # ---- summit (the standalone Snowflake Summit dashboard is static/baked —
    # no live upstream API; we probe the deployed page itself for "is it up") ----
    {"label": "Summit dashboard",     "category": "Summit",        "url": "https://btabiado.github.io/alpine-data/summit/",                                                 "key_env": None},
    # ---- city pulse (the City tab): Socrata 311/permits/crime per city, plus
    # Miami via ArcGIS + FBI CDE, and Census/BLS/AirNow context. Socrata's
    # keyless catalog endpoint is a cheap per-portal ping; SOCRATA_APP_TOKEN
    # only raises rate limits, so it's an optional key (200 keyless → "up").
    {"label": "Socrata · Chicago",    "category": "City",          "url": "https://data.cityofchicago.org/api/catalog/v1?limit=1",                                          "key_env": "SOCRATA_APP_TOKEN"},
    {"label": "Socrata · Los Angeles","category": "City",          "url": "https://data.lacity.org/api/catalog/v1?limit=1",                                                 "key_env": "SOCRATA_APP_TOKEN"},
    {"label": "Socrata · Seattle",    "category": "City",          "url": "https://data.seattle.gov/api/catalog/v1?limit=1",                                                "key_env": "SOCRATA_APP_TOKEN"},
    {"label": "Socrata · San Francisco","category": "City",        "url": "https://data.sfgov.org/api/catalog/v1?limit=1",                                                  "key_env": "SOCRATA_APP_TOKEN"},
    {"label": "Socrata · New York",   "category": "City",          "url": "https://data.cityofnewyork.us/api/catalog/v1?limit=1",                                           "key_env": "SOCRATA_APP_TOKEN"},
    {"label": "ArcGIS (Miami)",       "category": "City",          "url": "https://services.arcgis.com/8Pc9XBTAsYuxx9Ny/arcgis/rest/services/BuildingPermit_gdb/FeatureServer/0?f=json", "key_env": None},
    {"label": "FBI Crime Data Explorer","category": "City",        "url": "https://cde.ucr.cjis.gov/LATEST/agency/byStateAbbr/FL",                                          "key_env": "FBI_CDE_API_KEY"},
    # Probe the median-income variable metadata (the exact ACS field the City
    # context fetcher pulls) — small + keyless + fast. The /data.json discovery
    # doc is huge and trips the 8s timeout whenever Census is sluggish.
    {"label": "Census ACS",           "category": "City",          "url": "https://api.census.gov/data/2023/acs/acs5/variables/B19013_001E.json",                           "key_env": "CENSUS_API_KEY"},
    {"label": "BLS",                  "category": "City",          "url": "https://api.bls.gov/publicAPI/v2/timeseries/data/LNS14000000",                                   "key_env": "BLS_API_KEY"},
    {"label": "EPA AirNow",           "category": "City",          "url": "https://www.airnowapi.org/aq/observation/latLong/current/?format=application/json&latitude=40&longitude=-74&distance=25&API_KEY=", "key_env": "AIRNOW_API_KEY"},
    # ---- aviation: OpenSky live ADS-B. Anonymous access works (rate-limited);
    # OPENSKY_CLIENT_ID only raises limits. Tiny bbox keeps the probe cheap.
    {"label": "OpenSky Network",      "category": "Aviation",      "url": "https://opensky-network.org/api/states/all?lamin=45.8&lomin=5.9&lamax=46.0&lomax=6.1",            "key_env": "OPENSKY_CLIENT_ID"},
    # TSA daily throughput (powers the TSA Throughput sub-view via fetch_tsa.py).
    # tsa.gov 403s a non-browser UA, so send a real browser UA like the scraper.
    {"label": "TSA passenger volumes","category": "Aviation",      "url": "https://www.tsa.gov/travel/passenger-volumes",                                                   "key_env": None, "headers": {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"}},
    # FRED ENPLANE — the air-travel enplanements series behind the Air Travel
    # sub-view. Use the FRED *API* host (api.stlouisfed.org, same reliable host
    # as the Macro FRED probe) rather than the web fredgraph.csv endpoint, which
    # Akamai tarpits for non-curl clients from CI. Keyed on FRED_API_KEY:
    # auth_required without a key, up in CI where the secret is set.
    {"label": "FRED ENPLANE (air travel)","category": "Aviation",  "url": "https://api.stlouisfed.org/fred/series/observations?series_id=ENPLANE&file_type=json&limit=1",     "key_env": "FRED_API_KEY"},
    # AOPA Air Safety Institute (McSpadden Report) — the GA accident/rate source
    # behind the Safety sub-view. Browser UA; annual data, probed for reachability.
    {"label": "AOPA ASI (GA safety)",  "category": "Aviation",      "url": "https://www.aopa.org/training-and-safety/air-safety-institute/accident-analysis/richard-g-mcspadden-report", "key_env": None, "headers": {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"}},
    # ---- real estate: Zillow + Redfin keyless CSVs, Census gazetteer for metro
    # coords. These are large files; the probe GET reads one byte then closes.
    {"label": "Zillow Research",      "category": "Real Estate",   "url": "https://files.zillowstatic.com/research/public_csvs/zhvi/Metro_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv", "key_env": None},
    {"label": "Redfin Data Center",   "category": "Real Estate",   "url": "https://redfin-public-data.s3.us-west-2.amazonaws.com/redfin_market_tracker/redfin_metro_market_tracker.tsv000.gz", "key_env": None},
    {"label": "Census Gazetteer",     "category": "Real Estate",   "url": "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/2024_Gaz_cbsa_national.zip", "key_env": None},
    # ---- metals (FRED + Yahoo are shared with crypto/macro and probed above):
    # IMF SDMX central-bank gold holdings, USGS ScienceBase mine production.
    {"label": "IMF SDMX",             "category": "Metals",        "url": "https://api.imf.org/external/sdmx/2.1/dataflow/IMF.STA/IRFCL",                                   "key_env": None},
    # ScienceBase is finicky — send a browser UA + Accept. (It periodically drops
    # connections host-wide; a "down" here usually means the metals USGS fetch
    # is failing too, not that this probe is misconfigured.)
    {"label": "USGS ScienceBase",     "category": "Metals",        "url": "https://www.sciencebase.gov/catalog/item/65b7d7b2d34e36a39045b4c8?format=json",                  "key_env": None, "headers": {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15", "Accept": "application/json"}},
    # ---- supplies: Port of L.A. TEU scrape, NY Fed GSCPI csv (FRED above).
    # Probe a fixed historical-year page (permanent once published) rather than
    # the current year, whose page may not exist yet early in a new year.
    {"label": "Port of Los Angeles",  "category": "Supplies",      "url": "https://portoflosangeles.org/business/statistics/container-statistics/historical-teu-statistics-2024", "key_env": None, "headers": {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"}},
    {"label": "NY Fed GSCPI",         "category": "Supplies",      "url": "https://www.newyorkfed.org/medialibrary/research/interactives/data/gscpi/gscpi_interactive_data.csv", "key_env": None},
    # ---- UAP / MUFON: NUFORC sightings scrape. The historical mirror lives on
    # raw.githubusercontent.com, already covered via the ETF-mirror host above.
    {"label": "NUFORC",               "category": "UAP",           "url": "https://nuforc.org/subndx/",                                                                     "key_env": None},
    # ---- travel: State Dept advisories — probe the RSS feed (lighter than the
    # HTML table). Akamai-fronted, so send a browser UA to avoid a bot 403.
    {"label": "State Dept advisories","category": "Travel",        "url": "https://travel.state.gov/_res/rss/TAsTWs.xml",                                                   "key_env": None, "headers": {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"}},
]


# Every distinct env var the targets above name. Exported so the wiring test
# (and anyone auditing the workflow) can enumerate the promise this file makes
# without re-deriving it from TARGETS by hand — a hand-copied list is exactly
# how the workflow's env block fell seven names behind in the first place.
KEY_ENVS: list[str] = sorted({t["key_env"] for t in TARGETS if t.get("key_env")})

# The three states a named key can be in, from the probe process's point of view.
KEY_SET = "set"              # var present and non-empty — the secret reached us
KEY_UNSET = "unset"          # var present but empty — wired, secret not configured
KEY_NOT_WIRED = "not_wired"  # var absent entirely — nobody handed it to this process


def key_state(key_env: str | None) -> str | None:
    """Classify a named key into set / unset / not_wired. Never reads a value.

    This is the fix for the defect that made /health/apis.html unfalsifiable: a
    source whose key was never plumbed through and a source whose secret is
    genuinely missing both rendered as a flat "no key", so a page full of "no
    key" tags could mean "you never set these" OR "we never asked for them" and
    there was no way to tell which. They are different problems with different
    owners — one is the user's to fix in repo settings, the other is a workflow
    bug — and reporting them identically is how the workflow bug survived.

    The observable that separates them is *membership*, not truthiness. GitHub
    Actions materialises ``FOO: ${{ secrets.FOO }}`` as an env var set to the
    EMPTY STRING when the secret does not exist, so:

        "FOO" in os.environ  and  value == ""   ->  wired, secret not set
        "FOO" not in os.environ                 ->  NOT wired: a plumbing bug

    Outside CI (a plain shell, a test runner) nothing is exported, so almost
    everything reads ``not_wired``. That is still the honest statement — "this
    process was not given the key" — and the snapshot's ``ci`` flag lets a
    consumer say so in different words. Whitespace-only counts as unset, matching
    scripts/check_secrets_present.py, so a stray-newline paste is not "set".

    Returns None for a keyless source (there is nothing to classify).
    """
    if not key_env:
        return None
    if key_env not in os.environ:
        return KEY_NOT_WIRED
    return KEY_SET if (os.environ[key_env] or "").strip() else KEY_UNSET


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
    # Key-gated sources commonly answer a *keyless* probe with 400 (missing
    # api_key/params), 401, or 403 — all mean "alive, just needs a key", so
    # surface them as auth_required rather than blocked/degraded.
    if needs_key and status in (400, 401, 403):
        return "auth_required"
    if status in (401, 403):
        return "blocked"
    return "degraded"


def _probe_one(target: dict, timeout: float, attempts: int = PROBE_ATTEMPTS) -> dict:
    url = target["url"]
    key_env = target.get("key_env")
    needs_key = bool(key_env)
    # Default UA for all probes; a target may override/extend via "headers"
    # (e.g. SEC EDGAR requires a polite contact UA + Accept:application/json
    # per SEC fair-access rules, mirroring fetch_market.py's SEC_HEADERS —
    # without it EDGAR returns 403/500).
    headers = {"User-Agent": _UA}
    headers.update(target.get("headers") or {})
    t0 = time.monotonic()
    status: int | None = None
    note = ""
    retried = 0
    # Probe with one retry. GitHub Actions runners intermittently get slow DNS /
    # TLS handshakes (and the odd connection reset) to gov/city hosts, producing
    # transient timeouts that aren't real outages — the "down" cast rotates run to
    # run. Retry only connection-level failures (no HTTP response); an HTTPError
    # is a genuine server reply, so take it as-is and stop.
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
                status = r.status
                # Drain a little so keep-alive sockets close cleanly; ignore body.
                r.read(1)
            note = ""
            break
        except urllib.error.HTTPError as e:
            status = e.code
            note = (e.reason or "")[:80]
            break
        except Exception as e:  # URLError, timeout, ssl, etc. — transient, retry
            note = f"{type(e).__name__}: {e}"[:120]
            retried = attempt + 1
    latency_ms = int((time.monotonic() - t0) * 1000)
    verdict = _verdict(status, needs_key)
    kstate = key_state(key_env)
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
        # key_present is kept (older snapshots and any external reader depend on
        # it) but it is now derived from key_state, and it is deliberately NOT
        # the whole truth: False covers both "unset" and "not_wired". Consumers
        # that want to tell a settings problem from a plumbing bug must read
        # key_state. health/index.html renders all three distinctly.
        "key_present": (kstate == KEY_SET) if key_env else None,
        "key_state": kstate,
        # Named so the page can say WHICH variable is missing instead of making
        # the reader guess. A name, never a value.
        "key_env": key_env,
        "retries": retried,
        "note": note,
    }


def probe_all(timeout: float = DEFAULT_TIMEOUT, max_workers: int = 12) -> dict:
    """Probe every target in parallel. Returns a JSON-ready snapshot dict."""
    with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
        sources = list(ex.map(lambda t: _probe_one(t, timeout), TARGETS))
    sources.sort(key=lambda s: (s["category"], s["label"]))

    def count(*verdicts: str) -> int:
        return sum(1 for s in sources if s["verdict"] in verdicts)

    def keys(state: str) -> int:
        return sum(1 for s in sources if s["key_state"] == state)

    summary = {
        "total": len(sources),
        "up": count("up"),
        "auth_required": count("auth_required"),
        "rate_limited": count("rate_limited"),
        "degraded": count("degraded"),
        "blocked": count("blocked"),
        "down": count("down"),
        "reachable": sum(1 for s in sources if s["reachable"]),
        # Key accounting, so "how many of my keys actually arrived?" is answerable
        # from the summary alone. keys_not_wired > 0 is a WORKFLOW bug, not a
        # missing secret — see key_state().
        "keyed": sum(1 for s in sources if s["needs_key"]),
        "keys_set": keys(KEY_SET),
        "keys_unset": keys(KEY_UNSET),
        "keys_not_wired": keys(KEY_NOT_WIRED),
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "timeout_s": timeout,
        # Whether this snapshot came from a CI runner. Off a runner, "not_wired"
        # just means the operator's shell did not export the key, which is
        # unremarkable; on a runner it means the workflow forgot to map it.
        "ci": bool(os.environ.get("GITHUB_ACTIONS")),
        # Names only — never values. Lets the page and the CI log say exactly
        # which variable never arrived instead of "some key is missing".
        "unwired_key_envs": sorted(
            {s["key_env"] for s in sources if s["key_state"] == KEY_NOT_WIRED}
        ),
        "summary": summary,
        "sources": sources,
    }


# ---- TTL cache for the server endpoint ----------------------------------
# Probing every target host on every page load would be slow and rude to upstreams, so
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
    key_tag = {KEY_SET: " [key set]", KEY_UNSET: " [no key]",
               KEY_NOT_WIRED: " [KEY NOT WIRED]"}
    for src in snapshot["sources"]:
        st = src["status"] if src["status"] is not None else "—"
        print(f"  {src['verdict']:<13} {str(st):>4} {src['latency_ms']:>5}ms  "
              f"{src['label']}{key_tag.get(src['key_state'], '')}")
    # Shout about unwired keys. This is a defect in whatever launched us, and it
    # is invisible in the per-row table if you are skimming: every unwired source
    # otherwise looks exactly like a source whose secret you never set. On a
    # runner it is emitted as a ::warning so it lands on the run summary too.
    unwired = snapshot["unwired_key_envs"]
    if unwired:
        names = ", ".join(unwired)
        if snapshot["ci"]:
            print(f"::warning title=API key not wired into the probe::"
                  f"{len(unwired)} key(s) named by api_status.py never reached "
                  f"this step: {names}. /health/apis.html cannot tell these apart "
                  f"from unset secrets. Add them to the step's env: block.")
        print(f"\n  !! {len(unwired)} named key(s) NOT WIRED into this process: "
              f"{names}")
        print("     These are reported as 'not wired', NOT as 'no key' — the "
              "difference is\n     a workflow plumbing bug vs. an unset "
              "repository secret.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
