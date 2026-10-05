#!/usr/bin/env python3
"""Build-time indexes for the static LTHCS pages (run by pages.yml).

GitHub Pages cannot list a directory, so the LTHCS pages used to *discover*
files by guessing URLs: /lthcs/health/pipeline.html walked back day by day
with HEAD requests (285 x 404 and ~35 s of "Loading cron freshness..."),
/lthcs/v2/ asked for today's sector_strength file (404 -> "Sector data
unavailable"), /lthcs/health/ downloaded 30 days of insider + holdings +
snapshot JSON (~50 MB) just to count keys, and the company-detail pillar
toggle downloaded every daily snapshot (~48 MB) to draw five lines.

This script answers those questions once, at deploy time, from the files on
disk, and writes three small artifacts next to the data:

  data/lthcs/file_index.json
      Which date-stamped files exist, per directory / filename prefix
      (newest first), the ISO weeks under trends/, and the JSON files under
      backtest/. Pure listing: no file contents, no timestamps that could be
      mistaken for data dates.

  data/lthcs/health_summary.json
      The per-date coverage counts /lthcs/health/ renders (snapshot tickers,
      insider/13F coverage, macro source counts, pillar data-quality flags)
      for the last HEALTH_DAYS snapshot dates. Every count is computed from
      the same fields the page used to read in the browser.

  data/lthcs/history/pillars_by_ticker/<TICKER>.json
      Per-ticker pillar sub-score history ({date, composite, <pillar>...}),
      the series the detail modal's pillar chips plot.

  data/lthcs/history/trend_index.json
      Every ticker's composite-score rows the /lthcs/ cards need for their
      30-day trend pill, in one file. The index page used to fetch all 215
      history/by_ticker/<T>.json files on first load (249 requests in all)
      to compute one delta per card.

None of these are committed (see .gitignore); they are regenerated on every
deploy from the committed data, so they can never drift from it. The pages
fall back to their old behaviour when a file is missing (e.g. local dev).

Usage: python scripts/build_lthcs_site_index.py [--root data/lthcs]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATE_FILE = re.compile(r"^(?:(?P<prefix>[A-Za-z0-9_]+?)_)?(?P<date>\d{4}-\d{2}-\d{2})\.json$")
WEEK_FILE = re.compile(r"^(?P<week>\d{4}-W\d{2})\.json$")
PILLARS = ("adoption_momentum", "institutional_confidence",
           "financial_evolution", "thesis_integrity", "des")
HEALTH_DAYS = 30
# Single files whose mere presence the pipeline page reports on.
EXISTS_PROBES = ("sentiment_llm/AAPL.json", "sentiment/AAPL.json", "weights.json")


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def build_file_index(root: Path) -> dict:
    """List date-stamped files per directory (one level deep) and prefix.

    Key is the directory relative to ``root`` plus ``/<prefix>`` when the file
    name carries one: ``snapshots``, ``macro/breadth_sentiment``, ...
    """
    dated: dict[str, set[str]] = {}
    weekly: dict[str, set[str]] = {}
    dirs = [root] + sorted(p for p in root.iterdir() if p.is_dir())
    for d in dirs:
        rel = "" if d == root else d.relative_to(root).as_posix()
        for f in d.iterdir():
            if not f.is_file():
                continue
            m = DATE_FILE.match(f.name)
            if m:
                key = "/".join(x for x in (rel, m.group("prefix") or "") if x)
                dated.setdefault(key or ".", set()).add(m.group("date"))
                continue
            w = WEEK_FILE.match(f.name)
            if w:
                weekly.setdefault(rel or ".", set()).add(w.group("week"))
    out_dated = {k: {"latest": max(v), "count": len(v), "dates": sorted(v, reverse=True)}
                 for k, v in sorted(dated.items())}
    out_weekly = {k: {"latest": max(v), "count": len(v), "weeks": sorted(v, reverse=True)}
                  for k, v in sorted(weekly.items())}
    backtest = root / "backtest"
    bt_files = sorted(p.relative_to(backtest).as_posix()
                      for p in backtest.rglob("*.json")) if backtest.is_dir() else []
    return {
        "schema": 1,
        "note": ("Listing of files present at deploy time. Dates are file-name "
                 "dates (observation days), not modification times."),
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dated": out_dated,
        "weekly": out_weekly,
        "backtest_files": bt_files,
        "exists": {p: (root / p).is_file() for p in EXISTS_PROBES},
    }


def _sources(dq) -> tuple[int | None, int | None]:
    if not isinstance(dq, dict):
        return None, None
    ok = dq.get("sources_ok")
    bad = dq.get("sources_failed")
    return (ok if isinstance(ok, int) else None, bad if isinstance(bad, int) else None)


def _pillar_flags(vd) -> dict:
    """Same aggregation renderPillarBreakdown() did in the browser: for each
    pillar, count tickers where each boolean data_quality flag is true."""
    if not isinstance(vd, dict) or not isinstance(vd.get("variables"), list):
        return {}
    by: dict[str, list] = {}
    for v in vd["variables"]:
        if isinstance(v, dict) and v.get("pillar"):
            by.setdefault(v["pillar"], []).append(v)
    out = {}
    for pillar, rows in by.items():
        first = rows[0].get("data_quality") or {}
        flags = {}
        for k, sample in first.items():
            if not isinstance(sample, bool):
                continue
            flags[k] = sum(1 for r in rows if (r.get("data_quality") or {}).get(k) is True)
        out[pillar] = {"total": len(rows), "flags": flags}
    return out


def build_health_summary(root: Path, index: dict, days: int = HEALTH_DAYS) -> dict | None:
    snaps = (index.get("dated") or {}).get("snapshots") or {}
    dates = (snaps.get("dates") or [])[:days]
    if not dates:
        return None
    rows = []
    for d in dates:
        snap = _load(root / "snapshots" / f"{d}.json")
        ins = _load(root / "insider" / f"{d}.json")
        hol = _load(root / "holdings" / f"{d}.json")
        mb = _load(root / "macro" / f"breadth_{d}.json")
        mbs = _load(root / "macro" / f"breadth_sentiment_{d}.json")
        mss = _load(root / "macro" / f"sector_strength_{d}.json")
        mb_ok, mb_bad = _sources((mb or {}).get("data_quality")) if isinstance(mb, dict) else (None, None)
        s_ok, s_bad = _sources((mbs or {}).get("data_quality")) if isinstance(mbs, dict) else (None, None)
        rows.append({
            "date": d,
            "snapshot": isinstance(snap, dict),
            "snapshot_calc_date": (snap or {}).get("calc_date") if isinstance(snap, dict) else None,
            "tickers": len(snap["scores"]) if isinstance(snap, dict) and isinstance(snap.get("scores"), list) else None,
            "insider": len(ins) if isinstance(ins, dict) else None,
            "holdings_covered": (sum(1 for v in hol.values()
                                     if isinstance(v, dict) and (v.get("manager_count") or 0) > 0)
                                 if isinstance(hol, dict) else None),
            "macro_as_of": (mb or {}).get("as_of") if isinstance(mb, dict) else None,
            "macro_ok": mb_ok, "macro_failed": mb_bad,
            "sentiment_ok": s_ok, "sentiment_failed": s_bad,
            "sectors": (len(mss["sectors"]) if isinstance(mss, dict) and isinstance(mss.get("sectors"), dict) else None),
        })
    latest = dates[0]
    vd = _load(root / "variable_detail" / f"{latest}.json")
    ab = _load(root / "analyst_breadth" / f"{latest}.json")
    weeks = ((index.get("weekly") or {}).get("trends") or {}).get("weeks") or []
    trends = _load(root / "trends" / f"{weeks[0]}.json") if weeks else None
    uni = _load(root / "universe.json")
    return {
        "schema": 1,
        "latest": latest,
        "snapshot_dates": snaps.get("dates") or [],
        "universe_size": len(uni["tickers"]) if isinstance(uni, dict) and isinstance(uni.get("tickers"), list) else None,
        "variable_detail": ({"calc_date": vd.get("calc_date") or latest, "pillars": _pillar_flags(vd)}
                            if isinstance(vd, dict) else None),
        "analyst": len(ab) if isinstance(ab, dict) else None,
        "trends": ({"week": weeks[0], "as_of": trends.get("as_of"),
                    "tickers": len(trends.get("tickers") or {}),
                    "terms": len(trends.get("term_map") or {})}
                   if isinstance(trends, dict) else None),
        "days": rows,
    }


def build_pillar_history(root: Path, index: dict) -> dict[str, list]:
    """ticker -> chronological [{date, composite, <pillar>: value}]."""
    snaps = (index.get("dated") or {}).get("snapshots") or {}
    by: dict[str, list] = {}
    for d in sorted(snaps.get("dates") or []):
        snap = _load(root / "snapshots" / f"{d}.json")
        if not isinstance(snap, dict) or not isinstance(snap.get("scores"), list):
            continue
        for row in snap["scores"]:
            if not isinstance(row, dict) or not row.get("ticker"):
                continue
            subs = row.get("subscores") or {}
            entry = {"date": d, "composite": row.get("lthcs_score")}
            for p in PILLARS:
                entry[p] = subs.get(p)
            by.setdefault(str(row["ticker"]), []).append(entry)
    return by


TREND_LOOKBACK_DAYS = 30   # max(TREND_FALLBACK_DAYS) in lthcs_tab/lthcs-tab.js


def build_trend_index(root: Path, index: dict, lookback: int = TREND_LOOKBACK_DAYS) -> dict | None:
    """{ticker: [{date, score}]} trimmed so the browser's anchor pick is
    unchanged. pickAnchorForLookback() takes the newest row on or before
    (calc_date - N days) for N <= lookback, so it only ever reads rows after
    latest - lookback plus the newest row at or before that cutoff. Keeping
    exactly those gives the same anchor as the full file for calc_date =
    latest (the page only uses the index when its `latest` matches)."""
    latest = ((index.get("dated") or {}).get("snapshots") or {}).get("latest")
    hist_dir = root / "history" / "by_ticker"
    if not latest or not hist_dir.is_dir():
        return None
    cutoff = (datetime.strptime(latest, "%Y-%m-%d")
              - timedelta(days=lookback)).strftime("%Y-%m-%d")
    out: dict[str, list] = {}
    for f in sorted(hist_dir.glob("*.json")):
        doc = _load(f)
        rows = doc.get("history") if isinstance(doc, dict) else None
        if not isinstance(rows, list):
            continue
        ticker = str(doc.get("ticker") or f.stem)
        if not SAFE_TICKER.match(ticker):
            continue
        clean = sorted(
            ({"date": r["date"], "score": r["score"]} for r in rows
             if isinstance(r, dict) and isinstance(r.get("date"), str)
             and isinstance(r.get("score"), (int, float)) and r["score"] == r["score"]),
            key=lambda r: r["date"])
        before = [r for r in clean if r["date"] <= cutoff]
        out[ticker] = before[-1:] + [r for r in clean if r["date"] > cutoff]
    return {"schema": 1, "latest": latest, "lookback_days": lookback,
            "note": ("Trimmed copy of history/by_ticker/*.json for the /lthcs/ "
                     "trend pills; the per-ticker files remain the source."),
            "tickers": out}


SAFE_TICKER = re.compile(r"^[A-Za-z0-9.\-^=]{1,15}$")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default="data/lthcs")
    args = ap.parse_args(argv)
    root = Path(args.root)
    if not root.is_dir():
        print(f"[lthcs-index] {root} not found; nothing to do")
        return 0
    index = build_file_index(root)
    (root / "file_index.json").write_text(json.dumps(index, separators=(",", ":")), encoding="utf-8")
    summary = build_health_summary(root, index)
    if summary is not None:
        (root / "health_summary.json").write_text(json.dumps(summary, separators=(",", ":")), encoding="utf-8")
    hist = build_pillar_history(root, index)
    out = root / "history" / "pillars_by_ticker"
    out.mkdir(parents=True, exist_ok=True)
    written = 0
    for t, rows in hist.items():
        if not SAFE_TICKER.match(t):
            continue
        (out / f"{t}.json").write_text(
            json.dumps({"ticker": t, "pillars": list(PILLARS), "history": rows}, separators=(",", ":")),
            encoding="utf-8")
        written += 1
    trend = build_trend_index(root, index)
    if trend is not None:
        (root / "history" / "trend_index.json").write_text(
            json.dumps(trend, separators=(",", ":")), encoding="utf-8")
    print(f"[lthcs-index] file_index: {len(index['dated'])} dated keys, "
          f"{len(index['backtest_files'])} backtest files; health_summary: "
          f"{len(summary['days']) if summary else 0} days; pillar history: {written} tickers; "
          f"trend index: {len(trend['tickers']) if trend else 0} tickers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
