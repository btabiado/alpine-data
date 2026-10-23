#!/usr/bin/env python3
"""Daily audit report builder: problems, regressions vs. yesterday, alert verdict.

Merges the two halves of the daily audit —

    scripts/daily_audit_data.py  -> data.json  (feeds, workflows, schedules,
                                                API status, spot checks)
    scripts/daily_audit_ux.mjs   -> ux.json    (browser audit of the live site)

— into one report per UTC day:

    audit/daily/YYYY-MM-DD.json   structured: summary, problems, regressions,
                                  and the raw sections
    audit/daily/YYYY-MM-DD.md     short human summary: headline, NEW problems
                                  first, then ongoing, then fixed
    audit/daily/latest.json / latest.md   copies of today's

and prunes dated reports older than --keep-days (default 90).

Every finding becomes a *problem* with a stable ``id`` so consecutive days can
be diffed: new = in today's report only, ongoing = in both (``first_seen`` is
carried forward), fixed = in yesterday's only. When a section could not run
today (API outage, UX budget exhausted) yesterday's problems from that section
are carried as ongoing + ``unverified`` instead of being reported as fixed —
an outage of the auditor must never read as a recovery.

Severities:
    P0  a tab a user cannot reach, an uncaught JS exception, a page that does
        not load, a feed stale/broken per data_health, a workflow failing two
        days running, the live site not rebuilt for hours
    P1  a workflow failure today, a scheduled workflow that missed a day or
        whose last scheduled run failed with no success since (a later
        manual or watchdog run clears it), a spot check that disagrees
        with its primary source, console errors, failed same-origin
        requests, visible NaN/undefined text, an upstream API down after
        two consecutive failed probe runs, an LTHCS pillar other than
        the accepted thesis_integrity stub that is the 50.0 placeholder
        for the whole universe
    P2  layout overflow at 390px, heavy pages, degraded/blocked upstreams
        (including a one-off probe failure), a feed whose source was
        checked and has not published newer data
        (disclosed upstream lag), a transient Farside 5xx/timeout soft warning
        while the ETF flows CSV is under 2 trading days old (the job exits 0;
        this is not a workflow failure). A late cron start or a skipped
        schedule is a P2 only when that condition has already lasted more
        than a day AND the job's output is stale or it has no success
        (schedule, watchdog catch-up, or manual run) inside its freshness
        window. Late or skipped jobs whose data is fresh are one
        informational line, not a P2 each.

Alert verdict (written to $GITHUB_OUTPUT as ``alert_action``):
    open    there is at least one NEW P0 -> open/update the tracking issue
    update  P0s exist but none are new -> refresh an already-open issue only
    close   no P0 at all -> close the tracking issue
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

P0, P1, P2 = "P0", "P1", "P2"
SEV_ORDER = {P0: 0, P1: 1, P2: 2}
# api_status.FAILURES_BEFORE_DOWN. A recorded streak below this is one
# failed probe run: grade it P2 (degraded), not P1. A missing streak is a
# snapshot from before the counter and stays P1, so an outage already on
# the board is not hidden.
API_FAILURES_BEFORE_DOWN = 2

PAGE_LABELS = {
    "v1": "V1 (/)", "summit": "/summit/", "health": "/health/",
    "real-estate": "/real-estate/", "lthcs": "/lthcs/",
    "api-universe": "/api-universe/",
}

# Thresholds for the P2 "heavy page" flag (initial load, encoded bytes).
HEAVY_BYTES = 10_000_000
HEAVY_REQUESTS = 200
# Schedules: a cron is "late" when its median start delay exceeds this, and
# a several-times-a-day cron "skipped" when it gets fewer than this share
# of its ticks. Neither is a P2 by itself — see ``_timing_is_p2``.
# LATE_MINUTES matches daily_audit_data.SCHEDULE_LATE_MINUTES.
LATE_MINUTES = 180
MIN_COVERAGE_PCT = 50.0
# Same line as scripts/publish_github_status.py. A cron that fires at least
# every 6h stays fresh when a success landed within 6h (GitHub drops those
# ticks; the watchdog's catch-up is what keeps the data current). Slower
# crons are fresh for twice their longest gap (a daily job, 48h).
FREQUENT_CRON_MAX_H = 6.0
FREQUENT_FRESHNESS_H = 6.0
# "More than 1 day": two distinct due days, or a span strictly over 24h.
SCHEDULE_PERSIST_H = 24.0
_CRON_LOOKBACK = timedelta(days=62)
# Live payload stamps older than this are listed in the markdown (info only;
# freshness verdicts belong to data_health).
STAMP_NOTE_H = 48

# Console errors that are real but cosmetic: browser complaints about markup,
# not broken behaviour. Kept visible, at P2.
BENIGN_CONSOLE = (
    re.compile(r"Content Security Policy directive 'frame-ancestors' is ignored", re.I),
)

# Pages whose top-level tabs the UX audit opens one by one.
# (V2 at /v2/ was retired in 2026-10; /v2/ is now a redirect to /.)
TABBED_PAGES = ("v1",)

DATED_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.(json|md)$")

def _quality_acceptance(data: dict | None) -> tuple[str, bool]:
    """(workflow name, whether the recorded verdict explicitly does not fail).

    Suppression is opt-in and uses only the ``quality_audit`` section that
    ``daily_audit_data.section_quality_audit`` wrote into this day's JSON.
    ``verdict_fails is False`` means the committed LTHCS verdict — including
    an accepted known gap — is not an error, so a stale red Actions run for
    that workflow is not paged. A missing section, a missing flag, or any
    other value keeps the old behavior: the red run still pages. Historical
    fixtures that predate the section therefore stay stable, and a summary
    that could not be read cannot clear the alarm.
    """
    sec = data.get("quality_audit") if isinstance(data, dict) else None
    if not isinstance(sec, dict):
        return "lthcs-quality-audit-monthly", False
    name = str(sec.get("workflow") or "lthcs-quality-audit-monthly")
    return name, sec.get("verdict_fails") is False


# Problem-id prefix -> the section whose failure means "not re-checked today".
SECTION_OF_PREFIX = {
    "feed:": "data_health",
    "workflow:": "workflows",
    "schedule:": "schedules",
    "api:": "api_status",
    "spot:": "spot_checks",
    "live:": "live_payloads",
    "ux:": "ux",
    "lthcs:": "lthcs_pillars",
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _short(s: Any, n: int = 160) -> str:
    s = re.sub(r"\s+", " ", "" if s is None else str(s)).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _key(text: str) -> str:
    norm = re.sub(r"\d+", "#", re.sub(r"\s+", " ", text or "")).strip().lower()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:8]


def _hours(h: Any) -> str:
    if h is None:
        return "?"
    h = float(h)
    if h < 48:
        return f"{h:.0f}h" if h >= 1 else f"{h * 60:.0f}m"
    return f"{h / 24:.0f}d"


def _section_ok(sec: Any) -> bool:
    return isinstance(sec, dict) and sec.get("status") != "error"


def problem(pid: str, severity: str, category: str, title: str, detail: str = "",
            **extra: Any) -> dict:
    p = {"id": pid, "severity": severity, "category": category, "title": _short(title, 220)}
    if detail:
        p["detail"] = _short(detail, 600)
    p.update({k: v for k, v in extra.items() if v is not None})
    return p


# ---------------------------------------------------------------------------
# problems from the data half
# ---------------------------------------------------------------------------

_GENERIC_EXIT = re.compile(r"^Process completed with exit code \d+\.?$")
_ANNOTATION_NOISE = re.compile(r"Node\.js \d+ (is deprecated|actions are deprecated)", re.I)


def is_etf_flows_soft_warning(title: str | None, message: str | None) -> bool:
    """True for the fetcher's transient-Farside notice, not a hard refusal.

    ``scripts/fetch_etf_flows.py`` exits 0 and emits ``::warning title=ETF flows
    delayed`` while the CSV is under two trading days old. That annotation is
    a notice. ``ETF flows failed`` is the exit-1 refusal and stays a failure.
    """
    title = (title or "").strip()
    message = (message or "").strip()
    lowered = title.lower()
    if lowered.startswith("etf flows failed"):
        return False
    if lowered.startswith("etf flows delayed"):
        return True
    return message.lower().startswith("soft warning:")


def _annotation_noise(ann: dict) -> bool:
    text = f"{ann.get('title') or ''} {ann.get('message') or ''}"
    return bool(_ANNOTATION_NOISE.search(text))


def _soft_warning_annotations(w: dict) -> list[dict]:
    found: list[dict] = []
    for job in w.get("failed_jobs") or []:
        for ann in job.get("annotations") or []:
            if is_etf_flows_soft_warning(ann.get("title"), ann.get("message")):
                found.append(ann)
    for ann in w.get("soft_warnings") or []:
        if is_etf_flows_soft_warning(ann.get("title"), ann.get("message")):
            found.append(ann)
    return found


def _only_soft_etf_warning(w: dict) -> bool:
    """The run's informative annotations are only the ETF soft warning.

    A generic "exit code 1" line and Node deprecation notices do not count.
    No annotations at all is not this case: an unexplained red run stays a
    failure. A real ``::error`` (``ETF flows failed``) keeps it a failure too.
    """
    anns = [a for job in (w.get("failed_jobs") or []) for a in (job.get("annotations") or [])]
    if not anns:
        return False
    informative: list[dict] = []
    for ann in anns:
        if _annotation_noise(ann):
            continue
        msg = (ann.get("message") or "").strip()
        if _GENERIC_EXIT.match(msg):
            continue
        informative.append(ann)
    if not informative:
        return False
    return all(is_etf_flows_soft_warning(a.get("title"), a.get("message")) for a in informative)


def _best_annotation(jobs: list[dict]) -> str:
    """The most informative line from a failed run's annotations.

    The ETF soft warning is skipped when a real failure annotation is present,
    so a red run is not described as if the notice were the failure.
    """
    lines: list[str] = []
    for j in jobs or []:
        steps = ", ".join(j.get("failed_steps") or [])
        anns = [a for a in (j.get("annotations") or []) if not _annotation_noise(a)]
        specific = [a for a in anns
                    if not _GENERIC_EXIT.match((a.get("message") or "").strip())
                    and not is_etf_flows_soft_warning(a.get("title"), a.get("message"))]
        if not specific:
            specific = [a for a in anns if not _GENERIC_EXIT.match((a.get("message") or "").strip())]
        pick = specific[0] if specific else (anns[0] if anns else None)
        bit = ""
        if pick:
            title = (pick.get("title") or "").strip()
            msg = (pick.get("message") or "").strip()
            bit = f"{title}: {msg}" if title else msg
        if steps and bit:
            lines.append(f"step '{steps}' — {bit}")
        elif steps:
            lines.append(f"step '{steps}' failed")
        elif bit:
            lines.append(bit)
    return "; ".join(lines)


# ---------------------------------------------------------------------------
# late / skipped schedules: P2 only when stale, otherwise one info line
# ---------------------------------------------------------------------------

_CRON_FIRE_TIMES = None


def _cron_fire_times(expr: str, start: datetime, end: datetime) -> list[datetime]:
    """``daily_audit_data.cron_fire_times``, loaded from the sibling script.

    The report is run as a file, not as a package, so a normal import of
    that module is not on ``sys.path``.
    """
    global _CRON_FIRE_TIMES
    if _CRON_FIRE_TIMES is None:
        import importlib.util
        path = Path(__file__).resolve().with_name("daily_audit_data.py")
        spec = importlib.util.spec_from_file_location("_dad_cron_for_report", path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load {path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _CRON_FIRE_TIMES = mod.cron_fire_times
    return _CRON_FIRE_TIMES(expr, start, end)


def _parse_ts(value: Any) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _audit_clock(data: dict) -> datetime | None:
    """When this audit's data was collected. None on fixtures that omit it."""
    for value in (data.get("generated_at"),):
        stamp = _parse_ts(value)
        if stamp is not None:
            return stamp
    dh = data.get("data_health")
    if isinstance(dh, dict):
        stamp = _parse_ts(dh.get("checked_at"))
        if stamp is not None:
            return stamp
    return None


def _workflows_by_name(data: dict) -> dict[str, dict]:
    wf = data.get("workflows")
    if not isinstance(wf, dict):
        return {}
    return {w.get("name"): w for w in (wf.get("workflows") or []) if isinstance(w, dict) and w.get("name")}


def _freshness_window_h(crons: list, now: datetime) -> float | None:
    """How long this schedule may go without a success and still be fresh.

    Mirrors ``publish_github_status.freshness_hours``: twice the longest gap
    between fires, and at least 6h when the cron is more frequent than that.
    """
    if not crons:
        return None
    fires: list[datetime] = []
    start = now - _CRON_LOOKBACK
    end = now + timedelta(days=1)
    for expr in crons:
        try:
            fires.extend(_cron_fire_times(str(expr), start, end))
        except (TypeError, ValueError):
            return None
    ordered = sorted(set(fires))
    if len(ordered) < 2:
        return None
    gap_h = max((b - a).total_seconds() for a, b in zip(ordered, ordered[1:])) / 3600.0
    doubled = 2.0 * gap_h
    if gap_h <= FREQUENT_CRON_MAX_H:
        return max(doubled, FREQUENT_FRESHNESS_H)
    return doubled


def _owner_mentions(owner: str, schedule: dict) -> bool:
    """True when a data_health owner string names this workflow's file."""
    text = owner.lower()
    if not text:
        return False
    base = Path(str(schedule.get("path") or "")).name.lower()
    if base and base in text:
        return True
    name = str(schedule.get("name") or "").strip().lower()
    return bool(name) and (f"{name}.yml" in text or f"{name}.yaml" in text)


def _output_is_stale(schedule: dict, data: dict) -> bool | None:
    """Whether a feed this job owns is stale.

    True: at least one owned feed is in data_health's failing statuses.
    False: the job owns feeds and none of them is failing (ok, skipped,
    suppressed, and disclosed upstream lag all count as not stale — the
    disclosed row has its own P2).
    None: this job has no feed, or data_health did not run, so freshness
    falls through to the last success.
    """
    dh = data.get("data_health")
    if not isinstance(dh, dict) or not _section_ok(dh):
        return None
    failing = {str(x).lower() for x in (dh.get("failing_statuses") or ())}
    if not failing:
        failing = {"stale", "unknown", "missing", "unwatched", "expired"}
    owned: list[dict] = []
    for row in list(dh.get("results") or []) + list(dh.get("extra_failing") or []):
        if isinstance(row, dict) and _owner_mentions(str(row.get("owner") or ""), schedule):
            owned.append(row)
    if not owned:
        return None
    return any(str(row.get("status") or "").lower() in failing for row in owned)


def _success_stamps(schedule: dict, workflow: dict | None) -> list[datetime]:
    """Completion times of successes, including catch-ups and manual runs."""
    stamps: list[datetime] = []
    runs: list[Any] = [
        schedule.get("last_run"),
        schedule.get("last_catchup"),
        schedule.get("recovered_run"),
    ]
    if isinstance(workflow, dict):
        runs.extend((workflow.get("latest_24h"), workflow.get("previous_day")))
    for run in runs:
        if not isinstance(run, dict) or run.get("conclusion") != "success":
            continue
        stamp = _parse_ts(run.get("updated_at")) or _parse_ts(run.get("created_at"))
        if stamp is not None:
            stamps.append(stamp)
    # Collector's newest success of any event, which may be newer than the
    # last schedule event (a manual run that was not clearing a failure).
    extra = _parse_ts(schedule.get("last_success_at"))
    if extra is not None:
        stamps.append(extra)
    return stamps


def _success_within_window(schedule: dict, workflow: dict | None, now: datetime | None) -> bool | None:
    """True when a success landed inside the freshness window.

    None when this row has no clock or no parseable cron, so a legacy
    fixture is not treated as fresh. False when the clock is known and
    nothing succeeded in the window.
    """
    if now is None:
        return None
    window_h = _freshness_window_h(schedule.get("crons") or [], now)
    if window_h is None:
        return None
    stamps = _success_stamps(schedule, workflow)
    if not stamps:
        return False
    return (now - max(stamps)) <= timedelta(hours=window_h)


def _condition_persists(schedule: dict, kind: str) -> bool:
    """True when a late start or skipped ticks already cover more than a day.

    ``kind`` is ``late`` or ``skip``. Current collector rows carry
    ``{kind}_days`` and ``{kind}_span_h``. Older audit JSON does not: the
    median and the coverage were computed over every due day in the window,
    so more than one due day is the same fact.
    """
    days_key, span_key = f"{kind}_days", f"{kind}_span_h"
    if days_key in schedule or span_key in schedule:
        days = schedule.get(days_key)
        span = schedule.get(span_key)
        if days is not None and int(days) >= 2:
            return True
        if span is not None and float(span) > SCHEDULE_PERSIST_H:
            return True
        return False
    return (schedule.get("due_days") or 0) > 1


def _is_late(schedule: dict) -> bool:
    delay = schedule.get("median_delay_min")
    return delay is not None and float(delay) > LATE_MINUTES


def _is_dropped(schedule: dict) -> bool:
    cov = schedule.get("coverage_pct")
    if cov is None or float(cov) >= MIN_COVERAGE_PCT:
        return False
    # One tick a day is judged as a missed day (P1), not as dropped ticks.
    return (schedule.get("due_runs") or 0) > (schedule.get("due_days") or 0)


def _data_is_fresh(schedule: dict, data: dict, workflow: dict | None, now: datetime | None) -> bool:
    """A late or skipped job whose output is current and that did succeed."""
    if _output_is_stale(schedule, data) is True:
        return False
    return _success_within_window(schedule, workflow, now) is True


def _timing_is_p2(schedule: dict, data: dict, workflow: dict | None,
                  now: datetime | None, kind: str) -> bool:
    """Open a P2 only after the pattern lasts more than a day and data is stale.

    Stale means a feed this job owns is failing, or there is no success
    (schedule, watchdog catch-up, or manual) inside the freshness window.
    A row with no clock keeps the old P2 so existing fixtures stay stable.
    """
    if not _condition_persists(schedule, kind):
        return False
    if _output_is_stale(schedule, data) is True:
        return True
    if _success_within_window(schedule, workflow, now) is True:
        return False
    return True


def _had_watchdog(schedule: dict) -> bool:
    return bool(schedule.get("catchup_ticks") or schedule.get("catchup_runs") or schedule.get("last_catchup"))


def _n_jobs(n: int, what: str) -> str:
    noun = "job" if n == 1 else "jobs"
    return f"{n} {noun} {what}"


def format_schedule_note(late: int, skipped: int, *, watchdog: bool) -> str | None:
    """One informational line for late or skipped jobs whose data is fresh."""
    if late <= 0 and skipped <= 0:
        return None
    parts: list[str] = []
    if late > 0:
        parts.append(_n_jobs(late, "started late"))
    if skipped > 0:
        parts.append(_n_jobs(skipped, "skipped schedule ticks"))
    text = "; ".join(parts)
    if watchdog:
        text += "; watchdog caught up"
    return text + "; data fresh"


def schedule_timing_note(data: dict | None) -> str | None:
    """Summarize late/skipped jobs that are fresh. Not a finding."""
    if not isinstance(data, dict):
        return None
    sc = data.get("schedules")
    if not isinstance(sc, dict) or not _section_ok(sc):
        return None
    now = _audit_clock(data)
    by_name = _workflows_by_name(data)
    late = skipped = 0
    watchdog = False
    for schedule in sc.get("schedules") or []:
        if not isinstance(schedule, dict):
            continue
        workflow = by_name.get(schedule.get("name"))
        if not _data_is_fresh(schedule, data, workflow, now):
            continue
        is_late = _is_late(schedule)
        is_dropped = _is_dropped(schedule)
        if not is_late and not is_dropped:
            continue
        if is_late:
            late += 1
        if is_dropped:
            skipped += 1
        watchdog = watchdog or _had_watchdog(schedule)
    return format_schedule_note(late, skipped, watchdog=watchdog)


def _disclosed_upstream_lag(row: dict) -> bool:
    """A freshness row data_health marked disclosed.

    History disclosures (``path`` contains ``[history:``) are known gaps from
    health/known_gaps.json. They stay out of the audit problem list. A
    freshness row with status ``disclosed`` is an upstream that was checked
    and has not published anything newer — reported at P2, not paged as P0.
    """
    if str(row.get("status") or "").lower() != "disclosed":
        return False
    path = str(row.get("path") or "")
    return bool(path) and "[history:" not in path


def _api_failure_detail(label: str, api: dict, streak: Any) -> str:
    """Why an upstream row is down or degraded, when the probe said so.

    The note is the probe's own text (timeout vs HTTP status, and the
    streak sentence). The previous probe is appended so a second strike
    is not read as a one-off flip from yesterday's snapshot.
    """
    parts: list[str] = []
    notes = api.get("notes") if isinstance(api.get("notes"), dict) else {}
    note = notes.get(label) if isinstance(notes, dict) else None
    if isinstance(note, str) and note.strip():
        parts.append(note.strip())
    elif type(streak) is int and streak >= API_FAILURES_BEFORE_DOWN:
        parts.append(f"{streak} consecutive probe failures")
    prior = api.get("prior_probe") if isinstance(api.get("prior_probe"), dict) else {}
    prior_verdicts = prior.get("verdicts") if isinstance(prior.get("verdicts"), dict) else {}
    prior_verdict = prior_verdicts.get(label) if isinstance(prior_verdicts, dict) else None
    if prior_verdict:
        when = prior.get("generated_at") or "the previous probe"
        parts.append(f"previous probe {when} was {prior_verdict}")
    return "; ".join(parts)


def data_problems(data: dict | None) -> list[dict]:
    out: list[dict] = []
    if not isinstance(data, dict):
        return [problem("audit:data-missing", P1, "audit", "Data audit produced no output")]

    # -- feeds (data_health) -------------------------------------------------
    dh = data.get("data_health")
    if dh is not None and not _section_ok(dh):
        out.append(problem("audit:data_health", P1, "audit",
                           "data_health.py could not be evaluated", dh.get("error", "")))
    elif dh:
        failing = set(dh.get("failing_statuses") or ["stale", "unknown", "missing", "unwatched", "expired"])
        for r in dh.get("results") or []:
            if r.get("status") in failing:
                out.append(problem(
                    f"feed:{r.get('path')}", P0, "feed",
                    f"Feed {r.get('path')} is {str(r.get('status')).upper()} "
                    f"({_hours(r.get('age_h'))} old, limit {_hours(r.get('limit_h'))})",
                    r.get("detail", ""), owner=r.get("owner") or None))
            elif _disclosed_upstream_lag(r):
                # data_health's DISCLOSED freshness row: the pipeline checked
                # and the source has not published. History disclosures stay
                # out of this list (they are not failures). P2 is visible in
                # the report and does not page — only a P0 opens the issue.
                path = r.get("path")
                out.append(problem(
                    f"feed:{path}:upstream-lag", P2, "feed",
                    f"Feed {path} is an upstream lag "
                    f"({_hours(r.get('age_h'))} old, limit {_hours(r.get('limit_h'))}); "
                    f"the source has not published a newer row",
                    r.get("detail", ""), owner=r.get("owner") or None))
        for r in dh.get("extra_failing") or []:
            name = r.get("path") or r.get("name") or r.get("feed") or "?"
            out.append(problem(
                f"feed:{r.get('section')}:{name}", P0, "feed",
                f"{r.get('section')}: {name} is {str(r.get('status')).upper()}",
                r.get("detail") or r.get("message") or ""))

    # -- workflows -------------------------------------------------------------
    # The quality verdict was recorded when the data JSON was collected.
    # An accepted known gap is not an error, even if Actions still shows
    # an older red run. No section in the JSON means do not suppress.
    quality_name, quality_ok = _quality_acceptance(data)
    quality_verdict_fails = not quality_ok

    def _actions_failure_is_error(name: str | None) -> bool:
        if name != quality_name:
            return True
        return quality_verdict_fails

    wf = data.get("workflows")
    flagged_workflows: set[str] = set()
    if wf is not None and not _section_ok(wf):
        out.append(problem("audit:workflows", P1, "audit",
                           "Workflow run history could not be read", wf.get("error", "")))
    elif wf:
        for w in wf.get("workflows") or []:
            name = w.get("name")
            if (w.get("failing_2_days") or w.get("failed_24h")) and not _actions_failure_is_error(name):
                continue
            latest = w.get("latest_24h") or {}
            # A green run (or a red run whose only informative annotation is
            # the fetcher's soft warning) is not a workflow failure. The CSV
            # age is what turns the same outage into an exit-1, and that
            # refusal is titled "ETF flows failed".
            only_soft = _only_soft_etf_warning(w)
            hard = ((w.get("failing_2_days") or w.get("failed_24h")) and not only_soft)
            if hard:
                why = _best_annotation(w.get("failed_jobs") or [])
                if w.get("failing_2_days"):
                    flagged_workflows.add(name)
                    out.append(problem(f"workflow:{name}:failing-2d", P0, "workflow",
                                       f"Workflow {name} failed two days running", why,
                                       url=latest.get("url")))
                elif w.get("failed_24h"):
                    flagged_workflows.add(name)
                    out.append(problem(f"workflow:{name}:failed", P1, "workflow",
                                       f"Workflow {name} failed in the last 24h", why,
                                       url=latest.get("url")))
            notes = _soft_warning_annotations(w)
            if notes and not hard:
                detail = " | ".join(
                    (f"{a.get('title')}: {a.get('message')}".strip(": ")
                     if a.get("title") else (a.get("message") or ""))
                    for a in notes[:3]
                )
                out.append(problem(
                    f"workflow:{name}:soft-warning", P2, "workflow",
                    f"Workflow {name} soft-warned: Farside was unreachable and "
                    f"the ETF flows CSV is under 2 trading days old",
                    detail, url=latest.get("url")))

    # -- schedules -------------------------------------------------------------
    sc = data.get("schedules")
    if sc is not None and not _section_ok(sc):
        out.append(problem("audit:schedules", P1, "audit",
                           "Scheduled-workflow history could not be read", sc.get("error", "")))
    elif sc:
        clock = _audit_clock(data)
        by_name = _workflows_by_name(data)
        for s in sc.get("schedules") or []:
            name = s.get("name")
            crons = ", ".join(s.get("crons") or [])
            workflow = by_name.get(name)
            if s.get("state") and s.get("state") != "active":
                out.append(problem(f"schedule:{name}:disabled", P1, "schedule",
                                   f"Scheduled workflow {name} is {s.get('state')} — its cron ({crons}) will not fire"))
            if s.get("missed_days"):
                out.append(problem(
                    f"schedule:{name}:missed", P1, "schedule",
                    f"Scheduled workflow {name} missed {len(s['missed_days'])} day(s) in the last week",
                    f"cron {crons}; no scheduled run on: {', '.join(s['missed_days'])}"))
            last = s.get("last_run") or {}
            # last_failed is already false when a later success (manual
            # workflow_dispatch or watchdog catch-up) recovered the workflow.
            # Do not keep the P1 until the next monthly schedule.
            if (s.get("last_failed") and name not in flagged_workflows
                    and _actions_failure_is_error(name)):
                out.append(problem(
                    f"schedule:{name}:failing", P1, "schedule",
                    f"Scheduled workflow {name}: last scheduled run failed ({(last.get('created_at') or '?')[:10]})",
                    f"cron {crons}", url=last.get("url")))
            cov = s.get("coverage_pct")
            sched_cov = s.get("schedule_coverage_pct")
            catchup_ticks = s.get("catchup_ticks") or 0
            missed_ticks = s.get("missed_ticks")
            # coverage_pct is effective freshness (schedule event or a labeled
            # watchdog catch-up). A scheduler-only gap that the watchdog
            # filled is not "ticks that never ran". missed_ticks is the
            # remainder: due fires with neither. A low ratio is a P2 only
            # when the skips have lasted more than a day and the data is
            # stale (or nothing has succeeded in the freshness window).
            # Fresh jobs are one line from schedule_timing_note, not a P2.
            if _is_dropped(s) and _timing_is_p2(s, data, workflow, clock, "skip"):
                detail = (f"cron {crons}; {s.get('scheduled_runs')} scheduled runs")
                if catchup_ticks:
                    detail += f", {catchup_ticks} tick(s) covered first by a watchdog catch-up"
                detail += f" for {s.get('due_runs')} due ticks"
                if sched_cov is not None and abs(float(sched_cov) - float(cov)) >= 0.05:
                    detail += f"; scheduler delivered {sched_cov:.0f}%, effective {cov:.0f}%"
                if missed_ticks is not None:
                    detail += f"; {int(missed_ticks)} tick(s) never ran"
                title = f"{name}: {cov:.0f}% of cron ticks ran"
                if catchup_ticks and sched_cov is not None and abs(float(sched_cov) - float(cov)) >= 0.05:
                    title = (f"{name}: {cov:.0f}% of cron ticks covered "
                             f"({catchup_ticks} by watchdog catch-up)")
                    if missed_ticks is not None:
                        title += f"; {int(missed_ticks)} never ran"
                elif missed_ticks is not None:
                    title = f"{name}: {cov:.0f}% of cron ticks ran; {int(missed_ticks)} never ran"
                item = f"{name} {cov:.0f}%"
                if missed_ticks is not None:
                    item = (f"{name} {cov:.0f}% ({catchup_ticks} catch-up, "
                            f"{int(missed_ticks)} missed)")
                out.append(problem(
                    f"schedule:{name}:dropped", P2, "schedule", title, detail,
                    group="schedule:dropped", group_title="Crons that lose most of their ticks",
                    item=item))
            delay = s.get("median_delay_min")
            if _is_late(s) and _timing_is_p2(s, data, workflow, clock, "late"):
                late_detail = f"cron {crons}"
                sched_delay = s.get("schedule_median_delay_min")
                if sched_delay is not None and abs(float(sched_delay) - float(delay)) >= 30:
                    late_detail += (f"; scheduler median {float(sched_delay) / 60:.1f}h, "
                                    f"effective {float(delay) / 60:.1f}h")
                out.append(problem(
                    f"schedule:{name}:late", P2, "schedule",
                    f"{name} starts ~{delay / 60:.1f}h after its cron time (median)", late_detail,
                    group="schedule:late",
                    group_title=f"Crons starting >{LATE_MINUTES // 60}h late (median delay)",
                    item=f"{name} {delay / 60:.1f}h"))

    # -- API status --------------------------------------------------------------
    api = data.get("api_status")
    if api is not None and not _section_ok(api):
        out.append(problem("audit:api_status", P2, "audit",
                           "data/health/api_status.json could not be read", api.get("error", "")))
    elif api:
        streaks = api.get("streaks") if isinstance(api.get("streaks"), dict) else {}
        for label, verdict in sorted((api.get("verdicts") or {}).items()):
            # "retired" is a by-design row: the probe does not call the host
            # (bitinfocharts, retired 2026-10-19 after HTTP 403). It is not an
            # outage and must not reappear as a new blocked-API finding.
            if verdict in ("retired",):
                continue
            streak = streaks.get(label)
            # The probe rewrites a single connection failure to "degraded"
            # and leaves "down" only after API_FAILURES_BEFORE_DOWN runs
            # (USGS ScienceBase 2026-10-23: 10:00Z degraded, 12:43Z down).
            # Grade from the streak anyway. A snapshot that says "down"
            # with a streak below that bar is one failed run — P2, not P1.
            # No streak at all is a pre-counter snapshot: keep P1.
            if verdict == "down" and type(streak) is int and streak < API_FAILURES_BEFORE_DOWN:
                detail = _api_failure_detail(label, api, streak)
                word = "failure" if streak == 1 else "failures"
                bar = (f"{streak} consecutive probe {word}; "
                       f"not marked down until {API_FAILURES_BEFORE_DOWN}")
                if bar not in detail:
                    detail = f"{detail}; {bar}" if detail else bar
                out.append(problem(
                    f"api:{label}", P2, "api", f"Upstream API {label} is degraded", detail,
                    group="api:degraded", group_title="Upstream APIs degraded", item=label))
            elif verdict == "down":
                out.append(problem(
                    f"api:{label}", P1, "api", f"Upstream API {label} is DOWN",
                    _api_failure_detail(label, api, streak)))
            elif verdict in ("blocked", "degraded"):
                out.append(problem(f"api:{label}", P2, "api", f"Upstream API {label} is {verdict}",
                                   _api_failure_detail(label, api, streak),
                                   group=f"api:{verdict}", group_title=f"Upstream APIs {verdict}",
                                   item=label))
        if api.get("unwired_key_envs"):
            out.append(problem("api:unwired-keys", P2, "api",
                               "API keys named by the probe but not wired into pages.yml",
                               ", ".join(map(str, api["unwired_key_envs"]))))

    # -- spot checks -------------------------------------------------------------
    spot = data.get("spot_checks")
    if spot is not None and not _section_ok(spot):
        out.append(problem("audit:spot_checks", P1, "audit", "Spot checks could not run",
                           spot.get("error", "")))
    elif spot:
        for name, c in sorted((spot.get("checks") or {}).items()):
            if c.get("status") != "fail":
                continue
            sev = P0 if name == "live_site_build_age" else P1
            out.append(problem(f"spot:{name}", sev, "spot", spot_title(name, c), spot_detail(c)))

    # -- live payloads -----------------------------------------------------------
    live = data.get("live_payloads")
    if live is not None and not _section_ok(live):
        out.append(problem("audit:live_payloads", P1, "audit", "Live payloads could not be read",
                           live.get("error", "")))
    elif live:
        for p in live.get("payloads") or []:
            if p.get("status") == "fail":
                out.append(problem(f"live:payload:{p.get('name')}", P1, "live",
                                   f"Live sidecar {p.get('name')} is broken: {p.get('error')}"))

    # -- LTHCS pillar stubs ------------------------------------------------------
    # thesis_integrity is the only accepted full-universe stub (it is
    # listed in known_pillar_stubs.json and the daily job runs
    # --skip-thesis). Any other pillar at the 50.0 placeholder is a P1.
    # A missing section (historical fixtures) does not invent a finding.
    pillars = data.get("lthcs_pillars")
    if isinstance(pillars, dict) and not _section_ok(pillars):
        out.append(problem(
            "lthcs:pillar-check", P1, "lthcs",
            "LTHCS pillar stub check could not run",
            pillars.get("error", "")))
    elif isinstance(pillars, dict):
        accepted = ", ".join(pillars.get("accepted_pillars") or []) or "none"
        when = pillars.get("snapshot_date") or "the latest snapshot"
        for row in pillars.get("unaccepted_stubs") or []:
            if not isinstance(row, dict) or not row.get("pillar"):
                continue
            name = row["pillar"]
            cov = row.get("coverage_pct")
            n = row.get("n")
            out.append(problem(
                f"lthcs:{name}:stub", P1, "lthcs",
                f"LTHCS pillar {name} is STUB on {when} "
                f"({cov}% of names differ from the 50.0 placeholder)",
                f"Only an explicit entry in data/lthcs/known_pillar_stubs.json "
                f"is an accepted stub (currently {accepted}). "
                f"{n} scored names were graded. A full placeholder means the "
                f"pillar's inputs did not score.",
            ))
    return out


def spot_title(name: str, c: dict) -> str:
    if name == "live_site_build_age":
        return (f"Live site last rebuilt {_hours(c.get('age_h'))} ago "
                f"(limit {_hours(c.get('max_age_h'))}) — pages deploy may be stuck")
    if "price" in name:
        coin = name.split("_")[0].upper()
        reason = str(c.get("reason") or "")
        age = _hours(c.get("page_age_h"))
        source = c.get("source") or "CoinGecko"
        if "price_mismatch" in reason:
            stale_bit = " and the quote is stale" if "stale" in reason else ""
            return (f"{coin} price on the site ({c.get('page')}) is {c.get('diff_pct')}% off "
                    f"CoinGecko at the payload time ({c.get('reference')}){stale_bit} "
                    f"— payload {age} old; source {source}")
        if "stale" in reason:
            return (f"{coin} price on the site is stale "
                    f"(payload {age} old, limit {_hours(c.get('max_age_h'))}; source {source})")
        return (f"{coin} price on the site ({c.get('page')}) is {c.get('diff_pct')}% off "
                f"CoinGecko ({c.get('reference')})")
    pretty = name.replace("_", " ")
    return f"Spot check failed: {pretty} — {c.get('note') or 'mismatch'}"


def spot_detail(c: dict) -> str:
    keep = {k: v for k, v in c.items() if k not in ("status", "elapsed_s")}
    return ", ".join(f"{k}={v}" for k, v in keep.items())


# ---------------------------------------------------------------------------
# problems from the UX half
# ---------------------------------------------------------------------------

def ux_problems(ux: dict | None) -> list[dict]:
    if not isinstance(ux, dict):
        return [problem("audit:ux-missing", P1, "audit", "UX audit produced no output")]
    out: list[dict] = []
    if ux.get("fatal"):
        out.append(problem("audit:ux-fatal", P1, "audit", "UX audit could not run",
                           ux.get("fatal", "")))
    merged: dict[str, dict] = {}   # cross-viewport de-dup (same id -> one problem)

    def add(p: dict, vp: str) -> None:
        if p["id"] in merged:
            vps = merged[p["id"]].setdefault("viewports", [])
            if vp not in vps:
                vps.append(vp)
        else:
            p.setdefault("viewports", [vp])
            merged[p["id"]] = p

    skipped: list[str] = []
    for rec in ux.get("pages") or []:
        page, vp = rec.get("page"), rec.get("viewport")
        label = PAGE_LABELS.get(page, rec.get("path") or page)
        if rec.get("skipped"):
            skipped.append(f"{label}@{vp}")
            continue
        status = rec.get("status")
        if rec.get("error") or (isinstance(status, int) and status >= 400):
            add(problem(f"ux:{page}:{vp}:load", P0, "ux",
                        f"{label} failed to load on {vp}: {rec.get('error') or f'HTTP {status}'}"), vp)
            if not rec.get("tabs"):
                continue
        for e in rec.get("page_errors") or []:
            add(problem(f"ux:{page}:js-exception:{_key(e.get('text', ''))}", P0, "ux",
                        f"{label}: uncaught JS exception — {_short(e.get('text'), 140)}",
                        f"during {e.get('step')}"), vp)
        for e in rec.get("console_errors") or []:
            text = e.get("text", "")
            sev = P2 if any(rx.search(text) for rx in BENIGN_CONSOLE) else P1
            add(problem(f"ux:{page}:console:{_key(text)}", sev, "ux",
                        f"{label}: console error — {_short(text, 140)}", f"during {e.get('step')}"), vp)
        for f in rec.get("failed_requests") or []:
            path = urlparse(f.get("url") or "").path
            code = f.get("status") or f.get("error") or "failed"
            add(problem(f"ux:{page}:http:{code}:{path}", P1, "ux",
                        f"{label}: same-origin request {path} -> {code}", f"during {f.get('step')}"), vp)
        ov = rec.get("overflow") or {}
        if vp == "phone" and ov.get("overflow"):
            offenders = ", ".join(o.get("element", "?") for o in (ov.get("offenders") or [])[:3])
            add(problem(f"ux:{page}:phone:overflow", P2, "ux",
                        f"{label}: horizontal overflow at 390px (page is {ov.get('scroll_width')}px wide)",
                        f"widest elements: {offenders}" if offenders else ""), vp)
        for b in rec.get("bad_text") or []:
            add(problem(f"ux:{page}:bad-text:{b.get('match')}:{b.get('where')}", P1, "ux",
                        f"{label}: visible \"{b.get('match')}\" in {b.get('where')} — “{_short(b.get('text'), 90)}”",
                        f"during {b.get('step')}"), vp)
        if page in TABBED_PAGES and not rec.get("tabs"):
            add(problem(f"ux:{page}:{vp}:no-tabs", P1, "ux",
                        f"{label} @ {vp}: no tabs found (selector `.tabs [data-tab]`) — "
                        "the tab audit could not run"), vp)
        w = rec.get("weight") or {}
        if (w.get("bytes") or 0) > HEAVY_BYTES or (w.get("requests") or 0) > HEAVY_REQUESTS:
            add(problem(f"ux:{page}:heavy", P2, "ux",
                        f"{label}: heavy initial load — {w.get('requests')} requests, "
                        f"{(w.get('bytes') or 0) / 1e6:.1f} MB"), vp)
        for t in rec.get("tabs") or []:
            tid, tlabel = t.get("id"), t.get("label") or t.get("id")
            if t.get("skipped"):
                continue
            if t.get("reachable") is False:
                why = t.get("problem") or "not reachable"
                cover = f"; covered by {t['covered_by']}" if t.get("covered_by") else ""
                add(problem(f"ux:{page}:{vp}:tab-unreachable:{tid}", P0, "ux",
                            f"{label} @ {vp}: tab '{tlabel}' cannot be tapped ({why}{cover})",
                            f"opened via {t.get('via')}",
                            group=f"ux:{page}:{vp}:tab-unreachable",
                            group_title=f"{label} @ {vp}: tabs a user cannot tap ({why})",
                            item=tlabel), vp)
            if t.get("switched") is False:
                # Exit controls (Summit) have no panel. "Did not switch" means
                # the tap did not leave the dashboard, not a hidden #tab-<id>.
                if t.get("exit"):
                    add(problem(f"ux:{page}:{vp}:tab-dead:{tid}", P0, "ux",
                                f"{label} @ {vp}: tab '{tlabel}' does not leave the dashboard",
                                group=f"ux:{page}:{vp}:tab-exit-dead",
                                group_title=f"{label} @ {vp}: exit tabs that do not leave the dashboard",
                                item=tlabel), vp)
                else:
                    add(problem(f"ux:{page}:{vp}:tab-dead:{tid}", P0, "ux",
                                f"{label} @ {vp}: tab '{tlabel}' does not open its panel",
                                group=f"ux:{page}:{vp}:tab-dead",
                                group_title=f"{label} @ {vp}: tabs that do not open their panel",
                                item=tlabel), vp)
            if t.get("error"):
                add(problem(f"ux:{page}:{vp}:tab-error:{tid}", P2, "ux",
                            f"{label} @ {vp}: audit error on tab '{tlabel}' — {_short(t['error'], 100)}"), vp)
            tov = t.get("overflow") or {}
            if vp == "phone" and tov.get("overflow"):
                add(problem(f"ux:{page}:phone:overflow:{tid}", P2, "ux",
                            f"{label} @ phone: tab '{tlabel}' overflows 390px ({tov.get('scroll_width')}px)",
                            group=f"ux:{page}:phone:overflow-tabs",
                            group_title=f"{label} @ phone: tabs wider than 390px", item=tlabel), vp)
    out.extend(merged.values())
    if skipped:
        out.append(problem("audit:ux-budget", P2, "audit",
                           f"UX audit ran out of time; not checked: {', '.join(skipped)}"))
    return out


def tab_count_problems(ux: dict | None, previous: dict | None) -> list[dict]:
    """Fewer tabs than yesterday means tabs vanished — or moved somewhere the
    audit no longer looks, which would silently shrink what it checks."""
    prev_ux = (previous or {}).get("ux") or {}
    if not isinstance(ux, dict) or not prev_ux:
        return []

    def counts(doc: dict) -> dict[tuple, int]:
        return {(r.get("page"), r.get("viewport")): len(r.get("tabs") or [])
                for r in doc.get("pages") or []
                if r.get("page") in TABBED_PAGES and not r.get("skipped") and not r.get("error")}
    before, now = counts(prev_ux), counts(ux)
    out = []
    for (page, vp), n in sorted(now.items()):
        was = before.get((page, vp))
        if was and 0 < n < was:
            out.append(problem(f"ux:{page}:{vp}:tab-count", P1, "ux",
                               f"{PAGE_LABELS.get(page, page)} @ {vp}: {n} tabs found, {was} yesterday",
                               "a tab was removed, or moved out of `.tabs [data-tab]` where the audit looks"))
    return out


# ---------------------------------------------------------------------------
# regressions
# ---------------------------------------------------------------------------

def unverified_prefixes(data: dict | None, ux: dict | None) -> list[str]:
    """Problem-id prefixes whose section did not run today."""
    out: list[str] = []
    for prefix, section in SECTION_OF_PREFIX.items():
        if section == "ux":
            if not isinstance(ux, dict) or ux.get("fatal"):
                out.append("ux:")
                continue
            for rec in ux.get("pages") or []:
                if rec.get("skipped"):
                    out.append(f"ux:{rec.get('page')}:")
            continue
        sec = data.get(section) if isinstance(data, dict) else None
        if not _section_ok(sec):
            out.append(prefix)
    return out


def diff_problems(current: list[dict], previous: dict | None, today: str,
                  unverified: list[str] | None = None) -> dict:
    """Annotate ``current`` in place with state/first_seen; return the diff."""
    prev_list = (previous or {}).get("problems") or []
    prev_date = (previous or {}).get("date")
    prev_by_id = {p["id"]: p for p in prev_list if "id" in p}
    cur_ids = {p["id"] for p in current}
    new, ongoing = [], []
    for p in current:
        old = prev_by_id.get(p["id"])
        if old is None:
            p["state"] = "new"
            p["first_seen"] = today
            new.append(p["id"])
        else:
            p["state"] = "ongoing"
            p["first_seen"] = old.get("first_seen") or prev_date or today
            ongoing.append(p["id"])
    fixed, carried = [], []
    for pid, old in prev_by_id.items():
        if pid in cur_ids:
            continue
        if any(pid.startswith(pre) for pre in (unverified or [])):
            c = dict(old)
            c["state"] = "ongoing"
            c["unverified"] = True
            c["first_seen"] = old.get("first_seen") or prev_date or today
            current.append(c)
            carried.append(pid)
        else:
            f = dict(old)
            f["state"] = "fixed"
            fixed.append(f)
    return {"previous_date": prev_date, "new": new, "ongoing": ongoing + carried,
            "unverified": carried, "fixed": fixed}


def alert_action(problems: list[dict]) -> str:
    p0 = [p for p in problems if p["severity"] == P0]
    if any(p.get("state") == "new" for p in p0):
        return "open"
    return "update" if p0 else "close"


def summarize(problems: list[dict], diff: dict) -> dict:
    count = lambda sev: sum(1 for p in problems if p["severity"] == sev)  # noqa: E731
    p0, p1, p2 = count(P0), count(P1), count(P2)
    status = "RED" if p0 else ("AMBER" if p1 else "GREEN")
    return {
        "status": status, "p0": p0, "p1": p1, "p2": p2,
        "new": len(diff["new"]),
        "new_p0": sum(1 for p in problems if p["severity"] == P0 and p.get("state") == "new"),
        "ongoing": len(diff["ongoing"]), "fixed": len(diff["fixed"]),
        "unverified": len(diff.get("unverified") or []),
    }


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def _sorted(problems: list[dict]) -> list[dict]:
    return sorted(problems, key=lambda p: (SEV_ORDER.get(p["severity"], 9), p.get("category", ""),
                                           p.get("group") or p["id"], p["id"]))


def render_problem_lines(problems: list[dict], show_since: bool = False) -> list[str]:
    lines: list[str] = []
    seen_groups: set[str] = set()
    for p in _sorted(problems):
        g = p.get("group")
        if g:
            if g in seen_groups:
                continue
            seen_groups.add(g)
            members = [q for q in problems if q.get("group") == g]
            sev = min((q["severity"] for q in members), key=lambda s: SEV_ORDER.get(s, 9))
            items = ", ".join(q.get("item") or q["id"] for q in members)
            line = f"- **{sev}** [{p['category']}] {p.get('group_title') or g} ({len(members)}): {items}"
        else:
            line = f"- **{p['severity']}** [{p['category']}] {p['title']}"
            if p.get("detail"):
                line += f" — {_short(p['detail'], 200)}"
            if p.get("url"):
                line += f" ([run]({p['url']}))"
        if show_since and p.get("first_seen"):
            line += f" _(since {p['first_seen']})_"
        if p.get("unverified"):
            line += " _(not re-checked today)_"
        lines.append(line)
    return lines


def _table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    def cell(v: Any) -> str:
        return str("" if v is None else v).replace("|", "\\|").replace("\n", " ")
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(cell(c) for c in r) + " |" for r in rows]
    return out


def _prior_probe_line(api: dict) -> str | None:
    """One line under the ~24h diff for sources that failed the latest probe.

    The ~24h baseline is what made USGS ScienceBase on 2026-10-23 read as
    ``up → down``. The probe before the one the audit graded was already
    ``degraded``.
    """
    prior = api.get("prior_probe")
    if not isinstance(prior, dict) or not isinstance(prior.get("verdicts"), dict):
        return None
    streaks = api.get("streaks") if isinstance(api.get("streaks"), dict) else {}
    prior_streaks = prior.get("streaks") if isinstance(prior.get("streaks"), dict) else {}
    bits: list[str] = []
    for label, verdict in sorted((api.get("verdicts") or {}).items()):
        old = prior["verdicts"].get(label)
        if verdict not in ("down", "degraded"):
            continue
        if verdict != "down" and old == verdict:
            continue
        streak = streaks.get(label)
        bit = f"{label}: {old} → {verdict}"
        if type(streak) is int:
            word = "failure" if streak == 1 else "failures"
            bit += f" ({streak} consecutive probe {word})"
        old_streak = prior_streaks.get(label)
        if type(old_streak) is int and old != verdict:
            bit += f"; previous probe had {old_streak}"
        bits.append(bit)
    if not bits:
        return None
    return (f"  - prior probe {prior.get('source')} ({prior.get('generated_at')}): "
            + "; ".join(bits[:8]))


def render_md(report: dict) -> str:
    s = report["summary"]
    reg = report["regressions"]
    problems = report["problems"]
    data = report.get("data") or {}
    ux = report.get("ux") or {}
    L: list[str] = [f"# Daily audit {report['date']} — {s['status']}", ""]
    L.append(f"**{s['status']}** · {s['p0']} P0 · {s['p1']} P1 · {s['p2']} P2 · "
             f"{s['new']} new · {s['fixed']} fixed"
             + (f" (vs. {reg['previous_date']})" if reg.get("previous_date") else " (no previous report)"))
    L.append("")
    meta = f"Generated {report['generated_at']}"
    if report.get("run_url"):
        meta += f" by [daily-audit]({report['run_url']})"
    L += [meta + ". Machine-readable: `audit/daily/" + report["date"] + ".json`.", ""]
    if report.get("schedule_note"):
        # Not a finding. Late or skipped crons whose data is still fresh.
        L += [str(report["schedule_note"]), ""]

    new = [p for p in problems if p.get("state") == "new"]
    ongoing = [p for p in problems if p.get("state") == "ongoing"]
    L.append(f"## New since yesterday ({len(new)})")
    L += render_problem_lines(new) or ["- none"]
    L += ["", f"## Ongoing ({len(ongoing)})"]
    L += render_problem_lines(ongoing, show_since=True) or ["- none"]
    L += ["", f"## Fixed since yesterday ({len(reg['fixed'])})"]
    L += render_problem_lines(reg["fixed"]) or ["- none"]

    # ---- compact snapshot ---------------------------------------------------
    # Accepted quality-audit gaps follow the verdict recorded in this day's
    # data JSON, not a live re-read and not a stale Actions failure.
    quality_name, quality_ok = _quality_acceptance(data)
    L += ["", "## Snapshot", ""]
    qa = data.get("quality_audit") if isinstance(data, dict) else None
    if isinstance(qa, dict) and (qa.get("overall_verdict") or qa.get("known_gaps")):
        bits = []
        for g in qa.get("known_gaps") or []:
            if not isinstance(g, dict) or not g.get("pillar"):
                continue
            label = g.get("verdict") or "KNOWN GAP (accepted)"
            when = g.get("decided") or "?"
            review = f", review by {g['review_by']}" if g.get("review_by") else ""
            bits.append(f"{g['pillar']} is {label} (decided {when}{review})")
        line = f"- **LTHCS quality audit** ({qa.get('summary') or 'latest_summary.json'}): {qa.get('overall_verdict') or '?'}"
        if bits:
            line += ". " + "; ".join(bits)
        L.append(line)
    lp = data.get("lthcs_pillars") if isinstance(data, dict) else None
    if isinstance(lp, dict) and lp.get("unaccepted_stubs"):
        bits = []
        for row in lp["unaccepted_stubs"]:
            if isinstance(row, dict) and row.get("pillar"):
                bits.append(f"{row['pillar']} ({row.get('coverage_pct')}% off 50.0)")
        accepted = ", ".join(lp.get("accepted_pillars") or []) or "none"
        L.append(
            f"- **LTHCS pillars** ({lp.get('snapshot_date')}): unaccepted STUB "
            + ", ".join(bits)
            + f". Accepted stub: {accepted}."
        )
    dh = data.get("data_health") or {}
    if _section_ok(dh) and dh:
        counts = ", ".join(f"{v} {k}" for k, v in sorted((dh.get("counts") or {}).items()))
        L.append(f"- **Feeds** (data_health, committed): {counts or 'n/a'}")
        muted = [r for r in dh.get("results") or [] if r.get("status") == "suppressed"]
        if muted:
            L.append("  - muted: " + "; ".join(f"{r['path']} ({_short(r.get('detail'), 70)})" for r in muted))
    wf = data.get("workflows") or {}
    if _section_ok(wf) and wf:
        ran = [w for w in wf.get("workflows") or [] if w.get("latest_24h")]
        bad = [w["name"] for w in ran
               if w.get("failed_24h")
               and not (w.get("name") == quality_name and quality_ok)
               and not _only_soft_etf_warning(w)]
        L.append(f"- **Workflows** (last 24h, main): {len(ran)} ran, {len(bad)} ended failed"
                 + (f" ({', '.join(bad)})" if bad else ""))
        soft = [w["name"] for w in ran
                if _soft_warning_annotations(w)
                and not (w.get("failed_24h") and not _only_soft_etf_warning(w))]
        if soft:
            L.append("  - soft warning (not a failure): " + ", ".join(soft))
    api = data.get("api_status") or {}
    if _section_ok(api) and api:
        counts = ", ".join(f"{v} {k}" for k, v in sorted((api.get("counts") or {}).items()))
        ch = report.get("api_changes") or []
        L.append(f"- **API status** ({api.get('generated_at')}): {counts}")
        if not report.get("api_baseline"):
            L.append("  - no snapshot from ~24h earlier to compare against yet")
        elif ch:
            L.append(f"  - changed vs. {report['api_baseline']}: " + "; ".join(
                f"{c['source']}: {c.get('from')} → {c.get('to')}" for c in ch[:12]))
        else:
            L.append(f"  - no verdict changes vs. {report['api_baseline']}")
        prior_line = _prior_probe_line(api)
        if prior_line:
            L.append(prior_line)
    spot = data.get("spot_checks") or {}
    if _section_ok(spot) and spot:
        L += ["", "**Spot checks** (live site vs. primary sources)", ""]
        rows = []
        for name, c in (spot.get("checks") or {}).items():
            detail = c.get("error") or c.get("note") or ""
            if "diff_pct" in c:
                detail = f"site {c.get('page')} vs {c.get('reference')} ({c.get('diff_pct')}%)"
                if c.get("page_age_h") is not None:
                    detail += f", payload {_hours(c.get('page_age_h'))} old"
                if c.get("reason"):
                    detail += f", {c['reason']}"
                if c.get("source"):
                    detail += f", source {c['source']}"
            elif c.get("reason"):
                detail = (f"{c['reason']}: payload {_hours(c.get('page_age_h'))} old "
                          f"(limit {_hours(c.get('max_age_h'))}), source {c.get('source')}")
            elif "lag_blocks" in c:
                detail = f"site {c.get('page')} vs {c.get('reference')} (lag {c.get('lag_blocks')} blocks)"
            elif "age_h" in c:
                detail = f"built {c.get('generated_at')} ({_hours(c.get('age_h'))} ago)"
            elif "page_date" in c:
                detail = (f"{c.get('page_date')}: site {c.get('page')} vs {c.get('reference')}"
                          + (f" — {c['note']}" if c.get("note") else ""))
            rows.append([name, c.get("status"), _short(detail, 110)])
        L += _table(["check", "status", "detail"], rows)
    live = data.get("live_payloads") or {}
    if _section_ok(live) and live:
        idx = live.get("index") or {}
        allp = [idx] + list(live.get("payloads") or [])
        # Only the payloads worth a look: broken, unstamped, or a stamp older
        # than STAMP_NOTE_H. The JSON keeps every row.
        notable = [p for p in allp if p.get("status", "ok") != "ok"
                   or not (p.get("as_of") or p.get("generated"))
                   or max(p.get("as_of_age_h") or 0, p.get("generated_age_h") or 0) > STAMP_NOTE_H]
        L += ["", f"**Live payload stamps**: {len(allp) - len(notable)} of {len(allp)} stamped "
              f"within {STAMP_NOTE_H}h" + (", the rest:" if notable else ".")]
        if notable:
            L.append("")
            rows = [[p.get("name"), p.get("status", "ok"),
                     f"{(p.get('as_of') or '')[:10]} ({_hours(p.get('as_of_age_h'))})" if p.get("as_of") else "–",
                     f"{(p.get('generated') or '')[:16]} ({_hours(p.get('generated_age_h'))})" if p.get("generated") else "–"]
                    for p in notable]
            L += _table(["payload", "status", "data as_of", "generated"], rows)

    sc = data.get("schedules") or {}
    L += ["", "## Schedules (last 7 days)", ""]
    if _section_ok(sc) and sc:
        rows = []
        any_catchup = False
        for x in sc.get("schedules") or []:
            last = x.get("last_run") or {}
            flags = []
            if x.get("missed_days"):
                flags.append(f"MISSED {len(x['missed_days'])}d")
            recovered = x.get("recovered_run") or {}
            if x.get("last_failed") and not (x.get("name") == quality_name and quality_ok):
                flags.append("FAILING")
            elif recovered.get("created_at"):
                # Last scheduled run failed; a later success cleared it.
                flags.append("recovered")
            if x.get("state") and x.get("state") != "active":
                flags.append(str(x["state"]).upper())
            catchup_ticks = x.get("catchup_ticks") or 0
            if catchup_ticks:
                any_catchup = True
                flags.append(f"catch-up {catchup_ticks}")
            eff = x.get("coverage_pct")
            sched_cov = x.get("schedule_coverage_pct")
            if eff is None:
                ticks = "–"
            elif sched_cov is not None and abs(float(sched_cov) - float(eff)) >= 0.05:
                ticks = f"{eff:.0f}% (scheduler {sched_cov:.0f}%)"
            else:
                ticks = f"{eff:.0f}%"
            delay = x.get("median_delay_min")
            sched_delay = x.get("schedule_median_delay_min")
            if delay is None:
                delay_cell = "–"
            elif sched_delay is not None and abs(float(sched_delay) - float(delay)) >= 30:
                delay_cell = f"{delay / 60:.1f}h (scheduler {float(sched_delay) / 60:.1f}h)"
            else:
                delay_cell = f"{delay / 60:.1f}h"
            last_cell = f"{(last.get('created_at') or '')[:16]} {last.get('conclusion') or ''}".strip() or "never"
            caught = x.get("last_catchup") or {}
            if caught.get("created_at"):
                any_catchup = True
                last_cell += (f"; catch-up {(caught.get('created_at') or '')[:16]} "
                              f"{caught.get('conclusion') or ''}").rstrip()
            if recovered.get("created_at"):
                last_cell += (f"; recovered {(recovered.get('created_at') or '')[:16]} "
                              f"{recovered.get('conclusion') or ''} "
                              f"({recovered.get('event') or 'run'})").rstrip()
            rows.append([
                x.get("name"), " ; ".join(x.get("crons") or []),
                f"{x.get('days_with_run')}/{x.get('due_days')}",
                ticks, delay_cell, last_cell,
                ", ".join(flags) or "ok",
            ])
        L += _table(["workflow", "cron (UTC)", "days run/due", "ticks run", "median delay",
                     "last scheduled run", "flag"], rows)
        if any_catchup:
            # One sentence, split for line length. + keeps the join explicit
            # so these stay a single list element.
            L += ["",
                  ("A catch-up is a `workflow_dispatch` by `github-actions[bot]` (the cron "
                   + "watchdog), not a GitHub schedule event. Ticks run and median delay count "
                   + "it toward freshness; the scheduler figure is what the schedule event "
                   + "delivered on its own.")]
    else:
        L.append(f"- could not be read: {sc.get('error', 'no data')}")

    L += ["", "## UX (live site)", ""]
    if ux.get("fatal"):
        L.append(f"- UX audit could not run: {_short(ux['fatal'], 200)}")
    rows = []
    for rec in ux.get("pages") or []:
        if rec.get("skipped"):
            rows.append([rec.get("path"), rec.get("viewport"), "skipped", "", "", "", "", "", ""])
            continue
        w = rec.get("weight") or {}
        tabs = [t for t in rec.get("tabs") or [] if not t.get("skipped")]
        reach = (f"{sum(1 for t in tabs if t.get('reachable'))}/{len(tabs)}" if tabs else "–")
        rows.append([
            rec.get("path"), rec.get("viewport"), rec.get("status") or (rec.get("error") or "")[:30],
            f"{(w.get('bytes') or 0) / 1e6:.1f} MB / {w.get('requests', 0)}",
            len(rec.get("page_errors") or []), len(rec.get("console_errors") or []),
            len(rec.get("failed_requests") or []),
            "yes" if (rec.get("overflow") or {}).get("overflow") else "no",
            reach,
        ])
    if rows:
        L += _table(["page", "viewport", "HTTP", "weight / requests", "JS exceptions",
                     "console errors", "failed requests", "overflow", "tabs tappable"], rows)
        if ux.get("duration_s") is not None:
            L += ["", f"UX audit took {ux['duration_s']}s."]

    couldnt = [f"{name}: {c.get('error')}" for name, c in ((data.get("spot_checks") or {}).get("checks") or {}).items()
               if c.get("status") == "error"]
    if couldnt:
        L += ["", "## Checks that could not run", ""] + [f"- {_short(x, 200)}" for x in couldnt]
    return "\n".join(L).rstrip() + "\n"


def render_issue(report: dict) -> str:
    md = render_md(report)
    return (md + "\n---\nOpened by `.github/workflows/daily-audit.yml` when the daily audit finds a NEW P0 "
            "problem; edited in place while P0s persist and closed automatically once none remain.\n")


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------

def load_json(path: Path | None) -> Any:
    if not path:
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def find_previous(out_dir: Path, today: str) -> dict | None:
    """The newest dated report strictly before ``today``."""
    best: tuple[str, Path] | None = None
    if out_dir.is_dir():
        for f in out_dir.iterdir():
            m = DATED_RE.match(f.name)
            if m and m.group(2) == "json" and m.group(1) < today:
                if best is None or m.group(1) > best[0]:
                    best = (m.group(1), f)
    return load_json(best[1]) if best else None


def prune(out_dir: Path, today: str, keep_days: int) -> list[str]:
    cutoff = (date.fromisoformat(today) - timedelta(days=keep_days)).isoformat()
    removed = []
    if out_dir.is_dir():
        for f in sorted(out_dir.iterdir()):
            m = DATED_RE.match(f.name)
            if m and m.group(1) < cutoff:
                f.unlink()
                removed.append(f.name)
    return removed


def build_report(data: dict | None, ux: dict | None, previous: dict | None, today: str,
                 now: datetime | None = None, run_url: str | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    problems = data_problems(data) + ux_problems(ux) + tab_count_problems(ux, previous)
    # Stable ids must be unique; keep the first occurrence.
    seen: set[str] = set()
    problems = [p for p in problems if not (p["id"] in seen or seen.add(p["id"]))]
    diff = diff_problems(problems, previous, today, unverified_prefixes(data, ux))
    problems = _sorted(problems)
    # Without a git copy of yesterday's api_status, diff against yesterday's report.
    api_changes: list[dict] = []
    api_baseline = None
    api = (data or {}).get("api_status") if isinstance(data, dict) else None
    if isinstance(api, dict) and _section_ok(api):
        if api.get("previous"):
            api_baseline = (f"{api['previous'].get('source')} "
                            f"({api['previous'].get('generated_at')})")
            api_changes = list(api.get("changes") or [])
        else:
            prev_api = ((previous or {}).get("data") or {}).get("api_status") or {}
            if prev_api.get("verdicts"):
                api_baseline = f"report {previous.get('date')} ({prev_api.get('generated_at')})"
                cur, old = api.get("verdicts") or {}, prev_api["verdicts"]
                api_changes = [{"source": k, "from": old.get(k), "to": cur.get(k)}
                               for k in sorted(set(old) | set(cur)) if old.get(k) != cur.get(k)]
    report = {
        "schema": 1,
        "date": today,
        "generated_at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_url": run_url,
        "summary": summarize(problems, diff),
        "alert_action": alert_action(problems),
        "problems": problems,
        "regressions": diff,
        "api_changes": api_changes,
        "api_baseline": api_baseline,
        "schedule_note": schedule_timing_note(data) if isinstance(data, dict) else None,
        "data": data,
        "ux": ux,
    }
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", type=Path, help="output of daily_audit_data.py")
    ap.add_argument("--ux", type=Path, help="output of daily_audit_ux.mjs")
    ap.add_argument("--out-dir", type=Path, default=Path("audit/daily"))
    ap.add_argument("--date", help="report date (YYYY-MM-DD, default: today UTC)")
    ap.add_argument("--previous", type=Path, help="override: previous report JSON")
    ap.add_argument("--keep-days", type=int, default=90)
    ap.add_argument("--issue-body", type=Path, help="also write the issue body here")
    ap.add_argument("--run-url", default=None)
    args = ap.parse_args(argv)

    now = datetime.now(timezone.utc)
    today = args.date or now.date().isoformat()
    data, ux = load_json(args.data), load_json(args.ux)
    previous = load_json(args.previous) if args.previous else find_previous(args.out_dir, today)
    report = build_report(data, ux, previous, today, now, args.run_url)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    js = json.dumps(report, indent=1, ensure_ascii=False, default=str) + "\n"
    md = render_md(report)
    for name, text in ((f"{today}.json", js), (f"{today}.md", md),
                       ("latest.json", js), ("latest.md", md)):
        (args.out_dir / name).write_text(text, encoding="utf-8")
    removed = prune(args.out_dir, today, args.keep_days)
    if args.issue_body:
        args.issue_body.write_text(render_issue(report), encoding="utf-8")

    s = report["summary"]
    print(f"[audit-report] {today}: {s['status']} — {s['p0']} P0 ({s['new_p0']} new), {s['p1']} P1, "
          f"{s['p2']} P2; {s['new']} new, {s['fixed']} fixed; alert={report['alert_action']}"
          + (f"; pruned {len(removed)}" if removed else ""), file=sys.stderr)
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as fh:
            fh.write(f"status={s['status']}\nalert_action={report['alert_action']}\n"
                     f"p0={s['p0']}\nnew_p0={s['new_p0']}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
