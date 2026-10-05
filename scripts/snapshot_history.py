#!/usr/bin/env python3
"""Carry deploy-time feeds forward: append one compact row-set per day.

WHY
---
Two sidecars the dashboard serves are rebuilt by every pages.yml run and never
committed back, and neither upstream offers history to re-fetch:

  data-stock-money-flow.json  per-ticker MFI / CMF / flow score, scored from
                              the last daily bar. Yahoo has the bars, but the
                              scored universe and the score are ours.
  data-travel.json            State Department advisory levels. travel.state.gov
                              publishes the CURRENT level only.

So every build computed today's value and threw yesterday's away. The R2
archive (upload_to_r2.py) keeps whole payloads, but nothing the build or the
dashboard can read. This script keeps a compact, committed record instead,
the way data/composites/ does for the composite indexes:

  data/stock_money_flow_history.csv   date,symbol,score,label,mfi,cmf,provenance
                                      one row per scored ticker per trading day
                                      (~15 rows, ~0.7 KB a day, ~170 KB a year)
  data/travel_advisory_levels.csv     date,countries,level_1..level_4,source,provenance
                                      one row per day (~50 B, ~18 KB a year)
  data/travel_advisory_changes.csv    date,country,level,prev_level,advisory_date,provenance
                                      a row only when a country's level changes,
                                      plus the first-seen baseline (~9 KB once,
                                      then a few rows a week)

RULES
-----
* Append-only, keyed by OBSERVATION date. A date already in a file is never
  rewritten, so re-running (hourly) is a no-op and pages.yml commits at most
  once per new date.
* Stock rows are recorded only for bars that are COMPLETE: as_of strictly
  before today (UTC). During US trading hours the last Yahoo bar is today's
  partial bar; recording it would archive an intraday value as the day's.
* Travel rows are recorded only from a payload generated TODAY (a stale-kept
  file says nothing about today), and only when it lists at least 80% of the
  countries the previous row counted (a half-parsed fetch is not a day of
  countries disappearing).
* Never invents a value: a ticker without a numeric score is skipped, not
  zeroed; a country missing from one fetch is not recorded as removed.
* The same pure functions build rows from R2-archived payloads in
  scripts/backfill_history.py, so a backfilled day and a live day are
  produced by identical code.

Run from the repo root: python scripts/snapshot_history.py
"""
from __future__ import annotations

import csv
import io
import json
import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

STOCK_SRC = "data-stock-money-flow.json"
TRAVEL_SRC = "data-travel.json"
STOCK_CSV = "data/stock_money_flow_history.csv"
TRAVEL_LEVELS_CSV = "data/travel_advisory_levels.csv"
TRAVEL_CHANGES_CSV = "data/travel_advisory_changes.csv"

# `provenance` is empty for rows the live build recorded, and names the real
# source of a backfilled row, so a backfilled day is always distinguishable:
#   r2@YYYY-MM-DD           the R2 archive copy raw/alpine-data/<day>/<source>
#   pages@YYYY-MM-DDTHH:MMZ the copy the live Pages site served at that time
# (<source> is the sidecar named in STOCK_SRC / TRAVEL_SRC for that file).
STOCK_HEADER = ["date", "symbol", "score", "label", "mfi", "cmf", "provenance"]
TRAVEL_LEVELS_HEADER = ["date", "countries", "level_1", "level_2", "level_3",
                        "level_4", "source", "provenance"]
TRAVEL_CHANGES_HEADER = ["date", "country", "level", "prev_level",
                         "advisory_date", "provenance"]

# A live stock payload whose newest bar is older than this is a stale-kept
# fallback (the committed repo copy is from 2026-06-08), not a new observation.
STOCK_MAX_LAG_DAYS = 7
TRAVEL_MIN_COVERAGE = 0.8


def _day(v) -> str | None:
    if isinstance(v, str) and len(v) >= 10:
        try:
            return date.fromisoformat(v[:10]).isoformat()
        except ValueError:
            return None
    return None


def _num(v) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _fmt(v) -> str:
    """Compact, lossless-enough CSV cell: integers without .0, else repr."""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


# --------------------------------------------------------------------------
# Row builders (pure)
# --------------------------------------------------------------------------

def stock_rows(payload: dict, today: date, max_lag_days: int | None = STOCK_MAX_LAG_DAYS,
               provenance: str = "") -> list[dict]:
    """Rows for every scored ticker whose bar is complete (as_of < today)."""
    if not isinstance(payload, dict):
        return []
    default = _day(payload.get("as_of"))
    floor = (today - timedelta(days=max_lag_days)).isoformat() if max_lag_days else ""
    out = []
    for s in payload.get("stocks") or []:
        if not isinstance(s, dict) or not s.get("symbol"):
            continue
        d = _day(s.get("as_of")) or default
        score = _num(s.get("score"))
        if not d or score is None or d >= today.isoformat() or d < floor:
            continue
        out.append({
            "date": d,
            "symbol": str(s["symbol"]).upper(),
            "score": _fmt(score),
            "label": s.get("label") if isinstance(s.get("label"), str) else "",
            "mfi": _fmt(_num(s.get("mfi"))),
            "cmf": _fmt(_num(s.get("cmf"))),
            "provenance": provenance,
        })
    return out


def travel_levels(payload: dict) -> dict[str, tuple[int, str]]:
    """{country: (level, advisory_date)} for advisories with a 1-4 level."""
    out = {}
    for a in (payload or {}).get("advisories") or []:
        if not isinstance(a, dict) or not a.get("name"):
            continue
        lvl = a.get("level")
        if isinstance(lvl, bool) or not isinstance(lvl, int) or not 1 <= lvl <= 4:
            continue
        out[str(a["name"]).strip()] = (lvl, _day(a.get("date")) or "")
    return out


def travel_summary(payload: dict, day: str, provenance: str = "") -> dict | None:
    levels = travel_levels(payload)
    if not levels:
        return None
    counts = {n: 0 for n in range(1, 5)}
    for lvl, _ in levels.values():
        counts[lvl] += 1
    return {"date": day, "countries": str(len(levels)),
            **{f"level_{n}": str(counts[n]) for n in range(1, 5)},
            "source": str((payload or {}).get("source") or ""),
            "provenance": provenance}


def travel_change_rows(prev: dict[str, str], payload: dict, day: str,
                       provenance: str = "") -> list[dict]:
    """Rows for countries first seen, or whose level differs from `prev`
    ({country: last recorded level as str}). Absent countries are NOT
    recorded as removed: one thin fetch is not a geopolitical event."""
    out = []
    for name, (lvl, adv_date) in sorted(travel_levels(payload).items()):
        before = prev.get(name)
        if before == str(lvl):
            continue
        out.append({"date": day, "country": name, "level": str(lvl),
                    "prev_level": before or "", "advisory_date": adv_date,
                    "provenance": provenance})
    return out


# --------------------------------------------------------------------------
# CSV plumbing
# --------------------------------------------------------------------------

def read_csv(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: Path, header: list[str], rows: list[dict]) -> None:
    rows = sorted(rows, key=lambda r: tuple(str(r.get(k, "")) for k in header[:2]))
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=header, lineterminator="\n",
                       extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(buf.getvalue(), encoding="utf-8")


def append_new_dates(existing: list[dict], new: list[dict]) -> list[dict]:
    """Rows of `new` whose DATE is not in `existing` at all. Dates already
    recorded are never touched, which is what makes the files append-only and
    re-runs no-ops."""
    have = {r.get("date") for r in existing}
    return [r for r in new if r.get("date") not in have]


def normalize_changes(changes: list[dict]) -> list[dict]:
    """Keep the log meaning "first seen, or level changed" after rows were
    inserted out of order (a backfilled older day lands BEFORE rows the live
    build already wrote). Replays the log by date, drops rows that repeat the
    level already in force, and rewrites prev_level to the level actually in
    force before each change."""
    out, cur = [], {}
    for r in sorted(changes, key=lambda r: (r.get("date", ""), r.get("country", ""))):
        name, lvl = r.get("country"), str(r.get("level", ""))
        if not name:
            continue
        if cur.get(name) == lvl:
            continue
        out.append({**r, "prev_level": cur.get(name, "")})
        cur[name] = lvl
    return out


def last_levels(changes: list[dict]) -> dict[str, str]:
    prev: dict[str, str] = {}
    for r in sorted(changes, key=lambda r: r.get("date", "")):
        if r.get("country"):
            prev[r["country"]] = str(r.get("level", ""))
    return prev


# --------------------------------------------------------------------------
# Apply one day's payloads to the files
# --------------------------------------------------------------------------

def apply_stock(root: Path, payload: dict | None, today: date,
                max_lag_days: int | None = STOCK_MAX_LAG_DAYS,
                provenance: str = "") -> int:
    if not payload:
        return 0
    path = root / STOCK_CSV
    existing = read_csv(path)
    add = append_new_dates(existing, stock_rows(payload, today, max_lag_days,
                                                provenance))
    if add:
        write_csv(path, STOCK_HEADER, existing + add)
    return len(add)


def apply_travel(root: Path, payload: dict | None, day: str,
                 require_generated_on: str | None = None,
                 provenance: str = "") -> tuple[int, int]:
    """(summary rows added, change rows added) for one day's travel payload.
    `require_generated_on` refuses a payload whose generated_at is another
    day — a stale-kept file is not an observation of `day`."""
    if not payload:
        return 0, 0
    if require_generated_on and _day(payload.get("generated_at")) != require_generated_on:
        return 0, 0
    lpath, cpath = root / TRAVEL_LEVELS_CSV, root / TRAVEL_CHANGES_CSV
    levels = read_csv(lpath)
    if any(r.get("date") == day for r in levels):
        return 0, 0
    summary = travel_summary(payload, day, provenance)
    if summary is None:
        return 0, 0
    prior = [r for r in levels if r.get("date", "") < day]
    if prior:
        last_count = int(max(prior, key=lambda r: r["date"]).get("countries") or 0)
        if int(summary["countries"]) < TRAVEL_MIN_COVERAGE * last_count:
            print(f"  [history] travel {day}: {summary['countries']} countries vs "
                  f"{last_count} last time; partial fetch, not recorded")
            return 0, 0
    changes = read_csv(cpath)
    # Only changes up to `day` define the baseline, so a backfill that fills
    # an older day sees the levels as they were then.
    prev = last_levels([r for r in changes if r.get("date", "") < day])
    new_changes = [r for r in travel_change_rows(prev, payload, day, provenance)
                   if not any(c.get("date") == day and c.get("country") == r["country"]
                              for c in changes)]
    write_csv(lpath, TRAVEL_LEVELS_HEADER, levels + [summary])
    if new_changes:
        write_csv(cpath, TRAVEL_CHANGES_HEADER,
                  normalize_changes(changes + new_changes))
    return 1, len(new_changes)


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def main(argv: list[str] | None = None) -> int:
    now = datetime.now(timezone.utc)
    today = now.date()
    n_stock = apply_stock(REPO_ROOT, _load(REPO_ROOT / STOCK_SRC), today)
    n_lvl, n_chg = apply_travel(REPO_ROOT, _load(REPO_ROOT / TRAVEL_SRC),
                                today.isoformat(),
                                require_generated_on=today.isoformat())
    print(f"[history] stock money flow: +{n_stock} rows; travel: +{n_lvl} day, "
          f"+{n_chg} level changes")
    # Never fail the build: a missed row is a gap the continuity monitor
    # reports (scripts/data_health.py), not a reason to withhold the site.
    return 0


if __name__ == "__main__":
    sys.exit(main())
