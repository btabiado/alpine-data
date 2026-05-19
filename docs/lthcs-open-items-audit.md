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

## 2026-05-18 PM → 2026-05-19 AM — Sector RSS closed + β diagnosed + Option B live

Second swarm of the day. **Tier 4 closes entirely** (#22 P2 polish + a UX
nice-to-have). Sector RSS (#5 P4) goes from incomplete-by-gating to truly
wired. The Adoption-pillar β post-mortem identifies the real driver and
points at a one-line XS fix. LLM sentiment (#28) is reclassified as a
refactor, not a build. And hourly news refresh ships as a standalone
workflow (Option B) — daily snapshot stays daily, news data goes hourly.

**Tier 4 #22 P2 polish — fully SHIPPED** (`07a7dae`):
`-webkit-text-size-adjust`, `touch-action: manipulation`,
`-webkit-tap-highlight-color: transparent`, `:active` states. **Tier 4
is now fully closed** (#17-#22 all green).

**Tier 4 follow-up — detail-modal dragging-pillar callout** (`014aadc`):
UX nice-to-have that closed organically. Surfaces the Phase 5
verification finding inline per ticker for Weakening / Review tickers
("Adoption pillar dragging composite" etc.), so users see the
pillar-level story without leaving the modal. Worth promoting — closes
the gap between the verification doc and the live UI.

**Sector RSS (#5 P4) — actually shipped now**:
The Phase 5 P4 commit (`16f7945`) wired the source module, but the
daily pipeline was still running with `--skip-thesis` which gated
`sector_rss` out alongside the heavy Thesis work. Universal
`had_sector_rss=False` across all 167 tickers in tonight's snapshot
exposed it. Two follow-up fixes:
- `9cd10a7` — Fed feed UTF-8 BOM encoding fix (was failing parse on the
  byte-order mark; added 1 test → total 1441 passing)
- `ef1cc06` — un-gate `sector_rss` from `--skip-thesis`, mirrors the P0
  pattern used for Form 4 / 13F in `a55aab8`

Expected ~20 of 167 tickers flip to `has_sector_rss=True` on next
pipeline run (next daily 23:00 UTC or the new hourly news-only run).

**Adoption pillar β — root cause diagnosed** (`daba54e`,
`docs/adoption-pillar-inversion-2026-05-19.md`):
Phase 5 P1 (`fa926bf`) added sector-relative framing + QoQ accel, but
the residual inversion-at-21d isn't from QoQ or Trends — **the real
driver is `sector_relative_revenue` percentile-rank clamping**. 19% of
the universe is pinned at the 0 or 100 boundary because peer cohorts
are too small (`_MIN_SECTOR_COHORT=8`), which compresses signal into a
bimodal distribution and inverts the IC. Recommended fix:
`_MIN_SECTOR_COHORT 8 → 20` (XS, single-constant change). Expected
+0.02-0.03 IC at 21d, no other moving parts.

**This is now the highest-ROI next item** — ahead of Tier 2 / 3 / 5
work. It's strictly a tuning change against existing pillar
infrastructure.

**Adoption IC re-validation window**: moved from 2026-06-17 →
**2026-06-24** per the β analysis (+~7d for stable IC + Trends weekly
batch resolution; see `docs/adoption-pillar-inversion-2026-05-19.md`
lines 120-122).

**LLM sentiment shadow (Tier 5 #28) — spec landed; effort downgrade**
(`7732d9e`, `docs/lthcs-llm-sentiment-shadow-spec.md`):
Discovery during spec-writing: `lthcs/sources/llm_sentiment.py` is
**718 lines, not a stub**. The module is ~90% built — the open work is
shadow-run wiring + retention, not greenfield. Cost re-estimate with
Haiku 4.5 + prompt caching: **~$0.19/day** (audit's earlier $0.85/day
was based on a stale Sonnet assumption). Tier 5 #28 effort tag
**M-refactor, not M-build**.

**Hourly news refresh (Option B) — shipped**:
- `6c65fac` — `lthcs_daily.py --news-only` flag: refreshes Finnhub
  recos + 8-K + Yahoo earnings + sector RSS for today's snapshot only;
  history append skipped; runtime 30-90s per run.
- `01f5f38` — `.github/workflows/lthcs-news-hourly.yml`: every hour at
  `:15` past, calls `--news-only`.

This keeps the daily cron's 23:00 UTC full-pipeline run authoritative
for composite math + history; the hourly run only swaps in fresh news
sub-signals on the already-published day's snapshot. See updated
Automation schedule table below.

**Crypto dashboard side-quest** (app.py, not LTHCS — noted for
completeness only):
- `c99a9c4` — AI News tab side-by-side: AI insights left, top-5 AI news
  right.
- `300f05b` — tightened insight cards to match Crypto tab's "Top
  insights" stacked style.
- `78be9b9` — `avgFee.toFixed` JS error on ETH whale stats fixed.

**Mockup variants A / B / C — planning artifact** (`lthcs_tab/mockups/`,
commits `226d99b` → `7e70d22`):
Three pyramid / tree layout variants exploring a redesigned LTHCS
dashboard. **Variant C (indexed drill-down) is the user-approved
direction**; awaiting implementation decision (not yet on the build
queue — this is a sketch, not a commit-in-progress).

**Tests**: 1338 at audit start → 1440 at first 2026-05-18 PM swarm
→ **1441 tonight** (the Fed-encoding fix added one).

---

## 2026-05-19 AM — Audit list swarm: Tier 5 #26/#28 closed + #27 Phase 2 ready + backfill verified

Tonight's swarm chewed through five Tier 5 items in parallel and verified
the 90-day backfill. Net effect: **Tier 5 contracts hard** (the surveys
keep finding "~85-90% already built"), Adoption β follow-up closes, two
Phase 2 specs (#27 crypto, Tier 2 #7 HW/SW split) land ready-to-build.

**Adoption β fix — SHIPPED** (`333e5dd`):
The previous refresh's "Suggested next 3 commits" #1 lands cleanly.
`_MIN_SECTOR_COHORT 8 → 20` plus mid-rank ties. **62 tickers across 7
sectors** now fall back to universe-relative ranking instead of being
pinned at percentile 0 / 100 inside under-populated sector cohorts. The
audit's #1 next-3-commits item is **closed**. Adoption IC re-validation
window stays **2026-06-24** (revised tonight per the β analysis;
unchanged here).

**Test gap close** (`c035366`): added `--skip-thesis` variant of the
sector RSS integration test. Catches the exact gating regression that
left sector RSS dark for a day in the previous refresh.

**Tier 5 #26 (MCP server) — SHIPPED** (`6d26a03`): `mcp[cli]>=1.0`
pinned + boot test + `get_dragging_pillar` tool. The earlier `8f4e2c6`
state survey found the module was ~90% built; tonight's commit closes
the remaining polish. **Tier 5 #26 effectively closed.**

**Tier 5 #27 (Crypto pillar adapter) — SPEC READY** (`da10a8b`):
Survey discovered Phase 1 (BTC/ETH/SOL fetchers + sub-pillar math) is
already in tree. Remaining work is Phase 2 integration into composite +
heatmap. Effort tag revised **M-L → S**. Spec landed; ready to build.

**Tier 5 #28 (LLM sentiment shadow) — SHIPPED** (`37199d7`): Haiku 4.5
default, daily cost cap, `LTHCS_LLM_SENTIMENT_ENABLED` env flag
default-0, byte-identical default behavior. **Tier 5 #28 effectively
closed pending flag flip after shadow data clears.**

**Tier 2 #7 (HW/SW peer split) — SPEC READY** (`6d08632`): Compound
peer-group key `(maturity_stage, sector_group)` was the design blocker
from `peer-group-audit §3.4` — naive split made AAPL worse inside a
bimodal Tech-compounder cohort. Tonight's spec proposes a curated
Hardware/Software split that empirically dissolves the bimodality.
Effort revised **M → S**. Ready to implement.

**Backfill verified** (`2e1654c`): 90-day `--force` rewrite completed
cleanly. Validator: **PASS 91/91 dates**, 15,197 ticker-days, 0 NaN,
0 out-of-range. All previously-failing band consistency + history
continuity issues are resolved. Phase 5 effects are now in historical
scores end-to-end (P0 Form 4 / 13F populated, P3 margin fallback chain,
P1 Adoption sector-relative + QoQ accel, P4 FRED tier-2 + sector RSS).
**Small known inconsistency**: the last ~6 dates include `sector_rss`
data; the earlier ~80 don't, because `ef1cc06` (the un-gate fix) landed
mid-backfill. **Not worth re-running** — the affected dimension is a
sub-pillar data-quality flag, not the composite math itself.

**Tier 5 narrowing observation**: tonight's three Tier 5 surveys
(`llm_sentiment`, `lthcs_mcp`, crypto pillars) all converged on the
same pattern — the modules are **~85-90% already built**. The audit's
Tier 5 backlog is materially smaller than it looked on paper. Net
result: of the six original Tier 5 items, **#26 + #28 effectively
closed**, **#27 reduced to S**, **#23 (LLM narratives) remains M-L**,
**#24 (backtest engine) and #25 (adaptive weights)** are still the
genuine V2/V3 build items.

**Tests**: 1452 → **1471** (LTHCS subset; full suite at 1710 incl.
non-LTHCS test files).

---

## 2026-05-18 PM — Phase 5 + UX swarm shipped

Massive ship day. Tier 1 (#1-5) and the bulk of Tier 4 (#17-22) all
closed in two parallel agent swarms. Verification snapshot `a51e444`
regenerated against the fully wired pipeline.

**Phase 5 — Tier 1 data wires (5 of 5 shipped)**:
- `a55aab8` **#1 P0** — Un-gated Form 4 + 13F. Institutional regression
  fixed; `has_insider` now 165/167, `has_holdings` 167/167.
- `fa926bf` **#2 P1** — Adoption overhaul: Google Trends + sector-relative
  framing + QoQ revenue acceleration. `has_qoq` 0 → 159, `has_trends`
  0 → 11. Inverts-at-21d problem (audit item β) directly targeted.
- `4c7892b` **#3 P2** — 8-K material events + Yahoo earnings/recos
  into Thesis. `events_refinement_sources` 0 → 135. Begins eroding
  the constant-50 Thesis problem (audit item γ).
- `f5e2259` **#4 P3** — Margin XBRL fallback chain (concept_fallbacks
  + bank cohort). Margin coverage 56% → 89% (93 → 158/167 names).
  Bank cohort 7 → 11 (addresses #15).
- `16f7945` **#5 P4** — FRED tier-2 indicators + sector RSS into DES.
  6 tier-2 indicators wired. Closes Tier 2 #9 (no longer deferred).

**Tier 4 — UX swarm (6 of 6 shipped)**:
- `69508ee` **#21** — About-modal data-feed lineage table.
- `655cb4b` **#17** — Sortable Bloomberg-style table with click-to-sort
  + 3 new columns.
- `9f33d1c` **#18** — Movers leaderboard (top gainers / losers strip).
- `6367616` **#19 + #20** — Detail modal Evidence accordion +
  multi-series composite-history chart.
- `509bdb2` + `a81d2b5` **#22** (P1) — Mobile/Safari: backdrop-filter
  webkit prefix, body-scroll lock, `100dvh`, 44px touch targets, V2 tab
  fix. **#22 P2 polish still open** (see follow-up below).

**Verification (today's `a51e444` snapshot)**:
- `has_insider` 165/167 ✓
- `has_holdings` 167/167 ✓
- `has_trends` 11 ✓ (weekly batch — broader coverage on next Monday cron)
- `has_qoq` 159 ✓
- `has_margin` 158/167 ✓
- DES sub max 73.7 (+0.11pt today; see ceiling note below)
- 1440 tests passing (was 1338 at session start this morning)

**DES ceiling note**: Tier-2 FRED indicators + sector RSS are wired
correctly. Sector RSS surfaces on Thesis `data_quality`, not DES math
(by design — sector RSS is qualitative context, not a quantitative
input). The DES p99 ceiling (~73) noted in the δ Elite investigation
above is governed by `TIER2_MAX_POINTS=5.0`. Widening that constant is
the single lever for a higher DES ceiling — defer until a re-tilt is
actually wanted.

**Still open from today**:
- **#22 P2 polish** — `-webkit-text-size-adjust`, `touch-action:
  manipulation`, `-webkit-tap-highlight-color: transparent`, `:active`
  states. Small, isolated CSS pass; queued as next UX commit.

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
| 1 | **Un-gate Form 4 + 13F (Institutional)** | (this audit / Phase 5 P0) | M | ✅ SHIPPED `a55aab8` (2026-05-18) — Institutional regression fixed. `has_insider` 165/167, `has_holdings` 167/167. Form 4 + 13F now active in pillar math (no longer renormed away). |
| 2 | **Adoption overhaul: Trends + sector-rel + QoQ accel** | news-feeds + peer-group | M-L | ✅ SHIPPED `fa926bf` (2026-05-18) — `has_qoq` 0 → 159, `has_trends` 0 → 11 (weekly cron grows this). Sector-relative framing + QoQ revenue acceleration directly target the "inverts at 21d" finding (audit item β). |
| 3 | **8-K + Yahoo earnings/recos → Thesis** | news-feeds-earnings-events §1.1, §2.1, §3 | M | ✅ SHIPPED `4c7892b` (2026-05-18) — `events_refinement_sources` 0 → 135. SEC 8-K material events + Yahoo earnings/recommendations now feed Thesis. Begins eroding constant-50 Thesis (audit item γ); LLM sentiment #28 is the next step. |
| 4 | **Margin XBRL fallback chain + bank cohort** | (this audit / Phase 5 P3) | M | ✅ SHIPPED `f5e2259` (2026-05-18) — Margin coverage 56% → 89% (93 → 158/167). Concept-fallback chain over alternate XBRL tags. Bank cohort 7 → 11 (closes the Financial-pillar drag for regional banks; addresses #15). |
| 5 | **FRED tier-2 + sector RSS → DES** | des-audit + news-feeds-sector-specific | M | ✅ SHIPPED `16f7945` (2026-05-18) — 6 tier-2 FRED indicators (closes Tier 2 #9, no longer deferred) + sector RSS into Thesis `data_quality`. DES sub max 73.7 today; ceiling now governed by `TIER2_MAX_POINTS=5.0` (see Phase 5 ship note for ceiling lever). **Follow-up (2026-05-19 AM)**: Phase 5 P4 sector RSS was incomplete — `had_sector_rss=False` universally on every ticker because `--skip-thesis` was gating it out. Fixed by `9cd10a7` (Fed feed UTF-8 BOM encoding) + `ef1cc06` (un-gate `sector_rss` from `--skip-thesis`, mirrors P0 pattern). Expected ~20 of 167 tickers flip to `has_sector_rss=True` on next pipeline run. |
| 6 | **AI-news threshold polish** | (this audit) | XS | ✅ SHIPPED `36c48aa` — mention-count multiplier (3-5: 1.0×, 6-10: 1.1×, 11-20: 1.2×, 21+: 1.3×) on top of engagement tier, cap at +0.75. MSFT/META/GOOGL/TSLA/PLTR live: 0.60→0.75 (cap binds, Constructive→low High). NVDA/AMD low-engagement: 0.35→0.455. Doesn't fire today (Finnhub/SEC/Yahoo cascade pre-empts AI news) — materializes when earlier-cascade sentiment goes stale. |

**Items 1-5 all shipped 2026-05-18 PM** (see Phase 5 ship note above).
Original prediction held: framework now feels "live" between Thesis
rotations and Institutional/Adoption pillars have honest data instead
of renormed stubs. Next wave of impact comes from #28 (LLM sentiment)
to actually move Thesis off 50, and from #13 (full 13F implementation)
to deepen Institutional beyond binary signal.

---

## TIER 2 — Open items needing more design before build

| # | Item | Source | Effort | Status |
|---|---|---|---|---|
| 7 | **Compound peer-group key** `(maturity_stage, sector_group)` | peer-group-audit §3.4 | S | 📋 SPEC READY `6d08632` (2026-05-19) — Hardware/Software split spec landed; bimodality vanishes under proposed split. Effort revised **M → S**. Ready to implement. |
| 8 | **De-dup `Technology` ↔ `Information Technology`** in sector_des_weights.json | des-audit §6 | XS | ✅ SHIPPED `09147a7` — canonical "Information Technology" + `_alias_of` resolver in des.py with defensive handling (broken alias → neutral fallback). DES scores identical pre/post for AAPL/MSFT/NVDA/AVGO/ORCL. |
| 9 | **Tier 2 macro signals**: Brent crude, gasoline cracks, ISM PMI, housing starts, consumer confidence, U-6 | des-audit | M-L | ✅ SHIPPED `16f7945` (2026-05-18) — 6 tier-2 FRED indicators wired into DES via Phase 5 P4. DES ceiling now governed by `TIER2_MAX_POINTS=5.0`; widen constant to lift ceiling further. |
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

| # | Item | Source | Effort | Status |
|---|---|---|---|---|
| 17 | **Sortable Bloomberg-style table view** at `/lthcs/table/` | ux-research §4.2 | M | ✅ SHIPPED `655cb4b` (2026-05-18) — click-to-sort + 3 new columns. |
| 18 | **"Movers" leaderboard strip** (top-10 gainers + losers by drift) | ux-research §4.3 | S | ✅ SHIPPED `9f33d1c` (2026-05-18) — top gainers/losers strip live. |
| 19 | **Detail modal: expand narrative + variable-detail evidence** | (this audit) | S — already 90% there | ✅ SHIPPED `6367616` (2026-05-18) — Evidence accordion. |
| 20 | **Time-series chart on detail modal** showing composite history | (this audit) | M — sparkline exists; full chart is bigger | ✅ SHIPPED `6367616` (2026-05-18) — multi-series chart on detail modal. |
| 21 | **About-modal updates** with current data-feed lineage (which sources feed which pillar) | (this audit) | XS | ✅ SHIPPED `69508ee` (2026-05-18) — data-feed lineage table in About modal. |
| 22 | **Mobile/Safari testing pass** | ux-research | S — heatmap was tested; main tab probably needs one too | ✅ SHIPPED — P1 `509bdb2` + `a81d2b5` (backdrop-filter webkit prefix, body-scroll lock, `100dvh`, 44px touch targets, V2 tabs fix) + P2 `07a7dae` (`-webkit-text-size-adjust`, `touch-action: manipulation`, `-webkit-tap-highlight-color: transparent`, `:active` states). **Tier 4 fully closed.** |
| 22b | **Detail modal — dragging-pillar callout** (follow-up nice-to-have) | (this audit) | XS | ✅ SHIPPED `014aadc` (2026-05-19) — surfaces Phase 5 verification finding inline per ticker for Weakening / Review tickers. Closed organically; not on original Tier 4 list. |

---

## TIER 5 — V2/V3 framework changes (model-shape, not just config)

| # | Item | Why | Effort |
|---|---|---|---|
| 23 | **Replace templated narratives with LLM-generated** | V1 narratives are sentence templates; LLM would weave in actual data quality flags + cross-pillar context. | M-L |
| 24 | **Backtest engine** | Score history → P&L attribution to validate the framework | L |
| 25 | **Adaptive weights** (V2) | Use backtest to suggest per-ticker weight adjustments | XL (depends on 24) |
| 26 | **MCP server / API exposure** | LTHCS data as Claude Connector | ✅ SHIPPED `6d26a03` (2026-05-19) — `mcp[cli]>=1.0` pinned + boot test + `get_dragging_pillar` tool. **Effectively closed.** |
| 27 | **Crypto pillar adapter** | Score BTC/ETH/SOL in the same framework | 📋 SPEC READY `da10a8b` (2026-05-19) — Phase 1 (BTC/ETH/SOL fetchers + sub-pillar math) already in tree; Phase 2 integration into composite + heatmap remains. Effort revised **M-L → S**. |
| 28 | **Real LLM-derived sentiment** (replace AI-news engagement heuristic) | Engagement ≠ sentiment direction; Claude call per ticker per day could give real polarity | ✅ SHIPPED `37199d7` (2026-05-19) — Haiku 4.5 default, daily cost cap, gated `LTHCS_LLM_SENTIMENT_ENABLED` env flag (default-0), byte-identical default behavior. **Effectively closed pending flag flip** after shadow data clears. Earlier spec: `docs/lthcs-llm-sentiment-shadow-spec.md` (`7732d9e`). |

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

Tier 1 #1-5 shipped 2026-05-18; Tier 4 fully closed 2026-05-19 AM;
tonight's Tier 5 swarm closed #26 + #28, downgraded #27 + Tier 2 #7 to
S. Previous refresh's #1 (Adoption β fix) shipped tonight `333e5dd`.
New top-of-queue:

1. **Backtest re-validation** — S. Re-run `scripts/lthcs_backtest.py --start <-90d> --end <yesterday> --horizon 21` against the freshly verified 90-day backfill (`2e1654c`, validator PASS 91/91). Produces empirical IC comparison pre / post Phase 5 — confirms Adoption stops inverting at 21d (post-β fix `333e5dd`) and re-baselines composite IC before LLM-sentiment shadow flips on. **Auto-fires from another agent tonight.**
2. **Tier 2 #7 Hardware/Software split implementation** — S. Spec ready (`6d08632`); bimodality vanishes under proposed split, unblocking the long-deferred compound peer-group key. **Likely shipping tonight from another agent.**
3. **Tier 3 #13 Full 13F implementation OR Tier 5 #23 LLM narratives** — both M-L; pick based on the backtest result. If Institutional IC is still the workhorse (+0.204 in the morning read), prioritize #13 to deepen the strongest pillar. If composite IC plateaus post-backfill, prioritize #23 to add a qualitative differentiator.

After those, the natural follow-ups are: flip `LTHCS_LLM_SENTIMENT_ENABLED=1` once shadow data clears (then re-tighten `elite.min` back to 90 if Thesis_p99 ≥ 85), widen `TIER2_MAX_POINTS` to lift the DES ceiling, and Tier 5 #27 Phase 2 (crypto pillar adapter into composite + heatmap, now S effort).

**Time-gated**: Adoption IC re-validation window remains **2026-06-24** (revised tonight per β analysis; +~7d for stable IC + Trends weekly batch resolution).

---

## Outstanding from the diagnostic tool's perspective

Run `python scripts/lthcs_diagnose.py AAPL INTC NVDA LLY JPM` to see which
items in this audit are causing each ticker's current sub-pillar drag.
The tool labels each pillar as REAL / PARTIAL / STUB / NEUTRAL / MISSING
so you can map a ticker's score directly to the audit items that gate
the next composite move.

Tests: **1471 passing** (LTHCS subset; full suite at 1710 incl. non-LTHCS test files). Was 614 at session start 2026-05-17 morning; 1338 at start of 2026-05-18; 1440 after first 2026-05-18 PM swarm; 1441 after the Fed-feed UTF-8 BOM encoding fix in `9cd10a7`; 1452 → **1471** tonight from the Tier 5 swarm (`333e5dd` + `c035366` + `6d26a03` + `37199d7`).

**Tier 5 narrowing observation**: tonight's three Tier 5 surveys (`llm_sentiment`, `lthcs_mcp`, crypto pillars) all found **~85-90% already built** — the audit's Tier 5 backlog is materially smaller than it looked on paper. Of the six original Tier 5 items: #26 + #28 effectively closed, #27 reduced to S, #23 remains M-L, #24 + #25 are the genuine V2/V3 builds.

---

## Automation schedule

Every recurring LTHCS job is wired into `.github/workflows/`. Cron times
are UTC and intentionally staggered so two workflows never fight for
the same runner pool or push to `main` in the same minute.

| Cadence | UTC cron | Workflow file | What it does | Commits to main? |
|---|---|---|---|---|
| Hourly | `15 * * * *` | `lthcs-news-hourly.yml` | `lthcs_daily.py --news-only` (commit `6c65fac`) — refreshes Finnhub recos + 8-K + Yahoo earnings + sector RSS for today's already-published snapshot only. **History append skipped**; daily 23:00 UTC run remains authoritative for composite math + history. Runtime 30-90s per run. Workflow `01f5f38`. | Yes |
| Daily | `0 23 * * *` | `lthcs-daily.yml` | `lthcs_daily.py --force --catch-up --skip-thesis` — accumulates the daily snapshot under `data/lthcs/`. Note: `sector_rss` is no longer gated by `--skip-thesis` (see `ef1cc06`); Form 4 + 13F similarly un-gated in `a55aab8`. | Yes |
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
