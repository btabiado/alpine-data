"""Sector ETF relative-strength snapshot.

Computes 1-month and 3-month total-return relative strength for the 11
S&P-sector SPDR ETFs against the SPY benchmark.  Used by the LTHCS
Thesis pillar as a top-down sector tailwind / headwind signal: a stock
in a sector trading well above SPY is in a regime the market is paying
up for, and vice-versa.

Public API::

    fetch_sector_strength(cache_dir: Path | None = None) -> dict
    get_sector_relative_strength(ticker_sector: str, snapshot: dict) -> dict | None

Plus the ``SECTOR_TO_ETF`` mapping for the yfinance sector strings
yfinance returns on ``Ticker.info["sector"]``.

Implementation notes:

* Returns are computed off the trailing 21 / 63 trading-day window of
  closes (~1mo / ~3mo).  We use ``adj_close`` so dividends are
  accounted for — sector ETFs pay non-trivial yields and a price-only
  comparison would be biased.
* If SPY itself fails to fetch we cannot compute relative strength and
  the snapshot's ``sectors`` map is empty (benchmarks are non-optional).
* Individual sector ETF failures degrade gracefully: that sector is
  simply absent from the ``sectors`` map.
* Caching: shared :class:`FileCache("sector_etf")` at the snapshot
  layer (1h TTL).  The per-ticker price fetches go through the
  existing 24h ``yahoo`` cache.
"""

from __future__ import annotations

import datetime as _dt
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from lthcs.sources import yahoo
from lthcs.sources._cache import FileCache

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# 1h snapshot cache.  Underlying ``yahoo`` source has its own 24h cache
# for raw daily prices.
_SNAPSHOT_CACHE_TTL = 60 * 60

# Trading-day windows for the 1mo / 3mo total-return computation.
_TRADING_DAYS_1M = 21
_TRADING_DAYS_3M = 63

BENCHMARK_TICKER = "SPY"

# 11 SPDR sector ETFs (Select Sector SPDRs).  Order matters only for
# deterministic ranking ties and the docstring above; ``fetch_sector_strength``
# always returns a stable, alphabetically-ordered ``sectors`` dict because
# Python 3.7+ preserves insertion order and we insert in sorted order.
SECTOR_ETFS: Dict[str, str] = {
    "XLK": "Technology",
    "XLF": "Financials",
    "XLE": "Energy",
    "XLI": "Industrials",
    "XLY": "Consumer Cyclical",
    "XLP": "Consumer Defensive",
    "XLV": "Healthcare",
    "XLB": "Basic Materials",
    "XLU": "Utilities",
    "XLRE": "Real Estate",
    "XLC": "Communication Services",
}

# Mapping from a yfinance ``Ticker.info["sector"]`` string (and common
# aliases / GICS names) to the SPDR ETF that proxies it.  Lookups are
# done case-insensitively in ``get_sector_relative_strength``.
SECTOR_TO_ETF: Dict[str, str] = {
    "Technology": "XLK",
    "Information Technology": "XLK",
    "Financials": "XLF",
    "Financial Services": "XLF",
    "Energy": "XLE",
    "Industrials": "XLI",
    "Consumer Cyclical": "XLY",
    "Consumer Discretionary": "XLY",
    "Consumer Defensive": "XLP",
    "Consumer Staples": "XLP",
    "Healthcare": "XLV",
    "Basic Materials": "XLB",
    "Materials": "XLB",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Communication Services": "XLC",
}

logger = logging.getLogger(__name__)

# Module-level cache singleton for the assembled snapshot.
_cache = FileCache("sector_etf")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _today_iso() -> str:
    return _dt.date.today().isoformat()


def _normalize_as_of(as_of: Optional[str]) -> Optional[str]:
    """Coerce an ``as_of`` argument to ISO ``YYYY-MM-DD`` or ``None``.

    Invalid / empty inputs silently degrade to ``None`` (i.e. "today").
    """
    if as_of is None:
        return None
    if not isinstance(as_of, str) or not as_of.strip():
        return None
    try:
        return _dt.date.fromisoformat(as_of.strip()).isoformat()
    except ValueError:
        return None


def _adj_closes(prices: List[Dict[str, Any]]) -> List[float]:
    """Pull the adjusted-close series out of yahoo's row dicts.

    Falls back to ``close`` if ``adj_close`` is missing for any row.
    """
    out: List[float] = []
    for r in prices or []:
        v = r.get("adj_close")
        if v is None:
            v = r.get("close")
        if v is None:
            continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


def _trailing_return(closes: List[float], n: int) -> Optional[float]:
    """Total return over the trailing ``n`` trading-day bars.

    Requires ``n + 1`` closes (n returns from n+1 prices); returns ``None``
    if there aren't enough bars or the lookback close is zero.
    """
    if n <= 0:
        return None
    if len(closes) < n + 1:
        return None
    last = closes[-1]
    past = closes[-(n + 1)]
    if past == 0:
        return None
    return last / past - 1.0


def _safe_fetch_closes(ticker: str, as_of: Optional[str] = None) -> List[float]:
    """``yahoo.get_daily_prices`` -> adj-close list, with all errors logged
    and converted to an empty list.  Non-fatal for the snapshot.

    Passing ``as_of`` slices the returned series to bars on or before
    that ISO date.
    """
    try:
        prices = yahoo.get_daily_prices(ticker, period="6mo", as_of=as_of)
    except Exception as exc:  # noqa: BLE001
        logger.warning("sector_etf: failed to fetch %s prices: %s", ticker, exc)
        return []
    return _adj_closes(prices)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_sector_strength(
    cache_dir: Optional[Path] = None,
    as_of: Optional[str] = None,
) -> Dict[str, Any]:
    """Build today's (or as-of) sector-ETF relative-strength snapshot.

    Output shape (see module docstring + spec)::

        {
          "as_of": "YYYY-MM-DD",
          "benchmark_return_1m": float | None,
          "benchmark_return_3m": float | None,
          "sectors": {
             "XLK": {
                "sector_name": "Technology",
                "return_1m": float, "return_3m": float,
                "relative_1m": float, "relative_3m": float,
                "rank_1m": int, "rank_3m": int,
             },
             ...
          }
        }

    Always returns a dict; individual ETF failures are silently dropped
    from ``sectors``.  If SPY itself is unavailable the ``sectors`` map
    is empty (relative strength has no meaning without a benchmark).

    When ``as_of`` is supplied (ISO ``YYYY-MM-DD``) the 1m/3m returns end
    on the last trading bar at or before ``as_of`` rather than today, and
    the snapshot's ``as_of`` field reflects that date. Cache key includes
    ``as_of`` so historical snapshots don't collide with today's.
    """
    cache = _cache
    if cache_dir is not None:
        cache = FileCache("sector_etf", root=Path(cache_dir))

    normalised_as_of = _normalize_as_of(as_of)
    label = normalised_as_of if normalised_as_of else _today_iso()
    cache_key = f"sector_strength/{label}"
    hit = cache.get(cache_key)
    if hit is not None and isinstance(hit.value, dict):
        return dict(hit.value)

    spy_closes = _safe_fetch_closes(BENCHMARK_TICKER, as_of=normalised_as_of)
    bench_1m = _trailing_return(spy_closes, _TRADING_DAYS_1M)
    bench_3m = _trailing_return(spy_closes, _TRADING_DAYS_3M)

    sectors_out: Dict[str, Dict[str, Any]] = {}

    # If we can't anchor on SPY, relative strength is undefined.  Persist
    # a snapshot with empty sectors so callers can still inspect
    # ``benchmark_return_*`` (both will be None in this branch).
    if bench_1m is None and bench_3m is None:
        snapshot = {
            "as_of": label,
            "benchmark_return_1m": bench_1m,
            "benchmark_return_3m": bench_3m,
            "sectors": sectors_out,
        }
        try:
            cache.set(cache_key, snapshot, ttl_seconds=_SNAPSHOT_CACHE_TTL)
        except Exception:  # pragma: no cover
            pass
        return snapshot

    # Compute per-sector returns relative to the benchmark.  Sort the
    # ETF list alphabetically so the output dict has a stable iteration
    # order — easier to diff snapshots across days.
    raw: Dict[str, Dict[str, Any]] = {}
    for etf in sorted(SECTOR_ETFS.keys()):
        closes = _safe_fetch_closes(etf, as_of=normalised_as_of)
        r1 = _trailing_return(closes, _TRADING_DAYS_1M)
        r3 = _trailing_return(closes, _TRADING_DAYS_3M)
        # Drop the ETF entirely if both returns are unavailable — there's
        # nothing useful we can say about it.
        if r1 is None and r3 is None:
            continue

        rel_1m = None if (r1 is None or bench_1m is None) else r1 - bench_1m
        rel_3m = None if (r3 is None or bench_3m is None) else r3 - bench_3m
        raw[etf] = {
            "sector_name": SECTOR_ETFS[etf],
            "return_1m": r1,
            "return_3m": r3,
            "relative_1m": rel_1m,
            "relative_3m": rel_3m,
        }

    # Rank by relative strength.  Highest relative return = rank 1.
    # ETFs missing the relevant relative metric are unranked (rank=None).
    rel1_pairs = [(etf, blk["relative_1m"]) for etf, blk in raw.items()
                  if blk["relative_1m"] is not None]
    rel3_pairs = [(etf, blk["relative_3m"]) for etf, blk in raw.items()
                  if blk["relative_3m"] is not None]
    rel1_pairs.sort(key=lambda p: p[1], reverse=True)
    rel3_pairs.sort(key=lambda p: p[1], reverse=True)
    rank_1m = {etf: idx + 1 for idx, (etf, _) in enumerate(rel1_pairs)}
    rank_3m = {etf: idx + 1 for idx, (etf, _) in enumerate(rel3_pairs)}

    for etf, blk in raw.items():
        blk["rank_1m"] = rank_1m.get(etf)
        blk["rank_3m"] = rank_3m.get(etf)
        sectors_out[etf] = blk

    snapshot = {
        "as_of": label,
        "benchmark_return_1m": bench_1m,
        "benchmark_return_3m": bench_3m,
        "sectors": sectors_out,
    }
    try:
        cache.set(cache_key, snapshot, ttl_seconds=_SNAPSHOT_CACHE_TTL)
    except Exception:  # pragma: no cover
        pass
    return snapshot


def _resolve_sector_etf(ticker_sector: str) -> Optional[str]:
    """Case-insensitive lookup against ``SECTOR_TO_ETF``.

    yfinance has historically been inconsistent about casing
    (``"Technology"`` vs ``"technology"`` in some adapters), so we
    normalise on the way in rather than forcing callers to.
    """
    if not ticker_sector:
        return None
    target = ticker_sector.strip().lower()
    if not target:
        return None
    for name, etf in SECTOR_TO_ETF.items():
        if name.lower() == target:
            return etf
    return None


def get_sector_relative_strength(
    ticker_sector: str, snapshot: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Look up a stock's sector ETF and return its relative-strength block.

    Returns ``{'relative_1m': float|None, 'relative_3m': float|None,
    'rank_1m': int|None}`` if the sector resolves to a known SPDR ETF and
    that ETF is present in the snapshot's ``sectors`` map.  Returns
    ``None`` if the sector string is unknown, the snapshot is malformed,
    or the resolved ETF isn't in the snapshot (e.g. it failed to fetch).
    """
    if not isinstance(snapshot, dict):
        return None
    etf = _resolve_sector_etf(ticker_sector)
    if etf is None:
        return None
    sectors = snapshot.get("sectors") or {}
    if not isinstance(sectors, dict):
        return None
    blk = sectors.get(etf)
    if not isinstance(blk, dict):
        return None
    return {
        "relative_1m": blk.get("relative_1m"),
        "relative_3m": blk.get("relative_3m"),
        "rank_1m": blk.get("rank_1m"),
    }


__all__ = [
    "BENCHMARK_TICKER",
    "SECTOR_ETFS",
    "SECTOR_TO_ETF",
    "fetch_sector_strength",
    "get_sector_relative_strength",
]
