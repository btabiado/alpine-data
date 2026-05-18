# Adaptive Weights Walk-Forward CV — 2026-05-18

**Verdict: HOLD.** Do NOT flip `adaptive_overrides.enabled` to `true`.

The ship gate (`test_ic > 0.04` AND `overfit_gap < 0.04`) is mechanically passing
in every configuration tested, but the underlying numbers are **artifacts of two
structural problems**, not a real signal. Detail below.

---

## 1. CLI runs (all written to `data/lthcs/adaptive_weights/*.json`)

Data window: **2026-02-17 → 2026-05-17** (90 calendar days, 90 score snapshots,
168 tickers). Split: 60% train (54 dates, 2026-02-17..2026-04-11), 40% test
(36 dates, 2026-04-12..2026-05-17). All runs `--offline` to use the cached
2-year price panel; cache last close = 2026-05-15.

### Global walk-forward (horizon=21d)

| ridge_alpha | train_ic | test_ic | overfit_gap | rec   | dominant weight |
| ----------- | -------- | ------- | ----------- | ----- | ---------------- |
| 0.1         | +0.0920  | +0.2539 | −0.1619     | ship* | thesis 0.99      |
| 0.5         | +0.0920  | +0.2539 | −0.1619     | ship* | thesis 0.99      |
| 1.0         | +0.0920  | +0.2539 | −0.1619     | ship* | thesis 0.99      |
| 5.0         | +0.0920  | +0.2539 | −0.1619     | ship* | thesis 0.99      |

\* Recommendation per the script's gate logic, but **not credible** — see §3.

### Global walk-forward, ridge_alpha=0.5, multiple horizons

| horizon | train_ic | test_ic | overfit_gap | rec   | dominant weight |
| ------- | -------- | ------- | ----------- | ----- | ---------------- |
| 1d      | +0.0302  | +0.0627 | −0.0325     | ship* | thesis 1.00      |
| 5d      | +0.0522  | +0.1204 | −0.0682     | ship* | thesis 1.00      |
| 21d     | +0.0920  | +0.2539 | −0.1619     | ship* | thesis 0.99      |

### Per-cohort walk-forward (horizon=21d, ridge_alpha=0.5)

| cohort               | n_tickers | train_ic | test_ic | overfit_gap | dominant weight        |
| -------------------- | --------- | -------- | ------- | ----------- | ---------------------- |
| growth_compounder    | 14        | +0.2421  | +0.3072 | −0.0651     | thesis 0.96, des 0.03  |
| mature_compounder    | 49        | +0.2310  | +0.3253 | −0.0944     | thesis 0.99            |
| standard_compounder  | 101       | +0.0768  | +0.2628 | −0.1861     | thesis 0.99            |
| bank (8 tickers)     | 8         | +0.6680  | +0.7282 | −0.0602     | thesis 0.50, des 0.50  |

Banks: explicit list `BAC,BK,C,GS,JPM,MS,USB,WFC`.

---

## 2. Sample-size honesty

- Train design matrix rows: ~8,964 globally (54 dates × ~166 tickers/day,
  minus per-day NaN drops). Cohort splits: 432–5,400 rows.
- Test split: 36 dates, but **only 5 *unique* 21d-forward cross-sections**
  (forward returns are forward-filled from 2026-04-17 onward because the price
  cache ends 2026-05-15 — the 21-day forward window can only be computed for
  observation dates ≤ 2026-04-17). At 5d horizon, 21 unique; at 1d, 25 unique.
- That means the 36 test "dates" reported by `_composite_ic` are heavily
  redundant. The effective N for the OOS evaluation at 21d is **5 cross-sections**,
  not 36. Treating these as independent inflates apparent IC substantially.

---

## 3. Why the SHIP recommendation is not credible

Two compounding structural issues:

### (a) Forward-return ffill produces redundant test "dates"

`backtest.fetch_forward_returns` forward-fills the most recent observable
forward return onto subsequent calendar days (to support weekend/holiday score
stamps). With end_date = 2026-05-17 and h=21d, the last observation date with
a real 21d forward is ~2026-04-17. Every score date from 2026-04-18 → 2026-05-17
inherits the *same* forward-return cross-section. So the test IC computation
correlates 30+ score panels (which evolve over time) against essentially one
returns snapshot. This inflates test_ic and inverts the train/test gap (test
IC > train IC, i.e. `overfit_gap` is *negative* across the board — that's a
"too-good-to-be-true" signal that the OOS evaluation is degenerate, not that
the model generalizes better than it fits).

### (b) Pillar feature scaling collapses the ridge fit

`_assemble_design_matrix` date-centers the pillar columns but does NOT scale
them. Per-pillar standard deviations after centering:

| pillar                   | std  | abs_mean |
| ------------------------ | ---- | -------- |
| adoption_momentum        | 29.0 | 24.8     |
| institutional_confidence | 29.0 | 25.1     |
| financial_evolution      | 20.1 | 16.5     |
| thesis_integrity         |  1.2 |  0.1     |
| des                      |  6.5 |  4.4     |

The raw ridge coefficients (before simplex projection) are tiny (~0.001
across the board) and three of the five are *negative*. The simplex
projection clips negatives to zero and renormalizes — which artificially
concentrates ~99% of the weight on `thesis_integrity` simply because it's
the column with the smallest positive raw coef among a mostly-negative set.

It is NOT meaningful evidence that thesis_integrity is the most predictive
pillar. It's an artifact of (i) unscaled features in a ridge fit + (ii)
sign-aware simplex projection acting on near-zero noise.

### (c) Ridge_alpha is effectively zero

`XᵀX` has entries on the order of 10⁶–10⁷ (because pillars have stds of
20–30 and we have ~9k rows). Any α ∈ {0.1, 0.5, 1.0, 5.0} is negligible
against that — the four runs return literally identical weights to 4 decimal
places. The current ridge parameterization can't actually regularize against
this dataset; you'd need α ≳ 10⁴ to bend the fit toward the prior.

---

## 4. What I recommend

**HOLD** the adaptive overrides off, and treat the e4ccd27 module as
unproven on this data window. Three things should happen before the next
walk-forward read is even worth running:

1. **Standardize pillar features** in `_assemble_design_matrix` (z-score each
   pillar column by date, or by global std after centering). Without scaling,
   the ridge coefficients have no apples-to-apples interpretation and the
   simplex projection introduces a winner-takes-all bias.
2. **Truncate the test split to dates with *real* forward returns** (or compute
   forward returns only out to `last_cached_close − horizon` trading days).
   Otherwise the test IC measures rank correlation against a frozen snapshot
   rather than realized OOS returns.
3. **Re-derive a meaningful ridge_alpha scale** after standardization, or
   normalize ridge by `n_obs` (e.g. `α · n_obs · I` in the penalty) so the
   knob actually moves the solution between equal-weight and OLS extremes.

Even after those fixes, the agent's prior guidance from e4ccd27 still applies:
60+ trading days is a floor, and 90 days only gives ~63 trading days of which
the last ~21 lack credible 21d forwards. **Real test IC sample size today is
closer to 5 cross-sections than 36.** Wait for 4+ months of history with
priced-through forwards before treating the gate output as a green light.

---

## 5. Per-cohort verdict

- **Bank cohort (test_ic 0.73)** is the most striking apparent SHIP, but it
  has only 8 names × 36 dates × 5 unique forward cross-sections, and the
  weights split is exactly `thesis 0.50 / des 0.50` — both of which are
  low-variance pillars that get amplified by the simplex projection bug.
  Not credible.
- **Growth and mature compounders** show high test ICs (0.31, 0.33) but the
  *negative* overfit gap is again diagnostic of the ffill issue.
- **Standard compounder** has the largest train→test inflation (−0.19), most
  consistent with the structural artifacts.

No cohort produces a clean, defensible recommendation today.

---

## 6. Files written

- `data/lthcs/adaptive_weights/20260518T100536Z.json` — global, α=0.1, h=21
- `data/lthcs/adaptive_weights/20260518T100547Z.json` — global, α=0.5, h=21
- `data/lthcs/adaptive_weights/20260518T100552Z.json` — global, α=1.0, h=21
- `data/lthcs/adaptive_weights/20260518T100555Z.json` — global, α=5.0, h=21
- `data/lthcs/adaptive_weights/20260518T100608Z.json` — global, α=0.5, h=1
- `data/lthcs/adaptive_weights/20260518T100612Z.json` — global, α=0.5, h=5
- `data/lthcs/adaptive_weights/20260518T100631Z.json` — growth_compounder
- `data/lthcs/adaptive_weights/20260518T100633Z.json` — mature_compounder
- `data/lthcs/adaptive_weights/20260518T100639Z.json` — standard_compounder
- `data/lthcs/adaptive_weights/20260518T100641Z.json` — bank cohort

`weights.json::adaptive_overrides` is **unchanged**. The `weights` block was
already pre-populated as equal-weight on 2026-05-18, which is the correct
fallback while we HOLD.
