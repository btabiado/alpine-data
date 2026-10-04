# LTHCS Backtest — 20261001T115239

Generated: **2026-10-01T11:56:13.712514Z**
- Window: **2026-07-02 -> 2026-09-30**
- Horizon: **21 trading days**
- Universe: **217** tickers across **214** observation dates
- Long bands: ['elite', 'high_confidence', 'constructive']
- Short bands: ['review']

## Band-portfolio P&L

| Metric | Value |
|:-------|------:|
| Rebalances | 88 |
| Cumulative return | -0.3327 |
| Sharpe (annualised) | -1.135 |
| Max drawdown | -0.8730 |
| Hit rate | 0.614 |
| Turnover / rebalance | 0.1228 |
| Avg n_long | 0.0 |
| Avg n_short | 125.7 |

> NOTE: at horizons > 1d, forward returns are overlapping so Sharpe and
> cumulative return are inflated by serial correlation. Treat the IC
> numbers and 1-day Sharpe (if computed) as the honest readings.

## Pillar Information Coefficient (Spearman vs forward return)

| Pillar | IC mean | IC std | IC Sharpe (ann.) | n_obs |
|:-------|--------:|-------:|-----------------:|------:|
| composite | -0.1307 | 0.1816 | -11.426 | 88 |
| thesis_integrity | +0.0329 | 0.0611 | +8.561 | 88 |
| financial_evolution | +0.0000 | 0.0000 | +0.000 | 0 |
| adoption_momentum | -0.0052 | 0.0546 | -1.525 | 88 |
| des | -0.0934 | 0.1515 | -9.790 | 88 |
| institutional_confidence | -0.1259 | 0.2328 | -8.583 | 88 |

## Quintile Q5-Q1 spread (mean across dates)

| Pillar | mean spread | n |
|:-------|------------:|--:|
| adoption_momentum | +0.0022 | 88 |
| institutional_confidence | -0.0232 | 88 |
| financial_evolution | +0.0082 | 88 |
| thesis_integrity | +0.0084 | 88 |
| des | -0.0169 | 88 |

