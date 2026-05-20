"""LTHCS monthly quality-audit orchestrator (Phase 4).

Runs the three Phase 3 audit scripts in sequence — pillar quality (EE),
score-distribution + correlation (FF), weights vs IC + bands (GG) — and
produces a single combined-summary markdown plus a machine-readable JSON
snapshot for the status page.

Usage:

    python scripts/lthcs_quality_audit_runner.py [--asof YYYY-MM-DD] [--out DIR]

Exit codes:
    0 — all pillars HEALTHY (or DEGRADED) and distribution acceptable
    1 — at least one pillar STUB or distribution critically broken

The runner imports the three audit modules rather than shelling out:

* faster (no Python startup x3),
* easier to test (we can patch SNAPSHOT_DIR per audit), and
* per-audit failure isolation works naturally with try/except on each call.

Outputs (under ``data/lthcs/quality_audit/``):
* ``<asof>_summary.md`` — human-readable combined verdict
* ``latest_summary.json`` — machine-readable snapshot consumed by the
  ``lthcs_health/quality.html`` status page
* (sub-audit outputs from EE/FF/GG already land in the same directory)
"""

from __future__ import annotations

import argparse
import importlib
import json
import statistics
import sys
import traceback
from datetime import date as _date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data" / "lthcs"
SNAPSHOT_DIR = DATA / "snapshots"
AUDIT_DIR = DATA / "quality_audit"

# Pillar coverage thresholds — anything below STUB_COVERAGE means the
# pillar isn't producing real per-ticker signal and the daily pipeline
# has effectively defaulted to the 50.0 placeholder.
STUB_COVERAGE = 30.0       # below this -> STUB
DEGRADED_COVERAGE = 75.0   # below this -> DEGRADED (but above stub)

# A pillar that has acceptable coverage but no measurable cross-ticker
# variance is still degraded — the framework can't distinguish tickers.
DEGRADED_STDEV = 3.0

PILLARS = [
    "adoption_momentum",
    "institutional_confidence",
    "financial_evolution",
    "thesis_integrity",
    "des",
]


# ---------------------------------------------------------------------------
# Lazy module imports (so a per-audit crash doesn't kill the runner)
# ---------------------------------------------------------------------------

def _ensure_scripts_on_path() -> None:
    scripts_dir = REPO / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))


def _load_module(name: str):
    _ensure_scripts_on_path()
    return importlib.import_module(name)


# ---------------------------------------------------------------------------
# Snapshot helpers (kept tiny so the runner stays self-contained)
# ---------------------------------------------------------------------------

def latest_snapshot_date(snap_dir: Path = SNAPSHOT_DIR) -> Optional[str]:
    if not snap_dir.exists():
        return None
    dates = sorted(p.stem for p in snap_dir.glob("*.json") if p.stem != "index")
    return dates[-1] if dates else None


def _load_snapshot(asof: str, snap_dir: Path = SNAPSHOT_DIR) -> Optional[Dict[str, Any]]:
    path = snap_dir / f"{asof}.json"
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Per-pillar verdict (from EE's outputs / raw snapshot)
# ---------------------------------------------------------------------------

def _pillar_verdict(
    coverage_pct: float,
    stdev: float,
) -> str:
    """HEALTHY / DEGRADED / STUB based on coverage + cross-ticker stdev."""
    if coverage_pct < STUB_COVERAGE:
        return "STUB"
    if coverage_pct < DEGRADED_COVERAGE or stdev < DEGRADED_STDEV:
        return "DEGRADED"
    return "HEALTHY"


def _compute_pillar_verdicts(snapshot: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Read a snapshot envelope and compute per-pillar verdicts directly.

    Mirrors the EE audit's coverage/stdev math at a high level so the
    runner can stay self-sufficient (and so we can run the verdict over
    synthetic snapshots in tests without touching the EE module).
    """
    records = snapshot.get("scores") or []
    out: Dict[str, Dict[str, Any]] = {}
    for pillar in PILLARS:
        values: List[float] = []
        for r in records:
            sub = r.get("subscores") or {}
            v = sub.get(pillar)
            if isinstance(v, (int, float)):
                values.append(float(v))
        if not values:
            out[pillar] = {
                "coverage_pct": 0.0,
                "mean": None,
                "stdev": None,
                "verdict": "STUB",
                "n": 0,
            }
            continue
        non_default = sum(1 for v in values if v != 50.0)
        coverage = round(100.0 * non_default / len(values), 1)
        mean = round(sum(values) / len(values), 2)
        sd = round(statistics.pstdev(values), 2) if len(values) > 1 else 0.0
        out[pillar] = {
            "coverage_pct": coverage,
            "mean": mean,
            "stdev": sd,
            "verdict": _pillar_verdict(coverage, sd),
            "n": len(values),
        }
    return out


# ---------------------------------------------------------------------------
# Distribution + bands verdict (from FF / GG outputs)
# ---------------------------------------------------------------------------

def _distribution_summary(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Composite-score distribution one-liner."""
    records = snapshot.get("scores") or []
    comps = [r.get("lthcs_score") for r in records if isinstance(r.get("lthcs_score"), (int, float))]
    if not comps:
        return {
            "n": 0,
            "mean": None,
            "stdev": None,
            "elite_count": 0,
            "high_conf_count": 0,
            "review_count": 0,
            "critical": True,
        }
    mean = sum(comps) / len(comps)
    sd = statistics.pstdev(comps) if len(comps) > 1 else 0.0
    elite = sum(1 for v in comps if v >= 85)
    high_conf = sum(1 for v in comps if 80 <= v < 85)
    review = sum(1 for v in comps if v < 50)
    # Critical: elite + high_conf both zero AND review band overflowing
    critical = (elite == 0 and high_conf == 0 and review / max(1, len(comps)) >= 0.40)
    return {
        "n": len(comps),
        "mean": round(mean, 2),
        "stdev": round(sd, 2),
        "elite_count": elite,
        "high_conf_count": high_conf,
        "review_count": review,
        "review_pct": round(100.0 * review / len(comps), 1),
        "critical": critical,
    }


def _weights_verdict(weights_path: Path, cohort_ic: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """ALIGNED / MISALIGNED across cohorts. Falls back to UNKNOWN if no IC available."""
    if not cohort_ic:
        return {"verdict": "UNKNOWN", "misaligned_cohorts": [], "n_cohorts": 0}
    if not weights_path.exists():
        return {"verdict": "UNKNOWN", "misaligned_cohorts": [], "n_cohorts": 0}
    weights_cfg = json.loads(weights_path.read_text(encoding="utf-8"))
    profiles = weights_cfg.get("profiles", {})
    misaligned: List[Tuple[str, float, str]] = []
    n = 0
    for cohort, ic in cohort_ic.items():
        if cohort not in profiles:
            continue
        n += 1
        current = profiles[cohort]
        # Implied weights: Sharpe-proportional, clipped non-negative.
        raw: Dict[str, float] = {}
        for p in PILLARS:
            sub = ic.get(p) or {}
            raw[p] = max(0.0, float(sub.get("ic_sharpe", 0.0)))
        tot = sum(raw.values())
        if tot <= 0:
            implied = {p: 1.0 / len(PILLARS) for p in PILLARS}
        else:
            implied = {p: raw[p] / tot for p in PILLARS}
        worst = 0.0
        worst_pillar = ""
        for i, p in enumerate(PILLARS):
            gap = abs(current[i] - implied[p])
            if gap > worst:
                worst = gap
                worst_pillar = p
        if worst > 0.10:
            misaligned.append((cohort, round(worst, 3), worst_pillar))
    misaligned.sort(key=lambda t: -t[1])
    verdict = "MISALIGNED" if misaligned else "ALIGNED"
    return {
        "verdict": verdict,
        "n_cohorts": n,
        "misaligned_cohorts": [
            {"cohort": c, "worst_gap": g, "worst_pillar": p} for c, g, p in misaligned
        ],
    }


def _band_verdict(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Latest-snapshot band counts + summary verdict."""
    records = snapshot.get("scores") or []
    counts: Dict[str, int] = {
        "elite": 0,
        "high_confidence": 0,
        "constructive": 0,
        "monitor": 0,
        "weakening": 0,
        "review": 0,
    }
    for r in records:
        b = r.get("band")
        if b in counts:
            counts[b] += 1
    starved = [b for b, c in counts.items() if c == 0 and b in ("elite", "high_confidence")]
    total = sum(counts.values()) or 1
    review_pct = 100.0 * counts.get("review", 0) / total
    if starved and review_pct >= 40.0:
        verdict = "MISALIGNED"
    elif starved or review_pct >= 50.0:
        verdict = "SKEWED"
    else:
        verdict = "BALANCED"
    return {
        "verdict": verdict,
        "counts": counts,
        "starved": starved,
        "review_pct": round(review_pct, 1),
    }


# ---------------------------------------------------------------------------
# Cross-cutting alerts
# ---------------------------------------------------------------------------

def _cross_cutting_alerts(snapshot: Dict[str, Any]) -> List[str]:
    alerts: List[str] = []
    records = snapshot.get("scores") or []
    if not records:
        alerts.append("snapshot is empty — no scores to audit")
        return alerts
    # Drift_30d regression check: HH's recent fix should make drift_30d non-zero
    # for >=80% of tickers. If everything is zero again, flag it.
    with_drift = sum(1 for r in records if isinstance(r.get("drift_30d"), (int, float)))
    nonzero_drift = sum(
        1 for r in records
        if isinstance(r.get("drift_30d"), (int, float)) and abs(float(r["drift_30d"])) > 1e-9
    )
    if with_drift and nonzero_drift / with_drift < 0.20:
        alerts.append(
            f"drift_30d regression: only {nonzero_drift}/{with_drift} tickers "
            f"have nonzero 30d drift — HH's fix may have regressed"
        )
    # All bands collapsed to "review"
    bands = {r.get("band") for r in records if r.get("band")}
    if bands == {"review"}:
        alerts.append("every ticker collapsed into the `review` band — composite distribution is broken")
    return alerts


# ---------------------------------------------------------------------------
# Sub-audit invocation (failure-isolated)
# ---------------------------------------------------------------------------

def _run_pillar_audit(asof: str, out_dir: Path) -> Dict[str, Any]:
    """Invoke EE — pillar quality audit.

    If no snapshot exists for ``asof`` we fall back to the latest available
    snapshot (typical case when the audit runs at 09:00 UTC on the 1st of
    the month and the daily pipeline hasn't completed yet for that date).
    """
    try:
        mod = _load_module("lthcs_pillar_quality_audit")
        # Pick the snapshot date: prefer asof if present, else latest.
        snap_path = SNAPSHOT_DIR / f"{asof}.json"
        if snap_path.exists():
            snap_date = asof
        else:
            snap_date = mod.latest_snapshot_date(SNAPSHOT_DIR)
        report = mod.build_report(snap_date, SNAPSHOT_DIR)
        out_path = out_dir / f"{asof}_pillar_quality.md"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report)
        return {"ok": True, "path": str(out_path), "snapshot_date": snap_date}
    except Exception as exc:  # noqa: BLE001 — failure isolation
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()}


def _run_distribution_audit(asof: str, out_dir: Path) -> Dict[str, Any]:
    """Invoke FF — score-distribution + correlation audit."""
    try:
        mod = _load_module("lthcs_score_distribution_audit")
        snapshot_date = mod.latest_snapshot_date()
        if not snapshot_date:
            return {"ok": False, "error": "no snapshots found"}
        snapshot = mod.load_snapshot(snapshot_date)
        scoreset = mod.extract_scores(snapshot)
        if not scoreset:
            return {"ok": False, "error": "snapshot empty"}
        out_dir.mkdir(parents=True, exist_ok=True)
        dist_md = mod.render_distribution_report(snapshot_date, asof, scoreset)
        dist_path = out_dir / f"{asof}_composite_distribution.md"
        dist_path.write_text(dist_md, encoding="utf-8")
        # 30d stability window
        all_dates = mod.list_snapshot_dates()
        stab: List[str] = []
        try:
            from datetime import date as _d
            snap_d = _d.fromisoformat(snapshot_date)
            for d in all_dates:
                try:
                    cur = _d.fromisoformat(d)
                except ValueError:
                    continue
                delta = (snap_d - cur).days
                if 0 <= delta <= 30:
                    stab.append(d)
            stab.sort()
        except ValueError:
            stab = []
        corr_md = mod.render_correlation_report(snapshot_date, asof, scoreset, stability_window_dates=stab)
        corr_path = out_dir / f"{asof}_pillar_correlation.md"
        corr_path.write_text(corr_md, encoding="utf-8")
        return {"ok": True, "paths": [str(dist_path), str(corr_path)]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()}


def _run_weights_audit(asof: str, out_dir: Path) -> Dict[str, Any]:
    """Invoke GG — weights + band threshold audit. Returns cohort IC for downstream use."""
    try:
        mod = _load_module("lthcs_weight_threshold_audit")
        out_dir.mkdir(parents=True, exist_ok=True)
        # We do the same computation as mod.run() but with a custom today,
        # then mirror its writers. This avoids needing to monkeypatch mod.TODAY.
        weights_cfg = mod.jload(mod.WEIGHTS_PATH)
        eq = mod.load_snapshots(mod.SNAPSHOTS_EQ)
        crypto = mod.load_snapshots(mod.SNAPSHOTS_CRYPTO)
        from collections import defaultdict
        combined: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for d, rows in eq.items():
            combined[d].extend(rows)
        for d, rows in crypto.items():
            combined[d].extend(rows)
        cohort_ic = mod.compute_cohort_ic(combined, horizon=mod.FORWARD_DAYS)
        weights_md = mod.fmt_weights_md(cohort_ic, weights_cfg, asof)
        band_md = mod.fmt_band_md(eq, crypto, weights_cfg, asof)
        wp = out_dir / f"{asof}_weights_vs_ic.md"
        bp = out_dir / f"{asof}_band_distribution.md"
        wp.write_text(weights_md)
        bp.write_text(band_md)
        # Strip __meta__ before returning to keep payload lean.
        clean_ic: Dict[str, Any] = {}
        for cohort, sub in cohort_ic.items():
            clean_ic[cohort] = {k: v for k, v in sub.items() if k != "__meta__"}
        return {"ok": True, "paths": [str(wp), str(bp)], "cohort_ic": clean_ic}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()}


# ---------------------------------------------------------------------------
# Combined-summary rendering
# ---------------------------------------------------------------------------

_PILLAR_BADGE = {"HEALTHY": "🟢", "DEGRADED": "🟡", "STUB": "🔴"}


def _badge(verdict: str) -> str:
    return _PILLAR_BADGE.get(verdict, "⚪")


def render_summary_md(payload: Dict[str, Any]) -> str:
    """Combined human-readable verdict markdown."""
    asof = payload["asof"]
    snapshot_date = payload.get("snapshot_date") or asof
    lines: List[str] = []
    lines.append(f"# LTHCS Monthly Quality Audit — {asof}")
    lines.append("")
    lines.append(
        f"Snapshot used: `data/lthcs/snapshots/{snapshot_date}.json`. "
        f"Generated at {payload.get('generated_at_utc', '')}."
    )
    lines.append("")
    lines.append(f"## Overall verdict: **{payload['overall_verdict']}**")
    lines.append("")

    # Per-pillar
    lines.append("## Per-pillar signal quality")
    lines.append("")
    lines.append("| pillar | verdict | coverage | mean | stdev |")
    lines.append("|---|---|---:|---:|---:|")
    for pillar in PILLARS:
        info = payload["pillars"].get(pillar) or {}
        v = info.get("verdict", "?")
        cov = info.get("coverage_pct", 0)
        mean = info.get("mean")
        sd = info.get("stdev")
        lines.append(
            f"| {pillar} | {_badge(v)} {v} | {cov}% | "
            f"{'-' if mean is None else mean} | {'-' if sd is None else sd} |"
        )
    lines.append("")

    # Distribution
    dist = payload["distribution"]
    lines.append("## Composite distribution")
    lines.append("")
    if dist["n"] == 0:
        lines.append("- (snapshot empty — no composites to summarise)")
    else:
        crit = " — **CRITICAL**" if dist.get("critical") else ""
        lines.append(
            f"- n={dist['n']}, mean={dist['mean']}, stdev={dist['stdev']}{crit}"
        )
        lines.append(
            f"- elite (>=85): **{dist['elite_count']}**, "
            f"high-confidence (80-84): **{dist['high_conf_count']}**, "
            f"review (<50): **{dist['review_count']}** ({dist.get('review_pct', 0)}%)"
        )
    lines.append("")

    # Weights
    w = payload["weights"]
    lines.append("## Weight alignment vs IC")
    lines.append("")
    lines.append(
        f"- verdict: **{w['verdict']}** "
        f"({w['n_cohorts']} cohort(s) with measurable IC)"
    )
    if w.get("misaligned_cohorts"):
        for m in w["misaligned_cohorts"]:
            lines.append(
                f"  - `{m['cohort']}` worst gap **{m['worst_gap']:+.3f}** "
                f"on `{m['worst_pillar']}`"
            )
    lines.append("")

    # Bands
    b = payload["bands"]
    lines.append("## Band distribution")
    lines.append("")
    lines.append(f"- verdict: **{b['verdict']}** (review share {b['review_pct']}%)")
    if b.get("starved"):
        lines.append(f"- starved bands: {', '.join(b['starved'])}")
    lines.append("")

    # Alerts
    alerts = payload.get("alerts") or []
    lines.append("## Cross-cutting alerts")
    lines.append("")
    if alerts:
        for a in alerts:
            lines.append(f"- ⚠️ {a}")
    else:
        lines.append("- (none)")
    lines.append("")

    # Sub-audit status
    lines.append("## Sub-audit run status")
    lines.append("")
    for name, info in payload["sub_audits"].items():
        status = "OK" if info.get("ok") else f"FAILED — {info.get('error')}"
        lines.append(f"- **{name}**: {status}")
    lines.append("")
    lines.append(
        "Full per-audit reports: "
        f"`data/lthcs/quality_audit/{asof}_pillar_quality.md`, "
        f"`{asof}_composite_distribution.md`, `{asof}_pillar_correlation.md`, "
        f"`{asof}_weights_vs_ic.md`, `{asof}_band_distribution.md`."
    )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run(
    asof: Optional[str] = None,
    out_dir: Optional[Path] = None,
    snap_dir: Optional[Path] = None,
) -> Tuple[Dict[str, Any], Path]:
    """Run all three audits + build the combined summary.

    Returns ``(payload, summary_path)``. The payload is also persisted to
    ``<out_dir>/latest_summary.json`` for the status page.

    Resolves ``snap_dir`` and ``out_dir`` against the current module-level
    constants so tests can monkeypatch ``runner.SNAPSHOT_DIR`` /
    ``runner.AUDIT_DIR`` without rebinding the function signature.
    """
    out_dir = out_dir or AUDIT_DIR
    snap_dir = snap_dir or SNAPSHOT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    asof = asof or _date.today().isoformat()
    snapshot_date = latest_snapshot_date(snap_dir)
    snapshot = _load_snapshot(snapshot_date, snap_dir) if snapshot_date else None

    # Per-pillar verdicts (independent of EE crash)
    pillars = _compute_pillar_verdicts(snapshot or {})
    distribution = _distribution_summary(snapshot or {})
    bands = _band_verdict(snapshot or {})
    alerts = _cross_cutting_alerts(snapshot or {})

    # Run all three sub-audits with failure isolation
    sub_audits: Dict[str, Any] = {}
    sub_audits["pillar_quality"] = _run_pillar_audit(asof, out_dir)
    sub_audits["score_distribution"] = _run_distribution_audit(asof, out_dir)
    sub_audits["weights_thresholds"] = _run_weights_audit(asof, out_dir)

    # Weights verdict — uses cohort_ic if GG succeeded; otherwise UNKNOWN.
    cohort_ic = (sub_audits["weights_thresholds"].get("cohort_ic")
                 if sub_audits["weights_thresholds"].get("ok") else None)
    weights = _weights_verdict(DATA / "weights.json", cohort_ic)

    # Overall verdict
    any_stub = any(p.get("verdict") == "STUB" for p in pillars.values())
    overall_verdict = "CRITICAL" if (any_stub or distribution.get("critical")) else (
        "DEGRADED" if any(p.get("verdict") == "DEGRADED" for p in pillars.values())
                  or weights["verdict"] == "MISALIGNED"
                  or bands["verdict"] in ("SKEWED", "MISALIGNED")
                  else "HEALTHY"
    )

    payload: Dict[str, Any] = {
        "asof": asof,
        "snapshot_date": snapshot_date,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "overall_verdict": overall_verdict,
        "pillars": pillars,
        "distribution": distribution,
        "weights": weights,
        "bands": bands,
        "alerts": alerts,
        "sub_audits": {
            k: {kk: vv for kk, vv in v.items() if kk != "cohort_ic"}
            for k, v in sub_audits.items()
        },
    }

    summary_md = render_summary_md(payload)
    summary_path = out_dir / f"{asof}_summary.md"
    summary_path.write_text(summary_md, encoding="utf-8")

    # Machine-readable for the status page
    latest_path = out_dir / "latest_summary.json"
    latest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    return payload, summary_path


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asof", default=None, help="Audit date (YYYY-MM-DD). Defaults to today.")
    parser.add_argument(
        "--out",
        default=None,
        help=f"Output directory (default: {AUDIT_DIR}).",
    )
    args = parser.parse_args(argv)

    out_dir = Path(args.out) if args.out else AUDIT_DIR
    payload, summary_path = run(asof=args.asof, out_dir=out_dir)

    print(f"summary -> {summary_path}")
    print(f"overall verdict: {payload['overall_verdict']}")

    # Exit non-zero if any pillar is STUB or distribution is critically broken
    any_stub = any(p.get("verdict") == "STUB" for p in payload["pillars"].values())
    crit_dist = payload["distribution"].get("critical")
    if any_stub or crit_dist:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
