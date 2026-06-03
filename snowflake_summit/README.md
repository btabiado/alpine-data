# Snowflake Summit 2026 — Partner Scouting Dashboard

A self-contained dashboard built from **Bryan's `Snowflake_Summit_2026_Master_Partner_Scouting.xlsx`** —
197 partner vendors transcribed from the Summit partner-booth directory and scored across five
dimensions. It turns the workbook into KPIs, charts, and a ranked **Must-See** list so you can decide
which booths to hit.

## Quick start

```bash
cd snowflake_summit
python3 build.py          # reads vendors.json -> writes dashboard.html
open dashboard.html       # (macOS) or just double-click it
```

`dashboard.html` is fully self-contained (data inlined at build time). Charts load Chart.js from a CDN,
so viewing needs an internet connection. A **⬇ Download source spreadsheet** button in the header serves
the bundled `.xlsx`.

## What's inside

- **`vendors.json`** — the 197-partner Master Directory (the dashboard's data source).
- **`build.py`** — aggregates the data + renders `dashboard.html`.
- **`dashboard.html`** — the generated dashboard.
- **`Snowflake_Summit_2026_Master_Partner_Scouting.xlsx`** — the source workbook (Master Directory),
  linked for download from the page.

### Dashboard sections
- **6 KPIs** — total partners, Must-See (Tier A) count, Priority (A+B), avg Overall score, avg AI score, public-company count.
- **⭐ Must-See** — the 24 Tier-A partners, ranked by Overall Score (Sigma, Atlan, Hightouch, dbt, Fivetran, …).
- **💎 Hidden Gems** — Overall ≥ 7 but not Tier A.
- **🤝 Best Bryan-Fit** — top career/networking-fit partners.
- **Charts** — Top 15 by Overall, Priority-Tier mix, Partners by Category, and a radar of the average
  score profile (Tier A vs all).
- **Sortable / filterable table** of all 197 partners with every score column.

## The scoring (0–10, Bryan's directional ratings)

| dimension | meaning |
|-----------|---------|
| `snowflake_score` | Snowflake ecosystem relevance |
| `ai_score` | AI / ML relevance (the Summit's dominant theme) |
| `retail_score` | Retail / customer-analytics relevance |
| `ipo_score` | IPO / upside potential |
| `bryan_score` | Bryan career / networking fit |
| `overall_score` | blended overall priority |
| `tier` | Priority **A** (must-see) / **B** / **C** |

> **Caveat (from the workbook):** first-pass scouting database. Public market caps are researched;
> many private funding/valuation entries still need validation (Crunchbase/PitchBook/company disclosures).

## Updating the data

Edit `vendors.json` (or replace it with a fresh export using the same field names) and re-run
`python build.py`. To regenerate the downloadable spreadsheet from the JSON, the builder logic for that
lives alongside the parse step; the simplest path is to edit `vendors.json` as the source of truth.
