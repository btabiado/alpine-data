# LTHCS Backtest Engine Report

Window: **2026-02-17 -> 2026-10-15** (168 trading days)
Universe: **517 tickers** | long bands: ['constructive', 'elite', 'high_confidence'] | cost: 5.0 bps/side | delay: 1 td

## Headline P&L (non-overlapping)

| Metric | Value |
|:-------|------:|
| Total return | +0.2166 |
| Annualized return | +0.3443 |
| Annualized Sharpe | +1.636 (95% CI: -0.52 ... +4.17) |
| Annualized Sortino | +1.074 (95% CI: -0.30 ... +3.23) |
| Max drawdown | -0.1058 |
| Hit rate (daily) | 0.310 |
| Avg hold days | 8.3 |
| Avg turnover / day | 0.1094 |
| Total trades | 146 |
| Unique tickers | 98 |

> Non-overlapping construction: every trading day's return is realized on the actual close-to-close of held names. No forward-window reuse, so Sharpe is directly comparable to a passive benchmark.

## Per-band sub-portfolio total return

| Band | Total return |
|:-----|------:|
| elite | +0.0111 |
| high_confidence | +0.4212 |
| constructive | +0.1651 |
| monitor | +0.1132 |
| weakening | +0.0422 |
| review | +0.1071 |

## Benchmark

Benchmark total return: **+0.1391**

## Run metadata

```json
{
  "band_hash": "cc0a1ca6da6f1f3b",
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
  "price_hash": "8ad787b8f59a8df6",
  "profile_name": "long_only_buy",
  "short_bottom_quintile": false,
  "short_set": [],
  "top_k": 0,
  "universe_size": 517,
  "window": {
    "end": "2026-10-15",
    "n_trading_days": 168,
    "start": "2026-02-17"
  }
}
```
