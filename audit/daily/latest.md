# Daily audit 2026-10-16 — RED

**RED** · 4 P0 · 4 P1 · 25 P2 · 1 new · 2 fixed (vs. 2026-10-15)

Generated 2026-10-16T13:59:16Z by [daily-audit](https://github.com/btabiado/alpine-data/actions/runs/38911833799). Machine-readable: `audit/daily/2026-10-16.json`.

## New since yesterday (1)
- **P1** [api] Upstream API USGS ScienceBase is DOWN

## Ongoing (32)
- **P0** [feed] Feed R2 archive [history: daily payload copies] is GAP (? old, limit ?) — missing 90: v2/data-cpi.json: 2026-10-07, v2/data-cpi.json: 2026-10-08, v2/data-cpi.json: 2026-10-09, v2/data-cpi.json: 2026-10-10, v2/data-cpi.json: 2026-10-11, v2/data-cpi.json: 2026-10-12, v2/data… _(since 2026-10-07)_
- **P0** [feed] Feed data/btc_flows.csv is STALE (9d old, limit 2d) _(since 2026-10-08)_
- **P0** [feed] Feed data/eth_flows.csv is STALE (9d old, limit 2d) _(since 2026-10-08)_
- **P0** [workflow] Workflow etf-flows-daily failed two days running — step 'Fetch BTC + ETH spot-ETF flows' — Process completed with exit code 1. ([run](https://github.com/btabiado/alpine-data/actions/runs/38749113718)) _(since 2026-10-14)_
- **P1** [schedule] Scheduled workflow lthcs-beta-verdict-monthly: last scheduled run failed (2026-10-01) — cron 0 8 1 * * ([run](https://github.com/btabiado/alpine-data/actions/runs/36882479373)) _(since 2026-10-05)_
- **P1** [schedule] Scheduled workflow lthcs-quality-audit-monthly: last scheduled run failed (2026-10-01) — cron 0 9 1 * * ([run](https://github.com/btabiado/alpine-data/actions/runs/36887779801)) _(since 2026-10-05)_
- **P1** [schedule] Scheduled workflow lthcs-validate-weekly: last scheduled run failed (2026-10-12) — cron 0 5 * * 1 ([run](https://github.com/btabiado/alpine-data/actions/runs/38251149250)) _(since 2026-10-13)_
- **P2** [api] Upstream APIs blocked (2): Farside (ETF flows), NUFORC _(since 2026-10-06)_
- **P2** [api] Upstream APIs degraded (1): DeFiLlama bridges _(since 2026-10-05)_
- **P2** [schedule] Crons that lose most of their ticks (3): aviation-opensky 20%, lthcs-news-hourly 19%, pages 18% _(since 2026-10-05)_
- **P2** [schedule] Crons starting >3h late (median delay) (19): aviation-tsa 4.6h, catalog-health 9.7h, cfpb-daily 6.7h, city-daily 6.6h, codeql 6.5h, crypto-odds-daily 6.0h, daily-audit 6.4h, data-health 4.4h, etf-flows-daily 6.5h, lthcs-crypto-daily 5.0h, lthcs-trends-daily 6.5h, lthcs-trends-weekly 7.0h, lthcs-validate-weekly 7.8h, money-flow-daily 6.5h, real-estate-daily 6.4h, secrets-check 7.4h, security-audit 9.4h, trufflehog-weekly 6.5h, usaspending-daily 6.8h _(since 2026-10-05)_

## Fixed since yesterday (2)
- **P0** [feed] Feed data/real_estate.json is STALE (9d old, limit 2d)
- **P1** [workflow] Workflow crypto-odds-daily failed in the last 24h ([run](https://github.com/btabiado/alpine-data/actions/runs/38662091770))

## Snapshot

- **Feeds** (data_health, committed): 2 disclosed, 1 gap, 28 ok, 14 skipped, 2 stale, 1 suppressed
  - muted: data-mufon.json (muted until 2027-01-04: NUFORC blocks automated access (Cloudflare ch…)
- **Workflows** (last 24h, main): 17 ran, 1 ended failed (etf-flows-daily)
- **API status** (2026-10-16T12:19:52+00:00): 2 auth_required, 2 blocked, 1 degraded, 1 down, 49 up
  - changed vs. git:4a6e8c9e13 (2026-10-15T13:19:00+00:00): USGS ScienceBase: up → down

**Spot checks** (live site vs. primary sources)

| check | status | detail |
|---|---|---|
| live_site_build_age | ok | built 2026-10-16T12:17:49Z (2h ago) |
| btc_price_vs_coingecko | ok | site 82129.53 vs 81981.0 (0.181%) |
| eth_price_vs_coingecko | ok | site 2462.62 vs 2456.93 (0.232%) |
| fear_greed_vs_alternative_me | ok | 2026-10-16: site 63 vs 63 |
| btc_tip_height_vs_mempool_space | ok | site 971709 vs 971715 (lag 6 blocks) |
| treasury_10y_vs_treasury_gov | ok | 2026-10-14: site 5.19 vs 5.19 |

**Live payload stamps**: 10 of 15 stamped within 48h, the rest:

| payload | status | data as_of | generated |
|---|---|---|---|
| data-aviation.json | ok | 2025-12-31 (290d) | – |
| data-city.json | ok | 2026-09-01 (46d) | 2026-10-16T12:03 (2h) |
| data-mufon.json | ok | – | 2026-10-06T12:22 (10d) |
| data-tsa.json | ok | 2026-10-14 (3d) | 2026-10-15T18:12 (20h) |
| data-us_states.json | ok | – | – |

## Schedules (last 7 days)

| workflow | cron (UTC) | days run/due | ticks run | median delay | last scheduled run | flag |
|---|---|---|---|---|---|---|
| aviation-opensky | 25 * * * * | 8/8 | 20% | 0.3h | 2026-10-16T12:32 success | ok |
| aviation-tsa | 10 14 * * * | 7/7 | 100% | 4.6h | 2026-10-15T18:11 success | ok |
| catalog-health | 0 8 * * 1 | 1/1 | 100% | 9.7h | 2026-10-12T17:40 success | ok |
| cfpb-daily | 0 7 * * * | 6/6 | 100% | 6.7h | 2026-10-16T12:02 success | ok |
| city-daily | 0 7 * * * | 6/6 | 100% | 6.6h | 2026-10-16T11:56 success | ok |
| codeql | 0 4 * * 0 | 1/1 | 100% | 6.5h | 2026-10-11T10:28 success | ok |
| crypto-odds-daily | 15 14 * * * | 4/4 | 100% | 6.0h | 2026-10-15T18:14 success | ok |
| daily-audit | 17 9 * * * | 6/6 | 100% | 6.4h | 2026-10-15T14:09 success | ok |
| data-health | 0 15 * * * | 7/7 | 100% | 4.4h | 2026-10-15T18:58 success | ok |
| etf-flows-daily | 15 9 * * * | 6/6 | 100% | 6.5h | 2026-10-15T14:11 failure | FAILING |
| lthcs-backtest-daily | 30 23 * * * | 7/7 | 100% | 2.5h | 2026-10-16T01:31 success | ok |
| lthcs-backtest-monthly | 0 6 1 * * | 0/0 | – | – | 2026-10-01T11:52 success | ok |
| lthcs-beta-verdict-monthly | 0 8 1 * * | 0/0 | – | – | 2026-10-01T15:12 failure | FAILING |
| lthcs-crypto-daily | 47 11 * * * | 6/6 | 100% | 5.0h | 2026-10-15T15:45 success | ok |
| lthcs-daily | 0 23 * * * | 7/7 | 100% | 2.5h | 2026-10-16T00:52 success | ok |
| lthcs-news-hourly | 0 * * * * | 8/8 | 19% | 0.2h | 2026-10-16T10:37 success | ok |
| lthcs-quality-audit-monthly | 0 9 1 * * | 0/0 | – | – | 2026-10-01T15:53 failure | FAILING |
| lthcs-trends-daily | 13 4 * * * | 6/6 | 100% | 6.5h | 2026-10-16T08:58 success | ok |
| lthcs-trends-weekly | 47 2 * * 1 | 1/1 | 100% | 7.0h | 2026-10-12T09:46 success | ok |
| lthcs-tune-weights-monthly | 0 7 1 * * | 0/0 | – | – | 2026-10-01T14:10 success | ok |
| lthcs-validate-weekly | 0 5 * * 1 | 1/1 | 100% | 7.8h | 2026-10-12T12:46 failure | FAILING |
| money-flow-daily | 30 8 * * * | 6/6 | 100% | 6.5h | 2026-10-16T13:26 success | ok |
| pages | 0 * * * * | 8/8 | 18% | 0.2h | 2026-10-16T12:16 success | ok |
| real-estate-daily | 0 6 * * * | 6/6 | 100% | 6.4h | 2026-10-16T11:01 success | ok |
| secrets-check | 5 15 * * 1 | 1/1 | 100% | 7.4h | 2026-10-12T22:28 success | ok |
| security-audit | 0 9 * * 1 | 1/1 | 100% | 9.4h | 2026-10-12T18:24 success | ok |
| trufflehog-weekly | 0 3 * * 0 | 1/1 | 100% | 6.5h | 2026-10-11T09:32 success | ok |
| usaspending-daily | 30 7 * * * | 6/6 | 100% | 6.8h | 2026-10-16T12:35 success | ok |

## UX (live site)

| page | viewport | HTTP | weight / requests | JS exceptions | console errors | failed requests | overflow | tabs tappable |
|---|---|---|---|---|---|---|---|---|
| / | phone | 200 | 1.9 MB / 29 | 0 | 0 | 0 | no | 21/21 |
| / | desktop | 200 | 1.9 MB / 29 | 0 | 0 | 0 | no | 21/21 |
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

UX audit took 46s.
