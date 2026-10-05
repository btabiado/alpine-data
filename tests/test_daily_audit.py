"""Offline tests for the daily audit (scripts/daily_audit_{data,report}.py).

No network: the data-audit helpers are exercised on synthetic inputs, the
report builder on fixtures, and the workflow wiring by parsing the YAML. The
Playwright half (scripts/daily_audit_ux.mjs) is not run here — only the shape
of its JSON output, via the committed example report.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "daily_audit"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "daily-audit.yml"
PAGES_YML = REPO_ROOT / ".github" / "workflows" / "pages.yml"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"_{name}_under_test", SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def dad():
    return _load("daily_audit_data")


@pytest.fixture(scope="module")
def dar():
    return _load("daily_audit_report")


UTC = timezone.utc


def T(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


# ==========================================================================
# daily_audit_data: pure helpers
# ==========================================================================

class TestCron:
    def test_daily(self, dad):
        fires = dad.cron_fire_times("17 9 * * *", T("2026-10-01T00:00"), T("2026-10-04T00:00"))
        assert fires == [T("2026-10-01T09:17"), T("2026-10-02T09:17"), T("2026-10-03T09:17")]

    def test_hourly_and_step(self, dad):
        assert len(dad.cron_fire_times("0 * * * *", T("2026-10-01T00:00"), T("2026-10-02T00:00"))) == 24
        assert len(dad.cron_fire_times("*/15 * * * *", T("2026-10-01T00:00"), T("2026-10-01T01:00"))) == 4
        assert len(dad.cron_fire_times("0 */6 * * *", T("2026-10-01T00:00"), T("2026-10-02T00:00"))) == 4

    def test_weekly_dow_and_names(self, dad):
        # 2026-10-05 is a Monday.
        mon = dad.cron_fire_times("0 8 * * 1", T("2026-10-01T00:00"), T("2026-10-12T00:00"))
        assert mon == [T("2026-10-05T08:00")]
        assert dad.cron_fire_times("0 8 * * MON", T("2026-10-01T00:00"), T("2026-10-12T00:00")) == mon
        # 0 and 7 are both Sunday (2026-10-04).
        assert dad.cron_fire_times("0 3 * * 7", T("2026-10-01T00:00"), T("2026-10-06T00:00")) == [
            T("2026-10-04T03:00")]

    def test_monthly(self, dad):
        fires = dad.cron_fire_times("0 6 1 * *", T("2026-09-15T00:00"), T("2026-11-15T00:00"))
        assert fires == [T("2026-10-01T06:00"), T("2026-11-01T06:00")]

    def test_dom_and_dow_both_restricted_are_ored(self, dad):
        # Vixie semantics: the 1st of the month OR any Monday.
        fires = dad.cron_fire_times("0 0 1 * 1", T("2026-10-01T00:00"), T("2026-10-13T00:00"))
        assert [f.day for f in fires] == [1, 5, 12]

    def test_rejects_garbage(self, dad):
        with pytest.raises(ValueError):
            dad.cron_fire_times("every day", T("2026-10-01T00:00"), T("2026-10-02T00:00"))

    def test_workflow_crons_reads_every_scheduled_workflow(self, dad):
        crons = dad.workflow_crons(REPO_ROOT / ".github" / "workflows")
        assert crons[".github/workflows/daily-audit.yml"] == ["17 9 * * *"]
        assert crons[".github/workflows/data-health.yml"] == ["0 15 * * *"]
        assert ".github/workflows/tests.yml" not in crons


class TestScheduleEvaluation:
    NOW = T("2026-10-08T20:00")

    def _runs(self, *stamps):
        return [{"created_at": s + "Z", "event": "schedule"} for s in stamps]

    def test_late_runs_still_count(self, dad):
        # GitHub here starts 07:00 crons ~6h late; every day still ran.
        runs = self._runs(*[f"2026-10-0{d}T13:0{d}:00" for d in range(1, 9)])
        out = dad.evaluate_schedule(["0 7 * * *"], runs, self.NOW)
        assert out["missed_days"] == []
        assert out["due_days"] == 7          # 10-02 .. 10-08 (window opens 10-01 20:00)
        assert out["coverage_pct"] == 100.0
        assert 360 <= out["median_delay_min"] <= 370

    def test_a_day_without_a_run_is_missed(self, dad):
        runs = self._runs("2026-10-02T07:05:00", "2026-10-03T07:05:00", "2026-10-05T07:05:00",
                          "2026-10-06T07:05:00", "2026-10-07T07:05:00", "2026-10-08T07:05:00")
        out = dad.evaluate_schedule(["0 7 * * *"], runs, self.NOW)
        assert out["missed_days"] == ["2026-10-04"]
        assert out["days_with_run"] == out["due_days"] - 1

    def test_a_run_cannot_serve_the_next_days_slot(self, dad):
        # One very late run must not paper over two due days.
        runs = self._runs("2026-10-07T06:50:00")   # 23:50 late for 10-06 07:00
        out = dad.evaluate_schedule(["0 7 * * *"], runs, T("2026-10-08T00:00"), days=3)
        assert "2026-10-06" in out["missed_days"]

    def test_hourly_coverage_and_days(self, dad):
        runs = self._runs("2026-10-07T03:10:00", "2026-10-07T09:20:00")
        out = dad.evaluate_schedule(["0 * * * *"], runs, T("2026-10-08T00:00"), days=1)
        assert out["missed_days"] == []           # 10-07 had runs
        assert out["coverage_pct"] < 20

    def test_fires_before_the_workflow_existed_are_not_due(self, dad):
        out = dad.evaluate_schedule(["0 7 * * *"], [], self.NOW, created_at=T("2026-10-08T12:00"))
        assert out["due_days"] == 0 and out["missed_days"] == []


class TestParsing:
    @pytest.mark.parametrize("raw,expect", [
        ("2026-10-04T21:11:18", "2026-10-04T21:11:18"),
        ("2026-10-04T21:11:18Z", "2026-10-04T21:11:18"),
        ("2026-10-04T21:11:07+00:00", "2026-10-04T21:11:07"),
        ("2026-10-04", "2026-10-04T00:00:00"),
        ("2026-09", "2026-09-01T00:00:00"),
        ("6/17/2026", "2026-06-17T00:00:00"),
        ("2026-10-04 17:40 UTC", "2026-10-04T17:40:00"),
        (1791135629, "2026-10-04T17:40:29"),
        ("1791135629", "2026-10-04T17:40:29"),
    ])
    def test_parse_ts(self, dad, raw, expect):
        assert dad.parse_ts(raw).strftime("%Y-%m-%dT%H:%M:%S") == expect

    @pytest.mark.parametrize("raw", [None, "", "FAA airman data Dec 31 2025 · FA", True, 42])
    def test_parse_ts_rejects_prose(self, dad, raw):
        assert dad.parse_ts(raw) is None

    def test_payload_stamps_prefers_data_date_and_build_time(self, dad):
        now = T("2026-10-04T22:00")
        s = dad.payload_stamps({"as_of": "2026-10-02", "generated_at": "2026-10-04T21:00:00Z",
                                "asOf": "prose"}, now)
        assert s["as_of_field"] == "as_of" and s["as_of_age_h"] == 70.0
        assert s["generated_field"] == "generated_at" and s["generated_age_h"] == 1.0
        # Prose asOf is skipped in favour of the next parseable key.
        s2 = dad.payload_stamps({"asOf": "FAA data Dec 2025", "data_date": "2025-12-31"}, now)
        assert s2["as_of_field"] == "data_date"

    def test_extract_inline_data(self, dad):
        html = '<script>const DATA = {"generated_at": "2026-10-04T21:11:18", "x": [1, {"y": "};"}]};\nrender();</script>'
        assert dad.extract_inline_data(html) == {"generated_at": "2026-10-04T21:11:18",
                                                 "x": [1, {"y": "};"}]}
        with pytest.raises(RuntimeError):
            dad.extract_inline_data("<html>no payload</html>")

    def test_diff_verdicts(self, dad):
        assert dad.diff_verdicts({"A": "up", "B": "up", "C": "down"},
                                 {"A": "up", "B": "down", "D": "up"}) == [
            {"source": "B", "from": "up", "to": "down"},
            {"source": "C", "from": "down", "to": None},
            {"source": "D", "from": None, "to": "up"},
        ]


class FakeSite:
    def __init__(self, data):
        self.data = data
        self.base = "https://example.invalid"

    def coingecko(self):
        return {"bitcoin": {"usd": 100_000.0}, "ethereum": {"usd": 4000.0}}


class TestSpotChecks:
    NOW = T("2026-10-04T22:00")

    def test_price_within_and_outside_tolerance(self, dad):
        ok = dad.check_price_vs_coingecko(
            FakeSite({"market": {"coinbase": {"btc": {"price_usd": 100_500, "time": "2026-10-04T21:00:00Z"}}}}),
            self.NOW, "bitcoin", "btc")
        assert ok["status"] == "ok" and ok["diff_pct"] == 0.5 and ok["page_age_h"] == 1.0
        bad = dad.check_price_vs_coingecko(
            FakeSite({"market": {"coinbase": {"eth": {"price_usd": 4100}}}}), self.NOW, "ethereum", "eth")
        assert bad["status"] == "fail" and bad["diff_pct"] == 2.5

    def test_missing_field_is_error_not_fail(self, dad):
        out = dad.check_price_vs_coingecko(FakeSite({"market": {}}), self.NOW, "bitcoin", "btc")
        assert out["status"] == "error"

    def test_live_build_age(self, dad):
        fresh = dad.check_live_build_age(FakeSite({"generated_at": "2026-10-04T21:00:00"}), self.NOW)
        stale = dad.check_live_build_age(FakeSite({"generated_at": "2026-10-04T10:00:00"}), self.NOW)
        assert fresh["status"] == "ok" and stale["status"] == "fail" and stale["age_h"] == 12.0

    def test_one_failing_check_does_not_stop_the_others(self, dad, monkeypatch):
        def boom(site, now):
            raise ConnectionError("upstream down")
        monkeypatch.setitem(dad.SPOT_CHECKS, "btc_price_vs_coingecko", boom)
        monkeypatch.setitem(dad.SPOT_CHECKS, "eth_price_vs_coingecko", lambda s, n: {"status": "ok"})
        for k in list(dad.SPOT_CHECKS):
            if k not in ("btc_price_vs_coingecko", "eth_price_vs_coingecko"):
                monkeypatch.delitem(dad.SPOT_CHECKS, k)
        out = dad.section_spot_checks(FakeSite({}), self.NOW)
        assert out["checks"]["btc_price_vs_coingecko"]["status"] == "error"
        assert "upstream down" in out["checks"]["btc_price_vs_coingecko"]["error"]
        assert out["checks"]["eth_price_vs_coingecko"]["status"] == "ok"


def test_data_audit_main_survives_every_section_failing(dad, monkeypatch, tmp_path):
    """No token, no gh, no network: the JSON is still written, every section
    carrying its own error instead of the script crashing."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(dad.shutil, "which", lambda _: None)

    def boom(*a, **k):
        raise OSError("network unreachable")
    monkeypatch.setattr(dad, "section_data_health", boom)
    monkeypatch.setattr(dad, "section_api_status", boom)
    monkeypatch.setattr(dad, "http_get", boom)
    out = tmp_path / "data.json"
    assert dad.main(["--out", str(out)]) == 0
    doc = json.loads(out.read_text())
    for section in dad.SECTIONS:
        assert section in doc, section
    assert doc["data_health"]["status"] == "error"
    assert doc["workflows"]["status"] == "error" and doc["schedules"]["status"] == "error"
    assert all(c["status"] == "error" for c in doc["spot_checks"]["checks"].values())
    assert doc["live_payloads"]["status"] == "error"


# ==========================================================================
# daily_audit_report: problems, regressions, alert verdict
# ==========================================================================

def _data(**over):
    base = {
        "kind": "data",
        "data_health": {"status": "ok", "failing_statuses": ["stale", "unknown", "missing", "unwatched", "expired"],
                        "counts": {"ok": 2}, "results": [
                            {"path": "data-a.json", "status": "ok", "age_h": 1, "limit_h": 24},
                            {"path": "data-tsa.json", "status": "suppressed", "age_h": 2000, "limit_h": 24,
                             "detail": "muted until 2026-10-18"}]},
        "workflows": {"status": "ok", "workflows": [
            {"name": "pages", "latest_24h": {"conclusion": "success"}, "failed_24h": False, "failing_2_days": False}]},
        "schedules": {"status": "ok", "schedules": [
            {"name": "pages", "crons": ["0 * * * *"], "state": "active", "due_days": 8, "due_runs": 160,
             "days_with_run": 8, "missed_days": [], "coverage_pct": 90.0, "median_delay_min": 20,
             "scheduled_runs": 150, "last_run": {"conclusion": "success", "created_at": "2026-10-04T09:00:00Z"},
             "last_failed": False}]},
        "api_status": {"status": "ok", "verdicts": {"CoinGecko": "up"}, "counts": {"up": 1},
                       "previous": None, "changes": [], "unwired_key_envs": []},
        "spot_checks": {"status": "ok", "checks": {"btc_price_vs_coingecko": {"status": "ok", "diff_pct": 0.2}}},
        "live_payloads": {"status": "ok", "index": {"name": "index.html DATA"}, "payloads": []},
    }
    base.update(over)
    return base


def _ux_page(page="v1", vp="phone", **over):
    rec = {"page": page, "path": "/", "viewport": vp, "status": 200, "console_errors": [],
           "page_errors": [], "failed_requests": [], "bad_text": [],
           "overflow": {"overflow": False, "scroll_width": 390},
           "weight": {"requests": 30, "bytes": 1_500_000},
           "tabs": ([{"id": "overview", "label": "Overview", "via": "direct", "reachable": True,
                      "switched": True}] if page in ("v1", "v2") else [])}
    rec.update(over)
    return rec


def _ux(*pages, **over):
    doc = {"kind": "ux", "pages": list(pages) or [_ux_page(), _ux_page(vp="desktop")], "duration_s": 80}
    doc.update(over)
    return doc


def _ids(problems):
    return {p["id"] for p in problems}


class TestProblems:
    def test_clean_inputs_produce_no_problems(self, dar):
        assert dar.data_problems(_data()) == []
        assert dar.ux_problems(_ux()) == []

    def test_feed_failure_is_p0_and_suppressed_is_not_a_problem(self, dar):
        d = _data()
        d["data_health"]["results"].append({"path": "data-b.json", "status": "stale", "age_h": 50, "limit_h": 24})
        probs = dar.data_problems(d)
        assert [(p["id"], p["severity"]) for p in probs] == [("feed:data-b.json", "P0")]
        assert "STALE" in probs[0]["title"]

    def test_extra_data_health_records_are_judged_too(self, dar):
        d = _data()
        d["data_health"]["extra_failing"] = [
            {"section": "continuity", "path": "data/btc_flows.csv", "status": "stale", "detail": "gap"}]
        assert ("feed:continuity:data/btc_flows.csv", "P0") in {
            (p["id"], p["severity"]) for p in dar.data_problems(d)}

    def test_workflow_one_day_is_p1_two_days_is_p0_with_annotation(self, dar):
        jobs = [{"job": "run", "failed_steps": ["Fetch TSA"], "annotations": [
            {"level": "failure", "title": "", "message": "Process completed with exit code 1."},
            {"level": "warning", "title": "", "message": "Node.js 20 is deprecated. blah"},
            {"level": "warning", "title": "TSA not refreshed", "message": "tsa.gov 403"}]}]
        d = _data(workflows={"status": "ok", "workflows": [
            {"name": "aviation-tsa", "latest_24h": {"url": "u1"}, "failed_24h": True, "failing_2_days": True,
             "failed_jobs": jobs},
            {"name": "data-health", "latest_24h": {"url": "u2"}, "failed_24h": True, "failing_2_days": False,
             "failed_jobs": []}]})
        probs = {p["id"]: p for p in dar.data_problems(d)}
        assert probs["workflow:aviation-tsa:failing-2d"]["severity"] == "P0"
        assert "TSA not refreshed: tsa.gov 403" in probs["workflow:aviation-tsa:failing-2d"]["detail"]
        assert "Node.js" not in probs["workflow:aviation-tsa:failing-2d"]["detail"]
        assert probs["workflow:data-health:failed"]["severity"] == "P1"

    def test_schedule_flags(self, dar):
        s = _data()["schedules"]["schedules"][0]
        d = _data(schedules={"status": "ok", "schedules": [
            {**s, "name": "city-daily", "crons": ["0 7 * * *"], "due_days": 7, "due_runs": 7,
             "missed_days": ["2026-10-02"], "median_delay_min": 380, "coverage_pct": 85.7},
            {**s, "name": "security-audit", "crons": ["0 9 * * 1"], "due_days": 1, "due_runs": 1,
             "last_failed": True, "last_run": {"created_at": "2026-09-28T17:12:36Z", "url": "u"}},
            {**s, "name": "pages", "coverage_pct": 19.0},
            {**s, "name": "old", "state": "disabled_inactivity"},
        ]})
        probs = {p["id"]: p for p in dar.data_problems(d)}
        assert probs["schedule:city-daily:missed"]["severity"] == "P1"
        assert probs["schedule:city-daily:late"]["severity"] == "P2"
        assert probs["schedule:security-audit:failing"]["severity"] == "P1"
        assert probs["schedule:pages:dropped"]["severity"] == "P2"
        assert probs["schedule:old:disabled"]["severity"] == "P1"
        # Daily crons are not judged on tick coverage (one tick a day).
        assert "schedule:city-daily:dropped" not in probs

    def test_schedule_failure_not_double_counted_with_workflow_failure(self, dar):
        s = _data()["schedules"]["schedules"][0]
        d = _data(workflows={"status": "ok", "workflows": [
                      {"name": "aviation-tsa", "failed_24h": True, "failing_2_days": False}]},
                  schedules={"status": "ok", "schedules": [{**s, "name": "aviation-tsa", "last_failed": True}]})
        ids = _ids(dar.data_problems(d))
        assert "workflow:aviation-tsa:failed" in ids and "schedule:aviation-tsa:failing" not in ids

    def test_spot_checks_and_api(self, dar):
        d = _data(spot_checks={"status": "ok", "checks": {
                      "live_site_build_age": {"status": "fail", "age_h": 9, "max_age_h": 6},
                      "btc_price_vs_coingecko": {"status": "fail", "page": 1, "reference": 2, "diff_pct": 50},
                      "treasury_10y_vs_treasury_gov": {"status": "error", "error": "timeout"}}},
                  api_status={"status": "ok", "verdicts": {"A": "down", "B": "degraded", "C": "up"},
                              "unwired_key_envs": []})
        probs = {p["id"]: p["severity"] for p in dar.data_problems(d)}
        assert probs == {"spot:live_site_build_age": "P0", "spot:btc_price_vs_coingecko": "P1",
                         "api:A": "P1", "api:B": "P2"}

    def test_unreachable_tabs_are_p0_and_grouped(self, dar):
        tabs = [{"id": "etf", "label": "ETF Flows", "reachable": False, "problem": "tap target covered",
                 "covered_by": "div.card", "via": "menu", "switched": True},
                {"id": "cpi", "label": "CPI", "reachable": False, "problem": "tap target covered",
                 "via": "menu", "switched": True},
                {"id": "lthcs", "label": "LTHCS", "reachable": True, "switched": False},
                {"id": "summit", "label": "Summit", "skipped": "navigates away"}]
        probs = dar.ux_problems(_ux(_ux_page(tabs=tabs), _ux_page(vp="desktop")))
        by = {p["id"]: p for p in probs}
        assert by["ux:v1:phone:tab-unreachable:etf"]["severity"] == "P0"
        assert by["ux:v1:phone:tab-unreachable:etf"]["group"] == by["ux:v1:phone:tab-unreachable:cpi"]["group"]
        assert by["ux:v1:phone:tab-dead:lthcs"]["severity"] == "P0"
        assert not any("summit" in i for i in by)
        lines = dar.render_problem_lines(probs)
        assert any("(2): ETF Flows, CPI" in ln or "(2): CPI, ETF Flows" in ln for ln in lines)

    def test_ux_findings_dedupe_across_viewports(self, dar):
        err = {"text": "TypeError: x is undefined", "step": "tab:etf"}
        req = {"url": "https://btabiado.github.io/alpine-data/data-x.json", "status": 404, "step": "load"}
        bad = {"match": "NaN", "where": "#tab-etf", "text": "Flow NaN%", "step": "tab:etf"}
        csp = {"text": "The Content Security Policy directive 'frame-ancestors' is ignored when delivered via a <meta> element.",
               "step": "load"}
        pages = [_ux_page(vp=v, page_errors=[err], failed_requests=[req], bad_text=[bad], console_errors=[csp])
                 for v in ("phone", "desktop")]
        probs = dar.ux_problems(_ux(*pages))
        sev = {p["id"].split(":")[2]: p["severity"] for p in probs}
        assert sev == {"js-exception": "P0", "http": "P1", "bad-text": "P1", "console": "P2"}
        assert all(p["viewports"] == ["phone", "desktop"] for p in probs)

    def test_page_load_failure_overflow_and_weight(self, dar):
        probs = dar.ux_problems(_ux(
            _ux_page(page="real-estate", status=503),
            _ux_page(page="lthcs", overflow={"overflow": True, "scroll_width": 645,
                                             "offenders": [{"element": "table.x", "right": 645}]},
                     weight={"requests": 245, "bytes": 700_000}),
            _ux_page(page="lthcs", vp="desktop", overflow={"overflow": True, "scroll_width": 1500})))
        sev = {p["id"]: p["severity"] for p in probs}
        assert sev["ux:real-estate:phone:load"] == "P0"
        assert sev["ux:lthcs:phone:overflow"] == "P2"
        assert sev["ux:lthcs:heavy"] == "P2"
        assert "ux:lthcs:desktop:overflow" not in sev   # overflow is judged at 390px only

    def test_missing_or_crashed_halves_are_problems(self, dar):
        assert _ids(dar.data_problems(None)) == {"audit:data-missing"}
        assert _ids(dar.ux_problems(None)) == {"audit:ux-missing"}
        assert "audit:ux-fatal" in _ids(dar.ux_problems({"pages": [], "fatal": "browser died"}))
        assert "audit:workflows" in _ids(dar.data_problems(_data(workflows={"status": "error", "error": "401"})))


class TestRegressions:
    def _prev(self, dar, problems, day="2026-10-03"):
        return {"date": day, "problems": problems}

    def test_new_ongoing_fixed_and_first_seen(self, dar):
        prev = self._prev(dar, [
            {"id": "a", "severity": "P1", "category": "x", "title": "A", "first_seen": "2026-09-30"},
            {"id": "b", "severity": "P0", "category": "x", "title": "B"}])
        cur = [{"id": "a", "severity": "P1", "category": "x", "title": "A"},
               {"id": "c", "severity": "P0", "category": "x", "title": "C"}]
        diff = dar.diff_problems(cur, prev, "2026-10-04")
        assert diff["new"] == ["c"] and diff["ongoing"] == ["a"]
        assert [f["id"] for f in diff["fixed"]] == ["b"]
        st = {p["id"]: (p["state"], p["first_seen"]) for p in cur}
        assert st == {"a": ("ongoing", "2026-09-30"), "c": ("new", "2026-10-04")}

    def test_no_previous_report_means_everything_is_new(self, dar):
        cur = [{"id": "a", "severity": "P2", "category": "x", "title": "A"}]
        diff = dar.diff_problems(cur, None, "2026-10-04")
        assert diff["new"] == ["a"] and diff["previous_date"] is None

    def test_a_section_that_did_not_run_does_not_read_as_fixed(self, dar):
        prev = self._prev(dar, [
            {"id": "workflow:tsa:failing-2d", "severity": "P0", "category": "workflow", "title": "t"},
            {"id": "ux:v1:phone:tab-unreachable:etf", "severity": "P0", "category": "ux", "title": "u"},
            {"id": "spot:btc_price_vs_coingecko", "severity": "P1", "category": "spot", "title": "s"}])
        data = _data(workflows={"status": "error", "error": "API down"})
        ux = _ux(_ux_page(skipped="time budget exhausted"), _ux_page(page="v2"))
        cur: list = []
        diff = dar.diff_problems(cur, prev, "2026-10-04", dar.unverified_prefixes(data, ux))
        assert set(diff["unverified"]) == {"workflow:tsa:failing-2d", "ux:v1:phone:tab-unreachable:etf"}
        assert [f["id"] for f in diff["fixed"]] == ["spot:btc_price_vs_coingecko"]
        assert all(p["unverified"] and p["state"] == "ongoing" for p in cur)

    @pytest.mark.parametrize("problems,expect", [
        ([], "close"),
        ([{"severity": "P1", "state": "new"}], "close"),
        ([{"severity": "P0", "state": "ongoing"}, {"severity": "P1", "state": "new"}], "update"),
        ([{"severity": "P0", "state": "ongoing"}, {"severity": "P0", "state": "new"}], "open"),
    ])
    def test_alert_action(self, dar, problems, expect):
        assert dar.alert_action(problems) == expect


class TestReportFiles:
    def _run(self, dar, tmp_path, monkeypatch, data, ux, day):
        (tmp_path / "data.json").write_text(json.dumps(data))
        (tmp_path / "ux.json").write_text(json.dumps(ux))
        gh_out = tmp_path / "gh_output"
        monkeypatch.setenv("GITHUB_OUTPUT", str(gh_out))
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        out_dir = tmp_path / "audit" / "daily"
        rc = dar.main(["--data", str(tmp_path / "data.json"), "--ux", str(tmp_path / "ux.json"),
                       "--out-dir", str(out_dir), "--date", day,
                       "--issue-body", str(tmp_path / "issue.md")])
        assert rc == 0
        outputs = dict(ln.split("=", 1) for ln in gh_out.read_text().splitlines() if "=" in ln)
        gh_out.unlink()
        return out_dir, outputs

    def test_two_days_end_to_end(self, dar, tmp_path, monkeypatch):
        broken_tab = [{"id": "etf", "label": "ETF Flows", "reachable": False, "problem": "tap target covered",
                       "via": "menu", "switched": True}]
        # Day 1: a P0 (unreachable tab) -> open the issue.
        out_dir, o1 = self._run(dar, tmp_path, monkeypatch, _data(),
                                _ux(_ux_page(tabs=broken_tab), _ux_page(vp="desktop")), "2026-10-03")
        assert o1 == {"status": "RED", "alert_action": "open", "p0": "1", "new_p0": "1"}
        assert sorted(f.name for f in out_dir.iterdir()) == [
            "2026-10-03.json", "2026-10-03.md", "latest.json", "latest.md"]
        assert "ETF Flows" in (tmp_path / "issue.md").read_text()

        # Day 2: same P0 + a new P1 -> update only.
        d2 = _data(workflows={"status": "ok", "workflows": [
            {"name": "cfpb-daily", "latest_24h": {"url": "u"}, "failed_24h": True, "failing_2_days": False}]})
        _, o2 = self._run(dar, tmp_path, monkeypatch, d2,
                          _ux(_ux_page(tabs=broken_tab), _ux_page(vp="desktop")), "2026-10-04")
        assert o2["alert_action"] == "update" and o2["new_p0"] == "0"
        rep = json.loads((out_dir / "2026-10-04.json").read_text())
        assert rep["regressions"]["previous_date"] == "2026-10-03"
        assert rep["regressions"]["new"] == ["workflow:cfpb-daily:failed"]
        tab = next(p for p in rep["problems"] if p["id"] == "ux:v1:phone:tab-unreachable:etf")
        assert tab["state"] == "ongoing" and tab["first_seen"] == "2026-10-03"
        assert json.loads((out_dir / "latest.json").read_text()) == rep

        # Day 3: everything fixed -> close; the fixed list names both.
        _, o3 = self._run(dar, tmp_path, monkeypatch, _data(), _ux(), "2026-10-05")
        assert o3 == {"status": "GREEN", "alert_action": "close", "p0": "0", "new_p0": "0"}
        md = (out_dir / "2026-10-05.md").read_text()
        assert md.index("## New since yesterday") < md.index("## Ongoing") < md.index("## Fixed since yesterday")
        fixed = md.split("## Fixed since yesterday")[1].split("##")[0]
        assert "ETF Flows" in fixed and "cfpb-daily" in fixed

    def test_previous_is_the_newest_strictly_earlier_dated_report(self, dar, tmp_path):
        d = tmp_path / "daily"
        d.mkdir()
        for day in ("2026-10-01", "2026-10-03", "2026-10-04"):
            (d / f"{day}.json").write_text(json.dumps({"date": day, "problems": []}))
        (d / "latest.json").write_text(json.dumps({"date": "2026-10-04", "problems": []}))
        assert dar.find_previous(d, "2026-10-04")["date"] == "2026-10-03"
        assert dar.find_previous(d, "2026-10-01") is None

    def test_prune_keeps_ninety_days(self, dar, tmp_path):
        d = tmp_path / "daily"
        d.mkdir()
        today = date(2026, 10, 4)
        for back in (0, 89, 90, 91, 200):
            day = (today - timedelta(days=back)).isoformat()
            (d / f"{day}.json").write_text("{}")
            (d / f"{day}.md").write_text("")
        (d / "latest.json").write_text("{}")
        removed = dar.prune(d, today.isoformat(), 90)
        assert sorted(removed) == sorted(
            f"{(today - timedelta(days=b)).isoformat()}.{ext}" for b in (91, 200) for ext in ("json", "md"))
        assert (d / "latest.json").exists()
        assert (d / f"{(today - timedelta(days=90)).isoformat()}.json").exists()


class TestExampleFixture:
    """The committed example is a real report produced from a live run."""

    @pytest.fixture(scope="class")
    def example(self):
        return json.loads((FIXTURES / "example_report.json").read_text())

    def test_shape(self, example, dar):
        assert example["schema"] == 1
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", example["date"])
        assert set(example["summary"]) >= {"status", "p0", "p1", "p2", "new", "new_p0", "ongoing", "fixed"}
        for p in example["problems"]:
            assert {"id", "severity", "category", "title", "state", "first_seen"} <= set(p)
            assert p["severity"] in ("P0", "P1", "P2")
        assert example["alert_action"] == dar.alert_action(example["problems"])
        assert example["ux"]["pages"] and example["data"]["schedules"]["schedules"]

    def test_example_problems_are_reproducible_from_its_raw_sections(self, example, dar):
        rebuilt = dar.build_report(example["data"], example["ux"], None, example["date"])
        assert _ids(rebuilt["problems"]) == _ids(example["problems"])

    def test_example_markdown_is_the_rendering_of_the_json(self, example, dar):
        assert (FIXTURES / "example_report.md").read_text() == dar.render_md(example)


# ==========================================================================
# workflow wiring
# ==========================================================================

@pytest.fixture(scope="module")
def wf():
    return yaml.safe_load(WORKFLOW.read_text())


def test_workflow_runs_daily_and_on_demand(wf):
    on = wf.get("on", wf.get(True))
    assert [s["cron"] for s in on["schedule"]] == ["17 9 * * *"]
    assert "workflow_dispatch" in on


def test_workflow_permissions_are_minimal(wf):
    assert wf["permissions"] == {"contents": "write", "issues": "write", "actions": "read",
                                 "checks": "read"}


def test_workflow_actions_are_pinned_to_shas(wf):
    uses = [s["uses"] for s in wf["jobs"]["audit"]["steps"] if "uses" in s]
    assert uses
    for u in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", u), u
    text = WORKFLOW.read_text()
    assert "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0 # v7.0.0" in text
    assert "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1 # v6.3.0" in text


def _step(wf, needle):
    hits = [s for s in wf["jobs"]["audit"]["steps"] if needle in (s.get("run") or "")]
    assert len(hits) == 1, needle
    return hits[0]


def test_audits_cannot_skip_the_report(wf):
    """A crashed audit must be reported, not skip the report and the alarm."""
    for needle in ("daily_audit_data.py", "daily_audit_ux.mjs", "playwright@"):
        assert _step(wf, needle).get("continue-on-error") is True, needle
    report = _step(wf, "daily_audit_report.py")
    assert "continue-on-error" not in report
    assert "--keep-days 90" in report["run"] and "--out-dir audit/daily" in report["run"]


def test_report_is_committed_signed_with_skip_ci(wf):
    commit = _step(wf, "api-commit.sh")
    assert "git add -A audit/daily" in commit["run"]   # -A: stage the prune's deletions
    assert "[skip ci]" in commit["run"]
    assert commit["env"]["GH_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"


def test_issue_lifecycle_is_wired(wf):
    steps = wf["jobs"]["audit"]["steps"]
    opener = next(s for s in steps if "gh issue create" in (s.get("run") or ""))
    closer = next(s for s in steps if "gh issue close" in (s.get("run") or ""))
    assert "alert_action == 'open'" in opener["if"] and "alert_action == 'update'" in opener["if"]
    assert "alert_action == 'close'" in closer["if"]
    assert "gh label create daily-audit" in opener["run"]
    assert wf["env"]["ISSUE_TITLE"] == "Daily audit: action needed"


def test_pages_publishes_the_latest_report():
    pages = yaml.safe_load(PAGES_YML.read_text())
    stage = [s for s in pages["jobs"]["build"]["steps"]
             if "cp dashboard.html _site/index.html" in (s.get("run") or "")]
    assert len(stage) == 1
    run = stage[0]["run"]
    assert "for f in audit/daily/latest.md audit/daily/latest.json; do" in run
    assert 'cp "$f" _site/audit/' in run


def test_api_changes_fall_back_to_the_previous_report(dar):
    prev = {"date": "2026-10-03", "problems": [],
            "data": {"api_status": {"generated_at": "g0", "verdicts": {"A": "up", "B": "up"}}}}
    d = _data(api_status={"status": "ok", "verdicts": {"A": "up", "B": "down"}, "previous": None,
                          "unwired_key_envs": []})
    rep = dar.build_report(d, _ux(), prev, "2026-10-04")
    assert rep["api_changes"] == [{"source": "B", "from": "up", "to": "down"}]
    assert rep["api_baseline"].startswith("report 2026-10-03")
    assert "B: up → down" in dar.render_md(rep)
    # No baseline at all is said plainly, not reported as "no changes".
    rep2 = dar.build_report(d, _ux(), None, "2026-10-04")
    assert rep2["api_baseline"] is None
    assert "no snapshot from ~24h earlier" in dar.render_md(rep2)


def test_unknown_data_health_extensions_are_still_judged(dad):
    failing = {"stale", "missing"}
    extra = {
        "continuity": [{"path": "data/btc_flows.csv", "status": "stale", "detail": "gap 2026-09"},
                       {"path": "data/eth_flows.csv", "status": "ok"}],
        "history": {"composites": {"status": "missing", "detail": "no file"},
                    "lthcs": {"status": "ok"}},
        "note": "free text",
    }
    got = [r for k, v in extra.items() for r in dad.collect_failing(v, k, failing)]
    assert {(r["section"], r.get("path") or r.get("name"), r["status"]) for r in got} == {
        ("continuity", "data/btc_flows.csv", "stale"), ("history", "composites", "missing")}


def test_tab_strip_shrinking_or_vanishing_is_flagged(dar):
    tabs = [{"id": f"t{i}", "label": f"T{i}", "reachable": True, "switched": True} for i in range(5)]
    prev = {"date": "2026-10-03", "problems": [], "ux": _ux(_ux_page(tabs=tabs), _ux_page(vp="desktop", tabs=tabs))}
    today = _ux(_ux_page(tabs=tabs[:2]), _ux_page(vp="desktop", tabs=[]))
    rep = dar.build_report(_data(), today, prev, "2026-10-04")
    sev = {p["id"]: p["severity"] for p in rep["problems"]}
    assert sev["ux:v1:phone:tab-count"] == "P1"
    assert sev["ux:v1:desktop:no-tabs"] == "P1"
    assert "ux:v1:desktop:tab-count" not in sev   # zero is reported as no-tabs, once


def test_pages_stage_step_survives_missing_audit_report():
    """The stage step runs under `shopt -s nullglob`; a `latest.*` glob that
    matches nothing made `ls` succeed and `cp` fail with no source, which
    broke the 2026-10-04 deploy before the first audit report existed."""
    import subprocess
    import tempfile
    from pathlib import Path

    import yaml

    repo = Path(__file__).resolve().parent.parent
    wf = yaml.safe_load((repo / ".github/workflows/pages.yml").read_text())
    step = next(s for s in wf["jobs"]["build"]["steps"]
                if s.get("name") == "Stage site directory")
    run = step["run"]
    start = run.index("for f in audit/daily/latest.md")
    snippet = run[start:run.index("done", start) + len("done")]
    with tempfile.TemporaryDirectory() as d:
        script = "set -e\nshopt -s nullglob\nmkdir -p _site\n" + snippet + "\n"
        r = subprocess.run(["bash", "-c", script], cwd=d, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert not (Path(d) / "_site/audit").exists()
        (Path(d) / "audit/daily").mkdir(parents=True)
        (Path(d) / "audit/daily/latest.md").write_text("x")
        r = subprocess.run(["bash", "-c", script], cwd=d, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert (Path(d) / "_site/audit/latest.md").exists()
