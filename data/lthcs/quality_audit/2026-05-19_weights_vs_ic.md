# LTHCS per-cohort weight audit — current vs IC-implied

**Generated:** 2026-05-19
**Horizon:** 21-trading-day forward return
**Method:** per-date Spearman rank-IC within cohort, Sharpe-aggregated across dates, implied weight ∝ max(0, IC_Sharpe).

> **Critical caveat — Thesis Integrity.** Today's commit `10daa39` migrated Thesis sentiment from AV NEWS_SENTIMENT to Finnhub `/news-sentiment`. The snapshot data used in this audit is **pre-Finnhub** (V1 daily pipeline kept Thesis neutral at 50 for most tickers due to the AV multi-ticker AND-filter quirk). As a result, the Thesis IC measured below understates the framework's realised Thesis signal post-Finnhub. **Re-run this audit ~7 days after Finnhub data accumulates** (~2026-05-26); expect Thesis IC to rise and Thesis weights to need an upward bump in several cohorts.

## Summary table

| Cohort | Verdict | Worst pillar | Gap |
|---|---|---|---:|
| growth_compounder | MISALIGNED | des | +0.174 |
| mature_compounder | MISALIGNED | thesis_integrity | +0.384 |
| standard_compounder | MISALIGNED | institutional_confidence | +0.427 |

## growth_compounder

_n_obs=826, n_dates=59_

| Pillar | Current | IC mean | IC std | IC Sharpe | n | Implied | Gap |
|---|---:|---:|---:|---:|---:|---:|---:|
| adoption_momentum | 0.250 | +0.0857 | 0.2116 | +0.405 | 59 | 0.129 | +0.121 |
| institutional_confidence | 0.200 | +0.2700 | 0.2719 | +0.993 | 59 | 0.315 | -0.115 |
| financial_evolution | 0.150 | +0.0119 | 0.1718 | +0.069 | 59 | 0.022 | +0.128 |
| thesis_integrity | 0.200 | +0.1514 | 0.3009 | +0.503 | 59 | 0.160 | +0.040 |
| des | 0.200 | +0.2196 | 0.1863 | +1.179 | 59 | 0.374 | -0.174 |

**Verdict:** **MISALIGNED** — worst gap on `des` (+0.174)

## mature_compounder

_n_obs=2891, n_dates=59_

| Pillar | Current | IC mean | IC std | IC Sharpe | n | Implied | Gap |
|---|---:|---:|---:|---:|---:|---:|---:|
| adoption_momentum | 0.200 | +0.0778 | 0.1544 | +0.504 | 59 | 0.124 | +0.076 |
| institutional_confidence | 0.200 | -0.0087 | 0.2578 | -0.034 | 59 | 0.000 | +0.200 |
| financial_evolution | 0.200 | +0.1518 | 0.2309 | +0.657 | 59 | 0.162 | +0.038 |
| thesis_integrity | 0.200 | +0.1878 | 0.0792 | +2.373 | 59 | 0.584 | -0.384 |
| des | 0.200 | +0.1227 | 0.2316 | +0.530 | 59 | 0.130 | +0.070 |

**Verdict:** **MISALIGNED** — worst gap on `thesis_integrity` (+0.384)

## standard_compounder

_n_obs=5900, n_dates=59_

| Pillar | Current | IC mean | IC std | IC Sharpe | n | Implied | Gap |
|---|---:|---:|---:|---:|---:|---:|---:|
| adoption_momentum | 0.250 | -0.0376 | 0.1335 | -0.282 | 59 | 0.000 | +0.250 |
| institutional_confidence | 0.200 | +0.0806 | 0.1798 | +0.448 | 59 | 0.627 | -0.427 |
| financial_evolution | 0.150 | +0.0370 | 0.1388 | +0.267 | 59 | 0.373 | -0.223 |
| thesis_integrity | 0.200 | -0.0296 | 0.0701 | -0.422 | 59 | 0.000 | +0.200 |
| des | 0.200 | -0.0482 | 0.1038 | -0.465 | 59 | 0.000 | +0.200 |

**Verdict:** **MISALIGNED** — worst gap on `institutional_confidence` (+0.427)

## Cohorts with insufficient observations

These profiles exist in `weights.json` but the snapshot window did not yield enough (date, ticker) observations for the 21-day forward-return horizon. They are skipped here:

- `btc`
- `eth`
- `layer_1_alt`
- `layer_2`
- `meme`
- `oracle_defi`
- `path_to_profitability`
- `payments`
- `pre_profit_growth`
- `pre_revenue`
- `profitability_inflection`
- `recovery_earnings`
- `recovery_operational`
- `recovery_rerating`
- `recovery_stabilization`
- `sol`

_Crypto cohorts (`btc`/`eth`/`sol`/`layer_1_alt`/`oracle_defi`/`layer_2`/`payments`/`meme`) only have 8 daily snapshots and no cached daily prices — re-audit after ~30 days of crypto snapshot accumulation._
