# LTHCS pillar correlation — 2026-08-01

Snapshot file: `data/lthcs/snapshots/2026-07-31.json` (latest available; today is 2026-08-01).

## 5x5 Pearson correlation matrix

| pillar | adoption_momentum | institutional_confidence | financial_evolution | thesis_integrity | des |
|---|---|---|---|---|---|
| adoption_momentum | +1.000 | +0.005 | — | +0.148 | +0.076 |
| institutional_confidence | +0.005 | +1.000 | — | +0.286 | +0.109 |
| financial_evolution | — | — | — | — | — |
| thesis_integrity | +0.148 | +0.286 | — | +1.000 | +0.123 |
| des | +0.076 | +0.109 | — | +0.123 | +1.000 |

## Near-redundant pillar pairs (|r| >= 0.7)

(none — every pillar pair has |r| < 0.7)

## Near-orthogonal pillar pairs (|r| <= 0.2)

| pair | r |
|---|---|
| adoption_momentum ↔ institutional_confidence | +0.005 |
| adoption_momentum ↔ des | +0.076 |
| des ↔ institutional_confidence | +0.109 |
| des ↔ thesis_integrity | +0.123 |
| adoption_momentum ↔ thesis_integrity | +0.148 |

Pairs above carry independent signal — these are the structural workhorses of the composite.

## 30-day correlation stability

Snapshots scanned: **29** (window: 2026-07-02 → 2026-07-31)

| pair | mean | min | max | range |
|---|---|---|---|---|
| institutional_confidence ↔ thesis_integrity | +0.253 | -0.117 | +0.304 | 0.421 |
| des ↔ institutional_confidence | +0.121 | -0.229 | +0.180 | 0.410 |
| adoption_momentum ↔ institutional_confidence | +0.013 | -0.146 | +0.193 | 0.339 |
| adoption_momentum ↔ des | +0.041 | -0.037 | +0.149 | 0.186 |
| adoption_momentum ↔ thesis_integrity | +0.077 | -0.034 | +0.148 | 0.182 |
| des ↔ thesis_integrity | +0.081 | +0.016 | +0.164 | 0.149 |

**Unstable pairs (range >= 0.30 over 30d):** adoption_momentum↔institutional_confidence, institutional_confidence↔thesis_integrity, des↔institutional_confidence

