# Daily audit 2026-10-11 — RED

**RED** · 5 P0 · 4 P1 · 25 P2 · 2 new · 3 fixed (vs. 2026-10-10)

Generated 2026-10-11T15:41:58Z by [daily-audit](https://github.com/btabiado/alpine-data/actions/runs/38152224791). Machine-readable: `audit/daily/2026-10-11.json`.

## New since yesterday (2)
- **P0** [feed] Feed data-tsa.json is STALE (3d old, limit 2d)
- **P1** [schedule] Scheduled workflow etf-flows-daily: last scheduled run failed (2026-10-10) — cron 15 9 * * * ([run](https://github.com/btabiado/alpine-data/actions/runs/38063435029))

## Ongoing (32)
- **P0** [feed] Feed R2 archive [history: daily payload copies] is GAP (? old, limit ?) — missing 45: v2/data-cpi.json: 2026-10-07, v2/data-cpi.json: 2026-10-08, v2/data-cpi.json: 2026-10-09, v2/data-cpi.json: 2026-10-10, v2/data-cpi.json: 2026-10-11, v2/data-defi.json: 2026-10-07, v2/dat… _(since 2026-10-07)_
- **P0** [feed] Feed data/btc_flows.csv is STALE (4d old, limit 2d) _(since 2026-10-08)_
- **P0** [feed] Feed data/eth_flows.csv is STALE (4d old, limit 2d) _(since 2026-10-08)_
- **P0** [feed] Feed data/real_estate.json is STALE (5d old, limit 2d) _(since 2026-10-08)_
- **P1** [schedule] Scheduled workflow lthcs-beta-verdict-monthly: last scheduled run failed (2026-10-01) — cron 0 8 1 * * ([run](https://github.com/btabiado/alpine-data/actions/runs/36882479373)) _(since 2026-10-05)_
- **P1** [schedule] Scheduled workflow lthcs-quality-audit-monthly: last scheduled run failed (2026-10-01) — cron 0 9 1 * * ([run](https://github.com/btabiado/alpine-data/actions/runs/36887779801)) _(since 2026-10-05)_
- **P1** [schedule] Scheduled workflow lthcs-validate-weekly: last scheduled run failed (2026-10-05) — cron 0 5 * * 1 ([run](https://github.com/btabiado/alpine-data/actions/runs/37306933010)) _(since 2026-10-06)_
- **P2** [api] Upstream APIs blocked (2): Farside (ETF flows), NUFORC _(since 2026-10-06)_
- **P2** [api] Upstream APIs degraded (1): DeFiLlama bridges _(since 2026-10-05)_
- **P2** [schedule] Crons that lose most of their ticks (3): aviation-opensky 18%, lthcs-news-hourly 18%, pages 17% _(since 2026-10-05)_
- **P2** [schedule] Crons starting >3h late (median delay) (19): aviation-tsa 5.7h, catalog-health 9.0h, cfpb-daily 7.1h, city-daily 7.0h, codeql 6.5h, daily-audit 7.2h, data-health 5.5h, etf-flows-daily 7.3h, lthcs-backtest-daily 3.0h, lthcs-crypto-daily 6.2h, lthcs-trends-daily 6.8h, lthcs-trends-weekly 8.3h, lthcs-validate-weekly 7.1h, money-flow-daily 7.2h, real-estate-daily 7.0h, secrets-check 6.9h, security-audit 8.6h, trufflehog-weekly 6.5h, usaspending-daily 7.1h _(since 2026-10-05)_

## Fixed since yesterday (3)
- **P0** [workflow] Workflow etf-flows-daily failed two days running — step 'Fetch BTC + ETH spot-ETF flows' — Process completed with exit code 1. ([run](https://github.com/btabiado/alpine-data/actions/runs/37957764568))
- **P1** [schedule] Scheduled workflow lthcs-crypto-daily missed 1 day(s) in the last week — cron 47 11 * * *; no scheduled run on: 2026-10-04
- **P2** [schedule] Crons starting >3h late (median delay) (1): lthcs-daily 3.2h

## Snapshot

- **Feeds** (data_health, committed): 2 disclosed, 1 gap, 26 ok, 14 skipped, 4 stale, 1 suppressed
  - muted: data-mufon.json (muted until 2027-01-04: NUFORC blocks automated access (Cloudflare ch…)
- **Workflows** (last 24h, main): 16 ran, 0 ended failed
- **API status** (2026-10-11T12:34:47+00:00): 2 auth_required, 2 blocked, 1 degraded, 50 up
  - no verdict changes vs. git:c5f5285b0b (2026-10-10T15:13:13+00:00)

**Spot checks** (live site vs. primary sources)

| check | status | detail |
|---|---|---|
| live_site_build_age | ok | built 2026-10-11T12:33:58Z (3h ago) |
| btc_price_vs_coingecko | ok | site 83060.76 vs 83210.0 (0.179%) |
| eth_price_vs_coingecko | ok | site 2502.94 vs 2510.69 (0.309%) |
| fear_greed_vs_alternative_me | ok | 2026-10-11: site 61 vs 61 |
| btc_tip_height_vs_mempool_space | ok | site 970930 vs 970949 (lag 19 blocks) |
| treasury_10y_vs_treasury_gov | ok | 2026-10-08: site 5.22 vs 5.22 |

**Live payload stamps**: 9 of 15 stamped within 48h, the rest:

| payload | status | data as_of | generated |
|---|---|---|---|
| data-aviation.json | ok | 2025-12-31 (285d) | – |
| data-city.json | ok | 2026-09-01 (41d) | 2026-10-10T13:12 (26h) |
| data-mufon.json | ok | – | 2026-10-06T12:22 (5d) |
| data-stock-money-flow.json | ok | 2026-10-09 (3d) | – |
| data-tsa.json | ok | 2026-10-08 (4d) | 2026-10-09T19:27 (44h) |
| data-us_states.json | ok | – | – |

## Schedules (last 7 days)

| workflow | cron (UTC) | days run/due | ticks run | median delay | last scheduled run | flag |
|---|---|---|---|---|---|---|
| aviation-opensky | 25 * * * * | 8/8 | 18% | 0.3h | 2026-10-11T09:03 success | ok |
| aviation-tsa | 10 14 * * * | 6/6 | 100% | 5.7h | 2026-10-10T18:28 success | ok |
| catalog-health | 0 8 * * 1 | 1/1 | 100% | 9.0h | 2026-10-05T17:01 success | ok |
| cfpb-daily | 0 7 * * * | 6/6 | 100% | 7.1h | 2026-10-11T13:42 success | ok |
| city-daily | 0 7 * * * | 6/6 | 100% | 7.0h | 2026-10-11T13:35 success | ok |
| codeql | 0 4 * * 0 | 1/1 | 100% | 6.5h | 2026-10-11T10:28 success | ok |
| daily-audit | 17 9 * * * | 6/6 | 100% | 7.2h | 2026-10-10T15:22 success | ok |
| data-health | 0 15 * * * | 6/6 | 100% | 5.5h | 2026-10-10T19:11 success | ok |
| etf-flows-daily | 15 9 * * * | 6/6 | 100% | 7.3h | 2026-10-10T15:23 failure | FAILING |
| lthcs-backtest-daily | 30 23 * * * | 7/7 | 100% | 3.0h | 2026-10-11T02:00 success | ok |
| lthcs-backtest-monthly | 0 6 1 * * | 0/0 | – | – | 2026-10-01T11:52 success | ok |
| lthcs-beta-verdict-monthly | 0 8 1 * * | 0/0 | – | – | 2026-10-01T15:12 failure | FAILING |
| lthcs-crypto-daily | 47 11 * * * | 6/6 | 100% | 6.2h | 2026-10-10T16:23 success | ok |
| lthcs-daily | 0 23 * * * | 7/7 | 100% | 3.0h | 2026-10-11T01:24 success | ok |
| lthcs-news-hourly | 0 * * * * | 8/8 | 18% | 0.2h | 2026-10-11T10:33 success | ok |
| lthcs-quality-audit-monthly | 0 9 1 * * | 0/0 | – | – | 2026-10-01T15:53 failure | FAILING |
| lthcs-trends-daily | 13 4 * * * | 7/7 | 100% | 6.8h | 2026-10-11T10:46 success | ok |
| lthcs-trends-weekly | 47 2 * * 1 | 1/1 | 100% | 8.3h | 2026-10-05T11:03 success | ok |
| lthcs-tune-weights-monthly | 0 7 1 * * | 0/0 | – | – | 2026-10-01T14:10 success | ok |
| lthcs-validate-weekly | 0 5 * * 1 | 1/1 | 100% | 7.1h | 2026-10-05T12:03 failure | FAILING |
| money-flow-daily | 30 8 * * * | 6/6 | 100% | 7.2h | 2026-10-11T15:00 success | ok |
| pages | 0 * * * * | 8/8 | 17% | 0.2h | 2026-10-11T12:32 success | ok |
| real-estate-daily | 0 6 * * * | 6/6 | 100% | 7.0h | 2026-10-11T12:24 success | ok |
| secrets-check | 5 15 * * 1 | 1/1 | 100% | 6.9h | 2026-10-05T21:57 success | ok |
| security-audit | 0 9 * * 1 | 1/1 | 100% | 8.6h | 2026-10-05T17:37 success | ok |
| trufflehog-weekly | 0 3 * * 0 | 1/1 | 100% | 6.5h | 2026-10-11T09:32 success | ok |
| usaspending-daily | 30 7 * * * | 6/6 | 100% | 7.1h | 2026-10-11T14:15 success | ok |

## UX (live site)

| page | viewport | HTTP | weight / requests | JS exceptions | console errors | failed requests | overflow | tabs tappable |
|---|---|---|---|---|---|---|---|---|
| / | phone | 200 | 1.9 MB / 28 | 0 | 0 | 0 | no | 21/21 |
| / | desktop | 200 | 1.9 MB / 28 | 0 | 0 | 0 | no | 21/21 |
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
