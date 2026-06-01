# LTHCS Backtest Engine Report

Window: **2026-02-17 -> 2026-05-29** (72 trading days)
Universe: **209 tickers** | long bands: ['constructive', 'elite', 'high_confidence'] | cost: 5.0 bps/side | delay: 1 td

## Headline P&L (non-overlapping)

| Metric | Value |
|:-------|------:|
| Total return | +0.1818 |
| Annualized return | +0.8091 |
| Annualized Sharpe | +2.448 (95% CI: -0.94 ... +6.64) |
| Annualized Sortino | +2.393 (95% CI: -0.91 ... +7.16) |
| Max drawdown | -0.1058 |
| Hit rate (daily) | 0.597 |
| Avg hold days | 7.5 |
| Avg turnover / day | 0.1954 |
| Total trades | 52 |
| Unique tickers | 20 |

> Non-overlapping construction: every trading day's return is realized on the actual close-to-close of held names. No forward-window reuse, so Sharpe is directly comparable to a passive benchmark.

## Per-band sub-portfolio total return

| Band | Total return |
|:-----|------:|
| elite | +0.0000 |
| high_confidence | +0.4498 |
| constructive | +0.1292 |
| monitor | +0.0826 |
| weakening | +0.0908 |
| review | +0.0145 |

## Benchmark

Benchmark total return: **+0.1109**

## Run metadata

```json
{
  "band_hash": "8fb7e6cc851bd61a",
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
  "price_hash": "5fc2e6221ac35457",
  "profile_name": "long_only_buy",
  "short_bottom_quintile": false,
  "short_set": [],
  "top_k": 0,
  "universe_size": 209,
  "window": {
    "end": "2026-05-29",
    "n_trading_days": 72,
    "start": "2026-02-17"
  }
}
```
