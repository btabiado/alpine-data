"""Unit tests for the SEC EDGAR Form D fetcher in fetch_market.py.

Targets:
  - _ai_keyword_hit         (issuer-name word-boundary matcher)
  - _parse_form_d_xml       (primary_doc.xml field extractor)
  - _fetch_sec_form_d_filings_impl  (one-shot search + optional enrich)
  - fetch_sec_form_d_filings        (stale-fallback wrapper)

All HTTP is mocked via ``unittest.mock.patch`` on ``fetch_market._sec_get``.
No real network calls.
"""
from __future__ import annotations

from unittest.mock import patch

import fetch_market


# ============================================================================
# _ai_keyword_hit — word-boundary matcher
# ============================================================================

def test_ai_keyword_hit_positive_whole_word():
    """Word-boundary match: 'Acme AI Labs' has 'ai' as a whole word."""
    assert fetch_market._ai_keyword_hit("Acme AI Labs, Inc.") is True


def test_ai_keyword_hit_positive_multiword():
    """Multi-word phrase: substring match."""
    assert fetch_market._ai_keyword_hit("Foundation Machine Learning Co") is True


def test_ai_keyword_hit_positive_brand():
    assert fetch_market._ai_keyword_hit("Anthropic PBC") is True


def test_ai_keyword_hit_negative_partial_word():
    """'main' contains 'ai' but only as a substring — should not match."""
    assert fetch_market._ai_keyword_hit("Mainstreet Holdings LLC") is False
    assert fetch_market._ai_keyword_hit("Captain Industries") is False


def test_ai_keyword_hit_negative_empty():
    assert fetch_market._ai_keyword_hit("") is False
    assert fetch_market._ai_keyword_hit(None) is False  # type: ignore[arg-type]


# ============================================================================
# _parse_form_d_xml — XML field extractor
# ============================================================================

_SAMPLE_FORM_D_XML = """<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/formd">
  <offeringData>
    <dateOfFirstSale>2026-04-15</dateOfFirstSale>
    <federalExemptionsExclusions>
      <item>06b</item>
      <item>3C</item>
    </federalExemptionsExclusions>
    <offeringSalesAmounts>
      <totalOfferingAmount>50000000</totalOfferingAmount>
      <totalAmountSold>12500000</totalAmountSold>
    </offeringSalesAmounts>
    <federalExemptionsExclusionsList>
      <exemption>06b</exemption>
      <exemption>3C</exemption>
    </federalExemptionsExclusionsList>
  </offeringData>
</edgarSubmission>
"""


def test_parse_form_d_xml_happy_path():
    out = fetch_market._parse_form_d_xml(_SAMPLE_FORM_D_XML)
    assert out["total_offering_amount"] == 50_000_000.0
    assert out["total_amount_sold"] == 12_500_000.0
    assert out["date_of_first_sale"] == "2026-04-15"
    assert out["exemptions"] == ["06b", "3C"]


def test_parse_form_d_xml_missing_fields():
    """All four fields are tolerant of an empty XML doc."""
    out = fetch_market._parse_form_d_xml(
        '<?xml version="1.0"?><edgarSubmission></edgarSubmission>'
    )
    assert out["total_offering_amount"] is None
    assert out["total_amount_sold"] is None
    assert out["date_of_first_sale"] == ""
    assert out["exemptions"] == []


def test_parse_form_d_xml_garbage_returns_defaults():
    """Malformed XML must not raise — returns the default-shaped dict."""
    out = fetch_market._parse_form_d_xml("<not really xml")
    assert out["total_offering_amount"] is None
    assert out["total_amount_sold"] is None
    assert out["date_of_first_sale"] == ""
    assert out["exemptions"] == []


def test_parse_form_d_xml_empty_input():
    out = fetch_market._parse_form_d_xml("")
    assert out["exemptions"] == []
    out = fetch_market._parse_form_d_xml(None)  # type: ignore[arg-type]
    assert out["exemptions"] == []


# ============================================================================
# _fetch_sec_form_d_filings_impl — search + enrich
# ============================================================================

def _make_search_response(rows):
    """Wrap a list of (display_name, cik, adsh, file_date) tuples into the
    EDGAR search-index JSON shape."""
    hits = []
    for name, cik, adsh, file_date in rows:
        hits.append({
            "_id": f"{adsh}:primary_doc.xml",
            "_source": {
                "display_names": [f"{name}  (CIK {cik}) (Filer)"],
                "ciks": [cik],
                "form": "D",
                "file_date": file_date,
            },
        })
    return {"hits": {"total": {"value": len(hits)}, "hits": hits}}


def test_fetch_form_d_filters_by_ai_and_enriches():
    """Three search hits: only the AI-adjacent one survives the filter,
    and its primary_doc.xml is fetched & parsed."""
    search_json = _make_search_response([
        ("Mainstreet Holdings LLC", "0001000001", "0001000001-26-000001", "2026-05-10"),
        ("Acme AI Labs, Inc.",       "0001000002", "0001000002-26-000002", "2026-05-09"),
        ("Generic Widgets Co",       "0001000003", "0001000003-26-000003", "2026-05-08"),
    ])

    # _sec_get is called once for search, then once per AI hit for the XML.
    # Order: [search_json, primary_doc_xml]
    responses = [search_json, _SAMPLE_FORM_D_XML]

    def fake_sec_get(url, params=None, timeout=20):
        return responses.pop(0)

    with patch.object(fetch_market, "_sec_get", side_effect=fake_sec_get), \
         patch.object(fetch_market.time, "sleep") as mock_sleep:
        out = fetch_market._fetch_sec_form_d_filings_impl(
            days=60, max_results=20, enrich_details=True,
        )

    assert len(out) == 1
    row = out[0]
    assert row["issuer"] == "Acme AI Labs, Inc."
    assert row["cik"] == "0001000002"
    assert row["accession"] == "0001000002-26-000002"
    assert row["filed_date"] == "2026-05-09"
    assert row["form"] == "D"
    assert "0001000002-26-000002-index.htm" in row["filing_url"]
    # Enrichment populated:
    assert row["total_offering_amount"] == 50_000_000.0
    assert row["total_amount_sold"] == 12_500_000.0
    assert row["date_of_first_sale"] == "2026-04-15"
    assert row["exemptions"] == ["06b", "3C"]
    # We slept once between filing fetches (just one filing here).
    assert mock_sleep.called


def test_fetch_form_d_no_enrich_skips_xml_fetch():
    """When enrich_details=False the per-filing XML fetch is skipped, so
    only the single search request is issued."""
    search_json = _make_search_response([
        ("Acme AI Labs, Inc.", "0001000002", "0001000002-26-000002", "2026-05-09"),
    ])
    with patch.object(fetch_market, "_sec_get", return_value=search_json) as mget:
        out = fetch_market._fetch_sec_form_d_filings_impl(
            days=60, max_results=20, enrich_details=False,
        )
    assert len(out) == 1
    assert out[0]["total_offering_amount"] is None
    # Exactly one HTTP call (the search) — no per-filing fetches.
    assert mget.call_count == 1


def test_fetch_form_d_search_failure_returns_empty():
    """If the search endpoint returns None (HTTP 4xx/5xx, network down),
    the fetcher must return [] without crashing."""
    with patch.object(fetch_market, "_sec_get", return_value=None):
        out = fetch_market._fetch_sec_form_d_filings_impl(
            days=60, max_results=20, enrich_details=True,
        )
    assert out == []


def test_fetch_form_d_respects_max_results():
    """Hand the matcher 5 AI-adjacent issuers, ask for 2 — gets 2."""
    rows = [
        (f"AI Company {i}", f"000100000{i}", f"000100000{i}-26-00000{i}", "2026-05-10")
        for i in range(1, 6)
    ]
    search_json = _make_search_response(rows)
    # max_results=2 means we'll do 1 search + 2 XML fetches = 3 calls total.
    responses = [search_json, _SAMPLE_FORM_D_XML, _SAMPLE_FORM_D_XML]
    with patch.object(fetch_market, "_sec_get", side_effect=responses), \
         patch.object(fetch_market.time, "sleep"):
        out = fetch_market._fetch_sec_form_d_filings_impl(
            days=60, max_results=2, enrich_details=True,
        )
    assert len(out) == 2


def test_fetch_form_d_xml_fetch_failure_keeps_row_with_defaults():
    """When a per-filing XML fetch fails (returns None), the row is kept
    with its enrichment fields left at their defaults — graceful degrade."""
    search_json = _make_search_response([
        ("Acme AI Labs, Inc.", "0001000002", "0001000002-26-000002", "2026-05-09"),
    ])
    with patch.object(fetch_market, "_sec_get", side_effect=[search_json, None]), \
         patch.object(fetch_market.time, "sleep"):
        out = fetch_market._fetch_sec_form_d_filings_impl(
            days=60, max_results=20, enrich_details=True,
        )
    assert len(out) == 1
    assert out[0]["issuer"] == "Acme AI Labs, Inc."
    # Enrichment defaults preserved on failed XML fetch.
    assert out[0]["total_offering_amount"] is None
    assert out[0]["total_amount_sold"] is None


# ============================================================================
# fetch_sec_form_d_filings — stale-fallback wrapper
# ============================================================================

def test_wrapper_uses_cache_on_failure():
    """If the live impl raises and a stale cache exists, return the cache."""
    cached = [{"issuer": "Cached AI Co", "filing_url": "x"}]
    with patch.object(
        fetch_market,
        "_fetch_sec_form_d_filings_impl",
        side_effect=RuntimeError("boom"),
    ), patch.object(fetch_market, "_stale_load", return_value=cached):
        out = fetch_market.fetch_sec_form_d_filings()
    assert out == cached


def test_wrapper_empty_live_returns_cache():
    """Empty live result + non-empty cache -> cache wins."""
    cached = [{"issuer": "Cached AI Co", "filing_url": "x"}]
    with patch.object(
        fetch_market,
        "_fetch_sec_form_d_filings_impl",
        return_value=[],
    ), patch.object(fetch_market, "_stale_load", return_value=cached):
        out = fetch_market.fetch_sec_form_d_filings()
    assert out == cached


def test_wrapper_no_cache_no_live_returns_empty_list():
    """Empty live + no cache -> [] (never None)."""
    with patch.object(
        fetch_market,
        "_fetch_sec_form_d_filings_impl",
        return_value=[],
    ), patch.object(fetch_market, "_stale_load", return_value=None):
        out = fetch_market.fetch_sec_form_d_filings()
    assert out == []


def test_wrapper_saves_on_success():
    """Successful live fetch -> _stale_save is called with the value."""
    live = [{"issuer": "Live AI Co", "filing_url": "y"}]
    with patch.object(
        fetch_market,
        "_fetch_sec_form_d_filings_impl",
        return_value=live,
    ), patch.object(fetch_market, "_stale_save") as save:
        out = fetch_market.fetch_sec_form_d_filings()
    assert out == live
    save.assert_called_once_with("fetch_sec_form_d_filings", live)
