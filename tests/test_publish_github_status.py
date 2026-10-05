"""Offline tests for scripts/publish_github_status.py and its pages.yml wiring.

No network: the GitHub REST API is replaced by a fake ``requests`` session
that routes on the URL path, so the real client code (headers, retries, error
parsing, budget) runs against canned responses.
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "publish_github_status.py"
PAGES_YML = REPO_ROOT / ".github" / "workflows" / "pages.yml"
DAILY_AUDIT_YML = REPO_ROOT / ".github" / "workflows" / "daily-audit.yml"
HEALTH_HTML = REPO_ROOT / "health" / "index.html"

UTC = timezone.utc
NOW = datetime(2026, 10, 5, 23, 0, tzinfo=UTC)
REPO = "btabiado/alpine-data"
ADW = "btabiado/adw-site"


@pytest.fixture(scope="module")
def pgs():
    spec = importlib.util.spec_from_file_location("_publish_github_status_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def ts(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(rid: int, hours_ago: float, conclusion: str | None = "success", event: str = "schedule",
        status: str = "completed", branch: str = "main") -> dict:
    return {"id": rid, "event": event, "status": status, "conclusion": conclusion,
            "head_branch": branch,
            "created_at": ts(hours_ago), "updated_at": ts(max(hours_ago - 0.1, 0)),
            "html_url": f"https://github.com/{REPO}/actions/runs/{rid}", "run_number": rid,
            "head_sha": "f" * 40, "pull_requests": []}


# ---------------------------------------------------------------------------
# Fake HTTP
# ---------------------------------------------------------------------------

class FakeResp:
    def __init__(self, status: int, body, headers: dict | None = None) -> None:
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def json(self):
        if isinstance(self._body, (bytes, str)):
            return json.loads(self._body)
        return self._body

    @property
    def text(self) -> str:
        return self._body if isinstance(self._body, str) else json.dumps(self._body)


class FakeSession:
    """``routes`` maps an exact URL path to (status, body), an Exception to
    raise, or a callable (query, headers) -> one of those."""

    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.requests: list[tuple[str, dict]] = []

    def get(self, url, headers=None, timeout=None):
        self.requests.append((url, dict(headers or {})))
        parts = urlsplit(url)
        assert parts.scheme == "https" and parts.netloc == "api.github.com", url
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        r = self.routes.get(parts.path)
        if r is None:
            return FakeResp(404, {"message": "Not Found"})
        if callable(r):
            r = r(query, headers or {})
        if isinstance(r, Exception):
            raise r
        status, body = r
        return FakeResp(status, body)

    def paths(self) -> list[str]:
        return [urlsplit(u).path for u, _ in self.requests]


NODE_DEPRECATION = ("Node.js 20 actions are deprecated. The following actions are running on "
                    "Node.js 20 and may not work as expected: actions/checkout@v4. Actions will be "
                    "forced to run with Node.js 24 by default starting June 2nd, 2026.")
RUNNER_LABEL = ("The ubuntu-latest label will migrate to Ubuntu 26.04 on 2026-10-19. "
                "Pin ubuntu-24.04 to keep the current image.")


def _workflows_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True)
    crons = {"pages.yml": "0 * * * *", "aviation-tsa.yml": "10 14 * * *",
             "lthcs-trends-daily.yml": "0 4 * * *", "broken.yml": "0 5 * * 1"}
    for name, cron in crons.items():
        (d / name).write_text(f'name: {name[:-4]}\non:\n  schedule:\n    - cron: "{cron}"\n'
                              f"  workflow_dispatch: {{}}\njobs: {{}}\n")
    (d / "tests.yml").write_text("name: tests\non:\n  push:\n    branches: [main]\njobs: {}\n")
    return d


def _routes() -> dict:
    wf = lambda i, name, path, state="active": {  # noqa: E731
        "id": i, "name": name, "path": path, "state": state, "created_at": "2026-01-01T00:00:00Z",
        "html_url": f"https://github.com/{REPO}/blob/main/{path}"}
    base = f"/repos/{REPO}"
    return {
        f"{base}/actions/workflows": (200, {"total_count": 6, "workflows": [
            wf(1, "pages", ".github/workflows/pages.yml"),
            wf(2, "aviation-tsa", ".github/workflows/aviation-tsa.yml"),
            wf(3, "tests", ".github/workflows/tests.yml"),
            wf(4, "lthcs-trends-daily", ".github/workflows/lthcs-trends-daily.yml"),
            wf(5, "broken", ".github/workflows/broken.yml"),
            wf(6, "Dependabot Updates", "dynamic/dependabot/dependabot-updates"),
        ]}),
        f"{base}/actions/workflows/1/runs": (200, {"workflow_runs": [
            # PR and side-branch runs are not main's health.
            run(100, 0.2, "failure", "pull_request", branch="dependabot/x"),
            run(99, 0.3, "failure", "workflow_dispatch", branch="claude/feature"),
            run(101, 0.5, event="push"), run(102, 1.2)]}),
        f"{base}/actions/workflows/2/runs": (200, {"workflow_runs": [
            run(201, 2), run(202, 26, "failure"), run(203, 50, "failure"), run(204, 74, "failure")]}),
        f"{base}/actions/workflows/3/runs": (200, {"workflow_runs": [
            run(300, 0.1, None, "push", "in_progress"),
            run(301, 3, "failure", "push"), run(302, 5, "failure", "push"),
            run(303, 6, "failure", "push"), run(304, 7, "cancelled", "push")]}),
        f"{base}/actions/workflows/4/runs": (200, {"workflow_runs": [run(401, 600)]}),
        f"{base}/actions/workflows/5/runs": (500, {"message": "Server Error"}),
        f"{base}/actions/workflows/6/runs": (200, {"workflow_runs": []}),
        # Failed-run drill-down.
        f"{base}/actions/runs/202/jobs": (200, {"jobs": [
            {"id": 9202, "name": "run", "conclusion": "failure",
             "html_url": f"https://github.com/{REPO}/actions/runs/202/job/9202",
             "steps": [{"name": "Set up job", "conclusion": "success"},
                       {"name": "Fetch TSA throughput snapshot", "conclusion": "failure"}]},
            {"id": 9203, "name": "lint", "conclusion": "success", "steps": []}]}),
        f"{base}/check-runs/9202/annotations": (200, [
            {"annotation_level": "warning", "title": "", "message": NODE_DEPRECATION, "path": ".github"},
            {"annotation_level": "failure", "title": "", "message": "Process completed with exit code 1.",
             "path": ".github"},
            {"annotation_level": "warning", "title": "TSA not refreshed",
             "message": "tsa.gov: HTTPError: HTTP Error 403: Forbidden", "path": ".github"}]),
        f"{base}/actions/runs/301/jobs": (200, {"jobs": [
            {"id": 9301, "name": "pytest", "conclusion": "failure", "steps": [
                {"name": "Run tests", "conclusion": "failure"}]}]}),
        f"{base}/check-runs/9301/annotations": (403, {"message": "Resource not accessible by integration"}),
        f"{base}/actions/runs/302/jobs": (200, {"jobs": [
            {"id": 9302, "name": "pytest", "conclusion": "failure", "steps": []}]}),
        f"{base}/check-runs/9302/annotations": (200, []),
        # Issues (the endpoint also lists PRs, which must be skipped).
        f"{base}/issues": (200, [
            {"number": 80, "title": "Daily audit: action needed", "labels": [{"name": "daily-audit"}],
             "user": {"login": "github-actions[bot]"}, "created_at": ts(30),
             "html_url": f"https://github.com/{REPO}/issues/80"},
            {"number": 81, "title": "a PR", "labels": [], "user": {"login": "x"}, "created_at": ts(1),
             "pull_request": {"url": "..."}}]),
        f"{base}/code-scanning/alerts": (200, [
            {"rule": {"security_severity_level": "high", "severity": "error"}},
            {"rule": {"severity": "warning"}}, {"rule": {"severity": "note"}}]),
        f"{base}/dependabot/alerts": (403, {"message": "Resource not accessible by integration"}),
        f"{base}/pulls": (200, [
            {"number": 70, "title": "Add GitHub status JSON", "user": {"login": "btabiado"},
             "draft": False, "created_at": ts(48), "html_url": f"https://github.com/{REPO}/pull/70",
             "head": {"sha": "a" * 40}}]),
        f"{base}/commits/{'a' * 40}/check-runs": (200, {"total_count": 2, "check_runs": [
            {"name": "pytest", "status": "completed", "conclusion": "failure"},
            {"name": "CodeQL", "status": "in_progress", "conclusion": None}]}),
        f"/repos/{ADW}/pulls": (404, {"message": "Not Found"}),
    }


def _client(pgs, routes: dict | None = None, *, token: str | None = "tok_test_value_123",
            budget: int | None = None):
    session = FakeSession(routes if routes is not None else _routes())
    kw = {"budget": budget} if budget is not None else {}
    return pgs.GitHubClient(token, session=session, sleep=lambda s: None, **kw), session


def _build(pgs, tmp_path, routes=None, **kw):
    client, session = _client(pgs, routes, **{k: v for k, v in kw.items() if k == "budget"})
    doc = pgs.build_status(client, repo=REPO, workflows_dir=_workflows_dir(tmp_path), now=NOW)
    return doc, client, session


def _wf(doc: dict, name: str) -> dict:
    return next(w for w in doc["workflows"]["items"] if w["name"] == name)


# ===========================================================================
# Pure helpers
# ===========================================================================

class TestCronInterval:
    @pytest.mark.parametrize("crons,hours", [
        (["0 * * * *"], 1.0),
        (["17 9 * * *"], 24.0),
        (["0 6 * * *", "0 18 * * *"], 12.0),     # merged
        (["30 13 * * 1-5"], 72.0),               # Fri 13:30 -> Mon 13:30
        (["0 5 * * 1"], 168.0),
    ])
    def test_longest_gap(self, pgs, crons, hours):
        assert pgs.cron_interval_hours(crons, NOW) == hours

    def test_monthly_is_a_month(self, pgs):
        assert 28 * 24 <= pgs.cron_interval_hours(["0 6 1 * *"], NOW) <= 31 * 24


class TestDerivedFlags:
    def flags(self, pgs, runs, interval=24.0, **kw):
        return pgs.derive_flags(runs, NOW, interval_h=interval, **kw)

    def test_failed_last_24h(self, pgs):
        assert self.flags(pgs, [run(1, 3, "failure")])["failed_last_24h"] is True
        assert self.flags(pgs, [run(1, 3, "timed_out")])["failed_last_24h"] is True
        assert self.flags(pgs, [run(1, 30, "failure")])["failed_last_24h"] is False
        # cancelled is not a failure: pages.yml cancels superseded runs by design.
        assert self.flags(pgs, [run(1, 3, "cancelled")])["failed_last_24h"] is False

    def test_repeat_failure_is_two_of_the_last_three_completed(self, pgs):
        f = lambda *c: self.flags(pgs, [run(i, i + 1, x) for i, x in enumerate(c)])["repeat_failure"]  # noqa: E731
        assert f("failure", "success", "failure") is True
        assert f("success", "failure", "failure") is True
        assert f("failure", "success", "success", "failure") is False   # 4th run is outside the 3
        # An in-progress run is not one of the "last 3 completed".
        runs = [run(0, 0.1, None, status="in_progress"), run(1, 1, "failure"), run(2, 2, "success"),
                run(3, 3, "failure")]
        assert self.flags(pgs, runs)["repeat_failure"] is True

    def test_recovered(self, pgs):
        assert self.flags(pgs, [run(1, 1), run(2, 30, "failure")])["recovered"] is True
        assert self.flags(pgs, [run(1, 1), run(2, 120, "failure")])["recovered"] is False  # old news
        assert self.flags(pgs, [run(1, 1, "failure"), run(2, 3)])["recovered"] is False
        assert self.flags(pgs, [run(1, 1), run(2, 3)])["recovered"] is False

    def test_not_running_is_twice_the_cron_interval(self, pgs):
        assert self.flags(pgs, [run(1, 1.5)], interval=1.0)["not_running"] is False
        assert self.flags(pgs, [run(1, 2.5)], interval=1.0)["not_running"] is True
        assert self.flags(pgs, [run(1, 47)], interval=24.0)["not_running"] is False
        assert self.flags(pgs, [run(1, 49)], interval=24.0)["not_running"] is True
        # Any run counts (a push keeps it alive), whatever its conclusion.
        assert self.flags(pgs, [run(1, 0.5, "failure", "push"), run(2, 9)],
                          interval=1.0)["not_running"] is False

    def test_not_running_edge_cases(self, pgs):
        assert self.flags(pgs, [run(1, 1)], interval=None)["not_running"] is None    # unscheduled
        assert self.flags(pgs, [run(1, 1)], interval=24.0,
                          state="disabled_inactivity")["not_running"] is True
        assert self.flags(pgs, [], interval=168.0,
                          workflow_created_at=ts(24))["not_running"] is False         # too new to judge
        assert self.flags(pgs, [], interval=24.0,
                          workflow_created_at=ts(24 * 30))["not_running"] is True


class TestAnnotations:
    def test_platform_notices(self, pgs):
        assert pgs.is_platform_notice(NODE_DEPRECATION)
        assert pgs.is_platform_notice(RUNNER_LABEL)
        assert pgs.is_platform_notice("ubuntu-latest pipelines will use ubuntu-24.04 soon")
        assert not pgs.is_platform_notice("tsa.gov: HTTPError: HTTP Error 403: Forbidden")
        assert not pgs.is_platform_notice("npm ERR! node 20 is fine but the lockfile is broken")

    def test_selection_filters_ranks_dedups_and_trims(self, pgs):
        raw = [
            {"annotation_level": "warning", "title": "", "message": NODE_DEPRECATION},
            {"annotation_level": "warning", "title": "Runner image", "message": RUNNER_LABEL},
            {"annotation_level": "notice", "title": "", "message": "Some notice"},
            {"annotation_level": "failure", "title": "", "message": "Process completed with exit code 1."},
            {"annotation_level": "warning", "title": "Feed stale", "message": "x  is\n 30h old"},
            {"annotation_level": "warning", "title": "Feed stale", "message": "x  is\n 30h old"},
            {"annotation_level": "failure", "title": "AssertionError", "message": "y" * 1000,
             "path": "tests/test_x.py"},
            # A failure-level runner problem is real, never filtered.
            {"annotation_level": "failure", "title": "",
             "message": "No runner matching the specified labels was found: ubuntu-26.04"},
        ]
        got = pgs.select_annotations(raw, limit=10)
        msgs = [a["message"] for a in got]
        assert NODE_DEPRECATION not in msgs and RUNNER_LABEL not in msgs
        assert "Some notice" not in msgs
        assert msgs.count("x is 30h old") == 1                       # de-duplicated, whitespace folded
        assert got[0]["title"] == "AssertionError" and len(got[0]["message"]) <= 300
        assert got[0]["path"] == "tests/test_x.py"
        assert got[1]["message"].startswith("No runner matching")
        assert got[2]["level"] == "warning"
        assert got[-1]["message"] == "Process completed with exit code 1."   # generic one last
        assert len(pgs.select_annotations(raw, limit=2)) == 2

    def test_bad_input_is_empty_not_a_crash(self, pgs):
        assert pgs.select_annotations(None) == []
        assert pgs.select_annotations({"message": "not a list"}) == []
        assert pgs.select_annotations([None, "x", {"annotation_level": "failure"}]) == [
            {"level": "failure", "title": "", "message": ""}]


def test_summarize_checks(pgs):
    s = pgs.summarize_checks
    assert s([]) == {"conclusion": "none", "total": 0}
    assert s([{"name": "a", "status": "completed", "conclusion": "success"},
              {"name": "b", "status": "completed", "conclusion": "skipped"}])["conclusion"] == "success"
    assert s([{"name": "a", "status": "in_progress", "conclusion": None}])["conclusion"] == "pending"
    got = s([{"name": "a", "status": "completed", "conclusion": "failure"},
             {"name": "b", "status": "queued", "conclusion": None}])
    assert got == {"conclusion": "failure", "total": 2, "failing": ["a"], "pending": 1}
    assert s([{"name": "a", "status": "completed", "conclusion": "cancelled"}])["conclusion"] == "cancelled"


# ===========================================================================
# Whole document against the fake API
# ===========================================================================

class TestBuildStatus:
    def test_shape_and_summary(self, pgs, tmp_path):
        doc, client, _ = _build(pgs, tmp_path)
        assert doc["schema"] == 1 and doc["generated_at"] == "2026-10-05T23:00:00Z"
        keys = list(doc)
        assert keys[:4] == ["schema", "generated_at", "repo", "auth"]
        assert keys.index("summary") < keys.index("workflows")      # summary first, read top-down
        s = doc["summary"]
        assert s["sections"] == {"workflows": "partial", "issues": "ok", "code_scanning": "ok",
                                 "dependabot": "unavailable", "pull_requests": "partial"}
        assert s["workflows"] == 6
        assert s["failed_last_24h"] == ["tests"]
        assert s["repeat_failure"] == ["aviation-tsa", "tests"]
        assert s["not_running"] == ["lthcs-trends-daily"]
        assert s["recovered"] == ["aviation-tsa"]
        assert s["open_prs"] == {REPO: 1, ADW: None}          # unreadable is null, not 0
        assert s["open_issues"] == 1
        assert s["code_scanning_open"] == 3
        assert s["dependabot_open"] is None
        assert doc["api_calls"] == client.calls <= doc["api_budget"] == 100

    def test_names_the_run_that_produced_it(self, pgs, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_RUN_ID", "37354453626")
        monkeypatch.setenv("GITHUB_SHA", "8499922e" + "0" * 32)
        monkeypatch.setenv("GITHUB_WORKFLOW", "pages")
        doc, _, _ = _build(pgs, tmp_path)
        assert doc["generated_by"] == {
            "workflow": "pages", "sha": "8499922e0000",
            "run_url": f"https://github.com/{REPO}/actions/runs/37354453626"}

    def test_workflow_items(self, pgs, tmp_path):
        doc, _, _ = _build(pgs, tmp_path)
        pages = _wf(doc, "pages")
        assert pages["schedule"] == ["0 * * * *"] and pages["cron_interval_h"] == 1.0
        assert [r["id"] for r in pages["runs"]] == [101, 102]
        assert "failed_runs" not in pages and pages["flags"]["failed_last_24h"] is False
        assert set(pages["runs"][0]) == {"id", "event", "status", "conclusion", "created_at",
                                         "updated_at", "html_url"}
        assert pages["flags"] == {"failed_last_24h": False, "repeat_failure": False,
                                  "not_running": False, "recovered": False}
        assert "failed_runs" not in pages
        assert _wf(doc, "tests")["schedule"] == [] and _wf(doc, "tests")["flags"]["not_running"] is None
        trends = _wf(doc, "lthcs-trends-daily")
        assert trends["flags"]["not_running"] is True and trends["hours_since_last_run"] == 600.0
        dyn = _wf(doc, "Dependabot Updates")
        assert dyn["runs"] == [] and dyn["last_run_at"] is None

    def test_failed_runs_carry_key_annotations(self, pgs, tmp_path):
        doc, _, session = _build(pgs, tmp_path)
        tsa = _wf(doc, "aviation-tsa")
        # Only the failure inside 48h is drilled into (203 is 50h old, 204 74h).
        assert [f["run_id"] for f in tsa["failed_runs"]] == [202]
        job = tsa["failed_runs"][0]["jobs"]
        assert len(job) == 1 and job[0]["name"] == "run"
        assert job[0]["failed_steps"] == ["Fetch TSA throughput snapshot"]
        assert [a["message"] for a in job[0]["annotations"]] == [
            "tsa.gov: HTTPError: HTTP Error 403: Forbidden", "Process completed with exit code 1."]
        assert all("path" not in a for a in job[0]["annotations"])       # ".github" is noise
        # Per-workflow cap: 3 recent failures, 2 inspected, the rest counted.
        tests = _wf(doc, "tests")
        assert [f["run_id"] for f in tests["failed_runs"]] == [301, 302]
        assert tests["failed_runs_not_inspected"] == 1
        # An annotations 403 is recorded on that job only.
        assert tests["failed_runs"][0]["jobs"][0]["annotations_unavailable"].startswith("HTTP 403")
        assert tests["failed_runs"][1]["jobs"][0]["annotations"] == []
        assert f"/repos/{REPO}/actions/runs/303/jobs" not in session.paths()
        assert f"/repos/{REPO}/actions/runs/203/jobs" not in session.paths()

    def test_prs_issues_and_alerts(self, pgs, tmp_path):
        doc, _, _ = _build(pgs, tmp_path)
        repos = {r["repo"]: r for r in doc["pull_requests"]["repos"]}
        pr = repos[REPO]["items"][0]
        assert pr["number"] == 70 and pr["author"] == "btabiado" and pr["age_days"] == 2.0
        assert pr["head_ci"] == {"conclusion": "failure", "total": 2, "failing": ["pytest"], "pending": 1}
        assert repos[ADW]["open"] is None and "items" not in repos[ADW]
        assert repos[ADW]["unavailable"].startswith("HTTP 404: Not Found")
        assert "ADW_SITE_TOKEN" in repos[ADW]["unavailable"]
        issues = doc["issues"]
        assert issues["open"] == 1 and issues["items"][0]["number"] == 80
        assert issues["items"][0]["labels"] == ["daily-audit"] and issues["items"][0]["age_days"] == 1.2
        assert doc["code_scanning"] == {"status": "ok", "open": 3,
                                        "by_severity": {"high": 1, "note": 1, "warning": 1}}
        dep = doc["dependabot"]
        assert dep["status"] == "unavailable" and dep["open"] is None and "by_severity" not in dep
        assert dep["unavailable"].startswith("HTTP 403: Resource not accessible by integration")

    def test_runs_are_listed_unfiltered_and_narrowed_client_side(self, pgs, tmp_path):
        # GitHub's `branch=` filter on workflow runs intermittently answers
        # from a stale index; the client must not rely on it.
        _, _, session = _build(pgs, tmp_path)
        listing = [u for u, _ in session.requests if u.split("?")[0].endswith("/actions/workflows/1/runs")]
        assert listing and all("branch=" not in u for u in listing)
        assert "per_page=20" in listing[0]

    def test_server_error_is_retried_once_then_isolated(self, pgs, tmp_path):
        doc, _, session = _build(pgs, tmp_path)
        broken = _wf(doc, "broken")
        assert broken["unavailable"] == "HTTP 500: Server Error" and broken["flags"] is None
        assert session.paths().count(f"/repos/{REPO}/actions/workflows/5/runs") == 2
        assert doc["workflows"]["items_unavailable"] == ["broken"]


class TestFailureIsolation:
    def test_workflow_listing_down_leaves_everything_else(self, pgs, tmp_path):
        routes = _routes()
        routes[f"/repos/{REPO}/actions/workflows"] = ConnectionError("connection reset by peer")
        doc, _, _ = _build(pgs, tmp_path, routes)
        assert doc["workflows"]["status"] == "unavailable"
        assert "network error" in doc["workflows"]["unavailable"]
        assert doc["summary"]["failed_last_24h"] is None        # unknown, not "none failed"
        assert doc["summary"]["workflows"] is None
        assert doc["issues"]["status"] == "ok" and doc["code_scanning"]["status"] == "ok"
        assert doc["summary"]["open_prs"][REPO] == 1

    def test_a_section_that_raises_is_contained(self, pgs, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise KeyError("unexpected payload shape")
        monkeypatch.setattr(pgs, "section_issues", boom)
        doc, _, _ = _build(pgs, tmp_path)
        assert doc["issues"] == {"status": "unavailable", "unavailable": "KeyError: 'unexpected payload shape'"}
        assert doc["summary"]["open_issues"] is None
        assert doc["workflows"]["status"] == "partial" and doc["pull_requests"]["repos"]

    def test_garbage_json_body_is_contained(self, pgs, tmp_path):
        routes = _routes()
        routes[f"/repos/{REPO}/code-scanning/alerts"] = (200, "<html>not json")
        doc, _, _ = _build(pgs, tmp_path, routes)
        assert doc["code_scanning"]["open"] is None
        assert "unparseable JSON" in doc["code_scanning"]["unavailable"]

    def test_rate_limit_is_named(self, pgs, tmp_path):
        routes = _routes()
        routes[f"/repos/{REPO}/issues"] = lambda q, h: (403, {"message": "API rate limit exceeded"})
        client, session = _client(pgs, routes)
        orig = session.get

        def get(url, headers=None, timeout=None):
            r = orig(url, headers, timeout)
            if "/issues" in url:
                r.headers = {"X-RateLimit-Remaining": "0"}
            return r
        session.get = get
        doc = pgs.build_status(client, repo=REPO, workflows_dir=_workflows_dir(tmp_path), now=NOW)
        assert doc["issues"]["unavailable"] == "HTTP 403: rate limit exhausted"

    def test_budget_is_a_hard_cap(self, pgs, tmp_path):
        doc, client, session = _build(pgs, tmp_path, budget=6)
        assert client.calls == len(session.requests) == 6
        assert "budget (6) exhausted" in json.dumps(doc)
        assert doc["summary"]["open_issues"] is None and doc["summary"]["code_scanning_open"] is None

    def test_realistic_repo_stays_well_under_budget(self, pgs, tmp_path):
        """32 workflows, every one with a fresh failure, plenty of open PRs:
        the per-section caps keep the run under the 100-call budget without
        the budget itself having to cut anything."""
        routes = _routes()
        base = f"/repos/{REPO}"
        routes[f"{base}/actions/workflows"] = (200, {"workflows": [
            {"id": 1000 + i, "name": f"wf{i:02d}", "path": f".github/workflows/wf{i:02d}.yml",
             "state": "active"} for i in range(32)]})
        for i in range(32):
            routes[f"{base}/actions/workflows/{1000 + i}/runs"] = (200, {"workflow_runs": [
                run(5000 + i * 10 + k, 1 + k, "failure") for k in range(5)]})
        jobs = {"jobs": [{"id": 1, "name": "a", "conclusion": "failure"},
                         {"id": 2, "name": "b", "conclusion": "failure"},
                         {"id": 3, "name": "c", "conclusion": "failure"}]}
        for i in range(32):
            for k in range(5):
                routes[f"{base}/actions/runs/{5000 + i * 10 + k}/jobs"] = (200, jobs)
        for j in (1, 2, 3):
            routes[f"{base}/check-runs/{j}/annotations"] = (200, [])
        prs = [{"number": n, "title": "t", "user": {"login": "u"}, "created_at": ts(1),
                "head": {"sha": f"{n:040d}"}} for n in range(40)]
        routes[f"{base}/pulls"] = (200, prs)
        routes[f"/repos/{ADW}/pulls"] = (200, prs)
        for n in range(40):
            routes[f"{base}/commits/{n:040d}/check-runs"] = (200, {"check_runs": []})
            routes[f"/repos/{ADW}/commits/{n:040d}/check-runs"] = (200, {"check_runs": []})
        doc, client, _ = _build(pgs, tmp_path, routes)
        assert client.calls <= 100
        assert "exhausted" not in json.dumps(doc)
        inspected = [f for w in doc["workflows"]["items"] for f in w.get("failed_runs", [])
                     if "jobs" in f]
        assert len(inspected) == pgs.MAX_FAILED_RUNS_INSPECTED
        assert doc["summary"]["open_prs"] == {REPO: 40, ADW: 40}


# ===========================================================================
# Secrets
# ===========================================================================

SECRET = "ghs_" + "S3cr3tT0kenValue" * 3
ADW_SECRET = "github_pat_" + "AdwSiteToken_0123456789" * 2


def test_no_secret_reaches_the_file_or_the_log(pgs, tmp_path, monkeypatch, capsys):
    import requests

    routes = _routes()
    # Upstreams that echo the credential back: in an error body, and in data.
    routes[f"/repos/{REPO}/dependabot/alerts"] = lambda q, h: (
        403, {"message": f"Bad credentials: {h.get('Authorization')}"})
    routes[f"/repos/{REPO}/code-scanning/alerts"] = lambda q, h: (
        401, {"message": f"token={SECRET} rejected"})
    routes[f"/repos/{REPO}/issues"] = (200, [
        {"number": 1, "title": f"leaked {SECRET} in a title", "labels": [], "created_at": ts(1)}])
    # adw-site answers only to its own token.
    routes[f"/repos/{ADW}/pulls"] = lambda q, h: (
        (200, []) if h.get("Authorization") == f"Bearer {ADW_SECRET}" else (404, {"message": "Not Found"}))
    session = FakeSession(routes)
    monkeypatch.setattr(requests, "Session", lambda: session)
    for k in ("GITHUB_RUN_ID", "GITHUB_SHA", "GH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", SECRET)
    monkeypatch.setenv("ADW_SITE_TOKEN", ADW_SECRET)

    out = tmp_path / "_site" / "health" / "github_status.json"
    assert pgs.main(["--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    logs = capsys.readouterr()
    for blob in (text, logs.out, logs.err):
        assert SECRET not in blob and ADW_SECRET not in blob
        assert "S3cr3tT0ken" not in blob and "AdwSiteToken" not in blob
    doc = json.loads(text)
    assert doc["auth"] == "token"
    assert doc["summary"]["open_prs"][ADW] == 0          # the per-repo token was used ...
    sent = {(urlsplit(u).path.split("/")[3], h.get("Authorization")) for u, h in session.requests}
    assert ("adw-site", f"Bearer {ADW_SECRET}") in sent
    assert ("alpine-data", f"Bearer {SECRET}") in sent
    assert ("alpine-data", f"Bearer {ADW_SECRET}") not in sent    # ... and only for adw-site
    assert ("adw-site", f"Bearer {SECRET}") not in sent
    assert all("token" not in urlsplit(u).query.lower() for u, _ in session.requests)


def test_main_still_writes_a_file_when_everything_breaks(pgs, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(pgs, "build_status", boom)
    monkeypatch.setenv("GITHUB_TOKEN", SECRET)
    out = tmp_path / "s.json"
    assert pgs.main(["--out", str(out)]) == 0
    doc = json.loads(out.read_text())
    assert doc["unavailable"] == "RuntimeError: kaboom" and doc["generated_at"]


def test_scrub_masks_token_shapes(pgs):
    text = 'x ghp_' + "a" * 36 + ' y github_pat_' + "B" * 40 + " z literal-secret-1234"
    out = pgs.scrub(text, ["literal-secret-1234"])
    assert "ghp_" not in out and "github_pat_" not in out and "literal-secret-1234" not in out
    assert out.count(pgs.MASK) == 3


# ===========================================================================
# Wiring: pages.yml, the /audit/ fix, /health/ link
# ===========================================================================

@pytest.fixture(scope="module")
def pages():
    return yaml.safe_load(PAGES_YML.read_text(encoding="utf-8"))


def _steps(pages):
    return pages["jobs"]["build"]["steps"]


def _index(pages, pred) -> int:
    return next(i for i, s in enumerate(_steps(pages)) if pred(s))


def test_pages_runs_the_publisher_non_fatally(pages):
    steps = _steps(pages)
    i = _index(pages, lambda s: "publish_github_status.py" in (s.get("run") or ""))
    step = steps[i]
    assert step.get("continue-on-error") is True
    run_ = " ".join(step["run"].split())
    assert "python scripts/publish_github_status.py --out _site/health/github_status.json" in run_
    assert "|| echo \"::warning" in run_            # a crash is logged, not fatal
    assert step["env"]["GITHUB_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"
    assert 0 < step.get("timeout-minutes", 0) <= 10
    # After staging (so _site/health/ is not overwritten by `cp -R health/*`)
    # and before the artifact upload (so it ships).
    assert i > _index(pages, lambda s: s.get("name") == "Stage site directory")
    assert i < _index(pages, lambda s: "upload-pages-artifact" in (s.get("uses") or ""))
    assert "if" not in step                          # runs on every trigger


def test_pages_permissions_only_add_reads(pages):
    assert pages["permissions"] == {
        "contents": "write", "pages": "write", "id-token": "write",   # unchanged
        "actions": "read", "checks": "read", "pull-requests": "read",
        "issues": "read", "security-events": "read",
    }
    assert "permissions" not in pages["jobs"]["build"]


def test_pages_deploys_when_the_daily_audit_finishes(pages):
    on = pages.get("on", pages.get(True))
    wr = on["workflow_run"]
    audit = yaml.safe_load(DAILY_AUDIT_YML.read_text(encoding="utf-8"))
    assert wr["workflows"] == [audit["name"]] == ["daily-audit"]
    assert wr["types"] == ["completed"] and wr["branches"] == ["main"]
    # Existing triggers untouched.
    assert on["push"] == {"branches": ["main"]} and on["schedule"] == [{"cron": "0 * * * *"}]
    assert "workflow_dispatch" in on
    # workflow_run must build main as checked out, never the triggering run's head.
    checkout = next(s for s in _steps(pages) if "actions/checkout" in (s.get("uses") or ""))
    assert "ref" not in (checkout.get("with") or {})
    assert "workflow_run" not in PAGES_YML.read_text().split("jobs:", 1)[1]


def test_audit_report_is_staged_and_nothing_wipes_it(pages):
    stage = next(s for s in _steps(pages) if s.get("name") == "Stage site directory")
    run_ = stage["run"]
    start = run_.index("for f in audit/daily/latest.md")
    snippet = run_[start:run_.index("done", start) + len("done")]
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "audit/daily").mkdir(parents=True)
        (Path(d) / "audit/daily/latest.md").write_text("# report")
        (Path(d) / "audit/daily/latest.json").write_text("{}")
        script = "set -e\nshopt -s nullglob\nmkdir -p _site\n" + snippet + "\n"
        r = subprocess.run(["bash", "-c", script], cwd=d, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert (Path(d) / "_site/audit/latest.md").read_text() == "# report"
        assert (Path(d) / "_site/audit/latest.json").read_text() == "{}"
    # No later step removes or re-creates _site (or _site/audit) before upload.
    later = _steps(pages)[_steps(pages).index(stage) + 1:]
    for s in later:
        text = (s.get("run") or "")
        assert not re.search(r"\brm\s+-[a-zA-Z]*\s+_site\b|\brm\b[^\n]*_site/audit", text), s.get("name")
    upload = next(s for s in _steps(pages) if "upload-pages-artifact" in (s.get("uses") or ""))
    assert upload["with"]["path"] == "_site"


def test_daily_audit_report_is_committed_where_pages_reads_it():
    audit = yaml.safe_load(DAILY_AUDIT_YML.read_text(encoding="utf-8"))
    steps = audit["jobs"]["audit"]["steps"]
    assert any("--out-dir audit/daily" in (s.get("run") or "") for s in steps)
    assert any("git add -A audit/daily" in (s.get("run") or "") for s in steps)


def test_health_page_links_the_status_json():
    html = HEALTH_HTML.read_text(encoding="utf-8")
    assert 'href="./github_status.json"' in html
