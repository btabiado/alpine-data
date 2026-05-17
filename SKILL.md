---
name: lthcs-phase1
description: Use when working on the LTHCS (Long-Term Hold Confidence Score) Phase 1 build for the btc-eth-etf-dashboard. Covers daily score calculation in Python, JSON snapshot persistence, and the client-side LTHCS tab integration. Trigger whenever the user references LTHCS, the conviction score, the daily pipeline, the new tab on the dashboard, or any file under data/lthcs/ or scripts named lthcs_*.
---

# LTHCS Phase 1 — Project conventions for Claude Code

## What this project is

A new LTHCS (Long-Term Hold Confidence Score) tab for an existing static GitHub Pages dashboard at `btabiado.github.io/btc-eth-etf-dashboard`. Two parts:

1. **Python daily fetcher/calculator** that runs on the user's laptop, pulls free-tier financial data, computes V1 scores per the LTHCS framework, and writes JSON snapshots to `data/lthcs/`.
2. **Client-side LTHCS tab** (HTML + vanilla JS + CSS) added to the existing dashboard. Reads the JSON snapshots as static assets, renders score cards, search, filters, and detail modals.

Both parts are **standalone** — they do not touch the existing crypto/ETF/whale/POC tabs.

## Repo layout (assume working in repo root)

```
btc-eth-etf-dashboard/
├── index.html                          ← existing dashboard; ADD new tab here
├── app.py                              ← existing fetcher; DO NOT modify
├── lthcs_daily.py                      ← NEW: daily calculator
├── lthcs/                              ← NEW: Python package for LTHCS logic
│   ├── __init__.py
│   ├── universe.py                     ← loads the 75-ticker universe
│   ├── sources/                        ← API clients
│   │   ├── alpha_vantage.py
│   │   ├── fred.py
│   │   ├── eia.py
│   │   └── sec_edgar.py
│   ├── pillars/                        ← per-pillar scoring
│   │   ├── adoption.py
│   │   ├── institutional.py
│   │   ├── financial.py
│   │   ├── thesis.py
│   │   └── des.py
│   ├── score.py                        ← combines pillars into LTHCS score
│   ├── normalize.py                    ← percentile/z-score helpers
│   └── persist.py                      ← writes snapshots, narratives, history
├── js/
│   └── lthcs/                          ← NEW: tab JS modules
│       ├── lthcs-tab.js                ← main controller
│       ├── lthcs-cards.js              ← score card rendering
│       ├── lthcs-detail.js             ← detail modal
│       ├── lthcs-search.js             ← ticker search
│       └── lthcs-filters.js            ← exchange / band / drift filters
├── css/
│   └── lthcs.css                       ← NEW: scoped styles for the tab
├── data/
│   └── lthcs/                          ← NEW: all LTHCS data
│       ├── universe.json
│       ├── weights.json
│       ├── snapshots/YYYY-MM-DD.json
│       ├── variable_detail/YYYY-MM-DD.json
│       ├── narratives/YYYY-MM-DD.json
│       └── history/by_ticker/<TICKER>.json
├── .cache/                             ← NEW: gitignored raw API responses
│   └── lthcs/<source>/<key>.json
└── .gitignore                          ← UPDATE: add `.cache/`
```

## Conventions

- **Python 3.11+.** Use `requests`, `python-dotenv`, `pydantic` for schemas. No pandas unless absolutely needed (keep deps light; this runs on Bryan's laptop).
- **Vanilla JS.** No React, no build step, no bundler. Match the existing dashboard's pattern (you'll see existing tabs use plain JS modules loaded via `<script type="module">`).
- **JSON schemas live in `lthcs/schemas/`.** Every file under `data/lthcs/` must validate against a schema. Use pydantic models on the Python side; mirror them in `js/lthcs/schemas.js` for client-side validation.
- **No score is ever overwritten.** Once a snapshot is written for a date, it's append-only. To restate, increment `model_version` and write under a new date with a note.
- **Cache aggressively.** Free-tier APIs rate-limit hard (Alpha Vantage = 25 req/day). Every fetch goes through `lthcs/sources/_cache.py` which checks `.cache/lthcs/<source>/<key>.json` first.
- **Secrets via `.env`.** Never commit keys. The user creates `.env` from `.env.example`. Load with `python-dotenv` in Python and *never* read in client JS.
- **Idempotent runs.** `python lthcs_daily.py` must be safe to run multiple times the same day — second run is a no-op if today's snapshot exists, unless `--force` is passed.
- **Clear console output.** Print one line per stage with ✓ on success and ✗ on failure. Bryan will be watching the run; verbosity matters more than log files at V1.

## When implementing

1. **Read `PHASE_1_BUILD_SPEC.md` first.** That's the master plan.
2. **Build in milestone order.** Each weekly milestone in the spec produces a working, testable artifact. Don't skip ahead.
3. **Validate after every stage.** Run `python -m lthcs.validate` after writing any new snapshot. It checks schemas, score ranges (0-100), and required-field completeness.
4. **Test with 3 tickers first.** Before running on the full 75, test with AAPL, LCID, INTC. They exercise compounder, pre-profit, and recovery scoring paths respectively.
5. **Match the existing dashboard's visual style.** Look at `css/` and `index.html` for color tokens, card padding, font sizes. Don't introduce a new design system.

## Hard rules

- **Never modify `app.py` or existing tabs.** LTHCS is additive only.
- **Never commit anything under `.cache/`.** Add to `.gitignore` before first commit.
- **Never put API keys in client JS.** All API calls are server-side in `lthcs_daily.py`.
- **Never call free-tier APIs in a loop without rate-limiting.** Use `lthcs/sources/_ratelimit.py`.
- **Never write a score outside [0, 100].** The score combiner caps; if a cap fires more than 5% of the time, log a warning — something is wrong upstream.
- **Always update `data/lthcs/history/by_ticker/<TICKER>.json` at the end of a daily run** so the client-side chart has fresh data.

## Validation commands

```bash
# After any file change in lthcs/
python -m lthcs.validate

# After any snapshot write
python -m lthcs.validate --date 2026-05-16

# Smoke test the full pipeline against 3 tickers
python lthcs_daily.py --tickers AAPL,LCID,INTC --dry-run

# Run for real
python lthcs_daily.py
```

## Out of scope for Phase 1

- Real-time scoring (Phase 2)
- API exposure (Phase 2)
- Backtesting (Phase 3)
- V2 / V3 model formulas (Phase 2/3)
- Crypto assets (covered by other tabs; Phase 2 for LTHCS crypto pillar)
- AI narrative generation via LLM (Phase 1 uses templated narratives; LLM in Phase 2)
