"""Socrata (SODA v2.1) adapter for the City dashboard tab.

Produces monthly count series ``[{"month": "YYYY-MM", "n": int}, ...]`` ascending
by month for the Socrata-hosted city feeds in ``docs/city/city_registry.resolved.json``.
The City Pulse scorer (a separate module) turns these series into the frozen
``data-city.schema.json`` per-feed math.

Quirks this module handles (see RECON.md / the registry per-feed ``note`` fields):

* Big tables (NYC 311 ``erm2-nwe9`` ~38M rows, Chicago crime) MUST be queried with a
  recent ``since`` ``$where`` filter or they time out keyless. Callers pass ``since``.
* NYC DOB ``ipu4-2q9a`` stores ``issuance_date`` as TEXT in ``MM/DD/YYYY`` form, so
  ``date_trunc_ym`` raises a SoQL type-mismatch. ``date_is_text=True`` switches to a
  ``substring(...)||'-'||substring(...)`` month bucket instead.
* ``IS NOT NULL`` on the date column is always applied (Seattle permits / un-issued
  rows otherwise inflate a large null bucket).
* Union feeds (``baseline_dataset``): NYC complaints 5uac-w243 union qgea-i56i; LA crime
  k7nn-b2ep union y8y3-fqfu. ``feed_series`` fetches both and sums ``n`` per month.
* LA 311 rotates yearly (``dataset_rotates_yearly``): the current-year dataset is
  catalog-resolved by title ``MyLA311 Cases {year}`` (do NOT construct the retired
  ``...Service Request Data {year}`` ids), then unioned with the baseline file.
* Auth: one free ``SOCRATA_APP_TOKEN`` is portal-agnostic across all 5 hosts; passed as
  the ``X-App-Token`` header. Keyless works for small queries but throttles (429) on
  large tables.
"""
from __future__ import annotations

import os
from typing import Iterable, Optional

import requests

# Module-level default session (connection pooling + keep-alive). Injectable via
# ``session=`` on every public function so tests can swap in a canned transport.
_SESSION = requests.Session()

# Socrata caps an un-paged $limit at 50000 rows; a monthly aggregation returns at most
# a few hundred buckets, so one page always covers it.
_DEFAULT_LIMIT = 50000

# LA 311 catalog resolution endpoint (data.lacity.org). Kept module-level so tests can
# assert the URL without re-deriving it.
_LA_CATALOG_URL = "https://data.lacity.org/api/catalog/v1"


class SocrataError(Exception):
    """Raised on any non-200 response, throttling (429), or malformed payload."""


# --------------------------------------------------------------------------- #
# internal helpers
# --------------------------------------------------------------------------- #
def _resolve_session(session):
    return session if session is not None else _SESSION


def _headers(app_token: Optional[str]) -> dict:
    """Build request headers. Socrata accepts the token as ``X-App-Token``."""
    headers = {"Accept": "application/json"}
    if app_token:
        headers["X-App-Token"] = app_token
    return headers


def _month_bucket_expr(date_col: str, *, date_is_text: bool) -> str:
    """SoQL ``$select`` expression that yields a ``YYYY-MM`` month key aliased ``m``.

    Normal (real date/timestamp) column -> ``date_trunc_ym(col) AS m``.
    Text ``MM/DD/YYYY`` column -> ``substring(col,7,4)||'-'||substring(col,1,2) AS m``
    (positions 7..10 = year, 1..2 = month).
    """
    if date_is_text:
        return (
            f"substring({date_col},7,4)||'-'||substring({date_col},1,2) AS m"
        )
    return f"date_trunc_ym({date_col}) AS m"


def _build_where(
    date_col: str,
    *,
    since: Optional[str],
    extra_where: Optional[str],
    date_is_text: bool = False,
) -> str:
    """Compose the ``$where`` clause.

    Always ``{date_col} IS NOT NULL``; add a ``since`` lower-bound when given (``since`` may
    be a bare ``YYYY`` or a full ``YYYY-MM[-DD]`` string); and append any caller-supplied
    ``extra_where`` with ``AND``.

    For a real date/timestamp column the bound is ``{date_col} >= 'YYYY-01-01'`` (a bare year
    is normalized to Jan-1). For a TEXT ``MM/DD/YYYY`` column (``date_is_text=True``) a plain
    ``>=`` against the raw column is a lexicographic string compare ('06/17/2020' vs
    '2026-...') and silently matches nothing — so instead we compare the parsed YEAR
    substring: ``substring({date_col},7,4) >= 'YYYY'`` (year-floor). This both stays correct
    and trims the row scan (helping avoid keyless 429s).
    """
    clauses = [f"{date_col} IS NOT NULL"]
    if since:
        since_str = str(since).strip()
        if date_is_text:
            # Year-floor on the parsed text year (positions 7..10 of MM/DD/YYYY).
            year = since_str[:4]
            if len(year) == 4 and year.isdigit():
                clauses.append(f"substring({date_col},7,4) >= '{year}'")
        else:
            # Accept a bare year ("2024") or a full date; normalize a year to Jan-1.
            if len(since_str) == 4 and since_str.isdigit():
                since_str = f"{since_str}-01-01"
            clauses.append(f"{date_col} >= '{since_str}'")
    if extra_where:
        clauses.append(f"({extra_where})")
    return " AND ".join(clauses)


def _normalize_month(raw) -> Optional[str]:
    """Coerce a Socrata month value to a ``YYYY-MM`` string, or ``None`` if unusable.

    ``date_trunc_ym`` returns a floating timestamp like ``2026-04-01T00:00:00.000``;
    the substring path returns ``2026-04`` directly. Both reduce to the first 7 chars.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if len(s) < 7:
        return None
    month = s[:7]
    # Expect "YYYY-MM"; reject anything that isn't digit-digit-digit-digit-dash-digit-digit.
    if month[4] != "-" or not (month[:4].isdigit() and month[5:7].isdigit()):
        return None
    return month


def _parse_rows(rows) -> list[dict]:
    """Turn Socrata aggregation rows ``[{"m":..., "n":...}, ...]`` into an ascending
    ``[{"month","n"}]`` series, summing any duplicate month keys defensively."""
    if not isinstance(rows, list):
        raise SocrataError(f"Socrata returned a non-list payload: {type(rows).__name__}")
    acc: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise SocrataError(f"Socrata row is not an object: {row!r}")
        month = _normalize_month(row.get("m"))
        if month is None:
            # Null/blank month bucket (shouldn't happen given IS NOT NULL, but be safe).
            continue
        raw_n = row.get("n", 0)
        try:
            n = int(float(raw_n))
        except (TypeError, ValueError):
            raise SocrataError(f"Socrata count value not numeric: {raw_n!r}")
        acc[month] = acc.get(month, 0) + n
    return [{"month": m, "n": acc[m]} for m in sorted(acc)]


def _merge_series(series_list: Iterable[list[dict]]) -> list[dict]:
    """Sum multiple ``[{"month","n"}]`` series by month into one ascending series.

    Used to assemble union feeds (primary ∪ baseline_dataset). Months present in only
    one series pass through; months in both are summed (correct for the LA crime seam,
    which doesn't overlap, and harmless if it ever did per the registry design note)."""
    acc: dict[str, int] = {}
    for series in series_list:
        for row in series or []:
            month = row.get("month")
            if not month:
                continue
            acc[month] = acc.get(month, 0) + int(row.get("n", 0))
    return [{"month": m, "n": acc[m]} for m in sorted(acc)]


def _get_json(session, url: str, *, params: dict, headers: dict, timeout: int):
    """Issue a GET and return parsed JSON, mapping transport/HTTP/parse failures to
    ``SocrataError``. 429 (throttle) is called out explicitly."""
    try:
        resp = session.get(url, params=params, headers=headers, timeout=timeout)
    except requests.RequestException as exc:  # network error, timeout, etc.
        raise SocrataError(f"Socrata request to {url} failed: {exc}") from exc

    status = getattr(resp, "status_code", None)
    if status == 429:
        raise SocrataError(
            f"Socrata throttled (HTTP 429) at {url}; pass SOCRATA_APP_TOKEN and/or a "
            f"narrower 'since' window."
        )
    if status != 200:
        body = ""
        try:
            body = (resp.text or "")[:200]
        except Exception:
            pass
        raise SocrataError(f"Socrata returned HTTP {status} at {url}: {body}")

    try:
        return resp.json()
    except Exception as exc:
        raise SocrataError(f"Socrata returned malformed JSON at {url}: {exc}") from exc


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def monthly_counts(
    host,
    dataset,
    date_col,
    *,
    app_token=None,
    since=None,
    date_is_text=False,
    text_fmt="MM/DD/YYYY",
    extra_where=None,
    timeout=120,
    session=None,
) -> list[dict]:
    """Return monthly counts for one Socrata dataset, ascending by month.

    ``[{"month": "YYYY-MM", "n": int}, ...]``

    Normal path builds ``$select=date_trunc_ym({date_col}) AS m, count(*) AS n`` with
    ``$group=m`` and ``$order=m``. When ``date_is_text=True`` (NYC DOB ``issuance_date``,
    text ``MM/DD/YYYY``) the month bucket is instead
    ``substring({date_col},7,4)||'-'||substring({date_col},1,2) AS m`` because
    ``date_trunc_ym`` raises a SoQL type-mismatch on a text column.

    The ``$where`` always includes ``{date_col} IS NOT NULL`` and, when given,
    ``{date_col} >= 'YYYY-01-01'`` (from ``since``) plus any ``extra_where``. ``since`` is
    REQUIRED in practice for the very large tables (NYC 311, Chicago crime) or the query
    times out / 429s keyless.

    ``app_token`` is sent as the ``X-App-Token`` header. ``$limit`` is high (50000) so the
    full set of monthly buckets returns in one page.

    Raises ``SocrataError`` on non-200, 429, or a malformed/non-list payload.

    ``text_fmt`` is accepted for interface/forward-compat; the substring positions are
    fixed to ``MM/DD/YYYY`` (the only text format in the resolved registry).
    """
    sess = _resolve_session(session)

    select_expr = f"{_month_bucket_expr(date_col, date_is_text=date_is_text)}, count(*) AS n"
    where_clause = _build_where(
        date_col, since=since, extra_where=extra_where, date_is_text=date_is_text
    )

    params = {
        "$select": select_expr,
        "$where": where_clause,
        "$group": "m",
        "$order": "m",
        "$limit": _DEFAULT_LIMIT,
    }

    url = f"https://{host}/resource/{dataset}.json"
    payload = _get_json(sess, url, params=params, headers=_headers(app_token), timeout=timeout)
    return _parse_rows(payload)


def la_current_311_dataset(*, app_token=None, session=None, fallback="2cy6-i7zn") -> str:
    """Resolve the current 'MyLA311 Cases {year}' dataset id via the LA data catalog.

    ``GET https://data.lacity.org/api/catalog/v1?q=MyLA311 Cases`` and pick the result
    whose name matches ``MyLA311 Cases {year}``, preferring the most recent year present.
    Returns that item's 4x4 id. On ANY failure (network, non-200, no match, malformed)
    returns ``fallback`` (``2cy6-i7zn`` = 'MyLA311 Cases 2026' at freeze time).

    Per the registry note, the retired ``...Service Request Data {year}`` series is NOT
    constructed here — only the 'Cases' product is matched.
    """
    sess = _resolve_session(session)
    params = {"q": "MyLA311 Cases", "limit": 20}
    try:
        payload = _get_json(
            sess, _LA_CATALOG_URL, params=params, headers=_headers(app_token), timeout=120
        )
    except SocrataError:
        return fallback

    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return fallback

    best_year = -1
    best_id = None
    for item in results:
        if not isinstance(item, dict):
            continue
        # Catalog v1 nests the dataset under "resource"; name + id live there.
        resource = item.get("resource") if isinstance(item.get("resource"), dict) else item
        name = resource.get("name") or item.get("name") or ""
        if not isinstance(name, str):
            continue
        # Match 'MyLA311 Cases <4-digit-year>' (case-insensitive); skip 'Service Request
        # Data', date-range bridge files ('... March 2025 to December 2025'), etc.
        year = _extract_cases_year(name)
        if year is None:
            continue
        ds_id = resource.get("id") or item.get("id")
        if not isinstance(ds_id, str) or not ds_id:
            continue
        if year > best_year:
            best_year = year
            best_id = ds_id

    return best_id if best_id else fallback


def _extract_cases_year(name: str) -> Optional[int]:
    """If ``name`` is exactly a 'MyLA311 Cases {year}' title, return the int year; else None.

    Deliberately strict: the title must contain 'MyLA311 Cases' followed by a 4-digit
    year as the trailing token, so date-range bridge files ('MyLA311 Cases March 2025 to
    December 2025') and the retired 'Service Request Data' series do not match.
    """
    low = name.strip().lower()
    marker = "myla311 cases"
    if marker not in low:
        return None
    tail = low.split(marker, 1)[1].strip()
    # The remaining tail must be a bare 4-digit year (e.g. "2026").
    if len(tail) == 4 and tail.isdigit():
        year = int(tail)
        if 2000 <= year <= 2100:
            return year
    return None


def feed_series(feed_cfg, host, *, app_token=None, since=None, session=None) -> list[dict]:
    """High-level per-feed entry point. ``feed_cfg`` is one feed dict from the resolved
    registry. Returns one merged ascending ``[{"month","n"}]`` series for the feed.

    Handles, in combination:

    * ``baseline_dataset`` union: fetch BOTH the primary ``dataset`` and ``baseline_dataset``
      and sum ``n`` per month (NYC complaints 5uac-w243 ∪ qgea-i56i; LA crime
      k7nn-b2ep ∪ y8y3-fqfu).
    * ``dataset_rotates_yearly`` (LA 311): resolve the current primary dataset via
      ``la_current_311_dataset()`` instead of the static ``dataset`` id, then still union
      with ``baseline_dataset``.
    * text date: when ``date_col_status == 'text_not_date'`` (NYC DOB ``issuance_date``),
      query with ``date_is_text=True`` and the feed's ``date_text_format``.

    ``app_token`` defaults to ``os.environ['SOCRATA_APP_TOKEN']`` when not passed (still
    injectable for tests). ``since`` is threaded to every underlying ``monthly_counts`` call
    — pass it for the big tables (NYC 311, Chicago crime).
    """
    if app_token is None:
        app_token = os.environ.get("SOCRATA_APP_TOKEN")

    date_col = feed_cfg["date_col"]
    date_is_text = feed_cfg.get("date_col_status") == "text_not_date"
    text_fmt = feed_cfg.get("date_text_format", "MM/DD/YYYY")

    # Resolve the primary dataset id. LA 311 rotates yearly -> catalog-resolve it.
    if feed_cfg.get("dataset_rotates_yearly"):
        primary = la_current_311_dataset(
            app_token=app_token,
            session=session,
            fallback=feed_cfg.get("dataset", "2cy6-i7zn"),
        )
    else:
        primary = feed_cfg["dataset"]

    datasets = [primary]
    baseline = feed_cfg.get("baseline_dataset")
    if baseline and baseline not in datasets:
        datasets.append(baseline)

    series_list = [
        monthly_counts(
            host,
            ds,
            date_col,
            app_token=app_token,
            since=since,
            date_is_text=date_is_text,
            text_fmt=text_fmt,
            session=session,
        )
        for ds in datasets
    ]

    # Single dataset: _parse_rows already returned a clean ascending series. Multiple:
    # merge/sum by month. _merge_series is order-stable and ascending either way.
    if len(series_list) == 1:
        return series_list[0]
    return _merge_series(series_list)
