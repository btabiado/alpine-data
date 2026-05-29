# LTHCS Backtest Engine Report

Window: **2026-02-17 -> 2026-05-28** (71 trading days)
Universe: **209 tickers** | long bands: ['constructive', 'elite', 'high_confidence'] | cost: 5.0 bps/side | delay: 1 td

## Headline P&L (non-overlapping)

| Metric | Value |
|:-------|------:|
| Total return | +0.1794 |
| Annualized return | +0.8112 |
| Annualized Sharpe | +2.436 (95% CI: -1.15 ... +6.49) |
| Annualized Sortino | +2.399 (95% CI: -1.07 ... +7.42) |
| Max drawdown | -0.1058 |
| Hit rate (daily) | 0.592 |
| Avg hold days | 7.5 |
| Avg turnover / day | 0.1981 |
| Total trades | 52 |
| Unique tickers | 20 |

> Non-overlapping construction: every trading day's return is realized on the actual close-to-close of held names. No forward-window reuse, so Sharpe is directly comparable to a passive benchmark.

## Per-band sub-portfolio total return

| Band | Total return |
|:-----|------:|
| elite | +0.0000 |
| high_confidence | +0.3789 |
| constructive | +0.1315 |
| monitor | +0.0744 |
| weakening | +0.0838 |
| review | +0.0132 |

## Benchmark

Benchmark total return: **+0.1081**

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
  "price_hash": "eb22ec5d10175326",
  "profile_name": "long_only_buy",
  "short_bottom_quintile": false,
  "short_set": [],
  "top_k": 0,
  "universe_size": 209,
  "window": {
    "end": "2026-05-28",
    "n_trading_days": 71,
    "start": "2026-02-17"
  }
}
```
