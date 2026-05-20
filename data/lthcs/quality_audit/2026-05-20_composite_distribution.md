# LTHCS composite-score distribution — 2026-05-20

Snapshot file: `data/lthcs/snapshots/2026-05-20.json` (latest available; today is 2026-05-20).  Universe size: **167**.

## Distribution summary

- mean: **49.35**   stdev: **11.71**
- min/max: **19.2 / 78.6**
- p5/p25/p50/p75/p95: **29.22 / 41.1 / 50.1 / 56.6 / 67.96**

## Histogram (10-point bins)

```
  0-9   |                                          0
 10-19  | #                                        1
 20-29  | ########                                 10
 30-39  | ####################                     26
 40-49  | ###################################      46
 50-59  | ######################################## 52
 60-69  | ####################                     26
 70-79  | #####                                    6
 80-89  |                                          0
 90-100 |                                          0
```

## Band cohorts vs documented thresholds

| band | range | count | share |
|---|---|---|---|
| review | 0-49 | 83 | 49.7% |
| weakening | 50-59 | 52 | 31.1% |
| monitor | 60-69 | 26 | 15.6% |
| constructive | 70-79 | 6 | 3.6% |
| high_confidence | 80-84 | 0 | 0.0% |
| elite | 85-100 | 0 | 0.0% |

**Starved bands (count=0):** high_confidence, elite.
**Over-populated bands (>=40% share):** review (83, 49.7%).

## Per-cohort distribution

| cohort | n | mean | stdev | p25 | p50 | p75 |
|---|---|---|---|---|---|---|
| growth_compounder | 14 | 53.21 | 14.97 | 47.6 | 52.2 | 63.38 |
| mature_compounder | 49 | 50.0 | 11.12 | 40.9 | 51.2 | 56.3 |
| pre_profit_growth | 1 | 35.9 | 0.0 | 35.9 | 35.9 | 35.9 |
| recovery_rerating | 1 | 50.9 | 0.0 | 50.9 | 50.9 | 50.9 |
| recovery_stabilization | 2 | 47.1 | 8.9 | 42.65 | 47.1 | 51.55 |
| standard_compounder | 100 | 48.65 | 11.43 | 41.98 | 49.15 | 56.55 |

### growth_compounder (14)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  | ####################                     2
 30-39  | ##########                               1
 40-49  | ####################                     2
 50-59  | ######################################## 4
 60-69  | ##############################           3
 70-79  | ####################                     2
 80-89  |                                          0
 90-100 |                                          0
```

### mature_compounder (49)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  | ######                                   3
 30-39  | ############                             6
 40-49  | ######################                   11
 50-59  | ######################################## 20
 60-69  | ################                         8
 70-79  | ##                                       1
 80-89  |                                          0
 90-100 |                                          0
```

### standard_compounder (100)

```
  0-9   |                                          0
 10-19  | #                                        1
 20-29  | ######                                   5
 30-39  | #####################                    17
 40-49  | ######################################## 33
 50-59  | ################################         26
 60-69  | ##################                       15
 70-79  | ####                                     3
 80-89  |                                          0
 90-100 |                                          0
```

## Top 5 / bottom 5 by composite

**Top 5**

| ticker | composite | band | maturity | adoption | inst | fin | thesis | des | flags |
|---|---|---|---|---|---|---|---|---|---|
| FANG | 78.6 | constructive | standard_compounder | 92.3 | 92.8 | 69.6 | 54.9 | 61.9 | thesis_unavailable |
| MU | 78.4 | constructive | growth_compounder | 93.0 | 99.4 | 93.6 | 58.8 | 47.8 | thesis_unavailable |
| AVGO | 74.8 | constructive | mature_compounder | 93.0 | 80.3 | 84.8 | 78.0 | 47.8 | - |
| ADI | 74.1 | constructive | standard_compounder | 83.9 | 91.6 | 85.9 | 76.0 | 43.8 | - |
| NVDA | 74.1 | constructive | growth_compounder | 93.0 | 76.8 | 84.5 | 55.0 | 47.8 | thesis_unavailable |

**Bottom 5**

| ticker | composite | band | maturity | adoption | inst | fin | thesis | des | flags |
|---|---|---|---|---|---|---|---|---|---|
| NKE | 19.2 | review | standard_compounder | 20.8 | 2.7 | 23.9 | 51.9 | 38.1 | thesis_unavailable |
| GM | 21.2 | review | standard_compounder | 14.7 | 21.4 | 19.7 | 58.8 | 38.1 | thesis_unavailable |
| HD | 25.9 | review | mature_compounder | 13.3 | 19.5 | 40.6 | 50.0 | 38.1 | thesis_unavailable |
| ZS | 26.5 | review | growth_compounder | 25.5 | 14.7 | 47.5 | 58.8 | 43.8 | thesis_unavailable |
| WBD | 27.0 | review | standard_compounder | 17.6 | 37.0 | 16.8 | 41.2 | 44.6 | thesis_unavailable |

## Pillar-vs-peer-group z-score outliers (|z| >= 2.0)

Grouping: `des` is bucketed by **sector** (Phase 3 hotfix — DES is sector-driven; per-cohort grouping clustered Financials as 6/10 outliers). All other pillars remain bucketed by **maturity_stage**. Buckets of size <3 fall back to a universe-wide baseline; the `cohort` column shows which bucket was actually used (`_universe` = fallback).

| ticker | cohort | pillar | value | cohort_mean | cohort_sd | z | composite | flags |
|---|---|---|---|---|---|---|---|---|
| GE | Industrials | des | 49.7 | 47.03 | 0.6 | 4.47 | 50.9 | thesis_unavailable |
| LCID | Consumer Discretionary | des | 51.9 | 39.56 | 3.95 | 3.12 | 35.9 | thesis_unavailable |
| TSLA | Consumer Discretionary | des | 50.6 | 39.56 | 3.95 | 2.8 | 27.4 | thesis_unavailable |
| USB | standard_compounder | financial_evolution | 15.8 | 54.75 | 17.09 | -2.28 | 42.6 | thesis_unavailable |
| DDOG | growth_compounder | thesis_integrity | 81.6 | 60.05 | 9.6 | 2.24 | 64.0 | - |
| WBD | standard_compounder | financial_evolution | 16.8 | 54.75 | 17.09 | -2.22 | 27.0 | thesis_unavailable |
| DXCM | standard_compounder | thesis_integrity | 82.1 | 58.88 | 10.75 | 2.16 | 63.5 | - |
| MU | growth_compounder | financial_evolution | 93.6 | 59.09 | 16.59 | 2.08 | 78.4 | thesis_unavailable |
| BA | _universe | thesis_integrity | 81.0 | 59.2 | 10.5 | 2.08 | 56.0 | - |
| GM | standard_compounder | financial_evolution | 19.7 | 54.75 | 17.09 | -2.05 | 21.2 | thesis_unavailable |

## Stuck tickers (|drift_30d| < 5.0)

Stuck count: **94 / 167**

| ticker | composite | band | drift_30d | drift_90d | maturity | flags |
|---|---|---|---|---|---|---|
| ADP | 51.5 | weakening | 0.0 | -1.6 | mature_compounder | - |
| ADSK | 60.4 | monitor | -0.2 | 5.9 | standard_compounder | - |
| COP | 63.7 | monitor | 0.2 | 4.3 | standard_compounder | - |
| EMR | 43.5 | review | -0.4 | -7.9 | standard_compounder | - |
| NFLX | 62.7 | monitor | -0.4 | 7.0 | standard_compounder | thesis_unavailable |
| TRV | 51.5 | weakening | -0.4 | -4.0 | mature_compounder | thesis_unavailable |
| ZS | 26.5 | review | 0.4 | -9.8 | growth_compounder | thesis_unavailable |
| DHR | 40.0 | review | -0.5 | -10.1 | standard_compounder | - |
| VRSK | 44.6 | review | -0.5 | -2.5 | standard_compounder | thesis_unavailable |
| GD | 56.7 | weakening | -0.6 | -3.5 | standard_compounder | thesis_unavailable |
| MRVL | 68.2 | monitor | -0.6 | 35.1 | growth_compounder | thesis_unavailable |
| QCOM | 50.8 | weakening | 0.6 | -4.3 | mature_compounder | thesis_unavailable |
| KO | 40.9 | review | -0.8 | -8.9 | mature_compounder | thesis_unavailable |
| MCHP | 47.1 | review | -0.8 | 1.6 | standard_compounder | thesis_unavailable |
| NOW | 50.5 | weakening | -0.8 | -4.8 | mature_compounder | thesis_unavailable |
| TMUS | 57.6 | weakening | -0.8 | -1.1 | standard_compounder | thesis_unavailable |
| UNP | 46.5 | review | -0.8 | -4.8 | standard_compounder | thesis_unavailable |
| AAPL | 55.0 | weakening | 0.9 | -2.0 | mature_compounder | - |
| ISRG | 53.8 | weakening | -0.9 | -9.0 | mature_compounder | thesis_unavailable |
| DE | 39.5 | review | -1.0 | -12.0 | standard_compounder | - |
| FANG | 78.6 | constructive | 1.0 | 6.2 | standard_compounder | thesis_unavailable |
| LRCX | 52.1 | weakening | -1.0 | -4.2 | growth_compounder | thesis_unavailable |
| NKE | 19.2 | review | -1.0 | -16.5 | standard_compounder | thesis_unavailable |
| CSGP | 56.5 | weakening | 1.2 | -0.1 | standard_compounder | - |
| AMAT | 55.8 | weakening | -1.3 | -0.2 | mature_compounder | - |

