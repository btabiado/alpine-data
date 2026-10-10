# LTHCS Backtest Engine Report

Window: **2026-02-17 -> 2026-10-09** (164 trading days)
Universe: **517 tickers** | long bands: ['constructive', 'elite', 'high_confidence'] | cost: 5.0 bps/side | delay: 1 td

## Headline P&L (non-overlapping)

| Metric | Value |
|:-------|------:|
| Total return | +0.2054 |
| Annualized return | +0.3349 |
| Annualized Sharpe | +1.589 (95% CI: -0.57 ... +4.05) |
| Annualized Sortino | +1.036 (95% CI: -0.37 ... +3.18) |
| Max drawdown | -0.1058 |
| Hit rate (daily) | 0.299 |
| Avg hold days | 10.5 |
| Avg turnover / day | 0.1089 |
| Total trades | 84 |
| Unique tickers | 43 |

> Non-overlapping construction: every trading day's return is realized on the actual close-to-close of held names. No forward-window reuse, so Sharpe is directly comparable to a passive benchmark.

## Per-band sub-portfolio total return

| Band | Total return |
|:-----|------:|
| elite | +0.0117 |
| high_confidence | +0.4136 |
| constructive | +0.1522 |
| monitor | +0.1026 |
| weakening | +0.0274 |
| review | +0.1137 |

## Benchmark

Benchmark total return: **+0.1491**

## Run metadata

```json
{
  "band_hash": "2bf7bbabf4b7cc32",
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
  "price_hash": "e61457585273db05",
  "profile_name": "long_only_buy",
  "short_bottom_quintile": false,
  "short_set": [],
  "top_k": 0,
  "universe_size": 517,
  "window": {
    "end": "2026-10-09",
    "n_trading_days": 164,
    "start": "2026-02-17"
  }
}
```
