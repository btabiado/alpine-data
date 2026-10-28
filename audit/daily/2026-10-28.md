# Daily audit 2026-10-28 — AMBER

**AMBER** · 0 P0 · 1 P1 · 3 P2 · 2 new · 0 fixed (vs. 2026-10-27)

Generated 2026-10-28T16:04:07Z by [daily-audit](https://github.com/btabiado/alpine-data/actions/runs/40721784824). Machine-readable: `audit/daily/2026-10-28.json`.

7 jobs started late; 4 jobs skipped schedule ticks; watchdog caught up; data fresh

## New since yesterday (2)
- **P1** [workflow] Workflow real-estate-daily failed in the last 24h — step 'Fetch real estate data' — Real estate snapshot rejected: redfin contributed 0 rows; redfin rows collapsed from 878 to 0 ([run](https://github.com/btabiado/alpine-data/actions/runs/40690172315))
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
- **API status** (2026-10-28T15:58:21+00:00): 2 auth_required, 2 blocked, 1 degraded, 1 retired, 48 up
  - changed vs. git:a3b7f76723 (2026-10-27T13:17:06+00:00): Redfin Data Center: up → blocked

**Spot checks** (live site vs. primary sources)

| check | status | detail |
|---|---|---|
| live_site_build_age | ok | built 2026-10-28T15:57:09Z (4m ago) |
| btc_price_vs_coingecko | ok | site 80409.61 vs 80410.32427866892 (0.001%), payload 5m old, source CoinGecko market_chart days=1 (nearest to… |
| eth_price_vs_coingecko | ok | site 2329.96 vs 2329.296763163518 (0.028%), payload 5m old, source CoinGecko market_chart days=1 (nearest to … |
| fear_greed_vs_alternative_me | ok | 2026-10-28: site 51 vs 51 |
| btc_tip_height_vs_mempool_space | ok | site 973574 vs 973574 (lag 0 blocks) |
| treasury_10y_vs_treasury_gov | ok | 2026-10-26: site 5.0 vs 5.0 |

**Live payload stamps**: 10 of 15 stamped within 48h, the rest:

| payload | status | data as_of | generated |
|---|---|---|---|
| data-aviation.json | ok | 2025-12-31 (302d) | – |
| data-city.json | ok | 2026-09-01 (58d) | 2026-10-28T14:21 (2h) |
| data-mufon.json | ok | – | 2026-10-06T12:22 (22d) |
| data-tsa.json | ok | 2026-10-26 (3d) | 2026-10-27T16:30 (24h) |
| data-us_states.json | ok | – | – |

## Schedules (last 7 days)

| workflow | cron (UTC) | days run/due | ticks run | median delay | last scheduled run | flag |
|---|---|---|---|---|---|---|
| aviation-opensky | 25 * * * * | 8/8 | 44% (scheduler 22%) | 0.2h | 2026-10-28T14:07 success; catch-up 2026-10-28T12:44 success | catch-up 47 |
| aviation-tsa | 10 14 * * * | 6/6 | 100% | 1.9h (scheduler 4.3h) | 2026-10-27T19:22 success; catch-up 2026-10-28T15:57 success | catch-up 6 |
| catalog-health | 23 8 * * 1 | 1/1 | 100% | 6.9h | 2026-10-26T15:15 success | ok |
| cfpb-daily | 17 7 * * * | 6/6 | 100% | 2.4h (scheduler 5.7h) | 2026-10-28T14:02 success; catch-up 2026-10-28T08:59 success | catch-up 6 |
| city-daily | 29 7 * * * | 6/6 | 100% | 2.2h (scheduler 5.6h) | 2026-10-28T14:17 success; catch-up 2026-10-28T08:59 success | catch-up 6 |
| codeql | 23 4 * * 0 | 1/1 | 100% | 4.8h | 2026-10-25T09:12 success | ok |
| cron-watchdog | 12 * * * * ; 27 * * * * ; 42 * * * * ; 57 * * * * | 8/8 | 37% | 0.0h | 2026-10-28T15:57 success | ok |
| crypto-odds-daily | 15 14 * * * | 6/6 | 100% | 1.9h (scheduler 4.2h) | 2026-10-27T19:24 success; catch-up 2026-10-28T15:57 success | catch-up 6 |
| daily-audit | 17 9 * * * | 6/6 | 100% | 2.3h (scheduler 5.2h) | 2026-10-27T15:20 success; catch-up 2026-10-28T10:51 success | catch-up 6 |
| data-health | 11 15 * * * | 6/6 | 100% | 2.5h (scheduler 4.0h) | 2026-10-27T20:05 success; catch-up 2026-10-27T16:45 success | catch-up 6 |
| etf-flows-daily | 15 9 * * * | 6/6 | 100% | 2.1h (scheduler 5.3h) | 2026-10-27T15:22 success; catch-up 2026-10-28T10:51 success | catch-up 6 |
| lthcs-backtest-daily | 30 23 * * * | 7/7 | 100% | 1.8h | 2026-10-28T02:28 success; catch-up 2026-10-28T01:14 success | catch-up 7 |
| lthcs-backtest-monthly | 19 6 1 * * | 0/0 | – | – | 2026-10-01T11:52 success | ok |
| lthcs-beta-verdict-monthly | 37 8 1 * * | 0/0 | – | – | 2026-10-01T15:12 failure; recovered 2026-10-16T21:48 success (workflow_dispatch) | recovered |
| lthcs-crypto-daily | 47 11 * * * | 6/6 | 100% | 1.6h (scheduler 4.3h) | 2026-10-27T16:52 success; catch-up 2026-10-28T13:41 success | catch-up 6 |
| lthcs-daily | 11 23 * * * | 7/7 | 100% | 1.9h | 2026-10-28T02:12 success; catch-up 2026-10-28T00:51 success | catch-up 6 |
| lthcs-news-hourly | 19 * * * * | 8/8 | 42% (scheduler 22%) | 0.2h | 2026-10-28T09:47 success; catch-up 2026-10-28T15:20 success | catch-up 40 |
| lthcs-quality-audit-monthly | 43 9 1 * * | 0/0 | – | – | 2026-10-01T15:53 failure; recovered 2026-10-17T12:56 success (workflow_dispatch) | recovered |
| lthcs-trends-daily | 13 4 * * * | 7/7 | 100% | 1.8h (scheduler 5.2h) | 2026-10-28T10:31 success; catch-up 2026-10-28T05:56 success | catch-up 7 |
| lthcs-trends-weekly | 47 2 * * 1 | 1/1 | 100% | 5.3h | 2026-10-26T08:08 success | ok |
| lthcs-tune-weights-monthly | 41 7 1 * * | 0/0 | – | – | 2026-10-01T14:10 success | ok |
| lthcs-validate-weekly | 11 5 * * 1 | 1/1 | 100% | 5.8h | 2026-10-26T10:58 success | ok |
| money-flow-daily | 30 8 * * * | 6/6 | 100% | 2.0h (scheduler 5.5h) | 2026-10-28T15:19 success; catch-up 2026-10-28T10:05 success | catch-up 6 |
| pages | 7 * * * * | 8/8 | 40% (scheduler 22%) | 0.1h | 2026-10-28T15:55 success; catch-up 2026-10-28T13:14 success | catch-up 31 |
| real-estate-daily | 13 6 * * * | 6/6 | 100% | 1.8h (scheduler 5.5h) | 2026-10-28T12:43 failure; catch-up 2026-10-28T08:21 failure | FAILING, catch-up 6 |
| secrets-check | 23 15 * * 1 | 1/1 | 100% | 5.2h | 2026-10-26T20:35 success | ok |
| security-audit | 31 9 * * 1 | 1/1 | 100% | 6.8h | 2026-10-26T16:16 success | ok |
| trufflehog-weekly | 37 3 * * 0 | 1/1 | 100% | 5.1h | 2026-10-25T08:42 success | ok |
| usaspending-daily | 47 7 * * * | 6/6 | 100% | 2.4h (scheduler 5.4h) | 2026-10-28T14:32 success; catch-up 2026-10-28T10:05 success | catch-up 6 |

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

UX audit took 76s.
