# LTHCS band-threshold audit

**Generated:** 2026-05-19
**Latest equity snapshot:** `2026-05-18`
**Latest crypto snapshot:** `2026-05-19`

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

## Equity universe — band distribution on 2026-05-18

| Band | Count | Pct | Verdict |
|---|---:|---:|---|
| elite | 0 | 0.0% | SHIFT-DOWN (elite empty — threshold may be too high) |
| high_confidence | 0 | 0.0% | EMPTY (consider widening adjacent bands) |
| constructive | 5 | 3.0% | KEEP |
| monitor | 23 | 13.8% | KEEP |
| weakening | 53 | 31.7% | KEEP |
| review | 86 | 51.5% | SHIFT-UP (review overflowing — threshold may be too low) |
| **TOTAL** | **167** |  |  |

## Crypto universe — band distribution on 2026-05-19

| Band | Count | Pct | Verdict |
|---|---:|---:|---|
| elite | 0 | 0.0% | SHIFT-DOWN (elite empty — threshold may be too high) |
| high_confidence | 0 | 0.0% | EMPTY (consider widening adjacent bands) |
| constructive | 0 | 0.0% | EMPTY (consider widening adjacent bands) |
| monitor | 2 | 20.0% | KEEP |
| weakening | 6 | 60.0% | KEEP |
| review | 2 | 20.0% | KEEP |
| **TOTAL** | **10** |  |  |

## Stability (30-day band churn) — equity universe

- tickers with band data: **167**
- mean churn rate: **0.048** changes per consecutive-day pair
- median churn rate: **0.034**
- p90 churn rate: **0.138**
- tickers with churn ≥ 0.20 (= ~6 band-flips in 30 days): **5**

Top 10 churners:

| Ticker | Churn rate |
|---|---:|
| UNP | 0.276 |
| CCEP | 0.241 |
| NEE | 0.207 |
| PM | 0.207 |
| SMCI | 0.207 |

**Verdict:** churn rate acceptable; no hysteresis needed.
