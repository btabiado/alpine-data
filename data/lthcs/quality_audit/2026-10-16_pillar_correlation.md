# LTHCS pillar correlation — 2026-10-16

Snapshot file: `data/lthcs/snapshots/2026-10-16.json` (latest available; today is 2026-10-16).

## 5x5 Pearson correlation matrix

| pillar | adoption_momentum | institutional_confidence | financial_evolution | thesis_integrity | des |
|---|---|---|---|---|---|
| adoption_momentum | +1.000 | +0.010 | +0.613 | — | -0.006 |
| institutional_confidence | +0.010 | +1.000 | -0.042 | — | +0.141 |
| financial_evolution | +0.613 | -0.042 | +1.000 | — | -0.077 |
| thesis_integrity | — | — | — | — | — |
| des | -0.006 | +0.141 | -0.077 | — | +1.000 |

## Near-redundant pillar pairs (|r| >= 0.7)

(none — every pillar pair has |r| < 0.7)

## Near-orthogonal pillar pairs (|r| <= 0.2)

| pair | r |
|---|---|
| adoption_momentum ↔ des | -0.006 |
| adoption_momentum ↔ institutional_confidence | +0.010 |
| financial_evolution ↔ institutional_confidence | -0.042 |
| des ↔ financial_evolution | -0.077 |
| des ↔ institutional_confidence | +0.141 |

Pairs above carry independent signal — these are the structural workhorses of the composite.

## 30-day correlation stability

Snapshots scanned: **31** (window: 2026-09-16 → 2026-10-16)

| pair | mean | min | max | range |
|---|---|---|---|---|
| des ↔ institutional_confidence | +0.024 | -0.236 | +0.187 | 0.423 |
| institutional_confidence ↔ thesis_integrity | +0.049 | -0.042 | +0.158 | 0.200 |
| adoption_momentum ↔ institutional_confidence | -0.022 | -0.133 | +0.055 | 0.188 |
| adoption_momentum ↔ des | +0.044 | -0.042 | +0.106 | 0.148 |
| adoption_momentum ↔ thesis_integrity | +0.088 | -0.024 | +0.115 | 0.139 |
| financial_evolution ↔ institutional_confidence | -0.031 | -0.059 | -0.005 | 0.054 |
| adoption_momentum ↔ financial_evolution | +0.605 | +0.589 | +0.636 | 0.047 |
| des ↔ financial_evolution | -0.072 | -0.081 | -0.041 | 0.040 |
| des ↔ thesis_integrity | +0.078 | +0.071 | +0.108 | 0.037 |
| financial_evolution ↔ thesis_integrity | -0.067 | -0.067 | -0.067 | 0.000 |

**Unstable pairs (range >= 0.30 over 30d):** des↔institutional_confidence

