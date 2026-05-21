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
    # The "weights" dict carries the documented configuration, including
    # the QoQ slot (0.0 here since legacy interest_series path doesn't
    # activate the three-component branch).
    assert result["weights"] == {"revenue": 0.60, "trends": 0.40, "qoq": 0.0}
    # has_qoq=False here because two annual rows alone give no quarterly
    # series, so compute_revenue_growth_qoq returns None.
    assert result["data_quality"] == {
        "has_revenue": True, "has_trends": True, "has_qoq": False,
    }

    comps = result["components"]
    assert comps["revenue_growth_yoy"] == pytest.approx(0.10, rel=1e-6)
    # 9 peer values; growth 0.10 ranks between P6 (0.08) and P7 (0.12).
    # 6 below, 0 equal, 3 above -> 6/9 * 100 = 66.6667.
    assert comps["revenue_subscore"] == pytest.approx(66.6667, abs=1e-3)
    assert comps["trends_subscore"] == pytest.approx(100.0)

    expected = round(0.60 * comps["revenue_subscore"] + 0.40 * 100.0, 1)
    assert result["sub_score"] == expected


def test_compute_adoption_revenue_only_trends_missing() -> None:
    """Empty trends list -> Adoption RENORMALIZES so revenue carries 100%.

    Per the 13F-stub handling pattern in the Institutional pillar:
    when Trends data isn't available (V1 reality on free-tier pytrends),
    the pillar reweights to revenue=1.0/trends=0.0 rather than
    diluting toward the neutral-50 placeholder. Without this renorm,
    the Adoption pillar would mechanically cap at 80 and prevent any
    composite from reaching Elite (>=90).
    """
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
    # RENORMALIZED: revenue carries 100% when trends is unavailable.
    assert result["effective_weights"]["revenue"] == 1.0
    assert result["effective_weights"]["trends"] == 0.0
    assert result["sub_score"] == pytest.approx(100.0)


def test_compute_adoption_trends_only_revenue_missing() -> None:
    """No revenue data + Trends present -> documented 60/40 weighting kicks in.

    Asymmetric to the renorm path above: when Trends IS present, it has
    real signal and gets its documented 40% weight. Revenue falls back
    to neutral 50 (since we genuinely don't know the growth)."""
    interest = [float(i) for i in range(50, 100)]  # slope = 1.0 -> 100.
    # peer_growths has values but the focal isn't computable from rows=[]
    peer_growths = {"P1": 0.05, "P2": 0.10}
    result = adoption.compute_adoption("AAPL", [], interest, peer_growths)

    assert result["data_quality"]["has_revenue"] is False
    assert result["data_quality"]["has_trends"] is True
    assert result["components"]["revenue_growth_yoy"] is None
    assert result["components"]["revenue_subscore"] == 50.0
    assert result["components"]["trends_subscore"] == pytest.approx(100.0)
    # Documented weights apply (Trends is real signal; no renorm).
    assert result["effective_weights"]["revenue"] == 0.60
    assert result["effective_weights"]["trends"] == 0.40
    assert result["sub_score"] == pytest.approx(0.60 * 50.0 + 0.40 * 100.0)


def test_compute_adoption_both_missing_yields_50() -> None:
    """No revenue + no trends -> sub_score is exactly 50.0."""
    result = adoption.compute_adoption("AAPL", [], [], {})
    assert result["sub_score"] == 50.0
    assert result["data_quality"] == {
        "has_revenue": False, "has_trends": False, "has_qoq": False,
    }
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


# --- compute_adoption with trends_data (Phase 2 weekly-batch path) --------


def _trends_blob(
    ticker: str = "NVDA",
    *,
    acceleration_4w_pct: float = 25.0,
    acceleration_12w_pct: float = 60.0,
    regime: str = "accelerating",
    signal_score: float = 0.6,
    data_quality: str = "good",
    trend_week: str = "2026-W19",
) -> Dict[str, Any]:
    return {
        "ticker": ticker,
        "as_of": "2026-05-17",
        "trend_week": trend_week,
        "search_interest_latest": 78,
        "search_interest_4w_ago": 62,
        "search_interest_12w_ago": 45,
        "acceleration_4w_pct": acceleration_4w_pct,
        "acceleration_12w_pct": acceleration_12w_pct,
        "regime": regime,
        "signal_score": signal_score,
        "data_quality": data_quality,
    }


def test_compute_adoption_with_trends_data_uses_70_30_weights() -> None:
    """When trends_data is supplied, revenue=70%, trends=30%."""
    rows = [
        _annual("2025-09-30", 110.0, 2025),
        _annual("2024-09-30", 100.0, 2024),
    ]
    peer_growths = _peer_growths_universe("NVDA", 0.10)
    trends = _trends_blob("NVDA", acceleration_4w_pct=25.0)
    universe_trends = {
        "NVDA": trends,
        "P1": _trends_blob("P1", acceleration_4w_pct=-10.0),
        "P2": _trends_blob("P2", acceleration_4w_pct=0.0),
        "P3": _trends_blob("P3", acceleration_4w_pct=10.0),
        "P4": _trends_blob("P4", acceleration_4w_pct=15.0),
        "P5": _trends_blob("P5", acceleration_4w_pct=40.0),
    }
    result = adoption.compute_adoption(
        "NVDA", rows, [], peer_growths,
        trends_data=trends, universe_trends_data=universe_trends,
    )
    assert result["data_quality"]["has_trends"] is True
    assert result["effective_weights"]["revenue"] == pytest.approx(0.70)
    assert result["effective_weights"]["trends"] == pytest.approx(0.30)
    # NVDA's 25.0 ranks above P1/P2/P3/P4 (4 of 5 peers) -> 80.0.
    assert result["components"]["trends_subscore"] == pytest.approx(80.0)
    # variable_detail surfaces the trends sub-block.
    detail = result["variable_detail"]
    assert "trends" in detail
    assert detail["trends"]["regime"] == "accelerating"
    assert detail["trends"]["acceleration_4w_pct"] == 25.0
    assert detail["trends"]["quality"] == "good"


def test_compute_adoption_with_stale_trends_drops_to_85_15() -> None:
    rows = [
        _annual("2025-09-30", 110.0, 2025),
        _annual("2024-09-30", 100.0, 2024),
    ]
    peer_growths = _peer_growths_universe("NVDA", 0.10)
    trends = _trends_blob("NVDA", data_quality="stale")
    result = adoption.compute_adoption(
        "NVDA", rows, [], peer_growths,
        trends_data=trends, universe_trends_data={"NVDA": trends},
    )
    assert result["data_quality"]["has_trends"] is True
    assert result["effective_weights"]["revenue"] == pytest.approx(0.85)
    assert result["effective_weights"]["trends"] == pytest.approx(0.15)
    assert result["variable_detail"]["trends"]["quality"] == "stale"


def test_compute_adoption_with_trends_data_none_preserves_legacy() -> None:
    """trends_data=None falls back to legacy interest_series path (revenue 100% renorm)."""
    rows = [
        _annual("2025-09-30", 200.0, 2025),
        _annual("2024-09-30", 100.0, 2024),
    ]
    peer_growths = _peer_growths_universe("AAPL", 1.0)
    result = adoption.compute_adoption(
        "AAPL", rows, [], peer_growths, trends_data=None,
    )
    # Same as the existing test_compute_adoption_revenue_only_trends_missing —
    # revenue carries the full pillar weight.
    assert result["effective_weights"]["revenue"] == 1.0
    assert result["effective_weights"]["trends"] == 0.0
    assert result["data_quality"]["has_trends"] is False


def test_compute_adoption_with_trends_data_missing_quality_treated_as_no_signal() -> None:
    """data_quality='missing' (or unknown) -> trends ignored, revenue carries 100%."""
    rows = [
        _annual("2025-09-30", 200.0, 2025),
        _annual("2024-09-30", 100.0, 2024),
    ]
    peer_growths = _peer_growths_universe("AAPL", 0.5)
    trends = _trends_blob("AAPL", data_quality="missing")
    result = adoption.compute_adoption(
        "AAPL", rows, [], peer_growths,
        trends_data=trends, universe_trends_data={},
    )
    assert result["data_quality"]["has_trends"] is False
    assert result["effective_weights"]["revenue"] == 1.0
    assert result["effective_weights"]["trends"] == 0.0


def test_compute_adoption_with_trends_data_partial_quality_uses_70_30() -> None:
    """data_quality='partial' is still real signal -> standard 70/30 split."""
    rows = [
        _annual("2025-09-30", 110.0, 2025),
        _annual("2024-09-30", 100.0, 2024),
    ]
    peer_growths = _peer_growths_universe("NVDA", 0.10)
    trends = _trends_blob("NVDA", data_quality="partial", acceleration_12w_pct=None)
    result = adoption.compute_adoption(
        "NVDA", rows, [], peer_growths,
        trends_data=trends, universe_trends_data={"NVDA": trends},
    )
    assert result["data_quality"]["has_trends"] is True
    assert result["effective_weights"]["revenue"] == pytest.approx(0.70)
    assert result["effective_weights"]["trends"] == pytest.approx(0.30)


def test_compute_adoption_with_trends_data_no_universe_falls_back_to_signal_score() -> None:
    """Empty universe -> trends_subscore derived from signal_score directly (50 + 50*ss)."""
    rows = [
        _annual("2025-09-30", 110.0, 2025),
        _annual("2024-09-30", 100.0, 2024),
    ]
    peer_growths = _peer_growths_universe("NVDA", 0.10)
    trends = _trends_blob("NVDA", signal_score=0.5)
    result = adoption.compute_adoption(
        "NVDA", rows, [], peer_growths,
        trends_data=trends, universe_trends_data=None,
    )
    # 50 + 50*0.5 = 75
    assert result["components"]["trends_subscore"] == pytest.approx(75.0)


def test_compute_adoption_has_trends_flag_with_trends_data() -> None:
    """has_trends should reflect that the new trends_data path supplied signal."""
    rows: List[Dict[str, Any]] = []
    trends = _trends_blob("NVDA")
    result = adoption.compute_adoption(
        "NVDA", rows, [], {},
        trends_data=trends, universe_trends_data={"NVDA": trends},
    )
    assert result["data_quality"]["has_trends"] is True
    # Revenue has no rows -> has_revenue=False; but trends still carries
    # its weight component, so subscore is not 50 exactly.
    assert result["data_quality"]["has_revenue"] is False


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


# --- compute_adoption with compound peer-key (Tier 2 #7) -------------------


def _peer_groups_config_two_groups() -> Dict[str, Any]:
    """Minimal peer_groups config with two sector_groups for cohort tests."""
    return {
        "min_cohort_size": 3,
        "sector_groups": {
            "group_a": {
                "tickers": ["AAPL", "P1", "P2", "P3"],
            },
            "group_b": {
                "tickers": ["P4", "P5", "P6", "P7", "P8", "P9"],
            },
        },
    }


def _synthetic_universe_two_groups() -> Dict[str, Any]:
    """Universe matching the two-group config above. All mature_compounder."""
    return {
        "tickers": [
            {"ticker": "AAPL", "maturity_stage": "mature_compounder", "active": True},
            {"ticker": "P1", "maturity_stage": "mature_compounder", "active": True},
            {"ticker": "P2", "maturity_stage": "mature_compounder", "active": True},
            {"ticker": "P3", "maturity_stage": "mature_compounder", "active": True},
            {"ticker": "P4", "maturity_stage": "mature_compounder", "active": True},
            {"ticker": "P5", "maturity_stage": "mature_compounder", "active": True},
            {"ticker": "P6", "maturity_stage": "mature_compounder", "active": True},
            {"ticker": "P7", "maturity_stage": "mature_compounder", "active": True},
            {"ticker": "P8", "maturity_stage": "mature_compounder", "active": True},
            {"ticker": "P9", "maturity_stage": "mature_compounder", "active": True},
        ]
    }


def test_compute_adoption_peer_groups_config_none_preserves_legacy() -> None:
    """When peer_groups_config=None, behaviour is identical to legacy path."""
    rows = [
        _annual("2025-09-30", 110.0, 2025),
        _annual("2024-09-30", 100.0, 2024),
    ]
    peer_growths = _peer_growths_universe("AAPL", 0.10)
    result = adoption.compute_adoption("AAPL", rows, [], peer_growths)
    # Legacy path: 9 peers, 6 below 0.10, 3 above -> 66.6667.
    assert result["components"]["revenue_subscore"] == pytest.approx(66.6667, abs=1e-3)
    assert result["components"]["peer_cohort_strategy"] == "maturity_only"
    # No peer_cohort_size when config is None.
    assert "peer_cohort_size" not in result["components"]


def test_compute_adoption_peer_groups_config_restricts_to_compound_cohort() -> None:
    """When peer_groups_config provided, percentile is computed in the compound cohort."""
    rows = [
        _annual("2025-09-30", 110.0, 2025),  # growth 10%
        _annual("2024-09-30", 100.0, 2024),
    ]
    # P1..P3 are in group_a with focal AAPL; their growths are low (-10, -5, 0).
    # P4..P9 are in group_b with high growths (5, 8, 12, 18, 25, 30).
    peer_growths = {
        "AAPL": 0.10,
        "P1": -0.10,
        "P2": -0.05,
        "P3": 0.00,
        "P4": 0.05,
        "P5": 0.08,
        "P6": 0.12,
        "P7": 0.18,
        "P8": 0.25,
        "P9": 0.30,
    }
    result = adoption.compute_adoption(
        "AAPL",
        rows,
        [],
        peer_growths,
        peer_groups_config=_peer_groups_config_two_groups(),
        universe=_synthetic_universe_two_groups(),
    )
    # Compound cohort = group_a = {AAPL, P1, P2, P3}. AAPL excluded -> peers
    # = [-0.10, -0.05, 0.00]. growth 0.10 ranks above all three -> 100.
    assert result["components"]["revenue_subscore"] == pytest.approx(100.0)
    assert result["components"]["peer_cohort_strategy"] == "compound"
    assert result["components"]["peer_cohort_size"] == 4  # AAPL + 3 peers


def test_compute_adoption_aapl_compound_vs_stage_only_delta() -> None:
    """AAPL spot-check: percentile changes when going from stage-only to compound.

    With AAPL in group_a (small, low-growth peers), it should score MUCH
    higher (100) than against the broader 9-name "stage-only" cohort that
    includes high-growth names like P7-P9 (~66).
    """
    rows = [
        _annual("2025-09-30", 110.0, 2025),  # growth 10%
        _annual("2024-09-30", 100.0, 2024),
    ]
    peer_growths = {
        "AAPL": 0.10,
        "P1": -0.10,
        "P2": -0.05,
        "P3": 0.00,
        "P4": 0.05,
        "P5": 0.08,
        "P6": 0.12,
        "P7": 0.18,
        "P8": 0.25,
        "P9": 0.30,
    }
    # Stage-only (legacy) score:
    legacy = adoption.compute_adoption("AAPL", rows, [], peer_growths)
    # Compound score:
    compound = adoption.compute_adoption(
        "AAPL",
        rows,
        [],
        peer_growths,
        peer_groups_config=_peer_groups_config_two_groups(),
        universe=_synthetic_universe_two_groups(),
    )
    legacy_rev = legacy["components"]["revenue_subscore"]
    compound_rev = compound["components"]["revenue_subscore"]
    # Compound should be strictly higher when the focal's peer group has
    # weaker peers than the universe — that's the whole point.
    assert compound_rev > legacy_rev
    assert compound_rev == pytest.approx(100.0)


def test_compute_adoption_peer_groups_safety_valve_falls_back() -> None:
    """A focal with too-small compound cohort should fall back, surfacing
    a non-'compound' strategy in variable_detail."""
    rows = [
        _annual("2025-09-30", 110.0, 2025),
        _annual("2024-09-30", 100.0, 2024),
    ]
    # Tiny cohort: AAPL alone in its sector_group with a high min floor.
    peer_growths = {
        "AAPL": 0.10,
        "P1": 0.05,
        "P2": 0.08,
        "P3": 0.12,
        "P4": 0.20,
        "P5": 0.30,
    }
    syn_config = {
        "min_cohort_size": 10,  # force universe fallback
        "sector_groups": {
            "tiny": {"tickers": ["AAPL"]},
            "other_grp": {"tickers": ["P1", "P2", "P3", "P4", "P5"]},
        },
    }
    syn_universe = {
        "tickers": [
            {"ticker": "AAPL", "maturity_stage": "mature_compounder", "active": True},
            {"ticker": "P1", "maturity_stage": "mature_compounder", "active": True},
            {"ticker": "P2", "maturity_stage": "mature_compounder", "active": True},
            {"ticker": "P3", "maturity_stage": "mature_compounder", "active": True},
            {"ticker": "P4", "maturity_stage": "mature_compounder", "active": True},
            {"ticker": "P5", "maturity_stage": "mature_compounder", "active": True},
        ]
    }
    result = adoption.compute_adoption(
        "AAPL",
        rows,
        [],
        peer_growths,
        peer_groups_config=syn_config,
        universe=syn_universe,
    )
    # Compound: 1 -> too small. sector_group_only: 1 -> too small.
    # maturity_only: 6 -> too small (floor=10). universe_fallback fires.
    assert result["components"]["peer_cohort_strategy"] == "universe_fallback"


# --- compute_revenue_growth_qoq (audit-fix 2026-05-18) ---------------------


def test_compute_revenue_growth_qoq_basic() -> None:
    """Sequential QoQ from two consecutive quarterly rows."""
    rows = [
        _quarter("2026-03-31", 110.0, 2026, "Q1"),
        _quarter("2025-12-31", 100.0, 2025, "Q4"),
    ]
    assert adoption.compute_revenue_growth_qoq(rows) == pytest.approx(0.10, rel=1e-6)


def test_compute_revenue_growth_qoq_picks_most_recent() -> None:
    """If many quarters are present, only the most-recent pair matters."""
    rows = [
        _quarter("2026-03-31", 120.0, 2026, "Q1"),
        _quarter("2025-12-31", 100.0, 2025, "Q4"),
        _quarter("2025-09-30", 80.0, 2025, "Q3"),
        _quarter("2025-06-30", 70.0, 2025, "Q2"),
    ]
    assert adoption.compute_revenue_growth_qoq(rows) == pytest.approx(0.20, rel=1e-6)


def test_compute_revenue_growth_qoq_single_quarter_returns_none() -> None:
    rows = [_quarter("2025-12-31", 100.0, 2025, "Q4")]
    assert adoption.compute_revenue_growth_qoq(rows) is None


def test_compute_revenue_growth_qoq_outlier_rejected() -> None:
    """A 5x quarterly jump (growth=4.0) is data noise -> None."""
    rows = [
        _quarter("2026-03-31", 600.0, 2026, "Q1"),
        _quarter("2025-12-31", 100.0, 2025, "Q4"),
    ]
    assert adoption.compute_revenue_growth_qoq(rows) is None


def test_compute_revenue_growth_qoq_no_quarterly_returns_none() -> None:
    """Annual-only rows -> no QoQ signal."""
    rows = [
        _annual("2025-09-30", 110.0, 2025),
        _annual("2024-09-30", 100.0, 2024),
    ]
    assert adoption.compute_revenue_growth_qoq(rows) is None


def test_compute_revenue_growth_qoq_zero_prior_returns_none() -> None:
    rows = [
        _quarter("2026-03-31", 100.0, 2026, "Q1"),
        _quarter("2025-12-31", 0.0, 2025, "Q4"),
    ]
    assert adoption.compute_revenue_growth_qoq(rows) is None


# --- Adoption: sector-relative revenue percentile --------------------------


def test_compute_adoption_uses_sector_cohort_when_large_enough() -> None:
    """Non-bank with a sector of >=_MIN_SECTOR_COHORT members ranks against
    sector peers only.
    """
    # 20-member Tech sector + a Financials decoy. Focal AAPL revenue +10%
    # ranks LOW within Tech (where peers grow 30-100%) but HIGH within the
    # full universe.
    rows = [
        _annual("2025-09-30", 110.0, 2025),
        _annual("2024-09-30", 100.0, 2024),
    ]
    tech_growths = {
        "AAPL": 0.10,
        "T1": 0.30, "T2": 0.32, "T3": 0.34, "T4": 0.36, "T5": 0.38,
        "T6": 0.40, "T7": 0.45, "T8": 0.50, "T9": 0.55, "T10": 0.60,
        "T11": 0.62, "T12": 0.64, "T13": 0.66, "T14": 0.68, "T15": 0.70,
        "T16": 0.72, "T17": 0.74, "T18": 0.76, "T19": 1.00,
    }
    peer_growths = {
        **tech_growths,
        "F1": -0.10, "F2": -0.05, "F3": 0.02, "F4": 0.03,
    }
    peer_sectors = {
        **{sym: "Tech" for sym in tech_growths},
        "F1": "Financials", "F2": "Financials",
        "F3": "Financials", "F4": "Financials",
    }
    result = adoption.compute_adoption(
        "AAPL", rows, [], peer_growths,
        sector="Tech", peer_sectors=peer_sectors,
    )
    detail = result["components"]
    assert detail["peer_cohort_strategy"] == "sector_relative"
    assert detail["sector_cohort"] == "Tech"
    assert detail["sector_cohort_size"] == 20
    # AAPL (+10%) is below all 19 other Tech peers in the sector. Raw
    # percentile pins at 0.0; tie-softening pulls it to the floor
    # constant (_SECTOR_RANK_FLOOR = 10.0).
    assert detail["revenue_subscore"] == pytest.approx(adoption._SECTOR_RANK_FLOOR)


def test_compute_adoption_skips_sector_for_small_sector() -> None:
    """A sector with <_MIN_SECTOR_COHORT members falls back to universe rank."""
    rows = [
        _annual("2025-09-30", 110.0, 2025),
        _annual("2024-09-30", 100.0, 2024),
    ]
    # Energy: 3 members -> below floor of 20. Universe path uses the
    # full peer_growths distribution (excluding focal).
    peer_growths = {
        "XOM": 0.10, "E1": 0.05, "E2": 0.20,
        "T1": -0.05, "T2": 0.00, "T3": 0.02, "T4": 0.04,
        "T5": 0.06, "T6": 0.08, "T7": 0.15, "T8": 0.25, "T9": 0.40,
    }
    peer_sectors = {
        "XOM": "Energy", "E1": "Energy", "E2": "Energy",
        "T1": "Tech", "T2": "Tech", "T3": "Tech", "T4": "Tech",
        "T5": "Tech", "T6": "Tech", "T7": "Tech", "T8": "Tech",
        "T9": "Tech",
    }
    result = adoption.compute_adoption(
        "XOM", rows, [], peer_growths,
        sector="Energy", peer_sectors=peer_sectors,
    )
    # Energy is too small -> sector_cohort not surfaced -> falls back to legacy
    # universe path.
    assert "sector_cohort" not in result["components"]
    assert result["components"]["peer_cohort_strategy"] != "sector_relative"


def test_compute_adoption_skips_sector_for_bank() -> None:
    """Bank tickers preserve the existing maturity-only / compound cohort
    behaviour even if peer_sectors is supplied -- the Financial pillar's
    bank decomposition owns bank-cohort logic.
    """
    rows = [
        _annual("2025-09-30", 110.0, 2025),
        _annual("2024-09-30", 100.0, 2024),
    ]
    # Big Financials cohort that would be valid for non-banks.
    peer_growths = {
        "JPM": 0.10, "BAC": 0.12, "WFC": 0.08, "C": 0.05, "GS": 0.15,
        "MS": 0.20, "USB": 0.07, "TFC": 0.04, "P9": 0.30,
    }
    peer_sectors = {sym: "Financials" for sym in peer_growths}
    result = adoption.compute_adoption(
        "JPM", rows, [], peer_growths,
        sector="Financials", peer_sectors=peer_sectors,
    )
    # JPM is in BANK_TICKERS -> sector_cohort path skipped.
    assert "sector_cohort" not in result["components"]
    assert result["components"]["peer_cohort_strategy"] != "sector_relative"


# --- Adoption: 2026-05-19 β follow-up (sector-cohort recalibration) --------


def test_min_sector_cohort_pinned_at_20() -> None:
    """Lock the cohort floor at 20.

    β follow-up: smaller cohorts (Consumer Staples n=14, Comm Svcs n=14)
    produce coarse 7+pt rank steps that pin three different YoY growth
    rates at the same 0.0 rev_subscore. Floor stays at 20 until the next
    re-validation.
    """
    assert adoption._MIN_SECTOR_COHORT == 20


def test_compute_adoption_falls_back_when_sector_below_new_floor() -> None:
    """A 14-member cohort (Consumer Staples-style) no longer qualifies.

    Mirrors the 2026-05-18 Adoption pillar inversion case: 14 staples
    were getting sector-relative scoring with a 7.14-pt step rank — the
    smoking gun in `docs/adoption-pillar-inversion-2026-05-19.md`.
    Should now fall back to universe-relative.
    """
    rows = [
        _annual("2025-09-30", 103.0, 2025),
        _annual("2024-09-30", 100.0, 2024),
    ]
    # 14-member staples cohort (was previously >=8, so sector_relative).
    staples = {
        "PG": 0.003,  # focal: +0.3% YoY
        "S1": 0.058, "S2": 0.045, "S3": 0.062, "S4": 0.070,
        "S5": 0.030, "S6": 0.040, "S7": 0.050, "S8": 0.020,
        "S9": -0.031, "S10": -0.001, "S11": 0.015, "S12": 0.025,
        "S13": 0.035,
    }
    peer_growths = {
        **staples,
        # Universe filler so the universe-relative path still has mass.
        **{f"U{i}": 0.10 + i * 0.02 for i in range(10)},
    }
    peer_sectors = {
        **{sym: "Consumer Staples" for sym in staples},
        **{f"U{i}": "Other" for i in range(10)},
    }
    result = adoption.compute_adoption(
        "PG", rows, [], peer_growths,
        sector="Consumer Staples", peer_sectors=peer_sectors,
    )
    detail = result["components"]
    # 14 < 20 -> sector path skipped.
    assert "sector_cohort" not in detail
    assert detail["peer_cohort_strategy"] != "sector_relative"


def test_soften_rank_extremes_helper_maps_boundaries() -> None:
    """Direct unit test of `_soften_rank_extremes`: 0.0 -> floor, 100.0 ->
    ceiling, interior pass-through, NaN preserved.
    """
    import math

    assert adoption._soften_rank_extremes(0.0) == adoption._SECTOR_RANK_FLOOR
    assert adoption._soften_rank_extremes(100.0) == adoption._SECTOR_RANK_CEILING
    # Sub-floor / above-ceiling inputs (defensive against drift) get pulled in too.
    assert adoption._soften_rank_extremes(-5.0) == adoption._SECTOR_RANK_FLOOR
    assert adoption._soften_rank_extremes(105.0) == adoption._SECTOR_RANK_CEILING
    # Interior scores untouched.
    assert adoption._soften_rank_extremes(25.0) == pytest.approx(25.0)
    assert adoption._soften_rank_extremes(50.0) == pytest.approx(50.0)
    assert adoption._soften_rank_extremes(99.0) == pytest.approx(99.0)
    # NaN preserved.
    out = adoption._soften_rank_extremes(float("nan"))
    assert math.isnan(out)


def test_compute_adoption_softens_cohort_floor_pin() -> None:
    """Focal below every sector peer no longer pins at 0.0 — it lands at
    `_SECTOR_RANK_FLOOR`. Mirrors PG +0.3% (sector min) post-fix.
    """
    rows = [
        _annual("2025-09-30", 110.0, 2025),
        _annual("2024-09-30", 100.0, 2024),
    ]
    # 20 Tech peers; focal AAPL at +10% is the cohort min.
    tech_growths = {
        "AAPL": 0.10,
        **{f"T{i}": 0.30 + 0.02 * i for i in range(1, 20)},
    }
    peer_sectors = {sym: "Tech" for sym in tech_growths}
    result = adoption.compute_adoption(
        "AAPL", rows, [], tech_growths,
        sector="Tech", peer_sectors=peer_sectors,
    )
    detail = result["components"]
    assert detail["peer_cohort_strategy"] == "sector_relative"
    # Pre-fix this would be 0.0; post-fix the floor pin softens to 10.0.
    assert detail["revenue_subscore"] == pytest.approx(adoption._SECTOR_RANK_FLOOR)
    assert detail["revenue_subscore"] > 0.0


def test_compute_adoption_softens_cohort_ceiling_pin() -> None:
    """Focal above every sector peer no longer pins at 100.0 — it lands at
    `_SECTOR_RANK_CEILING`. NVDA-style cohort ceiling case.
    """
    rows = [
        _annual("2025-09-30", 200.0, 2025),  # +100% YoY (cohort max).
        _annual("2024-09-30", 100.0, 2024),
    ]
    tech_growths = {
        "NVDA": 1.00,
        **{f"T{i}": 0.10 + 0.02 * i for i in range(1, 20)},
    }
    peer_sectors = {sym: "Tech" for sym in tech_growths}
    result = adoption.compute_adoption(
        "NVDA", rows, [], tech_growths,
        sector="Tech", peer_sectors=peer_sectors,
    )
    detail = result["components"]
    assert detail["peer_cohort_strategy"] == "sector_relative"
    # Pre-fix this would be 100.0; post-fix ceiling pin softens to 90.0.
    assert detail["revenue_subscore"] == pytest.approx(adoption._SECTOR_RANK_CEILING)
    assert detail["revenue_subscore"] < 100.0


def test_compute_adoption_softening_only_applies_to_sector_path() -> None:
    """Universe-relative / compound-cohort path keeps raw 0.0 / 100.0
    boundary behaviour. The β analysis flagged the pin as a sector-cohort
    artifact; the larger universe distribution doesn't need softening.
    """
    rows = [
        _annual("2025-09-30", 90.0, 2025),  # -10% YoY: below all peers.
        _annual("2024-09-30", 100.0, 2024),
    ]
    # No peer_sectors supplied -> sector path doesn't fire at all.
    peer_growths = {f"P{i}": 0.05 + 0.01 * i for i in range(20)}
    result = adoption.compute_adoption(
        "Z", rows, [], peer_growths,
    )
    detail = result["components"]
    assert detail.get("peer_cohort_strategy") != "sector_relative"
    # Raw 0.0 (not 10.0) — universe path is left alone.
    assert detail["revenue_subscore"] == pytest.approx(0.0)


def test_compute_adoption_softening_breaks_intra_floor_ties() -> None:
    """Two tickers both below the cohort min should land at the same
    softened floor — but the *score itself* (10.0) is no longer the same
    as a "barely positive" focal that lands strictly inside the cohort.
    PG (+0.3%) vs MO (-3.1%) get the same softened rank only if they
    both clear the cohort min; the doc's intent is that the *floor pin
    magnitude* shrinks so downstream pillar averaging stops snapping the
    tails. Confirm a focal *strictly inside* the cohort beats the floor.
    """
    rows_pg = [_annual("2025-09-30", 100.3, 2025), _annual("2024-09-30", 100.0, 2024)]
    rows_mo = [_annual("2025-09-30", 96.9, 2025), _annual("2024-09-30", 100.0, 2024)]

    # 20-member cohort with a long below-PG tail so PG lands strictly
    # interior (above MO and 4 others); MO is at the cohort floor.
    staples = {
        "PG": 0.003, "MO": -0.031,
        "S1": -0.030, "S2": -0.020, "S3": -0.015, "S4": -0.010,
        "S5": 0.010, "S6": 0.020, "S7": 0.030, "S8": 0.040,
        "S9": 0.050, "S10": 0.060, "S11": 0.070, "S12": 0.080,
        "S13": 0.090, "S14": 0.100, "S15": 0.110, "S16": 0.120,
        "S17": 0.130, "S18": 0.140,
    }
    peer_sectors = {sym: "Consumer Staples" for sym in staples}

    pg_result = adoption.compute_adoption(
        "PG", rows_pg, [], staples,
        sector="Consumer Staples", peer_sectors=peer_sectors,
    )
    mo_result = adoption.compute_adoption(
        "MO", rows_mo, [], staples,
        sector="Consumer Staples", peer_sectors=peer_sectors,
    )
    pg_sub = pg_result["components"]["revenue_subscore"]
    mo_sub = mo_result["components"]["revenue_subscore"]

    # Both used sector-relative.
    assert pg_result["components"]["peer_cohort_strategy"] == "sector_relative"
    assert mo_result["components"]["peer_cohort_strategy"] == "sector_relative"

    # MO sits at the cohort min -> softens to floor.
    assert mo_sub == pytest.approx(adoption._SECTOR_RANK_FLOOR)
    # PG sits strictly inside the cohort -> beats the floor.
    assert pg_sub > adoption._SECTOR_RANK_FLOOR


def test_compute_adoption_qoq_component_active_when_quarters_present() -> None:
    """When QoQ is computable, has_qoq=True and weights renorm to 0.50/0.30/0.20."""
    # 8 quarters + 2 annuals so revenue_growth_yoy uses the annual path
    # and QoQ uses the latest two quarters.
    rows = [
        _annual("2025-12-31", 440.0, 2025),
        _annual("2024-12-31", 400.0, 2024),
        _quarter("2026-03-31", 110.0, 2026, "Q1"),
        _quarter("2025-12-31", 100.0, 2025, "Q4"),
        _quarter("2025-09-30", 105.0, 2025, "Q3"),
        _quarter("2025-06-30", 110.0, 2025, "Q2"),
        _quarter("2025-03-31", 90.0, 2025, "Q1"),
        _quarter("2024-12-31", 90.0, 2024, "Q4"),
        _quarter("2024-09-30", 95.0, 2024, "Q3"),
        _quarter("2024-06-30", 100.0, 2024, "Q2"),
    ]
    peer_growths = {"AAPL": 0.10, "P1": 0.05, "P2": 0.20}
    # Trends data: present, quality good
    trends = {
        "data_quality": "good",
        "acceleration_4w_pct": 12.5,
        "acceleration_12w_pct": 8.0,
        "signal_score": 0.30,
        "trend_week": "2026-W20",
        "regime": "accelerating",
    }
    universe_trends = {
        "AAPL": trends,
        "P1": {**trends, "acceleration_4w_pct": 5.0},
        "P2": {**trends, "acceleration_4w_pct": -10.0},
    }
    result = adoption.compute_adoption(
        "AAPL", rows, [], peer_growths,
        trends_data=trends, universe_trends_data=universe_trends,
    )
    assert result["data_quality"]["has_qoq"] is True
    eff = result["effective_weights"]
    assert eff["revenue"] == pytest.approx(0.50)
    assert eff["trends"] == pytest.approx(0.30)
    assert eff["qoq"] == pytest.approx(0.20)
    assert result["components"]["qoq_acceleration_pct"] == pytest.approx(0.10, rel=1e-6)


def test_compute_adoption_qoq_no_trends_promotes_qoq_to_30pct() -> None:
    """Trends missing + QoQ present -> revenue 0.70 / qoq 0.30."""
    rows = [
        _quarter("2026-03-31", 110.0, 2026, "Q1"),
        _quarter("2025-12-31", 100.0, 2025, "Q4"),
    ]
    peer_growths = {"AAPL": 0.10}
    result = adoption.compute_adoption("AAPL", rows, [], peer_growths)
    assert result["data_quality"]["has_qoq"] is True
    assert result["data_quality"]["has_trends"] is False
    eff = result["effective_weights"]
    assert eff["revenue"] == pytest.approx(0.70)
    assert eff["trends"] == pytest.approx(0.0)
    assert eff["qoq"] == pytest.approx(0.30)


# --- Hardware/Software split: bimodality regression guard ------------------
#
# Spec: docs/lthcs-tech-hardware-software-split.md §7 — guards the audit fix
# that closes the AAPL bimodality (peer-group-audit.md §3.4). The split adds
# tech_sub_bucket to universe.json + extends the compound peer key to a
# 3-tuple for Tech tickers only. Tests below catch:
#   1. Schema drift in universe.json (every Tech ticker has a valid bucket;
#      no non-Tech ticker has the field)
#   2. Cohort-size invariants per spec §4 (Hardware/IT Services intentionally
#      below floor; Software/Semiconductors above)
#   3. Distribution: Software stdev < parent-Tech stdev on adoption_momentum
#      (the deterministic case from spec §3 §7)
#   4. The AAPL regression — cohort excludes the bimodality-driving
#      growth-stage semis after the split


import json
from statistics import pstdev as _pstdev

from lthcs.peer_groups import (
    ALLOWED_TECH_SUB_BUCKETS,
    TECH_SECTORS,
    get_peer_cohort_with_strategy,
    get_tech_sub_bucket,
    load_peer_groups_config,
)


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_UNIVERSE_PATH = _REPO_ROOT / "data" / "lthcs" / "universe.json"


@pytest.fixture(scope="module")
def _universe() -> Dict[str, Any]:
    with open(_UNIVERSE_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def _peer_groups_config() -> Dict[str, Any]:
    return load_peer_groups_config()


def test_tech_sub_bucket_schema_every_tech_ticker_curated(
    _universe: Dict[str, Any],
) -> None:
    """Every Tech ticker in universe.json carries a valid tech_sub_bucket
    (spec §5 — universe.json v2.2.0 schema)."""
    missing: List[str] = []
    bad_value: Dict[str, Any] = {}
    for t in _universe["tickers"]:
        if t.get("sector") not in TECH_SECTORS:
            continue
        if not t.get("active", True):
            continue
        bucket = t.get("tech_sub_bucket")
        if bucket is None:
            missing.append(t["ticker"])
        elif bucket not in ALLOWED_TECH_SUB_BUCKETS:
            bad_value[t["ticker"]] = bucket
    assert not missing, (
        f"Tech tickers missing tech_sub_bucket: {missing}"
    )
    assert not bad_value, (
        f"Tech tickers with invalid tech_sub_bucket: {bad_value} "
        f"(allowed: {sorted(ALLOWED_TECH_SUB_BUCKETS)})"
    )


def test_tech_sub_bucket_schema_non_tech_tickers_have_no_field(
    _universe: Dict[str, Any],
) -> None:
    """Non-Tech tickers must NOT carry tech_sub_bucket — backwards-compat
    invariant (spec §6). A leak here means the 2-tuple compound-key path
    for non-Tech sectors could break."""
    leakage = [
        t["ticker"]
        for t in _universe["tickers"]
        if t.get("sector") not in TECH_SECTORS
        and "tech_sub_bucket" in t
    ]
    assert not leakage, f"Non-Tech tickers carrying tech_sub_bucket: {leakage}"


def test_tech_sub_bucket_cohort_sizes_match_spec(
    _universe: Dict[str, Any],
) -> None:
    """Spec §2 — bucket counts after Wave A expansion (2026-05-21):
    Hardware=8, Semiconductors=19, Software=18, IT Services=4.
    Pre-Wave A was Hardware=3, Semiconductors=18, otherwise identical.
    Detects accidental reclassifications. Note: Hardware crossed the n=6
    floor with Wave A, which flips AAPL/CSCO/SMCI from maturity_only
    cascade to sector_group_only scoring — accepted with eyes-open."""
    counts: Dict[str, int] = {}
    for t in _universe["tickers"]:
        if t.get("sector") not in TECH_SECTORS:
            continue
        if not t.get("active", True):
            continue
        bucket = t.get("tech_sub_bucket")
        if bucket:
            counts[bucket] = counts.get(bucket, 0) + 1
    assert counts == {
        "Hardware": 8,
        "Semiconductors": 19,
        "Software": 18,
        "IT Services": 4,
    }, f"Bucket counts drifted: {counts}"


def test_software_and_semiconductors_clear_min_cohort_size(
    _universe: Dict[str, Any],
) -> None:
    """All tech sub-buckets except IT Services now clear the n=6 floor
    after Wave A (Hardware n=8, Semiconductors n=19, Software n=18,
    IT Services n=4). Pre-Wave A, Hardware was n=3 and cascaded; that
    cascade was intentional but is no longer in force — AAPL/CSCO/SMCI
    now ride the compound path. See test_aapl_cohort_cascades_through_split
    for the downstream behaviour change."""
    buckets: Dict[str, List[str]] = {}
    for t in _universe["tickers"]:
        if t.get("sector") not in TECH_SECTORS:
            continue
        if not t.get("active", True):
            continue
        bucket = t.get("tech_sub_bucket")
        if bucket:
            buckets.setdefault(bucket, []).append(t["ticker"])
    assert len(buckets.get("Software", [])) >= 6
    assert len(buckets.get("Semiconductors", [])) >= 6
    assert len(buckets.get("Hardware", [])) >= 6
    # IT Services still intentionally below floor — only sub-bucket that cascades.
    assert len(buckets.get("IT Services", [])) < 6


def test_software_distribution_tighter_than_parent_tech(
    _universe: Dict[str, Any],
) -> None:
    """Spec §7 case 3 — distribution check on a synthetic stand-in for the
    adoption_momentum sub-score. Uses the maturity_stage label as a coarse
    growth proxy (mature=5, standard=15, growth=40, recovery=10,
    pre_profit=60) so the test is fully deterministic without a snapshot.
    Software's stdev (mostly mature + standard) must be < parent-Tech
    stdev (which spans all stages including growth-stage semis)."""
    stage_score = {
        "mature_compounder": 5.0,
        "standard_compounder": 15.0,
        "growth_compounder": 40.0,
        "recovery_stabilization": 10.0,
        "pre_profit": 60.0,
    }

    parent_scores: List[float] = []
    software_scores: List[float] = []
    for t in _universe["tickers"]:
        if t.get("sector") not in TECH_SECTORS:
            continue
        if not t.get("active", True):
            continue
        score = stage_score.get(t.get("maturity_stage"), 20.0)
        parent_scores.append(score)
        if t.get("tech_sub_bucket") == "Software":
            software_scores.append(score)

    assert len(software_scores) >= 6
    assert _pstdev(software_scores) < _pstdev(parent_scores), (
        f"Software stdev {_pstdev(software_scores):.2f} not tighter than "
        f"parent Tech stdev {_pstdev(parent_scores):.2f}"
    )


def test_aapl_cohort_excludes_growth_stage_semis_post_split(
    _universe: Dict[str, Any], _peer_groups_config: Dict[str, Any]
) -> None:
    """Bimodality-fix regression guard (spec §7 case 4) — Wave A revision.

    Pre-Wave A: AAPL cascaded to maturity_only because tech_hardware was
    n=3. Wave A expanded tech_hardware to n=8 (added ANET/APH/GLW/STX/WDC),
    so AAPL now resolves via STRATEGY_SECTOR_GROUP_ONLY. The bimodality
    guard still holds because the growth-stage offenders (NVDA, AMD, MU,
    MRVL) live in tech_semiconductors, not tech_hardware — they were never
    going to leak into a Hardware cohort regardless of cascade level.
    SMCI is in tech_hardware (n=8 with AAPL) so it WILL appear in AAPL's
    cohort post-Wave A; that's an accepted trade-off, not a regression
    (SMCI's growth-stage profile is the only AI-server name left in
    hardware and the cohort is small enough that one outlier is tolerable).
    """
    cohort, strategy = get_peer_cohort_with_strategy(
        "AAPL", _universe, _peer_groups_config
    )

    # Post-Wave A: Hardware sector_group is large enough to resolve directly.
    assert strategy == "sector_group_only", (
        f"AAPL strategy regressed to {strategy}; post-Wave A expects "
        f"sector_group_only (tech_hardware n=8 clears the floor)"
    )

    # The peak-cycle semis live in tech_semiconductors, NOT tech_hardware,
    # so they cannot leak into AAPL's hardware cohort.
    bimodality_offenders = {"NVDA", "AMD", "MU", "MRVL"}
    leaked = bimodality_offenders & set(cohort)
    assert not leaked, (
        f"AAPL cohort still contains bimodality-driving growth-stage "
        f"semis: {sorted(leaked)} (strategy={strategy})"
    )

    # AAPL itself is always in its own cohort (caller excludes self).
    assert "AAPL" in cohort


def test_get_tech_sub_bucket_helper(_universe: Dict[str, Any]) -> None:
    """Helper-level: get_tech_sub_bucket returns the right bucket for Tech
    and None for non-Tech."""
    assert get_tech_sub_bucket("AAPL", _universe) == "Hardware"
    assert get_tech_sub_bucket("NVDA", _universe) == "Semiconductors"
    assert get_tech_sub_bucket("MSFT", _universe) == "Software"
    assert get_tech_sub_bucket("ACN", _universe) == "IT Services"
    # Non-Tech: no field, no bucket.
    assert get_tech_sub_bucket("JPM", _universe) is None
    assert get_tech_sub_bucket("XOM", _universe) is None
    # Unknown ticker: None.
    assert get_tech_sub_bucket("ZZZZ", _universe) is None
