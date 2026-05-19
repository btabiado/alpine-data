# Adaptive Weights V2 Prep — 2026-05-19

This directory holds the first run of the new **equity-curve walk-forward
CV** bridge that wires the backtest engine (`lthcs/backtest_engine.py`,
Tier 5 #24 Phases 1–3) into the adaptive-weights gate
(`lthcs/adaptive_weights.py::walk_forward_tune_equity`). It is **plumbing
only** — the promotion gate (`SHIP_MIN_TEST_OBS = 20` non-overlapping
h-day blocks) remains time-locked until ~July 2026.

## Verdict: HOLD across all 5 profiles

The engine baseline at `data/lthcs/backtest/2026-05-18_validation/`
covers 64 trading days (2026-02-17 → 2026-05-18). At 60/40 walk-forward
split that's 38 train / 25 test daily OOS returns. At horizon h=21d the
gate counts **non-overlapping h-day forward blocks**, which is
`floor(25/21) = 1` block — far below the gate of 20. The verdict
therefore fires **HOLD** on every profile, regardless of the (very
high) point Sharpe numbers reported below.

| Profile                  | n_test_daily | n_test_blocks | train_sharpe | test_sharpe | overfit_gap | verdict |
| ------------------------ | -----------: | ------------: | -----------: | ----------: | ----------: | :------ |
| baseline                 |           25 |             1 |       0.6766 |      6.4679 |     -5.7913 | hold    |
| dollar_neutral           |           25 |             1 |       1.4973 |      5.8129 |     -4.3156 | hold    |
| long_buy_short_review    |           25 |             1 |       1.5208 |      4.8422 |     -3.3214 | hold    |
| long_only_buy            |           25 |             1 |       0.6766 |      6.4679 |     -5.7913 | hold    |
| top_k_by_composite       |           25 |             1 |       0.0368 |      8.1854 |     -8.1486 | hold    |

The very high test Sharpe numbers (6-8 annualised) are *exactly* the
artifact we expect when the OOS slice is too short — they are not real
signal, they are point estimates on a single 25-day window dominated by
whatever happened in late April / early May 2026. The whole point of
the gate is to refuse to act on them.

## What this verifies

1. **Wire is correct.** `walk_forward_tune_equity` ingests
   `equity_curve.json` artifacts from the backtest engine end-to-end
   (top-level baseline + all 4 per-profile subdirs).
2. **Schema is sane.** Output includes train/test daily counts, block
   counts, Sharpes, overfit gap, total returns, and an explicit
   `recommendation` field that's one of
   `{"ship","hold","reject","insufficient_data"}`.
3. **Promotion gate fires.** Despite spectacular point Sharpes, all 5
   profiles return HOLD with reason text referencing the time-lock.
4. **No promotion side-effects.** `weights.json::adaptive_overrides.enabled`
   is unchanged; `lthcs/score.py` is unchanged; the daily pipeline is
   unchanged.

## How this flips to SHIP at ~July 2026

The function call signature stays the same. When the engine has rolled
forward to ~250+ daily OOS observations at h=21d (equivalently 20+
non-overlapping 21-day forward blocks), the gate condition flips and
`walk_forward_tune_equity` will start returning `"ship"` for any
profile whose `test_sharpe > 1.0` and `overfit_gap < 1.5`. There is no
code flag to toggle — the verdict is data-gated, not feature-flagged.

Operationally:
- The daily backtest cron (`lthcs-backtest-daily.yml`) keeps rolling
  forward the engine equity curve under `data/lthcs/backtest/<date>/`.
- The monthly weights cron (`lthcs-tune-weights-monthly.yml`) is *not*
  yet calling `walk_forward_tune_equity` (it still runs the IC-based
  `walk_forward_tune`). Adding the equity path to the monthly cron is
  the next ship step — but it's safe to add today because the gate
  ensures no false-positive promotion.

## See also

- `docs/lthcs-backtest-engine-spec.md` §12 — Tier 5 #25 dependency.
- `data/lthcs/adaptive_weights/2026-05-18_walk_forward_after_fixes.md`
  — the IC-path HOLD that established the `SHIP_MIN_TEST_OBS=20`
  contract.
- `lthcs/adaptive_weights.py::walk_forward_tune_equity` — the bridge.
- `tests/lthcs/test_adaptive_weights.py` (`test_walk_forward_tune_equity_*`)
  — the small-OOS-must-HOLD regression guard.
