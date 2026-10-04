# LTHCS band-threshold audit

**Generated:** 2026-09-01
**Latest equity snapshot:** `2026-09-01`
**Latest crypto snapshot:** `2026-09-01`

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

## Equity universe — band distribution on 2026-09-01

| Band | Count | Pct | Verdict |
|---|---:|---:|---|
| elite | 0 | 0.0% | SHIFT-DOWN (elite empty — threshold may be too high) |
| high_confidence | 0 | 0.0% | EMPTY (consider widening adjacent bands) |
| constructive | 0 | 0.0% | EMPTY (consider widening adjacent bands) |
| monitor | 12 | 5.6% | KEEP |
| weakening | 97 | 45.1% | KEEP |
| review | 106 | 49.3% | SHIFT-UP (review overflowing — threshold may be too low) |
| **TOTAL** | **215** |  |  |

## Crypto universe — band distribution on 2026-09-01

| Band | Count | Pct | Verdict |
|---|---:|---:|---|
| elite | 0 | 0.0% | SHIFT-DOWN (elite empty — threshold may be too high) |
| high_confidence | 2 | 20.0% | KEEP |
| constructive | 2 | 20.0% | KEEP |
| monitor | 3 | 30.0% | KEEP |
| weakening | 2 | 20.0% | KEEP |
| review | 1 | 10.0% | KEEP |
| **TOTAL** | **10** |  |  |

## Stability (30-day band churn) — equity universe

- tickers with band data: **216**
- mean churn rate: **0.081** changes per consecutive-day pair
- median churn rate: **0.069**
- p90 churn rate: **0.207**
- tickers with churn ≥ 0.20 (= ~6 band-flips in 30 days): **24**

Top 10 churners:

| Ticker | Churn rate |
|---|---:|
| WELL | 0.414 |
| BIIB | 0.310 |
| CSCO | 0.310 |
| LRCX | 0.310 |
| MELI | 0.310 |
| CDW | 0.276 |
| CRWD | 0.276 |
| GM | 0.276 |
| CB | 0.276 |
| FDX | 0.276 |

**Verdict:** churn rate acceptable; no hysteresis needed.
