# LTHCS Revamp C — "Cockpit / At-a-Glance"

**File:** `lthcs_tab/mockups/revamp-C-cockpit/index.html`
**Angle:** Dashboard as instrument panel. Minimal text. Maximum signal density via shape, color, and motion. A 2-second glance answers "what's the read?" without reading.
**As-of data:** `2026-05-20` &middot; composite **&minus;26** &middot; band **NEUTRAL** &middot; 9 components.

---

## Visual hierarchy philosophy

The page enforces reading order by **physics**, not by labels.

1. **The score is the page.** `−26` is rendered at ~13vw (clamps 78–168 px) in monospace, with a colored radial glow behind it. It occupies the optical center. Eye lands there first because it is, by a wide margin, the largest contrast object on screen.
2. **The 30-day arc is the second beat.** A full-width sparkline sits directly to the right (or below, on mobile). Zero line dashed. Area filled in band tone. Drop endpoint dot. This is the verdict's *journey* — no caption needed.
3. **The gauge is the legend.** A horizontal red-to-green band gradient with a knife-edge needle gives the user a felt sense of "where on the spectrum am I?" Zone labels (DISTRIBUTING → ELITE) sit *underneath* the gauge so the gauge itself stays visually dominant.
4. **Components are tiles, not rows.** Each of the 9 inputs is a discrete instrument. Band lean (the ±30 component, biggest cap) gets a featured 2-column tile to mirror its weight in the math. The other 8 are uniform 1-column tiles. Reading order across the panel is left-to-right, top-to-bottom — the same order as the data file.

Every tile is built from the same four elements, stacked top-to-bottom:

```
┌─────────────────────────────────────────┐
│ [icon] NAME              [▲/▼/■] +/−N   │  ← head
│ 50.8                                    │  ← big number (the value)
│ ──────────●─────                        │  ← centered ±bar (today)
│   sparkline (30 days)                   │  ← trail
│ CAP ±10                       read text │  ← foot
└─────────────────────────────────────────┘
```

The big number is the **value**; the bar is the **contribution to the composite** (signed, capped); the sparkline is the **trail**. Three different views of the same component in one tile, glanceable in a half-second.

---

## Reading order enforced by size & contrast

| Rank | Element                         | Visual weight                                          |
|------|---------------------------------|--------------------------------------------------------|
| 1    | Hero score `−26`                | ~13vw mono, band-tone, radial glow behind              |
| 2    | Hero 30d sparkline + dot        | Full-width SVG, area fill, end-dot in band tone        |
| 3    | Hero label `NEUTRAL` + Δ over 30d | Bold, smaller, supports the number                   |
| 4    | Gauge needle on red→green track | Knife-edge needle, drop-shadow                         |
| 5    | Featured tile (Band Lean)       | 2-col, 48px num, 64px spark                            |
| 6    | The other 8 component tiles     | Uniform, 30px num, 28px spark                          |
| 7    | Legend strip                    | Mono, 11px, muted                                      |

No element in tier N can outweigh anything in tier N−1. Eye physics, not user discipline, drives the read order.

---

## Icons and glyphs (color is not the only signal)

Per the constraint that color cannot be the sole signal, every state has a redundant **shape** affordance:

- **Positive delta** &rarr; `▲` (triangle up) + green
- **Negative delta** &rarr; `▼` (triangle down) + red
- **Flat** (delta = 0)  &rarr; `■` (filled square) + blue
- **Component tile rail** (the 3 px left accent stripe) &mdash; signed color, also redundant with the arrow
- **Hero glyph** &mdash; reflects the 30-day **trend direction**, not just today's value, so you see "things are getting worse" via the arrow even before reading the number
- **Live status dot** &mdash; pulses, color-coded to "fresh"

Each component tile also carries a unique mark in its icon slot (`⚶ ◎ ⦿ ¤ ❖ ◈ ✵ ❖ ⧉`) so the row reads as nine distinct instruments rather than nine homogeneous boxes.

CSS-only tooltips (`.tip[data-tip]::after`) cover the "what does this mean?" affordance for any user who wants the long form &mdash; without spending pixel real estate on it by default.

---

## Color tokens (reused, not invented)

All band tones are inlined from `lthcs_tab/lthcs.css` `--band-*-bright` (kept in lock-step):

```
--band-elite-bright:        #4D7AB5
--band-high-bright:         #6FD18C
--band-constructive-bright: #E9C04A
--band-monitor-bright:      #F0A861   ← active page tone (band_key = "monitor")
--band-weakening-bright:    #E27A5C
--band-review-bright:       #C25640
```

Positive / negative / flat use the existing `--band-high-bright`, `--band-weakening-bright`, and a new flat-blue (`#6EA8FE`) that already exists in `lthcs.css` as `--drift-flat-accent`.

---

## Mobile behavior

- `@media (max-width: 760px)` &mdash; hero collapses to single column (score above sparkline). Tile grid drops from 3 cols to 2. Featured tile still spans 2 cols.
- `@media (max-width: 420px)` &mdash; tile grid collapses to a single column. Each tile becomes a full-width strip but **retains all four elements** (head, num, bar, spark, foot). The cockpit never degrades to illegible tiles; it linearizes.
- Hero score uses `clamp(78px, 13vw, 168px)` so the headline number always remains the largest object on the page regardless of viewport.

---

## Tradeoffs

**What we lose vs. the verdict-paragraph or guided-tutorial angles:**

- **Steeper learning curve for first-time visitors.** A new user lands on a wall of numbers and shapes with no introductory sentence telling them what the dashboard *is*. The legend strip and tooltips are the only safety net.
- **No narrative.** "Why is it −26?" doesn't get answered in prose. The user has to read the trail (declining 30d arc), look at the worst-bar tiles (Band Lean −23, 13F Breadth −8, Insider Breadth −7), and synthesize the story themselves. Power users love this; novices may bounce.
- **Information density requires attention.** This is a Bloomberg/cockpit aesthetic. It rewards 30 seconds of focused looking and punishes a half-second skim &mdash; though the hero is engineered to *make* that half-second skim still yield "negative, getting worse."

**What we gain:**

- **Speed.** Returning users get the read in <2 seconds without reading.
- **Density.** All 9 components, today + 30 days each, fit above the fold on desktop.
- **Memorability.** The shape of the chart sticks &mdash; "I remember the cliff" beats "I remember the paragraph."
- **Accessibility.** Shape + color + number redundancy. Screen readers get `aria-label`s with delta values; keyboard focus walks the tiles in DOM order; tooltip content is in `data-tip` attrs that screen readers can surface.

---

## Files touched

- `lthcs_tab/mockups/revamp-C-cockpit/index.html` &mdash; self-contained mockup (inline `<style>` + `<script>`, no external deps). Real data hardcoded from `data/lthcs/index/2026-05-20.json`. 30-day component trails mined from `data/lthcs/index/*.json`.
- `docs/lthcs-revamp-C-cockpit.md` &mdash; this rationale.

No production files (`lthcs_tab/index.html`, `app.py`, `v2/`, `.github/workflows/`) were modified.
