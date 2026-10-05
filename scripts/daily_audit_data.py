#!/usr/bin/env python3
"""Daily audit, data half: feeds, workflows, schedules, API status, spot checks.

Run by .github/workflows/daily-audit.yml once a day; its JSON output is merged
with the UX half (scripts/daily_audit_ux.mjs) by scripts/daily_audit_report.py
into audit/daily/YYYY-MM-DD.{json,md}.

What it collects (each section is isolated: a network or API failure in one
becomes ``{"status": "error", ...}`` for that section and never stops the
others, so a flaky upstream degrades the report instead of erasing it):

  data_health   ``scripts/data_health.py --mode committed --report json`` —
                reused verbatim; freshness is NOT reimplemented here.
  workflows     every workflow's runs on main over the last 7 days (GitHub
                REST, ``actions: read``): latest conclusion in the last 24h,
                the day before, and for failures the failed jobs' annotation
                titles/messages (``/check-runs/<job>/annotations``).
  schedules     every workflow with an ``on.schedule`` cron: which of the
                last 7 days it was due, which of those days actually got a
                scheduled run, the last scheduled run and its conclusion.
  api_status    ``data/health/api_status.json`` verdict counts, and per-source
                verdict changes vs. the committed copy from ~24h earlier.
  spot_checks   cheap correctness checks of the LIVE site against primary
                sources that need no secrets (CoinGecko, alternative.me,
                mempool.space, treasury.gov) plus the live build's age.
  live_payloads the ``as_of`` / generated stamps of every sidecar JSON the
                live V1 page loads.

This script observes; it does not grade. Severity and new-vs-ongoing are the
report builder's job.

Usage:
    python scripts/daily_audit_data.py --out data.json
    python scripts/daily_audit_data.py --skip workflows,schedules   # offline-ish

GitHub access uses $GITHUB_TOKEN / $GH_TOKEN; without a token it falls back to
the ``gh api`` CLI when present (handy locally), else the GitHub sections
report ``error``.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPO = "btabiado/alpine-data"
DEFAULT_BASE = "https://btabiado.github.io/alpine-data"
UA = "alpine-data-daily-audit/1.0 (+https://github.com/btabiado/alpine-data)"
HTTP_TIMEOUT_S = 20

# Conclusions that mean "this run broke" (cancelled/skipped do not: pages.yml
# cancels superseded runs every hour by design).
FAILED_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})

# Spot-check tolerances.
PRICE_TOLERANCE_PCT = 1.0
TREASURY_TOLERANCE = 0.02            # percentage points
TREASURY_MAX_LAG_DAYS = 6            # FRED trails treasury.gov by ~1 business day
LIVE_BUILD_MAX_AGE_H = 6.0           # pages.yml rebuilds hourly
SCHEDULE_GRACE = timedelta(hours=10)  # GitHub cron here routinely starts 5-8 h late


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def clip(s: Any, n: int = 300) -> str:
    s = "" if s is None else str(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def parse_ts(value: Any) -> datetime | None:
    """Best-effort timestamp parser for the stamps the feeds actually use.

    Handles ISO-8601 (with Z, offset, or naive = UTC), date-only, ``YYYY-MM``,
    ``M/D/YYYY``, ``YYYY-MM-DD HH:MM UTC`` and epoch seconds/milliseconds.
    Returns an aware UTC datetime, or None for prose (``"FAA airman data …"``).
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e12:
            v /= 1000.0
        if 1e9 < v < 1e10:
            return datetime.fromtimestamp(v, tz=timezone.utc)
        return None
    s = str(value).strip()
    if not s:
        return None
    if re.fullmatch(r"\d{10}(\.\d+)?|\d{13}", s):
        return parse_ts(float(s))
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})(?::\d{2})?\s*UTC", s)
    if m:
        s = f"{m.group(1)}T{m.group(2)}:00+00:00"
    if re.fullmatch(r"\d{4}-\d{2}", s):
        s += "-01"
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        s = f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def age_hours(dt: datetime | None, now: datetime) -> float | None:
    return None if dt is None else round((now - dt).total_seconds() / 3600.0, 2)


def http_get(url: str, *, timeout: float = HTTP_TIMEOUT_S, headers: dict | None = None,
             attempts: int = 2) -> tuple[int, bytes]:
    """GET with one retry on network errors / 5xx. Returns (status, body)."""
    last: Exception | None = None
    for i in range(attempts):
        req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            retryable = e.code >= 500 or e.code == 429
            if not retryable or i == attempts - 1:
                return e.code, e.read() if hasattr(e, "read") else b""
            last = e
            if e.code == 429:        # free-tier rate limit: back off properly
                time.sleep(8 * (i + 1))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET {url} failed: {last}")


def get_json(url: str, **kw) -> Any:
    status, body = http_get(url, **kw)
    if status != 200:
        raise RuntimeError(f"GET {url} -> HTTP {status}")
    return json.loads(body)


def run_section(name: str, fn: Callable[[], dict]) -> dict:
    """Run one section; any exception becomes that section's error result."""
    t0 = time.monotonic()
    try:
        out = fn()
    except Exception as e:  # noqa: BLE001 - isolation is the point
        out = {"status": "error", "error": clip(f"{type(e).__name__}: {e}", 400)}
    out.setdefault("status", "ok")
    out["elapsed_s"] = round(time.monotonic() - t0, 2)
    print(f"[audit-data] {name}: {out['status']} ({out['elapsed_s']}s)", file=sys.stderr)
    return out


# ---------------------------------------------------------------------------
# GitHub REST
# ---------------------------------------------------------------------------

class GitHub:
    """Minimal REST client: token via urllib, else the ``gh api`` CLI."""

    def __init__(self, repo: str, token: str | None = None) -> None:
        self.repo = repo
        self.token = token
        self.use_cli = not token and shutil.which("gh") is not None
        if not token and not self.use_cli:
            raise RuntimeError("no GITHUB_TOKEN/GH_TOKEN and no gh CLI available")
        self.calls = 0

    def get(self, path: str, params: dict | None = None) -> Any:
        path = path.replace("{repo}", self.repo)
        if params:
            path += ("&" if "?" in path else "?") + urllib.parse.urlencode(params, safe=":>=<")
        self.calls += 1
        if self.use_cli:
            proc = subprocess.run(["gh", "api", path.lstrip("/")], capture_output=True,
                                  text=True, timeout=60)
            if proc.returncode != 0:
                raise RuntimeError(f"gh api {path}: {clip(proc.stderr, 200)}")
            return json.loads(proc.stdout or "null")
        url = "https://api.github.com" + (path if path.startswith("/") else "/" + path)
        status, body = http_get(url, timeout=30, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        if status != 200:
            raise RuntimeError(f"GitHub {path} -> HTTP {status}: {clip(body.decode('utf-8', 'replace'), 200)}")
        return json.loads(body)

    def paginate(self, path: str, key: str, params: dict | None = None,
                 max_pages: int = 5) -> list:
        out: list = []
        for page in range(1, max_pages + 1):
            data = self.get(path, {**(params or {}), "per_page": 100, "page": page})
            items = data.get(key, []) if isinstance(data, dict) else data
            out.extend(items)
            if len(items) < 100:
                break
        return out


# ---------------------------------------------------------------------------
# Cron parsing (enough of POSIX cron for GitHub Actions schedules)
# ---------------------------------------------------------------------------

_DOW_NAMES = {n: i for i, n in enumerate(["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"])}
_MON_NAMES = {n: i + 1 for i, n in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


def _cron_field(spec: str, lo: int, hi: int, names: dict | None = None) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip().upper()
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
        if names:
            for n, v in names.items():
                part = part.replace(n, str(v))
        if part in ("*", "?"):
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
        else:
            a = int(part)
            b = hi if step > 1 else a
        out.update(range(a, b + 1, step))
    return out


def cron_fire_times(expr: str, start: datetime, end: datetime) -> list[datetime]:
    """Every UTC minute in [start, end) at which ``expr`` fires."""
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"not a 5-field cron: {expr!r}")
    minutes = sorted(_cron_field(fields[0], 0, 59))
    hours = sorted(_cron_field(fields[1], 0, 23))
    doms = _cron_field(fields[2], 1, 31)
    months = _cron_field(fields[3], 1, 12, _MON_NAMES)
    dows = {d % 7 for d in _cron_field(fields[4], 0, 7, _DOW_NAMES)}
    dom_any = fields[2] in ("*", "?")
    dow_any = fields[4] in ("*", "?")
    out: list[datetime] = []
    day = start.astimezone(timezone.utc).date()
    while day <= end.date():
        cron_dow = (day.weekday() + 1) % 7
        if dom_any and dow_any:
            day_ok = True
        elif dom_any:
            day_ok = cron_dow in dows
        elif dow_any:
            day_ok = day.day in doms
        else:  # both restricted: Vixie cron ORs them
            day_ok = day.day in doms or cron_dow in dows
        if day_ok and day.month in months:
            for h in hours:
                for m in minutes:
                    t = datetime(day.year, day.month, day.day, h, m, tzinfo=timezone.utc)
                    if start <= t < end:
                        out.append(t)
        day += timedelta(days=1)
    return out


def workflow_crons(workflows_dir: Path) -> dict[str, list[str]]:
    """``{".github/workflows/x.yml": [cron, ...]}`` for every scheduled workflow."""
    out: dict[str, list[str]] = {}
    try:
        import yaml  # PyYAML is in requirements.txt
    except ImportError:  # pragma: no cover - fallback for bare runners
        yaml = None
    for f in sorted(workflows_dir.glob("*.y*ml")):
        text = f.read_text(encoding="utf-8")
        crons: list[str] = []
        if yaml is not None:
            try:
                doc = yaml.safe_load(text) or {}
                on = doc.get("on", doc.get(True)) or {}   # YAML 1.1 reads `on:` as True
                sched = on.get("schedule") if isinstance(on, dict) else None
                crons = [s["cron"] for s in (sched or []) if isinstance(s, dict) and s.get("cron")]
            except Exception:  # noqa: BLE001
                crons = []
        if not crons:
            crons = re.findall(r"^\s*-\s*cron:\s*[\"']([^\"']+)[\"']", text, flags=re.M)
        if crons:
            out[f".github/workflows/{f.name}"] = [c.strip() for c in crons]
    return out


def evaluate_schedule(crons: list[str], runs: list[dict], now: datetime,
                      created_at: datetime | None = None, days: int = 7) -> dict:
    """Which of the last ``days`` days the crons were due, and which got a run.

    ``runs`` are this workflow's runs with ``event == "schedule"``. GitHub's
    scheduler is best-effort: on this repo top-of-the-hour crons routinely
    start 5-8 hours late, and hourly crons lose most of their ticks. So a due
    time counts as served by any scheduled run created from 15 min before it
    up to 15 min before the NEXT due time (capped at now). A day is MISSED
    only when none of its due times got a run at all.

    Due times later than ``now - SCHEDULE_GRACE`` are not judged yet; that is
    also why the audit itself runs well after the overnight crons.
    """
    window_start = now - timedelta(days=days)
    if created_at and created_at > window_start:
        window_start = created_at
    judge_until = now - SCHEDULE_GRACE
    fires: list[datetime] = []
    for c in crons:
        # One extra day past `now` so the last judged fire knows its successor.
        fires.extend(cron_fire_times(c, window_start, now + timedelta(days=1)))
    fires = sorted(set(fires))
    run_times = sorted(t for t in (parse_ts(r.get("created_at")) for r in runs) if t)
    slack = timedelta(minutes=15)
    served: dict[date, int] = {}
    due: dict[date, int] = {}
    delays: list[float] = []
    for i, t in enumerate(fires):
        if t >= judge_until:
            break
        nxt = fires[i + 1] if i + 1 < len(fires) else now
        hi = min(nxt, now) - slack
        hit = next((rt for rt in run_times if t - slack <= rt < max(hi, t + slack)), None)
        due[t.date()] = due.get(t.date(), 0) + 1
        if hit is not None:
            served[t.date()] = served.get(t.date(), 0) + 1
            delays.append(max(0.0, (hit - t).total_seconds() / 60.0))
    due_runs = sum(due.values())
    missed = sorted(d.isoformat() for d in due if not served.get(d))
    delays.sort()
    median = delays[len(delays) // 2] if delays else None
    return {
        "due_days": len(due),
        "due_runs": due_runs,
        "days_with_run": len(due) - len(missed),
        "missed_days": missed,
        "scheduled_runs": sum(1 for rt in run_times if rt >= now - timedelta(days=days)),
        "coverage_pct": round(100.0 * sum(served.values()) / due_runs, 1) if due_runs else None,
        "median_delay_min": round(median) if median is not None else None,
    }


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def section_data_health(repo_root: Path) -> dict:
    proc = subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "data_health.py"),
         "--mode", "committed", "--report", "json"],
        capture_output=True, text=True, timeout=900, cwd=str(repo_root),
        env={**os.environ, "GITHUB_STEP_SUMMARY": ""},
    )
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"status": "error", "exit_code": proc.returncode,
                "error": clip(f"unparseable output; stderr: {proc.stderr[-400:]}", 400)}
    failing = _failing_statuses(repo_root)
    results = [
        {k: (clip(v, 300) if k == "detail" else v) for k, v in r.items()}
        for r in report.get("results", [])
    ]
    # Forward-compatible: any extra top-level list of {status: ...} records
    # (e.g. a history-continuity check) is carried through and judged by the
    # same failing-status set.
    extra: dict[str, Any] = {}
    for k, v in report.items():
        if k in ("mode", "checked_at", "healthy", "results", "remediation"):
            continue
        extra[k] = v
    extra_failing: list[dict] = []
    for k, v in extra.items():
        extra_failing.extend(collect_failing(v, k, failing))
    return {
        "status": "ok",
        "exit_code": proc.returncode,
        "healthy": bool(report.get("healthy")),
        "checked_at": report.get("checked_at"),
        "failing_statuses": sorted(failing),
        "counts": dict(Counter(r.get("status") for r in results)),
        "results": results,
        "extra": json.loads(json.dumps(extra, default=str)) if extra else {},
        "extra_failing": extra_failing,
        "remediation": report.get("remediation", []),
    }


def collect_failing(obj: Any, section: str, failing: set[str], depth: int = 0) -> list[dict]:
    """Records carrying a failing ``status`` anywhere (2 levels) under ``obj``.

    Lets a check data_health.py grows later (e.g. history continuity) reach
    the report without this script knowing its exact shape: a list of
    records, a dict of named records, or one record with a status.
    """
    out: list[dict] = []
    if depth > 2:
        return out
    if isinstance(obj, dict):
        if str(obj.get("status", "")).lower() in failing:
            out.append({"section": section,
                        **{k: (clip(v, 300) if isinstance(v, str) else v)
                           for k, v in obj.items() if not isinstance(v, (dict, list))}})
            return out
        for name, v in obj.items():
            for rec in collect_failing(v, section, failing, depth + 1):
                rec.setdefault("name", name)
                out.append(rec)
    elif isinstance(obj, list):
        for v in obj:
            out.extend(collect_failing(v, section, failing, depth + 1))
    return out


def _failing_statuses(repo_root: Path) -> set[str]:
    """data_health.py's own FAILING_STATUSES — it decides what counts."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_dh_for_audit", repo_root / "scripts" / "data_health.py")
        mod = importlib.util.module_from_spec(spec)
        # Registered before exec: data_health declares @dataclass types, and
        # dataclass resolves annotations through sys.modules[cls.__module__];
        # unregistered, exec raised and this always fell back to the
        # hard-coded set below, which misses statuses data_health adds
        # (history continuity's "gap" / "duplicate").
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return {str(s) for s in mod.FAILING_STATUSES}
    except Exception:  # noqa: BLE001
        return {"stale", "unknown", "missing", "unwatched", "expired",
                "gap", "duplicate"}


def _summarize_run(r: dict) -> dict:
    return {
        "id": r.get("id"), "conclusion": r.get("conclusion"), "status": r.get("status"),
        "event": r.get("event"), "created_at": r.get("created_at"),
        "url": r.get("html_url"), "run_number": r.get("run_number"),
    }


def _annotations(gh: GitHub, run_id: int) -> list[dict]:
    jobs = gh.get(f"/repos/{{repo}}/actions/runs/{run_id}/jobs", {"filter": "latest", "per_page": 50})
    out: list[dict] = []
    for job in (jobs.get("jobs") or []):
        if job.get("conclusion") not in FAILED_CONCLUSIONS:
            continue
        failed_steps = [s.get("name") for s in (job.get("steps") or [])
                        if s.get("conclusion") in FAILED_CONCLUSIONS]
        anns: list[dict] = []
        try:
            raw = gh.get(f"/repos/{{repo}}/check-runs/{job['id']}/annotations", {"per_page": 50})
            for a in raw or []:
                if a.get("annotation_level") not in ("failure", "warning"):
                    continue
                anns.append({"level": a.get("annotation_level"), "title": clip(a.get("title"), 120),
                             "message": clip(a.get("message"), 300), "path": a.get("path")})
        except Exception as e:  # noqa: BLE001
            anns.append({"level": "error", "title": "annotations unavailable", "message": clip(e, 200)})
        # Failures first, then warnings; cap the list.
        anns.sort(key=lambda a: 0 if a["level"] == "failure" else 1)
        out.append({"job": job.get("name"), "url": job.get("html_url"),
                    "failed_steps": failed_steps[:5], "annotations": anns[:5]})
        if len(out) >= 3:
            break
    return out


def section_workflows_and_schedules(gh: GitHub, repo_root: Path, now: datetime,
                                    days: int = 7) -> tuple[dict, dict]:
    wfs = gh.paginate("/repos/{repo}/actions/workflows", "workflows", max_pages=2)
    crons_by_path = workflow_crons(repo_root / ".github" / "workflows")
    since = now - timedelta(days=days)
    workflows: list[dict] = []
    schedules: list[dict] = []
    for wf in wfs:
        name, path = wf.get("name"), wf.get("path", "")
        entry: dict[str, Any] = {"name": name, "path": path, "state": wf.get("state")}
        try:
            runs = gh.paginate(f"/repos/{{repo}}/actions/workflows/{wf['id']}/runs", "workflow_runs",
                               {"branch": "main", "created": f">={iso(since)}",
                                "exclude_pull_requests": "true"}, max_pages=4)
        except Exception as e:  # noqa: BLE001
            entry["error"] = clip(e, 200)
            workflows.append(entry)
            continue
        completed = [r for r in runs if r.get("status") == "completed"]
        completed.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        d1 = [r for r in completed if (parse_ts(r.get("created_at")) or since) >= now - timedelta(hours=24)]
        d2 = [r for r in completed
              if now - timedelta(hours=48) <= (parse_ts(r.get("created_at")) or since) < now - timedelta(hours=24)]
        entry["runs_24h"] = dict(Counter(r.get("conclusion") for r in d1))
        entry["latest_24h"] = _summarize_run(d1[0]) if d1 else None
        entry["previous_day"] = _summarize_run(d2[0]) if d2 else None
        latest_failed = bool(d1) and d1[0].get("conclusion") in FAILED_CONCLUSIONS
        prev_failed = bool(d2) and d2[0].get("conclusion") in FAILED_CONCLUSIONS
        entry["failed_24h"] = latest_failed
        entry["failing_2_days"] = latest_failed and prev_failed
        if latest_failed:
            try:
                entry["failed_jobs"] = _annotations(gh, d1[0]["id"])
            except Exception as e:  # noqa: BLE001
                entry["failed_jobs"] = [{"job": "?", "annotations": [
                    {"level": "error", "title": "jobs unavailable", "message": clip(e, 200)}]}]
        workflows.append(entry)

        crons = crons_by_path.get(path)
        if not crons:
            continue
        sched_runs = [r for r in runs if r.get("event") == "schedule"]
        s: dict[str, Any] = {"name": name, "path": path, "crons": crons, "state": wf.get("state")}
        try:
            s.update(evaluate_schedule(crons, sched_runs, now, parse_ts(wf.get("created_at")), days))
        except ValueError as e:
            s["error"] = clip(e, 200)
        last = sorted((r for r in sched_runs if r.get("status") == "completed"),
                      key=lambda r: r.get("created_at") or "", reverse=True)
        if not last:
            try:
                older = gh.get(f"/repos/{{repo}}/actions/workflows/{wf['id']}/runs",
                               {"event": "schedule", "status": "completed", "per_page": 1})
                last = older.get("workflow_runs") or []
            except Exception:  # noqa: BLE001
                last = []
        s["last_run"] = _summarize_run(last[0]) if last else None
        s["last_failed"] = bool(last) and last[0].get("conclusion") in FAILED_CONCLUSIONS
        schedules.append(s)
    workflows.sort(key=lambda w: w.get("name") or "")
    schedules.sort(key=lambda w: w.get("name") or "")
    return ({"status": "ok", "window_h": 24, "workflows": workflows, "api_calls": gh.calls},
            {"status": "ok", "window_days": days, "schedules": schedules})


def _verdicts(snapshot: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for s in snapshot.get("sources") or []:
        label = s.get("label") or s.get("host")
        if label:
            out[str(label)] = str(s.get("verdict"))
    return out


def section_api_status(repo_root: Path, gh: GitHub | None, now: datetime) -> dict:
    path = repo_root / "data" / "health" / "api_status.json"
    snap = json.loads(path.read_text(encoding="utf-8"))
    verdicts = _verdicts(snap)
    out: dict[str, Any] = {
        "status": "ok",
        "generated_at": snap.get("generated_at"),
        "summary": snap.get("summary"),
        "counts": dict(Counter(verdicts.values())),
        "verdicts": verdicts,
        "unwired_key_envs": snap.get("unwired_key_envs") or [],
        "previous": None,
        "changes": [],
    }
    if gh is not None:
        try:
            commits = gh.get("/repos/{repo}/commits", {
                "path": "data/health/api_status.json",
                "until": iso(now - timedelta(hours=24)), "per_page": 1})
            if commits:
                sha = commits[0]["sha"]
                blob = gh.get("/repos/{repo}/contents/data/health/api_status.json", {"ref": sha})
                prev = json.loads(base64.b64decode(blob["content"]).decode("utf-8"))
                out["previous"] = {"source": f"git:{sha[:10]}", "generated_at": prev.get("generated_at"),
                                   "counts": dict(Counter(_verdicts(prev).values())),
                                   "verdicts": _verdicts(prev)}
        except Exception as e:  # noqa: BLE001
            out["previous_error"] = clip(e, 200)
    if out["previous"]:
        out["changes"] = diff_verdicts(out["previous"]["verdicts"], verdicts)
    return out


def diff_verdicts(prev: dict[str, str], cur: dict[str, str]) -> list[dict]:
    changes = []
    for label in sorted(set(prev) | set(cur)):
        a, b = prev.get(label), cur.get(label)
        if a != b:
            changes.append({"source": label, "from": a, "to": b})
    return changes


# ---- live site --------------------------------------------------------------

class LiveSite:
    """Fetches the live V1 page once and exposes its inline ``DATA``."""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self._html: str | None = None
        self._data: dict | None = None
        self._coingecko: dict | None = None

    @property
    def html(self) -> str:
        if self._html is None:
            status, body = http_get(self.base + "/", timeout=60)
            if status != 200:
                raise RuntimeError(f"live index -> HTTP {status}")
            self._html = body.decode("utf-8", "replace")
        return self._html

    @property
    def data(self) -> dict:
        if self._data is None:
            self._data = extract_inline_data(self.html)
        return self._data

    def coingecko(self) -> dict:
        """One CoinGecko call for every coin checked (its free tier 429s fast)."""
        if self._coingecko is None:
            self._coingecko = get_json(
                "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum"
                "&vs_currencies=usd&include_last_updated_at=true", attempts=3)
        return self._coingecko


def extract_inline_data(html: str) -> dict:
    marker = re.search(r"const\s+DATA\s*=\s*", html)
    if not marker:
        raise RuntimeError("no `const DATA =` payload in the live page")
    obj, _ = json.JSONDecoder().raw_decode(html[marker.end():])
    return obj


def _dig(d: Any, *keys: str) -> Any:
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def check_price_vs_coingecko(site: LiveSite, now: datetime, coin: str, symbol: str) -> dict:
    page = _dig(site.data, "market", "coinbase", symbol) or {}
    page_price = page.get("price_usd")
    page_time = parse_ts(page.get("time")) or parse_ts(_dig(site.data, "market", "fetched_at"))
    if page_price is None:
        return {"status": "error", "error": f"no market.coinbase.{symbol}.price_usd in live DATA"}
    ref = float(site.coingecko()[coin]["usd"])
    diff = abs(float(page_price) - ref) / ref * 100.0
    return {
        "status": "ok" if diff <= PRICE_TOLERANCE_PCT else "fail",
        "page": page_price, "reference": ref, "diff_pct": round(diff, 3),
        "tolerance_pct": PRICE_TOLERANCE_PCT, "page_age_h": age_hours(page_time, now),
        "source": "api.coingecko.com simple/price",
    }


def check_fear_greed(site: LiveSite, now: datetime) -> dict:
    series = _dig(site.data, "market", "fear_greed") or []
    if not series:
        return {"status": "error", "error": "no market.fear_greed series in live DATA"}
    last = series[-1]
    src = get_json("https://api.alternative.me/fng/?limit=10&format=json")
    ref = {datetime.fromtimestamp(int(x["timestamp"]), tz=timezone.utc).date().isoformat(): int(x["value"])
           for x in src.get("data", [])}
    latest_ref = max(ref) if ref else None
    page_date, page_val = str(last.get("date")), last.get("value")
    out = {"page_date": page_date, "page": page_val, "reference": ref.get(page_date),
           "reference_latest_date": latest_ref, "source": "api.alternative.me/fng"}
    if page_date not in ref:
        out["status"] = "fail"
        out["note"] = "page's latest date is not in the source's last 10 days"
    elif ref[page_date] != page_val:
        out["status"] = "fail"
        out["note"] = "value differs from the source for the same date"
    elif latest_ref and (date.fromisoformat(latest_ref) - date.fromisoformat(page_date)).days > 1:
        out["status"] = "fail"
        out["note"] = "page is more than one day behind the source"
    else:
        out["status"] = "ok"
    return out


def check_btc_tip_height(site: LiveSite, now: datetime) -> dict:
    mp = _dig(site.data, "market", "mempool") or {}
    page_h = mp.get("tip_height")
    if page_h is None:
        return {"status": "error", "error": "no market.mempool.tip_height in live DATA"}
    page_time = parse_ts(mp.get("fetched_at")) or parse_ts(site.data.get("generated_at"))
    status, body = http_get("https://mempool.space/api/blocks/tip/height")
    if status != 200:
        raise RuntimeError(f"mempool.space -> HTTP {status}")
    live_h = int(body.decode().strip())
    age = age_hours(page_time, now) or 0.0
    allowed = int(age * 6 * 2) + 12      # ~6 blocks/h, generous for variance
    lag = live_h - int(page_h)
    ok = -2 <= lag <= allowed
    return {"status": "ok" if ok else "fail", "page": page_h, "reference": live_h,
            "lag_blocks": lag, "allowed_lag_blocks": allowed, "page_age_h": age,
            "source": "mempool.space/api/blocks/tip/height"}


def check_treasury_10y(site: LiveSite, now: datetime) -> dict:
    """FRED-free: the page's 10Y (from FRED DGS10) vs treasury.gov's own CSV."""
    series = _dig(site.data, "market", "fred", "treasury_10y") or []
    if not series:
        return {"status": "error", "error": "no market.fred.treasury_10y in live DATA"}
    last = series[-1]
    page_date = date.fromisoformat(str(last["date"])[:10])
    rows: dict[date, float] = {}
    for year in sorted({page_date.year, now.year}):
        url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
               f"daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve"
               f"&field_tdr_date_value={year}&page&_format=csv")
        status, body = http_get(url, timeout=30)
        if status != 200:
            raise RuntimeError(f"treasury.gov -> HTTP {status}")
        lines = body.decode("utf-8", "replace").splitlines()
        header = [h.strip().strip('"') for h in lines[0].split(",")]
        col = header.index("10 Yr")
        for ln in lines[1:]:
            cells = ln.split(",")
            try:
                m, d, y = (int(x) for x in cells[0].strip('"').split("/"))
                rows[date(y, m, d)] = float(cells[col])
            except (ValueError, IndexError):
                continue
    if not rows:
        raise RuntimeError("treasury.gov CSV had no rows")
    latest = max(rows)
    ref = rows.get(page_date)
    lag_days = (latest - page_date).days
    out = {"page_date": page_date.isoformat(), "page": last.get("value"), "reference": ref,
           "reference_latest_date": latest.isoformat(), "lag_days": lag_days,
           "source": "home.treasury.gov daily par yield curve (10 Yr)"}
    if ref is None:
        out["status"] = "fail"
        out["note"] = "treasury.gov has no 10 Yr value for the page's date"
    elif abs(float(last["value"]) - ref) > TREASURY_TOLERANCE:
        out["status"] = "fail"
        out["note"] = f"differs by more than {TREASURY_TOLERANCE} pp"
    elif lag_days > TREASURY_MAX_LAG_DAYS:
        out["status"] = "fail"
        out["note"] = f"page is {lag_days} days behind treasury.gov"
    else:
        out["status"] = "ok"
    return out


def check_live_build_age(site: LiveSite, now: datetime) -> dict:
    gen = parse_ts(site.data.get("generated_at"))
    if gen is None:
        return {"status": "error", "error": "live DATA has no parseable generated_at"}
    age = age_hours(gen, now)
    return {"status": "ok" if age <= LIVE_BUILD_MAX_AGE_H else "fail",
            "generated_at": iso(gen), "age_h": age, "max_age_h": LIVE_BUILD_MAX_AGE_H}


SPOT_CHECKS: dict[str, Callable[[LiveSite, datetime], dict]] = {
    "live_site_build_age": check_live_build_age,
    "btc_price_vs_coingecko": lambda s, n: check_price_vs_coingecko(s, n, "bitcoin", "btc"),
    "eth_price_vs_coingecko": lambda s, n: check_price_vs_coingecko(s, n, "ethereum", "eth"),
    "fear_greed_vs_alternative_me": check_fear_greed,
    "btc_tip_height_vs_mempool_space": check_btc_tip_height,
    "treasury_10y_vs_treasury_gov": check_treasury_10y,
}


def section_spot_checks(site: LiveSite, now: datetime) -> dict:
    checks = {name: run_section(f"spot:{name}", lambda fn=fn: fn(site, now))
              for name, fn in SPOT_CHECKS.items()}
    return {"status": "ok", "checks": checks,
            "counts": dict(Counter(c["status"] for c in checks.values()))}


# Field preference for the two stamps a payload can carry.
_ASOF_KEYS = ("as_of", "asOf", "data_date", "latest_date", "date")
_BUILT_KEYS = ("generated_at", "generated", "generated_utc", "snapshot_fetched_at",
               "fetched_at", "updated_at", "ts", "tstr")


def payload_stamps(doc: Any, now: datetime) -> dict:
    out: dict[str, Any] = {}
    if not isinstance(doc, dict):
        return out
    for keys, label in ((_ASOF_KEYS, "as_of"), (_BUILT_KEYS, "generated")):
        for k in keys:
            if k in doc:
                dt = parse_ts(doc[k])
                if dt:
                    out[label] = iso(dt)
                    out[f"{label}_field"] = k
                    out[f"{label}_age_h"] = age_hours(dt, now)
                    break
    return out


def section_live_payloads(site: LiveSite, now: datetime) -> dict:
    names = sorted(set(re.findall(r"data-[a-z0-9_\-]+\.json", site.html)))
    payloads = []
    for name in names:
        rec: dict[str, Any] = {"name": name}
        try:
            status, body = http_get(f"{site.base}/{name}", timeout=30)
            rec["http_status"] = status
            rec["bytes"] = len(body)
            if status == 200:
                try:
                    rec.update(payload_stamps(json.loads(body), now))
                    rec["status"] = "ok"
                except json.JSONDecodeError:
                    rec["status"] = "fail"
                    rec["error"] = "not valid JSON"
            else:
                rec["status"] = "fail"
                rec["error"] = f"HTTP {status}"
        except Exception as e:  # noqa: BLE001
            rec["status"] = "error"
            rec["error"] = clip(e, 200)
        payloads.append(rec)
    page = payload_stamps(site.data, now)
    return {"status": "ok", "index": {"name": "index.html DATA", **page}, "payloads": payloads}


# ---------------------------------------------------------------------------

SECTIONS = ("data_health", "workflows", "schedules", "api_status", "spot_checks", "live_payloads")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", help="write JSON here (default: stdout)")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", DEFAULT_REPO))
    ap.add_argument("--base", default=os.environ.get("ALPINE_PAGES_URL", DEFAULT_BASE))
    ap.add_argument("--skip", default="", help="comma-separated sections to skip")
    args = ap.parse_args(argv)
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}

    now = utcnow()
    out: dict[str, Any] = {"kind": "data", "generated_at": iso(now), "repo": args.repo,
                           "base": args.base}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    gh: GitHub | None = None
    gh_error = None
    try:
        gh = GitHub(args.repo, token)
    except Exception as e:  # noqa: BLE001
        gh_error = clip(e, 200)

    if "data_health" not in skip:
        out["data_health"] = run_section("data_health", lambda: section_data_health(REPO_ROOT))

    if not ({"workflows", "schedules"} <= skip):
        if gh is None:
            err = {"status": "error", "error": gh_error or "GitHub client unavailable"}
            out["workflows"], out["schedules"] = dict(err), dict(err)
        else:
            holder: dict[str, Any] = {}

            def _wf() -> dict:
                w, s = section_workflows_and_schedules(gh, REPO_ROOT, now)
                holder["schedules"] = s
                return w

            out["workflows"] = run_section("workflows", _wf)
            out["schedules"] = holder.get("schedules") or {
                "status": "error", "error": out["workflows"].get("error", "not collected")}

    if "api_status" not in skip:
        out["api_status"] = run_section("api_status", lambda: section_api_status(REPO_ROOT, gh, now))

    site = LiveSite(args.base)
    if "spot_checks" not in skip:
        out["spot_checks"] = run_section("spot_checks", lambda: section_spot_checks(site, now))
    if "live_payloads" not in skip:
        out["live_payloads"] = run_section("live_payloads", lambda: section_live_payloads(site, now))

    text = json.dumps(out, indent=2, default=str)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
