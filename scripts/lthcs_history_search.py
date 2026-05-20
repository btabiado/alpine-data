#!/usr/bin/env python3
"""CLI for the LTHCS score-history search.

Mirrors the three modes exposed by the web UI at /lthcs/history/:

* ``ticker``: dump full composite history + band-change events for one symbol.
* ``band``: list tickers that touched a given band within a window
  (``--window 7|30|90|all``), sorted by days-in-band descending.
* ``streak``: top-N longest consecutive runs in each band across the
  whole universe.

Source data: ``data/lthcs/history/by_ticker/<TKR>.json`` — the same per-ticker
time series the browser reads. The file shape::

    {
      "ticker": "AAPL",
      "model_version": "v1.1.0",
      "history": [
        {"date": "2026-05-18", "score": 54.2, "band": "weakening"},
        ...
      ]
    }

Phase 5 ZETA — useful for tests + ad-hoc analyst questions like
"every time AAPL was in Elite" without firing up the dev server.

CLI::

    python -m scripts.lthcs_history_search ticker AAPL
    python -m scripts.lthcs_history_search band elite --window 30
    python -m scripts.lthcs_history_search streak --top 10
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

BANDS = (
    "elite",
    "high_confidence",
    "constructive",
    "monitor",
    "weakening",
    "review",
)


def _repo_root_from_here() -> Path:
    # scripts/lthcs_history_search.py -> repo root is parent of "scripts".
    return Path(__file__).resolve().parent.parent


def _default_data_root() -> Path:
    return _repo_root_from_here() / "data" / "lthcs"


@dataclass(frozen=True)
class Run:
    ticker: str
    band: Optional[str]
    start: str
    end: str
    days: int


def load_ticker_history(ticker: str, data_root: Path) -> Optional[Dict[str, Any]]:
    """Load one ticker's history JSON, ascending by date. Returns None if missing."""
    path = data_root / "history" / "by_ticker" / f"{ticker.upper()}.json"
    if not path.exists():
        return None
    raw = json.loads(path.read_text())
    hist = list(raw.get("history") or [])
    hist.sort(key=lambda h: h.get("date", ""))
    return {
        "ticker": raw.get("ticker", ticker.upper()),
        "model_version": raw.get("model_version"),
        "history": hist,
    }


def iter_history_files(data_root: Path) -> Iterable[Path]:
    base = data_root / "history" / "by_ticker"
    if not base.exists():
        return ()
    return sorted(base.glob("*.json"))


def band_runs(history: List[Dict[str, Any]], ticker: str = "") -> List[Run]:
    """Run-length encode a sorted-ascending history into Run objects."""
    runs: List[Run] = []
    cur_band: Optional[str] = None
    cur_start: Optional[str] = None
    cur_end: Optional[str] = None
    cur_len = 0
    for h in history:
        d = h.get("date")
        if not d:
            continue
        b = h.get("band")
        if cur_band == b and cur_start is not None:
            cur_end = d
            cur_len += 1
        else:
            if cur_start is not None:
                runs.append(Run(ticker, cur_band, cur_start, cur_end or cur_start, cur_len))
            cur_band = b
            cur_start = d
            cur_end = d
            cur_len = 1
    if cur_start is not None:
        runs.append(Run(ticker, cur_band, cur_start, cur_end or cur_start, cur_len))
    return runs


def _window_start(window: str, latest_iso: Optional[str]) -> Optional[str]:
    if window == "all":
        return None
    try:
        n = int(window)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    if latest_iso:
        today = datetime.strptime(latest_iso, "%Y-%m-%d").date()
    else:
        today = datetime.now(timezone.utc).date()
    return (today - timedelta(days=n - 1)).isoformat()


def search_by_ticker(ticker: str, data_root: Path) -> Dict[str, Any]:
    data = load_ticker_history(ticker, data_root)
    if data is None:
        return {"ticker": ticker.upper(), "snapshots": 0, "events": []}
    runs = band_runs(data["history"], ticker=data["ticker"])
    events = [
        {"band": r.band, "date_in": r.start, "date_out": r.end, "days": r.days}
        for r in runs
    ]
    return {
        "ticker": data["ticker"],
        "model_version": data.get("model_version"),
        "snapshots": len(data["history"]),
        "first_date": data["history"][0]["date"] if data["history"] else None,
        "last_date": data["history"][-1]["date"] if data["history"] else None,
        "events": events,
    }


def search_by_band(band: str, window: str, data_root: Path) -> Dict[str, Any]:
    if band not in BANDS:
        raise ValueError(f"unknown band: {band!r} (expected one of {BANDS})")
    # First pass: figure out latest date across the universe.
    files = list(iter_history_files(data_root))
    latest: Optional[str] = None
    all_data: List[Dict[str, Any]] = []
    for p in files:
        try:
            raw = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        hist = sorted(raw.get("history") or [], key=lambda h: h.get("date", ""))
        if not hist:
            continue
        last = hist[-1].get("date")
        if last and (latest is None or last > latest):
            latest = last
        all_data.append({"ticker": raw.get("ticker", p.stem), "history": hist})

    start = _window_start(window, latest)
    rows: List[Dict[str, Any]] = []
    for d in all_data:
        days = 0
        first_hit: Optional[str] = None
        last_hit: Optional[str] = None
        for h in d["history"]:
            if not h.get("band"):
                continue
            if start and h["date"] < start:
                continue
            if h["band"] == band:
                days += 1
                if not first_hit:
                    first_hit = h["date"]
                last_hit = h["date"]
        if days > 0:
            rows.append({
                "ticker": d["ticker"],
                "days": days,
                "first_hit": first_hit,
                "last_hit": last_hit,
            })
    rows.sort(key=lambda r: (-r["days"], r["ticker"]))
    return {
        "band": band,
        "window": window,
        "window_start": start,
        "latest_date_in_universe": latest,
        "ticker_count": len(rows),
        "rows": rows,
    }


def search_by_streak(top: int, data_root: Path) -> Dict[str, Any]:
    per_band: Dict[str, List[Run]] = {b: [] for b in BANDS}
    for p in iter_history_files(data_root):
        try:
            raw = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        hist = sorted(raw.get("history") or [], key=lambda h: h.get("date", ""))
        if not hist:
            continue
        ticker = raw.get("ticker", p.stem)
        for r in band_runs(hist, ticker=ticker):
            if r.band in per_band:
                per_band[r.band].append(r)
    out: Dict[str, List[Dict[str, Any]]] = {}
    for b, runs in per_band.items():
        runs.sort(key=lambda r: (-r.days, r.ticker))
        out[b] = [
            {"ticker": r.ticker, "days": r.days, "start": r.start, "end": r.end}
            for r in runs[:top]
        ]
    return {"top": top, "by_band": out}


# ---- CLI -----------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-root", default=str(_default_data_root()),
                   help="path to data/lthcs/ root (default: repo data dir)")
    sub = p.add_subparsers(dest="mode", required=True)

    pt = sub.add_parser("ticker", help="full composite history for one ticker")
    pt.add_argument("ticker")

    pb = sub.add_parser("band", help="tickers that hit a band in a window")
    pb.add_argument("band", choices=BANDS)
    pb.add_argument("--window", default="30", help="7 | 30 | 90 | all (default: 30)")

    ps = sub.add_parser("streak", help="top-N longest streaks per band")
    ps.add_argument("--top", type=int, default=10)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    data_root = Path(args.data_root)

    if args.mode == "ticker":
        result = search_by_ticker(args.ticker, data_root)
    elif args.mode == "band":
        result = search_by_band(args.band, args.window, data_root)
    elif args.mode == "streak":
        result = search_by_streak(args.top, data_root)
    else:
        parser.print_help()
        return 2

    json.dump(result, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
