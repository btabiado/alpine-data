# LTHCS Backtest Engine Report

Window: **2026-02-17 -> 2026-10-26** (175 trading days)
Universe: **517 tickers** | long bands: ['constructive', 'elite', 'high_confidence'] | cost: 5.0 bps/side | delay: 1 td

## Headline P&L (non-overlapping)

| Metric | Value |
|:-------|------:|
| Total return | +0.2291 |
| Annualized return | +0.3481 |
| Annualized Sharpe | +1.674 (95% CI: -0.35 ... +4.37) |
| Annualized Sortino | +1.126 (95% CI: -0.21 ... +3.51) |
| Max drawdown | -0.1058 |
| Hit rate (daily) | 0.320 |
| Avg hold days | 6.9 |
| Avg turnover / day | 0.1107 |
| Total trades | 246 |
| Unique tickers | 150 |

> Non-overlapping construction: every trading day's return is realized on the actual close-to-close of held names. No forward-window reuse, so Sharpe is directly comparable to a passive benchmark.

## Per-band sub-portfolio total return

| Band | Total return |
|:-----|------:|
| elite | +0.0055 |
| high_confidence | +0.4395 |
| constructive | +0.1762 |
| monitor | +0.1241 |
| weakening | +0.0533 |
| review | +0.0941 |

## Benchmark

Benchmark total return: **+0.1296**

## Run metadata

```json
{
  "band_hash": "4ced5a24ae8c8bb3",
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
  "price_hash": "80eaa34ee778bb6b",
  "profile_name": "long_only_buy",
  "short_bottom_quintile": false,
  "short_set": [],
  "top_k": 0,
  "universe_size": 517,
  "window": {
    "end": "2026-10-26",
    "n_trading_days": 175,
    "start": "2026-02-17"
  }
}
```
