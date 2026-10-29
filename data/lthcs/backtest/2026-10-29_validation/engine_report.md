# LTHCS Backtest Engine Report

Window: **2026-02-17 -> 2026-10-28** (177 trading days)
Universe: **517 tickers** | long bands: ['constructive', 'elite', 'high_confidence'] | cost: 5.0 bps/side | delay: 1 td

## Headline P&L (non-overlapping)

| Metric | Value |
|:-------|------:|
| Total return | +0.2225 |
| Annualized return | +0.3332 |
| Annualized Sharpe | +1.623 (95% CI: -0.38 ... +4.05) |
| Annualized Sortino | +1.115 (95% CI: -0.26 ... +3.31) |
| Max drawdown | -0.1058 |
| Hit rate (daily) | 0.316 |
| Avg hold days | 6.7 |
| Avg turnover / day | 0.1103 |
| Total trades | 264 |
| Unique tickers | 155 |

> Non-overlapping construction: every trading day's return is realized on the actual close-to-close of held names. No forward-window reuse, so Sharpe is directly comparable to a passive benchmark.

## Per-band sub-portfolio total return

| Band | Total return |
|:-----|------:|
| elite | -0.0136 |
| high_confidence | +0.4184 |
| constructive | +0.1762 |
| monitor | +0.1286 |
| weakening | +0.0607 |
| review | +0.1253 |

## Benchmark

Benchmark total return: **+0.1398**

## Run metadata

```json
{
  "band_hash": "fb71f1055f3f5051",
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
  "price_hash": "bfae6985a503eeb5",
  "profile_name": "long_only_buy",
  "short_bottom_quintile": false,
  "short_set": [],
  "top_k": 0,
  "universe_size": 517,
  "window": {
    "end": "2026-10-28",
    "n_trading_days": 177,
    "start": "2026-02-17"
  }
}
```
