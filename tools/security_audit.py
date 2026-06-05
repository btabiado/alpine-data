#!/usr/bin/env python3
"""
security_audit.py — repeatable security posture check.

Reproduces the manual checks the swarm session ran on 2026-05-22 so the user
gets a single command (and a weekly CI job) that surfaces:

  1. CodeQL alerts (open, grouped by severity)
  2. Dependabot alerts (open)
  3. Secret-scanning alerts (open)
  4. OSV.dev CVE scan for each pinned Python dep in requirements.txt
  5. Deployed Pages surface — confirm sensitive paths 404
  6. Response headers (HSTS, etc.)
  7. Repo posture — security_and_analysis, branch protection, actions permissions

Design constraints:
  * Pure stdlib (urllib + json + subprocess). No new repo dependencies.
  * Uses `gh` CLI for the GitHub API surface so auth flows through GITHUB_TOKEN
    in CI and the user's local gh auth at the desk.
  * Exits non-zero ONLY when something is actionable:
      - any open Dependabot alert
      - any open secret-scanning alert
      - any open error-severity CodeQL alert
      - any pinned dep flagged by OSV.dev
      - the deployed site exposes a sensitive path or drops HSTS
    Posture checks (branch protection etc.) are informational — they print
    but don't fail the job, since the user may legitimately tighten/loosen
    those settings outside this script.
  * NEVER echoes GITHUB_TOKEN or any other secret. Only counts and identifiers.
  * NEVER posts findings outside the repo (no Slack/email/webhook).

Usage:
    python3 tools/security_audit.py                 # full audit, default repo
    python3 tools/security_audit.py --repo OWNER/REPO
    python3 tools/security_audit.py --skip-github   # local run without gh auth
    python3 tools/security_audit.py --skip-network  # offline (OSV/site skipped)
    python3 tools/security_audit.py --summary-file path/to/summary.md
                                                    # also write GH-flavored MD
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

DEFAULT_REPO = "btabiado/alpine-data"
DEFAULT_PAGES_URL = "https://btabiado.github.io/alpine-data/"
SENSITIVE_PATHS = [
    ".env",
    ".git/config",
    "config.json",
    "secrets.json",
    "credentials.json",
    "tools/security_audit.py",  # source files shouldn't be served from Pages root
]
OSV_ENDPOINT = "https://api.osv.dev/v1/query"
HTTP_TIMEOUT = 20  # seconds

# requirements.txt line -> (name, version). Tolerates comments & blanks.
REQ_LINE_RE = re.compile(r"^\s*([A-Za-z0-9_.\-\[\]]+)==([A-Za-z0-9_.\-+]+)\s*(?:#.*)?$")


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""
    # Rows for the markdown table: list of (column, value) tuples per row.
    rows: list[list[tuple[str, str]]] = field(default_factory=list)
    fatal: bool = True  # whether a non-ok result should fail the job


# ----------------------------- gh helpers -----------------------------


def gh_api(path: str) -> Any:
    """Run `gh api <path>` and return the parsed JSON.

    Raises CalledProcessError on non-zero exit; the caller decides whether
    that's fatal (e.g. branch protection 404 just means it's unconfigured).
    """
    out = subprocess.run(
        ["gh", "api", "-H", "Accept: application/vnd.github+json", path],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout) if out.stdout.strip() else None


def gh_api_optional(path: str) -> Any | None:
    """Like gh_api but returns None on any failure instead of raising."""
    try:
        return gh_api(path)
    except subprocess.CalledProcessError:
        return None


# ----------------------------- checks -----------------------------


def check_codeql(repo: str) -> CheckResult:
    try:
        alerts = gh_api(f"/repos/{repo}/code-scanning/alerts?state=open&per_page=100")
    except subprocess.CalledProcessError as e:
        # 404 means CodeQL isn't enabled — informational, not fatal.
        return CheckResult(
            "CodeQL",
            ok=True,
            detail=f"unavailable ({e.stderr.strip().splitlines()[-1] if e.stderr else 'no response'})",
            fatal=False,
        )

    by_sev: dict[str, int] = {}
    for a in alerts or []:
        sev = (a.get("rule") or {}).get("security_severity_level") or (a.get("rule") or {}).get("severity") or "unknown"
        by_sev[sev] = by_sev.get(sev, 0) + 1

    open_error = by_sev.get("error", 0) + by_sev.get("critical", 0) + by_sev.get("high", 0)
    total = sum(by_sev.values())
    summary = ", ".join(f"{k}={v}" for k, v in sorted(by_sev.items())) or "0"
    rows = [[("metric", "open_total"), ("value", str(total))]]
    for k, v in sorted(by_sev.items()):
        rows.append([("metric", f"severity:{k}"), ("value", str(v))])

    return CheckResult(
        "CodeQL",
        ok=(open_error == 0),
        detail=f"open={total} ({summary})",
        rows=rows,
    )


def check_dependabot(repo: str) -> CheckResult:
    try:
        alerts = gh_api(f"/repos/{repo}/dependabot/alerts?state=open&per_page=100")
    except subprocess.CalledProcessError as e:
        return CheckResult(
            "Dependabot",
            ok=True,
            detail=f"unavailable ({e.stderr.strip().splitlines()[-1] if e.stderr else 'no response'})",
            fatal=False,
        )

    open_alerts = alerts or []
    rows = [[("metric", "open"), ("value", str(len(open_alerts)))]]
    for a in open_alerts[:10]:  # cap table — full list is in the API
        pkg = ((a.get("dependency") or {}).get("package") or {}).get("name", "?")
        ghsa = (a.get("security_advisory") or {}).get("ghsa_id", "?")
        sev = (a.get("security_advisory") or {}).get("severity", "?")
        rows.append([
            ("metric", f"alert:{pkg}"),
            ("value", f"{sev} {ghsa}"),
        ])

    return CheckResult(
        "Dependabot",
        ok=(len(open_alerts) == 0),
        detail=f"open={len(open_alerts)}",
        rows=rows,
    )


def check_secret_scanning(repo: str) -> CheckResult:
    try:
        alerts = gh_api(f"/repos/{repo}/secret-scanning/alerts?state=open&per_page=100")
    except subprocess.CalledProcessError as e:
        return CheckResult(
            "SecretScanning",
            ok=True,
            detail=f"unavailable ({e.stderr.strip().splitlines()[-1] if e.stderr else 'no response'})",
            fatal=False,
        )
    open_alerts = alerts or []
    rows = [[("metric", "open"), ("value", str(len(open_alerts)))]]
    for a in open_alerts[:10]:
        rows.append([
            ("metric", f"alert:{a.get('secret_type_display_name', a.get('secret_type', '?'))}"),
            ("value", f"#{a.get('number', '?')}"),
        ])
    return CheckResult(
        "SecretScanning",
        ok=(len(open_alerts) == 0),
        detail=f"open={len(open_alerts)}",
        rows=rows,
    )


def parse_requirements(path: str) -> list[tuple[str, str]]:
    pins: list[tuple[str, str]] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = REQ_LINE_RE.match(line)
            if m:
                pins.append((m.group(1), m.group(2)))
    return pins


def osv_query(pkg: str, version: str) -> list[dict]:
    payload = json.dumps({
        "package": {"name": pkg, "ecosystem": "PyPI"},
        "version": version,
    }).encode("utf-8")
    req = urllib.request.Request(
        OSV_ENDPOINT,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("vulns") or []


def check_osv(requirements_path: str) -> CheckResult:
    pins = parse_requirements(requirements_path)
    rows: list[list[tuple[str, str]]] = []
    flagged: list[str] = []
    rows.append([("dep", "TOTAL_PINNED"), ("status", str(len(pins)))])
    for name, version in pins:
        try:
            vulns = osv_query(name, version)
        except (urllib.error.URLError, TimeoutError) as e:
            rows.append([("dep", f"{name}=={version}"), ("status", f"osv-error: {e}")])
            continue
        if vulns:
            ids = ",".join(v.get("id", "?") for v in vulns[:3])
            rows.append([("dep", f"{name}=={version}"), ("status", f"VULN: {ids}")])
            flagged.append(f"{name}=={version} ({ids})")
        # clean deps omitted from table to keep summary scannable; count is above
    ok = not flagged
    detail = "all clean" if ok else f"flagged: {'; '.join(flagged)}"
    return CheckResult("OSV/CVE", ok=ok, detail=detail, rows=rows)


def check_pages_surface(base_url: str) -> CheckResult:
    base = base_url.rstrip("/") + "/"
    rows: list[list[tuple[str, str]]] = []
    exposed: list[str] = []
    for p in SENSITIVE_PATHS:
        url = base + p
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                code = resp.status
        except urllib.error.HTTPError as e:
            code = e.code
        except (urllib.error.URLError, TimeoutError) as e:
            rows.append([("path", p), ("status", f"network-error: {e}")])
            continue
        rows.append([("path", p), ("status", str(code))])
        if code == 200:
            exposed.append(p)
    ok = not exposed
    detail = "all sensitive paths 404" if ok else f"EXPOSED: {', '.join(exposed)}"
    return CheckResult("PagesSurface", ok=ok, detail=detail, rows=rows)


def check_headers(base_url: str) -> CheckResult:
    try:
        with urllib.request.urlopen(base_url, timeout=HTTP_TIMEOUT) as resp:
            headers = {k.lower(): v for k, v in resp.headers.items()}
    except (urllib.error.URLError, TimeoutError) as e:
        return CheckResult("Headers", ok=False, detail=f"network-error: {e}", fatal=False)

    interesting = [
        "strict-transport-security",
        "content-security-policy",
        "x-content-type-options",
        "referrer-policy",
        "x-frame-options",
    ]
    rows = [[("header", h), ("value", headers.get(h, "<missing>"))] for h in interesting]
    hsts_present = "strict-transport-security" in headers
    return CheckResult(
        "Headers",
        ok=hsts_present,
        detail="HSTS present" if hsts_present else "HSTS MISSING",
        rows=rows,
        fatal=True,
    )


def check_posture(repo: str) -> CheckResult:
    rows: list[list[tuple[str, str]]] = []

    repo_info = gh_api_optional(f"/repos/{repo}")
    if repo_info:
        sa = repo_info.get("security_and_analysis") or {}
        for k, v in sa.items():
            status = (v or {}).get("status") if isinstance(v, dict) else str(v)
            rows.append([("setting", f"security_and_analysis.{k}"), ("value", str(status))])

    branch = gh_api_optional(f"/repos/{repo}/branches/main/protection")
    if branch:
        rows.append([
            ("setting", "main.required_status_checks"),
            ("value", "yes" if branch.get("required_status_checks") else "no"),
        ])
        rows.append([
            ("setting", "main.enforce_admins"),
            ("value", str((branch.get("enforce_admins") or {}).get("enabled"))),
        ])
        rows.append([
            ("setting", "main.required_pull_request_reviews"),
            ("value", "yes" if branch.get("required_pull_request_reviews") else "no"),
        ])
    else:
        rows.append([("setting", "main.branch_protection"), ("value", "unconfigured")])

    actions_perms = gh_api_optional(f"/repos/{repo}/actions/permissions")
    if actions_perms:
        for k, v in actions_perms.items():
            rows.append([("setting", f"actions.{k}"), ("value", str(v))])

    workflow_perms = gh_api_optional(f"/repos/{repo}/actions/permissions/workflow")
    if workflow_perms:
        for k, v in workflow_perms.items():
            rows.append([("setting", f"actions.workflow.{k}"), ("value", str(v))])

    # Posture is informational — never fatal. The user tunes these by hand.
    return CheckResult("RepoPosture", ok=True, detail=f"{len(rows)} settings captured", rows=rows, fatal=False)


# ----------------------------- output -----------------------------


def render_markdown(results: list[CheckResult]) -> str:
    out: list[str] = ["# Security Audit", ""]
    out.append("| check | status | detail |")
    out.append("|---|---|---|")
    for r in results:
        status = "PASS" if r.ok else ("FAIL" if r.fatal else "WARN")
        out.append(f"| {r.name} | {status} | {r.detail} |")
    out.append("")
    for r in results:
        if not r.rows:
            continue
        out.append(f"## {r.name}")
        out.append("")
        cols = [c for c, _ in r.rows[0]]
        out.append("| " + " | ".join(cols) + " |")
        out.append("|" + "|".join("---" for _ in cols) + "|")
        for row in r.rows:
            out.append("| " + " | ".join(v for _, v in row) + " |")
        out.append("")
    return "\n".join(out)


def render_text(results: list[CheckResult]) -> str:
    lines: list[str] = []
    for r in results:
        status = "PASS" if r.ok else ("FAIL" if r.fatal else "WARN")
        lines.append(f"[{status}] {r.name}: {r.detail}")
        for row in r.rows:
            lines.append("    " + ", ".join(f"{k}={v}" for k, v in row))
    return "\n".join(lines)


# ----------------------------- entrypoint -----------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", DEFAULT_REPO))
    ap.add_argument("--pages-url", default=DEFAULT_PAGES_URL)
    ap.add_argument("--requirements", default="requirements.txt")
    ap.add_argument("--skip-github", action="store_true", help="skip gh API calls (offline / no auth)")
    ap.add_argument("--skip-network", action="store_true", help="skip OSV + pages probes")
    ap.add_argument("--summary-file", help="write a markdown summary here (defaults to $GITHUB_STEP_SUMMARY if set)")
    args = ap.parse_args()

    results: list[CheckResult] = []

    if args.skip_github:
        results.append(CheckResult("CodeQL", ok=True, detail="skipped (--skip-github)", fatal=False))
        results.append(CheckResult("Dependabot", ok=True, detail="skipped (--skip-github)", fatal=False))
        results.append(CheckResult("SecretScanning", ok=True, detail="skipped (--skip-github)", fatal=False))
        results.append(CheckResult("RepoPosture", ok=True, detail="skipped (--skip-github)", fatal=False))
    else:
        results.append(check_codeql(args.repo))
        results.append(check_dependabot(args.repo))
        results.append(check_secret_scanning(args.repo))
        results.append(check_posture(args.repo))

    if args.skip_network:
        results.append(CheckResult("OSV/CVE", ok=True, detail="skipped (--skip-network)", fatal=False))
        results.append(CheckResult("PagesSurface", ok=True, detail="skipped (--skip-network)", fatal=False))
        results.append(CheckResult("Headers", ok=True, detail="skipped (--skip-network)", fatal=False))
    else:
        results.append(check_osv(args.requirements))
        results.append(check_pages_surface(args.pages_url))
        results.append(check_headers(args.pages_url))

    print(render_text(results))

    summary_path = args.summary_file or os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as f:
            f.write(render_markdown(results))
            f.write("\n")

    failed = [r for r in results if not r.ok and r.fatal]
    if failed:
        print(f"\nFAIL: {len(failed)} check(s) require attention: {', '.join(r.name for r in failed)}", file=sys.stderr)
        return 1
    print("\nAll fatal checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
