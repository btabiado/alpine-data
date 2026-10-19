# Daily audit 2026-10-19 — AMBER

**AMBER** · 0 P0 · 1 P1 · 23 P2 · 4 new · 7 fixed (vs. 2026-10-18)

Generated 2026-10-19T16:30:08Z by [daily-audit](https://github.com/btabiado/alpine-data/actions/runs/39285594871). Machine-readable: `audit/daily/2026-10-19.json`.

## New since yesterday (4)
- **P2** [api] Upstream APIs blocked (1): bitinfocharts
- **P2** [schedule] Crons that lose most of their ticks (1): cron-watchdog 17%
- **P2** [schedule] Crons starting >3h late (median delay) (2): codeql 4.5h, trufflehog-weekly 4.7h

## Ongoing (20)
- **P1** [schedule] Scheduled workflow lthcs-beta-verdict-monthly: last scheduled run failed (2026-10-01) — cron 37 8 1 * * ([run](https://github.com/btabiado/alpine-data/actions/runs/36882479373)) _(since 2026-10-05)_
- **P2** [api] Upstream APIs blocked (1): Farside (ETF flows) _(since 2026-10-06)_
- **P2** [api] Upstream APIs degraded (1): DeFiLlama bridges _(since 2026-10-05)_
- **P2** [schedule] Crons that lose most of their ticks (3): aviation-opensky 24%, lthcs-news-hourly 23%, pages 23% _(since 2026-10-05)_
- **P2** [schedule] Crons starting >3h late (median delay) (14): aviation-tsa 4.0h, cfpb-daily 5.1h, city-daily 4.7h, crypto-odds-daily 4.0h, daily-audit 4.9h, data-health 3.8h, etf-flows-daily 4.9h, lthcs-crypto-daily 4.0h, lthcs-trends-daily 4.8h, lthcs-trends-weekly 5.3h, lthcs-validate-weekly 5.5h, money-flow-daily 5.1h, real-estate-daily 4.8h, usaspending-daily 5.0h _(since 2026-10-05)_

## Fixed since yesterday (7)
- **P0** [feed] Feed data-tsa.json is STALE (3d old, limit 2d)
- **P1** [schedule] Scheduled workflow crypto-odds-daily: last scheduled run failed (2026-10-17) — cron 15 14 * * * ([run](https://github.com/btabiado/alpine-data/actions/runs/39052317800))
- **P1** [schedule] Scheduled workflow lthcs-validate-weekly: last scheduled run failed (2026-10-12) — cron 11 5 * * 1 ([run](https://github.com/btabiado/alpine-data/actions/runs/38251149250))
- **P2** [schedule] Crons starting >3h late (median delay) (4): catalog-health 9.3h, cron-watchdog 4.7h, secrets-check 7.1h, security-audit 8.9h

## Snapshot

- **LTHCS quality audit** (data/lthcs/quality_audit/latest_summary.json): DEGRADED. thesis_integrity is KNOWN GAP (accepted) (decided 2026-10-17, review by 2027-04-17)
- **Feeds** (data_health, committed): 2 disclosed, 32 ok, 15 skipped, 1 suppressed
  - muted: data-mufon.json (muted until 2027-01-04: NUFORC permitted aggregated totals only. Raw …)
- **Workflows** (last 24h, main): 24 ran, 0 ended failed
- **API status** (2026-10-19T14:38:41+00:00): 2 auth_required, 2 blocked, 1 degraded, 49 up
  - changed vs. git:c7d63cc163 (2026-10-18T10:57:18+00:00): bitinfocharts: up → blocked

**Spot checks** (live site vs. primary sources)

| check | status | detail |
|---|---|---|
| live_site_build_age | ok | built 2026-10-19T14:37:43Z (2h ago) |
| btc_price_vs_coingecko | ok | site 81870.75 vs 81919.0 (0.059%) |
| eth_price_vs_coingecko | ok | site 2391.48 vs 2398.74 (0.303%) |
| fear_greed_vs_alternative_me | ok | 2026-10-19: site 59 vs 59 |
| btc_tip_height_vs_mempool_space | ok | site 972169 vs 972178 (lag 9 blocks) |
| treasury_10y_vs_treasury_gov | ok | 2026-10-15: site 5.1 vs 5.1 |

**Live payload stamps**: 10 of 15 stamped within 48h, the rest:

| payload | status | data as_of | generated |
|---|---|---|---|
| data-aviation.json | ok | 2025-12-31 (293d) | – |
| data-city.json | ok | 2026-09-01 (49d) | 2026-10-19T11:45 (5h) |
| data-mufon.json | ok | – | 2026-10-06T12:22 (13d) |
| data-tsa.json | ok | 2026-10-15 (5d) | 2026-10-16T17:57 (3d) |
| data-us_states.json | ok | – | – |

## Schedules (last 7 days)

| workflow | cron (UTC) | days run/due | ticks run | median delay | last scheduled run | flag |
|---|---|---|---|---|---|---|
| aviation-opensky | 25 * * * * | 8/8 | 24% (scheduler 23%) | 0.1h | 2026-10-19T15:43 success; catch-up 2026-10-19T13:38 success | catch-up 5 |
| aviation-tsa | 10 14 * * * | 6/6 | 100% | 4.0h | 2026-10-18T17:15 success; catch-up 2026-10-19T16:11 success | ok |
| catalog-health | 23 8 * * 1 | 0/0 | – | – | 2026-10-19T15:41 success | ok |
| cfpb-daily | 17 7 * * * | 6/6 | 100% | 5.1h | 2026-10-19T14:41 success; catch-up 2026-10-19T11:16 success | ok |
| city-daily | 29 7 * * * | 6/6 | 100% | 4.7h | 2026-10-19T14:52 success; catch-up 2026-10-19T11:41 success | catch-up 1 |
| codeql | 23 4 * * 0 | 1/1 | 100% | 4.5h | 2026-10-18T08:53 success | ok |
| cron-watchdog | 12 * * * * ; 27 * * * * ; 42 * * * * ; 57 * * * * | 3/3 | 17% | 0.0h | 2026-10-19T13:37 success | ok |
| crypto-odds-daily | 15 14 * * * | 6/6 | 100% | 4.0h | 2026-10-18T17:19 success | ok |
| daily-audit | 17 9 * * * | 6/6 | 100% | 4.9h | 2026-10-18T13:16 success; catch-up 2026-10-19T11:41 success | ok |
| data-health | 11 15 * * * | 6/6 | 100% | 3.8h | 2026-10-18T18:04 success | ok |
| etf-flows-daily | 15 9 * * * | 6/6 | 100% | 4.9h | 2026-10-18T13:19 success; catch-up 2026-10-19T11:41 success | ok |
| lthcs-backtest-daily | 30 23 * * * | 7/7 | 100% | 2.0h | 2026-10-19T01:26 success; catch-up 2026-10-19T01:08 success | catch-up 1 |
| lthcs-backtest-monthly | 19 6 1 * * | 0/0 | – | – | 2026-10-01T11:52 success | ok |
| lthcs-beta-verdict-monthly | 37 8 1 * * | 0/0 | – | – | 2026-10-01T15:12 failure | FAILING |
| lthcs-crypto-daily | 47 11 * * * | 6/6 | 100% | 4.0h | 2026-10-18T14:52 success; catch-up 2026-10-19T13:38 success | ok |
| lthcs-daily | 11 23 * * * | 7/7 | 100% | 1.9h | 2026-10-19T01:13 success; catch-up 2026-10-19T01:08 success | catch-up 1 |
| lthcs-news-hourly | 19 * * * * | 8/8 | 23% (scheduler 22%) | 0.1h | 2026-10-19T15:41 success; catch-up 2026-10-19T13:38 success | catch-up 3 |
| lthcs-quality-audit-monthly | 43 9 1 * * | 0/0 | – | – | 2026-10-01T15:53 failure | ok |
| lthcs-trends-daily | 13 4 * * * | 7/7 | 100% | 4.8h | 2026-10-19T09:49 success; catch-up 2026-10-19T06:07 success | catch-up 1 |
| lthcs-trends-weekly | 47 2 * * 1 | 1/1 | 100% | 5.3h | 2026-10-19T08:05 success | ok |
| lthcs-tune-weights-monthly | 41 7 1 * * | 0/0 | – | – | 2026-10-01T14:10 success | ok |
| lthcs-validate-weekly | 11 5 * * 1 | 1/1 | 100% | 5.5h | 2026-10-19T10:40 success | ok |
| money-flow-daily | 30 8 * * * | 6/6 | 100% | 5.1h | 2026-10-19T15:47 success; catch-up 2026-10-19T11:41 success | catch-up 1 |
| pages | 7 * * * * | 8/8 | 23% (scheduler 23%) | 0.1h | 2026-10-19T14:36 success; catch-up 2026-10-19T13:38 success | catch-up 1 |
| real-estate-daily | 13 6 * * * | 7/7 | 100% | 4.8h | 2026-10-19T13:00 success; catch-up 2026-10-19T07:55 success | catch-up 1 |
| secrets-check | 23 15 * * 1 | 0/0 | – | – | 2026-10-12T22:28 success | ok |
| security-audit | 31 9 * * 1 | 0/0 | – | – | 2026-10-12T18:24 success | ok |
| trufflehog-weekly | 37 3 * * 0 | 1/1 | 100% | 4.7h | 2026-10-18T08:18 success | ok |
| usaspending-daily | 47 7 * * * | 6/6 | 100% | 5.0h | 2026-10-19T15:00 success; catch-up 2026-10-19T11:16 success | catch-up 1 |

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
