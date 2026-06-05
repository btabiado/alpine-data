"""Free, no-rate-limit AI/tech news source for LTHCS Thesis Integrity.

Complements the rate-limited Alpha Vantage NEWS_SENTIMENT source by
aggregating mentions of cohort tickers across three free, no-API-key
upstreams:

    * Hacker News via the Algolia search API
    * TechCrunch RSS
    * VentureBeat RSS

For each ticker we count mentions, gather engagement signals
(HN points + comments), and emit a Thesis-pillar-shaped dict that
``compute_thesis_from_stored_sentiment`` can consume the same way it
consumes Alpha Vantage output.

Sentiment is intentionally conservative in V1 — we do NOT assign a
direction unless engagement clearly suggests "in the news cycle".
Phase 2 will swap the engagement heuristic for an actual LLM scorer.

Module layout / external dependencies:
    * stdlib only (``xml.etree.ElementTree`` for RSS parsing)
    * ``requests`` (already in requirements.txt)
    * ``FileCache`` and ``TokenBucket`` from ``lthcs.sources``
"""

from __future__ import annotations

import datetime as _dt
import re
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple, Union

import requests

from lthcs.sources._cache import FileCache
from lthcs.sources._ratelimit import TokenBucket

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HN_URL = "https://hn.algolia.com/api/v1/search"
_TECHCRUNCH_URL = "https://techcrunch.com/feed/"
_VENTUREBEAT_URL = "https://venturebeat.com/feed/"

# Cache TTLs (seconds).
_HN_CACHE_TTL = 6 * 60 * 60       # 6h — HN search per ticker query
_RSS_CACHE_TTL = 60 * 60          # 1h — RSS feed payloads (shared across tickers)

# Polite User-Agent so RSS hosts don't 403 us on default python-requests UA.
_DEFAULT_HEADERS = {
    "User-Agent": (
        "LTHCS-Dashboard/1.0 (+https://github.com/bryantabiadon/alpine-data) "
        "Python-requests"
    ),
    "Accept": "application/json, application/rss+xml, application/xml;q=0.9, */*;q=0.5",
}

# The five labels Alpha Vantage uses on ``ticker_sentiment_label``. Kept here
# so callers can mix our output with AV output without a separate mapping.
_SENTIMENT_LABELS: Tuple[str, ...] = (
    "Bearish",
    "Somewhat-Bearish",
    "Neutral",
    "Somewhat-Bullish",
    "Bullish",
)

# Engagement thresholds for the V1 heuristic.
_ENGAGEMENT_MIN_MENTIONS = 3
_ENGAGEMENT_POINTS_THRESHOLD = 50.0
_ENGAGEMENT_COMMENTS_THRESHOLD = 30.0

# Mention-count multiplier tiers for the 3+ mention path. Allows
# very-frequently-mentioned names (think NVDA with 20+ AI mentions in a
# week) to cross above the +0.60 ceiling. Capped below so AI-news
# engagement never claims the top of the confidence band — that's
# reserved for real sentiment sources (Finnhub, Alpha Vantage).
_MENTION_MULTIPLIER_TIERS: Tuple[Tuple[int, float], ...] = (
    # (min_mentions_inclusive, multiplier)
    (21, 1.3),
    (11, 1.2),
    (6,  1.1),
    (3,  1.0),
)
_SENTIMENT_CAP = 0.75

# Base sentiment values for the 3+ mention engagement tiers.
_BASE_SENTIMENT_LOW_ENGAGEMENT = 0.35
_BASE_SENTIMENT_HIGH_ENGAGEMENT = 0.60
# Floor signal for the 1-2 mention path (no multiplier applied).
_SENTIMENT_WEAK = 0.15

# Ticker -> search keywords. Limited to the AI cohort + mega-caps; tickers
# outside this map don't have enough AI-news coverage to be worth a search.
TICKER_KEYWORDS: Dict[str, List[str]] = {
    "NVDA": ["NVIDIA", "Nvidia"],
    "AVGO": ["Broadcom"],
    "AMD":  ["AMD", "Advanced Micro"],
    "MU":   ["Micron", "HBM"],
    "MSFT": ["Microsoft", "Azure", "Copilot"],
    "GOOG": ["Google", "Alphabet", "Gemini"],
    "GOOGL": ["Google", "Alphabet", "Gemini"],
    "META": ["Meta", "Facebook", "Llama AI"],
    "AAPL": ["Apple", "iPhone"],
    "AMZN": ["Amazon", "AWS", "Bedrock"],
    "ORCL": ["Oracle", "OCI"],
    "CRM":  ["Salesforce", "Agentforce"],
    "TSLA": ["Tesla", "Cybertruck"],
    "INTC": ["Intel"],
    "ARM":  ["Arm Holdings", "ARM chip"],
    "TSM":  ["TSMC", "Taiwan Semi"],
    "ASML": ["ASML"],
    "PLTR": ["Palantir"],
    "SMCI": ["Super Micro"],
    "DDOG": ["Datadog"],
    "MDB":  ["MongoDB"],
    "CRWD": ["CrowdStrike"],
    "PANW": ["Palo Alto Networks"],
    "FTNT": ["Fortinet"],
    "ZS":   ["Zscaler"],
    "NET":  ["Cloudflare"],
    "OKTA": ["Okta"],
    "SNOW": ["Snowflake"],
    "MELI": ["MercadoLibre"],
    "BABA": ["Alibaba"],
    "JD":   ["JD.com"],
    "BIDU": ["Baidu"],
    "NFLX": ["Netflix"],
    "ADBE": ["Adobe"],
    "INTU": ["Intuit", "TurboTax"],
    "WDAY": ["Workday"],
    "NOW":  ["ServiceNow"],
    "IBM":  ["IBM", "Watson"],
}


# ---------------------------------------------------------------------------
# Module-level singletons (mirrors alpha_vantage.py pattern).
# ---------------------------------------------------------------------------

_HN_CACHE = FileCache("hn_news")
_RSS_CACHE = FileCache("ai_rss")

# HN: ~5 req/sec is plenty for Algolia (very generous public limit).
_HN_BUCKET = TokenBucket(capacity=10, refill_rate=5.0)
# RSS: 1 request every 30 min per feed (1/60 tokens-per-second, capacity 2).
_RSS_BUCKET = TokenBucket(capacity=2, refill_rate=1.0 / 60.0)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AINewsError(RuntimeError):
    """Raised when an AI news upstream returns a non-200 or invalid body."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _today_iso() -> str:
    return _dt.date.today().isoformat()


def _epoch_cutoff(days: int) -> int:
    return int(time.time()) - max(int(days), 0) * 86400


def _to_iso_date(raw: Any) -> Optional[str]:
    """Best-effort ISO YYYY-MM-DD extraction from common date formats.

    Accepts an int/float epoch, an HN ``created_at`` ISO timestamp string,
    or an RFC-822 RSS ``pubDate``. Returns ``None`` if nothing parses.
    """
    if raw is None:
        return None
    # Epoch seconds.
    if isinstance(raw, (int, float)):
        try:
            return _dt.datetime.fromtimestamp(
                int(raw), tz=_dt.timezone.utc
            ).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    # HN-style: 2026-05-15T12:34:56.000Z (or .000000Z)
    try:
        # fromisoformat in 3.9 doesn't accept the trailing Z.
        cleaned = s.replace("Z", "+00:00")
        return _dt.datetime.fromisoformat(cleaned).date().isoformat()
    except ValueError:
        pass
    # RFC 822 (RSS pubDate): "Sat, 16 May 2026 09:00:00 +0000"
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%a, %d %b %Y %H:%M:%S",
    ):
        try:
            return _dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    # A plain YYYY-MM-DD.
    try:
        return _dt.date.fromisoformat(s[:10]).isoformat()
    except ValueError:
        return None


def _safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return default


def _strip_html(s: str) -> str:
    """Very lightweight HTML/entity strip — RSS ``description`` often
    contains a CDATA HTML snippet. Good enough for keyword matching."""
    if not s:
        return ""
    # Drop tags then collapse whitespace.
    no_tags = re.sub(r"<[^>]+>", " ", s)
    # Decode a couple of common entities; we don't need full HTML parsing.
    no_tags = (
        no_tags.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&apos;", "'")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )
    return re.sub(r"\s+", " ", no_tags).strip()


def _haystack(item: Dict[str, Any]) -> str:
    """Build a lowercase haystack from an RSS or HN item for keyword matching."""
    title = str(item.get("title") or "")
    summary = str(item.get("summary") or "")
    return (title + " " + summary).lower()


def _matches_any_keyword(item: Dict[str, Any], keywords: List[str]) -> bool:
    if not keywords:
        return False
    hay = _haystack(item)
    for kw in keywords:
        kw_lower = kw.lower().strip()
        if not kw_lower:
            continue
        if kw_lower in hay:
            return True
    return False


# ---------------------------------------------------------------------------
# Hacker News (Algolia)
# ---------------------------------------------------------------------------


def _hn_cache_key(query: str, days: int) -> str:
    return f"hn_search/{query.lower().strip()}/{int(days)}"


def _parse_hn_hit(hit: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalize one Algolia hit to our flat shape. Returns None on bad input."""
    if not isinstance(hit, dict):
        return None
    title = str(hit.get("title") or "").strip()
    if not title:
        return None
    url = (hit.get("url") or "").strip()
    if not url:
        obj_id = hit.get("objectID")
        if obj_id:
            url = f"https://news.ycombinator.com/item?id={obj_id}"
    points = _safe_int(hit.get("points"))
    num_comments = _safe_int(hit.get("num_comments"))
    # Prefer the integer epoch when present; fall back to the string field.
    time_published = _to_iso_date(hit.get("created_at_i")) or _to_iso_date(
        hit.get("created_at")
    )
    return {
        "title": title,
        "url": url,
        "points": points,
        "num_comments": num_comments,
        "time_published": time_published,
        "source": "HN",
    }


def fetch_hn_mentions(query: str, days: int = 30) -> List[Dict[str, Any]]:
    """Query Hacker News for stories matching ``query`` over the last ``days``.

    Returns a list of dicts with shape::

        {"title", "url", "points", "num_comments", "time_published", "source"}

    Cached for 6h per (query, days). Rate-limited via a polite token bucket.
    Returns ``[]`` on upstream failure rather than raising — this is a
    "nice to have" signal and a failed fetch should not bubble up to the
    Thesis pillar.
    """
    if not query or not query.strip():
        return []

    key = _hn_cache_key(query, days)
    hit = _HN_CACHE.get(key)
    if hit is not None:
        # Defensive: ensure we hand callers a fresh list of dicts.
        return [dict(item) for item in (hit.value or [])]

    # Polite rate limit. If the bucket is exhausted we skip the call rather
    # than block the daily pipeline; the caller falls back to neutral.
    if not _HN_BUCKET.try_acquire():
        return []

    params: Dict[str, Union[str, int]] = {
        "query": query,
        "tags": "story",
        "numericFilters": f"created_at_i>{_epoch_cutoff(days)}",
        "hitsPerPage": 50,
    }
    try:
        resp = requests.get(
            _HN_URL, params=params, headers=_DEFAULT_HEADERS, timeout=20
        )
    except requests.RequestException:
        return []
    if getattr(resp, "status_code", 0) != 200:
        return []
    try:
        payload = resp.json()
    except ValueError:
        return []

    hits = []
    if isinstance(payload, dict):
        hits = payload.get("hits") or []

    parsed: List[Dict[str, Any]] = []
    for h in hits:
        item = _parse_hn_hit(h)
        if item is not None:
            parsed.append(item)

    _HN_CACHE.set(key, parsed, ttl_seconds=_HN_CACHE_TTL)
    return parsed


# ---------------------------------------------------------------------------
# RSS feeds (TechCrunch, VentureBeat)
# ---------------------------------------------------------------------------


def _parse_rss_xml(xml_text: str, source_label: str) -> List[Dict[str, Any]]:
    """Parse a standard RSS 2.0 channel/item XML doc into our flat shape.

    Defensive: any parse error returns ``[]``. Items missing a title are
    dropped — we can't keyword-match them.
    """
    if not xml_text or not xml_text.strip():
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out: List[Dict[str, Any]] = []
    # RSS 2.0: rss > channel > item. Be lenient: accept ``item`` anywhere.
    for item in root.iter("item"):
        title_el = item.find("title")
        title = (title_el.text or "").strip() if title_el is not None and title_el.text else ""
        if not title:
            continue
        link_el = item.find("link")
        url = (link_el.text or "").strip() if link_el is not None and link_el.text else ""
        # ``description`` is the usual RSS summary field. Real feeds wrap
        # it in CDATA so it parses as flat text, but some test fixtures (and
        # the occasional malformed feed) embed raw HTML tags as child
        # elements — ``itertext()`` flattens both shapes.
        desc_el = item.find("description")
        if desc_el is not None:
            raw_desc = "".join(desc_el.itertext())
            summary = _strip_html(raw_desc)
        else:
            summary = ""
        pub_el = item.find("pubDate")
        time_published = (
            _to_iso_date(pub_el.text) if pub_el is not None and pub_el.text else None
        )
        out.append(
            {
                "title": title,
                "url": url,
                "summary": summary,
                "time_published": time_published,
                "source": source_label,
            }
        )
    return out


def _fetch_rss(url: str, cache_key: str, source_label: str) -> List[Dict[str, Any]]:
    """Shared RSS fetch+parse+cache path."""
    hit = _RSS_CACHE.get(cache_key)
    if hit is not None:
        return [dict(item) for item in (hit.value or [])]

    if not _RSS_BUCKET.try_acquire():
        return []

    try:
        resp = requests.get(url, headers=_DEFAULT_HEADERS, timeout=20)
    except requests.RequestException:
        return []
    if getattr(resp, "status_code", 0) != 200:
        return []
    text = getattr(resp, "text", "") or ""
    parsed = _parse_rss_xml(text, source_label)
    _RSS_CACHE.set(cache_key, parsed, ttl_seconds=_RSS_CACHE_TTL)
    return parsed


def fetch_techcrunch_feed() -> List[Dict[str, Any]]:
    """Pull the TechCrunch RSS feed and return a flat list of items.

    Each item: ``{title, url, summary, time_published, source}``.
    Cached for 1h, shared across all tickers in a run.
    """
    return _fetch_rss(_TECHCRUNCH_URL, "rss/techcrunch", "TechCrunch")


def fetch_venturebeat_feed() -> List[Dict[str, Any]]:
    """Pull the VentureBeat RSS feed. Same shape as TechCrunch."""
    return _fetch_rss(_VENTUREBEAT_URL, "rss/venturebeat", "VentureBeat")


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _empty_aggregate(ticker: str) -> Dict[str, Any]:
    return {
        "ticker": ticker.upper(),
        "hn_mention_count": 0,
        "hn_total_points": 0,
        "hn_total_comments": 0,
        "rss_mention_count": 0,
        "total_mentions": 0,
        "sample_titles": [],
        "first_seen": None,
        "last_seen": None,
    }


def _pick_sample_titles(
    hn_items: List[Dict[str, Any]],
    rss_items: List[Dict[str, Any]],
    limit: int = 3,
) -> List[str]:
    """Top titles by HN points (desc), then by recency for RSS-only items."""
    ranked_hn = sorted(
        hn_items,
        key=lambda it: (
            _safe_int(it.get("points")),
            it.get("time_published") or "",
        ),
        reverse=True,
    )
    ranked_rss = sorted(
        rss_items,
        key=lambda it: (it.get("time_published") or ""),
        reverse=True,
    )
    titles: List[str] = []
    seen: set = set()
    for it in ranked_hn + ranked_rss:
        t = (it.get("title") or "").strip()
        if not t or t in seen:
            continue
        seen.add(t)
        titles.append(t)
        if len(titles) >= limit:
            break
    return titles


def _date_bounds(items: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    """Min/max time_published over a list of items (None if all missing)."""
    dates = [str(it.get("time_published")) for it in items if it.get("time_published")]
    if not dates:
        return None, None
    return min(dates), max(dates)


def aggregate_ai_news(
    tickers: List[str], days: int = 30
) -> Dict[str, Dict[str, Any]]:
    """Per-ticker aggregate of HN + TechCrunch + VentureBeat mentions.

    Per-call cost (worst case, no cache):
        * 1 HN query per ticker     (6h TTL)
        * 1 TechCrunch feed pull    (1h TTL, shared)
        * 1 VentureBeat feed pull   (1h TTL, shared)

    Tickers not in ``TICKER_KEYWORDS`` are returned as an empty aggregate
    (the Thesis pipeline can use that as the signal to fall back to AV).
    """
    if not tickers:
        return {}

    # Pull the two RSS feeds once — they're shared across all tickers.
    tc_items = fetch_techcrunch_feed()
    vb_items = fetch_venturebeat_feed()
    rss_pool = tc_items + vb_items

    out: Dict[str, Dict[str, Any]] = {}
    for raw_t in tickers:
        ticker = str(raw_t).upper()
        keywords = TICKER_KEYWORDS.get(ticker)
        if not keywords:
            out[ticker] = _empty_aggregate(ticker)
            continue

        # HN search: query each keyword, dedupe by URL+title.
        hn_items: List[Dict[str, Any]] = []
        hn_seen: set = set()
        for kw in keywords:
            for it in fetch_hn_mentions(kw, days=days):
                key = (it.get("url"), it.get("title"))
                if key in hn_seen:
                    continue
                hn_seen.add(key)
                hn_items.append(it)

        # RSS: filter the shared pool by keywords (no extra HTTP).
        rss_items = [it for it in rss_pool if _matches_any_keyword(it, keywords)]

        hn_count = len(hn_items)
        rss_count = len(rss_items)
        hn_points = sum(_safe_int(it.get("points")) for it in hn_items)
        hn_comments = sum(_safe_int(it.get("num_comments")) for it in hn_items)

        first_seen, last_seen = _date_bounds(hn_items + rss_items)

        out[ticker] = {
            "ticker": ticker,
            "hn_mention_count": hn_count,
            "hn_total_points": hn_points,
            "hn_total_comments": hn_comments,
            "rss_mention_count": rss_count,
            "total_mentions": hn_count + rss_count,
            "sample_titles": _pick_sample_titles(hn_items, rss_items),
            "first_seen": first_seen,
            "last_seen": last_seen,
        }
    return out


# ---------------------------------------------------------------------------
# Thesis-pillar adapter
# ---------------------------------------------------------------------------


def _empty_label_counts() -> Dict[str, int]:
    return {label: 0 for label in _SENTIMENT_LABELS}


def _mention_count_multiplier(total_mentions: int) -> float:
    """Logarithmic-ish multiplier on the base engagement sentiment.

    Lets very-frequently-mentioned names (NVDA with 20+ AI mentions in
    a week) edge above the +0.60 ceiling without changing behavior for
    moderately-mentioned names. The tiers are deliberately coarse — we
    don't want a 6th mention to dramatically swing the signal.

    Tier table::

         3-5  mentions -> 1.0x  (no change)
         6-10 mentions -> 1.1x
        11-20 mentions -> 1.2x
        21+  mentions  -> 1.3x
    """
    for min_mentions, mult in _MENTION_MULTIPLIER_TIERS:
        if total_mentions >= min_mentions:
            return mult
    return 1.0


def _engagement_sentiment_detail(
    total_mentions: int, hn_total_points: int, hn_total_comments: int
) -> Dict[str, Any]:
    """V1 sentiment heuristic based on aggregate news engagement.

    Returns a diagnostic dict with the math broken out, plus the final
    ``sentiment`` value (or ``None`` for no-signal). Keeping the
    intermediate fields in the return value lets ``compute_thesis_signal_from_news``
    surface them in ``variable_detail`` for downstream transparency.

    Returned ``sentiment`` values:
        ``None``  — no mentions at all; Thesis treats as no signal.
        ``+0.15`` — 1-2 mentions; weak engaged-but-niche signal.
        ``base * multiplier`` (capped at ``_SENTIMENT_CAP``) — for 3+
        mentions, where ``base`` is +0.35 (low engagement) or +0.60
        (high engagement) and ``multiplier`` is the mention-count tier
        from ``_mention_count_multiplier``.

    Tuning history:
    - V1 (initial): uniform +0.2 / 0.0 scheme. Caused regression: for
      top-pillar tickers where other pillars score 80+, writing a +0.2
      sentiment (subscore 60) is LOWER than the composite-renorm path
      that redistributes thesis weight to strong pillars (effective
      subscore ~80).
    - V2 (bumped 2026-05-17 same-day): +0.45 high-engagement (subscore
      ~72). Better, but still under renorm baseline for top names.
    - V3 (2026-05-17): +0.60 high-engagement (subscore ~80). Matched
      the renorm baseline for the strongest names so AI news is
      strictly an upgrade.
    - V4 (current, 2026-05-17): mention-count multiplier on top of the
      engagement tier so very-frequently-mentioned names (NVDA with
      20+ AI mentions/week) can cross the +0.60 ceiling. Capped at
      +0.75 (subscore ~88) — engagement-as-proxy never claims the top
      of the band; that's reserved for real sentiment scorers.
    """
    detail: Dict[str, Any] = {
        "mention_count": int(total_mentions),
        "engagement_tier": None,
        "base_sentiment": None,
        "multiplier": None,
        "final_sentiment": None,
        "capped": False,
    }
    if total_mentions <= 0:
        return detail
    if total_mentions < _ENGAGEMENT_MIN_MENTIONS:
        detail["engagement_tier"] = "weak"
        detail["base_sentiment"] = _SENTIMENT_WEAK
        detail["multiplier"] = 1.0
        detail["final_sentiment"] = _SENTIMENT_WEAK
        return detail
    # Use averages so a single viral post doesn't dominate.
    avg_points = hn_total_points / max(total_mentions, 1)
    avg_comments = hn_total_comments / max(total_mentions, 1)
    high_engagement = (
        avg_points >= _ENGAGEMENT_POINTS_THRESHOLD
        or avg_comments >= _ENGAGEMENT_COMMENTS_THRESHOLD
    )
    base = (
        _BASE_SENTIMENT_HIGH_ENGAGEMENT
        if high_engagement
        else _BASE_SENTIMENT_LOW_ENGAGEMENT
    )
    multiplier = _mention_count_multiplier(total_mentions)
    raw = base * multiplier
    capped = raw > _SENTIMENT_CAP
    final = min(raw, _SENTIMENT_CAP)
    # Round to 4 dp so a 0.60 * 1.1 = 0.66 doesn't surface as 0.66000000000001.
    final = round(final, 4)
    detail["engagement_tier"] = "high" if high_engagement else "low"
    detail["base_sentiment"] = base
    detail["multiplier"] = multiplier
    detail["final_sentiment"] = final
    detail["capped"] = capped
    return detail


def _engagement_sentiment(
    total_mentions: int, hn_total_points: int, hn_total_comments: int
) -> Optional[float]:
    """Back-compat thin wrapper returning just the final sentiment value.

    Existing callers (and tests) read only the scalar score; the new
    diagnostic fields are surfaced through ``_engagement_sentiment_detail``.
    """
    return _engagement_sentiment_detail(
        total_mentions, hn_total_points, hn_total_comments
    )["final_sentiment"]


def compute_thesis_signal_from_news(news_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Convert an ``aggregate_ai_news`` entry into a Thesis-pillar payload.

    The output shape matches what ``parse_ticker_sentiment`` returns from
    Alpha Vantage so downstream
    ``compute_thesis_from_stored_sentiment`` doesn't need a branch.
    """
    ticker = str((news_dict or {}).get("ticker") or "").upper()
    total_mentions = _safe_int((news_dict or {}).get("total_mentions"))
    hn_points = _safe_int((news_dict or {}).get("hn_total_points"))
    hn_comments = _safe_int((news_dict or {}).get("hn_total_comments"))

    label_counts = _empty_label_counts()
    if total_mentions > 0:
        # V1: don't fabricate a direction — record all mentions as neutral.
        # Phase 2 will replace this with an LLM-scored breakdown.
        label_counts["Neutral"] = total_mentions

    detail = _engagement_sentiment_detail(total_mentions, hn_points, hn_comments)
    sentiment_score = detail["final_sentiment"]
    # Relevance score is meaningful only when we have any signal at all.
    # Use a flat 0.5 ("matched a keyword") so downstream consumers can
    # weight us below AV's per-article relevance numbers.
    relevance_score: Optional[float] = 0.5 if total_mentions > 0 else None

    return {
        "ticker": ticker,
        "article_count": total_mentions,
        "mean_sentiment_score": sentiment_score,
        "mean_relevance_score": relevance_score,
        "label_counts": label_counts,
        "source": "ai_news_aggregate",
        "last_scored": _today_iso(),
        # Additive diagnostic fields for variable_detail transparency.
        # Downstream consumers reading ``mean_sentiment_score`` are
        # unchanged; UIs/inspectors that want to explain the score can
        # render the engagement breakdown from these keys.
        "engagement_tier": detail["engagement_tier"],
        "base_sentiment": detail["base_sentiment"],
        "mention_multiplier": detail["multiplier"],
        "sentiment_capped": detail["capped"],
    }


__all__ = [
    "AINewsError",
    "TICKER_KEYWORDS",
    "aggregate_ai_news",
    "compute_thesis_signal_from_news",
    "fetch_hn_mentions",
    "fetch_techcrunch_feed",
    "fetch_venturebeat_feed",
]
