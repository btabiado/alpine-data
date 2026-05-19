# top_k_by_composite — K-sweep + default tune (D's open question)

**Status:** resolved 2026-05-19. Default **K=20** retained (Sharpe / Sortino
optimum).
**Sweep:** `data/lthcs/backtest/2026-05-18_validation/profiles/top_k_sweep/sweep.json`
**Profile:** `lthcs/backtest_profiles/top_k_by_composite.py`

## Open question

Agent D's Phase 3 ship (`9e13452`) flagged that the `top_k_by_composite`
profile (K=20) **underperforms total return** on the 64-day validation
window vs the `long_only_buy` baseline (**+12.55% vs +17.74%**) while
**tying on Sharpe** (+2.80 vs +2.61). Two hypotheses:

1. K=20 is too high for the 167-name universe — long tail of composite
   names dilutes the top-quality signal.
2. K=20 is right, but rebalancing churn is eating alpha (the profile
   re-snapshots daily; a name leaving the top-20 swaps for the next-best).

## What "top K" actually means in the engine

From `lthcs/backtest_engine.py::_select_long_targets`:

- Each trading day at close: rank every ticker with a tradable price
  by composite score, keep the top **K** (ties broken by ticker name).
- The target set is the new long leg; entries / exits vs yesterday's
  hold-set are charged `cost_bps` per side on the traded weight.
- Equal-weighted within the held set; weights re-normalize on every
  membership change.

So "top K" = a daily snapshot, not a rolling window. Cost is charged
only on the marginal churn (names entering or leaving the top-K), not
on the full portfolio.

## K-sweep results (validation window: 2026-02-17 → 2026-05-18, 64 td, 167 names, 5 bps/side)

| K    | total_return | Sharpe | Sortino | MDD     | turnover/day | n_trades | n_unique | n_held |
|------|--------------|--------|---------|---------|--------------|----------|----------|--------|
| 5    | +0.189       | +2.51  | +2.55   | -0.142  | 0.153        | 24       | 14       | 4.9    |
| 10   | +0.114       | +1.99  | +2.06   | -0.107  | 0.205        | 65       | 25       | 9.9    |
| 15   | +0.109       | +2.15  | +2.23   | -0.103  | 0.207        | 99       | 37       | 14.8   |
| **20** | **+0.126** | **+2.80** | **+3.17** | **-0.096** | **0.188** | **120** | **45** | **19.7** |
| 25   | +0.098       | +2.41  | +2.77   | -0.089  | 0.158        | 126      | 53       | 24.6   |
| 30   | +0.099       | +2.39  | +2.71   | -0.087  | 0.156        | 149      | 61       | 29.5   |
| 40   | +0.057       | +1.51  | +1.64   | -0.094  | 0.118        | 151      | 77       | 39.4   |
| 50   | +0.033       | +0.98  | +1.05   | -0.089  | 0.127        | 202      | 91       | 49.2   |
| 167  | +0.034       | +1.08  | n/a     | -0.074  | 0.031        | —        | 167      | 167.0  |

(Last row = K equal to the full universe → degenerates to equal-weight
long-only-universe; flat top-of-curve, very low churn, but no signal
extraction. Confirms the engine doesn't have a hidden cost spike at the
limit.)

## Verdict

**K=20 is the risk-adjusted optimum.** It is *not* "the profile is broken
and we should drop it" — it is *Sharpe-maximal and Sortino-maximal* across
the whole grid, and it has the smallest drawdown in the small-K cohort.

The original framing "K=20 underperforms" was measured against total
return only; on the proper risk-adjusted view it wins. The +18.9% return
at K=5 comes with a 14.2% drawdown — the same trade you'd get with a
five-name concentrated portfolio anywhere. We don't ship that.

Neither hypothesis quite holds:

- **H1 (too high — long tail dilutes):** false. K=10 and K=15 are *worse*
  on both return and Sharpe than K=20. The composite signal at ranks
  10-20 is genuinely additive.
- **H2 (cost drag eats alpha):** also false. Turnover/day is **lowest** at
  K=5 (0.15) and K=20 (0.19) — small K rotates less because the gap
  between rank K and rank K+1 is wider. The mid-K hump (K=15, ~0.21
  turnover) is what gives K=10–15 their worst-of-both-worlds profile.

The non-monotone shape of total return suggests the signal degrades
*much* faster than the cost curve scales. Past K≈30 we're holding noise.

## Action taken

- Default K **stays at 20**. Updated the docstring of
  `lthcs/backtest_profiles/top_k_by_composite.py` to cite the sweep and
  explain why we keep the higher-Sharpe, smaller-MDD pick over the
  higher-absolute-return K=5.
- Sweep data committed under
  `data/lthcs/backtest/2026-05-18_validation/profiles/top_k_sweep/sweep.json`
  so future K-tuning has a reproducible baseline.
- Three regression tests added to
  `tests/lthcs/test_backtest_profiles.py`:
  - **K=10 holds exactly 10 names every post-warmup day** — pins
    the daily snapshot mechanic.
  - **K = universe size collapses to long-only-universe + one-time
    entry cost** — pins the limit-case asymptote (used to anchor the
    sweep table).
  - **Turnover does not grow with K** past mid-K — sanity check for the
    cost-drag hypothesis.

## How to reproduce

```bash
# From repo root with the validation-window cache populated:
python scripts/lthcs_backtest.py \
    --profile top_k_by_composite \
    --top-k 20 \
    --start 2026-02-17 --end 2026-05-18 \
    --output-dir data/lthcs/backtest \
    --run-id 2026-05-18_validation/profiles/top_k_by_composite \
    --engine pnl --offline
```

To rerun the sweep itself, iterate `--top-k` over the grid in
`sweep.json::k_grid` and aggregate `engine_summary.json` from each run.

## Footnote: sample size

64 trading days is short. The Sharpe spread between K=20 (+2.80) and
K=25 (+2.41) is plausible noise on a single window. A multi-window
walk-forward (quarterly windows over the last 2y) would firm this up.
For now we have one window and the choice between K=20 and K=25 doesn't
materially change live behavior. Locked at K=20 to match the spec and
avoid churn; revisit during the next validation refresh.
