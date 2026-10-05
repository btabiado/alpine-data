"""Three upstreams that stopped answering keyless requests the way we asked.

Live state on 2026-10-04 (data/health/api_status.json):

* DeFiLlama bridges -> HTTP 402. Bridges are Pro-only now; there is no
  keyless equivalent, so the card must SAY it is unavailable (it used to hide
  itself, leaving no trace that a panel had gone).
* CoinDesk CADLI -> HTTP 401 "API key required". The whole CoinDesk Data API
  is keyed now and the owner will not buy a plan, so the CADLI chart was
  RETIRED (2026-10) and its slot shows Alpine Data's own Large-Cap Crypto
  Index instead (tests/test_crypto_free_sources.py). What is checked here is
  that nothing still calls CoinDesk and the replacement states its reason
  when empty.
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
# CoinDesk CADLI (retired) -> Alpine Large-Cap Crypto Index
# --------------------------------------------------------------------------

def test_cadli_is_retired_everywhere():
    """No fetcher, no probe and no dashboard call to the CoinDesk Data API
    remain; the keyed probe that reported auth_required is gone with it."""
    assert not hasattr(fetch_market, "coindesk_cadli")
    assert not hasattr(fetch_market, "COINDESK_CADLI_URL")
    retired = ("coindesk.com", "cryptocompare.com")
    hosts = [(urllib.parse.urlsplit(t["url"]).hostname or "") for t in api_status.TARGETS]
    assert not [h for h in hosts
                if any(h == d or h.endswith("." + d) for d in retired)]
    for rel in ("fetch_market.py", "app.py", "v2/app.py", "server.py", "api_status.py"):
        src = (ROOT / rel).read_text()
        for host in ("data-api.coindesk.com", "min-api.cryptocompare.com",
                     "data-api.cryptocompare.com"):
            assert host not in src, f"{rel} still references {host}"


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
    # The Alpine index (CADLI's replacement) states the fetcher's reason when
    # it is not computed, instead of a bare empty chart.
    idx_fn = src.split("function renderAlpineIndexChart(){", 1)[1].split("\n}\n", 1)[0]
    assert "idx.available === false && idx.reason" in idx_fn
    assert "renderCadliChart" not in src and "cadli_btc" not in src
    # Bridges card stays visible with the reason instead of hiding.
    assert "bridgesMeta.available === false && bridgesMeta.reason" in src
    assert "escapeHtml(bridgesMeta.reason)" in src
    # ...and reads the field names the fetcher actually emits.
    assert "b.daily_volume_usd" in src and "b.weekly_volume_usd" in src
    # Santiment cards print the data date.
    assert "data through" in src
