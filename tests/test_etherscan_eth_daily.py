"""Tests for the Etherscan ETH 90-day daily on-chain series.

Covers:
  - etherscan_eth_daily() returns {"available": False, ...} when
    ETHERSCAN_API_KEY is missing, and never touches the network.
  - The mocked happy path produces a {date, value} series with the
    expected shape and block-delta math.
  - The whale.eth.etherscan_daily key is wired into fetch_whale() and
    populates with the expected contract.
"""
from __future__ import annotations

import pytest

import fetch_market


# ---------- 1. No-key path ----------

def test_etherscan_eth_daily_no_key_returns_unavailable(monkeypatch):
    """Without ETHERSCAN_API_KEY in env, the fetcher returns
    available=False, never raises, and never calls requests.get."""
    monkeypatch.delenv("ETHERSCAN_API_KEY", raising=False)

    def _explode(*args, **kwargs):
        raise AssertionError(
            "etherscan_eth_daily() must not hit the network when ETHERSCAN_API_KEY is unset"
        )

    monkeypatch.setattr(fetch_market.requests, "get", _explode)

    out = fetch_market.etherscan_eth_daily()
    assert isinstance(out, dict)
    assert out.get("available") is False
    assert out.get("reason") == "no ETHERSCAN_API_KEY in env"
    # Should never carry a populated series in the no-key branch.
    assert "series" not in out or out["series"] == []


# ---------- 2. Mocked happy path ----------

class _FakeResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def test_etherscan_eth_daily_happy_path(monkeypatch):
    """With the key set and the API returning sane block numbers,
    the fetcher should produce a series of (date, blocks_per_day) deltas.

    We seed a counter so each successive call returns a block number
    that's exactly 7,000 higher than the previous — that's the daily
    delta we expect to see in every row of the returned series.
    """
    monkeypatch.setenv("ETHERSCAN_API_KEY", "test-key-deadbeef")

    counter = {"n": 17_000_000}
    calls = {"count": 0}

    def _fake_get(url, params=None, headers=None, timeout=None):
        calls["count"] += 1
        # Sanity-check we're hitting the Etherscan v2 endpoint with the
        # right module/action and that the apikey is being forwarded.
        assert "etherscan.io" in url
        assert params is not None
        assert params.get("module") == "block"
        assert params.get("action") == "getblocknobytime"
        assert params.get("apikey") == "test-key-deadbeef"
        assert params.get("closest") == "before"
        # status=1 + numeric string is the Etherscan success contract.
        block = counter["n"]
        counter["n"] += 7_000
        return _FakeResp({"status": "1", "message": "OK", "result": str(block)})

    monkeypatch.setattr(fetch_market.requests, "get", _fake_get)

    # 5 days → 6 checkpoints → 5 daily deltas.
    out = fetch_market.etherscan_eth_daily(days=5)
    assert out.get("available") is True
    assert out.get("metric") == "blocks_per_day"
    assert "fetched_at" in out

    series = out.get("series")
    assert isinstance(series, list)
    assert len(series) == 5
    for row in series:
        assert set(row.keys()) == {"date", "value"}
        # YYYY-MM-DD shape
        assert isinstance(row["date"], str) and len(row["date"]) == 10
        # 7000 block delta per checkpoint by construction
        assert row["value"] == 7_000

    # 6 checkpoint calls expected (days + 1).
    assert calls["count"] == 6


# ---------- 3. API failure → empty series, available=False ----------

def test_etherscan_eth_daily_handles_rate_limit(monkeypatch):
    """When the API returns status=0 (rate-limit / error) for every call,
    the fetcher returns available=False with an empty series rather
    than raising — and bails out early after a handful of failures
    rather than burning the whole budget."""
    monkeypatch.setenv("ETHERSCAN_API_KEY", "test-key-deadbeef")
    # Wipe any pre-existing stale cache so this test exercises the
    # no-cache fallback path deterministically.
    stale_path = fetch_market._stale_path("etherscan_eth_daily")
    if stale_path.exists():
        stale_path.unlink()

    def _fake_get(url, params=None, headers=None, timeout=None):
        return _FakeResp({"status": "0", "message": "NOTOK", "result": "Max rate limit reached"})

    monkeypatch.setattr(fetch_market.requests, "get", _fake_get)

    out = fetch_market.etherscan_eth_daily(days=10)
    assert isinstance(out, dict)
    assert out.get("available") is False
    assert out.get("series") == []


# ---------- 4. Wired into fetch_whale → whale.eth.etherscan_daily ----------

def test_fetch_whale_exposes_etherscan_daily_key(monkeypatch):
    """fetch_whale() must surface the Etherscan series under
    whale.eth.etherscan_daily so the renderer can find it."""
    # Force every fetcher into its no-key/empty path so the test is
    # offline and fast — we only care about the schema wiring.
    monkeypatch.delenv("ETHERSCAN_API_KEY", raising=False)
    monkeypatch.delenv("GLASSNODE_API_KEY", raising=False)
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.delenv("COINMETRICS_API_KEY", raising=False)

    # Stub the heavy sub-fetchers so we don't actually hit the network.
    monkeypatch.setattr(fetch_market, "whale_proxies_btc", lambda: {})
    monkeypatch.setattr(fetch_market, "bitinfocharts_btc_distribution", lambda: {})
    monkeypatch.setattr(fetch_market, "glassnode_btc_whale_metrics", lambda: {})
    monkeypatch.setattr(fetch_market, "mempool_whale_transactions", lambda *_: [])
    monkeypatch.setattr(fetch_market, "blockchair_eth_stats", lambda: {})
    monkeypatch.setattr(fetch_market, "blockchair_eth_large_transactions", lambda *_: {})
    monkeypatch.setattr(fetch_market, "coin_metrics_eth_whale_metrics", lambda: {})
    monkeypatch.setattr(fetch_market, "fetch_multichain_whale_stats", lambda: {})

    out = fetch_market.fetch_whale()
    assert isinstance(out, dict)
    eth = out.get("eth") or {}
    assert "etherscan_daily" in eth, "whale.eth.etherscan_daily key missing — renderer won't find it"
    ed = eth["etherscan_daily"]
    assert isinstance(ed, dict)
    # No-key branch contract, verbatim — must match what the
    # renderer's noKeyReason check expects.
    assert ed.get("available") is False
    assert ed.get("reason") == "no ETHERSCAN_API_KEY in env"
