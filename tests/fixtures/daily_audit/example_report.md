# Daily audit 2026-10-04 — RED

**RED** · 20 P0 · 7 P1 · 29 P2 · 56 new · 0 fixed (no previous report)

Generated 2026-10-04T22:20:29Z. Machine-readable: `audit/daily/2026-10-04.json`.

## New since yesterday (56)
- **P0** [ux] V1 (/) @ phone: tabs a user cannot tap (tap target covered) (19): Aviation, City, CPI, DeFi, ETF Flows, Metals, Money Flow, UAP, Overview, Point of Control, Real Estate, Signals, Research, Stock Flows, Stocks, Supplies, Futures, Travel Advisories, Whale
- **P0** [workflow] Workflow aviation-tsa failed two days running — step 'Fetch TSA throughput snapshot' — TSA not refreshed: tsa.gov: HTTPError: HTTP Error 403: Forbidden; web.archive.org: ValueError: no rows parsed from snapshot 20261004121514 ([run](https://github.com/btabiado/alpine-data/actions/runs/37223025786))
- **P1** [schedule] Scheduled workflow lthcs-beta-verdict-monthly: last scheduled run failed (2026-10-01) — cron 0 8 1 * * ([run](https://github.com/btabiado/alpine-data/actions/runs/36882479373))
- **P1** [schedule] Scheduled workflow lthcs-quality-audit-monthly: last scheduled run failed (2026-10-01) — cron 0 9 1 * * ([run](https://github.com/btabiado/alpine-data/actions/runs/36887779801))
- **P1** [schedule] Scheduled workflow lthcs-validate-weekly: last scheduled run failed (2026-09-28) — cron 0 5 * * 1 ([run](https://github.com/btabiado/alpine-data/actions/runs/36415761128))
- **P1** [schedule] Scheduled workflow security-audit: last scheduled run failed (2026-09-28) — cron 0 9 * * 1 ([run](https://github.com/btabiado/alpine-data/actions/runs/36456505370))
- **P1** [ux] /health/: same-origin request /api/status -> 404 — during load
- **P1** [ux] /lthcs/: same-origin request /alpine-data/data/lthcs/macro/sector_strength_2026-10-04.json -> 404 — during load
- **P1** [workflow] Workflow data-health failed in the last 24h — step 'Build issue body' — Process completed with exit code 1. ([run](https://github.com/btabiado/alpine-data/actions/runs/37224950186))
- **P2** [api] Upstream APIs blocked (1): NUFORC
- **P2** [api] Upstream APIs degraded (1): DeFiLlama bridges
- **P2** [schedule] Crons that lose most of their ticks (3): aviation-opensky 18%, lthcs-news-hourly 19%, pages 18%
- **P2** [schedule] Crons starting >3h late (median delay) (19): aviation-tsa 5.2h, catalog-health 8.7h, cfpb-daily 6.5h, city-daily 6.3h, codeql 6.3h, etf-flows-daily 6.7h, lthcs-backtest-monthly 5.9h, lthcs-beta-verdict-monthly 7.2h, lthcs-quality-audit-monthly 6.9h, lthcs-trends-daily 6.3h, lthcs-trends-weekly 6.4h, lthcs-tune-weights-monthly 7.2h, lthcs-validate-weekly 6.5h, money-flow-daily 6.5h, real-estate-daily 6.2h, secrets-check 6.1h, security-audit 8.2h, trufflehog-weekly 6.2h, usaspending-daily 6.5h
- **P2** [ux] /lthcs/: console error — The Content Security Policy directive 'frame-ancestors' is ignored when delivered via a <meta> element. — during load
- **P2** [ux] /lthcs/: heavy initial load — 245 requests, 0.7 MB
- **P2** [ux] /lthcs/: horizontal overflow at 390px (page is 645px wide) — widest elements: table.lthcs-index-table
- **P2** [ux] /real-estate/: horizontal overflow at 390px (page is 765px wide) — widest elements: table
- **P2** [ux] V1 (/) @ phone: tabs wider than 390px (1): Stock Flows

## Ongoing (0)
- none

## Fixed since yesterday (0)
- none

## Snapshot

- **Feeds** (data_health, committed): 16 ok, 7 skipped, 2 suppressed
  - muted: data-mufon.json (muted until 2027-01-04: NUFORC blocks automated access (Cloudflare ch…); data-tsa.json (muted until 2026-10-18: tsa.gov (Akamai) 403s datacenter IPs incl. Gi…)
- **Workflows** (last 24h, main): 20 ran, 2 ended failed (aviation-tsa, data-health)
- **API status** (2026-10-04T20:29:00+00:00): 8 auth_required, 1 blocked, 1 degraded, 42 up
  - no snapshot from ~24h earlier to compare against yet

**Spot checks** (live site vs. primary sources)

| check | status | detail |
|---|---|---|
| live_site_build_age | ok | built 2026-10-04T21:11:18Z (1h ago) |
| btc_price_vs_coingecko | ok | site 85873.37 vs 86487.0 (0.71%) |
| eth_price_vs_coingecko | ok | site 2706.51 vs 2718.84 (0.454%) |
| fear_greed_vs_alternative_me | ok | 2026-10-04: site 65 vs 65 |
| btc_tip_height_vs_mempool_space | ok | site 969901 vs 969911 (lag 10 blocks) |
| treasury_10y_vs_treasury_gov | ok | 2026-10-01: site 5.24 vs 5.24 |

**Live payload stamps**: 10 of 15 stamped within 48h, the rest:

| payload | status | data as_of | generated |
|---|---|---|---|
| data-aviation.json | ok | 2025-12-31 (278d) | – |
| data-city.json | ok | 2026-09-01 (34d) | 2026-10-04T13:02 (9h) |
| data-stock-money-flow.json | ok | 2026-10-02 (3d) | – |
| data-tsa.json | ok | – | 2026-06-18T17:22 (108d) |
| data-us_states.json | ok | – | – |

## Schedules (last 7 days)

| workflow | cron (UTC) | days run/due | ticks run | median delay | last scheduled run | flag |
|---|---|---|---|---|---|---|
| aviation-opensky | 25 * * * * | 8/8 | 18% | 0.5h | 2026-10-04T21:38 success | ok |
| aviation-tsa | 10 14 * * * | 6/6 | 100% | 5.2h | 2026-10-04T18:05 failure | FAILING |
| catalog-health | 0 8 * * 1 | 1/1 | 100% | 8.7h | 2026-09-28T16:39 success | ok |
| cfpb-daily | 0 7 * * * | 7/7 | 100% | 6.5h | 2026-10-04T12:59 success | ok |
| city-daily | 0 7 * * * | 7/7 | 100% | 6.3h | 2026-10-04T12:53 success | ok |
| codeql | 0 4 * * 0 | 1/1 | 100% | 6.3h | 2026-10-04T10:19 success | ok |
| data-health | 0 15 * * * | 0/0 | – | – | 2026-10-04T18:34 failure | FAILING |
| etf-flows-daily | 15 9 * * * | 7/7 | 100% | 6.7h | 2026-10-04T14:47 success | ok |
| lthcs-backtest-daily | 30 23 * * * | 7/7 | 100% | 2.8h | 2026-10-04T02:46 success | ok |
| lthcs-backtest-monthly | 0 6 1 * * | 1/1 | 100% | 5.9h | 2026-10-01T11:52 success | ok |
| lthcs-beta-verdict-monthly | 0 8 1 * * | 1/1 | 100% | 7.2h | 2026-10-01T15:12 failure | FAILING |
| lthcs-crypto-daily | 0 22 * * * | 6/6 | 100% | 3.0h | 2026-10-04T00:17 success | ok |
| lthcs-daily | 0 23 * * * | 7/7 | 100% | 2.7h | 2026-10-04T02:13 success | ok |
| lthcs-news-hourly | 0 * * * * | 8/8 | 19% | 0.3h | 2026-10-04T18:59 success | ok |
| lthcs-quality-audit-monthly | 0 9 1 * * | 1/1 | 100% | 6.9h | 2026-10-01T15:53 failure | FAILING |
| lthcs-trends-daily | 0 4 * * * | 7/7 | 100% | 6.3h | 2026-10-04T10:20 success | ok |
| lthcs-trends-weekly | 0 4 * * 1 | 1/1 | 100% | 6.4h | 2026-09-28T10:22 success | ok |
| lthcs-tune-weights-monthly | 0 7 1 * * | 1/1 | 100% | 7.2h | 2026-10-01T14:10 success | ok |
| lthcs-validate-weekly | 0 5 * * 1 | 1/1 | 100% | 6.5h | 2026-09-28T11:27 failure | FAILING |
| money-flow-daily | 30 8 * * * | 7/7 | 100% | 6.5h | 2026-10-04T14:16 success | ok |
| pages | 0 * * * * | 8/8 | 18% | 0.3h | 2026-10-04T19:27 success | ok |
| real-estate-daily | 0 6 * * * | 7/7 | 100% | 6.2h | 2026-10-04T12:02 success | ok |
| secrets-check | 5 15 * * 1 | 1/1 | 100% | 6.1h | 2026-09-28T21:11 success | ok |
| security-audit | 0 9 * * 1 | 1/1 | 100% | 8.2h | 2026-09-28T17:12 failure | FAILING |
| trufflehog-weekly | 0 3 * * 0 | 1/1 | 100% | 6.2h | 2026-10-04T09:10 success | ok |
| usaspending-daily | 30 7 * * * | 7/7 | 100% | 6.5h | 2026-10-04T13:20 success | ok |

## UX (live site)

| page | viewport | HTTP | weight / requests | JS exceptions | console errors | failed requests | overflow | tabs tappable |
|---|---|---|---|---|---|---|---|---|
| / | phone | 200 | 1.6 MB / 31 | 0 | 0 | 0 | no | 2/21 |
| / | desktop | 200 | 1.6 MB / 31 | 0 | 0 | 0 | no | 21/21 |
| /v2/ | phone | 200 | 1.5 MB / 29 | 0 | 0 | 0 | no | 15/15 |
| /v2/ | desktop | 200 | 1.5 MB / 29 | 0 | 0 | 0 | no | 15/15 |
| /summit/ | phone | 200 | 0.4 MB / 1 | 0 | 0 | 0 | no | – |
| /summit/ | desktop | 200 | 0.4 MB / 1 | 0 | 0 | 0 | no | – |
| /health/ | phone | 200 | 0.0 MB / 5 | 0 | 0 | 1 | no | – |
| /health/ | desktop | 200 | 0.0 MB / 5 | 0 | 0 | 1 | no | – |
| /real-estate/ | phone | 200 | 1.2 MB / 6 | 0 | 0 | 0 | yes | – |
| /real-estate/ | desktop | 200 | 1.2 MB / 6 | 0 | 0 | 0 | no | – |
| /lthcs/ | phone | 200 | 0.7 MB / 245 | 0 | 1 | 1 | yes | – |
| /lthcs/ | desktop | 200 | 0.7 MB / 245 | 0 | 1 | 1 | no | – |

UX audit took 80s.
