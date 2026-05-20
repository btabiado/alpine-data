# LTHCS Backtest Page — UX Audit & Restructure
**Date:** 2026-05-19
**Scope:** `lthcs_backtest/` (Phase 2 task 2.2, swarm agent BB)
**Touch:** `index.html`, `lthcs-backtest.js`, `lthcs-backtest.css` only

---

## Audit findings (before)

| # | Section (top → bottom)                            | Default state | Notes                                                                  |
|---|---------------------------------------------------|---------------|------------------------------------------------------------------------|
| 1 | Verdict card                                      | visible       | OK — headline pillar, kept first                                       |
| 2 | Per-pillar IC ranking                             | visible       | Dense, but secondary to portfolio P&L                                  |
| 3 | **Band portfolio P&L** (legacy, overlapping)      | visible       | **Sharpe inflated**; visual prominence equal to honest curve below     |
| 4 | Engine P&L (non-overlapping, Phase 1–3 work)      | visible       | The actual headline number, buried in position #4                      |
| 5 | Per-band sub-portfolio bar chart                  | visible       | Folded inside Engine section — OK                                      |
| 6 | Per-pillar attribution (ΔSharpe)                  | visible       | Folded inside Engine section, fights for visual attention              |
| 7 | Quintile spread per pillar                        | visible       | Diagnostic, not headline                                               |
| 8 | Walk-forward CV table                             | visible       | Important but compact — better collapsed by default                    |
| 9 | Per-cohort breakdown                              | visible       | Lowest-priority diagnostic                                             |
|10 | Sample size disclaimer                            | visible       | Kept always-visible                                                    |

**Scroll-depth problem:** a cold visitor had to scroll past **3 dense sections** (per-pillar IC + legacy P&L + its caveat) before hitting the honest Engine P&L equity curve. The legacy curve looked identical in weight to the honest one — only a tiny italic caveat at the bottom told the user the Sharpe was inflated. High risk of mis-reading the legacy number as the verdict.

**Mobile problem:** 6+ stacked sections × 200–280px chart heights = ~2000px of vertical swipe before the disclaimer. No way to skim or skip.

**TOC problem:** none existed. The only navigation was scroll.

**Profile selector problem:** the active chip relied on `.is-active` styling alone. The new caption (`currently showing: Baseline (long-only)`) is already wired — kept.

---

## Restructure shipped

### New section order
1. **Verdict** (default-expanded)
2. **TOC chip group** — jump-to navigation; not a section, sits between verdict and content
3. **Engine P&L** (default-expanded; accented border + `headline` badge)
4. **Per-pillar IC ranking** (default-collapsed)
5. **Per-pillar attribution** (default-collapsed; split out of Engine for TOC linking)
6. **Walk-forward adaptive weights** (default-collapsed)
7. **Quintile spread** (default-collapsed)
8. **Per-cohort breakdown** (default-collapsed)
9. **Legacy · Band portfolio P&L** (default-collapsed; muted bg, dashed border, `legacy` badge, prominent caveat with inline link back up to Engine P&L)
10. **Sample size disclaimer** (always-visible, not collapsible)

### Default-expanded
- Verdict
- Engine P&L

### Default-collapsed
- Per-pillar IC, Per-pillar attribution, Walk-forward CV, Quintile spread, Per-cohort breakdown, Legacy Band Portfolio P&L

### Storage
- Collapse state: `localStorage["lthcs.backtest.collapse"]` — JSON map keyed by `data-section-key`, values `"open"` / `"closed"`. Hydrated on load; overrides HTML defaults if user has touched a section.
- Profile selector: unchanged — `localStorage["lthcs.backtest.profile"]`.

---

## UX wins

1. **The honest number is now section #1 after the verdict.** A cold visitor's first scroll lands on Engine P&L with a green accent border and a `headline` badge. The legacy overlapping-window curve is at the bottom, dashed-border, muted, default-collapsed, and prefixed `Legacy`. The pre-restructure failure mode of "user reads the inflated Sharpe and quotes it" is structurally prevented.
2. **Scroll depth shrinks dramatically.** Six dense sections collapse to title-only rows by default; users see ~5 toggle buttons + the verdict + engine chart on first paint. The TOC chips let a power user jump straight to walk-forward or quintile without scrolling. On mobile this is the difference between 2000px of swipe and ~one screen.
3. **Legacy section is impossible to mistake for the headline.** Dashed border + muted background + `legacy` badge + a prominent yellow caveat callout that links inline back up to Engine P&L. Even a user who deep-links into `#section-legacy-pnl` from outside sees the caveat before the curve.

## Accessibility notes

- Each section toggle is a real `<button>` with `aria-expanded` synced to collapse state and `aria-controls` pointing at the body wrapper. Keyboard `Enter`/`Space` toggle naturally.
- TOC is a `<nav aria-label="Sections on this page">` containing a `role="group"` chip group; chips are real `<a href="#…">` anchors so they're keyboard-tabbable and screen-reader-friendly, with JS adding smooth-scroll + auto-expand on top of the native anchor behavior.
- Focus-visible outlines added to both toggles and TOC chips. Clicking a TOC chip moves focus to the destination section's toggle so keyboard users land where they expect.
- 44px minimum touch targets inherited from `.lthcs-chip` baseline (the shared lthcs.css token).

## Files touched
- `lthcs_backtest/index.html` — section reorder + collapsible scaffold + TOC nav
- `lthcs_backtest/lthcs-backtest.js` — `setupCollapsibleSections()`, `setupTocNav()`, `COLLAPSE_STORAGE_KEY`
- `lthcs_backtest/lthcs-backtest.css` — `.lbt-collapsible`, `.lbt-section-toggle`, `.lbt-section-badge`, `.lbt-section-headline`, `.lbt-section-legacy`, `.lbt-toc*`, mobile rules

## Out of scope (deferred)
- The per-band sub-portfolio chart still lives inside the Engine section. If it grows another layer of detail, splitting it into its own collapsible would be the next move.
- The Walk-Forward "Recommended weights" block (only shown on SHIP verdict) inherits collapsibility from its parent. Could be its own card if SHIP becomes common.
