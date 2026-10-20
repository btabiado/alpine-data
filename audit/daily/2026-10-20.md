# Daily audit 2026-10-20 — AMBER

**AMBER** · 0 P0 · 1 P1 · 25 P2 · 3 new · 1 fixed (vs. 2026-10-19)

Generated 2026-10-20T11:31:35Z by [daily-audit](https://github.com/btabiado/alpine-data/actions/runs/39415304915). Machine-readable: `audit/daily/2026-10-20.json`.

## New since yesterday (3)
- **P2** [schedule] Crons starting >3h late (median delay) (3): catalog-health 7.3h, secrets-check 5.2h, security-audit 7.1h

## Ongoing (23)
- **P1** [schedule] Scheduled workflow lthcs-beta-verdict-monthly: last scheduled run failed (2026-10-01) — cron 37 8 1 * * ([run](https://github.com/btabiado/alpine-data/actions/runs/36882479373)) _(since 2026-10-05)_
- **P2** [api] Upstream APIs blocked (1): Farside (ETF flows) _(since 2026-10-06)_
- **P2** [api] Upstream APIs degraded (1): DeFiLlama bridges _(since 2026-10-05)_
- **P2** [schedule] Crons that lose most of their ticks (4): aviation-opensky 27%, cron-watchdog 21%, lthcs-news-hourly 27%, pages 25% _(since 2026-10-05)_
- **P2** [schedule] Crons starting >3h late (median delay) (16): aviation-tsa 3.8h, cfpb-daily 4.8h, city-daily 4.7h, codeql 4.5h, crypto-odds-daily 3.8h, daily-audit 4.7h, data-health 3.5h, etf-flows-daily 4.7h, lthcs-crypto-daily 3.8h, lthcs-trends-daily 4.8h, lthcs-trends-weekly 5.3h, lthcs-validate-weekly 5.5h, money-flow-daily 5.0h, real-estate-daily 4.8h, trufflehog-weekly 4.7h, usaspending-daily 4.8h _(since 2026-10-05)_

## Fixed since yesterday (1)
- **P2** [api] Upstream APIs blocked (1): bitinfocharts

## Snapshot

- **LTHCS quality audit** (data/lthcs/quality_audit/latest_summary.json): DEGRADED. thesis_integrity is KNOWN GAP (accepted) (decided 2026-10-17, review by 2027-04-17)
- **Feeds** (data_health, committed): 2 disclosed, 32 ok, 15 skipped, 1 suppressed
  - muted: data-mufon.json (muted until 2027-01-04: NUFORC permitted aggregated totals only. Raw …)
- **Workflows** (last 24h, main): 25 ran, 0 ended failed
- **API status** (2026-10-20T11:28:04+00:00): 2 auth_required, 1 blocked, 1 degraded, 1 retired, 49 up
  - changed vs. git:02fdebc7a9 (2026-10-19T11:19:07+00:00): bitinfocharts: up → retired

**Spot checks** (live site vs. primary sources)

| check | status | detail |
|---|---|---|
| live_site_build_age | ok | built 2026-10-20T09:43:53Z (2h ago) |
| btc_price_vs_coingecko | ok | site 81304.23 vs 81447.0 (0.175%) |
| eth_price_vs_coingecko | ok | site 2378.85 vs 2383.14 (0.18%) |
| fear_greed_vs_alternative_me | ok | 2026-10-20: site 55 vs 55 |
| btc_tip_height_vs_mempool_space | ok | site 972303 vs 972312 (lag 9 blocks) |
| treasury_10y_vs_treasury_gov | ok | 2026-10-16: site 5.09 vs 5.09 |

**Live payload stamps**: 10 of 15 stamped within 48h, the rest:

| payload | status | data as_of | generated |
|---|---|---|---|
| data-aviation.json | ok | 2025-12-31 (293d) | – |
| data-city.json | ok | 2026-09-01 (49d) | 2026-10-19T14:57 (21h) |
| data-mufon.json | ok | – | 2026-10-06T12:22 (14d) |
| data-tsa.json | ok | 2026-10-18 (2d) | 2026-10-19T16:11 (19h) |
| data-us_states.json | ok | – | – |

## Schedules (last 7 days)

| workflow | cron (UTC) | days run/due | ticks run | median delay | last scheduled run | flag |
|---|---|---|---|---|---|---|
| aviation-opensky | 25 * * * * | 8/8 | 27% (scheduler 23%) | 0.1h | 2026-10-20T06:07 success; catch-up 2026-10-20T09:42 success | catch-up 9 |
| aviation-tsa | 10 14 * * * | 7/7 | 100% | 3.8h | 2026-10-19T19:47 success; catch-up 2026-10-19T16:11 success | catch-up 1 |
| catalog-health | 23 8 * * 1 | 1/1 | 100% | 7.3h | 2026-10-19T15:41 success | ok |
| cfpb-daily | 17 7 * * * | 6/6 | 100% | 4.8h | 2026-10-19T14:41 success; catch-up 2026-10-20T09:42 success | catch-up 1 |
| city-daily | 29 7 * * * | 6/6 | 100% | 4.7h | 2026-10-19T14:52 success; catch-up 2026-10-20T09:58 success | catch-up 2 |
| codeql | 23 4 * * 0 | 1/1 | 100% | 4.5h | 2026-10-18T08:53 success | ok |
| cron-watchdog | 12 * * * * ; 27 * * * * ; 42 * * * * ; 57 * * * * | 4/4 | 21% | 0.0h | 2026-10-20T10:11 success | ok |
| crypto-odds-daily | 15 14 * * * | 7/7 | 100% | 3.8h | 2026-10-19T19:52 success; catch-up 2026-10-19T16:11 success | catch-up 1 |
| daily-audit | 17 9 * * * | 6/6 | 100% | 4.7h | 2026-10-19T16:27 success; catch-up 2026-10-19T11:41 success | catch-up 1 |
| data-health | 11 15 * * * | 7/7 | 100% | 3.5h | 2026-10-19T20:35 success; catch-up 2026-10-19T16:46 success | catch-up 1 |
| etf-flows-daily | 15 9 * * * | 6/6 | 100% | 4.7h | 2026-10-19T16:28 success; catch-up 2026-10-20T11:29 success | catch-up 1 |
| lthcs-backtest-daily | 30 23 * * * | 7/7 | 100% | 1.9h | 2026-10-20T01:56 success; catch-up 2026-10-20T01:17 success | catch-up 2 |
| lthcs-backtest-monthly | 19 6 1 * * | 0/0 | – | – | 2026-10-01T11:52 success | ok |
| lthcs-beta-verdict-monthly | 37 8 1 * * | 0/0 | – | – | 2026-10-01T15:12 failure | FAILING |
| lthcs-crypto-daily | 47 11 * * * | 7/7 | 100% | 3.8h | 2026-10-19T17:47 success; catch-up 2026-10-19T13:38 success | catch-up 1 |
| lthcs-daily | 11 23 * * * | 7/7 | 100% | 1.9h | 2026-10-20T01:48 success; catch-up 2026-10-20T01:17 success | catch-up 2 |
| lthcs-news-hourly | 19 * * * * | 8/8 | 27% (scheduler 23%) | 0.2h | 2026-10-20T06:06 success; catch-up 2026-10-20T09:42 success | catch-up 7 |
| lthcs-quality-audit-monthly | 43 9 1 * * | 0/0 | – | – | 2026-10-01T15:53 failure | ok |
| lthcs-trends-daily | 13 4 * * * | 6/6 | 100% | 4.8h | 2026-10-20T09:22 success; catch-up 2026-10-20T06:01 success | catch-up 1 |
| lthcs-trends-weekly | 47 2 * * 1 | 1/1 | 100% | 5.3h | 2026-10-19T08:05 success | ok |
| lthcs-tune-weights-monthly | 41 7 1 * * | 0/0 | – | – | 2026-10-01T14:10 success | ok |
| lthcs-validate-weekly | 11 5 * * 1 | 1/1 | 100% | 5.5h | 2026-10-19T10:40 success | ok |
| money-flow-daily | 30 8 * * * | 6/6 | 100% | 5.0h | 2026-10-19T15:47 success; catch-up 2026-10-20T10:11 success | catch-up 2 |
| pages | 7 * * * * | 8/8 | 25% (scheduler 23%) | 0.2h | 2026-10-20T05:55 success; catch-up 2026-10-20T09:42 success | catch-up 4 |
| real-estate-daily | 13 6 * * * | 6/6 | 100% | 4.8h | 2026-10-20T11:27 success; catch-up 2026-10-20T09:42 success | catch-up 1 |
| secrets-check | 23 15 * * 1 | 1/1 | 100% | 5.2h | 2026-10-19T20:38 success | ok |
| security-audit | 31 9 * * 1 | 1/1 | 100% | 7.1h | 2026-10-19T16:36 success | ok |
| trufflehog-weekly | 37 3 * * 0 | 1/1 | 100% | 4.7h | 2026-10-18T08:18 success | ok |
| usaspending-daily | 47 7 * * * | 6/6 | 100% | 4.8h | 2026-10-19T15:00 success; catch-up 2026-10-20T09:58 success | catch-up 2 |

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

UX audit took 39s.
