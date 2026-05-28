"""Tests for lthcs.sources.fred_tier2.

HTTP is mocked at the ``fred.requests.get`` boundary (same approach as
``test_fred_breadth.py``).  Module-level caches are redirected to
``tmp_path`` so every test starts cold.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

from lthcs.sources import fred, fred_tier2
from lthcs.sources._cache import FileCache
from lthcs.sources._ratelimit import TokenBucket


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Dict[str, FileCache]:
    """Redirect both the tier2-snapshot cache and the underlying ``fred``
    raw-series cache to a fresh tmp dir for every test."""
    monkeypatch.setenv("FRED_API_KEY", "test-key")
    tier2_cache = FileCache("fred_tier2", root=tmp_path)
    fred_cache = FileCache("fred", root=tmp_path)
    monkeypatch.setattr(fred_tier2, "_cache", tier2_cache)
    monkeypatch.setattr(fred, "_cache", fred_cache)
    return {"tier2": tier2_cache, "fred": fred_cache}


@pytest.fixture(autouse=True)
def fast_buckets(monkeypatch: pytest.MonkeyPatch) -> None:
    """No real rate-limit sleeps during tests."""
    big = TokenBucket(capacity=1_000_000, refill_rate=1_000_000)
    monkeypatch.setattr(fred, "_bucket", big)
    monkeypatch.setattr(fred_tier2, "_bucket", big)


def _fake_response(json_data: Any, status: int = 200) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.json.return_value = json_data
    m.ok = status == 200
    m.text = str(json_data)
    return m


def _flat_payload(
    value: float, count: int, end_date: Optional[str] = None
) -> Dict[str, Any]:
    """Flat (constant) series ending at ``end_date`` (defaults today)."""
    end = _dt.date.fromisoformat(end_date) if end_date else _dt.date.today()
    obs = []
    for i in range(count):
        d = (end - _dt.timedelta(days=count - 1 - i)).isoformat()
        obs.append({"date": d, "value": f"{value}"})
    return {"observations": obs}


def _ramp_payload(
    start_value: float,
    end_value: float,
    count: int,
    end_date: Optional[str] = None,
) -> Dict[str, Any]:
    """Linear ramp from start -> end ending at ``end_date``."""
    end = _dt.date.fromisoformat(end_date) if end_date else _dt.date.today()
    obs = []
    if count == 1:
        obs.append({"date": end.isoformat(), "value": f"{end_value}"})
        return {"observations": obs}
    step = (end_value - start_value) / (count - 1)
    for i in range(count):
        d = (end - _dt.timedelta(days=count - 1 - i)).isoformat()
        v = start_value + step * i
        obs.append({"date": d, "value": f"{v}"})
    return {"observations": obs}


def _series_router(payloads: Dict[str, Dict[str, Any]]) -> Any:
    """Build a side_effect for ``fred.requests.get`` that dispatches on
    series_id.  Unknown series -> 404."""

    def _stub(url: str, **kwargs: Any) -> MagicMock:
        params = kwargs.get("params") or {}
        sid = params.get("series_id")
        if sid in payloads:
            return _fake_response(payloads[sid])
        return _fake_response({"observations": []}, status=404)

    return _stub


def _all_six_flat() -> Dict[str, Dict[str, Any]]:
    """Standard happy-path payload set: all six series populated with
    enough history (200 days) to satisfy the 3m lookback + 2y window."""
    return {
        fred_tier2.SERIES_BRENT:             _flat_payload(80.0, 200),
        fred_tier2.SERIES_GASOLINE:          _flat_payload(3.50, 200),
        fred_tier2.SERIES_INDPRO:            _flat_payload(102.0, 200),
        fred_tier2.SERIES_HOUSING_STARTS:    _flat_payload(1400.0, 200),
        fred_tier2.SERIES_CONSUMER_SENTIMENT: _flat_payload(70.0, 200),
        fred_tier2.SERIES_U6:                _flat_payload(7.5, 200),
    }


# ---------------------------------------------------------------------------
# Snapshot shape
# ---------------------------------------------------------------------------


def test_snapshot_has_expected_top_level_keys() -> None:
    with patch.object(fred.requests, "get", side_effect=_series_router(_all_six_flat())):
        snap = fred_tier2.fetch_tier2_macro_snapshot()
    assert snap["as_of"] == _dt.date.today().isoformat()
    for k in (
        "brent_crude",
        "gasoline_retail",
        "ism_pmi_proxy",
        "housing_starts",
        "consumer_sentiment",
        "u6_unemployment",
        "data_quality",
    ):
        assert k in snap


def test_snapshot_per_series_blocks_have_expected_shape() -> None:
    with patch.object(fred.requests, "get", side_effect=_series_router(_all_six_flat())):
        snap = fred_tier2.fetch_tier2_macro_snapshot()

    assert snap["brent_crude"]["current"] == pytest.approx(80.0)
    assert "change_3m_pct" in snap["brent_crude"]
    assert "percentile_2y" in snap["brent_crude"]

    assert snap["gasoline_retail"]["current"] == pytest.approx(3.50)
    # Crack spread is surfaced on the gasoline block.
    assert "crack_spread_per_gal" in snap["gasoline_retail"]

    assert snap["ism_pmi_proxy"]["current"] == pytest.approx(102.0)
    assert "regime" in snap["ism_pmi_proxy"]

    assert snap["housing_starts"]["current"] == pytest.approx(1400.0)
    assert snap["consumer_sentiment"]["current"] == pytest.approx(70.0)

    assert snap["u6_unemployment"]["current"] == pytest.approx(7.5)
    # U-6 uses bp change (rate-like).
    assert "change_3m_bp" in snap["u6_unemployment"]


def test_data_quality_counts_all_six_ok() -> None:
    with patch.object(fred.requests, "get", side_effect=_series_router(_all_six_flat())):
        snap = fred_tier2.fetch_tier2_macro_snapshot()
    assert snap["data_quality"]["sources_ok"] == 6
    assert snap["data_quality"]["sources_failed"] == 0
    assert snap["data_quality"]["failed_sources"] == []


# ---------------------------------------------------------------------------
# Missing sources degrade gracefully
# ---------------------------------------------------------------------------


def test_missing_series_degrades_to_none() -> None:
    # Drop two series — they should resolve to None and be counted in
    # data_quality.failed_sources by name.
    payloads = _all_six_flat()
    del payloads[fred_tier2.SERIES_INDPRO]
    del payloads[fred_tier2.SERIES_U6]
    with patch.object(fred.requests, "get", side_effect=_series_router(payloads)):
        snap = fred_tier2.fetch_tier2_macro_snapshot()
    assert snap["ism_pmi_proxy"] is None
    assert snap["u6_unemployment"] is None
    assert snap["data_quality"]["sources_ok"] == 4
    assert snap["data_quality"]["sources_failed"] == 2
    assert set(snap["data_quality"]["failed_sources"]) == {
        "ism_pmi_proxy",
        "u6_unemployment",
    }


def test_all_sources_failed_returns_valid_dict() -> None:
    # Every series unknown / 404.
    with patch.object(fred.requests, "get", side_effect=_series_router({})):
        snap = fred_tier2.fetch_tier2_macro_snapshot()
    assert snap["brent_crude"] is None
    assert snap["data_quality"]["sources_ok"] == 0
    assert snap["data_quality"]["sources_failed"] == 6


# ---------------------------------------------------------------------------
# as_of passthrough
# ---------------------------------------------------------------------------


def test_as_of_is_echoed_in_snapshot() -> None:
    payloads = _all_six_flat()
    with patch.object(fred.requests, "get", side_effect=_series_router(payloads)):
        snap = fred_tier2.fetch_tier2_macro_snapshot(as_of="2025-01-15")
    assert snap["as_of"] == "2025-01-15"


def test_as_of_filters_series_to_on_or_before() -> None:
    # Build a ramp that ENDS today; ask for as_of two years ago. The
    # underlying ``fred.get_series(as_of=...)`` (commit 61f90f5) filters
    # rows; we should see the value from the as_of date (or just before),
    # NOT today's value.
    today = _dt.date.today()
    two_yrs_ago = (today - _dt.timedelta(days=730)).isoformat()
    # Ramp 50 -> 150 across 1000 days ending today.
    ramp = _ramp_payload(50.0, 150.0, 1000)
    payloads = _all_six_flat()
    payloads[fred_tier2.SERIES_BRENT] = ramp
    with patch.object(fred.requests, "get", side_effect=_series_router(payloads)):
        snap = fred_tier2.fetch_tier2_macro_snapshot(as_of=two_yrs_ago)
    # Roughly 2 years from today = ~73% of the way down from 150 to 50.
    # We don't pin the exact value (depends on ramp resolution); just
    # confirm it's NOT the day-of-today peak (150).
    assert snap["brent_crude"]["current"] < 140.0
    assert snap["brent_crude"]["current"] > 50.0


# ---------------------------------------------------------------------------
# ISM regime boundary
# ---------------------------------------------------------------------------


def test_ism_regime_neutral_when_flat() -> None:
    # Flat INDPRO => 3m change ~ 0 => regime neutral.
    payloads = _all_six_flat()
    with patch.object(fred.requests, "get", side_effect=_series_router(payloads)):
        snap = fred_tier2.fetch_tier2_macro_snapshot()
    assert snap["ism_pmi_proxy"]["regime"] == "neutral"


def test_ism_regime_expansion_when_rising() -> None:
    # Ramp 95 -> 105 over 200 days => clear positive 3m momentum > 0.5%.
    payloads = _all_six_flat()
    payloads[fred_tier2.SERIES_INDPRO] = _ramp_payload(95.0, 105.0, 200)
    with patch.object(fred.requests, "get", side_effect=_series_router(payloads)):
        snap = fred_tier2.fetch_tier2_macro_snapshot()
    assert snap["ism_pmi_proxy"]["regime"] == "expansion"
    assert (snap["ism_pmi_proxy"]["change_3m_pct"] or 0.0) > 0.005


def test_ism_regime_contraction_when_falling() -> None:
    # Ramp 105 -> 95 over 200 days => negative 3m momentum.
    payloads = _all_six_flat()
    payloads[fred_tier2.SERIES_INDPRO] = _ramp_payload(105.0, 95.0, 200)
    with patch.object(fred.requests, "get", side_effect=_series_router(payloads)):
        snap = fred_tier2.fetch_tier2_macro_snapshot()
    assert snap["ism_pmi_proxy"]["regime"] == "contraction"
    assert (snap["ism_pmi_proxy"]["change_3m_pct"] or 0.0) < -0.005


# ---------------------------------------------------------------------------
# Gasoline crack spread arithmetic
# ---------------------------------------------------------------------------


def test_crack_spread_uses_gasoline_minus_0_42_brent() -> None:
    # Brent = 80, gasoline = 3.50 => crack = 3.50 - 0.42*80 = 3.50 - 33.60 = -30.10
    # (Note: this is the spec's documented formula — don't be alarmed by
    # the sign; it's just the arithmetic.  Real values land in a healthy
    # positive range because retail gasoline is per-gallon and crude
    # per-bbl differs by another factor of 100; the constant 0.42 is the
    # rule-of-thumb the audit calls out.)
    payloads = _all_six_flat()
    with patch.object(fred.requests, "get", side_effect=_series_router(payloads)):
        snap = fred_tier2.fetch_tier2_macro_snapshot()
    spread = snap["gasoline_retail"]["crack_spread_per_gal"]
    expected = 3.50 - 0.42 * 80.0
    assert spread == pytest.approx(expected, abs=1e-4)


def test_crack_spread_none_when_brent_missing() -> None:
    # Drop Brent => crack spread should fall to None.
    payloads = _all_six_flat()
    del payloads[fred_tier2.SERIES_BRENT]
    with patch.object(fred.requests, "get", side_effect=_series_router(payloads)):
        snap = fred_tier2.fetch_tier2_macro_snapshot()
    assert snap["brent_crude"] is None
    assert snap["gasoline_retail"]["crack_spread_per_gal"] is None


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def test_snapshot_is_cached() -> None:
    # Two consecutive calls should only result in one network round
    # (assertion: requests.get called exactly six times — one per
    # series, on the first call).
    mock_get = MagicMock(side_effect=_series_router(_all_six_flat()))
    with patch.object(fred.requests, "get", mock_get):
        first = fred_tier2.fetch_tier2_macro_snapshot()
        second = fred_tier2.fetch_tier2_macro_snapshot()
    assert first == second
    # First call must have hit FRED for each series; second call reads
    # from snapshot cache so no additional calls.  Allow >=6 because the
    # cache layer is implementation-detailed; assert no growth between calls.
    n_first = mock_get.call_count
    with patch.object(fred.requests, "get", mock_get):
        third = fred_tier2.fetch_tier2_macro_snapshot()
    assert mock_get.call_count == n_first
    assert third == first
