# Daily audit 2026-10-29 — GREEN

**GREEN** · 0 P0 · 0 P1 · 3 P2 · 1 new · 2 fixed (vs. 2026-10-28)

Generated 2026-10-29T16:10:09Z by [daily-audit](https://github.com/btabiado/alpine-data/actions/runs/40908375095). Machine-readable: `audit/daily/2026-10-29.json`.

7 jobs started late; 4 jobs skipped schedule ticks; watchdog caught up; data fresh

## New since yesterday (1)
- **P2** [feed] City scored month is older than a normal monthly lag (1): nyc data_health.last_updated 2026-06-01 is 150.7d old (warn after 120.0d). A monthly series that lags about two months stays under this; this is a warning, not a failure.

## Ongoing (2)
- **P2** [api] Upstream APIs blocked (1): Farside (ETF flows) _(since 2026-10-06)_
- **P2** [api] Upstream APIs degraded (1): DeFiLlama bridges _(since 2026-10-05)_

## Fixed since yesterday (2)
- **P1** [workflow] Workflow real-estate-daily failed in the last 24h — step 'Fetch real estate data' — Real estate snapshot rejected: redfin contributed 0 rows; redfin rows collapsed from 878 to 0 ([run](https://github.com/btabiado/alpine-data/actions/runs/40690172315))
- **P2** [api] Upstream APIs blocked (1): Redfin Data Center

## Snapshot

- **LTHCS quality audit** (data/lthcs/quality_audit/latest_summary.json): DEGRADED. thesis_integrity is KNOWN GAP (accepted) (decided 2026-10-17, review by 2027-04-17)
- **Feeds** (data_health, committed): 1 aged, 1 disclosed, 33 ok, 15 skipped, 1 suppressed
  - muted: data-mufon.json (muted until 2027-01-04: NUFORC permitted aggregated totals only. Raw …)
- **Workflows** (last 24h, main): 20 ran, 0 ended failed
- **API status** (2026-10-29T14:10:46+00:00): 2 auth_required, 1 blocked, 1 degraded, 1 retired, 49 up
  - changed vs. git:f30c09607a (2026-10-28T16:06:59+00:00): Redfin Data Center: blocked → up; Santiment: rate_limited → up

**Spot checks** (live site vs. primary sources)

| check | status | detail |
|---|---|---|
| live_site_build_age | ok | built 2026-10-29T14:09:42Z (2h ago) |
| btc_price_vs_coingecko | ok | site 80221.59 vs 80235.1368829105 (0.017%), payload 2h old, source CoinGecko market_chart days=1 (nearest to … |
| eth_price_vs_coingecko | ok | site 2322.91 vs 2323.0699697942555 (0.007%), payload 2h old, source CoinGecko market_chart days=1 (nearest to… |
| fear_greed_vs_alternative_me | ok | 2026-10-29: site 51 vs 51 |
| btc_tip_height_vs_mempool_space | ok | site 973693 vs 973708 (lag 15 blocks) |
| treasury_10y_vs_treasury_gov | ok | 2026-10-27: site 4.99 vs 4.99 |

**Live payload stamps**: 10 of 15 stamped within 48h, the rest:

| payload | status | data as_of | generated |
|---|---|---|---|
| data-aviation.json | ok | 2025-12-31 (303d) | – |
| data-city.json | ok | 2026-09-01 (59d) | 2026-10-29T10:17 (6h) |
| data-mufon.json | ok | – | 2026-10-06T12:22 (23d) |
| data-tsa.json | ok | 2026-10-27 (3d) | 2026-10-28T15:58 (24h) |
| data-us_states.json | ok | – | – |

## Schedules (last 7 days)

| workflow | cron (UTC) | days run/due | ticks run | median delay | last scheduled run | flag |
|---|---|---|---|---|---|---|
| aviation-opensky | 25 * * * * | 8/8 | 44% (scheduler 22%) | 0.2h | 2026-10-29T13:20 success; catch-up 2026-10-29T15:27 success | catch-up 45 |
| aviation-tsa | 10 14 * * * | 6/6 | 100% | 1.9h (scheduler 5.2h) | 2026-10-28T19:27 success; catch-up 2026-10-28T15:57 success | catch-up 6 |
| catalog-health | 23 8 * * 1 | 1/1 | 100% | 6.9h | 2026-10-26T15:15 success | ok |
| cfpb-daily | 17 7 * * * | 6/6 | 100% | 2.4h (scheduler 5.9h) | 2026-10-29T14:22 success; catch-up 2026-10-29T08:54 success | catch-up 6 |
| city-daily | 29 7 * * * | 6/6 | 100% | 2.2h (scheduler 6.0h) | 2026-10-29T14:34 success; catch-up 2026-10-29T10:12 success | catch-up 6 |
| codeql | 23 4 * * 0 | 1/1 | 100% | 4.8h | 2026-10-25T09:12 success | ok |
| cron-watchdog | 12 * * * * ; 27 * * * * ; 42 * * * * ; 57 * * * * | 8/8 | 36% | 0.0h | 2026-10-29T14:39 success | ok |
| crypto-odds-daily | 15 14 * * * | 6/6 | 100% | 1.9h (scheduler 5.2h) | 2026-10-28T19:31 success; catch-up 2026-10-28T15:57 success | catch-up 6 |
| daily-audit | 17 9 * * * | 6/6 | 100% | 2.0h (scheduler 6.1h) | 2026-10-28T16:01 success; catch-up 2026-10-29T11:11 success | catch-up 6 |
| data-health | 11 15 * * * | 6/6 | 100% | 1.8h (scheduler 4.9h) | 2026-10-28T20:15 success; catch-up 2026-10-28T17:00 success | catch-up 6 |
| etf-flows-daily | 15 9 * * * | 6/6 | 100% | 2.0h (scheduler 6.1h) | 2026-10-28T16:05 success; catch-up 2026-10-29T11:11 success | catch-up 6 |
| lthcs-backtest-daily | 30 23 * * * | 7/7 | 100% | 1.8h (scheduler 2.2h) | 2026-10-29T02:34 success; catch-up 2026-10-29T01:14 success | catch-up 7 |
| lthcs-backtest-monthly | 19 6 1 * * | 0/0 | – | – | 2026-10-01T11:52 success | ok |
| lthcs-beta-verdict-monthly | 37 8 1 * * | 0/0 | – | – | 2026-10-01T15:12 failure; recovered 2026-10-16T21:48 success (workflow_dispatch) | recovered |
| lthcs-crypto-daily | 47 11 * * * | 6/6 | 100% | 1.6h (scheduler 5.1h) | 2026-10-28T17:26 success; catch-up 2026-10-29T13:21 success | catch-up 6 |
| lthcs-daily | 11 23 * * * | 7/7 | 100% | 1.7h (scheduler 2.4h) | 2026-10-29T02:24 success; catch-up 2026-10-29T00:50 success | catch-up 6 |
| lthcs-news-hourly | 19 * * * * | 8/8 | 42% (scheduler 21%) | 0.2h | 2026-10-29T08:36 success; catch-up 2026-10-29T14:10 success | catch-up 38 |
| lthcs-quality-audit-monthly | 43 9 1 * * | 0/0 | – | – | 2026-10-01T15:53 failure; recovered 2026-10-17T12:56 success (workflow_dispatch) | recovered |
| lthcs-trends-daily | 13 4 * * * | 7/7 | 100% | 1.8h (scheduler 5.5h) | 2026-10-29T10:50 success; catch-up 2026-10-29T06:02 success | catch-up 7 |
| lthcs-trends-weekly | 47 2 * * 1 | 1/1 | 100% | 5.3h | 2026-10-26T08:08 success | ok |
| lthcs-tune-weights-monthly | 41 7 1 * * | 0/0 | – | – | 2026-10-01T14:10 success | ok |
| lthcs-validate-weekly | 11 5 * * 1 | 1/1 | 100% | 5.8h | 2026-10-26T10:58 success | ok |
| money-flow-daily | 30 8 * * * | 6/6 | 100% | 2.0h (scheduler 6.3h) | 2026-10-29T15:25 success; catch-up 2026-10-29T10:13 success | catch-up 6 |
| pages | 7 * * * * | 8/8 | 40% (scheduler 22%) | 0.1h | 2026-10-29T14:08 success; catch-up 2026-10-29T12:54 success | catch-up 31 |
| real-estate-daily | 13 6 * * * | 6/6 | 100% | 1.9h (scheduler 5.8h) | 2026-10-29T12:53 success; catch-up 2026-10-29T08:54 success | catch-up 6 |
| secrets-check | 23 15 * * 1 | 1/1 | 100% | 5.2h | 2026-10-26T20:35 success | ok |
| security-audit | 31 9 * * 1 | 1/1 | 100% | 6.8h | 2026-10-26T16:16 success | ok |
| trufflehog-weekly | 37 3 * * 0 | 1/1 | 100% | 5.1h | 2026-10-25T08:42 success | ok |
| usaspending-daily | 47 7 * * * | 6/6 | 100% | 2.4h (scheduler 5.8h) | 2026-10-29T14:40 success; catch-up 2026-10-29T10:12 success | catch-up 6 |

A catch-up is a `workflow_dispatch` by `github-actions[bot]` (the cron watchdog), not a GitHub schedule event. Ticks run and median delay count it toward freshness; the scheduler figure is what the schedule event delivered on its own.

## UX (live site)

| page | viewport | HTTP | weight / requests | JS exceptions | console errors | failed requests | overflow | tabs tappable |
|---|---|---|---|---|---|---|---|---|
| / | phone | 200 | 1.9 MB / 30 | 0 | 0 | 0 | no | 22/22 |
| / | desktop | 200 | 1.9 MB / 30 | 0 | 0 | 0 | no | 22/22 |
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

UX audit took 78s.
