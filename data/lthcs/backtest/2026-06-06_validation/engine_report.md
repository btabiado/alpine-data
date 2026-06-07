# LTHCS Backtest Engine Report

Window: **2026-02-17 -> 2026-06-05** (77 trading days)
Universe: **209 tickers** | long bands: ['constructive', 'elite', 'high_confidence'] | cost: 5.0 bps/side | delay: 1 td

## Headline P&L (non-overlapping)

| Metric | Value |
|:-------|------:|
| Total return | +0.1590 |
| Annualized return | +0.6312 |
| Annualized Sharpe | +1.887 (95% CI: -1.40 ... +5.92) |
| Annualized Sortino | +1.711 (95% CI: -1.24 ... +5.69) |
| Max drawdown | -0.1058 |
| Hit rate (daily) | 0.597 |
| Avg hold days | 7.4 |
| Avg turnover / day | 0.1917 |
| Total trades | 58 |
| Unique tickers | 22 |

> Non-overlapping construction: every trading day's return is realized on the actual close-to-close of held names. No forward-window reuse, so Sharpe is directly comparable to a passive benchmark.

## Per-band sub-portfolio total return

| Band | Total return |
|:-----|------:|
| elite | +0.0000 |
| high_confidence | +0.2703 |
| constructive | +0.1166 |
| monitor | +0.0684 |
| weakening | +0.0842 |
| review | +0.0134 |

## Benchmark

Benchmark total return: **+0.0831**

## Run metadata

```json
{
  "band_hash": "cfbb21a7a7e7bc5a",
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
  "price_hash": "76a69a9b997fe375",
  "profile_name": "long_only_buy",
  "short_bottom_quintile": false,
  "short_set": [],
  "top_k": 0,
  "universe_size": 209,
  "window": {
    "end": "2026-06-05",
    "n_trading_days": 77,
    "start": "2026-02-17"
  }
}
```
