# Daily audit 2026-11-05 — AMBER

**AMBER** · 0 P0 · 4 P1 · 4 P2 · 5 new · 0 fixed (vs. 2026-11-04)

Generated 2026-11-05T11:15:38Z by [daily-audit](https://github.com/btabiado/alpine-data/actions/runs/42017146571). Machine-readable: `audit/daily/2026-11-05.json`.

13 jobs started late; 4 jobs skipped schedule ticks; watchdog caught up; data fresh

Source publish lag (quarterly cadence, expected): source publish lag: nyc data_health.last_updated 2026-06-01 is the scored month of NYPD Complaints (5uac-w243). That dataset publishes complete quarters only. 157.5d old, inside the quarterly window (overdue after 175.0d).

## New since yesterday (5)
- **P1** [schedule] Scheduled workflow aviation-opensky missed 1 day(s) in the last week — cron 25 * * * *; no scheduled run on: 2026-11-05
- **P1** [schedule] Scheduled workflow cron-watchdog missed 1 day(s) in the last week — cron 12 * * * *, 27 * * * *, 42 * * * *, 57 * * * *; no scheduled run on: 2026-11-05
- **P1** [schedule] Scheduled workflow lthcs-news-hourly missed 1 day(s) in the last week — cron 19 * * * *; no scheduled run on: 2026-11-05
- **P1** [schedule] Scheduled workflow pages missed 1 day(s) in the last week — cron 7 * * * *; no scheduled run on: 2026-11-05
- **P2** [api] Upstream APIs blocked (1): TSA passenger volumes

## Ongoing (3)
- **P2** [api] Upstream APIs blocked (1): Farside (ETF flows) _(since 2026-10-06)_
- **P2** [api] Upstream APIs degraded (1): DeFiLlama bridges _(since 2026-10-05)_
- **P2** [lthcs] LTHCS snapshot 2026-11-05 scored 514 of 515; missing WBD (no SEC CIK match; no Yahoo prices) — WBD (no SEC CIK match; no Yahoo prices) _(since 2026-11-02)_

## Fixed since yesterday (0)
- none

## Snapshot

- **LTHCS quality audit** (data/lthcs/quality_audit/latest_summary.json): HEALTHY. thesis_integrity is KNOWN GAP (accepted) (decided 2026-10-17, review by 2027-04-17)
- **LTHCS scored tickers** (2026-11-05): 514 of 515. Missing: WBD (no SEC CIK match; no Yahoo prices)
- **Feeds** (data_health, committed): 2 disclosed, 33 ok, 15 skipped, 1 suppressed
  - 5 git daily-commit history checks not judged in this checkout (shallow clone); data-health.yml owns those gaps
  - muted: data-mufon.json (muted until 2027-01-04: NUFORC permitted aggregated totals only. Raw …)
- **Workflows** (last 24h, main): 18 ran, 0 ended failed
- **API status** (2026-11-05T10:47:52+00:00): 2 auth_required, 2 blocked, 1 degraded, 1 retired, 48 up
  - changed vs. git:64d81f10bb (2026-11-04T09:01:45+00:00): TSA passenger volumes: up → blocked

**Spot checks** (live site vs. primary sources)

| check | status | detail |
|---|---|---|
| live_site_build_age | ok | built 2026-11-05T10:46:39Z (26m ago) |
| btc_price_vs_coingecko | ok | site 80233.09 vs 80282.956969112 (0.062%), payload 28m old, source CoinGecko market_chart days=1 (nearest to … |
| eth_price_vs_coingecko | ok | site 2415.21 vs 2417.429109298439 (0.092%), payload 28m old, source CoinGecko market_chart days=1 (nearest to… |
| fear_greed_vs_alternative_me | ok | 2026-11-05: site 50 vs 50 |
| btc_tip_height_vs_mempool_space | ok | site 974656 vs 974660 (lag 4 blocks) |
| treasury_10y_vs_treasury_gov | ok | 2026-11-03: site 4.95 vs 4.95 |

**Live payload stamps**: 10 of 15 stamped within 48h, the rest:

| payload | status | data as_of | generated |
|---|---|---|---|
| data-aviation.json | ok | 2025-12-31 (309d) | – |
| data-city.json | ok | 2026-10-01 (35d) | 2026-11-04T15:45 (19h) |
| data-mufon.json | ok | – | 2026-10-06T12:22 (30d) |
| data-tsa.json | ok | 2026-11-03 (2d) | 2026-11-04T15:46 (19h) |
| data-us_states.json | ok | – | – |

## Schedules (last 7 days)

| workflow | cron (UTC) | days run/due | ticks run | median delay | last scheduled run | flag |
|---|---|---|---|---|---|---|
| aviation-opensky | 25 * * * * | 7/8 | 44% (scheduler 16%) | 0.3h | 2026-11-05T11:11 success; catch-up 2026-11-05T10:45 success | MISSED 1d, catch-up 46 |
| aviation-tsa | 10 14 * * * | 7/7 | 100% | 1.8h (scheduler 5.4h) | 2026-11-04T20:45 success; catch-up 2026-11-04T15:46 success | catch-up 7 |
| catalog-health | 23 8 * * 1 | 1/1 | 100% | 10.1h | 2026-11-02T18:29 success | ok |
| cfpb-daily | 17 7 * * * | 6/6 | 100% | 2.2h (scheduler 7.9h) | 2026-11-04T15:26 success; catch-up 2026-11-05T10:45 success | catch-up 6 |
| city-daily | 29 7 * * * | 6/6 | 100% | 2.0h (scheduler 7.9h) | 2026-11-04T15:39 success; catch-up 2026-11-04T09:05 success | catch-up 6 |
| codeql | 23 4 * * 0 | 1/1 | 100% | 7.0h | 2026-11-01T11:19 success | ok |
| cron-watchdog | 12 * * * * ; 27 * * * * ; 42 * * * * ; 57 * * * * | 7/8 | 29% | 0.0h | 2026-11-05T10:44 success | MISSED 1d |
| crypto-odds-daily | 15 14 * * * | 7/7 | 100% | 1.8h (scheduler 5.3h) | 2026-11-04T20:48 success; catch-up 2026-11-04T15:46 success | catch-up 7 |
| daily-audit | 17 9 * * * | 6/6 | 100% | 3.0h (scheduler 7.8h) | 2026-11-04T17:13 success; catch-up 2026-11-04T14:23 success | catch-up 6 |
| data-health | 11 15 * * * | 7/7 | 100% | 1.9h (scheduler 5.1h) | 2026-11-04T21:19 success; catch-up 2026-11-04T16:43 success | catch-up 7 |
| etf-flows-daily | 15 9 * * * | 6/6 | 100% | 2.5h (scheduler 7.8h) | 2026-11-04T17:17 success; catch-up 2026-11-05T11:13 success | catch-up 6 |
| lthcs-backtest-daily | 30 23 * * * | 7/7 | 100% | 1.9h (scheduler 3.2h) | 2026-11-05T03:34 success; catch-up 2026-11-05T02:19 success | catch-up 7 |
| lthcs-backtest-monthly | 19 6 1 * * | 1/1 | 100% | 7.1h | 2026-11-01T13:26 success | ok |
| lthcs-beta-verdict-monthly | 37 8 1 * * | 1/1 | 100% | 6.8h | 2026-11-01T15:26 success | ok |
| lthcs-crypto-daily | 47 11 * * * | 7/7 | 100% | 2.3h (scheduler 5.8h) | 2026-11-04T18:52 success; catch-up 2026-11-04T14:23 success | catch-up 7 |
| lthcs-daily | 11 23 * * * | 7/7 | 100% | 2.0h (scheduler 3.4h) | 2026-11-05T03:27 success; catch-up 2026-11-05T02:19 success | catch-up 7 |
| lthcs-news-hourly | 19 * * * * | 7/8 | 39% (scheduler 16%) | 0.2h | 2026-11-05T06:55 success; catch-up 2026-11-05T10:44 success | MISSED 1d, catch-up 43 |
| lthcs-quality-audit-monthly | 43 9 1 * * | 1/1 | 100% | 6.6h | 2026-11-01T16:21 success | ok |
| lthcs-trends-daily | 13 4 * * * | 6/6 | 100% | 2.0h (scheduler 7.6h) | 2026-11-04T12:01 success; catch-up 2026-11-05T07:14 success | catch-up 6 |
| lthcs-trends-weekly | 47 2 * * 1 | 1/1 | 100% | 7.2h | 2026-11-02T09:59 success | ok |
| lthcs-tune-weights-monthly | 41 7 1 * * | 1/1 | 100% | 6.8h | 2026-11-01T14:29 success | ok |
| lthcs-validate-weekly | 11 5 * * 1 | 1/1 | 100% | 8.2h | 2026-11-02T13:23 success | ok |
| money-flow-daily | 30 8 * * * | 6/6 | 100% | 3.1h (scheduler 7.9h) | 2026-11-04T16:28 success; catch-up 2026-11-05T11:12 success | catch-up 6 |
| pages | 7 * * * * | 7/8 | 38% (scheduler 16%) | 0.3h | 2026-11-05T03:26 success; catch-up 2026-11-05T10:45 success | MISSED 1d, catch-up 36 |
| real-estate-daily | 13 6 * * * | 6/6 | 100% | 2.1h (scheduler 7.8h) | 2026-11-04T14:23 success; catch-up 2026-11-05T10:45 success | catch-up 6 |
| secrets-check | 23 15 * * 1 | 1/1 | 100% | 7.8h | 2026-11-02T23:08 success | ok |
| security-audit | 31 9 * * 1 | 1/1 | 100% | 10.2h | 2026-11-02T19:41 success | ok |
| trufflehog-weekly | 37 3 * * 0 | 1/1 | 100% | 7.2h | 2026-11-01T10:46 success | ok |
| usaspending-daily | 47 7 * * * | 6/6 | 100% | 1.9h (scheduler 7.7h) | 2026-11-04T15:46 success; catch-up 2026-11-05T11:12 success | catch-up 6 |

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
| /real-estate/ | phone | 200 | 1.2 MB / 6 | 0 | 0 | 0 | no | – |
| /real-estate/ | desktop | 200 | 1.2 MB / 24 | 0 | 0 | 0 | no | – |
| /lthcs/ | phone | 200 | 0.4 MB / 37 | 0 | 0 | 0 | no | – |
| /lthcs/ | desktop | 200 | 0.4 MB / 37 | 0 | 0 | 0 | no | – |
| /api-universe/ | phone | 200 | 0.4 MB / 6 | 0 | 0 | 0 | no | – |
| /api-universe/ | desktop | 200 | 0.4 MB / 6 | 0 | 0 | 0 | no | – |

UX audit took 76s.
