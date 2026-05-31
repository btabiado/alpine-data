"""Census ACS 5-year adapter for the City tab's Context layer (Layer B).

The Context layer enriches each city with American Community Survey (ACS)
5-year estimates: median household income, median gross rent, median home
value, and median real-estate taxes paid, plus a derived effective property
tax rate (taxes / home value). The output dict matches
``#/definitions/context`` in ``docs/city/data-city.schema.json`` (the
unemployment/AQI/context_score fields are populated by other adapters and are
emitted here as ``None``).

Geography (from ``context_layer.sources.census_acs.geo_by_city``):
  * Chicago / NYC / LA / Seattle / SF are **places** (``for=place:{place}``).
  * Miami is a **county** — Miami-Dade County 12086
    (``for=county:{county}``) — to match the Pulse footprint (ArcGIS County
    feeds + FBI ORI FL0130000). ACS exposes the same B-table variables for
    both geographies, so :func:`fetch_acs` handles either ``geo`` form off the
    same ``geo_cfg`` block.

Request shape (one row of estimates for the target geography)::

    GET https://api.census.gov/data/{vintage}/acs/acs5
        ?get=NAME,B19013_001E,B25064_001E,B25077_001E,B25103_001E
        &for=place:{place}&in=state:{state}        (place form)
        &for=county:{county}&in=state:{state}      (county form)
        [&key={api_key}]

Live-probe findings (2026-05-31):
  * The Census API now **REQUIRES a key for DATA queries** — a keyless data
    request returns a ``"Missing Key"`` HTML page, not JSON. So this module is
    built against the documented response shape and exercised against mocks;
    no live data call is made without a key. (The variables *metadata*
    endpoints stay keyless and were used to confirm the four variable codes
    resolve for vintage 2024.)
  * ACS responds as a **two-row JSON array** —
    ``[[header...], [values...]]`` — so values are read **by header name**,
    never by fixed position.
  * Unavailable estimates come back as ACS "jam"/annotation **sentinel
    negatives** (e.g. ``-666666666``). None of these dollar-valued estimates
    can legitimately be negative, so any negative (or non-numeric / missing)
    value maps to ``None`` for that field.

Public surface:
  * :class:`CensusError`
  * :func:`fetch_acs`

The network function accepts a ``session=`` for injection (tests pass a fake)
and defaults to a module-level :class:`requests.Session`. The API key defaults
to ``os.environ.get('CENSUS_API_KEY')``.
"""
from __future__ import annotations

import os
from typing import Optional

import requests

__all__ = ["CensusError", "fetch_acs"]


class CensusError(Exception):
    """Raised when a Census ACS request fails or its body cannot be parsed.

    Covers transport failures, non-200 responses, the keyless ``"Missing Key"``
    HTML page, and any body that is not the expected two-row JSON array.
    """


# Registry variable codes -> schema context field names. Order here is also the
# order the codes are requested in the ``get=`` list (NAME is prepended).
_VAR_TO_FIELD = {
    "B19013_001E": "median_income",
    "B25064_001E": "median_rent",
    "B25077_001E": "median_home_value",
    "B25103_001E": "median_real_estate_taxes",
}
_ACS_VARS = list(_VAR_TO_FIELD.keys())

_ACS_BASE = "https://api.census.gov/data/{vintage}/acs/acs5"

# Module-level default session (connection pooling + keep-alive). Callers may
# override per-call via ``session=``; tests inject a fake exposing ``.get``.
_SESSION = requests.Session()


def _coerce_estimate(raw) -> Optional[int]:
    """Coerce one ACS estimate cell to ``int``, or ``None`` if unusable.

    ACS returns numbers as JSON *strings* (``"74590"``). Unavailable estimates
    arrive as negative "jam"/annotation sentinels (``-666666666`` and friends);
    none of income / rent / home-value / taxes can legitimately be negative, so
    any negative value — like ``None``, ``""``, or a non-numeric string — maps
    to ``None``.
    """
    if raw is None:
        return None
    try:
        # ACS estimates are whole-dollar integers delivered as strings; float()
        # first tolerates an accidental "74590.0" without choking on it.
        value = int(float(raw))
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return value


def _effective_property_tax_rate(
    taxes: Optional[int], home_value: Optional[int]
) -> Optional[float]:
    """taxes / home_value, or ``None`` if either is missing or home_value is 0.

    Guards the zero/None denominator so a missing or sentinel home value never
    raises and never yields a bogus rate.
    """
    if taxes is None or not home_value:  # None or 0 home_value -> undefined
        return None
    return taxes / home_value


def _parse_acs_rows(data) -> dict:
    """Map the ACS two-row array to schema context fields, by header name.

    ``data`` is ``[[header...], [values...]]``. Values are looked up by the
    variable code in the header row (never by fixed column position, because
    the API may reorder ``NAME`` / ``state`` / ``place`` / ``county`` columns).
    Raises :class:`CensusError` if the shape is not two-plus rows of equal
    width or if a requested variable code is absent from the header.
    """
    if not isinstance(data, list) or len(data) < 2:
        raise CensusError(
            f"ACS body is not a two-row array (got {type(data).__name__} "
            f"len={len(data) if isinstance(data, list) else 'n/a'})"
        )
    header, values = data[0], data[1]
    if not isinstance(header, list) or not isinstance(values, list):
        raise CensusError("ACS rows are not lists")
    if len(header) != len(values):
        raise CensusError(
            f"ACS header/value width mismatch ({len(header)} vs {len(values)})"
        )

    by_name = dict(zip(header, values))

    fields: dict = {}
    for code, field in _VAR_TO_FIELD.items():
        if code not in by_name:
            raise CensusError(f"ACS response missing variable {code!r} in header")
        fields[field] = _coerce_estimate(by_name[code])

    fields["effective_property_tax_rate"] = _effective_property_tax_rate(
        fields["median_real_estate_taxes"], fields["median_home_value"]
    )
    # Populated by other Context adapters (BLS / AirNow / composite); the ACS
    # adapter always emits them as None so the dict is schema-complete.
    fields["unemployment_rate"] = None
    fields["aqi"] = None
    fields["context_score"] = None
    return fields


def fetch_acs(
    geo_cfg: dict,
    *,
    vintage: int = 2024,
    api_key: Optional[str] = None,
    session=None,
    timeout: int = 30,
) -> dict:
    """Fetch ACS 5-year context estimates for one city's geography.

    Args:
        geo_cfg: one entry from ``geo_by_city``. Must carry ``geo`` (``'place'``
            or ``'county'``), ``state``, and the matching code — ``place`` for
            place geographies or ``county`` for county geographies (Miami).
        vintage: ACS data vintage year (default 2024).
        api_key: Census API key. Defaults to ``os.environ['CENSUS_API_KEY']``.
            Sent as ``&key=`` only when present; a missing/empty key is allowed
            through so the caller can surface the Census ``"Missing Key"`` body
            as a :class:`CensusError`.
        session: optional injected HTTP session (defaults to module session).
        timeout: per-request timeout (seconds).

    Returns:
        The schema context dict::

            {median_income, median_rent, median_home_value,
             median_real_estate_taxes, effective_property_tax_rate,
             unemployment_rate: None, aqi: None, context_score: None}

        Each dollar field is an ``int`` or ``None`` (sentinel / missing).

    Raises:
        CensusError: on a missing ``geo``/``state``/geo-code in ``geo_cfg``, an
            unknown ``geo`` form, transport failure, non-200 response, the
            keyless ``"Missing Key"`` HTML body, or any non-two-row-array body.
    """
    geo = geo_cfg.get("geo")
    state = geo_cfg.get("state")
    if not geo:
        raise CensusError("geo_cfg missing 'geo' (want 'place' or 'county')")
    if not state:
        raise CensusError("geo_cfg missing 'state'")

    if geo == "place":
        code = geo_cfg.get("place")
        if not code:
            raise CensusError("place geo_cfg missing 'place' code")
        for_clause = f"place:{code}"
    elif geo == "county":
        code = geo_cfg.get("county")
        if not code:
            raise CensusError("county geo_cfg missing 'county' code")
        for_clause = f"county:{code}"
    else:
        raise CensusError(f"unsupported geo {geo!r} (want 'place' or 'county')")

    if api_key is None:
        api_key = os.environ.get("CENSUS_API_KEY")

    url = _ACS_BASE.format(vintage=vintage)
    params = {
        "get": "NAME," + ",".join(_ACS_VARS),
        "for": for_clause,
        "in": f"state:{state}",
    }
    if api_key:
        params["key"] = api_key

    sess = session if session is not None else _SESSION
    try:
        resp = sess.get(url, params=params, timeout=timeout)
    except requests.RequestException as exc:
        raise CensusError(f"ACS request to {url} failed: {exc}") from exc

    status = getattr(resp, "status_code", 200)
    if status >= 400:
        # Census returns the "Missing Key" / "Invalid Key" message as the body
        # of a non-200 response; include a short snippet for diagnosis.
        body = (getattr(resp, "text", "") or "")[:200]
        raise CensusError(
            f"ACS request to {url} returned HTTP {status}: {body!r}".rstrip()
        )

    try:
        data = resp.json()
    except ValueError as exc:
        # Keyless DATA queries return an HTML "Missing Key" page (HTTP 200),
        # which is not JSON — surface it rather than letting json() bubble.
        body = (getattr(resp, "text", "") or "")[:200]
        raise CensusError(
            f"ACS response from {url} was not JSON (missing/invalid key?): "
            f"{body!r}"
        ) from exc

    return _parse_acs_rows(data)
