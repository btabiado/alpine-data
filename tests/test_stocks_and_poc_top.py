"""Unit tests for the recently-added stock signal and POC-top fetchers.

Targets (all in ``fetch_market``):
  - yahoo_most_active
  - yahoo_chart_history
  - compute_stock_signal
  - fetch_stocks_signals
  - cryptocompare_market
  - compute_poc_top_markets

HTTP is mocked via ``unittest.mock.patch`` on ``fetch_market._get`` and
``fetch_market.requests.get`` (for functions that bypass ``_get`` such as
``cryptocompare_market``). No live network calls.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import fetch_market


# ============================================================================
# yahoo_most_active
# ============================================================================

def _yahoo_screener_response(quotes: list[dict]) -> dict:
    return {"finance": {"result": [{"quotes": quotes}]}}


def test_yahoo_most_active_happy_path():
    quotes = [
        {
            "symbol": "AAPL",
            "shortName": "Apple Inc.",
            "regularMarketPrice": 195.5,
            "regularMarketChangePercent": 1.23,
            "regularMarketVolume": 50_000_000,
        },
        {
            "symbol": "NVDA",
            "shortName": "NVIDIA",
            "regularMarketPrice": 900.1,
            "regularMarketChangePercent": -0.45,
            "regularMarketVolume": 40_000_000,
        },
    ]
    with patch.object(fetch_market, "_get",
                      return_value=_yahoo_screener_response(quotes)):
        out = fetch_market.yahoo_most_active(limit=5)
    assert len(out) == 2
    assert out[0]["symbol"] == "AAPL"
    assert out[0]["name"] == "Apple Inc."
    assert out[0]["last_price"] == 195.5
    assert out[0]["change_pct"] == 1.23
    assert out[0]["volume"] == 50_000_000
    # Schema keys
    for entry in out:
        assert set(entry.keys()) == {
            "symbol", "name", "last_price", "change_pct", "volume",
        }


def test_yahoo_most_active_empty_result_array():
    """finance.result is an empty list -> [] without crashing."""
    with patch.object(fetch_market, "_get",
                      return_value={"finance": {"result": []}}):
        out = fetch_market.yahoo_most_active(limit=20)
    assert out == []


def test_yahoo_most_active_get_returns_none():
    with patch.object(fetch_market, "_get", return_value=None):
        out = fetch_market.yahoo_most_active(limit=20)
    assert out == []


def test_yahoo_most_active_skips_quotes_missing_symbol():
    quotes = [
        {"shortName": "No Symbol Co.", "regularMarketPrice": 10.0},
        {"symbol": "OK", "shortName": "Okay Co.",
         "regularMarketPrice": 5.0, "regularMarketChangePercent": 0.1,
         "regularMarketVolume": 1000},
    ]
    with patch.object(fetch_market, "_get",
                      return_value=_yahoo_screener_response(quotes)):
        out = fetch_market.yahoo_most_active(limit=20)
    assert len(out) == 1
    assert out[0]["symbol"] == "OK"


# ============================================================================
# yahoo_chart_history
# ============================================================================

def _yahoo_chart_response(timestamps: list, closes: list, volumes: list) -> dict:
    return {
        "chart": {
            "result": [
                {
                    "timestamp": timestamps,
                    "indicators": {
                        "quote": [
                            {"close": closes, "volume": volumes},
                        ],
                    },
                },
            ],
        },
    }


def test_yahoo_chart_history_happy_path():
    # Three days of data.
    base = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
    timestamps = [base, base + 86400, base + 86400 * 2]
    closes = [100.0, 101.0, 102.5]
    volumes = [1_000_000, 1_200_000, 900_000]
    with patch.object(fetch_market, "_get",
                      return_value=_yahoo_chart_response(timestamps, closes, volumes)):
        out = fetch_market.yahoo_chart_history("AAPL")
    assert len(out) == 3
    assert out[0]["date"] == "2025-01-01"
    assert out[0]["close"] == 100.0
    assert out[0]["volume"] == 1_000_000
    assert out[2]["close"] == 102.5


def test_yahoo_chart_history_skips_none_closes():
    base = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
    timestamps = [base, base + 86400, base + 86400 * 2]
    closes = [100.0, None, 102.0]
    volumes = [1_000_000, 0, 900_000]
    with patch.object(fetch_market, "_get",
                      return_value=_yahoo_chart_response(timestamps, closes, volumes)):
        out = fetch_market.yahoo_chart_history("AAPL")
    # The None close should be dropped; we keep the two valid rows.
    assert len(out) == 2
    assert out[0]["close"] == 100.0
    assert out[1]["close"] == 102.0


def test_yahoo_chart_history_handles_none_volume():
    """Volume None should fall back to 0 without crashing."""
    base = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
    timestamps = [base]
    closes = [50.0]
    volumes = [None]
    with patch.object(fetch_market, "_get",
                      return_value=_yahoo_chart_response(timestamps, closes, volumes)):
        out = fetch_market.yahoo_chart_history("AAPL")
    assert len(out) == 1
    assert out[0]["volume"] == 0


def test_yahoo_chart_history_bad_shape_returns_empty():
    # No 'chart' key
    with patch.object(fetch_market, "_get", return_value={"weird": "shape"}):
        assert fetch_market.yahoo_chart_history("AAPL") == []
    # chart.result is empty list (IndexError path)
    with patch.object(fetch_market, "_get",
                      return_value={"chart": {"result": []}}):
        assert fetch_market.yahoo_chart_history("AAPL") == []


def test_yahoo_chart_history_get_returns_none():
    with patch.object(fetch_market, "_get", return_value=None):
        assert fetch_market.yahoo_chart_history("AAPL") == []


# ============================================================================
# compute_stock_signal
# ============================================================================

def _make_history(closes: list[float], start_date: str = "2024-01-01",
                  volume: int = 1_000_000) -> list[dict]:
    start = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    out = []
    for i, c in enumerate(closes):
        out.append({
            "date":   (start + timedelta(days=i)).strftime("%Y-%m-%d"),
            "close":  float(c),
            "volume": int(volume),
        })
    return out


EXPECTED_COMPONENT_NAMES = {
    "Above 50d SMA",
    "RSI(14)",
    "MACD signal",
    "5d momentum",
    "Volume z-score",
    "50/200 cross",
}


def test_compute_stock_signal_climbing_prices_bullish():
    # 220 days of monotonically climbing prices -> bullish signals everywhere.
    closes = [100.0 + i * 0.5 for i in range(220)]
    hist = _make_history(closes)
    sig = fetch_market.compute_stock_signal(hist)
    assert sig["score"] > 0
    assert sig["label"] in {"BUY", "STRONG BUY"}
    names = {c["name"] for c in sig["components"]}
    assert names == EXPECTED_COMPONENT_NAMES
    # last_close above SMA-50 -> Above 50d SMA component should score +10
    above_50 = next(c for c in sig["components"] if c["name"] == "Above 50d SMA")
    assert above_50["score"] == 10
    # 50/200 cross "above"
    cross = next(c for c in sig["components"] if c["name"] == "50/200 cross")
    assert cross["value"] == "above"
    assert cross["score"] == 10
    # Rolling history has up to 90 entries
    assert 1 <= len(sig["history"]) <= 90
    for h in sig["history"]:
        assert set(h.keys()) == {"date", "score"}


def test_compute_stock_signal_declining_prices_bearish():
    # 220 days of monotonically declining prices -> bearish.
    closes = [200.0 - i * 0.5 for i in range(220)]
    hist = _make_history(closes)
    sig = fetch_market.compute_stock_signal(hist)
    assert sig["score"] < 0
    assert sig["label"] in {"SELL", "STRONG SELL"}
    # 50/200 cross "below"
    cross = next(c for c in sig["components"] if c["name"] == "50/200 cross")
    assert cross["value"] == "below"
    assert cross["score"] == -10


def test_compute_stock_signal_partial_history_under_50_days():
    """< 50 days of data uses partial-mode component scores; must not crash."""
    closes = [100.0 + i for i in range(20)]  # 20 days, climbing
    hist = _make_history(closes)
    sig = fetch_market.compute_stock_signal(hist)
    names = {c["name"] for c in sig["components"]}
    assert names == EXPECTED_COMPONENT_NAMES
    # 50/200 cross with n < 50 -> "n/a" (score 0)
    cross = next(c for c in sig["components"] if c["name"] == "50/200 cross")
    assert cross["value"] == "n/a"
    assert cross["score"] == 0
    # MACD requires n >= 35 -> with n=20, score should be 0/value None
    macd = next(c for c in sig["components"] if c["name"] == "MACD signal")
    assert macd["score"] == 0
    # Volume z-score requires >=31 days -> n=20 -> 0
    vz = next(c for c in sig["components"] if c["name"] == "Volume z-score")
    assert vz["score"] == 0
    # Score should be a bounded int
    assert isinstance(sig["score"], int)
    assert -100 <= sig["score"] <= 100


def test_compute_stock_signal_empty_input():
    sig = fetch_market.compute_stock_signal([])
    assert sig == {"score": 0, "label": "HOLD", "components": [], "history": []}


def test_compute_stock_signal_components_have_expected_keys():
    closes = [100.0 + i * 0.1 for i in range(60)]
    hist = _make_history(closes)
    sig = fetch_market.compute_stock_signal(hist)
    assert len(sig["components"]) == 6
    for comp in sig["components"]:
        assert set(comp.keys()) == {"name", "value", "score"}
        assert isinstance(comp["name"], str)
        assert isinstance(comp["score"], int)


# ============================================================================
# fetch_stocks_signals
# ============================================================================

def test_fetch_stocks_signals_happy_path():
    movers = [
        {"symbol": "AAPL", "name": "Apple Inc.", "last_price": 100.0,
         "change_pct": 1.0, "volume": 1_000_000},
        {"symbol": "NVDA", "name": "NVIDIA",     "last_price": 200.0,
         "change_pct": 2.0, "volume": 2_000_000},
    ]
    hist = _make_history([100.0 + i * 0.5 for i in range(220)])
    with patch.object(fetch_market, "yahoo_most_active", return_value=movers), \
         patch.object(fetch_market, "yahoo_chart_history", return_value=hist), \
         patch.object(fetch_market.time, "sleep"):
        out = fetch_market.fetch_stocks_signals(limit=2)
    assert len(out) == 2
    expected_keys = {
        "symbol", "name", "last_price", "change_pct", "volume",
        "score", "label", "components", "history", "poc",
    }
    for entry in out:
        assert set(entry.keys()) == expected_keys
    assert out[0]["symbol"] == "AAPL"
    assert out[1]["symbol"] == "NVDA"
    assert isinstance(out[0]["score"], int)
    assert out[0]["label"] in {"STRONG BUY", "BUY", "HOLD", "SELL", "STRONG SELL"}
    # POC computed from the same 220-day OHLCV history. Shape matches
    # compute_poc_top_markets so the frontend renders the same card.
    assert out[0]["poc"] is not None
    assert {"d30", "d90", "d180", "migration", "migration_series", "naked"} <= set(out[0]["poc"].keys())


def test_fetch_stocks_signals_empty_movers_returns_empty():
    with patch.object(fetch_market, "yahoo_most_active", return_value=[]):
        out = fetch_market.fetch_stocks_signals(limit=10)
    assert out == []


def test_fetch_stocks_signals_handles_empty_history_per_symbol():
    """Even if a symbol's history fetch is empty, the entry should still
    be emitted (with score=0/label=HOLD) — does not crash."""
    movers = [
        {"symbol": "TSLA", "name": "Tesla", "last_price": 250.0,
         "change_pct": 0.0, "volume": 5_000_000},
    ]
    with patch.object(fetch_market, "yahoo_most_active", return_value=movers), \
         patch.object(fetch_market, "yahoo_chart_history", return_value=[]), \
         patch.object(fetch_market.time, "sleep"):
        out = fetch_market.fetch_stocks_signals(limit=1)
    assert len(out) == 1
    assert out[0]["symbol"] == "TSLA"
    assert out[0]["score"] == 0
    assert out[0]["label"] == "HOLD"


# ============================================================================
# cryptocompare_market
# ============================================================================

def _make_cc_response(success: bool, rows: list[dict]) -> MagicMock:
    """Build a fake requests.Response mock."""
    resp = MagicMock()
    resp.status_code = 200
    payload = {
        "Response": "Success" if success else "Error",
        "Data": {"Data": rows},
    }
    resp.json = MagicMock(return_value=payload)
    return resp


def _isolate_stale(monkeypatch, tmp_path):
    """Point the stale-cache dir at a fresh temp dir so individual tests do
    not see (or pollute) each other's persisted stale snapshots."""
    monkeypatch.setattr(fetch_market, "_STALE_DIR", tmp_path / ".stale")


def test_cryptocompare_market_happy_path(monkeypatch, tmp_path):
    _isolate_stale(monkeypatch, tmp_path)
    base = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
    rows = [
        {"time": base + i * 86400, "close": 100.0 + i, "volumeto": 10_000 + i * 100}
        for i in range(5)
    ]
    resp = _make_cc_response(True, rows)
    with patch.object(fetch_market.requests, "get", return_value=resp):
        out = fetch_market.cryptocompare_market("BTC", days=10)
    assert isinstance(out, dict)
    assert "price" in out and "volume" in out
    assert len(out["price"]) == 5
    assert len(out["volume"]) == 5
    assert out["price"][0]["date"] == "2025-01-01"
    assert out["price"][0]["value"] == 100.0
    assert out["volume"][0]["value"] == 10_000.0


def test_cryptocompare_market_response_not_success(monkeypatch, tmp_path):
    """Response != Success and no stale cache -> {price: [], volume: []}."""
    _isolate_stale(monkeypatch, tmp_path)
    rows = [{"time": 1, "close": 100.0, "volumeto": 1.0}]
    resp = _make_cc_response(False, rows)
    with patch.object(fetch_market.requests, "get", return_value=resp):
        out = fetch_market.cryptocompare_market("BTC", days=10)
    assert out == {"price": [], "volume": []}


def test_cryptocompare_market_zero_or_missing_volume(monkeypatch, tmp_path):
    _isolate_stale(monkeypatch, tmp_path)
    base = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
    rows = [
        {"time": base,             "close": 100.0, "volumeto": 0},
        {"time": base + 86400,     "close": 101.0},  # missing volumeto entirely
        {"time": base + 86400 * 2, "close": 102.0, "volumeto": 5_000},
    ]
    resp = _make_cc_response(True, rows)
    with patch.object(fetch_market.requests, "get", return_value=resp):
        out = fetch_market.cryptocompare_market("BTC", days=10)
    # All three rows kept; missing/zero volume becomes 0.0
    assert len(out["price"]) == 3
    assert len(out["volume"]) == 3
    assert out["volume"][0]["value"] == 0.0
    assert out["volume"][1]["value"] == 0.0
    assert out["volume"][2]["value"] == 5_000.0


def test_cryptocompare_market_empty_symbol_returns_empty(monkeypatch, tmp_path):
    _isolate_stale(monkeypatch, tmp_path)
    out = fetch_market.cryptocompare_market("", days=10)
    assert out == {"price": [], "volume": []}


def test_cryptocompare_market_skips_nonpositive_close(monkeypatch, tmp_path):
    _isolate_stale(monkeypatch, tmp_path)
    base = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
    rows = [
        {"time": base,             "close": 0.0,   "volumeto": 100},
        {"time": base + 86400,     "close": -5.0,  "volumeto": 200},
        {"time": base + 86400 * 2, "close": 99.0,  "volumeto": 300},
    ]
    resp = _make_cc_response(True, rows)
    with patch.object(fetch_market.requests, "get", return_value=resp):
        out = fetch_market.cryptocompare_market("BTC", days=10)
    assert len(out["price"]) == 1
    assert out["price"][0]["value"] == 99.0


def test_cryptocompare_market_http_error_returns_empty(monkeypatch, tmp_path):
    """status_code != 200 and no stale cache -> empty dict."""
    _isolate_stale(monkeypatch, tmp_path)
    resp = MagicMock()
    resp.status_code = 500
    resp.json = MagicMock(return_value={})
    with patch.object(fetch_market.requests, "get", return_value=resp):
        out = fetch_market.cryptocompare_market("BTC", days=10)
    assert out == {"price": [], "volume": []}


# ============================================================================
# compute_poc_top_markets
# ============================================================================

def _series(n: int, base_price: float = 100.0,
            base_vol: float = 1_000_000.0, start: str = "2024-01-01") -> tuple[list[dict], list[dict]]:
    """Return (price_series, volume_series) suitable for point_of_control:
    at least 10 matching dates so the POC calc returns a non-None dict."""
    start_dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    prices, vols = [], []
    for i in range(n):
        d = (start_dt + timedelta(days=i)).strftime("%Y-%m-%d")
        # Oscillating prices so hi > lo (otherwise POC returns None).
        p = base_price + (i % 5) * 1.5
        prices.append({"date": d, "value": p})
        vols.append({"date": d, "value": base_vol + (i % 7) * 1000})
    return prices, vols


def test_compute_poc_top_markets_happy_path(tmp_path, monkeypatch):
    # Redirect CACHE so the stale-load doesn't read the real file.
    monkeypatch.setattr(fetch_market, "CACHE", tmp_path)
    prices, vols = _series(60)

    top = [
        {"id": "bitcoin",  "symbol": "btc", "name": "Bitcoin",
         "image": "https://example/btc.png", "price_usd": 50_000.0},
        {"id": "ethereum", "symbol": "eth", "name": "Ethereum",
         "image": "https://example/eth.png", "price_usd": 3_000.0},
    ]
    with patch.object(fetch_market, "cryptocompare_market",
                      return_value={"price": prices, "volume": vols}):
        out = fetch_market.compute_poc_top_markets(top, n=2, days=60)

    assert len(out) == 2
    required_keys = {
        "coin_id", "symbol", "name", "image", "current_price", "poc",
        "signal_history",
    }
    for entry in out:
        # Subset check: required keys present; additive fields allowed.
        assert required_keys <= set(entry.keys())
        # POC sub-dict must have d30/d90/d180/migration/naked/migration_series
        for k in ("d30", "d90", "d180", "migration", "naked", "migration_series"):
            assert k in entry["poc"]
        # signal_history mirrors stocks_signals[i].history shape so the
        # crypto breadth chart can render off it.
        assert isinstance(entry["signal_history"], list)
        for h in entry["signal_history"]:
            assert isinstance(h.get("date"), str)
            assert isinstance(h.get("score"), int)
            assert -100 <= h["score"] <= 100
    assert out[0]["coin_id"] == "bitcoin"
    assert out[0]["symbol"] == "BTC"  # uppercased
    assert out[0]["current_price"] == 50_000.0


def test_compute_poc_top_markets_empty_returns_empty():
    assert fetch_market.compute_poc_top_markets([], n=25, days=180) == []


def test_compute_poc_top_markets_stale_keep_loads_prev(tmp_path, monkeypatch):
    """When cryptocompare returns empty for a coin, it should pull the
    previous entry from data/market.json (if present) and emit it with
    stale=True."""
    monkeypatch.setattr(fetch_market, "CACHE", tmp_path)
    prev_payload = {
        "poc_top": [
            {
                "coin_id": "bitcoin", "symbol": "BTC", "name": "Bitcoin",
                "image": "x", "current_price": 49_000.0,
                "poc": {"d30": None, "d90": None, "d180": None,
                        "migration": None, "naked": [], "migration_series": []},
            },
        ],
    }
    (tmp_path / "market.json").write_text(json.dumps(prev_payload))

    top = [
        {"id": "bitcoin", "symbol": "btc", "name": "Bitcoin",
         "image": "x", "price_usd": 50_000.0},
    ]
    # cryptocompare_market returns empty -> stale path triggers.
    with patch.object(fetch_market, "cryptocompare_market",
                      return_value={"price": [], "volume": []}):
        out = fetch_market.compute_poc_top_markets(top, n=1, days=60)

    assert len(out) == 1
    assert out[0]["coin_id"] == "bitcoin"
    assert out[0].get("stale") is True


def test_compute_poc_top_markets_skips_missing_id_or_symbol(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_market, "CACHE", tmp_path)
    top = [
        {"id": None, "symbol": "btc"},        # missing id
        {"id": "ethereum", "symbol": ""},     # missing symbol
    ]
    with patch.object(fetch_market, "cryptocompare_market") as ccm:
        out = fetch_market.compute_poc_top_markets(top, n=5, days=60)
    assert out == []
    ccm.assert_not_called()


def test_compute_poc_top_markets_no_prev_file_no_stale(tmp_path, monkeypatch):
    """No market.json on disk + empty cryptocompare -> coin simply skipped."""
    monkeypatch.setattr(fetch_market, "CACHE", tmp_path)
    top = [
        {"id": "bitcoin", "symbol": "btc", "name": "Bitcoin",
         "image": "x", "price_usd": 50_000.0},
    ]
    with patch.object(fetch_market, "cryptocompare_market",
                      return_value={"price": [], "volume": []}):
        out = fetch_market.compute_poc_top_markets(top, n=1, days=60)
    assert out == []
