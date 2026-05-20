#!/usr/bin/env python3
"""Bulk CSV export of the LTHCS universe (Phase 5 ETA follow-on).

Emits ``data/lthcs/public/universe.csv`` — a spreadsheet-friendly flat
file with one row per ticker for the latest (or a specified historical)
snapshot. Power users want this for offline analysis: Excel, pandas,
Google Sheets, R, etc. all consume CSV natively, and the existing public
JSON manifest (see ``scripts/lthcs_build_public_manifest.py``) is great
for code but less ergonomic for ad-hoc spreadsheet work.

The output is intentionally a flat table:

  ticker, calc_date, lthcs_score, band, confidence_level,
  adoption_momentum, institutional_confidence, financial_evolution,
  thesis_integrity, des, dropped_pillars, data_quality_flags,
  drift_1d, drift_7d, drift_30d, drift_90d, sector, maturity_stage

Multi-value fields (``dropped_pillars``, ``data_quality_flags``) are
joined with semicolons so the row stays CSV-safe (no embedded commas).

Flags:
  --asof YYYY-MM-DD            Export a historical snapshot instead of latest.
  --include-effective-weights  Append 5 columns of effective pillar weights.
  --data-root <path>           Override data/lthcs/ root (for tests).
  --out <path>                 Override output path (default: <root>/public/universe.csv).

The script is pure-I/O — fixture a synthetic data root and the CSV comes
out byte-deterministically (rows sorted by ticker, numeric values
formatted with ``repr``-equivalent precision).

CLI::

    python -m scripts.lthcs_export_csv
    python -m scripts.lthcs_export_csv --include-effective-weights
    python -m scripts.lthcs_export_csv --asof 2026-05-17
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Base column set (always present). Order is the contract — ALPHA's
# button + downstream consumers depend on this header.
BASE_COLUMNS: Tuple[str, ...] = (
    "ticker",
    "calc_date",
    "lthcs_score",
    "band",
    "confidence_level",
    "adoption_momentum",
    "institutional_confidence",
    "financial_evolution",
    "thesis_integrity",
    "des",
    "dropped_pillars",
    "data_quality_flags",
    "drift_1d",
    "drift_7d",
    "drift_30d",
    "drift_90d",
    "sector",
    "maturity_stage",
)

# Extra columns appended when --include-effective-weights is set. Pillar
# order matches lthcs.score.PILLAR_ORDER (also reflected in BASE_COLUMNS).
EFFECTIVE_WEIGHT_COLUMNS: Tuple[str, ...] = (
    "effective_weight_adoption_momentum",
    "effective_weight_institutional_confidence",
    "effective_weight_financial_evolution",
    "effective_weight_thesis_integrity",
    "effective_weight_des",
)

# Multi-value list fields → joined with this delimiter. Semicolon keeps
# the cell CSV-safe (no quoting needed) and is the conventional join
# character for "list of tags" in flat exports.
LIST_JOIN = ";"


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _resolve_data_root(data_root: Optional[Path]) -> Path:
    if data_root is not None:
        return Path(data_root)
    return _project_root() / "data" / "lthcs"


def _is_date_stem(stem: str) -> bool:
    return len(stem) == 10 and stem[4] == "-" and stem[7] == "-"


def _latest_snapshot_date(data_root: Path) -> Optional[str]:
    """Pick the most recent dated snapshot under ``snapshots/``."""
    snaps_dir = data_root / "snapshots"
    if not snaps_dir.is_dir():
        return None
    candidates: List[str] = []
    for path in snaps_dir.iterdir():
        if not path.is_file() or path.suffix != ".json":
            continue
        if _is_date_stem(path.stem):
            candidates.append(path.stem)
    if not candidates:
        return None
    candidates.sort()
    return candidates[-1]


def _load_snapshot(data_root: Path, calc_date: str) -> Dict[str, Any]:
    """Load a dated snapshot JSON. Raises FileNotFoundError if missing."""
    path = data_root / "snapshots" / f"{calc_date}.json"
    if not path.is_file():
        raise FileNotFoundError(f"snapshot not found: {path}")
    return json.loads(path.read_text())


def _format_number(value: Any) -> str:
    """Format a numeric field for the CSV.

    - ``None``/missing → empty string (so spreadsheets read it as blank
      rather than the literal text ``None``).
    - ``float`` → ``repr``-equivalent (no scientific notation for normal
      ranges, full precision preserved). We don't round — the upstream
      snapshot already rounds scores to one decimal.
    - ``int`` / ``str`` passes through.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        # bool is a subclass of int — handle before int to avoid "True" → "1".
        return "true" if value else "false"
    if isinstance(value, float):
        # Strip the trailing ".0" on whole-number floats (e.g. 0.0 → "0.0"
        # is fine, but 28.1 → "28.1" not "28.100000000000001"). Python's
        # default ``str(float)`` already does the shortest round-trippable
        # representation, so just use that.
        return str(value)
    return str(value)


def _join_list(values: Optional[Iterable[Any]]) -> str:
    """Join a list of stringy values with the LIST_JOIN delimiter.

    None or empty → empty string. Non-string elements are coerced.
    """
    if not values:
        return ""
    return LIST_JOIN.join(str(v) for v in values)


def _row_for_score(row: Dict[str, Any], calc_date: str, *, include_eff: bool) -> List[str]:
    """Flatten one score dict into a CSV row.

    Missing fields default to empty strings — the CSV's column contract
    stays fixed regardless of which optional pillars / drifts / sector
    metadata a given ticker carries.
    """
    sub = row.get("subscores") or {}
    out: List[str] = [
        _format_number(row.get("ticker")),
        calc_date,
        _format_number(row.get("lthcs_score")),
        _format_number(row.get("band")),
        _format_number(row.get("confidence_level")),
        _format_number(sub.get("adoption_momentum")),
        _format_number(sub.get("institutional_confidence")),
        _format_number(sub.get("financial_evolution")),
        _format_number(sub.get("thesis_integrity")),
        _format_number(sub.get("des")),
        _join_list(row.get("dropped_pillars")),
        _join_list(row.get("data_quality_flags")),
        _format_number(row.get("drift_1d")),
        _format_number(row.get("drift_7d")),
        _format_number(row.get("drift_30d")),
        _format_number(row.get("drift_90d")),
        _format_number(row.get("sector")),
        _format_number(row.get("maturity_stage")),
    ]
    if include_eff:
        eff = row.get("effective_weights") or []
        # Always emit 5 columns; pad missing trailing weights with empty.
        for i in range(5):
            out.append(_format_number(eff[i]) if i < len(eff) else "")
    return out


def build_csv_text(
    snapshot: Dict[str, Any],
    *,
    include_effective_weights: bool = False,
) -> str:
    """Build the CSV text from a loaded snapshot dict.

    Rows are sorted by ticker for stable, diff-friendly output. The
    header is always emitted, even for an empty snapshot (so callers
    can detect "no rows" vs "no file" cleanly).
    """
    calc_date = snapshot.get("calc_date") or ""
    scores: List[Dict[str, Any]] = list(snapshot.get("scores") or [])
    # Sort by ticker — keeps git diffs noise-free when the universe
    # stays stable day-over-day.
    scores.sort(key=lambda r: str(r.get("ticker") or ""))

    columns = list(BASE_COLUMNS)
    if include_effective_weights:
        columns.extend(EFFECTIVE_WEIGHT_COLUMNS)

    # ``csv.writer`` handles quoting for any cell that happens to contain
    # a comma / quote / newline. We use a StringIO buffer + ``lineterminator``
    # of ``\n`` so the output is identical across platforms (the stdlib
    # default is ``\r\n``, which would create cross-OS diff churn).
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(columns)
    for row in scores:
        writer.writerow(
            _row_for_score(row, calc_date, include_eff=include_effective_weights)
        )
    return buf.getvalue()


def write_csv(
    data_root: Path,
    csv_text: str,
    *,
    out_path: Optional[Path] = None,
) -> Path:
    """Write the CSV to ``data/lthcs/public/universe.csv`` (or override)."""
    if out_path is None:
        out_path = data_root / "public" / "universe.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(csv_text)
    return out_path


def build_and_write(
    data_root: Optional[Path] = None,
    *,
    asof: Optional[str] = None,
    include_effective_weights: bool = False,
    out_path: Optional[Path] = None,
) -> Tuple[Path, str]:
    """Top-level helper used by both the CLI and the daily pipeline.

    Returns (output_path, resolved_calc_date). Raises FileNotFoundError
    if no snapshot exists at all (fresh repo) or if --asof points to a
    missing date.
    """
    root = _resolve_data_root(data_root)
    if asof is not None:
        calc_date = asof
    else:
        latest = _latest_snapshot_date(root)
        if latest is None:
            raise FileNotFoundError(
                f"no dated snapshots found under {root / 'snapshots'}"
            )
        calc_date = latest
    snapshot = _load_snapshot(root, calc_date)
    csv_text = build_csv_text(
        snapshot, include_effective_weights=include_effective_weights
    )
    written = write_csv(root, csv_text, out_path=out_path)
    return written, calc_date


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--data-root",
        type=Path,
        default=None,
        help="Override data/lthcs/ root (defaults to repo data/lthcs/).",
    )
    p.add_argument(
        "--asof",
        type=str,
        default=None,
        help=(
            "Export the snapshot for this YYYY-MM-DD date instead of "
            "the latest. Errors out if the snapshot file is missing."
        ),
    )
    p.add_argument(
        "--include-effective-weights",
        action="store_true",
        help=(
            "Append 5 columns of effective pillar weights "
            "(post-renormalization, after dropped pillars are removed)."
        ),
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "Override output path (default: <data-root>/public/universe.csv)."
        ),
    )
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    try:
        out_path, calc_date = build_and_write(
            data_root=args.data_root,
            asof=args.asof,
            include_effective_weights=args.include_effective_weights,
            out_path=args.out,
        )
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {out_path} (calc_date={calc_date})")
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI entry point
    raise SystemExit(main())
