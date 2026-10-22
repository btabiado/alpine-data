# Daily audit 2026-10-22 — RED

**RED** · 2 P0 · 0 P1 · 21 P2 · 2 new · 5 fixed (vs. 2026-10-21)

Generated 2026-10-22T11:50:26Z by [daily-audit](https://github.com/btabiado/alpine-data/actions/runs/39772861691). Machine-readable: `audit/daily/2026-10-22.json`.

## New since yesterday (2)
- **P0** [feed] Feed data/btc_flows.csv is STALE (2d old, limit 2d)
- **P0** [feed] Feed data/eth_flows.csv is STALE (2d old, limit 2d)

## Ongoing (21)
- **P2** [api] Upstream APIs blocked (1): Farside (ETF flows) _(since 2026-10-06)_
- **P2** [api] Upstream APIs degraded (1): DeFiLlama bridges _(since 2026-10-05)_
- **P2** [schedule] Crons that lose most of their ticks (4): aviation-opensky 35% (25 catch-up, 103 missed), cron-watchdog 28% (0 catch-up, 296 missed), lthcs-news-hourly 34% (21 catch-up, 104 missed), pages 34% (16 catch-up, 105 missed) _(since 2026-10-05)_
- **P2** [schedule] Crons starting >3h late (median delay) (15): catalog-health 7.3h, cfpb-daily 4.0h, city-daily 4.2h, codeql 4.5h, daily-audit 3.7h, etf-flows-daily 3.7h, lthcs-trends-daily 4.3h, lthcs-trends-weekly 5.3h, lthcs-validate-weekly 5.5h, money-flow-daily 3.6h, real-estate-daily 4.2h, secrets-check 5.2h, security-audit 7.1h, trufflehog-weekly 4.7h, usaspending-daily 3.9h _(since 2026-10-20)_

## Fixed since yesterday (5)
- **P1** [api] Upstream API FRED ENPLANE (air travel) is DOWN
- **P1** [workflow] Workflow etf-flows-daily failed in the last 24h — step 'Fetch BTC + ETH spot-ETF flows' — ETF flows failed (eth): fetch: Farside returned HTTP 521 <none> for https://farside.co.uk/ethereum-etf-flow-all-data/.; leaving eth_flows.csv untouched ([run](https://github.com/btabiado/alpine-data/actions/runs/39588030660))
- **P2** [schedule] Crons starting >3h late (median delay) (3): aviation-tsa 3.1h, crypto-odds-daily 3.1h, lthcs-crypto-daily 3.1h

## Snapshot

- **LTHCS quality audit** (data/lthcs/quality_audit/latest_summary.json): DEGRADED. thesis_integrity is KNOWN GAP (accepted) (decided 2026-10-17, review by 2027-04-17)
- **Feeds** (data_health, committed): 2 disclosed, 30 ok, 15 skipped, 2 stale, 1 suppressed
  - muted: data-mufon.json (muted until 2027-01-04: NUFORC permitted aggregated totals only. Raw …)
- **Workflows** (last 24h, main): 18 ran, 0 ended failed
- **API status** (2026-10-22T09:45:39+00:00): 2 auth_required, 1 blocked, 1 degraded, 1 retired, 49 up
  - no verdict changes vs. git:956fa440f3 (2026-10-21T09:41:50+00:00)

**Spot checks** (live site vs. primary sources)

| check | status | detail |
|---|---|---|
| live_site_build_age | ok | built 2026-10-22T10:03:53Z (2h ago) |
| btc_price_vs_coingecko | ok | site 81933.3 vs 81998.98617462833 (0.08%), payload 2h old, source CoinGecko market_chart days=1 (nearest to p… |
| eth_price_vs_coingecko | ok | site 2408.68 vs 2410.250451811121 (0.065%), payload 2h old, source CoinGecko market_chart days=1 (nearest to … |
| fear_greed_vs_alternative_me | ok | 2026-10-22: site 68 vs 68 |
| btc_tip_height_vs_mempool_space | ok | site 972601 vs 972614 (lag 13 blocks) |
| treasury_10y_vs_treasury_gov | ok | 2026-10-20: site 5.12 vs 5.12 |

**Live payload stamps**: 10 of 15 stamped within 48h, the rest:

| payload | status | data as_of | generated |
|---|---|---|---|
| data-aviation.json | ok | 2025-12-31 (295d) | – |
| data-city.json | ok | 2026-09-01 (51d) | 2026-10-22T09:44 (2h) |
| data-mufon.json | ok | – | 2026-10-06T12:22 (16d) |
| data-tsa.json | ok | 2026-10-20 (2d) | 2026-10-21T15:50 (20h) |
| data-us_states.json | ok | – | – |

## Schedules (last 7 days)

| workflow | cron (UTC) | days run/due | ticks run | median delay | last scheduled run | flag |
|---|---|---|---|---|---|---|
| aviation-opensky | 25 * * * * | 8/8 | 35% (scheduler 24%) | 0.1h | 2026-10-22T07:44 success; catch-up 2026-10-22T10:09 success | catch-up 25 |
| aviation-tsa | 10 14 * * * | 7/7 | 100% | 2.9h (scheduler 4.0h) | 2026-10-21T18:32 success; catch-up 2026-10-21T15:50 success | catch-up 3 |
| catalog-health | 23 8 * * 1 | 1/1 | 100% | 7.3h | 2026-10-19T15:41 success | ok |
| cfpb-daily | 17 7 * * * | 6/6 | 100% | 4.0h (scheduler 5.5h) | 2026-10-21T12:56 success; catch-up 2026-10-22T09:42 success | catch-up 3 |
| city-daily | 29 7 * * * | 6/6 | 100% | 4.2h (scheduler 5.4h) | 2026-10-21T13:04 success; catch-up 2026-10-22T09:42 success | catch-up 4 |
| codeql | 23 4 * * 0 | 1/1 | 100% | 4.5h | 2026-10-18T08:53 success | ok |
| cron-watchdog | 12 * * * * ; 27 * * * * ; 42 * * * * ; 57 * * * * | 6/6 | 28% | 0.0h | 2026-10-22T10:19 success | ok |
| crypto-odds-daily | 15 14 * * * | 7/7 | 100% | 2.9h (scheduler 4.0h) | 2026-10-21T18:34 success; catch-up 2026-10-21T15:50 success | catch-up 3 |
| daily-audit | 17 9 * * * | 6/6 | 100% | 3.7h (scheduler 5.0h) | 2026-10-21T14:28 success; catch-up 2026-10-21T10:54 success | catch-up 3 |
| data-health | 11 15 * * * | 7/7 | 100% | 2.6h (scheduler 3.8h) | 2026-10-21T19:17 success; catch-up 2026-10-21T17:23 success | catch-up 3 |
| etf-flows-daily | 15 9 * * * | 6/6 | 100% | 3.7h (scheduler 5.1h) | 2026-10-21T14:29 failure; catch-up 2026-10-22T11:44 success; recovered 2026-10-22T11:44 success (workflow_dispatch) | recovered, catch-up 3 |
| lthcs-backtest-daily | 30 23 * * * | 7/7 | 100% | 1.8h | 2026-10-22T01:39 success; catch-up 2026-10-22T01:18 success | catch-up 4 |
| lthcs-backtest-monthly | 19 6 1 * * | 0/0 | – | – | 2026-10-01T11:52 success | ok |
| lthcs-beta-verdict-monthly | 37 8 1 * * | 0/0 | – | – | 2026-10-01T15:12 failure; recovered 2026-10-16T21:48 success (workflow_dispatch) | recovered |
| lthcs-crypto-daily | 47 11 * * * | 7/7 | 100% | 2.7h (scheduler 4.0h) | 2026-10-21T16:07 success; catch-up 2026-10-21T13:43 success | catch-up 3 |
| lthcs-daily | 11 23 * * * | 7/7 | 100% | 1.8h | 2026-10-22T01:28 success; catch-up 2026-10-22T01:18 success | catch-up 4 |
| lthcs-news-hourly | 19 * * * * | 8/8 | 34% (scheduler 23%) | 0.2h | 2026-10-22T10:16 success; catch-up 2026-10-22T10:09 cancelled | catch-up 21 |
| lthcs-quality-audit-monthly | 43 9 1 * * | 0/0 | – | – | 2026-10-01T15:53 failure; recovered 2026-10-17T12:56 success (workflow_dispatch) | recovered |
| lthcs-trends-daily | 13 4 * * * | 6/6 | 100% | 4.3h (scheduler 5.1h) | 2026-10-22T09:21 success; catch-up 2026-10-22T06:53 success | catch-up 3 |
| lthcs-trends-weekly | 47 2 * * 1 | 1/1 | 100% | 5.3h | 2026-10-19T08:05 success | ok |
| lthcs-tune-weights-monthly | 41 7 1 * * | 0/0 | – | – | 2026-10-01T14:10 success | ok |
| lthcs-validate-weekly | 11 5 * * 1 | 1/1 | 100% | 5.5h | 2026-10-19T10:40 success | ok |
| money-flow-daily | 30 8 * * * | 6/6 | 100% | 3.6h (scheduler 5.3h) | 2026-10-21T14:01 success; catch-up 2026-10-22T10:09 success | catch-up 4 |
| pages | 7 * * * * | 8/8 | 34% (scheduler 23%) | 0.2h | 2026-10-22T10:02 success; catch-up 2026-10-22T09:42 success | catch-up 16 |
| real-estate-daily | 13 6 * * * | 6/6 | 100% | 4.2h (scheduler 5.2h) | 2026-10-22T11:43 success; catch-up 2026-10-22T07:45 success | catch-up 3 |
| secrets-check | 23 15 * * 1 | 1/1 | 100% | 5.2h | 2026-10-19T20:38 success | ok |
| security-audit | 31 9 * * 1 | 1/1 | 100% | 7.1h | 2026-10-19T16:36 success | ok |
| trufflehog-weekly | 37 3 * * 0 | 1/1 | 100% | 4.7h | 2026-10-18T08:18 success | ok |
| usaspending-daily | 47 7 * * * | 6/6 | 100% | 3.9h (scheduler 5.2h) | 2026-10-21T13:15 success; catch-up 2026-10-22T09:42 success | catch-up 4 |

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

UX audit took 46s.
