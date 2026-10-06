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
        whose last scheduled run failed, a spot check that disagrees with its
        primary source, console errors, failed same-origin requests, visible
        NaN/undefined text, an upstream API down
    P2  layout overflow at 390px, heavy pages, chronically late/dropped cron
        ticks, degraded/blocked upstreams

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

PAGE_LABELS = {
    "v1": "V1 (/)", "summit": "/summit/", "health": "/health/",
    "real-estate": "/real-estate/", "lthcs": "/lthcs/",
}

# Thresholds for the P2 "heavy page" flag (initial load, encoded bytes).
HEAVY_BYTES = 10_000_000
HEAVY_REQUESTS = 200
# Schedules: P2 when a cron's median start delay exceeds this, or a
# several-times-a-day cron gets fewer than this share of its ticks.
LATE_MINUTES = 180
MIN_COVERAGE_PCT = 50.0
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

# Problem-id prefix -> the section whose failure means "not re-checked today".
SECTION_OF_PREFIX = {
    "feed:": "data_health",
    "workflow:": "workflows",
    "schedule:": "schedules",
    "api:": "api_status",
    "spot:": "spot_checks",
    "live:": "live_payloads",
    "ux:": "ux",
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

def _best_annotation(jobs: list[dict]) -> str:
    """The most informative line from a failed run's annotations."""
    generic = re.compile(r"^Process completed with exit code \d+\.?$")
    noise = re.compile(r"Node\.js \d+ (is deprecated|actions are deprecated)", re.I)
    lines: list[str] = []
    for j in jobs or []:
        steps = ", ".join(j.get("failed_steps") or [])
        anns = [a for a in (j.get("annotations") or []) if not noise.search(a.get("message") or "")]
        specific = [a for a in anns if not generic.match((a.get("message") or "").strip())]
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
        for r in dh.get("extra_failing") or []:
            name = r.get("path") or r.get("name") or r.get("feed") or "?"
            out.append(problem(
                f"feed:{r.get('section')}:{name}", P0, "feed",
                f"{r.get('section')}: {name} is {str(r.get('status')).upper()}",
                r.get("detail") or r.get("message") or ""))

    # -- workflows -------------------------------------------------------------
    wf = data.get("workflows")
    flagged_workflows: set[str] = set()
    if wf is not None and not _section_ok(wf):
        out.append(problem("audit:workflows", P1, "audit",
                           "Workflow run history could not be read", wf.get("error", "")))
    elif wf:
        for w in wf.get("workflows") or []:
            name = w.get("name")
            latest = w.get("latest_24h") or {}
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

    # -- schedules -------------------------------------------------------------
    sc = data.get("schedules")
    if sc is not None and not _section_ok(sc):
        out.append(problem("audit:schedules", P1, "audit",
                           "Scheduled-workflow history could not be read", sc.get("error", "")))
    elif sc:
        for s in sc.get("schedules") or []:
            name = s.get("name")
            crons = ", ".join(s.get("crons") or [])
            if s.get("state") and s.get("state") != "active":
                out.append(problem(f"schedule:{name}:disabled", P1, "schedule",
                                   f"Scheduled workflow {name} is {s.get('state')} — its cron ({crons}) will not fire"))
            if s.get("missed_days"):
                out.append(problem(
                    f"schedule:{name}:missed", P1, "schedule",
                    f"Scheduled workflow {name} missed {len(s['missed_days'])} day(s) in the last week",
                    f"cron {crons}; no scheduled run on: {', '.join(s['missed_days'])}"))
            last = s.get("last_run") or {}
            if s.get("last_failed") and name not in flagged_workflows:
                out.append(problem(
                    f"schedule:{name}:failing", P1, "schedule",
                    f"Scheduled workflow {name}: last scheduled run failed ({(last.get('created_at') or '?')[:10]})",
                    f"cron {crons}", url=last.get("url")))
            cov = s.get("coverage_pct")
            if cov is not None and (s.get("due_runs") or 0) > (s.get("due_days") or 0) and cov < MIN_COVERAGE_PCT:
                out.append(problem(
                    f"schedule:{name}:dropped", P2, "schedule", f"{name}: {cov:.0f}% of cron ticks ran",
                    f"cron {crons}; {s.get('scheduled_runs')} scheduled runs for {s.get('due_runs')} due ticks",
                    group="schedule:dropped", group_title="Crons that lose most of their ticks",
                    item=f"{name} {cov:.0f}%"))
            delay = s.get("median_delay_min")
            if delay is not None and delay > LATE_MINUTES:
                out.append(problem(
                    f"schedule:{name}:late", P2, "schedule",
                    f"{name} starts ~{delay / 60:.1f}h after its cron time (median)", f"cron {crons}",
                    group="schedule:late",
                    group_title=f"Crons starting >{LATE_MINUTES // 60}h late (median delay)",
                    item=f"{name} {delay / 60:.1f}h"))

    # -- API status --------------------------------------------------------------
    api = data.get("api_status")
    if api is not None and not _section_ok(api):
        out.append(problem("audit:api_status", P2, "audit",
                           "data/health/api_status.json could not be read", api.get("error", "")))
    elif api:
        for label, verdict in sorted((api.get("verdicts") or {}).items()):
            if verdict == "down":
                out.append(problem(f"api:{label}", P1, "api", f"Upstream API {label} is DOWN"))
            elif verdict in ("blocked", "degraded"):
                out.append(problem(f"api:{label}", P2, "api", f"Upstream API {label} is {verdict}",
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
    return out


def spot_title(name: str, c: dict) -> str:
    if name == "live_site_build_age":
        return (f"Live site last rebuilt {_hours(c.get('age_h'))} ago "
                f"(limit {_hours(c.get('max_age_h'))}) — pages deploy may be stuck")
    if "price" in name:
        coin = name.split("_")[0].upper()
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

    new = [p for p in problems if p.get("state") == "new"]
    ongoing = [p for p in problems if p.get("state") == "ongoing"]
    L.append(f"## New since yesterday ({len(new)})")
    L += render_problem_lines(new) or ["- none"]
    L += ["", f"## Ongoing ({len(ongoing)})"]
    L += render_problem_lines(ongoing, show_since=True) or ["- none"]
    L += ["", f"## Fixed since yesterday ({len(reg['fixed'])})"]
    L += render_problem_lines(reg["fixed"]) or ["- none"]

    # ---- compact snapshot ---------------------------------------------------
    L += ["", "## Snapshot", ""]
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
        bad = [w["name"] for w in ran if w.get("failed_24h")]
        L.append(f"- **Workflows** (last 24h, main): {len(ran)} ran, {len(bad)} ended failed"
                 + (f" ({', '.join(bad)})" if bad else ""))
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
    spot = data.get("spot_checks") or {}
    if _section_ok(spot) and spot:
        L += ["", "**Spot checks** (live site vs. primary sources)", ""]
        rows = []
        for name, c in (spot.get("checks") or {}).items():
            detail = c.get("error") or c.get("note") or ""
            if "diff_pct" in c:
                detail = f"site {c.get('page')} vs {c.get('reference')} ({c.get('diff_pct')}%)"
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
        for x in sc.get("schedules") or []:
            last = x.get("last_run") or {}
            flags = []
            if x.get("missed_days"):
                flags.append(f"MISSED {len(x['missed_days'])}d")
            if x.get("last_failed"):
                flags.append("FAILING")
            if x.get("state") and x.get("state") != "active":
                flags.append(str(x["state"]).upper())
            delay = x.get("median_delay_min")
            rows.append([
                x.get("name"), " ; ".join(x.get("crons") or []),
                f"{x.get('days_with_run')}/{x.get('due_days')}",
                f"{x.get('coverage_pct'):.0f}%" if x.get("coverage_pct") is not None else "–",
                f"{delay / 60:.1f}h" if delay is not None else "–",
                f"{(last.get('created_at') or '')[:16]} {last.get('conclusion') or ''}".strip() or "never",
                ", ".join(flags) or "ok",
            ])
        L += _table(["workflow", "cron (UTC)", "days run/due", "ticks run", "median delay",
                     "last scheduled run", "flag"], rows)
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
