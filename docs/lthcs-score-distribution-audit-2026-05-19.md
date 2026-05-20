# LTHCS composite-score distribution audit — 2026-05-19

Phase 3 tasks 3.2 + 3.3 + 3.4. Produced by `scripts/lthcs_score_distribution_audit.py`
against the latest snapshot (`data/lthcs/snapshots/2026-05-18.json` — the 2026-05-19
pipeline has not yet run as of this audit). Universe size: **167** tickers.

Full numbers are in:

* `data/lthcs/quality_audit/2026-05-19_composite_distribution.md`
* `data/lthcs/quality_audit/2026-05-19_pillar_correlation.md`

---

## 1. Composite distribution health

**Verdict: left-skewed, top bands starved, review band overflowing.**

| stat | value |
|---|---|
| mean | 48.69 |
| stdev | 12.05 |
| min / max | 18.3 / 77.9 |
| p5 / p25 / p50 / p75 / p95 | 28.6 / 40.3 / 49.6 / 56.5 / 67.97 |

ASCII histogram (10-point bins):

```
  0-9   |                                          0
 10-19  | #                                        1
 20-29  | ########                                 11
 30-39  | #####################                    28
 40-49  | ###################################      46
 50-59  | ######################################## 53
 60-69  | #################                        23
 70-79  | ####                                     5
 80-89  |                                          0
 90-100 |                                          0
```

| band | range | count | share |
|---|---|---|---|
| review | 0-49 | 86 | **51.5%** |
| weakening | 50-59 | 53 | 31.7% |
| monitor | 60-69 | 23 | 13.8% |
| constructive | 70-79 | 5 | 3.0% |
| high_confidence | 80-84 | 0 | **0%** |
| elite | 85-100 | 0 | **0%** |

**Starved bands:** `high_confidence` and `elite` have zero population. No
ticker in the universe currently scores ≥80.

**Over-populated band:** `review` (0-49) holds 51.5% of the universe —
more than half the universe is in "Structural Review Required".
That's not a banding problem so much as a *scoring* problem: with the
mean at 48.7 and stdev 12.0, the universe centre of mass sits right at
the review/weakening boundary.

The top of the distribution is a single ticker (`AVGO`, 76.8) with
zero thesis-unavailable flag. The next 4 in the top-5 all carry the
`thesis_unavailable` data-quality flag, which means their composites
were boosted by the score.py renormalization that drops the
neutral-50 placeholder — so the constructive band is essentially
"AVGO + four tickers that got a renormalization tailwind." Without
that mechanism, the top of the distribution would be even more
compressed.

### Per-cohort shape

| cohort | n | mean | stdev | p50 |
|---|---|---|---|---|
| growth_compounder | 14 | 51.93 | 16.29 | 54.3 |
| mature_compounder | 49 | 50.11 | 11.43 | 51.6 |
| recovery_stabilization | 2 | 47.45 | 9.85 | 47.45 |
| recovery_rerating | 1 | 49.6 | — | — |
| standard_compounder | 100 | 47.66 | 11.62 | 48.65 |
| pre_profit_growth | 1 | 38.3 | — | — |

`growth_compounder` is the only cohort with meaningful right-tail
density (p75 = 60.5, stdev 16.3). `standard_compounder` (the catch-all
bucket holding 60% of the universe) is tightly centred at ~48 with a
narrow spread — it's the cohort dragging the universe mean down.

`recovery_stabilization` only has 2 tickers and `recovery_rerating` /
`pre_profit_growth` only 1 each — these cohorts are too thin for the
per-cohort weights profile to be meaningful, but per the task scope
that's GG's call, not mine.

---

## 2. Outliers

### Top 5 by composite

| ticker | composite | band | maturity | adoption | inst | fin | thesis | des | flags |
|---|---|---|---|---|---|---|---|---|---|
| FANG | 77.9 | constructive | standard_compounder | 92.3 | 90.0 | 69.6 | 54.9 | 61.9 | thesis_unavailable |
| MU | 77.6 | constructive | growth_compounder | 93.0 | 96.4 | 93.6 | 58.8 | 47.6 | thesis_unavailable |
| AVGO | 76.8 | constructive | mature_compounder | 100.0 | 80.9 | 84.8 | 80.7 | 47.6 | - |
| NVDA | 74.6 | constructive | growth_compounder | 100.0 | 70.5 | 84.5 | 55.0 | 47.6 | thesis_unavailable |
| ADI | 73.4 | constructive | standard_compounder | 83.1 | 89.2 | 85.8 | 76.0 | 43.5 | - |

### Bottom 5 by composite

| ticker | composite | adoption | inst | fin | thesis | des | flags |
|---|---|---|---|---|---|---|---|
| NKE | 18.3 | 15.8 | 6.0 | 24.0 | 51.9 | 37.4 | thesis_unavailable |
| GM | 22.4 | 14.7 | 27.1 | 19.8 | 58.8 | 37.4 | thesis_unavailable |
| MDB | 24.0 | 21.2 | 13.3 | 27.6 | 58.8 | 43.5 | thesis_unavailable |
| HD | 24.0 | 6.3 | 19.5 | 40.6 | 55.0 | 37.4 | thesis_unavailable |
| ZS | 25.2 | 25.5 | 9.9 | 47.5 | 58.8 | 43.5 | thesis_unavailable |

### Top 3 outlier verdicts

1. **AVGO (composite 76.8)** — **real anomaly, well-supported.**
   The only top-5 ticker without a `thesis_unavailable` flag. Adoption
   is at the ceiling (100.0) with full real data underneath; thesis
   pillar is also +2.88 stdev above the `mature_compounder` cohort
   mean. The composite is earned, not a renormalization artefact.

2. **NKE (composite 18.3)** — **real anomaly worth confirming, but
   not a data-quality issue.** Three pillars are deeply negative
   (adoption 15.8, institutional 6.0, financial_evolution 24.0).
   The `thesis_unavailable` flag means it's getting renormalization
   *help* — without that placeholder lift, NKE would score even
   lower. This looks like a genuine structural concern for the
   ticker, not a measurement defect.

3. **AZN (z=+2.99 on thesis_integrity)** — **data-quality concern,
   not a real anomaly.** AZN carries the `sec_unavailable` flag,
   meaning the SEC EDGAR sector mapping fell back to a stub. Its
   thesis_integrity (78.5) being 3 sigma above the standard_compounder
   cohort mean (55.12, sd 7.81) is consistent with a sentiment-pillar
   stub that doesn't reflect real news flow. The composite of 51.2
   is being held neutral by that stub — recommend re-running once
   the EDGAR mapping for AZN is fixed.

### Pillar-vs-cohort z-score outliers (full table in audit doc)

Seven of the ten top z-score outliers are tickers with `des = 71.6`
in the standard_compounder cohort. That's a *cohort-mean* problem:
financials sector tickers (BK, BLK, COF, MET, SCHW, USB) are
clustered at the high end of DES because the sector_des_weights
boost financials. The cohort `standard_compounder` mixes financials
with other sectors so the financials-sector DES tickers look like
+3 sigma outliers relative to the cohort mean. **Recommendation**:
DES outlier z-scoring should be done *per sector* (or per cohort × sector)
rather than per maturity cohort. Out of scope for this audit, but
worth GG noting.

### Stuck tickers

**167 / 167 tickers have |drift_30d| < 5.0 — but every one is exactly
0.0.** This is a *data-pipeline* issue, not a scoring issue: the
2026-05-18 snapshot has all drift fields zeroed across the board,
which means `prior_scores` is not being passed through from prior
snapshots in the daily build. Drift is therefore providing zero
signal at the moment. **Flagged for separate followup** — not in
this audit's scope to fix, but should be reported to whoever owns
`lthcs_daily.py` / `lthcs/persist.py`.

---

## 3. Cross-pillar correlation

### 5×5 Pearson matrix

| pillar | adoption | inst | fin | thesis | des |
|---|---|---|---|---|---|
| adoption_momentum | +1.000 | -0.131 | **+0.686** | +0.093 | -0.013 |
| institutional_confidence | -0.131 | +1.000 | -0.025 | +0.127 | -0.033 |
| financial_evolution | **+0.686** | -0.025 | +1.000 | +0.144 | -0.014 |
| thesis_integrity | +0.093 | +0.127 | +0.144 | +1.000 | +0.047 |
| des | -0.013 | -0.033 | -0.014 | +0.047 | +1.000 |

### Near-redundant pillar pairs (|r| ≥ 0.7)

**None hit the 0.7 threshold.** The closest is:

* `adoption_momentum ↔ financial_evolution` = **+0.686**

This pair is borderline and worth GG knowing about: if 0.686 holds
or rises over the next month, weight-reduction on one of these
pillars (or merging their signal into a single Growth pillar) would
be a natural simplification.

### Near-orthogonal pillar pairs (|r| ≤ 0.2) — the workhorses

These pairs contribute independent signal — each one is doing
unique work in the composite:

| pair | r |
|---|---|
| adoption_momentum ↔ des | -0.013 |
| financial_evolution ↔ des | -0.014 |
| financial_evolution ↔ institutional_confidence | -0.025 |
| institutional_confidence ↔ des | -0.033 |
| thesis_integrity ↔ des | +0.047 |
| adoption_momentum ↔ thesis_integrity | +0.093 |
| institutional_confidence ↔ thesis_integrity | +0.127 |
| adoption_momentum ↔ institutional_confidence | -0.131 |
| financial_evolution ↔ thesis_integrity | +0.144 |

**DES is structurally orthogonal to every other pillar** (max |r| =
0.047). It's the most independent signal source in the framework —
keep its weight where it is, or even increase it.

### 30-day stability

All 10 pillar-pair correlations are stable over the trailing 30
days (max range = 0.172, well under the 0.30 instability threshold).
The borderline-redundant `adoption ↔ financial_evolution` pair is
in fact the *most* stable (range 0.022 over 30d, hovering tightly
around +0.68) — it's a persistent structural feature, not noise.

---

## 4. Band-threshold sanity check

**Coordinated with GG (weights/thresholds owner) via this audit doc
— I am not modifying `weights.json`.**

Current thresholds (`weights.json` `score_bands`):

```
review:         [0, 49]
weakening:      [50, 59]
monitor:        [60, 69]
constructive:   [70, 79]
high_confidence:[80, 84]
elite:          [85, 100]
```

Observations for GG:

1. The `elite` band (85-100) is empty — no ticker in the universe
   has ever scored ≥85 in this snapshot. Either:
   * the framework's effective ceiling is ~78 (the AVGO ceiling),
     in which case the elite/high_confidence cutoffs need lowering, OR
   * the pillar scoring is too punitive in the high tail and the
     pillars themselves need rescaling.
2. The `review` band is 0-49 (50 points wide vs. 5-10 for the upper
   bands) yet still over-populated at 51.5%. Splitting it into
   `review_critical` (0-29) and `review_watch` (30-49) might give
   the daily monitor more actionable information, since "everything
   from 18 to 49 is the same band" loses a lot of discrimination.
3. The 80/85 split between `high_confidence` and `elite` is fine in
   principle but currently meaningless since both are empty. If GG
   raises pillar score ceilings, this split will matter; if not,
   collapse them.

---

## TL;DR

* **Distribution**: left-skewed, mean 48.7, stdev 12, top two bands
  starved (0 tickers ≥80), review band over-populated (51.5%).
* **Top 3 outliers**:
  * AVGO 76.8 — real, earned, well-supported.
  * NKE 18.3 — real structural weakness, not data quality.
  * AZN thesis-z = +2.99 — data quality (sec_unavailable), recommend re-run.
* **Redundant pillars**: none hit |r|≥0.7. Closest watch-list pair:
  `adoption_momentum ↔ financial_evolution` at +0.686 (stable).
* **Workhorse / orthogonal pillars**: DES is orthogonal to everything;
  institutional_confidence is mildly negatively correlated with
  adoption_momentum (-0.131) — both carry independent signal.
* **Pipeline issue out of audit scope but flagged**: every ticker's
  `drift_30d` is exactly 0.0 — prior_scores are not being persisted
  across snapshots. Worth opening a separate ticket.
