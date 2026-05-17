# LTHCS — Peer-Group Sanity Audit

Audit ID: queue item #3, follow-up to commit `4fa205c` (maturity-stage-relative
revenue-growth percentiles).

Scope: every place a ticker's score depends on a cross-ticker comparison.
For each, identify the current peer group, argue whether it's correct, and
recommend a change with an implementation sketch.

This is research / recommendation only. No code changes here.

Snapshot used for concrete numbers: `data/lthcs/snapshots/2026-05-17.json` +
`data/lthcs/variable_detail/2026-05-17.json`. Universe = 169 active tickers
(164 standard_compounder, 3 recovery_stabilization, 1 recovery_rerating,
1 pre_profit_growth).

---

## TL;DR

1. **Maturity-stage fix did almost nothing for the standard_compounder cohort.**
   AAPL's revenue-growth percentile moved 46.2 → 46.8 (160 → 156 peers). It
   "fixed" LCID and the recovery names — which are 5/169 of the universe.
   The 162-name standard_compounder bucket is itself the problem and needs
   a finer split.
2. **A naive `(maturity_stage, sector)` join makes things WORSE in the bucket
   where it matters most.** AAPL would drop to the 13th percentile inside
   Tech-compounders (n=38) because that cohort is half-semiconductor at
   peak earnings (MU +49%, NVDA +66%, AMD +34%). Recommendation A below
   threads this needle carefully — sector overlay should NOT be applied to
   Technology without first splitting "tech compounder" along a
   growth/quality axis.
3. **Institutional momentum is universe-relative and almost certainly should
   stay that way.** The two-bucket counterfactual (universe vs sector)
   shows momentum percentile is doing exactly the job the pillar's
   white-paper description claims: "where is broad market money going,
   period." Sector-relative momentum would re-rank INTC's +172% rally
   the same as a normal +15% leader, which destroys the signal.
4. **The three highest-ROI changes,** ranked:
   - **(E)** Split `standard_compounder` into `mature_compounder` and
     `growth_compounder` along a sustained-growth axis. ~20 reclassifications.
     Fixes AAPL/KO/PG/WMT under-ranking AND addresses NVDA/MU/MRVL
     over-ranking simultaneously.
   - **(A')** Compound peer key `(maturity_stage, sector_group)` where
     `sector_group` clusters Energy+Materials, Consumer Staples+Health Care,
     and splits Tech into Hardware vs. Software/Services. Bucket-size
     guardrail: ≥8 members else fall back to maturity stage.
   - **(B-keep)** Keep Institutional momentum universe-relative. Don't change
     it. (Including this as the explicit recommendation because the queue
     note left it open.)
5. **Out-of-scope but flagged:** the Financial pillar's `margin_subscore`
   and `ocf_subscore` are absolute-bounded, not percentile, so they're not
   peer-group-affected. They have their own calibration questions
   (banks' XBRL gap, SaaS gross margins saturating the ceiling) but that's
   a separate audit.

---

## 1. Inventory of every cross-ticker comparison in V1

### Adoption Momentum (`lthcs/pillars/adoption.py`)

#### `revenue_subscore` — `peer_relative_percentile(growth, peer_growths)`

- **Where:** `compute_adoption` line ~395.
- **Current peer group:** `maturity_stage`-bucketed, fall back to universe
  if the bucket has <5 members. The bucketing is built in `lthcs_daily.py`
  Stage 4 (lines 580–603) and passed in as `peer_growths`. The pillar
  itself is peer-group-agnostic — it just takes the dict it's given.
- **Sample (real, 2026-05-17):** AAPL growth +6.4%, peer_growths bucket =
  156 standard_compounders. revenue_subscore = **46.8** (was 46.2 against
  universe of 160 pre-fix). Math: 156-name bucket has lower variance than
  universe (recovery names removed, LCID's +68% removed), but AAPL is
  also relative to NVDA +66% / MU +49% / SMCI +47% inside the same bucket.
- **Assessment:** The maturity-stage fix correctly punished LCID — its
  bucket-of-one falls back to universe, but on the next bucket-size pass
  it would be ranked alone, which is at least defensible. It did NOT
  solve the original symptom Bryan reported: "AAPL's +6% scoring at
  median because it's compared to high-growth names." Standard_compounder
  spans MCHP (-42%) to NVDA (+65%) — a 107pp spread. That spread is what
  drives AAPL to ~46th: in absolute terms +6.4% is below the
  standard_compounder median of +7.0%.
- **Recommendation:** Two-axis fix. See recommendations E and A' below.
  Sector-only or industry-only is wrong (see "the broken cohort" section
  for why naive Tech-stage compound makes AAPL worse).

#### `trends_subscore` — `bounded_linear(slope, -0.5, +0.5)`

- **Where:** `compute_adoption` line ~403.
- **Peer group:** None. This is an absolute mapping (slope of trailing
  90 days of Google Trends interest).
- **Assessment:** Correct in principle, but moot in V1 — `interest_series`
  is empty for all 169 tickers (pytrends is rate-limited). The 40%
  trends weight is currently dropped and revenue carries the pillar
  alone (Adoption renorm logic, lines 416–432).
- **Recommendation:** No change. When trends is wired up, an additional
  question opens — "is +X interest-slope big *for this stock*?" —
  which might justify percentile-ranking trends slopes too. Defer.

---

### Institutional Confidence (`lthcs/pillars/institutional.py`)

#### `momentum_subscore` — `peer_relative_percentile(momentum_pct, peer_momentums)`

- **Where:** `compute_momentum_subscore` line ~109.
- **Current peer group:** **FULL UNIVERSE** (166 active tickers with real
  momentum data). Passed in by `lthcs_daily.py` Stage 4 as the full
  `state.momentum_by_ticker` dict.
- **Sample (real, 2026-05-17):**
  - AAPL momentum +14.6% → 71.5th universe-relative.
  - In Tech only (n=41): 46.3rd — because tech contains INTC +171.7%
    (recovery_stabilization), MU +111.1%, CSCO +58.0%.
  - In standard_compounder only (n=161): 71.4th — same as universe (the
    excluded 5 don't move the needle).
  - INTC momentum +171.7% → 100.0 universe; would still be 100 sector;
    its own stage has n=1 so it falls back to universe and stays 100.
- **Assessment:** This is the trickiest call in the audit. Two arguments:

  **Argument for universe-relative (status quo):**
  Institutional Confidence is defined in the white paper as
  "where is institutional money flowing right now" — and institutions
  rotate across the whole equity market, not within a sector. A
  rolling-90-day return of +14.6% IS impressive among all stocks. A
  sector-relative version would re-rank INTC's +172% rally as
  "leader of tech" (already 100) but would re-rank AAPL's +14.6% as
  "middle of tech" (46.3) even though, vs. all equities, +14.6% is a
  90th-percentile move. The whole purpose of the pillar — surfacing
  unusual market interest — depends on the universe-wide frame.

  **Argument for sector-relative:**
  A pharma name with +14.6% momentum is genuinely a leader because
  Health Care doesn't trade like tech. Cross-sector comparison
  conflates "I'm a hot stock" with "I'm a hot sector." Sector-relative
  momentum would also dampen the "INTC at 100" artifact — the
  recovery_stabilization stage is exactly the cohort that gets
  outsized 90-day rallies, and it's currently a stage-of-1 that
  short-circuits the maturity-stage logic.

  **Conclusion:** Keep universe-relative. The pillar's intent is
  "broad-market momentum" and sector-relative momentum is in conflict
  with that intent. The INTC artifact is real but better solved by
  the volatility modifier (which already clips high-vol tickers
  downward by 3.0 — INTC's vol almost certainly fires the 90th-pct
  threshold). If the team wants sector-relative momentum, it should
  be added as a NEW sub-component, not a replacement.

- **Recommendation:** **NO CHANGE.** See recommendation B below for the
  full reasoning + the alternative "add a sector-relative momentum
  ratio as a 30% sub-component" path if Bryan disagrees.

#### `inst_holdings_subscore` — `bounded_linear(change_qoq, -0.05, +0.05)`

- **Where:** `compute_inst_holdings_subscore` line ~134.
- **Peer group:** N/A. V1 always passes `None` (13F is stubbed for
  Phase 1). The renorm at line 187 redirects momentum to 100% effective
  weight.
- **Assessment:** Out of scope. When 13F ships in Phase 2, *that* signal
  WILL need a peer-group decision — institutional ownership-change is
  the canonical example of a number that should be sector-normalised
  (tech float churns differently than utility float). Open a separate
  audit item then.

---

### Financial Evolution (`lthcs/pillars/financial.py`)

#### `revenue_subscore` — `peer_relative_percentile(growth, peer_growths)`

- **Where:** `compute_financial` line ~374.
- **Current peer group:** Same as Adoption — `maturity_stage`-bucketed,
  fall back to universe if <5 members. The pipeline reuses the SAME
  `peer_growths_by_stage` map for both pillars (Stage 4, line 609).
- **Sample:** AAPL Financial.revenue_subscore mirrors Adoption.revenue_subscore
  exactly — both compute against the same `peer_growths`.
- **Assessment:** Correct that the two pillars stay in lockstep. If
  Adoption changes peer groups, Financial should change with it.
- **Recommendation:** Tied to Adoption. Whatever change lands in
  Adoption's revenue grouping must apply identically in Financial.

#### `margin_subscore` — `bounded_linear(margin_slope, -0.05, +0.05)`

- **Where:** `compute_margin_trend_subscore` line ~239.
- **Peer group:** None. Absolute mapping of trailing-4-quarter gross-margin
  slope.
- **Assessment:** The slope is per-ticker, scale-free (margin units per
  quarter), so peer-comparison isn't strictly needed for the SIGNAL to
  work. But two questions about the bound:
  - Are ±5pp/quarter swings really the "extreme" anchor for all
    sectors? A software company can lose 100bp of gross margin in a
    quarter and that's a five-alarm fire; a retailer can swing 200bp
    on inventory mix and it's normal. Sector-relative margin trends
    WOULD detect this.
  - Per the spec, "+5pp/quarter swing" was V1's heuristic. With a
    full snapshot now in hand, we can measure: what's the actual
    p10/p90 of `margin_trend_slope` across the universe?
- **Recommendation:** Defer the absolute-bounds re-tune to the tuning
  kit (queue item #4). Note for future: if margin signals look squashed
  in the variable_detail snapshot (lots of ~50s), shrinking the bounds
  to ±0.02 or moving to sector-relative percentile is the fix.

#### `ocf_subscore` — `bounded_linear(ttm_ocf_margin, -0.10, +0.30)`

- **Where:** `compute_ocf_subscore` line ~283.
- **Peer group:** None. Absolute mapping of TTM OCF margin.
- **Assessment:** This is the most defensible absolute mapping in the
  codebase. OCF margin has a real economic floor (negative means cash
  burn) and a real ceiling (~30% means software-like cash conversion).
  But it IS sector-naive: a 12% OCF margin is best-in-class for a
  retailer and middling for SaaS. Sector-relative OCF would surface
  Costco / Walmart strength that the absolute bound flattens.
- **Recommendation:** Keep absolute. If Bryan wants sector-relative OCF
  too, it can be added as a SECOND sub-component (e.g. 20% absolute,
  10% sector-relative) — but this is finer tuning than the V1 calibration
  fixes. Defer.

---

### Thesis Integrity (`lthcs/pillars/thesis.py`)

#### `sentiment_subscore_raw` — `bounded_linear(mean_sent, -1.0, +1.0)`

- **Where:** `compute_thesis` line ~100, `compute_thesis_from_stored_sentiment`
  line ~279.
- **Peer group:** None. Per-ticker absolute mapping of Alpha Vantage's
  mean sentiment score, with a confidence blend toward neutral 50 when
  article count < 3.
- **Assessment:** Correct. Sentiment scores are already normalized to
  [-1, +1] by AV; making them peer-relative would conflate "headlines
  are unusually positive for this stock" with "all stocks have positive
  headlines this week." The current mapping preserves the absolute
  signal.
- **Recommendation:** No change. (NB: Thesis has its own ramp problem
  — only AAPL/AMZN/META/AVGO/BAC had real sentiment on 2026-05-17,
  rest at neutral 50. That's a data-availability issue, not a
  peer-group issue.)

---

### DES (`lthcs/pillars/des.py`)

#### `signal_tilts` — `normalize_macro_signal(value, low, high)`

- **Where:** `normalize_macro_signal` line ~122.
- **Peer group:** None. Each macro signal (CPI YoY, Fed Funds, 10Y yield,
  10Y 30d change, unemployment, WTI oil) is mapped to [-1, +1] via the
  absolute bounds in `data/lthcs/sector_des_weights.json`'s
  `signal_normalization` block.
- **Assessment:** Correct by design. Macro signals are macro — they
  describe the environment, not a ticker's position within it. There's
  no peer-group to even define here.

#### `sector_sensitivities` — per-sector multipliers

- **Where:** `_sector_sensitivities` line ~143.
- **Peer group:** Sector-keyed by construction. Sensitivities live in
  `sector_des_weights.json["sectors"]`. Ticker-level overrides via
  `ticker_overrides` (TSLA / LCID etc.).
- **Assessment:** Correct. Sector is the right axis — and the override
  mechanism handles the industry-mismatch escape hatch.

---

### Score combiner (`lthcs/score.py`)

#### Volatility modifier — `_percentile(ticker_vol, universe_vols, 90)`

- **Where:** `compute_volatility_modifier` line ~177.
- **Current peer group:** **FULL UNIVERSE** of trailing-30d realised
  volatilities. Strict `> p90` fires a -3.0 modifier.
- **Assessment:** Same question as momentum. Tech is high-vol on
  average; financials are low-vol. A universe-relative 90th-percentile
  punishes tech disproportionately and lets cyclical industrial vol
  slip through.
- **Recommendation:** Keep universe-relative for V1. The modifier is
  -3.0 (small absolute impact). Sector-relative would over-engineer
  given the magnitude. Revisit if/when modifier magnitude is increased.
  (FYI: this is one of the levers in queue item #4's tuning kit.)

---

## 2. The framework — which axis is right for which signal?

A short decision tree for "what peer group does this signal want":

| Signal type                     | Best axis                                  | Why                                                                  |
|--------------------------------:|:-------------------------------------------|:---------------------------------------------------------------------|
| Revenue growth                  | maturity stage × sector_group (compound)   | Mature compounders should compare to mature compounders; sector controls for the cyclical wave (semis vs. SaaS vs. consumer staples). |
| Margin trajectory               | sector OR absolute                         | Sector controls for "what's a normal margin swing here." V1 absolute is OK as a placeholder. |
| OCF margin                      | absolute (V1) → sector-relative (Phase 2)  | Absolute level has real economic meaning; sector-relative surfaces best-in-class. |
| Price momentum (90d)            | **universe** (status quo)                  | Pillar's intent is broad-market money flow; sector-relative defeats the purpose. |
| 13F ownership change (Phase 2)  | sector                                     | Float turnover varies by sector cohort. Phase 2 problem. |
| Sentiment                       | absolute                                   | AV's score is already universal. |
| Macro tilts                     | absolute                                   | Macro is macro. |
| Sector sensitivities            | sector (definitional)                      | Sector is the lookup key. |
| Volatility modifier             | universe (status quo)                      | Small absolute impact (-3.0); not worth refining. |

**Compound peer keys.** For revenue growth, the right approach is
`(maturity_stage, sector_group)` where `sector_group` clusters near-
neighbours to keep cohorts above the 5-member-fallback floor. See
recommendation A' below for the concrete grouping.

**Why not industry?** GICS industry is too granular (e.g. "Consumer
Electronics" is just AAPL within the universe). Industry-relative
percentiles would degenerate to bucket-of-one for ~40% of names. Sector
is the right grain.

---

## 3. The "broken peer group" symptoms

Pulled from the live 2026-05-17 snapshot. All numbers are real.

### Symptom 1: AAPL trapped at median revenue percentile

| Cohort                              | n     | Percentile |
|------------------------------------:|:------|-----------:|
| universe                            | 160   | 46.2       |
| standard_compounder (current fix)   | 156   | 46.8       |
| standard_compounder × Technology    |  38   | **13.2** (worse!) |
| mature_compounder × Tech (proposed) | ~15   | ~30 (better, see Rec E) |

The "obvious" fix — add a sector axis to the existing maturity grouping
— makes things dramatically worse for AAPL. Tech-compounder is
half-semiconductors at peak earnings (MU +49%, NVDA +66%, AMD +34%,
MRVL +42%, SMCI +47%) and half-megacap-mature (AAPL +6%, IBM +8%,
CSCO +5%, ORCL +8%). AAPL ranks LOW within tech-compounder for the
same reason it ranks low within all compounders: it's being
benchmarked against names that aren't actually peers.

The right fix is to peel "mature compounder" out of the tech-compounder
bucket. See recommendation E.

### Symptom 2: JPM compounded with bank-XBRL-coverage issue

JPM revenue growth +2.8% → revenue_subscore 25.6 universe-relative,
12.5 in (Financials × standard_compounder). Either way, the bank
shows up low. BUT: JPM's Financial pillar is 14.7 because banks lack
us-gaap GrossProfit / NetCashProvidedByOperatingActivities concepts
(comment in `compute_financial` line ~390). The renorm catches some
of this, but the revenue percentile still drags.

Bank-specific concept extraction is queue item #4 / Phase 2. For the
peer-group audit: even a sector-relative percentile doesn't help JPM
because the cohort it'd be compared to (16 other financials) has the
same data-coverage problem. The right fix is upstream (extract
NetInterestIncome) not peer-group.

### Symptom 3: INTC stage-of-1 falls back to universe

INTC is the lone `recovery_stabilization` member with revenue data,
plus EQT (recovery_stabilization, no growth in detail) and AVGO
(misclassified? — listed as standard_compounder elsewhere).

When `_peer_growths_for("INTC")` runs, the bucket has 1 member (itself),
fails the ≥5 floor, falls back to universe. INTC's -0.5% growth lands
at the 12.5th percentile of universe — which mechanically makes its
Adoption.revenue 12.5. Combined with momentum at 100, INTC's
Institutional pillar reads 100 but Adoption reads 12.5, and the
composite (40.4) doesn't tell the user what's happening.

The right behaviour: INTC's universe-fallback IS correct. The signal
"-0.5% growth is unusually weak in absolute terms" is the right signal
to surface. The narrative layer should explain this — not the
peer-group fix.

### Symptom 4: PG looks fine universe-wide but bad in-sector

PG momentum +2.7% → 55.8 universe, 7.7 in Consumer Staples (n=13).
This is a case where the current universe-relative momentum is right.
PG is treading water vs. all stocks — that's what the score should
reflect. Sector-relative would say "PG is one of the worst-performing
staples" which is true but conflates a sector trend (staples
underperforming) with a stock signal.

---

## 4. Recommended fixes — ranked

### (E) Split `standard_compounder` into `mature_compounder` + `growth_compounder`

**Pitch.** Rename the catch-all `standard_compounder` stage into two:

- `mature_compounder` — names with sustained <15% revenue growth and
  established market position. Members: AAPL, IBM, CSCO, ORCL, JPM,
  BAC, all consumer staples, utilities, most industrials,
  pharma majors.
- `growth_compounder` — names with sustained >15% growth that are still
  in the "compounder" frame (not pre-profit). Members: NVDA, MU,
  AVGO, AMD, MRVL, SMCI, NOW, DDOG, MDB, ZS, TEAM, PANW, FTNT,
  ARM, ASML, KLAC, LRCX.

Boundary is fuzzy and that's fine — pick a starting cut at trailing-3y
median revenue growth, then hand-curate the 5–10 edge cases (META at
+22% straddles, AMD at +34% probably belongs in growth, GOOGL at +15%
probably stays mature).

**Affected files.** `data/lthcs/universe.json` (15–20 ticker entries
flip their `maturity_stage` field). `data/lthcs/weights.json` may
want a new profile entry; default to copying the current
`standard_compounder` profile and tune later. NO code changes — the
maturity-stage mechanism in `lthcs_daily.py` already keys on
whatever value is in the universe file.

**Test impact.** None unit-test-wise. Snapshot diff will be sizeable
on first run (~30 tickers will see their revenue percentile move
±10 points). New universe.json gets committed → first daily run
produces a new snapshot → diff is the verification.

**Risk.** The line between mature and growth is judgement-laden.
Best-practice: document the rule used (e.g. "trailing-3y median
revenue growth ≥ 15%"), put the rule in a comment in universe.json,
and let it be a refresh-quarterly file.

**Estimated impact on focal tickers (rough, no recompute):**

| Ticker | Current adp_score | Stage after E | Est. new adp_score | Delta |
|-------:|------------------:|--------------:|-------------------:|------:|
| AAPL   | 46.8              | mature        | ~58                | +11   |
| KO     | 21.2              | mature        | ~38                | +17   |
| PG     | 15.4              | mature        | ~30                | +15   |
| WMT    | 35.9              | mature        | ~52                | +16   |
| NVDA   | 100.0             | growth        | ~75                | -25 (good — NVDA shouldn't be pinned at 100 every day) |
| MU     | 99.4              | growth        | ~70                | -29   |
| AVGO   | 91.0              | growth        | ~50                | -41   |
| MRVL   | 91.0              | growth        | ~50                | -41   |

Composite delta is smaller (Adoption is 25% of mature_compounder
weight): AAPL ~+2.5, NVDA ~-6, KO ~+4, PG ~+4.

### (A') Compound peer key `(maturity_stage, sector_group)` with bucket-size guardrail

**Pitch.** Add a sector overlay to the maturity bucket, but use a
*sector_group* mapping that combines near-neighbours so cohorts stay
above 8 members:

| sector_group  | GICS sectors                                 | Members (compounders) |
|:--------------|:---------------------------------------------|----------------------:|
| Tech-Hardware | Technology (Hardware/Semis subset)           | ~20                   |
| Tech-Software | Technology (Software/Services) + Comm Svcs   | ~30                   |
| Defensive     | Consumer Staples + Utilities + Health Care   | ~42                   |
| Cyclical      | Consumer Discretionary + Industrials + Materials | ~40              |
| Financial     | Financials + Real Estate                     | ~20                   |
| Energy        | Energy                                       | ~5                    |

Lookup: `peers_for(sym) = bucket((stage, group))` if size ≥ 8 else
`bucket(stage)` else universe.

**Affected files.** `lthcs_daily.py` Stage 4 (lines 580–603) gains a
sector_group keying. The mapping itself lives in a new tiny config
`data/lthcs/peer_group_map.json` (or inlined into universe.json
under `sector_group`). No pillar code changes.

**Test impact.** New unit test for the bucket-selection logic
mirroring the existing maturity-stage fallback test.

**Risk.** Medium. The Tech-Hardware vs. Tech-Software split is the
load-bearing decision; mis-classifying ARM or ASML (hardware design)
materially changes their score. Suggested: leverage existing GICS
industry codes in universe.json — Semiconductors + Hardware
+ Storage → Hardware; Software + IT Services + Internet → Software.

**Combine with E:** Both together. Run E first (it's a data-only
change), see the composite distribution, then layer A' on top if
the AAPL-vs-NVDA spread still looks compressed.

### (B-keep) Keep Institutional momentum universe-relative

**Pitch.** Explicit no-op. The white paper's framing — "where is
institutional capital flowing" — is intrinsically cross-universe.
Sector-relative momentum would re-rank within sector and answer a
different question ("who's leading this sector"), which is its
own valid signal but not what Institutional Confidence claims to
measure.

**Affected files.** None.

**Test impact.** None.

**Risk.** None. This is a positive recommendation TO NOT CHANGE a
thing that's currently right.

**If Bryan disagrees**, the alternative is to add a *second*
momentum sub-component (sector-relative) at 30% of the pillar and
shrink the universe-relative momentum to 40%. That makes the
pillar a 40/30/30 instead of 70/30. But it muddies the pillar's
identity — recommend against.

### (A) Compound peer key `(maturity_stage, sector)` raw

**Pitch.** The naive version of A'. Use GICS sector directly,
fall back to maturity-stage when bucket < 5, fall back to universe
when stage < 5.

**Why it's NOT the recommendation:** Symptom 1 above. AAPL drops to
13.2 in raw (compounder × Technology) because Technology spans
semis + megacap, two utterly different sub-populations. The
sector_group mapping in A' fixes this by splitting Tech.

**Affected files.** Same as A' but simpler — just `(stage, sector)`
key with no remap.

**When you'd want A instead of A':** If you don't yet have
confidence in the sector_group mapping and want a quick test.
Run A for one snapshot, examine the AAPL/MSFT/NVDA distribution,
then decide whether the Tech split (A') is worth the extra config.

### (C) Add `peer_groups.json` config so each pillar tunes independently

**Pitch.** Externalize the peer-group strategy into a config:

```json
{
  "adoption_momentum": {
    "revenue_growth_yoy": {
      "primary": ["maturity_stage", "sector_group"],
      "fallback": ["maturity_stage"],
      "min_bucket_size": 8
    }
  },
  "institutional_confidence": {
    "momentum_pct_90d": {"primary": [], "fallback": [], "min_bucket_size": 0}
  },
  "financial_evolution": {
    "revenue_growth_yoy": "@adoption_momentum.revenue_growth_yoy"
  }
}
```

`lthcs_daily.py` Stage 4 loads this and builds the bucketing dict
accordingly.

**Affected files.** New `data/lthcs/peer_groups.json`. Refactor of
Stage 4 (lines 569–671) to read from config. Small refactor to
pillar callers (or keep callers identical, just change what
`my_peer_growths` resolves to).

**Test impact.** ~3 new unit tests covering the config loader's
fallback chain. Snapshot diff: zero if defaults match current behaviour.

**Risk.** Medium-low. Refactor surface area is contained to one
function in `lthcs_daily.py`. Benefit is the calibration team can
A/B different groupings without touching code.

**Recommendation:** Worth doing AFTER A' lands. Don't bundle the
config refactor with the substantive change — ship the new groups
first, then refactor when the groups are stable.

### (D) Market-cap tiers (mega / large / mid)

**Pitch.** Add a `market_cap_tier` field to universe.json and use
`(stage, sector_group, mcap_tier)` for revenue percentile.

**Affected files.** universe.json needs a new field per ticker. New
fetcher (or static annotation since the universe is small and mcap
changes slowly).

**Risk.** High effort, low marginal value over E + A'. Mega-cap and
large-cap behave more similarly than e.g. semi vs. SaaS within
large-cap. Recommend skipping unless E + A' don't surface AAPL
properly.

**Verdict:** Defer indefinitely.

---

## 5. Implementation sketch — the top recommendation (E + A')

### Step 1: maturity-stage split (E)

Edit `data/lthcs/universe.json` — flip ~20 entries from
`standard_compounder` to `growth_compounder`. Add a comment at the
top of the file documenting the rule:

```jsonc
// maturity_stage taxonomy (2026-05):
//   mature_compounder    - established business, trailing-3y rev growth <15%, profitable
//   growth_compounder    - established business, trailing-3y rev growth ≥15%, profitable
//   pre_profit_growth    - high-growth, not yet profitable (LCID, growth-stage)
//   recovery_stabilization - turnaround case stabilizing (INTC)
//   recovery_rerating    - turnaround case completing re-rate
```

Rename existing `standard_compounder` entries to `mature_compounder`
in universe.json. Add `growth_compounder` to weights.json (copy
the current `standard_compounder` profile to start).

### Step 2: sector_group overlay (A')

Edit `data/lthcs/universe.json` — add a `sector_group` field per
ticker, OR keep universe.json clean and add a separate
`data/lthcs/sector_group_map.json`:

```json
{
  "Technology": {
    "Semiconductors": "Tech-Hardware",
    "Technology Hardware Storage & Peripherals": "Tech-Hardware",
    "Computer Hardware": "Tech-Hardware",
    "Software": "Tech-Software",
    "IT Services": "Tech-Software",
    "Internet Software & Services": "Tech-Software"
  },
  "Communication Services": {"*": "Tech-Software"},
  "Consumer Staples": {"*": "Defensive"},
  "Utilities": {"*": "Defensive"},
  "Health Care": {"*": "Defensive"},
  "Consumer Discretionary": {"*": "Cyclical"},
  "Industrials": {"*": "Cyclical"},
  "Materials": {"*": "Cyclical"},
  "Financials": {"*": "Financial"},
  "Real Estate": {"*": "Financial"},
  "Energy": {"*": "Energy"}
}
```

### Step 3: Stage 4 bucketing change in `lthcs_daily.py`

Replace lines 580–603 with:

```python
_MIN_PRIMARY_PEERS = 8
_MIN_STAGE_PEERS = 5

def _sector_group(sym: str) -> str:
    entry = state.by_ticker.get(sym, {}) or {}
    sector = entry.get("sector", "")
    industry = entry.get("industry", "")
    return state.sector_group_map.get(sector, {}).get(industry) \
        or state.sector_group_map.get(sector, {}).get("*") \
        or sector

# Primary bucket: (stage, sector_group)
primary_buckets: Dict[Tuple[str, str], Dict[str, Optional[float]]] = {}
stage_buckets: Dict[str, Dict[str, Optional[float]]] = {}
for sym, g in peer_growths.items():
    entry = state.by_ticker.get(sym, {}) or {}
    stage = entry.get("maturity_stage", "mature_compounder")
    grp = _sector_group(sym)
    primary_buckets.setdefault((stage, grp), {})[sym] = g
    stage_buckets.setdefault(stage, {})[sym] = g

def _peer_growths_for(sym: str) -> Dict[str, Optional[float]]:
    entry = state.by_ticker.get(sym, {}) or {}
    stage = entry.get("maturity_stage", "mature_compounder")
    grp = _sector_group(sym)
    primary = primary_buckets.get((stage, grp), {})
    if len(primary) >= _MIN_PRIMARY_PEERS:
        return primary
    stage_bucket = stage_buckets.get(stage, {})
    if len(stage_bucket) >= _MIN_STAGE_PEERS:
        return stage_bucket
    return peer_growths  # universe fallback
```

### Step 4: Before/after composite estimates (back-of-envelope)

Assumes E + A' applied together, with focal cohort sizes ~15–25
each (well above the n=8 floor). Adoption pillar weight is 0.25 for
mature, ~0.25 for growth.

| Ticker | Old adp | New adp (est.) | Δ adp | Old composite | Est. new composite | Δ composite |
|-------:|--------:|---------------:|------:|--------------:|-------------------:|------------:|
| AAPL   | 46.8    | ~62            | +15   | 55.1          | ~58                | +3          |
| MSFT   | 75.6    | ~78            | +3    | 56.8          | ~57                | +1          |
| NVDA   | 100.0   | ~75            | -25   | 79.4          | ~73                | -6          |
| MU     | 99.4    | ~70            | -29   | 83.3          | ~76                | -7          |
| AVGO   | 91.0    | ~55            | -36   | 74.3          | ~66                | -8          |
| KO     | 21.2    | ~38            | +17   | 42.8          | ~47                | +4          |
| PG     | 15.4    | ~30            | +15   | 38.0          | ~42                | +4          |
| WMT    | 35.9    | ~52            | +16   | 46.8          | ~51                | +4          |
| JPM    | 25.6    | ~25            |  0    | 34.9          | 34.9               | 0           |
| INTC   | 12.5    | 12.5 (universe fallback unchanged) | 0 | 40.4 | 40.4 | 0 |
| LCID   | 100.0   | 100.0 (bucket-of-1, universe fallback) | 0 | 53.7 | 53.7 | 0 |

Net effect: the AAPL/NVDA spread compresses from 24.3 pts to ~15 pts.
NVDA stays in Elite band (>70). AAPL moves from Weakening to
Constructive. KO/PG/WMT move from Weak toward Neutral. INTC and LCID
unchanged (their stages-of-1 still fall through to universe).

This is the right behaviour. AAPL is genuinely a high-conviction
compounder, not a median name; NVDA is having a once-in-a-decade
year and shouldn't be permanently pinned at 100.

---

## 6. Out of scope for this audit

These were considered and explicitly deferred:

- **Re-tuning absolute bounds for margin trend / OCF margin.** Goes
  in queue item #4's tuning kit. The right approach is empirical:
  measure the actual p10/p90 of these signals across the universe,
  then decide if the V1 heuristic bounds need tightening.
- **Phase 2 sector-relative OCF and sector-relative 13F.** When 13F
  is wired up (Phase 2), it's a sector-relative signal by nature.
  Note for the Phase 2 spec, don't act now.
- **Decimal-place precision in maturity stage assignments.** The
  growth/mature cut at 15% is judgement-laden — borderline names
  (META at +22%, GOOGL at +15%) will need a quarterly re-check.
  Build the process around it (a quarterly universe.json review)
  rather than the audit.
- **Sector-relative volatility modifier.** Same logic as momentum.
  Modifier magnitude is small enough that universe-relative is fine.
- **Industry-grain (GICS sub-industry) peer groups.** Too fine —
  most buckets degenerate to n=1.
- **Restructuring the pillar weights themselves.** Different audit.

---

## Appendix A — concrete cohort distributions (2026-05-17 snapshot)

Revenue growth YoY across the universe (160 with real growth):

```
min  -42.3%   p10  -1.6%   p25  +2.7%   median +7.1%
p75 +15.1%   p90 +23.9%   max +67.6%
```

Standard_compounder bucket (156 with real growth) — virtually identical
distribution. Stage-only fix changed AAPL's percentile by 0.6 points.

Standard_compounder × Technology (38 tickers): bottom 5 are MCHP -42%,
ON -15%, NXPI -3%, AMAT +4%, CSCO +5%; top 5 are MU +49%, NVDA +66%,
MRVL +42%, SMCI +47%, AMD +34%. AAPL +6% lands at the 13th percentile
of this cohort.

Standard_compounder × Financials (17 tickers): bottom 3 are WFC -1.6%,
BRK.B -1.0%, JPM +2.8%. JPM's bank-XBRL coverage issue is the dominant
driver, not the peer group.

90d momentum across the universe (166 with real momentum):

```
min -51.4%   p10 -29.9%   median -0.6%
p90 +34.7%   max +171.7%
```

Universe momentum spread is ~223pp. Sector-relative compresses this
dramatically — e.g. Tech sector (n=41) spans INTC +172% to MCHP -49%,
spread ~221pp; sector-relative does almost nothing to compress the
extremes inside Tech but dramatically changes the rank ordering of
megacap-mature names (AAPL drops from 71.5 universe to 46.3 in-tech).

---

## Appendix B — checklist if you wanted to ship A' + E end-to-end

- [ ] Decide the 15% growth boundary for mature vs. growth (or a
      different metric — e.g. EV/Sales as a quality overlay).
- [ ] Hand-curate the ~20 ticker reclassifications in universe.json.
      Particularly: META, GOOGL, AMD, ASML, ARM (boundary cases).
- [ ] Add `growth_compounder` profile to `data/lthcs/weights.json`.
      Start with the same weights as the current `standard_compounder`
      profile.
- [ ] Add `data/lthcs/sector_group_map.json` (or inline `sector_group`
      in universe.json).
- [ ] Update `lthcs_daily.py` Stage 4 peer-bucketing (snippet above).
- [ ] Add unit test for the 3-tier fallback (primary → stage → universe).
- [ ] Bump `model_version` v1.0.x → v1.1.0 (per queue item #4 spec —
      this is a calibration change, not a structural change).
- [ ] Run `python lthcs_daily.py`. Snapshot diff = the verification.
- [ ] Update `docs/lthcs-followups-queue.md` to close item #3.

Total estimated effort: 1 swarm if E + A' shipped together, 2 if
adding the config (C) refactor.
