# LTHCS Crypto Pillar Adapter — Design Spec

**Status**: design spec, retroactive on partial V1 build (Tier 5 #27, `lthcs-open-items-audit.md` line 363).
**Last updated**: 2026-05-18.
**Owns**: extension of the equity LTHCS framework to crypto assets (BTC, ETH, SOL) without forking pillar math or persistence.

A partial V1 already exists (`lthcs/pillars/crypto_*.py`, `lthcs/sources/crypto_data.py`, `scripts/lthcs_crypto_daily.py`, `data/lthcs/crypto_universe.json`, `btc/eth/sol` weight profiles in `data/lthcs/weights.json`). This spec freezes the design intent, documents what is wired, and ranks the work still needed to ship.

---

## 1. Goal

Extend LTHCS to a 0–100 Long-Term Hold Confidence Score for crypto (BTC / ETH / SOL initially), preserving the six bands (Elite / High / Constructive / Monitor / Weakening / Review), persistence layout, and UI patterns. Each pillar gets a crypto-native input layer and component math; the composite combiner (`lthcs/score.py:compute_lthcs_score`) is reused unchanged. Inputs come from `fetch_market.py`'s CoinGecko / Coinbase / Blockchain.info / DeFiLlama / Farside fetchers plus cached `data/whale.json` — no new paid feeds in Phase 1.

---

## 2. Pillar mapping — equity → crypto

| Pillar | Equity inputs (today) | Crypto inputs (proposed) | Maps cleanly? |
|---|---|---|---|
| Adoption | Revenue %, sector-relative, QoQ, Google Trends | Active addresses Δ30d, on-chain tx volume USD Δ30d, hash rate (BTC) or tx count (ETH/SOL) Δ30d | direct |
| Institutional | Form 4 insider, 13F, 90d momentum | Whale-cohort balance Δ30d (BTC), spot ETF net flows trailing 30d (BTC/ETH), 30d price momentum | analogous |
| Financial | Revenue %, margin, OCF (banks: NII) | Network revenue Δ30d (miners' rev for BTC; tx-volume proxy for ETH/SOL), realized-cap proxy (30d market-cap Δ), supply inflation %/yr (lower = better) | requires asset-class math |
| Thesis | Finnhub recs, 8-K, Yahoo earnings, sector RSS | Funding-rate normalcy (perp 8h rate), L/S-ratio normalcy, narrative sentiment placeholder | adapt existing |
| DES | FRED tier-1 + tier-2 macros, sector tilt | Stablecoin supply Δ30d (DeFiLlama), exchange-reserves Δ30d (optional), macro overlay (HY OAS, VIX, 10Y Δ30d bp from FRED) | similar |

### 2.1 Adoption Momentum — `lthcs/pillars/crypto_adoption.py`

- **Active addresses Δ30d** 0.40 — % change vs. 30 days prior, `bounded_linear(-25, +25)`.
- **Tx volume USD Δ30d** 0.30 — same shape, bounds (-50, +50).
- **Security/throughput Δ30d** 0.30 — BTC uses hash-rate (-15, +15); ETH/SOL use tx-count series; falls back to a tighter cut of active addresses with a `data_quality` flag noting the proxy.
- **Renormalize** any missing component proportionally; all-missing → neutral 50.

### 2.2 Institutional Confidence — `lthcs/pillars/crypto_institutional.py`

- **Whale-cohort balance Δ30d (BTC only)** 0.50 — sum of `b1k_10k + b10k_100k + b100k_1m` from `data/whale.json` distribution buckets; bounds (-1.0%, +1.0%) matching the V1 Whale Sentiment Index saturation point. ETH/SOL drop this component.
- **ETF flows trailing 30d** 0.30 — sum of Farside `Total` column from `data/btc_flows.csv` / `data/eth_flows.csv`, bounds (-$3B, +$3B). SOL drops (no spot ETF yet).
- **30d price momentum** 0.20 — CoinGecko `price_change_pct_30d`, bounds (-30%, +30%). Always available; serves as the floor signal when whale + ETF data are missing.

### 2.3 Financial Evolution — `lthcs/pillars/crypto_financial.py`

- **Network revenue Δ30d** 0.40 — BTC: `miners_revenue_usd_series` (blockchain.info). ETH/SOL: `tx_volume_usd_series` as a fee-revenue proxy (until explicit gas-fee fetchers ship in Phase 4). Bounds (-40%, +40%).
- **Realized-cap proxy** 0.30 — CoinGecko 30d market-cap change (supply moves slowly, so it tracks realized cap to first order). Bounds (-30%, +30%). Phase 4 swap target: CoinMetrics community realized-cap series.
- **Supply stability** 0.30 — annual supply-inflation %, **inverted** via `bounded_linear(0, 10, invert=True)`. Defaults: BTC 0.83, ETH 0.10, SOL 5.5; override per-asset via `supply_inflation_pct_yr` in inputs.

### 2.4 Thesis Integrity — `lthcs/pillars/crypto_thesis.py`

- **Funding-rate normalcy** 0.50 — perpetual-swap funding rate (% per 8h). `|r| ≤ 0.01%` scores 100; `|r| ≥ 0.10%` scores 0; symmetric around zero (both euphoric longs and panic shorts hurt the score).
- **L/S-ratio normalcy** 0.30 — top-trader long/short ratio; 1.0 scores 100; ≥1.8 or ≤0.55 scores 0; log-space distance from 1.0 so reciprocal extremes score equally.
- **Narrative sentiment** 0.20 — placeholder in Phase 1 (drops out → renormalize). Phase 3 will plug in Alpha Vantage `NEWS_SENTIMENT` for `CRYPTO:BTC` etc., or share the LLM sentiment shadow (Tier 5 #28 / `lthcs/sources/llm_sentiment.py`).
- Funding + L/S inputs come from `fetch_market.py`'s `coinbase_intl_perpetuals()` + Deribit / OKX wrappers; the runner currently does not persist them per-asset (Phase 3 work).

### 2.5 Demand Environment Score — `lthcs/pillars/crypto_des.py`

- **Stablecoin supply Δ30d** 0.50 — DeFiLlama `/stablecoins` aggregate market-cap Δ30d, bounds (-10%, +10%). Universe-wide; same value for all assets.
- **Exchange reserves Δ30d** 0.20 — optional `exchange_reserves_pct_30d`, **inverted** (falling reserves = accumulation = good). Drops when absent (default in V1).
- **Macro overlay** 0.30 — HY OAS, VIX, 10Y Δ30d bp from FRED. Each maps to a `[-1, +1]` tilt; the average tilt shifts the score ±25 points off the neutral 50.

### 2.6 Normalization & renormalization

All sub-components use `lthcs.normalize.bounded_linear` so the score surface matches the equity pillars. Missing components → proportional renormalization; all-missing → neutral 50.0 with a `data_quality` flag. The composite combiner already redistributes pillar weights via `lthcs/score.py:_FLAGS_TO_DROPPED_PILLAR`; Phase 3 adds `crypto_thesis_unavailable` to that map.

---

## 3. Universe definition

- Initial roster: BTC, ETH, SOL. Lives in `data/lthcs/crypto_universe.json` (separate from `universe.json` to avoid mixing asset classes through equity loaders).
- Each row: `symbol`, `name`, `active`, `weight_profile` (resolves to a profile in `data/lthcs/weights.json`).
- Schema extension target: add `asset_class: "crypto" | "equity"` to both universe files when Phase 5 unifies the loader; add `asset_id` (CoinGecko id) when Phase 4 needs a stable cross-source key.
- The `maturity_stage` concept maps to crypto-flavored profile names rather than equity stages: `btc` (digital gold), `eth` (smart-contract platform), `sol` (high-throughput L1, higher inflation).

---

## 4. Weights profile

`data/lthcs/weights.json:profiles` already includes three crypto profiles (lines 24–26):

| Profile | adoption | institutional | financial | thesis | des |
|---|---|---|---|---|---|
| `btc` | 0.10 | 0.30 | 0.25 | 0.15 | 0.20 |
| `eth` | 0.25 | 0.20 | 0.20 | 0.20 | 0.15 |
| `sol` | 0.30 | 0.15 | 0.20 | 0.20 | 0.15 |

Priors: BTC leans on institutional flow (ETFs + whales); ETH balances adoption (smart-contract activity) with institutional; SOL leans on adoption since institutional access is thin. Financial is lower for ETH/SOL than equity-mature profiles because the realized-cap proxy is weak in V1. Bands + modifiers are shared with equities — calibration revisited in §5.

---

## 5. Score bands

Same six-band structure + integer-floor banding logic (`lthcs/score.py:assign_band`) — no crypto-specific labels in Phase 1. A composite of 75 implies the same conviction across asset classes, so we **share thresholds** and absorb crypto's wider variance by tuning component bounds (already done: -25/+25 for active addresses vs. -10/+10 for revenue growth). Phase 5 re-evaluates after 60–90 days of snapshots; if BTC/ETH/SOL cluster above 80 in flat regimes, tighten the macro overlay rather than introduce a new band map. The volatility modifier keeps its 90th-percentile universe trigger, computed within the crypto cohort only.

---

## 6. Data sources (cross-reference)

| Input | Source | Where wired |
|---|---|---|
| Active addresses, hash rate, tx volume, miners revenue | `data/whale.json` (from `fetch_market.py`) + blockchain.info `/charts` fallback | `lthcs/sources/crypto_data.py:CryptoDataAdapter.whale`, `.blockchain_chart` |
| Market price + 30d ROI | CoinGecko `/coins/markets` | `crypto_data.py:fetch_coingecko_markets` |
| ETF flows | `data/btc_flows.csv`, `data/eth_flows.csv` (Farside via `parse_farside.py`) | `crypto_data.py:load_etf_flows` |
| Stablecoin supply | DeFiLlama `/stablecoins` | `crypto_data.py:fetch_stablecoin_total` |
| Whale-cohort distribution | `data/whale.json:btc.distribution` | `crypto_data.py:whale_distribution` |
| FRED macro (HY OAS, VIX, 10Y) | `lthcs/sources/fred.py`, `fred_tier2.py` | passed into adapter via runner |
| Funding rate, L/S ratio | `fetch_market.py:coinbase_intl_perpetuals`, Deribit, OKX | **gap** — adapter does not yet expose these per-asset (Phase 3) |
| Realized cap (Glassnode-grade) | — | **gap** — Phase 4 considers CoinMetrics community feed |
| On-chain fee revenue (ETH/SOL) | — | **gap** — Phase 4 (Etherscan API, Solana RPC, or DeFiLlama fees endpoint) |
| Exchange reserves Δ30d | — | **gap** — optional pillar input; no free reliable feed identified |

---

## 7. UI integration

Three options, ranked:

1. **Recommended: new `/lthcs/crypto/` route** mirroring `/lthcs/` layout. The equity UI is tuned to large universes (sector filters, peer-percentile bars); with three crypto assets those affordances misfire. A parallel route tailors the layout (per-asset detail card + funding/ETF charts) without bloating equity templates. Snapshots write to a sibling `data/lthcs/snapshots_crypto/` directory.
2. **Toggle inside `/lthcs/`** to filter by asset class — cheapest; reuses templates. Loses crypto-native context and forces the peer-percentile widget onto a 3-row group.
3. **Embed in Crypto Trading Dashboard "Whale Activity" tab** — cohesive with existing crypto tooling but breaks the LTHCS-as-singular-product framing and complicates V1/V2 dual-build deployment.

Phase 5 ships Option 1; V1-only (per `lthcs_phase1` memory).

---

## 8. Implementation phasing

| Phase | Scope | Effort | Status |
|---|---|---|---|
| 1 | Universe + Adoption + DES + Financial + Institutional pillars + standalone runner | M | **shipped** (`crypto_*.py`, `lthcs_crypto_daily.py`, `crypto_universe.json`) |
| 2 | ETF-flow + whale-cohort polish; integration into `lthcs_daily.py` dispatch (`LTHCS_CRYPTO_ENABLED=1`); persist snapshots to `data/lthcs/snapshots_crypto/` and per-asset history | S | runner exists; needs snapshot-dir + history wiring + CI gate |
| 3 | Thesis Integrity wiring — persist per-asset funding rate + L/S ratio from `fetch_market.py` into adapter, add `crypto_thesis_unavailable` to `_FLAGS_TO_DROPPED_PILLAR` | M | none of this is wired |
| 4 | Financial polish — gas-fee fetchers (Etherscan / Solana RPC / DeFiLlama fees), CoinMetrics community realized-cap, supply-inflation auto-refresh | M-L | gap; needs new source modules |
| 5 | UI surface — `/lthcs/crypto/` route, crypto-aware narratives, dashboard card | M | none |

Phase 1 + 2 ship the daily score; Phase 3 brings Thesis online; Phase 4 raises the Financial pillar from "proxy" to "first-class"; Phase 5 makes it visible.

---

## 9. Files to create / modify (checklist)

Existing (do not touch unless phase calls for it):
- `lthcs/pillars/crypto_adoption.py`, `crypto_institutional.py`, `crypto_financial.py`, `crypto_thesis.py`, `crypto_des.py`
- `lthcs/sources/crypto_data.py`
- `scripts/lthcs_crypto_daily.py`
- `data/lthcs/crypto_universe.json`
- `data/lthcs/weights.json` (btc/eth/sol profiles)

Phase 2:
- `lthcs_daily.py` lines 1936–1945 — already dispatches `run_crypto`; verify `LTHCS_CRYPTO_ENABLED=1` path writes snapshots to `data/lthcs/snapshots_crypto/<date>.json`.
- `lthcs/persist.py` — confirm per-asset history paths do not collide (BTC/ETH/SOL never overlap with equity tickers in this repo).
- `tests/lthcs/crypto/` — new unit tests for each pillar (pure math; fixtures in JSON).
- `.github/workflows/pages.yml` — wire `LTHCS_CRYPTO_ENABLED=1` into the daily job.

Phase 3:
- `lthcs/sources/crypto_data.py:inputs_for` — populate `funding_rate_pct_8h`, `long_short_ratio` from `fetch_market.py` outputs.
- `lthcs/score.py:_FLAGS_TO_DROPPED_PILLAR` — add `"crypto_thesis_unavailable": "thesis_integrity"`.

Phase 4:
- `lthcs/sources/crypto_fees.py` (new) — Etherscan / Solana RPC / DeFiLlama fees fetchers.
- `lthcs/sources/coinmetrics.py` (new, optional) — community realized-cap.
- `lthcs/pillars/crypto_financial.py` — swap proxies for real series; tighten bounds.

Phase 5:
- `lthcs_tab/` (new templates) or extend `lthcs_tab_v2/` analogue for V1 layout.
- `app.py` — register `/lthcs/crypto/` route reading `data/lthcs/snapshots_crypto/`.
- `lthcs/narratives.py` — add crypto-aware narrative templates.

---

## 10. Open questions / blockers

- **Adaptive weights (Tier 5 #25)**: 3-asset universe is too thin for stable walk-forward IC. Defer adaptive overrides for crypto until 12+ months of history; keep `adaptive_overrides.enabled=false` for crypto profiles even if equity flips on.
- **Backtest engine (Tier 5 #24)**: equity backtest assumes total return with peer-percentile machinery; crypto needs a parallel run on CoinGecko price series, no dividends, no peer percentiles.
- **CoinMetrics community vs. paid**: free has 1-day lag and a narrower metric set; paid unlocks realized cap, SOPR, MVRV. Phase 4 cost-benefit.
- **Funding-rate aggregation**: V1 has Coinbase International + Deribit + OKX. Recommend open-interest-weighted average, median fallback when <2 venues report.
- **AV NEWS_SENTIMENT for crypto tickers**: untested on `CRYPTO:BTC`; shared free-tier rate budget (Tier 6 #31) already gates equity. Phase 3 should reuse the LLM sentiment shadow (Tier 5 #28) instead.
- **`asset_class` field rollout**: breaking change for anything reading universe by index. Coordinate `lthcs/peer_groups_loader.py` + V1 narratives loader in the same commit.
- **Score-band recalibration**: audit at 90 days; if crypto composites systematically sit above equity, tighten macro overlay magnitude rather than introduce a separate band table.
