# LTHCS Backtest Engine Report

Window: **2026-02-17 -> 2026-06-03** (75 trading days)
Universe: **209 tickers** | long bands: ['constructive', 'elite', 'high_confidence'] | cost: 5.0 bps/side | delay: 1 td

## Headline P&L (non-overlapping)

| Metric | Value |
|:-------|------:|
| Total return | +0.2468 |
| Annualized return | +1.1197 |
| Annualized Sharpe | +3.046 (95% CI: -0.57 ... +7.20) |
| Annualized Sortino | +3.005 (95% CI: -0.55 ... +8.18) |
| Max drawdown | -0.1058 |
| Hit rate (daily) | 0.613 |
| Avg hold days | 7.4 |
| Avg turnover / day | 0.1937 |
| Total trades | 55 |
| Unique tickers | 22 |

> Non-overlapping construction: every trading day's return is realized on the actual close-to-close of held names. No forward-window reuse, so Sharpe is directly comparable to a passive benchmark.

## Per-band sub-portfolio total return

| Band | Total return |
|:-----|------:|
| elite | +0.0000 |
| high_confidence | +0.5873 |
| constructive | +0.1869 |
| monitor | +0.1014 |
| weakening | +0.0879 |
| review | +0.0133 |

## Benchmark

Benchmark total return: **+0.1076**

## Run metadata

```json
{
  "band_hash": "263c091626fdbaea",
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
  "price_hash": "2b3ebc37a6e03dbb",
  "profile_name": "long_only_buy",
  "short_bottom_quintile": false,
  "short_set": [],
  "top_k": 0,
  "universe_size": 209,
  "window": {
    "end": "2026-06-03",
    "n_trading_days": 75,
    "start": "2026-02-17"
  }
}
```
