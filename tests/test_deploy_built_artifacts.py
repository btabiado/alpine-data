"""Two derived files are built by pages.yml at deploy time instead of committed.

* data-opensky-positions.json: live Flight Map positions. Its hourly commits
  were 46% of all repo history; only the latest snapshot has any value.
* health/api-catalog.xlsx: /health/'s workbook download. A zip, so every
  weekly version was stored whole (11% of history); it is derived entirely
  from committed JSON.

These tests pin both halves: nothing commits them any more, and the deploy
still produces them before anything that reads or publishes them.
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows"
POSITIONS = "data-opensky-positions.json"
XLSX = "health/api-catalog.xlsx"


def _wf(name):
    return yaml.safe_load((WF / name).read_text(encoding="utf-8"))


def _steps(name, job):
    return _wf(name)["jobs"][job]["steps"]


def _index(steps, pred):
    return next(i for i, s in enumerate(steps) if pred(s))


def _tracked(rel):
    return subprocess.run(["git", "ls-files", "--error-unmatch", rel], cwd=ROOT,
                          capture_output=True, text=True).returncode == 0


@pytest.mark.parametrize("rel", [POSITIONS, XLSX])
def test_ignored(rel):
    ignored = subprocess.run(["git", "check-ignore", "-q", "--no-index", rel], cwd=ROOT)
    assert ignored.returncode == 0, f"{rel} is not gitignored; a stray `git add -A` would commit it"


def test_workbook_is_not_tracked():
    assert not _tracked(XLSX), f"{XLSX} is still tracked"


def test_aviation_workflow_commits_the_summary_and_untracks_positions():
    """The positions file is untracked by this job's own next commit (it lands
    several commits a day, so untracking it in a PR would conflict)."""
    run = _steps("aviation-opensky.yml", "run")[-1]["run"]
    lines = [ln.strip() for ln in run.splitlines()]
    rm = f"git rm -q --cached --ignore-unmatch {POSITIONS}"
    assert rm in lines and "git add data-opensky.json" in lines
    assert lines.index(rm) < next(i for i, ln in enumerate(lines) if "api-commit.sh" in ln)
    assert not any(ln.startswith("git add") and POSITIONS in ln for ln in lines)


def test_catalog_workflow_no_longer_builds_or_commits_the_workbook():
    wf = _wf("catalog-health.yml")
    text = (WF / "catalog-health.yml").read_text(encoding="utf-8")
    runs = " ".join(s.get("run") or "" for s in wf["jobs"]["run"]["steps"])
    assert "build_catalog_xlsx.py" not in runs
    assert XLSX not in runs
    assert "git add health/catalog_health.json health/catalog_health.md" in runs
    # The schedule is weekly; the commit message and header say so.
    on = wf.get("on", wf.get(True))
    assert on["schedule"] == [{"cron": "0 8 * * 1"}]
    assert "weekly link re-check" in runs
    assert "monthly link" not in text.lower()


def test_pages_fetches_positions_before_anything_reads_or_publishes_them():
    steps = _steps("pages.yml", "build")
    fetch = _index(steps, lambda s: "python fetch_opensky.py" in (s.get("run") or ""))
    step = steps[fetch]
    assert step.get("continue-on-error") is True
    assert 0 < step.get("timeout-minutes", 0) <= 5
    assert set(step["env"]) == {"OPENSKY_CLIENT_ID", "OPENSKY_CLIENT_SECRET"}
    assert "::warning" in step["run"]
    v1 = _index(steps, lambda s: "app.py --fetch-market" in (s.get("run") or ""))
    for later in ("Build dashboard health status JSON", "Stage site directory",
                  "Upload daily snapshot to ADW R2 archive"):
        assert v1 < fetch < _index(steps, lambda s, n=later: s.get("name") == n), later
    judge = _index(steps, lambda s: "data_health.py --mode built" in (s.get("run") or ""))
    assert fetch < judge


def test_pages_caches_the_last_good_positions():
    steps = _steps("pages.yml", "build")
    cache = steps[_index(steps, lambda s: "actions/cache@" in (s.get("uses") or ""))]
    assert POSITIONS in cache["with"]["path"].split()


def test_pages_builds_the_workbook_before_staging_health():
    steps = _steps("pages.yml", "build")
    build = _index(steps, lambda s: "scripts/build_catalog_xlsx.py" in (s.get("run") or ""))
    stage = _index(steps, lambda s: s.get("name") == "Stage site directory")
    assert build < stage
    assert "cp -R health/* _site/health/" in steps[stage]["run"]
    assert "openpyxl==" in steps[build]["run"]           # pinned
    assert steps[build].get("continue-on-error") is True
    # The download link it serves is still there.
    assert 'href="./api-catalog.xlsx"' in (ROOT / "health" / "index.html").read_text(encoding="utf-8")


def test_data_health_judges_positions_as_a_built_artifact():
    spec = importlib.util.spec_from_file_location("_dh_deploy", ROOT / "scripts" / "data_health.py")
    dh = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = dh
    spec.loader.exec_module(dh)
    feed = dh.MANIFEST[POSITIONS]
    assert feed.kind == dh.BUILT and "pages.yml" in feed.owner
    assert not [p for p in dh.verify_manifest() if POSITIONS in p]
