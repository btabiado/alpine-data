# LTHCS — README

**Long-Term Hold Confidence Score.** A daily 0–100 score for every active ticker in [`data/lthcs/universe.json`](data/lthcs/universe.json), published as JSON files in the repo and rendered by the `/lthcs/` pages of the `alpine-data` GitHub Pages site.

🌐 **Live URL:** https://btabiado.github.io/alpine-data/lthcs/

## Current state (2026-10)

- **Universe:** the S&P 500 and Dow 30, plus NASDAQ-100 names and Index Exiles (names that left every tracked index but are still scored): 519 tickers, 515 active. The file is the source of truth; pages read the count from it.
- **Bands:** `data/lthcs/weights.json` → `score_bands`, recalibrated 2026-10-06: elite 70–100, high_confidence 63–69, constructive 52–62, monitor 42–51, weakening 33–41, review 0–32. Crypto scores use the same bands.
- **Runs:** in GitHub Actions; see [Automation schedule](#automation-schedule-github-actions). Nobody runs the pipeline by hand for production.
- **Thesis pillar:** dropped for every name today. The daily job passes `--skip-thesis` and no `FINNHUB_API_KEY` secret is set, so its weight is spread over the other four pillars. The About modal on `/lthcs/` shows live per-pillar coverage.

This README covers setup and operation. Repo-wide conventions for agents are in [`SKILL.md`](SKILL.md). [`PHASE_1_BUILD_SPEC.md`](PHASE_1_BUILD_SPEC.md) is the original May 2026 plan and is kept for history; its universe, bands and layout are out of date.

The sections from "V1 status" to "Phase 1–4 ship summary" are a record of May 2026 and are left as written.

---

## V1 status (2026-05-20)

All 10 weeks of the build plan shipped. The framework runs end-to-end, and Phases 1–4 of the post-V1 build queue are live (see "Phase 1–4 ship summary" below). The framework runs end-to-end:

| Week | Module | Status |
|---|---|---|
| 1 | Schemas + validate gate | ✅ |
| 2 | 5 source clients (Yahoo, SEC EDGAR, FRED, EIA, Alpha Vantage) | ✅ |
| 3 | normalize + Adoption pillar | ✅ |
| 4 | Institutional + Financial pillars | ✅ |
| 5 | Thesis + DES pillars | ✅ |
| 6 | Score combiner + templated narratives | ✅ |
| 7 | Daily pipeline (`lthcs_daily.py`) + JSON persistence | ✅ |
| 8 | Tab UI (cards grid + search + filters) | ✅ |
| 9 | Detail modal + 90-day SVG sparkline + variable detail | ✅ |
| 10 | About modal + README updates + polish | ✅ |

**Universe:** 75 entries / 74 active. WBA marked inactive (Walgreens taken private late 2025).

**V1 limitations honestly disclosed in the About modal** (as of 2026-05-20):
- Thesis pillar now sources from **Finnhub `/news-sentiment`** (commit `10daa39`) — coverage jumped from 145 → 166 tickers vs the prior Alpha Vantage `NEWS_SENTIMENT` rotation. AV's free-tier `NEWS_SENTIMENT` is retained as a degraded fallback; an AND-not-OR multi-ticker quirk in that endpoint drove the migration.
- Google Trends acceleration (40% of Adoption) ships partial coverage via the weekly batch (~11/167 names); Phase 2 upgrade to a non-rate-limited source is queued (Tier 2 #14).
- 13F holdings change (Institutional) — **Phase 1 + Phase 2 SHIPPED** (commits `29f2140`, `a27823f`). CUSIP coverage 50 → 169, then AUM-weighted across 113 managers. Phase 3 (finer manager-weighting heuristics) still open.
- Banks: **bank-cohort revenue ranking SHIPPED** (`e793a6b`) — JPM Financial Evolution sub-pillar 27 → 100 once benchmarked against the bank cohort instead of universe-wide.
- **Hotfixes 2026-05-19/20**: drift_30d universe-wide zero bug fixed (`58659dd`), AZN IFRS / foreign-issuer 20-F fallback (`0696853`), DES per-sector outlier z-score (`06d4af0`), HW/SW compound peer-group key (`eca7560`).

---

## Phase 1–4 ship summary (2026-05-19 → 2026-05-20)

The framework grew substantially across two days. Headline deliverables:

### Live routes (in addition to `/lthcs/`)

| Route | What | Commit |
|---|---|---|
| `/lthcs/crypto/` | Crypto pillar dashboard — 10-asset universe (BTC, ETH, SOL, ADA, AVAX, DOT, LINK, POL, XRP, DOGE) scored daily | `5842149` + `88912bb` |
| `/lthcs/backtest/ab.html` | A/B comparison of strategy variants — GG's tweak validated at **+0.184 Sharpe** vs baseline | `5381c69` |
| `/lthcs/position/` | Position-sizing helper (Kelly + band-aware sizing) | `3814f99` |
| `/lthcs/health/quality.html` | Monthly quality-audit status page | `68b43d6` |
| `/lthcs/health/pipeline.html` | Pipeline freshness + cron observability | `d1eaf0d` |

### Data-layer improvements

- **Thesis: Finnhub `/news-sentiment` migration** (`10daa39`) — coverage 145 → 166 tickers, replaces the AV `NEWS_SENTIMENT` rotation that suffered the AND-not-OR multi-ticker bug.
- **Crypto universe expansion** (`88912bb`) — 3 → 10 large-cap assets, scored daily by `scripts/lthcs_crypto_daily.py` (now at 11:47 UTC; see the schedule below).
- **13F Phase 2** (`a27823f`) — 113 managers, AUM-weighted ranking; Phase 1 (`29f2140`) had already taken CUSIP coverage 50 → 169.
- **Bank-cohort revenue ranking** (`e793a6b`) — JPM Financial Evolution sub-pillar 27 → 100.
- **HW/SW compound peer-group key** (`eca7560`) — `(maturity_stage, sector_group, tech_sub_bucket)` resolves the bimodal Tech-compounder cohort flagged in `peer-group-audit §3.4`.

### Backtest engine (Tier 5 #24, Phases 1–3 + follow-ons)

- Phase 1: non-overlapping P&L (`a996ad3`)
- Phase 2: per-pillar attribution (`eb0d5db`)
- Phase 3: strategy variants — `dollar_neutral` surfaces at **+3.1 Sharpe** (`9e13452`)
- Sharpe/Sortino 95% CIs via block bootstrap (`afabbb1`) — `afab1b9`
- A/B comparison view (`5381c69`)
- Phase 4 plumbing (`306176a`): `walk_forward_tune_equity` ingests engine equity curves; promotion gate time-locked to ~July 2026.

### LLM shadows (Tier 5 #23 + #28)

CI sets `LTHCS_LLM_SENTIMENT_ENABLED=1` + `LTHCS_LLM_NARRATIVES_ENABLED=1` in `lthcs-daily.yml` (`c8b74c1`). **Neither shadow runs today:**

- The `ANTHROPIC_API_KEY` repo secret is unset. Without it, both modules log and skip.
- The daily job passes `--skip-thesis`, so the sentiment shadow stays off even with a key.

The daily cost caps were scaled to the 515-ticker universe (2026-10): narratives `$6.17` (`LTHCS_LLM_NARRATIVES_MAX_USD_PER_DAY`) and sentiment `$3.08` (`LTHCS_LLM_SENTIMENT_MAX_USD_PER_DAY`). Both use Haiku 4.5 with prompt caching.

### UI niceties (Phase 4 polish)

- Custom watchlists in card view (`d81263d`)
- Side-by-side ticker comparison, 2–4 tickers (`173ed19`)
- V1↔LLM narratives toggle in the detail modal (`5ebf973`)
- Dragging-pillar callout in detail modal (`014aadc`)

### P0 cron fix (2026-05-20)

`requirements.txt` now gates `mcp[cli]>=1.0` to `python_version >= "3.10"` (`8d373a9`) — unblocks scheduled workflows that were failing the pip install on Python 3.9 runner images.

---

## First-time setup (one-time, ~20 minutes)

### 1. Clone the repo locally

```bash
cd ~/Documents          # or wherever you keep code
git clone https://github.com/btabiado/alpine-data.git
cd alpine-data
```

### 2. Get free API keys (10 minutes total)

| Service | Where | Time |
|---|---|---|
| Alpha Vantage | https://www.alphavantage.co/support/#api-key | 1 min |
| FRED | https://fred.stlouisfed.org/docs/api/api_key.html | 5 min (email verify) |
| EIA | https://www.eia.gov/opendata/register.php | 2 min |
| SEC EDGAR | No key needed — just set a User-Agent string |  |

### 3. Create `.env`

```bash
cp .env.example .env
# Then edit .env and paste your keys
```

`.env` is gitignored. Never commit it.

### 4. Install Python dependencies

```bash
python -m venv .venv
source .venv/bin/activate                  # macOS / Linux
pip install -r requirements.txt
```

For tests and local tools, also `pip install -r requirements-dev.txt`.

### 5. Verify the install

```bash
python -m lthcs.validate
```

It checks `universe.json` and `weights.json` against their schemas and prints one ✓ line per check, for example `✓ universe.json valid (519 tickers, 515 active, ...)`.

### 6. Smoke-test the pipeline on 3 tickers

```bash
python lthcs_daily.py --tickers AAPL,LCID,INTC --dry-run
```

It prints one `✓` line per stage and ends with three computed scores. `--dry-run` writes nothing. Without it, the run writes `data/lthcs/snapshots/<today>.json` with only these three tickers. Don't commit that: production snapshots come only from the scheduled `lthcs-daily.yml` run.

### 7. View it locally

```bash
python -m http.server 8000
# Open http://localhost:8000/lthcs_tab/ (the card view; data loads from ../data/lthcs/)
```

Click any card to open the detail modal.

### 8. Production

There is no manual push step. `main` is protected, and the scheduled workflows commit data through `.github/scripts/api-commit.sh`. Code changes go through a pull request, and `pages.yml` deploys `main`.

---

## Running the pipeline locally

Production runs in GitHub Actions (below). A local full run is for development only, and takes over an hour at 515 tickers because SEC EDGAR is rate-limited:

```bash
python lthcs_daily.py --dry-run             # all active tickers, nothing written
```

CLI flags (see `python lthcs_daily.py --help` for the full list):

| Flag | Purpose |
|---|---|
| `--tickers AAPL,NVDA` | Restrict to a subset (default: all active in `universe.json`). |
| `--force` | Overwrite today's snapshot/narratives/variable_detail if present. |
| `--catch-up` | Forward-fill days the cron missed (no snapshot file) between each active ticker's last history entry and today, as `synthetic: true` copies of its last score. Never past a run that did not score the ticker, nor from a score older than its universe `added_on`. |
| `--skip-thesis` | Bypass Alpha Vantage (Thesis falls back to Finnhub or neutral 50). |
| `--news-only` | Hourly path: refresh news-derived inputs only (Finnhub recommendations, SEC 8-K, Yahoo earnings, sector RSS). Re-emits today's snapshot with a refreshed Thesis sub-score and recomputed composite. Requires today's snapshot to already exist. Used by the `lthcs-news-hourly.yml` workflow. |
| `--as-of YYYY-MM-DD` | Backfill mode: compute the pipeline as if it were the given date. |
| `--dry-run` | Compute everything but skip persistence. |

If a scheduled day is missed, the gap stays visible in the history files and the pages show the most recent snapshot with its own date.

### Automation schedule (GitHub Actions)

In production, the dashboard refreshes itself without you running anything locally. These workflows live in `.github/workflows/`:

| Workflow | Cadence | What it does | Sources touched |
|---|---|---|---|
| `lthcs-daily.yml` | Daily, 23:00 UTC | Full pipeline (`lthcs_daily.py --catch-up --skip-thesis`). Computes every pillar, writes the canonical daily snapshot, appends each ticker's history entry, refreshes the macro / breadth / index files. `LTHCS_LLM_SENTIMENT_ENABLED=1` + `LTHCS_LLM_NARRATIVES_ENABLED=1` shadows run when `ANTHROPIC_API_KEY` is set (`c8b74c1`). | All sources (Yahoo, SEC EDGAR XBRL, FRED, EIA, SEC 13F, SEC Form 4, Finnhub `/news-sentiment`, sector RSS, Google Trends cache). |
| `lthcs-news-hourly.yml` | Hourly | News-only refresh (`lthcs_daily.py --news-only --force`). Recomputes Thesis + composite for every ticker using fresh news inputs; reuses the morning's Adoption / Institutional / Financial / DES sub-scores untouched. Does NOT append to history (the daily run owns that). | Finnhub recommendations, SEC 8-K, Yahoo earnings, sector RSS only. |
| `lthcs-crypto-daily.yml` | Daily, 11:47 UTC (was 22:00 until 2026-10-04, then 12:00) | Crypto pillar snapshot — 10-asset universe (BTC, ETH, SOL, ADA, AVAX, DOT, LINK, POL, XRP, DOGE) into `data/lthcs/snapshots_crypto/`. 8-day initial backfill seeded; race-safe push retry. Surfaced at `/lthcs/crypto/`. Workflow `8af023b` + `88912bb`. | Coingecko / on-chain proxies; see `docs/lthcs-crypto-pillar-adapter-spec.md`. |
| `lthcs-backtest-daily.yml` | Daily, 23:30 UTC | Runs the backtest engine (`scripts/lthcs_backtest.py`) — non-overlapping P&L + per-pillar attribution + strategy variants + Sharpe CIs → `data/lthcs/backtest/`. Race-safe push retry. Workflow `580d341`. | (read-only over snapshots) |
| `lthcs-trends-daily.yml` | Daily, 04:13 UTC | Refreshes Google Trends acceleration cache (daily replacement of the weekly batch — Sunday-only batch caused weekly cliff-effects on Adoption). | Google Trends. |
| `lthcs-trends-weekly.yml` | Weekly Mon, 02:47 UTC | Legacy weekly batch — kept as failsafe behind the daily trends cron. | Google Trends. |
| `lthcs-validate-weekly.yml` | Weekly Mon, 05:00 UTC | `scripts/lthcs_backfill_validate.py` over every snapshot day: files present (gaps disclosed in `health/known_gaps.json` are reported, not failed), ticker coverage (a ticker counts from its first score or the day after its universe `added_on`), score / band sanity, variable_detail + narrative completeness, sampled history continuity. A red run has one `::error` annotation per failing check and a per-check table in the job summary. | (read-only) |
| `lthcs-beta-verdict-monthly.yml` | Monthly 1st, 08:00 UTC | Re-runs the Adoption-pillar β post-mortem against the latest 30-day window; emits SHIP/HOLD verdict + per-cohort IC. Workflow part of `fdf2384`. | (read-only) |
| `lthcs-quality-audit-monthly.yml` | Monthly 1st, 09:00 UTC | Monthly pillar-quality audit runner; output surfaced at `/lthcs/health/quality.html`. Workflows `68b43d6` + `fdf2384`. | (read-only) |
| `lthcs-tune-weights-monthly.yml` | Monthly 1st, 07:00 UTC | Adaptive weight tuning sweep. | (read-only) |
| `lthcs-backtest-monthly.yml` | Monthly 1st, 06:00 UTC | Backtest sweep across the rolling window. | (read-only) |

The hourly news-only path keeps Thesis sub-scores and the composite band fresh on a tight cadence without burning API quotas or churning slow-moving fundamentals — Finnhub's 7-day cache means most hours are net-zero network. Concurrency is set to `cancel-in-progress: true` so a newer hour always wins. The new daily crons (`crypto`, `backtest`, `trends`) and monthly crons (`beta-verdict`, `quality-audit`) are intentionally staggered so two workflows never push to `main` in the same minute.

---

## What lives where

```
alpine-data/
├── .env                                   ← your API keys for local runs (gitignored)
├── .cache/lthcs/                          ← raw API responses (gitignored)
└── data/lthcs/                            ← the LTHCS data (committed)
    ├── universe.json                      ← the universe (active flag per ticker)
    ├── weights.json                       ← pillar weights by maturity stage + score bands
    ├── snapshots/<date>.json              ← that day's scores for every active ticker
    ├── variable_detail/<date>.json        ← every variable behind that day's scores
    ├── narratives/<date>.json             ← that day's narrative per ticker
    ├── snapshots_crypto/<date>.json       ← crypto scores
    └── history/by_ticker/<TICKER>.json    ← per-ticker score history
```

Snapshots are versioned in git, so the history of every score is the git history of `data/lthcs/snapshots/`. The commits are made through the GitHub API and signed by GitHub.

`.cache/` and `.env` are in `.gitignore`, so an accidental `git add .` won't commit them.

---

## How to add a ticker

Edit `data/lthcs/universe.json`, add an entry following the existing schema, save. The next `python lthcs_daily.py` run will include it. No code changes needed.

To remove a ticker, set `"active": false` rather than deleting the entry — that preserves history files and any backtest references.

---

## How to retire / restate a score

Don't overwrite. Append. If a score needs to be corrected:

1. Increment `model_version` in `lthcs/__init__.py` (e.g., `v1.1.1` → `v1.1.2`).
2. Record the change as a `methodology_breaks` entry (date, pillars, summary, detail), so the pages can explain the jump and re-anchor drift.
3. Let the new version run forward from the next scheduled run.

The original score remains in git history for auditability — same principle as financial restatements.

---

## Troubleshooting

**Alpha Vantage 25-call/day limit hit.** Expected for full universe runs more than once a day. The cache should keep you under the limit on normal daily runs. If you've burned through quota, wait 24h or skip the news-sentiment pillar (`--skip-thesis`).

**Yahoo Finance returns nothing for ticker X.** The `yfinance` package occasionally breaks when Yahoo changes their HTML. Try `pip install -U yfinance`. If that doesn't work, use the Alpha Vantage fallback in `lthcs/sources/yahoo.py` (already wired).

**SEC EDGAR rate-limits you.** Make sure your User-Agent header in `.env` is set to a real email address — SEC requires it.

**Validation fails after a run.** Run `python -m lthcs.validate --date <today>` for detailed diagnostics. Common causes: a source returned empty (network blip), a ticker has no XBRL filings yet (newly IPO'd), or a percentile calculation got NaN (one ticker has all-zero history). All have specific error messages.

**The new tab doesn't show up on the live site.** GitHub Pages caches aggressively. Wait 2 minutes, hard-refresh (Cmd+Shift+R / Ctrl+Shift+R), check the deployment in the GitHub Actions tab of the repo.

### Degradation matrix (optional sources)

| Missing key / state | Behavior | Notes |
|---|---|---|
| `ALPHA_VANTAGE_API_KEY` empty | Thesis pillar drops to Finnhub + neutral 50 fallback (V1 daily CI behavior with `--skip-thesis`). | Documented in §V1 status. |
| `FINNHUB_API_KEY` empty | Thesis base falls back to AV sentiment cache; 8-K + Yahoo refinement still run. | Cache-warm-friendly. |
| `FRED_API_KEY` / `EIA_API_KEY` empty | Macro overlay falls back to neutral; DES pillar drops to sector-relative. | Daily DES still computes. |
| `SEC_USER_AGENT` empty | 8-K + 13F + Form 4 fetches degrade to "no events"; pillars still score. | SEC requires a real email. |
| `ANTHROPIC_API_KEY` missing | **LLM sentiment shadow disabled; production Thesis byte-unaffected.** Shadow files in `data/lthcs/llm_sentiment/` simply aren't written. | Shadow path only; never read by Stage 4 (Tier 5 #28, spec `docs/lthcs-llm-sentiment-shadow-spec.md`). |
| `LTHCS_LLM_SENTIMENT_ENABLED` unset / `"0"` | Shadow run is a no-op (no API call, no files). Default. | Flip to `"1"` to enable. |
| Cost cap hit (`LTHCS_LLM_SENTIMENT_MAX_USD_PER_DAY`, default `$3.08`; narratives `LTHCS_LLM_NARRATIVES_MAX_USD_PER_DAY`, default `$6.17`) | Shadow persistence aborted cleanly; prior day's shadow file is last good record; production Thesis unaffected. | Both caps were scaled from the 167-ticker defaults to 515 tickers (2026-10). |

### How to enable the LLM sentiment shadow run

1. Add `ANTHROPIC_API_KEY` as a repo secret (Settings → Secrets and variables → Actions).
2. `lthcs-daily.yml` already sets `LTHCS_LLM_SENTIMENT_ENABLED: "1"`.
3. The sentiment shadow sits in the Thesis cascade, which the daily job skips (`--skip-thesis`). Dropping that flag also spends the Alpha Vantage quota, so decide that first.

---

## What V2 adds (not in scope here)

- ~~Adaptive weights based on backtest performance per asset~~ — **plumbing shipped in V1** (`306176a`, Phase 4). Promotion to live is a pure calendar gate at ~July 2026 once enough non-overlapping 21d blocks accumulate (need ≥20).
- Real-time intraday scoring for institutional users
- ~~MCP server + Anthropic Claude Connector listing (per §23 of the white paper)~~ — **shipped in V1**: 15-tool MCP server (`6d26a03` + subsequent expansion), `mcp[cli]>=1.0` pinned (Python 3.10+ via `8d373a9`).
- ~~LLM-generated narratives (Claude / GPT-4-class, grounded in stored variables)~~ — **shadow shipped in V1** (`e734272`, `5ebf973`, `c8b74c1`). Set `ANTHROPIC_API_KEY` repo secret to fire; toggle is in the detail modal.
- ~~Crypto pillar adapter (so LTHCS can score BTC, ETH, SOL, etc.)~~ — **shipped in V1**: 10-asset crypto universe (BTC, ETH, SOL, ADA, AVAX, DOT, LINK, POL, XRP, DOGE) scored daily by `scripts/lthcs_crypto_daily.py`, surfaced at `/lthcs/crypto/`. See `docs/lthcs-crypto-pillar-adapter-spec.md`.
- Premium data sources (Polygon, FMP paid, Glassnode)
- ~~Full backtest engine (§24 methodology)~~ — **shipped in V1** (Phases 1–3 + Sharpe CIs + A/B view + Phase 4 plumbing).

V2 begins after V1 has been live for ~60 days and has accumulated enough daily snapshots to be worth backtesting against. As of 2026-05-20, large chunks of the original V2 roadmap have collapsed back into V1.

---

## Backtest engine (Tier 5 #24, Phases 1–2)

A non-overlapping event-driven P&L sits next to the IC + quintile validator. It exists because the IC validator's band-portfolio Sharpe (computed from forward-window returns) reuses ~95% of the next horizon-day window on every observation, inflating Sharpe roughly h-fold — the +18.7 Sharpe headline is not real.

**Strategy (Phase 1):** long-only Buy band (elite / high_confidence / constructive), enter at the next trading-day close after a ticker enters the Buy set, exit at the next close after it leaves. 1 trading-day execution delay (look-ahead guard). 5 bps/side cost. Equal-weight, daily rebalance to equal weight on close (intra-portfolio rebalance is cost-free).

**Run locally:**

```bash
# Both IC validator and engine (default)
python scripts/lthcs_backtest.py --run-id 2026-05-19_local

# Engine only
python scripts/lthcs_backtest.py --engine pnl --run-id 2026-05-19_engine \
  --cost-bps 5.0 --benchmark SPY

# Engine + per-pillar attribution (Phase 2)
python scripts/lthcs_backtest.py --engine pnl --attribute \
  --run-id 2026-05-19_attrib --offline --no-report
```

**Output (`data/lthcs/backtest/<run_id>/`):**

- `equity_curve.csv` / `.json` — daily portfolio equity, normalized to 1.0
- `positions_daily.csv` — daily equal-weight portfolio composition
- `trades.csv` — entry/exit pairs with `hold_days`, gross/net returns
- `band_curves.json` — per-band sub-portfolio curves (smell test: higher bands should compound faster)
- `benchmark_curve.json` — SPY normalized to the engine window
- `engine_summary.json` — `summary` (total/ann return, Sharpe, Sortino, max DD, hit rate, turnover) + `run_meta` (window, hashes, params)
- `engine_report.md` — human-readable markdown
- `pillar_attribution.json` (Phase 2, `--attribute` only) — per-pillar Δ-Sharpe / Δ-return / Δ-max-DD vs baseline. Approach B: zero pillar `p`'s weight, renormalize the other four, re-band, re-run. Caveat: attributions are not additive.

**Automation:** `.github/workflows/lthcs-backtest-daily.yml` runs at 23:30 UTC (30 min after `lthcs-daily.yml` lands the snapshot). Skips silently when fewer than 30 snapshots exist. Writes into `data/lthcs/backtest/<latest-snapshot-date>_validation/` alongside the weekly IC validator. The `/lthcs/backtest/` page picks up the new artifacts on the next pages.yml deploy.

**First baseline (2026-05-19, 90-day history):** trading days = 64, total return +17.7%, ann. Sharpe **+2.6** (vs the inflated +19.4 legacy headline), max DD −10.6%, hit rate 59.4%, avg hold 11.8d, 53 trades over 22 unique tickers. Per-band: high_confidence +41.5% > constructive +12.9% > weakening +3.8% > monitor +2.8% > elite 0% > review −1.0% — the framework's band ordering holds out of sample.

**Phase 2 — per-pillar attribution (2026-05-19):** Δ-Sharpe vs baseline 2.61 over the 64-trading-day window — Financial Evolution −1.27, Institutional Confidence −1.15, Adoption Momentum −1.00, DES −0.45, Thesis Integrity −0.09 (Thesis was neutralized at 50 at the time). Negative Δ means removing the pillar hurt — i.e. the pillar contributed positively. Numbers live in `data/lthcs/backtest/2026-05-18_validation/pillar_attribution.json`; the V1 backtest tab renders a bar chart.

**Phase 3 — strategy variants (2026-05-19, `9e13452`)**: long/short, dollar-neutral, top-K, band-weighted. `dollar_neutral` surfaces at **+3.1 Sharpe**. Selectable in the backtest UI via the profile picker (`a17d8a9`). A/B view at `/lthcs/backtest/ab.html` (`5381c69`) — GG's tweak validated at **+0.184 Sharpe** vs baseline. Sharpe / Sortino now ship with 95% block-bootstrap confidence intervals (`afab1b9`).

**Phase 4 plumbing (2026-05-19, `306176a`)**: `lthcs/adaptive_weights.py::walk_forward_tune_equity` ingests engine equity curves; first run on `2026-05-18_validation` HOLDs all 5 profiles per the `SHIP_MIN_TEST_OBS=20` gate (OOS slice only ~1 non-overlapping 21d block; need ≥20). Promotion to live Adaptive Weights V2 is now a pure calendar gate at ~July 2026.

## Where the framework lives

The methodology behind every number on the dashboard is in the LTHCS Intelligence White Paper v9.5 — section references throughout the codebase (e.g., `# Per §5.2 of white paper`) point back to the relevant section so you can always trace a line of code to its intellectual source.

The white paper is the spec. This code is the implementation. Drift between them is a bug.
