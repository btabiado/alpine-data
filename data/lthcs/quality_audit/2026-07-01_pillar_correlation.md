# LTHCS pillar correlation — 2026-07-01

Snapshot file: `data/lthcs/snapshots/2026-06-30.json` (latest available; today is 2026-07-01).

## 5x5 Pearson correlation matrix

| pillar | adoption_momentum | institutional_confidence | financial_evolution | thesis_integrity | des |
|---|---|---|---|---|---|
| adoption_momentum | +1.000 | +0.057 | — | +0.102 | +0.003 |
| institutional_confidence | +0.057 | +1.000 | — | +0.272 | +0.005 |
| financial_evolution | — | — | — | — | — |
| thesis_integrity | +0.102 | +0.272 | — | +1.000 | +0.046 |
| des | +0.003 | +0.005 | — | +0.046 | +1.000 |

## Near-redundant pillar pairs (|r| >= 0.7)

(none — every pillar pair has |r| < 0.7)

## Near-orthogonal pillar pairs (|r| <= 0.2)

| pair | r |
|---|---|
| adoption_momentum ↔ des | +0.003 |
| des ↔ institutional_confidence | +0.005 |
| des ↔ thesis_integrity | +0.046 |
| adoption_momentum ↔ institutional_confidence | +0.057 |
| adoption_momentum ↔ thesis_integrity | +0.102 |

Pairs above carry independent signal — these are the structural workhorses of the composite.

## 30-day correlation stability

Snapshots scanned: **24** (window: 2026-05-31 → 2026-06-30)

| pair | mean | min | max | range |
|---|---|---|---|---|
| adoption_momentum ↔ thesis_integrity | +0.093 | -0.042 | +0.283 | 0.325 |
| adoption_momentum ↔ institutional_confidence | -0.005 | -0.116 | +0.131 | 0.247 |
| des ↔ institutional_confidence | -0.031 | -0.121 | +0.105 | 0.226 |
| institutional_confidence ↔ thesis_integrity | +0.261 | +0.113 | +0.327 | 0.214 |
| des ↔ thesis_integrity | +0.057 | -0.044 | +0.135 | 0.179 |
| adoption_momentum ↔ des | +0.022 | -0.038 | +0.120 | 0.158 |
| financial_evolution ↔ thesis_integrity | +0.242 | +0.153 | +0.290 | 0.137 |
| des ↔ financial_evolution | -0.050 | -0.095 | -0.024 | 0.072 |
| financial_evolution ↔ institutional_confidence | +0.047 | +0.010 | +0.080 | 0.070 |
| adoption_momentum ↔ financial_evolution | +0.686 | +0.674 | +0.692 | 0.018 |

**Unstable pairs (range >= 0.30 over 30d):** adoption_momentum↔thesis_integrity

