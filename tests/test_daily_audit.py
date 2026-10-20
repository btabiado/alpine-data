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
        assert crons[".github/workflows/data-health.yml"] == ["11 15 * * *"]
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

    def test_schedule_only_history_reports_scheduler_coverage_separately(self, dad):
        runs = self._runs(*[f"2026-10-0{d}T13:0{d}:00" for d in range(1, 9)])
        out = dad.evaluate_schedule(["0 7 * * *"], runs, self.NOW)
        assert out["coverage_pct"] == out["schedule_coverage_pct"] == 100.0
        assert out["catchup_ticks"] == 0 and out["catchup_runs"] == 0
        assert out["schedule_median_delay_min"] == out["median_delay_min"]

    def test_watchdog_catchup_covers_a_tick_and_stays_labeled(self, dad):
        # No schedule events. A bot dispatch 95 minutes after 07:00 each judged day.
        catch = [{"created_at": f"2026-10-0{d}T08:35:00Z", "event": "workflow_dispatch",
                  "actor": {"login": "github-actions[bot]"}} for d in range(2, 9)]
        out = dad.evaluate_schedule(["0 7 * * *"], [], self.NOW, catchup_runs=catch)
        assert out["missed_days"] == []
        assert out["coverage_pct"] == 100.0
        assert out["schedule_coverage_pct"] == 0.0
        assert out["catchup_ticks"] == out["due_runs"]
        assert out["catchup_runs"] == out["due_runs"]
        assert out["median_delay_min"] == 95
        assert out["schedule_median_delay_min"] is None
        assert out["scheduled_runs"] == 0

    def test_catchup_that_lands_first_does_not_hide_a_later_schedule_delivery(self, dad):
        # Catch-up at 08:35, scheduler finally delivers at 13:00. Both cover the
        # same daily tick. Freshness delay is the catch-up; scheduler delay stays visible.
        sched = self._runs("2026-10-08T13:00:00")
        catch = [{"created_at": "2026-10-08T08:35:00Z"}]
        out = dad.evaluate_schedule(["0 7 * * *"], sched, self.NOW, days=1, catchup_runs=catch)
        assert out["coverage_pct"] == 100.0
        assert out["schedule_coverage_pct"] == 100.0
        assert out["catchup_ticks"] == 1
        assert out["median_delay_min"] == 95
        assert out["schedule_median_delay_min"] == 360

    def test_human_dispatch_is_not_a_watchdog_catchup(self, dad):
        assert dad.is_watchdog_catchup({
            "event": "workflow_dispatch", "actor": {"login": "github-actions[bot]"}})
        assert dad.is_watchdog_catchup({
            "event": "workflow_dispatch", "triggering_actor": {"login": "github-actions[bot]"}})
        assert not dad.is_watchdog_catchup({
            "event": "workflow_dispatch", "actor": {"login": "octocat"}})
        assert not dad.is_watchdog_catchup({
            "event": "schedule", "actor": {"login": "github-actions[bot]"}})
        assert not dad.is_watchdog_catchup({
            "event": "workflow_run", "actor": {"login": "github-actions[bot]"}})
        assert dad.WATCHDOG_ACTOR == "github-actions[bot]"


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


def _pt(dad, stamp, price):
    return (dad.parse_ts(stamp), price)


class FakeSite:
    def __init__(self, data, charts=None):
        self.data = data
        self.base = "https://example.invalid"
        self.charts = charts or {}
        self.chart_calls: list[str] = []

    def coingecko_chart(self, coin):
        self.chart_calls.append(coin)
        if coin not in self.charts:
            raise AssertionError(f"unexpected chart fetch {coin}")
        return self.charts[coin]


class TestSpotChecks:
    NOW = T("2026-10-04T22:00")

    def test_price_within_and_outside_tolerance(self, dad):
        # A later sample (the live spot the old check used) is 3% away. The
        # reference has to be the sample next to the payload timestamp.
        charts = {
            "bitcoin": [_pt(dad, "2026-10-04T21:00:00Z", 100_000.0),
                        _pt(dad, "2026-10-04T22:00:00Z", 103_000.0)],
            "ethereum": [_pt(dad, "2026-10-04T21:00:00Z", 4_000.0),
                         _pt(dad, "2026-10-04T22:00:00Z", 4_000.0)],
        }
        ok = dad.check_price_vs_coingecko(
            FakeSite({"market": {"coinbase": {"btc": {"price_usd": 100_500,
                                                      "time": "2026-10-04T21:00:00Z"}}}}, charts),
            self.NOW, "bitcoin", "btc")
        assert ok["status"] == "ok" and ok["diff_pct"] == 0.5 and ok["page_age_h"] == 1.0
        assert ok["reference"] == 100_000.0 and ok["reference_offset_s"] == 0
        assert "reason" not in ok
        assert "market_chart" in ok["source"] and "no key" in ok["source"]
        assert "payload timestamp" in ok["note"]
        bad = dad.check_price_vs_coingecko(
            FakeSite({"market": {"coinbase": {"eth": {"price_usd": 4100,
                                                      "time": "2026-10-04T21:00:00Z"}}}}, charts),
            self.NOW, "ethereum", "eth")
        assert bad["status"] == "fail" and bad["diff_pct"] == 2.5
        assert bad["reason"] == "price_mismatch"
        assert bad["page_age_h"] == 1.0 and "payload timestamp" in bad["note"]

    def test_rally_after_the_print_is_not_a_mismatch(self, dad):
        """2026-10-20 shape: the page's Coinbase print matches CoinGecko at
        13:50Z, and both venues are ~1.8% higher by the time the audit runs."""
        now = T("2026-10-20T14:18:18")
        charts = {"bitcoin": [
            _pt(dad, "2026-10-20T13:45:00Z", 81_574.08),
            _pt(dad, "2026-10-20T13:50:00Z", 81_840.35),
            _pt(dad, "2026-10-20T14:15:00Z", 83_122.18),
        ]}
        out = dad.check_price_vs_coingecko(
            FakeSite({"market": {"coinbase": {"btc": {"price_usd": 81634.01,
                                                      "time": "2026-10-20T13:47:40Z"}}}}, charts),
            now, "bitcoin", "btc")
        assert out["status"] == "ok"
        assert out["page_age_h"] == 0.51
        assert out["reference"] == 81_840.35
        assert out["reference_time"] == "2026-10-20T13:50:00Z"
        assert out["reference_offset_s"] == 140
        assert out["diff_pct"] == 0.252
        assert "reason" not in out

    def test_stale_quote_fails_even_when_it_matched(self, dad):
        charts = {"bitcoin": [_pt(dad, "2026-10-04T10:00:00Z", 80_000.0)]}
        out = dad.check_price_vs_coingecko(
            FakeSite({"market": {"coinbase": {"btc": {"price_usd": 80_000,
                                                      "time": "2026-10-04T10:00:00Z"}}}}, charts),
            self.NOW, "bitcoin", "btc")
        assert out["status"] == "fail" and out["reason"] == "stale"
        assert out["page_age_h"] == 12.0 and out["diff_pct"] == 0.0
        assert out["max_age_h"] == 6.0
        assert "too old" in out["note"]

    def test_stale_and_wrong_are_both_reported(self, dad):
        charts = {"ethereum": [_pt(dad, "2026-10-04T10:00:00Z", 4_000.0)]}
        out = dad.check_price_vs_coingecko(
            FakeSite({"market": {"coinbase": {"eth": {"price_usd": 4500,
                                                      "time": "2026-10-04T10:00:00Z"}}}}, charts),
            self.NOW, "ethereum", "eth")
        assert out["status"] == "fail" and out["reason"] == "price_mismatch+stale"
        assert out["diff_pct"] == 12.5

    def test_missing_timestamp_is_stale_and_does_not_fetch(self, dad):
        site = FakeSite({"market": {"coinbase": {"eth": {"price_usd": 4100}}}},
                        {"ethereum": [_pt(dad, "2026-10-04T22:00:00Z", 4_000.0)]})
        out = dad.check_price_vs_coingecko(site, self.NOW, "ethereum", "eth")
        assert out["status"] == "fail" and out["reason"] == "stale"
        assert out["page_age_h"] is None and "diff_pct" not in out
        assert site.chart_calls == []
        assert "no timestamp" in out["note"]

    def test_chart_gap_is_an_error_not_a_price_fail(self, dad):
        charts = {"bitcoin": [_pt(dad, "2026-10-04T18:00:00Z", 90_000.0)]}
        out = dad.check_price_vs_coingecko(
            FakeSite({"market": {"coinbase": {"btc": {"price_usd": 100_000,
                                                      "time": "2026-10-04T21:00:00Z"}}}}, charts),
            self.NOW, "bitcoin", "btc")
        assert out["status"] == "error"
        assert "within" in out["error"] and "diff_pct" not in out

    def test_old_quote_outside_the_chart_is_stale(self, dad):
        charts = {"bitcoin": [_pt(dad, "2026-10-04T18:00:00Z", 90_000.0)]}
        out = dad.check_price_vs_coingecko(
            FakeSite({"market": {"coinbase": {"btc": {"price_usd": 80_000,
                                                      "time": "2026-10-04T08:00:00Z"}}}}, charts),
            self.NOW, "bitcoin", "btc")
        assert out["status"] == "fail" and out["reason"] == "stale"
        assert "diff_pct" not in out

    def test_missing_field_is_error_not_fail(self, dad):
        out = dad.check_price_vs_coingecko(FakeSite({"market": {}}), self.NOW, "bitcoin", "btc")
        assert out["status"] == "error"

    def test_chart_url_is_keyless_and_rejects_odd_ids(self, dad):
        url = dad.coingecko_chart_url("bitcoin")
        assert url.startswith("https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?")
        assert "vs_currency=usd" in url and "days=1" in url and "interval" not in url
        with pytest.raises(ValueError):
            dad.coingecko_chart_url("bitcoin/../admin")

    def test_parse_chart_drops_junk_and_reads_epoch_millis(self, dad):
        pts = dad.parse_coingecko_chart({"prices": [
            [1792503900000, 81574.08],
            ["nope", 1],
            [1792504200000, 0],
            [1792504200000, "81840.35"],
        ]})
        assert len(pts) == 2
        assert pts[0][1] == 81574.08
        assert pts[0][0] == dad.parse_ts(1792503900000)
        assert pts[1][1] == 81840.35

    def test_chart_fetch_is_cached_per_coin(self, dad, monkeypatch):
        calls = []

        def fake_get(url, **kw):
            calls.append(url)
            return {"prices": [[1792503900000, 81574.08]]}

        monkeypatch.setattr(dad, "get_json", fake_get)
        site = dad.LiveSite("https://example.invalid")
        assert site.coingecko_chart("bitcoin") == site.coingecko_chart("bitcoin")
        assert len(calls) == 1 and "days=1" in calls[0] and "interval" not in calls[0]

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

    def test_retired_upstream_is_not_a_blocked_finding(self, dar):
        """bitinfocharts is a by-design retirement (2026-10-19 audit), not a
        new outage. A still-blocked source on the same snapshot stays a P2."""
        d = _data()
        d["api_status"]["verdicts"] = {
            "bitinfocharts": "retired",
            "Farside (ETF flows)": "blocked",
        }
        probs = dar.data_problems(d)
        ids = {p["id"] for p in probs}
        assert "api:bitinfocharts" not in ids
        assert "api:Farside (ETF flows)" in ids

    def test_feed_failure_is_p0_and_suppressed_is_not_a_problem(self, dar):
        d = _data()
        d["data_health"]["results"].append({"path": "data-b.json", "status": "stale", "age_h": 50, "limit_h": 24})
        probs = dar.data_problems(d)
        assert [(p["id"], p["severity"]) for p in probs] == [("feed:data-b.json", "P0")]
        assert "STALE" in probs[0]["title"]

    def test_tsa_upstream_lag_is_p2_and_does_not_page(self, dar):
        """A disclosed freshness row is an upstream that has not published.
        A known history gap uses the same status and stays out of the list.
        Neither one is a P0, so the tracking issue closes."""
        d = _data()
        d["data_health"]["results"].extend([
            {"path": "data-tsa.json", "status": "disclosed", "age_h": 72, "limit_h": 48,
             "detail": "upstream lag: source newest date 2026-10-15 equals this file's as_of",
             "owner": "aviation-tsa.yml (daily 14:10Z)"},
            {"path": "data-cfpb.json [history: daily commits]", "status": "disclosed",
             "detail": "disclosed 1: 2026-10-12"},
        ])
        probs = dar.data_problems(d)
        assert [(p["id"], p["severity"]) for p in probs] == [
            ("feed:data-tsa.json:upstream-lag", "P2")]
        assert "upstream lag" in probs[0]["title"]
        assert "3d" in probs[0]["title"] and "2d" in probs[0]["title"]
        assert probs[0]["detail"].startswith("upstream lag")
        assert dar.alert_action(probs) == "close"

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

    def test_catchups_count_as_freshness_and_stay_labeled(self, dar):
        s = _data()["schedules"]["schedules"][0]
        covered = {**s, "name": "pages", "coverage_pct": 92.0, "schedule_coverage_pct": 18.0,
                   "due_runs": 160, "due_days": 8, "catchup_ticks": 120, "catchup_runs": 120,
                   "median_delay_min": 90, "schedule_median_delay_min": 360,
                   "scheduled_runs": 30,
                   "last_catchup": {"created_at": "2026-10-17T16:40:00Z", "conclusion": "success",
                                    "event": "workflow_dispatch"}}
        still_thin = {**s, "name": "aviation-opensky", "coverage_pct": 40.0,
                      "schedule_coverage_pct": 18.0, "due_runs": 160, "due_days": 8,
                      "catchup_ticks": 35, "scheduled_runs": 29, "median_delay_min": 80}
        d = _data(schedules={"status": "ok", "schedules": [covered, still_thin]})
        probs = {p["id"]: p for p in dar.data_problems(d)}
        # Effective freshness is above the dropped-tick line, so no P2.
        assert "schedule:pages:dropped" not in probs
        # Scheduler lateness is disclosed, but the effective start is inside 3h.
        assert "schedule:pages:late" not in probs
        dropped = probs["schedule:aviation-opensky:dropped"]
        assert "watchdog catch-up" in dropped["title"]
        assert "scheduler delivered 18%" in dropped["detail"]
        assert "35 tick" in dropped["detail"]
        md = dar.render_md(dar.build_report(d, _ux(), None, "2026-10-17"))
        assert "92% (scheduler 18%)" in md
        assert "1.5h (scheduler 6.0h)" in md
        assert "catch-up 120" in md
        assert "catch-up 2026-10-17T16:40 success" in md
        # One paragraph, not four list lines. A missing comma between the
        # source literals would split this sentence across newlines.
        note = (
            "A catch-up is a `workflow_dispatch` by `github-actions[bot]` (the cron watchdog), "
            "not a GitHub schedule event. Ticks run and median delay count it toward freshness; "
            "the scheduler figure is what the schedule event delivered on its own."
        )
        assert note in md.splitlines()

    def test_schedule_failure_not_double_counted_with_workflow_failure(self, dar):
        s = _data()["schedules"]["schedules"][0]
        d = _data(workflows={"status": "ok", "workflows": [
                      {"name": "aviation-tsa", "failed_24h": True, "failing_2_days": False}]},
                  schedules={"status": "ok", "schedules": [{**s, "name": "aviation-tsa", "last_failed": True}]})
        ids = _ids(dar.data_problems(d))
        assert "workflow:aviation-tsa:failed" in ids and "schedule:aviation-tsa:failing" not in ids

    def test_accepted_quality_gap_is_not_an_email_or_repo_audit_error(self, dar):
        """A red Actions run for the monthly audit stops paging once this day's
        recorded verdict says it does not fail. Other workflows are unchanged.
        The accepted gap stays in the markdown."""
        s = _data()["schedules"]["schedules"][0]
        d = _data(
            quality_audit={
                "status": "ok",
                "workflow": "lthcs-quality-audit-monthly",
                "verdict_fails": False,
                "overall_verdict": "DEGRADED",
                "summary": "data/lthcs/quality_audit/latest_summary.json",
                "known_gaps": [{
                    "pillar": "thesis_integrity",
                    "verdict": "KNOWN GAP (accepted)",
                    "decided": "2026-10-17",
                    "review_by": "2027-04-17",
                    "accepted": True,
                }],
            },
            workflows={"status": "ok", "workflows": [
                {"name": "lthcs-quality-audit-monthly", "latest_24h": {"url": "u", "conclusion": "failure"},
                 "failed_24h": True, "failing_2_days": True, "failed_jobs": []},
                {"name": "other", "latest_24h": {"url": "u2"}, "failed_24h": True, "failing_2_days": False},
            ]},
            schedules={"status": "ok", "schedules": [
                {**s, "name": "lthcs-quality-audit-monthly", "last_failed": True,
                 "last_run": {"created_at": "2026-10-01T15:53:00Z", "url": "u", "conclusion": "failure"}},
            ]},
        )
        ids = _ids(dar.data_problems(d))
        assert "workflow:lthcs-quality-audit-monthly:failing-2d" not in ids
        assert "workflow:lthcs-quality-audit-monthly:failed" not in ids
        assert "schedule:lthcs-quality-audit-monthly:failing" not in ids
        assert "workflow:other:failed" in ids
        report = dar.build_report(d, _ux(), None, "2026-10-17")
        md = dar.render_md(report)
        assert "FAILING" not in md
        assert "ended failed (lthcs-quality-audit-monthly)" not in md
        assert "thesis_integrity is KNOWN GAP (accepted)" in md
        assert "decided 2026-10-17" in md
        assert "review by 2027-04-17" in md

    def test_quality_audit_still_pages_when_the_verdict_is_a_failure(self, dar):
        s = _data()["schedules"]["schedules"][0]
        d = _data(
            quality_audit={
                "status": "ok",
                "workflow": "lthcs-quality-audit-monthly",
                "verdict_fails": True,
                "overall_verdict": "CRITICAL",
            },
            workflows={"status": "ok", "workflows": [
                {"name": "lthcs-quality-audit-monthly", "latest_24h": {"url": "u"},
                 "failed_24h": True, "failing_2_days": True, "failed_jobs": []},
            ]},
            schedules={"status": "ok", "schedules": [
                {**s, "name": "lthcs-quality-audit-monthly", "last_failed": True,
                 "last_run": {"created_at": "2026-10-01T15:53:00Z", "url": "u"}},
            ]},
        )
        ids = _ids(dar.data_problems(d))
        assert "workflow:lthcs-quality-audit-monthly:failing-2d" in ids
        # The same red run is not listed twice.
        assert "schedule:lthcs-quality-audit-monthly:failing" not in ids

    def test_quality_audit_pages_when_the_verdict_was_not_recorded(self, dar):
        """No quality_audit section (historical fixtures) keeps paging."""
        s = _data()["schedules"]["schedules"][0]
        d = _data(
            workflows={"status": "ok", "workflows": [
                {"name": "lthcs-quality-audit-monthly", "latest_24h": {"url": "u"},
                 "failed_24h": True, "failing_2_days": False, "failed_jobs": []},
            ]},
            schedules={"status": "ok", "schedules": [
                {**s, "name": "lthcs-quality-audit-monthly", "last_failed": True,
                 "last_run": {"created_at": "2026-10-01T15:53:00Z", "url": "u"}},
            ]},
        )
        assert "quality_audit" not in d
        ids = _ids(dar.data_problems(d))
        assert "workflow:lthcs-quality-audit-monthly:failed" in ids
        # Flag present but not exactly false still pages.
        d["quality_audit"] = {"status": "ok", "workflow": "lthcs-quality-audit-monthly"}
        ids = _ids(dar.data_problems(d))
        assert "workflow:lthcs-quality-audit-monthly:failed" in ids

    def test_quality_verdict_reader_follows_audit_failed(self, dad, tmp_path):
        missing = dad.section_quality_audit(tmp_path)
        assert missing["verdict_fails"] is True
        assert missing["status"] == "error"
        summary = tmp_path / "data" / "lthcs" / "quality_audit"
        summary.mkdir(parents=True)
        (summary / "latest_summary.json").write_text("{")
        broken = dad.section_quality_audit(tmp_path)
        assert broken["verdict_fails"] is True
        accepted = {
            "overall_verdict": "DEGRADED",
            "known_gaps": [{
                "pillar": "thesis_integrity",
                "verdict": "KNOWN GAP (accepted)",
                "decided": "2026-10-17",
                "review_by": "2027-04-17",
                "reason": "accepted gap",
                "accepted": True,
                "mismatch": False,
            }],
            "pillars": {"thesis_integrity": {"verdict": "KNOWN GAP (accepted)", "coverage_pct": 0.0}},
            "distribution": {"critical": False},
        }
        (summary / "latest_summary.json").write_text(json.dumps(accepted))
        ok = dad.section_quality_audit(tmp_path)
        assert ok["verdict_fails"] is False
        assert ok["workflow"] == "lthcs-quality-audit-monthly"
        assert ok["overall_verdict"] == "DEGRADED"
        assert ok["known_gaps"][0]["pillar"] == "thesis_integrity"
        assert ok["known_gaps"][0]["verdict"] == "KNOWN GAP (accepted)"
        accepted["pillars"]["adoption_momentum"] = {"verdict": "STUB"}
        (summary / "latest_summary.json").write_text(json.dumps(accepted))
        assert dad.section_quality_audit(tmp_path)["verdict_fails"] is True

    def test_known_gap_verdict_is_not_a_data_health_failure(self, dad):
        failing = dad._failing_statuses(REPO_ROOT)
        lowered = {s.lower() for s in failing}
        assert "known gap (accepted)" not in lowered
        assert "critical" not in lowered
        recs = dad.collect_failing(
            {"quality_audit": {"status": "KNOWN GAP (accepted)", "verdict": "KNOWN GAP (accepted)"}},
            "quality_audit",
            failing,
        )
        assert recs == []

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

    def test_price_titles_say_which_failure_and_name_the_source(self, dar):
        mismatch = dar.spot_title("btc_price_vs_coingecko", {
            "status": "fail", "page": 81634.01, "reference": 80000.0, "diff_pct": 2.043,
            "page_age_h": 0.51, "reason": "price_mismatch",
            "source": "CoinGecko market_chart days=1 (nearest to payload time, no key)"})
        assert "2.043% off CoinGecko at the payload time" in mismatch
        assert "payload 31m old" in mismatch and "price_mismatch" not in mismatch
        assert "market_chart" in mismatch and "no key" in mismatch
        stale = dar.spot_title("eth_price_vs_coingecko", {
            "status": "fail", "page_age_h": 7.71, "max_age_h": 6.0, "reason": "stale",
            "source": "CoinGecko market_chart days=1 (nearest to payload time, no key)"})
        assert stale.startswith("ETH price on the site is stale")
        assert "8h" in stale and "6h" in stale and "market_chart" in stale
        both = dar.spot_title("btc_price_vs_coingecko", {
            "status": "fail", "page": 100, "reference": 80, "diff_pct": 25.0,
            "page_age_h": 12, "reason": "price_mismatch+stale",
            "source": "CoinGecko market_chart days=1 (nearest to payload time, no key)"})
        assert "25.0% off CoinGecko at the payload time" in both
        assert "quote is stale" in both and "payload 12h old" in both

    def test_price_problem_records_age_source_and_reason(self, dar):
        d = _data(spot_checks={"status": "ok", "checks": {
            "btc_price_vs_coingecko": {
                "status": "fail", "page": 81634.01, "reference": 80000.0,
                "diff_pct": 2.043, "page_age_h": 0.51, "tolerance_pct": 1.0,
                "reason": "price_mismatch",
                "source": "CoinGecko market_chart days=1 (nearest to payload time, no key)",
                "note": "wrong print"}}})
        p = next(x for x in dar.data_problems(d) if x["id"] == "spot:btc_price_vs_coingecko")
        assert p["severity"] == "P1"
        assert "payload 31m old" in p["title"] and "market_chart" in p["title"]
        assert "page_age_h=0.51" in p["detail"]
        assert "reason=price_mismatch" in p["detail"]
        assert "source=CoinGecko market_chart days=1" in p["detail"]

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

    def test_exit_tab_switch_is_navigation_not_a_panel(self, dar):
        """Summit is a link. switched:true means the tap left the dashboard;
        a missing `switched` used to be read as 'did not register as switched'."""
        gone = [{"id": "summit", "label": "Summit↗", "exit": True, "reachable": True,
                 "opened": "tap", "switched": True,
                 "navigated_to": "https://btabiado.github.io/alpine-data/landscape/?pres=absent"}]
        assert dar.ux_problems(_ux(_ux_page(tabs=gone), _ux_page(vp="desktop", tabs=gone))) == []
        stuck = [{"id": "summit", "label": "Summit↗", "exit": True, "reachable": True,
                  "opened": "tap", "switched": False, "problem": "tap did not leave the dashboard"}]
        probs = dar.ux_problems(_ux(_ux_page(tabs=stuck)))
        assert [p["id"] for p in probs] == ["ux:v1:phone:tab-dead:summit"]
        assert probs[0]["severity"] == "P0"
        assert "does not leave the dashboard" in probs[0]["title"]
        assert "panel" not in probs[0]["title"]

    def test_ux_script_checks_exit_navigation(self):
        src = (SCRIPTS / "daily_audit_ux.mjs").read_text(encoding="utf-8")
        # The old tap test skipped Summit before tapping and never set
        # `switched`, which the 2026-10-17 email read as a failed switch.
        assert "entry.skipped = 'navigates away from the dashboard'" not in src
        assert "export async function confirmExitTab" in src
        assert "waitUntil: 'commit'" in src
        assert "entry.switched = dest !== before" in src
        assert "entry.exit = true" in src

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
    # Each pin carries its human-readable version as a trailing comment.
    # Match the shape, not specific versions, so Dependabot bumps pass.
    text = WORKFLOW.read_text()
    for action in ("actions/checkout", "actions/setup-python"):
        assert re.search(rf"{action}@[0-9a-f]{{40}} # v\d+(\.\d+)*\b", text), action


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
