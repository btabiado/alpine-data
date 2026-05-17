"""Validation entry point: `python -m lthcs.validate`.

Week 1: validates universe.json and weights.json.
Later weeks add --date checks for daily snapshot artifacts.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Tuple

from pydantic import ValidationError

from lthcs.schemas import Universe, Weights

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data" / "lthcs"

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lthcs.validate",
        description="Validate LTHCS data files.",
    )
    parser.add_argument(
        "--date",
        help="Validate a specific daily snapshot date (YYYY-MM-DD). "
        "Not implemented until Week 6.",
    )
    args = parser.parse_args(argv)

    if args.date:
        _print_fail(f"--date validation lands in Week 6; got --date {args.date}")
        return 2

    ok_universe, universe = validate_universe()
    ok_weights, weights = validate_weights()

    if not (ok_universe and ok_weights):
        return 1

    assert universe is not None and weights is not None
    if not validate_universe_against_weights(universe, weights):
        return 1

    print()
    _print_ok("Week 1 validation gate passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
