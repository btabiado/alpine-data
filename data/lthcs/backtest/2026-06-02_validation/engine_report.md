# LTHCS Backtest Engine Report

Window: **2026-02-17 -> 2026-06-02** (74 trading days)
Universe: **209 tickers** | long bands: ['constructive', 'elite', 'high_confidence'] | cost: 5.0 bps/side | delay: 1 td

## Headline P&L (non-overlapping)

| Metric | Value |
|:-------|------:|
| Total return | +0.2412 |
| Annualized return | +1.1082 |
| Annualized Sharpe | +3.006 (95% CI: -0.65 ... +7.17) |
| Annualized Sortino | +2.985 (95% CI: -0.56 ... +8.36) |
| Max drawdown | -0.1058 |
| Hit rate (daily) | 0.608 |
| Avg hold days | 7.6 |
| Avg turnover / day | 0.1942 |
| Total trades | 54 |
| Unique tickers | 21 |

> Non-overlapping construction: every trading day's return is realized on the actual close-to-close of held names. No forward-window reuse, so Sharpe is directly comparable to a passive benchmark.

## Per-band sub-portfolio total return

| Band | Total return |
|:-----|------:|
| elite | +0.0000 |
| high_confidence | +0.5880 |
| constructive | +0.1815 |
| monitor | +0.1063 |
| weakening | +0.0958 |
| review | +0.0162 |

## Benchmark

Benchmark total return: **+0.1154**

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
  "price_hash": "a00ec8148afc0c2b",
  "profile_name": "long_only_buy",
  "short_bottom_quintile": false,
  "short_set": [],
  "top_k": 0,
  "universe_size": 209,
  "window": {
    "end": "2026-06-02",
    "n_trading_days": 74,
    "start": "2026-02-17"
  }
}
```
