"""Tests for the FRED macro overlay (Tier 2 data expansion).

Covers:
  - fetch_fred() returns {"available": False, ...} when FRED_API_KEY is missing
  - fetch_fred() filters FRED's "." sentinel for missing observations
  - build_insights() does not crash when market.fred is absent
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

import fetch_market
from insights import build_insights


# ---------- 1. No-key path ----------

def test_fetch_fred_no_key_returns_unavailable(monkeypatch):
    """Without FRED_API_KEY in env, fetch_fred() returns available=False and never raises."""
    # Wipe both possible env var names to guarantee absence.
    monkeypatch.delenv("FRED_API_KEY", raising=False)

    # Also assert that no HTTP call is made when the key is missing.
    def _explode(*args, **kwargs):
        raise AssertionError("fetch_fred() should not call requests.get when FRED_API_KEY is unset")

    monkeypatch.setattr(fetch_market.requests, "get", _explode)

    out = fetch_market.fetch_fred()
    assert isinstance(out, dict)
    assert out.get("available") is False
    assert "fetched_at" in out
    # Should not have populated series keys when disabled.
    assert "dxy" not in out
    assert "sp500" not in out


# ---------- 2. Skip "." values ----------

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload


def test_fetch_fred_skips_dots(monkeypatch):
    """FRED uses '.' as a missing-data sentinel — fetch_fred must filter those out."""
    monkeypatch.setenv("FRED_API_KEY", "test-key-deadbeef")

    # Every series_id query returns the same payload with a mix of real values,
    # dots, empties, and non-numeric junk.
    payload = {
        "observations": [
            {"date": "2024-01-01", "value": "100.5"},
            {"date": "2024-01-02", "value": "."},       # skip
            {"date": "2024-01-03", "value": ""},          # skip
            {"date": "2024-01-04", "value": "101.25"},
            {"date": "2024-01-05", "value": "not-a-num"}, # skip (ValueError)
            {"date": "2024-01-06", "value": "102.0"},
        ]
    }

    def _fake_get(url, params=None, headers=None, timeout=None):
        # Sanity check: the URL is the FRED observations endpoint.
        assert "stlouisfed.org" in url
        assert params is not None and params.get("api_key") == "test-key-deadbeef"
        return _FakeResp(payload)

    monkeypatch.setattr(fetch_market.requests, "get", _fake_get)

    out = fetch_market.fetch_fred()
    assert out.get("available") is True

    # All 5 series should be present and equally filtered.
    for key in ("dxy", "sp500", "gold", "treasury_10y", "m2"):
        rows = out.get(key)
        assert isinstance(rows, list), f"{key} should be a list"
        # 3 valid rows: 2024-01-01, 2024-01-04, 2024-01-06
        assert len(rows) == 3, f"{key} expected 3 rows after filtering, got {len(rows)}"
        assert rows[0] == {"date": "2024-01-01", "value": 100.5}
        assert rows[1] == {"date": "2024-01-04", "value": 101.25}
        assert rows[2] == {"date": "2024-01-06", "value": 102.0}
        # Make sure no "." leaked through.
        for r in rows:
            assert r["value"] != "."
            assert isinstance(r["value"], float)


# ---------- 3. Insights handles missing FRED ----------

def test_insights_handles_missing_fred():
    """build_insights() must not raise when market.fred is absent."""
    # Minimal synthetic payload — no 'fred' key on market.
    payload = {
        "btc": {"daily": [], "stats": {}},
        "eth": {"daily": [], "stats": {}},
        "signals": {},
        "market": {
            "fear_greed": [],
            "btc": {"price": [], "funding": [], "dvol": []},
            "eth": {"price": [], "funding": [], "dvol": []},
            "link": {"price": [], "funding": [], "dvol": []},
            # explicitly NO 'fred' key
        },
    }
    # Should return a list (possibly empty) without raising.
    insights = build_insights(payload)
    assert isinstance(insights, list)

    # Also: an explicit available=False fred should not generate FRED insights.
    payload["market"]["fred"] = {"available": False, "fetched_at": datetime.now(timezone.utc).isoformat()}
    insights2 = build_insights(payload)
    assert isinstance(insights2, list)
    # None of the headlines should mention DXY/Treasury/Gold/SP500 macro phrases.
    for ins in insights2:
        head = (ins.get("headline") or "").lower()
        assert "dxy" not in head
        assert "10y treasury yield" not in head
        assert "s&p 500" not in head
