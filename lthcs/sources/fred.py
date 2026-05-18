"""FRED (Federal Reserve Economic Data) source client.

Pulls macro time series from the St. Louis Fed's public FRED API
(https://api.stlouisfed.org/fred/series/observations). These feed the
DES (Demand Environment Score) pillar — CPI, Fed Funds, 10Y Treasury,
unemployment, retail sales, etc.

Public functions:
    * ``get_series(series_id, observation_start=None)``
    * ``get_cpi()``
    * ``get_fed_funds()``
    * ``get_ten_year_yield()``
    * ``get_unemployment_rate()``
    * ``get_retail_sales()``
    * ``get_latest_value(series_id)``

All upstream calls go through:
    * a 24h ``FileCache("fred")`` for response bodies, and
    * a ``TokenBucket(capacity=20, refill_rate=5.0)`` (5 req/sec burst 20).

Auth: requires ``FRED_API_KEY`` in the environment. We read it lazily
(at first call), not at import time, so importing this module in a
process without the key set doesn't blow up.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import requests

from lthcs.sources._cache import FileCache
from lthcs.sources._ratelimit import TokenBucket

# 24 hours.
_CACHE_TTL_SECONDS = 24 * 60 * 60

_FRED_URL = "https://api.stlouisfed.org/fred/series/observations"

# Module-level singletons. One cache + one rate limiter per source.
_cache = FileCache("fred")
_bucket = TokenBucket(capacity=20, refill_rate=5.0)


class FredAPIError(RuntimeError):
    """Raised when the FRED API returns a non-200 response."""


def _api_key() -> str:
    """Read the FRED API key from the environment.

    Read lazily at call time (not import time) so this module can be
    imported in processes that don't actually use FRED.
    """
    key = os.environ.get("FRED_API_KEY")
    if not key:
        raise RuntimeError(
            "FRED_API_KEY is not set. Add it to your environment or .env file "
            "to use the FRED source client."
        )
    return key


def _cache_key(
    series_id: str,
    observation_start: Optional[str],
    as_of: Optional[str] = None,
) -> str:
    base = f"{series_id}/{observation_start or 'all'}"
    # Including ``as_of`` in the cache key keeps historical views from
    # colliding with the "latest" view (and with each other).  ``None``
    # maps to ``"latest"`` so the default cache key is unchanged from the
    # pre-``as_of`` implementation.
    return f"{base}/{as_of or 'latest'}"


def _parse_value(raw: Any) -> Optional[float]:
    """FRED encodes missing observations as the literal string ``"."``.

    Convert that to ``None``; coerce anything else to ``float``.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        if raw == "." or raw.strip() == "":
            return None
        return float(raw)
    return float(raw)


def _parse_observations(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Normalize the raw FRED response into our wire format."""
    raw_obs = payload.get("observations", []) or []
    out: List[Dict[str, Any]] = []
    for obs in raw_obs:
        date = obs.get("date")
        if not date:
            continue
        out.append({"date": date, "value": _parse_value(obs.get("value"))})
    # FRED already returns these ascending, but sort defensively.
    out.sort(key=lambda r: r["date"])
    return out


def _fetch_from_fred(
    series_id: str, observation_start: Optional[str]
) -> Dict[str, Any]:
    """Hit FRED (subject to the rate limiter) and return the raw JSON body."""
    _bucket.acquire()
    params: Dict[str, str] = {
        "series_id": series_id,
        "api_key": _api_key(),
        "file_type": "json",
    }
    if observation_start:
        params["observation_start"] = observation_start

    resp = requests.get(_FRED_URL, params=params, timeout=30)
    if not getattr(resp, "ok", resp.status_code == 200):
        body = ""
        try:
            body = resp.text[:200]
        except Exception:
            pass
        raise FredAPIError(
            f"FRED API returned HTTP {resp.status_code} for series "
            f"{series_id!r}: {body}"
        )
    return resp.json()


def get_series(
    series_id: str,
    observation_start: Optional[str] = None,
    as_of: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return all observations for ``series_id``.

    Each observation is a dict ``{"date": "YYYY-MM-DD", "value": float | None}``,
    sorted by date ascending. Missing values (FRED encodes them as ``"."``)
    are converted to ``None``.

    When ``as_of`` (ISO ``YYYY-MM-DD``) is provided, the returned series is
    filtered to entries whose ``date`` is on or before ``as_of`` — i.e. the
    state of the series as it would have looked on that date.  Comparison
    is inclusive (``<=``), so an observation that lands exactly on
    ``as_of`` IS included.

    Results are cached for 24h per (series_id, observation_start, as_of).
    """
    key = _cache_key(series_id, observation_start, as_of)
    hit = _cache.get(key)
    if hit is not None:
        # Cache stores the parsed (normalized) observation list directly.
        return list(hit.value)

    payload = _fetch_from_fred(series_id, observation_start)
    rows = _parse_observations(payload)
    if as_of is not None:
        rows = [r for r in rows if r["date"] <= as_of]
    _cache.set(key, rows, ttl_seconds=_CACHE_TTL_SECONDS)
    return rows


def get_latest_value(
    series_id: str, as_of: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Return the most recent non-null observation for ``series_id``.

    When ``as_of`` (ISO ``YYYY-MM-DD``) is provided, returns the most
    recent non-null observation whose ``date`` is on or before ``as_of``.
    Returns ``None`` if there are no qualifying observations.
    """
    series = get_series(series_id, as_of=as_of)
    for row in reversed(series):
        if row.get("value") is not None:
            return row
    return None


# --- Convenience wrappers for the DES pillar inputs. -------------------------


def get_cpi() -> List[Dict[str, Any]]:
    """CPI for All Urban Consumers (CPIAUCSL), monthly, seasonally adjusted."""
    return get_series("CPIAUCSL")


def get_fed_funds() -> List[Dict[str, Any]]:
    """Effective Federal Funds Rate (FEDFUNDS), monthly average."""
    return get_series("FEDFUNDS")


def get_ten_year_yield() -> List[Dict[str, Any]]:
    """10-Year Treasury Constant Maturity Rate (DGS10), daily."""
    return get_series("DGS10")


def get_unemployment_rate() -> List[Dict[str, Any]]:
    """Civilian Unemployment Rate (UNRATE), monthly."""
    return get_series("UNRATE")


def get_retail_sales() -> List[Dict[str, Any]]:
    """Advance Retail Sales: Retail Trade (RSXFS), monthly."""
    return get_series("RSXFS")
