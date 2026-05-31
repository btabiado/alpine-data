"""ArcGIS FeatureServer adapter for the City tab (Miami = Miami-Dade County).

Miami-Dade publishes its open data on an ArcGIS Hub org
(``gis-mdc.opendata.arcgis.com``) backed by ArcGIS FeatureServers, not Socrata.
Two feeds matter here (the third Miami feed is FBI/CDE — a different adapter):

  * **Building permits (LIVE).** Layer
    ``.../BuildingPermit_gdb/FeatureServer/0`` with an ``esriFieldTypeDate``
    column ``ISSUDATE`` spanning 1982 -> present. ArcGIS has no
    ``date_trunc``, so monthly counts are assembled with one
    ``returnCountOnly`` query per calendar month over a date-range predicate
    (``ISSUDATE >= TIMESTAMP 'YYYY-MM-01 00:00:00' AND ISSUDATE < <next-month>``).

  * **311 service requests (STALE).** Table
    ``.../data_311_2023/FeatureServer/0`` is a *frozen 2023 yearly snapshot*
    (``ticket_created_date_time`` ranges 2023-01-01 -> 2024-01-01). No current
    County 311 feed exists. It carries a precomputed integer month bucket
    ``created_year_month`` (``yyyymm``), so monthly counts come from a single
    grouped-statistics query — no per-month loop and no date math needed.

Live-probe findings (2026-05-31, keyless):
  * The ANSI ``TIMESTAMP 'YYYY-MM-DD HH:MM:SS'`` date literal works against
    this server (so do ``DATE 'YYYY-MM-DD'`` and a date-only ``TIMESTAMP``,
    all returning identical counts). We use the full
    ``TIMESTAMP 'YYYY-MM-DD HH:MM:SS'`` form because it is the most explicit
    and matches the FeatureServer field type (``esriFieldTypeDate`` carries a
    time component).
  * ``created_year_month`` integers for Jan-Sep come back WITHOUT zero padding
    (``20231`` = January 2023), so we format from the integer arithmetically
    (``ym // 100`` / ``ym % 100``) rather than slicing a string.

Public surface:
  * :class:`ArcGISError`
  * :func:`permits_monthly`
  * :func:`snapshot_311_monthly`
  * :func:`feed_series`

All network functions accept a ``session=`` for injection (tests pass a fake);
they default to a module-level :class:`requests.Session`.
"""
from __future__ import annotations

from typing import Optional

import requests

__all__ = [
    "ArcGISError",
    "permits_monthly",
    "snapshot_311_monthly",
    "feed_series",
]


class ArcGISError(Exception):
    """Raised when an ArcGIS FeatureServer request fails or returns an error.

    ArcGIS reports query errors *inside* an HTTP 200 body as
    ``{"error": {"code": ..., "message": ..., "details": [...]}}`` rather than
    via the HTTP status, so this is raised both for transport failures and for
    that in-band error envelope.
    """


# Module-level default session (connection pooling + keep-alive). Callers may
# override per-call via ``session=``; tests inject a fake with a ``.get``.
_SESSION = requests.Session()


# ---------------------------------------------------------------------------
# Small month helpers (no third-party date dep needed)
# ---------------------------------------------------------------------------
def _parse_ym(ym: str) -> tuple[int, int]:
    """'YYYY-MM' -> (year, month). Raises ArcGISError on malformed input."""
    try:
        year_s, month_s = str(ym).split("-")
        year, month = int(year_s), int(month_s)
    except (ValueError, AttributeError) as exc:
        raise ArcGISError(f"invalid month string {ym!r} (want 'YYYY-MM')") from exc
    if not 1 <= month <= 12:
        raise ArcGISError(f"invalid month {ym!r}: month out of range")
    return year, month


def _fmt_ym(year: int, month: int) -> str:
    """(year, month) -> zero-padded 'YYYY-MM'."""
    return f"{year:04d}-{month:02d}"


def _split_yyyymm(ym: int) -> tuple[int, int]:
    """Decompose an ArcGIS ``created_year_month`` integer into (year, month).

    The live snapshot returns this field UNPADDED for single-digit months:
    January 2023 arrives as the 5-digit integer ``20231`` (not ``202301``),
    while October 2023 is the 6-digit ``202310``. So a naive ``ym // 100`` /
    ``ym % 100`` mis-reads ``20231`` as year 202, month 31.

    Robust rule: the first 4 digits are the year; the remaining 1-2 digits are
    the month. Works for both the unpadded 5-digit and padded 6-digit forms.
    """
    s = str(int(ym))
    if len(s) not in (5, 6):
        raise ArcGISError(f"311 bucket {ym!r} is not a yyyymm (got {len(s)} digits)")
    year, month = int(s[:4]), int(s[4:])
    if not 1 <= month <= 12:
        raise ArcGISError(f"311 bucket {ym} is not a valid yyyymm (month={month})")
    return year, month


def _next_month(year: int, month: int) -> tuple[int, int]:
    """Return the first month after (year, month), rolling Dec -> Jan."""
    if month == 12:
        return year + 1, 1
    return year, month + 1


def _iter_months(since: str, until: str):
    """Yield (year, month) for each month in [since..until] inclusive, ascending.

    ``since``/``until`` are 'YYYY-MM' strings. Raises if since > until.
    """
    sy, sm = _parse_ym(since)
    uy, um = _parse_ym(until)
    if (sy, sm) > (uy, um):
        raise ArcGISError(f"since {since!r} is after until {until!r}")
    y, m = sy, sm
    while (y, m) <= (uy, um):
        yield y, m
        y, m = _next_month(y, m)


def _request_json(session, url: str, params: dict, timeout: int) -> dict:
    """GET ``url`` with ``params`` and return parsed JSON.

    Raises :class:`ArcGISError` on transport failure, non-JSON bodies, or the
    in-band ``{"error": ...}`` envelope ArcGIS returns inside HTTP 200.
    """
    sess = session if session is not None else _SESSION
    try:
        resp = sess.get(url, params=params, timeout=timeout)
    except requests.RequestException as exc:
        raise ArcGISError(f"ArcGIS request to {url} failed: {exc}") from exc

    # Surface real HTTP errors (the FeatureServer mostly uses 200 + in-band
    # error, but guard the genuine 4xx/5xx path too).
    status = getattr(resp, "status_code", 200)
    if status >= 400:
        raise ArcGISError(f"ArcGIS request to {url} returned HTTP {status}")

    try:
        data = resp.json()
    except ValueError as exc:
        raise ArcGISError(f"ArcGIS response from {url} was not JSON") from exc

    if isinstance(data, dict) and "error" in data:
        err = data["error"] or {}
        code = err.get("code", "?")
        message = err.get("message") or ""
        details = "; ".join(err.get("details", []) or [])
        raise ArcGISError(
            f"ArcGIS error from {url}: code={code} {message} {details}".strip()
        )
    return data


# ---------------------------------------------------------------------------
# Permits: per-month date-range count loop
# ---------------------------------------------------------------------------
def permits_monthly(
    layer_url: str,
    date_field: str,
    *,
    since: str,
    until: str,
    timeout: int = 120,
    session=None,
) -> list[dict]:
    """Monthly permit counts via a per-month date-range loop.

    ArcGIS has no ``date_trunc``, so for each month ``M`` in ``[since..until]``
    inclusive we issue::

        GET {layer_url}/query
            ?where=({date_field} >= TIMESTAMP 'YYYY-MM-01 00:00:00'
                    AND {date_field} < TIMESTAMP '<next-month>-01 00:00:00'
                    AND {date_field} IS NOT NULL)
            &returnCountOnly=true
            &f=json

    The ``IS NOT NULL`` clause is required: ArcGIS permit layers carry a large
    null/un-issued bucket that would otherwise leak across boundaries.

    Args:
        layer_url: FeatureServer layer URL, e.g.
            ``.../BuildingPermit_gdb/FeatureServer/0`` (no trailing ``/query``).
        date_field: the ``esriFieldTypeDate`` column to bucket on (``ISSUDATE``).
        since, until: inclusive 'YYYY-MM' bounds.
        timeout: per-request timeout (seconds).
        session: optional injected HTTP session (defaults to module session).

    Returns:
        ``[{"month": "YYYY-MM", "n": int}, ...]`` ascending by month.
    """
    layer_url = layer_url.rstrip("/")
    query_url = f"{layer_url}/query"

    out: list[dict] = []
    for year, month in _iter_months(since, until):
        ny, nm = _next_month(year, month)
        lo = f"{year:04d}-{month:02d}-01 00:00:00"
        hi = f"{ny:04d}-{nm:02d}-01 00:00:00"
        where = (
            f"{date_field} >= TIMESTAMP '{lo}' "
            f"AND {date_field} < TIMESTAMP '{hi}' "
            f"AND {date_field} IS NOT NULL"
        )
        params = {
            "where": where,
            "returnCountOnly": "true",
            "f": "json",
        }
        data = _request_json(session, query_url, params, timeout)
        count = data.get("count")
        if count is None:
            raise ArcGISError(
                f"permits count query for {_fmt_ym(year, month)} returned no "
                f"'count' field: {data!r}"
            )
        out.append({"month": _fmt_ym(year, month), "n": int(count)})
    return out


# ---------------------------------------------------------------------------
# 311 stale snapshot: single grouped-statistics query
# ---------------------------------------------------------------------------
def snapshot_311_monthly(
    layer_url: str,
    *,
    month_bucket_field: str = "created_year_month",
    object_id_field: str = "ObjectId",
    timeout: int = 120,
    session=None,
) -> list[dict]:
    """Monthly counts for the stale 2023 311 snapshot via grouped statistics.

    The snapshot table carries a precomputed integer ``yyyymm`` bucket
    (``created_year_month``), so one grouped-count query yields every month
    without a per-month loop or any date literal::

        GET {layer_url}/query
            ?where=1=1
            &groupByFieldsForStatistics={month_bucket_field}
            &outStatistics=[{"statisticType":"count",
                             "onStatisticField":"<object_id_field>",
                             "outStatisticFieldName":"n"}]
            &f=json

    Integer ``yyyymm`` values for Jan-Sep arrive WITHOUT zero padding
    (``20231`` == January 2023), so the 'YYYY-MM' key is computed
    arithmetically rather than by string slicing.

    Args:
        layer_url: FeatureServer table URL (``.../data_311_2023/FeatureServer/0``).
        month_bucket_field: integer ``yyyymm`` field to group on.
        object_id_field: field counted by the statistic (case matters — this
            table exposes ``ObjectId``, not ``OBJECTID``).
        timeout: per-request timeout (seconds).
        session: optional injected HTTP session.

    Returns:
        ``[{"month": "YYYY-MM", "n": int}, ...]`` ascending by month, skipping
        any null bucket.
    """
    layer_url = layer_url.rstrip("/")
    query_url = f"{layer_url}/query"

    # Build outStatistics as a stable JSON string (avoids dict-ordering churn).
    out_statistics = (
        '[{"statisticType":"count",'
        f'"onStatisticField":"{object_id_field}",'
        '"outStatisticFieldName":"n"}]'
    )
    params = {
        "where": "1=1",
        "groupByFieldsForStatistics": month_bucket_field,
        "outStatistics": out_statistics,
        "f": "json",
    }
    data = _request_json(session, query_url, params, timeout)

    features = data.get("features")
    if features is None:
        raise ArcGISError(
            f"311 grouped query returned no 'features': {data!r}"
        )

    buckets: list[tuple[int, int]] = []
    for feat in features:
        attrs = feat.get("attributes", {}) if isinstance(feat, dict) else {}
        ym = attrs.get(month_bucket_field)
        n = attrs.get("n")
        if ym is None or n is None:
            # Null month bucket or empty group — skip rather than emit a
            # bogus 'None' month.
            continue
        buckets.append((int(ym), int(n)))

    buckets.sort(key=lambda t: t[0])
    out: list[dict] = []
    for ym, n in buckets:
        year, month = _split_yyyymm(ym)
        out.append({"month": _fmt_ym(year, month), "n": n})
    return out


# ---------------------------------------------------------------------------
# Per-feed dispatch from a resolved-registry Miami feed dict
# ---------------------------------------------------------------------------
def feed_series(
    feed_cfg: dict,
    *,
    since: Optional[str] = None,
    until: Optional[str] = None,
    timeout: int = 120,
    session=None,
) -> tuple[list[dict], str]:
    """Resolve one Miami feed dict to ``(series, status)``.

    Dispatch by the registry's ``date_col_status`` / ``adapter`` so the caller
    just hands over the feed block from ``city_registry.resolved.json``:

      * permits (``date_col_status == 'confirmed'``) ->
        ``(permits_monthly(...), 'ok')``. Requires ``since``/``until``.
      * 311 (``date_col_status == 'stale_source'``) ->
        ``(snapshot_311_monthly(...), 'stale')``. ``since``/``until`` ignored —
        the snapshot is a fixed 2023 year.
      * FBI feed (``adapter == 'fbi'``) -> ``([], 'not_published')`` — handled
        by a different adapter; we never hit the network for it.

    Uses ``feed_cfg['endpoint']`` as the layer URL, ``feed_cfg['date_col']`` as
    the date field, ``feed_cfg.get('object_id_field')``, and
    ``feed_cfg.get('month_bucket_field')``.

    Returns:
        ``(series, status)`` where ``status`` is one of
        ``'ok' | 'stale' | 'not_published'``.
    """
    # FBI / non-ArcGIS feed: not our job, and there is nothing to fetch.
    if feed_cfg.get("adapter") == "fbi":
        return [], "not_published"

    status_flag = feed_cfg.get("date_col_status")
    endpoint = feed_cfg.get("endpoint")
    date_field = feed_cfg.get("date_col")

    if status_flag == "stale_source":
        if not endpoint:
            raise ArcGISError("311 feed_cfg missing 'endpoint'")
        bucket_field = feed_cfg.get("month_bucket_field") or "created_year_month"
        oid = feed_cfg.get("object_id_field") or "ObjectId"
        series = snapshot_311_monthly(
            endpoint,
            month_bucket_field=bucket_field,
            object_id_field=oid,
            timeout=timeout,
            session=session,
        )
        return series, "stale"

    if status_flag == "confirmed":
        if not endpoint:
            raise ArcGISError("permits feed_cfg missing 'endpoint'")
        if not date_field:
            raise ArcGISError("permits feed_cfg missing 'date_col'")
        if since is None or until is None:
            raise ArcGISError(
                "permits feed_series requires since= and until= 'YYYY-MM' bounds"
            )
        series = permits_monthly(
            endpoint,
            date_field,
            since=since,
            until=until,
            timeout=timeout,
            session=session,
        )
        return series, "ok"

    raise ArcGISError(
        f"unhandled feed_cfg shape: adapter={feed_cfg.get('adapter')!r} "
        f"date_col_status={status_flag!r}"
    )
