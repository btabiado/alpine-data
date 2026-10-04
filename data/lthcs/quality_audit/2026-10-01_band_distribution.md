# LTHCS band-threshold audit

**Generated:** 2026-10-01
**Latest equity snapshot:** `2026-10-01`
**Latest crypto snapshot:** `2026-10-01`

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

## Equity universe — band distribution on 2026-10-01

| Band | Count | Pct | Verdict |
|---|---:|---:|---|
| elite | 0 | 0.0% | SHIFT-DOWN (elite empty — threshold may be too high) |
| high_confidence | 0 | 0.0% | EMPTY (consider widening adjacent bands) |
| constructive | 0 | 0.0% | EMPTY (consider widening adjacent bands) |
| monitor | 4 | 1.9% | KEEP |
| weakening | 80 | 37.2% | KEEP |
| review | 131 | 60.9% | SHIFT-UP (review overflowing — threshold may be too low) |
| **TOTAL** | **215** |  |  |

## Crypto universe — band distribution on 2026-10-01

| Band | Count | Pct | Verdict |
|---|---:|---:|---|
| elite | 0 | 0.0% | SHIFT-DOWN (elite empty — threshold may be too high) |
| high_confidence | 0 | 0.0% | EMPTY (consider widening adjacent bands) |
| constructive | 2 | 20.0% | KEEP |
| monitor | 5 | 50.0% | KEEP |
| weakening | 2 | 20.0% | KEEP |
| review | 1 | 10.0% | KEEP |
| **TOTAL** | **10** |  |  |

## Stability (30-day band churn) — equity universe

- tickers with band data: **215**
- mean churn rate: **0.147** changes per consecutive-day pair
- median churn rate: **0.138**
- p90 churn rate: **0.345**
- tickers with churn ≥ 0.20 (= ~6 band-flips in 30 days): **91**

Top 10 churners:

| Ticker | Churn rate |
|---|---:|
| CHTR | 0.414 |
| CTSH | 0.414 |
| FTNT | 0.414 |
| ADP | 0.379 |
| AMD | 0.345 |
| AMZN | 0.345 |
| BA | 0.345 |
| BIIB | 0.345 |
| CDW | 0.345 |
| CSGP | 0.345 |

**Verdict:** churn rate elevated — consider adding **band-edge hysteresis** (e.g. require 2 consecutive snapshots above/below a threshold before reclassifying).
