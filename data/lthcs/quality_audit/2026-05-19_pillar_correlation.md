# LTHCS pillar correlation — 2026-05-19

Snapshot file: `data/lthcs/snapshots/2026-05-18.json` (latest available; today is 2026-05-19).

## 5x5 Pearson correlation matrix

| pillar | adoption_momentum | institutional_confidence | financial_evolution | thesis_integrity | des |
|---|---|---|---|---|---|
| adoption_momentum | +1.000 | -0.131 | +0.686 | +0.093 | -0.013 |
| institutional_confidence | -0.131 | +1.000 | -0.025 | +0.127 | -0.033 |
| financial_evolution | +0.686 | -0.025 | +1.000 | +0.144 | -0.014 |
| thesis_integrity | +0.093 | +0.127 | +0.144 | +1.000 | +0.047 |
| des | -0.013 | -0.033 | -0.014 | +0.047 | +1.000 |

## Near-redundant pillar pairs (|r| >= 0.7)

(none — every pillar pair has |r| < 0.7)

## Near-orthogonal pillar pairs (|r| <= 0.2)

| pair | r |
|---|---|
| adoption_momentum ↔ des | -0.013 |
| des ↔ financial_evolution | -0.014 |
| financial_evolution ↔ institutional_confidence | -0.025 |
| des ↔ institutional_confidence | -0.033 |
| des ↔ thesis_integrity | +0.047 |
| adoption_momentum ↔ thesis_integrity | +0.093 |
| institutional_confidence ↔ thesis_integrity | +0.127 |
| adoption_momentum ↔ institutional_confidence | -0.131 |
| financial_evolution ↔ thesis_integrity | +0.144 |

Pairs above carry independent signal — these are the structural workhorses of the composite.

## 30-day correlation stability

Snapshots scanned: **31** (window: 2026-04-18 → 2026-05-18)

| pair | mean | min | max | range |
|---|---|---|---|---|
| institutional_confidence ↔ thesis_integrity | +0.079 | -0.029 | +0.143 | 0.172 |
| financial_evolution ↔ institutional_confidence | -0.072 | -0.180 | -0.020 | 0.160 |
| adoption_momentum ↔ institutional_confidence | -0.162 | -0.245 | -0.123 | 0.122 |
| des ↔ institutional_confidence | -0.011 | -0.059 | +0.048 | 0.107 |
| financial_evolution ↔ thesis_integrity | +0.115 | +0.075 | +0.156 | 0.081 |
| adoption_momentum ↔ thesis_integrity | +0.079 | +0.042 | +0.116 | 0.074 |
| des ↔ thesis_integrity | +0.027 | -0.005 | +0.062 | 0.067 |
| des ↔ financial_evolution | -0.017 | -0.048 | +0.002 | 0.050 |
| adoption_momentum ↔ des | -0.027 | -0.046 | -0.013 | 0.033 |
| adoption_momentum ↔ financial_evolution | +0.682 | +0.669 | +0.692 | 0.022 |

All pillar-pair correlations are stable (range < 0.30) over the 30d window.

