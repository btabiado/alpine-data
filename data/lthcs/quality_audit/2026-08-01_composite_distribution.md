# LTHCS composite-score distribution — 2026-08-01

Snapshot file: `data/lthcs/snapshots/2026-07-31.json` (latest available; today is 2026-08-01).  Universe size: **216**.

## Distribution summary

- mean: **49.04**   stdev: **7.63**
- min/max: **31.9 / 64.9**
- p5/p25/p50/p75/p95: **37.33 / 42.4 / 49.5 / 55.5 / 60.73**

## Histogram (10-point bins)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  |                                          0
 30-39  | ###############                          33
 40-49  | ###################################      78
 50-59  | ######################################## 90
 60-69  | #######                                  15
 70-79  |                                          0
 80-89  |                                          0
 90-100 |                                          0
```

## Band cohorts vs documented thresholds

| band | range | count | share |
|---|---|---|---|
| review | 0-49 | 111 | 51.4% |
| weakening | 50-59 | 90 | 41.7% |
| monitor | 60-69 | 15 | 6.9% |
| constructive | 70-79 | 0 | 0.0% |
| high_confidence | 80-84 | 0 | 0.0% |
| elite | 85-100 | 0 | 0.0% |

**Starved bands (count=0):** constructive, high_confidence, elite.
**Over-populated bands (>=40% share):** review (111, 51.4%), weakening (90, 41.7%).

## Per-cohort distribution

| cohort | n | mean | stdev | p25 | p50 | p75 |
|---|---|---|---|---|---|---|
| financial | 8 | 52.85 | 5.5 | 49.62 | 52.8 | 56.2 |
| growth_compounder | 20 | 53.43 | 5.41 | 51.8 | 54.6 | 56.55 |
| mature_compounder | 63 | 49.81 | 8.56 | 41.4 | 51.0 | 56.7 |
| pre_profit_growth | 1 | 35.5 | 0.0 | 35.5 | 35.5 | 35.5 |
| recovery_rerating | 1 | 57.2 | 0.0 | 57.2 | 57.2 | 57.2 |
| recovery_stabilization | 3 | 52.9 | 2.05 | 52.1 | 54.2 | 54.35 |
| standard_compounder | 120 | 47.6 | 7.09 | 41.58 | 47.55 | 53.23 |

### financial (8)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  |                                          0
 30-39  |                                          0
 40-49  | ################                         2
 50-59  | ######################################## 5
 60-69  | ########                                 1
 70-79  |                                          0
 80-89  |                                          0
 90-100 |                                          0
```

### growth_compounder (20)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  |                                          0
 30-39  | ###                                      1
 40-49  | #####                                    2
 50-59  | ######################################## 15
 60-69  | #####                                    2
 70-79  |                                          0
 80-89  |                                          0
 90-100 |                                          0
```

### mature_compounder (63)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  |                                          0
 30-39  | #################                        10
 40-49  | #####################################    21
 50-59  | ######################################## 23
 60-69  | ################                         9
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
 40-49  |                                          0
 50-59  | ######################################## 3
 60-69  |                                          0
 70-79  |                                          0
 80-89  |                                          0
 90-100 |                                          0
```

### standard_compounder (120)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  |                                          0
 30-39  | ################                         21
 40-49  | ######################################## 53
 50-59  | ################################         43
 60-69  | ##                                       3
 70-79  |                                          0
 80-89  |                                          0
 90-100 |                                          0
```

## Top 5 / bottom 5 by composite

**Top 5**

| ticker | composite | band | maturity | adoption | inst | fin | thesis | des | flags |
|---|---|---|---|---|---|---|---|---|---|
| MET | 64.9 | monitor | standard_compounder | 50.0 | 89.1 | 50.0 | 55.0 | 70.5 | sec_unavailable,thesis_unavailable |
| BAC | 64.3 | monitor | mature_compounder | 51.2 | 85.4 | 50.0 | 55.0 | 70.5 | sec_unavailable,thesis_unavailable |
| MS | 63.4 | monitor | mature_compounder | 50.0 | 83.0 | 50.0 | 58.8 | 70.5 | sec_unavailable,thesis_unavailable |
| PYPL | 63.1 | monitor | mature_compounder | 50.0 | 81.9 | 50.0 | 55.0 | 70.5 | sec_unavailable,thesis_unavailable |
| TRV | 62.9 | monitor | mature_compounder | 50.0 | 81.2 | 50.0 | 58.8 | 70.5 | sec_unavailable,thesis_unavailable |

**Bottom 5**

| ticker | composite | band | maturity | adoption | inst | fin | thesis | des | flags |
|---|---|---|---|---|---|---|---|---|---|
| ACN | 31.9 | review | standard_compounder | 40.2 | 7.0 | 50.0 | 50.0 | 45.0 | sec_unavailable,thesis_unavailable |
| IBM | 35.2 | review | mature_compounder | 50.0 | 7.7 | 50.0 | 50.0 | 45.0 | sec_unavailable,thesis_unavailable |
| LCID | 35.5 | review | pre_profit_growth | 50.0 | 6.9 | 50.0 | 41.2 | 46.3 | sec_unavailable,thesis_unavailable |
| CTSH | 35.8 | review | standard_compounder | 50.0 | 10.2 | 50.0 | 50.0 | 45.0 | sec_unavailable,thesis_unavailable |
| ADSK | 36.4 | review | standard_compounder | 37.3 | 16.4 | 50.0 | 55.0 | 45.0 | sec_unavailable,thesis_unavailable |

## Pillar-vs-peer-group z-score outliers (|z| >= 2.0)

Grouping: `des` is bucketed by **sector** (Phase 3 hotfix — DES is sector-driven; per-cohort grouping clustered Financials as 6/10 outliers). All other pillars remain bucketed by **maturity_stage**. Buckets of size <3 fall back to a universe-wide baseline; the `cohort` column shows which bucket was actually used (`_universe` = fallback).

| ticker | cohort | pillar | value | cohort_mean | cohort_sd | z | composite | flags |
|---|---|---|---|---|---|---|---|---|
| ASML | standard_compounder | adoption_momentum | 65.0 | 50.03 | 3.35 | 4.47 | 58.3 | sec_unavailable,thesis_unavailable |
| AMAT | mature_compounder | adoption_momentum | 35.0 | 49.75 | 3.42 | -4.31 | 52.8 | sec_unavailable,thesis_unavailable |
| AMD | growth_compounder | adoption_momentum | 59.2 | 50.58 | 2.04 | 4.23 | 61.2 | sec_unavailable,thesis_unavailable |
| BKR | standard_compounder | adoption_momentum | 63.8 | 50.03 | 3.35 | 4.11 | 47.4 | sec_unavailable,thesis_unavailable |
| ADP | mature_compounder | adoption_momentum | 36.2 | 49.75 | 3.42 | -3.96 | 55.5 | sec_unavailable,thesis_unavailable |
| ADSK | standard_compounder | adoption_momentum | 37.3 | 50.03 | 3.35 | -3.8 | 36.4 | sec_unavailable,thesis_unavailable |
| BMY | standard_compounder | adoption_momentum | 62.7 | 50.03 | 3.35 | 3.78 | 55.6 | sec_unavailable,thesis_unavailable |
| ABNB | standard_compounder | adoption_momentum | 38.5 | 50.03 | 3.35 | -3.44 | 47.2 | sec_unavailable,thesis_unavailable |
| AZN | standard_compounder | adoption_momentum | 61.5 | 50.03 | 3.35 | 3.42 | 41.7 | sec_unavailable,thesis_unavailable |
| META | mature_compounder | thesis_integrity | 41.2 | 54.04 | 3.92 | -3.28 | 40.2 | sec_unavailable,thesis_unavailable |

## Stuck tickers (|drift_30d| < 5.0)

Stuck count: **143 / 216**

| ticker | composite | band | drift_30d | drift_90d | maturity | flags |
|---|---|---|---|---|---|---|
| CSCO | 59.6 | weakening | 0.0 | 4.3 | mature_compounder | sec_unavailable,thesis_unavailable |
| GILD | 39.2 | review | 0.0 | -10.8 | standard_compounder | sec_unavailable,thesis_unavailable |
| AAPL | 56.6 | weakening | -0.1 | 3.6 | mature_compounder | sec_unavailable,thesis_unavailable |
| CL | 46.2 | review | 0.1 | -7.1 | standard_compounder | sec_unavailable,thesis_unavailable |
| NKE | 37.6 | review | 0.1 | 15.3 | standard_compounder | sec_unavailable,thesis_unavailable |
| ON | 54.5 | weakening | -0.1 | 4.9 | standard_compounder | sec_unavailable,thesis_unavailable |
| MCO | 53.5 | weakening | 0.1 | 0.0 | financial | sec_unavailable,thesis_unavailable |
| CDW | 57.3 | weakening | 0.2 | 11.5 | standard_compounder | sec_unavailable,thesis_unavailable |
| CSGP | 39.4 | review | 0.2 | -19.5 | standard_compounder | sec_unavailable,thesis_unavailable |
| LLY | 55.7 | weakening | -0.2 | 6.5 | growth_compounder | sec_unavailable,thesis_unavailable |
| LULU | 38.1 | review | -0.2 | 1.1 | standard_compounder | sec_unavailable,thesis_unavailable |
| MU | 57.8 | weakening | -0.2 | -22.9 | growth_compounder | sec_unavailable,thesis_unavailable |
| PANW | 62.3 | monitor | -0.2 | 4.1 | mature_compounder | sec_unavailable,thesis_unavailable |
| SPG | 57.2 | weakening | -0.2 | -3.0 | standard_compounder | sec_unavailable,thesis_unavailable |
| BSX | 38.0 | review | -0.2 | 0.0 | standard_compounder | sec_unavailable,thesis_unavailable |
| PLD | 46.8 | review | -0.2 | 0.0 | standard_compounder | sec_unavailable,thesis_unavailable |
| STX | 57.1 | weakening | 0.2 | 0.0 | standard_compounder | sec_unavailable,thesis_unavailable |
| VLO | 59.4 | weakening | 0.2 | 0.0 | standard_compounder | sec_unavailable,thesis_unavailable |
| AMT | 40.3 | review | 0.3 | -25.5 | standard_compounder | sec_unavailable,thesis_unavailable |
| DDOG | 60.1 | monitor | 0.3 | 3.7 | growth_compounder | sec_unavailable,thesis_unavailable |
| DE | 46.0 | review | 0.3 | 0.9 | standard_compounder | sec_unavailable,thesis_unavailable |
| FTNT | 60.4 | monitor | 0.3 | -2.3 | mature_compounder | sec_unavailable,thesis_unavailable |
| ILMN | 58.8 | weakening | 0.3 | 23.6 | standard_compounder | sec_unavailable,thesis_unavailable |
| UNH | 58.4 | weakening | 0.3 | -7.6 | standard_compounder | sec_unavailable,thesis_unavailable |
| NOC | 38.7 | review | 0.3 | 0.0 | mature_compounder | sec_unavailable,thesis_unavailable |

