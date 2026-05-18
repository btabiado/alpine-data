"""EIA (Energy Information Administration) v2 API source client.

Hits the EIA v2 REST API for energy commodity prices that feed the
Demand Environment Score (DES) pillar — especially for energy-exposed
tickers (XOM, CVX, TSLA, LCID).

V1 surface area is intentionally narrow: WTI crude, Brent crude, and US
regular gasoline retail price. Each is a thin wrapper around a single
generic :func:`get_series` call.

Public functions:
    * :func:`get_series` — generic data fetch for any EIA v2 route
    * :func:`get_wti` — WTI crude spot price (daily)
    * :func:`get_brent` — Brent crude spot price (daily)
    * :func:`get_gasoline` — US regular gasoline retail price (weekly)
    * :func:`get_latest_value` — newest observation for one of the above

All upstream calls go through:
    * a 24h :class:`FileCache` (``"eia"``), and
    * a :class:`TokenBucket` (capacity=20, refill_rate=1.0).

EIA's published cap is 5,000 requests/hour, so 1 req/sec with a burst of
20 leaves ample headroom and matches the conservative posture used by
the other source clients.

The API key is read from the ``EIA_API_KEY`` environment variable at
first call (not at import time) so missing-key errors surface with a
clear message rather than as an ImportError.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

import requests

from lthcs.sources._cache import FileCache
from lthcs.sources._ratelimit import TokenBucket

# 24 hours — energy spot prices update at most a few times per day.
_CACHE_TTL_SECONDS = 24 * 60 * 60

_BASE_URL = "https://api.eia.gov/v2"

# Module-level singletons. One cache + one rate limiter per source.
_cache = FileCache("eia")
_bucket = TokenBucket(capacity=20, refill_rate=1.0)


# Hardcoded request specs for the three V1 series. Each entry captures
# exactly what get_series needs to build the call. Kept as plain dicts so
# tests can introspect them without importing private helpers.
_SERIES_SPECS: Dict[str, Dict[str, Any]] = {
    "wti": {
        "route": "petroleum/pri/spt",
        "frequency": "daily",
        "facets": {"product": ["EPCWTI"], "series": ["RWTC"]},
    },
    "brent": {
        "route": "petroleum/pri/spt",
        "frequency": "daily",
        "facets": {"product": ["EPCBRENT"], "series": ["RBRTE"]},
    },
    "gasoline": {
        "route": "petroleum/pri/gnd",
        "frequency": "weekly",
        "facets": {"duoarea": ["NUS"], "product": ["EPMR"]},
    },
}


class EIAError(RuntimeError):
    """Raised on EIA API errors (missing key, non-200, malformed body)."""


def _get_api_key() -> str:
    """Read EIA_API_KEY from the environment, raising a clear error if unset."""
    key = os.environ.get("EIA_API_KEY")
    if not key:
        raise EIAError(
            "EIA_API_KEY is not set. Register for a free key at "
            "https://www.eia.gov/opendata/register.php and export it "
            "before calling the EIA source client."
        )
    return key


def _build_params(
    api_key: str,
    frequency: str,
    data: str,
    facets: Optional[Dict[str, List[str]]] = None,
) -> List[tuple]:
    """Build the query-string param list for an EIA v2 ``/data/`` call.

    Uses a list of tuples (not a dict) so that repeated ``data[]`` and
    ``facets[...][]`` keys round-trip correctly through ``requests``.
    """
    params: List[tuple] = [
        ("api_key", api_key),
        ("frequency", frequency),
        ("data[]", data),
        ("sort[0][column]", "period"),
        ("sort[0][direction]", "desc"),
        ("length", "5000"),
    ]
    if facets:
        for facet_name, values in facets.items():
            for v in values:
                params.append((f"facets[{facet_name}][]", v))
    return params


def _cache_key(route: str, params: List[tuple]) -> str:
    """Stable cache key from the route + the non-secret params.

    The api_key is stripped so two callers with different keys still
    share the same cache entry (and so the key never lands on disk).
    """
    scrubbed = [(k, v) for (k, v) in params if k != "api_key"]
    return route + "?" + json.dumps(sorted(scrubbed), separators=(",", ":"))


def get_series(
    route: str,
    frequency: str = "daily",
    data: str = "value",
    facets: Optional[Dict[str, List[str]]] = None,
) -> List[Dict[str, Any]]:
    """Fetch a series from EIA v2 and normalize to ``[{date, value}, ...]``.

    ``route`` is the path between ``/v2/`` and ``/data/`` (e.g.
    ``"petroleum/pri/spt"``). ``facets`` is an optional mapping of facet
    name to a list of values, all of which become repeated ``facets[name][]``
    query params.

    Returns rows sorted ascending by date (newest last), regardless of
    the API's sort direction. ``value`` is coerced to ``float``; rows
    with non-numeric or missing values are dropped.
    """
    api_key = _get_api_key()
    params = _build_params(api_key, frequency, data, facets)

    key = _cache_key(route, params)
    hit = _cache.get(key)
    if hit is not None:
        return list(hit.value)

    url = f"{_BASE_URL}/{route}/data/"
    _bucket.acquire()
    resp = requests.get(url, params=params, timeout=30)
    if resp.status_code != 200:
        body = (resp.text or "")[:200]
        raise EIAError(
            f"EIA API returned HTTP {resp.status_code} for {route}: {body}"
        )

    try:
        body = resp.json()
    except ValueError as e:
        raise EIAError(f"EIA API returned non-JSON body for {route}: {e}") from e

    raw = (body.get("response") or {}).get("data") or []
    rows: List[Dict[str, Any]] = []
    for entry in raw:
        period = entry.get("period")
        value = entry.get("value")
        if period is None or value is None:
            continue
        try:
            num = float(value)
        except (TypeError, ValueError):
            continue
        rows.append({"date": str(period), "value": num})

    # Sort ascending by date so the newest observation is last regardless
    # of the API's response order.
    rows.sort(key=lambda r: r["date"])

    _cache.set(key, rows, ttl_seconds=_CACHE_TTL_SECONDS)
    return rows


def _get_spec_series(key: str) -> List[Dict[str, Any]]:
    spec = _SERIES_SPECS[key]
    return get_series(
        route=spec["route"],
        frequency=spec["frequency"],
        facets=spec["facets"],
    )


def get_wti() -> List[Dict[str, Any]]:
    """WTI (Cushing OK) crude spot price, daily, oldest -> newest."""
    return _get_spec_series("wti")


def get_brent() -> List[Dict[str, Any]]:
    """Brent crude spot price, daily, oldest -> newest."""
    return _get_spec_series("brent")


def get_gasoline() -> List[Dict[str, Any]]:
    """US regular gasoline retail price, weekly, oldest -> newest."""
    return _get_spec_series("gasoline")


def get_latest_value(
    route_key: str, as_of: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Return the most recent observation for one of the V1 series.

    ``route_key`` must be one of ``"wti"``, ``"brent"``, ``"gasoline"``.
    Returns ``None`` if the series is empty.

    When ``as_of`` (ISO ``YYYY-MM-DD``) is provided, returns the most
    recent observation whose ``date`` is on or before ``as_of`` — i.e.
    the latest reading as of that historical date.  Comparison is
    inclusive (``<=``).  Returns ``None`` if no observation satisfies
    the filter.
    """
    if route_key not in _SERIES_SPECS:
        raise EIAError(
            f"Unknown route_key '{route_key}'. "
            f"Expected one of: {sorted(_SERIES_SPECS)}"
        )
    rows = _get_spec_series(route_key)
    if not rows:
        return None
    if as_of is not None:
        rows = [r for r in rows if r["date"] <= as_of]
        if not rows:
            return None
    return rows[-1]
