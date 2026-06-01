# LTHCS band-threshold audit

**Generated:** 2026-06-01
**Latest equity snapshot:** `2026-05-31`
**Latest crypto snapshot:** `2026-05-31`

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

## Equity universe — band distribution on 2026-05-31

| Band | Count | Pct | Verdict |
|---|---:|---:|---|
| elite | 0 | 0.0% | SHIFT-DOWN (elite empty — threshold may be too high) |
| high_confidence | 0 | 0.0% | EMPTY (consider widening adjacent bands) |
| constructive | 13 | 6.2% | KEEP |
| monitor | 32 | 15.3% | KEEP |
| weakening | 66 | 31.6% | KEEP |
| review | 98 | 46.9% | SHIFT-UP (review overflowing — threshold may be too low) |
| **TOTAL** | **209** |  |  |

## Crypto universe — band distribution on 2026-05-31

| Band | Count | Pct | Verdict |
|---|---:|---:|---|
| elite | 0 | 0.0% | SHIFT-DOWN (elite empty — threshold may be too high) |
| high_confidence | 0 | 0.0% | EMPTY (consider widening adjacent bands) |
| constructive | 0 | 0.0% | EMPTY (consider widening adjacent bands) |
| monitor | 1 | 10.0% | KEEP |
| weakening | 5 | 50.0% | KEEP |
| review | 4 | 40.0% | KEEP |
| **TOTAL** | **10** |  |  |

## Stability (30-day band churn) — equity universe

- tickers with band data: **209**
- mean churn rate: **0.049** changes per consecutive-day pair
- median churn rate: **0.034**
- p90 churn rate: **0.138**
- tickers with churn ≥ 0.20 (= ~6 band-flips in 30 days): **7**

Top 10 churners:

| Ticker | Churn rate |
|---|---:|
| CCEP | 0.276 |
| C | 0.241 |
| CEG | 0.241 |
| SMCI | 0.241 |
| UNP | 0.241 |
| CI | 0.222 |
| HWM | 0.222 |

**Verdict:** churn rate acceptable; no hysteresis needed.
