# LTHCS band-threshold audit

**Generated:** 2026-10-17
**Latest equity snapshot:** `2026-10-17`
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

## Equity universe — band distribution on 2026-10-17

| Band | Count | Pct | Verdict |
|---|---:|---:|---|
| elite | 11 | 2.1% | KEEP |
| high_confidence | 57 | 11.1% | KEEP |
| constructive | 188 | 36.5% | KEEP |
| monitor | 171 | 33.2% | KEEP |
| weakening | 85 | 16.5% | KEEP |
| review | 3 | 0.6% | KEEP |
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
- mean churn rate: **0.176** changes per consecutive-day pair
- median churn rate: **0.182**
- p90 churn rate: **0.345**
- tickers with churn ≥ 0.20 (= ~6 band-flips in 30 days): **203**

Top 10 churners:

| Ticker | Churn rate |
|---|---:|
| COO | 0.727 |
| EFX | 0.636 |
| AIZ | 0.545 |
| APD | 0.545 |
| APTV | 0.455 |
| BR | 0.455 |
| CAH | 0.455 |
| CASY | 0.455 |
| CLX | 0.455 |
| DG | 0.455 |

**Verdict:** churn rate elevated — consider adding **band-edge hysteresis** (e.g. require 2 consecutive snapshots above/below a threshold before reclassifying).
