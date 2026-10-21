# Daily audit 2026-10-21 — AMBER

**AMBER** · 0 P0 · 2 P1 · 24 P2 · 2 new · 4 fixed (vs. 2026-10-20)

Generated 2026-10-21T14:30:51Z by [daily-audit](https://github.com/btabiado/alpine-data/actions/runs/39617139612). Machine-readable: `audit/daily/2026-10-21.json`.

## New since yesterday (2)
- **P1** [api] Upstream API FRED ENPLANE (air travel) is DOWN
- **P1** [workflow] Workflow etf-flows-daily failed in the last 24h — step 'Fetch BTC + ETH spot-ETF flows' — ETF flows failed (eth): fetch: Farside returned HTTP 521 <none> for https://farside.co.uk/ethereum-etf-flow-all-data/.; leaving eth_flows.csv untouched ([run](https://github.com/btabiado/alpine-data/actions/runs/39588030660))

## Ongoing (24)
- **P2** [api] Upstream APIs blocked (1): Farside (ETF flows) _(since 2026-10-06)_
- **P2** [api] Upstream APIs degraded (1): DeFiLlama bridges _(since 2026-10-05)_
- **P2** [schedule] Crons that lose most of their ticks (4): aviation-opensky 32% (18 catch-up, 107 missed), cron-watchdog 24% (0 catch-up, 246 missed), lthcs-news-hourly 32% (15 catch-up, 108 missed), pages 30% (10 catch-up, 111 missed) _(since 2026-10-05)_
- **P2** [schedule] Crons starting >3h late (median delay) (18): aviation-tsa 3.1h, catalog-health 7.3h, cfpb-daily 4.8h, city-daily 4.5h, codeql 4.5h, crypto-odds-daily 3.1h, daily-audit 4.0h, etf-flows-daily 4.1h, lthcs-crypto-daily 3.1h, lthcs-trends-daily 4.3h, lthcs-trends-weekly 5.3h, lthcs-validate-weekly 5.5h, money-flow-daily 3.8h, real-estate-daily 4.7h, secrets-check 5.2h, security-audit 7.1h, trufflehog-weekly 4.7h, usaspending-daily 4.3h _(since 2026-10-05)_

## Fixed since yesterday (4)
- **P1** [schedule] Scheduled workflow lthcs-beta-verdict-monthly: last scheduled run failed (2026-10-01) — cron 37 8 1 * * ([run](https://github.com/btabiado/alpine-data/actions/runs/36882479373))
- **P1** [spot] BTC price on the site (81634.01) is 1.769% off CoinGecko (83104.0) — page=81634.01, reference=83104.0, diff_pct=1.769, tolerance_pct=1.0, page_age_h=0.51, source=api.coingecko.com simple/price
- **P1** [spot] ETH price on the site (2395.78) is 2.682% off CoinGecko (2461.81) — page=2395.78, reference=2461.81, diff_pct=2.682, tolerance_pct=1.0, page_age_h=0.51, source=api.coingecko.com simple/price
- **P2** [schedule] Crons starting >3h late (median delay) (1): data-health 3.5h

## Snapshot

- **LTHCS quality audit** (data/lthcs/quality_audit/latest_summary.json): DEGRADED. thesis_integrity is KNOWN GAP (accepted) (decided 2026-10-17, review by 2027-04-17)
- **Feeds** (data_health, committed): 2 disclosed, 32 ok, 15 skipped, 1 suppressed
  - muted: data-mufon.json (muted until 2027-01-04: NUFORC permitted aggregated totals only. Raw …)
- **Workflows** (last 24h, main): 20 ran, 1 ended failed (etf-flows-daily)
- **API status** (2026-10-21T13:01:00+00:00): 2 auth_required, 1 blocked, 1 degraded, 1 down, 1 retired, 48 up
  - changed vs. git:fc7ee7ee41 (2026-10-20T14:24:41+00:00): BLS: down → up; FRED ENPLANE (air travel): up → down

**Spot checks** (live site vs. primary sources)

| check | status | detail |
|---|---|---|
| live_site_build_age | ok | built 2026-10-21T12:59:41Z (1h ago) |
| btc_price_vs_coingecko | ok | site 83397.56 vs 83410.62644600445 (0.016%), payload 2h old, source CoinGecko market_chart days=1 (nearest to… |
| eth_price_vs_coingecko | ok | site 2465.81 vs 2466.2001233994465 (0.016%), payload 2h old, source CoinGecko market_chart days=1 (nearest to… |
| fear_greed_vs_alternative_me | ok | 2026-10-21: site 63 vs 63 |
| btc_tip_height_vs_mempool_space | ok | site 972479 vs 972488 (lag 9 blocks) |
| treasury_10y_vs_treasury_gov | ok | 2026-10-19: site 5.15 vs 5.15 |

**Live payload stamps**: 10 of 15 stamped within 48h, the rest:

| payload | status | data as_of | generated |
|---|---|---|---|
| data-aviation.json | ok | 2025-12-31 (295d) | – |
| data-city.json | ok | 2026-09-01 (51d) | 2026-10-21T09:41 (5h) |
| data-mufon.json | ok | – | 2026-10-06T12:22 (15d) |
| data-tsa.json | ok | 2026-10-19 (3d) | 2026-10-20T15:52 (23h) |
| data-us_states.json | ok | – | – |

## Schedules (last 7 days)

| workflow | cron (UTC) | days run/due | ticks run | median delay | last scheduled run | flag |
|---|---|---|---|---|---|---|
| aviation-opensky | 25 * * * * | 8/8 | 32% (scheduler 24%) | 0.2h | 2026-10-21T10:18 success; catch-up 2026-10-21T12:58 success | catch-up 18 |
| aviation-tsa | 10 14 * * * | 6/6 | 100% | 3.1h (scheduler 4.0h) | 2026-10-20T18:25 success; catch-up 2026-10-20T15:52 success | catch-up 2 |
| catalog-health | 23 8 * * 1 | 1/1 | 100% | 7.3h | 2026-10-19T15:41 success | ok |
| cfpb-daily | 17 7 * * * | 6/6 | 100% | 4.8h | 2026-10-21T12:56 success; catch-up 2026-10-21T09:39 success | catch-up 2 |
| city-daily | 29 7 * * * | 6/6 | 100% | 4.5h | 2026-10-21T13:04 success; catch-up 2026-10-21T09:39 success | catch-up 3 |
| codeql | 23 4 * * 0 | 1/1 | 100% | 4.5h | 2026-10-18T08:53 success | ok |
| cron-watchdog | 12 * * * * ; 27 * * * * ; 42 * * * * ; 57 * * * * | 5/5 | 24% | 0.0h | 2026-10-21T13:42 success | ok |
| crypto-odds-daily | 15 14 * * * | 6/6 | 100% | 3.1h (scheduler 4.0h) | 2026-10-20T18:26 success; catch-up 2026-10-20T15:52 success | catch-up 2 |
| daily-audit | 17 9 * * * | 6/6 | 100% | 4.0h (scheduler 4.9h) | 2026-10-20T14:17 success; catch-up 2026-10-21T10:54 success | catch-up 2 |
| data-health | 11 15 * * * | 7/7 | 100% | 2.9h (scheduler 3.8h) | 2026-10-20T19:15 success; catch-up 2026-10-20T16:56 success | catch-up 2 |
| etf-flows-daily | 15 9 * * * | 6/6 | 100% | 4.1h (scheduler 4.9h) | 2026-10-20T14:19 success; catch-up 2026-10-21T10:54 failure | catch-up 2 |
| lthcs-backtest-daily | 30 23 * * * | 7/7 | 100% | 1.9h | 2026-10-21T01:45 success; catch-up 2026-10-21T01:30 success | catch-up 3 |
| lthcs-backtest-monthly | 19 6 1 * * | 0/0 | – | – | 2026-10-01T11:52 success | ok |
| lthcs-beta-verdict-monthly | 37 8 1 * * | 0/0 | – | – | 2026-10-01T15:12 failure; recovered 2026-10-16T21:48 success (workflow_dispatch) | recovered |
| lthcs-crypto-daily | 47 11 * * * | 6/6 | 100% | 3.1h (scheduler 4.0h) | 2026-10-20T15:50 success; catch-up 2026-10-21T13:43 success | catch-up 2 |
| lthcs-daily | 11 23 * * * | 7/7 | 100% | 1.8h | 2026-10-21T01:32 success; catch-up 2026-10-21T00:43 success | catch-up 3 |
| lthcs-news-hourly | 19 * * * * | 8/8 | 32% (scheduler 24%) | 0.3h | 2026-10-21T10:15 success; catch-up 2026-10-21T12:58 success | catch-up 15 |
| lthcs-quality-audit-monthly | 43 9 1 * * | 0/0 | – | – | 2026-10-01T15:53 failure; recovered 2026-10-17T12:56 success (workflow_dispatch) | recovered |
| lthcs-trends-daily | 13 4 * * * | 7/7 | 100% | 4.3h (scheduler 4.9h) | 2026-10-21T09:18 success; catch-up 2026-10-21T06:07 success | catch-up 3 |
| lthcs-trends-weekly | 47 2 * * 1 | 1/1 | 100% | 5.3h | 2026-10-19T08:05 success | ok |
| lthcs-tune-weights-monthly | 41 7 1 * * | 0/0 | – | – | 2026-10-01T14:10 success | ok |
| lthcs-validate-weekly | 11 5 * * 1 | 1/1 | 100% | 5.5h | 2026-10-19T10:40 success | ok |
| money-flow-daily | 30 8 * * * | 6/6 | 100% | 3.8h (scheduler 5.1h) | 2026-10-21T14:01 success; catch-up 2026-10-21T10:08 success | catch-up 3 |
| pages | 7 * * * * | 8/8 | 30% (scheduler 23%) | 0.2h | 2026-10-21T10:01 success; catch-up 2026-10-21T12:58 success | catch-up 10 |
| real-estate-daily | 13 6 * * * | 6/6 | 100% | 4.7h | 2026-10-21T11:40 success; catch-up 2026-10-21T09:39 success | catch-up 2 |
| secrets-check | 23 15 * * 1 | 1/1 | 100% | 5.2h | 2026-10-19T20:38 success | ok |
| security-audit | 31 9 * * 1 | 1/1 | 100% | 7.1h | 2026-10-19T16:36 success | ok |
| trufflehog-weekly | 37 3 * * 0 | 1/1 | 100% | 4.7h | 2026-10-18T08:18 success | ok |
| usaspending-daily | 47 7 * * * | 6/6 | 100% | 4.3h (scheduler 5.0h) | 2026-10-21T13:15 success; catch-up 2026-10-21T10:08 success | catch-up 3 |

A catch-up is a `workflow_dispatch` by `github-actions[bot]` (the cron watchdog), not a GitHub schedule event. Ticks run and median delay count it toward freshness; the scheduler figure is what the schedule event delivered on its own.

## UX (live site)

| page | viewport | HTTP | weight / requests | JS exceptions | console errors | failed requests | overflow | tabs tappable |
|---|---|---|---|---|---|---|---|---|
| / | phone | 200 | 1.8 MB / 28 | 0 | 0 | 0 | no | 22/22 |
| / | desktop | 200 | 1.8 MB / 28 | 0 | 0 | 0 | no | 22/22 |
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
