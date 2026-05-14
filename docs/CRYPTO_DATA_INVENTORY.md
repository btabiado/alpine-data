# Crypto Data Source Inventory

This document is a research-only inventory of crypto data, signal, and analytics providers that could plausibly extend the personal trading dashboard at this repo. Compiled 2026-05-14. Sources that are already integrated (see CONTEXT in the README) are tagged "✓ INTEGRATED" only when referenced as overlap; they are not re-cataloged as new candidates. Pricing was verified via vendor sites / search at the time of writing; where a number could not be confirmed it is shown as `?`. Difficulty and Relevancy scores are subjective judgments for an *active BTC/ETH/LINK/LTC + macro + whale-tracking dashboard*, not generic ratings.

Scoring conventions:

- **Relevancy** (1-10): value for an active trader's daily dashboard. 10 = must-have, 1 = peripheral noise.
- **Difficulty** (1-10): 1 = trivial REST/JSON; 5 = paginated + rate-limited REST; 7 = OAuth or GraphQL with auth; 9 = JS-rendered scrape; 10 = data not really exposed.
- **Score** = Relevancy × (11 − Difficulty). Higher = better ROI to integrate.

---

## TOP 20 SHORTLIST (ranked by Relevancy × ease-of-integration)

| # | Name | Cat | Relev | Diff | Score | One-line why |
|---|------|-----|-------|------|-------|--------------|
| 1 | Binance Public API (futures) | Derivs | 9 | 2 | 81 | Free, no key needed for public endpoints; funding, OI, mark price across all pairs, plus historical OHLCV. Best public derivatives source. |
| 2 | GeckoTerminal API | Price | 8 | 2 | 72 | Free, 30 calls/min, no key — DEX OHLCV/liquidity across 1,800+ DEXes and 260+ chains. Fills the on-chain price gap. |
| 3 | Bybit v5 public API | Derivs | 8 | 2 | 72 | Free public endpoints for funding-rate history, OI, kline. Complements OKX and Binance for cross-exchange derivatives consensus. |
| 4 | CoinGlass (free web data + cheap API) | Derivs | 9 | 3 | 72 | Aggregated cross-exchange liquidation, funding heatmap, long/short — far better aggregation than wiring each exchange individually. |
| 5 | Coin Metrics Community API | On-chain | 8 | 2 | 72 | Free, no key; reliable BTC/ETH network metrics. Backup/cross-check for blockchain.info and to add ETH metrics not in current set. |
| 6 | CryptoPanic news API | News | 7 | 2 | 63 | Free tier with sentiment-voted news, 50-200 req/hr. Adds breaking-news widget with crowd sentiment without scraping RSS. |
| 7 | Kraken public REST | Price/Derivs | 7 | 2 | 63 | Free, no auth for public spot OHLC + Kraken Futures funding/OI. Diversification away from US-restricted venues. |
| 8 | LunarCrush API (free tier) | Sentiment | 8 | 3 | 64 | Social sentiment + Galaxy Score + AltRank for top assets; the cheapest serious social signal. Free tier daily credit pool. |
| 9 | DefiLlama (extra endpoints) | Stable/DeFi | 7 | 2 | 63 | We already use TVL; the same free API also exposes token unlocks, fees/revenue, treasuries, oracle feeds, bridges — easy wins. |
| 10 | Whale Alert websocket | On-chain | 8 | 4 | 56 | $30/mo (or free RSS-style Twitter mirror) for $1M+ transfer alerts across BTC/ETH/USDT/USDC. Best whale-flow firehose. |
| 11 | CoinMarketCap free tier | Price | 6 | 2 | 54 | 10K credits/mo, 30 req/min. Useful only as a cross-reference for CoinGecko outages; cheap insurance. |
| 12 | Etherscan v2 (more endpoints) | On-chain | 6 | 2 | 54 | We use gas oracle; the same key also gives token balances, ERC-20 transfer feeds, ENS, contract ABI — easy expansion. |
| 13 | Coinpaprika free | Price | 6 | 2 | 54 | 20K calls/mo free; OHLC + market overview; non-commercial. Good redundancy layer. |
| 14 | DexScreener API | Price | 7 | 3 | 56 | Best free DEX pair discovery + 24h price/volume; auth required but free, real-time. |
| 15 | CoinGecko Pro endpoints already free in v3 | Price | 6 | 2 | 54 | The public v3 has trending coins, derivatives, exchange tickers we may not be using — zero-cost expansion of existing key. |
| 16 | Numerai Crypto Signals universe (CSV) | AI / Quant | 6 | 3 | 48 | Free download of curated top-300 universe with daily features; useful as a feature library / ground-truth for an ML widget. |
| 17 | Blockchair multi-chain API | On-chain | 6 | 3 | 48 | Free for low volume; useful for LTC on-chain metrics (we have nothing for LTC today). |
| 18 | Reddit JSON endpoints (r/cryptocurrency, r/bitcoin) | Sentiment | 6 | 3 | 48 | Public `.json` endpoints still work for read-only post counts/scores; rough but free social-volume proxy. |
| 19 | Bitquery free tier (10k queries/mo) | On-chain | 6 | 4 | 42 | GraphQL across 40+ chains; one-stop for DEX trades + token flows. Free for prototyping. |
| 20 | RWA.xyz | Stable/DeFi | 5 | 4 | 35 | Tokenized treasuries / RWA flow — increasingly load-bearing in 2026's macro picture. Public site, API behind contact form. |

---

## 1. Price + Market Data APIs

| Name | URL | What you get | Pricing | Auth | Relev | Diff | Overlap | Worth integrating? |
|------|-----|--------------|---------|------|-------|------|---------|--------------------|
| **CoinMarketCap** | https://coinmarketcap.com/api/ | Listings, OHLC (limited), market cap rankings, exchange data, dominance | Free Basic: 10K credits/mo, 30 rpm, personal only | API key | 6 | 2 | CoinGecko ✓ | Maybe — useful as a redundancy layer when CoinGecko throttles. |
| **Coinpaprika** | https://coinpaprika.com/api/ | 2,000 assets, 1yr daily history, 25+ endpoints; OHLC, tickers, global | Free: 20K calls/mo, non-commercial. Starter $99/mo (internal only) | API key (free tier no key) | 6 | 2 | CoinGecko ✓ | Maybe — redundancy + slightly different field set. Watch the non-commercial clause. |
| **Messari** | https://messari.io/api | Asset metrics, fundraising, token unlocks, news, signals; 40K+ assets | Free: 20 rpm, register required. Paid from $29/mo | API key | 7 | 3 | CoinGecko + partial Glassnode | Yes — token unlocks + research metrics are unique; free tier covers daily polling. |
| **Kaiko** | https://www.kaiko.com/ | L1/L2 institutional tick data, indices, reference rates | No public free tier; ~$9.5K–55K/yr | API key (sales) | 5 | 8 | Likely overkill | No — institutional pricing, no path for personal use. |
| **Amberdata** | https://www.amberdata.io/ | Market, on-chain, DeFi, derivatives; deep IV surfaces, GEX | No public free tier; enterprise pricing | API key (sales) | 6 | 8 | Glassnode + CoinGlass overlap | No — enterprise-only. |
| **CoinAPI.io** | https://www.coinapi.io/ | Spot + derivatives + indices REST/WS; 350+ exchanges | $25 starter credits (~100 calls/day free); paid from $25/mo PAYG | API key | 5 | 3 | CryptoCompare ✓ | No — overlaps with what we have for free elsewhere. |
| **Polygon.io (crypto)** | https://polygon.io/crypto | Real-time + historical crypto OHLC + trades across major exchanges | Free: 5 calls/min, 15-min delayed. Paid from $29/mo | API key | 5 | 2 | CryptoCompare ✓ | Maybe — handy if you already have a Polygon stocks key; otherwise duplicative. |
| **dexscreener.com API** | https://docs.dexscreener.com/api/reference | DEX pair OHLCV, liquidity, social profiles, trending pairs | Free tier; API key required (since 2024) | API key | 7 | 3 | GeckoTerminal | Yes — best DEX discovery for memecoin / altcoin spotters. |
| **GeckoTerminal** | https://www.geckoterminal.com/dex-api | DEX pools, OHLCV, pool liquidity, 1,800+ DEXes, 260+ chains | Free: 30 calls/min, no key. Paid 25x for $? | None for free | 8 | 2 | partial CoinGecko ✓ | Yes — strictly DEX side, zero overlap with CG spot endpoints we use. |
| **Bitquery** | https://bitquery.io/ | GraphQL across 40+ chains; DEX trades, NFT, token flows | Free Developer: 10K queries/mo, 1,000 trial points, 10 rpm. Paid from $49/mo | API key | 6 | 4 | partial DeFiLlama ✓ | Maybe — powerful but GraphQL points system makes it harder to budget than REST. |
| **CryptoCompare paid (CCData/CoinDesk Data)** | https://data.coindesk.com/ | Indices, full L2 books, derivatives feeds | Free tier RETIRING May 21, 2026; institutional pricing thereafter | API key | 7 | 3 | CryptoCompare ✓ | No — already integrated; flag that the free tier is sunsetting. |
| **Polygon.network/blockstack public RPC** | (chain-specific RPCs) | Raw chain data (eth_call etc.) | Free (with per-provider rate limits) | None / API key | 4 | 5 | partial Etherscan ✓ | No — too low-level for a dashboard. |
| **CryptoCompare free CCCAGG (already in)** | https://min-api.cryptocompare.com/ | Cross-exchange index | Free | API key | — | — | ✓ INTEGRATED | — |
| **CoinGecko Pro (paid)** | https://www.coingecko.com/api/pricing | Higher rate limits + on-chain DEX + exchange volumes audited | Free Demo 30 rpm; Analyst $129/mo | API key | 7 | 2 | ✓ INTEGRATED (free) | Maybe — upgrade path, not a new source. |

## 2. On-Chain Analytics

| Name | URL | What you get | Pricing | Auth | Relev | Diff | Overlap | Worth integrating? |
|------|-----|--------------|---------|------|-------|------|---------|--------------------|
| **Glassnode Tier 2/3** | https://studio.glassnode.com/pricing | Hourly/10-min on-chain metrics, SOPR, MVRV, LTH/STH cohorts | Standard $39/mo (24h res), Advanced $300/mo, Pro API add-on (sales) | API key | 9 | 3 | ✓ wired but inactive | Yes — most cost-effective at Advanced; the *quintessential* on-chain source. |
| **CryptoQuant** | https://cryptoquant.com/pricing | Exchange flows, miner flows, derivatives, entity labels | Basic Free; Advanced $29/mo, Professional $99/mo (API), Premium $799/mo | API key | 8 | 3 | partial Glassnode | Yes — Professional tier is the cheapest credible API competitor to Glassnode. |
| **Santiment / SanAPI** | https://academy.santiment.net/products-and-plans/sanapi-plans/ | On-chain + social + dev activity; GraphQL, Python SDK | Free tier + 14-day trial; paid tiers | API key | 7 | 4 | overlaps Glassnode + LunarCrush | Maybe — strongest if you specifically want dev-activity + social in one feed. |
| **Nansen** | https://nansen.ai/api | Smart Money labels, token-god-mode, wallet profiler | Pro $49/mo (annual); API on free credits + Pro credits | API key | 8 | 5 | partial Lookonchain | Maybe — UI is the killer feature; API is credit-based and not cheap at scale. |
| **Sentora (ex-IntoTheBlock)** | https://sentora.com/analytics-research | Risk Pulse + DeFi protocol metrics; free research portal | Free research, commercial API by sales | API key (sales) | 6 | 7 | partial DeFiLlama ✓ | No — legacy ITB API was deprecated; current API is enterprise. |
| **Dune Analytics** | https://dune.com/pricing | Custom SQL on indexed chains; community dashboards | Free (no API), Plus $399/mo, Premium $999/mo (API) | API key | 7 | 4 | none | Maybe — only if you'll write custom SQL; otherwise pay for nothing. |
| **Lookonchain** | https://twitter.com/lookonchain | Curated whale-narrative posts (Twitter feed) | Free; no API | None (X scrape / RSS) | 7 | 7 | partial Whale Alert | Maybe — value is human curation; integrate via X RSS bridge if at all. |
| **Arkham Intelligence** | https://intel.arkm.com/api/docs | Entity-resolved addresses + portfolio + alerts | Public API launched Feb 2026; request access | API key | 8 | 6 | partial Nansen | Yes — uniquely good entity attribution; gated access is the main hurdle. |
| **Whale Alert** | https://developer.whale-alert.io/pricing.html | Large transfer firehose (BTC/ETH/USDT/USDC + more) | ALERTS plan ~$30/mo (7-day trial); free Twitter feed | API key | 8 | 4 | partial Lookonchain | Yes — cheap and high-signal; pair with Lookonchain for narrative. |
| **Blockchair** | https://blockchair.com/api/docs | Multi-chain explorer API (48+ chains incl. LTC) | Free for low volume; paid for higher | API key | 6 | 3 | partial blockchain.info ✓ | Yes — only credible way to add LTC on-chain metrics. |
| **BlockCypher** | https://www.blockcypher.com/dev/bitcoin/ | BTC/ETH/LTC/DOGE/DASH unified API, address/tx data | Free with rate limits; paid for more | API key | 5 | 3 | overlaps blockchain.info ✓ + mempool ✓ | No — duplicative of what we have. |
| **Coin Metrics (Community)** | https://docs.coinmetrics.io/info/account-types | Free Network Data + Market Data subset; no key required | Free (10 req / 6 s) | None | 8 | 2 | overlaps blockchain.info ✓ | Yes — broader coverage than blockchain.info, includes ETH and stablecoin metrics. |
| **Etherscan v2 (more endpoints)** | https://docs.etherscan.io/ | Gas (already used), token balances, txlists, ABI, ENS | Free 5 calls/sec | API key ✓ already have | 6 | 2 | ✓ partial | Yes — same key unlocks lots more; cheapest expansion. |

## 3. Derivatives / Options / Futures

| Name | URL | What you get | Pricing | Auth | Relev | Diff | Overlap | Worth integrating? |
|------|-----|--------------|---------|------|-------|------|---------|--------------------|
| **CoinGlass** | https://www.coinglass.com/CryptoApi | Aggregated funding, OI, liquidations, long/short, options OI, ETF flows | Free web; API from $35/mo | API key | 9 | 3 | partial OKX ✓ + Deribit ✓ | Yes — aggregation is the value-add; scraping the free web is also viable. |
| **Laevitas** | https://app.laevitas.ch/dashApi | Options Greeks, IV surfaces, funding, OI across Binance/Deribit/OKX/Bybit/Hyperliquid | Premium ~$50/mo; PAYG with crypto pay | API key | 7 | 4 | partial Deribit ✓ | Maybe — best non-Deribit options-focused source. |
| **Tardis.dev** | https://tardis.dev/ | Tick-level historical L2/L3 for backtesting | Solo/Pro subscription tiers (specifics gated) | API key | 5 | 5 | none for real-time | No — backtesting tool, not a dashboard feed. |
| **Velo Data** | https://velodata.app/ | Free terminal aggregating CEX derivatives in browser | Free terminal; API gated | API key | 6 | 5 | overlaps CoinGlass | Maybe — visual-first; programmatic access is unclear. |
| **Binance Futures public** | https://developers.binance.com/docs/derivatives | Funding rate, OI, mark, kline, premium index — all coins | Free, no auth for public endpoints | None | 9 | 2 | partial OKX ✓ | Yes — easiest big-win add. |
| **Bybit v5 public** | https://bybit-exchange.github.io/docs/v5/market/history-fund-rate | Funding history, OI, kline, recent trades | Free, no auth | None | 8 | 2 | partial OKX ✓ | Yes — cross-venue confirmation of OKX signals. |
| **Kraken Futures** | https://docs.kraken.com/api/docs/futures-api/trading/historical-funding-rates/ | Historical funding, OI per instrument | Free public | None | 7 | 2 | partial OKX ✓ | Yes — adds a US-regulated venue datapoint. |
| **Deribit (already in for DVOL)** | https://docs.deribit.com/ | Options chains, full IV surface, BTC/ETH DVOL | Free | None | — | — | ✓ INTEGRATED (DVOL only) | Yes — expand: pull the full options chain for ATM IV term structure. |
| **Genesis Volatility / blockanalitica.com** | https://gvol.io/ | Options Greeks, vol surfaces (premium) | Paid only | API key | 5 | 5 | overlaps Laevitas | No — overlaps with cheaper Laevitas. |
| **Skew (Coinbase)** | https://www.coinbase.com/institutional/insights | Coinbase has limited what's public; some skew metrics in research | Free reports, no API | None / scrape | 4 | 7 | overlaps CoinGlass | No — data not really exposed any more. |

## 4. Sentiment / Social

| Name | URL | What you get | Pricing | Auth | Relev | Diff | Overlap | Worth integrating? |
|------|-----|--------------|---------|------|-------|------|---------|--------------------|
| **LunarCrush v4** | https://lunarcrush.com/about/api | Galaxy Score, AltRank, social volume, bullish/bearish sentiment per asset | Free (basic), Individual $24/mo, Builder $240/mo; credit-metered | API key | 8 | 3 | none currently | Yes — best mainstream social signal at consumer pricing. |
| **The TIE** | https://www.thetie.io/solutions/sentiment-api/ | Quant-grade sentiment back to 2017; institutional | Sales only | API key | 7 | 6 | overlaps LunarCrush | No — institutional pricing, no path for personal. |
| **Santiment social** | https://santiment.net/ | Social volume, dominance, weighted sentiment | Free + paid tiers (see §2) | API key | 7 | 4 | LunarCrush | Maybe — pick one of LunarCrush vs Santiment. |
| **Augmento** | https://augmento.ai/ | Sentiment scores from X, Reddit, Bitcointalk | Paid (sales-led); no public price | API key | 6 | 6 | LunarCrush | No — opaque pricing. |
| **Adanos** | https://adanos.org/ | Stocks + crypto + Polymarket sentiment in one API | Free trial; paid tiers `?` | API key | 5 | 4 | LunarCrush | Maybe — novel Polymarket signal worth a look. |
| **Reddit JSON** | https://www.reddit.com/r/cryptocurrency.json | Top/new posts, scores, comment counts | Free read-only (User-Agent required); rate-limited | None for read | 6 | 3 | none | Yes — cheap "social volume" proxy widget. |
| **CryptoMood** | https://cryptomood.com/ | AI sentiment + market signals | Sales only | API key | 4 | 7 | LunarCrush | No. |
| **Alternative.me (Fear & Greed)** | https://alternative.me/crypto/fear-and-greed-index/ | Daily index | Free | None | — | — | ✓ INTEGRATED | — |

## 5. Trading Signal Services (skeptical lens)

Most paid signal services charge for what is essentially public TA + hype. Treat them as marketing channels, not data sources. Listing here for completeness only.

| Name | URL | What you get | Pricing | Auth | Relev | Diff | Worth integrating? |
|------|-----|--------------|---------|------|-------|------|--------------------|
| **TradingView ideas / Pine alerts** | https://www.tradingview.com/ | Crowd-sourced "ideas" + Pine Script alerts via webhook | Free + Pro from $14.95/mo | Webhook (no API for data pull) | 6 | 6 | Maybe — only via webhook ingestion. No data API. |
| **CoinTelegraph Markets Pro** | https://marketspro.cointelegraph.com/ | VORTECS score + NewsQuakes alerts | $79.99/mo | App / web; no API | 4 | 8 | No — closed system. |
| **3Commas signal marketplace** | https://3commas.io/signal-bot | Marketplace of signal providers with verified P&L | Free read; subs vary | 3Commas account | 3 | 7 | No — not a data source; opens execution risk. |
| **CryptoHopper signal market** | https://www.cryptohopper.com/signals | Same pattern as 3Commas | Free + paid signals | account | 3 | 7 | No. |
| **AltSignals / Fat Pig / Cryptosignals.org** | various | Paid Telegram/Discord signal groups | Subscription $30-300/mo | none / scrape | 2 | 8 | No — typical "guru" services with no edge proof. |
| **Quantify Crypto** | https://quantifycrypto.com/ | Quant scores + watchlists | Free + paid | account | 4 | 7 | No — UI, no real API. |
| **AlphaPicks / MEXC signal market** | https://www.mexc.com/copytrading | Exchange-hosted copy-trading | Free | account | 3 | 7 | No — execution, not data. |
| **altFINS API** | https://altfins.com/ | 130+ pre-built signals + 150+ TA indicators | Paid (price `?`) | API key | 5 | 4 | Maybe — could replace homegrown TA, but unclear pricing. |
| **Glassnode signals** | included in Glassnode Advanced | Threshold-based on-chain alerts | inside Glassnode plan | API key | 7 | 3 | Yes — bundle, not separate. |

## 6. AI / Quant Models

| Name | URL | What you get | Pricing | Auth | Relev | Diff | Worth integrating? |
|------|-----|--------------|---------|------|-------|------|--------------------|
| **Numerai Crypto Signals** | https://signals.numer.ai/ | Daily token universe (top ~300) + features CSV | Free download; payouts in NMR | numerapi key | 6 | 3 | Yes — for an ML widget or feature library; rare free quant-grade dataset. |
| **Token Metrics API** | https://www.tokenmetrics.com/api | AI ratings, trader/investor grades, MCP server for LLMs | Free tier + credit-based; paid tiers `?` | API key | 5 | 4 | Maybe — but ratings are opaque (low explainability). |
| **CryptoVision / TradeSanta / others** | varied | "AI" models with no public methodology | Subscription | account | 2 | 6 | No — black-box / marketing. |
| **Kaggle crypto competitions** | https://www.kaggle.com/competitions | Open datasets and notebooks (e.g. G-Research Crypto Forecasting) | Free | account | 4 | 4 | Maybe — research input, not a live feed. |
| **NBER / SSRN crypto research datasets** | https://www.ssrn.com/ | Academic datasets, often one-off CSVs | Free | none | 3 | 7 | No — sporadic, hard to maintain. |

## 7. News + Macro APIs

| Name | URL | What you get | Pricing | Auth | Relev | Diff | Overlap | Worth integrating? |
|------|-----|--------------|---------|------|-------|------|---------|--------------------|
| **CryptoPanic** | https://cryptopanic.com/developers/api/ | Aggregated crypto news + community sentiment voting | Free 50-200 req/hr; paid tiers | API key | 7 | 2 | partial RSS ✓ | Yes — adds vote-sentiment dimension RSS lacks. |
| **NewsAPI.org** | https://newsapi.org/ | General news (incl. crypto) by source/keyword | Dev free: 100 req/day, 24h delay, dev only. Business $449/mo | API key | 4 | 3 | partial RSS ✓ | No — free tier is dev-only and delayed. |
| **NewsData.io** | https://newsdata.io/ | Crypto news endpoint, multi-source | Free 200/day | API key | 5 | 3 | RSS ✓ | Maybe — better free quotas than NewsAPI.org. |
| **Cryptonews-API** | https://cryptonews-api.com/ | Crypto-only news with tickers, sentiment scores | Free 100/mo; paid from $19/mo | API key | 5 | 3 | RSS ✓ | Maybe — sentiment-labeled headlines are convenient. |
| **AlphaSense** | https://www.alpha-sense.com/ | Enterprise news + filings | Enterprise sales | OAuth | 6 | 9 | none | No. |
| **RavenPack** | https://www.ravenpack.com/ | Quant-grade event tagging | Enterprise sales | API key | 7 | 9 | none | No — institutional. |
| **Bloomberg Open API tier** | https://www.bloomberg.com/professional/support/api-library/ | Bloomberg Terminal data | Requires Terminal license | DAPI | 8 | 10 | none | No — Terminal-only. |
| **FRED (macro)** | already ✓ | macro series | Free | API key | — | — | ✓ INTEGRATED | — |
| **Yahoo Finance** | already ✓ | broad markets | Free (unstable) | None | — | — | ✓ INTEGRATED | — |
| **Tradeable economic calendar (Trading Economics)** | https://tradingeconomics.com/api/ | Macro calendar events (CPI, FOMC, NFP) | Free guest key (15 calls); paid from $25/mo | API key | 7 | 3 | partial FRED ✓ | Yes — calendar widget is a common gap; FRED has data, not event timing. |
| **investpy / Investing.com calendar scrape** | https://github.com/alvarobartt/investpy | Same calendar via scrape | Free | none / fragile | 5 | 6 | TradingEconomics | Maybe — fragile but free. |

## 8. Stablecoin / DeFi Specific

| Name | URL | What you get | Pricing | Auth | Relev | Diff | Overlap | Worth integrating? |
|------|-----|--------------|---------|------|-------|------|---------|--------------------|
| **DefiLlama (extra endpoints)** | https://api-docs.defillama.com/ | Token unlocks, fees/revenue, treasuries, oracle, bridges, yields | Free | None | 7 | 2 | ✓ partial | Yes — same source, more endpoints. |
| **DefiLlama Pro** | https://defillama.com/subscription | Higher rate limits, premium prices/inflows/unlocks | $300/mo | API key | 7 | 3 | ✓ partial | Maybe — only if you push request volume. |
| **RWA.xyz** | https://app.rwa.xyz/ | Tokenized treasuries, RWA categories, issuer flows | Public web; API by contact | sales | 5 | 5 | none | Maybe — important narrative in 2026 macro. |
| **Stablewatch** | https://www.stablewatch.io/ | Yield-bearing stablecoin APY/TVL across 60+ assets | Public web; API `?` | unclear | 5 | 6 | DefiLlama ✓ | Maybe — niche but uncovered today. |
| **Circle API (USDC mint/burn)** | https://developers.circle.com/ | USDC mint/burn webhooks | Free dev; production requires KYB | API key | 6 | 5 | DefiLlama stable supplies ✓ | Maybe — USDC supply changes are a leading indicator. |
| **Tron stablecoin volume** | https://tronscan.org/ | USDT-TRC20 supply, transfers | Free public explorer | None / scrape | 5 | 5 | DefiLlama | Maybe — Tron USDT dominance is informative. |

## 9. NFT / Memecoin / Speculative

| Name | URL | What you get | Pricing | Auth | Relev | Diff | Overlap | Worth integrating? |
|------|-----|--------------|---------|------|-------|------|---------|--------------------|
| **NFTGo** | https://developer.nftgo.io/ | Floor prices, ML-driven NFT valuations, 5,000+ collections | Free tier + paid; `?` | API key | 3 | 4 | none | No — out of scope for BTC/ETH/LINK/LTC dashboard. |
| **Crypto Slam** | https://cryptoslam.io/ | NFT volume rankings | Free web; API `?` | none / scrape | 2 | 7 | none | No. |
| **DappRadar** | https://dappradar.com/api | NFT + dapp + token analytics | Free tier with limited endpoints | API key | 3 | 4 | none | No — out of scope. |
| **DexScreener (memecoin discovery)** | listed in §1 | Trending pairs on Solana / Base / etc. | Free w/ key | API key | 6 | 3 | GeckoTerminal | Maybe — if memecoin / altcoin widget is desired. |
| **Pump.fun / GMGN.ai aggregators** | https://gmgn.ai/ | Solana memecoin telemetry, dev wallet tracking | Free web; APIs unofficial | scrape | 4 | 8 | DexScreener | No — too speculative + fragile scrape. |

## 10. Regulatory / On-Chain Compliance

| Name | URL | What you get | Pricing | Auth | Relev | Diff | Worth integrating? |
|------|-----|--------------|---------|------|-------|------|--------------------|
| **Chainalysis Reactor** | https://www.chainalysis.com/ | Sanctions screening, illicit flow attribution | Enterprise only | OAuth | 5 | 10 | No — no free tier. |
| **TRM Labs** | https://www.trmlabs.com/ | Same category | Enterprise | OAuth | 5 | 10 | No. |
| **Elliptic** | https://www.elliptic.co/ | Same category | Enterprise | OAuth | 5 | 10 | No. |
| **OFAC SDN list (govt)** | https://www.treasury.gov/ofac/downloads/sdnlist.txt | Sanctioned crypto addresses | Free | None | 4 | 4 | Maybe — sanity check for token-flow widgets, but rarely actionable for a trader. |
| **Arkham public watchlists** | listed in §2 | Government / treasury / fund labeled addresses | Free web | none | 7 | 6 | Yes — entity-resolved flows beat raw on-chain. |

---

## Skipped / Not Recommended

These were considered and rejected. One-line reasons follow.

1. **Kaiko, Amberdata, RavenPack, AlphaSense, Bloomberg, Chainalysis/TRM/Elliptic** — institutional-only pricing with no realistic personal-use path.
2. **The TIE Sentiment API** — same; LunarCrush + Santiment cover 90% at a fraction of the cost.
3. **Skew (Coinbase Institutional Insights)** — most public data was retired after the Coinbase acquisition; replaced by paywalled reports.
4. **Pushshift Reddit dataset** — public API broken since Reddit's 2023 changes; intermittent at best.
5. **Reddit official API (paid tier)** — 2023 pricing makes it economically unjustifiable when public `.json` endpoints suffice for read-only.
6. **AltSignals / Fat Pig Signals / Cryptosignals.org / similar Telegram groups** — no verifiable edge; data-quality and survivorship-bias issues.
7. **CoinTelegraph Markets Pro** — closed app, no data API; you'd be paying for a UI, not a feed.
8. **3Commas / CryptoHopper signal marketplaces** — execution tools with signal byproducts; not data sources for a dashboard.
9. **TradeSanta / Quantify Crypto / proprietary "AI" services** — opaque models, no explainability, no API guarantees.
10. **NBER / SSRN datasets** — useful for backtests, not maintainable live feeds.
11. **Pump.fun / GMGN.ai unofficial APIs** — memecoin scraping; fragile and out of scope for BTC/ETH/LINK/LTC focus.
12. **Crypto Slam, NFTGo, DappRadar** — NFT/dapp focus; out of scope.
13. **Genesis Volatility / blockanalitica.com** — overlap Laevitas at a higher price.
14. **CryptoCompare (CCData/CoinDesk Data) free tier** — sunsetting May 21, 2026; the integrated cadli endpoint should be wrapped with a fallback before then.
15. **Sentora commercial API** — IntoTheBlock's legacy API has been deprecated; current API is enterprise-gated.

---

## Notes on the integrated set (referenced for overlap)

For reference, the dashboard already covers: CoinGecko (price/MC), OKX (funding/OI/LS), Deribit (DVOL), Alternative.me (F&G), blockchain.info (BTC on-chain), mempool.space (BTC fees/hashrate), DeFiLlama (TVL/yields/bridges), Etherscan v2 (ETH gas), CoinDesk cadli (BTC OHLC), FRED (DXY/SPX/Gold/10Y/M2), Yahoo (Dow/SPX/Nasdaq/VIX/gold futures), bitinfocharts (BTC whale supply), Crypto RSS (CoinDesk/Cointelegraph/Decrypt/The Block/Bitcoin Magazine), CryptoCompare CCCAGG. Glassnode is wired but inactive pending paid key.
