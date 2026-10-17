# LTHCS composite-score distribution — 2026-10-16

Snapshot file: `data/lthcs/snapshots/2026-10-16.json` (latest available; today is 2026-10-16).  Universe size: **515**.

## Distribution summary

- mean: **51.15**   stdev: **11.89**
- min/max: **20.4 / 83.8**
- p5/p25/p50/p75/p95: **31.78 / 43.7 / 50.8 / 59.85 / 69.53**

## Histogram (10-point bins)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  | ######                                   22
 30-39  | ##################                       69
 40-49  | ######################################## 151
 50-59  | ######################################   145
 60-69  | ###########################              103
 70-79  | ######                                   22
 80-89  | #                                        3
 90-100 |                                          0
```

## Band cohorts vs documented thresholds

| band | range | count | share |
|---|---|---|---|
| review | 0-32 | 37 | 7.2% |
| weakening | 33-41 | 72 | 14.0% |
| monitor | 42-51 | 167 | 32.4% |
| constructive | 52-62 | 145 | 28.2% |
| high_confidence | 63-69 | 69 | 13.4% |
| elite | 70-100 | 25 | 4.9% |

Band populations look balanced — no starved or runaway band.

## Per-cohort distribution

| cohort | n | mean | stdev | p25 | p50 | p75 |
|---|---|---|---|---|---|---|
| financial | 56 | 59.08 | 10.52 | 50.12 | 59.1 | 67.05 |
| growth_compounder | 56 | 48.41 | 12.57 | 41.0 | 47.35 | 55.8 |
| mature_compounder | 73 | 53.1 | 10.14 | 46.2 | 51.6 | 60.7 |
| pre_profit_growth | 2 | 68.95 | 0.45 | 68.72 | 68.95 | 69.18 |
| recovery_rerating | 4 | 49.3 | 14.12 | 35.73 | 48.9 | 62.48 |
| recovery_stabilization | 24 | 49.08 | 11.16 | 42.12 | 49.1 | 55.8 |
| standard_compounder | 300 | 49.78 | 11.68 | 42.1 | 50.1 | 58.4 |

### financial (56)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  |                                          0
 30-39  |                                          0
 40-49  | #################################        14
 50-59  | ###################################      15
 60-69  | ######################################## 17
 70-79  | ########################                 10
 80-89  |                                          0
 90-100 |                                          0
```

### growth_compounder (56)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  | #######                                  4
 30-39  | ##############                           8
 40-49  | ######################################## 23
 50-59  | ################                         9
 60-69  | ################                         9
 70-79  | #####                                    3
 80-89  |                                          0
 90-100 |                                          0
```

### mature_compounder (73)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  |                                          0
 30-39  | ##########                               6
 40-49  | #####################################    23
 50-59  | ######################################## 25
 60-69  | ########################                 15
 70-79  | ###                                      2
 80-89  | ###                                      2
 90-100 |                                          0
```

### recovery_rerating (4)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  |                                          0
 30-39  | ######################################## 2
 40-49  |                                          0
 50-59  |                                          0
 60-69  | ######################################## 2
 70-79  |                                          0
 80-89  |                                          0
 90-100 |                                          0
```

### recovery_stabilization (24)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  | #####                                    1
 30-39  | ####################                     4
 40-49  | ######################################## 8
 50-59  | ##############################           6
 60-69  | #########################                5
 70-79  |                                          0
 80-89  |                                          0
 90-100 |                                          0
```

### standard_compounder (300)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  | ########                                 17
 30-39  | ######################                   49
 40-49  | #####################################    83
 50-59  | ######################################## 90
 60-69  | ########################                 53
 70-79  | ###                                      7
 80-89  |                                          1
 90-100 |                                          0
```

## Top 5 / bottom 5 by composite

**Top 5**

| ticker | composite | band | maturity | adoption | inst | fin | thesis | des | flags |
|---|---|---|---|---|---|---|---|---|---|
| MA | 83.8 | elite | mature_compounder | 88.5 | 93.6 | 79.3 | None | 73.7 | thesis_unavailable |
| V | 81.1 | elite | mature_compounder | 81.0 | 88.1 | 81.5 | None | 73.7 | thesis_unavailable |
| COF | 80.4 | elite | standard_compounder | 96.9 | 74.0 | 70.5 | None | 73.7 | thesis_unavailable |
| ACGL | 77.9 | elite | financial | 78.6 | 68.5 | 88.4 | None | 73.7 | thesis_unavailable |
| ARES | 77.2 | elite | financial | 85.9 | 47.0 | 98.9 | None | 73.7 | thesis_unavailable |

**Bottom 5**

| ticker | composite | band | maturity | adoption | inst | fin | thesis | des | flags |
|---|---|---|---|---|---|---|---|---|---|
| FLEX | 20.4 | review | growth_compounder | 22.9 | 0.0 | 31.8 | None | 41.2 | thesis_unavailable |
| LII | 21.3 | review | standard_compounder | 16.6 | 2.5 | 20.6 | None | 46.4 | thesis_unavailable |
| FIX | 22.6 | review | growth_compounder | 25.0 | 2.9 | 29.1 | None | 46.4 | thesis_unavailable |
| WY | 24.5 | review | standard_compounder | 25.8 | 13.0 | 30.7 | None | 29.7 | thesis_unavailable |
| PCG | 24.8 | review | standard_compounder | 10.4 | 11.0 | 57.7 | None | 31.8 | thesis_unavailable |

## Pillar-vs-peer-group z-score outliers (|z| >= 2.0)

Grouping: `des` is bucketed by **sector** (Phase 3 hotfix — DES is sector-driven; per-cohort grouping clustered Financials as 6/10 outliers). All other pillars remain bucketed by **maturity_stage**. Buckets of size <3 fall back to a universe-wide baseline; the `cohort` column shows which bucket was actually used (`_universe` = fallback).

| ticker | cohort | pillar | value | cohort_mean | cohort_sd | z | composite | flags |
|---|---|---|---|---|---|---|---|---|
| AMAT | Technology | des | 45.8 | 41.72 | 1.46 | 2.79 | 34.8 | thesis_unavailable |
| AMD | Technology | des | 45.8 | 41.72 | 1.46 | 2.79 | 66.7 | thesis_unavailable |
| AVGO | Technology | des | 45.8 | 41.72 | 1.46 | 2.79 | 59.9 | thesis_unavailable |
| KLAC | Technology | des | 45.8 | 41.72 | 1.46 | 2.79 | 35.5 | thesis_unavailable |
| LRCX | Technology | des | 45.8 | 41.72 | 1.46 | 2.79 | 43.9 | thesis_unavailable |
| MSFT | Technology | des | 45.8 | 41.72 | 1.46 | 2.79 | 72.7 | thesis_unavailable |
| MU | Technology | des | 45.8 | 41.72 | 1.46 | 2.79 | 63.5 | thesis_unavailable |
| NVDA | Technology | des | 45.8 | 41.72 | 1.46 | 2.79 | 69.4 | thesis_unavailable |
| ORCL | Technology | des | 45.8 | 41.72 | 1.46 | 2.79 | 51.0 | thesis_unavailable |
| EXE | standard_compounder | financial_evolution | 100.0 | 53.95 | 17.21 | 2.68 | 63.5 | thesis_unavailable |

## Stuck tickers (|drift_30d| < 5.0)

Stuck count: **410 / 515**

| ticker | composite | band | drift_30d | drift_90d | maturity | flags |
|---|---|---|---|---|---|---|
| A | 60.2 | constructive | 0.0 | 0.0 | standard_compounder | thesis_unavailable |
| ACGL | 77.9 | elite | 0.0 | 0.0 | financial | thesis_unavailable |
| ADM | 48.1 | monitor | 0.0 | 0.0 | mature_compounder | thesis_unavailable |
| AEE | 51.6 | monitor | 0.0 | 0.0 | standard_compounder | thesis_unavailable |
| AES | 38.0 | weakening | 0.0 | 0.0 | standard_compounder | thesis_unavailable |
| AFL | 45.3 | monitor | 0.0 | 0.0 | financial | thesis_unavailable |
| AIG | 46.6 | monitor | 0.0 | 0.0 | financial | thesis_unavailable |
| AIZ | 65.2 | high_confidence | 0.0 | 0.0 | financial | thesis_unavailable |
| AJG | 77.0 | elite | 0.0 | 0.0 | financial | thesis_unavailable |
| AKAM | 32.2 | review | 0.0 | 0.0 | standard_compounder | thesis_unavailable |
| ALB | 36.2 | weakening | 0.0 | 0.0 | recovery_rerating | thesis_unavailable |
| ALGN | 32.9 | review | 0.0 | 0.0 | growth_compounder | thesis_unavailable |
| ALL | 58.8 | constructive | 0.0 | 0.0 | financial | thesis_unavailable |
| ALLE | 55.8 | constructive | 0.0 | 0.0 | standard_compounder | thesis_unavailable |
| AMCR | 53.6 | constructive | 0.0 | 0.0 | growth_compounder | thesis_unavailable |
| AME | 59.0 | constructive | 0.0 | 0.0 | standard_compounder | thesis_unavailable |
| AMP | 61.0 | constructive | 0.0 | 0.0 | financial | thesis_unavailable |
| AON | 53.2 | constructive | 0.0 | 0.0 | financial | thesis_unavailable |
| AOS | 43.1 | monitor | 0.0 | 0.0 | standard_compounder | thesis_unavailable |
| APA | 60.6 | constructive | 0.0 | 0.0 | standard_compounder | sec_unavailable,thesis_unavailable |
| APD | 45.0 | monitor | 0.0 | 0.0 | mature_compounder | thesis_unavailable |
| APO | 62.9 | constructive | 0.0 | 0.0 | financial | thesis_unavailable |
| APP | 64.9 | high_confidence | 0.0 | 0.0 | growth_compounder | thesis_unavailable |
| APTV | 29.8 | review | 0.0 | 0.0 | standard_compounder | thesis_unavailable |
| ARE | 35.0 | weakening | 0.0 | 0.0 | standard_compounder | sec_unavailable,thesis_unavailable |

