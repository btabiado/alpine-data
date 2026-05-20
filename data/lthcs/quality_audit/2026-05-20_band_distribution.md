# LTHCS band-threshold audit

**Generated:** 2026-05-20
**Latest equity snapshot:** `2026-05-20`
**Latest crypto snapshot:** `2026-05-20`

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

## Equity universe — band distribution on 2026-05-20

| Band | Count | Pct | Verdict |
|---|---:|---:|---|
| elite | 0 | 0.0% | SHIFT-DOWN (elite empty — threshold may be too high) |
| high_confidence | 0 | 0.0% | EMPTY (consider widening adjacent bands) |
| constructive | 6 | 3.6% | KEEP |
| monitor | 26 | 15.6% | KEEP |
| weakening | 52 | 31.1% | KEEP |
| review | 83 | 49.7% | SHIFT-UP (review overflowing — threshold may be too low) |
| **TOTAL** | **167** |  |  |

## Crypto universe — band distribution on 2026-05-20

| Band | Count | Pct | Verdict |
|---|---:|---:|---|
| elite | 0 | 0.0% | SHIFT-DOWN (elite empty — threshold may be too high) |
| high_confidence | 0 | 0.0% | EMPTY (consider widening adjacent bands) |
| constructive | 0 | 0.0% | EMPTY (consider widening adjacent bands) |
| monitor | 2 | 20.0% | KEEP |
| weakening | 7 | 70.0% | KEEP |
| review | 1 | 10.0% | KEEP |
| **TOTAL** | **10** |  |  |

## Stability (30-day band churn) — equity universe

- tickers with band data: **167**
- mean churn rate: **0.051** changes per consecutive-day pair
- median churn rate: **0.034**
- p90 churn rate: **0.138**
- tickers with churn ≥ 0.20 (= ~6 band-flips in 30 days): **5**

Top 10 churners:

| Ticker | Churn rate |
|---|---:|
| UNP | 0.276 |
| CCEP | 0.241 |
| PM | 0.207 |
| SMCI | 0.207 |
| SPG | 0.207 |

**Verdict:** churn rate acceptable; no hysteresis needed.
