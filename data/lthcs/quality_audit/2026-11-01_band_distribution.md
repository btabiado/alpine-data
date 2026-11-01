# LTHCS band-threshold audit

**Generated:** 2026-11-01
**Latest equity snapshot:** `2026-11-01`
**Latest crypto snapshot:** `2026-11-01`

## Threshold configuration (from `data/lthcs/weights.json`)

| Band | Range | Label |
|---|---|---|
| elite | 70–100 | Elite Confidence Hold |
| high_confidence | 63–69 | High Confidence Hold |
| constructive | 52–62 | Constructive Hold |
| monitor | 42–51 | Monitor Closely |
| weakening | 33–41 | Confidence Weakening |
| review | 0–32 | Structural Review Required |

_Bands calibrated 2026-10-06 from snapshot(s) 2026-10-06 (515 tickers, method `quantile-share-v1`)._

## Equity universe — band distribution on 2026-11-01

| Band | Count | Pct | Verdict |
|---|---:|---:|---|
| elite | 46 | 8.9% | KEEP |
| high_confidence | 54 | 10.5% | KEEP |
| constructive | 183 | 35.6% | KEEP |
| monitor | 155 | 30.2% | KEEP |
| weakening | 58 | 11.3% | KEEP |
| review | 18 | 3.5% | KEEP |
| **TOTAL** | **514** |  |  |

## Crypto universe — band distribution on 2026-11-01

| Band | Count | Pct | Verdict |
|---|---:|---:|---|
| elite | 0 | 0.0% | SHIFT-DOWN (elite empty — threshold may be too high) |
| high_confidence | 0 | 0.0% | EMPTY (consider widening adjacent bands) |
| constructive | 0 | 0.0% | EMPTY (consider widening adjacent bands) |
| monitor | 2 | 20.0% | KEEP |
| weakening | 2 | 20.0% | KEEP |
| review | 6 | 60.0% | SHIFT-UP (review overflowing — threshold may be too low) |
| **TOTAL** | **10** |  |  |

## Stability (30-day band churn) — equity universe

- tickers with band data: **515**
- mean churn rate: **0.177** changes per consecutive-day pair
- median churn rate: **0.172**
- p90 churn rate: **0.308**
- tickers with churn ≥ 0.20 (= ~6 band-flips in 30 days): **198**

Top 10 churners:

| Ticker | Churn rate |
|---|---:|
| MRVL | 0.483 |
| BLK | 0.448 |
| AIZ | 0.423 |
| COO | 0.423 |
| OKE | 0.423 |
| CSGP | 0.414 |
| DXCM | 0.414 |
| IBM | 0.414 |
| DGX | 0.385 |
| HRL | 0.385 |

**Verdict:** churn rate elevated — consider adding **band-edge hysteresis** (e.g. require 2 consecutive snapshots above/below a threshold before reclassifying).
