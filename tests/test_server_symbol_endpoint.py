"""Contract tests for the universal symbol lookup endpoint.

``/api/symbol/<symbol>`` is the server-side counterpart to the dashboard's
universal symbol-search box. Because Yahoo Finance doesn't expose reliable
browser CORS, the dashboard has to round-trip stock lookups through this
route. Crypto symbols also route through it so any ticker — not just the
top-25 by mcap captured in ``market.json`` — can be looked up live.

These tests verify the route's contract without hitting the network:
    * validation rejects garbage inputs with 400
    * crypto branch returns ``kind='crypto'`` with score + label + poc
    * stock branch (fallback when crypto comes back empty) returns ``kind='stock'``
    * both branches empty → 404

The Flask test client wiring mirrors ``tests/test_server.py`` — the
``client`` fixture seeds a tmp data dir and pre-sets the
``X-Requested-With`` header so CSRF middleware lets requests through.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import app
import fetch_market
import server


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    """Flask test client with DATA_DIR redirected to a tmp_path.

    Identical setup to ``tests/test_server.py`` so the auth/CSRF middleware
    behaves the same. The symbol-lookup route doesn't actually read from
    DATA_DIR, but other before-request handlers in server.py may.
    """
    monkeypatch.setattr(app, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server.dash, "DATA_DIR", tmp_path)

    (tmp_path / "btc_flows.csv").write_text("date,Total\n2024-01-11,100.0\n")
    (tmp_path / "eth_flows.csv").write_text("date,Total\n2024-07-23,5.0\n")
    (tmp_path / "market.json").write_text(json.dumps({"btc": {"price": []}, "eth": {"price": []}}))
    (tmp_path / "whale.json").write_text(json.dumps({"btc": {}}))

    # The route memoises results via lru_cache keyed on (symbol, hour_bucket).
    # Clear the cache before each test so mocks aren't shadowed by a prior
    # test's cached fetch.
    try:
        server._symbol_lookup_cached.cache_clear()
    except AttributeError:
        pass

    server.flask_app.config["TESTING"] = True
    with server.flask_app.test_client() as c:
        c.environ_base["HTTP_X_REQUESTED_WITH"] = "XMLHttpRequest"
        yield c

    # Tear-down: clear again so cached mock data doesn't leak to other suites.
    try:
        server._symbol_lookup_cached.cache_clear()
    except AttributeError:
        pass


def _canned_crypto_series(days: int = 180) -> dict:
    """Return a synthetic CryptoCompare-style {price, volume} pair.

    Values trend up slowly so compute_stock_signal produces a non-degenerate
    score (above SMAs, positive momentum). Enough rows to satisfy the
    SMA200 / 30d rolling history branches.
    """
    base_date = 20000  # arbitrary; only ordering matters for the scorer
    prices = []
    volumes = []
    for i in range(days):
        # Use ISO-like dates so anything date-comparing downstream is happy.
        date = f"2024-{(i // 30) % 12 + 1:02d}-{(i % 30) + 1:02d}"
        prices.append({"date": date, "value": 100.0 + i * 0.5})
        volumes.append({"date": date, "value": 1_000_000.0 + i * 1000})
    return {"price": prices, "volume": volumes}


def _canned_stock_history(days: int = 180) -> list[dict]:
    """Yahoo-style daily OHLCV list — same shape `yahoo_chart_history` returns."""
    out = []
    for i in range(days):
        date = f"2024-{(i // 30) % 12 + 1:02d}-{(i % 30) + 1:02d}"
        out.append({
            "date":   date,
            "close":  100.0 + i * 0.5,
            "volume": 1_000_000 + i * 1000,
        })
    return out


# ---------- tests ----------


def test_symbol_endpoint_validates_input(client):
    """Garbage paths must be rejected with 400 before we hit any upstream
    fetcher. The route's regex allows ``[A-Za-z]{1,10}`` only — anything
    with punctuation (``?``), digits, or empty is invalid.

    Note: Flask routes a bare empty segment as a different URL entirely
    (``/api/symbol/`` 404s at the routing layer), so this test exercises the
    closest user-facing inputs the JS client could plausibly send.
    """
    # A '?' would normally be a query-string delimiter; quote it inline to
    # force it into the path segment.
    r = client.get("/api/symbol/%3F")
    assert r.status_code == 400
    body = r.get_json()
    assert "error" in body

    # Numeric-only also fails the [A-Za-z] regex
    r = client.get("/api/symbol/12345")
    assert r.status_code == 400

    # Too long (> 10 letters)
    r = client.get("/api/symbol/ABCDEFGHIJKL")
    assert r.status_code == 400


def test_symbol_endpoint_returns_crypto_data(client, monkeypatch):
    """Happy path: BTC resolves through the crypto branch. Mock
    ``cryptocompare_market`` so the test never touches the network."""
    monkeypatch.setattr(
        fetch_market, "cryptocompare_market",
        lambda symbol, days=180: _canned_crypto_series(),
    )

    r = client.get("/api/symbol/BTC")
    assert r.status_code == 200, f"unexpected status: {r.status_code} body={r.get_data(as_text=True)}"
    body = r.get_json()
    assert body is not None
    assert body.get("kind") == "crypto"
    assert body.get("symbol") == "BTC"
    # The contract: score, label, poc all present
    assert "score" in body
    assert isinstance(body.get("score"), (int, float))
    assert "label" in body
    assert isinstance(body.get("label"), str) and body["label"]
    assert "poc" in body and isinstance(body["poc"], dict)
    # POC sub-keys the dashboard renderer reads
    for k in ("d30", "d90", "d180", "naked", "migration_series"):
        assert k in body["poc"], f"poc.{k} missing from response"


def test_symbol_endpoint_returns_stock_data(client, monkeypatch):
    """Happy path for a stock ticker: NVDA falls through to the Yahoo
    branch. Mock both fetchers — crypto returns empty (forcing fallthrough),
    yahoo returns canned history."""
    monkeypatch.setattr(
        fetch_market, "cryptocompare_market",
        lambda symbol, days=180: {"price": [], "volume": []},
    )
    monkeypatch.setattr(
        fetch_market, "yahoo_chart_history",
        lambda symbol, range_="6mo": _canned_stock_history(),
    )

    r = client.get("/api/symbol/NVDA")
    assert r.status_code == 200, f"unexpected status: {r.status_code} body={r.get_data(as_text=True)}"
    body = r.get_json()
    assert body is not None
    assert body.get("kind") == "stock"
    assert body.get("symbol") == "NVDA"
    assert "score" in body and isinstance(body.get("score"), (int, float))
    assert "label" in body and isinstance(body.get("label"), str)
    assert "poc" in body and isinstance(body["poc"], dict)


def test_symbol_endpoint_404_when_both_sources_empty(client, monkeypatch):
    """If both the crypto and stock fetchers come back empty, the endpoint
    must return 404 rather than a stub 200 with null fields — the JS client
    branches on status to display "symbol not found"."""
    monkeypatch.setattr(
        fetch_market, "cryptocompare_market",
        lambda symbol, days=180: {"price": [], "volume": []},
    )
    monkeypatch.setattr(
        fetch_market, "yahoo_chart_history",
        lambda symbol, range_="6mo": [],
    )

    r = client.get("/api/symbol/ZZZZ")
    assert r.status_code == 404
    body = r.get_json()
    assert isinstance(body, dict)
    assert "error" in body
