# City Tab — Phase-0 Recon (data contract freeze)

**Probed:** 2026-05-31 (live `curl` against each portal). **Methodology v1.0.**
**Scope of this doc:** confirm every date column, resolve all `verify`/`RESOLVE_AT_BUILD` placeholders, freeze the output schema. No fetcher/scorer/app code written.

---

## 1. Socrata feeds — date-column + recency verification

Date columns confirmed authoritatively via each dataset's `/api/views/{id}/columns.json` (returns `fieldName` + `dataTypeName`), cross-checked with a one-row `?$limit=1` probe and a `date_trunc_ym(...)` monthly-count aggregation. Today = 2026-05; "complete month" excludes the in-progress May.

| City | Feed | Dataset | Listed col | **Confirmed col** | Type | Most-recent month present | Last COMPLETE month | Lag note |
|---|---|---|---|---|---|---|---|---|
| Chicago | 311 | `v6vf-nfxy` | `created_date` | `created_date` ✅ | calendar_date | 2026-05 | **2026-04** | already-confirmed; near-realtime |
| Chicago | Crimes | `ijzp-q8t2` | `date` | `date` ✅ | calendar_date | 2026-05 (13,636) | **2026-04** | excludes last ~7 days → May partial; latest *complete* month is Apr |
| Chicago | Permits | `ydr8-5enu` | `issue_date` | `issue_date` ✅ | calendar_date | 2026-05 | **2026-04** | low lag |
| NYC | 311 | `erm2-nwe9` | `created_date` | `created_date` ✅ | calendar_date | (huge table; metadata-confirmed) | **2026-04** | ~38M rows — `$limit=1`/aggregation TIMES OUT keyless; needs token + `$where` recent filter |
| NYC | NYPD complaints (YTD) | `5uac-w243` | `cmplnt_fr_dt` | `cmplnt_fr_dt` ✅ | calendar_date | 2026-03 | **2026-03** | YTD file; ~1-month publish lag (no Apr at probe time) |
| NYC | NYPD complaints (historic baseline) | `qgea-i56i` | `cmplnt_fr_dt` | `cmplnt_fr_dt` ✅ | calendar_date | (metadata-confirmed) | n/a (frozen historic) | large; throttled keyless |
| NYC | DOB permits | `ipu4-2q9a` | `issuance_date` | `issuance_date` ⚠️ **TEXT** | **text (MM/DD/YYYY)** | — | — | **`date_trunc_ym` FAILS (type-mismatch).** See §1a. |
| LA | MyLA311 (2025, dead) | `h73f-gn57` | `createddate` | `createddate` ✅ | calendar_date | 2025-11 (tail counts 1–39) | **dead ~2025-08** | **ROTATED OUT — do not poll for current.** See §2. |
| LA | LAPD NIBRS (live) | `k7nn-b2ep` | `date_occ` | `date_occ` ✅ | calendar_date | 2026-05 (3,688 partial) | **2026-04** | live; dense start 2026-01 |
| LA | LAPD NIBRS (baseline) | `y8y3-fqfu` | `date_occ` | `date_occ` ✅ | calendar_date | 2025-12 | **2025-12** | baseline ends 2025-12 (clean union seam, see §2) |
| LA | B&S permits | `pi9x-tg5x` | `issue_date` | `issue_date` ✅ | calendar_date | 2026-05 | **2026-04** | low lag |
| Seattle | 311 (Find It Fix It) | `5ngg-rpne` | `created_date` | **`createddate`** ❌→ corrected | calendar_date | 2026-05 | **2026-04** | **CORRECTED: one word, no underscore** |
| Seattle | SPD crime | `tazs-3rd5` | `report_datetime` | **`report_date_time`** ❌→ corrected | calendar_date | 2026-05 | **2026-04** | **CORRECTED: `report_date_time`** (alt: `offense_date`). 2019 RMS breakpoint stands. |
| Seattle | Building permits | `76t5-zqzr` | `issueddate` | `issueddate` ✅ | calendar_date | 2026-05 | **2026-04** | confirmed via metadata (a `$limit=1` row had it null → looked "missing"). **Must filter `issueddate IS NOT NULL`** — a large null bucket = un-issued applications. |
| SF | 311 | `vw6y-z8j6` | `requested_datetime` | `requested_datetime` ✅ | floating_timestamp | 2026-05 | **2026-04** | near-realtime |
| SF | Police incidents | `wg3w-h783` | `incident_datetime` | `incident_datetime` ✅ | floating_timestamp | 2026-05 | **2026-04** | near-realtime |
| SF | Building permits | `i98e-djp9` | `permit_creation_date` | `permit_creation_date` ✅ | calendar_date | 2026-05 | **2026-04** | low lag |

**Net:** 2 hard corrections (Seattle 311 `createddate`, Seattle crime `report_date_time`), 1 false-negative resolved (Seattle permits `issueddate` is fine), 1 type-trap (NYC DOB text date), rest confirmed.

### 1a. NYC DOB permits — text-date trap (build BLOCKER if unaddressed)
`ipu4-2q9a` stores **all** permit dates as `text`, not `calendar_date`: `issuance_date`, `filing_date`, `job_start_date`, `expiration_date` are all text in `MM/DD/YYYY` form (sample: `06/17/2020`). Only `dobrundate` is a real `calendar_date`. Consequences/options:
- `date_trunc_ym(issuance_date)` → SoQL error `query.soql.type-mismatch ... is text`.
- **Option A (recommended):** bucket by parsing the text — month key = `substring(issuance_date,7,4) || '-' || substring(issuance_date,1,2)` (YYYY-MM), grouped client-side or via `$select` of those substrings. Filter `issuance_date IS NOT NULL`.
- **Option B:** use `dobrundate` (the pipeline run date, real timestamp) as a ~1-day-lagged proxy for issuance. Simpler but semantically "loaded date" not "issued date" — disclose if used.

---

## 2. LA period-split — RESOLVED

**LA changed its 311 product mid-2025.** The "MyLA311 Service Request Data {YEAR}" series the registry pointed at (`h73f-gn57` = 2025) **stopped receiving data ~Aug 2025** (probe shows dense through 2025-08, then trailing single-digit counts to 2025-11, nothing in 2026). There is **no `...Data 2026`** in that series.

The current product is a **new "MyLA311 Cases" series** (catalog search on `data.lacity.org`):
- **`2cy6-i7zn` — "MyLA311 Cases 2026"** → THIS is the current-year feed. Date col **`createddate`** (same name). Dense & live: 2026-05 = 185,246 rows. **Use this for LA Services.**
- `73a2-6ar5` — "MyLA311 Cases March 2025 to December 2025" → bridge/baseline for the new series, also `createddate`.
- Legacy "…Service Request Data" IDs (`...2015`–`2025`) remain queryable but frozen per year.

**Resolution convention for the build:** search the catalog `https://data.lacity.org/api/catalog/v1?q=MyLA311%20Cases&limit=20`, pick the item titled `MyLA311 Cases {currentYear}`; baseline = union with the prior "Cases" file(s). Do NOT auto-construct a `…Service Request Data {year}` ID — that series is retired.

**LA crime union seam (CONFIRMED):**
- Baseline `y8y3-fqfu` complete through **2025-12** (Dec = 11,950).
- Live `k7nn-b2ep` dense from **2026-01** (Jan = 13,059) → 2026-04 complete.
- Clean seam, no overlap, no gap: UNION = `y8y3-fqfu (…→2025-12)` ∪ `k7nn-b2ep (2026-01→)`, both keyed on `date_occ`. (Note: both tables contain a handful of dirty pre-2010 dates from data-entry errors — clip the baseline window to the trailing 12–24 months; do not trust the raw `MIN(date_occ)`.) Legacy `2nrs-mtv8` left untouched.

---

## 3. Miami / Miami-Dade — ArcGIS findings (and the real blocker)

ArcGIS Hub items resolve to underlying FeatureServers via `https://www.arcgis.com/sharing/rest/content/items/{id}?f=json` → `.url`.

### Permits — GOOD ✅
- Hub item `31cd319f45544648b59f0418aea60091` ("Building Permit")
- **FeatureServer:** `https://services.arcgis.com/8Pc9XBTAsYuxx9Ny/arcgis/rest/services/BuildingPermit_gdb/FeatureServer`
- **Layer 0** (`BuildingPermit`), **date field `ISSUDATE`** (esriFieldTypeDate), `supportsStatistics: true`, `maxRecordCount: 2000`.
- **Date range `ISSUDATE`: 1982-09-22 → 2026-05-21** (current). Plenty for 12-mo baseline + YoY (the "rolling 3-yr window" caveat is conservative; this layer goes back to 1982).

### 311 — STALE ⚠️ (Miami Services pillar at risk)
- Registry's 311 Hub item `6fce6d69cdd8447389894138444aea2d` = **"311 Service Requests - Miami-Dade County - 2023"**.
- **FeatureServer:** `https://services.arcgis.com/8Pc9XBTAsYuxx9Ny/arcgis/rest/services/data_311_2023/FeatureServer` — note 311 is exposed as a **Table** (`/0`, `tables[]`), not a `layers[]` entry.
- **Date field `ticket_created_date_time`** (esriFieldTypeDate). Bonus: precomputed integer **`created_year_month`** (e.g. `202301`) and `ticket_year` exist → month-bucketing without date math.
- **BUT date range = 2023-01-01 → 2024-01-01.** This is a frozen **yearly snapshot**, not a live feed. The whole County-311 publication scheme is per-year snapshots (`…- 2013` … `…- 2023`); **latest available year = 2023.** So Miami's Services pillar would be ~2 years stale.
- **Alternates checked, all dead-ends for a current County feed:**
  - "311 Service Requests" `b74b1dd0…` → max `Ticket_Created_Date___Time` = **2018-08** (stale).
  - "311 Service Request" `a6b9c3e3…` (org `8Pc9…`, `Citizen311ServiceRequest` table) → **only `OBJECTID` exposed, no usable fields/dates** (unusable).
  - "City of Miami 311 Service Requests Since 2015" `7cc10915…` → max **2024-08**, but this is **City-of-Miami-proper** (urban core) — wrong footprint vs the County scope, and also stale.
- **Recommendation:** Build the Miami-311 adapter to resolve the latest yearly snapshot dynamically (catalog title pattern `311 Service Requests - Miami-Dade County - {year}`), use `created_year_month` for bucketing, and **treat Miami Services as `stale`** (status flag) until/unless the County publishes a current-year layer. Realistically Miami ships **2-of-3 pillars** in P0 (Dev/Econ + the FBI-pending Safety), with Services flagged stale. `opendata.miamidade.gov` is the same ArcGIS-Hub org (not Socrata SODA) and exposes the same yearly snapshots — no fresher 311 there either. The spec's "Socrata-style 311 BETA on opendata.miamidade.gov" was **not found**; everything is ArcGIS FeatureServer.

### ArcGIS month-bucketing approach (no `date_trunc`)
ArcGIS has no `date_trunc_ym`. Three viable approaches, in order of preference:
1. **Precomputed field** — the 2023 311 table already has `created_year_month` (int) and `ticket_year`; group on that directly.
2. **`outStatistics` + `groupByFieldsForStatistics`** with a SQL date expression (server `supportsStatistics: true`). `EXTRACT(YEAR FROM …)` support is server-dependent and was flaky in probes — verify per layer.
3. **Date-range loop / client-side bucketing** (most robust): issue 12–24 monthly count queries `where=ISSUDATE >= timestamp 'YYYY-MM-01' AND ISSUDATE < 'next-month'` using `returnCountOnly=true`, or pull rows (respect `maxRecordCount: 2000` → paginate with `resultOffset`) and bucket in code. **Recommended for permits** (`ISSUDATE`), since it sidesteps EXTRACT support questions. Query shape confirmed working: `…/FeatureServer/0/query?where=1=1&outStatistics=[{"statisticType":"count","onStatisticField":"OBJECTID","outStatisticFieldName":"n"}]&f=json` (OBJECTID field is `ObjectId` on the 311 table, `OBJECTID` on permits — case matters; read it from `objectIdField` in layer metadata).

---

## 4. FBI CDE — Miami-Dade ORI RESOLVED

- **Agencies endpoint (keyless, works):** `https://cde.ucr.cjis.gov/LATEST/agency/byStateAbbr/FL` → JSON grouped by county; each agency has `ori`, `agency_name`, `agency_type_name`, `is_nibrs`, `nibrs_start_date`.
- **Resolved county agency:** **"Miami-Dade County Police Department" → ORI `FL0130000`** (type `County`, `is_nibrs: true`, NIBRS start **2022-01-01**).
  - Naming note: spec says "Miami-Dade Sheriff's Office (MDSO)". The county PD↔Sheriff transition (2025) hasn't renamed the CDE entry; the **county-footprint ORI is `FL0130000`** — that is the correct one for Miami-Dade County scope.
  - For reference, City-of-Miami-proper PD = `FL0130600` (if the scope ever flips to the urban core).
- **Data API:** `https://cde.ucr.cjis.gov/LATEST/...` (e.g. NIBRS offense counts by ORI) requires a **free api.data.gov key** appended as `?API_KEY=...`. **The agencies/ORI list is keyless; the data series is NOT** — so the Safety series itself cannot be fully exercised in P0 without the key. NIBRS data exists from 2022-01 → a 12-mo baseline + YoY is feasible once keyed.
- Coverage caveat (per spec §4) stands: FBI reporting completeness varies; treat as best-effort fallback.

---

## 5. Census ACS + Layer B

- **Latest ACS5 vintage = 2024** — confirmed available: `https://api.census.gov/data/2024/acs/acs5.json` lists `c_isAvailable: true` (modified 2025-09-02). All 4 KPI vars resolve in 2024: `B19013_001E` (median HH income), `B25064_001E` (median gross rent), `B25077_001E` (median home value), `B25103_001E` (median real-estate taxes). **Target vintage 2024.** (2023 also live as fallback.)
- ⚠️ **Census now REQUIRES a key for data queries.** A keyless `…/acs/acs5?get=…&for=place:…` returns an HTML **"Missing Key"** page (the spec's "key recommended, not required" is no longer true for data calls; metadata endpoints like `variables.json` remain keyless). Treat `CENSUS_API_KEY` as **required**, not optional.

- **Place FIPS (verified via TIGERweb Incorporated Places, layer 4):**
  | City | State FIPS | Place FIPS | GEOID | Census name |
  |---|---|---|---|---|
  | Chicago | 17 | 14000 | 1714000 | Chicago city |
  | New York | 36 | 51000 | 3651000 | New York city |
  | Los Angeles | 06 | 44000 | 0644000 | Los Angeles city |
  | Seattle | 53 | 63000 | 5363000 | Seattle city |
  | San Francisco | 06 | 67000 | 0667000 | San Francisco city |
  | **Miami (city-of-Miami)** | 12 | 45000 | 1245000 | Miami city |
  | **Miami-Dade County** | 12 | county 086 | 12086 | Miami-Dade County |

- **Miami geography decision (FLAGGED):** the ops feeds (ArcGIS 311 + permits, FBI ORI `FL0130000`) are all **Miami-Dade County (12086)**. The City-of-Miami `place` (1245000) covers only the urban core (~440K people) vs the County (~2.7M). **Recommendation: Context layer should use Miami-Dade County (`for=county:086&in=state:12`, GEOID 12086) to match the Pulse footprint** — keeps both layers describing the same population. Mismatch to note in the panel: every other city's Context is a `place`; Miami's is a `county`, so Miami's Context numbers (income/rent/home value) are county-wide, not city-core. (ACS supports `county` geography for all the B-table vars, so this is clean.)

- **Layer B key requirements / standardized env-var names** (for `.env.example` + CI later):
  | Source | Env var | Key needed? |
  |---|---|---|
  | Socrata (all 5 hosts) | `SOCRATA_APP_TOKEN` | recommended (hard keyless throttling — see §6) |
  | Census ACS | `CENSUS_API_KEY` | **required** (was "recommended") |
  | BLS LAUS | `BLS_API_KEY` | free; raises daily limits |
  | EPA AirNow | `AIRNOW_API_KEY` | **required** for AirNow API |
  | FBI CDE / api.data.gov | `FBI_CDE_API_KEY` | **required** for data series (not for ORI list) |
  | (HUD, if used) | `HUD_API_KEY` | required for HUD USER API |

---

## 6. Socrata throttling note

- Keyless requests are throttled hard. During probing, repeated keyless calls to the large NYC datasets returned **HTTP 429 `{"errorCode":"too-many-requests"}`** and several large-table queries **timed out** (empty body) without a token.
- Socrata does **not** emit `X-RateLimit-*` headers on success — a 200 response carries only `X-SODA2-Fields` / `X-SODA2-Types` / `X-SODA2-*` metadata headers (verified on SF). Throttle surfaces as 429s, not as rate-limit counters.
- **Recommendation (confirmed):** register **one** free Socrata app token, store as **`SOCRATA_APP_TOKEN`**, pass as `$$app_token=<token>` query param **or** `X-App-Token: <token>` header. A Socrata app token is **portal-agnostic** — the same token works across all 5 hosts (data.cityofchicago.org, data.cityofnewyork.us, data.lacity.org, data.seattle.gov, data.sfgov.org). Reuse it everywhere; it lifts the throttle into the comfortable tier for monthly-aggregation calls.

---

## 7. SURPRISES / BLOCKERS (read this, swarm)

1. **Seattle date columns were wrong in two of three feeds.** 311 = `createddate` (not `created_date`), crime = `report_date_time` (not `report_datetime`). A wrong col yields a zero series → garbage score. **Fixed in `city_registry.resolved.json`.**
2. **NYC DOB permits date is TEXT (`MM/DD/YYYY`), not a date type.** `date_trunc_ym` errors out. Must substring-parse `issuance_date` or use `dobrundate` proxy (§1a). Build BLOCKER until handled.
3. **LA 311 "Service Request Data" series is RETIRED (~Aug 2025).** Current feed is the new **`2cy6-i7zn` "MyLA311 Cases 2026"** (`createddate`). The registry's `h73f-gn57` is dead for current data. (§2)
4. **Miami 311 has NO current County feed.** Everything is frozen yearly snapshots; latest = 2023. The registry's layer is the 2023 snapshot. No Socrata BETA exists at opendata.miamidade.gov (it's ArcGIS Hub). **Miami Services pillar is effectively stale/unavailable in P0 → Miami ships 2-of-3 pillars** (Dev/Econ live; Safety pending FBI key; Services stale). (§3)
5. **Miami Safety needs an api.data.gov key.** ORI resolved (`FL0130000`) but the CDE data series is keyed. Until `FBI_CDE_API_KEY` is set, Miami Safety = `not_published`/pending. So in P0 Miami may render with **1 truly-live pillar (Dev/Econ)** + 1 stale (Services) + 1 pending (Safety). Score honestly with `pillars_present`.
6. **Census now requires a key for data calls.** Treat `CENSUS_API_KEY` as required for P1 Context.
7. **Large NYC/Chicago tables time out keyless** — the token isn't just nice-to-have; without it the NYC 311 (`erm2-nwe9`, ~38M rows) and historic NYPD baseline calls fail. Always pass a recent-window `$where` AND the app token for these.
8. **Reporting-lag alignment:** Chicago crime drops the last ~7 days (May shows partial). Across all feeds the latest **complete** month at probe time is **2026-04** (NYPD YTD lags to 2026-03). Set `as_of = 2026-04` and align every "Recent" to the last complete month per feed.
9. **Null-date buckets:** Seattle permits (`issueddate`) and ArcGIS permits have large null/un-issued buckets — filter `IS NOT NULL` before bucketing or counts inflate.
