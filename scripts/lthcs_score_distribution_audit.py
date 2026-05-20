"""LTHCS composite-score distribution + outlier + cross-pillar audit.

Phase 3 tasks 3.2 / 3.3 / 3.4. Produces two markdown reports under
``data/lthcs/quality_audit/`` plus a verdict-ready dataset on stdout.

Sections produced:

* **Distribution** — mean/stdev, p5/p25/p50/p75/p95, ASCII histogram of
  10-point composite-score bins, per-band cohort counts compared with
  the band thresholds documented in ``lthcs/score.py``/weights.json,
  and a per-maturity-stage breakdown.
* **Outliers** — top 5 / bottom 5 composite tickers with their pillar
  breakdown, pillar-vs-cohort z-score outliers, and "stuck" tickers
  (|drift_30d| < 5).
* **Correlation** — 5x5 Pearson pillar correlation matrix with
  near-redundant (>0.7) and near-orthogonal (<0.2) pillar pairs, plus
  a 30-day stability scan.

The script is read-only against snapshots and weights.json. Outputs:

* ``data/lthcs/quality_audit/<date>_composite_distribution.md``
* ``data/lthcs/quality_audit/<date>_pillar_correlation.md``

Usage::

    python scripts/lthcs_score_distribution_audit.py [--date YYYY-MM-DD]

If ``--date`` is not provided, the most recent snapshot under
``data/lthcs/snapshots/`` is used. ``--label`` (default = date string)
controls the prefix used for output filenames so the script can emit
``2026-05-19_*`` reports when the latest underlying snapshot is dated
2026-05-18 (the pre-pipeline-run case for an in-day audit).
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SNAPSHOT_DIR = REPO_ROOT / "data" / "lthcs" / "snapshots"
WEIGHTS_PATH = REPO_ROOT / "data" / "lthcs" / "weights.json"
AUDIT_DIR = REPO_ROOT / "data" / "lthcs" / "quality_audit"

PILLAR_ORDER = (
    "adoption_momentum",
    "institutional_confidence",
    "financial_evolution",
    "thesis_integrity",
    "des",
)

# Documented band thresholds from lthcs/score.py / weights.json. Kept here
# as a *read-only* compile-time copy so the audit can run even if weights.json
# is being touched concurrently by a sibling task (it is — GG owns that file).
DEFAULT_BANDS = [
    ("review", 0, 49),
    ("weakening", 50, 59),
    ("monitor", 60, 69),
    ("constructive", 70, 79),
    ("high_confidence", 80, 84),
    ("elite", 85, 100),
]

# 10-point histogram bins covering [0, 100]. The last bin is (90, 100)
# inclusive of 100; all others are right-open (e.g. 80-89 = [80, 90)).
HIST_BINS: List[Tuple[int, int]] = [(lo, lo + 9) for lo in range(0, 90, 10)] + [(90, 100)]


# ---------------------------------------------------------------------------
# Snapshot loading
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")


def list_snapshot_dates(snapshot_dir: Path = SNAPSHOT_DIR) -> List[str]:
    """Return all dated snapshot filenames (descending date order)."""
    out: List[str] = []
    if not snapshot_dir.exists():
        return out
    for p in snapshot_dir.iterdir():
        m = _DATE_RE.match(p.name)
        if m:
            out.append(m.group(1))
    return sorted(out, reverse=True)


def latest_snapshot_date(snapshot_dir: Path = SNAPSHOT_DIR) -> Optional[str]:
    """Most-recent snapshot date in ``snapshot_dir`` (YYYY-MM-DD) or None."""
    dates = list_snapshot_dates(snapshot_dir)
    return dates[0] if dates else None


def load_snapshot(date: str, snapshot_dir: Path = SNAPSHOT_DIR) -> Dict[str, Any]:
    """Read ``<snapshot_dir>/<date>.json`` and return the parsed object."""
    path = snapshot_dir / f"{date}.json"
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Universe extraction
# ---------------------------------------------------------------------------

def extract_scores(snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Pull the score list out of a snapshot envelope. List passthrough OK."""
    if isinstance(snapshot, list):
        return list(snapshot)
    if isinstance(snapshot, dict):
        if "scores" in snapshot and isinstance(snapshot["scores"], list):
            return list(snapshot["scores"])
    return []


# ---------------------------------------------------------------------------
# Math helpers (pure-Python; no numpy dependency to match score.py)
# ---------------------------------------------------------------------------

def percentile(values: Sequence[float], pct: float) -> Optional[float]:
    """Linear-interpolation percentile, ignoring None/NaN. Matches score._percentile."""
    cleaned: List[float] = []
    for v in values:
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f != f:  # NaN
            continue
        cleaned.append(f)
    if not cleaned:
        return None
    cleaned.sort()
    if len(cleaned) == 1:
        return cleaned[0]
    rank = (pct / 100.0) * (len(cleaned) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(cleaned) - 1)
    frac = rank - lo
    return cleaned[lo] + (cleaned[hi] - cleaned[lo]) * frac


def summary_stats(values: Sequence[float]) -> Dict[str, Any]:
    """Mean / stdev / count / percentiles for a numeric sample."""
    cleaned = [float(v) for v in values if v is not None and not (isinstance(v, float) and v != v)]
    n = len(cleaned)
    if n == 0:
        return {
            "count": 0,
            "mean": None,
            "stdev": None,
            "p5": None,
            "p25": None,
            "p50": None,
            "p75": None,
            "p95": None,
            "min": None,
            "max": None,
        }
    mean = sum(cleaned) / n
    stdev = statistics.pstdev(cleaned) if n > 1 else 0.0
    return {
        "count": n,
        "mean": round(mean, 2),
        "stdev": round(stdev, 2),
        "p5": round(percentile(cleaned, 5.0), 2),
        "p25": round(percentile(cleaned, 25.0), 2),
        "p50": round(percentile(cleaned, 50.0), 2),
        "p75": round(percentile(cleaned, 75.0), 2),
        "p95": round(percentile(cleaned, 95.0), 2),
        "min": round(min(cleaned), 2),
        "max": round(max(cleaned), 2),
    }


def histogram(
    values: Sequence[float],
    bins: Sequence[Tuple[int, int]] = HIST_BINS,
) -> List[Tuple[Tuple[int, int], int]]:
    """Bucket ``values`` into ``bins`` (inclusive intervals)."""
    out = [((lo, hi), 0) for (lo, hi) in bins]
    counts = [0] * len(bins)
    for v in values:
        if v is None:
            continue
        try:
            x = float(v)
        except (TypeError, ValueError):
            continue
        if x != x:
            continue
        x_clamped = max(0.0, min(100.0, x))
        for i, (lo, hi) in enumerate(bins):
            # Each bin is [lo, hi+1) except the last bin which is [lo, hi]
            # (so the score 100 lands in the top bin, not off-by-one).
            if i == len(bins) - 1:
                if lo <= x_clamped <= hi + 1e-9:
                    counts[i] += 1
                    break
            else:
                if lo <= x_clamped < hi + 1:
                    counts[i] += 1
                    break
    return [(bins[i], counts[i]) for i in range(len(bins))]


def ascii_histogram(
    hist: Sequence[Tuple[Tuple[int, int], int]],
    width: int = 40,
    label_fmt: str = "{lo:>3}-{hi:<3}",
) -> str:
    """Render a histogram as a left-aligned ASCII bar chart."""
    if not hist:
        return "(empty)"
    max_count = max((c for _, c in hist), default=0) or 1
    lines: List[str] = []
    for (lo, hi), c in hist:
        bar_len = int(round((c / max_count) * width)) if c else 0
        bar = "#" * bar_len
        lines.append(f"{label_fmt.format(lo=lo, hi=hi)} | {bar:<{width}} {c}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Distribution analysis
# ---------------------------------------------------------------------------

def band_counts(
    scores: Sequence[float],
    bands: Sequence[Tuple[str, int, int]] = DEFAULT_BANDS,
) -> List[Tuple[str, int, int, int]]:
    """Count how many scores fall in each band. Returns (name, lo, hi, count)."""
    counts: List[int] = [0] * len(bands)
    for v in scores:
        if v is None:
            continue
        try:
            x = float(v)
        except (TypeError, ValueError):
            continue
        if x != x:
            continue
        x_floor = int(math.floor(max(0.0, min(100.0, x))))
        for i, (_, lo, hi) in enumerate(bands):
            if lo <= x_floor <= hi:
                counts[i] += 1
                break
    return [(bands[i][0], bands[i][1], bands[i][2], counts[i]) for i in range(len(bands))]


def per_cohort_distribution(
    scoreset: Sequence[Dict[str, Any]],
    cohort_key: str = "maturity_stage",
) -> Dict[str, Dict[str, Any]]:
    """Group by cohort and compute summary + histogram for each cohort."""
    by_cohort: Dict[str, List[float]] = defaultdict(list)
    for row in scoreset:
        c = row.get(cohort_key) or "_unclassified"
        sc = row.get("lthcs_score")
        if sc is None:
            continue
        by_cohort[c].append(float(sc))
    out: Dict[str, Dict[str, Any]] = {}
    for cohort, vals in sorted(by_cohort.items()):
        out[cohort] = {
            "summary": summary_stats(vals),
            "histogram": histogram(vals),
            "bands": band_counts(vals),
        }
    return out


# ---------------------------------------------------------------------------
# Outliers
# ---------------------------------------------------------------------------

def top_bottom_n(
    scoreset: Sequence[Dict[str, Any]],
    n: int = 5,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return ``(top_n_rows, bottom_n_rows)`` sorted descending/ascending by score."""
    rows = [r for r in scoreset if isinstance(r.get("lthcs_score"), (int, float))]
    rows_sorted = sorted(rows, key=lambda r: float(r["lthcs_score"]), reverse=True)
    return rows_sorted[:n], rows_sorted[-n:][::-1]


def pillar_zscore_outliers(
    scoreset: Sequence[Dict[str, Any]],
    threshold: float = 2.0,
    top_n: int = 10,
) -> List[Dict[str, Any]]:
    """Find tickers whose pillar score is >``threshold`` stdev from their cohort mean
    on that pillar. Returns the ``top_n`` by absolute z-score.

    Cohort = maturity_stage. Cohorts of size <3 are skipped (insufficient sample).
    """
    by_cohort: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in scoreset:
        by_cohort[r.get("maturity_stage") or "_unclassified"].append(r)

    outliers: List[Dict[str, Any]] = []
    for cohort, rows in by_cohort.items():
        if len(rows) < 3:
            continue
        for pillar in PILLAR_ORDER:
            sample = [
                float(r.get("subscores", {}).get(pillar))
                for r in rows
                if isinstance(r.get("subscores", {}).get(pillar), (int, float))
            ]
            if len(sample) < 3:
                continue
            mu = sum(sample) / len(sample)
            sd = statistics.pstdev(sample)
            if sd <= 1e-9:
                continue
            for r in rows:
                v = r.get("subscores", {}).get(pillar)
                if not isinstance(v, (int, float)):
                    continue
                z = (float(v) - mu) / sd
                if abs(z) >= threshold:
                    outliers.append({
                        "ticker": r.get("ticker"),
                        "cohort": cohort,
                        "pillar": pillar,
                        "value": round(float(v), 2),
                        "cohort_mean": round(mu, 2),
                        "cohort_stdev": round(sd, 2),
                        "z": round(z, 2),
                        "composite": r.get("lthcs_score"),
                        "data_quality_flags": list(r.get("data_quality_flags") or []),
                    })
    outliers.sort(key=lambda d: abs(d["z"]), reverse=True)
    return outliers[:top_n]


def stuck_tickers(
    scoreset: Sequence[Dict[str, Any]],
    drift_threshold: float = 5.0,
) -> List[Dict[str, Any]]:
    """Tickers whose 30-day composite drift magnitude is below ``drift_threshold``.

    Returns rows sorted by |drift_30d| ascending (most-stuck first).
    """
    out: List[Dict[str, Any]] = []
    for r in scoreset:
        d30 = r.get("drift_30d")
        if not isinstance(d30, (int, float)):
            continue
        if abs(float(d30)) < drift_threshold:
            out.append({
                "ticker": r.get("ticker"),
                "composite": r.get("lthcs_score"),
                "band": r.get("band"),
                "drift_30d": float(d30),
                "drift_90d": r.get("drift_90d"),
                "maturity_stage": r.get("maturity_stage"),
                "data_quality_flags": list(r.get("data_quality_flags") or []),
            })
    out.sort(key=lambda r: abs(r["drift_30d"]))
    return out


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------

def pearson_corr(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    """Pearson correlation of two equal-length numeric sequences."""
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    sx = math.sqrt(sum((xs[i] - mx) ** 2 for i in range(n)))
    sy = math.sqrt(sum((ys[i] - my) ** 2 for i in range(n)))
    if sx <= 1e-12 or sy <= 1e-12:
        return None
    return num / (sx * sy)


def pillar_correlation_matrix(
    scoreset: Sequence[Dict[str, Any]],
) -> Dict[Tuple[str, str], Optional[float]]:
    """Compute the 5x5 pillar correlation matrix from a snapshot."""
    cols: Dict[str, List[float]] = {p: [] for p in PILLAR_ORDER}
    for r in scoreset:
        subs = r.get("subscores") or {}
        # Only include rows with all pillars present and numeric.
        if any(not isinstance(subs.get(p), (int, float)) for p in PILLAR_ORDER):
            continue
        for p in PILLAR_ORDER:
            cols[p].append(float(subs[p]))
    out: Dict[Tuple[str, str], Optional[float]] = {}
    for a in PILLAR_ORDER:
        for b in PILLAR_ORDER:
            out[(a, b)] = pearson_corr(cols[a], cols[b])
    return out


def classify_pillar_pairs(
    corr: Dict[Tuple[str, str], Optional[float]],
    high_thresh: float = 0.7,
    low_thresh: float = 0.2,
) -> Dict[str, List[Tuple[str, str, float]]]:
    """Bucket unordered pillar pairs as redundant (>=0.7) or orthogonal (<=0.2)."""
    redundant: List[Tuple[str, str, float]] = []
    orthogonal: List[Tuple[str, str, float]] = []
    seen: set = set()
    for (a, b), v in corr.items():
        if a == b or v is None:
            continue
        key = tuple(sorted((a, b)))
        if key in seen:
            continue
        seen.add(key)
        if abs(v) >= high_thresh:
            redundant.append((key[0], key[1], round(v, 3)))
        if abs(v) <= low_thresh:
            orthogonal.append((key[0], key[1], round(v, 3)))
    redundant.sort(key=lambda t: -abs(t[2]))
    orthogonal.sort(key=lambda t: abs(t[2]))
    return {"redundant": redundant, "orthogonal": orthogonal}


def correlation_stability(
    dates: Sequence[str],
    snapshot_dir: Path = SNAPSHOT_DIR,
) -> Dict[str, Any]:
    """Compute correlation matrices over ``dates`` and report per-pair stability.

    Returns ``{"per_pair": {(a,b): {"min": .., "max": .., "range": ..}}, "dates_used": [..]}``
    so callers can flag pairs that drift week-to-week.
    """
    per_pair: Dict[Tuple[str, str], List[float]] = defaultdict(list)
    used: List[str] = []
    for d in dates:
        path = snapshot_dir / f"{d}.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        scores = extract_scores(data)
        if not scores:
            continue
        corr = pillar_correlation_matrix(scores)
        used.append(d)
        seen: set = set()
        for (a, b), v in corr.items():
            if a == b or v is None:
                continue
            key = tuple(sorted((a, b)))
            if key in seen:
                continue
            seen.add(key)
            per_pair[key].append(v)
    out_pairs: Dict[Tuple[str, str], Dict[str, float]] = {}
    for k, vals in per_pair.items():
        if not vals:
            continue
        out_pairs[k] = {
            "min": round(min(vals), 3),
            "max": round(max(vals), 3),
            "range": round(max(vals) - min(vals), 3),
            "mean": round(sum(vals) / len(vals), 3),
            "n": len(vals),
        }
    return {"per_pair": out_pairs, "dates_used": used}


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

def render_distribution_report(
    snapshot_date: str,
    label: str,
    scoreset: Sequence[Dict[str, Any]],
) -> str:
    """Markdown text for the composite-distribution report."""
    composites = [r["lthcs_score"] for r in scoreset if isinstance(r.get("lthcs_score"), (int, float))]
    summary = summary_stats(composites)
    hist = histogram(composites)
    bands = band_counts(composites)

    cohort = per_cohort_distribution(scoreset)
    top5, bottom5 = top_bottom_n(scoreset, n=5)
    z_outliers = pillar_zscore_outliers(scoreset, threshold=2.0, top_n=10)
    stuck = stuck_tickers(scoreset, drift_threshold=5.0)

    lines: List[str] = []
    lines.append(f"# LTHCS composite-score distribution — {label}")
    lines.append("")
    lines.append(
        f"Snapshot file: `data/lthcs/snapshots/{snapshot_date}.json` "
        f"(latest available; today is {label}).  "
        f"Universe size: **{summary['count']}**."
    )
    lines.append("")

    lines.append("## Distribution summary")
    lines.append("")
    lines.append(
        f"- mean: **{summary['mean']}** "
        f"  stdev: **{summary['stdev']}**"
    )
    lines.append(
        f"- min/max: **{summary['min']} / {summary['max']}**"
    )
    lines.append(
        f"- p5/p25/p50/p75/p95: "
        f"**{summary['p5']} / {summary['p25']} / {summary['p50']} / "
        f"{summary['p75']} / {summary['p95']}**"
    )
    lines.append("")

    lines.append("## Histogram (10-point bins)")
    lines.append("")
    lines.append("```")
    lines.append(ascii_histogram(hist))
    lines.append("```")
    lines.append("")

    lines.append("## Band cohorts vs documented thresholds")
    lines.append("")
    lines.append("| band | range | count | share |")
    lines.append("|---|---|---|---|")
    total = sum(c for _, _, _, c in bands) or 1
    for name, lo, hi, c in bands:
        lines.append(
            f"| {name} | {lo}-{hi} | {c} | {100.0 * c / total:.1f}% |"
        )
    lines.append("")
    # Detect starved or overflowing bands.
    starved = [n for (n, _, _, c) in bands if c == 0]
    overflowing = [
        (n, c, 100.0 * c / total) for (n, _, _, c) in bands if 100.0 * c / total >= 40.0
    ]
    if starved:
        lines.append(f"**Starved bands (count=0):** {', '.join(starved)}.")
    if overflowing:
        ov = ", ".join(f"{n} ({c}, {pct:.1f}%)" for n, c, pct in overflowing)
        lines.append(f"**Over-populated bands (>=40% share):** {ov}.")
    if not starved and not overflowing:
        lines.append("Band populations look balanced — no starved or runaway band.")
    lines.append("")

    lines.append("## Per-cohort distribution")
    lines.append("")
    lines.append("| cohort | n | mean | stdev | p25 | p50 | p75 |")
    lines.append("|---|---|---|---|---|---|---|")
    for cohort_name, info in cohort.items():
        s = info["summary"]
        lines.append(
            f"| {cohort_name} | {s['count']} | {s['mean']} | {s['stdev']} | "
            f"{s['p25']} | {s['p50']} | {s['p75']} |"
        )
    lines.append("")
    for cohort_name, info in cohort.items():
        if info["summary"]["count"] < 3:
            continue
        lines.append(f"### {cohort_name} ({info['summary']['count']})")
        lines.append("")
        lines.append("```")
        lines.append(ascii_histogram(info["histogram"]))
        lines.append("```")
        lines.append("")

    lines.append("## Top 5 / bottom 5 by composite")
    lines.append("")
    lines.append("**Top 5**")
    lines.append("")
    lines.append("| ticker | composite | band | maturity | adoption | inst | fin | thesis | des | flags |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in top5:
        s = r.get("subscores") or {}
        flags = ",".join(r.get("data_quality_flags") or []) or "-"
        lines.append(
            f"| {r['ticker']} | {r['lthcs_score']} | {r.get('band')} | "
            f"{r.get('maturity_stage')} | "
            f"{s.get('adoption_momentum')} | {s.get('institutional_confidence')} | "
            f"{s.get('financial_evolution')} | {s.get('thesis_integrity')} | "
            f"{s.get('des')} | {flags} |"
        )
    lines.append("")
    lines.append("**Bottom 5**")
    lines.append("")
    lines.append("| ticker | composite | band | maturity | adoption | inst | fin | thesis | des | flags |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in bottom5:
        s = r.get("subscores") or {}
        flags = ",".join(r.get("data_quality_flags") or []) or "-"
        lines.append(
            f"| {r['ticker']} | {r['lthcs_score']} | {r.get('band')} | "
            f"{r.get('maturity_stage')} | "
            f"{s.get('adoption_momentum')} | {s.get('institutional_confidence')} | "
            f"{s.get('financial_evolution')} | {s.get('thesis_integrity')} | "
            f"{s.get('des')} | {flags} |"
        )
    lines.append("")

    lines.append("## Pillar-vs-cohort z-score outliers (|z| >= 2.0)")
    lines.append("")
    if z_outliers:
        lines.append("| ticker | cohort | pillar | value | cohort_mean | cohort_sd | z | composite | flags |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for o in z_outliers:
            flags = ",".join(o.get("data_quality_flags") or []) or "-"
            lines.append(
                f"| {o['ticker']} | {o['cohort']} | {o['pillar']} | {o['value']} | "
                f"{o['cohort_mean']} | {o['cohort_stdev']} | {o['z']} | "
                f"{o['composite']} | {flags} |"
            )
    else:
        lines.append("(none — every pillar within 2 sigma of its cohort mean)")
    lines.append("")

    lines.append("## Stuck tickers (|drift_30d| < 5.0)")
    lines.append("")
    lines.append(f"Stuck count: **{len(stuck)} / {summary['count']}**")
    lines.append("")
    if stuck:
        lines.append("| ticker | composite | band | drift_30d | drift_90d | maturity | flags |")
        lines.append("|---|---|---|---|---|---|---|")
        # Top 25 most-stuck.
        for s in stuck[:25]:
            flags = ",".join(s.get("data_quality_flags") or []) or "-"
            lines.append(
                f"| {s['ticker']} | {s['composite']} | {s['band']} | "
                f"{s['drift_30d']} | {s['drift_90d']} | {s['maturity_stage']} | {flags} |"
            )
    lines.append("")
    return "\n".join(lines) + "\n"


def render_correlation_report(
    snapshot_date: str,
    label: str,
    scoreset: Sequence[Dict[str, Any]],
    stability_window_dates: Sequence[str] = (),
) -> str:
    """Markdown text for the pillar-correlation report."""
    corr = pillar_correlation_matrix(scoreset)
    classes = classify_pillar_pairs(corr, high_thresh=0.7, low_thresh=0.2)

    lines: List[str] = []
    lines.append(f"# LTHCS pillar correlation — {label}")
    lines.append("")
    lines.append(
        f"Snapshot file: `data/lthcs/snapshots/{snapshot_date}.json` "
        f"(latest available; today is {label})."
    )
    lines.append("")

    lines.append("## 5x5 Pearson correlation matrix")
    lines.append("")
    header = ["pillar"] + list(PILLAR_ORDER)
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "---|" * len(header))
    for a in PILLAR_ORDER:
        row = [a]
        for b in PILLAR_ORDER:
            v = corr.get((a, b))
            row.append("—" if v is None else f"{v:+.3f}")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    lines.append("## Near-redundant pillar pairs (|r| >= 0.7)")
    lines.append("")
    if classes["redundant"]:
        lines.append("| pair | r |")
        lines.append("|---|---|")
        for a, b, v in classes["redundant"]:
            lines.append(f"| {a} ↔ {b} | {v:+.3f} |")
        lines.append("")
        lines.append(
            "Pairs above add little independent signal — candidates for "
            "weight reduction in the next weighting profile review."
        )
    else:
        lines.append("(none — every pillar pair has |r| < 0.7)")
    lines.append("")

    lines.append("## Near-orthogonal pillar pairs (|r| <= 0.2)")
    lines.append("")
    if classes["orthogonal"]:
        lines.append("| pair | r |")
        lines.append("|---|---|")
        for a, b, v in classes["orthogonal"]:
            lines.append(f"| {a} ↔ {b} | {v:+.3f} |")
        lines.append("")
        lines.append(
            "Pairs above carry independent signal — these are the structural "
            "workhorses of the composite."
        )
    else:
        lines.append("(none — every pillar pair has |r| > 0.2)")
    lines.append("")

    if stability_window_dates:
        lines.append("## 30-day correlation stability")
        lines.append("")
        stab = correlation_stability(stability_window_dates, SNAPSHOT_DIR)
        lines.append(
            f"Snapshots scanned: **{len(stab['dates_used'])}** "
            f"(window: {stability_window_dates[0]} → {stability_window_dates[-1]})"
        )
        lines.append("")
        lines.append("| pair | mean | min | max | range |")
        lines.append("|---|---|---|---|---|")
        # Sort by range descending so the least-stable pairs surface first.
        ordered = sorted(
            stab["per_pair"].items(), key=lambda kv: -kv[1]["range"]
        )
        for (a, b), info in ordered:
            lines.append(
                f"| {a} ↔ {b} | {info['mean']:+.3f} | {info['min']:+.3f} | "
                f"{info['max']:+.3f} | {info['range']:.3f} |"
            )
        unstable = [k for k, v in stab["per_pair"].items() if v["range"] >= 0.3]
        lines.append("")
        if unstable:
            lines.append(
                "**Unstable pairs (range >= 0.30 over 30d):** "
                + ", ".join(f"{a}↔{b}" for a, b in unstable)
            )
        else:
            lines.append("All pillar-pair correlations are stable (range < 0.30) over the 30d window.")
        lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        default=None,
        help="Snapshot date to audit (YYYY-MM-DD). Defaults to most-recent snapshot.",
    )
    parser.add_argument(
        "--label",
        default=None,
        help="Output filename prefix (defaults to the snapshot date).",
    )
    parser.add_argument(
        "--no-stability",
        action="store_true",
        help="Skip the 30-day correlation-stability scan.",
    )
    args = parser.parse_args()

    snapshot_date = args.date or latest_snapshot_date()
    if not snapshot_date:
        print("ERROR: no snapshots found under", SNAPSHOT_DIR, file=sys.stderr)
        return 2
    label = args.label or snapshot_date

    snapshot = load_snapshot(snapshot_date)
    scoreset = extract_scores(snapshot)
    if not scoreset:
        print(f"ERROR: snapshot {snapshot_date} has no scores", file=sys.stderr)
        return 2

    AUDIT_DIR.mkdir(parents=True, exist_ok=True)

    dist_md = render_distribution_report(snapshot_date, label, scoreset)
    dist_path = AUDIT_DIR / f"{label}_composite_distribution.md"
    dist_path.write_text(dist_md, encoding="utf-8")

    stability_dates: List[str] = []
    if not args.no_stability:
        all_dates = list_snapshot_dates()
        # Window of the 30 calendar days ending at snapshot_date.
        try:
            from datetime import date as _date

            snap = _date.fromisoformat(snapshot_date)
            for d in all_dates:
                try:
                    cur = _date.fromisoformat(d)
                except ValueError:
                    continue
                delta = (snap - cur).days
                if 0 <= delta <= 30:
                    stability_dates.append(d)
            stability_dates.sort()
        except ValueError:
            stability_dates = []

    corr_md = render_correlation_report(
        snapshot_date, label, scoreset, stability_window_dates=stability_dates
    )
    corr_path = AUDIT_DIR / f"{label}_pillar_correlation.md"
    corr_path.write_text(corr_md, encoding="utf-8")

    print(f"distribution -> {dist_path}")
    print(f"correlation  -> {corr_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
