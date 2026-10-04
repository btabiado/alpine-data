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
* Union feeds (``baseline_dataset``): NYC complaints 5uac-w243 union qgea-i56i.
  ``feed_series`` fetches both and sums ``n`` per month. (LA crime used to union
  k7nn-b2ep with y8y3-fqfu; LA consolidated every NIBRS offense into k7nn-b2ep on
  2026-08-18 and y8y3-fqfu now answers HTTP 403 "You must be logged in", which
  failed the whole feed every night. The registry no longer names it.)
* LA 311 rotates yearly (``dataset_rotates_yearly``): LA publishes one dataset per
  calendar year, titled ``MyLA311 Cases {year}`` (do NOT construct the retired
  ``...Service Request Data {year}`` ids), plus the ``baseline_dataset`` bridge
  file for 2025 (Mar..Dec). ``feed_series`` stitches one dataset per calendar
  year across the whole requested window, each catalog-resolved by title — see
  ``_la_311_year_plan`` for why the newest year alone is not enough.
* Auth: one free ``SOCRATA_APP_TOKEN`` is portal-agnostic across all 5 hosts; passed as
  the ``X-App-Token`` header. Keyless works for small queries but throttles (429) on
  large tables.
"""
from __future__ import annotations

import calendar
import os
import sys
import time
from datetime import date, datetime, timezone
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

# Calendar years of the two ids the registry names for LA 311, used when a feed
# block does not say (``dataset_year`` / ``baseline_dataset_year``). ``dataset``
# 2cy6-i7zn is 'MyLA311 Cases 2026'; ``baseline_dataset`` 73a2-6ar5 is 'MyLA311
# Cases March 2025 to December 2025'. Years after the baseline year are
# catalog-resolved; the registry id is only the fallback for its own year.
_LA_311_DATASET_YEAR = 2026
_LA_311_BASELINE_YEAR = 2025

# Transient-failure retry policy for _get_json: HTTP statuses worth a second try
# (throttle + upstream/gateway hiccups) and the sleep before each retry. Three
# attempts total. Kept small: the scheduled job fetches ~25 datasets serially.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_RETRY_BACKOFF_S = (3, 10)

# Sleep hook so tests can run the retry path without waiting.
_sleep = time.sleep

# A month is treated as complete when its newest record falls within this many
# days of the month's last calendar day. Two days absorbs a weekend for
# weekday-only feeds (permits issued Mon-Fri) without accepting a feed that
# stopped mid-month. See _incomplete_tail_month.
_COMPLETE_MONTH_SLACK_DAYS = 2


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


def _day_of(raw_last, month: str) -> Optional[int]:
    """Day-of-month of a per-bucket ``max(date_col)`` value, or ``None``.

    Handles both shapes Socrata returns: a floating timestamp
    (``2026-09-19T00:00:00.000``) and the NYC DOB text date (``09/19/2026``).
    The value must belong to ``month`` (``YYYY-MM``); anything else (junk rows
    in a text column, a malformed value) yields ``None`` so it is ignored.
    """
    if raw_last is None:
        return None
    s = str(raw_last).strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":          # YYYY-MM-DD...
        ym, day = s[:7], s[8:10]
    elif len(s) >= 10 and s[2] == "/" and s[5] == "/":        # MM/DD/YYYY
        ym, day = f"{s[6:10]}-{s[0:2]}", s[3:5]
    else:
        return None
    if ym != month or not day.isdigit():
        return None
    return int(day)


def _incomplete_tail_month(rows) -> Optional[str]:
    """Return the newest month in ``rows`` if its data stops short of month-end.

    ``rows`` are the raw aggregation rows, which carry ``last`` = the newest
    record date inside each month bucket. Only the NEWEST month can be partial:
    any earlier month is followed by later data, so the portal had already moved
    past it. The newest month is partial when its last record lands more than
    ``_COMPLETE_MONTH_SLACK_DAYS`` before the month's final day.

    Why this matters: the build scores the last COMPLETE calendar month, but a
    feed that lags upstream has not finished that month yet. On 2026-10-04
    LAPD's NIBRS file ran to 09-19, so September held 10,097 offenses against a
    ~18,300/month baseline — a fake 45% crime drop that clips z to the max and
    hands LA a free Public Safety boost. NYC's crash file stopped on 06-11
    (upstream paused); scoring that June as a full month is the same lie.

    Returns ``None`` when nothing is provably partial (no ``last`` values, e.g.
    an older caller's canned rows, or the newest month runs to month-end).
    """
    if not isinstance(rows, list):
        return None
    newest, newest_last = None, None
    for row in rows:
        if not isinstance(row, dict):
            continue
        month = _normalize_month(row.get("m"))
        if month is None:
            continue
        if newest is None or month > newest:
            newest, newest_last = month, row.get("last")
    if newest is None:
        return None
    day = _day_of(newest_last, newest)
    if day is None:
        return None
    month_len = calendar.monthrange(int(newest[:4]), int(newest[5:7]))[1]
    if day < month_len - _COMPLETE_MONTH_SLACK_DAYS:
        return newest
    return None


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
    ``SocrataError``. 429 (throttle) is called out explicitly.

    Transient failures are retried (see ``_RETRY_STATUSES`` / ``_RETRY_BACKOFF_S``)
    before giving up. Without a ``SOCRATA_APP_TOKEN`` the portals throttle
    unauthenticated callers, and the city-daily logs show that as a different
    feed dying each night (SF 311 read timeout on 2026-10-03, LA 311 baseline
    read timeout on 2026-10-02) while the same query answers in seconds when
    re-run. A 403 is never retried: it is either permanent (JSON "You must be
    logged in" = retired/private dataset) or the old data.sfgov.org front end
    refusing us (nginx 403 on 2026-09-30), which the move to the canonical
    data.sf.gov host addresses instead. 404 / 400 (bad SoQL) are not retried.
    """
    attempts = len(_RETRY_BACKOFF_S) + 1
    for attempt in range(attempts):
        last_attempt = attempt == attempts - 1
        try:
            resp = session.get(url, params=params, headers=headers, timeout=timeout)
        except requests.RequestException as exc:  # network error, timeout, etc.
            if not last_attempt:
                _sleep(_RETRY_BACKOFF_S[attempt])
                continue
            raise SocrataError(
                f"Socrata request to {url} failed after {attempts} attempts: {exc}"
            ) from exc
        status = getattr(resp, "status_code", None)
        if status in _RETRY_STATUSES and not last_attempt:
            _sleep(_RETRY_BACKOFF_S[attempt])
            continue
        break

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
            pass  # response body is optional context for the error raised below
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

    Each bucket also selects ``max({date_col}) AS last`` (same request, no extra
    round trip) so a newest month that the portal has not finished publishing is
    dropped rather than scored as a full month — see ``_incomplete_tail_month``.

    ``text_fmt`` is accepted for interface/forward-compat; the substring positions are
    fixed to ``MM/DD/YYYY`` (the only text format in the resolved registry).
    """
    sess = _resolve_session(session)

    select_expr = (
        f"{_month_bucket_expr(date_col, date_is_text=date_is_text)}, count(*) AS n, "
        f"max({date_col}) AS last"
    )
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
    series = _parse_rows(payload)
    partial = _incomplete_tail_month(payload)
    if partial is not None:
        # The calendar month in progress is always partial and is past the
        # build's cutoff anyway; only an EARLIER unfinished month (upstream lag
        # or a stalled feed) is worth a log line.
        if partial < datetime.now(timezone.utc).strftime("%Y-%m"):
            print(f"  [socrata] {host}/{dataset}: newest month {partial} stops "
                  f"before month-end upstream; treating it as incomplete (not "
                  f"scored)", file=sys.stderr)
        series = [row for row in series if row["month"] != partial]
    return series


def la_311_datasets_by_year(*, app_token=None, session=None) -> Optional[dict]:
    """Catalog-resolve every 'MyLA311 Cases {year}' dataset as ``{year: id}``.

    ``GET https://data.lacity.org/api/catalog/v1?q=MyLA311 Cases`` and keep each result
    whose name is exactly ``MyLA311 Cases {year}`` (see ``_extract_cases_year``). If a
    year is listed twice, the first (most relevant) result wins.

    Returns ``None`` when the catalog could not be read (network, non-200 after the
    ``_get_json`` retries, malformed payload) so the caller can fall back to the
    registry ids, and ``{}`` when the catalog answered but listed no matching title.

    Per the registry note, the retired ``...Service Request Data {year}`` series is NOT
    constructed here — only the 'Cases' product is matched. The live items are Socrata
    filtered views (catalog ``type: filter``) of a private parent table, so results are
    deliberately not filtered by type.
    """
    sess = _resolve_session(session)
    params = {"q": "MyLA311 Cases", "limit": 100}
    try:
        payload = _get_json(
            sess, _LA_CATALOG_URL, params=params, headers=_headers(app_token), timeout=120
        )
    except SocrataError:
        return None

    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return None

    by_year: dict[int, str] = {}
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
        by_year.setdefault(year, ds_id)
    return by_year


def la_current_311_dataset(*, app_token=None, session=None, fallback="2cy6-i7zn") -> str:
    """The newest 'MyLA311 Cases {year}' dataset id in the LA catalog.

    Returns ``fallback`` (``2cy6-i7zn`` = 'MyLA311 Cases 2026' at freeze time) on ANY
    failure (network, non-200, no match, malformed). ``feed_series`` does not use this:
    the newest year on its own drops every earlier year (see ``_la_311_year_plan``).
    """
    by_year = la_311_datasets_by_year(app_token=app_token, session=session)
    if not by_year:
        return fallback
    return by_year[max(by_year)]


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


def _normalize_since(since) -> Optional[str]:
    """``since`` as ``YYYY-MM-DD`` (bare year -> Jan-1, ``YYYY-MM`` -> the 1st), or None."""
    if not since:
        return None
    s = str(since).strip()
    if len(s) == 4 and s.isdigit():
        return f"{s}-01-01"
    if len(s) == 7:
        return f"{s}-01"
    return s[:10]


def _la_311_year_plan(feed_cfg, *, since, today, by_year) -> list[tuple[int, str]]:
    """Pick the dataset for each calendar year of the window: ``[(year, id)]``, newest first.

    WHY ONE DATASET PER YEAR. LA publishes 311 as one dataset per calendar year. This
    used to resolve only the NEWEST 'MyLA311 Cases {year}' and union it with the 2025
    bridge file. That holds while the newest year is 2026 and breaks the day 'MyLA311
    Cases 2027' appears: the union becomes 2027 + Mar..Dec 2025, all of 2026 drops
    out, the 12 months before the scored month go missing, and LA City Services sits
    at ``insufficient_history`` for the whole of 2027.

    For every calendar year from ``since`` through ``today``:

    * the baseline year (2025) comes from ``baseline_dataset``, never from a catalog
      title, so a 'MyLA311 Cases 2025' item can't double count the bridge file;
    * a later year comes from the catalog title 'MyLA311 Cases {year}'. The registry
      ``dataset`` covers its own year (2026) when the catalog is down or lost it;
    * a current year that is not published yet is skipped and the previous year's
      dataset carries the window. That is the normal state for the first weeks of
      January (the 2026 view was created on 2026-01-13), and in January the scored
      month (last month) lives in the previous year's dataset anyway;
    * years before the baseline year have no 'Cases' data and are skipped quietly.
    """
    baseline = feed_cfg.get("baseline_dataset")
    baseline_year = int(feed_cfg.get("baseline_dataset_year", _LA_311_BASELINE_YEAR))
    floor = baseline_year if baseline else None

    known: dict[int, str] = {}
    if feed_cfg.get("dataset"):
        known[int(feed_cfg.get("dataset_year", _LA_311_DATASET_YEAR))] = feed_cfg["dataset"]
    for year, ds_id in by_year.items():
        if floor is None or year > floor:
            known[year] = ds_id
    if baseline:
        known[baseline_year] = baseline

    first = int(since[:4]) if since else min(known, default=today.year)
    plan = []
    for year in range(today.year, first - 1, -1):
        ds_id = known.get(year)
        if ds_id is not None:
            plan.append((year, ds_id))
        elif year == today.year:
            print(f"  [socrata] 'MyLA311 Cases {year}' is not in the LA catalog yet; "
                  f"using {year - 1} and earlier", file=sys.stderr)
        elif floor is None or year > floor:
            print(f"  [socrata] no 'MyLA311 Cases {year}' dataset in the LA catalog; "
                  f"{year} months are missing from LA 311", file=sys.stderr)
    return plan


def _la_311_series(feed_cfg, host, *, app_token, since, today, session,
                   date_is_text, text_fmt) -> list[dict]:
    """Stitch LA 311 from one dataset per calendar year (see ``_la_311_year_plan``).

    Each dataset is queried for its own calendar year only and its rows are then
    filtered to that year, so every month comes from exactly one dataset. The upper
    bound also keeps the newest month in each response inside that year, so
    ``monthly_counts``' partial-month check looks at the right month.
    """
    by_year = la_311_datasets_by_year(app_token=app_token, session=session)
    if by_year is None:
        print("  [socrata] LA catalog search for 'MyLA311 Cases {year}' failed; "
              "falling back to the registry dataset ids", file=sys.stderr)
    since_norm = _normalize_since(since)
    plan = _la_311_year_plan(feed_cfg, since=since_norm, today=today, by_year=by_year or {})

    date_col = feed_cfg["date_col"]
    series_list = []
    for year, ds_id in plan:
        year_start = f"{year:04d}-01-01"
        lower = max(since_norm, year_start) if since_norm else year_start
        # A text date column can't be range-compared with '<'; the year filter
        # below still keeps each month to one dataset.
        upper = None if date_is_text else f"{date_col} < '{year + 1:04d}-01-01'"
        rows = monthly_counts(
            host,
            ds_id,
            date_col,
            app_token=app_token,
            since=lower,
            date_is_text=date_is_text,
            text_fmt=text_fmt,
            extra_where=upper,
            session=session,
        )
        prefix = f"{year:04d}-"
        series_list.append([row for row in rows if row["month"].startswith(prefix)])
    return _merge_series(series_list)


def feed_series(feed_cfg, host, *, app_token=None, since=None, session=None,
                today: Optional[date] = None) -> list[dict]:
    """High-level per-feed entry point. ``feed_cfg`` is one feed dict from the resolved
    registry. Returns one merged ascending ``[{"month","n"}]`` series for the feed.

    Handles, in combination:

    * ``baseline_dataset`` union: fetch BOTH the primary ``dataset`` and ``baseline_dataset``
      and sum ``n`` per month (NYC complaints 5uac-w243 ∪ qgea-i56i; LA crime
      formerly k7nn-b2ep ∪ y8y3-fqfu, retired 2026-08 — see module docstring).
    * ``dataset_rotates_yearly`` (LA 311): stitch one dataset per calendar year from
      ``since`` through ``today`` — each later year catalog-resolved by title, the
      ``baseline_dataset`` for 2025 — with each month taken from exactly one dataset.
      See ``_la_311_year_plan``.
    * text date: when ``date_col_status == 'text_not_date'`` (NYC DOB ``issuance_date``),
      query with ``date_is_text=True`` and the feed's ``date_text_format``.

    ``app_token`` defaults to ``os.environ['SOCRATA_APP_TOKEN']`` when not passed (still
    injectable for tests). ``since`` is threaded to every underlying ``monthly_counts`` call
    — pass it for the big tables (NYC 311, Chicago crime). ``today`` (a ``date``; default
    the UTC clock) only decides which yearly datasets a rotating feed needs.
    """
    if app_token is None:
        app_token = os.environ.get("SOCRATA_APP_TOKEN")

    date_col = feed_cfg["date_col"]
    date_is_text = feed_cfg.get("date_col_status") == "text_not_date"
    text_fmt = feed_cfg.get("date_text_format", "MM/DD/YYYY")

    if feed_cfg.get("dataset_rotates_yearly"):
        if today is None:
            today = datetime.now(timezone.utc).date()
        return _la_311_series(
            feed_cfg, host, app_token=app_token, since=since, today=today,
            session=session, date_is_text=date_is_text, text_fmt=text_fmt,
        )

    datasets = [feed_cfg["dataset"]]
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
