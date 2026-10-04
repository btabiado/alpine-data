"""Three upstreams that stopped answering keyless requests the way we asked.

Live state on 2026-10-04 (data/health/api_status.json):

* DeFiLlama bridges -> HTTP 402. Bridges are Pro-only now; there is no
  keyless equivalent, so the card must SAY it is unavailable (it used to hide
  itself, leaving no trace that a panel had gone).
* CoinDesk CADLI -> HTTP 401 "API key required". The whole CoinDesk Data API
  is keyed now; the fetcher sends CRYPTOCOMPARE_API_KEY when it is set and
  otherwise ships a status record with the reason.
* Santiment -> HTTP 400 from the PROBE only: a bare GET of /graphql has no
  query document. The fetcher worked, but its hour-0 gate missed most days
  because GitHub fires the hourly cron only a few times a day.

All offline: every network call is monkeypatched.
"""
from __future__ import annotations

import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import api_status
import fetch_market

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 4, 19, 27, tzinfo=timezone.utc)


def _target(label: str) -> dict:
    hits = [t for t in api_status.TARGETS if t["label"] == label]
    assert len(hits) == 1, label
    return hits[0]


# --------------------------------------------------------------------------
# DeFiLlama bridges
# --------------------------------------------------------------------------

BRIDGES_BODY = {
    "bridges": [
        {"name": "small", "displayName": "Small Bridge", "lastDailyVolume": 5,
         "lastWeeklyVolume": 30, "lastMonthlyVolume": 100, "chains": ["Ethereum"]},
        {"name": "big", "lastDailyVolume": 900, "lastWeeklyVolume": 6000,
         "lastMonthlyVolume": 20000, "chains": ["Ethereum", "Arbitrum"]},
        {"name": "novol", "lastDailyVolume": None},
        "not-a-dict",
    ]
}


def test_bridges_rows_sorted_by_daily_volume_and_named():
    rows = fetch_market._bridges_rows(BRIDGES_BODY)
    assert [r["name"] for r in rows] == ["big", "Small Bridge", "novol"]
    assert rows[0] == {
        "name": "big", "daily_volume_usd": 900, "weekly_volume_usd": 6000,
        "monthly_volume_usd": 20000, "chains": ["Ethereum", "Arbitrum"],
    }


def test_bridges_rows_caps_at_ten_and_tolerates_junk():
    body = {"bridges": [{"name": f"b{i}", "lastDailyVolume": i} for i in range(25)]}
    assert len(fetch_market._bridges_rows(body)) == 10
    for junk in (None, [], "x", {"bridges": None}, {"bridges": "nope"}):
        assert fetch_market._bridges_rows(junk) == []


def test_bridges_402_is_reported_not_hidden(monkeypatch):
    monkeypatch.setattr(fetch_market, "_get_status", lambda *a, **k: (402, None))
    out = fetch_market.defillama_bridges(now=NOW)
    assert out["top_bridges"] == []          # schema: still a list
    assert out["available"] is False
    assert out["http_status"] == 402
    assert "paid" in out["reason"] and "402" in out["reason"]
    assert out["checked_at"] == "2026-10-04T19:27:00+00:00"


@pytest.mark.parametrize("status,body,needle", [
    (None, None, "did not respond"),
    (200, {"bridges": []}, "returned no bridges"),
    (500, None, "HTTP 500"),
])
def test_bridges_other_failures_name_their_cause(monkeypatch, status, body, needle):
    monkeypatch.setattr(fetch_market, "_get_status", lambda *a, **k: (status, body))
    out = fetch_market.defillama_bridges(now=NOW)
    assert out["available"] is False and out["top_bridges"] == []
    assert needle in out["reason"]


def test_bridges_success_path(monkeypatch):
    monkeypatch.setattr(fetch_market, "_get_status", lambda *a, **k: (200, BRIDGES_BODY))
    out = fetch_market.defillama_bridges(now=NOW)
    assert out["available"] is True
    assert out["top_bridges"][0]["name"] == "big"
    assert "reason" not in out


def test_unavailable_bridges_contribute_no_date_to_defi_provenance(monkeypatch):
    monkeypatch.setattr(fetch_market, "_get_status", lambda *a, **k: (402, None))
    bridges = fetch_market.defillama_bridges(now=NOW)
    prov = fetch_market.defi_provenance([{"x": 1}], None, None, bridges, {}, observed_at=NOW)
    assert prov["sources"]["bridges"] is None
    assert prov["as_of"] == "2026-10-04"     # from chains only


def test_bridges_probe_hits_the_fetcher_url():
    assert _target("DeFiLlama bridges")["url"] == fetch_market.DEFILLAMA_BRIDGES_URL


# --------------------------------------------------------------------------
# CoinDesk CADLI
# --------------------------------------------------------------------------

CADLI_BODY = {"Data": [
    {"TIMESTAMP": 1790985600, "OPEN": 2, "HIGH": 3, "LOW": 1, "CLOSE": 2.5, "VOLUME": 10},
    {"TIMESTAMP": 1790899200, "OPEN": 1, "HIGH": 2, "LOW": 0.5, "CLOSE": 1.5, "VOLUME": 9},
    {"TIMESTAMP": None, "CLOSE": 4},           # no timestamp -> dropped
    {"TIMESTAMP": 1791072000, "CLOSE": None},  # no close -> dropped, never zero-filled
]}


class _Recorder:
    def __init__(self, result):
        self.result = result
        self.calls: list[dict] = []

    def __call__(self, url, params=None, headers=None, timeout=25):
        self.calls.append({"url": url, "params": dict(params or {}),
                           "headers": dict(headers or {})})
        return self.result


def test_cadli_rows_parse_sort_and_drop_incomplete():
    rows = fetch_market._cadli_rows(CADLI_BODY)
    assert [r["date"] for r in rows] == ["2026-10-02", "2026-10-03"]
    assert rows[1] == {"date": "2026-10-03", "open": 2, "high": 3, "low": 1,
                       "close": 2.5, "volume": 10}
    for junk in (None, [], {"Data": {}}, {"Data": None}):
        assert fetch_market._cadli_rows(junk) == []


def test_cadli_without_key_reports_why(monkeypatch):
    monkeypatch.delenv("CRYPTOCOMPARE_API_KEY", raising=False)
    rec = _Recorder((401, None))
    monkeypatch.setattr(fetch_market, "_get_status", rec)
    out = fetch_market.coindesk_cadli(90, now=NOW)
    assert out["rows"] == []
    st = out["status"]
    assert st["available"] is False and st["http_status"] == 401
    assert st["key_configured"] is False and st["key_env"] == "CRYPTOCOMPARE_API_KEY"
    assert "requires an API key" in st["reason"]
    assert st["checked_at"] == "2026-10-04T19:27:00+00:00"
    assert "Authorization" not in rec.calls[0]["headers"]


def test_cadli_sends_key_as_header_never_query(monkeypatch):
    secret = "sekrit-test-value"
    monkeypatch.setenv("CRYPTOCOMPARE_API_KEY", f"  {secret}\n")
    rec = _Recorder((200, CADLI_BODY))
    monkeypatch.setattr(fetch_market, "_get_status", rec)
    out = fetch_market.coindesk_cadli(90, now=NOW)
    call = rec.calls[0]
    assert call["url"] == fetch_market.COINDESK_CADLI_URL
    assert call["headers"]["Authorization"] == f"Apikey {secret}"
    assert "api_key" not in call["params"]
    assert call["params"] == {"market": "cadli", "instrument": "BTC-USD", "limit": "90"}
    assert [r["close"] for r in out["rows"]] == [1.5, 2.5]
    assert out["status"]["available"] is True and "reason" not in out["status"]
    assert secret not in repr(out)            # the payload is published


def test_cadli_rejected_key_is_named(monkeypatch):
    monkeypatch.setenv("CRYPTOCOMPARE_API_KEY", "bad")
    monkeypatch.setattr(fetch_market, "_get_status", lambda *a, **k: (401, None))
    st = fetch_market.coindesk_cadli(90, now=NOW)["status"]
    assert st["key_configured"] is True
    assert "rejected the configured API key" in st["reason"]


def test_cadli_probe_matches_fetcher_and_is_key_gated():
    t = _target("CoinDesk CADLI")
    assert t["url"].split("?")[0] == fetch_market.COINDESK_CADLI_URL
    assert t["key_env"] == fetch_market.CADLI_KEY_ENV
    # Keyless 401 on a key-gated source = live but gated, not "blocked".
    assert api_status._verdict(401, needs_key=True) == "auth_required"


# --------------------------------------------------------------------------
# Santiment
# --------------------------------------------------------------------------

def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


PREV_COINS = {"btc": {"slug": "bitcoin",
                      "daily_active_addresses": [{"date": "2026-10-01", "value": 1.0}]}}


@pytest.mark.parametrize("prev,expected", [
    (None, None),
    ({}, None),
    # Attempted earlier today (any outcome) -> hold.
    ({"attempted_at": _iso(NOW.replace(hour=2)), "coins": {}}, "daily_gate_attempted_today"),
    ({"attempted_at": _iso(NOW.replace(hour=0, minute=1)), "coins": PREV_COINS},
     "daily_gate_attempted_today"),
    # Last attempt was yesterday -> fetch, whatever the hour (the old gate
    # only fetched at hour 0, which most days never got a run).
    ({"attempted_at": _iso(NOW - timedelta(days=1)), "coins": PREV_COINS}, None),
    # Pre-attempted_at snapshot: fall back to fetched_at, but only for a real one.
    ({"fetched_at": _iso(NOW.replace(hour=0)), "coins": PREV_COINS}, "daily_gate_attempted_today"),
    ({"fetched_at": "2026-10-02T00:39:45+00:00", "coins": PREV_COINS}, None),
    ({"fetched_at": _iso(NOW), "coins": {}, "reason": "daily_gate_hour_19"}, None),
])
def test_santiment_gate(prev, expected):
    assert fetch_market.santiment_gate(prev, NOW) == expected


class _FetchSpy:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def __call__(self, now):
        self.calls += 1
        return self.result


def _wire(monkeypatch, prev, fetched):
    spy = _FetchSpy(fetched)
    monkeypatch.setattr(fetch_market, "_santiment_prev", lambda: prev)
    monkeypatch.setattr(fetch_market, "_santiment_fetch_coins", spy)
    return spy


def test_santiment_fetches_first_run_of_day_at_any_hour(monkeypatch):
    prev = {"available": True, "coins": PREV_COINS, "fetched_at": "2026-10-02T00:39:45+00:00"}
    fresh = {"btc": {"slug": "bitcoin",
                     "daily_active_addresses": [{"date": "2026-10-03", "value": 2.0}]}}
    spy = _wire(monkeypatch, prev, fresh)
    out = fetch_market.santiment_metrics(now=NOW)        # 19:27 UTC, not hour 0
    assert spy.calls == 1
    assert out["available"] is True and out["coins"] == fresh
    assert out["fetched_at"] == out["attempted_at"] == _iso(NOW)
    assert "stale" not in out


def test_santiment_gated_run_serves_cache_without_calling_api(monkeypatch):
    prev = {"available": True, "coins": PREV_COINS, "fetched_at": _iso(NOW.replace(hour=1)),
            "attempted_at": _iso(NOW.replace(hour=1))}
    spy = _wire(monkeypatch, prev, {"should": "not be used"})
    out = fetch_market.santiment_metrics(now=NOW)
    assert spy.calls == 0
    assert out["stale"] is True and out["stale_reason"] == "daily_gate_attempted_today"
    assert out["coins"] == PREV_COINS
    assert out["fetched_at"] == prev["fetched_at"]   # date of the data, not of this run


def test_santiment_failed_attempt_keeps_old_coins_and_spends_the_day(monkeypatch):
    prev = {"available": True, "coins": PREV_COINS, "fetched_at": "2026-10-02T00:39:45+00:00"}
    _wire(monkeypatch, prev, {})
    out = fetch_market.santiment_metrics(now=NOW)
    assert out["stale"] is True and out["stale_reason"] == "fetch_failed"
    assert out["coins"] == PREV_COINS
    assert out["fetched_at"] == "2026-10-02T00:39:45+00:00"
    assert out["attempted_at"] == _iso(NOW)
    # The next run the same day must not retry (quota: 1,000 calls/month keyless).
    spy = _wire(monkeypatch, out, {"x": {}})
    again = fetch_market.santiment_metrics(now=NOW + timedelta(hours=3))
    assert spy.calls == 0 and again["coins"] == PREV_COINS


def test_santiment_failed_attempt_without_cache_is_explicitly_empty(monkeypatch):
    _wire(monkeypatch, None, {})
    out = fetch_market.santiment_metrics(now=NOW)
    assert out == {"available": False, "reason": "fetch_failed", "coins": {},
                   "fetched_at": _iso(NOW), "attempted_at": _iso(NOW)}
    spy = _wire(monkeypatch, out, {"x": {}})
    held = fetch_market.santiment_metrics(now=NOW + timedelta(hours=1))
    assert spy.calls == 0
    assert held["available"] is False and held["attempted_at"] == _iso(NOW)


def test_santiment_probe_sends_a_query_document():
    """A bare GET of /graphql is a 400 ("No query document supplied")."""
    url = _target("Santiment")["url"]
    parts = urllib.parse.urlsplit(url)
    assert parts.netloc == "api.santiment.net" and parts.path == "/graphql"
    q = urllib.parse.parse_qs(parts.query).get("query", [""])[0]
    assert "getMetric" in q and "timeseriesData" in q
    # Relative dates: the probe must not age out of the free-tier window.
    assert "utc_now" in q
    assert _target("Santiment")["key_env"] is None


# --------------------------------------------------------------------------
# The dashboards render the reasons (V1 at /, V2 at /v2/)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("rel", ["app.py", "v2/app.py"])
def test_dashboards_disclose_unavailable_sources(rel):
    src = (ROOT / rel).read_text()
    # CADLI empty state reads the fetcher's reason.
    cadli_fn = src.split("function renderCadliChart(){", 1)[1].split("\n}\n", 1)[0]
    assert "cadli_btc_status" in cadli_fn and "cst.reason" in cadli_fn
    # Bridges card stays visible with the reason instead of hiding.
    assert "bridgesMeta.available === false && bridgesMeta.reason" in src
    assert "escapeHtml(bridgesMeta.reason)" in src
    # ...and reads the field names the fetcher actually emits.
    assert "b.daily_volume_usd" in src and "b.weekly_volume_usd" in src
    # Santiment cards print the data date.
    assert "data through" in src
