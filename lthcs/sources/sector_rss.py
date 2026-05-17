"""Sector-specific RSS news source for LTHCS Thesis Integrity.

Pulls three high-ROI free public RSS feeds, none of which require an API
key, to complement the AI-tech cohort coverage already provided by
``ai_news.py``:

    * FDA Press Announcements + Drug Approvals  (pharma / medtech)
    * EIA "Today in Energy"                      (energy)
    * Federal Reserve press releases             (financials)

For each ticker in our pharma / energy / financials lists we scan the
freshly-fetched feed items for a keyword substring match in title or
summary. Matched items become a per-ticker event aggregate that maps
into a Thesis-pillar payload with the same shape as
``ai_news.compute_thesis_signal_from_news``.

Conventions mirror ``ai_news.py``:

    * stdlib RSS parsing (``xml.etree.ElementTree``) — no ``feedparser``
    * shared ``FileCache`` + ``TokenBucket`` from ``lthcs.sources``
    * any upstream error returns ``[]`` rather than raising; this is a
      "nice to have" Thesis pillar and a failed fetch must not bubble
      up into the daily pipeline
    * sentiment is intentionally conservative — regulatory/macro news
      mentions are treated as mild positive engagement, no direction
      inference
"""

from __future__ import annotations

import datetime as _dt
import email.utils as _eut
import html
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

import requests

from lthcs.sources._cache import FileCache
from lthcs.sources._ratelimit import TokenBucket

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FDA_PRESS_RSS = (
    "https://www.fda.gov/about-fda/contact-fda/stay-informed/"
    "rss-feeds/press-releases/rss.xml"
)
FDA_DRUG_APPROVALS_RSS = (
    "https://www.fda.gov/about-fda/contact-fda/stay-informed/"
    "rss-feeds/drug-approvals/rss.xml"
)
EIA_TODAY_IN_ENERGY_RSS = "https://www.eia.gov/rss/todayinenergy.xml"
FED_PRESS_RELEASES_RSS = "https://www.federalreserve.gov/feeds/press_all.xml"

# 1h shared TTL — these feeds don't update often, and the daily pipeline
# only fetches them once per run anyway.
_RSS_CACHE_TTL = 60 * 60

# The five labels Alpha Vantage uses on ``ticker_sentiment_label``. Kept
# here so callers can mix our output with AV / ai_news output without a
# separate mapping.
_SENTIMENT_LABELS: Tuple[str, ...] = (
    "Bearish",
    "Somewhat-Bearish",
    "Neutral",
    "Somewhat-Bullish",
    "Bullish",
)

# Polite UA — federal sites in particular tend to 403 default urllib/requests UAs.
_DEFAULT_HEADERS = {
    "User-Agent": (
        "LTHCS-Dashboard/1.0 (+https://github.com/bryantabiadon/btc-eth-etf-dashboard) "
        "Python-requests"
    ),
    "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.5",
}

# Engagement bands for the V1 thesis-signal heuristic.
_EVENT_LOW_BAND_MAX = 2     # 1-2 events => mildly positive
_EVENT_LOW_SENTIMENT = 0.2
_EVENT_HIGH_SENTIMENT = 0.5

# ---------------------------------------------------------------------------
# Ticker -> keyword maps
# ---------------------------------------------------------------------------

PHARMA_TICKER_KEYWORDS: Dict[str, List[str]] = {
    "LLY":  ["Eli Lilly", "Lilly", "tirzepatide", "Zepbound", "Mounjaro"],
    "MRK":  ["Merck", "Keytruda"],
    "ABBV": ["AbbVie", "Humira", "Skyrizi", "Rinvoq"],
    "JNJ":  ["Johnson & Johnson", "Janssen", "Stelara"],
    "PFE":  ["Pfizer", "Paxlovid", "Comirnaty"],
    "AMGN": ["Amgen", "Tezspire", "Repatha", "Otezla"],
    "VRTX": ["Vertex Pharma", "Casgevy", "Trikafta"],
    "REGN": ["Regeneron", "Eylea", "Dupixent"],
    "GILD": ["Gilead", "Trodelvy", "Veklury"],
    "BMY":  ["Bristol-Myers", "Bristol Myers", "Eliquis", "Opdivo"],
    "BIIB": ["Biogen", "Leqembi", "Aduhelm"],
    "ISRG": ["Intuitive Surgical", "da Vinci"],
    "BSX":  ["Boston Scientific"],
    "TMO":  ["Thermo Fisher"],
    "MDT":  ["Medtronic"],
}

ENERGY_TICKER_KEYWORDS: Dict[str, List[str]] = {
    "XOM":  ["Exxon", "ExxonMobil"],
    "CVX":  ["Chevron"],
    "COP":  ["ConocoPhillips"],
    "EOG":  ["EOG Resources"],
    "SLB":  ["Schlumberger", "SLB"],
    "PSX":  ["Phillips 66"],
    "MPC":  ["Marathon Petroleum"],
    "VLO":  ["Valero"],
    "OXY":  ["Occidental Petroleum"],
    "HES":  ["Hess Corp"],
    # EIA Today-in-Energy posts often discuss the sector broadly without
    # naming any single company; ``is_sector_relevant`` widens the net via
    # sector-level keywords below.
}

FINANCIALS_TICKER_KEYWORDS: Dict[str, List[str]] = {
    "JPM":  ["JPMorgan", "JP Morgan"],
    "BAC":  ["Bank of America", "BofA"],
    "WFC":  ["Wells Fargo"],
    "C":    ["Citigroup", "Citibank"],
    "GS":   ["Goldman Sachs"],
    "MS":   ["Morgan Stanley"],
    "USB":  ["U.S. Bancorp"],
    "COF":  ["Capital One"],
    "AXP":  ["American Express"],
    "BLK":  ["BlackRock"],
    "SCHW": ["Charles Schwab"],
    # Many Fed press releases mention only "large banks" / "stress test" /
    # "supervisory" without a specific name — ``is_sector_relevant``
    # promotes those for ALL banks-and-broker tickers above.
}

# Sector-level keywords. When a feed item mentions one of these but no
# specific ticker, ``aggregate_sector_events`` still attaches it to every
# ticker in that sector — but at a clearly lower relevance (caller can
# choose to weight separately by inspecting ``sectors_matched``).
_PHARMA_SECTOR_KEYWORDS = (
    "drug approval", "drug approvals", "biosimilar", "FDA approves",
    "fast track", "breakthrough therapy", "warning letter",
)
_ENERGY_SECTOR_KEYWORDS = (
    "crude oil", "petroleum", "natural gas", "gasoline",
    "OPEC", "refinery", "shale", "WTI", "Brent",
)
_FINANCIALS_SECTOR_KEYWORDS = (
    "stress test", "supervisory", "Basel", "capital requirement",
    "SLR", "discount window", "FOMC", "federal funds rate",
    "large bank", "large banks", "banking organizations",
)

# ---------------------------------------------------------------------------
# Module-level singletons (mirrors ai_news.py pattern)
# ---------------------------------------------------------------------------

_RSS_CACHE = FileCache("sector_rss")
# ~1 req per 10 minutes per feed; capacity 3 lets the pipeline pull
# (FDA press, FDA approvals, EIA, Fed) in one burst without blocking.
_RSS_BUCKET = TokenBucket(capacity=3, refill_rate=1.0 / 600.0)

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SectorRssError(RuntimeError):
    """Raised internally when a sector-RSS upstream returns an unusable body.

    Currently unused — every public function catches and degrades to ``[]``.
    Exists so callers can ``except`` it once we add stricter modes.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _today_iso() -> str:
    return _dt.date.today().isoformat()


def _to_iso_date(raw: Any) -> Optional[str]:
    """Best-effort ISO YYYY-MM-DD extraction.

    RSS ``pubDate`` is RFC 822, e.g. ``"Mon, 12 May 2026 10:30:00 GMT"``.
    Fall back to a couple of common ISO shapes so synthetic test fixtures
    can use whatever's most convenient.
    """
    if raw is None:
        return None
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

    # Primary path: RFC 822 via stdlib email.utils.
    try:
        dt = _eut.parsedate_to_datetime(s)
        if dt is not None:
            return dt.date().isoformat()
    except (TypeError, ValueError, IndexError):
        pass

    # ISO with trailing Z (3.9 doesn't accept Z directly).
    try:
        cleaned = s.replace("Z", "+00:00")
        return _dt.datetime.fromisoformat(cleaned).date().isoformat()
    except ValueError:
        pass

    # Plain YYYY-MM-DD.
    try:
        return _dt.date.fromisoformat(s[:10]).isoformat()
    except ValueError:
        return None


def _strip_html(s: str) -> str:
    """Drop tags, decode entities, collapse whitespace.

    RSS ``description`` blocks are often HTML inside CDATA. We don't need
    a real HTML parser — keyword matching only cares about plain text.
    """
    if not s:
        return ""
    no_tags = re.sub(r"<[^>]+>", " ", s)
    decoded = html.unescape(no_tags)
    return re.sub(r"\s+", " ", decoded).strip()


def _haystack(item: Dict[str, Any]) -> str:
    """Lowercase concat of title + summary for keyword matching."""
    title = str(item.get("title") or "")
    summary = str(item.get("summary") or "")
    return (title + " " + summary).lower()


def _matches_any_keyword(item: Dict[str, Any], keywords: List[str]) -> bool:
    """Case-insensitive substring match against title/summary."""
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


def _matches_any_sector_keyword(
    item: Dict[str, Any], sector_keywords: Tuple[str, ...]
) -> bool:
    if not sector_keywords:
        return False
    hay = _haystack(item)
    for kw in sector_keywords:
        if kw.lower() in hay:
            return True
    return False


def is_sector_relevant(item: Dict[str, Any], sector: str) -> bool:
    """Does this item plausibly belong to ``sector``?

    Used by ``aggregate_sector_events`` to widen the net beyond ticker-name
    matches — many regulatory releases never name a specific company.
    Exposed as part of the public API for callers who want to filter
    feeds themselves without going through the aggregate path.
    """
    sector = (sector or "").lower()
    if sector == "pharma":
        return _matches_any_sector_keyword(item, _PHARMA_SECTOR_KEYWORDS)
    if sector == "energy":
        return _matches_any_sector_keyword(item, _ENERGY_SECTOR_KEYWORDS)
    if sector == "financials":
        return _matches_any_sector_keyword(item, _FINANCIALS_SECTOR_KEYWORDS)
    return False


def _within_age_window(item: Dict[str, Any], cutoff_iso: Optional[str]) -> bool:
    """Filter items older than the cutoff. Items missing a date are kept."""
    if not cutoff_iso:
        return True
    published = item.get("published_at")
    if not published:
        return True
    try:
        return str(published) >= cutoff_iso
    except TypeError:
        return True


def _age_cutoff_iso(max_age_days: int) -> Optional[str]:
    """ISO date string ``max_age_days`` before today, or None for no cutoff."""
    if max_age_days is None or int(max_age_days) <= 0:
        return None
    cutoff = _dt.date.today() - _dt.timedelta(days=int(max_age_days))
    return cutoff.isoformat()


# ---------------------------------------------------------------------------
# RSS parsing
# ---------------------------------------------------------------------------


def _parse_rss_xml(
    xml_text: str, *, source: str, feed: str
) -> List[Dict[str, Any]]:
    """Parse a standard RSS 2.0 channel/item doc.

    Returns a list of normalised dicts shaped like::

        {
          "title": str,
          "summary": str,
          "url": str,
          "published_at": str | None,   # ISO YYYY-MM-DD
          "source": str,
          "feed": str,
        }

    Defensive: any parse error returns ``[]``. Items missing a title are
    dropped — without a title we have nothing to keyword-match against.
    """
    if not xml_text or not xml_text.strip():
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    out: List[Dict[str, Any]] = []
    for item in root.iter("item"):
        title_el = item.find("title")
        if title_el is not None and title_el.text:
            raw_title = title_el.text
        else:
            raw_title = ""
        title = html.unescape(raw_title or "").strip()
        if not title:
            continue

        link_el = item.find("link")
        if link_el is not None and link_el.text:
            url = link_el.text.strip()
        else:
            url = ""

        desc_el = item.find("description")
        if desc_el is not None:
            raw_desc = "".join(desc_el.itertext())
            summary = _strip_html(raw_desc)
        else:
            summary = ""

        pub_el = item.find("pubDate")
        if pub_el is not None and pub_el.text:
            published_at = _to_iso_date(pub_el.text)
        else:
            published_at = None

        out.append(
            {
                "title": title,
                "summary": summary,
                "url": url,
                "published_at": published_at,
                "source": source,
                "feed": feed,
            }
        )
    return out


def _fetch_rss(
    url: str, *, cache_key: str, source: str, feed: str
) -> List[Dict[str, Any]]:
    """Shared fetch -> parse -> cache path.

    Every failure mode returns ``[]``:
        * cache layer raised
        * rate-limit bucket empty
        * connection / read error
        * non-200 response
        * malformed XML
    """
    try:
        hit = _RSS_CACHE.get(cache_key)
    except Exception:
        hit = None
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
    parsed = _parse_rss_xml(text, source=source, feed=feed)

    try:
        _RSS_CACHE.set(cache_key, parsed, ttl_seconds=_RSS_CACHE_TTL)
    except Exception:
        # Cache failure must not break the call path.
        pass
    return parsed


# ---------------------------------------------------------------------------
# Public fetchers
# ---------------------------------------------------------------------------


def _filter_and_dedupe(
    items: List[Dict[str, Any]], cutoff_iso: Optional[str]
) -> List[Dict[str, Any]]:
    """Drop items past ``cutoff_iso`` and dedupe by URL.

    Items without a URL are kept (cannot be reliably deduped).
    Sort order: newest ``published_at`` first; items with no date sink to
    the bottom (sorts ``""`` last when descending).
    """
    seen_urls: set = set()
    kept: List[Dict[str, Any]] = []
    for it in items:
        if not _within_age_window(it, cutoff_iso):
            continue
        url = it.get("url") or ""
        if url:
            if url in seen_urls:
                continue
            seen_urls.add(url)
        kept.append(it)
    kept.sort(key=lambda x: x.get("published_at") or "", reverse=True)
    return kept


def fetch_fda_press_releases(max_age_days: int = 30) -> List[Dict[str, Any]]:
    """Recent FDA press-release items, newest first.

    Combines the general FDA press feed and the FDA drug-approvals feed,
    deduping by URL. Items older than ``max_age_days`` (vs. today's date)
    are dropped. Each entry::

        {
          "title": str,
          "summary": str,
          "url": str,
          "published_at": "YYYY-MM-DD",
          "source": "FDA",
          "feed": "press" | "drug_approvals",
        }

    Returns ``[]`` on any error so the Thesis pipeline never crashes on a
    feed outage. Cached for 1h per (feed, max_age_days) tuple.
    """
    try:
        press = _fetch_rss(
            FDA_PRESS_RSS,
            cache_key="fda/press",
            source="FDA",
            feed="press",
        )
    except Exception:
        press = []
    try:
        approvals = _fetch_rss(
            FDA_DRUG_APPROVALS_RSS,
            cache_key="fda/drug_approvals",
            source="FDA",
            feed="drug_approvals",
        )
    except Exception:
        approvals = []

    cutoff = _age_cutoff_iso(max_age_days)
    return _filter_and_dedupe(press + approvals, cutoff)


def fetch_eia_today_in_energy(max_age_days: int = 30) -> List[Dict[str, Any]]:
    """Recent EIA "Today in Energy" items, newest first.

    Same shape as ``fetch_fda_press_releases``; ``source="EIA"`` and
    ``feed="today_in_energy"``. Returns ``[]`` on any error.
    """
    try:
        items = _fetch_rss(
            EIA_TODAY_IN_ENERGY_RSS,
            cache_key="eia/today_in_energy",
            source="EIA",
            feed="today_in_energy",
        )
    except Exception:
        items = []
    cutoff = _age_cutoff_iso(max_age_days)
    return _filter_and_dedupe(items, cutoff)


def fetch_fed_press_releases(max_age_days: int = 30) -> List[Dict[str, Any]]:
    """Recent Federal Reserve press releases, newest first.

    Includes FOMC statements, supervisory letters, stress-test results,
    and similar regulatory communications. ``source="Fed"`` and
    ``feed="press"``. Returns ``[]`` on any error.
    """
    try:
        items = _fetch_rss(
            FED_PRESS_RELEASES_RSS,
            cache_key="fed/press",
            source="Fed",
            feed="press",
        )
    except Exception:
        items = []
    cutoff = _age_cutoff_iso(max_age_days)
    return _filter_and_dedupe(items, cutoff)


# ---------------------------------------------------------------------------
# Sector aggregation
# ---------------------------------------------------------------------------


def _ticker_sector(ticker: str) -> Optional[str]:
    """Which keyword map owns this ticker (if any)."""
    t = ticker.upper()
    if t in PHARMA_TICKER_KEYWORDS:
        return "pharma"
    if t in ENERGY_TICKER_KEYWORDS:
        return "energy"
    if t in FINANCIALS_TICKER_KEYWORDS:
        return "financials"
    return None


def _empty_aggregate(ticker: str) -> Dict[str, Any]:
    return {
        "ticker": ticker.upper(),
        "event_count": 0,
        "event_titles": [],
        "first_seen": None,
        "last_seen": None,
        "sectors_matched": [],
    }


def _date_bounds(items: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    dates = [
        str(it.get("published_at"))
        for it in items
        if it.get("published_at")
    ]
    if not dates:
        return None, None
    return min(dates), max(dates)


def aggregate_sector_events(
    tickers: List[str], max_age_days: int = 30
) -> Dict[str, Dict[str, Any]]:
    """Per-ticker aggregate of relevant items across the three feeds.

    For each ticker:

        1. Resolve sector via the keyword maps.
        2. Pull each feed once (shared across all tickers in this call).
        3. For pharma tickers, scan FDA items via ticker keywords AND
           ``is_sector_relevant("pharma")`` (drug-approval coverage).
        4. For energy tickers, scan EIA items via ticker keywords AND
           ``is_sector_relevant("energy")``.
        5. For financials, scan Fed items the same way.
        6. Aggregate event count, title sample (up to 3, newest first),
           and date bounds.

    Tickers outside all three keyword maps return an empty aggregate so
    the Thesis pipeline can fall back to neutral without a branch.
    """
    out: Dict[str, Dict[str, Any]] = {}
    if not tickers:
        return out

    # Tickers we need to actually run. Empty-map tickers still go in the
    # output (downstream consumers iterate over the input list).
    interesting_sectors = {
        _ticker_sector(t) for t in tickers if _ticker_sector(t) is not None
    }

    # Pull feeds we actually need. If no ticker in the input is pharma,
    # don't bother hitting the FDA feed at all.
    fda_items: List[Dict[str, Any]] = (
        fetch_fda_press_releases(max_age_days=max_age_days)
        if "pharma" in interesting_sectors
        else []
    )
    eia_items: List[Dict[str, Any]] = (
        fetch_eia_today_in_energy(max_age_days=max_age_days)
        if "energy" in interesting_sectors
        else []
    )
    fed_items: List[Dict[str, Any]] = (
        fetch_fed_press_releases(max_age_days=max_age_days)
        if "financials" in interesting_sectors
        else []
    )

    for raw in tickers:
        ticker = str(raw).upper()
        sector = _ticker_sector(ticker)
        if sector is None:
            out[ticker] = _empty_aggregate(ticker)
            continue

        if sector == "pharma":
            keywords = PHARMA_TICKER_KEYWORDS[ticker]
            pool = fda_items
        elif sector == "energy":
            keywords = ENERGY_TICKER_KEYWORDS[ticker]
            pool = eia_items
        else:  # financials
            keywords = FINANCIALS_TICKER_KEYWORDS[ticker]
            pool = fed_items

        matched: List[Dict[str, Any]] = []
        sectors_matched: set = set()
        for item in pool:
            ticker_hit = _matches_any_keyword(item, keywords)
            sector_hit = is_sector_relevant(item, sector)
            if ticker_hit or sector_hit:
                matched.append(item)
                if ticker_hit:
                    sectors_matched.add(sector)
                if sector_hit and sector not in sectors_matched:
                    # Sector-level hit still belongs to the same sector
                    # bucket — just tagged so callers can distinguish
                    # "named match" vs "sector mention".
                    sectors_matched.add(sector)

        # Sort newest first, then pick titles.
        matched.sort(key=lambda x: x.get("published_at") or "", reverse=True)
        first_seen, last_seen = _date_bounds(matched)
        titles: List[str] = []
        seen: set = set()
        for it in matched:
            t = (it.get("title") or "").strip()
            if not t or t in seen:
                continue
            seen.add(t)
            titles.append(t)
            if len(titles) >= 3:
                break

        out[ticker] = {
            "ticker": ticker,
            "event_count": len(matched),
            "event_titles": titles,
            "first_seen": first_seen,
            "last_seen": last_seen,
            "sectors_matched": sorted(sectors_matched),
        }
    return out


# ---------------------------------------------------------------------------
# Thesis-pillar adapter
# ---------------------------------------------------------------------------


def _empty_label_counts() -> Dict[str, int]:
    return {label: 0 for label in _SENTIMENT_LABELS}


def _events_sentiment(event_count: int) -> Optional[float]:
    """Conservative sector-news heuristic.

    Tuning rationale (per spec):
        * Regulatory mentions skew at-least-positive — FDA warning
          letters and CRLs still count as coverage, and "no news" is
          rarely good news for pharma.
        * 1-2 events => mildly positive (+0.2): in the news cycle.
        * 3+ events => stronger signal (+0.5): substantial sector activity.
        * 0 events  => no signal at all (None).

    A future Phase 3 LLM call could provide directional sentiment per
    article. Until then we keep the dial conservative.
    """
    if event_count <= 0:
        return None
    if event_count <= _EVENT_LOW_BAND_MAX:
        return _EVENT_LOW_SENTIMENT
    return _EVENT_HIGH_SENTIMENT


def parse_thesis_signal(events: Dict[str, Any]) -> Dict[str, Any]:
    """Convert an ``aggregate_sector_events`` entry to a Thesis payload.

    Output shape matches ``ai_news.compute_thesis_signal_from_news`` so
    downstream Thesis composition code doesn't need a branch::

        {
          "ticker": str,
          "article_count": int,
          "mean_sentiment_score": float | None,
          "mean_relevance_score": float | None,
          "label_counts": Dict[str, int],
          "source": "sector_rss_aggregate",
          "last_scored": "YYYY-MM-DD",
        }
    """
    events = events or {}
    ticker = str(events.get("ticker") or "").upper()
    event_count = int(events.get("event_count") or 0)

    label_counts = _empty_label_counts()
    if event_count > 0:
        # V1: regulatory coverage logged as Neutral mentions — no
        # directional inference. Phase 3 LLM scoring would split this
        # across the five labels.
        label_counts["Neutral"] = event_count

    sentiment = _events_sentiment(event_count)
    relevance: Optional[float] = 0.5 if event_count > 0 else None

    return {
        "ticker": ticker,
        "article_count": event_count,
        "mean_sentiment_score": sentiment,
        "mean_relevance_score": relevance,
        "label_counts": label_counts,
        "source": "sector_rss_aggregate",
        "last_scored": _today_iso(),
    }


__all__ = [
    "SectorRssError",
    "PHARMA_TICKER_KEYWORDS",
    "ENERGY_TICKER_KEYWORDS",
    "FINANCIALS_TICKER_KEYWORDS",
    "FDA_PRESS_RSS",
    "FDA_DRUG_APPROVALS_RSS",
    "EIA_TODAY_IN_ENERGY_RSS",
    "FED_PRESS_RELEASES_RSS",
    "fetch_fda_press_releases",
    "fetch_eia_today_in_energy",
    "fetch_fed_press_releases",
    "aggregate_sector_events",
    "parse_thesis_signal",
    "is_sector_relevant",
]
