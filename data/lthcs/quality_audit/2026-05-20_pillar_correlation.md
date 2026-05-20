# LTHCS pillar correlation — 2026-05-20

Snapshot file: `data/lthcs/snapshots/2026-05-20.json` (latest available; today is 2026-05-20).

## 5x5 Pearson correlation matrix

| pillar | adoption_momentum | institutional_confidence | financial_evolution | thesis_integrity | des |
|---|---|---|---|---|---|
| adoption_momentum | +1.000 | -0.110 | +0.723 | +0.114 | -0.019 |
| institutional_confidence | -0.110 | +1.000 | +0.010 | +0.109 | -0.058 |
| financial_evolution | +0.723 | +0.010 | +1.000 | +0.130 | -0.041 |
| thesis_integrity | +0.114 | +0.109 | +0.130 | +1.000 | +0.061 |
| des | -0.019 | -0.058 | -0.041 | +0.061 | +1.000 |

## Near-redundant pillar pairs (|r| >= 0.7)

| pair | r |
|---|---|
| adoption_momentum ↔ financial_evolution | +0.723 |

Pairs above add little independent signal — candidates for weight reduction in the next weighting profile review.

## Near-orthogonal pillar pairs (|r| <= 0.2)

| pair | r |
|---|---|
| financial_evolution ↔ institutional_confidence | +0.010 |
| adoption_momentum ↔ des | -0.019 |
| des ↔ financial_evolution | -0.041 |
| des ↔ institutional_confidence | -0.058 |
| des ↔ thesis_integrity | +0.061 |
| institutional_confidence ↔ thesis_integrity | +0.109 |
| adoption_momentum ↔ institutional_confidence | -0.110 |
| adoption_momentum ↔ thesis_integrity | +0.114 |
| financial_evolution ↔ thesis_integrity | +0.130 |

Pairs above carry independent signal — these are the structural workhorses of the composite.

## 30-day correlation stability

Snapshots scanned: **30** (window: 2026-04-20 → 2026-05-20)

| pair | mean | min | max | range |
|---|---|---|---|---|
| financial_evolution ↔ institutional_confidence | -0.062 | -0.178 | +0.010 | 0.188 |
| institutional_confidence ↔ thesis_integrity | +0.087 | -0.029 | +0.143 | 0.172 |
| adoption_momentum ↔ institutional_confidence | -0.155 | -0.245 | -0.110 | 0.135 |
| des ↔ institutional_confidence | -0.015 | -0.059 | +0.048 | 0.107 |
| financial_evolution ↔ thesis_integrity | +0.116 | +0.075 | +0.156 | 0.081 |
| adoption_momentum ↔ thesis_integrity | +0.078 | +0.042 | +0.116 | 0.074 |
| des ↔ thesis_integrity | +0.030 | -0.005 | +0.062 | 0.067 |
| adoption_momentum ↔ financial_evolution | +0.684 | +0.669 | +0.723 | 0.054 |
| des ↔ financial_evolution | -0.016 | -0.048 | +0.002 | 0.050 |
| adoption_momentum ↔ des | -0.026 | -0.046 | -0.013 | 0.033 |

All pillar-pair correlations are stable (range < 0.30) over the 30d window.

