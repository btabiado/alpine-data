# LTHCS Backtest Engine Report

Window: **2026-02-17 -> 2026-10-07** (162 trading days)
Universe: **517 tickers** | long bands: ['constructive', 'elite', 'high_confidence'] | cost: 5.0 bps/side | delay: 1 td

## Headline P&L (non-overlapping)

| Metric | Value |
|:-------|------:|
| Total return | +0.1882 |
| Annualized return | +0.3098 |
| Annualized Sharpe | +1.485 (95% CI: -0.91 ... +3.93) |
| Annualized Sortino | +0.973 (95% CI: -0.55 ... +3.04) |
| Max drawdown | -0.1058 |
| Hit rate (daily) | 0.290 |
| Avg hold days | 12.6 |
| Avg turnover / day | 0.1093 |
| Total trades | 68 |
| Unique tickers | 28 |

> Non-overlapping construction: every trading day's return is realized on the actual close-to-close of held names. No forward-window reuse, so Sharpe is directly comparable to a passive benchmark.

## Per-band sub-portfolio total return

| Band | Total return |
|:-----|------:|
| elite | -0.0005 |
| high_confidence | +0.3943 |
| constructive | +0.1352 |
| monitor | +0.0910 |
| weakening | +0.0182 |
| review | +0.1098 |

## Benchmark

Benchmark total return: **+0.1471**

## Run metadata

```json
{
  "band_hash": "2aec0862b80c7495",
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
  "price_hash": "2ee9b26ea11c67d8",
  "profile_name": "long_only_buy",
  "short_bottom_quintile": false,
  "short_set": [],
  "top_k": 0,
  "universe_size": 517,
  "window": {
    "end": "2026-10-07",
    "n_trading_days": 162,
    "start": "2026-02-17"
  }
}
```
