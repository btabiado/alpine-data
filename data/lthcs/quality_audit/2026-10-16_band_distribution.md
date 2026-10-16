# LTHCS band-threshold audit

**Generated:** 2026-10-16
**Latest equity snapshot:** `2026-10-16`
**Latest crypto snapshot:** `2026-10-16`

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

## Equity universe — band distribution on 2026-10-16

| Band | Count | Pct | Verdict |
|---|---:|---:|---|
| elite | 25 | 4.9% | KEEP |
| high_confidence | 69 | 13.4% | KEEP |
| constructive | 145 | 28.2% | KEEP |
| monitor | 167 | 32.4% | KEEP |
| weakening | 72 | 14.0% | KEEP |
| review | 37 | 7.2% | KEEP |
| **TOTAL** | **515** |  |  |

## Crypto universe — band distribution on 2026-10-16

| Band | Count | Pct | Verdict |
|---|---:|---:|---|
| elite | 5 | 50.0% | KEEP |
| high_confidence | 2 | 20.0% | KEEP |
| constructive | 2 | 20.0% | KEEP |
| monitor | 0 | 0.0% | EMPTY (consider widening adjacent bands) |
| weakening | 0 | 0.0% | EMPTY (consider widening adjacent bands) |
| review | 1 | 10.0% | KEEP |
| **TOTAL** | **10** |  |  |

## Stability (30-day band churn) — equity universe

- tickers with band data: **515**
- mean churn rate: **0.153** changes per consecutive-day pair
- median churn rate: **0.138**
- p90 churn rate: **0.300**
- tickers with churn ≥ 0.20 (= ~6 band-flips in 30 days): **227**

Top 10 churners:

| Ticker | Churn rate |
|---|---:|
| COO | 0.700 |
| EFX | 0.600 |
| AIZ | 0.500 |
| APD | 0.500 |
| MKC | 0.500 |
| AMD | 0.414 |
| MET | 0.414 |
| MRVL | 0.414 |
| AON | 0.400 |
| APTV | 0.400 |

**Verdict:** churn rate elevated — consider adding **band-edge hysteresis** (e.g. require 2 consecutive snapshots above/below a threshold before reclassifying).
