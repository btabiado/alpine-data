#!/usr/bin/env python3
"""LTHCS daily Google Trends nudge.

Phase 2 of the LTHCS Adoption pillar's Google Trends coverage push.

Background
----------
The Phase 1 weekly batch (:mod:`scripts.lthcs_trends_weekly`) tries to
fetch all 167 tickers in one ~11-minute pass. In practice Google's
pytrends limiter only tolerates a tiny burst (~10 tickers/min) before
hitting a 429 wall whose cooldown stretches for hours. The post-Phase 1
audit shows only ~11/167 active tickers carry trends data because of
this.

Phase 2's answer is to **spread the work across the week**: process
a small slice (~30 tickers) per day with aggressive backoff +
resumable progress. Run daily at 04:00 UTC for 5-6 days and the
universe gets fully refreshed without ever hitting a sustained 429.

Concretely this script:

1. Reads the current ISO week's aggregated snapshot
   (``data/lthcs/trends/<YYYY-Www>.json``) if present.
2. Computes the candidate set = universe tickers not in the snapshot,
   or whose ``fetched_at`` is older than ``--stale-after-days`` (5 by
   default = a rolling refresh window).
3. Loads ``.cache/lthcs/trends_progress.json`` (today-keyed) so that
   if yesterday/this-morning's run died mid-way, this run picks up
   without redoing successful tickers.
4. Caps the slice at ``--batch-size`` (default 30 tickers) and fetches
   each with an **adaptive sleep cadence**:
   - Base sleep: ``--sleep-base`` (default 12 s = 5 req/min, ~half
     pytrends' historical 429 ceiling).
   - On 429 / rate-limit error: exponentially back off — base, 2x,
     4x, ... up to ``--max-backoff`` (default 300 s = 5 min) — and
     hold that elevated cadence until the next successful fetch.
   - On success after a backoff: snap back to base.
5. Merges the new fetches into the aggregated snapshot (additive: never
   removes existing tickers from the snapshot) and writes the
   progress file before exit so a re-run resumes cleanly.

The script is deliberately defensive: if every ticker in the slice
returns 429, it exits 0 (not failure) and logs a warning. The next
day's run will retry the same slice with a fresh per-day progress
file. Persistent 429s are the signal to fall back to SerpAPI — see
``data/lthcs/trends/README.md`` for the trade-off matrix.

Usage::

    # Default: today's slice (~30 tickers), 5-day stale TTL, base 12 s
    python scripts/lthcs_trends_daily.py

    # Larger slice for a manual catch-up
    python scripts/lthcs_trends_daily.py --batch-size 60

    # Force refresh — ignore stale-TTL and re-fetch everything in slice
    python scripts/lthcs_trends_daily.py --force

    # Dry-run (no network); shows which tickers would be picked
    python scripts/lthcs_trends_daily.py --dry-run

This script never deletes anything from the snapshot; it only adds
or refreshes. NOT part of the live 23:00 UTC daily pipeline — runs on
its own cron earlier in the day.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import random
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Make the project root importable when running as a standalone script.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Reuse helpers from the weekly script so behaviour stays consistent.
from scripts.lthcs_trends_weekly import (  # noqa: E402
    _iso_week_str,
    _is_rate_limit_error,
    _per_ticker_cache_path,
    _read_per_ticker_cache,
    _write_per_ticker_cache,
    fetch_one_live,
    load_universe,
)
from lthcs.sources.google_trends import resolve_search_term  # noqa: E402


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Adaptive sleep parameters — see module docstring for the full story.
_DEFAULT_SLEEP_BASE_SECONDS = 12.0       # ~5 req/min steady state
_DEFAULT_MAX_BACKOFF_SECONDS = 300.0     # 5 min cap on backoff
_DEFAULT_JITTER_SECONDS = 2.0            # +/- jitter to dodge synchronised spikes

# Slice sizing: 167 tickers / 5.5 days ~= 30 tickers/day fully refreshes
# the universe over a working week. Tunable for catch-up runs.
_DEFAULT_BATCH_SIZE = 30

# How long a per-ticker entry stays "fresh" before being re-eligible.
# 5 days lines up with the daily-nudge cadence: a ticker fetched Mon
# stays fresh through Fri, becomes eligible again on Sat.
_DEFAULT_STALE_AFTER_DAYS = 5

# Per-ticker pytrends retries (within fetch_one_live) before giving up
# on this ticker for the run. The slice's adaptive cadence layers ON
# TOP of these inner retries — so a single 429 here only tanks the
# next ticker's pacing, not the whole batch.
_DEFAULT_MAX_RETRIES = 2

_DEFAULT_TIMEFRAME = "today 5-y"
_DEFAULT_GEO = ""

_DEFAULT_UNIVERSE_PATH = Path("data/lthcs/universe.json")
_DEFAULT_DATA_ROOT = Path("data/lthcs")
_DEFAULT_CACHE_ROOT = Path(".cache/lthcs/google_trends")
_DEFAULT_PROGRESS_PATH = Path(".cache/lthcs/trends_progress.json")


logger = logging.getLogger("lthcs_trends_daily")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lthcs_trends_daily",
        description=(
            "Daily Google Trends nudge: fetch a small slice of the LTHCS "
            "universe with adaptive backoff and resumable progress."
        ),
    )
    p.add_argument(
        "--week",
        type=str,
        default=None,
        help="ISO week of the target snapshot (YYYY-Www). Default: current ISO week.",
    )
    p.add_argument(
        "--tickers",
        type=str,
        default=None,
        help=(
            "Comma-separated subset of tickers (default: auto-pick stale/missing "
            "tickers from universe.json up to --batch-size)."
        ),
    )
    p.add_argument(
        "--universe",
        type=Path,
        default=_DEFAULT_UNIVERSE_PATH,
        help="Path to universe.json (default: data/lthcs/universe.json).",
    )
    p.add_argument(
        "--data-root",
        type=Path,
        default=_DEFAULT_DATA_ROOT,
        help="Root for aggregated snapshot output (default: data/lthcs/).",
    )
    p.add_argument(
        "--cache-root",
        type=Path,
        default=_DEFAULT_CACHE_ROOT,
        help="Root for per-ticker raw cache (default: .cache/lthcs/google_trends/).",
    )
    p.add_argument(
        "--progress-path",
        type=Path,
        default=_DEFAULT_PROGRESS_PATH,
        help=(
            "Where to persist resumable per-day progress "
            "(default: .cache/lthcs/trends_progress.json)."
        ),
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=_DEFAULT_BATCH_SIZE,
        help=f"Max tickers to process this run (default: {_DEFAULT_BATCH_SIZE}).",
    )
    p.add_argument(
        "--stale-after-days",
        type=int,
        default=_DEFAULT_STALE_AFTER_DAYS,
        help=(
            "Re-fetch tickers whose snapshot fetched_at is older than this many "
            f"days (default: {_DEFAULT_STALE_AFTER_DAYS})."
        ),
    )
    p.add_argument(
        "--sleep-base",
        type=float,
        default=_DEFAULT_SLEEP_BASE_SECONDS,
        help=(
            "Base sleep between successful fetches in seconds "
            f"(default: {_DEFAULT_SLEEP_BASE_SECONDS})."
        ),
    )
    p.add_argument(
        "--max-backoff",
        type=float,
        default=_DEFAULT_MAX_BACKOFF_SECONDS,
        help=(
            "Cap on the adaptive backoff sleep in seconds "
            f"(default: {_DEFAULT_MAX_BACKOFF_SECONDS})."
        ),
    )
    p.add_argument(
        "--jitter",
        type=float,
        default=_DEFAULT_JITTER_SECONDS,
        help=f"+/-Jitter on adaptive sleep (default: {_DEFAULT_JITTER_SECONDS}).",
    )
    p.add_argument(
        "--max-retries",
        type=int,
        default=_DEFAULT_MAX_RETRIES,
        help=(
            "Inner per-ticker retries (passed to fetch_one_live, "
            f"default: {_DEFAULT_MAX_RETRIES})."
        ),
    )
    p.add_argument(
        "--timeframe",
        type=str,
        default=_DEFAULT_TIMEFRAME,
        help='pytrends timeframe (default: "today 5-y").',
    )
    p.add_argument(
        "--geo",
        type=str,
        default=_DEFAULT_GEO,
        help='pytrends geo string (default: "" = worldwide).',
    )
    p.add_argument(
        "--force",
        action="store_true",
        help=(
            "Ignore stale-TTL: re-fetch every ticker in the candidate slice, "
            "even if it has a fresh entry in the snapshot."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be fetched. No network calls.",
    )
    p.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        help="Logging level (DEBUG, INFO, WARNING, ERROR).",
    )
    return p


# ---------------------------------------------------------------------------
# Snapshot I/O
# ---------------------------------------------------------------------------


def _snapshot_path(data_root: Path, week: str) -> Path:
    return Path(data_root) / "trends" / f"{week}.json"


def _read_snapshot(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        logger.warning("Snapshot at %s is unreadable; treating as empty.", path)
        return None


def _write_snapshot(path: Path, snapshot: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, indent=2)
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Progress file (resumable)
# ---------------------------------------------------------------------------


def _today_key() -> str:
    return _dt.date.today().isoformat()


def read_progress(
    progress_path: Path,
    today: Optional[str] = None,
) -> Dict[str, Any]:
    """Read the resumable progress file for today.

    Schema::

        {
          "date": "YYYY-MM-DD",
          "completed": ["AAPL", "MSFT", ...],
          "failures": ["TSLA", ...]
        }

    If the file is missing, unparseable, or its ``date`` field doesn't
    match today's date, return a fresh empty progress dict — yesterday's
    progress is meaningless because the next day's slice should reset.
    """
    today_key = today or _today_key()
    if not progress_path.exists():
        return {"date": today_key, "completed": [], "failures": []}
    try:
        with progress_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        logger.warning(
            "Progress file at %s is unreadable; starting fresh.", progress_path,
        )
        return {"date": today_key, "completed": [], "failures": []}
    if not isinstance(data, dict):
        return {"date": today_key, "completed": [], "failures": []}
    if data.get("date") != today_key:
        logger.info(
            "Progress file dates %s != today %s; resetting.",
            data.get("date"), today_key,
        )
        return {"date": today_key, "completed": [], "failures": []}
    return {
        "date": today_key,
        "completed": [
            t for t in data.get("completed", []) if isinstance(t, str)
        ],
        "failures": [
            t for t in data.get("failures", []) if isinstance(t, str)
        ],
    }


def write_progress(progress_path: Path, progress: Dict[str, Any]) -> None:
    """Persist the in-flight progress dict atomically."""
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = progress_path.with_suffix(progress_path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(progress, fh, indent=2)
    tmp.replace(progress_path)


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------


def _parse_fetched_at(value: Any) -> Optional[_dt.datetime]:
    if not isinstance(value, str):
        return None
    try:
        # ``datetime.fromisoformat`` in 3.9 doesn't accept "Z" suffix; strip.
        cleaned = value.rstrip("Z")
        return _dt.datetime.fromisoformat(cleaned)
    except (TypeError, ValueError):
        return None


def select_candidates(
    universe: List[str],
    snapshot: Optional[Dict[str, Any]],
    *,
    stale_after_days: int,
    batch_size: int,
    completed: List[str],
    now: Optional[_dt.datetime] = None,
    force: bool = False,
) -> List[str]:
    """Pick the slice to fetch this run.

    Priority order (first wins):
    1. Tickers missing entirely from ``snapshot.tickers``.
    2. Tickers whose snapshot entry is older than ``stale_after_days``.
    3. (force only) Anyone else, in universe order.

    Tickers already in ``completed`` (today's progress file) are
    skipped — they're either done or in-flight from an earlier
    invocation today.

    The returned list is bounded by ``batch_size``.
    """
    if batch_size <= 0:
        return []

    now = now or _dt.datetime.now(_dt.timezone.utc)
    cutoff = now - _dt.timedelta(days=max(0, stale_after_days))

    snapshot_tickers: Dict[str, Any] = {}
    if snapshot and isinstance(snapshot.get("tickers"), dict):
        snapshot_tickers = snapshot["tickers"]

    completed_set = {t.upper() for t in completed if isinstance(t, str)}

    missing: List[str] = []
    stale: List[str] = []
    fresh: List[str] = []

    for ticker in universe:
        norm = ticker.upper()
        if norm in completed_set:
            continue
        entry = snapshot_tickers.get(norm)
        if not isinstance(entry, dict) or not entry.get("series"):
            missing.append(norm)
            continue
        fetched_dt = _parse_fetched_at(entry.get("fetched_at"))
        if fetched_dt is None:
            # No fetched_at recorded — treat as stale to refresh metadata.
            stale.append(norm)
            continue
        # Compare in UTC; if fetched_dt is naive, assume UTC.
        if fetched_dt.tzinfo is None:
            fetched_dt = fetched_dt.replace(tzinfo=_dt.timezone.utc)
        if fetched_dt < cutoff:
            stale.append(norm)
        else:
            fresh.append(norm)

    if force:
        ordered = missing + stale + fresh
    else:
        ordered = missing + stale

    return ordered[:batch_size]


# ---------------------------------------------------------------------------
# Adaptive sleep controller
# ---------------------------------------------------------------------------


class AdaptiveSleeper:
    """Adaptive sleep cadence with exponential backoff on rate-limit hits.

    Steady-state: sleep ``base`` between successful fetches.

    On a rate-limit notification (``on_rate_limit()``), the next sleep
    doubles, then doubles again, etc., capped at ``max_backoff``. A
    successful fetch (``on_success()``) snaps the cadence back to base.

    Jitter is added on every sleep to dodge synchronisation with
    Google's limiter, which appears to bucket requests at second
    boundaries.

    The class is deliberately mockable: pass a custom ``sleep_fn`` for
    tests (no real time.sleep).
    """

    def __init__(
        self,
        base: float,
        max_backoff: float,
        *,
        jitter: float = 0.0,
        sleep_fn: Any = time.sleep,
        rng: Optional[random.Random] = None,
    ) -> None:
        if base < 0:
            raise ValueError("base must be >= 0")
        if max_backoff < base:
            raise ValueError("max_backoff must be >= base")
        self._base = float(base)
        self._max_backoff = float(max_backoff)
        self._jitter = float(max(0.0, jitter))
        self._current = float(base)
        self._sleep_fn = sleep_fn
        self._rng = rng or random.Random()

    @property
    def current(self) -> float:
        """Current cadence in seconds (visible for logging/tests)."""
        return self._current

    def on_success(self) -> None:
        """Reset cadence to base after a successful fetch."""
        self._current = self._base

    def on_rate_limit(self) -> None:
        """Double cadence (capped at max_backoff) after a 429-like hit."""
        # First rate-limit doubles from base; subsequent ones keep doubling.
        if self._current < self._base or self._current == 0:
            self._current = self._base if self._base > 0 else 1.0
        nxt = self._current * 2.0
        self._current = min(nxt, self._max_backoff)

    def sleep(self) -> float:
        """Sleep ``current + jitter`` seconds. Returns the actual wait."""
        if self._current <= 0:
            return 0.0
        delta = (
            self._rng.uniform(-self._jitter, self._jitter)
            if self._jitter > 0
            else 0.0
        )
        wait = max(0.0, self._current + delta)
        self._sleep_fn(wait)
        return wait


# ---------------------------------------------------------------------------
# Live fetch with rate-limit detection
# ---------------------------------------------------------------------------


def _fetch_with_rate_limit_signal(
    ticker: str,
    term: str,
    timeframe: str,
    geo: str,
    *,
    max_retries: int,
    trend_req_factory: Any = None,
) -> Tuple[Optional[List[float]], bool]:
    """Wrap :func:`fetch_one_live` and surface whether 429 was observed.

    Returns ``(series_or_none, rate_limit_observed)``. The
    rate-limit flag is the signal the adaptive sleeper uses to back
    off — fetch_one_live itself doesn't propagate it.

    The detection path: when fetch_one_live exhausts retries on a
    429-like error it logs ``"giving up after"`` and returns None,
    but we can't intercept that. So we wrap the trend_req_factory in
    a tiny shim that records the last exception observed.
    """
    last_exc: Dict[str, BaseException] = {}

    if trend_req_factory is None:
        try:
            from pytrends.request import TrendReq

            base_factory = lambda: TrendReq(hl="en-US", tz=0)  # noqa: E731
        except Exception as exc:  # pragma: no cover - import shim
            logger.error("pytrends not importable: %s", exc)
            return None, False
    else:
        base_factory = trend_req_factory

    def _wrapped_factory() -> Any:
        inst = base_factory()
        original_build = inst.build_payload

        def _shim_build(*args: Any, **kwargs: Any) -> Any:
            try:
                return original_build(*args, **kwargs)
            except BaseException as exc:
                last_exc["e"] = exc
                raise

        inst.build_payload = _shim_build  # type: ignore[assignment]
        return inst

    series = fetch_one_live(
        ticker, term, timeframe, geo,
        max_retries=max_retries,
        trend_req_factory=_wrapped_factory,
    )
    rate_limited = "e" in last_exc and _is_rate_limit_error(last_exc["e"])
    return series, rate_limited


# ---------------------------------------------------------------------------
# Main batch driver
# ---------------------------------------------------------------------------


def run_daily_batch(
    args: argparse.Namespace,
    trend_req_factory: Any = None,
    sleeper: Optional[AdaptiveSleeper] = None,
    *,
    now: Optional[_dt.datetime] = None,
) -> Dict[str, Any]:
    """Execute the daily nudge and return the updated snapshot dict.

    ``trend_req_factory`` and ``sleeper`` are injection points for tests
    (no live network, no real sleeps).
    """
    week = args.week or _iso_week_str()
    as_of = _dt.date.today().isoformat()
    today = (now or _dt.datetime.now(_dt.timezone.utc)).date().isoformat()

    # 1. Universe
    if args.tickers:
        universe = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        universe = load_universe(args.universe)
    if not universe:
        logger.warning("Empty universe. Nothing to do.")
        return {"week": week, "as_of": as_of, "term_map": {}, "tickers": {}}

    # 2. Existing snapshot (additive merge target)
    snap_path = _snapshot_path(args.data_root, week)
    snapshot = _read_snapshot(snap_path) or {
        "week": week,
        "as_of": as_of,
        "term_map": {},
        "tickers": {},
    }
    snapshot.setdefault("term_map", {})
    snapshot.setdefault("tickers", {})
    snapshot["as_of"] = as_of  # bump on every run
    snapshot["week"] = week

    # 3. Progress (resume)
    progress = read_progress(args.progress_path, today=today)
    completed = list(progress.get("completed", []))
    failures = list(progress.get("failures", []))

    # 4. Candidate slice
    candidates = select_candidates(
        universe,
        snapshot,
        stale_after_days=args.stale_after_days,
        batch_size=args.batch_size,
        completed=completed,
        now=now,
        force=args.force,
    )

    if not candidates:
        logger.info(
            "Nothing to do: 0 candidates (universe=%d, already-completed=%d, "
            "snapshot-fresh=%d).",
            len(universe), len(completed),
            sum(
                1 for t in universe
                if isinstance(snapshot["tickers"].get(t.upper()), dict)
                and snapshot["tickers"][t.upper()].get("series")
            ),
        )
        return snapshot

    logger.info(
        "Starting daily nudge | week=%s | universe=%d | slice=%d | "
        "stale_after_days=%d | sleep_base=%.0fs | max_backoff=%.0fs | "
        "force=%s | dry_run=%s",
        week, len(universe), len(candidates), args.stale_after_days,
        args.sleep_base, args.max_backoff, args.force, args.dry_run,
    )

    if sleeper is None:
        sleeper = AdaptiveSleeper(
            base=args.sleep_base,
            max_backoff=args.max_backoff,
            jitter=args.jitter,
        )

    rate_limit_hits = 0
    new_fetches = 0
    consecutive_rate_limits = 0
    # If every fetch in a window of N consecutive tickers 429s, bail —
    # pytrends has put us in penalty box; better to ship what we have.
    _CONSECUTIVE_429_BAIL = 5

    for idx, ticker in enumerate(candidates, start=1):
        term = resolve_search_term(ticker)
        snapshot["term_map"][ticker] = term

        if args.dry_run:
            logger.info("DRY-RUN [%d/%d] would fetch %s (term=%r)", idx, len(candidates), ticker, term)
            continue

        series, rate_limited = _fetch_with_rate_limit_signal(
            ticker, term, args.timeframe, args.geo,
            max_retries=args.max_retries,
            trend_req_factory=trend_req_factory,
        )

        if series is None:
            if rate_limited:
                rate_limit_hits += 1
                consecutive_rate_limits += 1
                sleeper.on_rate_limit()
                logger.warning(
                    "ticker=%s: rate-limited; cadence now %.0fs (consecutive_429=%d)",
                    ticker, sleeper.current, consecutive_rate_limits,
                )
            else:
                logger.warning("ticker=%s: fetch failed (non-429)", ticker)
            if ticker not in failures:
                failures.append(ticker)
            # Persist progress before we sleep so a Ctrl-C is safe.
            progress = {"date": today, "completed": completed, "failures": failures}
            write_progress(args.progress_path, progress)

            if consecutive_rate_limits >= _CONSECUTIVE_429_BAIL:
                logger.warning(
                    "Hit %d consecutive 429s; bailing on this run to avoid a "
                    "longer cooldown. %d/%d candidates processed.",
                    consecutive_rate_limits, idx, len(candidates),
                )
                break
            if idx < len(candidates):
                sleeper.sleep()
            continue

        # Success path
        new_fetches += 1
        consecutive_rate_limits = 0
        sleeper.on_success()
        snapshot["tickers"][ticker] = {"series": series, "term": term}
        # Also stamp fetched_at in the snapshot blob so the stale-TTL
        # check on the next run can read it without round-tripping through
        # the per-ticker cache file (which is gitignored).
        snapshot["tickers"][ticker]["fetched_at"] = _dt.datetime.now(
            _dt.timezone.utc
        ).isoformat()

        # Also write the per-ticker cache file (interop with weekly script).
        cache_path = _per_ticker_cache_path(args.cache_root, ticker, week)
        try:
            _write_per_ticker_cache(
                cache_path,
                {
                    "ticker": ticker,
                    "term": term,
                    "week": week,
                    "fetched_at": snapshot["tickers"][ticker]["fetched_at"],
                    "timeframe": args.timeframe,
                    "geo": args.geo,
                    "series": series,
                },
            )
        except OSError as exc:  # pragma: no cover
            logger.warning("ticker=%s: per-ticker cache write failed: %s", ticker, exc)

        if ticker not in completed:
            completed.append(ticker)
        if ticker in failures:
            failures.remove(ticker)

        # Persist BOTH progress + snapshot after every success so a
        # subsequent crash doesn't lose the work.
        progress = {"date": today, "completed": completed, "failures": failures}
        write_progress(args.progress_path, progress)
        _write_snapshot(snap_path, snapshot)

        if idx < len(candidates):
            sleeper.sleep()

    logger.info(
        "Done. fetched=%d rate_limit_hits=%d completed_today=%d failed_today=%d",
        new_fetches, rate_limit_hits, len(completed), len(failures),
    )
    if args.dry_run:
        logger.info("DRY-RUN: no snapshot/progress writes performed.")
    return snapshot


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_argparser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.log_level.upper() != "DEBUG":
        logging.getLogger("urllib3").setLevel(logging.WARNING)

    try:
        run_daily_batch(args)
    except Exception as exc:  # noqa: BLE001
        logger.error("Daily nudge failed: %s", exc, exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
