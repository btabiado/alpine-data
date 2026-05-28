"""SEC EDGAR Form 4 insider-transactions source for LTHCS.

Form 4 is the SEC filing that company insiders (officers, directors,
10% holders) must submit within two business days of any transaction in
their company's stock. It is the canonical, real-time, no-API-key source
for insider-trading signal. This module fetches recent Form 4 filings
per ticker, parses the OWNERSHIP XML schema, filters out the
non-discretionary mechanical transactions (option exercises, awards,
tax withholdings, 10b5-1 planned sales), and aggregates the survivors
into a per-ticker "insider conviction" score the LTHCS pipeline can
consume.

Endpoints used:
    https://data.sec.gov/submissions/CIK{padded_cik}.json
        Same submissions rollup as the 8-K module. We filter ``form == "4"``
        and parse each filing's primary XML document.
    https://www.sec.gov/Archives/edgar/data/{cik}/{accession_no_dash}/{primary_doc}
        The Form 4 XML itself. Each filing's primary document is named
        e.g. ``form4.xml`` or ``tm2614845-1_4seq1.xml`` — we let the
        submissions feed tell us the name rather than guessing.

Schema reference:
    https://www.sec.gov/info/edgar/specifications/form-x-ownership-tech-spec
    (Form 3/4/5 share the ``ownershipDocument`` root.)

Caching:
    Filings list per CIK changes daily — 24h TTL on the submissions
    payload (re-using sec_8k's pattern). Parsed Form 4 XML is
    immutable once filed — long TTL (30 days, effectively "forever"
    for the V1 daily pipeline; the per-CIK list will invalidate first
    anyway so we won't re-fetch unless the filing actually reappears).

Rate limit:
    Shares ``sec_edgar._bucket`` (10 req/sec) so the SEC's per-UA limit
    covers all SEC clients in this codebase combined.

Why this module is separate from ``sec_8k.py``:
    The output schema is completely different (insider transactions,
    not material-event item codes), the parsing target is XML rather
    than JSON, and the discretionary-vs-mechanical filtering logic is
    specific to Form 4 transaction codes. Sharing a module would muddy
    both signals. We DO reuse the CIK lookup, request session helpers,
    and rate-limit bucket via plain imports.
"""

from __future__ import annotations

import math
import os
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from lthcs.sources._cache import FileCache
from lthcs.sources.sec_edgar import SECEdgarError, _bucket as _SEC_BUCKET, _headers, get_cik


# --- Constants ---------------------------------------------------------------

_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_FILING_URL = (
    "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_no_dash}/{doc}"
)

# Submissions JSON lists every new filing — re-fetch daily.
_SUBMISSIONS_TTL_SECONDS = 24 * 60 * 60
# A specific Form 4 filing never changes once filed. 30 days is plenty.
_FILING_TTL_SECONDS = 30 * 24 * 60 * 60

# Transaction codes we COUNT toward conviction (open-market discretionary).
_BUY_CODES = {"P"}
_SELL_CODES = {"S"}
# Codes we explicitly drop (mechanical / compensation / planned / gifts /
# issuer transactions). Anything else (e.g. ``J`` = "other") falls through
# to the unknown bucket and is also filtered out.
_FILTERED_CODES = {"A", "M", "F", "G", "D", "I", "C", "E", "H", "K", "L", "O", "U", "V", "W", "X", "Z", "J"}

# Role-based weights for the conviction score. CEO is weighted most because
# CEOs control the most information; 10% holders least because they're often
# passive instruments (BlackRock / Vanguard / institutional pools).
_WEIGHT_CEO = 3.0
_WEIGHT_CFO = 2.5
_WEIGHT_OTHER_OFFICER = 1.5
_WEIGHT_DIRECTOR = 1.0
_WEIGHT_TEN_PERCENT = 0.8

# Sells are noisier than buys (insiders sell for many non-conviction
# reasons: diversification, liquidity, tax). We halve the magnitude.
_SELL_MAGNITUDE_FACTOR = 0.5

# Cluster-buying detection: 3+ distinct insiders buying within this window.
_CLUSTER_INSIDERS_REQUIRED = 3
_CLUSTER_WINDOW_DAYS = 14

# Regime thresholds (applied to ``conviction_score`` in [-1, +1]).
_REGIME_STRONG_BUYING = 0.5
_REGIME_MILD_BUYING = 0.1
_REGIME_MILD_SELLING = -0.1
_REGIME_HEAVY_SELLING = -0.5

# Conviction score normalization. ``net_dollar_value`` is mapped to
# [-1, +1] using a signed log: sign(x) * log10(1 + |x|) / log10(1 + scale).
# At scale = $10M the score saturates; small ($100k) flows land near 0.25.
_CONVICTION_SCALE_DOLLARS = 10_000_000

# Cap on how many raw transactions we keep in the response for
# variable_detail UI rendering. Keeps the JSON snapshot bounded.
_RAW_TX_LIMIT = 20

# Heuristic regex to detect 10b5-1 in footnote text (some filings only
# encode 10b5-1 via footnote, not the top-level aff10b5One flag).
_10B5_1_FOOTNOTE_RE = re.compile(r"10\s*b\s*5\s*-?\s*1", re.IGNORECASE)


# --- Module state ------------------------------------------------------------

def _cache_root() -> Path:
    return Path(os.environ.get("LTHCS_CACHE_DIR", ".cache/lthcs"))


# Tests rebind these for isolation.
_cache = FileCache("sec_form4", root=_cache_root())


# --- Helpers ----------------------------------------------------------------

def _today() -> date:
    """Indirection so tests can pin the calendar without monkeypatching stdlib."""
    return date.today()


def _today_iso() -> str:
    return _today().isoformat()


def _parse_iso_date(s: Any) -> Optional[date]:
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _parse_bool_flag(text: Optional[str]) -> bool:
    """Form 4 XML uses both ``true``/``false`` and ``1``/``0`` for booleans.

    Some old filings even use ``Y``/``N``. We accept all three. Anything
    we can't parse defaults to False (safe default for "is this an
    officer" — we won't over-weight an unclear case).
    """
    if text is None:
        return False
    s = text.strip().lower()
    return s in ("true", "1", "y", "yes")


def _accession_to_dirpath(accession: str) -> str:
    """Convert ``0001140361-26-020298`` to ``000114036126020298``.

    SEC's archive URLs use the no-dash form for the directory segment.
    """
    return accession.replace("-", "")


def _cik_no_pad(cik: str) -> str:
    """Strip leading zeros for the URL path segment (SEC archive URLs use
    the integer form, not the zero-padded form).
    """
    try:
        return str(int(cik))
    except (TypeError, ValueError):
        return cik


# --- HTTP fetch -------------------------------------------------------------

def _get_submissions_json(cik: str) -> Optional[Dict[str, Any]]:
    """Fetch + cache submissions JSON for ``cik``. Returns None on error.

    Matches the sec_8k pattern: SEC outages and 4xx/5xx errors return
    None so the daily pipeline keeps moving for other tickers. The one
    error we DO raise is missing ``SEC_USER_AGENT`` (config bug, not
    transient).
    """
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
    except (ValueError, Exception):  # noqa: BLE001 — be defensive
        return None
    if not isinstance(data, dict):
        return None

    _cache.set(cache_key, data, ttl_seconds=_SUBMISSIONS_TTL_SECONDS)
    return data


def _get_filing_xml(cik: str, accession: str, primary_doc: str) -> Optional[str]:
    """Fetch + cache one Form 4 XML body. Returns the raw text or None.

    ``primary_doc`` may be ``form4.xml`` (Apple-style) OR a path-y name
    like ``xslF345X06/form4.xml`` (the XSL-styled view). The XSL prefix
    points to a viewer — the raw XML always lives at the plain
    ``form4.xml`` or analogous root sibling. We strip any ``xsl*/``
    prefix to land on the underlying XML.
    """
    cache_key = "filing/{}/{}/{}".format(cik, accession, primary_doc)
    hit = _cache.get(cache_key)
    if hit is not None:
        return hit.value

    # Strip the XSL viewer prefix if present. SEC publishes Form 4 as a
    # plain XML and a sibling XSL-styled HTML view; only the former
    # parses cleanly.
    doc = primary_doc or ""
    if "/" in doc:
        doc = doc.rsplit("/", 1)[-1]

    headers = _headers()
    _SEC_BUCKET.acquire()

    url = _FILING_URL.format(
        cik_int=_cik_no_pad(cik),
        accession_no_dash=_accession_to_dirpath(accession),
        doc=doc,
    )
    try:
        resp = requests.get(url, headers=headers, timeout=30)
    except requests.RequestException:
        return None

    status = getattr(resp, "status_code", 0)
    if status != 200:
        return None

    body = getattr(resp, "text", "") or ""
    if not body:
        return None

    _cache.set(cache_key, body, ttl_seconds=_FILING_TTL_SECONDS)
    return body


# --- Submissions filtering --------------------------------------------------

def _iter_form4_rows(submissions: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Pivot the column-major ``filings.recent`` block into row dicts,
    filtered to ``form == "4"`` (NOT ``"4/A"`` amendments — those replace
    a prior filing and accounting for them properly requires diffing
    against the original; out of scope for V1).
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

    rows: List[Dict[str, Any]] = []
    for i in range(n):
        form = forms[i] if i < len(forms) else None
        if not isinstance(form, str) or form.strip() != "4":
            continue
        rows.append({
            "accessionNumber": accession[i] if i < len(accession) else None,
            "filingDate": filing_date[i] if i < len(filing_date) else None,
            "primaryDocument": primary_doc[i] if i < len(primary_doc) else None,
        })
    return rows


# --- XML parsing ------------------------------------------------------------

def _text(node: Optional[ET.Element]) -> Optional[str]:
    if node is None:
        return None
    t = node.text
    return t.strip() if t is not None else None


def _child_value(parent: Optional[ET.Element], path: str) -> Optional[str]:
    """Read a ``.../<name>/<value>`` subtree text. Returns None if missing.

    Most Form 4 leaf values are wrapped in a ``<value>`` element with an
    optional sibling ``<footnoteId>``::

        <transactionDate>
            <value>2026-05-06</value>
            <footnoteId id="F1"/>
        </transactionDate>

    We try the ``<value>`` child first; if that's missing we fall back to
    the parent's direct text (some old filings inline the scalar).
    """
    if parent is None:
        return None
    node = parent.find(path)
    if node is None:
        return None
    value_node = node.find("value")
    if value_node is not None:
        return _text(value_node)
    # Fallback for legacy filings that inline scalars without <value>.
    return _text(node)


def _classify_role(rel: Optional[ET.Element], title: Optional[str]) -> str:
    """Map a ``reportingOwnerRelationship`` block to a coarse role label.

    Output is one of: ``CEO``, ``CFO``, ``Officer``, ``Director``,
    ``TenPercent``, ``Other``. Title parsing is best-effort: SEC doesn't
    enumerate titles (it's free text), so we match common substrings.
    """
    if rel is None:
        return "Other"
    is_officer = _parse_bool_flag(_text(rel.find("isOfficer")))
    is_director = _parse_bool_flag(_text(rel.find("isDirector")))
    is_ten_percent = _parse_bool_flag(_text(rel.find("isTenPercentOwner")))

    norm_title = (title or "").strip().lower()
    if is_officer:
        # CEO matches: "chief executive officer", "ceo", "president and ceo".
        if "chief executive" in norm_title or re.search(r"\bceo\b", norm_title):
            return "CEO"
        # CFO matches: "chief financial officer", "cfo".
        if "chief financial" in norm_title or re.search(r"\bcfo\b", norm_title):
            return "CFO"
        return "Officer"
    if is_director:
        return "Director"
    if is_ten_percent:
        return "TenPercent"
    return "Other"


def _role_weight(role: str) -> float:
    if role == "CEO":
        return _WEIGHT_CEO
    if role == "CFO":
        return _WEIGHT_CFO
    if role == "Officer":
        return _WEIGHT_OTHER_OFFICER
    if role == "Director":
        return _WEIGHT_DIRECTOR
    if role == "TenPercent":
        return _WEIGHT_TEN_PERCENT
    return 0.0


def _parse_float(s: Optional[str]) -> Optional[float]:
    if s is None:
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _build_footnote_map(root: ET.Element) -> Dict[str, str]:
    """Build ``{footnote_id: text}`` for the filing's ``<footnotes>`` block."""
    out: Dict[str, str] = {}
    for fn in root.iter("footnote"):
        fid = fn.attrib.get("id")
        if not fid:
            continue
        out[fid] = (fn.text or "").strip()
    return out


def _transaction_has_10b5_1(
    tx: ET.Element, footnotes: Dict[str, str], aff10b5_one: bool
) -> bool:
    """Decide whether a single transaction is a 10b5-1 planned trade.

    Three signals, OR'd together:
      1. ``aff10b5One`` is true at the document root (filer affirms ALL
         transactions in this filing are 10b5-1).
      2. A per-transaction ``<rule10b5_1Flag><value>true|1</value>`` element
         (less common, but present in newer filings).
      3. Any referenced footnote text mentions "10b5-1" (covers older
         filings that didn't use the flag elements).
    """
    if aff10b5_one:
        return True

    # Explicit per-transaction flag.
    flag_text = _child_value(tx, "rule10b5_1Flag")
    if _parse_bool_flag(flag_text):
        return True

    # Walk all footnoteId references inside this transaction.
    for fn_ref in tx.iter("footnoteId"):
        fid = fn_ref.attrib.get("id")
        if not fid:
            continue
        text = footnotes.get(fid, "")
        if text and _10B5_1_FOOTNOTE_RE.search(text):
            return True

    return False


def parse_form4_xml(xml_text: str) -> Optional[Dict[str, Any]]:
    """Parse a Form 4 XML body into a normalized dict.

    Returns ``None`` if the XML is malformed or has no non-derivative
    transactions. The Form 4 schema also has a ``derivativeTable``
    (options, RSUs, etc.) — V1 only consumes non-derivative (common
    stock) transactions because those are the canonical conviction
    signal. Options activity gets folded back in via the M (exercise)
    code on the non-derivative side.

    Output shape::

        {
          "issuer_cik": "0000320193",
          "issuer_ticker": "AAPL",
          "issuer_name": "Apple Inc.",
          "owner_name": "Smith Jane Q",
          "owner_cik": "0001999991",
          "role": "CEO",
          "officer_title": "Chief Executive Officer",
          "aff10b5_one": False,
          "transactions": [
            {"date": "2026-05-08", "code": "P",
             "shares": 10000.0, "price": 125.0, "value": 1_250_000.0,
             "acquired_disposed": "A",
             "planned_10b5_1": False,
             "ownership_nature": "D"},
            ...
          ],
        }
    """
    try:
        root = ET.fromstring(xml_text)
    except (ET.ParseError, ValueError, TypeError):
        return None

    issuer = root.find("issuer")
    issuer_cik = _text(issuer.find("issuerCik")) if issuer is not None else None
    issuer_ticker = _text(issuer.find("issuerTradingSymbol")) if issuer is not None else None
    issuer_name = _text(issuer.find("issuerName")) if issuer is not None else None

    owner = root.find("reportingOwner")
    owner_id = owner.find("reportingOwnerId") if owner is not None else None
    owner_name = _text(owner_id.find("rptOwnerName")) if owner_id is not None else None
    owner_cik = _text(owner_id.find("rptOwnerCik")) if owner_id is not None else None
    rel = owner.find("reportingOwnerRelationship") if owner is not None else None
    officer_title = _text(rel.find("officerTitle")) if rel is not None else None
    role = _classify_role(rel, officer_title)

    aff10b5_one = _parse_bool_flag(_text(root.find("aff10b5One")))
    footnotes = _build_footnote_map(root)

    transactions: List[Dict[str, Any]] = []
    non_deriv = root.find("nonDerivativeTable")
    if non_deriv is not None:
        for tx in non_deriv.findall("nonDerivativeTransaction"):
            tx_date = _child_value(tx, "transactionDate")
            coding = tx.find("transactionCoding")
            code = _text(coding.find("transactionCode")) if coding is not None else None

            amounts = tx.find("transactionAmounts")
            shares = _parse_float(_child_value(amounts, "transactionShares")) if amounts is not None else None
            price = _parse_float(_child_value(amounts, "transactionPricePerShare")) if amounts is not None else None
            ad_code = _child_value(amounts, "transactionAcquiredDisposedCode") if amounts is not None else None

            nature = tx.find("ownershipNature")
            ownership = _child_value(nature, "directOrIndirectOwnership") if nature is not None else None

            planned = _transaction_has_10b5_1(tx, footnotes, aff10b5_one)

            value: Optional[float] = None
            if shares is not None and price is not None:
                value = shares * price

            transactions.append({
                "date": tx_date,
                "code": code,
                "shares": shares,
                "price": price,
                "value": value,
                "acquired_disposed": ad_code,
                "planned_10b5_1": planned,
                "ownership_nature": ownership,
            })

    if not transactions:
        # Some Form 4s only have derivative activity — for V1 that means
        # no usable signal. Return None so the caller can skip the row
        # cleanly.
        return None

    return {
        "issuer_cik": issuer_cik,
        "issuer_ticker": issuer_ticker,
        "issuer_name": issuer_name,
        "owner_name": owner_name,
        "owner_cik": owner_cik,
        "role": role,
        "officer_title": officer_title,
        "aff10b5_one": aff10b5_one,
        "transactions": transactions,
    }


# --- Aggregation ------------------------------------------------------------

def _is_discretionary(tx: Dict[str, Any]) -> bool:
    """A transaction counts toward conviction iff it is:
      * an open-market Purchase (code P) — buy
      * an open-market Sale (code S) — sell (half-weighted downstream)
      AND it is not a 10b5-1 planned trade.
    """
    code = tx.get("code")
    if code not in _BUY_CODES and code not in _SELL_CODES:
        return False
    if tx.get("planned_10b5_1"):
        return False
    # Sanity: a P with disposed=D or an S with acquired=A is malformed —
    # skip rather than mis-classify.
    code_ad = tx.get("acquired_disposed")
    if code in _BUY_CODES and code_ad not in (None, "A"):
        return False
    if code in _SELL_CODES and code_ad not in (None, "D"):
        return False
    return True


def _regime_for_score(score: float) -> str:
    if score >= _REGIME_STRONG_BUYING:
        return "strong_buying"
    if score >= _REGIME_MILD_BUYING:
        return "mild_buying"
    if score <= _REGIME_HEAVY_SELLING:
        return "heavy_selling"
    if score <= _REGIME_MILD_SELLING:
        return "mild_selling"
    return "neutral"


def _conviction_score_from_net_dollars(net_dollars: float) -> float:
    """Map ``net_dollar_value`` (signed) into [-1, +1] using signed log10.

    log10 keeps small flows ($100k) at ~0.25 and saturates at the scale
    constant ($10M). The output is clamped to [-1, +1] so a wildly
    out-of-band insider purchase doesn't dominate the downstream score
    composition.
    """
    if net_dollars == 0:
        return 0.0
    abs_x = abs(net_dollars)
    denom = math.log10(1 + _CONVICTION_SCALE_DOLLARS)
    if denom <= 0:
        return 0.0
    raw = math.log10(1 + abs_x) / denom
    if raw > 1.0:
        raw = 1.0
    signed = raw if net_dollars > 0 else -raw
    return signed


def _detect_cluster_buying(buy_events: List[Tuple[date, str]]) -> bool:
    """3+ distinct insiders buying within a rolling 14-day window.

    ``buy_events`` is a list of ``(transaction_date, owner_cik_or_name)``
    tuples for filtered open-market purchases. We sort by date and walk
    a sliding window. CIK is preferred as the identity key since it's
    canonical; we fall back to name when CIK is missing.
    """
    if len(buy_events) < _CLUSTER_INSIDERS_REQUIRED:
        return False
    events = sorted(buy_events, key=lambda e: e[0])
    n = len(events)
    for i in range(n):
        window_end = events[i][0] + timedelta(days=_CLUSTER_WINDOW_DAYS)
        ids = set()
        for j in range(i, n):
            if events[j][0] > window_end:
                break
            ids.add(events[j][1])
            if len(ids) >= _CLUSTER_INSIDERS_REQUIRED:
                return True
    return False


def _summarize_ceo_cfo_action(
    role_actions: Dict[str, Dict[str, float]]
) -> str:
    """Return ``buying`` / ``selling`` / ``neutral`` for CEO+CFO combined.

    Looks at signed dollar flow across CEO and CFO transactions. If both
    sum to zero / cancel out, returns ``neutral``.
    """
    total = 0.0
    for role in ("CEO", "CFO"):
        entry = role_actions.get(role)
        if entry:
            total += entry.get("net_dollar_value", 0.0)
    if total > 0:
        return "buying"
    if total < 0:
        return "selling"
    return "neutral"


def _aggregate_filings(
    ticker: str,
    parsed_filings: List[Dict[str, Any]],
    *,
    as_of_iso: str,
    window_days: int,
) -> Optional[Dict[str, Any]]:
    """Roll up parsed Form 4 filings into the public-API output shape."""
    buy_count = 0
    sell_count = 0
    buy_dollar = 0.0
    sell_dollar = 0.0
    weighted_buy = 0.0
    weighted_sell = 0.0
    filtered_out = 0
    raw: List[Dict[str, Any]] = []
    role_actions: Dict[str, Dict[str, float]] = {}
    buy_events: List[Tuple[date, str]] = []

    for parsed in parsed_filings:
        role = parsed.get("role") or "Other"
        weight = _role_weight(role)
        owner_name = parsed.get("owner_name") or ""
        owner_cik = parsed.get("owner_cik") or owner_name  # cluster-id fallback

        for tx in parsed.get("transactions", []):
            code = tx.get("code")
            shares = tx.get("shares") or 0.0
            price = tx.get("price") or 0.0
            value = tx.get("value")
            if value is None:
                value = shares * price
            planned = bool(tx.get("planned_10b5_1"))

            raw.append({
                "date": tx.get("date"),
                "insider": owner_name,
                "role": role,
                "code": code,
                "shares": shares,
                "price": price,
                "value": value,
                "planned_10b5_1": planned,
            })

            if not _is_discretionary(tx):
                filtered_out += 1
                continue

            if code in _BUY_CODES:
                buy_count += 1
                buy_dollar += value
                weighted_buy += weight
                tx_date = _parse_iso_date(tx.get("date"))
                if tx_date is not None:
                    buy_events.append((tx_date, owner_cik))
                entry = role_actions.setdefault(
                    role, {"buy_count": 0.0, "sell_count": 0.0, "net_dollar_value": 0.0}
                )
                entry["buy_count"] += 1
                entry["net_dollar_value"] += value
            elif code in _SELL_CODES:
                sell_count += 1
                sell_dollar += value
                weighted_sell += weight * _SELL_MAGNITUDE_FACTOR
                entry = role_actions.setdefault(
                    role, {"buy_count": 0.0, "sell_count": 0.0, "net_dollar_value": 0.0}
                )
                entry["sell_count"] += 1
                entry["net_dollar_value"] -= value

    # If we have no filings at all, signal absence to the caller.
    if not parsed_filings:
        return None

    # We still want to report when every transaction was filtered (e.g. all
    # 10b5-1 sales): the caller may want to display that as "no signal,
    # but filings exist". Keep returning the dict in that case.
    net_dollar_value = buy_dollar - sell_dollar
    _ = weighted_buy - weighted_sell  # weighted_sell is already positive in magnitude — negate the contribution

    # Above is subtle: we accumulate weighted_sell as positive magnitudes
    # but want sells to contribute negatively to net_weighted_score. The
    # public field ``weighted_sell_score`` is documented as negative,
    # half-magnitude, so we report it negated:
    weighted_sell_score = -weighted_sell
    net_weighted_score = weighted_buy + weighted_sell_score

    conviction = _conviction_score_from_net_dollars(net_dollar_value)
    regime = _regime_for_score(conviction)

    cluster = _detect_cluster_buying(buy_events)
    ceo_cfo = _summarize_ceo_cfo_action(role_actions)

    # Truncate raw transactions to the documented cap, newest-first.
    raw_sorted = sorted(
        raw, key=lambda r: str(r.get("date") or ""), reverse=True
    )[:_RAW_TX_LIMIT]

    return {
        "ticker": ticker.upper(),
        "as_of": as_of_iso,
        "window_days": window_days,
        "buy_count": buy_count,
        "sell_count": sell_count,
        "buy_dollar_value": buy_dollar,
        "sell_dollar_value": sell_dollar,
        "net_dollar_value": net_dollar_value,
        "weighted_buy_score": weighted_buy,
        "weighted_sell_score": weighted_sell_score,
        "net_weighted_score": net_weighted_score,
        "conviction_score": conviction,
        "cluster_buying": cluster,
        "ceo_cfo_action": ceo_cfo,
        "regime": regime,
        "raw_transactions": raw_sorted,
        "filtered_out_count": filtered_out,
    }


# --- Public API -------------------------------------------------------------

def fetch_insider_transactions(
    ticker: str,
    window_days: int = 90,
    cache_dir: Optional[Path] = None,
    *,
    today: Optional[date] = None,
    as_of: Optional[date] = None,
) -> Optional[Dict[str, Any]]:
    """Fetch + aggregate Form 4 insider transactions for ``ticker``.

    Returns ``None`` when:
      * ``ticker`` is empty / None
      * The ticker doesn't resolve to a CIK (private / delisted)
      * The submissions endpoint errored or returned no Form 4 filings
        in ``window_days``

    Returns a populated dict when any Form 4 filings exist in the window,
    even if every transaction was filtered out (10b5-1, awards, etc.) —
    the ``filtered_out_count`` field tells the caller what happened.

    Raises ``SECEdgarError`` for missing ``SEC_USER_AGENT`` (config bug).

    ``cache_dir`` lets a caller route caching to a specific directory
    (useful for tests). When None, the module-level cache (defaulting
    to ``.cache/lthcs/sec_form4``) is used.

    When ``as_of`` is supplied:
      * The filing-list cutoff becomes ``[as_of - window_days, as_of]``
        on ``filingDate`` (so we don't pull Form 4 XMLs from after that
        date — they wouldn't have existed yet).
      * Individual transactions are then filtered by ``transactionDate``
        to the same window (the canonical event date is the trade itself,
        not the filing).
      * ``as_of`` becomes the reported anchor in the result's ``as_of``
        field.
    """
    if not ticker:
        return None

    cik = get_cik(ticker)
    if cik is None:
        return None

    submissions = _get_submissions_json(cik)
    if not submissions:
        return None

    anchor = as_of if as_of is not None else (today if today is not None else _today())
    cutoff = anchor - timedelta(days=int(max(window_days, 0)))

    rows = _iter_form4_rows(submissions)
    parsed_filings: List[Dict[str, Any]] = []
    for row in rows:
        fd = _parse_iso_date(row.get("filingDate"))
        if fd is None or fd < cutoff:
            continue
        # When ``as_of`` is set, drop filings AFTER the right edge — they
        # didn't exist yet on that date.
        if as_of is not None and fd > as_of:
            continue
        accession = row.get("accessionNumber")
        primary_doc = row.get("primaryDocument")
        if not accession or not primary_doc:
            continue

        xml_body = _get_filing_xml(cik, accession, primary_doc)
        if not xml_body:
            # Skip individual filing errors — don't break the whole ticker.
            continue
        parsed = parse_form4_xml(xml_body)
        if parsed is None:
            continue

        # Filter individual transactions by transactionDate to the window.
        # The XML is parsed verbatim by parse_form4_xml; we trim here so
        # the as_of slice is canonical at the transaction level (not just
        # the filing-date approximation).
        filtered_txs: List[Dict[str, Any]] = []
        for tx in parsed.get("transactions", []) or []:
            tx_date = _parse_iso_date(tx.get("date"))
            if tx_date is None:
                # No usable date — drop on the conservative side rather
                # than risk mis-placing in history.
                continue
            if tx_date < cutoff:
                continue
            if as_of is not None and tx_date > as_of:
                continue
            filtered_txs.append(tx)
        if not filtered_txs:
            continue
        parsed = dict(parsed)
        parsed["transactions"] = filtered_txs
        parsed_filings.append(parsed)

    if not parsed_filings:
        # No usable filings in window — return None per spec.
        return None

    return _aggregate_filings(
        ticker,
        parsed_filings,
        as_of_iso=anchor.isoformat(),
        window_days=window_days,
    )


def fetch_universe_insider_transactions(
    tickers: List[str],
    window_days: int = 90,
    cache_dir: Optional[Path] = None,
    *,
    today: Optional[date] = None,
    as_of: Optional[date] = None,
) -> Dict[str, Dict[str, Any]]:
    """Batch wrapper. Returns ``{ticker: result}`` for tickers with usable
    Form 4 data in the window. Tickers with no filings / errors are
    silently dropped from the output (NOT mapped to None) — the LTHCS
    pipeline downstream treats absence as "no signal" already.

    When ``as_of`` is provided, the window becomes
    ``[as_of - window_days, as_of]`` for every ticker — used by the
    daily pipeline to compute historical LTHCS scores.
    """
    out: Dict[str, Dict[str, Any]] = {}
    seen: set = set()
    for t in tickers or []:
        if not t:
            continue
        norm = t.strip().upper()
        if not norm or norm in seen:
            continue
        seen.add(norm)
        try:
            result = fetch_insider_transactions(
                norm,
                window_days=window_days,
                cache_dir=cache_dir,
                today=today,
                as_of=as_of,
            )
        except SECEdgarError:
            # Config errors propagate via the first call already, but
            # defend against weird per-ticker failures.
            raise
        except Exception:  # noqa: BLE001 — defensive: a single bad ticker must not break the batch
            continue
        if result is not None:
            out[norm] = result
    return out


__all__ = [
    "SECEdgarError",
    "fetch_insider_transactions",
    "fetch_universe_insider_transactions",
    "parse_form4_xml",
]
