# LTHCS Backtest — 2026-05-19_post_phase5

Generated: **2026-05-19T03:45:23.269931Z**
- Window: **2026-02-17 -> 2026-05-18**
- Horizon: **21 trading days**
- Universe: **167** tickers across **91** observation dates
- Long bands: ['elite', 'high_confidence', 'constructive']
- Short bands: ['review']

## Band-portfolio P&L

| Metric | Value |
|:-------|------:|
| Rebalances | 91 |
| Cumulative return | +2919.1330 |
| Sharpe (annualised) | +19.441 |
| Max drawdown | -0.1298 |
| Hit rate | 0.813 |
| Turnover / rebalance | 0.0670 |
| Avg n_long | 7.2 |
| Avg n_short | 62.2 |

> NOTE: at horizons > 1d, forward returns are overlapping so Sharpe and
> cumulative return are inflated by serial correlation. Treat the IC
> numbers and 1-day Sharpe (if computed) as the honest readings.

## Pillar Information Coefficient (Spearman vs forward return)

| Pillar | IC mean | IC std | IC Sharpe (ann.) | n_obs |
|:-------|--------:|-------:|-----------------:|------:|
| composite | +0.1218 | 0.1492 | +12.961 | 91 |
| institutional_confidence | +0.2086 | 0.2833 | +11.690 | 91 |
| thesis_integrity | +0.0822 | 0.0758 | +17.211 | 91 |
| financial_evolution | +0.0777 | 0.1016 | +12.143 | 91 |
| des | +0.0285 | 0.1066 | +4.241 | 91 |
| adoption_momentum | -0.0130 | 0.0858 | -2.397 | 91 |

## Quintile Q5-Q1 spread (mean across dates)

| Pillar | mean spread | n |
|:-------|------------:|--:|
| adoption_momentum | -0.0255 | 91 |
| institutional_confidence | +0.1087 | 91 |
| financial_evolution | +0.0513 | 91 |
| thesis_integrity | +0.0588 | 91 |
| des | +0.0179 | 91 |

