# LTHCS Tuning Kit — minimal-touch recalibration

_Companion to queue item #4 (`docs/lthcs-followups-queue.md`). For diagnosis (is this calibration vs. real signal vs. missing data?) see `scripts/lthcs_diagnose.py` and `docs/lthcs-diagnostic-runbook.md`. This doc starts AFTER diagnosis has pointed at calibration._

---

## TL;DR

- The model has **four config-only tuning surfaces**: pillar `weights.json`, `sector_des_weights.json` (sectors + ticker overrides + signal bounds + magnitude_scale), modifier thresholds in `lthcs/score.py`, and band thresholds in `weights.json`.
- A symptom-to-lever decision table is below. Most calibration problems are a 10-20% nudge on one signal — not a structural rewrite.
- **Always preview first.** Run `scripts/lthcs_tune_preview.py` against the latest snapshot before editing any config. The script writes nothing — it only prints the predicted before/after.
- Bump `model_version` per §1.4 of the white paper when the config changes. Old snapshots stay valid because each snapshot records the `model_version` that produced it.
- Don't tune to make the distribution look pretty. If a band is empty for a real reason (e.g. the universe genuinely has no Elite-tier names this week), leave it.

---

## The 4 tuning surfaces

### 1. `data/lthcs/weights.json` — pillar weights and bands

**What it controls.** Two things:
1. The 5-element pillar weight vector for each maturity stage (`profiles.*`). These are the multipliers applied to each pillar sub-score before summing. Each profile must sum to 1.0.
2. The score band cutoffs (`score_bands`). Integer-bounded — `weakening = 50..59`, `constructive = 70..79`, etc. The lookup is `floor(score)` to handle the one-decimal precision of composites.

**When to touch the weight profiles.**

- Symptom: a particular maturity stage is producing scores that systematically miss the band you expect. Example — `recovery_stabilization` names (legacy industrials post-COVID) are scoring 65-70 but you have strong priors they should be 75+. The Financial-Evolution weight there is `0.35` (highest in any profile by design). If margin and OCF data is real and the issue is that the *qualitative* recovery thesis isn't being captured, shifting weight 5-10pp from `financial_evolution` → `thesis_integrity` is a minimal-impact move.
- Symptom: a stage's composites are noisy (large 1d drifts). Often this means a high-weight pillar has volatile inputs. Reducing that pillar's weight by 5pp and redistributing to lower-weight pillars dampens the drift.

**When to touch bands.**

- Symptom: the entire universe is shifted relative to the white paper's intended distribution (e.g. universe median is 62 but the spec assumes ~70). If the underlying signal mix is right, sliding band thresholds down 5 points (so `constructive = 65..74`, `high = 75..84`, etc.) is a "cosmetic" recalibration that doesn't restate any sub-score.
- **Don't do this** to "fix" an empty Elite tier. If 0/167 names truly qualify for Elite at the spec'd 90+ threshold, that's accurate information. Reach for bands only when a *systemic* offset is visible across the whole universe.

**Minimal-impact move (5-10pp redistribution within one profile):**

```json
// before
"standard_compounder": [0.25, 0.20, 0.15, 0.20, 0.20],
// after — moved 5pp from des → thesis_integrity
"standard_compounder": [0.25, 0.20, 0.15, 0.25, 0.15],
```

Each profile vector still sums to 1.0 (the spec validator will reject otherwise). Only the affected stage's tickers re-score; everything else is unchanged.

**Structural move:** changing the `pillar_order`, adding a new pillar, or restating the band labels themselves. This is a major-version bump (see §The model_version bump playbook below).

---

### 2. `data/lthcs/sector_des_weights.json` — DES sensitivities

**What it controls.**

- `signal_normalization` — per-macro-signal `[low, neutral, high]` bounds that map a raw value to a tilt in `[-1, +1]`. E.g. `fed_funds_pct: {low: 0, high: 6, neutral: 2.5}` means today's 4.5% Fed Funds maps to `(4.5 - 0)/(6 - 0) * 2 - 1 = +0.50` tilt.
- `sectors.<Sector>.<signal>` — sensitivity in `[-1, +1]`. Positive = sector benefits from higher values of that signal. Combined with the tilt: `contribution = sensitivity × tilt`. Sum all signal contributions, multiply by `magnitude_scale` (default 30), add to baseline 50.0 → that's the DES sub-score.
- `ticker_overrides.<TICKER>.<signal>` — replaces the sector's sensitivity for that specific signal for that ticker. Partial overrides are allowed (override just `fed_funds_pct`, inherit everything else from the sector).
- `magnitude_scale` — implicit `DEFAULT_MAGNITUDE_SCALE = 30.0` in `lthcs/pillars/des.py`. Caller can override per-call. Reducing this to 20 compresses DES contributions; raising to 40 amplifies them.

**When to touch sector sensitivities.**

- Symptom: a whole sector clusters in `monitor` or `weakening` despite real-signal pillars looking fine. Check the DES sub-scores — if they're all 35-45, the macro regime is mechanically dragging them. If you believe the sector is genuinely less rate-sensitive than the default assumes (current AI-capex Tech story is the canonical example), soften the sensitivities.
- Already done in V1 for Information Technology and Communication Services (softened 2026-05-17, see `_note_v2` keys). Pattern: cut the magnitude by ~30-50% on `fed_funds_pct`, `ten_y_yield_pct`, `ten_y_30d_change_bp`.

**When to use ticker_overrides instead.**

- Use ticker_overrides when a *specific name* doesn't behave like its sector. EV automakers (TSLA, LCID) under Consumer Discretionary are oil-positive while the sector is oil-negative. AI infra names (NVDA, AVGO, AMD, MU, MSFT, GOOG, META) are softened further than the Tech sector default.
- The override block in `sector_des_weights.json` already documents the pattern. Don't override > ~20% of a sector — at that point you're saying the sector definition itself is wrong, and you should consider re-classifying or splitting the sector.

**When to touch `magnitude_scale`.**

- Symptom: DES sub-scores are too volatile (move 20+ points period-over-period when macro moves a little). Drop `magnitude_scale` from 30 to 25 or 20.
- Symptom: DES sub-scores are anemic (cluster in 45-55 even when macro is clearly tilted). Raise to 35 or 40.
- This is a *global* dial — it scales every DES contribution proportionally. Use sparingly.

**Minimal-impact move (single sector signal nudge):**

```json
// before
"Information Technology": {
  "fed_funds_pct":      -0.22,
  "ten_y_yield_pct":    -0.22,
  ...
}
// after — softened another step
"Information Technology": {
  "fed_funds_pct":      -0.15,
  "ten_y_yield_pct":    -0.15,
  ...
}
```

**Structural move:** introducing a new signal (e.g. M2 money supply) or adding a new sector. Adding a signal means it lives in `signal_normalization` AND every sector block needs a sensitivity for it. That's a minor-version bump even though it's still config-only.

---

### 3. Modifier thresholds — in `lthcs/score.py`

**What it controls.** Two universal modifiers applied AFTER pillar weighting:

```python
_MACRO_THRESHOLD_BP = 25.0          # |10Y 30d change| > 25bp triggers ±2.0
_MACRO_MAGNITUDE = 2.0
_VOLATILITY_PERCENTILE = 90.0       # vol > 90th pct triggers -3.0
_VOLATILITY_MAGNITUDE = -3.0
```

These are currently hardcoded constants. The `weights.json` `modifiers` block documents them but doesn't override them — the code path is the source of truth.

**When to touch.**

- Symptom: macro_adj is firing on every ticker in volatile regimes (so it's not actually adding information — it's just an offset). Raise the threshold to 35bp.
- Symptom: volatility_modifier is too sticky (the same 17 names get -3.0 every day even though the universe is uniformly volatile). Tighten to 95th percentile, or lower magnitude to -2.0.

**Recommended config extraction (optional):** lift these to `data/lthcs/modifiers.json`:

```json
{
  "version": "1.0.0",
  "macro_adjustment": {
    "threshold_bp": 25.0,
    "magnitude": 2.0
  },
  "volatility_modifier": {
    "percentile_threshold": 90.0,
    "magnitude": -3.0
  }
}
```

Then `compute_macro_adjustment` and `compute_volatility_modifier` accept those as kwargs (default to current constants when the file is absent). This is the LAST tuning lever to extract — it's lowest-frequency, and shipping the doc is more useful than shipping the file.

**Minimal-impact move:** raise the macro threshold from 25 → 30 (won't change much in normal regimes, suppresses noise in rate-volatile periods).

**Structural move:** changing the modifier *shape* — e.g. making volatility a continuous penalty instead of a step function, or adding a third modifier. Major version.

---

### 4. `data/lthcs/universe.json` — maturity stage tags

**What it controls.** Each ticker has a `maturity_stage` tag that picks which profile in `weights.json` applies to it. Changing the tag changes the pillar weighting (and nothing else).

**When to touch.**

- Symptom: a specific ticker scores in the wrong band because the wrong stage is applied. Example: a `recovery_stabilization` name has stabilized and is now a normal compounder — its 0.35 weight on Financial Evolution is artificially boosting its score. Move it to `standard_compounder`.
- Symptom: a `pre_profit_growth` name has reached profitability — move to `profitability_inflection` or `standard_compounder`.

**Minimal-impact move:** retag a single ticker. Score change for that ticker is bounded by the maximum profile delta (typically < 8 points unless the name has very lopsided pillar sub-scores).

**Structural move:** redefining what a maturity stage *means* (e.g. adding a new stage, deprecating `recovery_stabilization`). Touch all impacted tickers in one PR, document in the changelog, bump minor version.

---

## Symptom-to-lever decision table

Match the symptom from the left column. The "magnitude" column is the recommended initial nudge — preview, observe, iterate.

| Observed symptom | Most likely lever | Magnitude of nudge | Risk |
|---|---|---|---|
| Top tier (high/elite) is empty, math caps at ~78 | Composite renorm or pillar weights — drop a stubbed pillar's weight from the documented profile and let the V1 dropped-pillar renorm carry it (already shipped — verify `data_quality_flags` is populated) | n/a, code-level | low |
| Whole sector clusters in `monitor`/`weakening` despite real pillar data | `sector_des_weights.json` — soften that sector's rate sensitivities | -10% to -25% on rate-sensitive signals | low |
| Banks score artificially low | Financial pillar renorm (V1 bug; already shipped) | code change, not config | n/a |
| Mid-cap growth dominates top of universe | Peer-group fix (already shipped for maturity-stage); consider sector-relative for revenue (queue item #3) | code change | medium |
| A specific ticker keeps falling into Review | `universe.json` `maturity_stage` retag OR `ticker_overrides` in `sector_des_weights.json` | reclassify or override | medium |
| Whole universe trended down 10 pts vs. last week | Macro modifier or DES magnitude — likely real macro shift, NOT a tuning fix | preview only | low |
| Universe median is 62, spec assumed 70 (systemic offset) | `weights.json.score_bands` — shift down 5pts | -5 across all bands | low-med |
| DES sub-scores swinging 15+ points daily | `magnitude_scale` 30 → 20-25 | -25-33% | low |
| Volatility modifier hits the same 15 names every day | Tighten percentile threshold 90 → 95 OR drop magnitude 3.0 → 2.0 | code edit or modifiers.json | low |
| Macro modifier never fires even in clearly rate-driven regimes | Threshold too loose — narrow 25bp → 15-20bp | code edit or modifiers.json | medium |
| AI infra names (NVDA/AVGO/MSFT) scoring same as old-line software | Add/tighten `ticker_overrides` for the relevant names | -0.05 to -0.10 per signal | low |
| `recovery_*` names score too high because their stage profile over-weights Financial Evolution | Reclassify (universe.json) when the recovery is complete | per-ticker, 1 line of JSON | low |

---

## The model_version bump playbook (per white paper §1.4)

Every snapshot has a `model_version` field at the top. Original snapshots are preserved — git history is the audit log. Old snapshots stay valid because they record the model_version that produced them. The bump rule below tells callers what they're comparing against when they look at two snapshots from different versions.

### v1.0.0 → v1.0.1 (PATCH) — bug fix, no scoring intent change

Use when:
- A bug in `assign_band` or `compute_drift` produced wrong results that you've now corrected.
- A data-quality flag was being miswritten and the renorm path didn't activate.
- Snapshots produced *before* the fix may have wrong scores — but you don't restate them. You note the bug fix in the changelog, bump to v1.0.1 on the NEXT daily snapshot, and downstream consumers can filter by `model_version >= v1.0.1` if they care.

### v1.0.0 → v1.1.0 (MINOR) — config tweak, no restatement

Use when:
- A sector sensitivity softened (the 2026-05-17 Tech/Communication Services softening was a v1.0.0 → v1.1.0 candidate, but was bundled as part of the calibration ship and kept at 1.0.0).
- A `magnitude_scale` change.
- A band threshold shift.
- New ticker overrides added.
- A new maturity stage added (NOT removed — removing requires re-tagging which is structural).
- New macro signal wired in (config + pipeline change).

The behavior: old snapshots reflect old config and remain valid for historical comparison. The first snapshot under the new minor version annotates the change in commit history. The dashboard's drift columns should be interpreted with caution across a minor-version boundary — a 1d drift that spans the bump combines real signal change with the recalibration.

### v1.0.0 → v2.0.0 (MAJOR) — model-shape change, restatement implied

Use when:
- Adding a NEW pillar (e.g. ESG, governance) to the 5-pillar set.
- Replacing the Thesis source (Alpha Vantage NEWS_SENTIMENT → something else with different bounds).
- Replacing the maturity-stage profile concept itself (e.g. switching to sector-relative weighting).
- Changing the formula (`composite = weighted_sum + modifiers + ...`) — adding a new modifier term.

When you bump major:
- The old model is preserved by git history. You do not delete it.
- Downstream consumers MUST be aware of the version boundary and treat pre-v2 snapshots as a different scoring schema.
- Document in `README_LTHCS.md` what changed and why.

### How old snapshots stay valid

Each snapshot's top-level `model_version` field is the audit anchor. The validator (`lthcs/validate.py`) doesn't enforce a single version — it accepts any string. So:

1. Today's snapshot: `model_version = "v1.0.0"`, written under current config.
2. You tune (e.g. soften a sector sensitivity), bump to `v1.1.0` in the daily pipeline, commit.
3. Tomorrow's snapshot: `model_version = "v1.1.0"`, written under new config.
4. Both files coexist in `data/lthcs/snapshots/`. The dashboard reads the latest. Backfilled comparisons can read older snapshots and treat them as the version they were tagged with — no re-computation.

**Where the `model_version` string is set.** Search `model_version` in `lthcs_daily.py` and the persist module — that's the one constant to bump when you ship a config change.

---

## Sensitivity analysis — what each lever does

The examples below assume the 2026-05-17 snapshot as the base. Run the preview script to see today's numbers.

### Example 1: soften Tech `fed_funds_pct` from -0.22 to -0.10

This is the canonical "AI mega-caps aren't rate-sensitive anymore" move. Run:

```sh
python3 scripts/lthcs_tune_preview.py \
  --sensitivity "Information Technology:fed_funds_pct=-0.10" \
  --top 10
```

Predicted output:

```
Tuning preview — base snapshot: 2026-05-17 (167 tickers)
Applied changes:
  - Information Technology.fed_funds_pct: -0.22 → -0.10 (override)

Top movers (|Δ| ≥ 0.5):
  ticker  before  after   Δ      band-shift
  MU      83.3    84.0    +0.7   high → high (no change)
  AVGO    78.6    79.3    +0.7   constructive → constructive
  KLAC    77.5    78.2    +0.7   constructive → constructive
  ...

Distribution shift:
  elite             0 →  0   (+0)
  high_confidence   3 →  3   (+0)
  constructive     12 → 14   (+2)
  monitor          21 → 19   (-2)
  weakening        ...
  review           ...

(no files modified)
```

Interpretation: the move is mild (~0.7pt per Tech name) because:
1. `fed_funds_pct` is one of 6 signals (its contribution is ~17% of total DES tilt).
2. DES is 0.20 weight in `standard_compounder` (so the change is `0.20 × magnitude_scale=30 × sensitivity_delta=0.12 × tilt`).
3. The current tilt is roughly +0.20 (Fed Funds 3.5% mapped to bounds [0, 6]).
4. So composite delta ≈ `0.20 × 30 × 0.12 × 0.20 ≈ 0.14` PER signal. Multiple AI-tier names already have `ticker_overrides` at -0.10 so their delta is 0 — the move primarily helps non-overridden Tech names.

To do a *meaningful* Tech recalibration, soften all four rate-sensitive signals (`fed_funds_pct`, `ten_y_yield_pct`, `ten_y_30d_change_bp`, and possibly `cpi_yoy_pct`) together. The cumulative effect is ~2-3 points per name.

### Example 2: bump magnitude_scale from 30 → 20

```sh
python3 scripts/lthcs_tune_preview.py --magnitude-scale 20
```

This is a UNIVERSE-WIDE move. DES contributions compress toward 50 (every signal contribution is multiplied by 20 instead of 30). The effect:
- Names whose DES was pulling them UP (Energy with high oil) lose some lift.
- Names whose DES was dragging them DOWN (rate-sensitive Tech) recover some.
- Universe variance compresses.

Use this when DES is mechanically driving more variance than the other pillars combined.

### Example 3: redistribute `standard_compounder` weights (move 5pp from DES → Thesis)

```sh
python3 scripts/lthcs_tune_preview.py \
  --weights-profile "standard_compounder=0.25,0.20,0.15,0.25,0.15"
```

Affects only tickers with `maturity_stage = standard_compounder` (most of the AAPL/MSFT/JPM type names). The shift transfers 5% of the composite from DES (~50 baseline) to Thesis (50.0 stub on un-rotated names, real Alpha Vantage score on rotated ones). For un-rotated tickers this is a no-op — both pillars are 50. For rotated tickers, the composite moves in the direction of the Thesis-vs-DES delta × 0.05.

### Example 4: shift bands down 5 points

```sh
python3 scripts/lthcs_tune_preview.py \
  --band-thresholds "elite=85,high_confidence=75,constructive=65,monitor=55,weakening=45,review=0"
```

Pure cosmetic. No composite scores change. Distribution histogram shifts upward — names previously at 78 (top of `constructive` under old bands) move into `high_confidence` under new bands. Use only when you've confirmed a systemic 5-point offset, not when one tier looks "empty for a day."

### Example 5: per-ticker override (NVDA-style)

```sh
python3 scripts/lthcs_tune_preview.py \
  --ticker-override "PLTR:fed_funds_pct=-0.10" \
  --ticker-override "PLTR:ten_y_yield_pct=-0.10"
```

Adds AI-tier softening to a specific name not yet in `ticker_overrides`. Single-ticker move, bounded delta (typically < 1.5 points unless the name has high DES weight).

---

## Anti-patterns — don't do these

1. **Don't tune bands to make distributions look pretty.** If a band is empty, that's information. The first instinct when the elite tier is empty should be to investigate (run the diagnostic), not to lower the threshold.
2. **Don't move multiple levers in one ship.** Tune one surface at a time so you can attribute the effect. Combined moves are how a model becomes un-auditable.
3. **Don't redistribute pillar weights beyond ±10pp per profile in one move.** Profiles are meant to encode genuine maturity-stage differences, not absorb day-to-day calibration drift.
4. **Don't add a ticker_override when the underlying issue is sector-level.** If 8 Tech names need the same override, the SECTOR sensitivity is wrong — fix it there. Overrides are for genuine outliers within an otherwise-correct sector.
5. **Don't tune `magnitude_scale` to mask a noisy macro input.** If `ten_y_30d_change_bp` is flopping around because the input is stale, fix the data fetch in `lthcs_daily.py`, don't muffle the signal.
6. **Don't change `pillar_order` without a major version bump.** Order is encoded in every snapshot's `weights_used` / `effective_weights` arrays. Reordering invalidates the audit trail.
7. **Don't tune to fix a single ticker's score.** One-ticker problems are usually data (`scripts/lthcs_diagnose.py`), maturity-stage mis-tagging (`universe.json`), or genuine signal you don't like. The tuning kit is for systemic recalibration.
8. **Don't ship a config change without running the preview first.** It takes 5 seconds and tells you whether the move did what you expected.

---

## Reference: tuning cheat sheet

One-line per dial. File path + effect.

| Key | File | Effect (1 line) |
|---|---|---|
| `profiles.<stage>` | `data/lthcs/weights.json` | 5-element pillar weight vector for one maturity stage (must sum to 1.0) |
| `score_bands.<band>.{min,max}` | `data/lthcs/weights.json` | Integer-bounded score range that maps a composite to a band label |
| `pillar_order` | `data/lthcs/weights.json` | Ordering of pillars in the weight vector (changing this is a major version bump) |
| `signal_normalization.<signal>.{low,high,neutral}` | `data/lthcs/sector_des_weights.json` | Linear map from raw macro value to `[-1, +1]` tilt |
| `sectors.<Sector>.<signal>` | `data/lthcs/sector_des_weights.json` | Sector sensitivity to one macro signal in `[-1, +1]` |
| `ticker_overrides.<TICKER>.<signal>` | `data/lthcs/sector_des_weights.json` | Per-ticker replacement of sector sensitivity for one signal |
| `magnitude_scale` (caller kwarg) | `lthcs/pillars/des.py` (`DEFAULT_MAGNITUDE_SCALE`) | Global multiplier on DES contributions (default 30.0) |
| `_MACRO_THRESHOLD_BP` | `lthcs/score.py` | 10Y 30d change in basis points needed to trigger macro_adj (default 25.0) |
| `_MACRO_MAGNITUDE` | `lthcs/score.py` | Magnitude of macro_adj when triggered (default 2.0) |
| `_VOLATILITY_PERCENTILE` | `lthcs/score.py` | Universe percentile threshold for volatility_modifier (default 90.0) |
| `_VOLATILITY_MAGNITUDE` | `lthcs/score.py` | Magnitude of volatility_modifier when triggered (default -3.0) |
| `<ticker>.maturity_stage` | `data/lthcs/universe.json` | Which `profiles.<stage>` vector applies to this ticker |
| `model_version` | `lthcs_daily.py` (constant) + snapshot field | Version tag written into every snapshot; bump per the playbook above |

---

## Workflow — using this kit end to end

1. **Diagnose first.** Don't tune until `scripts/lthcs_diagnose.py` has confirmed the issue is calibration (not real signal, not missing data). The diagnostic separates the three cases.
2. **Identify the lever.** Match the symptom against the decision table.
3. **Preview.** Run `scripts/lthcs_tune_preview.py` with the proposed change. Inspect the top movers, the distribution shift, and the band-shift column. Confirm the move does what you expected.
4. **Edit the config.** Make the JSON change. Run `python -c "import json; json.load(open('data/lthcs/<file>'))"` to confirm it parses.
5. **Bump `model_version`.** Patch / minor / major per the playbook.
6. **Re-run the daily.** `python lthcs_daily.py` writes a new snapshot under the new version.
7. **Validate.** Spot-check 5-10 tickers — did they move the way the preview predicted? If not, you've found a bug in your mental model OR the preview script — investigate before shipping more changes.
8. **Commit.** One ship per tuning move, with a commit message describing WHICH symptom you were addressing, WHICH lever you moved, and the version bump rationale.
9. **Update the snapshot index.** `data/lthcs/snapshots/index.json` if your pipeline maintains it.

---

## What this kit deliberately does NOT cover

- **Adding a new pillar** — that's a major version event and a whole spec, not a tuning move. See `PHASE_1_BUILD_SPEC.md` for the existing 5-pillar architecture.
- **Replacing the Thesis source** — same reason; major version.
- **Fixing peer-group composition** — see queue item #3 (`docs/peer-group-audit.md`).
- **Wiring up un-wired DES signals** — see queue item #2 (`docs/des-audit-framework.md`).
- **Per-ticker score forcing** — there is no "score override" mechanism. If a ticker's score is genuinely wrong, the fix is to identify which input is wrong and fix that input. Tuning is never the answer to a single-ticker issue.
