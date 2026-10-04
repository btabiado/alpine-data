# LTHCS composite-score distribution — 2026-09-01

Snapshot file: `data/lthcs/snapshots/2026-09-01.json` (latest available; today is 2026-09-01).  Universe size: **215**.

## Distribution summary

- mean: **49.13**   stdev: **7.43**
- min/max: **32.5 / 63.1**
- p5/p25/p50/p75/p95: **37.67 / 42.15 / 50.1 / 55.45 / 60.06**

## Histogram (10-point bins)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  |                                          0
 30-39  | #############                            32
 40-49  | ###############################          74
 50-59  | ######################################## 97
 60-69  | #####                                    12
 70-79  |                                          0
 80-89  |                                          0
 90-100 |                                          0
```

## Band cohorts vs documented thresholds

| band | range | count | share |
|---|---|---|---|
| review | 0-49 | 106 | 49.3% |
| weakening | 50-59 | 97 | 45.1% |
| monitor | 60-69 | 12 | 5.6% |
| constructive | 70-79 | 0 | 0.0% |
| high_confidence | 80-84 | 0 | 0.0% |
| elite | 85-100 | 0 | 0.0% |

**Starved bands (count=0):** constructive, high_confidence, elite.
**Over-populated bands (>=40% share):** review (106, 49.3%), weakening (97, 45.1%).

## Per-cohort distribution

| cohort | n | mean | stdev | p25 | p50 | p75 |
|---|---|---|---|---|---|---|
| financial | 8 | 52.95 | 2.48 | 51.6 | 53.05 | 54.17 |
| growth_compounder | 20 | 50.83 | 7.13 | 45.95 | 51.1 | 57.17 |
| mature_compounder | 63 | 50.29 | 7.66 | 43.8 | 50.5 | 57.3 |
| pre_profit_growth | 1 | 35.8 | 0.0 | 35.8 | 35.8 | 35.8 |
| recovery_rerating | 1 | 58.3 | 0.0 | 58.3 | 58.3 | 58.3 |
| recovery_stabilization | 3 | 49.13 | 3.87 | 46.95 | 49.8 | 51.65 |
| standard_compounder | 119 | 48.01 | 7.32 | 41.1 | 48.2 | 54.0 |

### financial (8)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  |                                          0
 30-39  |                                          0
 40-49  | ######                                   1
 50-59  | ######################################## 7
 60-69  |                                          0
 70-79  |                                          0
 80-89  |                                          0
 90-100 |                                          0
```

### growth_compounder (20)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  |                                          0
 30-39  | ########                                 2
 40-49  | ############################             7
 50-59  | ######################################## 10
 60-69  | ####                                     1
 70-79  |                                          0
 80-89  |                                          0
 90-100 |                                          0
```

### mature_compounder (63)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  |                                          0
 30-39  | #########                                7
 40-49  | ############################             21
 50-59  | ######################################## 30
 60-69  | #######                                  5
 70-79  |                                          0
 80-89  |                                          0
 90-100 |                                          0
```

### recovery_stabilization (3)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  |                                          0
 30-39  |                                          0
 40-49  | ######################################## 2
 50-59  | ####################                     1
 60-69  |                                          0
 70-79  |                                          0
 80-89  |                                          0
 90-100 |                                          0
```

### standard_compounder (119)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  |                                          0
 30-39  | ##################                       22
 40-49  | ####################################     43
 50-59  | ######################################## 48
 60-69  | #####                                    6
 70-79  |                                          0
 80-89  |                                          0
 90-100 |                                          0
```

## Top 5 / bottom 5 by composite

**Top 5**

| ticker | composite | band | maturity | adoption | inst | fin | thesis | des | flags |
|---|---|---|---|---|---|---|---|---|---|
| PANW | 63.1 | monitor | mature_compounder | 50.0 | 106.5 | 50.0 | 55.0 | 45.9 | sec_unavailable,thesis_unavailable |
| ADP | 63.0 | monitor | mature_compounder | 57.9 | 93.0 | 50.0 | 50.0 | 51.1 | sec_unavailable,thesis_unavailable |
| MET | 62.6 | monitor | standard_compounder | 50.0 | 83.0 | 50.0 | 55.0 | 67.4 | sec_unavailable,thesis_unavailable |
| TRV | 61.8 | monitor | mature_compounder | 50.0 | 79.7 | 50.0 | 58.8 | 67.4 | sec_unavailable,thesis_unavailable |
| V | 61.6 | monitor | mature_compounder | 50.0 | 79.2 | 50.0 | 50.0 | 67.4 | sec_unavailable,thesis_unavailable |

**Bottom 5**

| ticker | composite | band | maturity | adoption | inst | fin | thesis | des | flags |
|---|---|---|---|---|---|---|---|---|---|
| MNST | 32.5 | review | standard_compounder | 50.0 | -3.5 | 50.0 | 50.0 | 45.3 | sec_unavailable,thesis_unavailable |
| AZN | 33.0 | review | standard_compounder | 38.5 | 2.6 | 50.0 | 55.0 | 43.7 | sec_unavailable,thesis_unavailable |
| TTD | 33.1 | review | standard_compounder | 50.0 | -3.0 | 50.0 | 41.2 | 47.6 | sec_unavailable,thesis_unavailable |
| GLW | 35.1 | review | standard_compounder | 50.0 | 6.7 | 50.0 | 55.0 | 45.9 | sec_unavailable,thesis_unavailable |
| LCID | 35.8 | review | pre_profit_growth | 50.0 | 6.4 | 50.0 | 41.2 | 48.4 | sec_unavailable,thesis_unavailable |

## Pillar-vs-peer-group z-score outliers (|z| >= 2.0)

Grouping: `des` is bucketed by **sector** (Phase 3 hotfix — DES is sector-driven; per-cohort grouping clustered Financials as 6/10 outliers). All other pillars remain bucketed by **maturity_stage**. Buckets of size <3 fall back to a universe-wide baseline; the `cohort` column shows which bucket was actually used (`_universe` = fallback).

| ticker | cohort | pillar | value | cohort_mean | cohort_sd | z | composite | flags |
|---|---|---|---|---|---|---|---|---|
| ABNB | standard_compounder | adoption_momentum | 35.0 | 49.7 | 2.55 | -5.76 | 54.4 | sec_unavailable,thesis_unavailable |
| AMGN | standard_compounder | adoption_momentum | 36.8 | 49.7 | 2.55 | -5.05 | 52.9 | sec_unavailable,thesis_unavailable |
| AMZN | mature_compounder | adoption_momentum | 65.0 | 50.48 | 2.92 | 4.98 | 50.2 | sec_unavailable,thesis_unavailable |
| AZN | standard_compounder | adoption_momentum | 38.5 | 49.7 | 2.55 | -4.39 | 33.0 | sec_unavailable,thesis_unavailable |
| AAPL | mature_compounder | adoption_momentum | 63.2 | 50.48 | 2.92 | 4.36 | 57.4 | sec_unavailable,thesis_unavailable |
| AEP | standard_compounder | adoption_momentum | 59.7 | 49.7 | 2.55 | 3.92 | 42.2 | sec_unavailable,thesis_unavailable |
| AMD | growth_compounder | adoption_momentum | 61.5 | 50.27 | 2.91 | 3.86 | 60.8 | sec_unavailable,thesis_unavailable |
| ADI | standard_compounder | adoption_momentum | 40.3 | 49.7 | 2.55 | -3.68 | 37.6 | sec_unavailable,thesis_unavailable |
| META | mature_compounder | thesis_integrity | 41.2 | 54.66 | 3.96 | -3.4 | 39.2 | sec_unavailable,thesis_unavailable |
| GEV | growth_compounder | thesis_integrity | 41.2 | 55.71 | 4.33 | -3.35 | 37.7 | sec_unavailable,thesis_unavailable |

## Stuck tickers (|drift_30d| < 5.0)

Stuck count: **127 / 215**

| ticker | composite | band | drift_30d | drift_90d | maturity | flags |
|---|---|---|---|---|---|---|
| SBUX | 49.6 | review | 0.0 | 8.0 | standard_compounder | sec_unavailable,thesis_unavailable |
| CB | 52.1 | weakening | 0.0 | 0.0 | financial | sec_unavailable,thesis_unavailable |
| WM | 44.3 | review | 0.0 | -10.7 | mature_compounder | sec_unavailable,thesis_unavailable |
| AAPL | 57.4 | weakening | 0.1 | -2.5 | mature_compounder | sec_unavailable,thesis_unavailable |
| EXC | 40.0 | review | -0.1 | -8.9 | standard_compounder | sec_unavailable,thesis_unavailable |
| KHC | 57.2 | weakening | -0.1 | 15.9 | standard_compounder | sec_unavailable,thesis_unavailable |
| LOW | 39.0 | review | 0.1 | 1.7 | standard_compounder | sec_unavailable,thesis_unavailable |
| PH | 47.8 | review | -0.1 | 16.2 | mature_compounder | sec_unavailable,thesis_unavailable |
| BKR | 47.2 | review | -0.2 | -6.7 | standard_compounder | sec_unavailable,thesis_unavailable |
| LIN | 43.7 | review | -0.2 | -9.3 | standard_compounder | sec_unavailable,thesis_unavailable |
| SMCI | 55.7 | weakening | 0.2 | -4.1 | growth_compounder | sec_unavailable,thesis_unavailable |
| WFC | 54.4 | weakening | 0.2 | 23.8 | mature_compounder | sec_unavailable,thesis_unavailable |
| ISRG | 36.8 | review | 0.3 | -18.1 | mature_compounder | sec_unavailable,thesis_unavailable |
| LCID | 35.8 | review | 0.3 | -1.7 | pre_profit_growth | sec_unavailable,thesis_unavailable |
| MU | 58.1 | weakening | 0.3 | -22.8 | growth_compounder | sec_unavailable,thesis_unavailable |
| CCEP | 53.2 | weakening | -0.4 | 4.2 | standard_compounder | sec_unavailable,thesis_unavailable |
| COST | 40.1 | review | -0.4 | -11.9 | mature_compounder | sec_unavailable,thesis_unavailable |
| ILMN | 59.2 | weakening | 0.4 | 18.3 | standard_compounder | sec_unavailable,thesis_unavailable |
| LULU | 38.6 | review | 0.4 | 4.1 | standard_compounder | sec_unavailable,thesis_unavailable |
| MSFT | 55.3 | weakening | 0.4 | -7.2 | mature_compounder | sec_unavailable,thesis_unavailable |
| NVDA | 49.8 | review | -0.5 | -29.2 | growth_compounder | sec_unavailable,thesis_unavailable |
| FTNT | 61.0 | monitor | 0.6 | -7.5 | mature_compounder | sec_unavailable,thesis_unavailable |
| BSX | 38.6 | review | 0.6 | -9.6 | standard_compounder | sec_unavailable,thesis_unavailable |
| VLO | 60.2 | monitor | 0.6 | -0.3 | standard_compounder | sec_unavailable,thesis_unavailable |
| PDD | 38.1 | review | -0.7 | -18.0 | standard_compounder | sec_unavailable,thesis_unavailable |

