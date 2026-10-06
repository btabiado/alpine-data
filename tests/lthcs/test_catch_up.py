"""Tests for ``LthcsPersist.fill_history_gaps`` and ``clear_synthetic_entries``.

The catch-up flag in ``lthcs_daily.py --catch-up`` forward-fills missing
days between each ticker's last real history entry and ``today - 1``.
This file exercises the helper directly with a temporary data root so
the real ``data/lthcs/`` tree is never touched.

Synthetic entries carry ``synthetic: true`` so the front-end chart
can render them differently (hollow markers / dashed line). The
score and band are copied verbatim from the most recent real entry —
the catch-up does NOT recompute pillar scores.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from lthcs.persist import LthcsPersist


MODEL_VERSION = "v1.0.0"


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def store(tmp_path: Path) -> LthcsPersist:
    return LthcsPersist(tmp_path)


def _seed_history(
    store: LthcsPersist,
    ticker: str,
    entries: List[Dict[str, Any]],
) -> Path:
    """Write a raw history payload directly to disk for a ticker.

    Bypasses ``append_history_entry`` so the test can construct exact
    multi-day histories without depending on the append helper's
    sort/truncate behaviour.
    """
    path = store.history_path(ticker)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ticker": ticker,
        "model_version": MODEL_VERSION,
        "history": entries,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _read(store: LthcsPersist, ticker: str) -> Dict[str, Any]:
    return store.read_history(ticker)


# ---------------------------------------------------------------------------
# fill_history_gaps
# ---------------------------------------------------------------------------

def test_no_gap_is_noop(store: LthcsPersist) -> None:
    # Last real entry is yesterday; today's entry will be written by
    # rebuild_history_for_all_tickers, NOT by fill_history_gaps.
    _seed_history(
        store,
        "AAPL",
        [{"date": "2026-05-16", "score": 56.0, "band": "weakening"}],
    )
    written = store.fill_history_gaps(today="2026-05-17")
    assert written == 0
    history = _read(store, "AAPL")["history"]
    # Only the original entry; no synthetic added.
    assert len(history) == 1
    assert history[0]["date"] == "2026-05-16"
    assert "synthetic" not in history[0]


def test_one_day_gap_writes_one_synthetic(store: LthcsPersist) -> None:
    # Last real entry was 2 days before today => fill 1 synthetic for yesterday.
    _seed_history(
        store,
        "AAPL",
        [{"date": "2026-05-15", "score": 56.0, "band": "weakening"}],
    )
    written = store.fill_history_gaps(today="2026-05-17")
    assert written == 1
    history = _read(store, "AAPL")["history"]
    assert len(history) == 2
    # Sorted desc.
    assert history[0]["date"] == "2026-05-16"
    assert history[0]["synthetic"] is True
    assert history[0]["score"] == 56.0
    assert history[0]["band"] == "weakening"
    # Original entry preserved unchanged.
    assert history[1] == {"date": "2026-05-15", "score": 56.0, "band": "weakening"}


def test_five_day_gap_writes_five_synthetics(store: LthcsPersist) -> None:
    # Last real entry 6 days before today => 5 synthetic entries fill
    # the gap (today is written separately by Stage 8).
    _seed_history(
        store,
        "MSFT",
        [{"date": "2026-05-10", "score": 72.0, "band": "constructive"}],
    )
    written = store.fill_history_gaps(today="2026-05-16")
    assert written == 5
    history = _read(store, "MSFT")["history"]
    assert len(history) == 6  # 1 real + 5 synthetic
    # Every synthetic copies the same score + band + flag.
    synths = [r for r in history if r.get("synthetic")]
    assert len(synths) == 5
    for s in synths:
        assert s["score"] == 72.0
        assert s["band"] == "constructive"
        assert s["synthetic"] is True
    # Synthetics cover May 11 .. May 15 (today=May 16 is NOT here).
    dates_filled = sorted(s["date"] for s in synths)
    assert dates_filled == [
        "2026-05-11",
        "2026-05-12",
        "2026-05-13",
        "2026-05-14",
        "2026-05-15",
    ]


def test_multiple_tickers_each_handle_own_gap(store: LthcsPersist) -> None:
    # AAPL has a 2-day gap; MSFT has a 4-day gap; NVDA is up-to-date.
    _seed_history(
        store, "AAPL",
        [{"date": "2026-05-14", "score": 56.0, "band": "weakening"}],
    )
    _seed_history(
        store, "MSFT",
        [{"date": "2026-05-12", "score": 72.0, "band": "constructive"}],
    )
    _seed_history(
        store, "NVDA",
        [{"date": "2026-05-16", "score": 88.0, "band": "high_confidence"}],
    )
    written = store.fill_history_gaps(today="2026-05-17")
    # AAPL +2 (May 15, 16) + MSFT +4 (May 13, 14, 15, 16) + NVDA +0 = 6.
    assert written == 6
    assert len([r for r in _read(store, "AAPL")["history"] if r.get("synthetic")]) == 2
    assert len([r for r in _read(store, "MSFT")["history"] if r.get("synthetic")]) == 4
    assert len([r for r in _read(store, "NVDA")["history"] if r.get("synthetic")]) == 0


def test_empty_history_skipped(store: LthcsPersist) -> None:
    # Ticker with a file but an empty history array — nothing to forward-
    # fill from. The pipeline's rebuild_history_for_all_tickers will write
    # the first real entry later.
    _seed_history(store, "NEWCO", [])
    written = store.fill_history_gaps(today="2026-05-17")
    assert written == 0
    history = _read(store, "NEWCO")["history"]
    assert history == []


def test_weekend_gap_filled_with_calendar_days(store: LthcsPersist) -> None:
    # Friday 2026-05-15 was the last real run. The cron is calendar-day
    # based (not trading-day based), so a Sunday catch-up should fill
    # Saturday too — synthetic, but present, so the chart has no gap.
    _seed_history(
        store, "AAPL",
        [{"date": "2026-05-15", "score": 56.0, "band": "weakening"}],
    )
    # today = Sunday (May 17). Gap to fill: just Saturday May 16.
    written = store.fill_history_gaps(today="2026-05-17")
    assert written == 1
    history = _read(store, "AAPL")["history"]
    saturday = [r for r in history if r["date"] == "2026-05-16"]
    assert len(saturday) == 1
    assert saturday[0]["synthetic"] is True


def test_idempotency_double_run(store: LthcsPersist) -> None:
    # Running --catch-up twice on the same day must not double-write.
    _seed_history(
        store, "AAPL",
        [{"date": "2026-05-13", "score": 56.0, "band": "weakening"}],
    )
    first = store.fill_history_gaps(today="2026-05-17")
    assert first == 3  # May 14, 15, 16
    history_after_first = _read(store, "AAPL")["history"]

    second = store.fill_history_gaps(today="2026-05-17")
    assert second == 0  # nothing new to write
    history_after_second = _read(store, "AAPL")["history"]
    assert history_after_first == history_after_second


def test_catchup_preserves_real_entries(store: LthcsPersist) -> None:
    # Mix of older real entries plus a recent gap. Real rows must remain
    # unchanged (no synthetic marker, no score/band rewrite).
    _seed_history(
        store, "AAPL",
        [
            {"date": "2026-05-13", "score": 56.0, "band": "weakening"},
            {"date": "2026-05-10", "score": 54.0, "band": "weakening"},
            {"date": "2026-05-08", "score": 52.0, "band": "weakening"},
        ],
    )
    store.fill_history_gaps(today="2026-05-15")
    history = _read(store, "AAPL")["history"]
    # Pull out the three real dates and check they're untouched.
    by_date = {row["date"]: row for row in history}
    assert by_date["2026-05-13"] == {"date": "2026-05-13", "score": 56.0, "band": "weakening"}
    assert by_date["2026-05-10"] == {"date": "2026-05-10", "score": 54.0, "band": "weakening"}
    assert by_date["2026-05-08"] == {"date": "2026-05-08", "score": 52.0, "band": "weakening"}
    # And the new May-14 entry is synthetic.
    assert by_date["2026-05-14"]["synthetic"] is True


def test_catchup_carries_latest_score_and_band(store: LthcsPersist) -> None:
    # Make sure the LATEST real entry's score/band is the one forward-
    # filled (not, say, the oldest one).
    _seed_history(
        store, "AAPL",
        [
            {"date": "2026-05-13", "score": 88.0, "band": "high_confidence"},
            {"date": "2026-05-10", "score": 42.0, "band": "review"},
        ],
    )
    store.fill_history_gaps(today="2026-05-15")
    history = _read(store, "AAPL")["history"]
    synth = next(r for r in history if r["date"] == "2026-05-14")
    assert synth["score"] == 88.0
    assert synth["band"] == "high_confidence"


def test_history_stays_sorted_desc_after_fill(store: LthcsPersist) -> None:
    # The append helper keeps history sorted desc; fill_history_gaps
    # must preserve that invariant so downstream consumers (front-end
    # chart) don't see misordered rows.
    _seed_history(
        store, "AAPL",
        [{"date": "2026-05-10", "score": 56.0, "band": "weakening"}],
    )
    store.fill_history_gaps(today="2026-05-15")
    history = _read(store, "AAPL")["history"]
    dates = [r["date"] for r in history]
    assert dates == sorted(dates, reverse=True)


def test_fill_history_gaps_returns_zero_for_empty_dir(store: LthcsPersist) -> None:
    # No ticker history files exist at all.
    written = store.fill_history_gaps(today="2026-05-17")
    assert written == 0


def test_fill_history_gaps_rejects_bad_date(store: LthcsPersist) -> None:
    with pytest.raises(ValueError):
        store.fill_history_gaps(today="not-a-date")


# ---------------------------------------------------------------------------
# clear_synthetic_entries
# ---------------------------------------------------------------------------

def test_clear_synthetic_entries_removes_only_synthetics(store: LthcsPersist) -> None:
    # Seed a history with a mix of real and synthetic rows.
    _seed_history(
        store, "AAPL",
        [
            {"date": "2026-05-16", "score": 56.0, "band": "weakening", "synthetic": True},
            {"date": "2026-05-15", "score": 56.0, "band": "weakening", "synthetic": True},
            {"date": "2026-05-14", "score": 56.0, "band": "weakening"},
        ],
    )
    removed = store.clear_synthetic_entries("AAPL")
    assert removed == 2
    history = _read(store, "AAPL")["history"]
    assert len(history) == 1
    assert history[0]["date"] == "2026-05-14"
    assert "synthetic" not in history[0]


def test_clear_synthetic_entries_noop_when_none(store: LthcsPersist) -> None:
    _seed_history(
        store, "AAPL",
        [{"date": "2026-05-14", "score": 56.0, "band": "weakening"}],
    )
    removed = store.clear_synthetic_entries("AAPL")
    assert removed == 0


def test_clear_synthetic_entries_missing_ticker(store: LthcsPersist) -> None:
    # No file at all => 0.
    removed = store.clear_synthetic_entries("DOESNOTEXIST")
    assert removed == 0


# ---------------------------------------------------------------------------
# Interaction with rebuild_history_for_all_tickers
# ---------------------------------------------------------------------------

def test_catchup_then_today_write_produces_continuous_history(store: LthcsPersist) -> None:
    # Realistic end-to-end: last real entry 3 days ago, then catch-up
    # fills 2 synthetic days, then today's row is written by
    # rebuild_history_for_all_tickers — should yield 4 consecutive dates.
    _seed_history(
        store, "AAPL",
        [{"date": "2026-05-13", "score": 56.0, "band": "weakening"}],
    )
    store.fill_history_gaps(today="2026-05-16")
    # Now simulate Stage 8's rebuild_history_for_all_tickers writing today.
    snapshot_row = {
        "ticker": "AAPL",
        "lthcs_score": 58.0,
        "band": "weakening",
    }
    store.rebuild_history_for_all_tickers(
        [snapshot_row], "2026-05-16", MODEL_VERSION
    )
    history = _read(store, "AAPL")["history"]
    dates = [r["date"] for r in history]
    assert dates == ["2026-05-16", "2026-05-15", "2026-05-14", "2026-05-13"]
    # Today's row is REAL (no synthetic flag).
    today_row = next(r for r in history if r["date"] == "2026-05-16")
    assert "synthetic" not in today_row
    assert today_row["score"] == 58.0
    # The two gap days are synthetic.
    assert next(r for r in history if r["date"] == "2026-05-15")["synthetic"] is True
    assert next(r for r in history if r["date"] == "2026-05-14")["synthetic"] is True


# ---------------------------------------------------------------------------
# Membership: no row for a day the ticker was not being scored
# ---------------------------------------------------------------------------
# Catch-up copies a score only onto days the daily run missed (no snapshot
# file: the gap days health/known_gaps.json discloses) and only while the
# ticker was in the scored set. Regression for 2026-10-06: DOW was scored once
# on 2026-05-16, left the universe on 05-17, was re-added on 2026-10-05, and
# the nightly --catch-up wrote 142 copies of its May score (05-17..10-05).

def _seed_snapshot(store: LthcsPersist, day: str, tickers: List[str]) -> None:
    store.snapshots_dir.mkdir(parents=True, exist_ok=True)
    rows = [{"ticker": t, "lthcs_score": 50.0, "band": "monitor"} for t in tickers]
    store.snapshot_path(day).write_text(
        json.dumps({"calc_date": day, "scores": rows}), encoding="utf-8")


def test_ticker_added_after_the_gap_gets_no_rows(store: LthcsPersist) -> None:
    # No snapshot files at all, so every day in the gap looks like a missed
    # run; universe added_on alone must stop the carry.
    _seed_history(store, "DOW", [{"date": "2026-05-16", "score": 48.8, "band": "review"}])
    written = store.fill_history_gaps(
        today="2026-10-06", tickers=["DOW"], added_on={"DOW": "2026-10-05"})
    assert written == 0
    assert _read(store, "DOW")["history"] == [
        {"date": "2026-05-16", "score": 48.8, "band": "review"}]


def test_added_on_on_or_before_the_last_real_row_still_fills(store: LthcsPersist) -> None:
    _seed_history(store, "AAPL", [{"date": "2026-10-03", "score": 60.0, "band": "monitor"}])
    _seed_history(store, "MSFT", [{"date": "2026-10-03", "score": 70.0, "band": "constructive"}])
    written = store.fill_history_gaps(
        today="2026-10-06", tickers=["AAPL", "MSFT"],
        added_on={"AAPL": "2026-10-03", "MSFT": "2026-02-01"})
    assert written == 4  # 10-04 and 10-05 for each
    for t in ("AAPL", "MSFT"):
        dates = sorted(r["date"] for r in _read(store, t)["history"] if r.get("synthetic"))
        assert dates == ["2026-10-04", "2026-10-05"]


def test_unparseable_added_on_is_ignored(store: LthcsPersist) -> None:
    _seed_history(store, "AAPL", [{"date": "2026-10-03", "score": 60.0, "band": "monitor"}])
    written = store.fill_history_gaps(
        today="2026-10-05", tickers=["AAPL"], added_on={"AAPL": "late May 2026"})
    assert written == 1


def test_no_copy_across_a_run_that_left_the_ticker_out(store: LthcsPersist) -> None:
    # DOW: scored 05-16, then runs on 05-17 / 05-18 / 05-20 scored the others
    # but not DOW; 05-19 is a missed-run gap day. AAPL kept being scored and
    # gets the 05-19 gap-day row; DOW gets nothing, not even on the gap day.
    _seed_history(store, "DOW", [{"date": "2026-05-16", "score": 48.8, "band": "review"}])
    _seed_history(store, "AAPL", [
        {"date": "2026-05-18", "score": 60.0, "band": "monitor"},
        {"date": "2026-05-17", "score": 59.0, "band": "monitor"},
        {"date": "2026-05-16", "score": 58.0, "band": "monitor"},
    ])
    _seed_snapshot(store, "2026-05-16", ["AAPL", "DOW"])
    for day in ("2026-05-17", "2026-05-18", "2026-05-20"):
        _seed_snapshot(store, day, ["AAPL"])
    # AAPL's 05-20 row was written by that day's run.
    store.append_history_entry("AAPL", "2026-05-20", 61.0, "monitor", MODEL_VERSION)
    written = store.fill_history_gaps(today="2026-05-21", tickers=["AAPL", "DOW"])
    assert written == 0  # AAPL is up to date; DOW is not filled
    assert [r["date"] for r in _read(store, "DOW")["history"]] == ["2026-05-16"]

    # Same store, but AAPL's last real row is 05-18 and the 05-19 run was
    # missed: the gap-day fill (health/known_gaps.json policy) is kept.
    path = store.history_path("AAPL")
    payload = json.loads(path.read_text())
    payload["history"] = [r for r in payload["history"] if r["date"] != "2026-05-20"]
    path.write_text(json.dumps(payload))
    store.snapshot_path("2026-05-20").unlink()
    written = store.fill_history_gaps(today="2026-05-20", tickers=["AAPL", "DOW"])
    assert written == 1
    assert [r for r in _read(store, "AAPL")["history"] if r.get("synthetic")] == [
        {"date": "2026-05-19", "score": 60.0, "band": "monitor", "synthetic": True}]
    assert [r["date"] for r in _read(store, "DOW")["history"]] == ["2026-05-16"]


def test_day_with_a_snapshot_never_gets_a_copy(store: LthcsPersist) -> None:
    # Last real row 05-10; 05-11 and 05-12 were missed (no snapshot), the
    # 05-13 run happened without the ticker. Only the missed days before
    # that run are filled; 05-13 and 05-14 are not.
    _seed_history(store, "XYZ", [{"date": "2026-05-10", "score": 55.0, "band": "monitor"}])
    _seed_snapshot(store, "2026-05-10", ["XYZ"])
    _seed_snapshot(store, "2026-05-13", ["OTHER"])
    written = store.fill_history_gaps(today="2026-05-15", tickers=["XYZ"])
    assert written == 2
    assert sorted(r["date"] for r in _read(store, "XYZ")["history"] if r.get("synthetic")) == [
        "2026-05-11", "2026-05-12"]
    # Idempotent: a second run adds nothing.
    assert store.fill_history_gaps(today="2026-05-15", tickers=["XYZ"]) == 0


def test_existing_carried_rows_are_not_extended(store: LthcsPersist) -> None:
    # A file still holding copies written past a run that left the ticker
    # out (the pre-fix catch-up did this) is not extended further.
    _seed_history(store, "DOW", [
        {"date": "2026-05-18", "score": 48.8, "band": "review", "synthetic": True},
        {"date": "2026-05-17", "score": 48.8, "band": "review", "synthetic": True},
        {"date": "2026-05-16", "score": 48.8, "band": "review"},
    ])
    _seed_snapshot(store, "2026-05-17", ["AAPL"])
    assert store.fill_history_gaps(today="2026-05-21", tickers=["DOW"]) == 0
    assert len(_read(store, "DOW")["history"]) == 3


def test_daily_catch_up_passes_universe_added_on(tmp_path: Path) -> None:
    # The lthcs_daily --catch-up wiring: active universe + added_on from
    # universe.json. DOW (re-added 2026-10-05, last real row 2026-05-16)
    # gets no rows over the gap; AAPL, scored through 10-04, gets the 10-05
    # gap day; BK (inactive) gets nothing.
    import types

    import lthcs_daily

    store = LthcsPersist(tmp_path)
    _seed_history(store, "DOW", [{"date": "2026-05-16", "score": 48.8, "band": "review"}])
    _seed_history(store, "AAPL", [{"date": "2026-10-04", "score": 60.0, "band": "monitor"}])
    _seed_history(store, "BK", [{"date": "2026-07-09", "score": 59.6, "band": "weakening"}])
    state = lthcs_daily.PipelineState(args=types.SimpleNamespace(catch_up=True))
    state.persist = store
    state.calc_date = "2026-10-06"
    state.by_ticker = {
        "AAPL": {"ticker": "AAPL", "active": True},
        "DOW": {"ticker": "DOW", "active": True, "added_on": "2026-10-05"},
        "BK": {"ticker": "BK", "active": False},
    }
    state.active_tickers = ["AAPL", "DOW"]
    assert lthcs_daily._catch_up_history(state) == 1
    assert _read(store, "DOW")["history"] == [
        {"date": "2026-05-16", "score": 48.8, "band": "review"}]
    assert [r["date"] for r in _read(store, "BK")["history"]] == ["2026-07-09"]
    assert [(r["date"], r.get("synthetic")) for r in _read(store, "AAPL")["history"]] == [
        ("2026-10-05", True), ("2026-10-04", None)]
