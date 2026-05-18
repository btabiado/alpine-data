# Adaptive Weights Walk-Forward CV — 2026-05-18 (AFTER fixes)

Follow-up to `2026-05-18_walk_forward_summary.md`. Three structural bugs in
`lthcs/adaptive_weights.py` (commit 2bcacd3) were fixed; this is the honest
read on the same data window.

**Verdict: HOLD.** The new verdict is HOLD for honest reasons (test sample
size too small after rejecting ffilled forward returns), not the previous
artifact-driven SHIP. `adaptive_overrides.enabled` stays `false`.

---

## 1. What changed

| Bug | Symptom (before) | Fix |
| --- | ---------------- | --- |
| **B1: ffill inflates test_ic** | `fetch_forward_returns` ffills calendar days where `close[t+h]` is missing (price cache ends 2026-05-15). The test split therefore correlated 36 evolving score-panels against the same frozen returns snapshot. | `_build_ffill_mask` detects ffilled cells by consecutive-value-equality per ticker column; `_apply_real_mask` nullifies them. New `n_rejected_ffill` field reports the count. |
| **B2: pillar-scale imbalance** | Raw pillar stds were `[29, 29, 20, 0.0, 6.3]`. The simplex projection on raw ridge coefs collapsed ~99% of the weight onto `thesis_integrity` (the lowest-variance pillar). | Z-score each pillar column on the panel before fitting; un-scale fitted coefs back via `w_raw = w_z / σ`. Degenerate columns (σ < `_STD_FLOOR`) get `w_raw = 0` honestly. |
| **B3: ridge_alpha effectively zero** | `XᵀX` ~10⁶ on raw pillars, so α ∈ {0.1, 5.0} gave identical fits. | After z-scoring, `ZᵀZ` diagonals ≈ `n_obs`. CLI flag is now a unit-free fraction in `[0, ~1]` multiplied by `n_obs` internally; new diagnostic `ridge_alpha_effective` records the actual scalar fed to the normal equation. |
| **B1 follow-up: small-N guard** | Even after B1, n_test_obs=4 at h=21d was being treated as a credible OOS read. | New `SHIP_MIN_TEST_OBS = 20` floor in `_recommendation`: if real OOS cross-sections < 20, downgrade ship → hold regardless of point IC. |

CLI breaking change: `--ridge-alpha 0.5` no longer means "raw α = 0.5 fed to
the normal equation" — it now means "effective α = 0.5 × n_obs". Values in
`[0, 1]` are the sensible range. Old large values (e.g. `1e6`) still hug
the prior. Help text updated.

`compute_lthcs_score`, `lthcs_daily.py`, `app.py`, and the CI workflow are
untouched. The strategic capability remains gated; only the underlying math
moves.

---

## 2. Fixed walk-forward — global, h=21d

Data window: 2026-02-17 → 2026-05-18 (91 score snapshots, 168 tickers, cache
last close 2026-05-15). Split: 60% train (54 dates), 40% test (37 dates).
All runs `--offline` against the cached 2-year price panel.

### α sweep (h=21d)

| ridge_alpha | train_ic | test_ic | overfit_gap | n_train | n_test_real | dominant weights | verdict |
| ----------- | -------- | ------- | ----------- | ------- | ----------- | ---------------- | ------- |
| 0.0  (OLS)  | +0.0922  | +0.1783 | −0.0861     | 6308    | 4           | financial 0.80, institutional 0.20 | HOLD (small N) |
| 0.1         | +0.1079  | +0.1845 | −0.0766     | 6308    | 4           | des 0.46, financial 0.28, institutional 0.23 | HOLD (small N) |
| 0.5 (default) | +0.0819 | +0.1522 | −0.0702   | 6308    | 4           | des 0.56, institutional 0.18, financial 0.15, adoption 0.11 | HOLD (small N) |
| 1.0         | +0.0768  | +0.1420 | −0.0653     | 6308    | 4           | des 0.57, institutional 0.17, financial 0.15, adoption 0.11 | HOLD (small N) |

Key wins:
- α now visibly moves the weight distribution (was identical to 4dp pre-fix).
- `thesis_integrity` correctly gets weight 0.0 across all settings — it
  literally has zero cross-sectional variance over the window (sub-score
  was a constant 50 for almost every date). The pre-fix 0.99 was pure
  artifact of unscaled-column simplex projection.
- `n_train_obs` dropped from 8,964 → 6,308 because the train split also
  rejects ffilled cells (weekend stamps and the 2-3 days where price data
  isn't fully h-trading-days forward observable yet).
- Effective `n_test_obs` collapsed from 36 → 4 once we honestly drop the
  ffilled tail. **2026-04-13 → 2026-04-16 is the actual real-forward test
  window at h=21d.**

### Horizon sweep (α=0.5)

| horizon | train_ic | test_ic | overfit_gap | n_train | n_test_real | verdict |
| ------- | -------- | ------- | ----------- | ------- | ----------- | ------- |
| 1d  | +0.0312 | +0.0471 | −0.0159 | 6308 | 24 | ship (point only — see §3) |
| 5d  | +0.0371 | +0.1390 | −0.1019 | 6308 | 20 | ship (point only — see §3) |
| 21d | +0.0819 | +0.1522 | −0.0702 | 6308 |  4 | **HOLD (small N)** |

The shorter horizons clear `SHIP_MIN_TEST_OBS=20` but still show *negative
overfit gap*, which (after fixes) remains diagnostic of a degenerate test
window — the 5d horizon "real" tail is itself only 20 dates of which many
are calendar-adjacent and likely partially correlated through residual
ffill on intra-week non-trading days. Treating the shorter-horizon SHIPs as
real evidence would be irresponsible. The h=21d HOLD is the primary read.

---

## 3. BEFORE vs AFTER comparison (h=21d, α=0.5)

| Metric | BEFORE (commit 2bcacd3) | AFTER (this fix) |
| ------ | ----------------------- | ---------------- |
| train_ic | +0.0920 | +0.0819 |
| test_ic  | +0.2539 (artifact) | +0.1522 (honest, N=4) |
| overfit_gap | −0.1619 | −0.0702 |
| n_test_obs (reported) | 36 | 4 (real cross-sections) |
| n_rejected_ffill | n/a | 8,183 cells |
| Dominant weight | thesis 0.99 (artifact) | des 0.56 + institutional 0.18 + financial 0.15 + adoption 0.11 |
| α sensitivity | identical results at α ∈ {0.1, 0.5, 1.0, 5.0} | distinct weights at each α |
| Verdict | SHIP (mechanical, not credible) | **HOLD (small-N honest)** |

Note the train_ic dropped slightly (0.092 → 0.082) — the fix is more
conservative on the training fit too, because the train split now also
rejects ffilled (mostly weekend) cells, which slightly reduces n_train_obs
and removes a small amount of redundancy that was inflating train IC.

A useful sanity check: if I re-run with `reject_ffill=False` (Bug 1 fix off
but z-score + α-rescale still on), I get `test_ic = +0.350` with
`n_test_obs = 36`, recommendation SHIP. The 0.350 vs the honest 0.152 is
the magnitude of the Bug 1 inflation.

---

## 4. What's still true after fixes

1. The data window remains too short for a credible OOS read at h=21d.
   We need at least ~2 more months of priced-through forwards before
   the 21d-horizon test split has ≥ 20 real cross-sections.
2. `thesis_integrity` is uninformative on the current window — not
   because the pillar is broken, but because its sub-score happens to be
   constant 50.0 for ~99% of the date×ticker cells. Once that pillar
   actually moves (post Phase-1 rollout activity), it'll re-enter the
   adaptive fit. Until then, the degenerate-column guard correctly zeros
   its weight.
3. The *negative* overfit gap remains a yellow flag at every horizon. At
   h=21d it's smaller post-fix (−0.07 vs −0.16) but the test_ic >
   train_ic pattern shouldn't appear with real signal — it's a
   diagnostic of either small-N test noise or residual sample
   peculiarities in the test window.

---

## 5. Recommendation

**Keep `adaptive_overrides.enabled = false`.** The fixed math is sound; the
sample is not. Re-run this walk-forward at the end of July 2026 (or earlier
if the price cache rolls forward and gives us a full 20+ real test
cross-sections at h=21d). Until then, production stays on curated weights.

---

## 6. Files modified by the fix

- `lthcs/adaptive_weights.py` — three structural fixes + small-sample
  ship-gate guard.
- `tests/lthcs/test_adaptive_weights.py` — 20 new tests covering ffill
  detection, z-score round-trip, α scaling, degenerate-column handling,
  and the small-sample guard. Total suite: 47 tests (was 27), all green.
- `scripts/lthcs_tune_weights.py` — CLI help updated for new
  `--ridge-alpha` semantics; print-summary surfaces new diagnostic fields
  (`n_rejected_ffill`, `ridge_alpha_effective`, `pillar_sigmas_train`,
  `test_dates_after_ffill_reject`).

No source modules (pillars, normalize, score, daily pipeline, app) were
touched. `weights.json::adaptive_overrides` block is unchanged.

## 7. New JSON output for this run

- `data/lthcs/adaptive_weights/20260518T131631Z.json` — global, α=0.5,
  h=21d, HOLD verdict.
