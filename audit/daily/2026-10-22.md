# Daily audit 2026-10-22 — GREEN

**GREEN** · 0 P0 · 0 P1 · 20 P2 · 0 new · 6 fixed (vs. 2026-10-21)

Generated 2026-10-22T14:32:50Z by [daily-audit](https://github.com/btabiado/alpine-data/actions/runs/39796272064). Machine-readable: `audit/daily/2026-10-22.json`.

## New since yesterday (0)
- none

## Ongoing (20)
- **P2** [api] Upstream APIs blocked (1): Farside (ETF flows) _(since 2026-10-06)_
- **P2** [api] Upstream APIs degraded (1): DeFiLlama bridges _(since 2026-10-05)_
- **P2** [schedule] Crons that lose most of their ticks (4): aviation-opensky 35% (26 catch-up, 102 missed), cron-watchdog 27% (0 catch-up, 306 missed), lthcs-news-hourly 34% (22 catch-up, 104 missed), pages 34% (16 catch-up, 105 missed) _(since 2026-10-05)_
- **P2** [schedule] Crons starting >3h late (median delay) (14): catalog-health 7.3h, cfpb-daily 4.0h, city-daily 4.2h, codeql 4.5h, daily-audit 3.7h, etf-flows-daily 3.7h, lthcs-trends-weekly 5.3h, lthcs-validate-weekly 5.5h, money-flow-daily 3.6h, real-estate-daily 4.2h, secrets-check 5.2h, security-audit 7.1h, trufflehog-weekly 4.7h, usaspending-daily 3.9h _(since 2026-10-20)_

## Fixed since yesterday (6)
- **P1** [api] Upstream API FRED ENPLANE (air travel) is DOWN
- **P1** [workflow] Workflow etf-flows-daily failed in the last 24h — step 'Fetch BTC + ETH spot-ETF flows' — ETF flows failed (eth): fetch: Farside returned HTTP 521 <none> for https://farside.co.uk/ethereum-etf-flow-all-data/.; leaving eth_flows.csv untouched ([run](https://github.com/btabiado/alpine-data/actions/runs/39588030660))
- **P2** [schedule] Crons starting >3h late (median delay) (4): aviation-tsa 3.1h, crypto-odds-daily 3.1h, lthcs-crypto-daily 3.1h, lthcs-trends-daily 4.3h

## Snapshot

- **LTHCS quality audit** (data/lthcs/quality_audit/latest_summary.json): DEGRADED. thesis_integrity is KNOWN GAP (accepted) (decided 2026-10-17, review by 2027-04-17)
- **Feeds** (data_health, committed): 2 disclosed, 32 ok, 15 skipped, 1 suppressed
  - muted: data-mufon.json (muted until 2027-01-04: NUFORC permitted aggregated totals only. Raw …)
- **Workflows** (last 24h, main): 18 ran, 0 ended failed
- **API status** (2026-10-22T09:45:39+00:00): 2 auth_required, 1 blocked, 1 degraded, 1 retired, 49 up
  - changed vs. git:a800a9cc23 (2026-10-21T13:01:00+00:00): FRED ENPLANE (air travel): down → up

**Spot checks** (live site vs. primary sources)

| check | status | detail |
|---|---|---|
| live_site_build_age | ok | built 2026-10-22T12:45:47Z (2h ago) |
| btc_price_vs_coingecko | ok | site 81651.58 vs 81670.53643087615 (0.023%), payload 2h old, source CoinGecko market_chart days=1 (nearest to… |
| eth_price_vs_coingecko | ok | site 2399.6 vs 2400.3298310452997 (0.03%), payload 2h old, source CoinGecko market_chart days=1 (nearest to p… |
| fear_greed_vs_alternative_me | ok | 2026-10-22: site 68 vs 68 |
| btc_tip_height_vs_mempool_space | ok | site 972616 vs 972623 (lag 7 blocks) |
| treasury_10y_vs_treasury_gov | ok | 2026-10-20: site 5.12 vs 5.12 |

**Live payload stamps**: 10 of 15 stamped within 48h, the rest:

| payload | status | data as_of | generated |
|---|---|---|---|
| data-aviation.json | ok | 2025-12-31 (296d) | – |
| data-city.json | ok | 2026-09-01 (52d) | 2026-10-22T09:44 (5h) |
| data-mufon.json | ok | – | 2026-10-06T12:22 (16d) |
| data-tsa.json | ok | 2026-10-20 (3d) | 2026-10-21T15:50 (23h) |
| data-us_states.json | ok | – | – |

## Schedules (last 7 days)

| workflow | cron (UTC) | days run/due | ticks run | median delay | last scheduled run | flag |
|---|---|---|---|---|---|---|
| aviation-opensky | 25 * * * * | 8/8 | 35% (scheduler 24%) | 0.1h | 2026-10-22T13:54 success; catch-up 2026-10-22T12:44 success | catch-up 26 |
| aviation-tsa | 10 14 * * * | 6/6 | 100% | 2.9h (scheduler 4.2h) | 2026-10-21T18:32 success; catch-up 2026-10-21T15:50 success | catch-up 3 |
| catalog-health | 23 8 * * 1 | 1/1 | 100% | 7.3h | 2026-10-19T15:41 success | ok |
| cfpb-daily | 17 7 * * * | 6/6 | 100% | 4.0h (scheduler 5.5h) | 2026-10-22T12:59 success; catch-up 2026-10-22T09:42 success | catch-up 3 |
| city-daily | 29 7 * * * | 6/6 | 100% | 4.2h (scheduler 5.4h) | 2026-10-22T13:05 success; catch-up 2026-10-22T09:42 success | catch-up 4 |
| codeql | 23 4 * * 0 | 1/1 | 100% | 4.5h | 2026-10-18T08:53 success | ok |
| cron-watchdog | 12 * * * * ; 27 * * * * ; 42 * * * * ; 57 * * * * | 6/6 | 27% | 0.0h | 2026-10-22T14:05 success | ok |
| crypto-odds-daily | 15 14 * * * | 6/6 | 100% | 2.9h (scheduler 4.2h) | 2026-10-21T18:34 success; catch-up 2026-10-21T15:50 success | catch-up 3 |
| daily-audit | 17 9 * * * | 6/6 | 100% | 3.7h (scheduler 5.0h) | 2026-10-21T14:28 success; catch-up 2026-10-22T11:44 success | catch-up 3 |
| data-health | 11 15 * * * | 7/7 | 100% | 2.6h (scheduler 3.8h) | 2026-10-21T19:17 success; catch-up 2026-10-21T17:23 success | catch-up 3 |
| etf-flows-daily | 15 9 * * * | 6/6 | 100% | 3.7h (scheduler 5.1h) | 2026-10-21T14:29 failure; catch-up 2026-10-22T11:44 success; recovered 2026-10-22T11:44 success (workflow_dispatch) | recovered, catch-up 3 |
| lthcs-backtest-daily | 30 23 * * * | 7/7 | 100% | 1.8h | 2026-10-22T01:39 success; catch-up 2026-10-22T01:18 success | catch-up 4 |
| lthcs-backtest-monthly | 19 6 1 * * | 0/0 | – | – | 2026-10-01T11:52 success | ok |
| lthcs-beta-verdict-monthly | 37 8 1 * * | 0/0 | – | – | 2026-10-01T15:12 failure; recovered 2026-10-16T21:48 success (workflow_dispatch) | recovered |
| lthcs-crypto-daily | 47 11 * * * | 6/6 | 100% | 2.7h (scheduler 4.1h) | 2026-10-21T16:07 success; catch-up 2026-10-22T13:44 success | catch-up 3 |
| lthcs-daily | 11 23 * * * | 7/7 | 100% | 1.8h | 2026-10-22T01:28 success; catch-up 2026-10-22T01:18 success | catch-up 4 |
| lthcs-news-hourly | 19 * * * * | 8/8 | 34% (scheduler 23%) | 0.2h | 2026-10-22T10:16 success; catch-up 2026-10-22T12:44 success | catch-up 22 |
| lthcs-quality-audit-monthly | 43 9 1 * * | 0/0 | – | – | 2026-10-01T15:53 failure; recovered 2026-10-17T12:56 success (workflow_dispatch) | recovered |
| lthcs-trends-daily | 13 4 * * * | 7/7 | 100% | 2.7h (scheduler 5.1h) | 2026-10-22T09:21 success; catch-up 2026-10-22T06:53 success | catch-up 4 |
| lthcs-trends-weekly | 47 2 * * 1 | 1/1 | 100% | 5.3h | 2026-10-19T08:05 success | ok |
| lthcs-tune-weights-monthly | 41 7 1 * * | 0/0 | – | – | 2026-10-01T14:10 success | ok |
| lthcs-validate-weekly | 11 5 * * 1 | 1/1 | 100% | 5.5h | 2026-10-19T10:40 success | ok |
| money-flow-daily | 30 8 * * * | 6/6 | 100% | 3.6h (scheduler 5.3h) | 2026-10-22T13:59 success; catch-up 2026-10-22T10:09 success | catch-up 4 |
| pages | 7 * * * * | 8/8 | 34% (scheduler 23%) | 0.2h | 2026-10-22T10:02 success; catch-up 2026-10-22T12:44 success | catch-up 16 |
| real-estate-daily | 13 6 * * * | 6/6 | 100% | 4.2h (scheduler 5.2h) | 2026-10-22T11:43 success; catch-up 2026-10-22T07:45 success | catch-up 3 |
| secrets-check | 23 15 * * 1 | 1/1 | 100% | 5.2h | 2026-10-19T20:38 success | ok |
| security-audit | 31 9 * * 1 | 1/1 | 100% | 7.1h | 2026-10-19T16:36 success | ok |
| trufflehog-weekly | 37 3 * * 0 | 1/1 | 100% | 4.7h | 2026-10-18T08:18 success | ok |
| usaspending-daily | 47 7 * * * | 6/6 | 100% | 3.9h (scheduler 5.2h) | 2026-10-22T13:13 success; catch-up 2026-10-22T09:42 success | catch-up 4 |

A catch-up is a `workflow_dispatch` by `github-actions[bot]` (the cron watchdog), not a GitHub schedule event. Ticks run and median delay count it toward freshness; the scheduler figure is what the schedule event delivered on its own.

## UX (live site)

| page | viewport | HTTP | weight / requests | JS exceptions | console errors | failed requests | overflow | tabs tappable |
|---|---|---|---|---|---|---|---|---|
| / | phone | 200 | 1.9 MB / 30 | 0 | 0 | 0 | no | 22/22 |
| / | desktop | 200 | 1.9 MB / 30 | 0 | 0 | 0 | no | 22/22 |
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

UX audit took 36s.
