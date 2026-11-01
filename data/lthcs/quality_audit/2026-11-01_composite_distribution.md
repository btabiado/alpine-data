# LTHCS composite-score distribution — 2026-11-01

Snapshot file: `data/lthcs/snapshots/2026-11-01.json` (latest available; today is 2026-11-01).  Universe size: **514**.

## Distribution summary

- mean: **53.62**   stdev: **11.12**
- min/max: **24.0 / 82.5**
- p5/p25/p50/p75/p95: **35.77 / 46.23 / 53.6 / 61.48 / 72.3**

## Histogram (10-point bins)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  | ##                                       9
 30-39  | ###########                              48
 40-49  | ################################         135
 50-59  | ######################################## 171
 60-69  | #########################                105
 70-79  | ##########                               43
 80-89  | #                                        3
 90-100 |                                          0
```

## Band cohorts vs documented thresholds

| band | range | count | share |
|---|---|---|---|
| review | 0-32 | 18 | 3.5% |
| weakening | 33-41 | 58 | 11.3% |
| monitor | 42-51 | 155 | 30.2% |
| constructive | 52-62 | 183 | 35.6% |
| high_confidence | 63-69 | 54 | 10.5% |
| elite | 70-100 | 46 | 8.9% |

Band populations look balanced — no starved or runaway band.

## Per-cohort distribution

| cohort | n | mean | stdev | p25 | p50 | p75 |
|---|---|---|---|---|---|---|
| financial | 56 | 58.43 | 10.77 | 49.62 | 56.65 | 66.97 |
| growth_compounder | 56 | 49.97 | 11.61 | 40.27 | 50.55 | 57.35 |
| mature_compounder | 73 | 55.09 | 9.9 | 47.6 | 54.2 | 61.5 |
| pre_profit_growth | 2 | 61.25 | 4.85 | 58.82 | 61.25 | 63.67 |
| recovery_rerating | 4 | 50.55 | 9.25 | 42.67 | 50.65 | 58.52 |
| recovery_stabilization | 24 | 51.16 | 10.47 | 43.42 | 52.15 | 58.62 |
| standard_compounder | 299 | 53.23 | 11.09 | 45.85 | 53.6 | 61.3 |

### financial (56)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  |                                          0
 30-39  |                                          0
 40-49  | ###################################      15
 50-59  | ######################################## 17
 60-69  | ###############################          13
 70-79  | ########################                 10
 80-89  | ##                                       1
 90-100 |                                          0
```

### growth_compounder (56)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  | ###                                      2
 30-39  | ###################                      11
 40-49  | #####################                    12
 50-59  | ######################################## 23
 60-69  | #########                                5
 70-79  | ###                                      2
 80-89  | ##                                       1
 90-100 |                                          0
```

### mature_compounder (73)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  |                                          0
 30-39  | #####                                    3
 40-49  | #####################################    22
 50-59  | ######################################## 24
 60-69  | ##############################           18
 70-79  | ########                                 5
 80-89  | ##                                       1
 90-100 |                                          0
```

### recovery_rerating (4)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  |                                          0
 30-39  | ######################################## 1
 40-49  | ######################################## 1
 50-59  | ######################################## 1
 60-69  | ######################################## 1
 70-79  |                                          0
 80-89  |                                          0
 90-100 |                                          0
```

### recovery_stabilization (24)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  | ####                                     1
 30-39  | #########                                2
 40-49  | ###############################          7
 50-59  | ######################################## 9
 60-69  | ##################                       4
 70-79  | ####                                     1
 80-89  |                                          0
 90-100 |                                          0
```

### standard_compounder (299)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  | ##                                       6
 30-39  | #############                            31
 40-49  | ################################         78
 50-59  | ######################################## 96
 60-69  | ##########################               63
 70-79  | ##########                               25
 80-89  |                                          0
 90-100 |                                          0
```

## Top 5 / bottom 5 by composite

**Top 5**

| ticker | composite | band | maturity | adoption | inst | fin | thesis | des | flags |
|---|---|---|---|---|---|---|---|---|---|
| ARES | 82.5 | elite | financial | 85.9 | 69.9 | 98.9 | None | 64.1 | thesis_unavailable |
| MA | 80.9 | elite | mature_compounder | 83.7 | 89.3 | 78.5 | None | 64.1 | thesis_unavailable |
| PLTR | 80.9 | elite | growth_compounder | 88.8 | 97.5 | 82.1 | None | 45.6 | thesis_unavailable |
| V | 79.2 | elite | mature_compounder | 81.0 | 82.2 | 81.5 | None | 64.1 | thesis_unavailable |
| CF | 79.0 | elite | standard_compounder | 85.2 | 80.9 | 91.4 | None | 52.1 | thesis_unavailable |

**Bottom 5**

| ticker | composite | band | maturity | adoption | inst | fin | thesis | des | flags |
|---|---|---|---|---|---|---|---|---|---|
| FLEX | 24.0 | review | growth_compounder | 24.9 | 0.0 | 31.3 | None | 45.6 | thesis_unavailable |
| CARR | 24.6 | review | standard_compounder | 18.9 | 7.0 | 12.3 | None | 50.7 | thesis_unavailable |
| WY | 26.2 | review | standard_compounder | 17.4 | 10.9 | 30.4 | None | 41.2 | thesis_unavailable |
| LII | 27.3 | review | standard_compounder | 23.4 | 2.9 | 24.5 | None | 50.7 | thesis_unavailable |
| CHRW | 27.9 | review | standard_compounder | 26.2 | 5.8 | 35.0 | None | 50.7 | thesis_unavailable |

## Pillar-vs-peer-group z-score outliers (|z| >= 2.0)

Grouping: `des` is bucketed by **sector** (Phase 3 hotfix — DES is sector-driven; per-cohort grouping clustered Financials as 6/10 outliers). All other pillars remain bucketed by **maturity_stage**. Buckets of size <3 fall back to a universe-wide baseline; the `cohort` column shows which bucket was actually used (`_universe` = fallback).

| ticker | cohort | pillar | value | cohort_mean | cohort_sd | z | composite | flags |
|---|---|---|---|---|---|---|---|---|
| AMAT | Technology | des | 48.4 | 45.92 | 0.89 | 2.79 | 34.7 | thesis_unavailable |
| AMD | Technology | des | 48.4 | 45.92 | 0.89 | 2.79 | 58.2 | thesis_unavailable |
| AVGO | Technology | des | 48.4 | 45.92 | 0.89 | 2.79 | 62.8 | thesis_unavailable |
| KLAC | Technology | des | 48.4 | 45.92 | 0.89 | 2.79 | 39.1 | thesis_unavailable |
| LRCX | Technology | des | 48.4 | 45.92 | 0.89 | 2.79 | 47.2 | thesis_unavailable |
| MSFT | Technology | des | 48.4 | 45.92 | 0.89 | 2.79 | 77.3 | thesis_unavailable |
| MU | Technology | des | 48.4 | 45.92 | 0.89 | 2.79 | 59.6 | thesis_unavailable |
| NVDA | Technology | des | 48.4 | 45.92 | 0.89 | 2.79 | 70.6 | thesis_unavailable |
| ORCL | Technology | des | 48.4 | 45.92 | 0.89 | 2.79 | 55.4 | thesis_unavailable |
| EXE | standard_compounder | financial_evolution | 100.0 | 54.4 | 16.98 | 2.68 | 74.2 | thesis_unavailable |

## Stuck tickers (|drift_30d| < 5.0)

Stuck count: **375 / 514**

| ticker | composite | band | drift_30d | drift_90d | maturity | flags |
|---|---|---|---|---|---|---|
| A | 61.7 | constructive | 0.0 | 0.0 | standard_compounder | thesis_unavailable |
| ACGL | 76.9 | elite | 0.0 | 0.0 | financial | thesis_unavailable |
| ADM | 49.9 | monitor | 0.0 | 0.0 | mature_compounder | thesis_unavailable |
| AEE | 54.6 | constructive | 0.0 | 0.0 | standard_compounder | thesis_unavailable |
| AES | 41.6 | weakening | 0.0 | 0.0 | standard_compounder | thesis_unavailable |
| AFL | 46.8 | monitor | 0.0 | 0.0 | financial | thesis_unavailable |
| AIG | 45.6 | monitor | 0.0 | 0.0 | financial | thesis_unavailable |
| AIZ | 63.3 | high_confidence | 0.0 | 0.0 | financial | thesis_unavailable |
| AJG | 77.1 | elite | 0.0 | 0.0 | financial | thesis_unavailable |
| AKAM | 37.0 | weakening | 0.0 | 0.0 | standard_compounder | thesis_unavailable |
| ALB | 39.3 | weakening | 0.0 | 0.0 | recovery_rerating | thesis_unavailable |
| ALGN | 36.8 | weakening | 0.0 | 0.0 | growth_compounder | thesis_unavailable |
| ALL | 56.8 | constructive | 0.0 | 0.0 | financial | thesis_unavailable |
| ALLE | 66.9 | high_confidence | 0.0 | 0.0 | standard_compounder | thesis_unavailable |
| AMCR | 62.9 | constructive | 0.0 | 0.0 | growth_compounder | thesis_unavailable |
| AME | 56.6 | constructive | 0.0 | 0.0 | standard_compounder | thesis_unavailable |
| AMP | 56.5 | constructive | 0.0 | 0.0 | financial | thesis_unavailable |
| AON | 57.5 | constructive | 0.0 | 0.0 | financial | thesis_unavailable |
| AOS | 42.7 | monitor | 0.0 | 0.0 | standard_compounder | thesis_unavailable |
| APA | 64.2 | high_confidence | 0.0 | 0.0 | standard_compounder | sec_unavailable,thesis_unavailable |
| APD | 50.1 | monitor | 0.0 | 0.0 | mature_compounder | thesis_unavailable |
| APO | 65.7 | high_confidence | 0.0 | 0.0 | financial | thesis_unavailable |
| APP | 55.3 | constructive | 0.0 | 0.0 | growth_compounder | thesis_unavailable |
| APTV | 35.0 | weakening | 0.0 | 0.0 | standard_compounder | thesis_unavailable |
| ARE | 39.1 | weakening | 0.0 | 0.0 | standard_compounder | sec_unavailable,thesis_unavailable |

