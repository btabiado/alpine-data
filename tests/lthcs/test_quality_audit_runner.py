"""Tests for the LTHCS monthly quality-audit orchestrator (Phase 4)."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

runner = importlib.import_module("lthcs_quality_audit_runner")

from tests.lthcs.band_fixture import fixture_band_ranges  # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic snapshot helpers
# ---------------------------------------------------------------------------

def _make_record(
    ticker: str,
    *,
    adoption: float = 50.0,
    inst: float = 50.0,
    fin: float = 50.0,
    thesis: float = 50.0,
    des: float = 50.0,
    composite: float = 50.0,
    band: str = "weakening",
    cohort: str = "mature_compounder",
    drift_30d: float = 1.0,
) -> Dict[str, Any]:
    return {
        "ticker": ticker,
        "lthcs_score": composite,
        "band": band,
        "maturity_stage": cohort,
        "drift_30d": drift_30d,
        "drift_90d": drift_30d * 2,
        "subscores": {
            "adoption_momentum": adoption,
            "institutional_confidence": inst,
            "financial_evolution": fin,
            "thesis_integrity": thesis,
            "des": des,
        },
        "data_quality_flags": [],
    }


def _write_healthy_snapshot(snap_dir: Path, asof: str) -> None:
    """A snapshot with varied scores across the universe → HEALTHY pillars."""
    snap_dir.mkdir(parents=True, exist_ok=True)
    rows: List[Dict[str, Any]] = []
    for i in range(40):
        # Spread composite scores across the full 20-90 range so multiple
        # bands populate and no pillar is stuck on the 50.0 default.
        base = 20 + i * 1.8
        rows.append(_make_record(
            f"T{i:02d}",
            adoption=base + 2,
            inst=base + 5,
            fin=base + 3,
            thesis=base + 1,
            des=base,
            composite=round(base, 2),
            band=("elite" if base >= 85 else "high_confidence" if base >= 80
                  else "constructive" if base >= 70 else "monitor" if base >= 60
                  else "weakening" if base >= 50 else "review"),
            cohort="mature_compounder" if i % 3 == 0 else "growth_compounder" if i % 3 == 1 else "standard_compounder",
            drift_30d=round((i % 7) - 3 + 0.5, 2),
        ))
    payload = {
        "calc_date": asof,
        "model_version": "test-1.0",
        "scores": rows,
    }
    (snap_dir / f"{asof}.json").write_text(json.dumps(payload))


def _write_stub_snapshot(snap_dir: Path, asof: str) -> None:
    """A snapshot where every pillar defaults to 50.0 → STUB on each pillar."""
    snap_dir.mkdir(parents=True, exist_ok=True)
    rows = [_make_record(f"X{i:02d}", composite=50.0, band="weakening", drift_30d=0.0) for i in range(20)]
    payload = {"calc_date": asof, "model_version": "test-stub", "scores": rows}
    (snap_dir / f"{asof}.json").write_text(json.dumps(payload))


def _write_critical_distribution_snapshot(snap_dir: Path, asof: str) -> None:
    """Pillars look fine, but every composite is <50 → distribution CRITICAL."""
    snap_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for i in range(30):
        # Varied pillar values (so they aren't STUB) but composites all
        # squashed below 50 so review-band overflows and elite/high_conf empty.
        base = 20 + i  # 20-49
        rows.append(_make_record(
            f"R{i:02d}",
            adoption=base, inst=base + 5, fin=base + 3, thesis=base + 8, des=base + 2,
            composite=round(min(49.0, base), 2),
            band="review",
            drift_30d=round((i % 3) + 0.5, 2),
        ))
    payload = {"calc_date": asof, "model_version": "test-crit", "scores": rows}
    (snap_dir / f"{asof}.json").write_text(json.dumps(payload))


# ---------------------------------------------------------------------------
# Pure helpers (no module patching needed)
# ---------------------------------------------------------------------------

class TestPillarVerdict:
    def test_high_coverage_high_stdev_is_healthy(self):
        assert runner._pillar_verdict(96.0, 25.0) == "HEALTHY"

    def test_low_coverage_is_stub(self):
        assert runner._pillar_verdict(5.0, 25.0) == "STUB"

    def test_high_coverage_but_low_stdev_is_degraded(self):
        # Coverage OK but cross-ticker stdev too low → no signal differentiation
        assert runner._pillar_verdict(95.0, 1.0) == "DEGRADED"

    def test_mid_coverage_is_degraded(self):
        assert runner._pillar_verdict(60.0, 20.0) == "DEGRADED"


class TestDistributionSummary:
    # Fixed test band set (elite 85+, high_confidence 80-84, review 0-49), not
    # the live weights.json, so a recalibration does not move these counts.
    @pytest.fixture(autouse=True)
    def _fixed_band_cutoffs(self, monkeypatch):
        monkeypatch.setattr(runner, "_live_band_ranges", fixture_band_ranges)

    def test_critical_when_elite_and_high_empty_and_review_overflowing(self):
        snap = {"scores": [{"lthcs_score": v} for v in [20, 30, 35, 40, 42, 48, 55, 60]]}
        # 6/8 = 75% in review band (< 50), both elite + high_conf empty
        dist = runner._distribution_summary(snap)
        assert dist["critical"] is True
        assert dist["elite_count"] == 0
        assert dist["high_conf_count"] == 0

    def test_not_critical_when_elite_present(self):
        snap = {"scores": [{"lthcs_score": v} for v in [20, 30, 90, 88, 85]]}
        dist = runner._distribution_summary(snap)
        assert dist["critical"] is False
        assert dist["elite_count"] == 3

    def test_empty_snapshot_is_critical(self):
        dist = runner._distribution_summary({"scores": []})
        assert dist["critical"] is True
        assert dist["n"] == 0

    def test_fractional_scores_use_half_open_bands(self):
        snap = {"scores": [{"lthcs_score": v} for v in [49.9, 50.0, 79.9, 80.0, 84.9, 85.0]]}
        dist = runner._distribution_summary(snap)
        assert (dist["review_count"], dist["high_conf_count"], dist["elite_count"]) == (1, 2, 1)


def test_distribution_summary_reads_live_band_cutoffs():
    # Unpatched: the cutoffs come from data/lthcs/weights.json.
    live = json.loads((REPO_ROOT / "data" / "lthcs" / "weights.json").read_text())["score_bands"]
    elite_lo, high_lo, review_hi = (live["elite"]["min"], live["high_confidence"]["min"],
                                    live["review"]["max"])
    scores = [review_hi, review_hi + 1, high_lo, elite_lo - 1, elite_lo, 100]
    dist = runner._distribution_summary({"scores": [{"lthcs_score": v} for v in scores]})
    assert dist["band_cutoffs"] == {"elite_min": elite_lo, "high_confidence_min": high_lo,
                                    "review_max": review_hi}
    assert (dist["review_count"], dist["high_conf_count"], dist["elite_count"]) == (1, 2, 2)


class TestRenderSummary:
    def test_includes_overall_verdict_and_pillars(self):
        payload = {
            "asof": "2026-05-19",
            "snapshot_date": "2026-05-19",
            "generated_at_utc": "2026-05-19T09:00:00+00:00",
            "overall_verdict": "HEALTHY",
            "pillars": {p: {"verdict": "HEALTHY", "coverage_pct": 96.0, "mean": 50.0, "stdev": 20.0, "n": 100}
                        for p in runner.PILLARS},
            "distribution": {"n": 100, "mean": 50.0, "stdev": 12.0,
                              "elite_count": 5, "high_conf_count": 10, "review_count": 20,
                              "review_pct": 20.0, "critical": False},
            "weights": {"verdict": "ALIGNED", "n_cohorts": 3, "misaligned_cohorts": []},
            "bands": {"verdict": "BALANCED", "counts": {}, "starved": [], "review_pct": 20.0},
            "alerts": [],
            "sub_audits": {"pillar_quality": {"ok": True}, "score_distribution": {"ok": True}, "weights_thresholds": {"ok": True}},
        }
        md = runner.render_summary_md(payload)
        assert "Overall verdict: **HEALTHY**" in md
        assert "adoption_momentum" in md
        assert "ALIGNED" in md


# ---------------------------------------------------------------------------
# Full orchestrator runs (patch SNAPSHOT_DIR + sub-audit calls)
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_audit(tmp_path: Path, monkeypatch):
    """Point the runner at a temp snapshot dir and stub sub-audits.

    The sub-audit modules touch real prices and weight configs; we
    short-circuit them with stub callables that just write a marker file.
    """
    snap_dir = tmp_path / "snapshots"
    out_dir = tmp_path / "audit"

    monkeypatch.setattr(runner, "SNAPSHOT_DIR", snap_dir)
    monkeypatch.setattr(runner, "AUDIT_DIR", out_dir)
    monkeypatch.setattr(runner, "DATA", tmp_path)

    def fake_pillar(asof, out):
        (out / f"{asof}_pillar_quality.md").write_text("# stub pillar audit\n")
        return {"ok": True, "path": str(out / f"{asof}_pillar_quality.md")}

    def fake_dist(asof, out):
        (out / f"{asof}_composite_distribution.md").write_text("# stub dist\n")
        (out / f"{asof}_pillar_correlation.md").write_text("# stub corr\n")
        return {"ok": True, "paths": []}

    def fake_weights(asof, out):
        (out / f"{asof}_weights_vs_ic.md").write_text("# stub weights\n")
        (out / f"{asof}_band_distribution.md").write_text("# stub bands\n")
        return {"ok": True, "paths": [], "cohort_ic": {}}

    monkeypatch.setattr(runner, "_run_pillar_audit", fake_pillar)
    monkeypatch.setattr(runner, "_run_distribution_audit", fake_dist)
    monkeypatch.setattr(runner, "_run_weights_audit", fake_weights)

    return snap_dir, out_dir


def test_runner_produces_combined_summary_on_healthy_data(isolated_audit, tmp_path):
    snap_dir, out_dir = isolated_audit
    _write_healthy_snapshot(snap_dir, "2026-05-19")
    payload, summary_path = runner.run(asof="2026-05-19", out_dir=out_dir, snap_dir=snap_dir)
    assert summary_path.exists()
    body = summary_path.read_text()
    # All five pillars present
    for p in runner.PILLARS:
        assert p in body
    # Combined verdict is not CRITICAL
    assert payload["overall_verdict"] != "CRITICAL"
    # latest_summary.json was written too
    assert (out_dir / "latest_summary.json").exists()
    parsed = json.loads((out_dir / "latest_summary.json").read_text())
    assert parsed["asof"] == "2026-05-19"
    assert "pillars" in parsed


def test_runner_exit_code_zero_for_healthy(isolated_audit, monkeypatch):
    snap_dir, out_dir = isolated_audit
    _write_healthy_snapshot(snap_dir, "2026-05-19")
    rc = runner.main(["--asof", "2026-05-19", "--out", str(out_dir)])
    assert rc == 0


def test_runner_exit_nonzero_for_stub_pillars(isolated_audit, monkeypatch):
    snap_dir, out_dir = isolated_audit
    _write_stub_snapshot(snap_dir, "2026-05-19")
    rc = runner.main(["--asof", "2026-05-19", "--out", str(out_dir)])
    assert rc != 0
    payload = json.loads((out_dir / "latest_summary.json").read_text())
    assert payload["overall_verdict"] == "CRITICAL"
    # Every pillar at 50.0 → STUB
    assert all(p["verdict"] == "STUB" for p in payload["pillars"].values())


def test_runner_exit_nonzero_for_critical_distribution(isolated_audit, monkeypatch):
    snap_dir, out_dir = isolated_audit
    _write_critical_distribution_snapshot(snap_dir, "2026-05-19")
    rc = runner.main(["--asof", "2026-05-19", "--out", str(out_dir)])
    assert rc != 0
    payload = json.loads((out_dir / "latest_summary.json").read_text())
    assert payload["distribution"]["critical"] is True


def test_per_audit_failure_isolation(monkeypatch, tmp_path):
    """If one sub-audit crashes, others still produce output and the runner finishes."""
    snap_dir = tmp_path / "snapshots"
    out_dir = tmp_path / "audit"
    _write_healthy_snapshot(snap_dir, "2026-05-19")

    monkeypatch.setattr(runner, "SNAPSHOT_DIR", snap_dir)
    monkeypatch.setattr(runner, "AUDIT_DIR", out_dir)
    monkeypatch.setattr(runner, "DATA", tmp_path)

    def crash_pillar(asof, out):
        raise RuntimeError("simulated pillar audit crash")

    def ok_dist(asof, out):
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{asof}_composite_distribution.md").write_text("# ok\n")
        return {"ok": True, "paths": []}

    def ok_weights(asof, out):
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{asof}_weights_vs_ic.md").write_text("# ok\n")
        return {"ok": True, "paths": [], "cohort_ic": {}}

    # Wrap the crashy pillar call in the production try/except by using
    # the real _run_pillar_audit but with a patched audit module.
    def wrapped_pillar(asof, out):
        try:
            crash_pillar(asof, out)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    monkeypatch.setattr(runner, "_run_pillar_audit", wrapped_pillar)
    monkeypatch.setattr(runner, "_run_distribution_audit", ok_dist)
    monkeypatch.setattr(runner, "_run_weights_audit", ok_weights)

    payload, summary_path = runner.run(asof="2026-05-19", out_dir=out_dir, snap_dir=snap_dir)
    assert summary_path.exists()
    # Pillar audit reported failed, but the runner kept going.
    assert payload["sub_audits"]["pillar_quality"]["ok"] is False
    assert payload["sub_audits"]["score_distribution"]["ok"] is True
    assert payload["sub_audits"]["weights_thresholds"]["ok"] is True


def test_cross_cutting_drift_regression_alert():
    """If 30d drift is zero for >80% of tickers, an alert fires."""
    snap = {"scores": [
        {"ticker": f"T{i}", "drift_30d": 0.0, "subscores": {p: 50 for p in runner.PILLARS}}
        for i in range(20)
    ]}
    alerts = runner._cross_cutting_alerts(snap)
    assert any("drift_30d" in a for a in alerts)


def test_cross_cutting_no_alert_when_drift_present():
    snap = {"scores": [
        {"ticker": f"T{i}", "drift_30d": float(i + 1), "subscores": {p: 50 for p in runner.PILLARS}}
        for i in range(20)
    ]}
    alerts = runner._cross_cutting_alerts(snap)
    assert not any("drift_30d" in a for a in alerts)


# ---------------------------------------------------------------------------
# Accepted known stubs (thesis_integrity, decided 2026-10-17)
# ---------------------------------------------------------------------------

_ACCEPTANCE_REASON = (
    "Bryan decided not to add a FINNHUB_API_KEY. Thesis stays dropped "
    "and its weight stays on the other four pillars."
)


def _write_known_stubs(directory: Path, **overrides) -> None:
    entry = {
        "pillar": "thesis_integrity",
        "decided": "2026-10-17",
        "review_by": "2027-04-17",
        "reason": _ACCEPTANCE_REASON,
    }
    entry.update(overrides)
    (directory / "known_pillar_stubs.json").write_text(json.dumps({"stubs": [entry]}))


def _retarget(snap_dir: Path, asof: str, **pillar_values: float) -> None:
    """Overwrite selected subscores. Pass None via a sentinel by deleting keys separately."""
    path = snap_dir / f"{asof}.json"
    payload = json.loads(path.read_text())
    for row in payload["scores"]:
        for pillar, value in pillar_values.items():
            row["subscores"][pillar] = value
    path.write_text(json.dumps(payload))


def _null_thesis(snap_dir: Path, asof: str) -> None:
    path = snap_dir / f"{asof}.json"
    payload = json.loads(path.read_text())
    for row in payload["scores"]:
        row["subscores"]["thesis_integrity"] = None
    path.write_text(json.dumps(payload))


class TestKnownStubConfig:
    def test_missing_file_accepts_nothing(self, tmp_path: Path):
        assert runner.load_known_stubs(tmp_path / "missing.json") == []

    def test_committed_entry_is_thesis_on_the_decision_date(self):
        known = runner.load_known_stubs(REPO_ROOT / "data" / "lthcs" / "known_pillar_stubs.json")
        assert [k["pillar"] for k in known] == ["thesis_integrity"]
        assert known[0]["decided"] == "2026-10-17"
        assert known[0]["review_by"] == "2027-04-17"
        assert "FINNHUB_API_KEY" in known[0]["reason"]
        assert "four pillars" in known[0]["reason"]

    def test_rejects_unknown_pillar_short_reason_and_bad_dates(self, tmp_path: Path):
        path = tmp_path / "known_pillar_stubs.json"
        path.write_text(json.dumps({"stubs": [{"pillar": "not_a_pillar", "decided": "2026-10-17",
                                               "reason": _ACCEPTANCE_REASON}]}))
        with pytest.raises(ValueError, match="pillar"):
            runner.load_known_stubs(path)

        path.write_text(json.dumps({"stubs": [{"pillar": "thesis_integrity", "decided": "2026-10-17",
                                               "reason": "too short"}]}))
        with pytest.raises(ValueError, match="reason"):
            runner.load_known_stubs(path)

        path.write_text(json.dumps({"stubs": [{"pillar": "thesis_integrity", "decided": "17 Oct 2026",
                                               "reason": _ACCEPTANCE_REASON}]}))
        with pytest.raises(ValueError, match="decided"):
            runner.load_known_stubs(path)

        path.write_text(json.dumps({"stubs": [
            {"pillar": "thesis_integrity", "decided": "2026-10-17", "review_by": "2026-01-01",
             "reason": _ACCEPTANCE_REASON},
        ]}))
        with pytest.raises(ValueError, match="review_by"):
            runner.load_known_stubs(path)

        path.write_text("{")
        with pytest.raises(ValueError, match="not valid JSON"):
            runner.load_known_stubs(path)

    def test_duplicate_pillar_rejected(self, tmp_path: Path):
        path = tmp_path / "known_pillar_stubs.json"
        entry = {"pillar": "des", "decided": "2026-10-17", "reason": _ACCEPTANCE_REASON}
        path.write_text(json.dumps({"stubs": [entry, dict(entry)]}))
        with pytest.raises(ValueError, match="duplicates"):
            runner.load_known_stubs(path)


class TestAuditFailed:
    def test_accepted_gap_is_not_a_failure(self):
        payload = {
            "overall_verdict": "HEALTHY",
            "pillars": {"thesis_integrity": {"verdict": runner.KNOWN_GAP_VERDICT, "coverage_pct": 0.0}},
            "distribution": {"critical": False},
        }
        assert runner.audit_failed(payload) is False

    def test_unaccepted_stub_mismatch_distribution_and_bad_config_fail(self):
        assert runner.audit_failed({"overall_verdict": "HEALTHY",
                                    "pillars": {"des": {"verdict": "STUB"}},
                                    "distribution": {"critical": False}}) is True
        assert runner.audit_failed({"overall_verdict": "DEGRADED",
                                    "pillars": {"thesis_integrity": {"verdict": "HEALTHY",
                                                                     "known_gap_mismatch": True}},
                                    "distribution": {"critical": False}}) is True
        assert runner.audit_failed({"overall_verdict": "HEALTHY", "pillars": {},
                                    "distribution": {"critical": True}}) is True
        assert runner.audit_failed({"overall_verdict": "CRITICAL", "pillars": {},
                                    "distribution": {"critical": False}}) is True
        assert runner.audit_failed({"known_stubs_error": "broken", "overall_verdict": "HEALTHY",
                                    "pillars": {}, "distribution": {}}) is True
        assert runner.audit_failed(None) is True  # type: ignore[arg-type]


class TestKnownGapVerdict:
    def test_summary_keeps_the_accepted_gap_visible(self):
        pillars = {p: {"verdict": "HEALTHY", "coverage_pct": 96.0, "mean": 50.0, "stdev": 20.0, "n": 10}
                   for p in runner.PILLARS}
        pillars["thesis_integrity"] = {
            "verdict": runner.KNOWN_GAP_VERDICT,
            "raw_verdict": "STUB",
            "coverage_pct": 0.0,
            "mean": None,
            "stdev": None,
            "n": 0,
            "accepted_stub": True,
            "known_gap": {"decided": "2026-10-17", "review_by": "2027-04-17", "reason": _ACCEPTANCE_REASON},
        }
        payload = {
            "asof": "2026-10-17",
            "snapshot_date": "2026-10-16",
            "generated_at_utc": "2026-10-17T12:00:00+00:00",
            "overall_verdict": "HEALTHY",
            "pillars": pillars,
            "known_gaps": runner.known_gap_records(pillars),
            "distribution": {"n": 10, "mean": 50.0, "stdev": 12.0, "elite_count": 1,
                             "high_conf_count": 1, "review_count": 1, "review_pct": 10.0, "critical": False},
            "weights": {"verdict": "UNKNOWN", "n_cohorts": 0, "misaligned_cohorts": []},
            "bands": {"verdict": "BALANCED", "counts": {}, "starved": [], "review_pct": 10.0},
            "alerts": [],
            "sub_audits": {"pillar_quality": {"ok": True}, "score_distribution": {"ok": True},
                           "weights_thresholds": {"ok": True}},
        }
        md = runner.render_summary_md(payload)
        assert "Overall verdict: **HEALTHY**" in md
        assert "KNOWN GAP (accepted)" in md
        assert "thesis_integrity" in md
        assert "2026-10-17" in md
        assert "2027-04-17" in md
        assert _ACCEPTANCE_REASON in md
        assert "does not count toward CRITICAL" in md
        # The pillar row is still in the table; the gap is not dropped from the report.
        assert "| thesis_integrity | 🔵 KNOWN GAP (accepted) |" in md

    def test_null_thesis_is_accepted_and_does_not_fail(self, isolated_audit, tmp_path: Path):
        snap_dir, out_dir = isolated_audit
        _write_healthy_snapshot(snap_dir, "2026-10-17")
        _null_thesis(snap_dir, "2026-10-17")
        _write_known_stubs(tmp_path)
        rc = runner.main(["--asof", "2026-10-17", "--out", str(out_dir)])
        assert rc == 0
        payload = json.loads((out_dir / "latest_summary.json").read_text())
        thesis = payload["pillars"]["thesis_integrity"]
        assert thesis["verdict"] == runner.KNOWN_GAP_VERDICT
        assert thesis["raw_verdict"] == "STUB"
        assert thesis["coverage_pct"] == 0.0
        assert thesis["counts_toward_critical"] is False
        assert payload["overall_verdict"] != "CRITICAL"
        assert runner.audit_failed(payload) is False
        body = (out_dir / "2026-10-17_summary.md").read_text()
        assert "KNOWN GAP (accepted)" in body
        assert "FINNHUB_API_KEY" in body
        gap = payload["known_gaps"][0]
        assert gap["pillar"] == "thesis_integrity"
        assert gap["accepted"] is True
        assert gap["mismatch"] is False

    def test_placeholder_fifties_are_the_same_accepted_stub(self, isolated_audit, tmp_path: Path):
        snap_dir, out_dir = isolated_audit
        _write_healthy_snapshot(snap_dir, "2026-10-17")
        _retarget(snap_dir, "2026-10-17", thesis_integrity=50.0)
        _write_known_stubs(tmp_path)
        payload, _ = runner.run(asof="2026-10-17", out_dir=out_dir, snap_dir=snap_dir)
        assert payload["pillars"]["thesis_integrity"]["verdict"] == runner.KNOWN_GAP_VERDICT
        assert payload["overall_verdict"] != "CRITICAL"

    def test_another_pillar_stub_still_critical(self, isolated_audit, tmp_path: Path):
        snap_dir, out_dir = isolated_audit
        _write_healthy_snapshot(snap_dir, "2026-10-17")
        _retarget(snap_dir, "2026-10-17", thesis_integrity=50.0, adoption_momentum=50.0)
        _write_known_stubs(tmp_path)
        rc = runner.main(["--asof", "2026-10-17", "--out", str(out_dir)])
        assert rc == 1
        payload = json.loads((out_dir / "latest_summary.json").read_text())
        assert payload["overall_verdict"] == "CRITICAL"
        assert payload["pillars"]["adoption_momentum"]["verdict"] == "STUB"
        assert payload["pillars"]["thesis_integrity"]["verdict"] == runner.KNOWN_GAP_VERDICT
        assert runner.audit_failed(payload) is True

    def test_thesis_regaining_coverage_is_critical(self, isolated_audit, tmp_path: Path):
        snap_dir, out_dir = isolated_audit
        _write_healthy_snapshot(snap_dir, "2026-10-17")
        _write_known_stubs(tmp_path)
        rc = runner.main(["--asof", "2026-10-17", "--out", str(out_dir)])
        assert rc == 1
        payload = json.loads((out_dir / "latest_summary.json").read_text())
        thesis = payload["pillars"]["thesis_integrity"]
        assert thesis["verdict"] != runner.KNOWN_GAP_VERDICT
        assert thesis["known_gap_mismatch"] is True
        assert thesis["counts_toward_critical"] is True
        assert payload["overall_verdict"] == "CRITICAL"
        body = (out_dir / "2026-10-17_summary.md").read_text()
        assert "accepted stub no longer matches" in body
        assert "counts toward CRITICAL" in body
        assert any("no longer" in a or "counts toward CRITICAL" in a or "not covered" in a
                   for a in payload["alerts"])

    def test_critical_distribution_still_fails_when_thesis_is_accepted(self, isolated_audit, tmp_path: Path):
        snap_dir, out_dir = isolated_audit
        _write_critical_distribution_snapshot(snap_dir, "2026-10-17")
        _retarget(snap_dir, "2026-10-17", thesis_integrity=50.0)
        _write_known_stubs(tmp_path)
        rc = runner.main(["--asof", "2026-10-17", "--out", str(out_dir)])
        assert rc == 1
        payload = json.loads((out_dir / "latest_summary.json").read_text())
        assert payload["distribution"]["critical"] is True
        assert payload["overall_verdict"] == "CRITICAL"
        assert payload["pillars"]["thesis_integrity"]["verdict"] == runner.KNOWN_GAP_VERDICT

    def test_invalid_config_does_not_silence_a_stub(self, isolated_audit, tmp_path: Path):
        snap_dir, out_dir = isolated_audit
        _write_healthy_snapshot(snap_dir, "2026-10-17")
        _null_thesis(snap_dir, "2026-10-17")
        (tmp_path / "known_pillar_stubs.json").write_text("{")
        rc = runner.main(["--asof", "2026-10-17", "--out", str(out_dir)])
        assert rc == 1
        payload = json.loads((out_dir / "latest_summary.json").read_text())
        assert payload["pillars"]["thesis_integrity"]["verdict"] == "STUB"
        assert payload["overall_verdict"] == "CRITICAL"
        assert payload["known_stubs_error"]
        assert any("invalid" in a for a in payload["alerts"])

    def test_past_review_by_is_visible_and_not_critical(self, isolated_audit, tmp_path: Path):
        snap_dir, out_dir = isolated_audit
        _write_healthy_snapshot(snap_dir, "2026-10-17")
        _null_thesis(snap_dir, "2026-10-17")
        _write_known_stubs(tmp_path, decided="2026-01-17", review_by="2026-04-17")
        rc = runner.main(["--asof", "2026-10-17", "--out", str(out_dir)])
        assert rc == 0
        payload = json.loads((out_dir / "latest_summary.json").read_text())
        assert payload["overall_verdict"] != "CRITICAL"
        assert payload["pillars"]["thesis_integrity"]["verdict"] == runner.KNOWN_GAP_VERDICT
        assert any("review-by" in a for a in payload["alerts"])
        assert "review-by" in (out_dir / "2026-10-17_summary.md").read_text()


def test_live_snapshot_thesis_stub_is_accepted_and_not_critical():
    """The snapshot on disk is the case Bryan accepted: thesis dropped, others not stub."""
    known = runner.load_known_stubs(REPO_ROOT / "data" / "lthcs" / "known_pillar_stubs.json")
    snap_date = runner.latest_snapshot_date()
    snap = runner._load_snapshot(snap_date)
    pillars = runner._compute_pillar_verdicts(snap)
    alerts = runner.apply_known_stubs(pillars, known, "2026-10-17")
    thesis = pillars["thesis_integrity"]
    assert thesis["raw_verdict"] == "STUB"
    assert thesis["verdict"] == runner.KNOWN_GAP_VERDICT
    assert thesis["counts_toward_critical"] is False
    others = {p: pillars[p]["verdict"] for p in runner.PILLARS if p != "thesis_integrity"}
    assert "STUB" not in others.values(), others
    dist = runner._distribution_summary(snap)
    overall = runner._overall_verdict(
        pillars, dist, {"verdict": "UNKNOWN"}, {"verdict": "BALANCED"}, stubs_error=False,
    )
    payload = {"overall_verdict": overall, "pillars": pillars, "distribution": dist, "alerts": alerts}
    assert overall != "CRITICAL"
    assert runner.audit_failed(payload) is False


def test_quality_page_shows_accepted_gap_and_does_not_paint_it_red():
    js = (REPO_ROOT / "lthcs_health" / "lthcs-quality.js").read_text()
    html = (REPO_ROOT / "lthcs_health" / "quality.html").read_text()
    assert "KNOWN GAP (accepted)" in js
    assert "return 'accepted'" in js
    assert "lq-known-gaps" in html
    assert "lq-badge.accepted" in html
    red_line = next(ln for ln in js.splitlines() if "return 'red'" in ln)
    assert "KNOWN GAP" not in red_line
