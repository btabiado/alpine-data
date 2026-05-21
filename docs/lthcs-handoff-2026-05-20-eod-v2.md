# Handoff — 2026-05-20 EOD (v2, post-launchd setup)

Paste this whole document into the next Claude Code session.

---

## Project at a glance

- **LTHCS** (Long-Term Hold Confidence Score) — daily scoring across 169
  US-equity tickers + 10-coin crypto universe.
- **Repo on GitHub**: `btabiado/btc-eth-etf-dashboard` (deployed to GitHub
  Pages on every push to `main`).
- **Two local clones** on disk — pick one as canonical, treat the other
  as scratch:

  | Path | HEAD | Notes |
  | --- | --- | --- |
  | `~/btc-eth-etf-dashboard/` | current `main` | **TCC-clean** (outside `~/Documents/`). The launchd-managed Flask server reads from here. Recommended canonical. |
  | `~/Documents/btc-eth-etf-dashboard/` | current `main` | Where today's 32 commits originated. Has an active scaletest run going (see "open work"). |

---

## State at handoff

- Local + origin in sync at the same `main` HEAD as of last push tonight.
- 1931 tests passing on local (Py 3.9); CI tests green after audit fix.
- All workflows green (`pages`, `codeql`, `tests`, `lthcs-news-hourly`,
  `lthcs-crypto-daily`, `lthcs-backtest-daily`).
- Local Flask server (`server.py`) running under launchd with gunicorn
  (4 workers, `127.0.0.1:8765`).

---

## What landed today (32 commits, categorized)

### Audit + CI fixes (1 commit)

- `221226c` — gated `requests==2.33.0` to Py>=3.10 with `requests==2.32.5`
  fallback (matches the existing `mcp[cli]` pattern); hardened
  `test_yahoo.py` to mock instead of hitting live yfinance.
  Result: `lthcs-news-hourly` cron now passes; tests go green on
  Py3.10/3.11/3.12 even when the calendar moves.

### LTHCS narrative card iteration on /lthcs/ (10 commits)

- Started: dropped "LTHCS " prefix in verdict (e.g. `LTHCS NEUTRAL` →
  `NEUTRAL`), added per-component strength bars.
- 3-up UX swarm (revamp-A verdict-first, revamp-B narrative, revamp-C
  cockpit) → user picked **B** → promoted to `/lthcs/`.
- Tightened B (3-col component grid, Steps 3+4 as `<details>`
  accordions, smaller padding/fonts).
- **Reverted /lthcs/ to compact composite-index card** when user
  reconsidered ("I do not need the LTHCS full narrative page"). Kept
  `cleanLabel()` + strength bars (the original 2-item ask).
- Added "Jump to stocks ↓" CTA at the top of `/lthcs/`.
- Tightened the components table — value column now renders as
  positioned bars (0–100 scale for pillar avgs, −100/+100 centered
  scale for band lean), strength bars bumped to 16px × 200px.

### V1 main dashboard (`/`) (5 commits)

- Promoted Revamp B narrative card to V1's Overview + Stocks tab via
  `renderLthcsNarrativePanel`. Old `renderLthcsCompositePanel` kept as
  rollback safety.
- Crypto Overview top row: News + Insights LEFT (cap 3 each), new
  AI-exposed stocks card RIGHT.
- AI News tab mirrors the same layout (insights+news LEFT cap 3,
  AI-exposed stocks RIGHT). Sentiment back to full-width below.
- Strong Buys card on Crypto Overview: now `STRONG BUY` **OR** `BUY`
  (sorted by score), heading renamed "Strong Buy / Buy Signals".
- LTHCS Insights row moved BELOW the Composite Index card on V1's
  LTHCS tab; top-right outlined CTA chip "Open full LTHCS →" added
  inside the card head (saves ~40px vs a standalone button row).

### QA & Audit UX mockups (1 commit)

- `d293b26` — desktop + mobile mockups at
  `lthcs_tab/mockups/qa-audit-{desktop,mobile}/`. Six surfaces
  consolidated: pipeline runs, drift sparkline, per-pillar quality
  matrix, security ledger, universe state, top-5 score movers. Real
  data baked in. Not promoted to a real `/lthcs/qa/` route yet.

### Routine bot snapshots (rest of commits)

- `lthcs-bot` daily/news commits — data refreshes, all `[skip ci]`.

---

## Live now (UTC crons)

| Time | Workflow |
| --- | --- |
| `:53` past every hour | `lthcs-news-hourly` |
| 22:00 daily | `lthcs-crypto-daily` |
| 23:00 daily | `lthcs-daily` (equity pipeline + LLM shadows, `--skip-thesis`) |
| 23:30 daily | `lthcs-backtest-daily` |
| 04:00 daily | `lthcs-trends-daily` |
| 04:00 Mon | `lthcs-trends-weekly` |
| 05:00 Mon | `validate` |
| Mon | `dependabot` |
| 03:00 Sun | `trufflehog` |
| 04:00 Sun | `codeql` |
| 1st of month 06:00 | `lthcs-backtest-monthly` |
| 1st of month 07:00 | `lthcs-tune-weights-monthly` |
| 1st of month 08:00 | `lthcs-β-verdict-monthly` |
| 1st of month 09:00 | `lthcs-quality-audit-monthly` |

---

## Live routes (16, all 200)

`/lthcs/` `/heatmap/` `/table/` `/crypto/` `/backtest/` `/backtest/ab.html`
`/health/` `/health/quality.html` `/health/pipeline.html` `/position/`
`/public/` `/help/` `/diff/` `/history/` `/leaderboards/`

Plus 5 mockup routes (revamp-A / revamp-B / revamp-C / qa-audit-desktop
/ qa-audit-mobile) under `/lthcs/mockups/`.

---

## Local Flask server (NEW today)

- **Running under launchd** — auto-starts at login, restarts on crash
  (30s throttle).
- **Files**:

  | Path | Purpose | Mode |
  | --- | --- | --- |
  | `~/.config/lthcs/dash.env` | `DASH_USER` + random 28-char `DASH_PASS` | `600` |
  | `~/.config/lthcs/launch-server.sh` | sources env + exec's gunicorn (4 workers, 120s timeout) | `700` |
  | `~/Library/LaunchAgents/com.btabiado.lthcs-server.plist` | launchd config | `644` |
  | `~/.config/lthcs/server.log` / `.err` | gunicorn access + error logs | `644` |

- **Day-2 commands**:

  ```bash
  cat ~/.config/lthcs/dash.env                                          # see password
  launchctl list | grep lthcs-server                                    # PID + last exit
  launchctl kickstart -k gui/$(id -u)/com.btabiado.lthcs-server         # restart (after editing env)
  launchctl unload ~/Library/LaunchAgents/com.btabiado.lthcs-server.plist  # stop
  tail -f ~/.config/lthcs/server.log                                    # tail logs
  ```

- **Reads from**: `~/btc-eth-etf-dashboard/` (NOT `~/Documents/...`).
  This was the fix for the launchd TCC block — files inside `~/Documents/`
  are denied to launchd-spawned processes by default.

---

## Memory rules still apply

- **Auto-push for LTHCS-only commits** — `lthcs/`, `tests/lthcs/`,
  `data/lthcs/`, `lthcs_*/` UI dirs, `docs/lthcs-*.md`,
  `scripts/lthcs_*.py`, `README_LTHCS.md`. Push directly to
  `origin/main` without asking.
- **Ask first** for: `app.py`, `v2/app.py`, `lthcs/score.py`,
  `.github/workflows/`. Today's many `app.py` edits had explicit user
  authorization in each case.
- **Branch protection on `main`** blocks force-push + deletion but
  allows direct push (admin bypass available for emergencies).
- **V1 production protection** — narrative panel was the biggest V1
  touch today; `renderLthcsCompositePanel` left in place as a one-flip
  rollback (just edit the 2 call sites in `app.py`).

---

## Open work — pick up here

### 🟡 Universe expansion (Wave A, +50 tickers) — IN FLIGHT

- **Scaletest #2 running right now** in `~/Documents/btc-eth-etf-dashboard/`
  with `--skip-thesis` (the fix — mirrors production cron, sidesteps
  the Finnhub /news-sentiment 403 retry storm that caused the first
  scaletest to NO-GO).
- Started ~22:50 ET. Should finish in 5–15 min once Finnhub retries
  aren't dominating.
- **To check**:
  ```bash
  ps aux | grep lthcs_universe_scaletest | grep -v grep    # still running?
  tail -10 /tmp/scaletest2.log                              # progress
  cat ~/Documents/btc-eth-etf-dashboard/data/lthcs/scaletest/2026-05-20_scaletest_report.md
  ```
- **If verdict = GO**: run `scripts/lthcs_universe_expand.py` to ship
  Wave A (~+50 tickers, bringing universe to ~219). Commit to
  `data/lthcs/universe.json` falls under auto-push.
- **If verdict = NO-GO**: read the report's `reasons` block. If it's
  STILL wall-clock related, options are (a) ship anyway since prod
  uses `--skip-thesis` (option 3 from yesterday), (b) park to
  2026-05-27 as originally planned.

### 🟡 Finnhub `/news-sentiment` 403 — UNRESOLVED

- Was flagged this morning; still returning HTTP 403 "You don't have
  access to this resource" for free-tier accounts.
- Production daily cron is unaffected (`--skip-thesis` is the default
  in CI) — Thesis pillar falls back to neutral 50.
- Needs decision: check Finnhub plan / rotate to a different provider /
  formally accept Thesis-as-neutral.

### 🟢 QA & Audit page promotion — READY

- Mockups exist at `/lthcs/mockups/qa-audit-{desktop,mobile}/`. User
  has seen them, hasn't picked one to promote yet. Mirror the
  `/lthcs/health/` route pattern when promoting (live HTML route under
  `lthcs_qa/` or similar).

### 🟢 Stash to drop in canonical clone

- `~/btc-eth-etf-dashboard/` has `stash@{0}: stale-edits-before-2026-05-20-sync`
  from earlier sync. 3 files: `lthcs_tab/index.html`,
  `lthcs_tab/lthcs-index.js`, `lthcs_tab/lthcs.css`. Likely pre-empted
  by today's main-branch work. Recoverable with `git stash show -p`.
  Drop with `git stash drop` once you confirm they're not needed.

### 🟢 Untracked scaletest artifacts in `~/Documents/...`

- `data/lthcs/candidate_run/`, `data/lthcs/scaletest/`,
  `data/lthcs/universe_candidate_full.json` — outputs of the scaletest
  runs. Decide whether to commit (`data/lthcs/scaletest/` is the
  audit-trail dir per the universe-expansion plan) or clean.

---

## Calendar-gated next moves

| Date | What |
| --- | --- |
| **2026-05-26** | Phase 3 re-audit auto-fires; check `/lthcs/health/quality.html` |
| **2026-05-27** | Originally-planned Wave A date (universe expansion). May already be done by handoff time. |
| **~2026-06-17** | β IC verdict auto-fires; check `lthcs-β-verdict-monthly` job summary. |
| **2026-07-XX** | V2 SHIP gates unlock (need 20 OOS observations at h=21d). |
| **2026-08-15** | Quarterly security review (your calendar). |

---

## Reference docs to read first

- `docs/lthcs-parking-lot.md` — deferred items, single source of truth
- `docs/lthcs-threat-model-2026-05-20.md` — security posture
- `docs/lthcs-universe-expansion-plan-2026-05-27.md` — 3-wave rollout
- `docs/lthcs-revamp-{A,B,C}.md` — yesterday's 3-up UX swarm proposals
- `docs/lthcs-qa-audit-{desktop,mobile}.md` — yesterday's QA & Audit
  mockup rationales

---

## Recommended first moves in new session

```bash
# 1. Land in the TCC-free clone
cd ~/btc-eth-etf-dashboard
git log --oneline -5
git status --short

# 2. Confirm Flask server is still serving
launchctl list | grep lthcs-server
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://127.0.0.1:8765/   # → 401

# 3. Check scaletest #2 result
ps aux | grep lthcs_universe_scaletest | grep -v grep                  # → empty = done
cat ~/Documents/btc-eth-etf-dashboard/data/lthcs/scaletest/2026-05-20_scaletest_report.md

# 4. If GO and you want to ship Wave A:
cd ~/Documents/btc-eth-etf-dashboard
.venv/bin/python scripts/lthcs_universe_expand.py --wave A
# (review the universe.json diff, then commit + push)
```

If scaletest hadn't finished by handoff time, just wait — the Python
process is orphaned but will run to completion on its own; the report
will be written when it finishes.

That's everything. Paste into a fresh session and you're caught up in
~2 min. 👋
