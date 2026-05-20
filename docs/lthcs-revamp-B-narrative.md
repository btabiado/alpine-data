# LTHCS Revamp B — Guided Narrative / Self-Teaching

**Mockup:** `lthcs_tab/mockups/revamp-B-narrative/index.html`
**Angle:** Every section teaches itself. Reading order is the design. Jargon is glossed inline, never hidden behind a separate help page.
**Audience:** The user who has never seen this product and lands on the page from a link.

## Teaching philosophy

The user feedback was unambiguous: *"I need a clear path to reading — easy button. Not clear to new users what message we are providing and how to flow the index."* Revamp B answers this literally — there is a **single clear button at the top** (the "Start the 30-second tour" CTA) and a **single linear path** through four numbered sections. No tabs. No nav strip. No 10-button band grid above the fold. No competing CTAs.

The model: Stripe docs, Notion onboarding, Duolingo's friendliness. Confident enough to define every term *in place*, modest enough to assume the reader knows nothing. The page should be readable cold, top-to-bottom, the first time the user sees it — and on day 30, the user should be able to skim the same page in 15 seconds because the structure is so familiar.

## Reading order (the entire design)

| # | Section | Question it answers | Cognitive load |
|---|---|---|---|
| 0 | Welcome banner | "Where am I?" | One sentence, dismissible. |
| 1 | The big picture | "Where is the market leaning right now?" | One number + a sentence-long gloss. |
| 2 | What changed inside the number | "Which signals pushed it up or down?" | 9 components, each with a one-line plain-English gloss + a jargon popover. |
| 3 | Why today's read matters | "What story do these numbers tell?" | Auto-generated narrative paragraph naming the largest mover, the largest pusher-back, and any macro-vs-breadth divergence. |
| 4 | How to read this dashboard | "A cheat sheet I can come back to." | Five short rules + an explicit "what this is NOT" box. |

Each section ends with a "Next: 2. What changed →" link to the next-numbered step. The user is **never lost** — they always know where they are (numbered circle in the heading) and where they're going next (footer link).

## Copy choices (citing `lthcs_help/index.html`)

The Guided Narrative angle's secret weapon is that the help page already contains beautiful, plain-English copy. Revamp B doesn't reinvent — it **lifts and tightens**.

| Where copy was reused | Source in `lthcs_help/index.html` |
|---|---|
| Verdict label glosses (NEUTRAL = "balanced or noisy — no clear directional bias") | "What the labels mean" list, lines 102–109. Adapted into one-sentence narrative form. |
| Per-component plain-English glosses (band lean, Adoption pillar, Institutional pillar, etc.) | "Row by row" list, lines 130–140. Each row became one component card's `.gloss` line. |
| Inline `<details>` popover definitions (band lean, 13F, Form 4, Adoption, Institutional, Financial, Thesis, DES, Macro regime) | Mix of "Row by row" and "The 5 pillars" sections. Tightened from paragraph form to 1–2 sentences each so they fit a popover. |
| "How to read" cheat sheet (Big swings matter / Bands &gt; absolute number / Disagreement is interesting / 13F lags / as-of caveat) | Composed from the `<dl class="lhlp-qa">` "What it means / When to care / Gotcha" trios on lines 110–117, 141–148, 259–266, and 284–291. |
| "What this is NOT" closing box | Lifted from the intro section, lines 86–89 ("Everything here is a directional read… Do your own work before risking real money."). |

The math accuracy was cross-checked against `lthcs/index_aggregate.py`:
- The `±30` cap for band lean vs. `±10` for everything else (`_BAND_LEAN_CAP = 30`, line 57) is explained inline in step 2.
- The verdict thresholds in `_label_for()` (lines 63–73) are reflected in the band-legend strip in step 1.

## Jargon level

Two-tier:

1. **Outer text** uses zero unexplained jargon. "Band lean" never appears without "of every 168 names we track, what share is in the top 3 bands vs. the bottom 2." "13F" is glossed as "across the universe, are institutions accumulating or distributing? Lags one quarter."
2. **Inline popovers** (pure `<details>` + `<summary>`, no JS framework, no `<dialog>`) provide the deeper definition on demand. The popover for "13F" tells you *what* the filing is and *why* it lags. Closing happens via click-outside (one event listener, ~5 lines).

This keeps the page short enough that a new user reads top-to-bottom, while letting the curious user dig one layer deeper without leaving the page.

## Design tokens reused

All colors come from `lthcs_tab/lthcs.css`:
- `--band-elite-bright #4D7AB5`
- `--band-high-bright #6FD18C`
- `--band-constructive-bright #E9C04A`
- `--band-monitor-bright #F0A861` (today's tone — driven by `band_key: "monitor"`)
- `--band-weakening-bright #E27A5C`
- `--band-review-bright #C25640`
- Background / text / border tokens (`--bg-page`, `--bg-card`, `--text-primary`, etc.)

A single `--tone` CSS custom property is set on `:root` from the payload's `band_key`. It paints the score color, the numbered-circle backgrounds, the why-paragraph's left border, and the band-legend highlight — so a future BULLISH (green) or BEARISH (red) day repaints automatically without code changes.

## Tradeoffs

| Win | Cost |
|---|---|
| New users land oriented in &lt;30 seconds. | Vertical scroll is longer than the prod page. |
| Every term is glossed inline — no round-trip to /help/. | More words on screen than Angle A (verdict-first) or Angle C (cockpit). |
| Reading order is enforced, so the message is the same for everyone. | Power users may want to jump straight to the data; the "I've seen this" banner-dismiss + skip-to-content anchor are the escape hatches. |
| Pure HTML `<details>` popovers — no JS framework, CSP-safe (one inline `<script>` we'd extract for prod), works without JS for the static parts. | Popover positioning is left-anchored; on very narrow phones it can overflow if a term lives near the right edge. Mitigated with `width: min(280px, 90vw)`. |
| Why-paragraph is generated from real component deltas — it adapts to whatever today's data says (big drag + biggest pusher + macro-vs-breadth divergence). | One more place to maintain when the component set changes. Acceptable: pillar list is stable. |
| Verbose copy compared to A and C. | Intentional. The brief specifically says **lean hard into the angle, do not hedge.** This page is for the user who would otherwise bounce — and bouncing has infinite cost. |

## Where the message lives

If you read nothing else, **step 3 ("Why today's read matters")** is the message. It auto-generates a paragraph that says, for 2026-05-20: *"The headline is &minus;26 — NEUTRAL. The biggest drag is **Band lean** at &minus;23 (universe distributing). Pulling the other direction: **Macro regime** at +10 (risk-on backdrop). **Watch the divergence:** macro is risk-on but breadth is distributing — the signal is idiosyncratic, not market-wide. Drill into individual tickers."* That is the whole message of the index, written in plain English, every day, automatically.
