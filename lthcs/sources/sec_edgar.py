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

# Concept-name lists per metric. Order matters: when both concepts report
# the SAME period for the same company, the later one in the tuple wins
# on the merge (so put the modern / preferred concept last).
#
# Revenue: US GAAP used ``Revenues`` historically; newer filings under
# ASC 606 (effective 2018) moved to RevenueFromContractWithCustomerExcludingAssessedTax.
_REVENUE_CONCEPTS = (
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
)

# Gross profit — single canonical concept.
_GROSS_PROFIT_CONCEPTS = (
    "GrossProfit",
)

# --- Gross-margin XBRL fallbacks --------------------------------------------
#
# The post-audit P3 fix-up (May 2026): the canonical ``GrossProfit`` concept
# is only filed by ~56% of the universe. Services-heavy companies, banks,
# utilities, and some legacy filers report margin components under a
# different concept family. The Financial Evolution pillar walks a fallback
# chain (GrossProfit → SalesRevenueGross → Revenues - CostOfRevenue →
# OperatingIncomeLoss) so we can fairly rank margin quality across the
# universe instead of dropping 74 names to neutral 50.

# Legacy revenue concept (pre-ASC 606 reporters). When a company reports
# ``SalesRevenueGross`` it usually pairs revenue with a cost line so a
# gross-margin proxy can be computed via the cost-of-revenue concept below.
_SALES_REVENUE_GROSS_CONCEPTS = (
    "SalesRevenueGross",
    "SalesRevenueNet",
)

# Cost of revenue / cost of goods sold concepts. Used to derive a
# gross-margin proxy ``(Revenue - CostOfRevenue) / Revenue`` when
# ``GrossProfit`` is missing. Multiple variants exist across SIC codes;
# we accept all and let the period-merge dedup.
_COST_OF_REVENUE_CONCEPTS = (
    "CostOfRevenue",
    "CostOfGoodsAndServicesSold",
    "CostOfGoodsSold",
    "CostOfServices",
)

# Operating income loss — a looser margin proxy. Not the same metric as
# gross margin (it nets out OpEx) but reliably present across the
# universe and correlated enough with margin profile to be a useful
# last-resort fallback. The Financial pillar tags rows derived from this
# concept with ``margin_source == "operating_income"`` so consumers can
# discount the signal if needed.
_OPERATING_INCOME_CONCEPTS = (
    "OperatingIncomeLoss",
)

# Operating cash flow — two equivalent labels; SEC has used both.
_OPERATING_CASH_FLOW_CONCEPTS = (
    "NetCashProvidedByOperatingActivities",
    "NetCashProvidedByUsedInOperatingActivities",
)


# --- Bank-specific concepts -------------------------------------------------
#
# Banks (JPM, BAC, GS, WFC, C, MS, USB, TFC, etc.) don't report ``GrossProfit``
# or ``NetCashProvidedByOperatingActivities`` under the standard us-gaap
# concepts that industrial / tech companies use. They use a different
# financial-services concept family. The Financial Evolution pillar's
# bank code path consumes these series.
#
# Concept-tuple ordering follows the same rule as the non-bank concepts:
# the LATER entry wins on a period collision, so put the modern /
# preferred concept last.

# Net Interest Income (bank revenue analog) — interest earned minus interest
# paid. Some filers report the operating gross via
# ``InterestAndDividendIncomeOperating`` (income side only); the explicit
# net concept ``NetInterestIncome`` is rarer and only appears for some
# legacy JPM/BAC filings. We accept all three and let the merge dedup.
_BANK_NET_INTEREST_INCOME_CONCEPTS = (
    "InterestIncomeOperating",
    "InterestAndDividendIncomeOperating",
    "NetInterestIncome",
)

# Provision for Credit Losses (bank cost-of-revenue analog) — the
# anticipated-loan-loss accrual. ``ProvisionForCreditLosses`` is the
# post-2020 CECL-era concept used by JPM and BAC; the older
# ``ProvisionForLoanLeaseAndOtherLosses`` is what most banks still file
# under, with ``ProvisionForLoanAndLeaseLosses`` an even older variant.
_BANK_PROVISION_FOR_CREDIT_LOSSES_CONCEPTS = (
    "ProvisionForLoanAndLeaseLosses",
    "ProvisionForLoanLeaseAndOtherLosses",
    "ProvisionForCreditLosses",
)

# Noninterest Income (bank fee revenue) — trading, advisory, asset-mgmt
# fees, etc. The single canonical concept is universal across the big
# banks.
_BANK_NONINTEREST_INCOME_CONCEPTS = (
    "NoninterestIncome",
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


def _extract_concept_history(
    facts: Dict[str, Any], concepts: tuple, as_of: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Walk the XBRL ``us-gaap`` block and merge the given concepts.

    De-duplicates by ``(start_date, end_date)`` — later concepts in the
    tuple win on collision. Returns rows sorted by ``end_date`` desc.

    Each row::

        {"start_date": "2023-10-01", "end_date": "2024-09-28",
         "value": 391_035_000_000, "form": "10-K", "fy": 2024,
         "fp": "FY", "concept": "RevenueFromContract..."}

    Returns an empty list if none of ``concepts`` are present.

    Note: ``form`` / ``fy`` / ``fp`` describe the FILING that reported
    the fact, not the fact's period. A 10-K filing contains both annual
    and quarterly facts. Callers that need annual values should filter
    by period duration (end - start ≈ 365 days), not by these tags.

    When ``as_of`` is provided (ISO YYYY-MM-DD), only facts whose ``filed``
    date is ≤ ``as_of`` are included, so historical LTHCS scoring sees
    the same data the filing universe held on that date.
    """
    gaap = (facts or {}).get("facts", {}).get("us-gaap", {})
    if not isinstance(gaap, dict):
        return []

    as_of_str = str(as_of) if as_of else None

    by_period: Dict[tuple, Dict[str, Any]] = {}
    for concept in concepts:
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
            if as_of_str is not None:
                filed = item.get("filed")
                # Drop facts with no ``filed`` date when as_of filtering
                # is requested — we can't place them in time so they
                # can't be trusted as historical signal.
                if filed is None:
                    continue
                if str(filed) > as_of_str:
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


def get_revenue_history(
    ticker: str, as_of: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Revenue history merged across legacy + post-ASC 606 concepts.

    See :func:`_extract_concept_history` for the row schema. When
    ``as_of`` is provided, only facts ``filed`` on or before that ISO
    date are returned.
    """
    return _extract_concept_history(
        get_company_facts(ticker), _REVENUE_CONCEPTS, as_of=as_of
    )


def get_gross_profit_history(
    ticker: str, as_of: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Gross profit history. Same schema as :func:`get_revenue_history`."""
    return _extract_concept_history(
        get_company_facts(ticker), _GROSS_PROFIT_CONCEPTS, as_of=as_of
    )


def get_operating_cash_flow_history(
    ticker: str, as_of: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Operating cash flow history. Same schema as :func:`get_revenue_history`."""
    return _extract_concept_history(
        get_company_facts(ticker), _OPERATING_CASH_FLOW_CONCEPTS, as_of=as_of
    )


# --- Public API: gross-margin XBRL fallbacks --------------------------------

def get_sales_revenue_gross_history(
    ticker: str, as_of: Optional[str] = None
) -> List[Dict[str, Any]]:
    """``SalesRevenueGross`` / ``SalesRevenueNet`` history.

    Legacy revenue concepts (pre-ASC 606). Used as a fallback revenue
    series when neither ``Revenues`` nor
    ``RevenueFromContractWithCustomerExcludingAssessedTax`` is filed and
    the company reports under the older sales-revenue concepts. Same row
    schema as :func:`get_revenue_history`.
    """
    return _extract_concept_history(
        get_company_facts(ticker), _SALES_REVENUE_GROSS_CONCEPTS, as_of=as_of
    )


def get_cost_of_revenue_history(
    ticker: str, as_of: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Cost-of-revenue history.

    Merges ``CostOfRevenue`` / ``CostOfGoodsAndServicesSold`` /
    ``CostOfGoodsSold`` / ``CostOfServices``. Used to derive a
    gross-margin proxy ``(Revenue - CostOfRevenue) / Revenue`` when
    ``GrossProfit`` is missing. Same row schema as
    :func:`get_revenue_history`.
    """
    return _extract_concept_history(
        get_company_facts(ticker), _COST_OF_REVENUE_CONCEPTS, as_of=as_of
    )


def get_operating_income_history(
    ticker: str, as_of: Optional[str] = None
) -> List[Dict[str, Any]]:
    """``OperatingIncomeLoss`` history.

    Loose margin proxy (operating margin, not gross margin) used as the
    last-resort fallback in the gross-margin fallback chain. Consumers
    that consume this for gross-margin slope should tag the source so
    the signal can be discounted. Same row schema as
    :func:`get_revenue_history`.
    """
    return _extract_concept_history(
        get_company_facts(ticker), _OPERATING_INCOME_CONCEPTS, as_of=as_of
    )


# --- Public API: bank-specific concepts -------------------------------------

def get_net_interest_income_history(
    ticker: str, as_of: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Net Interest Income history (bank revenue analog).

    Merges ``InterestIncomeOperating`` / ``InterestAndDividendIncomeOperating``
    / ``NetInterestIncome`` so a single time series falls out regardless of
    which concept the filer uses. Same row schema as
    :func:`get_revenue_history`.

    Note: most large banks report the gross interest-income side
    (``InterestAndDividendIncomeOperating``) rather than a single
    ``NetInterestIncome`` concept; in V1 we treat that as the bank's
    "revenue line" for growth and ratio purposes since it's the dominant
    series the SEC actually fills.
    """
    return _extract_concept_history(
        get_company_facts(ticker), _BANK_NET_INTEREST_INCOME_CONCEPTS, as_of=as_of
    )


def get_provision_for_credit_losses_history(
    ticker: str, as_of: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Provision for Credit Losses history (bank cost-of-revenue analog).

    Merges the older ``ProvisionForLoanAndLeaseLosses`` /
    ``ProvisionForLoanLeaseAndOtherLosses`` and the post-2020 CECL-era
    ``ProvisionForCreditLosses`` so a contiguous series is available
    regardless of when the filer migrated concepts. Same row schema as
    :func:`get_revenue_history`.
    """
    return _extract_concept_history(
        get_company_facts(ticker),
        _BANK_PROVISION_FOR_CREDIT_LOSSES_CONCEPTS,
        as_of=as_of,
    )


def get_noninterest_income_history(
    ticker: str, as_of: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Noninterest Income history (bank fee-revenue line).

    Captures fees from trading, advisory, asset management, card / payments,
    and similar non-interest sources. Same row schema as
    :func:`get_revenue_history`.
    """
    return _extract_concept_history(
        get_company_facts(ticker), _BANK_NONINTEREST_INCOME_CONCEPTS, as_of=as_of
    )
