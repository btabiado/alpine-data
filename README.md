# BDT Dashboards

[![tests](https://github.com/btabiado/alpine-data/actions/workflows/tests.yml/badge.svg)](https://github.com/btabiado/alpine-data/actions/workflows/tests.yml)

Crypto, markets and macro dashboards, published as a static site on GitHub Pages (<https://btabiado.github.io/alpine-data/>). Scheduled GitHub Actions workflows fetch the data from free public sources and commit it to this repo, and `pages.yml` rebuilds the site from those files. The same dashboard can also run locally as a live Flask server (`server.py`).

The tab strip has 22 buttons: four menus, two direct tabs and the Summit launcher. The `data-tab` entries in `app.py` are the source of truth for the set and order.

| Menu | Tabs |
|---|---|
| Crypto | Overview, Signals, Whale, Point of Control, DeFi, ETF Flows, Futures |
| Markets | Stocks, Money Flow, Stock Flows, Research |
| Macro | CPI, Supplies, Metals, Real Estate |
| Explore | Travel Advisories, UAP, City, Aviation |
| Direct | LTHCS, AI News |
| Launcher | Summit (opens the Competitive Landscape site) |

What the main tabs show:

- **Overview**: sortable top 25 by market cap with sparklines, 1h/24h/7d/30d %, trending coins, global stats, and news and insights above the sentiment block.
- **Signals**: a transparent rules-based composite score (−100…+100) per asset with the full component breakdown, top 25 grouped by bucket (Strong Buy → Strong Sell), a per-coin 90-day history chart and a 90-day breadth chart. Not investment advice.
- **Whale**:
  - BTC on-chain proxies, mining-pool concentration, Lightning Network and the difficulty adjustment.
  - A BTC/ETH switcher. The ETH panel has the 24h EIP-1559 burn, largest tx, ERC-20/721 activity, supply and the ETH Whale Sentiment Index.
  - An ETH whale-tx feed (via Blockchair) and a Whale Alerts scan of the latest mempool.space block.
  - A multi-chain snapshot for LTC, BCH and DOGE.
- **Point of Control**: volume-weighted price levels for the top 50 by market cap over 30d / 90d / 180d windows, sorted by signal score, with a per-coin breakdown modal.
- **DeFi**: TVL KPIs and a per-chain view (Ethereum / Solana / Arbitrum / Base), stablecoin yields and 365-day TVL history. Lazy-loaded (see [Performance](#performance)).
- **ETF Flows**: daily, weekly, monthly and YoY net flows for US spot BTC and ETH ETFs, with per-fund detail.
- **Futures**:
  - Price, volume, funding, open interest, long/short ratio, implied vol (DVOL), Fear & Greed, dominance and ETH/BTC.
  - Crowded longs / crowded shorts tables from Coinbase International Exchange perpetual funding rates.
  - The Alpine Large-Cap Crypto Index (see [Data sources](#data-sources)).
- **Stocks**: signals for the top 50 most active US stocks via Yahoo Finance, with a per-stock component modal and the Traditional Indices strip (DOW / S&P 500 / NDX / VIX).
- **Money Flow / Stock Flows**: the Money Flow Index composite from ETF share-count flows and ICI fund flows (`SPEC_money_flow_index.md`), and per-stock MFI and Chaikin Money Flow for index constituents.
- **Research**: Reddit subreddit stats, CoinGecko community and GitHub developer stats, and headline sentiment scored by Alpine Data's own keyword rule.
- **CPI, Supplies, Metals, Real Estate**:
  - CPI and PCE inflation from FRED.
  - Supply-chain indicators: Port of L.A. TEU, the inventory-to-sales ratio and the NY Fed GSCPI.
  - Gold and silver.
  - A top-50 US metro housing snapshot from Zillow, Redfin and FRED.
- **Travel Advisories, UAP, City, Aviation**:
  - U.S. State Department advisories.
  - A NUFORC sightings map.
  - City Pulse scores for six US cities.
  - FAA registry data, OpenSky live traffic and TSA checkpoint throughput.
- **LTHCS**: a summary of the Long-Term Hold Confidence Score. The full pages are at `/lthcs/` (see [`README_LTHCS.md`](README_LTHCS.md)).
- **AI News**: RSS AI headlines with sentiment, AI-exposed stock signals, AI VC funding KPIs, and a keyless SEC EDGAR Form D feed of AI-adjacent private placements.

Also on the dashboard:

- A rule-based **insights bar**.
- A Claude-powered **Ask the data** chat dock. Under `server.py` it uses the server's `ANTHROPIC_API_KEY`. On the public site, the visitor pastes their own key, which stays in their browser.
- **Symbol search**:
  - Crypto symbols outside the cached top 25 are fetched live in the browser from CoinGecko, falling back to Coinbase, Kraken and Binance.US daily candles.
  - Stock tickers outside the cached top 50 use Twelvedata, with Alpha Vantage as the fallback. Both take a free key that the visitor pastes, and it is stored only in their browser.
  - Under `python server.py`, the search box uses the server's `/api/symbol/<symbol>` (Yahoo Finance) first.

> **Note:** The global BTC/ETH/LINK/LTC asset selector was removed from the header. The internal `state.asset` still defaults to `'btc'`, so the **ETF Flows** and **Futures** tabs are pinned to BTC.

All data sources are **free, no key required** for the core dashboard. Optional keys unlock additional depth (chat, macro overlay, true whale cohorts, Reddit subscriber counts, ETH whale series) — see [Environment variables](#environment-variables) and [`docs/SETUP.md`](docs/SETUP.md).

For a stable public share-link host (your own subdomain over a named Cloudflare Tunnel), use the helper scripts in [`scripts/`](scripts/): `tunnel-status.sh` to diagnose, `tunnel-config.sh` to set up, `tunnel-up.sh` to run. Details in [`docs/SETUP.md`](docs/SETUP.md) §4.

## Data sources

- **Price + market cap**: CoinGecko (BTC/ETH/LINK price+vol+mcap, top 25 markets, trending, global stats)
- **Top-50 daily closes + volume** (Point of Control table, signal-breadth chart): CoinGecko `market_chart` (aggregate USD volume) with keyless Coinbase → Kraken → Binance.US candle fallbacks; complete UTC days only, cached per coin per UTC day so the CoinGecko Demo budget is spent at most once a day per coin. Each coin records which source it came from; a fallback is labelled as exchange-only volume.
- **Coinbase data feeds the dashboard from these places:**
  - **Coinbase Exchange spot** (`api.exchange.coinbase.com`) — bid/ask, 24h range, 24h volume per asset (BTC/ETH/LINK/LTC). Used for the spot quote tiles and a cross-exchange price-divergence sanity check.
  - **Coinbase International Exchange perpetuals** (`api.international.coinbase.com`) — funding rate, mark price, open interest, and volume across all 246 PERP instruments. Surfaced in the **Futures** tab as the crowded longs / crowded shorts tables.
  - **Coinbase Exchange daily candles** — the first keyless fallback for the top-50 daily series (and the browser's "look up any crypto") when CoinGecko has no series for a coin.
- **Alpine Large-Cap Crypto Index** (Futures tab) — Alpine Data's own market-cap-weighted index of the 10 largest eligible coins (stablecoins, wrapped/staked tokens and tokenized real-world assets excluded), computed from CoinGecko daily closes and market caps, 100 = first day of the 90-day window, reconstituted monthly; method, constituents, changes and uncomputed days ship with the payload. It replaced the CoinDesk CADLI chart (CADLI is CoinDesk's proprietary index and its API needs a paid key since 2026-10) and is not CADLI.
- **Derivatives**: OKX (funding rate, open interest, long/short ratio)
- **Options-implied vol**: Deribit DVOL (BTC, ETH)
- **Sentiment**: Alternative.me Fear & Greed
- **BTC on-chain**: blockchain.info charts (tx vol, hash rate, miners rev, active addresses), Blockchair (supplementary stats), bitinfocharts (rich list / distribution)
- **BTC network**: mempool.space (fees, hashrate, tip height, **difficulty adjustment**, **Lightning Network**, **mining pools**, latest-block scan for Whale Alerts ≥$1M)
- **ETH on-chain**: Etherscan v2 (gas oracle, ETH whale stats — burn, largest tx, ERC-20/721)
- **ETH large transactions**: Blockchair (top 10 recent whale txs ≥$1M USD, no key required)
- **Multi-chain whale snapshot**: Blockchair (LTC, BCH, DOGE 24h network stats + largest single tx, no key required)
- **Blockchair**: free public endpoints — used for BTC supplementary stats, ETH large transactions, and the LTC/BCH/DOGE multi-chain snapshot
- **US equities**: Yahoo Finance (top-50 most-active US stocks → daily OHLCV for the Stocks tab signal scores)
- **DeFi**: DeFiLlama (TVL by chain, top 25 protocols, top stablecoin yields, 365-day historical TVL across 4 chains)
- **News + social**: RSS from CoinDesk, Cointelegraph, Decrypt, The Block, Bitcoin Magazine (deduped headlines); Google News RSS per coin, scored by Alpine Data's own committed keyword rule (no vendor labels, no LLM); CoinGecko community counts + GitHub REST repo stats; Reddit subreddit stats (optional OAuth, RSS-only fallback)
- **AI news + funding**: RSS from AI-focused outlets; **SEC EDGAR Form D** filings filtered to AI-adjacent issuers (last 60d, keyless); Wikipedia infobox enrichment for top-funded AI companies
- **Research metrics**: Santiment (daily active addresses, optional)
- **ETF flows**: Farside Investors, scraped daily in CI by `scripts/fetch_etf_flows.py` (see [Getting ETF flow data](#getting-etf-flow-data))
- **Optional macro**: FRED — DXY, S&P 500, Gold, 10Y Treasury, M2 (needs free key)
- **Optional whale cohorts**: Glassnode (true exchange-flow series), Coin Metrics (ETH whale series), Etherscan (90-day ETH blocks/day chart)
- **Optional chat**: Anthropic API (Claude) — chat dock with live dashboard as context

## Quickstart

```bash
cd alpine-data
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python server.py
# → open http://127.0.0.1:8765/
```

`requirements.txt` is what the pipelines need. `requirements-dev.txt` adds pytest and the packages for the local tools (`server.py`, the LTHCS MCP server).

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

The committed CSVs are kept current automatically; you can also paste a table by hand. Two options:

**1. Paste from Farside** (manual)
- Visit [farside.co.uk/bitcoin-etf-flow-all-data/](https://farside.co.uk/bitcoin-etf-flow-all-data/) (Cloudflare lets your real browser through; it blocks scripts).
- Select the table, copy.
- Click **"Paste CSV…"** in the dashboard, paste, choose BTC or ETH, Import.
- Tab-separated also works (browser table copy-paste defaults to tabs).

**2. Automatic (CI)** — this is what actually keeps the committed CSVs current
- [`scripts/fetch_etf_flows.py`](scripts/fetch_etf_flows.py) scrapes Farside from a GitHub Actions runner on its own cron ([`etf-flows-daily.yml`](.github/workflows/etf-flows-daily.yml)) and commits `data/btc_flows.csv` + `data/eth_flows.csv`. Keyless.
- There is no paid-API option any more, and the old one-click "Seed BTC" button (the abandoned canadiancode/btc-etf-flows mirror, last row 2025-05-02) and `fetch_live.py` were removed in 2026-10. The CoinGlass and SoSoValue integrations in `fetch_live.py` had been removed on 2026-08-03: nothing ever invoked them (`app.py --fetch` was their only caller and no workflow passes `--fetch`), and `api.sosovalue.com` no longer resolves. `COINGLASS_API_KEY` and `SOSOVALUE_API_KEY` are retired and reach no code.

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

All optional. Core dashboard runs with none of these set; the dashboard surfaces a `key_set: false` flag or falls back to a free path where applicable. [`.env.example`](.env.example) lists every key the code reads, including the LTHCS and City ones; copy it to `.env` for local runs.

| Variable | Used in | Effect when unset |
|---|---|---|
| `ANTHROPIC_API_KEY` | `chat.py` | Chat dock disabled |
| `CHAT_MODEL` | `chat.py` | Defaults to `claude-haiku-4-5-20251001` |
| `FRED_API_KEY` | `fetch_market.py` | Macro overlay (DXY, S&P 500, Gold, 10Y, M2) hidden |
| `GLASSNODE_API_KEY` | `fetch_market.py` | True whale-cohort metrics off; free on-chain proxies still shown |
| `COINGECKO_API_KEY` | `fetch_market.py`, `lthcs/sources/crypto_data.py` | Demo key (sent only to api.coingecko.com). Unset = keyless CoinGecko (~5-30 calls/min), so the daily top-50 sweep falls back to exchange candles more often |
| `GITHUB_TOKEN` | `fetch_market.py` (Actions token) | Sent only to api.github.com for the Research tab's repo stats; unset = anonymous GitHub API (60 requests/hour per IP) |
| `COINMETRICS_API_KEY` | `fetch_market.py` | ETH whale series omitted from Whale tab |
| `ETHERSCAN_API_KEY` | `fetch_market.py` | 90-day ETH blocks-per-day chart on the Whale tab hidden; gas oracle still works (separate keyless endpoint) |
| `REDDIT_CLIENT_ID` + `REDDIT_CLIENT_SECRET` | `fetch_market.py` | Reddit subscriber counts unavailable; public dashboard falls back to RSS post titles only |
| `DASH_USER` + `DASH_PASS` | `server.py` | HTTP Basic Auth disabled (server is open on bound interface) |
| `HOST` | `server.py` | Defaults to `127.0.0.1` |
| `PORT` | `server.py` | Defaults to `8765` |
| `REFRESH_MINUTES` | `server.py` | Defaults to `30`; set `0` to disable background refresh |
| `SHARE_HOST` | `share.py` | Defaults to `http://127.0.0.1:8765` for share-link generation |

Retired, read by no code: `CRYPTOCOMPARE_API_KEY` (every CryptoCompare/CoinDesk dependency, including the CADLI chart, moved to free sources in 2026-10), `COINGLASS_API_KEY` and `SOSOVALUE_API_KEY` (removed 2026-08-03).

## Tests

```bash
.venv/bin/python -m pytest tests/ -q   # about 4,100 tests, ~2 min
```

The suite covers the dashboard builder, the fetchers, the LTHCS pipeline (`tests/lthcs/`), the City tab, the health monitors and the workflows' own scripts. It must leave `git status` clean:

- Files are written under `tmp_path`.
- Real network access is blocked by `tests/conftest.py`. A test that needs the internet is marked `@pytest.mark.network` and is skipped in CI.

The tests that read the built `dashboard.html` skip until you run `python app.py --no-open`; CI builds it first. [`SKILL.md`](SKILL.md) has the exact CI-faithful sequence.

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
app.py            dashboard builder: data → dashboard.html + data-*.json sidecars
server.py         local Flask server (live mode), not deployed
fetch_*.py        per-source fetchers (market, CPI, metals, supplies, travel, UAP, City, aviation, ...)
signals.py        rules-based signal scores (crypto + stocks)
insights.py       insights bar rules
chat.py           "Ask the data" chat dock
city/             City tab sources and scoring
lthcs/            LTHCS scoring package; lthcs_daily.py runs it, lthcs_*/ are its pages
scripts/          workflow entry points and maintenance tools
tools/            validators for the built dashboard
tests/            pytest suite
data/             committed data (ETF-flow CSVs, data/lthcs/, ...); market.json / whale.json are generated
data-*.json       tab sidecars next to dashboard.html; some committed, the rest generated (see .gitignore)
.github/          workflows, and scripts/api-commit.sh for signed data commits
```

[`SKILL.md`](SKILL.md) describes the layout and conventions in more detail.

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
    <string>/path/to/alpine-data/.venv/bin/python</string>
    <string>/path/to/alpine-data/server.py</string>
  </array>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><true/>
<key>StandardOutPath</key><string>/tmp/etfdash.out</string>
<key>StandardErrorPath</key><string>/tmp/etfdash.err</string>
</dict></plist>
PLIST
launchctl load ~/Library/LaunchAgents/com.user.etfdash.plist
```
