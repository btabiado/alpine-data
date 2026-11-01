# Daily audit 2026-11-01 — GREEN

**GREEN** · 0 P0 · 0 P1 · 3 P2 · 1 new · 0 fixed (vs. 2026-10-31)

Generated 2026-11-01T16:03:10Z by [daily-audit](https://github.com/btabiado/alpine-data/actions/runs/41324072671). Machine-readable: `audit/daily/2026-11-01.json`.

7 jobs started late; 4 jobs skipped schedule ticks; watchdog caught up; data fresh

Source publish lag (quarterly cadence, expected): source publish lag: nyc data_health.last_updated 2026-06-01 is the scored month of NYPD Complaints (5uac-w243). That dataset publishes complete quarters only. 153.7d old, inside the quarterly window (overdue after 175.0d).

## New since yesterday (1)
- **P2** [feed] Feed data-tsa.json is an upstream lag (3d old, limit 2d); the source has not published a newer row — upstream lag: source newest date 2026-10-29 equals this file's as_of, checked 21.6h ago (2026-10-31T18:27:29Z). TSA has not published a newer checkpoint. Disclosed under 7d; a missed check or an olde…

## Ongoing (2)
- **P2** [api] Upstream APIs blocked (1): Farside (ETF flows) _(since 2026-10-06)_
- **P2** [api] Upstream APIs degraded (1): DeFiLlama bridges _(since 2026-10-05)_

## Fixed since yesterday (0)
- none

## Snapshot

- **LTHCS quality audit** (data/lthcs/quality_audit/latest_summary.json): DEGRADED. thesis_integrity is KNOWN GAP (accepted) (decided 2026-10-17, review by 2027-04-17)
- **Feeds** (data_health, committed): 3 disclosed, 32 ok, 15 skipped, 1 suppressed
  - 5 git daily-commit history checks not judged in this checkout (shallow clone); data-health.yml owns those gaps
  - muted: data-mufon.json (muted until 2027-01-04: NUFORC permitted aggregated totals only. Raw …)
- **Workflows** (last 24h, main): 23 ran, 0 ended failed
- **API status** (2026-11-01T11:38:23+00:00): 2 auth_required, 1 blocked, 1 degraded, 1 retired, 49 up
  - no verdict changes vs. git:416261db32 (2026-10-31T14:43:32+00:00)

**Spot checks** (live site vs. primary sources)

| check | status | detail |
|---|---|---|
| live_site_build_age | ok | built 2026-11-01T15:00:05Z (1h ago) |
| btc_price_vs_coingecko | ok | site 77909.06 vs 77971.56573395313 (0.08%), payload 1h old, source CoinGecko market_chart days=1 (nearest to … |
| eth_price_vs_coingecko | ok | site 2268.55 vs 2269.9075578433435 (0.06%), payload 1h old, source CoinGecko market_chart days=1 (nearest to … |
| fear_greed_vs_alternative_me | ok | 2026-11-01: site 34 vs 34 |
| btc_tip_height_vs_mempool_space | ok | site 974136 vs 974143 (lag 7 blocks) |
| treasury_10y_vs_treasury_gov | ok | 2026-10-29: site 4.98 vs 4.98 |

**Live payload stamps**: 9 of 15 stamped within 48h, the rest:

| payload | status | data as_of | generated |
|---|---|---|---|
| data-aviation.json | ok | 2025-12-31 (306d) | – |
| data-city.json | ok | 2026-10-01 (32d) | 2026-11-01T14:28 (2h) |
| data-mufon.json | ok | – | 2026-10-06T12:22 (26d) |
| data-stock-money-flow.json | ok | 2026-10-30 (3d) | – |
| data-tsa.json | ok | 2026-10-29 (4d) | 2026-10-30T15:58 (2d) |
| data-us_states.json | ok | – | – |

## Schedules (last 7 days)

| workflow | cron (UTC) | days run/due | ticks run | median delay | last scheduled run | flag |
|---|---|---|---|---|---|---|
| aviation-opensky | 25 * * * * | 8/8 | 46% (scheduler 20%) | 0.2h | 2026-11-01T09:24 success; catch-up 2026-11-01T14:18 success | catch-up 46 |
| aviation-tsa | 10 14 * * * | 6/6 | 100% | 1.8h (scheduler 5.3h) | 2026-10-31T18:27 success; catch-up 2026-10-31T15:42 success | catch-up 6 |
| catalog-health | 23 8 * * 1 | 1/1 | 100% | 6.9h | 2026-10-26T15:15 success | ok |
| cfpb-daily | 17 7 * * * | 6/6 | 100% | 2.2h (scheduler 6.9h) | 2026-11-01T14:16 success; catch-up 2026-11-01T09:25 success | catch-up 6 |
| city-daily | 29 7 * * * | 6/6 | 100% | 2.6h (scheduler 7.0h) | 2026-11-01T14:24 success; catch-up 2026-11-01T09:25 success | catch-up 6 |
| codeql | 23 4 * * 0 | 1/1 | 100% | 7.0h | 2026-11-01T11:19 success | ok |
| cron-watchdog | 12 * * * * ; 27 * * * * ; 42 * * * * ; 57 * * * * | 8/8 | 35% | 0.0h | 2026-11-01T15:37 success | ok |
| crypto-odds-daily | 15 14 * * * | 6/6 | 100% | 1.9h (scheduler 5.3h) | 2026-10-31T18:29 success; catch-up 2026-10-31T15:52 success | catch-up 6 |
| daily-audit | 17 9 * * * | 6/6 | 100% | 2.0h (scheduler 6.7h) | 2026-10-31T15:19 success; catch-up 2026-11-01T13:12 success | catch-up 6 |
| data-health | 11 15 * * * | 6/6 | 100% | 1.8h (scheduler 5.1h) | 2026-10-31T19:16 success; catch-up 2026-10-31T17:44 success | catch-up 6 |
| etf-flows-daily | 15 9 * * * | 6/6 | 100% | 2.1h (scheduler 6.8h) | 2026-10-31T15:20 success; catch-up 2026-11-01T11:34 success | catch-up 6 |
| lthcs-backtest-daily | 30 23 * * * | 7/7 | 100% | 1.8h (scheduler 3.0h) | 2026-11-01T02:23 success; catch-up 2026-11-01T01:09 success | catch-up 7 |
| lthcs-backtest-monthly | 19 6 1 * * | 0/0 | – | – | 2026-11-01T13:26 success | ok |
| lthcs-beta-verdict-monthly | 37 8 1 * * | 0/0 | – | – | 2026-11-01T15:26 success | ok |
| lthcs-crypto-daily | 47 11 * * * | 6/6 | 100% | 1.8h (scheduler 5.7h) | 2026-10-31T16:20 success; catch-up 2026-11-01T14:18 success | catch-up 6 |
| lthcs-daily | 11 23 * * * | 7/7 | 100% | 1.7h (scheduler 3.0h) | 2026-11-01T02:11 success; catch-up 2026-11-01T01:09 failure | catch-up 6 |
| lthcs-news-hourly | 19 * * * * | 8/8 | 40% (scheduler 19%) | 0.2h | 2026-11-01T15:15 success; catch-up 2026-11-01T14:18 success | catch-up 40 |
| lthcs-quality-audit-monthly | 43 9 1 * * | 0/0 | – | – | 2026-10-01T15:53 failure; recovered 2026-10-17T12:56 success (workflow_dispatch) | recovered |
| lthcs-trends-daily | 13 4 * * * | 7/7 | 100% | 1.8h (scheduler 6.3h) | 2026-11-01T11:13 success; catch-up 2026-11-01T05:53 success | catch-up 7 |
| lthcs-trends-weekly | 47 2 * * 1 | 1/1 | 100% | 5.3h | 2026-10-26T08:08 success | ok |
| lthcs-tune-weights-monthly | 41 7 1 * * | 0/0 | – | – | 2026-11-01T14:29 success | ok |
| lthcs-validate-weekly | 11 5 * * 1 | 1/1 | 100% | 5.8h | 2026-10-26T10:58 success | ok |
| money-flow-daily | 30 8 * * * | 6/6 | 100% | 2.0h (scheduler 6.8h) | 2026-11-01T15:23 success; catch-up 2026-11-01T11:34 success | catch-up 6 |
| pages | 7 * * * * | 8/8 | 39% (scheduler 19%) | 0.1h | 2026-11-01T14:58 success; catch-up 2026-11-01T14:18 success | catch-up 34 |
| real-estate-daily | 13 6 * * * | 6/6 | 100% | 2.1h (scheduler 6.5h) | 2026-11-01T13:13 success; catch-up 2026-11-01T07:56 success | catch-up 6 |
| secrets-check | 23 15 * * 1 | 1/1 | 100% | 5.2h | 2026-10-26T20:35 success | ok |
| security-audit | 31 9 * * 1 | 1/1 | 100% | 6.8h | 2026-10-26T16:16 success | ok |
| trufflehog-weekly | 37 3 * * 0 | 1/1 | 100% | 7.2h | 2026-11-01T10:46 success | ok |
| usaspending-daily | 47 7 * * * | 6/6 | 100% | 2.3h (scheduler 6.8h) | 2026-11-01T14:32 success; catch-up 2026-11-01T09:25 success | catch-up 6 |

A catch-up is a `workflow_dispatch` by `github-actions[bot]` (the cron watchdog), not a GitHub schedule event. Ticks run and median delay count it toward freshness; the scheduler figure is what the schedule event delivered on its own.

## UX (live site)

| page | viewport | HTTP | weight / requests | JS exceptions | console errors | failed requests | overflow | tabs tappable |
|---|---|---|---|---|---|---|---|---|
| / | phone | 200 | 1.9 MB / 28 | 0 | 0 | 0 | no | 22/22 |
| / | desktop | 200 | 1.9 MB / 28 | 0 | 0 | 0 | no | 22/22 |
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

UX audit took 60s.
