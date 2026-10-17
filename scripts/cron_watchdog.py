#!/usr/bin/env python3
"""Catch up scheduled workflows that GitHub dropped or started very late.

The daily audit for 2026-10-17 (TOP ISSUES #3) measured three hourly crons
landing about 20% of their ticks, and most daily crons starting 4–10 hours
after the minute they name. GitHub documents the schedule event as
best-effort and names the start of every hour as the congested window.

This script is the in-repo safeguard. It does not add a secret, a PAT, or an
external scheduler. A scheduled workflow calls it with the built-in
``GITHUB_TOKEN`` (``actions: write``). GitHub's rule is that events fired by
``GITHUB_TOKEN`` do not start new workflow runs, except ``workflow_dispatch``
and ``repository_dispatch``, which always do. So a POST to
``/actions/workflows/{file}/dispatches`` is the supported catch-up.

What it will not do:

* Dispatch while a run is queued or in progress (the target's concurrency
  group is the second line of defense, not the only one).
* Dispatch again for a fire that already produced a completed attempt other
  than a cancel. Failures are not retried inside the same window; the audit
  already flags those.
* Treat a cancelled run as "done". ``pages`` cancels superseded deploys, and
  a cancel-only hour is the dropped-tick symptom.
* Dispatch more than ``MAX_DISPATCH`` workflows per run. Hourly jobs go
  first (they are the ones losing ticks), then ``pages`` (so a deploy is not
  crowded out by data jobs), then the stalest dailies.
* Run at all when ``CRON_WATCHDOG_DISABLED`` is true. ``--dry-run`` prints
  the same decisions and does not POST.

Thresholds (UTC):

* Hourly: the latest fire that is at least 3 hours old was not served.
* Daily: same rule. A job scheduled at 07:29 is not eligible until 10:29,
  which is "not run today by a cutoff" without inventing a second clock.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import daily_audit_data as dad  # noqa: E402

# 3h is the conservative end of "hourly jobs older than ~2–3h". The same
# grace is the daily cutoff (scheduled time + 3h). A run created up to 15
# minutes before the nominal fire still counts, matching the daily audit.
HOURLY_GRACE = timedelta(hours=3)
DAILY_GRACE = timedelta(hours=3)
SLACK = timedelta(minutes=15)
MAX_DISPATCH = 5
LOOKBACK = timedelta(days=4)
RUNS_PER_WORKFLOW = 40

ACTIVE_STATUSES = frozenset({
    "queued", "in_progress", "waiting", "pending", "requested", "action_required",
})
# A cancel did not finish the work. `skipped` / `stale` did not either.
NON_COVERING = frozenset({"cancelled", "skipped", "stale"})

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
UA = "alpine-data-cron-watchdog/1.0 (+https://github.com/btabiado/alpine-data)"
HTTP_TIMEOUT_S = 30

# filename -> (kind, crons). Crons are checked against the workflow files by
# tests, so a schedule edit cannot drift from the threshold that watches it.
# Manual-only workflows (r2-backfill, history-backfill) and security scans
# are intentionally absent: catch-up is for data freshness.
WATCHES: dict[str, tuple[str, tuple[str, ...]]] = {
    # Hourly. These three are the dropped-tick group in the 2026-10-17 audit.
    "aviation-opensky.yml": ("hourly", ("25 * * * *",)),
    "lthcs-news-hourly.yml": ("hourly", ("19 * * * *",)),
    "pages.yml": ("hourly", ("7 * * * *",)),
    # Daily data refresh, plus the two jobs that judge freshness.
    "aviation-tsa.yml": ("daily", ("10 14 * * *",)),
    "cfpb-daily.yml": ("daily", ("17 7 * * *",)),
    "city-daily.yml": ("daily", ("29 7 * * *",)),
    "crypto-odds-daily.yml": ("daily", ("15 14 * * *",)),
    "daily-audit.yml": ("daily", ("17 9 * * *",)),
    "data-health.yml": ("daily", ("11 15 * * *",)),
    "etf-flows-daily.yml": ("daily", ("15 9 * * *",)),
    "lthcs-backtest-daily.yml": ("daily", ("30 23 * * *",)),
    "lthcs-crypto-daily.yml": ("daily", ("47 11 * * *",)),
    "lthcs-daily.yml": ("daily", ("11 23 * * *",)),
    "lthcs-trends-daily.yml": ("daily", ("13 4 * * *",)),
    "money-flow-daily.yml": ("daily", ("30 8 * * *",)),
    "real-estate-daily.yml": ("daily", ("13 6 * * *",)),
    "usaspending-daily.yml": ("daily", ("47 7 * * *",)),
}


@dataclass
class Decision:
    workflow: str
    kind: str
    action: str  # dispatch | skip | error
    reason: str
    overdue_hours: float = 0.0
    due_fire: datetime | None = None
    last_success: datetime | None = None
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime | None) -> str:
    if dt is None:
        return "never"
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%MZ")


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def grace_for(kind: str) -> timedelta:
    if kind == "hourly":
        return HOURLY_GRACE
    if kind == "daily":
        return DAILY_GRACE
    raise ValueError(f"unknown watch kind {kind!r}")


def fire_times(crons: tuple[str, ...] | list[str], now: datetime) -> list[datetime]:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    out: list[datetime] = []
    start = now - LOOKBACK
    end = now + timedelta(minutes=1)
    for expr in crons:
        out.extend(dad.cron_fire_times(expr, start, end))
    return sorted(set(out))


def _covers(run: dict, window_start: datetime) -> bool:
    """True when this run already served the fire that opened at window_start."""
    if run.get("conclusion") in NON_COVERING:
        return False
    status = run.get("status")
    created = parse_ts(run.get("created_at"))
    updated = parse_ts(run.get("updated_at"))
    if status in ACTIVE_STATUSES:
        return True
    if run.get("conclusion") == "success" and updated is not None and updated >= window_start:
        return True
    if created is not None and created >= window_start:
        return True
    return False


def _last_success(runs: list[dict]) -> datetime | None:
    stamps = [
        ts for r in runs if r.get("conclusion") == "success"
        for ts in (parse_ts(r.get("updated_at")) or parse_ts(r.get("created_at")),)
        if ts is not None
    ]
    return max(stamps) if stamps else None


def classify(workflow: str, kind: str, crons: tuple[str, ...] | list[str],
             runs: list[dict], now: datetime) -> Decision:
    """Decide whether one workflow is overdue. Pure: no I/O."""
    fires = fire_times(crons, now)
    elapsed = [t for t in fires if t <= now]
    if not elapsed:
        return Decision(workflow, kind, "skip", "no fire at or before now")
    grace = grace_for(kind)
    eligible = [t for t in elapsed if t <= now - grace]
    if not eligible:
        return Decision(workflow, kind, "skip",
                        f"no fire older than {grace.total_seconds() / 3600:g}h grace")
    due = eligible[-1]
    latest = elapsed[-1]
    window = due - SLACK
    last_success = _last_success(runs)
    overdue_hours = ((now - last_success).total_seconds() / 3600.0) if last_success else 24 * 30

    if any(r.get("status") in ACTIVE_STATUSES for r in runs):
        return Decision(workflow, kind, "skip", "a run is queued or in progress",
                        overdue_hours, due, last_success)
    served = [r for r in runs if _covers(r, window)]
    if served:
        succeeded = any(
            r.get("conclusion") == "success"
            and (parse_ts(r.get("updated_at")) or parse_ts(r.get("created_at")) or now) >= window
            for r in served
        )
        if latest != due:
            how = "succeeded" if succeeded else "was already attempted"
            reason = (f"inside grace until {iso(latest + grace)}; "
                      f"latest fire {iso(latest)} is not eligible yet; "
                      f"previous fire {iso(due)} {how}")
        elif succeeded:
            reason = f"succeeded since {iso(due)}"
        else:
            reason = f"already attempted since {iso(due)} (not retrying a failure)"
        return Decision(workflow, kind, "skip", reason, overdue_hours, due, last_success)

    reason = f"due {iso(due)}, last success {iso(last_success)} ({overdue_hours:.1f}h)"
    return Decision(workflow, kind, "dispatch", reason, overdue_hours, due, last_success)


def _tier(decision: Decision) -> int:
    # Hourly data first (the dropped ticks), then the site publish, then dailies.
    if decision.kind == "hourly" and decision.workflow != "pages.yml":
        return 0
    if decision.workflow == "pages.yml":
        return 1
    return 2


def apply_cap(decisions: list[Decision], cap: int) -> list[Decision]:
    """Keep at most ``cap`` dispatches. Others stay visible as capped skips."""
    if cap < 0:
        raise ValueError("cap must be >= 0")
    wanted = [d for d in decisions if d.action == "dispatch"]
    wanted.sort(key=lambda d: (_tier(d), -d.overdue_hours, d.workflow))
    keep = {d.workflow for d in wanted[:cap]}
    for d in decisions:
        if d.action == "dispatch" and d.workflow not in keep:
            d.action = "skip"
            d.reason = f"overdue but cap {cap} reached ({d.reason})"
    return decisions


def plan(now: datetime, snapshots: dict[str, dict], cap: int = MAX_DISPATCH) -> list[Decision]:
    """``snapshots[filename]`` is ``{state, runs, error}``.

    ``runs is None`` means the runs request failed (fail closed: no dispatch).
    A workflow whose state is not ``active`` is skipped, not dispatched.
    """
    decisions: list[Decision] = []
    for workflow in sorted(WATCHES):
        kind, crons = WATCHES[workflow]
        snap = snapshots.get(workflow) or {}
        error = snap.get("error")
        if error:
            decisions.append(Decision(workflow, kind, "error", str(error)))
            continue
        state = snap.get("state")
        if state != "active":
            decisions.append(Decision(
                workflow, kind, "skip",
                f"workflow state is {state or 'missing'}; not dispatching"))
            continue
        runs = snap.get("runs")
        if runs is None:
            decisions.append(Decision(workflow, kind, "error", "runs were not loaded"))
            continue
        decisions.append(classify(workflow, kind, crons, runs, now))
    return apply_cap(decisions, cap)


def render_summary(decisions: list[Decision], *, dry_run: bool, disabled: bool,
                   cap: int, posted: list[str]) -> str:
    if disabled:
        banner = "**Disabled** (`CRON_WATCHDOG_DISABLED`). No API calls, no dispatches."
    elif dry_run:
        banner = "**Dry run.** Decisions below were not dispatched."
    else:
        banner = f"Dispatched **{len(posted)}** workflow(s) (cap {cap})."
    lines = ["## Cron watchdog", "", banner, "",
             "| workflow | kind | decision | detail |",
             "|---|---|---|---|"]
    for d in decisions:
        detail = d.reason.replace("|", "/")
        if d.detail:
            detail = f"{detail} — {d.detail.replace('|', '/')}"
        lines.append(f"| `{d.workflow}` | {d.kind} | {d.action} | {detail} |")
    lines.append("")
    lines.append(
        "Hourly and daily grace is 3 hours (a daily job is eligible once its "
        "scheduled UTC time is 3 hours in the past). Queued or in-progress runs "
        "are left alone. A completed failure is not retried until the next fire "
        "is itself overdue. Cancelled runs do not count as served."
    )
    lines.append("")
    return "\n".join(lines)


def write_summary(text: str) -> None:
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text)
        if not text.endswith("\n"):
            fh.write("\n")


class GitHub:
    """Minimal REST client. The token is never logged."""

    def __init__(self, repo: str, token: str) -> None:
        self.repo = repo
        self.token = token

    def _request(self, method: str, path: str, body: dict | None = None) -> tuple[int, Any]:
        url = API_ROOT + path
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": UA,
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as e:
            raw = e.read() if e.fp is not None else b""
            status = e.code
        if status == 204 or not raw:
            return status, None
        text = raw.decode("utf-8", "replace")
        try:
            return status, json.loads(text)
        except json.JSONDecodeError:
            return status, text

    def get(self, path: str) -> Any:
        status, payload = self._request("GET", path)
        if status != 200:
            raise RuntimeError(f"GET {path} -> HTTP {status}: {_clip(payload)}")
        return payload

    def post(self, path: str, body: dict) -> tuple[int, Any]:
        return self._request("POST", path, body)


def _clip(payload: Any, n: int = 240) -> str:
    text = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def list_states(gh: GitHub) -> dict[str, str]:
    """``{filename: state}`` for every workflow on the default branch."""
    states: dict[str, str] = {}
    for page in range(1, 6):
        data = gh.get(f"/repos/{gh.repo}/actions/workflows?per_page=100&page={page}")
        items = data.get("workflows") or []
        for item in items:
            path = item.get("path") or ""
            name = path.rsplit("/", 1)[-1]
            if name:
                states[name] = item.get("state") or ""
        if len(items) < 100:
            break
    return states


def runs_on_ref(runs: list[dict], ref: str) -> list[dict]:
    """Keep runs on the branch we would dispatch. Pull-request runs are already
    excluded by the API flag; this also drops a manual run of another branch."""
    return [r for r in runs if r.get("head_branch") in (None, ref)]


def list_runs(gh: GitHub, workflow: str, ref: str) -> list[dict]:
    quoted = urllib.parse.quote(workflow)
    path = (f"/repos/{gh.repo}/actions/workflows/{quoted}/runs"
            f"?per_page={RUNS_PER_WORKFLOW}&exclude_pull_requests=true")
    data = gh.get(path)
    return runs_on_ref(data.get("workflow_runs") or [], ref)


def dispatch(gh: GitHub, workflow: str, ref: str) -> tuple[int, Any]:
    quoted = urllib.parse.quote(workflow)
    # Omit `inputs`. Workflows with defaults (daily-audit, data-health,
    # lthcs-trends-daily) then use those defaults; workflows with no inputs
    # reject an unknown inputs object.
    return gh.post(
        f"/repos/{gh.repo}/actions/workflows/{quoted}/dispatches",
        {"ref": ref},
    )


def dispatch_accepted(status: int) -> bool:
    # 204 is the historical response. Current REST docs return 200 with the
    # new run id. Accept both so an API-version difference does not fail closed
    # after a successful dispatch.
    return status in (200, 204)


def run(gh: GitHub, *, ref: str, now: datetime, cap: int, dry_run: bool) -> tuple[list[Decision], list[str]]:
    states = list_states(gh)
    snapshots: dict[str, dict] = {}
    for workflow in WATCHES:
        if workflow not in states:
            snapshots[workflow] = {"error": "workflow not found on the default branch"}
            continue
        try:
            runs = list_runs(gh, workflow, ref)
        except Exception as e:  # noqa: BLE001 - one target must not hide the others
            snapshots[workflow] = {"state": states[workflow], "error": f"runs: {e}"}
            continue
        snapshots[workflow] = {"state": states[workflow], "runs": runs}
    decisions = plan(now, snapshots, cap=cap)
    posted: list[str] = []
    if dry_run:
        return decisions, posted
    queue = sorted((d for d in decisions if d.action == "dispatch"),
                   key=lambda d: (_tier(d), -d.overdue_hours, d.workflow))
    for d in queue:
        # A short gap keeps a full cap of dispatches under the secondary rate limit.
        if posted:
            time.sleep(2)
        try:
            status, payload = dispatch(gh, d.workflow, ref)
        except Exception as e:  # noqa: BLE001
            d.action = "error"
            d.detail = f"dispatch failed: {e}"
            continue
        if not dispatch_accepted(status):
            d.action = "error"
            d.detail = f"dispatch HTTP {status}: {_clip(payload)}"
            continue
        posted.append(d.workflow)
        if isinstance(payload, dict) and payload.get("html_url"):
            d.detail = str(payload["html_url"])
        else:
            d.detail = f"HTTP {status}"
    return decisions, posted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "btabiado/alpine-data"))
    parser.add_argument("--ref", default=os.environ.get("CRON_WATCHDOG_REF") or "main")
    parser.add_argument("--max-dispatch", type=int, default=MAX_DISPATCH)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    disabled = env_flag("CRON_WATCHDOG_DISABLED")
    dry_run = args.dry_run or env_flag("CRON_WATCHDOG_DRY_RUN")
    if disabled:
        text = render_summary([], dry_run=False, disabled=True, cap=args.max_dispatch, posted=[])
        write_summary(text)
        print("[cron-watchdog] disabled via CRON_WATCHDOG_DISABLED")
        return 0

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        write_summary(render_summary(
            [Decision("-", "-", "error", "GITHUB_TOKEN is not set")],
            dry_run=dry_run, disabled=False, cap=args.max_dispatch, posted=[],
        ))
        print("[cron-watchdog] GITHUB_TOKEN is not set", file=sys.stderr)
        return 1

    gh = GitHub(args.repo, token)
    try:
        decisions, posted = run(gh, ref=args.ref, now=utcnow(), cap=args.max_dispatch, dry_run=dry_run)
    except Exception as e:  # noqa: BLE001 - the summary is the alarm
        write_summary(render_summary(
            [Decision("-", "-", "error", f"watchdog failed: {e}")],
            dry_run=dry_run, disabled=False, cap=args.max_dispatch, posted=[],
        ))
        print(f"[cron-watchdog] {e}", file=sys.stderr)
        return 1

    text = render_summary(decisions, dry_run=dry_run, disabled=False,
                          cap=args.max_dispatch, posted=posted)
    write_summary(text)
    for d in decisions:
        tail = f" — {d.detail}" if d.detail else ""
        print(f"[cron-watchdog] {d.action} {d.workflow}: {d.reason}{tail}")
    errors = [d for d in decisions if d.action == "error"]
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
