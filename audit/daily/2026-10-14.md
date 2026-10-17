# Daily audit 2026-10-14 — RED

**RED** · 5 P0 · 5 P1 · 26 P2 · 3 new · 4 fixed (vs. 2026-10-13)

Generated 2026-10-14T14:32:39Z by [daily-audit](https://github.com/btabiado/alpine-data/actions/runs/38581118310). Machine-readable: `audit/daily/2026-10-14.json`.

## New since yesterday (3)
- **P0** [workflow] Workflow etf-flows-daily failed two days running — step 'Fetch BTC + ETH spot-ETF flows' — Process completed with exit code 1. ([run](https://github.com/btabiado/alpine-data/actions/runs/38434743315))
- **P1** [spot] BTC price on the site (82829.95) is 1.102% off CoinGecko (81927.0) — page=82829.95, reference=81927.0, diff_pct=1.102, tolerance_pct=1.0, page_age_h=0.96, source=api.coingecko.com simple/price
- **P1** [spot] ETH price on the site (2480.9) is 2.152% off CoinGecko (2428.63) — page=2480.9, reference=2428.63, diff_pct=2.152, tolerance_pct=1.0, page_age_h=0.96, source=api.coingecko.com simple/price

## Ongoing (33)
- **P0** [feed] Feed R2 archive [history: daily payload copies] is GAP (? old, limit ?) — missing 72: v2/data-cpi.json: 2026-10-07, v2/data-cpi.json: 2026-10-08, v2/data-cpi.json: 2026-10-09, v2/data-cpi.json: 2026-10-10, v2/data-cpi.json: 2026-10-11, v2/data-cpi.json: 2026-10-12, v2/data… _(since 2026-10-07)_
- **P0** [feed] Feed data/btc_flows.csv is STALE (7d old, limit 2d) _(since 2026-10-08)_
- **P0** [feed] Feed data/eth_flows.csv is STALE (7d old, limit 2d) _(since 2026-10-08)_
- **P0** [feed] Feed data/real_estate.json is STALE (8d old, limit 2d) _(since 2026-10-08)_
- **P1** [schedule] Scheduled workflow lthcs-beta-verdict-monthly: last scheduled run failed (2026-10-01) — cron 0 8 1 * * ([run](https://github.com/btabiado/alpine-data/actions/runs/36882479373)) _(since 2026-10-05)_
- **P1** [schedule] Scheduled workflow lthcs-quality-audit-monthly: last scheduled run failed (2026-10-01) — cron 0 9 1 * * ([run](https://github.com/btabiado/alpine-data/actions/runs/36887779801)) _(since 2026-10-05)_
- **P1** [schedule] Scheduled workflow lthcs-validate-weekly: last scheduled run failed (2026-10-12) — cron 0 5 * * 1 ([run](https://github.com/btabiado/alpine-data/actions/runs/38251149250)) _(since 2026-10-13)_
- **P2** [api] Upstream APIs blocked (2): Farside (ETF flows), NUFORC _(since 2026-10-06)_
- **P2** [api] Upstream APIs degraded (1): DeFiLlama bridges _(since 2026-10-05)_
- **P2** [schedule] Crons that lose most of their ticks (3): aviation-opensky 18%, lthcs-news-hourly 17%, pages 17% _(since 2026-10-05)_
- **P2** [schedule] Crons starting >3h late (median delay) (20): aviation-tsa 5.7h, catalog-health 9.7h, cfpb-daily 7.3h, city-daily 7.1h, codeql 6.5h, crypto-odds-daily 7.9h, daily-audit 7.2h, data-health 5.5h, etf-flows-daily 7.3h, lthcs-backtest-daily 3.0h, lthcs-crypto-daily 6.2h, lthcs-trends-daily 7.1h, lthcs-trends-weekly 7.0h, lthcs-validate-weekly 7.8h, money-flow-daily 7.3h, real-estate-daily 7.1h, secrets-check 7.4h, security-audit 9.4h, trufflehog-weekly 6.5h, usaspending-daily 7.2h _(since 2026-10-05)_

## Fixed since yesterday (4)
- **P0** [feed] Feed data-tsa.json is STALE (5d old, limit 2d)
- **P0** [spot] Live site last rebuilt 8h ago (limit 6h) — pages deploy may be stuck — generated_at=2026-10-13T09:14:37Z, age_h=7.7, max_age_h=6.0
- **P1** [workflow] Workflow etf-flows-daily failed in the last 24h — step 'Fetch BTC + ETH spot-ETF flows' — Process completed with exit code 1. ([run](https://github.com/btabiado/alpine-data/actions/runs/38295256050))
- **P2** [api] Upstream APIs degraded (1): Deribit

## Snapshot

- **Feeds** (data_health, committed): 2 disclosed, 1 gap, 27 ok, 14 skipped, 3 stale, 1 suppressed
  - muted: data-mufon.json (muted until 2027-01-04: NUFORC blocks automated access (Cloudflare ch…)
- **Workflows** (last 24h, main): 17 ran, 1 ended failed (etf-flows-daily)
- **API status** (2026-10-14T13:34:46+00:00): 2 auth_required, 2 blocked, 1 degraded, 50 up
  - changed vs. git:37102e72b0 (2026-10-13T09:15:25+00:00): Deribit: degraded → up

**Spot checks** (live site vs. primary sources)

| check | status | detail |
|---|---|---|
| live_site_build_age | ok | built 2026-10-14T13:34:06Z (56m ago) |
| btc_price_vs_coingecko | fail | site 82829.95 vs 81927.0 (1.102%) |
| eth_price_vs_coingecko | fail | site 2480.9 vs 2428.63 (2.152%) |
| fear_greed_vs_alternative_me | ok | 2026-10-14: site 62 vs 62 |
| btc_tip_height_vs_mempool_space | ok | site 971407 vs 971413 (lag 6 blocks) |
| treasury_10y_vs_treasury_gov | ok | 2026-10-09: site 5.24 vs 5.24 |

**Live payload stamps**: 10 of 15 stamped within 48h, the rest:

| payload | status | data as_of | generated |
|---|---|---|---|
| data-aviation.json | ok | 2025-12-31 (288d) | – |
| data-city.json | ok | 2026-09-01 (44d) | 2026-10-14T12:33 (2h) |
| data-mufon.json | ok | – | 2026-10-06T12:22 (8d) |
| data-tsa.json | ok | 2026-10-12 (3d) | 2026-10-13T20:11 (18h) |
| data-us_states.json | ok | – | – |

## Schedules (last 7 days)

| workflow | cron (UTC) | days run/due | ticks run | median delay | last scheduled run | flag |
|---|---|---|---|---|---|---|
| aviation-opensky | 25 * * * * | 8/8 | 18% | 0.3h | 2026-10-14T13:50 success | ok |
| aviation-tsa | 10 14 * * * | 6/6 | 100% | 5.7h | 2026-10-13T20:11 success | ok |
| catalog-health | 0 8 * * 1 | 1/1 | 100% | 9.7h | 2026-10-12T17:40 success | ok |
| cfpb-daily | 0 7 * * * | 6/6 | 100% | 7.3h | 2026-10-14T12:35 success | ok |
| city-daily | 0 7 * * * | 6/6 | 100% | 7.1h | 2026-10-14T12:30 success | ok |
| codeql | 0 4 * * 0 | 1/1 | 100% | 6.5h | 2026-10-11T10:28 success | ok |
| crypto-odds-daily | 15 14 * * * | 2/2 | 100% | 7.9h | 2026-10-13T20:15 success | ok |
| daily-audit | 17 9 * * * | 6/6 | 100% | 7.2h | 2026-10-13T16:56 success | ok |
| data-health | 0 15 * * * | 7/7 | 100% | 5.5h | 2026-10-13T20:34 success | ok |
| etf-flows-daily | 15 9 * * * | 6/6 | 100% | 7.3h | 2026-10-13T16:57 failure | FAILING |
| lthcs-backtest-daily | 30 23 * * * | 7/7 | 100% | 3.0h | 2026-10-14T02:00 success | ok |
| lthcs-backtest-monthly | 0 6 1 * * | 0/0 | – | – | 2026-10-01T11:52 success | ok |
| lthcs-beta-verdict-monthly | 0 8 1 * * | 0/0 | – | – | 2026-10-01T15:12 failure | FAILING |
| lthcs-crypto-daily | 47 11 * * * | 6/6 | 100% | 6.2h | 2026-10-13T18:31 success | ok |
| lthcs-daily | 0 23 * * * | 7/7 | 100% | 3.0h | 2026-10-14T01:27 success | ok |
| lthcs-news-hourly | 0 * * * * | 8/8 | 17% | 0.2h | 2026-10-14T11:06 success | ok |
| lthcs-quality-audit-monthly | 0 9 1 * * | 0/0 | – | – | 2026-10-01T15:53 failure | FAILING |
| lthcs-trends-daily | 13 4 * * * | 7/7 | 100% | 7.1h | 2026-10-14T09:28 success | ok |
| lthcs-trends-weekly | 47 2 * * 1 | 1/1 | 100% | 7.0h | 2026-10-12T09:46 success | ok |
| lthcs-tune-weights-monthly | 0 7 1 * * | 0/0 | – | – | 2026-10-01T14:10 success | ok |
| lthcs-validate-weekly | 0 5 * * 1 | 1/1 | 100% | 7.8h | 2026-10-12T12:46 failure | FAILING |
| money-flow-daily | 30 8 * * * | 6/6 | 100% | 7.3h | 2026-10-14T13:56 success | ok |
| pages | 0 * * * * | 8/8 | 17% | 0.2h | 2026-10-14T13:32 success | ok |
| real-estate-daily | 0 6 * * * | 6/6 | 100% | 7.1h | 2026-10-14T11:29 success | ok |
| secrets-check | 5 15 * * 1 | 1/1 | 100% | 7.4h | 2026-10-12T22:28 success | ok |
| security-audit | 0 9 * * 1 | 1/1 | 100% | 9.4h | 2026-10-12T18:24 success | ok |
| trufflehog-weekly | 0 3 * * 0 | 1/1 | 100% | 6.5h | 2026-10-11T09:32 success | ok |
| usaspending-daily | 30 7 * * * | 6/6 | 100% | 7.2h | 2026-10-14T12:57 success | ok |

## UX (live site)

| page | viewport | HTTP | weight / requests | JS exceptions | console errors | failed requests | overflow | tabs tappable |
|---|---|---|---|---|---|---|---|---|
| / | phone | 200 | 1.7 MB / 29 | 0 | 0 | 0 | no | 21/21 |
| / | desktop | 200 | 1.7 MB / 29 | 0 | 0 | 0 | no | 21/21 |
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
