# Crypto Trading Dashboard

[![tests](https://github.com/btabiado/btc-eth-etf-dashboard/actions/workflows/tests.yml/badge.svg)](https://github.com/btabiado/btc-eth-etf-dashboard/actions/workflows/tests.yml)

Local, live web dashboard for actively monitoring BTC, ETH, LINK, and the broader crypto market. Six tabs:

1. **ETF Flows** — daily/weekly/monthly/YoY net flows from US spot BTC and ETH ETFs, per-fund detail.
2. **Trading** — price, volume, funding rate, open interest, long/short ratio, implied vol (DVOL), Fear & Greed, dominance, ETH/BTC, live news feed.
3. **Signals** — transparent rules-based composite score (−100…+100) per asset with full component breakdown. Not investment advice.
4. **Markets** — sortable top 25 by market cap with sparklines, 1h/24h/7d/30d %, plus trending coins.
5. **DeFi** — TVL by chain, top 25 protocols, stablecoin yields, 365-day TVL history across Ethereum/Solana/Arbitrum/Base.
6. **Whale Activity** (BTC) — on-chain proxies, mining pool concentration, Lightning Network stats, difficulty adjustment.

Plus: insights bar (rule-based, ~12 live notifications), a Claude-powered **Ask the data** chat dock (right side), optional HTTP Basic Auth, GitHub Pages mirror, Tailscale-ready.

All data sources are **free, no key required** except the optional Anthropic chat (`ANTHROPIC_API_KEY`) and FRED macro overlay (`FRED_API_KEY`). See [`docs/SETUP.md`](docs/SETUP.md).

For a stable public share-link host (your own subdomain over a named Cloudflare Tunnel), use the helper scripts in [`scripts/`](scripts/): `tunnel-status.sh` to diagnose, `tunnel-config.sh` to set up, `tunnel-up.sh` to run. Details in [`docs/SETUP.md`](docs/SETUP.md) §4.

## Data sources (15 wired, all free)

- **Price + market cap**: CoinGecko (BTC/ETH/LINK price+vol+mcap, top 25 markets, trending, global stats)
- **Cross-exchange price**: CryptoCompare CCCAGG (BTC/ETH/LINK aggregate)
- **Derivatives**: OKX (funding rate, open interest, long/short ratio)
- **Options-implied vol**: Deribit DVOL (BTC, ETH)
- **Sentiment**: Alternative.me Fear & Greed
- **BTC on-chain**: blockchain.info charts (tx vol, hash rate, miners rev, active addresses)
- **BTC network**: mempool.space (fees, hashrate, tip height, **difficulty adjustment**, **Lightning Network**, **mining pools**)
- **ETH on-chain**: Etherscan v2 (gas oracle)
- **BTC index**: CoinDesk cadli (manipulation-resistant daily OHLC)
- **DeFi**: DeFiLlama (TVL by chain, top 25 protocols, top stablecoin yields, 365-day historical TVL across 4 chains)
- **News**: RSS from CoinDesk, Cointelegraph, Decrypt, The Block, Bitcoin Magazine (25 deduped headlines)
- **ETF flows**: Farside Investors via paste workflow + GitHub mirror fallback
- **Optional macro**: FRED — DXY, S&P 500, Gold, 10Y Treasury, M2 (needs free key)
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

Env: `HOST=127.0.0.1`, `PORT=8765`, `REFRESH_MINUTES=30` (set 0 to disable).

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

## Trading data sources

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

## Tests

```bash
.venv/bin/python -m pytest tests/ -v   # 39 tests, ~2s
```

The suite covers `app.py`, `signals.py`, and `server.py` (Flask test client).
All tests use `tmp_path` and monkeypatched `DATA_DIR` — production CSVs are
never touched. Network is mocked for fetcher tests.

## Whale activity (BTC)

True whale exchange-flow series (Glassnode / CryptoQuant / CoinMetrics Pro) require paid keys. Free proxies:

- **Avg tx value (USD)** = `tx_volume_usd / tx_count` — rises when whales move large amounts
- **On-chain tx value (USD)** — daily $ value moved on-chain
- **Active addresses** — usage breadth
- **Hash rate** — miner commitment
- **Miners revenue (USD)** — block reward + fees
- **Output volume (BTC)** — total BTC moved per day

Source: blockchain.info `/charts/...`. ETH-side proxies need Etherscan v2 API key — not yet wired.

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
tests/            pytest suite (39 tests)
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

Behind a reverse proxy / launchd / systemd. With `HOST=0.0.0.0` it's reachable from your LAN — only do that on a trusted network, the server has no auth.

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
