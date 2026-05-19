# LTHCS Backtest Engine — Tier 5 #24 Design Spec

**Status**: Spec. No code. Targets `lthcs/backtest_engine.py` (new).
**Gates**: Tier 5 #25 (Adaptive Weights V2) per
`docs/lthcs-open-items-audit.md:427-428`.
**Effort revision**: Audit calls #24 "L"
(`docs/lthcs-open-items-audit.md:427`). After scoping below, **Phase 1
alone is M**; Phases 2–4 are additive M / M / L.

## 1. Current state

The existing validator (`scripts/lthcs_backtest.py`,
`lthcs/backtest.py`) computes:

- Per-pillar Spearman IC vs forward returns over the backfill window
  (`lthcs/backtest.py:604-694`, `attribute_returns`).
- Quintile sort spreads per pillar (`lthcs/backtest.py:541-597`).
- A band-portfolio P&L *proxy* — equal-weight long/short on rebalance
  dates, but using the *forward h-day return* as the per-rebalance
  return (`lthcs/backtest.py:422-518`,
  `data/lthcs/backtest/2026-05-18_validation/report.md:14-17`). This is
  the source of the inflated "+18.7 Sharpe" headline — every daily
  observation reuses ~95% of the next day's 21d window
  (`report.md:142-143`).
- Band-ordering hypothesis check
  (`report.md:62-93`).
- Walk-forward CV for adaptive weights, gated HOLD until ≥20 OOS
  observations exist (`lthcs/adaptive_weights.py:636`,
  `data/lthcs/adaptive_weights/2026-05-18_walk_forward_after_fixes.md:20`).

**What is missing**: a *non-overlapping* simulated trading P&L. We can
report "composite IC = +0.127 @ 21d" but cannot honestly answer "if you
had bought every Buy-band ticker at the next-day close and exited when
it left the band, what would the equity curve look like — and what is
the per-pillar marginal contribution to that equity curve?"

## 2. Goal

A long-only event-driven backtest engine that simulates a tradable
LTHCS strategy on the daily snapshots, producing:

- Daily equity curve (no horizon overlap, no double-counting).
- Drawdowns, annualized Sharpe / Sortino, hit rate, average holding
  period, turnover.
- Per-pillar P&L attribution.
- Cached, rerunnable, fast on incremental data.

End-state nightly metric: *trailing 90-day Sharpe of the
high_confidence band* (and per pillar).

## 3. Strategy definition (Phase 1)

- **Universe**: the 167 LTHCS-scored equity tickers
  (`lthcs/score.py` universe). Crypto deferred to Tier 5 #27.
- **Entry**: ticker first enters a Buy band (default
  `["elite", "high_confidence", "constructive"]`, mirroring
  `lthcs/backtest.py:50` `DEFAULT_LONG_BANDS`). Buy at the **next
  trading-day close**.
- **Exit**: ticker drops out of the Buy set. Sell at the **next
  trading-day close** after the snapshot that registered the drop.
- **Sizing**: equal-weight across all currently-held names, rebalanced
  daily to equal weight on the close.
- **Costs**: 5 bps each side (round-trip ~10 bps). Calibration deferred
  to §11.
- **Slippage**: 1-trading-day delay between snapshot date and trade
  execution. This is the look-ahead guard — it also matches how a
  user would act on the 23:00 UTC snapshot the next session.
- **Initial capital**: $1M notional (curves normalized to 1.0 anyway).
- **Cash**: any unallocated capital earns 0% (conservative — defer
  cash-yield modelling).

## 4. Output schema (under `data/lthcs/backtest/<run_id>/`)

- `equity_curve.csv` — date, equity, daily_return, n_positions, cash.
- `equity_curve.json` — same series, JSON for the UI.
- `positions_daily.csv` — date, ticker, weight, entry_date,
  cumulative_pnl.
- `trades.csv` — entry_date, exit_date, ticker, entry_px, exit_px,
  gross_return, net_return, hold_days.
- `band_curves.json` — one equity curve per band (elite,
  high_confidence, constructive, monitor, weakening, review) computed
  as the per-band sub-portfolio. Lets the UI plot "what would Review
  have done?"
- `pillar_attribution.json` — per-pillar marginal contribution (see §5).
- `summary.json` — total_return, ann_return, max_drawdown, sharpe,
  sortino, hit_rate, avg_hold_days, turnover, n_trades, n_unique_tkr.
- `report.md` — human-readable, same shape as the existing validation
  report (`data/lthcs/backtest/2026-05-18_validation/report.md`).
- `run_meta.json` — start, end, weights_profile_hash, code_sha, params.

## 5. Per-pillar attribution math

The interesting question: of the realized return, how much can be
attributed to each pillar?

- **Approach A**: re-run the entire backtest with `weights = [1, 0, 0,
  0, 0]` for each pillar in turn. Each run produces an isolated equity
  curve.
- **Approach B (recommended)**: anchor on the *production* weight
  profile (current `lthcs/score.py` weights). For each pillar `p`,
  re-run with `weights[p] := 0` and renormalize. Δ-equity between
  baseline and pillar-zeroed = "P&L if pillar p disappeared". This is
  more realistic and matches how the composite is actually shipped.
- **Phase 2 ships Approach B**; the engine exposes Approach A behind a
  flag for diagnostic comparison.

Caveat to document in the report: pillar attributions are *not
additive* — removing two pillars is not the sum of removing each. We
report both the marginal contribution (Δ vs baseline) and the LOO
(leave-one-out) curve.

## 6. Architecture

- `lthcs/backtest_engine.py` (new) — pure event-driven engine. Takes a
  `band_history` panel + a `prices` panel + a `params` dict and returns
  the artifact dict. Stateless; no I/O.
- `lthcs/backtest_engine_attribution.py` (new) — orchestrates the §5
  Approach-B re-runs. Imports the engine, does not duplicate logic.
- `scripts/lthcs_backtest.py` — refactor as a thin wrapper that
  delegates to `backtest_engine` for the P&L sections and keeps the
  existing `lthcs/backtest.attribute_returns` IC tables (the IC report
  is still useful and cheap). Existing CLI flags preserved; new flags:
  `--engine {ic,pnl,both}` (default `both`), `--cost-bps 5`,
  `--weights-profile <path>` (default: production profile).
- **Cache key**: `(start_date, end_date, universe_hash,
  weights_profile_hash, params_hash)`. Cached artifacts under
  `.cache/lthcs/backtest_engine/<key>.json`. Saves recomputation when
  attribution re-runs differ only in one pillar weight.
- **Read-only with respect to `data/lthcs/`** — same constraint as
  `lthcs/backtest.py:24`.

## 7. Cadence

- Hourly: too expensive (price fetches + per-pillar re-runs).
- **Daily 23:30 UTC** — runs *after* the existing `lthcs-daily.yml`
  cron (`0 23 * * *`, `docs/lthcs-open-items-audit.md:486`) lands the
  day's snapshot. New workflow: `lthcs-backtest-daily.yml`. Skips
  silently if fewer than 30 snapshots exist (mirrors the monthly
  workflow's behavior at `docs/lthcs-open-items-audit.md:489`).
- The existing `lthcs-backtest-monthly.yml` stays (cheap, runs the
  IC-only validator); the new daily workflow runs the full P&L engine.

## 8. Phases

- **Phase 1 (M)**: Engine + Phase-1 strategy (long-only Buy band,
  cost-aware, 1-day delay). Daily equity curve, summary stats,
  band-curves output, `report.md`. One swarm.
- **Phase 2 (M)**: Per-pillar attribution (Approach B), UI bar chart.
- **Phase 3 (M)**: Strategy variants — long-only-Buy,
  long-Buy/short-Review, dollar-neutral, top-K-by-composite. Each as a
  named profile under `lthcs/backtest_profiles/`.
- **Phase 4 (L)**: Feed Phase 1–3 curves into walk-forward CV for
  Tier 5 #25 (Adaptive Weights V2). See §12.

## 9. Files to create / modify

- `lthcs/backtest_engine.py` (new) — core engine.
- `lthcs/backtest_engine_attribution.py` (new) — per-pillar runner.
- `lthcs/backtest_profiles/` (new) — strategy profile dataclasses /
  JSON (Phase 3).
- `scripts/lthcs_backtest.py` — refactor: keep IC section, swap the
  P&L section to call the engine.
- `lthcs_tab/backtest/` (new or extend if present) — UI page.
- `data/lthcs/backtest/<run_id>/...` — output convention (dir already
  exists).
- `.github/workflows/lthcs-backtest-daily.yml` (new) — daily cron.
- `tests/lthcs/test_backtest_engine.py` (new) — unit + synthetic-data
  integration tests.
- `README_LTHCS.md` — new "Backtest engine" section.

## 10. UI integration

New tab `/lthcs/backtest/`. The path is mentioned in earlier docs; the
implementer should check `lthcs_tab/` for an existing stub before
scaffolding. Components:

- **Equity curve chart** — strategy vs benchmark (SPY by default; user
  toggle for QQQ + equal-weight LTHCS universe).
- **Per-band sub-portfolio curves** — one line per band.
- **Per-pillar attribution bar chart** — Δ-Sharpe vs baseline.
- **Drawdown chart** — peak-to-trough underwater curve.
- **Summary stats card** — total return, ann return, Sharpe, Sortino,
  max DD, hit rate, avg hold, turnover.

V2 only (ship under `/v2/lthcs/backtest/`). Do not touch V1 per
`memory/work_style.md`.

## 11. Open questions

- **Benchmark**: SPY by default. Equal-weight LTHCS universe is a
  fairer benchmark for picking-out-of-our-list skill — ship both.
  Decision deferred to Phase 1 implementer.
- **Crypto handling**: defer to Tier 5 #27 implementation. Engine
  should accept any ticker with a price series; the universe filter
  lives at the wrapper layer.
- **Look-ahead leaks**: (i) entry executed on the close *after* the
  snapshot date — already specified §3; (ii) `band_history` is loaded
  from `snapshots/<date>.json` which is written by `lthcs_daily.py` at
  23:00 UTC, so a same-day execution would be a leak — the 1-day
  delay handles this; (iii) any pillar that uses *forward-looking* AV
  NEWS_SENTIMENT publish times needs to be checked (the AND-not-OR
  quirk in `memory/alpha_vantage_news_sentiment_quirk.md` already
  neutralizes Thesis at 50, so no leak today, but flag for any future
  Thesis fix).
- **Transaction-cost calibration**: 5 bps each side is a starting
  guess. A follow-up task (out of scope here) should sweep 0 / 5 /
  10 / 20 bps and report sensitivity; if Sharpe collapses at 10 bps
  the strategy is not real.

## 12. Tier 5 #25 dependency

Once the engine produces equity curves (Phase 1 lands), the
walk-forward CV in `lthcs/adaptive_weights.py:636` (`walk_forward_tune`)
can ingest *equity-based* P&L instead of IC time series — this is the
upgrade Adaptive Weights V2 needs. **Promotion gate remains
time-gated**: per
`data/lthcs/adaptive_weights/2026-05-18_walk_forward_after_fixes.md:20`,
`SHIP_MIN_TEST_OBS = 20` at `h=21d` needs ~July 2026 before the OOS
sample is credible. The engine generates the curve immediately; the
SHIP verdict waits.

## 13. Effort revision

Audit: **L** (`docs/lthcs-open-items-audit.md:427`). Revised:

| Phase | Effort | Deliverable |
|---|---|---|
| 1 | **M** | Engine + base curve + summary + report.md |
| 2 | M | Per-pillar attribution + UI chart |
| 3 | M | Strategy variants |
| 4 | L | Adaptive Weights V2 consumer (#25 begins) |

Phase 1 is shippable in **one swarm**. Auditor's "L" was the sum of
all four phases.

## 14. Run on current 90-day history?

**Yes — Phase 1 should run on the existing 90-day backfill
immediately.** It is enough data to (a) validate the engine wiring
end-to-end, (b) produce a first honest non-overlapping equity curve
that supplants the inflated "+18.7 Sharpe" number, and (c) seed the
nightly artifact. The *promotion gate* for Tier 5 #25 still waits
until July 2026 — but the engine itself must not.
