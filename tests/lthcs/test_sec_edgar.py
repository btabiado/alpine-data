"""Tests for lthcs.sources.sec_edgar.

All HTTP is mocked -- no live network calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from lthcs.sources._cache import FileCache
from lthcs.sources import sec_edgar


# --- Fixtures ----------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point sec_edgar at a per-test cache directory.

    We rebind the module-level singleton so every test starts with a
    fresh FileCache and never touches the real ``.cache/lthcs`` dir.
    """
    fresh = FileCache("sec_edgar", root=tmp_path)
    monkeypatch.setattr(sec_edgar, "_cache", fresh)


@pytest.fixture()
def ua(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEC_USER_AGENT", "Test bot test@example.com")


def _fake_response(json_data: Any, status: int = 200) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.ok = status == 200
    m.json.return_value = json_data
    m.text = "" if status == 200 else "error body snippet"
    return m


# A realistic-enough subset of the SEC tickers map.
TICKERS_FIXTURE: Dict[str, Dict[str, Any]] = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corporation"},
    "2": {"cik_str": 1318605, "ticker": "TSLA", "title": "Tesla, Inc."},
}


def _facts_with(units_concept: str, units: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "cik": 320193,
        "entityName": "Apple Inc.",
        "facts": {
            "us-gaap": {
                units_concept: {
                    "label": "Revenues",
                    "description": "Revenue",
                    "units": {"USD": units},
                }
            }
        },
    }


# --- get_cik -----------------------------------------------------------------

def test_get_cik_returns_padded_cik(ua: None) -> None:
    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.return_value = _fake_response(TICKERS_FIXTURE)
        assert sec_edgar.get_cik("AAPL") == "0000320193"


def test_get_cik_is_case_insensitive(ua: None) -> None:
    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.return_value = _fake_response(TICKERS_FIXTURE)
        assert sec_edgar.get_cik("aapl") == "0000320193"


def test_get_cik_unknown_ticker_returns_none(ua: None) -> None:
    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.return_value = _fake_response(TICKERS_FIXTURE)
        assert sec_edgar.get_cik("ZZZZ") is None


def test_get_cik_empty_ticker_returns_none(ua: None) -> None:
    # Should short-circuit without even hitting the network.
    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        assert sec_edgar.get_cik("") is None
        mock_get.assert_not_called()


# --- SEC_USER_AGENT enforcement ---------------------------------------------

def test_company_facts_raises_without_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        with pytest.raises(sec_edgar.SECEdgarError, match="SEC_USER_AGENT"):
            sec_edgar.get_company_facts("AAPL")
        # Must fail before any HTTP call.
        mock_get.assert_not_called()


def test_blank_user_agent_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEC_USER_AGENT", "   ")
    with pytest.raises(sec_edgar.SECEdgarError, match="SEC_USER_AGENT"):
        sec_edgar.get_company_facts("AAPL")


# --- get_company_facts -------------------------------------------------------

def test_get_company_facts_returns_parsed_json(ua: None) -> None:
    facts = _facts_with("Revenues", [
        {"end": "2024-12-31", "val": 100, "form": "10-K", "fy": 2024, "fp": "FY"},
    ])

    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.side_effect = [
            _fake_response(TICKERS_FIXTURE),
            _fake_response(facts),
        ]
        result = sec_edgar.get_company_facts("AAPL")

    assert result == facts
    # Verify the second call hit the right URL with the right headers.
    assert mock_get.call_count == 2
    facts_call = mock_get.call_args_list[1]
    assert facts_call.args[0] == (
        "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"
    )
    headers = facts_call.kwargs["headers"]
    assert headers["User-Agent"] == "Test bot test@example.com"
    assert headers["Accept"] == "application/json"


def test_get_company_facts_unknown_ticker_raises(ua: None) -> None:
    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.return_value = _fake_response(TICKERS_FIXTURE)
        with pytest.raises(sec_edgar.SECEdgarError, match="resolve ticker"):
            sec_edgar.get_company_facts("ZZZZ")


def test_get_company_facts_non_200_raises(ua: None) -> None:
    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.side_effect = [
            _fake_response(TICKERS_FIXTURE),
            _fake_response(None, status=429),
        ]
        with pytest.raises(sec_edgar.SECEdgarError) as excinfo:
            sec_edgar.get_company_facts("AAPL")
        msg = str(excinfo.value)
        assert "429" in msg
        assert "error body snippet" in msg


# --- Cache behavior ----------------------------------------------------------

def test_cache_hit_avoids_second_http_call(ua: None) -> None:
    facts = _facts_with("Revenues", [
        {"end": "2024-12-31", "val": 100, "form": "10-K", "fy": 2024, "fp": "FY"},
    ])

    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.side_effect = [
            _fake_response(TICKERS_FIXTURE),
            _fake_response(facts),
        ]
        first = sec_edgar.get_company_facts("AAPL")
        # Second call -- both URLs should be served from cache.
        second = sec_edgar.get_company_facts("AAPL")

    assert first == second == facts
    assert mock_get.call_count == 2  # one for tickers, one for facts; no extras


def test_cache_miss_refetches(ua: None) -> None:
    facts_v1 = _facts_with("Revenues", [
        {"end": "2024-12-31", "val": 100, "form": "10-K", "fy": 2024, "fp": "FY"},
    ])
    facts_v2 = _facts_with("Revenues", [
        {"end": "2025-03-31", "val": 200, "form": "10-Q", "fy": 2025, "fp": "Q1"},
    ])

    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.side_effect = [
            _fake_response(TICKERS_FIXTURE),
            _fake_response(facts_v1),
        ]
        assert sec_edgar.get_company_facts("AAPL") == facts_v1

    # Wipe the cache; the next call should re-fetch.
    sec_edgar._cache.clear()

    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.side_effect = [
            _fake_response(TICKERS_FIXTURE),
            _fake_response(facts_v2),
        ]
        assert sec_edgar.get_company_facts("AAPL") == facts_v2


# --- get_revenue_history -----------------------------------------------------

def test_revenue_history_extracts_and_sorts(ua: None) -> None:
    facts = _facts_with("Revenues", [
        {"end": "2022-12-31", "val": 80, "form": "10-K", "fy": 2022, "fp": "FY"},
        {"end": "2024-12-31", "val": 120, "form": "10-K", "fy": 2024, "fp": "FY"},
        {"end": "2023-12-31", "val": 100, "form": "10-K", "fy": 2023, "fp": "FY"},
    ])

    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.side_effect = [
            _fake_response(TICKERS_FIXTURE),
            _fake_response(facts),
        ]
        rows = sec_edgar.get_revenue_history("AAPL")

    assert [r["end_date"] for r in rows] == ["2024-12-31", "2023-12-31", "2022-12-31"]
    assert rows[0] == {
        "end_date": "2024-12-31",
        "value": 120,
        "form": "10-K",
        "fy": 2024,
        "fp": "FY",
    }


def test_revenue_history_falls_back_to_asc606_concept(ua: None) -> None:
    facts = _facts_with("RevenueFromContractWithCustomerExcludingAssessedTax", [
        {"end": "2024-12-31", "val": 999, "form": "10-K", "fy": 2024, "fp": "FY"},
    ])

    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.side_effect = [
            _fake_response(TICKERS_FIXTURE),
            _fake_response(facts),
        ]
        rows = sec_edgar.get_revenue_history("AAPL")

    assert len(rows) == 1
    assert rows[0]["value"] == 999


def test_revenue_history_prefers_revenues_over_fallback(ua: None) -> None:
    facts = {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {"USD": [
                        {"end": "2024-12-31", "val": 111, "form": "10-K",
                         "fy": 2024, "fp": "FY"},
                    ]},
                },
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {"USD": [
                        {"end": "2024-12-31", "val": 222, "form": "10-K",
                         "fy": 2024, "fp": "FY"},
                    ]},
                },
            }
        }
    }

    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.side_effect = [
            _fake_response(TICKERS_FIXTURE),
            _fake_response(facts),
        ]
        rows = sec_edgar.get_revenue_history("AAPL")

    assert rows == [{
        "end_date": "2024-12-31",
        "value": 111,
        "form": "10-K",
        "fy": 2024,
        "fp": "FY",
    }]


def test_revenue_history_empty_facts_returns_empty_list(ua: None) -> None:
    facts = {"cik": 320193, "entityName": "Apple Inc.", "facts": {"us-gaap": {}}}

    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.side_effect = [
            _fake_response(TICKERS_FIXTURE),
            _fake_response(facts),
        ]
        rows = sec_edgar.get_revenue_history("AAPL")

    assert rows == []


def test_revenue_history_missing_facts_key_returns_empty_list(ua: None) -> None:
    # Some responses might be malformed / partial; we must not blow up.
    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.side_effect = [
            _fake_response(TICKERS_FIXTURE),
            _fake_response({}),  # no "facts" key at all
        ]
        assert sec_edgar.get_revenue_history("AAPL") == []
