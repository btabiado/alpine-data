"""FRED market-breadth / regime snapshot.

Pulls four FRED series that, together, characterise the current macro
risk regime — used by LTHCS as a top-down filter on top of the per-name
fundamentals signals:

    * ``BAMLH0A0HYM2`` — ICE BofA US High Yield Index Option-Adjusted
      Spread.  Best leading indicator of credit-market stress.
    * ``BAMLC0A0CM``   — ICE BofA US Corporate (Investment Grade) OAS.
    * ``T10Y2Y``       — 10-Year minus 2-Year Treasury yield (curve
      shape; inversion historically precedes recession).
    * ``DTWEXBGS``     — Trade-Weighted Broad Dollar Index (Goods +
      Services).  DXY-equivalent proxy; a strong dollar is a drag on
      large multinationals.

Public API::

    fetch_breadth_snapshot(cache_dir: Path | None = None) -> dict

Returns a single flat dict describing the current level, 30-day change,
2-year percentile, and three regime flags.  Any individual series that
fails to fetch / has insufficient history is reported as ``None`` with a
matching bump to ``data_quality.sources_failed`` — this is an *additive*
input and a single 5xx must never break the daily pipeline.

Conventions inherited from :mod:`lthcs.sources.fred`:

    * Lazy ``FRED_API_KEY`` read (import is safe without the key set).
    * Shared :class:`FileCache("fred_breadth")` for the assembled
      snapshot (1h TTL).  Upstream series fetches reuse the existing
      ``fred`` source cache transparently via ``get_series``.
    * Shared :class:`TokenBucket` (5 req/sec, burst 20) — same rate as
      ``fred.py`` since we hit the same upstream.
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

# Snapshot cache TTL: 1h.  The underlying ``fred`` source has its own
# 24h cache; we keep this layer short so a re-run picks up newly-posted
# observations within the day without re-hitting the FRED endpoint.
_SNAPSHOT_CACHE_TTL = 60 * 60

# FRED series IDs.
SERIES_HY_OAS = "BAMLH0A0HYM2"
SERIES_IG_OAS = "BAMLC0A0CM"
SERIES_2S10S = "T10Y2Y"
SERIES_BROAD_DOLLAR = "DTWEXBGS"

# Regime-flag thresholds (documented in the snapshot output and unit-tested).
HY_STRESS_BP_30D = 50.0          # 30d-Δ in HY OAS > +50bp => hy_stress
DOLLAR_STRONG_PCT_30D = 0.02     # 30d-Δ in DXY > +2% => dollar_strong

# 2-year percentile window in calendar days.  504 trading days is close
# enough to 2 years for this purpose and matches conventional usage.
_PERCENTILE_WINDOW_DAYS = 730

# Approx number of trading-day observations in 30 calendar days.  HY/IG
# OAS and broad dollar are reported on business days; for the percentile
# window we just take all observations in the trailing 2y.
_LOOKBACK_30D = 21

logger = logging.getLogger(__name__)

# Module-level singletons.  The snapshot-layer cache is separate from the
# raw-series cache that ``fred.py`` owns.
_cache = FileCache("fred_breadth")
_bucket = TokenBucket(capacity=20, refill_rate=5.0)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _today_iso() -> str:
    return _dt.date.today().isoformat()


def _non_null_observations(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop observations whose value is ``None`` (FRED ``"."`` markers)."""
    out: List[Dict[str, Any]] = []
    for r in rows or []:
        if r is None:
            continue
        if r.get("value") is None:
            continue
        out.append(r)
    return out


def _latest_and_lookback(
    rows: List[Dict[str, Any]], lookback_obs: int = _LOOKBACK_30D
) -> Tuple[Optional[float], Optional[float]]:
    """Return ``(latest_value, value_lookback_obs_ago)``.

    Both as floats. Either can be ``None`` if there isn't enough data.
    Operates on the already-filtered non-null list.
    """
    if not rows:
        return None, None
    latest = float(rows[-1]["value"])
    if len(rows) <= lookback_obs:
        return latest, None
    past = float(rows[-1 - lookback_obs]["value"])
    return latest, past


def _percentile_in_window(
    rows: List[Dict[str, Any]],
    value: float,
    window_days: int = _PERCENTILE_WINDOW_DAYS,
) -> Optional[float]:
    """Where does ``value`` sit in the trailing ``window_days`` distribution?

    Returns a float in [0.0, 1.0] (0 = lowest, 1 = highest) or ``None``
    if the window is empty.  Uses the ``<= value`` rank convention so the
    most recent observation always has a well-defined rank within its own
    distribution.
    """
    if not rows:
        return None
    today = _dt.date.today()
    cutoff = (today - _dt.timedelta(days=window_days)).isoformat()
    window = [float(r["value"]) for r in rows if str(r.get("date") or "") >= cutoff]
    if not window:
        return None
    leq = sum(1 for v in window if v <= value)
    return leq / len(window)


def _safe_fetch(series_id: str) -> List[Dict[str, Any]]:
    """``fred.get_series`` wrapped in best-effort error handling.

    Returns ``[]`` on any failure and logs at WARNING.  We never re-raise
    because regime data is additive — a single failed series should not
    break the entire snapshot.
    """
    try:
        return _non_null_observations(fred.get_series(series_id))
    except Exception as exc:  # noqa: BLE001  (deliberate broad except)
        logger.warning("fred_breadth: failed to fetch %s: %s", series_id, exc)
        return []


# ---------------------------------------------------------------------------
# Per-series builders
# ---------------------------------------------------------------------------


def _build_oas_block(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """OAS series share a shape: ``current`` in percent, ``Δ`` in **basis
    points**, ``percentile_2y`` over the trailing window.
    """
    if not rows:
        return None
    latest, past = _latest_and_lookback(rows)
    if latest is None:
        return None
    # OAS series are quoted in percent (e.g. 3.42 == 342bp).  Δ in bp.
    change_bp: Optional[float]
    if past is None:
        change_bp = None
    else:
        change_bp = round((latest - past) * 100.0, 2)
    pct = _percentile_in_window(rows, latest)
    return {
        "current": round(latest, 4),
        "change_30d_bp": change_bp,
        "percentile_2y": pct,
    }


def _build_curve_block(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """2s10s yield curve: ``current`` in percent, ``inverted`` if < 0,
    Δ in basis points.  We deliberately don't emit a 2y percentile here —
    the curve's regime is captured by the inversion flag and we want to
    keep the dict narrow.
    """
    if not rows:
        return None
    latest, past = _latest_and_lookback(rows)
    if latest is None:
        return None
    change_bp = None if past is None else round((latest - past) * 100.0, 2)
    return {
        "current": round(latest, 4),
        "inverted": latest < 0.0,
        "change_30d_bp": change_bp,
    }


def _build_dollar_block(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Broad dollar index: ``current`` index level, ``change_30d_pct`` as
    a decimal (0.012 == +1.2%), ``percentile_2y`` over trailing window.
    """
    if not rows:
        return None
    latest, past = _latest_and_lookback(rows)
    if latest is None:
        return None
    if past is None or past == 0:
        change_pct = None
    else:
        change_pct = round((latest - past) / past, 6)
    pct = _percentile_in_window(rows, latest)
    return {
        "current": round(latest, 4),
        "change_30d_pct": change_pct,
        "percentile_2y": pct,
    }


# ---------------------------------------------------------------------------
# Regime flags
# ---------------------------------------------------------------------------


def _regime_flags(
    hy_block: Optional[Dict[str, Any]],
    curve_block: Optional[Dict[str, Any]],
    dollar_block: Optional[Dict[str, Any]],
) -> Dict[str, bool]:
    """Boolean regime flags derived from the per-series blocks.

    Each flag defaults to ``False`` (not flagged) when the underlying
    series is missing — we intentionally do *not* go ``None`` here so
    downstream composition code can branch on bools without three-way
    logic.
    """
    hy_stress = False
    if hy_block is not None:
        bp = hy_block.get("change_30d_bp")
        if bp is not None and bp > HY_STRESS_BP_30D:
            hy_stress = True

    curve_inverted = False
    if curve_block is not None:
        curve_inverted = bool(curve_block.get("inverted"))

    dollar_strong = False
    if dollar_block is not None:
        pct = dollar_block.get("change_30d_pct")
        if pct is not None and pct > DOLLAR_STRONG_PCT_30D:
            dollar_strong = True

    return {
        "hy_stress": hy_stress,
        "curve_inverted": curve_inverted,
        "dollar_strong": dollar_strong,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_breadth_snapshot(cache_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Build today's FRED breadth / regime snapshot.

    ``cache_dir`` is accepted for parity with other source modules; when
    provided it overrides the module-level snapshot cache root for the
    duration of this call.  The underlying ``fred`` source's own 24h
    cache is not affected.

    Always returns a dict — never raises.  Missing series degrade
    gracefully to ``None`` and are counted in ``data_quality``.
    """
    cache = _cache
    if cache_dir is not None:
        cache = FileCache("fred_breadth", root=Path(cache_dir))

    today = _today_iso()
    cache_key = f"breadth/{today}"
    hit = cache.get(cache_key)
    if hit is not None and isinstance(hit.value, dict):
        return dict(hit.value)

    # Soft-rate-limit the *assembly* call as well, in case a caller hammers
    # this in a loop.  Each individual ``fred.get_series`` already goes
    # through ``fred._bucket``.
    try:
        _bucket.acquire()
    except Exception:  # pragma: no cover  (defensive only)
        pass

    hy_rows = _safe_fetch(SERIES_HY_OAS)
    ig_rows = _safe_fetch(SERIES_IG_OAS)
    curve_rows = _safe_fetch(SERIES_2S10S)
    dollar_rows = _safe_fetch(SERIES_BROAD_DOLLAR)

    hy_block = _build_oas_block(hy_rows)
    ig_block = _build_oas_block(ig_rows)
    curve_block = _build_curve_block(curve_rows)
    dollar_block = _build_dollar_block(dollar_rows)

    blocks = [hy_block, ig_block, curve_block, dollar_block]
    ok = sum(1 for b in blocks if b is not None)
    failed = len(blocks) - ok

    snapshot: Dict[str, Any] = {
        "as_of": today,
        "hy_oas": hy_block,
        "ig_oas": ig_block,
        "yield_curve_2s10s": curve_block,
        "broad_dollar": dollar_block,
        "regime_flags": _regime_flags(hy_block, curve_block, dollar_block),
        "data_quality": {"sources_ok": ok, "sources_failed": failed},
    }

    try:
        cache.set(cache_key, snapshot, ttl_seconds=_SNAPSHOT_CACHE_TTL)
    except Exception:  # pragma: no cover
        # Cache failure must never break the call path.
        pass

    return snapshot


__all__ = [
    "SERIES_HY_OAS",
    "SERIES_IG_OAS",
    "SERIES_2S10S",
    "SERIES_BROAD_DOLLAR",
    "HY_STRESS_BP_30D",
    "DOLLAR_STRONG_PCT_30D",
    "fetch_breadth_snapshot",
]
