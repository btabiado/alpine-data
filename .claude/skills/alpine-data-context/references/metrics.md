# Metrics & derivations

Formulas with `file:line`/`file:function` anchors. Verified against source at commit `f765438`. Line numbers may drift — grep the function name if they don't match.

---

## Money Flow Index family (`money_flow.py`)

### MFI — Money Flow Index (14-bar) · `money_flow.py:mfi` (≈L113)
- **Definition:** classic 14-period MFI, 0–100. >70 overbought, <30 oversold.
- **Formula:** typical price `TP = (H+L+C)/3`; raw money flow `MF = TP × Volume`; MF is *positive* when TP rises vs the prior bar, *negative* when it falls, *neither* when flat. Then:
  `MFI = 100 − 100 / (1 + Σ(positive MF) / Σ(negative MF))` over the last 14 bars.
- **Input:** cleaned OHLCV bars (`_clean_bars`, `_bar_ohlcv`); needs ≥ period+1 valid bars or returns `None`.

### CMF — Chaikin Money Flow (20-bar) · `money_flow.py:cmf` (≈L166)
- **Definition:** −1…+1. Positive = accumulation, negative = distribution.
- **Formula:** money-flow multiplier `MFM = ((C−L) − (H−C)) / (H−L)`; money-flow volume `MFV = MFM × Volume`; `CMF = Σ(MFV) / Σ(Volume)` over 20 bars.

### OBV — On-Balance Volume · `money_flow.py:obv` (≈L212)
- Cumulative: `+Volume` when close up, `−Volume` when down, unchanged when flat. Returns the full series (UI shows the trend).

### Money Flow Index composite (the ±100 gauge) · `money_flow.py:build_money_flow_index` (≈L361)
Design (see also `SPEC_money_flow_index.md`):
- **Per-index sub-score** (Dow / S&P / Nasdaq) = `mean( z(ETF ΔSO×NAV flow), z(MFI − 50) ) → ±100`.
  - `MFI − 50` centers MFI so neutral (50) maps to 0 before z-scoring (`_per_index_subscore` ≈L302, `_component_z` ≈L274).
  - z-score is mapped to ±100 via `_z_to_pm100` (≈L262).
- **Headline** = blend of the three per-index sub-scores + `z(ICI equity mutual-fund flow)` + `z(MMF cash, inverted)`, **renormalized** when a leg is missing (e.g. ETF leg not yet warmed up), then capped to ±100 with a **band label** (`_band_label` ≈L250).
- **Warm-up:** the ETF-flow leg needs ≥ 2 daily snapshots of shares-outstanding, so it is neutral for ~1 trading day after a cold start.

---

## ETF net flow (ΔSO×NAV) · `fetch_equity_etf_flows.py`
- **Formula:** `net_flow_t = (sharesOutstanding_t − sharesOutstanding_prev) × NAV_t`, in USD millions (`net_flow_musd`).
- **Source:** Yahoo `quoteSummary` (crumb-gated) for sharesOut + NAV; **fallback** Nasdaq quote where `sharesOut = MarketCap / price` and `NAV ≈ price`.
- **Store:** appended to `data/equity_etf_flows.csv`, columns `[date, ticker, shares_out, nav, price, net_flow_musd]`. **Needs the prior-day row** to compute today's delta — the committed CSV history is load-bearing.

---

## Asset signal (−100…+100) · `signals.py`
- **Entry points:** `compute_signal(asset, payload)` (≈L146), `compute_all(payload)` (≈L277); scoring core `_score_at(...)` (≈L79); label `_label(score)` (≈L136).
- **Weighted components** (`SIGNAL_WEIGHTS`, ≈L32):

  | Component | Weight | Direction |
  |-----------|-------:|-----------|
  | Price vs SMA50/SMA200 | 20 | trend-following |
  | RSI band (`_rsi`, period 14, ≈L56) | 15 | mean-revert at extremes |
  | MACD histogram sign (`_macd_hist` 12/26/9, ≈L68) | 10 | momentum |
  | Perp funding rate | 10 | **contrarian (inverted)** |
  | Fear & Greed | 10 | **contrarian (inverted)** |
  | 7-day ETF net flow | 10 | follow |
  | Deribit DVOL z-score | 5 | risk |
  | VIX z-score | 5 | macro risk-on/off |

- **Top-25 simplified signal:** `compute_signal_simple(coin)` (≈L338) / `compute_all_top20(...)` (≈L426); excludes stablecoins via `_is_stable_symbol` (USD-prefix, ≈L296).

---

## Other computed metrics

| Metric | Where | Note |
|--------|-------|------|
| **BTC/ETH whale sentiment** (±100) | `fetch_market.py` whale section | z-blend of exchange-outflow / avg-tx-value / active-addresses (7-day trailing); ETH leg uses Etherscan/CoinMetrics, Glassnode as premium layer |
| **Fear & Greed** | Alternative.me → `fetch_market.py` | trimmed to 1095 days at build (`FEAR_GREED_MAX_DAYS`); used inverted in the signal |
| **City pulse** | `city/pulse.py` | 50 = baseline; per-pillar normalized trailing-12-mo trend, polarity-corrected (permits↑ good, crime↓ good); see `METHODOLOGY_DISCLOSURES` in `fetch_city.py` for data-continuity breaks |
| **CPI / inflation subindexes** | `fetch_cpi.py` | passthrough FRED observations per category |
| **Central-bank gold / mine production** | `fetch_metals.py` | IMF SDMX → tonnes; USGS annual by country |
| **Supply-chain pressure (GSCPI), inventory/sales (ISRATIO)** | `fetch_supplies.py` | NY Fed CSV (keyless); ISRATIO needs FRED |
| **Insights** (cross-tab rules) | `insights.py` | heuristic rules over current vs prior market state; persists history |
| **Point of Control** | client-side in `app.py` | volume-weighted price level from OHLCV (no server formula) |

---

## Verifying a metric end-to-end
```bash
# 1. Find where it's computed
grep -n "def build_money_flow_index\|def mfi\|def cmf\|def compute_signal" ~/alpine-data/money_flow.py ~/alpine-data/signals.py
# 2. See the live output it produces
curl -s https://btabiado.github.io/alpine-data/data/market.json 2>/dev/null | jq '.money_flow.headline, .money_flow.by_index' 2>/dev/null \
  || echo "market.json is inline-only on live; inspect data/market.json after a local build"
```
> Note: `data/market.json` is gitignored and generated at build — to inspect it, run `fetch_market.py` locally or read the inline `DATA` blob in the built `dashboard.html`.
