# LTHCS UX cross-page audit — 2026-05-19

**Author:** swarm agent DD (Phase 2 task 2.4)
**Scope:** read-only audit + light polish across all LTHCS UI sub-routes.
**Sibling Phase 2 work this closes out:**
- AA `2feda0f` — `/lthcs/` card view UX
- BB `00e2703` — `/lthcs/backtest/` restructure
- CC `b8df43f` — `/lthcs/crypto/` polish

---

## 1. Inconsistencies found

### 1a. Back-link copy + URLs drifted across sub-routes

The "back to card view" link had three different copy variants and one broken URL:

| Page | Copy | aria-label | href | Verdict |
|---|---|---|---|---|
| crypto | **Card View** | Back to card view | `../` | inconsistent casing |
| health | **Card View** | Back to card view | `../` | inconsistent casing |
| backtest | **Card View** | Back to card view | `../` | inconsistent casing |
| table | **Card view** | Switch to card view | `../lthcs/` | **broken URL** + diff copy |
| heatmap | **Card view** | Back to LTHCS card view | `../` | diff aria-label |

The table page's `href="../lthcs/"` resolved to `/lthcs/lthcs/` in production
and to a non-existent path on the dev server. The other four all used `../`
which is correct against the prod mount point (`/lthcs/<route>/ → /lthcs/`).

**Fix:** normalized all five to:
- Copy: **"Card view"** (sentence case, matches Anthropic-house style)
- aria-label: **"Back to card view"**
- href: **`../`**

### 1b. Card-view nav was missing entry points to half the sub-routes

`/lthcs/` only linked to **Heatmap** and **Crypto** in its header `<nav>`. The
**Backtest**, **Health**, and **Table** sub-routes were reachable only via the
deployment URL — not discoverable from the card view itself.

**Fix:** added three new ghost buttons (Table / Backtest / Health) so the
card view is the hub. Ordering: `Heatmap · Table · Crypto · Backtest ·
Health · About` (About last because it opens a modal, not a navigation).

### 1c. Narrative source toggle was undocumented

AA deferred adding helper copy to the Templated|LLM toggle (commit 5ebf973).
The LLM payload only first landed at 23:00 UTC tonight; a cold visitor
clicking "LLM" before that would see an "unavailable" state with no context
on what they're actually switching between.

**Fix:** added a `?` info icon next to the "AI narrative" heading with a
`title` tooltip explaining both modes, plus per-chip `title` tooltips. Pure
HTML `title` attr — no JS, works in Safari + keyboard focus.

---

## 2. Mobile Safari pass (≤640px and ≤375px)

### 2a. Card view nav overflow

With the new entry points the header `<nav>` jumped from 3 to 6 buttons,
which overflowed on narrow phones and caused the nav to wrap onto multiple
lines, eating vertical space above the fold.

**Fix:** at ≤640px the nav now scrolls horizontally with
`-webkit-overflow-scrolling: touch` and a hidden scrollbar. Matches the iOS
Safari tab-strip pattern. Buttons stay 44px tall (touch-target ok).

### 2b. Sub-route mobile coverage (verified, no new fixes needed)

- **Card view (AA):** chip-row drift filter already uses `flex-wrap: wrap`
  with chips at `min-height: 44px` + `touch-action: manipulation`. Active-
  filter breadcrumb has a `<640px` polish at line 1918 in lthcs.css. Clean.
- **Backtest (BB):** TOC chips and collapsibles have a dedicated `<640px`
  block (lthcs-backtest.css line 791). TOC label flex-basis 100% lets the
  chips wrap below it. Engine-window helper hides cleanly. Clean.
- **Crypto (CC):** `.lcry-cards { grid-template-columns: 1fr }` fires at
  ≤720px (line 564). Sparkline SVG uses `viewBox` so it scales fluidly with
  the card width. Modal payload + drift grid restack at ≤720/640. Clean.
- **Health:** dedicated `<600px` block (line 273). Clean.
- **Table:** dedicated `<768px` block + sticky-column behavior preserved.
  Hint text wraps; the table itself stays horizontally scrollable. Clean.
- **Heatmap:** has both `<900px` and `<560px` blocks. Cells reflow. Clean.

All five sub-routes inherit `-webkit-text-size-adjust: 100%` from
`lthcs.css` body, which they all `<link>`. v2 (separate stylesheet) sets it
explicitly on line 67.

### 2c. Touch targets

Refresh button + chip system both already enforce `min-height: 44px` and
`touch-action: manipulation` in `.lthcs-refresh-btn` (line 199) and
`.lthcs-chip` (line 1354). No regressions introduced.

---

## 3. Empty-state polish

| Page | Empty/error state before | After |
|---|---|---|
| Card view | already polished (AA) | unchanged |
| Backtest | "Validation in progress…" w/ retry | already polished (BB) — unchanged |
| Crypto | full empty-state w/ universe roster | already polished (CC) — unchanged |
| **Table** | one-liner "No tickers match…" / "Could not load snapshot…" | **two-line title + sub copy + cross-route deep link to `/lthcs/health/`** |
| **Health** | "Could not load snapshots/index.json. The pipeline has not produced any snapshots yet." | **friendlier two-line copy + card-view back-link** |
| **Heatmap** | "Could not load LTHCS data. Try refreshing." | **deep link to `/lthcs/health/` and back to card view** |

The pattern: a bold-ish title line + a sub line that explains *what to do
next* + a link out. Matches CC's crypto empty-state template.

---

## 4. Sparkline cross-import sanity check

CC's `lthcs_crypto/lthcs-crypto.js` imports:

```js
import { renderSparkline, bandColorForScore } from '../lthcs_tab/lthcs-sparkline.js';
```

Verified `lthcs_tab/lthcs-sparkline.js` exports both:

```js
export function bandColorForScore(score, bandColors = null) { ... }  // line 35
export function renderSparkline(history, options = {}) { ... }       // line 61
```

Both exports are named, the import statement matches, and **the prod
staging step** (`pages.yml` line 261-262):

```bash
mkdir -p _site/lthcs/lthcs_tab
cp -R lthcs_tab/* _site/lthcs/lthcs_tab/ 2>/dev/null || true
```

mirrors `lthcs_tab/` next to `/lthcs/crypto/`, so the relative path
`../lthcs_tab/lthcs-sparkline.js` resolves to
`/lthcs/lthcs_tab/lthcs-sparkline.js` — exists. On the dev server the same
relative path works because `lthcs_crypto/` and `lthcs_tab/` are siblings
under the repo root.

**Verdict: clean — no normalization needed.**

---

## 5. What I deferred

1. **Active-filter chip row scroll on `<375px`.** The card view's
   `.lthcs-active-filters` is `flex-wrap: wrap` and works correctly under
   stress; AA's note about it was speculative. Worth re-checking once we
   have real users on iPhone SE / 12 mini.
2. **Heatmap empty-state for "data file exists but is empty array."** The
   current path treats "JSON parse failed" and "empty universe" identically.
   Edge case — defer.
3. **Pages.yml heatmap mount.** Heatmap is currently mounted via the
   `cp -R lthcs_tab/*` blanket copy, not as a top-level branch. Works fine
   today but is implicit; a future refactor could lift it to an explicit
   block.
4. **lthcs_tab_v2 (visual-only experiment) header normalization.** v2 has
   its own design language (crypto-dashboard styling) and is intentionally
   walled off from the lthcs_tab tokens. Not part of the unified header
   pass.
5. **A "What changed?" diff link.** Each sub-route shows a static
   timestamp; there's no "compare to yesterday" UI yet. Out of scope for
   Phase 2.
