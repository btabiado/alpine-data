# LTHCS Backtest Engine Report

Window: **2026-02-17 -> 2026-10-27** (176 trading days)
Universe: **517 tickers** | long bands: ['constructive', 'elite', 'high_confidence'] | cost: 5.0 bps/side | delay: 1 td

## Headline P&L (non-overlapping)

| Metric | Value |
|:-------|------:|
| Total return | +0.2233 |
| Annualized return | +0.3367 |
| Annualized Sharpe | +1.632 (95% CI: -0.32 ... +4.14) |
| Annualized Sortino | +1.110 (95% CI: -0.21 ... +3.35) |
| Max drawdown | -0.1058 |
| Hit rate (daily) | 0.318 |
| Avg hold days | 6.9 |
| Avg turnover / day | 0.1105 |
| Total trades | 256 |
| Unique tickers | 153 |

> Non-overlapping construction: every trading day's return is realized on the actual close-to-close of held names. No forward-window reuse, so Sharpe is directly comparable to a passive benchmark.

## Per-band sub-portfolio total return

| Band | Total return |
|:-----|------:|
| elite | -0.0070 |
| high_confidence | +0.4292 |
| constructive | +0.1733 |
| monitor | +0.1281 |
| weakening | +0.0616 |
| review | +0.1199 |

## Benchmark

Benchmark total return: **+0.1359**

## Run metadata

```json
{
  "band_hash": "e0a1d68d2d38e5b4",
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
  "price_hash": "eca7f3f74ee2d424",
  "profile_name": "long_only_buy",
  "short_bottom_quintile": false,
  "short_set": [],
  "top_k": 0,
  "universe_size": 517,
  "window": {
    "end": "2026-10-27",
    "n_trading_days": 176,
    "start": "2026-02-17"
  }
}
```
