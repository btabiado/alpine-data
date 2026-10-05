"""Tests for ``lthcs.backfill_validate``.

Each test builds a tiny synthetic LTHCS data tree in ``tmp_path`` and
calls ``run_validation`` directly. The CLI (``scripts/lthcs_backfill_validate.py``,
a thin wrapper around this module) is exercised separately.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from lthcs import backfill_validate as _backfill_validate


# ---------------------------------------------------------------------------
# Provide the existing ``lbv`` fixture so individual tests don't need
# rewriting; it now just exposes the proper module import.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def lbv():
    return _backfill_validate


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


# ---------------------------------------------------------------------------
# Universe growth, unscored-ticker diagnostics, known_gaps disclosures
# ---------------------------------------------------------------------------

def test_ticker_added_mid_range_is_not_absent_or_missing_history(tmp_path, lbv):
    """A ticker that joined on day 2 is neither 'absent' on day 1 nor
    'missing history' for day 1 (the universe grew; nothing was lost)."""
    root = _build_root(tmp_path, ("AAPL", "MSFT", "NVDA"))
    _write_day(root, "2026-01-01", ("AAPL",))
    _write_day(root, "2026-01-02", ("AAPL", "MSFT", "NVDA"))
    _write_history(root, "AAPL", ["2026-01-01", "2026-01-02"])
    _write_history(root, "MSFT", ["2026-01-02"])
    _write_history(root, "NVDA", ["2026-01-02"])

    report, _, hist, _ = lbv.run_validation(
        root, start=date(2026, 1, 1), end=date(2026, 1, 2),
        sample_tickers=["AAPL", "MSFT", "NVDA"],
    )
    assert report.exit_code() == 0, [f.message for f in report.findings]
    assert hist["MSFT"]["expected"] == 1


def test_ticker_dropped_after_joining_still_warns(tmp_path, lbv):
    """Two tickers scored on day 1 and gone on day 2 are a real coverage hole."""
    root = _build_root(tmp_path, ("AAPL", "MSFT", "NVDA"))
    _write_day(root, "2026-01-01", ("AAPL", "MSFT", "NVDA"))
    _write_day(root, "2026-01-02", ("AAPL",))
    for t in ("AAPL", "MSFT", "NVDA"):
        _write_history(root, t, ["2026-01-01", "2026-01-02"])
    report, _, _, _ = lbv.run_validation(
        root, start=date(2026, 1, 1), end=date(2026, 1, 2), sample_tickers=["AAPL"],
    )
    cov = [f for f in report.warnings if f.check == "ticker_coverage"]
    assert len(cov) == 1 and "2026-01-02" in cov[0].message


def test_never_scored_active_tickers_still_warn(tmp_path, lbv):
    root = _build_root(tmp_path, ("AAPL", "MSFT", "NVDA"))
    _write_day(root, "2026-01-01", ("AAPL",))
    _write_history(root, "AAPL", ["2026-01-01"])
    report, _, _, _ = lbv.run_validation(
        root, start=date(2026, 1, 1), end=date(2026, 1, 1), sample_tickers=["AAPL"],
    )
    assert any(f.check == "ticker_coverage" for f in report.warnings)


def test_variable_detail_rows_for_unscored_tickers_are_not_counted(tmp_path, lbv):
    """variable_detail may carry rows for tickers the quality gate dropped."""
    root = _build_root(tmp_path, ("AAPL", "MSFT"))
    _write_day(root, "2026-01-01", ("AAPL", "MSFT"))
    vd_path = root / "variable_detail" / "2026-01-01.json"
    vd = json.loads(vd_path.read_text())
    vd["variables"] += [{"ticker": "ZZZZ", "pillar": p, "components": {}, "sub_score": 50.0}
                        for p in PILLARS]
    vd_path.write_text(json.dumps(vd))
    for t in ("AAPL", "MSFT"):
        _write_history(root, t, ["2026-01-01"])
    report, per_date, _, _ = lbv.run_validation(
        root, start=date(2026, 1, 1), end=date(2026, 1, 1), sample_tickers=["AAPL"],
    )
    assert not [f for f in report.warnings if f.check == "variable_detail_rowcount"]
    assert per_date[0]["variable_detail_rows"] == 10


def _known_gaps(tmp_path, **gap):
    entry = {"feed": "data/lthcs/", "history": "daily index",
             "start": "2026-01-02", "end": "2026-01-02",
             "reason": "lthcs-daily did not run that day; nothing to restore",
             "backfill_attempted": ["git"], "fillable": True}
    entry.update(gap)
    p = tmp_path / "known_gaps.json"
    p.write_text(json.dumps({"gaps": [entry]}))
    return p


def test_missing_snapshot_disclosed_in_known_gaps_does_not_fail(tmp_path, lbv):
    tickers = ("AAPL", "MSFT")
    root = _build_root(tmp_path, tickers)
    _write_day(root, "2026-01-01", tickers)
    _write_day(root, "2026-01-03", tickers)
    for t in tickers:
        _write_history(root, t, ["2026-01-01", "2026-01-02", "2026-01-03"])
    report, _, _, _ = lbv.run_validation(
        root, start=date(2026, 1, 1), end=date(2026, 1, 3),
        sample_tickers=list(tickers), known_gaps_path=_known_gaps(tmp_path),
    )
    assert report.exit_code() == 0, [f.message for f in report.findings]
    assert [f.detail["date"] for f in report.disclosed] == ["2026-01-02"]
    text = lbv.render_report(report, [], {}, [])
    assert "[DISCLOSED] 1 known gap(s)" in text


def test_known_gap_for_another_history_does_not_mute(tmp_path, lbv):
    """The crypto snapshot disclosures share the feed but not the history."""
    tickers = ("AAPL",)
    root = _build_root(tmp_path, tickers)
    _write_day(root, "2026-01-01", tickers)
    _write_history(root, "AAPL", ["2026-01-01", "2026-01-02"])
    report, _, _, _ = lbv.run_validation(
        root, start=date(2026, 1, 1), end=date(2026, 1, 2), sample_tickers=["AAPL"],
        known_gaps_path=_known_gaps(tmp_path, history="daily crypto snapshots"),
    )
    assert any(f.check == "snapshot_exists" for f in report.failures)


def test_repo_known_gaps_not_applied_to_a_synthetic_root(tmp_path, lbv, monkeypatch):
    """Without an explicit path, only the repo's own data/lthcs reads the
    repo's known_gaps.json; a synthetic tree is judged on its own."""
    gaps = _known_gaps(tmp_path)
    monkeypatch.setattr(lbv, "KNOWN_GAPS_PATH", gaps)
    tickers = ("AAPL",)
    root = _build_root(tmp_path, tickers)
    _write_day(root, "2026-01-01", tickers)
    _write_history(root, "AAPL", ["2026-01-01", "2026-01-02"])
    report, _, _, _ = lbv.run_validation(
        root, start=date(2026, 1, 1), end=date(2026, 1, 2), sample_tickers=["AAPL"],
    )
    assert any(f.check == "snapshot_exists" for f in report.failures)


# ---------------------------------------------------------------------------
# Universe add dates (added_on) and GitHub Actions rendering
# ---------------------------------------------------------------------------

def _set_added_on(root: Path, **added: str) -> None:
    p = root / "universe.json"
    u = json.loads(p.read_text())
    for e in u["tickers"]:
        if e["ticker"] in added:
            e["added_on"] = added[e["ticker"]]
    p.write_text(json.dumps(u))


def test_ticker_added_on_the_snapshot_day_is_not_expected_until_the_next(tmp_path, lbv):
    """2026-10-05: the snapshot was cut at 01:59Z and the S&P 500 sync added
    300 tickers at 02:08Z. A snapshot has no generation time, so neither the
    add day nor any earlier day may count them absent, and with no expected
    day they have no history to sample."""
    root = _build_root(tmp_path, ("AAPL", "MSFT", "NVDA"))
    _set_added_on(root, MSFT="2026-01-02", NVDA="2026-01-02")
    for d in ("2026-01-01", "2026-01-02"):
        _write_day(root, d, ("AAPL",))
    _write_history(root, "AAPL", ["2026-01-01", "2026-01-02"])

    report, _, hist, sample = lbv.run_validation(
        root, start=date(2026, 1, 1), end=date(2026, 1, 2),
    )
    assert report.exit_code() == 0, [f.message for f in report.findings]
    assert sample == ["AAPL"]
    # Sampled explicitly anyway, a not-yet-expected ticker needs no file.
    report, _, hist, _ = lbv.run_validation(
        root, start=date(2026, 1, 1), end=date(2026, 1, 2), sample_tickers=["MSFT"],
    )
    assert report.exit_code() == 0
    assert hist["MSFT"] == {"ticker": "MSFT", "found": 0, "expected": 0,
                            "missing": [], "expected_from": "2026-01-03"}


def test_added_ticker_still_unscored_the_next_day_warns_and_fails_history(tmp_path, lbv):
    root = _build_root(tmp_path, ("AAPL", "MSFT", "NVDA"))
    _set_added_on(root, MSFT="2026-01-01", NVDA="2026-01-01")
    for d in ("2026-01-01", "2026-01-02"):
        _write_day(root, d, ("AAPL",))
    _write_history(root, "AAPL", ["2026-01-01", "2026-01-02"])

    report, _, hist, _ = lbv.run_validation(
        root, start=date(2026, 1, 1), end=date(2026, 1, 2), sample_tickers=["MSFT"],
    )
    cov = [f for f in report.warnings if f.check == "ticker_coverage"]
    assert [f.detail["date"] for f in cov] == ["2026-01-02"]
    assert "MSFT, NVDA" in cov[0].message
    assert hist["MSFT"]["expected"] == 1
    assert any(f.check == "history_continuity" for f in report.failures)


def test_scored_before_its_add_date_counts_from_first_score(tmp_path, lbv):
    """The earlier evidence wins: a ticker scored before its recorded add
    date (backfilled, or re-added) is expected from its first score, so the
    add date cannot excuse a gap after that."""
    root = _build_root(tmp_path, ("AAPL", "MSFT", "NVDA"))
    _set_added_on(root, MSFT="2026-01-03", NVDA="2026-01-03")
    _write_day(root, "2026-01-01", ("AAPL", "MSFT", "NVDA"))
    _write_day(root, "2026-01-02", ("AAPL",))
    _write_day(root, "2026-01-03", ("AAPL", "MSFT", "NVDA"))
    for t in ("AAPL", "MSFT", "NVDA"):
        _write_history(root, t, ["2026-01-01", "2026-01-02", "2026-01-03"])
    report, _, _, _ = lbv.run_validation(
        root, start=date(2026, 1, 1), end=date(2026, 1, 3), sample_tickers=["AAPL"],
    )
    cov = [f for f in report.warnings if f.check == "ticker_coverage"]
    assert [f.detail["date"] for f in cov] == ["2026-01-02"]
    assert lbv.expected_from("MSFT", {"MSFT": "2026-01-01"}, {"MSFT": "2026-01-03"}) == "2026-01-01"


def test_unparseable_added_on_is_ignored_not_guessed(tmp_path, lbv):
    root = _build_root(tmp_path, ("AAPL", "MSFT", "NVDA"))
    _set_added_on(root, MSFT="late May 2026", NVDA="late May 2026")
    _write_day(root, "2026-01-01", ("AAPL",))
    _write_history(root, "AAPL", ["2026-01-01"])
    report, _, _, _ = lbv.run_validation(
        root, start=date(2026, 1, 1), end=date(2026, 1, 1), sample_tickers=["AAPL"],
    )
    assert any(f.check == "ticker_coverage" for f in report.warnings)


def _failing_report(lbv):
    report = lbv.Report(start="2026-01-01", end="2026-01-30", data_root="x",
                        active_universe_size=3)
    for i in range(30):
        report.add(lbv.Finding("ticker_coverage", lbv.SEVERITY_WARN,
                               f"2026-01-{i + 1:02d}: 1 scored, 300 active-universe tickers absent "
                               + "x" * 200))
    report.add(lbv.Finding("history_continuity", lbv.SEVERITY_FAIL,
                           "history file missing for SW at data/lthcs/history/by_ticker/SW.json"))
    report.add(lbv.Finding("snapshot_exists", lbv.SEVERITY_DISCLOSED, "missing (disclosed)",
                           {"date": "2026-01-05"}))
    return report


def test_github_annotations_one_error_per_failing_check_trimmed(lbv):
    lines = lbv.github_annotations(_failing_report(lbv))
    assert len(lines) == 2   # coverage + history; the disclosed gap is not a failure
    assert lines[0].startswith("::error title=lthcs-validate WARN%3A ticker_coverage::")
    assert lines[1].startswith("::error title=lthcs-validate FAIL%3A history_continuity::")
    for line in lines:
        message = line.split("::", 2)[2]
        assert "\n" not in line
        assert len(message) <= lbv.ANNOTATION_MAX_CHARS
    assert "30 warning(s)" in lines[0]
    assert "history file missing for SW" in lines[1]


def test_markdown_summary_has_a_row_per_check_and_the_findings(lbv):
    md = lbv.render_markdown_summary(_failing_report(lbv), {"SW": {"found": 0, "expected": 30}}, ["SW"])
    assert md.startswith("## LTHCS backfill validation: FAIL (exit 2)")
    for _, label in lbv.CHECK_LABELS:
        assert f"| {label} |" in md
    assert "❌ 1 failure(s)" in md and "⚠️ 30 warning(s)" in md
    assert "(1 disclosed in health/known_gaps.json)" in md
    assert "SW 0/30" in md
    assert "… and 10 more" in md


def test_cli_github_flag_annotates_and_writes_step_summary(tmp_path, lbv, monkeypatch, capsys):
    tickers = ("AAPL", "MSFT")
    root = _build_root(tmp_path, tickers)
    _write_day(root, "2026-01-01", tickers)
    _write_day(root, "2026-01-03", tickers)
    for t in tickers:
        _write_history(root, t, ["2026-01-01", "2026-01-02", "2026-01-03"])
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    rc = lbv.main(["--data-root", str(root), "--start", "2026-01-01", "--end", "2026-01-03",
                   "--no-json", "--github"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "::error title=lthcs-validate FAIL%3A snapshot_exists::" in out
    assert "missing snapshot for 2026-01-02" in summary.read_text()
