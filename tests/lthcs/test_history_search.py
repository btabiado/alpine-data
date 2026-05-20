"""Tests for scripts/lthcs_history_search.py (Phase 5 ZETA).

The CLI is a thin wrapper around three pure functions; we exercise them
directly with a synthetic on-disk fixture so the tests are deterministic
and don't depend on whatever is currently in data/lthcs/history/.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import lthcs_history_search as hs


def _write_ticker(root: Path, ticker: str, snapshots) -> None:
    """Write a by_ticker JSON file in the same shape the pipeline emits.

    Source files are stored descending-by-date; the loader normalizes to
    ascending. Pass ``snapshots`` in any order — we shuffle on write to
    prove the normalization happens.
    """
    payload = {
        "ticker": ticker,
        "model_version": "test-v0.1",
        # Reverse on write to mimic descending-by-date storage in prod.
        "history": list(reversed(snapshots)),
    }
    base = root / "history" / "by_ticker"
    base.mkdir(parents=True, exist_ok=True)
    (base / f"{ticker}.json").write_text(json.dumps(payload))


@pytest.fixture()
def data_root(tmp_path: Path) -> Path:
    # AAPL: never elite, mostly weakening with a 3-day monitor blip.
    _write_ticker(tmp_path, "AAPL", [
        {"date": "2026-05-10", "score": 40.0, "band": "weakening"},
        {"date": "2026-05-11", "score": 41.0, "band": "weakening"},
        {"date": "2026-05-12", "score": 46.0, "band": "monitor"},
        {"date": "2026-05-13", "score": 47.0, "band": "monitor"},
        {"date": "2026-05-14", "score": 46.5, "band": "monitor"},
        {"date": "2026-05-15", "score": 43.0, "band": "weakening"},
    ])
    # NVDA: 4-day elite streak then high_confidence.
    _write_ticker(tmp_path, "NVDA", [
        {"date": "2026-05-10", "score": 88.0, "band": "elite"},
        {"date": "2026-05-11", "score": 90.0, "band": "elite"},
        {"date": "2026-05-12", "score": 91.0, "band": "elite"},
        {"date": "2026-05-13", "score": 87.0, "band": "elite"},
        {"date": "2026-05-14", "score": 80.0, "band": "high_confidence"},
        {"date": "2026-05-15", "score": 78.0, "band": "high_confidence"},
    ])
    # MU: 2 elite days only on the most recent day (window=1 should catch it).
    _write_ticker(tmp_path, "MU", [
        {"date": "2026-05-14", "score": 84.0, "band": "high_confidence"},
        {"date": "2026-05-15", "score": 86.0, "band": "elite"},
    ])
    return tmp_path


def test_ticker_mode_returns_events_in_chronological_order(data_root):
    out = hs.search_by_ticker("AAPL", data_root)
    assert out["ticker"] == "AAPL"
    assert out["snapshots"] == 6
    assert out["first_date"] == "2026-05-10"
    assert out["last_date"] == "2026-05-15"
    # Three runs: weakening(2) -> monitor(3) -> weakening(1).
    assert [e["band"] for e in out["events"]] == ["weakening", "monitor", "weakening"]
    assert [e["days"] for e in out["events"]] == [2, 3, 1]


def test_ticker_mode_missing_history_is_graceful(data_root):
    out = hs.search_by_ticker("ZZZZ", data_root)
    assert out == {"ticker": "ZZZZ", "snapshots": 0, "events": []}


def test_band_search_all_time_groups_and_sorts_by_days(data_root):
    out = hs.search_by_band("elite", "all", data_root)
    tickers = [r["ticker"] for r in out["rows"]]
    days = {r["ticker"]: r["days"] for r in out["rows"]}
    assert tickers == ["NVDA", "MU"]  # NVDA(4) > MU(1)
    assert days == {"NVDA": 4, "MU": 1}


def test_band_search_window_excludes_old_hits(data_root):
    # Universe latest = 2026-05-15. window=1 -> only that single day.
    out = hs.search_by_band("elite", "1", data_root)
    assert out["window_start"] == "2026-05-15"
    tickers = [r["ticker"] for r in out["rows"]]
    # Only MU's 2026-05-15 elite day falls in window; NVDA's elite ended 5/13.
    assert tickers == ["MU"]


def test_band_search_unknown_band_raises(data_root):
    with pytest.raises(ValueError):
        hs.search_by_band("not_a_band", "all", data_root)


def test_streak_mode_ranks_per_band(data_root):
    out = hs.search_by_streak(top=10, data_root=data_root)
    elite = out["by_band"]["elite"]
    assert elite[0]["ticker"] == "NVDA"
    assert elite[0]["days"] == 4
    assert elite[0]["start"] == "2026-05-10"
    assert elite[0]["end"] == "2026-05-13"
    # MU's single elite day shows up as the runner-up.
    assert elite[1] == {"ticker": "MU", "days": 1, "start": "2026-05-15", "end": "2026-05-15"}
    # Bands with no hits are present but empty.
    assert out["by_band"]["review"] == []


def test_streak_mode_top_n_limits_results(data_root):
    out = hs.search_by_streak(top=1, data_root=data_root)
    assert len(out["by_band"]["elite"]) == 1
