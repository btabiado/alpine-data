---
name: alpine-data-context
description: "Data-context reference for the alpine-data ('BDT Dashboards') project — a FILE-BASED 'data warehouse' where Python fetch_*.py scripts pull ~50 upstream APIs into JSON sidecars that two static-HTML builders (app.py = V1, v2/app.py = V2) render and serve via GitHub Pages. Use when analyzing, debugging, or extending alpine-data's data for: (1) tracing where a tab's data comes from and how fresh it is, (2) reading or deriving its metrics (Money Flow Index, MFI/CMF, ETF flows, signal scores, city pulse), (3) diagnosing empty/stale tabs and the live-vs-repo trap, or any question needing alpine-data-specific data context. NOT for a SQL warehouse — this 'warehouse' is JSON files on disk + GitHub Pages."
---

# Alpine-Data Data Context

`alpine-data` ("BDT Dashboards") has **no SQL warehouse**. Its "warehouse" is a **fetch → sidecar → build** pipeline of Python scripts and JSON files. This skill is the map of that data layer: where each datum comes from, how it is keyed, how it is computed, how fresh it is, and the standing gotchas an analyst must know.

- **Canonical repo:** `/Users/bryantabiadon/alpine-data` (GitHub `btabiado/alpine-data`, branch `main`)
- **Live site:** V1 at https://btabiado.github.io/alpine-data/ · V2 at https://btabiado.github.io/alpine-data/v2/ · also `/summit/` and `/landscape/`
- **UI source of truth:** `app.py` (V1, ~985 KB) and `v2/app.py` (V2, ~847 KB) — single-file builders whose inline `HTML_TEMPLATE` renders to the built artifacts `dashboard.html` / `v2/dashboard.html`. **Never hand-edit the built `.html`** — edit the builder and rebuild.
- **`btabiado.github.io/` root 404s** — the project lives under `/alpine-data/`.

---

## Architecture at a glance

```
upstream APIs (~50)        Python fetchers            JSON sidecars                static builders            GitHub Pages
─────────────────────  →   fetch_market.py        →   data/market.json,        →   app.py  (V1)          →   /        (V1)
CoinGecko, FRED, ICI,      fetch_cpi.py               data-*.json (root),          v2/app.py (V2)            /v2/     (V2)
Yahoo, OpenSky, NUFORC,    fetch_metals.py            data/*.json                  embed core data inline    /summit/
Socrata, State Dept, ...   fetch_money_flows.py       (one per feed/tab)           + lazy-load sidecars      /landscape/
                           ... (see references/sources.md)
                                   │                                                       │
                                   └── scheduled by GitHub Actions ──────────────────────┘
                                       pages.yml (hourly + on push) builds & deploys everything;
                                       per-tab crons (city/money-flow/real-estate/aviation) refresh
                                       individual sidecars and commit them back to the repo.
```

Two delivery modes for data inside the dashboard:
- **Inline at build time** — core/critical data is embedded directly in the HTML by the builder (the page is ~5 MB, ~96% inline JS+data).
- **Lazy sidecars at runtime** — heavier per-tab data lives in `data-*.json` files fetched on first tab selection. Look for `SIDECARS` / `loadSidecar` / `SIDECAR_FOR_TAB` in `app.py`. A tab maps to a sidecar; selecting it `fetch()`es that JSON from the deployed site.

There is **no `data.json`** at the site root (it 404s) — that is expected; data is either inline or in a named `data-*.json`.

---

## ⭐ The one rule that matters most: trust LIVE, not the repo file

Many sidecars are **committed to git as cold placeholders or last-known-good fallbacks**, then **regenerated fresh on every Pages deploy**. So the file in the repo can look empty or stale while production is fully populated and current.

- `data-cpi.json` in the repo: `{fred_available:false, series:[]}` (151 B). **Live:** `fred_available:true`, 22 series, regenerated each deploy.
- `data-stock-money-flow.json` repo: `scored_count:0` (90 B). **Live:** 13 stocks scored.
- `data-whale.json` repo: `sentiment:null` (198 B). **Live:** full BTC whale series.

**When a date or count looks wrong in the repo, check the deployed copy before concluding anything is broken:**
```bash
curl -s https://btabiado.github.io/alpine-data/data-cpi.json | python3 -m json.tool | head
```
The committed file is the API *contract / fallback*; the **live Pages copy is authoritative for current state.** (See `references/gotchas.md` for the full list of which files are placeholders vs real.)

---

## Access patterns (how to "query" this data)

There is no SQL. You "query" by reading JSON. Common patterns:

```bash
# 1. Read a LIVE sidecar (authoritative current state) and inspect with jq/python
curl -s https://btabiado.github.io/alpine-data/data-metals.json | jq '.generated_at, (.gold | keys)'
curl -s https://btabiado.github.io/alpine-data/data/health/api_status.json | jq '.summary'

# 2. Read the on-disk repo copy (fallback / last-known-good — may be stale; see the rule above)
jq '.generated_at' ~/alpine-data/data-metals.json

# 3. Check a feed's freshness field (varies: generated_at | as_of | fetched_at | ts)
curl -s https://btabiado.github.io/alpine-data/data-tsa.json | jq '{generated_at, as_of}'

# 4. Run a fetcher locally to regenerate a sidecar (needs the repo's venv + any API keys in .env)
cd ~/alpine-data && .venv/bin/python fetch_metals.py        # writes data-metals.json (+ v2/)
cd ~/alpine-data && .venv/bin/python api_status.py          # writes data/health/api_status.json

# 5. Rebuild a dashboard from source (renders the builder's HTML_TEMPLATE to the artifact)
cd ~/alpine-data && .venv/bin/python app.py --no-open       # -> dashboard.html (V1)
cd ~/alpine-data && .venv/bin/python v2/app.py --no-open    # -> v2/dashboard.html (V2)
```

Freshness timestamp field is **not uniform** — grep the file or see the per-source column in `references/sources.md`.

---

## Entity disambiguation

The word "flow" is heavily overloaded here. Clarify which one:

**"Flows" can mean:**
- **ETF flows (crypto)** — daily BTC/ETH spot-ETF net inflows/outflows. Source: Farside mirror + CoinGlass. Files: `data/btc_flows.csv`, `data/eth_flows.csv`. Tab: *ETF Flows*.
- **Equity ETF flows** — per-index (SPY/DIA/QQQ) `ΔSharesOutstanding × NAV`. File: `data/equity_etf_flows.csv`. Feeds the Money Flow Index.
- **Money flows (mutual funds)** — ICI weekly equity-fund + money-market-fund cash flows. Files: `data-mf-flows.json`, `data-mmf.json`.
- **Money Flow Index (MFX)** — the **composite ±100 gauge** built from the three above plus MFI/CMF. Tab: *Markets → Money Flow*. Built in `money_flow.py:build_money_flow_index`.
- **Stock Flows** — per-stock MFI/CMF score for index constituents. File: `data-stock-money-flow.json`. Tab: *Stocks → Money Flow*.

**Other overloaded terms:**
- **"Signal"** — the −100…+100 composite trade signal per asset (`signals.py`), NOT the same as the Money Flow Index.
- **"Whale"** — BTC on-chain large-holder proxies (`data-whale.json`), distinct from the "crowded longs/shorts" futures positioning.
- **"Sidecar" vs "tab"** — a *tab* is a UI panel (`<div id="tab-X">`); a *sidecar* is the `data-*.json` it lazy-loads. One tab → at most one sidecar (`SIDECAR_FOR_TAB`).
- **V1 vs V2** — two independent builders/sites (`app.py` → `/`, `v2/app.py` → `/v2/`). They share the **Python data layer** (V2 imports `fetch_market`, `signals`, `insights` from the repo root) but have **separately maintained frontends** (~86–93% duplicated JS). A data fix in a fetcher reaches both; a UI fix usually must be applied twice.

---

## Business terminology

| Term | Meaning | Notes / gotcha |
|------|---------|----------------|
| **Sidecar** | A `data-*.json` file a tab lazy-loads at runtime | Committed ones are fallbacks; trust live |
| **MFI** | Money Flow Index (14-bar), 0–100 | `money_flow.py:mfi`; >70 overbought, <30 oversold |
| **CMF** | Chaikin Money Flow (20-bar), −1…+1 | `money_flow.py:cmf` |
| **MFX / headline** | The ±100 Money Flow Index composite gauge | `money_flow.py:build_money_flow_index`; ETF leg warms up ~1 trading day |
| **ΔSO×NAV** | ETF net flow = (sharesOut_t − sharesOut_prev) × NAV_t | Needs ≥2 daily snapshots; `data/equity_etf_flows.csv` |
| **Signal / score** | −100…+100 weighted trade signal per asset | `signals.py`; weights SMA20/RSI15/MACD10/Funding10/FNG10/ETF10/DVOL5/VIX5 |
| **Pulse** | City health score, 50 = baseline | `city/pulse.py`; >50 favorable |
| **Thesis** | LTHCS long-term-hold confidence; also a per-asset sentiment leg | Kept **neutral (50)** in V1 daily pipeline (Finnhub free tier unreliable) |
| **api_status / health** | Reachability snapshot of all upstreams | `api_status.py` → `data/health/api_status.json`, mirror `health/apis.html` |
| **Carve-out** | A `!data-X.json` negation in `.gitignore` that commits one sidecar | Lets CI start from a baseline on a cold checkout |
| **LTHCS** | Long-Term Hold Confidence Score | Separate pipeline (`lthcs_daily.py`); V1-only; see `README_LTHCS.md` |

---

## Key metrics (summary — full detail in `references/metrics.md`)

| Metric | One-line formula | Where |
|--------|------------------|-------|
| MFI(14) | `100 − 100/(1 + ΣposMF/ΣnegMF)`, MF = typical-price × volume | `money_flow.py:113` |
| CMF(20) | `Σ(MFM×V)/ΣV`, MFM = ((C−L)−(H−C))/(H−L) | `money_flow.py:166` |
| Money Flow Index composite | per-index = mean(z(ETF flow), z(MFI−50)) → ±100; headline renormalizes + ICI MF + MMF cash | `money_flow.py:361` |
| ETF net flow | `(sharesOut_t − sharesOut_prev) × NAV_t` (USD millions) | `fetch_equity_etf_flows.py` |
| Asset signal | weighted blend of SMA/RSI/MACD/funding/FNG/ETF/DVOL/VIX, contrarian on funding & FNG | `signals.py:compute_signal` |
| City pulse | normalized trailing-12-mo trend per pillar, 50 baseline | `city/pulse.py` |

---

## Data freshness

| Feed / sidecar | Cadence | Refresh mechanism | Typical lag |
|----------------|---------|-------------------|-------------|
| Core crypto/market (`data/market.json`, signals, whale, defi, money-flows) | Hourly + on push | `pages.yml` runs `fetch_market.fetch_all()` during V1 build | ≤1 h |
| `api_status.json` | Hourly | `pages.yml` probe step after build | ≤1 h |
| City (`data-city.json`) | Daily 06:00 UTC | `city-daily.yml` commits the file | ≤24 h |
| Equity ETF flows (`equity_etf_flows.csv`) | Daily 08:30 UTC | `money-flow-daily.yml` appends + commits | 1 trading-day warm-up |
| Real estate (`data/real_estate.json`) | Daily | `real-estate-daily.yml` commits | ≤24 h |
| OpenSky (`data-opensky*.json`) | Hourly | `aviation-opensky.yml` commits; flaky on GH IPs | ~1–2 h (self-heals) |
| TSA (`data-tsa.json`) | Daily 14:10 UTC | `aviation-tsa.yml`; else baked seed | ≤24 h |
| CPI / Metals / Supplies / Travel / MUFON / Stock-prices | Each Pages deploy | **V2 build step** in `pages.yml` | ⚠️ MUFON/stock-prices currently flaky — see `references/gotchas.md` |

---

## Knowledge base navigation

| Reference | Use for |
|-----------|---------|
| `references/sources.md` | Per-fetcher inventory: upstream API, output file, freshness field, schedule, error handling (the "tables") |
| `references/sidecars.md` | Sidecar catalog: which `data-*.json` feeds which tab, committed-vs-gitignored, size, populated-vs-placeholder |
| `references/entities.md` | Entities & primary keys (crypto symbol, fund ticker, city slug, ICAO24, FRED series id, country, …) |
| `references/metrics.md` | Full metric derivations & formulas with file:function anchors |
| `references/gotchas.md` | Standing data-hygiene quirks — read before trusting any number |

---

## Common tasks

```bash
# Which tab is empty/broken? Compare repo vs live for its sidecar:
f=data-stock-money-flow.json
echo "repo:"; jq '{as_of, scored_count}' ~/alpine-data/$f 2>/dev/null
echo "live:"; curl -s https://btabiado.github.io/alpine-data/$f | jq '{as_of, scored_count}'

# What upstreams are degraded right now?
curl -s https://btabiado.github.io/alpine-data/data/health/api_status.json \
  | jq '.sources[] | select(.verdict!="up") | {label, category, verdict, note}'

# Trace a metric to its code:
grep -n "def build_money_flow_index" ~/alpine-data/money_flow.py
```

---

## Troubleshooting / common mistakes (top 5; full list in `references/gotchas.md`)

1. **Reading the repo file instead of live.** Committed sidecars are placeholders/fallbacks. Always confirm against the deployed Pages copy.
2. **Assuming MUFON/UAP "as of" = today.** `fetch_mufon.py` anchors the date range to the **newest entry on file**, not the current date; a timed-out scrape can show an old range.
3. **Expecting CPI/macro without a key.** CPI needs `FRED_API_KEY`; without it `fetch_cpi.py` writes an empty `{fred_available:false}` state.
4. **Retrying Yahoo on a 429.** `fetch_stock_money_flow.py` deliberately does **not** retry rate-limits (retrying deepens the IP ban). Empty stock flows usually = throttling, not a code bug.
5. **Fixing a UI bug in V1 only.** V1 and V2 frontends are ~86–93% duplicated; the same fix usually must land in both `app.py` and `v2/app.py`.
