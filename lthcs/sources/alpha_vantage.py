"""Alpha Vantage source client.

Wraps Alpha Vantage (https://www.alphavantage.co/query) for the LTHCS
Thesis Integrity pillar (news sentiment) and as a price fallback when
yfinance is unavailable.

Public functions:
    * ``get_news_sentiment(tickers, topics=None, limit=50)``
    * ``parse_ticker_sentiment(av_response, ticker)``
    * ``get_daily_prices(ticker, outputsize="compact")``

All upstream calls go through:
    * a 24h ``FileCache("alpha_vantage")`` for response bodies, and
    * a ``TokenBucket(capacity=25, refill_rate=25/86400)`` — Alpha Vantage's
      free tier is 25 requests/day. This is the binding constraint that
      shapes how the rest of the pipeline calls this source.

Auth: requires ``ALPHA_VANTAGE_API_KEY`` in the environment. Read lazily
at first call (not import time) so importing this module in a process
without the key set doesn't blow up.

Daily-price function choice: the spec mentions
``TIME_SERIES_DAILY_ADJUSTED`` but notes that AV moved that function to
their paid tier. We use ``TIME_SERIES_DAILY`` (free) since the pipeline
only hits this path as a fallback when yfinance fails, and an unadjusted
close is acceptable for that fallback purpose.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

import requests

from lthcs.sources._cache import FileCache
from lthcs.sources._ratelimit import TokenBucket

# 24 hours.
_CACHE_TTL_SECONDS = 24 * 60 * 60

_AV_URL = "https://www.alphavantage.co/query"

# Free tier: 25 requests per day, refilling smoothly.
_DAILY_LIMIT = 25
_REFILL_PER_SECOND = _DAILY_LIMIT / 86400.0

# The five labels Alpha Vantage uses on ``ticker_sentiment_label``.
_SENTIMENT_LABELS = (
    "Bearish",
    "Somewhat-Bearish",
    "Neutral",
    "Somewhat-Bullish",
    "Bullish",
)

# Daily-prices function. See module docstring for the rationale on using
# the free TIME_SERIES_DAILY instead of TIME_SERIES_DAILY_ADJUSTED.
_DAILY_FUNCTION = "TIME_SERIES_DAILY"

# Module-level singletons. One cache + one rate limiter per source.
_cache = FileCache("alpha_vantage")
_bucket = TokenBucket(capacity=_DAILY_LIMIT, refill_rate=_REFILL_PER_SECOND)


class AlphaVantageError(RuntimeError):
    """Raised when Alpha Vantage returns a non-200 or otherwise invalid response."""


class RateLimitExhausted(RuntimeError):
    """Raised when the local token bucket for Alpha Vantage is empty.

    V1's caller (the daily run) should catch this and fall back to the
    documented ``--skip-thesis`` path rather than block waiting for the
    next refill.
    """


def _api_key() -> str:
    """Read the Alpha Vantage API key from the environment.

    Read lazily at call time (not import time) so this module can be
    imported in processes that don't actually use Alpha Vantage.
    """
    key = os.environ.get("ALPHA_VANTAGE_API_KEY")
    if not key:
        raise RuntimeError(
            "ALPHA_VANTAGE_API_KEY is not set. Add it to your environment or "
            ".env file to use the Alpha Vantage source client."
        )
    return key


def _acquire_token() -> None:
    """Take one token from the daily bucket or raise ``RateLimitExhausted``."""
    if not _bucket.try_acquire():
        raise RateLimitExhausted(
            "Alpha Vantage daily quota exhausted (25 requests/day). "
            "Re-run later or use --skip-thesis."
        )


def _check_for_av_throttle(payload: Any) -> None:
    """Raise if Alpha Vantage returned a 200 with a throttle/note envelope.

    AV often returns HTTP 200 with a body like
    ``{"Note": "Thank you for using Alpha Vantage..."}`` or
    ``{"Information": "..."}`` when the upstream rate limit is hit.
    """
    if not isinstance(payload, dict):
        return
    for field in ("Note", "Information", "Error Message"):
        if field in payload:
            msg = str(payload[field])[:240]
            raise AlphaVantageError(
                f"Alpha Vantage returned a {field!r} envelope (likely rate "
                f"limited or invalid request): {msg}"
            )


def _http_get(params: Dict[str, str]) -> Dict[str, Any]:
    """Hit Alpha Vantage and return the parsed JSON body.

    Raises ``AlphaVantageError`` on non-200, on JSON-decode failure, or on
    a 200-with-throttle-envelope response.
    """
    resp = requests.get(_AV_URL, params=params, timeout=30)
    status = getattr(resp, "status_code", 0)
    if status != 200:
        body = ""
        try:
            body = resp.text[:200]
        except Exception:
            pass
        raise AlphaVantageError(
            f"Alpha Vantage returned HTTP {status} for function "
            f"{params.get('function')!r}: {body}"
        )
    try:
        payload = resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise AlphaVantageError(
            f"Alpha Vantage returned a non-JSON body: {exc}"
        ) from None
    _check_for_av_throttle(payload)
    return payload


# ---------------------------------------------------------------------------
# News sentiment
# ---------------------------------------------------------------------------


def _news_cache_key(
    tickers: List[str], topics: Optional[List[str]], limit: int
) -> str:
    """Build a deterministic cache key for a NEWS_SENTIMENT request.

    The ticker list is sorted so callers that pass the same set of
    tickers in different orders hit the same cache entry.
    """
    sorted_tickers = ",".join(sorted(t.upper() for t in tickers))
    sorted_topics = ",".join(sorted(topics)) if topics else ""
    return f"news_sentiment/{sorted_tickers}/{sorted_topics}/{limit}"


def get_news_sentiment(
    tickers: List[str],
    topics: Optional[List[str]] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    """Fetch the NEWS_SENTIMENT batch for ``tickers``.

    One Alpha Vantage call returns sentiment-scored articles for all
    tickers in the request. Cached for 24h. Cache key is order-insensitive
    over the ticker list.

    Raises ``RateLimitExhausted`` if the local daily quota is empty.
    Raises ``AlphaVantageError`` on HTTP failures or throttle envelopes.
    """
    if not tickers:
        raise ValueError("tickers must be a non-empty list")

    key = _news_cache_key(tickers, topics, limit)
    hit = _cache.get(key)
    if hit is not None:
        return hit.value  # type: ignore[no-any-return]

    # Only consume a token (and require the API key) on a real upstream call.
    _acquire_token()

    params: Dict[str, str] = {
        "function": "NEWS_SENTIMENT",
        "tickers": ",".join(t.upper() for t in tickers),
        "limit": str(limit),
        "apikey": _api_key(),
    }
    if topics:
        params["topics"] = ",".join(topics)

    payload = _http_get(params)
    _cache.set(key, payload, ttl_seconds=_CACHE_TTL_SECONDS)
    return payload


def _coerce_float(raw: Any) -> Optional[float]:
    """Alpha Vantage encodes numbers as strings; coerce to float or None."""
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def parse_ticker_sentiment(av_response: Dict[str, Any], ticker: str) -> Dict[str, Any]:
    """Extract per-ticker sentiment from a batched NEWS_SENTIMENT response.

    Pure function — no HTTP, no cache access. If ``ticker`` is not
    mentioned in any article, returns ``article_count=0``,
    ``mean_sentiment_score=None``, ``mean_relevance_score=None``, and a
    fully-zeroed label histogram.
    """
    target = ticker.upper()
    label_counts: Dict[str, int] = {label: 0 for label in _SENTIMENT_LABELS}
    sent_scores: List[float] = []
    rel_scores: List[float] = []

    feed = []
    if isinstance(av_response, dict):
        feed = av_response.get("feed") or []

    for article in feed:
        if not isinstance(article, dict):
            continue
        for ts in article.get("ticker_sentiment", []) or []:
            if not isinstance(ts, dict):
                continue
            if str(ts.get("ticker", "")).upper() != target:
                continue
            sent = _coerce_float(ts.get("ticker_sentiment_score"))
            rel = _coerce_float(ts.get("relevance_score"))
            if sent is not None:
                sent_scores.append(sent)
            if rel is not None:
                rel_scores.append(rel)
            label = ts.get("ticker_sentiment_label")
            if isinstance(label, str) and label in label_counts:
                label_counts[label] += 1
            # Stop after the first matching entry per article — a single
            # article never lists the same ticker twice.
            break

    article_count = sum(label_counts.values())
    # If the label was missing/unknown but we still saw a score, fall
    # back to the score-count so article_count reflects mentions.
    if article_count == 0 and sent_scores:
        article_count = len(sent_scores)

    mean_sent: Optional[float] = (
        sum(sent_scores) / len(sent_scores) if sent_scores else None
    )
    mean_rel: Optional[float] = (
        sum(rel_scores) / len(rel_scores) if rel_scores else None
    )

    return {
        "ticker": target,
        "article_count": article_count,
        "mean_sentiment_score": mean_sent,
        "mean_relevance_score": mean_rel,
        "label_counts": label_counts,
    }


# ---------------------------------------------------------------------------
# Daily prices (fallback)
# ---------------------------------------------------------------------------


def _prices_cache_key(ticker: str, outputsize: str) -> str:
    return f"daily_prices/{ticker.upper()}/{outputsize}"


def _parse_daily_prices(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Normalize an Alpha Vantage TIME_SERIES_DAILY body into OHLCV rows."""
    series = payload.get("Time Series (Daily)") or {}
    rows: List[Dict[str, Any]] = []
    for date_str, bar in series.items():
        if not isinstance(bar, dict):
            continue
        try:
            rows.append(
                {
                    "date": date_str,
                    "open": float(bar["1. open"]),
                    "high": float(bar["2. high"]),
                    "low": float(bar["3. low"]),
                    "close": float(bar["4. close"]),
                    "volume": int(float(bar["5. volume"])),
                }
            )
        except (KeyError, TypeError, ValueError):
            # Skip malformed bars rather than fail the whole fetch.
            continue
    rows.sort(key=lambda r: r["date"])
    return rows


def get_daily_prices(
    ticker: str, outputsize: str = "compact"
) -> List[Dict[str, Any]]:
    """Return daily OHLCV bars for ``ticker`` via Alpha Vantage.

    This is the fallback path used when yfinance fails. ``outputsize``
    is passed straight through to Alpha Vantage:
    ``"compact"`` (last 100 bars) or ``"full"`` (~20 years).

    Cached for 24h per (ticker, outputsize).
    """
    key = _prices_cache_key(ticker, outputsize)
    hit = _cache.get(key)
    if hit is not None:
        return list(hit.value)

    _acquire_token()

    params: Dict[str, str] = {
        "function": _DAILY_FUNCTION,
        "symbol": ticker.upper(),
        "outputsize": outputsize,
        "apikey": _api_key(),
    }
    payload = _http_get(params)
    rows = _parse_daily_prices(payload)
    _cache.set(key, rows, ttl_seconds=_CACHE_TTL_SECONDS)
    return rows
