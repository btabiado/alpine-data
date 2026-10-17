# LTHCS composite-score distribution — 2026-10-17

Snapshot file: `data/lthcs/snapshots/2026-10-17.json` (latest available; today is 2026-10-17).  Universe size: **515**.

## Distribution summary

- mean: **51.71**   stdev: **9.38**
- min/max: **31.6 / 78.2**
- p5/p25/p50/p75/p95: **37.5 / 44.1 / 51.7 / 58.6 / 67.43**

## Histogram (10-point bins)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  |                                          0
 30-39  | #############                            57
 40-49  | ######################################   173
 50-59  | ######################################## 180
 60-69  | #####################                    94
 70-79  | ##                                       11
 80-89  |                                          0
 90-100 |                                          0
```

## Band cohorts vs documented thresholds

| band | range | count | share |
|---|---|---|---|
| review | 0-32 | 3 | 0.6% |
| weakening | 33-41 | 85 | 16.5% |
| monitor | 42-51 | 171 | 33.2% |
| constructive | 52-62 | 188 | 36.5% |
| high_confidence | 63-69 | 57 | 11.1% |
| elite | 70-100 | 11 | 2.1% |

Band populations look balanced — no starved or runaway band.

## Per-cohort distribution

| cohort | n | mean | stdev | p25 | p50 | p75 |
|---|---|---|---|---|---|---|
| financial | 56 | 56.8 | 9.31 | 50.35 | 55.95 | 64.6 |
| growth_compounder | 56 | 52.36 | 10.01 | 44.4 | 52.35 | 60.65 |
| mature_compounder | 73 | 52.39 | 8.67 | 45.2 | 52.9 | 57.7 |
| pre_profit_growth | 2 | 69.3 | 0.5 | 69.05 | 69.3 | 69.55 |
| recovery_rerating | 4 | 53.58 | 12.87 | 45.2 | 56.9 | 65.27 |
| recovery_stabilization | 24 | 50.22 | 9.97 | 42.8 | 50.3 | 58.67 |
| standard_compounder | 300 | 50.46 | 8.88 | 43.3 | 50.4 | 56.65 |

### financial (56)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  |                                          0
 30-39  |                                          0
 40-49  | ########################                 14
 50-59  | ######################################## 23
 60-69  | ########################                 14
 70-79  | #########                                5
 80-89  |                                          0
 90-100 |                                          0
```

### growth_compounder (56)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  |                                          0
 30-39  | ##################                       8
 40-49  | ######################################## 18
 50-59  | #################################        15
 60-69  | #############################            13
 70-79  | ####                                     2
 80-89  |                                          0
 90-100 |                                          0
```

### mature_compounder (73)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  |                                          0
 30-39  | #########                                6
 40-49  | #####################################    25
 50-59  | ######################################## 27
 60-69  | ###################                      13
 70-79  | ###                                      2
 80-89  |                                          0
 90-100 |                                          0
```

### recovery_rerating (4)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  |                                          0
 30-39  | ####################                     1
 40-49  | ####################                     1
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
 20-29  |                                          0
 30-39  | #############################            5
 40-49  | ######################################## 7
 50-59  | ######################################## 7
 60-69  | #############################            5
 70-79  |                                          0
 80-89  |                                          0
 90-100 |                                          0
```

### standard_compounder (300)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  |                                          0
 30-39  | ##############                           37
 40-49  | ######################################## 108
 50-59  | ######################################## 108
 60-69  | #################                        45
 70-79  | #                                        2
 80-89  |                                          0
 90-100 |                                          0
```

## Top 5 / bottom 5 by composite

**Top 5**

| ticker | composite | band | maturity | adoption | inst | fin | thesis | des | flags |
|---|---|---|---|---|---|---|---|---|---|
| IBKR | 78.2 | elite | financial | 91.1 | 50.0 | 97.8 | None | 72.3 | thesis_unavailable |
| ARES | 76.8 | elite | financial | 85.9 | 47.0 | 98.9 | None | 72.3 | thesis_unavailable |
| HOOD | 74.8 | elite | growth_compounder | 82.5 | 55.0 | 91.7 | None | 72.3 | thesis_unavailable |
| COF | 74.6 | elite | standard_compounder | 96.9 | 52.0 | 70.5 | None | 72.3 | thesis_unavailable |
| SCHW | 72.6 | elite | standard_compounder | 89.5 | 47.0 | 79.0 | None | 72.3 | thesis_unavailable |

**Bottom 5**

| ticker | composite | band | maturity | adoption | inst | fin | thesis | des | flags |
|---|---|---|---|---|---|---|---|---|---|
| DE | 31.6 | review | standard_compounder | 10.6 | 50.0 | 19.9 | None | 48.3 | thesis_unavailable |
| MRNA | 32.2 | review | recovery_stabilization | 10.0 | 50.0 | 30.0 | None | 41.8 | thesis_unavailable |
| FDXF | 32.8 | review | standard_compounder | 15.1 | 55.0 | 11.8 | None | 48.3 | thesis_unavailable |
| CARR | 33.2 | weakening | standard_compounder | 16.0 | 50.0 | 19.5 | None | 48.3 | thesis_unavailable |
| WBD | 33.3 | weakening | standard_compounder | 20.0 | 51.0 | 15.9 | None | 45.2 | yahoo_unavailable,thesis_unavailable,volatility_unavailable |

## Pillar-vs-peer-group z-score outliers (|z| >= 2.0)

Grouping: `des` is bucketed by **sector** (Phase 3 hotfix — DES is sector-driven; per-cohort grouping clustered Financials as 6/10 outliers). All other pillars remain bucketed by **maturity_stage**. Buckets of size <3 fall back to a universe-wide baseline; the `cohort` column shows which bucket was actually used (`_universe` = fallback).

| ticker | cohort | pillar | value | cohort_mean | cohort_sd | z | composite | flags |
|---|---|---|---|---|---|---|---|---|
| TSCO | standard_compounder | institutional_confidence | 62.0 | 49.53 | 3.2 | 3.9 | 53.4 | thesis_unavailable |
| VST | growth_compounder | institutional_confidence | 62.0 | 49.52 | 3.4 | 3.67 | 46.5 | thesis_unavailable |
| ORCL | mature_compounder | institutional_confidence | 60.0 | 49.04 | 3.16 | 3.47 | 66.5 | thesis_unavailable |
| PFE | mature_compounder | institutional_confidence | 60.0 | 49.04 | 3.16 | 3.47 | 39.8 | thesis_unavailable |
| APTV | standard_compounder | institutional_confidence | 60.0 | 49.53 | 3.2 | 3.27 | 42.6 | thesis_unavailable |
| BSX | standard_compounder | institutional_confidence | 60.0 | 49.53 | 3.2 | 3.27 | 64.8 | thesis_unavailable |
| HONA | standard_compounder | institutional_confidence | 60.0 | 49.53 | 3.2 | 3.27 | 55.2 | yahoo_unavailable,thesis_unavailable |
| LEN | standard_compounder | institutional_confidence | 60.0 | 49.53 | 3.2 | 3.27 | 37.5 | thesis_unavailable |
| AON | financial | institutional_confidence | 55.0 | 48.23 | 2.18 | 3.11 | 61.3 | thesis_unavailable |
| FISV | financial | institutional_confidence | 55.0 | 48.23 | 2.18 | 3.11 | 50.8 | thesis_unavailable |

## Stuck tickers (|drift_30d| < 5.0)

Stuck count: **385 / 515**

| ticker | composite | band | drift_30d | drift_90d | maturity | flags |
|---|---|---|---|---|---|---|
| A | 48.8 | monitor | 0.0 | 0.0 | standard_compounder | thesis_unavailable |
| ACGL | 71.9 | elite | 0.0 | 0.0 | financial | thesis_unavailable |
| ADM | 46.8 | monitor | 0.0 | 0.0 | mature_compounder | thesis_unavailable |
| AEE | 54.6 | constructive | 0.0 | 0.0 | standard_compounder | thesis_unavailable |
| AES | 37.5 | weakening | 0.0 | 0.0 | standard_compounder | thesis_unavailable |
| AFL | 44.0 | monitor | 0.0 | 0.0 | financial | thesis_unavailable |
| AIG | 42.4 | monitor | 0.0 | 0.0 | financial | thesis_unavailable |
| AIZ | 58.7 | constructive | 0.0 | 0.0 | financial | thesis_unavailable |
| AJG | 68.1 | high_confidence | 0.0 | 0.0 | financial | thesis_unavailable |
| AKAM | 48.5 | monitor | 0.0 | 0.0 | standard_compounder | thesis_unavailable |
| ALB | 48.7 | monitor | 0.0 | 0.0 | recovery_rerating | thesis_unavailable |
| ALGN | 42.2 | monitor | 0.0 | 0.0 | growth_compounder | thesis_unavailable |
| ALL | 52.5 | constructive | 0.0 | 0.0 | financial | thesis_unavailable |
| ALLE | 56.3 | constructive | 0.0 | 0.0 | standard_compounder | thesis_unavailable |
| AMCR | 54.0 | constructive | 0.0 | 0.0 | growth_compounder | thesis_unavailable |
| AME | 54.0 | constructive | 0.0 | 0.0 | standard_compounder | thesis_unavailable |
| AMP | 54.6 | constructive | 0.0 | 0.0 | financial | thesis_unavailable |
| AON | 61.3 | constructive | 0.0 | 0.0 | financial | thesis_unavailable |
| AOS | 41.7 | weakening | 0.0 | 0.0 | standard_compounder | thesis_unavailable |
| APA | 50.4 | monitor | 0.0 | 0.0 | standard_compounder | sec_unavailable,thesis_unavailable |
| APD | 41.3 | weakening | 0.0 | 0.0 | mature_compounder | thesis_unavailable |
| APO | 71.0 | elite | 0.0 | 0.0 | financial | thesis_unavailable |
| APP | 65.3 | high_confidence | 0.0 | 0.0 | growth_compounder | thesis_unavailable |
| APTV | 42.6 | monitor | 0.0 | 0.0 | standard_compounder | thesis_unavailable |
| ARE | 45.0 | monitor | 0.0 | 0.0 | standard_compounder | sec_unavailable,thesis_unavailable |

