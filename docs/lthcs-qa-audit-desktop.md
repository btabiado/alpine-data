# LTHCS QA & Audit — Desktop Page (mockup rationale)

**Date:** 2026-05-20
**Mockup:** `lthcs_tab/mockups/qa-audit-desktop/index.html`
**Viewport target:** 1440 × 900 (desktop-only — no responsive collapse)
**Audience:** the single LTHCS operator (Bryan), opening the page when something feels off — must verify in **30 seconds** that yesterday's pipeline ran cleanly, data quality didn't regress, no security alerts, and no universe drift.

This page consolidates content scattered across `lthcs_health/index.html`, `lthcs_health/pipeline.html`, `lthcs_health/quality.html`, and the monthly markdown audits under `data/lthcs/quality_audit/`. It is **read-only** observability over already-committed artifacts.

---

## 1. Information hierarchy (top → bottom)

The page is a 3-row grid plus a verdict banner. Reading order matches operator priority — the first thing scanned should answer "is anything wrong?", and only then "where exactly?".

| Position | Surface | Question it answers |
|---|---|---|
| Banner | **Verdict strip** (6 KPI tiles) | "In one glance: are pipeline + quality + security all green?" |
| Row 1 left (7-col) | **Pipeline runs** (last 7 cron firings + dense run table) | "Did yesterday's cron run? Was anything skipped?" |
| Row 1 right (5-col) | **Composite drift** (30d sparkline + band-change events) | "Has the universe-aggregate score moved meaningfully? Was a regime flip data or signal?" |
| Row 2 left (7-col) | **Per-pillar quality matrix** (5 pillars × 9 columns) | "Which pillar is broken? Where are the outliers? What did we drop?" |
| Row 2 right (5-col) | **Security audit** (Dependabot / CodeQL / Trufflehog / sensitive-path commits) | "Are there any open security alerts? Has anyone touched score.py / workflows recently?" |
| Row 3 left (5-col) | **Universe state** (sector grid + active/scored deltas + pre-warm cache) | "Did the universe drift? Are IFRS fallbacks still bounded?" |
| Row 3 right (7-col) | **Score audit / top movers** (5 biggest moves with pillar-attribution reason) | "If the composite moved, which tickers and which pillar drove it?" |

The 7/5 ratio alternates by row so that the "noisy" content (tables, commit lists, mover reasons) always lives in the wider column.

## 2. The verdict banner (most important component)

A single colored strip across the top. Color is the worst-of-three: green (clean), amber (one or more sub-system warning), red (failure). Today it reads **amber / "1 ISSUE"** because:

- pipeline = clean (7/7 last 7 runs)
- security = clean (0 Dependabot, 0 CodeQL, 0 Trufflehog)
- **quality = CRITICAL** — driven by band-distribution misalignment (`elite`/`high_confidence` empty; `review` overflowing at 49.7%), per `data/lthcs/quality_audit/2026-05-20_summary.md`.

The six KPI tiles in the banner are deliberately the same six values the operator otherwise scrapes from three separate health pages and a markdown file:
- Last run + duration
- Tickers scored / active
- Avg pillar coverage
- Band churn (30d)
- Security alert count
- Pushed-by (lthcs-bot vs human)

## 3. Real data driving the mockup

Every number is sourced from a committed file in-repo, not invented:

| Surface | Source |
|---|---|
| 167 scored, 168 active, 169 declared | `data/lthcs/universe.json` (169 with 1 inactive `WBA`) vs snapshot (167; ANSS missing) |
| Composite score history | 30 daily files in `data/lthcs/index/` (2026-04-20 → 2026-05-20: -8 → -26) |
| Pillar coverage / outliers | `data/lthcs/quality_audit/2026-05-20_pillar_quality.md` |
| Band distribution + churn | `data/lthcs/quality_audit/2026-05-20_band_distribution.md` |
| Top movers + pillar reasons | computed live from snapshots 2026-05-18 vs 2026-05-20 |
| Security commit list | `git log --since=7d -- lthcs/score.py app.py .github/workflows/` |
| Pre-warm counts | `data/lthcs/prewarm_status.json` |
| Sector breakdown | aggregated from snapshot `sector` field |
| Thesis-skip note (108 tickers) | `.github/workflows/lthcs-daily.yml` (`--skip-thesis` flag) |

## 4. Density choices (desktop-specific)

The mobile sibling at `lthcs_tab/mockups/qa-audit-mobile/` will accordion these sections; this design **does not**. Operator on a 1440 viewport gets:

- **All 9 columns of pillar-quality** visible without scroll (verdict / coverage / mean / stdev / floor-ceil / 30d-σ / dropped / outliers). The mobile version will likely show a stacked-card-per-pillar.
- **Run timeline as 7-tile horizontal strip + table below**, not stacked. Operator wants peripheral status (the 7 green dots) before diving into the table.
- **Sparkline at 300×110 SVG** with inline grid lines, band-tint zones, and y-axis labels in monospace. No tooltip-on-hover library — the labels under the chart hold the load.
- **Top movers in a single dense table** with from-to fractions inline (`27.8 → 38.4`), pillar-attribution as a sentence ("thesis_integrity +19.8 — narratives_llm refresh"), and band chips with a "← prior band" arrow when a band changed.
- **Security panel as a 2×3 KPI grid + 6-row commit ledger** rather than two stacked subsections.

## 5. What got cut

- **Hourly news cron / trends-weekly / backtest-daily / quality-audit-monthly** runs are not in the run timeline. The operator opens this page to check the *daily* pipeline, not the satellite jobs. Those live on `lthcs_health/pipeline.html` (linked in the header).
- **Crypto universe (10 tickers)**. Not in scope — separate dashboard at `/lthcs/crypto/`.
- **Per-ticker drift sparklines.** The page surfaces the *universe-aggregate* sparkline only. Per-ticker drift lives on the card-view tab.
- **Adaptive-weight / IC dashboards.** Auditing weight vs IC alignment ships from `data/lthcs/quality_audit/2026-05-20_weights_vs_ic.md` — currently UNKNOWN (no measurable IC), so surfacing it would add noise.
- **LLM cost monitor.** Shadow only; not in production score. Lives separately in the LLM observability doc.
- **Score formula breakdown.** This page audits *health*, not *methodology*. The about page at `/lthcs/about/` is the right home for formula docs.

## 6. What each section answers (quick reference)

| Section | If green | If amber | If red |
|---|---|---|---|
| **Banner** | "All systems clean." Close tab. | "One subsystem flagged — read the strip below it." | "Pipeline or security regression. Open the relevant card." |
| **Pipeline runs** | 7/7 cron firings succeeded, durations stable | A run was skipped or `--catch-up` had to fill a gap | A run failed; CI logs needed |
| **Composite drift** | flat or expected band motion | step-change ≥ 5 points / 3d (today: −15) | band-flip on the universe aggregate |
| **Per-pillar quality** | 5/5 pillars healthy | dropped > 5% of universe or coverage < 90% | floor/ceil cluster, or 30d σ jumps |
| **Security audit** | 0 alerts, 0 commits | dependabot open, or sensitive-path commit unsigned | CodeQL finding, or trufflehog hit |
| **Universe state** | scored == active | 1–3 tickers missing (CIK lookup etc.) | sector skew or universe drift |
| **Score audit** | top-5 moves < ±5 points | one move ≥ ±10 with clear pillar attribution | unexplained move (no pillar Δ > 5) |

## 7. Not implemented (deferred)

- **Hover tooltips with full pillar-delta breakdown** on the movers table. Static mockup only.
- **"Acknowledge" / "snooze" UX on the banner.** Single-operator dashboard — out of scope per threat model §4.
- **Live data binding.** This is a static HTML mockup with hardcoded values from today's snapshot. Production wiring would mount the same DOM and hydrate from the same JSON files `lthcs_health/lthcs-health.js` already loads.

## 8. Token usage

All colors / radii / fonts mirror `lthcs_tab/lthcs.css` (band-* hex, bg-page/elevated/card, text-primary/secondary/tertiary, border-subtle/strong, font-sans/mono, radius-sm/md/lg). The mockup duplicates these inline so it is portable — no `@import "../../lthcs.css"`. When/if this graduates to a real page, the inline `:root` block can be deleted and the production stylesheet imported instead.
