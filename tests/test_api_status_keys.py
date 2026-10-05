"""api_status probes must send a configured key the way the fetcher does.

Audit 2026-10-04: FRED / FRED ENPLANE (400) and AirNow (401) read
``auth_required`` with ``key_state=set`` because ``_probe_one`` never attached
the key — the verdict described the keyless probe, not the source. Offline:
urlopen is replaced with a fake that records each request.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse

import api_status

SECRET = "sekret-KEY/with+chars"


def _target(label):
    return next(t for t in api_status.TARGETS if t["label"] == label)


class _Resp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        return b"{}"


def _recording_urlopen(calls, fail_with=None):
    def fake(req, timeout=None, context=None):
        calls.append({"url": req.full_url, "headers": dict(req.header_items())})
        if fail_with is not None:
            raise fail_with
        return _Resp()
    return fake


def test_fred_probe_sends_api_key_param_when_set(monkeypatch):
    calls = []
    monkeypatch.setenv("FRED_API_KEY", SECRET)
    monkeypatch.setattr(api_status.urllib.request, "urlopen", _recording_urlopen(calls))
    row = api_status._probe_one(_target("FRED"), timeout=1, attempts=1)
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(calls[0]["url"]).query))
    assert q["api_key"] == SECRET and q["file_type"] == "json"
    assert row["verdict"] == "up" and row["key_sent"] is True
    assert SECRET not in json.dumps(row)


def test_airnow_empty_param_is_replaced_not_duplicated(monkeypatch):
    calls = []
    monkeypatch.setenv("AIRNOW_API_KEY", SECRET)
    monkeypatch.setattr(api_status.urllib.request, "urlopen", _recording_urlopen(calls))
    api_status._probe_one(_target("EPA AirNow"), timeout=1, attempts=1)
    pairs = urllib.parse.parse_qsl(urllib.parse.urlsplit(calls[0]["url"]).query,
                                   keep_blank_values=True)
    assert [v for k, v in pairs if k == "API_KEY"] == [SECRET]


def test_header_keys_for_cryptocompare_and_socrata(monkeypatch):
    calls = []
    monkeypatch.setenv("CRYPTOCOMPARE_API_KEY", SECRET)
    monkeypatch.setenv("SOCRATA_APP_TOKEN", SECRET)
    monkeypatch.setattr(api_status.urllib.request, "urlopen", _recording_urlopen(calls))
    api_status._probe_one(_target("CryptoCompare CCCAGG"), timeout=1, attempts=1)
    api_status._probe_one(_target("Socrata · Chicago"), timeout=1, attempts=1)
    h0 = {k.lower(): v for k, v in calls[0]["headers"].items()}
    h1 = {k.lower(): v for k, v in calls[1]["headers"].items()}
    assert h0["authorization"] == f"Apikey {SECRET}"
    assert h1["x-app-token"] == SECRET
    assert SECRET not in calls[0]["url"] and SECRET not in calls[1]["url"]


def test_unset_key_probes_the_url_as_written(monkeypatch):
    calls = []
    monkeypatch.setenv("FRED_API_KEY", "")
    monkeypatch.setattr(api_status.urllib.request, "urlopen", _recording_urlopen(calls))
    row = api_status._probe_one(_target("FRED ENPLANE (air travel)"), timeout=1, attempts=1)
    assert calls[0]["url"] == _target("FRED ENPLANE (air travel)")["url"]
    assert row["key_sent"] is False and row["key_state"] == "unset"


def test_key_never_leaks_into_note_on_failure(monkeypatch):
    calls = []
    monkeypatch.setenv("FRED_API_KEY", SECRET)
    url_with_key = "https://api.stlouisfed.org/fred/releases?api_key=" + urllib.parse.quote(SECRET, safe="")
    boom = urllib.error.URLError(f"failed GET {url_with_key} raw {SECRET}")
    monkeypatch.setattr(api_status.urllib.request, "urlopen",
                        _recording_urlopen(calls, fail_with=boom))
    row = api_status._probe_one(_target("FRED"), timeout=1, attempts=1)
    assert row["verdict"] == "down"
    blob = json.dumps(row)
    assert SECRET not in blob and urllib.parse.quote(SECRET, safe="") not in blob


def test_rejected_key_still_reads_auth_required(monkeypatch):
    calls = []
    monkeypatch.setenv("AIRNOW_API_KEY", SECRET)
    err = urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
    monkeypatch.setattr(api_status.urllib.request, "urlopen",
                        _recording_urlopen(calls, fail_with=err))
    row = api_status._probe_one(_target("EPA AirNow"), timeout=1, attempts=1)
    assert row["verdict"] == "auth_required" and row["key_sent"] is True
