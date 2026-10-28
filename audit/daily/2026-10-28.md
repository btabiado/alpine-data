# Daily audit 2026-10-28 — RED

**RED** · 1 P0 · 3 P1 · 3 P2 · 5 new · 0 fixed (vs. 2026-10-27)

Generated 2026-10-28T10:55:10Z by [daily-audit](https://github.com/btabiado/alpine-data/actions/runs/40675468596). Machine-readable: `audit/daily/2026-10-28.json`.

7 jobs started late; 4 jobs skipped schedule ticks; watchdog caught up; data fresh

## New since yesterday (5)
- **P0** [ux] V1 (/) failed to load on phone: page.goto: Timeout 45000ms exceeded. Call log: - navigating to "https://btabiado.github.io/alpine-data/", waiting until "load"
- **P1** [schedule] Scheduled workflow lthcs-news-hourly missed 1 day(s) in the last week — cron 19 * * * *; no scheduled run on: 2026-10-28
- **P1** [schedule] Scheduled workflow pages missed 1 day(s) in the last week — cron 7 * * * *; no scheduled run on: 2026-10-28
- **P1** [workflow] Workflow real-estate-daily failed in the last 24h — step 'Fetch real estate data' — Real estate snapshot rejected: redfin contributed 0 rows; redfin rows collapsed from 878 to 0 ([run](https://github.com/btabiado/alpine-data/actions/runs/40656164643))
- **P2** [api] Upstream APIs blocked (1): Redfin Data Center

## Ongoing (2)
- **P2** [api] Upstream APIs blocked (1): Farside (ETF flows) _(since 2026-10-06)_
- **P2** [api] Upstream APIs degraded (1): DeFiLlama bridges _(since 2026-10-05)_

## Fixed since yesterday (0)
- none

## Snapshot

- **LTHCS quality audit** (data/lthcs/quality_audit/latest_summary.json): DEGRADED. thesis_integrity is KNOWN GAP (accepted) (decided 2026-10-17, review by 2027-04-17)
- **Feeds** (data_health, committed): 1 disclosed, 33 ok, 15 skipped, 1 suppressed
  - muted: data-mufon.json (muted until 2027-01-04: NUFORC permitted aggregated totals only. Raw …)
- **Workflows** (last 24h, main): 20 ran, 1 ended failed (real-estate-daily)
- **API status** (2026-10-28T08:20:59+00:00): 2 auth_required, 2 blocked, 1 degraded, 1 retired, 48 up
  - changed vs. git:d06e4d3b6f (2026-10-27T10:27:08+00:00): Census ACS: down → up; Redfin Data Center: up → blocked

**Spot checks** (live site vs. primary sources)

| check | status | detail |
|---|---|---|
| live_site_build_age | ok | built 2026-10-28T08:20:06Z (3h ago) |
| btc_price_vs_coingecko | ok | site 81061.49 vs 81089.17756162131 (0.034%), payload 3h old, source CoinGecko market_chart days=1 (nearest to… |
| eth_price_vs_coingecko | ok | site 2362.18 vs 2362.9469654744125 (0.032%), payload 3h old, source CoinGecko market_chart days=1 (nearest to… |
| fear_greed_vs_alternative_me | ok | 2026-10-28: site 51 vs 51 |
| btc_tip_height_vs_mempool_space | ok | site 973524 vs 973538 (lag 14 blocks) |
| treasury_10y_vs_treasury_gov | ok | 2026-10-26: site 5.0 vs 5.0 |

**Live payload stamps**: 10 of 15 stamped within 48h, the rest:

| payload | status | data as_of | generated |
|---|---|---|---|
| data-aviation.json | ok | 2025-12-31 (301d) | – |
| data-city.json | ok | 2026-09-01 (57d) | 2026-10-27T13:34 (21h) |
| data-mufon.json | ok | – | 2026-10-06T12:22 (22d) |
| data-tsa.json | ok | 2026-10-26 (2d) | 2026-10-27T16:30 (18h) |
| data-us_states.json | ok | – | – |

## Schedules (last 7 days)

| workflow | cron (UTC) | days run/due | ticks run | median delay | last scheduled run | flag |
|---|---|---|---|---|---|---|
| aviation-opensky | 25 * * * * | 8/8 | 44% (scheduler 23%) | 0.2h | 2026-10-28T06:55 success; catch-up 2026-10-28T08:59 success | catch-up 47 |
| aviation-tsa | 10 14 * * * | 7/7 | 100% | 1.8h (scheduler 4.3h) | 2026-10-27T19:22 success; catch-up 2026-10-27T16:30 success | catch-up 7 |
| catalog-health | 23 8 * * 1 | 1/1 | 100% | 6.9h | 2026-10-26T15:15 success | ok |
| cfpb-daily | 17 7 * * * | 6/6 | 100% | 2.4h (scheduler 5.7h) | 2026-10-27T13:13 success; catch-up 2026-10-28T08:59 success | catch-up 6 |
| city-daily | 29 7 * * * | 6/6 | 100% | 2.2h (scheduler 5.6h) | 2026-10-27T13:28 success; catch-up 2026-10-28T08:59 success | catch-up 6 |
| codeql | 23 4 * * 0 | 1/1 | 100% | 4.8h | 2026-10-25T09:12 success | ok |
| cron-watchdog | 12 * * * * ; 27 * * * * ; 42 * * * * ; 57 * * * * | 8/8 | 37% | 0.0h | 2026-10-28T08:59 success | ok |
| crypto-odds-daily | 15 14 * * * | 7/7 | 100% | 1.9h (scheduler 4.2h) | 2026-10-27T19:24 success; catch-up 2026-10-27T16:30 success | catch-up 7 |
| daily-audit | 17 9 * * * | 6/6 | 100% | 2.3h (scheduler 5.2h) | 2026-10-27T15:20 success; catch-up 2026-10-27T11:19 success | catch-up 6 |
| data-health | 11 15 * * * | 7/7 | 100% | 2.2h (scheduler 4.0h) | 2026-10-27T20:05 success; catch-up 2026-10-27T16:45 success | catch-up 7 |
| etf-flows-daily | 15 9 * * * | 6/6 | 100% | 2.1h (scheduler 5.3h) | 2026-10-27T15:22 success; catch-up 2026-10-28T10:51 success | catch-up 6 |
| lthcs-backtest-daily | 30 23 * * * | 7/7 | 100% | 1.8h | 2026-10-28T02:28 success; catch-up 2026-10-28T01:14 success | catch-up 7 |
| lthcs-backtest-monthly | 19 6 1 * * | 0/0 | – | – | 2026-10-01T11:52 success | ok |
| lthcs-beta-verdict-monthly | 37 8 1 * * | 0/0 | – | – | 2026-10-01T15:12 failure; recovered 2026-10-16T21:48 success (workflow_dispatch) | recovered |
| lthcs-crypto-daily | 47 11 * * * | 7/7 | 100% | 1.6h (scheduler 4.3h) | 2026-10-27T16:52 success; catch-up 2026-10-27T13:34 success | catch-up 7 |
| lthcs-daily | 11 23 * * * | 7/7 | 100% | 1.9h | 2026-10-28T02:12 success; catch-up 2026-10-28T00:51 success | catch-up 6 |
| lthcs-news-hourly | 19 * * * * | 7/8 | 41% (scheduler 22%) | 0.2h | 2026-10-28T09:47 success; catch-up 2026-10-28T08:21 success | MISSED 1d, catch-up 39 |
| lthcs-quality-audit-monthly | 43 9 1 * * | 0/0 | – | – | 2026-10-01T15:53 failure; recovered 2026-10-17T12:56 success (workflow_dispatch) | recovered |
| lthcs-trends-daily | 13 4 * * * | 6/6 | 100% | 1.9h (scheduler 5.2h) | 2026-10-28T10:31 success; catch-up 2026-10-28T05:56 success | catch-up 6 |
| lthcs-trends-weekly | 47 2 * * 1 | 1/1 | 100% | 5.3h | 2026-10-26T08:08 success | ok |
| lthcs-tune-weights-monthly | 41 7 1 * * | 0/0 | – | – | 2026-10-01T14:10 success | ok |
| lthcs-validate-weekly | 11 5 * * 1 | 1/1 | 100% | 5.8h | 2026-10-26T10:58 success | ok |
| money-flow-daily | 30 8 * * * | 6/6 | 100% | 2.0h (scheduler 5.5h) | 2026-10-27T14:48 success; catch-up 2026-10-28T10:05 success | catch-up 6 |
| pages | 7 * * * * | 7/8 | 41% (scheduler 22%) | 0.1h | 2026-10-28T08:18 success; catch-up 2026-10-28T05:56 success | MISSED 1d, catch-up 32 |
| real-estate-daily | 13 6 * * * | 6/6 | 100% | 1.8h (scheduler 5.5h) | 2026-10-27T12:02 success; catch-up 2026-10-28T08:21 failure | catch-up 6 |
| secrets-check | 23 15 * * 1 | 1/1 | 100% | 5.2h | 2026-10-26T20:35 success | ok |
| security-audit | 31 9 * * 1 | 1/1 | 100% | 6.8h | 2026-10-26T16:16 success | ok |
| trufflehog-weekly | 37 3 * * 0 | 1/1 | 100% | 5.1h | 2026-10-25T08:42 success | ok |
| usaspending-daily | 47 7 * * * | 6/6 | 100% | 2.4h (scheduler 5.4h) | 2026-10-27T13:38 success; catch-up 2026-10-28T10:05 success | catch-up 6 |

A catch-up is a `workflow_dispatch` by `github-actions[bot]` (the cron watchdog), not a GitHub schedule event. Ticks run and median delay count it toward freshness; the scheduler figure is what the schedule event delivered on its own.

## UX (live site)

| page | viewport | HTTP | weight / requests | JS exceptions | console errors | failed requests | overflow | tabs tappable |
|---|---|---|---|---|---|---|---|---|
| / | phone | page.goto: Timeout 45000ms exc | 0.0 MB / 0 | 0 | 0 | 0 | no | – |
| / | desktop | 200 | 1.9 MB / 28 | 0 | 0 | 0 | no | 22/22 |
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

UX audit took 97s.
