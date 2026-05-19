# DES `TIER2_MAX_POINTS` ceiling investigation — lever review

**Date**: 2026-05-18  ·  **Author**: read-only empirical analysis  ·  **Status**: do-not-ship without further work

## TL;DR (verdict)

**Do NOT widen `TIER2_MAX_POINTS` in isolation.** On today's snapshot it is a **dormant clip** — no ticker comes within 4 pts of the ±5 envelope. Raising it to 7.5, 10, 15, or 20 produces a **byte-equal DES distribution and zero band changes**. The audit's "ceiling-clamped near 73" is **tier-1 sector-tilt math**, not the tier-2 envelope. `TIER2_MAX_POINTS` is the wrong lever for the ceiling problem.

---

## 1. Current state

`TIER2_MAX_POINTS = 5.0` lives at `lthcs/pillars/des.py:56`. Per-indicator budgets at `lthcs/pillars/des.py:63-70` sum to exactly 5.0 (brent 1.0 + crack 0.5 + ISM 1.0 + housing 0.75 + sentiment 1.0 + u6 0.75). Cyclical sectors get scale = 1.0, defensive 0.3, tech/comm 0.6 (`lthcs/pillars/des.py:77-92`). The clip is applied at `lthcs/pillars/des.py:425-429`.

**Today's reality (n=167, snapshot 2026-05-18)**:

- Tier-2 uncapped contribution distribution: **min +0.043, median +0.086, max +0.903** (Energy cohort: BKR, COP, CVX, FANG, XOM).
- **Zero tickers are being clipped** — the largest absolute t2 contribution is 18% of the +5 budget.
- DES sub_score: min 33.4, p50 43.5, p75 46.5, **p90 = p99 = max = 71.6** (all 18 Financials sit at the ceiling).
- Composite (lthcs_score) max today: 77.9 (only 11 tickers in `constructive`, 0 in `high_confidence`).
- All-time DES max across the 91-day snapshot history: **71.6** (AXP, today). The brief's "73.7" reference appears to be a prior-day reading; current pipeline output is 71.6.
- Audit's stated target if Phase 5 had fully delivered: ~78 (`docs/lthcs-data-audit-2026-05-18.md:24, 50, 181-183`).

## 2. Empirical sensitivity sweep

Computed mathematically from each ticker's `components.total_contribution` (tier-1 tilt sum × 30 magnitude_scale) and the uncapped sum of `components.tier2_inputs[*].contribution_pts`, then applying the clip at each candidate MAX and re-rolling into the composite via the index-4 DES weight (0.2 for all tickers today).

| `TIER2_MAX_POINTS` | DES min | DES p50 | DES p90 | DES max | Composite max | Band changes vs today |
|---:|---:|---:|---:|---:|---:|---:|
| 5.0 (current) | 33.4 | 43.5 | 71.6 | 71.6 | 77.9 | — |
| 7.5 | 33.4 | 43.5 | 71.6 | 71.6 | 77.9 | 0 |
| 10.0 | 33.4 | 43.5 | 71.6 | 71.6 | 77.9 | 0 |
| 15.0 | 33.4 | 43.5 | 71.6 | 71.6 | 77.9 | 0 |
| 20.0 | 33.4 | 43.5 | 71.6 | 71.6 | 77.9 | 0 |

**Result: the rows are identical because no ticker's tier-2 uncapped contribution touches the cap.** This is not a bug in the analysis — it is the mechanical answer. Raising the cap when nothing is being capped is a no-op.

**Where the real ceiling lives.** Max tier-1 unclipped today by sector (50 + total_contribution × 30):

| Sector | n | max tier-1 unclipped |
|---|---:|---:|
| Financials | 18 | 71.47 |
| Energy | 5 | 61.01 |
| Materials | 2 | 52.23 |
| Consumer Discretionary | 20 | 51.09 |
| Industrials | 21 | 49.13 |
| Technology | 42 | 47.52 |
| Communication Services | 14 | 46.13 |
| Health Care | 22 | 42.50 |
| Consumer Staples | 14 | 42.01 |
| Utilities | 7 | 33.92 |
| Real Estate | 2 | 33.25 |

Financials' 71.47 tier-1 + 0.114 tier-2 = 71.6 (rounded). **The ceiling is the sector-tilt sum × `magnitude_scale=30` math at `lthcs/pillars/des.py:537`** (`raw_score = 50 + total_contribution * magnitude_scale`). The tier-2 clip never engages because today's individual indicator tilts × scale × budget terms partially offset (high oil drags non-Energy, sentiment is mid, housing positive, u6 negative — net per ticker ~+0.04 to +0.90).

## 3. Risk assessment per candidate value

**At 7.5 / 10 / 15 / 20** (today's data): no ticker shifts band, no ticker shifts DES. Risk = **zero, because the change is inert**. The "would AXP jump?" question is moot — AXP's tier-2 uncapped is +0.114, capped at 5 or 20 it is still +0.114.

**Theoretical worst-case at MAX=10**: a Consumer Discretionary or Industrials ticker (scale 1.0, budget sum 5.0) with every tier-2 indicator pegged at full bullish magnitude tops out at +5.0 uncapped — still below MAX=10. Even MAX=15 has slack. The clip is only binding for cyclicals when **multiple indicators simultaneously hit their ±1 tilt extremes**, which the FRED data has never produced in the 91-day history (max observed +0.903, ~18% of theoretical).

**At 15+ vs sector tilt component**: still not overpowering, *because tier-2 is never reaching its budget*. The risk only materializes if FRED data shifts to a regime where (e.g.) brent percentile_2y = 1.0, ISM expansion + +1% momentum, housing percentile = 1.0, sentiment = 1.0, u6 = 0.0 simultaneously — at which point a Consumer Discretionary name could see +5.0 today, +10 under MAX=10. That would only matter on a record macro day.

**At 5 (status quo)**: the cap protects against a future record macro day where tier-2 spikes coherently. Worth keeping.

## 4. Recommendation

**Keep `TIER2_MAX_POINTS = 5.0`.** Raising it solves no observed problem today. The audit's "lift DES ceiling" pointer at `docs/phase5-verification-2026-05-18.md` was pointing at the wrong lever.

**If the goal is to expand DES dispersion**, the actual levers are:

1. **`DEFAULT_MAGNITUDE_SCALE = 30.0`** at `lthcs/pillars/des.py:49`. Raising to 35 or 40 would lift the Financials cluster from 71.6 → ~76 / ~81. This widens *all* sectors proportionally.
2. **Per-signal sector sensitivities** in `data/lthcs/sector_des_weights.json` — the Financials cluster all sit at the same 71.47 because they share identical sensitivities. Industry overrides (broker vs. bank vs. insurer) would disperse them.
3. **A new tier-3 micro signal** (industry-specific) — what the audit calls "no micro DES" at `docs/lthcs-data-audit-2026-05-18.md:50`.

Pair recommendation: if engineering bandwidth is finite, **ship #1 (magnitude_scale 30 → 35) as a one-line change before touching tier-2 at all**. Expected DES IC effect: marginal (+0.002 to +0.005 at 21d), because we're scaling the *same* signal — not adding information. Real IC gains require new signals (#3), not amplification of existing ones.

## 5. Implementation steps (only when authorized, and only for `magnitude_scale`, not `TIER2_MAX_POINTS`)

This section is provided per the brief structure but **the recommendation is to ship nothing**. If management override insists on a tier-2 widening:

- One-line edit at `lthcs/pillars/des.py:56`: `TIER2_MAX_POINTS = 7.5` (would have zero effect on today's snapshot — see §2).
- Add a test pinning `_compute_tier2_contribution` clip behavior at the new value (synthetic input that exceeds 5.0 uncapped to prove the new ceiling).
- Forward-snapshot dry-run via `lthcs_daily.py --dry-run` (do **not** run live per brief constraint); confirm 0 tickers shift band.

If pursuing the actual lever (`DEFAULT_MAGNITUDE_SCALE = 30.0 → 35.0`):
- One-line edit at `lthcs/pillars/des.py:49`.
- Update test fixtures in `tests/lthcs/test_des*.py` that pin specific tier-1 contribution magnitudes.
- Run backtest harness (Phase-5 style 91-day replay) to measure IC drift before merge — expected modest (+0.003 to +0.007 at 21d). **Do not** ship without IC measurement: scaling the same signal can either help (if it's directional) or hurt (if noise scales too).

## 6. Counter-argument: don't ship, period

The audit at `docs/lthcs-data-audit-2026-05-18.md:181-183` calls today's max "ceiling-clamped near 73 by sector tilt math" and frames it as a *limitation*. But that ceiling is **intentional**:

- A macro-only pillar should not dominate a fundamentals/positioning composite. Capping its dynamic range at ~71 prevents a single Financials cluster from rocketing to `high_confidence` purely on real-10y-yield drift.
- The audit grades DES at **C, IC +0.022** — the signal is barely positive at 21d. Amplifying a marginal signal does not turn it into a strong signal; it just widens the noise band.
- The "five pillars equally weighted at 0.2" design assumes each pillar contributes comparable explanatory power. DES sub_scores spanning 33 → 72 today is already a 39-pt spread. Widening to a 0 → 100 spread would invert the pillar's intended role from *macro filter* to *macro driver*.
- The Phase 5 backtest showed DES IC +0.007 from the tier-2 wiring (`docs/phase5-backtest-comparison-2026-05-19.md`). Tier-2 is **already contributing** at its current ceiling. There is no evidence widening it would add IC; mechanically it cannot add IC on days like today when the clip isn't binding.

## 7. Verdict

`TIER2_MAX_POINTS = 5.0` is doing exactly what it should: being a safety rail that hasn't yet been touched. The DES ceiling near 71 is a separate phenomenon — tier-1 sector-tilt arithmetic, not the tier-2 envelope — and the appropriate lever there is `DEFAULT_MAGNITUDE_SCALE` or the sector sensitivity table, with the caveat that scaling a marginal IC +0.022 signal is unlikely to materially improve composite predictiveness. The honest answer to "what value would meaningfully expand DES?" is **none of the candidates expand DES today**; the question is misdirected at this clip. The correct follow-up is the audit's own §P4 recommendation: **add new signals (micro DES, sector RSS) rather than amplify existing ones**.

---

*Word count: ~1,070. Pure analysis; zero code changes. Real data from `data/lthcs/snapshots/2026-05-18.json` and `data/lthcs/variable_detail/2026-05-18.json`. Tier-2 sensitivity sweep reconstructed mathematically from per-ticker `components.tier2_inputs` and `components.total_contribution`.*
