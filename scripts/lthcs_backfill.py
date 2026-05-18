#!/usr/bin/env python3
"""LTHCS historical backfill orchestrator.

Runs ``python lthcs_daily.py --as-of <date> --force --skip-thesis`` in a
loop across a window of past calendar dates, so the history files under
``data/lthcs/history/by_ticker/<TICKER>.json`` get real (not synthetic)
score rows for the requested range. Sister script to
``scripts/lthcs_trends_weekly.py`` — same calling pattern: stand-alone
CLI, never imported by the daily pipeline, designed to be triggered
manually or by the GitHub Action's ``workflow_dispatch``.

Usage::

    python scripts/lthcs_backfill.py                       # default 30 days
    python scripts/lthcs_backfill.py --days 7              # only the last week
    python scripts/lthcs_backfill.py --start 2026-04-01 --end 2026-04-30
    python scripts/lthcs_backfill.py --tickers AAPL,MSFT   # subset
    python scripts/lthcs_backfill.py --dry-run             # show plan, no work
    python scripts/lthcs_backfill.py --force               # re-run even if
                                                            # a snapshot exists

Semantics:
- Iterates ``[start, end]`` inclusive in calendar-day order (oldest first).
- Skips a date if ``data/lthcs/snapshots/<date>.json`` already exists,
  unless ``--force`` is given (in which case the daily pipeline is
  invoked with its own ``--force`` so it overwrites).
- A failed date (non-zero exit) is logged and the loop continues. Use
  the printed summary to retry just the failures.
- Rate-limit pause: if any SEC call returned 429 (or the daily pipeline
  exits with the rate-limit hint), pause 30 s before the next date.
- ``--skip-thesis`` is always forwarded because Alpha Vantage's free tier
  has no historical news archive (see lthcs_daily.py).

Output: a one-line summary per date and an aggregate report at the end::

    Backfill 5/30  2026-04-15  done in 47s
    Backfill 6/30  2026-04-16  SKIPPED (snapshot exists)
    Backfill 7/30  2026-04-17  FAILED  (exit 1, see log)
    ...
    Done. backfilled=24  skipped=4  failed=2  runtime=18m32s
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Repo-root self-locator so the script works whether invoked from cwd or via
# an absolute path. Mirrors the lthcs_daily.py pattern.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_DAYS = 30

# Pause this long if a date appears to have been rate-limited. Generous
# because SEC EDGAR's per-IP throttle resets in ~10 s but neighbour
# requests can still 429 for a while after.
_RATE_LIMIT_PAUSE_SECONDS = 30.0

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Heuristic strings that indicate a rate-limit happened anywhere downstream.
# We can't always tell from the exit code (the daily pipeline returns 1 for
# everything that isn't snapshot-collision), so we sniff the captured log.
_RATE_LIMIT_HINTS = ("429", "RateLimit", "rate limit", "Too Many Requests")

_DATA_ROOT = _REPO_ROOT / "data" / "lthcs"
_SNAPSHOTS_DIR = _DATA_ROOT / "snapshots"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lthcs_backfill",
        description=(
            "Run lthcs_daily.py --as-of <date> across a window of past dates "
            "to backfill the per-ticker history files. Stand-alone; not part "
            "of the daily cron."
        ),
    )
    p.add_argument(
        "--days",
        type=int,
        default=_DEFAULT_DAYS,
        help="How many calendar days back from --end (default: %d)." % _DEFAULT_DAYS,
    )
    p.add_argument(
        "--end",
        type=str,
        default=None,
        help="Last date to backfill, inclusive (YYYY-MM-DD). Defaults to yesterday.",
    )
    p.add_argument(
        "--start",
        type=str,
        default=None,
        help="First date to backfill, inclusive (YYYY-MM-DD). If omitted, "
             "computed as --end - --days.",
    )
    p.add_argument(
        "--tickers",
        type=str,
        default=None,
        help="Comma-separated ticker subset forwarded to lthcs_daily.py "
             "(default: all active).",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Re-run even if snapshots/<date>.json already exists "
             "(passes --force to lthcs_daily.py).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned date list and exit (no subprocess calls).",
    )
    p.add_argument(
        "--python",
        type=str,
        default=sys.executable,
        help="Python interpreter to invoke lthcs_daily.py with "
             "(default: the one running this script).",
    )
    p.add_argument(
        "--data-root",
        type=Path,
        default=_DATA_ROOT,
        help="Data root for snapshot-exists check "
             "(default: <repo>/data/lthcs).",
    )
    return p


# ---------------------------------------------------------------------------
# Date math
# ---------------------------------------------------------------------------

def _parse_iso_date(value: str, *, label: str) -> date:
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise SystemExit(
            "ERROR: %s must be a YYYY-MM-DD date string, got %r" % (label, value)
        )
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise SystemExit("ERROR: %s %r is not a real calendar date" % (label, value))


def compute_date_window(
    *,
    days: int,
    start: Optional[str],
    end: Optional[str],
    today: Optional[date] = None,
) -> Tuple[date, date]:
    """Return ``(start_dt, end_dt)`` inclusive, in calendar days.

    Rules:
    - ``end`` defaults to yesterday (today - 1 day).
    - ``start`` defaults to ``end - (days - 1)`` so the window contains
      exactly ``days`` calendar days when only ``--days`` was given.
    - When both ``--start`` and ``--end`` are explicit, ``--days`` is ignored.
    - Raises SystemExit on invalid combinations (start > end, future end, etc.).
    """
    today = today or date.today()
    if days < 1:
        raise SystemExit("ERROR: --days must be >= 1, got %d" % days)

    if end is None:
        end_dt = today - timedelta(days=1)
    else:
        end_dt = _parse_iso_date(end, label="--end")

    if start is None:
        start_dt = end_dt - timedelta(days=days - 1)
    else:
        start_dt = _parse_iso_date(start, label="--start")

    if end_dt > today:
        raise SystemExit(
            "ERROR: --end %s is in the future (today=%s)" % (end_dt, today)
        )
    if start_dt > end_dt:
        raise SystemExit(
            "ERROR: --start %s is after --end %s" % (start_dt, end_dt)
        )
    return start_dt, end_dt


def iter_dates(start_dt: date, end_dt: date) -> List[str]:
    """Inclusive list of ISO date strings, oldest first."""
    out: List[str] = []
    cursor = start_dt
    while cursor <= end_dt:
        out.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return out


# ---------------------------------------------------------------------------
# Snapshot existence
# ---------------------------------------------------------------------------

def snapshot_exists(data_root: Path, calc_date: str) -> bool:
    return (data_root / "snapshots" / ("%s.json" % calc_date)).exists()


# ---------------------------------------------------------------------------
# Subprocess driver
# ---------------------------------------------------------------------------

@dataclass
class DateResult:
    calc_date: str
    status: str            # "ok" | "skipped" | "failed"
    elapsed_s: float = 0.0
    exit_code: int = 0
    rate_limited: bool = False
    reason: str = ""


def build_command(
    python: str,
    *,
    calc_date: str,
    tickers: Optional[str],
    force: bool,
) -> List[str]:
    """Build the argv to run ``lthcs_daily.py`` for one date.

    ``--force`` is always passed so a re-run idempotently overwrites the
    snapshot file we just decided to (re-)write. ``--skip-thesis`` is
    always passed because Alpha Vantage's free tier has no historical
    news archive; the same goes for ai_news / breadth_sentiment (handled
    inside lthcs_daily.py).
    """
    daily_path = _REPO_ROOT / "lthcs_daily.py"
    cmd: List[str] = [
        python,
        str(daily_path),
        "--as-of", calc_date,
        "--skip-thesis",
    ]
    if force:
        cmd.append("--force")
    if tickers:
        cmd.extend(["--tickers", tickers])
    return cmd


def run_one_date(
    *,
    calc_date: str,
    python: str,
    tickers: Optional[str],
    force: bool,
    data_root: Path,
) -> DateResult:
    """Execute the daily pipeline for one date, capturing the outcome.

    Idempotent: if the snapshot already exists and ``--force`` is not
    set, returns a ``"skipped"`` result without spawning a subprocess.
    """
    if not force and snapshot_exists(data_root, calc_date):
        return DateResult(
            calc_date=calc_date,
            status="skipped",
            reason="snapshot exists",
        )

    cmd = build_command(python, calc_date=calc_date, tickers=tickers, force=force)
    start = time.monotonic()
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return DateResult(
            calc_date=calc_date,
            status="failed",
            elapsed_s=time.monotonic() - start,
            exit_code=-1,
            reason="subprocess error: %s" % exc,
        )
    elapsed = time.monotonic() - start

    combined_log = (completed.stdout or "") + "\n" + (completed.stderr or "")
    rate_limited = any(h in combined_log for h in _RATE_LIMIT_HINTS)

    if completed.returncode == 0:
        return DateResult(
            calc_date=calc_date,
            status="ok",
            elapsed_s=elapsed,
            exit_code=0,
            rate_limited=rate_limited,
        )
    # Daily pipeline returns 2 on snapshot-collision specifically; surface
    # that as a distinct failure reason so the operator knows to add --force.
    reason = "exit %d" % completed.returncode
    if completed.returncode == 2:
        reason = "snapshot exists (re-run --force to overwrite)"
    elif rate_limited:
        reason = "rate-limited (exit %d)" % completed.returncode

    # Echo the daily-pipeline log on failure to make the operator's life
    # easier. We don't dump it on success because that would be 10× the
    # noise of the per-date summary line.
    if combined_log.strip():
        print("--- lthcs_daily.py output for %s ---" % calc_date)
        # Indent so it's visually distinct from the orchestrator's lines.
        for line in combined_log.splitlines():
            print("    " + line)
        print("--- end %s ---" % calc_date)

    return DateResult(
        calc_date=calc_date,
        status="failed",
        elapsed_s=elapsed,
        exit_code=completed.returncode,
        rate_limited=rate_limited,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

@dataclass
class BackfillSummary:
    backfilled: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    failed: List[Tuple[str, str]] = field(default_factory=list)  # (date, reason)
    total_runtime_s: float = 0.0

    @property
    def total_attempted(self) -> int:
        return len(self.backfilled) + len(self.skipped) + len(self.failed)


def _format_elapsed(seconds: float) -> str:
    s = int(round(seconds))
    if s < 60:
        return "%ds" % s
    m, s = divmod(s, 60)
    if m < 60:
        return "%dm%02ds" % (m, s)
    h, m = divmod(m, 60)
    return "%dh%02dm%02ds" % (h, m, s)


def run_backfill(args: argparse.Namespace) -> BackfillSummary:
    """Drive the per-date loop. Pure data — caller is responsible for printing.

    Tests can call this directly with a constructed ``argparse.Namespace``
    and inspect the returned summary instead of asserting on stdout.
    """
    start_dt, end_dt = compute_date_window(
        days=args.days, start=args.start, end=args.end
    )
    dates = iter_dates(start_dt, end_dt)

    print(
        "Backfill plan: %d calendar days from %s to %s (inclusive)"
        % (len(dates), start_dt.isoformat(), end_dt.isoformat())
    )
    if args.tickers:
        print("  tickers: %s" % args.tickers)
    if args.force:
        print("  --force: existing snapshots will be overwritten")
    if args.dry_run:
        print("Dry run — listing dates and exiting.")
        for i, d in enumerate(dates, start=1):
            already = snapshot_exists(args.data_root, d)
            marker = " (snapshot already exists)" if already else ""
            print("  [%2d/%2d] %s%s" % (i, len(dates), d, marker))
        return BackfillSummary()

    summary = BackfillSummary()
    overall_start = time.monotonic()
    total = len(dates)
    for idx, calc_date in enumerate(dates, start=1):
        result = run_one_date(
            calc_date=calc_date,
            python=args.python,
            tickers=args.tickers,
            force=args.force,
            data_root=args.data_root,
        )
        if result.status == "ok":
            summary.backfilled.append(calc_date)
            print(
                "Backfill %d/%d  %s  done in %s"
                % (idx, total, calc_date, _format_elapsed(result.elapsed_s))
            )
        elif result.status == "skipped":
            summary.skipped.append(calc_date)
            print(
                "Backfill %d/%d  %s  SKIPPED (%s)"
                % (idx, total, calc_date, result.reason)
            )
        else:
            summary.failed.append((calc_date, result.reason))
            print(
                "Backfill %d/%d  %s  FAILED  (%s)"
                % (idx, total, calc_date, result.reason)
            )

        # Rate-limit cooldown between dates if the prior run sniffed a 429.
        # No point sleeping after the last date.
        if result.rate_limited and idx < total:
            print(
                "  rate-limit detected; pausing %.0fs before next date"
                % _RATE_LIMIT_PAUSE_SECONDS
            )
            time.sleep(_RATE_LIMIT_PAUSE_SECONDS)

    summary.total_runtime_s = time.monotonic() - overall_start
    return summary


def print_summary(summary: BackfillSummary) -> None:
    print("")
    print("=" * 60)
    print(
        "Done. backfilled=%d  skipped=%d  failed=%d  runtime=%s"
        % (
            len(summary.backfilled),
            len(summary.skipped),
            len(summary.failed),
            _format_elapsed(summary.total_runtime_s),
        )
    )
    if summary.failed:
        print("")
        print("Failed dates (retry with --start/--end or --force):")
        for d, reason in summary.failed:
            print("  %s  %s" % (d, reason))


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_argparser()
    args = parser.parse_args(argv)
    summary = run_backfill(args)
    if not args.dry_run:
        print_summary(summary)
    # Non-zero exit if any date failed, so a wrapping CI step can detect it.
    return 1 if summary.failed else 0


if __name__ == "__main__":  # pragma: no cover - thin entry point
    raise SystemExit(main())
