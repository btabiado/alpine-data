# LTHCS Backtest Engine Report

Window: **2026-02-17 -> 2026-08-03** (116 trading days)
Universe: **217 tickers** | long bands: ['constructive', 'elite', 'high_confidence'] | cost: 5.0 bps/side | delay: 1 td

## Headline P&L (non-overlapping)

| Metric | Value |
|:-------|------:|
| Total return | +0.1902 |
| Annualized return | +0.4645 |
| Annualized Sharpe | +1.772 (95% CI: -0.82 ... +4.77) |
| Annualized Sortino | +1.327 (95% CI: -0.59 ... +4.26) |
| Max drawdown | -0.1058 |
| Hit rate (daily) | 0.405 |
| Avg hold days | 12.6 |
| Avg turnover / day | 0.1359 |
| Total trades | 68 |
| Unique tickers | 28 |

> Non-overlapping construction: every trading day's return is realized on the actual close-to-close of held names. No forward-window reuse, so Sharpe is directly comparable to a passive benchmark.

## Per-band sub-portfolio total return

| Band | Total return |
|:-----|------:|
| elite | +0.0000 |
| high_confidence | +0.3950 |
| constructive | +0.1378 |
| monitor | +0.1009 |
| weakening | +0.0568 |
| review | +0.1037 |

## Benchmark

Benchmark total return: **+0.1155**

## Run metadata

```json
{
  "band_hash": "235f6e2e15ed44cf",
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
  "price_hash": "36064dfa80f12cd3",
  "profile_name": "long_only_buy",
  "short_bottom_quintile": false,
  "short_set": [],
  "top_k": 0,
  "universe_size": 217,
  "window": {
    "end": "2026-08-03",
    "n_trading_days": 116,
    "start": "2026-02-17"
  }
}
```
