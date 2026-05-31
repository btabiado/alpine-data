"""Tests for the BLS LAUS unemployment adapter (city/bls.py).

All HTTP is mocked via a tiny fake session that records each ``.post(...)`` and
returns a canned BLS JSON payload — no live network in pytest. The live smoke
(keyless POST against api.bls.gov for at least two cities) is run separately
during recon; the resolved/verified series IDs live in
``city.bls.CITY_LAUS_SERIES``.
"""
from __future__ import annotations

import pytest

from city import bls
from city.bls import BLSError, CITY_LAUS_SERIES, fetch_unemployment


# ---------------------------------------------------------------------------
# Fake HTTP session (POST-based, mirrors the BLS v2 timeseries endpoint)
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
    """Records POST calls and replays a queue of responses (or a single one)."""

    def __init__(self, responses):
        # ``responses`` may be a list (consumed in order) or a single payload /
        # FakeResponse / Exception reused for every call.
        self._responses = responses
        self.calls = []  # list of dicts: {"url","json","timeout"}

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        resp = self._responses
        if isinstance(resp, list):
            resp = resp.pop(0)
        if isinstance(resp, FakeResponse):
            return resp
        return FakeResponse(resp)


# ---------------------------------------------------------------------------
# Canned BLS responses
# ---------------------------------------------------------------------------
def _bls_ok(series_id, data_rows):
    """A REQUEST_SUCCEEDED envelope wrapping one series with ``data_rows``."""
    return {
        "status": "REQUEST_SUCCEEDED",
        "responseTime": 12,
        "message": [],
        "Results": {"series": [{"seriesID": series_id, "data": data_rows}]},
    }


# Newest-first, exactly as BLS returns it, with the newest tagged latest:"true".
_CHICAGO_ROWS = [
    {"year": "2026", "period": "M03", "periodName": "March",
     "latest": "true", "value": "5.1", "footnotes": [{"code": "P", "text": "Preliminary."}]},
    {"year": "2026", "period": "M02", "periodName": "February",
     "value": "5.3", "footnotes": [{}]},
    {"year": "2026", "period": "M01", "periodName": "January",
     "value": "5.4", "footnotes": [{}]},
]


# ---------------------------------------------------------------------------
# Resolved series-ID map sanity
# ---------------------------------------------------------------------------
def test_city_laus_series_covers_all_six_cities_and_well_formed():
    assert set(CITY_LAUS_SERIES) == {"chicago", "nyc", "la", "seattle", "sf", "miami"}
    for city_id, series in CITY_LAUS_SERIES.items():
        # LAUS series id = LAU + (CT|CN) + 13-char area code + measure, == 20 chars.
        assert len(series) == 20, (city_id, series)
        assert series.startswith("LAU"), series
        assert series[3:5] in ("CT", "CN"), series
        assert series.endswith("03"), f"{city_id} measure must be 03 (unemployment rate)"
    # Miami is the county ("CN") footprint; everyone else is a city ("CT") place.
    assert CITY_LAUS_SERIES["miami"][3:5] == "CN"
    for city_id in ("chicago", "nyc", "la", "seattle", "sf"):
        assert CITY_LAUS_SERIES[city_id][3:5] == "CT"


# ---------------------------------------------------------------------------
# Happy path: parse the latest value
# ---------------------------------------------------------------------------
def test_fetch_unemployment_parses_latest_value():
    session = FakeSession(_bls_ok(CITY_LAUS_SERIES["chicago"], _CHICAGO_ROWS))
    rate = fetch_unemployment("chicago", session=session)
    assert rate == 5.1
    assert isinstance(rate, float)

    # One POST to the v2 timeseries endpoint carrying the right series id.
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"] == "https://api.bls.gov/publicAPI/v2/timeseries/data/"
    assert call["json"]["seriesid"] == [CITY_LAUS_SERIES["chicago"]]


def test_fetch_unemployment_picks_latest_flag_not_array_position():
    """Honor latest:"true" even when it isn't the first array element."""
    rows = [
        {"year": "2025", "period": "M12", "value": "6.0"},
        {"year": "2026", "period": "M03", "latest": "true", "value": "5.1"},
        {"year": "2026", "period": "M02", "value": "5.3"},
    ]
    session = FakeSession(_bls_ok(CITY_LAUS_SERIES["la"], rows))
    assert fetch_unemployment("la", session=session) == 5.1


def test_fetch_unemployment_falls_back_to_max_year_month_without_latest_flag():
    """No latest flag anywhere -> pick max by (year, month), ignoring order."""
    rows = [
        {"year": "2026", "period": "M01", "value": "5.4"},
        {"year": "2026", "period": "M03", "value": "5.1"},  # newest month
        {"year": "2025", "period": "M12", "value": "6.0"},
    ]
    session = FakeSession(_bls_ok(CITY_LAUS_SERIES["sf"], rows))
    assert fetch_unemployment("sf", session=session) == 5.1


def test_fetch_unemployment_ignores_annual_average_m13_row():
    """'M13' (annual average) must not be treated as the latest month."""
    rows = [
        {"year": "2026", "period": "M13", "periodName": "Annual", "value": "9.9"},
        {"year": "2026", "period": "M04", "latest": "true", "value": "4.8"},
    ]
    session = FakeSession(_bls_ok(CITY_LAUS_SERIES["nyc"], rows))
    assert fetch_unemployment("nyc", session=session) == 4.8


# ---------------------------------------------------------------------------
# None on empty / unusable series
# ---------------------------------------------------------------------------
def test_fetch_unemployment_none_on_empty_data_array():
    session = FakeSession(_bls_ok(CITY_LAUS_SERIES["seattle"], []))
    assert fetch_unemployment("seattle", session=session) is None


def test_fetch_unemployment_none_on_no_series_block():
    payload = {"status": "REQUEST_SUCCEEDED", "message": [], "Results": {"series": []}}
    session = FakeSession(payload)
    assert fetch_unemployment("miami", session=session) is None


def test_fetch_unemployment_none_on_suppressed_value():
    """BLS uses '-' for suppressed/unavailable values -> None, not a crash."""
    rows = [{"year": "2026", "period": "M03", "latest": "true", "value": "-"}]
    session = FakeSession(_bls_ok(CITY_LAUS_SERIES["chicago"], rows))
    assert fetch_unemployment("chicago", session=session) is None


# ---------------------------------------------------------------------------
# Error envelope + HTTP + parse failures -> BLSError
# ---------------------------------------------------------------------------
def test_fetch_unemployment_raises_on_in_band_error_envelope():
    """HTTP 200 but status REQUEST_NOT_PROCESSED (e.g. daily threshold)."""
    payload = {
        "status": "REQUEST_NOT_PROCESSED",
        "responseTime": 0,
        "message": ["Daily threshold for series exceeded."],
        "Results": {},
    }
    session = FakeSession(payload)
    with pytest.raises(BLSError) as exc:
        fetch_unemployment("chicago", session=session)
    assert "REQUEST_NOT_PROCESSED" in str(exc.value)


def test_fetch_unemployment_raises_on_non_200():
    session = FakeSession(FakeResponse({}, status_code=500, text="Internal Error"))
    with pytest.raises(BLSError) as exc:
        fetch_unemployment("chicago", session=session)
    assert "500" in str(exc.value)


def test_fetch_unemployment_raises_on_malformed_json():
    session = FakeSession(FakeResponse(ValueError("no json"), status_code=200))
    with pytest.raises(BLSError):
        fetch_unemployment("chicago", session=session)


def test_fetch_unemployment_raises_on_non_numeric_value():
    rows = [{"year": "2026", "period": "M03", "latest": "true", "value": "N/A"}]
    session = FakeSession(_bls_ok(CITY_LAUS_SERIES["chicago"], rows))
    with pytest.raises(BLSError):
        fetch_unemployment("chicago", session=session)


def test_fetch_unemployment_raises_on_transport_error():
    import requests as _requests

    class BoomSession:
        def post(self, *a, **k):
            raise _requests.RequestException("connection reset")

    with pytest.raises(BLSError):
        fetch_unemployment("chicago", session=BoomSession())


def test_fetch_unemployment_raises_on_unknown_city():
    with pytest.raises(BLSError) as exc:
        fetch_unemployment("boston", session=FakeSession({}))
    assert "boston" in str(exc.value)


# ---------------------------------------------------------------------------
# registrationkey handling (present iff a key is set; never sent otherwise)
# ---------------------------------------------------------------------------
def test_registrationkey_included_when_api_key_passed():
    session = FakeSession(_bls_ok(CITY_LAUS_SERIES["chicago"], _CHICAGO_ROWS))
    fetch_unemployment("chicago", api_key="SECRETKEY123", session=session)
    body = session.calls[0]["json"]
    assert body.get("registrationkey") == "SECRETKEY123"


def test_registrationkey_omitted_when_no_key(monkeypatch):
    """Keyless is valid: no env var, no explicit key -> field absent entirely."""
    monkeypatch.delenv("BLS_API_KEY", raising=False)
    session = FakeSession(_bls_ok(CITY_LAUS_SERIES["chicago"], _CHICAGO_ROWS))
    fetch_unemployment("chicago", session=session)
    body = session.calls[0]["json"]
    assert "registrationkey" not in body


def test_registrationkey_defaults_to_env_var(monkeypatch):
    monkeypatch.setenv("BLS_API_KEY", "ENVKEY456")
    session = FakeSession(_bls_ok(CITY_LAUS_SERIES["chicago"], _CHICAGO_ROWS))
    fetch_unemployment("chicago", session=session)
    assert session.calls[0]["json"].get("registrationkey") == "ENVKEY456"


def test_explicit_api_key_overrides_env_var(monkeypatch):
    monkeypatch.setenv("BLS_API_KEY", "ENVKEY456")
    session = FakeSession(_bls_ok(CITY_LAUS_SERIES["chicago"], _CHICAGO_ROWS))
    fetch_unemployment("chicago", api_key="EXPLICIT789", session=session)
    assert session.calls[0]["json"].get("registrationkey") == "EXPLICIT789"


# ---------------------------------------------------------------------------
# Module-level default session fallback (no session= passed)
# ---------------------------------------------------------------------------
def test_uses_module_session_when_none_passed(monkeypatch):
    captured = {}

    class _Sess:
        def post(self, url, json=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            return FakeResponse(_bls_ok(CITY_LAUS_SERIES["chicago"], _CHICAGO_ROWS))

    monkeypatch.setattr(bls, "_SESSION", _Sess())
    monkeypatch.delenv("BLS_API_KEY", raising=False)
    rate = fetch_unemployment("chicago")
    assert rate == 5.1
    assert captured["json"]["seriesid"] == [CITY_LAUS_SERIES["chicago"]]
