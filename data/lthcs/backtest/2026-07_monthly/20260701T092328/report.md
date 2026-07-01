# LTHCS Backtest — 20260701T092328

Generated: **2026-07-01T09:27:01.094970Z**
- Window: **2026-04-01 -> 2026-06-30**
- Horizon: **21 trading days**
- Universe: **217** tickers across **125** observation dates
- Long bands: ['elite', 'high_confidence', 'constructive']
- Short bands: ['review']

## Band-portfolio P&L

| Metric | Value |
|:-------|------:|
| Rebalances | 82 |
| Cumulative return | +159.1405 |
| Sharpe (annualised) | +20.261 |
| Max drawdown | -0.1193 |
| Hit rate | 0.902 |
| Turnover / rebalance | 0.0821 |
| Avg n_long | 6.5 |
| Avg n_short | 82.9 |

> NOTE: at horizons > 1d, forward returns are overlapping so Sharpe and
> cumulative return are inflated by serial correlation. Treat the IC
> numbers and 1-day Sharpe (if computed) as the honest readings.

## Pillar Information Coefficient (Spearman vs forward return)

| Pillar | IC mean | IC std | IC Sharpe (ann.) | n_obs |
|:-------|--------:|-------:|-----------------:|------:|
| composite | +0.1329 | 0.1528 | +13.810 | 82 |
| institutional_confidence | +0.1872 | 0.1389 | +21.392 | 82 |
| thesis_integrity | +0.0452 | 0.0691 | +10.380 | 82 |
| des | +0.0398 | 0.0611 | +10.348 | 80 |
| financial_evolution | +0.0253 | 0.1147 | +3.500 | 61 |
| adoption_momentum | -0.0284 | 0.0692 | -6.510 | 82 |

## Quintile Q5-Q1 spread (mean across dates)

| Pillar | mean spread | n |
|:-------|------------:|--:|
| adoption_momentum | -0.0150 | 82 |
| institutional_confidence | +0.0964 | 82 |
| financial_evolution | +0.0229 | 82 |
| thesis_integrity | +0.0392 | 82 |
| des | +0.0189 | 82 |

