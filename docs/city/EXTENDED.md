# City Tab — Extended (Supplementary) Feeds

**Companion to `city_registry.extended.json`.** P2 of the `CITY_TAB_BUILD.md` build sequence: expand each city's KPI list *beyond* the 3-pillar backbone. These feeds are **supplementary, display-only** — they are **NOT folded into the City Pulse composite** (every feed carries `supplementary: true`). They run through the **existing** Socrata adapter (`city/socrata.py` `monthly_counts` / `feed_series`) unchanged — resolution + registration only, no adapter code.

- **Resolved:** 2026-05-31 via live `curl` probes (one `?$limit=1` to confirm dataset id + column, plus a `date_trunc_ym(date_col)` monthly aggregation to confirm recent data).
- **Latest COMPLETE month** across all feeds: **2026-04** (May 2026 is the current incomplete month). Matches `city_registry.resolved.json` `as_of_complete_month`.
- **Scope:** Chicago is the template (4 feeds). Analogous extras added for NYC, SF, Seattle. **Miami (ArcGIS) intentionally skipped.** **LA — no clean live feed found** (see Blockers).
- **Polarity:** `+1` up-favorable · `-1` down-favorable · `0` context-only (raw counts not clearly good/bad).

## Resolved feeds

| City | Label | Dataset | Date col | Polarity | Rationale | Latest month | pillar_hint |
| --- | --- | --- | --- | :---: | --- | :---: | --- |
| **Chicago** | Food Inspections | `4ijn-s7e5` | `inspection_date` | **0** | Inspection *volume* is workload, not an outcome — the signal is the Fail/Pass `results`, not how many ran | 2026-04 | context |
| **Chicago** | Traffic Crashes | `85ca-t3if` | `crash_date` | **-1** | Fewer reported crashes is favorable (Vision-Zero framing); dense ~8-9k/mo | 2026-04 | public_safety |
| **Chicago** | Towed Vehicles | `ygr5-vcbg` | `tow_date` | **0** | Tow counts reflect enforcement/relocation activity, not safety/wellbeing. **Rolling ~90-day snapshot** (no baseline — see Surprises) | 2026-04* | context |
| **Chicago** | Business Licenses | `r5kz-chrr` | `license_start_date` | **+1** | New/renewed licenses up-is-favorable (mirrors backbone permits). Filter future-dated noise first | 2026-04 | development_economy |
| **NYC** | DOHMH Restaurant Inspections | `43nn-pn8j` | `inspection_date` | **0** | Inspection *volume* is workload, not outcome (signal = `critical_flag`/score). Filter `1900-01-01` placeholders first | 2026-04 | context |
| **NYC** | Motor Vehicle Collisions | `h9gi-nx95` | `crash_date` | **-1** | NYC Vision Zero crash file; fewer collisions is favorable; dense ~6-6.7k/mo | 2026-04 | public_safety |
| **SF** | Registered Business Locations | `g8m3-pdis` | `dba_start_date` | **+1** | New business registrations up-is-favorable; clean ~0.75-1.5k/mo flow; `data_as_of` 2026-05-30 | 2026-04 | development_economy |
| **Seattle** | Fire 911 Calls | `kzjm-xkqj` | `datetime` | **0** | Fire/medic call *volume* is demand/workload, not a safety outcome (no clean good/bad direction); very dense ~8-9k/mo | 2026-04 | context |

`*` Chicago Towed Vehicles is a rolling inventory, not a historical series — render as a recent-volume tile, not a scored trend (see Surprises).

**Per-city live count:** Chicago 4 · NYC 2 · SF 1 · Seattle 1 · LA 0 · Miami n/a (out of scope). **Total: 8 live supplementary feeds.**

## SURPRISES / BLOCKERS

### Blockers — LA has no clean live supplementary feed (0 registered)
Three candidates probed, all unusable:
- **`d5tf-ez2w` (Traffic Collision Data 2010+) — FROZEN.** Confirmed `max(date_occ)` = **2025-03-08**; last full month 2025-02. LAPD retired/stopped refreshing this dataset in early 2025 (same pattern as the MyLA311 series rotation noted in the resolved registry). Over a year stale → unusable for a 12-mo baseline.
- **`6rrh-rzua` (Listing of Active Businesses) — STOCK SNAPSHOT, not a flow.** Live (625,585 rows) but keyed on each business's *original* `location_start_date`, so the monthly series is heavily back-loaded across decades, carries a large NULL bucket (~3.6k) and future-dated dirty rows (`max(location_start_date)` = **2026-12-31**). It's a roster of currently-active businesses, not a monthly count of *new* registrations → no usable momentum series.
- **`8ced-xbvn` (Building Inspections Results) and `9cmf-hjdi` (CorStat - Building Inspections) — DEAD endpoints.** Both show a fresh `updatedAt` (2026-05-30) in the LA Socrata catalog but **404 `dataset.missing`** on the actual `/resource/{id}.json` data endpoint. The catalog's `updatedAt` tracks metadata touches, not live data — do not trust it without a data-endpoint probe.

Per the directive ("a couple solid live feeds per city beats many shaky ones"), LA is left with **zero** supplementary feeds rather than registering the back-loaded Active-Businesses snapshot.

### Surprises — gotchas baked into the resolved feeds
- **Chicago Towed Vehicles `ygr5-vcbg` is a rolling ~90-day inventory, not a time series.** `min(tow_date)` 2026-03-03 → `max` 2026-05-31, total ~4.7k rows. Registered with `date_col_status: "rolling_snapshot"`; the scorer should emit `status: "insufficient_history"` and show a recent-volume tile only — never z-score it.
- **NYC Restaurant Inspections `43nn-pn8j` text-placeholder dates.** ~3,434 rows carry `inspection_date = '1900-01-01'` (new/never-inspected establishments). Filter `inspection_date > '1901-01-01'` or `date_trunc_ym` produces a huge bogus 1900 bucket.
- **Chicago Business Licenses `r5kz-chrr` future-dated + NULL noise on `license_start_date`.** Raw monthly agg returns a ~2.7k NULL bucket plus forward-dated rows (2026-08/09/11). Filter `license_start_date IS NOT NULL AND license_start_date < <next-month>` before bucketing. **Cleaner alternative: `date_issued`** (no future noise, `max` 2026-05-29) — recorded as `date_col_alt`.
- **SF restaurant-inspection feeds could not be resolved (text/dead ids).** The classic SF LIVES food-inspection ids 404 on the data endpoint: `sipz-fdjf` → `dataset.missing`, `pyih-qa8q` → `dataset.missing`, and the federated-catalog hit `7ktr-3bhb` also 404s. The only live SF "restaurant inspection" dataset, `4dx7-axux` ("Open Restaurants Inspections"), is sidewalk-dining *permit* compliance, not food safety — too niche, skipped. SF therefore ships its business-registrations feed only.
- **LA catalog `updatedAt` is misleading.** Several LA datasets report a same-day `updatedAt` yet 404 on `/resource/{id}.json` (see Blockers). Always confirm with a live data-endpoint probe, not catalog metadata.
- **Federated-catalog cross-jurisdiction noise.** Socrata's catalog API (`/api/catalog/v1`) federates across portals: a `q=collision` search on `data.lacity.org` surfaced NYC's `h9gi-nx95` and out-of-state datasets (Calgary, Boulder). Match on the data-endpoint host, not catalog hits.
- **Throttling.** No keyless 429s hit during this probe pass (single low-volume requests), but the larger tables (Chicago crashes ~900k+, NYC collisions, NYC inspections) need `SOCRATA_APP_TOKEN` for production polling — same rule as the backbone feeds.
- **No text-date `date_trunc_ym` failures among the *registered* feeds.** All 8 registered date_cols are real Socrata timestamps (`floating_timestamp`). (Seattle's business-license `license_start_date` *is* a text `YYYYMMDD` field, but that feed was dropped as a weak back-loaded snapshot, not registered.)
