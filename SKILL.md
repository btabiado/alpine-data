---
name: alpine-data
description: Conventions for working in the alpine-data repo, which holds a static GitHub Pages dashboard, the LTHCS stock-scoring pipeline, and the scheduled workflows that fetch and commit their data. Use for any change to app.py, the fetchers, lthcs/, the lthcs_* pages, data/, tests/ or .github/.
---

# alpine-data: conventions for agents

## What this repo is

- **A static site** on GitHub Pages: <https://btabiado.github.io/alpine-data/>.
  `.github/workflows/pages.yml` builds it on every push to `main`, hourly, and
  after the daily audit. Nothing runs server-side for readers.
- **Scheduled data workflows** (`.github/workflows/*-daily.yml`, `*-hourly.yml`,
  and so on) fetch from free public sources and commit the results to `main`.
  The site build reads those committed files.
- **`server.py`** is an optional local Flask server for the same dashboard. It
  is not deployed.
- **`main` is protected.** Changes go through pull requests, and `tests.yml` must
  pass.

## Layout

| Path | What it is |
|---|---|
| `app.py` | Builds `dashboard.html`, the main dashboard: 22 tab buttons in Crypto / Markets / Macro / Explore menus, plus LTHCS, AI News and the Summit launcher. Inline HTML/JS template; data is inlined or lazy-loaded from `data-*.json` sidecars. |
| `fetch_*.py`, `signals.py`, `insights.py`, `money_flow.py`, `wiki_enrich.py` | Root fetchers and scorers that `app.py` and the workflows call. |
| `city/`, `fetch_city.py` | City tab (City Pulse) sources and scoring. |
| `lthcs/` | LTHCS package: `sources/` (API clients behind a file cache), `pillars/` (five pillar scorers), `score.py`, `bands.py`, `persist.py`, `schemas/`. |
| `lthcs_daily.py` | The daily LTHCS pipeline (8 stages). |
| `lthcs_tab/` | LTHCS card view (`/lthcs/`) and shared ES modules such as `lthcs-bands.js`, `lthcs-freshness.js` and `lthcs-files.js`. |
| `lthcs_<page>/` | Other LTHCS pages: help, table, health, history, leaderboards, diff, position, backtest, crypto, public. |
| `lthcs_mcp/` | Read-only MCP server over `data/lthcs/`. |
| `data/` | Committed data. `data/lthcs/` holds the universe, weights, dated snapshots and per-ticker history. |
| `data-*.json` (root) | Committed tab sidecars. Others are build outputs and are gitignored; see `.gitignore`. |
| `scripts/` | Workflow entry points and maintenance tools. |
| `tools/` | Validators, such as `tools/validate_dashboard.py` for the built dashboard. |
| `tests/` | pytest suite. LTHCS tests are in `tests/lthcs/`. |
| `docs/` | Runbooks and specs. Dated files (`*-2026-05-*.md`) are point-in-time records, not current state. |
| `v2/`, `lthcs_tab_v2/` | Being retired. Don't build on them. |

Facts that are easy to get wrong:

- **LTHCS universe:** `data/lthcs/universe.json`. As of 2026-10 that is the S&P 500
  and Dow 30, plus NASDAQ-100 names and Index Exiles: 519 tickers, 515 active.
  Read the count from the file; don't hard-code it in pages or docs.
- **Score bands:** `data/lthcs/weights.json` → `score_bands`, read through
  `lthcs/bands.py` (Python) and `lthcs_tab/lthcs-bands.js` (pages). The fallback
  copies must equal `weights.json`, and `tests/lthcs/test_calibrate_bands.py`
  enforces that. Recalibrate with `scripts/lthcs_calibrate_bands.py`.
- **`PHASE_1_BUILD_SPEC.md`** is the original May 2026 plan. Code comments cite
  its sections, but its universe, bands and layout are out of date.

## Data honesty (do not "simplify" these away)

These rules are enforced in `app.py` (`freshness()`), `lthcs_tab/lthcs-freshness.js`
and the health scripts.

1. **A date describes the data.** A stamp reports when the data was observed,
   never when it was built or fetched. Build time appears only where it is
   labelled "built".
2. **A composite is as old as its oldest input.** Use the minimum, never the
   newest date or an average.
3. **Gaps are counted and shown.** Dropped pillars, data-quality flags and
   carried-forward (`stale: true`) entries are shown with their counts.
4. **No date means "as of —".** Never fall back to today's date.
5. **Missing is not zero.** Don't write 0, 50 or a copied value for a day or
   field that was never observed. Carry-forward keeps the original `as_of`, is
   flagged and is bounded. Don't create synthetic history rows.
6. **Committed snapshots are append-only.** To restate history, bump
   `model_version` and record a `methodology_breaks` entry instead of editing
   old files.
7. **Respect sources.** Don't scrape a source that forbids it or blocks
   automation (see the NUFORC note in `fetch_mufon.py`). Keys live in GitHub
   secrets or `.env`, never in page JS. An unset key turns a feature off with a
   stated reason.

## How data gets committed

Workflows never `git push`. They stage files and call
`.github/scripts/api-commit.sh "<area>: <what> <date> [skip ci]"`. The script
creates the commit through the GitHub Git Data API, so GitHub signs it
("Verified"). It retries against a moving `main` and does nothing when nothing
is staged. Keep the message prefix (`lthcs:`, `city:`, `aviation:` and so on)
and `[skip ci]`.

## Tests

- **Install:** `pip install -r requirements.txt -r requirements-dev.txt`.
- **Run:** `python -m pytest tests/ -q`, about 2 minutes. The suite must leave
  `git status` clean:
  - `tests/conftest.py` blocks real network access. A test that tries to
    connect fails, even if the code under test swallowed the error.
  - Code that writes files is pointed at `tmp_path`.
  - A test that truly needs the internet is marked `@pytest.mark.network`.
    Those run locally and are skipped in CI (`CI` set).
- **CI-faithful run** (what `tests.yml` does). The two stub CSVs and the built
  `dashboard.html` are throwaway; restore the CSVs afterwards.

  ```bash
  printf 'date,Total\n2024-01-11,100\n' > data/btc_flows.csv
  printf 'date,Total\n2024-07-23,50\n' > data/eth_flows.csv
  python app.py --no-open && python tools/validate_dashboard.py dashboard.html
  REQUIRE_DASHBOARD=1 python -m pytest tests/ -q
  git checkout -- data/btc_flows.csv data/eth_flows.csv data-travel.json
  ```

  Without the build step, the tests that read `dashboard.html` skip locally.
  `app.py --no-open` also refreshes `data-travel.json` from travel.state.gov,
  so restore that file too.

## Commits and pull requests

- Subject line: `<area>: <what changed, in plain words>`. Examples:
  `lthcs: clamp Institutional Confidence to [0, 100]`, `tests: …`,
  `docs(lthcs): …`, `pages: …`.
- The body explains why and what was checked.
- Data commits from workflows end in `[skip ci]`. Human and agent commits don't.
- Agent commits carry the attribution trailers the session asks for.
- One topic per pull request. Say what you verified and what you could not.
