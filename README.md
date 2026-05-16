# Crypto Trading Dashboard

[![tests](https://github.com/btabiado/btc-eth-etf-dashboard/actions/workflows/tests.yml/badge.svg)](https://github.com/btabiado/btc-eth-etf-dashboard/actions/workflows/tests.yml)

Local, live web dashboard for actively monitoring BTC, ETH, LINK, and the broader crypto market. Ten tabs (left → right):

1. **AI News** — RSS-aggregated AI headlines with sentiment, AI-exposed stock signals, AI VC funding KPIs + top-funded companies (Wikipedia infobox enriched), and a live **SEC EDGAR Form D** feed of recent AI-adjacent private placements (keyless).
2. **Crypto** *(default tab)* — sortable top 25 by market cap with sparklines, 1h/24h/7d/30d %, trending coins, global stats, news + insights above the sentiment block.
3. **Crypto Signals** — transparent rules-based composite score (−100…+100) per asset with full component breakdown. Top-25 grouped by bucket (Strong Buy → Strong Sell), per-coin signal box + 90-day history chart, plus a 90-day breadth chart. Not investment advice.
4. **Whale Activity** — BTC on-chain proxies, mining pool concentration, Lightning Network, difficulty adjustment. BTC/ETH switcher with a separate ETH panel (24h EIP-1559 burn, largest tx, ERC-20/721 activity, supply, **ETH Whale Sentiment Index** parallel to BTC's, and an optional 90-day ETH blocks/day chart from Etherscan) plus a dedicated **ETH whale-tx feed** (top 10 recent transactions ≥$1M USD via Blockchair). Whale Alerts feed scans mempool.space for BTC transactions ≥$1M in the latest block. A **multi-chain whale snapshot** beneath the BTC panel surfaces 24h network stats + largest single tx for LTC, BCH, and DOGE.
5. **Point of Control** — volume-weighted price levels for the **top 50 by market cap** across 30d / 90d / 180d windows. Cards are sorted by signal score (Strong Buy → Strong Sell) and filterable via chips. Click any card to open a full breakdown modal with the POC ladder, migration sparkline, and naked POCs; desktop view has a fullscreen toggle on the volume profile chart.
6. **Research** — Reddit subreddit stats, CryptoCompare social/news depth, Santiment daily active addresses, plus per-coin top-25 news sentiment with a click-to-expand article modal.
7. **DeFi** — KPI strip (total TVL, 24h change, top chain share, dominant category) above a chain selector for **Ethereum / Solana / Arbitrum / Base**. Selecting a chain swaps the panel below to that chain's TVL sparkline, protocols filtered to that chain, and the chain's share of the total. Stablecoin yields and 365-day TVL history per chain surface beneath the per-chain view. Payload is lazy-loaded on first tab-select (see [Performance](#performance)).
8. **ETF Flows** — daily/weekly/monthly/YoY net flows from US spot BTC and ETH ETFs, per-fund detail.
9. **Futures** — price, volume, funding rate, open interest, long/short ratio, implied vol (DVOL), Fear & Greed, dominance, ETH/BTC, live news feed. Naked POC overlays on the price chart; 30d POC drift sparkline in each POC card. Side-by-side **crowded longs / crowded shorts** tables built from Coinbase International Exchange perpetual funding rates (246 perps), and CoinDesk CADLI as the regulated reference index used in derivatives settlement. Perpetuals explainer is collapsible on mobile.
10. **Stocks** — signals for the top 50 most active US stocks via Yahoo Finance, grouped by signal bucket. Compact cards show symbol/name header, big colored score, label (STRONG BUY → STRONG SELL), price + change %, and 30d score sparkline. Click any card to open a modal with the full per-component breakdown (SMA, RSI(14), MACD, 5-day momentum, volume z-score, 50/200 cross). Also hosts the **Traditional Indices** strip (DOW / S&P 500 / NDX / VIX).

Plus: a rule-based **insights bar** populated across all 10 tabs (29 cross-tab rules + 7 AI News rules); a Claude-powered **Ask the data** chat dock (right side); **Symbol search** — type any ticker (BTC, NVDA, SOL, AAPL...) in the header search box → a universal modal shows the signal score, POC if available, recent news, and sentiment for that symbol. Any crypto symbol not already in the cached top-25 is fetched **LIVE from CryptoCompare** client-side so the lookup covers the long tail (SHIB, INJ, FET, ...). Stock tickers beyond the cached top-50 most-active fall back to a live client-side fetch from **Twelvedata** (free tier: 800 req/day; CORS `*`; user-supplied key stored locally in the browser) with **Alpha Vantage** as a secondary fallback — on first lookup of an uncached ticker the modal walks you through pasting a free API key. The signal is then computed client-side from SMA50/200, RSI(14), 5-day momentum, and the golden-cross axis. When the dashboard is running locally via `python server.py` (same-origin), the search box prefers the `/api/symbol/<symbol>` endpoint, which routes through Yahoo Finance server-side (no rate limit, full 6-component scorer); stock-shaped tickers (4-5 letter alpha) are resolved against Yahoo first to avoid hitting same-named meme/scam crypto tokens (e.g. GME). Optional HTTP Basic Auth, GitHub Pages mirror, Tailscale-ready.

> **Note:** The global BTC/ETH/LINK/LTC asset selector was removed from the header. The internal `state.asset` still defaults to `'btc'`, so the **ETF Flows** and **Futures** tabs are pinned to BTC.

All data sources are **free, no key required** for the core dashboard. Optional keys unlock additional depth (chat, macro overlay, true whale cohorts, social/news, Reddit subscriber counts, ETH whale series, paid ETF APIs) — see [Environment variables](#environment-variables) and [`docs/SETUP.md`](docs/SETUP.md).

For a stable public share-link host (your own subdomain over a named Cloudflare Tunnel), use the helper scripts in [`scripts/`](scripts/): `tunnel-status.sh` to diagnose, `tunnel-config.sh` to set up, `tunnel-up.sh` to run. Details in [`docs/SETUP.md`](docs/SETUP.md) §4.

## Data sources

- **Price + market cap**: CoinGecko (BTC/ETH/LINK price+vol+mcap, top 25 markets, trending, global stats)
- **Cross-exchange price**: CryptoCompare CCCAGG (BTC/ETH/LINK aggregate)
- **Coinbase data feeds the dashboard from three places:**
  - **Coinbase Exchange spot** (`api.exchange.coinbase.com`) — bid/ask, 24h range, 24h volume per asset (BTC/ETH/LINK/LTC). Used for the spot quote tiles and a cross-exchange price-divergence sanity check.
  - **Coinbase International Exchange perpetuals** (`api.international.coinbase.com`) — funding rate, mark price, open interest, and volume across all 246 PERP instruments. Surfaced in the **Futures** tab as the crowded longs / crowded shorts tables.
  - **CoinDesk CADLI** (`data-api.coindesk.com`) — manipulation-resistant daily OHLC index. CADLI is the regulated reference index used in derivatives settlement; shown in the Futures tab alongside the perp positioning view.
- **Derivatives**: OKX (funding rate, open interest, long/short ratio)
- **Options-implied vol**: Deribit DVOL (BTC, ETH)
- **Sentiment**: Alternative.me Fear & Greed
- **BTC on-chain**: blockchain.info charts (tx vol, hash rate, miners rev, active addresses), Blockchair (supplementary stats), bitinfocharts (rich list / distribution)
- **BTC network**: mempool.space (fees, hashrate, tip height, **difficulty adjustment**, **Lightning Network**, **mining pools**, latest-block scan for Whale Alerts ≥$1M)
- **ETH on-chain**: Etherscan v2 (gas oracle, ETH whale stats — burn, largest tx, ERC-20/721)
- **ETH large transactions**: Blockchair (top 10 recent whale txs ≥$1M USD, no key required)
- **Multi-chain whale snapshot**: Blockchair (LTC, BCH, DOGE 24h network stats + largest single tx, no key required)
- **Blockchair**: free public endpoints — used for BTC supplementary stats, ETH large transactions, and the LTC/BCH/DOGE multi-chain snapshot
- **US equities**: Yahoo Finance (top-20 most-active US stocks → daily OHLCV for the Stocks tab signal scores)
- **DeFi**: DeFiLlama (TVL by chain, top 25 protocols, top stablecoin yields, 365-day historical TVL across 4 chains)
- **News + social**: RSS from CoinDesk, Cointelegraph, Decrypt, The Block, Bitcoin Magazine (25 deduped headlines); CryptoCompare social/news depth (optional key); Reddit subreddit stats (optional OAuth, RSS-only fallback)
- **AI news + funding**: RSS from AI-focused outlets; **SEC EDGAR Form D** filings filtered to AI-adjacent issuers (last 60d, keyless); Wikipedia infobox enrichment for top-funded AI companies
- **Research metrics**: Santiment (daily active addresses, optional)
- **ETF flows**: Farside Investors via paste workflow + GitHub mirror fallback
- **Optional macro**: FRED — DXY, S&P 500, Gold, 10Y Treasury, M2 (needs free key)
- **Optional whale cohorts**: Glassnode (true exchange-flow series), Coin Metrics (ETH whale series), Etherscan (90-day ETH blocks/day chart)
- **Optional chat**: Anthropic API (Claude) — chat dock with live dashboard as context

## Quickstart

```bash
cd ~/btc-eth-etf-dashboard
python3 -m venv .venv
.venv/bin/pip install pandas requests lxml beautifulsoup4 flask
.venv/bin/python server.py
# → open http://127.0.0.1:8765/
```

**Daily startup recipe** (`dash-up` / `dash-status` / `dash-down` aliases): see [`docs/MORNING.md`](docs/MORNING.md).

The server auto-refreshes market + whale data every 30 minutes in the background. The browser polls `/api/data` every 60s for the freshest cached payload.

## Two ways to run

### A. Live web server (recommended)
```bash
.venv/bin/python server.py
```
Browse to **http://127.0.0.1:8765/** — bookmarkable, refreshes itself.

Endpoints:
| Method | Path | Purpose |
|---|---|---|
| GET  | `/` | dashboard HTML |
| GET  | `/api/data` | latest payload as JSON |
| POST | `/api/refresh` | force re-fetch market + whale |
| POST | `/api/seed-etf` | seed BTC ETF flows from GitHub mirror |
| POST | `/api/upload-csv?asset=btc\|eth` | import a pasted CSV/TSV |
| GET  | `/api/export/csv?series=<path>&from=<date>&to=<date>` | download a time-series as CSV |
| GET  | `/healthz` | status |

The CSV export route returns `text/csv` with a `Content-Disposition: attachment` header so a browser hit triggers a download. `series` is a dotted path into the live payload — whitelisted to: `btc.daily`, `eth.daily`, `market.{btc,eth,link}.price`, `market.{btc,eth}.funding`, `market.btc.dvol`, `market.fear_greed`, `market.fred.{dxy,sp500,gold,treasury_10y}`, and `whale.btc.{tx_volume_usd,tx_count,active_addresses,avg_tx_usd,miners_revenue_usd,hash_rate}`. The optional `from`/`to` query params filter inclusively on ISO date strings. Share-token holders can hit this route — it's read-only.

Env: `HOST=127.0.0.1`, `PORT=8765`, `REFRESH_MINUTES=30` (set 0 to disable). Full list in [Environment variables](#environment-variables).

### B. Static HTML (no server)
```bash
.venv/bin/python app.py --fetch-market   # refresh + write dashboard.html, open it
.venv/bin/python app.py --no-open        # rebuild from cache only (offline)
```

`python app.py --no-open` writes `dashboard.html` and one sidecar per key in `SIDECAR_KEYS` (currently `whale`, `defi`) — e.g. `data-whale.json`, `data-defi.json`. Add `--fetch-market` to refresh the cached payloads first.

## Performance

First paint dropped from ~3.2MB → ~2.4MB by splitting the two heaviest sub-payloads into lazy-loaded sidecars:

- `SIDECAR_KEYS = ("whale", "defi")` in [`app.py`](app.py) — listed keys are stripped out of the inline `DATA` blob at HTML-render time.
- The page fetches `/data-<name>.json` on first tab-select (whale on Whale Activity, defi on DeFi).
- In live-server mode, [`server.py`](server.py) serves these from `/data-<name>.json` and adds them to the read-only allowlist so share-token holders can hit them.
- In static / GitHub Pages mode, [`.github/workflows/pages.yml`](.github/workflows/pages.yml) globs `data-*.json` next to `dashboard.html` into `_site/` so new sidecar keys don't require a workflow edit.

## Getting ETF flow data

The ETF Flows tab is empty until you load data. Three options:

**1. One-click GitHub mirror** (BTC only, ~Jan 2024 → May 2025, Total column only)
- In the dashboard, click **"Seed BTC from GitHub mirror"**.
- Or `curl -X POST http://127.0.0.1:8765/api/seed-etf`.
- Source: [canadiancode/btc-etf-flows](https://github.com/canadiancode/btc-etf-flows). Community-maintained, **may be stale**.

**2. Paste from Farside** (freshest, manual)
- Visit [farside.co.uk/bitcoin-etf-flow-all-data/](https://farside.co.uk/bitcoin-etf-flow-all-data/) (Cloudflare lets your real browser through; it blocks scripts).
- Select the table, copy.
- Click **"Paste CSV…"** in the dashboard, paste, choose BTC or ETH, Import.
- Tab-separated also works (browser table copy-paste defaults to tabs).

**3. Paid API**
- `export SOSOVALUE_API_KEY=...` (SoSoValue Open API)
- `export COINGLASS_API_KEY=...` (CoinGlass v4)
- `python app.py --fetch` or `curl -X POST http://localhost:8765/api/refresh` (after wiring keys into your shell).

## Reality check on history

- BTC spot ETFs launched **2024-01-11** → max ~2.3y of flow history.
- ETH spot ETFs launched **2024-07-23** → max ~1.8y of flow history.
- CoinGecko free tier caps price/volume at **365 days**.
- OKX funding-rate history caps at **~93 days**.
- Deribit DVOL: **3+ years**. Alternative.me F&G: **3+ years**. blockchain.info: **3+ years**.

The 3Y range button just clips to whatever's loaded.

## Futures data sources

| KPI | Source | Auth | History |
|---|---|---|---|
| Spot price, 24h volume, market cap | CoinGecko | none | 365d |
| Funding rate | OKX `BTC-USDT-SWAP` / `ETH-USDT-SWAP` | none | ~93d |
| Open interest (USD) | OKX rubik | none | ~180d |
| Long/short account ratio | OKX rubik | none | ~180d |
| Implied vol (DVOL) | Deribit | none | 3y+ |
| Fear & Greed | Alternative.me | none | 3y+ |
| BTC.D, total mcap, ETH/BTC | CoinGecko global + ratio | none | snapshot / 365d |

Binance and Bybit are intentionally not used — both 451-block from many regions including this machine.

## Signals (BTC + ETH)

The Signals tab shows a transparent rules-based composite score from
**−100 (bearish)** to **+100 (bullish)** for each asset, with every
component visible so you can see exactly what's driving it.

**Not investment advice.** This is a structured indicator like an RSI or a
Glassnode signal — useful for discipline, not a recommendation.

| Component | Source | Range of contribution |
|---|---|---|
| Price vs SMA50 | CoinGecko price | ±20 |
| Price vs SMA200 | CoinGecko price | ±20 |
| RSI(14) | derived | ±15 (oversold/overbought) |
| MACD histogram sign | derived | ±10 |
| Funding rate | OKX | ±10 (contrarian: negative funding → buy) |
| Fear & Greed | Alternative.me | ±10 (contrarian: <30 → buy, >70 → sell) |
| ETF flow 7d | your CSVs | ±10 (skipped if data >14d stale) |
| DVOL z-score (30d) | Deribit | ±5 |

Classification:
| Score | Label |
|---|---|
| ≥ +50 | STRONG BUY |
| +20 to +49 | BUY |
| −19 to +19 | HOLD |
| −20 to −49 | SELL |
| ≤ −50 | STRONG SELL |

The 90-day signal history is plotted alongside price so you can see how
the indicator behaved through past regimes.

## Stock signals (top 50 most active)

The Stocks tab applies the same `−100 … +100` score idea to the 50 most active US stocks (by daily volume) pulled from Yahoo Finance, grouped on screen by signal bucket. Same five-bucket label scheme (STRONG BUY → STRONG SELL), different components — the crypto-specific inputs (funding, DVOL, F&G, ETF flows) don't exist for equities, so the score is built from price/volume only:

| Component | Source | Notes |
|---|---|---|
| Price vs SMA50 | derived from Yahoo daily | trend filter |
| Price vs SMA200 | derived from Yahoo daily | trend filter |
| RSI(14) | derived | overbought/oversold |
| MACD histogram sign | derived | momentum direction |
| 5-day momentum | derived | short-term acceleration |
| Volume z-score | derived | unusual participation flag |
| 50/200 cross | derived | golden / death cross state |

Cards are sorted Strong Buy → Strong Sell. Each compact card shows the symbol/name header, the colored score, the label, current price + change %, and a 30-day score sparkline. Click any card to open a modal with the full component breakdown.

Above the card grid, a **90-day signal breadth chart** stacks the count of stocks in each bucket (STRONG BUY → STRONG SELL) per day, so you can see at a glance how the population of signals has rotated over the last three months. Green bars expanding from the bottom mean buys are accumulating; red bars expanding from the top mean sells are taking over. The Crypto Signals tab carries a matching **90-day breadth chart** built from the top-25 markets — same five buckets, same stacked shape, same colour key.

Both breadth charts answer "is the market shifting toward more buys or more sells?" at a portfolio level without forcing you to scan dozens of individual cards.

Not investment advice — same caveat as the crypto signal.

## Environment variables

All optional. Core dashboard runs with none of these set; the dashboard surfaces a `key_set: false` flag or falls back to a free path where applicable.

| Variable | Used in | Effect when unset |
|---|---|---|
| `ANTHROPIC_API_KEY` | `chat.py` | Chat dock disabled |
| `CHAT_MODEL` | `chat.py` | Defaults to `claude-haiku-4-5-20251001` |
| `FRED_API_KEY` | `fetch_market.py` | Macro overlay (DXY, S&P 500, Gold, 10Y, M2) hidden |
| `GLASSNODE_API_KEY` | `fetch_market.py` | True whale-cohort metrics off; free on-chain proxies still shown |
| `CRYPTOCOMPARE_API_KEY` | `fetch_market.py` | Social/news depth limited to anonymous tier; also used for the top-25 historical daily OHLCV that feeds the Point of Control tab (free tier works, key raises the rate limit) |
| `COINMETRICS_API_KEY` | `fetch_market.py` | ETH whale series omitted from Whale tab |
| `ETHERSCAN_API_KEY` | `fetch_market.py` | 90-day ETH blocks-per-day chart on the Whale tab hidden; gas oracle still works (separate keyless endpoint) |
| `REDDIT_CLIENT_ID` + `REDDIT_CLIENT_SECRET` | `fetch_market.py` | Reddit subscriber counts unavailable; public dashboard falls back to RSS post titles only |
| `COINGLASS_API_KEY` | `fetch_live.py` | CoinGlass v4 ETF-flow path disabled |
| `SOSOVALUE_API_KEY` | `fetch_live.py` | SoSoValue ETF-flow path disabled |
| `DASH_USER` + `DASH_PASS` | `server.py` | HTTP Basic Auth disabled (server is open on bound interface) |
| `HOST` | `server.py` | Defaults to `127.0.0.1` |
| `PORT` | `server.py` | Defaults to `8765` |
| `REFRESH_MINUTES` | `server.py` | Defaults to `30`; set `0` to disable background refresh |
| `SHARE_HOST` | `share.py` | Defaults to `http://127.0.0.1:8765` for share-link generation |

## Tests

```bash
.venv/bin/python -m pytest tests/ -v   # 283 tests, ~2s
```

The suite covers `app.py`, `signals.py`, `server.py` (Flask test client), `chat.py`, `fetch_market.py`, `shares.py`, `insights.py`, `wiki_enrich.py`, CSV export, sidecar split, and FRED/Farside/EDGAR parsers.
All tests use `tmp_path` and monkeypatched `DATA_DIR` — production CSVs are
never touched. Network is mocked for fetcher tests.

## Whale activity (BTC + ETH)

True whale exchange-flow series (Glassnode / CryptoQuant / CoinMetrics Pro) require paid keys. Free BTC proxies:

- **Avg tx value (USD)** = `tx_volume_usd / tx_count` — rises when whales move large amounts
- **On-chain tx value (USD)** — daily $ value moved on-chain
- **Active addresses** — usage breadth
- **Hash rate** — miner commitment
- **Miners revenue (USD)** — block reward + fees
- **Output volume (BTC)** — total BTC moved per day

Source: blockchain.info `/charts/...`, supplemented by Blockchair and bitinfocharts.

**ETH panel** (BTC/ETH switcher in the Whale tab): 24h EIP-1559 burn, largest tx, ERC-20/721 activity, supply. Etherscan v2 (free) covers the basics; `COINMETRICS_API_KEY` unlocks the historical ETH whale series. ETH side now lists top 10 recent whale transactions (≥$1M USD) via Blockchair, in addition to the existing largest-24h tx.

**Multi-chain whale snapshot** below the BTC panel shows 24h network stats + largest single tx for LTC, BCH, DOGE (Blockchair, no key required).

**Whale Alerts feed**: continuous scan of the latest mempool.space block for individual transactions ≥$1M.

## CSV schema (for paste / manual edit)

Wide format, USD millions, negative = outflow. A `Total` column is optional;
if missing, it's computed from the other numeric columns.

```
data/btc_flows.csv
  date,IBIT,FBTC,BITB,ARKB,BTCO,EZBC,BRRR,HODL,BTCW,GBTC,BTC,Total

data/eth_flows.csv
  date,ETHA,FETH,ETHW,CETH,ETHV,QETH,EZET,ETHE,ETH,Total
```

Columns can be added/removed freely — whatever's there gets aggregated.

## Files

```
server.py         Flask web server (live mode) + sidecar serving
app.py            CSV + JSON loader → aggregator → HTML generator (SIDECAR_KEYS split)
signals.py        Composite BTC/ETH/top-25/stocks signal indicator
insights.py       Rule-based cross-tab insights bar (29 + 7 AI rules)
fetch_market.py   Free trading + whale + AI-news + EDGAR Form D fetcher
fetch_live.py     ETF-flow fetcher (GitHub mirror fallback / SoSoValue / CoinGlass)
wiki_enrich.py    Wikipedia infobox enrichment for top-funded AI companies
chat.py           Claude-powered "Ask the data" chat dock
share.py          Read-only share-link minting
tests/            pytest suite (283 tests)
data/
  btc_flows.csv   daily BTC ETF flows
  eth_flows.csv   daily ETH ETF flows
  market.json     cached trading data (generated)
  whale.json      cached whale proxies (generated)
  ai_curated.json curated AI-funding seed list (Wikipedia-enriched at build)
dashboard.html    static-mode HTML output (open in browser)
data-whale.json   lazy-loaded whale sidecar (generated alongside dashboard.html)
data-defi.json    lazy-loaded DeFi sidecar (generated alongside dashboard.html)
```

## Running headless

```bash
HOST=0.0.0.0 PORT=8765 REFRESH_MINUTES=30 .venv/bin/python server.py
```

Behind a reverse proxy / launchd / systemd. With `HOST=0.0.0.0` it's reachable from your LAN — set `DASH_USER` + `DASH_PASS` for HTTP Basic Auth before exposing it on any untrusted network.

## Auto-start on macOS (launchd)

```bash
cat > ~/Library/LaunchAgents/com.user.etfdash.plist <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.user.etfdash</string>
<key>ProgramArguments</key>
  <array>
    <string>/Users/bryantabiadon/btc-eth-etf-dashboard/.venv/bin/python</string>
    <string>/Users/bryantabiadon/btc-eth-etf-dashboard/server.py</string>
  </array>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><true/>
<key>StandardOutPath</key><string>/tmp/etfdash.out</string>
<key>StandardErrorPath</key><string>/tmp/etfdash.err</string>
</dict></plist>
PLIST
launchctl load ~/Library/LaunchAgents/com.user.etfdash.plist
```
