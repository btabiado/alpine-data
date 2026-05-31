"""FBI Crime Data Explorer (CDE) adapter for the City tab — Miami Public Safety.

Miami-Dade County publishes its open data on ArcGIS (permits, a stale 2023 311
snapshot), but has **no clean open crime dataset**. The Public Safety pillar is
therefore filled from the FBI's Crime Data Explorer (``cde.ucr.cjis.gov``), which
exposes NIBRS offense counts for the county police department
(ORI ``FL0130000`` = Miami-Dade County Police Department, NIBRS since 2022-01).

Two distinct CDE surfaces are used here:

  * **ORI resolution (KEYLESS).** ``GET .../agency/byStateAbbr/{ST}`` returns the
    state's agencies so an ORI can be looked up by name. NOTE the response is a
    **dict keyed by COUNTY name**, each value a *list* of agency objects — NOT a
    flat list — so :func:`resolve_ori` flattens across every county bucket before
    matching. (Confirmed live 2026-05-31: ``('FL', 'Miami-Dade')`` -> ``FL0130000``.)

  * **Offense counts (DATA series).** ``GET .../summarized/agency/{ori}/{offense}``
    returns monthly buckets. The agency's own monthly counts live at
    ``offenses.actuals.{Agency Name} Offenses`` keyed by ``MM-YYYY``; the sibling
    ``... Clearances`` series (cleared cases) and the ``offenses.rates`` per-100k /
    Florida / United States comparison series are deliberately ignored.

Live-probe findings (2026-05-31):
  * The ``from`` / ``to`` query params use **``MM-YYYY``** (a bare year ``2024`` is
    rejected: *"expected format MM-YYYY"*). :func:`monthly_offenses` formats them
    from the caller's ``YYYY-MM`` / ``YYYY`` ``since`` / ``until``.
  * ``offenses.actuals`` months come back **unordered** across a cross-year range
    (e.g. ``11-2024, 12-2024, 01-2025`` arrived as ``01-2025, 02-2025, 11-2024,
    12-2024``), so the parsed series is sorted ascending.
  * A bogus / unparticipating ORI returns **HTTP 200 with ``actuals: null``**
    (not a 404) — treated as "no data" -> ``[]``, not an error.

Key policy (per the build spec / registry ``fbi_cde`` source): the DATA series
requires a free ``api.data.gov`` key in ``FBI_CDE_API_KEY`` (passed as the
``API_KEY`` query param). :func:`monthly_offenses` defaults the key from the
environment and, when no key is available, returns ``[]`` **without making any
HTTP request** — Miami Safety stays ``not_published`` until a key is set. (The
ORI list is keyless and always resolvable.)

Public surface:
  * :class:`FBIError`
  * :func:`resolve_ori`
  * :func:`monthly_offenses`

All network functions accept a ``session=`` for injection (tests pass a fake);
they default to a module-level :class:`requests.Session`.
"""
from __future__ import annotations

import os
from typing import Optional

import requests

__all__ = [
    "FBIError",
    "resolve_ori",
    "monthly_offenses",
]


class FBIError(Exception):
    """Raised when a CDE request fails (transport, non-200, or unparseable body).

    Note: a bogus ORI is *not* an error — CDE answers HTTP 200 with
    ``actuals: null`` and :func:`monthly_offenses` maps that to an empty series.
    """


# Module-level default session (connection pooling + keep-alive). Callers may
# override per call via ``session=``; tests inject a fake exposing ``.get``.
_SESSION = requests.Session()

# Default CDE base. The registry's ``data_endpoint_base`` is the same value; it
# is accepted as a parameter on the data call so the registry stays the source
# of truth, but kept here so ORI resolution needs no config.
_CDE_BASE = "https://cde.ucr.cjis.gov/LATEST"

# In-process ORI cache, keyed by (STATE_ABBR_UPPER, name_substr_lower). Spares a
# ~220 KB state-agency download on repeat lookups within a single run.
_ORI_CACHE: dict[tuple[str, str], Optional[str]] = {}


def _resolve_session(session):
    return session if session is not None else _SESSION


# --------------------------------------------------------------------------- #
# Small month helpers (no third-party date dependency)
# --------------------------------------------------------------------------- #
def _parse_period(value) -> tuple[int, int]:
    """Coerce a ``since`` / ``until`` bound to ``(year, month)``.

    Accepts ``'YYYY-MM'`` (month preserved) or a bare ``'YYYY'`` / 4-digit int.
    A bare year is anchored to January for ``since`` and December for ``until``
    by the caller passing ``month_default``; here a bare year yields month 1 and
    the caller widens ``until`` separately. Raises :class:`FBIError` on garbage.
    """
    s = str(value).strip()
    if "-" in s:
        try:
            year_s, month_s = s.split("-")[:2]
            year, month = int(year_s), int(month_s)
        except (ValueError, IndexError) as exc:
            raise FBIError(f"invalid period {value!r} (want 'YYYY-MM' or 'YYYY')") from exc
    elif len(s) == 4 and s.isdigit():
        year, month = int(s), 1
    else:
        raise FBIError(f"invalid period {value!r} (want 'YYYY-MM' or 'YYYY')")
    if not 1 <= month <= 12:
        raise FBIError(f"invalid period {value!r}: month out of range")
    return year, month


def _to_mm_yyyy(year: int, month: int) -> str:
    """``(year, month)`` -> CDE ``'MM-YYYY'`` query token (zero-padded month)."""
    return f"{month:02d}-{year:04d}"


def _mm_yyyy_to_iso(token: str) -> Optional[str]:
    """CDE ``'MM-YYYY'`` bucket key -> contract ``'YYYY-MM'``; ``None`` if unparseable."""
    s = str(token).strip()
    parts = s.split("-")
    if len(parts) != 2:
        return None
    mm, yyyy = parts
    if not (mm.isdigit() and yyyy.isdigit()):
        return None
    month, year = int(mm), int(yyyy)
    if not (1 <= month <= 12 and 1000 <= year <= 9999):
        return None
    return f"{year:04d}-{month:02d}"


def _get_json(session, url: str, *, params: dict, timeout: int):
    """GET ``url`` and return parsed JSON, mapping failures to :class:`FBIError`.

    Unlike ArcGIS, CDE reports bad input via real HTTP status codes (e.g. 400 for
    a malformed date), so any non-200 raises.
    """
    sess = _resolve_session(session)
    try:
        resp = sess.get(url, params=params, timeout=timeout)
    except requests.RequestException as exc:
        raise FBIError(f"CDE request to {url} failed: {exc}") from exc

    status = getattr(resp, "status_code", 200)
    if status != 200:
        body = ""
        try:
            body = (resp.text or "")[:200]
        except Exception:
            pass
        raise FBIError(f"CDE returned HTTP {status} at {url}: {body}".rstrip(": "))

    try:
        return resp.json()
    except ValueError as exc:
        raise FBIError(f"CDE response from {url} was not JSON") from exc


# --------------------------------------------------------------------------- #
# ORI resolution (KEYLESS)
# --------------------------------------------------------------------------- #
def resolve_ori(state_abbr, name_substr, *, session=None) -> Optional[str]:
    """Resolve an agency ORI by state + name substring (KEYLESS, cached).

    ``GET {_CDE_BASE}/agency/byStateAbbr/{state_abbr}`` and return the ORI of the
    first agency whose ``agency_name`` contains ``name_substr``
    (case-insensitive). For Miami: ``resolve_ori('FL', 'Miami-Dade')`` ->
    ``'FL0130000'`` (Miami-Dade County Police Department).

    The CDE response is a **dict keyed by county name**, each value a list of
    agency objects, so all county buckets are flattened before matching. Returns
    ``None`` if no agency matches or the payload isn't the expected shape (e.g. a
    bogus state code yields a different ``cde_agencies_query`` envelope).

    Result is cached in-process on ``(STATE.upper(), substr.lower())``.

    Raises :class:`FBIError` only on transport / non-200 / non-JSON failures.
    """
    st = str(state_abbr).strip().upper()
    substr = str(name_substr).strip().lower()
    cache_key = (st, substr)
    if cache_key in _ORI_CACHE:
        return _ORI_CACHE[cache_key]

    url = f"{_CDE_BASE}/agency/byStateAbbr/{st}"
    data = _get_json(session, url, params={}, timeout=40)

    ori = _find_ori_in_agencies(data, substr)
    _ORI_CACHE[cache_key] = ori
    return ori


def _find_ori_in_agencies(data, substr_lower: str) -> Optional[str]:
    """Flatten the dict-of-county-lists agencies payload and first-match by name.

    Returns the matching agency's ``ori`` (preserving CDE's bucket/list order) or
    ``None`` if ``data`` isn't the expected shape or nothing matches.
    """
    if not isinstance(data, dict):
        return None
    for _county, agencies in data.items():
        if not isinstance(agencies, list):
            # e.g. the ``cde_agencies_query`` echo envelope a bad state returns.
            continue
        for agency in agencies:
            if not isinstance(agency, dict):
                continue
            name = agency.get("agency_name") or ""
            if substr_lower in str(name).lower():
                ori = agency.get("ori")
                if isinstance(ori, str) and ori:
                    return ori
    return None


# --------------------------------------------------------------------------- #
# Monthly offense counts (DATA series — needs api.data.gov key)
# --------------------------------------------------------------------------- #
def monthly_offenses(
    ori,
    *,
    api_key=None,
    since=None,
    until=None,
    offense="violent-crime",
    timeout=40,
    session=None,
) -> list[dict]:
    """Monthly offense counts for an ORI from the CDE summarized-agency endpoint.

    ``GET {_CDE_BASE}/summarized/agency/{ori}/{offense}?from={MM-YYYY}&to={MM-YYYY}
        &API_KEY={api_key}``

    Returns an ascending ``[{"month": "YYYY-MM", "n": int}, ...]`` built from the
    agency's own ``offenses.actuals."{Agency Name} Offenses"`` series (the sibling
    ``... Clearances`` series and the Florida / United States comparison series
    under ``offenses.rates`` are ignored).

    Key handling (build-spec policy): ``api_key`` defaults to
    ``os.environ.get('FBI_CDE_API_KEY')``. **With no key this returns ``[]`` and
    makes no HTTP request** — Miami Safety stays ``not_published`` until a key is
    set. The ORI list (:func:`resolve_ori`) is keyless and unaffected.

    Date bounds: ``since`` / ``until`` accept ``'YYYY-MM'`` or a bare ``'YYYY'``
    and are sent as CDE's ``MM-YYYY`` ``from`` / ``to`` params; a bare-year
    ``until`` widens to December. When ``until`` is omitted it defaults to
    ``since``; when ``since`` is omitted no ``from`` / ``to`` is sent and CDE
    returns its full available window.

    Args:
        ori: agency ORI, e.g. ``'FL0130000'``.
        api_key: api.data.gov key; falls back to ``FBI_CDE_API_KEY``.
        since, until: inclusive bounds (``'YYYY-MM'`` or ``'YYYY'``).
        offense: CDE offense slug path segment (default ``'violent-crime'``;
            ``'property-crime'`` etc. also valid).
        timeout: per-request timeout (seconds).
        session: optional injected HTTP session.

    Returns:
        Ascending ``[{"month": "YYYY-MM", "n": int}, ...]``. Empty list when no
        key is available, or when CDE reports no agency actuals
        (``offenses.actuals`` is ``null`` / absent — e.g. an unknown ORI).

    Raises:
        :class:`FBIError` on transport failure, a non-200 status, a non-JSON
        body, or a malformed/non-numeric offense value.
    """
    if api_key is None:
        api_key = os.environ.get("FBI_CDE_API_KEY")
    # No key -> no network, empty series (Safety stays not_published).
    if not api_key:
        return []

    base = _CDE_BASE.rstrip("/")
    url = f"{base}/summarized/agency/{ori}/{offense}"

    params: dict = {"API_KEY": api_key}
    if since is not None:
        sy, sm = _parse_period(since)
        params["from"] = _to_mm_yyyy(sy, sm)
        if until is not None:
            uy, um = _parse_period(until)
            # A bare-year `until` ("2024") parses to month 1; widen to December
            # so the year is inclusive.
            if "-" not in str(until).strip() and len(str(until).strip()) == 4:
                um = 12
        else:
            uy, um = sy, sm
        params["to"] = _to_mm_yyyy(uy, um)

    data = _get_json(session, url, params=params, timeout=timeout)
    return _parse_actuals(data)


def _parse_actuals(data) -> list[dict]:
    """Extract the agency Offenses ``actuals`` map -> ascending ``[{month,n}]``.

    Picks the single ``offenses.actuals`` series whose name ends with
    ``' Offenses'`` (the agency's own counts) and skips the ``' Clearances'``
    sibling. Months arrive unordered, so the result is sorted ascending.
    """
    if not isinstance(data, dict):
        raise FBIError(f"CDE payload is not an object: {type(data).__name__}")

    offenses = data.get("offenses")
    if not isinstance(offenses, dict):
        raise FBIError(f"CDE payload missing 'offenses' object: {data!r}"[:300])

    actuals = offenses.get("actuals")
    # Unknown / unparticipating ORI: CDE returns actuals=null (HTTP 200). No
    # agency counts -> empty series (not an error).
    if actuals is None:
        return []
    if not isinstance(actuals, dict):
        raise FBIError(f"CDE 'offenses.actuals' is not an object: {actuals!r}"[:300])

    # Choose the agency's Offenses series (exclude its Clearances sibling). There
    # is exactly one '* Offenses' actuals series per agency response.
    series_map = None
    for name, month_map in actuals.items():
        if not isinstance(name, str):
            continue
        if name.endswith(" Offenses") and isinstance(month_map, dict):
            series_map = month_map
            break
    if series_map is None:
        # actuals present but no Offenses series (shouldn't happen) -> empty.
        return []

    acc: dict[str, int] = {}
    for mm_yyyy, raw_n in series_map.items():
        month = _mm_yyyy_to_iso(mm_yyyy)
        if month is None:
            continue
        try:
            n = int(round(float(raw_n)))
        except (TypeError, ValueError) as exc:
            raise FBIError(
                f"CDE offense count not numeric for {mm_yyyy!r}: {raw_n!r}"
            ) from exc
        acc[month] = acc.get(month, 0) + n

    return [{"month": m, "n": acc[m]} for m in sorted(acc)]
