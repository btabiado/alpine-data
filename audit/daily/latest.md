# Daily audit 2026-10-26 — RED

**RED** · 1 P0 · 0 P1 · 3 P2 · 1 new · 4 fixed (vs. 2026-10-25)

Generated 2026-10-26T12:53:10Z by [daily-audit](https://github.com/btabiado/alpine-data/actions/runs/40325593076). Machine-readable: `audit/daily/2026-10-26.json`.

4 jobs started late; 4 jobs skipped schedule ticks; watchdog caught up; data fresh

## New since yesterday (1)
- **P0** [feed] Feed data-city.json is STALE (786d old, limit 160d) — container says 10m via generated_at, but contents at cities[].data_health.last_updated are 785.5d old — reporting the older of the two, because a composite is only as fresh as its oldest input

## Ongoing (3)
- **P2** [api] Upstream APIs blocked (1): Farside (ETF flows) _(since 2026-10-06)_
- **P2** [api] Upstream APIs degraded (1): DeFiLlama bridges _(since 2026-10-05)_
- **P2** [feed] Feed data-tsa.json is an upstream lag (4d old, limit 2d); the source has not published a newer row — upstream lag: source newest date 2026-10-22 equals this file's as_of, checked 19.2h ago (2026-10-25T17:36:04Z). TSA has not published a newer checkpoint. Disclosed under 7d; a missed check or an olde… _(since 2026-10-25)_

## Fixed since yesterday (4)
- **P1** [ux] V1 (/): visible "Infinity" in #newsFeed — “rs Have Pushed to Cut Discs Since 2007, Infinity Ward Veteran Says” — during tab:trading
- **P1** [ux] V1 (/): visible "Infinity" in #overviewNewsHost — “rs Have Pushed to Cut Discs Since 2007, Infinity Ward Veteran Says” — during load
- **P1** [ux] V1 (/): visible "Infinity" in #researchNewsHost — “rs Have Pushed to Cut Discs Since 2007, Infinity Ward Veteran Says” — during tab:social
- **P2** [api] Upstream APIs degraded (1): BLS

## Snapshot

- **LTHCS quality audit** (data/lthcs/quality_audit/latest_summary.json): DEGRADED. thesis_integrity is KNOWN GAP (accepted) (decided 2026-10-17, review by 2027-04-17)
- **Feeds** (data_health, committed): 3 disclosed, 30 ok, 15 skipped, 1 stale, 1 suppressed
  - muted: data-mufon.json (muted until 2027-01-04: NUFORC permitted aggregated totals only. Raw …)
- **Workflows** (last 24h, main): 22 ran, 0 ended failed
- **API status** (2026-10-26T12:35:26+00:00): 2 auth_required, 1 blocked, 1 degraded, 1 retired, 49 up
  - changed vs. git:e1a5310440 (2026-10-25T05:21:41+00:00): BLS: degraded → up

**Spot checks** (live site vs. primary sources)

| check | status | detail |
|---|---|---|
| live_site_build_age | ok | built 2026-10-26T12:34:20Z (16m ago) |
| btc_price_vs_coingecko | ok | site 80339.07 vs 80427.16375056055 (0.11%), payload 17m old, source CoinGecko market_chart days=1 (nearest to… |
| eth_price_vs_coingecko | ok | site 2328.43 vs 2330.869988497936 (0.105%), payload 17m old, source CoinGecko market_chart days=1 (nearest to… |
| fear_greed_vs_alternative_me | ok | 2026-10-26: site 50 vs 50 |
| btc_tip_height_vs_mempool_space | ok | site 973235 vs 973237 (lag 2 blocks) |
| treasury_10y_vs_treasury_gov | ok | 2026-10-22: site 4.96 vs 4.96 |

**Live payload stamps**: 9 of 15 stamped within 48h, the rest:

| payload | status | data as_of | generated |
|---|---|---|---|
| data-aviation.json | ok | 2025-12-31 (300d) | – |
| data-city.json | ok | 2026-09-01 (56d) | 2026-10-25T12:47 (24h) |
| data-mufon.json | ok | – | 2026-10-06T12:22 (20d) |
| data-stock-money-flow.json | ok | 2026-10-23 (4d) | – |
| data-tsa.json | ok | 2026-10-22 (5d) | 2026-10-23T15:58 (3d) |
| data-us_states.json | ok | – | – |

## Schedules (last 7 days)

| workflow | cron (UTC) | days run/due | ticks run | median delay | last scheduled run | flag |
|---|---|---|---|---|---|---|
| aviation-opensky | 25 * * * * | 8/8 | 45% (scheduler 23%) | 0.2h | 2026-10-26T06:26 success; catch-up 2026-10-26T12:32 success | catch-up 47 |
| aviation-tsa | 10 14 * * * | 7/7 | 100% | 1.8h (scheduler 4.2h) | 2026-10-25T17:35 success; catch-up 2026-10-25T16:05 success | catch-up 7 |
| catalog-health | 23 8 * * 1 | 0/0 | – | – | 2026-10-19T15:41 success | ok |
| cfpb-daily | 17 7 * * * | 6/6 | 100% | 2.4h (scheduler 5.5h) | 2026-10-25T12:34 success; catch-up 2026-10-26T12:32 success | catch-up 6 |
| city-daily | 29 7 * * * | 6/6 | 100% | 2.2h (scheduler 5.4h) | 2026-10-25T12:41 success; catch-up 2026-10-26T12:32 success | catch-up 6 |
| codeql | 23 4 * * 0 | 1/1 | 100% | 4.8h | 2026-10-25T09:12 success | ok |
| cron-watchdog | 12 * * * * ; 27 * * * * ; 42 * * * * ; 57 * * * * | 8/8 | 37% | 0.0h | 2026-10-26T12:49 success | ok |
| crypto-odds-daily | 15 14 * * * | 7/7 | 100% | 1.8h (scheduler 4.2h) | 2026-10-25T17:37 success; catch-up 2026-10-25T16:05 success | catch-up 7 |
| daily-audit | 17 9 * * * | 6/6 | 100% | 2.2h (scheduler 5.0h) | 2026-10-25T13:42 success; catch-up 2026-10-25T11:16 success | catch-up 6 |
| data-health | 11 15 * * * | 7/7 | 100% | 1.8h (scheduler 4.0h) | 2026-10-25T18:24 success; catch-up 2026-10-25T16:48 success | catch-up 7 |
| etf-flows-daily | 15 9 * * * | 6/6 | 100% | 2.0h (scheduler 5.1h) | 2026-10-25T13:43 success; catch-up 2026-10-26T12:50 success | catch-up 6 |
| lthcs-backtest-daily | 30 23 * * * | 7/7 | 100% | 1.8h | 2026-10-26T01:25 success; catch-up 2026-10-26T01:16 success | catch-up 7 |
| lthcs-backtest-monthly | 19 6 1 * * | 0/0 | – | – | 2026-10-01T11:52 success | ok |
| lthcs-beta-verdict-monthly | 37 8 1 * * | 0/0 | – | – | 2026-10-01T15:12 failure; recovered 2026-10-16T21:48 success (workflow_dispatch) | recovered |
| lthcs-crypto-daily | 47 11 * * * | 6/6 | 100% | 1.9h (scheduler 4.2h) | 2026-10-25T15:03 success; catch-up 2026-10-25T13:18 success | catch-up 6 |
| lthcs-daily | 11 23 * * * | 7/7 | 100% | 1.9h | 2026-10-26T01:07 success; catch-up 2026-10-25T01:04 success | catch-up 6 |
| lthcs-news-hourly | 19 * * * * | 8/8 | 42% (scheduler 23%) | 0.2h | 2026-10-26T06:23 success; catch-up 2026-10-26T12:32 success | catch-up 41 |
| lthcs-quality-audit-monthly | 43 9 1 * * | 0/0 | – | – | 2026-10-01T15:53 failure; recovered 2026-10-17T12:56 success (workflow_dispatch) | recovered |
| lthcs-trends-daily | 13 4 * * * | 6/6 | 100% | 1.9h (scheduler 5.1h) | 2026-10-26T10:01 success; catch-up 2026-10-26T06:04 success | catch-up 6 |
| lthcs-trends-weekly | 47 2 * * 1 | 1/1 | 100% | 5.3h | 2026-10-26T08:08 success | ok |
| lthcs-tune-weights-monthly | 41 7 1 * * | 0/0 | – | – | 2026-10-01T14:10 success | ok |
| lthcs-validate-weekly | 11 5 * * 1 | 0/0 | – | – | 2026-10-26T10:58 success | ok |
| money-flow-daily | 30 8 * * * | 6/6 | 100% | 1.7h (scheduler 5.3h) | 2026-10-25T13:17 success; catch-up 2026-10-26T12:50 success | catch-up 6 |
| pages | 7 * * * * | 8/8 | 44% (scheduler 23%) | 0.3h | 2026-10-26T06:00 success; catch-up 2026-10-26T12:32 success | catch-up 32 |
| real-estate-daily | 13 6 * * * | 6/6 | 100% | 3.0h (scheduler 5.3h) | 2026-10-25T11:17 success; catch-up 2026-10-26T08:07 success | catch-up 6 |
| secrets-check | 23 15 * * 1 | 1/1 | 100% | 5.2h | 2026-10-19T20:38 success | ok |
| security-audit | 31 9 * * 1 | 0/0 | – | – | 2026-10-19T16:36 success | ok |
| trufflehog-weekly | 37 3 * * 0 | 1/1 | 100% | 5.1h | 2026-10-25T08:42 success | ok |
| usaspending-daily | 47 7 * * * | 6/6 | 100% | 2.4h (scheduler 5.2h) | 2026-10-25T12:46 success; catch-up 2026-10-26T12:50 success | catch-up 6 |

A catch-up is a `workflow_dispatch` by `github-actions[bot]` (the cron watchdog), not a GitHub schedule event. Ticks run and median delay count it toward freshness; the scheduler figure is what the schedule event delivered on its own.

## UX (live site)

| page | viewport | HTTP | weight / requests | JS exceptions | console errors | failed requests | overflow | tabs tappable |
|---|---|---|---|---|---|---|---|---|
| / | phone | 200 | 1.8 MB / 27 | 0 | 0 | 0 | no | 22/22 |
| / | desktop | 200 | 1.8 MB / 27 | 0 | 0 | 0 | no | 22/22 |
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
