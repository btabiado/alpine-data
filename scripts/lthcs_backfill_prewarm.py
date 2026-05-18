#!/usr/bin/env python3
"""LTHCS backfill pre-warmer.

Pre-warms all source caches with historical data BEFORE the 90-day
``lthcs_backfill.py`` loop runs. The backfill orchestrator otherwise
makes ~90 per-date source calls (one per ticker per date), and most of
the data it needs is a slice of a single big historical pull. Yahoo's
``Ticker.history(period="2y")`` returns 2 years of prices in one call —
every backfill date can be served from that one cache entry. Same for
FRED series, SEC filings indexes, Form 4 submissions, and Finnhub
recommendation history.

This script does the big batch fetches ONCE, populates the on-disk
caches under ``.cache/lthcs/``, and writes a tiny status file at
``data/lthcs/prewarm_status.json`` so the backfill can detect "you
haven't pre-warmed; consider running the warmer first."

Usage::

    python scripts/lthcs_backfill_prewarm.py
    python scripts/lthcs_backfill_prewarm.py --days 30
    python scripts/lthcs_backfill_prewarm.py --tickers AAPL,MSFT
    python scripts/lthcs_backfill_prewarm.py --dry-run
    python scripts/lthcs_backfill_prewarm.py --skip-sec
    python scripts/lthcs_backfill_prewarm.py --max-concurrency 8

The script writes ONLY to ``.cache/lthcs/`` plus the small
``data/lthcs/prewarm_status.json`` status file. ``git status`` after a
successful run shows nothing changed in ``data/lthcs/`` aside from the
status file (and that file is ignored in most environments — it's a
runtime artifact, not a snapshot).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# Repo-root self-locator so the script works whether invoked from cwd or via
# an absolute path. Mirrors the lthcs_daily.py / lthcs_backfill.py pattern.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_DAYS = 90
_DEFAULT_MAX_CONCURRENCY = 5

_DATA_ROOT = _REPO_ROOT / "data" / "lthcs"
_UNIVERSE_PATH = _DATA_ROOT / "universe.json"
_STATUS_PATH = _DATA_ROOT / "prewarm_status.json"

# Macro FRED series LTHCS consumes. Kept hard-coded so the warmer doesn't
# need to import the daily pipeline (which would pull in dozens of extra
# modules just to discover this list).
_FRED_SERIES = (
    "CPIAUCSL",         # CPI - All Urban Consumers
    "DGS10",            # 10-Year Treasury yield
    "FEDFUNDS",         # Effective Federal Funds Rate
    "UNRATE",           # Civilian Unemployment Rate
    "BAMLH0A0HYM2",     # ICE BofA US High Yield Index OAS
    "BAMLC0A0CM",       # ICE BofA US Corporate Index OAS
    "T10Y2Y",           # 10Y - 2Y Treasury spread
    "DTWEXBGS",         # Trade Weighted US Dollar Index
    "DFII10",           # 10Y TIPS yield (real rate)
    "VIXCLS",           # CBOE VIX
    "M2SL",             # M2 money stock
)

# Per-source signed log scale for the rough cache-footprint summary at end.
# These are intentionally generous upper bounds — the cache files are JSON,
# so the actual disk usage tends to be smaller after gzip-on-FS, but we
# want the headline number to not under-promise.
_FOOTPRINT_PER_YAHOO_KB = 700      # ~2y daily OHLCV per ticker
_FOOTPRINT_PER_FRED_KB = 250
_FOOTPRINT_PER_EIA_KB = 200
_FOOTPRINT_PER_SEC_FACTS_KB = 1200
_FOOTPRINT_PER_SEC_SUBS_KB = 350
_FOOTPRINT_PER_FINNHUB_RECO_KB = 25


# ---------------------------------------------------------------------------
# Result data classes
# ---------------------------------------------------------------------------


@dataclass
class SourceResult:
    """Per-source outcome accumulator."""

    name: str
    planned: int = 0
    live: int = 0           # fetched fresh during this run
    cached_prior: int = 0   # already in cache (no live fetch needed)
    failures: int = 0
    errors: List[str] = field(default_factory=list)
    skipped: bool = False
    duration_seconds: float = 0.0

    @property
    def total(self) -> int:
        return self.live + self.cached_prior + self.failures


@dataclass
class PrewarmReport:
    """Aggregate report across every source."""

    started_at: _dt.datetime
    finished_at: Optional[_dt.datetime] = None
    window_days: int = _DEFAULT_DAYS
    end_date: Optional[_dt.date] = None
    universe_size: int = 0
    dry_run: bool = False
    results: Dict[str, SourceResult] = field(default_factory=dict)

    def duration_seconds(self) -> float:
        if self.finished_at is None:
            return 0.0
        return (self.finished_at - self.started_at).total_seconds()

    def sources_warmed(self) -> List[str]:
        return [name for name, r in self.results.items() if not r.skipped]

    def all_errors(self) -> List[str]:
        out: List[str] = []
        for r in self.results.values():
            for e in r.errors:
                out.append(f"{r.name}: {e}")
        return out


# ---------------------------------------------------------------------------
# Universe loader
# ---------------------------------------------------------------------------


def load_universe(path: Path = _UNIVERSE_PATH) -> List[str]:
    """Return the list of active LTHCS universe tickers."""
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    tickers = raw.get("tickers", [])
    out: List[str] = []
    for entry in tickers:
        if not isinstance(entry, dict):
            continue
        if entry.get("active", True) is False:
            continue
        sym = entry.get("ticker")
        if isinstance(sym, str) and sym.strip():
            out.append(sym.strip().upper())
    return out


# ---------------------------------------------------------------------------
# Progress printing
# ---------------------------------------------------------------------------


class ProgressPrinter:
    """Tiny throttled stdout writer.

    Emits ``Source [bar] done/total (elapsed, ~remaining)`` lines. We
    intentionally do NOT use ``\r``-overwrite carriage returns — log
    aggregators / GitHub Actions render them as a single long line. Plain
    newlines are friendlier for both terminal and CI.
    """

    def __init__(self, *, throttle_seconds: float = 0.5) -> None:
        self._throttle = throttle_seconds
        self._lock = threading.Lock()
        self._last_print: Dict[str, float] = {}

    def update(
        self,
        source: str,
        done: int,
        total: int,
        *,
        started_at: float,
        force: bool = False,
    ) -> None:
        if total <= 0:
            return
        now = time.monotonic()
        with self._lock:
            last = self._last_print.get(source, 0.0)
            if not force and (now - last) < self._throttle and done < total:
                return
            self._last_print[source] = now
            elapsed = now - started_at
            bar = self._bar(done, total)
            remaining = self._estimate_remaining(done, total, elapsed)
            line = (
                f"  {source:<11} {bar} {done}/{total} "
                f"({elapsed:>5.1f}s elapsed{remaining})"
            )
            print(line, flush=True)

    @staticmethod
    def _bar(done: int, total: int, width: int = 16) -> str:
        if total <= 0:
            return "[" + (" " * width) + "]"
        pct = max(0.0, min(1.0, done / total))
        filled = int(pct * width)
        return "[" + ("#" * filled) + ("." * (width - filled)) + "]"

    @staticmethod
    def _estimate_remaining(done: int, total: int, elapsed: float) -> str:
        if done <= 0 or done >= total:
            return ""
        per_unit = elapsed / done
        remaining = per_unit * (total - done)
        return f", ~{remaining:.0f}s remaining"


# ---------------------------------------------------------------------------
# Per-source warmers
# ---------------------------------------------------------------------------


def warm_yahoo(
    tickers: List[str],
    *,
    max_concurrency: int,
    progress: ProgressPrinter,
    dry_run: bool,
) -> SourceResult:
    """Pre-warm Yahoo daily prices for every universe ticker.

    yfinance is thread-safe enough for moderate concurrency; we keep the
    pool small (default 5) because Yahoo enforces per-IP soft limits that
    are easy to trip from a single laptop.
    """
    result = SourceResult(name="yahoo", planned=len(tickers))
    if dry_run:
        return result

    # Lazy import — yfinance pulls in pandas, which is slow at import time.
    from lthcs.sources import yahoo as _yahoo

    started = time.monotonic()
    started_at = started

    def _one(ticker: str) -> Tuple[str, bool, Optional[str], bool]:
        """Returns (ticker, ok, error_message, was_cached_prior)."""
        try:
            cache_key = _yahoo._cache_key(ticker, "1y", None)  # type: ignore[attr-defined]
            prior_hit = _yahoo._cache.get(cache_key)            # type: ignore[attr-defined]
            cached_prior = prior_hit is not None
            # Always call get_daily_prices — it's a no-op on a fresh cache
            # entry and a fast slice on a stale one. This also exercises
            # the same code path the backfill loop hits.
            _yahoo.get_daily_prices(ticker, period="1y")
            return ticker, True, None, cached_prior
        except Exception as exc:  # noqa: BLE001
            return ticker, False, f"{type(exc).__name__}: {exc}", False

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, max_concurrency)) as ex:
        futures = [ex.submit(_one, t) for t in tickers]
        for fut in as_completed(futures):
            ticker, ok, err, cached_prior = fut.result()
            done += 1
            if ok:
                if cached_prior:
                    result.cached_prior += 1
                else:
                    result.live += 1
            else:
                result.failures += 1
                if err:
                    result.errors.append(f"{ticker}: {err}")
            progress.update("Yahoo", done, len(tickers), started_at=started_at)
    progress.update("Yahoo", done, len(tickers), started_at=started_at, force=True)
    result.duration_seconds = time.monotonic() - started
    return result


def warm_fred(
    series: Tuple[str, ...] = _FRED_SERIES,
    *,
    progress: Optional[ProgressPrinter] = None,
    dry_run: bool = False,
) -> SourceResult:
    """Pre-warm FRED macro series. Single-threaded — FRED is fast and the
    burst limit is generous; ~11 calls is well under any threshold."""
    result = SourceResult(name="fred", planned=len(series))
    if dry_run:
        return result

    from lthcs.sources import fred as _fred

    started = time.monotonic()
    for i, series_id in enumerate(series, start=1):
        try:
            # Check whether this series is already cached. _cache_key has
            # a stable signature we can call directly.
            cache_key = _fred._cache_key(series_id, None, None)  # type: ignore[attr-defined]
            prior_hit = _fred._cache.get(cache_key)              # type: ignore[attr-defined]
            _fred.get_series(series_id)
            if prior_hit is not None:
                result.cached_prior += 1
            else:
                result.live += 1
        except Exception as exc:  # noqa: BLE001
            result.failures += 1
            result.errors.append(f"{series_id}: {type(exc).__name__}: {exc}")
        if progress is not None:
            progress.update("FRED", i, len(series), started_at=started)
    if progress is not None:
        progress.update("FRED", len(series), len(series), started_at=started, force=True)
    result.duration_seconds = time.monotonic() - started
    return result


def warm_eia(
    *,
    progress: Optional[ProgressPrinter] = None,
    dry_run: bool = False,
) -> SourceResult:
    """Pre-warm EIA WTI series. Single call (the daily pipeline's only
    EIA dependency in the LTHCS DES pillar)."""
    result = SourceResult(name="eia", planned=1)
    if dry_run:
        return result

    from lthcs.sources import eia as _eia

    started = time.monotonic()
    try:
        _eia.get_wti()
        result.live += 1
    except Exception as exc:  # noqa: BLE001
        result.failures += 1
        result.errors.append(f"wti: {type(exc).__name__}: {exc}")
    if progress is not None:
        progress.update("EIA", 1, 1, started_at=started, force=True)
    result.duration_seconds = time.monotonic() - started
    return result


def warm_sec_edgar(
    tickers: List[str],
    *,
    progress: Optional[ProgressPrinter] = None,
    dry_run: bool = False,
) -> SourceResult:
    """Pre-warm SEC EDGAR submissions + XBRL company-facts for every ticker.

    Single-threaded by design: SEC enforces a per-User-Agent 10 req/sec
    limit. The source module's TokenBucket already enforces it, but we
    don't gain anything by parallelizing on top of a serializing rate
    limiter and we lose deterministic error reporting.
    """
    result = SourceResult(name="sec_edgar", planned=len(tickers))
    if dry_run:
        return result

    from lthcs.sources import sec_edgar as _se

    started = time.monotonic()
    for i, ticker in enumerate(tickers, start=1):
        try:
            cik = _se.get_cik(ticker)
            if cik is None:
                result.failures += 1
                result.errors.append(f"{ticker}: no CIK")
                if progress is not None:
                    progress.update(
                        "SEC EDGAR", i, len(tickers), started_at=started
                    )
                continue
            cache_key = f"company_facts/{cik}"
            prior_hit = _se._cache.get(cache_key)  # type: ignore[attr-defined]
            _se.get_company_facts(ticker)
            if prior_hit is not None:
                result.cached_prior += 1
            else:
                result.live += 1
        except Exception as exc:  # noqa: BLE001
            result.failures += 1
            result.errors.append(f"{ticker}: {type(exc).__name__}: {exc}")
        if progress is not None:
            progress.update(
                "SEC EDGAR", i, len(tickers), started_at=started
            )
    if progress is not None:
        progress.update(
            "SEC EDGAR", len(tickers), len(tickers), started_at=started, force=True
        )
    result.duration_seconds = time.monotonic() - started
    return result


def warm_sec_form4(
    tickers: List[str],
    *,
    progress: Optional[ProgressPrinter] = None,
    dry_run: bool = False,
) -> SourceResult:
    """Pre-warm SEC Form 4 submissions index for every ticker.

    We only fetch the per-CIK submissions JSON here, NOT each Form 4
    XML. The XML bodies are filing-specific and the backfill will pull
    them as needed; the index lets the loop quickly skip dates with no
    new Form 4 activity.
    """
    result = SourceResult(name="sec_form4", planned=len(tickers))
    if dry_run:
        return result

    from lthcs.sources import sec_edgar as _se
    from lthcs.sources import sec_form4 as _f4

    started = time.monotonic()
    for i, ticker in enumerate(tickers, start=1):
        try:
            cik = _se.get_cik(ticker)
            if cik is None:
                result.failures += 1
                result.errors.append(f"{ticker}: no CIK")
                if progress is not None:
                    progress.update(
                        "Form 4", i, len(tickers), started_at=started
                    )
                continue
            cache_key = f"submissions/{cik}"
            prior_hit = _f4._cache.get(cache_key)  # type: ignore[attr-defined]
            _f4._get_submissions_json(cik)         # type: ignore[attr-defined]
            if prior_hit is not None:
                result.cached_prior += 1
            else:
                result.live += 1
        except Exception as exc:  # noqa: BLE001
            result.failures += 1
            result.errors.append(f"{ticker}: {type(exc).__name__}: {exc}")
        if progress is not None:
            progress.update(
                "Form 4", i, len(tickers), started_at=started
            )
    if progress is not None:
        progress.update(
            "Form 4", len(tickers), len(tickers), started_at=started, force=True
        )
    result.duration_seconds = time.monotonic() - started
    return result


def warm_sec_13f(
    *,
    progress: Optional[ProgressPrinter] = None,
    dry_run: bool = False,
) -> SourceResult:
    """Pre-warm the 21 tracked 13F managers' submissions JSON.

    Each manager files ~4 13F-HRs/year so the submissions JSON is the
    only big upstream call; per-filing indexes are pulled on demand by
    the backfill (their 365-day TTL means the cache stays warm once
    populated).
    """
    from lthcs.sources import sec_13f as _f13

    managers = _f13.TRACKED_MANAGERS
    result = SourceResult(name="sec_13f", planned=len(managers))
    if dry_run:
        return result

    started = time.monotonic()
    for i, (name, cik) in enumerate(managers.items(), start=1):
        try:
            cache_key = f"submissions/{cik}"
            prior_hit = _f13._cache.get(cache_key)  # type: ignore[attr-defined]
            _f13._get_submissions_json(cik)         # type: ignore[attr-defined]
            if prior_hit is not None:
                result.cached_prior += 1
            else:
                result.live += 1
        except Exception as exc:  # noqa: BLE001
            result.failures += 1
            result.errors.append(f"{name}: {type(exc).__name__}: {exc}")
        if progress is not None:
            progress.update(
                "13F mgrs", i, len(managers), started_at=started
            )
    if progress is not None:
        progress.update(
            "13F mgrs", len(managers), len(managers), started_at=started, force=True
        )
    result.duration_seconds = time.monotonic() - started
    return result


def warm_sec_8k(
    tickers: List[str],
    *,
    progress: Optional[ProgressPrinter] = None,
    dry_run: bool = False,
) -> SourceResult:
    """Pre-warm 8-K submissions index for every ticker.

    The submissions endpoint returns the full filings list; the per-date
    filter happens in memory inside ``get_recent_8k_events``. One fetch
    per ticker covers every backfill date.
    """
    result = SourceResult(name="sec_8k", planned=len(tickers))
    if dry_run:
        return result

    from lthcs.sources import sec_edgar as _se
    from lthcs.sources import sec_8k as _s8k

    started = time.monotonic()
    for i, ticker in enumerate(tickers, start=1):
        try:
            cik = _se.get_cik(ticker)
            if cik is None:
                result.failures += 1
                result.errors.append(f"{ticker}: no CIK")
                if progress is not None:
                    progress.update(
                        "8-K", i, len(tickers), started_at=started
                    )
                continue
            cache_key = f"submissions/{cik}"
            prior_hit = _s8k._cache.get(cache_key)  # type: ignore[attr-defined]
            _s8k._get_submissions_json(cik)         # type: ignore[attr-defined]
            if prior_hit is not None:
                result.cached_prior += 1
            else:
                result.live += 1
        except Exception as exc:  # noqa: BLE001
            result.failures += 1
            result.errors.append(f"{ticker}: {type(exc).__name__}: {exc}")
        if progress is not None:
            progress.update(
                "8-K", i, len(tickers), started_at=started
            )
    if progress is not None:
        progress.update(
            "8-K", len(tickers), len(tickers), started_at=started, force=True
        )
    result.duration_seconds = time.monotonic() - started
    return result


def warm_finnhub(
    tickers: List[str],
    *,
    progress: Optional[ProgressPrinter] = None,
    dry_run: bool = False,
) -> SourceResult:
    """Pre-warm Finnhub recommendation history for every universe ticker.

    A single ``get_recommendation_trends`` call returns the FULL monthly
    history per ticker — the as_of-aware backfill will just slice that
    cached list. We skip the news endpoint because Finnhub's free tier
    only returns the trailing 30 days no matter what ``days`` we pass.
    """
    result = SourceResult(name="finnhub", planned=len(tickers))
    if dry_run:
        return result

    from lthcs.sources import finnhub as _finnhub

    started = time.monotonic()
    for i, ticker in enumerate(tickers, start=1):
        try:
            cache_key = _finnhub._reco_cache_key(ticker, as_of=None)  # type: ignore[attr-defined]
            prior_hit = _finnhub._RECO_CACHE.get(cache_key)           # type: ignore[attr-defined]
            _finnhub.get_recommendation_trends(ticker)
            if prior_hit is not None:
                result.cached_prior += 1
            else:
                result.live += 1
        except Exception as exc:  # noqa: BLE001
            result.failures += 1
            result.errors.append(f"{ticker}: {type(exc).__name__}: {exc}")
        if progress is not None:
            progress.update(
                "Finnhub", i, len(tickers), started_at=started
            )
    if progress is not None:
        progress.update(
            "Finnhub", len(tickers), len(tickers), started_at=started, force=True
        )
    result.duration_seconds = time.monotonic() - started
    return result


# ---------------------------------------------------------------------------
# Status file
# ---------------------------------------------------------------------------


def write_status(report: PrewarmReport, *, path: Path = _STATUS_PATH) -> None:
    """Write the post-run status JSON.

    The backfill orchestrator reads this to detect a stale or missing
    pre-warm. Skipped sources are recorded under ``sources_skipped`` so
    the backfill can warn the user that e.g. SEC wasn't pre-warmed.
    """
    payload = {
        "last_run": (report.finished_at or report.started_at)
        .replace(tzinfo=_dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "window_days": report.window_days,
        "end_date": report.end_date.isoformat() if report.end_date else None,
        "universe_size": report.universe_size,
        "sources_warmed": [
            name for name, r in report.results.items() if not r.skipped
        ],
        "sources_skipped": [
            name for name, r in report.results.items() if r.skipped
        ],
        "duration_seconds": round(report.duration_seconds(), 2),
        "dry_run": report.dry_run,
        "errors": report.all_errors(),
        "counts": {
            name: {
                "planned": r.planned,
                "live": r.live,
                "cached_prior": r.cached_prior,
                "failures": r.failures,
            }
            for name, r in report.results.items()
            if not r.skipped
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# Footprint estimate
# ---------------------------------------------------------------------------


def estimate_footprint_mb(report: PrewarmReport) -> float:
    """Rough total cache size after a clean pre-warm. Used in the summary."""
    kb = 0.0
    counts = {name: r.total for name, r in report.results.items()}
    kb += counts.get("yahoo", 0) * _FOOTPRINT_PER_YAHOO_KB
    kb += counts.get("fred", 0) * _FOOTPRINT_PER_FRED_KB
    kb += counts.get("eia", 0) * _FOOTPRINT_PER_EIA_KB
    kb += counts.get("sec_edgar", 0) * _FOOTPRINT_PER_SEC_FACTS_KB
    kb += counts.get("sec_8k", 0) * _FOOTPRINT_PER_SEC_SUBS_KB
    kb += counts.get("sec_form4", 0) * _FOOTPRINT_PER_SEC_SUBS_KB
    kb += counts.get("sec_13f", 0) * _FOOTPRINT_PER_SEC_SUBS_KB
    kb += counts.get("finnhub", 0) * _FOOTPRINT_PER_FINNHUB_RECO_KB
    return kb / 1024.0


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_prewarm(
    *,
    tickers: List[str],
    days: int = _DEFAULT_DAYS,
    end_date: Optional[_dt.date] = None,
    skip_yahoo: bool = False,
    skip_fred: bool = False,
    skip_sec: bool = False,
    skip_finnhub: bool = False,
    max_concurrency: int = _DEFAULT_MAX_CONCURRENCY,
    dry_run: bool = False,
    progress: Optional[ProgressPrinter] = None,
    write_status_file: bool = True,
    status_path: Path = _STATUS_PATH,
) -> PrewarmReport:
    """Run the full pre-warm pipeline.

    A failure inside any one source does NOT abort the others; each
    source maintains its own error list and the orchestrator finalizes a
    status file at the end regardless.

    Sources are pre-warmed in coarse priority order (highest-leverage
    first) so that a Ctrl-C mid-run still leaves the most impactful
    caches populated:

      1. Yahoo prices (90 dates x 168 tickers = 15,120 saved calls)
      2. FRED macros
      3. EIA
      4. SEC EDGAR XBRL facts
      5. SEC 8-K submissions
      6. SEC Form 4 submissions
      7. SEC 13F manager submissions
      8. Finnhub recommendation history
    """
    end_date = end_date or _dt.date.today()
    progress = progress or ProgressPrinter()

    report = PrewarmReport(
        started_at=_dt.datetime.now(_dt.timezone.utc),
        window_days=days,
        end_date=end_date,
        universe_size=len(tickers),
        dry_run=dry_run,
    )

    # --- Yahoo -------------------------------------------------------------
    if skip_yahoo:
        report.results["yahoo"] = SourceResult(name="yahoo", skipped=True)
    else:
        report.results["yahoo"] = warm_yahoo(
            tickers,
            max_concurrency=max_concurrency,
            progress=progress,
            dry_run=dry_run,
        )

    # --- FRED --------------------------------------------------------------
    if skip_fred:
        report.results["fred"] = SourceResult(name="fred", skipped=True)
    else:
        report.results["fred"] = warm_fred(
            progress=progress, dry_run=dry_run
        )

    # --- EIA ---------------------------------------------------------------
    # EIA tucked under --skip-fred because it's the same "macros" category
    # for the user mental-model; the flag set was already getting big.
    if skip_fred:
        report.results["eia"] = SourceResult(name="eia", skipped=True)
    else:
        report.results["eia"] = warm_eia(progress=progress, dry_run=dry_run)

    # --- SEC sources (EDGAR + 8-K + Form 4 + 13F) --------------------------
    if skip_sec:
        for name in ("sec_edgar", "sec_8k", "sec_form4", "sec_13f"):
            report.results[name] = SourceResult(name=name, skipped=True)
    else:
        report.results["sec_edgar"] = warm_sec_edgar(
            tickers, progress=progress, dry_run=dry_run
        )
        report.results["sec_8k"] = warm_sec_8k(
            tickers, progress=progress, dry_run=dry_run
        )
        report.results["sec_form4"] = warm_sec_form4(
            tickers, progress=progress, dry_run=dry_run
        )
        report.results["sec_13f"] = warm_sec_13f(
            progress=progress, dry_run=dry_run
        )

    # --- Finnhub -----------------------------------------------------------
    if skip_finnhub:
        report.results["finnhub"] = SourceResult(name="finnhub", skipped=True)
    else:
        report.results["finnhub"] = warm_finnhub(
            tickers, progress=progress, dry_run=dry_run
        )

    report.finished_at = _dt.datetime.now(_dt.timezone.utc)

    if write_status_file and not dry_run:
        try:
            write_status(report, path=status_path)
        except OSError as exc:
            report.results.setdefault(
                "_status_write",
                SourceResult(name="_status_write"),
            ).errors.append(f"status write failed: {exc}")

    return report


# ---------------------------------------------------------------------------
# Pretty-print summary
# ---------------------------------------------------------------------------


def _format_hms(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m {sec:02d}s"


def print_summary(report: PrewarmReport) -> None:
    """Print the final-line per-source counts + footprint estimate."""
    print()
    print(f"Done in {_format_hms(report.duration_seconds())}.")
    for name, r in report.results.items():
        if r.skipped:
            print(f"  {name:<11} SKIPPED")
            continue
        print(
            f"  {name:<11} live={r.live} cached-prior={r.cached_prior} "
            f"failures={r.failures} total={r.total}"
        )
        # Cap error spam in the on-screen summary — full list still lands
        # in the status file.
        for err in r.errors[:3]:
            print(f"      ! {err}")
        if len(r.errors) > 3:
            print(f"      ... (+{len(r.errors) - 3} more in prewarm_status.json)")
    if not report.dry_run:
        footprint = estimate_footprint_mb(report)
        print(f"Cache populated: ~{footprint:.0f} MB across .cache/lthcs/")
    print("Next step: python scripts/lthcs_backfill.py "
          f"--days {report.window_days}")


def print_dry_run(report: PrewarmReport) -> None:
    """Print what the run WOULD do, without making any calls."""
    print()
    print("Dry run — no upstream calls made. Planned fetches:")
    for name, r in report.results.items():
        if r.skipped:
            print(f"  {name:<11} SKIPPED")
        else:
            print(f"  {name:<11} planned={r.planned}")
    print()
    print(f"Window: {report.end_date} (end) — {report.window_days} day backfill")
    print(f"Universe: {report.universe_size} tickers")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lthcs_backfill_prewarm",
        description=(
            "Pre-warm LTHCS source caches with historical data so the "
            "90-day backfill runs ~5x faster. Safe to re-run."
        ),
    )
    p.add_argument(
        "--days", type=int, default=_DEFAULT_DAYS,
        help=f"Backfill window in days (default {_DEFAULT_DAYS}).",
    )
    p.add_argument(
        "--end", type=str, default=None,
        help="End date (YYYY-MM-DD). Defaults to today.",
    )
    p.add_argument(
        "--tickers", type=str, default=None,
        help="Comma-separated subset to pre-warm. Defaults to the full active universe.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Report planned fetches without making network calls.",
    )
    p.add_argument(
        "--skip-yahoo", action="store_true",
        help="Skip the Yahoo prices pre-warm step.",
    )
    p.add_argument(
        "--skip-fred", action="store_true",
        help="Skip the FRED + EIA macros pre-warm steps.",
    )
    p.add_argument(
        "--skip-sec", action="store_true",
        help="Skip every SEC pre-warm step (EDGAR, 8-K, Form 4, 13F).",
    )
    p.add_argument(
        "--skip-finnhub", action="store_true",
        help="Skip the Finnhub recommendation history pre-warm step.",
    )
    p.add_argument(
        "--max-concurrency", type=int, default=_DEFAULT_MAX_CONCURRENCY,
        help=(
            "Yahoo thread-pool size (default %(default)s). SEC + FRED stay "
            "single-threaded regardless because their token buckets serialize."
        ),
    )
    return p


def _parse_tickers_arg(raw: Optional[str]) -> Optional[List[str]]:
    if not raw:
        return None
    parts = [p.strip().upper() for p in raw.split(",") if p.strip()]
    return parts or None


def main(argv: Optional[List[str]] = None) -> int:
    args = build_argparser().parse_args(argv)

    end_date: Optional[_dt.date] = None
    if args.end:
        try:
            end_date = _dt.date.fromisoformat(args.end)
        except ValueError:
            print(f"ERROR: --end must be ISO YYYY-MM-DD, got {args.end!r}", file=sys.stderr)
            return 2
    end_date = end_date or _dt.date.today()
    start_date = end_date - _dt.timedelta(days=int(args.days))

    explicit_tickers = _parse_tickers_arg(args.tickers)
    universe = explicit_tickers if explicit_tickers else load_universe()
    if not universe:
        print("ERROR: empty universe. Pass --tickers AAPL,MSFT,... or ensure "
              "data/lthcs/universe.json has active tickers.", file=sys.stderr)
        return 2

    print(f"LTHCS Backfill Pre-warmer")
    print(f"Window: {start_date} -> {end_date} ({args.days} days)")
    print(f"Universe: {len(universe)} tickers")
    if args.dry_run:
        print("Mode: DRY RUN (no network calls)")
    print()

    progress = ProgressPrinter()
    report = run_prewarm(
        tickers=universe,
        days=args.days,
        end_date=end_date,
        skip_yahoo=args.skip_yahoo,
        skip_fred=args.skip_fred,
        skip_sec=args.skip_sec,
        skip_finnhub=args.skip_finnhub,
        max_concurrency=args.max_concurrency,
        dry_run=args.dry_run,
        progress=progress,
    )

    if args.dry_run:
        print_dry_run(report)
    else:
        print_summary(report)

    # Non-zero exit if every enabled source failed completely; otherwise zero
    # (per-ticker failures are allowed).
    any_success = any(
        r.live + r.cached_prior > 0
        for r in report.results.values()
        if not r.skipped
    )
    if not args.dry_run and not any_success and report.results:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
