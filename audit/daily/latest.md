# Daily audit 2026-10-25 — AMBER

**AMBER** · 0 P0 · 3 P1 · 4 P2 · 4 new · 1 fixed (vs. 2026-10-24)

Generated 2026-10-25T13:45:13Z by [daily-audit](https://github.com/btabiado/alpine-data/actions/runs/40198851190). Machine-readable: `audit/daily/2026-10-25.json`.

6 jobs started late; 4 jobs skipped schedule ticks; watchdog caught up; data fresh

## New since yesterday (4)
- **P1** [ux] V1 (/): visible "Infinity" in #newsFeed — “rs Have Pushed to Cut Discs Since 2007, Infinity Ward Veteran Says” — during tab:trading
- **P1** [ux] V1 (/): visible "Infinity" in #overviewNewsHost — “rs Have Pushed to Cut Discs Since 2007, Infinity Ward Veteran Says” — during load
- **P1** [ux] V1 (/): visible "Infinity" in #researchNewsHost — “rs Have Pushed to Cut Discs Since 2007, Infinity Ward Veteran Says” — during tab:social
- **P2** [feed] Feed data-tsa.json is an upstream lag (3d old, limit 2d); the source has not published a newer row — upstream lag: source newest date 2026-10-22 equals this file's as_of, checked 20.3h ago (2026-10-24T17:22:01Z). TSA has not published a newer checkpoint. Disclosed under 7d; a missed check or an olde…

## Ongoing (3)
- **P2** [api] Upstream APIs blocked (1): Farside (ETF flows) _(since 2026-10-06)_
- **P2** [api] Upstream APIs degraded (2): BLS, DeFiLlama bridges _(since 2026-10-24)_

## Fixed since yesterday (1)
- **P2** [api] Upstream APIs degraded (1): IMF SDMX

## Snapshot

- **LTHCS quality audit** (data/lthcs/quality_audit/latest_summary.json): DEGRADED. thesis_integrity is KNOWN GAP (accepted) (decided 2026-10-17, review by 2027-04-17)
- **Feeds** (data_health, committed): 3 disclosed, 31 ok, 15 skipped, 1 suppressed
  - muted: data-mufon.json (muted until 2027-01-04: NUFORC permitted aggregated totals only. Raw …)
- **Workflows** (last 24h, main): 21 ran, 0 ended failed
- **API status** (2026-10-25T13:31:48+00:00): 2 auth_required, 1 blocked, 2 degraded, 1 retired, 48 up
  - changed vs. git:2167498ae1 (2026-10-24T13:04:34+00:00): IMF SDMX: degraded → up

**Spot checks** (live site vs. primary sources)

| check | status | detail |
|---|---|---|
| live_site_build_age | ok | built 2026-10-25T13:30:07Z (13m ago) |
| btc_price_vs_coingecko | ok | site 80152.71 vs 80209.23307739852 (0.07%), payload 14m old, source CoinGecko market_chart days=1 (nearest to… |
| eth_price_vs_coingecko | ok | site 2325.6 vs 2326.874355149351 (0.055%), payload 14m old, source CoinGecko market_chart days=1 (nearest to … |
| fear_greed_vs_alternative_me | ok | 2026-10-25: site 54 vs 54 |
| btc_tip_height_vs_mempool_space | ok | site 973075 vs 973076 (lag 1 blocks) |
| treasury_10y_vs_treasury_gov | ok | 2026-10-22: site 4.96 vs 4.96 |

**Live payload stamps**: 9 of 15 stamped within 48h, the rest:

| payload | status | data as_of | generated |
|---|---|---|---|
| data-aviation.json | ok | 2025-12-31 (299d) | – |
| data-city.json | ok | 2026-09-01 (55d) | 2026-10-25T12:47 (56m) |
| data-mufon.json | ok | – | 2026-10-06T12:22 (19d) |
| data-stock-money-flow.json | ok | 2026-10-23 (3d) | – |
| data-tsa.json | ok | 2026-10-22 (4d) | 2026-10-23T15:58 (46h) |
| data-us_states.json | ok | – | – |

## Schedules (last 7 days)

| workflow | cron (UTC) | days run/due | ticks run | median delay | last scheduled run | flag |
|---|---|---|---|---|---|---|
| aviation-opensky | 25 * * * * | 8/8 | 44% (scheduler 23%) | 0.2h | 2026-10-25T11:46 success; catch-up 2026-10-25T09:27 success | catch-up 46 |
| aviation-tsa | 10 14 * * * | 7/7 | 100% | 1.8h (scheduler 4.2h) | 2026-10-24T17:21 success; catch-up 2026-10-24T16:57 success | catch-up 6 |
| catalog-health | 23 8 * * 1 | 1/1 | 100% | 7.3h | 2026-10-19T15:41 success | ok |
| cfpb-daily | 17 7 * * * | 6/6 | 100% | 2.4h (scheduler 5.7h) | 2026-10-25T12:34 success; catch-up 2026-10-25T09:27 success | catch-up 6 |
| city-daily | 29 7 * * * | 6/6 | 100% | 2.2h (scheduler 5.6h) | 2026-10-25T12:41 success; catch-up 2026-10-25T09:27 success | catch-up 6 |
| codeql | 23 4 * * 0 | 0/0 | – | – | 2026-10-25T09:12 success | ok |
| cron-watchdog | 12 * * * * ; 27 * * * * ; 42 * * * * ; 57 * * * * | 8/8 | 33% | 0.0h | 2026-10-25T13:28 success | ok |
| crypto-odds-daily | 15 14 * * * | 7/7 | 100% | 1.9h (scheduler 4.2h) | 2026-10-24T17:24 success; catch-up 2026-10-24T16:57 success | catch-up 6 |
| daily-audit | 17 9 * * * | 6/6 | 100% | 2.3h (scheduler 5.2h) | 2026-10-24T13:19 success; catch-up 2026-10-25T11:16 success | catch-up 6 |
| data-health | 11 15 * * * | 7/7 | 100% | 2.2h (scheduler 4.0h) | 2026-10-24T18:07 success; catch-up 2026-10-24T16:57 success | catch-up 6 |
| etf-flows-daily | 15 9 * * * | 6/6 | 100% | 2.2h (scheduler 5.2h) | 2026-10-24T13:21 success; catch-up 2026-10-25T11:16 success | catch-up 6 |
| lthcs-backtest-daily | 30 23 * * * | 7/7 | 100% | 1.8h | 2026-10-25T01:21 success; catch-up 2026-10-25T01:04 success | catch-up 7 |
| lthcs-backtest-monthly | 19 6 1 * * | 0/0 | – | – | 2026-10-01T11:52 success | ok |
| lthcs-beta-verdict-monthly | 37 8 1 * * | 0/0 | – | – | 2026-10-01T15:12 failure; recovered 2026-10-16T21:48 success (workflow_dispatch) | recovered |
| lthcs-crypto-daily | 47 11 * * * | 6/6 | 100% | 1.9h (scheduler 4.3h) | 2026-10-24T14:54 success; catch-up 2026-10-25T13:18 success | catch-up 6 |
| lthcs-daily | 11 23 * * * | 7/7 | 100% | 2.0h | 2026-10-25T01:07 success; catch-up 2026-10-25T01:04 success | catch-up 7 |
| lthcs-news-hourly | 19 * * * * | 8/8 | 41% (scheduler 23%) | 0.2h | 2026-10-25T11:40 success; catch-up 2026-10-25T09:27 success | catch-up 40 |
| lthcs-quality-audit-monthly | 43 9 1 * * | 0/0 | – | – | 2026-10-01T15:53 failure; recovered 2026-10-17T12:56 success (workflow_dispatch) | recovered |
| lthcs-trends-daily | 13 4 * * * | 6/6 | 100% | 1.9h (scheduler 5.2h) | 2026-10-25T09:06 success; catch-up 2026-10-25T05:59 success | catch-up 6 |
| lthcs-trends-weekly | 47 2 * * 1 | 1/1 | 100% | 5.3h | 2026-10-19T08:05 success | ok |
| lthcs-tune-weights-monthly | 41 7 1 * * | 0/0 | – | – | 2026-10-01T14:10 success | ok |
| lthcs-validate-weekly | 11 5 * * 1 | 1/1 | 100% | 5.5h | 2026-10-19T10:40 success | ok |
| money-flow-daily | 30 8 * * * | 6/6 | 100% | 1.7h (scheduler 5.5h) | 2026-10-25T13:17 success; catch-up 2026-10-25T10:09 success | catch-up 6 |
| pages | 7 * * * * | 8/8 | 42% (scheduler 23%) | 0.3h | 2026-10-25T11:14 success; catch-up 2026-10-25T13:28 success | catch-up 29 |
| real-estate-daily | 13 6 * * * | 6/6 | 100% | 3.0h (scheduler 5.5h) | 2026-10-25T11:17 success; catch-up 2026-10-25T07:58 success | catch-up 6 |
| secrets-check | 23 15 * * 1 | 1/1 | 100% | 5.2h | 2026-10-19T20:38 success | ok |
| security-audit | 31 9 * * 1 | 1/1 | 100% | 7.1h | 2026-10-19T16:36 success | ok |
| trufflehog-weekly | 37 3 * * 0 | 1/1 | 100% | 5.1h | 2026-10-25T08:42 success | ok |
| usaspending-daily | 47 7 * * * | 6/6 | 100% | 2.4h (scheduler 5.4h) | 2026-10-25T12:46 success; catch-up 2026-10-25T10:09 success | catch-up 6 |

A catch-up is a `workflow_dispatch` by `github-actions[bot]` (the cron watchdog), not a GitHub schedule event. Ticks run and median delay count it toward freshness; the scheduler figure is what the schedule event delivered on its own.

## UX (live site)

| page | viewport | HTTP | weight / requests | JS exceptions | console errors | failed requests | overflow | tabs tappable |
|---|---|---|---|---|---|---|---|---|
| / | phone | 200 | 1.9 MB / 28 | 0 | 0 | 0 | no | 22/22 |
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

UX audit took 38s.
