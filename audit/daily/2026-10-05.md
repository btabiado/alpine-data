# Daily audit 2026-10-05 — AMBER

**AMBER** · 0 P0 · 6 P1 · 24 P2 · 30 new · 0 fixed (no previous report)

Generated 2026-10-05T18:29:15Z by [daily-audit](https://github.com/btabiado/alpine-data/actions/runs/37356031049). Machine-readable: `audit/daily/2026-10-05.json`.

## New since yesterday (30)
- **P1** [schedule] Scheduled workflow aviation-tsa: last scheduled run failed (2026-10-04) — cron 10 14 * * * ([run](https://github.com/btabiado/alpine-data/actions/runs/37223025786))
- **P1** [schedule] Scheduled workflow lthcs-beta-verdict-monthly: last scheduled run failed (2026-10-01) — cron 0 8 1 * * ([run](https://github.com/btabiado/alpine-data/actions/runs/36882479373))
- **P1** [schedule] Scheduled workflow lthcs-crypto-daily missed 1 day(s) in the last week — cron 0 12 * * *; no scheduled run on: 2026-10-04
- **P1** [schedule] Scheduled workflow lthcs-quality-audit-monthly: last scheduled run failed (2026-10-01) — cron 0 9 1 * * ([run](https://github.com/btabiado/alpine-data/actions/runs/36887779801))
- **P1** [workflow] Workflow data-health failed in the last 24h — step 'Build issue body' — Process completed with exit code 1. ([run](https://github.com/btabiado/alpine-data/actions/runs/37224950186))
- **P1** [workflow] Workflow lthcs-validate-weekly failed in the last 24h — step 'Fail workflow if validation reported issues' — Process completed with exit code 2. ([run](https://github.com/btabiado/alpine-data/actions/runs/37306933010))
- **P2** [api] Upstream APIs blocked (1): NUFORC
- **P2** [api] Upstream APIs degraded (1): DeFiLlama bridges
- **P2** [schedule] Crons that lose most of their ticks (3): aviation-opensky 19%, lthcs-news-hourly 20%, pages 18%
- **P2** [schedule] Crons starting >3h late (median delay) (19): aviation-tsa 5.0h, catalog-health 9.0h, cfpb-daily 6.5h, city-daily 6.3h, codeql 6.3h, data-health 3.6h, etf-flows-daily 6.7h, lthcs-backtest-monthly 5.9h, lthcs-beta-verdict-monthly 7.2h, lthcs-crypto-daily 12.9h, lthcs-quality-audit-monthly 6.9h, lthcs-trends-daily 6.3h, lthcs-trends-weekly 7.1h, lthcs-tune-weights-monthly 7.2h, lthcs-validate-weekly 7.1h, money-flow-daily 6.5h, real-estate-daily 6.2h, trufflehog-weekly 6.2h, usaspending-daily 6.5h

## Ongoing (0)
- none

## Fixed since yesterday (0)
- none

## Snapshot

- **Feeds** (data_health, committed): 2 disclosed, 31 ok, 14 skipped, 2 suppressed
  - muted: data-mufon.json (muted until 2027-01-04: NUFORC blocks automated access (Cloudflare ch…); data-tsa.json (muted until 2026-10-18: tsa.gov (Akamai) 403s datacenter IPs incl. Gi…)
- **Workflows** (last 24h, main): 21 ran, 2 ended failed (data-health, lthcs-validate-weekly)
- **API status** (2026-10-05T13:33:24+00:00): 2 auth_required, 1 blocked, 1 degraded, 51 up
  - changed vs. git:755919ab9c (2026-10-04T12:11:11+00:00): Binance.US klines: None → up; CoinDesk CADLI: blocked → None; CoinGecko market_chart: None → up; Coinbase candles: None → up; CryptoCompare CCCAGG: auth_required → None; CryptoCompare data-api: auth_required → None; EPA AirNow: auth_required → up; FRED: auth_required → up; FRED ENPLANE (air travel): auth_required → up; GitHub REST: None → up; Google News RSS: None → up; Kraken OHLC: None → up

**Spot checks** (live site vs. primary sources)

| check | status | detail |
|---|---|---|
| live_site_build_age | ok | built 2026-10-05T18:15:52Z (11m ago) |
| btc_price_vs_coingecko | ok | site 85444.79 vs 85500.0 (0.065%) |
| eth_price_vs_coingecko | ok | site 2707.41 vs 2707.78 (0.014%) |
| fear_greed_vs_alternative_me | ok | 2026-10-05: site 70 vs 70 |
| btc_tip_height_vs_mempool_space | ok | site 970059 vs 970060 (lag 1 blocks) |
| treasury_10y_vs_treasury_gov | ok | 2026-10-01: site 5.24 vs 5.24 |

**Live payload stamps**: 11 of 15 stamped within 48h, the rest:

| payload | status | data as_of | generated |
|---|---|---|---|
| data-aviation.json | ok | 2025-12-31 (279d) | – |
| data-city.json | ok | 2026-09-01 (35d) | 2026-10-05T15:37 (3h) |
| data-tsa.json | ok | – | 2026-06-18T17:22 (109d) |
| data-us_states.json | ok | – | – |

## Schedules (last 7 days)

| workflow | cron (UTC) | days run/due | ticks run | median delay | last scheduled run | flag |
|---|---|---|---|---|---|---|
| aviation-opensky | 25 * * * * | 8/8 | 19% | 0.4h | 2026-10-05T16:10 success | ok |
| aviation-tsa | 10 14 * * * | 6/6 | 100% | 5.0h | 2026-10-04T18:05 failure | FAILING |
| catalog-health | 0 8 * * 1 | 1/1 | 100% | 9.0h | 2026-10-05T17:01 success | ok |
| cfpb-daily | 0 7 * * * | 7/7 | 100% | 6.5h | 2026-10-05T15:28 success | ok |
| city-daily | 0 7 * * * | 7/7 | 100% | 6.3h | 2026-10-05T15:20 success | ok |
| codeql | 0 4 * * 0 | 1/1 | 100% | 6.3h | 2026-10-04T10:19 success | ok |
| daily-audit | 17 9 * * * | 0/0 | – | – | never | ok |
| data-health | 0 15 * * * | 1/1 | 100% | 3.6h | 2026-10-04T18:34 failure | FAILING |
| etf-flows-daily | 15 9 * * * | 6/6 | 100% | 6.7h | 2026-10-04T14:47 success | ok |
| lthcs-backtest-daily | 30 23 * * * | 7/7 | 100% | 2.8h | 2026-10-05T02:10 success | ok |
| lthcs-backtest-monthly | 0 6 1 * * | 1/1 | 100% | 5.9h | 2026-10-01T11:52 success | ok |
| lthcs-beta-verdict-monthly | 0 8 1 * * | 1/1 | 100% | 7.2h | 2026-10-01T15:12 failure | FAILING |
| lthcs-crypto-daily | 0 12 * * * | 5/6 | 83% | 12.9h | 2026-10-04T00:17 success | MISSED 1d |
| lthcs-daily | 0 23 * * * | 7/7 | 100% | 2.7h | 2026-10-05T01:22 success | ok |
| lthcs-news-hourly | 0 * * * * | 8/8 | 20% | 0.1h | 2026-10-05T17:48 success | ok |
| lthcs-quality-audit-monthly | 0 9 1 * * | 1/1 | 100% | 6.9h | 2026-10-01T15:53 failure | FAILING |
| lthcs-trends-daily | 0 4 * * * | 7/7 | 100% | 6.3h | 2026-10-05T11:05 success | ok |
| lthcs-trends-weekly | 0 4 * * 1 | 1/1 | 100% | 7.1h | 2026-10-05T11:03 success | ok |
| lthcs-tune-weights-monthly | 0 7 1 * * | 1/1 | 100% | 7.2h | 2026-10-01T14:10 success | ok |
| lthcs-validate-weekly | 0 5 * * 1 | 1/1 | 100% | 7.1h | 2026-10-05T12:03 failure | FAILING |
| money-flow-daily | 30 8 * * * | 6/6 | 100% | 6.5h | 2026-10-05T17:17 success | ok |
| pages | 0 * * * * | 8/8 | 18% | 0.2h | 2026-10-05T18:14 success | ok |
| real-estate-daily | 0 6 * * * | 7/7 | 100% | 6.2h | 2026-10-05T14:04 success | ok |
| secrets-check | 5 15 * * 1 | 0/0 | – | – | 2026-09-28T21:11 success | ok |
| security-audit | 0 9 * * 1 | 0/0 | – | – | 2026-10-05T17:37 success | ok |
| trufflehog-weekly | 0 3 * * 0 | 1/1 | 100% | 6.2h | 2026-10-04T09:10 success | ok |
| usaspending-daily | 30 7 * * * | 7/7 | 100% | 6.5h | 2026-10-05T16:15 success | ok |

## UX (live site)

| page | viewport | HTTP | weight / requests | JS exceptions | console errors | failed requests | overflow | tabs tappable |
|---|---|---|---|---|---|---|---|---|
| / | phone | 200 | 1.9 MB / 30 | 0 | 0 | 0 | no | 21/21 |
| / | desktop | 200 | 1.9 MB / 30 | 0 | 0 | 0 | no | 21/21 |
| /v2/ | phone | 200 | 1.8 MB / 29 | 0 | 0 | 0 | no | 15/15 |
| /v2/ | desktop | 200 | 1.8 MB / 29 | 0 | 0 | 0 | no | 15/15 |
| /summit/ | phone | 200 | 0.4 MB / 1 | 0 | 0 | 0 | no | – |
| /summit/ | desktop | 200 | 0.4 MB / 1 | 0 | 0 | 0 | no | – |
| /health/ | phone | 200 | 0.0 MB / 4 | 0 | 0 | 0 | no | – |
| /health/ | desktop | 200 | 0.0 MB / 4 | 0 | 0 | 0 | no | – |
| /real-estate/ | phone | 200 | 1.2 MB / 6 | 0 | 0 | 0 | no | – |
| /real-estate/ | desktop | 200 | 1.2 MB / 24 | 0 | 0 | 0 | no | – |
| /lthcs/ | phone | 200 | 0.3 MB / 36 | 0 | 0 | 0 | no | – |
| /lthcs/ | desktop | 200 | 0.3 MB / 36 | 0 | 0 | 0 | no | – |

UX audit took 52s.
