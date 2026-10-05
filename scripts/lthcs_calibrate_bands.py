#!/usr/bin/env python3
"""Propose (and optionally apply) LTHCS score-band cutoffs from the real
composite distribution.

Why
---
``data/lthcs/weights.json`` -> ``score_bands`` was hand-set at
85 / 80 / 70 / 60 / 50 before the composite had a real distribution. The
top composite has never got past the high 70s, so ``elite`` and
``high_confidence`` are permanently empty and the monthly quality audit
(``scripts/lthcs_quality_audit_runner.py``) reports the bands as
MISALIGNED/SKEWED by construction. This tool re-derives integer cutoffs as
quantiles of the observed distribution, so each band holds a documented
share of the universe.

Method (``quantile-share-v1``)
------------------------------
1. Load one or more daily equity snapshots (default: the latest file in
   ``data/lthcs/snapshots/``; ``--snapshots N`` pools the latest N days to
   damp day-to-day noise). Each snapshot is ``{"calc_date", "scores": [
   {"ticker", "lthcs_score", ...}, ...]}``; a row counts as scored when
   ``lthcs_score`` is a finite number.
2. Refuse to run if any selected snapshot has fewer than ``--min-tickers``
   scored rows (default 400). This guards against calibrating on the old
   ~219-name universe by accident; override explicitly for experiments.
3. Target shares, top-down (``DEFAULT_TARGETS_PCT``)::

       elite            5%   top 1-in-20
       high_confidence 10%   next 10%  (top 15% cumulative)
       constructive    25%   next 25%  (top 40%)
       monitor         30%   next 30%  (top 70%)
       weakening       20%   next 20%  (top 90%)
       review          10%   the rest

   These are chosen against what the quality audit checks:
   ``_band_verdict`` / ``_distribution_summary`` flag elite or
   high_confidence being empty (we give them 15% together, so ~75 names on
   a 520 universe) and review >= 40% (critical) / >= 50% (skewed); the
   band-threshold audit flags review above ~15% of the universe. Review at
   10% leaves headroom for a broad drawdown before those alarms fire.
4. For each band boundary, pick the integer cutoff ``m`` whose cumulative
   share ``P(floor(score) >= m)`` is closest to the cumulative target
   (ties go to the higher, stricter cutoff). Flooring matches
   ``lthcs.score.assign_band``.
5. Enforce structure: cutoffs strictly descending, every band at least
   ``--min-width`` points wide (default 3), elite ends at 100, review
   starts at 0, bands contiguous. Keys, labels and colors are never
   touched. The result is validated against ``lthcs/schemas/weights.py``.

Output
------
A human-readable report (current vs proposed cutoffs, ticker count per band
before/after, plus the audit-facing checks). Read-only by default.
``--write`` updates ``weights.json`` in place: only the ``min``/``max``
numbers of each band, ``last_updated``, and a top-level
``score_bands_calibration`` provenance block (snapshot dates, ticker count,
method, targets, min width, previous cutoffs, date). Note that crypto
(``scripts/lthcs_crypto_daily.py``) shares these bands.

Usage
-----
    python scripts/lthcs_calibrate_bands.py                 # report only
    python scripts/lthcs_calibrate_bands.py --snapshots 5   # pool 5 days
    python scripts/lthcs_calibrate_bands.py --write         # apply
    python scripts/lthcs_calibrate_bands.py --min-tickers 0 # experiment
    python scripts/lthcs_calibrate_bands.py \\
        --targets elite=5,high_confidence=10,constructive=25,monitor=30,weakening=20,review=10
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import date as _date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lthcs.bands import BAND_ORDER_HIGH_TO_LOW  # noqa: E402

DEFAULT_SNAPSHOT_DIR = REPO_ROOT / "data" / "lthcs" / "snapshots"
DEFAULT_WEIGHTS_PATH = REPO_ROOT / "data" / "lthcs" / "weights.json"

METHOD = "quantile-share-v1"
DEFAULT_MIN_TICKERS = 400
DEFAULT_MIN_WIDTH = 3

# Share of the universe per band, top-down, in percent. Must sum to 100.
DEFAULT_TARGETS_PCT: Dict[str, float] = {
    "elite": 5.0,
    "high_confidence": 10.0,
    "constructive": 25.0,
    "monitor": 30.0,
    "weakening": 20.0,
    "review": 10.0,
}

_DATE_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")


class CalibrationError(Exception):
    """Raised for any condition that must stop the calibration."""


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def list_snapshot_files(snapshot_dir: Path) -> List[Path]:
    """Dated snapshot files (``YYYY-MM-DD.json``), oldest first."""
    if not snapshot_dir.is_dir():
        return []
    files = [p for p in snapshot_dir.iterdir() if _DATE_FILE_RE.match(p.name)]
    return sorted(files, key=lambda p: p.name)


def scored_values(snapshot: Dict[str, Any]) -> Dict[str, float]:
    """``{ticker: lthcs_score}`` for rows with a finite numeric score."""
    out: Dict[str, float] = {}
    for row in snapshot.get("scores") or []:
        if not isinstance(row, dict):
            continue
        v = row.get("lthcs_score")
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        if not math.isfinite(float(v)):
            continue
        out[str(row.get("ticker", f"_row{len(out)}"))] = float(v)
    return out


def load_snapshots(paths: Sequence[Path]) -> List[Tuple[str, Dict[str, float]]]:
    """``[(calc_date, {ticker: score})]`` in the given order."""
    loaded: List[Tuple[str, Dict[str, float]]] = []
    for p in paths:
        try:
            snap = json.loads(Path(p).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CalibrationError(f"cannot read snapshot {p}: {exc}") from exc
        if not isinstance(snap, dict) or "scores" not in snap:
            raise CalibrationError(
                f"{p} is not an LTHCS snapshot (expected a top-level object with 'scores')"
            )
        m = _DATE_FILE_RE.match(Path(p).name)
        calc_date = str(snap.get("calc_date") or (m.group(1) if m else Path(p).stem))
        loaded.append((calc_date, scored_values(snap)))
    return loaded


def parse_targets(spec: Optional[str]) -> Dict[str, float]:
    """Parse ``elite=5,high_confidence=10,...`` (percent). Must cover all bands, sum 100."""
    if not spec:
        return dict(DEFAULT_TARGETS_PCT)
    out: Dict[str, float] = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise CalibrationError(f"bad --targets entry {part!r} (want band=pct)")
        k, v = part.split("=", 1)
        k = k.strip()
        if k not in BAND_ORDER_HIGH_TO_LOW:
            raise CalibrationError(f"unknown band {k!r} in --targets")
        try:
            out[k] = float(v)
        except ValueError as exc:
            raise CalibrationError(f"bad pct for {k!r}: {v!r}") from exc
    validate_targets(out)
    return out


def validate_targets(targets: Dict[str, float]) -> None:
    missing = [b for b in BAND_ORDER_HIGH_TO_LOW if b not in targets]
    if missing:
        raise CalibrationError(f"targets missing band(s): {missing}")
    if any(targets[b] <= 0 for b in BAND_ORDER_HIGH_TO_LOW):
        raise CalibrationError("every band target must be > 0%")
    total = sum(targets[b] for b in BAND_ORDER_HIGH_TO_LOW)
    if abs(total - 100.0) > 1e-6:
        raise CalibrationError(f"targets must sum to 100%, got {total:g}%")


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------

def _floor_scores(scores: Iterable[float]) -> List[int]:
    return [int(math.floor(max(0.0, min(100.0, s)))) for s in scores]


def share_at_or_above(floored: Sequence[int], cutoff: int) -> float:
    if not floored:
        return 0.0
    return sum(1 for f in floored if f >= cutoff) / len(floored)


def ideal_cutoff(floored: Sequence[int], cum_target: float) -> int:
    """Integer m in [1, 100] with P(floor >= m) closest to ``cum_target``.

    Ties resolve to the higher (stricter) cutoff.
    """
    best_m, best_err = 100, float("inf")
    for m in range(100, 0, -1):
        err = abs(share_at_or_above(floored, m) - cum_target)
        if err < best_err - 1e-12:
            best_m, best_err = m, err
    return best_m


def propose_cutoffs(
    scores: Sequence[float],
    targets_pct: Dict[str, float],
    min_width: int = DEFAULT_MIN_WIDTH,
) -> Dict[str, Tuple[int, int]]:
    """Return ``{band: (min, max)}`` contiguous integer ranges over [0, 100]."""
    validate_targets(targets_pct)
    n_bands = len(BAND_ORDER_HIGH_TO_LOW)
    if min_width < 1 or n_bands * min_width > 101:
        raise CalibrationError(
            f"--min-width {min_width} is infeasible for {n_bands} bands over 0-100"
        )
    if not scores:
        raise CalibrationError("no scores to calibrate on")
    floored = _floor_scores(scores)

    # Lower cutoffs for every band except review (whose min is always 0).
    upper_bands = BAND_ORDER_HIGH_TO_LOW[:-1]
    cum = 0.0
    ideal: List[int] = []
    for b in upper_bands:
        cum += targets_pct[b] / 100.0
        ideal.append(ideal_cutoff(floored, cum))

    # Top-down: elite needs [m, 100] >= min_width wide, each next cutoff at
    # least min_width below the previous one.
    cuts = list(ideal)
    cuts[0] = min(cuts[0], 101 - min_width)
    for i in range(1, len(cuts)):
        cuts[i] = min(cuts[i], cuts[i - 1] - min_width)
    # Bottom-up: review [0, m-1] needs m >= min_width, and spacing again.
    cuts[-1] = max(cuts[-1], min_width)
    for i in range(len(cuts) - 2, -1, -1):
        cuts[i] = max(cuts[i], cuts[i + 1] + min_width)

    out: Dict[str, Tuple[int, int]] = {}
    hi = 100
    for b, lo in zip(upper_bands, cuts):
        out[b] = (lo, hi)
        hi = lo - 1
    out[BAND_ORDER_HIGH_TO_LOW[-1]] = (0, hi)
    _assert_structure(out, min_width)
    return out


def _assert_structure(ranges: Dict[str, Tuple[int, int]], min_width: int) -> None:
    prev_lo = 101
    for b in BAND_ORDER_HIGH_TO_LOW:
        lo, hi = ranges[b]
        if hi != prev_lo - 1:
            raise CalibrationError(f"internal: band {b} not contiguous ({lo}-{hi})")
        if hi - lo + 1 < min_width:
            raise CalibrationError(f"internal: band {b} narrower than {min_width}")
        prev_lo = lo
    if prev_lo != 0:
        raise CalibrationError("internal: bands do not start at 0")


def count_by_band(scores: Iterable[float], ranges: Dict[str, Tuple[int, int]]) -> Dict[str, int]:
    counts = {b: 0 for b in ranges}
    for f in _floor_scores(scores):
        for b, (lo, hi) in ranges.items():
            if lo <= f <= hi:
                counts[b] += 1
                break
    return counts


def current_ranges(weights_cfg: Dict[str, Any]) -> Dict[str, Tuple[int, int]]:
    bands = weights_cfg.get("score_bands") or {}
    missing = [b for b in BAND_ORDER_HIGH_TO_LOW if b not in bands]
    if missing:
        raise CalibrationError(f"weights.json score_bands missing band(s): {missing}")
    extra = [b for b in bands if b not in BAND_ORDER_HIGH_TO_LOW]
    if extra:
        raise CalibrationError(f"weights.json has unexpected band key(s): {extra}")
    return {b: (int(bands[b]["min"]), int(bands[b]["max"])) for b in BAND_ORDER_HIGH_TO_LOW}


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _pctile(sorted_vals: Sequence[float], q: float) -> float:
    if not sorted_vals:
        return float("nan")
    idx = q * (len(sorted_vals) - 1)
    lo, hi = int(math.floor(idx)), int(math.ceil(idx))
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (idx - lo)


def build_report(
    *,
    snapshot_dates: Sequence[str],
    latest_scores: Sequence[float],
    pooled_scores: Sequence[float],
    current: Dict[str, Tuple[int, int]],
    proposed: Dict[str, Tuple[int, int]],
    targets_pct: Dict[str, float],
    min_width: int,
    min_tickers: int,
    guard_overridden: bool,
    labels: Dict[str, str],
) -> str:
    n = len(latest_scores)
    before = count_by_band(latest_scores, current)
    after = count_by_band(latest_scores, proposed)
    sv = sorted(pooled_scores)
    lines: List[str] = []
    lines.append("LTHCS score-band calibration")
    lines.append("=" * 28)
    lines.append(f"method:        {METHOD}")
    lines.append(f"snapshot(s):   {', '.join(snapshot_dates)}")
    lines.append(
        f"tickers:       {n} scored in latest snapshot"
        + (f"; {len(pooled_scores)} pooled observations" if len(snapshot_dates) > 1 else "")
    )
    guard = f"min {min_tickers} scored tickers"
    if guard_overridden:
        guard += "  ** OVERRIDDEN (below default guard) — illustration only **"
    lines.append(f"guard:         {guard}")
    lines.append(f"min width:     {min_width} points per band")
    if sv:
        lines.append(
            "distribution:  "
            + ", ".join(
                f"{lab}={_pctile(sv, q):.1f}"
                for lab, q in (("min", 0), ("p10", .1), ("p30", .3), ("p50", .5),
                               ("p60", .6), ("p85", .85), ("p95", .95), ("max", 1))
            )
        )
    lines.append("")
    hdr = (
        f"{'band':<16} {'label':<28} {'target':>6}  {'current':>8} {'n':>4} {'%':>6}"
        f"   {'proposed':>8} {'n':>4} {'%':>6}"
    )
    lines.append(hdr)
    lines.append("-" * len(hdr))
    for b in BAND_ORDER_HIGH_TO_LOW:
        clo, chi = current[b]
        plo, phi = proposed[b]
        cp = 100.0 * before[b] / n if n else 0.0
        pp = 100.0 * after[b] / n if n else 0.0
        changed = "" if (clo, chi) == (plo, phi) else " *"
        lines.append(
            f"{b:<16} {labels.get(b, '')[:28]:<28} {targets_pct[b]:>5.0f}%  "
            f"{f'{clo}-{chi}':>8} {before[b]:>4} {cp:>5.1f}%"
            f"   {f'{plo}-{phi}':>8} {after[b]:>4} {pp:>5.1f}%{changed}"
        )
    lines.append("")
    lines.append("audit checks (latest snapshot, proposed bands):")
    starved = [b for b in ("elite", "high_confidence") if after[b] == 0]
    review_pct = 100.0 * after["review"] / n if n else 0.0
    lines.append(
        f"  elite/high_confidence populated: {'NO — ' + ', '.join(starved) if starved else 'yes'}"
    )
    lines.append(f"  review share: {review_pct:.1f}% (audit flags >=40% critical, >=50% skewed)")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def apply_ranges(
    weights_cfg: Dict[str, Any],
    proposed: Dict[str, Tuple[int, int]],
    provenance: Dict[str, Any],
    today: str,
) -> Dict[str, Any]:
    """Return a new config with updated band min/max, last_updated, provenance."""
    cfg = json.loads(json.dumps(weights_cfg))
    for b, (lo, hi) in proposed.items():
        cfg["score_bands"][b]["min"] = lo
        cfg["score_bands"][b]["max"] = hi
    cfg["last_updated"] = today
    cfg["score_bands_calibration"] = provenance
    return cfg


def _render_preserving_layout(original_text: str, new_cfg: Dict[str, Any]) -> Optional[str]:
    """Patch numbers in place so the hand-aligned layout of weights.json survives.

    Returns None if the patched text does not round-trip to ``new_cfg``
    (caller then falls back to a plain ``json.dumps``).
    """
    text = original_text
    for b in BAND_ORDER_HIGH_TO_LOW:
        spec = new_cfg["score_bands"][b]
        pat = re.compile(
            r'("' + re.escape(b) + r'"\s*:\s*\{\s*"min"\s*:\s*)-?\d+(\s*,\s*"max"\s*:\s*)-?\d+'
        )
        text, n = pat.subn(lambda m: f'{m.group(1)}{spec["min"]}{m.group(2)}{spec["max"]}', text, count=1)
        if n != 1:
            return None
    text, n = re.subn(
        r'("last_updated"\s*:\s*)"[^"]*"', lambda m: f'{m.group(1)}"{new_cfg["last_updated"]}"', text, count=1
    )
    if n != 1:
        return None

    block = json.dumps(new_cfg["score_bands_calibration"], indent=2, ensure_ascii=False)
    block = block.replace("\n", "\n  ")
    key = '"score_bands_calibration"'
    idx = text.find(key)
    if idx >= 0:
        colon = text.index(":", idx + len(key))
        start = colon + 1
        while text[start] in " \t\r\n":
            start += 1
        _, end = json.JSONDecoder().raw_decode(text, start)
        text = text[:start] + block + text[end:]
    else:
        # Insert right after the score_bands object.
        sb = text.find('"score_bands"')
        if sb < 0:
            return None
        start = text.index("{", sb)
        _, end = json.JSONDecoder().raw_decode(text, start)
        text = text[:end] + ",\n  " + key + ": " + block + text[end:]
    try:
        if json.loads(text) != new_cfg:
            return None
    except ValueError:
        return None
    return text


def write_weights(path: Path, original_text: str, new_cfg: Dict[str, Any]) -> None:
    from lthcs.schemas.weights import Weights

    Weights.model_validate(new_cfg)  # raises on any structural problem
    text = _render_preserving_layout(original_text, new_cfg)
    if text is None:
        text = json.dumps(new_cfg, indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Propose LTHCS score-band cutoffs from the live score distribution.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--snapshot", action="append", type=Path, default=None,
                    help="Explicit snapshot file (repeatable). Overrides --snapshots/--snapshot-dir.")
    ap.add_argument("--snapshot-dir", type=Path, default=DEFAULT_SNAPSHOT_DIR,
                    help="Directory of dated equity snapshots (default: data/lthcs/snapshots).")
    ap.add_argument("--snapshots", type=int, default=1, metavar="N",
                    help="Pool the latest N dated snapshots (default 1).")
    ap.add_argument("--weights", type=Path, default=DEFAULT_WEIGHTS_PATH,
                    help="weights.json to read (and update with --write).")
    ap.add_argument("--targets", default=None,
                    help="Band shares in percent, e.g. elite=5,high_confidence=10,... (sum 100).")
    ap.add_argument("--min-width", type=int, default=DEFAULT_MIN_WIDTH,
                    help=f"Minimum band width in points (default {DEFAULT_MIN_WIDTH}).")
    ap.add_argument("--min-tickers", type=int, default=DEFAULT_MIN_TICKERS,
                    help=f"Refuse if any snapshot has fewer scored tickers (default {DEFAULT_MIN_TICKERS}).")
    ap.add_argument("--write", action="store_true",
                    help="Apply the proposal to weights.json (default: read-only report).")
    ap.add_argument("--today", default=None, help=argparse.SUPPRESS)
    return ap.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        if args.snapshot:
            paths = list(args.snapshot)
        else:
            if args.snapshots < 1:
                raise CalibrationError("--snapshots must be >= 1")
            files = list_snapshot_files(args.snapshot_dir)
            if not files:
                raise CalibrationError(f"no dated snapshots found in {args.snapshot_dir}")
            paths = files[-args.snapshots:]
        snaps = load_snapshots(paths)
        snaps.sort(key=lambda t: t[0])

        small = [(d, len(v)) for d, v in snaps if len(v) < args.min_tickers]
        if small:
            detail = ", ".join(f"{d}: {n}" for d, n in small)
            raise CalibrationError(
                f"refusing to calibrate: snapshot(s) below the {args.min_tickers}-ticker guard "
                f"({detail}). Bands should be calibrated on the expanded universe; pass "
                f"--min-tickers N to override for an experiment."
            )

        targets = parse_targets(args.targets)
        weights_text = args.weights.read_text(encoding="utf-8")
        weights_cfg = json.loads(weights_text)
        current = current_ranges(weights_cfg)

        latest_date, latest = snaps[-1]
        pooled: List[float] = [s for _, v in snaps for s in v.values()]
        proposed = propose_cutoffs(pooled, targets, args.min_width)

        labels = {b: str(weights_cfg["score_bands"][b].get("label", "")) for b in BAND_ORDER_HIGH_TO_LOW}
        report = build_report(
            snapshot_dates=[d for d, _ in snaps],
            latest_scores=list(latest.values()),
            pooled_scores=pooled,
            current=current,
            proposed=proposed,
            targets_pct=targets,
            min_width=args.min_width,
            min_tickers=args.min_tickers,
            guard_overridden=args.min_tickers < DEFAULT_MIN_TICKERS,
            labels=labels,
        )
        print(report)

        if not args.write:
            print("read-only: weights.json not modified (pass --write to apply).")
            return 0
        if proposed == current:
            print("--write: proposed cutoffs equal current ones; nothing to do.")
            return 0

        today = args.today or _date.today().isoformat()
        provenance = {
            "calibrated_at": today,
            "calibrated_from": [d for d, _ in snaps],
            "ticker_count": len(latest),
            "observations": len(pooled),
            "method": METHOD,
            "targets_pct": {b: targets[b] for b in BAND_ORDER_HIGH_TO_LOW},
            "min_width": args.min_width,
            "min_tickers_guard": args.min_tickers,
            "previous_cutoffs": {b: [current[b][0], current[b][1]] for b in BAND_ORDER_HIGH_TO_LOW},
            "tool": "scripts/lthcs_calibrate_bands.py",
        }
        new_cfg = apply_ranges(weights_cfg, proposed, provenance, today)
        write_weights(args.weights, weights_text, new_cfg)
        print(f"--write: updated {args.weights} (score_bands + score_bands_calibration).")
        return 0
    except CalibrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
