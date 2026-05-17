"""Tests for lthcs.sources.sec_13f.

HTTP is mocked. We isolate the module-level FileCache to a per-test
``tmp_path`` so cache state doesn't leak across tests.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from lthcs.sources._cache import FileCache
from lthcs.sources import sec_edgar, sec_13f


FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "sec_13f"


# --- Fixtures ---------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_caches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fresh_13f = FileCache("sec_13f", root=tmp_path)
    monkeypatch.setattr(sec_13f, "_cache", fresh_13f)
    fresh_edgar = FileCache("sec_edgar", root=tmp_path)
    monkeypatch.setattr(sec_edgar, "_cache", fresh_edgar)


@pytest.fixture()
def ua(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEC_USER_AGENT", "Test bot test@example.com")


@pytest.fixture()
def fixed_today(monkeypatch: pytest.MonkeyPatch) -> date:
    fixed = date(2026, 5, 17)
    monkeypatch.setattr(sec_13f, "_today", lambda: fixed)
    return fixed


def _fake_response(body: Any, status: int = 200, *, text: bool = False) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.ok = status == 200
    if text:
        m.text = body if isinstance(body, str) else ""
        m.json.side_effect = AssertionError("text response, no JSON")
    else:
        m.json.return_value = body
        m.text = "" if status == 200 else "error body snippet"
    return m


def _load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text()


def _make_submissions(filings: List[Dict[str, Any]]) -> Dict[str, Any]:
    cols = ("form", "filingDate", "accessionNumber", "primaryDocument", "reportDate")
    recent: Dict[str, List[Any]] = {c: [] for c in cols}
    for f in filings:
        for c in cols:
            recent[c].append(f.get(c, ""))
    return {
        "cik": "0000000999",
        "name": "Example Manager Inc.",
        "filings": {"recent": recent},
    }


def _index_json(files: List[str]) -> Dict[str, Any]:
    """Build a fake index.json directory listing."""
    return {
        "directory": {
            "name": "/Archives/edgar/data/999/0000000999-26-000001",
            "parent-dir": "/Archives/edgar/data/999",
            "item": [
                {"last-modified": "2026-05-13 14:52:46", "name": f, "type": "text.gif", "size": "1024"}
                for f in files
            ],
        }
    }


# --- Tickers fixture used by all CIK lookups -------------------------------

TICKERS_FIXTURE: Dict[str, Dict[str, Any]] = {
    "0": {"cik_str": 999, "ticker": "EXMP", "title": "Example Corp"},
    "1": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
}


def _build_dispatcher(
    tickers_resp: Optional[MagicMock] = None,
    submissions_by_cik: Optional[Dict[str, MagicMock]] = None,
    index_by_accession: Optional[Dict[str, MagicMock]] = None,
    docs_by_suffix: Optional[Dict[str, MagicMock]] = None,
):
    """Route patched ``requests.get`` calls by URL inspection."""
    submissions_by_cik = submissions_by_cik or {}
    index_by_accession = index_by_accession or {}
    docs_by_suffix = docs_by_suffix or {}

    def _side_effect(url: str, *args: Any, **kwargs: Any) -> MagicMock:
        if "submissions/CIK" in url and url.endswith(".json"):
            # Extract the CIK from the URL.
            import re
            m = re.search(r"CIK(\d+)\.json", url)
            if m:
                cik = m.group(1)
                if cik in submissions_by_cik:
                    return submissions_by_cik[cik]
            raise AssertionError(f"No submissions fixture for URL: {url}")
        if url.endswith("/index.json"):
            for acc, resp in index_by_accession.items():
                if acc in url:
                    return resp
            raise AssertionError(f"No index fixture for URL: {url}")
        if "/Archives/edgar/data/" in url:
            for suffix, resp in docs_by_suffix.items():
                if url.endswith(suffix):
                    return resp
            raise AssertionError(f"No doc fixture for URL: {url}")
        # Tickers file fallback
        if tickers_resp is not None:
            return tickers_resp
        raise AssertionError(f"No fixture for URL: {url}")

    return _side_effect


# --- Pure helpers ----------------------------------------------------------

def test_normalize_cusip_strips_check_digit() -> None:
    assert sec_13f._normalize_cusip("037833100") == "03783310"
    # 8-char input is returned as-is.
    assert sec_13f._normalize_cusip("03783310") == "03783310"
    # Whitespace + lower case tolerated.
    assert sec_13f._normalize_cusip(" 037833100 ") == "03783310"
    # Malformed.
    assert sec_13f._normalize_cusip(None) is None
    assert sec_13f._normalize_cusip("") is None
    assert sec_13f._normalize_cusip("abc") is None


def test_build_cusip_lookup_returns_8char_keys() -> None:
    lookup = sec_13f._build_cusip_lookup(["AAPL", "MSFT", "BRK.B"])
    assert lookup["03783310"] == "AAPL"
    assert lookup["59491810"] == "MSFT"
    # BRK.B alias maps to BRK.B (one of the variants); we don't care which.
    assert lookup["08467070"] in ("BRK.B", "BRK-B", "BRKB")


def test_quarter_label_basic() -> None:
    assert sec_13f._quarter_label(date(2026, 3, 31)) == "2026-Q1"
    assert sec_13f._quarter_label(date(2025, 12, 31)) == "2025-Q4"
    assert sec_13f._quarter_label(date(2024, 6, 30)) == "2024-Q2"
    assert sec_13f._quarter_label(None) is None


def test_prev_quarter_label_handles_q1_rollover() -> None:
    assert sec_13f._prev_quarter_label("2026-Q1") == "2025-Q4"
    assert sec_13f._prev_quarter_label("2026-Q2") == "2026-Q1"
    assert sec_13f._prev_quarter_label(None) is None
    assert sec_13f._prev_quarter_label("garbage") is None


def test_parse_period_of_report_accepts_multiple_formats() -> None:
    assert sec_13f._parse_period_of_report("2026-03-31") == date(2026, 3, 31)
    assert sec_13f._parse_period_of_report("03-31-2026") == date(2026, 3, 31)
    assert sec_13f._parse_period_of_report("03/31/2026") == date(2026, 3, 31)
    assert sec_13f._parse_period_of_report(None) is None
    assert sec_13f._parse_period_of_report("garbage") is None


def test_holdings_unit_multiplier_legacy_vs_modern() -> None:
    # 2022-09-30 is BEFORE the 2023-01-01 cutover -> thousands.
    assert sec_13f._holdings_unit_multiplier(date(2022, 9, 30)) == 1000.0
    # 2023-03-31 is on/after cutover -> dollars.
    assert sec_13f._holdings_unit_multiplier(date(2023, 3, 31)) == 1.0
    # Unknown defaults to modern (dollars).
    assert sec_13f._holdings_unit_multiplier(None) == 1.0


# --- Cover-page parser -----------------------------------------------------

def test_parse_cover_page_extracts_period_and_manager() -> None:
    body = _load_fixture("primary_doc_2026q1.xml")
    parsed = sec_13f._parse_cover_page(body)
    assert parsed["period_of_report"] == date(2026, 3, 31)
    assert parsed["form_type"] == "13F-HR"
    assert parsed["manager_name"] == "Example Manager Inc."


def test_parse_cover_page_garbage_xml_returns_empty_dict() -> None:
    parsed = sec_13f._parse_cover_page("<<not xml>>")
    assert parsed["period_of_report"] is None
    assert parsed["manager_name"] is None


# --- Info-table iterator ---------------------------------------------------

def test_iter_info_table_rows_yields_all_rows() -> None:
    body = _load_fixture("info_table_2026q1_aapl_msft.xml")
    rows = list(sec_13f._iter_info_table_rows(body))
    # 5 SH rows + 1 PRN row = 6 total rows.
    assert len(rows) == 6
    aapl_rows = [r for r in rows if (r.get("name_of_issuer") or "").startswith("APPLE")]
    # APPLE INC has 2 share rows + 1 note row = 3.
    assert len(aapl_rows) == 3


def test_extract_holdings_aggregates_multiple_rows_per_ticker() -> None:
    """Two AAPL rows (5M + 2.5M shares) should sum to 7.5M shares."""
    body = _load_fixture("info_table_2026q1_aapl_msft.xml")
    cusip_lookup = sec_13f._build_cusip_lookup(["AAPL", "MSFT", "BRK.B"])
    name_lookup: Dict[str, str] = {}
    extracted = sec_13f._extract_holdings_for_universe(
        body, cusip_lookup, name_lookup, unit_multiplier=1.0
    )
    assert "AAPL" in extracted
    assert extracted["AAPL"]["shares"] == 7_500_000.0
    assert extracted["AAPL"]["value"] == 1_500_000_000.0
    # MSFT (single row)
    assert extracted["MSFT"]["shares"] == 2_000_000.0
    # BRK.B matched via CUSIP 084670702.
    assert "BRK.B" in extracted or "BRK-B" in extracted or "BRKB" in extracted


def test_extract_holdings_skips_prn_amount_rows() -> None:
    """The PRN (principal) row for AAPL bonds must NOT count toward shares."""
    body = _load_fixture("info_table_2026q1_aapl_msft.xml")
    cusip_lookup = sec_13f._build_cusip_lookup(["AAPL"])
    extracted = sec_13f._extract_holdings_for_universe(
        body, cusip_lookup, {}, unit_multiplier=1.0
    )
    # If PRN was counted, shares would include the 50M PRN. Verify excluded.
    assert extracted["AAPL"]["shares"] == 7_500_000.0


def test_extract_holdings_legacy_thousands_units_multiplied() -> None:
    body = _load_fixture("info_table_legacy_thousands.xml")
    cusip_lookup = sec_13f._build_cusip_lookup(["AAPL"])
    extracted = sec_13f._extract_holdings_for_universe(
        body, cusip_lookup, {}, unit_multiplier=1000.0
    )
    # Raw value = 1_000_000 (thousands) -> 1_000_000_000 dollars.
    assert extracted["AAPL"]["value"] == 1_000_000_000.0
    assert extracted["AAPL"]["shares"] == 5_000_000.0


def test_extract_holdings_cusip_matches_before_name_fallback() -> None:
    """When CUSIP matches, name fallback should NOT also match (no double-count)."""
    body = _load_fixture("info_table_2026q1_aapl_msft.xml")
    cusip_lookup = sec_13f._build_cusip_lookup(["AAPL"])
    # Provide a name lookup that ALSO matches "APPLE" — verify we don't
    # double-count if CUSIP already hit.
    name_lookup = {sec_13f._normalize_name("apple"): "AAPL"}
    extracted = sec_13f._extract_holdings_for_universe(
        body, cusip_lookup, name_lookup, unit_multiplier=1.0
    )
    assert extracted["AAPL"]["shares"] == 7_500_000.0


def test_extract_holdings_name_fallback_when_cusip_missing() -> None:
    """A ticker with no CUSIP mapping should match via the name fallback."""
    body = _load_fixture("info_table_2026q1_aapl_msft.xml")
    # Empty CUSIP lookup but a name fallback for AAPL.
    name_lookup = {sec_13f._normalize_name("apple"): "AAPL"}
    extracted = sec_13f._extract_holdings_for_universe(
        body, cusip_lookup={}, name_lookup=name_lookup, unit_multiplier=1.0
    )
    assert "AAPL" in extracted
    assert extracted["AAPL"]["shares"] == 7_500_000.0


def test_extract_holdings_brk_b_class_b_disambiguation() -> None:
    """BRK.B CUSIP 084670702 must match WITHOUT also matching BRK.A (084670108)."""
    body = _load_fixture("info_table_2026q1_aapl_msft.xml")
    cusip_lookup = sec_13f._build_cusip_lookup(["BRK.B", "BRK.A"])
    extracted = sec_13f._extract_holdings_for_universe(
        body, cusip_lookup, {}, unit_multiplier=1.0
    )
    # Either BRK.B (or alias) is present and BRK.A is not.
    brk_b_keys = [k for k in extracted if k in ("BRK.B", "BRK-B", "BRKB")]
    assert len(brk_b_keys) == 1
    assert extracted[brk_b_keys[0]]["shares"] == 250_000.0
    assert "BRK.A" not in extracted


def test_iter_info_table_rows_handles_malformed_xml() -> None:
    rows = list(sec_13f._iter_info_table_rows("<<not xml>>"))
    assert rows == []


# --- Filing-row pivoting + deduplication -----------------------------------

def test_iter_13f_filings_filters_correct_forms() -> None:
    submissions = _make_submissions([
        {"form": "13F-HR", "accessionNumber": "A1", "filingDate": "2026-05-13", "reportDate": "2026-03-31"},
        {"form": "10-K", "accessionNumber": "X1", "filingDate": "2026-05-01", "reportDate": "2025-12-31"},
        {"form": "13F-HR/A", "accessionNumber": "A2", "filingDate": "2026-05-14", "reportDate": "2026-03-31"},
        {"form": "13F-NT", "accessionNumber": "X2", "filingDate": "2026-05-15", "reportDate": "2026-03-31"},
    ])
    rows = sec_13f._iter_13f_filings(submissions)
    assert [r["accessionNumber"] for r in rows] == ["A1", "A2"]


def test_dedupe_filings_by_quarter_prefers_amendment() -> None:
    """A 13F-HR/A should supersede the original 13F-HR for the same quarter."""
    rows = [
        {"form": "13F-HR", "accessionNumber": "ORIG", "filingDate": "2026-05-13",
         "reportDate": "2026-03-31"},
        {"form": "13F-HR/A", "accessionNumber": "AMEND", "filingDate": "2026-05-20",
         "reportDate": "2026-03-31"},
        {"form": "13F-HR", "accessionNumber": "Q4", "filingDate": "2026-02-12",
         "reportDate": "2025-12-31"},
    ]
    out = sec_13f._dedupe_filings_by_quarter(rows)
    # Most recent quarter first.
    assert out[0]["accessionNumber"] == "AMEND"
    assert out[0]["form"] == "13F-HR/A"
    assert out[1]["accessionNumber"] == "Q4"


# --- Conviction-signal classification --------------------------------------

def test_conviction_signal_accumulating() -> None:
    # 12 buyers, 4 sellers, 18 managers -> (12-4)/18 = 0.444 > 0.3 -> accumulating
    score, label = sec_13f._conviction_signal(12, 4, 18)
    assert label == "accumulating"
    assert score == pytest.approx(0.444, abs=0.01)


def test_conviction_signal_distributing() -> None:
    score, label = sec_13f._conviction_signal(2, 14, 18)
    assert label == "distributing"
    assert score < 0


def test_conviction_signal_steady_band() -> None:
    # 9 vs 9 with 20 managers -> 0/20 = 0, |0| < 0.1 -> steady
    _, label = sec_13f._conviction_signal(9, 9, 20)
    assert label == "steady"
    # 11 vs 9 with 20 managers -> 2/20 = 0.10, NOT strictly < 0.1 -> "mixed"
    _, label2 = sec_13f._conviction_signal(11, 9, 20)
    assert label2 == "mixed"


def test_conviction_signal_boundary_at_plus_point_three() -> None:
    # 0.3 is NOT > 0.3 (strict greater-than) -> "mixed"
    _, label = sec_13f._conviction_signal(13, 10, 10)
    # (13-10)/10 = 0.3 -> NOT > 0.3 -> falls through to "mixed"
    assert label == "mixed"
    # 0.4 IS > 0.3 -> accumulating
    _, label2 = sec_13f._conviction_signal(14, 10, 10)
    assert label2 == "accumulating"


def test_conviction_signal_boundary_at_minus_point_three() -> None:
    # (-0.3) is NOT < -0.3 -> "mixed"
    _, label = sec_13f._conviction_signal(7, 10, 10)
    assert label == "mixed"
    # (-0.4) IS < -0.3 -> distributing
    _, label2 = sec_13f._conviction_signal(6, 10, 10)
    assert label2 == "distributing"


def test_conviction_signal_empty_universe_returns_steady() -> None:
    score, label = sec_13f._conviction_signal(0, 0, 0)
    assert score == 0.0
    assert label == "steady"


def test_data_quality_thresholds() -> None:
    assert sec_13f._data_quality(15) == "good"
    assert sec_13f._data_quality(10) == "good"      # boundary
    assert sec_13f._data_quality(9) == "partial"
    assert sec_13f._data_quality(5) == "partial"    # boundary
    assert sec_13f._data_quality(4) == "sparse"
    assert sec_13f._data_quality(0) == "sparse"


# --- aggregate_holdings_for_ticker ----------------------------------------

def _mgr_filing(quarter: str, holdings: Dict[str, Dict[str, float]]) -> Dict[str, Any]:
    return {
        "form_type": "13F-HR",
        "period_of_report": quarter,
        "quarter": quarter,
        "manager_name": None,
        "holdings": holdings,
    }


def test_aggregate_happy_path_good_quality_accumulating() -> None:
    """15 managers all increased AAPL stake QoQ -> "good" + "accumulating"."""
    manager_data: Dict[str, List[Dict[str, Any]]] = {}
    for i in range(15):
        manager_data[f"M{i}"] = [
            _mgr_filing("2026-Q1", {"AAPL": {"shares": 1_000_000 + i*100_000, "value": 200_000_000 + i*20_000_000}}),
            _mgr_filing("2025-Q4", {"AAPL": {"shares": 900_000, "value": 180_000_000}}),
        ]
    out = sec_13f.aggregate_holdings_for_ticker("AAPL", manager_data, as_of_iso="2026-05-17")
    assert out["ticker"] == "AAPL"
    assert out["latest_quarter"] == "2026-Q1"
    assert out["manager_count"] == 15
    assert out["data_quality"] == "good"
    assert out["conviction_signal"] == "accumulating"
    assert out["signal_score"] == pytest.approx(1.0)
    assert out["quarter_over_quarter"]["net_buyers"] == 15
    assert out["quarter_over_quarter"]["net_sellers"] == 0
    # top_holders capped to 10.
    assert len(out["top_holders"]) == 10
    # Ranks start at 1 and the top holder has the largest value.
    assert out["top_holders"][0]["rank"] == 1
    assert out["top_holders"][0]["value_bn"] > out["top_holders"][-1]["value_bn"]


def test_aggregate_sparse_coverage() -> None:
    """Only 2 managers hold the ticker -> "sparse" quality."""
    manager_data: Dict[str, List[Dict[str, Any]]] = {
        "M1": [_mgr_filing("2026-Q1", {"OBSC": {"shares": 100_000, "value": 5_000_000}})],
        "M2": [_mgr_filing("2026-Q1", {"OBSC": {"shares": 50_000, "value": 2_500_000}})],
    }
    out = sec_13f.aggregate_holdings_for_ticker("OBSC", manager_data)
    assert out["manager_count"] == 2
    assert out["data_quality"] == "sparse"


def test_aggregate_no_holdings_returns_zero_managers() -> None:
    """Ticker that NO tracked manager holds -> manager_count=0, sparse, top_holders=[]."""
    manager_data: Dict[str, List[Dict[str, Any]]] = {
        "M1": [_mgr_filing("2026-Q1", {"AAPL": {"shares": 1_000_000, "value": 200_000_000}})],
    }
    out = sec_13f.aggregate_holdings_for_ticker("ZZZZ", manager_data)
    assert out["manager_count"] == 0
    assert out["data_quality"] == "sparse"
    assert out["top_holders"] == []
    assert out["total_shares_held_mm"] == 0.0


def test_aggregate_qoq_share_change_pct() -> None:
    """Latest 1.1M vs prior 1M shares -> +10% QoQ across one manager."""
    manager_data: Dict[str, List[Dict[str, Any]]] = {
        "M1": [
            _mgr_filing("2026-Q1", {"AAPL": {"shares": 1_100_000, "value": 220_000_000}}),
            _mgr_filing("2025-Q4", {"AAPL": {"shares": 1_000_000, "value": 200_000_000}}),
        ],
    }
    out = sec_13f.aggregate_holdings_for_ticker("AAPL", manager_data)
    assert out["quarter_over_quarter"]["share_change_pct"] == pytest.approx(10.0)
    assert out["quarter_over_quarter"]["net_buyers"] == 1
    assert out["quarter_over_quarter"]["prior_quarter"] == "2025-Q4"


def test_aggregate_manager_count_change_when_new_holder_appears() -> None:
    """Mgr A held last quarter; Mgr B is new this quarter -> manager_count_change=+1."""
    manager_data: Dict[str, List[Dict[str, Any]]] = {
        "A": [
            _mgr_filing("2026-Q1", {"AAPL": {"shares": 1_000_000, "value": 200_000_000}}),
            _mgr_filing("2025-Q4", {"AAPL": {"shares": 1_000_000, "value": 200_000_000}}),
        ],
        "B": [
            _mgr_filing("2026-Q1", {"AAPL": {"shares": 500_000, "value": 100_000_000}}),
            # No 2025-Q4 entry for B.
        ],
    }
    out = sec_13f.aggregate_holdings_for_ticker("AAPL", manager_data)
    assert out["manager_count"] == 2
    assert out["quarter_over_quarter"]["manager_count_change"] == 1


# --- Full-pipeline end-to-end (mocked HTTP) --------------------------------

def test_fetch_manager_13f_holdings_end_to_end(ua: None, fixed_today: date) -> None:
    """Single-manager happy path: submissions -> index.json -> primary_doc + info_table."""
    submissions = _make_submissions([
        {"form": "13F-HR", "filingDate": "2026-05-13",
         "accessionNumber": "0000000999-26-000001",
         "primaryDocument": "xslForm13F_X02/primary_doc.xml",
         "reportDate": "2026-03-31"},
    ])
    index = _index_json(["primary_doc.xml", "form13fInfoTable.xml"])
    side = _build_dispatcher(
        tickers_resp=_fake_response(TICKERS_FIXTURE),
        submissions_by_cik={"0000000999": _fake_response(submissions)},
        index_by_accession={"000000099926000001": _fake_response(index)},
        docs_by_suffix={
            "primary_doc.xml": _fake_response(_load_fixture("primary_doc_2026q1.xml"), text=True),
            "form13fInfoTable.xml": _fake_response(_load_fixture("info_table_2026q1_aapl_msft.xml"), text=True),
        },
    )
    with patch("lthcs.sources.sec_13f.requests.get", side_effect=side), \
         patch("lthcs.sources.sec_edgar.requests.get", side_effect=side):
        result = sec_13f.fetch_manager_13f_holdings(
            "0000000999", tickers=["AAPL", "MSFT", "BRK.B"]
        )
    assert len(result) == 1
    entry = result[0]
    assert entry["quarter"] == "2026-Q1"
    assert entry["period_of_report"] == "2026-03-31"
    assert entry["form_type"] == "13F-HR"
    assert entry["manager_name"] == "Example Manager Inc."
    assert entry["holdings"]["AAPL"]["shares"] == 7_500_000.0
    assert entry["holdings"]["MSFT"]["shares"] == 2_000_000.0


def test_fetch_universe_full_path(ua: None, fixed_today: date) -> None:
    """End-to-end through fetch_universe_institutional_holdings with two managers."""
    # Two managers, both filing 2026-Q1 + 2025-Q4. AAPL holdings:
    #   Mgr1: Q1=7.5M, Q4=4.5M -> buyer
    #   Mgr2: Q1=7.5M, Q4=4.5M -> buyer
    submissions_a = _make_submissions([
        {"form": "13F-HR", "filingDate": "2026-05-13",
         "accessionNumber": "0000000999-26-000001",
         "primaryDocument": "xslForm13F_X02/primary_doc.xml",
         "reportDate": "2026-03-31"},
        {"form": "13F-HR", "filingDate": "2026-02-12",
         "accessionNumber": "0000000999-26-000002",
         "primaryDocument": "xslForm13F_X02/primary_doc.xml",
         "reportDate": "2025-12-31"},
    ])
    submissions_b = _make_submissions([
        {"form": "13F-HR", "filingDate": "2026-05-15",
         "accessionNumber": "0000000888-26-000003",
         "primaryDocument": "xslForm13F_X02/primary_doc.xml",
         "reportDate": "2026-03-31"},
        {"form": "13F-HR", "filingDate": "2026-02-14",
         "accessionNumber": "0000000888-26-000004",
         "primaryDocument": "xslForm13F_X02/primary_doc.xml",
         "reportDate": "2025-12-31"},
    ])
    index = _index_json(["primary_doc.xml", "form13fInfoTable.xml"])
    side = _build_dispatcher(
        tickers_resp=_fake_response(TICKERS_FIXTURE),
        submissions_by_cik={
            "0000000999": _fake_response(submissions_a),
            "0000000888": _fake_response(submissions_b),
        },
        index_by_accession={
            "000000099926000001": _fake_response(index),
            "000000099926000002": _fake_response(index),
            "000000088826000003": _fake_response(index),
            "000000088826000004": _fake_response(index),
        },
        docs_by_suffix={
            "primary_doc.xml": _fake_response(_load_fixture("primary_doc_2026q1.xml"), text=True),
            "form13fInfoTable.xml": _fake_response(_load_fixture("info_table_2026q1_aapl_msft.xml"), text=True),
        },
    )
    # We need different primary docs per quarter — overload by index+doc combo.
    # Simpler: keep the same primary_doc.xml for both filings (both will say Q1=2026-03-31).
    # That makes Q4 incorrectly parse as Q1; instead route by URL more precisely.
    def _routing_side(url: str, *args: Any, **kwargs: Any) -> MagicMock:
        if "submissions/CIK0000000999" in url:
            return _fake_response(submissions_a)
        if "submissions/CIK0000000888" in url:
            return _fake_response(submissions_b)
        if url.endswith("/index.json"):
            return _fake_response(index)
        if url.endswith("/primary_doc.xml"):
            # Look at the accession in the URL to know which quarter.
            if "000000099926000002" in url or "000000088826000004" in url:
                return _fake_response(_load_fixture("primary_doc_2025q4.xml"), text=True)
            return _fake_response(_load_fixture("primary_doc_2026q1.xml"), text=True)
        if url.endswith("/form13fInfoTable.xml"):
            if "000000099926000002" in url or "000000088826000004" in url:
                return _fake_response(_load_fixture("info_table_2025q4_aapl_only.xml"), text=True)
            return _fake_response(_load_fixture("info_table_2026q1_aapl_msft.xml"), text=True)
        if "company_tickers.json" in url or url.endswith(".json"):
            return _fake_response(TICKERS_FIXTURE)
        raise AssertionError(f"No fixture for URL: {url}")

    with patch("lthcs.sources.sec_13f.requests.get", side_effect=_routing_side), \
         patch("lthcs.sources.sec_edgar.requests.get", side_effect=_routing_side):
        result = sec_13f.fetch_universe_institutional_holdings(
            ["AAPL", "MSFT"],
            managers={"MgrA": "0000000999", "MgrB": "0000000888"},
        )

    assert "AAPL" in result
    assert "MSFT" in result
    aapl = result["AAPL"]
    assert aapl["manager_count"] == 2
    # Both managers increased: 7.5M (Q1) vs 4.5M (Q4) -> +66.67% QoQ overall.
    assert aapl["quarter_over_quarter"]["net_buyers"] == 2
    assert aapl["quarter_over_quarter"]["net_sellers"] == 0
    # Sparse since only 2 managers.
    assert aapl["data_quality"] == "sparse"
    # Total shares 7.5M + 7.5M = 15M, in millions = 15.0.
    assert aapl["total_shares_held_mm"] == pytest.approx(15.0)


# --- Defensive paths -------------------------------------------------------

def test_fetch_manager_13f_holdings_submissions_429_returns_empty(
    ua: None, fixed_today: date
) -> None:
    """A rate-limited submissions endpoint should NOT raise."""
    def _side(url: str, *args: Any, **kwargs: Any) -> MagicMock:
        return _fake_response(None, status=429)
    with patch("lthcs.sources.sec_13f.requests.get", side_effect=_side):
        out = sec_13f.fetch_manager_13f_holdings("0000000999", tickers=["AAPL"])
    assert out == []


def test_fetch_universe_no_managers_returns_empty_dict(ua: None, fixed_today: date) -> None:
    result = sec_13f.fetch_universe_institutional_holdings(
        [], managers={}
    )
    assert result == {}


def test_fetch_manager_missing_user_agent_raises(
    monkeypatch: pytest.MonkeyPatch, fixed_today: date
) -> None:
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    def _side(url: str, *args: Any, **kwargs: Any) -> MagicMock:
        return _fake_response(None, status=200)
    with patch("lthcs.sources.sec_13f.requests.get", side_effect=_side):
        with pytest.raises(sec_edgar.SECEdgarError, match="SEC_USER_AGENT"):
            sec_13f.fetch_manager_13f_holdings("0000000999", tickers=["AAPL"])


# --- Module surface --------------------------------------------------------

def test_module_exports_public_api() -> None:
    expected = {
        "SECEdgarError",
        "TRACKED_MANAGERS",
        "TICKER_TO_CUSIP",
        "fetch_manager_13f_holdings",
        "fetch_universe_institutional_holdings",
        "aggregate_holdings_for_ticker",
    }
    assert set(sec_13f.__all__) == expected


def test_tracked_managers_has_twenty_entries() -> None:
    """Phase 1 spec calls for top 20 managers; we have 21 (Capital Group's
    two-CIK structure adds one extra entry)."""
    assert 20 <= len(sec_13f.TRACKED_MANAGERS) <= 22
    # All CIKs are 10-digit zero-padded.
    for name, cik in sec_13f.TRACKED_MANAGERS.items():
        assert len(cik) == 10
        assert cik.isdigit()
