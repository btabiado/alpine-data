"""History-continuity tests (``scripts/history_continuity.py`` and its wiring
into ``scripts/data_health.py``).

The freshness monitor asks "is the newest point recent". It cannot see a hole
in the middle, and it could not see that every archived composite carried
whale_sentiment_* = null for two months, because a file full of nulls is still
a fresh file. These tests pin the other axis: a missing day/month, a doubled
period, or a null required field must FAIL, unless health/known_gaps.json
discloses it as unfillable, in which case it is reported and does not fail.

Everything is offline: fixture trees under tmp_path, a throwaway git repo for
the `git` source, and an in-memory R2 coverage report.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"


def _load(name: str):
    """Load a script by path (scripts/ is not a package), reusing an instance
    another test module already loaded so monkeypatches hit the module the
    code under test actually reads its globals from."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def hc():
    return _load("history_continuity")


@pytest.fixture(scope="module")
def dh():
    return _load("data_health")


@pytest.fixture(autouse=True)
def _no_live_site(dh, monkeypatch):
    def _offline(url):
        raise dh.LiveFetchError("live fetch disabled in tests")
    monkeypatch.setattr(dh, "_fetch_deployed", _offline)


TODAY = date(2026, 10, 4)


def _files(root: Path, rel: str, days, payload=None):
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    for day in days:
        body = payload(day) if callable(payload) else (payload or {"as_of": day})
        (d / f"{day}.json").write_text(json.dumps(body))


def _days(a: str, b: str) -> list[str]:
    from datetime import timedelta
    x, out = date.fromisoformat(a), []
    while x <= date.fromisoformat(b):
        out.append(x.isoformat())
        x += timedelta(days=1)
    return out


# ==========================================================================
# periods and dates
# ==========================================================================

def test_parse_period_accepts_the_shapes_this_repo_writes(hc):
    assert hc.parse_period("2026-09-14", hc.DAILY) == "2026-09-14"
    assert hc.parse_period("2026-09-14T23:51:07Z", hc.DAILY) == "2026-09-14"
    assert hc.parse_period("6/7/2026", hc.DAILY) == "2026-06-07"       # TSA
    assert hc.parse_period("2026-09", hc.MONTHLY) == "2026-09"          # city
    assert hc.parse_period("2026-09-30", hc.MONTHLY) == "2026-09"


def test_parse_period_refuses_to_guess(hc):
    for junk in ("", "Sept 2026", "2026-13-01", "2026-09", None, 20260914):
        assert hc.parse_period(junk, hc.DAILY) is None, junk


def test_trading_days_skip_weekends_and_nyse_holidays(hc):
    # 2026-09-04 Fri, 09-07 Labor Day, 09-08 Tue
    got = hc.expected_periods("2026-09-04", "2026-09-08", hc.TRADING)
    assert got == ["2026-09-04", "2026-09-08"]
    assert "2025-01-09" not in hc.expected_periods("2025-01-08", "2025-01-10", hc.TRADING)


def test_monthly_periods_cross_the_year(hc):
    assert hc.expected_periods("2025-11", "2026-02", hc.MONTHLY) == [
        "2025-11", "2025-12", "2026-01", "2026-02"]


def test_compress_keeps_non_adjacent_days_apart(hc):
    assert hc.compress(["2026-08-21", "2026-08-19", "2026-08-20", "2026-09-02"]) == [
        "2026-08-19..2026-08-21", "2026-09-02"]


# ==========================================================================
# files source (data/composites/, LTHCS)
# ==========================================================================

def test_missing_day_inside_the_window_is_a_gap(hc, tmp_path):
    days = [d for d in _days("2026-09-01", "2026-10-04") if d not in ("2026-09-14", "2026-09-16")]
    _files(tmp_path, "snaps/", days)
    f = hc.check("x", hc.History("snaps/", hc.DAILY), tmp_path, TODAY)
    assert f.missing == ["2026-09-14", "2026-09-16"]
    assert not f.ok


def test_gap_older_than_the_window_is_not_judged(hc, tmp_path):
    days = [d for d in _days("2026-07-01", "2026-10-04") if d != "2026-07-10"]
    _files(tmp_path, "snaps/", days)
    f = hc.check("x", hc.History("snaps/", hc.DAILY, window_days=35), tmp_path, TODAY)
    assert f.ok, f.missing


def test_the_trailing_edge_is_the_freshness_checks_job(hc, tmp_path):
    """A history that STOPPED is stale, and the freshness check says so.
    Continuity only judges holes between entries, or it would page twice."""
    _files(tmp_path, "snaps/", _days("2026-09-01", "2026-09-20"))
    f = hc.check("x", hc.History("snaps/", hc.DAILY), tmp_path, TODAY)
    assert f.ok and f.last == "2026-09-20"


def test_history_entirely_older_than_the_window_is_a_note_not_a_failure(hc, tmp_path):
    _files(tmp_path, "snaps/", _days("2026-06-01", "2026-06-17"))
    f = hc.check("x", hc.History("snaps/", hc.DAILY), tmp_path, TODAY)
    assert f.ok and f.note and "predates" in f.note


def test_empty_or_absent_history_is_an_error(hc, tmp_path):
    (tmp_path / "empty").mkdir()
    assert hc.check("x", hc.History("empty/", hc.DAILY), tmp_path, TODAY).error
    assert hc.check("x", hc.History("absent/", hc.DAILY), tmp_path, TODAY).error


def test_non_dated_members_are_ignored(hc, tmp_path):
    _files(tmp_path, "snaps/", _days("2026-09-20", "2026-10-04"))
    (tmp_path / "snaps" / "index.json").write_text("{}")
    (tmp_path / "snaps" / "latest.json").write_text("{}")
    assert hc.check("x", hc.History("snaps/", hc.DAILY), tmp_path, TODAY).ok


def test_a_null_required_field_is_a_gap_even_when_every_file_exists(hc, tmp_path):
    """The exact failure behind two months of whale_sentiment_* = null."""
    _files(tmp_path, "c/", _days("2026-09-25", "2026-10-04"),
           lambda d: {"indexes": {"a": {"score": 1}, "whale": None}})
    f = hc.check("x", hc.History("c/", hc.DAILY, required_fields=("indexes.whale",)),
                 tmp_path, TODAY)
    assert not f.missing and len(f.field_gaps) == 10
    assert "indexes.whale null on 10: 2026-09-25..2026-10-04" in hc.summarize(f)


def test_required_field_since_ignores_days_before_the_key_existed(hc, tmp_path):
    _files(tmp_path, "c/", _days("2026-09-25", "2026-10-04"),
           lambda d: {"indexes": {"new": {"score": 1} if d >= "2026-10-02" else None}})
    f = hc.check("x", hc.History("c/", hc.DAILY,
                                 required_fields=("indexes.new@2026-10-02",)),
                 tmp_path, TODAY)
    assert f.ok


# ==========================================================================
# csv source (ETF flows, equity ETF flows, history CSVs)
# ==========================================================================

def _csv(root: Path, rel: str, header: str, rows: list[str]):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(header + "\n" + "\n".join(rows) + "\n")


def test_trading_csv_flags_a_missing_session_but_not_a_holiday(hc, tmp_path):
    days = [d for d in hc.expected_periods("2026-08-31", "2026-10-02", hc.TRADING)
            if d != "2026-09-15"]
    _csv(tmp_path, "f.csv", "date,Total", [f"{d},1" for d in days])
    f = hc.check("x", hc.History("f.csv", hc.TRADING, "csv"), tmp_path, TODAY)
    assert f.missing == ["2026-09-15"]     # Labor Day 09-07 is not expected


def test_csv_duplicates_respect_key_fields(hc, tmp_path):
    rows = ["2026-10-01,SPY,1", "2026-10-01,QQQ,1", "2026-10-02,SPY,1", "2026-10-02,SPY,2"]
    _csv(tmp_path, "e.csv", "date,ticker,v", rows)
    f = hc.check("x", hc.History("e.csv", hc.TRADING, "csv", key_fields=("ticker",)),
                 tmp_path, TODAY)
    assert f.duplicates == ["2026-10-02 SPY x2"]
    # ...and without the key, two tickers on one day are NOT duplicates of
    # each other only if the spec says so: one row per date is the default.
    f2 = hc.check("x", hc.History("e.csv", hc.TRADING, "csv"), tmp_path, TODAY)
    assert "2026-10-01 x2" in f2.duplicates


def test_csv_without_the_date_column_is_an_error_not_a_pass(hc, tmp_path):
    _csv(tmp_path, "f.csv", "day,Total", ["2026-10-01,1"])
    assert hc.check("x", hc.History("f.csv", hc.DAILY, "csv"), tmp_path, TODAY).error


# ==========================================================================
# json source (city, real estate, TSA)
# ==========================================================================

def test_grouped_monthly_series_names_the_series_with_the_hole(hc, tmp_path):
    doc = {"cities": [
        {"feeds": [{"dataset": "aaaa-1111", "series": [{"month": m} for m in
                    ("2026-05", "2026-06", "2026-08", "2026-09")]},
                   {"dataset": "bbbb-2222", "series": [{"month": m} for m in
                    ("2026-05", "2026-06", "2026-07")]}]}]}
    (tmp_path / "city.json").write_text(json.dumps(doc))
    f = hc.check("x", hc.History("city.json", hc.MONTHLY, "json", rows="cities[].feeds[]",
                                 series_key="series", date_field="month",
                                 group="dataset"), tmp_path, TODAY)
    assert f.missing == ["aaaa-1111: 2026-07"]


def test_json_rows_that_are_bare_date_strings(hc, tmp_path):
    doc = {"metros": [{"h": {"labels": ["2026-06", "2026-07", "2026-09"]}}]}
    (tmp_path / "re.json").write_text(json.dumps(doc))
    f = hc.check("x", hc.History("re.json", hc.MONTHLY, "json", rows="metros[].h",
                                 series_key="labels", date_field=""), tmp_path, TODAY)
    assert f.missing == ["#0: 2026-08"]


def test_tsa_us_dates_are_understood(hc, tmp_path):
    doc = {"series": [{"d": "9/28/2026"}, {"d": "9/29/2026"}, {"d": "10/1/2026"}]}
    (tmp_path / "tsa.json").write_text(json.dumps(doc))
    f = hc.check("x", hc.History("tsa.json", hc.DAILY, "json", series_key="series",
                                 date_field="d"), tmp_path, TODAY)
    assert f.missing == ["#0: 2026-09-30"]


# ==========================================================================
# git source (files overwritten in place: the commit record IS the archive)
# ==========================================================================

def _git(root: Path, *args, env=None):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                   env={**os.environ, **(env or {})})


def test_git_source_flags_a_day_without_a_commit(hc, tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    for day in ("2026-09-28", "2026-09-29", "2026-10-01", "2026-10-01"):
        (tmp_path / "feed.json").write_text(json.dumps({"d": day, "n": os.urandom(4).hex()}))
        _git(tmp_path, "add", "feed.json")
        stamp = f"{day}T12:00:00+00:00"
        _git(tmp_path, "commit", "-q", "-m", day,
             env={"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp})
    f = hc.check("x", hc.History("feed.json", hc.DAILY, "git"), tmp_path, TODAY)
    assert f.missing == ["2026-09-30"]
    assert not f.duplicates, "two commits on one day are one overwritten file"


def test_git_source_in_a_shallow_or_missing_repo_is_skipped_not_failed(hc, tmp_path):
    """One commit reads as 'every day missing'. That says nothing about the
    feed, so it must not page anyone from a shallow CI checkout..."""
    (tmp_path / "feed.json").write_text("{}")
    f = hc.check("x", hc.History("feed.json", hc.DAILY, "git"), tmp_path, TODAY)
    assert f.skipped and not f.missing and not f.error


def test_the_run_that_owns_git_history_checks_out_full_history():
    """...which is only safe because data-health.yml really does fetch the
    whole history. If this line goes, the daily-commit check goes blind."""
    import yaml
    wf = yaml.safe_load((REPO_ROOT / ".github/workflows/data-health.yml").read_text())
    steps = wf["jobs"]["check"]["steps"]
    co = next(s for s in steps if str(s.get("uses", "")).startswith("actions/checkout"))
    assert (co.get("with") or {}).get("fetch-depth") == 0


# ==========================================================================
# r2 source (published coverage of the R2 archive)
# ==========================================================================

def _coverage(files: dict[str, list[str]]) -> dict:
    return {"categories": {"x": [{"file": k, "present": {d: True for d in v}}
                                 for k, v in files.items()]}}


def test_r2_gap_and_a_file_that_stopped_uploading(hc, tmp_path):
    every = _days("2026-09-25", "2026-10-03")
    cov = _coverage({
        "data-a.json": [d for d in every if d != "2026-09-30"],
        "data-b.json": _days("2026-09-25", "2026-10-01"),   # stopped
    })
    f = hc.check("R2 archive", hc.History("health/r2-coverage.json", hc.DAILY, "r2"),
                 tmp_path, TODAY, r2_coverage=cov)
    assert "data-a.json: 2026-09-30" in f.missing
    assert {"data-b.json: 2026-10-02", "data-b.json: 2026-10-03"} <= set(f.missing)


def test_r2_report_that_is_empty_is_an_error(hc, tmp_path):
    f = hc.check("R2 archive", hc.History("health/r2-coverage.json", hc.DAILY, "r2"),
                 tmp_path, TODAY, r2_coverage={"empty_reason": "R2_BUCKET_NAME unset"})
    assert f.error


# ==========================================================================
# known gaps: disclosed, never failed; but they must justify themselves
# ==========================================================================

def test_known_gap_is_disclosed_instead_of_failing(hc, tmp_path):
    days = [d for d in _days("2026-09-01", "2026-10-04") if d != "2026-09-14"]
    _files(tmp_path, "snaps/", days)
    spec = hc.History("snaps/", hc.DAILY, label="daily")
    gaps = [{"feed": "x", "history": "daily", "start": "2026-09-14", "end": "2026-09-14"}]
    f = hc.check("x", spec, tmp_path, TODAY, gaps)
    assert f.ok and f.disclosed == ["2026-09-14"]


def test_a_field_disclosure_does_not_hide_a_missing_file(hc, tmp_path):
    days = [d for d in _days("2026-09-01", "2026-10-04") if d != "2026-09-14"]
    _files(tmp_path, "snaps/", days)
    spec = hc.History("snaps/", hc.DAILY, label="daily")
    gaps = [{"feed": "x", "history": "daily", "field": "indexes.poc",
             "start": "2026-09-01", "end": "2026-10-04"}]
    assert hc.check("x", spec, tmp_path, TODAY, gaps).missing == ["2026-09-14"]


def test_verify_known_gaps_rejects_vague_or_dangling_entries(hc):
    specs = {"data/composites/": (hc.History("data/composites/", hc.DAILY,
                                             label="daily snapshots"),)}
    good = {"feed": "data/composites/", "history": "daily snapshots",
            "start": "2026-08-19", "end": "2026-08-21",
            "reason": "no build ran; nothing was captured on these days",
            "backfill_attempted": ["git", "r2"]}
    assert hc.verify_known_gaps([good], specs) == []
    bad = [
        {**good, "feed": "nope.json"},
        {**good, "history": "nope"},
        {**good, "start": "Aug 19"},
        {**good, "start": "2026-08-22"},
        {**good, "reason": "x"},
        {k: v for k, v in good.items() if k != "backfill_attempted"},
    ]
    for b in bad:
        assert hc.verify_known_gaps([b], specs), b


def test_the_committed_known_gaps_file_is_valid(dh, hc):
    gaps = hc.load_known_gaps(REPO_ROOT)
    assert gaps, "health/known_gaps.json is missing or empty"
    assert hc.verify_known_gaps(gaps, dh.history_specs()) == []


# ==========================================================================
# wiring into data_health.py
# ==========================================================================

def test_manifest_history_specs_are_structurally_sound(dh):
    assert dh.verify_manifest() == []
    specs = dh.history_specs()
    for rel in ("data/composites/", "data/btc_flows.csv", "data/eth_flows.csv",
                "data/equity_etf_flows.csv", "data/lthcs/", "data-cfpb.json",
                "data/stock_money_flow_history.csv",
                "data/travel_advisory_levels.csv", "R2 archive"):
        assert rel in specs, f"{rel} has no history spec"


def test_composites_require_the_indexes_the_cards_chart(dh):
    req = dh.MANIFEST["data/composites/"].history[0].required_fields
    for key in ("whale_sentiment_btc", "whale_sentiment_eth", "poc_signal_breadth",
                "money_flow_index", "etf_flow_sentiment_btc"):
        assert any(r.split("@")[0] == f"indexes.{key}" for r in req), key


def test_bad_history_spec_is_a_manifest_problem(dh, monkeypatch):
    bad = dh.Feed(dh.COMMITTED, "someone", history=(
        dh.History("data/x.csv", "fortnightly", "csv"),
        dh.History("data/x.csv", dh.DAILY, "files")))
    monkeypatch.setitem(dh.MANIFEST, "data/x.csv", bad)
    problems = " ".join(dh.verify_manifest())
    assert "fortnightly" in problems and "must end with '/'" in problems


def _history_rows(dh, results, owner):
    return [r for r in results if r.path.startswith(f"{owner} [history:")]


def test_gap_fails_and_disclosed_does_not(dh, tmp_path, monkeypatch):
    monkeypatch.setattr(dh, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(dh, "MANIFEST", {
        "data/snaps/": dh.Feed(dh.SERIES, "owner", history=(
            dh.History("data/snaps/", dh.DAILY, label="daily"),)),
        "data/other/": dh.Feed(dh.SERIES, "owner", history=(
            dh.History("data/other/", dh.DAILY, label="daily"),)),
    })
    monkeypatch.setattr(dh, "ARCHIVES", {})
    days = [d for d in _days("2026-09-01", "2026-10-04") if d != "2026-09-14"]
    _files(tmp_path, "data/snaps/", days)
    _files(tmp_path, "data/other/", days)
    (tmp_path / "health").mkdir()
    (tmp_path / "health" / "known_gaps.json").write_text(json.dumps({"gaps": [{
        "feed": "data/other/", "history": "daily", "start": "2026-09-14",
        "end": "2026-09-14", "reason": "a documented, unfillable missing day here",
        "backfill_attempted": ["git"]}]}))
    rows = dh.evaluate_history(TODAY)
    snaps = _history_rows(dh, rows, "data/snaps/")[0]
    other = _history_rows(dh, rows, "data/other/")[0]
    assert snaps.status == dh.GAP and snaps.fails and "2026-09-14" in snaps.detail
    assert other.status == dh.DISCLOSED and not other.fails


def test_history_runs_only_in_committed_mode_and_can_be_skipped(dh, monkeypatch):
    calls = []
    monkeypatch.setattr(dh, "evaluate_history", lambda today=None: calls.append(1) or [])
    dh.evaluate(dh.BUILT)
    dh.evaluate(dh.COMMITTED, history=False)
    assert calls == []
    dh.evaluate(dh.COMMITTED)
    assert calls == [1]


def test_unreachable_r2_report_is_unknown_not_ok(dh, tmp_path, monkeypatch):
    monkeypatch.setattr(dh, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(dh, "MANIFEST", {})
    rows = dh.evaluate_history(TODAY)
    r2 = _history_rows(dh, rows, "R2 archive")[0]
    assert r2.status == dh.UNKNOWN and r2.fails


def test_live_suppression_mutes_history_too(dh, tmp_path, monkeypatch):
    monkeypatch.setattr(dh, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(dh, "ARCHIVES", {})
    monkeypatch.setattr(dh, "MANIFEST", {"data/snaps/": dh.Feed(
        dh.SERIES, "owner", history=(dh.History("data/snaps/", dh.DAILY),))})
    monkeypatch.setattr(dh, "SUPPRESSIONS", {"data/snaps/": dh.Suppression(
        reason="x" * 50, until=date(2026, 12, 1))})
    _files(tmp_path, "data/snaps/",
           [d for d in _days("2026-09-01", "2026-10-04") if d != "2026-09-14"])
    row = _history_rows(dh, dh.evaluate_history(TODAY), "data/snaps/")[0]
    assert row.status == dh.SUPPRESSED and not row.fails


def test_history_rows_render_in_text_and_issue_reports(dh, tmp_path, monkeypatch):
    monkeypatch.setattr(dh, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(dh, "ARCHIVES", {})
    monkeypatch.setattr(dh, "MANIFEST", {"data/snaps/": dh.Feed(
        dh.SERIES, "owner", history=(dh.History("data/snaps/", dh.DAILY),))})
    _files(tmp_path, "data/snaps/",
           [d for d in _days("2026-09-01", "2026-10-04") if d != "2026-09-14"])
    rows = dh.evaluate_history(TODAY)
    text = dh.render_text(rows, [])
    assert "HISTORY GAP" in text and "2026-09-14" in text and "? old" not in text
    assert "data/snaps/ [history:" in dh.render_issue(rows, [])
