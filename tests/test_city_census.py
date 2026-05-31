"""Tests for the Census ACS adapter (city/census.py).

All HTTP is mocked via a tiny fake session that records each ``.get(...)`` and
returns a canned response — no live network in pytest. The Census DATA API now
requires a key (keyless data returns a "Missing Key" HTML page), so the live
two-row-array shape is replayed from fixtures; only the variable *metadata*
endpoints were probed keyless out-of-band to confirm the four codes resolve.
"""
from __future__ import annotations

import pytest

from city import census
from city.census import CensusError, fetch_acs


# ---------------------------------------------------------------------------
# Fake HTTP session (records GET calls, replays a queued/canned response).
# Supports both ``.json()`` and ``.text`` so we can exercise the keyless
# "Missing Key" HTML body and non-200 paths.
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, payload=None, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        if self._payload is None:
            # No JSON payload set -> behave like a non-JSON (HTML) body.
            raise ValueError("No JSON object could be decoded")
        return self._payload


class FakeSession:
    def __init__(self, response):
        self._response = response
        self.calls = []  # list of (url, params) tuples

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        if isinstance(self._response, FakeResponse):
            return self._response
        return FakeResponse(self._response)


# Real geo blocks from city_registry.resolved.json context_layer.
PLACE_CFG_CHICAGO = {
    "geo": "place", "state": "17", "place": "14000",
    "geoid": "1714000", "census_name": "Chicago city",
}
COUNTY_CFG_MIAMI = {
    "geo": "county", "state": "12", "county": "086",
    "geoid": "12086", "census_name": "Miami-Dade County",
}

ACS_URL_2024 = "https://api.census.gov/data/2024/acs/acs5"


def _acs_payload(name, income, rent, home_value, taxes, *, geo="place"):
    """Build a realistic two-row ACS array: [[header...], [values...]].

    Values come back as JSON strings (as the live API delivers them). The geo
    columns differ between place and county responses, and we deliberately
    place them AFTER the variable columns to prove parsing is by header name,
    not fixed position.
    """
    header = ["NAME", "B19013_001E", "B25064_001E", "B25077_001E", "B25103_001E"]
    values = [name, income, rent, home_value, taxes]
    if geo == "place":
        header += ["state", "place"]
        values += ["17", "14000"]
    else:
        header += ["state", "county"]
        values += ["12", "086"]
    return [header, [str(v) if v is not None else v for v in values]]


# ---------------------------------------------------------------------------
# Query-param construction: place vs county
# ---------------------------------------------------------------------------
def test_place_query_params_correct():
    payload = _acs_payload("Chicago city, Illinois", 74590, 1404, 339000, 4500)
    session = FakeSession(payload)

    fetch_acs(PLACE_CFG_CHICAGO, api_key="TESTKEY", session=session)

    assert len(session.calls) == 1
    url, params = session.calls[0]
    assert url == ACS_URL_2024
    # get= leads with NAME then the four variable codes in registry order.
    assert params["get"] == (
        "NAME,B19013_001E,B25064_001E,B25077_001E,B25103_001E"
    )
    assert params["for"] == "place:14000"
    assert params["in"] == "state:17"
    assert params["key"] == "TESTKEY"


def test_county_query_params_correct_miami():
    """Miami is county geography (12086) -> for=county:086&in=state:12."""
    payload = _acs_payload(
        "Miami-Dade County, Florida", 68000, 1650, 410000, 4200, geo="county"
    )
    session = FakeSession(payload)

    fetch_acs(COUNTY_CFG_MIAMI, api_key="TESTKEY", session=session)

    url, params = session.calls[0]
    assert url == ACS_URL_2024
    assert params["for"] == "county:086"
    assert params["in"] == "state:12"
    assert "place" not in params["for"]


def test_vintage_is_reflected_in_url():
    payload = _acs_payload("Chicago city, Illinois", 74590, 1404, 339000, 4500)
    session = FakeSession(payload)
    fetch_acs(PLACE_CFG_CHICAGO, vintage=2023, api_key="K", session=session)
    assert session.calls[0][0] == "https://api.census.gov/data/2023/acs/acs5"


def test_api_key_defaults_to_env(monkeypatch):
    monkeypatch.setenv("CENSUS_API_KEY", "FROM_ENV")
    payload = _acs_payload("Chicago city, Illinois", 74590, 1404, 339000, 4500)
    session = FakeSession(payload)
    fetch_acs(PLACE_CFG_CHICAGO, session=session)
    assert session.calls[0][1]["key"] == "FROM_ENV"


def test_no_key_param_when_key_absent(monkeypatch):
    """With no key arg and no env var, 'key' is omitted (caller may want the
    Census Missing-Key body to surface as an error rather than send key=None)."""
    monkeypatch.delenv("CENSUS_API_KEY", raising=False)
    payload = _acs_payload("Chicago city, Illinois", 74590, 1404, 339000, 4500)
    session = FakeSession(payload)
    fetch_acs(PLACE_CFG_CHICAGO, session=session)
    assert "key" not in session.calls[0][1]


# ---------------------------------------------------------------------------
# Parsing by header name + happy-path output shape
# ---------------------------------------------------------------------------
def test_parses_values_by_header_name_not_position():
    payload = _acs_payload("Chicago city, Illinois", 74590, 1404, 339000, 4500)
    session = FakeSession(payload)
    result = fetch_acs(PLACE_CFG_CHICAGO, api_key="K", session=session)

    assert result["median_income"] == 74590
    assert result["median_rent"] == 1404
    assert result["median_home_value"] == 339000
    assert result["median_real_estate_taxes"] == 4500
    # Integers, not strings (the API delivers strings; we coerce).
    assert isinstance(result["median_income"], int)


def test_output_has_exact_schema_context_keys():
    payload = _acs_payload("Chicago city, Illinois", 74590, 1404, 339000, 4500)
    session = FakeSession(payload)
    result = fetch_acs(PLACE_CFG_CHICAGO, api_key="K", session=session)

    assert set(result.keys()) == {
        "median_income",
        "median_rent",
        "median_home_value",
        "median_real_estate_taxes",
        "effective_property_tax_rate",
        "unemployment_rate",
        "aqi",
        "context_score",
    }
    # Fields owned by other Context adapters are emitted as None here.
    assert result["unemployment_rate"] is None
    assert result["aqi"] is None
    assert result["context_score"] is None


def test_parses_even_when_geo_columns_reordered():
    """Header order shouldn't matter: put NAME/state/place AROUND the vars."""
    header = [
        "B25077_001E", "NAME", "state", "B19013_001E",
        "place", "B25103_001E", "B25064_001E",
    ]
    values = ["339000", "Chicago city, Illinois", "17", "74590",
              "14000", "4500", "1404"]
    session = FakeSession([header, values])
    result = fetch_acs(PLACE_CFG_CHICAGO, api_key="K", session=session)
    assert result["median_income"] == 74590
    assert result["median_home_value"] == 339000
    assert result["median_rent"] == 1404
    assert result["median_real_estate_taxes"] == 4500


# ---------------------------------------------------------------------------
# Effective property tax rate (derived, null-safe)
# ---------------------------------------------------------------------------
def test_effective_property_tax_rate_computed():
    payload = _acs_payload("Chicago city, Illinois", 74590, 1404, 300000, 6000)
    session = FakeSession(payload)
    result = fetch_acs(PLACE_CFG_CHICAGO, api_key="K", session=session)
    # 6000 / 300000 == 0.02
    assert result["effective_property_tax_rate"] == pytest.approx(0.02)


def test_effective_rate_none_when_home_value_sentinel():
    """Home value sentinel -> None numerator denom -> rate None (no ZeroDiv)."""
    payload = _acs_payload("X city", 74590, 1404, -666666666, 6000)
    session = FakeSession(payload)
    result = fetch_acs(PLACE_CFG_CHICAGO, api_key="K", session=session)
    assert result["median_home_value"] is None
    assert result["effective_property_tax_rate"] is None


def test_effective_rate_none_when_taxes_missing():
    payload = _acs_payload("X city", 74590, 1404, 300000, -666666666)
    session = FakeSession(payload)
    result = fetch_acs(PLACE_CFG_CHICAGO, api_key="K", session=session)
    assert result["median_real_estate_taxes"] is None
    assert result["effective_property_tax_rate"] is None


def test_effective_rate_none_when_home_value_zero():
    """A literal 0 home value must not raise ZeroDivisionError."""
    payload = _acs_payload("X city", 74590, 1404, 0, 6000)
    session = FakeSession(payload)
    result = fetch_acs(PLACE_CFG_CHICAGO, api_key="K", session=session)
    assert result["median_home_value"] == 0
    assert result["effective_property_tax_rate"] is None


# ---------------------------------------------------------------------------
# Sentinel-negative / missing handling
# ---------------------------------------------------------------------------
def test_sentinel_negative_maps_to_none():
    """The classic -666666666 jam value -> None for that field."""
    payload = _acs_payload("X city", -666666666, 1404, 339000, 4500)
    session = FakeSession(payload)
    result = fetch_acs(PLACE_CFG_CHICAGO, api_key="K", session=session)
    assert result["median_income"] is None
    # Other fields unaffected.
    assert result["median_rent"] == 1404
    assert result["median_home_value"] == 339000


@pytest.mark.parametrize(
    "sentinel",
    ["-999999999", "-888888888", "-666666666", "-555555555",
     "-333333333", "-222222222", "-1"],
)
def test_all_negative_sentinels_map_to_none(sentinel):
    header = ["NAME", "B19013_001E", "B25064_001E", "B25077_001E",
              "B25103_001E", "state", "place"]
    values = ["X city", sentinel, "1404", "339000", "4500", "17", "14000"]
    session = FakeSession([header, values])
    result = fetch_acs(PLACE_CFG_CHICAGO, api_key="K", session=session)
    assert result["median_income"] is None


def test_missing_and_empty_values_map_to_none():
    header = ["NAME", "B19013_001E", "B25064_001E", "B25077_001E",
              "B25103_001E", "state", "place"]
    # income is JSON null, rent is empty string, home value non-numeric junk.
    values = ["X city", None, "", "n/a", "4500", "17", "14000"]
    session = FakeSession([header, values])
    result = fetch_acs(PLACE_CFG_CHICAGO, api_key="K", session=session)
    assert result["median_income"] is None
    assert result["median_rent"] is None
    assert result["median_home_value"] is None
    assert result["median_real_estate_taxes"] == 4500


# ---------------------------------------------------------------------------
# Error paths -> CensusError
# ---------------------------------------------------------------------------
def test_non_200_raises():
    session = FakeSession(FakeResponse(status_code=404, text="Not Found"))
    with pytest.raises(CensusError) as exc:
        fetch_acs(PLACE_CFG_CHICAGO, api_key="K", session=session)
    assert "404" in str(exc.value)


def test_missing_key_html_body_raises():
    """Keyless DATA query returns HTML 'Missing Key' (HTTP 200, non-JSON)."""
    html = (
        "<html><body><h1>Missing Key</h1>"
        "A valid key must be included with each data API request.</body></html>"
    )
    # status 200 but .json() raises ValueError (HTML, not JSON).
    session = FakeSession(FakeResponse(payload=None, status_code=200, text=html))
    with pytest.raises(CensusError) as exc:
        fetch_acs(PLACE_CFG_CHICAGO, session=session)
    assert "key" in str(exc.value).lower()


def test_invalid_key_non_200_surfaces_body():
    session = FakeSession(
        FakeResponse(status_code=403, text="Invalid Key: KEYNOTFOUND")
    )
    with pytest.raises(CensusError) as exc:
        fetch_acs(PLACE_CFG_CHICAGO, api_key="BAD", session=session)
    assert "Invalid Key" in str(exc.value)


def test_transport_failure_raises():
    import requests

    class BoomSession:
        def get(self, *a, **k):
            raise requests.ConnectionError("boom")

    with pytest.raises(CensusError):
        fetch_acs(PLACE_CFG_CHICAGO, api_key="K", session=BoomSession())


def test_one_row_array_raises():
    """Header only, no value row -> not the expected two-row shape."""
    session = FakeSession([["NAME", "B19013_001E", "state", "place"]])
    with pytest.raises(CensusError):
        fetch_acs(PLACE_CFG_CHICAGO, api_key="K", session=session)


def test_non_list_body_raises():
    session = FakeSession({"error": "something"})
    with pytest.raises(CensusError):
        fetch_acs(PLACE_CFG_CHICAGO, api_key="K", session=session)


def test_header_value_width_mismatch_raises():
    session = FakeSession([["NAME", "B19013_001E"], ["X", "74590", "extra"]])
    with pytest.raises(CensusError):
        fetch_acs(PLACE_CFG_CHICAGO, api_key="K", session=session)


def test_missing_variable_in_header_raises():
    """If a requested variable code is absent from the header, raise."""
    header = ["NAME", "B19013_001E", "B25064_001E", "state", "place"]
    values = ["X city", "74590", "1404", "17", "14000"]
    session = FakeSession([header, values])
    with pytest.raises(CensusError) as exc:
        fetch_acs(PLACE_CFG_CHICAGO, api_key="K", session=session)
    assert "B25077_001E" in str(exc.value) or "B25103_001E" in str(exc.value)


# ---------------------------------------------------------------------------
# geo_cfg validation
# ---------------------------------------------------------------------------
def test_missing_geo_raises():
    session = FakeSession([])
    with pytest.raises(CensusError):
        fetch_acs({"state": "17", "place": "14000"}, api_key="K", session=session)
    assert session.calls == []  # never hit the network


def test_missing_state_raises():
    session = FakeSession([])
    with pytest.raises(CensusError):
        fetch_acs({"geo": "place", "place": "14000"}, api_key="K", session=session)


def test_place_geo_missing_place_code_raises():
    session = FakeSession([])
    with pytest.raises(CensusError):
        fetch_acs({"geo": "place", "state": "17"}, api_key="K", session=session)


def test_county_geo_missing_county_code_raises():
    session = FakeSession([])
    with pytest.raises(CensusError):
        fetch_acs({"geo": "county", "state": "12"}, api_key="K", session=session)


def test_unsupported_geo_raises():
    session = FakeSession([])
    with pytest.raises(CensusError):
        fetch_acs(
            {"geo": "tract", "state": "12", "tract": "000100"},
            api_key="K", session=session,
        )


# ---------------------------------------------------------------------------
# Default session wiring
# ---------------------------------------------------------------------------
def test_default_session_is_a_requests_session():
    import requests

    assert isinstance(census._SESSION, requests.Session)


def test_uses_module_session_when_none(monkeypatch):
    recorded = {}
    payload = _acs_payload("Chicago city, Illinois", 74590, 1404, 339000, 4500)

    def fake_get(url, params=None, timeout=None):
        recorded["url"] = url
        recorded["params"] = dict(params or {})
        return FakeResponse(payload)

    monkeypatch.setattr(census._SESSION, "get", fake_get)
    result = fetch_acs(PLACE_CFG_CHICAGO, api_key="K")
    assert result["median_income"] == 74590
    assert recorded["url"] == ACS_URL_2024
    assert recorded["params"]["for"] == "place:14000"
