"""SEC EDGAR source client.

Pulls XBRL company facts (revenue, gross margin, operating cash flow) from
the SEC's free, no-API-key REST endpoints at ``data.sec.gov`` and
``www.sec.gov``. Used to feed the Financial Evolution pillar of LTHCS.

Endpoints used:
    https://www.sec.gov/files/company_tickers.json
        Big JSON map: ticker symbol -> {cik_str, ticker, title}.
        Cached for the full source TTL (7 days).
    https://data.sec.gov/api/xbrl/companyfacts/CIK{padded_cik}.json
        XBRL "company facts" rollup for a given CIK. Updated only when
        the company files (so 7-day TTL is plenty).

Auth:
    The SEC has no API key, but it REQUIRES a custom ``User-Agent``
    header containing a real contact email. We read that from the
    ``SEC_USER_AGENT`` environment variable (loading a ``.env`` file via
    ``python-dotenv`` if available). If it's missing, we raise on the
    first network call -- not at import -- so test collection doesn't
    blow up in environments that don't need this source.

Rate limit:
    SEC allows ~10 req/sec from a single user agent. We use a
    ``TokenBucket(capacity=10, refill_rate=10.0)`` to stay under that.

Non-goals for this module:
    No retry logic on 429 / 5xx -- the caller decides what to do. We
    raise ``SECEdgarError`` with the status code and a body snippet so
    diagnosis is easy.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from lthcs.sources._cache import FileCache
from lthcs.sources._ratelimit import TokenBucket

# python-dotenv is optional -- if it's installed we read .env at import
# time so SEC_USER_AGENT picks up the dev's local config automatically.
try:  # pragma: no cover - trivial import shim
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass


# --- Constants ---------------------------------------------------------------

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

# 7 days. XBRL facts update on filing cadence (quarterly at most), so a
# week-long cache is conservative and saves a ton of bandwidth.
CACHE_TTL_SECONDS = 7 * 24 * 60 * 60

# SEC publishes its rate limit as "no more than 10 requests per second."
_RATE_CAPACITY = 10
_RATE_REFILL = 10.0

# Concept names to try (in order) when extracting revenue from XBRL.
# US GAAP uses ``Revenues`` historically; newer filings under ASC 606
# moved to ``RevenueFromContractWithCustomerExcludingAssessedTax``.
_REVENUE_CONCEPTS = (
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
)


# --- Module state ------------------------------------------------------------

def _cache_root() -> Path:
    return Path(os.environ.get("LTHCS_CACHE_DIR", ".cache/lthcs"))


# Module-level singletons so all callers share the same cache + bucket.
# ``FileCache`` creates its directory in ``__init__``, so we resolve the
# root lazily-ish: tests that need a different root override
# ``LTHCS_CACHE_DIR`` *before* importing this module, or replace the
# ``_cache`` singleton directly via ``sec_edgar._cache = FileCache(...)``.
_cache = FileCache("sec_edgar", root=_cache_root())
_bucket = TokenBucket(capacity=_RATE_CAPACITY, refill_rate=_RATE_REFILL)


# --- Errors ------------------------------------------------------------------

class SECEdgarError(RuntimeError):
    """Raised on non-200 responses or missing configuration."""


# --- Internals ---------------------------------------------------------------

def _user_agent() -> str:
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua:
        raise SECEdgarError(
            "SEC_USER_AGENT env var is not set. SEC requires a custom "
            "User-Agent containing a real contact email, e.g. "
            "'Acme Research bryan@example.com'. Set SEC_USER_AGENT in "
            "your environment or .env file."
        )
    return ua


def _headers() -> Dict[str, str]:
    return {"User-Agent": _user_agent(), "Accept": "application/json"}


def _get_json(url: str, cache_key: str) -> Any:
    """Fetch a URL, honoring cache + rate limit. Returns parsed JSON."""
    hit = _cache.get(cache_key)
    if hit is not None:
        return hit.value

    # Build headers (and validate SEC_USER_AGENT) *before* taking a token
    # so a misconfigured caller doesn't burn rate-limit budget.
    headers = _headers()
    _bucket.acquire()

    resp = requests.get(url, headers=headers, timeout=30)
    status = getattr(resp, "status_code", 0)
    if status != 200:
        body = ""
        try:
            body = (resp.text or "")[:200]
        except Exception:
            pass
        raise SECEdgarError(
            "SEC EDGAR request to {url} failed with status {status}: {body}".format(
                url=url, status=status, body=body
            )
        )

    data = resp.json()
    _cache.set(cache_key, data, ttl_seconds=CACHE_TTL_SECONDS)
    return data


# --- Public API --------------------------------------------------------------

def get_cik(ticker: str) -> Optional[str]:
    """Return the 10-digit zero-padded CIK for ``ticker``, or None.

    Tries exact match first, then a dot-stripped match (e.g. ``BRK.B`` ->
    ``BRKB``) because the SEC ticker file uses different separator conventions
    than Yahoo / S&P (e.g. SEC has ``BRKB``, Yahoo has ``BRK-B`` or ``BRK.B``).
    """
    if not ticker:
        return None
    norm = ticker.strip().upper()
    # SEC uses ``BRK-B`` style; Yahoo/S&P use ``BRK.B``. Try all common
    # separator substitutions so the same universe entry works across sources.
    candidates = {
        norm,
        norm.replace(".", ""),
        norm.replace("-", ""),
        norm.replace(".", "").replace("-", ""),
        norm.replace(".", "-"),
        norm.replace("-", "."),
    }

    raw = _get_json(TICKERS_URL, cache_key="company_tickers")
    # SEC ships this file as ``{"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, "1": {...}}``.
    if not isinstance(raw, dict):
        return None
    for entry in raw.values():
        if not isinstance(entry, dict):
            continue
        if str(entry.get("ticker", "")).upper() in candidates:
            cik = entry.get("cik_str")
            if cik is None:
                return None
            try:
                return str(int(cik)).zfill(10)
            except (TypeError, ValueError):
                return None
    return None


def get_company_facts(ticker: str) -> Dict[str, Any]:
    """Return the parsed company-facts JSON for ``ticker``.

    Raises ``SECEdgarError`` if SEC_USER_AGENT is unset, the ticker can't
    be resolved to a CIK, or the upstream returns a non-200 status.
    """
    # Surface config errors eagerly so the caller doesn't waste a CIK
    # lookup against a misconfigured env.
    _user_agent()

    cik = get_cik(ticker)
    if cik is None:
        raise SECEdgarError(
            "Could not resolve ticker {!r} to a CIK via SEC tickers file.".format(ticker)
        )

    url = COMPANY_FACTS_URL.format(cik=cik)
    return _get_json(url, cache_key="company_facts/{}".format(cik))


def get_revenue_history(ticker: str) -> List[Dict[str, Any]]:
    """Extract revenue rows from XBRL company facts.

    Merges ``us-gaap:Revenues`` (legacy, pre-ASC 606) and
    ``RevenueFromContractWithCustomerExcludingAssessedTax`` (post-ASC 606)
    so coverage spans the 2018 concept-change boundary. Most large companies
    will have both — we de-duplicate by ``(start_date, end_date)`` and
    prefer the modern concept's value when both report the same period.

    Each row::

        {"start_date": "2023-10-01", "end_date": "2024-09-28",
         "value": 391_035_000_000, "form": "10-K", "fy": 2024,
         "fp": "FY", "concept": "RevenueFromContract..."}

    sorted by ``end_date`` descending. Returns an empty list (not an
    exception) if no revenue facts are present.

    Note: ``form`` / ``fy`` / ``fp`` describe the FILING that reported the
    fact, not the fact's period. A 10-K filing contains both annual and
    quarterly facts. Callers that need annual values should filter by
    period duration (end - start ≈ 365 days), not by these tags.
    """
    facts = get_company_facts(ticker)

    gaap = (facts or {}).get("facts", {}).get("us-gaap", {})
    if not isinstance(gaap, dict):
        return []

    # Walk both concepts and merge by (start, end). Later concepts in the
    # list win on collision -- so put the modern concept last to prefer it.
    merge_order = ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax")
    by_period: Dict[tuple, Dict[str, Any]] = {}
    for concept in merge_order:
        node = gaap.get(concept)
        if not isinstance(node, dict):
            continue
        units = node.get("units", {})
        if not isinstance(units, dict):
            continue
        usd = units.get("USD")
        series = usd if isinstance(usd, list) else None
        if series is None:
            for _, candidate in units.items():
                if isinstance(candidate, list) and candidate:
                    series = candidate
                    break
        if not series:
            continue
        for item in series:
            if not isinstance(item, dict):
                continue
            end = item.get("end")
            val = item.get("val")
            if end is None or val is None:
                continue
            start = item.get("start")
            key = (str(start) if start is not None else None, str(end))
            by_period[key] = {
                "start_date": str(start) if start is not None else None,
                "end_date": str(end),
                "value": val,
                "form": item.get("form"),
                "fy": item.get("fy"),
                "fp": item.get("fp"),
                "concept": concept,
            }

    rows = list(by_period.values())
    rows.sort(key=lambda r: r["end_date"], reverse=True)
    return rows
