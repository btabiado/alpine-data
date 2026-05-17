"""Tests for lthcs.sources.google_trends — cached-reader for the weekly batch."""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from lthcs.sources import google_trends as gt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso_week_str(d: _dt.date) -> str:
    iso = d.isocalendar()
    return f"{iso[0]:04d}-W{iso[1]:02d}"


def _current_week_str() -> str:
    return _iso_week_str(_dt.date.today())


def _weeks_ago_str(weeks: int) -> str:
    return _iso_week_str(_dt.date.today() - _dt.timedelta(weeks=weeks))


def _write_snapshot(
    tmp_path: Path,
    week: str,
    tickers: Dict[str, Any],
    as_of: str = "2026-05-17",
) -> Path:
    """Write a snapshot JSON in tmp_path/trends/<week>.json and return its directory."""
    trends_dir = tmp_path / "trends"
    trends_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "week": week,
        "as_of": as_of,
        "term_map": {t: f"{t}_term" for t in tickers},
        "tickers": tickers,
    }
    (trends_dir / f"{week}.json").write_text(json.dumps(payload), encoding="utf-8")
    return trends_dir


def _make_series(start: float, end: float, n: int) -> List[float]:
    """Linearly interpolated series of length ``n`` from ``start`` to ``end``."""
    if n == 1:
        return [float(start)]
    step = (end - start) / (n - 1)
    return [round(start + i * step, 4) for i in range(n)]


# ---------------------------------------------------------------------------
# resolve_search_term + TICKER_TO_TREND_TERM
# ---------------------------------------------------------------------------


def test_resolve_search_term_uses_topic_id_for_megacaps() -> None:
    # Top-5 megacaps must have hand-curated topic IDs.
    for ticker in ("AAPL", "MSFT", "NVDA", "GOOGL", "AMZN"):
        term = gt.resolve_search_term(ticker)
        assert term.startswith("/m/"), f"{ticker}: expected topic ID, got {term!r}"
        assert gt.TICKER_TO_TREND_TERM[ticker] == term


def test_resolve_search_term_fallback_for_unknown_ticker() -> None:
    # An obscure ticker should fall back to "<TICKER> stock" — disambiguates
    # short ambiguous symbols while losing cultural-search interest.
    assert gt.resolve_search_term("ZTS") == "ZTS stock"
    assert gt.resolve_search_term("zts") == "ZTS stock"  # case-insensitive
    assert gt.resolve_search_term("") == ""
    assert gt.resolve_search_term("   ") == ""


# ---------------------------------------------------------------------------
# get_trends_acceleration — happy paths
# ---------------------------------------------------------------------------


def test_get_trends_acceleration_full_history_returns_good_quality(
    tmp_path: Path,
) -> None:
    """13+ weeks of data -> both 4w and 12w deltas are computed and quality=good."""
    week = _current_week_str()
    series = _make_series(45, 78, 13)  # 13 weeks: idx 0 = 12w ago, idx 8 = 4w ago, idx 12 = latest
    _write_snapshot(tmp_path, week, {"NVDA": {"series": series, "term": "/m/04rn9k"}})

    acc = gt.get_trends_acceleration("NVDA", cache_dir=tmp_path / "trends")
    assert acc is not None
    assert acc["ticker"] == "NVDA"
    assert acc["trend_week"] == week
    assert acc["data_quality"] == "good"
    assert acc["search_interest_latest"] == 78
    # search_interest_12w_ago is the value 12 indices before the last (i.e. series[0]).
    assert acc["search_interest_12w_ago"] == 45
    # acceleration_12w_pct = (78-45)/45 * 100 = 73.33
    assert acc["acceleration_12w_pct"] == pytest.approx(73.33, abs=0.05)


def test_get_trends_acceleration_4w_pct_math(tmp_path: Path) -> None:
    """Verify the 4w % calc against a concrete fixture."""
    week = _current_week_str()
    # 13 entries; series[-5] (4 weeks ago) is the comparison value.
    series = [10.0] * 8 + [62.0] + [70.0, 72.0, 75.0, 78.0]  # latest=78, 4w-ago=62
    _write_snapshot(tmp_path, week, {"NVDA": {"series": series}})

    acc = gt.get_trends_acceleration("NVDA", cache_dir=tmp_path / "trends")
    assert acc is not None
    assert acc["search_interest_latest"] == 78
    assert acc["search_interest_4w_ago"] == 62
    # (78-62)/62 = 0.2580 -> 25.81%
    assert acc["acceleration_4w_pct"] == pytest.approx(25.81, abs=0.05)


# ---------------------------------------------------------------------------
# Regime classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "acc_pct,expected_regime",
    [
        (60.0, "surging"),       # > +50
        (50.01, "surging"),
        (50.0, "accelerating"),  # boundary: == 50 falls into "accelerating"
        (25.0, "accelerating"),  # > +15
        (15.0, "stable"),        # == +15 -> stable
        (0.0, "stable"),
        (-15.0, "stable"),       # == -15 -> stable
        (-15.01, "fading"),
        (-25.0, "fading"),
        (-50.0, "collapsing"),
        (-75.0, "collapsing"),
    ],
)
def test_regime_classification_boundaries(acc_pct: float, expected_regime: str) -> None:
    assert gt._classify_regime(acc_pct) == expected_regime


def test_regime_classification_handles_none() -> None:
    assert gt._classify_regime(None) == "unknown"


# ---------------------------------------------------------------------------
# Signal-score compression
# ---------------------------------------------------------------------------


def test_signal_score_in_bounded_range(tmp_path: Path) -> None:
    """signal_score must always fall in [-1, +1]."""
    week = _current_week_str()
    # Huge spike: latest=99, 4w-ago=1 -> +9800%
    series = [1.0] * 8 + [1.0] + [50.0, 80.0, 95.0, 99.0]
    _write_snapshot(tmp_path, week, {"NVDA": {"series": series}})
    acc = gt.get_trends_acceleration("NVDA", cache_dir=tmp_path / "trends")
    assert acc is not None
    assert -1.0 <= acc["signal_score"] <= 1.0
    assert acc["signal_score"] > 0.99  # near upper saturation


# ---------------------------------------------------------------------------
# Sparse history (partial quality)
# ---------------------------------------------------------------------------


def test_get_trends_acceleration_partial_history(tmp_path: Path) -> None:
    """4 weeks of data: 4w-pct computable, 12w-pct=None, quality=partial."""
    week = _current_week_str()
    series = [50.0, 55.0, 60.0, 65.0, 70.0]  # 5 entries — latest plus 4 weeks back
    _write_snapshot(tmp_path, week, {"AAPL": {"series": series}})

    acc = gt.get_trends_acceleration("AAPL", cache_dir=tmp_path / "trends")
    assert acc is not None
    assert acc["data_quality"] == "partial"
    assert acc["search_interest_latest"] == 70
    assert acc["search_interest_4w_ago"] == 50
    assert acc["search_interest_12w_ago"] is None
    assert acc["acceleration_4w_pct"] == pytest.approx(40.0, abs=0.05)
    assert acc["acceleration_12w_pct"] is None


def test_get_trends_acceleration_very_sparse_history(tmp_path: Path) -> None:
    """Only 2 weeks of data: neither 4w nor 12w computable; still returns with partial."""
    week = _current_week_str()
    _write_snapshot(tmp_path, week, {"AAPL": {"series": [40.0, 50.0]}})

    acc = gt.get_trends_acceleration("AAPL", cache_dir=tmp_path / "trends")
    assert acc is not None
    assert acc["data_quality"] == "partial"
    assert acc["acceleration_4w_pct"] is None
    assert acc["acceleration_12w_pct"] is None
    assert acc["regime"] == "unknown"


# ---------------------------------------------------------------------------
# Stale data
# ---------------------------------------------------------------------------


def test_get_trends_acceleration_stale_snapshot(tmp_path: Path) -> None:
    """Snapshot >3 ISO weeks old marks data_quality='stale' but still returns numbers."""
    stale_week = _weeks_ago_str(5)  # 5 weeks ago = stale
    series = _make_series(45, 78, 13)
    _write_snapshot(tmp_path, stale_week, {"NVDA": {"series": series}})

    acc = gt.get_trends_acceleration("NVDA", cache_dir=tmp_path / "trends")
    assert acc is not None
    assert acc["data_quality"] == "stale"
    # Values are still computed.
    assert acc["acceleration_4w_pct"] is not None
    assert acc["acceleration_12w_pct"] is not None


def test_get_trends_acceleration_recent_snapshot_not_stale(tmp_path: Path) -> None:
    """A snapshot 2 ISO weeks old is still 'good' (boundary check)."""
    week = _weeks_ago_str(2)
    series = _make_series(45, 78, 13)
    _write_snapshot(tmp_path, week, {"NVDA": {"series": series}})

    acc = gt.get_trends_acceleration("NVDA", cache_dir=tmp_path / "trends")
    assert acc is not None
    assert acc["data_quality"] == "good"


# ---------------------------------------------------------------------------
# Missing ticker / no snapshot
# ---------------------------------------------------------------------------


def test_get_trends_acceleration_no_snapshot_returns_none(tmp_path: Path) -> None:
    """Empty trends directory -> None."""
    (tmp_path / "trends").mkdir()
    acc = gt.get_trends_acceleration("NVDA", cache_dir=tmp_path / "trends")
    assert acc is None


def test_get_trends_acceleration_missing_ticker_returns_none(tmp_path: Path) -> None:
    """Snapshot exists but doesn't include the requested ticker -> None."""
    week = _current_week_str()
    _write_snapshot(tmp_path, week, {"AAPL": {"series": [50.0, 60.0, 70.0]}})
    acc = gt.get_trends_acceleration("NVDA", cache_dir=tmp_path / "trends")
    assert acc is None


def test_get_trends_acceleration_picks_most_recent_snapshot(tmp_path: Path) -> None:
    """When multiple snapshots exist, the newest week wins."""
    old_week = _weeks_ago_str(2)
    new_week = _current_week_str()
    _write_snapshot(tmp_path, old_week, {"NVDA": {"series": [10.0] * 13}})
    _write_snapshot(tmp_path, new_week, {"NVDA": {"series": [99.0] * 13}})

    acc = gt.get_trends_acceleration("NVDA", cache_dir=tmp_path / "trends")
    assert acc is not None
    assert acc["trend_week"] == new_week
    assert acc["search_interest_latest"] == 99


# ---------------------------------------------------------------------------
# get_universe_trends_acceleration
# ---------------------------------------------------------------------------


def test_get_universe_trends_acceleration_aggregates(tmp_path: Path) -> None:
    """Returns {ticker: acc_dict} for all tickers with cached series; drops misses."""
    week = _current_week_str()
    _write_snapshot(
        tmp_path,
        week,
        {
            "NVDA": {"series": _make_series(45, 78, 13)},
            "MSFT": {"series": _make_series(60, 65, 13)},
        },
    )
    out = gt.get_universe_trends_acceleration(
        ["NVDA", "MSFT", "ZZZ_MISSING"], cache_dir=tmp_path / "trends"
    )
    assert set(out.keys()) == {"NVDA", "MSFT"}
    assert out["NVDA"]["data_quality"] == "good"
    assert out["MSFT"]["data_quality"] == "good"


def test_get_universe_trends_acceleration_empty_input() -> None:
    assert gt.get_universe_trends_acceleration([]) == {}


def test_get_universe_trends_acceleration_no_snapshot(tmp_path: Path) -> None:
    """Missing cache dir -> empty dict, not exception."""
    out = gt.get_universe_trends_acceleration(["AAPL"], cache_dir=tmp_path / "no_such")
    assert out == {}


# ---------------------------------------------------------------------------
# Tolerant series formats
# ---------------------------------------------------------------------------


def test_get_trends_acceleration_accepts_raw_list_blob(tmp_path: Path) -> None:
    """Per-ticker blob may be a bare list (legacy shape) instead of a dict."""
    week = _current_week_str()
    series = _make_series(40, 60, 13)
    _write_snapshot(tmp_path, week, {"NVDA": series})  # raw list, not dict
    acc = gt.get_trends_acceleration("NVDA", cache_dir=tmp_path / "trends")
    assert acc is not None
    assert acc["search_interest_latest"] == 60


def test_get_trends_acceleration_ticker_case_insensitive(tmp_path: Path) -> None:
    week = _current_week_str()
    _write_snapshot(tmp_path, week, {"NVDA": {"series": _make_series(45, 78, 13)}})
    acc = gt.get_trends_acceleration("nvda", cache_dir=tmp_path / "trends")
    assert acc is not None
    assert acc["ticker"] == "NVDA"
