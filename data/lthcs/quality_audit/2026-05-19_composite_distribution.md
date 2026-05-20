# LTHCS composite-score distribution — 2026-05-19

Snapshot file: `data/lthcs/snapshots/2026-05-18.json` (latest available; today is 2026-05-19).  Universe size: **167**.

## Distribution summary

- mean: **48.69**   stdev: **12.05**
- min/max: **18.3 / 77.9**
- p5/p25/p50/p75/p95: **28.6 / 40.3 / 49.6 / 56.5 / 67.97**

## Histogram (10-point bins)

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

## Band cohorts vs documented thresholds

| band | range | count | share |
|---|---|---|---|
| review | 0-49 | 86 | 51.5% |
| weakening | 50-59 | 53 | 31.7% |
| monitor | 60-69 | 23 | 13.8% |
| constructive | 70-79 | 5 | 3.0% |
| high_confidence | 80-84 | 0 | 0.0% |
| elite | 85-100 | 0 | 0.0% |

**Starved bands (count=0):** high_confidence, elite.
**Over-populated bands (>=40% share):** review (86, 51.5%).

## Per-cohort distribution

| cohort | n | mean | stdev | p25 | p50 | p75 |
|---|---|---|---|---|---|---|
| growth_compounder | 14 | 51.93 | 16.29 | 46.5 | 54.3 | 60.5 |
| mature_compounder | 49 | 50.11 | 11.43 | 41.9 | 51.6 | 56.1 |
| pre_profit_growth | 1 | 38.3 | 0.0 | 38.3 | 38.3 | 38.3 |
| recovery_rerating | 1 | 49.6 | 0.0 | 49.6 | 49.6 | 49.6 |
| recovery_stabilization | 2 | 47.45 | 9.85 | 42.52 | 47.45 | 52.38 |
| standard_compounder | 100 | 47.66 | 11.62 | 39.8 | 48.65 | 55.05 |

### growth_compounder (14)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  | ########################                 3
 30-39  |                                          0
 40-49  | ################                         2
 50-59  | ######################################## 5
 60-69  | ################                         2
 70-79  | ################                         2
 80-89  |                                          0
 90-100 |                                          0
```

### mature_compounder (49)

```
  0-9   |                                          0
 10-19  |                                          0
 20-29  | ####                                     2
 30-39  | #################                        8
 40-49  | #####################                    10
 50-59  | ######################################## 19
 60-69  | ###################                      9
 70-79  | ##                                       1
 80-89  |                                          0
 90-100 |                                          0
```

### standard_compounder (100)

```
  0-9   |                                          0
 10-19  | #                                        1
 20-29  | #######                                  6
 30-39  | ######################                   18
 40-49  | ######################################## 33
 50-59  | ##################################       28
 60-69  | ###############                          12
 70-79  | ##                                       2
 80-89  |                                          0
 90-100 |                                          0
```

## Top 5 / bottom 5 by composite

**Top 5**

| ticker | composite | band | maturity | adoption | inst | fin | thesis | des | flags |
|---|---|---|---|---|---|---|---|---|---|
| FANG | 77.9 | constructive | standard_compounder | 92.3 | 90.0 | 69.6 | 54.9 | 61.9 | thesis_unavailable |
| MU | 77.6 | constructive | growth_compounder | 93.0 | 96.4 | 93.6 | 58.8 | 47.6 | thesis_unavailable |
| AVGO | 76.8 | constructive | mature_compounder | 100.0 | 80.9 | 84.8 | 80.7 | 47.6 | - |
| NVDA | 74.6 | constructive | growth_compounder | 100.0 | 70.5 | 84.5 | 55.0 | 47.6 | thesis_unavailable |
| ADI | 73.4 | constructive | standard_compounder | 83.1 | 89.2 | 85.8 | 76.0 | 43.5 | - |

**Bottom 5**

| ticker | composite | band | maturity | adoption | inst | fin | thesis | des | flags |
|---|---|---|---|---|---|---|---|---|---|
| NKE | 18.3 | review | standard_compounder | 15.8 | 6.0 | 24.0 | 51.9 | 37.4 | thesis_unavailable |
| GM | 22.4 | review | standard_compounder | 14.7 | 27.1 | 19.8 | 58.8 | 37.4 | thesis_unavailable |
| MDB | 24.0 | review | growth_compounder | 21.2 | 13.3 | 27.6 | 58.8 | 43.5 | thesis_unavailable |
| HD | 24.0 | review | mature_compounder | 6.3 | 19.5 | 40.6 | 55.0 | 37.4 | thesis_unavailable |
| ZS | 25.2 | review | growth_compounder | 25.5 | 9.9 | 47.5 | 58.8 | 43.5 | thesis_unavailable |

## Pillar-vs-cohort z-score outliers (|z| >= 2.0)

| ticker | cohort | pillar | value | cohort_mean | cohort_sd | z | composite | flags |
|---|---|---|---|---|---|---|---|---|
| AZN | standard_compounder | thesis_integrity | 78.5 | 55.12 | 7.81 | 2.99 | 51.2 | sec_unavailable |
| BK | standard_compounder | des | 71.6 | 45.0 | 8.88 | 2.99 | 65.6 | - |
| BLK | standard_compounder | des | 71.6 | 45.0 | 8.88 | 2.99 | 69.2 | thesis_unavailable |
| COF | standard_compounder | des | 71.6 | 45.0 | 8.88 | 2.99 | 60.3 | thesis_unavailable |
| MET | standard_compounder | des | 71.6 | 45.0 | 8.88 | 2.99 | 62.4 | thesis_unavailable |
| SCHW | standard_compounder | des | 71.6 | 45.0 | 8.88 | 2.99 | 63.9 | thesis_unavailable |
| USB | standard_compounder | des | 71.6 | 45.0 | 8.88 | 2.99 | 43.4 | thesis_unavailable |
| AVGO | mature_compounder | thesis_integrity | 80.7 | 57.83 | 7.94 | 2.88 | 76.8 | - |
| ADSK | standard_compounder | thesis_integrity | 76.6 | 55.12 | 7.81 | 2.75 | 59.8 | - |
| AMT | standard_compounder | thesis_integrity | 76.2 | 55.12 | 7.81 | 2.7 | 61.0 | - |

## Stuck tickers (|drift_30d| < 5.0)

Stuck count: **167 / 167**

| ticker | composite | band | drift_30d | drift_90d | maturity | flags |
|---|---|---|---|---|---|---|
| AAPL | 54.2 | weakening | 0.0 | 0.0 | mature_compounder | - |
| ABBV | 51.5 | weakening | 0.0 | 0.0 | mature_compounder | - |
| ABNB | 51.6 | weakening | 0.0 | 0.0 | standard_compounder | - |
| ABT | 45.8 | review | 0.0 | 0.0 | standard_compounder | - |
| ACN | 38.4 | review | 0.0 | 0.0 | standard_compounder | - |
| ADBE | 46.5 | review | 0.0 | 0.0 | mature_compounder | - |
| ADI | 73.4 | constructive | 0.0 | 0.0 | standard_compounder | - |
| ADP | 53.0 | weakening | 0.0 | 0.0 | mature_compounder | - |
| ADSK | 59.8 | weakening | 0.0 | 0.0 | standard_compounder | - |
| AEP | 57.7 | weakening | 0.0 | 0.0 | standard_compounder | - |
| AMAT | 53.4 | weakening | 0.0 | 0.0 | mature_compounder | - |
| AMD | 61.0 | monitor | 0.0 | 0.0 | growth_compounder | - |
| AMGN | 50.4 | weakening | 0.0 | 0.0 | standard_compounder | - |
| AMT | 61.0 | monitor | 0.0 | 0.0 | standard_compounder | - |
| AMZN | 64.4 | monitor | 0.0 | 0.0 | mature_compounder | - |
| ARM | 55.3 | weakening | 0.0 | 0.0 | growth_compounder | - |
| ASML | 68.0 | monitor | 0.0 | 0.0 | standard_compounder | - |
| AVGO | 76.8 | constructive | 0.0 | 0.0 | mature_compounder | - |
| AXP | 56.1 | weakening | 0.0 | 0.0 | mature_compounder | - |
| AZN | 51.2 | weakening | 0.0 | 0.0 | standard_compounder | sec_unavailable |
| BA | 57.3 | weakening | 0.0 | 0.0 | recovery_stabilization | - |
| BAC | 51.6 | weakening | 0.0 | 0.0 | mature_compounder | - |
| BIIB | 44.3 | review | 0.0 | 0.0 | standard_compounder | - |
| BK | 65.6 | monitor | 0.0 | 0.0 | standard_compounder | - |
| BKNG | 44.8 | review | 0.0 | 0.0 | standard_compounder | thesis_unavailable |

