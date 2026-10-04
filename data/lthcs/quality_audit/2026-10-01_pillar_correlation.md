# LTHCS pillar correlation — 2026-10-01

Snapshot file: `data/lthcs/snapshots/2026-10-01.json` (latest available; today is 2026-10-01).

## 5x5 Pearson correlation matrix

| pillar | adoption_momentum | institutional_confidence | financial_evolution | thesis_integrity | des |
|---|---|---|---|---|---|
| adoption_momentum | +1.000 | -0.055 | — | +0.086 | +0.076 |
| institutional_confidence | -0.055 | +1.000 | — | +0.117 | +0.089 |
| financial_evolution | — | — | — | — | — |
| thesis_integrity | +0.086 | +0.117 | — | +1.000 | +0.079 |
| des | +0.076 | +0.089 | — | +0.079 | +1.000 |

## Near-redundant pillar pairs (|r| >= 0.7)

(none — every pillar pair has |r| < 0.7)

## Near-orthogonal pillar pairs (|r| <= 0.2)

| pair | r |
|---|---|
| adoption_momentum ↔ institutional_confidence | -0.055 |
| adoption_momentum ↔ des | +0.076 |
| des ↔ thesis_integrity | +0.079 |
| adoption_momentum ↔ thesis_integrity | +0.086 |
| des ↔ institutional_confidence | +0.089 |
| institutional_confidence ↔ thesis_integrity | +0.117 |

Pairs above carry independent signal — these are the structural workhorses of the composite.

## 30-day correlation stability

Snapshots scanned: **31** (window: 2026-09-01 → 2026-10-01)

| pair | mean | min | max | range |
|---|---|---|---|---|
| des ↔ institutional_confidence | -0.081 | -0.236 | +0.133 | 0.369 |
| institutional_confidence ↔ thesis_integrity | +0.059 | -0.051 | +0.234 | 0.285 |
| adoption_momentum ↔ des | +0.057 | -0.041 | +0.146 | 0.187 |
| adoption_momentum ↔ institutional_confidence | -0.044 | -0.133 | +0.044 | 0.176 |
| adoption_momentum ↔ thesis_integrity | +0.077 | -0.046 | +0.115 | 0.161 |
| des ↔ thesis_integrity | +0.070 | +0.040 | +0.079 | 0.039 |

**Unstable pairs (range >= 0.30 over 30d):** des↔institutional_confidence

