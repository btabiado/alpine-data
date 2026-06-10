"""Yahoo Finance source client.

Wraps ``yfinance`` to provide daily prices, volatility, and momentum for
all 75 universe tickers. No API key is needed; yfinance scrapes Yahoo's
public endpoints, so we keep request rates conservative.

Public functions:
    * ``get_daily_prices(ticker, period="1y", as_of=None)``
    * ``get_volatility(ticker, window=30, as_of=None)``
    * ``get_momentum_pct(ticker, days=90, as_of=None)``

All public functions accept an optional ``as_of="YYYY-MM-DD"`` argument
that returns results AS-OF that historical date — i.e. as if the
function were called on ``as_of``. When ``as_of`` is ``None`` (the
default) behaviour is unchanged: results end on today.

All upstream calls go through:
    * a 24h ``FileCache("yahoo")`` for response bodies, and
    * a ``TokenBucket(capacity=10, refill_rate=1.0)`` (1 req/sec burst 10).

The cache stores price history as a list of JSON-friendly dicts so the
derived metrics (volatility, momentum) can be computed from cached data
without re-hitting Yahoo.
"""

from __future__ import annotations

import datetime as _dt
import math
from typing import Any, Dict, List, Optional

import yfinance as yf

from lthcs.sources._cache import FileCache
from lthcs.sources._ratelimit import TokenBucket
from lthcs.sources import _api_counter

# 24 hours.
_CACHE_TTL_SECONDS = 24 * 60 * 60

# Trading days in a year, used to annualize realized volatility.
_TRADING_DAYS = 252

# When ``as_of`` is supplied we widen the upstream fetch window so we
# don't accidentally request a period that has already rolled past the
# requested historical date. 2y comfortably covers a 12m momentum
# lookback measured from any as_of in the recent past.
_AS_OF_FETCH_PERIOD = "2y"

# Module-level singletons. One cache + one rate limiter per source.
_cache = FileCache("yahoo")
_bucket = TokenBucket(capacity=10, refill_rate=1.0)


def _cache_key(ticker: str, period: str, as_of: Optional[str] = None) -> str:
    if as_of:
        return f"{ticker}/prices/{period}/asof/{as_of}"
    return f"{ticker}/prices/{period}"


def _validate_as_of(as_of: Optional[str]) -> Optional[str]:
    """Normalise an ``as_of`` argument to ISO YYYY-MM-DD or ``None``.

    Invalid strings are coerced to ``None`` so callers can pass whatever
    they have without raising — a missing/garbled as_of just degrades to
    today.
    """
    if as_of is None:
        return None
    if not isinstance(as_of, str) or not as_of.strip():
        return None
    try:
        return _dt.date.fromisoformat(as_of.strip()).isoformat()
    except ValueError:
        return None


def _slice_to_as_of(rows: List[Dict[str, Any]], as_of: str) -> List[Dict[str, Any]]:
    """Return only rows whose ``date`` is <= ``as_of`` (string compare OK for ISO)."""
    return [r for r in rows if str(r.get("date", "")) <= as_of]


def _row_to_dict(date_str: str, row: Any) -> Dict[str, Any]:
    """Coerce a yfinance row into a JSON-serialisable dict."""
    def _f(name: str) -> float:
        # yfinance occasionally returns NaN; coerce to float and let the
        # caller decide. We don't drop rows here.
        return float(row[name])

    # ``Adj Close`` is only emitted by yfinance when ``auto_adjust=False``
    # (the historical default). If it's missing, fall back to ``Close``.
    if "Adj Close" in row.index:
        adj_close = float(row["Adj Close"])
    else:
        adj_close = float(row["Close"])

    return {
        "date": date_str,
        "open": _f("Open"),
        "high": _f("High"),
        "low": _f("Low"),
        "close": _f("Close"),
        "adj_close": adj_close,
        "volume": int(row["Volume"]),
    }


def _yahoo_symbol_variants(ticker: str) -> List[str]:
    """Return Yahoo symbol variants to try, in order.

    Yahoo Finance uses ``BRK-B`` (hyphen) for class-share tickers; many
    upstream datasets (S&P, our universe.json) use ``BRK.B`` (dot). When
    a ticker contains a ``.`` we yield the hyphen-substituted form first
    (Yahoo's native convention — the dot form 404s, logging a spurious
    "possibly delisted" error and burning a rate-limited request every
    uncached run) and the original dot form as a fallback. Tickers
    without a dot return a single-element list (no fallback attempted).
    Only the outbound Yahoo symbol changes — the canonical dot ticker is
    still used for cache keys, history files, and snapshot rows.
    """
    if "." not in ticker:
        return [ticker]
    return [ticker.replace(".", "-"), ticker]


def _fetch_prices_from_yahoo(ticker: str, period: str) -> List[Dict[str, Any]]:
    """Hit yfinance (subject to the rate limiter) and normalize the result.

    For tickers containing a ``.`` (e.g. ``BRK.B``) we try the hyphen
    variant (``BRK-B``) first — Yahoo's symbol convention uses hyphens
    for class shares — and fall back to the dot form if it returns no
    rows.
    """
    for variant in _yahoo_symbol_variants(ticker):
        _bucket.acquire()
        t = yf.Ticker(variant)
        try:
            df = t.history(period=period)
        except Exception:
            _api_counter.bump("yahoo", "error")
            raise
        if df is None or len(df) == 0:
            _api_counter.bump("yahoo", "error")
            continue
        _api_counter.bump("yahoo", "ok")

        rows: List[Dict[str, Any]] = []
        for ts, row in df.iterrows():
            # ``ts`` may be a pandas Timestamp; strftime gives a stable
            # YYYY-MM-DD regardless of timezone metadata.
            try:
                date_str = ts.strftime("%Y-%m-%d")
            except AttributeError:
                date_str = str(ts)[:10]
            rows.append(_row_to_dict(date_str, row))
        if rows:
            return rows
    return []


def get_daily_prices(
    ticker: str,
    period: str = "1y",
    as_of: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return daily OHLCV bars for ``ticker`` over ``period``.

    Each bar is a dict with keys: ``date`` (YYYY-MM-DD string), ``open``,
    ``high``, ``low``, ``close``, ``adj_close`` (floats), ``volume`` (int).

    Results are cached for 24h per (ticker, period, as_of) and rate-limited
    at ~1 request/second with a burst of 10.

    When ``as_of`` is supplied (ISO ``YYYY-MM-DD``) the returned rows are
    sliced to ``date <= as_of`` — i.e. the function returns "what you
    would have seen on ``as_of``". To make sure the slice has enough
    history regardless of how far back ``as_of`` is, we widen the
    upstream yfinance fetch to ``2y`` when ``as_of`` is provided. The
    caller's ``period`` argument is still honoured for the cache key so
    different as-of views don't collide.

    Invalid ``as_of`` strings (non-ISO, empty, etc.) are silently ignored
    — the call behaves as if ``as_of=None`` was passed.
    """
    normalised_as_of = _validate_as_of(as_of)

    key = _cache_key(ticker, period, normalised_as_of)
    hit = _cache.get(key)
    if hit is not None:
        return list(hit.value)

    # When as_of is in play we always pull a wider window from yfinance
    # so a 12-month momentum lookback ending on any recent ``as_of``
    # still has the data it needs. Callers asking for a short ``period``
    # (e.g. "5d") would otherwise see only ~5 bars ending today, none of
    # which extend back to a historical ``as_of``.
    fetch_period = _AS_OF_FETCH_PERIOD if normalised_as_of else period
    rows = _fetch_prices_from_yahoo(ticker, fetch_period)
    if normalised_as_of:
        rows = _slice_to_as_of(rows, normalised_as_of)

    _cache.set(key, rows, ttl_seconds=_CACHE_TTL_SECONDS)
    return rows


def _closes(prices: List[Dict[str, Any]]) -> List[float]:
    return [float(p["close"]) for p in prices]


def get_volatility(
    ticker: str,
    window: int = 30,
    as_of: Optional[str] = None,
) -> Optional[float]:
    """Annualized stdev of the trailing ``window`` daily returns.

    Returns ``None`` if fewer than ``window`` returns are available.
    Annualization uses sqrt(252).

    When ``as_of`` is supplied the window ends on the last trading bar
    at or before ``as_of`` rather than today.
    """
    if window <= 1:
        return None

    # We need ``window`` returns, which requires ``window + 1`` closes.
    # Pull enough history to cover that comfortably (default 1y is fine).
    prices = get_daily_prices(ticker, as_of=as_of)
    closes = _closes(prices)
    if len(closes) < window + 1:
        return None

    # Daily simple returns from the most recent ``window + 1`` closes.
    tail = closes[-(window + 1):]
    returns = [tail[i] / tail[i - 1] - 1.0 for i in range(1, len(tail))]
    n = len(returns)
    mean = sum(returns) / n
    # Sample stdev (ddof=1) — standard for return volatility.
    variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
    daily_std = math.sqrt(variance)
    return daily_std * math.sqrt(_TRADING_DAYS)


def get_momentum_pct(
    ticker: str,
    days: int = 90,
    as_of: Optional[str] = None,
) -> Optional[float]:
    """Return ``(last_close / close_N_days_ago) - 1`` as a decimal.

    ``days`` is measured in trading-day bars, not calendar days. Returns
    ``None`` if fewer than ``days + 1`` bars are available.

    When ``as_of`` is supplied the "last close" is the last trading bar
    at or before ``as_of`` rather than today.
    """
    if days <= 0:
        return None

    prices = get_daily_prices(ticker, as_of=as_of)
    closes = _closes(prices)
    if len(closes) < days + 1:
        return None

    last = closes[-1]
    past = closes[-(days + 1)]
    if past == 0:
        return None
    return last / past - 1.0
