# LTHCS band-threshold audit

**Generated:** 2026-08-01
**Latest equity snapshot:** `2026-07-31`
**Latest crypto snapshot:** `2026-07-31`

## Threshold configuration (from `data/lthcs/weights.json`)

| Band | Range | Label |
|---|---|---|
| elite | 85–100 | Elite Confidence Hold |
| high_confidence | 80–84 | High Confidence Hold |
| constructive | 70–79 | Constructive Hold |
| monitor | 60–69 | Monitor Closely |
| weakening | 50–59 | Confidence Weakening |
| review | 0–49 | Structural Review Required |

_Note: the task brief lists thresholds at 90/80/70/60/50/<50, but the live `weights.json` config has Elite at 85+ (not 90+). All counts below are computed against the **live config**._

## Equity universe — band distribution on 2026-07-31

| Band | Count | Pct | Verdict |
|---|---:|---:|---|
| elite | 0 | 0.0% | SHIFT-DOWN (elite empty — threshold may be too high) |
| high_confidence | 0 | 0.0% | EMPTY (consider widening adjacent bands) |
| constructive | 0 | 0.0% | EMPTY (consider widening adjacent bands) |
| monitor | 15 | 6.9% | KEEP |
| weakening | 90 | 41.7% | KEEP |
| review | 111 | 51.4% | SHIFT-UP (review overflowing — threshold may be too low) |
| **TOTAL** | **216** |  |  |

## Crypto universe — band distribution on 2026-07-31

| Band | Count | Pct | Verdict |
|---|---:|---:|---|
| elite | 0 | 0.0% | SHIFT-DOWN (elite empty — threshold may be too high) |
| high_confidence | 0 | 0.0% | EMPTY (consider widening adjacent bands) |
| constructive | 0 | 0.0% | EMPTY (consider widening adjacent bands) |
| monitor | 3 | 30.0% | KEEP |
| weakening | 2 | 20.0% | KEEP |
| review | 5 | 50.0% | KEEP |
| **TOTAL** | **10** |  |  |

## Stability (30-day band churn) — equity universe

- tickers with band data: **217**
- mean churn rate: **0.083** changes per consecutive-day pair
- median churn rate: **0.069**
- p90 churn rate: **0.207**
- tickers with churn ≥ 0.20 (= ~6 band-flips in 30 days): **24**

Top 10 churners:

| Ticker | Churn rate |
|---|---:|
| DDOG | 0.414 |
| CSCO | 0.379 |
| MRK | 0.379 |
| DASH | 0.310 |
| MPC | 0.310 |
| TT | 0.310 |
| CRWD | 0.276 |
| KDP | 0.276 |
| ROST | 0.276 |
| BK | 0.250 |

**Verdict:** churn rate acceptable; no hysteresis needed.
