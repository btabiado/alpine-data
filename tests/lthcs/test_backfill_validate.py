"""Tests for ``scripts/lthcs_backfill_validate.py``.

Each test builds a tiny synthetic LTHCS data tree in ``tmp_path`` and
calls ``run_validation`` directly. The CLI is exercised separately.
"""

from __future__ import annotations

import importlib.util
import json
import math
from datetime import date
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Import the script module (it lives under scripts/ not in a package)
# ---------------------------------------------------------------------------

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "lthcs_backfill_validate.py"


@pytest.fixture(scope="module")
def lbv():
    spec = importlib.util.spec_from_file_location("lthcs_backfill_validate", SCRIPT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Synthetic data builders
# ---------------------------------------------------------------------------

PILLARS = (
    "adoption_momentum",
    "institutional_confidence",
    "financial_evolution",
    "thesis_integrity",
    "des",
)

SCORE_BANDS = {
    "elite":          {"min": 90, "max": 100},
    "high_confidence": {"min": 80, "max": 89},
    "constructive":   {"min": 70, "max": 79},
    "monitor":        {"min": 60, "max": 69},
    "weakening":      {"min": 50, "max": 59},
    "review":         {"min": 0,  "max": 49},
}


def _band_for(score: float) -> str:
    for name, spec in SCORE_BANDS.items():
        if spec["min"] <= score <= spec["max"]:
            return name
    return "review"


def _build_root(tmp_path: Path, tickers=("AAPL", "MSFT", "NVDA")) -> Path:
    """Create a minimal lthcs data root with universe + weights."""
    root = tmp_path / "lthcs"
    (root / "snapshots").mkdir(parents=True)
    (root / "variable_detail").mkdir(parents=True)
    (root / "narratives").mkdir(parents=True)
    (root / "history" / "by_ticker").mkdir(parents=True)

    universe = {
        "version": "test",
        "tickers": [{"ticker": t, "active": True} for t in tickers],
    }
    (root / "universe.json").write_text(json.dumps(universe))

    weights = {"version": "test", "score_bands": SCORE_BANDS}
    (root / "weights.json").write_text(json.dumps(weights))
    return root


def _write_day(
    root: Path,
    d: str,
    tickers,
    *,
    score: float = 55.0,
    band_override: str | None = None,
    composite_override: float | None = None,
    as_of_mode: str | None = None,
    vd_rows_override: int | None = None,
    nan_for: tuple[str, ...] = (),
    omit_snapshot: bool = False,
    omit_variable_detail: bool = False,
    omit_narratives: bool = False,
    include_article_count: bool = True,
) -> None:
    """Write a full set of (snapshot, variable_detail, narratives) for one date.

    ``include_article_count`` controls the heuristic backfill detector.
    """
    if not omit_snapshot:
        scores = []
        for t in tickers:
            s = composite_override if composite_override is not None else score
            row_score: object = s
            if t in nan_for:
                row_score = None
            scores.append({
                "ticker": t,
                "lthcs_score": row_score,
                "band": band_override if band_override is not None else _band_for(float(s)),
                "subscores": {p: 55.0 for p in PILLARS},
            })
        snap = {"calc_date": d, "model_version": "test", "scores": scores}
        if as_of_mode:
            snap["as_of_mode"] = as_of_mode
        (root / "snapshots" / f"{d}.json").write_text(json.dumps(snap))

    if not omit_variable_detail:
        variables = []
        n_rows = vd_rows_override if vd_rows_override is not None else len(tickers) * len(PILLARS)
        # Emit in a deterministic order — ticker × pillar — up to n_rows.
        emitted = 0
        for t in tickers:
            for p in PILLARS:
                if emitted >= n_rows:
                    break
                comps = {}
                if p == "thesis_integrity" and include_article_count:
                    comps["article_count"] = 25
                variables.append({
                    "ticker": t,
                    "pillar": p,
                    "components": comps,
                    "sub_score": 55.0,
                })
                emitted += 1
            if emitted >= n_rows:
                break
        vd = {"calc_date": d, "model_version": "test", "variables": variables}
        (root / "variable_detail" / f"{d}.json").write_text(json.dumps(vd))

    if not omit_narratives:
        narr = {
            "calc_date": d,
            "model_version": "test",
            "narratives": [
                {"ticker": t, "todays_take": "test"} for t in tickers
            ],
        }
        (root / "narratives" / f"{d}.json").write_text(json.dumps(narr))


def _write_history(root: Path, ticker: str, dates: list[str], score: float = 55.0) -> None:
    rows = [{"date": d, "score": score, "band": _band_for(score)} for d in dates]
    (root / "history" / "by_ticker" / f"{ticker}.json").write_text(
        json.dumps({"ticker": ticker, "history": rows})
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_clean_two_day_backfill_passes(tmp_path, lbv):
    """Happy path: 2 days, full coverage, no NaN — exit 0."""
    tickers = ("AAPL", "MSFT", "NVDA")
    root = _build_root(tmp_path, tickers)
    for d in ("2026-01-01", "2026-01-02"):
        _write_day(root, d, tickers, score=55.0)
    for t in tickers:
        _write_history(root, t, ["2026-01-01", "2026-01-02"])

    report, _, _, _ = lbv.run_validation(
        root, start=date(2026, 1, 1), end=date(2026, 1, 2),
        sample_tickers=list(tickers),
    )
    assert report.failures == [], [f.message for f in report.failures]
    assert report.warnings == [], [f.message for f in report.warnings]
    assert report.exit_code() == 0


def test_missing_snapshot_reports_failure(tmp_path, lbv):
    tickers = ("AAPL", "MSFT")
    root = _build_root(tmp_path, tickers)
    _write_day(root, "2026-01-01", tickers)
    # 2026-01-02 deliberately omitted
    for t in tickers:
        _write_history(root, t, ["2026-01-01", "2026-01-02"])

    report, _, _, _ = lbv.run_validation(
        root, start=date(2026, 1, 1), end=date(2026, 1, 2),
        sample_tickers=list(tickers),
    )
    fail_checks = {f.check for f in report.failures}
    assert "snapshot_exists" in fail_checks
    assert report.exit_code() == 2


def test_nan_score_reports_failure(tmp_path, lbv):
    tickers = ("AAPL", "MSFT")
    root = _build_root(tmp_path, tickers)
    _write_day(root, "2026-01-01", tickers, nan_for=("AAPL",))
    for t in tickers:
        _write_history(root, t, ["2026-01-01"])

    report, _, _, _ = lbv.run_validation(
        root, start=date(2026, 1, 1), end=date(2026, 1, 1),
        sample_tickers=list(tickers),
    )
    sanity = [f for f in report.failures if f.check == "score_sanity"]
    assert sanity, "expected at least one score_sanity failure"
    assert any("AAPL" in f.message for f in sanity)
    assert report.exit_code() == 2


def test_band_inconsistency_detected(tmp_path, lbv):
    """Score 95 with band 'weakening' should be flagged."""
    tickers = ("AAPL",)
    root = _build_root(tmp_path, tickers)
    _write_day(
        root, "2026-01-01", tickers,
        composite_override=95.0,
        band_override="weakening",  # wrong — 95 belongs to "elite"
    )
    _write_history(root, "AAPL", ["2026-01-01"])

    report, _, _, _ = lbv.run_validation(
        root, start=date(2026, 1, 1), end=date(2026, 1, 1),
        sample_tickers=list(tickers),
    )
    band_fails = [f for f in report.failures if f.check == "band_consistency"]
    assert band_fails
    assert "elite" in band_fails[0].message
    assert report.exit_code() == 2


def test_variable_detail_rowcount_off_reports_warning(tmp_path, lbv):
    tickers = ("AAPL", "MSFT", "NVDA")  # expects 15 rows (3 * 5)
    root = _build_root(tmp_path, tickers)
    _write_day(root, "2026-01-01", tickers, vd_rows_override=10)  # short by 5
    for t in tickers:
        _write_history(root, t, ["2026-01-01"])

    report, _, _, _ = lbv.run_validation(
        root, start=date(2026, 1, 1), end=date(2026, 1, 1),
        sample_tickers=list(tickers),
    )
    rc = [f for f in report.warnings if f.check == "variable_detail_rowcount"]
    assert rc, "expected variable_detail_rowcount warning"
    assert report.exit_code() == 1


def test_history_missing_date_named_in_failure(tmp_path, lbv):
    tickers = ("AAPL", "MSFT")
    root = _build_root(tmp_path, tickers)
    for d in ("2026-01-01", "2026-01-02"):
        _write_day(root, d, tickers)
    # AAPL only has one of the two dates
    _write_history(root, "AAPL", ["2026-01-01"])
    _write_history(root, "MSFT", ["2026-01-01", "2026-01-02"])

    report, _, _, _ = lbv.run_validation(
        root, start=date(2026, 1, 1), end=date(2026, 1, 2),
        sample_tickers=["AAPL", "MSFT"],
    )
    hist_fails = [f for f in report.failures if f.check == "history_continuity"]
    assert hist_fails
    assert any("AAPL" in f.message for f in hist_fails)
    assert report.exit_code() == 2


def test_explicit_as_of_mode_marks_date_as_backfilled(tmp_path, lbv):
    tickers = ("AAPL",)
    root = _build_root(tmp_path, tickers)
    _write_day(root, "2026-01-01", tickers, as_of_mode="backfill")
    _write_history(root, "AAPL", ["2026-01-01"])

    _, per_date, _, _ = lbv.run_validation(
        root, start=date(2026, 1, 1), end=date(2026, 1, 1),
        sample_tickers=list(tickers),
    )
    assert per_date[0]["is_backfilled"] is True


def test_heuristic_marks_date_as_backfilled_when_article_count_missing(tmp_path, lbv):
    """No explicit marker but Thesis lacks article_count -> backfill heuristic kicks in."""
    tickers = ("AAPL",)
    root = _build_root(tmp_path, tickers)
    _write_day(root, "2026-01-01", tickers, include_article_count=False)
    _write_history(root, "AAPL", ["2026-01-01"])

    _, per_date, _, _ = lbv.run_validation(
        root, start=date(2026, 1, 1), end=date(2026, 1, 1),
        sample_tickers=list(tickers),
    )
    assert per_date[0]["is_backfilled"] is True


def test_thesis_renorm_check_only_applies_to_backfilled_dates(tmp_path, lbv):
    """A real-time-run date with no article_count? Actually that *would* trip
    the heuristic. So we set as_of_mode='realtime' explicitly to keep this
    date as real-time, and check that thesis_renormalization does NOT fire."""
    tickers = ("AAPL",)
    root = _build_root(tmp_path, tickers)
    # Real-time date with article_count present
    _write_day(root, "2026-01-01", tickers, include_article_count=True)
    _write_history(root, "AAPL", ["2026-01-01"])

    report, per_date, _, _ = lbv.run_validation(
        root, start=date(2026, 1, 1), end=date(2026, 1, 1),
        sample_tickers=list(tickers),
    )
    assert per_date[0]["is_backfilled"] is False
    # No thesis_renormalization findings should appear for a real-time date.
    assert not [f for f in report.findings if f.check == "thesis_renormalization"]


def test_json_report_written_to_disk(tmp_path, lbv):
    tickers = ("AAPL",)
    root = _build_root(tmp_path, tickers)
    _write_day(root, "2026-01-01", tickers)
    _write_history(root, "AAPL", ["2026-01-01"])

    report, per_date, hist, sample = lbv.run_validation(
        root, start=date(2026, 1, 1), end=date(2026, 1, 1),
        sample_tickers=list(tickers),
    )
    out = tmp_path / "report.json"
    lbv.write_json_report(report, per_date, hist, sample, out)
    payload = json.loads(out.read_text())
    assert payload["schema"] == "lthcs_backfill_validation/v1"
    assert payload["summary"]["exit_code"] == 0
    assert payload["dates_checked"] == ["2026-01-01"]


def test_repair_suggestions_only_for_failed_dates(tmp_path, lbv):
    tickers = ("AAPL",)
    root = _build_root(tmp_path, tickers)
    _write_day(root, "2026-01-01", tickers)  # good
    # 2026-01-02 missing
    _write_history(root, "AAPL", ["2026-01-01", "2026-01-02"])

    report, _, _, _ = lbv.run_validation(
        root, start=date(2026, 1, 1), end=date(2026, 1, 2),
        sample_tickers=list(tickers),
    )
    suggestions = lbv.render_repair_suggestions(report)
    assert any("2026-01-02" in s for s in suggestions)
    assert not any("2026-01-01" in s for s in suggestions)
