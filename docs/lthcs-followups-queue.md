# LTHCS — Queued follow-ups

Captured 2026-05-17 after V1 ship + DES calibration fix + sector heatmap.

Bryan wants these tackled in order after current threads wrap. Each item
below is sized as a single swarm round unless noted.

---

## 1. Diagnostic checklist — "is this real signal, calibration, or missing data?"

**Goal.** A repeatable procedure for figuring out, when a ticker scores
unexpectedly low (or high), whether the cause is:

a. **Real signal** — the ticker genuinely deserves the score given the
   inputs we have,
b. **Calibration** — the inputs are right but the weights / normalization
   bounds are mis-tuned,
c. **Missing data** — one or more pillar components fell back to neutral
   50 because an upstream fetch failed or a sub-component is a V1 stub.

**Method (per Bryan's note).** Pick three names you have strong priors about:
- One you expect HIGH: e.g. AAPL or NVDA
- One you expect LOW:  e.g. INTC or LCID
- One you expect MID:  e.g. one of the older industrials

For each, pull:
- `data/lthcs/snapshots/<latest>.json` row
- `data/lthcs/variable_detail/<latest>.json` rows matching that ticker
- `data/lthcs/narratives/<latest>.json` row
- `data/lthcs/sentiment/<ticker>.json` (if present)
- `data/lthcs/history/by_ticker/<ticker>.json`

Then audit each pillar's contribution:
- Is `revenue_growth_yoy` a real number or `null`?
- Is `momentum_pct_90d` a real number or `null`?
- Is `mean_sentiment_score` real or 50.0 neutral?
- Did `volatility_modifier` fire (–3.0) and is that correct?
- Are the modifier columns zero because the conditions weren't met, or
  because the input data was missing?

**Deliverable.**
- `scripts/lthcs_diagnose.py <TICKER1> <TICKER2> <TICKER3>` — prints a
  side-by-side per-pillar table marking each input as REAL / NEUTRAL /
  MISSING / STUB.
- `docs/lthcs-diagnostic-runbook.md` — narrative version explaining how
  to read the output and decide which of (a) (b) (c) applies.

**Effort.** ~1 swarm, mostly script work.

---

## 2. DES audit framework

**Goal.** Every DES input from §6.1 of the white paper, with a checkbox
for "is this wired up?" + status notes. Hand to Claude Code and it tells
you exactly which inputs are stubs vs. live.

**Method.**
- Inventory §6.1 of the LTHCS white paper (the framework spec — separate
  from PHASE_1_BUILD_SPEC.md). For each macro signal listed there, note:
  - Is it present in `data/lthcs/sector_des_weights.json` `signal_normalization`?
  - Is it being populated by `lthcs_daily.py` `build_macro_inputs()`?
  - Are the sector sensitivities defined for it?
  - Is the live snapshot reflecting it?
- Cross-reference with what V1 actually wires up:
    cpi_yoy_pct, fed_funds_pct, ten_y_yield_pct,
    ten_y_30d_change_bp, unemployment_pct, wti_oil_usd

**Deliverable.**
- `docs/des-audit-framework.md` — table:
   ```
   | Signal (§6.1)         | In config | In daily pipeline | In snapshot | Notes |
   |-----------------------|-----------|-------------------|-------------|-------|
   | CPI YoY               | ✓         | ✓                 | ✓           |       |
   | Brent crude           | ✓         | ✗                 | —           | Only WTI wired |
   | M2 money supply       | ✗         | ✗                 | —           | Phase 2 |
   | ...                   |           |                   |             |       |
   ```
- Plus a recommendation: which of the un-wired signals to add first,
  what FRED/EIA series IDs they map to, and rough effort.

**Effort.** ~1 swarm (research + write).

**Prereq.** Need the white-paper §6.1 text accessible to the agent. If
it's not in the repo, Bryan needs to paste/upload it OR we work from
PHASE_1_BUILD_SPEC.md §5 which is the V1 distillation.

---

## 3. Peer group sanity check

**Goal.** Confirm that percentile normalization peer groups are
sector-coherent. AAPL being percentile-ranked against LCID is wrong —
they're not comparable.

**Current state.** `lthcs/pillars/adoption.py` and
`lthcs/pillars/financial.py` use `peer_relative_percentile(growth,
peer_growths)` where `peer_growths` is the FULL universe of 168 tickers.

This means:
- AAPL's revenue growth (+6%) gets ranked against MU (+50%) and LCID (+68%)
  → AAPL lands at the LOW end of the peer percentile even though +6% is
  normal for a $3T compounder
- DE (industrials, -11.7%) gets ranked against the same group
- LCID (pre-profit, +67%) is in the comparison set for everyone

**Method.**
- Audit each percentile call site in `lthcs/pillars/*.py`
- Decide which signals SHOULD be sector-relative vs. universe-relative:
  - revenue_growth_yoy → sector-relative (yes, definitely)
  - momentum_90d → universe-relative (Institutional Confidence wants
    cross-universe comparison)
  - margin_trend → sector-relative
  - OCF margin → sector-relative
- Add a sector-bucketed peer_relative_percentile path
- Re-run distribution check; verify mega-caps no longer rank in the
  bottom of revenue-growth percentile

**Deliverable.**
- `docs/peer-group-audit.md` — current vs. proposed mapping per signal
- Code change to introduce `peer_relative_percentile_by_sector(...)`
- Pillar updates to use sector-relative where appropriate
- Re-run + commit new snapshot

**Effort.** ~1–2 swarms (the analysis is fast, the code change is small,
but expect re-tuning surprises).

---

## 4. Small "tuning kit" — minimal-touch recalibration

**Goal.** If calibration (not missing inputs) is the issue, what's the
minimal config-only set of changes needed to recalibrate, with
model_version increment + original-snapshot preservation per §1.4 of the
white paper.

**Method.**
- Levers available without code changes:
  - `data/lthcs/weights.json` — per-maturity-stage pillar weights
  - `data/lthcs/sector_des_weights.json` — sector + ticker overrides,
    signal normalization bounds, magnitude_scale
  - Volatility-modifier threshold (currently 90th percentile,
    magnitude –3.0) — would need a small config file extracted from
    `lthcs/score.py` constants
- For each lever:
  - What signal does it tune?
  - What does a small move (10–20%) do to the universe?
  - Sensitivity analysis: which tickers shift bands?

**Deliverable.**
- `docs/lthcs-tuning-kit.md` — playbook for "if scores look X, adjust Y by Z"
- Optional new file: `data/lthcs/modifiers.json` so volatility and
  macro-adjustment thresholds become config rather than code constants
- Script: `scripts/lthcs_tune_preview.py --weight pillar=value --reweight`
  shows what would happen WITHOUT writing files
- Document the model_version bump procedure: when to go v1.0.0 → v1.0.1
  vs. v1.1.0 vs. v2.0.0; how old snapshots stay valid (they already do —
  git history is the audit log)

**Effort.** ~1 swarm.

---

## 5. Decision tree for what comes next

(Bryan's message cut off here — placeholder for the full content. My read
of the implied tree based on items 1–4 above:)

```
Run diagnostic-checklist (#1) on 3 teaching cases.
│
├─ If all 3 inputs are REAL and scores still feel wrong →
│   probably a peer-group issue. Run #3 (sector-coherent percentiles).
│   Then re-evaluate.
│
├─ If multiple inputs are NEUTRAL/MISSING for the high-conviction
│   names (e.g. NVDA has no real Thesis sentiment yet) →
│   it's a DATA gap. Wait for Thesis rotation to ramp (~10–14 days)
│   OR force the rotation to score those names first.
│
├─ If inputs are REAL but weights feel off →
│   it's CALIBRATION. Use the tuning kit (#4) to nudge.
│
└─ If a whole pillar's inputs are STUBS (e.g. 13F at 30% weight) →
    that's a PHASE 2 build, not a recalibration. Plan accordingly.
```

This decision tree should live inside `docs/lthcs-diagnostic-runbook.md`
once item #1 lands; queue it there.

---

## Currently in flight

- **Thesis rotation ramp.** Naturally progresses with each daily run.
  At ~5–25 new tickers/day on AV free tier, full universe coverage in
  ~10–14 days. No action needed — just keep running `python lthcs_daily.py`
  daily and pushing the snapshots.
- Otherwise nothing autonomous is running.

## Out of scope for this queue (was offered, not picked)

- Sortable Bloomberg-style table view (UX research recommendation #2)
- "Movers" leaderboard (UX research recommendation #3)
- Institutional pillar audit (MSFT 27.9 / CRM 6.7 anomaly flagged in
  docs/des-analysis.md)
- Phase 2 13F holdings real implementation (replace stub)
- Phase 2 Google Trends acceleration wiring (40% of Adoption pillar)

These remain queued informally; the items 1–5 above take priority.
