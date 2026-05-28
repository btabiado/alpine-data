#!/usr/bin/env python3
"""Build the LTHCS public read-only data manifest.

Emits ``data/lthcs/public/manifest.json`` and a stable
``data/lthcs/public/latest_snapshot.json`` mirror of today's dated snapshot.

The manifest is a single discoverable index for external consumers of the
already-public ``/data/lthcs/`` tree on the Pages-deployed mirror. The
underlying JSON files (snapshots, narratives, history, weights, backtests)
are already served as static assets — this script just publishes a
"table of contents" pointing at them so callers don't have to guess paths.

Phase 4 follow-on (public read-only data export). Intentionally a static
JSON producer, NOT a live API server.

Idempotency: running twice on the same inputs produces byte-identical
output (sorted keys, fixed indentation, deterministic ordering of
endpoint rows). ``generated_at`` is the one exception — when callers
care about reproducibility they should pass ``--frozen-time``.

CLI::

    python -m scripts.lthcs_build_public_manifest
    python -m scripts.lthcs_build_public_manifest --data-root /tmp/lthcs
    python -m scripts.lthcs_build_public_manifest --frozen-time 2026-05-20T23:00:00Z
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Try to import the canonical model version. The script falls back to a
# hardcoded constant when run outside the project root (e.g. via cron with
# a slim PYTHONPATH) so it never silently mis-stamps the manifest.
_FALLBACK_VERSION = "v1.1.0"
try:
    from lthcs import MODEL_VERSION as _MODEL_VERSION  # type: ignore
except Exception:  # pragma: no cover - import-only fallback path
    _MODEL_VERSION = _FALLBACK_VERSION


# Pillar + band vocabularies are stable enough to live as module constants
# here. They're sourced from lthcs/score.py and lthcs/normalize.py and
# match what's emitted in snapshot rows. If either of those modules ever
# diverges, the manifest test will catch it (it imports both lists from
# this module and round-trips them through json).
PILLARS: Tuple[str, ...] = (
    "adoption_momentum",
    "institutional_confidence",
    "financial_evolution",
    "thesis_integrity",
    "des",
)

BANDS: Tuple[str, ...] = (
    "elite",
    "high_confidence",
    "constructive",
    "monitor",
    "weakening",
    "review",
)

LICENSE_TEXT = (
    "Read-only public mirror. Data is for informational use only — "
    "not investment advice."
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _project_root() -> Path:
    """Return the repo root (parent of ``scripts/``)."""
    return Path(__file__).resolve().parent.parent


def _resolve_data_root(data_root: Optional[Path]) -> Path:
    if data_root is not None:
        return Path(data_root)
    return _project_root() / "data" / "lthcs"


def _latest_snapshot_date(data_root: Path) -> Optional[str]:
    """Pick the most recent dated snapshot under ``snapshots/``.

    Returns ``None`` if no dated snapshot exists yet (fresh checkout, or
    universe-only state).
    """
    snaps_dir = data_root / "snapshots"
    if not snaps_dir.is_dir():
        return None
    candidates: List[str] = []
    for path in snaps_dir.iterdir():
        if not path.is_file() or path.suffix != ".json":
            continue
        stem = path.stem
        # Skip index.json and any non-date helper files.
        if len(stem) == 10 and stem[4] == "-" and stem[7] == "-":
            candidates.append(stem)
    if not candidates:
        return None
    candidates.sort()
    return candidates[-1]


def _count_active(items: List[Dict[str, Any]]) -> int:
    """Count entries with ``active`` truthy (defaulting to True)."""
    return sum(1 for it in items if it.get("active", True))


def _universe_size(data_root: Path) -> int:
    """Active ticker count from ``universe.json``. Returns 0 on missing file."""
    path = data_root / "universe.json"
    if not path.is_file():
        return 0
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return 0
    tickers = payload.get("tickers") or []
    return _count_active(tickers)


def _crypto_universe_size(data_root: Path) -> int:
    path = data_root / "crypto_universe.json"
    if not path.is_file():
        return 0
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return 0
    assets = payload.get("assets") or []
    return _count_active(assets)


def _build_endpoints(latest_date: Optional[str]) -> List[Dict[str, str]]:
    """Build the data_endpoints array.

    Endpoint paths are written as URL-style strings relative to the Pages
    site root. We don't probe the filesystem for existence — these are
    advertised contract paths, and a freshly-deployed mirror might not
    yet have every dated file. Templated paths use ``<DATE>``, ``<TICKER>``,
    and ``<RUN_ID>`` placeholders so the contract is self-documenting.
    """
    date_token = latest_date or "<DATE>"
    return [
        {
            "endpoint": f"/data/lthcs/snapshots/{date_token}.json",
            "description": (
                "Full-universe LTHCS scores for the given trading day "
                "(equity universe — DJIA + NDX + S&P 100, deduped)."
            ),
            "shape": (
                "{ calc_date, model_version, scores: [{ ticker, lthcs_score, "
                "band, subscores, modifiers, ... }] }"
            ),
        },
        {
            "endpoint": f"/data/lthcs/snapshots_crypto/{date_token}.json",
            "description": (
                "Crypto-universe LTHCS scores for the given day "
                "(BTC, ETH, SOL, ADA, AVAX, DOT, LINK, POL, XRP, DOGE)."
            ),
            "shape": (
                "{ calc_date, model_version, scores: [{ ticker, lthcs_score, "
                "band, subscores, ... }] }"
            ),
        },
        {
            "endpoint": "/data/lthcs/history/by_ticker/<TICKER>.json",
            "description": (
                "Per-ticker time series of composite scores + bands "
                "(append-only history file, one per ticker)."
            ),
            "shape": "{ ticker, rows: [{ calc_date, lthcs_score, band, ... }] }",
        },
        {
            "endpoint": f"/data/lthcs/narratives/{date_token}.json",
            "description": (
                "Templated per-ticker narrative blurbs for the given day "
                "(always available — no LLM required)."
            ),
            "shape": "{ calc_date, model_version, narratives: [{ ticker, text, ... }] }",
        },
        {
            "endpoint": f"/data/lthcs/narratives_llm/{date_token}.json",
            "description": (
                "LLM-authored narratives for the given day. Gated — only "
                "present on days the LLM SHADOW path ran successfully."
            ),
            "shape": "{ calc_date, model_version, narratives: [{ ticker, text, ... }] }",
        },
        {
            "endpoint": f"/data/lthcs/variable_detail/{date_token}.json",
            "description": (
                "Per-(ticker, pillar) breakdown of sub-scores + raw "
                "component values for the given day."
            ),
            "shape": (
                "{ calc_date, model_version, rows: [{ ticker, pillar, "
                "sub_score, components, data_quality }] }"
            ),
        },
        {
            "endpoint": "/data/lthcs/backtest/<RUN_ID>/equity_curve.json",
            "description": (
                "Backtest equity curve for a given run id (LTHCS-band-weighted "
                "vs. equal-weight + SPY baselines)."
            ),
            "shape": "{ run_id, dates, strategies: { lthcs_weighted: [...], ... } }",
        },
        {
            "endpoint": "/data/lthcs/weights.json",
            "description": (
                "Pillar weights by maturity stage (equity + crypto profiles). "
                "Updated when the model is retuned."
            ),
            "shape": "{ version, profiles: { <profile>: [w1, w2, w3, w4, w5] } }",
        },
        {
            "endpoint": "/data/lthcs/public/universe.csv",
            "description": (
                "Bulk CSV export of the latest universe snapshot — one row "
                "per ticker, spreadsheet-ready (Excel / pandas / Sheets). "
                "Multi-value fields (dropped_pillars, data_quality_flags) "
                "are semicolon-joined."
            ),
            "shape": (
                "header: ticker, calc_date, lthcs_score, band, "
                "confidence_level, adoption_momentum, institutional_confidence, "
                "financial_evolution, thesis_integrity, des, dropped_pillars, "
                "data_quality_flags, drift_1d, drift_7d, drift_30d, drift_90d, "
                "sector, maturity_stage"
            ),
        },
    ]


def build_manifest(
    data_root: Path,
    *,
    frozen_time: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble the manifest dict from on-disk data.

    Args:
        data_root: ``data/lthcs/`` directory.
        frozen_time: optional ISO-8601 string to pin ``generated_at`` for
            deterministic snapshot tests. When ``None`` we use UTC now.
    """
    latest_date = _latest_snapshot_date(data_root)
    return {
        "generated_at": frozen_time or _utc_now_iso(),
        "latest_snapshot_date": latest_date,
        "universe_size": _universe_size(data_root),
        "crypto_universe_size": _crypto_universe_size(data_root),
        "pillars": list(PILLARS),
        "bands": list(BANDS),
        "data_endpoints": _build_endpoints(latest_date),
        "version": _MODEL_VERSION,
        "license": LICENSE_TEXT,
    }


def write_manifest(
    data_root: Path,
    manifest: Dict[str, Any],
) -> Path:
    """Write the manifest under ``data/lthcs/public/manifest.json``.

    Returns the path written. Idempotent on byte content (sorted keys,
    explicit indent, trailing newline).
    """
    public_dir = data_root / "public"
    public_dir.mkdir(parents=True, exist_ok=True)
    out = public_dir / "manifest.json"
    text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    out.write_text(text)
    return out


def mirror_latest_snapshot(data_root: Path, latest_date: Optional[str]) -> Optional[Path]:
    """Copy ``snapshots/<latest>.json`` to ``public/latest_snapshot.json``.

    Stable filename so consumers can hard-code a single URL without
    having to first read the manifest to discover today's date. No-op
    when there's no dated snapshot yet.
    """
    if latest_date is None:
        return None
    src = data_root / "snapshots" / f"{latest_date}.json"
    if not src.is_file():
        return None
    public_dir = data_root / "public"
    public_dir.mkdir(parents=True, exist_ok=True)
    dst = public_dir / "latest_snapshot.json"
    # shutil.copyfile is byte-exact (no metadata copy) which keeps the
    # idempotency test simple: src and dst hash equal.
    shutil.copyfile(src, dst)
    return dst


def build_and_write(
    data_root: Optional[Path] = None,
    *,
    frozen_time: Optional[str] = None,
) -> Tuple[Path, Optional[Path]]:
    """Top-level helper used by both the CLI and the daily pipeline.

    Returns (manifest_path, latest_snapshot_path_or_None).
    """
    root = _resolve_data_root(data_root)
    manifest = build_manifest(root, frozen_time=frozen_time)
    manifest_path = write_manifest(root, manifest)
    snap_path = mirror_latest_snapshot(root, manifest.get("latest_snapshot_date"))
    return manifest_path, snap_path


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--data-root",
        type=Path,
        default=None,
        help="Override data/lthcs/ root (defaults to repo data/lthcs/).",
    )
    p.add_argument(
        "--frozen-time",
        type=str,
        default=None,
        help=(
            "Pin generated_at to this ISO-8601 string (for tests / "
            "deterministic snapshots). Defaults to UTC now."
        ),
    )
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    manifest_path, snap_path = build_and_write(
        data_root=args.data_root,
        frozen_time=args.frozen_time,
    )
    print(f"✓ wrote {manifest_path}")
    if snap_path is not None:
        print(f"✓ mirrored latest snapshot → {snap_path}")
    else:
        print("• no dated snapshot found; latest_snapshot.json not written")
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI entry point
    raise SystemExit(main())
