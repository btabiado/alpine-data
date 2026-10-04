# Sources — the fetcher inventory (the "tables")

Each `fetch_*.py` (and a couple of library modules) is a "source loader": it pulls one or more upstream APIs and writes a sidecar. Treat the **output file** as the table and the **fetcher** as its ETL job.

> Freshness field is **not uniform** — most use `generated_at`, some use `as_of`, `fetched_at`, or `ts` (epoch). The column below names the one each writes.

| Fetcher | Upstream source(s) | Output file(s) | Freshness field | Schedule (workflow) | Error handling |
|---------|--------------------|----------------|-----------------|---------------------|----------------|
| `fetch_market.py` | CoinGecko, OKX, Deribit (DVOL), Alternative.me (Fear&Greed), blockchain.info, Coinbase, CoinDesk CADLI, Binance Futures, mempool.space, +optional CryptoCompare/FRED/Glassnode/Etherscan/CoinMetrics/Reddit, SEC EDGAR. Orchestrates `fetch_coinbase`, `fetch_money_flows`, `fetch_stock_money_flow` via `fetch_all()` | `data/market.json`, `data/whale.json`, `data/defi.json`, several root `data-*.json` | `generated_at` | Hourly + on-push (`pages.yml`, V1 build) | Per-source try/except; CI caches last-good `data/market.json`; builder falls back to on-disk data if the whole fetch crashes |
| `fetch_cpi.py` | FRED (CPIAUCSL, CPILFESL, PCEPI, food/energy/housing/health/auto, ~35 series) | `v2/data-cpi.json` → served as `data-cpi.json` | `generated_at` | V2 build step (`pages.yml`); needs `FRED_API_KEY` | No key → writes `{fred_available:false, series:[]}`; per-series try/except; prior file preserved if all fail |
| `fetch_metals.py` | FRED (London Gold PM fix), Yahoo (`GC=F`, `SI=F` futures), IMF SDMX (central-bank gold), USGS ScienceBase (mine production) | `v2/data-metals.json` + dual-write `data-metals.json` (V1) | `generated_at` / `as_of` | V2 build step | Per-source try/except; on a source failure the **prior good value is preserved** (never blanked); file written once at end |
| `fetch_supplies.py` | Port of LA TEU (HTML scrape), FRED ISRATIO (optional), NY Fed GSCPI CSV | `v2/data-supplies.json` → `data-supplies.json` | `generated_at` | V2 build step | Per-feed try/except; 24 h freshness guard skips work if prior output fresh |
| `fetch_city.py` | Socrata (Chicago/LA/Seattle/SF/NYC 311·permits·crime), ArcGIS (Miami), +optional Census ACS / BLS / EPA AirNow / FBI CDE | `data-city.json` (committed baseline) | `generated_at` | Daily 06:00 UTC (`city-daily.yml`) commits; V1 reads on cold checkout | Per-feed try/except; 24 h freshness guard; Layer-B context stays null until keys present |
| `fetch_opensky.py` | OpenSky Network `states/all` (OAuth optional) | `data-opensky.json` (summary), `data-opensky-positions.json` (≤4000 positions) | `generated_at` / `ts` | Hourly (`aviation-opensky.yml`) commits | Retry once on connection error; on failure keeps prior sidecar + seed fallback; keyless tier is rate-limited (see gotchas) |
| `fetch_tsa.py` | TSA daily throughput (tsa.gov HTML table) | `data-tsa.json` | `generated_at` / `as_of` | Daily 14:10 UTC (`aviation-tsa.yml`); seed fallback | On fetch/parse failure exits non-zero **without** overwriting (stale-keep); stdlib only |
| `fetch_mufon.py` | NUFORC monthly scrape (2014+) + planetsig/ufo-reports mirror (1906–2014) | `v2/data-mufon.json` (committed carve-out) → `data-mufon.json` | `generated_at`, `date_range` | V2 build step (no dedicated cron) | Per-month cache `data/.stale/nuforc_subndx_YYYYMM.json`; `always_refresh={today,prior}`; merges historical on failure. ⚠️ **cache is gitignored → CI re-fetches all ~240 months → V2 build times out** (see gotchas) |
| `fetch_advisories.py` | State Dept advisories (per-country HTML) + RSS bulletins | `v2/data-travel.json` (committed carve-out) → `data-travel.json` | `generated_at` | V2 build step | On scrape failure reads existing file, logs, exits non-zero without overwriting; wrapped in `app.py` so it can't kill the build |
| `fetch_coinbase.py` | Coinbase Exchange (spot) + Coinbase Intl (perps: funding/OI/mark) | `data/coinbase_spot.json`, `data/coinbase_perps.json` | `fetched_at` (epoch) | Via `fetch_market.fetch_all()` (hourly) | Per-venue try/except; skips failing asset/perp |
| `fetch_equity_etf_flows.py` | Yahoo `quoteSummary` (crumb-gated: sharesOut+NAV for SPY/DIA/QQQ); fallback Nasdaq quote (MarketCap/price) | `data/equity_etf_flows.csv` (committed, accumulated), `data-equity-etf-flows.json` | `as_of` (date) | Daily 08:30 UTC (`money-flow-daily.yml`) commits CSV | Yahoo 429 → Nasdaq fallback; both fail → keep last-good CSV + warn. Needs ≥2 days for a delta |
| `fetch_money_flows.py` | ICI weekly MMF summary `.xls`, ICI weekly equity-flows `.xls` | `data-mmf.json`, `data-mf-flows.json` (regenerated, not committed) | `generated_at` | Via `fetch_market.fetch_all()` (hourly) | Per-series try/except; neutral dict on failure; preserves prior file if every series fails. ICI is weekly (Wed) → forward-filled |
| `fetch_stock_money_flow.py` | Yahoo chart OHLCV for ~180 S&P500/Nasdaq-100/Dow constituents; computes MFI(14)+CMF(20) | `data-stock-money-flow.json` (committed carve-out) | `as_of` | Via `fetch_market.fetch_all()` (V1 build) | Caps `_MAX_UNIVERSE=180` / `_MAX_WORKERS=6`; **no retry on 429** (deliberate); prior file preserved on total failure |
| `fetch_stock_prices.py` | Yahoo `/v8/finance/chart` per-ticker hourly OHLC (7d/1h), tickers from `market.json` stocks | `v2/data-stock-prices.json` | `generated_at` | V2 build step | Per-ticker try/except; 200 ms inter-call delay; partial-update keeps prior data for failing tickers. ⚠️ currently starved by the MUFON timeout (see gotchas) |
| `fetch_live.py` | Farside mirror CSV (BTC-ETF flows); +optional CoinGlass | `data/btc_flows.csv`, `data/eth_flows.csv` | — (manual-paste workflow) | Not run by CI (committed CSVs are the contract) | GitHub raw mirror always available; CoinGlass/SoSoValue optional |
| `money_flow.py` | (library, not a fetcher) computes MFI/CMF/OBV + the composite index from widened OHLCV | returns into `market.json` `money_flow` block | — | called during V1 build | See `references/metrics.md` |
| `signals.py` | (library) computes the −100…+100 asset signal | returns into `market.json` per-asset `signal` | — | called during V1 build | See `references/metrics.md` |
| `insights.py` | (library) heuristic cross-tab insight rules + history | `insights` block | — | called during V1 build | history persisted to disk |
| `api_status.py` | Probes all ~50 upstream endpoints (53 live, 19 categories) | `data/health/api_status.json` (mirror `health/apis.html`) | `generated_at` | `pages.yml` probe step (hourly) | stdlib only; `max_workers=12`; one retry on connection-level fail; per-target verdict up/auth_required/rate_limited/degraded/down |

## Workflows (the schedulers)

| Workflow | Trigger | Does |
|----------|---------|------|
| `pages.yml` | hourly + push to `main` | fetch market → build V1 (`/`) → validate JS → build V2 (`/v2/`) → validate → build Summit/Landscape → probe api_status → deploy `_site` to Pages |
| `city-daily.yml` | cron 06:00 UTC | `fetch_city.py` → commit `data-city.json` |
| `money-flow-daily.yml` | cron 08:30 UTC | `fetch_equity_etf_flows.py` → append + commit `data/equity_etf_flows.csv` |
| `real-estate-daily.yml` | cron (daily) | refresh + commit `data/real_estate.json`, `data/metro_coords.json` |
| `aviation-opensky.yml` | cron hourly | `fetch_opensky.py` → commit `data-opensky*.json` |
| `aviation-tsa.yml` | cron 14:10 UTC | `fetch_tsa.py` → commit `data-tsa.json` |
| `lthcs-*.yml` (many) | various crons | separate LTHCS pipeline (`lthcs_daily.py`); see `README_LTHCS.md` |
| `codeql.yml`, `security-audit.yml` (OSV), `trufflehog-weekly.yml`, `tests.yml` | push/PR/cron | security + test gates |

**Key insight:** crons that **commit** their sidecar (city, money-flow, real-estate, aviation) keep the repo file reasonably fresh; feeds refreshed **only inside the Pages build** (CPI/metals/supplies/travel/MUFON/stock-prices) are fresh **on live** but their committed copy may be a stale fallback.
