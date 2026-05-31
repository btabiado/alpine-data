"""EPA AirNow current-AQI adapter for the City tab's Context layer (Layer B).

Fills the integer ``context.aqi`` field of ``data-city.schema.json`` for the six
City-tab cities. AirNow's current-observation endpoint queries by lat/lon (or by
zip), so this module hardcodes a downtown centroid per city (``CITY_LATLON``) and
asks for the current observations within a 50-mile radius.

AirNow REQUIRES an API key (registry ``context_layer.sources.epa_airnow``:
``env_var=AIRNOW_API_KEY``, ``key_required=true``). The key is read from
``AIRNOW_API_KEY`` when not passed explicitly. With no key we short-circuit and
return ``None`` WITHOUT touching the network (mirrors the ``fetch_fred``
no-key convention in ``fetch_market``/``tests/test_fred.py``).

Response shape (documented; we have no key, so it is mocked in tests). The
current-observation endpoint returns a JSON LIST with one object per reporting
parameter at the nearest monitor(s)::

    [
      {"DateObserved": "2026-05-31", "HourObserved": 14, "LocalTimeZone": "EST",
       "ReportingArea": "Miami", "StateCode": "FL", "Latitude": 25.77,
       "Longitude": -80.19, "ParameterName": "O3",
       "AQI": 41, "Category": {"Number": 1, "Name": "Good"}},
      {... "ParameterName": "PM2.5", "AQI": 58, ...},
      {... "ParameterName": "PM10",  "AQI": 22, ...}
    ]

We return the MAX ``AQI`` across the parameter objects. AirNow's headline
"overall" AQI for an area is defined as the AQI of the *worst* pollutant at that
moment (the AQI scale is a per-pollutant index and the reported overall value is
the maximum across pollutants), so taking the max over the per-parameter rows
reconstructs that overall figure. Returns ``None`` when the list is empty (no
monitor reported in range) or when there are no usable ``AQI`` values.

City coordinates (downtown centroids; WGS84 lat, lon). Sources: well-known
city-hall / civic-center points, cross-checked against the U.S. Census Gazetteer
place centroid and AirNow ``ReportingArea`` coverage. Miami = Miami / Miami-Dade
County (downtown Miami) to match the Pulse + Context county footprint (GEOID
12086) used elsewhere in the registry.
"""
from __future__ import annotations

import os
from typing import Optional

import requests

__all__ = ["AirNowError", "CITY_LATLON", "fetch_aqi"]


class AirNowError(Exception):
    """Raised on AirNow HTTP failure (non-200 / transport error) or a payload
    that cannot be parsed as the documented JSON list of observation objects."""


# Module-level default session (connection pooling + keep-alive). Injectable via
# ``session=`` so tests can swap in a canned transport; mirrors the socrata /
# arcgis adapters in this package.
_SESSION = requests.Session()

# AirNow current-observation endpoint (current AQI by lat/lon).
_AIRNOW_URL = "https://www.airnowapi.org/aq/observation/latLong/current/"

# Downtown centroids (WGS84 lat, lon) for the six City-tab cities.
#
# Source: well-known downtown / city-hall civic-center coordinates, rounded to
# 4 decimals (~11 m), each within its city's AirNow ReportingArea. Cross-checked
# against the U.S. Census Gazetteer place-centroid for the same place. Miami uses
# downtown Miami (Miami / Miami-Dade County) to match the registry's county
# footprint (state 12 / county 086 / GEOID 12086) used for the rest of Context.
CITY_LATLON: dict[str, tuple[float, float]] = {
    "chicago": (41.8781, -87.6298),   # The Loop, Chicago, IL
    "nyc":     (40.7128, -74.0060),   # Lower Manhattan / City Hall, New York, NY
    "la":      (34.0522, -118.2437),  # Downtown / Civic Center, Los Angeles, CA
    "seattle": (47.6062, -122.3321),  # Downtown, Seattle, WA
    "sf":      (37.7749, -122.4194),  # Civic Center, San Francisco, CA
    "miami":   (25.7617, -80.1918),   # Downtown Miami, Miami-Dade County, FL
}

# Radius (miles) for the nearest-monitor search. The registry/spec uses 50.
_DISTANCE_MILES = 50


def _resolve_session(session):
    return session if session is not None else _SESSION


def _coerce_aqi(value) -> Optional[int]:
    """Coerce one observation's ``AQI`` field to a non-negative int, or ``None``.

    AirNow uses ``-1`` (and occasionally ``null``) for "no current value" on a
    parameter; treat those as missing so they never win the ``max``.
    """
    if value is None:
        return None
    try:
        aqi = int(value)
    except (TypeError, ValueError):
        return None
    if aqi < 0:
        return None
    return aqi


def fetch_aqi(city_id, *, api_key=None, session=None) -> "int | None":
    """Current AQI for ``city_id`` from EPA AirNow, or ``None``.

    Issues::

        GET https://www.airnowapi.org/aq/observation/latLong/current/
            ?format=application/json
            &latitude={lat}&longitude={lon}
            &distance=50
            &API_KEY={api_key}

    where ``(lat, lon)`` comes from :data:`CITY_LATLON`. ``api_key`` defaults to
    ``os.environ.get('AIRNOW_API_KEY')``.

    Returns the MAX ``AQI`` across the per-parameter observation objects (O3,
    PM2.5, PM10, ...) — AirNow's reported overall AQI for an area is the AQI of
    the worst pollutant — as an ``int``. Returns ``None`` when:

      * no API key is available (short-circuits WITHOUT any HTTP request), or
      * ``city_id`` is unknown, or
      * the response list is empty / carries no usable ``AQI`` value.

    Raises :class:`AirNowError` on a non-200 response, a transport failure, or a
    body that is not the documented JSON list of observation objects.
    """
    if api_key is None:
        api_key = os.environ.get("AIRNOW_API_KEY")
    # No key -> short-circuit. Do NOT touch the network (the endpoint 401s
    # without a key, and the registry marks the key as required).
    if not api_key:
        return None

    coords = CITY_LATLON.get(city_id)
    if coords is None:
        return None
    lat, lon = coords

    sess = _resolve_session(session)
    params = {
        "format": "application/json",
        "latitude": lat,
        "longitude": lon,
        "distance": _DISTANCE_MILES,
        "API_KEY": api_key,
    }

    try:
        resp = sess.get(_AIRNOW_URL, params=params, timeout=30)
    except requests.RequestException as exc:
        raise AirNowError(f"AirNow request failed: {exc}") from exc

    status = getattr(resp, "status_code", None)
    if status != 200:
        body = ""
        try:
            body = (resp.text or "")[:200]
        except Exception:
            pass
        raise AirNowError(f"AirNow returned HTTP {status}: {body}")

    try:
        payload = resp.json()
    except Exception as exc:
        raise AirNowError(f"AirNow returned malformed JSON: {exc}") from exc

    # The current-observation endpoint returns a JSON list (one object per
    # parameter). An empty list = no monitor reported in range -> None.
    if not isinstance(payload, list):
        raise AirNowError(
            f"AirNow returned a non-list payload: {type(payload).__name__}"
        )
    if not payload:
        return None

    best: Optional[int] = None
    for obs in payload:
        if not isinstance(obs, dict):
            raise AirNowError(f"AirNow observation is not an object: {obs!r}")
        aqi = _coerce_aqi(obs.get("AQI"))
        if aqi is None:
            continue
        if best is None or aqi > best:
            best = aqi

    return best
