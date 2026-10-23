# Daily audit 2026-10-23 — GREEN

**GREEN** · 0 P0 · 0 P1 · 4 P2 · 2 new · 18 fixed (vs. 2026-10-22)

Generated 2026-10-23T11:35:27Z by [daily-audit](https://github.com/btabiado/alpine-data/actions/runs/39946860292). Machine-readable: `audit/daily/2026-10-23.json`.

12 jobs started late; 4 jobs skipped schedule ticks; watchdog caught up; data fresh

## New since yesterday (2)
- **P2** [api] Upstream APIs degraded (2): Census ACS, USGS ScienceBase

## Ongoing (2)
- **P2** [api] Upstream APIs blocked (1): Farside (ETF flows) _(since 2026-10-06)_
- **P2** [api] Upstream APIs degraded (1): DeFiLlama bridges _(since 2026-10-05)_

## Fixed since yesterday (18)
- **P2** [schedule] Crons that lose most of their ticks (4): aviation-opensky 35% (26 catch-up, 102 missed), cron-watchdog 27% (0 catch-up, 306 missed), lthcs-news-hourly 34% (22 catch-up, 104 missed), pages 34% (16 catch-up, 105 missed)
- **P2** [schedule] Crons starting >3h late (median delay) (14): catalog-health 7.3h, cfpb-daily 4.0h, city-daily 4.2h, codeql 4.5h, daily-audit 3.7h, etf-flows-daily 3.7h, lthcs-trends-weekly 5.3h, lthcs-validate-weekly 5.5h, money-flow-daily 3.6h, real-estate-daily 4.2h, secrets-check 5.2h, security-audit 7.1h, trufflehog-weekly 4.7h, usaspending-daily 3.9h

## Snapshot

- **LTHCS quality audit** (data/lthcs/quality_audit/latest_summary.json): DEGRADED. thesis_integrity is KNOWN GAP (accepted) (decided 2026-10-17, review by 2027-04-17)
- **Feeds** (data_health, committed): 2 disclosed, 32 ok, 15 skipped, 1 suppressed
  - muted: data-mufon.json (muted until 2027-01-04: NUFORC permitted aggregated totals only. Raw …)
- **Workflows** (last 24h, main): 20 ran, 0 ended failed
- **API status** (2026-10-23T10:00:09+00:00): 2 auth_required, 1 blocked, 3 degraded, 1 retired, 47 up
  - changed vs. git:40fed88056 (2026-10-22T09:45:39+00:00): Census ACS: up → degraded; USGS ScienceBase: up → degraded

**Spot checks** (live site vs. primary sources)

| check | status | detail |
|---|---|---|
| live_site_build_age | ok | built 2026-10-23T09:58:06Z (2h ago) |
| btc_price_vs_coingecko | ok | site 81578.23 vs 81548.60214514683 (0.036%), payload 2h old, source CoinGecko market_chart days=1 (nearest to… |
| eth_price_vs_coingecko | ok | site 2397.85 vs 2396.110231419594 (0.073%), payload 2h old, source CoinGecko market_chart days=1 (nearest to … |
| fear_greed_vs_alternative_me | ok | 2026-10-23: site 50 vs 50 |
| btc_tip_height_vs_mempool_space | ok | site 972752 vs 972762 (lag 10 blocks) |
| treasury_10y_vs_treasury_gov | ok | 2026-10-21: site 5.13 vs 5.13 |

**Live payload stamps**: 10 of 15 stamped within 48h, the rest:

| payload | status | data as_of | generated |
|---|---|---|---|
| data-aviation.json | ok | 2025-12-31 (296d) | – |
| data-city.json | ok | 2026-09-01 (52d) | 2026-10-23T09:40 (2h) |
| data-mufon.json | ok | – | 2026-10-06T12:22 (17d) |
| data-tsa.json | ok | 2026-10-21 (2d) | 2026-10-22T15:41 (20h) |
| data-us_states.json | ok | – | – |

## Schedules (last 7 days)

| workflow | cron (UTC) | days run/due | ticks run | median delay | last scheduled run | flag |
|---|---|---|---|---|---|---|
| aviation-opensky | 25 * * * * | 8/8 | 39% (scheduler 24%) | 0.2h | 2026-10-23T07:42 success; catch-up 2026-10-23T10:04 success | catch-up 33 |
| aviation-tsa | 10 14 * * * | 7/7 | 100% | 2.0h (scheduler 4.2h) | 2026-10-22T18:27 success; catch-up 2026-10-22T15:41 success | catch-up 4 |
| catalog-health | 23 8 * * 1 | 1/1 | 100% | 7.3h | 2026-10-19T15:41 success | ok |
| cfpb-daily | 17 7 * * * | 6/6 | 100% | 4.0h (scheduler 5.7h) | 2026-10-22T12:59 success; catch-up 2026-10-23T09:38 success | catch-up 4 |
| city-daily | 29 7 * * * | 6/6 | 100% | 3.7h (scheduler 5.6h) | 2026-10-22T13:05 success; catch-up 2026-10-23T09:38 success | catch-up 5 |
| codeql | 23 4 * * 0 | 1/1 | 100% | 4.5h | 2026-10-18T08:53 success | ok |
| cron-watchdog | 12 * * * * ; 27 * * * * ; 42 * * * * ; 57 * * * * | 7/7 | 29% | 0.0h | 2026-10-23T10:45 success | ok |
| crypto-odds-daily | 15 14 * * * | 7/7 | 100% | 1.9h (scheduler 4.2h) | 2026-10-22T18:29 success; catch-up 2026-10-22T16:05 success | catch-up 4 |
| daily-audit | 17 9 * * * | 6/6 | 100% | 2.5h (scheduler 5.2h) | 2026-10-22T14:30 success; catch-up 2026-10-22T11:44 success | catch-up 4 |
| data-health | 11 15 * * * | 7/7 | 100% | 2.6h (scheduler 4.0h) | 2026-10-22T19:12 success; catch-up 2026-10-22T17:46 success | catch-up 4 |
| etf-flows-daily | 15 9 * * * | 6/6 | 100% | 2.5h (scheduler 5.2h) | 2026-10-22T14:31 success; catch-up 2026-10-23T10:46 success | catch-up 4 |
| lthcs-backtest-daily | 30 23 * * * | 7/7 | 100% | 1.8h | 2026-10-23T01:42 success; catch-up 2026-10-23T01:26 success | catch-up 5 |
| lthcs-backtest-monthly | 19 6 1 * * | 0/0 | – | – | 2026-10-01T11:52 success | ok |
| lthcs-beta-verdict-monthly | 37 8 1 * * | 0/0 | – | – | 2026-10-01T15:12 failure; recovered 2026-10-16T21:48 success (workflow_dispatch) | recovered |
| lthcs-crypto-daily | 47 11 * * * | 7/7 | 100% | 2.0h (scheduler 4.1h) | 2026-10-22T16:04 success; catch-up 2026-10-22T13:44 success | catch-up 4 |
| lthcs-daily | 11 23 * * * | 7/7 | 100% | 2.0h | 2026-10-23T01:28 success; catch-up 2026-10-23T01:26 success | catch-up 5 |
| lthcs-news-hourly | 19 * * * * | 8/8 | 37% (scheduler 23%) | 0.2h | 2026-10-23T10:09 success; catch-up 2026-10-23T10:04 cancelled | catch-up 29 |
| lthcs-quality-audit-monthly | 43 9 1 * * | 0/0 | – | – | 2026-10-01T15:53 failure; recovered 2026-10-17T12:56 success (workflow_dispatch) | recovered |
| lthcs-trends-daily | 13 4 * * * | 6/6 | 100% | 2.7h (scheduler 5.2h) | 2026-10-23T09:17 success; catch-up 2026-10-23T07:24 success | catch-up 4 |
| lthcs-trends-weekly | 47 2 * * 1 | 1/1 | 100% | 5.3h | 2026-10-19T08:05 success | ok |
| lthcs-tune-weights-monthly | 41 7 1 * * | 0/0 | – | – | 2026-10-01T14:10 success | ok |
| lthcs-validate-weekly | 11 5 * * 1 | 1/1 | 100% | 5.5h | 2026-10-19T10:40 success | ok |
| money-flow-daily | 30 8 * * * | 6/6 | 100% | 3.2h (scheduler 5.5h) | 2026-10-22T13:59 success; catch-up 2026-10-23T10:04 success | catch-up 5 |
| pages | 7 * * * * | 8/8 | 37% (scheduler 23%) | 0.2h | 2026-10-23T09:56 success; catch-up 2026-10-23T09:38 success | catch-up 22 |
| real-estate-daily | 13 6 * * * | 6/6 | 100% | 3.5h (scheduler 5.5h) | 2026-10-23T11:31 success; catch-up 2026-10-23T07:43 success | catch-up 4 |
| secrets-check | 23 15 * * 1 | 1/1 | 100% | 5.2h | 2026-10-19T20:38 success | ok |
| security-audit | 31 9 * * 1 | 1/1 | 100% | 7.1h | 2026-10-19T16:36 success | ok |
| trufflehog-weekly | 37 3 * * 0 | 1/1 | 100% | 4.7h | 2026-10-18T08:18 success | ok |
| usaspending-daily | 47 7 * * * | 6/6 | 100% | 3.5h (scheduler 5.4h) | 2026-10-22T13:13 success; catch-up 2026-10-23T09:38 success | catch-up 5 |

A catch-up is a `workflow_dispatch` by `github-actions[bot]` (the cron watchdog), not a GitHub schedule event. Ticks run and median delay count it toward freshness; the scheduler figure is what the schedule event delivered on its own.

## UX (live site)

| page | viewport | HTTP | weight / requests | JS exceptions | console errors | failed requests | overflow | tabs tappable |
|---|---|---|---|---|---|---|---|---|
| / | phone | 200 | 1.9 MB / 29 | 0 | 0 | 0 | no | 22/22 |
| / | desktop | 200 | 1.9 MB / 29 | 0 | 0 | 0 | no | 22/22 |
| /summit/ | phone | 200 | 0.4 MB / 1 | 0 | 0 | 0 | no | – |
| /summit/ | desktop | 200 | 0.4 MB / 1 | 0 | 0 | 0 | no | – |
| /health/ | phone | 200 | 0.0 MB / 4 | 0 | 0 | 0 | no | – |
| /health/ | desktop | 200 | 0.0 MB / 4 | 0 | 0 | 0 | no | – |
| /real-estate/ | phone | 200 | 1.2 MB / 6 | 0 | 0 | 0 | no | – |
| /real-estate/ | desktop | 200 | 1.2 MB / 24 | 0 | 0 | 0 | no | – |
| /lthcs/ | phone | 200 | 0.4 MB / 37 | 0 | 0 | 0 | no | – |
| /lthcs/ | desktop | 200 | 0.4 MB / 37 | 0 | 0 | 0 | no | – |
| /api-universe/ | phone | 200 | 0.4 MB / 6 | 0 | 0 | 0 | no | – |
| /api-universe/ | desktop | 200 | 0.4 MB / 6 | 0 | 0 | 0 | no | – |

UX audit took 38s.
