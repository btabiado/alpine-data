# LTHCS Backtest Engine Report

Window: **2026-02-17 -> 2026-10-12** (165 trading days)
Universe: **517 tickers** | long bands: ['constructive', 'elite', 'high_confidence'] | cost: 5.0 bps/side | delay: 1 td

## Headline P&L (non-overlapping)

| Metric | Value |
|:-------|------:|
| Total return | +0.2058 |
| Annualized return | +0.3332 |
| Annualized Sharpe | +1.587 (95% CI: -0.54 ... +4.12) |
| Annualized Sortino | +1.032 (95% CI: -0.36 ... +3.10) |
| Max drawdown | -0.1058 |
| Hit rate (daily) | 0.303 |
| Avg hold days | 9.8 |
| Avg turnover / day | 0.1087 |
| Total trades | 95 |
| Unique tickers | 52 |

> Non-overlapping construction: every trading day's return is realized on the actual close-to-close of held names. No forward-window reuse, so Sharpe is directly comparable to a passive benchmark.

## Per-band sub-portfolio total return

| Band | Total return |
|:-----|------:|
| elite | +0.0072 |
| high_confidence | +0.4152 |
| constructive | +0.1531 |
| monitor | +0.1053 |
| weakening | +0.0267 |
| review | +0.1107 |

## Benchmark

Benchmark total return: **+0.1484**

## Run metadata

```json
{
  "band_hash": "e2ac7a89e6adec4d",
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
  "price_hash": "1ba8f78d4649282c",
  "profile_name": "long_only_buy",
  "short_bottom_quintile": false,
  "short_set": [],
  "top_k": 0,
  "universe_size": 517,
  "window": {
    "end": "2026-10-12",
    "n_trading_days": 165,
    "start": "2026-02-17"
  }
}
```
