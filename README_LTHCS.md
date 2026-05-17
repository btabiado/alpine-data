# LTHCS Phase 1 / V1 — README

**Long-Term Hold Confidence Score** — a sibling page on the `btc-eth-etf-dashboard` GitHub Pages site, with a daily Python pipeline that computes scores for 74 active US-listed tickers and persists them as JSON files in the repo.

🌐 **Live URL:** https://btabiado.github.io/btc-eth-etf-dashboard/lthcs/

This README is for Bryan to set the project up the first time and run it daily after that. The full build specification for Claude Code is in [`PHASE_1_BUILD_SPEC.md`](PHASE_1_BUILD_SPEC.md). The project conventions Claude Code reads on every session are in [`SKILL.md`](SKILL.md).

---

## V1 status (2026-05-16)

All 10 weeks of the build plan shipped. The framework runs end-to-end:

| Week | Module | Status |
|---|---|---|
| 1 | Schemas + validate gate | ✅ |
| 2 | 5 source clients (Yahoo, SEC EDGAR, FRED, EIA, Alpha Vantage) | ✅ |
| 3 | normalize + Adoption pillar | ✅ |
| 4 | Institutional + Financial pillars | ✅ |
| 5 | Thesis + DES pillars | ✅ |
| 6 | Score combiner + templated narratives | ✅ |
| 7 | Daily pipeline (`lthcs_daily.py`) + JSON persistence | ✅ |
| 8 | Tab UI (cards grid + search + filters) | ✅ |
| 9 | Detail modal + 90-day SVG sparkline + variable detail | ✅ |
| 10 | About modal + README updates + polish | ✅ |

**Universe:** 75 entries / 74 active. WBA marked inactive (Walgreens taken private late 2025).

**V1 limitations honestly disclosed in the About modal:**
- Alpha Vantage NEWS_SENTIMENT is AND-semantics on multi-ticker, so the daily pipeline gets news for one sample ticker; Thesis falls back to neutral 50 for the rest. Phase 2 upgrade to AV Premium or alternate news source.
- Google Trends acceleration (40% of Adoption) is not driven for 74 tickers — Google rate-limits aggressively.
- 13F holdings change (30% of Institutional) is a Phase 2 stub.
- Banks score low on Financial Evolution (don't report GrossProfit / OCF the standard XBRL way). Sector-aware financial scoring is Phase 2.

---

## First-time setup (one-time, ~20 minutes)

### 1. Clone the repo locally

```bash
cd ~/Documents          # or wherever you keep code
git clone https://github.com/btabiado/btc-eth-etf-dashboard.git
cd btc-eth-etf-dashboard
```

### 2. Get free API keys (10 minutes total)

| Service | Where | Time |
|---|---|---|
| Alpha Vantage | https://www.alphavantage.co/support/#api-key | 1 min |
| FRED | https://fred.stlouisfed.org/docs/api/api_key.html | 5 min (email verify) |
| EIA | https://www.eia.gov/opendata/register.php | 2 min |
| SEC EDGAR | No key needed — just set a User-Agent string |  |

### 3. Create `.env`

```bash
cp .env.example .env
# Then edit .env and paste your keys
```

`.env` is gitignored. Never commit it.

### 4. Install Python dependencies

```bash
python -m venv .venv
source .venv/bin/activate                  # macOS / Linux
pip install -r requirements.txt
```

Requirements are intentionally minimal: `requests`, `python-dotenv`, `pydantic`, `yfinance`, `pytrends`. No pandas, no numpy unless something downstream forces it.

### 5. Verify the install

```bash
python -m lthcs.validate
```

Should print: `✓ universe.json valid (75 tickers)` and `✓ weights.json valid (9 profiles)`.

### 6. Run your first daily pipeline (smoke test with 3 tickers)

```bash
python lthcs_daily.py --tickers AAPL,LCID,INTC --dry-run
```

Should print each stage's `✓` line, end with three computed scores, and write nothing (dry run). Expected band placements:
- **AAPL** — High Confidence (80-89)
- **LCID** — Monitor or Weakening (Pre-Profit Growth weighting; weak Financial Evolution)
- **INTC** — Review (Recovery Stabilization weighting; multiple thesis-break flags)

If the smoke test looks right, run for real:

```bash
python lthcs_daily.py --tickers AAPL,LCID,INTC
```

This writes `data/lthcs/snapshots/<today>.json` (and friends) with three entries.

### 7. View it locally

```bash
python -m http.server 8000
# Open http://localhost:8000 in your browser
# Click the new "LTHCS" tab
```

You should see 3 score cards (AAPL, LCID, INTC). Search and filters work even on a small universe. Click any card to open the detail modal.

### 8. Push to production

```bash
git add data/lthcs/ index.html js/lthcs/ css/lthcs.css lthcs/ lthcs_daily.py requirements.txt .env.example .gitignore
git commit -m "lthcs: initial 3-ticker snapshot"
git push
```

Within ~1 minute, the new tab will be live at `https://btabiado.github.io/btc-eth-etf-dashboard/`.

---

## Daily workflow (after V1 is live)

```bash
cd ~/Documents/btc-eth-etf-dashboard
source .venv/bin/activate
python lthcs_daily.py                       # Full run, all 75 tickers, ~45-60 sec
git add data/lthcs/
git commit -m "lthcs: daily snapshot $(date +%Y-%m-%d)"
git push
```

That's it. Three lines once a day. No server, no cron, no database, no cloud bill.

If you skip a day, no harm done — the gap is visible in the history files and the dashboard shows the most recent snapshot regardless of date.

---

## What goes where on your laptop

```
~/Documents/btc-eth-etf-dashboard/        ← The repo (everything lives here)
├── .env                                   ← Your API keys (gitignored)
├── .cache/lthcs/                          ← Raw API responses (gitignored)
└── data/lthcs/                            ← The LTHCS data (COMMITTED to git)
    ├── universe.json                      ← 75 tickers
    ├── weights.json                       ← Pillar weights by maturity stage
    ├── snapshots/2026-05-16.json          ← Today's scores for all 75
    ├── variable_detail/2026-05-16.json    ← Every variable behind today's scores
    ├── narratives/2026-05-16.json         ← Today's narrative per ticker
    └── history/by_ticker/AAPL.json        ← 365-day rolling history per ticker
```

**Nothing lives outside this folder.** Snapshots are versioned in git, so the historical confidence graph (the moat in §5.1) is literally the git history of `data/lthcs/snapshots/`. Every score the system has ever produced is timestamped, signed by GitHub, and free to query.

**`.cache/` and `.env` never leave your laptop.** They're in `.gitignore` so an accidental `git add .` won't push them.

---

## How to add a ticker

Edit `data/lthcs/universe.json`, add an entry following the existing schema, save. The next `python lthcs_daily.py` run will include it. No code changes needed.

To remove a ticker, set `"active": false` rather than deleting the entry — that preserves history files and any backtest references.

---

## How to retire / restate a score

Don't overwrite. Append. If a score needs to be corrected:

1. Increment `model_version` in `lthcs/__init__.py` (e.g., `v1.0.0` → `v1.0.1`)
2. Add a note in `data/lthcs/restatements.md` explaining what changed and why
3. Run the daily pipeline with `--force` to overwrite the affected dates *or* let the new version run forward from today

The original score remains in git history for auditability — same principle as financial restatements.

---

## Troubleshooting

**Alpha Vantage 25-call/day limit hit.** Expected for full universe runs more than once a day. The cache should keep you under the limit on normal daily runs. If you've burned through quota, wait 24h or skip the news-sentiment pillar (`--skip-thesis`).

**Yahoo Finance returns nothing for ticker X.** The `yfinance` package occasionally breaks when Yahoo changes their HTML. Try `pip install -U yfinance`. If that doesn't work, use the Alpha Vantage fallback in `lthcs/sources/yahoo.py` (already wired).

**SEC EDGAR rate-limits you.** Make sure your User-Agent header in `.env` is set to a real email address — SEC requires it.

**Validation fails after a run.** Run `python -m lthcs.validate --date <today>` for detailed diagnostics. Common causes: a source returned empty (network blip), a ticker has no XBRL filings yet (newly IPO'd), or a percentile calculation got NaN (one ticker has all-zero history). All have specific error messages.

**The new tab doesn't show up on the live site.** GitHub Pages caches aggressively. Wait 2 minutes, hard-refresh (Cmd+Shift+R / Ctrl+Shift+R), check the deployment in the GitHub Actions tab of the repo.

---

## What V2 adds (not in scope here)

- Adaptive weights based on backtest performance per asset
- Real-time intraday scoring for institutional users
- MCP server + Anthropic Claude Connector listing (per §23 of the white paper)
- LLM-generated narratives (Claude / GPT-4-class, grounded in stored variables)
- Crypto pillar adapter (so LTHCS can score BTC, ETH, SOL, etc.)
- Premium data sources (Polygon, FMP paid, Glassnode)
- Full backtest engine (§24 methodology)

V2 begins after V1 has been live for ~60 days and has accumulated enough daily snapshots to be worth backtesting against.

---

## Where the framework lives

The methodology behind every number on the dashboard is in the LTHCS Intelligence White Paper v9.5 — section references throughout the codebase (e.g., `# Per §5.2 of white paper`) point back to the relevant section so you can always trace a line of code to its intellectual source.

The white paper is the spec. This code is the implementation. Drift between them is a bug.
