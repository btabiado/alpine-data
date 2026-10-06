#!/usr/bin/env python3
"""Remove carried-forward synthetic history rows for tickers that were not scored.

Before 2026-10-05 the daily catch-up (``LthcsPersist.fill_history_gaps``)
forward-filled EVERY ``history/by_ticker/*.json`` file, so a ticker that
stopped being scored (BK after its 2026-07 ticker change, EA after going
private, DOW after it left the universe on 2026-05-17) kept receiving one
flat synthetic row per day, a copy of its last real score. lthcs_daily then
filled only the active universe, which did not cover DOW: it was re-added on
2026-10-05, so the 2026-10-06 catch-up wrote its 2026-05-16 score onto
05-17..10-05 again (removed with this tool, record
``_synthetic_rows_removed_2026-10-06.json``). Catch-up now fills only days
with no snapshot file, only up to the first run after the ticker's last real
row, and never from a row older than its universe ``added_on``. This tool
removes rows already written.

A row is removed only when the data proves it is such a copy:

* it is ``synthetic: true``;
* it sits in a run of synthetic rows that follows a real row R (up to the
  next real row, or the end of the file) and carries R's exact score and
  band;
* daily snapshot files exist for at least one date of that run, and the
  ticker is in none of them (it was not being scored).

A run made only of days with no snapshot file at all is a gap-day fill
every scored ticker got (disclosed in health/known_gaps.json) and is kept.
A row in a removable run whose score or band differs from R is kept and
reported as ambiguous. Only tickers in universe.json are considered (the
crypto history files share the directory but have their own snapshots).

Usage::

    python scripts/lthcs_prune_carried_forward_history.py            # report only
    python scripts/lthcs_prune_carried_forward_history.py --write \\
        --record data/lthcs/universe_candidate/mapping_2026-10-05/_synthetic_rows_removed.json
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from lthcs.persist import LthcsPersist  # noqa: E402

DATE_FILE = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")

RULE = (
    "Removed: synthetic rows in a run that follows a real row R and copies R's exact score and band, "
    "where daily snapshot files exist for at least one date of the run and the ticker is in none of them "
    "(it was not being scored, so the rows are catch-up copies, not scores). Kept: real rows; synthetic "
    "runs made only of days with no snapshot file at all (gap-day fills every scored ticker got, disclosed "
    "in health/known_gaps.json); any row in a removable run whose value differs from R (ambiguous)."
)


def snapshot_index(snapshots_dir: Path) -> Tuple[Set[str], Dict[str, Set[str]]]:
    """(dates with a snapshot file, {ticker: dates the ticker was scored})."""
    dates: Set[str] = set()
    scored: Dict[str, Set[str]] = {}
    for f in sorted(snapshots_dir.iterdir()):
        if not DATE_FILE.match(f.name):
            continue
        d = f.stem
        dates.add(d)
        try:
            rows = json.loads(f.read_text(encoding="utf-8")).get("scores") or []
        except (OSError, ValueError):
            continue
        for row in rows:
            t = row.get("ticker") if isinstance(row, dict) else None
            if isinstance(t, str):
                scored.setdefault(t, set()).add(d)
    return dates, scored


def carried_forward_rows(
    history: List[Dict[str, Any]],
    scored_dates: Set[str],
    snapshot_dates: Set[str],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return (rows to remove, ambiguous rows kept) for one ticker's history."""
    rows = sorted((r for r in history if isinstance(r, dict) and isinstance(r.get("date"), str)),
                  key=lambda r: r["date"])
    remove: List[Dict[str, Any]] = []
    ambiguous: List[Dict[str, Any]] = []
    anchor: Optional[Dict[str, Any]] = None
    run: List[Dict[str, Any]] = []

    def close_run() -> None:
        if anchor is None or not run:
            return
        dates = {r["date"] for r in run}
        if not (dates & snapshot_dates):
            return  # gap days only: no snapshot was taken for anyone
        if dates & scored_dates:
            return  # the ticker WAS scored on one of these days
        for r in run:
            if r.get("score") == anchor.get("score") and r.get("band") == anchor.get("band"):
                remove.append(r)
            else:
                ambiguous.append(r)

    for r in rows:
        if r.get("synthetic"):
            if anchor is not None:
                run.append(r)
            continue
        close_run()
        anchor, run = r, []
    close_run()
    return remove, ambiguous


def plan(data_root: Path) -> Dict[str, Any]:
    universe = json.loads((data_root / "universe.json").read_text(encoding="utf-8"))
    status = {e["ticker"]: ("active" if e.get("active", True) else "inactive")
              for e in universe.get("tickers", [])}
    snapshot_dates, scored = snapshot_index(data_root / "snapshots")
    store = LthcsPersist(data_root)
    out: Dict[str, Any] = {}
    for f in sorted(store.history_dir.glob("*.json")):
        ticker = f.stem
        if ticker not in status:
            continue
        history = store.read_history(ticker).get("history") or []
        remove, ambiguous = carried_forward_rows(history, scored.get(ticker, set()), snapshot_dates)
        if not remove and not ambiguous:
            continue
        real = sorted((r for r in history if not r.get("synthetic")), key=lambda r: r["date"])
        dates = sorted(r["date"] for r in remove)
        kept_synthetic = sorted(r["date"] for r in history
                                if r.get("synthetic") and r["date"] not in set(dates))
        scored_t = scored.get(ticker, set())
        out[ticker] = {
            "universe_status": status[ticker],
            "rows_before": len(history),
            "real_rows": len(real),
            "last_real_row_before_removed": max(
                (r for r in real if r["date"] < dates[0]), key=lambda r: r["date"]) if dates else None,
            "removed_count": len(remove),
            "removed_from": dates[0] if dates else None,
            "removed_to": dates[-1] if dates else None,
            "removed_dates": dates,
            "removed_values": sorted({(r.get("score"), r.get("band")) for r in remove}),
            "snapshot_files_on_removed_dates": len(set(dates) & snapshot_dates),
            "ticker_rows_in_those_snapshots": len(set(dates) & scored_t),
            "last_snapshot_with_ticker": max(scored_t) if scored_t else None,
            "ambiguous_kept": ambiguous,
            "synthetic_rows_kept": kept_synthetic,
        }
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=REPO_ROOT / "data" / "lthcs")
    ap.add_argument("--write", action="store_true", help="rewrite the history files")
    ap.add_argument("--record", type=Path, help="write the removal record (JSON) here")
    args = ap.parse_args(argv)
    result = plan(args.root)
    if not result:
        print("no carried-forward synthetic rows")
        return 0
    for t, r in result.items():
        print("%s (%s): remove %d synthetic rows %s..%s copying %s; ticker in %d of %d snapshot files "
              "on those dates; last scored %s; ambiguous kept %d; synthetic kept %d"
              % (t, r["universe_status"], r["removed_count"], r["removed_from"], r["removed_to"],
                 r["removed_values"], r["ticker_rows_in_those_snapshots"],
                 r["snapshot_files_on_removed_dates"], r["last_snapshot_with_ticker"],
                 len(r["ambiguous_kept"]), len(r["synthetic_rows_kept"])))
    if args.write:
        store = LthcsPersist(args.root)
        for t, r in result.items():
            n = store.drop_synthetic_entries(t, r["removed_dates"])
            assert n == r["removed_count"], (t, n, r["removed_count"])
        print("rewrote %d history files" % len(result))
    if args.record:
        record = {
            "generated": _dt.date.today().isoformat(),
            "tool": "scripts/lthcs_prune_carried_forward_history.py",
            "rule": RULE,
            "tickers": result,
        }
        args.record.write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
        print("wrote %s" % args.record)
    return 0


if __name__ == "__main__":
    sys.exit(main())
