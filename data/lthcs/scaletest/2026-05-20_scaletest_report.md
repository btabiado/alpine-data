# LTHCS Universe Scaletest Report

- Run date: 2026-05-21T01:17:28Z
- Verdict: **NO-GO**
- Reasons: wall clock 5555s exceeds 1800s limit, finnhub hit 1 rate-limit response(s)

## Universe shape

- production_count: 169
- candidate_seed_count: 333
- merged_count: 502
- final_count: 500
- added_from_seed: 331

## Runtime

- Wall clock: 5555.3s
- Peak RSS: 886.6 MB
- Pipeline status: ok

## API call counts

| source | ok | cache_hit | rate_limit | error |
| --- | --- | --- | --- | --- |
| finnhub | 60 | 0 | 1 | 271 |
| sec_edgar | 317 | 6669 | 0 | 0 |
| yahoo | 500 | 0 | 0 | 13 |

## Per-pillar coverage

- adoption_momentum: 97.1%
- des: 100.0%
- financial_evolution: 97.5%
- institutional_confidence: 100.0%
- thesis_integrity: 89.5%

## Cohort population

- standard_compounder: 299
- mature_compounder: 73
- financial: 51
- growth_compounder: 41
- recovery_stabilization: 30
- recovery_rerating: 5
- pre_profit_growth: 1

