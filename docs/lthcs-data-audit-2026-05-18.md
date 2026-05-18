# LTHCS Data Audit — 2026-05-18

**Snapshot:** 2026-05-18 (`model_version v1.1.0`, 168-ticker universe).
**Window analyzed:** 91 daily snapshots (2026-02-17 → 2026-05-18), 15,104
ticker-day observations.
**Reference inputs:** `data/lthcs/backtest/2026-05-18_validation/report.md`,
`data/lthcs/adaptive_weights/2026-05-18_walk_forward_after_fixes.md`,
`data/lthcs/snapshots/`, `data/lthcs/variable_detail/`, `data/lthcs/insider/`,
`data/lthcs/holdings/`, `data/lthcs/sentiment/`.

---

## Executive summary

LTHCS is empirically validated as a cross-sectional signal (21d composite
IC = +0.127, t≈+8.3 raw / +1.8 overlap-corrected; band ordering monotone
high→review) but the surface hides a **lopsided dependence on one pillar
(Institutional, IC +0.204) which itself is currently riding on only one
sub-component (90d momentum).** Smart-money inputs (Form 4, 13F) — the
nominal raison-d'être of the Institutional pillar — landed in production on
exactly **one** day of the 91-day backfill (2026-05-17) and **dropped out
again on 2026-05-18.** Adoption is dead-weight (IC +0.004 at 21d, inverts at
Q5–Q1) because Google Trends is unwired and revenue alone is too coarse.
DES is positive but ceiling-limited around 73; Thesis just came alive
yesterday via Finnhub recommendations but has no live history to back-test.

- **Overall scoring grade: C+** — predictive at the headline level, but
  three of five pillars are stub-equivalents and one is a 1-of-91-day
  pipeline glitch from collapsing into pure momentum.
- **Single biggest risk:** the institutional pipeline regression on
  2026-05-18 (insider=0, holdings=0, inst_holdings=0 across 167 tickers
  when 2026-05-17 had insider=165, holdings=167). The workhorse pillar
  silently lost its differentiated inputs in today's production run.
- **Single biggest opportunity:** because the Institutional IC of +0.204
  was generated almost entirely from `momentum_pct_90d` (the only
  insider/13F-bearing date was the **last** date of the backfill window),
  any genuine wire-up of Form 4 + 13F into the daily pipeline is pure
  upside — the +0.204 is the *floor* for what this pillar should deliver
  once smart-money inputs persist.

---

## Pillar-by-pillar quality scorecard

| Pillar | Grade | One-line justification | Most damaging gap |
| --- | :-: | --- | --- |
| Institutional Confidence | **B** | +0.204 IC (workhorse) but driven by momentum alone; insider+13F integrated on 1 of 91 days. | Form 4 + 13F not actually wired into daily pipeline |
| Financial Evolution | **B** | Most consistent (IC +0.086 @ 21d); revenue/OCF coverage 94–97%. | Margin coverage only 56% (74 tickers missing); banks (NII) only 7 tickers |
| Thesis Integrity | **C-** | Just came alive today (5-18, mean 70.4) via Finnhub recommendations; constant 50 on first 88 of 90 backfill dates. | Zero live history to validate; single-source dependency (Finnhub) |
| DES | **C** | IC +0.022 @ 21d, marginal; ceiling-capped near 73 by sector tilt table; +0.033 @ 5d is real. | No micro DES (industry-specific cycle signals); sector-adjustment table is conservative |
| Adoption Momentum | **D** | IC +0.004 @ 21d, INVERTS Q5–Q1 by –1.4% (t=–4.97); revenue-only because Google Trends offline. | Inverted sign at the canonical 21d horizon; trends sub-score is constant 50 across 100% of universe-days |

**Composite grade: C+.** The validation report's "+18.7 Sharpe" headline
hides that two of five pillars are noise (Adoption) or untested (Thesis),
and the strong pillar (Institutional) is a momentum signal in disguise.

---

## Systematic data gaps

### Today's coverage matrix (2026-05-18, n=167 active scored)

| Pillar / Component | Real-data tickers | % | Notes |
| --- | --- | --- | --- |
| Adoption · revenue | 161 | 96.4% | SEC EDGAR; gap = ~6 tickers w/ XBRL parse issues |
| Adoption · trends | **0** | **0.0%** | pytrends rate-limited; weekly batch unwired |
| Institutional · momentum_90d | 166 | 99.4% | BRK.B fallback still broken (.B suffix) |
| Institutional · inst_holdings (13F overall) | **0** | **0.0%** | Was nominally there yesterday; regressed today |
| Institutional · insider (Form 4) | **0** | **0.0%** | 5-17 had 165/165; 5-18 lost it |
| Institutional · holdings (top-10 13F) | **0** | **0.0%** | 5-17 had 167/167; 5-18 lost it |
| Financial · revenue % | 162 | 97.0% | SEC EDGAR |
| Financial · margin (GrossProfit) | **93** | **55.7%** | XBRL concept missing on services-heavy names |
| Financial · OCF | 158 | 94.6% | 9 tickers missing across 6 sectors |
| Financial · NII / PCL / noninterest (bank cohort) | 7 | 4.2% | Only 7 bank cohort members (correct) |
| Thesis · sentiment | 167 | 100.0% | All Finnhub recommendation (single source) |
| Thesis · finnhub_recommendation | 167 | 100.0% | Just wired (yesterday all `source=none`) |
| Thesis · sec_8k_real | 0 | 0.0% | News-feed earnings spec un-wired |
| Thesis · yahoo_earnings_real | 0 | 0.0% | News-feed earnings spec un-wired |
| DES · macro_signals | 9.0 avg | 100% | Tier-1 macros wired (oil/CPI/FF/10y/UR/real10y/VIX/M2 + Δ10y) |
| DES · tier2_macro | partial | n/a | Code exists (`fred_tier2.py`) — actual integration TBD |
| DES · sector_known | 167 | 100.0% | sector_des_weights.json |

### Top 10 holes (universe-wide; ranked by IC-impact-per-fix)

1. **Form 4 insider → Institutional** — wire-up regression; the dir has
   data, the variable_detail doesn't pull it. Single biggest "free win"
   because the integration code already worked **on 2026-05-17**.
2. **13F holdings → Institutional** — same regression as above. Quarterly
   cadence still gives real cross-sectional signal.
3. **Google Trends → Adoption (trends_subscore)** — frozen at 50 on 100% of
   universe-days. Without this, Adoption has only revenue, which clearly
   doesn't carry signal at 21d.
4. **Margin (74 missing tickers) → Financial** — disproportionately
   services / banks / utilities; can't fairly rank financial quality when
   nearly half the universe is missing the central margin metric.
5. **Thesis live history** — Finnhub-recommendation only fired starting
   2026-05-18 (mean 70.4, range 30–82). No back-test history to verify
   IC. Until ~30 days pass, Thesis is effectively unvalidated.
6. **8-K material events → Thesis** — 0% coverage; per
   `docs/news-feeds-earnings-events.md` §3, the highest-S/N event source
   for the whole universe. SEC EDGAR access already wired.
7. **Yahoo earnings_dates + recommendations** — 0% coverage; cheap to add
   (already pulling Yahoo for prices). Would give earnings beat/miss for
   the entire universe.
8. **FDA Press Announcements RSS → Thesis** — 0% coverage; per
   `news-feeds-sector-specific.md` the top S/N source for ~15 pharma
   names; pharma currently scores blind on industry-specific news.
9. **Bank-cohort revenue benchmark** — only 7 bank-cohort tickers; without
   a wider bank cohort, NII/PCL/noninterest percentiles are degenerate
   (effectively binary).
10. **BRK.B Yahoo fallback** — 1 ticker missing momentum for ~3 months
    because `.B` suffix breaks yfinance. Trivial fix (try BRK-B) and
    deterministic.

### Sector concentration of gaps

- **Margin missing** disproportionately hits Financials (11), Comm
  Services (11), Consumer Discretionary (10), Tech (9). Confirms the
  Phase-2 hypothesis that XBRL GrossProfit doesn't apply uniformly.
- **OCF missing** scattered (Tech 4, Health 1, Financials 1, Staples 1,
  Industrials 1, Cons Disc 1) — no obvious systematic pattern; likely
  XBRL parse-quality issue.
- **Thesis sources**: 100% Finnhub recommendations across all sectors;
  no sector-relative differentiation yet.

---

## Cross-source consistency checks

### Form 4 (insider) vs 13F (holdings) on 2026-05-17 (only available day)

- n_pairs: 165 (intersect insider × holdings)
- **Pearson(insider conviction_score, 13F signal_score) = +0.021** (≈ no
  correlation).
- "Smart money agrees BUY" (both > 0): **1** ticker
- "Smart money agrees SELL" (both < 0): **14**
- Opposite signs: **16**
- Vast majority (~134): one or both at zero.

**Interpretation:** Insider Form 4 selling and 13F manager flows are
**not** measuring the same underlying signal at our 1-quarter cadence.
Sometimes a CEO is selling (planned 10b5-1) while large managers are
adding — that's not an inconsistency, it's two different time horizons of
"smart money." The +0.021 correlation says we shouldn't treat them as
redundant; they're complementary inputs that need separate weights inside
the Institutional pillar. (Right now, when both are wired,
`combined_adjustment_pts` does add them — that's fine, but we should not
collapse them.)

### Finnhub recommendation vs composite (today only)

Implicitly captured by Thesis IC dynamics (cannot back-test yet — only one
date with real data). Re-evaluate in 2 weeks.

### Bank-cohort Financial scoring fairness

The 7-ticker bank cohort (`is_bank_cohort=true`) now has NII/PCL/noninterest
percentiles wired, but the cohort is too small (7 ≈ 4% of universe) for
percentile ranks to be discriminating. JPM and BAC will land near 50 on
each component because the cross-sectional pool is tiny. Expanding the
cohort to all 11 Financials sector tickers (regional banks + universal
banks) would 50%-improve cohort statistical power.

---

## Empirical pillar distributions (2026-05-18)

| Pillar | n | mean | median | std | min | p5 | p25 | p75 | p95 | max | n_at_exactly_50 |
| --- | -: | -: | -: | -: | -: | -: | -: | -: | -: | -: | -: |
| adoption_momentum | 167 | 50.5 | 50.0 | 29.3 | 0.0 | 4.3 | 26.1 | 75.8 | 95.8 | 100.0 | 7 |
| institutional_confidence | 167 | 50.0 | 50.0 | 29.0 | 0.0 | 4.8 | 25.5 | 74.5 | 95.2 | 100.0 | 1 |
| financial_evolution | 167 | 57.3 | 55.0 | 20.2 | 15.2 | 25.4 | 44.1 | 72.5 | 92.6 | 100.0 | 5 |
| thesis_integrity | 167 | 70.4 | 72.9 | 8.4 | 30.0 | 57.2 | 66.2 | 76.9 | 80.2 | 81.8 | 0 |
| des | 167 | 46.0 | 42.8 | 10.8 | 30.9 | 31.0 | 41.6 | 45.8 | 73.6 | 73.6 | 0 |
| **composite** | 167 | 54.3 | 55.3 | 11.5 | 26.6 | — | — | — | — | 85.7 | — |

Discriminating power read:
- Adoption / Institutional / Financial: full 0–100 range, good spread.
- Thesis: compressed band 30–82 (now finally moving, since 5-18). Real
  signal-volume TBD.
- DES: ceiling-clamped near 73 — see `lthcs-open-items-audit.md` §δ. p95
  = p75 = max suggests the sector-tilt math caps the distribution well
  below 90.

---

## Score band distribution health

### Today (2026-05-18, post-recalibration `elite.min=85`)

| Band | Count | % | t-stat (21d fwd, validation report) |
| --- | -: | -: | -: |
| elite | 0 | 0.0% | n/a (no observations in 90-day backfill) |
| high_confidence | 1 | 0.6% | +8.46 (180 obs in backfill, +17.4% return) |
| constructive | 16 | 9.6% | +13.84 (+5.9%) |
| monitor | 36 | 21.6% | +12.68 (+4.0%) |
| weakening | 55 | 32.9% | +9.17 (+2.0%) |
| review | 59 | 35.3% | +6.03 (+1.1%) |

### 90-day historical (post-elite recalibration applied today)

| Band | Count | % |
| --- | -: | -: |
| elite | 0 | 0.0% |
| high_confidence | 181 | 1.2% |
| constructive | 874 | 5.8% |
| monitor | 2,393 | 15.8% |
| weakening | 4,574 | 30.3% |
| review | 7,082 | 46.9% |

**Band ordering hypothesis holds monotonically at 21d.** That's the
strongest single piece of evidence the framework is doing real work.

But: 47% of observations sit in "Review" (worst band). That's high — the
heatmap looks largely red. Two reads:
1. Honest: many tickers genuinely deserve Review because Thesis was dead
   (constant 50) for the entire 90-day window, anchoring composites low.
2. Calibration: bottom band may be too wide; consider tightening
   `review.max` from 49 → 44 once Thesis live history accumulates.

---

## Data freshness audit

| Source | Latest file | Stale days |
| --- | --- | -: |
| snapshots | 2026-05-18 | 0 |
| variable_detail | 2026-05-18 | 0 |
| narratives | 2026-05-18 | 0 |
| insider (Form 4) | **2026-05-17** | **1** |
| holdings (13F) | **2026-05-17** | **1** |
| sentiment (per-ticker) | all 168 within ≤7d | 0–1 |
| analyst_breadth | **2026-05-17** | **1** |
| macro (FRED tier 1) | 2026-05-17 | 1 |
| trends (Google) | **2026-W20 (week-stamped)** | n/a (weekly) |
| index | 2026-05-18 | 0 |

**Yellow flag:** insider, holdings, analyst_breadth all stuck on 5-17 —
suggests the daily pipeline didn't re-fetch them today even though the
snapshots / variable_detail did write today. This is precisely the
regression that the Section 6 analysis surfaces.

---

## Pipeline integration health — today vs yesterday

| Institutional flag | 2026-05-17 | 2026-05-18 | Delta |
| --- | -: | -: | -: |
| has_momentum | 166 | 166 | 0 |
| has_inst_holdings | 0 | 0 | 0 (chronic Phase-2 stub) |
| has_insider | **165** | **0** | **−165** |
| has_holdings | **167** | **0** | **−167** |

**This is a regression to investigate immediately.** Likely causes:
1. `lthcs_daily.py` is reading insider/holdings from a per-date file
   (`insider/2026-05-18.json`) that wasn't created today, instead of
   falling back to the most-recent ≤T file. The Form 4 / 13F fetchers
   may have failed silently when the per-date file didn't exist.
2. A pillar-side guard rejected stale insider/holdings input (>=24h old)
   without warning.
3. An environment-variable / secret didn't propagate to today's run
   (less likely given snapshots/narratives generated normally).

This must be confirmed before any phase-5 work — otherwise we're tuning a
model running on degraded inputs.

### Other pipeline reads

- **Adoption.trends_subscore** is constant 50 across 100% of universe-days
  for all 91 days — Google Trends weekly batch isn't feeding the daily
  pipeline output, despite the `data/lthcs/trends/2026-W20.json` weekly
  file existing. The wire-up between `lthcs/sources/google_trends.py` and
  the Adoption pillar exists but the daily pipeline isn't reading the
  weekly file.
- **Thesis source coverage** flipped from `none` (100%) on 2026-05-17 to
  `finnhub_recommendation` (100%) on 2026-05-18. Good news, but it means
  every backtest IC for Thesis is on a different signal than what runs in
  production today.

### Cache health

`.cache/lthcs/` total 2.2GB. Breakdown:
- `yahoo/` 1.2GB (price + earnings caches, dominated by historical bars)
- `sec_edgar/` 676MB (XBRL companyfacts)
- `fred/` 190MB
- `sec_form4/` 77MB · `sec_8k/` 41MB · `sec_13f/` 20MB
- `backtest/` 18MB
- `finnhub_sentiment/` **0B** — concerning if production reads from it;
  otherwise just unused
- `fred_tier2/` 4KB — code present but nothing cached yet

No bloat. The `finnhub_sentiment/` empty dir suggests the Finnhub
sentiment endpoint was attempted then removed in favor of
`finnhub_recommendation` (the latter has a 984KB cache). The empty dir
should be removed in a cleanup pass; it doesn't affect scoring.

---

## Recommended fixes — ranked by impact-per-effort (Phase 5 input)

### P0 — Pipeline integration regressions (zero-net-new-code; just diagnose)

1. **Fix the 2026-05-18 insider/holdings drop-out.** Trace why
   `variable_detail/2026-05-18.json` has `has_insider=False` / `has_holdings=False`
   on every ticker when `data/lthcs/insider/2026-05-17.json` and
   `data/lthcs/holdings/2026-05-17.json` both have data. Likely a date-
   mismatched lookup or a "stale > 24h, drop" guard.
2. **Wire Google Trends weekly file into Adoption pillar.** The file
   `data/lthcs/trends/2026-W20.json` is written by the weekly cron but
   the Adoption pillar still uses null. Either the consumer-side reader
   doesn't exist or the join key is wrong.
3. **BRK.B Yahoo fallback** — try `BRK-B` ticker form. Trivial.

### P1 — Adoption pillar overhaul (IC +0.004 → expected +0.05+)

4. **Use sub-pillar percentile ranks within sector for revenue %,
   instead of universe rank**, for non-AI / non-megacap tickers. The 21d
   IC inversion implies high-revenue-growth names are getting *penalized*
   forward — likely a mean-reversion artifact from comparing PLTR / NVDA
   vs the entire universe.
5. **Reactivate trends weekly batch + actually plug it in** (P0 #2 is
   the wire-up; this is the consequences of having two real
   sub-components).
6. **Add a "second derivative" revenue accel signal** (QoQ acceleration
   vs YoY). This is one of the original spec's design intents and is
   trivially derivable from existing SEC EDGAR cached XBRL.

### P2 — Thesis live-history + redundancy

7. **Run for 30 trading days post-Finnhub-recommendation wire, then re-
   run the validation script** with Thesis included for real. Right now
   the IC numbers for Thesis are statistically meaningless.
8. **Wire 8-K material events filter** (`docs/news-feeds-earnings-events.md`
   §3) — SEC EDGAR access already exists. Items 1.01/2.02/5.02/8.01.
   Adds event-driven signal across 100% universe on the days it fires.
9. **Wire Yahoo earnings_dates + recommendations** (same doc §1.1, §2.1).
   Half-swarm; pulls from a Yahoo source already wired.

### P3 — Financial pillar fill-in

10. **Investigate the 74 has_margin=False tickers.** XBRL has many
    revenue-cost variants; falling back to OperatingIncome / RevenueLess
    CostOfRevenue / SalesRevenueGross would close most of the 56%→90%
    gap.
11. **Expand bank cohort to all sector=Financials (11 tickers)** for
    NII / PCL / noninterest percentile statistical power; or merge into
    Insurance/AssetMgmt as a "financials" peer group.

### P4 — DES expansion

12. **Wire FRED tier-2 macros** (Brent, gasoline, ISM, housing, consumer
    confidence, U-6) — source module exists, cache is empty. Adds
    industry-specific cycle context.
13. **Sector-specific RSS** (FDA / EIA / Fed press) per
    `news-feeds-sector-specific.md`. Cheap and adds ~30 names of
    sector-driven Thesis lift.

### P5 — Calibration follow-ups

14. **Tighten review.max from 49 → 44** once Thesis live-history shows
    natural composite drift upward (1–2 months out).
15. **Re-run walk-forward CV in August 2026** when n_test_real ≥ 20 at
    h=21d (per `walk_forward_after_fixes.md` §5).

---

## Recommended data sources to add

Gap-prioritized by expected IC contribution × effort cost:

| Source | Pillar served | Effort | Why now |
| --- | --- | :-: | --- |
| **Google Trends weekly batch wire** | Adoption | XS | Already running on cron; just consumer-side wire-up. Closes the 0% trends_real coverage. |
| **SEC 8-K material-event filter** | Thesis | S | EDGAR access exists; 100% universe coverage on event days; orthogonal to recommendation-based Thesis. |
| **Yahoo earnings beat/miss + reco** | Thesis | S | yfinance already in deps; gives earnings event signal across whole universe. |
| **FDA Press Announcements RSS** | Thesis (pharma) | M | Best S/N per `news-feeds-sector-specific.md` for the ~15 pharma names that are currently scored blind on news. |
| **FRED tier-2 macros (Brent, ISM, housing)** | DES | M | Code exists (`fred_tier2.py`), cache empty — just turn it on. Lifts DES out of its 0.02 IC. |
| **Form 4 + 13F daily fetch** (re-enabling regressed pipeline) | Institutional | XS | This is just a bug fix — the integration worked on 2026-05-17. |
| **Expanded bank cohort to 11 names** | Financial (banks) | XS | Just config change to is_bank_cohort criterion. Restores statistical power. |
| **Sector-specific EIA / Fed RSS** | DES + Thesis | S | Adds sector-cyclical signal for Energy and Financials sub-universes. |

---

## Single most impactful recommendation for Phase 5

**Resolve the 2026-05-18 insider/holdings pipeline regression and wire
Google Trends into Adoption — in that order.** Both are bug-style fixes
(the integration worked at least once for each) with disproportionate
scoring upside:

- The Institutional regression turns the workhorse pillar from real
  smart-money signal back into pure momentum. **Until that's fixed,
  every other Phase 5 tuning is on degraded inputs.** This is the gating
  P0.
- Google Trends being constant-50 across 100% of universe-days renders
  Adoption a one-leg pillar (revenue only) which has demonstrably
  inverted at 21d. Fixing the trends consumer-side wire makes Adoption
  two-legged for the first time, and is the prerequisite to having a
  fair shot at the IC recalibration target.

Everything else (Thesis 8-K + Yahoo earnings, FDA RSS, FRED tier-2 macros,
bank-cohort expansion) is high-value but only meaningful once the data
that the framework already nominally consumes is actually flowing through
the production daily run.

---

## Files referenced

- `/Users/bryantabiadon/Documents/btc-eth-etf-dashboard/data/lthcs/snapshots/2026-05-18.json`
- `/Users/bryantabiadon/Documents/btc-eth-etf-dashboard/data/lthcs/variable_detail/2026-05-18.json`
- `/Users/bryantabiadon/Documents/btc-eth-etf-dashboard/data/lthcs/variable_detail/2026-05-17.json`
- `/Users/bryantabiadon/Documents/btc-eth-etf-dashboard/data/lthcs/insider/2026-05-17.json`
- `/Users/bryantabiadon/Documents/btc-eth-etf-dashboard/data/lthcs/holdings/2026-05-17.json`
- `/Users/bryantabiadon/Documents/btc-eth-etf-dashboard/data/lthcs/backtest/2026-05-18_validation/report.md`
- `/Users/bryantabiadon/Documents/btc-eth-etf-dashboard/data/lthcs/adaptive_weights/2026-05-18_walk_forward_after_fixes.md`
- `/Users/bryantabiadon/Documents/btc-eth-etf-dashboard/data/lthcs/weights.json`
- `/Users/bryantabiadon/Documents/btc-eth-etf-dashboard/docs/lthcs-open-items-audit.md`
- `/Users/bryantabiadon/Documents/btc-eth-etf-dashboard/scripts/lthcs_audit_data_quality.py` (this run's analysis script)
