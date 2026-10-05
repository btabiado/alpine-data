"""Validation entry point: `python -m lthcs.validate [--date YYYY-MM-DD]`.

Without args: validates universe.json + weights.json (Week 1 gate).
With --date: also validates that day's snapshot, variable_detail,
narratives, and per-ticker history entries are consistent (Week 7 gate).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Tuple

from pydantic import ValidationError

from lthcs.schemas import Universe, Weights

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data" / "lthcs"

PILLAR_KEYS = (
    "adoption_momentum",
    "institutional_confidence",
    "financial_evolution",
    "thesis_integrity",
    "des",
)

OK = "✓"
FAIL = "✗"


def _print_ok(msg: str) -> None:
    print(f"{OK} {msg}")


def _print_fail(msg: str) -> None:
    print(f"{FAIL} {msg}", file=sys.stderr)


def _load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def validate_universe(path: Path | None = None) -> Tuple[bool, Universe | None]:
    path = path or (DATA_DIR / "universe.json")
    if not path.exists():
        _print_fail(f"universe.json not found at {path}")
        return False, None
    try:
        raw = _load_json(path)
        universe = Universe.model_validate(raw)
    except (json.JSONDecodeError, ValidationError) as exc:
        _print_fail(f"universe.json invalid: {exc}")
        return False, None
    active = sum(1 for t in universe.tickers if t.active)
    _print_ok(
        f"universe.json valid ({len(universe.tickers)} tickers, "
        f"{active} active, version {universe.version})"
    )
    # Index Exiles: an active ticker with no index tag must carry the
    # index_exile marker (it left every tracked index and is still scored).
    # A tagless active ticker without it would silently drop out of every
    # index filter AND the exile filter.
    untagged = sorted(t.ticker for t in universe.tickers
                      if t.active and not t.index_membership and t.index_exile is None)
    if untagged:
        _print_fail(f"active tickers with no index tag and no index_exile marker: {untagged}")
        return False, universe
    exiles = sorted(t.ticker for t in universe.tickers if t.active and t.index_exile is not None)
    _print_ok(f"index exiles (active, scored, in no index): {len(exiles)}"
              + (f" ({', '.join(exiles)})" if exiles else ""))
    return True, universe


def validate_weights(path: Path | None = None) -> Tuple[bool, Weights | None]:
    path = path or (DATA_DIR / "weights.json")
    if not path.exists():
        _print_fail(f"weights.json not found at {path}")
        return False, None
    try:
        raw = _load_json(path)
        weights = Weights.model_validate(raw)
    except (json.JSONDecodeError, ValidationError) as exc:
        _print_fail(f"weights.json invalid: {exc}")
        return False, None
    _print_ok(
        f"weights.json valid ({len(weights.profiles)} profiles, "
        f"{len(weights.score_bands)} score bands, version {weights.version})"
    )
    return True, weights


def validate_universe_against_weights(universe: Universe, weights: Weights) -> bool:
    profile_names = set(weights.profiles.keys())
    missing: list[str] = []
    for entry in universe.tickers:
        if entry.maturity_stage not in profile_names:
            missing.append(f"{entry.ticker}={entry.maturity_stage}")
    if missing:
        _print_fail(
            f"universe references maturity_stage profiles not in weights.json: {missing}"
        )
        return False
    _print_ok(
        f"cross-check: every universe maturity_stage has a weights profile"
    )
    return True


def validate_snapshot_for_date(date_str: str, universe: Universe) -> bool:
    """Validate snapshot, variable_detail, narratives, and history for one date.

    Returns True if every check passes; False otherwise.
    """
    snap_path = DATA_DIR / "snapshots" / f"{date_str}.json"
    var_path = DATA_DIR / "variable_detail" / f"{date_str}.json"
    narr_path = DATA_DIR / "narratives" / f"{date_str}.json"

    all_ok = True

    # 1. Snapshot file exists and parses.
    if not snap_path.exists():
        _print_fail(f"snapshot for {date_str} not found at {snap_path}")
        return False
    try:
        snap = _load_json(snap_path)
    except json.JSONDecodeError as exc:
        _print_fail(f"snapshot for {date_str} not valid JSON: {exc}")
        return False

    scores = snap.get("scores")
    if not isinstance(scores, list):
        _print_fail(f"snapshot.scores must be a list, got {type(scores).__name__}")
        return False

    snap_tickers = {row.get("ticker") for row in scores if isinstance(row, dict)}
    # A ticker added to universe.json ON or after this date is not expected in
    # it: the day's snapshot can predate the change (2026-10-05's was cut at
    # 01:59Z, the S&P 500 sync added 300 tickers at 02:08Z). Same rule as
    # lthcs.backfill_validate.expected_from.
    try:
        day = date.fromisoformat(date_str)
    except ValueError:
        day = None
    not_yet = {t.ticker for t in universe.tickers
               if t.active and t.added_on is not None and day is not None and t.added_on >= day}
    active_tickers = {t.ticker for t in universe.tickers if t.active} - not_yet

    # 2. Every active universe ticker has a score.
    missing = sorted(active_tickers - snap_tickers)
    if missing:
        _print_fail(
            f"snapshot missing {len(missing)} active universe tickers: "
            f"{missing[:5]}{'...' if len(missing) > 5 else ''}"
        )
        all_ok = False
    else:
        _print_ok(f"snapshot covers all {len(active_tickers)} active universe tickers"
                  + (f" ({len(not_yet)} added on/after {date_str} not expected yet)"
                     if not_yet else ""))

    # 3. Every score and sub-score is in [0, 100].
    out_of_range = []
    for row in scores:
        if not isinstance(row, dict):
            continue
        ticker = row.get("ticker", "?")
        s = row.get("lthcs_score")
        if not (isinstance(s, (int, float)) and 0 <= s <= 100):
            out_of_range.append(f"{ticker} lthcs_score={s}")
        dropped = set(row.get("dropped_pillars") or [])
        for k in PILLAR_KEYS:
            v = (row.get("subscores") or {}).get(k)
            if v is None and k in dropped:
                continue  # dropped pillar: published as null by design
            if not (isinstance(v, (int, float)) and 0 <= v <= 100):
                out_of_range.append(f"{ticker} {k}={v}")
    if out_of_range:
        _print_fail(
            f"{len(out_of_range)} score/sub-score values out of [0, 100]: "
            f"{out_of_range[:3]}{'...' if len(out_of_range) > 3 else ''}"
        )
        all_ok = False
    else:
        _print_ok("all lthcs_score and sub-score values in [0, 100]")

    # 4. variable_detail file present.
    if not var_path.exists():
        _print_fail(f"variable_detail for {date_str} not found at {var_path}")
        all_ok = False
    else:
        try:
            var = _load_json(var_path)
            if not isinstance(var.get("variables"), list):
                raise ValueError("variables must be a list")
            _print_ok(
                f"variable_detail valid ({len(var['variables'])} entries)"
            )
        except (json.JSONDecodeError, ValueError) as exc:
            _print_fail(f"variable_detail invalid: {exc}")
            all_ok = False

    # 5. narratives file: every ticker in snapshot has a narrative.
    if not narr_path.exists():
        _print_fail(f"narratives for {date_str} not found at {narr_path}")
        all_ok = False
    else:
        try:
            narr = _load_json(narr_path)
            narratives_list = narr.get("narratives")
            if not isinstance(narratives_list, list):
                raise ValueError("narratives must be a list")
            narr_tickers = {n.get("ticker") for n in narratives_list if isinstance(n, dict)}
            missing_narr = sorted(snap_tickers - narr_tickers)
            if missing_narr:
                _print_fail(
                    f"{len(missing_narr)} snapshot tickers have no narrative: "
                    f"{missing_narr[:5]}{'...' if len(missing_narr) > 5 else ''}"
                )
                all_ok = False
            else:
                _print_ok(
                    f"narratives present for all {len(snap_tickers)} snapshot tickers"
                )
        except (json.JSONDecodeError, ValueError) as exc:
            _print_fail(f"narratives invalid: {exc}")
            all_ok = False

    # 6. History file for each snapshot ticker has today's entry.
    hist_dir = DATA_DIR / "history" / "by_ticker"
    missing_hist: list[str] = []
    for ticker in sorted(snap_tickers):
        if ticker is None:
            continue
        hist_path = hist_dir / f"{ticker}.json"
        if not hist_path.exists():
            missing_hist.append(f"{ticker}(no file)")
            continue
        try:
            hist = _load_json(hist_path)
        except json.JSONDecodeError:
            missing_hist.append(f"{ticker}(bad json)")
            continue
        dates_in_hist = {h.get("date") for h in (hist.get("history") or [])}
        if date_str not in dates_in_hist:
            missing_hist.append(f"{ticker}(no row for {date_str})")
    if missing_hist:
        _print_fail(
            f"{len(missing_hist)} history files missing today's entry: "
            f"{missing_hist[:5]}{'...' if len(missing_hist) > 5 else ''}"
        )
        all_ok = False
    else:
        _print_ok(
            f"history entry for {date_str} present in all {len(snap_tickers)} ticker files"
        )

    return all_ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lthcs.validate",
        description="Validate LTHCS data files.",
    )
    parser.add_argument(
        "--date",
        help="Also validate the snapshot/variable_detail/narratives/history "
        "for a specific date (YYYY-MM-DD).",
    )
    args = parser.parse_args(argv)

    ok_universe, universe = validate_universe()
    ok_weights, weights = validate_weights()

    if not (ok_universe and ok_weights):
        return 1

    assert universe is not None and weights is not None
    if not validate_universe_against_weights(universe, weights):
        return 1

    if args.date:
        print()
        date_ok = validate_snapshot_for_date(args.date, universe)
        print()
        if not date_ok:
            _print_fail(f"Snapshot validation FAILED for {args.date}")
            return 1
        _print_ok(f"Snapshot validation passed for {args.date}")
        return 0

    print()
    _print_ok("Week 1 validation gate passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
