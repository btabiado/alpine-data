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
    # Redirect the cached-payload lookup so tests don't pick up the
    # developer's real data/market.json (which would short-circuit the live
    # fetch paths these tests are verifying).
    monkeypatch.setattr(fetch_market, "CACHE", tmp_path)

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
    _ = 20000  # arbitrary; only ordering matters for the scorer
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


# ---------- cached-payload coverage tests --------------------------------
#
# The polish/symbol-coverage branch added a cached-payload pre-pass so any
# symbol the dashboard already has scored data for is returned without a
# round-trip to CryptoCompare/Yahoo. These tests verify the cached path
# returns 200 + a non-empty body for representative tickers from each
# cached source (stocks_signals, poc_top, markets_top), without making any
# real network calls.


def _write_cached_market(tmp_path: Path, payload: dict) -> None:
    """Write a synthetic market.json to the fixture's CACHE dir so the
    cached-payload lookup branch has something to find."""
    (tmp_path / "market.json").write_text(json.dumps(payload))


def test_symbol_endpoint_cached_stocks_signal(client, tmp_path, monkeypatch):
    """A symbol present in cached ``stocks_signals`` resolves immediately
    without hitting Yahoo — verified by mocking Yahoo to raise (any network
    call would fail the test)."""
    _write_cached_market(tmp_path, {
        "stocks_signals": [{
            "symbol": "NVDA", "name": "NVIDIA Corp",
            "last_price": 100.0, "change_pct": 1.5, "volume": 100_000,
            "score": 42, "label": "BUY", "components": [{"name": "x", "value": "1", "contribution": 5, "explanation": "ok"}],
            "history": [{"date": "2024-01-01", "score": 42, "price": 100.0}],
        }],
    })
    monkeypatch.setattr(fetch_market, "yahoo_chart_history",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call Yahoo")))
    monkeypatch.setattr(fetch_market, "cryptocompare_market",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call CC")))

    r = client.get("/api/symbol/NVDA")
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["kind"] == "stock"
    assert body["symbol"] == "NVDA"
    assert body["score"] == 42
    assert body["label"] == "BUY"
    assert body.get("source") == "cache:stocks_signals"


def test_symbol_endpoint_cached_poc_top(client, tmp_path, monkeypatch):
    """A symbol present in cached ``poc_top`` resolves with the full POC
    bundle attached — no CryptoCompare call needed."""
    _write_cached_market(tmp_path, {
        "poc_top": [{
            "coin_id": "solana", "symbol": "SOL", "name": "Solana",
            "current_price": 150.0,
            "poc": {
                "d30":  {"poc": 145.0},
                "d90":  {"poc": 140.0},
                "d180": {"poc": 135.0},
                "naked": [],
                "migration_series": [{"date": "2024-01-01", "migration": "UP"}],
            },
            "signal_history": [{"date": "2024-01-01", "score": 10, "price": 150.0}],
        }],
    })
    monkeypatch.setattr(fetch_market, "cryptocompare_market",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call CC")))

    r = client.get("/api/symbol/SOL")
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["kind"] == "crypto"
    assert body["symbol"] == "SOL"
    assert body["name"] == "Solana"
    assert body["poc"]["d30"] == {"poc": 145.0}
    assert body["poc"]["d180"] == {"poc": 135.0}
    assert body.get("source") == "cache:poc_top"


def test_symbol_endpoint_cached_markets_top_stables_and_obscure(client, tmp_path, monkeypatch):
    """Several symbols (USDT, USDC, FIGR_HELOC, AVAX, XRP) sit in the
    top-25 ``markets_top`` slice but get filtered out of signals_top20
    (stables) or poc_top (lower-rank). They must still resolve via the
    markets_top fallback so the modal isn't blank."""
    _write_cached_market(tmp_path, {
        "markets_top": [
            {"rank": 1, "id": "bitcoin",  "symbol": "BTC",        "name": "Bitcoin",  "price_usd": 65000.0, "market_cap_usd": 1.3e12, "volume_24h_usd": 4e10, "change_24h_pct": 1.2,  "change_7d_pct": 5.0, "sparkline_7d": [64000, 64500, 65000]},
            {"rank": 3, "id": "tether",   "symbol": "USDT",       "name": "Tether",   "price_usd": 1.0,     "market_cap_usd": 1.3e11, "volume_24h_usd": 5e10, "change_24h_pct": 0.0,  "change_7d_pct": 0.01,"sparkline_7d": [1.0, 1.0, 1.0]},
            {"rank": 5, "id": "ripple",   "symbol": "XRP",        "name": "XRP",      "price_usd": 0.5,     "market_cap_usd": 3e10,   "volume_24h_usd": 2e9,  "change_24h_pct": -1.0, "change_7d_pct": 3.0, "sparkline_7d": [0.49, 0.50, 0.50]},
            {"rank": 6, "id": "usd-coin", "symbol": "USDC",       "name": "USD Coin", "price_usd": 1.0,     "market_cap_usd": 3e10,   "volume_24h_usd": 5e9,  "change_24h_pct": 0.0,  "change_7d_pct": 0.0, "sparkline_7d": [1.0, 1.0, 1.0]},
            {"rank": 9, "id": "figr-heloc","symbol": "FIGR_HELOC","name": "Figure HELOC","price_usd": 1.05, "market_cap_usd": 2e9,   "volume_24h_usd": 1e6,  "change_24h_pct": 0.1,  "change_7d_pct": 0.5, "sparkline_7d": [1.04, 1.05, 1.05]},
            {"rank": 12,"id": "avalanche","symbol": "AVAX",       "name": "Avalanche","price_usd": 35.0,   "market_cap_usd": 1.4e10, "volume_24h_usd": 5e8,  "change_24h_pct": 2.0,  "change_7d_pct": 7.0, "sparkline_7d": [33, 34, 35]},
        ],
    })
    monkeypatch.setattr(fetch_market, "cryptocompare_market",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call CC")))
    monkeypatch.setattr(fetch_market, "yahoo_chart_history",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call Yahoo")))

    for sym, expected_name in [
        ("BTC", "Bitcoin"),
        ("USDT", "Tether"),
        ("XRP", "XRP"),
        ("USDC", "USD Coin"),
        ("FIGR_HELOC", "Figure HELOC"),
        ("AVAX", "Avalanche"),
    ]:
        # FIGR_HELOC contains an underscore — the endpoint regex
        # ``[A-Za-z]{1,10}`` rejects it, so callers can only reach this
        # path via the JS resolver. We still test the underlying
        # _try_cached_payload_lookup helper directly for that symbol.
        if "_" in sym:
            status, body = server._try_cached_payload_lookup(sym)
            assert status == 200, f"cached lookup miss for {sym}: {body}"
            assert body["name"] == expected_name
            continue

        r = client.get(f"/api/symbol/{sym}")
        assert r.status_code == 200, f"{sym}: {r.status_code} {r.get_data(as_text=True)}"
        body = r.get_json()
        assert body["symbol"] == sym
        assert body["name"] == expected_name
        # Markets-top entries fall through with score=None (no full signal)
        # but must still carry the rank/mcap/sparkline payload the UI uses.
        assert body.get("source") == "cache:markets_top"
        assert body.get("market_top", {}).get("market_cap_usd") is not None


def test_symbol_endpoint_cashtag_prefix(client, tmp_path, monkeypatch):
    """A leading ``$`` (cashtag, as pasted from social) must resolve the
    same way as the bare ticker. The route strips the prefix before
    validating against the [A-Za-z]{1,10} regex."""
    _write_cached_market(tmp_path, {
        "markets_top": [{
            "rank": 1, "id": "bitcoin", "symbol": "BTC", "name": "Bitcoin",
            "price_usd": 65000.0, "market_cap_usd": 1.3e12, "volume_24h_usd": 4e10,
            "change_24h_pct": 1.0, "change_7d_pct": 2.0, "sparkline_7d": [64500, 65000],
        }],
    })
    monkeypatch.setattr(fetch_market, "cryptocompare_market",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call CC")))

    # Flask test client URL-encodes the ``$`` automatically.
    r = client.get("/api/symbol/$BTC")
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["symbol"] == "BTC"


def test_symbol_endpoint_404_message_mentions_coverage_scope(client, tmp_path, monkeypatch):
    """The 404 body must mention the coverage scope (top-25 crypto /
    top-50 stocks) so the user knows why the symbol failed rather than
    seeing a generic "not found"."""
    # Empty cache, both live fetchers return empty.
    _write_cached_market(tmp_path, {})
    monkeypatch.setattr(fetch_market, "cryptocompare_market",
                        lambda symbol, days=180: {"price": [], "volume": []})
    monkeypatch.setattr(fetch_market, "yahoo_chart_history",
                        lambda symbol, range_="6mo": [])

    r = client.get("/api/symbol/ZZZZZ")
    assert r.status_code == 404
    body = r.get_json()
    assert "top-25" in body["error"] or "top-50" in body["error"], (
        f"404 body does not mention coverage scope: {body!r}"
    )
