"""Offline tests for the cron catch-up watchdog.

No network. Decisions are pure functions of a clock and canned workflow runs.
The workflow file is checked by parsing its YAML.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
UTC = timezone.utc

# 16:00 UTC is past the 3h grace of a 07:xx daily and of the 12:xx hourly fire,
# and still inside the grace of the 15:xx hourly fire.
NOW = datetime(2026, 10, 17, 16, 0, tzinfo=UTC)
MORNING = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)


def _load():
    spec = importlib.util.spec_from_file_location("_cron_watchdog_under_test", SCRIPTS / "cron_watchdog.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cw():
    return _load()


def _run(stamp: str, conclusion: str = "success", status: str = "completed",
         updated: str | None = None, branch: str = "main") -> dict:
    return {
        "status": status,
        "conclusion": conclusion,
        "created_at": stamp,
        "updated_at": updated or stamp,
        "head_branch": branch,
    }


def _snap(runs: list[dict], state: str = "active", error: str | None = None) -> dict:
    snap: dict = {"state": state, "runs": runs}
    if error:
        snap["error"] = error
    return snap


def _active(cw, workflow: str, runs: list[dict]) -> dict:
    return {workflow: _snap(runs)}


class TestClassify:
    def test_hourly_inside_grace_is_not_dispatched(self, cw):
        # Last success 15:20. The 15:07 fire is only ~1h old, under the 3h grace.
        runs = [_run("2026-10-17T15:20:00Z")]
        d = cw.classify("pages.yml", "hourly", ["7 * * * *"], runs, NOW)
        assert d.action == "skip"
        assert "inside grace" in d.reason

    def test_hourly_older_than_grace_is_dispatched(self, cw):
        # Last success 11:00. The 12:07 fire is more than 3h before 16:00 and unserved.
        runs = [_run("2026-10-17T11:00:00Z")]
        d = cw.classify("pages.yml", "hourly", ["7 * * * *"], runs, NOW)
        assert d.action == "dispatch"
        assert "12:07Z" in d.reason
        assert d.overdue_hours > 3

    def test_hourly_in_progress_is_left_alone(self, cw):
        runs = [_run("2026-10-17T15:50:00Z", conclusion=None, status="in_progress")]
        d = cw.classify("aviation-opensky.yml", "hourly", ["25 * * * *"], runs, NOW)
        assert d.action == "skip"
        assert "in progress" in d.reason

    def test_queued_run_is_left_alone(self, cw):
        runs = [_run("2026-10-17T15:55:00Z", conclusion=None, status="queued")]
        d = cw.classify("lthcs-news-hourly.yml", "hourly", ["19 * * * *"], runs, NOW)
        assert d.action == "skip"
        assert "queued" in d.reason

    def test_failure_covers_the_fire_so_it_is_not_retried(self, cw):
        runs = [_run("2026-10-17T15:10:00Z", conclusion="failure")]
        d = cw.classify("pages.yml", "hourly", ["7 * * * *"], runs, NOW)
        assert d.action == "skip"
        assert "not retrying" in d.reason or "already attempted" in d.reason

    def test_cancelled_run_does_not_count_as_served(self, cw):
        runs = [
            _run("2026-10-17T11:00:00Z"),
            _run("2026-10-17T15:10:00Z", conclusion="cancelled"),
        ]
        d = cw.classify("pages.yml", "hourly", ["7 * * * *"], runs, NOW)
        assert d.action == "dispatch"

    def test_daily_before_cutoff_is_not_dispatched(self, cw):
        # 09:00 is before 07:29 + 3h. Yesterday's success covers yesterday's fire.
        runs = [_run("2026-10-16T11:12:00Z")]
        d = cw.classify("city-daily.yml", "daily", ["29 7 * * *"], runs, MORNING)
        assert d.action == "skip"
        assert "inside grace" in d.reason

    def test_daily_after_cutoff_with_no_run_today_is_dispatched(self, cw):
        runs = [_run("2026-10-16T11:12:00Z")]
        late = datetime(2026, 10, 17, 11, 0, tzinfo=UTC)  # past 07:29+3h
        d = cw.classify("city-daily.yml", "daily", ["29 7 * * *"], runs, late)
        assert d.action == "dispatch"
        assert "07:29Z" in d.reason

    def test_daily_success_today_is_not_dispatched(self, cw):
        runs = [_run("2026-10-17T11:12:00Z")]
        d = cw.classify("city-daily.yml", "daily", ["29 7 * * *"], runs, NOW)
        assert d.action == "skip"
        assert "succeeded" in d.reason


class TestPlan:
    def test_cap_prefers_hourlies_then_the_stalest_daily(self, cw):
        def old(hours: float) -> list[dict]:
            stamp = (NOW - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
            return [_run(stamp)]

        snapshots = {name: _snap([]) for name in cw.WATCHES}
        snapshots["aviation-opensky.yml"] = _snap(old(6))
        snapshots["lthcs-news-hourly.yml"] = _snap(old(6))
        snapshots["pages.yml"] = _snap(old(6))
        snapshots["city-daily.yml"] = _snap(old(40))
        snapshots["cfpb-daily.yml"] = _snap(old(30))
        snapshots["real-estate-daily.yml"] = _snap(old(20))
        snapshots["money-flow-daily.yml"] = _snap(old(10))
        # The rest keep an empty run list: never succeeded, so they also want
        # a dispatch and must lose to the explicit staleness ordering only if
        # the cap accounts for "never" as very stale. Give them a fresh success
        # so the cap choice is unambiguous.
        fresh = [_run("2026-10-17T15:30:00Z")]
        for name, (kind, _crons) in cw.WATCHES.items():
            if name in snapshots and snapshots[name]["runs"]:
                continue
            snapshots[name] = _snap(fresh if kind == "hourly" else [_run("2026-10-17T12:00:00Z")])

        decisions = cw.plan(NOW, snapshots, cap=5)
        dispatched = {d.workflow for d in decisions if d.action == "dispatch"}
        # Hourlies first, then pages, then the two stalest dailies (40h, 30h).
        # real-estate (20h) and money-flow (10h) are overdue but over the cap.
        assert dispatched == {
            "aviation-opensky.yml",
            "lthcs-news-hourly.yml",
            "pages.yml",
            "city-daily.yml",
            "cfpb-daily.yml",
        }
        for name in ("real-estate-daily.yml", "money-flow-daily.yml"):
            capped = next(d for d in decisions if d.workflow == name)
            assert capped.action == "skip" and "cap 5" in capped.reason, name

    def test_disabled_workflow_is_not_dispatched(self, cw):
        snapshots = {
            name: _snap([_run("2026-10-17T15:30:00Z")]) for name in cw.WATCHES
        }
        snapshots["pages.yml"] = _snap([_run("2026-10-17T08:00:00Z")], state="disabled_manually")
        decisions = cw.plan(NOW, snapshots, cap=5)
        pages = next(d for d in decisions if d.workflow == "pages.yml")
        assert pages.action == "skip"
        assert "disabled_manually" in pages.reason

    def test_fetch_error_fails_closed(self, cw):
        snapshots = {name: _snap([_run("2026-10-17T08:00:00Z")]) for name in cw.WATCHES}
        snapshots["pages.yml"] = {"error": "runs: HTTP 502"}
        decisions = cw.plan(NOW, snapshots, cap=5)
        pages = next(d for d in decisions if d.workflow == "pages.yml")
        assert pages.action == "error"
        assert not any(d.workflow == "pages.yml" and d.action == "dispatch" for d in decisions)

    def test_runs_on_another_branch_do_not_count(self, cw):
        runs = [_run("2026-10-17T15:20:00Z", branch="some-feature")]
        kept = cw.runs_on_ref(runs, "main")
        assert kept == []
        # With that run filtered out, the hourly job is overdue.
        d = cw.classify("pages.yml", "hourly", ["7 * * * *"], kept, NOW)
        assert d.action == "dispatch"


class TestSummaryAndFlags:
    def test_summary_names_each_decision(self, cw):
        decisions = cw.plan(NOW, {
            name: _snap([_run("2026-10-17T15:40:00Z")]) for name in cw.WATCHES
        })
        text = cw.render_summary(decisions, dry_run=True, disabled=False, cap=5, posted=[])
        assert "Dry run" in text
        assert "`pages.yml`" in text
        assert "3 hours" in text

    def test_disabled_summary_makes_no_promises_of_a_dispatch(self, cw):
        text = cw.render_summary([], dry_run=False, disabled=True, cap=5, posted=[])
        assert "Disabled" in text and "CRON_WATCHDOG_DISABLED" in text

    def test_dispatch_accepts_both_rest_status_codes(self, cw):
        assert cw.dispatch_accepted(200) and cw.dispatch_accepted(204)
        assert not cw.dispatch_accepted(422)
        assert not cw.dispatch_accepted(401)

    def test_env_flag(self, cw, monkeypatch):
        monkeypatch.setenv("CRON_WATCHDOG_DISABLED", "true")
        assert cw.env_flag("CRON_WATCHDOG_DISABLED")
        monkeypatch.setenv("CRON_WATCHDOG_DISABLED", "false")
        assert not cw.env_flag("CRON_WATCHDOG_DISABLED")

    def test_disabled_exits_without_a_token(self, cw, monkeypatch, tmp_path):
        monkeypatch.setenv("CRON_WATCHDOG_DISABLED", "true")
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
        assert cw.main([]) == 0
        assert "Disabled" in (tmp_path / "summary.md").read_text()

    def test_missing_token_is_an_error(self, cw, monkeypatch, tmp_path):
        monkeypatch.delenv("CRON_WATCHDOG_DISABLED", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
        assert cw.main(["--dry-run"]) == 1


class TestWiring:
    def test_thresholds_match_the_audit(self, cw):
        assert cw.HOURLY_GRACE == timedelta(hours=3)
        assert cw.DAILY_GRACE == timedelta(hours=3)
        assert cw.MAX_DISPATCH == 5
        watched = set(cw.WATCHES)
        assert {"aviation-opensky.yml", "lthcs-news-hourly.yml", "pages.yml"} <= watched
        for skipped in ("r2-backfill.yml", "history-backfill.yml", "codeql.yml",
                        "security-audit.yml", "cron-watchdog.yml", "trufflehog-weekly.yml"):
            assert skipped not in watched

    def test_watched_crons_match_the_workflow_files_and_can_be_dispatched(self, cw):
        for name, (kind, crons) in cw.WATCHES.items():
            doc = yaml.safe_load((WORKFLOWS / name).read_text())
            on = doc.get("on", doc.get(True))
            assert [s["cron"] for s in on["schedule"]] == list(crons), name
            assert "workflow_dispatch" in on, name
            group = doc["concurrency"]["group"]
            assert isinstance(group, str) and group, name
            assert kind in ("hourly", "daily")

    def test_no_scheduled_workflow_fires_at_minute_zero(self):
        """GitHub documents minute 0 as the congested window. Keep schedules off it."""
        for path in sorted(WORKFLOWS.glob("*.yml")):
            doc = yaml.safe_load(path.read_text()) or {}
            on = doc.get("on", doc.get(True)) or {}
            if not isinstance(on, dict):
                continue
            for entry in on.get("schedule") or []:
                cron = entry["cron"]
                minute = cron.split()[0]
                assert minute not in {"0", "00"}, f"{path.name} still fires at minute 0: {cron}"

    def test_watchdog_workflow_is_conservative(self):
        doc = yaml.safe_load((WORKFLOWS / "cron-watchdog.yml").read_text())
        on = doc.get("on", doc.get(True))
        assert "pull_request" not in on and "push" not in on
        crons = [s["cron"] for s in on["schedule"]]
        assert len(crons) >= 3
        assert all(c.split()[0] not in {"0", "00"} for c in crons)
        # Staggered: not the same minute, and not all the same hour.
        minutes = {c.split()[0] for c in crons}
        hours = {c.split()[1] for c in crons}
        assert len(minutes) == len(crons)
        assert len(hours) == len(crons)
        assert doc["permissions"] == {"contents": "read", "actions": "write"}
        assert doc["concurrency"]["group"] == "cron-watchdog"
        assert doc["concurrency"]["cancel-in-progress"] is False
        step = doc["jobs"]["watch"]["steps"][-1]
        assert "python scripts/cron_watchdog.py" in step["run"]
        assert step["env"]["GITHUB_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"
        assert "default_branch" in step["env"]["CRON_WATCHDOG_REF"]
        assert "CRON_WATCHDOG_DISABLED" in step["env"]
        text = (WORKFLOWS / "cron-watchdog.yml").read_text()
        assert "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in text
        assert "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97" in text
        # No PAT, no new secret name.
        assert "secrets." in text
        assert text.count("secrets.") == 1
        assert "CRON_JOB" not in text and "PERSONAL_ACCESS" not in text

    def test_inputs_with_defaults_are_omitted_by_the_client(self, cw):
        # Dispatch posts only `ref`. A body that includes unknown inputs is a 422
        # on workflows that declare none; defaults cover the ones that do.
        posted: dict = {}

        class Fake(cw.GitHub):
            def post(self, path, body):
                posted["path"] = path
                posted["body"] = body
                return 204, None

        status, _payload = cw.dispatch(Fake("btabiado/alpine-data", "token"), "pages.yml", "main")
        assert status == 204
        assert posted["body"] == {"ref": "main"}
        assert posted["path"].endswith("/workflows/pages.yml/dispatches")
