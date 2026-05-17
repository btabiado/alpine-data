"""Yahoo Finance source client.

Wraps ``yfinance`` to provide daily prices, volatility, and momentum for
all 75 universe tickers. No API key is needed; yfinance scrapes Yahoo's
public endpoints, so we keep request rates conservative.

Public functions:
    * ``get_daily_prices(ticker, period="1y")``
    * ``get_volatility(ticker, window=30)``
    * ``get_momentum_pct(ticker, days=90)``

All upstream calls go through:
    * a 24h ``FileCache("yahoo")`` for response bodies, and
    * a ``TokenBucket(capacity=10, refill_rate=1.0)`` (1 req/sec burst 10).

The cache stores price history as a list of JSON-friendly dicts so the
derived metrics (volatility, momentum) can be computed from cached data
without re-hitting Yahoo.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

import yfinance as yf

from lthcs.sources._cache import FileCache
from lthcs.sources._ratelimit import TokenBucket

# 24 hours.
_CACHE_TTL_SECONDS = 24 * 60 * 60

# Trading days in a year, used to annualize realized volatility.
_TRADING_DAYS = 252

# Module-level singletons. One cache + one rate limiter per source.
_cache = FileCache("yahoo")
_bucket = TokenBucket(capacity=10, refill_rate=1.0)


def _cache_key(ticker: str, period: str) -> str:
    return f"{ticker}/prices/{period}"


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


def _fetch_prices_from_yahoo(ticker: str, period: str) -> List[Dict[str, Any]]:
    """Hit yfinance (subject to the rate limiter) and normalize the result."""
    _bucket.acquire()
    t = yf.Ticker(ticker)
    df = t.history(period=period)
    if df is None or len(df) == 0:
        return []

    rows: List[Dict[str, Any]] = []
    for ts, row in df.iterrows():
        # ``ts`` may be a pandas Timestamp; strftime gives a stable
        # YYYY-MM-DD regardless of timezone metadata.
        try:
            date_str = ts.strftime("%Y-%m-%d")
        except AttributeError:
            date_str = str(ts)[:10]
        rows.append(_row_to_dict(date_str, row))
    return rows


def get_daily_prices(ticker: str, period: str = "1y") -> List[Dict[str, Any]]:
    """Return daily OHLCV bars for ``ticker`` over ``period``.

    Each bar is a dict with keys: ``date`` (YYYY-MM-DD string), ``open``,
    ``high``, ``low``, ``close``, ``adj_close`` (floats), ``volume`` (int).

    Results are cached for 24h per (ticker, period) and rate-limited at
    ~1 request/second with a burst of 10.
    """
    key = _cache_key(ticker, period)
    hit = _cache.get(key)
    if hit is not None:
        return list(hit.value)

    rows = _fetch_prices_from_yahoo(ticker, period)
    _cache.set(key, rows, ttl_seconds=_CACHE_TTL_SECONDS)
    return rows


def _closes(prices: List[Dict[str, Any]]) -> List[float]:
    return [float(p["close"]) for p in prices]


def get_volatility(ticker: str, window: int = 30) -> Optional[float]:
    """Annualized stdev of the trailing ``window`` daily returns.

    Returns ``None`` if fewer than ``window`` returns are available.
    Annualization uses sqrt(252).
    """
    if window <= 1:
        return None

    # We need ``window`` returns, which requires ``window + 1`` closes.
    # Pull enough history to cover that comfortably (default 1y is fine).
    prices = get_daily_prices(ticker)
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


def get_momentum_pct(ticker: str, days: int = 90) -> Optional[float]:
    """Return ``(last_close / close_N_days_ago) - 1`` as a decimal.

    ``days`` is measured in trading-day bars, not calendar days. Returns
    ``None`` if fewer than ``days + 1`` bars are available.
    """
    if days <= 0:
        return None

    prices = get_daily_prices(ticker)
    closes = _closes(prices)
    if len(closes) < days + 1:
        return None

    last = closes[-1]
    past = closes[-(days + 1)]
    if past == 0:
        return None
    return last / past - 1.0
