"""fetch_money_flows.py — ICI legs must never vanish silently.

ici.org answers the .xls downloads with HTTP 403 (verified 2026-10-04), which
used to leave data-mmf.json / data-mf-flows.json with `weekly: []` and no
explanation, so the Money Flow tab simply dropped both blocks. Offline: all
HTTP is stubbed.
"""
from __future__ import annotations

import fetch_money_flows as fmf


class _Resp:
    def __init__(self, status, text="", content=b"", payload=None):
        self.status_code = status
        self.text = text
        self.content = content
        self._payload = payload

    def json(self):
        return self._payload


_FRED_CSV = ("observation_date,WRMFNS\n"
             "2026-08-17,3034.8\n2026-08-24,3037.4\n2026-08-31,3034.0\n")


def _fake_get(url, headers=None, timeout=None, params=None):
    if "ici.org" in url:
        return _Resp(403)
    if "fredgraph.csv" in url:
        return _Resp(200, text=_FRED_CSV)
    return _Resp(404)


def test_mmf_falls_back_to_fred_retail_when_ici_403s(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.setattr(fmf.requests, "get", _fake_get)
    m = fmf.fetch_mmf()
    assert m["available"] is True and m["partial"] is True
    assert m["fallback"] == "fred_wrmfns"
    assert "HTTP 403" in m["ici_unavailable_reason"]
    assert m["as_of"] == "2026-08-31"            # data date, not fetch time
    last = m["weekly"][-1]
    assert last == {"date": "2026-08-31", "total": None, "retail": 3034.0,
                    "institutional": None}
    # Retail is not the total: the TOTAL WoW field the composite reads stays null.
    assert m["wow_change"] is None
    assert m["retail_wow_change"] == -3.4
    assert "retail" in m["note"].lower()


def test_mmf_marked_unavailable_when_fred_also_fails(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.setattr(fmf.requests, "get",
                        lambda url, **kw: _Resp(403 if "ici.org" in url else 500))
    m = fmf.fetch_mmf()
    assert m["available"] is False
    assert m["weekly"] == [] and m["as_of"] is None
    assert "HTTP 403" in m["unavailable_reason"]
    assert "FRED" in m["unavailable_reason"]


def test_mf_flows_marked_unavailable_with_reason(monkeypatch):
    monkeypatch.setattr(fmf.requests, "get", _fake_get)
    f = fmf.fetch_mf_flows()
    assert f["available"] is False
    assert f["weekly"] == [] and f["as_of"] is None
    assert "flows_data_" in f["unavailable_reason"] and "HTTP 403" in f["unavailable_reason"]
    assert "no free equivalent" in f["unavailable_reason"]


def test_fred_api_used_when_key_set_and_key_not_in_payload(monkeypatch):
    seen = {}

    def get(url, headers=None, timeout=None, params=None):
        if "ici.org" in url:
            return _Resp(403)
        if "api.stlouisfed.org" in url:
            seen["params"] = params
            return _Resp(200, payload={"observations": [
                {"date": "2026-08-24", "value": "3037.4"},
                {"date": "2026-08-31", "value": "3034.0"}]})
        raise AssertionError("CSV fallback should not be needed")

    monkeypatch.setenv("FRED_API_KEY", "sekret-test-key")
    monkeypatch.setattr(fmf.requests, "get", get)
    m = fmf.fetch_mmf()
    assert seen["params"]["series_id"] == "WRMFNS"
    assert m["weekly"][-1]["retail"] == 3034.0
    assert "sekret-test-key" not in repr(m)
