# LTHCS pillar correlation — 2026-06-01

Snapshot file: `data/lthcs/snapshots/2026-05-31.json` (latest available; today is 2026-06-01).

## 5x5 Pearson correlation matrix

| pillar | adoption_momentum | institutional_confidence | financial_evolution | thesis_integrity | des |
|---|---|---|---|---|---|
| adoption_momentum | +1.000 | -0.028 | +0.674 | +0.026 | -0.038 |
| institutional_confidence | -0.028 | +1.000 | +0.052 | +0.163 | +0.065 |
| financial_evolution | +0.674 | +0.052 | +1.000 | +0.153 | -0.024 |
| thesis_integrity | +0.026 | +0.163 | +0.153 | +1.000 | +0.048 |
| des | -0.038 | +0.065 | -0.024 | +0.048 | +1.000 |

## Near-redundant pillar pairs (|r| >= 0.7)

(none — every pillar pair has |r| < 0.7)

## Near-orthogonal pillar pairs (|r| <= 0.2)

| pair | r |
|---|---|
| des ↔ financial_evolution | -0.024 |
| adoption_momentum ↔ thesis_integrity | +0.026 |
| adoption_momentum ↔ institutional_confidence | -0.028 |
| adoption_momentum ↔ des | -0.038 |
| des ↔ thesis_integrity | +0.048 |
| financial_evolution ↔ institutional_confidence | +0.052 |
| des ↔ institutional_confidence | +0.065 |
| financial_evolution ↔ thesis_integrity | +0.153 |
| institutional_confidence ↔ thesis_integrity | +0.163 |

Pairs above carry independent signal — these are the structural workhorses of the composite.

## 30-day correlation stability

Snapshots scanned: **29** (window: 2026-05-01 → 2026-05-31)

| pair | mean | min | max | range |
|---|---|---|---|---|
| adoption_momentum ↔ thesis_integrity | +0.076 | +0.026 | +0.257 | 0.231 |
| financial_evolution ↔ thesis_integrity | +0.139 | +0.075 | +0.298 | 0.223 |
| adoption_momentum ↔ institutional_confidence | -0.114 | -0.166 | -0.028 | 0.138 |
| des ↔ institutional_confidence | -0.003 | -0.059 | +0.066 | 0.125 |
| financial_evolution ↔ institutional_confidence | -0.013 | -0.068 | +0.053 | 0.121 |
| des ↔ thesis_integrity | +0.043 | -0.041 | +0.071 | 0.112 |
| des ↔ financial_evolution | -0.032 | -0.093 | +0.000 | 0.093 |
| institutional_confidence ↔ thesis_integrity | +0.120 | +0.086 | +0.163 | 0.077 |
| adoption_momentum ↔ financial_evolution | +0.683 | +0.669 | +0.728 | 0.059 |
| adoption_momentum ↔ des | -0.024 | -0.045 | +0.001 | 0.046 |

All pillar-pair correlations are stable (range < 0.30) over the 30d window.

