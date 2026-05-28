# LTHCS Backtest Engine Report

Window: **2026-02-17 -> 2026-05-27** (70 trading days)
Universe: **209 tickers** | long bands: ['constructive', 'elite', 'high_confidence'] | cost: 5.0 bps/side | delay: 1 td

## Headline P&L (non-overlapping)

| Metric | Value |
|:-------|------:|
| Total return | +0.1737 |
| Annualized return | +0.7947 |
| Annualized Sharpe | +2.385 (95% CI: -1.17 ... +6.46) |
| Annualized Sortino | +2.365 (95% CI: -1.09 ... +7.08) |
| Max drawdown | -0.1058 |
| Hit rate (daily) | 0.586 |
| Avg hold days | 7.6 |
| Avg turnover / day | 0.1999 |
| Total trades | 51 |
| Unique tickers | 20 |

> Non-overlapping construction: every trading day's return is realized on the actual close-to-close of held names. No forward-window reuse, so Sharpe is directly comparable to a passive benchmark.

## Per-band sub-portfolio total return

| Band | Total return |
|:-----|------:|
| elite | +0.0000 |
| high_confidence | +0.3862 |
| constructive | +0.1252 |
| monitor | +0.0697 |
| weakening | +0.0798 |
| review | +0.0093 |

## Benchmark

Benchmark total return: **+0.1020**

## Run metadata

```json
{
  "band_hash": "68b4e625051c6f30",
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
  "price_hash": "58b1309f80e1be0b",
  "profile_name": "long_only_buy",
  "short_bottom_quintile": false,
  "short_set": [],
  "top_k": 0,
  "universe_size": 209,
  "window": {
    "end": "2026-05-27",
    "n_trading_days": 70,
    "start": "2026-02-17"
  }
}
```
