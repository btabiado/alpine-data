"""Tests for scripts/lthcs_pillar_quality_audit.py — math only, no I/O against repo data."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import lthcs_pillar_quality_audit as audit


# -------- pure math --------


def test_percentile_basic():
    xs = [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    # p0 -> min, p100 -> max
    assert audit.percentile(xs, 0.0) == 0
    assert audit.percentile(xs, 1.0) == 100
    # p50 of 11-element list -> index 5
    assert audit.percentile(xs, 0.5) == 50


def test_percentile_empty_is_nan():
    import math

    assert math.isnan(audit.percentile([], 0.5))


def test_pillar_stats_distribution_fields():
    values = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
    s = audit.pillar_stats(values)
    assert s["n"] == 10
    assert s["mean"] == 55.0
    assert s["count_at_floor"] == 0
    # 100.0 IS at ceiling
    assert s["count_at_ceiling"] == 1
    # percentile sanity
    assert s["p5"] <= s["p25"] <= s["p50"] <= s["p75"] <= s["p95"]


def test_pillar_stats_counts_floor_and_ceiling():
    values = [0.0, 0.0, 50.0, 100.0]
    s = audit.pillar_stats(values)
    assert s["count_at_floor"] == 2
    assert s["count_at_ceiling"] == 1


def test_pillar_stats_empty():
    assert audit.pillar_stats([]) == {"n": 0}


def test_coverage_pct_default_is_50():
    # 3 of 5 are non-50.0
    vals = [50.0, 50.0, 47.0, 80.0, 12.0]
    assert audit.coverage_pct(vals) == 60.0


def test_coverage_pct_all_default():
    assert audit.coverage_pct([50.0, 50.0, 50.0]) == 0.0


def test_coverage_pct_empty():
    assert audit.coverage_pct([]) == 0.0


def test_per_ticker_stdev_filters_short_histories():
    # ticker_a: 1 sample -> excluded; ticker_b: stable -> 0; ticker_c: spread -> >0
    hist = {"A": [50.0], "B": [50.0, 50.0, 50.0], "C": [10.0, 90.0]}
    out = audit.per_ticker_stdev(hist)
    assert len(out) == 2  # A dropped
    assert min(out) == 0.0
    assert max(out) > 0.0


# -------- record-level helpers --------


def _records_fixture():
    return [
        {"ticker": "AAA", "subscores": {"p": 10.0}, "maturity_stage": "growth_compounder"},
        {"ticker": "BBB", "subscores": {"p": 90.0}, "maturity_stage": "growth_compounder"},
        {"ticker": "CCC", "subscores": {"p": 50.0}, "maturity_stage": "mature_compounder"},
        {"ticker": "DDD", "subscores": {"p": 75.0}, "maturity_stage": "mature_compounder"},
        {"ticker": "EEE", "subscores": {"p": 25.0}, "maturity_stage": "mature_compounder"},
        {"ticker": "FFF", "subscores": {"p": 100.0}, "maturity_stage": "mature_compounder"},
    ]


def test_top_bottom_orders_correctly():
    recs = _records_fixture()
    top, bottom = audit.top_bottom(recs, "p", k=2)
    # Top descending by value
    assert top[0] == ("FFF", 100.0)
    assert top[1] == ("BBB", 90.0)
    # Bottom ascending
    assert bottom[0] == ("AAA", 10.0)
    assert bottom[1] == ("EEE", 25.0)


def test_cohort_means_uses_universe_then_record_fallback():
    recs = _records_fixture()
    # Universe disagrees with record (AAA tagged mature in universe)
    cohorts = {"AAA": "mature_compounder", "BBB": "growth_compounder"}
    means = audit.cohort_means(recs, "p", cohorts)
    # AAA forced into mature via universe map
    # mature now: AAA(10), CCC(50), DDD(75), EEE(25), FFF(100) = mean 52.0
    assert means["mature_compounder"] == 52.0
    # growth only BBB (90)
    assert means["growth_compounder"] == 90.0


# -------- file-aware end-to-end with tmp dir --------


@pytest.fixture
def tmp_snap_dir(tmp_path: Path) -> Path:
    snap_dir = tmp_path / "snapshots"
    snap_dir.mkdir()
    # 3 days of two tickers, varying scores for one pillar
    days = ["2026-01-01", "2026-01-02", "2026-01-03"]
    for i, d in enumerate(days):
        records = [
            {
                "ticker": "X",
                "subscores": {
                    "adoption_momentum": 50.0 + i,  # rises
                    "institutional_confidence": 50.0,
                    "financial_evolution": 50.0,
                    "thesis_integrity": 50.0,
                    "des": 50.0,
                },
                "maturity_stage": "growth_compounder",
            },
            {
                "ticker": "Y",
                "subscores": {
                    "adoption_momentum": 80.0,
                    "institutional_confidence": 0.0,
                    "financial_evolution": 100.0,
                    "thesis_integrity": 50.0,
                    "des": 60.0,
                },
                "maturity_stage": "mature_compounder",
            },
        ]
        snap = {"calc_date": d, "model_version": "test", "scores": records}
        (snap_dir / f"{d}.json").write_text(json.dumps(snap))
    return snap_dir


def test_latest_snapshot_date_picks_max(tmp_snap_dir):
    assert audit.latest_snapshot_date(tmp_snap_dir) == "2026-01-03"


def test_recent_window_truncates(tmp_snap_dir):
    w = audit.recent_window(tmp_snap_dir, "2026-01-03", 2)
    assert w == ["2026-01-02", "2026-01-03"]
    w_full = audit.recent_window(tmp_snap_dir, "2026-01-03", 30)
    assert w_full == ["2026-01-01", "2026-01-02", "2026-01-03"]


def test_collect_pillar_history_assembles_per_ticker_series(tmp_snap_dir):
    window = ["2026-01-01", "2026-01-02", "2026-01-03"]
    hist = audit.collect_pillar_history(tmp_snap_dir, window, "adoption_momentum")
    assert hist["X"] == [50.0, 51.0, 52.0]
    assert hist["Y"] == [80.0, 80.0, 80.0]


def test_build_report_emits_all_five_pillars(monkeypatch, tmp_snap_dir, tmp_path):
    # Stub cohort_map so it doesn't touch repo's real universe.json
    monkeypatch.setattr(audit, "cohort_map", lambda *a, **k: {"X": "growth_compounder", "Y": "mature_compounder"})
    report = audit.build_report("2026-01-03", snap_dir=tmp_snap_dir)
    for pillar in audit.PILLARS:
        assert f"## {pillar}" in report
    # Floor + ceiling counts reflect Y's institutional=0 and financial=100
    assert "Floor (==0): 1" in report
    assert "Ceiling (==100): 1" in report


def test_build_report_handles_missing_pillar_gracefully(monkeypatch, tmp_path):
    snap_dir = tmp_path / "snapshots"
    snap_dir.mkdir()
    # Snapshot missing 'des' for one ticker
    snap = {
        "calc_date": "2026-01-01",
        "model_version": "test",
        "scores": [
            {"ticker": "X", "subscores": {"adoption_momentum": 50.0, "institutional_confidence": 50.0, "financial_evolution": 50.0, "thesis_integrity": 50.0, "des": 50.0}},
            {"ticker": "Y", "subscores": {"adoption_momentum": 60.0, "institutional_confidence": 60.0, "financial_evolution": 60.0, "thesis_integrity": 60.0}},
        ],
    }
    (snap_dir / "2026-01-01.json").write_text(json.dumps(snap))
    monkeypatch.setattr(audit, "cohort_map", lambda *a, **k: {})
    # Should not raise even though Y lacks 'des'
    report = audit.build_report("2026-01-01", snap_dir=snap_dir)
    assert "## des" in report
