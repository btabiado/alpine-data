"""FRED Tier-2 macro snapshot (audit-deferred indicators).

Pulls the six Tier-2 macro indicators that the DES audit framework
identified as lower-marginal-value than the Tier-1 inputs (CPI, Fed
Funds, 10Y, real 10Y, VIX, M2, U-3 unemployment, WTI) but still useful
for completeness:

    * ``DCOILBRENTEU`` — Brent crude (USD/bbl, daily)
    * ``GASREGW``       — US regular gasoline retail price (USD/gal, weekly)
    * ``INDPRO``        — Industrial Production: Total Index (ISM PMI proxy,
                          monthly). The traditional ISM Manufacturing PMI
                          (``NAPM``, ``NAPMNOI`` etc.) is retired on FRED;
                          INDPRO is the cleanest modern, free, monthly
                          proxy for industrial / manufacturing activity.
    * ``HOUST``         — Housing Starts: Total, thousands of units (monthly)
    * ``UMCSENT``       — University of Michigan Consumer Sentiment
                          (monthly). Used as the Consumer Confidence proxy
                          since the Conference Board CCI is paywalled.
    * ``U6RATE``        — U-6 broad unemployment rate (monthly), captures
                          marginally attached + part-time-for-economic-
                          reasons slack that U-3 misses.

This module ONLY reads from FRED (no EIA dependency).  The gasoline
"crack spread" is derived inside the snapshot dict as
``gasoline_retail.current - 0.42 * brent_crude.current`` and exposed on
the gasoline block for downstream consumption.

Public API::

    fetch_tier2_macro_snapshot(as_of=None, cache_dir=None) -> dict

Returns a flat dict, one block per indicator, plus ``as_of`` and
``data_quality``.  Each block has a stable shape and always exists in
the snapshot — failed sources resolve to ``None`` so callers can index
without ``KeyError``.

Conventions follow :mod:`lthcs.sources.fred_breadth`:

    * Lazy ``FRED_API_KEY`` read (import-safe without the key).
    * 1h snapshot cache (raw fred source has its own 24h cache).
    * Defensive: a single failed series never raises; counts toward
      ``data_quality.sources_failed``.
"""

from __future__ import annotations

import datetime as _dt
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from lthcs.sources import fred
from lthcs.sources._cache import FileCache
from lthcs.sources._ratelimit import TokenBucket

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# 1h snapshot cache TTL; underlying fred raw-series cache is 24h.
_SNAPSHOT_CACHE_TTL = 60 * 60

# FRED series IDs (verified live 2026-05; see module docstring for the
# ISM/PMI proxy rationale).
SERIES_BRENT = "DCOILBRENTEU"
SERIES_GASOLINE = "GASREGW"
SERIES_INDPRO = "INDPRO"
SERIES_HOUSING_STARTS = "HOUST"
SERIES_CONSUMER_SENTIMENT = "UMCSENT"
SERIES_U6 = "U6RATE"

# 2-year percentile window in calendar days. 730 ~ 2 years (matches
# fred_breadth.py convention).
_PERCENTILE_WINDOW_DAYS = 730

# ISM-style expansion / contraction boundary (PMI convention: 50 is
# neutral; INDPRO doesn't have the same scale, so we apply the regime
# label on a normalized 3m-momentum basis — see _classify_ism_regime).
ISM_PMI_EXPANSION_THRESHOLD = 50.0
ISM_PMI_NEUTRAL = 50.0

# Crack spread proxy: $/gallon = gasoline_retail($/gal) - 0.42 * brent($/bbl).
# 42 gallons per barrel; dividing crude $/bbl by 100 gives an approximate
# per-gallon refined-product input cost. The 0.42 factor is the
# documented rule of thumb the audit framework calls out.
CRACK_SPREAD_CRUDE_FACTOR = 0.42

logger = logging.getLogger(__name__)

# Module-level cache + bucket.  Same rate (5 req/sec, burst 20) as
# fred_breadth since we hit the same upstream.
_cache = FileCache("fred_tier2")
_bucket = TokenBucket(capacity=20, refill_rate=5.0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _today_iso() -> str:
    return _dt.date.today().isoformat()


def _non_null_observations(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop observations whose value is ``None`` (FRED ``.`` markers)."""
    out: List[Dict[str, Any]] = []
    for r in rows or []:
        if r is None:
            continue
        if r.get("value") is None:
            continue
        out.append(r)
    return out


def _safe_fetch(
    series_id: str, as_of: Optional[str] = None
) -> List[Dict[str, Any]]:
    """``fred.get_series`` wrapped in best-effort error handling.

    Returns ``[]`` on any failure and logs at WARNING.  We never re-raise;
    Tier-2 data is additive and a single failed series must not break
    the snapshot.
    """
    try:
        return _non_null_observations(fred.get_series(series_id, as_of=as_of))
    except Exception as exc:  # noqa: BLE001
        logger.warning("fred_tier2: failed to fetch %s: %s", series_id, exc)
        return []


def _latest_value(rows: List[Dict[str, Any]]) -> Optional[float]:
    if not rows:
        return None
    try:
        return float(rows[-1]["value"])
    except (TypeError, ValueError, KeyError):
        return None


def _value_at_or_before(
    rows: List[Dict[str, Any]], anchor: _dt.date
) -> Optional[float]:
    """Most recent observation whose date is on or before ``anchor``.

    Used to find the value ~3 months prior for the change_3m_pct calc.
    """
    cutoff = anchor.isoformat()
    for r in reversed(rows):
        if str(r.get("date") or "") <= cutoff and r.get("value") is not None:
            try:
                return float(r["value"])
            except (TypeError, ValueError):
                continue
    return None


def _change_3m_pct(
    rows: List[Dict[str, Any]],
    latest: Optional[float],
    anchor: Optional[_dt.date] = None,
) -> Optional[float]:
    """Trailing 3-month percent change as a decimal (0.05 == +5%).

    Picks the latest observation as ``latest`` and the most recent
    observation on/before (anchor - 90 days) as the lookback.  Returns
    ``None`` if either is missing or the prior value is zero.

    The ``anchor`` defaults to the latest observation date in ``rows``
    when not supplied — this is the natural choice both for live and
    historical as_of snapshots.
    """
    if latest is None or not rows:
        return None
    if anchor is None:
        try:
            anchor = _dt.date.fromisoformat(str(rows[-1]["date"]))
        except (TypeError, ValueError, KeyError):
            return None
    prior_anchor = anchor - _dt.timedelta(days=90)
    prior = _value_at_or_before(rows, prior_anchor)
    if prior is None or prior == 0:
        return None
    return round((latest - prior) / abs(prior), 6)


def _change_3m_bp(
    rows: List[Dict[str, Any]],
    latest: Optional[float],
    anchor: Optional[_dt.date] = None,
) -> Optional[float]:
    """Trailing 3-month change in basis points (for rate-like series).

    Convention: 1 percentage-point move = 100 bp.  Used for U-6 where
    the absolute change is more interpretable than a pct change off a
    small base.
    """
    if latest is None or not rows:
        return None
    if anchor is None:
        try:
            anchor = _dt.date.fromisoformat(str(rows[-1]["date"]))
        except (TypeError, ValueError, KeyError):
            return None
    prior_anchor = anchor - _dt.timedelta(days=90)
    prior = _value_at_or_before(rows, prior_anchor)
    if prior is None:
        return None
    return round((latest - prior) * 100.0, 2)


def _percentile_in_window(
    rows: List[Dict[str, Any]],
    value: float,
    window_days: int = _PERCENTILE_WINDOW_DAYS,
    anchor: Optional[_dt.date] = None,
) -> Optional[float]:
    """Rank ``value`` within the trailing-``window_days`` distribution.

    Returns a float in [0.0, 1.0] (0 = lowest, 1 = highest), or ``None``
    if the window is empty.  Mirrors fred_breadth's percentile semantics.
    """
    if not rows:
        return None
    right_edge = anchor if anchor is not None else _dt.date.today()
    cutoff = (right_edge - _dt.timedelta(days=window_days)).isoformat()
    window: List[float] = []
    for r in rows:
        if str(r.get("date") or "") < cutoff:
            continue
        v = r.get("value")
        if v is None:
            continue
        try:
            window.append(float(v))
        except (TypeError, ValueError):
            continue
    if not window:
        return None
    leq = sum(1 for v in window if v <= value)
    return leq / len(window)


# ---------------------------------------------------------------------------
# Per-series builders
# ---------------------------------------------------------------------------


def _build_price_block(
    rows: List[Dict[str, Any]], anchor: Optional[_dt.date] = None
) -> Optional[Dict[str, Any]]:
    """Standard block for price-like series (Brent, gasoline).

    Returns ``{current, change_3m_pct, percentile_2y}`` or ``None`` if
    the series has no usable observations.
    """
    if not rows:
        return None
    latest = _latest_value(rows)
    if latest is None:
        return None
    change_pct = _change_3m_pct(rows, latest, anchor=anchor)
    pct = _percentile_in_window(rows, latest, anchor=anchor)
    return {
        "current": round(latest, 4),
        "change_3m_pct": change_pct,
        "percentile_2y": pct,
    }


def _classify_ism_regime(
    rows: List[Dict[str, Any]], change_3m_pct: Optional[float]
) -> str:
    """Map INDPRO 3-month momentum to an ISM-style regime label.

    Convention (matches PMI <50/=50/>50 buckets, applied to INDPRO 3m
    momentum because INDPRO levels are an index ~100, not 50):

      * "expansion"  : 3m momentum > +0.5%
      * "contraction": 3m momentum < -0.5%
      * "neutral"    : in between (or unknown)
    """
    if change_3m_pct is None:
        return "neutral"
    if change_3m_pct > 0.005:
        return "expansion"
    if change_3m_pct < -0.005:
        return "contraction"
    return "neutral"


def _build_ism_block(
    rows: List[Dict[str, Any]], anchor: Optional[_dt.date] = None
) -> Optional[Dict[str, Any]]:
    """Build the ISM PMI proxy block from INDPRO.

    Shape: ``{current, change_3m_pct, regime, percentile_2y}``.  Note
    we expose ``regime`` instead of a raw 50-pivot threshold because
    INDPRO is an index ~100, not a PMI ~50.
    """
    if not rows:
        return None
    latest = _latest_value(rows)
    if latest is None:
        return None
    change_pct = _change_3m_pct(rows, latest, anchor=anchor)
    pct = _percentile_in_window(rows, latest, anchor=anchor)
    regime = _classify_ism_regime(rows, change_pct)
    return {
        "current": round(latest, 4),
        "change_3m_pct": change_pct,
        "regime": regime,
        "percentile_2y": pct,
    }


def _build_rate_block(
    rows: List[Dict[str, Any]], anchor: Optional[_dt.date] = None
) -> Optional[Dict[str, Any]]:
    """Standard block for rate-like series (U-6).

    Uses bp change (1pp = 100bp) over the trailing 3m, plus a 2y
    percentile of the absolute level.  Shape:
    ``{current, change_3m_bp, percentile_2y}``.
    """
    if not rows:
        return None
    latest = _latest_value(rows)
    if latest is None:
        return None
    change_bp = _change_3m_bp(rows, latest, anchor=anchor)
    pct = _percentile_in_window(rows, latest, anchor=anchor)
    return {
        "current": round(latest, 4),
        "change_3m_bp": change_bp,
        "percentile_2y": pct,
    }


def _gasoline_crack_spread(
    brent_block: Optional[Dict[str, Any]],
    gasoline_block: Optional[Dict[str, Any]],
) -> Optional[float]:
    """Per-gallon crack spread proxy = gasoline - 0.42 * brent.

    Returns ``None`` if either input is missing.  Surfaces inside the
    gasoline block (so downstream callers don't have to recompute).
    """
    if not brent_block or not gasoline_block:
        return None
    g = gasoline_block.get("current")
    b = brent_block.get("current")
    if g is None or b is None:
        return None
    try:
        return round(float(g) - CRACK_SPREAD_CRUDE_FACTOR * float(b), 4)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_tier2_macro_snapshot(
    as_of: Optional[str] = None, cache_dir: Optional[Path] = None
) -> Dict[str, Any]:
    """Build the Tier-2 macro snapshot.

    Returns a dict with one block per indicator
    (``brent_crude``, ``gasoline_retail``, ``ism_pmi_proxy``,
    ``housing_starts``, ``consumer_sentiment``, ``u6_unemployment``)
    plus ``as_of`` and ``data_quality``.

    Every block is either a dict with the documented shape or ``None``.
    Missing values (or whole missing series) never raise — they count
    toward ``data_quality.sources_failed`` so the caller can decide
    whether to apply a confidence haircut.

    Args:
        as_of: ISO ``YYYY-MM-DD``.  When provided, the snapshot reflects
            the state of every series as it would have looked on that
            date (forwarded to ``fred.get_series(as_of=...)`` which
            already supports the as-of axis).  The 2y percentile window
            and the 3m change anchor follow ``as_of`` as well.
        cache_dir: optional override for the snapshot cache root.  The
            underlying ``fred`` 24h raw-series cache is unaffected.
    """
    cache = _cache
    if cache_dir is not None:
        cache = FileCache("fred_tier2", root=Path(cache_dir))

    snapshot_date = as_of if as_of is not None else _today_iso()
    cache_key = f"tier2/{snapshot_date}"
    hit = cache.get(cache_key)
    if hit is not None and isinstance(hit.value, dict):
        return dict(hit.value)

    # Soft rate-limit at the assembly level.  Each fred.get_series call
    # already goes through fred._bucket.
    try:
        _bucket.acquire()
    except Exception:  # pragma: no cover
        pass

    brent_rows = _safe_fetch(SERIES_BRENT, as_of=as_of)
    gasoline_rows = _safe_fetch(SERIES_GASOLINE, as_of=as_of)
    indpro_rows = _safe_fetch(SERIES_INDPRO, as_of=as_of)
    housing_rows = _safe_fetch(SERIES_HOUSING_STARTS, as_of=as_of)
    sentiment_rows = _safe_fetch(SERIES_CONSUMER_SENTIMENT, as_of=as_of)
    u6_rows = _safe_fetch(SERIES_U6, as_of=as_of)

    # Anchor for the 2y window + 3m lookback.  When historical, prefer
    # the as_of date itself so percentiles stay relative to that day.
    anchor: Optional[_dt.date]
    if as_of is not None:
        try:
            anchor = _dt.date.fromisoformat(as_of)
        except ValueError:
            anchor = None
    else:
        anchor = None

    brent_block = _build_price_block(brent_rows, anchor=anchor)
    gasoline_block = _build_price_block(gasoline_rows, anchor=anchor)
    ism_block = _build_ism_block(indpro_rows, anchor=anchor)
    housing_block = _build_price_block(housing_rows, anchor=anchor)
    sentiment_block = _build_price_block(sentiment_rows, anchor=anchor)
    u6_block = _build_rate_block(u6_rows, anchor=anchor)

    # Crack spread: surface on the gasoline block (so downstream can
    # pull "the gasoline indicator" and get both retail price and spread
    # without two lookups).
    if gasoline_block is not None:
        gasoline_block["crack_spread_per_gal"] = _gasoline_crack_spread(
            brent_block, gasoline_block
        )

    blocks_and_names: List[Tuple[str, Optional[Dict[str, Any]]]] = [
        ("brent_crude", brent_block),
        ("gasoline_retail", gasoline_block),
        ("ism_pmi_proxy", ism_block),
        ("housing_starts", housing_block),
        ("consumer_sentiment", sentiment_block),
        ("u6_unemployment", u6_block),
    ]
    ok = sum(1 for _, b in blocks_and_names if b is not None)
    failed_names = [name for name, b in blocks_and_names if b is None]

    snapshot: Dict[str, Any] = {
        "as_of": snapshot_date,
        "brent_crude": brent_block,
        "gasoline_retail": gasoline_block,
        "ism_pmi_proxy": ism_block,
        "housing_starts": housing_block,
        "consumer_sentiment": sentiment_block,
        "u6_unemployment": u6_block,
        "data_quality": {
            "sources_ok": ok,
            "sources_failed": len(failed_names),
            "failed_sources": failed_names,
        },
    }

    try:
        cache.set(cache_key, snapshot, ttl_seconds=_SNAPSHOT_CACHE_TTL)
    except Exception:  # pragma: no cover
        pass

    return snapshot


__all__ = [
    "SERIES_BRENT",
    "SERIES_GASOLINE",
    "SERIES_INDPRO",
    "SERIES_HOUSING_STARTS",
    "SERIES_CONSUMER_SENTIMENT",
    "SERIES_U6",
    "ISM_PMI_EXPANSION_THRESHOLD",
    "ISM_PMI_NEUTRAL",
    "CRACK_SPREAD_CRUDE_FACTOR",
    "fetch_tier2_macro_snapshot",
]
