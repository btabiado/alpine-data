# LTHCS Crypto Route — UX Audit + Phase 2 Polish

Date: 2026-05-19
Page: `/lthcs/crypto/` (source: `lthcs_crypto/`)
Author: swarm agent CC (worktree: `swarm-ux-crypto`)

## Context

The crypto extension shipped today (`5842149` page scaffold, `8af023b` daily cron,
`88912bb` 10-coin universe, pages.yml entry). First-day inspection found a
read-only static page that listed scores, pillar breakdowns, and a Thesis call
line — but it had **no per-asset history view**, **no click-through detail**,
and the freshness of the snapshot was not surfaced. Mobile rendered 10 cards
in a tight grid that quickly went off-screen.

## Audit findings → action taken

### 1. First-impression (what hits in the first viewport)
**Finding:** The first section was "Composite scores" with no universe-level
context. A cold user landing here had no idea what "average" health looks like
or how old the data was.
**Action:** Added a `lcry-strip` section above the cards — 4 stat tiles
(universe avg, best, lowest, bands-in-play distribution). Also added a
freshness pill in the header (`today` / `2d ago` / `7d ago`) with color tiers.

### 2. Scan-ability — can a power user read all 10 scores fast?
**Finding:** Score numbers were visible but trends were not. Each card had
drift Δ1d/Δ7d/Δ30d numbers, but no shape — you couldn't tell at a glance
whether BTC was reverting to its 14d average.
**Action:** Added a 14-day sparkline per card. Reused
`../lthcs_tab/lthcs-sparkline.js` (same SVG renderer the equity card view uses)
to keep visual language consistent. Stroke color = band-color for the current
score, so the eye binds score + trend together.

### 3. Drift indicator visibility
**Finding:** Drift values existed but were tiny `gray on gray` chips. The
label `drift_1d` -> `Δ1d` was rendered weirdly (the replacement just stripped
the prefix without adding `Δ` consistently).
**Action:** Cleaned up label rendering (`Δ1d` / `Δ7d` / `Δ30d`), and added a
sibling `lcry-card-vsavg` pill to the score row that shows `vs avg +1.4`
or `−2.7` — a one-line "is this above or below the crypto universe today?"
read.

### 4. Detail modal on click
**Finding:** Cards were inert. Users had to scroll to the pillar-breakdown
section to see the sub-scores for a specific asset, and there was no in-page
way to see 30d of history.
**Action:** Made each card a `<button>`. Click opens a bespoke modal showing:
- Larger 30d sparkline (with band guides + axes — `showBands: true,
  showAxes: true`)
- Pillar breakdown bars (band-colored fills, dropped pillars dimmed)
- Thesis call line
- 4-cell drift table (1d/7d/30d/90d)
- Plain-English data-quality flag explanations
Modal has Esc-to-close, click-backdrop-to-close, focus management, and
ARIA roles. We did NOT reuse `lthcs_tab/lthcs-detail.js` (2774 lines, equity-
specific — holdings, narratives, variable_detail). Bespoke modal is ~150
lines and focused on the crypto data shape.

### 5. Empty-state copy
**Finding:** "No crypto snapshot on disk yet" was technically correct but
sounded like a permanent failure. It also referenced "Phase 2 + CI gating
land" — which already shipped today.
**Action:** Rewrote to "Waiting on today's crypto snapshot" with a concrete
explanation (cron runs ~04:00 UTC, this is the N-day probe window). Added a
back-link to `/lthcs/` so users aren't trapped on an empty page.

### 6. Cross-link prominence
**Finding:** The "← Card View" link in the header was styled identically to
a refresh button — visually one of three peers, not the primary navigation.
**Action:** Added `.lcry-backlink` class that uses the accent border color,
so the path back to V1 is unmistakable.

### 7. Section ordering
**Old:** Score Cards → Pillar Breakdown → Thesis Calls
**New:** Universe Strip → Score Cards → Thesis Calls → Pillar Breakdown
**Reasoning:** Pillar breakdown is the most data-dense section (10 tables of
5 rows = 50 rows). It belongs at the bottom for users who scroll for detail.
Thesis Calls is a one-liner-per-asset and sits well between the visual cards
and the deep tables.

### 8. Mobile (≤720px)
**Finding:** 10 cards in `auto-fit, minmax(220px, 1fr)` produced 1-column
stacked cards on phones already, but the score number was a bit oversized
relative to viewport, and the modal didn't reflow its 4-column drift grid.
**Action:** Tightened: cards stack 1-col explicitly below 720px (clearer
intent), score size drops to 28px, modal drift grid drops to 2 cols, thesis
strip stacks 1-col.

### 9. Data-quality flags
**Finding:** `thesis_unavailable` was returned for all 10 assets (Phase 2
funding wiring not yet live for these specific assets in today's snapshot).
The page surfaced "data unavailable" in the thesis section but didn't
surface this flag on the cards themselves.
**Action:** Added a compact `lcry-card-flag` chip row at the bottom of each
card, plus a full-sentence explanation in the modal's "Data quality notes"
section. Color: monitor-orange — visible but not alarmist.

## Smoke test (real data, 2026-05-19 snapshot)

Local server: `python3 -m http.server 8901` from worktree root.

- `GET /lthcs_crypto/index.html` → 200
- `GET /lthcs_crypto/lthcs-crypto.js` → 200 (loads
  `../lthcs_tab/lthcs-sparkline.js` via ESM import)
- `GET /data/lthcs/snapshots_crypto/2026-05-19.json` → 200
- `GET /data/lthcs/history/by_ticker/{BTC,ETH,SOL,ADA,AVAX,DOGE,DOT,LINK,POL,XRP}.json`
  → all 200, 8 entries each (May 12 – May 19)

10 cards render with the latest scores. All show the `thesis_unavailable`
flag (expected — Phase 2 funding wiring is staged for tickers but not yet
flowing). Sparklines populate after card-row paint (lazy).

## Cross-page dependencies introduced

- `import { renderSparkline, bandColorForScore } from
  '../lthcs_tab/lthcs-sparkline.js'`
- `lthcs.css` (was already there)

**Test in cross-page nav pass:** if `lthcs_tab/lthcs-sparkline.js` changes
its export surface, this page breaks. The exported symbols used are
`renderSparkline(history, options)` and `bandColorForScore(score)`. The
production layout (`/lthcs/crypto/` ← `../lthcs_tab/`) is established by
`.github/workflows/pages.yml:258-266` (mirrors `lthcs_tab/` next to
`crypto/`). Dev server resolves the same path because `lthcs_tab/` and
`lthcs_crypto/` are siblings in the repo.

## What's intentionally out of scope

- LLM narratives: not in the snapshot, not needed for this page.
- Holdings drill-down: equity-specific, no crypto analog.
- Sentiment toggle: deferred until Finnhub-for-crypto wiring lands.
- Compare-two-coins side-by-side: deferred — universe-strip + vs-avg pill
  covers the same need for ≤5 sec scans.
