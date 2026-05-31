"""Tests for the FBI CDE adapter (``city/fbi.py``) — Miami Public Safety pillar.

All HTTP is mocked via a tiny fake session that records each ``.get(...)`` and
replays a canned JSON payload — NO live network in the bulk of the suite. The
single exception is one opt-out-able LIVE smoke test
(``test_resolve_ori_live_smoke_miami_dade``) that confirms the KEYLESS ORI
lookup still returns ``FL0130000`` from the real CDE agencies endpoint; it is
skipped automatically if the network is unavailable.

Canned payloads mirror the real CDE shapes captured 2026-05-31:
  * agencies = a dict keyed by COUNTY name, each value a list of agency objects.
  * summarized-agency = ``offenses.actuals."{Agency} Offenses"`` keyed by MM-YYYY
    (plus a ``Clearances`` sibling and ``offenses.rates`` comparison series that
    must be ignored), with months deliberately out of order.
"""
from __future__ import annotations

import socket

import pytest

from city import fbi
from city.fbi import FBIError, monthly_offenses, resolve_ori


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #
class FakeResponse:
    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    """Records GET calls and replays a queue of canned responses."""

    def __init__(self, responses):
        # ``responses`` may be a list (consumed in order) or a single payload
        # reused for every call.
        self._responses = responses
        self.calls = []  # list of (url, params) tuples

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        resp = (
            self._responses.pop(0)
            if isinstance(self._responses, list)
            else self._responses
        )
        if isinstance(resp, FakeResponse):
            return resp
        return FakeResponse(resp)


@pytest.fixture(autouse=True)
def _clear_ori_cache():
    """Isolate the in-process ORI cache between tests."""
    fbi._ORI_CACHE.clear()
    yield
    fbi._ORI_CACHE.clear()


@pytest.fixture(autouse=True)
def _no_ambient_key(monkeypatch):
    """Ensure no real FBI_CDE_API_KEY leaks in from the environment.

    Individual tests set it explicitly when they want the keyed path.
    """
    monkeypatch.delenv("FBI_CDE_API_KEY", raising=False)


# --------------------------------------------------------------------------- #
# canned payloads
# --------------------------------------------------------------------------- #
def _fl_agencies_payload():
    """Trimmed FL agencies payload: dict keyed by county, each a list of agencies.

    Mirrors the real shape, including the Miami-Dade County PD entry and several
    decoy 'Miami …' city agencies plus an unrelated county bucket.
    """
    return {
        "BROWARD": [
            {"ori": "FL0060100", "agency_name": "Fort Lauderdale Police Department",
             "agency_type_name": "City", "state_abbr": "FL"},
        ],
        "MIAMI-DADE": [
            # Decoys first to prove the substring match (not bucket order) selects
            # the right agency.
            {"ori": "FL0130600", "agency_name": "Miami Police Department",
             "agency_type_name": "City", "state_abbr": "FL"},
            {"ori": "FL0130700", "agency_name": "Miami Beach Police Department",
             "agency_type_name": "City", "state_abbr": "FL"},
            {"ori": "FL0130000", "agency_name": "Miami-Dade County Police Department",
             "agency_type_name": "County", "state_abbr": "FL",
             "nibrs_start_date": "2022-01-01", "is_nibrs": True},
            {"ori": "FL0133700", "agency_name": "Miami-Dade County Public Schools",
             "agency_type_name": "Other", "state_abbr": "FL"},
        ],
    }


def _summarized_payload(agency="Miami-Dade County Police Department",
                        offenses=None, clearances=None):
    """A CDE summarized-agency payload with the real nested shape.

    ``offenses`` / ``clearances`` are MM-YYYY -> count dicts. The agency's own
    counts live under ``offenses.actuals."{agency} Offenses"``; a sibling
    ``Clearances`` series and ``offenses.rates`` comparison series are included
    to prove they're ignored.
    """
    if offenses is None:
        # Deliberately OUT OF ORDER and spanning a year boundary.
        offenses = {"01-2025": 340, "02-2025": 297, "11-2024": 361, "12-2024": 352}
    if clearances is None:
        clearances = {"11-2024": 150, "12-2024": 160, "01-2025": 139, "02-2025": 154}
    return {
        "offenses": {
            "rates": {
                "Florida Offenses": {"11-2024": 15.71, "12-2024": 72.64},
                "United States Offenses": {"11-2024": 27.53, "12-2024": 30.24},
                f"{agency} Offenses": {"11-2024": 30.6, "12-2024": 29.1},
                f"{agency} Clearances": {"11-2024": 12.4, "12-2024": 13.1},
            },
            "actuals": {
                f"{agency} Offenses": dict(offenses),
                f"{agency} Clearances": dict(clearances),
            },
        },
        "tooltips": {"leftYAxisHeaders": {"yAxisHeaderActual": "Offenses"}},
        "populations": {"population": {agency: {"11-2024": 1212003}}},
        "cde_properties": {"max_data_date": {"UCR": "05/2026"}},
    }


CDE_BASE = "https://cde.ucr.cjis.gov/LATEST"
SUMMARIZED_URL = f"{CDE_BASE}/summarized/agency/FL0130000/violent-crime"


# --------------------------------------------------------------------------- #
# resolve_ori — canned (mock) parsing
# --------------------------------------------------------------------------- #
def test_resolve_ori_finds_miami_dade_from_canned_agencies():
    session = FakeSession([_fl_agencies_payload()])
    ori = resolve_ori("FL", "Miami-Dade", session=session)
    assert ori == "FL0130000"
    # Hit the keyless byStateAbbr endpoint with the uppercased state.
    assert session.calls[0][0] == f"{CDE_BASE}/agency/byStateAbbr/FL"


def test_resolve_ori_is_case_insensitive():
    session = FakeSession([_fl_agencies_payload()])
    # Lowercase substring + lowercase state still resolves.
    assert resolve_ori("fl", "miami-dade", session=session) == "FL0130000"


def test_resolve_ori_first_match_wins_over_decoys():
    """'Miami' (broad) matches the first 'Miami …' agency in bucket order."""
    session = FakeSession([_fl_agencies_payload()])
    # 'Miami Police Department' is listed before 'Miami-Dade …' in the bucket.
    assert resolve_ori("FL", "Miami", session=session) == "FL0130600"


def test_resolve_ori_returns_none_when_no_match():
    session = FakeSession([_fl_agencies_payload()])
    assert resolve_ori("FL", "Nonexistent Agency", session=session) is None


def test_resolve_ori_returns_none_on_unexpected_envelope():
    """A bogus state returns a different (non-county-dict) envelope -> None."""
    bogus = {
        "cde_agencies_query": {
            "counties": None,
            "parameters": {"state_abbr": "ZZ", "county_name": ""},
        }
    }
    session = FakeSession([bogus])
    assert resolve_ori("ZZ", "Miami-Dade", session=session) is None


def test_resolve_ori_caches_in_process():
    """Second identical lookup must NOT hit the network again."""
    session = FakeSession([_fl_agencies_payload()])  # only ONE response queued
    first = resolve_ori("FL", "Miami-Dade", session=session)
    second = resolve_ori("FL", "Miami-Dade", session=session)
    assert first == second == "FL0130000"
    assert len(session.calls) == 1  # cache served the second call


def test_resolve_ori_http_error_raises():
    session = FakeSession([FakeResponse({}, status_code=503, text="upstream down")])
    with pytest.raises(FBIError):
        resolve_ori("FL", "Miami-Dade", session=session)


def test_resolve_ori_non_json_raises():
    session = FakeSession([FakeResponse(ValueError("no json"))])
    with pytest.raises(FBIError):
        resolve_ori("FL", "Miami-Dade", session=session)


# --------------------------------------------------------------------------- #
# resolve_ori — LIVE smoke (keyless). Skips cleanly if offline.
# --------------------------------------------------------------------------- #
def _online() -> bool:
    try:
        socket.create_connection(("cde.ucr.cjis.gov", 443), timeout=5).close()
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _online(), reason="CDE host unreachable; skipping live smoke")
def test_resolve_ori_live_smoke_miami_dade():
    """LIVE keyless check: the real CDE agencies endpoint -> FL0130000."""
    ori = resolve_ori("FL", "Miami-Dade")  # real module session, no key needed
    assert ori == "FL0130000"


# --------------------------------------------------------------------------- #
# monthly_offenses — no key short-circuit (NO network)
# --------------------------------------------------------------------------- #
def test_monthly_offenses_no_key_returns_empty_without_network():
    session = FakeSession([])  # any .get would IndexError
    out = monthly_offenses("FL0130000", api_key=None, session=session)
    assert out == []
    assert session.calls == []  # never touched the network


def test_monthly_offenses_no_key_via_env_empty(monkeypatch):
    monkeypatch.delenv("FBI_CDE_API_KEY", raising=False)
    session = FakeSession([])
    assert monthly_offenses("FL0130000", session=session) == []
    assert session.calls == []


def test_monthly_offenses_empty_string_key_treated_as_no_key():
    session = FakeSession([])
    assert monthly_offenses("FL0130000", api_key="", session=session) == []
    assert session.calls == []


# --------------------------------------------------------------------------- #
# monthly_offenses — keyed parsing
# --------------------------------------------------------------------------- #
def test_monthly_offenses_parses_actuals_offenses_ascending():
    session = FakeSession([_summarized_payload()])
    out = monthly_offenses(
        "FL0130000", api_key="KEY", since="2024-11", until="2025-02", session=session
    )
    # Sorted ascending; Clearances + rates ignored; counts are the Offenses actuals.
    assert out == [
        {"month": "2024-11", "n": 361},
        {"month": "2024-12", "n": 352},
        {"month": "2025-01", "n": 340},
        {"month": "2025-02", "n": 297},
    ]


def test_monthly_offenses_query_shape_mm_yyyy_and_key():
    session = FakeSession([_summarized_payload()])
    monthly_offenses(
        "FL0130000", api_key="SECRET", since="2024-11", until="2025-02", session=session
    )
    url, params = session.calls[0]
    assert url == SUMMARIZED_URL
    # Date bounds use CDE's MM-YYYY format (NOT YYYY-MM, NOT bare year).
    assert params["from"] == "11-2024"
    assert params["to"] == "02-2025"
    # Key passed as the API_KEY query param.
    assert params["API_KEY"] == "SECRET"


def test_monthly_offenses_offense_slug_in_path():
    session = FakeSession([_summarized_payload()])
    monthly_offenses(
        "FL0130000", api_key="KEY", since="2024-01", until="2024-02",
        offense="property-crime", session=session,
    )
    assert session.calls[0][0] == f"{CDE_BASE}/summarized/agency/FL0130000/property-crime"


def test_monthly_offenses_until_defaults_to_since():
    session = FakeSession([_summarized_payload(offenses={"03-2024": 397})])
    monthly_offenses("FL0130000", api_key="KEY", since="2024-03", session=session)
    params = session.calls[0][1]
    assert params["from"] == "03-2024"
    assert params["to"] == "03-2024"


def test_monthly_offenses_bare_year_until_widens_to_december():
    session = FakeSession([_summarized_payload()])
    monthly_offenses("FL0130000", api_key="KEY", since="2024", until="2024", session=session)
    params = session.calls[0][1]
    assert params["from"] == "01-2024"  # bare-year since -> January
    assert params["to"] == "12-2024"    # bare-year until -> December (inclusive)


def test_monthly_offenses_no_since_omits_date_params():
    session = FakeSession([_summarized_payload()])
    monthly_offenses("FL0130000", api_key="KEY", session=session)
    params = session.calls[0][1]
    assert "from" not in params
    assert "to" not in params
    assert params["API_KEY"] == "KEY"


def test_monthly_offenses_null_actuals_returns_empty():
    """A bogus/unparticipating ORI -> HTTP 200 with actuals=null -> []."""
    payload = {
        "offenses": {
            "rates": {"United States Offenses": {"01-2024": 27.79}},
            "actuals": None,
        },
        "tooltips": {},
    }
    session = FakeSession([payload])
    out = monthly_offenses("ZZ9999999", api_key="KEY", since="2024-01", session=session)
    assert out == []


def test_monthly_offenses_ignores_clearances_series():
    """Only the '* Offenses' actuals series is summed; '* Clearances' is dropped."""
    payload = _summarized_payload(
        offenses={"01-2024": 100},
        clearances={"01-2024": 999999},  # would dominate if mistakenly summed
    )
    session = FakeSession([payload])
    out = monthly_offenses("FL0130000", api_key="KEY", since="2024-01", session=session)
    assert out == [{"month": "2024-01", "n": 100}]


def test_monthly_offenses_http_error_raises():
    session = FakeSession([FakeResponse({}, status_code=400, text="bad date")])
    with pytest.raises(FBIError):
        monthly_offenses("FL0130000", api_key="KEY", since="2024-01", session=session)


def test_monthly_offenses_non_json_raises():
    session = FakeSession([FakeResponse(ValueError("nope"))])
    with pytest.raises(FBIError):
        monthly_offenses("FL0130000", api_key="KEY", since="2024-01", session=session)


def test_monthly_offenses_missing_offenses_object_raises():
    session = FakeSession([{"unexpected": True}])
    with pytest.raises(FBIError):
        monthly_offenses("FL0130000", api_key="KEY", since="2024-01", session=session)


def test_monthly_offenses_non_numeric_count_raises():
    payload = _summarized_payload(offenses={"01-2024": "not-a-number"})
    session = FakeSession([payload])
    with pytest.raises(FBIError):
        monthly_offenses("FL0130000", api_key="KEY", since="2024-01", session=session)


def test_monthly_offenses_float_counts_coerced_to_int():
    payload = _summarized_payload(offenses={"01-2024": 100.0, "02-2024": 200.0})
    session = FakeSession([payload])
    out = monthly_offenses(
        "FL0130000", api_key="KEY", since="2024-01", until="2024-02", session=session
    )
    assert out == [{"month": "2024-01", "n": 100}, {"month": "2024-02", "n": 200}]
    assert all(isinstance(r["n"], int) for r in out)


def test_monthly_offenses_bad_since_raises():
    session = FakeSession([_summarized_payload()])
    with pytest.raises(FBIError):
        monthly_offenses("FL0130000", api_key="KEY", since="2024-13", session=session)


# --------------------------------------------------------------------------- #
# default session wiring
# --------------------------------------------------------------------------- #
def test_default_session_is_a_requests_session():
    import requests

    assert isinstance(fbi._SESSION, requests.Session)


def test_resolve_ori_uses_module_session_when_none(monkeypatch):
    """When session= is omitted, the lookup goes through the module session."""
    recorded = {}

    def fake_get(url, params=None, timeout=None):
        recorded["url"] = url
        return FakeResponse(_fl_agencies_payload())

    monkeypatch.setattr(fbi._SESSION, "get", fake_get)
    assert resolve_ori("FL", "Miami-Dade") == "FL0130000"
    assert recorded["url"] == f"{CDE_BASE}/agency/byStateAbbr/FL"
