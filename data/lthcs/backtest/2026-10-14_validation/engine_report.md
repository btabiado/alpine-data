# LTHCS Backtest Engine Report

Window: **2026-02-17 -> 2026-10-14** (167 trading days)
Universe: **517 tickers** | long bands: ['constructive', 'elite', 'high_confidence'] | cost: 5.0 bps/side | delay: 1 td

## Headline P&L (non-overlapping)

| Metric | Value |
|:-------|------:|
| Total return | +0.1992 |
| Annualized return | +0.3175 |
| Annualized Sharpe | +1.532 (95% CI: -0.65 ... +4.16) |
| Annualized Sortino | +1.005 (95% CI: -0.37 ... +3.08) |
| Max drawdown | -0.1058 |
| Hit rate (daily) | 0.305 |
| Avg hold days | 8.9 |
| Avg turnover / day | 0.1085 |
| Total trades | 114 |
| Unique tickers | 68 |

> Non-overlapping construction: every trading day's return is realized on the actual close-to-close of held names. No forward-window reuse, so Sharpe is directly comparable to a passive benchmark.

## Per-band sub-portfolio total return

| Band | Total return |
|:-----|------:|
| elite | -0.0089 |
| high_confidence | +0.4004 |
| constructive | +0.1500 |
| monitor | +0.1004 |
| weakening | +0.0268 |
| review | +0.0926 |

## Benchmark

Benchmark total return: **+0.1358**

## Run metadata

```json
{
  "band_hash": "0c947db80d19dfec",
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
  "price_hash": "d79234b9c519279b",
  "profile_name": "long_only_buy",
  "short_bottom_quintile": false,
  "short_set": [],
  "top_k": 0,
  "universe_size": 517,
  "window": {
    "end": "2026-10-14",
    "n_trading_days": 167,
    "start": "2026-02-17"
  }
}
```
