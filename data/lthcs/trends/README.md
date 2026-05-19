# LTHCS Google Trends snapshots

This directory holds the aggregated weekly Google Trends snapshot the
LTHCS Adoption pillar reads each daily build. The shape on disk is::

    data/lthcs/trends/<YYYY-Www>.json

with the schema documented in `lthcs/sources/google_trends.py`.

## Two writers, one reader

| Writer | When | Behaviour |
|---|---|---|
| `scripts/lthcs_trends_weekly.py` | Mon 04:00 UTC (`.github/workflows/lthcs-trends-weekly.yml`) | Try to refresh the full universe in one ~11-minute pass. Polite cadence ~4 s/ticker. Empirically rate-limited — only ~11/167 tickers survive the run. |
| `scripts/lthcs_trends_daily.py` | Daily 04:00 UTC (`.github/workflows/lthcs-trends-daily.yml`, **staged but not yet committed** as of 2026-05-19) | Process ~30 tickers/day with adaptive backoff + resumable progress. Spreads work over ~5.5 days to refresh the universe without hitting a sustained 429 wall. |

Both writers merge **additively** into the same per-week snapshot
file. Neither writer deletes existing tickers — the only way a ticker
falls out is the natural week rollover when a new `<YYYY-Www>.json`
file starts empty.

The reader, `lthcs/sources/google_trends.py`, picks the most-recent
snapshot and serves cached numbers to the live daily pipeline. It
performs no network I/O.

## Rate-limit reality (Phase 1 audit)

Pre-Phase-2 (weekly batch only): **~11/167 active tickers** carry
Google Trends data. pytrends's keyless endpoint tolerates roughly
5-10 requests per minute before issuing 429s whose cooldown can
stretch for hours.

Phase 2 (daily nudge) hypothesis: at ~30 tickers/day with adaptive
backoff, the universe should fully refresh in **5-6 days** without
ever tripping the long cooldown. The first 1-2 weeks of operation
will tell us whether that holds in production.

### Retry / backoff parameters (Phase 2)

| Parameter | Default | Why |
|---|---|---|
| `--batch-size` | 30 | 167 / 30 ~= 5.5 days to refresh the universe — one weekly cycle with slack. |
| `--sleep-base` | 12 s | ~5 req/min. About half of pytrends' historical 429 ceiling — leaves headroom for the wrapped retry loop. |
| `--max-backoff` | 300 s (5 min) | Caps how long a single 429 stalls the run. Beyond this we'd rather bail and try again tomorrow. |
| `--stale-after-days` | 5 | Rolling refresh window matches the daily cadence: Mon fetch stays fresh through Fri, becomes re-eligible Sat. |
| `--max-retries` | 2 | Inner retry count inside `fetch_one_live`. The adaptive sleeper layered on top is the primary throttling mechanism. |
| consecutive-429 bail | 5 | If 5 candidates in a row 429, we're in pytrends' penalty box — abort the run, ship what we have, try again tomorrow. |

### Progress / resume contract

`scripts/lthcs_trends_daily.py` writes `.cache/lthcs/trends_progress.json`
(gitignored — local-only) after every successful fetch::

    {
      "date": "YYYY-MM-DD",
      "completed": ["AAPL", "MSFT", ...],
      "failures": ["TSLA", ...]
    }

The `date` key is the safety latch: if the file's date doesn't match
today, it's silently reset. So a re-run mid-day picks up where the
last run died, but tomorrow's run starts fresh.

In CI the resume file is ephemeral (fresh checkout each run), but
the `fetched_at` timestamp stamped into the **committed** snapshot
(`data/lthcs/trends/<YYYY-Www>.json`) drives the stale-TTL logic
that picks tomorrow's slice. That's the durable signal.

## Fallback paths (if pytrends collapses entirely)

If Phase 2's adaptive backoff still doesn't budge `has_trends`
materially after 2-3 weeks of daily runs, the audit's two
alternatives are on the table:

1. **SerpAPI** — paid (`$50-75/mo` for the volume we'd need), ToS-safe,
   keyed. Drop-in replacement: only `fetch_one_live` would need to
   change. Snapshot schema, weekly/daily writers, and reader stay
   identical. Best option if rate-limit reality keeps biting.
2. **Headless scraping** — medium ToS risk, free. Last resort; not
   recommended unless legal cover is acquired first.

Both alternatives plug into the same architecture by swapping a
single fetch function. The investment in Phase 2's progress /
backoff / staleness machinery carries over.
