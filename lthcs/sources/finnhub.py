"""Finnhub source client.

Wraps three Finnhub endpoints used by the LTHCS pipeline:

* ``company-news`` — per-ticker headline feed for the trailing N days
* ``news-sentiment`` — bullish/bearish percentage + buzz score per ticker
* ``stock/recommendation`` — analyst recommendation trends (monthly buckets)

The news-sentiment endpoint is the one that materially changes the
pipeline: it ships ``bullishPercent``/``bearishPercent`` directly, so we
don't have to derive a label distribution from raw headlines the way we
do with Alpha Vantage. The recommendation-trends feed fills the
"analyst actions" slot of the Institutional Confidence pillar that V1
previously stubbed out while waiting for a 13F source.

Public API (see individual docstrings for shapes):

* ``get_company_news(ticker, days=30)``
* ``get_news_sentiment(ticker)``
* ``get_recommendation_trends(ticker)``
* ``parse_thesis_signal(sentiment_dict)``
* ``parse_recommendation_signal(reco_trends)``

All upstream calls go through:

* a ``FileCache`` per endpoint (``finnhub_news``, ``finnhub_sentiment``,
  ``finnhub_recommendations``) — 24h TTL for news/sentiment, 7d for
  recommendations.
* a single ``TokenBucket(capacity=10, refill_rate=1.0)`` shared across
  all three endpoints — Finnhub free tier is 60 requests/minute, i.e.
  1 req/sec, with bursts.

Auth: ``FINNHUB_API_KEY`` read lazily at the first call site so importing
this module in a process without the key doesn't blow up. If
``python-dotenv`` happens to be installed, ``.env`` is loaded.

Error policy:

* Missing API key -> ``FinnhubAPIKeyMissing`` at first call
* HTTP 429 -> ``FinnhubRateLimit`` (caller should stop & retry later)
* Other non-200 / network error / invalid JSON -> log and return the
  function's documented "empty" shape (``[]`` / ``{}``). The pipeline
  should keep running on the other sources rather than crash.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
from typing import Any, Dict, List, Optional

import requests

from lthcs.sources._cache import FileCache
from lthcs.sources._ratelimit import TokenBucket
from lthcs.sources import _api_counter

# Best-effort .env load. python-dotenv is in the project's requirements,
# but if it isn't available at runtime we just fall back to os.environ.
try:  # pragma: no cover - import-side optional dependency
    from dotenv import load_dotenv as _load_dotenv  # type: ignore

    _load_dotenv()
except Exception:  # pragma: no cover
    pass


_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

_FINNHUB_BASE = "https://finnhub.io/api/v1"

# 24h for daily-refresh endpoints (news + sentiment),
# 7d for monthly-refresh endpoints (recommendation trends).
_CACHE_TTL_NEWS_SECONDS = 24 * 60 * 60
_CACHE_TTL_SENTIMENT_SECONDS = 24 * 60 * 60
_CACHE_TTL_RECO_SECONDS = 7 * 24 * 60 * 60

# 60 req/min => 1 req/sec; capacity 10 lets a small burst through.
_BUCKET_CAPACITY = 10
_BUCKET_REFILL_PER_SECOND = 1.0

# HTTP timeout — Finnhub typically responds in <1s; 30s is a generous
# upper bound that still won't hang the daily pipeline forever.
_HTTP_TIMEOUT = 30

# Canonical five-bucket label histogram, matching Alpha Vantage's labels
# so downstream Thesis Integrity code can treat Finnhub and AV identically.
_SENTIMENT_LABELS = (
    "Bearish",
    "Somewhat-Bearish",
    "Neutral",
    "Somewhat-Bullish",
    "Bullish",
)


# Module-level singletons. One cache per endpoint, one shared bucket.
_NEWS_CACHE = FileCache("finnhub_news")
_SENTIMENT_CACHE = FileCache("finnhub_sentiment")
_RECO_CACHE = FileCache("finnhub_recommendations")
_BUCKET = TokenBucket(
    capacity=_BUCKET_CAPACITY, refill_rate=_BUCKET_REFILL_PER_SECOND
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class FinnhubError(RuntimeError):
    """Base class for Finnhub-related errors."""


class FinnhubAPIKeyMissing(FinnhubError):
    """Raised at the first call when ``FINNHUB_API_KEY`` is unset."""


class FinnhubRateLimit(FinnhubError):
    """Raised when Finnhub returns HTTP 429.

    The caller should treat this as a hard stop for the remainder of the
    minute and either fall back to cached data or skip the source.
    """


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _api_key() -> str:
    """Read the Finnhub API key from the environment.

    Read lazily at call time (not import time) so this module can be
    imported in processes that don't actually exercise Finnhub.
    """
    key = os.environ.get("FINNHUB_API_KEY")
    if not key:
        raise FinnhubAPIKeyMissing(
            "FINNHUB_API_KEY is not set. Add it to your environment or "
            ".env file to use the Finnhub source client."
        )
    return key


def _acquire_token() -> None:
    """Take one token from the shared bucket, blocking up to ~2s if needed.

    Finnhub's free tier is 60 req/min, so a single token never takes more
    than ~1s to refill. We use a short blocking ``acquire`` here (rather
    than ``try_acquire``) because the binding constraint on this client
    is per-minute, not per-day — briefly waiting is the correct behaviour
    rather than failing fast.
    """
    # Block up to 2s for a token. If the bucket is empty for longer than
    # that we surface a rate-limit error to the caller; that means the
    # process is sustainedly above the budget and should back off.
    if not _BUCKET.acquire(1.0, timeout=2.0):
        raise FinnhubRateLimit(
            "Finnhub local rate-limit bucket exhausted (60 req/min). "
            "Back off and retry."
        )


def _http_get(path: str, params: Dict[str, str]) -> Optional[Any]:
    """Hit Finnhub and return the parsed JSON body.

    Returns ``None`` on any recoverable failure (non-200 other than 429,
    network error, invalid JSON). The caller is responsible for
    translating ``None`` into the documented empty shape for that
    endpoint.

    Raises:
        FinnhubRateLimit: on HTTP 429.
    """
    # Always send the API key as a query param (Finnhub accepts both
    # ``token=`` and header auth; query is simpler to cache-key on).
    full = dict(params)
    full["token"] = _api_key()

    url = f"{_FINNHUB_BASE}{path}"
    try:
        resp = requests.get(url, params=full, timeout=_HTTP_TIMEOUT)
    except Exception as exc:  # network / DNS / TLS
        _api_counter.bump("finnhub", "error")
        _logger.warning("Finnhub network error for %s: %s", path, exc)
        return None

    status = getattr(resp, "status_code", 0)
    if status == 429:
        # Surface rate-limit so the caller can stop scheduling more work.
        _api_counter.bump("finnhub", "rate_limit")
        raise FinnhubRateLimit(
            f"Finnhub returned HTTP 429 for {path!r}. Hourly/minute quota hit."
        )
    if status != 200:
        _api_counter.bump("finnhub", "error")
        body = ""
        try:
            body = (resp.text or "")[:200]
        except Exception:
            pass
        _logger.warning(
            "Finnhub returned HTTP %s for %s: %s", status, path, body
        )
        return None

    try:
        body_json = resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        _api_counter.bump("finnhub", "error")
        _logger.warning(
            "Finnhub returned non-JSON body for %s: %s", path, exc
        )
        return None
    _api_counter.bump("finnhub", "ok")
    return body_json


def _today_iso() -> str:
    return _dt.date.today().isoformat()


def _days_ago_iso(days: int) -> str:
    days = max(int(days), 0)
    return (_dt.date.today() - _dt.timedelta(days=days)).isoformat()


def _safe_int(raw: Any) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _safe_float(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _empty_label_counts() -> Dict[str, int]:
    return {label: 0 for label in _SENTIMENT_LABELS}


# ---------------------------------------------------------------------------
# company-news
# ---------------------------------------------------------------------------


def _news_cache_key(ticker: str, days: int) -> str:
    # Include today's date so the cache naturally rolls over with the
    # window even when the on-disk TTL hasn't expired.
    return f"company_news/{ticker.upper()}/{days}/{_today_iso()}"


def get_company_news(
    ticker: Optional[str], days: int = 30
) -> List[Dict[str, Any]]:
    """Pull Finnhub company-news feed for the last ``days`` days.

    Returns a list of dicts with the keys ``datetime`` (unix seconds, as
    Finnhub returns), ``headline``, ``summary``, ``source``, ``url``,
    ``category``. Cached for 24h per ``(ticker, days)`` pair.

    A falsy ``ticker`` returns ``[]`` without any HTTP call. Network
    errors, non-200 (other than 429), or invalid JSON also yield ``[]``.
    """
    if not ticker:
        return []
    days = max(int(days), 1)

    key = _news_cache_key(ticker, days)
    hit = _NEWS_CACHE.get(key)
    if hit is not None:
        return list(hit.value or [])

    _acquire_token()

    params = {
        "symbol": str(ticker).upper(),
        "from": _days_ago_iso(days),
        "to": _today_iso(),
    }
    payload = _http_get("/company-news", params)
    if not isinstance(payload, list):
        return []

    normalized: List[Dict[str, Any]] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        normalized.append(
            {
                "datetime": _safe_int(entry.get("datetime")),
                "headline": str(entry.get("headline") or ""),
                "summary": str(entry.get("summary") or ""),
                "source": str(entry.get("source") or ""),
                "url": str(entry.get("url") or ""),
                "category": str(entry.get("category") or ""),
            }
        )

    _NEWS_CACHE.set(key, normalized, ttl_seconds=_CACHE_TTL_NEWS_SECONDS)
    return normalized


# ---------------------------------------------------------------------------
# news-sentiment
# ---------------------------------------------------------------------------


def _sentiment_cache_key(ticker: str) -> str:
    return f"news_sentiment/{ticker.upper()}/{_today_iso()}"


def get_news_sentiment(ticker: Optional[str]) -> Dict[str, Any]:
    """Pull Finnhub news-sentiment snapshot for ``ticker``.

    Returns:
        Dict with keys ``ticker``, ``article_count``, ``weekly_average``,
        ``buzz_score``, ``company_news_score``, ``sector_avg_news_score``,
        ``bullish_percent``, ``bearish_percent``, ``sector_avg_bullish``.
        Empty ``{}`` on missing ticker, network error, or non-200.

    Cached 24h.
    """
    if not ticker:
        return {}

    key = _sentiment_cache_key(ticker)
    hit = _SENTIMENT_CACHE.get(key)
    if hit is not None:
        return dict(hit.value or {})

    _acquire_token()

    payload = _http_get(
        "/news-sentiment", {"symbol": str(ticker).upper()}
    )
    if not isinstance(payload, dict):
        return {}

    buzz = payload.get("buzz") if isinstance(payload.get("buzz"), dict) else {}
    sent = (
        payload.get("sentiment")
        if isinstance(payload.get("sentiment"), dict)
        else {}
    )

    normalized: Dict[str, Any] = {
        "ticker": str(payload.get("symbol") or ticker).upper(),
        "article_count": _safe_int(buzz.get("articlesInLastWeek")),
        "weekly_average": _safe_float(buzz.get("weeklyAverage")) or 0.0,
        "buzz_score": _safe_float(buzz.get("buzz")) or 0.0,
        "company_news_score": _safe_float(payload.get("companyNewsScore"))
        or 0.0,
        "sector_avg_news_score": _safe_float(
            payload.get("sectorAverageNewsScore")
        )
        or 0.0,
        "bullish_percent": _safe_float(sent.get("bullishPercent")) or 0.0,
        "bearish_percent": _safe_float(sent.get("bearishPercent")) or 0.0,
        "sector_avg_bullish": _safe_float(
            payload.get("sectorAverageBullishPercent")
        )
        or 0.0,
    }

    _SENTIMENT_CACHE.set(
        key, normalized, ttl_seconds=_CACHE_TTL_SENTIMENT_SECONDS
    )
    return normalized


# ---------------------------------------------------------------------------
# recommendation trends
# ---------------------------------------------------------------------------


def _reco_cache_key(ticker: str, as_of: Optional[str] = None) -> str:
    # Weekly cache key: recommendation trends are monthly, so we tolerate
    # up to a week's staleness without re-fetching.
    #
    # When ``as_of`` is provided, embed it in the key so that historical
    # views (used by the LTHCS backfill) don't collide with the live
    # current-week view, and different historical anchors don't collide
    # with each other.
    iso_week = _dt.date.today().isocalendar()
    base = (
        f"recommendation_trends/{ticker.upper()}/"
        f"{iso_week[0]}W{iso_week[1]:02d}"
    )
    if as_of:
        return f"{base}/as_of/{as_of}"
    return base


def _normalize_reco_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "period": str(entry.get("period") or ""),
        "strong_buy": _safe_int(entry.get("strongBuy")),
        "buy": _safe_int(entry.get("buy")),
        "hold": _safe_int(entry.get("hold")),
        "sell": _safe_int(entry.get("sell")),
        "strong_sell": _safe_int(entry.get("strongSell")),
    }


def get_recommendation_trends(
    ticker: Optional[str], as_of: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Pull Finnhub recommendation-trends history for ``ticker``.

    Returns a list of monthly snapshots **newest first** in the shape:

        [{"period": "2026-05-01", "strong_buy": int, "buy": int,
          "hold": int, "sell": int, "strong_sell": int}, ...]

    Args:
        ticker: Symbol to fetch. Falsy ticker returns ``[]``.
        as_of: Optional ISO date (``YYYY-MM-DD``). When provided, the
            returned list is filtered to records with
            ``period <= as_of`` (inclusive). Used by the LTHCS backfill
            to reproduce historical analyst snapshots. ``None`` (the
            default) returns the full history exactly as before.

    Cached 7 days. Empty list on missing ticker, network error, or
    non-200. The cache key includes ``as_of`` so historical views don't
    collide with the live view or with each other.
    """
    if not ticker:
        return []

    key = _reco_cache_key(ticker, as_of=as_of)
    hit = _RECO_CACHE.get(key)
    if hit is not None:
        return list(hit.value or [])

    _acquire_token()

    payload = _http_get(
        "/stock/recommendation", {"symbol": str(ticker).upper()}
    )
    if not isinstance(payload, list):
        return []

    rows: List[Dict[str, Any]] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        rows.append(_normalize_reco_entry(entry))

    # Newest first by ISO ``period`` string (Finnhub's own ordering is
    # newest-first, but normalizing here makes us robust to upstream
    # changes).
    rows.sort(key=lambda r: r["period"], reverse=True)

    if as_of:
        # Inclusive filter: a record whose ``period`` equals ``as_of``
        # stays in the result. ISO ``YYYY-MM-DD`` strings compare
        # lexicographically the same as they do chronologically.
        rows = [r for r in rows if r.get("period") and r["period"] <= as_of]

    _RECO_CACHE.set(key, rows, ttl_seconds=_CACHE_TTL_RECO_SECONDS)
    return rows


# ---------------------------------------------------------------------------
# parse_thesis_signal — Finnhub sentiment -> Thesis pillar payload
# ---------------------------------------------------------------------------


def _distribute_labels(
    article_count: int, bullish: float, bearish: float
) -> Dict[str, int]:
    """Split ``article_count`` across the five canonical labels.

    Finnhub reports bullish/bearish percentages of articles (not five
    buckets), so we approximate the five-bucket histogram as:

        * bullish_percent  -> "Bullish"
        * bearish_percent  -> "Bearish"
        * remainder        -> "Neutral"

    The somewhat-* buckets stay zero. This is intentional — Finnhub
    doesn't expose a confidence-tier breakdown, so claiming we have one
    would be a fabricated signal. Downstream code that aggregates
    label_counts (e.g. compute_thesis_from_stored_sentiment) treats
    Bullish/Bearish/Neutral as the primary axis anyway.

    Largest-remainder rounding ensures the buckets sum to
    ``article_count``.
    """
    counts = _empty_label_counts()
    if article_count <= 0:
        return counts

    b = _clamp(bullish, 0.0, 1.0)
    s = _clamp(bearish, 0.0, 1.0)
    # If bullish + bearish > 1.0 due to upstream rounding, scale back.
    if b + s > 1.0:
        scale = 1.0 / (b + s)
        b *= scale
        s *= scale
    n = max(0.0, 1.0 - b - s)

    raw = {
        "Bullish": article_count * b,
        "Bearish": article_count * s,
        "Neutral": article_count * n,
    }
    # Floor each, then assign the remaining articles to the buckets with
    # the largest fractional parts (largest-remainder method).
    floored = {k: int(v) for k, v in raw.items()}
    assigned = sum(floored.values())
    leftover = article_count - assigned
    if leftover > 0:
        fractions = sorted(
            ((k, raw[k] - floored[k]) for k in raw),
            key=lambda kv: kv[1],
            reverse=True,
        )
        for k, _frac in fractions[:leftover]:
            floored[k] += 1

    counts["Bullish"] = floored["Bullish"]
    counts["Bearish"] = floored["Bearish"]
    counts["Neutral"] = floored["Neutral"]
    return counts


def parse_thesis_signal(sentiment_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Convert ``get_news_sentiment`` output to the Thesis pillar payload.

    Output shape matches ``alpha_vantage.parse_ticker_sentiment`` +
    ``ai_news.compute_thesis_signal_from_news`` so downstream
    ``compute_thesis_from_stored_sentiment`` doesn't need to branch on
    source.

    Sentiment derivation:
        * mean_sentiment_score = bullish_percent - bearish_percent
          (range [-1, +1]). ``None`` if article_count == 0.
        * mean_relevance_score = buzz_score clamped to [0, 1].
          ``None`` if article_count == 0.
        * label_counts: see ``_distribute_labels``.
    """
    if not isinstance(sentiment_dict, dict):
        sentiment_dict = {}

    ticker = str(sentiment_dict.get("ticker") or "").upper()
    article_count = _safe_int(sentiment_dict.get("article_count"))
    bullish = _safe_float(sentiment_dict.get("bullish_percent")) or 0.0
    bearish = _safe_float(sentiment_dict.get("bearish_percent")) or 0.0
    buzz_raw = _safe_float(sentiment_dict.get("buzz_score"))

    mean_sent: Optional[float] = None
    mean_rel: Optional[float] = None
    if article_count > 0:
        mean_sent = _clamp(bullish - bearish, -1.0, 1.0)
        # Buzz is "relative to sector average"; values >1 are common.
        # Clamp into [0, 1] so it slots in as a relevance score the same
        # way Alpha Vantage's relevance_score does.
        if buzz_raw is None:
            mean_rel = 0.0
        else:
            mean_rel = _clamp(buzz_raw, 0.0, 1.0)

    label_counts = _distribute_labels(article_count, bullish, bearish)

    return {
        "ticker": ticker,
        "article_count": article_count,
        "mean_sentiment_score": mean_sent,
        "mean_relevance_score": mean_rel,
        "label_counts": label_counts,
        "source": "finnhub",
        "last_scored": _today_iso(),
    }


# ---------------------------------------------------------------------------
# parse_recommendation_signal — analyst trends -> Institutional payload
# ---------------------------------------------------------------------------


# Weighting for consensus_score. Mirrors the standard analyst-score
# convention: strong_buy = +1, buy = +0.5, hold = 0, sell = -0.5,
# strong_sell = -1. The result is in [-1, +1].
_CONSENSUS_WEIGHTS = {
    "strong_buy": 1.0,
    "buy": 0.5,
    "hold": 0.0,
    "sell": -0.5,
    "strong_sell": -1.0,
}


def _consensus_score(snapshot: Dict[str, Any]) -> Optional[float]:
    total = 0
    weighted = 0.0
    for key, weight in _CONSENSUS_WEIGHTS.items():
        n = _safe_int(snapshot.get(key))
        total += n
        weighted += weight * n
    if total <= 0:
        return None
    return _clamp(weighted / total, -1.0, 1.0)


def parse_recommendation_signal(
    reco_trends: List[Dict[str, Any]],
    as_of: Optional[str] = None,
) -> Dict[str, Any]:
    """Convert recommendation trends to an analyst-actions signal.

    Fills the 13F-stub slot of the Institutional Confidence pillar.

    Input is a list of monthly snapshots (newest first), each with the
    keys produced by ``get_recommendation_trends``.

    Args:
        reco_trends: List of monthly snapshots (any order; this function
            re-sorts defensively).
        as_of: Optional ISO date (``YYYY-MM-DD``). When provided, the
            anchor "latest" record is the most recent snapshot with
            ``period <= as_of`` (inclusive). Snapshots after ``as_of``
            are ignored for both the latest-month payload and the
            MoM-delta prior. ``None`` (default) preserves the original
            behaviour exactly: anchor on whatever the newest record is.

    Output:

        {
          "ticker": str,
          "latest_month": str | None,
          "buy_count": int,                # strong_buy + buy
          "hold_count": int,
          "sell_count": int,               # sell + strong_sell
          "total_analysts": int,
          "consensus_score": float | None, # weighted avg in [-1, +1]
          "change_from_prior_month": float | None,  # MoM delta
        }
    """
    empty = {
        "ticker": "",
        "latest_month": None,
        "buy_count": 0,
        "hold_count": 0,
        "sell_count": 0,
        "total_analysts": 0,
        "consensus_score": None,
        "change_from_prior_month": None,
    }

    if not reco_trends or not isinstance(reco_trends, list):
        return empty

    # Defensive sort: newest first by ``period`` string. The source
    # function already does this but we don't want a caller's local
    # mutation to silently flip the meaning of "latest".
    rows = sorted(
        (r for r in reco_trends if isinstance(r, dict)),
        key=lambda r: str(r.get("period") or ""),
        reverse=True,
    )

    if as_of:
        # Inclusive: a record with period == as_of is kept and may serve
        # as the anchor. Records after as_of are dropped entirely so the
        # MoM delta also uses the right historical prior.
        rows = [
            r for r in rows
            if str(r.get("period") or "") and str(r.get("period")) <= as_of
        ]

    if not rows:
        return empty

    latest = rows[0]
    ticker = str(latest.get("ticker") or "").upper()

    strong_buy = _safe_int(latest.get("strong_buy"))
    buy = _safe_int(latest.get("buy"))
    hold = _safe_int(latest.get("hold"))
    sell = _safe_int(latest.get("sell"))
    strong_sell = _safe_int(latest.get("strong_sell"))
    total = strong_buy + buy + hold + sell + strong_sell

    consensus = _consensus_score(latest)

    change_mom: Optional[float] = None
    if consensus is not None and len(rows) > 1:
        prior = _consensus_score(rows[1])
        if prior is not None:
            change_mom = consensus - prior

    return {
        "ticker": ticker,
        "latest_month": str(latest.get("period") or "") or None,
        "buy_count": strong_buy + buy,
        "hold_count": hold,
        "sell_count": sell + strong_sell,
        "total_analysts": total,
        "consensus_score": consensus,
        "change_from_prior_month": change_mom,
    }


__all__ = [
    "FinnhubError",
    "FinnhubAPIKeyMissing",
    "FinnhubRateLimit",
    "get_company_news",
    "get_news_sentiment",
    "get_recommendation_trends",
    "parse_thesis_signal",
    "parse_recommendation_signal",
]
