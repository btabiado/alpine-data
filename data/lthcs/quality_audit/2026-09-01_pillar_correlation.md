# LTHCS pillar correlation — 2026-09-01

Snapshot file: `data/lthcs/snapshots/2026-09-01.json` (latest available; today is 2026-09-01).

## 5x5 Pearson correlation matrix

| pillar | adoption_momentum | institutional_confidence | financial_evolution | thesis_integrity | des |
|---|---|---|---|---|---|
| adoption_momentum | +1.000 | -0.005 | — | -0.045 | +0.020 |
| institutional_confidence | -0.005 | +1.000 | — | +0.224 | +0.055 |
| financial_evolution | — | — | — | — | — |
| thesis_integrity | -0.045 | +0.224 | — | +1.000 | +0.063 |
| des | +0.020 | +0.055 | — | +0.063 | +1.000 |

## Near-redundant pillar pairs (|r| >= 0.7)

(none — every pillar pair has |r| < 0.7)

## Near-orthogonal pillar pairs (|r| <= 0.2)

| pair | r |
|---|---|
| adoption_momentum ↔ institutional_confidence | -0.005 |
| adoption_momentum ↔ des | +0.020 |
| adoption_momentum ↔ thesis_integrity | -0.045 |
| des ↔ institutional_confidence | +0.055 |
| des ↔ thesis_integrity | +0.063 |

Pairs above carry independent signal — these are the structural workhorses of the composite.

## 30-day correlation stability

Snapshots scanned: **29** (window: 2026-08-02 → 2026-09-01)

| pair | mean | min | max | range |
|---|---|---|---|---|
| adoption_momentum ↔ des | -0.013 | -0.156 | +0.109 | 0.265 |
| adoption_momentum ↔ institutional_confidence | +0.058 | -0.028 | +0.141 | 0.168 |
| adoption_momentum ↔ thesis_integrity | +0.059 | -0.050 | +0.118 | 0.168 |
| institutional_confidence ↔ thesis_integrity | +0.262 | +0.162 | +0.306 | 0.144 |
| des ↔ institutional_confidence | +0.097 | +0.021 | +0.144 | 0.123 |
| des ↔ thesis_integrity | +0.079 | +0.050 | +0.127 | 0.077 |

All pillar-pair correlations are stable (range < 0.30) over the 30d window.

