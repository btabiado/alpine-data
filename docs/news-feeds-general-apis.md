# General News & Sentiment APIs — Free/Freemium Survey

**Scope.** This doc evaluates broadly-applicable per-ticker news + sentiment sources for the LTHCS Thesis Integrity pillar. Sibling docs `news-feeds-sector-specific.md` and `news-feeds-earnings-events.md` cover their respective verticals — do not cross-reference content between them.

**Context.** Bryan's current stack:
- **Alpha Vantage NEWS_SENTIMENT** — 25 req/day free, AND-not-OR multi-ticker quirk forces single-ticker calls, rotation logic already shipped, today covers ~47/167 tickers per daily run.
- **HN Algolia + TechCrunch RSS + VentureBeat RSS** — already wired as the "AI news supplement"; no API key, mention counting only (no sentiment direction in V1).
- **Reddit OAuth** — blocked indefinitely per Bryan's memory; ignored throughout.

The Thesis pillar consumes this exact shape:
```
{"article_count": int, "mean_sentiment_score": float in [-1,+1],
 "mean_relevance_score": float in [0,1], "label_counts": {bullish/somewhat_bullish/neutral/somewhat_bearish/bearish: int}}
```
Anything we add must produce that shape (or be cheaply adapted to it).

---

## 1. TL;DR — Top 3 Recommendations

1. **Finnhub** (`finnhub.io`) — single best free addition. 60 req/min is effectively unlimited for 167 tickers/day, built-in `news-sentiment` endpoint returns Reuters-grade buzz/sentiment, response shape maps cleanly to LTHCS labels with one helper. **This is the AV replacement candidate.**
2. **Yahoo Finance via `yfinance.Ticker(...).news`** — zero-cost, zero-auth, comprehensive US coverage. No sentiment, but mention counts + headlines are enough to fill the gap for the ~120 tickers AV misses on any given day. Treat as a fallback layer, not a primary.
3. **MarketAux** (`marketaux.com`) — 100 req/day free with built-in sentiment + entity tagging. Lower daily ceiling than Finnhub but better aggregation breadth (Reuters + Bloomberg headlines via 3rd-party syndication). Use it as the tiebreaker / cross-validation signal on the highest-conviction names.

Combined uplift estimate: **47/167 → ~155/167** with daily fresh-news coverage if all three are wired. Details in §5.

---

## 2. Comparison Matrix

Sorted by recommendation strength (most-recommended first).

| API | Per-ticker | Sentiment? | Free quota | Auth | Update latency | Cost beyond free | Recommended? |
|---|---|---|---|---|---|---|---|
| **Finnhub** | Yes (`/company-news` + `/news-sentiment`) | Yes, native (bullish %, bearish %, buzz score) | 60 req/min, no daily cap on free tier (last I checked — confirm in dashboard) | API key only | ~Hourly for company news, real-time for top headlines | ~$50/mo "Starter" for premium endpoints (institutional data, earnings call transcripts) | **Strong yes** |
| **Yahoo Finance (yfinance)** | Yes (`Ticker.news`) | No — derive from titles | Unmetered (it's screen-scraping a public endpoint; be polite) | None | ~15-30 min behind | N/A (not a paid product; just don't hammer it) | **Yes — as fallback** |
| **MarketAux** | Yes (`?symbols=AAPL`) | Yes (`sentiment_score` per entity) | 100 req/day free | API key only | Real-time | $19/mo for 10k req/day | **Yes — supplemental** |
| **Polygon.io** | Yes (`/v2/reference/news`) | Insight tags (sentiment + reasoning) on some tickers | 5 req/min on free | API key only | Real-time | $29/mo Starter, $99/mo Developer | Maybe — rate cap kills batch use |
| **NewsAPI (newsapi.org)** | No — keyword search only (`q=AAPL`) | No | 100 req/day; **dev tier returns articles up to 24h old only** | API key only | Real-time on paid; 24h-delayed on free | $449/mo for commercial — non-starter | No |
| **GDELT 2.0** | Indirect (entity match) | Yes (multiple tone scores) | Free, no key — but URL query language has a steep learning curve | None | 15-min batched | N/A | Maybe — high effort, V3 candidate |
| **GNews** | No — keyword search | No | 100 req/day | API key only | Real-time | $50/mo+ | No |
| **StockTwits** | Yes (symbol streams) | Yes (user-tagged Bull/Bear) | Public stream is unauth but throttled; full API gated | Public read-only is open; OAuth for full | Real-time | Enterprise pricing | Maybe — social-only, noisy |
| **SEC EDGAR (8-K filings)** | Yes (filer CIK) | No — structural events only | Free, polite UA required | None | Same-day filings | N/A | Yes — but covered in `earnings-events.md` sibling doc |
| **Guardian Open Platform** | No — keyword search; UK-skewed | No | 5,000 req/day free, 12 calls/sec | API key | Real-time | Free for non-commercial | No (UK skew kills US-stock relevance) |
| **NYT Article Search API** | No — keyword search | No | 4,000 req/day, 10/min | API key | Daily | Free for non-commercial | No (low signal-per-call) |
| **RSS aggregators (Reuters/CNBC/MarketWatch/WSJ)** | Indirect (text match) | No | Unmetered (public feeds) | None | 15 min - 1h | N/A | Yes — see §3.5 |
| **HackerNews Algolia** | Already wired | No | Unmetered | None | Real-time | N/A | Already shipped |
| Reddit OAuth | Yes (`/r/wallstreetbets`, etc.) | Derivable | — | **Blocked per Bryan's memory** | — | — | **Skip** |
| **Alpha Vantage Premium** | Yes | Yes | 75 req/min on $50/mo, 1200/min on $250/mo | API key | Real-time | $49.99 - $249.99/mo | No — Finnhub gives more for $0 |
| **RavenPack** | Yes, gold-standard | Yes, gold-standard | None — institutional only | Custom | Real-time | $$$$ (5-6 figures/yr) | No (hobby project) |
| **Bloomberg Terminal API (BLPAPI)** | Yes | Yes | None — requires terminal subscription | Terminal | Real-time | ~$24k/yr/terminal | **Definitely no** |
| **Refinitiv (LSEG) Eikon/Workspace API** | Yes | Yes | None — institutional | OAuth + entitlements | Real-time | ~$22k/yr | No |

---

## 3. Per-API Deep Dive (Top 5)

### 3.1 Finnhub — `finnhub.io`

**What it solves for LTHCS.** Replaces Alpha Vantage's `NEWS_SENTIMENT` as the primary daily source. The free tier comfortably handles all 167 tickers per day (167 calls is ~3 minutes of capacity at 60 req/min). It also has a *dedicated* sentiment-aggregate endpoint that returns Bryan's exact pillar inputs without per-article looping.

**Sample call.**
```
GET https://finnhub.io/api/v1/news-sentiment?symbol=AAPL&token=YOUR_KEY
GET https://finnhub.io/api/v1/company-news?symbol=AAPL&from=2026-05-10&to=2026-05-17&token=YOUR_KEY
```

**Sample response — `news-sentiment` (this is the real shape, last verified mid-2024; double-check for renamed keys).**
```json
{
  "buzz": {
    "articlesInLastWeek": 47,
    "buzz": 0.835,
    "weeklyAverage": 28.7
  },
  "companyNewsScore": 0.617,
  "sectorAverageBullishPercent": 0.51,
  "sectorAverageNewsScore": 0.583,
  "sentiment": {
    "bearishPercent": 0.17,
    "bullishPercent": 0.83
  },
  "symbol": "AAPL"
}
```

**Sample response — `company-news` (one element of a list).**
```json
{
  "category": "company",
  "datetime": 1715865600,
  "headline": "Apple unveils new AI chip ahead of WWDC",
  "id": 7493012,
  "image": "https://...",
  "related": "AAPL",
  "source": "Reuters",
  "summary": "Apple Inc. announced ...",
  "url": "https://..."
}
```

**Mapping to LTHCS Thesis pillar shape.**
| LTHCS field | Finnhub source |
|---|---|
| `article_count` | `buzz.articlesInLastWeek` (already 7-day window — good enough proxy for "rolling 30d" V1 input) |
| `mean_sentiment_score` | `2 * sentiment.bullishPercent - 1` → maps [0,1] onto [-1,+1] |
| `mean_relevance_score` | `buzz.buzz` (already [0,1]-ish, normalised vs sector average) |
| `label_counts` | Derive: `bullish = round(bullishPercent * articlesInLastWeek)`, `bearish = round(bearishPercent * articlesInLastWeek)`, rest neutral. Lossy but adequate. |

For a richer `label_counts`, loop the `company-news` results through a lightweight title-keyword classifier (already in `lthcs/sources/ai_news.py` for AI/TC/VB).

**Implementation effort.** **S.** Single endpoint, single ticker per call, no rotation logic needed because 167 calls < 1 min of quota. Add `lthcs/sources/finnhub.py` mirroring the `alpha_vantage.py` module layout. Estimated 1-2 hours including tests.

**Known gotchas.**
- The `news-sentiment` endpoint is documented as US-equities-only — international ADRs may return empty `sentiment` blocks. Confirm before assuming coverage for ADR tickers in the 167-set.
- Finnhub has historically deprecated free-tier endpoints when usage spikes (the `recommendation-trends` endpoint moved behind paywall around 2023). The `news-sentiment` endpoint has been stable as of my last check but **not 100% guaranteed**. Build with a graceful-degradation path back to `company-news` + your own scoring.
- The free key sometimes serves slightly-stale aggregates (24h behind). If realtime matters, fall back to `company-news` and recompute.
- Symbol set is "primary US listing only" — `BRK.B` is `BRK.B`, but check edge cases like `BF.B`, `STZ.B`.

---

### 3.2 Yahoo Finance via `yfinance.Ticker(...).news`

**What it solves for LTHCS.** Free, zero-auth, mention-count coverage for every ticker in the 167-set. Doesn't replace sentiment, but pairs perfectly with the existing AI-news supplement: if Finnhub returns empty for ticker X, yfinance is almost always populated. Acts as the universal fallback.

**Sample call.** No URL — it's a Python library wrapping Yahoo's internal v2 quote API:
```python
import yfinance as yf
items = yf.Ticker("AAPL").news  # returns List[Dict]
```

**Sample response (one item).** Shape changed in yfinance 0.2.x to embed everything under a `content` key — be aware older blog posts show the flat schema.
```json
{
  "uuid": "abc-123",
  "title": "Apple to launch new MacBook lineup in October",
  "publisher": "Yahoo Finance",
  "link": "https://finance.yahoo.com/news/...",
  "providerPublishTime": 1715865600,
  "type": "STORY",
  "thumbnail": {"resolutions": [...]},
  "relatedTickers": ["AAPL", "TSM"]
}
```
(Newer yfinance versions return `{"id": ..., "content": {"title": ..., "pubDate": ..., "provider": {...}, ...}}` — verify against the actual installed version before parsing.)

**Mapping to LTHCS Thesis pillar shape.**
| LTHCS field | Yahoo source |
|---|---|
| `article_count` | `len(items)` (typically 8-20 articles; covers ~2 weeks) |
| `mean_sentiment_score` | **Not available** — set to `None` and let the pillar collapse to neutral 50 with low-confidence damping, OR run titles through a tiny VADER/lexicon scorer |
| `mean_relevance_score` | `1.0` if ticker is in `relatedTickers[0]`, `0.5` if not first — heuristic only |
| `label_counts` | Either all-neutral, or derive via title keywords |

**Implementation effort.** **S.** Add `lthcs/sources/yahoo_news.py` (note: `lthcs/sources/yahoo.py` already exists for price/recommendations — keep them separate or namespace cleanly). Estimated 1 hour incl. tests.

**Known gotchas.**
- yfinance scrapes Yahoo's *unofficial* JSON endpoint. Yahoo has rate-limited and CAPTCHA-walled it more than once (notably mid-2023). Cache aggressively (24h cache like AV) and back off on `requests.HTTPError`.
- The schema flips between yfinance versions. Pin a version in `requirements.txt` and test against `Ticker("AAPL").news` in CI smoke.
- No sentiment, period. If you want sentiment from Yahoo, you have to derive it client-side.
- Be VERY polite — a `User-Agent` mirroring the existing `LTHCS-Dashboard/1.0` UA in `ai_news.py` is correct. Adding random sleep jitter between calls (200-500ms) prevents Yahoo rate-limit pages.

---

### 3.3 MarketAux — `marketaux.com`

**What it solves for LTHCS.** Independent sentiment signal. MarketAux aggregates Reuters, Bloomberg headlines (via syndication), Seeking Alpha, Benzinga, and ~50 other outlets — different breadth than Finnhub (which is Reuters-heavy). Use it on the top-conviction 50 tickers per day as a cross-validation layer; disagreement between Finnhub and MarketAux is a useful data quality signal.

**Sample call.**
```
GET https://api.marketaux.com/v1/news/all
    ?symbols=AAPL,MSFT,NVDA
    &filter_entities=true
    &language=en
    &api_token=YOUR_KEY
```
The `filter_entities=true` flag is critical — it constrains results to articles where the entity match is the primary subject, not just incidentally mentioned. This is the OPPOSITE of AV's AND-not-OR quirk: MarketAux does OR-style multi-symbol, which is what you actually want.

**Sample response (one element of `data` list).**
```json
{
  "uuid": "abc-123-def",
  "title": "Apple Stock Hits Record High On Strong iPhone Demand",
  "description": "...",
  "url": "https://...",
  "published_at": "2026-05-17T13:24:00.000000Z",
  "source": "Reuters",
  "entities": [
    {
      "symbol": "AAPL",
      "name": "Apple Inc.",
      "exchange": "NASDAQ",
      "exchange_long": "NASDAQ Stock Exchange",
      "country": "us",
      "type": "equity",
      "industry": "Technology",
      "match_score": 78.5,
      "sentiment_score": 0.6234,
      "highlights": [...]
    }
  ]
}
```

**Mapping to LTHCS Thesis pillar shape.**
| LTHCS field | MarketAux source |
|---|---|
| `article_count` | `meta.found` (or len of returned list) |
| `mean_sentiment_score` | `mean([e.sentiment_score for e in entities if e.symbol == TICKER])` across the returned articles — note: already in [-1,+1] so no normalisation needed |
| `mean_relevance_score` | `mean(match_score) / 100` |
| `label_counts` | Bucket the per-article `sentiment_score`: `>0.35` bullish, `0.15-0.35` somewhat_bullish, `-0.15..0.15` neutral, `-0.35..-0.15` somewhat_bearish, `<-0.35` bearish. Thresholds are guesswork — AV uses different cutpoints — but the symmetry is right. |

**Implementation effort.** **M.** The actual integration is small (S), but the rotation logic is more involved than Finnhub: at 100 req/day with 3 symbols per call (free tier limits), you can cover ~300 ticker-checks/day, but a single request returns ~3 articles by default (`limit=3`). The right pattern is a **conviction-weighted rotation**: hit the top 50 tickers daily, rotate the rest on a 3-day cadence. The thesis-rotation module already shipped (`lthcs/sources/thesis_rotation.py`) is the template.

**Known gotchas.**
- The free tier limits `limit` to 3 articles per call. To get a meaningful article_count, you'd need multiple calls per ticker → blows the daily quota quickly.
- `sentiment_score` is computed via VADER (older articles) or an undocumented model — quality is "decent, not Bloomberg". Don't expect institutional-grade signal.
- The `published_at` timestamps occasionally lag the article URL by an hour or two; don't use MarketAux for intraday triggering.
- Pricing jump from free to $19/mo is small enough that if Bryan ever needs more, it's not a budget event. But you can probably stay free.

---

### 3.4 RSS aggregator bundle (Reuters / MarketWatch / CNBC / WSJ headlines)

**What it solves for LTHCS.** Cheap covering-fire for tickers that fall through the cracks of every other source. RSS is unmetered, no key required, and parseable with stdlib `xml.etree.ElementTree` — *which is already imported by `lthcs/sources/ai_news.py`*. The shape mirrors the existing TC/VB pattern: parse feed → regex-match ticker mentions in titles → count.

**Sample call (no auth, no rate limit, just polite UA).**
```
GET https://feeds.reuters.com/reuters/businessNews     (note: Reuters retired some feeds in 2023; verify)
GET https://feeds.marketwatch.com/marketwatch/topstories/
GET https://www.cnbc.com/id/100003114/device/rss/rss.html   (CNBC business news)
GET https://feeds.a.dj.com/rss/RSSMarketsMain.xml          (WSJ Markets)
```

**Sample response (truncated RSS item).**
```xml
<item>
  <title>Apple to Launch AI-Powered MacBook Pro in Q3</title>
  <link>https://www.marketwatch.com/...</link>
  <pubDate>Fri, 17 May 2026 14:23:00 +0000</pubDate>
  <description>Apple Inc. (AAPL) announced today that...</description>
  <category>Technology</category>
</item>
```

**Mapping.** Same approach as the existing `ai_news.py` — regex-match ticker symbol on title+description, count, set sentiment to None. The `label_counts` stays all-neutral until you add a lexicon scorer.

**Implementation effort.** **S.** Extend `lthcs/sources/ai_news.py` (or fork to `lthcs/sources/rss_news.py` if you'd rather keep AI-specific feeds separate) and add the feed URLs to the existing rotation. ~30 minutes per feed.

**Known gotchas.**
- Reuters consolidated/retired many of their public RSS feeds in 2023 after the Refinitiv split. The list of currently-working feeds is a moving target — start with what `https://www.reuters.com/sitemap.xml` actually serves.
- CNBC's RSS only carries the *front page* — about 30 stories/day, not searchable per-ticker. Coverage of mid-cap names is poor.
- WSJ's feeds are summary-only (paywall on the full article) — you only get title + 1-sentence description for ticker matching.
- Ticker-mention regex needs to be careful: `MSFT` matches `Microsoft` only via aliasing, and short tickers (`A`, `T`, `V`) match common English words. Use `\b(?:AAPL|MSFT|...)\b` with a longest-first ordering, and consider blocking 1-2 char tickers from this path entirely (let them ride on Finnhub).

---

### 3.5 GDELT 2.0 — `gdeltproject.org`

**What it solves for LTHCS.** *Potentially* the highest-value signal of the entire list — GDELT 2.0's Global Knowledge Graph tags every news article worldwide with entities (companies, people, locations), themes, and ~6 different tone metrics, updated every 15 minutes. **If Bryan ever wants international-event sentiment for ADRs or supply-chain signals, this is the only free source that even attempts it.**

**Why I'm hedging it.** The DOC API query language is genuinely hostile. Getting a clean "articles mentioning AAPL with tone scores in the last 24h" requires composing a URL like:
```
https://api.gdeltproject.org/api/v2/doc/doc?query=%22AAPL%22%20sourcecountry:US&mode=ArtList&format=json&maxrecords=250&timespan=24H&sort=hybridrel
```
And the resulting JSON is non-trivial to map onto a ticker (GDELT doesn't know ticker symbols — it knows company *names*). You'd need a ticker→company-name lookup (Bryan already has `fund_meta.py`), then post-filter on the GKG entity tags.

**Sample response (one element).**
```json
{
  "url": "https://...",
  "url_mobile": "",
  "title": "Apple Faces Supply Chain Headwinds In China",
  "seendate": "20260517T142300Z",
  "socialimage": "https://...",
  "domain": "reuters.com",
  "language": "English",
  "sourcecountry": "United States",
  "tone": -2.34
}
```
The tone score is the GDELT "tone" metric: roughly [-10, +10] but most values cluster in [-5, +5]. Linear-rescale to [-1, +1] for the LTHCS pillar.

**Mapping.**
| LTHCS field | GDELT source |
|---|---|
| `article_count` | `len(articles)` after filtering by `sourcecountry:US` and matching company name |
| `mean_sentiment_score` | `mean(tone) / 5.0`, clamped to [-1, +1] |
| `mean_relevance_score` | No clean equivalent; default to `0.6` |
| `label_counts` | Bucket tone by sign and magnitude |

**Implementation effort.** **L.** Query-language wrangling, ticker→name mapping, entity-tag post-filtering, plus retry logic (GDELT's API rate-limits opaquely — "be polite" is the official guidance). 6-10 hours minimum. **Defer to V3** unless international/supply-chain signal becomes a priority.

**Known gotchas.**
- API has no rate limit headers — you don't know you're being limited until you get a 429 or a 503. Cache aggressively.
- The DOC API and the BigQuery dataset disagree on schema in subtle ways (different tone fields). Stick with the DOC API for production; use BigQuery only for one-off historical analyses.
- Coverage of niche US tickers via name-matching is hit-or-miss; "Snowflake Inc" matches plenty of articles about literal snowflakes.

---

## 4. The LTHCS-Specific Recommendation

Given the constraints (free preferred; 167-ticker universe; daily refresh; AV+HN/TC/VB already shipped; rotation logic already exists):

### Highest-ROI addition: **Finnhub** (one source, replaces AV as primary)
- Solves the rate-limit problem in one shot. 167 daily calls fits in <3 min of free-tier capacity.
- Native sentiment field — no client-side derivation needed.
- Maps onto LTHCS pillar shape with a thin adapter (~30 LOC).
- Risk: if Finnhub deprecates `news-sentiment` from free tier (they have form for this), you fall back to `company-news` + your own scoring, which still beats AV's rotation pain.
- **Expected coverage uplift: 47/167 → ~130/167** (covers all US large/mid-cap; some thin coverage on ADRs and recent IPOs).

### Second addition: **Yahoo Finance (yfinance) as fallback layer**
- Different signal axis: where Finnhub returns empty (ADRs, micro-caps, recent IPOs), Yahoo almost always has 5+ articles.
- No auth, no key management, no rate cost — pure upside.
- Doesn't add sentiment, but the existing pillar handles `mean_sentiment_score=None` gracefully via confidence-blend toward 50.
- **Coverage uplift: 130/167 → ~155/167** after the Finnhub layer.

### Third addition: **MarketAux on top-50 conviction tickers**
- Independent cross-validation of Finnhub's sentiment direction.
- Disagreement between Finnhub and MarketAux on a ticker = "noisy news cycle" → useful data quality flag for the pillar's `data_quality` field.
- Only 50-100 calls/day → comfortably under the 100/day quota.
- **No further coverage uplift, but improves sentiment confidence on the high-stakes names.**

### What NOT to do
- **Don't pay for Alpha Vantage Premium.** Finnhub's free tier dominates the AV $50/mo tier on every dimension.
- **Don't pay for Bloomberg, Refinitiv, or RavenPack** — these are institutional products for trading desks billing $$$ per seat. Wildly inappropriate for a hobby dashboard.
- **Don't try to scrape Reddit without OAuth** — the unauth `.json` endpoints are aggressively rate-limited and Cloudflare-walled. Per Bryan's memory, the OAuth path is also gated by their Responsible Builder Policy. **Defer indefinitely.**
- **Don't wire NewsAPI.org** — 24h-delayed articles on the free tier and a $449/mo jump to anything usable means it's strictly worse than every other option.
- **Don't wire GDELT in V2.** Real signal, but the implementation cost (entity-mapping, query-language wrangling) is V3-tier work. Park it.
- **Don't add per-feed RSS aggregators piecemeal.** The existing `ai_news.py` pattern already proves you don't get incremental signal from feed #5, #6, #7 — each new feed mostly duplicates the previous one's coverage of top stories.

---

## 5. Combined Coverage Estimate

**Today** (AV + HN/TC/VB):
- ~47/167 (28%) tickers have AV NEWS_SENTIMENT data per daily run due to 25-req/day quota and rotation.
- HN/TC/VB adds *mention counts* (not sentiment direction) for an additional ~20-30 tickers — but only AI/tech-adjacent names. Most ETF holdings aren't covered.
- Realistic "fresh sentiment-bearing data per day": **~47/167 (28%)**

**After wiring Finnhub** (primary replacement):
- 167/167 attempted; ~130/167 return populated sentiment (78%)
- Empty returns concentrated in: international ADRs (~15), recent IPOs (~10), low-volume names (~12)
- **130/167 (78%)**

**After adding yfinance fallback layer:**
- Catches ~25 of the 37 tickers Finnhub misses (Yahoo has broader ADR and micro-cap coverage)
- Adds *mention count + headlines* but not sentiment direction → pillar uses neutral-50 with confidence damping
- **~155/167 (93%)** have *some* news signal; **~130/167 (78%)** have sentiment direction

**After adding MarketAux on top-50 cohort:**
- No further coverage uplift, but:
- **Sentiment confidence improves** on the top 50 names via two-source agreement check
- Disagreement (Finnhub bullish, MarketAux bearish, or vice versa) sets `data_quality.warning: "split_sentiment"` → pillar damps toward neutral

**Net of all three:** 155/167 with *some* signal, 130/167 with confirmed sentiment, top-50 cross-validated. The "missing 12" are likely permanent holes — micro-caps with genuinely no news flow — and the pillar should report `article_count=0` honestly rather than fabricate a signal.

---

## 6. Implementation Sequencing

Ship in this order. Each step is independently shippable and leaves the system better than before.

### Step 1 — Finnhub primary (1-2 hours)
- Add `lthcs/sources/finnhub.py` mirroring `alpha_vantage.py` layout.
- Add `FINNHUB_API_KEY` to env / Secrets.
- Add a smoke check in `lthcs/sources/smoke.py`.
- Update `lthcs/pillars/thesis.py` to prefer Finnhub when available, fall back to AV.
- Tests: inline JSON fixtures for `news-sentiment` and `company-news`, no network in CI.
- **Ship gate:** LTHCS smoke run shows Finnhub returns populated data for 5 sample tickers spanning AAPL/AMD/COIN/IBIT/an-ADR.

### Step 2 — yfinance fallback (1 hour)
- Add `lthcs/sources/yahoo_news.py` (separate from existing `yahoo.py` for prices).
- Pin yfinance version in `requirements.txt`; the news schema has flipped between minor versions.
- In `thesis.py`, only call yfinance when Finnhub returns `article_count == 0`.
- **Ship gate:** the 12-15 ADRs that Finnhub misses now report `article_count >= 3` from yfinance.

### Step 3 — MarketAux cross-validation on top 50 (2-3 hours)
- Add `lthcs/sources/marketaux.py`.
- Define "top 50" as: ETF underlying holdings with the largest weight in IBIT, FBTC, ETHA, GLD, etc. — `fund_meta.py` already has the weights.
- Add a new `data_quality.cross_source_agreement: "agree" | "disagree" | "single"` field. Disagreement damps the sub-score toward 50 by 30%.
- **Ship gate:** disagreement rate < 20% on the top-50 cohort (if higher, the sentiment threshold buckets need recalibrating).

### Step 4 — Retire Alpha Vantage from the daily pipeline (15 min)
- Keep the source module (it's good code) but stop calling `get_news_sentiment` in the daily run.
- Reclaim the 25 daily requests for the *fundamental data* endpoints (earnings calendar, etc.) that AV is actually best-in-class for.

### Step 5 (V3, defer) — GDELT for supply-chain / international signal
- Only if Bryan adds non-US ETF exposure or wants supply-chain-disruption tracking.
- Budget 6-10 hours.

### Step 6 (V3, defer) — Polygon.io if "insight" tags prove valuable
- Polygon's news API has interesting "insight" tags (sentiment + reasoning text per ticker per article) but the 5 req/min cap forces rotation logic and the free tier doesn't justify the work.

---

## 7. References — Bookmarks Bryan Should Save

1. **Finnhub API docs (news endpoints)** — `https://finnhub.io/docs/api/company-news` and `https://finnhub.io/docs/api/news-sentiment` — read both before implementing; the field names matter and have changed once or twice historically.
2. **yfinance source on GitHub** — `https://github.com/ranaroussi/yfinance` — when the `Ticker.news` schema flips, the `_news` parser in this repo is the source of truth. Look at `tests/test_news.py` for the latest expected shape.
3. **MarketAux API docs** — `https://www.marketaux.com/documentation` — the `filter_entities=true` flag and the `sentiment_score` range are documented inconsistently across blog tutorials; trust the official docs.
4. **GDELT 2.0 DOC API cookbook** — `https://blog.gdeltproject.org/announcing-the-gdelt-2-0-doc-api/` and `https://api.gdeltproject.org/api/v2/doc/doc?query=help` — the closest thing to a query-language reference. Plan on an afternoon of experimenting before this clicks.
5. **Alpha Vantage NEWS_SENTIMENT response shape** — `https://www.alphavantage.co/documentation/#news-sentiment` — useful as the *contract* the LTHCS pillar consumes. Any new source has to project onto this shape (or the pillar has to be generalised).

---

## Appendix A — Notes on Confidence in My Own Numbers

A few honest caveats:

- **Rate limits and pricing tiers change.** The numbers above are accurate as of my late-2024 knowledge cutoff with some 2025-2026 adjustments where I have confidence. Verify the Finnhub free-tier `news-sentiment` endpoint is still free on signup before committing to it as primary — the cost of being wrong is "fall back to `company-news` and do your own scoring", which is ~3 extra hours.
- **Coverage uplift numbers (47 → 155) are estimates** based on rough breakdowns of where US-listed coverage tends to drop off (ADRs, recent IPOs, micro-caps). The actual number for Bryan's specific 167-ticker universe will depend on its composition — if it's heavily ETF-underlying-holdings of big indices, expect closer to 165/167; if it has a long tail of crypto-related micro-caps, expect closer to 140/167.
- **MarketAux's sentiment quality is described as "VADER or similar"** — I'm not 100% sure of the current model; their docs are vague. The threshold bucketing in §3.3 is a starting point, not a calibrated mapping.
- **Bloomberg/Refinitiv/RavenPack pricing** is from public reports + analyst estimates; actual contracts vary wildly by seat count and data feeds. The takeaway ("massively expensive, not for hobbyists") holds regardless of the exact number.

When in doubt, ship Step 1 (Finnhub) first, measure actual coverage, and recalibrate the plan before committing to Steps 2-3.
