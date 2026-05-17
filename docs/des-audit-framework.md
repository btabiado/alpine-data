# DES Audit Framework — White-Paper §6.1 Coverage vs. V1 Implementation

**Author:** research agent
**Date:** 2026-05-17
**Snapshot under audit:** `data/lthcs/snapshots/2026-05-17.json`
**Config under audit:** `data/lthcs/sector_des_weights.json` v1.0.0 (post 2026-05-16 calibration fix)
**Pipeline under audit:** `lthcs_daily.py::build_macro_inputs()`
**Companion doc:** `docs/des-analysis.md` (calibration diagnosis — recommended pre-read)

---

## 1. TL;DR

- DES wires **6 of ~25** plausible §6.1 macro signals. Coverage is best in **rates / price / labor / energy** (one signal each, the canonical proxy), and **zero** in **money supply, risk-regime, real-yield, broad inflation breadth, and sector-specific demand indicators (PMI, housing, durables)**.
- The single highest-impact gap is **real 10Y yield (DFII10)**. We currently model nominal 10Y, which conflates real-rate compression with the inflation premium. In a regime like today's (CPI 3.78%, nominal 10Y 4.47%, real 10Y ~0.7%) the pillar is double-counting inflation.
- The second is **VIX / risk regime**. There is no volatility-regime gate on DES tilts; the same +21bp move in 10Y contributes the same negative tilt whether VIX is 12 or 35. Real-world rate-sensitivity is highly non-linear in risk regime.
- The third is **M2 / liquidity**. Long-duration multiples respond to liquidity at least as much as to rates. Missing entirely.
- Per-ticker AI overrides (post 2026-05-16) suppress rate sensitivity for 12 names — they reduce the symptom (Tech flatness) but the underlying disease (DES = rate-sensitivity model labeled as "demand") is still present.
- Scope statement to keep handy: **DES today is a 6-signal rate-and-cycle pillar at 30-pt magnitude_scale.** Calling it "Demand Environment" oversells what's actually wired.

---

## 2. White-paper §6.1 inferred inventory

Bryan owns the LTHCS Intelligence White Paper externally; the agent does not have §6.1 verbatim. The inventory below is reconstructed from (a) `PHASE_1_BUILD_SPEC.md` §5 hints ("Inputs from FRED (CPI, Fed Funds, 10Y) and EIA (oil)"), (b) the §6.1 reference in `docs/lthcs-followups-queue.md`, and (c) standard macroeconomic best-practice for a "demand environment" composite. Signals are flagged with an inferred §6.1 likelihood (HIGH = clearly canonical to a demand pillar, MED = plausibly listed, LOW = best-practice addition that may not be in §6.1).

### 2.1 Real-rate / yield-curve

| Signal | §6.1 likelihood | Belongs in DES? | One-line rationale |
|---|---|---|---|
| 10Y Treasury yield (nominal) | HIGH | Yes | Canonical discount-rate proxy; long-duration multiple driver. |
| 10Y 30d Δ (bp) | HIGH | Yes | Trajectory signal; captures regime shifts the level misses. |
| 3M–10Y spread (curve slope) | HIGH | Yes | Inversion = recession lead-indicator; cyclicals care. |
| 2Y yield | MED | Yes | Front-end policy expectations; complements Fed Funds. |
| **Real 10Y yield (TIPS, DFII10)** | HIGH | **Yes — missing** | The actual rate that compresses long-duration assets. Nominal alone is wrong in a high-CPI regime. |
| Fed Funds rate | HIGH | Yes | Policy level; short-end discount rate. |
| M2 money supply growth | MED | Yes — missing | Liquidity drives risk-asset multiples independent of rates. |

### 2.2 Price / inflation

| Signal | §6.1 likelihood | Belongs in DES? | One-line rationale |
|---|---|---|---|
| CPI YoY | HIGH | Yes | Headline inflation; the canonical pillar input. |
| Core CPI (ex food/energy) | MED | Marginal | High collinearity with headline; useful only if headline is noisy. |
| PCE / Core PCE | MED | Marginal | Fed's preferred gauge; collinear with CPI for the V1 use case. |
| PPI | LOW | Marginal | Margin-pressure signal; covered indirectly by oil + CPI. |

### 2.3 Energy

| Signal | §6.1 likelihood | Belongs in DES? | One-line rationale |
|---|---|---|---|
| WTI crude (spot) | HIGH | Yes | US energy benchmark; the V1 wire. |
| Brent crude | MED | Marginal | High correlation with WTI; only matters if WTI–Brent spread is the signal. |
| Gasoline retail (regular) | MED | Yes — missing | Consumer-facing energy cost; closer to Consumer Discretionary demand than WTI. |
| Natural gas (Henry Hub) | MED | Yes — missing | Utility / industrial cost driver; distinct from oil. |
| WTI–gasoline crack spread | LOW | No | Refiner economics; too narrow for a sector-level pillar. |

### 2.4 Labor / consumer

| Signal | §6.1 likelihood | Belongs in DES? | One-line rationale |
|---|---|---|---|
| U-3 unemployment rate | HIGH | Yes | Canonical labor health gauge; the V1 wire. |
| U-6 (broader underemployment) | LOW | No | Highly collinear with U-3; adds little. |
| Nonfarm payrolls (Δ) | MED | Yes — missing | Trajectory signal; U-3 is a level. |
| Avg hourly earnings YoY | MED | Yes — missing | Consumer purchasing power; staples / discretionary demand input. |
| JOLTS openings | LOW | Marginal | Labor-market tightness; redundant once U-3 and earnings are in. |
| Consumer confidence (UMich / Conf Board) | MED | Yes — missing | Forward-looking demand for Consumer Disc. |

### 2.5 Capital markets / risk regime

| Signal | §6.1 likelihood | Belongs in DES? | One-line rationale |
|---|---|---|---|
| VIX | HIGH | Yes — missing | Risk regime gates rate sensitivity; V1 has no risk-off mechanism. |
| High-yield credit spread (BAML HY OAS) | MED | Yes — missing | Financing-conditions proxy; matters for Real Estate, Financials, low-quality Industrials. |
| Dollar index (DXY) | MED | Yes — missing | FX-driven earnings for multinationals; matters for Tech / Staples mega-caps. |
| Gold | LOW | No | Macro hedge; doesn't differentiate sectors usefully. |

### 2.6 Sector-specific demand indicators

| Signal | §6.1 likelihood | Belongs in DES? | One-line rationale |
|---|---|---|---|
| ISM Manufacturing PMI | HIGH | Yes — missing | The single best Industrials / Materials demand gauge. |
| ISM Services PMI | HIGH | Yes — missing | The Consumer Discretionary / Comm Services analog. |
| Housing starts | MED | Yes — missing | Real Estate / Materials direct demand input. |
| Durable goods orders | MED | Yes — missing | Industrials forward demand. |
| Retail sales (advance) | MED | Marginal | Consumer Disc demand; partly covered by consumer confidence + earnings. |

---

## 3. Implementation reality — master table

`In V1 config` = present as a key under `sector_des_weights.json::signal_normalization`.
`In V1 pipeline` = populated by `lthcs_daily.py::build_macro_inputs()`.
`In snapshot today` = surfaces with a non-null tilt in `variable_detail/2026-05-17.json`.
Live values are the snapshot's macro inputs (per `docs/des-analysis.md` Phase 1).

| Signal | Category | In V1 config | In V1 pipeline | In snapshot | Live value | Series ID | Phase 2 priority |
|---|---|:---:|:---:|:---:|---|---|---|
| CPI YoY | Inflation | ✓ | ✓ | ✓ | 3.78% | FRED `CPIAUCSL` (YoY derived) | — |
| Core CPI YoY | Inflation | ✗ | ✗ | ✗ | — | FRED `CPILFESL` | LOW (redundant w/ CPI) |
| PCE YoY | Inflation | ✗ | ✗ | ✗ | — | FRED `PCEPI` | LOW |
| Core PCE YoY | Inflation | ✗ | ✗ | ✗ | — | FRED `PCEPILFE` | LOW |
| PPI YoY | Inflation | ✗ | ✗ | ✗ | — | FRED `PPIACO` | LOW |
| Fed Funds rate | Rates | ✓ | ✓ | ✓ | 3.64% | FRED `FEDFUNDS` (or `DFEDTARU`) | — |
| 10Y yield (nominal) | Rates | ✓ | ✓ | ✓ | 4.47% | FRED `DGS10` | — |
| 10Y 30d Δ (bp) | Rates | ✓ | ✓ (derived) | ✓ | +21 bp | derived from `DGS10` | — |
| 2Y yield | Rates | ✗ | ✗ | ✗ | — | FRED `DGS2` | MEDIUM |
| 3M–10Y spread | Rates | ✗ | ✗ | ✗ | — | FRED `T10Y3M` | MEDIUM |
| **Real 10Y yield (TIPS)** | Rates | ✗ | ✗ | ✗ | — | FRED `DFII10` | **HIGH** |
| **M2 growth YoY** | Liquidity | ✗ | ✗ | ✗ | — | FRED `M2SL` (YoY derived) | **HIGH** |
| U-3 unemployment | Labor | ✓ | ✓ | ✓ | 4.30% | FRED `UNRATE` | — |
| U-6 underemployment | Labor | ✗ | ✗ | ✗ | — | FRED `U6RATE` | LOW |
| Nonfarm payrolls Δ | Labor | ✗ | ✗ | ✗ | — | FRED `PAYEMS` (MoM Δ) | MEDIUM |
| Avg hourly earnings YoY | Labor | ✗ | ✗ | ✗ | — | FRED `CES0500000003` | MEDIUM |
| Consumer confidence | Consumer | ✗ | ✗ | ✗ | — | FRED `UMCSENT` | MEDIUM |
| WTI crude spot | Energy | ✓ | ✓ | ✓ | $105.78 | EIA `PET.RWTC` | — |
| Brent crude spot | Energy | ✗ | ✗ | ✗ | — | EIA `PET.RBRTE` | LOW |
| Gasoline retail (regular) | Energy | ✗ | ✗ | ✗ | — | EIA `PET.EMM_EPMR_PTE_NUS_DPG` | MEDIUM |
| Natural gas (Henry Hub) | Energy | ✗ | ✗ | ✗ | — | EIA `NG.RNGWHHD.D` | MEDIUM |
| **VIX** | Risk | ✗ | ✗ | ✗ | — | FRED `VIXCLS` | **HIGH** |
| High-yield OAS | Risk | ✗ | ✗ | ✗ | — | FRED `BAMLH0A0HYM2` | MEDIUM |
| Dollar index (DXY) | Risk / FX | ✗ | ✗ | ✗ | — | FRED `DTWEXBGS` (broad) | MEDIUM |
| ISM Manufacturing PMI | Sector demand | ✗ | ✗ | ✗ | — | FRED `MANEMP` proxy / ISM direct | MEDIUM |
| ISM Services PMI | Sector demand | ✗ | ✗ | ✗ | — | ISM direct (no clean FRED) | LOW |
| Housing starts | Sector demand | ✗ | ✗ | ✗ | — | FRED `HOUST` | MEDIUM |
| Durable goods orders | Sector demand | ✗ | ✗ | ✗ | — | FRED `DGORDER` | LOW |

**Coverage scorecard.** Wired: 6 of 28 inferred signals (21%). Wired by §6.1-HIGH-likelihood-only: 6 of 14 (43%). Top three HIGH-likelihood gaps: real 10Y yield, M2, VIX. The pillar's blind spot is **liquidity and risk regime**, not breadth of inflation or labor proxies.

---

## 4. Per-sector sensitivity audit (V1 wired signals only)

Reading the post 2026-05-16 calibration. For each sector, one-line opinion + the most questionable cell.

| Sector | Verdict | Comment |
|---|---|---|
| Energy | Reasonable | Oil +0.70, CPI +0.30, unemployment −0.10 is textbook. Missing: nat-gas tilt for E&Ps with gas exposure. |
| Materials | Reasonable | Inflation-beneficiary; rate-hostile. Could justifiably raise CPI sensitivity above +0.30 in commodity-led inflation regimes. |
| Industrials | Reasonable but **under-spec'd** | Missing ISM Mfg PMI — the canonical Industrials demand signal. The −0.50 unemployment tilt is doing all the cyclicality work; that's lazy. |
| Consumer Discretionary | **Too oil-negative at sector level** | wti_oil_usd = −0.40 ignores that ICE-truck demand (F / GM) is roughly oil-neutral and EV demand (TSLA / LCID, already overridden) is oil-positive. Fine after ticker overrides; the *sector default* is miscalibrated. |
| Consumer Staples | Reasonable | Modestly defensive across the board. unemployment_pct = −0.20 is mild — could argue −0.30 (Staples sells more when employed) but it's debatable. |
| Health Care | Reasonable | Defensive, mildly rate-sensitive via biotech DCF tail. Could add a real-yield row when wired (Health Care biotech is more real-yield-sensitive than nominal). |
| Financials | Reasonable | +0.45 Fed Funds, +0.50 10Y, −0.35 unemployment is the canonical bank P&L sensitivity. Missing: HY OAS would catch credit-cycle risk that NIM-driven sensitivities don't. |
| Information Technology / Technology | Reasonable (post-fix) | The 2026-05-17 softening to −0.22/−0.22 (from −0.45) is well-motivated by `docs/des-analysis.md`. Note: this is the *sector* sensitivity; AI-tier ticker overrides further halve it for 12 names. |
| Communication Services | Reasonable (post-fix) | Same logic as Tech; the −0.15/−0.15 post-fix is appropriate. |
| Utilities | Reasonable but **probably under-rate-sensitive** | −0.55 on 10Y is the right sign but Utilities arguably want −0.65 to −0.75. They're the textbook bond-proxy. |
| Real Estate | Reasonable | −0.60 on 10Y is correct; −0.50 on Fed Funds is correct. Strongest negative rate beta in the model, which is right. |

Cross-cutting flags:
- **No ticker overrides exist for traditional financials, energy, or real estate.** Sector-level sensitivities are doing 100% of the work for ~140 names. The override mechanism is currently used purely for Tech/Comm AI exposure + 4 auto names (TSLA, LCID, F, GE). That's an asymmetric maintenance pattern.
- **No `_alias_of` resolution helper.** The `"Technology"` block carries duplicated sensitivity numbers (not a true alias). If the IT block changes, Technology must change in lockstep. Latent drift risk.

---

## 5. Specific gaps to flag (impact-ranked)

1. **Real 10Y yield is missing (FRED `DFII10`).** We model nominal 10Y at 4.47%, but the real 10Y is closer to 0.7% after CPI 3.78%. Long-duration multiple compression is a function of *real* rates, not nominal. Adding `real_10y_yield_pct` and routing the existing Tech / Real Estate / Utilities rate sensitivity primarily through it would be the single biggest realism upgrade.
2. **M2 growth is missing (FRED `M2SL` YoY).** Broad money is the cleanest available liquidity proxy. Risk assets re-rate on liquidity quarters before they re-rate on rates. Missing this means DES is structurally late.
3. **VIX is missing (FRED `VIXCLS`).** With no risk-regime input, the same 30-day yield move contributes the same tilt in a calm tape vs. a credit-event tape. A simple `vix_above_25` gate that doubles negative tilt magnitudes would be cheap and material.
4. **No sector-specific demand input for Industrials.** Industrials sensitivities all flow through generic rate/oil/unemployment dials. ISM Manufacturing PMI is the textbook signal here and its absence is the reason Industrials DES is currently undifferentiated from Materials.
5. **No HY credit spread (FRED `BAMLH0A0HYM2`).** Real Estate and lower-quality Financials are sensitive to financing conditions independent of risk-free rates. HY OAS captures this. Without it, REIT DES is solely a rate model.
6. **No 3M–10Y spread (FRED `T10Y3M`).** Curve inversion is the most reliable recession lead-indicator; cyclicals (Industrials, Materials, Discretionary) should have a negative tilt to inversion. Today's spread is informative and free to wire.
7. **No gasoline retail price.** Consumer Discretionary demand maps far more cleanly to *pump prices* than to WTI futures. WTI $105.78 might mean gasoline $4.20 or $4.80 depending on refinery margins. We currently assume linearity.
8. **No DXY.** Tech and Staples mega-caps generate 40–60% of revenue overseas. A strong dollar materially compresses earnings without any rate move. Missing entirely.
9. **No nonfarm payrolls trajectory.** U-3 is a level; PAYEMS Δ captures direction. A 4.30% U-3 means very different things if payrolls are +250k vs. −80k.
10. **No `magnitude_scale` config.** Currently a constant in `des.py` (`DEFAULT_MAGNITUDE_SCALE = 30.0`). Should live in the JSON config alongside the sensitivities. Flagged in `docs/des-analysis.md` Option A; still unfixed. Low effort.

---

## 6. Recommended Phase 2 build order

Three additions, ranked by ROI / effort. All three are pure-pipeline-and-config changes; only #1 needs a sensitivity row added to all 12 sectors.

### Phase 2.1 — Real 10Y yield (effort: M)

- Add to `build_macro_inputs()`:
  ```python
  try: real_10y = fred.get_latest_value("DFII10")
  except Exception: real_10y = None
  ...
  "real_10y_yield_pct": real_10y["value"] if real_10y else None,
  ```
- Add to `sector_des_weights.json::signal_normalization`:
  ```json
  "real_10y_yield_pct": {"low": -1.0, "high": 3.0, "neutral": 0.5,
    "_note": "10Y TIPS yield; below 0 = financial-repression-friendly, above 2 = restrictive"}
  ```
- Default sector sensitivities (suggested starting calibration):
  - Real Estate: −0.55 (and consider dropping nominal 10Y from −0.60 to −0.30 to avoid double-count)
  - Utilities: −0.50 (drop nominal to −0.30 similarly)
  - Information Technology / Technology: −0.20 (drop nominal to −0.10)
  - Communication Services: −0.15
  - Health Care: −0.20 (biotech DCF)
  - Financials: +0.30 (NIM benefits from real-rate widening)
  - Energy / Materials / Industrials / Staples / Discretionary: 0.00 to −0.10
- AI-tier ticker overrides: copy the existing pattern (`−0.10`) for the 12 AI names.
- Effort: **M.** One pipeline call + one config row across 12 sectors + override copy + tests + adjust nominal sensitivities to avoid double-counting.

### Phase 2.2 — VIX (effort: S)

- Add to `build_macro_inputs()`:
  ```python
  try: vix = fred.get_latest_value("VIXCLS")
  except Exception: vix = None
  ...
  "vix_level": vix["value"] if vix else None,
  ```
- Add to `signal_normalization`:
  ```json
  "vix_level": {"low": 10.0, "high": 35.0, "neutral": 18.0,
    "_note": "CBOE VIX index; >25 = risk-off regime"}
  ```
- Default sector sensitivities (high VIX = risk-off, generally bad for cyclicals and high-beta):
  - Information Technology / Technology / Communication Services: −0.20
  - Consumer Discretionary: −0.30
  - Financials (high-beta segment): −0.15
  - Industrials / Materials: −0.20
  - Consumer Staples / Health Care / Utilities: +0.10 (mild flight-to-quality benefit)
  - Real Estate: −0.10
  - Energy: −0.05
- Effort: **S.** Single signal, single pipeline call, one sensitivity row across sectors.

### Phase 2.3 — M2 growth YoY (effort: M)

- Add to `build_macro_inputs()` with a YoY derivation analogous to CPI:
  ```python
  try: m2_series = fred.get_series("M2SL")
  except Exception: m2_series = []
  ...
  "m2_growth_yoy_pct": _yoy_change_pct(m2_series),
  ```
- Add to `signal_normalization`:
  ```json
  "m2_growth_yoy_pct": {"low": -2.0, "high": 12.0, "neutral": 5.0,
    "_note": "M2 broad money YoY growth; negative = QT, double-digit = QE-style liquidity"}
  ```
- Default sector sensitivities (liquidity beta):
  - Information Technology / Technology / Communication Services: +0.20
  - Consumer Discretionary: +0.15
  - Real Estate: +0.20 (rate-equivalent liquidity benefit)
  - Financials: +0.10
  - Energy / Materials / Industrials / Staples / Health Care: +0.05 to +0.10
  - Utilities: +0.10
- Effort: **M.** Reuses the YoY helper; one config row across 12 sectors.

### Stretch — 3M–10Y spread, HY OAS, gasoline, DXY, PMI, payrolls, hourly earnings, consumer confidence

All FRED. Each is **S effort individually** (one series, one config row, sector sensitivities). Recommended to ship as a single Phase 2.4 batch *after* 2.1–2.3 land, so the calibration team can pick which six of these get in vs. wait for V3.

---

## 7. What we DON'T need (keep V2 lean)

- **Brent crude.** WTI–Brent correlation is ~0.96 over 5 years. Adding both is duplication.
- **Core CPI and PCE and Core PCE.** All three are 0.85+ correlated with headline CPI YoY at quarterly frequency. The only argument for adding them is for periods of high food/energy noise; the V1 use case doesn't need that resolution.
- **U-6 underemployment.** Highly correlated with U-3. Adds no sector discrimination.
- **Gold.** Doesn't differentiate sectors in any defensible way. It's a macro hedge, not a demand input.
- **JOLTS openings.** Once U-3 and hourly earnings are in, JOLTS adds no incremental discrimination.
- **WTI–gasoline crack spread.** Refiner economics; too narrow for a 12-sector pillar.
- **PPI.** Margin-pressure proxy; in practice it's covered by CPI + WTI for the V1 use case.
- **ISM Services PMI from raw ISM.** No clean FRED series; licensing-adjacent. Defer to V3.
- **Retail sales advance.** Already implied by consumer confidence + hourly earnings + unemployment combo.

**Heuristic:** if a candidate signal has > 0.7 correlation with something already wired and doesn't change sector relative-sensitivity, it doesn't earn its keep.

---

## 8. Reference table — signal → §6.1 guess → series ID → unit

| Signal name (key in config) | Likely §6.1 reference | Series ID | Unit | Frequency |
|---|---|---|---|---|
| `cpi_yoy_pct` | §6.1 inflation | FRED `CPIAUCSL` (YoY of index) | % | Monthly |
| `fed_funds_pct` | §6.1 policy rate | FRED `FEDFUNDS` | % | Daily / Monthly |
| `ten_y_yield_pct` | §6.1 long rate | FRED `DGS10` | % | Daily |
| `ten_y_30d_change_bp` | §6.1 rate-trajectory derived | derived from `DGS10` | bp | Daily |
| `unemployment_pct` | §6.1 labor | FRED `UNRATE` | % | Monthly |
| `wti_oil_usd` | §6.1 energy | EIA `PET.RWTC` | USD/bbl | Daily |
| `real_10y_yield_pct` *(Phase 2)* | §6.1 real rate | FRED `DFII10` | % | Daily |
| `m2_growth_yoy_pct` *(Phase 2)* | §6.1 liquidity | FRED `M2SL` (YoY) | % | Monthly |
| `vix_level` *(Phase 2)* | §6.1 risk regime | FRED `VIXCLS` | index | Daily |
| `two_y_yield_pct` *(Phase 2)* | §6.1 short rate | FRED `DGS2` | % | Daily |
| `curve_3m_10y_bp` *(Phase 2)* | §6.1 curve | FRED `T10Y3M` | bp | Daily |
| `hy_oas_pct` *(Phase 2)* | §6.1 credit | FRED `BAMLH0A0HYM2` | % | Daily |
| `dxy_index` *(Phase 2)* | §6.1 FX | FRED `DTWEXBGS` (broad TWI) | index | Daily |
| `gasoline_retail_usd_gal` *(Phase 2)* | §6.1 consumer energy | EIA `PET.EMM_EPMR_PTE_NUS_DPG` | USD/gal | Weekly |
| `natgas_henry_hub_usd_mmbtu` *(Phase 2)* | §6.1 industrial energy | EIA `NG.RNGWHHD.D` | USD/MMBtu | Daily |
| `payrolls_mom_k` *(Phase 2)* | §6.1 labor trajectory | FRED `PAYEMS` (Δ) | thousands | Monthly |
| `hourly_earnings_yoy_pct` *(Phase 2)* | §6.1 wages | FRED `CES0500000003` (YoY) | % | Monthly |
| `consumer_confidence` *(Phase 2)* | §6.1 sentiment | FRED `UMCSENT` | index | Monthly |
| `ism_mfg_pmi` *(Phase 2)* | §6.1 manufacturing | ISM direct (no FRED) | index | Monthly |
| `housing_starts_k` *(Phase 2)* | §6.1 housing | FRED `HOUST` | thousands | Monthly |
| `durable_goods_orders_yoy` *(Phase 2)* | §6.1 capex | FRED `DGORDER` (YoY) | % | Monthly |

---

## Appendix — auditor's checklist when grading a new signal

For any new candidate macro signal someone proposes for DES, walk this list before wiring:

1. Is it in §6.1, or is it a best-practice addition? (Don't smuggle pet signals into a "spec compliance" build.)
2. Correlation with already-wired signals — is it > 0.7 with any of them at quarterly frequency? If yes, redundant.
3. Does it discriminate *between sectors*? A signal that moves every sector by the same magnitude adds noise to the composite without adding information.
4. Series availability: is there a clean FRED / EIA endpoint with daily or monthly cadence? If it's behind a vendor paywall or requires scraping, defer to V3.
5. Is the sensitivity defensible from public data (correlations vs. sector earnings revisions, sector index returns)? If the answer is "vibes," document the methodology before merging.
6. Will it survive regime change? (Tech rate-sensitivity was −0.45 in 2018–2022 and is closer to −0.20 in the AI-capex era. Build the override pattern *in*, don't retrofit it.)
