# Sidecar catalog — the `data-*.json` "tables"

Each row is a sidecar file: which tab consumes it, whether it is committed to git or generated, its rough size, and whether the **committed** copy is real data or a placeholder. **Live size/freshness ≠ repo size/freshness** — see `gotchas.md` and the "trust LIVE" rule.

> "Committed?" = is there a `!data-X.json` carve-out in `.gitignore` (the blanket rule `data-*.json` ignores everything by default).

| File | Tab / feature | Committed? | Repo size | Repo = real or placeholder? |
|------|---------------|-----------|-----------|------------------------------|
| `data-mufon.json` | UAP / MUFON | ✅ carve-out (`!data-mufon.json`) | ~106 KB | Real (last-good); refreshed in V2 build |
| `data-city.json` | City Pulse (V1) | ✅ carve-out | ~67 KB | Real; daily cron commit |
| `data-travel.json` | Travel Advisories | ✅ carve-out | ~78 KB | Real (last-good fallback for a once-broken parser) |
| `data-aviation.json` | Aviation (used-market/seed) | ✅ carve-out | ~25 KB | Real (static seed) |
| `data-tsa.json` | Aviation → TSA | ✅ carve-out | ~1.5 KB | Real |
| `data-opensky.json` | Aviation → Live summary | ✅ carve-out | ~0.5 KB | Real (hourly) |
| `data-opensky-positions.json` | Aviation → Flight Map | ✅ carve-out | ~205 KB | Real (≤4000 positions) |
| `data-stock-money-flow.json` | Stocks → Money Flow | ✅ carve-out | **90 B** | ⚠️ **Placeholder** (`scored_count:0`); live has ~13 stocks |
| `data-metals.json` | Metals (also V1 mirror) | partial (dual-write) | ~203 KB | Real |
| `data-us_states.json` | Real Estate heat map | ✅ committed (static geometry) | ~121 KB | Real (SVG paths; never changes) |
| `data-cpi.json` | CPI (V2) | ❌ gitignored | **151 B** | ⚠️ **Placeholder** (`fred_available:false`); live has 22 series |
| `data-whale.json` | Whale Activity | ❌ gitignored | **198 B** | ⚠️ **Placeholder** (`sentiment:null`); live full |
| `data-supplies.json` | Supplies (V2) | ❌ gitignored | ~56 KB | Generated each build |
| `data-mmf.json` | Money Flow Index (MMF leg) | ❌ gitignored | small | Generated each build |
| `data-mf-flows.json` | Money Flow Index (equity-MF leg) | ❌ gitignored | small | Generated each build |
| `data-stock-prices.json` (V2) | Stocks → per-ticker sparkline | ❌ gitignored | — | ⚠️ Currently **404 on live** (starved by MUFON timeout — see gotchas) |
| `data/market.json` | Core crypto/markets (most of V1) | ❌ gitignored | large | Generated each build (CI-cached last-good) |
| `data/whale.json`, `data/defi.json` | Whale / DeFi tabs | ❌ gitignored | varies | Generated each build |
| `data/equity_etf_flows.csv` | Money Flow Index (ETF leg) | ✅ committed | grows | **Load-bearing history** — deltas need the prior row |
| `data/btc_flows.csv`, `data/eth_flows.csv` | ETF Flows tab | ✅ committed | small | Manual-paste workflow |
| `data/real_estate.json`, `data/metro_coords.json` | Real Estate | ✅ committed | varies | Real; daily cron |
| `data/health/api_status.json` | `/health/` (`apis.html`) | ❌ gitignored | ~10 KB | Generated each build |
| `data/lthcs/*` | LTHCS tab (separate pipeline) | ✅ committed snapshots | varies | See `README_LTHCS.md` |

## The lazy-load contract

- `app.py` maps tab → sidecar via `SIDECAR_FOR_TAB`; the client `loadSidecar()`s the JSON on first tab selection.
- The `SIDECARS` manifest **only lists files with non-empty content** — an empty `{fred_available:false, series:[]}` may be omitted from the manifest so the client never `fetch()`es a guaranteed-empty file (avoids spurious 404s). That is intentional, not a bug.
- A sidecar that 404s is treated by the client as an empty/loading state (no JS error). So a *dead feature* (e.g. `data-stock-prices.json` 404) fails **silently** — verify by curling the file, not by watching the console.

## Quick placeholder check

```bash
for f in data-cpi.json data-whale.json data-stock-money-flow.json data-stock-prices.json; do
  printf '%-28s repo=%-5s  live=' "$f" "$(wc -c < ~/alpine-data/$f 2>/dev/null || echo NA)"
  curl -s -o /dev/null -w '%{http_code} %{size_download}B\n' "https://btabiado.github.io/alpine-data/$f"
done
```
