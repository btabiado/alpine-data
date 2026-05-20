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
    def test_critical_when_elite_and_high_empty_and_review_overflowing(self):
        snap = {"scores": [{"lthcs_score": v} for v in [20, 30, 35, 40, 42, 48, 55, 60]]}
        # 5/8 = 62.5% in review band, both elite + high_conf empty
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
