# Daily audit 2026-10-24 — GREEN

**GREEN** · 0 P0 · 0 P1 · 4 P2 · 2 new · 1 fixed (vs. 2026-10-23)

Generated 2026-10-24T13:21:59Z by [daily-audit](https://github.com/btabiado/alpine-data/actions/runs/40097646560). Machine-readable: `audit/daily/2026-10-24.json`.

8 jobs started late; 4 jobs skipped schedule ticks; watchdog caught up; data fresh

## New since yesterday (2)
- **P2** [api] Upstream APIs degraded (2): BLS, IMF SDMX

## Ongoing (2)
- **P2** [api] Upstream APIs blocked (1): Farside (ETF flows) _(since 2026-10-06)_
- **P2** [api] Upstream APIs degraded (1): DeFiLlama bridges _(since 2026-10-05)_

## Fixed since yesterday (1)
- **P1** [api] Upstream API USGS ScienceBase is DOWN

## Snapshot

- **LTHCS quality audit** (data/lthcs/quality_audit/latest_summary.json): DEGRADED. thesis_integrity is KNOWN GAP (accepted) (decided 2026-10-17, review by 2027-04-17)
- **Feeds** (data_health, committed): 2 disclosed, 32 ok, 15 skipped, 1 suppressed
  - muted: data-mufon.json (muted until 2027-01-04: NUFORC permitted aggregated totals only. Raw …)
- **Workflows** (last 24h, main): 20 ran, 0 ended failed
- **API status** (2026-10-24T13:04:34+00:00): 2 auth_required, 1 blocked, 3 degraded, 1 retired, 47 up
  - changed vs. git:d8508f9cfd (2026-10-23T12:43:53+00:00): BLS: up → degraded; IMF SDMX: up → degraded; USGS ScienceBase: down → up
  - prior probe git:89c51adb87 (2026-10-24T09:12:56+00:00): BLS: up → degraded (0 consecutive probe failures); previous probe had 0

**Spot checks** (live site vs. primary sources)

| check | status | detail |
|---|---|---|
| live_site_build_age | ok | built 2026-10-24T13:03:38Z (16m ago) |
| btc_price_vs_coingecko | ok | site 80743.1 vs 80751.63395406138 (0.011%), payload 17m old, source CoinGecko market_chart days=1 (nearest to… |
| eth_price_vs_coingecko | ok | site 2369.21 vs 2368.9314199227397 (0.012%), payload 17m old, source CoinGecko market_chart days=1 (nearest t… |
| fear_greed_vs_alternative_me | ok | 2026-10-24: site 51 vs 51 |
| btc_tip_height_vs_mempool_space | ok | site 972920 vs 972921 (lag 1 blocks) |
| treasury_10y_vs_treasury_gov | ok | 2026-10-22: site 4.96 vs 4.96 |

**Live payload stamps**: 10 of 15 stamped within 48h, the rest:

| payload | status | data as_of | generated |
|---|---|---|---|
| data-aviation.json | ok | 2025-12-31 (298d) | – |
| data-city.json | ok | 2026-09-01 (54d) | 2026-10-24T12:12 (1h) |
| data-mufon.json | ok | – | 2026-10-06T12:22 (18d) |
| data-tsa.json | ok | 2026-10-22 (3d) | 2026-10-23T15:58 (21h) |
| data-us_states.json | ok | – | – |

## Schedules (last 7 days)

| workflow | cron (UTC) | days run/due | ticks run | median delay | last scheduled run | flag |
|---|---|---|---|---|---|---|
| aviation-opensky | 25 * * * * | 8/8 | 41% (scheduler 23%) | 0.2h | 2026-10-24T11:16 success; catch-up 2026-10-24T09:10 success | catch-up 41 |
| aviation-tsa | 10 14 * * * | 7/7 | 100% | 1.8h (scheduler 4.2h) | 2026-10-23T18:14 success; catch-up 2026-10-23T15:57 success | catch-up 5 |
| catalog-health | 23 8 * * 1 | 1/1 | 100% | 7.3h | 2026-10-19T15:41 success | ok |
| cfpb-daily | 17 7 * * * | 6/6 | 100% | 2.4h (scheduler 5.7h) | 2026-10-24T12:01 success; catch-up 2026-10-24T09:10 success | catch-up 5 |
| city-daily | 29 7 * * * | 6/6 | 100% | 2.5h (scheduler 5.6h) | 2026-10-24T12:08 success; catch-up 2026-10-24T09:10 success | catch-up 6 |
| codeql | 23 4 * * 0 | 1/1 | 100% | 4.5h | 2026-10-18T08:53 success | ok |
| cron-watchdog | 12 * * * * ; 27 * * * * ; 42 * * * * ; 57 * * * * | 8/8 | 30% | 0.0h | 2026-10-24T11:44 success | ok |
| crypto-odds-daily | 15 14 * * * | 7/7 | 100% | 1.9h (scheduler 4.2h) | 2026-10-23T18:16 success; catch-up 2026-10-23T15:57 success | catch-up 5 |
| daily-audit | 17 9 * * * | 6/6 | 100% | 2.4h (scheduler 5.2h) | 2026-10-23T14:14 success; catch-up 2026-10-24T10:51 success | catch-up 5 |
| data-health | 11 15 * * * | 7/7 | 100% | 2.6h (scheduler 4.0h) | 2026-10-23T19:12 success; catch-up 2026-10-23T18:15 success | catch-up 5 |
| etf-flows-daily | 15 9 * * * | 6/6 | 100% | 2.5h (scheduler 5.2h) | 2026-10-23T14:15 success; catch-up 2026-10-24T10:51 success | catch-up 5 |
| lthcs-backtest-daily | 30 23 * * * | 7/7 | 100% | 1.8h | 2026-10-24T01:44 success; catch-up 2026-10-24T01:07 success | catch-up 6 |
| lthcs-backtest-monthly | 19 6 1 * * | 0/0 | – | – | 2026-10-01T11:52 success | ok |
| lthcs-beta-verdict-monthly | 37 8 1 * * | 0/0 | – | – | 2026-10-01T15:12 failure; recovered 2026-10-16T21:48 success (workflow_dispatch) | recovered |
| lthcs-crypto-daily | 47 11 * * * | 6/6 | 100% | 1.9h (scheduler 4.3h) | 2026-10-23T15:56 success; catch-up 2026-10-23T13:21 success | catch-up 5 |
| lthcs-daily | 11 23 * * * | 7/7 | 100% | 2.0h | 2026-10-24T01:33 success; catch-up 2026-10-24T00:42 success | catch-up 6 |
| lthcs-news-hourly | 19 * * * * | 8/8 | 40% (scheduler 23%) | 0.2h | 2026-10-24T12:44 success; catch-up 2026-10-24T10:50 success | catch-up 36 |
| lthcs-quality-audit-monthly | 43 9 1 * * | 0/0 | – | – | 2026-10-01T15:53 failure; recovered 2026-10-17T12:56 success (workflow_dispatch) | recovered |
| lthcs-trends-daily | 13 4 * * * | 6/6 | 100% | 2.7h (scheduler 5.2h) | 2026-10-24T08:49 success; catch-up 2026-10-24T05:44 success | catch-up 5 |
| lthcs-trends-weekly | 47 2 * * 1 | 1/1 | 100% | 5.3h | 2026-10-19T08:05 success | ok |
| lthcs-tune-weights-monthly | 41 7 1 * * | 0/0 | – | – | 2026-10-01T14:10 success | ok |
| lthcs-validate-weekly | 11 5 * * 1 | 1/1 | 100% | 5.5h | 2026-10-19T10:40 success | ok |
| money-flow-daily | 30 8 * * * | 6/6 | 100% | 1.7h (scheduler 5.5h) | 2026-10-24T12:51 success; catch-up 2026-10-24T10:51 success | catch-up 6 |
| pages | 7 * * * * | 8/8 | 39% (scheduler 23%) | 0.2h | 2026-10-24T10:48 success; catch-up 2026-10-24T13:02 success | catch-up 26 |
| real-estate-daily | 13 6 * * * | 6/6 | 100% | 3.4h (scheduler 5.5h) | 2026-10-24T10:51 success; catch-up 2026-10-24T09:10 success | catch-up 5 |
| secrets-check | 23 15 * * 1 | 1/1 | 100% | 5.2h | 2026-10-19T20:38 success | ok |
| security-audit | 31 9 * * 1 | 1/1 | 100% | 7.1h | 2026-10-19T16:36 success | ok |
| trufflehog-weekly | 37 3 * * 0 | 1/1 | 100% | 4.7h | 2026-10-18T08:18 success | ok |
| usaspending-daily | 47 7 * * * | 6/6 | 100% | 2.4h (scheduler 5.4h) | 2026-10-24T12:14 success; catch-up 2026-10-24T10:50 success | catch-up 6 |

A catch-up is a `workflow_dispatch` by `github-actions[bot]` (the cron watchdog), not a GitHub schedule event. Ticks run and median delay count it toward freshness; the scheduler figure is what the schedule event delivered on its own.

## UX (live site)

| page | viewport | HTTP | weight / requests | JS exceptions | console errors | failed requests | overflow | tabs tappable |
|---|---|---|---|---|---|---|---|---|
| / | phone | 200 | 1.9 MB / 27 | 0 | 0 | 0 | no | 22/22 |
| / | desktop | 200 | 1.9 MB / 27 | 0 | 0 | 0 | no | 22/22 |
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
