"""Tests for city/context.py — Layer B composition, and its LOUDNESS.

Graceful degradation and silent degradation are not the same thing, and this
module used to do the second while documenting the first. ``except CensusError:
acs = {}`` printed nothing, so median_income / median_rent / median_home_value /
median_real_estate_taxes / effective_property_tax_rate were null for every city
in every nightly build, indefinitely, and the only symptom was a tidy row of
em-dashes on the page — which reads as "this city has no data", not as "we never
sent a usable request".

These tests assert the two properties that make that impossible to repeat:

  * every degradation announces itself and names the fields it just nulled, and
  * a missing key is reported as a MISSING KEY, naming the environment variable,
    distinctly from a request that was sent and failed.

Plus the non-negotiable: none of it is ever fatal, and one dead adapter cannot
take the other two down with it.

No network. Every adapter is stubbed.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from city import context as city_context  # noqa: E402
from city import census, bls, airnow  # noqa: E402

CITY = {"id": "chicago", "name": "Chicago"}
GEO = {"geo": "place", "state": "17", "place": "14000"}

ACS_OK = {
    "median_income": 74590, "median_rent": 1470, "median_home_value": 379600,
    "median_real_estate_taxes": 5300, "effective_property_tax_rate": 0.01396,
    "unemployment_rate": None, "aqi": None, "context_score": None,
}


@pytest.fixture(autouse=True)
def no_keys(monkeypatch):
    """Default to the state city-daily actually runs in: no optional keys."""
    for var in ("CENSUS_API_KEY", "AIRNOW_API_KEY", "FBI_CDE_API_KEY"):
        monkeypatch.delenv(var, raising=False)


# --------------------------------------------------------------------------- #
# missing credentials are reported AS missing credentials
# --------------------------------------------------------------------------- #
def test_missing_census_key_is_announced_and_names_every_lost_field(
        monkeypatch, capsys):
    monkeypatch.setattr(bls, "fetch_unemployment", lambda *a, **k: 5.9)
    diags: list = []
    ctx = city_context.build_context(CITY, GEO, diagnostics=diags)

    err = capsys.readouterr().err
    assert "CENSUS_API_KEY" in err
    assert "no key" in err
    for field in ("median_income", "median_rent", "median_home_value",
                  "median_real_estate_taxes", "effective_property_tax_rate"):
        assert field in err, "the log must say WHAT went missing, not just what failed"
        assert ctx[field] is None

    entry = [d for d in diags if d["source"] == "census"][0]
    assert entry["kind"] == "no key"
    assert "key_signup" in entry["detail"]   # tells the operator where to go


def test_missing_census_key_sends_no_request_at_all(monkeypatch):
    """Keyless, the ACS DATA endpoint returns an HTML 'Missing Key' page. Firing
    the request anyway turned an operator problem into a JSON parse error."""
    def must_not_be_called(*a, **k):
        raise AssertionError("fetch_acs called without CENSUS_API_KEY")
    monkeypatch.setattr(census, "fetch_acs", must_not_be_called)
    monkeypatch.setattr(bls, "fetch_unemployment", lambda *a, **k: 5.9)
    city_context.build_context(CITY, GEO)


def test_missing_airnow_key_is_announced(monkeypatch, capsys):
    """airnow.fetch_aqi short-circuits to None WITHOUT raising, so the old
    `except AirNowError` never fired and this degradation produced no output
    anywhere in the build."""
    monkeypatch.setattr(bls, "fetch_unemployment", lambda *a, **k: 5.9)
    diags: list = []
    ctx = city_context.build_context(CITY, GEO, diagnostics=diags)
    err = capsys.readouterr().err
    assert "AIRNOW_API_KEY" in err
    assert ctx["aqi"] is None
    assert any(d["source"] == "airnow" and d["kind"] == "no key" for d in diags)


def test_missing_geography_is_announced(monkeypatch, capsys):
    monkeypatch.setattr(bls, "fetch_unemployment", lambda *a, **k: 5.9)
    ctx = city_context.build_context(CITY, None)
    assert "geo_by_city" in capsys.readouterr().err
    assert ctx["median_income"] is None


# --------------------------------------------------------------------------- #
# a request that was sent and failed reports differently
# --------------------------------------------------------------------------- #
def test_census_failure_with_a_key_is_reported_as_failed_not_missing(
        monkeypatch, capsys):
    monkeypatch.setenv("CENSUS_API_KEY", "REAL")
    monkeypatch.setattr(census, "fetch_acs", lambda *a, **k: (_ for _ in ()).throw(
        census.CensusError("HTTP 503 from api.census.gov")))
    monkeypatch.setattr(bls, "fetch_unemployment", lambda *a, **k: 5.9)

    diags: list = []
    ctx = city_context.build_context(CITY, GEO, diagnostics=diags)
    entry = [d for d in diags if d["source"] == "census"][0]
    assert entry["kind"] == "failed", "an outage is not a missing secret"
    assert "503" in capsys.readouterr().err
    assert ctx["median_income"] is None


def test_unexpected_adapter_bug_does_not_cost_the_other_sources(
        monkeypatch, capsys):
    """A non-CensusError escaping census used to propagate out of build_context
    and lose the BLS and AirNow values with it."""
    monkeypatch.setenv("CENSUS_API_KEY", "REAL")
    monkeypatch.setenv("AIRNOW_API_KEY", "REAL")
    monkeypatch.setattr(census, "fetch_acs", lambda *a, **k: (_ for _ in ()).throw(
        KeyError("B19013_001E")))
    monkeypatch.setattr(bls, "fetch_unemployment", lambda *a, **k: 5.9)
    monkeypatch.setattr(airnow, "fetch_aqi", lambda *a, **k: 42)

    ctx = city_context.build_context(CITY, GEO)
    assert ctx["unemployment_rate"] == 5.9
    assert ctx["aqi"] == 42
    assert ctx["median_income"] is None
    assert "unexpected KeyError" in capsys.readouterr().err


def test_build_context_never_raises(monkeypatch):
    """A missing OPTIONAL key must never break the nightly build."""
    monkeypatch.setenv("CENSUS_API_KEY", "REAL")
    monkeypatch.setenv("AIRNOW_API_KEY", "REAL")
    for mod, fn in ((census, "fetch_acs"), (bls, "fetch_unemployment"),
                    (airnow, "fetch_aqi")):
        monkeypatch.setattr(mod, fn, lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("catastrophe")))
    assert city_context.build_context(CITY, GEO) is None  # nothing survived


# --------------------------------------------------------------------------- #
# the happy path, and the BLS control
# --------------------------------------------------------------------------- #
def test_all_sources_present(monkeypatch, capsys):
    monkeypatch.setenv("CENSUS_API_KEY", "REAL")
    monkeypatch.setenv("AIRNOW_API_KEY", "REAL")
    monkeypatch.setattr(census, "fetch_acs", lambda *a, **k: dict(ACS_OK))
    monkeypatch.setattr(bls, "fetch_unemployment", lambda *a, **k: 5.9)
    monkeypatch.setattr(airnow, "fetch_aqi", lambda *a, **k: 42)

    diags: list = []
    ctx = city_context.build_context(CITY, GEO, diagnostics=diags)
    assert ctx["median_income"] == 74590
    assert ctx["unemployment_rate"] == 5.9
    assert ctx["aqi"] == 42
    assert diags == [], "a clean run must not cry wolf"
    assert capsys.readouterr().err == ""


def test_bls_is_the_control_that_proves_the_pipeline_runs(monkeypatch):
    """BLS is keyless. unemployment_rate populated while the keyed sources are
    null is the signature of 'the job runs, the credentials are missing' — as
    opposed to 'the job is dead', which looks completely different."""
    monkeypatch.setattr(bls, "fetch_unemployment", lambda *a, **k: 5.9)
    ctx = city_context.build_context(CITY, GEO)
    assert ctx is not None
    assert ctx["unemployment_rate"] == 5.9
    assert ctx["median_income"] is None and ctx["aqi"] is None


def test_context_is_none_when_nothing_at_all_resolved(monkeypatch):
    """None -> the card shows 'Context coming' rather than a row of dashes."""
    monkeypatch.setattr(bls, "fetch_unemployment", lambda *a, **k: None)
    assert city_context.build_context(CITY, GEO) is None


def test_fbi_key_helpers(monkeypatch):
    assert city_context.fbi_key_missing() is True
    assert "api.data.gov" in city_context.fbi_key_help()
    monkeypatch.setenv("FBI_CDE_API_KEY", "REAL")
    assert city_context.fbi_key_missing() is False


def test_fbi_crime_series_without_an_ori_is_empty_and_hits_no_network():
    assert city_context.fbi_crime_series({}, since="2023-01", until="2026-07") == []
