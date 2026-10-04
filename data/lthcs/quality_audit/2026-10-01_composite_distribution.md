# LTHCS composite-score distribution — 2026-10-01

Snapshot file: `data/lthcs/snapshots/2026-10-01.json` (latest available; today is 2026-10-01).  Universe size: **215**.

## Distribution summary

- mean: **46.29**   stdev: **8.25**
- min/max: **28.9 / 64.0**
- p5/p25/p50/p75/p95: **33.2 / 39.2 / 46.9 / 53.1 / 58.72**

## Histogram (10-point bins)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  |                                          1
 30-39  | ############################             57
 40-49  | ####################################     73
 50-59  | ######################################## 80
 60-69  | ##                                       4
 70-79  |                                          0
 80-89  |                                          0
 90-100 |                                          0
```

## Band cohorts vs documented thresholds

| band | range | count | share |
|---|---|---|---|
| review | 0-49 | 131 | 60.9% |
| weakening | 50-59 | 80 | 37.2% |
| monitor | 60-69 | 4 | 1.9% |
| constructive | 70-79 | 0 | 0.0% |
| high_confidence | 80-84 | 0 | 0.0% |
| elite | 85-100 | 0 | 0.0% |

**Starved bands (count=0):** constructive, high_confidence, elite.
**Over-populated bands (>=40% share):** review (131, 60.9%).

## Per-cohort distribution

| cohort | n | mean | stdev | p25 | p50 | p75 |
|---|---|---|---|---|---|---|
| financial | 8 | 53.66 | 2.57 | 52.4 | 54.0 | 55.83 |
| growth_compounder | 20 | 47.9 | 6.84 | 40.95 | 48.55 | 53.35 |
| mature_compounder | 63 | 47.83 | 8.04 | 41.6 | 49.1 | 53.9 |
| pre_profit_growth | 1 | 32.0 | 0.0 | 32.0 | 32.0 | 32.0 |
| recovery_rerating | 1 | 48.5 | 0.0 | 48.5 | 48.5 | 48.5 |
| recovery_stabilization | 3 | 42.6 | 1.76 | 41.5 | 42.5 | 43.65 |
| standard_compounder | 119 | 44.9 | 8.42 | 37.25 | 44.6 | 52.0 |

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
 30-39  | #########################                5
 40-49  | ###################################      7
 50-59  | ######################################## 8
 60-69  |                                          0
 70-79  |                                          0
 80-89  |                                          0
 90-100 |                                          0
```

### mature_compounder (63)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  |                                          0
 30-39  | #####################                    14
 40-49  | ############################             19
 50-59  | ######################################## 27
 60-69  | ####                                     3
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
 40-49  | ######################################## 3
 50-59  |                                          0
 60-69  |                                          0
 70-79  |                                          0
 80-89  |                                          0
 90-100 |                                          0
```

### standard_compounder (119)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  | #                                        1
 30-39  | ###################################      37
 40-49  | ######################################## 42
 50-59  | ####################################     38
 60-69  | #                                        1
 70-79  |                                          0
 80-89  |                                          0
 90-100 |                                          0
```

## Top 5 / bottom 5 by composite

**Top 5**

| ticker | composite | band | maturity | adoption | inst | fin | thesis | des | flags |
|---|---|---|---|---|---|---|---|---|---|
| PYPL | 64.0 | monitor | mature_compounder | 50.0 | 85.0 | 50.0 | 55.0 | 79.1 | sec_unavailable,thesis_unavailable |
| TRV | 62.8 | monitor | mature_compounder | 50.0 | 80.2 | 50.0 | 58.8 | 79.1 | sec_unavailable,thesis_unavailable |
| MET | 61.2 | monitor | standard_compounder | 50.0 | 73.6 | 50.0 | 55.0 | 79.1 | sec_unavailable,thesis_unavailable |
| MA | 60.5 | monitor | mature_compounder | 50.0 | 70.8 | 50.0 | 55.0 | 79.1 | sec_unavailable,thesis_unavailable |
| JPM | 59.9 | weakening | mature_compounder | 50.0 | 68.5 | 50.0 | 55.0 | 79.1 | sec_unavailable,thesis_unavailable |

**Bottom 5**

| ticker | composite | band | maturity | adoption | inst | fin | thesis | des | flags |
|---|---|---|---|---|---|---|---|---|---|
| ON | 28.9 | review | standard_compounder | 50.0 | -4.6 | 50.0 | 55.0 | 40.2 | sec_unavailable,thesis_unavailable |
| AZN | 30.1 | review | standard_compounder | 35.4 | 7.3 | 50.0 | 55.0 | 39.2 | sec_unavailable,thesis_unavailable |
| GLW | 30.3 | review | standard_compounder | 50.0 | 1.1 | 50.0 | 55.0 | 40.2 | sec_unavailable,thesis_unavailable |
| LULU | 31.6 | review | standard_compounder | 50.0 | 6.8 | 50.0 | 58.8 | 39.6 | sec_unavailable,thesis_unavailable |
| LCID | 32.0 | review | pre_profit_growth | 50.0 | 5.9 | 50.0 | 41.2 | 39.6 | sec_unavailable,thesis_unavailable |

## Pillar-vs-peer-group z-score outliers (|z| >= 2.0)

Grouping: `des` is bucketed by **sector** (Phase 3 hotfix — DES is sector-driven; per-cohort grouping clustered Financials as 6/10 outliers). All other pillars remain bucketed by **maturity_stage**. Buckets of size <3 fall back to a universe-wide baseline; the `cohort` column shows which bucket was actually used (`_universe` = fallback).

| ticker | cohort | pillar | value | cohort_mean | cohort_sd | z | composite | flags |
|---|---|---|---|---|---|---|---|---|
| META | mature_compounder | thesis_integrity | 41.2 | 54.66 | 3.96 | -3.4 | 55.7 | sec_unavailable,thesis_unavailable |
| GEV | growth_compounder | thesis_integrity | 41.2 | 55.52 | 4.27 | -3.35 | 39.9 | sec_unavailable,thesis_unavailable |
| CCEP | standard_compounder | thesis_integrity | 41.2 | 54.61 | 4.26 | -3.15 | 50.2 | sec_unavailable,thesis_unavailable |
| TSLA | standard_compounder | thesis_integrity | 41.2 | 54.61 | 4.26 | -3.15 | 34.2 | sec_unavailable,thesis_unavailable |
| TTD | standard_compounder | thesis_integrity | 41.2 | 54.61 | 4.26 | -3.15 | 33.0 | sec_unavailable,thesis_unavailable |
| WELL | standard_compounder | thesis_integrity | 41.2 | 54.61 | 4.26 | -3.15 | 45.4 | sec_unavailable,thesis_unavailable |
| LCID | _universe | thesis_integrity | 41.2 | 54.63 | 4.31 | -3.11 | 32.0 | sec_unavailable,thesis_unavailable |
| AMD | growth_compounder | adoption_momentum | 61.9 | 51.28 | 3.48 | 3.06 | 57.8 | sec_unavailable,thesis_unavailable |
| ADP | mature_compounder | adoption_momentum | 35.0 | 50.13 | 5.31 | -2.85 | 51.8 | sec_unavailable,thesis_unavailable |
| BKNG | standard_compounder | adoption_momentum | 64.1 | 49.66 | 5.13 | 2.82 | 50.1 | sec_unavailable,thesis_unavailable |

## Stuck tickers (|drift_30d| < 5.0)

Stuck count: **128 / 215**

| ticker | composite | band | drift_30d | drift_90d | maturity | flags |
|---|---|---|---|---|---|---|
| CSGP | 40.9 | review | 0.1 | -2.8 | standard_compounder | sec_unavailable,thesis_unavailable |
| MSFT | 55.4 | weakening | 0.1 | 8.3 | mature_compounder | sec_unavailable,thesis_unavailable |
| PG | 46.4 | review | -0.1 | 4.8 | mature_compounder | sec_unavailable,thesis_unavailable |
| TTD | 33.0 | review | -0.1 | -5.2 | standard_compounder | sec_unavailable,thesis_unavailable |
| WDAY | 57.3 | weakening | -0.1 | 8.0 | standard_compounder | sec_unavailable,thesis_unavailable |
| KKR | 52.7 | weakening | 0.1 | 0.5 | financial | sec_unavailable,thesis_unavailable |
| BAC | 59.7 | weakening | -0.2 | -1.1 | mature_compounder | sec_unavailable,thesis_unavailable |
| GEHC | 46.2 | review | 0.4 | 6.3 | standard_compounder | sec_unavailable,thesis_unavailable |
| IDXX | 41.9 | review | 0.4 | 1.2 | standard_compounder | sec_unavailable,thesis_unavailable |
| BLK | 55.3 | weakening | 0.5 | 7.7 | standard_compounder | sec_unavailable,thesis_unavailable |
| XOM | 49.8 | review | -0.5 | 7.9 | standard_compounder | sec_unavailable,thesis_unavailable |
| CME | 48.5 | review | -0.5 | 6.8 | financial | sec_unavailable,thesis_unavailable |
| DIS | 46.8 | review | -0.6 | 2.4 | mature_compounder | sec_unavailable,thesis_unavailable |
| MU | 57.5 | weakening | -0.6 | -0.4 | growth_compounder | sec_unavailable,thesis_unavailable |
| WM | 43.7 | review | -0.6 | -4.1 | mature_compounder | sec_unavailable,thesis_unavailable |
| ITW | 49.1 | review | 0.7 | 5.5 | mature_compounder | sec_unavailable,thesis_unavailable |
| AMGN | 52.1 | weakening | -0.8 | 10.6 | standard_compounder | sec_unavailable,thesis_unavailable |
| CAT | 42.6 | review | -0.9 | -14.3 | standard_compounder | sec_unavailable,thesis_unavailable |
| COP | 49.6 | review | -0.9 | 5.3 | standard_compounder | sec_unavailable,thesis_unavailable |
| CTSH | 52.9 | weakening | 0.9 | 11.3 | standard_compounder | sec_unavailable,thesis_unavailable |
| MCO | 56.2 | weakening | 0.9 | -2.3 | financial | sec_unavailable,thesis_unavailable |
| ACN | 44.8 | review | 1.0 | 8.7 | standard_compounder | sec_unavailable,thesis_unavailable |
| CRWD | 56.2 | weakening | -1.0 | -2.3 | mature_compounder | sec_unavailable,thesis_unavailable |
| CVX | 51.9 | weakening | -1.0 | 6.5 | standard_compounder | sec_unavailable,thesis_unavailable |
| TRV | 62.8 | monitor | 1.0 | 4.8 | mature_compounder | sec_unavailable,thesis_unavailable |

