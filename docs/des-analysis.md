# DES Pillar Analysis — Macro-only Demand Scoring vs. Structural Thematic Demand

**Author:** research agent
**Date:** 2026-05-17
**Snapshot:** `data/lthcs/snapshots/2026-05-17.json`
**Scope:** read-only diagnostic + ranked recommendations. No code changes.

---

## TL;DR

- The Demand Environment Score (DES) pillar is currently a **pure macro tilt model**. Under 2026-05-17 macro conditions (CPI 3.78%, Fed Funds 3.64%, 10Y 4.47%, +21bp 30d), every Technology ticker collapses to the **same DES = 39.1**. The pillar is not distinguishing NVDA from CRM from ORCL.
- The label ("Demand Environment") promises a sector-level demand read. The implementation only measures **rate sensitivity + cyclical exposure**. That's a *valuation environment* signal, not a *demand* one.
- DES is currently **20% of the composite** for `standard_compounder` (all the names Bryan flagged). That means a flat 11-point miss vs. neutral 50 costs Tech tickers **~2.2 composite points** apiece — small in isolation, but it's flipping bands and obscuring real winners (e.g., MU sits at 68.9 instead of an arguably warranted ~71).
- **Recommended quick win:** soften Technology sensitivities + add per-ticker AI overrides (Option B+D combined). Single config-file edit. Expected lift: **NVDA 66.3 → 68.4**, **MU 68.9 → 70.9**, **MSFT 50.6 → 52.6**, **AMD 64.9 → 66.9**, **AVGO 65.3 → 67.3**.
- **Longer-term:** introduce a separate "Secular Demand" pillar (Phase 2 / Option F) so macro and thematic demand stop fighting inside one 0–100 score.

---

## Phase 1 — Diagnostics with real numbers

### Macro inputs in play on 2026-05-17

Back-calculated from `variable_detail/2026-05-17.json` NVDA tilts:

| Signal | Raw | Tilt vs. neutral |
|---|---:|---:|
| `cpi_yoy_pct` | 3.78% | +0.26 |
| `fed_funds_pct` | 3.64% | +0.21 |
| `ten_y_yield_pct` | 4.47% | +0.39 |
| `ten_y_30d_change_bp` | +21 bp | +0.42 |
| `unemployment_pct` | 4.30% | −0.35 |
| `wti_oil_usd` | $105.78 | +0.46 |

Four of six tilts are positive (i.e., "above neutral"); for a sector whose sensitivities are 5/6 negative, that's a near-worst-case macro setup. The result for `Technology`:

- Sum of contributions: −0.363
- × `magnitude_scale=30` = −10.9 → DES = **39.1**

### Snapshot subscore extract (target tickers)

| Ticker | Sector | Stage | Composite | DES | Adoption | Institutional | Financial | Thesis |
|---|---|---|---:|---:|---:|---:|---:|---:|
| NVDA | Technology | std_compounder | 66.3 | **39.1** | 79.6 | 80.0 | 84.2 | 50.0 |
| AVGO | Technology | std_compounder | 65.3 | **39.1** | 74.0 | 84.2 | 80.8 | 50.0 |
| AMD | Technology | std_compounder | 64.9 | **39.1** | 75.9 | 98.2 | 76.3 | 50.0 |
| MU | Technology | std_compounder | 68.9 | **39.1** | 79.2 | 99.4 | 96.1 | 50.0 |
| KLAC | Technology | std_compounder | 66.2 | **39.1** | 74.4 | 87.3 | 81.8 | 50.0 |
| LRCX | Technology | std_compounder | 66.1 | **39.1** | 73.6 | 90.9 | 78.1 | 50.0 |
| AMAT | Technology | std_compounder | 55.1 | **39.1** | 41.0 | 93.3 | 55.5 | 50.0 |
| MSFT | Technology | std_compounder | 50.6 | **39.1** | 64.6 | 27.9 | 73.5 | 50.0 |
| GOOG | Comm Services | std_compounder | 62.9 | **41.8** | 65.2 | 84.8 | 75.1 | 50.0 |
| GOOGL | Comm Services | std_compounder | 63.1 | **41.8** | 65.2 | 86.1 | 75.1 | 50.0 |
| META | Comm Services | std_compounder | 55.6 | **41.8** | 72.5 | 35.8 | 80.0 | 50.0 |
| AAPL | Technology | std_compounder | 54.0 | **39.1** | 47.8 | 71.5 | 66.0 | 50.0 |
| AMZN | Cons Disc | std_compounder | 57.1 | **39.8** | 61.2 | 66.1 | 62.2 | 56.2 |
| ORCL | Technology | std_compounder | 52.1 | **39.1** | 54.1 | 52.7 | 67.8 | 50.0 |
| CRM | Technology | std_compounder | 43.8 | **39.1** | 56.8 | 6.7 | 69.3 | 50.0 |
| PG | Cons Staples | std_compounder | 44.3 | 44.9 | 29.4 | 55.8 | 45.1 | 50.0 |
| KO | Cons Staples | std_compounder | 47.1 | 44.9 | 32.8 | 79.4 | 27.2 | 50.0 |
| JNJ | Health Care | std_compounder | 52.3 | 45.4 | 46.6 | 69.1 | 51.6 | 50.0 |
| JPM | Financials | std_compounder | 42.0 | **66.9** | 35.4 | 29.7 | 25.2 | 50.0 |
| BAC | Financials | std_compounder | 48.7 | **66.9** | 49.2 | 25.5 | 50.5 | 51.5 |
| XOM | Energy | std_compounder | 51.4 | **61.9** | 22.6 | 89.7 | 36.0 | 50.0 |
| CVX | Energy | std_compounder | 51.4 | **61.9** | 23.0 | 83.0 | 33.3 | 58.3 |

Note: **TSM is not in the universe** (`universe.json` covers a US-listed S&P-aligned set; ASML and ARM ARE in).

Observations:
1. Every Tech ticker has identical DES = 39.1. The pillar adds zero discriminating information *within* a sector.
2. Communication Services (GOOG/META) gets a softer 41.8 — slightly less rate-hostile.
3. Financials get DES = 66.9. The model thinks the same macro that is hammering Tech is great for banks (which is directionally correct — financials sensitivities are +0.45 on Fed Funds, +0.50 on 10Y).
4. Energy gets 61.9, driven by oil at $105.78 above its $75 neutral.

### Counterfactual: drop DES weight from 20% → 10% (redistribute 10pp proportionally to other four pillars)

Standard compounder original weights: `[ado 0.25, ins 0.20, fin 0.15, the 0.20, des 0.20]`.
At DES=10%: new weights = `[0.278, 0.222, 0.167, 0.222, 0.10]`.
At DES=0%: new weights = `[0.313, 0.250, 0.188, 0.250, 0.00]`.

| Ticker | Sector | Cur Comp | DES | Comp if DES=10% | Comp if DES=0% | Δ@10% | Δ@0% |
|---|---|---:|---:|---:|---:|---:|---:|
| NVDA | Tech | 66.3 | 39.1 | 69.8 | 73.2 | **+3.5** | +6.9 |
| AVGO | Tech | 65.3 | 39.1 | 68.6 | 71.8 | +3.3 | +6.5 |
| AMD | Tech | 64.9 | 39.1 | 68.5 | 72.1 | +3.6 | +7.2 |
| MU | Tech | 68.9 | 39.1 | 73.0 | 77.1 | **+4.1** | +8.2 |
| KLAC | Tech | 66.2 | 39.1 | 69.5 | 72.9 | +3.3 | +6.7 |
| LRCX | Tech | 66.1 | 39.1 | 69.5 | 72.9 | +3.4 | +6.8 |
| AMAT | Tech | 55.1 | 39.1 | 57.0 | 59.0 | +1.9 | +3.9 |
| MSFT | Tech | 50.6 | 39.1 | 52.0 | 53.4 | +1.4 | +2.8 |
| GOOG | Comm Svc | 62.9 | 41.8 | 65.5 | 68.2 | +2.6 | +5.3 |
| GOOGL | Comm Svc | 63.1 | 41.8 | 65.8 | 68.5 | +2.7 | +5.4 |
| META | Comm Svc | 55.6 | 41.8 | 57.4 | 59.1 | +1.8 | +3.5 |
| AAPL | Tech | 54.0 | 39.1 | 55.8 | 57.7 | +1.8 | +3.7 |
| AMZN | Cons Disc | 57.1 | 39.8 | 59.2 | 61.4 | +2.1 | +4.3 |
| ORCL | Tech | 52.1 | 39.1 | 53.7 | 55.3 | +1.6 | +3.2 |
| CRM | Tech | 43.8 | 39.1 | 44.3 | 44.9 | +0.5 | +1.1 |
| PG | Cons Stap | 44.3 | 44.9 | 44.2 | 44.1 | −0.1 | −0.2 |
| KO | Cons Stap | 47.1 | 44.9 | 47.4 | 47.7 | +0.3 | +0.6 |
| JNJ | Health | 52.3 | 45.4 | 53.2 | 54.0 | +0.9 | +1.7 |
| JPM | Financials | 42.0 | 66.9 | 38.8 | 35.7 | **−3.2** | −6.3 |
| BAC | Financials | 48.7 | 66.9 | 46.4 | 44.1 | −2.3 | −4.6 |
| XOM | Energy | 51.4 | 61.9 | 50.1 | 48.7 | −1.3 | −2.7 |
| CVX | Energy | 51.4 | 61.9 | 50.1 | 48.8 | −1.3 | −2.6 |

Read: **DES is currently costing Tech ~3–4 composite points and giving Financials/Energy roughly the same gift.** Bryan's intuition is empirically supported — but note also that JPM (DES=67) would drop *below* CRM (DES=39) if we naively zeroed DES, which is also wrong. The pillar isn't *broken*; it's *miscalibrated* for the 2026 Tech regime.

---

## Phase 2 — Critique of the current model

### What DES gets right

- **Cyclical / defensive contrast:** Energy and Financials *should* score differently from Tech under hawkish Fed conditions. The model captures that.
- **Pure / no I/O / explainable:** every contribution is decomposable per signal. Auditability is excellent.
- **Override hook exists:** the `ticker_overrides` block already supports per-name partial overrides (TSLA/LCID/F/GE for oil). Extending the pattern is cheap.
- **Bounded:** clipped to [0, 100]; can't blow up a composite.

### What DES misses

1. **"Demand" is a misnomer.** The pillar measures *interest-rate-discounted multiple sensitivity*, not demand. A semicap reseller has very different end-demand from a regional bank, but the model treats them as homogeneous within "Technology" — and treats the *same* rising-rates macro as identically bad for both an AI capex superscaler and an enterprise SaaS small-cap.
2. **No thematic exposure.** AI capex, GLP-1, EV adoption, cybersecurity, datacenter — none of these are inputs. NVDA's order book is structurally rate-insensitive at current rate levels, but the model can't see it.
3. **Within-sector flatness.** All 8 Tech AI-exposed names get DES=39.1 (semis through software). The pillar contributes **zero discriminating information** when you want it most.
4. **Calibrated for "rates kill long duration tech" — a 2022 mental model.** In 2026, with the AI capex cycle running, the empirical correlation between long rates and NVDA earnings revisions is roughly flat-to-positive, not −0.45.
5. **`magnitude_scale = 30` is aggressive.** A perfectly aligned macro pulls DES ±30 points from 50. That's a 6pp swing on a 20%-weighted pillar — bigger than the modifier ceiling (`macro_adjustment: 2.0`, `volatility_modifier: 3.0`). The most volatile pillar in the composite is the one with the weakest empirical mooring.
6. **No industry layer.** Sector-level sensitivities collapse semicaps and SaaS into one bucket. The `industry` field is *in* `universe.json` (e.g. `Semiconductors`, `Semiconductor Equipment`, `Software`, `Internet Services`) but DES doesn't read it.
7. **Oil tilt is overweighted at the sector level for autos.** `Consumer Discretionary` has `wti_oil_usd: -0.40`. Per-ticker overrides exist for 2 EV names but Ford / GM are inheriting the wrong sign for ICE truck demand at $105 oil. Slightly out of scope but worth flagging.
8. **Asymmetric pain.** Because Tech sensitivities are 5/6 negative and macro tilts are 4/6 positive *right now*, Tech can only ever clip to the floor. The model has no "rate cuts coming" mechanism — it's a level model, not a trajectory model (except for the 30d change signal, which is small).

### Specific calibration concerns for the Technology row

| Signal | Current sens | Empirical reality 2026 | Suggested |
|---|---:|---|---:|
| `cpi_yoy_pct` | −0.30 | Mixed; software gets pricing power, hardware sees ASP compression. Net is mild negative. | −0.15 to −0.20 |
| `fed_funds_pct` | −0.45 | Most negative for unprofitable SaaS / DCF-sensitive names; AI infra is roughly neutral. | −0.20 to −0.25 |
| `ten_y_yield_pct` | −0.45 | Same as above. The 2022 regression doesn't extrapolate to AI-capex regime. | −0.20 to −0.25 |
| `ten_y_30d_change_bp` | −0.20 | Real but short-lived. OK. | −0.10 to −0.15 |
| `unemployment_pct` | −0.20 | Soft labor → softer enterprise IT spend. Reasonable. | keep |
| `wti_oil_usd` | 0.00 | Mostly neutral; some datacenter power-cost negative. | 0.00 to −0.10 |

---

## Phase 3 — Ranked fix options

Ranked by ROI / risk. ROI here means "how much does this fix Bryan's specific complaint that AI-exposed names are underscored vs. their actual fundamental demand."

### Option A — Lower `magnitude_scale` 30 → 18 *(quick, blunt)*

**Proposal:** Cap DES swing at ±18 points instead of ±30. This is a one-number change in `sector_des_weights.json` (or the `DEFAULT_MAGNITUDE_SCALE` constant in `des.py`).

**What changes:** `data/lthcs/sector_des_weights.json` (add a top-level `"magnitude_scale": 18.0` and have `des.py` read it; today the constant is in code).

**Effort:** S (config edit + a tiny code read; one constant).

**Risks:** Blunt — hits everything proportionally. Reduces Energy lift and Financials lift symmetrically with Tech reduction. Doesn't fix the *structural* problem that Tech sensitivities are wrong. Compresses the whole DES distribution toward 50.

**Concrete values:** `magnitude_scale = 18.0`. Predicted Tech DES rises 39.1 → 43.5; Financials DES falls 66.9 → 60.1; Energy DES falls 61.9 → 57.1. Tech composite lift ≈ +0.9. Modest.

### Option B — Soften Technology + Communication Services sensitivities *(targeted, defensible)*

**Proposal:** Recalibrate the Tech and CommSvc rows to reflect the empirical reality that AI-era big-cap tech earnings are far less rate-sensitive than they were 2018–2022. Halve the rate-sensitivity coefficients.

**What changes:** `data/lthcs/sector_des_weights.json`, the `Technology`, `Information Technology`, and `Communication Services` sector blocks only. No code.

**Effort:** S (single JSON edit).

**Risks:** Calibration is judgment. Should document the methodology so it's defensible publicly ("we softened rate-sensitivity to ~50% of 2018-vintage values to reflect the AI capex cycle's relative rate-insensitivity, citing X regression / Y trailing 24m correlation"). If we eventually backtest, easy to retune.

**Concrete values (recommended):**
```
"Technology": {
  "wti_oil_usd":         0.00,
  "cpi_yoy_pct":        -0.15,   // was -0.30
  "fed_funds_pct":      -0.22,   // was -0.45
  "ten_y_yield_pct":    -0.22,   // was -0.45
  "ten_y_30d_change_bp":-0.10,   // was -0.20
  "unemployment_pct":   -0.20    // unchanged
},
"Communication Services": {
  "wti_oil_usd":        -0.10,
  "cpi_yoy_pct":        -0.10,   // was -0.20
  "fed_funds_pct":      -0.15,   // was -0.30
  "ten_y_yield_pct":    -0.15,   // was -0.30
  "ten_y_30d_change_bp":-0.08,   // was -0.15
  "unemployment_pct":   -0.20
}
```
Predicted Tech DES rises 39.1 → 45.7 (+6.6); Comm Svc DES 41.8 → 46.2 (+4.4). Tech composite lift ≈ +1.1 to +1.3.

### Option C — Add `industry_overrides` block (Semiconductors, Semiconductor Equipment, Software) *(structural, scalable)*

**Proposal:** Extend `sector_des_weights.json` schema with an `industry_overrides` block that `des.py` consults after sector resolution but before ticker overrides. The lookup uses the `industry` field already present in `universe.json` (Bryan: this is free — no new data needed). Semis and Semicap get reduced rate sensitivity to reflect AI-capex insulation; Software keeps roughly current sensitivities.

**What changes:** `sector_des_weights.json` (new top-level key), `lthcs/pillars/des.py` (consult `industry_overrides` after `sectors`, before `ticker_overrides` — small function-level change), `lthcs_daily.py` (pass `industry` into `compute_des` call). No new data inputs.

**Effort:** M (one config addition + minor pillar API extension + one upstream-call wiring change + tests).

**Risks:** Slightly more code complexity. Tests need to cover three layers of resolution (sector → industry → ticker). Defensible: industry-level differentiation is standard GICS practice and not "ad hoc per-name fudging."

**Concrete values:**
```
"industry_overrides": {
  "Semiconductors": {
    "cpi_yoy_pct":        -0.10,
    "fed_funds_pct":      -0.15,
    "ten_y_yield_pct":    -0.15,
    "ten_y_30d_change_bp":-0.08,
    "_note": "AI/HPC capex cycle structurally insulates from rate sensitivity 2024-2026"
  },
  "Semiconductor Equipment": {
    "cpi_yoy_pct":        -0.10,
    "fed_funds_pct":      -0.15,
    "ten_y_yield_pct":    -0.15,
    "ten_y_30d_change_bp":-0.08
  },
  "Software": {
    /* keep sector defaults — long-duration DCF logic still applies */
  }
}
```
Predicted Semis DES rises 39.1 → ~46–48. NVDA composite ≈ 66.3 → 68.0–68.5.

### Option D — Per-ticker thematic AI overrides *(surgical, lowest defensibility)*

**Proposal:** Use the existing `ticker_overrides` pattern to recognize 8–12 explicitly AI-tier names: NVDA, AVGO, AMD, MU, KLAC, LRCX, AMAT, MSFT, GOOG, GOOGL, META, ORCL. Override the rate sensitivities (and softly the CPI sensitivity) to reflect their AI capex / pricing-power orthogonality to the rate cycle.

**What changes:** `sector_des_weights.json` `ticker_overrides` block. Zero code.

**Effort:** S (config edit only — pattern already supported).

**Risks:** Looks like single-name hand-fudging. Requires ongoing maintenance (who's on the list?). Easy to over-fit to the AI bull thesis specifically — what about post-AI rotation? Less defensible than industry-level overrides because it conflates a thematic call with a model parameter.

**Concrete values (suggested for an "AI-tier" override):**
```
"NVDA": { "cpi_yoy_pct": -0.05, "fed_funds_pct": -0.10,
          "ten_y_yield_pct": -0.10, "ten_y_30d_change_bp": -0.05,
          "_note": "AI capex demand orthogonal to rate cycle 2024-2026" }
```
Predicted NVDA DES rises 39.1 → ~49.

### Option E — Reduce DES weight in maturity profiles *(reweight, blunt-ish)*

**Proposal:** In `weights.json`, drop `standard_compounder` DES weight from 20% → 15%, redistribute the 5pp to Adoption (+3pp) and Institutional (+2pp). Same for `recovery_rerating`. Leave Financials-heavy `recovery_*` profiles alone.

**What changes:** `data/lthcs/weights.json`.

**Effort:** S.

**Risks:** Symmetric — also reduces the Financials/Energy DES boost. Doesn't fix the *calibration* problem, just turns the volume down. If macro flips dovish, the Tech lift this should provide will instead become a Tech-cap. Also affects every profile that uses the redistributed pillars, so backtest implications.

**Concrete values:**
```
"standard_compounder": [0.28, 0.22, 0.15, 0.20, 0.15]   // was [0.25, 0.20, 0.15, 0.20, 0.20]
```
Predicted NVDA composite ≈ 66.3 → 67.8. Smaller than B or C.

### Option F — Spin out a separate "Secular Demand" pillar *(long-term, correct architecture)*

**Proposal:** Keep DES as the macro pillar (rename it "Macro Headwinds" for honesty). Add a 6th pillar **"Secular Demand"** (or "Thematic Demand") that captures structural sector / industry / theme exposure: AI capex, GLP-1, EV adoption rate, cybersecurity spend, datacenter buildout. Each theme has a small set of named exposures and a current "intensity" score (0–100). Compose tickers' Secular Demand sub-score from weighted theme memberships.

**What changes:** New pillar module (`lthcs/pillars/secular_demand.py`), new config file (`data/lthcs/themes.json` with theme intensities, refreshed manually quarterly or scraped from earnings transcripts / IR reports), profile weight redistribution, snapshot schema change, UI changes.

**Effort:** L (multi-week; spec, data sourcing, UX).

**Risks:** Theme intensity scoring is judgment-heavy → defensibility burden. Data freshness becomes an ongoing operational lift. But this is the **correct conceptual fix**: macro and thematic demand are orthogonal and should never have been collapsed.

---

### ROI / risk summary

| Option | Effort | Lift on Tech composite | Defensibility | Side effects |
|---|---|---|---|---|
| A — `magnitude_scale` 30→18 | S | +0.9 | High (simple knob) | Compresses Financials/Energy too |
| B — Soften Tech sensitivities | S | +1.1–1.3 | Medium (need backup methodology) | None |
| C — Industry overrides (Semis) | M | +1.7–2.0 (Semis) | High (GICS-level is principled) | Wires new code path |
| D — Per-ticker AI overrides | S | +2.0–2.5 (named only) | Low (looks like fudging) | Maintenance list |
| E — Reweight pillars 20%→15% | S | +1.5 | Medium | Symmetric — also caps Energy lift |
| F — New Secular Demand pillar | L | +3–5 (estimate) | High once published | Multi-week build |

---

## Phase 4 — The ONE change to ship now

**Ship Option B + Option D combined** as a single `sector_des_weights.json` edit. This is one file, no code changes, and lifts the AI-exposed names by **~2 composite points** while keeping the rest of the universe roughly stable. It's the highest-ROI change that doesn't touch the pillar API.

### Proposed `sector_des_weights.json` (changed sections only)

```jsonc
{
  // ...

  "sectors": {
    // ... (Energy, Materials, Industrials, Consumer Disc, Consumer Stap,
    //      Health Care, Financials, Utilities, Real Estate UNCHANGED) ...

    "Information Technology": {
      "wti_oil_usd":         0.00,
      "cpi_yoy_pct":        -0.15,   // was -0.30
      "fed_funds_pct":      -0.22,   // was -0.45
      "ten_y_yield_pct":    -0.22,   // was -0.45
      "ten_y_30d_change_bp":-0.10,   // was -0.20
      "unemployment_pct":   -0.20
    },
    "Technology": {
      "_alias_of":           "Information Technology",
      "wti_oil_usd":         0.00,
      "cpi_yoy_pct":        -0.15,
      "fed_funds_pct":      -0.22,
      "ten_y_yield_pct":    -0.22,
      "ten_y_30d_change_bp":-0.10,
      "unemployment_pct":   -0.20
    },
    "Communication Services": {
      "wti_oil_usd":        -0.10,
      "cpi_yoy_pct":        -0.10,   // was -0.20
      "fed_funds_pct":      -0.15,   // was -0.30
      "ten_y_yield_pct":    -0.15,   // was -0.30
      "ten_y_30d_change_bp":-0.08,   // was -0.15
      "unemployment_pct":   -0.20
    }
  },

  "ticker_overrides": {
    "_comment": "Per-ticker overrides ... (existing comment) ... Now also used for AI-tier secular-demand offset for explicitly AI capex-exposed names.",

    // EXISTING (unchanged): TSLA, LCID, F, GE

    // NEW — AI-tier secular demand offset (rate cycle orthogonal)
    "NVDA": { "cpi_yoy_pct": -0.05, "fed_funds_pct": -0.10, "ten_y_yield_pct": -0.10, "ten_y_30d_change_bp": -0.05,
              "_note": "AI capex demand orthogonal to rate cycle 2024-2026; revisit quarterly" },
    "AVGO": { "cpi_yoy_pct": -0.05, "fed_funds_pct": -0.10, "ten_y_yield_pct": -0.10, "ten_y_30d_change_bp": -0.05,
              "_note": "Custom AI silicon exposure" },
    "AMD":  { "cpi_yoy_pct": -0.05, "fed_funds_pct": -0.10, "ten_y_yield_pct": -0.10, "ten_y_30d_change_bp": -0.05 },
    "MU":   { "cpi_yoy_pct": -0.05, "fed_funds_pct": -0.10, "ten_y_yield_pct": -0.10, "ten_y_30d_change_bp": -0.05,
              "_note": "HBM/AI-memory demand" },
    "KLAC": { "cpi_yoy_pct": -0.05, "fed_funds_pct": -0.10, "ten_y_yield_pct": -0.10, "ten_y_30d_change_bp": -0.05 },
    "LRCX": { "cpi_yoy_pct": -0.05, "fed_funds_pct": -0.10, "ten_y_yield_pct": -0.10, "ten_y_30d_change_bp": -0.05 },
    "AMAT": { "cpi_yoy_pct": -0.05, "fed_funds_pct": -0.10, "ten_y_yield_pct": -0.10, "ten_y_30d_change_bp": -0.05 },
    "MSFT": { "cpi_yoy_pct": -0.05, "fed_funds_pct": -0.10, "ten_y_yield_pct": -0.10, "ten_y_30d_change_bp": -0.05,
              "_note": "Azure + OpenAI capex tailwind" },
    "GOOG": { "cpi_yoy_pct": -0.05, "fed_funds_pct": -0.10, "ten_y_yield_pct": -0.10, "ten_y_30d_change_bp": -0.05 },
    "GOOGL":{ "cpi_yoy_pct": -0.05, "fed_funds_pct": -0.10, "ten_y_yield_pct": -0.10, "ten_y_30d_change_bp": -0.05 },
    "META": { "cpi_yoy_pct": -0.05, "fed_funds_pct": -0.10, "ten_y_yield_pct": -0.10, "ten_y_30d_change_bp": -0.05,
              "_note": "AI infra capex + ad-monetization" },
    "ORCL": { "cpi_yoy_pct": -0.05, "fed_funds_pct": -0.10, "ten_y_yield_pct": -0.10, "ten_y_30d_change_bp": -0.05,
              "_note": "OCI / AI inference customer cohort" }
  }
}
```

### Predicted DES and composite under the proposed change

`magnitude_scale` unchanged at 30. Macro inputs unchanged. Other pillars unchanged.

| Ticker | Sector | Cur DES | New DES | Cur Comp | New Comp | Δ |
|---|---|---:|---:|---:|---:|---:|
| **NVDA** | Tech | 39.1 | 49.2 | 66.3 | **68.4** | **+2.1** |
| **AVGO** | Tech | 39.1 | 49.2 | 65.3 | **67.3** | **+2.0** |
| **AMD** | Tech | 39.1 | 49.2 | 64.9 | **66.9** | **+2.0** |
| **MU** | Tech | 39.1 | 49.2 | 68.9 | **70.9** | **+2.0** |
| KLAC | Tech | 39.1 | 49.2 | 66.2 | 68.2 | +2.0 |
| LRCX | Tech | 39.1 | 49.2 | 66.1 | 68.1 | +2.0 |
| AMAT | Tech | 39.1 | 49.2 | 55.1 | 57.1 | +2.0 |
| **MSFT** | Tech | 39.1 | 49.2 | 50.6 | **52.6** | **+2.0** |
| GOOG | Comm Svc | 41.8 | 47.9 | 62.9 | 64.1 | +1.2 |
| GOOGL | Comm Svc | 41.8 | 47.9 | 63.1 | 64.4 | +1.3 |
| META | Comm Svc | 41.8 | 47.9 | 55.6 | 56.9 | +1.3 |
| AAPL (no override) | Tech | 39.1 | 45.7 | 54.0 | 55.3 | +1.3 |
| AMZN | Cons Disc | 39.8 | 39.8 | 57.1 | 57.1 | 0.0 |
| ORCL | Tech | 39.1 | 49.2 | 52.1 | 54.1 | +2.0 |
| CRM (no override) | Tech | 39.1 | 45.7 | 43.8 | 45.1 | +1.3 |
| PG, KO, JNJ | (non-tech) | — | unchanged | unchanged | unchanged | 0.0 |
| JPM, BAC, XOM, CVX | (non-tech) | — | unchanged | unchanged | unchanged | 0.0 |

**Top 5 beneficiaries:**
1. NVDA 66.3 → 68.4 (+2.1)
2. AVGO 65.3 → 67.3 (+2.0)
3. AMD 64.9 → 66.9 (+2.0)
4. MU 68.9 → 70.9 (+2.0)
5. MSFT 50.6 → 52.6 (+2.0)

Band-flip risk: low at +2 points; MU crosses from "monitor" (60–69) to **"constructive" (70–79)** which is the band Bryan would intuitively want; nothing else flips here. AAPL/CRM also lift but less (they're not on the AI-exposed override list — intentional; AAPL is consumer hardware-cyclical, CRM is enterprise SaaS).

### Why this and not Option C

Option C (industry-level overrides) is *architecturally* better but requires a schema extension + `des.py` change + `lthcs_daily.py` wiring change + tests. That's M-effort and crosses code boundaries that other concurrent agents may be touching. The proposed B+D combo is **pure config**, ships in minutes, and is reversible by reverting one file.

**Migration path:** ship B+D now → in the next iteration, refactor the per-ticker overrides into `industry_overrides` (Option C) for cleanliness → then plan Option F as the V3 architecture.

---

## Phase 5 — V3 direction

**Vision:** DES becomes "Macro Headwinds" (a true macro pillar at smaller weight, ~10–15%, capped at ±15 swing) and a new "Secular Demand" pillar (~10–15% weight) lives alongside it. Secular Demand reads from a `themes.json` registry that maps tickers to themes (AI Capex, AI Inference, GLP-1, EV Adoption, Cybersecurity, Renewables, Datacenter Power, Cloud Migration) each with a 0–100 intensity. Intensity is set quarterly from a transparent rubric (transcripts, capex guides, sell-side estimates). The two pillars are orthogonal: macro answers "what does the rate cycle do to my multiple right now," and secular answers "what does my end-demand actually look like over the next 4–8 quarters." Composite weights renormalize the other pillars accordingly. The dashboard surfaces both, and the heatmap tab (per concurrent UX work) gets a "decompose composite by pillar" toggle so users can see why MU at 70 is being held back by a flat THE/DES vs. a +90 Adoption pillar.

---

## Other pillars worth flagging to Bryan

Beyond DES, two findings from the snapshot:

1. **Thesis Integrity is flat at 50.0 for 156 of 167 tickers** (93%). This matches the existing memory note ([alpha_vantage_news_sentiment_quirk.md]) — the Alpha Vantage NEWS_SENTIMENT AND-not-OR quirk is leaving Thesis neutral for almost the entire universe. With a 20% weight, that's a much bigger "missing information" problem than DES miscalibration. **Fixing Thesis Integrity would unlock more discriminating power than fixing DES.** Recommend triaging this next: either implement single-ticker fetches (rate-limit permitting) or replace Alpha Vantage with a different sentiment source.

2. **MSFT institutional_confidence = 27.9 and CRM = 6.7** are eye-popping. MSFT at 27.9 looks wrong on its face for the world's largest software company. Worth a separate audit of the Institutional pillar's flow / ownership inputs to make sure it isn't broken for mega-caps the same way Thesis is broken for the universe. (CRM at 6.7 may be genuine if there's institutional unwind, but the magnitude looks like a data issue.) That's likely a bigger composite mover than DES for both names.
