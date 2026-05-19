# LTHCS Alt-Data Sources Research — Tier 6 Environmental Blockers

**Date.** 2026-05-19
**Author.** Swarm agent X (research-only; no code touched)
**Scope.** Evaluate legitimate alternatives for the three Tier 6 environmental blockers currently marked "permanent / no fix possible." For each blocker, decide whether the cost/effort tradeoff is worth revisiting.

**Blockers in scope:**
- **#30** Reddit OAuth blocked (Responsible Builder Policy)
- **#31** Alpha Vantage NEWS_SENTIMENT rate limit (5-7 calls/day in practice on free tier)
- **#32** pytrends rate limit (already softened by `7a469df` + `ae2c1c2` adaptive backoff)

**LTHCS scale assumed throughout:** 167 equities + 10 cryptos, daily refresh, single-user hobby/research deployment, no commercial-API license needed.

**Pricing convention.** Where I couldn't verify a number with high confidence I marked `[needs lookup]` rather than guess. All numbers here should be confirmed against vendor pricing pages before any actual spend commitment.

---

## #30 — Reddit OAuth Blocked

### Current state
- **Code path:** `fetch_market.py` has Reddit functions wired and ready; secrets (`REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`) will never be set.
- **Status quo:** RSS-only fallback ships in production. Subscriber counts, comment sentiment, and r/wallstreetbets-style buzz signals never reach the Adoption or Thesis pillars.
- **Cost of staying:** The Adoption pillar already inverted away from raw social mentions (see `adoption-pillar-inversion-2026-05-19.md`), so Reddit's marginal contribution is *already low*. Thesis loses a corroborating retail-sentiment cross-check on the highest-buzz names (TSLA, NVDA, GME-class movers, BTC/ETH).
- **Memory note.** Bryan attempted re-registration 2026-05-15 and was rejected. Per `reddit_oauth_blocked.md` — do not propose retries.

### Alternatives

| Name | Cost/mo at LTHCS scale | ToS compliance | Effort | Quality vs. status quo |
|---|---|---|---|---|
| **Pushshift (`api.pushshift.io`)** | Free | **Partially closed since 2023** — public API was shut down post-Reddit-API-pricing-fight (Apr 2023); restricted access for moderators only at present. Rolling back online is intermittent. | M (if it comes back) | **Same** or worse — old archive only, not live |
| **PRAW with app-only / "script" credentials** | Free | **Doesn't work** — script and read-only PRAW flows still require an app registered under the same Responsible Builder Policy. Same gate Bryan hit. | — | — |
| **Apify / Bright Data Reddit scrapers** | Apify Reddit Scraper ~$30-50/mo at 167-symbol weekly refresh `[needs lookup]`; Bright Data residential proxy + scraper ~$100+/mo | **Borderline / hostile to Reddit ToS** — Reddit explicitly bans scraping in current ToS. Litigation precedent (hiQ vs. LinkedIn) suggests public data scraping is legally defensible but Reddit specifically has been aggressive. | M | Comparable to OAuth API, but legal/ToS risk is unacceptable for a hobby project |
| **RedditAPI third-party aggregators (e.g., social-searcher.com, RedditDataAPI, etc.)** | Most are scrapers under a wrapper; same ToS risk + uptime depends on the aggregator dodging Reddit's blocks. ~$20-40/mo typical `[needs lookup]` | Inherits Reddit's anti-scraping stance | S | Worse than direct API (extra latency + missing fields) |
| **StockTwits public stream** | Free (public read-only) | Compliant (StockTwits has explicit "free tier" intent for read access) | S | **Different signal class** — StockTwits is more retail-trader-focused than Reddit but ~70% topic overlap on equities. Weak on crypto. Already noted as "Maybe — social-only, noisy" in `news-feeds-general-apis.md` §2. |
| **Skip Reddit entirely; lean harder on existing sentiment sources** | $0 | Trivially compliant | XS (just don't add it) | Marginal loss — Reddit was already a corroborating signal, not load-bearing |

### Top pick: **Skip Reddit entirely; optionally layer in StockTwits public stream if/when retail sentiment becomes a missing primary signal**

Reddit's ToS makes every scraping path either unreliable, expensive, or legally risky. Pushshift is effectively dead for live data. The only ToS-compliant Reddit path is OAuth, which is gated. Since Adoption pillar already inverted away from raw social mentions and Finnhub will give better-quality news sentiment than Reddit ever did, **the cost of staying with the status quo is < $0 — there is no meaningful signal loss worth chasing.** If retail sentiment ever becomes critical (e.g., for a "meme stock detector" feature), StockTwits public stream is the only ToS-clean option, but that's a V3+ concern.

---

## #31 — Alpha Vantage NEWS_SENTIMENT Rate Limit

### Current state
- **Theoretical free quota:** 25 calls/day per AV docs.
- **Practical free quota:** 5-7 calls/day (AV throttles aggressively under the rotation logic; spec'd 25 not realized in practice).
- **AND-not-OR quirk** (`alpha_vantage_news_sentiment_quirk.md`): multi-ticker batching defeated — `tickers=A,B,C` filters for articles mentioning *all* of A, B, C, returning ~0 hits at scale.
- **Daily pipeline impact:** Thesis pillar sub_score sits at neutral 50.0 for ~120 of 167 tickers on any given day. Rotation covers ~47/167 daily.
- **Cost of staying:** Thesis is the 20%-weight pillar nominally — under-realized today.

### Alternatives

| Name | Cost/mo at LTHCS scale | ToS compliance | Effort | Quality vs. status quo |
|---|---|---|---|---|
| **Finnhub `/news-sentiment`** | Free tier: 60 req/min, no documented daily cap → 167 calls/day = trivial (~3 min wall clock) | Compliant — explicit free tier for individual devs | S | **Strict upgrade** — buzz score, bullish %, bearish %, native sector-average baseline. Returns LTHCS's exact pillar shape with one helper. See `news-feeds-general-apis.md` §3.1. |
| **Polygon.io `/v2/reference/news`** | Free tier: 5 req/min (60 req/min Starter at $29/mo). At 167 tickers, free tier would take ~35 min/day; Starter trivial. | Compliant | M | Insight tags (sentiment+reasoning) on a subset of tickers only; weaker than Finnhub for breadth. Better for *event* attribution than aggregate sentiment. |
| **NewsAPI.org (newsapi.org)** | Free: 100 req/day, **24h delayed on free tier**; Business: $449/mo `[verify]` — non-starter for hobby project | Compliant | S | Keyword-only search (`q=AAPL`), no per-ticker entity tagging, no native sentiment. Worse signal-per-call than Finnhub. |
| **MarketAux** | Free: 100 req/day; $19/mo for 10k req/day | Compliant | S | Native sentiment + entity tagging; 100/day caps you at ~60% coverage even with rotation. Good as *supplemental* signal, not primary. |
| **SEC EDGAR 8-K (already in tree)** | Free | Compliant | M (Thesis re-architecture) | **Structurally different** — covers material *events* (filings, M&A, exec changes), not market-news sentiment. Could carry partial Thesis weight by mapping "filing density" → buzz proxy, but would need a new Thesis sub-formula. High effort, narrow signal class. |
| **LLM-as-news-source via Anthropic web search (shadow already wired)** | Anthropic API: ~$3-15 per 1M tokens depending on model. Per ticker per day @ ~5k tokens for a sentiment summary = ~$0.075/ticker/day = ~$0.075 × 167 × 30 ≈ **$375-400/mo** `[verify against current pricing]` for full daily coverage. | Compliant (Bryan's own API account) | M | **High quality but expensive** — LLM can give nuanced reasoning ("buzz is up because of earnings beat, sentiment net-bullish, drivers: A, B, C"). Variance/non-determinism in scores is a concern. Shadow path exists per `lthcs-llm-sentiment-shadow-spec.md`; full rollout costs ~$400/mo. |
| **Stay on AV free + rotation** | $0 | Compliant | XS | Covers 47/167 tickers daily; the 120/167 gap is the active cost |

### Top pick: **Finnhub `/news-sentiment` as drop-in replacement; deprecate AV NEWS_SENTIMENT path entirely**

Finnhub's free tier covers all 167 tickers daily with no daily cap, native sentiment output that maps to LTHCS's exact pillar shape, and a `companyNewsScore` field that already does the aggregation work AV makes us do per-article. Effort is small (S — one new fetcher + helper). This single change moves the Thesis pillar from "neutral 50.0 for 72% of tickers" to **full daily coverage** at $0 incremental cost. There is no reason to keep AV NEWS_SENTIMENT in the daily pipeline once Finnhub is wired. (AV remains useful for other endpoints — earnings calendar, fundamentals — that aren't gated by NEWS_SENTIMENT's quirks.)

---

## #32 — pytrends Rate Limit

### Current state
- **Already mitigated** by `7a469df` (resumable pytrends with retry/backoff) and `ae2c1c2` (lthcs-trends-daily 04:00 UTC cron).
- **Status quo:** adaptive backoff handles 429s gracefully; daily pipeline completes with full 167-ticker trends coverage across multiple runs (resumable).
- **Cost of staying:** Real — pytrends is fundamentally an *unofficial* Google Trends scraper; Google can break or block it at any time (has happened in the past, ~2022 outage lasted weeks). The adaptive backoff is a *workaround*, not a fix.
- **This blocker is the lowest-priority of the three** — current pipeline works; upgrade is insurance, not a functional fix.

### Alternatives

| Name | Cost/mo at LTHCS scale | ToS compliance | Effort | Quality vs. status quo |
|---|---|---|---|---|
| **SerpAPI Google Trends endpoint** | $50/mo Developer (5k searches/mo) — at 167 tickers daily = 167×30 = 5,010 calls/mo, **just barely fits** Developer tier; one batch retry/month busts it. $130/mo Production (15k searches) gives headroom. `[verify against current SerpAPI pricing page]` | **Compliant** — SerpAPI handles Google's anti-bot layer legitimately as a search-API reseller | S | **Strict upgrade** — official-grade reliability, no random outages, same data shape as pytrends. |
| **SearchAPI.io** | $40/mo for 10k searches `[verify]`; cheaper than SerpAPI at similar quality | Compliant (same model as SerpAPI) | S | Equivalent to SerpAPI; slightly less mature docs but cheaper at LTHCS volume |
| **DataForSEO Google Trends** | $0.50 per 100 keywords on pay-as-you-go; 167×30 = 5,010 lookups/mo = ~$25/mo `[verify]` | Compliant | S | Equivalent quality; PAYG pricing is friendlier than monthly subscription for variable volume |
| **Google Trends RSS feeds** | Free | Compliant | M | **Very limited** — RSS only exposes the *Trending Searches* daily list (top 20 nationwide), not per-keyword historical interest. Not a substitute for what pytrends provides. |
| **Glimpse / Exploding Topics paid APIs** | $200+/mo enterprise pricing `[needs lookup]` | Compliant | M | Different signal class — emerging-topic detection, not per-ticker interest over time. Wrong tool for LTHCS. |
| **Stay on pytrends + adaptive backoff** | $0 | Borderline (unofficial scraper of Google's frontend; Google's ToS technically prohibits but enforcement is sporadic; pytrends is widely used in research and Google has not pursued it) | XS (already shipped) | Status quo — works today, fragile to upstream changes |

### Top pick: **Stay on pytrends + adaptive backoff for now; pre-prototype the DataForSEO swap as the insurance plan**

The status quo works after the 7a469df fix and the cost of upgrading ($25-50/mo) is real but the *signal-quality* gain is zero — both paths deliver the same Google Trends data. Upgrading buys *reliability* against future Google blocks, not better data. DataForSEO PAYG at ~$25/mo is the cheapest legitimate path; SerpAPI at $50/mo is the most battle-tested. **Don't migrate today**, but keep the DataForSEO adapter sketched (~half-day's work) so the switch is a one-day fire-drill if pytrends breaks again. This is the only blocker of the three where "do nothing" is the right answer right now.

---

## Triple-Blocker Order of Operations

If future-Bryan wants to fix all three, the order that **minimizes total cost while maximizing signal-quality gain per dollar** is:

### **Phase A — Free, high-impact (do this first)**
1. **Wire Finnhub `/news-sentiment` and deprecate AV NEWS_SENTIMENT from daily pipeline.** Cost: $0. Effort: ~1 day. Signal gain: Thesis pillar goes from 28% real coverage (47/167) to ~100% (167/167) with native sentiment scoring. **This is the single highest-ROI move on the board** — it's strictly dominant and free.

### **Phase B — Acceptance, not action (do this second)**
2. **Formally close Reddit (#30) as "deferred indefinitely, no future revisit planned."** Cost: $0. Effort: 0 (just update the audit doc). Signal gain: 0 — but it removes mental overhead. Reddit's ToS makes every alternative either non-compliant or expensive, and post-Finnhub the Thesis pillar no longer needs Reddit as a corroborating signal. Stop treating Reddit as a "TODO" and treat it as "WONTFIX — environmental."

### **Phase C — Insurance, not feature work (do this last, opportunistically)**
3. **Pre-build the DataForSEO Trends adapter as a dormant fallback.** Cost: $0 today, $25-30/mo if/when activated. Effort: ~half a day to write the adapter, register the account, leave it disabled. Signal gain: 0 today, ~100% Adoption-pillar continuity if Google breaks pytrends. This is a "smoke alarm" investment — you don't want to be hunting for an alternative on the day pytrends 500s on you.

### What NOT to do
- **Don't pay $400/mo for the LLM-as-news shadow rollout.** Finnhub gives 90% of the signal at 0% of the cost. LLM-as-news is only worth it if/when Thesis becomes the limiting pillar *after* Finnhub is in place.
- **Don't pay for SerpAPI today.** It's strictly more expensive than DataForSEO at LTHCS volume and the data is identical.
- **Don't go near Reddit scraping services.** Reddit's anti-scraping posture makes the legal/uptime risk indefensible for a hobby project.

### Net cost of "fix all three"
- **Active monthly cost:** $0 (Phase A and B are free, Phase C is dormant by default)
- **Standby contingency cost:** $25-30/mo if pytrends breaks and Phase C activates
- **One-time effort:** ~1.5-2 engineering days total

The Tier 6 "environmental blocker" label is most accurate for #30 (Reddit), partially accurate for #32 (pytrends — softened but not eliminated), and **flat-out wrong for #31 (AV NEWS_SENTIMENT)** — that one has a free, strict-upgrade replacement (Finnhub) sitting in the existing `news-feeds-general-apis.md` recommendations. The fact that #31 has been treated as "no fix possible" suggests the Tier 6 list should be re-audited; "environmental" should mean *no legitimate fix exists*, not *we haven't reached for the obvious one yet*.

---

## Appendix — Sources & Cross-References

- `docs/news-feeds-general-apis.md` §2-§3 — Finnhub, Polygon, MarketAux, NewsAPI specs (Bryan's existing research; this doc cites but doesn't duplicate)
- `docs/news-feeds-sector-specific.md` — sector-specific feeds (out of scope here)
- `docs/news-feeds-earnings-events.md` — EDGAR / earnings APIs (touched on for #31 SEC alternative)
- `docs/adoption-pillar-inversion-2026-05-19.md` — context for why Adoption no longer leans on raw social mentions
- `docs/lthcs-llm-sentiment-shadow-spec.md` — context for LLM-as-news cost calculations under #31
- Memory: `reddit_oauth_blocked.md`, `alpha_vantage_news_sentiment_quirk.md`
- Code: `lthcs/sources/` (where future fetchers would land), `lthcs/score.py` (consumer)

**All pricing should be re-verified against vendor pricing pages before any spend commitment.** Numbers flagged `[needs lookup]` or `[verify]` are estimates from memory or context, not freshly confirmed.
