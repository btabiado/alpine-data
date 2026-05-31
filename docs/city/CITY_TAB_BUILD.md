# City Tab — Build File (v1)

**Single source of truth** for the `City` tab. Consolidates and supersedes `ROADMAP_v1.md` §2a and `CITY_TAB_SPEC.md`. Everything resolvable has been resolved; everything left open is open *by design* (flagged below).

- **Slate (6):** Chicago · NYC · LA · Seattle · SF · Miami
- **Two layers:** **A — City Pulse** (within-city operational momentum) · **B — City Context** (cross-city levels + tax data)
- **Adapters:** Socrata/SODA (5 cities) · ArcGIS Hub (Miami) · national REST APIs (Layer B)

---

## 1. What the tab does

Landing = a grid of **city cards**, each showing a **City Pulse** score (0–100) headline + a **City Context** strip (e.g., median rent, effective property tax). Click a card → detail view: each pillar's score, every feed's trend + Δ, and the Context KPIs. If a city doesn't publish a feed, show "Not published by this city" — never an empty panel.

---

## 2. City Pulse — scoring methodology (canonical)

> If you change the math, change it here only. This is the one place it lives now.

**Principle: each city vs its own history, never vs other cities.** Raw counts aren't comparable across cities (different systems, populations, definitions). Every feed is scored as a deviation from *that city's own trailing baseline*. Pulse answers "is this city trending better or worse than its own normal?" — `50 = on its own baseline`, `>50 favorable`, `<50 unfavorable`.

**Per-feed score**
1. Monthly time series of the feed's primary metric `M` (usually `count(*)`; sometimes a rate, e.g. 311 time-to-close).
2. Baseline: trailing-12-month mean `μ`, std `σ`. Prefer **year-over-year** framing for the headline (Recent vs same period last year) to neutralize seasonality; z-score carries the magnitude.
3. `Recent` = most recent **complete** period (align to complete data — see lag caveat).
4. `z = clip((Recent − μ) / σ, −3, +3)`.
5. Polarity `p ∈ {+1, −1, 0}`: `+1` up-is-favorable (permits, licenses); `−1` down-is-favorable (crime, 311 backlog); `0` ambiguous → context only (raw 311 volume).
6. Directional contribution `d = p × z`.

**Pillars → composite** (equal weight within & across pillars by default)
- **Public Safety** — crime/incidents (`p = −1`)
- **Development & Economy** — permits, business licenses (`p = +1`)
- **City Services** — 311 time-to-close/backlog (`p = −1`); 311 volume = context only
- Pillar `P_k` = mean of its feeds' `d`. Composite `C` = weighted mean of pillars (≈ −3…+3).
- **Map:** `Pulse = round(50 + (C / 3) × 50)`, clipped [0, 100].
- **Coverage honesty:** missing a pillar → compute on what's present, label "N of 3 pillars." Never treat missing as zero.

**Display** — Card: Pulse + glyph (▲/▬/▼) + label + "N of 3 pillars." Detail: each pillar score and each feed's YoY % and z, laid out like the existing crypto signal-score breakdown.

**Caveats to ship inline**
- Not a cross-city ranking — "SF 62 vs Chicago 55" is not "SF is better."
- Polarity is an editorial choice — disclose every feed's polarity.
- Data-continuity breaks cause artificial jumps (SPD's 2019 RMS change; LA's yearly dataset rotation; SF portal migration) — flag known breakpoints.
- Reporting lag — Chicago Crimes excludes the last 7 days; align "Recent" to complete periods.

---

## 3. Layer A registry — operational feeds (RESOLVED)

Adapter: `socrata` unless noted. All dataset IDs verified live against each portal (May 2026). Date columns marked `*` are confirmed; unmarked are best-known and must be confirmed at build with a one-row `?$limit=1` probe (standard step, applies to all).

| City | Adapter · host | Pillar | Feed | Dataset ID | Date col |
| --- | --- | --- | --- | --- | --- |
| **Chicago** | socrata · data.cityofchicago.org | Services | 311 Service Requests | `v6vf-nfxy` | `created_date`* |
| Chicago | | Safety | Crimes 2001–present | `ijzp-q8t2` | `date`* |
| Chicago | | Dev/Econ | Building Permits | `ydr8-5enu` | `issue_date`* |
| Chicago | | Dev/Econ | Business Licenses *(optional)* | *build-time* | `license_start_date` |
| **NYC** | socrata · data.cityofnewyork.us | Services | 311 (2010–present) | `erm2-nwe9` | `created_date`* |
| NYC | | Safety | NYPD Complaints — YTD · Historic | `5uac-w243` · `qgea-i56i` | `cmplnt_fr_dt`* |
| NYC | | Dev/Econ | DOB Permit Issuance | `ipu4-2q9a` | `issuance_date` |
| **LA** | socrata · data.lacity.org | Services | MyLA311 — 2025 · 2024 | `h73f-gn57` · `b7dx-7gc3` | `createddate` |
| LA | | Safety | LAPD NIBRS Offenses — 2026→ (live) · 24–25 (baseline) | `k7nn-b2ep` · `y8y3-fqfu` | `date_occ` |
| LA | | Dev/Econ | Building & Safety Permits 2020→ | `pi9x-tg5x` | `issue_date` |
| **Seattle** | socrata · data.seattle.gov | Services | Customer Service Requests (Find It Fix It) | `5ngg-rpne` | `created_date` |
| Seattle | | Safety | SPD Crime Data 2008–present | `tazs-3rd5` | `report_datetime`* |
| Seattle | | Dev/Econ | Building Permits | `76t5-zqzr` | `issueddate` |
| **SF** | socrata · data.sfgov.org | Services | 311 Cases | `vw6y-z8j6` | `requested_datetime`* |
| SF | | Safety | Police Incident Reports 2018→ | `wg3w-h783` | `incident_datetime`* |
| SF | | Dev/Econ | Building Permits | `i98e-djp9` | `permit_creation_date`* |
| **Miami** *(= Miami-Dade County)* | **arcgis** · gis-mdc.opendata.arcgis.com | Services | Miami-Dade 311 Service Requests | layer `6fce6d69cdd8447389894138444aea2d` | ArcGIS attr (verify) |
| Miami | | Dev/Econ | Miami-Dade Building Permit (rolling 3 yr) | layer `31cd319f45544648b59f0418aea60091` | ArcGIS attr (verify) |
| Miami | **fbi** (fallback) | Safety | FBI CDE — Miami-Dade Sheriff's Office (MDSO) | ORI: resolve at build | — |

**Two structural watch-items (handled in code, not lookups):**
- **LA is period-split.** 311 rotates yearly → resolve the current-year dataset by convention (or union recent years). Crime live feed `k7nn-b2ep` starts 2026 → **union with `y8y3-fqfu` (24–25)** for a full 12-mo baseline. Legacy `2nrs-mtv8` is frozen — never poll it.
- **Miami = Miami-Dade County (decided).** Use the County portals: `gis-mdc.opendata.arcgis.com` (ArcGIS) + `opendata.miamidade.gov` (Socrata-style, has a 311 BETA + Certificates of Use). Two caveats: (1) **crime is still a gap** — Miami-Dade has a Sheriff's (MDSO) crime *dashboard*, not a clean open dataset, so use the FBI fallback (§4, resolve the MDSO ORI) or accept a 2-of-3 Pulse; (2) **County 311 excludes City-of-Miami-proper requests** (only County-serviced areas), so Miami's Services pillar reflects the county footprint, not the urban core — note this in the disclosure panel. The permits layer is a rolling 3-year window — enough for a 12-mo baseline + YoY, but historical depth is capped.

---

## 4. Layer B registry — cross-city context (national APIs)

One source set covers all 6 cities — no per-city dataset hunting. This is where **tax rates** and a **build-your-own city score** live.

| Source | API (free key) | KPIs |
| --- | --- | --- |
| **Census ACS 5-yr** | `api.census.gov/data/{yr}/acs/acs5` | income, rent, home value, real-estate taxes, poverty, owner-occupancy, commute, population |
| **BLS LAUS** | `api.bls.gov/publicAPI/v2` | local unemployment rate (monthly) |
| **EPA AirNow** | `airnowapi.org` | current AQI |
| **FBI CDE** | `cde.ucr.cjis.gov` (key via api.data.gov) | NIBRS crime by ORI — also the Miami Safety fallback |
| **HUD** | `huduser.gov` | Fair Market Rents, income limits |

**Effective property tax rate** (clean, no millage scraping):
`effective_rate = B25103_001E (median real-estate taxes paid) ÷ B25077_001E (median home value)` — one ACS call per city. *(Nominal sales-tax/millage rates aren't in any clean free per-city API; the ACS effective rate is the defensible cross-city tax KPI.)*

Useful ACS variable codes (confirm vintage at build): median household income `B19013_001E` · median gross rent `B25064_001E` · median home value `B25077_001E` · median real-estate taxes `B25103_001E`. Geography = `place` (city).

**FBI ORIs — resolve at build, don't hardcode.** The CDE API keys off each agency's ORI. Pull the agencies list from the API (its own frontend loads a JSON of every ORI + name) and match by city/state, then cache. **Coverage caveat:** FBI reporting completeness varies by agency and year (Chicago and some large agencies have historically underreported to UCR/NIBRS), so treat the FBI series as best-effort — fine as Miami's fallback, not a guaranteed-clean series everywhere.

**On "city scores":** no free, reusable index API exists (AARP Livability, Numbeo, EIU, IMD are proprietary web tools). Build a transparent 0–100 **City Context** composite from the sources above instead — same disclosure rules as City Pulse. **Keep Pulse and Context as two separate numbers, shown side by side; never merge them.**

---

## 5. Registry config

The machine-readable config the adapter reads is a standalone file: **`city_registry.json`** (kept in sync with this doc). It holds the `pulse` parameters (§2), one entry per city with resolved dataset IDs / ArcGIS layers / FBI fallback, and the `context_layer` sources (§4). Field conventions (`polarity`, `date_col_verify`, `dataset_rotates_yearly`, `baseline_dataset`) are documented in the file's `_meta` block.

## 6. Query patterns

**Socrata (SODA) — monthly series for baseline:**
```
GET https://{host}/resource/{dataset}.json
  ?$select=date_trunc_ym({date_col}) AS m, count(*) AS n
  &$where={date_col} >= '{baseline_start}'
  &$group=m&$order=m
  &$$app_token={token}
```
311 closure rate: two counts over the window (created vs `status='Closed'` by closed-date), take the ratio. Keep date/status column names in config, not hard-coded.

**ArcGIS Hub (Miami) — same shape, different API:**
```
GET https://{host}/datasets/{layer}_0.geojson  (or the FeatureServer /query endpoint)
  ?where={date_field} >= DATE '{baseline_start}'
  &outStatistics=[{"statisticType":"count","onStatisticField":"OBJECTID","outStatisticFieldName":"n"}]
  &groupByFieldsForStatistics={date_field truncated to month}
  &f=json
```

**App tokens / keys:** Socrata throttles hard keyless — register a free app token (`$$app_token` query or `X-App-Token` header). Each Layer-B source needs its own free key.

---

## 7. Build sequence

**P0 — ships v1**
- [ ] Implement Socrata adapter + City Pulse calculator (§2 math, §5 config).
- [ ] Wire the 5 Socrata cities, full 3-pillar Pulse on each card + click-through to feed trends.
- [ ] LA period-split logic: current-year 311 resolution; crime baseline `k7nn-b2ep` ∪ `y8y3-fqfu`.
- [ ] ArcGIS adapter + Miami (311 + permits); Miami scores 2-of-3 until FBI crime lands.
- [ ] One-row `?$limit=1` probe to confirm each `date_col` flagged `verify`.
- [ ] City Pulse methodology-disclosure panel (the caveats in §2) before the score goes live.

**P1 — context + polish**
- [ ] Layer B: Census ACS (incl. effective property tax), BLS, EPA AirNow → Context strip on cards.
- [ ] FBI CDE adapter: resolve ORIs from agencies endpoint, cache; fills Miami's Safety pillar.
- [ ] Flag known continuity breakpoints per feed (SPD 2019 RMS; LA yearly rotation; SF portal migration; Miami County-311 municipal exclusion).
- [ ] Per-pillar empty states + per-feed "last updated."

**P2 — extend**
- [ ] Expand per-city KPI lists beyond the backbone (food inspections, traffic crashes, towed vehicles, etc.) — *build-time, by design; Chicago is the template.*
- [ ] Optional transparent "City Context" composite score (with disclosure).
- [ ] CKAN adapter for non-Socrata/ArcGIS cities; cross-city compare view.

---

## 8. Status of open items (nothing is a hidden bug)

| Item | State |
| --- | --- |
| Layer A backbone IDs (5 Socrata cities) | ✅ resolved |
| Miami scope | ✅ decided — **Miami-Dade County** |
| Miami 311 + permits | ✅ resolved (County ArcGIS) |
| Miami crime | ✅ handled — FBI fallback (resolve MDSO ORI at build) or 2-of-3 |
| LA period-split feeds | ✅ handled in adapter logic |
| FBI ORIs | ⚙️ resolve programmatically at build (don't hardcode) |
| Per-city `date_col` confirmations | ⚙️ build-time probe (standard) |
| Deeper per-city KPI lists | 📋 template by design (P2) |
