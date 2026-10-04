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


class _CDESession:
    """Fake transport for the FBI CDE summarized endpoint (no network)."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        payload = self.payload

        class _R:
            status_code = 200
            text = ""

            def json(self):
                return payload
        return _R()


def _cde_payload(months):
    return {"offenses": {"actuals": {
        "Miami-Dade County Police Department Offenses": dict(months),
        "Miami-Dade County Police Department Clearances": {}}},
        "cde_properties": {"last_refresh_date": {"UCR": "09/15/2026"}}}


def test_fbi_feed_is_fetched_without_a_key(monkeypatch):
    """Miami Public Safety used to be refused without FBI_CDE_API_KEY, before
    any request was sent. The CDE host serves the series keyless (verified
    live 2026-10-04), so the build must ask, and score what comes back."""
    monkeypatch.delenv("FBI_CDE_API_KEY", raising=False)
    months = {}
    for i, ym in enumerate(r["month"] for r in dense("2023-06", "2026-09")):
        months["{}-{}".format(ym[5:7], ym[:4])] = 300 + (i % 5) * 7
    sess = _CDESession(_cde_payload(months))
    cfg = {"pillar": "public_safety", "label": "FBI CDE (fallback)",
           "adapter": "fbi", "ori": "FL0130000", "polarity": -1}

    series, hint, reason = fetch_city._fetch_feed_series(
        cfg, {"id": "miami"}, as_of="2026-09", since_date="2023-06-01",
        session=sess)

    assert hint == "ok" and reason is None
    assert len(sess.calls) == 1 and "API_KEY" not in sess.calls[0][1]
    # September was still in progress at the 09/15 refresh: not handed over.
    assert series[-1]["month"] == "2026-08"


def test_fbi_empty_response_is_fetch_error_not_not_published(monkeypatch):
    monkeypatch.delenv("FBI_CDE_API_KEY", raising=False)
    sess = _CDESession({"offenses": {"actuals": None}})
    cfg = {"pillar": "public_safety", "label": "FBI CDE (fallback)",
           "adapter": "fbi", "ori": "FL0130000", "polarity": -1}
    series, hint, reason = fetch_city._fetch_feed_series(
        cfg, {"id": "miami"}, as_of="2026-09", since_date="2023-06-01",
        session=sess)
    assert (series, hint) == ([], "fetch_error")
    assert "FL0130000" in reason


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


# --------------------------------------------------------------------------- #
# 5. GitHub Actions annotations: loud, grouped, and never echoing raw errors
# --------------------------------------------------------------------------- #
DIAGS = [
    {"city": "chicago", "source": "census", "kind": "no key",
     "detail": "CENSUS_API_KEY unset; see https://api.census.gov/data/key_signup.html",
     "lost": "median_income"},
    {"city": "nyc", "source": "census", "kind": "no key",
     "detail": "CENSUS_API_KEY unset", "lost": "median_income"},
    {"city": "sf", "source": "socrata", "kind": "failed",
     "detail": "Socrata request to data.sf.gov failed: HTTPSConnectionPool(host="
               "'data.sf.gov', port=443): Read timed out. (read timeout=120)",
     "lost": "feed '311 Cases'"},
    {"city": "miami", "source": "arcgis", "kind": "failed",
     "detail": "ArcGIS error from https://x.invalid/q?token=abc123: code=499 Token Required",
     "lost": "feed 'MDC Building Permit'"},
]


def _annotations(out):
    return [ln for ln in out.splitlines() if ln.startswith("::warning ")]


def test_annotations_emitted_only_under_github_actions(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    fetch_city._annotate_github(DIAGS)
    assert _annotations(capsys.readouterr().out) == []

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    fetch_city._annotate_github(DIAGS)
    lines = _annotations(capsys.readouterr().out)
    assert lines, "a degraded build must annotate the run"
    assert all(ln.startswith("::warning title=City data not refreshed::") for ln in lines)


def test_annotations_group_by_source_and_cause(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    fetch_city._annotate_github(DIAGS)
    lines = _annotations(capsys.readouterr().out)
    assert len(lines) == 3   # census/no key (x2 cities), socrata/timeout, arcgis/499
    census = [ln for ln in lines if "::census: " in ln][0]
    assert "missing secret CENSUS_API_KEY" in census
    # Same loss across cities is listed once, with the cities that lost it.
    assert "median_income (chicago, nyc)" in census
    assert any("socrata: timeout" in ln and "311 Cases" in ln for ln in lines)
    assert any("arcgis: error code 499" in ln for ln in lines)


def test_annotations_never_echo_the_raw_error_text(monkeypatch, capsys):
    """Only fixed cause labels go out — no URL, no query string, no message."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    fetch_city._annotate_github(DIAGS)
    out = capsys.readouterr().out
    for leaked in ("abc123", "token=", "https://", "HTTPSConnectionPool", "x.invalid"):
        assert leaked not in out


def test_annotation_body_is_workflow_escaped():
    assert fetch_city._workflow_escape("50% done\r\nnext") == "50%25 done%0D%0Anext"


def test_annotation_escaping_applies_to_emitted_lines(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    fetch_city._annotate_github([{"city": "la", "source": "socrata", "kind": "failed",
                                  "detail": "HTTP 503", "lost": "feed '100%\nreal'"}])
    line = _annotations(capsys.readouterr().out)[0]
    assert "100%25%0Areal" in line
    assert "\n" not in line.split("::", 2)[2]


def test_annotations_are_capped_at_ten(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    many = [{"city": "c%d" % i, "source": "src%d" % i, "kind": "failed",
             "detail": "HTTP 500", "lost": "x"} for i in range(14)]
    fetch_city._annotate_github(many)
    lines = _annotations(capsys.readouterr().out)
    assert len(lines) == 10
    assert "5 more degraded" in lines[-1]


def test_no_annotation_when_nothing_degraded(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    fetch_city._annotate_github([])
    assert _annotations(capsys.readouterr().out) == []


def test_main_annotates_a_degraded_build(monkeypatch, capsys, tmp_path):
    """End to end: a dead feed in a real main() run becomes an annotation and
    the run still exits 0 (one broken source must not block the rest)."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(socrata, "feed_series", lambda *a, **k: (_ for _ in ()).throw(
        socrata.SocrataError("Socrata returned HTTP 403 at https://h/resource/x.json")))
    monkeypatch.setattr(arcgis, "feed_series",
                        lambda *a, **k: (dense("2024-01", "2026-08"), "ok"))
    monkeypatch.setattr(city_context, "fbi_crime_series",
                        lambda *a, **k: dense("2024-01", "2026-08"))
    monkeypatch.setattr(city_context, "build_context", lambda *a, **k: None)
    monkeypatch.setattr(fetch_city, "_load_extended_feeds", lambda: {})

    rc = fetch_city.main(["--force", "--out", str(tmp_path / "city.json"),
                          "--as-of", "2026-08"])
    assert rc == 0
    lines = _annotations(capsys.readouterr().out)
    assert any("socrata: HTTP 403" in ln for ln in lines)
    assert not any("https://h/" in ln for ln in lines)


# --------------------------------------------------------------------------- #
# 6. registry: moved / retired upstream datasets stay fixed
# --------------------------------------------------------------------------- #
def _registry_city(cid):
    reg = json.loads(fetch_city.REGISTRY.read_text())
    return next(c for c in reg["cities"] if c["id"] == cid)


def test_la_crime_no_longer_unions_the_retired_baseline():
    """y8y3-fqfu answers 403 'You must be logged in' since LA consolidated NIBRS
    into k7nn-b2ep (2026-08-18); unioning it failed LA Public Safety nightly."""
    feed = next(f for f in _registry_city("la")["feeds"] if f["pillar"] == "public_safety")
    assert feed["dataset"] == "k7nn-b2ep"
    assert "baseline_dataset" not in feed


def test_sf_uses_the_canonical_portal_host():
    """data.sfgov.org now 301s to data.sf.gov (and served an nginx 403 on 2026-09-30)."""
    assert _registry_city("sf")["host"] == "data.sf.gov"


@pytest.mark.parametrize("entry, label", [
    ({"kind": "no key", "source": "census"}, "missing secret CENSUS_API_KEY"),
    ({"kind": "no key", "source": "airnow"}, "missing secret AIRNOW_API_KEY"),
    ({"kind": "failed", "detail": "Socrata throttled (HTTP 429) at https://h/x"}, "HTTP 429"),
    ({"kind": "failed", "detail": "ArcGIS error ...: code=499 Token Required"}, "error code 499"),
    ({"kind": "failed", "detail": "ACS request rejected by Census: invalid key"},
     "key rejected by upstream"),
    ({"kind": "failed", "detail": "Read timed out. (read timeout=120)"}, "timeout"),
    ({"kind": "failed", "detail": "BLS request not processed for LAUCT1714: "
      "REQUEST_NOT_PROCESSED ... the daily threshold for total number of requests"},
     "daily request quota exhausted"),
    ({"kind": "failed", "detail": "CDE response from x was not JSON"}, "non-JSON response"),
    ({"kind": "failed", "detail": "connection reset"}, "request failed"),
    ({"kind": "empty", "detail": "FBI CDE returned no offense rows"}, "empty response"),
    ({"kind": "no geography in registry (context_layer...)"}, "no geography in registry"),
    ({"kind": "bug", "detail": "KeyError: 'x'"}, "code bug (traceback in job log)"),
])
def test_cause_labels(entry, label):
    assert fetch_city._cause_class(entry) == label
