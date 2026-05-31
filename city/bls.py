"""BLS LAUS unemployment adapter for the City tab Context layer (Layer B).

Fills ``context.unemployment_rate`` in ``data-city.schema.json`` with the most
recent monthly unemployment rate (%) for each of the six City-tab footprints.

Source
------
BLS Local Area Unemployment Statistics (LAUS), served by the BLS Public Data
API v2 at ``https://api.bls.gov/publicAPI/v2``. The API works KEYLESS (with a
lower daily request cap); a free registration key (``BLS_API_KEY``) raises the
limits and is passed as ``registrationkey`` in the POST body.

LAUS series-ID structure (resolved + live-verified 2026-05-31, keyless)
-----------------------------------------------------------------------
A LAUS series ID is exactly 20 characters::

    LAU  <prefix>  <area_code(13)>  <measure(2)>
    ^^^  ^^^^^^^^  ^^^^^^^^^^^^^^^  ^^^^^^^^^^^
    "LAU" type      FIPS-derived    "03" = unemployment rate

  * ``prefix`` is the area-type code: ``CT`` = "Cities and towns" (a Census
    place), ``CN`` = "Counties and equivalents".
  * ``measure`` ``03`` = the unemployment RATE (percent). (``04``/``05``/``06``
    are unemployment level / employment / labor force.)
  * The 13-char ``area_code`` is built from Census FIPS:
      - **City (CT):**   state FIPS (2) + place FIPS (5) + ``000000`` (6 zeros).
        Chicago = ``17`` + ``14000`` + ``000000`` -> ``1714000000000``.
      - **County (CN):** state FIPS (2) + county FIPS (3) + ``00000000`` (8
        zeros). Miami-Dade = ``12`` + ``086`` + ``00000000`` -> ``1208600000000``.

How the six series were derived (FIPS from the registry ``context_layer``)
--------------------------------------------------------------------------
Five footprints have a clean **city-level** ("Cities and towns", ``CT``)
series; Miami uses a **county** (``CN``) series to match the ops footprint
(Miami-Dade County 12086), exactly as the registry's
``miami_geography_decision`` dictates for Census ACS.

    city     scope   FIPS (state, place/county)   LAUS series ID          live rate (period)
    chicago  city    17 / 14000 (place)           LAUCT171400000000003    5.1% (2026-03)
    nyc      city    36 / 51000 (place)           LAUCT365100000000003    4.8% (2026-04)
    la       city    06 / 44000 (place)           LAUCT064400000000003    5.1% (2026-03)
    seattle  city    53 / 63000 (place)           LAUCT536300000000003    4.5% (2026-03)
    sf       city    06 / 67000 (place)           LAUCT066700000000003    3.7% (2026-03)
    miami    county  12 / 086   (county)          LAUCN120860000000003    3.1% (2026-04)

Every series above was confirmed live (keyless POST to the v2 timeseries
endpoint, 2026-05-31) returning recent monthly values — that round-trip is the
proof the IDs are correct, since LAUS only returns data for a valid series.

Notes on the recon
------------------
  * No metro/county *fallback* was needed: all five cities have a published
    city-level (CT) LAUS series, so none fall back to a metro/county series.
    (For reference, the matching counties exist too — e.g. King County, WA =
    ``LAUCN530330000000003`` (4.8%, 2026-03) — but the cleaner place-level CT
    series is preferred where available.)
  * The ``catalog:true`` flag on the API (which returns the human-readable
    ``series_title``) is gated behind a registered key and comes back empty
    keyless; the geography was therefore validated by FIPS arithmetic against
    the registry plus the live data round-trip rather than by catalog title.
  * LAUS county/place rates are MONTHLY and lag ~1 month; the latest available
    period differs per series (some 2026-03, some 2026-04). This adapter always
    returns the single most-recent period BLS reports as ``latest:true`` (with
    a defensive max-by-(year, month) fallback if that flag is absent).

Public surface
--------------
  * :class:`BLSError`
  * :data:`CITY_LAUS_SERIES`  (city_id -> LAUS series id)
  * :func:`fetch_unemployment`

The network function accepts a ``session=`` for injection (tests pass a fake);
it defaults to a module-level :class:`requests.Session`.
"""
from __future__ import annotations

import os
from typing import Optional

import requests

__all__ = [
    "BLSError",
    "CITY_LAUS_SERIES",
    "fetch_unemployment",
]


class BLSError(Exception):
    """Raised when a BLS API request fails or its payload can't be parsed.

    Covers transport failures, non-200 HTTP, malformed/non-JSON bodies, and the
    in-band ``status: "REQUEST_NOT_PROCESSED"`` error envelope the BLS API
    returns *inside* an HTTP 200 (e.g. daily-threshold-exceeded, bad key).
    """


# ---------------------------------------------------------------------------
# Resolved + live-verified LAUS series IDs (see module docstring for derivation)
# ---------------------------------------------------------------------------
# city_id -> 20-char LAUS series id (measure 03 = unemployment rate).
# Five city ("CT") series + Miami-Dade County ("CN") to match the ops footprint.
CITY_LAUS_SERIES: dict[str, str] = {
    "chicago": "LAUCT171400000000003",  # Chicago city, IL  (place 1714000)
    "nyc":     "LAUCT365100000000003",  # New York city, NY (place 3651000)
    "la":      "LAUCT064400000000003",  # Los Angeles city, CA (place 0644000)
    "seattle": "LAUCT536300000000003",  # Seattle city, WA  (place 5363000)
    "sf":      "LAUCT066700000000003",  # San Francisco city, CA (place 0667000)
    "miami":   "LAUCN120860000000003",  # Miami-Dade County, FL (county 12086)
}

# BLS Public Data API v2 single/multi-series timeseries endpoint.
_API_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"

# Module-level default session (connection pooling + keep-alive). Callers may
# override per-call via ``session=``; tests inject a fake exposing ``.post``.
_SESSION = requests.Session()

# Default per-request timeout (seconds).
_DEFAULT_TIMEOUT = 30


def _resolve_session(session):
    return session if session is not None else _SESSION


def _period_to_month(period) -> Optional[int]:
    """Map a BLS monthly ``period`` code ('M01'..'M12') to an int 1..12.

    Returns ``None`` for non-monthly periods (annual averages come back as
    'M13'; quarterly as 'Q01'..) so the caller can ignore them when picking the
    latest month.
    """
    if not isinstance(period, str) or len(period) != 3 or period[0] != "M":
        return None
    try:
        month = int(period[1:])
    except ValueError:
        return None
    if not 1 <= month <= 12:
        return None  # 'M13' = annual average, etc.
    return month


def _pick_latest(data_rows: list) -> Optional[dict]:
    """Return the most-recent monthly observation from a LAUS ``data`` array.

    BLS returns ``data`` newest-first and tags the newest with
    ``"latest": "true"``. We honor that flag first (it's authoritative), but
    fall back to a max-by-(year, month) scan over genuine monthly rows so the
    function is robust if the flag is ever missing or the order changes.
    Non-monthly rows (e.g. 'M13' annual averages) are excluded from both paths.
    """
    if not isinstance(data_rows, list):
        return None

    monthly = []
    for row in data_rows:
        if not isinstance(row, dict):
            continue
        month = _period_to_month(row.get("period"))
        if month is None:
            continue
        try:
            year = int(row.get("year"))
        except (TypeError, ValueError):
            continue
        monthly.append((year, month, row))

    if not monthly:
        return None

    # Prefer the row BLS flags as latest (string "true"), if it's a monthly row.
    for _, _, row in monthly:
        if str(row.get("latest", "")).lower() == "true":
            return row

    # Fallback: newest by (year, month).
    monthly.sort(key=lambda t: (t[0], t[1]))
    return monthly[-1][2]


def fetch_unemployment(city_id, *, api_key=None, session=None) -> Optional[float]:
    """Latest available monthly unemployment rate (%) for ``city_id``.

    POSTs to the BLS Public Data API v2 timeseries endpoint with
    ``{"seriesid": [<series>], "registrationkey": api_key?}`` and returns the
    most recent period's value as a ``float`` (e.g. ``5.1``).

    Args:
        city_id: one of the keys in :data:`CITY_LAUS_SERIES`
            (``chicago``/``nyc``/``la``/``seattle``/``sf``/``miami``).
        api_key: optional BLS registration key. Defaults to
            ``os.environ.get('BLS_API_KEY')``. The API works keyless (lower
            daily cap); a key only raises the limits. When falsy, the
            ``registrationkey`` field is omitted from the request body.
        session: optional injected HTTP session exposing ``.post`` (tests pass a
            fake). Defaults to the module-level :class:`requests.Session`.

    Returns:
        The most recent month's unemployment rate as a ``float``, or ``None`` if
        the series carries no usable data (empty/absent ``data`` array, or no
        monthly observation).

    Raises:
        BLSError: on an unknown ``city_id``, transport failure, non-200 HTTP, a
            non-JSON body, the in-band ``REQUEST_NOT_PROCESSED`` error envelope,
            or a non-numeric value where a rate is expected.
    """
    series_id = CITY_LAUS_SERIES.get(city_id)
    if series_id is None:
        raise BLSError(
            f"unknown city_id {city_id!r}; expected one of "
            f"{sorted(CITY_LAUS_SERIES)}"
        )

    if api_key is None:
        api_key = os.environ.get("BLS_API_KEY")

    payload: dict = {"seriesid": [series_id]}
    if api_key:  # omit entirely when unset/empty — keyless is valid
        payload["registrationkey"] = api_key

    sess = _resolve_session(session)
    try:
        resp = sess.post(_API_URL, json=payload, timeout=_DEFAULT_TIMEOUT)
    except requests.RequestException as exc:
        raise BLSError(f"BLS request for {series_id} failed: {exc}") from exc

    status = getattr(resp, "status_code", None)
    if status is not None and status != 200:
        body = ""
        try:
            body = (resp.text or "")[:200]
        except Exception:
            pass
        raise BLSError(f"BLS returned HTTP {status} for {series_id}: {body}")

    try:
        data = resp.json()
    except Exception as exc:
        raise BLSError(f"BLS returned malformed JSON for {series_id}: {exc}") from exc

    if not isinstance(data, dict):
        raise BLSError(f"BLS payload for {series_id} was not an object: {data!r}")

    # In-band error envelope (HTTP 200 + status REQUEST_NOT_PROCESSED), e.g.
    # daily threshold exceeded or invalid key.
    api_status = data.get("status")
    if api_status and api_status != "REQUEST_SUCCEEDED":
        messages = data.get("message") or []
        if isinstance(messages, list):
            messages = "; ".join(str(m) for m in messages)
        raise BLSError(
            f"BLS request not processed for {series_id}: {api_status} {messages}".strip()
        )

    series_list = (data.get("Results") or {}).get("series") or []
    if not series_list:
        # No series block at all — treat as unavailable rather than an error
        # (the request itself succeeded).
        return None

    data_rows = series_list[0].get("data") or []
    latest = _pick_latest(data_rows)
    if latest is None:
        return None  # empty series / no monthly observation

    raw_value = latest.get("value")
    # BLS uses "-" / "" for suppressed or unavailable values.
    if raw_value in (None, "", "-"):
        return None
    try:
        return float(raw_value)
    except (TypeError, ValueError) as exc:
        raise BLSError(
            f"BLS value for {series_id} not numeric: {raw_value!r}"
        ) from exc
