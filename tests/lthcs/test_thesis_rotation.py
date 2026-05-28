"""Tests for ``lthcs.sources.thesis_rotation``.

Every test uses ``tmp_path`` as the data root so we never touch the real
``data/lthcs/`` tree. Mirrors the style of ``tests/lthcs/test_persist.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List

import pytest

from lthcs.sources.thesis_rotation import (
    ThesisRotation,
    get_default_data_root,
)


MODEL_VERSION = "v1.0.0"
TODAY = "2026-05-16"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def rot(tmp_path: Path) -> ThesisRotation:
    return ThesisRotation(tmp_path, model_version=MODEL_VERSION)


def _make_universe(n: int) -> List[str]:
    """Synthesise an N-ticker universe ``T000..T###`` (zero-padded so the
    alphabetic sort order is predictable)."""
    return ["T%03d" % i for i in range(n)]


# ---------------------------------------------------------------------------
# Construction / paths
# ---------------------------------------------------------------------------

def test_init_creates_sentiment_directory(tmp_path: Path) -> None:
    ThesisRotation(tmp_path)
    assert (tmp_path / "sentiment").is_dir()


def test_init_is_idempotent(tmp_path: Path) -> None:
    ThesisRotation(tmp_path)
    # Second construction should not raise even though sentiment/ already exists.
    ThesisRotation(tmp_path)
    assert (tmp_path / "sentiment").is_dir()


def test_state_path_and_sentiment_path(rot: ThesisRotation, tmp_path: Path) -> None:
    assert rot.state_path() == tmp_path / "thesis_rotation.json"
    assert rot.sentiment_path("AAPL") == tmp_path / "sentiment" / "AAPL.json"


def test_get_default_data_root_points_into_repo() -> None:
    root = get_default_data_root()
    assert isinstance(root, Path)
    # Should resolve to <repo>/data/lthcs/ — and the lthcs package
    # should be a sibling of data/.
    assert root.name == "lthcs"
    assert root.parent.name == "data"
    assert (root.parent.parent / "lthcs" / "sources" / "thesis_rotation.py").exists()


def test_sentiment_path_rejects_empty_ticker(rot: ThesisRotation) -> None:
    with pytest.raises(ValueError):
        rot.sentiment_path("")
    with pytest.raises(ValueError):
        rot.sentiment_path("   ")


# ---------------------------------------------------------------------------
# load_state / save_state
# ---------------------------------------------------------------------------

def test_load_state_returns_empty_when_missing(rot: ThesisRotation) -> None:
    state = rot.load_state()
    assert state == {
        "model_version": MODEL_VERSION,
        "last_updated": None,
        "tickers": {},
    }


def test_save_and_load_state_round_trip(rot: ThesisRotation) -> None:
    state = {
        "model_version": MODEL_VERSION,
        "last_updated": TODAY,
        "tickers": {"AAPL": {"last_scored": TODAY}},
    }
    rot.save_state(state)
    assert rot.state_path().exists()
    loaded = rot.load_state()
    assert loaded == state


def test_load_state_tolerates_malformed_file(rot: ThesisRotation) -> None:
    rot.state_path().write_text("{not valid json", encoding="utf-8")
    # Should recover by returning the empty shell, not raise.
    state = rot.load_state()
    assert state["tickers"] == {}
    assert state["last_updated"] is None


# ---------------------------------------------------------------------------
# select_tickers_for_today
# ---------------------------------------------------------------------------

def test_select_fresh_universe_returns_first_25_alphabetic(rot: ThesisRotation) -> None:
    universe = _make_universe(74)
    picked = rot.select_tickers_for_today(universe, today=TODAY)
    assert len(picked) == 25
    # All tickers were never scored, so the picks are simply the first 25
    # alphabetic entries.
    assert picked == universe[:25]


def test_select_after_recording_returns_different_tickers(rot: ThesisRotation) -> None:
    universe = _make_universe(74)
    first = rot.select_tickers_for_today(universe, today=TODAY)
    for t in first:
        rot.record_scored(t, today=TODAY)
    # Same day, next batch should completely avoid the already-scored ones.
    second = rot.select_tickers_for_today(universe, today=TODAY)
    assert len(second) == 25
    assert set(second).isdisjoint(set(first))
    assert second == universe[25:50]


def test_select_returns_empty_when_all_scored_today(rot: ThesisRotation) -> None:
    universe = _make_universe(74)
    for t in universe:
        rot.record_scored(t, today=TODAY)
    picked = rot.select_tickers_for_today(universe, today=TODAY)
    assert picked == []


def test_select_prefers_older_dates(rot: ThesisRotation) -> None:
    # Build a tiny universe with hand-set last_scored dates.
    universe = ["AAA", "BBB", "CCC", "DDD"]
    state = {
        "model_version": MODEL_VERSION,
        "last_updated": "2026-05-15",
        "tickers": {
            "AAA": {"last_scored": "2026-05-15"},  # most recent
            "BBB": {"last_scored": "2026-05-10"},  # oldest
            "CCC": {"last_scored": "2026-05-13"},
            "DDD": {"last_scored": "2026-05-12"},
        },
    }
    rot.save_state(state)
    picked = rot.select_tickers_for_today(universe, today=TODAY, budget=4)
    # Oldest first: BBB (5-10), DDD (5-12), CCC (5-13), AAA (5-15).
    assert picked == ["BBB", "DDD", "CCC", "AAA"]


def test_select_never_scored_outranks_any_dated(rot: ThesisRotation) -> None:
    # NEW has never been scored; the other two are scored but still days old.
    universe = ["NEW", "OLD_A", "OLD_B"]
    state = {
        "model_version": MODEL_VERSION,
        "last_updated": "2026-05-15",
        "tickers": {
            "OLD_A": {"last_scored": "2026-05-01"},  # very old
            "OLD_B": {"last_scored": "2026-05-02"},
            # NEW intentionally omitted.
        },
    }
    rot.save_state(state)
    picked = rot.select_tickers_for_today(universe, today=TODAY, budget=3)
    assert picked[0] == "NEW"
    assert picked == ["NEW", "OLD_A", "OLD_B"]


def test_select_null_last_scored_treated_as_never(rot: ThesisRotation) -> None:
    # Explicit null in the file should be equivalent to "never scored".
    universe = ["AAA", "BBB"]
    state = {
        "model_version": MODEL_VERSION,
        "last_updated": "2026-05-15",
        "tickers": {
            "AAA": {"last_scored": "2026-05-01"},
            "BBB": {"last_scored": None},
        },
    }
    rot.save_state(state)
    picked = rot.select_tickers_for_today(universe, today=TODAY, budget=2)
    assert picked == ["BBB", "AAA"]


def test_select_alphabetic_tiebreak_on_same_date(rot: ThesisRotation) -> None:
    universe = ["ZZZZ", "AAAA", "MMMM"]
    state = {
        "model_version": MODEL_VERSION,
        "last_updated": "2026-05-10",
        "tickers": {
            "AAAA": {"last_scored": "2026-05-10"},
            "MMMM": {"last_scored": "2026-05-10"},
            "ZZZZ": {"last_scored": "2026-05-10"},
        },
    }
    rot.save_state(state)
    picked = rot.select_tickers_for_today(universe, today=TODAY, budget=3)
    assert picked == ["AAAA", "MMMM", "ZZZZ"]


def test_select_custom_budget_override(rot: ThesisRotation) -> None:
    universe = _make_universe(74)
    picked = rot.select_tickers_for_today(universe, today=TODAY, budget=5)
    assert len(picked) == 5
    assert picked == universe[:5]


def test_select_zero_or_negative_budget_returns_empty(rot: ThesisRotation) -> None:
    universe = _make_universe(10)
    assert rot.select_tickers_for_today(universe, today=TODAY, budget=0) == []
    assert rot.select_tickers_for_today(universe, today=TODAY, budget=-1) == []


def test_select_handles_new_ticker_added_to_universe(rot: ThesisRotation) -> None:
    # AAA is in the state file, BRAND_NEW is not — it should be picked first
    # because never-scored outranks any dated entry.
    state = {
        "model_version": MODEL_VERSION,
        "last_updated": "2026-05-15",
        "tickers": {"AAA": {"last_scored": "2026-05-15"}},
    }
    rot.save_state(state)
    picked = rot.select_tickers_for_today(
        ["AAA", "BRAND_NEW"], today=TODAY, budget=2
    )
    assert picked == ["BRAND_NEW", "AAA"]


def test_select_does_not_mutate_state(rot: ThesisRotation) -> None:
    universe = _make_universe(30)
    before = rot.load_state()
    rot.select_tickers_for_today(universe, today=TODAY)
    after = rot.load_state()
    assert before == after  # selection is pure
    # And no state file was created as a side effect of selecting.
    assert not rot.state_path().exists()


# ---------------------------------------------------------------------------
# record_scored
# ---------------------------------------------------------------------------

def test_record_scored_updates_state_atomically(rot: ThesisRotation) -> None:
    rot.record_scored("AAPL", today=TODAY)
    state = rot.load_state()
    assert state["tickers"]["AAPL"]["last_scored"] == TODAY
    assert state["last_updated"] == TODAY
    # No lingering temp file.
    leftover = [p.name for p in rot.data_root.iterdir() if p.name.startswith(".tmp-")]
    assert leftover == []


def test_record_scored_idempotent_same_day(rot: ThesisRotation) -> None:
    rot.record_scored("AAPL", today=TODAY)
    rot.record_scored("AAPL", today=TODAY)  # second call same day
    state = rot.load_state()
    # Only the one ticker, still recorded for today — no exceptions.
    assert list(state["tickers"].keys()) == ["AAPL"]
    assert state["tickers"]["AAPL"]["last_scored"] == TODAY


def test_record_scored_rejects_empty_ticker(rot: ThesisRotation) -> None:
    with pytest.raises(ValueError):
        rot.record_scored("", today=TODAY)
    with pytest.raises(ValueError):
        rot.record_scored("   ", today=TODAY)


# ---------------------------------------------------------------------------
# write_sentiment / read_sentiment round trip
# ---------------------------------------------------------------------------

def test_write_and_read_sentiment_round_trip(rot: ThesisRotation) -> None:
    label_counts = {
        "Bearish": 1,
        "Somewhat-Bearish": 5,
        "Neutral": 18,
        "Somewhat-Bullish": 22,
        "Bullish": 4,
    }
    path = rot.write_sentiment(
        "AAPL",
        article_count=50,
        mean_sentiment_score=0.248,
        mean_relevance_score=0.42,
        label_counts=label_counts,
        today=TODAY,
    )
    assert path.exists()
    assert path.name == "AAPL.json"

    payload = rot.read_sentiment("AAPL")
    assert payload is not None
    assert payload["ticker"] == "AAPL"
    assert payload["last_scored"] == TODAY
    assert payload["model_version"] == MODEL_VERSION
    assert payload["article_count"] == 50
    assert payload["mean_sentiment_score"] == pytest.approx(0.248)
    assert payload["mean_relevance_score"] == pytest.approx(0.42)
    assert payload["label_counts"] == label_counts


def test_write_sentiment_allows_none_scores(rot: ThesisRotation) -> None:
    # If AV returned zero articles, the means are None.
    path = rot.write_sentiment(
        "MSFT",
        article_count=0,
        mean_sentiment_score=None,
        mean_relevance_score=None,
        label_counts={},
        today=TODAY,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["mean_sentiment_score"] is None
    assert payload["mean_relevance_score"] is None
    assert payload["label_counts"] == {}


def test_read_sentiment_returns_none_when_missing(rot: ThesisRotation) -> None:
    assert rot.read_sentiment("NOPE") is None


def test_read_sentiment_returns_none_on_malformed_json(rot: ThesisRotation) -> None:
    rot.sentiment_path("BORK").write_text("{not valid json", encoding="utf-8")
    # Must not raise — caller treats None as "no data, please refetch".
    assert rot.read_sentiment("BORK") is None


def test_write_sentiment_no_lingering_tmp_files(rot: ThesisRotation) -> None:
    rot.write_sentiment("AAPL", 10, 0.1, 0.5, {"Neutral": 10}, today=TODAY)
    leftover = [
        p.name for p in rot.sentiment_dir.iterdir() if p.name.startswith(".tmp-")
    ]
    assert leftover == []


# ---------------------------------------------------------------------------
# is_stale
# ---------------------------------------------------------------------------

def test_is_stale_today_is_fresh(rot: ThesisRotation) -> None:
    sent = {"last_scored": TODAY}
    assert rot.is_stale(sent, today=TODAY) is False


def test_is_stale_four_days_ago_is_stale(rot: ThesisRotation) -> None:
    # Default staleness cap is 3 days, so 4 days back should trip it.
    sent = {"last_scored": "2026-05-12"}
    assert rot.is_stale(sent, today=TODAY) is True


def test_is_stale_exactly_three_days_ago_still_fresh(rot: ThesisRotation) -> None:
    # 2026-05-13 is exactly 3 days before TODAY; threshold is "> 3 days".
    sent = {"last_scored": "2026-05-13"}
    assert rot.is_stale(sent, today=TODAY) is False


def test_is_stale_none_is_stale(rot: ThesisRotation) -> None:
    assert rot.is_stale(None, today=TODAY) is True


def test_is_stale_missing_date_is_stale(rot: ThesisRotation) -> None:
    assert rot.is_stale({"ticker": "AAPL"}, today=TODAY) is True


def test_is_stale_unparseable_date_is_stale(rot: ThesisRotation) -> None:
    assert rot.is_stale({"last_scored": "not-a-date"}, today=TODAY) is True


def test_is_stale_custom_threshold(rot: ThesisRotation) -> None:
    # With a 7-day window, 4 days ago is no longer stale.
    sent = {"last_scored": "2026-05-12"}
    assert rot.is_stale(sent, today=TODAY, max_staleness_days=7) is False


# ---------------------------------------------------------------------------
# coverage_stats
# ---------------------------------------------------------------------------

def test_coverage_stats_buckets_sum_to_total(rot: ThesisRotation) -> None:
    universe = ["NEVER", "STALE", "FRESH", "TODAY_T"]
    # NEVER: no file at all.
    rot.write_sentiment("STALE", 10, 0.0, 0.0, {}, today="2026-05-10")  # 6d ago
    rot.write_sentiment("FRESH", 10, 0.0, 0.0, {}, today="2026-05-14")  # 2d ago
    rot.write_sentiment("TODAY_T", 10, 0.0, 0.0, {}, today=TODAY)

    stats = rot.coverage_stats(universe, today=TODAY)
    assert stats == {
        "total": 4,
        "never_scored": 1,
        "stale": 1,
        "fresh": 1,
        "scored_today": 1,
    }
    # And the invariant the brief calls out:
    assert (
        stats["never_scored"]
        + stats["stale"]
        + stats["fresh"]
        + stats["scored_today"]
        == stats["total"]
    )


def test_coverage_stats_empty_universe(rot: ThesisRotation) -> None:
    stats = rot.coverage_stats([], today=TODAY)
    assert stats == {
        "total": 0,
        "never_scored": 0,
        "stale": 0,
        "fresh": 0,
        "scored_today": 0,
    }


# ---------------------------------------------------------------------------
# Three-day full-cycle integration test
# ---------------------------------------------------------------------------

def test_three_day_cycle_covers_full_74_ticker_universe(rot: ThesisRotation) -> None:
    """Simulate three consecutive days: every ticker should be scored at
    least once and the per-day batches must not overlap within a day.
    """
    universe = _make_universe(74)
    days = ["2026-05-14", "2026-05-15", "2026-05-16"]
    scored_by_day = {}
    for day in days:
        picked = rot.select_tickers_for_today(universe, today=day)
        # Each day picks no more than the budget.
        assert len(picked) <= ThesisRotation.DAILY_BUDGET
        scored_by_day[day] = picked
        for t in picked:
            rot.record_scored(t, today=day)
    all_scored = set().union(*scored_by_day.values())
    # 74 tickers / 25 per day -> 3 days covers the whole universe.
    assert all_scored == set(universe)
    # And the daily batches never re-burned quota on already-scored tickers
    # within the same day (trivially true because record_scored is invoked
    # after select). Cross-day overlap IS allowed once 3 days have rolled.


# ---------------------------------------------------------------------------
# Atomic-write resilience (mirrors test_persist.py style)
# ---------------------------------------------------------------------------

def test_save_state_no_lingering_tmp_files(rot: ThesisRotation) -> None:
    rot.save_state(rot.load_state())
    leftover = [
        p.name for p in rot.data_root.iterdir() if p.name.startswith(".tmp-")
    ]
    assert leftover == []


def test_atomic_write_cleans_tmp_on_failure(
    rot: ThesisRotation, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the JSON encoder raises mid-write, the .tmp file is cleaned up
    and the destination file is not created."""
    real_dump = json.dump

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated encode failure")

    monkeypatch.setattr(json, "dump", boom)
    with pytest.raises(RuntimeError):
        rot.write_sentiment(
            "AAPL", 10, 0.1, 0.5, {"Neutral": 10}, today=TODAY
        )
    monkeypatch.setattr(json, "dump", real_dump)

    leftover = [
        p.name for p in rot.sentiment_dir.iterdir() if p.name.startswith(".tmp-")
    ]
    assert leftover == []
    assert not rot.sentiment_path("AAPL").exists()
