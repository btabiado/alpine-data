"""SEC EDGAR 13F-HR institutional-holdings source for LTHCS.

Form 13F-HR is the quarterly filing that institutional investment
managers with >$100M AUM must submit within 45 days of each quarter-end,
listing every Section-13(f) equity holding they exercise investment
discretion over (share count + market value). By aggregating filings
from the top 20 managers, we get a per-ticker institutional ownership
picture — manager count, total shares/value, top holders, and a
quarter-over-quarter conviction signal — without paying for a 13F
aggregator service.

Endpoints used:
    https://data.sec.gov/submissions/CIK{padded_cik}.json
        Same submissions rollup as sec_8k / sec_form4. We filter on
        ``form in {"13F-HR", "13F-HR/A"}`` and walk the recent filings.
    https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_no_dash}/index.json
        Per-filing index JSON. Lists the docs inside the filing so we
        can find the holdings XML (named ``form13fInfoTable.xml`` for
        modern filings but the index tells us authoritatively).
    https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_no_dash}/{doc}
        The holdings XML itself. Large (5-30MB for big funds), so we
        stream-parse with ``ET.iterparse()`` and extract only the
        CUSIPs / names that match our universe — caching the small
        per-filing-per-universe extracted result rather than the raw
        body.

Schema reference:
    https://www.sec.gov/info/edgar/specifications/13ffilertechspecs

Caching:
    Submissions JSON: 24h TTL (same cadence as sec_8k/sec_form4).
    Per-filing universe-extracted holdings: 365 days (filings are
    immutable once filed; the 365d window covers a full rotation).
    Per-ticker aggregate: 14 days (refreshed shortly after each 45-day
    13F deadline; 14d is conservative enough that one missed daily
    cron doesn't stale the user-facing data badly).

Rate limit:
    Shares ``sec_edgar._bucket`` (10 req/sec) so all SEC clients in
    this codebase combine under the SEC's per-UA limit.

Value-units:
    The SEC changed Form 13F to require dollars (not thousands) in
    January 2023. We detect the cutover via ``periodOfReport``: filings
    covering quarters ending on/after 2022-12-31 (final period before
    cutover) report in dollars; older filings in thousands. The pillar
    consumes value in **dollars** so we multiply legacy filings by 1000.

Why this module is separate from ``sec_form4.py``:
    Different filing form, different XML schema (informationTable, not
    ownershipDocument), and a per-ticker fan-IN aggregation (many
    managers per ticker) rather than the per-ticker fan-OUT of Form 4
    (one issuer per filing). Sharing a module would muddy both signals.
    We DO reuse the request session helpers and rate-limit bucket.

Phase 1 scope:
    Top 20 institutional managers by 13F AUM, hand-curated and
    verified via SEC full-text search (see ``TRACKED_MANAGERS``).
    Captures the bulk of institutional ownership signal for large caps;
    small caps will fall in the "sparse" / "partial" data-quality bucket
    which the pillar handles gracefully.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from lthcs.sources._cache import FileCache
from lthcs.sources.sec_edgar import (
    SECEdgarError,
    _bucket as _SEC_BUCKET,
    _headers,
    _user_agent,
    get_cik,
)


# --- Tracked managers --------------------------------------------------------
#
# Top 20 institutional managers by 13F AUM, with the CIK of the entity that
# actually files the consolidated 13F-HR for that manager group. CIKs were
# verified against EDGAR full-text-search results filtered to recent
# (2026-Q1) 13F-HR filings — several managers file under multiple CIKs
# (parent + subsidiaries) and we pick the one that owns the consolidated
# group filing. See module docstring for the verification methodology.
#
# Discrepancies vs. the original spec:
#   * BlackRock moved its consolidated 13F to a new CIK (2012383) circa 2024;
#     the spec's 1364742 ("BlackRock Finance, Inc.") only has 3 stub filings.
#   * The spec had "JPMorgan AM" pointing at 1364742 (a paste error duplicating
#     BlackRock). The actual JPMorgan consolidated 13F filer is CIK 19617
#     ("JPMorgan Chase & Co").
#   * Fidelity files as "FMR LLC" under CIK 315066 (no change).
#   * Capital Research files under two separate entities: 1422848 (Global
#     Investors) and 1422849 (World Investors). We include both as one
#     logical "Capital Group" entry (using the larger Global Investors CIK).
TRACKED_MANAGERS: Dict[str, str] = {
    "BlackRock":             "0002012383",
    "Vanguard":              "0000102909",
    "State Street":          "0000093751",
    "Fidelity (FMR LLC)":    "0000315066",
    "T. Rowe Price":         "0000080255",
    "Capital Research":      "0001422848",
    "Capital World":         "0001422849",
    "Berkshire Hathaway":    "0001067983",
    "JPMorgan Chase":        "0000019617",
    "Wellington":            "0000902219",
    "Geode Capital":         "0001214717",
    "Bank of NY Mellon":     "0001390777",
    "Morgan Stanley":        "0000895421",
    "Goldman Sachs":         "0000886982",
    "Bridgewater":           "0001350694",
    "Renaissance Tech":      "0001037389",
    "Tiger Global":          "0001167483",
    "Citadel":               "0001423053",
    "Two Sigma":             "0001179392",
    "AQR Capital":           "0001167557",
    "Millennium":            "0001273087",
}


# --- Constants ---------------------------------------------------------------

_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_INDEX_URL = (
    "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_no_dash}/index.json"
)
_FILING_DOC_URL = (
    "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_no_dash}/{doc}"
)

# Submissions JSON: a manager files at most 4 13F-HRs per year, so 24h TTL
# is more than enough — same cadence as the other SEC clients.
_SUBMISSIONS_TTL_SECONDS = 24 * 60 * 60
# Filing index.json: per-filing manifest, immutable once filed.
_INDEX_TTL_SECONDS = 365 * 24 * 60 * 60
# Per-filing universe-extracted holdings: filings are immutable, so cache
# essentially forever. 365 days is a comfortable upper bound.
_FILING_TTL_SECONDS = 365 * 24 * 60 * 60
# Per-ticker aggregate snapshot: refreshed shortly after each 45-day 13F
# deadline. 14 days is safe inside that envelope.
_AGGREGATE_TTL_SECONDS = 14 * 24 * 60 * 60

# Common XML namespaces seen in 13F filings. The schema lives under
# http://www.sec.gov/edgar/document/thirteenf/informationtable but newer
# filings use that, older filings use a slightly different path. We strip
# all namespaces before walking so the parsing is robust to the version.
_XML_NS_STRIP_RE = re.compile(r"\{[^}]*\}")

# Top-N holders to surface per ticker.
_TOP_HOLDERS_LIMIT = 10

# Cutover date for the SEC's "report dollars, not thousands" rule. Filings
# whose periodOfReport is BEFORE this date use thousands; on/after use dollars.
# The rule was finalized in 2022 and applies to filings covering periods
# ending 2023-01-01 onward. We accept either MM-DD-YYYY or YYYY-MM-DD parsing.
_DOLLAR_UNITS_CUTOVER = date(2023, 1, 1)

# Conviction-signal thresholds.
_SIGNAL_ACCUMULATING = 0.3
_SIGNAL_DISTRIBUTING = -0.3
_SIGNAL_STEADY_BAND = 0.1  # |score| < 0.1 -> steady

# Data-quality thresholds.
_QUALITY_GOOD_MIN = 10
_QUALITY_PARTIAL_MIN = 5


# --- Module state ------------------------------------------------------------

def _cache_root() -> Path:
    return Path(os.environ.get("LTHCS_CACHE_DIR", ".cache/lthcs"))


# Tests rebind these for isolation.
_cache = FileCache("sec_13f", root=_cache_root())


# --- Errors / exceptions -----------------------------------------------------

# We re-use sec_edgar's SECEdgarError for config issues (missing UA).
# All other failures degrade silently to "no data for this manager" so a
# single bad fetch doesn't break the entire universe rollup.


# --- Hardcoded CUSIP map ----------------------------------------------------
#
# The most reliable way to match a 13F holding row to "this is AAPL" is by
# CUSIP. yfinance occasionally exposes CUSIP via Ticker.info but coverage is
# spotty. We hard-code the LTHCS universe's primary CUSIPs (and a few
# alternate CUSIPs for tickers that have multiple classes). For tickers
# NOT in this map, the parser falls back to issuer-name normalization,
# which is less reliable but works for most large caps.
#
# CUSIP-9 format: 8-char issuer + 1-char issue-type check digit. We compare
# only the first 6 chars (issuer-level CUSIP-6) plus the 2 issue chars
# (excluding the check digit), because some filers report 8-char CUSIPs and
# some report 9. The check digit is computed from the other 8 so it's
# redundant for matching purposes.
TICKER_TO_CUSIP: Dict[str, Tuple[str, ...]] = {
    "AAPL":  ("037833100",),
    "MSFT":  ("594918104",),
    "NVDA":  ("67066G104",),
    "AMZN":  ("023135106",),
    "GOOGL": ("02079K305", "02079K107"),  # Class A & Class C
    "GOOG":  ("02079K107",),
    "META":  ("30303M102",),
    "TSLA":  ("88160R101",),
    "AVGO":  ("11135F101",),
    "AMD":   ("007903107",),
    "ORCL":  ("68389X105",),
    "CRM":   ("79466L302",),
    "ADBE":  ("00724F101",),
    "INTC":  ("458140100",),
    "QCOM":  ("747525103",),
    "NFLX":  ("64110L106",),
    "DIS":   ("254687106",),
    "JPM":   ("46625H100",),
    "BAC":   ("060505104",),
    "WFC":   ("949746101",),
    "C":     ("172967424",),
    "GS":    ("38141G104",),
    "MS":    ("617446448",),
    "V":     ("92826C839",),
    "MA":    ("57636Q104",),
    "BRK.B": ("084670702",),
    "BRK-B": ("084670702",),
    "BRKB":  ("084670702",),
    "BRK.A": ("084670108",),
    "JNJ":   ("478160104",),
    "PFE":   ("717081103",),
    "MRK":   ("58933Y105",),
    "UNH":   ("91324P102",),
    "LLY":   ("532457108",),
    "ABBV":  ("00287Y109",),
    "WMT":   ("931142103",),
    "HD":    ("437076102",),
    "COST":  ("22160K105",),
    "PG":    ("742718109",),
    "KO":    ("191216100",),
    "PEP":   ("713448108",),
    "MCD":   ("580135101",),
    "XOM":   ("30231G102",),
    "CVX":   ("166764100",),
    "BA":    ("097023105",),
    "CAT":   ("149123101",),
    "GE":    ("369604301",),
    "T":     ("00206R102",),
    "VZ":    ("92343V104",),
}


def _normalize_cusip(c: Optional[str]) -> Optional[str]:
    """Normalize a CUSIP to its 8-char prefix (drop check digit if present).

    Returns ``None`` for empty / malformed input.
    """
    if not c or not isinstance(c, str):
        return None
    s = c.strip().upper()
    # CUSIPs are alphanumeric; strip anything that isn't.
    s = re.sub(r"[^A-Z0-9]", "", s)
    if len(s) < 8:
        return None
    # Compare on 8-char issuer+issue prefix (drop the check digit).
    return s[:8]


def _build_cusip_lookup(tickers: Iterable[str]) -> Dict[str, str]:
    """Build ``{normalized_cusip_prefix: ticker}`` for the requested universe.

    Multiple tickers can map to the same CUSIP (e.g. BRK.B aliases) — last
    write wins, but since the canonical ticker maps first the alias maps
    don't displace it materially.
    """
    out: Dict[str, str] = {}
    for t in tickers:
        norm_t = (t or "").strip().upper()
        if not norm_t:
            continue
        for c in TICKER_TO_CUSIP.get(norm_t, ()):  # type: ignore[arg-type]
            nc = _normalize_cusip(c)
            if nc:
                out[nc] = norm_t
    return out


def _build_name_lookup(tickers: Iterable[str]) -> Dict[str, str]:
    """Fallback issuer-name -> ticker map for tickers without a CUSIP.

    The matching key is the lowercased issuer-name with all whitespace and
    punctuation stripped — the SEC files use varied conventions like
    "APPLE INC", "Apple Inc.", "APPLE INCORPORATED". The lookup matches
    if the normalized issuer-name STARTS WITH the normalized lookup key.
    """
    # Coarse company-name fallbacks. Keep these short / unambiguous so we
    # don't match "VISA EUROPE" to "V" or similar. This map covers the
    # large-cap tickers most likely to be needed if CUSIP matching fails.
    NAMES: Dict[str, str] = {
        "AAPL":  "apple",
        "MSFT":  "microsoft",
        "NVDA":  "nvidia",
        "AMZN":  "amazon",
        "GOOGL": "alphabet",
        "GOOG":  "alphabet",
        "META":  "meta platforms",
        "TSLA":  "tesla",
        "JPM":   "jpmorgan chase",
        "XOM":   "exxon mobil",
    }
    out: Dict[str, str] = {}
    for t in tickers:
        norm_t = (t or "").strip().upper()
        if not norm_t:
            continue
        nm = NAMES.get(norm_t)
        if nm:
            out[_normalize_name(nm)] = norm_t
    return out


def _normalize_name(s: Optional[str]) -> str:
    """Lowercase, strip punctuation/whitespace, remove common corp suffixes."""
    if not s or not isinstance(s, str):
        return ""
    s = s.lower()
    # Strip common corporate suffixes (after lowercasing).
    s = re.sub(r"\b(inc|incorporated|corp|corporation|co|llc|ltd|plc|holdings|holdco|nv|sa|ag|class\s+[a-z]|cl\s+[a-z]|com|common\s+stock)\b", "", s)
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


# --- Helpers ----------------------------------------------------------------

def _today() -> date:
    """Indirection so tests can pin the calendar."""
    return date.today()


def _today_iso() -> str:
    return _today().isoformat()


def _accession_to_dirpath(accession: str) -> str:
    return (accession or "").replace("-", "")


def _cik_no_pad(cik: str) -> str:
    try:
        return str(int(cik))
    except (TypeError, ValueError):
        return cik


def _parse_period_of_report(s: Optional[str]) -> Optional[date]:
    """Parse SEC's period-of-report dates. Both MM-DD-YYYY and YYYY-MM-DD seen."""
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%m-%d-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _quarter_label(d: Optional[date]) -> Optional[str]:
    """``2026-03-31`` -> ``"2026-Q1"``."""
    if d is None:
        return None
    q = (d.month - 1) // 3 + 1
    return "{}-Q{}".format(d.year, q)


def _prev_quarter_label(label: Optional[str]) -> Optional[str]:
    if not label:
        return None
    m = re.match(r"^(\d{4})-Q(\d)$", label)
    if not m:
        return None
    y, q = int(m.group(1)), int(m.group(2))
    if q == 1:
        return "{}-Q4".format(y - 1)
    return "{}-Q{}".format(y, q - 1)


# --- HTTP ------------------------------------------------------------------

def _get_submissions_json(cik: str) -> Optional[Dict[str, Any]]:
    """Fetch + cache submissions JSON for ``cik``. Returns None on error."""
    cache_key = "submissions/{}".format(cik)
    hit = _cache.get(cache_key)
    if hit is not None:
        return hit.value

    headers = _headers()  # validates SEC_USER_AGENT before burning a token
    _SEC_BUCKET.acquire()

    url = _SUBMISSIONS_URL.format(cik=cik)
    try:
        resp = requests.get(url, headers=headers, timeout=30)
    except requests.RequestException:
        return None

    status = getattr(resp, "status_code", 0)
    if status != 200:
        return None

    try:
        data = resp.json()
    except (ValueError, Exception):  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None

    _cache.set(cache_key, data, ttl_seconds=_SUBMISSIONS_TTL_SECONDS)
    return data


def _get_filing_index(cik: str, accession: str) -> Optional[Dict[str, Any]]:
    """Fetch + cache the per-filing index.json. Returns None on error."""
    cache_key = "index/{}/{}".format(cik, accession)
    hit = _cache.get(cache_key)
    if hit is not None:
        return hit.value

    headers = _headers()
    _SEC_BUCKET.acquire()

    url = _INDEX_URL.format(
        cik_int=_cik_no_pad(cik),
        accession_no_dash=_accession_to_dirpath(accession),
    )
    try:
        resp = requests.get(url, headers=headers, timeout=30)
    except requests.RequestException:
        return None
    if getattr(resp, "status_code", 0) != 200:
        return None
    try:
        data = resp.json()
    except (ValueError, Exception):  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    _cache.set(cache_key, data, ttl_seconds=_INDEX_TTL_SECONDS)
    return data


def _get_filing_doc(cik: str, accession: str, doc: str) -> Optional[str]:
    """Fetch a single document text from a filing. NOT cached at the raw
    level — 13F holdings tables are 5-30MB each. We cache the extracted
    per-universe result instead."""
    headers = _headers()
    _SEC_BUCKET.acquire()
    url = _FILING_DOC_URL.format(
        cik_int=_cik_no_pad(cik),
        accession_no_dash=_accession_to_dirpath(accession),
        doc=doc,
    )
    try:
        resp = requests.get(url, headers=headers, timeout=120)
    except requests.RequestException:
        return None
    if getattr(resp, "status_code", 0) != 200:
        return None
    body = getattr(resp, "text", "") or ""
    return body or None


# --- Submissions filtering -------------------------------------------------

def _iter_13f_filings(submissions: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Pivot the column-major submissions block to row dicts filtered to
    ``form in {"13F-HR", "13F-HR/A"}``. Includes amendments; the caller
    de-duplicates per quarter (preferring the latest amendment).
    """
    recent = (
        (submissions or {})
        .get("filings", {})
        .get("recent", {})
    )
    if not isinstance(recent, dict):
        return []

    forms = recent.get("form") or []
    if not isinstance(forms, list) or not forms:
        return []
    n = len(forms)

    def _col(name: str) -> List[Any]:
        col = recent.get(name) or []
        return col if isinstance(col, list) else []

    accession = _col("accessionNumber")
    filing_date = _col("filingDate")
    primary_doc = _col("primaryDocument")
    report_date = _col("reportDate")

    rows: List[Dict[str, Any]] = []
    for i in range(n):
        form = forms[i] if i < len(forms) else None
        if not isinstance(form, str):
            continue
        if form not in ("13F-HR", "13F-HR/A"):
            continue
        rows.append({
            "form": form,
            "accessionNumber": accession[i] if i < len(accession) else None,
            "filingDate": filing_date[i] if i < len(filing_date) else None,
            "primaryDocument": primary_doc[i] if i < len(primary_doc) else None,
            "reportDate": report_date[i] if i < len(report_date) else None,
        })
    return rows


def _dedupe_filings_by_quarter(
    rows: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Keep only one filing per quarter (reportDate), preferring 13F-HR/A
    over 13F-HR. Returns rows sorted by reportDate descending.
    """
    # Index by quarter label.
    by_quarter: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        rep_d = _parse_period_of_report(r.get("reportDate"))
        q = _quarter_label(rep_d)
        if q is None:
            continue
        existing = by_quarter.get(q)
        if existing is None:
            by_quarter[q] = r
            continue
        # Prefer amendment over original.
        if r.get("form") == "13F-HR/A" and existing.get("form") != "13F-HR/A":
            by_quarter[q] = r
        elif r.get("form") == existing.get("form"):
            # Same form, prefer the more recent filing date (later amendment).
            if (r.get("filingDate") or "") > (existing.get("filingDate") or ""):
                by_quarter[q] = r
    out = list(by_quarter.values())
    out.sort(key=lambda r: (r.get("reportDate") or ""), reverse=True)
    return out


# --- XML parsing -----------------------------------------------------------

def _strip_ns(tag: str) -> str:
    """Strip XML namespace from a tag name."""
    return _XML_NS_STRIP_RE.sub("", tag)


def _find_info_table_doc(index_json: Dict[str, Any]) -> Optional[str]:
    """Pull the holdings-table document filename out of an index.json.

    Looks for the canonical ``form13fInfoTable.xml`` first; falls back
    to any ``*InfoTable.xml`` or ``*infotable*.xml`` named file. Returns
    just the filename (no path prefix) so the caller can build the doc URL.
    """
    directory = (index_json or {}).get("directory") or {}
    items = directory.get("item") or []
    if not isinstance(items, list):
        return None

    # Canonical name first.
    for it in items:
        if not isinstance(it, dict):
            continue
        name = it.get("name") or ""
        if name.lower() == "form13finfotable.xml":
            return name

    # Fuzzy fallback.
    for it in items:
        if not isinstance(it, dict):
            continue
        name = it.get("name") or ""
        n = name.lower()
        if n.endswith(".xml") and "infotable" in n:
            return name

    return None


def _parse_cover_page(xml_text: str) -> Dict[str, Any]:
    """Extract the small set of cover-page fields we care about.

    Returns ``{"period_of_report": date|None, "manager_name": str|None,
    "form_type": str|None}``. Robust to namespace variation.
    """
    out: Dict[str, Any] = {
        "period_of_report": None,
        "manager_name": None,
        "form_type": None,
    }
    try:
        root = ET.fromstring(xml_text)
    except (ET.ParseError, ValueError, TypeError):
        return out

    # Walk the tree once and pick out the elements by stripped-tag name.
    for el in root.iter():
        tag = _strip_ns(el.tag)
        if tag == "periodOfReport":
            d = _parse_period_of_report((el.text or "").strip())
            if d is not None:
                out["period_of_report"] = d
        elif tag == "submissionType":
            out["form_type"] = (el.text or "").strip() or None
        elif tag == "name" and out["manager_name"] is None:
            # The first <name> inside the doc is the filingManager.
            # We accept the first non-empty.
            txt = (el.text or "").strip()
            if txt:
                out["manager_name"] = txt
    return out


def _holdings_unit_multiplier(period_of_report: Optional[date]) -> float:
    """Pre-2023 13F filings report ``value`` in thousands of dollars; modern
    filings report dollars. Returns the multiplier to convert raw value to
    dollars.
    """
    if period_of_report is None:
        # Conservative default for unknown era: treat as modern (dollars).
        return 1.0
    if period_of_report < _DOLLAR_UNITS_CUTOVER:
        return 1000.0
    return 1.0


def _iter_info_table_rows(xml_text: str) -> Iterable[Dict[str, Any]]:
    """Stream-parse the holdings XML, yielding one row dict per ``<infoTable>``.

    Uses ``ET.iterparse()`` and clears parsed elements as we go so a 30MB
    file stays under a few-MB memory footprint. The yielded shape::

        {
          "name_of_issuer": "APPLE INC",
          "title_of_class": "COM",
          "cusip": "037833100",
          "value": 123456,            # raw, NOT yet multiplied for units
          "shares": 1000.0,
          "shrs_or_prn_type": "SH",   # "SH" (shares) or "PRN" (principal)
        }

    Rows with non-share quantities (PRN — bonds, options) are emitted but
    callers should filter them out.
    """
    try:
        stream = BytesIO(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except Exception:  # noqa: BLE001
        return

    try:
        ctx = ET.iterparse(stream, events=("end",))
    except Exception:  # noqa: BLE001
        return

    current: Optional[Dict[str, Any]] = None
    try:
        for event, elem in ctx:
            tag = _strip_ns(elem.tag)
            if tag == "infoTable":
                # End of a row: yield whatever we collected and reset.
                # Walk the row's children directly — iterparse fires "end"
                # for each child first, so we can pick them off here too.
                row: Dict[str, Any] = {
                    "name_of_issuer": None,
                    "title_of_class": None,
                    "cusip": None,
                    "value": None,
                    "shares": None,
                    "shrs_or_prn_type": None,
                }
                for child in list(elem):
                    ctag = _strip_ns(child.tag)
                    if ctag == "nameOfIssuer":
                        row["name_of_issuer"] = (child.text or "").strip() or None
                    elif ctag == "titleOfClass":
                        row["title_of_class"] = (child.text or "").strip() or None
                    elif ctag == "cusip":
                        row["cusip"] = (child.text or "").strip() or None
                    elif ctag == "value":
                        try:
                            row["value"] = float((child.text or "").strip())
                        except (TypeError, ValueError):
                            row["value"] = None
                    elif ctag == "shrsOrPrnAmt":
                        for gc in list(child):
                            gtag = _strip_ns(gc.tag)
                            if gtag == "sshPrnamt":
                                try:
                                    row["shares"] = float((gc.text or "").strip())
                                except (TypeError, ValueError):
                                    row["shares"] = None
                            elif gtag == "sshPrnamtType":
                                row["shrs_or_prn_type"] = (gc.text or "").strip() or None
                yield row
                elem.clear()
    except ET.ParseError:
        return


def _extract_holdings_for_universe(
    xml_text: str,
    cusip_lookup: Dict[str, str],
    name_lookup: Dict[str, str],
    unit_multiplier: float,
) -> Dict[str, Dict[str, float]]:
    """Aggregate the rows of an info-table XML into per-ticker totals.

    Returns ``{ticker: {"shares": <float>, "value": <dollars>}}`` where
    ``value`` has already been multiplied by ``unit_multiplier``. Multiple
    rows for the same ticker (sub-fund splits) are SUMMED. Rows that
    don't match any universe ticker are silently dropped.
    """
    out: Dict[str, Dict[str, float]] = {}
    for row in _iter_info_table_rows(xml_text):
        # Drop principal-amount rows (bonds / debt). Only SH counts.
        if (row.get("shrs_or_prn_type") or "").upper() not in ("SH", ""):
            continue
        ticker: Optional[str] = None
        norm = _normalize_cusip(row.get("cusip"))
        if norm and norm in cusip_lookup:
            ticker = cusip_lookup[norm]
        else:
            # Fallback: match issuer-name STARTS WITH a normalized lookup key.
            nn = _normalize_name(row.get("name_of_issuer"))
            if nn:
                for key, t in name_lookup.items():
                    if nn.startswith(key):
                        ticker = t
                        break
        if ticker is None:
            continue

        shares = row.get("shares") or 0.0
        value = (row.get("value") or 0.0) * unit_multiplier
        entry = out.setdefault(ticker, {"shares": 0.0, "value": 0.0})
        entry["shares"] = float(entry["shares"]) + float(shares)
        entry["value"] = float(entry["value"]) + float(value)
    return out


# --- Per-manager / per-quarter fetch ---------------------------------------

def _extracted_cache_key(cik: str, accession: str, universe_fingerprint: str) -> str:
    return "filing/{}/{}/{}".format(cik, accession, universe_fingerprint)


def _universe_fingerprint(tickers: Iterable[str]) -> str:
    """Stable short fingerprint of the universe so cache entries don't
    cross-contaminate across pipeline runs with different ticker sets."""
    norm = sorted({(t or "").strip().upper() for t in tickers if t})
    import hashlib
    return hashlib.sha256("|".join(norm).encode("utf-8")).hexdigest()[:12]


def _fetch_one_filing_extracted(
    cik: str,
    filing_row: Dict[str, Any],
    cusip_lookup: Dict[str, str],
    name_lookup: Dict[str, str],
    universe_fingerprint: str,
) -> Optional[Dict[str, Any]]:
    """Fetch a single 13F filing, extract universe-relevant rows, cache result.

    Returned shape::

        {
          "form_type": "13F-HR",
          "period_of_report": "2026-03-31",
          "quarter": "2026-Q1",
          "manager_name": "BlackRock, Inc.",
          "holdings": {
            "AAPL": {"shares": 1050200000, "value": 188400000000},
            ...
          },
        }
    """
    accession = filing_row.get("accessionNumber")
    if not accession:
        return None

    cache_key = _extracted_cache_key(cik, accession, universe_fingerprint)
    hit = _cache.get(cache_key)
    if hit is not None:
        return hit.value

    # 1. Find the holdings XML doc via the filing's index.json.
    idx = _get_filing_index(cik, accession)
    if not idx:
        return None
    info_table_doc = _find_info_table_doc(idx)
    if not info_table_doc:
        return None

    # 2. Find the primary cover-page doc (used for period_of_report + manager name).
    # The submissions feed's ``primaryDocument`` is often something like
    # ``xslForm13F_X02/primary_doc.xml``; the underlying XML lives at
    # plain ``primary_doc.xml``. We look it up in the index instead.
    primary_doc: Optional[str] = None
    directory = (idx or {}).get("directory") or {}
    items = directory.get("item") or []
    if isinstance(items, list):
        for it in items:
            if not isinstance(it, dict):
                continue
            name = it.get("name") or ""
            if name.lower() == "primary_doc.xml":
                primary_doc = name
                break

    cover: Dict[str, Any] = {
        "period_of_report": None,
        "manager_name": None,
        "form_type": filing_row.get("form"),
    }
    if primary_doc:
        cover_body = _get_filing_doc(cik, accession, primary_doc)
        if cover_body:
            parsed_cover = _parse_cover_page(cover_body)
            if parsed_cover.get("period_of_report") is not None:
                cover["period_of_report"] = parsed_cover["period_of_report"]
            if parsed_cover.get("manager_name"):
                cover["manager_name"] = parsed_cover["manager_name"]

    # Fall back to the submissions-feed reportDate if cover-page parse failed.
    if cover["period_of_report"] is None:
        cover["period_of_report"] = _parse_period_of_report(filing_row.get("reportDate"))

    # 3. Fetch + stream-parse the info table.
    info_body = _get_filing_doc(cik, accession, info_table_doc)
    if not info_body:
        return None
    multiplier = _holdings_unit_multiplier(cover["period_of_report"])
    holdings = _extract_holdings_for_universe(
        info_body, cusip_lookup, name_lookup, multiplier
    )

    period = cover["period_of_report"]
    out = {
        "form_type": cover["form_type"],
        "period_of_report": period.isoformat() if period else None,
        "quarter": _quarter_label(period),
        "manager_name": cover["manager_name"],
        "holdings": holdings,
    }
    _cache.set(cache_key, out, ttl_seconds=_FILING_TTL_SECONDS)
    return out


def fetch_manager_13f_holdings(
    cik: str,
    quarter: Optional[str] = None,
    *,
    tickers: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Fetch one manager's 13F holdings extracted for the given universe.

    Parameters
    ----------
    cik:
        Manager CIK (10-digit padded form).
    quarter:
        Quarter label like ``"2026-Q1"``. ``None`` returns all available
        quarters in the submissions feed (most recent first).
    tickers:
        Universe to extract. ``None`` extracts the default
        ``TICKER_TO_CUSIP`` universe.

    Returns a list of per-quarter result dicts (most recent first). Each
    entry is the return-shape of :func:`_fetch_one_filing_extracted`.
    """
    cik = (cik or "").strip()
    if not cik:
        return []

    universe = list(tickers or TICKER_TO_CUSIP.keys())
    cusip_lookup = _build_cusip_lookup(universe)
    name_lookup = _build_name_lookup(universe)
    fingerprint = _universe_fingerprint(universe)

    submissions = _get_submissions_json(cik)
    if not submissions:
        return []
    rows = _iter_13f_filings(submissions)
    rows = _dedupe_filings_by_quarter(rows)

    out: List[Dict[str, Any]] = []
    for row in rows:
        rep_d = _parse_period_of_report(row.get("reportDate"))
        q = _quarter_label(rep_d)
        if quarter is not None and q != quarter:
            continue
        extracted = _fetch_one_filing_extracted(
            cik, row, cusip_lookup, name_lookup, fingerprint
        )
        if extracted is not None:
            out.append(extracted)
    return out


# --- Aggregation -----------------------------------------------------------

def _conviction_signal(
    net_buyers: int, net_sellers: int, manager_count: int
) -> Tuple[float, str]:
    """Return ``(signal_score, signal_label)``.

    ``signal_score = (net_buyers - net_sellers) / manager_count`` clamped
    to [-1, +1]. Labels follow the spec:

        score > +0.3  -> accumulating
        score < -0.3  -> distributing
        |score| < 0.1 -> steady
        otherwise     -> mixed
    """
    if manager_count <= 0:
        return 0.0, "steady"
    raw = (net_buyers - net_sellers) / float(manager_count)
    score = max(-1.0, min(1.0, raw))
    if score > _SIGNAL_ACCUMULATING:
        label = "accumulating"
    elif score < _SIGNAL_DISTRIBUTING:
        label = "distributing"
    elif abs(score) < _SIGNAL_STEADY_BAND:
        label = "steady"
    else:
        label = "mixed"
    return score, label


def _data_quality(manager_count: int) -> str:
    if manager_count >= _QUALITY_GOOD_MIN:
        return "good"
    if manager_count >= _QUALITY_PARTIAL_MIN:
        return "partial"
    return "sparse"


def aggregate_holdings_for_ticker(
    ticker: str,
    manager_data: Dict[str, List[Dict[str, Any]]],
    *,
    as_of_iso: Optional[str] = None,
) -> Dict[str, Any]:
    """Aggregate one ticker's holdings across all tracked managers.

    Parameters
    ----------
    ticker:
        Subject ticker.
    manager_data:
        ``{manager_display_name: [quarterly_filing_dict, ...]}`` — list per
        manager is "most-recent quarter first" as returned by
        :func:`fetch_manager_13f_holdings`. Empty / missing managers are
        treated as "doesn't hold the ticker".

    Returns the per-ticker output shape documented in the module-level
    public API (manager_count, top_holders, quarter_over_quarter, etc.).
    """
    norm_t = (ticker or "").strip().upper()
    as_of = as_of_iso or _today_iso()

    # 1. Pull the most recent quarter that has at least one tracked manager
    # reporting. We pick this as the "latest_quarter" anchor; the QoQ
    # comparison uses the immediately prior quarter (when we have it).
    latest_quarter: Optional[str] = None
    for filings in manager_data.values():
        if not filings:
            continue
        q = filings[0].get("quarter")
        if q is None:
            continue
        # Compare lexicographically — quarter labels like "2026-Q1" sort
        # the way we want as strings, so the max is the most recent.
        if latest_quarter is None or q > latest_quarter:
            latest_quarter = q

    prior_quarter = _prev_quarter_label(latest_quarter)

    # 2. Walk each manager once, picking the entry for the latest_quarter
    # and prior_quarter. Sum shares + value across managers for the
    # latest quarter; compute per-manager share-change for the QoQ.
    holders_latest: List[Dict[str, Any]] = []
    net_buyers = 0
    net_sellers = 0
    unchanged = 0
    manager_count_prior = 0
    total_shares_latest = 0.0
    total_value_latest = 0.0
    total_shares_prior = 0.0

    for manager, filings in manager_data.items():
        latest_entry: Optional[Dict[str, Any]] = None
        prior_entry: Optional[Dict[str, Any]] = None
        for f in filings:
            q = f.get("quarter")
            if q == latest_quarter and latest_entry is None:
                latest_entry = f
            elif q == prior_quarter and prior_entry is None:
                prior_entry = f
            if latest_entry is not None and prior_entry is not None:
                break

        latest_holding = (latest_entry or {}).get("holdings", {}).get(norm_t)
        prior_holding = (prior_entry or {}).get("holdings", {}).get(norm_t)

        if latest_holding is not None:
            shares = float(latest_holding.get("shares") or 0.0)
            value = float(latest_holding.get("value") or 0.0)
            total_shares_latest += shares
            total_value_latest += value
            holders_latest.append({
                "manager": manager,
                "shares_mm": shares / 1_000_000.0,
                "value_bn": value / 1_000_000_000.0,
            })

        if prior_holding is not None:
            total_shares_prior += float(prior_holding.get("shares") or 0.0)
            manager_count_prior += 1

        # QoQ direction comparison — only relevant if we have BOTH quarters.
        if latest_holding is not None and prior_holding is not None:
            ls = float(latest_holding.get("shares") or 0.0)
            ps = float(prior_holding.get("shares") or 0.0)
            if ls > ps:
                net_buyers += 1
            elif ls < ps:
                net_sellers += 1
            else:
                unchanged += 1
        elif latest_holding is not None and prior_entry is not None:
            # Manager had data prior quarter but did NOT hold the ticker —
            # they're a new buyer.
            net_buyers += 1
        elif latest_holding is None and prior_holding is not None:
            # Manager exited the position.
            net_sellers += 1

    # 3. Sort + rank top holders.
    holders_latest.sort(key=lambda h: h["value_bn"], reverse=True)
    top_holders: List[Dict[str, Any]] = []
    for idx, h in enumerate(holders_latest[:_TOP_HOLDERS_LIMIT], start=1):
        top_holders.append({
            "manager": h["manager"],
            "shares_mm": round(h["shares_mm"], 3),
            "value_bn": round(h["value_bn"], 3),
            "rank": idx,
        })

    manager_count = len(holders_latest)
    signal_score, signal_label = _conviction_signal(net_buyers, net_sellers, manager_count)

    # 4. QoQ share-change percent. Guard against division by zero.
    if total_shares_prior > 0:
        share_change_pct = (total_shares_latest - total_shares_prior) / total_shares_prior * 100.0
    else:
        share_change_pct = 0.0
    manager_count_change = manager_count - manager_count_prior

    quality = _data_quality(manager_count)

    return {
        "ticker": norm_t,
        "as_of": as_of,
        "latest_quarter": latest_quarter,
        "manager_count": manager_count,
        "total_shares_held_mm": round(total_shares_latest / 1_000_000.0, 3),
        "total_value_held_bn": round(total_value_latest / 1_000_000_000.0, 3),
        "top_holders": top_holders,
        "quarter_over_quarter": {
            "prior_quarter": prior_quarter,
            "share_change_pct": round(share_change_pct, 2),
            "manager_count_change": manager_count_change,
            "net_buyers": net_buyers,
            "net_sellers": net_sellers,
            "unchanged": unchanged,
        },
        "conviction_signal": signal_label,
        "signal_score": round(signal_score, 3),
        "data_quality": quality,
    }


# --- Public API: universe roll-up ------------------------------------------

def fetch_universe_institutional_holdings(
    tickers: List[str],
    cache_dir: Optional[Path] = None,
    *,
    today: Optional[date] = None,
    managers: Optional[Dict[str, str]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Top-level: fan out across tracked managers, aggregate per ticker.

    Parameters
    ----------
    tickers:
        Universe of tickers to score. Tickers without a CUSIP mapping
        still work via name-fallback for the well-known large caps; the
        rest will simply yield ``manager_count=0`` and
        ``data_quality="sparse"``.
    cache_dir:
        Unused at the public-API level — caching is wired through the
        module-level singleton + ``LTHCS_CACHE_DIR`` env var. Accepted
        for symmetry with :func:`lthcs.sources.sec_form4.fetch_universe_insider_transactions`.
    managers:
        Optional override of :data:`TRACKED_MANAGERS`. Useful for tests.

    Returns ``{ticker: aggregate_dict}`` for every ticker in ``tickers``.
    Tickers with no holdings across any tracked manager still get an
    entry (manager_count=0, top_holders=[], etc.) — the caller's pillar
    code wants to know "no data" vs "missing ticker".
    """
    out: Dict[str, Dict[str, Any]] = {}
    if not tickers:
        return out

    # De-dupe + normalize.
    seen: set = set()
    universe: List[str] = []
    for t in tickers:
        if not t:
            continue
        n = t.strip().upper()
        if not n or n in seen:
            continue
        seen.add(n)
        universe.append(n)
    if not universe:
        return out

    mgr_map = managers if managers is not None else TRACKED_MANAGERS

    # 1. Fetch each manager's full holdings (latest + prior quarter cached).
    per_manager: Dict[str, List[Dict[str, Any]]] = {}
    for name, cik in mgr_map.items():
        try:
            per_manager[name] = fetch_manager_13f_holdings(cik, tickers=universe)
        except SECEdgarError:
            raise
        except Exception:  # noqa: BLE001 — per-manager failures shouldn't break the batch
            per_manager[name] = []

    # 2. Aggregate per ticker.
    as_of_iso = (today or _today()).isoformat()
    for t in universe:
        out[t] = aggregate_holdings_for_ticker(t, per_manager, as_of_iso=as_of_iso)
    return out


__all__ = [
    "SECEdgarError",
    "TRACKED_MANAGERS",
    "TICKER_TO_CUSIP",
    "fetch_manager_13f_holdings",
    "fetch_universe_institutional_holdings",
    "aggregate_holdings_for_ticker",
]
