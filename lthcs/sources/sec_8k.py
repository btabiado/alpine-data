"""SEC EDGAR 8-K material event filter for LTHCS.

Pulls recent 8-K filings per ticker from the SEC EDGAR submissions
endpoint and emits structured event records the LTHCS Thesis Integrity
pillar can consume. 8-K filings are a HIGH-INFORMATION, structured
complement to news-sentiment streams: a CEO departure or restatement is
a discrete, dated event — not a vibe.

Endpoints used:
    https://data.sec.gov/submissions/CIK{padded_cik}.json
        Recent filings rollup. The ``filings.recent`` block lists every
        filing form (8-K, 10-K, 10-Q, ...) the company has made; we
        filter to 8-Ks within a configurable rolling window and parse
        the comma-separated ``items`` field into individual item codes.

Why this module is separate from ``sec_edgar.py``:
    ``sec_edgar.py`` is the XBRL company-facts client used by the
    Financial Evolution pillar. The submissions endpoint has different
    semantics (filings list, not facts), a different cache TTL (24h, not
    7d, because new 8-Ks land daily), and a different output schema, so
    it lives in its own module. We DO reuse ``get_cik``,
    ``_user_agent``, ``_headers``, and the rate-limit bucket — there's
    no reason to spin up a second 10 req/sec pool for the same API.

Cache:
    24h ``FileCache("sec_8k")`` keyed by ``submissions/{cik}``. The
    submissions JSON includes every recent filing, so a single fetch
    serves any ``days`` window — we filter in memory.

Rate limit:
    Shares ``sec_edgar._bucket`` (10 req/sec) so this module + the
    company-facts client together stay under the SEC's per-UA limit.

Auth:
    Inherits ``SEC_USER_AGENT`` enforcement from ``sec_edgar._user_agent``.
    Missing / blank => raise before any HTTP.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from lthcs.sources._cache import FileCache
from lthcs.sources.sec_edgar import (
    SECEdgarError,
    _bucket as _SEC_BUCKET,
    _headers,
    _user_agent,
    get_cik,
)


# --- Constants ---------------------------------------------------------------

_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"

# 24 hours. Filings can land any business day, so a daily TTL strikes
# the right balance between freshness and bandwidth.
_CACHE_TTL_SECONDS = 24 * 60 * 60

# High-signal threshold for ``summarize_events_for_thesis``: any 8-K item
# whose weight is at or above this counts toward
# ``high_signal_event_count``.
_HIGH_SIGNAL_WEIGHT = 0.6

# Sentiment-label histogram bucketing for ``summarize_events_for_thesis``.
# Mirrors the five-label scheme Alpha Vantage uses so the LTHCS Thesis
# pillar's downstream consumer doesn't need a separate branch.
_LABELS = ("Bearish", "Somewhat-Bearish", "Neutral", "Somewhat-Bullish", "Bullish")


# 8-K item-code weight map. Each entry: (weight 0..1, direction -1/0/+1, label).
# Weight = how strongly this event should move Thesis. Direction = sign
# of the typical sentiment for that item type. Both are V1 heuristics;
# see ``docs/news-feeds-earnings-events.md`` Section 3.1 for rationale.
ITEM_CODE_WEIGHTS: Dict[str, Tuple[float, int, str]] = {
    "1.01": (0.5, +1, "Material definitive agreement"),
    "1.02": (0.6, -1, "Termination of material agreement"),
    "1.03": (0.7, -1, "Bankruptcy or receivership"),
    "1.04": (0.5,  0, "Mine safety"),
    "2.01": (0.5,  0, "Completion of acquisition or disposition"),
    "2.02": (0.8,  0, "Results of operations + financial condition"),
    "2.03": (0.4, -1, "Material direct financial obligation"),
    "2.04": (0.7, -1, "Triggering events accelerating financial obligation"),
    "2.05": (0.6, -1, "Costs from exit or disposal activities"),
    "2.06": (0.7, -1, "Material impairment"),
    "3.01": (0.5,  0, "Notice of delisting / failure to satisfy listing"),
    "3.02": (0.4,  0, "Unregistered sale of equity"),
    "3.03": (0.5,  0, "Modification to rights of security holders"),
    "4.01": (0.5,  0, "Change in registrant's certifying accountant"),
    "4.02": (0.9, -1, "Non-reliance on previously-issued financials"),
    "5.01": (0.6,  0, "Changes in control of registrant"),
    "5.02": (0.6,  0, "Departure or appointment of officers"),
    "5.03": (0.3,  0, "Amendments to articles or bylaws"),
    "5.04": (0.3,  0, "Temporary suspension of trading"),
    "5.05": (0.4,  0, "Amendments to code of ethics"),
    "5.07": (0.1,  0, "Submission of matters to vote of security holders"),
    "5.08": (0.5,  0, "Shareholder director nominations"),
    "6.01": (0.3,  0, "ABS Informational"),
    "6.02": (0.3,  0, "Change of servicer or trustee"),
    "6.03": (0.3,  0, "Change in credit enhancement"),
    "6.04": (0.3,  0, "Failure to make required distribution"),
    "6.05": (0.3,  0, "Securities Act updating disclosure"),
    "7.01": (0.4,  0, "Regulation FD disclosure"),
    "8.01": (0.5,  0, "Other events"),
    "9.01": (0.1,  0, "Financial statements and exhibits"),
}


# --- Module state ------------------------------------------------------------

def _cache_root() -> Path:
    return Path(os.environ.get("LTHCS_CACHE_DIR", ".cache/lthcs"))


# Module-level singleton. Tests rebind this for isolation. We deliberately
# do NOT create a new TokenBucket — we share ``sec_edgar._bucket`` so the
# 10 req/sec SEC limit covers both clients combined.
_cache = FileCache("sec_8k", root=_cache_root())


# --- Helpers ----------------------------------------------------------------

def _today() -> date:
    """Indirection point so tests can patch ``date.today``-equivalent without
    monkeypatching the stdlib. Pure functions below take a ``today=`` kwarg
    where possible; this helper is just for the public-API default.
    """
    return date.today()


def _parse_items_field(items_raw: Any) -> List[str]:
    """Parse the SEC's ``items`` field into a list of canonical codes.

    SEC encodes the items on an 8-K as a comma-separated string like
    ``"2.02,9.01"`` or sometimes ``"5.02"`` (single) or ``""`` (none —
    a tiny minority of 8-Ks have no item-code listed). Split on commas,
    strip whitespace, drop empties. Preserves order so the *first* code
    in the filing is preserved (some downstream consumers care).

    Returns an empty list if ``items_raw`` is None, empty, or not a string.
    """
    if not items_raw or not isinstance(items_raw, str):
        return []
    out: List[str] = []
    for chunk in items_raw.split(","):
        code = chunk.strip()
        if code:
            out.append(code)
    return out


def _item_meta(code: str) -> Tuple[float, int, str]:
    """Return ``(weight, direction, label)`` for an item code.

    Unknown codes fall back to a low-weight, neutral-direction tuple with
    the raw code as the label. This means a newly-introduced SEC item
    code won't crash the pipeline; it'll just be down-weighted to noise
    until the map is updated.
    """
    return ITEM_CODE_WEIGHTS.get(code, (0.3, 0, "Item {} (unknown)".format(code)))


def _signum(x: float) -> int:
    if x > 0:
        return 1
    if x < 0:
        return -1
    return 0


def _parse_iso_date(s: str) -> Optional[date]:
    """Parse an ISO YYYY-MM-DD date string. Returns None on garbage."""
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _today_iso() -> str:
    return _today().isoformat()


def _label_for_direction(direction: int) -> str:
    """Map a -1/0/+1 direction summary to one of the five sentiment labels.

    The 8-K item weights only express three buckets; we widen to AV's
    five-label scheme so the histogram lines up with the Alpha Vantage /
    AI news sources downstream.
    """
    if direction > 0:
        return "Bullish"
    if direction < 0:
        return "Bearish"
    return "Neutral"


# --- HTTP fetch -------------------------------------------------------------

def _get_submissions_json(cik: str) -> Optional[Dict[str, Any]]:
    """Fetch + cache the submissions JSON for ``cik``.

    Returns the parsed body on success, ``None`` on non-200 or network
    exception. We swallow non-fatal errors here (rather than raising
    ``SECEdgarError`` like ``sec_edgar.get_company_facts`` does) because
    the public 8-K API in this module is best-effort: an 8-K outage
    shouldn't take down the whole Thesis pillar. The caller gets an
    empty list and the daily run continues.

    ``SEC_USER_AGENT`` is still required — that's a config bug, not a
    transient failure, and we want the test suite to surface it
    immediately rather than silently return empty.
    """
    cache_key = "submissions/{}".format(cik)
    hit = _cache.get(cache_key)
    if hit is not None:
        return hit.value

    # Resolve headers (and validate SEC_USER_AGENT) BEFORE taking a rate
    # token so a misconfigured caller doesn't burn budget.
    headers = _headers()
    _SEC_BUCKET.acquire()

    url = _SUBMISSIONS_URL.format(cik=cik)
    try:
        resp = requests.get(url, headers=headers, timeout=30)
    except requests.RequestException:
        # Network blew up. Treat as transient and return empty so the
        # daily run keeps moving.
        return None

    status = getattr(resp, "status_code", 0)
    if status != 200:
        return None

    try:
        data = resp.json()
    except (ValueError, Exception):  # noqa: BLE001 — keep this defensive
        return None

    if not isinstance(data, dict):
        return None

    _cache.set(cache_key, data, ttl_seconds=_CACHE_TTL_SECONDS)
    return data


# --- Filtering / parsing ----------------------------------------------------

def _iter_recent_rows(submissions: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Pivot the column-major ``filings.recent`` block into row dicts.

    SEC ships this section as parallel arrays (one array per column,
    same index = same filing). We zip them into a list of dicts so the
    downstream filter is easy to read. Missing columns / mismatched
    lengths are tolerated: each row only picks up the fields that exist
    at its index.
    """
    recent = (
        (submissions or {})
        .get("filings", {})
        .get("recent", {})
    )
    if not isinstance(recent, dict):
        return []

    forms = recent.get("form") or []
    if not isinstance(forms, list):
        return []
    n = len(forms)
    if n == 0:
        return []

    def _col(name: str) -> List[Any]:
        col = recent.get(name) or []
        return col if isinstance(col, list) else []

    accession = _col("accessionNumber")
    filing_date = _col("filingDate")
    items = _col("items")
    primary_doc = _col("primaryDocument")

    rows: List[Dict[str, Any]] = []
    for i in range(n):
        rows.append({
            "form": forms[i] if i < len(forms) else None,
            "accessionNumber": accession[i] if i < len(accession) else None,
            "filingDate": filing_date[i] if i < len(filing_date) else None,
            "items": items[i] if i < len(items) else "",
            "primaryDocument": primary_doc[i] if i < len(primary_doc) else None,
        })
    return rows


def _row_to_event(
    row: Dict[str, Any], ticker: str, cik: str
) -> Optional[Dict[str, Any]]:
    """Convert one ``filings.recent`` row to an event record.

    Returns None if the row is malformed (no date, no accession, etc.)
    or if it has no parseable item codes — but ONLY items-less rows are
    dropped. A row with one or more known item codes always survives.

    Weight = max(weight) across all items present.
    Direction = sign of the SUM of per-item directions (a single 4.02
    restatement dominates because it carries weight 0.9, but for the
    direction summary we use sign-of-sum which is a coarser bucket).
    """
    accession = row.get("accessionNumber")
    filing_date = row.get("filingDate")
    if not accession or not filing_date:
        return None

    codes = _parse_items_field(row.get("items"))
    # If there are NO item codes at all on an 8-K, we still emit the
    # event — some 8-Ks file with an empty items field (rare but real).
    # We use a 0/0/"Other" placeholder so downstream callers can still
    # see the filing existed.
    if codes:
        weights = []
        directions = []
        labels = []
        for c in codes:
            w, d, lbl = _item_meta(c)
            weights.append(w)
            directions.append(d)
            labels.append(lbl)
        weight = max(weights)
        direction = _signum(sum(directions))
    else:
        weight = 0.0
        direction = 0
        labels = []

    return {
        "ticker": ticker.upper(),
        "cik": cik,
        "filing_date": str(filing_date),
        "accession_number": str(accession),
        "items": list(codes),
        "item_labels": labels,
        "weight": float(weight),
        "direction": int(direction),
        "primary_document": str(row.get("primaryDocument") or ""),
    }


# --- Public API -------------------------------------------------------------

def get_recent_8k_events(
    ticker: str,
    days: int = 90,
    *,
    today: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """Return 8-K filings for ``ticker`` in the last ``days`` days, newest-first.

    Each event has the shape::

        {
          "ticker": "AAPL",
          "cik": "0000320193",
          "filing_date": "2026-05-15",
          "accession_number": "0000320193-25-000123",
          "items": ["2.02", "9.01"],
          "item_labels": ["Results of operations + financial condition",
                          "Financial statements and exhibits"],
          "weight": 0.8,          # max weight across items
          "direction": 0,          # sign-of-sum direction summary
          "primary_document": "aapl-20260515.htm",
        }

    Returns an empty list when:
      * ``ticker`` is empty / None
      * The ticker doesn't resolve to a CIK (unknown to SEC)
      * The submissions endpoint returns non-200 / errors
      * The company has no 8-Ks in the window

    Raises ``SECEdgarError`` only for config bugs (missing
    ``SEC_USER_AGENT``) — those are surfaced loudly rather than buried.

    Cached for 24h. The cache is keyed only by CIK (not by ``days``)
    because the submissions response is the same regardless of window;
    filtering happens in memory.
    """
    if not ticker:
        return []

    cik = get_cik(ticker)
    if cik is None:
        return []

    submissions = _get_submissions_json(cik)
    if not submissions:
        return []

    today_d = today if today is not None else _today()
    cutoff = today_d - timedelta(days=int(max(days, 0)))

    rows = _iter_recent_rows(submissions)
    events: List[Dict[str, Any]] = []
    for row in rows:
        # Filter out non-8-K filings. The submissions response includes
        # every form (10-K, 10-Q, 8-K, S-1, ...) so this filter is
        # essential — otherwise we'd return e.g. 10-Ks as "events".
        form = row.get("form")
        if not isinstance(form, str) or form.strip().upper() != "8-K":
            continue

        # Drop rows older than the window. ``filingDate`` is ISO
        # YYYY-MM-DD per SEC docs. Unparseable dates are dropped (we
        # could keep them, but a filing without a known date can't be
        # placed in any window so it's not useful for trend signal).
        fd = _parse_iso_date(row.get("filingDate"))
        if fd is None:
            continue
        if fd < cutoff:
            continue

        event = _row_to_event(row, ticker=ticker, cik=cik)
        if event is not None:
            events.append(event)

    # Newest-first ordering.
    events.sort(key=lambda e: e["filing_date"], reverse=True)
    return events


def summarize_events_for_thesis(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate a ticker's recent 8-K events into a Thesis-pillar payload.

    The weighted sentiment is::

        avg(weight * direction)  for events with weight > 0,
        scaled to [-1, +1]

    This means a single Item 4.02 restatement (weight 0.9, direction -1,
    contribution = -0.9) heavily anchors negative even when accompanied
    by neutral-direction events — those contribute 0 to the numerator
    but still count in the denominator, so each pulls the average toward
    zero. With one 4.02 (-0.9) and one 2.02 (0.0) the score is -0.45.

    Events with weight == 0 (an 8-K with no item codes) are excluded
    from the sentiment calculation — they don't carry directional
    signal — but they DO count in ``event_count``.

    Empty / None input -> ``event_count=0``,
    ``weighted_sentiment_score=None``, neutral label bucket.
    """
    safe_events: List[Dict[str, Any]] = list(events) if events else []
    ticker = ""
    if safe_events:
        ticker = str(safe_events[0].get("ticker") or "").upper()

    label_counts: Dict[str, int] = {label: 0 for label in _LABELS}

    if not safe_events:
        return {
            "ticker": ticker,
            "event_count": 0,
            "high_signal_event_count": 0,
            "weighted_sentiment_score": None,
            "label_counts": label_counts,
            "most_recent_event": None,
            "most_recent_date": None,
            "events": [],
        }

    contributions: List[float] = []
    high_signal = 0
    for e in safe_events:
        w = float(e.get("weight") or 0.0)
        d = int(e.get("direction") or 0)
        if w >= _HIGH_SIGNAL_WEIGHT:
            high_signal += 1
        if w > 0:
            contributions.append(w * d)
        # Histogram bucketing: map each event's direction to a label.
        label_counts[_label_for_direction(d)] += 1

    score: Optional[float] = None
    if contributions:
        raw = sum(contributions) / len(contributions)
        # Clamp into [-1, +1] — weights are in [0,1] and directions in
        # {-1,0,+1}, so raw is already bounded, but we clamp defensively
        # in case future weights exceed 1.
        if raw > 1.0:
            raw = 1.0
        elif raw < -1.0:
            raw = -1.0
        score = raw

    # ``events`` arrives newest-first from ``get_recent_8k_events`` but
    # we don't *require* that — sort to find the most recent.
    most_recent = max(
        safe_events,
        key=lambda e: str(e.get("filing_date") or ""),
    )
    mr_labels = most_recent.get("item_labels") or []
    mr_label = mr_labels[0] if mr_labels else None
    mr_date = most_recent.get("filing_date") or None

    return {
        "ticker": ticker,
        "event_count": len(safe_events),
        "high_signal_event_count": high_signal,
        "weighted_sentiment_score": score,
        "label_counts": label_counts,
        "most_recent_event": mr_label,
        "most_recent_date": mr_date,
        "events": safe_events,
    }


def event_signal_for_ticker(
    ticker: str,
    days: int = 90,
    *,
    today: Optional[date] = None,
) -> Dict[str, Any]:
    """Convenience: fetch + summarize, returning a Thesis-compatible payload.

    The output shape matches what ``alpha_vantage.parse_ticker_sentiment``
    and ``ai_news.compute_thesis_signal_from_news`` return so the
    downstream ``compute_thesis_from_stored_sentiment`` consumer doesn't
    need a branch::

        {
          "ticker": "AAPL",
          "article_count": 3,                # = event_count
          "mean_sentiment_score": -0.45,     # = weighted_sentiment_score
          "mean_relevance_score": 0.5,       # flat marker; events != articles
          "label_counts": {...},
          "source": "sec_8k",
          "last_scored": "2026-05-17",
          "high_signal_event_count": 1,
          "most_recent_event": "Non-reliance on previously-issued financials",
          "most_recent_date": "2026-05-10",
          "events": [...],
        }

    ``mean_relevance_score`` is a flat ``0.5`` whenever we have any
    events at all — mirrors what ``ai_news`` does. 8-K events are
    inherently 100% relevant (they're per-ticker filings), but a flat
    0.5 lets downstream code weight us alongside the per-article
    relevance numbers Alpha Vantage publishes without giving us a
    runaway advantage.
    """
    events = get_recent_8k_events(ticker, days=days, today=today)
    summary = summarize_events_for_thesis(events)

    ticker_norm = (ticker or "").upper()
    article_count = int(summary.get("event_count") or 0)
    score = summary.get("weighted_sentiment_score")
    relevance: Optional[float] = 0.5 if article_count > 0 else None

    return {
        "ticker": ticker_norm,
        "article_count": article_count,
        "mean_sentiment_score": score,
        "mean_relevance_score": relevance,
        "label_counts": dict(summary.get("label_counts") or {}),
        "source": "sec_8k",
        "last_scored": _today_iso() if today is None else today.isoformat(),
        "high_signal_event_count": int(summary.get("high_signal_event_count") or 0),
        "most_recent_event": summary.get("most_recent_event"),
        "most_recent_date": summary.get("most_recent_date"),
        "events": list(summary.get("events") or []),
    }


__all__ = [
    "ITEM_CODE_WEIGHTS",
    "SECEdgarError",
    "event_signal_for_ticker",
    "get_recent_8k_events",
    "summarize_events_for_thesis",
]
