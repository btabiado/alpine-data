# LTHCS Backtest — 20260901T103341

Generated: **2026-09-01T10:37:14.401371Z**
- Window: **2026-06-02 -> 2026-08-31**
- Horizon: **21 trading days**
- Universe: **217** tickers across **184** observation dates
- Long bands: ['elite', 'high_confidence', 'constructive']
- Short bands: ['review']

## Band-portfolio P&L

| Metric | Value |
|:-------|------:|
| Rebalances | 81 |
| Cumulative return | -0.9697 |
| Sharpe (annualised) | -25.401 |
| Max drawdown | -0.9674 |
| Hit rate | 0.025 |
| Turnover / rebalance | 0.1085 |
| Avg n_long | 0.3 |
| Avg n_short | 113.6 |

> NOTE: at horizons > 1d, forward returns are overlapping so Sharpe and
> cumulative return are inflated by serial correlation. Treat the IC
> numbers and 1-day Sharpe (if computed) as the honest readings.

## Pillar Information Coefficient (Spearman vs forward return)

| Pillar | IC mean | IC std | IC Sharpe (ann.) | n_obs |
|:-------|--------:|-------:|-----------------:|------:|
| composite | -0.1058 | 0.2259 | -7.435 | 81 |
| des | +0.0335 | 0.1126 | +4.723 | 79 |
| thesis_integrity | -0.0016 | 0.0681 | -0.364 | 81 |
| adoption_momentum | -0.0119 | 0.0571 | -3.310 | 81 |
| financial_evolution | -0.1144 | 0.0312 | -58.245 | 2 |
| institutional_confidence | -0.1427 | 0.2650 | -8.550 | 81 |

## Quintile Q5-Q1 spread (mean across dates)

| Pillar | mean spread | n |
|:-------|------------:|--:|
| adoption_momentum | -0.0028 | 81 |
| institutional_confidence | -0.0378 | 81 |
| financial_evolution | +0.0109 | 81 |
| thesis_integrity | +0.0043 | 81 |
| des | +0.0083 | 81 |

