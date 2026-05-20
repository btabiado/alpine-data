# LTHCS Universe Expansion Plan — 2026-05-27

Target: grow the LTHCS production universe from **169 tickers** (DJIA 30
+ NASDAQ-100 + S&P 100, dedup'd) to **~500 tickers** (full S&P 500)
across three controlled waves. The whole expansion is **parked until
2026-05-27**, immediately after the Phase 3 re-audit closes on
2026-05-26. The infrastructure in this PR ships now; the universe
itself does not change.

## Why three waves

A 3x universe expansion changes:

- API call volume at SEC EDGAR, Finnhub, FRED, Yahoo
- File I/O fanout across `snapshots/`, `variable_detail/`,
  `narratives/`, `history/by_ticker/`
- Backfill cost (history must be reconstructed for each new ticker)
- Distribution shape of every cohort-aware scoring rule

Doing it in one swing risks tripping a rate limit at hour 1, getting an
unreadable failure mode, and rolling back to V1 with bad data in
production. Three waves let us validate the additional load incrementally.

## Wave plan

| Wave | Adds | Cumulative | Target date | Gate |
| --- | --- | --- | --- | --- |
| A | +50 (largest market cap from the seed) | ~220 | 2026-05-27 | scaletest GO + 3 clean daily runs |
| B | +100 (next-largest 100) | ~320 | 2026-06-03 | Wave A daily SLOs hold for ≥5 calendar days |
| C | +183 (remainder of seed) | ~503 | 2026-06-17 | Wave B daily SLOs hold + Finnhub plan review |

"Cumulative" totals are approximate — the production universe may shift
by ±5 if S&P rebalances between waves.

## Gating criteria between waves

A wave only proceeds when ALL of the following are green:

1. **Scaletest verdict = GO** in the previous wave's `data/lthcs/scaletest/`
   report. NO-GO blocks until the underlying issue is resolved.
2. **Daily pipeline wall-clock** stays under 25 minutes for 5 consecutive
   trading days (cron has a 30-minute budget).
3. **Rate-limit hits = 0** at every source (Finnhub, SEC EDGAR, FRED,
   EIA, Alpha Vantage, Yahoo) for 5 consecutive trading days.
4. **Pillar coverage** on Adoption and Financial stays above 80% across
   the expanded universe.
5. **Distribution audit clean** — run
   `scripts/lthcs_score_distribution_audit.py` and confirm no cohort has
   collapsed into a single bucket.
6. **No new alerts** open in `data/lthcs/quality_audit/` related to the
   pillar quality runner.

If any gate is red, halt waves and run the rollback procedure.

## Concrete operator checklist for 2026-05-27 (Wave A)

```
[ ] Confirm Phase 3 re-audit closed 2026-05-26 and signed off (green).
[ ] git pull origin main
[ ] Verify the universe-prep infrastructure is present:
    - scripts/lthcs_universe_expand.py
    - scripts/lthcs_universe_scaletest.py
    - data/lthcs/sp500_candidate_seed.json (333 candidate tickers)
[ ] Snapshot the current production universe — copy of file:
    cp data/lthcs/universe.json data/lthcs/universe.snapshot-pre-2026-05-27.json
[ ] Run a CSV expand for the Wave A candidates (top 50 by market cap from the seed):
    python scripts/lthcs_universe_expand.py \
        --input wave_a_candidates.csv \
        --output-dir data/lthcs/universe_candidate/wave_a/
[ ] Review data/lthcs/universe_candidate/wave_a/_summary.json
    Make sure passed_count >= 50; resolve failed_count individually.
[ ] Build the wave-A universe by appending validated candidate JSONs
    to a copy of universe.json:
    python -c "..."  # manual: append the 50 passed records into a fresh universe.json
[ ] Save as data/lthcs/universe.wave-a-candidate.json (DO NOT overwrite production yet).
[ ] Run scaletest against the candidate file:
    python scripts/lthcs_universe_scaletest.py \
        --n 220 \
        --out data/lthcs/scaletest/wave_a/
[ ] If verdict = GO -> promote:
    mv data/lthcs/universe.json data/lthcs/universe.pre-wave-a.json
    mv data/lthcs/universe.wave-a-candidate.json data/lthcs/universe.json
[ ] Commit + push:
    git add data/lthcs/universe.json
    git commit -m "lthcs/universe: Wave A expansion (+50 to ~220)"
    git push origin main
[ ] Watch the next daily cron run end-to-end. Bail if anything misses.
[ ] After 5 clean daily runs, repeat for Wave B.
```

## Rollback procedure

**One-liner**: restore the pre-wave snapshot file and clean up the candidate run dir.

```
cp data/lthcs/universe.pre-wave-a.json data/lthcs/universe.json && \
  rm -rf data/lthcs/candidate_run/ && \
  rm -rf data/lthcs/universe_candidate/wave_a/ && \
  git add data/lthcs/universe.json && \
  git commit -m "lthcs/universe: rollback wave A (universe restored to pre-2026-05-27)" && \
  git push origin main
```

The rollback never touches `snapshots/`, `variable_detail/`,
`narratives/`, or `history/` — those will simply omit the rolled-back
tickers from the next daily run.

## Why the universe.json is NOT touched in this PR

The whole point of this infrastructure PR is to make Wave A a five-minute
operation on 2026-05-27. Anything beyond:

- `scripts/lthcs_universe_expand.py` (new)
- `scripts/lthcs_universe_scaletest.py` (new)
- `data/lthcs/sp500_candidate_seed.json` (new)
- `data/lthcs/universe_candidate/` (new output dir)
- `lthcs_daily.py` (+ ~30 lines: `--candidate-universe` flag + redirect)
- `lthcs/sources/_api_counter.py` (new, opt-in via env var)
- `lthcs/sources/{finnhub,yahoo,sec_edgar}.py` (+ ~10 lines each: counter bumps)
- `tests/lthcs/test_universe_expand.py` (new)
- `docs/lthcs-universe-expansion-plan-2026-05-27.md` (this doc)

…stays unchanged. Production behavior is bit-for-bit identical until the
operator explicitly invokes the new scripts.
