# LTHCS pillar correlation — 2026-11-01

Snapshot file: `data/lthcs/snapshots/2026-11-01.json` (latest available; today is 2026-11-01).

## 5x5 Pearson correlation matrix

| pillar | adoption_momentum | institutional_confidence | financial_evolution | thesis_integrity | des |
|---|---|---|---|---|---|
| adoption_momentum | +1.000 | -0.082 | +0.625 | — | -0.005 |
| institutional_confidence | -0.082 | +1.000 | -0.092 | — | +0.077 |
| financial_evolution | +0.625 | -0.092 | +1.000 | — | -0.120 |
| thesis_integrity | — | — | — | — | — |
| des | -0.005 | +0.077 | -0.120 | — | +1.000 |

## Near-redundant pillar pairs (|r| >= 0.7)

(none — every pillar pair has |r| < 0.7)

## Near-orthogonal pillar pairs (|r| <= 0.2)

| pair | r |
|---|---|
| adoption_momentum ↔ des | -0.005 |
| des ↔ institutional_confidence | +0.077 |
| adoption_momentum ↔ institutional_confidence | -0.082 |
| financial_evolution ↔ institutional_confidence | -0.092 |
| des ↔ financial_evolution | -0.120 |

Pairs above carry independent signal — these are the structural workhorses of the composite.

## 30-day correlation stability

Snapshots scanned: **31** (window: 2026-10-02 → 2026-11-01)

| pair | mean | min | max | range |
|---|---|---|---|---|
| des ↔ institutional_confidence | +0.104 | -0.116 | +0.187 | 0.303 |
| adoption_momentum ↔ institutional_confidence | -0.011 | -0.091 | +0.055 | 0.146 |
| adoption_momentum ↔ thesis_integrity | +0.048 | -0.024 | +0.094 | 0.118 |
| financial_evolution ↔ institutional_confidence | -0.043 | -0.097 | -0.005 | 0.092 |
| des ↔ financial_evolution | -0.087 | -0.120 | -0.041 | 0.079 |
| adoption_momentum ↔ des | -0.004 | -0.042 | +0.036 | 0.078 |
| adoption_momentum ↔ financial_evolution | +0.610 | +0.589 | +0.636 | 0.047 |
| institutional_confidence ↔ thesis_integrity | +0.101 | +0.078 | +0.117 | 0.040 |
| des ↔ thesis_integrity | +0.090 | +0.080 | +0.108 | 0.028 |
| financial_evolution ↔ thesis_integrity | -0.067 | -0.067 | -0.067 | 0.000 |

**Unstable pairs (range >= 0.30 over 30d):** des↔institutional_confidence

