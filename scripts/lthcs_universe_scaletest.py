#!/usr/bin/env python3
"""Run the LTHCS daily pipeline against a synthetic ~500-ticker universe.

This is the GO / NO-GO gate for the planned S&P 500 expansion
(2026-05-27). It does NOT touch the production universe or production
snapshots: a temporary universe file is assembled from
``data/lthcs/universe.json`` (production, 169 tickers) plus
``data/lthcs/sp500_candidate_seed.json`` (333 tickers), written to
``data/lthcs/universe_candidate_full.json``, and the daily pipeline is
invoked with ``--candidate-universe`` + ``--dry-run`` so persistence
(snapshots, history, narratives) is bypassed.

What we measure:

* Wall-clock for the full Stage 1 → Stage 8 sequence
* API call counts per source (via ``lthcs.sources._api_counter``)
* Rate-limit hits per source (HTTP 429 buckets)
* Peak RSS (``resource.getrusage`` self + children)
* Per-pillar coverage (% of tickers with a real, non-default signal)
* Per-cohort population (maturity_stage histogram)

Output: ``data/lthcs/scaletest/<calc_date>_scaletest_report.md``

Verdict logic:

* NO-GO if any source hit >0 rate-limit buckets at the end of the run
* NO-GO if total wall-clock > 30 minutes (cron safety margin)
* NO-GO if Adoption or Financial pillar coverage falls below 80%
  (a sign that the new tickers don't have enough SEC / Yahoo signal
  to score reliably)
* GO otherwise

CLI:
    python scripts/lthcs_universe_scaletest.py \\
        [--n 500] \\
        [--out data/lthcs/scaletest/]

The ``--n`` knob is intentionally permissive: anything > the combined
production+candidate count is clamped down; anything smaller is a
randomly-sampled subset (useful for a 50-ticker dry run before the full
500-ticker pull).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import random
import resource
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parent.parent
PROD_UNIVERSE = REPO_ROOT / "data" / "lthcs" / "universe.json"
CANDIDATE_SEED = REPO_ROOT / "data" / "lthcs" / "sp500_candidate_seed.json"
DEFAULT_FULL_UNIVERSE = REPO_ROOT / "data" / "lthcs" / "universe_candidate_full.json"
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "lthcs" / "scaletest"

# Verdict thresholds — keep in lockstep with the rollout doc.
WALL_CLOCK_LIMIT_S = 30 * 60         # 30 min
COVERAGE_FLOOR = 0.80                 # 80% per blocking pillar
BLOCKING_PILLARS = {"adoption", "financial"}


def build_full_universe(n: int) -> Tuple[Path, Dict[str, Any]]:
    """Merge production + candidate seed into a single universe file.

    Returns (path_to_universe, stats_dict).
    """
    prod = json.loads(PROD_UNIVERSE.read_text())
    seed = json.loads(CANDIDATE_SEED.read_text())

    prod_tickers = list(prod.get("tickers", []))
    prod_syms = {t["ticker"] for t in prod_tickers if isinstance(t, dict)}

    # Convert seed entries into the production-universe ticker schema.
    new_tickers: List[Dict[str, Any]] = []
    for entry in seed.get("tickers", []):
        sym = entry.get("symbol") or entry.get("ticker")
        if not sym or sym in prod_syms:
            continue
        new_tickers.append(
            {
                "ticker": sym,
                "name": entry.get("name", sym),
                "sector": entry.get("gics_sector", "Industrials"),
                "industry": entry.get("gics_sector_group", ""),
                "index_membership": ["S&P 500"],
                "maturity_stage": entry.get(
                    "inferred_maturity_stage", "standard_compounder"
                ),
                "active": True,
                "source": "sp500_candidate_seed",
            }
        )

    combined = prod_tickers + new_tickers

    # Clamp / sample down to ``n``.
    n = max(1, int(n))
    if n < len(combined):
        # Always keep all production tickers first, then sample from the
        # candidate pool. That way a small --n still exercises the
        # expansion paths.
        keep_prod = prod_tickers[: min(len(prod_tickers), n)]
        remaining = n - len(keep_prod)
        rng = random.Random(42)
        sampled = rng.sample(new_tickers, min(remaining, len(new_tickers)))
        final = keep_prod + sampled
    else:
        final = combined

    out = {
        "version": "scaletest-0.1",
        "last_updated": _dt.date.today().isoformat(),
        "description": (
            "SYNTHETIC scaletest universe — production + S&P 500 candidate "
            "seed. NEVER replace data/lthcs/universe.json with this file."
        ),
        "tickers": final,
    }

    DEFAULT_FULL_UNIVERSE.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_FULL_UNIVERSE.write_text(json.dumps(out, indent=2))

    stats = {
        "production_count": len(prod_tickers),
        "candidate_seed_count": len(seed.get("tickers", [])),
        "merged_count": len(combined),
        "final_count": len(final),
        "added_from_seed": max(0, len(final) - len(prod_tickers)),
    }
    return DEFAULT_FULL_UNIVERSE, stats


def measure_pillar_coverage(state: Any) -> Dict[str, float]:
    """Compute per-pillar coverage by inspecting ``variable_detail_rows``.

    A pillar is 'covered' for a ticker when ``data_quality.has_data`` is
    True OR when the sub-score is non-default (anything not exactly
    50.0). The choice is deliberately tolerant: at S&P 500 scale we
    expect a long tail of tickers with sparse signal, and we want to
    detect a collapse, not nitpick edge cases.
    """
    rows = getattr(state, "variable_detail_rows", None) or []
    if not rows:
        return {}

    by_pillar: Dict[str, Dict[str, int]] = {}
    for row in rows:
        pillar = row.get("pillar")
        if not pillar:
            continue
        slot = by_pillar.setdefault(pillar, {"total": 0, "covered": 0})
        slot["total"] += 1
        dq = row.get("data_quality") or {}
        has_data = bool(dq.get("has_data"))
        sub = row.get("sub_score")
        non_default = isinstance(sub, (int, float)) and abs(float(sub) - 50.0) > 1e-6
        if has_data or non_default:
            slot["covered"] += 1

    return {
        pillar: (slot["covered"] / slot["total"]) if slot["total"] else 0.0
        for pillar, slot in by_pillar.items()
    }


def measure_cohort_population(universe_path: Path) -> Dict[str, int]:
    """Group tickers by ``maturity_stage``."""
    data = json.loads(universe_path.read_text())
    hist: Dict[str, int] = {}
    for entry in data.get("tickers", []):
        stage = entry.get("maturity_stage", "unknown")
        hist[stage] = hist.get(stage, 0) + 1
    return hist


def run_pipeline(universe_path: Path) -> Tuple[bool, Optional[Any], Optional[str]]:
    """Invoke the LTHCS daily pipeline in-process and return its state.

    We import ``lthcs_daily`` lazily so the script can be imported by
    tests without dragging in yfinance/etc.
    """
    sys.path.insert(0, str(REPO_ROOT))
    try:
        import lthcs_daily  # type: ignore
    except Exception as exc:
        return False, None, "import lthcs_daily failed: %s" % exc

    argv = [
        "--candidate-universe",
        str(universe_path),
        "--dry-run",
        # No --tickers => use the full candidate universe.
    ]
    try:
        args = lthcs_daily.parse_args(argv)
    except SystemExit as exc:
        return False, None, "parse_args SystemExit: %s" % exc

    state = lthcs_daily.PipelineState(args=args)
    try:
        # Execute each pipeline stage in order. Stage 8 is a no-op in
        # dry-run mode (see lthcs_daily.stage_8_persist).
        for stage in lthcs_daily.STAGES:  # type: ignore[attr-defined]
            ok = stage(state)
            if not ok:
                return False, state, "stage %s returned False" % stage.__name__
    except Exception:
        return False, state, traceback.format_exc()
    return True, state, None


def render_report(payload: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("# LTHCS Universe Scaletest Report")
    lines.append("")
    lines.append("- Run date: %s" % payload["run_date"])
    lines.append("- Verdict: **%s**" % payload["verdict"])
    lines.append("- Reasons: %s" % (", ".join(payload["reasons"]) or "—"))
    lines.append("")
    lines.append("## Universe shape")
    lines.append("")
    for k, v in payload["universe_stats"].items():
        lines.append("- %s: %s" % (k, v))
    lines.append("")
    lines.append("## Runtime")
    lines.append("")
    lines.append("- Wall clock: %.1fs" % payload["wall_clock_s"])
    lines.append("- Peak RSS: %.1f MB" % payload["peak_rss_mb"])
    lines.append("- Pipeline status: %s" % ("ok" if payload["pipeline_ok"] else "ERROR"))
    if payload.get("pipeline_error"):
        lines.append("")
        lines.append("```")
        lines.append(payload["pipeline_error"][:2000])
        lines.append("```")
    lines.append("")
    lines.append("## API call counts")
    lines.append("")
    lines.append("| source | ok | cache_hit | rate_limit | error |")
    lines.append("| --- | --- | --- | --- | --- |")
    for source, buckets in sorted(payload["api_counts"].items()):
        lines.append(
            "| %s | %d | %d | %d | %d |"
            % (
                source,
                buckets.get("ok", 0),
                buckets.get("cache_hit", 0),
                buckets.get("rate_limit", 0),
                buckets.get("error", 0),
            )
        )
    lines.append("")
    lines.append("## Per-pillar coverage")
    lines.append("")
    if payload["pillar_coverage"]:
        for pillar, frac in sorted(payload["pillar_coverage"].items()):
            lines.append("- %s: %.1f%%" % (pillar, frac * 100))
    else:
        lines.append("(no variable_detail rows captured)")
    lines.append("")
    lines.append("## Cohort population")
    lines.append("")
    for stage, n in sorted(payload["cohort_population"].items(), key=lambda kv: -kv[1]):
        lines.append("- %s: %d" % (stage, n))
    lines.append("")
    return "\n".join(lines) + "\n"


def determine_verdict(
    wall_clock_s: float,
    api_counts: Dict[str, Dict[str, int]],
    pillar_coverage: Dict[str, float],
    pipeline_ok: bool,
) -> Tuple[str, List[str]]:
    reasons: List[str] = []
    if not pipeline_ok:
        reasons.append("pipeline did not complete cleanly")
    if wall_clock_s > WALL_CLOCK_LIMIT_S:
        reasons.append(
            "wall clock %.0fs exceeds %.0fs limit" % (wall_clock_s, WALL_CLOCK_LIMIT_S)
        )
    for source, buckets in api_counts.items():
        if buckets.get("rate_limit", 0) > 0:
            reasons.append(
                "%s hit %d rate-limit response(s)"
                % (source, buckets["rate_limit"])
            )
    for pillar in BLOCKING_PILLARS:
        frac = pillar_coverage.get(pillar)
        if frac is not None and frac < COVERAGE_FLOOR:
            reasons.append(
                "%s coverage %.1f%% below %.0f%% floor"
                % (pillar, frac * 100, COVERAGE_FLOOR * 100)
            )
    return ("NO-GO" if reasons else "GO"), reasons


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="lthcs_universe_scaletest",
        description="Run a GO / NO-GO scaletest for the LTHCS universe expansion.",
    )
    p.add_argument(
        "--n",
        type=int,
        default=500,
        help="Cap universe size at N tickers (default 500).",
    )
    p.add_argument(
        "--out",
        default=str(DEFAULT_OUT_DIR),
        help="Output directory for the scaletest report (default: %s)" % DEFAULT_OUT_DIR,
    )
    p.add_argument(
        "--skip-pipeline",
        action="store_true",
        help=(
            "Build the synthetic universe + emit a stub report but do "
            "NOT run the pipeline. Used by smoke tests where importing "
            "the daily pipeline would require yfinance / fred / etc."
        ),
    )
    args = p.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    universe_path, universe_stats = build_full_universe(args.n)
    cohort_population = measure_cohort_population(universe_path)

    # Activate the API counter for the duration of the run.
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from lthcs.sources import _api_counter
    except Exception:
        _api_counter = None  # type: ignore[assignment]
    if _api_counter is not None:
        _api_counter.reset()
        _api_counter.enable()

    pipeline_ok = True
    pipeline_error: Optional[str] = None
    pillar_coverage: Dict[str, float] = {}
    wall_clock_s = 0.0
    state = None

    if not args.skip_pipeline:
        start = time.monotonic()
        pipeline_ok, state, pipeline_error = run_pipeline(universe_path)
        wall_clock_s = time.monotonic() - start
        if state is not None:
            pillar_coverage = measure_pillar_coverage(state)

    api_counts = _api_counter.snapshot() if _api_counter is not None else {}

    usage = resource.getrusage(resource.RUSAGE_SELF)
    # On macOS ru_maxrss is bytes; on Linux it's kilobytes. Normalize
    # to MB conservatively (assume KB on Linux, bytes elsewhere).
    if sys.platform.startswith("linux"):
        peak_rss_mb = usage.ru_maxrss / 1024.0
    else:
        peak_rss_mb = usage.ru_maxrss / (1024.0 * 1024.0)

    verdict, reasons = determine_verdict(
        wall_clock_s=wall_clock_s,
        api_counts=api_counts,
        pillar_coverage=pillar_coverage,
        pipeline_ok=pipeline_ok if not args.skip_pipeline else True,
    )
    if args.skip_pipeline:
        reasons.append("pipeline skipped (--skip-pipeline)")

    payload = {
        "run_date": _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "verdict": verdict,
        "reasons": reasons,
        "wall_clock_s": wall_clock_s,
        "peak_rss_mb": peak_rss_mb,
        "pipeline_ok": pipeline_ok,
        "pipeline_error": pipeline_error,
        "universe_stats": universe_stats,
        "api_counts": api_counts,
        "pillar_coverage": pillar_coverage,
        "cohort_population": cohort_population,
        "universe_path": str(universe_path),
    }

    today = _dt.date.today().isoformat()
    report_path = out_dir / ("%s_scaletest_report.md" % today)
    json_path = out_dir / ("%s_scaletest_report.json" % today)
    report_path.write_text(render_report(payload))
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True))

    print("scaletest verdict: %s" % verdict)
    if reasons:
        for r in reasons:
            print("  - %s" % r)
    print("report:        %s" % report_path)
    print("report (json): %s" % json_path)

    if _api_counter is not None:
        _api_counter.disable()
        _api_counter.reset()

    return 0 if verdict == "GO" or args.skip_pipeline else 1


if __name__ == "__main__":
    sys.exit(main())
