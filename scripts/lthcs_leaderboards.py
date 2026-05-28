#!/usr/bin/env python3
"""Compute LTHCS per-pillar leaderboards from the latest snapshot.

CLI surface mirroring the in-browser /lthcs/leaderboards/ page (Phase 5 #7):
seven leaderboards over the universe — top 10 by composite, top 10 by each of
the five pillars, and bottom 10 by composite (the "trouble" watch list).

Useful for:
  - Sanity-checking the page renders the same ranks the JS does.
  - Piping a snapshot into a one-line CLI report.

Idempotency: deterministic for a fixed snapshot. Ties broken by ticker (A→Z)
so two runs over identical input produce byte-identical output.

CLI::

    python -m scripts.lthcs_leaderboards
    python -m scripts.lthcs_leaderboards --snapshot data/lthcs/snapshots/2026-05-18.json
    python -m scripts.lthcs_leaderboards --scope sp-100
    python -m scripts.lthcs_leaderboards --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


# Pillar key → human label. Order matches the page render order.
PILLARS: Tuple[Tuple[str, str], ...] = (
    ("adoption_momentum", "Adoption Momentum"),
    ("institutional_confidence", "Institutional Confidence"),
    ("financial_evolution", "Financial Evolution"),
    ("thesis_integrity", "Thesis Integrity"),
    ("des", "DES"),
)

# Scope key → universe.json `index_membership` value.
SCOPE_TO_INDEX: Dict[str, str] = {
    "djia": "DJIA",
    "sp-100": "S&P 100",
    "nasdaq-100": "NASDAQ-100",
}

TOP_N = 10
REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def find_latest_snapshot(data_root: Path) -> Path:
    """Pick the alphabetically-last dated snapshot under ``data_root/snapshots/``.

    ``index.json`` is a metadata file and is excluded. Raises ``FileNotFoundError``
    when no dated snapshots exist.
    """
    snap_dir = data_root / "snapshots"
    candidates = sorted(
        p for p in snap_dir.glob("*.json") if p.stem != "index"
    )
    if not candidates:
        raise FileNotFoundError(f"No dated snapshots found in {snap_dir}")
    return candidates[-1]


def load_universe_index_members(
    universe: Dict[str, Any], scope: str
) -> Optional[set]:
    """Return the ticker set for a given index-membership scope, or ``None``
    when ``scope`` doesn't narrow the universe (i.e. 'all').

    Inactive tickers are excluded — matches the JS side.
    """
    if scope == "all":
        return None
    label = SCOPE_TO_INDEX.get(scope)
    if not label:
        raise ValueError(f"Unknown scope: {scope!r}")
    out: set = set()
    for row in universe.get("tickers", []) or []:
        if not row or row.get("active") is False:
            continue
        memberships = row.get("index_membership") or []
        if label in memberships:
            out.add(row["ticker"])
    return out


# ---------------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------------


def _entry(score_row: Dict[str, Any], pillar_value: Any) -> Dict[str, Any]:
    return {
        "ticker": score_row.get("ticker"),
        "composite": score_row.get("lthcs_score"),
        "pillar_score": pillar_value,
        "band": score_row.get("band"),
    }


def _sort_key_desc(value: Any, ticker: str) -> Tuple[float, str]:
    """Sort by score descending, ties broken by ticker ascending. Tuple-sort
    sorts ascending, so we negate the score and keep ticker positive.
    """
    if not isinstance(value, (int, float)):
        return (float("inf"), ticker)  # missing values sink
    return (-float(value), ticker)


def _sort_key_asc(value: Any, ticker: str) -> Tuple[float, str]:
    """Sort by score ascending (for the 'trouble' bottom-N), ties broken by
    ticker ascending so identical scores still order deterministically.
    """
    if not isinstance(value, (int, float)):
        return (float("inf"), ticker)
    return (float(value), ticker)


def compute_leaderboards(
    snapshot: Dict[str, Any],
    ticker_set: Optional[set] = None,
    top_n: int = TOP_N,
) -> Dict[str, Any]:
    """Compute the 7 leaderboards from a snapshot.

    Returns a dict with keys:
      - composite_top: List[entry]
      - composite_bottom: List[entry]
      - pillars: Dict[pillar_key, List[entry]]
    """
    scores = snapshot.get("scores") or []
    if ticker_set is not None:
        scores = [s for s in scores if s.get("ticker") in ticker_set]

    by_composite_desc = sorted(
        scores,
        key=lambda s: _sort_key_desc(s.get("lthcs_score"), s.get("ticker") or ""),
    )
    composite_top = [
        _entry(s, s.get("lthcs_score")) for s in by_composite_desc[:top_n]
    ]

    by_composite_asc = sorted(
        scores,
        key=lambda s: _sort_key_asc(s.get("lthcs_score"), s.get("ticker") or ""),
    )
    composite_bottom = [
        _entry(s, s.get("lthcs_score")) for s in by_composite_asc[:top_n]
    ]

    pillars: Dict[str, List[Dict[str, Any]]] = {}
    for key, _ in PILLARS:
        ranked = sorted(
            scores,
            key=lambda s, k=key: _sort_key_desc(
                (s.get("subscores") or {}).get(k), s.get("ticker") or ""
            ),
        )
        pillars[key] = [
            _entry(s, (s.get("subscores") or {}).get(key))
            for s in ranked[:top_n]
        ]

    return {
        "composite_top": composite_top,
        "composite_bottom": composite_bottom,
        "pillars": pillars,
    }


# ---------------------------------------------------------------------------
# Pretty-print
# ---------------------------------------------------------------------------


def _format_board(title: str, entries: Sequence[Dict[str, Any]]) -> str:
    lines = [f"== {title} =="]
    if not entries:
        lines.append("  (no tickers in scope)")
        return "\n".join(lines)
    for i, e in enumerate(entries, 1):
        score = e.get("pillar_score")
        comp = e.get("composite")
        score_s = f"{score:5.1f}" if isinstance(score, (int, float)) else "  —  "
        comp_s = (
            f"composite {comp:5.1f}" if isinstance(comp, (int, float)) else "composite   —"
        )
        band_s = e.get("band") or ""
        lines.append(
            f"  {i:2d}. {e['ticker']:<6} {score_s}  ({comp_s}, {band_s})"
        )
    return "\n".join(lines)


def format_leaderboards(boards: Dict[str, Any], calc_date: str) -> str:
    sections = [f"LTHCS Leaderboards — {calc_date}", ""]
    sections.append(_format_board("Top 10 by Composite", boards["composite_top"]))
    for key, label in PILLARS:
        sections.append("")
        sections.append(
            _format_board(f"Top 10 by {label}", boards["pillars"].get(key, []))
        )
    sections.append("")
    sections.append(
        _format_board("Bottom 10 by Composite", boards["composite_bottom"])
    )
    return "\n".join(sections)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lthcs_leaderboards",
        description=__doc__.split("\n\n")[0] if __doc__ else None,
    )
    p.add_argument(
        "--data-root",
        type=Path,
        default=REPO_ROOT / "data" / "lthcs",
        help="Root of the data/lthcs/ tree (default: repo's data/lthcs/).",
    )
    p.add_argument(
        "--snapshot",
        type=Path,
        default=None,
        help="Specific snapshot JSON to use (default: latest under data-root).",
    )
    p.add_argument(
        "--scope",
        choices=["all", *SCOPE_TO_INDEX.keys()],
        default="all",
        help="Restrict to an index membership (default: all).",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of the pretty-printed table.",
    )
    p.add_argument(
        "--top",
        type=int,
        default=TOP_N,
        help=f"Number of entries per board (default: {TOP_N}).",
    )
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_argparser().parse_args(argv)

    if args.snapshot is None:
        snapshot_path = find_latest_snapshot(args.data_root)
    else:
        snapshot_path = args.snapshot
    snapshot = _read_json(snapshot_path)

    ticker_set: Optional[set] = None
    if args.scope != "all":
        universe = _read_json(args.data_root / "universe.json")
        ticker_set = load_universe_index_members(universe, args.scope)

    boards = compute_leaderboards(snapshot, ticker_set, top_n=args.top)

    if args.json:
        payload = {
            "calc_date": snapshot.get("calc_date"),
            "scope": args.scope,
            "boards": boards,
        }
        json.dump(payload, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(
            format_leaderboards(boards, snapshot.get("calc_date") or "—") + "\n"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
