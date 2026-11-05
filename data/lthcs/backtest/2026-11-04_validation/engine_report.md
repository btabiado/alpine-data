# LTHCS Backtest Engine Report

Window: **2026-02-17 -> 2026-11-04** (182 trading days)
Universe: **517 tickers** | long bands: ['constructive', 'elite', 'high_confidence'] | cost: 5.0 bps/side | delay: 1 td

## Headline P&L (non-overlapping)

| Metric | Value |
|:-------|------:|
| Total return | +0.2353 |
| Annualized return | +0.3421 |
| Annualized Sharpe | +1.673 (95% CI: -0.47 ... +4.08) |
| Annualized Sortino | +1.164 (95% CI: -0.29 ... +3.52) |
| Max drawdown | -0.1058 |
| Hit rate (daily) | 0.324 |
| Avg hold days | 11.2 |
| Avg turnover / day | 0.1137 |
| Total trades | 502 |
| Unique tickers | 273 |

> Non-overlapping construction: every trading day's return is realized on the actual close-to-close of held names. No forward-window reuse, so Sharpe is directly comparable to a passive benchmark.

## Per-band sub-portfolio total return

| Band | Total return |
|:-----|------:|
| elite | -0.0107 |
| high_confidence | +0.4445 |
| constructive | +0.1874 |
| monitor | +0.1721 |
| weakening | +0.1059 |
| review | +0.1391 |

## Benchmark

Benchmark total return: **+0.1317**

## Run metadata

```json
{
  "band_hash": "1238ae52bc48300a",
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
  "price_hash": "e676a08b7d1f5924",
  "profile_name": "long_only_buy",
  "short_bottom_quintile": false,
  "short_set": [],
  "top_k": 0,
  "universe_size": 517,
  "window": {
    "end": "2026-11-04",
    "n_trading_days": 182,
    "start": "2026-02-17"
  }
}
```
