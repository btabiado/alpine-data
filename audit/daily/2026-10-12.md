# Daily audit 2026-10-12 — RED

**RED** · 5 P0 · 6 P1 · 24 P2 · 3 new · 2 fixed (vs. 2026-10-11)

Generated 2026-10-12T19:04:16Z by [daily-audit](https://github.com/btabiado/alpine-data/actions/runs/38294955398). Machine-readable: `audit/daily/2026-10-12.json`.

## New since yesterday (3)
- **P1** [schedule] Scheduled workflow pages missed 1 day(s) in the last week — cron 0 * * * *; no scheduled run on: 2026-10-05
- **P1** [workflow] Workflow cfpb-daily failed in the last 24h — step 'Fetch CFPB complaint counts' — Process completed with exit code 1. ([run](https://github.com/btabiado/alpine-data/actions/runs/38277091475))
- **P1** [workflow] Workflow lthcs-validate-weekly failed in the last 24h — step 'Fail workflow if validation reported issues' — lthcs-validate-weekly: Validator exited 2: hard failures. Each failing check has its own annotation and a row in the job summary; the full list is… ([run](https://github.com/btabiado/alpine-data/actions/runs/38251149250))

## Ongoing (32)
- **P0** [feed] Feed R2 archive [history: daily payload copies] is GAP (? old, limit ?) — missing 54: v2/data-cpi.json: 2026-10-07, v2/data-cpi.json: 2026-10-08, v2/data-cpi.json: 2026-10-09, v2/data-cpi.json: 2026-10-10, v2/data-cpi.json: 2026-10-11, v2/data-cpi.json: 2026-10-12, v2/data… _(since 2026-10-07)_
- **P0** [feed] Feed data-tsa.json is STALE (4d old, limit 2d) _(since 2026-10-11)_
- **P0** [feed] Feed data/btc_flows.csv is STALE (5d old, limit 2d) _(since 2026-10-08)_
- **P0** [feed] Feed data/eth_flows.csv is STALE (5d old, limit 2d) _(since 2026-10-08)_
- **P0** [feed] Feed data/real_estate.json is STALE (6d old, limit 2d) _(since 2026-10-08)_
- **P1** [schedule] Scheduled workflow etf-flows-daily: last scheduled run failed (2026-10-11) — cron 15 9 * * * ([run](https://github.com/btabiado/alpine-data/actions/runs/38152526725)) _(since 2026-10-11)_
- **P1** [schedule] Scheduled workflow lthcs-beta-verdict-monthly: last scheduled run failed (2026-10-01) — cron 0 8 1 * * ([run](https://github.com/btabiado/alpine-data/actions/runs/36882479373)) _(since 2026-10-05)_
- **P1** [schedule] Scheduled workflow lthcs-quality-audit-monthly: last scheduled run failed (2026-10-01) — cron 0 9 1 * * ([run](https://github.com/btabiado/alpine-data/actions/runs/36887779801)) _(since 2026-10-05)_
- **P2** [api] Upstream APIs blocked (2): Farside (ETF flows), NUFORC _(since 2026-10-06)_
- **P2** [api] Upstream APIs degraded (1): DeFiLlama bridges _(since 2026-10-05)_
- **P2** [schedule] Crons that lose most of their ticks (3): aviation-opensky 18%, lthcs-news-hourly 18%, pages 17% _(since 2026-10-05)_
- **P2** [schedule] Crons starting >3h late (median delay) (18): aviation-tsa 5.3h, catalog-health 9.7h, cfpb-daily 7.0h, city-daily 6.9h, codeql 6.5h, daily-audit 6.9h, data-health 5.1h, etf-flows-daily 7.0h, lthcs-backtest-daily 3.0h, lthcs-crypto-daily 5.8h, lthcs-trends-daily 6.8h, lthcs-trends-weekly 7.0h, lthcs-validate-weekly 7.8h, money-flow-daily 7.0h, real-estate-daily 7.0h, security-audit 9.4h, trufflehog-weekly 6.5h, usaspending-daily 7.0h _(since 2026-10-05)_

## Fixed since yesterday (2)
- **P1** [schedule] Scheduled workflow lthcs-validate-weekly: last scheduled run failed (2026-10-05) — cron 0 5 * * 1 ([run](https://github.com/btabiado/alpine-data/actions/runs/37306933010))
- **P2** [schedule] Crons starting >3h late (median delay) (1): secrets-check 6.9h

## Snapshot

- **Feeds** (data_health, committed): 2 disclosed, 1 gap, 26 ok, 14 skipped, 4 stale, 1 suppressed
  - muted: data-mufon.json (muted until 2027-01-04: NUFORC blocks automated access (Cloudflare ch…)
- **Workflows** (last 24h, main): 19 ran, 2 ended failed (cfpb-daily, lthcs-validate-weekly)
- **API status** (2026-10-12T18:57:00+00:00): 2 auth_required, 2 blocked, 1 degraded, 50 up
  - no verdict changes vs. git:5d914b2fb1 (2026-10-11T12:34:47+00:00)

**Spot checks** (live site vs. primary sources)

| check | status | detail |
|---|---|---|
| live_site_build_age | ok | built 2026-10-12T18:56:11Z (7m ago) |
| btc_price_vs_coingecko | ok | site 82282.59 vs 82402.0 (0.145%) |
| eth_price_vs_coingecko | ok | site 2508.02 vs 2512.64 (0.184%) |
| fear_greed_vs_alternative_me | ok | 2026-10-12: site 69 vs 69 |
| btc_tip_height_vs_mempool_space | ok | site 971127 vs 971127 (lag 0 blocks) |
| treasury_10y_vs_treasury_gov | ok | 2026-10-08: site 5.22 vs 5.22 |

**Live payload stamps**: 9 of 15 stamped within 48h, the rest:

| payload | status | data as_of | generated |
|---|---|---|---|
| data-aviation.json | ok | 2025-12-31 (286d) | – |
| data-city.json | ok | 2026-09-01 (42d) | 2026-10-12T16:18 (3h) |
| data-mufon.json | ok | – | 2026-10-06T12:22 (6d) |
| data-stock-money-flow.json | ok | 2026-10-09 (4d) | – |
| data-tsa.json | ok | 2026-10-08 (5d) | 2026-10-09T19:27 (3d) |
| data-us_states.json | ok | – | – |

## Schedules (last 7 days)

| workflow | cron (UTC) | days run/due | ticks run | median delay | last scheduled run | flag |
|---|---|---|---|---|---|---|
| aviation-opensky | 25 * * * * | 8/8 | 18% | 0.3h | 2026-10-12T15:44 success | ok |
| aviation-tsa | 10 14 * * * | 6/6 | 100% | 5.3h | 2026-10-11T18:47 success | ok |
| catalog-health | 0 8 * * 1 | 1/1 | 100% | 9.7h | 2026-10-12T17:40 success | ok |
| cfpb-daily | 0 7 * * * | 7/7 | 100% | 7.0h | 2026-10-12T16:23 failure | FAILING |
| city-daily | 0 7 * * * | 7/7 | 100% | 6.9h | 2026-10-12T16:11 success | ok |
| codeql | 0 4 * * 0 | 1/1 | 100% | 6.5h | 2026-10-11T10:28 success | ok |
| crypto-odds-daily | 15 14 * * * | 0/0 | – | – | never | ok |
| daily-audit | 17 9 * * * | 6/6 | 100% | 6.9h | 2026-10-11T15:39 success | ok |
| data-health | 0 15 * * * | 6/6 | 100% | 5.1h | 2026-10-11T19:25 success | ok |
| etf-flows-daily | 15 9 * * * | 6/6 | 100% | 7.0h | 2026-10-11T15:44 failure | FAILING |
| lthcs-backtest-daily | 30 23 * * * | 7/7 | 100% | 3.0h | 2026-10-12T02:31 success | ok |
| lthcs-backtest-monthly | 0 6 1 * * | 0/0 | – | – | 2026-10-01T11:52 success | ok |
| lthcs-beta-verdict-monthly | 0 8 1 * * | 0/0 | – | – | 2026-10-01T15:12 failure | FAILING |
| lthcs-crypto-daily | 47 11 * * * | 6/6 | 100% | 5.8h | 2026-10-11T16:47 success | ok |
| lthcs-daily | 0 23 * * * | 7/7 | 100% | 3.0h | 2026-10-12T01:53 success | ok |
| lthcs-news-hourly | 0 * * * * | 8/8 | 18% | 0.2h | 2026-10-12T16:27 success | ok |
| lthcs-quality-audit-monthly | 0 9 1 * * | 0/0 | – | – | 2026-10-01T15:53 failure | FAILING |
| lthcs-trends-daily | 13 4 * * * | 7/7 | 100% | 6.8h | 2026-10-12T11:52 success | ok |
| lthcs-trends-weekly | 47 2 * * 1 | 1/1 | 100% | 7.0h | 2026-10-12T09:46 success | ok |
| lthcs-tune-weights-monthly | 0 7 1 * * | 0/0 | – | – | 2026-10-01T14:10 success | ok |
| lthcs-validate-weekly | 0 5 * * 1 | 1/1 | 100% | 7.8h | 2026-10-12T12:46 failure | FAILING |
| money-flow-daily | 30 8 * * * | 7/7 | 100% | 7.0h | 2026-10-12T18:07 success | ok |
| pages | 0 * * * * | 7/8 | 17% | 0.2h | 2026-10-12T18:54 success | MISSED 1d |
| real-estate-daily | 0 6 * * * | 7/7 | 100% | 7.0h | 2026-10-12T14:53 success | ok |
| secrets-check | 5 15 * * 1 | 0/0 | – | – | 2026-10-05T21:57 success | ok |
| security-audit | 0 9 * * 1 | 1/1 | 100% | 9.4h | 2026-10-12T18:24 success | ok |
| trufflehog-weekly | 0 3 * * 0 | 1/1 | 100% | 6.5h | 2026-10-11T09:32 success | ok |
| usaspending-daily | 30 7 * * * | 7/7 | 100% | 7.0h | 2026-10-12T16:52 success | ok |

## UX (live site)

| page | viewport | HTTP | weight / requests | JS exceptions | console errors | failed requests | overflow | tabs tappable |
|---|---|---|---|---|---|---|---|---|
| / | phone | 200 | 1.9 MB / 28 | 0 | 0 | 0 | no | 21/21 |
| / | desktop | 200 | 1.9 MB / 28 | 0 | 0 | 0 | no | 21/21 |
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

UX audit took 35s.
