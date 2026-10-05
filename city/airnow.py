"""EPA AirNow current-AQI adapter for the City tab's Context layer (Layer B).

Fills the integer ``context.aqi`` field of ``data-city.schema.json`` for the six
City-tab cities. AirNow's current-observation service queries by lat/lon (or by
zip), so this module hardcodes a downtown centroid per city (``CITY_LATLON``).

AirNow REQUIRES an API key (registry ``context_layer.sources.epa_airnow``:
``env_var=AIRNOW_API_KEY``, ``key_required=true``). The key is read from
``AIRNOW_API_KEY`` when not passed explicitly. With no key we short-circuit and
return ``None`` WITHOUT touching the network (mirrors the ``fetch_fred``
no-key convention in ``fetch_market``/``tests/test_fred.py``).

Endpoint (2026 web services). AirNow released six new web services on
2026-06-17 and retired the old ``/aq/observation/latLong/current/`` and
``/aq/observation/zipCode/current/`` services on 2026-09-30: with a valid key
they now answer HTTP 410 Gone, and their docs pages redirect to
``docs.airnowapi.org/webservices/retired``. The replacement "Current
Observations - By Zip Code or Lat/Long" service is::

    GET https://www.airnowapi.org/aq/observation/current/ziplatLong/
        ?format=application/json&latitude={lat}&longitude={lon}&API_KEY={key}

``distance`` is gone (the service applies the reporting area's own lookup
boundary, typically 50 miles, and ignores the parameter). It returns a JSON
LIST with one object per pollutant, lowerCamelCase, the AQI in ``nowcastAQI``::

    [
      {"dateObserved": "2026-09-09", "hourObserved": "17:00",
       "localTimeZone": "PDT", "reportingAreaName": "NW Coastal LA",
       "siteID": "060370113", "siteName": "West Los Angeles - VA Hospital",
       "parameterName": "OZONE", "nowcastAQI": 34, "aqiCategoryName": "Good",
       "reportingAgency": "South Coast AQMD",
       "lookupBehavior": "Closest Reading By Pollutant",
       "consideredMonitors": "All", "lookupBoundary": "50 Miles"},
      {... "parameterName": "PM2.5", "nowcastAQI": 11, ...}
    ]

"No observations in range" is a 200 whose body is the error envelope
``{"WebServiceError": [{"Message": "There are no observations available ..."}]}``
and maps to ``None``; any other ``WebServiceError`` raises. The legacy
PascalCase ``AQI`` field is still accepted so an old-shape payload parses.

We return the MAX AQI across the pollutant objects. AirNow's headline
"overall" AQI for an area is defined as the AQI of the *worst* pollutant at that
moment (the AQI scale is a per-pollutant index and the reported overall value is
the maximum across pollutants), so taking the max over the per-parameter rows
reconstructs that overall figure. Returns ``None`` when the list is empty (no
monitor reported in range) or when there are no usable AQI values.

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

from .redact import redact

__all__ = ["AirNowError", "CITY_LATLON", "fetch_aqi"]


class AirNowError(Exception):
    """Raised on AirNow HTTP failure (non-200 / transport error) or a payload
    that cannot be parsed as the documented JSON list of observation objects."""


# Module-level default session (connection pooling + keep-alive). Injectable via
# ``session=`` so tests can swap in a canned transport; mirrors the socrata /
# arcgis adapters in this package.
_SESSION = requests.Session()

# AirNow "Current Observations - By Zip Code or Lat/Long" (2026 web services).
# The old /aq/observation/latLong/current/ was retired 2026-09-30 (HTTP 410).
_AIRNOW_URL = "https://www.airnowapi.org/aq/observation/current/ziplatLong/"

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

# The "nothing in range" message of a 200 WebServiceError envelope. Anything
# else in that envelope is a real error.
_NO_DATA_PREFIXES = ("there are no ", "there is no ", "error - there are no ")


def _resolve_session(session):
    return session if session is not None else _SESSION


def _coerce_aqi(value) -> Optional[int]:
    """Coerce one observation's AQI field to a non-negative int, or ``None``.

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

        GET https://www.airnowapi.org/aq/observation/current/ziplatLong/
            ?format=application/json
            &latitude={lat}&longitude={lon}
            &API_KEY={api_key}

    where ``(lat, lon)`` comes from :data:`CITY_LATLON`. ``api_key`` defaults to
    ``os.environ.get('AIRNOW_API_KEY')``.

    Returns the MAX ``nowcastAQI`` across the per-parameter observation objects (O3,
    PM2.5, PM10, ...) — AirNow's reported overall AQI for an area is the AQI of
    the worst pollutant — as an ``int``. Returns ``None`` when:

      * no API key is available (short-circuits WITHOUT any HTTP request), or
      * ``city_id`` is unknown, or
      * the response list is empty / carries no usable AQI value, or the
        service answers with its "there are no observations" envelope.

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
            pass  # response body is optional context for the error raised below
        raise AirNowError(redact(f"AirNow returned HTTP {status}: {body}"))

    try:
        payload = resp.json()
    except Exception as exc:
        raise AirNowError(f"AirNow returned malformed JSON: {exc}") from exc

    # "No data" arrives as a 200 carrying the error envelope, not as [].
    if isinstance(payload, dict) and "WebServiceError" in payload:
        msgs = [
            str((m or {}).get("Message", "")) if isinstance(m, dict) else str(m)
            for m in (payload.get("WebServiceError") or [])
        ]
        if msgs and all(m.strip().lower().startswith(_NO_DATA_PREFIXES) for m in msgs):
            return None
        raise AirNowError(redact(f"AirNow returned an error: {'; '.join(msgs)[:200]}"))

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
        # nowcastAQI (2026 services); AQI (the retired services' field name).
        raw = obs.get("nowcastAQI", obs.get("AQI"))
        aqi = _coerce_aqi(raw)
        if aqi is None:
            continue
        if best is None or aqi > best:
            best = aqi

    return best
