# LTHCS Backtest — 20260801T075509

Generated: **2026-08-01T07:58:42.011188Z**
- Window: **2026-05-02 -> 2026-07-31**
- Horizon: **21 trading days**
- Universe: **217** tickers across **154** observation dates
- Long bands: ['elite', 'high_confidence', 'constructive']
- Short bands: ['review']

## Band-portfolio P&L

| Metric | Value |
|:-------|------:|
| Rebalances | 80 |
| Cumulative return | -0.6071 |
| Sharpe (annualised) | -2.954 |
| Max drawdown | -0.8884 |
| Hit rate | 0.250 |
| Turnover / rebalance | 0.0982 |
| Avg n_long | 3.7 |
| Avg n_short | 104.2 |

> NOTE: at horizons > 1d, forward returns are overlapping so Sharpe and
> cumulative return are inflated by serial correlation. Treat the IC
> numbers and 1-day Sharpe (if computed) as the honest readings.

## Pillar Information Coefficient (Spearman vs forward return)

| Pillar | IC mean | IC std | IC Sharpe (ann.) | n_obs |
|:-------|--------:|-------:|-----------------:|------:|
| composite | -0.1120 | 0.1391 | -12.781 | 80 |
| des | +0.0924 | 0.0710 | +20.661 | 78 |
| thesis_integrity | -0.0062 | 0.0611 | -1.620 | 80 |
| adoption_momentum | -0.0218 | 0.0665 | -5.194 | 80 |
| financial_evolution | -0.0787 | 0.0569 | -21.947 | 30 |
| institutional_confidence | -0.1202 | 0.2235 | -8.534 | 80 |

## Quintile Q5-Q1 spread (mean across dates)

| Pillar | mean spread | n |
|:-------|------------:|--:|
| adoption_momentum | -0.0036 | 80 |
| institutional_confidence | -0.0370 | 80 |
| financial_evolution | +0.0041 | 80 |
| thesis_integrity | -0.0072 | 80 |
| des | +0.0282 | 80 |

