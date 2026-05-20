#!/usr/bin/env python3
"""Diff two LTHCS snapshots and emit per-ticker deltas + KPI summary.

This module is the canonical, unit-testable implementation of the snapshot
diff math that the ``/lthcs/diff/`` UI uses. The JS front-end re-implements
the same shape on the client (so we don't ship a server round-trip), but
ANY drift in semantics should be caught here first — the Python helper is
the source of truth for what "diff" means.

A "snapshot" is the on-disk shape at ``data/lthcs/snapshots/<YYYY-MM-DD>.json``:

    {
      "calc_date": "2026-05-18",
      "model_version": "v1.1.0",
      "scores": [
        {
          "ticker": "AAPL",
          "lthcs_score": 54.2,
          "band": "weakening",
          "subscores": {
            "adoption_momentum": 28.1,
            "institutional_confidence": 68.7,
            "financial_evolution": 67.5,
            "thesis_integrity": 73.1,
            "des": 43.5
          },
          ...
        },
        ...
      ]
    }

The diff is keyed by ticker. Tickers present in A but missing from B are
marked ``inactive`` (with their previous band carried through so the UI
can render "was Review"). Tickers present in B but missing from A are
marked ``new``. The "band promotion / demotion" KPI counts only operate
on the intersection — newly-added/removed tickers don't move the
"tickers up / tickers down" counters.

Band ordering for promotion/demotion detection:
    elite > high_confidence > constructive > monitor > weakening > review

Run as a CLI for ad-hoc diffing::

    python -m scripts.lthcs_diff_snapshots 2026-05-17 2026-05-18
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


# Band ordering. Index 0 is the strongest band; higher index = weaker.
# Used to detect promotion (B index < A index) vs demotion (B index > A index).
BAND_ORDER = [
    "elite",
    "high_confidence",
    "constructive",
    "monitor",
    "weakening",
    "review",
]

PILLAR_KEYS = [
    "adoption_momentum",
    "institutional_confidence",
    "financial_evolution",
    "thesis_integrity",
    "des",
]


def _band_rank(band: str | None) -> int:
    """Return the rank index for a band string, or len(BAND_ORDER) for unknown."""
    if band is None:
        return len(BAND_ORDER)
    try:
        return BAND_ORDER.index(band)
    except ValueError:
        return len(BAND_ORDER)


def _ticker_index(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Build a ticker -> score-row map from a snapshot dict."""
    if not isinstance(snapshot, dict):
        return {}
    scores = snapshot.get("scores") or []
    out: dict[str, dict[str, Any]] = {}
    for row in scores:
        ticker = row.get("ticker")
        if isinstance(ticker, str) and ticker:
            out[ticker] = row
    return out


def _pillar_delta(
    row_a: dict[str, Any], row_b: dict[str, Any], key: str
) -> float | None:
    """Return ``b - a`` for a single pillar, or None if either side is missing."""
    sub_a = (row_a or {}).get("subscores") or {}
    sub_b = (row_b or {}).get("subscores") or {}
    a_val = sub_a.get(key)
    b_val = sub_b.get(key)
    if a_val is None or b_val is None:
        return None
    try:
        return round(float(b_val) - float(a_val), 2)
    except (TypeError, ValueError):
        return None


def diff_snapshots(
    snapshot_a: dict[str, Any], snapshot_b: dict[str, Any]
) -> dict[str, Any]:
    """Diff two LTHCS snapshots.

    Returns a dict with:
        - ``date_a`` / ``date_b``: the ``calc_date`` from each snapshot
        - ``tickers``: list of per-ticker diff dicts
        - ``summary``: KPI counts (tickers_up, tickers_down, tickers_unchanged,
          avg_composite_shift, total_compared, tickers_inactive, tickers_new)

    Per-ticker diff shape::

        {
          "ticker": "AAPL",
          "score_a": 54.2,
          "score_b": 53.1,
          "delta": -1.1,
          "band_a": "weakening",
          "band_b": "weakening",
          "band_change": "same" | "promotion" | "demotion" | "inactive" | "new",
          "pillar_deltas": {
            "adoption_momentum": +1.0,
            "institutional_confidence": -0.4,
            ...
          },
          "sector": "Technology"  # carried through from row B (or A if inactive)
        }
    """
    if not isinstance(snapshot_a, dict) or not isinstance(snapshot_b, dict):
        raise TypeError("diff_snapshots requires dict inputs")

    date_a = snapshot_a.get("calc_date")
    date_b = snapshot_b.get("calc_date")

    idx_a = _ticker_index(snapshot_a)
    idx_b = _ticker_index(snapshot_b)

    all_tickers = sorted(set(idx_a.keys()) | set(idx_b.keys()))

    rows: list[dict[str, Any]] = []
    deltas_for_avg: list[float] = []
    tickers_up = 0
    tickers_down = 0
    tickers_unchanged = 0
    tickers_inactive = 0
    tickers_new = 0

    for ticker in all_tickers:
        row_a = idx_a.get(ticker)
        row_b = idx_b.get(ticker)

        if row_a is not None and row_b is None:
            # Was in A, dropped in B → inactive (e.g. delisting, universe trim).
            tickers_inactive += 1
            band_a = row_a.get("band")
            rows.append(
                {
                    "ticker": ticker,
                    "score_a": row_a.get("lthcs_score"),
                    "score_b": None,
                    "delta": None,
                    "band_a": band_a,
                    "band_b": None,
                    "band_change": "inactive",
                    "pillar_deltas": {},
                    "sector": row_a.get("sector"),
                }
            )
            continue

        if row_a is None and row_b is not None:
            # New in B (e.g. universe addition).
            tickers_new += 1
            rows.append(
                {
                    "ticker": ticker,
                    "score_a": None,
                    "score_b": row_b.get("lthcs_score"),
                    "delta": None,
                    "band_a": None,
                    "band_b": row_b.get("band"),
                    "band_change": "new",
                    "pillar_deltas": {},
                    "sector": row_b.get("sector"),
                }
            )
            continue

        # Common ticker — the meat of the diff.
        score_a_raw = row_a.get("lthcs_score") if row_a else None
        score_b_raw = row_b.get("lthcs_score") if row_b else None
        try:
            score_a = float(score_a_raw) if score_a_raw is not None else None
            score_b = float(score_b_raw) if score_b_raw is not None else None
        except (TypeError, ValueError):
            score_a = None
            score_b = None

        if score_a is not None and score_b is not None:
            delta: float | None = round(score_b - score_a, 2)
            deltas_for_avg.append(delta)
        else:
            delta = None

        band_a = row_a.get("band") if row_a else None
        band_b = row_b.get("band") if row_b else None
        if band_a == band_b:
            band_change = "same"
            if delta is not None and abs(delta) < 0.05:
                tickers_unchanged += 1
        else:
            rank_a = _band_rank(band_a)
            rank_b = _band_rank(band_b)
            if rank_b < rank_a:
                band_change = "promotion"
                tickers_up += 1
            elif rank_b > rank_a:
                band_change = "demotion"
                tickers_down += 1
            else:
                # Same rank but different label (shouldn't happen w/ canonical
                # bands; treat as "same" defensively).
                band_change = "same"

        pillar_deltas: dict[str, float | None] = {}
        for key in PILLAR_KEYS:
            pillar_deltas[key] = _pillar_delta(row_a or {}, row_b or {}, key)

        rows.append(
            {
                "ticker": ticker,
                "score_a": score_a,
                "score_b": score_b,
                "delta": delta,
                "band_a": band_a,
                "band_b": band_b,
                "band_change": band_change,
                "pillar_deltas": pillar_deltas,
                "sector": (row_b or {}).get("sector") or (row_a or {}).get("sector"),
            }
        )

    total_compared = len(deltas_for_avg)
    if total_compared > 0:
        avg_shift = round(sum(deltas_for_avg) / total_compared, 3)
    else:
        avg_shift = 0.0

    summary = {
        "tickers_up": tickers_up,
        "tickers_down": tickers_down,
        "tickers_unchanged": tickers_unchanged,
        "avg_composite_shift": avg_shift,
        "total_compared": total_compared,
        "tickers_inactive": tickers_inactive,
        "tickers_new": tickers_new,
    }

    return {
        "date_a": date_a,
        "date_b": date_b,
        "tickers": rows,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


def _load_snapshot(data_root: Path, date: str) -> dict[str, Any]:
    path = data_root / "snapshots" / f"{date}.json"
    if not path.exists():
        raise FileNotFoundError(f"snapshot missing: {path}")
    return json.loads(path.read_text())


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diff two LTHCS snapshots.")
    parser.add_argument("date_a", help="From-date (YYYY-MM-DD)")
    parser.add_argument("date_b", help="To-date (YYYY-MM-DD)")
    parser.add_argument(
        "--data-root",
        default="data/lthcs",
        help="Path to data/lthcs/ (default: data/lthcs)",
    )
    parser.add_argument(
        "--top", type=int, default=5, help="Show top-N movers (default 5)"
    )
    args = parser.parse_args(argv)

    root = Path(args.data_root)
    snap_a = _load_snapshot(root, args.date_a)
    snap_b = _load_snapshot(root, args.date_b)
    result = diff_snapshots(snap_a, snap_b)

    s = result["summary"]
    print(
        f"{result['date_a']} -> {result['date_b']}: "
        f"up={s['tickers_up']} down={s['tickers_down']} "
        f"flat={s['tickers_unchanged']} "
        f"avg_shift={s['avg_composite_shift']:+.2f} "
        f"compared={s['total_compared']} "
        f"new={s['tickers_new']} inactive={s['tickers_inactive']}"
    )
    movers = [r for r in result["tickers"] if r["delta"] is not None]
    movers.sort(key=lambda r: -abs(r["delta"]))
    print(f"\nTop {args.top} movers by |delta|:")
    for row in movers[: args.top]:
        sign = "+" if row["delta"] >= 0 else ""
        print(
            f"  {row['ticker']:<6} "
            f"{row['score_a']:>5.1f} -> {row['score_b']:>5.1f} "
            f"({sign}{row['delta']:+.2f})  "
            f"{row['band_a']} -> {row['band_b']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(_main())
