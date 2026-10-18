# Daily audit 2026-10-18 — RED

**RED** · 1 P0 · 3 P1 · 23 P2 · 3 new · 5 fixed (vs. 2026-10-17)

Generated 2026-10-18T13:18:41Z by [daily-audit](https://github.com/btabiado/alpine-data/actions/runs/39130825049). Machine-readable: `audit/daily/2026-10-18.json`.

## New since yesterday (3)
- **P0** [feed] Feed data-tsa.json is STALE (3d old, limit 2d)
- **P1** [schedule] Scheduled workflow crypto-odds-daily: last scheduled run failed (2026-10-17) — cron 15 14 * * * ([run](https://github.com/btabiado/alpine-data/actions/runs/39052317800))
- **P2** [schedule] Crons starting >3h late (median delay) (1): cron-watchdog 4.7h

## Ongoing (24)
- **P1** [schedule] Scheduled workflow lthcs-beta-verdict-monthly: last scheduled run failed (2026-10-01) — cron 37 8 1 * * ([run](https://github.com/btabiado/alpine-data/actions/runs/36882479373)) _(since 2026-10-05)_
- **P1** [schedule] Scheduled workflow lthcs-validate-weekly: last scheduled run failed (2026-10-12) — cron 11 5 * * 1 ([run](https://github.com/btabiado/alpine-data/actions/runs/38251149250)) _(since 2026-10-13)_
- **P2** [api] Upstream APIs blocked (1): Farside (ETF flows) _(since 2026-10-06)_
- **P2** [api] Upstream APIs degraded (1): DeFiLlama bridges _(since 2026-10-05)_
- **P2** [schedule] Crons that lose most of their ticks (3): aviation-opensky 22%, lthcs-news-hourly 21%, pages 20% _(since 2026-10-05)_
- **P2** [schedule] Crons starting >3h late (median delay) (17): aviation-tsa 4.3h, catalog-health 9.3h, cfpb-daily 5.3h, city-daily 5.0h, crypto-odds-daily 4.4h, daily-audit 5.2h, data-health 4.1h, etf-flows-daily 5.3h, lthcs-crypto-daily 4.2h, lthcs-trends-daily 5.2h, lthcs-trends-weekly 7.0h, lthcs-validate-weekly 7.6h, money-flow-daily 5.4h, real-estate-daily 5.3h, secrets-check 7.1h, security-audit 8.9h, usaspending-daily 5.2h _(since 2026-10-05)_

## Fixed since yesterday (5)
- **P1** [schedule] Scheduled workflow aviation-opensky missed 1 day(s) in the last week — cron 25 * * * *; no scheduled run on: 2026-10-17
- **P1** [schedule] Scheduled workflow etf-flows-daily: last scheduled run failed (2026-10-16) — cron 15 9 * * * ([run](https://github.com/btabiado/alpine-data/actions/runs/38912040330))
- **P1** [schedule] Scheduled workflow pages missed 1 day(s) in the last week — cron 0 * * * *; no scheduled run on: 2026-10-17
- **P2** [schedule] Crons starting >3h late (median delay) (2): codeql 6.5h, trufflehog-weekly 6.5h

## Snapshot

- **LTHCS quality audit** (data/lthcs/quality_audit/latest_summary.json): DEGRADED. thesis_integrity is KNOWN GAP (accepted) (decided 2026-10-17, review by 2027-04-17)
- **Feeds** (data_health, committed): 2 disclosed, 31 ok, 15 skipped, 1 stale, 1 suppressed
  - muted: data-mufon.json (muted until 2027-01-04: NUFORC permitted aggregated totals only. Raw …)
- **Workflows** (last 24h, main): 21 ran, 0 ended failed
- **API status** (2026-10-18T10:57:18+00:00): 2 auth_required, 1 blocked, 1 degraded, 50 up
  - no verdict changes vs. git:c6ab621edc (2026-10-17T11:23:12+00:00)

**Spot checks** (live site vs. primary sources)

| check | status | detail |
|---|---|---|
| live_site_build_age | ok | built 2026-10-18T10:55:39Z (2h ago) |
| btc_price_vs_coingecko | ok | site 82017.33 vs 81825.0 (0.235%) |
| eth_price_vs_coingecko | ok | site 2417.54 vs 2412.02 (0.229%) |
| fear_greed_vs_alternative_me | ok | 2026-10-18: site 53 vs 53 |
| btc_tip_height_vs_mempool_space | ok | site 971983 vs 971998 (lag 15 blocks) |
| treasury_10y_vs_treasury_gov | ok | 2026-10-15: site 5.1 vs 5.1 |

**Live payload stamps**: 9 of 15 stamped within 48h, the rest:

| payload | status | data as_of | generated |
|---|---|---|---|
| data-aviation.json | ok | 2025-12-31 (292d) | – |
| data-city.json | ok | 2026-09-01 (48d) | 2026-10-17T11:17 (26h) |
| data-mufon.json | ok | – | 2026-10-06T12:22 (12d) |
| data-stock-money-flow.json | ok | 2026-10-16 (3d) | – |
| data-tsa.json | ok | 2026-10-15 (4d) | 2026-10-16T17:57 (43h) |
| data-us_states.json | ok | – | – |

## Schedules (last 7 days)

| workflow | cron (UTC) | days run/due | ticks run | median delay | last scheduled run | flag |
|---|---|---|---|---|---|---|
| aviation-opensky | 25 * * * * | 8/8 | 22% | 0.2h | 2026-10-18T11:23 success | ok |
| aviation-tsa | 10 14 * * * | 7/7 | 100% | 4.3h | 2026-10-17T17:01 success | ok |
| catalog-health | 23 8 * * 1 | 1/1 | 100% | 9.3h | 2026-10-12T17:40 success | ok |
| cfpb-daily | 17 7 * * * | 6/6 | 100% | 5.3h | 2026-10-18T12:04 success | ok |
| city-daily | 29 7 * * * | 6/6 | 100% | 5.0h | 2026-10-18T12:08 success | ok |
| codeql | 23 4 * * 0 | 0/0 | – | – | 2026-10-18T08:53 success | ok |
| cron-watchdog | 17 1 * * * ; 23 7 * * * ; 41 13 * * * ; 53 19 * * * | 2/2 | 100% | 4.7h | 2026-10-18T12:07 success | ok |
| crypto-odds-daily | 15 14 * * * | 6/6 | 100% | 4.4h | 2026-10-17T17:06 failure | FAILING |
| daily-audit | 17 9 * * * | 6/6 | 100% | 5.2h | 2026-10-17T12:58 success | ok |
| data-health | 11 15 * * * | 7/7 | 100% | 4.1h | 2026-10-17T17:48 success | ok |
| etf-flows-daily | 15 9 * * * | 6/6 | 100% | 5.3h | 2026-10-17T12:59 success | ok |
| lthcs-backtest-daily | 30 23 * * * | 7/7 | 100% | 2.1h | 2026-10-18T01:17 success | ok |
| lthcs-backtest-monthly | 19 6 1 * * | 0/0 | – | – | 2026-10-01T11:52 success | ok |
| lthcs-beta-verdict-monthly | 37 8 1 * * | 0/0 | – | – | 2026-10-01T15:12 failure | FAILING |
| lthcs-crypto-daily | 47 11 * * * | 6/6 | 100% | 4.2h | 2026-10-17T14:27 success | ok |
| lthcs-daily | 11 23 * * * | 7/7 | 100% | 1.9h | 2026-10-18T01:00 success | ok |
| lthcs-news-hourly | 19 * * * * | 8/8 | 21% | 0.2h | 2026-10-18T11:15 success | ok |
| lthcs-quality-audit-monthly | 43 9 1 * * | 0/0 | – | – | 2026-10-01T15:53 failure | ok |
| lthcs-trends-daily | 13 4 * * * | 6/6 | 100% | 5.2h | 2026-10-18T08:48 success | ok |
| lthcs-trends-weekly | 47 2 * * 1 | 1/1 | 100% | 7.0h | 2026-10-12T09:46 success | ok |
| lthcs-tune-weights-monthly | 41 7 1 * * | 0/0 | – | – | 2026-10-01T14:10 success | ok |
| lthcs-validate-weekly | 11 5 * * 1 | 1/1 | 100% | 7.6h | 2026-10-12T12:46 failure | FAILING |
| money-flow-daily | 30 8 * * * | 6/6 | 100% | 5.4h | 2026-10-18T12:44 success | ok |
| pages | 7 * * * * | 8/8 | 20% | 0.1h | 2026-10-18T10:54 success | ok |
| real-estate-daily | 13 6 * * * | 6/6 | 100% | 5.3h | 2026-10-18T10:56 success | ok |
| secrets-check | 23 15 * * 1 | 1/1 | 100% | 7.1h | 2026-10-12T22:28 success | ok |
| security-audit | 31 9 * * 1 | 1/1 | 100% | 8.9h | 2026-10-12T18:24 success | ok |
| trufflehog-weekly | 37 3 * * 0 | 0/0 | – | – | 2026-10-18T08:18 success | ok |
| usaspending-daily | 47 7 * * * | 6/6 | 100% | 5.2h | 2026-10-18T12:11 success | ok |

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

UX audit took 39s.
