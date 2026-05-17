"""Tests for lthcs.pillars.adoption.

All Google Trends / pytrends calls are mocked -- no live network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from lthcs.pillars import adoption
from lthcs.sources._cache import FileCache


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the trends cache at a per-test directory."""
    fresh = FileCache("google_trends", root=tmp_path)
    monkeypatch.setattr(adoption, "_cache", fresh)


@pytest.fixture(autouse=True)
def _fast_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the rate-limit bucket with one that always succeeds instantly."""
    fake = MagicMock()
    fake.acquire.return_value = True
    fake.try_acquire.return_value = True
    fake.wait_time.return_value = 0.0
    monkeypatch.setattr(adoption, "_bucket", fake)


def _annual(end_date: str, value: float, fy: int) -> Dict[str, Any]:
    return {
        "end_date": end_date,
        "value": value,
        "form": "10-K",
        "fy": fy,
        "fp": "FY",
    }


def _quarter(end_date: str, value: float, fy: int, fp: str) -> Dict[str, Any]:
    return {
        "end_date": end_date,
        "value": value,
        "form": "10-Q",
        "fy": fy,
        "fp": fp,
    }


# --- compute_revenue_growth_yoy --------------------------------------------


def test_revenue_growth_two_annuals() -> None:
    rows = [
        _annual("2025-09-30", 108_000_000.0, 2025),
        _annual("2024-09-30", 100_000_000.0, 2024),
        _annual("2023-09-30", 90_000_000.0, 2023),
    ]
    g = adoption.compute_revenue_growth_yoy(rows)
    assert g is not None
    assert g == pytest.approx(0.08, rel=1e-6)


def test_revenue_growth_picks_most_recent_pair() -> None:
    """Even if older annuals exist, only the latest two matter."""
    rows = [
        _annual("2025-12-31", 220.0, 2025),
        _annual("2024-12-31", 200.0, 2024),
        _annual("2023-12-31", 150.0, 2023),
        _annual("2022-12-31", 100.0, 2022),
    ]
    g = adoption.compute_revenue_growth_yoy(rows)
    assert g == pytest.approx(0.10, rel=1e-6)


def test_revenue_growth_ttm_fallback_when_only_quarterly() -> None:
    """Quarterly-only fixture -> TTM-vs-prior-TTM."""
    # Recent 4 quarters (descending end_date): sum = 440. Prior 4 sum = 400.
    # Expected growth = 0.10.
    rows = [
        _quarter("2026-03-31", 120.0, 2026, "Q1"),
        _quarter("2025-12-31", 115.0, 2025, "Q4"),
        _quarter("2025-09-30", 105.0, 2025, "Q3"),
        _quarter("2025-06-30", 100.0, 2025, "Q2"),
        _quarter("2025-03-31", 110.0, 2025, "Q1"),
        _quarter("2024-12-31", 105.0, 2024, "Q4"),
        _quarter("2024-09-30", 95.0, 2024, "Q3"),
        _quarter("2024-06-30", 90.0, 2024, "Q2"),
    ]
    g = adoption.compute_revenue_growth_yoy(rows)
    assert g is not None
    assert g == pytest.approx(0.10, rel=1e-6)


def test_revenue_growth_single_row_returns_none() -> None:
    rows = [_annual("2025-09-30", 100.0, 2025)]
    assert adoption.compute_revenue_growth_yoy(rows) is None


def test_revenue_growth_empty_returns_none() -> None:
    assert adoption.compute_revenue_growth_yoy([]) is None


def test_revenue_growth_quarterly_only_under_8_rows_returns_none() -> None:
    rows = [
        _quarter("2025-12-31", 100.0, 2025, "Q4"),
        _quarter("2025-09-30", 95.0, 2025, "Q3"),
        _quarter("2025-06-30", 90.0, 2025, "Q2"),
    ]
    assert adoption.compute_revenue_growth_yoy(rows) is None


def test_revenue_growth_outlier_rejected() -> None:
    """A 1500% jump (growth=15.0) is bad data -> None."""
    rows = [
        _annual("2025-09-30", 16_000.0, 2025),
        _annual("2024-09-30", 1_000.0, 2024),
    ]
    # (16000 - 1000) / 1000 = 15.0, outside [-1.0, 10.0].
    assert adoption.compute_revenue_growth_yoy(rows) is None


def test_revenue_growth_outlier_negative_rejected() -> None:
    """Below -100% is structurally impossible -> None (defends against bad signs)."""
    rows = [
        _annual("2025-09-30", -250.0, 2025),
        _annual("2024-09-30", 100.0, 2024),
    ]
    # growth = -3.5, below _GROWTH_MIN.
    assert adoption.compute_revenue_growth_yoy(rows) is None


def test_revenue_growth_zero_prior_returns_none() -> None:
    rows = [
        _annual("2025-09-30", 100.0, 2025),
        _annual("2024-09-30", 0.0, 2024),
    ]
    assert adoption.compute_revenue_growth_yoy(rows) is None


# --- compute_search_interest_slope -----------------------------------------


def test_search_interest_slope_basic() -> None:
    # Perfectly linear y = 2x -> slope = 2.
    series = [2.0, 4.0, 6.0, 8.0, 10.0]
    s = adoption.compute_search_interest_slope(series)
    assert s is not None
    assert s == pytest.approx(2.0, rel=1e-6)


def test_search_interest_slope_flat() -> None:
    s = adoption.compute_search_interest_slope([50.0] * 10)
    assert s is not None
    assert s == pytest.approx(0.0, abs=1e-9)


def test_search_interest_slope_empty() -> None:
    assert adoption.compute_search_interest_slope([]) is None


def test_search_interest_slope_single_point() -> None:
    assert adoption.compute_search_interest_slope([42.0]) is None


def test_search_interest_slope_delegates_to_normalize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the wrapper calls lthcs.normalize.slope, not a re-impl."""
    sentinel = MagicMock(return_value=0.1234)
    monkeypatch.setattr(adoption, "slope", sentinel)
    out = adoption.compute_search_interest_slope([1.0, 2.0, 3.0])
    sentinel.assert_called_once_with([1.0, 2.0, 3.0])
    assert out == 0.1234


# --- compute_adoption ------------------------------------------------------


def _peer_growths_universe(focal_ticker: str, focal_growth: float) -> Dict[str, float]:
    """Build a peer-growth map: focal ticker plus 9 peers spanning -10% to +30%."""
    peers = {
        "P1": -0.10,
        "P2": -0.05,
        "P3": 0.00,
        "P4": 0.02,
        "P5": 0.05,
        "P6": 0.08,
        "P7": 0.12,
        "P8": 0.18,
        "P9": 0.30,
    }
    peers[focal_ticker] = focal_growth
    return peers


def test_compute_adoption_both_components_present() -> None:
    """Verify weights and math when both revenue and trends are present."""
    rows = [
        _annual("2025-09-30", 110.0, 2025),
        _annual("2024-09-30", 100.0, 2024),
    ]
    peer_growths = _peer_growths_universe("AAPL", 0.10)
    # Rising trend: slope ~0.5 -> trends_subscore should be 100.
    interest = [float(i) for i in range(50, 100)]  # slope = 1.0, clamped to 100.

    result = adoption.compute_adoption("AAPL", rows, interest, peer_growths)

    assert result["ticker"] == "AAPL"
    assert result["weights"] == {"revenue": 0.60, "trends": 0.40}
    assert result["data_quality"] == {"has_revenue": True, "has_trends": True}

    comps = result["components"]
    assert comps["revenue_growth_yoy"] == pytest.approx(0.10, rel=1e-6)
    # 9 peer values; growth 0.10 ranks between P6 (0.08) and P7 (0.12).
    # 6 below, 0 equal, 3 above -> 6/9 * 100 = 66.6667.
    assert comps["revenue_subscore"] == pytest.approx(66.6667, abs=1e-3)
    assert comps["trends_subscore"] == pytest.approx(100.0)

    expected = round(0.60 * comps["revenue_subscore"] + 0.40 * 100.0, 1)
    assert result["sub_score"] == expected


def test_compute_adoption_revenue_only_trends_missing() -> None:
    """Empty trends list -> trends falls back to 50, revenue still drives."""
    rows = [
        _annual("2025-09-30", 200.0, 2025),
        _annual("2024-09-30", 100.0, 2024),
    ]
    peer_growths = _peer_growths_universe("AAPL", 1.0)  # focal at top of pack
    result = adoption.compute_adoption("AAPL", rows, [], peer_growths)

    assert result["data_quality"]["has_revenue"] is True
    assert result["data_quality"]["has_trends"] is False
    assert result["components"]["trends_slope"] is None
    assert result["components"]["trends_subscore"] == 50.0
    # Focal growth (1.0) is above all 9 peers (max 0.30) -> 100.
    assert result["components"]["revenue_subscore"] == pytest.approx(100.0)
    assert result["sub_score"] == pytest.approx(0.60 * 100.0 + 0.40 * 50.0)


def test_compute_adoption_trends_only_revenue_missing() -> None:
    """No revenue data -> revenue falls back to 50, trends still drives."""
    interest = [float(i) for i in range(50, 100)]  # slope = 1.0 -> 100.
    # peer_growths has values but the focal isn't computable from rows=[]
    peer_growths = {"P1": 0.05, "P2": 0.10}
    result = adoption.compute_adoption("AAPL", [], interest, peer_growths)

    assert result["data_quality"]["has_revenue"] is False
    assert result["data_quality"]["has_trends"] is True
    assert result["components"]["revenue_growth_yoy"] is None
    assert result["components"]["revenue_subscore"] == 50.0
    assert result["components"]["trends_subscore"] == pytest.approx(100.0)
    assert result["sub_score"] == pytest.approx(0.60 * 50.0 + 0.40 * 100.0)


def test_compute_adoption_both_missing_yields_50() -> None:
    """No revenue + no trends -> sub_score is exactly 50.0."""
    result = adoption.compute_adoption("AAPL", [], [], {})
    assert result["sub_score"] == 50.0
    assert result["data_quality"] == {"has_revenue": False, "has_trends": False}
    assert result["components"]["revenue_subscore"] == 50.0
    assert result["components"]["trends_subscore"] == 50.0


def test_compute_adoption_excludes_self_from_peer_distribution() -> None:
    """Focal ticker's own growth must not appear in the peer set used for ranking."""
    rows = [
        _annual("2025-09-30", 110.0, 2025),
        _annual("2024-09-30", 100.0, 2024),
    ]
    # Build a universe where the focal value would distort the rank
    # if it leaked in.
    peer_growths = {
        "AAPL": 0.10,  # focal -- should be excluded
        "P1": 0.10,
        "P2": 0.10,
        "P3": 0.10,
    }
    result = adoption.compute_adoption("AAPL", rows, [], peer_growths)
    # Against 3 peers all equal to 0.10: 0 below, 3 equal -> 50.
    assert result["components"]["revenue_subscore"] == pytest.approx(50.0)


def test_compute_adoption_ignores_none_peers() -> None:
    """None entries in peer_growths must be filtered before percentile-ranking."""
    rows = [
        _annual("2025-09-30", 110.0, 2025),
        _annual("2024-09-30", 100.0, 2024),
    ]
    peer_growths = {
        "AAPL": 0.10,
        "P1": None,
        "P2": None,
        "P3": 0.05,
        "P4": 0.08,
    }
    result = adoption.compute_adoption("AAPL", rows, [], peer_growths)
    # Cleaned peer dist (excl self, excl None): [0.05, 0.08]. growth=0.10
    # ranks above both -> 100.
    assert result["components"]["revenue_subscore"] == pytest.approx(100.0)


def test_compute_adoption_sub_score_rounded_to_one_decimal() -> None:
    """Spec requires the sub_score be rounded to 1 decimal place."""
    rows = [
        _annual("2025-09-30", 108.0, 2025),
        _annual("2024-09-30", 100.0, 2024),
    ]
    peer_growths = _peer_growths_universe("AAPL", 0.08)
    interest = [float(i) for i in range(0, 30)]  # rising
    result = adoption.compute_adoption("AAPL", rows, interest, peer_growths)
    # Sub-score should be a float rounded to 1 decimal.
    assert isinstance(result["sub_score"], float)
    assert result["sub_score"] == round(result["sub_score"], 1)


# --- fetch_google_trends_interest (mocked pytrends) ------------------------


def test_fetch_google_trends_interest_returns_floats() -> None:
    fake_df = pd.DataFrame(
        {
            "AAPL": [40, 42, 45, 48, 50],
            "isPartial": [False, False, False, False, False],
        },
        index=pd.date_range("2026-05-10", periods=5),
    )

    with patch("lthcs.pillars.adoption.TrendReq") as mock_trendreq:
        instance = mock_trendreq.return_value
        instance.build_payload.return_value = None
        instance.interest_over_time.return_value = fake_df
        result = adoption.fetch_google_trends_interest("AAPL", days=5)

    assert result == [40.0, 42.0, 45.0, 48.0, 50.0]
    assert all(isinstance(x, float) for x in result)
    mock_trendreq.assert_called_once()
    instance.build_payload.assert_called_once()
    args, kwargs = instance.build_payload.call_args
    # ticker is uppercased and passed as a single-element list.
    assert args[0] == ["AAPL"]
    # 5-day window propagated to the timeframe string.
    assert "5" in kwargs.get("timeframe", args[1] if len(args) > 1 else "")


def test_fetch_google_trends_interest_pytrends_exception_returns_empty() -> None:
    """A pytrends error must not propagate -- caller treats as missing signal."""
    with patch("lthcs.pillars.adoption.TrendReq") as mock_trendreq:
        instance = mock_trendreq.return_value
        instance.build_payload.side_effect = RuntimeError("429 blocked")
        result = adoption.fetch_google_trends_interest("AAPL", days=90)
    assert result == []


def test_fetch_google_trends_interest_empty_dataframe_returns_empty() -> None:
    empty_df = pd.DataFrame()
    with patch("lthcs.pillars.adoption.TrendReq") as mock_trendreq:
        instance = mock_trendreq.return_value
        instance.build_payload.return_value = None
        instance.interest_over_time.return_value = empty_df
        result = adoption.fetch_google_trends_interest("AAPL", days=90)
    assert result == []


def test_fetch_google_trends_interest_missing_column_returns_empty() -> None:
    """If Google returns a frame without our keyword column -> []."""
    fake_df = pd.DataFrame(
        {
            "OTHER": [1, 2, 3],
            "isPartial": [False, False, False],
        },
        index=pd.date_range("2026-05-10", periods=3),
    )
    with patch("lthcs.pillars.adoption.TrendReq") as mock_trendreq:
        instance = mock_trendreq.return_value
        instance.build_payload.return_value = None
        instance.interest_over_time.return_value = fake_df
        result = adoption.fetch_google_trends_interest("AAPL", days=3)
    assert result == []


def test_fetch_google_trends_interest_empty_ticker_returns_empty() -> None:
    assert adoption.fetch_google_trends_interest("", days=90) == []
    assert adoption.fetch_google_trends_interest("   ", days=90) == []


def test_fetch_google_trends_interest_uses_cache_on_second_call() -> None:
    """Second call with same args must hit the cache, not pytrends."""
    fake_df = pd.DataFrame(
        {
            "AAPL": [10, 20, 30],
            "isPartial": [False, False, False],
        },
        index=pd.date_range("2026-05-10", periods=3),
    )

    with patch("lthcs.pillars.adoption.TrendReq") as mock_trendreq:
        instance = mock_trendreq.return_value
        instance.build_payload.return_value = None
        instance.interest_over_time.return_value = fake_df

        first = adoption.fetch_google_trends_interest("AAPL", days=3)
        second = adoption.fetch_google_trends_interest("AAPL", days=3)

    assert first == [10.0, 20.0, 30.0]
    assert second == [10.0, 20.0, 30.0]
    # Network only hit once thanks to the FileCache.
    assert mock_trendreq.call_count == 1


def test_fetch_google_trends_interest_acquires_rate_limit_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Confirm the polite TokenBucket is consulted before each live fetch."""
    bucket = MagicMock()
    bucket.acquire.return_value = True
    monkeypatch.setattr(adoption, "_bucket", bucket)

    fake_df = pd.DataFrame(
        {"AAPL": [1, 2], "isPartial": [False, False]},
        index=pd.date_range("2026-05-10", periods=2),
    )
    with patch("lthcs.pillars.adoption.TrendReq") as mock_trendreq:
        instance = mock_trendreq.return_value
        instance.build_payload.return_value = None
        instance.interest_over_time.return_value = fake_df
        adoption.fetch_google_trends_interest("AAPL", days=2)

    bucket.acquire.assert_called_once()


def test_fetch_google_trends_interest_skips_when_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the bucket times out, return [] without calling pytrends."""
    bucket = MagicMock()
    bucket.acquire.return_value = False  # never get a token
    monkeypatch.setattr(adoption, "_bucket", bucket)

    with patch("lthcs.pillars.adoption.TrendReq") as mock_trendreq:
        result = adoption.fetch_google_trends_interest("AAPL", days=2)

    assert result == []
    mock_trendreq.assert_not_called()
