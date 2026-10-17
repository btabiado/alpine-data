# LTHCS pillar correlation — 2026-10-17

Snapshot file: `data/lthcs/snapshots/2026-10-17.json` (latest available; today is 2026-10-17).

## 5x5 Pearson correlation matrix

| pillar | adoption_momentum | institutional_confidence | financial_evolution | thesis_integrity | des |
|---|---|---|---|---|---|
| adoption_momentum | +1.000 | -0.091 | +0.609 | — | -0.004 |
| institutional_confidence | -0.091 | +1.000 | -0.056 | — | -0.116 |
| financial_evolution | +0.609 | -0.056 | +1.000 | — | -0.082 |
| thesis_integrity | — | — | — | — | — |
| des | -0.004 | -0.116 | -0.082 | — | +1.000 |

## Near-redundant pillar pairs (|r| >= 0.7)

(none — every pillar pair has |r| < 0.7)

## Near-orthogonal pillar pairs (|r| <= 0.2)

| pair | r |
|---|---|
| adoption_momentum ↔ des | -0.004 |
| financial_evolution ↔ institutional_confidence | -0.056 |
| des ↔ financial_evolution | -0.082 |
| adoption_momentum ↔ institutional_confidence | -0.091 |
| des ↔ institutional_confidence | -0.116 |

Pairs above carry independent signal — these are the structural workhorses of the composite.

## 30-day correlation stability

Snapshots scanned: **31** (window: 2026-09-17 → 2026-10-17)

| pair | mean | min | max | range |
|---|---|---|---|---|
| des ↔ institutional_confidence | +0.028 | -0.236 | +0.187 | 0.423 |
| institutional_confidence ↔ thesis_integrity | +0.054 | -0.042 | +0.158 | 0.200 |
| adoption_momentum ↔ institutional_confidence | -0.023 | -0.133 | +0.055 | 0.188 |
| adoption_momentum ↔ des | +0.041 | -0.042 | +0.106 | 0.148 |
| adoption_momentum ↔ thesis_integrity | +0.087 | -0.024 | +0.115 | 0.139 |
| financial_evolution ↔ institutional_confidence | -0.033 | -0.059 | -0.005 | 0.054 |
| adoption_momentum ↔ financial_evolution | +0.605 | +0.589 | +0.636 | 0.047 |
| des ↔ financial_evolution | -0.073 | -0.082 | -0.041 | 0.041 |
| des ↔ thesis_integrity | +0.078 | +0.071 | +0.108 | 0.037 |
| financial_evolution ↔ thesis_integrity | -0.067 | -0.067 | -0.067 | 0.000 |

**Unstable pairs (range >= 0.30 over 30d):** des↔institutional_confidence

