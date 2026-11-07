# Daily audit 2026-11-07 — AMBER

**AMBER** · 0 P0 · 1 P1 · 3 P2 · 1 new · 0 fixed (vs. 2026-11-06)

Generated 2026-11-07T11:23:19Z by [daily-audit](https://github.com/btabiado/alpine-data/actions/runs/42367796105). Machine-readable: `audit/daily/2026-11-07.json`.

14 jobs started late; 4 jobs skipped schedule ticks; watchdog caught up; data fresh

## New since yesterday (1)
- **P1** [schedule] Scheduled workflow lthcs-news-hourly missed 1 day(s) in the last week — cron 19 * * * *; no scheduled run on: 2026-11-07

## Ongoing (3)
- **P2** [api] Upstream APIs blocked (1): Farside (ETF flows) _(since 2026-10-06)_
- **P2** [api] Upstream APIs degraded (1): DeFiLlama bridges _(since 2026-10-05)_
- **P2** [lthcs] LTHCS snapshot 2026-11-07 scored 514 of 515; missing WBD (no SEC CIK match; no Yahoo prices) — WBD (no SEC CIK match; no Yahoo prices) _(since 2026-11-02)_

## Fixed since yesterday (0)
- none

## Snapshot

- **LTHCS quality audit** (data/lthcs/quality_audit/latest_summary.json): HEALTHY. thesis_integrity is KNOWN GAP (accepted) (decided 2026-10-17, review by 2027-04-17)
- **LTHCS scored tickers** (2026-11-07): 514 of 515. Missing: WBD (no SEC CIK match; no Yahoo prices)
- **Feeds** (data_health, committed): 1 disclosed, 33 ok, 15 skipped, 1 suppressed
  - 5 git daily-commit history checks not judged in this checkout (shallow clone); data-health.yml owns those gaps
  - muted: data-mufon.json (muted until 2027-01-04: NUFORC permitted aggregated totals only. Raw …)
- **Workflows** (last 24h, main): 18 ran, 0 ended failed
- **API status** (2026-11-07T07:05:15+00:00): 2 auth_required, 1 blocked, 1 degraded, 1 retired, 49 up
  - no verdict changes vs. git:5f62346206 (2026-11-06T09:19:48+00:00)

**Spot checks** (live site vs. primary sources)

| check | status | detail |
|---|---|---|
| live_site_build_age | ok | built 2026-11-07T09:56:53Z (1h ago) |
| btc_price_vs_coingecko | ok | site 75929.4 vs 75971.83427199487 (0.056%), payload 1h old, source CoinGecko market_chart days=1 (nearest to … |
| eth_price_vs_coingecko | ok | site 2242.53 vs 2243.198627319531 (0.03%), payload 1h old, source CoinGecko market_chart days=1 (nearest to p… |
| fear_greed_vs_alternative_me | ok | 2026-11-07: site 25 vs 25 |
| btc_tip_height_vs_mempool_space | ok | site 974916 vs 974927 (lag 11 blocks) |
| treasury_10y_vs_treasury_gov | ok | 2026-11-05: site 4.84 vs 4.84 |

**Live payload stamps**: 10 of 15 stamped within 48h, the rest:

| payload | status | data as_of | generated |
|---|---|---|---|
| data-aviation.json | ok | 2025-12-31 (311d) | – |
| data-city.json | ok | 2026-10-01 (37d) | 2026-11-06T15:21 (20h) |
| data-mufon.json | ok | – | 2026-10-06T12:22 (32d) |
| data-tsa.json | ok | 2026-11-05 (2d) | 2026-11-06T16:09 (19h) |
| data-us_states.json | ok | – | – |

## Schedules (last 7 days)

| workflow | cron (UTC) | days run/due | ticks run | median delay | last scheduled run | flag |
|---|---|---|---|---|---|---|
| aviation-opensky | 25 * * * * | 8/8 | 42% (scheduler 16%) | 0.3h | 2026-11-07T07:19 success; catch-up 2026-11-07T10:01 success | catch-up 47 |
| aviation-tsa | 10 14 * * * | 7/7 | 100% | 1.6h (scheduler 6.3h) | 2026-11-06T20:23 success; catch-up 2026-11-06T16:09 success | catch-up 7 |
| catalog-health | 23 8 * * 1 | 1/1 | 100% | 10.1h | 2026-11-02T18:29 success | ok |
| cfpb-daily | 17 7 * * * | 6/6 | 100% | 2.5h (scheduler 8.2h) | 2026-11-06T15:04 success; catch-up 2026-11-07T10:01 success | catch-up 6 |
| city-daily | 29 7 * * * | 6/6 | 100% | 2.2h (scheduler 8.2h) | 2026-11-06T15:15 success; catch-up 2026-11-07T10:01 success | catch-up 6 |
| codeql | 23 4 * * 0 | 1/1 | 100% | 7.0h | 2026-11-01T11:19 success | ok |
| cron-watchdog | 12 * * * * ; 27 * * * * ; 42 * * * * ; 57 * * * * | 8/8 | 25% | 0.0h | 2026-11-07T08:03 success | ok |
| crypto-odds-daily | 15 14 * * * | 7/7 | 100% | 1.8h (scheduler 6.2h) | 2026-11-06T20:25 success; catch-up 2026-11-06T16:09 success | catch-up 7 |
| daily-audit | 17 9 * * * | 6/6 | 100% | 2.4h (scheduler 8.0h) | 2026-11-06T16:45 success; catch-up 2026-11-06T11:19 success | catch-up 6 |
| data-health | 11 15 * * * | 7/7 | 100% | 1.9h (scheduler 5.8h) | 2026-11-06T20:53 success; catch-up 2026-11-06T16:48 success | catch-up 7 |
| etf-flows-daily | 15 9 * * * | 6/6 | 100% | 2.3h (scheduler 8.0h) | 2026-11-06T16:47 success; catch-up 2026-11-07T11:20 success | catch-up 6 |
| lthcs-backtest-daily | 30 23 * * * | 7/7 | 100% | 2.1h (scheduler 3.6h) | 2026-11-07T02:57 success; catch-up 2026-11-07T01:12 success | catch-up 7 |
| lthcs-backtest-monthly | 19 6 1 * * | 1/1 | 100% | 7.1h | 2026-11-01T13:26 success | ok |
| lthcs-beta-verdict-monthly | 37 8 1 * * | 1/1 | 100% | 6.8h | 2026-11-01T15:26 success | ok |
| lthcs-crypto-daily | 47 11 * * * | 7/7 | 100% | 2.5h (scheduler 6.9h) | 2026-11-06T18:31 success; catch-up 2026-11-06T14:12 success | catch-up 7 |
| lthcs-daily | 11 23 * * * | 7/7 | 100% | 2.2h (scheduler 3.8h) | 2026-11-07T02:46 success; catch-up 2026-11-07T00:52 success | catch-up 7 |
| lthcs-news-hourly | 19 * * * * | 7/8 | 38% (scheduler 16%) | 0.2h | 2026-11-07T10:21 success; catch-up 2026-11-07T10:01 success | MISSED 1d, catch-up 42 |
| lthcs-quality-audit-monthly | 43 9 1 * * | 1/1 | 100% | 6.6h | 2026-11-01T16:21 success | ok |
| lthcs-trends-daily | 13 4 * * * | 6/6 | 100% | 2.3h (scheduler 7.8h) | 2026-11-07T10:59 success; catch-up 2026-11-07T07:02 success | catch-up 6 |
| lthcs-trends-weekly | 47 2 * * 1 | 1/1 | 100% | 7.2h | 2026-11-02T09:59 success | ok |
| lthcs-tune-weights-monthly | 41 7 1 * * | 1/1 | 100% | 6.8h | 2026-11-01T14:29 success | ok |
| lthcs-validate-weekly | 11 5 * * 1 | 1/1 | 100% | 8.2h | 2026-11-02T13:23 success | ok |
| money-flow-daily | 30 8 * * * | 6/6 | 100% | 3.1h (scheduler 8.0h) | 2026-11-06T16:07 success; catch-up 2026-11-07T10:39 success | catch-up 6 |
| pages | 7 * * * * | 8/8 | 39% (scheduler 16%) | 0.3h | 2026-11-07T09:55 success; catch-up 2026-11-07T07:02 success | catch-up 37 |
| real-estate-daily | 13 6 * * * | 6/6 | 100% | 3.2h (scheduler 8.2h) | 2026-11-06T14:10 success; catch-up 2026-11-07T07:46 success | catch-up 6 |
| secrets-check | 23 15 * * 1 | 1/1 | 100% | 7.8h | 2026-11-02T23:08 success | ok |
| security-audit | 31 9 * * 1 | 1/1 | 100% | 10.2h | 2026-11-02T19:41 success | ok |
| trufflehog-weekly | 37 3 * * 0 | 1/1 | 100% | 7.2h | 2026-11-01T10:46 success | ok |
| usaspending-daily | 47 7 * * * | 6/6 | 100% | 3.4h (scheduler 8.0h) | 2026-11-06T15:24 success; catch-up 2026-11-07T10:01 success | catch-up 6 |

A catch-up is a `workflow_dispatch` by `github-actions[bot]` (the cron watchdog), not a GitHub schedule event. Ticks run and median delay count it toward freshness; the scheduler figure is what the schedule event delivered on its own.

## UX (live site)

| page | viewport | HTTP | weight / requests | JS exceptions | console errors | failed requests | overflow | tabs tappable |
|---|---|---|---|---|---|---|---|---|
| / | phone | 200 | 1.9 MB / 27 | 0 | 0 | 0 | no | 22/22 |
| / | desktop | 200 | 1.9 MB / 27 | 0 | 0 | 0 | no | 22/22 |
| /summit/ | phone | 200 | 0.5 MB / 1 | 0 | 0 | 0 | no | – |
| /summit/ | desktop | 200 | 0.5 MB / 1 | 0 | 0 | 0 | no | – |
| /health/ | phone | 200 | 0.0 MB / 4 | 0 | 0 | 0 | no | – |
| /health/ | desktop | 200 | 0.0 MB / 4 | 0 | 0 | 0 | no | – |
| /real-estate/ | phone | 200 | 1.2 MB / 12 | 0 | 0 | 0 | no | – |
| /real-estate/ | desktop | 200 | 1.2 MB / 24 | 0 | 0 | 0 | no | – |
| /lthcs/ | phone | 200 | 0.4 MB / 37 | 0 | 0 | 0 | no | – |
| /lthcs/ | desktop | 200 | 0.4 MB / 37 | 0 | 0 | 0 | no | – |
| /api-universe/ | phone | 200 | 0.4 MB / 6 | 0 | 0 | 0 | no | – |
| /api-universe/ | desktop | 200 | 0.4 MB / 6 | 0 | 0 | 0 | no | – |

UX audit took 58s.
