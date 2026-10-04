# Data hygiene & standing gotchas

Read this before trusting any number. Ordered by how often it bites.

---

### 1. ⭐ Committed sidecars are cold placeholders — trust the LIVE Pages copy
The repo commits many sidecars as fallbacks/baselines, then regenerates them on every deploy. A repo file can be empty/stale while production is full and current.
- `data-cpi.json` (151 B, `fred_available:false`) · `data-stock-money-flow.json` (90 B, `scored_count:0`) · `data-whale.json` (198 B, `sentiment:null`) — **all healthy on live.**
- **Always** confirm against `https://btabiado.github.io/alpine-data/<file>` before declaring a feed broken.

### 2. MUFON/UAP "as of" ≠ today
`fetch_mufon.py` anchors `date_range` to the **newest entry in the fetched data**, not the current date. A scrape that times out (or falls back to the 1906–2014 historical mirror) can show an old range even when the run is current. Don't read the UAP date as "data freshness."

### 3. ⚠️ V2 build can time out → MUFON stale + stock-prices 404 (known issue, fix in flight)
`fetch_mufon.py` re-fetches ~240 NUFORC months from scratch each CI run because its per-month cache `data/.stale/nuforc_subndx_YYYYMM.json` is **gitignored**. The V2 build step (`pages.yml`, 4-min cap, `continue-on-error:true`) gets killed mid-backfill, so:
- `data-mufon.json` on live drifts stale (only Bryan's local rebuilds refresh it), and
- `fetch_stock_prices.py` (runs after MUFON in `v2/app.py`) is **starved → `data-stock-prices.json` 404s** → the V2 per-ticker sparkline is silently dead.
- The failure is **masked by `continue-on-error:true`** (run shows green).
**Fix:** commit the NUFORC month-cache via a `.gitignore` carve-out (`data/.stale/*` + `!data/.stale/nuforc_subndx_*.json`) so only `{today, prior}` months refetch and the build finishes in seconds. *(A P0+P1 fix swarm for exactly this is in progress as of 2026-06-09.)*

### 4. CPI / macro needs `FRED_API_KEY`
Without the key, `fetch_cpi.py` writes `{fred_available:false, series:[]}` and the CPI tab shows a "key needed" empty state. The key **is set in repo Actions secrets** (confirmed working in CI), so **live** CPI is populated even though the committed `data-cpi.json` looks empty. `fetch_metals.py` falls back FRED→Yahoo so metals render without the key.

### 5. Never retry Yahoo on a 429
`fetch_stock_money_flow.py` is **deliberately hardcoded to not retry rate-limits** — retrying multiplies calls and deepens the IP ban (observed: 219 tickers × retries → 0 scored, full ban). Mitigations: `_MAX_UNIVERSE=180`, `_MAX_WORKERS=6`, 200 ms jitter; prior file preserved on total failure. **Empty stock flows usually = Yahoo throttling, not a code bug.** `fetch_equity_etf_flows.py` hits the same wall and falls back Yahoo→Nasdaq.

### 6. OpenSky throttles GitHub-Actions IPs
Free-tier OpenSky (~400 req/day) flags datacenter IPs aggressively; `aviation-opensky.yml` (hourly) sees intermittent failures. The fetcher keeps the prior sidecar + a seed fallback, so it **self-heals** within ~1–2 h. Set `OPENSKY_CLIENT_ID/SECRET` (OAuth) to raise the limit. Not data-wrong, just occasional hourly gaps.

### 7. DeFiLlama bridges = 402 paywall (standing item)
`bridges.llama.fi` returns HTTP 402 from CI (`api_status.py` probe → `degraded`). The bridges sub-view stays empty; not an outage. USGS ScienceBase (metals) is similarly flaky (503) and covered by per-source last-good preservation.

### 8. ICI fund flows are weekly → forward-filled
ICI MMF + equity-flow `.xls` release weekly (Wed). The Money Flow Index needs a daily value, so `fetch_money_flows.py` snapshots first-print data and forward-fills through the week (no double-count). A flat MMF/MF leg mid-week is expected, not stale.

### 9. ETF-flow leg of the Money Flow Index warms up ~1 trading day
`net_flow = ΔsharesOut × NAV` needs ≥ 2 daily snapshots. On a cold start (or first deploy after the CSV is reset) the ETF leg is neutral until the second daily run lands; the headline renormalizes around the missing leg meanwhile.

### 10. `data/equity_etf_flows.csv` history is load-bearing
The daily-delta ETF metric needs the **prior-day row**. The CSV must stay committed and accumulating (`money-flow-daily.yml` is its keeper). A cold checkout with no prior CSV → empty flow series on day 1.

### 11. A 404 sidecar fails silently
The client treats a missing sidecar as an empty/loading state (no console error). A *dead* feature (e.g. `data-stock-prices.json` 404) won't show an error — **verify by curling the file**, not by watching the console.

### 12. V1 vs V2 frontends are ~86–93% duplicated
`app.py` and `v2/app.py` share the Python data layer but copy-paste the JS frontend. The same UI/escaping fix usually must land in **both**. Several functions have already drifted. (Source-of-truth is the builder; never edit the built `dashboard.html`/`v2/dashboard.html`.)

### 13. Alpha Vantage NEWS_SENTIMENT is AND-not-OR
A multi-ticker AV news filter returns only articles mentioning **all** tickers (intersection, not union) — it defeats batched multi-ticker calls. This is why the LTHCS thesis leg stays neutral in the V1 daily pipeline rather than relying on batched AV sentiment.

### 14. Finnhub thesis kept neutral (50)
Finnhub `/news-sentiment` free tier is unreliable, so the thesis value is pinned neutral in the V1 cron (`--skip-thesis`). Don't read a 50 thesis as a computed signal — it's a deliberate hold. (EODHD was planned as a paid replacement.)

### 15. City pulse has data-continuity breaks
`fetch_city.py:METHODOLOGY_DISCLOSURES` documents portal/records-system changes (Seattle PD 2019, SF 2018 migration, Chicago ~7-day crime exclusion window, LA dataset rotation). A sudden pulse jump can be a **data artifact**, not a real-world trend.

### 16. api_status verdicts: `auth_required` ≠ down
A keyed source without its env var still probes and returns 401/403 → verdict `auth_required` (endpoint is *live*, just gated). Live summary (2026-06-09): 53 sources, 42 up, 8 auth_required, 1 rate_limited, 2 degraded, 0 down. Zillow/Redfin are probed by GETting **one byte** (liveness, not freshness).

---

## When something looks wrong — triage order
1. **Curl the live sidecar** (rule #1). Repo file ≠ live.
2. **Check `api_status.json`** for the upstream's verdict (degraded/rate_limited/auth_required).
3. **Check the freshness field** (`generated_at`/`as_of`/`ts`) on the *live* copy vs the feed's cadence (`references/sources.md`).
4. **Check the relevant cron's last runs** (`gh run list --workflow=<name>.yml`) for silent failures — many steps are `continue-on-error`.
5. **Only then** read the fetcher source.
