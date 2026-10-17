# LTHCS Backtest Engine Report

Window: **2026-02-17 -> 2026-10-08** (163 trading days)
Universe: **517 tickers** | long bands: ['constructive', 'elite', 'high_confidence'] | cost: 5.0 bps/side | delay: 1 td

## Headline P&L (non-overlapping)

| Metric | Value |
|:-------|------:|
| Total return | +0.1953 |
| Annualized return | +0.3198 |
| Annualized Sharpe | +1.528 (95% CI: -0.71 ... +3.91) |
| Annualized Sortino | +0.998 (95% CI: -0.43 ... +3.07) |
| Max drawdown | -0.1058 |
| Hit rate (daily) | 0.294 |
| Avg hold days | 11.2 |
| Avg turnover / day | 0.1090 |
| Total trades | 77 |
| Unique tickers | 36 |

> Non-overlapping construction: every trading day's return is realized on the actual close-to-close of held names. No forward-window reuse, so Sharpe is directly comparable to a passive benchmark.

## Per-band sub-portfolio total return

| Band | Total return |
|:-----|------:|
| elite | -0.0008 |
| high_confidence | +0.4019 |
| constructive | +0.1435 |
| monitor | +0.0988 |
| weakening | +0.0249 |
| review | +0.1166 |

## Benchmark

Benchmark total return: **+0.1422**

## Run metadata

```json
{
  "band_hash": "232387a33a06a49f",
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
  "price_hash": "b42da72dc4bd56f7",
  "profile_name": "long_only_buy",
  "short_bottom_quintile": false,
  "short_set": [],
  "top_k": 0,
  "universe_size": 517,
  "window": {
    "end": "2026-10-08",
    "n_trading_days": 163,
    "start": "2026-02-17"
  }
}
```
