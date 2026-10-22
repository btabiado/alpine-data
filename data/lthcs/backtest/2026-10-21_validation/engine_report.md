# LTHCS Backtest Engine Report

Window: **2026-02-17 -> 2026-10-21** (172 trading days)
Universe: **517 tickers** | long bands: ['constructive', 'elite', 'high_confidence'] | cost: 5.0 bps/side | delay: 1 td

## Headline P&L (non-overlapping)

| Metric | Value |
|:-------|------:|
| Total return | +0.2240 |
| Annualized return | +0.3471 |
| Annualized Sharpe | +1.660 (95% CI: -0.48 ... +4.35) |
| Annualized Sortino | +1.110 (95% CI: -0.31 ... +3.52) |
| Max drawdown | -0.1058 |
| Hit rate (daily) | 0.314 |
| Avg hold days | 7.2 |
| Avg turnover / day | 0.1102 |
| Total trades | 196 |
| Unique tickers | 129 |

> Non-overlapping construction: every trading day's return is realized on the actual close-to-close of held names. No forward-window reuse, so Sharpe is directly comparable to a passive benchmark.

## Per-band sub-portfolio total return

| Band | Total return |
|:-----|------:|
| elite | +0.0074 |
| high_confidence | +0.4355 |
| constructive | +0.1704 |
| monitor | +0.1213 |
| weakening | +0.0434 |
| review | +0.1160 |

## Benchmark

Benchmark total return: **+0.1410**

## Run metadata

```json
{
  "band_hash": "a6b8ea3811a4c87d",
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
  "price_hash": "41ab0d8c22358e84",
  "profile_name": "long_only_buy",
  "short_bottom_quintile": false,
  "short_set": [],
  "top_k": 0,
  "universe_size": 517,
  "window": {
    "end": "2026-10-21",
    "n_trading_days": 172,
    "start": "2026-02-17"
  }
}
```
