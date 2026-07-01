# LTHCS composite-score distribution — 2026-07-01

Snapshot file: `data/lthcs/snapshots/2026-06-30.json` (latest available; today is 2026-07-01).  Universe size: **217**.

## Distribution summary

- mean: **49.05**   stdev: **7.13**
- min/max: **35.2 / 62.5**
- p5/p25/p50/p75/p95: **37.86 / 42.8 / 49.7 / 55.6 / 59.12**

## Histogram (10-point bins)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  |                                          0
 30-39  | #############                            33
 40-49  | ################################         78
 50-59  | ######################################## 99
 60-69  | ###                                      7
 70-79  |                                          0
 80-89  |                                          0
 90-100 |                                          0
```

## Band cohorts vs documented thresholds

| band | range | count | share |
|---|---|---|---|
| review | 0-49 | 111 | 51.2% |
| weakening | 50-59 | 99 | 45.6% |
| monitor | 60-69 | 7 | 3.2% |
| constructive | 70-79 | 0 | 0.0% |
| high_confidence | 80-84 | 0 | 0.0% |
| elite | 85-100 | 0 | 0.0% |

**Starved bands (count=0):** constructive, high_confidence, elite.
**Over-populated bands (>=40% share):** review (111, 51.2%), weakening (99, 45.6%).

## Per-cohort distribution

| cohort | n | mean | stdev | p25 | p50 | p75 |
|---|---|---|---|---|---|---|
| financial | 8 | 49.76 | 5.81 | 45.1 | 50.65 | 54.6 |
| growth_compounder | 20 | 52.44 | 7.32 | 49.05 | 54.45 | 57.92 |
| mature_compounder | 63 | 49.18 | 7.59 | 42.4 | 49.4 | 56.4 |
| pre_profit_growth | 1 | 36.0 | 0.0 | 36.0 | 36.0 | 36.0 |
| recovery_rerating | 1 | 55.6 | 0.0 | 55.6 | 55.6 | 55.6 |
| recovery_stabilization | 3 | 51.93 | 3.99 | 50.4 | 54.5 | 54.75 |
| standard_compounder | 121 | 48.36 | 6.73 | 42.8 | 48.3 | 54.4 |

### financial (8)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  |                                          0
 30-39  |                                          0
 40-49  | ######################################## 4
 50-59  | ######################################## 4
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
 30-39  | ######                                   2
 40-49  | ############                             4
 50-59  | ######################################## 13
 60-69  | ###                                      1
 70-79  |                                          0
 80-89  |                                          0
 90-100 |                                          0
```

### mature_compounder (63)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  |                                          0
 30-39  | ##############                           9
 40-49  | ######################################   24
 50-59  | ######################################## 25
 60-69  | ########                                 5
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
 40-49  | ####################                     1
 50-59  | ######################################## 2
 60-69  |                                          0
 70-79  |                                          0
 80-89  |                                          0
 90-100 |                                          0
```

### standard_compounder (121)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  |                                          0
 30-39  | ################                         21
 40-49  | #################################        45
 50-59  | ######################################## 54
 60-69  | #                                        1
 70-79  |                                          0
 80-89  |                                          0
 90-100 |                                          0
```

## Top 5 / bottom 5 by composite

**Top 5**

| ticker | composite | band | maturity | adoption | inst | fin | thesis | des | flags |
|---|---|---|---|---|---|---|---|---|---|
| AMD | 62.5 | monitor | growth_compounder | 63.7 | 96.1 | 50.0 | 55.0 | 48.9 | sec_unavailable,thesis_unavailable |
| PANW | 62.5 | monitor | mature_compounder | 50.0 | 104.2 | 50.0 | 55.0 | 46.0 | sec_unavailable,thesis_unavailable |
| ASML | 61.9 | monitor | standard_compounder | 59.8 | 89.4 | 50.0 | 55.0 | 46.0 | sec_unavailable,thesis_unavailable |
| MS | 60.9 | monitor | mature_compounder | 50.0 | 80.8 | 50.0 | 58.8 | 62.6 | sec_unavailable,thesis_unavailable |
| AMZN | 60.4 | monitor | mature_compounder | 65.0 | 79.6 | 50.0 | 58.8 | 47.1 | sec_unavailable,thesis_unavailable |

**Bottom 5**

| ticker | composite | band | maturity | adoption | inst | fin | thesis | des | flags |
|---|---|---|---|---|---|---|---|---|---|
| INTU | 35.2 | review | mature_compounder | 50.0 | 6.7 | 50.0 | 50.0 | 46.0 | sec_unavailable,thesis_unavailable |
| ZS | 35.9 | review | growth_compounder | 50.0 | 9.4 | 50.0 | 55.0 | 46.0 | sec_unavailable,thesis_unavailable |
| LCID | 36.0 | review | pre_profit_growth | 50.0 | 8.2 | 50.0 | 41.2 | 47.1 | sec_unavailable,thesis_unavailable |
| ACN | 36.1 | review | standard_compounder | 48.0 | 0.9 | 50.0 | 50.0 | 46.0 | sec_unavailable,thesis_unavailable |
| CTSH | 36.8 | review | standard_compounder | 50.0 | 1.4 | 50.0 | 55.0 | 46.0 | sec_unavailable,thesis_unavailable |

## Pillar-vs-peer-group z-score outliers (|z| >= 2.0)

Grouping: `des` is bucketed by **sector** (Phase 3 hotfix — DES is sector-driven; per-cohort grouping clustered Financials as 6/10 outliers). All other pillars remain bucketed by **maturity_stage**. Buckets of size <3 fall back to a universe-wide baseline; the `cohort` column shows which bucket was actually used (`_universe` = fallback).

| ticker | cohort | pillar | value | cohort_mean | cohort_sd | z | composite | flags |
|---|---|---|---|---|---|---|---|---|
| ADP | mature_compounder | adoption_momentum | 35.0 | 50.22 | 3.17 | -4.8 | 48.5 | sec_unavailable,thesis_unavailable |
| AMZN | mature_compounder | adoption_momentum | 65.0 | 50.22 | 3.17 | 4.66 | 60.4 | sec_unavailable,thesis_unavailable |
| ABT | standard_compounder | adoption_momentum | 37.6 | 49.89 | 2.8 | -4.39 | 37.1 | sec_unavailable,thesis_unavailable |
| BIIB | standard_compounder | adoption_momentum | 61.1 | 49.89 | 2.8 | 4.0 | 58.2 | sec_unavailable,thesis_unavailable |
| BK | standard_compounder | adoption_momentum | 38.9 | 49.89 | 2.8 | -3.92 | 57.5 | sec_unavailable,thesis_unavailable |
| AAPL | mature_compounder | adoption_momentum | 62.4 | 50.22 | 3.17 | 3.84 | 56.7 | sec_unavailable,thesis_unavailable |
| SYK | mature_compounder | thesis_integrity | 41.2 | 54.14 | 3.52 | -3.67 | 39.3 | sec_unavailable,thesis_unavailable |
| ASML | standard_compounder | adoption_momentum | 59.8 | 49.89 | 2.8 | 3.54 | 61.9 | sec_unavailable,thesis_unavailable |
| ABNB | standard_compounder | adoption_momentum | 40.2 | 49.89 | 2.8 | -3.46 | 52.7 | sec_unavailable,thesis_unavailable |
| UBER | growth_compounder | thesis_integrity | 41.2 | 55.08 | 4.36 | -3.18 | 47.1 | sec_unavailable,thesis_unavailable |

## Stuck tickers (|drift_30d| < 5.0)

Stuck count: **84 / 217**

| ticker | composite | band | drift_30d | drift_90d | maturity | flags |
|---|---|---|---|---|---|---|
| F | 50.0 | weakening | 0.0 | 13.3 | standard_compounder | sec_unavailable,thesis_unavailable |
| BX | 47.9 | review | 0.0 | 0.0 | financial | sec_unavailable,thesis_unavailable |
| CB | 54.0 | weakening | 0.0 | 0.0 | financial | sec_unavailable,thesis_unavailable |
| CME | 41.0 | review | 0.0 | 0.0 | financial | sec_unavailable,thesis_unavailable |
| ICE | 42.7 | review | 0.0 | 0.0 | financial | sec_unavailable,thesis_unavailable |
| KKR | 45.9 | review | 0.0 | 0.0 | financial | sec_unavailable,thesis_unavailable |
| MCO | 53.4 | weakening | 0.0 | 0.0 | financial | sec_unavailable,thesis_unavailable |
| PGR | 56.8 | weakening | 0.0 | 0.0 | financial | sec_unavailable,thesis_unavailable |
| PNC | 56.4 | weakening | 0.0 | 0.0 | financial | sec_unavailable,thesis_unavailable |
| LMT | 39.4 | review | -0.1 | -19.1 | standard_compounder | sec_unavailable,thesis_unavailable |
| LRCX | 56.2 | weakening | -0.1 | 4.1 | growth_compounder | sec_unavailable,thesis_unavailable |
| GFS | 57.4 | weakening | 0.2 | 1.7 | standard_compounder | sec_unavailable,thesis_unavailable |
| VLO | 59.2 | weakening | -0.3 | 0.0 | standard_compounder | sec_unavailable,thesis_unavailable |
| CSCO | 59.6 | weakening | -0.4 | 11.4 | mature_compounder | sec_unavailable,thesis_unavailable |
| EMR | 44.9 | review | -0.4 | -2.0 | standard_compounder | sec_unavailable,thesis_unavailable |
| HCA | 37.5 | review | 0.4 | 0.0 | mature_compounder | sec_unavailable,thesis_unavailable |
| ARM | 52.4 | weakening | -0.5 | -0.9 | growth_compounder | sec_unavailable,thesis_unavailable |
| KLAC | 57.4 | weakening | -0.6 | 1.3 | growth_compounder | sec_unavailable,thesis_unavailable |
| ABNB | 52.7 | weakening | 0.7 | -12.1 | standard_compounder | sec_unavailable,thesis_unavailable |
| CCEP | 45.6 | review | 0.7 | -5.3 | standard_compounder | sec_unavailable,thesis_unavailable |
| CMCSA | 38.7 | review | -0.7 | -18.2 | standard_compounder | sec_unavailable,thesis_unavailable |
| DE | 45.7 | review | -0.8 | 1.3 | standard_compounder | sec_unavailable,thesis_unavailable |
| BMY | 45.4 | review | 1.0 | -5.6 | standard_compounder | sec_unavailable,thesis_unavailable |
| DIS | 42.6 | review | -1.0 | -4.9 | mature_compounder | sec_unavailable,thesis_unavailable |
| PM | 48.1 | review | 1.0 | -4.5 | standard_compounder | sec_unavailable,thesis_unavailable |

