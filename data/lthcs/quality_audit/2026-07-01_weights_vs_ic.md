# LTHCS per-cohort weight audit — current vs IC-implied

**Generated:** 2026-07-01
**Horizon:** 21-trading-day forward return
**Method:** per-date Spearman rank-IC within cohort, Sharpe-aggregated across dates, implied weight ∝ max(0, IC_Sharpe).

> **Critical caveat — Thesis Integrity.** Today's commit `10daa39` migrated Thesis sentiment from AV NEWS_SENTIMENT to Finnhub `/news-sentiment`. The snapshot data used in this audit is **pre-Finnhub** (V1 daily pipeline kept Thesis neutral at 50 for most tickers due to the AV multi-ticker AND-filter quirk). As a result, the Thesis IC measured below understates the framework's realised Thesis signal post-Finnhub. **Re-run this audit ~7 days after Finnhub data accumulates** (~2026-05-26); expect Thesis IC to rise and Thesis weights to need an upward bump in several cohorts.

## Summary table

| Cohort | Verdict | Worst pillar | Gap |
|---|---|---|---:|

## Cohorts with insufficient observations

These profiles exist in `weights.json` but the snapshot window did not yield enough (date, ticker) observations for the 21-day forward-return horizon. They are skipped here:

- `btc`
- `eth`
- `financial`
- `growth_compounder`
- `layer_1_alt`
- `layer_2`
- `mature_compounder`
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
- `standard_compounder`

_Crypto cohorts (`btc`/`eth`/`sol`/`layer_1_alt`/`oracle_defi`/`layer_2`/`payments`/`meme`) only have 8 daily snapshots and no cached daily prices — re-audit after ~30 days of crypto snapshot accumulation._
