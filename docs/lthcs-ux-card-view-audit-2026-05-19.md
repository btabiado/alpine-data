# LTHCS card-view UX audit — 2026-05-19

Phase 2, task 2.1. Scope: the `/lthcs/` card view (the standalone
`lthcs_tab/` directory). Goal: identify the top friction points and
ship targeted fixes — not a full redesign.

## Audit method

Walked the page top-to-bottom as a first-time user, then as a returning
user with persisted filters. Re-read `index.html`, `lthcs-tab.js`,
`lthcs.css`, and the detail/about/index/sparkline/movers/regime modules
end-to-end. Cross-checked against `mockups/variant-c-indexed/` (the
canonical UI per commit `22ef488`) and the Variant C drill-down spec.

## Top friction points

1. **No sort control at all.** Cards rendered in whatever order the
   snapshot's `scores` array provided (effectively ticker A→Z). When the
   primary use case is "show me the most confident names" or "show me
   the weakest names within Monitor", the user had no way to ask for
   it. This is the single biggest "doesn't flow well" miss.
2. **Refresh feels broken on fast networks.** Clicking refresh disabled
   the button silently. With a warm browser cache the round-trip can
   finish in &lt;100ms — the user sees nothing happen and clicks again.
3. **Empty state was a dead end.** When filters produced no matches,
   the user got a one-line "No tickers match current filters." with no
   icon, no description of which filters were active, and no way to
   recover without scrolling back up.
4. **Variant C drill-down "selected" state was subtle.** Active index
   button only differed by a soft blue tint + a slightly thicker left
   stripe. Easy to miss which of S&amp;P 100 / DOW 30 / NASDAQ-100 you
   were drilled into.
5. **Header buttons had no visual hierarchy.** Refresh (primary action)
   shared the same chip styling as Heatmap, Crypto, About (secondary
   nav). All four read as a single flat row of equally-weighted
   choices.

## Fixes shipped

- **Sort dropdown** in the controls row with six modes (score
  desc/asc, trend desc/asc, ticker A→Z / Z→A). Default is `score-desc`
  — the right answer for a confidence-score table. Persisted to
  `localStorage` under `lthcs.sortMode`. Trend-sort modes re-order
  automatically when the trend map resolves a moment after first paint.
- **Refresh loading state.** Spinning icon + `aria-busy` for the
  duration of the fetch. Reduced-motion media query disables the
  animation gracefully.
- **Actionable empty state.** Icon, friendly title, dynamic
  description naming the active filters (e.g. "Active: S&amp;P 100 ·
  Elite · search 'xyz'"), and a "Clear all filters" button that
  preserves the drill-down index per Variant C rules.
- **Bolder Variant C selected-state.** Active index button now reads
  with a full accent-blue ring, deeper tinted background, bolder name,
  and a glow on the count.
- **Header visual hierarchy.** Refresh promoted to primary styling
  (filled chip + accent hover); Heatmap / Crypto / About demoted to
  ghost-button styling and grouped in a `<nav>` with a subtle divider.
- **"Showing N of M in &lt;Index&gt;" results-summary line** under the
  controls section. Gives the user a quick read of how the filter is
  shaping the universe without scrolling.

## Intentionally deferred (next pass)

- Sticky sort/search controls on long scroll — once filtered, the user
  has to scroll back up to change the sort. Mid-priority.
- Detail-modal narrative V1↔LLM toggle discoverability — works but the
  segmented control could carry a one-line "Templated vs. AI-generated"
  helper sub-text. Low priority.
- Movers/Regime strip entry points — they auto-show once data loads.
  No header anchor link to scroll back to them. Low priority; the
  layout is short enough today.
- Index button sub-text ("100 scored" / "Dow Jones Industrial Avg" /
  "97 scored") is inconsistent — DOW gets a name, the other two get a
  count. Should be uniform. Cosmetic; left for the next pass.
- Mobile (&le;480px): the controls row stacks but the chip-row Drift
  filter could use a horizontal scroll instead of wrap. Low priority.
