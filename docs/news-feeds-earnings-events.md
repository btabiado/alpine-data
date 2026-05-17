# News Feeds: Earnings, Analyst Actions, and SEC 8-K Material Events

> **Scope.** Event-driven news sources for LTHCS Phase 2/3. This is the
> third companion doc to `news-feeds-general-apis.md` (broad sentiment
> streams) and `news-feeds-sector-specific.md` (vertical feeds). Where
> those docs catalog *streams of headlines*, this one catalogs
> *discrete, structured events*: an earnings beat, an analyst upgrade, a
> CEO departure 8-K. These are higher-information per byte than generic
> news mentions and they map cleanly onto the Thesis Integrity pillar
> (and, for analyst actions, onto Institutional Confidence).
>
> **Why event-driven feeds matter for LTHCS.** Today the Thesis pillar
> reads Alpha Vantage `NEWS_SENTIMENT` once per day and collapses to
> neutral 50 when sample size is thin or the AV `tickers=` filter
> returns AND-not-OR matches (see `alpha_vantage_news_sentiment_quirk`).
> The pillar is honest about that, but it's also flat: a quarter where
> a company beats by 12% and raises guidance scores the same as a
> quarter where it crawls in-line, because neither leaves a strong
> sentiment trail in the AV snapshot. Event feeds fix that by giving us
> a separate, structured signal that lives alongside sentiment.

---

## Table of contents

1. Earnings beats / misses + guidance
2. Analyst upgrades / downgrades + target-price changes
3. SEC 8-K material event filings
4. Implementation design (`lthcs/sources/*.py` sketches)
5. Integration patterns with existing pillars
6. Ranked recommendation (what to ship first)
7. Anti-patterns and gotchas
8. Reference URLs

---

## Section 1 — Earnings beats / misses + guidance

The earnings event is the single highest-information moment of a
quarter for a public company. We want four things out of it:

* **EPS actual vs. estimate** (beat / in-line / miss)
* **Revenue actual vs. estimate**
* **Forward guidance** (raised / maintained / lowered / withdrawn)
* **Conference call date** (so we can pre-position the daily run)

### 1.1 Yahoo Finance via `yfinance`

* **Cost:** free, no key. Already a dependency (`lthcs/sources/yahoo.py`).
* **Endpoint surface:**
  * `Ticker(symbol).earnings_dates` — upcoming + recent earnings,
    columns: `EPS Estimate`, `Reported EPS`, `Surprise(%)`.
  * `Ticker(symbol).calendar` — next event metadata (date, revenue
    estimate range).
  * `Ticker(symbol).income_stmt` / `quarterly_income_stmt` — gives the
    *actual* revenue once a quarter posts, useful for our own revenue
    surprise vs. consensus if we cache the consensus separately.
* **Coverage:** essentially 100% of the LTHCS universe (all US-listed
  large caps). Yahoo's surprise % is populated within an hour of the
  release for liquid names; small caps can lag.
* **Sample call:**
  ```python
  import yfinance as yf
  t = yf.Ticker("AAPL")
  df = t.earnings_dates           # DataFrame indexed by event datetime
  upcoming = t.calendar           # dict-like; "Earnings Date" key
  ```
* **Extract:** for each row of `earnings_dates` younger than `today -
  90d`, take `Reported EPS - EPS Estimate` and `Surprise(%)`. A beat
  is `Surprise(%) > +1` (the noise band — a 0.4% beat is in-line for
  scoring purposes).
* **Verdict: ship-it source.** Same dependency we already pull for
  prices; coverage is full-universe; rate limit budget shared with our
  existing yahoo bucket (10 burst / 1 rps).

### 1.2 Finnhub

* **Cost:** free tier 60 req/min with a single key. Sign-up is
  email-only, no card.
* **Endpoint surface:**
  * `GET /calendar/earnings?from=YYYY-MM-DD&to=YYYY-MM-DD` — full
    market calendar for any window. Each row: `symbol`, `epsActual`,
    `epsEstimate`, `revenueActual`, `revenueEstimate`, `hour` (bmo/amc).
  * `GET /stock/earnings?symbol=AAPL` — last 4 quarters surprise.
  * `GET /stock/earnings-quality-score` — proprietary numeric, ignore
    in V2 (smells like noise).
* **Coverage:** full US market + most ADRs. Better revenue surprise
  coverage than Yahoo (Yahoo often omits revenue actual until the 10-Q
  files).
* **Sample call:**
  ```bash
  curl "https://finnhub.io/api/v1/calendar/earnings?from=2026-05-15&to=2026-05-22&token=$FINNHUB_KEY"
  ```
* **Verdict: best free dedicated source for forward calendar +
  revenue surprise.** Worth a Phase 2 wire-in if Yahoo's revenue
  coverage proves spotty for our universe.

### 1.3 Zacks

* **Cost:** free RSS, paid for structured API.
* **Endpoint:** `https://www.zacks.com/stock/research/{TICKER}/earnings-announcements`
  — HTML; their `rss.xml` feeds are headline-level.
* **Coverage:** US large + mid cap.
* **Verdict: skip.** RSS gives us headlines, not structured EPS deltas.
  Scraping the HTML earnings page is fragile. Yahoo and Finnhub both
  do this better.

### 1.4 Earnings Whispers / Estimize

* **Cost:** Earnings Whispers is paid for the "whisper number" data.
  Estimize was acquired by ExtractAlpha and is no longer offering a
  public free tier as of 2024.
* **Coverage:** crowdsourced — strong on widely-held names, sparse on
  smaller universe entries.
* **Verdict: skip.** The "whisper number" thesis is interesting (it
  captures buy-side expectation drift beyond sell-side consensus) but
  the value-for-cost doesn't pencil for a hobby dashboard. Note this
  as a deferred V4 nice-to-have if LTHCS ever monetizes.

### 1.5 SEC EDGAR 8-K item 2.02

* **Cost:** free, no key, ~10 rps rate limit. Already a dependency
  (`lthcs/sources/sec_edgar.py`).
* **Mechanic:** companies file an 8-K with item 2.02 ("Results of
  Operations and Financial Condition") within four business days of an
  earnings release — usually same day or next morning. The
  press-release PDF is attached as exhibit 99.1.
* **Pros:** authoritative, real-time, the *exact* document the company
  published. Doesn't lag like sentiment aggregators.
* **Cons:** unstructured — to extract EPS / revenue / guidance you
  either parse the press-release exhibit (messy) or you don't. The 8-K
  *header* is structured (filing date, item code) but the body is not.
* **Verdict: use as a trigger + dating signal, not a metrics source.**
  When an 8-K with item 2.02 lands, set a flag "earnings event for
  TICKER today" and pull the actual EPS / revenue numbers from Yahoo
  or Finnhub. The 8-K is the timer; Yahoo/Finnhub is the structure.

### 1.6 Alpha Vantage `EARNINGS` function

* **Cost:** burns 1 of our 25 daily AV requests, but **does NOT touch
  the NEWS_SENTIMENT quota** (separate function). Already configured
  (`lthcs/sources/alpha_vantage.py` holds the key).
* **Endpoint:** `function=EARNINGS&symbol=AAPL` — returns annual +
  quarterly EPS actual and estimate; `function=EARNINGS_CALENDAR` —
  bulk CSV of upcoming earnings (no key needed for the calendar
  variant, surprisingly).
* **Pros:** consolidates with our existing AV plumbing; no new client.
* **Cons:** quota cost. The daily run already burns ~4–6 AV calls; if
  we use AV `EARNINGS` per-ticker over the 75-name universe we'd blow
  through 25/day on the first sweep alone. The bulk calendar CSV
  sidesteps this.
* **Verdict: use the bulk `EARNINGS_CALENDAR` CSV (no quota cost)** as
  a forward-looking signal. Skip per-ticker `EARNINGS` calls.

### 1.7 Selection summary (earnings)

| Source | Cost | Coverage | EPS Δ | Rev Δ | Guidance | Verdict |
|---|---|---|---|---|---|---|
| yfinance | free | 100% | Y | partial | N | ship-it |
| Finnhub | free 60/min | ~100% US | Y | Y | N | Phase 2 |
| AV EARNINGS_CALENDAR | free | ~100% US | dates only | — | — | use for forward |
| SEC 8-K item 2.02 | free | 100% | trigger only | trigger only | trigger only | timing signal |
| Zacks | free RSS | partial | — | — | — | skip |
| Estimize / Whispers | paid | partial | — | — | — | defer |

**Guidance** is the gap: none of the free structured sources extracts
"raised / maintained / lowered" reliably. Pragmatic V2 plan: keep
guidance as a *narrative-only* field surfaced via the press-release
text from the 8-K item 2.02 exhibit, with a manual override slot.
Don't try to regex it.

---

## Section 2 — Analyst upgrades / downgrades + target-price changes

Sell-side analyst actions are a leading indicator of institutional
flows. An upgrade from Sell to Buy at a major shop reliably moves
short-term price; the durability is more contested but at minimum it's
useful for Thesis sentiment direction and Institutional Confidence
peer comparison.

### 2.1 Yahoo Finance via `yfinance`

* **Cost:** free, already a dependency.
* **Endpoint surface:**
  * `Ticker(symbol).recommendations` — DataFrame of `firm`, `to grade`,
    `from grade`, `action` (upgrade / downgrade / init / reiterated)
    indexed by datetime.
  * `Ticker(symbol).recommendations_summary` — count of buy / hold /
    sell across firms.
  * `Ticker(symbol).analyst_price_targets` — current consensus high /
    low / mean / median (newer addition to yfinance).
  * `Ticker(symbol).upgrades_downgrades` — recent actions, same shape
    as `recommendations` on most builds.
* **Coverage:** ~100% of the LTHCS universe; Yahoo aggregates from
  Refinitiv / S&P feeds.
* **Sample extract:**
  ```python
  import yfinance as yf
  rec = yf.Ticker("MSFT").recommendations
  recent = rec.tail(10)            # last 10 actions
  upgrades = recent[recent["Action"] == "up"]
  ```
* **Verdict: ship-it source.** Best free option for the LTHCS
  universe; integrates with the existing Yahoo client; cost is shared
  rate budget.

### 2.2 Finnhub recommendation trends

* **Cost:** free tier 60 req/min.
* **Endpoint:** `/stock/recommendation?symbol=AAPL` — monthly snapshot
  of strong-buy / buy / hold / sell / strong-sell counts going back ~2
  years.
* **Strength:** trend over time (not just current snapshot). A ticker
  going from 12 buys / 4 holds / 2 sells in March to 6 buys / 8 holds
  / 4 sells in May is a clear institutional-confidence erosion signal
  that the Yahoo "current consensus" can't show.
* **Coverage:** same as their earnings endpoint — full US market.
* **Verdict: best dedicated source for *trend* in analyst stance.**
  Wire-in candidate for Phase 3 if we want a directional analyst
  signal beyond raw event count.

### 2.3 TipRanks

* **Cost:** free limited tier — 5 lookups per day before paywall.
* **Endpoint:** no documented public API; their site is React-rendered
  and rate-limits scrapers aggressively.
* **Verdict: skip.** Quota too thin to be useful at universe size.

### 2.4 Benzinga

* **Cost:** their structured analyst-actions API is paid ($300/mo+).
  They have a free RSS at `https://www.benzinga.com/feed` but it's
  catch-all news, not filtered analyst events.
* **Verdict: skip for hobby budget.** Note: Benzinga's data is what
  Yahoo and many free aggregators rebroadcast on a delay — paying for
  the real-time feed only makes sense for trading, not for daily-cadence
  Thesis scoring.

### 2.5 MarketBeat

* **Cost:** paid ($25/mo+). Email digests are free but unstructured.
* **Verdict: skip.** Same logic as Benzinga.

### 2.6 NewsAPI / GNews keyword filter

* **Cost:** NewsAPI free tier = 100 requests/day, GNews free tier =
  100 requests/day.
* **Mechanic:** search for `"<TICKER> upgraded"` OR `"<TICKER>
  downgraded"` OR `"price target"` and post-filter.
* **Coverage:** broad media (Reuters, Bloomberg, MarketWatch, Seeking
  Alpha) but noisy — keyword search can return discussion *about* an
  upgrade without the actual event.
* **Verdict: backup, not primary.** Useful for cross-checking yfinance
  when it's missing recent actions; not worth a quota burn as the
  primary source. Document the keyword recipe but don't ship it as a
  default in V2.

### 2.7 Selection summary (analyst actions)

| Source | Cost | Coverage | Action history | Target Δ | Verdict |
|---|---|---|---|---|---|
| yfinance | free | ~100% | Y | Y (consensus only) | ship-it |
| Finnhub recommendations | free 60/min | ~100% US | Y (monthly trend) | N | Phase 3 wire-in |
| TipRanks | thin free | partial | Y | Y | skip |
| Benzinga | $$$ | full | Y | Y | skip |
| MarketBeat | $$ | full | Y | Y | skip |
| NewsAPI/GNews keyword | free thin | broad | N (noisy) | N | backup only |

---

## Section 3 — SEC 8-K material event filings

This is the structurally cleanest event source we have access to. 8-K
is the "current report" companies file for material events between
quarterly 10-Q / 10-K filings. SEC requires filing within four
business days. Each filing has one or more **item codes** identifying
the event type — and that's the gold: we can pattern-match on the
item code to know what *kind* of event happened, even before reading
the body.

### 3.1 8-K item codes — opinionated map to LTHCS signals

Not every 8-K item matters. Below is an opinionated take on each.

| Item | Description | Thesis signal? | Notes |
|---|---|---|---|
| 1.01 | Material definitive agreement | maybe + | Mixed — could be a great customer win or a routine vendor agreement. Don't auto-score; flag for narrative review. |
| 1.02 | Termination of a material agreement | usually − | Strong negative if it's a top-10 customer or a major partnership. Auto-flag as Thesis-break candidate. |
| 1.03 | Bankruptcy or receivership | strong − | Auto Thesis-break. Tier the score to ~0. |
| 2.01 | Completion of acquisition or disposition | + | Direction depends on whether it's acquiring or divesting. Surface in narrative, don't auto-score numerically. |
| 2.02 | Results of operations and financial condition | EARNINGS trigger | Use as a "the earnings just landed" signal — pair with Section 1 for the numbers. |
| 2.03 | Creation of a material financial obligation | weak − | New debt issuance. Sometimes positive (capex funded) but more often dilutive. Narrative-surface; no auto-score. |
| 2.04 | Triggering events that accelerate financial obligations | strong − | Debt covenant breach. Auto Thesis-break candidate. |
| 2.05 | Costs associated with exit or disposal activities | − | Restructuring charge / layoffs. Mild negative for Thesis (forward earnings haircut); could be positive long-term if the restructuring fixes a margin problem — narrative-surface, don't auto-score. |
| 2.06 | Material impairment | − | Goodwill write-down → past acquisition didn't pan out. Mild Thesis-negative. |
| 3.01 / 3.02 | Notice of delisting; unregistered securities sales | − | 3.01 is a strong negative (NYSE / Nasdaq compliance breach). 3.02 is dilution. |
| 4.01 | Changes in registrant's certifying accountant | − | Auditor change is a yellow flag — could be routine, but often precedes a restatement. Narrative-only. |
| 4.02 | Non-reliance on previously issued financial statements | very strong − | Restatement. Auto Thesis-break candidate. |
| 5.01 | Changes in control of registrant | + | M&A close from the target's side. |
| 5.02 | Departure or appointment of officers | depends | CEO/CFO departure is *material*. New-hire announcements are usually positive. Auto-flag with item code, but disambiguate departure vs. appointment by parsing the title line. |
| 5.03 | Amendments to articles or bylaws | neutral | Usually routine governance housekeeping. Ignore. |
| 5.04 | Temporary suspension of trading | strong − | Rare. Auto Thesis-break. |
| 5.05 | Amendments to code of ethics | neutral | Ignore. |
| 5.06 | Change in shell company status | neutral | SPAC-related. Ignore for the LTHCS large-cap universe. |
| 5.07 | Submission of matters to a vote of security holders | mostly neutral | Annual meeting vote results — boring. Ignore by default. |
| 5.08 | Shareholder director nominations | neutral | Ignore. |
| 6.x | Asset-backed securities events | neutral | LTHCS universe is mostly operating companies; ignore. |
| 7.01 | Regulation FD disclosure | maybe | Catch-all for non-material-but-disclosable announcements. Sometimes contains guidance updates. Narrative-surface only. |
| 8.01 | Other events | maybe | The catch-all bucket. Routine corporate ones are noise; surprise announcements (e.g., big customer win, dividend changes, share buyback authorization) live here. Narrative-surface; no auto-score. |
| 9.01 | Financial statements and exhibits | informational | Always co-filed with another item — use it to find the press-release exhibit attached to a 2.02. |

**Filter strategy in code.** Auto-score the strong-signal items
(1.02, 1.03, 2.04, 4.02, 5.04 → Thesis-break; 5.02-departure →
Thesis-break candidate). Narrative-surface the medium ones (2.01,
2.05, 2.06, 5.02-appointment, 7.01, 8.01). Ignore the boring ones
(3.03, 5.03, 5.05, 5.07, 5.08, 6.x).

### 3.2 Where to pull 8-K data

SEC EDGAR offers three ways to retrieve filings:

* **`data.sec.gov/submissions/CIK{padded_cik}.json`** — returns the
  most recent ~1,000 filings for one company, with item codes already
  parsed in `items` field. LTHCS already pulls this for company facts.
  Cheap to extend.
* **`www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=8-K&dateb=&owner=include&count=40&output=atom`** —
  Atom XML feed; same data, slower to parse.
* **`efts.sec.gov/LATEST/search-index?q=...&forms=8-K`** — full-text
  search across all filings. Useful for "which companies filed 8-K
  item 1.02 in the last 7 days" market-wide queries; not needed for
  the per-universe-ticker pull.

**Recommended path for LTHCS.** Use the per-CIK `submissions` JSON.
Format:
```python
recent = data["filings"]["recent"]
for form, date, items, accession in zip(
    recent["form"], recent["filingDate"],
    recent["items"], recent["accessionNumber"],
):
    if form == "8-K":
        item_codes = items.split(",")        # e.g. "5.02,9.01"
        ...
```
`items` is a comma-separated string of item codes. Already structured.
No HTML parsing required.

### 3.3 Rate limit + caching

* SEC allows 10 rps from one user agent. The existing
  `sec_edgar.TokenBucket(10, 10.0)` handles this.
* The `submissions` JSON is recomputed by SEC on each new filing, so a
  24h cache is fine for the universe-wide daily run. Per-filing detail
  (the full text) only needs to be fetched once and then cached
  for 7 days (filings are immutable post-amendment).

### 3.4 Selection summary (8-K)

| Endpoint | Cost | Latency | Structured items | Verdict |
|---|---|---|---|---|
| `data.sec.gov/submissions/CIK{cik}.json` | free | ~hours | yes (items field) | **primary** |
| `browse-edgar` Atom feed | free | ~hours | yes (XML parse) | skip — JSON is easier |
| `efts.sec.gov/LATEST/search-index` | free | ~minutes | yes (full text) | optional Phase 3 |

---

## Section 4 — Implementation design

Three new modules, mirroring `sec_edgar.py` shape. All three share
the existing `FileCache` and `TokenBucket` infra in
`lthcs/sources/_cache.py` and `lthcs/sources/_ratelimit.py`.

### 4.1 `lthcs/sources/earnings.py`

```python
"""Earnings surprise + calendar feed.

Sources, in order of preference:
  1. yfinance .earnings_dates (free, in-process)
  2. Finnhub /calendar/earnings (free, 60/min; optional fallback)

Caching:
  - Per-ticker quarterly surprises cached 24h. The data is stale-tolerant
    because surprises don't revise post-filing.
  - The forward calendar cached 6h (more volatile — companies update
    expected report dates).

Rate limit:
  - yfinance shares the existing _bucket (10 burst / 1 rps).
  - Finnhub gets its own TokenBucket(60, 1.0) if wired in.

Output → LTHCS pillar inputs:
  - Per-ticker `last_surprise` dict feeds the Thesis pillar event sub-score.
  - `upcoming` dict feeds the dashboard narrative ("AAPL reports tomorrow").
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class EarningsSurprise:
    ticker: str
    report_date: str         # YYYY-MM-DD
    eps_actual: Optional[float]
    eps_estimate: Optional[float]
    eps_surprise_pct: Optional[float]
    revenue_actual: Optional[float]
    revenue_estimate: Optional[float]
    revenue_surprise_pct: Optional[float]
    fiscal_period: Optional[str]   # e.g. "Q2 2026"


def get_recent_surprises(
    ticker: str, lookback_days: int = 120,
) -> List[EarningsSurprise]:
    """Most recent earnings surprises within the lookback window."""

def get_upcoming_event(ticker: str) -> Optional[Dict[str, Any]]:
    """Next scheduled earnings event, or None if none in the next 90 days."""

def classify_surprise(surprise: EarningsSurprise) -> str:
    """Return one of: 'beat', 'in_line', 'miss'.

    Bands: > +1% = beat, between -1% and +1% = in_line, < -1% = miss.
    """

def to_thesis_event_score(surprise: EarningsSurprise) -> float:
    """Map a surprise into the Thesis event sub-component in [-1, +1].

    Beat → +0.5 base, scaled by min(surprise_pct / 10, 1).
    Miss → -0.3 base, scaled the same way.
    In-line → 0.
    Revenue surprise gets half weight on top.
    """
```

### 4.2 `lthcs/sources/analyst_actions.py`

```python
"""Analyst upgrades / downgrades + target price changes.

Sources:
  1. yfinance .recommendations + .analyst_price_targets (primary, free)
  2. Finnhub /stock/recommendation (Phase 3 — adds trend over time)

Caching:
  - Recommendations cached 24h. Action history is append-only.
  - Price target consensus cached 24h. Slower-moving than headlines.

Rate limit:
  - Shares the yahoo bucket (10 burst / 1 rps).

Output → LTHCS pillar inputs:
  - Recent actions feed an event sub-score on Thesis (upgrade = +0.4,
    downgrade = -0.4, init = 0 unless from a top-tier shop).
  - Target-price delta vs. current price feeds Institutional Confidence
    as a forward-implied return proxy.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class AnalystAction:
    ticker: str
    date: str                # YYYY-MM-DD
    firm: str
    from_grade: Optional[str]
    to_grade: Optional[str]
    action: str              # 'up' / 'down' / 'init' / 'reit' / 'maintain'
    target_price: Optional[float]


def get_recent_actions(
    ticker: str, lookback_days: int = 30,
) -> List[AnalystAction]:
    """All upgrade/downgrade actions in the window, newest first."""

def get_target_price_consensus(ticker: str) -> Dict[str, Optional[float]]:
    """Return {mean, median, high, low, count} from analyst targets."""

def to_thesis_event_score(actions: List[AnalystAction]) -> float:
    """Aggregate recent actions into a Thesis event score in [-1, +1].

    Each upgrade = +0.4, downgrade = -0.4, top-firm modifier ×1.5.
    Clamped to [-1, +1].
    """

def to_institutional_target_score(
    consensus: Dict[str, Optional[float]], current_price: float,
) -> Optional[float]:
    """Return implied 12-mo return = (mean_target / current_price) - 1.

    Returns None if consensus is missing or has fewer than 3 analysts.
    """
```

### 4.3 `lthcs/sources/sec_8k_events.py`

```python
"""SEC 8-K material event feed.

Reuses the existing sec_edgar.get_cik + the /submissions endpoint
(already cached + rate-limited via sec_edgar's bucket).

Sources:
  - https://data.sec.gov/submissions/CIK{cik}.json   (primary)
  - https://www.sec.gov/Archives/.../{accession}.txt (per-filing, optional)

Caching:
  - The submissions JSON cache is 24h (matches sec_edgar default).
  - Per-filing detail (when fetched) cached 7d — filings are immutable.

Rate limit: shared sec_edgar bucket (10 rps).

Output → LTHCS pillar inputs:
  - Each 8-K Event maps to an opinionated category and an optional
    Thesis-break flag.
  - Categories: 'earnings_trigger', 'thesis_break', 'thesis_break_candidate',
    'narrative_only', 'ignore'.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# Item-code policy table. Keys are item codes ('1.01', '5.02', ...),
# values are (category, score_hint, narrative_note).
_ITEM_POLICY: Dict[str, Tuple[str, Optional[float], str]] = {
    "1.02": ("thesis_break_candidate", -0.6, "Material contract termination"),
    "1.03": ("thesis_break",          -1.0, "Bankruptcy / receivership"),
    "2.02": ("earnings_trigger",       None, "Earnings released"),
    "2.04": ("thesis_break_candidate", -0.7, "Debt covenant accelerated"),
    "2.05": ("narrative_only",         None, "Restructuring charge"),
    "2.06": ("narrative_only",        -0.2, "Material impairment"),
    "3.01": ("thesis_break_candidate", -0.8, "Delisting notice"),
    "4.01": ("narrative_only",         None, "Auditor change"),
    "4.02": ("thesis_break",          -0.9, "Financial restatement"),
    "5.02": ("narrative_only",         None, "Officer change — disambiguate"),
    "5.04": ("thesis_break",          -1.0, "Trading suspension"),
    "7.01": ("narrative_only",         None, "Reg FD disclosure"),
    "8.01": ("narrative_only",         None, "Other material event"),
    # Everything else: 'ignore'.
}


@dataclass(frozen=True)
class EightKEvent:
    ticker: str
    cik: str
    filing_date: str       # YYYY-MM-DD
    accession_number: str
    items: List[str]       # e.g. ['5.02', '9.01']
    category: str          # 'earnings_trigger' / 'thesis_break' / ...
    score_hint: Optional[float]   # in [-1, +1] when scoreable
    notes: List[str]       # human-readable, one per scoreable item


def get_recent_8k_events(
    ticker: str, lookback_days: int = 90,
) -> List[EightKEvent]:
    """All 8-K filings for ticker in the window, newest first.

    Pulls the per-CIK submissions JSON, filters form == '8-K', joins
    items against _ITEM_POLICY to produce the EightKEvent objects.
    """

def is_thesis_break(event: EightKEvent) -> bool:
    """True for category 'thesis_break' (auto) — caller decides on candidates."""

def to_thesis_event_score(events: List[EightKEvent]) -> float:
    """Aggregate recent events into a Thesis event score in [-1, +1].

    Sum score_hints, clamped. A single -1.0 dominates positives — it's a
    Thesis-break, not a sentiment input.
    """
```

### 4.4 Cross-module concerns

* **Identity / ticker normalization.** All three modules accept the
  same uppercase ticker shape as `sec_edgar.get_cik`. The 8-K module
  should use `sec_edgar.get_cik` directly rather than re-implementing
  the ticker → CIK map.
* **Cache root.** Honor `LTHCS_CACHE_DIR` like the existing modules
  so tests can redirect to a temp dir.
* **Failure modes.** Each `get_recent_*` raises a typed exception
  (e.g. `EarningsSourceError`) on upstream failure; the daily pipeline
  catches and collapses to "no events known today" → neutral 0 event
  score, so a Finnhub outage doesn't take down the run.
* **Empty results.** Explicitly return `[]`, never `None`. Aggregators
  rely on `len(events)` semantics.
* **Pure scoring.** All `to_*_score` helpers are pure functions —
  tests can fixture-feed them without network.

---

## Section 5 — Integration with existing LTHCS pillars

Three patterns to consider, each with trade-offs:

### Pattern A — Roll events into Thesis sentiment

* **Mechanic.** Compute a per-event score in [-1, +1] (`to_*_score`),
  blend with AV sentiment using a fixed weight (e.g. 60% sentiment,
  40% events), then run through the same `bounded_linear` → 0-100
  mapping the pillar already uses.
* **Pros.** Single sub-score; backwards-compatible with the
  `compute_thesis` API; doesn't require schema changes downstream.
* **Cons.** Loses information — a -0.9 8-K item 4.02 restatement
  shouldn't be "smoothed" by a +0.2 sentiment reading. Numerically
  averaging them is the wrong shape.

### Pattern B — Thesis-break flag (separate from numeric score)

* **Mechanic.** Strong-signal items (1.02, 1.03, 2.04, 4.02, 5.04,
  optionally CEO-departure 5.02) set a boolean `thesis_break` flag on
  the ticker's row. The Thesis pillar score stays sentiment-driven,
  but the dashboard surfaces the flag prominently in the narrative
  and the ticker drops to the bottom of the ranking until acknowledged.
* **Pros.** Preserves narrative clarity — "this ticker is broken
  because…" is more useful than "this ticker's score dropped 8 points."
  Matches how a real PM thinks about material events.
* **Cons.** Requires schema additions (`data_quality.thesis_break_flag`
  in the Thesis output dict) and downstream UI work.

### Pattern C — Events as a Thesis sub-component

* **Mechanic.** Add an `events_score` field alongside `sentiment_score`
  in the Thesis components dict. Combine them with a fixed weight
  inside `compute_thesis` (e.g. 60% sentiment, 40% events). Both
  remain visible in the components dict so the narrative can show
  them separately.
* **Pros.** Best of both — single sub-score for ranking, but the two
  inputs are independently inspectable. Easy to A/B the weight.
* **Cons.** Most code surface to wire up. Requires a small schema
  bump in the persisted Thesis JSON.

### Recommended: B + C

* **Patttern B for the strong-signal 8-K items.** A real restatement
  or trading suspension is not a "sentiment input" — it's a portfolio
  decision point. Surface as a flag.
* **Pattern C for everything else.** Earnings surprises, analyst
  actions, and the medium-signal 8-K items combine cleanly with the
  existing AV sentiment numeric. Wire them as a second component of
  the Thesis pillar with an explicit weight (start at 60/40 sentiment
  / events, tune later).
* **Don't do Pattern A.** It loses the structural information that
  makes event feeds valuable in the first place — a flat blend
  averages signal into noise.

### Concrete schema delta for Pattern C

The Thesis output dict gains:
```python
"components": {
    # ...existing sentiment fields...
    "events_score_raw": float,         # in [0, 100], same scale as sentiment_subscore_raw
    "events_breakdown": {
        "earnings_surprise": float,    # in [-1, +1] or None
        "analyst_actions": float,      # in [-1, +1] or None
        "sec_8k_events": float,        # in [-1, +1] or None
    },
    "events_weight": float,            # the weight used; default 0.4
},
"data_quality": {
    # ...existing fields...
    "thesis_break_flag": bool,         # Pattern B output
    "thesis_break_reason": str | None, # e.g. "5.04 — trading suspension"
}
```

Math:
```
events_score_raw = bounded_linear(
    mean_nonnull([earnings, analyst, sec_8k]),
    -1.0, +1.0,
)
sub_score = (1 - w) * sentiment_subscore + w * events_score_raw
```
where `w = events_weight` (default 0.4). If `thesis_break_flag` is
set, `sub_score` is forcibly floored at 25.0 *and* the flag surfaces
in the narrative.

### Where analyst actions plug into Institutional Confidence

The current Institutional Confidence pillar is 70% price momentum +
30% 13F change (with 13F stubbed in V1). The analyst target-price
consensus → implied-return signal is a natural fit for the gap that
the stubbed 13F was meant to fill. Two paths:

1. **Replace the 13F stub directly.** Use
   `analyst_actions.to_institutional_target_score` as the 30% component
   until 13F is wired (probably never, given the aggregation cost).
2. **Add it as a third component** alongside momentum and 13F.

Recommend path 1 — single replacement, no weight rebalancing needed.

---

## Section 6 — Ranked recommendation

Given Bryan's constraints (hobby project, prefers wiring existing
APIs over adding new ones, very protective of the V1 production
path) — ship in this order:

### Tier 1 — Ship in V2.1 (this quarter)

1. **SEC 8-K events (`sec_8k_events.py`).** Highest ROI. We already
   pull the `data.sec.gov/submissions` JSON for company facts —
   adding the 8-K filter is ~80 lines of code and gives us 100%
   universe coverage of a structurally clean signal. The item-code
   policy table does the heavy lifting; no per-filing text parsing
   needed for V2.1. Wire as Pattern B (Thesis-break flag) for the
   strong-signal items only, defer numeric scoring of the medium ones
   to V2.2.

### Tier 2 — Ship in V2.2 (next quarter)

2. **Earnings surprises via yfinance (`earnings.py`).** Yahoo is
   already a dependency. ~100 lines for `get_recent_surprises` +
   `to_thesis_event_score`. Wire as Pattern C events sub-component.
   Skip the Finnhub fallback in V2.2; add later if Yahoo coverage
   proves spotty.
3. **Analyst actions via yfinance (`analyst_actions.py`).** Same
   pattern, same dependency. Wire two things at once: (a) event score
   feeds Thesis (Pattern C); (b) target-price consensus feeds
   Institutional Confidence (replaces the 13F stub).

### Tier 3 — Consider for V3

4. **Finnhub recommendation trends.** Adds the *trend* dimension to
   analyst stance that yfinance lacks. Worth wiring if V2.2's analyst
   feed proves useful and we want directional sharpening.
5. **AV `EARNINGS_CALENDAR` bulk CSV.** Forward-looking — populates
   the dashboard "what reports this week" narrative without burning
   the NEWS_SENTIMENT quota. Trivial to add.

### Deferred indefinitely

* **Estimize / Earnings Whispers.** Paid; signal isn't worth it for
  this dashboard's use case.
* **TipRanks / Benzinga / MarketBeat.** Paid; redundant with yfinance.
* **8-K full-text parsing for guidance extraction.** Hard, brittle,
  and the manual-override narrative path is cheaper. Note as a
  long-running V4 research project if LTHCS ever turns into a real
  product.
* **Real-time 8-K streaming (e.g., SEC's RSS firehose).** Daily run
  cadence doesn't need it. Real-time only matters for trading.

### Acceptance criteria for "good enough" V2.1 ship

* `sec_8k_events.get_recent_8k_events("AAPL", lookback_days=180)` returns
  at least the most recent ~4 quarterly earnings 8-Ks + the K/A.
* `is_thesis_break(event)` returns True for any synthetic test fixture
  with item 4.02.
* Dashboard narrative for a ticker with a recent item 5.02 shows the
  filing date and notes "officer change".
* No change to V1 production output unless `events_score_raw` is
  explicitly enabled by a feature flag.

---

## Section 7 — Anti-patterns and gotchas

* **Don't auto-score 8-K item 1.01.** Half the time it's a routine
  vendor agreement; the other half it's a transformative customer
  win. Score-blind, narrative-surface. A regex over the body to
  detect "agreement with X" will get fooled.
* **Don't regex dollar amounts out of 8-K body text.** SEC filings
  are HTML-with-tables, the formatting is inconsistent across filers,
  and the cost of a single wrong extraction (showing the user a
  fabricated "$200M restructuring charge") is much higher than the
  value of having the dollar figure inline. Link to the filing
  instead.
* **Don't pay for Benzinga / MarketBeat / TipRanks / Estimize.** The
  marginal signal over yfinance + Finnhub free isn't worth the burn
  rate for a hobby project. Re-evaluate if LTHCS ever monetizes.
* **Don't double-count an earnings 8-K and an earnings surprise.**
  The 8-K item 2.02 *is* the earnings event. Use the 8-K as a date
  trigger and pull metrics from Yahoo / Finnhub once, not twice.
* **Don't score officer-appointments and officer-departures the same.**
  5.02 covers both; the LTHCS code path needs to look at the
  filing's text (or at least the title line / cover page) to know
  which it is. Cheap heuristic: word "Departure" in the filing title;
  AND word "Resign" or "Step Down" in the first ~500 chars.
* **Don't trust yfinance's `Surprise(%)` blindly for revenue.** It
  populates EPS reliably but revenue surprise comes from a different
  Yahoo endpoint that sometimes shows stale or missing estimates.
  Treat revenue surprise as optional in the event scoring.
* **Don't run earnings event scoring on a 24h cache.** A beat that
  printed at 4:00pm yesterday matters all day today; a 24h cache is
  fine. But a 24h cache means you'll *miss* an event that prints
  today at 4:00pm until tomorrow's run picks it up. Document this
  intentional latency rather than trying to engineer around it for
  V2.
* **Don't try to deduplicate analyst actions across firms.** Two
  upgrades from two firms on the same day are two separate signals.
  Aggregating them with the `to_thesis_event_score` clamp handles the
  "too many at once" case naturally.
* **Don't fetch the SEC `submissions` JSON more than once per day per
  ticker.** It's a ~few-hundred-KB file; over 75 tickers that's a lot
  of bandwidth even on the SEC's generous limit. Cache TTL 24h is the
  floor; 48h or 72h is fine if the wall-clock drift matters less than
  bandwidth.
* **Don't surface 5.07 shareholder vote results as a signal.** They're
  almost always routine annual-meeting outcomes. The very rare
  contested vote can be picked up by news sentiment if it matters.
* **Don't score `init` (initiation of coverage) the same as an
  upgrade.** A new sell-side shop picking up coverage isn't a
  re-rating event. Treat `init` as 0 in `to_thesis_event_score`
  unless it's at a top-tier firm with a Buy rating *and* an above-
  consensus target — at which point it's a +0.2 hint, not a +0.4
  upgrade.

---

## Section 8 — Reference URLs

Sources Bryan should bookmark for V2.x work:

* SEC EDGAR full-text search:
  <https://efts.sec.gov/LATEST/search-index?q=&forms=8-K>
* SEC EDGAR per-company filings (browse):
  <https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany>
* SEC EDGAR submissions JSON spec (the per-CIK feed LTHCS uses):
  <https://www.sec.gov/edgar/sec-api-documentation>
* SEC 8-K item code reference (form general instructions):
  <https://www.sec.gov/files/form8-k.pdf>
* yfinance docs (earnings, recommendations):
  <https://ranaroussi.github.io/yfinance/>
* Finnhub API docs (earnings calendar + recommendation trends):
  <https://finnhub.io/docs/api>
* Alpha Vantage `EARNINGS_CALENDAR` reference:
  <https://www.alphavantage.co/documentation/#earnings-calendar>
* NewsAPI docs (the keyword-filter fallback):
  <https://newsapi.org/docs>
* GNews docs (alternative free news search):
  <https://gnews.io/docs>

---

## Appendix — End-to-end example for one ticker

Walking through what V2.2 would produce for a hypothetical ticker
that has a fresh earnings beat, two recent upgrades, and a clean
8-K record:

```python
# Inputs collected by the daily pipeline:
surprises = earnings.get_recent_surprises("MSFT")        # 1 row, +12% EPS beat
actions   = analyst_actions.get_recent_actions("MSFT")   # 2 upgrades in 30d
events    = sec_8k_events.get_recent_8k_events("MSFT")   # 1 earnings 2.02, 1 8.01

# Scoring:
earnings_score   = earnings.to_thesis_event_score(surprises[0])     # +0.6
analyst_score    = analyst_actions.to_thesis_event_score(actions)   # +0.8 (two ups)
sec_8k_score     = sec_8k_events.to_thesis_event_score(events)      # 0.0 (no flag)

# Combine (mean of non-null):
events_raw = (earnings_score + analyst_score + sec_8k_score) / 3    # +0.467
events_subscore_0_100 = bounded_linear(events_raw, -1, +1)          # ~73.3

# Blend with AV sentiment subscore (say, 55.0 from a thin AV sample):
final_thesis = 0.6 * 55.0 + 0.4 * 73.3                              # ~62.3

# Thesis-break flag stays False — no item-4.02 / 5.04 / etc.
```

Compared to the V1 path (which would have produced ~52 from the thin
AV sample alone), the event-aware path lifts MSFT to ~62 — closer to
the underlying truth of a strong-quarter ticker with positive
analyst momentum. That delta is precisely the value the Thesis
pillar is missing today.

---

*End of document.*
