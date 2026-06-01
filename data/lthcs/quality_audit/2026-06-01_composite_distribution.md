# LTHCS composite-score distribution — 2026-06-01

Snapshot file: `data/lthcs/snapshots/2026-05-31.json` (latest available; today is 2026-06-01).  Universe size: **209**.

## Distribution summary

- mean: **51.01**   stdev: **11.52**
- min/max: **19.1 / 79.9**
- p5/p25/p50/p75/p95: **32.0 / 42.7 / 50.6 / 58.8 / 71.56**

## Histogram (10-point bins)

```
  0-9   |                                          0
 10-19  | #                                        1
 20-29  | ##                                       4
 30-39  | #####################                    34
 40-49  | ####################################     59
 50-59  | ######################################## 66
 60-69  | ###################                      32
 70-79  | ########                                 13
 80-89  |                                          0
 90-100 |                                          0
```

## Band cohorts vs documented thresholds

| band | range | count | share |
|---|---|---|---|
| review | 0-49 | 98 | 46.9% |
| weakening | 50-59 | 66 | 31.6% |
| monitor | 60-69 | 32 | 15.3% |
| constructive | 70-79 | 13 | 6.2% |
| high_confidence | 80-84 | 0 | 0.0% |
| elite | 85-100 | 0 | 0.0% |

**Starved bands (count=0):** high_confidence, elite.
**Over-populated bands (>=40% share):** review (98, 46.9%).

## Per-cohort distribution

| cohort | n | mean | stdev | p25 | p50 | p75 |
|---|---|---|---|---|---|---|
| growth_compounder | 20 | 56.58 | 13.96 | 49.42 | 57.15 | 66.6 |
| mature_compounder | 63 | 50.8 | 11.56 | 41.85 | 50.5 | 59.4 |
| pre_profit_growth | 1 | 34.8 | 0.0 | 34.8 | 34.8 | 34.8 |
| recovery_rerating | 1 | 62.6 | 0.0 | 62.6 | 62.6 | 62.6 |
| recovery_stabilization | 3 | 57.9 | 12.93 | 49.45 | 55.5 | 65.15 |
| standard_compounder | 121 | 50.07 | 10.63 | 43.2 | 49.7 | 57.8 |

### growth_compounder (20)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  | #######                                  1
 30-39  | #############                            2
 40-49  | #############                            2
 50-59  | ######################################## 6
 60-69  | ######################################## 6
 70-79  | ####################                     3
 80-89  |                                          0
 90-100 |                                          0
```

### mature_compounder (63)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  | ####                                     2
 30-39  | #######################                  12
 40-49  | ###########################              14
 50-59  | ######################################## 21
 60-69  | #################                        9
 70-79  | ##########                               5
 80-89  |                                          0
 90-100 |                                          0
```

### recovery_stabilization (3)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  |                                          0
 30-39  |                                          0
 40-49  | ######################################## 1
 50-59  | ######################################## 1
 60-69  |                                          0
 70-79  | ######################################## 1
 80-89  |                                          0
 90-100 |                                          0
```

### standard_compounder (121)

```
  0-9   |                                          0
 10-19  | #                                        1
 20-29  | #                                        1
 30-39  | ##################                       19
 40-49  | ######################################## 42
 50-59  | ####################################     38
 60-69  | ###############                          16
 70-79  | ####                                     4
 80-89  |                                          0
 90-100 |                                          0
```

## Top 5 / bottom 5 by composite

**Top 5**

| ticker | composite | band | maturity | adoption | inst | fin | thesis | des | flags |
|---|---|---|---|---|---|---|---|---|---|
| MU | 79.9 | constructive | growth_compounder | 94.2 | 96.1 | 94.5 | 58.8 | 46.9 | thesis_unavailable |
| AVGO | 77.6 | constructive | mature_compounder | 93.0 | 82.6 | 84.8 | 80.7 | 46.9 | - |
| NVDA | 77.1 | constructive | growth_compounder | 93.0 | 71.4 | 98.3 | 55.0 | 46.9 | thesis_unavailable |
| STX | 76.3 | constructive | standard_compounder | 90.8 | 95.5 | 69.2 | 58.8 | 44.5 | thesis_unavailable |
| WDC | 74.8 | constructive | recovery_stabilization | 92.6 | 95.1 | 71.5 | 58.8 | 44.5 | thesis_unavailable |

**Bottom 5**

| ticker | composite | band | maturity | adoption | inst | fin | thesis | des | flags |
|---|---|---|---|---|---|---|---|---|---|
| NKE | 19.1 | review | standard_compounder | 12.8 | 4.6 | 23.3 | 51.9 | 38.5 | thesis_unavailable |
| WBD | 26.4 | review | standard_compounder | 16.9 | 27.0 | 16.4 | 46.1 | 45.0 | thesis_unavailable |
| ZS | 28.2 | review | growth_compounder | 30.9 | -1.7 | 57.9 | 55.0 | 44.5 | thesis_unavailable |
| PEP | 29.6 | review | mature_compounder | 16.3 | 30.7 | 28.9 | 55.0 | 42.4 | thesis_unavailable |
| WFC | 29.9 | review | mature_compounder | 23.1 | 19.2 | 19.4 | 50.0 | 58.1 | thesis_unavailable |

## Pillar-vs-peer-group z-score outliers (|z| >= 2.0)

Grouping: `des` is bucketed by **sector** (Phase 3 hotfix — DES is sector-driven; per-cohort grouping clustered Financials as 6/10 outliers). All other pillars remain bucketed by **maturity_stage**. Buckets of size <3 fall back to a universe-wide baseline; the `cohort` column shows which bucket was actually used (`_universe` = fallback).

| ticker | cohort | pillar | value | cohort_mean | cohort_sd | z | composite | flags |
|---|---|---|---|---|---|---|---|---|
| GE | Industrials | des | 45.6 | 43.95 | 0.28 | 5.92 | 62.6 | thesis_unavailable |
| LCID | Consumer Discretionary | des | 47.0 | 39.31 | 2.32 | 3.31 | 34.8 | thesis_unavailable |
| TSLA | Consumer Discretionary | des | 46.1 | 39.31 | 2.32 | 2.93 | 31.4 | thesis_unavailable |
| DXCM | standard_compounder | thesis_integrity | 82.1 | 58.13 | 9.42 | 2.54 | 64.1 | - |
| DDOG | growth_compounder | thesis_integrity | 81.6 | 58.57 | 9.3 | 2.48 | 67.8 | - |
| AMZN | mature_compounder | thesis_integrity | 81.3 | 58.33 | 9.55 | 2.4 | 68.9 | - |
| AVGO | mature_compounder | thesis_integrity | 80.7 | 58.33 | 9.55 | 2.34 | 77.6 | - |
| USB | standard_compounder | financial_evolution | 15.8 | 54.57 | 17.1 | -2.27 | 40.6 | thesis_unavailable |
| C | mature_compounder | thesis_integrity | 80.0 | 58.33 | 9.55 | 2.27 | 56.7 | - |
| WELL | standard_compounder | adoption_momentum | 99.4 | 49.94 | 21.87 | 2.26 | 72.4 | thesis_unavailable |

## Stuck tickers (|drift_30d| < 5.0)

Stuck count: **130 / 209**

| ticker | composite | band | drift_30d | drift_90d | maturity | flags |
|---|---|---|---|---|---|---|
| ANET | 66.2 | monitor | 0.0 | 0.0 | growth_compounder | thesis_unavailable |
| APH | 61.6 | monitor | 0.0 | 0.0 | standard_compounder | thesis_unavailable |
| BSX | 50.6 | weakening | 0.0 | 0.0 | standard_compounder | thesis_unavailable |
| CI | 48.9 | review | 0.0 | 0.0 | mature_compounder | thesis_unavailable |
| CMI | 41.7 | review | 0.0 | 0.0 | standard_compounder | thesis_unavailable |
| CSX | 50.3 | weakening | 0.0 | 0.0 | mature_compounder | thesis_unavailable |
| ECL | 37.3 | review | 0.0 | 0.0 | mature_compounder | thesis_unavailable |
| ELV | 57.5 | weakening | 0.0 | 0.0 | mature_compounder | thesis_unavailable |
| EOG | 54.2 | weakening | 0.0 | 0.0 | standard_compounder | thesis_unavailable |
| EQIX | 58.2 | weakening | 0.0 | 0.0 | standard_compounder | thesis_unavailable |
| ETN | 66.1 | monitor | 0.0 | 0.0 | standard_compounder | thesis_unavailable |
| FCX | 41.6 | review | 0.0 | 0.0 | standard_compounder | thesis_unavailable |
| FDX | 48.4 | review | 0.0 | 0.0 | mature_compounder | thesis_unavailable |
| GEV | 43.8 | review | 0.0 | 0.0 | growth_compounder | thesis_unavailable |
| GLW | 65.7 | monitor | 0.0 | 0.0 | standard_compounder | thesis_unavailable |
| HCA | 37.1 | review | 0.0 | 0.0 | mature_compounder | thesis_unavailable |
| HLT | 57.7 | weakening | 0.0 | 0.0 | standard_compounder | thesis_unavailable |
| HWM | 51.3 | weakening | 0.0 | 0.0 | growth_compounder | thesis_unavailable |
| ITW | 41.3 | review | 0.0 | 0.0 | mature_compounder | thesis_unavailable |
| JCI | 46.2 | review | 0.0 | 0.0 | standard_compounder | thesis_unavailable |
| KMI | 68.6 | monitor | 0.0 | 0.0 | standard_compounder | thesis_unavailable |
| MCK | 46.6 | review | 0.0 | 0.0 | mature_compounder | thesis_unavailable |
| MPC | 47.6 | review | 0.0 | 0.0 | standard_compounder | thesis_unavailable |
| MPWR | 61.7 | monitor | 0.0 | 0.0 | growth_compounder | thesis_unavailable |
| NEM | 64.2 | monitor | 0.0 | 0.0 | standard_compounder | thesis_unavailable |

