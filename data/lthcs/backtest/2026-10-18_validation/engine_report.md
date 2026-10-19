# LTHCS Backtest Engine Report

Window: **2026-02-17 -> 2026-10-16** (169 trading days)
Universe: **517 tickers** | long bands: ['constructive', 'elite', 'high_confidence'] | cost: 5.0 bps/side | delay: 1 td

## Headline P&L (non-overlapping)

| Metric | Value |
|:-------|------:|
| Total return | +0.2091 |
| Annualized return | +0.3295 |
| Annualized Sharpe | +1.581 (95% CI: -0.64 ... +4.14) |
| Annualized Sortino | +1.050 (95% CI: -0.38 ... +3.19) |
| Max drawdown | -0.1058 |
| Hit rate (daily) | 0.308 |
| Avg hold days | 7.8 |
| Avg turnover / day | 0.1106 |
| Total trades | 162 |
| Unique tickers | 111 |

> Non-overlapping construction: every trading day's return is realized on the actual close-to-close of held names. No forward-window reuse, so Sharpe is directly comparable to a passive benchmark.

## Per-band sub-portfolio total return

| Band | Total return |
|:-----|------:|
| elite | -0.0079 |
| high_confidence | +0.4179 |
| constructive | +0.1580 |
| monitor | +0.1093 |
| weakening | +0.0369 |
| review | +0.1014 |

## Benchmark

Benchmark total return: **+0.1265**

## Run metadata

```json
{
  "band_hash": "bff2720a2e30c0d0",
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
  "price_hash": "31f2c17ac0b5d642",
  "profile_name": "long_only_buy",
  "short_bottom_quintile": false,
  "short_set": [],
  "top_k": 0,
  "universe_size": 517,
  "window": {
    "end": "2026-10-16",
    "n_trading_days": 169,
    "start": "2026-02-17"
  }
}
```
