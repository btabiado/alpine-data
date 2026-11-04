# LTHCS Backtest Engine Report

Window: **2026-02-17 -> 2026-11-03** (181 trading days)
Universe: **517 tickers** | long bands: ['constructive', 'elite', 'high_confidence'] | cost: 5.0 bps/side | delay: 1 td

## Headline P&L (non-overlapping)

| Metric | Value |
|:-------|------:|
| Total return | +0.2251 |
| Annualized return | +0.3288 |
| Annualized Sharpe | +1.617 (95% CI: -0.44 ... +4.06) |
| Annualized Sortino | +1.127 (95% CI: -0.29 ... +3.52) |
| Max drawdown | -0.1058 |
| Hit rate (daily) | 0.320 |
| Avg hold days | 6.6 |
| Avg turnover / day | 0.1104 |
| Total trades | 314 |
| Unique tickers | 178 |

> Non-overlapping construction: every trading day's return is realized on the actual close-to-close of held names. No forward-window reuse, so Sharpe is directly comparable to a passive benchmark.

## Per-band sub-portfolio total return

| Band | Total return |
|:-----|------:|
| elite | -0.0260 |
| high_confidence | +0.4159 |
| constructive | +0.1829 |
| monitor | +0.1414 |
| weakening | +0.0749 |
| review | +0.1438 |

## Benchmark

Benchmark total return: **+0.1133**

## Run metadata

```json
{
  "band_hash": "359e776c37d4361a",
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
  "price_hash": "91a1e961bc93d709",
  "profile_name": "long_only_buy",
  "short_bottom_quintile": false,
  "short_set": [],
  "top_k": 0,
  "universe_size": 517,
  "window": {
    "end": "2026-11-03",
    "n_trading_days": 181,
    "start": "2026-02-17"
  }
}
```
