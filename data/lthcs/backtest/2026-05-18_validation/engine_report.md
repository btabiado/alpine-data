# LTHCS Backtest Engine Report

Window: **2026-02-17 -> 2026-05-19** (65 trading days)
Universe: **167 tickers** | long bands: ['constructive', 'elite', 'high_confidence'] | cost: 5.0 bps/side | delay: 1 td

## Headline P&L (non-overlapping)

| Metric | Value |
|:-------|------:|
| Total return | +0.1544 |
| Annualized return | +0.7600 |
| Annualized Sharpe | +2.271 (95% CI: -1.45 ... +6.64) |
| Annualized Sortino | +2.226 (95% CI: -1.30 ... +6.98) |
| Max drawdown | -0.1058 |
| Hit rate (daily) | 0.585 |
| Avg hold days | 6.0 |
| Avg turnover / day | 0.1957 |
| Total trades | 49 |
| Unique tickers | 19 |

> Non-overlapping construction: every trading day's return is realized on the actual close-to-close of held names. No forward-window reuse, so Sharpe is directly comparable to a passive benchmark.

## Per-band sub-portfolio total return

| Band | Total return |
|:-----|------:|
| elite | +0.0000 |
| high_confidence | +0.3869 |
| constructive | +0.1067 |
| monitor | +0.0399 |
| weakening | +0.0451 |
| review | -0.0121 |

## Benchmark

Benchmark total return: **+0.0774**

## Run metadata

```json
{
  "band_hash": "0744bd7a193e52f0",
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
  "price_hash": "439920e2623ef29b",
  "profile_name": "long_only_buy",
  "short_bottom_quintile": false,
  "short_set": [],
  "top_k": 0,
  "universe_size": 167,
  "window": {
    "end": "2026-05-19",
    "n_trading_days": 65,
    "start": "2026-02-17"
  }
}
```
