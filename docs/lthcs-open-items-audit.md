# LTHCS Open Items — Consolidated Audit

Snapshot: 2026-05-17, model `v1.1.0`, commit `b64d9f7`.

Every open item across all research docs, recommendations, and known
limitations — categorized by priority, effort, and status.

---

## 2026-05-18 Backtest validation findings (morning)

After 90-day backfill (commits `43f41a4` + `d9eddbc`), first credible IC reads:

**Framework PREDICTS forward returns** (with caveats):
- Composite IC @ 21d: +0.127 (t-stat +8.34 raw → +1.8 overlap-corrected)
- 1d Sharpe: +2.78 (honest live-trading proxy)
- Band ordering hypothesis HOLDS monotonically:
  high_confidence +17.4% → review +1.1% (21d forward returns)

**Per-pillar IC (21d horizon)**:
- 🏆 Institutional Confidence: +0.204 (workhorse — Form 4 + 13F)
- 🥈 Financial Evolution: +0.086 (most consistent across horizons)
- ⚠️ Thesis Integrity: +0.060 BUT constant 50 on 88/90 dates → zero signal in practice
- DES: +0.022 (marginal)
- ❌ Adoption Momentum: +0.004 → INVERTS at 21d (Q5-Q1 = -0.014)

**Adaptive weights walk-forward verdict: HOLD** (commit `2bcacd3`)
Walk-forward CV mechanically passed but caught 3 structural bugs:
1. Forward-return ffill inflates test_ic
2. Pillar columns need z-scoring before ridge fit
3. Ridge_alpha is effectively zero at these scales
weights.json `adaptive_overrides.enabled` correctly stays `false`.

**New cleanup queue** (post-validation TODOs):
- α Fix 3 adaptive_weights bugs + re-run CV at 4 months
- β Recalibrate Adoption pillar (inverts at 21d)
- γ Activate LLM sentiment to fix dead Thesis pillar
- δ Investigate Elite-band threshold (zero observations across 90 days) — ✅ RESOLVED below
- ε BRK.B yfinance fallback (returns "no price data" for .B suffix)
- η Manual cron trigger to verify GitHub secrets work

### δ Elite-band threshold investigation (2026-05-18 afternoon)

**Question**: Why did ZERO tickers land in `elite` across all 90 backfilled
days + the 2 current snapshots? Threshold mis-calibrated, no current ticker
qualifies, or bug?

**Investigation** (scan of all 92 snapshot files, ~15,104 row-observations):

| Metric | Value |
|---|---|
| Theoretical max composite (all pillar subs=100, +macro tailwind, weights sum to 1.0) | **100.0** for every maturity profile |
| All-time-high composite observed | **88.5** (FANG, 2026-04-07 and 2026-05-04) |
| Distinct tickers ever ≥85 | **1** (FANG only) |
| Distinct tickers ever ≥80 | ~10 (FANG, MU, ADI, NVDA, ASML, AVGO, SCHW, GOOGL, GOOG, DUK) |
| Distinct tickers ever ≥89 | **0** |

**Top 5 all-time-high composite scores**:
1. FANG · 2026-04-07 · **88.5**
2. FANG · 2026-05-04 · **88.5**
3. FANG · 2026-03-30 · **88.2**
4. FANG · 2026-04-06 · **88.0**
5. FANG · 2026-04-30 · **87.9**

**Per-pillar empirical ceiling** (across all 15,104 row-observations):

| Pillar | min | median | p99 | max |
|---|---|---|---|---|
| adoption_momentum        | 0.0 | 50.0 | 100.0 | 100.0 |
| institutional_confidence | -1.6 | 50.0 | 99.4 | 100.0 |
| financial_evolution      | 4.2 | 54.9 | 99.4 | 100.0 |
| **thesis_integrity**     | 30.0 | 50.0 | **70.0** | **81.8** |
| **des**                  | 30.4 | 47.0 | **69.4** | **73.6** |

**Diagnosis**: Threshold mis-calibrated for current pillar quality.

The math allows 100, but two pillars are structurally capped low under
current data:
- **Thesis** is the placeholder-50 problem from line 23 above (constant
  50 on 88/90 dates; queued γ to activate LLM sentiment)
- **DES** appears to be ceiling-limited near 75 by the sector-adjustment
  table; no real run ever crosses 75

Composite from each pillar's empirical p99 vector at standard_compounder
weights `[0.25, 0.20, 0.15, 0.20, 0.20]`:
  `0.25·100 + 0.20·99.4 + 0.15·99.4 + 0.20·70 + 0.20·69.4 = 87.7`

So the realistic ceiling is ~88 — exactly where FANG sits. The original
`elite.min = 90` was set assuming Thesis would carry real signal; it
doesn't yet.

**Recommendation**: Drop `elite.min` from 90 → 85 (and `high_confidence.max`
from 89 → 84 to maintain contiguity). Leave the lower bands unchanged.

Rationale:
1. A band that never fires conveys zero information to the UI/heatmap.
2. 85+ is the natural break: only 1 ticker (FANG) ever reaches it, and
   only on days when every available pillar is firing. That matches the
   "rare top-tier" semantic Elite is meant to communicate.
3. Bumping `elite.min` back to 90 should be a follow-up once γ (LLM
   sentiment) and a DES recalibration ship — at that point the math
   will support genuine 90+ composites for the best names. Re-evaluate
   the threshold when Thesis_p99 ≥ 85.
4. The fix is data-only (`data/lthcs/weights.json`); no source-module
   changes. Score-band lookup goes through `assign_band` which reads
   thresholds from JSON.

**Shipped (2026-05-18 PM)**:
- `data/lthcs/weights.json`: `elite.min` 90 → 85, `high_confidence.max` 89 → 84
- `tests/lthcs/test_score.py`: new `TestTheoreticalMax` class (3 tests)
  pinning theoretical max=100, +macro modifier cap, and a regression
  guard that fails if a future config change makes Elite unreachable
  from the empirical-top-quintile pillar vector
- Existing `TestAssignBand.test_85_is_high_confidence` renamed/relaxed
  to reflect the new boundary
- Today's (2026-05-18) snapshot would now show: 1 Elite (FANG @ 85.7),
  0 High Confidence, 16 Constructive, 36 Monitor, 55 Weakening, 59 Review.
  All-time: 54 Elite-band observations, 127 High-Confidence (vs. 0/181
  pre-recalibration).

---

## Audits already produced (research only — no model changes)

| Doc | Scope | Status |
|---|---|---|
| `docs/lthcs-ux-research.md` | Survey of 9 dashboards, recommends heatmap + table + movers | Heatmap ✅ shipped at `/lthcs/heatmap/`; table + movers ❌ |
| `docs/des-analysis.md` | DES underweight diagnosis + 6 ranked fixes | Option B + D ✅ shipped (sector softening + AI overrides) |
| `docs/des-audit-framework.md` | Macro signal inventory; what's wired vs. stub | 3 of 3 HIGH-priority gaps now ✅ wired (real_10y / VIX / M2) |
| `docs/peer-group-audit.md` | Every cross-ticker comparison + ranked fixes | Maturity-stage split ✅ shipped (v1.1.0); compound key ❌ |
| `docs/lthcs-tuning-kit.md` | Symptom-to-lever playbook + tune-preview script | Tooling ✅; specific tunings ❌ deferred to as-needed |
| `docs/lthcs-diagnostic-runbook.md` | How to diagnose surprise scores | Tool `scripts/lthcs_diagnose.py` ✅ |
| `docs/news-feeds-general-apis.md` | 13 free APIs evaluated; top 3 = Finnhub / Yahoo / MarketAux | All 3 ❌ not yet wired |
| `docs/news-feeds-sector-specific.md` | Per-sector RSS feeds; top 3 = FDA / EIA / Fed | All 3 ❌ not yet wired |
| `docs/news-feeds-earnings-events.md` | Event-driven sources; top 3 = 8-K / yfinance reco / Finnhub earnings | All 3 ❌ not yet wired |
| `docs/lthcs-followups-queue.md` | Original 5-item queue from Bryan's note | All 5 ✅ shipped |

**No outstanding research items.** All four queue items have research docs.

---

## TIER 1 — Open items with the highest ROI and shovel-ready specs

These are the "wire it next" items. All have concrete designs in their
respective research docs.

| # | Item | Doc | Effort | Predicted impact |
|---|---|---|---|---|
| 1 | **Finnhub news + sentiment** | news-feeds-general-apis §2.1 | M (~1 swarm) | Coverage 47/167 → ~155/167. Bullish/bearish % per ticker. Kills the AV rotation logic. |
| 2 | **SEC 8-K material event filter** | news-feeds-earnings-events §3 | S (~½ swarm) | 100% universe coverage on event days. Items 1.01/2.02/5.02/8.01. Already have SEC EDGAR access. |
| 3 | **yfinance earnings_dates + recommendations** | news-feeds-earnings-events §1.1, §2.1 | S (~½ swarm) | Earnings beats/misses + analyst actions for entire universe. Already pulling Yahoo for prices. |
| 4 | **FDA Press Announcements RSS** | news-feeds-sector-specific §2.1 | M | Event-driven Thesis lift for ~15 pharma names. Highest signal-to-noise per the audit. |
| 5 | **EIA "Today in Energy" + Fed press-release RSS** | news-feeds-sector-specific §2.2, §2.4 | S | Sector signal for ~30 energy + financials names. |
| 6 | **AI-news threshold polish** | (this audit) | XS | ✅ SHIPPED `36c48aa` — mention-count multiplier (3-5: 1.0×, 6-10: 1.1×, 11-20: 1.2×, 21+: 1.3×) on top of engagement tier, cap at +0.75. MSFT/META/GOOGL/TSLA/PLTR live: 0.60→0.75 (cap binds, Constructive→low High). NVDA/AMD low-engagement: 0.35→0.455. Doesn't fire today (Finnhub/SEC/Yahoo cascade pre-empts AI news) — materializes when earlier-cascade sentiment goes stale. |

**If items 1+2+3 shipped together**: probably 8-12 names move from
Constructive → High Confidence given more honest sentiment data;
event-driven signal makes the framework feel "live" between Thesis
rotations.

---

## TIER 2 — Open items needing more design before build

| # | Item | Source | Effort | Status |
|---|---|---|---|---|
| 7 | **Compound peer-group key** `(maturity_stage, sector_group)` | peer-group-audit §3.4 | M | ❌ DEFERRED — Naive version makes AAPL worse (13.2 vs 46.8 inside Tech-compounder bimodal cohort). Needs a curated Hardware/Software split first. |
| 8 | **De-dup `Technology` ↔ `Information Technology`** in sector_des_weights.json | des-audit §6 | XS | ✅ SHIPPED `09147a7` — canonical "Information Technology" + `_alias_of` resolver in des.py with defensive handling (broken alias → neutral fallback). DES scores identical pre/post for AAPL/MSFT/NVDA/AVGO/ORCL. |
| 9 | **Tier 2 macro signals**: Brent crude, gasoline cracks, ISM PMI, housing starts, consumer confidence, U-6 | des-audit | M-L | ❌ DEFERRED — Lower marginal value than the 3 we shipped today. Build only when DES re-tilts. |
| 10 | **`peer_groups.json`** config file (declarative per-pillar peer-group strategy) | peer-group-audit §3.5 | L | ❌ DEFERRED — Architecturally cleaner than hard-coded grouping in lthcs_daily.py but premature for V1 universe size. |
| 11 | **Volatility modifier → `modifiers.json`** | tuning-kit §4 | S | ✅ SHIPPED `df7cfc3` — `weights.json` modifiers block now runtime-consumed via `_parse_trigger_expression` + `_load_volatility_modifier_config`. Fallback to defaults + WARNING log on malformed config. 31 new tests. |
| 12 | **`growth_compounder` weight retune** | tuning-kit + peer-group-audit | XS | ✅ ANALYZED — current weights `[0.25, 0.20, 0.15, 0.20, 0.20]` ARE correct post-v1.1.0 reclass. Adoption-pillar mean for cohort (50.0) ≈ universe (50.5) — NOT penalized. Adoption stdev 32.2 + correlation 0.83 means the 0.25 weight is doing real work. Pre-reclass drag was a cohort-membership problem (AVGO/META in wrong profile), resolved by reclass not reweighting. High confidence; no change. |

---

## TIER 3 — Phase 2 stub-replacement (real-data wires for currently-stubbed pillar components)

| # | Component | Pillar | Currently | Phase 2 plan |
|---|---|---|---|---|
| 13 | **13F institutional holdings** | Institutional | Stubbed (renormed). Momentum carries 100%. | Aggregate 13F filings across institutions per ticker; quarterly cadence. Genuine implementation work (~2-3 swarms). |
| 14 | **Google Trends acceleration** | Adoption | Renorms; revenue carries 100%. | pytrends is rate-limited so daily 168-ticker pulls don't work. Phase 2: do an offline weekly batch, cache, run during pipeline. |
| 15 | **Bank-specific revenue growth peer cohort** | Financial | Banks compete with all compounders on revenue % rank | Add `bank` peer group OR use NII growth percentile within bank cohort specifically. JPM revenue +2-3% YoY shouldn't be benchmarked against NVDA +65%. |
| 16 | **Sector-relative momentum for Institutional** | Institutional | Universe-relative | Peer-group audit argued KEEP universe-relative; flagged as not a fix. Re-evaluate if signal feels off. |

---

## TIER 4 — UX / dashboard layer

| # | Item | Source | Effort |
|---|---|---|---|
| 17 | **Sortable Bloomberg-style table view** at `/lthcs/table/` | ux-research §4.2 | M |
| 18 | **"Movers" leaderboard strip** (top-10 gainers + losers by drift) | ux-research §4.3 | S |
| 19 | **Detail modal: expand narrative + variable-detail evidence** | (this audit) | S — already 90% there |
| 20 | **Time-series chart on detail modal** showing composite history | (this audit) | M — sparkline exists; full chart is bigger |
| 21 | **About-modal updates** with current data-feed lineage (which sources feed which pillar) | (this audit) | XS |
| 22 | **Mobile/Safari testing pass** | ux-research | S — heatmap was tested; main tab probably needs one too |

---

## TIER 5 — V2/V3 framework changes (model-shape, not just config)

| # | Item | Why | Effort |
|---|---|---|---|
| 23 | **Replace templated narratives with LLM-generated** | V1 narratives are sentence templates; LLM would weave in actual data quality flags + cross-pillar context. | M-L |
| 24 | **Backtest engine** | Score history → P&L attribution to validate the framework | L |
| 25 | **Adaptive weights** (V2) | Use backtest to suggest per-ticker weight adjustments | XL (depends on 24) |
| 26 | **MCP server / API exposure** | LTHCS data as Claude Connector | M |
| 27 | **Crypto pillar adapter** | Score BTC/ETH/SOL in the same framework | M-L |
| 28 | **Real LLM-derived sentiment** (replace AI-news engagement heuristic) | Engagement ≠ sentiment direction; Claude call per ticker per day could give real polarity | M; cheap with prompt caching |

---

## TIER 6 — Known data outages / blocked

| # | Item | Status |
|---|---|---|
| 29 | **WBA inactive** | Walgreens taken private 2025; permanently inactive in universe. |
| 30 | **Reddit OAuth blocked** | Bryan can't register; defer indefinitely per `reddit_oauth_blocked` memory. |
| 31 | **AV NEWS_SENTIMENT free tier rate limit** | ~5-7 calls/day in practice (docs claim 25). Drives the rotation design. |
| 32 | **pytrends rate-limited** | Why Google Trends is stubbed in Adoption. |

---

## Suggested next 3 commits

If pushing forward, the cleanest sequencing:

1. **Items 1 + 8 + 12** — Wire Finnhub for real per-ticker sentiment (replaces the AI-news heuristic for the AI cohort and covers everyone else); fix the alias drift risk; retune growth_compounder weights now that cohort changed.
2. **Items 2 + 3** — SEC 8-K event filter + yfinance earnings/recommendations. Adds event-driven signal across the universe. Cheap.
3. **Items 4 + 5** — Sector-specific RSS (FDA + EIA + Fed). Pharma + energy + financials get event-driven signal.

After those 3, ~70% of the universe has substantively better data than V1 ship. Items in Tier 2-5 become "what do you want to tune next" rather than "what's broken."

---

## Outstanding from the diagnostic tool's perspective

Run `python scripts/lthcs_diagnose.py AAPL INTC NVDA LLY JPM` to see which
items in this audit are causing each ticker's current sub-pillar drag.
The tool labels each pillar as REAL / PARTIAL / STUB / NEUTRAL / MISSING
so you can map a ticker's score directly to the audit items that gate
the next composite move.

Tests: 1338 passing (was 614 at session start 2026-05-17 morning)

---

## Automation schedule

Every recurring LTHCS job is wired into `.github/workflows/`. Cron times
are UTC and intentionally staggered so two workflows never fight for
the same runner pool or push to `main` in the same minute.

| Cadence | UTC cron | Workflow file | What it does | Commits to main? |
|---|---|---|---|---|
| Daily | `0 23 * * *` | `lthcs-daily.yml` | `lthcs_daily.py --force --catch-up --skip-thesis` — accumulates the daily snapshot under `data/lthcs/`. | Yes |
| Weekly Mon | `0 4 * * 1` | `lthcs-trends-weekly.yml` | `scripts/lthcs_trends_weekly.py` — pytrends batch into `data/lthcs/trends/`. Sunday 23:00 ET gives Google's limiter overnight to cool. | Yes |
| Weekly Mon | `0 5 * * 1` | `lthcs-validate-weekly.yml` | `scripts/lthcs_backfill_validate.py` — read-only audit; uploads the JSON report as a 90-day artifact and fails the run if exit code is non-zero. | No (read-only) |
| Monthly 1st | `0 6 1 * *` | `lthcs-backtest-monthly.yml` | `scripts/lthcs_backtest.py --start <-90d> --end <yesterday> --horizon 21` into `data/lthcs/backtest/<YYYY-MM>_monthly/`. Skips silently if fewer than 30 snapshots exist. | Yes |
| Monthly 1st | `0 7 1 * *` | `lthcs-tune-weights-monthly.yml` | `scripts/lthcs_tune_weights.py --walk-forward` — writes timestamped JSON into `data/lthcs/adaptive_weights/`. Verdict (SHIP/HOLD/REJECT) surfaced in the run's Job Summary. **Does NOT flip `enabled` — promotion is manual.** | Yes |

All write-back workflows mirror the race-safe push retry loop from
`lthcs-daily.yml` commit `e3f072d`: up to 3 attempts with
`git fetch + git rebase --autostash origin/main` between tries.

### Quarterly cache pre-warmer — intentionally skipped

`scripts/lthcs_backfill_prewarm.py` populates `.cache/lthcs/` for the
local 90-day backfill loop. It was on the original Phase-3 task list as
`lthcs-prewarm-quarterly.yml` but is **not** implemented because:

1. `.cache/` is `.gitignore`d — nothing the script writes would survive
   the Actions runner teardown.
2. Caches don't persist between Actions runs, so each scheduled run
   would re-populate a cache nobody reads.
3. The only persisted output is `data/lthcs/prewarm_status.json`, and a
   "this was warmed in CI, not on your laptop" status row would
   actively mislead the local backfill orchestrator.

If we ever migrate caches to a shared store (e.g. `actions/cache@v4`
with a stable key, or S3), revisit. Until then, run the pre-warmer
locally before invoking `scripts/lthcs_backfill.py`.

### Manual triggers

Every workflow above accepts `workflow_dispatch: {}` so it can be
fired ad-hoc from the Actions UI without waiting for the cron.
