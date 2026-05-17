# LTHCS Phase 1 / V1 — Build Specification

**Project:** Long-Term Hold Confidence Score, integrated as a new tab on the existing `btc-eth-etf-dashboard` GitHub Pages site.
**Build target:** End-to-end working V1 in 8–10 weeks of part-time effort, executed by Claude Code on Bryan's laptop.
**Status:** Greenfield. No prior LTHCS code exists in the repo.

---

## 1. Goals and non-goals

### Phase 1 goals

1. **Daily scoring pipeline in Python** that pulls free-tier data, computes a V1 LTHCS score for 75 US-listed tickers (25 S&P, 25 NASDAQ, 25 DOW), and persists structured snapshots.
2. **Standalone LTHCS tab** added to the existing dashboard, showing all 75 tickers with score, drift, pillar breakdown, history sparkline, and AI narrative.
3. **Ticker search + filters** (exchange, score band, drift direction).
4. **Detail modal** matching the reference UX from Figure 8 of the white paper (anchor score, 90-day chart, pillar breakdown bars, narrative, thesis-break list).
5. **All data on Bryan's laptop**, committed to the repo as JSON files. No database. No cloud. No backend server.
6. **Append-only history** — every daily snapshot persisted as its own immutable file. Git is the audit log.

### Phase 1 non-goals (deferred to later phases)

- V2 adaptive weights, V3 confidence engine (V1 uses fixed weights from §5.2 of the white paper)
- LLM-generated narratives (V1 uses templated narratives grounded in component deltas)
- Crypto coverage on the LTHCS tab (existing crypto tabs remain unchanged)
- Real-time / intraday scoring (V1 is end-of-day, daily refresh)
- MCP server / API exposure (Phase 2+, per §23 of the white paper)
- Backtest engine (Phase 3)
- Premium data sources (Polygon, FMP paid tier, Glassnode — all Phase 2+)

---

## 2. Universe — 75 tickers

The universe is fixed at V1. It is hand-curated to exercise the framework across sectors, maturity stages, and the special frameworks (Pre-Profit, Recovery). It lives at `data/lthcs/universe.json`.

### S&P 25 (large-cap diversified)
AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA, BRK.B, LLY, JPM, V, UNH, XOM, MA, JNJ, PG, HD, COST, MRK, ABBV, AVGO, ORCL, CVX, KO, BAC

### NASDAQ 25 (Nasdaq-100 names not in S&P 25 above)
ADBE, AMD, INTC, INTU, ISRG, ADP, NFLX, CMCSA, PEP, QCOM, TXN, AMAT, BKNG, MU, GILD, PYPL, REGN, VRTX, LRCX, MDLZ, KLAC, SBUX, PANW, CDNS, SNPS

### DOW 25 (Dow 30 names not already in the lists above + close adjacencies)
CRM, CAT, MCD, AXP, HON, AMGN, BA, IBM, TRV, NKE, DIS, MMM, DOW, WMT, CSCO, VZ, GS, WBA, SHW, LMT, DE, RTX, F, GE, LCID

Note: LCID is added deliberately as a Pre-Profit Growth test case (see §5.5 of the white paper). INTC is in the NASDAQ list as a Recovery framework test case.

### Universe file schema (`data/lthcs/universe.json`)

```json
{
  "version": "1.0.0",
  "last_updated": "2026-05-16",
  "tickers": [
    {
      "ticker": "AAPL",
      "name": "Apple Inc.",
      "exchange": "NASDAQ",
      "index_membership": ["S&P 500", "NASDAQ-100", "DJIA"],
      "sector": "Technology",
      "industry": "Consumer Electronics",
      "maturity_stage": "standard_compounder",
      "active": true
    }
  ]
}
```

`maturity_stage` is one of: `pre_revenue`, `pre_profit_growth`, `path_to_profitability`, `profitability_inflection`, `standard_compounder`, `recovery_stabilization`, `recovery_operational`, `recovery_earnings`, `recovery_rerating`. Defaults to `standard_compounder` for V1 unless explicitly tagged (LCID = `pre_profit_growth`, INTC = `recovery_stabilization`).

---

## 3. Storage architecture

### Three layers, all on Bryan's laptop

**Layer 1 — Committed JSON files** under `data/lthcs/`:
- `universe.json` — the 75 tickers
- `weights.json` — pillar weights by maturity stage
- `snapshots/YYYY-MM-DD.json` — daily score for all 75 names (append-only)
- `variable_detail/YYYY-MM-DD.json` — variable-level inputs (append-only)
- `narratives/YYYY-MM-DD.json` — templated narratives per ticker
- `history/by_ticker/<TICKER>.json` — rolling 365-day history, rebuilt daily from snapshots

These are committed to git and served as static assets at `btabiado.github.io/btc-eth-etf-dashboard/data/lthcs/...`.

**Layer 2 — Browser localStorage** (per user):
- `lthcs.starred` — array of ticker symbols the user starred
- `lthcs.lastFilter` — last applied filter state
- `lthcs.lastTicker` — last viewed ticker (restore on reload)

Never holds score data. Just user preferences.

**Layer 3 — Local cache** under `.cache/lthcs/` (gitignored):
- Raw API responses, keyed by `<source>/<cache_key>.json`
- TTL per source (e.g., FRED CPI monthly = 30d, Alpha Vantage daily = 24h)

`.cache/` MUST be added to `.gitignore` before first commit. It contains raw vendor data that may be subject to redistribution restrictions.

### Why no database

GitHub Pages cannot run a database. JSON files served as static assets are fast, cacheable by CDN, and zero operational burden. The append-only daily-snapshot model is the right architecture for a historical confidence graph: every score the system has ever produced lives in git history, with cryptographic integrity and a free audit log.

If V1 outgrows this (unlikely before Phase 3), the migration to BigQuery or Postgres is straightforward: each JSON snapshot maps 1:1 to a row insert.

---

## 4. Data sources for V1 (all free tier)

| Source | Use | Auth | Rate limit | Cache TTL |
|---|---|---|---|---|
| Alpha Vantage | Daily prices, market news sentiment | API key | 25 req/day | 24h |
| FRED | CPI, Fed Funds, 10Y Treasury, unemployment, retail sales | API key | none meaningful | 24h |
| EIA | WTI, Brent, gasoline | API key | none meaningful | 24h |
| SEC EDGAR | Company facts (revenue, margins, cash flow) via XBRL | none | 10 req/sec | 7d |
| Yahoo Finance | Fallback for prices via `yfinance` | none | scrape-based, fragile | 24h |

**Critical:** Alpha Vantage's 25 req/day cap means we can't fetch all 75 tickers daily from Alpha Vantage alone. V1 uses Yahoo Finance (via `yfinance`) for daily prices/volume, and reserves Alpha Vantage for news sentiment (one batch call per day) and as a price fallback when Yahoo fails.

API keys live in `.env`:

```
ALPHA_VANTAGE_API_KEY=xxx
FRED_API_KEY=xxx
EIA_API_KEY=xxx
```

Provide a `.env.example` with placeholder values; the user copies to `.env` and fills in.

---

## 5. V1 scoring formula

Per §5.2 of the white paper. Implemented in `lthcs/score.py`.

```
LTHCS_v1 = (Adoption Momentum × 0.25)
         + (Institutional Confidence × 0.20)
         + (Financial Evolution × 0.15)
         + (Thesis Integrity × 0.20)
         + (DES × 0.20)
         + Macro Adjustments
         + Sector Adjustments
         + Volatility Modifiers
         , capped at [0, 100]
```

For Pre-Profit Growth maturity stage (e.g., LCID), substitute the weights from §5.5: `30/20/15/20/15`.
For Recovery Stabilization (e.g., INTC), substitute the weights from §5.6: heavier on Financial Evolution.

Each pillar produces a 0–100 sub-score. V1 implementations:

### Adoption Momentum (`lthcs/pillars/adoption.py`)
- Revenue growth YoY (Yahoo / SEC EDGAR): peer-relative percentile → 0–100
- Search interest acceleration (Google Trends via `pytrends`): trend slope vs trailing 90d → 0–100
- For V1, combine with 60/40 weight

### Institutional Confidence (`lthcs/pillars/institutional.py`)
- Trailing 90-day price momentum (Yahoo): percentile vs S&P 500 universe → 0–100
- 13F institutional ownership change (SEC EDGAR Form 13F): QoQ delta → 0–100
- For V1, combine with 70/30 weight (13F is quarterly; momentum is the live signal)

### Financial Evolution (`lthcs/pillars/financial.py`)
- Revenue growth (SEC EDGAR XBRL): YoY → 0–100 percentile
- Gross margin trajectory: trailing-4-quarter trend → 0–100
- Operating cash flow positivity: binary 0/100, scaled by magnitude → 0–100
- Combine with 40/30/30 weight

### Thesis Integrity (`lthcs/pillars/thesis.py`)
- Alpha Vantage news sentiment (rolling 30-day average): map [-1, 1] → [0, 100]
- V1 keeps this single-source; Phase 2 adds moat scoring + topic velocity

### Demand Environment Score / DES (`lthcs/pillars/des.py`)
- Per §6.1 formula
- Inputs from FRED (CPI, Fed Funds, 10Y) and EIA (oil)
- Sector-specific weighting (EV gets positive oil tilt, banks get rate tilt, etc.)

### Modifiers
- **Macro adjustment**: if 10Y Treasury moves > 25bp in 30 days, apply ±2 points
- **Sector adjustment**: per-sector factor table (Phase 1 has 11 GICS sectors)
- **Volatility modifier**: if trailing 30-day price volatility > 90th percentile of S&P 500, subtract 3 points (penalize unstable regimes)

All sub-scores, weights, and modifier contributions are persisted in `variable_detail/YYYY-MM-DD.json` for explainability.

---

## 6. Daily pipeline (`lthcs_daily.py`)

Per §11 of the white paper. Eight stages, each writing to a persisted artifact.

```
Stage 1: Load universe + weights config
Stage 2: Fetch raw data per source (with caching + rate-limit)
Stage 3: Data quality checks (freshness, nulls, outliers)
Stage 4: Normalize raw values to 0–100 sub-scores per pillar
Stage 5: Apply sector + macro + volatility modifiers
Stage 6: Calculate final LTHCS score, cap [0, 100], assign band + drift
Stage 7: Generate templated narratives
Stage 8: Persist snapshot, variable detail, narratives; rebuild per-ticker history
```

CLI:

```bash
python lthcs_daily.py                       # Full run, all 75 tickers
python lthcs_daily.py --tickers AAPL,LCID   # Subset
python lthcs_daily.py --dry-run             # Compute but don't write
python lthcs_daily.py --force               # Overwrite today's snapshot if exists
python lthcs_daily.py --stage 4             # Run from stage 4 onward (uses cached upstream)
```

Each stage logs one line:

```
✓ Stage 1: Loaded 75 tickers, 9 maturity-stage weight profiles
✓ Stage 2: Fetched 75/75 from Yahoo, 1/1 from Alpha Vantage news, 5/5 FRED, 3/3 EIA, 75/75 EDGAR  (cache hits: 64)
✓ Stage 3: Quality checks passed; 73/75 with full data, 2 flagged (BRK.B no XBRL, CRM stale earnings)
✓ Stage 4: Sub-scores computed for 75 tickers across 5 pillars
✓ Stage 5: Modifiers applied; volatility penalty fired for 3 tickers
✓ Stage 6: Scores computed; band distribution: Elite 2, High 14, Constructive 31, Monitor 19, Weakening 7, Review 2
✓ Stage 7: Generated 75 templated narratives
✓ Stage 8: Wrote data/lthcs/snapshots/2026-05-16.json (75 entries)
✓ Stage 8: Wrote data/lthcs/variable_detail/2026-05-16.json
✓ Stage 8: Wrote data/lthcs/narratives/2026-05-16.json
✓ Stage 8: Rebuilt 75 per-ticker history files
Done in 47.2s.
```

---

## 7. Snapshot schemas

### `data/lthcs/snapshots/YYYY-MM-DD.json`

```json
{
  "calc_date": "2026-05-16",
  "model_version": "v1.0.0",
  "weights_profile_default": "standard_compounder",
  "scores": [
    {
      "ticker": "AAPL",
      "lthcs_score": 82.4,
      "band": "high_confidence",
      "drift_1d": 0.3,
      "drift_7d": 1.2,
      "drift_30d": -0.8,
      "drift_90d": 2.4,
      "confidence_level": "high",
      "data_quality_flags": [],
      "subscores": {
        "adoption_momentum": 78.0,
        "institutional_confidence": 85.0,
        "financial_evolution": 80.0,
        "thesis_integrity": 76.0,
        "des": 88.0
      },
      "modifiers": {
        "macro_adj": 0.0,
        "sector_adj": 1.0,
        "volatility_mod": 0.0
      },
      "maturity_stage": "standard_compounder",
      "weights_used": [0.25, 0.20, 0.15, 0.20, 0.20]
    }
  ]
}
```

### `data/lthcs/variable_detail/YYYY-MM-DD.json`

One entry per (ticker, variable). Used by the detail modal to explain *why* each sub-score is what it is.

```json
{
  "calc_date": "2026-05-16",
  "model_version": "v1.0.0",
  "variables": [
    {
      "ticker": "AAPL",
      "variable_name": "revenue_growth_yoy",
      "pillar": "adoption_momentum",
      "raw_value": 0.082,
      "normalized_value": 65.0,
      "weight_within_pillar": 0.60,
      "contribution_to_pillar": 39.0,
      "source": "yfinance",
      "source_quality_score": 0.85,
      "as_of": "2026-04-30"
    }
  ]
}
```

### `data/lthcs/narratives/YYYY-MM-DD.json`

```json
{
  "calc_date": "2026-05-16",
  "model_version": "v1.0.0",
  "narratives": [
    {
      "ticker": "AAPL",
      "todays_take": "AAPL holds in High Confidence with score 82.4, supported by strong institutional confidence and constructive demand environment.",
      "why_changed": "Score is up 0.3 today, driven primarily by a +2 move in DES on softer 10Y yields.",
      "why_not_to_sell": "Recent intraday weakness appears volatility-driven; institutional and financial pillars remain firmly in the upper band.",
      "what_would_break": "A sustained move below 75 on financial evolution, or a thesis integrity drop below 60 on negative product cycle commentary, would force a structural review.",
      "confidence_level": "high"
    }
  ]
}
```

### `data/lthcs/history/by_ticker/AAPL.json`

```json
{
  "ticker": "AAPL",
  "model_version": "v1.0.0",
  "history": [
    {"date": "2026-05-16", "score": 82.4, "band": "high_confidence"},
    {"date": "2026-05-15", "score": 82.1, "band": "high_confidence"}
  ]
}
```

Capped at 365 entries; older data lives in the daily snapshot files.

### `data/lthcs/weights.json`

```json
{
  "version": "1.0.0",
  "profiles": {
    "standard_compounder":      [0.25, 0.20, 0.15, 0.20, 0.20],
    "pre_revenue":              [0.35, 0.20, 0.05, 0.20, 0.20],
    "pre_profit_growth":        [0.30, 0.20, 0.15, 0.20, 0.15],
    "path_to_profitability":    [0.25, 0.20, 0.20, 0.20, 0.15],
    "profitability_inflection": [0.25, 0.20, 0.20, 0.20, 0.15],
    "recovery_stabilization":   [0.15, 0.15, 0.35, 0.20, 0.15],
    "recovery_operational":     [0.20, 0.20, 0.30, 0.15, 0.15],
    "recovery_earnings":        [0.20, 0.25, 0.25, 0.15, 0.15],
    "recovery_rerating":        [0.25, 0.20, 0.15, 0.20, 0.20]
  },
  "pillar_order": ["adoption_momentum","institutional_confidence","financial_evolution","thesis_integrity","des"]
}
```

---

## 8. Client-side LTHCS tab

### Integration into `index.html`

Add a new tab button to the existing nav, between `Stocks` and `AI News`:

```html
<button class="tab-btn" data-tab="lthcs">LTHCS</button>
```

Add the tab content panel below the existing tabs:

```html
<section id="tab-lthcs" class="tab-panel">
  <!-- LTHCS tab content goes here -->
</section>
```

Load the LTHCS JS modules at the end of `index.html`:

```html
<script type="module" src="js/lthcs/lthcs-tab.js"></script>
<link rel="stylesheet" href="css/lthcs.css">
```

### Tab structure (top to bottom)

1. **Header bar** — title "LTHCS · Long-Term Hold Confidence Score", last-updated timestamp, model version
2. **Sentiment summary** (matches existing dashboard style) — total in each band, average drift, count of active thesis-break flags
3. **Search bar** — instant filter by ticker symbol or company name (matches existing search styling)
4. **Filter chips** — Exchange (S&P / NASDAQ / DOW), Score band (Elite, High, Constructive, Monitor, Weakening, Review), Drift (improving / stable / declining)
5. **Score card grid** — 75 cards, one per ticker. Each card shows: ticker, company name, score (large), band color, 30-day mini-sparkline, drift arrow, top contributing pillar
6. **Click any card → detail modal** — full pillar breakdown, 90-day chart, AI narrative, thesis-break list, variable-level evidence table

### Card layout

```
┌─────────────────────────────────┐
│ AAPL                       82.4 │
│ Apple Inc.        ▁▃▅▆▇▇▆▇   ↑  │
│ HIGH CONFIDENCE                 │
│ Top driver: Institutional 85    │
└─────────────────────────────────┘
```

Card colors follow §5.9 band colors:
- Elite (90-100): navy `#1F3A5F`
- High (80-89): green-tinted `#4A8F5F`
- Constructive (70-79): gold `#C9A227`
- Monitor (60-69): warm `#D89148`
- Weakening (50-59): rust `#B85A3E`
- Review (<50): deep rust `#7A2E1F`

### Detail modal (`lthcs-detail.js`)

Matches Figure 8 of the white paper:
- **Header**: ticker, score (huge), band, drift over 1d/7d/30d/90d
- **90-day score chart** (line + score-band shading)
- **Pillar breakdown** (5 horizontal bars, sub-score + weight + contribution)
- **AI narrative card** (4 templated paragraphs from `narratives/`)
- **Thesis-break watchlist** (active flags, if any)
- **Variable detail table** (every variable, raw value, normalized, contribution, source) — collapsed by default, "Show details" toggle

### Data fetching

On tab load:
1. Fetch `data/lthcs/snapshots/<latest>.json` — gets all 75 current scores
2. Don't fetch history or narratives until user clicks a card (lazy)
3. Cache the snapshot in memory; refetch only when user clicks "Refresh"

`latest` is resolved by fetching `data/lthcs/snapshots/index.json`, which is rebuilt by the Python pipeline each run and lists all available snapshot dates.

---

## 9. Templated narrative generation

V1 does not call an LLM. Narratives are generated by templated rules in `lthcs/narratives.py`. Templates are grounded in component deltas from `variable_detail/`.

Four narratives per ticker, matching §12 of the white paper:

1. **Today's LTHCS Take** — `"{ticker} {band_action} at {score} ({drift_30d:+.1f} over 30 days), supported by {top_pillar} at {top_pillar_score}."`
2. **Why Score Changed** — `"Score is {direction} {abs(drift_1d):.1f} today, driven primarily by a {top_driver_delta:+.1f} move in {top_driver_pillar}."`
3. **Why Not to Sell / Why to Review** — chosen based on band; conviction guardrail language for top bands, review framing for lower
4. **What Would Break the Thesis** — references the next-band-down threshold and the weakest pillar

LLM-generated narratives are Phase 2.

---

## 10. Validation and testing

### Unit tests (`tests/`)

- `test_normalize.py` — percentile / z-score helpers produce [0, 100]
- `test_pillars.py` — each pillar with known inputs produces expected sub-scores
- `test_score.py` — combiner respects weights, applies modifiers, caps to [0, 100]
- `test_schemas.py` — every produced JSON validates against its schema
- `test_pipeline.py` — full pipeline on 3 fixture tickers produces expected output

### Smoke test command

```bash
python lthcs_daily.py --tickers AAPL,LCID,INTC --dry-run --verbose
```

Expected: AAPL in High Confidence, LCID in Monitor or Weakening (Pre-Profit weighting penalizes weak Financial Evolution), INTC in Review (Recovery weighting + active thesis-break flags).

### Validation gate

`python -m lthcs.validate --date YYYY-MM-DD` checks:
- Snapshot file exists and parses
- All 75 universe tickers have a score (or are explicitly flagged inactive)
- Every score is in [0, 100]
- Every sub-score is in [0, 100]
- Variable detail has at least one variable per pillar per ticker
- Narratives exist for every ticker in the snapshot
- History file for each ticker has today's entry

If any check fails, the daily run exits non-zero and the snapshot is *not* committed.

---

## 11. Milestone plan — 10 weeks

Each milestone produces a working, testable artifact. Don't skip ahead.

### Week 1 — Foundation
- Repo structure created, `.gitignore` updated
- `.env.example` written, README updated with setup steps
- `data/lthcs/universe.json` populated with all 75 tickers
- `data/lthcs/weights.json` populated with all maturity-stage profiles
- pydantic schemas for all JSON files
- Validation command (`python -m lthcs.validate`) skeleton that checks universe + weights schemas

**Done when:** `python -m lthcs.validate` passes on universe.json and weights.json.

### Week 2 — Data source clients
- `lthcs/sources/_cache.py` — file-based cache with TTL
- `lthcs/sources/_ratelimit.py` — token-bucket rate limiter
- `lthcs/sources/yahoo.py` — daily prices, volume, volatility via yfinance
- `lthcs/sources/fred.py` — CPI, Fed Funds, 10Y, unemployment
- `lthcs/sources/eia.py` — WTI, Brent, gasoline
- `lthcs/sources/sec_edgar.py` — XBRL company facts (revenue, margins, cash flow)
- `lthcs/sources/alpha_vantage.py` — news sentiment (batch endpoint)
- Smoke test: pull data for AAPL from all sources, write fixtures

**Done when:** `python -m lthcs.sources.smoke AAPL` returns clean data from all 5 sources.

### Week 3 — Normalization + Adoption pillar
- `lthcs/normalize.py` — percentile, z-score, peer-relative helpers
- `lthcs/pillars/adoption.py` — revenue growth + search interest combined
- Unit tests for normalize and adoption
- Run pillar on all 75 tickers as a one-off; eyeball the distribution

**Done when:** Adoption sub-scores look sensible (mega-caps mid-pack, high-growth tech in top quartile, mature defensives lower).

### Week 4 — Institutional + Financial pillars
- `lthcs/pillars/institutional.py` — momentum + 13F change
- `lthcs/pillars/financial.py` — revenue growth + margin trend + cash flow
- Unit tests
- Distribution check on all 75 tickers

**Done when:** Both pillars produce sensible distributions. INTC's Financial Evolution should be visibly weak.

### Week 5 — Thesis Integrity + DES
- `lthcs/pillars/thesis.py` — Alpha Vantage news sentiment, 30-day rolling
- `lthcs/pillars/des.py` — DES formula from §6.1, sector-specific weights
- Unit tests
- Sector-weighting table (`data/lthcs/sector_des_weights.json`) for 11 GICS sectors

**Done when:** TSLA's DES should be sensitive to oil price; banks should be sensitive to rates; LCID should pick up EV-favorable signals.

### Week 6 — Score combiner + modifiers + narratives
- `lthcs/score.py` — combine pillars by maturity-stage weights, apply modifiers, cap [0, 100], assign band + drift
- `lthcs/narratives.py` — templated narrative generator
- Full pipeline end-to-end on 3 tickers (AAPL, LCID, INTC)
- Snapshot, variable detail, narrative files written and validated

**Done when:** `python lthcs_daily.py --tickers AAPL,LCID,INTC` produces three validated snapshots with the expected band placements.

### Week 7 — Full pipeline + history
- `lthcs/persist.py` — write snapshots, variable detail, narratives, rebuild per-ticker history
- `lthcs_daily.py` — full CLI with all flags
- Run for all 75 tickers daily for a week to build initial history
- Manual commit + push each day to populate the public data files

**Done when:** 7 days of snapshots exist, history files have 7 entries per ticker, all validation passes.

### Week 8 — LTHCS tab UI (cards + filters)
- New tab added to `index.html`, navigation wired up
- `js/lthcs/lthcs-tab.js` — main controller, fetches latest snapshot
- `js/lthcs/lthcs-cards.js` — renders all 75 cards in band-color grid
- `js/lthcs/lthcs-search.js` — instant ticker / name search
- `js/lthcs/lthcs-filters.js` — exchange, band, drift filters
- `css/lthcs.css` — scoped styles matching existing dashboard aesthetic

**Done when:** All 75 cards render with correct band colors, search and filters work, no console errors.

### Week 9 — Detail modal + chart
- `js/lthcs/lthcs-detail.js` — modal opens on card click
- 90-day score chart with band shading (use Chart.js if already in the dashboard; otherwise plain SVG)
- Pillar breakdown bars
- Narrative card
- Variable detail table (collapsible)

**Done when:** Clicking AAPL shows a complete detail view matching Figure 8 of the white paper.

### Week 10 — Polish, docs, deploy
- README updated with full setup + daily-run instructions
- "About LTHCS" info modal added (links to white paper, explains methodology)
- Disclaimer footer ("Not investment advice")
- Final visual polish; test on mobile / Safari / Firefox
- First production commit + push; verify the tab works on `btabiado.github.io/btc-eth-etf-dashboard`

**Done when:** The tab is live, works on the public URL, and a fresh git clone + `python lthcs_daily.py` produces today's snapshot end-to-end.

---

## 12. What Bryan does daily

Once V1 is live, the daily workflow is:

```bash
cd ~/Documents/btc-eth-etf-dashboard         # or wherever the repo lives
python lthcs_daily.py                         # ~45-60 seconds
git add data/lthcs/                           # stage new snapshot files
git commit -m "lthcs: daily snapshot 2026-05-16"
git push                                      # GitHub Pages picks it up in ~1 min
```

That's the entire operating model. No server. No database. No cron. No cloud bill. Bryan runs one command, commits, pushes. The dashboard updates itself.

If Bryan wants this automated, Week 11 (out of scope for V1) can add a `launchd` job on macOS that runs the daily script and auto-commits. But manual is fine for V1 — it keeps Bryan in the loop on each day's scores during the validation period.

---

## 13. What success looks like at the end of Phase 1

1. **`btabiado.github.io/btc-eth-etf-dashboard` has a new "LTHCS" tab** that loads instantly and shows 75 scored tickers.
2. **`data/lthcs/snapshots/` has 60+ daily files**, one per business day since launch.
3. **Click any ticker → modal with pillar breakdown, history, narrative, variables** — matches Figure 8.
4. **Three teaching cases work correctly**: AAPL in High Confidence, LCID in Monitor/Weakening with Pre-Profit weighting flagged, INTC in Review with Recovery weighting + thesis-break flags.
5. **Bryan can demo this end-to-end** in under 5 minutes to an advisor or investor as proof the framework works on real data.

That's V1. Phase 2 adds adaptive weights, real-time data, the MCP server, and the AI narrative LLM. But V1 has to ship first, and V1 is built end-to-end as specified in this document.
