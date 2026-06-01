# LTHCS Backtest — 20260601T110611

Generated: **2026-06-01T11:09:35.504810Z**
- Window: **2026-03-02 -> 2026-05-31**
- Horizon: **21 trading days**
- Universe: **209** tickers across **102** observation dates
- Long bands: ['elite', 'high_confidence', 'constructive']
- Short bands: ['review']

## Band-portfolio P&L

| Metric | Value |
|:-------|------:|
| Rebalances | 89 |
| Cumulative return | +2830.0124 |
| Sharpe (annualised) | +25.773 |
| Max drawdown | -0.1135 |
| Hit rate | 0.910 |
| Turnover / rebalance | 0.0641 |
| Avg n_long | 7.5 |
| Avg n_short | 69.2 |

> NOTE: at horizons > 1d, forward returns are overlapping so Sharpe and
> cumulative return are inflated by serial correlation. Treat the IC
> numbers and 1-day Sharpe (if computed) as the honest readings.

## Pillar Information Coefficient (Spearman vs forward return)

| Pillar | IC mean | IC std | IC Sharpe (ann.) | n_obs |
|:-------|--------:|-------:|-----------------:|------:|
| composite | +0.1018 | 0.0774 | +20.896 | 89 |
| institutional_confidence | +0.1986 | 0.1511 | +20.859 | 89 |
| thesis_integrity | +0.1080 | 0.0808 | +21.230 | 89 |
| financial_evolution | +0.0632 | 0.0909 | +11.038 | 89 |
| des | +0.0405 | 0.0735 | +8.744 | 89 |
| adoption_momentum | -0.0259 | 0.0774 | -5.316 | 89 |

## Quintile Q5-Q1 spread (mean across dates)

| Pillar | mean spread | n |
|:-------|------------:|--:|
| adoption_momentum | -0.0170 | 89 |
| institutional_confidence | +0.1007 | 89 |
| financial_evolution | +0.0533 | 89 |
| thesis_integrity | +0.0662 | 89 |
| des | +0.0261 | 89 |

