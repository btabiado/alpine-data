"""Tests for the Phase 2 LTHCS Google Trends daily nudge.

Covers the new behaviour layered on top of the Phase 1 weekly batch:

* Resumable progress file (completed/failures, per-day reset)
* Adaptive sleep cadence — backoff on 429, reset on success, max cap
* 5-day stale-cache TTL — fresh tickers skipped, stale ones picked
* Candidate selection ordering — missing > stale > (optional) fresh
* 429-bail circuit breaker after N consecutive rate-limits

All pytrends/network paths are mocked. No live HTTP. No real sleeps.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from scripts import lthcs_trends_daily as daily


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_args(
    tmp_path: Path,
    *,
    batch_size: int = 30,
    stale_after_days: int = 5,
    sleep_base: float = 12.0,
    max_backoff: float = 300.0,
    jitter: float = 0.0,
    max_retries: int = 2,
    tickers: Optional[str] = None,
    force: bool = False,
    dry_run: bool = False,
    week: Optional[str] = None,
) -> argparse.Namespace:
    """Build a Namespace mirroring the daily-script CLI args."""
    return argparse.Namespace(
        week=week,
        tickers=tickers,
        universe=tmp_path / "universe.json",
        data_root=tmp_path / "data" / "lthcs",
        cache_root=tmp_path / ".cache" / "lthcs" / "google_trends",
        progress_path=tmp_path / ".cache" / "lthcs" / "trends_progress.json",
        batch_size=batch_size,
        stale_after_days=stale_after_days,
        sleep_base=sleep_base,
        max_backoff=max_backoff,
        jitter=jitter,
        max_retries=max_retries,
        timeframe="today 5-y",
        geo="",
        force=force,
        dry_run=dry_run,
        log_level="WARNING",
    )


def _write_universe(path: Path, tickers: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"tickers": [{"ticker": t, "active": True} for t in tickers]}
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh)


def _write_snapshot(
    snap_path: Path,
    *,
    week: str = "2026-W21",
    tickers: Optional[Dict[str, Dict[str, Any]]] = None,
) -> None:
    snap_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "week": week,
        "as_of": _dt.date.today().isoformat(),
        "term_map": {t: f"{t} stock" for t in (tickers or {})},
        "tickers": tickers or {},
    }
    with snap_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh)


def _fake_pytrends_factory(
    *,
    series_by_term: Optional[Dict[str, List[float]]] = None,
    rate_limit_terms: Optional[List[str]] = None,
    failing_terms: Optional[List[str]] = None,
) -> Callable[[], Any]:
    """Build a stand-in for ``TrendReq`` whose behaviour is configurable per-term.

    * ``series_by_term``: term -> list of weekly interest values
    * ``rate_limit_terms``: terms that should raise a 429-like error
    * ``failing_terms``: terms that should raise a non-429 generic error
    """
    series_by_term = series_by_term or {}
    rate_limit_terms = rate_limit_terms or []
    failing_terms = failing_terms or []

    def _factory() -> Any:
        inst = MagicMock()
        state: Dict[str, Any] = {"term": None}

        def _build(kw_list: List[str], timeframe: str = "today 5-y", geo: str = "") -> None:
            term = kw_list[0]
            state["term"] = term
            if term in rate_limit_terms:
                # Mirror pytrends' ResponseError shape — class name carries
                # "TooManyRequests" so _is_rate_limit_error picks it up.
                raise type("TooManyRequestsError", (RuntimeError,), {})(
                    "429: too many requests"
                )
            if term in failing_terms:
                raise RuntimeError("boom: generic non-429 failure")

        def _interest() -> pd.DataFrame:
            term = state["term"]
            values = series_by_term.get(term, [10.0, 20.0, 30.0])
            return pd.DataFrame(
                {
                    term: values,
                    "isPartial": [False] * len(values),
                },
                index=pd.date_range("2026-04-01", periods=len(values)),
            )

        inst.build_payload.side_effect = _build
        inst.interest_over_time.side_effect = _interest
        return inst

    return _factory


@pytest.fixture
def _silent_sleeper() -> daily.AdaptiveSleeper:
    """An AdaptiveSleeper with a no-op sleep_fn (no real wall-time waits)."""
    return daily.AdaptiveSleeper(
        base=12.0, max_backoff=300.0, jitter=0.0, sleep_fn=lambda _s: None,
    )


# ---------------------------------------------------------------------------
# Progress file
# ---------------------------------------------------------------------------


def test_read_progress_missing_file_returns_empty(tmp_path: Path) -> None:
    progress_path = tmp_path / "progress.json"
    progress = daily.read_progress(progress_path, today="2026-05-19")
    assert progress == {
        "date": "2026-05-19", "completed": [], "failures": [],
    }


def test_read_progress_today_match_returns_existing(tmp_path: Path) -> None:
    progress_path = tmp_path / "progress.json"
    progress_path.write_text(json.dumps({
        "date": "2026-05-19",
        "completed": ["AAPL", "MSFT"],
        "failures": ["TSLA"],
    }))
    progress = daily.read_progress(progress_path, today="2026-05-19")
    assert progress["completed"] == ["AAPL", "MSFT"]
    assert progress["failures"] == ["TSLA"]


def test_read_progress_yesterday_resets(tmp_path: Path) -> None:
    """Yesterday's progress must not leak into today — slice should reset."""
    progress_path = tmp_path / "progress.json"
    progress_path.write_text(json.dumps({
        "date": "2026-05-18",
        "completed": ["AAPL", "MSFT"],
        "failures": [],
    }))
    progress = daily.read_progress(progress_path, today="2026-05-19")
    assert progress == {
        "date": "2026-05-19", "completed": [], "failures": [],
    }


def test_read_progress_malformed_returns_empty(tmp_path: Path) -> None:
    progress_path = tmp_path / "progress.json"
    progress_path.write_text("{not valid json")
    progress = daily.read_progress(progress_path, today="2026-05-19")
    assert progress["completed"] == []
    assert progress["failures"] == []


def test_write_progress_roundtrip(tmp_path: Path) -> None:
    progress_path = tmp_path / "subdir" / "progress.json"
    daily.write_progress(progress_path, {
        "date": "2026-05-19",
        "completed": ["AAPL"],
        "failures": [],
    })
    assert progress_path.exists()
    loaded = json.loads(progress_path.read_text())
    assert loaded["completed"] == ["AAPL"]


# ---------------------------------------------------------------------------
# Adaptive sleep cadence
# ---------------------------------------------------------------------------


def test_adaptive_sleeper_steady_state_uses_base() -> None:
    waits: List[float] = []
    s = daily.AdaptiveSleeper(
        base=12.0, max_backoff=300.0, jitter=0.0,
        sleep_fn=lambda w: waits.append(w),
    )
    s.sleep()
    s.sleep()
    assert waits == [12.0, 12.0]


def test_adaptive_sleeper_doubles_on_rate_limit() -> None:
    waits: List[float] = []
    s = daily.AdaptiveSleeper(
        base=10.0, max_backoff=300.0, jitter=0.0,
        sleep_fn=lambda w: waits.append(w),
    )
    s.on_rate_limit()        # 10 -> 20
    s.sleep()
    s.on_rate_limit()        # 20 -> 40
    s.sleep()
    s.on_rate_limit()        # 40 -> 80
    s.sleep()
    assert waits == [20.0, 40.0, 80.0]


def test_adaptive_sleeper_caps_at_max_backoff() -> None:
    waits: List[float] = []
    s = daily.AdaptiveSleeper(
        base=10.0, max_backoff=50.0, jitter=0.0,
        sleep_fn=lambda w: waits.append(w),
    )
    for _ in range(10):
        s.on_rate_limit()
    s.sleep()
    assert waits == [50.0]
    assert s.current == 50.0


def test_adaptive_sleeper_resets_on_success() -> None:
    waits: List[float] = []
    s = daily.AdaptiveSleeper(
        base=10.0, max_backoff=300.0, jitter=0.0,
        sleep_fn=lambda w: waits.append(w),
    )
    s.on_rate_limit()
    s.on_rate_limit()        # cadence = 40
    s.on_success()           # snap back to 10
    s.sleep()
    assert waits == [10.0]


def test_adaptive_sleeper_zero_base_skips_sleep() -> None:
    """A base of 0 means no waiting in steady state."""
    waits: List[float] = []
    s = daily.AdaptiveSleeper(
        base=0.0, max_backoff=10.0, jitter=0.0,
        sleep_fn=lambda w: waits.append(w),
    )
    s.sleep()
    assert waits == []


def test_adaptive_sleeper_rejects_bad_args() -> None:
    with pytest.raises(ValueError):
        daily.AdaptiveSleeper(base=-1.0, max_backoff=10.0)
    with pytest.raises(ValueError):
        daily.AdaptiveSleeper(base=10.0, max_backoff=5.0)


# ---------------------------------------------------------------------------
# Stale-cache TTL / candidate selection
# ---------------------------------------------------------------------------


def _fetched_at(days_ago: int, now: Optional[_dt.datetime] = None) -> str:
    now = now or _dt.datetime.now(_dt.timezone.utc)
    return (now - _dt.timedelta(days=days_ago)).isoformat()


def test_select_candidates_picks_missing_first() -> None:
    universe = ["AAPL", "MSFT", "NVDA"]
    snapshot = {
        "tickers": {
            "AAPL": {"series": [1.0], "fetched_at": _fetched_at(0)},
            "MSFT": {"series": [1.0], "fetched_at": _fetched_at(0)},
        }
    }
    picked = daily.select_candidates(
        universe, snapshot,
        stale_after_days=5, batch_size=30, completed=[],
    )
    # NVDA missing -> picked. AAPL/MSFT fresh -> not picked.
    assert picked == ["NVDA"]


def test_select_candidates_picks_stale_after_5_days() -> None:
    universe = ["AAPL", "MSFT", "NVDA"]
    snapshot = {
        "tickers": {
            "AAPL": {"series": [1.0], "fetched_at": _fetched_at(0)},
            "MSFT": {"series": [1.0], "fetched_at": _fetched_at(6)},   # stale
            "NVDA": {"series": [1.0], "fetched_at": _fetched_at(10)},  # very stale
        }
    }
    picked = daily.select_candidates(
        universe, snapshot,
        stale_after_days=5, batch_size=30, completed=[],
    )
    # Only the 2 stale ones; AAPL is fresh.
    assert set(picked) == {"MSFT", "NVDA"}
    assert "AAPL" not in picked


def test_select_candidates_fresh_under_ttl_skipped() -> None:
    """A 4-day-old entry with stale_after_days=5 stays fresh -> skipped."""
    universe = ["AAPL"]
    snapshot = {
        "tickers": {
            "AAPL": {"series": [1.0], "fetched_at": _fetched_at(4)},
        }
    }
    picked = daily.select_candidates(
        universe, snapshot,
        stale_after_days=5, batch_size=30, completed=[],
    )
    assert picked == []


def test_select_candidates_force_overrides_freshness() -> None:
    universe = ["AAPL", "MSFT"]
    snapshot = {
        "tickers": {
            "AAPL": {"series": [1.0], "fetched_at": _fetched_at(1)},
            "MSFT": {"series": [1.0], "fetched_at": _fetched_at(1)},
        }
    }
    picked = daily.select_candidates(
        universe, snapshot,
        stale_after_days=5, batch_size=30, completed=[], force=True,
    )
    assert set(picked) == {"AAPL", "MSFT"}


def test_select_candidates_caps_at_batch_size() -> None:
    universe = [f"T{i}" for i in range(50)]
    picked = daily.select_candidates(
        universe, snapshot=None,
        stale_after_days=5, batch_size=10, completed=[],
    )
    assert len(picked) == 10
    assert picked == universe[:10]


def test_select_candidates_skips_completed() -> None:
    """Tickers already in today's completed list are skipped (resume)."""
    universe = ["AAPL", "MSFT", "NVDA"]
    picked = daily.select_candidates(
        universe, snapshot=None,
        stale_after_days=5, batch_size=30,
        completed=["AAPL", "MSFT"],
    )
    assert picked == ["NVDA"]


def test_select_candidates_missing_fetched_at_treated_as_stale() -> None:
    """Snapshot entry without fetched_at -> treat as stale, refresh it."""
    universe = ["AAPL"]
    snapshot = {"tickers": {"AAPL": {"series": [1.0]}}}  # no fetched_at
    picked = daily.select_candidates(
        universe, snapshot,
        stale_after_days=5, batch_size=30, completed=[],
    )
    assert picked == ["AAPL"]


def test_select_candidates_empty_series_treated_as_missing() -> None:
    universe = ["AAPL"]
    snapshot = {"tickers": {"AAPL": {"series": [], "fetched_at": _fetched_at(0)}}}
    picked = daily.select_candidates(
        universe, snapshot,
        stale_after_days=5, batch_size=30, completed=[],
    )
    assert picked == ["AAPL"]


# ---------------------------------------------------------------------------
# End-to-end run_daily_batch with mocked pytrends
# ---------------------------------------------------------------------------


def test_run_daily_batch_fresh_universe_no_snapshot(
    tmp_path: Path, _silent_sleeper: daily.AdaptiveSleeper,
) -> None:
    """Empty snapshot + 3-ticker universe -> all 3 fetched."""
    _write_universe(tmp_path / "universe.json", ["AAPL", "MSFT", "NVDA"])
    args = _make_args(tmp_path, batch_size=10, sleep_base=0.0, max_backoff=0.0)

    factory = _fake_pytrends_factory(
        series_by_term={
            "AAPL stock": [10.0, 11.0, 12.0],
            "MSFT stock": [20.0, 21.0, 22.0],
            "NVDA stock": [30.0, 31.0, 32.0],
        },
    )

    # Patch resolve_search_term so we don't have to map topic IDs.
    with patch.object(
        daily, "resolve_search_term",
        side_effect=lambda t: f"{t.upper()} stock",
    ):
        snapshot = daily.run_daily_batch(
            args, trend_req_factory=factory, sleeper=_silent_sleeper,
        )

    assert set(snapshot["tickers"].keys()) == {"AAPL", "MSFT", "NVDA"}
    assert snapshot["tickers"]["AAPL"]["series"] == [10.0, 11.0, 12.0]
    # fetched_at stamp is recorded on each entry for the next stale-TTL check.
    for t in ["AAPL", "MSFT", "NVDA"]:
        assert isinstance(snapshot["tickers"][t]["fetched_at"], str)


def test_run_daily_batch_resumes_from_progress(
    tmp_path: Path, _silent_sleeper: daily.AdaptiveSleeper,
) -> None:
    """Progress shows AAPL already done today -> only MSFT + NVDA fetched."""
    _write_universe(tmp_path / "universe.json", ["AAPL", "MSFT", "NVDA"])
    args = _make_args(tmp_path, batch_size=10, sleep_base=0.0, max_backoff=0.0)

    today = _dt.date.today().isoformat()
    args.progress_path.parent.mkdir(parents=True, exist_ok=True)
    args.progress_path.write_text(json.dumps({
        "date": today, "completed": ["AAPL"], "failures": [],
    }))

    # Track which build_payload calls happen so we can assert AAPL is skipped.
    call_log: List[str] = []
    factory = _fake_pytrends_factory()

    def _factory_with_log() -> Any:
        inst = factory()
        orig = inst.build_payload.side_effect

        def _wrapped(kw_list: List[str], **kw: Any) -> Any:
            call_log.append(kw_list[0])
            return orig(kw_list, **kw)

        inst.build_payload.side_effect = _wrapped
        return inst

    with patch.object(
        daily, "resolve_search_term",
        side_effect=lambda t: f"{t.upper()} stock",
    ):
        snapshot = daily.run_daily_batch(
            args, trend_req_factory=_factory_with_log, sleeper=_silent_sleeper,
        )

    # AAPL's term must NEVER have appeared on the wire.
    assert "AAPL stock" not in call_log
    assert "MSFT stock" in call_log
    assert "NVDA stock" in call_log
    # Snapshot picks up MSFT/NVDA but not AAPL (no AAPL fetch happened).
    assert "MSFT" in snapshot["tickers"]
    assert "NVDA" in snapshot["tickers"]


def test_run_daily_batch_429_triggers_backoff(
    tmp_path: Path,
) -> None:
    """When a fetch 429s, the sleeper's on_rate_limit must fire (cadence doubles)."""
    _write_universe(tmp_path / "universe.json", ["AAPL", "MSFT", "NVDA"])
    args = _make_args(
        tmp_path, batch_size=10, sleep_base=10.0,
        max_backoff=300.0, max_retries=0,  # don't loop inside fetch_one_live
    )

    sleeper_waits: List[float] = []
    sleeper = daily.AdaptiveSleeper(
        base=10.0, max_backoff=300.0, jitter=0.0,
        sleep_fn=lambda w: sleeper_waits.append(w),
    )

    # MSFT 429s; AAPL/NVDA succeed.
    factory = _fake_pytrends_factory(
        series_by_term={
            "AAPL stock": [1.0, 2.0],
            "NVDA stock": [3.0, 4.0],
        },
        rate_limit_terms=["MSFT stock"],
    )

    with patch.object(
        daily, "resolve_search_term",
        side_effect=lambda t: f"{t.upper()} stock",
    ), patch.object(daily.time, "sleep", lambda _s: None):
        # Patch inner pytrends backoff sleep too so test is fast.
        snapshot = daily.run_daily_batch(
            args, trend_req_factory=factory, sleeper=sleeper,
        )

    # Order: AAPL (success, sleep base=10), MSFT (429, backoff -> 20),
    # NVDA (success, reset back to 10).
    # First sleep is post-AAPL at base 10.
    # Second sleep is post-MSFT-fail at backoff 20.
    # No sleep after the last ticker (NVDA).
    assert sleeper_waits[0] == 10.0
    assert sleeper_waits[1] == 20.0
    # After NVDA succeeds, sleeper.current snaps back to base.
    assert sleeper.current == 10.0
    # MSFT must be in the failures list (persisted to progress).
    progress = json.loads(args.progress_path.read_text())
    assert "MSFT" in progress["failures"]
    assert "AAPL" in progress["completed"]
    assert "NVDA" in progress["completed"]


def test_run_daily_batch_bails_on_consecutive_429s(
    tmp_path: Path, _silent_sleeper: daily.AdaptiveSleeper,
) -> None:
    """5+ consecutive 429s -> abort run, don't keep hammering."""
    tickers = [f"T{i}" for i in range(20)]
    _write_universe(tmp_path / "universe.json", tickers)
    args = _make_args(
        tmp_path, batch_size=20, sleep_base=0.0, max_backoff=0.0,
        max_retries=0,
    )

    # Every term 429s.
    factory = _fake_pytrends_factory(
        rate_limit_terms=[f"{t.upper()} stock" for t in tickers],
    )

    call_log: List[str] = []

    def _factory_with_log() -> Any:
        inst = factory()
        orig = inst.build_payload.side_effect

        def _wrapped(kw_list: List[str], **kw: Any) -> Any:
            call_log.append(kw_list[0])
            return orig(kw_list, **kw)

        inst.build_payload.side_effect = _wrapped
        return inst

    with patch.object(
        daily, "resolve_search_term",
        side_effect=lambda t: f"{t.upper()} stock",
    ), patch.object(daily.time, "sleep", lambda _s: None):
        daily.run_daily_batch(
            args, trend_req_factory=_factory_with_log, sleeper=_silent_sleeper,
        )

    # We should NOT have processed all 20 tickers — bail kicks in.
    # The implementation bails at 5 consecutive 429s, so at most ~6 calls
    # (5 to trigger bail + the one that triggered the count).
    assert len(call_log) < 20
    assert len(call_log) <= 6


def test_run_daily_batch_dry_run_no_writes(
    tmp_path: Path, _silent_sleeper: daily.AdaptiveSleeper,
) -> None:
    """Dry-run logs candidates but doesn't write snapshot or progress."""
    _write_universe(tmp_path / "universe.json", ["AAPL", "MSFT"])
    args = _make_args(
        tmp_path, batch_size=10, dry_run=True,
        sleep_base=0.0, max_backoff=0.0,
    )

    factory = _fake_pytrends_factory()

    with patch.object(
        daily, "resolve_search_term",
        side_effect=lambda t: f"{t.upper()} stock",
    ):
        snapshot = daily.run_daily_batch(
            args, trend_req_factory=factory, sleeper=_silent_sleeper,
        )

    # Snapshot returned but never written; progress file not created either.
    assert not (args.data_root / "trends").exists() or not list(
        (args.data_root / "trends").glob("*.json")
    )
    assert not args.progress_path.exists()


def test_run_daily_batch_writes_progress_atomically(
    tmp_path: Path, _silent_sleeper: daily.AdaptiveSleeper,
) -> None:
    """After a successful fetch, progress + snapshot are both on disk."""
    _write_universe(tmp_path / "universe.json", ["AAPL"])
    args = _make_args(tmp_path, batch_size=10, sleep_base=0.0, max_backoff=0.0)

    factory = _fake_pytrends_factory(
        series_by_term={"AAPL stock": [1.0, 2.0, 3.0]},
    )

    with patch.object(
        daily, "resolve_search_term",
        side_effect=lambda t: f"{t.upper()} stock",
    ):
        daily.run_daily_batch(
            args, trend_req_factory=factory, sleeper=_silent_sleeper,
        )

    assert args.progress_path.exists()
    progress = json.loads(args.progress_path.read_text())
    assert "AAPL" in progress["completed"]

    snap_path = args.data_root / "trends" / f"{daily._iso_week_str()}.json"
    assert snap_path.exists()
    snap = json.loads(snap_path.read_text())
    assert "AAPL" in snap["tickers"]


def test_run_daily_batch_additive_merge_preserves_existing(
    tmp_path: Path, _silent_sleeper: daily.AdaptiveSleeper,
) -> None:
    """Existing snapshot entries must survive a daily run; only NEW tickers added."""
    week = daily._iso_week_str()
    snap_path = tmp_path / "data" / "lthcs" / "trends" / f"{week}.json"
    _write_snapshot(
        snap_path, week=week,
        tickers={
            "AAPL": {
                "series": [1.0, 2.0, 3.0],
                "term": "/m/0k8z",
                "fetched_at": _fetched_at(0),  # fresh -> won't be re-fetched
            }
        },
    )
    _write_universe(tmp_path / "universe.json", ["AAPL", "MSFT"])
    args = _make_args(tmp_path, batch_size=10, sleep_base=0.0, max_backoff=0.0)

    factory = _fake_pytrends_factory(
        series_by_term={"MSFT stock": [10.0, 20.0, 30.0]},
    )

    with patch.object(
        daily, "resolve_search_term",
        side_effect=lambda t: f"{t.upper()} stock",
    ):
        snapshot = daily.run_daily_batch(
            args, trend_req_factory=factory, sleeper=_silent_sleeper,
        )

    # AAPL's pre-existing data MUST still be in the snapshot.
    assert snapshot["tickers"]["AAPL"]["series"] == [1.0, 2.0, 3.0]
    # MSFT got added.
    assert snapshot["tickers"]["MSFT"]["series"] == [10.0, 20.0, 30.0]
