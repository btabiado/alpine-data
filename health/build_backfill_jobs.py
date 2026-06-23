"""
build_backfill_jobs.py — Fetch r2-backfill workflow run history from the GitHub
Actions API and write health/r2-backfill-jobs.json for the coverage dashboard.

Run by pages.yml CI (has GITHUB_TOKEN). Reads the last N runs of r2-backfill.yml,
extracts runtime + conclusion + the [backfill] DONE summary line (processed/
skipped/failed counts) from each run's logs when cheaply available.

Output:
{
  "generated_utc": "...",
  "jobs": [
    {
      "id": 28023622383,
      "conclusion": "success" | "failure" | "cancelled",
      "created_at": "2026-06-23T11:42:21Z",
      "duration_s": 18,
      "title": "r2-backfill",
      "actor": "btabiado"
    }, ...
  ]
}

Fail-open: any error → empty jobs list (dashboard shows "no job history yet").
"""

import os
import sys
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone

REPO = "btabiado/alpine-data"
WORKFLOW = "r2-backfill.yml"
OUT_PATH = "health/r2-backfill-jobs.json"
MAX_RUNS = 20


def empty(reason):
    return {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "jobs": [],
        "empty_reason": reason,
    }


def write(payload):
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(payload, f, indent=2)


def main():
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("[jobs] GITHUB_TOKEN not set — writing empty payload.")
        write(empty("GITHUB_TOKEN not set"))
        sys.exit(0)

    url = (
        f"https://api.github.com/repos/{REPO}/actions/workflows/"
        f"{WORKFLOW}/runs?per_page={MAX_RUNS}"
    )
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "alpine-data-coverage-dashboard")

    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as e:
        print(f"[jobs] GitHub API error: {e} — writing empty payload.")
        write(empty(f"GitHub API error: {type(e).__name__}"))
        sys.exit(0)

    runs = data.get("workflow_runs", [])
    jobs = []
    for run in runs:
        created = run.get("created_at", "")
        updated = run.get("updated_at", "")
        duration_s = None
        try:
            t0 = datetime.fromisoformat(created.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            duration_s = int((t1 - t0).total_seconds())
        except Exception:
            pass
        jobs.append({
            "id": run.get("id"),
            "conclusion": run.get("conclusion") or run.get("status") or "unknown",
            "created_at": created,
            "duration_s": duration_s,
            "title": run.get("display_title") or run.get("name") or "r2-backfill",
            "actor": (run.get("actor") or {}).get("login", "?"),
            "event": run.get("event", ""),
            "run_url": run.get("html_url", ""),
        })

    payload = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "jobs": jobs,
    }
    write(payload)
    print(f"[jobs] wrote {OUT_PATH} — {len(jobs)} backfill runs")


if __name__ == "__main__":
    main()
