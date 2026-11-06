# LTHCS Backtest Engine Report

Window: **2026-02-17 -> 2026-11-04** (182 trading days)
Universe: **517 tickers** | long bands: ['constructive', 'elite', 'high_confidence'] | cost: 5.0 bps/side | delay: 1 td

## Headline P&L (non-overlapping)

| Metric | Value |
|:-------|------:|
| Total return | +0.2475 |
| Annualized return | +0.3606 |
| Annualized Sharpe | +1.739 (95% CI: -0.43 ... +4.14) |
| Annualized Sortino | +1.216 (95% CI: -0.27 ... +3.60) |
| Max drawdown | -0.1058 |
| Hit rate (daily) | 0.324 |
| Avg hold days | 6.4 |
| Avg turnover / day | 0.1105 |
| Total trades | 333 |
| Unique tickers | 183 |

> Non-overlapping construction: every trading day's return is realized on the actual close-to-close of held names. No forward-window reuse, so Sharpe is directly comparable to a passive benchmark.

## Per-band sub-portfolio total return

| Band | Total return |
|:-----|------:|
| elite | -0.0044 |
| high_confidence | +0.4503 |
| constructive | +0.2010 |
| monitor | +0.1630 |
| weakening | +0.0988 |
| review | +0.1159 |

## Benchmark

Benchmark total return: **+0.1317**

## Run metadata

```json
{
  "band_hash": "dde496703efb36b5",
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
  "price_hash": "591d58ab2ef3e3eb",
  "profile_name": "long_only_buy",
  "short_bottom_quintile": false,
  "short_set": [],
  "top_k": 0,
  "universe_size": 517,
  "window": {
    "end": "2026-11-04",
    "n_trading_days": 182,
    "start": "2026-02-17"
  }
}
```
