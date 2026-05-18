"""Tests for lthcs.sources.fred_breadth.

All HTTP is mocked at the ``fred.requests.get`` boundary (the
underlying source that fred_breadth uses).  Module-level caches are
redirected to ``tmp_path`` via ``monkeypatch`` so every test starts
cold.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from lthcs.sources import fred, fred_breadth
from lthcs.sources._cache import FileCache
from lthcs.sources._ratelimit import TokenBucket


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Dict[str, FileCache]:
    """Redirect both the breadth-snapshot cache and the underlying
    ``fred`` raw-series cache to a fresh tmp dir for every test."""
    monkeypatch.setenv("FRED_API_KEY", "test-key")
    breadth_cache = FileCache("fred_breadth", root=tmp_path)
    fred_cache = FileCache("fred", root=tmp_path)
    monkeypatch.setattr(fred_breadth, "_cache", breadth_cache)
    monkeypatch.setattr(fred, "_cache", fred_cache)
    return {"breadth": breadth_cache, "fred": fred_cache}


@pytest.fixture(autouse=True)
def fast_buckets(monkeypatch: pytest.MonkeyPatch) -> None:
    """No real rate-limit sleeps during tests."""
    big = TokenBucket(capacity=1_000_000, refill_rate=1_000_000)
    monkeypatch.setattr(fred, "_bucket", big)
    monkeypatch.setattr(fred_breadth, "_bucket", big)


def _fake_response(json_data: Any, status: int = 200) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.json.return_value = json_data
    m.ok = status == 200
    m.text = str(json_data)
    return m


def _series_payload(values: List[Optional[float]], start: str = "2024-01-02") -> Dict[str, Any]:
    """Build a FRED-shaped JSON payload from a list of values.

    Each value becomes an observation on consecutive *calendar* days
    starting at ``start``.  Use ``None`` to insert a missing observation
    (FRED would serialise this as ``"."``); use a float to set a real
    observation.
    """
    base = _dt.date.fromisoformat(start)
    obs = []
    for i, v in enumerate(values):
        d = (base + _dt.timedelta(days=i)).isoformat()
        if v is None:
            obs.append({"date": d, "value": "."})
        else:
            obs.append({"date": d, "value": f"{v}"})
    return {"observations": obs}


def _flat_payload(value: float, count: int, end_date: Optional[str] = None) -> Dict[str, Any]:
    """A flat (constant) series ending at ``end_date`` (defaults today)."""
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
    """A linear ramp from ``start_value`` -> ``end_value`` ending today."""
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
    the ``series_id`` query param.  Unknown series get a 404.
    """

    def _stub(url: str, **kwargs: Any) -> MagicMock:
        params = kwargs.get("params") or {}
        sid = params.get("series_id")
        if sid in payloads:
            return _fake_response(payloads[sid])
        return _fake_response({"observations": []}, status=404)

    return _stub


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_snapshot_has_expected_top_level_keys() -> None:
    payloads = {
        fred_breadth.SERIES_HY_OAS: _flat_payload(3.42, 60),
        fred_breadth.SERIES_IG_OAS: _flat_payload(1.02, 60),
        fred_breadth.SERIES_2S10S: _flat_payload(0.45, 60),
        fred_breadth.SERIES_BROAD_DOLLAR: _flat_payload(121.3, 60),
    }
    with patch.object(fred.requests, "get", side_effect=_series_router(payloads)):
        snap = fred_breadth.fetch_breadth_snapshot()

    assert snap["as_of"] == _dt.date.today().isoformat()
    for k in (
        "hy_oas",
        "ig_oas",
        "yield_curve_2s10s",
        "broad_dollar",
        "regime_flags",
        "data_quality",
    ):
        assert k in snap


def test_snapshot_per_series_blocks_have_expected_shape() -> None:
    payloads = {
        fred_breadth.SERIES_HY_OAS: _flat_payload(3.42, 60),
        fred_breadth.SERIES_IG_OAS: _flat_payload(1.02, 60),
        fred_breadth.SERIES_2S10S: _flat_payload(0.45, 60),
        fred_breadth.SERIES_BROAD_DOLLAR: _flat_payload(121.3, 60),
    }
    with patch.object(fred.requests, "get", side_effect=_series_router(payloads)):
        snap = fred_breadth.fetch_breadth_snapshot()

    assert snap["hy_oas"]["current"] == pytest.approx(3.42)
    assert "change_30d_bp" in snap["hy_oas"]
    assert "percentile_2y" in snap["hy_oas"]

    assert snap["ig_oas"]["current"] == pytest.approx(1.02)

    assert snap["yield_curve_2s10s"]["current"] == pytest.approx(0.45)
    assert snap["yield_curve_2s10s"]["inverted"] is False

    assert snap["broad_dollar"]["current"] == pytest.approx(121.3)


def test_snapshot_data_quality_counts_ok_and_failed() -> None:
    # All four series happy.
    payloads = {
        fred_breadth.SERIES_HY_OAS: _flat_payload(3.0, 40),
        fred_breadth.SERIES_IG_OAS: _flat_payload(1.0, 40),
        fred_breadth.SERIES_2S10S: _flat_payload(0.1, 40),
        fred_breadth.SERIES_BROAD_DOLLAR: _flat_payload(120.0, 40),
    }
    with patch.object(fred.requests, "get", side_effect=_series_router(payloads)):
        snap = fred_breadth.fetch_breadth_snapshot()
    assert snap["data_quality"] == {"sources_ok": 4, "sources_failed": 0}


# ---------------------------------------------------------------------------
# 30d-change math
# ---------------------------------------------------------------------------


def test_hy_oas_30d_change_is_in_basis_points() -> None:
    # 22 observations, ramp from 3.00 -> 3.50.  Δ over the trailing 21
    # bars is +0.50%, which is +50bp.
    payloads = {
        fred_breadth.SERIES_HY_OAS: _ramp_payload(3.00, 3.50, 22),
        fred_breadth.SERIES_IG_OAS: _flat_payload(1.0, 22),
        fred_breadth.SERIES_2S10S: _flat_payload(0.1, 22),
        fred_breadth.SERIES_BROAD_DOLLAR: _flat_payload(120.0, 22),
    }
    with patch.object(fred.requests, "get", side_effect=_series_router(payloads)):
        snap = fred_breadth.fetch_breadth_snapshot()
    assert snap["hy_oas"]["change_30d_bp"] == pytest.approx(50.0, abs=0.5)


def test_broad_dollar_30d_change_is_decimal_pct() -> None:
    # 22 obs ramping 100 -> 102.  Δ over trailing 21 bars = +2/100 = +0.02.
    payloads = {
        fred_breadth.SERIES_HY_OAS: _flat_payload(3.0, 22),
        fred_breadth.SERIES_IG_OAS: _flat_payload(1.0, 22),
        fred_breadth.SERIES_2S10S: _flat_payload(0.1, 22),
        fred_breadth.SERIES_BROAD_DOLLAR: _ramp_payload(100.0, 102.0, 22),
    }
    with patch.object(fred.requests, "get", side_effect=_series_router(payloads)):
        snap = fred_breadth.fetch_breadth_snapshot()
    assert snap["broad_dollar"]["change_30d_pct"] == pytest.approx(0.02, abs=1e-4)


# ---------------------------------------------------------------------------
# Regime flag thresholds
# ---------------------------------------------------------------------------


def test_hy_stress_flag_triggers_above_50bp() -> None:
    # 22 obs ramping 3.0 -> 3.6 over the trailing 21d => +60bp.
    payloads = {
        fred_breadth.SERIES_HY_OAS: _ramp_payload(3.0, 3.6, 22),
        fred_breadth.SERIES_IG_OAS: _flat_payload(1.0, 22),
        fred_breadth.SERIES_2S10S: _flat_payload(0.1, 22),
        fred_breadth.SERIES_BROAD_DOLLAR: _flat_payload(120.0, 22),
    }
    with patch.object(fred.requests, "get", side_effect=_series_router(payloads)):
        snap = fred_breadth.fetch_breadth_snapshot()
    assert snap["regime_flags"]["hy_stress"] is True


def test_hy_stress_flag_quiet_when_below_50bp() -> None:
    # +30bp over the window — well below the +50bp threshold.
    payloads = {
        fred_breadth.SERIES_HY_OAS: _ramp_payload(3.0, 3.3, 22),
        fred_breadth.SERIES_IG_OAS: _flat_payload(1.0, 22),
        fred_breadth.SERIES_2S10S: _flat_payload(0.1, 22),
        fred_breadth.SERIES_BROAD_DOLLAR: _flat_payload(120.0, 22),
    }
    with patch.object(fred.requests, "get", side_effect=_series_router(payloads)):
        snap = fred_breadth.fetch_breadth_snapshot()
    assert snap["regime_flags"]["hy_stress"] is False


def test_hy_stress_flag_quiet_exactly_at_50bp_threshold() -> None:
    # The threshold is strictly *greater than* +50bp.  At exactly +50bp
    # the flag must NOT trigger.
    payloads = {
        fred_breadth.SERIES_HY_OAS: _ramp_payload(3.0, 3.5, 22),
        fred_breadth.SERIES_IG_OAS: _flat_payload(1.0, 22),
        fred_breadth.SERIES_2S10S: _flat_payload(0.1, 22),
        fred_breadth.SERIES_BROAD_DOLLAR: _flat_payload(120.0, 22),
    }
    with patch.object(fred.requests, "get", side_effect=_series_router(payloads)):
        snap = fred_breadth.fetch_breadth_snapshot()
    assert snap["regime_flags"]["hy_stress"] is False


def test_curve_inverted_flag() -> None:
    # Negative current value => inverted.
    payloads = {
        fred_breadth.SERIES_HY_OAS: _flat_payload(3.0, 22),
        fred_breadth.SERIES_IG_OAS: _flat_payload(1.0, 22),
        fred_breadth.SERIES_2S10S: _flat_payload(-0.25, 22),
        fred_breadth.SERIES_BROAD_DOLLAR: _flat_payload(120.0, 22),
    }
    with patch.object(fred.requests, "get", side_effect=_series_router(payloads)):
        snap = fred_breadth.fetch_breadth_snapshot()
    assert snap["yield_curve_2s10s"]["inverted"] is True
    assert snap["regime_flags"]["curve_inverted"] is True


def test_dollar_strong_flag_triggers_above_2pct() -> None:
    # +3% rise across the window — clears the +2% bar.
    payloads = {
        fred_breadth.SERIES_HY_OAS: _flat_payload(3.0, 22),
        fred_breadth.SERIES_IG_OAS: _flat_payload(1.0, 22),
        fred_breadth.SERIES_2S10S: _flat_payload(0.1, 22),
        fred_breadth.SERIES_BROAD_DOLLAR: _ramp_payload(100.0, 103.0, 22),
    }
    with patch.object(fred.requests, "get", side_effect=_series_router(payloads)):
        snap = fred_breadth.fetch_breadth_snapshot()
    assert snap["regime_flags"]["dollar_strong"] is True


def test_dollar_strong_flag_quiet_exactly_at_2pct() -> None:
    payloads = {
        fred_breadth.SERIES_HY_OAS: _flat_payload(3.0, 22),
        fred_breadth.SERIES_IG_OAS: _flat_payload(1.0, 22),
        fred_breadth.SERIES_2S10S: _flat_payload(0.1, 22),
        fred_breadth.SERIES_BROAD_DOLLAR: _ramp_payload(100.0, 102.0, 22),
    }
    with patch.object(fred.requests, "get", side_effect=_series_router(payloads)):
        snap = fred_breadth.fetch_breadth_snapshot()
    assert snap["regime_flags"]["dollar_strong"] is False


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------


def test_single_series_failure_returns_none_block_and_bumps_counter() -> None:
    # HY OAS goes 500; the other three succeed.
    def _stub(url: str, **kwargs: Any) -> MagicMock:
        sid = (kwargs.get("params") or {}).get("series_id")
        if sid == fred_breadth.SERIES_HY_OAS:
            return _fake_response({"error_message": "boom"}, status=500)
        if sid == fred_breadth.SERIES_IG_OAS:
            return _fake_response(_flat_payload(1.0, 22))
        if sid == fred_breadth.SERIES_2S10S:
            return _fake_response(_flat_payload(0.1, 22))
        if sid == fred_breadth.SERIES_BROAD_DOLLAR:
            return _fake_response(_flat_payload(120.0, 22))
        return _fake_response({"observations": []}, status=404)

    with patch.object(fred.requests, "get", side_effect=_stub):
        snap = fred_breadth.fetch_breadth_snapshot()

    assert snap["hy_oas"] is None
    assert snap["ig_oas"] is not None
    assert snap["yield_curve_2s10s"] is not None
    assert snap["broad_dollar"] is not None
    assert snap["data_quality"] == {"sources_ok": 3, "sources_failed": 1}
    # Flag defaults to False when the underlying series is missing.
    assert snap["regime_flags"]["hy_stress"] is False


def test_all_series_failure_returns_well_formed_snapshot() -> None:
    # Every series returns 500.  Snapshot still shaped correctly.
    with patch.object(
        fred.requests,
        "get",
        return_value=_fake_response({"err": "x"}, status=500),
    ):
        snap = fred_breadth.fetch_breadth_snapshot()

    assert snap["hy_oas"] is None
    assert snap["ig_oas"] is None
    assert snap["yield_curve_2s10s"] is None
    assert snap["broad_dollar"] is None
    assert snap["data_quality"] == {"sources_ok": 0, "sources_failed": 4}
    assert snap["regime_flags"] == {
        "hy_stress": False,
        "curve_inverted": False,
        "dollar_strong": False,
    }


def test_empty_observations_returns_none_block() -> None:
    payloads = {
        fred_breadth.SERIES_HY_OAS: {"observations": []},
        fred_breadth.SERIES_IG_OAS: _flat_payload(1.0, 22),
        fred_breadth.SERIES_2S10S: _flat_payload(0.1, 22),
        fred_breadth.SERIES_BROAD_DOLLAR: _flat_payload(120.0, 22),
    }
    with patch.object(fred.requests, "get", side_effect=_series_router(payloads)):
        snap = fred_breadth.fetch_breadth_snapshot()
    assert snap["hy_oas"] is None
    assert snap["data_quality"]["sources_failed"] == 1


def test_all_null_observations_treated_as_missing() -> None:
    # FRED encodes missing values as "."; if every observation is null,
    # we should treat the series as unavailable.
    payloads = {
        fred_breadth.SERIES_HY_OAS: _series_payload([None, None, None, None]),
        fred_breadth.SERIES_IG_OAS: _flat_payload(1.0, 22),
        fred_breadth.SERIES_2S10S: _flat_payload(0.1, 22),
        fred_breadth.SERIES_BROAD_DOLLAR: _flat_payload(120.0, 22),
    }
    with patch.object(fred.requests, "get", side_effect=_series_router(payloads)):
        snap = fred_breadth.fetch_breadth_snapshot()
    assert snap["hy_oas"] is None


def test_short_history_change_is_none_but_current_set() -> None:
    # Only 5 observations — not enough for a 30d (21-obs) change.
    payloads = {
        fred_breadth.SERIES_HY_OAS: _flat_payload(3.42, 5),
        fred_breadth.SERIES_IG_OAS: _flat_payload(1.0, 5),
        fred_breadth.SERIES_2S10S: _flat_payload(0.1, 5),
        fred_breadth.SERIES_BROAD_DOLLAR: _flat_payload(120.0, 5),
    }
    with patch.object(fred.requests, "get", side_effect=_series_router(payloads)):
        snap = fred_breadth.fetch_breadth_snapshot()
    assert snap["hy_oas"]["current"] == pytest.approx(3.42)
    # Less than 21 bars lookback => change_30d_bp is None.
    assert snap["hy_oas"]["change_30d_bp"] is None
    # hy_stress flag defaults to False when the change is None.
    assert snap["regime_flags"]["hy_stress"] is False


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------


def test_snapshot_cached_between_calls() -> None:
    payloads = {
        fred_breadth.SERIES_HY_OAS: _flat_payload(3.0, 22),
        fred_breadth.SERIES_IG_OAS: _flat_payload(1.0, 22),
        fred_breadth.SERIES_2S10S: _flat_payload(0.1, 22),
        fred_breadth.SERIES_BROAD_DOLLAR: _flat_payload(120.0, 22),
    }
    with patch.object(
        fred.requests, "get", side_effect=_series_router(payloads)
    ) as mg:
        a = fred_breadth.fetch_breadth_snapshot()
        b = fred_breadth.fetch_breadth_snapshot()

    assert a == b
    # First call fetches 4 series, second hits the snapshot cache.
    # (The fred raw-series cache would also short-circuit, but the
    # snapshot cache is checked first.)
    assert mg.call_count == 4


def test_cache_dir_override_isolates_state(tmp_path: Path) -> None:
    """Passing a ``cache_dir`` should not pollute the module-level cache."""
    payloads = {
        fred_breadth.SERIES_HY_OAS: _flat_payload(3.0, 22),
        fred_breadth.SERIES_IG_OAS: _flat_payload(1.0, 22),
        fred_breadth.SERIES_2S10S: _flat_payload(0.1, 22),
        fred_breadth.SERIES_BROAD_DOLLAR: _flat_payload(120.0, 22),
    }
    isolated = tmp_path / "isolated"
    with patch.object(fred.requests, "get", side_effect=_series_router(payloads)):
        snap = fred_breadth.fetch_breadth_snapshot(cache_dir=isolated)
    assert isinstance(snap, dict)
    # The override directory got something written to it.
    assert any(isolated.rglob("*.json"))


# ---------------------------------------------------------------------------
# Percentile
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# as_of: historical snapshot computation
# ---------------------------------------------------------------------------


def test_as_of_none_matches_default_snapshot() -> None:
    # Default (as_of=None) must be identical to the no-arg call.
    payloads = {
        fred_breadth.SERIES_HY_OAS: _flat_payload(3.42, 60),
        fred_breadth.SERIES_IG_OAS: _flat_payload(1.02, 60),
        fred_breadth.SERIES_2S10S: _flat_payload(0.45, 60),
        fred_breadth.SERIES_BROAD_DOLLAR: _flat_payload(121.3, 60),
    }
    with patch.object(fred.requests, "get", side_effect=_series_router(payloads)):
        default_snap = fred_breadth.fetch_breadth_snapshot()

    # Clear caches so both calls have to rebuild from scratch.
    fred_breadth._cache.clear()
    fred._cache.clear()

    with patch.object(fred.requests, "get", side_effect=_series_router(payloads)):
        explicit_none = fred_breadth.fetch_breadth_snapshot(as_of=None)

    assert default_snap == explicit_none
    assert default_snap["as_of"] == _dt.date.today().isoformat()


def test_as_of_echoes_through_to_snapshot_date() -> None:
    # The ``as_of`` field of the returned dict reflects the passed date.
    payloads = {
        fred_breadth.SERIES_HY_OAS: _ramp_payload(
            3.00, 3.50, 22, end_date="2026-04-01"
        ),
        fred_breadth.SERIES_IG_OAS: _flat_payload(1.0, 22, end_date="2026-04-01"),
        fred_breadth.SERIES_2S10S: _flat_payload(0.1, 22, end_date="2026-04-01"),
        fred_breadth.SERIES_BROAD_DOLLAR: _flat_payload(
            120.0, 22, end_date="2026-04-01"
        ),
    }
    with patch.object(fred.requests, "get", side_effect=_series_router(payloads)):
        snap = fred_breadth.fetch_breadth_snapshot(as_of="2026-04-01")
    assert snap["as_of"] == "2026-04-01"


def test_as_of_anchors_30d_change_to_historical_date() -> None:
    # Build a series whose ramp ENDS on 2026-04-01 (so the latest visible
    # value when as_of=2026-04-01 is 3.50, and 21 obs prior is 3.00).
    # If we run with as_of=2026-04-01, the 30d Δ should be +50bp anchored
    # off the 2026-04-01 / ~2026-03-02 pair.  If as_of were ignored and the
    # function reached past wall-clock today, the math would be different
    # (or absent).
    payloads = {
        fred_breadth.SERIES_HY_OAS: _ramp_payload(
            3.00, 3.50, 22, end_date="2026-04-01"
        ),
        fred_breadth.SERIES_IG_OAS: _flat_payload(1.0, 22, end_date="2026-04-01"),
        fred_breadth.SERIES_2S10S: _flat_payload(0.1, 22, end_date="2026-04-01"),
        fred_breadth.SERIES_BROAD_DOLLAR: _flat_payload(
            120.0, 22, end_date="2026-04-01"
        ),
    }
    with patch.object(fred.requests, "get", side_effect=_series_router(payloads)):
        snap = fred_breadth.fetch_breadth_snapshot(as_of="2026-04-01")

    # latest (2026-04-01) -> 3.50, lookback (~2026-03-02 / +21 calendar days
    # earlier in this construction) -> 3.00.  Δ = +0.50% = +50bp.
    assert snap["hy_oas"]["current"] == pytest.approx(3.50)
    assert snap["hy_oas"]["change_30d_bp"] == pytest.approx(50.0, abs=0.5)


def test_as_of_in_middle_of_series_trims_future_observations() -> None:
    # Build a 60-bar ramp ending in mid-2026; query as_of partway through
    # and confirm the snapshot's "current" matches the ramp at that point
    # rather than the very last bar.
    end_date = "2026-06-30"
    bars = 60
    payloads = {
        fred_breadth.SERIES_HY_OAS: _ramp_payload(2.0, 5.0, bars, end_date=end_date),
        fred_breadth.SERIES_IG_OAS: _flat_payload(1.0, bars, end_date=end_date),
        fred_breadth.SERIES_2S10S: _flat_payload(0.1, bars, end_date=end_date),
        fred_breadth.SERIES_BROAD_DOLLAR: _ramp_payload(
            100.0, 130.0, bars, end_date=end_date
        ),
    }
    # The ramp linearly goes 2.0 -> 5.0 across 60 days ending 2026-06-30.
    # Pick as_of partway through and compute the expected "current" value.
    end = _dt.date.fromisoformat(end_date)
    as_of_date = (end - _dt.timedelta(days=30)).isoformat()
    # Step size + index of as_of in the ramp (60 bars, indexed 0..59):
    # index_of(as_of) = 59 - 30 = 29.  value = 2.0 + (3.0/59)*29.
    expected_current = 2.0 + (3.0 / 59) * 29

    with patch.object(fred.requests, "get", side_effect=_series_router(payloads)):
        snap = fred_breadth.fetch_breadth_snapshot(as_of=as_of_date)

    assert snap["hy_oas"]["current"] == pytest.approx(expected_current, abs=1e-3)


def test_as_of_2y_percentile_windowed_to_anchor_not_today() -> None:
    # Build a series that has two distinct "regimes": low values in
    # 2024 and high values in 2026.  If the percentile window is correctly
    # anchored to ``as_of=2024-12-31``, the window will only see the low
    # regime and the latest 2024 observation will be a high-rank value
    # within that window.  If the window were anchored to today (2026)
    # it would see both regimes — same input, different rank.

    # Construct: 100 calendar days ending 2024-12-31 with values ramping
    # 1.0 -> 2.0, then a long gap with no obs (so the window is just the
    # 2024 stretch when as_of=2024-12-31).
    payloads = {
        fred_breadth.SERIES_HY_OAS: _ramp_payload(
            1.0, 2.0, 100, end_date="2024-12-31"
        ),
        fred_breadth.SERIES_IG_OAS: _flat_payload(1.0, 22, end_date="2024-12-31"),
        fred_breadth.SERIES_2S10S: _flat_payload(0.1, 22, end_date="2024-12-31"),
        fred_breadth.SERIES_BROAD_DOLLAR: _ramp_payload(
            100.0, 110.0, 100, end_date="2024-12-31"
        ),
    }
    with patch.object(fred.requests, "get", side_effect=_series_router(payloads)):
        snap = fred_breadth.fetch_breadth_snapshot(as_of="2024-12-31")

    # The most recent visible HY value (2.0) is the maximum in the trimmed
    # series, so its percentile within the anchored 2y window must be 1.0.
    assert snap["hy_oas"]["percentile_2y"] == pytest.approx(1.0)
    # Sanity: the anchored "current" matches the ramp's end.
    assert snap["hy_oas"]["current"] == pytest.approx(2.0)


def test_as_of_cache_isolated_from_default_snapshot() -> None:
    # Hitting the function twice with the same as_of must reuse the
    # snapshot cache (no second round of fetches).  And the as_of-specific
    # entry must not contaminate the default ("today") entry — they live
    # under different cache keys.
    payloads = {
        fred_breadth.SERIES_HY_OAS: _flat_payload(3.0, 22, end_date="2026-04-01"),
        fred_breadth.SERIES_IG_OAS: _flat_payload(1.0, 22, end_date="2026-04-01"),
        fred_breadth.SERIES_2S10S: _flat_payload(0.1, 22, end_date="2026-04-01"),
        fred_breadth.SERIES_BROAD_DOLLAR: _flat_payload(
            120.0, 22, end_date="2026-04-01"
        ),
    }
    with patch.object(
        fred.requests, "get", side_effect=_series_router(payloads)
    ) as mg:
        a = fred_breadth.fetch_breadth_snapshot(as_of="2026-04-01")
        b = fred_breadth.fetch_breadth_snapshot(as_of="2026-04-01")

    assert a == b
    # Second call short-circuits via the snapshot cache.
    assert mg.call_count == 4
    assert a["as_of"] == "2026-04-01"


def test_as_of_before_any_data_returns_well_formed_empty_snapshot() -> None:
    # as_of earlier than the first observation -> every series filtered
    # empty -> blocks are None but the snapshot still has the right shape.
    payloads = {
        fred_breadth.SERIES_HY_OAS: _flat_payload(3.0, 22, end_date="2026-04-01"),
        fred_breadth.SERIES_IG_OAS: _flat_payload(1.0, 22, end_date="2026-04-01"),
        fred_breadth.SERIES_2S10S: _flat_payload(0.1, 22, end_date="2026-04-01"),
        fred_breadth.SERIES_BROAD_DOLLAR: _flat_payload(
            120.0, 22, end_date="2026-04-01"
        ),
    }
    with patch.object(fred.requests, "get", side_effect=_series_router(payloads)):
        snap = fred_breadth.fetch_breadth_snapshot(as_of="2020-01-01")

    assert snap["as_of"] == "2020-01-01"
    assert snap["hy_oas"] is None
    assert snap["ig_oas"] is None
    assert snap["yield_curve_2s10s"] is None
    assert snap["broad_dollar"] is None
    assert snap["data_quality"] == {"sources_ok": 0, "sources_failed": 4}


def test_percentile_within_unit_interval() -> None:
    payloads = {
        fred_breadth.SERIES_HY_OAS: _ramp_payload(2.0, 5.0, 60),
        fred_breadth.SERIES_IG_OAS: _flat_payload(1.0, 22),
        fred_breadth.SERIES_2S10S: _flat_payload(0.1, 22),
        fred_breadth.SERIES_BROAD_DOLLAR: _ramp_payload(100.0, 130.0, 60),
    }
    with patch.object(fred.requests, "get", side_effect=_series_router(payloads)):
        snap = fred_breadth.fetch_breadth_snapshot()

    for block in (snap["hy_oas"], snap["broad_dollar"]):
        p = block["percentile_2y"]
        assert p is not None
        assert 0.0 <= p <= 1.0
    # The most recent observation is the maximum in a strictly-increasing
    # series, so its percentile should be 1.0.
    assert snap["hy_oas"]["percentile_2y"] == pytest.approx(1.0)
    assert snap["broad_dollar"]["percentile_2y"] == pytest.approx(1.0)
