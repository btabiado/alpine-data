"""Tests for lthcs.sources.sec_form4.

All HTTP is mocked -- no live network. We isolate the module-level
``_cache`` to a per-test tmp_path FileCache (and the sec_edgar
tickers-file cache too).
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from lthcs.sources._cache import FileCache
from lthcs.sources import sec_edgar, sec_form4


FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "sec_form4"


# --- Fixtures ---------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_caches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-test FileCache for both sec_form4 and the sec_edgar tickers cache.

    Without this, tests would share cache state across the whole
    pytest run and a flaky write order could let one test see another
    test's HTTP mock results.
    """
    fresh_form4 = FileCache("sec_form4", root=tmp_path)
    monkeypatch.setattr(sec_form4, "_cache", fresh_form4)
    fresh_edgar = FileCache("sec_edgar", root=tmp_path)
    monkeypatch.setattr(sec_edgar, "_cache", fresh_edgar)


@pytest.fixture()
def ua(monkeypatch: pytest.MonkeyPatch) -> None:
    """SEC_USER_AGENT must be set for any code path that hits HTTP."""
    monkeypatch.setenv("SEC_USER_AGENT", "Test bot test@example.com")


@pytest.fixture()
def fixed_today(monkeypatch: pytest.MonkeyPatch) -> date:
    """Pin the calendar so window filtering is deterministic."""
    fixed = date(2026, 5, 17)
    monkeypatch.setattr(sec_form4, "_today", lambda: fixed)
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


# SEC tickers map subset used by all tests with a CIK lookup.
TICKERS_FIXTURE: Dict[str, Dict[str, Any]] = {
    "0": {"cik_str": 999, "ticker": "EXMP", "title": "Example Corp"},
    "1": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
}


def _make_submissions(filings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Pivot row dicts into SEC's column-major ``filings.recent`` shape."""
    cols = ("form", "filingDate", "accessionNumber", "primaryDocument")
    recent: Dict[str, List[Any]] = {c: [] for c in cols}
    for f in filings:
        for c in cols:
            recent[c].append(f.get(c, ""))
    return {
        "cik": "0000000999",
        "name": "Example Corp",
        "filings": {"recent": recent},
    }


def _load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text()


def _build_dispatcher(
    tickers_resp: MagicMock,
    submissions_resp: Optional[MagicMock],
    filing_bodies: Optional[Dict[str, MagicMock]] = None,
):
    """Route patched ``requests.get`` to the right fake response by URL.

    ``filing_bodies`` keys are filename suffixes — we match the last
    path segment of the URL so the test only needs to know its fixture
    filename, not the full archive path.
    """
    filing_bodies = filing_bodies or {}

    def _side_effect(url: str, *args: Any, **kwargs: Any) -> MagicMock:
        if "submissions/" in url:
            if submissions_resp is None:
                raise AssertionError(
                    "submissions URL hit but no submissions response configured"
                )
            return submissions_resp
        if "/Archives/edgar/data/" in url:
            for suffix, resp in filing_bodies.items():
                if url.endswith(suffix):
                    return resp
            raise AssertionError(
                "No filing-body fixture matches URL: {}".format(url)
            )
        return tickers_resp

    return _side_effect


# --- parse_form4_xml: pure-parser tests ------------------------------------

def test_parse_ceo_open_market_buy_extracts_role_and_transaction() -> None:
    xml = _load_fixture("ceo_open_market_buy.xml")
    parsed = sec_form4.parse_form4_xml(xml)
    assert parsed is not None
    assert parsed["issuer_ticker"] == "EXMP"
    assert parsed["owner_name"] == "Smith Jane Q"
    # CEO must be detected via officer_title (not the isOfficer flag alone).
    assert parsed["role"] == "CEO"
    assert parsed["officer_title"] == "Chief Executive Officer"
    assert parsed["aff10b5_one"] is False
    assert len(parsed["transactions"]) == 1
    tx = parsed["transactions"][0]
    assert tx["code"] == "P"
    assert tx["shares"] == 10000.0
    assert tx["price"] == 125.0
    assert tx["value"] == pytest.approx(10000 * 125.0)
    assert tx["acquired_disposed"] == "A"
    assert tx["planned_10b5_1"] is False


def test_parse_officer_10b5_1_sale_flags_planned_via_footnote() -> None:
    """The fixture has aff10b5One=1 AND a footnote — both signals should
    converge on planned_10b5_1=True so the transaction is filtered later."""
    xml = _load_fixture("officer_10b5_1_sale.xml")
    parsed = sec_form4.parse_form4_xml(xml)
    assert parsed is not None
    assert parsed["role"] == "Officer"  # PAO is not CEO/CFO
    assert parsed["aff10b5_one"] is True
    assert len(parsed["transactions"]) == 1
    tx = parsed["transactions"][0]
    assert tx["code"] == "S"
    assert tx["planned_10b5_1"] is True


def test_parse_routine_grant_and_exercise_keeps_all_transactions() -> None:
    """The parser surfaces ALL non-derivative transactions; filtering of
    mechanical codes (A/M/F) happens at aggregation time."""
    xml = _load_fixture("routine_grant_and_exercise.xml")
    parsed = sec_form4.parse_form4_xml(xml)
    assert parsed is not None
    assert parsed["role"] == "Director"
    codes = [tx["code"] for tx in parsed["transactions"]]
    assert codes == ["A", "M", "F"]


def test_parse_returns_none_for_malformed_xml() -> None:
    """Garbage XML must NOT raise — we return None and skip the filing."""
    assert sec_form4.parse_form4_xml("<<not really xml>>") is None
    assert sec_form4.parse_form4_xml("") is None


def test_parse_returns_none_when_no_non_derivative_transactions() -> None:
    """Some Form 4s only have derivative (options) activity. V1 skips them."""
    xml = """<?xml version="1.0"?>
    <ownershipDocument>
        <documentType>4</documentType>
        <issuer>
            <issuerCik>0000000999</issuerCik>
            <issuerName>Example Corp</issuerName>
            <issuerTradingSymbol>EXMP</issuerTradingSymbol>
        </issuer>
        <reportingOwner>
            <reportingOwnerId>
                <rptOwnerName>Empty</rptOwnerName>
            </reportingOwnerId>
            <reportingOwnerRelationship>
                <isOfficer>1</isOfficer>
                <officerTitle>CFO</officerTitle>
            </reportingOwnerRelationship>
        </reportingOwner>
        <aff10b5One>0</aff10b5One>
        <derivativeTable>
            <derivativeTransaction>
                <transactionCoding><transactionCode>A</transactionCode></transactionCoding>
            </derivativeTransaction>
        </derivativeTable>
    </ownershipDocument>"""
    assert sec_form4.parse_form4_xml(xml) is None


def test_parse_role_classifies_cfo_via_officer_title_abbreviation() -> None:
    """`CFO` as a literal title (no `Chief Financial`) must still map."""
    xml = """<?xml version="1.0"?>
    <ownershipDocument>
        <issuer><issuerTradingSymbol>EXMP</issuerTradingSymbol></issuer>
        <reportingOwner>
            <reportingOwnerId><rptOwnerName>X</rptOwnerName></reportingOwnerId>
            <reportingOwnerRelationship>
                <isOfficer>1</isOfficer>
                <officerTitle>CFO and Treasurer</officerTitle>
            </reportingOwnerRelationship>
        </reportingOwner>
        <aff10b5One>0</aff10b5One>
        <nonDerivativeTable>
            <nonDerivativeTransaction>
                <transactionDate><value>2026-05-01</value></transactionDate>
                <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
                <transactionAmounts>
                    <transactionShares><value>100</value></transactionShares>
                    <transactionPricePerShare><value>10</value></transactionPricePerShare>
                    <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
                </transactionAmounts>
            </nonDerivativeTransaction>
        </nonDerivativeTable>
    </ownershipDocument>"""
    parsed = sec_form4.parse_form4_xml(xml)
    assert parsed is not None
    assert parsed["role"] == "CFO"


def test_parse_role_classifies_ten_percent_holder() -> None:
    xml = """<?xml version="1.0"?>
    <ownershipDocument>
        <issuer><issuerTradingSymbol>EXMP</issuerTradingSymbol></issuer>
        <reportingOwner>
            <reportingOwnerId><rptOwnerName>BigFund</rptOwnerName></reportingOwnerId>
            <reportingOwnerRelationship>
                <isOfficer>0</isOfficer>
                <isDirector>0</isDirector>
                <isTenPercentOwner>1</isTenPercentOwner>
            </reportingOwnerRelationship>
        </reportingOwner>
        <aff10b5One>0</aff10b5One>
        <nonDerivativeTable>
            <nonDerivativeTransaction>
                <transactionDate><value>2026-05-01</value></transactionDate>
                <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
                <transactionAmounts>
                    <transactionShares><value>1</value></transactionShares>
                    <transactionPricePerShare><value>1</value></transactionPricePerShare>
                    <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
                </transactionAmounts>
            </nonDerivativeTransaction>
        </nonDerivativeTable>
    </ownershipDocument>"""
    parsed = sec_form4.parse_form4_xml(xml)
    assert parsed is not None
    assert parsed["role"] == "TenPercent"


# --- _is_discretionary / filtering --------------------------------------

def test_is_discretionary_open_market_buy_kept() -> None:
    tx = {"code": "P", "acquired_disposed": "A", "planned_10b5_1": False}
    assert sec_form4._is_discretionary(tx) is True


def test_is_discretionary_filters_10b5_1_sale() -> None:
    tx = {"code": "S", "acquired_disposed": "D", "planned_10b5_1": True}
    assert sec_form4._is_discretionary(tx) is False


def test_is_discretionary_filters_award_grant() -> None:
    tx = {"code": "A", "acquired_disposed": "A", "planned_10b5_1": False}
    assert sec_form4._is_discretionary(tx) is False


def test_is_discretionary_filters_option_exercise() -> None:
    tx = {"code": "M", "acquired_disposed": "A", "planned_10b5_1": False}
    assert sec_form4._is_discretionary(tx) is False


# --- Conviction-score normalization ----------------------------------------

def test_conviction_score_signed_log_scales_correctly() -> None:
    """A $10M net buy must saturate near +1, $0 must be 0, $10M sell at ~-1."""
    s_pos = sec_form4._conviction_score_from_net_dollars(10_000_000)
    s_neg = sec_form4._conviction_score_from_net_dollars(-10_000_000)
    assert s_pos == pytest.approx(1.0, abs=1e-6)
    assert s_neg == pytest.approx(-1.0, abs=1e-6)
    assert sec_form4._conviction_score_from_net_dollars(0) == 0.0
    # Small flows are subdued (not zero, not 1).
    s_small = sec_form4._conviction_score_from_net_dollars(100_000)
    assert 0.1 < s_small < 0.8


def test_regime_classification_boundaries() -> None:
    assert sec_form4._regime_for_score(0.6) == "strong_buying"
    assert sec_form4._regime_for_score(0.5) == "strong_buying"     # >= boundary
    assert sec_form4._regime_for_score(0.3) == "mild_buying"
    assert sec_form4._regime_for_score(0.1) == "mild_buying"       # >= boundary
    assert sec_form4._regime_for_score(0.0) == "neutral"
    assert sec_form4._regime_for_score(-0.05) == "neutral"
    assert sec_form4._regime_for_score(-0.1) == "mild_selling"     # <= boundary
    assert sec_form4._regime_for_score(-0.3) == "mild_selling"
    assert sec_form4._regime_for_score(-0.5) == "heavy_selling"    # <= boundary
    assert sec_form4._regime_for_score(-0.9) == "heavy_selling"


# --- Cluster-buying detection ----------------------------------------------

def test_detect_cluster_buying_three_insiders_in_window() -> None:
    """Three distinct insiders buying within 14d should trip the flag."""
    events = [
        (date(2026, 5, 1), "CIK1"),
        (date(2026, 5, 5), "CIK2"),
        (date(2026, 5, 10), "CIK3"),
    ]
    assert sec_form4._detect_cluster_buying(events) is True


def test_detect_cluster_buying_same_insider_three_times_does_not_trip() -> None:
    """Same person buying 3x is NOT cluster buying — must be distinct insiders."""
    events = [
        (date(2026, 5, 1), "CIK1"),
        (date(2026, 5, 5), "CIK1"),
        (date(2026, 5, 10), "CIK1"),
    ]
    assert sec_form4._detect_cluster_buying(events) is False


def test_detect_cluster_buying_outside_window_does_not_trip() -> None:
    """Three insiders but spread over 30d: not a cluster."""
    events = [
        (date(2026, 4, 1), "CIK1"),
        (date(2026, 4, 16), "CIK2"),
        (date(2026, 5, 5), "CIK3"),
    ]
    assert sec_form4._detect_cluster_buying(events) is False


# --- fetch_insider_transactions: end-to-end -------------------------------

def test_fetch_insider_transactions_ceo_buy_produces_buying_regime(
    ua: None, fixed_today: date
) -> None:
    """Happy path: a single CEO open-market purchase, recent date, no 10b5-1.
    Expect buy_count=1, conviction_score positive, regime mild/strong_buying.
    """
    submissions = _make_submissions([
        {"form": "4", "filingDate": "2026-05-09",
         "accessionNumber": "0000000999-26-000001",
         "primaryDocument": "form4.xml"},
    ])
    side = _build_dispatcher(
        _fake_response(TICKERS_FIXTURE),
        _fake_response(submissions),
        filing_bodies={
            "form4.xml": _fake_response(
                _load_fixture("ceo_open_market_buy.xml"), text=True
            ),
        },
    )
    with patch("lthcs.sources.sec_form4.requests.get", side_effect=side), \
         patch("lthcs.sources.sec_edgar.requests.get", side_effect=side):
        result = sec_form4.fetch_insider_transactions("EXMP", window_days=90)

    assert result is not None
    assert result["ticker"] == "EXMP"
    assert result["window_days"] == 90
    assert result["buy_count"] == 1
    assert result["sell_count"] == 0
    assert result["buy_dollar_value"] == pytest.approx(10000 * 125.0)
    assert result["net_dollar_value"] == pytest.approx(1_250_000)
    # CEO buy weight is _WEIGHT_CEO (3.0).
    assert result["weighted_buy_score"] == pytest.approx(3.0)
    assert result["weighted_sell_score"] == 0.0
    assert result["net_weighted_score"] == pytest.approx(3.0)
    # Conviction is positive; for $1.25M net buy it's around 0.86.
    assert result["conviction_score"] > 0.5
    assert result["regime"] == "strong_buying"
    assert result["ceo_cfo_action"] == "buying"
    assert result["cluster_buying"] is False
    assert result["filtered_out_count"] == 0
    # raw_transactions is capped and contains the buy.
    assert len(result["raw_transactions"]) == 1
    assert result["raw_transactions"][0]["code"] == "P"


def test_fetch_insider_transactions_10b5_1_sale_is_filtered(
    ua: None, fixed_today: date
) -> None:
    """A 10b5-1 sale must NOT count toward sell_count — but filings list
    isn't empty so we still get a populated dict with filtered_out_count=1."""
    submissions = _make_submissions([
        {"form": "4", "filingDate": "2026-05-07",
         "accessionNumber": "0000000999-26-000002",
         "primaryDocument": "form4.xml"},
    ])
    side = _build_dispatcher(
        _fake_response(TICKERS_FIXTURE),
        _fake_response(submissions),
        filing_bodies={
            "form4.xml": _fake_response(
                _load_fixture("officer_10b5_1_sale.xml"), text=True
            ),
        },
    )
    with patch("lthcs.sources.sec_form4.requests.get", side_effect=side), \
         patch("lthcs.sources.sec_edgar.requests.get", side_effect=side):
        result = sec_form4.fetch_insider_transactions("EXMP", window_days=90)

    assert result is not None
    assert result["buy_count"] == 0
    assert result["sell_count"] == 0  # 10b5-1 filtered
    assert result["filtered_out_count"] == 1
    assert result["conviction_score"] == 0.0
    assert result["regime"] == "neutral"
    # But raw_transactions still shows the filing happened.
    assert len(result["raw_transactions"]) == 1
    assert result["raw_transactions"][0]["planned_10b5_1"] is True


def test_fetch_insider_transactions_filters_awards_and_exercises(
    ua: None, fixed_today: date
) -> None:
    """A filing with only A/M/F transactions should contribute zero signal."""
    submissions = _make_submissions([
        {"form": "4", "filingDate": "2026-04-16",
         "accessionNumber": "0000000999-26-000003",
         "primaryDocument": "form4.xml"},
    ])
    side = _build_dispatcher(
        _fake_response(TICKERS_FIXTURE),
        _fake_response(submissions),
        filing_bodies={
            "form4.xml": _fake_response(
                _load_fixture("routine_grant_and_exercise.xml"), text=True
            ),
        },
    )
    with patch("lthcs.sources.sec_form4.requests.get", side_effect=side), \
         patch("lthcs.sources.sec_edgar.requests.get", side_effect=side):
        result = sec_form4.fetch_insider_transactions("EXMP", window_days=90)

    assert result is not None
    assert result["buy_count"] == 0
    assert result["sell_count"] == 0
    # 3 transactions, all filtered (A, M, F).
    assert result["filtered_out_count"] == 3


def test_fetch_insider_transactions_role_weighting_ceo_vs_director(
    ua: None, fixed_today: date
) -> None:
    """CEO buy weighted 3.0; Director buy weighted 1.0 — net score should
    reflect the role multipliers, not just transaction counts."""
    # Build two filings: one CEO, one Director, both equal-size open buys.
    director_xml = _load_fixture("ceo_open_market_buy.xml").replace(
        "Chief Executive Officer", "Independent Director"
    ).replace(
        "<isDirector>0</isDirector>", "<isDirector>1</isDirector>"
    ).replace(
        "<isOfficer>1</isOfficer>", "<isOfficer>0</isOfficer>"
    )
    submissions = _make_submissions([
        {"form": "4", "filingDate": "2026-05-09",
         "accessionNumber": "0000000999-26-000010",
         "primaryDocument": "ceo.xml"},
        {"form": "4", "filingDate": "2026-05-09",
         "accessionNumber": "0000000999-26-000011",
         "primaryDocument": "director.xml"},
    ])
    side = _build_dispatcher(
        _fake_response(TICKERS_FIXTURE),
        _fake_response(submissions),
        filing_bodies={
            "ceo.xml": _fake_response(_load_fixture("ceo_open_market_buy.xml"), text=True),
            "director.xml": _fake_response(director_xml, text=True),
        },
    )
    with patch("lthcs.sources.sec_form4.requests.get", side_effect=side), \
         patch("lthcs.sources.sec_edgar.requests.get", side_effect=side):
        result = sec_form4.fetch_insider_transactions("EXMP", window_days=90)

    assert result is not None
    assert result["buy_count"] == 2
    # weighted_buy = CEO (3.0) + Director (1.0) = 4.0.
    assert result["weighted_buy_score"] == pytest.approx(4.0)


def test_fetch_insider_transactions_cluster_buying_detected(
    ua: None, fixed_today: date
) -> None:
    """3 distinct insiders, all buying within 14d => cluster_buying=True."""
    base = _load_fixture("ceo_open_market_buy.xml")

    def _variant(name: str, cik: str) -> str:
        # Build a near-duplicate XML with different owner identity.
        out = base.replace("Smith Jane Q", name)
        out = out.replace("0001999991", cik)
        return out

    submissions = _make_submissions([
        {"form": "4", "filingDate": "2026-05-09",
         "accessionNumber": "A1", "primaryDocument": "a1.xml"},
        {"form": "4", "filingDate": "2026-05-10",
         "accessionNumber": "A2", "primaryDocument": "a2.xml"},
        {"form": "4", "filingDate": "2026-05-12",
         "accessionNumber": "A3", "primaryDocument": "a3.xml"},
    ])
    side = _build_dispatcher(
        _fake_response(TICKERS_FIXTURE),
        _fake_response(submissions),
        filing_bodies={
            "a1.xml": _fake_response(base, text=True),
            "a2.xml": _fake_response(_variant("Jones Bob", "0001999992"), text=True),
            "a3.xml": _fake_response(_variant("Lee Pat", "0001999993"), text=True),
        },
    )
    with patch("lthcs.sources.sec_form4.requests.get", side_effect=side), \
         patch("lthcs.sources.sec_edgar.requests.get", side_effect=side):
        result = sec_form4.fetch_insider_transactions("EXMP", window_days=90)

    assert result is not None
    assert result["buy_count"] == 3
    assert result["cluster_buying"] is True


def test_fetch_insider_transactions_net_dollar_arithmetic(
    ua: None, fixed_today: date
) -> None:
    """Mix of one CEO buy ($1.25M) and one open-market sale ($2.5M from a
    director). net = 1.25M - 2.5M = -1.25M; conviction should be negative.

    We construct a sale XML by editing the buy fixture so the test is
    self-contained."""
    sale_xml = _load_fixture("ceo_open_market_buy.xml").replace(
        "Chief Executive Officer", "Director"
    ).replace(
        "<isOfficer>1</isOfficer>", "<isOfficer>0</isOfficer>"
    ).replace(
        "<isDirector>0</isDirector>", "<isDirector>1</isDirector>"
    ).replace(
        "<transactionCode>P</transactionCode>", "<transactionCode>S</transactionCode>"
    ).replace(
        "<value>10000</value>", "<value>20000</value>"
    ).replace(
        "<value>125.00</value>", "<value>125.00</value>"
    ).replace(
        "<value>A</value>\n                </transactionAcquiredDisposedCode>",
        "<value>D</value>\n                </transactionAcquiredDisposedCode>",
    ).replace("Smith Jane Q", "Director Pat")

    submissions = _make_submissions([
        {"form": "4", "filingDate": "2026-05-09",
         "accessionNumber": "B1", "primaryDocument": "buy.xml"},
        {"form": "4", "filingDate": "2026-05-10",
         "accessionNumber": "S1", "primaryDocument": "sell.xml"},
    ])
    side = _build_dispatcher(
        _fake_response(TICKERS_FIXTURE),
        _fake_response(submissions),
        filing_bodies={
            "buy.xml": _fake_response(_load_fixture("ceo_open_market_buy.xml"), text=True),
            "sell.xml": _fake_response(sale_xml, text=True),
        },
    )
    with patch("lthcs.sources.sec_form4.requests.get", side_effect=side), \
         patch("lthcs.sources.sec_edgar.requests.get", side_effect=side):
        result = sec_form4.fetch_insider_transactions("EXMP", window_days=90)

    assert result is not None
    assert result["buy_count"] == 1
    assert result["sell_count"] == 1
    assert result["buy_dollar_value"] == pytest.approx(1_250_000)
    assert result["sell_dollar_value"] == pytest.approx(2_500_000)
    assert result["net_dollar_value"] == pytest.approx(-1_250_000)
    # Conviction is negative.
    assert result["conviction_score"] < 0
    # ceo_cfo_action only looks at CEO+CFO — director sells don't affect it.
    assert result["ceo_cfo_action"] == "buying"


def test_fetch_insider_transactions_no_filings_returns_none(
    ua: None, fixed_today: date
) -> None:
    """A ticker resolved to a CIK but with zero Form 4 filings -> None."""
    submissions = _make_submissions([])  # empty filings.recent
    side = _build_dispatcher(
        _fake_response(TICKERS_FIXTURE),
        _fake_response(submissions),
        filing_bodies={},
    )
    with patch("lthcs.sources.sec_form4.requests.get", side_effect=side), \
         patch("lthcs.sources.sec_edgar.requests.get", side_effect=side):
        result = sec_form4.fetch_insider_transactions("EXMP", window_days=90)

    assert result is None


def test_fetch_insider_transactions_unknown_ticker_returns_none(ua: None) -> None:
    """Ticker not in SEC tickers map -> None; submissions URL never hit."""
    side = _build_dispatcher(
        _fake_response(TICKERS_FIXTURE),
        submissions_resp=None,
        filing_bodies={},
    )
    with patch("lthcs.sources.sec_form4.requests.get", side_effect=side) as gm, \
         patch("lthcs.sources.sec_edgar.requests.get", side_effect=side):
        result = sec_form4.fetch_insider_transactions("ZZZZ", window_days=90)

    assert result is None
    for call in gm.call_args_list:
        assert "submissions/" not in call.args[0]


def test_fetch_insider_transactions_outside_window_excluded(
    ua: None, fixed_today: date
) -> None:
    """A Form 4 filed 120 days ago must be excluded from a 90d window."""
    old = (fixed_today - timedelta(days=120)).isoformat()
    submissions = _make_submissions([
        {"form": "4", "filingDate": old, "accessionNumber": "OLD",
         "primaryDocument": "form4.xml"},
    ])
    side = _build_dispatcher(
        _fake_response(TICKERS_FIXTURE),
        _fake_response(submissions),
        filing_bodies={"form4.xml": _fake_response("", status=200, text=True)},
    )
    with patch("lthcs.sources.sec_form4.requests.get", side_effect=side), \
         patch("lthcs.sources.sec_edgar.requests.get", side_effect=side):
        result = sec_form4.fetch_insider_transactions("EXMP", window_days=90)

    # No filings inside the window -> None.
    assert result is None


def test_fetch_insider_transactions_malformed_xml_skipped(
    ua: None, fixed_today: date
) -> None:
    """A malformed Form 4 XML body must be skipped; other filings still parse."""
    submissions = _make_submissions([
        {"form": "4", "filingDate": "2026-05-09",
         "accessionNumber": "BAD", "primaryDocument": "bad.xml"},
        {"form": "4", "filingDate": "2026-05-08",
         "accessionNumber": "GOOD", "primaryDocument": "good.xml"},
    ])
    side = _build_dispatcher(
        _fake_response(TICKERS_FIXTURE),
        _fake_response(submissions),
        filing_bodies={
            "bad.xml": _fake_response("<<not really xml>>", text=True),
            "good.xml": _fake_response(_load_fixture("ceo_open_market_buy.xml"), text=True),
        },
    )
    with patch("lthcs.sources.sec_form4.requests.get", side_effect=side), \
         patch("lthcs.sources.sec_edgar.requests.get", side_effect=side):
        result = sec_form4.fetch_insider_transactions("EXMP", window_days=90)

    assert result is not None
    # Only the GOOD filing's one P transaction made it through.
    assert result["buy_count"] == 1


def test_fetch_insider_transactions_submissions_429_returns_none(
    ua: None, fixed_today: date
) -> None:
    """SEC rate-limiting the submissions endpoint must NOT raise."""
    side = _build_dispatcher(
        _fake_response(TICKERS_FIXTURE),
        _fake_response(None, status=429),
        filing_bodies={},
    )
    with patch("lthcs.sources.sec_form4.requests.get", side_effect=side), \
         patch("lthcs.sources.sec_edgar.requests.get", side_effect=side):
        result = sec_form4.fetch_insider_transactions("EXMP", window_days=90)
    assert result is None


def test_fetch_insider_transactions_raises_without_user_agent(
    monkeypatch: pytest.MonkeyPatch, fixed_today: date
) -> None:
    """SEC_USER_AGENT missing => SECEdgarError surfaces immediately."""
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    side = _build_dispatcher(
        _fake_response(TICKERS_FIXTURE),
        submissions_resp=None,
        filing_bodies={},
    )
    with patch("lthcs.sources.sec_form4.requests.get", side_effect=side), \
         patch("lthcs.sources.sec_edgar.requests.get", side_effect=side):
        with pytest.raises(sec_edgar.SECEdgarError, match="SEC_USER_AGENT"):
            sec_form4.fetch_insider_transactions("EXMP", window_days=90)


# --- fetch_universe_insider_transactions ----------------------------------

def test_fetch_universe_mixed_coverage(ua: None, fixed_today: date) -> None:
    """One ticker has filings, one is unknown — output contains only the
    ticker with usable data."""
    submissions = _make_submissions([
        {"form": "4", "filingDate": "2026-05-09",
         "accessionNumber": "U1", "primaryDocument": "form4.xml"},
    ])
    side = _build_dispatcher(
        _fake_response(TICKERS_FIXTURE),
        _fake_response(submissions),
        filing_bodies={
            "form4.xml": _fake_response(_load_fixture("ceo_open_market_buy.xml"), text=True),
        },
    )
    with patch("lthcs.sources.sec_form4.requests.get", side_effect=side), \
         patch("lthcs.sources.sec_edgar.requests.get", side_effect=side):
        result = sec_form4.fetch_universe_insider_transactions(
            ["EXMP", "ZZZZ"], window_days=90
        )

    assert "EXMP" in result
    assert "ZZZZ" not in result
    assert result["EXMP"]["buy_count"] == 1


def test_fetch_universe_dedupes_and_normalizes(ua: None, fixed_today: date) -> None:
    """Duplicate / lowercase tickers must be folded together."""
    submissions = _make_submissions([
        {"form": "4", "filingDate": "2026-05-09",
         "accessionNumber": "U1", "primaryDocument": "form4.xml"},
    ])
    side = _build_dispatcher(
        _fake_response(TICKERS_FIXTURE),
        _fake_response(submissions),
        filing_bodies={
            "form4.xml": _fake_response(_load_fixture("ceo_open_market_buy.xml"), text=True),
        },
    )
    with patch("lthcs.sources.sec_form4.requests.get", side_effect=side), \
         patch("lthcs.sources.sec_edgar.requests.get", side_effect=side):
        result = sec_form4.fetch_universe_insider_transactions(
            ["EXMP", "exmp", "EXMP", ""], window_days=90
        )

    assert list(result.keys()) == ["EXMP"]


# --- Module surface sanity --------------------------------------------------

def test_module_exports_public_api() -> None:
    """All documented public functions must be in __all__."""
    expected = {
        "SECEdgarError",
        "fetch_insider_transactions",
        "fetch_universe_insider_transactions",
        "parse_form4_xml",
    }
    assert set(sec_form4.__all__) == expected
