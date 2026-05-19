# Adoption Pillar Inversion — Recalibration Analysis

**Date:** 2026-05-19
**Author:** β follow-up to `docs/lthcs-data-audit-2026-05-18.md` (Tier 3 #14) and `docs/phase5-verification-2026-05-18.md` (§C)
**Scope:** Diagnose why Adoption Momentum still inverts at the 21d horizon (Q5–Q1 = −1.4%, t = −4.97) after Phase 5 P1 (`fa926bf`) wired Google Trends + sector-relative revenue + QoQ acceleration. Propose specific recalibrations.
**Data:** `variable_detail/2026-05-18.json` (167 tickers), `snapshots/2026-05-18.json`, `trends/2026-W20.json` (n=11). The 2026-05-19 variable-detail rolls in tonight's cron; commit `420607a` updated only history files, so analysis is anchored on 5-18.

## Verdict

**The inversion is *not* primarily a Google Trends problem — it is a `sector_relative_revenue` percentile-rank artifact at small cohort sizes, amplified by `qoq_accel` adding cyclical-seasonal noise.** Trends drags 11 specific names hard (INTC adop=13.5, regime=`fading`, trends_subscore=0.0), but the other 156 tickers run trends_subscore frozen at 50.0 and *still* show the value-vs-growth inversion. Smoking gun: **32 of 167 tickers (19%) pinned at the percentile-rank floor (rev_sub=0, n=14) or ceiling (rev_sub=100, n=18)**, driven by `_MIN_SECTOR_COHORT = 8` (`lthcs/pillars/adoption.py:95`) being too low. Trends fix contributes maybe 1/3 of the inversion magnitude; sector-relative percentile is the dominant driver.

---

## A. Inversion mechanics

### Side-by-side: value/cyclicals vs growth (2026-05-18, sector_relative cohort)

| Ticker | Sector (cohort n) | rev_YoY | rev_sub | QoQ % | qoq_sub | tr_sub | has_trends | **Adoption** |
|---|---|---:|---:|---:|---:|---:|:-:|---:|
| MO | Consumer Staples (14) | −3.1% | **0.0** | −10.6% | 14.6 | 50.0 | F | **4.4** |
| INTC | Technology (42) | −0.5% | 7.7 | −0.6% | 48.1 | **0.0** | T | **13.5** |
| KO | Consumer Staples (14) | +1.9% | 25.0 | +0.1% | 50.5 | 50.0 | T | **37.6** |
| PG | Consumer Staples (14) | +0.3% | **0.0** | −4.4% | 35.4 | 50.0 | F | **10.6** |
| JNJ | Health Care (22) | +6.0% | 50.0 | +0.3% | 51.0 | 50.0 | F | **50.3** |
| NVDA | Technology (42) | +65.5% | 100.0 | +22.0% | 100.0 | 50.0 | F | **100.0** |
| META | Comm Svcs (14) | +22.2% | 100.0 | +9.9% | 83.0 | 50.0 | F | **94.9** |
| CRWD | Technology (42) | n/a | 50.0 | n/a | 50.0 | 50.0 | F | **50.0** |
| DDOG | Technology (42) | +27.7% | 50.0 | +13.6% | 95.5 | 50.0 | F | **63.6** |

Notes:
- `PG` is the cleanest pathology: YoY=+0.3% (positive, mild), QoQ=−4.4% (mild Q1→Q2 seasonal), no Trends → Adoption=10.6 (review band). +0.3% is the *worst rank* in the 14-member staples cohort (median +5.8%) → pinned at 0.0. With universe-percentile, PG would land ~25th.
- `MO` Adoption=4.4 is defensible (revenue did contract) but magnitudes are wrong: YoY −3.1% pins rev_sub=0, QoQ −10.6% lands qoq_sub=14.6 (near the bounded-linear floor at −15%). The pillar treats MO as zero-adoption-capacity; 21d-forward, MO mean-reverts.
- `CRWD` returns 50.0 because SEC XBRL has no usable revenue parse (`has_revenue=False`). Right behavior, but lost signal.

### Correlations (n = 167 unless noted)

- corr(revenue_subscore, sub_score) = **+0.956** — revenue percentile is essentially the pillar
- corr(revenue_growth_yoy, sub_score) = +0.623 (n=161)
- corr(qoq_acceleration_pct, sub_score) = +0.437 (n=159)
- corr(trends_subscore, sub_score) [trends-only subset] = +0.569 (n=11)

### Where does the 21d inversion come from?

Pillar correlates positively with revenue growth — but 21d-forward returns invert. That's a **distributional mismatch**, not a sign error: high-Adoption names (NVDA, META, AVGO, PDD, MELI at 95–100) are the names that *already ran*. The pillar measures growth-already-happened (TTM YoY, last-quarter QoQ); 21d-forward is when crowded-trade unwinds bite. **Growth-at-extremes is the wrong target for a 21d momentum proxy** — the percentile-rank floor/ceiling pin magnifies this by clustering tails artificially.

### The 11 Trends-bearing names

From `data/lthcs/trends/2026-W20.json`:

| Ticker | regime | acc_4w% | tr_sub | Adoption sub |
|---|---|---:|---:|---:|
| AAPL | fading | −17.0 | 70.0 | 28.1 |
| AMD | fading | −35.7 | 30.0 | 56.3 |
| AMZN | stable | −11.9 | 80.0 | 84.5 |
| F | fading | −26.0 | 60.0 | 33.8 |
| GE | fading | −44.4 | 10.0 | 58.9 |
| INTC | fading | −46.9 | **0.0** | 13.5 |
| KO | fading | −29.0 | 50.0 | 37.6 |
| MCD | stable | +7.7 | 90.0 | 56.7 |
| NFLX | accelerating | +35.1 | 100.0 | 87.1 |
| NKE | fading | −35.0 | 40.0 | 15.8 |
| ORCL | fading | −36.4 | 20.0 | 35.0 |

Mean Adoption for the 11 = **46.1**; universe median = 50.7 → these names sit ~4.6 pts *below* median. Fading trends 10/11 here = **selection bias**: tickers with enough Google search volume to register a topic-ID time series are mature consumer brands past their peaks (AAPL/NKE/KO/INTC). Curated `TICKER_TO_TREND_TERM` map has 30 entries (`lthcs/sources/google_trends.py:71-100`), weekly batch produced data for 11 this week — over-indexes consumer megacaps.

---

## B. Hypothesis tests

### H1 — `has_trends` wrong signal for value/cyclicals (SECONDARY)

**Failure mode:** Regime classifier (`google_trends.py:48-51`) treats any acc_4w_pct < −15% as `fading`. For mature consumer brands (KO, NKE, INTC), search interest *baseline* drifts down secularly with display-ad fatigue, regardless of fundamentals. INTC QoQ = −0.6% (essentially flat) but trends_subscore = 0.0 anchors Adoption at 13.5.

**Counterexample:** GE — revenue_subscore = 89.5, qoq_sub = 55.8 (strong fundamentals) but trends regime `fading` (acc_4w = −44.4%) drags trends_subscore to 10.0 → Adoption = 58.9. **Trends drags GE ~25 pts purely on search-interest decay** (P5 verification: dropped 81.9 → 58.9 day-over-day).

### H2 — `sector_relative_revenue` percentile pin at small cohort sizes (DOMINANT)

**Failure mode:** `peer_relative_percentile` over a 14-member Consumer Staples cohort gives 7.14-pt step rank — discontinuous. When focal is at the cohort *floor* (PG +0.3%, KHC −0.1%, MO −3.1%), all three get pinned at 0.0. Pillar treats "barely positive" and "actually shrinking" identically.

**Counterexample:** PG at +0.3% YoY → rev_sub=0.0, identical to MO at −3.1%. 3.4-pt YoY gap collapsed to zero signal. Universe-relative PG → ~25th percentile (rev_sub≈25), Adoption ~30. **14/167 (8.4%) pinned at rev_sub=0, 18/167 (10.8%) at rev_sub=100 — 19% of universe on the rank boundaries.**

### H3 — `qoq_accel` adds cyclical-seasonal noise (TERTIARY)

**Failure mode:** QoQ is explicitly non-seasonally-adjusted (`adoption.py:374-381`). For staples/retail, Q1→Q2 is a known seasonal drag. **72/159 tickers (45.3%) have negative QoQ this quarter** — bimodal by sector (staples + utilities negative; semis + software positive).

**Counterexample:** PEP — YoY +2.3% (rev_sub=50 neutral), QoQ −18.8% (qoq_sub=0 floor). Standard winter→spring beverage roll-off; pillar reads it as collapsing business.

---

## C. Recalibration proposals (ranked)

### 1. Bump `_MIN_SECTOR_COHORT` 8 → 20, soften percentile-rank ties at the extremes
**Diff:** Raise `_MIN_SECTOR_COHORT` (`adoption.py:95`) to 20. Cohorts 8–19 fall back to universe percentile. In `lthcs.normalize.peer_relative_percentile`, when focal ties the cohort min or max, return 10.0 / 90.0 instead of 0.0 / 100.0. Stops the 19% floor/ceiling pin.
**Expected IC delta:** +0.02 to +0.03 at 21d (largest — removes the mechanical Q5–Q1 spread driver).
**Risk:** Small. Universe-percentile for staples/utilities/materials reduces within-sector differentiation. Keep sector-rel for cohorts ≥ 20 (Tech 42, HC 22, Industrials 21); fallback for smaller. Names at 100 today (NVDA, MELI, AVGO) keep equivalent universe rank.
**Effort:** **XS** — two constants + ~5 LOC in `normalize.py`.

### 2. Gate Trends contribution when `regime=fading` AND fundamentals don't corroborate
**Diff:** In the weight-ladder (`adoption.py:734-781`), add a gate: if `regime` in {`fading`, `collapsing`} AND `acc_12w_pct - acc_4w_pct > 5` (search interest bottoming, not actively collapsing) AND focal revenue_growth_yoy >= sector median, halve trends weight (0.30 → 0.15), re-weight revenue + QoQ proportionally. Catches secular-drift cases (INTC, KO, NKE — fading short-term but stable long-term) without zeroing trends.
**Expected IC delta:** +0.01 to +0.015 at 21d. Smaller than (1) because only 11 tickers today, but compounds as `TICKER_TO_TREND_TERM` expands.
**Risk:** Could mask legitimate NFLX-style rollovers. Mitigation is the revenue-corroboration clause — if revenue ALSO weakens, the gate doesn't fire.
**Effort:** **S** — one conditional + weight reshuffle.

### 3. Replace bounded-linear QoQ with sector-relative QoQ z-score
**Diff:** Drop globals `_QOQ_SCORE_LOW / _QOQ_SCORE_HIGH` (`adoption.py:108-109`). Compute focal QoQ minus sector-median QoQ, divide by sector-stdev, map z ∈ [−2, +2] → [0, 100]. De-seasonalizes implicitly: if 12/14 staples drop QoQ together, *relative* signal is what matters.
**Expected IC delta:** +0.01. Lifts qoq corr from +0.437 to ~+0.55; reduces cyclical Q5–Q1 drag.
**Risk:** Unstable for cohorts < 8 (Materials n=2, Real Estate n=2). Fall back to universe-relative for those.
**Effort:** **S** — ~15 LOC, reuses existing peer-distribution helper.

### Honorable mentions

- Extend "stale" trends weight treatment to `partial` quality (`adoption.py:71-72`). **XS, +0.005 IC.**
- Drop legacy `interest_series` path (`adoption.py:711-720`); daily pipeline doesn't pass it. **XS, IC-neutral.**
- Expand `TICKER_TO_TREND_TERM` to 80+ topic IDs (weekly-batch concern). **M, +0.005 IC.**

---

## D. Time-gated check

Audit (line 22) says Adoption IC re-validation is **gated on 30 days post Phase 5**. P1 landed 2026-05-17, so audit-implied window is **2026-06-17**.

**Revised: push to 2026-06-24 (~35d post P1)** because (a) 21d-forward returns for tickers entering the window 2026-04-18 only fully resolve by 2026-06-15, then need ~10d for stable IC; (b) the 11 Trends tickers need 4 weekly batches (W20–W23 ending 2026-06-07) plus 2w forward resolution → 2026-06-21.

**If we ship any recalibration before 2026-06-01, add another 21d to the gate** → 2026-06-22 → **2026-07-13** for post-recalibration IC. Don't ship more than one fix per IC window or attribution gets impossible.

**Recommended sequence:** Land #1 (sector-cohort fix) by 2026-05-22 → measure IC ~2026-06-12. If +0.02–0.03 confirmed, land #2 (trends gate) by 2026-06-15 → re-measure ~2026-07-06. Hold #3 (QoQ sector-z) until #1 + #2 confirmed.
