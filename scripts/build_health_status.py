#!/usr/bin/env python3
"""Emit data/health/status.json — central monitoring snapshot.

Scans data/ and data/.stale/ for file mtimes, classifies each entry against
per-source freshness thresholds, and writes a single JSON the health page
reads. Pure stdlib, no extra deps so the pages.yml step is cheap.

Run from repo root: python scripts/build_health_status.py
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
STALE_DIR = DATA_DIR / ".stale"
OUT_PATH = DATA_DIR / "health" / "status.json"


@dataclass
class Threshold:
    fresh_h: float
    stale_h: float


# Per-source freshness thresholds (hours).
# - fresh: age below this → green
# - stale: age below this → amber; above → red
# Defaults chosen from how often each pipeline actually refreshes:
# market/whale = hourly cron, ETF flows = Farside daily, AI news = a few times
# per day, LTHCS = daily, insights = daily.
DEFAULT = Threshold(fresh_h=6, stale_h=24)
THRESHOLDS: dict[str, Threshold] = {
    # rendered files baked into dashboard.html
    "market.json": Threshold(2, 6),
    "whale.json": Threshold(2, 6),
    "ai_curated.json": Threshold(8, 24),
    "ai_curated_wiki.json": Threshold(8, 24),
    "btc_flows.csv": Threshold(30, 48),
    "eth_flows.csv": Threshold(30, 48),
    "insights_history.json": Threshold(26, 48),
    "shares.json": Threshold(168, 720),  # rarely changes
    # root-level v2 artifacts
    "data-defi.json": Threshold(8, 24),
    "data-whale.json": Threshold(2, 6),
    # high-frequency upstream caches
    "coinbase_spot.json": Threshold(1, 4),
    "mempool_space.json": Threshold(2, 6),
    "etherscan_gas.json": Threshold(2, 6),
    # daily-ish upstream caches
    "fetch_fred.json": Threshold(30, 72),
    "fetch_sec_form_d_filings.json": Threshold(30, 72),
    "fetch_yc_ai_companies.json": Threshold(168, 720),
    # LTHCS pipeline (daily cron)
    "universe.json": Threshold(26, 48),
    "weights.json": Threshold(26, 48),
    "prewarm_status.json": Threshold(26, 48),
    "13f_institutions.json": Threshold(720, 2160),  # quarterly
    "13f_cusip_map.json": Threshold(720, 2160),  # quarterly
}

# Maps each rendered data file to the V1 tab(s) that consume it. Drives the
# "per-tab" section on the health page. Multi-tab entries get listed under
# each tab so a glance at any tab's row tells you if its inputs are fresh.
TAB_INPUTS: dict[str, list[str]] = {
    "Crypto": ["market.json"],
    "Crypto Signals": ["market.json"],
    "Whale": ["whale.json", "data-whale.json"],
    "POC": ["market.json"],
    "ETF Flows": ["btc_flows.csv", "eth_flows.csv"],
    "AI News": ["ai_curated.json", "ai_curated_wiki.json"],
    "Research": ["ai_curated.json"],
    "DeFi": ["data-defi.json"],
    "Futures": ["market.json"],
    "Stocks": ["market.json"],
    "LTHCS": ["lthcs/universe.json"],
    "Insights": ["insights_history.json"],
}


def classify(age_h: float, t: Threshold) -> str:
    if age_h < t.fresh_h:
        return "fresh"
    if age_h < t.stale_h:
        return "stale"
    return "critical"


def humanize_age(age_h: float) -> str:
    if age_h < 1:
        return f"{int(age_h * 60)}m"
    if age_h < 48:
        return f"{age_h:.1f}h"
    return f"{age_h / 24:.1f}d"


def scan(path: Path, rel_to: Path, threshold_key_fn=None) -> list[dict]:
    """Return one entry per regular file under `path`. mtime → age_h → status."""
    rows: list[dict] = []
    if not path.exists():
        return rows
    now = datetime.now(timezone.utc).timestamp()
    for entry in sorted(path.iterdir()):
        if entry.name.startswith("."):
            continue
        if not entry.is_file():
            continue
        if entry.suffix in (".bak", ".tmp"):
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        age_h = (now - mtime) / 3600.0
        key = threshold_key_fn(entry) if threshold_key_fn else entry.name
        t = THRESHOLDS.get(key, DEFAULT)
        rows.append({
            "name": entry.name,
            "path": str(entry.relative_to(rel_to)),
            "size_bytes": entry.stat().st_size,
            "mtime_iso": datetime.fromtimestamp(mtime, timezone.utc).isoformat(),
            "age_h": round(age_h, 2),
            "age_human": humanize_age(age_h),
            "status": classify(age_h, t),
            "fresh_h": t.fresh_h,
            "stale_h": t.stale_h,
        })
    return rows


def collect_rendered() -> list[dict]:
    """Top-level data files baked into dashboard.html."""
    rows = scan(DATA_DIR, REPO_ROOT)
    # also pull root-level data-*.json
    now = datetime.now(timezone.utc).timestamp()
    for name in ("data-defi.json", "data-whale.json"):
        p = REPO_ROOT / name
        if not p.exists():
            continue
        mtime = p.stat().st_mtime
        age_h = (now - mtime) / 3600.0
        t = THRESHOLDS.get(name, DEFAULT)
        rows.append({
            "name": name,
            "path": name,
            "size_bytes": p.stat().st_size,
            "mtime_iso": datetime.fromtimestamp(mtime, timezone.utc).isoformat(),
            "age_h": round(age_h, 2),
            "age_human": humanize_age(age_h),
            "status": classify(age_h, t),
            "fresh_h": t.fresh_h,
            "stale_h": t.stale_h,
        })
    return rows


def collect_stale() -> list[dict]:
    """Upstream fetcher caches (data/.stale/). Mtime here = last successful fetch."""
    return scan(STALE_DIR, REPO_ROOT)


def collect_lthcs() -> list[dict]:
    """data/lthcs/ has its own pipeline — snapshot top-level files first, then
    a sample of nested files. Top-level always wins so the LTHCS tab card's
    input (e.g. universe.json) is guaranteed present even with the cap."""
    p = DATA_DIR / "lthcs"
    if not p.exists():
        return []
    now = datetime.now(timezone.utc).timestamp()

    def row_for(entry: Path) -> dict | None:
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            return None
        rel = entry.relative_to(REPO_ROOT)
        age_h = (now - mtime) / 3600.0
        t = THRESHOLDS.get(entry.name, DEFAULT)
        return {
            "name": str(rel.relative_to(Path("data/lthcs"))),
            "path": str(rel),
            "size_bytes": entry.stat().st_size,
            "mtime_iso": datetime.fromtimestamp(mtime, timezone.utc).isoformat(),
            "age_h": round(age_h, 2),
            "age_human": humanize_age(age_h),
            "status": classify(age_h, t),
            "fresh_h": t.fresh_h,
            "stale_h": t.stale_h,
        }

    top = [row_for(e) for e in sorted(p.glob("*.json")) if e.is_file()]
    nested = [row_for(e) for e in sorted(p.rglob("*.json"))
              if e.is_file() and e.parent != p]
    rows = [r for r in top if r] + [r for r in nested if r]
    return rows[:50]  # cap to keep payload small


def build_tab_view(rendered: list[dict], lthcs: list[dict]) -> list[dict]:
    by_path = {r["path"]: r for r in rendered}
    for r in lthcs:
        by_path[r["path"]] = r
    out = []
    for tab, inputs in TAB_INPUTS.items():
        rows = []
        worst = "fresh"
        for inp in inputs:
            full = f"data/{inp}" if not inp.startswith(("data/", "data-")) else inp
            row = by_path.get(full) or by_path.get(inp)
            if row:
                rows.append(row)
                if row["status"] == "critical":
                    worst = "critical"
                elif row["status"] == "stale" and worst != "critical":
                    worst = "stale"
            else:
                rows.append({"name": inp, "path": inp, "status": "missing",
                             "age_human": "—", "mtime_iso": None})
                worst = "critical"
        out.append({"tab": tab, "status": worst, "inputs": rows})
    return out


def main() -> int:
    rendered = collect_rendered()
    stale = collect_stale()
    lthcs = collect_lthcs()
    tabs = build_tab_view(rendered, lthcs)

    summary = {
        "fresh": sum(1 for r in rendered + stale + lthcs if r.get("status") == "fresh"),
        "stale": sum(1 for r in rendered + stale + lthcs if r.get("status") == "stale"),
        "critical": sum(1 for r in rendered + stale + lthcs if r.get("status") == "critical"),
        "total": len(rendered) + len(stale) + len(lthcs),
    }

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "tabs": tabs,
        "rendered": rendered,
        "upstream": stale,
        "lthcs": lthcs,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2))
    print(f"wrote {OUT_PATH.relative_to(REPO_ROOT)} "
          f"({summary['total']} files: {summary['fresh']} fresh, "
          f"{summary['stale']} stale, {summary['critical']} critical)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
