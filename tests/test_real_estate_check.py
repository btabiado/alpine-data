"""Real-estate freshness is the housing month plus a successful-check record.

Audit 2026-10-15 reported ``data/real_estate.json`` STALE (9d old, limit 2d,
source ``generated_at``) while real-estate-daily was succeeding. The workflow
skips the snapshot commit when only ``generated_at`` / ``fetched_at`` moved,
so that clock stays on the last month the vendors actually published. The
2026-10-06 commit was the previous substance change; 14a7513 on 2026-10-16
was the next one (September's month landed). The days between were checks,
not a dead feed.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def bhs():
    return _load("build_health_status")


@pytest.fixture(scope="module")
def fre(bhs):
    # fetch_real_estate reuses the already-loaded monitor.
    return _load("fetch_real_estate")


def _snap(**over):
    doc = {
        "generated_at": "2026-10-06T13:03:57Z",
        "sources": {
            "zillow": {"fetched_at": "2026-10-06T13:03:57Z", "rows": 894,
                       "last_modified": "Mon, 06 Oct 2026 01:00:00 GMT"},
            "redfin": {"fetched_at": "2026-10-06T13:03:57Z", "rows": 878,
                       "last_modified": "Tue, 02 Jun 2026 18:16:11 GMT"},
            "fred": {"fetched_at": "2026-10-06T13:03:57Z", "national_permits_yoy_pct": 4.0},
        },
        "metros": [{
            "history_5y_monthly": {"labels": ["2025-09", "2026-07", "2026-08"]},
        }],
    }
    doc.update(over)
    return doc


def _write(path: Path, doc: dict) -> Path:
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_housing_month_outranks_a_stale_fetch_clock(bhs, tmp_path):
    """The 2026-10-15 alarm: generated_at was still the October 6 commit."""
    path = _write(tmp_path / "real_estate.json", _snap())
    now = datetime(2026, 10, 15, 14, 12, tzinfo=timezone.utc).timestamp()
    probe = bhs.resolve_age(path, now, "data/real_estate.json")
    assert probe.key == "newest housing month 2026-08"
    # Month-end, not the 1st: Aug 31 → Oct 15 is 45 days.
    assert probe.age_h == 45 * 24
    limit = bhs.threshold_for("data/real_estate.json")
    assert limit.fresh_h == 55 * 24
    assert limit.stale_h == 75 * 24
    assert probe.age_h <= limit.stale_h
    assert probe.age_h < limit.fresh_h


def test_a_missed_housing_month_still_fails(bhs, tmp_path):
    path = _write(tmp_path / "real_estate.json", _snap(
        metros=[{"history_5y_monthly": {"labels": ["2026-06"]}}]))
    now = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc).timestamp()
    probe = bhs.resolve_age(path, now, "data/real_estate.json")
    # Jun 30 → Oct 16 is 108 days, past the 75-day red line.
    assert probe.age_h == 108 * 24
    assert probe.age_h > bhs.threshold_for("real_estate.json").stale_h


def test_metros_without_a_month_are_unknown_not_fresh(bhs, tmp_path):
    path = _write(tmp_path / "real_estate.json", {
        "generated_at": "2026-10-16T11:02:08Z",
        "metros": [{"history_5y_monthly": {"labels": []}}],
    })
    probe = bhs.resolve_age(path, datetime(2026, 10, 16, tzinfo=timezone.utc).timestamp())
    assert probe.age_h is None
    assert "no YYYY-MM" in probe.note


def test_a_fixture_without_metros_still_reads_its_own_stamp(bhs, tmp_path):
    """Health-monitor tests write a tiny real_estate.json with as_of only."""
    path = _write(tmp_path / "real_estate.json", {
        "as_of": "2026-10-16",
        "generated_at": "2026-10-01T00:00:00Z",
    })
    now = datetime(2026, 10, 16, 12, tzinfo=timezone.utc).timestamp()
    probe = bhs.resolve_age(path, now)
    assert probe.key == "as_of"
    assert probe.age_h == 0


def test_check_record_is_dated_from_the_check_not_the_housing_month(bhs, tmp_path):
    path = _write(tmp_path / "real_estate_check.json", {
        "generated_at": "2026-10-16T11:02:08Z",
        "outcome": "unchanged",
        "housing_month": "2026-08",
    })
    now = datetime(2026, 10, 16, 14, 2, 8, tzinfo=timezone.utc).timestamp()
    probe = bhs.resolve_age(path, now, "data/real_estate_check.json")
    assert probe.key == "generated_at"
    assert probe.age_h == pytest.approx(3.0, abs=0.01)
    assert bhs.threshold_for("data/real_estate_check.json").stale_h == 48


def test_newest_housing_month_ignores_padding_and_takes_the_latest(bhs):
    doc = {"metros": [
        {"history_5y_monthly": {"labels": ["", "2024-01", "2026-08"]}},
        {"history_5y_monthly": {"labels": ["2026-09", "not-a-month"]}},
        {"name": "no history"},
    ]}
    assert bhs.newest_housing_month(doc) == "2026-09"
    assert bhs.housing_month_end_iso("2026-09") == "2026-09-30"
    assert bhs.housing_month_end_iso("2024-02") == "2024-02-29"
    assert bhs.housing_month_end_iso("2025-12") == "2025-12-31"


def test_empty_or_collapsed_payload_is_refused(fre):
    good = _snap()
    assert fre.payload_problems(good, None) == []

    empty = _snap(metros=[])
    assert any("no metros" in p for p in fre.payload_problems(empty, good))

    no_month = _snap(metros=[{"history_5y_monthly": {"labels": []}}])
    assert any("no YYYY-MM" in p for p in fre.payload_problems(no_month, good))

    zero = _snap()
    zero["sources"]["redfin"]["rows"] = 0
    problems = fre.payload_problems(zero, good)
    assert any("redfin contributed 0 rows" in p for p in problems)
    assert any("redfin rows collapsed from 878 to 0" in p for p in problems)

    halved = _snap()
    halved["sources"]["zillow"]["rows"] = 400
    assert any("zillow rows fell from 894 to 400" in p
               for p in fre.payload_problems(halved, good))

    # Exactly half remains: not "more than half disappeared".
    half = _snap()
    half["sources"]["zillow"]["rows"] = 447
    assert not any("fell from" in p for p in fre.payload_problems(half, good))


def test_housing_month_regression_is_refused(fre):
    previous = _snap()
    nxt = _snap(metros=[{"history_5y_monthly": {"labels": ["2026-07"]}}])
    assert any("regressed from 2026-08 to 2026-07" in p
               for p in fre.payload_problems(nxt, previous))


def test_clocks_alone_are_not_a_substance_change(fre, tmp_path):
    previous = _snap()
    nxt = _snap(generated_at="2026-10-16T11:02:08Z")
    nxt["sources"]["zillow"]["fetched_at"] = "2026-10-16T11:02:09Z"
    assert fre.substance_changed(previous, nxt) is False
    nxt["metros"][0]["history_5y_monthly"]["labels"].append("2026-09")
    assert fre.substance_changed(previous, nxt) is True


def test_unchanged_run_records_a_check_and_keeps_the_old_snapshot_stamp(fre, tmp_path, monkeypatch):
    monkeypatch.setattr(fre, "CHECK_PATH", tmp_path / "real_estate_check.json")
    payload = _snap(generated_at="2026-10-16T11:02:08Z")
    payload["sources"]["zillow"]["fetched_at"] = "2026-10-16T11:02:08Z"
    rec = fre.write_check_record(
        payload, payload_changed=False, checked_at="2026-10-16T11:02:08Z",
        snapshot_generated_at="2026-10-06T13:03:57Z")
    assert rec["outcome"] == "unchanged"
    assert rec["housing_month"] == "2026-08"
    assert rec["snapshot_generated_at"] == "2026-10-06T13:03:57Z"
    assert rec["generated_at"] == "2026-10-16T11:02:08Z"
    on_disk = json.loads((tmp_path / "real_estate_check.json").read_text())
    assert on_disk == rec

    now = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
    assert fre.check_record_problems(rec, payload, now=now) == []
    stale = dict(rec, generated_at="2026-10-06T13:03:57Z")
    assert fre.check_record_problems(stale, payload, now=now)


def test_check_record_must_name_the_snapshot_month(fre):
    payload = _snap()
    rec = {
        "generated_at": "2026-10-16T11:02:08Z",
        "outcome": "updated",
        "housing_month": "2026-01",
    }
    now = datetime(2026, 10, 16, 11, 30, tzinfo=timezone.utc)
    problems = fre.check_record_problems(rec, payload, now=now)
    assert any("housing_month" in p for p in problems)


def test_committed_check_file_matches_the_snapshot(bhs, fre):
    """The check record describes the snapshot already in git, not a new fetch.

    real-estate-daily commits a check even when the vendors did not publish.
    That record keeps ``snapshot_generated_at`` on the October 16 fetch
    (14a7513) and sets outcome to ``unchanged``, with ``generated_at`` as
    the later check time. An ``updated`` record is the fetch itself.
    """
    payload = json.loads((REPO_ROOT / "data" / "real_estate.json").read_text())
    check = json.loads((REPO_ROOT / "data" / "real_estate_check.json").read_text())
    assert check["housing_month"] == bhs.newest_housing_month(payload)
    assert check["snapshot_generated_at"] == payload["generated_at"]
    # The snapshot stamp is the October 16 fetch, not "now".
    assert payload["generated_at"] == "2026-10-16T11:02:08Z"
    assert check["outcome"] in ("updated", "unchanged")
    if check["outcome"] == "updated":
        assert check["generated_at"] == payload["generated_at"]
    else:
        assert check["generated_at"] > payload["generated_at"]


def test_check_file_is_not_gitignored():
    ignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "!data/real_estate_check.json" in ignore
    proc = subprocess.run(
        ["git", "check-ignore", "-q", "data/real_estate_check.json"],
        cwd=REPO_ROOT)
    assert proc.returncode != 0, "the check record is ignored; the daily job cannot commit it"


def test_manifest_watches_the_check_and_not_daily_snapshot_commits(fre):
    dh = _load("data_health")
    snap = dh.MANIFEST["data/real_estate.json"]
    assert snap.cadence == "monthly"
    assert [h.source for h in snap.history] == ["json"]
    assert all(h.cadence != "daily" for h in snap.history)
    check = dh.MANIFEST["data/real_estate_check.json"]
    assert [(h.cadence, h.source) for h in check.history] == [("daily", "git")]
    assert "fetch_real_estate.py" in (check.refresher or "")
