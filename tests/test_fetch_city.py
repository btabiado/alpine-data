"""Tests for fetch_city.py — the City tab build orchestrator.

Two classes of defect are pinned here, both of the "silently produces a
plausible artifact" kind that no amount of green CI would have caught:

  1. THE FROZEN CUTOFF (test_as_of_*, test_regression_*). ``as_of`` decides how
     new a data point is allowed to be. It was read from a hand-written registry
     constant, so the build discarded every month published after recon while
     rewriting ``generated_at`` with the current clock every night. The
     regression test drives the REAL build with stubbed portals that publish
     right up to the present and asserts the payload follows them.

  2. FETCH FAILURE MISREPORTED AS A PUBLISHER GAP (test_*_fetch_error_*). Every
     adapter exception used to become ``not_published``, which the dashboard
     renders as "Not published by this city" — a false claim about a real
     government body, and one that makes a rotting feed indistinguishable from
     a feed that never existed.

No network: every adapter is stubbed. The proxy in CI 403s these upstreams
anyway, and a freshness test that depends on the internet being up is a
freshness test that gets deleted the first time it flakes.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import fetch_city  # noqa: E402
from city import arcgis, socrata  # noqa: E402
from city import context as city_context  # noqa: E402

NOW = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)
LIVE_MONTH = "2026-07"          # the last COMPLETE month relative to NOW
STALE_PIN = "2026-04"           # what the resolved registry still says


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def dense(since_ym: str, until_ym: str, base: int = 1000) -> list:
    """A contiguous monthly series with real variance, since..until inclusive."""
    y, m = int(since_ym[:4]), int(since_ym[5:7])
    uy, um = int(until_ym[:4]), int(until_ym[5:7])
    out, i = [], 0
    while (y, m) <= (uy, um):
        out.append({"month": "%04d-%02d" % (y, m), "n": base + (i % 7) * 13})
        i += 1
        m = 1 if m == 12 else m + 1
        y = y + 1 if m == 1 else y
    return out


CITY_CFG = {
    "id": "chicago", "name": "Chicago", "scope": "city",
    "host": "data.cityofchicago.org", "adapter": "socrata",
    "feeds": [
        {"pillar": "city_services", "label": "311", "dataset": "v6vf-nfxy",
         "date_col": "created_date", "polarity": -1},
        {"pillar": "development_economy", "label": "Permits", "dataset": "ydr8-5enu",
         "date_col": "issue_date", "polarity": 1},
    ],
}


@pytest.fixture
def portals_publishing_through_now(monkeypatch):
    """Every Socrata/ArcGIS portal is dense right up to LIVE_MONTH."""
    monkeypatch.setattr(
        socrata, "feed_series",
        lambda feed_cfg, host, since, session=None: dense(since[:7], LIVE_MONTH))
    monkeypatch.setattr(
        arcgis, "feed_series",
        lambda feed_cfg, since=None, until=None, timeout=120, session=None:
            (dense(since, LIVE_MONTH), "ok"))
    monkeypatch.setattr(city_context, "build_context",
                        lambda *a, **k: None)


# --------------------------------------------------------------------------- #
# 1. _resolve_as_of — the cutoff must track the calendar, not the registry
# --------------------------------------------------------------------------- #
def test_as_of_ignores_a_stale_registry_pin(capsys):
    registry = {"_meta": {"as_of_complete_month": STALE_PIN}}
    assert fetch_city._resolve_as_of(None, registry, NOW) == LIVE_MONTH
    err = capsys.readouterr().err
    assert STALE_PIN in err and LIVE_MONTH in err
    assert "IGNORING the pin" in err


def test_as_of_with_no_pin_uses_the_clock():
    assert fetch_city._resolve_as_of(None, {}, NOW) == LIVE_MONTH
    assert fetch_city._resolve_as_of(None, {"_meta": {}}, NOW) == LIVE_MONTH


def test_as_of_explicit_cli_still_wins():
    """Reproducible backfills need an absolute override; it stays absolute."""
    registry = {"_meta": {"as_of_complete_month": STALE_PIN}}
    assert fetch_city._resolve_as_of("2025-01", registry, NOW) == "2025-01"


def test_as_of_matching_pin_is_silent(capsys):
    registry = {"_meta": {"as_of_complete_month": LIVE_MONTH}}
    assert fetch_city._resolve_as_of(None, registry, NOW) == LIVE_MONTH
    assert capsys.readouterr().err == ""


def test_committed_registry_pin_is_currently_stale():
    """Documents the live state: the shipped registry pin is behind the calendar.

    This is what froze the payload. It asserts the GUARD, not the pin: whenever
    the pin drifts, _resolve_as_of must still hand back the clock-derived month.
    """
    registry = json.loads(fetch_city.REGISTRY.read_text())
    pin = registry["_meta"]["as_of_complete_month"]
    live = fetch_city._prev_complete_month(NOW)
    assert fetch_city._resolve_as_of(None, registry, NOW) == live
    if pin != live:
        assert fetch_city._month_minus(live, 0) > pin  # pin is genuinely behind


# --------------------------------------------------------------------------- #
# 2. THE REGRESSION: contents must move when the portals move
# --------------------------------------------------------------------------- #
def test_regression_payload_follows_the_portals_not_the_registry_pin(
        portals_publishing_through_now):
    """The container-vs-contents freeze, pinned.

    Portals publish through 2026-07. Before the fix the build clamped every
    feed to the registry's 2026-04 and threw three months of already-downloaded
    data away — while generated_at was stamped with the current clock. If this
    test ever fails with last_updated == 2026-04-01, the pin is load-bearing
    again.
    """
    as_of = fetch_city._resolve_as_of(None, {"_meta": {
        "as_of_complete_month": STALE_PIN}}, NOW)
    since = fetch_city._month_minus(as_of, 37) + "-01"
    city = fetch_city.build_city(CITY_CFG, as_of=as_of, since_date=since)

    assert city["data_health"]["last_updated"].startswith(LIVE_MONTH)
    assert city["data_health"]["last_updated"] != "2026-04-01T00:00:00+00:00"
    for pillar in city["pulse"]["pillars"]:
        for feed in pillar["feeds"]:
            assert feed["recent_period"] == LIVE_MONTH
            assert feed["status"] == "ok"


def test_regression_lagging_feed_sets_the_city_age(portals_publishing_through_now):
    """A feed that lags a month makes the whole card a month older, and says so.

    Chicago crime excludes the last ~7 days, so its complete month is one behind
    the others. Rule 2: the composite is only as fresh as its oldest input.
    """
    cfg = json.loads(json.dumps(CITY_CFG))
    cfg["feeds"][0]["note"] = "excludes last ~7 days; latest complete month lags"
    as_of = LIVE_MONTH
    since = fetch_city._month_minus(as_of, 37) + "-01"
    city = fetch_city.build_city(cfg, as_of=as_of, since_date=since)

    lagging = fetch_city._month_minus(LIVE_MONTH, 1)
    assert city["data_health"]["last_updated"].startswith(lagging)


# --------------------------------------------------------------------------- #
# 3. fetch failure != "not published by this city"
# --------------------------------------------------------------------------- #
def test_socrata_failure_is_fetch_error_not_not_published(monkeypatch, capsys):
    def boom(*a, **k):
        raise socrata.SocrataError("HTTP 429 rate limited")
    monkeypatch.setattr(socrata, "feed_series", boom)

    diags: list = []
    series, hint, reason = fetch_city._fetch_feed_series(
        CITY_CFG["feeds"][0], CITY_CFG, as_of=LIVE_MONTH,
        since_date="2023-06-01", diagnostics=diags)

    assert series == []
    assert hint == "fetch_error", "a rate limit is OUR problem, not the city's"
    assert "429" in reason
    assert diags and diags[0]["kind"] == "failed"
    assert "429" in capsys.readouterr().err


def test_arcgis_failure_is_fetch_error_not_not_published(monkeypatch):
    def boom(*a, **k):
        raise arcgis.ArcGISError("code=500 layer unavailable")
    monkeypatch.setattr(arcgis, "feed_series", boom)
    cfg = {"pillar": "development_economy", "label": "MDC Building Permit",
           "endpoint": "https://example.invalid/FeatureServer/0",
           "date_col": "ISSUDATE", "date_col_status": "confirmed", "polarity": 1}

    series, hint, reason = fetch_city._fetch_feed_series(
        cfg, {"id": "miami", "adapter": "arcgis"}, as_of=LIVE_MONTH,
        since_date="2023-06-01")

    assert (series, hint) == ([], "fetch_error")
    assert "500" in reason


def test_missing_fbi_key_is_fetch_error_and_names_the_variable(monkeypatch):
    """The FBI publishes this series. We just cannot ask for it without a key.

    Labelling that 'not_published' blames the FBI for our missing credential and
    hides the one thing an operator could actually fix.
    """
    monkeypatch.delenv("FBI_CDE_API_KEY", raising=False)
    cfg = {"pillar": "public_safety", "label": "FBI CDE (fallback)",
           "adapter": "fbi", "ori": "FL0130000", "polarity": -1}

    series, hint, reason = fetch_city._fetch_feed_series(
        cfg, {"id": "miami"}, as_of=LIVE_MONTH, since_date="2023-06-01")

    assert (series, hint) == ([], "fetch_error")
    assert "FBI_CDE_API_KEY" in reason
    assert "api.data.gov" in reason      # tells the operator where to go


def test_stale_snapshot_still_reports_stale_not_fetch_error(monkeypatch):
    """A frozen-but-real source is 'stale'. That distinction survives the change."""
    monkeypatch.setattr(
        arcgis, "feed_series",
        lambda *a, **k: (dense("2023-01", "2023-12"), "stale"))
    cfg = {"pillar": "city_services", "label": "Miami-Dade 311",
           "endpoint": "https://example.invalid/FeatureServer/0",
           "date_col_status": "stale_source", "polarity": -1}
    _, hint, reason = fetch_city._fetch_feed_series(
        cfg, {"id": "miami", "adapter": "arcgis"}, as_of=LIVE_MONTH,
        since_date="2023-06-01")
    assert hint == "stale"
    assert reason is None


def test_failure_reason_reaches_the_payload_note(monkeypatch):
    """The reason must survive into the artifact.

    The dashboard falls back to `note` for any status it has no copy for, so a
    reason that lives only in stderr is a reason no reader will ever see.
    """
    monkeypatch.setattr(socrata, "feed_series", lambda *a, **k: (_ for _ in ()).throw(
        socrata.SocrataError("connection reset")))
    since = fetch_city._month_minus(LIVE_MONTH, 37) + "-01"
    city = fetch_city.build_city(CITY_CFG, as_of=LIVE_MONTH, since_date=since)

    feeds = [f for p in city["pulse"]["pillars"] for f in p["feeds"]]
    assert feeds and all(f["status"] == "fetch_error" for f in feeds)
    assert all("connection reset" in (f["note"] or "") for f in feeds)
    # Nothing scored, and the age is null rather than a clock read.
    assert city["pulse"]["score"] is None
    assert city["data_health"]["last_updated"] is None


def test_registry_note_is_preserved_alongside_the_failure_reason(monkeypatch):
    monkeypatch.setattr(socrata, "feed_series", lambda *a, **k: (_ for _ in ()).throw(
        socrata.SocrataError("boom")))
    cfg = json.loads(json.dumps(CITY_CFG))
    cfg["feeds"] = [dict(cfg["feeds"][0], note="2018 portal migration breakpoint")]
    since = fetch_city._month_minus(LIVE_MONTH, 37) + "-01"
    city = fetch_city.build_city(cfg, as_of=LIVE_MONTH, since_date=since)
    note = city["pulse"]["pillars"][0]["feeds"][0]["note"]
    assert "portal migration" in note and "boom" in note


# --------------------------------------------------------------------------- #
# 4. one broken feed degrades one feed
# --------------------------------------------------------------------------- #
def test_one_dead_feed_does_not_take_the_other_pillar_down(monkeypatch):
    calls = {"n": 0}

    def flaky(feed_cfg, host, since, session=None):
        calls["n"] += 1
        if feed_cfg.get("label") == "311":
            raise socrata.SocrataError("timeout")
        return dense(since[:7], LIVE_MONTH)

    monkeypatch.setattr(socrata, "feed_series", flaky)
    monkeypatch.setattr(city_context, "build_context", lambda *a, **k: None)
    since = fetch_city._month_minus(LIVE_MONTH, 37) + "-01"
    city = fetch_city.build_city(CITY_CFG, as_of=LIVE_MONTH, since_date=since)

    by_label = {f["label"]: f for p in city["pulse"]["pillars"] for f in p["feeds"]}
    assert by_label["311"]["status"] == "fetch_error"
    assert by_label["Permits"]["status"] == "ok"
    assert city["pulse"]["pillars_present"] == 1
    assert city["data_health"]["feeds_ok"] == 1
    assert city["data_health"]["last_updated"].startswith(LIVE_MONTH)


def test_diagnostics_summary_separates_credentials_from_failures(capsys):
    fetch_city._report_diagnostics([
        {"city": "chicago", "source": "census", "kind": "no key",
         "detail": "CENSUS_API_KEY unset", "lost": "median_income"},
        {"city": "miami", "source": "arcgis", "kind": "failed",
         "detail": "HTTP 500", "lost": "feed 'MDC Building Permit'"},
    ])
    err = capsys.readouterr().err
    assert "MISSING CREDENTIALS (1)" in err
    assert "REQUESTS THAT FAILED (1)" in err
    assert "CENSUS_API_KEY" in err


def test_diagnostics_summary_says_so_when_everything_worked(capsys):
    fetch_city._report_diagnostics([])
    assert "no degradations" in capsys.readouterr().out
