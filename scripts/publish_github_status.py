#!/usr/bin/env python3
"""Publish the repo's GitHub status as JSON on the static site.

Writes ``_site/health/github_status.json`` (served at
https://btabiado.github.io/alpine-data/health/github_status.json) from the
GitHub REST API, using the pages.yml run's own ``GITHUB_TOKEN``.

WHY THIS EXISTS
---------------
The daily audit email is written by a scheduled agent whose sandbox can fetch
the live site but cannot reach the GitHub API for this repository. Its
workflow, pull-request, issue, Dependabot and code-scanning sections all came
back "could not check". pages.yml already holds a token and runs more often
than anything else, so it reads the API once per deploy and publishes what it
saw next to /health/, where the agent can fetch it.

WHAT IT RECORDS
---------------
  workflows       every workflow: name, path, cron schedule, the last 5 runs on
                  main (pull-request runs excluded), and derived flags:
                    failed_last_24h  a run among the last 5, created in the
                                     last 24h, concluded failure/timed_out/
                                     startup_failure (cancelled does not count:
                                     pages.yml cancels superseded runs by design)
                    repeat_failure   >= 2 of the last 3 completed runs failed
                    not_running      scheduled, and no run of any kind for more
                                     than 2x the longest gap between its cron
                                     fire times (or the workflow is disabled)
                    recovered        latest completed run succeeded after a
                                     failure in the last 48h
                  For failed runs in the last 48h: the failed jobs, their
                  failed steps and the key check-run annotations (trimmed;
                  Node.js-deprecation and runner-label notices dropped).
  pull_requests   open PRs on btabiado/alpine-data and btabiado/adw-site with
                  author, age and the head commit's CI conclusion.
  issues          open issues on alpine-data (pull requests skipped).
  code_scanning   open alert count by severity.
  dependabot      open alert count by severity.

FAILURE ISOLATION
-----------------
Every section, and every repository inside the PR section, is isolated: an API
error becomes ``"unavailable": "<reason>"`` there and the rest of the file is
still written. A count that could not be read is null, never 0. The script
exits 0 whenever it managed to write the file; pages.yml additionally runs it
continue-on-error, so it can never block a deploy.

API BUDGET
----------
GITHUB_TOKEN allows 1000 requests/hour per repository and pages.yml runs up to
hourly. A hard cap (``--budget``, default 100) stops the client; per-section
caps keep a normal run near 40-60 calls (one per workflow is the bulk).

SECRETS
-------
The token is only ever sent in the Authorization header. Error text is passed
through city.redact before it is stored, and the final JSON is scrubbed of the
literal token values and of anything shaped like a GitHub token.

Usage:
    python scripts/publish_github_status.py --out _site/health/github_status.json
    python scripts/publish_github_status.py --use-gh-cli          # local, via `gh api`
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlencode

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
for _p in (str(HERE), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Shared with the daily audit so both read cron schedules the same way.
import daily_audit_data as dad  # noqa: E402
from city.redact import MASK, redact  # noqa: E402

SCHEMA_VERSION = 1
DEFAULT_REPO = "btabiado/alpine-data"
DEFAULT_PR_REPOS = ("btabiado/alpine-data", "btabiado/adw-site")
DEFAULT_OUT = "_site/health/github_status.json"
API_ROOT = "https://api.github.com"
UA = "alpine-data-github-status/1.0 (+https://github.com/btabiado/alpine-data)"
HTTP_TIMEOUT_S = 20

DEFAULT_BUDGET = 100
RUNS_PER_WORKFLOW = 5
# Runs listed per workflow before keeping the newest RUNS_PER_WORKFLOW on the
# branch. The API's own ``branch=`` filter is not used: without a ``created``
# range it intermittently answers from a stale index (2026-10-05:
# aviation-opensky "last run" 2026-09-11 although it ran that evening, and
# total_count 299 / 820 / 1194 on three identical calls). Unfiltered listings
# are consistent; PR runs are dropped client-side, and 20 leaves room for
# them on tests.yml / codeql.yml.
RUNS_FETCH = 20
PR_EVENTS = frozenset({"pull_request", "pull_request_target", "pull_request_review",
                       "pull_request_review_comment", "merge_group"})
FAILED_WINDOW = timedelta(hours=24)
ANNOTATION_WINDOW = timedelta(hours=48)
MAX_FAILED_RUNS_INSPECTED = 8        # across all workflows, newest first
MAX_FAILED_RUNS_PER_WORKFLOW = 2
MAX_FAILED_JOBS_PER_RUN = 2
MAX_ANNOTATIONS_PER_JOB = 5
MAX_PRS_PER_REPO = 50
MAX_PR_CI_LOOKUPS = 12               # per repository
MAX_ISSUE_PAGES = 3
MAX_ALERT_PAGES = 3
CRON_LOOKBACK = timedelta(days=62)   # long enough to see two monthly fires

FAILED = dad.FAILED_CONCLUSIONS
CI_FAILED = FAILED | {"action_required"}
CI_OK = frozenset({"success", "neutral", "skipped"})

# Optional per-repository tokens, read from the environment. The workflow's
# GITHUB_TOKEN is scoped to alpine-data, so a private sibling repository needs
# its own read-only token or its section is recorded as unavailable.
REPO_TOKEN_ENVS = {"btabiado/adw-site": "ADW_SITE_TOKEN"}

clip = dad.clip
iso = dad.iso
parse_ts = dad.parse_ts


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# GitHub REST client
# ---------------------------------------------------------------------------

class ApiError(Exception):
    """A GitHub API call that did not return 200. ``status`` is None for
    network errors and for the client's own budget stop."""

    def __init__(self, status: int | None, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(f"HTTP {status}: {message}" if status else message)


class GitHubClient:
    """Minimal GET-only REST client with a hard call budget.

    Three modes: a token (Authorization header via requests), the ``gh api``
    CLI (local runs), or anonymous (public endpoints only, 60 req/h).
    """

    def __init__(self, token: str | None = None, *, use_cli: bool = False,
                 budget: int = DEFAULT_BUDGET, session: Any = None,
                 timeout: float = HTTP_TIMEOUT_S, sleep: Callable[[float], None] | None = None) -> None:
        self.token = token or None
        self.use_cli = use_cli
        self.budget = budget
        self.calls = 0
        self.timeout = timeout
        self._sleep = sleep or time.sleep
        self._session = session
        self.secrets: list[str] = [t for t in (self.token,) if t]

    @property
    def auth_mode(self) -> str:
        return "gh-cli" if self.use_cli else ("token" if self.token else "anonymous")

    def _spend(self) -> None:
        if self.calls >= self.budget:
            raise ApiError(None, f"API call budget ({self.budget}) exhausted before this request")
        self.calls += 1

    def _clean(self, text: Any, n: int = 200) -> str:
        return clip(redact(text, extra=self.secrets), n)

    def get(self, path: str, params: dict | None = None, *, token: str | None = None) -> Any:
        if token and token not in self.secrets:
            self.secrets.append(token)
        rel = path if path.startswith("/") else "/" + path
        if params:
            rel += ("&" if "?" in rel else "?") + urlencode(params)
        if self.use_cli and not token:
            return self._get_cli(rel)
        return self._get_http(rel, token or self.token)

    def _get_cli(self, rel: str) -> Any:
        self._spend()
        try:
            proc = subprocess.run(["gh", "api", rel.lstrip("/")], capture_output=True,
                                  text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            raise ApiError(None, self._clean(f"gh api failed: {type(e).__name__}: {e}"))
        if proc.returncode != 0:
            m = re.search(r"\(HTTP (\d{3})\)", proc.stderr or "")
            msg = ""
            try:
                msg = (json.loads(proc.stdout or "{}") or {}).get("message") or ""
            except (ValueError, AttributeError):
                msg = ""
            raise ApiError(int(m.group(1)) if m else None,
                           self._clean(msg or proc.stderr or "gh api failed"))
        try:
            return json.loads(proc.stdout or "null")
        except ValueError as e:
            raise ApiError(None, self._clean(f"unparseable gh api output: {e}"))

    def _get_http(self, rel: str, token: str | None) -> Any:
        if self._session is None:
            import requests  # in requirements.txt; imported lazily so tests can inject a session
            self._session = requests.Session()
        headers = {"Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": UA}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        url = API_ROOT + rel
        resp = None
        for attempt in range(2):
            self._spend()
            try:
                resp = self._session.get(url, headers=headers, timeout=self.timeout)
            except Exception as e:  # noqa: BLE001 - requests' exception tree, or a test double
                if attempt == 1:
                    raise ApiError(None, self._clean(f"network error: {type(e).__name__}: {e}"))
                self._sleep(2)
                continue
            if resp.status_code >= 500 and attempt == 0:
                self._sleep(2)
                continue
            break
        if resp is None:
            raise ApiError(None, "no response")
        if resp.status_code != 200:
            raise ApiError(resp.status_code, self._error_text(resp))
        try:
            return resp.json()
        except ValueError as e:
            raise ApiError(None, self._clean(f"unparseable JSON from {rel.split('?')[0]}: {e}"))

    def _error_text(self, resp: Any) -> str:
        headers = getattr(resp, "headers", None) or {}
        if resp.status_code in (403, 429) and str(headers.get("X-RateLimit-Remaining", "")) == "0":
            return "rate limit exhausted"
        msg = ""
        try:
            body = resp.json()
            if isinstance(body, dict):
                msg = body.get("message") or ""
        except Exception:  # noqa: BLE001
            msg = ""
        return self._clean(msg or getattr(resp, "text", "") or "no message")

    def paginate(self, path: str, params: dict | None = None, *, max_pages: int = 3,
                 per_page: int = 100, key: str | None = None,
                 token: str | None = None) -> tuple[list, bool]:
        """Items across pages, and whether ``max_pages`` cut the list short."""
        out: list = []
        for page in range(1, max_pages + 1):
            data = self.get(path, {**(params or {}), "per_page": per_page, "page": page}, token=token)
            items = (data or {}).get(key, []) if key else (data or [])
            if not isinstance(items, list):
                raise ApiError(None, f"unexpected payload from {path}: {type(items).__name__}")
            out.extend(items)
            if len(items) < per_page:
                return out, False
        return out, True


def reason(e: BaseException) -> str:
    """One-line, secret-free reason for an ``unavailable`` field."""
    if isinstance(e, ApiError):
        return str(e)
    return clip(redact(f"{type(e).__name__}: {e}"), 240)


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------

def is_failed(run: dict) -> bool:
    return run.get("status") == "completed" and run.get("conclusion") in FAILED


def _created(run: dict) -> datetime | None:
    return parse_ts(run.get("created_at"))


def cron_interval_hours(crons: Iterable[str], now: datetime) -> float | None:
    """Longest gap between consecutive fire times of ``crons`` (merged).

    "Every weekday at 13:30" is a 72 h gap over the weekend, not 24 h, so a
    workflow is only called not-running after it has missed more than one
    legitimate gap. None when the schedule fires fewer than twice in the
    lookback window or cannot be parsed.
    """
    fires: set[datetime] = set()
    for c in crons:
        fires.update(dad.cron_fire_times(c, now - CRON_LOOKBACK, now + timedelta(days=1)))
    ordered = sorted(fires)
    if len(ordered) < 2:
        return None
    gap = max(b - a for a, b in zip(ordered, ordered[1:]))
    return round(gap.total_seconds() / 3600.0, 2)


def derive_flags(runs: list[dict], now: datetime, *, interval_h: float | None,
                 state: str | None = "active", workflow_created_at: Any = None) -> dict:
    """The four derived flags for one workflow from its newest-first runs."""
    runs = sorted(runs, key=lambda r: r.get("created_at") or "", reverse=True)
    completed = [r for r in runs if r.get("status") == "completed"]
    failed_24h = any(is_failed(r) and (_created(r) or now - 2 * FAILED_WINDOW) >= now - FAILED_WINDOW
                     for r in completed)
    repeat = sum(1 for r in completed[:3] if is_failed(r)) >= 2
    recovered = bool(completed) and completed[0].get("conclusion") == "success" and any(
        is_failed(r) and (_created(r) or now - 2 * ANNOTATION_WINDOW) >= now - ANNOTATION_WINDOW
        for r in completed[1:])

    not_running: bool | None = None
    if interval_h is not None:
        if state and str(state).startswith("disabled"):
            not_running = True
        else:
            threshold = timedelta(hours=2 * interval_h)
            last = max((t for t in (_created(r) for r in runs) if t), default=None)
            if last is not None:
                not_running = now - last > threshold
            else:
                born = parse_ts(workflow_created_at)
                # Never ran: only a problem once it has existed for a full threshold.
                not_running = born is None or now - born > threshold
    return {"failed_last_24h": failed_24h, "repeat_failure": repeat,
            "not_running": not_running, "recovered": recovered}


_NODE_RE = re.compile(r"\bnode(?:\.?js)?\s*\d{2}\b", re.I)
_NODE_WORDS = re.compile(r"deprecat|forced to run|will run on|no longer supported|end[- ]of[- ]life"
                         r"|\bEOL\b|upgrade|runs? on node", re.I)
_RUNNER_RE = re.compile(r"\b(?:ubuntu|windows|macos)-(?:latest|\d+(?:\.\d+)?)\b", re.I)
_RUNNER_WORDS = re.compile(r"\blabel\b|migrat|redirect|\bimage\b|brownout|deprecat|retir"
                           r"|will (?:use|be|begin|start|point)|is changing|rolling out|rolled out", re.I)
_GENERIC_EXIT = re.compile(r"^Process completed with exit code \d+\.?$")


def is_platform_notice(text: str) -> bool:
    """GitHub's own Node.js-runtime deprecation and runner-label migration
    notices: they decorate nearly every run and say nothing about why it
    failed."""
    if _NODE_RE.search(text) and _NODE_WORDS.search(text):
        return True
    return bool(_RUNNER_RE.search(text) and _RUNNER_WORDS.search(text))


def select_annotations(raw: Any, limit: int = MAX_ANNOTATIONS_PER_JOB) -> list[dict]:
    """The key failure/warning annotations of one job, trimmed and de-duplicated.

    Platform notices are dropped unless they are failure-level (a failure that
    mentions a runner label, e.g. "no runner matching ubuntu-26.04", is real).
    Specific failures sort first, then warnings, then the generic
    "Process completed with exit code N." line (the failed step names already
    say where it broke).
    """
    out: list[dict] = []
    seen: set[tuple] = set()
    for a in raw if isinstance(raw, list) else []:
        if not isinstance(a, dict):
            continue
        level = a.get("annotation_level")
        if level not in ("failure", "warning"):
            continue
        title = clip(redact(a.get("title") or ""), 120)
        message = clip(redact(a.get("message") or ""), 300)
        if level != "failure" and is_platform_notice(f"{title} {message}"):
            continue
        key = (level, title, message)
        if key in seen:
            continue
        seen.add(key)
        rec = {"level": level, "title": title, "message": message}
        path = a.get("path")
        if path and path != ".github":
            rec["path"] = clip(path, 160)
        out.append(rec)

    def rank(r: dict) -> int:
        if r["level"] == "failure" and not _GENERIC_EXIT.match(r["message"]):
            return 0
        return 1 if r["level"] == "warning" else 2

    out.sort(key=rank)
    return out[:limit]


def summarize_checks(check_runs: list[dict]) -> dict:
    """One CI verdict for a commit from its check runs."""
    if not check_runs:
        return {"conclusion": "none", "total": 0}
    failing = [c.get("name") for c in check_runs if c.get("conclusion") in CI_FAILED]
    pending = [c.get("name") for c in check_runs if c.get("status") != "completed"]
    if failing:
        verdict = "failure"
    elif pending:
        verdict = "pending"
    elif all(c.get("conclusion") in CI_OK for c in check_runs):
        verdict = "success"
    else:
        verdict = next((c.get("conclusion") for c in check_runs
                        if c.get("conclusion") not in CI_OK), None) or "unknown"
    out: dict[str, Any] = {"conclusion": verdict, "total": len(check_runs)}
    if failing:
        out["failing"] = [clip(n, 80) for n in failing[:5]]
    if pending:
        out["pending"] = len(pending)
    return out


def age_days(ts: Any, now: datetime) -> float | None:
    t = parse_ts(ts)
    return None if t is None else round((now - t).total_seconds() / 86400.0, 1)


def _run_record(r: dict) -> dict:
    return {"id": r.get("id"), "event": r.get("event"), "status": r.get("status"),
            "conclusion": r.get("conclusion"), "created_at": r.get("created_at"),
            "updated_at": r.get("updated_at"), "html_url": r.get("html_url")}


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def run_section(name: str, fn: Callable[[], dict]) -> dict:
    """Run one section; any exception becomes that section's ``unavailable``."""
    t0 = time.monotonic()
    try:
        out = fn()
    except Exception as e:  # noqa: BLE001 - isolation is the point
        out = {"status": "unavailable", "unavailable": reason(e)}
    out.setdefault("status", "ok")
    print(f"[github-status] {name}: {out['status']} ({time.monotonic() - t0:.1f}s)", file=sys.stderr)
    return out


def _failed_run_detail(client: GitHubClient, repo: str, run: dict) -> dict:
    rec: dict[str, Any] = {"run_id": run.get("id"), "html_url": run.get("html_url"),
                           "event": run.get("event"), "conclusion": run.get("conclusion"),
                           "created_at": run.get("created_at")}
    try:
        data = client.get(f"/repos/{repo}/actions/runs/{run.get('id')}/jobs",
                          {"filter": "latest", "per_page": 30})
    except Exception as e:  # noqa: BLE001
        rec["unavailable"] = reason(e)
        return rec
    jobs: list[dict] = []
    failed_jobs = [j for j in (data or {}).get("jobs") or [] if j.get("conclusion") in FAILED]
    for job in failed_jobs[:MAX_FAILED_JOBS_PER_RUN]:
        j: dict[str, Any] = {
            "name": clip(job.get("name"), 120), "html_url": job.get("html_url"),
            "failed_steps": [clip(s.get("name"), 120) for s in job.get("steps") or []
                             if s.get("conclusion") in FAILED][:5],
        }
        try:
            raw = client.get(f"/repos/{repo}/check-runs/{job.get('id')}/annotations", {"per_page": 50})
            j["annotations"] = select_annotations(raw)
        except Exception as e:  # noqa: BLE001
            j["annotations_unavailable"] = reason(e)
        jobs.append(j)
    rec["jobs"] = jobs
    if len(failed_jobs) > MAX_FAILED_JOBS_PER_RUN:
        rec["more_failed_jobs"] = len(failed_jobs) - MAX_FAILED_JOBS_PER_RUN
    if not failed_jobs and run.get("conclusion") == "startup_failure":
        rec["note"] = "startup_failure: no job ran (invalid workflow file, missing runner label, ...)"
    return rec


def section_workflows(client: GitHubClient, repo: str, crons_by_path: dict[str, list[str]],
                      now: datetime, branch: str = "main") -> dict:
    data = client.get(f"/repos/{repo}/actions/workflows", {"per_page": 100})
    wfs = sorted((data or {}).get("workflows") or [], key=lambda w: (w.get("name") or "").lower())
    items: list[dict] = []
    failed_candidates: list[tuple[str, dict, dict]] = []   # (created_at, run, item)
    unavailable = 0
    for wf in wfs:
        path = wf.get("path") or ""
        crons = crons_by_path.get(path, [])
        item: dict[str, Any] = {"name": wf.get("name"), "path": path, "state": wf.get("state"),
                                "html_url": wf.get("html_url"), "schedule": crons}
        interval = None
        if crons:
            try:
                interval = cron_interval_hours(crons, now)
            except (ValueError, IndexError) as e:
                item["schedule_error"] = clip(e, 160)
        item["cron_interval_h"] = interval
        try:
            rdata = client.get(f"/repos/{repo}/actions/workflows/{wf.get('id')}/runs",
                               {"per_page": RUNS_FETCH, "exclude_pull_requests": "true"})
        except Exception as e:  # noqa: BLE001
            item["unavailable"] = reason(e)
            item["flags"] = None
            unavailable += 1
            items.append(item)
            continue
        runs = sorted((r for r in (rdata or {}).get("workflow_runs") or []
                       if r.get("head_branch") == branch and r.get("event") not in PR_EVENTS),
                      key=lambda r: r.get("created_at") or "", reverse=True)[:RUNS_PER_WORKFLOW]
        item["runs"] = [_run_record(r) for r in runs]
        last = max((t for t in (_created(r) for r in runs) if t), default=None)
        item["last_run_at"] = iso(last)
        item["hours_since_last_run"] = (round((now - last).total_seconds() / 3600.0, 1)
                                        if last else None)
        item["flags"] = derive_flags(runs, now, interval_h=interval, state=wf.get("state"),
                                     workflow_created_at=wf.get("created_at"))
        recent_failed = [r for r in runs if is_failed(r)
                         and (_created(r) or now - 2 * ANNOTATION_WINDOW) >= now - ANNOTATION_WINDOW]
        for r in recent_failed[:MAX_FAILED_RUNS_PER_WORKFLOW]:
            failed_candidates.append((r.get("created_at") or "", r, item))
        if len(recent_failed) > MAX_FAILED_RUNS_PER_WORKFLOW:
            item["failed_runs_not_inspected"] = len(recent_failed) - MAX_FAILED_RUNS_PER_WORKFLOW
        items.append(item)

    # Annotations: newest failures first, across workflows, under a global cap.
    failed_candidates.sort(key=lambda t: t[0], reverse=True)
    for i, (_, run, item) in enumerate(failed_candidates):
        item.setdefault("failed_runs", [])
        if i >= MAX_FAILED_RUNS_INSPECTED:
            item["failed_runs"].append({"run_id": run.get("id"), "html_url": run.get("html_url"),
                                        "created_at": run.get("created_at"),
                                        "conclusion": run.get("conclusion"),
                                        "unavailable": "not inspected (per-run cap on failed-run lookups)"})
            continue
        item["failed_runs"].append(_failed_run_detail(client, repo, run))

    def names(flag: str) -> list[str]:
        return [w["name"] for w in items if (w.get("flags") or {}).get(flag)]

    status = "ok" if not unavailable else ("unavailable" if unavailable == len(items) else "partial")
    out: dict[str, Any] = {
        "status": status, "branch": branch, "count": len(items),
        "failed_last_24h": names("failed_last_24h"), "repeat_failure": names("repeat_failure"),
        "not_running": names("not_running"), "recovered": names("recovered"),
        "items_unavailable": [w["name"] for w in items if "unavailable" in w],
        "items": items,
    }
    if status == "unavailable":
        out["unavailable"] = items[0].get("unavailable") if items else "no workflows listed"
    return out


def section_issues(client: GitHubClient, repo: str, now: datetime) -> dict:
    raw, truncated = client.paginate(f"/repos/{repo}/issues",
                                     {"state": "open", "sort": "created", "direction": "desc"},
                                     max_pages=MAX_ISSUE_PAGES)
    items = []
    for i in raw:
        if "pull_request" in i:      # the issues endpoint lists PRs too
            continue
        items.append({"number": i.get("number"), "title": clip(i.get("title"), 200),
                      "labels": [lb.get("name") for lb in i.get("labels") or [] if isinstance(lb, dict)],
                      "author": (i.get("user") or {}).get("login"),
                      "created_at": i.get("created_at"), "age_days": age_days(i.get("created_at"), now),
                      "html_url": i.get("html_url")})
    out: dict[str, Any] = {"status": "ok", "repo": repo, "open": len(items), "items": items}
    if truncated:
        out["truncated"] = True
    return out


def _pr_unavailable_reason(e: BaseException, repo: str, home_repo: str) -> str:
    msg = reason(e)
    if isinstance(e, ApiError) and e.status in (401, 403, 404) and repo != home_repo:
        env = REPO_TOKEN_ENVS.get(repo)
        hint = f"set the {env} secret (read-only token for {repo})" if env else "needs a token for this repo"
        msg += (f" -- the workflow token is scoped to {home_repo}; {repo} is private or "
                f"not readable with it; {hint}")
    return msg


def section_pull_requests(client: GitHubClient, repos: Iterable[str], now: datetime, *,
                          home_repo: str, tokens: dict[str, str] | None = None) -> dict:
    tokens = tokens or {}
    out_repos: list[dict] = []
    for repo in repos:
        tok = tokens.get(repo) or None
        try:
            prs = client.get(f"/repos/{repo}/pulls",
                             {"state": "open", "per_page": MAX_PRS_PER_REPO}, token=tok)
        except Exception as e:  # noqa: BLE001
            out_repos.append({"repo": repo, "open": None,
                              "unavailable": _pr_unavailable_reason(e, repo, home_repo)})
            continue
        items: list[dict] = []
        for n, pr in enumerate(prs or []):
            head = pr.get("head") or {}
            item: dict[str, Any] = {
                "number": pr.get("number"), "title": clip(pr.get("title"), 200),
                "author": (pr.get("user") or {}).get("login"), "draft": bool(pr.get("draft")),
                "created_at": pr.get("created_at"), "age_days": age_days(pr.get("created_at"), now),
                "html_url": pr.get("html_url"), "head_sha": (head.get("sha") or "")[:12] or None,
            }
            if n >= MAX_PR_CI_LOOKUPS:
                item["head_ci"] = {"conclusion": None,
                                   "unavailable": "not checked (per-repo cap on CI lookups)"}
            elif not head.get("sha"):
                item["head_ci"] = {"conclusion": None, "unavailable": "no head sha"}
            else:
                try:
                    cr = client.get(f"/repos/{repo}/commits/{head['sha']}/check-runs",
                                    {"per_page": 100}, token=tok)
                    item["head_ci"] = summarize_checks((cr or {}).get("check_runs") or [])
                except Exception as e:  # noqa: BLE001
                    item["head_ci"] = {"conclusion": None, "unavailable": reason(e)}
            items.append(item)
        rec: dict[str, Any] = {"repo": repo, "open": len(items), "items": items}
        if len(items) >= MAX_PRS_PER_REPO:
            rec["truncated"] = True
        out_repos.append(rec)
    ok = sum(1 for r in out_repos if "unavailable" not in r)
    status = "ok" if ok == len(out_repos) else ("partial" if ok else "unavailable")
    out: dict[str, Any] = {"status": status, "repos": out_repos}
    if status == "unavailable":
        out["unavailable"] = "; ".join(f"{r['repo']}: {r['unavailable']}" for r in out_repos)
    return out


def _security_reason(e: BaseException, kind: str) -> str:
    msg = reason(e)
    if isinstance(e, ApiError) and e.status in (401, 403):
        if kind == "dependabot":
            msg += (" -- the Actions GITHUB_TOKEN cannot read Dependabot alerts whatever the "
                    "permissions block says (documented limit); needs a token with "
                    "'Dependabot alerts: read'")
        else:
            msg += " -- needs `security-events: read` on the workflow token"
    elif isinstance(e, ApiError) and e.status == 404 and kind == "code_scanning":
        msg += " -- code scanning not enabled or no analysis uploaded yet"
    return msg


def _count_alerts(client: GitHubClient, path: str, severity: Callable[[dict], str]) -> dict:
    alerts, truncated = client.paginate(path, {"state": "open"}, max_pages=MAX_ALERT_PAGES)
    by = Counter(severity(a) for a in alerts if isinstance(a, dict))
    out: dict[str, Any] = {"status": "ok", "open": len(alerts),
                           "by_severity": dict(sorted(by.items()))}
    if truncated:
        out["truncated"] = True
    return out


def code_scanning_severity(a: dict) -> str:
    rule = a.get("rule") or {}
    return str(rule.get("security_severity_level") or rule.get("severity") or "unknown")


def dependabot_severity(a: dict) -> str:
    adv = a.get("security_advisory") or {}
    vuln = a.get("security_vulnerability") or {}
    return str(adv.get("severity") or vuln.get("severity") or "unknown")


def section_code_scanning(client: GitHubClient, repo: str) -> dict:
    try:
        return _count_alerts(client, f"/repos/{repo}/code-scanning/alerts", code_scanning_severity)
    except Exception as e:  # noqa: BLE001
        return {"status": "unavailable", "open": None, "unavailable": _security_reason(e, "code_scanning")}


def section_dependabot(client: GitHubClient, repo: str) -> dict:
    try:
        return _count_alerts(client, f"/repos/{repo}/dependabot/alerts", dependabot_severity)
    except Exception as e:  # noqa: BLE001
        return {"status": "unavailable", "open": None, "unavailable": _security_reason(e, "dependabot")}


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

FIELD_NOTES = {
    "flags.failed_last_24h": "a run among the last 5 on main, created in the last 24h, concluded "
                             "failure/timed_out/startup_failure (cancelled is not a failure)",
    "flags.repeat_failure": ">= 2 of the last 3 completed runs failed (can coexist with recovered)",
    "flags.not_running": "scheduled workflow with no run of any kind for more than 2 x cron_interval_h "
                         "(the longest gap between its cron fire times), or disabled; null = not scheduled",
    "flags.recovered": "latest completed run succeeded after a failure in the last 48h",
    "failed_runs": "failed runs from the last 48h: failed jobs, failed steps and key annotations "
                   "(Node.js-deprecation and runner-label notices dropped)",
    "unavailable": "this section or item could not be read; counts there are null, never 0",
}


def build_status(client: GitHubClient, *, repo: str = DEFAULT_REPO,
                 pr_repos: Iterable[str] = DEFAULT_PR_REPOS,
                 workflows_dir: Path | None = None, now: datetime | None = None,
                 branch: str = "main", repo_tokens: dict[str, str] | None = None) -> dict:
    now = now or utcnow()
    workflows_dir = workflows_dir or (REPO_ROOT / ".github" / "workflows")
    try:
        crons_by_path = dad.workflow_crons(workflows_dir)
    except Exception as e:  # noqa: BLE001
        print(f"[github-status] cron schedules unreadable: {reason(e)}", file=sys.stderr)
        crons_by_path = {}

    doc: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "generated_at": iso(now),
        "repo": repo,
        "auth": client.auth_mode,
    }
    run_id, sha = os.environ.get("GITHUB_RUN_ID"), os.environ.get("GITHUB_SHA")
    if run_id:
        doc["generated_by"] = {
            "workflow": os.environ.get("GITHUB_WORKFLOW") or None,
            "run_url": f"https://github.com/{repo}/actions/runs/{run_id}",
            "sha": (sha or "")[:12] or None,
        }
    # Order matters only under budget pressure: the cheap single-call sections
    # run before the per-PR CI lookups, which are the first thing to give way.
    doc["workflows"] = run_section("workflows", lambda: section_workflows(
        client, repo, crons_by_path, now, branch))
    doc["issues"] = run_section("issues", lambda: section_issues(client, repo, now))
    doc["code_scanning"] = run_section("code_scanning", lambda: section_code_scanning(client, repo))
    doc["dependabot"] = run_section("dependabot", lambda: section_dependabot(client, repo))
    doc["pull_requests"] = run_section("pull_requests", lambda: section_pull_requests(
        client, pr_repos, now, home_repo=repo, tokens=repo_tokens))

    wf = doc["workflows"]
    prs = {r["repo"]: r.get("open") for r in doc["pull_requests"].get("repos") or []}
    for r in pr_repos:
        prs.setdefault(r, None)
    doc["summary"] = {
        "sections": {k: doc[k].get("status") for k in
                     ("workflows", "issues", "code_scanning", "dependabot", "pull_requests")},
        "workflows": wf.get("count"),
        "failed_last_24h": wf.get("failed_last_24h"),
        "repeat_failure": wf.get("repeat_failure"),
        "not_running": wf.get("not_running"),
        "recovered": wf.get("recovered"),
        "open_prs": prs,
        "open_issues": doc["issues"].get("open"),
        "code_scanning_open": doc["code_scanning"].get("open"),
        "dependabot_open": doc["dependabot"].get("open"),
    }
    doc["api_calls"] = client.calls
    doc["api_budget"] = client.budget
    doc["field_notes"] = FIELD_NOTES
    # Summary first when read top-down.
    order = ["schema", "generated_at", "repo", "auth", "generated_by", "summary", "workflows",
             "pull_requests", "issues", "code_scanning", "dependabot", "api_calls", "api_budget",
             "field_notes"]
    return {k: doc[k] for k in order if k in doc}


_TOKEN_SHAPES = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")


def scrub(text: str, secrets: Iterable[str]) -> str:
    """Last line of defence on the serialized file: literal token values and
    anything shaped like a GitHub token become the mask."""
    for s in sorted({s for s in secrets if s and len(s) >= 8}, key=len, reverse=True):
        text = text.replace(s, MASK)
    return _TOKEN_SHAPES.sub(MASK, text)


def write_json(path: Path, doc: dict, secrets: Iterable[str]) -> None:
    text = scrub(json.dumps(doc, indent=2, ensure_ascii=False, default=str), secrets) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def make_client(args: argparse.Namespace) -> GitHubClient:
    token = None
    for env in args.token_env:
        token = (os.environ.get(env) or "").strip() or None
        if token:
            break
    use_cli = bool(args.use_gh_cli) or (token is None and shutil.which("gh") is not None)
    return GitHubClient(None if use_cli else token, use_cli=use_cli, budget=args.budget)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=DEFAULT_OUT, help=f"output path (default {DEFAULT_OUT}; '-' = stdout)")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY") or DEFAULT_REPO)
    ap.add_argument("--pr-repos", default=",".join(DEFAULT_PR_REPOS),
                    help="comma-separated repositories whose open PRs are listed")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--budget", type=int, default=DEFAULT_BUDGET, help="hard cap on API calls")
    ap.add_argument("--use-gh-cli", action="store_true", help="read through `gh api` (local runs)")
    ap.add_argument("--token-env", nargs="*", default=["GITHUB_TOKEN", "GH_TOKEN"],
                    help="environment variables tried, in order, for the API token")
    args = ap.parse_args(argv)

    repo_tokens = {r: (os.environ.get(env) or "").strip()
                   for r, env in REPO_TOKEN_ENVS.items() if (os.environ.get(env) or "").strip()}
    secrets = [v for v in (os.environ.get(e) for e in [*args.token_env, *REPO_TOKEN_ENVS.values()]) if v]
    secrets += list(repo_tokens.values())

    client = make_client(args)
    try:
        doc = build_status(client, repo=args.repo, branch=args.branch, repo_tokens=repo_tokens,
                           pr_repos=[r.strip() for r in args.pr_repos.split(",") if r.strip()])
    except Exception as e:  # noqa: BLE001 - every section is isolated; this is the backstop
        doc = {"schema": SCHEMA_VERSION, "generated_at": iso(utcnow()), "repo": args.repo,
               "unavailable": reason(e)}
    secrets += client.secrets

    if args.out == "-":
        sys.stdout.write(scrub(json.dumps(doc, indent=2, ensure_ascii=False, default=str), secrets) + "\n")
    else:
        write_json(Path(args.out), doc, secrets)
    s = doc.get("summary") or {}
    print(f"[github-status] wrote {args.out}: {client.calls}/{client.budget} API calls; "
          f"sections={s.get('sections')}; failed_24h={s.get('failed_last_24h')}; "
          f"not_running={s.get('not_running')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
