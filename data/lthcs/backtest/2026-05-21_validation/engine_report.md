# LTHCS Backtest Engine Report

Window: **2026-02-17 -> 2026-05-21** (67 trading days)
Universe: **209 tickers** | long bands: ['constructive', 'elite', 'high_confidence'] | cost: 5.0 bps/side | delay: 1 td

## Headline P&L (non-overlapping)

| Metric | Value |
|:-------|------:|
| Total return | +0.1509 |
| Annualized return | +0.7104 |
| Annualized Sharpe | +2.188 (95% CI: -1.56 ... +6.40) |
| Annualized Sortino | +2.151 (95% CI: -1.45 ... +6.89) |
| Max drawdown | -0.1058 |
| Hit rate (daily) | 0.582 |
| Avg hold days | 6.0 |
| Avg turnover / day | 0.1924 |
| Total trades | 49 |
| Unique tickers | 19 |

> Non-overlapping construction: every trading day's return is realized on the actual close-to-close of held names. No forward-window reuse, so Sharpe is directly comparable to a passive benchmark.

## Per-band sub-portfolio total return

| Band | Total return |
|:-----|------:|
| elite | +0.0000 |
| high_confidence | +0.3869 |
| constructive | +0.1034 |
| monitor | +0.0565 |
| weakening | +0.0681 |
| review | +0.0002 |

## Benchmark

Benchmark total return: **+0.0906**

## Run metadata

```json
{
  "band_hash": "369219fb940083df",
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
  "price_hash": "70f3ccd7acb109a7",
  "profile_name": "long_only_buy",
  "short_bottom_quintile": false,
  "short_set": [],
  "top_k": 0,
  "universe_size": 209,
  "window": {
    "end": "2026-05-21",
    "n_trading_days": 67,
    "start": "2026-02-17"
  }
}
```
