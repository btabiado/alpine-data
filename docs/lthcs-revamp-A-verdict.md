# LTHCS landing page — Revamp A: "Inverted Pyramid / Verdict-first"

Mockup: `lthcs_tab/mockups/revamp-A-verdict/index.html`
Data as-of: 2026-05-20 (score `-26`, label `LTHCS NEUTRAL`, 167-ticker universe).

## Core thesis

The user feedback was the diagnostic: *"I need a clear path to reading — easy
button. Not clear to new users what message we are providing and how to flow
the index."* The current page answers that question with a gauge, a number,
and a 9-row table. A first-time visitor has to **assemble** the message from
fragments.

Revamp A removes the assembly. The page is a Bloomberg lede: **one sentence
gives the answer**, the next paragraph qualifies it, then progressively-deeper
detail. No chart library, no sparkbars, no carousel of tiles. Words, hierarchy,
type scale. The reader is never more than three sentences from "what does
this mean for me today."

The hypothesis: a long-term-hold thesis dashboard is, structurally, a
journalism product. The number is the lede. We should treat it like one.

## Reading order (the path the eye actually follows)

1. **Dateline + dot.** Calibrates "is this fresh, is this on the right page."
   The dot color *is* the verdict — the reader's peripheral vision already
   tells them today is amber, not green or red.
2. **The lede (one sentence, 40px serif).** *"The long-term-hold tape is
   cooling, not breaking — but the slide is real."* Answers "what is today's
   read" before any number appears.
3. **The standfirst paragraph.** Score `−26`, fifth straight negative session,
   `−8` a month ago, `6 of 167` in Buy. Three numbers, all real. This is
   where the lede gets backed up.
4. **The easy button.** One primary CTA (`See the 135 Watch names`) is the
   action 80% of users want after reading the lede. Two secondary CTAs
   (`Read why`, `New here? Start here`) are quieter.
5. **Trajectory strip.** Four cells, no chart — a month ago `−8`, a week ago
   `−9`, two days ago `−20`, today `−26`. This is "story shape" in 4 numbers.
6. **"What the number means."** Plain prose definition of LTHCS for a
   newcomer. Notes that another `−4` flips the label.
7. **"Why — what's moving the number."** The 9 components, **re-ranked by
   absolute contribution and rewritten as headlines** instead of generic
   rows. *"Band lean is the story (−23 of ±30). Macro is the lone bid (+10).
   Institutions are net selling (−8)."* Each row is a sentence, not a stat.
8. **Leaders / laggards pull-quote.** Two columns: top 5 (FANG, MU, AVGO,
   ADI, NVDA) vs. bottom 5 (NKE, GM, HD, ZS, WBD) with a one-line sector
   gloss. The "so who actually is this about" answer.
9. **Methodology kicker.** Small print at the bottom — the thresholds, the
   caps, a link to `/lthcs/help/`. Curiosity-led, not in-your-face.
10. **Disclaimer.** Smallest type on the page, last thing.

## Copy choices

- **Headline is editorial, not formulaic.** "Cooling, not breaking" is
  honest about a `−26` NEUTRAL score sitting close to the CAUTIOUS line
  without dramatising it. A `+50` day would read something like
  *"…broadening, not just narrow-leadership."*
- **Numbers earn their pixels.** Only 3 numbers appear in the lede
  paragraph (`−26`, `−8`, `6 of 167`). Everything else is in the trajectory
  strip or the component list.
- **Component rows are sentences.** *"Macro is the lone bid"* tells the
  reader the *role* of `+10`, not just the value. The current production
  page shows `"Macro regime / clean / +10 / risk-on backdrop"` — four
  data points, no interpretation.
- **Caps are surfaced where they help.** `−23 of ±30`, `+10 of ±10` — a
  newcomer can tell at a glance that band lean is near its floor while
  macro is pinned to its ceiling, without me having to explain the math.
- **No emoji, no icons-as-language.** The only graphical element is the
  band-color dot in the masthead.

## What I cut

- **The big top-number score block.** The score lives inline in the
  standfirst sentence. It is one of three numbers there.
- **The horizontal gauge.** Replaced by the four-cell trajectory strip,
  which carries *direction* in addition to current position.
- **The band tiles (Elite / High / Constructive / Monitor / Weakening /
  Review) and the Buy/Hold/Watch summary tiles.** They survive only as
  prose: *"135 Watch, 26 Hold, 6 Buy."* Filtering by band still belongs
  on this page, but **below** the verdict, not as the verdict.
- **The index buttons (S&P 100 / DOW 30 / NASDAQ-100).** Defaulting to
  the full universe and pushing index filters out of the lede region.
  A first-time visitor doesn't know they care about S&P 100 vs. NASDAQ-100
  before they know what LTHCS *is*.
- **The apex headline + sub gauge.** The lede + standfirst replace it.
- **The 12-link top navigation.** Implicitly — this mockup shows only the
  hero. The full nav comes back on the real page but doesn't compete with
  the lede for first-look attention.
- **Drift / search / sort controls.** Moved below the fold. The hero's
  job is to answer the question, not to be a control panel.

## Tradeoffs

- **Editorial copy must be regenerated daily.** The lede paragraph
  ("cooling, not breaking", "fifth straight session", "lone bid") is
  inflection-dependent. This needs a small templated narrative layer
  driven off the same nine components — band lean, trajectory delta,
  count of positive components. Cheap to build, but it's net-new code
  vs. the current "render the JSON" approach.
- **Hides the universe filter and the ticker grid below the fold.**
  Power users who land here every morning to scan their watchlist will
  scroll past the verdict every time. Mitigation: a sticky "Skip to
  table" link on scroll, not shown in this mockup.
- **Single-column 760px layout sacrifices density.** Compared to the
  Cockpit angle (C), at-a-glance comparison is worse. We're betting
  that comprehension matters more than density for the first-paint
  use case.
- **Real prior-period numbers are baked in.** The trajectory strip
  references `2026-04-20`, `2026-05-13`, `2026-05-18`. Those need to
  be computed at render time from the existing index/*.json files —
  the mockup hardcodes them but production would resolve "today minus
  30d", "today minus 7d", "today minus 2d" against the latest index.
- **Bands are referenced only as prose ("Watch / Hold / Buy" buckets,
  not Elite/High/.../Review individually).** Closer to a newcomer's
  vocabulary, further from the framework's own. Help page bridges it.

## Why this beats the alternatives for *this* user feedback

The user asked for "a clear path to reading" and "an easy button." Angle B
(guided narrative) answers that by teaching the whole framework; Angle C
(cockpit) answers it by showing more at a glance. Both add to the page.
Revamp A answers it by **deleting** until the only thing left to read is
the answer. The verdict is the path. There's nothing else to flow through.
