"""Tests for the EPA AirNow AQI adapter (city/airnow.py).

AirNow REQUIRES an API key we do not have, so every test mocks HTTP against the
documented current-observation response shape (a JSON list of per-parameter
observation objects). No live network in pytest.

Conventions mirror tests/test_city_arcgis.py (FakeSession/FakeResponse) and
tests/test_fred.py (no HTTP call when the key is missing; canned JSON payloads).
"""
from __future__ import annotations

import pytest

from city import airnow
from city.airnow import AirNowError, CITY_LATLON, fetch_aqi


AIRNOW_URL = "https://www.airnowapi.org/aq/observation/latLong/current/"


# ---------------------------------------------------------------------------
# Fake HTTP session
# ---------------------------------------------------------------------------
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
    """Records GET calls and replays a queue of responses (AirNow .get shape)."""

    def __init__(self, responses):
        self._responses = responses
        self.calls = []  # list of (url, params) tuples

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        payload = self._responses.pop(0)
        if isinstance(payload, FakeResponse):
            return payload
        return FakeResponse(payload)


def _multi_param_payload(o3=41, pm25=58, pm10=22):
    """Canned current-observation list mirroring the live AirNow shape:
    one object per reporting parameter (O3, PM2.5, PM10)."""
    return [
        {
            "DateObserved": "2026-05-31", "HourObserved": 14, "LocalTimeZone": "EST",
            "ReportingArea": "Test Area", "StateCode": "ZZ",
            "Latitude": 0.0, "Longitude": 0.0,
            "ParameterName": "O3", "AQI": o3,
            "Category": {"Number": 1, "Name": "Good"},
        },
        {
            "DateObserved": "2026-05-31", "HourObserved": 14, "LocalTimeZone": "EST",
            "ReportingArea": "Test Area", "StateCode": "ZZ",
            "Latitude": 0.0, "Longitude": 0.0,
            "ParameterName": "PM2.5", "AQI": pm25,
            "Category": {"Number": 2, "Name": "Moderate"},
        },
        {
            "DateObserved": "2026-05-31", "HourObserved": 14, "LocalTimeZone": "EST",
            "ReportingArea": "Test Area", "StateCode": "ZZ",
            "Latitude": 0.0, "Longitude": 0.0,
            "ParameterName": "PM10", "AQI": pm10,
            "Category": {"Number": 1, "Name": "Good"},
        },
    ]


# ---------------------------------------------------------------------------
# 1. Happy path: max AQI across parameters
# ---------------------------------------------------------------------------
def test_fetch_aqi_returns_max_across_parameters(monkeypatch):
    """The reported overall AQI is the worst pollutant -> return the MAX AQI."""
    monkeypatch.setenv("AIRNOW_API_KEY", "test-key-deadbeef")
    session = FakeSession([_multi_param_payload(o3=41, pm25=58, pm10=22)])

    result = fetch_aqi("miami", session=session)

    # max(41, 58, 22) == 58, the PM2.5 value.
    assert result == 58
    assert isinstance(result, int)
    assert len(session.calls) == 1


def test_fetch_aqi_max_picks_ozone_when_it_is_worst(monkeypatch):
    """Sanity: the max isn't hardcoded to PM2.5 — ozone wins when it's worst."""
    monkeypatch.setenv("AIRNOW_API_KEY", "k")
    session = FakeSession([_multi_param_payload(o3=120, pm25=55, pm10=40)])
    assert fetch_aqi("la", session=session) == 120


def test_fetch_aqi_single_parameter(monkeypatch):
    """A one-element list returns that single AQI."""
    monkeypatch.setenv("AIRNOW_API_KEY", "k")
    payload = [{"ParameterName": "PM2.5", "AQI": 73}]
    session = FakeSession([payload])
    assert fetch_aqi("sf", session=session) == 73


# ---------------------------------------------------------------------------
# 2. Request shape
# ---------------------------------------------------------------------------
def test_fetch_aqi_request_shape(monkeypatch):
    """URL + query params match the documented endpoint and the city's centroid."""
    monkeypatch.setenv("AIRNOW_API_KEY", "secret-key")
    session = FakeSession([_multi_param_payload()])

    fetch_aqi("chicago", session=session)

    url, params = session.calls[0]
    assert url == AIRNOW_URL
    assert params["format"] == "application/json"
    assert params["distance"] == 50
    assert params["API_KEY"] == "secret-key"
    # Coordinates come from CITY_LATLON, not hardcoded.
    lat, lon = CITY_LATLON["chicago"]
    assert params["latitude"] == lat
    assert params["longitude"] == lon


def test_fetch_aqi_explicit_key_overrides_env(monkeypatch):
    """An explicit api_key= is used even when the env var is set differently."""
    monkeypatch.setenv("AIRNOW_API_KEY", "env-key")
    session = FakeSession([_multi_param_payload()])
    fetch_aqi("nyc", api_key="explicit-key", session=session)
    assert session.calls[0][1]["API_KEY"] == "explicit-key"


# ---------------------------------------------------------------------------
# 3. None on empty list
# ---------------------------------------------------------------------------
def test_fetch_aqi_empty_list_returns_none(monkeypatch):
    """No monitor reported within range -> AirNow returns [] -> None."""
    monkeypatch.setenv("AIRNOW_API_KEY", "k")
    session = FakeSession([[]])
    assert fetch_aqi("seattle", session=session) is None
    assert len(session.calls) == 1  # it DID query; the list was just empty


def test_fetch_aqi_all_values_unusable_returns_none(monkeypatch):
    """AirNow uses -1 / null for 'no current value'; all-missing -> None."""
    monkeypatch.setenv("AIRNOW_API_KEY", "k")
    payload = [
        {"ParameterName": "O3", "AQI": -1},
        {"ParameterName": "PM2.5", "AQI": None},
    ]
    session = FakeSession([payload])
    assert fetch_aqi("sf", session=session) is None


def test_fetch_aqi_skips_sentinel_but_keeps_valid(monkeypatch):
    """A -1 sentinel is ignored; the remaining valid value is returned."""
    monkeypatch.setenv("AIRNOW_API_KEY", "k")
    payload = [
        {"ParameterName": "O3", "AQI": -1},
        {"ParameterName": "PM2.5", "AQI": 47},
    ]
    session = FakeSession([payload])
    assert fetch_aqi("sf", session=session) == 47


# ---------------------------------------------------------------------------
# 4. No key -> None and NO HTTP call (mirrors test_fred convention)
# ---------------------------------------------------------------------------
def test_fetch_aqi_no_key_returns_none_without_http(monkeypatch):
    """Without AIRNOW_API_KEY, fetch_aqi() returns None and never calls .get."""
    monkeypatch.delenv("AIRNOW_API_KEY", raising=False)

    def _explode(*args, **kwargs):
        raise AssertionError(
            "fetch_aqi() should not issue an HTTP request when AIRNOW_API_KEY is unset"
        )

    # Guard both a passed session and the module-level default session.
    session = FakeSession([])  # would IndexError if .get fired
    monkeypatch.setattr(airnow._SESSION, "get", _explode)

    assert fetch_aqi("miami", session=session) is None
    assert session.calls == []


def test_fetch_aqi_empty_string_key_treated_as_missing(monkeypatch):
    """An empty/whitespace env key is falsy -> short-circuit, no HTTP."""
    monkeypatch.setenv("AIRNOW_API_KEY", "")
    session = FakeSession([])
    assert fetch_aqi("nyc", session=session) is None
    assert session.calls == []


# ---------------------------------------------------------------------------
# 5. AirNowError on non-200
# ---------------------------------------------------------------------------
def test_fetch_aqi_non_200_raises(monkeypatch):
    monkeypatch.setenv("AIRNOW_API_KEY", "k")
    session = FakeSession([FakeResponse(None, status_code=403, text="Forbidden")])
    with pytest.raises(AirNowError):
        fetch_aqi("chicago", session=session)


def test_fetch_aqi_non_200_500_raises(monkeypatch):
    monkeypatch.setenv("AIRNOW_API_KEY", "k")
    session = FakeSession([FakeResponse([], status_code=500, text="oops")])
    with pytest.raises(AirNowError):
        fetch_aqi("la", session=session)


def test_fetch_aqi_malformed_json_raises(monkeypatch):
    """A 200 whose body isn't JSON maps to AirNowError, not a raw ValueError."""
    monkeypatch.setenv("AIRNOW_API_KEY", "k")
    session = FakeSession([FakeResponse(ValueError("no json here"), status_code=200)])
    with pytest.raises(AirNowError):
        fetch_aqi("sf", session=session)


def test_fetch_aqi_non_list_payload_raises(monkeypatch):
    """The endpoint must return a list; a dict envelope is a parse failure."""
    monkeypatch.setenv("AIRNOW_API_KEY", "k")
    session = FakeSession([{"error": "bad request"}])
    with pytest.raises(AirNowError):
        fetch_aqi("nyc", session=session)


def test_fetch_aqi_transport_error_raises(monkeypatch):
    """A requests transport exception is wrapped as AirNowError."""
    import requests

    monkeypatch.setenv("AIRNOW_API_KEY", "k")

    class _BoomSession:
        def __init__(self):
            self.calls = []

        def get(self, url, params=None, timeout=None):
            self.calls.append((url, params))
            raise requests.RequestException("connection reset")

    with pytest.raises(AirNowError):
        fetch_aqi("chicago", session=_BoomSession())


# ---------------------------------------------------------------------------
# 6. Unknown city -> None (no HTTP)
# ---------------------------------------------------------------------------
def test_fetch_aqi_unknown_city_returns_none(monkeypatch):
    monkeypatch.setenv("AIRNOW_API_KEY", "k")
    session = FakeSession([])  # would IndexError if .get fired
    assert fetch_aqi("atlantis", session=session) is None
    assert session.calls == []


# ---------------------------------------------------------------------------
# 7. CITY_LATLON covers exactly the six registry cities
# ---------------------------------------------------------------------------
def test_city_latlon_covers_six_cities():
    assert set(CITY_LATLON) == {"chicago", "nyc", "la", "seattle", "sf", "miami"}
    for city_id, (lat, lon) in CITY_LATLON.items():
        # Plausible CONUS bounds: lat ~24..49 N, lon ~ -125..-66 W.
        assert 24.0 <= lat <= 49.5, f"{city_id} lat out of CONUS range: {lat}"
        assert -125.0 <= lon <= -66.0, f"{city_id} lon out of CONUS range: {lon}"


# ---------------------------------------------------------------------------
# 8. Default session wiring
# ---------------------------------------------------------------------------
def test_default_session_is_a_requests_session():
    import requests

    assert isinstance(airnow._SESSION, requests.Session)


def test_fetch_aqi_uses_module_session_when_none(monkeypatch):
    """When session= is omitted, the call goes through the module-level session."""
    monkeypatch.setenv("AIRNOW_API_KEY", "k")
    recorded = {}

    def fake_get(url, params=None, timeout=None):
        recorded["url"] = url
        recorded["params"] = dict(params or {})
        return FakeResponse(_multi_param_payload(o3=10, pm25=88, pm10=30))

    monkeypatch.setattr(airnow._SESSION, "get", fake_get)
    result = fetch_aqi("seattle")
    assert result == 88
    assert recorded["url"] == AIRNOW_URL
