# LTHCS Backtest — 20261101T132647

Generated: **2026-11-01T13:35:24.200752Z**
- Window: **2026-08-02 -> 2026-10-31**
- Horizon: **21 trading days**
- Universe: **517** tickers across **245** observation dates
- Long bands: ['elite', 'high_confidence', 'constructive']
- Short bands: ['review']

## Band-portfolio P&L

| Metric | Value |
|:-------|------:|
| Rebalances | 89 |
| Cumulative return | +2.2134 |
| Sharpe (annualised) | +5.343 |
| Max drawdown | -0.5277 |
| Hit rate | 0.584 |
| Turnover / rebalance | 0.1479 |
| Avg n_long | 69.7 |
| Avg n_short | 101.7 |

> NOTE: at horizons > 1d, forward returns are overlapping so Sharpe and
> cumulative return are inflated by serial correlation. Treat the IC
> numbers and 1-day Sharpe (if computed) as the honest readings.

## Pillar Information Coefficient (Spearman vs forward return)

| Pillar | IC mean | IC std | IC Sharpe (ann.) | n_obs |
|:-------|--------:|-------:|-----------------:|------:|
| composite | -0.0254 | 0.1402 | -2.872 | 89 |
| institutional_confidence | +0.0722 | 0.1913 | +5.990 | 89 |
| thesis_integrity | -0.0117 | 0.0875 | -2.118 | 62 |
| adoption_momentum | -0.0468 | 0.0815 | -9.105 | 89 |
| financial_evolution | -0.0955 | 0.0189 | -80.054 | 28 |
| des | -0.1067 | 0.0966 | -17.534 | 88 |

## Quintile Q5-Q1 spread (mean across dates)

| Pillar | mean spread | n |
|:-------|------------:|--:|
| adoption_momentum | -0.0108 | 89 |
| institutional_confidence | +0.0396 | 89 |
| financial_evolution | -0.0054 | 89 |
| thesis_integrity | +0.0026 | 62 |
| des | -0.0207 | 89 |

