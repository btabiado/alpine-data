# Entities & primary keys

The "dimensions" this data describes and the identifier you join/filter on. Unlike a warehouse there are no enforced foreign keys — the "join key" is whatever string both sidecars agree on (a ticker, a city slug, an ICAO24 hex, a FRED series id).

| Entity | Primary key / identifier | Cardinality | Source(s) | Notes |
|--------|--------------------------|-------------|-----------|-------|
| **Cryptocurrency** | `symbol` (BTC, ETH, SOL, LINK, …) | ~25–50 tracked | CoinGecko, Binance, OKX, Deribit, blockchain.info, Etherscan | UI `state.asset` toggles BTC/ETH; top-25 seeded at fetch; stablecoins excluded from top-20 signals via `signals._is_stable_symbol` (USD-prefix) |
| **Crypto spot ETF** | fund ticker (IBIT, FBTC, ETHA, FETH, …) | ~20 BTC / ~8 ETH | Farside mirror, CoinGlass; `fund_meta.py` | ETF Flows tab; history in `data/btc_flows.csv`, `data/eth_flows.csv` |
| **Equity index ETF** | ticker (SPY, DIA, QQQ) | 3 | Yahoo / Nasdaq | Drives Money Flow Index per-index legs; `data/equity_etf_flows.csv` |
| **Individual stock** | ticker (AAPL, NVDA, …) | ~180 (dashboard), 219 (LTHCS) | Yahoo (dashboard), Alpha Vantage (LTHCS) | Stocks tab MFI/CMF; universe shifts daily by most-active |
| **US city** | city slug (`chicago`,`la`,`seattle`,`sf`,`nyc`,`miami`) | 6 | Socrata (5), ArcGIS (Miami) | Registry `docs/city/city_registry.resolved.json`; per-pillar pulse |
| **US state / metro** | state abbrev / metro (CBSA) code | 50+DC / ~380 metros | Zillow, Redfin, Census | Heat map geometry `data-us_states.json`; centroids `data/metro_coords.json` |
| **Crypto futures instrument** | perp symbol (`BTC-USD-SWAP`, `ETH-USDT-PERP`, …) | ~246 (Coinbase Intl) | Coinbase Intl, OKX, Binance, Bybit, Deribit | Funding/OI/mark; crowded longs/shorts; DVOL for BTC/ETH |
| **Flight (live)** | `icao24` (hex) / callsign | ~hundreds–thousands airborne | OpenSky state vector | State-vector indices: `[0]`=icao24, `[1]`=callsign, `[5]`/`[6]`=lon/lat; map caps at 4000 |
| **UAP sighting** | `{date, state, city}` (+ optional shape) | ~145k–160k records (1906–present) | NUFORC + planetsig mirror | By-state heatmap, shape distribution; **time-anchored to newest entry on file, not today** |
| **Travel advisory** | country name | ~195 | State Dept HTML + RSS | Level 0–4; risk codes U/C/T/K/H/D/N |
| **Metal** | element symbol (AU, AG) + per-country CB holdings | 2 metals | FRED, Yahoo futures, IMF, USGS | Spot in USD/oz; CB gold in tonnes; mine production annual by country |
| **Port operation** | port slug (`los_angeles`) + month | LA monthly | Port of LA scrape | TEU: total / loaded-imports / empty / exports |
| **Macro indicator** | FRED series id (CPIAUCSL, DXY, SP500, M2, …) | ~40 (conditional) | FRED | Requires `FRED_API_KEY` |
| **Supply-chain index** | index name (GSCPI, ISRATIO) | 1–2 | NY Fed CSV, FRED | GSCPI keyless; ISRATIO needs FRED |
| **Fund-flow aggregate** | fund type (equity domestic/world/bond; MMF retail/govt/prime) | ~5 market-wide | ICI weekly `.xls` | WoW change; z-scored into the Money Flow Index |
| **AI company (news)** | company name | curated seed list | SEC EDGAR + Wikipedia enrich (`wiki_enrich.py`) | `data/ai_curated*.json` |
| **LTHCS holding** | ticker | 219 universe | Alpha Vantage, etc. | Separate pipeline; `data/lthcs/universe.json` |

## Cross-entity links (informal joins)

- **stock ticker** links `data-stock-money-flow.json` (MFI/CMF) ↔ `data-stock-prices.json` (sparkline, V2) ↔ LTHCS universe.
- **index ticker (SPY/DIA/QQQ)** links `equity_etf_flows.csv` (ETF leg) ↔ the per-index Money Flow Index sub-score in `market.json.money_flow.by_index`.
- **city slug** links the Socrata/ArcGIS raw feeds ↔ `data-city.json` pulse pillars ↔ `docs/city/data-city.schema.json` (the validation contract).
- **FRED series id** is the join key across CPI, metals (gold PM), supplies (ISRATIO), and macro overlays.
