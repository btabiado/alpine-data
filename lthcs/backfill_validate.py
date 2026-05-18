"""LTHCS backfill validation runner (module form).

Audits the completeness and consistency of a backfill run. READ-ONLY:
this module never writes to ``data/lthcs/`` source data, never touches
source modules, the pipeline, the app, or CI.

This is the importable module form of the ``scripts/lthcs_backfill_validate.py``
script.  The script itself remains a thin wrapper around :func:`main` so
that downstream tooling (dashboards, MCP servers, tests) can import the
validator without going through ``importlib.util.spec_from_file_location``.

Exit codes
----------
    0 — all checks pass (no warnings, no failures)
    1 — at least one warning, no failures
    2 — at least one hard failure (missing snapshot, NaN scores, etc.)

Design notes
------------
* Pure stdlib (``json``, ``pathlib``, ``datetime``, ``argparse``,
  ``random``, ``math``). No new deps.
* Each check accumulates ``Finding`` records into a single ``Report``;
  rendering and exit-code determination both flow from that list, so
  tests can build a ``Report`` directly and assert on findings without
  re-parsing stdout.
* Backfilled vs real-time-run dates are identified by an explicit
  ``as_of_mode == "backfill"`` field on the snapshot (preferred), with a
  documented heuristic fallback (Thesis pillar has no ``article_count``
  component) for snapshots that pre-date the field.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, List, Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PILLAR_NAMES = (
    "adoption_momentum",
    "institutional_confidence",
    "financial_evolution",
    "thesis_integrity",
    "des",
)

SEVERITY_OK = "ok"
SEVERITY_WARN = "warn"
SEVERITY_FAIL = "fail"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    """A single observation produced by one validation check.

    ``severity`` is one of ``ok`` / ``warn`` / ``fail``. ``ok`` findings
    are kept so the rendered report still says "OK 137 / 137 ...".
    """

    check: str
    severity: str
    message: str
    detail: dict = field(default_factory=dict)


@dataclass
class Report:
    start: str
    end: str
    data_root: str
    active_universe_size: int
    findings: List[Finding] = field(default_factory=list)
    dates_checked: List[str] = field(default_factory=list)

    def add(self, f: Finding) -> None:
        self.findings.append(f)

    @property
    def warnings(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == SEVERITY_WARN]

    @property
    def failures(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == SEVERITY_FAIL]

    def exit_code(self) -> int:
        if self.failures:
            return 2
        if self.warnings:
            return 1
        return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _daterange(start: date, end: date) -> Iterable[date]:
    """Inclusive ``[start, end]``."""
    n = (end - start).days
    for i in range(n + 1):
        yield start + timedelta(days=i)


def _is_finite_number(x: Any) -> bool:
    if x is None:
        return False
    if isinstance(x, bool):
        # bool is a subclass of int in Python — explicitly reject it
        # so True/False don't masquerade as valid scores.
        return False
    if not isinstance(x, (int, float)):
        return False
    return math.isfinite(float(x))


def _band_for_score(score: float, score_bands: dict) -> Optional[str]:
    """Return the band whose ``[min, max]`` range contains ``score``."""
    for name, spec in score_bands.items():
        lo = spec.get("min")
        hi = spec.get("max")
        if lo is None or hi is None:
            continue
        if lo <= score <= hi:
            return name
    return None


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _load_active_universe(data_root: Path) -> List[str]:
    universe_path = data_root / "universe.json"
    if not universe_path.exists():
        return []
    data = _load_json(universe_path)
    tickers = data.get("tickers", []) if isinstance(data, dict) else []
    return [t["ticker"] for t in tickers if t.get("active", True)]


def _load_score_bands(data_root: Path) -> dict:
    weights_path = data_root / "weights.json"
    if not weights_path.exists():
        return {}
    return _load_json(weights_path).get("score_bands", {})


# ---------------------------------------------------------------------------
# Backfill-mode detection
# ---------------------------------------------------------------------------

def is_backfilled_snapshot(snapshot: dict, variable_detail: Optional[dict] = None) -> bool:
    """Return True if a snapshot was produced by the backfill orchestrator.

    Preferred signal: explicit ``snapshot["as_of_mode"] == "backfill"``
    (or the same field on a ``metadata`` sub-dict). This is what the
    backfill orchestrator agent SHOULD set on every snapshot it writes;
    if it doesn't, see the report for a flagged coordination note.

    Heuristic fallback (only if the field is absent): inspect the
    Thesis pillar variable_detail. Real-time runs include
    ``components.article_count`` (an Alpha Vantage NEWS_SENTIMENT
    metric); backfilled runs cannot reach historical sentiment so that
    key is missing. The heuristic is best-effort.
    """
    if not isinstance(snapshot, dict):
        return False
    if snapshot.get("as_of_mode") == "backfill":
        return True
    meta = snapshot.get("metadata") if isinstance(snapshot.get("metadata"), dict) else None
    if meta and meta.get("as_of_mode") == "backfill":
        return True

    # Heuristic fallback via variable_detail
    if not variable_detail or not isinstance(variable_detail, dict):
        return False
    for row in variable_detail.get("variables", []) or []:
        if row.get("pillar") == "thesis_integrity":
            comps = row.get("components") or {}
            # If ANY ticker's thesis has article_count, treat as real-time.
            if "article_count" in comps:
                return False
    # If we walked all thesis rows and none had article_count, looks like backfill.
    return True


# ---------------------------------------------------------------------------
# Per-date checks
# ---------------------------------------------------------------------------

def _check_one_date(
    d: date,
    data_root: Path,
    active_universe: set,
    score_bands: dict,
    report: Report,
    history_sample: List[str],
) -> dict:
    """Run all per-date checks for a single date.

    Returns a small dict of per-date stats used by aggregate findings.
    """
    date_str = d.isoformat()
    stats = {
        "date": date_str,
        "snapshot_exists": False,
        "ticker_count": 0,
        "nan_scores": 0,
        "out_of_range_scores": 0,
        "band_mismatches": 0,
        "variable_detail_exists": False,
        "variable_detail_rows": 0,
        "narratives_exists": False,
        "narratives_rows": 0,
        "is_backfilled": False,
        "thesis_renorm_issues": 0,
    }

    snap_path = data_root / "snapshots" / f"{date_str}.json"
    vd_path = data_root / "variable_detail" / f"{date_str}.json"
    narr_path = data_root / "narratives" / f"{date_str}.json"

    # 1. Snapshot exists
    if not snap_path.exists():
        report.add(Finding(
            check="snapshot_exists",
            severity=SEVERITY_FAIL,
            message=f"missing snapshot for {date_str}",
            detail={"date": date_str, "path": str(snap_path)},
        ))
        return stats
    stats["snapshot_exists"] = True

    try:
        snapshot = _load_json(snap_path)
    except (json.JSONDecodeError, OSError) as e:
        report.add(Finding(
            check="snapshot_exists",
            severity=SEVERITY_FAIL,
            message=f"snapshot for {date_str} is unreadable: {e}",
            detail={"date": date_str, "path": str(snap_path)},
        ))
        return stats

    scores_rows = snapshot.get("scores", []) if isinstance(snapshot, dict) else []
    stats["ticker_count"] = len(scores_rows)

    # 2 + 3. Score sanity + ticker coverage
    seen_tickers: set = set()
    for row in scores_rows:
        ticker = row.get("ticker")
        if ticker:
            seen_tickers.add(ticker)
        score = row.get("lthcs_score")
        if not _is_finite_number(score):
            stats["nan_scores"] += 1
            report.add(Finding(
                check="score_sanity",
                severity=SEVERITY_FAIL,
                message=f"{date_str} {ticker}: composite_score is NaN/null/non-numeric ({score!r})",
                detail={"date": date_str, "ticker": ticker, "value": repr(score)},
            ))
            continue
        if not (0 <= float(score) <= 100):
            stats["out_of_range_scores"] += 1
            report.add(Finding(
                check="score_sanity",
                severity=SEVERITY_FAIL,
                message=f"{date_str} {ticker}: composite_score {score} is outside [0, 100]",
                detail={"date": date_str, "ticker": ticker, "value": score},
            ))
            continue
        # 4. Band consistency
        expected_band = _band_for_score(float(score), score_bands)
        actual_band = row.get("band")
        if expected_band and actual_band and expected_band != actual_band:
            stats["band_mismatches"] += 1
            report.add(Finding(
                check="band_consistency",
                severity=SEVERITY_FAIL,
                message=(
                    f"{date_str} {ticker}: band={actual_band!r} but score {score} "
                    f"belongs to band {expected_band!r}"
                ),
                detail={
                    "date": date_str,
                    "ticker": ticker,
                    "score": score,
                    "actual_band": actual_band,
                    "expected_band": expected_band,
                },
            ))

    # Ticker coverage vs universe — warn if under-covered.
    missing_from_universe = active_universe - seen_tickers
    if active_universe and len(missing_from_universe) > 1:
        # We tolerate 1 missing (e.g. a freshly delisted name) without warning.
        report.add(Finding(
            check="ticker_coverage",
            severity=SEVERITY_WARN,
            message=(
                f"{date_str}: {len(scores_rows)} scored, "
                f"{len(missing_from_universe)} active-universe tickers absent"
            ),
            detail={
                "date": date_str,
                "scored": len(scores_rows),
                "missing": sorted(missing_from_universe)[:20],
            },
        ))

    # 5. Variable-detail completeness
    if not vd_path.exists():
        report.add(Finding(
            check="variable_detail_exists",
            severity=SEVERITY_FAIL,
            message=f"missing variable_detail for {date_str}",
            detail={"date": date_str, "path": str(vd_path)},
        ))
        variable_detail = None
    else:
        try:
            variable_detail = _load_json(vd_path)
            stats["variable_detail_exists"] = True
            vrows = variable_detail.get("variables", []) if isinstance(variable_detail, dict) else []
            stats["variable_detail_rows"] = len(vrows)
            expected = len(scores_rows) * len(PILLAR_NAMES)
            if expected and stats["variable_detail_rows"] != expected:
                report.add(Finding(
                    check="variable_detail_rowcount",
                    severity=SEVERITY_WARN,
                    message=(
                        f"{date_str}: variable_detail has {stats['variable_detail_rows']} rows, "
                        f"expected {expected} (={len(scores_rows)} tickers x {len(PILLAR_NAMES)} pillars)"
                    ),
                    detail={
                        "date": date_str,
                        "actual": stats["variable_detail_rows"],
                        "expected": expected,
                    },
                ))
        except (json.JSONDecodeError, OSError) as e:
            variable_detail = None
            report.add(Finding(
                check="variable_detail_exists",
                severity=SEVERITY_FAIL,
                message=f"variable_detail for {date_str} unreadable: {e}",
                detail={"date": date_str},
            ))

    # 6. Narratives completeness
    if not narr_path.exists():
        report.add(Finding(
            check="narratives_exists",
            severity=SEVERITY_FAIL,
            message=f"missing narratives for {date_str}",
            detail={"date": date_str, "path": str(narr_path)},
        ))
    else:
        try:
            narratives = _load_json(narr_path)
            stats["narratives_exists"] = True
            nrows = narratives.get("narratives", []) if isinstance(narratives, dict) else []
            stats["narratives_rows"] = len(nrows)
            if scores_rows and stats["narratives_rows"] != len(scores_rows):
                report.add(Finding(
                    check="narratives_rowcount",
                    severity=SEVERITY_WARN,
                    message=(
                        f"{date_str}: {stats['narratives_rows']} narratives vs "
                        f"{len(scores_rows)} scores"
                    ),
                    detail={
                        "date": date_str,
                        "narratives": stats["narratives_rows"],
                        "scores": len(scores_rows),
                    },
                ))
        except (json.JSONDecodeError, OSError) as e:
            report.add(Finding(
                check="narratives_exists",
                severity=SEVERITY_FAIL,
                message=f"narratives for {date_str} unreadable: {e}",
                detail={"date": date_str},
            ))

    # 8. Skipped-sources audit (backfill mode)
    backfilled = is_backfilled_snapshot(snapshot, variable_detail)
    stats["is_backfilled"] = backfilled
    if backfilled and variable_detail:
        for row in variable_detail.get("variables", []) or []:
            if row.get("pillar") != "thesis_integrity":
                continue
            sub = row.get("sub_score")
            if not _is_finite_number(sub) or not (0 <= float(sub) <= 100):
                stats["thesis_renorm_issues"] += 1
                report.add(Finding(
                    check="thesis_renormalization",
                    severity=SEVERITY_FAIL,
                    message=(
                        f"{date_str} {row.get('ticker')}: Thesis sub_score {sub!r} "
                        f"is not a finite number in [0, 100] under backfill mode"
                    ),
                    detail={
                        "date": date_str,
                        "ticker": row.get("ticker"),
                        "sub_score": sub,
                    },
                ))

    return stats


# ---------------------------------------------------------------------------
# History continuity (sampled tickers)
# ---------------------------------------------------------------------------

def _check_history_continuity(
    data_root: Path,
    sample_tickers: List[str],
    expected_dates: List[str],
    report: Report,
) -> dict:
    out: dict = {}
    hist_root = data_root / "history" / "by_ticker"
    expected_set = set(expected_dates)

    for ticker in sample_tickers:
        path = hist_root / f"{ticker}.json"
        info = {"ticker": ticker, "found": 0, "expected": len(expected_dates), "missing": []}
        out[ticker] = info
        if not path.exists():
            report.add(Finding(
                check="history_continuity",
                severity=SEVERITY_FAIL,
                message=f"history file missing for {ticker} at {path}",
                detail={"ticker": ticker, "path": str(path)},
            ))
            continue
        try:
            data = _load_json(path)
        except (json.JSONDecodeError, OSError) as e:
            report.add(Finding(
                check="history_continuity",
                severity=SEVERITY_FAIL,
                message=f"history file for {ticker} unreadable: {e}",
                detail={"ticker": ticker, "path": str(path)},
            ))
            continue

        rows = data.get("history", []) if isinstance(data, dict) else []
        seen_dates: List[str] = []
        for row in rows:
            d = row.get("date")
            if d in expected_set:
                seen_dates.append(d)
        # Detect duplicates within the expected range.
        dupes = [d for d in set(seen_dates) if seen_dates.count(d) > 1]
        info["found"] = len(set(seen_dates))
        info["missing"] = sorted(expected_set - set(seen_dates))

        if dupes:
            report.add(Finding(
                check="history_continuity",
                severity=SEVERITY_FAIL,
                message=f"history for {ticker} has duplicate entries on {dupes[:5]}",
                detail={"ticker": ticker, "duplicates": dupes},
            ))
        if info["missing"]:
            report.add(Finding(
                check="history_continuity",
                severity=SEVERITY_FAIL,
                message=(
                    f"history for {ticker} missing {len(info['missing'])} of "
                    f"{len(expected_dates)} dates (first: {info['missing'][:3]})"
                ),
                detail={
                    "ticker": ticker,
                    "missing_count": len(info["missing"]),
                    "missing_sample": info["missing"][:10],
                },
            ))
    return out


# ---------------------------------------------------------------------------
# Aggregate report rendering
# ---------------------------------------------------------------------------

def _summarize_block(
    report: Report,
    check_name: str,
    ok_message: str,
    warn_label: Optional[str] = None,
    fail_label: Optional[str] = None,
) -> List[str]:
    """Render one section. Returns a list of lines."""
    lines = []
    block_findings = [f for f in report.findings if f.check == check_name]
    fails = [f for f in block_findings if f.severity == SEVERITY_FAIL]
    warns = [f for f in block_findings if f.severity == SEVERITY_WARN]
    if not fails and not warns:
        lines.append(f"  [OK] {ok_message}")
    else:
        if fails:
            lines.append(f"  [FAIL] {fail_label or check_name}: {len(fails)} failure(s)")
            for f in fails[:5]:
                lines.append(f"        - {f.message}")
            if len(fails) > 5:
                lines.append(f"        ... and {len(fails) - 5} more")
        if warns:
            lines.append(f"  [WARN] {warn_label or check_name}: {len(warns)} warning(s)")
            for f in warns[:5]:
                lines.append(f"        - {f.message}")
            if len(warns) > 5:
                lines.append(f"        ... and {len(warns) - 5} more")
    return lines


def render_report(report: Report, per_date_stats: List[dict], history_stats: dict, sample_tickers: List[str]) -> str:
    lines: List[str] = []
    lines.append("LTHCS Backfill Validation Report")
    lines.append("=" * 36)
    n = len(report.dates_checked)
    lines.append(f"Range: {report.start} -> {report.end} ({n} day{'s' if n != 1 else ''})")
    lines.append(f"Active universe: {report.active_universe_size} tickers")
    lines.append("")

    # Snapshot exist
    lines.append("Snapshot files")
    present = sum(1 for s in per_date_stats if s["snapshot_exists"])
    lines.append(f"  Present: {present} / {n} dates")
    lines.extend(_summarize_block(report, "snapshot_exists", "all snapshot files present"))
    lines.append("")

    # Ticker coverage
    counts = [s["ticker_count"] for s in per_date_stats if s["snapshot_exists"]]
    lines.append("Ticker coverage per date")
    if counts:
        mean = sum(counts) / len(counts)
        lines.append(f"  Mean: {mean:.1f} tickers/date (range {min(counts)}-{max(counts)})")
    lines.extend(_summarize_block(report, "ticker_coverage", "coverage within tolerance"))
    lines.append("")

    # Score sanity
    nan_total = sum(s["nan_scores"] for s in per_date_stats)
    oor_total = sum(s["out_of_range_scores"] for s in per_date_stats)
    ticker_days = sum(s["ticker_count"] for s in per_date_stats)
    lines.append("Score sanity")
    lines.append(f"  Ticker-days inspected: {ticker_days}")
    lines.append(f"  NaN/null scores: {nan_total}")
    lines.append(f"  Out-of-range [0, 100]: {oor_total}")
    lines.extend(_summarize_block(report, "score_sanity", "all composite_score values valid"))
    lines.append("")

    # Band
    lines.append("Band consistency")
    lines.extend(_summarize_block(report, "band_consistency", "all band assignments match score_bands"))
    lines.append("")

    # Variable detail
    vd_present = sum(1 for s in per_date_stats if s["variable_detail_exists"])
    vd_rows = [s["variable_detail_rows"] for s in per_date_stats if s["variable_detail_exists"]]
    lines.append("Variable-detail completeness")
    lines.append(f"  Present: {vd_present} / {n} dates")
    if vd_rows:
        mean = sum(vd_rows) / len(vd_rows)
        lines.append(f"  Mean rows/date: {mean:.1f}")
    lines.extend(_summarize_block(report, "variable_detail_exists", "all variable_detail files present"))
    lines.extend(_summarize_block(report, "variable_detail_rowcount", "row counts match ticker count x 5 pillars"))
    lines.append("")

    # Narratives
    narr_present = sum(1 for s in per_date_stats if s["narratives_exists"])
    lines.append("Narrative completeness")
    lines.append(f"  Present: {narr_present} / {n} dates")
    lines.extend(_summarize_block(report, "narratives_exists", "all narrative files present"))
    lines.extend(_summarize_block(report, "narratives_rowcount", "row counts match scores"))
    lines.append("")

    # History
    lines.append(f"History continuity ({len(sample_tickers)} sampled ticker{'s' if len(sample_tickers) != 1 else ''})")
    for t in sample_tickers:
        info = history_stats.get(t, {})
        found = info.get("found", 0)
        expected = info.get("expected", 0)
        missing = info.get("missing", [])
        marker = "OK" if not missing and found == expected else "FAIL"
        lines.append(f"  [{marker}] {t}: {found} / {expected} dates")
    lines.append("")

    # Skipped-sources audit (backfill mode)
    backfilled_dates = [s["date"] for s in per_date_stats if s["is_backfilled"]]
    lines.append("Skipped-sources audit (backfill mode)")
    lines.append(f"  Backfilled dates identified: {len(backfilled_dates)} / {n}")
    lines.extend(_summarize_block(
        report,
        "thesis_renormalization",
        "all backfilled Thesis sub_scores valid in [0, 100]",
    ))
    lines.append("")

    # Overall
    if report.failures:
        verdict = f"FAIL - {len(report.failures)} failure(s), {len(report.warnings)} warning(s)"
    elif report.warnings:
        verdict = f"WARN - {len(report.warnings)} warning(s), no failures"
    else:
        verdict = f"PASS - {n}/{n} dates pass all checks"
    lines.append(f"Overall: {verdict}")
    return "\n".join(lines)


def render_repair_suggestions(report: Report) -> List[str]:
    """Build repair-command suggestions for problem dates."""
    bad_dates: set = set()
    for f in report.failures:
        d = f.detail.get("date")
        if d:
            bad_dates.add(d)
    out = []
    for d in sorted(bad_dates):
        out.append(
            f"  python scripts/lthcs_backfill.py --start {d} --end {d} --force"
        )
    return out


# ---------------------------------------------------------------------------
# Public API: run_validation
# ---------------------------------------------------------------------------

def run_validation(
    data_root: Path,
    start: Optional[date] = None,
    end: Optional[date] = None,
    sample_tickers: Optional[List[str]] = None,
    rng_seed: int = 0,
):
    """Run the full validation suite.

    Returns ``(report, per_date_stats, history_stats, sample_tickers)``.
    Tests use this directly; the CLI wraps it.
    """
    data_root = Path(data_root)
    snap_dir = data_root / "snapshots"
    available_dates: List[str] = []
    if snap_dir.is_dir():
        for p in snap_dir.glob("*.json"):
            if p.stem == "index":
                continue
            available_dates.append(p.stem)
    available_dates.sort()

    if start is None:
        start = _parse_date(available_dates[0]) if available_dates else date.today()
    if end is None:
        end = _parse_date(available_dates[-1]) if available_dates else date.today()
    if end < start:
        start, end = end, start

    universe = _load_active_universe(data_root)
    score_bands = _load_score_bands(data_root)
    active_set = set(universe)

    dates = [d.isoformat() for d in _daterange(start, end)]
    report = Report(
        start=start.isoformat(),
        end=end.isoformat(),
        data_root=str(data_root),
        active_universe_size=len(universe),
        dates_checked=dates,
    )

    # Pick 5 sample tickers for history continuity.
    if sample_tickers is None:
        rng = random.Random(rng_seed)
        candidate_universe = universe or ["AAPL", "MSFT", "NVDA", "JPM", "XOM"]
        sample_tickers = rng.sample(candidate_universe, k=min(5, len(candidate_universe)))

    per_date_stats: List[dict] = []
    for d in _daterange(start, end):
        stats = _check_one_date(d, data_root, active_set, score_bands, report, sample_tickers)
        per_date_stats.append(stats)

    history_stats = _check_history_continuity(data_root, sample_tickers, dates, report)

    return report, per_date_stats, history_stats, sample_tickers


def write_json_report(
    report: Report,
    per_date_stats: List[dict],
    history_stats: dict,
    sample_tickers: List[str],
    out_path: Path,
) -> None:
    payload = {
        "schema": "lthcs_backfill_validation/v1",
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "range": {"start": report.start, "end": report.end},
        "data_root": report.data_root,
        "active_universe_size": report.active_universe_size,
        "sample_tickers": sample_tickers,
        "dates_checked": report.dates_checked,
        "per_date_stats": per_date_stats,
        "history_stats": history_stats,
        "findings": [asdict(f) for f in report.findings],
        "summary": {
            "warnings": len(report.warnings),
            "failures": len(report.failures),
            "exit_code": report.exit_code(),
        },
    }
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Validate an LTHCS backfill run.")
    p.add_argument("--start", type=str, default=None, help="Start date YYYY-MM-DD (inclusive)")
    p.add_argument("--end", type=str, default=None, help="End date YYYY-MM-DD (inclusive)")
    p.add_argument("--data-root", type=str, default="data/lthcs", help="LTHCS data root")
    p.add_argument("--repair", action="store_true", help="Print repair suggestions for problem dates")
    p.add_argument("--verbose", action="store_true", help="Verbose output (currently a no-op; reserved)")
    p.add_argument("--no-json", action="store_true", help="Don't write the JSON report file")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    start = _parse_date(args.start) if args.start else None
    end = _parse_date(args.end) if args.end else None
    data_root = Path(args.data_root)

    report, per_date_stats, history_stats, sample_tickers = run_validation(
        data_root=data_root, start=start, end=end,
    )

    text = render_report(report, per_date_stats, history_stats, sample_tickers)
    print(text)

    if args.repair and report.failures:
        print("")
        print("Repair suggestions (not auto-executed):")
        for line in render_repair_suggestions(report):
            print(line)

    # Write JSON report next to the data root (skippable for tests).
    if not args.no_json:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = data_root / f"backfill_validation_{ts}.json"
        try:
            write_json_report(report, per_date_stats, history_stats, sample_tickers, out_path)
            print(f"\nJSON report: {out_path}")
        except OSError as e:
            print(f"\n(JSON report could not be written: {e})", file=sys.stderr)

    return report.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
