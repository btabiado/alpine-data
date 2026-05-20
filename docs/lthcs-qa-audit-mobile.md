# LTHCS · QA & Audit (mobile) — design rationale

**Date:** 2026-05-20
**Mockup:** `lthcs_tab/mockups/qa-audit-mobile/index.html`
**Viewport:** 375×812 (iPhone 13/14 portrait)
**Audience:** the single operator/owner of the LTHCS pipeline, on their phone,
checking the page over coffee or in line. They have ~10 seconds to confirm
nothing is broken before drilling in.

---

## 1. Glance-then-drill philosophy

The page answers one question above the fold:

> _Is anything broken right now?_

Everything else is progressively disclosed. The screen is split into three
layers:

| Layer | Goal | Time-on-screen |
|---|---|---|
| Hero | "Should I keep scrolling?" | ~2s |
| 2-up status tiles | Universe size + DQ flag count — the two scalars that tell you scale and noise floor | ~3s |
| 6 collapsed accordions | Drill-in detail per surface area (Pipeline / Data quality / Verdict drift / Score audit / Universe / Security) | on demand |

If the hero is green, the operator can lock the phone and go back to their
coffee. If it's amber or red, the matching accordion is one tap away.

---

## 2. What's hero vs. collapsed

### Hero (always visible)

- **One giant traffic-light dot** (56×56, animated pulse) — green / amber /
  red. The dot is the only chrome on the page that animates, so the eye
  goes to it first.
- **Big-text status** ("ALL GREEN" / "1 WARN" / "FAIL") at 28px / 700 weight.
- **Three "is anything broken?" lines** under the divider:
  1. Last daily run + age (cron freshness)
  2. Last 7 daily runs count (recent reliability)
  3. Composite drift 7d (the score's own health)
- The whole hero is one big tap target (>156px tall). Tap jumps to and
  expands the Pipeline accordion — by default we assume the most likely
  reason to drill in is "what's the pipeline status?"

The hero today reads **1 WARN** because the monthly quality audit verdict is
**CRITICAL** (band-starve: 0 elite, 0 high-confidence — 49.7% of the
universe in the review band). The pipeline itself is fine; data shape is
not. That nuance is exactly what the "1 WARN" hero captures vs. a binary
green/red.

### Collapsed accordions (tap to expand)

Six sections, all `<details>` elements, all closed by default except Data
Quality (which is the source of today's warn). Each summary row has:

- **A 10px colored status dot** (left) — answers "is this section
  individually OK?"
- **Section name** (middle, 15px)
- **A monospace tag chip** (right) — the one-line summary
  (`7/7 OK`, `CRITICAL`, `−17 / 7d`, `8 moves`, `v2.2.0`, `clean 7d`)

So even fully collapsed, the operator can scan the dots + tags in ~2 seconds
and see the same shape of health as the hero, broken down by surface.
This is the "five status dots in a column" pattern from native iOS apps
(Files, Mail) — colored disclosure indicators are read instantly.

#### What's inside each accordion

1. **Pipeline** — last 7 daily runs as colored bars (green=OK,
   amber=late, red=fail, dashed=skipped/no-diff), median runtime, last
   failure, catch-up status, "14 crons total" chip.
2. **Data quality** — five horizontal bars (one per pillar) showing
   `coverage_pct` from `latest_summary.json`. Thesis at 89.2% renders in
   amber; the other four pillars in green. Bands row exposes the
   band-starve verdict.
3. **Verdict drift** — composite-score sparkline of last 7 days
   (−9 → −9 → −11 → −11 → −20 → (–) → −26), big −26 readout, top driver
   pulled from `index/2026-05-20.json` (`band lean −23`).
4. **Score audit · 24h movers** — top 8 |Δ1d| ticker moves from
   today's snapshot (DASH +10.6, AZN +9.9, … WMT −6.2) with the dominant
   subscore that drove each move.
5. **Universe state** — 167 active tickers, 0 added/dropped in 7d,
   108 thesis fallbacks (the `--skip-thesis` CI flag + AV NEWS_SENTIMENT
   AND-not-OR quirk), 3 SEC unavailable, parking-lot expansion wave.
6. **Security** — Dependabot / CodeQL / Trufflehog / SHA-pin / public
   manifest status + last 3 sensitive-path commits with shortened SHAs.

---

## 3. Tap-target choices

Apple HIG says ≥44pt; we use `--tap: 44px` as a hard floor everywhere.

| Element | Implementation |
|---|---|
| Hero | 156px tall, entire card is `<button>` |
| Accordion summary | `min-height: var(--tap)` + 12px×14px padding |
| Stat cards (2-up grid) | `min-height: var(--tap)` |
| Pipeline run-bars | 56px tall (≥44pt), 4px gutter between |
| Pillar / mover / kv rows | `min-height: var(--tap)`, padded vertically |
| Bottom tab bar buttons | 60px wide × 60px tall (5 across in 375px) |

Hover-only states are forbidden. Every affordance reads in static / passive
state — color, dot, chip, sparkline — without requiring a hover.

The bottom tab bar is the only multi-section nav: **Cards / Verdict / History
/ QA / Help**. QA is the active tab and shows a small amber dot overlay
because the page is in warn state, mirroring iOS app-icon badging.

---

## 4. Mobile constraints — what we lean into

- **Single column** everywhere except the 2-up status tiles (Universe size,
  DQ flag count). Tables become stacked rows.
- **No horizontal scrolling.** The 7-day pipeline run-bar strip uses
  `flex: 1 1 0` per bar so it always fits 375px.
- **Sparkline is SVG** with `preserveAspectRatio="none"` so it stretches
  to any width without breaking.
- **Dark theme** mirroring `lthcs_tab/lthcs.css` tokens inline (band /
  text / border / bg / radius / font tokens copied verbatim). No
  `../lthcs.css` import; the mockup is fully self-contained.
- **Sticky bottom tab bar** with `backdrop-filter: blur(12px)` —
  iOS-native look, doesn't block content.
- **Tap targets only.** No drag handles, no swipe gestures, no
  long-press menus. The operator can use it one-handed in line at the
  coffee shop.

---

## 5. Tradeoffs

| Choice | Cost | Why we accept it |
|---|---|---|
| Hero shows the worst single thing, not an aggregate | Operator must drill in to see all sections | The hero's job is "should I scroll?", not "tell me everything" |
| All accordions default-collapsed (except Data Quality today) | An extra tap per section | The page is a glance surface; scanning all six sections via summary dots/tags is faster than scrolling expanded content |
| 24h mover reasons are summarized to one phrase | Loses precision vs. the full subscores breakdown | The mover row is a teaser; the full per-ticker drilldown is on the existing card-view detail page (link not shown in mockup) |
| Hardcoded data (no JS data-fetch) | Mockup will go stale | This is a design mockup, not a production page. A real implementation would hydrate from `data/lthcs/index/<date>.json`, `data/lthcs/snapshots/<date>.json`, and `data/lthcs/quality_audit/latest_summary.json` — all already committed. |
| Doesn't try to scale up to desktop | Looks small on a laptop | A parallel agent is designing the desktop variant. Trying to be both would compromise both. |
| Sparkline shows interpolated value for May 19 (snapshot gap) | Slightly fudges history | The gap is real (the 19th snapshot index file is missing from `data/lthcs/index/`); marking it explicitly in the body text ("(–)") preserves honesty without breaking the visual trend |

---

## 6. Real-data sources used

All numbers in the mockup are hardcoded from today's snapshot files:

| Field | Source |
|---|---|
| Composite −26, band "monitor", components | `data/lthcs/index/2026-05-20.json` |
| 167 tickers, top movers, DQ flags counts | `data/lthcs/snapshots/2026-05-20.json` |
| Pillar coverage %, verdicts, band counts | `data/lthcs/quality_audit/latest_summary.json` |
| 14 crons total | `.github/workflows/*.yml` count |
| Recent sensitive-path commits | `git log --oneline` against `.github/`, `requirements.txt` |
| Universe v2.2.0, last_updated | `data/lthcs/universe.json` |
| Thesis-fallback rationale | `data/lthcs/thesis_rotation.json` + memory note on AV AND-not-OR quirk |

---

## 7. Out of scope (deferred to a real implementation)

- **Live data binding.** The mockup is static. A real page would fetch
  the three JSON files above and degrade gracefully if any are missing.
- **Auto-refresh.** Existing health pages auto-refresh hourly via JS
  `setTimeout`. This page would inherit that pattern.
- **Real per-ticker drilldown.** The mover rows would link to the
  existing card-view detail page (`lthcs_tab/lthcs-detail.js`).
- **CSP/SRI header parity** with `lthcs_health/index.html`. The mockup
  is a design artifact; a real page in `lthcs_health/qa.html` would
  inherit the existing CSP meta tags.
- **Sticky bottom tab routing.** The five tabs are visual stubs; real
  navigation would route to existing pages.
