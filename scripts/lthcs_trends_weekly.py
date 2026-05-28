#!/usr/bin/env python3
"""LTHCS weekly Google Trends batch.

The LTHCS daily pipeline cannot afford to query Google Trends directly:
pytrends is rate-limited so aggressively that we get 429s within
minutes of starting a 168-ticker pass. This script runs offline,
slowly (~4 s/ticker, exponential backoff on 429), and writes a single
aggregated snapshot the daily pipeline can read for free::

    data/lthcs/trends/<YYYY-Www>.json

The aggregated snapshot has shape::

    {
      "week": "YYYY-Www",
      "as_of": "YYYY-MM-DD",
      "term_map": {"AAPL": "/m/0k8z", "ZTS": "ZTS stock", ...},
      "tickers": {
        "AAPL": {"series": [38, 42, 47, ..., 81], "term": "/m/0k8z"},
        ...
      }
    }

Per-ticker raw pytrends responses are cached at
``.cache/lthcs/google_trends/<TICKER>_<YYYY-Www>.json`` so a re-run
restarts from where it left off.

Run weekly (cron or manually). NOT part of the daily pipeline.

Usage::

    # Default: current ISO week, full universe
    python scripts/lthcs_trends_weekly.py

    # Subset for testing
    python scripts/lthcs_trends_weekly.py --tickers AAPL,MSFT,NVDA

    # Dry-run (no network)
    python scripts/lthcs_trends_weekly.py --dry-run

    # Specific week
    python scripts/lthcs_trends_weekly.py --week 2026-W19

    # Force refresh (re-fetch even if cached)
    python scripts/lthcs_trends_weekly.py --force
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import random
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# Make the project root importable when running as a standalone script.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from lthcs.sources.google_trends import resolve_search_term


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Polite cadence: ~4 s between tickers with +/-1 s jitter. 168 tickers
# at 4 s = ~11 minutes per pass, which empirically stays below Google's
# soft rate limit. Adjustable via ``--cadence``.
_DEFAULT_CADENCE_SECONDS = 4.0
_DEFAULT_JITTER_SECONDS = 1.0

# Exponential backoff base + ceiling for 429 / network errors.
_BACKOFF_BASE_SECONDS = 30.0
_BACKOFF_MAX_SECONDS = 600.0

_PROGRESS_EVERY = 10  # log every N tickers

_DEFAULT_TIMEFRAME = "today 5-y"  # 5-year horizon, weekly granularity
_DEFAULT_GEO = ""  # worldwide (US-skewed for these tickers anyway)

_DEFAULT_UNIVERSE_PATH = Path("data/lthcs/universe.json")
_DEFAULT_DATA_ROOT = Path("data/lthcs")
_DEFAULT_CACHE_ROOT = Path(".cache/lthcs/google_trends")


logger = logging.getLogger("lthcs_trends_weekly")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _iso_week_str(d: Optional[_dt.date] = None) -> str:
    d = d or _dt.date.today()
    iso = d.isocalendar()
    return f"{iso[0]:04d}-W{iso[1]:02d}"


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lthcs_trends_weekly",
        description="Weekly Google Trends batch for LTHCS Adoption pillar.",
    )
    p.add_argument(
        "--week",
        type=str,
        default=None,
        help="ISO week to label this snapshot (YYYY-Www). Default: current ISO week.",
    )
    p.add_argument(
        "--tickers",
        type=str,
        default=None,
        help="Comma-separated subset of tickers (default: all active from universe.json).",
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
        "--force",
        action="store_true",
        help="Re-fetch even if a per-ticker cache file exists.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be fetched. No network calls.",
    )
    p.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Max retries per ticker on 429/network error (default: 3).",
    )
    p.add_argument(
        "--cadence",
        type=float,
        default=_DEFAULT_CADENCE_SECONDS,
        help=f"Base sleep between tickers in seconds (default: {_DEFAULT_CADENCE_SECONDS}).",
    )
    p.add_argument(
        "--jitter",
        type=float,
        default=_DEFAULT_JITTER_SECONDS,
        help=f"+/-Jitter on cadence in seconds (default: {_DEFAULT_JITTER_SECONDS}).",
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
        "--log-level",
        type=str,
        default="INFO",
        help="Logging level (DEBUG, INFO, WARNING, ERROR).",
    )
    return p


# ---------------------------------------------------------------------------
# Universe loader
# ---------------------------------------------------------------------------


def load_universe(path: Path) -> List[str]:
    """Read the active-ticker list from ``universe.json``."""
    if not path.exists():
        raise FileNotFoundError(f"universe.json not found at {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    rows = data.get("tickers") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError(f"{path} missing 'tickers' list")
    out: List[str] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        if not r.get("active", True):
            continue
        t = r.get("ticker")
        if isinstance(t, str) and t.strip():
            out.append(t.strip().upper())
    return out


# ---------------------------------------------------------------------------
# Per-ticker cache
# ---------------------------------------------------------------------------


def _per_ticker_cache_path(cache_root: Path, ticker: str, week: str) -> Path:
    cache_root.mkdir(parents=True, exist_ok=True)
    return cache_root / f"{ticker}_{week}.json"


def _read_per_ticker_cache(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _write_per_ticker_cache(path: Path, payload: Dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Fetcher (live + dry-run)
# ---------------------------------------------------------------------------


def _is_rate_limit_error(exc: BaseException) -> bool:
    """Heuristic: pytrends raises ``ResponseError``/``TooManyRequestsError``
    on 429, but the exact class varies by version. Fall back to string-match.
    """
    name = type(exc).__name__.lower()
    if "toomany" in name or "429" in name or "rate" in name:
        return True
    text = str(exc).lower()
    return "429" in text or "too many" in text


def fetch_one_live(
    ticker: str,
    term: str,
    timeframe: str,
    geo: str,
    *,
    max_retries: int,
    trend_req_factory: Any = None,
) -> Optional[List[float]]:
    """Fetch one ticker's weekly interest series via pytrends.

    Returns the list of weekly interest values (oldest first), or None
    on hard failure. Retries with exponential backoff on 429.
    """
    if trend_req_factory is None:  # pragma: no cover - import shim
        try:
            from pytrends.request import TrendReq

            trend_req_factory = lambda: TrendReq(hl="en-US", tz=0)  # noqa: E731
        except Exception as exc:
            logger.error("pytrends not importable: %s", exc)
            return None

    attempt = 0
    while True:
        try:
            pytrends = trend_req_factory()
            pytrends.build_payload([term], timeframe=timeframe, geo=geo or "")
            df = pytrends.interest_over_time()
        except Exception as exc:  # noqa: BLE001
            attempt += 1
            if attempt > max_retries:
                logger.warning(
                    "ticker=%s term=%s: giving up after %d retries (%s)",
                    ticker, term, max_retries, exc,
                )
                return None
            if _is_rate_limit_error(exc):
                wait = min(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), _BACKOFF_MAX_SECONDS)
                logger.warning(
                    "ticker=%s term=%s: 429-like error; backoff %.0fs (retry %d/%d): %s",
                    ticker, term, wait, attempt, max_retries, exc,
                )
                time.sleep(wait)
                continue
            logger.warning(
                "ticker=%s term=%s: transient error (retry %d/%d): %s",
                ticker, term, attempt, max_retries, exc,
            )
            time.sleep(min(5.0 * attempt, 60.0))
            continue

        if df is None:
            return None
        try:
            if df.empty:
                return []
        except Exception:
            return None
        col = term if term in df.columns else None
        if col is None and len(df.columns):
            # pytrends sometimes renames topic IDs; pick the first non-isPartial column.
            for c in df.columns:
                if c != "isPartial":
                    col = c
                    break
        if col is None:
            return []
        try:
            return [float(v) for v in df[col].tolist()]
        except (TypeError, ValueError):
            return []


# ---------------------------------------------------------------------------
# Main batch driver
# ---------------------------------------------------------------------------


def _sleep_with_jitter(base: float, jitter: float) -> None:
    if base <= 0:
        return
    delta = random.uniform(-jitter, jitter) if jitter > 0 else 0.0
    time.sleep(max(0.0, base + delta))


def _format_eta(remaining: int, cadence: float) -> str:
    secs = int(remaining * cadence)
    mins = secs // 60
    if mins >= 1:
        return f"~{mins}min @ {cadence:.0f}s/ticker"
    return f"~{secs}s @ {cadence:.0f}s/ticker"


def run_batch(args: argparse.Namespace, trend_req_factory: Any = None) -> Dict[str, Any]:
    """Execute the weekly batch and return the aggregated snapshot dict.

    When ``args.dry_run`` is true, no network calls happen and the
    returned snapshot has empty per-ticker series.
    """
    week = args.week or _iso_week_str()
    as_of = _dt.date.today().isoformat()

    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        tickers = load_universe(args.universe)

    if not tickers:
        logger.warning("No tickers to process. Exiting.")
        return {"week": week, "as_of": as_of, "term_map": {}, "tickers": {}}

    logger.info(
        "Starting trends batch | week=%s | tickers=%d | dry_run=%s | timeframe=%s | geo=%r",
        week, len(tickers), args.dry_run, args.timeframe, args.geo,
    )
    if args.dry_run:
        logger.info("DRY-RUN: no live pytrends calls will be made.")
    total = len(tickers)
    eta = _format_eta(total, args.cadence)
    logger.info("Estimated total runtime: %s", eta)

    term_map: Dict[str, str] = {}
    series_map: Dict[str, Dict[str, Any]] = {}

    processed_live = 0
    cache_hits = 0
    failures = 0

    for idx, ticker in enumerate(tickers, start=1):
        term = resolve_search_term(ticker)
        term_map[ticker] = term

        cache_path = _per_ticker_cache_path(args.cache_root, ticker, week)
        cached = None if args.force else _read_per_ticker_cache(cache_path)
        if cached and isinstance(cached.get("series"), list):
            cache_hits += 1
            series_map[ticker] = {
                "series": [float(v) for v in cached["series"]],
                "term": cached.get("term", term),
            }
            if idx % _PROGRESS_EVERY == 0 or idx == total:
                _log_progress(idx, total, args.cadence, "cache-hit")
            continue

        if args.dry_run:
            logger.debug(
                "DRY-RUN [%d/%d] ticker=%s term=%r (would fetch)",
                idx, total, ticker, term,
            )
            series_map[ticker] = {"series": [], "term": term, "dry_run": True}
            if idx % _PROGRESS_EVERY == 0 or idx == total:
                _log_progress(idx, total, args.cadence, "dry-run")
            continue

        series = fetch_one_live(
            ticker, term, args.timeframe, args.geo,
            max_retries=args.max_retries,
            trend_req_factory=trend_req_factory,
        )

        if series is None:
            failures += 1
            logger.warning("ticker=%s: fetch failed; will retry next week.", ticker)
        else:
            processed_live += 1
            payload = {
                "ticker": ticker,
                "term": term,
                "week": week,
                "fetched_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                "timeframe": args.timeframe,
                "geo": args.geo,
                "series": series,
            }
            try:
                _write_per_ticker_cache(cache_path, payload)
            except OSError as exc:  # pragma: no cover
                logger.warning("ticker=%s: cache write failed: %s", ticker, exc)
            series_map[ticker] = {"series": series, "term": term}

        if idx % _PROGRESS_EVERY == 0 or idx == total:
            _log_progress(idx, total, args.cadence, "live")

        # Polite cadence between tickers (skip on last).
        if idx < total:
            _sleep_with_jitter(args.cadence, args.jitter)

    snapshot = {
        "week": week,
        "as_of": as_of,
        "term_map": term_map,
        "tickers": series_map,
    }

    # Aggregated snapshot output
    trends_dir = Path(args.data_root) / "trends"
    trends_dir.mkdir(parents=True, exist_ok=True)
    snap_path = trends_dir / f"{week}.json"
    if not args.dry_run:
        tmp = snap_path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, indent=2)
        tmp.replace(snap_path)
        logger.info("Wrote aggregated snapshot: %s", snap_path)
    else:
        logger.info("DRY-RUN: would write snapshot to %s", snap_path)

    logger.info(
        "Done. live=%d cache-hits=%d failures=%d total=%d",
        processed_live, cache_hits, failures, total,
    )
    return snapshot


def _log_progress(idx: int, total: int, cadence: float, source: str) -> None:
    remaining = max(0, total - idx)
    eta = _format_eta(remaining, cadence)
    logger.info(
        "Processed %d/%d tickers (%s), %d remaining (%s)",
        idx, total, source, remaining, eta,
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_argparser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Reduce pytrends/urllib3 noise unless DEBUG.
    if args.log_level.upper() != "DEBUG":
        logging.getLogger("urllib3").setLevel(logging.WARNING)

    try:
        run_batch(args)
    except Exception as exc:  # noqa: BLE001
        logger.error("Batch failed: %s", exc, exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
