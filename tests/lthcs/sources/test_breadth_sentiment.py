"""Tests for lthcs.sources.breadth_sentiment.

All HTTP is mocked via ``unittest.mock.patch`` (matches the existing
test style — repo doesn't pull in ``responses`` or ``requests_mock``).

The module-level ``FileCache`` is redirected to ``tmp_path`` and the
token bucket is replaced with a generously-sized one so tests start
cold and don't rate-limit themselves.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from lthcs.sources import breadth_sentiment as bs
from lthcs.sources._cache import FileCache
from lthcs.sources._ratelimit import TokenBucket


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "breadth_sentiment"


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh cache root for every test."""
    monkeypatch.setattr(
        bs, "_CACHE", FileCache("breadth_sentiment", root=tmp_path)
    )


@pytest.fixture(autouse=True)
def generous_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        bs, "_BUCKET", TokenBucket(capacity=1_000_000, refill_rate=1_000_000)
    )


def _mock_response(
    *, text: Optional[str] = None, status_code: int = 200
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.text = text if text is not None else ""
    return resp


def _fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Regime classification — boundaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ratio,expected",
    [
        (0.5, "complacent"),
        (0.6999, "complacent"),
        (0.7, "normal"),       # boundary: 0.7 -> normal
        (0.85, "normal"),
        (1.0, "normal"),       # boundary: 1.0 -> normal
        (1.0001, "elevated_hedging"),
        (1.15, "elevated_hedging"),
        (1.3, "elevated_hedging"),   # boundary: 1.3 -> elevated_hedging
        (1.3001, "panic"),
        (1.8, "panic"),
    ],
)
def test_putcall_regime_boundaries(ratio: float, expected: str) -> None:
    assert bs._putcall_regime(ratio) == expected


@pytest.mark.parametrize(
    "spread,expected",
    [
        (-50, "extreme_bearish"),
        (-30.0001, "extreme_bearish"),
        (-30, "bearish"),      # boundary: -30 -> bearish
        (-15, "bearish"),
        (-10, "neutral"),      # boundary: -10 -> neutral
        (0, "neutral"),
        (10, "neutral"),       # boundary: +10 -> neutral
        (10.0001, "bullish"),
        (25, "bullish"),
        (30, "bullish"),       # boundary: +30 -> bullish
        (30.0001, "extreme_bullish"),
        (45, "extreme_bullish"),
    ],
)
def test_aaii_regime_boundaries(spread: float, expected: str) -> None:
    assert bs._aaii_regime(spread) == expected


@pytest.mark.parametrize(
    "exposure,expected",
    [
        (-50, "defensive"),
        (29.99, "defensive"),
        (30, "moderate"),       # boundary: 30 -> moderate
        (55, "moderate"),
        (80, "moderate"),       # boundary: 80 -> moderate
        (80.0001, "aggressive"),
        (100, "aggressive"),
        (120, "aggressive"),    # boundary: 120 -> aggressive
        (120.0001, "leveraged"),
        (175, "leveraged"),
    ],
)
def test_naaim_regime_boundaries(exposure: float, expected: str) -> None:
    assert bs._naaim_regime(exposure) == expected


# ---------------------------------------------------------------------------
# CBOE Put/Call happy path
# ---------------------------------------------------------------------------


def test_fetch_put_call_happy_path() -> None:
    html_text = _fixture("cboe_sample.html")
    with patch.object(
        bs.requests, "get", return_value=_mock_response(text=html_text)
    ):
        result = bs.fetch_put_call()

    assert result is not None
    assert result["latest"] == 1.05
    assert result["regime"] == "elevated_hedging"
    assert result["source"] == "cboe_daily_html"
    # last_updated is the page's selectedDate (2026-05-16 in the fixture).
    assert result["last_updated"] == "2026-05-16"


def test_fetch_put_call_all_404_returns_none() -> None:
    with patch.object(
        bs.requests, "get", return_value=_mock_response(status_code=404)
    ):
        result = bs.fetch_put_call()
    assert result is None


def test_fetch_put_call_request_exception_returns_none() -> None:
    with patch.object(
        bs.requests, "get", side_effect=bs.requests.RequestException("boom")
    ):
        result = bs.fetch_put_call()
    assert result is None


# ---------------------------------------------------------------------------
# AAII Sentiment happy path
# ---------------------------------------------------------------------------


def test_fetch_aaii_sentiment_happy_path() -> None:
    html = _fixture("aaii_sample.html")
    with patch.object(
        bs.requests, "get", return_value=_mock_response(text=html)
    ):
        result = bs.fetch_aaii_sentiment()

    assert result is not None
    assert result["bullish_pct"] == 28.5
    assert result["bearish_pct"] == 42.1
    assert result["neutral_pct"] == 29.4
    # spread = 28.5 - 42.1 = -13.6
    assert result["bull_bear_spread"] == pytest.approx(-13.6, abs=0.01)
    assert result["regime"] == "bearish"
    assert result["week_ending"] == "2026-05-14"


def test_fetch_aaii_sentiment_fetch_failure_returns_none() -> None:
    with patch.object(
        bs.requests, "get", return_value=_mock_response(status_code=500)
    ):
        result = bs.fetch_aaii_sentiment()
    assert result is None


def test_fetch_aaii_sentiment_missing_labels_returns_none() -> None:
    """If the page has none of bullish/neutral/bearish, return None."""
    with patch.object(
        bs.requests,
        "get",
        return_value=_mock_response(text="<html><body>unrelated page</body></html>"),
    ):
        result = bs.fetch_aaii_sentiment()
    assert result is None


# ---------------------------------------------------------------------------
# NAAIM Exposure happy path
# ---------------------------------------------------------------------------


def test_fetch_naaim_exposure_happy_path() -> None:
    html = _fixture("naaim_sample.html")
    with patch.object(
        bs.requests, "get", return_value=_mock_response(text=html)
    ):
        result = bs.fetch_naaim_exposure()

    assert result is not None
    assert result["exposure"] == pytest.approx(65.30, abs=0.01)
    assert result["regime"] == "moderate"
    assert result["week_ending"] == "2026-05-14"
    # mean_4w from rows: 65.30, 72.10, 78.40, 72.60 -> 72.1
    assert result["mean_4w"] == pytest.approx((65.30 + 72.10 + 78.40 + 72.60) / 4, abs=0.01)
    # percentile_1y is computed from 6 history points; latest at 65.30
    # is the lowest -> percentile ~ 1/6
    assert result["percentile_1y"] is not None
    assert 0.0 < result["percentile_1y"] <= 1.0


def test_fetch_naaim_exposure_fetch_failure_returns_none() -> None:
    with patch.object(
        bs.requests, "get", return_value=_mock_response(status_code=500)
    ):
        result = bs.fetch_naaim_exposure()
    assert result is None


def test_fetch_naaim_exposure_empty_html_returns_none() -> None:
    with patch.object(
        bs.requests,
        "get",
        return_value=_mock_response(text="<html><body>nothing here</body></html>"),
    ):
        result = bs.fetch_naaim_exposure()
    assert result is None


# ---------------------------------------------------------------------------
# Top-level fetch_breadth_sentiment — orchestration + graceful degradation
# ---------------------------------------------------------------------------


def _all_sources_url_stub() -> Any:
    """Side effect that returns the right fixture for each URL."""
    cboe_html = _fixture("cboe_sample.html")
    aaii_html = _fixture("aaii_sample.html")
    naaim_html = _fixture("naaim_sample.html")

    def _stub(url: str, **_kwargs: Any) -> MagicMock:
        if url == bs.CBOE_DAILY_HTML:
            return _mock_response(text=cboe_html)
        if url == bs.AAII_SENTIMENT_URL:
            return _mock_response(text=aaii_html)
        if url == bs.NAAIM_EXPOSURE_URL:
            return _mock_response(text=naaim_html)
        return _mock_response(status_code=404)

    return _stub


def test_fetch_breadth_sentiment_all_sources_ok() -> None:
    with patch.object(bs.requests, "get", side_effect=_all_sources_url_stub()):
        result = bs.fetch_breadth_sentiment()

    assert result["as_of"] == _dt.date.today().isoformat()
    assert result["put_call"] is not None
    assert result["aaii"] is not None
    assert result["naaim"] is not None
    assert result["data_quality"]["sources_ok"] == 3
    assert result["data_quality"]["sources_failed"] == 0
    assert result["data_quality"]["failed_sources"] == []


def test_fetch_breadth_sentiment_one_source_fails() -> None:
    """If NAAIM 500s, the other two still come back and data_quality flags it."""
    cboe_html = _fixture("cboe_sample.html")
    aaii_html = _fixture("aaii_sample.html")

    def _stub(url: str, **_kwargs: Any) -> MagicMock:
        if url == bs.CBOE_DAILY_HTML:
            return _mock_response(text=cboe_html)
        if url == bs.AAII_SENTIMENT_URL:
            return _mock_response(text=aaii_html)
        if url == bs.NAAIM_EXPOSURE_URL:
            return _mock_response(status_code=500)
        return _mock_response(status_code=404)

    with patch.object(bs.requests, "get", side_effect=_stub):
        result = bs.fetch_breadth_sentiment()

    assert result["put_call"] is not None
    assert result["aaii"] is not None
    assert result["naaim"] is None
    assert result["data_quality"]["sources_ok"] == 2
    assert result["data_quality"]["sources_failed"] == 1
    assert result["data_quality"]["failed_sources"] == ["naaim"]


def test_fetch_breadth_sentiment_all_sources_fail() -> None:
    """Total upstream outage degrades to None across the board, does NOT raise."""
    with patch.object(
        bs.requests, "get", return_value=_mock_response(status_code=503)
    ):
        result = bs.fetch_breadth_sentiment()

    assert result["put_call"] is None
    assert result["aaii"] is None
    assert result["naaim"] is None
    assert result["data_quality"]["sources_ok"] == 0
    assert result["data_quality"]["sources_failed"] == 3
    assert set(result["data_quality"]["failed_sources"]) == {
        "put_call",
        "aaii",
        "naaim",
    }
    # Composite must still be a valid string, not a crash.
    assert isinstance(result["composite_regime"], str)


def test_fetch_breadth_sentiment_inner_exception_is_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If one source's inner code raises, the top-level call must still return."""

    def _boom(**_kw: Any) -> None:
        raise RuntimeError("internal explosion")

    monkeypatch.setattr(bs, "fetch_put_call", _boom)
    with patch.object(bs.requests, "get", return_value=_mock_response(status_code=404)):
        result = bs.fetch_breadth_sentiment()

    assert result["put_call"] is None
    assert "put_call" in result["data_quality"]["failed_sources"]


# ---------------------------------------------------------------------------
# Composite regime — signal combinations
# ---------------------------------------------------------------------------


def _src(regime: str) -> Dict[str, Any]:
    return {"regime": regime}


def test_composite_all_defensive_is_extreme_caution() -> None:
    assert (
        bs._composite_regime(
            _src("elevated_hedging"),
            _src("bearish"),
            _src("defensive"),
        )
        == "extreme_caution"
    )


def test_composite_one_defensive_is_cautious() -> None:
    assert (
        bs._composite_regime(
            _src("normal"),
            _src("bearish"),
            _src("moderate"),
        )
        == "cautious"
    )


def test_composite_all_euphoric_is_euphoric() -> None:
    assert (
        bs._composite_regime(
            _src("complacent"),
            _src("extreme_bullish"),
            _src("leveraged"),
        )
        == "euphoric"
    )


def test_composite_one_euphoric_is_complacent() -> None:
    assert (
        bs._composite_regime(
            _src("complacent"),
            _src("neutral"),
            _src("moderate"),
        )
        == "complacent"
    )


def test_composite_defensive_and_euphoric_is_mixed() -> None:
    assert (
        bs._composite_regime(
            _src("complacent"),
            _src("bearish"),
            _src("moderate"),
        )
        == "mixed"
    )


def test_composite_all_neutral_is_neutral() -> None:
    assert (
        bs._composite_regime(
            _src("normal"),
            _src("neutral"),
            _src("moderate"),
        )
        == "neutral"
    )


def test_composite_all_none_is_mixed() -> None:
    """If every source failed there's nothing to roll up; flag as mixed."""
    assert bs._composite_regime(None, None, None) == "mixed"


# ---------------------------------------------------------------------------
# Caching behavior
# ---------------------------------------------------------------------------


def test_fetch_put_call_caches_within_day() -> None:
    html_text = _fixture("cboe_sample.html")
    with patch.object(
        bs.requests, "get", return_value=_mock_response(text=html_text)
    ) as mg:
        a = bs.fetch_put_call()
        b = bs.fetch_put_call()

    assert a == b
    # First call hits HTTP exactly once (single HTML page, no fallback);
    # second is served from cache.
    assert mg.call_count == 1
    # Run a third call and confirm count didn't grow.
    with patch.object(
        bs.requests, "get", return_value=_mock_response(text=html_text)
    ) as mg2:
        c = bs.fetch_put_call()
    assert c == a
    assert mg2.call_count == 0


def test_fetch_aaii_caches_within_day() -> None:
    html = _fixture("aaii_sample.html")
    with patch.object(
        bs.requests, "get", return_value=_mock_response(text=html)
    ) as mg:
        a = bs.fetch_aaii_sentiment()
        b = bs.fetch_aaii_sentiment()

    assert a == b
    # First call hits HTTP once; second is served from cache.
    assert mg.call_count == 1


def test_fetch_naaim_caches_within_day() -> None:
    html = _fixture("naaim_sample.html")
    with patch.object(
        bs.requests, "get", return_value=_mock_response(text=html)
    ) as mg:
        a = bs.fetch_naaim_exposure()
        b = bs.fetch_naaim_exposure()

    assert a == b
    assert mg.call_count == 1


# ---------------------------------------------------------------------------
# Misc parser robustness
# ---------------------------------------------------------------------------


def test_parse_cboe_csv_handles_missing_header_returns_empty() -> None:
    """A CSV with no recognisable P/C header should return []."""
    rows = bs._parse_cboe_csv("date,volume\n2026-05-16,100\n")
    assert rows == []


def test_parse_aaii_html_extracts_three_percentages() -> None:
    snippet = (
        "<html><body>"
        "Week ending: May 14, 2026<br/>"
        "Bullish 28.5% Neutral 29.4% Bearish 42.1%"
        "</body></html>"
    )
    parsed = bs._parse_aaii_html(snippet)
    assert parsed is not None
    assert parsed["bullish_pct"] == 28.5
    assert parsed["bearish_pct"] == 42.1
    assert parsed["week_ending"] == "2026-05-14"
