"""LTHCS monthly quality-audit orchestrator (Phase 4).

Runs the three Phase 3 audit scripts in sequence — pillar quality (EE),
score-distribution + correlation (FF), weights vs IC + bands (GG) — and
produces a single combined-summary markdown plus a machine-readable JSON
snapshot for the status page.

Usage:

    python scripts/lthcs_quality_audit_runner.py [--asof YYYY-MM-DD] [--out DIR]

Exit codes:
    0 — no unaccepted STUB, distribution acceptable, known-stub config valid
    1 — an unaccepted pillar is STUB, an accepted pillar is no longer a stub,
        the distribution is critically broken, or known_pillar_stubs.json
        is invalid

A pillar listed in ``data/lthcs/known_pillar_stubs.json`` whose measured
verdict is still STUB is reported as ``KNOWN GAP (accepted)``. That label
stays in the summary and does not count toward CRITICAL. The daily audit
and data-health read this same verdict (via ``audit_failed``); they do not
treat an accepted gap as an error. Any other pillar going STUB, or an
accepted pillar gaining coverage past the stub threshold, still fails.

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
import re
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

# Reported instead of STUB when a pillar is listed in known_pillar_stubs.json
# and its measured verdict is still STUB. Not an error. Kept visible.
KNOWN_GAP_VERDICT = "KNOWN GAP (accepted)"

# Workflow name the daily audit uses when it decides whether a red
# lthcs-quality-audit-monthly run is still an error. Same string as
# .github/workflows/lthcs-quality-audit-monthly.yml `name:`.
QUALITY_AUDIT_WORKFLOW = "lthcs-quality-audit-monthly"

KNOWN_STUBS_FILENAME = "known_pillar_stubs.json"
_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MIN_REASON_LEN = 40


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


def load_known_stubs(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Accepted pillar stubs. A missing file means nothing is accepted.

    A present but unreadable or incomplete file raises ``ValueError``.
    Callers must not swallow that into "accept the stubs": a broken
    disclosure has to fail the run, or a typo would mute a real STUB.
    """
    path = path or (DATA / KNOWN_STUBS_FILENAME)
    if not path.exists():
        return []
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path.name} is not valid JSON ({exc})") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("stubs"), list):
        raise ValueError(f"{path.name} must be an object with a stubs list")
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for i, raw in enumerate(doc["stubs"]):
        where = f"{path.name} stubs[{i}]"
        if not isinstance(raw, dict):
            raise ValueError(f"{where} must be an object")
        pillar = raw.get("pillar")
        if pillar not in PILLARS:
            raise ValueError(f"{where}.pillar must be one of {', '.join(PILLARS)}")
        if pillar in seen:
            raise ValueError(f"{where} duplicates pillar {pillar}")
        seen.add(pillar)
        decided = raw.get("decided")
        if not isinstance(decided, str) or not _ISO_DAY.match(decided):
            raise ValueError(f"{where}.decided must be YYYY-MM-DD")
        review_by = raw.get("review_by", None)
        if review_by is not None and (not isinstance(review_by, str) or not _ISO_DAY.match(review_by)):
            raise ValueError(f"{where}.review_by must be YYYY-MM-DD when set")
        if isinstance(review_by, str) and review_by < decided:
            raise ValueError(f"{where}.review_by is before decided")
        reason = str(raw.get("reason") or "").strip()
        if len(reason) < _MIN_REASON_LEN:
            raise ValueError(
                f"{where}.reason must explain the decision "
                f"(at least {_MIN_REASON_LEN} characters)"
            )
        out.append({
            "pillar": pillar,
            "decided": decided,
            "review_by": review_by,
            "reason": reason,
        })
    return out


def apply_known_stubs(
    pillars: Dict[str, Dict[str, Any]],
    known: List[Dict[str, Any]],
    asof: str,
) -> List[str]:
    """Relabel an accepted STUB as ``KNOWN GAP (accepted)``.

    Returns alerts. A listed pillar that is no longer STUB is left on its
    measured verdict, marked ``known_gap_mismatch``, and alerted — that
    counts toward CRITICAL. A review-by date that has passed adds a
    reminder and does not by itself restore CRITICAL.
    """
    alerts: List[str] = []
    for entry in known:
        pillar = entry["pillar"]
        info = pillars.get(pillar)
        if not isinstance(info, dict):
            continue
        raw = info.get("verdict")
        info["raw_verdict"] = raw
        info["known_gap"] = {
            "decided": entry["decided"],
            "review_by": entry.get("review_by"),
            "reason": entry["reason"],
        }
        if raw == "STUB":
            info["verdict"] = KNOWN_GAP_VERDICT
            info["accepted_stub"] = True
            info["counts_toward_critical"] = False
            review_by = entry.get("review_by")
            if review_by and asof > review_by:
                alerts.append(
                    f"{pillar} known gap is past its review-by date {review_by} "
                    f"(decided {entry['decided']}). The acceptance still holds and "
                    f"does not count toward CRITICAL; reconfirm or remove "
                    f"data/lthcs/{KNOWN_STUBS_FILENAME}."
                )
            continue
        info["known_gap_mismatch"] = True
        info["counts_toward_critical"] = True
        alerts.append(
            f"{pillar} was accepted as a stub on {entry['decided']} but measured "
            f"{raw} at {info.get('coverage_pct')}% coverage. Coverage past the "
            f"stub threshold ({STUB_COVERAGE:g}%) is not covered by the acceptance "
            f"and counts toward CRITICAL. Update data/lthcs/{KNOWN_STUBS_FILENAME}."
        )
    return alerts


def known_gap_records(pillars: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One record per pillar that has a known-stub entry, in pillar order."""
    out: List[Dict[str, Any]] = []
    for pillar in PILLARS:
        info = pillars.get(pillar) or {}
        if not info.get("known_gap"):
            continue
        gap = info.get("known_gap") or {}
        mismatch = bool(info.get("known_gap_mismatch"))
        out.append({
            "pillar": pillar,
            "verdict": info.get("verdict"),
            "raw_verdict": info.get("raw_verdict"),
            "coverage_pct": info.get("coverage_pct"),
            "decided": gap.get("decided"),
            "review_by": gap.get("review_by"),
            "reason": gap.get("reason"),
            "accepted": bool(info.get("accepted_stub")),
            "mismatch": mismatch,
            "counts_toward_critical": bool(mismatch or info.get("verdict") == "STUB"),
        })
    return out


def _overall_verdict(
    pillars: Dict[str, Dict[str, Any]],
    distribution: Dict[str, Any],
    weights: Dict[str, Any],
    bands: Dict[str, Any],
    *,
    stubs_error: bool,
) -> str:
    """CRITICAL only for a real failure. An accepted known gap is neither."""
    unaccepted_stub = any(
        isinstance(p, dict) and p.get("verdict") == "STUB" for p in pillars.values()
    )
    mismatch = any(
        isinstance(p, dict) and p.get("known_gap_mismatch") for p in pillars.values()
    )
    if stubs_error or unaccepted_stub or mismatch or distribution.get("critical"):
        return "CRITICAL"
    degraded = (
        any(isinstance(p, dict) and p.get("verdict") == "DEGRADED" for p in pillars.values())
        or weights.get("verdict") == "MISALIGNED"
        or bands.get("verdict") in ("SKEWED", "MISALIGNED")
    )
    return "DEGRADED" if degraded else "HEALTHY"


def _portable_path(value: str) -> str:
    """Repo-relative path when ``value`` points inside the checkout.

    The summary JSON is committed. Absolute runner paths (``/home/runner``
    or a local workspace) do not survive that.
    """
    try:
        return Path(value).resolve().relative_to(REPO).as_posix()
    except (ValueError, OSError):
        return value


def _portable_sub_audit(info: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in info.items():
        if key == "cohort_ic":
            continue
        if key == "path" and isinstance(value, str):
            out[key] = _portable_path(value)
        elif key == "paths" and isinstance(value, list):
            out[key] = [_portable_path(p) if isinstance(p, str) else p for p in value]
        else:
            out[key] = value
    return out


def audit_failed(payload: Dict[str, Any]) -> bool:
    """Whether this verdict is an error for the workflow, daily audit, and data-health.

    ``KNOWN GAP (accepted)`` is not a failure. A missing or unreadable payload
    is a failure: silence must not clear an error. ``overall_verdict`` of
    CRITICAL is a failure even if a caller rebuilt the pillar map.
    """
    if not isinstance(payload, dict):
        return True
    if payload.get("known_stubs_error"):
        return True
    if payload.get("overall_verdict") == "CRITICAL":
        return True
    pillars = payload.get("pillars") or {}
    if isinstance(pillars, dict):
        for info in pillars.values():
            if not isinstance(info, dict):
                continue
            if info.get("verdict") == "STUB" or info.get("known_gap_mismatch"):
                return True
    dist = payload.get("distribution") or {}
    if isinstance(dist, dict) and dist.get("critical"):
        return True
    return False


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

def _live_band_ranges() -> Dict[str, Tuple[int, int]]:
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from lthcs.bands import band_ranges, load_score_bands

    return band_ranges(load_score_bands(DATA / "weights.json"))


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
    # Cutoffs come from weights.json -> score_bands (not hard-coded), so a
    # band recalibration is reflected here automatically. Half-open
    # [min, max+1) to match lthcs.score.assign_band for fractional scores.
    rng = _live_band_ranges()
    elite_lo = rng["elite"][0]
    high_lo = rng["high_confidence"][0]
    review_hi = rng["review"][1]
    elite = sum(1 for v in comps if v >= elite_lo)
    high_conf = sum(1 for v in comps if high_lo <= v < elite_lo)
    review = sum(1 for v in comps if v < review_hi + 1)
    # Critical: elite + high_conf both zero AND review band overflowing
    critical = (elite == 0 and high_conf == 0 and review / max(1, len(comps)) >= 0.40)
    return {
        "n": len(comps),
        "band_cutoffs": {
            "elite_min": elite_lo,
            "high_confidence_min": high_lo,
            "review_max": review_hi,
        },
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
    # Rows with no 30-day prior (new to the universe) carry a 0.0
    # placeholder, not a measured drift — leave them out of the ratio.
    measured = [r for r in records if "30d" not in (r.get("drift_unavailable") or ())]
    with_drift = sum(1 for r in measured if isinstance(r.get("drift_30d"), (int, float)))
    nonzero_drift = sum(
        1 for r in measured
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

_PILLAR_BADGE = {
    "HEALTHY": "🟢",
    "DEGRADED": "🟡",
    "STUB": "🔴",
    KNOWN_GAP_VERDICT: "🔵",
}


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
    if payload.get("known_stubs_error"):
        lines.append(
            f"Known-stub config error (acceptance not applied): {payload['known_stubs_error']}"
        )
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
        note = " (accepted stub no longer matches)" if info.get("known_gap_mismatch") else ""
        lines.append(
            f"| {pillar} | {_badge(v)} {v}{note} | {cov}% | "
            f"{'-' if mean is None else mean} | {'-' if sd is None else sd} |"
        )
    lines.append("")

    # Accepted gaps stay in the table above and are repeated here with the
    # reason, decision date, and review-by date. Never omitted.
    lines.append("## Known gaps (accepted)")
    lines.append("")
    lines.append(
        "A pillar listed in `data/lthcs/known_pillar_stubs.json` whose measured "
        "verdict is still STUB is reported as **KNOWN GAP (accepted)**. It stays "
        "in the table above and does not count toward CRITICAL. Any other pillar "
        "that is STUB, or an accepted pillar that gains coverage past the stub "
        f"threshold ({STUB_COVERAGE:g}%), still counts toward CRITICAL."
    )
    lines.append("")
    gaps = payload.get("known_gaps") or []
    if not gaps:
        lines.append("- (none)")
    else:
        for g in gaps:
            review = g.get("review_by") or "not set"
            counts = g.get("mismatch") or g.get("verdict") == "STUB"
            effect = "Counts toward CRITICAL" if counts else "Does not count toward CRITICAL"
            lines.append(
                f"- **{g.get('pillar')}** — {g.get('verdict')} "
                f"(raw {g.get('raw_verdict')}, coverage {g.get('coverage_pct')}%). "
                f"Decided {g.get('decided')}. Review by {review}. {effect}."
            )
            reason = str(g.get("reason") or "").strip()
            if reason:
                lines.append(f"  - {reason}")
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
        cut = dist.get("band_cutoffs") or {}
        e_lo = cut.get("elite_min", "?")
        h_lo = cut.get("high_confidence_min", "?")
        r_hi = cut.get("review_max", "?")
        h_hi = (e_lo - 1) if isinstance(e_lo, int) else "?"
        r_lt = (r_hi + 1) if isinstance(r_hi, int) else "?"
        lines.append(
            f"- elite (>={e_lo}): **{dist['elite_count']}**, "
            f"high-confidence ({h_lo}-{h_hi}): **{dist['high_conf_count']}**, "
            f"review (<{r_lt}): **{dist['review_count']}** ({dist.get('review_pct', 0)}%)"
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

    # Accepted stubs are relabeled before the overall verdict. A broken
    # config does not relabel anything: every STUB still counts.
    stubs_error: Optional[str] = None
    try:
        known = load_known_stubs(DATA / KNOWN_STUBS_FILENAME)
    except ValueError as exc:
        known = []
        stubs_error = str(exc)
    if stubs_error:
        alerts.append(
            "known pillar stubs config is invalid and was not applied: "
            + stubs_error
        )
    else:
        alerts.extend(apply_known_stubs(pillars, known, asof))

    # Run all three sub-audits with failure isolation
    sub_audits: Dict[str, Any] = {}
    sub_audits["pillar_quality"] = _run_pillar_audit(asof, out_dir)
    sub_audits["score_distribution"] = _run_distribution_audit(asof, out_dir)
    sub_audits["weights_thresholds"] = _run_weights_audit(asof, out_dir)

    # Weights verdict — uses cohort_ic if GG succeeded; otherwise UNKNOWN.
    cohort_ic = (sub_audits["weights_thresholds"].get("cohort_ic")
                 if sub_audits["weights_thresholds"].get("ok") else None)
    weights = _weights_verdict(DATA / "weights.json", cohort_ic)

    # Overall verdict. An accepted known gap is not CRITICAL.
    overall_verdict = _overall_verdict(
        pillars, distribution, weights, bands, stubs_error=bool(stubs_error),
    )

    payload: Dict[str, Any] = {
        "asof": asof,
        "snapshot_date": snapshot_date,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "overall_verdict": overall_verdict,
        "known_stubs_error": stubs_error,
        "known_gaps": known_gap_records(pillars),
        "pillars": pillars,
        "distribution": distribution,
        "weights": weights,
        "bands": bands,
        "alerts": alerts,
        "sub_audits": {k: _portable_sub_audit(v) for k, v in sub_audits.items()},
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

    # Same rule the daily audit and data-health use. An accepted known gap
    # (KNOWN GAP (accepted)) does not fail the run.
    if audit_failed(payload):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
