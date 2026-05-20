"""LTHCS per-pillar signal quality audit (Phase 3 task 3.1) — READ-ONLY.

Run: .venv/bin/python scripts/lthcs_pillar_quality_audit.py [--asof YYYY-MM-DD] [--out PATH]

For each of the 5 pillars (adoption_momentum, institutional_confidence,
financial_evolution, thesis_integrity, des), reports:

- Coverage: % of universe with a non-default (!=50.0) sub-score
- Distribution: mean, stdev, p5/p25/p50/p75/p95
- Floor/ceiling counts: how many at exactly 0 or exactly 100
- Top-5 / Bottom-5 tickers
- Per-cohort mean (maturity_stage from universe.json)
- 30-day per-ticker stdev across snapshots (median across universe)

Emits a markdown report at data/lthcs/quality_audit/<asof>_pillar_quality.md.
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Iterable

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data" / "lthcs"
SNAP_DIR = DATA / "snapshots"
UNIVERSE_FILE = DATA / "universe.json"
OUT_DIR = DATA / "quality_audit"

PILLARS = [
    "adoption_momentum",
    "institutional_confidence",
    "financial_evolution",
    "thesis_integrity",
    "des",
]

DEFAULT_VALUE = 50.0  # treated as "no real signal"
FLOOR = 0.0
CEILING = 100.0


def jload(path: Path):
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def latest_snapshot_date(snap_dir: Path = SNAP_DIR) -> str:
    dates = sorted(
        p.stem for p in snap_dir.glob("*.json") if p.stem != "index"
    )
    if not dates:
        raise FileNotFoundError(f"No snapshots in {snap_dir}")
    return dates[-1]


def load_snapshot(asof: str, snap_dir: Path = SNAP_DIR) -> dict:
    path = snap_dir / f"{asof}.json"
    data = jload(path)
    if data is None:
        raise FileNotFoundError(path)
    return data


def cohort_map(universe_file: Path = UNIVERSE_FILE) -> dict[str, str]:
    u = jload(universe_file) or {}
    tickers = u.get("tickers", [])
    return {t["ticker"]: t.get("maturity_stage", "unknown") for t in tickers}


def percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    xs = sorted(values)
    n = len(xs)
    k = max(0, min(n - 1, int(round(p * (n - 1)))))
    return xs[k]


def pillar_stats(values: list[float]) -> dict:
    """Distribution stats for one pillar over all tickers."""
    if not values:
        return dict(n=0)
    n = len(values)
    mean = statistics.fmean(values)
    stdev = statistics.pstdev(values) if n > 1 else 0.0
    return dict(
        n=n,
        mean=round(mean, 2),
        stdev=round(stdev, 2),
        p5=round(percentile(values, 0.05), 2),
        p25=round(percentile(values, 0.25), 2),
        p50=round(percentile(values, 0.50), 2),
        p75=round(percentile(values, 0.75), 2),
        p95=round(percentile(values, 0.95), 2),
        count_at_floor=sum(1 for v in values if v == FLOOR),
        count_at_ceiling=sum(1 for v in values if v == CEILING),
    )


def coverage_pct(values: list[float], default: float = DEFAULT_VALUE) -> float:
    """% of values that are NOT the default placeholder."""
    if not values:
        return 0.0
    real = sum(1 for v in values if v != default)
    return round(100.0 * real / len(values), 1)


def top_bottom(records: list[dict], pillar: str, k: int = 5) -> tuple[list, list]:
    have = [r for r in records if pillar in r.get("subscores", {})]
    ranked = sorted(have, key=lambda r: r["subscores"][pillar])
    bottom = [(r["ticker"], r["subscores"][pillar]) for r in ranked[:k]]
    top = [(r["ticker"], r["subscores"][pillar]) for r in ranked[-k:][::-1]]
    return top, bottom


def cohort_means(records: list[dict], pillar: str, cohorts: dict[str, str]) -> dict[str, float]:
    buckets: dict[str, list[float]] = defaultdict(list)
    for r in records:
        sub = r.get("subscores", {})
        if pillar not in sub:
            continue
        c = cohorts.get(r["ticker"], r.get("maturity_stage", "unknown"))
        buckets[c].append(sub[pillar])
    return {c: round(statistics.fmean(vs), 2) for c, vs in buckets.items() if vs}


def collect_pillar_history(
    snap_dir: Path, window: Iterable[str], pillar: str
) -> dict[str, list[float]]:
    """For each ticker, the list of pillar scores across the given snapshot dates."""
    per_ticker: dict[str, list[float]] = defaultdict(list)
    for date in window:
        snap = jload(snap_dir / f"{date}.json")
        if not snap:
            continue
        for rec in snap.get("scores", []):
            sub = rec.get("subscores", {})
            if pillar in sub:
                per_ticker[rec["ticker"]].append(sub[pillar])
    return per_ticker


def per_ticker_stdev(per_ticker: dict[str, list[float]]) -> list[float]:
    out = []
    for _, vs in per_ticker.items():
        if len(vs) >= 2:
            out.append(statistics.pstdev(vs))
    return out


def recent_window(snap_dir: Path, asof: str, days: int) -> list[str]:
    all_dates = sorted(
        p.stem for p in snap_dir.glob("*.json") if p.stem != "index"
    )
    if asof in all_dates:
        idx = all_dates.index(asof)
        return all_dates[max(0, idx - days + 1) : idx + 1]
    return all_dates[-days:]


def build_report(asof: str, snap_dir: Path = SNAP_DIR) -> str:
    snap = load_snapshot(asof, snap_dir)
    records = snap["scores"]
    cohorts = cohort_map()
    window30 = recent_window(snap_dir, asof, 30)

    lines: list[str] = []
    lines.append(f"# LTHCS Pillar Quality Audit — {asof}\n")
    lines.append(
        f"Snapshot: `data/lthcs/snapshots/{asof}.json` "
        f"(model_version={snap.get('model_version')}, n={len(records)})\n"
    )
    lines.append(
        f"Stability window: {len(window30)} snapshots "
        f"({window30[0]} -> {window30[-1]})\n"
    )

    for pillar in PILLARS:
        values = [r["subscores"][pillar] for r in records if pillar in r["subscores"]]
        s = pillar_stats(values)
        cov = coverage_pct(values)
        top, bottom = top_bottom(records, pillar)
        cmeans = cohort_means(records, pillar, cohorts)
        hist = collect_pillar_history(snap_dir, window30, pillar)
        ticker_stdevs = per_ticker_stdev(hist)
        median_stdev = round(statistics.median(ticker_stdevs), 2) if ticker_stdevs else 0.0
        mean_stdev = round(statistics.fmean(ticker_stdevs), 2) if ticker_stdevs else 0.0

        lines.append(f"\n## {pillar}\n")
        lines.append(f"- Coverage (non-default 50.0): **{cov}%** ({s['n']} tickers)")
        lines.append(
            f"- Mean **{s['mean']}** | stdev {s['stdev']} | "
            f"p5/p25/p50/p75/p95 = {s['p5']}/{s['p25']}/{s['p50']}/{s['p75']}/{s['p95']}"
        )
        lines.append(
            f"- Floor (==0): {s['count_at_floor']} | Ceiling (==100): {s['count_at_ceiling']}"
        )
        lines.append(
            f"- 30d per-ticker stdev: median {median_stdev}, mean {mean_stdev} "
            f"(n_tickers_with_history={len(ticker_stdevs)})"
        )
        lines.append("- Cohort means: " + ", ".join(f"{c}={m}" for c, m in sorted(cmeans.items())))
        lines.append("- Top 5: " + ", ".join(f"{t}={v}" for t, v in top))
        lines.append("- Bottom 5: " + ", ".join(f"{t}={v}" for t, v in bottom))

    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", default=None, help="Snapshot date, default latest available")
    ap.add_argument("--out", default=None, help="Output markdown path")
    args = ap.parse_args()

    asof = args.asof or latest_snapshot_date()
    report = build_report(asof)
    out_path = Path(args.out) if args.out else OUT_DIR / f"{asof}_pillar_quality.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report)
    print(f"wrote {out_path} ({len(report)} chars)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
