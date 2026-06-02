# LTHCS Backtest Engine Report

Window: **2026-02-17 -> 2026-06-01** (73 trading days)
Universe: **209 tickers** | long bands: ['constructive', 'elite', 'high_confidence'] | cost: 5.0 bps/side | delay: 1 td

## Headline P&L (non-overlapping)

| Metric | Value |
|:-------|------:|
| Total return | +0.2024 |
| Annualized return | +0.9060 |
| Annualized Sharpe | +2.655 (95% CI: -0.86 ... +6.77) |
| Annualized Sortino | +2.594 (95% CI: -0.84 ... +7.65) |
| Max drawdown | -0.1058 |
| Hit rate (daily) | 0.603 |
| Avg hold days | 7.6 |
| Avg turnover / day | 0.1948 |
| Total trades | 53 |
| Unique tickers | 21 |

> Non-overlapping construction: every trading day's return is realized on the actual close-to-close of held names. No forward-window reuse, so Sharpe is directly comparable to a passive benchmark.

## Per-band sub-portfolio total return

| Band | Total return |
|:-----|------:|
| elite | +0.0000 |
| high_confidence | +0.5461 |
| constructive | +0.1442 |
| monitor | +0.0899 |
| weakening | +0.0965 |
| review | +0.0195 |

## Benchmark

Benchmark total return: **+0.1139**

## Run metadata

```json
{
  "band_hash": "84b3a50dddcfec81",
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
  "price_hash": "740c91e029443b94",
  "profile_name": "long_only_buy",
  "short_bottom_quintile": false,
  "short_set": [],
  "top_k": 0,
  "universe_size": 209,
  "window": {
    "end": "2026-06-01",
    "n_trading_days": 73,
    "start": "2026-02-17"
  }
}
```
