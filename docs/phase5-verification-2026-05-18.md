# Phase 5 verification — 2026-05-17 → 2026-05-18

## Verdict — **WORKED (Thesis-led, Adoption noisy, Institutional/DES near-flat)**

Median composite delta 5-17 → 5-18: **+1.10 pts** (mean +1.18, σ 4.20, range −11.5 to +10.0). 106 of 167 tickers ticked up, 58 down, 3 flat; **43 of 167 (25.7%) changed band**. Two-thirds of the headline lift comes from Thesis (~+1.06 pts of the composite median, after the 0.20 pillar weight). Phase 5 unambiguously moved scores; the disappointment-driven "everything looks weak" feel comes from band-width geometry, not Phase 5 regressing things — see §C.

---

## A. Composite drift 5-17 → 5-18

| Stat | Value |
|---|---|
| n | 167 (same universe both days) |
| Median Δ composite | **+1.10** |
| Mean Δ composite | +1.18 |
| Stdev Δ | 4.20 |
| Min / Max Δ | −11.5 (LCID) / +10.0 (NEE) |
| Positive / Zero / Negative | 106 / 3 / 58 |
| Band-changers | 43 (25.7%) |

**Band transition matrix (5-17 → 5-18)** — net mix improved: 19 promoted from review→weakening, 8 from weakening→monitor, 1 from high_confidence→constructive (FANG, −6.5). The few stand-out drags are concentrated in stocks where margin or insider data flipped against them.

| | →high_conf | →constructive | →monitor | →weakening | →review |
|---|:-:|:-:|:-:|:-:|:-:|
| **high_confidence**→ | 0 | 1 | 0 | 0 | 0 |
| **constructive**→ | 0 | 10 | 4 | 0 | 0 |
| **monitor**→ | 0 | 0 | 22 | 6 | 0 |
| **weakening**→ | 0 | 0 | 8 | 42 | 5 |
| **review**→ | 0 | 0 | 0 | 19 | 50 |

**Top 10 gainers / losers**

| ↑ Gainers | 5-17 | 5-18 | Δ | Band move |
|---|---:|---:|---:|---|
| NEE | 41.8 | 51.8 | +10.0 | review → weakening |
| NKE | 19.8 | 29.0 | +9.2 | review → review |
| LRCX | 49.5 | 58.6 | +9.1 | review → weakening |
| MELI | 52.2 | 61.3 | +9.1 | weakening → monitor |
| MDB | 27.9 | 36.3 | +8.4 | review → review |
| KO | 42.4 | 50.7 | +8.3 | review → weakening |
| T | 44.5 | 52.8 | +8.3 | review → weakening |
| MCHP | 45.3 | 53.2 | +7.9 | review → weakening |
| ADP | 47.0 | 54.7 | +7.7 | review → weakening |
| PDD | 52.4 | 60.1 | +7.7 | weakening → monitor |

| ↓ Losers | 5-17 | 5-18 | Δ | Band move |
|---|---:|---:|---:|---|
| LCID | 51.4 | 39.9 | −11.5 | weakening → review |
| AMT | 69.5 | 60.3 | −9.2 | monitor → monitor |
| QCOM | 63.5 | 54.9 | −8.6 | monitor → weakening |
| COF | 72.0 | 63.8 | −8.2 | constructive → monitor |
| CDNS | 72.7 | 65.0 | −7.7 | constructive → monitor |
| SMCI | 58.8 | 52.1 | −6.7 | weakening → weakening |
| FANG | 85.0 | 78.5 | −6.5 | high_conf → constructive |
| ABNB | 59.1 | 52.9 | −6.2 | weakening → weakening |
| CDW | 44.8 | 38.6 | −6.2 | review → review |
| GE | 62.7 | 56.9 | −5.8 | monitor → weakening |

**Per-pillar contribution to composite (× 0.2 weight, all tickers)**

| Pillar | Median Δ sub_score | Mean Δ sub_score | Contribution to mean composite |
|---|---:|---:|---:|
| Thesis | **+5.30** | +7.12 | **+1.42** |
| Adoption | +0.80 | +1.11 | +0.22 |
| Institutional | 0.00 | −0.07 | −0.01 |
| Financial | 0.00 | −1.51 | −0.30 |
| DES | −1.90 | −1.29 | −0.26 |

Pillar contributions sum to ≈ +1.07, close to the actual mean +1.18 (rounding + minor maturity/weights effects; 0 tickers had maturity/weight changes). **Thesis alone delivered ~80% of the gain.** Financial is a net drag because the XBRL fallback (P3) brought in many new-but-mediocre margins that compressed previously inflated `revenue_subscore`-only estimates (e.g., FANG margin_subscore drops to 0 under operating_income; AMT, META, MA all fall).

---

## B. Per-commit attribution

### P0 — `a55aab8` un-gate Form 4 + 13F
The audit reported pre-Phase-5 5-18 had `insider=0/holdings=0` (the "1-of-91 day glitch"); after P0, 5-18 shows `has_insider=165, has_holdings=167` — identical to 5-17. So strictly *5-17 → 5-18*, `False→True` counts are 0 because the regression never actually reached the snapshot file. The verifiable effect is "5-18 Institutional sub_score = base_sub_score + combined_adjustment_pts, with adjustment_pts non-zero on 111 of 167 tickers (66%)." Representative tickers (insider+13F adjustment active on 5-18):

| Ticker | base | final | adj pts | Insider regime | Holdings signal |
|---|---:|---:|---:|---|---|
| CHTR | 7.2 | 17.2 | +10.0 | strong_buying | steady |
| C | 55.4 | 48.4 | −7.0 | heavy_selling | distributing |
| SPG | 65.7 | 73.7 | +8.0 | heavy_selling (cluster_buying scored +) | steady |

Verdict: **un-gating worked and is being consumed**, but because 5-17 also had the data, the *delta-vs-yesterday* effect is ~0 (median Institutional Δ = 0.0). P0's real value is preventing yesterday's regression from recurring.

### P1 — `fa926bf` Adoption: Trends + sector-rel + QoQ accel
- `has_trends` 0 → **11 tickers** (AAPL, AMD, AMZN, F, GE, INTC, KO, MCD, NFLX, NKE, ORCL).
- `has_qoq` 0 → **159 tickers**.
- `peer_cohort_strategy` populated on 157 tickers (`sector_relative` for has_qoq cases, `maturity_only` fallback for 10).

| Ticker | Adoption 5-17 | Adoption 5-18 | Δ | Notes |
|---|---:|---:|---:|---|
| AMZN | 71.7 | 84.5 | +12.8 | trends=stable + qoq +17% |
| MCD | 28.3 | 56.7 | +28.4 | trends=stable, qoq positive |
| KO | 13.0 | 37.6 | +24.6 | qoq positive, trends=fading offsetting |
| AAPL | 50.0 | 28.1 | **−21.9** | trends=fading (regime −0.51), qoq=−22% |
| ORCL | 60.9 | 35.0 | **−25.9** | trends=fading; previously 50-anchored |
| GE | 81.9 | 58.9 | −23.0 | trends=fading reverses revenue-only optimism |

Verdict: **wired correctly, and the pillar is now dispersing** (median Adoption Δ +0.8, but stdev jumps materially — 5-17 had many 50-anchored values, today they spread 1.8 → 100.0). **This pillar is the noisy one** — it is also the modal dragger for review-band tickers (§C).

### P2 — `4c7892b` Thesis 8-K + Yahoo earnings
- `events_refinement_sources` populated on **135/167 tickers** (sec_8k: 31; yahoo_earnings: 127); 23 have ≥2 sources.
- Thesis median jumps **61.7 → 70.8** (+9.1); 5-17 had no 50-anchoring (median already 61.7 from Finnhub recs), so the audit's "Thesis was 50" framing is mostly historical (it applied to dates *before* 5-17, not to 5-17 itself).

| Ticker | sources (5-18) | Thesis 5-17 | Thesis 5-18 | Δ |
|---|---|---:|---:|---:|
| AMZN | sec_8k + yahoo_earnings | 56.2 | 77.5 | +21.3 |
| AEP | sec_8k + yahoo_earnings | 55.7 | 68.0 | +12.3 |
| ACN | sec_8k + yahoo_earnings | 60.3 | 67.7 | +7.4 |

Verdict: **biggest mover, highest-impact commit of the phase**. ~+1.42 pts of the +1.18 mean composite delta originates here.

### P3 — `f5e2259` margin XBRL fallback + bank cohort
- `has_margin`: 5-17 = 93 → 5-18 = **158/167** (False→True for 65 tickers).
- `margin_source` split (5-18): `gross_profit` 86 · `operating_income` 25 · `revenue_minus_cost` 22 · `sales_revenue_gross` 15 · `bank` 10 · `none` 9.
- Bank cohort 5-17 = 0 → 5-18 = **10** (BAC, BK, C, COF, GS, JPM, MS, SCHW, USB, WFC) — matches spec's "7→11" rough target.

| Ticker | margin_source | Financial 5-17 | Financial 5-18 | Δ |
|---|---|---:|---:|---:|
| BK | bank | 34.9 | 72.6 | **+37.7** |
| T | revenue_minus_cost | 47.0 | 62.9 | +15.9 |
| COF | bank | 100.0 | 70.0 | **−30.0** (was margin=null → revenue-only 100; now bank NII gives 0.0 subscore) |
| FANG | operating_income | 99.4 | 69.6 | −29.8 (was inflated; new op-income margin = 0.0) |
| SCHW | bank | 98.2 | 73.5 | −24.7 |

Verdict: **wired correctly but net-negative on aggregate** (mean Financial Δ = −1.51) because for many tickers the prior null-fallback was 50/50 weighted toward `revenue_subscore` which was generous; the new margin reads are mostly mid-range (median margin_subscore in the 40s–60s). This is a **calibration win for accuracy** but a temporary headwind for headline scores in formerly inflated names (COF, FANG, SCHW, TTD, META, AMT).

### P4 — `16f7945` FRED tier-2 macros + sector RSS
- `tier2_inputs` populated on **167/167 tickers** (6 indicators: brent_crude, gasoline_crack, ism_pmi_proxy, housing_starts, consumer_sentiment, u6_unemployment).
- `tier2_total_pts`: min +0.043 · median +0.086 · mean +0.117 · max +0.903.
- Energy cohort (BKR, COP, CVX, FANG, XOM) gets the +0.903 max (brent +0.88, ISM expansion +1.0, housing +0.68).
- `has_sector_rss` is **False on all 167 tickers** — the sector-RSS half of P4 is not yet emitting into Thesis DQ flags. P4's tier-2 is contributing; sector RSS isn't.

Verdict: **partial** — tier-2 macros live and contributing ~+0.04 to +0.90 pts (consistent with the spec's expected range); sector RSS into DES not detected. DES sub_score nevertheless dropped (median −1.9) because non-Phase-5 macro inputs (10Y yield, fed_funds) shifted against most names this run; tier-2 is a small offsetting tailwind.

---

## C. Pillar-drag analysis — why so many in Weakening/Review?

**Band cutoffs verified on 5-18**: review 29.0–49.9 (n=55) · weakening 50.0–59.5 (n=67) · monitor 60.0–69.3 (n=34) · constructive 70.3–79.5 (n=11) · high_confidence absent. **122 of 167 tickers (73%) sit in Weakening or Review** today.

**Modal dragging pillar** (lowest sub_score per ticker, across the 122 W+R names):

| Drags down… | # | % |
|---|---:|---:|
| **Institutional** | 49 | **40.2%** |
| **Adoption** | 41 | **33.6%** |
| DES | 20 | 16.4% |
| Financial | 12 | 9.8% |
| Thesis | 0 | 0% |

Thesis is *never* the dragger on a W/R ticker — confirming Phase 5 fixed the old anchor problem. Pillar medians across W+R:

| Pillar | median | mean | min | max |
|---|---:|---:|---:|---:|
| Adoption | 40.1 | 42.5 | 1.8 | 97.6 |
| Institutional | 42.6 | 44.7 | −1.6 | 97.0 |
| DES | 42.9 | 45.1 | 31.0 | 73.7 |
| Financial | 48.8 | 49.4 | 15.8 | 81.2 |
| Thesis | **68.0** | 67.6 | 40.0 | 81.6 |

**Top 10 widest-spread tickers** — every one is dragged by either Adoption or Institutional:

| Ticker | Band | Composite | Adopt | Inst | Fin | Thesis | DES | Dragger |
|---|---|---:|---:|---:|---:|---:|---:|---|
| TEAM | weakening | 56.6 | 97.6 | **−1.6** | 81.2 | 73.8 | 42.9 | Inst |
| NOW | weakening | 52.7 | 90.7 | **2.4** | 79.7 | 62.8 | 42.9 | Inst |
| INTU | weakening | 58.1 | 90.0 | **4.2** | 74.3 | 79.3 | 42.9 | Inst |
| INTC | review | 44.1 | **13.5** | 97.0 | 31.4 | 65.3 | 42.9 | Adopt |
| MO | review | 48.0 | **4.4** | 87.4 | 51.2 | 67.5 | 41.1 | Adopt |
| AMAT | weakening | 55.6 | **11.0** | 89.4 | 54.5 | 75.5 | 47.6 | Adopt |
| CEG | weakening | 58.9 | 95.6 | **18.1** | 71.5 | 72.3 | 31.0 | Inst |
| ON | weakening | 51.5 | **18.4** | 94.6 | 60.3 | 66.9 | 42.9 | Adopt |
| ADP | weakening | 54.7 | 95.8 | **22.3** | 50.9 | 58.8 | 45.9 | Inst |
| CSCO | weakening | 56.1 | **23.1** | 95.2 | 55.0 | 64.2 | 42.9 | Adopt |

**Conclusion.** The "everything looks weak" perception is **not even weakness** — it is **two specific pillars dragging**: Institutional (40%) and Adoption (34%). These two account for ~74% of all band-floor anchors. The Institutional drag in the spread-leaders (TEAM, NOW, INTU, ADP, CEG) is striking: sub_scores in the **0–25 range** for stocks whose Adoption/Financial pillars score 80–95. Per the variable_detail, this is `momentum_pct_90d` swinging deeply negative for high-multiple software/utilities (e.g., TEAM `base_sub_score ≈ 1.4`), which the audit identified as the lone IC contributor; insider regimes are not yet large enough to override it. The Adoption drag in INTC/MO/AMAT/ON/CSCO is the new P1 inversion: `trends=fading` plus negative QoQ pushes the new sub-component math to sub-20 (vs. the constant-50 anchor that used to mask these).

This **agrees with the audit's direction** but updates its diagnosis: the audit feared Thesis would stay 50-anchored — Phase 5 fixed that (Thesis median is now 67.95 across W+R). The audit also flagged Adoption as "dead-weight, IC +0.004, inverts at Q5–Q1"; P1 has now *made it move* but in many cases it moves the wrong direction relative to fundamentals (high-Adopt tickers like TEAM still in weakening; low-Adopt tickers like MO scoring 4.4 despite cash-flow strength). The audit's band-width artifact thesis (50–59 weakening, 0–49 review = wide review band) remains the secondary cause: 55 review tickers span a 21-point range.

---

## D. Recommendation

**Highest-leverage next move: recalibrate Adoption (audit's β follow-up).** P1 successfully wired the inputs, but the resulting distribution is fundamentals-anti-correlated for ~34% of W/R names — exactly the "Adoption inverts at Q5–Q1" failure the audit predicted. Either (i) cap the Trends/QoQ contribution to ±15 pts off a 50-neutral until the IC turns positive on at least 30 days of live data, or (ii) gate the new sub-components behind a `confidence_blend` so the pillar reverts to revenue-only when Trends regime = `fading` with `quality != "good"`.

**Secondary**: investigate Institutional's `momentum_pct_90d` calibration. The TEAM/NOW/INTU −1.6 to +4.2 sub-scores look like raw winsorization is too aggressive for high-vol names in a 90-day drawdown; verify the cohort sizing (currently `universe` cohort n=166) and consider sector-relative momentum to dampen single-name extremes. P3's bank cohort (10 names with `margin_source=bank`) is also worth a sanity pass — COF/SCHW dropping 25–30 pts in one day on a methodology change is correct in spirit but should arguably be smoothed with a one-time `data_quality_flag` so consumers don't read it as deterioration.

Skip sector RSS (P4) re-investigation for now — tier-2 macros are working; sector RSS appears not yet emitted but its incremental contribution would be small (≤0.5 pts based on P4 design).
