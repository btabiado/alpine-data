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
        {"start": "2022-01-01", "end": "2022-12-31", "val": 80,
         "form": "10-K", "fy": 2022, "fp": "FY"},
        {"start": "2024-01-01", "end": "2024-12-31", "val": 120,
         "form": "10-K", "fy": 2024, "fp": "FY"},
        {"start": "2023-01-01", "end": "2023-12-31", "val": 100,
         "form": "10-K", "fy": 2023, "fp": "FY"},
    ])

    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.side_effect = [
            _fake_response(TICKERS_FIXTURE),
            _fake_response(facts),
        ]
        rows = sec_edgar.get_revenue_history("AAPL")

    assert [r["end_date"] for r in rows] == ["2024-12-31", "2023-12-31", "2022-12-31"]
    assert rows[0] == {
        "start_date": "2024-01-01",
        "end_date": "2024-12-31",
        "value": 120,
        "form": "10-K",
        "fy": 2024,
        "fp": "FY",
        "concept": "Revenues",
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


def test_revenue_history_merges_both_concepts_preferring_modern(ua: None) -> None:
    """When both concepts report the same period, the modern (ASC 606) value wins.

    This matters because AAPL et al. report under ``Revenues`` historically but
    only ``RevenueFromContractWithCustomerExcludingAssessedTax`` is updated
    after 2018. We merge both and prefer the modern value when periods collide.
    """
    facts = {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {"USD": [
                        {"start": "2024-01-01", "end": "2024-12-31",
                         "val": 111, "form": "10-K", "fy": 2024, "fp": "FY"},
                        # Legacy-only earlier year, no overlap.
                        {"start": "2018-01-01", "end": "2018-12-31",
                         "val": 50, "form": "10-K", "fy": 2018, "fp": "FY"},
                    ]},
                },
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {"USD": [
                        # Modern value for the SAME period wins on collision.
                        {"start": "2024-01-01", "end": "2024-12-31",
                         "val": 222, "form": "10-K", "fy": 2024, "fp": "FY"},
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

    # Modern concept wins on the colliding 2024 period.
    assert rows[0]["end_date"] == "2024-12-31"
    assert rows[0]["value"] == 222
    assert rows[0]["concept"] == "RevenueFromContractWithCustomerExcludingAssessedTax"
    # Legacy-only period stays.
    assert rows[1]["end_date"] == "2018-12-31"
    assert rows[1]["value"] == 50
    assert rows[1]["concept"] == "Revenues"


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


# --- IFRS-full namespace fallback (foreign private issuers / 20-F filers) ----
#
# Phase 3 post-audit hotfix (May 2026): AZN, GFS, and other 20-F filers
# report under the ``ifrs-full`` namespace, not ``us-gaap``. The audit
# flagged AZN as a "+2.99 stdev" outlier on thesis_integrity, but the
# real bug was the misleading ``sec_unavailable`` flag co-occurring on
# a row whose Finnhub-derived thesis score was actually legitimate. With
# IFRS-full support, AZN's Financial pillar gets real data and the
# pipeline no longer raises ``sec_unavailable`` for it.

def _facts_ifrs(concept_to_series: Dict[str, List[Dict[str, Any]]],
                units_key: str = "USD") -> Dict[str, Any]:
    """Build a facts payload under the ``ifrs-full`` namespace.

    ``units_key`` controls the currency. Defaults to USD because that's
    what AZN, GFS, and most large foreign private issuers file in. Set
    to "EUR" to simulate a non-USD filer (e.g. CCEP) and verify the
    strict-USD guard.
    """
    ifrs: Dict[str, Any] = {}
    for concept, series in concept_to_series.items():
        ifrs[concept] = {"label": concept, "units": {units_key: series}}
    return {
        "cik": 901832,
        "entityName": "AstraZeneca PLC",
        "facts": {"ifrs-full": ifrs},
    }


def test_revenue_history_falls_back_to_ifrs_full_for_foreign_issuer(
    ua: None,
) -> None:
    """20-F filers (AZN etc.) report Revenue under ``ifrs-full``, not us-gaap.

    The fallback must pick up ``Revenue`` and
    ``RevenueFromContractsWithCustomers`` so the Financial Evolution pillar
    gets real signal instead of collapsing to neutral 50 + ``sec_unavailable``.
    """
    facts = _facts_ifrs({
        "Revenue": [
            {"start": "2024-01-01", "end": "2024-12-31",
             "val": 54_073_000_000, "form": "20-F", "fy": 2024, "fp": "FY"},
            {"start": "2023-01-01", "end": "2023-12-31",
             "val": 45_811_000_000, "form": "20-F", "fy": 2023, "fp": "FY"},
        ],
    })

    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.side_effect = [
            _fake_response({"0": {"cik_str": 901832, "ticker": "AZN",
                                  "title": "AstraZeneca PLC"}}),
            _fake_response(facts),
        ]
        rows = sec_edgar.get_revenue_history("AZN")

    assert len(rows) == 2
    assert rows[0]["end_date"] == "2024-12-31"
    assert rows[0]["value"] == 54_073_000_000
    assert rows[0]["concept"] == "Revenue"
    assert rows[0]["form"] == "20-F"


def test_gross_profit_history_falls_back_to_ifrs_full(ua: None) -> None:
    """``GrossProfit`` exists in BOTH namespaces — IFRS-full path picks it
    up when ``us-gaap`` is empty (foreign issuers)."""
    facts = _facts_ifrs({
        "GrossProfit": [
            {"start": "2024-01-01", "end": "2024-12-31",
             "val": 44_270_000_000, "form": "20-F", "fy": 2024, "fp": "FY"},
        ],
    })
    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.side_effect = [
            _fake_response({"0": {"cik_str": 901832, "ticker": "AZN",
                                  "title": "AstraZeneca PLC"}}),
            _fake_response(facts),
        ]
        rows = sec_edgar.get_gross_profit_history("AZN")

    assert len(rows) == 1
    assert rows[0]["value"] == 44_270_000_000
    assert rows[0]["concept"] == "GrossProfit"


def test_operating_cash_flow_history_falls_back_to_ifrs_full(ua: None) -> None:
    """OCF under IFRS uses different concept names than us-gaap."""
    facts = _facts_ifrs({
        "CashFlowsFromUsedInOperatingActivities": [
            {"start": "2024-01-01", "end": "2024-12-31",
             "val": 11_858_000_000, "form": "20-F", "fy": 2024, "fp": "FY"},
        ],
    })
    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.side_effect = [
            _fake_response({"0": {"cik_str": 901832, "ticker": "AZN",
                                  "title": "AstraZeneca PLC"}}),
            _fake_response(facts),
        ]
        rows = sec_edgar.get_operating_cash_flow_history("AZN")

    assert len(rows) == 1
    assert rows[0]["value"] == 11_858_000_000
    assert rows[0]["concept"] == "CashFlowsFromUsedInOperatingActivities"


def test_ifrs_fallback_only_fires_when_us_gaap_empty(ua: None) -> None:
    """Domestic 10-K filers must NEVER hit the IFRS branch.

    Guard against a regression where us-gaap data is correctly present
    but the IFRS-full namespace happens to also exist with different
    values — the us-gaap series must win in full.
    """
    facts = {
        "facts": {
            "us-gaap": {
                "Revenues": {"units": {"USD": [
                    {"start": "2024-01-01", "end": "2024-12-31",
                     "val": 100_000_000_000, "form": "10-K",
                     "fy": 2024, "fp": "FY"},
                ]}},
            },
            "ifrs-full": {
                "Revenue": {"units": {"USD": [
                    # Bogus IFRS value — must NOT be returned because
                    # the us-gaap walk already succeeded.
                    {"start": "2024-01-01", "end": "2024-12-31",
                     "val": 999_999, "form": "20-F",
                     "fy": 2024, "fp": "FY"},
                ]}},
            },
        }
    }

    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.side_effect = [
            _fake_response(TICKERS_FIXTURE),
            _fake_response(facts),
        ]
        rows = sec_edgar.get_revenue_history("AAPL")

    assert len(rows) == 1
    assert rows[0]["value"] == 100_000_000_000
    assert rows[0]["concept"] == "Revenues"
    assert rows[0]["form"] == "10-K"


def test_azn_data_quality_stub_regression_no_sec_unavailable(
    ua: None,
) -> None:
    """Regression for Phase 3 post-audit hotfix #2 — the AZN ``+2.99 stdev``
    outlier that was actually a misleading ``sec_unavailable`` flag.

    The audit flagged AZN (z=+2.99 on thesis_integrity) as a suspected
    unscored stub because the row carried ``sec_unavailable``. Root
    cause: AZN files 20-F under the ``ifrs-full`` namespace and our
    extractor only walked ``us-gaap``, so revenue came back empty and
    ``stage_3_quality_checks`` raised the flag. The IFRS fallback path
    fixes the underlying data flow — and so the ``has_sec`` predicate
    that drives the ``sec_unavailable`` flag in ``lthcs_daily.py`` flips
    to True for AZN-shaped 20-F filers.

    This test pins that contract: a 20-F-style payload yields non-empty
    revenue, which means ``has_sec = bool(state.rev_by_ticker.get(sym))``
    in the daily pipeline evaluates True and the
    ``flags.append("sec_unavailable")`` branch never fires for AZN.
    """
    # Synthetic 20-F-flavored facts modeled on AZN's real SEC payload
    # shape (ifrs-full, USD units, "Revenue" + "GrossProfit" +
    # "CashFlowsFromUsedInOperatingActivities"). Numbers are illustrative.
    facts = {
        "cik": 901832,
        "entityName": "AstraZeneca PLC",
        "facts": {
            "ifrs-full": {
                "Revenue": {"units": {"USD": [
                    {"start": "2024-01-01", "end": "2024-12-31",
                     "val": 54_073_000_000, "form": "20-F",
                     "fy": 2024, "fp": "FY"},
                    {"start": "2023-01-01", "end": "2023-12-31",
                     "val": 45_811_000_000, "form": "20-F",
                     "fy": 2023, "fp": "FY"},
                ]}},
                "GrossProfit": {"units": {"USD": [
                    {"start": "2024-01-01", "end": "2024-12-31",
                     "val": 44_270_000_000, "form": "20-F",
                     "fy": 2024, "fp": "FY"},
                ]}},
                "CashFlowsFromUsedInOperatingActivities": {"units": {"USD": [
                    {"start": "2024-01-01", "end": "2024-12-31",
                     "val": 11_858_000_000, "form": "20-F",
                     "fy": 2024, "fp": "FY"},
                ]}},
            }
        },
    }

    azn_tickers = {"0": {"cik_str": 901832, "ticker": "AZN",
                         "title": "AstraZeneca PLC"}}

    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        # Three back-to-back calls in this scenario:
        # 1) tickers list lookup, 2-4) the same companyfacts payload is
        # cached after the first companyfacts fetch — but
        # ``get_revenue_history`` and friends each go through the cache
        # via the same key, so subsequent fetches hit the in-process
        # FileCache and don't re-mock. To be safe we hand back the same
        # payload up to 4 times.
        mock_get.side_effect = [
            _fake_response(azn_tickers),
            _fake_response(facts),
            _fake_response(facts),
            _fake_response(facts),
        ]

        revenue = sec_edgar.get_revenue_history("AZN")
        gross = sec_edgar.get_gross_profit_history("AZN")
        ocf = sec_edgar.get_operating_cash_flow_history("AZN")

    # The whole point of the fix: AZN's three Financial-pillar series
    # are no longer empty. ``has_sec = bool(rev_by_ticker.get('AZN'))``
    # in lthcs_daily.stage_3_quality_checks therefore evaluates True,
    # and the ``sec_unavailable`` flag is NOT appended.
    assert revenue, "AZN revenue must not be empty after IFRS fallback"
    assert gross, "AZN gross profit must not be empty after IFRS fallback"
    assert ocf, "AZN OCF must not be empty after IFRS fallback"

    # Simulate the exact has_sec predicate from stage_3_quality_checks.
    rev_by_ticker = {"AZN": revenue}
    has_sec = bool(rev_by_ticker.get("AZN"))
    flags: list = []
    if not has_sec:
        flags.append("sec_unavailable")
    assert "sec_unavailable" not in flags, (
        "AZN must not carry sec_unavailable once IFRS fallback returns "
        "real revenue — this is the regression the Phase 3 audit "
        "flagged as a +2.99-stdev outlier."
    )


def test_ifrs_fallback_is_strict_usd_only(ua: None) -> None:
    """IFRS-full filers in non-USD (e.g. CCEP in EUR) must NOT be promoted.

    Cross-currency growth/margin comparisons would corrupt cohort
    z-scores, so EUR/GBP/etc. IFRS series stay invisible to the
    fallback and the daily pipeline keeps flagging ``sec_unavailable``
    — the conservative right answer.
    """
    facts = _facts_ifrs({
        "Revenue": [
            {"start": "2024-01-01", "end": "2024-12-31",
             "val": 20_000_000_000, "form": "20-F", "fy": 2024, "fp": "FY"},
        ],
    }, units_key="EUR")

    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.side_effect = [
            _fake_response({"0": {"cik_str": 1650107, "ticker": "CCEP",
                                  "title": "Coca-Cola Europacific Partners"}}),
            _fake_response(facts),
        ]
        rows = sec_edgar.get_revenue_history("CCEP")

    # Strict-USD guard means no rows surface from EUR-only IFRS filers.
    assert rows == []


# --- Bank concepts: get_net_interest_income_history --------------------------


def _facts_with_concepts(concept_to_series: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Build a facts payload with multiple us-gaap concepts at once."""
    gaap: Dict[str, Any] = {}
    for concept, series in concept_to_series.items():
        gaap[concept] = {"label": concept, "units": {"USD": series}}
    return {
        "cik": 19617,
        "entityName": "JPMorgan Chase & Co.",
        "facts": {"us-gaap": gaap},
    }


def test_net_interest_income_history_extracts_primary_concept(ua: None) -> None:
    """Banks reporting under ``InterestIncomeOperating`` should produce a series."""
    facts = _facts_with_concepts({
        "InterestIncomeOperating": [
            {"start": "2024-01-01", "end": "2024-03-31", "val": 25_000,
             "form": "10-Q", "fy": 2024, "fp": "Q1"},
            {"start": "2024-04-01", "end": "2024-06-30", "val": 26_000,
             "form": "10-Q", "fy": 2024, "fp": "Q2"},
        ],
    })
    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.side_effect = [
            _fake_response(TICKERS_FIXTURE),
            _fake_response(facts),
        ]
        rows = sec_edgar.get_net_interest_income_history("AAPL")
    assert len(rows) == 2
    # Desc by end_date.
    assert rows[0]["end_date"] == "2024-06-30"
    assert rows[0]["value"] == 26_000
    assert rows[0]["concept"] == "InterestIncomeOperating"


def test_net_interest_income_history_merges_legacy_and_modern(ua: None) -> None:
    """The newer (later-in-tuple) concept wins on a period collision."""
    facts = _facts_with_concepts({
        # Legacy / dividend-form -- earlier in the tuple.
        "InterestAndDividendIncomeOperating": [
            {"start": "2024-01-01", "end": "2024-03-31", "val": 25_000,
             "form": "10-Q", "fy": 2024, "fp": "Q1"},
            # Older year, no overlap.
            {"start": "2018-01-01", "end": "2018-03-31", "val": 18_000,
             "form": "10-Q", "fy": 2018, "fp": "Q1"},
        ],
        # ``NetInterestIncome`` is later in the tuple -- it wins on collision.
        "NetInterestIncome": [
            {"start": "2024-01-01", "end": "2024-03-31", "val": 99_999,
             "form": "10-Q", "fy": 2024, "fp": "Q1"},
        ],
    })
    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.side_effect = [
            _fake_response(TICKERS_FIXTURE),
            _fake_response(facts),
        ]
        rows = sec_edgar.get_net_interest_income_history("AAPL")
    by_end = {r["end_date"]: r for r in rows}
    assert by_end["2024-03-31"]["value"] == 99_999
    assert by_end["2024-03-31"]["concept"] == "NetInterestIncome"
    # Legacy-only earlier year still present.
    assert by_end["2018-03-31"]["value"] == 18_000


def test_net_interest_income_history_empty_when_concepts_missing(ua: None) -> None:
    """A non-bank company won't have any of these concepts -> empty series."""
    facts = _facts_with_concepts({
        # Only revenue, no bank concepts.
        "Revenues": [
            {"start": "2024-01-01", "end": "2024-03-31", "val": 1_000,
             "form": "10-Q", "fy": 2024, "fp": "Q1"},
        ],
    })
    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.side_effect = [
            _fake_response(TICKERS_FIXTURE),
            _fake_response(facts),
        ]
        rows = sec_edgar.get_net_interest_income_history("AAPL")
    assert rows == []


# --- Bank concepts: get_provision_for_credit_losses_history ------------------


def test_pcl_history_extracts_pre_cecl_concept(ua: None) -> None:
    facts = _facts_with_concepts({
        "ProvisionForLoanLeaseAndOtherLosses": [
            {"start": "2024-01-01", "end": "2024-03-31", "val": 3_000,
             "form": "10-Q", "fy": 2024, "fp": "Q1"},
            {"start": "2024-04-01", "end": "2024-06-30", "val": 3_200,
             "form": "10-Q", "fy": 2024, "fp": "Q2"},
        ],
    })
    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.side_effect = [
            _fake_response(TICKERS_FIXTURE),
            _fake_response(facts),
        ]
        rows = sec_edgar.get_provision_for_credit_losses_history("AAPL")
    assert len(rows) == 2
    assert rows[0]["end_date"] == "2024-06-30"
    assert rows[0]["value"] == 3_200
    assert rows[0]["concept"] == "ProvisionForLoanLeaseAndOtherLosses"


def test_pcl_history_modern_cecl_concept_wins_on_collision(ua: None) -> None:
    """When JPM/BAC report under both legacy AND CECL-era concepts for the same
    period, the post-2020 ``ProvisionForCreditLosses`` value wins."""
    facts = _facts_with_concepts({
        "ProvisionForLoanLeaseAndOtherLosses": [
            {"start": "2024-01-01", "end": "2024-03-31", "val": 1_000,
             "form": "10-Q", "fy": 2024, "fp": "Q1"},
        ],
        # Later in tuple -> wins on the same-period collision.
        "ProvisionForCreditLosses": [
            {"start": "2024-01-01", "end": "2024-03-31", "val": 9_999,
             "form": "10-Q", "fy": 2024, "fp": "Q1"},
        ],
    })
    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.side_effect = [
            _fake_response(TICKERS_FIXTURE),
            _fake_response(facts),
        ]
        rows = sec_edgar.get_provision_for_credit_losses_history("AAPL")
    assert len(rows) == 1
    assert rows[0]["value"] == 9_999
    assert rows[0]["concept"] == "ProvisionForCreditLosses"


def test_pcl_history_empty_when_concepts_missing(ua: None) -> None:
    facts = _facts_with_concepts({})  # no bank concepts at all
    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.side_effect = [
            _fake_response(TICKERS_FIXTURE),
            _fake_response(facts),
        ]
        rows = sec_edgar.get_provision_for_credit_losses_history("AAPL")
    assert rows == []


# --- Bank concepts: get_noninterest_income_history ---------------------------


def test_noninterest_income_history_extracts_series(ua: None) -> None:
    facts = _facts_with_concepts({
        "NoninterestIncome": [
            {"start": "2024-01-01", "end": "2024-03-31", "val": 12_000,
             "form": "10-Q", "fy": 2024, "fp": "Q1"},
            {"start": "2023-10-01", "end": "2023-12-31", "val": 11_500,
             "form": "10-Q", "fy": 2023, "fp": "Q4"},
        ],
    })
    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.side_effect = [
            _fake_response(TICKERS_FIXTURE),
            _fake_response(facts),
        ]
        rows = sec_edgar.get_noninterest_income_history("AAPL")
    assert [r["end_date"] for r in rows] == ["2024-03-31", "2023-12-31"]
    assert rows[0]["concept"] == "NoninterestIncome"
    assert rows[0]["value"] == 12_000


def test_noninterest_income_history_empty_when_concept_missing(ua: None) -> None:
    facts = _facts_with_concepts({
        "Revenues": [
            {"start": "2024-01-01", "end": "2024-03-31", "val": 1_000,
             "form": "10-Q", "fy": 2024, "fp": "Q1"},
        ],
    })
    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.side_effect = [
            _fake_response(TICKERS_FIXTURE),
            _fake_response(facts),
        ]
        rows = sec_edgar.get_noninterest_income_history("AAPL")
    assert rows == []


# --- as_of historical filtering ---------------------------------------------


def _facts_with_filed(units: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build a Revenues facts payload where every fact has a ``filed`` date."""
    return _facts_with("Revenues", units)


def test_revenue_history_as_of_none_preserves_existing_behavior(ua: None) -> None:
    """``as_of=None`` must return the same rows as the un-filtered call."""
    facts = _facts_with_filed([
        {"start": "2023-01-01", "end": "2023-12-31", "val": 100,
         "form": "10-K", "fy": 2023, "fp": "FY", "filed": "2024-02-01"},
        {"start": "2024-01-01", "end": "2024-12-31", "val": 120,
         "form": "10-K", "fy": 2024, "fp": "FY", "filed": "2025-02-01"},
    ])
    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.side_effect = [
            _fake_response(TICKERS_FIXTURE),
            _fake_response(facts),
        ]
        rows_default = sec_edgar.get_revenue_history("AAPL")
        rows_none = sec_edgar.get_revenue_history("AAPL", as_of=None)
    assert rows_default == rows_none
    assert [r["end_date"] for r in rows_default] == ["2024-12-31", "2023-12-31"]


def test_revenue_history_as_of_historical_returns_slice(ua: None) -> None:
    """``as_of`` between two filing dates yields only the older filing."""
    facts = _facts_with_filed([
        {"start": "2023-01-01", "end": "2023-12-31", "val": 100,
         "form": "10-K", "fy": 2023, "fp": "FY", "filed": "2024-02-01"},
        {"start": "2024-01-01", "end": "2024-12-31", "val": 120,
         "form": "10-K", "fy": 2024, "fp": "FY", "filed": "2025-02-01"},
    ])
    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.side_effect = [
            _fake_response(TICKERS_FIXTURE),
            _fake_response(facts),
        ]
        rows = sec_edgar.get_revenue_history("AAPL", as_of="2024-06-30")
    assert len(rows) == 1
    assert rows[0]["end_date"] == "2023-12-31"
    assert rows[0]["value"] == 100


def test_revenue_history_as_of_before_any_filings_returns_empty(ua: None) -> None:
    """``as_of`` before every filing date drops everything."""
    facts = _facts_with_filed([
        {"start": "2023-01-01", "end": "2023-12-31", "val": 100,
         "form": "10-K", "fy": 2023, "fp": "FY", "filed": "2024-02-01"},
    ])
    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.side_effect = [
            _fake_response(TICKERS_FIXTURE),
            _fake_response(facts),
        ]
        rows = sec_edgar.get_revenue_history("AAPL", as_of="2020-01-01")
    assert rows == []


def test_revenue_history_as_of_exact_filing_date_is_inclusive(ua: None) -> None:
    """``as_of`` equal to a filing's ``filed`` date INCLUDES that filing (≤)."""
    facts = _facts_with_filed([
        {"start": "2023-01-01", "end": "2023-12-31", "val": 100,
         "form": "10-K", "fy": 2023, "fp": "FY", "filed": "2024-02-01"},
    ])
    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.side_effect = [
            _fake_response(TICKERS_FIXTURE),
            _fake_response(facts),
        ]
        rows = sec_edgar.get_revenue_history("AAPL", as_of="2024-02-01")
    assert len(rows) == 1
    assert rows[0]["end_date"] == "2023-12-31"


def test_revenue_history_as_of_drops_facts_without_filed_date(ua: None) -> None:
    """Facts missing a ``filed`` date can't be placed in time -> drop under as_of."""
    facts = _facts_with_filed([
        {"start": "2023-01-01", "end": "2023-12-31", "val": 100,
         "form": "10-K", "fy": 2023, "fp": "FY"},  # no `filed` key
        {"start": "2024-01-01", "end": "2024-12-31", "val": 120,
         "form": "10-K", "fy": 2024, "fp": "FY", "filed": "2025-02-01"},
    ])
    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.side_effect = [
            _fake_response(TICKERS_FIXTURE),
            _fake_response(facts),
        ]
        # Without as_of, both come through (no filing filter).
        rows_unfiltered = sec_edgar.get_revenue_history("AAPL")
    assert len(rows_unfiltered) == 2

    # With as_of past both filings, only the one with `filed` survives.
    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.side_effect = [
            _fake_response(TICKERS_FIXTURE),
            _fake_response(facts),
        ]
        sec_edgar._cache.clear()
        rows_asof = sec_edgar.get_revenue_history("AAPL", as_of="2030-01-01")
    assert len(rows_asof) == 1
    assert rows_asof[0]["end_date"] == "2024-12-31"


def test_revenue_history_as_of_shares_upstream_cache(ua: None) -> None:
    """``as_of`` filtering happens in-memory after the company-facts fetch, so
    a second call with a different ``as_of`` reuses the cached HTTP payload
    without re-issuing the request (no cache poisoning)."""
    facts = _facts_with_filed([
        {"start": "2023-01-01", "end": "2023-12-31", "val": 100,
         "form": "10-K", "fy": 2023, "fp": "FY", "filed": "2024-02-01"},
        {"start": "2024-01-01", "end": "2024-12-31", "val": 120,
         "form": "10-K", "fy": 2024, "fp": "FY", "filed": "2025-02-01"},
    ])
    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.side_effect = [
            _fake_response(TICKERS_FIXTURE),
            _fake_response(facts),
        ]
        rows_a = sec_edgar.get_revenue_history("AAPL", as_of="2024-06-30")
        rows_b = sec_edgar.get_revenue_history("AAPL", as_of="2025-06-30")
        rows_c = sec_edgar.get_revenue_history("AAPL")  # un-filtered
        # Exactly two HTTP calls (tickers + facts); subsequent calls are
        # cache hits regardless of as_of.
        assert mock_get.call_count == 2
    assert len(rows_a) == 1
    assert len(rows_b) == 2
    assert len(rows_c) == 2


def test_gross_profit_and_ocf_history_accept_as_of(ua: None) -> None:
    """Both other public XBRL fetches must accept ``as_of`` symmetrically."""
    facts = _facts_with_concepts({
        "GrossProfit": [
            {"start": "2023-01-01", "end": "2023-12-31", "val": 50,
             "form": "10-K", "fy": 2023, "fp": "FY", "filed": "2024-02-01"},
            {"start": "2024-01-01", "end": "2024-12-31", "val": 60,
             "form": "10-K", "fy": 2024, "fp": "FY", "filed": "2025-02-01"},
        ],
        "NetCashProvidedByOperatingActivities": [
            {"start": "2023-01-01", "end": "2023-12-31", "val": 200,
             "form": "10-K", "fy": 2023, "fp": "FY", "filed": "2024-02-01"},
            {"start": "2024-01-01", "end": "2024-12-31", "val": 220,
             "form": "10-K", "fy": 2024, "fp": "FY", "filed": "2025-02-01"},
        ],
    })
    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.side_effect = [
            _fake_response(TICKERS_FIXTURE),
            _fake_response(facts),
        ]
        gp = sec_edgar.get_gross_profit_history("AAPL", as_of="2024-06-30")
        ocf = sec_edgar.get_operating_cash_flow_history("AAPL", as_of="2024-06-30")
    assert [r["end_date"] for r in gp] == ["2023-12-31"]
    assert [r["end_date"] for r in ocf] == ["2023-12-31"]


# --- Gross-margin XBRL fallback fetch functions (P3 audit fix-up, May 2026) --


def test_get_sales_revenue_gross_history_extracts_legacy_concept(ua: None) -> None:
    """``SalesRevenueGross`` is the legacy pre-ASC 606 revenue concept."""
    facts = _facts_with_concepts({
        "SalesRevenueGross": [
            {"start": "2024-01-01", "end": "2024-03-31", "val": 1_500,
             "form": "10-Q", "fy": 2024, "fp": "Q1"},
            {"start": "2024-04-01", "end": "2024-06-30", "val": 1_600,
             "form": "10-Q", "fy": 2024, "fp": "Q2"},
        ],
    })
    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.side_effect = [
            _fake_response(TICKERS_FIXTURE),
            _fake_response(facts),
        ]
        rows = sec_edgar.get_sales_revenue_gross_history("AAPL")
    assert len(rows) == 2
    # Sorted desc by end_date.
    assert rows[0]["end_date"] == "2024-06-30"
    assert rows[0]["concept"] == "SalesRevenueGross"


def test_get_cost_of_revenue_history_merges_variants(ua: None) -> None:
    """All CostOfRevenue concept variants are merged into one series."""
    facts = _facts_with_concepts({
        "CostOfGoodsSold": [
            {"start": "2018-01-01", "end": "2018-03-31", "val": 300,
             "form": "10-Q", "fy": 2018, "fp": "Q1"},
        ],
        # Modern variant -- different period, no collision.
        "CostOfRevenue": [
            {"start": "2024-01-01", "end": "2024-03-31", "val": 500,
             "form": "10-Q", "fy": 2024, "fp": "Q1"},
        ],
    })
    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.side_effect = [
            _fake_response(TICKERS_FIXTURE),
            _fake_response(facts),
        ]
        rows = sec_edgar.get_cost_of_revenue_history("AAPL")
    assert len(rows) == 2
    by_end = {r["end_date"]: r for r in rows}
    assert by_end["2024-03-31"]["value"] == 500
    assert by_end["2018-03-31"]["value"] == 300


def test_get_operating_income_history_extracts(ua: None) -> None:
    """``OperatingIncomeLoss`` is the operating-margin proxy fallback."""
    facts = _facts_with_concepts({
        "OperatingIncomeLoss": [
            {"start": "2024-01-01", "end": "2024-03-31", "val": 200,
             "form": "10-Q", "fy": 2024, "fp": "Q1"},
        ],
    })
    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        mock_get.side_effect = [
            _fake_response(TICKERS_FIXTURE),
            _fake_response(facts),
        ]
        rows = sec_edgar.get_operating_income_history("AAPL")
    assert len(rows) == 1
    assert rows[0]["value"] == 200
    assert rows[0]["concept"] == "OperatingIncomeLoss"


def test_fallback_fetch_functions_empty_when_concept_missing(ua: None) -> None:
    """A company that doesn't file any of the fallback concepts -> empty list."""
    facts = _facts_with_concepts({
        # Only the canonical GP -- none of the fallbacks present.
        "GrossProfit": [
            {"start": "2024-01-01", "end": "2024-03-31", "val": 400,
             "form": "10-Q", "fy": 2024, "fp": "Q1"},
        ],
    })
    with patch("lthcs.sources.sec_edgar.requests.get") as mock_get:
        # Three sequential calls -> three pairs of (tickers, facts) responses.
        mock_get.side_effect = [
            _fake_response(TICKERS_FIXTURE),
            _fake_response(facts),
        ]
        # The first call hits both URLs; subsequent calls cache-hit on the
        # same facts payload.
        assert sec_edgar.get_sales_revenue_gross_history("AAPL") == []
        assert sec_edgar.get_cost_of_revenue_history("AAPL") == []
        assert sec_edgar.get_operating_income_history("AAPL") == []
