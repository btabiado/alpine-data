# LTHCS V2 Visual Layer — UX Research

**Author:** UX research pass for LTHCS tab V2
**Date:** 2026-05-17
**Status:** Recommendation — pick 3 to build next
**Scope:** Survey of 10 market dashboards, mapping to LTHCS data, ranked recommendations
**Out of scope:** Implementation, copy, color tokens (handed off to build agents). Heatmap build is already underway in `/lthcs/heatmap/` — this doc informs that work but does not edit it.

---

## 0. TL;DR — read this first

The current LTHCS tab is a **74 → 168 card grid** with search, filter chips (Index / Band / Drift), and a detail modal. It is honest, scannable for ~30 tickers, and falls apart past ~80. Bryan is right: a 168-card uniform grid is the wrong shape for "what's the universe doing today?"

**Build these three, in this order:**

1. **Sector-grouped treemap** (Finviz-style). Cells colored by band, sized by **inverse band rank × index weight proxy** (Elite = biggest), grouped by sector. This is the "5-second universe state" view. It is also the most visually striking for external readers.
2. **Sortable Bloomberg-style table.** Dense rows, sortable on score / drift / pillar / sector. This is the daily-conviction-call workhorse and the format experienced finance users actually trust.
3. **Movers strip** (top 10 by drift up / top 10 by drift down). A small leaderboard pinned above whatever view is active. This is what Bryan actually opens the page to look at.

**Defer to V3:** sector-subtotals dashboard (low marginal value over treemap), pillar comparison matrix (powerful but expensive to design well), confidence-band ladder (clever but niche), mobile-first redesign (do desktop-first, make it not break on phones, plan a real mobile pass later).

Everything else in this doc is supporting evidence. If you read only one more section, read **§4 Recommendations**.

---

## 1. Reference review

Ten platforms, one paragraph each. Pulled from my working knowledge of these products plus product-page conventions. The goal is not encyclopaedia coverage — it is "what should LTHCS steal."

### 1.1 Finviz Heatmap (`finviz.com/map.ashx`)

A squarified treemap of the S&P 500 (and other indices), with each cell sized by **market cap** and colored by **daily % change** on a red-to-green gradient through dark grey. Cells are grouped by sector, then industry, in nested rectangles. Hover gives a popup with name, ticker, price, % change, P/E, EPS. **What works:** the universe state is comprehensible in under 5 seconds. Sector blocks form coherent visual regions (e.g. a sea of red in Energy is immediately legible). Market-cap sizing means the eye prioritizes the names that actually matter for index performance. **What doesn't:** the gradient is too smooth — 0.0% and -0.4% look identical. There is no legend visible by default. On mobile it is practically unusable; the labels become illegible below ~12pt. Click behavior is weak (it goes to a Finviz quote page, not a real drill-down). Despite the flaws, this is the single most-imitated finance visualization for a reason.

### 1.2 Finviz Screener

A dense, sortable HTML table — ~20+ columns per row, monospaced numbers, alternating row backgrounds, sticky header. Filter row at top, pagination at bottom, ~20 rows per page. **What works:** information density is extreme but legible because of strong vertical alignment, narrow padding, and consistent right-alignment on numeric columns. Sortable everything. Column groups (Descriptive / Fundamental / Technical / Performance / Ownership) toggle in/out — power users build their own view and bookmark it. **What doesn't:** it is dated visually (Geocities-era table styling), abbreviations are inscrutable to newcomers (P/C, P/FCF, Curr R), and there is no way to "save a view" without an account. For LTHCS, the screener is the right *idiom* — copy the density and sortability, drop the visual style.

### 1.3 TradingView Heatmap (`tradingview.com/heatmap/stock/`)

Same squarified treemap pattern as Finviz, but visually modernized — softer rounded rectangles, better typography, dark-mode-first, smoother hover transitions. Configurable size source (market cap, volume, # of employees) and color source (% change, gap, volatility, multiple periods). Sector grouping is animated and toggleable. **What works:** the *configurability* is the lesson — same chart, three different stories depending on the size/color pivot. Their hover card is also better than Finviz: 1-week sparkline + key stats. **What doesn't:** the configurator surface is overwhelming for casual users (8+ size options, 6+ color options, multiple grouping modes). LTHCS should ship one good default and hide configuration in a "..." menu.

### 1.4 Koyfin dashboards

Customizable widget canvas — sortable tables, mini-charts, ticker cards, news feeds — composed into a dashboard the user drags around. **What works:** the widget vocabulary is small and consistent (table, chart, card, list). Each widget has a clear title, a clean header bar, and a single dominant interaction. Color is used sparingly and almost always to indicate sign (red/green) or category — never decoratively. **What doesn't:** the canvas-style customization is overkill for an opinionated product like LTHCS. We don't want users dragging widgets; we want them looking at one *good* layout. Steal the widget aesthetic, skip the drag-drop.

### 1.5 Yahoo Finance "Most Active" / "Top Gainers"

A simple sortable list: Symbol, Name, Last Price, Change, % Change, Volume, Market Cap. Cap of ~25 rows per category, with a "View More" link. **What works:** it is the canonical "movers" list. Everyone understands it. % Change has subtle red/green text color (not background fills) and is right-aligned. **What doesn't:** Yahoo's general design is cluttered (ads, news cards, snapshot charts) — the list itself is fine, the surrounding page is noise. For LTHCS, **a Yahoo-style "movers" strip pinned at the top of the page is a no-brainer**. It's the single most checked thing on any finance site.

### 1.6 Bloomberg Terminal (and Bloomberg.com data tables)

The Terminal is monospace, dense, color-coded, keyboard-driven, and unapologetic about being for professionals. Numbers right-aligned, descriptive columns left-aligned, sign-coded color on deltas (orange for up, blue/cyan for down — yes, *not* red/green, because Bloomberg got there before that convention solidified). Sticky tickers across the top. Bloomberg.com's market data tables are a calmer translation of the same idea: tight rows, monospace numerics, minimal chrome. **What works:** when you respect the user's intelligence, you can show 30+ data points per row without it feeling cluttered. The Terminal proves dense ≠ cluttered. **What doesn't:** the Terminal aesthetic is genuinely off-putting to casual investors and the monospace look reads as "old" outside finance. LTHCS should aim for *Bloomberg.com* density, not Terminal density.

### 1.7 Stocktwits sentiment view

Stocktwits surfaces three signals per ticker: bullish-vs-bearish sentiment (% bullish), message volume, and trending direction. They display this as a small "sentiment badge" (Bullish / Bearish / Neutral with a percentage) plus a sparkline of message volume over time. **What works:** they took two messy signals (sentiment, volume) and reduced each to a single colored chip + a tiny chart. Very legible at scale. **What doesn't:** the underlying data is noisy (retail forum sentiment ≠ informed conviction), and the badges encourage chasing whatever's hot. LTHCS should *not* copy the sentiment angle, but **the sentiment-badge format** (single colored chip with one number) is a great pattern for our **Band**.

### 1.8 Seeking Alpha quant ratings

Seeking Alpha shows each stock with a single dominant "Quant Rating" (A+ through F, with a clear color gradient from emerald to red) plus five sub-grades (Valuation, Growth, Profitability, Momentum, Revisions) arrayed as a row of letter pills underneath. **What works:** this is almost exactly the LTHCS pattern (composite + 5 pillars) and they show that **the composite belongs front and center**, with pillars as a secondary row of pills. Their rating-history line chart on each ticker page is also excellent — a 5-year line of how the rating evolved is more compelling than the current snapshot alone. **What doesn't:** the letter grades collapse a lot of nuance into 11 buckets, and the historical line implies more precision than the underlying model has. For LTHCS, **steal the layout** (big composite + row of pillar pills + historical line) and use it as the **detail modal** layout.

### 1.9 Robinhood (mobile-first)

Robinhood's mobile view is hero-driven: big company name, big price, big sparkline, then a tight stack of cards beneath (Buying Power, About, News, Analyst Ratings). One ticker per screen. Tap anything to expand. **What works:** the visual hierarchy is brutal — there is exactly one thing per screen that matters, and it's the price. Everything else is below the fold. **What doesn't:** this idiom doesn't translate well to a universe view — Robinhood doesn't really *have* a universe view; they have a watchlist. For LTHCS, the lesson is the **detail modal**: on mobile, treat each ticker as a hero screen with a single composite score, a sparkline of drift, and pillar pills below.

### 1.10 Atom Finance (mobile-first dashboard)

Atom is what Robinhood's analyst view would be if it were designed for an actual professional. Tight cards, multi-metric, lots of small charts. On their mobile dashboard, you get a vertical stack of swipeable cards — each card is a self-contained "screen" of related metrics (Valuation card, Momentum card, etc.). **What works:** the vertical scroll + swipeable horizontal carousel pattern is genuinely good mobile UX for finance data. Each card is one screen high, no more, so you never lose context. **What doesn't:** desktop is an afterthought, and the cards-of-cards pattern doesn't scale to a 168-ticker universe view. The lesson: **for the LTHCS detail modal on mobile, use a vertical stack of self-contained card-screens**, one per pillar.

### 1.11 Honorable mentions (not full reviews, used as cross-reference)

- **Google Finance:** clean, minimal, but very thin on data. Their sparklines-in-tables pattern is good — copy that.
- **TradingView screener:** modern take on Finviz screener, with embedded mini-charts inside table rows. Heavy but beautiful. Probably the gold standard for table-with-charts.
- **Morningstar:** moat/stewardship/uncertainty pills — very LTHCS-shaped. Their "Fair Value Estimate vs Price" bar gauge is worth copying for Band visualization.

---

## 2. Comparison matrix

How these dashboards stack up on the dimensions that matter for LTHCS.

| Platform | Info density | Scan speed (5-sec universe state) | Mobile fit | Color use | Primary chart type | Audience |
|---|---|---|---|---|---|---|
| **Finviz Heatmap** | Very high | Excellent | Poor | Red/green gradient + sector grouping | Treemap | Power retail, screeners |
| **Finviz Screener** | Extreme | Poor (need to sort first) | Very poor | Minimal, sign-only on deltas | Dense table | Power retail, traders |
| **TradingView Heatmap** | Very high | Excellent | Poor | Red/green gradient + sector grouping | Treemap | Active traders, prosumers |
| **Koyfin** | High | Good (per widget) | OK | Sparing, semantic only | Mixed widgets (table + cards + charts) | Buy-side, prosumer |
| **Yahoo Movers** | Medium | Excellent (single list) | Good | Sign-only red/green | Sorted list | Casual retail |
| **Bloomberg Terminal** | Extreme | Good (for trained users) | N/A (desktop only) | Orange/cyan sign-coded | Dense table + small charts | Professional |
| **Bloomberg.com** | High | Good | Good | Subtle, sign-only | Tables + sparklines | News-reading professional |
| **Stocktwits** | Low | Good (single sentiment chip) | Excellent | Strong chip colors | Sentiment badge + volume sparkline | Retail / social |
| **Seeking Alpha quant** | Medium | Excellent (single letter) | Good | Letter-grade gradient (A+→F) | Big score + pillar pills + history line | Retail investors |
| **Robinhood** | Low (deliberate) | Per-ticker only | Excellent | Sign-only red/green | Hero number + sparkline | Casual retail / mobile-first |
| **Atom Finance** | High | Good (per card) | Excellent | Sparing, sign-only | Vertical stack of mini-cards | Mobile-first prosumer |
| **LTHCS today** | Medium-high | **Poor at 168 tickers** | Acceptable | Strong band chips | Card grid | Bryan + future investors |

**Reading this matrix:** the two formats that win on "5-second universe state" are **treemap** and **single-ranked list**. The format that wins on "I want to make a real decision" is **sortable dense table**. LTHCS should ship one of each, plus keep the card grid as a third "browse" view.

---

## 3. Mapping LTHCS data to candidate formats

LTHCS provides per ticker (confirmed from `data/lthcs/universe.json` and surrounding pillar code):

- **Ticker** (string, e.g. AAPL)
- **Name** (e.g. Apple Inc.)
- **Composite score** (0–100, integer-ish)
- **Band** (Elite / High / Constructive / Monitor / Weakening / Review) — 6 ordinal buckets
- **5 pillar sub-scores** (each 0–100): the pillar names vary, but conceptually Quality / Growth / Capital allocation / Conviction signal / Risk
- **30-day drift** (signed delta of composite score over the last 30 days)
- **Index membership** (DJIA, NASDAQ-100, S&P 100, S&P 500 — multi-valued)
- **Sector** (GICS-style: Technology, Healthcare, Financials, etc., ~11 buckets)
- **Industry** (finer-grained ~50–60 buckets)
- **Maturity stage** (e.g. standard_compounder, dividend_aristocrat, hyper_growth — ~5–8 buckets)
- **Data-quality flags** (boolean / enum: missing data, stale, model uncertainty)

For each candidate format, here is how each field maps in. **Bold = primary visual encoding for that format.** Italic = available but secondary.

### 3.1 Sector-grouped treemap

| LTHCS field | Encoding |
|---|---|
| Composite score | *Sort order within sector block; hover detail* |
| **Band** | **Cell fill color** (categorical, 6 levels) |
| 5 pillars | *Hover popup only* |
| 30-day drift | *Optional: small arrow/triangle in corner, or border accent* |
| Index membership | *Hover popup; could be a subtle border style* |
| **Sector** | **Outer grouping rectangle / region** |
| Industry | *Optional inner sub-grouping; probably skip — too many levels* |
| Maturity stage | *Hover popup* |
| Data-quality flags | *Stripe pattern or hatched fill if present* |
| (size source — derived) | **Cell size** = inverse band rank (Elite biggest) × index-weight tier proxy |

**Notes:** size is the hardest decision (see §5). Color must be band — anything else fights the eye.

### 3.2 Sortable Bloomberg-style table

| LTHCS field | Encoding |
|---|---|
| **Ticker** | **First column, monospace, left-aligned** |
| Name | Second column, truncated |
| **Composite score** | **Numeric column, right-aligned, light background-fill bar to show value** |
| **Band** | **Pill chip in its own column, sortable as ordinal** |
| 5 pillars | 5 numeric columns, right-aligned, optional micro-bar |
| 30-day drift | Signed numeric column with red/green text + small up/down arrow |
| Index membership | Compact icon set (D / N / 100) |
| Sector | Text column, sortable, optionally with color dot |
| Industry | Hidden by default, available in column picker |
| Maturity stage | Compact label or icon |
| Data-quality flags | Small ⚠ icon in a flags column |

**Notes:** column visibility should be toggleable. Default view: Ticker, Name, Composite, Band, 5 pillars, Drift, Sector, Flags. That's ~12 columns and that's fine.

### 3.3 Sector-grouped card view (current grid, but grouped)

| LTHCS field | Encoding |
|---|---|
| **Sector** | **Section header / vertical band** |
| Composite score | Big number in card |
| **Band** | **Card border / chip color** |
| Drift | Mini sparkline or signed number |
| Pillars | Five micro-bars at card bottom |
| Index membership | Icon row |
| Maturity stage | Subtitle text |
| Data-quality flags | Warning icon |

**Notes:** this is the lowest-lift improvement over the current grid. Take what exists, add `<h2>` section breaks per sector. Don't over-engineer.

### 3.4 "Movers" leaderboard (top 10 drift up / top 10 drift down)

| LTHCS field | Encoding |
|---|---|
| **30-day drift** | **Sort key + signed bar** |
| Ticker | Left column |
| Name | Truncated subtitle |
| Composite score | Right side number |
| Band | Small pill |
| Sector | Color dot |

**Notes:** trivially small. Two columns of 10 rows each. Pin above main view.

### 3.5 Big-number hero (per-ticker, in modal)

| LTHCS field | Encoding |
|---|---|
| **Composite score** | **Giant centered number (96pt)** |
| **Band** | **Subtitle pill (24pt)** |
| Drift | Sparkline + signed delta beneath |
| 5 pillars | Row of pillar pills below sparkline |
| Sector / industry | Small text in header |
| Maturity stage | Small text in header |
| Index membership | Icon row |
| Data-quality flags | Inline warning callout |

**Notes:** this is the **detail modal**, refined. Today's modal is dense; lead with the score.

### 3.6 Pillar comparison matrix

A 5-column-by-N-row grid where rows are tickers and columns are pillars, with each cell a heat-shaded square showing that pillar's score. Like a `seaborn` heatmap of pillars.

| LTHCS field | Encoding |
|---|---|
| **Ticker** | **Row label** |
| **Pillar score** | **Cell color (heat gradient)** |
| Composite score | Sort key on rows |
| Sector | Optional row grouping |
| Band | Row's left-edge accent color |
| Drift | Right-edge accent or trailing sparkline |
| Index | Filter only |

**Notes:** powerful for finding "high quality but weakening capital allocation" patterns. Expensive to design well — the legend, scale calibration, and ordering all matter.

### 3.7 Confidence-band ladder

Six horizontal lanes (one per band), tickers as chips inside each lane, ordered left-to-right by composite score within the lane.

| LTHCS field | Encoding |
|---|---|
| **Band** | **Lane (vertical position)** |
| **Composite score** | **Horizontal position within lane** |
| Ticker | Chip label |
| Drift | Chip color tint or small arrow |
| Sector | Could group chips inside lane; probably skip |

**Notes:** novel, but the value vs. a sorted table is unclear. Pretty for screenshots; weak for decisions.

---

## 4. Recommended formats for LTHCS V2 — ranked

Three picks, in build order. Treemap first because it directly answers Bryan's "more visual" ask and the "5-second universe state" external-reader requirement. Table second because it is the decision-making workhorse. Movers strip third because it's small, cheap, and constantly useful.

---

### 🥇 #1 — Sector-grouped treemap (Finviz-style)

**One-paragraph description.** A squarified treemap of all 168 tickers, with each cell sized by a **score-weighted index proxy** (described in §5) and colored by **band**. Cells are grouped into sector blocks separated by 2px gutters; sector blocks have soft labels in their upper-left corner. Hover reveals a popup with ticker, name, composite, drift, and pillar mini-bars. Click opens the existing detail modal. A small legend in the bottom-right shows the 6 band colors and the size rule. This is the marquee view — the thing you screenshot to show what LTHCS is.

**ASCII wireframe.**

```
+----------------------------------------------------------------------------------+
| LTHCS Universe State                                       [Treemap | Table | Cards] |
| Filter: [Index ▼] [Band ▼] [Drift ▼]                       [Search...........] [⋮] |
+----------------------------------------------------------------------------------+
| Movers ▲ AVGO +8 · NVDA +6 · LLY +5 · ASML +4 · ...   Movers ▼ INTC -7 · BA -6 · ...|
+----------------------------------------------------------------------------------+
|  TECHNOLOGY                       | HEALTHCARE         | FINANCIALS              |
|  +----------+----+----+           | +--------+-----+   | +---+---+---+---+---+   |
|  |          |    |    |           | |        |     |   | |JPM|BAC|WFC|GS |MS |   |
|  |   AAPL   | MS | NV |           | |  UNH   | LLY |   | +---+---+---+---+---+   |
|  |   Elite  |FT  |DA  |           | |  Hi    | Eli |   | | C |...|...|...|...|   |
|  +----------+----+----+           | +--------+-----+   | +---+---+---+---+---+   |
|  +--+--+--+----+----+              | +---+---+---+--+  |                          |
|  |GO|AM|AD| ORCL|CRM |              |JNJ|PFE|MRK|ABBV| | CONSUMER DISC           |
|  |OG|ZN|BE| Hi  |Hi  |              |...|...|...|... | | +-----+---+---+        |
|  +--+--+--+----+----+              | +---+---+---+--+  | |AMZN |...|...|        |
|                                    |                    | +-----+---+---+        |
|  ENERGY                INDUSTRIALS  | UTIL | STAPLES   | ...                     |
|  +-----+---+---+      +---+---+--+ |+--+ |+----+---+ |                            |
|  |XOM  |CVX|COP|      |GE |CAT|UPS|| .. | |...| ...| |                            |
|  +-----+---+---+      +---+---+--+ |+--+ |+----+---+ |                            |
+----------------------------------------------------------------------------------+
| Legend: ■ Elite  ■ High  ■ Constructive  ■ Monitor  ■ Weakening  ■ Review        |
| Size: cell area ∝ score × index weight tier                                       |
+----------------------------------------------------------------------------------+
```

**Encoding.**
- **Size** = `composite_score × index_weight_tier`, where index_weight_tier = {DJIA: 1.4, S&P 100: 1.2, NASDAQ-100: 1.1, S&P 500: 1.0, none of those: 0.8}. Boosts coverage to ensure Elite/High tickers are visually dominant even within smaller sectors. See §5 for full rationale and alternatives.
- **Color** = band (categorical 6-level palette; see §6 anti-patterns about contrast).
- **Position** = grouped by sector, then sorted descending by size within sector.
- **Hover** = ticker, composite, drift, pillar mini-bars, sector, maturity.
- **Click** = open existing detail modal (reuse).

**Best use case.** Daily "what does the universe look like today?" + external readers who need to grasp it in 5 seconds. Also the best Twitter/Slack screenshot.

**Build complexity:** **M.** D3's `treemap` + `treemapSquarify` does the layout; the real work is sector grouping (two-level treemap), label legibility at small sizes (skip labels below ~40px on the long edge), and good hover/click. A coding agent should be able to land this in 1–2 days. Use `d3-hierarchy` + plain SVG; no need for a framework.

**Mobile suitability.** **Acceptable, not great.** A 168-cell treemap on a 390px-wide screen forces tiny cells. Two mitigations: (a) on viewports under 600px, collapse to single-sector cards (one screen height per sector, swipeable), or (b) ship desktop-only treemap and route mobile users to the sorted table by default. **Recommend option (b)** — don't waste design time fighting physics.

---

### 🥈 #2 — Sortable Bloomberg-style table

**One-paragraph description.** A dense, sortable, sticky-header table showing all 168 tickers in ~12 columns: Ticker / Name / Composite / Band / 5 pillars / Drift / Sector / Flags. Numbers right-aligned, monospace. Band is a colored pill. Composite has a subtle horizontal bar fill in its cell (the in-cell bar is the secret weapon — gives you density + visualization for free). Drift is signed with red/green text and a small triangle. Click a column header to sort. Click a row to open the detail modal. Toggle column visibility via a "..." menu. Default sort: composite descending.

**ASCII wireframe.**

```
+--------------------------------------------------------------------------------+
| LTHCS Table                                          [Treemap | Table | Cards] |
| Filter: [Index ▼] [Band ▼] [Drift ▼]              [Search........] [Cols ⋮]    |
+--------------------------------------------------------------------------------+
| Ticker  Name              Comp▼  Band   Q   G   C   M   R   Drift  Sector  ⚠  |
+--------------------------------------------------------------------------------+
| AAPL    Apple Inc.        ████ 92  ■Elite 95 88 94 90 91   +2 ▲   Tech         |
| MSFT    Microsoft Corp.   ████ 91  ■Elite 93 92 95 87 89   +1 ▲   Tech         |
| LLY     Eli Lilly         ████ 90  ■Elite 88 96 89 92 86   +5 ▲   Health       |
| NVDA    NVIDIA Corp.      ███▌ 88  ■Elite 87 99 78 95 80   +6 ▲   Tech         |
| AVGO    Broadcom Inc.     ███▌ 87  ■High  85 90 88 88 84   +8 ▲   Tech         |
| UNH     UnitedHealth      ███  84  ■High  86 80 89 78 88   -3 ▼   Health    ⚠ |
| GOOG    Alphabet          ███  83  ■High  88 82 79 82 84    0     Tech         |
| META    Meta Platforms    ███  82  ■High  84 85 78 88 75   +3 ▲   Tech         |
| ...                                                                            |
| INTC    Intel Corp.       █▌   46  ■Weak  58 30 45 52 45   -7 ▼   Tech      ⚠ |
| BA      Boeing Co.        █▌   44  ■Weak  62 35 38 48 40   -6 ▼   Indust    ⚠ |
+--------------------------------------------------------------------------------+
| 168 tickers · sorted by Composite desc · [Export CSV]                          |
+--------------------------------------------------------------------------------+
```

**Encoding.**
- **Position** = sort order (user-controlled, default = composite desc).
- **Composite** = numeric + in-cell horizontal bar fill (0–100 scale).
- **Band** = pill chip color.
- **Pillars** = numeric, right-aligned, small enough that they recede unless you focus.
- **Drift** = signed numeric, red/green text, triangle.
- **Flags** = warning icon column.
- **Click row** = detail modal.

**Best use case.** "I need to make a decision today." Power-user workflow: sort by drift desc, scan for high-band names with positive drift; or sort by pillar to find single-pillar leaders. This is the view Bryan should bookmark.

**Build complexity:** **S.** Plain HTML table + a tiny sort helper. No virtualization needed at 168 rows. The trickiest part is the in-cell composite bar (use a CSS `background: linear-gradient(...)` trick — it's 5 lines). A coding agent can land this in half a day. Reuse the existing band-color CSS variables.

**Mobile suitability.** **Surprisingly good if you do it right.** Make the table horizontally scrollable, freeze the Ticker + Composite columns, and use compact font sizes. This is the right default mobile view. Pin the search/filter row at the top.

---

### 🥉 #3 — "Movers today" leaderboard strip

**One-paragraph description.** A two-column horizontal strip pinned above whatever view is active: **Top 10 by drift up** on the left, **Top 10 by drift down** on the right. Each entry is a row with ticker, mini sparkline of the last 30 days of composite, a signed drift number, and a small band pill. Click a row to open the detail modal. Fits in ~200px of vertical space. This is the "what changed?" view.

**ASCII wireframe.**

```
+----------------------------------------------------------------------+
|  Movers — 30-day drift                                  [hide ▼]     |
+----------------------------------------------------------------------+
|  ▲ Improving                       |  ▼ Weakening                    |
|  AVGO  ~~^^~~/~^   +8  ■High       |  INTC  ~~\~_~_\_   -7  ■Weak    |
|  NVDA  __/~~^/^~   +6  ■Elite      |  BA    ~~~\__\~_   -6  ■Weak    |
|  LLY   ~^^~~/~^^   +5  ■Elite      |  PFE   ~^~\__\~~   -5  ■Mon     |
|  ASML  __/~^~~/~   +4  ■High       |  KO    ~~~~~\\~~   -4  ■Mon     |
|  ...                               |  ...                            |
+----------------------------------------------------------------------+
```

**Encoding.**
- **Position** = sorted by drift magnitude.
- **Sparkline** = 30-day composite history.
- **Band pill** = current band.
- **Drift number** = signed.
- **Click** = detail modal.

**Best use case.** The first thing Bryan looks at when opening the page. Also the highest-information-per-pixel widget in the whole tab.

**Build complexity:** **S.** Two columns × 10 rows of data, the sparkline component is already presumably built (`lthcs-sparkline.js` exists in `lthcs_tab/`). Maybe 2–4 hours total.

**Mobile suitability.** **Excellent.** Just stack the two columns vertically — `▲ Improving` first, then `▼ Weakening`. Each row is a one-line item; fits a phone perfectly.

---

### Why these three and not the others

- **Sector-subtotals dashboard** — duplicates what the treemap shows visually. Build it only if treemap turns out to be too information-dense for casual users (unlikely, given Finviz's broad appeal).
- **Pillar comparison matrix** — genuinely powerful for analytical work, but the design surface is large (scale calibration, ordering, legend) and the audience is narrow (probably only Bryan uses it). Defer to V3 and treat as a power-user feature.
- **Confidence-band ladder** — pretty, but adds little decision value over the table. The band is already encoded everywhere else.
- **Sector-grouped card view** — incremental over the current grid. Worth doing as a 30-minute polish on the existing cards (just add sector `<h2>`s), but not a "format" in its own right.

---

## 5. Heatmap specifics

The user explicitly asked for this section. Here are the design decisions, with picks.

### 5.1 What should cell SIZE represent?

Five options, ranked:

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **Market cap** | Industry-standard. Reads as "real money at stake." Makes the heatmap directly comparable to Finviz. | Requires adding market cap to the pipeline (not currently in `universe.json`). Reinforces "big = important" which is true for index performance but **wrong for LTHCS** — a small-cap Elite is more interesting than a mega-cap Monitor. | **Skip.** Adds engineering load *and* fights the LTHCS thesis. |
| **Equal weight** (uniform grid) | Simplest. Every ticker gets the same visual weight. No pipeline change. | Becomes a 168-cell grid — basically the current card view but compressed. Loses the visual hierarchy that makes treemaps work. | Skip. Not really a treemap then; it's a grid. |
| **Composite score** | Directly meaningful. Big cells = high conviction. Reads cleanly. | A Monitor-band ticker at 45 has half the area of an Elite at 90. But Monitors are still useful to see. Risks visually erasing the bottom of the universe. | OK but flawed. |
| **Inverse band rank** (Elite biggest, Review smallest) | Visual hierarchy matches LTHCS conviction. Easy to explain. | Effectively a banded variant of "composite score" with discrete steps. | OK. |
| **Score × index-weight tier** ⭐ | Combines LTHCS conviction with universe-importance (index membership is a real proxy for "size that matters" without needing market cap data). Already in `universe.json`. | A new derived metric; needs a tiny bit of explaining. | **Pick this.** |

**Recommendation: size = `composite_score × index_weight_tier`**, where:

```
tier = 1.4  if 'DJIA' in index_membership
tier = 1.2  elif 'S&P 100' in index_membership
tier = 1.1  elif 'NASDAQ-100' in index_membership
tier = 1.0  elif 'S&P 500' in index_membership
tier = 0.8  else
```

Rationale: ensures the visual hierarchy roughly matches "names that matter" without requiring market cap. DJIA tickers get a small boost (30 names, all huge). The S&P 500-only names are baseline. Anything not in those indices is gently de-emphasized. **Document the rule plainly in the legend so it's not magic.** Don't expose this as a user toggle in V2 — pick one default, ship it.

### 5.2 What should cell COLOR represent?

| Option | Verdict |
|---|---|
| **Band (categorical 6-level)** ⭐ | **Pick this.** The band is the LTHCS thesis. Coloring by anything else fights the model. |
| Drift sign (red/green) | Loses the conviction signal entirely. A Monitor-band ticker drifting up still isn't a buy. |
| Composite score (continuous gradient) | Reads OK but blurs the band boundaries that LTHCS works hard to draw. |
| Sector | Redundant with the spatial grouping. Wasted channel. |

**Recommendation: color = band.** Use the existing band-color tokens from the current LTHCS tab (don't reinvent). One **secondary visual signal** worth adding: a small triangle in the top-right corner of each cell indicating drift direction (▲ +n / ▼ -n / no triangle if |drift| < 1). This adds the "what's changing?" dimension without losing the band signal.

### 5.3 Hover / click behavior

- **Hover (desktop):** popup card with ticker, name, composite, band, 5 pillar mini-bars, drift (with arrow), sector, industry, maturity stage, data-quality flags. Roughly the same content as a current LTHCS card but more compact. ~200ms delay before showing.
- **Click (any device):** open the existing detail modal. **Reuse, don't reinvent.** The modal already exists in `lthcs_tab/lthcs-detail.{js,css}`.
- **Tap on mobile:** opens modal directly (no hover affordance on touch).

### 5.4 Sector grouping vs. flat grid

**Pick sector grouping.** Three reasons:
1. The universe spans ~11 sectors, which is exactly the right cardinality for the human eye to parse spatial regions.
2. "Health Care all turned red today" or "Energy is solidly Elite" are real LTHCS observations and need a visual home.
3. Sector grouping makes the treemap *look like* the Finviz heatmap, which is the mental model the audience brings.

**Industry sub-grouping?** No. Too many industries (~60), and the inner labels would be illegible. Stop at one level of grouping.

### 5.5 168 tickers without illegibility

Three techniques, all needed:

1. **Hide labels below a size threshold.** If a cell's short edge is under ~32px, render the cell without any label — the color and position still communicate. The popup still works on hover. Finviz does this.
2. **Show only the ticker, not the name, inside cells.** Names go in the popup. Tickers are 3–5 chars; names can be 30+.
3. **Maintain minimum gutters.** 2px between cells, 4px between sectors. Without gutters, the grid becomes a single colored mass.

If 168 still feels cramped: provide a **filter** that limits the treemap to one index at a time (DJIA only = 30 cells, very legible; S&P 100 = 100 cells, comfortable). The filter chips already exist in the LTHCS tab — reuse.

---

## 6. Anti-patterns

What NOT to do. These are mistakes the surveyed dashboards (and probably some prior LTHCS iterations) make. Don't repeat them.

1. **Gradient confetti.** Don't color by composite-score continuous gradient *and* outline by band *and* tint by drift. Each cell gets one dominant color channel; everything else is a secondary cue. The Finviz heatmap works because color does one thing (% change). Pick one.
2. **Low-contrast band colors.** The current LTHCS band palette is probably OK, but verify Elite vs High and Monitor vs Weakening adjacent pairs at AAA contrast on both light and dark backgrounds. If two adjacent bands are visually indistinguishable, the entire treemap fails.
3. **Decorative animation.** Hover transitions should be <150ms. Don't animate cell entry on page load — for 168 cells it looks chaotic and delays time-to-information. Static load, animated only on user interaction.
4. **Density without a legend.** A treemap without a visible legend is hostile to first-time viewers (and external readers). Pin a small legend in the bottom-right of the treemap. Always. Same for the in-cell composite bar in the table — label the scale somewhere.
5. **Auto-refresh that re-jiggles layout.** If the data updates daily, fine, but **don't reflow on view**. The reader's mental map of "where is JPM in this layout?" is valuable. Layout stable per day.
6. **Hover-only information on touch devices.** Mobile users get no hover. Always provide a tap-to-open-modal fallback. Don't put critical info behind hover-only popups.
7. **Coloring sectors AND coloring cells.** If you color sector backgrounds, the cells fight visually. Sector backgrounds should be neutral grey/dark-grey (or invisible — just gutters do the grouping).
8. **Trying to fit everything on one screen.** Don't shrink the treemap to fit above-the-fold next to the table next to the cards. Each view is its own toggle. One view per screen.
9. **Renaming "Band" to be cute.** The taxonomy (Elite / High / Constructive / Monitor / Weakening / Review) is good. Don't relabel to emoji or color names. Trust the user to learn six words.
10. **Treating drift as a primary color signal.** Drift is a delta; band is a level. If color = drift, the heatmap becomes a daily mood ring, not a conviction view. Drift goes in the corner triangle, not the fill.

---

## 7. Implementation notes

### 7.1 Routing — sub-routes vs. toggle

Two options:

**Option A — Sub-routes.** `/lthcs/`, `/lthcs/heatmap/`, `/lthcs/table/`, `/lthcs/movers/`.
- **Pro:** Each view is its own URL, deep-linkable, screenshottable, individually cacheable. Matches the convention already in motion (a `/lthcs/heatmap/` build is underway).
- **Pro:** Bryan can bookmark the table view directly.
- **Con:** Some duplicated chrome (filter bar, header). Solvable with shared partials.

**Option B — Toggle inside the existing tab.** Single URL, view state in a query param `?view=heatmap`.
- **Pro:** Cheaper to ship, single page bundle, easy switching.
- **Con:** Heavier initial JS payload (all views load). External readers can't link to "the table view" directly.

**Recommendation: hybrid.** Use sub-routes (`/lthcs/heatmap/`, `/lthcs/table/`) for the three new views, but render a **persistent toggle bar** at the top of all four pages ("Treemap | Table | Cards | Movers") so switching is one click. This matches the agent already at work in `/lthcs/heatmap/` and avoids inventing a router. Each view loads only its own JS.

### 7.2 Shared chrome

All four views (cards / treemap / table / movers) should share:
- Header with title + view toggle.
- Filter chips (Index / Band / Drift) — these already exist; lift to a shared partial.
- Search input.
- Detail modal (already shared via `lthcs-detail.js`).
- Color tokens / band palette (lift to a CSS var file if not already).
- The **movers strip** lives at the top of the cards, treemap, and table views.

### 7.3 Data source

All views read from the same `data/lthcs/universe.json` (and whatever score/drift artifacts exist in `data/lthcs/`). No new data pipeline work. The treemap's derived size metric is computed client-side from `composite_score` + `index_membership`. If market cap is ever added later, swap the size source via a URL param without changing the layout code.

### 7.4 What lands in V2 vs V3

**V2 scope (this work):**
- Sector-grouped treemap at `/lthcs/heatmap/` (in progress).
- Sortable table at `/lthcs/table/`.
- Movers strip — shared component, pinned above cards/treemap/table.
- Shared filter + search + view toggle bar.
- Detail modal polish (lead with composite score as hero).

**V3 / future:**
- Pillar comparison matrix.
- Real mobile redesign (vertical card-per-pillar swipe stack à la Atom Finance).
- Saved views / view presets.
- Historical band-rating line chart in the detail modal (steal from Seeking Alpha).
- Confidence-band ladder, if anyone asks for it. (Probably nobody will.)
- Industry sub-grouping toggle on treemap, if there's appetite. (Skeptical.)

### 7.5 Testing notes

- **Visual regression:** screenshot the treemap with a fixed dataset and diff. Layout stability is a feature.
- **A11y:** every band color must pass AAA contrast on both light and dark backgrounds against text. Treemap cells need ARIA labels for screen readers (label = "Ticker NAME, Composite N, Band X, drift ±N").
- **Performance:** 168 cells × hover handlers — should be fine without virtualization, but verify on mobile Safari (per Bryan's recurring concern about Safari cache + perf). Avoid heavy SVG filters; flat fills only.

---

## 8. Closing opinion

Stop adding cards. The 74 → 168 expansion already proved the format doesn't scale. The next three views — **treemap, table, movers** — give Bryan three distinct mental models for the same data:

- **Treemap = "what does the universe look like today?"** (5-second scan, screenshot-worthy, external readers love it.)
- **Table = "what should I do today?"** (sort, filter, decide.)
- **Movers = "what changed?"** (always pinned, always glanceable.)

The current cards become the fourth view — "browse" — and they're fine for that. Don't kill them; just stop treating them as the only entry point.

Ship the treemap first because it answers Bryan's explicit ask and is the highest-leverage external-facing artifact. Ship the table second because it's the cheapest big win and is what experienced users actually want. Ship the movers strip third because it pays for itself in every session.

Everything else in this doc is supporting evidence for those three picks.

— end —
