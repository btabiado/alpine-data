# LTHCS Backtest Engine Report

Window: **2026-02-17 -> 2026-10-01** (158 trading days)
Universe: **217 tickers** | long bands: ['constructive', 'elite', 'high_confidence'] | cost: 5.0 bps/side | delay: 1 td

## Headline P&L (non-overlapping)

| Metric | Value |
|:-------|------:|
| Total return | +0.1902 |
| Annualized return | +0.3224 |
| Annualized Sharpe | +1.518 (95% CI: -0.82 ... +4.24) |
| Annualized Sortino | +0.975 (95% CI: -0.51 ... +3.25) |
| Max drawdown | -0.1058 |
| Hit rate (daily) | 0.297 |
| Avg hold days | 12.6 |
| Avg turnover / day | 0.0997 |
| Total trades | 68 |
| Unique tickers | 28 |

> Non-overlapping construction: every trading day's return is realized on the actual close-to-close of held names. No forward-window reuse, so Sharpe is directly comparable to a passive benchmark.

## Per-band sub-portfolio total return

| Band | Total return |
|:-----|------:|
| elite | +0.0000 |
| high_confidence | +0.3950 |
| constructive | +0.1378 |
| monitor | +0.0805 |
| weakening | +0.0147 |
| review | +0.0994 |

## Benchmark

Benchmark total return: **+0.1276**

## Run metadata

```json
{
  "band_hash": "c8e58257166df212",
  "engine_version": "1.0.0",
  "long_set": [
    "constructive",
    "elite",
    "high_confidence"
  ],
  "params": {
    "bands_long": [
      "elite",
      "high_confidence",
      "constructive"
    ],
    "bands_short": [],
    "cost_bps": 5.0,
    "delay_trading_days": 1,
    "initial_capital": 1.0,
    "profile_name": "long_only_buy",
    "rebalance_daily": true,
    "short_bottom_quintile": false,
    "top_k": 0
  },
  "params_hash": "49269b2e937d327d",
  "price_hash": "c42b2430336e95af",
  "profile_name": "long_only_buy",
  "short_bottom_quintile": false,
  "short_set": [],
  "top_k": 0,
  "universe_size": 217,
  "window": {
    "end": "2026-10-01",
    "n_trading_days": 158,
    "start": "2026-02-17"
  }
}
```
