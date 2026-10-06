"""CI deprecations (audit 2026-10-04).

* ``ubuntu-latest`` moves to Ubuntu 26.04 on 2026-10-19. Every job that runs
  actions/setup-python with a pinned interpreter depends on the image's
  toolcache having that build, so those jobs pin ``ubuntu-24.04``. CodeQL
  (its Python extraction uses the image's Python) and TruffleHog pin it too;
  only pages.yml's deploy job, which just runs deploy-pages, may stay on
  ``ubuntu-latest``.
* Only the jobs that read git history check out all of it.
* No action may run on the deprecated Node 20 runtime: every remote action is
  SHA-pinned with a version comment (all current pins are node24 or
  composite, verified against each action.yml on 2026-10-04).
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
USES = re.compile(r"uses:\s*([\w.-]+/[\w./-]+)@(\S+)(?:\s+#\s*(\S+))?")


def _jobs():
    for wf in sorted(WORKFLOWS.glob("*.yml")):
        doc = yaml.safe_load(wf.read_text())
        for name, job in (doc.get("jobs") or {}).items():
            yield wf.name, name, job


def test_python_jobs_pin_an_explicit_ubuntu_image():
    bad = []
    for wf, name, job in _jobs():
        steps = job.get("steps") or []
        uses_python = any("actions/setup-python@" in str(s.get("uses", "")) for s in steps)
        if uses_python and not re.fullmatch(r"ubuntu-\d{2}\.04", str(job.get("runs-on"))):
            bad.append(f"{wf}:{name} runs-on {job.get('runs-on')}")
    assert not bad, bad


def test_remote_actions_are_sha_pinned_with_a_version_comment():
    bad = []
    for wf in sorted(WORKFLOWS.glob("*.yml")):
        for line in wf.read_text().splitlines():
            m = USES.search(line)
            if not m or line.strip().startswith("#"):
                continue
            action, ref, comment = m.groups()
            if not re.fullmatch(r"[0-9a-f]{40}", ref) or not comment:
                bad.append(f"{wf.name}: {action}@{ref} {comment or '(no version comment)'}")
    assert not bad, bad


def test_codeql_pin_comment_matches_its_major():
    text = (WORKFLOWS / "codeql.yml").read_text()
    # 8aad20d1 is codeql-action 4.36.2 (node24); it used to be labelled "# v3".
    assert "8aad20d150bbac5944a9f9d289da16a4b0d87c1e # v3" not in text


def test_codeql_and_trufflehog_pin_an_explicit_ubuntu_image():
    for wf in ("codeql.yml", "trufflehog-weekly.yml"):
        doc = yaml.safe_load((WORKFLOWS / wf).read_text())
        for name, job in doc["jobs"].items():
            assert re.fullmatch(r"ubuntu-\d{2}\.04", str(job.get("runs-on"))), (wf, name)


# Workflows whose scripts read git history: data-health (history_continuity's
# `git log` for daily-commit feeds), r2-backfill (its commit walk) and the
# TruffleHog full-history scan. Everything else only fetches and commits
# through .github/scripts/api-commit.sh, which reads the base commit from the
# API, so a depth-1 checkout is enough and saves ~135 MB per run.
FULL_HISTORY = {"data-health.yml", "r2-backfill.yml", "trufflehog-weekly.yml"}


def test_only_history_readers_check_out_full_history():
    full = set()
    for wf in sorted(WORKFLOWS.glob("*.yml")):
        doc = yaml.safe_load(wf.read_text())
        for job in (doc.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                if str(step.get("uses", "")).startswith("actions/checkout@") \
                        and (step.get("with") or {}).get("fetch-depth") == 0:
                    full.add(wf.name)
    assert full == FULL_HISTORY
