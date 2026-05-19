# LTHCS Adoption β-Fix Verdict — Spec

**Created:** 2026-05-19
**Author:** swarm agent W (scaffolding)
**Status:** Scaffolding shipped. Verdict gated on 2026-06-17 (~30 trading days post-β-fix).

## Why

Commit `333e5dd` (2026-05-18) shipped the Adoption pillar β fix:

* `_MIN_SECTOR_COHORT` raised 8 → 20
* `_soften_rank_extremes()` applied on the sector-relative path

The fix targets the **21d Adoption Q5-Q1 inversion** documented in
`docs/adoption-pillar-inversion-2026-05-19.md` (Q5-Q1 = -1.4%, t = -4.97).
Expected IC delta is **+0.02 to +0.03 at the 21d horizon**.

Because the IC math needs ~30 trading days of forward returns
(`close[t+21] / close[t] - 1`) to attach to each post-fix observation,
the actual verdict can't be measured until **2026-06-17** at the
earliest. This spec describes the scaffolding that makes the mid-June
verdict a one-command operation rather than a from-scratch rebuild.

## Artifacts

| Path | What |
|---|---|
| `data/lthcs/adaptive_weights/beta_fix_baseline_2026-05-18.json` | Frozen pre-β IC numbers (composite + per-pillar). Sourced from `data/lthcs/backtest/2026-05-19_post_phase5/`. |
| `scripts/lthcs_beta_verdict.py` | CLI: computes post-β IC, compares to baseline, emits markdown verdict. |
| `tests/lthcs/test_beta_verdict.py` | Unit tests covering all four verdict branches + edge cases. |
| `.github/workflows/lthcs-beta-verdict-monthly.yml` | (Staged — not committed.) Monthly cron that runs the verdict and posts to the Job Summary. |

## Frozen baseline (21d horizon)

From `data/lthcs/backtest/2026-05-19_post_phase5/pillar_ic.json`
(window 2026-02-17 → 2026-05-18, n_obs=91, 167 tickers):

| Pillar | IC mean (pre-β) |
|---|---:|
| **composite** | **+0.1218** |
| institutional_confidence | +0.2086 |
| thesis_integrity | +0.0822 |
| financial_evolution | +0.0777 |
| des | +0.0285 |
| **adoption_momentum** | **−0.0130** (inversion) |

The Adoption row is the smoking gun: a negative IC means the pillar
ranks *anti-correlated* with 21d forward returns. The fix is supposed
to move that toward zero (or positive).

## Verdict rules

Classification runs in `lthcs_beta_verdict.classify_verdict` (pure
function, unit-tested):

| Verdict | Condition |
|---|---|
| **PASS** | Composite IC Δ ≥ +0.02 **AND** Adoption IC ≥ 0 **AND** n_obs ≥ 30 |
| **HOLD** | Directional improvement but short of threshold, OR n_obs < 30, OR Adoption improved but still <0 |
| **FAIL** | Composite IC dropped, OR Adoption inversion deepened (post < baseline) |

Order matters: FAIL conditions are checked before the sample-size guard,
so a genuine regression doesn't get masked as "still gathering data".

## Usage

```bash
# Default: last 30 days vs today, 21d horizon, frozen baseline.
python scripts/lthcs_beta_verdict.py

# Explicit window (the canonical mid-June run).
python scripts/lthcs_beta_verdict.py --since 2026-05-18 --end 2026-06-17

# Offline (CI without yfinance network).
python scripts/lthcs_beta_verdict.py --offline

# JSON output for piping to a Slack bot or job summary.
python scripts/lthcs_beta_verdict.py --json-only
```

The script writes
`data/lthcs/adaptive_weights/beta_fix_verdict_<today>.md` and exits 0
on PASS/HOLD, 1 on FAIL. Stdout also shows a compact comparison table
suitable for the GitHub Actions Job Summary.

## Auto-run (staged-not-committed)

`.github/workflows/lthcs-beta-verdict-monthly.yml` (staged only, **not
committed** — CI changes require explicit sign-off per the workflow
rule) runs on the 1st of each month at 08:00 UTC. First viable run is
**2026-06-01**, where the post-β window is ~13 calendar days — n_obs
will likely be too small (HOLD). The **2026-07-01** run is the first
where we expect a definitive verdict.

## Future moves

* Once the verdict lands PASS (or FAIL), archive the baseline JSON
  under `data/lthcs/adaptive_weights/archive/` and stop running the
  cron — keeping it lit forever just creates monthly noise.
* If verdict is HOLD past 2026-07-15, dig into per-pillar deltas in
  the markdown report; the most likely culprit is `n_obs < 30` due to
  weekend/holiday alignment in `fetch_forward_returns`.
* If verdict is FAIL, open a follow-up issue and revert / iterate on
  `lthcs/pillars/adoption.py` (sibling territory — do NOT touch from
  this scaffolding branch).
