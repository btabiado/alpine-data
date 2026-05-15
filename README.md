# Crypto Trading Dashboard

[![tests](https://github.com/btabiado/btc-eth-etf-dashboard/actions/workflows/tests.yml/badge.svg)](https://github.com/btabiado/btc-eth-etf-dashboard/actions/workflows/tests.yml)

Local, live web dashboard for actively monitoring BTC, ETH, LINK, and the broader crypto market. Nine tabs:

1. **Crypto Overview** — sortable top 25 by market cap with sparklines, 1h/24h/7d/30d %, trending coins, global stats.
2. **Signals** — transparent rules-based composite score (−100…+100) per asset with full component breakdown. Not investment advice.
3. **Point of Control** — volume-weighted price levels for the top 25 by market cap across 30d / 90d / 180d windows, with naked POCs, value-area drift sparkline, and migration badges per coin.
4. **Research** — Reddit subreddit stats, CryptoCompare social/news depth, Santiment daily active addresses.
5. **DeFi** — TVL by chain, top 25 protocols, stablecoin yields, 365-day TVL history across Ethereum/Solana/Arbitrum/Base.
6. **Whale Activity** — BTC on-chain proxies, mining pool concentration, Lightning Network, difficulty adjustment. BTC/ETH switcher with a separate ETH panel (24h EIP-1559 burn, largest tx, ERC-20/721 activity, supply). Whale Alerts feed scans mempool.space for transactions ≥$1M in the latest block.
7. **ETF Flows** — daily/weekly/monthly/YoY net flows from US spot BTC and ETH ETFs, per-fund detail.
8. **Futures** — price, volume, funding rate, open interest, long/short ratio, implied vol (DVOL), Fear & Greed, dominance, ETH/BTC, live news feed. Naked POC overlays on the price chart; 30d POC drift sparkline in each POC card. Side-by-side **crowded longs / crowded shorts** tables built from Coinbase International Exchange perpetual funding rates (246 perps), and CoinDesk CADLI as the regulated reference index used in derivatives settlement.
9. **Stocks** — signals for the top 20 most-active US stocks via Yahoo Finance. Each card shows symbol/name header, big colored score, label (STRONG BUY → STRONG SELL), price + change %, 30d score sparkline, and a per-component breakdown (SMA, RSI(14), MACD, 5-day momentum, volume z-score, 50/200 cross). Sorted Strong Buy → Strong Sell.

Plus: insights bar (rule-based, ~12 live notifications), a Claude-powered **Ask the data** chat dock (right side), optional HTTP Basic Auth, GitHub Pages mirror, Tailscale-ready.

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
- **US equities**: Yahoo Finance (top-20 most-active US stocks → daily OHLCV for the Stocks tab signal scores)
- **DeFi**: DeFiLlama (TVL by chain, top 25 protocols, top stablecoin yields, 365-day historical TVL across 4 chains)
- **News + social**: RSS from CoinDesk, Cointelegraph, Decrypt, The Block, Bitcoin Magazine (25 deduped headlines); CryptoCompare social/news depth (optional key); Reddit subreddit stats (optional OAuth, RSS-only fallback)
- **Research metrics**: Santiment (daily active addresses, optional)
- **ETF flows**: Farside Investors via paste workflow + GitHub mirror fallback
- **Optional macro**: FRED — DXY, S&P 500, Gold, 10Y Treasury, M2 (needs free key)
- **Optional whale cohorts**: Glassnode (true exchange-flow series), Coin Metrics (ETH whale series)
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
.venv/bin/python app.py --no-open        # rebuild from cache only
```

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

## Stock signals (top 20 most active)

The Stocks tab applies the same `−100 … +100` score idea to the 20 most-active US stocks (by daily volume) pulled from Yahoo Finance. Same five-bucket label scheme (STRONG BUY → STRONG SELL), different components — the crypto-specific inputs (funding, DVOL, F&G, ETF flows) don't exist for equities, so the score is built from price/volume only:

| Component | Source | Notes |
|---|---|---|
| Price vs SMA50 | derived from Yahoo daily | trend filter |
| Price vs SMA200 | derived from Yahoo daily | trend filter |
| RSI(14) | derived | overbought/oversold |
| MACD histogram sign | derived | momentum direction |
| 5-day momentum | derived | short-term acceleration |
| Volume z-score | derived | unusual participation flag |
| 50/200 cross | derived | golden / death cross state |

Cards are sorted Strong Buy → Strong Sell. Each card shows the symbol/name header, the colored score, the label, current price + change %, a 30-day score sparkline, and the component breakdown.

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
.venv/bin/python -m pytest tests/ -v   # 111 tests, ~2s
```

The suite covers `app.py`, `signals.py`, `server.py` (Flask test client), `chat.py`, `fetch_market.py`, `shares.py`, `insights.py`, CSV export, and FRED/Farside parsers.
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

**ETH panel** (BTC/ETH switcher in the Whale tab): 24h EIP-1559 burn, largest tx, ERC-20/721 activity, supply. Etherscan v2 (free) covers the basics; `COINMETRICS_API_KEY` unlocks the historical ETH whale series.

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
server.py         Flask web server (live mode)
app.py            CSV + JSON loader → aggregator → HTML generator (static mode)
signals.py        Composite BTC/ETH signal indicator
fetch_market.py   Free trading + whale fetcher (CoinGecko/OKX/Deribit/blockchain.info)
fetch_live.py     ETF-flow fetcher (GitHub mirror fallback / SoSoValue / CoinGlass)
tests/            pytest suite (111 tests)
data/
  btc_flows.csv   daily BTC ETF flows
  eth_flows.csv   daily ETH ETF flows
  market.json     cached trading data (generated)
  whale.json      cached whale proxies (generated)
dashboard.html    static-mode output (open in browser)
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
