# LTHCS Backtest Engine Report

Window: **2026-02-17 -> 2026-11-05** (183 trading days)
Universe: **517 tickers** | long bands: ['constructive', 'elite', 'high_confidence'] | cost: 5.0 bps/side | delay: 1 td

## Headline P&L (non-overlapping)

| Metric | Value |
|:-------|------:|
| Total return | +0.2123 |
| Annualized return | +0.3055 |
| Annualized Sharpe | +1.499 (95% CI: -0.68 ... +3.81) |
| Annualized Sortino | +1.041 (95% CI: -0.43 ... +3.10) |
| Max drawdown | -0.1058 |
| Hit rate (daily) | 0.322 |
| Avg hold days | 6.4 |
| Avg turnover / day | 0.1103 |
| Total trades | 341 |
| Unique tickers | 186 |

> Non-overlapping construction: every trading day's return is realized on the actual close-to-close of held names. No forward-window reuse, so Sharpe is directly comparable to a passive benchmark.

## Per-band sub-portfolio total return

| Band | Total return |
|:-----|------:|
| elite | -0.0212 |
| high_confidence | +0.4125 |
| constructive | +0.1628 |
| monitor | +0.1249 |
| weakening | +0.0575 |
| review | +0.0474 |

## Benchmark

Benchmark total return: **+0.0818**

## Run metadata

```json
{
  "band_hash": "1154639b2313b60e",
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
  "price_hash": "c2e907536101bef3",
  "profile_name": "long_only_buy",
  "short_bottom_quintile": false,
  "short_set": [],
  "top_k": 0,
  "universe_size": 517,
  "window": {
    "end": "2026-11-05",
    "n_trading_days": 183,
    "start": "2026-02-17"
  }
}
```
