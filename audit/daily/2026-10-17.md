# Daily audit 2026-10-17 — AMBER

**AMBER** · 0 P0 · 5 P1 · 24 P2 · 3 new · 7 fixed (vs. 2026-10-16)

Generated 2026-10-17T13:00:10Z by [daily-audit](https://github.com/btabiado/alpine-data/actions/runs/39034629814). Machine-readable: `audit/daily/2026-10-17.json`.

## New since yesterday (3)
- **P1** [schedule] Scheduled workflow aviation-opensky missed 1 day(s) in the last week — cron 25 * * * *; no scheduled run on: 2026-10-17
- **P1** [schedule] Scheduled workflow etf-flows-daily: last scheduled run failed (2026-10-16) — cron 15 9 * * * ([run](https://github.com/btabiado/alpine-data/actions/runs/38912040330))
- **P1** [schedule] Scheduled workflow pages missed 1 day(s) in the last week — cron 0 * * * *; no scheduled run on: 2026-10-17

## Ongoing (26)
- **P1** [schedule] Scheduled workflow lthcs-beta-verdict-monthly: last scheduled run failed (2026-10-01) — cron 0 8 1 * * ([run](https://github.com/btabiado/alpine-data/actions/runs/36882479373)) _(since 2026-10-05)_
- **P1** [schedule] Scheduled workflow lthcs-validate-weekly: last scheduled run failed (2026-10-12) — cron 0 5 * * 1 ([run](https://github.com/btabiado/alpine-data/actions/runs/38251149250)) _(since 2026-10-13)_
- **P2** [api] Upstream APIs blocked (1): Farside (ETF flows) _(since 2026-10-06)_
- **P2** [api] Upstream APIs degraded (1): DeFiLlama bridges _(since 2026-10-05)_
- **P2** [schedule] Crons that lose most of their ticks (3): aviation-opensky 20%, lthcs-news-hourly 20%, pages 19% _(since 2026-10-05)_
- **P2** [schedule] Crons starting >3h late (median delay) (19): aviation-tsa 4.3h, catalog-health 9.7h, cfpb-daily 6.7h, city-daily 6.6h, codeql 6.5h, crypto-odds-daily 4.4h, daily-audit 6.4h, data-health 4.2h, etf-flows-daily 6.5h, lthcs-crypto-daily 5.0h, lthcs-trends-daily 6.5h, lthcs-trends-weekly 7.0h, lthcs-validate-weekly 7.8h, money-flow-daily 6.5h, real-estate-daily 6.4h, secrets-check 7.4h, security-audit 9.4h, trufflehog-weekly 6.5h, usaspending-daily 6.8h _(since 2026-10-05)_

## Fixed since yesterday (7)
- **P0** [feed] Feed R2 archive [history: daily payload copies] is GAP (? old, limit ?) — missing 90: v2/data-cpi.json: 2026-10-07, v2/data-cpi.json: 2026-10-08, v2/data-cpi.json: 2026-10-09, v2/data-cpi.json: 2026-10-10, v2/data-cpi.json: 2026-10-11, v2/data-cpi.json: 2026-10-12, v2/data…
- **P0** [feed] Feed data/btc_flows.csv is STALE (9d old, limit 2d)
- **P0** [feed] Feed data/eth_flows.csv is STALE (9d old, limit 2d)
- **P0** [workflow] Workflow etf-flows-daily failed two days running — step 'Fetch BTC + ETH spot-ETF flows' — Process completed with exit code 1. ([run](https://github.com/btabiado/alpine-data/actions/runs/38749113718))
- **P1** [api] Upstream API USGS ScienceBase is DOWN
- **P1** [schedule] Scheduled workflow lthcs-quality-audit-monthly: last scheduled run failed (2026-10-01) — cron 0 9 1 * * ([run](https://github.com/btabiado/alpine-data/actions/runs/36887779801))
- **P2** [api] Upstream APIs blocked (1): NUFORC

## Snapshot

- **LTHCS quality audit** (data/lthcs/quality_audit/latest_summary.json): DEGRADED. thesis_integrity is KNOWN GAP (accepted) (decided 2026-10-17, review by 2027-04-17)
- **Feeds** (data_health, committed): 2 disclosed, 32 ok, 15 skipped, 1 suppressed
  - muted: data-mufon.json (muted until 2027-01-04: NUFORC permitted aggregated totals only. Raw …)
- **Workflows** (last 24h, main): 25 ran, 0 ended failed
- **API status** (2026-10-17T11:23:12+00:00): 2 auth_required, 1 blocked, 1 degraded, 50 up
  - changed vs. git:5232492abf (2026-10-16T12:19:52+00:00): NUFORC: blocked → None; USGS ScienceBase: down → up

**Spot checks** (live site vs. primary sources)

| check | status | detail |
|---|---|---|
| live_site_build_age | ok | built 2026-10-17T11:21:53Z (2h ago) |
| btc_price_vs_coingecko | ok | site 81473.32 vs 81415.0 (0.072%) |
| eth_price_vs_coingecko | ok | site 2432.16 vs 2423.1 (0.374%) |
| fear_greed_vs_alternative_me | ok | 2026-10-17: site 55 vs 55 |
| btc_tip_height_vs_mempool_space | ok | site 971846 vs 971863 (lag 17 blocks) |
| treasury_10y_vs_treasury_gov | ok | 2026-10-15: site 5.1 vs 5.1 |

**Live payload stamps**: 10 of 15 stamped within 48h, the rest:

| payload | status | data as_of | generated |
|---|---|---|---|
| data-aviation.json | ok | 2025-12-31 (291d) | – |
| data-city.json | ok | 2026-09-01 (47d) | 2026-10-17T11:17 (2h) |
| data-mufon.json | ok | – | 2026-10-06T12:22 (11d) |
| data-tsa.json | ok | 2026-10-15 (3d) | 2026-10-16T17:57 (19h) |
| data-us_states.json | ok | – | – |

## Schedules (last 7 days)

| workflow | cron (UTC) | days run/due | ticks run | median delay | last scheduled run | flag |
|---|---|---|---|---|---|---|
| aviation-opensky | 25 * * * * | 7/8 | 20% | 0.3h | 2026-10-17T09:29 success | MISSED 1d |
| aviation-tsa | 10 14 * * * | 7/7 | 100% | 4.3h | 2026-10-16T17:57 success | ok |
| catalog-health | 0 8 * * 1 | 1/1 | 100% | 9.7h | 2026-10-12T17:40 success | ok |
| cfpb-daily | 0 7 * * * | 6/6 | 100% | 6.7h | 2026-10-17T11:18 success | ok |
| city-daily | 0 7 * * * | 6/6 | 100% | 6.6h | 2026-10-17T11:12 success | ok |
| codeql | 0 4 * * 0 | 1/1 | 100% | 6.5h | 2026-10-11T10:28 success | ok |
| crypto-odds-daily | 15 14 * * * | 5/5 | 100% | 4.4h | 2026-10-16T18:00 success | ok |
| daily-audit | 17 9 * * * | 6/6 | 100% | 6.4h | 2026-10-16T13:57 success | ok |
| data-health | 0 15 * * * | 7/7 | 100% | 4.2h | 2026-10-16T18:42 success | ok |
| etf-flows-daily | 15 9 * * * | 6/6 | 100% | 6.5h | 2026-10-16T13:58 failure | FAILING |
| lthcs-backtest-daily | 30 23 * * * | 7/7 | 100% | 2.5h | 2026-10-17T01:22 success | ok |
| lthcs-backtest-monthly | 0 6 1 * * | 0/0 | – | – | 2026-10-01T11:52 success | ok |
| lthcs-beta-verdict-monthly | 0 8 1 * * | 0/0 | – | – | 2026-10-01T15:12 failure | FAILING |
| lthcs-crypto-daily | 47 11 * * * | 6/6 | 100% | 5.0h | 2026-10-16T15:35 success | ok |
| lthcs-daily | 0 23 * * * | 7/7 | 100% | 2.4h | 2026-10-17T00:53 success | ok |
| lthcs-news-hourly | 0 * * * * | 8/8 | 20% | 0.2h | 2026-10-17T11:19 success | ok |
| lthcs-quality-audit-monthly | 0 9 1 * * | 0/0 | – | – | 2026-10-01T15:53 failure | ok |
| lthcs-trends-daily | 13 4 * * * | 6/6 | 100% | 6.5h | 2026-10-17T08:30 success | ok |
| lthcs-trends-weekly | 47 2 * * 1 | 1/1 | 100% | 7.0h | 2026-10-12T09:46 success | ok |
| lthcs-tune-weights-monthly | 0 7 1 * * | 0/0 | – | – | 2026-10-01T14:10 success | ok |
| lthcs-validate-weekly | 0 5 * * 1 | 1/1 | 100% | 7.8h | 2026-10-12T12:46 failure | FAILING |
| money-flow-daily | 30 8 * * * | 6/6 | 100% | 6.5h | 2026-10-17T12:19 success | ok |
| pages | 0 * * * * | 7/8 | 19% | 0.2h | 2026-10-17T12:51 cancelled | MISSED 1d |
| real-estate-daily | 0 6 * * * | 6/6 | 100% | 6.4h | 2026-10-17T10:22 success | ok |
| secrets-check | 5 15 * * 1 | 1/1 | 100% | 7.4h | 2026-10-12T22:28 success | ok |
| security-audit | 0 9 * * 1 | 1/1 | 100% | 9.4h | 2026-10-12T18:24 success | ok |
| trufflehog-weekly | 0 3 * * 0 | 1/1 | 100% | 6.5h | 2026-10-11T09:32 success | ok |
| usaspending-daily | 30 7 * * * | 6/6 | 100% | 6.8h | 2026-10-17T11:41 success | ok |

## UX (live site)

| page | viewport | HTTP | weight / requests | JS exceptions | console errors | failed requests | overflow | tabs tappable |
|---|---|---|---|---|---|---|---|---|
| / | phone | 200 | 1.8 MB / 27 | 0 | 0 | 0 | no | 21/21 |
| / | desktop | 200 | 1.8 MB / 27 | 0 | 0 | 0 | no | 21/21 |
| /summit/ | phone | 200 | 0.4 MB / 1 | 0 | 0 | 0 | no | – |
| /summit/ | desktop | 200 | 0.4 MB / 1 | 0 | 0 | 0 | no | – |
| /health/ | phone | 200 | 0.0 MB / 4 | 0 | 0 | 0 | no | – |
| /health/ | desktop | 200 | 0.0 MB / 4 | 0 | 0 | 0 | no | – |
| /real-estate/ | phone | 200 | 1.2 MB / 6 | 0 | 0 | 0 | no | – |
| /real-estate/ | desktop | 200 | 1.2 MB / 24 | 0 | 0 | 0 | no | – |
| /lthcs/ | phone | 200 | 0.4 MB / 39 | 0 | 0 | 0 | no | – |
| /lthcs/ | desktop | 200 | 0.4 MB / 39 | 0 | 0 | 0 | no | – |
| /api-universe/ | phone | 200 | 0.4 MB / 6 | 0 | 0 | 0 | no | – |
| /api-universe/ | desktop | 200 | 0.4 MB / 6 | 0 | 0 | 0 | no | – |

UX audit took 37s.
