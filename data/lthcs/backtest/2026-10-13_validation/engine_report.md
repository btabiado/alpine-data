# LTHCS Backtest Engine Report

Window: **2026-02-17 -> 2026-10-13** (166 trading days)
Universe: **517 tickers** | long bands: ['constructive', 'elite', 'high_confidence'] | cost: 5.0 bps/side | delay: 1 td

## Headline P&L (non-overlapping)

| Metric | Value |
|:-------|------:|
| Total return | +0.2063 |
| Annualized return | +0.3318 |
| Annualized Sharpe | +1.585 (95% CI: -0.58 ... +4.08) |
| Annualized Sortino | +1.028 (95% CI: -0.34 ... +3.05) |
| Max drawdown | -0.1058 |
| Hit rate (daily) | 0.307 |
| Avg hold days | 9.5 |
| Avg turnover / day | 0.1086 |
| Total trades | 101 |
| Unique tickers | 56 |

> Non-overlapping construction: every trading day's return is realized on the actual close-to-close of held names. No forward-window reuse, so Sharpe is directly comparable to a passive benchmark.

## Per-band sub-portfolio total return

| Band | Total return |
|:-----|------:|
| elite | +0.0090 |
| high_confidence | +0.4081 |
| constructive | +0.1550 |
| monitor | +0.1048 |
| weakening | +0.0291 |
| review | +0.1097 |

## Benchmark

Benchmark total return: **+0.1483**

## Run metadata

```json
{
  "band_hash": "c1e30841beadc3ab",
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
  "price_hash": "cab76094c723bd17",
  "profile_name": "long_only_buy",
  "short_bottom_quintile": false,
  "short_set": [],
  "top_k": 0,
  "universe_size": 517,
  "window": {
    "end": "2026-10-13",
    "n_trading_days": 166,
    "start": "2026-02-17"
  }
}
```
