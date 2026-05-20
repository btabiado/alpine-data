"""Tests for ``lthcs.persist``.

Every test uses ``tmp_path`` as the data root, so we never touch the
real ``data/lthcs/`` tree. The persist layer is intentionally I/O heavy
so most tests just round-trip a write -> read and assert structural
invariants.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from lthcs.persist import LthcsPersist, get_default_data_root


MODEL_VERSION = "v1.0.0"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def store(tmp_path: Path) -> LthcsPersist:
    return LthcsPersist(tmp_path)


def _sample_score_row(ticker: str = "AAPL", score: float = 56.0, band: str = "weakening"):
    return {
        "ticker": ticker,
        "lthcs_score": score,
        "band": band,
        "drift_1d": 0.0,
        "drift_7d": 0.0,
        "drift_30d": 0.0,
        "drift_90d": 0.0,
        "confidence_level": "high",
        "data_quality_flags": [],
        "subscores": {
            "adoption_momentum": 60.0,
            "institutional_confidence": 55.0,
            "financial_evolution": 50.0,
            "thesis_integrity": 58.0,
            "des": 57.0,
        },
        "modifiers": {"macro_adj": 0.0, "sector_adj": 0.0, "volatility_mod": 0.0},
        "maturity_stage": "standard_compounder",
        "weights_used": [0.2, 0.2, 0.2, 0.2, 0.2],
        "weighted_components": [12.0, 11.0, 10.0, 11.6, 11.4],
        "sector": "Technology",
    }


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

def test_init_creates_four_subdirectories(tmp_path: Path) -> None:
    LthcsPersist(tmp_path)
    assert (tmp_path / "snapshots").is_dir()
    assert (tmp_path / "variable_detail").is_dir()
    assert (tmp_path / "narratives").is_dir()
    assert (tmp_path / "history" / "by_ticker").is_dir()
    # Tier 5 #23: shadow dir for LLM narratives also created on init.
    assert (tmp_path / "narratives_llm").is_dir()


def test_init_is_idempotent(tmp_path: Path) -> None:
    LthcsPersist(tmp_path)
    # Second call should not raise even though the dirs already exist.
    LthcsPersist(tmp_path)
    assert (tmp_path / "snapshots").is_dir()


def test_get_default_data_root_points_into_repo() -> None:
    root = get_default_data_root()
    assert isinstance(root, Path)
    # Resolved path should end with data/lthcs under the repo dir.
    assert root.name == "lthcs"
    assert root.parent.name == "data"
    # And the lthcs package directory should be a sibling of data/.
    assert (root.parent.parent / "lthcs" / "persist.py").exists()


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

def test_write_and_read_snapshot_round_trip(store: LthcsPersist) -> None:
    rows = [_sample_score_row("AAPL"), _sample_score_row("MSFT", score=72.5, band="constructive")]
    path = store.write_snapshot(
        "2026-05-16", MODEL_VERSION, "standard_compounder", rows
    )
    assert path.exists()
    assert path.name == "2026-05-16.json"
    assert store.snapshot_exists("2026-05-16")

    payload = store.read_snapshot("2026-05-16")
    assert payload["calc_date"] == "2026-05-16"
    assert payload["model_version"] == MODEL_VERSION
    assert payload["weights_profile_default"] == "standard_compounder"
    assert len(payload["scores"]) == 2
    assert payload["scores"][0]["ticker"] == "AAPL"
    assert payload["scores"][1]["ticker"] == "MSFT"


def test_write_snapshot_refuses_overwrite_by_default(store: LthcsPersist) -> None:
    rows = [_sample_score_row()]
    store.write_snapshot("2026-05-16", MODEL_VERSION, "standard_compounder", rows)
    with pytest.raises(FileExistsError):
        store.write_snapshot(
            "2026-05-16", MODEL_VERSION, "standard_compounder", rows
        )


def test_write_snapshot_overwrite_true_replaces(store: LthcsPersist) -> None:
    store.write_snapshot(
        "2026-05-16", MODEL_VERSION, "standard_compounder", [_sample_score_row("AAPL")]
    )
    new_rows = [_sample_score_row("AAPL", score=99.0, band="elite")]
    store.write_snapshot(
        "2026-05-16",
        MODEL_VERSION,
        "standard_compounder",
        new_rows,
        overwrite=True,
    )
    payload = store.read_snapshot("2026-05-16")
    assert payload["scores"][0]["lthcs_score"] == 99.0
    assert payload["scores"][0]["band"] == "elite"


def test_snapshot_path_validates_date(store: LthcsPersist) -> None:
    with pytest.raises(ValueError):
        store.snapshot_path("not-a-date")
    with pytest.raises(ValueError):
        store.snapshot_path("2026/05/16")
    with pytest.raises(ValueError):
        store.snapshot_path("")
    # Shape check only — "2026-13-99" matches the YYYY-MM-DD pattern and
    # is intentionally accepted (the daily pipeline supplies real dates).
    p = store.snapshot_path("2026-05-16")
    assert p.name == "2026-05-16.json"


def test_write_snapshot_rejects_non_list(store: LthcsPersist) -> None:
    with pytest.raises(TypeError):
        store.write_snapshot(
            "2026-05-16", MODEL_VERSION, "standard_compounder", {"not": "a list"}  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# Variable detail
# ---------------------------------------------------------------------------

def test_write_variable_detail_round_trip(store: LthcsPersist) -> None:
    variables = [
        {"ticker": "AAPL", "variable": "revenue_growth_yoy", "raw": 0.08, "normalized": 65.0},
        {"ticker": "AAPL", "variable": "fcf_margin", "raw": 0.25, "normalized": 80.0},
    ]
    path = store.write_variable_detail("2026-05-16", MODEL_VERSION, variables)
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["calc_date"] == "2026-05-16"
    assert payload["model_version"] == MODEL_VERSION
    assert payload["variables"] == variables


def test_write_variable_detail_overwrite_semantics(store: LthcsPersist) -> None:
    store.write_variable_detail("2026-05-16", MODEL_VERSION, [{"x": 1}])
    with pytest.raises(FileExistsError):
        store.write_variable_detail("2026-05-16", MODEL_VERSION, [{"x": 2}])
    store.write_variable_detail(
        "2026-05-16", MODEL_VERSION, [{"x": 2}], overwrite=True
    )
    payload = json.loads(
        store.variable_detail_path("2026-05-16").read_text(encoding="utf-8")
    )
    assert payload["variables"] == [{"x": 2}]


# ---------------------------------------------------------------------------
# Narratives
# ---------------------------------------------------------------------------

def test_write_narratives_round_trip_with_unicode(store: LthcsPersist) -> None:
    # Include non-ASCII content to verify UTF-8 round-trip.
    narratives = [
        {
            "ticker": "AAPL",
            "anchor_score_paragraph": "Apple holds in Elite Confidence — momentum strong.",
            "pillar_drivers_paragraph": "Top driver: Adoption Momentum (≥80).",
            "drift_paragraph": "Score drifted +0.3 over 7d.",
            "thesis_paragraph": "Risk: regulatory headwinds in EU (€-denominated fines).",
        }
    ]
    path = store.write_narratives("2026-05-16", MODEL_VERSION, narratives)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["narratives"][0]["anchor_score_paragraph"].startswith("Apple holds")
    # The em-dash and ≥ symbol must round-trip.
    assert "≥80" in payload["narratives"][0]["pillar_drivers_paragraph"]
    assert "€" in payload["narratives"][0]["thesis_paragraph"]


def test_write_narratives_overwrite_semantics(store: LthcsPersist) -> None:
    store.write_narratives("2026-05-16", MODEL_VERSION, [{"ticker": "AAPL"}])
    with pytest.raises(FileExistsError):
        store.write_narratives("2026-05-16", MODEL_VERSION, [{"ticker": "AAPL"}])
    store.write_narratives(
        "2026-05-16", MODEL_VERSION, [{"ticker": "MSFT"}], overwrite=True
    )
    payload = json.loads(
        store.narratives_path("2026-05-16").read_text(encoding="utf-8")
    )
    assert payload["narratives"] == [{"ticker": "MSFT"}]


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def test_read_history_returns_empty_shell_when_missing(store: LthcsPersist) -> None:
    h = store.read_history("AAPL")
    assert h == {"ticker": "AAPL", "model_version": "", "history": []}


def test_append_history_entry_creates_new_file(store: LthcsPersist) -> None:
    path = store.append_history_entry(
        "AAPL", "2026-05-16", 56.0, "weakening", MODEL_VERSION
    )
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["ticker"] == "AAPL"
    assert payload["model_version"] == MODEL_VERSION
    assert payload["history"] == [
        {"date": "2026-05-16", "score": 56.0, "band": "weakening"}
    ]


def test_append_history_entry_replaces_existing_date(store: LthcsPersist) -> None:
    # Simulate a --force re-run for the same day.
    store.append_history_entry("AAPL", "2026-05-16", 56.0, "weakening", MODEL_VERSION)
    store.append_history_entry("AAPL", "2026-05-16", 88.0, "high_confidence", MODEL_VERSION)
    payload = store.read_history("AAPL")
    assert len(payload["history"]) == 1
    assert payload["history"][0]["score"] == 88.0
    assert payload["history"][0]["band"] == "high_confidence"


def test_append_history_entry_keeps_sorted_desc(store: LthcsPersist) -> None:
    # Append out of order; result should be sorted newest-first.
    store.append_history_entry("AAPL", "2026-05-15", 55.0, "weakening", MODEL_VERSION)
    store.append_history_entry("AAPL", "2026-05-17", 57.0, "monitor", MODEL_VERSION)
    store.append_history_entry("AAPL", "2026-05-16", 56.0, "weakening", MODEL_VERSION)
    payload = store.read_history("AAPL")
    dates = [row["date"] for row in payload["history"]]
    assert dates == ["2026-05-17", "2026-05-16", "2026-05-15"]


def test_append_history_entry_truncates_to_max_entries(store: LthcsPersist) -> None:
    # Use a small cap so the test is fast.
    for day in range(1, 11):  # 10 entries
        date = "2026-05-%02d" % day
        store.append_history_entry(
            "AAPL", date, 50.0 + day, "monitor", MODEL_VERSION, max_entries=5
        )
    payload = store.read_history("AAPL")
    assert len(payload["history"]) == 5
    # Newest 5 days kept (May 6..10), sorted desc.
    assert payload["history"][0]["date"] == "2026-05-10"
    assert payload["history"][-1]["date"] == "2026-05-06"


def test_append_history_entry_rejects_bad_date(store: LthcsPersist) -> None:
    with pytest.raises(ValueError):
        store.append_history_entry("AAPL", "bad-date", 50.0, "monitor", MODEL_VERSION)


def test_history_path_handles_dot_in_ticker(store: LthcsPersist) -> None:
    # BRK.B is in the V1 universe — make sure the dot survives.
    path = store.history_path("BRK.B")
    assert path.name == "BRK.B.json"


def test_rebuild_history_for_all_tickers_returns_count(store: LthcsPersist) -> None:
    rows = [
        _sample_score_row("AAPL", score=56.0, band="weakening"),
        _sample_score_row("MSFT", score=72.5, band="constructive"),
        _sample_score_row("NVDA", score=88.0, band="high_confidence"),
    ]
    count = store.rebuild_history_for_all_tickers(rows, "2026-05-16", MODEL_VERSION)
    assert count == 3
    for ticker in ("AAPL", "MSFT", "NVDA"):
        payload = store.read_history(ticker)
        assert len(payload["history"]) == 1
        assert payload["history"][0]["date"] == "2026-05-16"


def test_rebuild_history_skips_malformed_rows(store: LthcsPersist) -> None:
    rows = [
        _sample_score_row("AAPL"),
        {"no_ticker": "oops"},
        "not a dict",  # also bad
        _sample_score_row("MSFT"),
    ]
    count = store.rebuild_history_for_all_tickers(rows, "2026-05-16", MODEL_VERSION)
    assert count == 2


# ---------------------------------------------------------------------------
# read_prior_scores -- drift_30d=0 universe-wide bug regression (Phase 3 hotfix)
# ---------------------------------------------------------------------------

def _seed_synthetic_history(
    store: LthcsPersist,
    ticker: str,
    anchor_date: str,
    days: int,
    *,
    score_fn=None,
) -> None:
    """Write `days` calendar days of history ending the day BEFORE anchor_date.

    Each entry's score is ``score_fn(offset)`` where ``offset`` is the
    integer number of days before anchor_date (1..days). Default
    ``score_fn`` is a simple linear ramp ``50.0 + offset * 0.1`` so the
    arithmetic checks below are exact.
    """
    from datetime import date as _date, timedelta as _td
    if score_fn is None:
        score_fn = lambda off: round(50.0 + off * 0.1, 2)
    anchor = _date.fromisoformat(anchor_date)
    for off in range(1, days + 1):
        d = (anchor - _td(days=off)).isoformat()
        store.append_history_entry(
            ticker, d, score_fn(off), "monitor", MODEL_VERSION,
        )


def test_read_prior_scores_returns_none_when_no_history(store: LthcsPersist) -> None:
    out = store.read_prior_scores("AAPL", "2026-05-19")
    assert out == {"1d": None, "7d": None, "30d": None, "90d": None}


def test_read_prior_scores_exact_offset_match(store: LthcsPersist) -> None:
    """When history has an entry exactly N days back, pick that score."""
    _seed_synthetic_history(store, "AAPL", "2026-05-19", days=91)
    out = store.read_prior_scores("AAPL", "2026-05-19")
    # score_fn(off) = 50.0 + off * 0.1
    assert out["1d"] == pytest.approx(50.1)
    assert out["7d"] == pytest.approx(50.7)
    assert out["30d"] == pytest.approx(53.0)
    assert out["90d"] == pytest.approx(59.0)


def test_read_prior_scores_excludes_calc_date_itself(store: LthcsPersist) -> None:
    """The bug we're fixing: --force re-runs must not return today's score.

    If calc_date itself is in history, drift_1d should look back to
    yesterday, not today.
    """
    # Yesterday: 60.0; today's row also exists in history at 80.0.
    store.append_history_entry("AAPL", "2026-05-18", 60.0, "monitor", MODEL_VERSION)
    store.append_history_entry("AAPL", "2026-05-19", 80.0, "elite", MODEL_VERSION)
    out = store.read_prior_scores("AAPL", "2026-05-19")
    # 1d back is 60.0 (yesterday) -- NOT 80.0 (today).
    assert out["1d"] == pytest.approx(60.0)


def test_read_prior_scores_nearest_prior_when_gap(store: LthcsPersist) -> None:
    """Weekend / gap-day case: target = calc_date - 7d falls on a missing
    date, so the nearest-prior entry should be picked.
    """
    # Seed entries only on M, W, F. calc_date = a Monday 2026-05-18.
    # Target dates for windows:
    #   1d  -> 2026-05-17 (Sun)  -> nearest prior is Fri 2026-05-15
    #   7d  -> 2026-05-11 (Mon)  -> exact entry exists
    #   30d -> 2026-04-18        -> nearest prior is whatever's earliest seeded
    store.append_history_entry("AAPL", "2026-05-15", 70.0, "monitor", MODEL_VERSION)  # Fri
    store.append_history_entry("AAPL", "2026-05-13", 65.0, "monitor", MODEL_VERSION)  # Wed
    store.append_history_entry("AAPL", "2026-05-11", 60.0, "monitor", MODEL_VERSION)  # Mon
    out = store.read_prior_scores("AAPL", "2026-05-18")
    assert out["1d"] == pytest.approx(70.0)  # latest <= 2026-05-17 is Fri 70.0
    assert out["7d"] == pytest.approx(60.0)  # exact match on 2026-05-11
    # 30d / 90d back: no entry that old -> None
    assert out["30d"] is None
    assert out["90d"] is None


def test_read_prior_scores_drives_nonzero_drift(store: LthcsPersist) -> None:
    """End-to-end seam: read_prior_scores -> compute_drift produces
    non-zero drift for ALL four windows when ≥91 days of history exist.

    Regression for the Phase 3 audit finding: drift_30d (and all drift
    windows) were universally 0.0 across the 167-ticker universe
    because the daily pipeline never wired prior_scores into
    compute_lthcs_score.
    """
    from lthcs.score import compute_drift

    _seed_synthetic_history(store, "AAPL", "2026-05-19", days=91)
    priors = store.read_prior_scores("AAPL", "2026-05-19")
    today_score = 80.0
    drift = compute_drift(today_score, priors)
    # Every window has a prior -> every drift must be non-zero and
    # match the analytic delta (today - prior) within rounding.
    for win in ("1d", "7d", "30d", "90d"):
        assert priors[win] is not None
        assert drift["drift_" + win] != 0.0
        assert drift["drift_" + win] == pytest.approx(today_score - priors[win], abs=0.05)


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------

def test_rebuild_index_empty_snapshots(store: LthcsPersist) -> None:
    path = store.rebuild_index(MODEL_VERSION)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {
        "model_version": MODEL_VERSION,
        "dates": [],
        "latest": None,
        "count": 0,
    }


def test_rebuild_index_scans_directory_and_picks_latest(store: LthcsPersist) -> None:
    # Write three snapshots out of order.
    for date in ("2026-05-15", "2026-05-17", "2026-05-16"):
        store.write_snapshot(date, MODEL_VERSION, "standard_compounder", [_sample_score_row()])
    store.rebuild_index(MODEL_VERSION)
    payload = json.loads(store.index_path().read_text(encoding="utf-8"))
    assert payload["dates"] == ["2026-05-17", "2026-05-16", "2026-05-15"]
    assert payload["latest"] == "2026-05-17"
    assert payload["count"] == 3
    assert payload["model_version"] == MODEL_VERSION


def test_rebuild_index_ignores_index_and_tmp_files(store: LthcsPersist) -> None:
    store.write_snapshot(
        "2026-05-16", MODEL_VERSION, "standard_compounder", [_sample_score_row()]
    )
    # Leave a stale tmp file behind, the kind a crashed atomic-write would leave.
    stale_tmp = store.snapshots_dir / ".tmp-abcd1234.json"
    stale_tmp.write_text("{not valid json", encoding="utf-8")
    # And a stray non-date JSON the dashboard team might park here.
    (store.snapshots_dir / "README.json").write_text("{}", encoding="utf-8")

    store.rebuild_index(MODEL_VERSION)
    payload = json.loads(store.index_path().read_text(encoding="utf-8"))
    assert payload["dates"] == ["2026-05-16"]
    assert payload["latest"] == "2026-05-16"
    assert payload["count"] == 1


def test_list_snapshot_dates_sorted_desc(store: LthcsPersist) -> None:
    for date in ("2026-05-15", "2026-05-17", "2026-05-16"):
        store.write_snapshot(date, MODEL_VERSION, "standard_compounder", [_sample_score_row()])
    assert store.list_snapshot_dates() == ["2026-05-17", "2026-05-16", "2026-05-15"]


# ---------------------------------------------------------------------------
# Atomic write resilience
# ---------------------------------------------------------------------------

def test_stale_tmp_file_does_not_break_reads(store: LthcsPersist) -> None:
    # Write a real snapshot, then drop a stale .tmp-*.json alongside it.
    store.write_snapshot(
        "2026-05-16", MODEL_VERSION, "standard_compounder", [_sample_score_row()]
    )
    (store.snapshots_dir / ".tmp-zzzz.json").write_text(
        "garbage", encoding="utf-8"
    )
    # read_snapshot of the real file still works.
    payload = store.read_snapshot("2026-05-16")
    assert payload["calc_date"] == "2026-05-16"
    # And the index rebuild also tolerates it.
    store.rebuild_index(MODEL_VERSION)
    idx = json.loads(store.index_path().read_text(encoding="utf-8"))
    assert idx["dates"] == ["2026-05-16"]


def test_write_creates_no_lingering_tmp_files(store: LthcsPersist) -> None:
    store.write_snapshot(
        "2026-05-16", MODEL_VERSION, "standard_compounder", [_sample_score_row()]
    )
    leftover = [p.name for p in store.snapshots_dir.iterdir() if p.name.startswith(".tmp-")]
    assert leftover == []


def test_atomic_write_cleans_tmp_on_failure(
    store: LthcsPersist, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the JSON encoder raises mid-write, the .tmp file is cleaned up."""
    real_dump = json.dump

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated encode failure")

    monkeypatch.setattr(json, "dump", boom)
    with pytest.raises(RuntimeError):
        store.write_snapshot(
            "2026-05-16", MODEL_VERSION, "standard_compounder", [_sample_score_row()]
        )
    monkeypatch.setattr(json, "dump", real_dump)

    leftover = [
        p.name for p in store.snapshots_dir.iterdir() if p.name.startswith(".tmp-")
    ]
    assert leftover == []
    # And the destination file was never created.
    assert not store.snapshot_path("2026-05-16").exists()
