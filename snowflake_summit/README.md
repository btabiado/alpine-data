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

`dashboard.html` is fully self-contained — the data **and** Chart.js are inlined at build time, so it
works fully offline (no CDN or network needed). A **⬇ Download source spreadsheet** button in the header
serves the bundled `.xlsx`.

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
- **🤝 Best Bryan Recommend** — top career / networking-fit partners.
- **Charts** — Top 15 by Overall, Priority-Tier mix, Partners by Niche, a radar of the average
  score profile (Tier A vs all), and Top 15 by Valuation.
- **Sortable / filterable table** of all 197 partners with every score column.
- **Pop-out views** (each opens in its own window): **📰 Summit News** (partner announcements from
  `news.json`), **📊 Magic Quadrant** (all partners + per-niche drill-down), and **🗺 Floor Map**
  (interactive Basecamp floor map from `floorplan.json`).

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
`python3 build.py`. `vendors.json` is the source of truth. The bundled `.xlsx` is a static download
artifact served next to the page — `build.py` only links it for download; it does **not** regenerate
the workbook from the JSON.
