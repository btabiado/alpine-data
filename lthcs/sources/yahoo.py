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

History is requested with ``auto_adjust=False``. A null adjusted close
falls back to the unadjusted close from the same bar. Exceptions and
all-non-finite frames are retried with backoff; a clean empty frame is not.

The cache stores price history as a list of JSON-friendly dicts so the
derived metrics (volatility, momentum) can be computed from cached data
without re-hitting Yahoo.
"""

from __future__ import annotations

import datetime as _dt
import math
import time
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

# Transient Yahoo / yfinance failures (HTTP 429, 5xx, or a chart body whose
# prices are all non-finite — the 2026-10-17 failure mode, where a null
# ``adjclose`` made every Close NaN under yfinance's default auto-adjust).
# A clean empty frame (unknown or delisted symbol) is not retried.
_PRICE_FETCH_ATTEMPTS = 3
_PRICE_FETCH_BACKOFF_S = (1.0, 2.0)


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


def _finite_float(value: Any) -> Optional[float]:
    """Return ``value`` as a finite float, or ``None`` for missing / NaN / inf."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _sleep_backoff(failed_attempt: int) -> None:
    """Sleep before retry ``failed_attempt`` (0-based). Tests zero the delays."""
    if failed_attempt < 0 or failed_attempt >= len(_PRICE_FETCH_BACKOFF_S):
        return
    delay = _PRICE_FETCH_BACKOFF_S[failed_attempt]
    if delay:
        time.sleep(delay)


def _row_to_dict(date_str: str, row: Any) -> Optional[Dict[str, Any]]:
    """Coerce a yfinance row into a JSON-serialisable dict.

    Returns ``None`` when neither the adjusted nor the unadjusted close
    is a finite price, so a placeholder bar cannot poison momentum.
    A null adjusted close falls back to the unadjusted close from the
    same bar (a real Yahoo price, not a fill). Non-finite OHLC legs
    fall back to that chosen close; a non-finite volume becomes 0.
    """
    raw_close = _finite_float(row["Close"]) if "Close" in row.index else None
    if "Adj Close" in row.index:
        adj = _finite_float(row["Adj Close"])
    else:
        adj = None
    # The series the pipeline scores has historically been the adjusted
    # close (yfinance's default auto-adjust wrote it into ``Close``).
    # Prefer a finite adjusted close; otherwise keep the finite raw close.
    chosen = adj if adj is not None else raw_close
    if chosen is None:
        return None
    close_out = raw_close if raw_close is not None else chosen
    adj_out = adj if adj is not None else chosen

    def _px(name: str) -> float:
        if name not in row.index:
            return chosen
        f = _finite_float(row[name])
        return chosen if f is None else f

    vol = _finite_float(row["Volume"]) if "Volume" in row.index else None
    return {
        "date": date_str,
        "open": _px("Open"),
        "high": _px("High"),
        "low": _px("Low"),
        "close": close_out,
        "adj_close": adj_out,
        "volume": int(vol) if vol is not None else 0,
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


def _frame_to_rows(df: Any) -> Optional[List[Dict[str, Any]]]:
    """Normalize a yfinance frame.

    Returns ``[]`` for an empty frame, a list of finite bars when at
    least one close is usable, or ``None`` when the frame had bars but
    every close was non-finite (worth a retry). A null adjusted close
    with a finite raw close is usable and is not a retry.
    """
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
        parsed = _row_to_dict(date_str, row)
        if parsed is not None:
            rows.append(parsed)
    if not rows:
        return None
    return rows


def _fetch_prices_once(ticker: str, period: str) -> Optional[List[Dict[str, Any]]]:
    """One pass over symbol variants.

    ``None`` means every non-empty frame had only non-finite closes.
    ``[]`` means Yahoo had no bars. A non-empty list is success.
    Exceptions propagate so the caller can retry them.
    """
    saw_nonfinite = False
    for variant in _yahoo_symbol_variants(ticker):
        _bucket.acquire()
        t = yf.Ticker(variant)
        try:
            # auto_adjust=False keeps the raw Close when Adj Close is
            # null. yfinance's default auto-adjust replaces Close with
            # Adj Close, so a null adjclose turns the whole series into
            # NaN and the 90-day return collapses.
            df = t.history(period=period, auto_adjust=False)
        except Exception:
            _api_counter.bump("yahoo", "error")
            raise
        parsed = _frame_to_rows(df)
        if parsed is None:
            _api_counter.bump("yahoo", "error")
            saw_nonfinite = True
            continue
        if not parsed:
            _api_counter.bump("yahoo", "error")
            continue
        _api_counter.bump("yahoo", "ok")
        return parsed
    if saw_nonfinite:
        return None
    return []


def _fetch_prices_from_yahoo(ticker: str, period: str) -> List[Dict[str, Any]]:
    """Hit yfinance (subject to the rate limiter) and normalize the result.

    For tickers containing a ``.`` (e.g. ``BRK.B``) we try the hyphen
    variant (``BRK-B``) first — Yahoo's symbol convention uses hyphens
    for class shares — and fall back to the dot form if it returns no
    rows.

    Exceptions and all-non-finite frames are retried with backoff.
    A clean empty response is returned immediately.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(_PRICE_FETCH_ATTEMPTS):
        try:
            parsed = _fetch_prices_once(ticker, period)
        except Exception as exc:
            last_exc = exc
            parsed = None
        else:
            last_exc = None
        if parsed:
            return parsed
        if parsed == []:
            return []
        if attempt + 1 < _PRICE_FETCH_ATTEMPTS:
            _sleep_backoff(attempt)
    if last_exc is not None:
        raise last_exc
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


def _bar_close(bar: Dict[str, Any]) -> Optional[float]:
    """Finite price for return math: adjusted close, else raw close."""
    for key in ("adj_close", "close"):
        f = _finite_float(bar.get(key))
        if f is not None:
            return f
    return None


def _closes(prices: List[Dict[str, Any]]) -> List[float]:
    out: List[float] = []
    for bar in prices:
        f = _bar_close(bar)
        if f is not None:
            out.append(f)
    return out


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
    annualized = daily_std * math.sqrt(_TRADING_DAYS)
    if not math.isfinite(annualized):
        return None
    return annualized


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
    result = last / past - 1.0
    if not math.isfinite(result):
        return None
    return result
