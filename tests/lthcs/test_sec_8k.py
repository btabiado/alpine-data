"""Tests for lthcs.sources.sec_8k.

All HTTP is mocked -- no live network calls. We monkeypatch the
module-level ``_cache`` to a per-test tmp_path FileCache so the SEC
EDGAR XBRL cache (a separate module) is untouched.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest
import requests

from lthcs.sources._cache import FileCache
from lthcs.sources import sec_8k, sec_edgar


# --- Fixtures ---------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_caches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point both the sec_8k cache AND the sec_edgar tickers cache at a
    per-test directory so nothing leaks across tests or into the real
    ``.cache/lthcs`` tree.
    """
    fresh_8k = FileCache("sec_8k", root=tmp_path)
    monkeypatch.setattr(sec_8k, "_cache", fresh_8k)
    # The CIK lookup goes through sec_edgar._get_json which also caches
    # the SEC tickers file. Isolate that too.
    fresh_edgar = FileCache("sec_edgar", root=tmp_path)
    monkeypatch.setattr(sec_edgar, "_cache", fresh_edgar)


@pytest.fixture()
def ua(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEC_USER_AGENT", "Test bot test@example.com")


@pytest.fixture()
def fixed_today(monkeypatch: pytest.MonkeyPatch) -> date:
    """Anchor sec_8k._today to a known calendar date so the ``days`` window
    filter is deterministic.
    """
    fixed = date(2026, 5, 17)
    monkeypatch.setattr(sec_8k, "_today", lambda: fixed)
    return fixed


def _fake_response(json_data: Any, status: int = 200) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.ok = status == 200
    m.json.return_value = json_data
    m.text = "" if status == 200 else "error body snippet"
    return m


# A subset of the SEC tickers map for get_cik.
TICKERS_FIXTURE: Dict[str, Dict[str, Any]] = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corporation"},
}


def _make_submissions(filings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Pivot a list of row-dicts into SEC's column-major submissions shape.

    Each input row::

        {"form": "8-K", "filingDate": "2026-05-15",
         "accessionNumber": "0000320193-25-000123",
         "items": "2.02,9.01", "primaryDocument": "doc.htm"}
    """
    cols = ("form", "filingDate", "accessionNumber", "items", "primaryDocument")
    recent: Dict[str, List[Any]] = {c: [] for c in cols}
    for f in filings:
        for c in cols:
            recent[c].append(f.get(c, ""))
    return {
        "cik": "320193",
        "name": "Apple Inc.",
        "filings": {"recent": recent},
    }


def _dispatch_by_url(
    tickers_resp: MagicMock,
    submissions_resp: Optional[MagicMock] = None,
    submissions_exc: Optional[Exception] = None,
):
    """Build a side_effect callable that returns the right fake response
    based on which URL the patched ``requests.get`` was called with.

    Both ``sec_edgar`` and ``sec_8k`` import the SAME ``requests`` module
    (it's a singleton in sys.modules), so patching ``sec_edgar.requests.get``
    and ``sec_8k.requests.get`` aliases the same callable. A URL-dispatched
    side_effect is the cleanest way to route correctly.
    """
    def _side_effect(url: str, *args: Any, **kwargs: Any) -> MagicMock:
        if "submissions/" in url:
            if submissions_exc is not None:
                raise submissions_exc
            if submissions_resp is None:
                raise AssertionError(
                    "submissions URL hit but no submissions response configured"
                )
            return submissions_resp
        # Anything else (the tickers URL).
        return tickers_resp

    return _side_effect


# --- _parse_items_field (pure helper, no network) ---------------------------

def test_parse_items_field_single_code() -> None:
    assert sec_8k._parse_items_field("5.02") == ["5.02"]


def test_parse_items_field_multiple_codes() -> None:
    assert sec_8k._parse_items_field("2.02,9.01") == ["2.02", "9.01"]


def test_parse_items_field_handles_whitespace_and_empties() -> None:
    # Real-world: SEC occasionally pads with spaces; some 8-Ks file with
    # a stray trailing comma. Both must round-trip cleanly.
    assert sec_8k._parse_items_field(" 2.02 , 9.01 ,") == ["2.02", "9.01"]


def test_parse_items_field_empty_string_returns_empty_list() -> None:
    assert sec_8k._parse_items_field("") == []


def test_parse_items_field_non_string_returns_empty_list() -> None:
    assert sec_8k._parse_items_field(None) == []
    assert sec_8k._parse_items_field(123) == []


# --- get_recent_8k_events ---------------------------------------------------

def test_get_recent_8k_events_parses_recent_filings(
    ua: None, fixed_today: date
) -> None:
    """Happy path: one 8-K with two items lands as a fully-populated event."""
    submissions = _make_submissions([
        {
            "form": "8-K",
            "filingDate": "2026-05-15",  # 2 days before fixed_today
            "accessionNumber": "0000320193-25-000123",
            "items": "2.02,9.01",
            "primaryDocument": "aapl-20260515.htm",
        },
    ])
    submissions_resp = _fake_response(submissions)
    side = _dispatch_by_url(_fake_response(TICKERS_FIXTURE), submissions_resp)
    with patch("lthcs.sources.sec_8k.requests.get", side_effect=side) as get_mock:
        events = sec_8k.get_recent_8k_events("AAPL", days=90)

    assert len(events) == 1
    e = events[0]
    assert e["ticker"] == "AAPL"
    assert e["cik"] == "0000320193"
    assert e["filing_date"] == "2026-05-15"
    assert e["accession_number"] == "0000320193-25-000123"
    assert e["items"] == ["2.02", "9.01"]
    # weight = max(0.8, 0.1)
    assert e["weight"] == pytest.approx(0.8)
    # 2.02 direction=0, 9.01 direction=0 -> sum=0
    assert e["direction"] == 0
    assert "Results of operations" in e["item_labels"][0]
    assert e["primary_document"] == "aapl-20260515.htm"

    # The submissions URL must include the padded CIK.
    urls = [call.args[0] for call in get_mock.call_args_list]
    assert "https://data.sec.gov/submissions/CIK0000320193.json" in urls


def test_get_recent_8k_events_filters_out_non_8k_forms(
    ua: None, fixed_today: date
) -> None:
    """10-Ks, 10-Qs, S-1s and the like must NOT appear in the output."""
    submissions = _make_submissions([
        {"form": "10-K", "filingDate": "2026-05-10",
         "accessionNumber": "X1", "items": "", "primaryDocument": "a.htm"},
        {"form": "10-Q", "filingDate": "2026-05-09",
         "accessionNumber": "X2", "items": "", "primaryDocument": "b.htm"},
        {"form": "S-1", "filingDate": "2026-05-08",
         "accessionNumber": "X3", "items": "", "primaryDocument": "c.htm"},
        {"form": "8-K", "filingDate": "2026-05-15",
         "accessionNumber": "Y1", "items": "5.02", "primaryDocument": "d.htm"},
    ])
    side = _dispatch_by_url(_fake_response(TICKERS_FIXTURE), _fake_response(submissions))
    with patch("lthcs.sources.sec_8k.requests.get", side_effect=side):
        events = sec_8k.get_recent_8k_events("AAPL", days=90)

    assert len(events) == 1
    assert events[0]["accession_number"] == "Y1"
    assert events[0]["items"] == ["5.02"]


def test_get_recent_8k_events_drops_rows_outside_window(
    ua: None, fixed_today: date
) -> None:
    """``days=30`` should exclude an 8-K filed 60 days ago."""
    inside = (fixed_today - timedelta(days=10)).isoformat()
    outside = (fixed_today - timedelta(days=60)).isoformat()
    submissions = _make_submissions([
        {"form": "8-K", "filingDate": outside,
         "accessionNumber": "OLD", "items": "5.02", "primaryDocument": "old.htm"},
        {"form": "8-K", "filingDate": inside,
         "accessionNumber": "NEW", "items": "2.02", "primaryDocument": "new.htm"},
    ])
    side = _dispatch_by_url(_fake_response(TICKERS_FIXTURE), _fake_response(submissions))
    with patch("lthcs.sources.sec_8k.requests.get", side_effect=side):
        events = sec_8k.get_recent_8k_events("AAPL", days=30)

    assert [e["accession_number"] for e in events] == ["NEW"]


def test_get_recent_8k_events_empty_items_field_is_preserved(
    ua: None, fixed_today: date
) -> None:
    """Some 8-Ks file with an empty items field. We still surface the row
    so analysts know the filing happened — weight is 0.0 (no signal)."""
    submissions = _make_submissions([
        {"form": "8-K", "filingDate": "2026-05-15",
         "accessionNumber": "EMPTY1", "items": "", "primaryDocument": "p.htm"},
    ])
    side = _dispatch_by_url(_fake_response(TICKERS_FIXTURE), _fake_response(submissions))
    with patch("lthcs.sources.sec_8k.requests.get", side_effect=side):
        events = sec_8k.get_recent_8k_events("AAPL", days=90)

    assert len(events) == 1
    assert events[0]["items"] == []
    assert events[0]["item_labels"] == []
    assert events[0]["weight"] == 0.0
    assert events[0]["direction"] == 0


def test_get_recent_8k_events_multiple_items_picks_max_weight(
    ua: None, fixed_today: date
) -> None:
    """For an 8-K with items 4.02 (w=0.9, d=-1) + 9.01 (w=0.1, d=0):
       weight = max(0.9, 0.1) = 0.9
       direction = sign(-1 + 0) = -1
    """
    submissions = _make_submissions([
        {"form": "8-K", "filingDate": "2026-05-15",
         "accessionNumber": "RESTATE", "items": "4.02,9.01",
         "primaryDocument": "p.htm"},
    ])
    side = _dispatch_by_url(_fake_response(TICKERS_FIXTURE), _fake_response(submissions))
    with patch("lthcs.sources.sec_8k.requests.get", side_effect=side):
        events = sec_8k.get_recent_8k_events("AAPL", days=90)

    assert events[0]["weight"] == pytest.approx(0.9)
    assert events[0]["direction"] == -1


def test_get_recent_8k_events_newest_first_ordering(
    ua: None, fixed_today: date
) -> None:
    submissions = _make_submissions([
        {"form": "8-K", "filingDate": "2026-04-01",
         "accessionNumber": "A1", "items": "5.02", "primaryDocument": "a.htm"},
        {"form": "8-K", "filingDate": "2026-05-10",
         "accessionNumber": "A2", "items": "2.02", "primaryDocument": "b.htm"},
        {"form": "8-K", "filingDate": "2026-05-01",
         "accessionNumber": "A3", "items": "8.01", "primaryDocument": "c.htm"},
    ])
    side = _dispatch_by_url(_fake_response(TICKERS_FIXTURE), _fake_response(submissions))
    with patch("lthcs.sources.sec_8k.requests.get", side_effect=side):
        events = sec_8k.get_recent_8k_events("AAPL", days=90)

    assert [e["filing_date"] for e in events] == ["2026-05-10", "2026-05-01", "2026-04-01"]


def test_get_recent_8k_events_unknown_ticker_returns_empty(ua: None) -> None:
    """Ticker not in the SEC tickers map => empty list. No submissions
    call should be made (we short-circuit on CIK lookup failure)."""
    side = _dispatch_by_url(_fake_response(TICKERS_FIXTURE), None)
    with patch("lthcs.sources.sec_8k.requests.get", side_effect=side) as get_mock:
        result = sec_8k.get_recent_8k_events("ZZZZ", days=90)

    assert result == []
    # Only the tickers URL was hit -- no submissions request.
    for call in get_mock.call_args_list:
        assert "submissions/" not in call.args[0]


def test_get_recent_8k_events_none_ticker_returns_empty(ua: None) -> None:
    """Defensive: a None ticker must short-circuit before touching HTTP."""
    with patch("lthcs.sources.sec_8k.requests.get") as get_mock:
        assert sec_8k.get_recent_8k_events(None, days=90) == []  # type: ignore[arg-type]
        assert sec_8k.get_recent_8k_events("", days=90) == []
        get_mock.assert_not_called()


def test_get_recent_8k_events_non_200_returns_empty(
    ua: None, fixed_today: date
) -> None:
    """SEC 429 / 500 / 404 must NOT raise — return empty so the daily run
    keeps moving for other tickers."""
    side = _dispatch_by_url(
        _fake_response(TICKERS_FIXTURE), _fake_response(None, status=429)
    )
    with patch("lthcs.sources.sec_8k.requests.get", side_effect=side):
        events = sec_8k.get_recent_8k_events("AAPL", days=90)

    assert events == []


def test_get_recent_8k_events_network_exception_returns_empty(
    ua: None, fixed_today: date
) -> None:
    """A connection error mid-fetch must also be swallowed."""
    side = _dispatch_by_url(
        _fake_response(TICKERS_FIXTURE),
        None,
        submissions_exc=requests.ConnectionError("simulated network drop"),
    )
    with patch("lthcs.sources.sec_8k.requests.get", side_effect=side):
        events = sec_8k.get_recent_8k_events("AAPL", days=90)

    assert events == []


def test_get_recent_8k_events_raises_without_user_agent(
    monkeypatch: pytest.MonkeyPatch, fixed_today: date
) -> None:
    """Config bug (SEC_USER_AGENT unset) must surface loudly — this is the
    one error class we DO raise, because it's not a transient failure."""
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    side = _dispatch_by_url(_fake_response(TICKERS_FIXTURE), None)
    with patch("lthcs.sources.sec_8k.requests.get", side_effect=side) as get_mock:
        # The CIK lookup goes through sec_edgar._get_json which calls
        # _user_agent() before the HTTP call -> raises before submissions
        # is hit. That's the expected ergonomic.
        with pytest.raises(sec_edgar.SECEdgarError, match="SEC_USER_AGENT"):
            sec_8k.get_recent_8k_events("AAPL", days=90)
        # No submissions URL should have been hit.
        for call in get_mock.call_args_list:
            assert "submissions/" not in call.args[0]


def test_get_recent_8k_events_cache_hit_avoids_second_http_call(
    ua: None, fixed_today: date
) -> None:
    submissions = _make_submissions([
        {"form": "8-K", "filingDate": "2026-05-15",
         "accessionNumber": "X1", "items": "2.02", "primaryDocument": "p.htm"},
    ])
    side = _dispatch_by_url(_fake_response(TICKERS_FIXTURE), _fake_response(submissions))
    with patch("lthcs.sources.sec_8k.requests.get", side_effect=side) as get_mock:
        first = sec_8k.get_recent_8k_events("AAPL", days=90)
        second = sec_8k.get_recent_8k_events("AAPL", days=90)
        # First call: 1 tickers fetch + 1 submissions fetch = 2.
        # Second call: both cached, so total stays at 2.
        assert get_mock.call_count == 2

    assert first == second
    assert len(first) == 1


def test_get_recent_8k_events_unparseable_date_dropped(
    ua: None, fixed_today: date
) -> None:
    """A malformed filingDate must not crash the row loop; we just drop it."""
    submissions = _make_submissions([
        {"form": "8-K", "filingDate": "not-a-date",
         "accessionNumber": "BAD", "items": "5.02", "primaryDocument": "p.htm"},
        {"form": "8-K", "filingDate": "2026-05-15",
         "accessionNumber": "GOOD", "items": "5.02", "primaryDocument": "q.htm"},
    ])
    side = _dispatch_by_url(_fake_response(TICKERS_FIXTURE), _fake_response(submissions))
    with patch("lthcs.sources.sec_8k.requests.get", side_effect=side):
        events = sec_8k.get_recent_8k_events("AAPL", days=90)
    assert [e["accession_number"] for e in events] == ["GOOD"]


# --- summarize_events_for_thesis -------------------------------------------

def _event(ticker: str, date_str: str, items: List[str], accession: str = "A") -> Dict[str, Any]:
    """Build an in-memory event dict mirroring the get_recent_8k_events shape.

    Used by the summarize tests so we don't need to mock HTTP just to test
    the aggregator.
    """
    weights = [sec_8k.ITEM_CODE_WEIGHTS.get(c, (0.3, 0, "X"))[0] for c in items]
    directions = [sec_8k.ITEM_CODE_WEIGHTS.get(c, (0.3, 0, "X"))[1] for c in items]
    labels = [sec_8k.ITEM_CODE_WEIGHTS.get(c, (0.3, 0, "X"))[2] for c in items]
    weight = max(weights) if weights else 0.0
    direction = (1 if sum(directions) > 0 else (-1 if sum(directions) < 0 else 0))
    return {
        "ticker": ticker.upper(),
        "cik": "0000320193",
        "filing_date": date_str,
        "accession_number": accession,
        "items": list(items),
        "item_labels": labels,
        "weight": weight,
        "direction": direction,
        "primary_document": "doc.htm",
    }


def test_summarize_events_empty_input_returns_none_score() -> None:
    """An empty event list must report event_count=0 and None for the score,
    matching the contract for the AV / ai_news parallel."""
    summary = sec_8k.summarize_events_for_thesis([])
    assert summary["event_count"] == 0
    assert summary["high_signal_event_count"] == 0
    assert summary["weighted_sentiment_score"] is None
    assert summary["most_recent_event"] is None
    assert summary["most_recent_date"] is None
    assert summary["events"] == []
    # Histogram is fully zeroed.
    assert all(v == 0 for v in summary["label_counts"].values())


def test_summarize_events_single_restatement_is_strongly_negative() -> None:
    """One Item 4.02 (restatement, w=0.9, d=-1) should anchor sentiment near -0.9."""
    events = [_event("AAPL", "2026-05-10", ["4.02"], "RESTATE")]
    summary = sec_8k.summarize_events_for_thesis(events)
    assert summary["event_count"] == 1
    assert summary["high_signal_event_count"] == 1
    assert summary["weighted_sentiment_score"] == pytest.approx(-0.9)
    assert summary["label_counts"]["Bearish"] == 1
    assert summary["most_recent_event"] == "Non-reliance on previously-issued financials"
    assert summary["most_recent_date"] == "2026-05-10"


def test_summarize_events_routine_items_near_neutral() -> None:
    """Item 5.07 (routine vote, w=0.1, d=0) + Item 9.01 (exhibits, w=0.1, d=0)
    should produce a near-neutral score (0.0)."""
    events = [
        _event("AAPL", "2026-05-10", ["5.07"], "VOTE"),
        _event("AAPL", "2026-05-08", ["9.01"], "EXH"),
    ]
    summary = sec_8k.summarize_events_for_thesis(events)
    assert summary["event_count"] == 2
    assert summary["high_signal_event_count"] == 0
    assert summary["weighted_sentiment_score"] == pytest.approx(0.0)
    assert summary["label_counts"]["Neutral"] == 2


def test_summarize_events_earnings_plus_restatement_combined() -> None:
    """The brief's headline scenario:
        Item 2.02 (earnings, w=0.8, d=0)  -> contribution 0.0
        Item 4.02 (restate,  w=0.9, d=-1) -> contribution -0.9
        avg = (0.0 + -0.9) / 2 = -0.45 — "around -0.5".
    """
    events = [
        _event("AAPL", "2026-05-15", ["2.02"], "EARN"),
        _event("AAPL", "2026-05-10", ["4.02"], "RESTATE"),
    ]
    summary = sec_8k.summarize_events_for_thesis(events)
    assert summary["weighted_sentiment_score"] == pytest.approx(-0.45)
    assert summary["high_signal_event_count"] == 2  # both >= 0.6
    # Most recent is the 2026-05-15 earnings filing.
    assert summary["most_recent_date"] == "2026-05-15"
    assert "Results of operations" in summary["most_recent_event"]


def test_summarize_events_zero_weight_events_excluded_from_score() -> None:
    """An empty-items 8-K (weight 0.0) must not affect the sentiment average."""
    events = [
        _event("AAPL", "2026-05-15", ["4.02"], "RESTATE"),
        # Synthetic zero-weight row (no items).
        {
            "ticker": "AAPL", "cik": "0000320193",
            "filing_date": "2026-05-14",
            "accession_number": "EMPTY",
            "items": [], "item_labels": [],
            "weight": 0.0, "direction": 0,
            "primary_document": "p.htm",
        },
    ]
    summary = sec_8k.summarize_events_for_thesis(events)
    # Score is computed only from the one weighted event -> -0.9.
    assert summary["weighted_sentiment_score"] == pytest.approx(-0.9)
    # But event_count counts both filings.
    assert summary["event_count"] == 2


def test_summarize_events_high_signal_threshold() -> None:
    """``high_signal_event_count`` counts events with weight >= 0.6."""
    events = [
        _event("AAPL", "2026-05-15", ["2.02"], "EARN"),    # w=0.8 high
        _event("AAPL", "2026-05-10", ["5.02"], "OFFICER"), # w=0.6 high
        _event("AAPL", "2026-05-08", ["5.07"], "VOTE"),    # w=0.1 low
        _event("AAPL", "2026-05-05", ["9.01"], "EXH"),     # w=0.1 low
    ]
    summary = sec_8k.summarize_events_for_thesis(events)
    assert summary["event_count"] == 4
    assert summary["high_signal_event_count"] == 2


# --- event_signal_for_ticker -----------------------------------------------

def test_event_signal_for_ticker_happy_path(ua: None, fixed_today: date) -> None:
    """End-to-end through the public convenience wrapper."""
    submissions = _make_submissions([
        {"form": "8-K", "filingDate": "2026-05-15",
         "accessionNumber": "EARN", "items": "2.02",
         "primaryDocument": "earn.htm"},
        {"form": "8-K", "filingDate": "2026-05-10",
         "accessionNumber": "RESTATE", "items": "4.02",
         "primaryDocument": "rest.htm"},
    ])
    side = _dispatch_by_url(_fake_response(TICKERS_FIXTURE), _fake_response(submissions))
    with patch("lthcs.sources.sec_8k.requests.get", side_effect=side):
        sig = sec_8k.event_signal_for_ticker("AAPL", days=90)

    # Shape must match the AV/parse_ticker_sentiment contract.
    assert sig["ticker"] == "AAPL"
    assert sig["source"] == "sec_8k"
    assert sig["article_count"] == 2
    assert sig["mean_sentiment_score"] == pytest.approx(-0.45)
    assert sig["mean_relevance_score"] == 0.5
    assert sig["high_signal_event_count"] == 2
    assert sig["most_recent_date"] == "2026-05-15"
    assert len(sig["events"]) == 2
    assert sig["last_scored"] == "2026-05-17"


def test_event_signal_for_ticker_unknown_ticker_returns_empty(ua: None) -> None:
    """Unknown ticker => zero events, score None, relevance None."""
    side = _dispatch_by_url(_fake_response(TICKERS_FIXTURE), None)
    with patch("lthcs.sources.sec_8k.requests.get", side_effect=side) as get_mock:
        sig = sec_8k.event_signal_for_ticker("ZZZZ", days=90)

    assert sig["ticker"] == "ZZZZ"
    assert sig["article_count"] == 0
    assert sig["mean_sentiment_score"] is None
    assert sig["mean_relevance_score"] is None
    assert sig["events"] == []
    # Only the tickers URL should have been hit -- never submissions.
    for call in get_mock.call_args_list:
        assert "submissions/" not in call.args[0]


def test_event_signal_for_ticker_uses_today_kwarg(
    ua: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passing ``today=`` should bypass _today() so the function is fully
    deterministic in test."""
    # Deliberately don't fix sec_8k._today -- the kwarg must dominate.
    submissions = _make_submissions([
        {"form": "8-K", "filingDate": "2026-05-15",
         "accessionNumber": "E1", "items": "2.02", "primaryDocument": "p.htm"},
    ])
    side = _dispatch_by_url(_fake_response(TICKERS_FIXTURE), _fake_response(submissions))
    with patch("lthcs.sources.sec_8k.requests.get", side_effect=side):
        sig = sec_8k.event_signal_for_ticker(
            "AAPL", days=30, today=date(2026, 5, 17)
        )
    assert sig["last_scored"] == "2026-05-17"
    assert sig["article_count"] == 1


# --- Module surface sanity --------------------------------------------------

def test_item_code_weights_map_is_well_formed() -> None:
    """Every entry must be (weight in [0,1], direction in {-1,0,+1}, str label)."""
    for code, tup in sec_8k.ITEM_CODE_WEIGHTS.items():
        assert isinstance(code, str) and "." in code
        assert isinstance(tup, tuple) and len(tup) == 3
        w, d, lbl = tup
        assert 0.0 <= w <= 1.0
        assert d in (-1, 0, 1)
        assert isinstance(lbl, str) and lbl


# --- as_of historical filtering --------------------------------------------


def test_as_of_none_preserves_existing_behavior(ua: None, fixed_today: date) -> None:
    """``as_of=None`` must produce the same output as omitting the kwarg."""
    submissions = _make_submissions([
        {"form": "8-K", "filingDate": "2026-05-15",
         "accessionNumber": "E1", "items": "2.02", "primaryDocument": "p.htm"},
    ])
    side = _dispatch_by_url(_fake_response(TICKERS_FIXTURE), _fake_response(submissions))
    with patch("lthcs.sources.sec_8k.requests.get", side_effect=side):
        a = sec_8k.get_recent_8k_events("AAPL", days=90)
        b = sec_8k.get_recent_8k_events("AAPL", days=90, as_of=None)
    assert a == b
    assert len(a) == 1


def test_as_of_historical_returns_window_slice(ua: None) -> None:
    """``as_of`` pins the right edge: only filings in [as_of-90d, as_of]."""
    # Filings spread across a year.
    submissions = _make_submissions([
        {"form": "8-K", "filingDate": "2026-05-15",
         "accessionNumber": "FUT", "items": "2.02", "primaryDocument": "p1.htm"},
        {"form": "8-K", "filingDate": "2026-03-15",
         "accessionNumber": "MID", "items": "5.02", "primaryDocument": "p2.htm"},
        {"form": "8-K", "filingDate": "2025-12-15",
         "accessionNumber": "OLD", "items": "4.02", "primaryDocument": "p3.htm"},
    ])
    side = _dispatch_by_url(_fake_response(TICKERS_FIXTURE), _fake_response(submissions))
    with patch("lthcs.sources.sec_8k.requests.get", side_effect=side):
        events = sec_8k.get_recent_8k_events(
            "AAPL", days=90, as_of=date(2026, 4, 15)
        )
    # 2026-04-15 minus 90d = 2026-01-15. Only MID (2026-03-15) fits.
    # FUT (2026-05-15) is AFTER as_of -> dropped.
    # OLD (2025-12-15) is BEFORE the cutoff -> dropped.
    assert [e["accession_number"] for e in events] == ["MID"]


def test_as_of_before_any_filings_returns_empty(ua: None) -> None:
    """``as_of`` before every filing -> empty list."""
    submissions = _make_submissions([
        {"form": "8-K", "filingDate": "2026-05-15",
         "accessionNumber": "E1", "items": "2.02", "primaryDocument": "p.htm"},
    ])
    side = _dispatch_by_url(_fake_response(TICKERS_FIXTURE), _fake_response(submissions))
    with patch("lthcs.sources.sec_8k.requests.get", side_effect=side):
        events = sec_8k.get_recent_8k_events(
            "AAPL", days=90, as_of=date(2024, 1, 1)
        )
    assert events == []


def test_as_of_right_at_filing_date_includes_that_filing(ua: None) -> None:
    """``as_of`` equal to a filing's date INCLUDES it (boundary is ≤, not <)."""
    submissions = _make_submissions([
        {"form": "8-K", "filingDate": "2026-04-15",
         "accessionNumber": "EXACT", "items": "2.02", "primaryDocument": "p.htm"},
    ])
    side = _dispatch_by_url(_fake_response(TICKERS_FIXTURE), _fake_response(submissions))
    with patch("lthcs.sources.sec_8k.requests.get", side_effect=side):
        events = sec_8k.get_recent_8k_events(
            "AAPL", days=90, as_of=date(2026, 4, 15)
        )
    assert [e["accession_number"] for e in events] == ["EXACT"]


def test_as_of_excludes_filings_after(ua: None, fixed_today: date) -> None:
    """An 8-K filed AFTER the as_of date must NOT appear in historical output."""
    submissions = _make_submissions([
        {"form": "8-K", "filingDate": "2026-05-15",
         "accessionNumber": "FUTURE", "items": "2.02", "primaryDocument": "p.htm"},
        {"form": "8-K", "filingDate": "2026-04-10",
         "accessionNumber": "PAST", "items": "5.02", "primaryDocument": "q.htm"},
    ])
    side = _dispatch_by_url(_fake_response(TICKERS_FIXTURE), _fake_response(submissions))
    with patch("lthcs.sources.sec_8k.requests.get", side_effect=side):
        events = sec_8k.get_recent_8k_events(
            "AAPL", days=90, as_of=date(2026, 4, 15)
        )
    assert [e["accession_number"] for e in events] == ["PAST"]


def test_cache_key_independent_of_as_of(ua: None) -> None:
    """The submissions cache key is per-CIK only; multiple ``as_of`` values
    share the same HTTP fetch — no cache poisoning, no extra HTTP."""
    submissions = _make_submissions([
        {"form": "8-K", "filingDate": "2026-05-15",
         "accessionNumber": "A", "items": "2.02", "primaryDocument": "p.htm"},
        {"form": "8-K", "filingDate": "2026-02-15",
         "accessionNumber": "B", "items": "5.02", "primaryDocument": "q.htm"},
    ])
    side = _dispatch_by_url(_fake_response(TICKERS_FIXTURE), _fake_response(submissions))
    with patch("lthcs.sources.sec_8k.requests.get", side_effect=side) as get_mock:
        sec_8k.get_recent_8k_events("AAPL", days=90, as_of=date(2026, 5, 17))
        sec_8k.get_recent_8k_events("AAPL", days=90, as_of=date(2026, 4, 15))
        sec_8k.get_recent_8k_events("AAPL", days=90)
        # 1 tickers + 1 submissions = 2 total HTTP calls, all later cached.
        assert get_mock.call_count == 2


def test_event_signal_for_ticker_uses_as_of_as_last_scored(ua: None) -> None:
    """When ``as_of`` is provided, ``last_scored`` reflects it (not 'today')."""
    submissions = _make_submissions([
        {"form": "8-K", "filingDate": "2026-04-10",
         "accessionNumber": "A1", "items": "2.02", "primaryDocument": "p.htm"},
    ])
    side = _dispatch_by_url(_fake_response(TICKERS_FIXTURE), _fake_response(submissions))
    with patch("lthcs.sources.sec_8k.requests.get", side_effect=side):
        sig = sec_8k.event_signal_for_ticker(
            "AAPL", days=90, as_of=date(2026, 4, 15)
        )
    assert sig["last_scored"] == "2026-04-15"
    assert sig["article_count"] == 1
