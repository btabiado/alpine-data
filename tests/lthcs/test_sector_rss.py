"""Tests for lthcs.sources.sector_rss.

All HTTP is mocked. The module-level cache is redirected to ``tmp_path``
via ``monkeypatch`` so every test starts cold. The RSS token bucket is
replaced with a generously-sized one by default; specific tests that
exercise rate limiting install their own.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from lthcs.sources import sector_rss as sr
from lthcs.sources._cache import FileCache
from lthcs.sources._ratelimit import TokenBucket


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh cache dir for every test."""
    monkeypatch.setattr(
        sr, "_RSS_CACHE", FileCache("sector_rss", root=tmp_path)
    )


@pytest.fixture(autouse=True)
def generous_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default bucket never blocks. Per-test overrides allowed."""
    monkeypatch.setattr(
        sr, "_RSS_BUCKET", TokenBucket(capacity=1_000_000, refill_rate=1_000_000)
    )


def _mock_response(
    *, text: Optional[str] = None, status_code: int = 200
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.text = text if text is not None else ""
    return resp


def _rss_xml(items: List[Dict[str, str]], *, channel_title: str = "Feed") -> str:
    """Build a minimal RSS 2.0 doc out of dicts."""
    item_xml = "".join(
        (
            "<item>"
            f"<title>{i.get('title', '')}</title>"
            f"<link>{i.get('link', '')}</link>"
            f"<description>{i.get('description', '')}</description>"
            f"<pubDate>{i.get('pubDate', '')}</pubDate>"
            "</item>"
        )
        for i in items
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel>'
        f"<title>{channel_title}</title>"
        "<link>https://example.com/</link>"
        "<description>test</description>"
        f"{item_xml}"
        "</channel></rss>"
    )


def _recent_iso(days_ago: int = 0) -> str:
    return (_dt.date.today() - _dt.timedelta(days=days_ago)).isoformat()


def _rfc822(days_ago: int = 0) -> str:
    """RFC 822 datetime ``days_ago`` days before today at noon UTC."""
    d = _dt.date.today() - _dt.timedelta(days=days_ago)
    dt = _dt.datetime(d.year, d.month, d.day, 12, 0, 0, tzinfo=_dt.timezone.utc)
    # Mon, 12 May 2026 12:00:00 +0000
    return dt.strftime("%a, %d %b %Y %H:%M:%S %z")


def _url_stub_factory(url_to_text: Dict[str, str]) -> Any:
    """Build a side_effect for ``requests.get`` that dispatches on URL."""

    def _stub(url: str, **_kwargs: Any) -> MagicMock:
        if url in url_to_text:
            return _mock_response(text=url_to_text[url])
        return _mock_response(status_code=404, text="not found")

    return _stub


# ---------------------------------------------------------------------------
# fetch_fda_press_releases
# ---------------------------------------------------------------------------


def test_fetch_fda_press_releases_parses_both_feeds() -> None:
    press_xml = _rss_xml(
        [
            {
                "title": "FDA announces new policy",
                "link": "https://fda.gov/press/1",
                "description": "<p>Policy change details.</p>",
                "pubDate": _rfc822(1),
            }
        ]
    )
    approvals_xml = _rss_xml(
        [
            {
                "title": "FDA approves new tirzepatide indication",
                "link": "https://fda.gov/approvals/1",
                "description": "Approval of Eli Lilly's drug.",
                "pubDate": _rfc822(2),
            }
        ]
    )
    stub = _url_stub_factory(
        {
            sr.FDA_PRESS_RSS: press_xml,
            sr.FDA_DRUG_APPROVALS_RSS: approvals_xml,
        }
    )
    with patch.object(sr.requests, "get", side_effect=stub) as mg:
        out = sr.fetch_fda_press_releases(max_age_days=30)
    # Both feeds got hit.
    urls_called = {c.args[0] for c in mg.call_args_list}
    assert sr.FDA_PRESS_RSS in urls_called
    assert sr.FDA_DRUG_APPROVALS_RSS in urls_called

    assert len(out) == 2
    # Newest first.
    titles = [it["title"] for it in out]
    assert "FDA announces new policy" in titles
    assert "FDA approves new tirzepatide indication" in titles
    # Each item carries source/feed labels.
    feeds = {it["feed"] for it in out}
    assert feeds == {"press", "drug_approvals"}
    for it in out:
        assert it["source"] == "FDA"
        assert isinstance(it["published_at"], str)
        assert isinstance(it["summary"], str)


def test_fetch_fda_press_releases_dedupes_by_url() -> None:
    shared_url = "https://fda.gov/shared/announcement-42"
    press_xml = _rss_xml(
        [
            {
                "title": "Joint announcement",
                "link": shared_url,
                "description": "Press version",
                "pubDate": _rfc822(0),
            }
        ]
    )
    approvals_xml = _rss_xml(
        [
            {
                "title": "Joint announcement",
                "link": shared_url,
                "description": "Approvals version",
                "pubDate": _rfc822(0),
            }
        ]
    )
    stub = _url_stub_factory(
        {sr.FDA_PRESS_RSS: press_xml, sr.FDA_DRUG_APPROVALS_RSS: approvals_xml}
    )
    with patch.object(sr.requests, "get", side_effect=stub):
        out = sr.fetch_fda_press_releases(max_age_days=30)
    assert len(out) == 1
    # Whichever wins, the URL appears exactly once.
    urls = [it["url"] for it in out]
    assert urls.count(shared_url) == 1


def test_fetch_fda_press_releases_drops_old_items() -> None:
    press_xml = _rss_xml(
        [
            {
                "title": "Recent item",
                "link": "https://fda.gov/recent",
                "description": "",
                "pubDate": _rfc822(2),
            },
            {
                "title": "Old item",
                "link": "https://fda.gov/old",
                "description": "",
                "pubDate": _rfc822(45),
            },
        ]
    )
    approvals_xml = _rss_xml([])
    stub = _url_stub_factory(
        {sr.FDA_PRESS_RSS: press_xml, sr.FDA_DRUG_APPROVALS_RSS: approvals_xml}
    )
    with patch.object(sr.requests, "get", side_effect=stub):
        out = sr.fetch_fda_press_releases(max_age_days=30)
    titles = [it["title"] for it in out]
    assert titles == ["Recent item"]


def test_fetch_fda_press_releases_one_feed_404_other_still_works() -> None:
    press_xml = _rss_xml(
        [
            {
                "title": "Survives",
                "link": "https://fda.gov/p/1",
                "description": "",
                "pubDate": _rfc822(0),
            }
        ]
    )

    def _stub(url: str, **_kwargs: Any) -> MagicMock:
        if url == sr.FDA_PRESS_RSS:
            return _mock_response(text=press_xml)
        if url == sr.FDA_DRUG_APPROVALS_RSS:
            return _mock_response(status_code=404, text="gone")
        return _mock_response(status_code=404, text="x")

    with patch.object(sr.requests, "get", side_effect=_stub):
        out = sr.fetch_fda_press_releases()
    assert len(out) == 1
    assert out[0]["title"] == "Survives"


# ---------------------------------------------------------------------------
# fetch_eia_today_in_energy
# ---------------------------------------------------------------------------


def test_fetch_eia_today_in_energy_parses_items() -> None:
    xml = _rss_xml(
        [
            {
                "title": "U.S. crude oil inventories rise",
                "link": "https://eia.gov/today/123",
                "description": "Weekly petroleum status report shows build.",
                "pubDate": _rfc822(0),
            },
            {
                "title": "ExxonMobil expands Permian operations",
                "link": "https://eia.gov/today/124",
                "description": "Discussion of Exxon's plans.",
                "pubDate": _rfc822(1),
            },
        ]
    )
    with patch.object(
        sr.requests, "get", return_value=_mock_response(text=xml)
    ) as mg:
        out = sr.fetch_eia_today_in_energy()
    mg.assert_called_once()
    assert mg.call_args.args[0] == sr.EIA_TODAY_IN_ENERGY_RSS
    assert len(out) == 2
    for it in out:
        assert it["source"] == "EIA"
        assert it["feed"] == "today_in_energy"


def test_fetch_eia_today_in_energy_empty_xml_returns_empty() -> None:
    with patch.object(
        sr.requests, "get", return_value=_mock_response(text="")
    ):
        assert sr.fetch_eia_today_in_energy() == []


# ---------------------------------------------------------------------------
# fetch_fed_press_releases
# ---------------------------------------------------------------------------


def test_fetch_fed_press_releases_parses_items() -> None:
    xml = _rss_xml(
        [
            {
                "title": "Federal Reserve releases stress test results",
                "link": "https://federalreserve.gov/press/1",
                "description": "<p>Annual large bank stress test.</p>",
                "pubDate": _rfc822(3),
            },
            {
                "title": "FOMC statement on federal funds rate",
                "link": "https://federalreserve.gov/press/2",
                "description": "Rate decision.",
                "pubDate": _rfc822(5),
            },
        ]
    )
    with patch.object(
        sr.requests, "get", return_value=_mock_response(text=xml)
    ) as mg:
        out = sr.fetch_fed_press_releases()
    assert mg.call_args.args[0] == sr.FED_PRESS_RELEASES_RSS
    assert len(out) == 2
    for it in out:
        assert it["source"] == "Fed"
        assert it["feed"] == "press"


def test_fetch_fed_press_releases_malformed_xml_returns_empty() -> None:
    with patch.object(
        sr.requests, "get", return_value=_mock_response(text="<this is not> xml")
    ):
        assert sr.fetch_fed_press_releases() == []


def test_fetch_fed_press_releases_request_exception_returns_empty() -> None:
    with patch.object(
        sr.requests, "get", side_effect=sr.requests.RequestException("boom")
    ):
        assert sr.fetch_fed_press_releases() == []


def test_fetch_fed_press_releases_non_200_returns_empty() -> None:
    with patch.object(
        sr.requests, "get", return_value=_mock_response(status_code=503, text="")
    ):
        assert sr.fetch_fed_press_releases() == []


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def test_fetch_fda_caches_between_calls() -> None:
    press_xml = _rss_xml(
        [
            {
                "title": "X",
                "link": "https://fda.gov/x",
                "description": "",
                "pubDate": _rfc822(0),
            }
        ]
    )
    approvals_xml = _rss_xml([])
    stub = _url_stub_factory(
        {sr.FDA_PRESS_RSS: press_xml, sr.FDA_DRUG_APPROVALS_RSS: approvals_xml}
    )
    with patch.object(sr.requests, "get", side_effect=stub) as mg:
        a = sr.fetch_fda_press_releases()
        b = sr.fetch_fda_press_releases()
    # Two URLs, one call each, even after two top-level fetches.
    assert mg.call_count == 2
    assert a == b


def test_fetch_eia_caches() -> None:
    xml = _rss_xml(
        [
            {
                "title": "Y",
                "link": "https://eia.gov/y",
                "description": "",
                "pubDate": _rfc822(0),
            }
        ]
    )
    with patch.object(
        sr.requests, "get", return_value=_mock_response(text=xml)
    ) as mg:
        a = sr.fetch_eia_today_in_energy()
        b = sr.fetch_eia_today_in_energy()
    assert mg.call_count == 1
    assert a == b


def test_rate_limited_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the token bucket is drained, no HTTP call is made."""
    monkeypatch.setattr(
        sr, "_RSS_BUCKET", TokenBucket(capacity=1, refill_rate=0.0)
    )
    sr._RSS_BUCKET.try_acquire()  # drain
    with patch.object(sr.requests, "get") as mg:
        assert sr.fetch_fed_press_releases() == []
    mg.assert_not_called()


# ---------------------------------------------------------------------------
# aggregate_sector_events
# ---------------------------------------------------------------------------


def _three_feed_stub(
    *,
    fda_press: str = "",
    fda_approvals: str = "",
    eia: str = "",
    fed: str = "",
) -> Any:
    return _url_stub_factory(
        {
            sr.FDA_PRESS_RSS: fda_press or _rss_xml([]),
            sr.FDA_DRUG_APPROVALS_RSS: fda_approvals or _rss_xml([]),
            sr.EIA_TODAY_IN_ENERGY_RSS: eia or _rss_xml([]),
            sr.FED_PRESS_RELEASES_RSS: fed or _rss_xml([]),
        }
    )


def test_aggregate_sector_events_matches_across_sectors() -> None:
    fda_approvals_xml = _rss_xml(
        [
            {
                "title": "FDA approves Lilly's tirzepatide for new indication",
                "link": "https://fda.gov/a/1",
                "description": "Eli Lilly drug expanded.",
                "pubDate": _rfc822(2),
            }
        ]
    )
    eia_xml = _rss_xml(
        [
            {
                "title": "Exxon expands offshore drilling",
                "link": "https://eia.gov/today/1",
                "description": "ExxonMobil announced new projects.",
                "pubDate": _rfc822(1),
            }
        ]
    )
    fed_xml = _rss_xml(
        [
            {
                "title": "JPMorgan and peers pass annual stress test",
                "link": "https://fed.gov/p/1",
                "description": "JPMorgan, Bank of America, and others...",
                "pubDate": _rfc822(0),
            }
        ]
    )
    stub = _three_feed_stub(
        fda_approvals=fda_approvals_xml, eia=eia_xml, fed=fed_xml
    )
    with patch.object(sr.requests, "get", side_effect=stub):
        out = sr.aggregate_sector_events(["LLY", "XOM", "JPM"], max_age_days=30)

    assert set(out.keys()) == {"LLY", "XOM", "JPM"}

    lly = out["LLY"]
    assert lly["event_count"] >= 1
    assert any("tirzepatide" in t.lower() or "lilly" in t.lower()
               for t in lly["event_titles"])
    assert "pharma" in lly["sectors_matched"]
    assert lly["first_seen"] is not None
    assert lly["last_seen"] is not None

    xom = out["XOM"]
    assert xom["event_count"] >= 1
    assert "energy" in xom["sectors_matched"]

    jpm = out["JPM"]
    assert jpm["event_count"] >= 1
    assert "financials" in jpm["sectors_matched"]


def test_aggregate_sector_events_unknown_ticker_returns_empty() -> None:
    # AAPL is not in any of the three keyword maps.
    stub = _three_feed_stub()
    with patch.object(sr.requests, "get", side_effect=stub):
        out = sr.aggregate_sector_events(["AAPL"])
    assert "AAPL" in out
    aapl = out["AAPL"]
    assert aapl["ticker"] == "AAPL"
    assert aapl["event_count"] == 0
    assert aapl["event_titles"] == []
    assert aapl["first_seen"] is None
    assert aapl["last_seen"] is None
    assert aapl["sectors_matched"] == []


def test_aggregate_sector_events_empty_input() -> None:
    with patch.object(sr.requests, "get") as mg:
        assert sr.aggregate_sector_events([]) == {}
    mg.assert_not_called()


def test_aggregate_sector_events_sector_keyword_widens_net() -> None:
    """A Fed press release mentioning 'stress test' but no specific bank
    name should still hit every financials ticker via is_sector_relevant."""
    fed_xml = _rss_xml(
        [
            {
                "title": "Annual stress test results for large banking organizations",
                "link": "https://fed.gov/stress/1",
                "description": "Aggregate results show resilience.",
                "pubDate": _rfc822(0),
            }
        ]
    )
    stub = _three_feed_stub(fed=fed_xml)
    with patch.object(sr.requests, "get", side_effect=stub):
        out = sr.aggregate_sector_events(["WFC", "GS"], max_age_days=30)
    # Neither bank is named, but the sector keywords trigger.
    assert out["WFC"]["event_count"] == 1
    assert out["GS"]["event_count"] == 1


def test_aggregate_sector_events_skips_feeds_with_no_matching_tickers() -> None:
    """If no pharma tickers are requested, the FDA feed should not be hit."""
    stub = _three_feed_stub()
    with patch.object(sr.requests, "get", side_effect=stub) as mg:
        sr.aggregate_sector_events(["XOM"], max_age_days=30)
    urls_called = {c.args[0] for c in mg.call_args_list}
    assert sr.FDA_PRESS_RSS not in urls_called
    assert sr.FDA_DRUG_APPROVALS_RSS not in urls_called
    assert sr.FED_PRESS_RELEASES_RSS not in urls_called
    assert sr.EIA_TODAY_IN_ENERGY_RSS in urls_called


def test_aggregate_sector_events_one_feed_failure_does_not_break_others() -> None:
    """Fed feed 500s but pharma + energy still aggregate cleanly."""
    fda_approvals_xml = _rss_xml(
        [
            {
                "title": "Lilly approval",
                "link": "https://fda.gov/a/1",
                "description": "Eli Lilly drug update.",
                "pubDate": _rfc822(0),
            }
        ]
    )
    eia_xml = _rss_xml(
        [
            {
                "title": "Chevron expands LNG",
                "link": "https://eia.gov/today/1",
                "description": "Chevron announced new project.",
                "pubDate": _rfc822(0),
            }
        ]
    )

    def _stub(url: str, **_kwargs: Any) -> MagicMock:
        if url == sr.FDA_PRESS_RSS:
            return _mock_response(text=_rss_xml([]))
        if url == sr.FDA_DRUG_APPROVALS_RSS:
            return _mock_response(text=fda_approvals_xml)
        if url == sr.EIA_TODAY_IN_ENERGY_RSS:
            return _mock_response(text=eia_xml)
        if url == sr.FED_PRESS_RELEASES_RSS:
            return _mock_response(status_code=500, text="")
        return _mock_response(status_code=404, text="")

    with patch.object(sr.requests, "get", side_effect=_stub):
        out = sr.aggregate_sector_events(["LLY", "CVX", "JPM"], max_age_days=30)
    assert out["LLY"]["event_count"] >= 1
    assert out["CVX"]["event_count"] >= 1
    # Fed feed broken => no JPM events.
    assert out["JPM"]["event_count"] == 0


# ---------------------------------------------------------------------------
# parse_thesis_signal
# ---------------------------------------------------------------------------


def test_parse_thesis_signal_zero_events() -> None:
    sig = sr.parse_thesis_signal(sr._empty_aggregate("LLY"))
    assert sig["ticker"] == "LLY"
    assert sig["article_count"] == 0
    assert sig["mean_sentiment_score"] is None
    assert sig["mean_relevance_score"] is None
    assert sig["label_counts"] == {
        "Bearish": 0,
        "Somewhat-Bearish": 0,
        "Neutral": 0,
        "Somewhat-Bullish": 0,
        "Bullish": 0,
    }
    assert sig["source"] == "sector_rss_aggregate"
    assert sig["last_scored"] == sr._today_iso()


def test_parse_thesis_signal_one_event_low_band() -> None:
    agg = {
        "ticker": "LLY",
        "event_count": 1,
        "event_titles": ["FDA approves tirzepatide"],
        "first_seen": _recent_iso(2),
        "last_seen": _recent_iso(2),
        "sectors_matched": ["pharma"],
    }
    sig = sr.parse_thesis_signal(agg)
    assert sig["article_count"] == 1
    assert sig["mean_sentiment_score"] == pytest.approx(0.2)
    assert sig["mean_relevance_score"] == 0.5
    assert sig["label_counts"]["Neutral"] == 1


def test_parse_thesis_signal_three_events_high_band() -> None:
    agg = {
        "ticker": "XOM",
        "event_count": 3,
        "event_titles": ["a", "b", "c"],
        "first_seen": _recent_iso(7),
        "last_seen": _recent_iso(0),
        "sectors_matched": ["energy"],
    }
    sig = sr.parse_thesis_signal(agg)
    assert sig["article_count"] == 3
    assert sig["mean_sentiment_score"] == pytest.approx(0.5)
    assert sig["label_counts"]["Neutral"] == 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_to_iso_date_handles_rfc822() -> None:
    assert sr._to_iso_date("Mon, 12 May 2026 10:30:00 GMT") == "2026-05-12"
    assert sr._to_iso_date("Sat, 16 May 2026 09:00:00 +0000") == "2026-05-16"
    assert sr._to_iso_date("2026-05-15T12:34:56Z") == "2026-05-15"
    assert sr._to_iso_date("") is None
    assert sr._to_iso_date(None) is None
    assert sr._to_iso_date("not a date") is None


def test_strip_html_removes_tags_and_entities() -> None:
    raw = "<p>Drug&nbsp;approval &amp; safety alert &#39;2026&#39;</p>"
    assert sr._strip_html(raw) == "Drug approval & safety alert '2026'"


def test_is_sector_relevant() -> None:
    pharma_item = {"title": "FDA approves new biosimilar", "summary": ""}
    energy_item = {"title": "Weekly crude oil inventories", "summary": ""}
    fin_item = {"title": "Bank stress test scenarios", "summary": ""}
    noise_item = {"title": "Unrelated thing", "summary": ""}
    assert sr.is_sector_relevant(pharma_item, "pharma") is True
    assert sr.is_sector_relevant(energy_item, "energy") is True
    assert sr.is_sector_relevant(fin_item, "financials") is True
    assert sr.is_sector_relevant(noise_item, "pharma") is False
    assert sr.is_sector_relevant(pharma_item, "energy") is False
    assert sr.is_sector_relevant(pharma_item, "nonexistent") is False


def test_keyword_maps_cover_expected_tickers() -> None:
    """Sanity check that the documented sector cohort is wired up."""
    for t in ("LLY", "MRK", "JNJ", "BMY", "VRTX"):
        assert t in sr.PHARMA_TICKER_KEYWORDS
        assert sr.PHARMA_TICKER_KEYWORDS[t], f"{t} has empty keyword list"
    for t in ("XOM", "CVX", "COP", "SLB", "VLO"):
        assert t in sr.ENERGY_TICKER_KEYWORDS
        assert sr.ENERGY_TICKER_KEYWORDS[t], f"{t} has empty keyword list"
    for t in ("JPM", "BAC", "WFC", "GS", "MS", "BLK"):
        assert t in sr.FINANCIALS_TICKER_KEYWORDS
        assert sr.FINANCIALS_TICKER_KEYWORDS[t], f"{t} has empty keyword list"
