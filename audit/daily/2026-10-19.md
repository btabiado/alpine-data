# Daily audit 2026-10-19 — AMBER

**AMBER** · 0 P0 · 1 P1 · 22 P2 · 4 new · 8 fixed (vs. 2026-10-18)

Generated 2026-10-19T11:43:57Z by [daily-audit](https://github.com/btabiado/alpine-data/actions/runs/39246495545). Machine-readable: `audit/daily/2026-10-19.json`.

## New since yesterday (4)
- **P2** [feed] Feed data-tsa.json is an upstream lag (4d old, limit 2d); the source has not published a newer row — upstream lag: source newest date 2026-10-15 equals this file's as_of, checked 18.4h ago (2026-10-18T17:16:08Z). TSA has not published a newer checkpoint. Disclosed under 7d; a missed check or an olde…
- **P2** [schedule] Crons that lose most of their ticks (1): cron-watchdog 15%
- **P2** [schedule] Crons starting >3h late (median delay) (2): codeql 4.5h, trufflehog-weekly 4.7h

## Ongoing (19)
- **P1** [schedule] Scheduled workflow lthcs-beta-verdict-monthly: last scheduled run failed (2026-10-01) — cron 37 8 1 * * ([run](https://github.com/btabiado/alpine-data/actions/runs/36882479373)) _(since 2026-10-05)_
- **P2** [api] Upstream APIs blocked (1): Farside (ETF flows) _(since 2026-10-06)_
- **P2** [api] Upstream APIs degraded (1): DeFiLlama bridges _(since 2026-10-05)_
- **P2** [schedule] Crons that lose most of their ticks (3): aviation-opensky 24%, lthcs-news-hourly 23%, pages 22% _(since 2026-10-05)_
- **P2** [schedule] Crons starting >3h late (median delay) (13): aviation-tsa 4.0h, cfpb-daily 5.1h, city-daily 4.7h, crypto-odds-daily 4.0h, daily-audit 4.9h, data-health 3.8h, etf-flows-daily 4.9h, lthcs-crypto-daily 4.0h, lthcs-trends-daily 4.9h, money-flow-daily 5.1h, real-estate-daily 4.9h, secrets-check 7.1h, usaspending-daily 5.0h _(since 2026-10-05)_

## Fixed since yesterday (8)
- **P0** [feed] Feed data-tsa.json is STALE (3d old, limit 2d)
- **P1** [schedule] Scheduled workflow crypto-odds-daily: last scheduled run failed (2026-10-17) — cron 15 14 * * * ([run](https://github.com/btabiado/alpine-data/actions/runs/39052317800))
- **P1** [schedule] Scheduled workflow lthcs-validate-weekly: last scheduled run failed (2026-10-12) — cron 11 5 * * 1 ([run](https://github.com/btabiado/alpine-data/actions/runs/38251149250))
- **P2** [schedule] Crons starting >3h late (median delay) (5): catalog-health 9.3h, cron-watchdog 4.7h, lthcs-trends-weekly 7.0h, lthcs-validate-weekly 7.6h, security-audit 8.9h

## Snapshot

- **LTHCS quality audit** (data/lthcs/quality_audit/latest_summary.json): DEGRADED. thesis_integrity is KNOWN GAP (accepted) (decided 2026-10-17, review by 2027-04-17)
- **Feeds** (data_health, committed): 3 disclosed, 31 ok, 15 skipped, 1 suppressed
  - muted: data-mufon.json (muted until 2027-01-04: NUFORC permitted aggregated totals only. Raw …)
- **Workflows** (last 24h, main): 21 ran, 0 ended failed
- **API status** (2026-10-19T11:19:07+00:00): 2 auth_required, 1 blocked, 1 degraded, 50 up
  - no verdict changes vs. git:c7d63cc163 (2026-10-18T10:57:18+00:00)

**Spot checks** (live site vs. primary sources)

| check | status | detail |
|---|---|---|
| live_site_build_age | ok | built 2026-10-19T11:17:48Z (24m ago) |
| btc_price_vs_coingecko | ok | site 82115.47 vs 82186.0 (0.086%) |
| eth_price_vs_coingecko | ok | site 2413.52 vs 2415.61 (0.087%) |
| fear_greed_vs_alternative_me | ok | 2026-10-19: site 59 vs 59 |
| btc_tip_height_vs_mempool_space | ok | site 972136 vs 972137 (lag 1 blocks) |
| treasury_10y_vs_treasury_gov | ok | 2026-10-15: site 5.1 vs 5.1 |

**Live payload stamps**: 9 of 15 stamped within 48h, the rest:

| payload | status | data as_of | generated |
|---|---|---|---|
| data-aviation.json | ok | 2025-12-31 (292d) | – |
| data-city.json | ok | 2026-09-01 (48d) | 2026-10-18T12:16 (23h) |
| data-mufon.json | ok | – | 2026-10-06T12:22 (13d) |
| data-stock-money-flow.json | ok | 2026-10-16 (3d) | – |
| data-tsa.json | ok | 2026-10-15 (4d) | 2026-10-16T17:57 (3d) |
| data-us_states.json | ok | – | – |

## Schedules (last 7 days)

| workflow | cron (UTC) | days run/due | ticks run | median delay | last scheduled run | flag |
|---|---|---|---|---|---|---|
| aviation-opensky | 25 * * * * | 8/8 | 24% (scheduler 23%) | 0.2h | 2026-10-19T07:54 success; catch-up 2026-10-19T11:16 success | catch-up 4 |
| aviation-tsa | 10 14 * * * | 7/7 | 100% | 4.0h | 2026-10-18T17:15 success | ok |
| catalog-health | 23 8 * * 1 | 0/0 | – | – | 2026-10-12T17:40 success | ok |
| cfpb-daily | 17 7 * * * | 6/6 | 100% | 5.1h | 2026-10-18T12:04 success; catch-up 2026-10-19T11:16 success | ok |
| city-daily | 29 7 * * * | 6/6 | 100% | 4.7h | 2026-10-18T12:08 success; catch-up 2026-10-18T12:08 success | catch-up 1 |
| codeql | 23 4 * * 0 | 1/1 | 100% | 4.5h | 2026-10-18T08:53 success | ok |
| cron-watchdog | 12 * * * * ; 27 * * * * ; 42 * * * * ; 57 * * * * | 3/3 | 15% | 0.0h | 2026-10-19T11:41 success | ok |
| crypto-odds-daily | 15 14 * * * | 7/7 | 100% | 4.0h | 2026-10-18T17:19 success | ok |
| daily-audit | 17 9 * * * | 6/6 | 100% | 4.9h | 2026-10-18T13:16 success | ok |
| data-health | 11 15 * * * | 7/7 | 100% | 3.8h | 2026-10-18T18:04 success | ok |
| etf-flows-daily | 15 9 * * * | 6/6 | 100% | 4.9h | 2026-10-18T13:19 success; catch-up 2026-10-19T11:41 success | ok |
| lthcs-backtest-daily | 30 23 * * * | 7/7 | 100% | 2.0h | 2026-10-19T01:26 success; catch-up 2026-10-19T01:08 success | catch-up 1 |
| lthcs-backtest-monthly | 19 6 1 * * | 0/0 | – | – | 2026-10-01T11:52 success | ok |
| lthcs-beta-verdict-monthly | 37 8 1 * * | 0/0 | – | – | 2026-10-01T15:12 failure | FAILING |
| lthcs-crypto-daily | 47 11 * * * | 7/7 | 100% | 4.0h | 2026-10-18T14:52 success | ok |
| lthcs-daily | 11 23 * * * | 7/7 | 100% | 1.9h | 2026-10-19T01:13 success; catch-up 2026-10-19T01:08 success | catch-up 1 |
| lthcs-news-hourly | 19 * * * * | 8/8 | 23% (scheduler 23%) | 0.1h | 2026-10-19T07:52 success; catch-up 2026-10-19T11:16 success | catch-up 2 |
| lthcs-quality-audit-monthly | 43 9 1 * * | 0/0 | – | – | 2026-10-01T15:53 failure | ok |
| lthcs-trends-daily | 13 4 * * * | 6/6 | 100% | 4.9h | 2026-10-18T08:48 success; catch-up 2026-10-19T06:07 success | ok |
| lthcs-trends-weekly | 47 2 * * 1 | 0/0 | – | – | 2026-10-12T09:46 success | ok |
| lthcs-tune-weights-monthly | 41 7 1 * * | 0/0 | – | – | 2026-10-01T14:10 success | ok |
| lthcs-validate-weekly | 11 5 * * 1 | 0/0 | – | – | 2026-10-19T10:40 success | ok |
| money-flow-daily | 30 8 * * * | 6/6 | 100% | 5.1h | 2026-10-18T12:44 success; catch-up 2026-10-19T11:41 success | catch-up 1 |
| pages | 7 * * * * | 8/8 | 22% | 0.1h | 2026-10-19T06:07 success; catch-up 2026-10-19T11:16 success | ok |
| real-estate-daily | 13 6 * * * | 6/6 | 100% | 4.9h | 2026-10-18T10:56 success; catch-up 2026-10-19T07:55 success | ok |
| secrets-check | 23 15 * * 1 | 1/1 | 100% | 7.1h | 2026-10-12T22:28 success | ok |
| security-audit | 31 9 * * 1 | 0/0 | – | – | 2026-10-12T18:24 success | ok |
| trufflehog-weekly | 37 3 * * 0 | 1/1 | 100% | 4.7h | 2026-10-18T08:18 success | ok |
| usaspending-daily | 47 7 * * * | 6/6 | 100% | 5.0h | 2026-10-18T12:11 success; catch-up 2026-10-19T11:16 success | catch-up 1 |

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
| /real-estate/ | phone | 200 | 1.2 MB / 12 | 0 | 0 | 0 | no | – |
| /real-estate/ | desktop | 200 | 1.2 MB / 24 | 0 | 0 | 0 | no | – |
| /lthcs/ | phone | 200 | 0.4 MB / 37 | 0 | 0 | 0 | no | – |
| /lthcs/ | desktop | 200 | 0.4 MB / 37 | 0 | 0 | 0 | no | – |
| /api-universe/ | phone | 200 | 0.4 MB / 6 | 0 | 0 | 0 | no | – |
| /api-universe/ | desktop | 200 | 0.4 MB / 6 | 0 | 0 | 0 | no | – |

UX audit took 44s.
