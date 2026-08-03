"""Data-layer provenance tests: every freshness stamp must report the age
of the DATA, never the age of the fetch or the build.

Background — the bug these guard against: the top-50 crypto breadth chart
sat frozen at 2026-06-09 for eight weeks behind a page that looked
completely current, because the payload carried no honest observation date
and the one date it did carry (``signals_top20[].as_of``) was
``datetime.now(UTC)`` stamped at scoring time.

The rules encoded here:

  1. An ``as_of`` is the date the DATA was observed upstream.
  2. A stale-kept / carried-forward payload's ``as_of`` must NOT advance.
  3. A composite is only as fresh as its OLDEST input (min, not max, not
     mean).
  4. Stale entries are flagged so a consumer can disclose "N of M cached".
  5. No honest date available -> ``None``, never a substituted clock read.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import fetch_market
import fetch_stock_money_flow as sf
import signals


TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
LONG_AGO = "2026-06-09"  # the date the breadth chart actually froze at


# ============================================================================
# 1. markets_top — CoinGecko last_updated becomes the row's as_of
# ============================================================================

def _cg_markets_row(**over):
    row = {
        "market_cap_rank": 1,
        "id": "bitcoin",
        "symbol": "btc",
        "name": "Bitcoin",
        "image": "img",
        "current_price": 50_000.0,
        "market_cap": 1e12,
        "total_volume": 3e10,
        "price_change_percentage_24h_in_currency": 1.0,
        "price_change_percentage_7d_in_currency": 2.0,
        "price_change_percentage_30d_in_currency": 3.0,
        "sparkline_in_7d": {"price": [float(i) for i in range(168)]},
        "last_updated": "2026-08-01T16:49:31.736Z",
    }
    row.update(over)
    return row


def test_coingecko_top_markets_carries_observation_date():
    with patch.object(fetch_market, "_get", return_value=[_cg_markets_row()]):
        out = fetch_market.coingecko_top_markets(1)
    assert len(out) == 1
    # Date portion of CoinGecko's own last_updated, not today.
    assert out[0]["as_of"] == "2026-08-01"


def test_coingecko_top_markets_missing_last_updated_is_none_not_today():
    """Rule 5: no upstream date -> explicit None. Substituting today here
    would make an undated row indistinguishable from a fresh one."""
    row = _cg_markets_row()
    del row["last_updated"]
    with patch.object(fetch_market, "_get", return_value=[row]):
        out = fetch_market.coingecko_top_markets(1)
    assert out[0]["as_of"] is None
    assert out[0]["as_of"] != TODAY


# ============================================================================
# 2. signals_top20 — as_of is the row's observation date, not now()
# ============================================================================

def _coin(**over):
    c = {
        "symbol": "BTC",
        "name": "Bitcoin",
        "rank": 1,
        "image": "img",
        "price_usd": 50_000.0,
        "market_cap_usd": 1e12,
        "volume_24h_usd": 3e10,
        "change_24h_pct": 1.0,
        "change_7d_pct": 2.0,
        "change_30d_pct": 3.0,
        # 168 hourly points, gently rising — enough for every component.
        "sparkline_7d": [100.0 + i * 0.1 for i in range(168)],
        "as_of": LONG_AGO,
    }
    c.update(over)
    return c


def test_compute_signal_simple_as_of_is_the_data_date():
    out = signals.compute_signal_simple(_coin())
    assert out is not None
    assert out["as_of"] == LONG_AGO


def test_compute_signal_simple_as_of_is_not_the_clock():
    """The regression itself: a two-month-old row must NOT come out
    stamped with today's date."""
    out = signals.compute_signal_simple(_coin())
    assert out["as_of"] != TODAY


def test_compute_signal_simple_as_of_none_when_row_undated():
    out = signals.compute_signal_simple(_coin(as_of=None))
    assert out["as_of"] is None


def test_compute_signal_simple_accepts_iso_datetime_as_of():
    out = signals.compute_signal_simple(_coin(as_of="2026-06-09T12:34:56Z"))
    assert out["as_of"] == "2026-06-09"


def test_compute_signal_simple_computed_at_is_distinct_from_as_of():
    """Fetch/build time still exists but under an unambiguous name, so it
    can never be mistaken for a data date."""
    out = signals.compute_signal_simple(_coin())
    assert out["computed_at"].startswith(TODAY)
    assert out["computed_at"] != out["as_of"]


def test_compute_signal_simple_propagates_stale_flag():
    """Rule 4: a date alone under-reports a partially-cached list."""
    assert signals.compute_signal_simple(_coin())["stale"] is False
    assert signals.compute_signal_simple(_coin(stale=True))["stale"] is True


def test_compute_all_top20_end_to_end_keeps_row_dates():
    payload = {"market": {"markets_top": [
        _coin(symbol="BTC", as_of="2026-07-30"),
        _coin(symbol="ETH", as_of=LONG_AGO, stale=True),
        _coin(symbol="USDT"),  # stablecoin, excluded
    ]}}
    out = signals.compute_all_top20(payload)
    by_sym = {s["symbol"]: s for s in out}
    assert "USDT" not in by_sym
    assert by_sym["BTC"]["as_of"] == "2026-07-30"
    assert by_sym["ETH"]["as_of"] == LONG_AGO
    assert by_sym["ETH"]["stale"] is True
    # A consumer computing the composite stamp takes the MIN (rule 3).
    assert min(s["as_of"] for s in out) == LONG_AGO
    # ...and can disclose the cached count (rule 4).
    assert sum(1 for s in out if s["stale"]) == 1


def test_markets_top_stale_keep_flags_rows_and_freezes_dates(tmp_path, monkeypatch):
    """The stale-keep path copies the previous list forward when CoinGecko
    429s. Those rows must keep their original as_of and gain stale=True so
    the UI can say "N of M served from cache" — and the signal computed
    from them must inherit both."""
    monkeypatch.setattr(fetch_market, "CACHE", tmp_path)
    (tmp_path / "market.json").write_text(json.dumps({"markets_top": [
        {"symbol": "BTC", "as_of": LONG_AGO, "price_usd": 1.0},
    ]}))
    carried = fetch_market.stale_keep_markets_top()
    assert carried[0]["as_of"] == LONG_AGO
    assert carried[0]["stale"] is True
    sig = signals.compute_signal_simple(_coin(**carried[0]))
    assert sig["as_of"] == LONG_AGO and sig["stale"] is True


def test_stale_keep_markets_top_no_cache_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_market, "CACHE", tmp_path)
    assert fetch_market.stale_keep_markets_top() == []


def test_stale_keep_markets_top_unreadable_cache_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_market, "CACHE", tmp_path)
    (tmp_path / "market.json").write_text("{not json")
    assert fetch_market.stale_keep_markets_top() == []


# ============================================================================
# 3. stocks_signals — per-row observation date from the last daily bar
# ============================================================================

def _bars(n: int, end: str = "2026-07-31") -> list[dict]:
    last = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return [{
        "date": (last - timedelta(days=n - 1 - i)).strftime("%Y-%m-%d"),
        "open": 100.0 + i, "high": 101.0 + i, "low": 99.0 + i,
        "close": 100.0 + i * 0.5, "volume": 1_000_000,
    } for i in range(n)]


def test_stocks_signals_row_carries_last_bar_date():
    movers = [{"symbol": "AAPL", "name": "Apple", "last_price": 1.0,
               "change_pct": 1.0, "volume": 1}]
    hist = _bars(220, end="2026-07-31")
    with patch.object(fetch_market, "yahoo_most_active", return_value=movers), \
         patch.object(fetch_market, "yahoo_chart_history", return_value=hist):
        out = fetch_market.fetch_stocks_signals(limit=1)
    assert out[0]["as_of"] == "2026-07-31"
    assert out[0]["as_of"] == out[0]["history"][-1]["date"]


def test_stocks_signals_row_as_of_trails_on_a_stale_calendar():
    """Weekend / holiday / throttled-fetch case: the newest bar is days
    old and the row must say so rather than reading as today."""
    movers = [{"symbol": "AAPL", "name": "Apple", "last_price": 1.0,
               "change_pct": 1.0, "volume": 1}]
    with patch.object(fetch_market, "yahoo_most_active", return_value=movers), \
         patch.object(fetch_market, "yahoo_chart_history",
                      return_value=_bars(220, end=LONG_AGO)):
        out = fetch_market.fetch_stocks_signals(limit=1)
    assert out[0]["as_of"] == LONG_AGO
    assert out[0]["as_of"] != TODAY


def test_stocks_signals_row_as_of_none_when_history_empty():
    movers = [{"symbol": "AAPL", "name": "Apple", "last_price": 1.0,
               "change_pct": 1.0, "volume": 1}]
    with patch.object(fetch_market, "yahoo_most_active", return_value=movers), \
         patch.object(fetch_market, "yahoo_chart_history", return_value=[]):
        out = fetch_market.fetch_stocks_signals(limit=1)
    assert out[0]["as_of"] is None


# ============================================================================
# 4. poc_top — carried-forward entries report their TRUE age
# ============================================================================

def _cc_series(n: int, end: str) -> dict:
    last = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    dates = [(last - timedelta(days=n - 1 - i)).strftime("%Y-%m-%d")
             for i in range(n)]
    return {
        "price":  [{"date": d, "value": 100.0 + (i % 17)} for i, d in enumerate(dates)],
        "volume": [{"date": d, "value": 1000.0 + (i % 7)} for i, d in enumerate(dates)],
    }


def test_poc_entry_as_of_prefers_explicit_field():
    assert fetch_market.poc_entry_as_of({"as_of": "2026-07-02"}) == "2026-07-02"


def test_poc_entry_as_of_backfills_from_signal_history():
    """Entries written before as_of existed (restored from the Actions
    cache) must still report a real age."""
    e = {"signal_history": [{"date": "2026-06-08", "score": 1},
                            {"date": LONG_AGO, "score": 2}]}
    assert fetch_market.poc_entry_as_of(e) == LONG_AGO


def test_poc_entry_as_of_none_when_undatable():
    assert fetch_market.poc_entry_as_of({}) is None
    assert fetch_market.poc_entry_as_of(None) is None
    assert fetch_market.poc_entry_as_of({"as_of": "not-a-date"}) is None
    assert fetch_market.poc_entry_as_of({"as_of": 20260609}) is None
    assert fetch_market.poc_entry_as_of({"signal_history": [{"score": 1}]}) is None


def test_poc_entry_as_of_falls_back_when_as_of_is_junk():
    """A corrupt as_of must not shadow a perfectly good signal_history."""
    e = {"as_of": "not-a-date", "signal_history": [{"date": LONG_AGO}]}
    assert fetch_market.poc_entry_as_of(e) == LONG_AGO


def test_compute_poc_top_markets_fresh_entry_dated_from_series(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_market, "CACHE", tmp_path)
    top = [{"id": "bitcoin", "symbol": "btc", "name": "Bitcoin",
            "image": "x", "price_usd": 50_000.0}]
    with patch.object(fetch_market, "cryptocompare_market",
                      return_value=_cc_series(180, "2026-08-01")):
        out = fetch_market.compute_poc_top_markets(top, n=1, days=180)
    assert len(out) == 1
    assert out[0]["as_of"] == "2026-08-01"
    assert out[0].get("stale") is not True


def test_compute_poc_top_markets_carry_forward_does_not_advance_as_of(tmp_path, monkeypatch):
    """The core stale-keep rule: a re-served entry keeps the date it was
    originally observed on."""
    monkeypatch.setattr(fetch_market, "CACHE", tmp_path)
    recent = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")
    (tmp_path / "market.json").write_text(json.dumps({"poc_top": [{
        "coin_id": "bitcoin", "symbol": "BTC", "name": "Bitcoin",
        "image": "x", "current_price": 49_000.0, "as_of": recent,
        "poc": {}, "signal_history": [{"date": recent, "score": 5}],
    }]}))
    top = [{"id": "bitcoin", "symbol": "btc", "name": "Bitcoin",
            "image": "x", "price_usd": 50_000.0}]
    with patch.object(fetch_market, "cryptocompare_market",
                      return_value={"price": [], "volume": []}):
        out = fetch_market.compute_poc_top_markets(top, n=1, days=60)
    assert len(out) == 1
    assert out[0]["stale"] is True
    assert out[0]["as_of"] == recent
    assert out[0]["as_of"] != TODAY


def test_compute_poc_top_markets_carry_forward_backfills_as_of(tmp_path, monkeypatch):
    """A previous entry with no as_of at all gets one derived from its own
    signal_history — not from the clock."""
    monkeypatch.setattr(fetch_market, "CACHE", tmp_path)
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
    (tmp_path / "market.json").write_text(json.dumps({"poc_top": [{
        "coin_id": "bitcoin", "symbol": "BTC", "name": "Bitcoin",
        "image": "x", "current_price": 49_000.0, "poc": {},
        "signal_history": [{"date": recent, "score": 5}],
    }]}))
    top = [{"id": "bitcoin", "symbol": "btc", "name": "Bitcoin",
            "image": "x", "price_usd": 50_000.0}]
    with patch.object(fetch_market, "cryptocompare_market",
                      return_value={"price": [], "volume": []}):
        out = fetch_market.compute_poc_top_markets(top, n=1, days=60)
    assert out[0]["as_of"] == recent


def test_compute_poc_top_markets_per_symbol_stale_cache_is_flagged(tmp_path, monkeypatch):
    """cryptocompare_market has its OWN stale-fallback; entries built from
    it must be counted as cached too, and dated by the cached series."""
    monkeypatch.setattr(fetch_market, "CACHE", tmp_path)
    cached = _cc_series(180, LONG_AGO)
    cached["stale"] = True
    cached["stale_age_sec"] = 999
    top = [{"id": "bitcoin", "symbol": "btc", "name": "Bitcoin",
            "image": "x", "price_usd": 50_000.0}]
    with patch.object(fetch_market, "cryptocompare_market", return_value=cached):
        out = fetch_market.compute_poc_top_markets(top, n=1, days=180)
    assert out[0]["stale"] is True
    assert out[0]["as_of"] == LONG_AGO


# ============================================================================
# 5. coinbase_intl_perps — per-row funding timestamp
# ============================================================================

def _perp(**over):
    inst = {
        "type": "PERP",
        "symbol": "BTC-PERP",
        "open_interest": "10",
        "qty_24hr": "5",
        "notional_24hr": "7",
        "quote": {
            "predicted_funding": "0.0001",
            "mark_price": "50000",
            "index_price": "49990",
            "timestamp": "2026-08-01T05:31:56Z",
        },
    }
    inst.update(over)
    return inst


def test_coinbase_intl_perps_row_carries_quote_timestamp():
    with patch.object(fetch_market, "_get", return_value=[_perp()]):
        out = fetch_market.coinbase_intl_perpetuals()
    assert len(out) == 1
    assert out[0]["as_of"] == "2026-08-01"
    assert out[0]["as_of_ts"] == "2026-08-01T05:31:56Z"


def test_coinbase_intl_perps_row_none_when_upstream_undated():
    inst = _perp()
    del inst["quote"]["timestamp"]
    with patch.object(fetch_market, "_get", return_value=[inst]):
        out = fetch_market.coinbase_intl_perpetuals()
    assert out[0]["as_of"] is None
    assert out[0]["as_of_ts"] is None


def test_coinbase_intl_perps_falls_back_to_instrument_timestamp():
    inst = _perp(timestamp="2026-07-15T00:00:00Z")
    del inst["quote"]["timestamp"]
    with patch.object(fetch_market, "_get", return_value=[inst]):
        out = fetch_market.coinbase_intl_perpetuals()
    assert out[0]["as_of"] == "2026-07-15"


# ============================================================================
# 6. defi — the sidecar's derivable date
# ============================================================================

FIXED_NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)


def _tvl(end: str, n: int = 5) -> list[dict]:
    last = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return [{"date": (last - timedelta(days=n - 1 - i)).strftime("%Y-%m-%d"),
             "tvl_usd": 1.0} for i in range(n)]


def test_defi_provenance_takes_the_oldest_input():
    """Rule 3 — min, not max. The Base series lags by two days and the
    composite stamp must say so."""
    out = fetch_market.defi_provenance(
        chains=[{"name": "Ethereum"}],
        protocols=[{"name": "Aave"}],
        yields_stablecoin=[{"pool": "x"}],
        bridges={"top_bridges": [{"name": "b"}]},
        tvl_history={"Ethereum": _tvl("2026-08-02"), "Base": _tvl("2026-07-31")},
        observed_at=FIXED_NOW,
    )
    assert out["as_of"] == "2026-07-31"
    assert out["sources"]["tvl_history"] == {"Ethereum": "2026-08-02",
                                             "Base": "2026-07-31"}
    assert out["sources"]["chains"] == "2026-08-02"


def test_defi_provenance_all_empty_is_unavailable():
    """Rule 5 — nothing observed, so no date. Never the clock."""
    out = fetch_market.defi_provenance(None, None, None, None, None,
                                       observed_at=FIXED_NOW)
    assert out["as_of"] is None
    assert all(v is None for k, v in out["sources"].items() if k != "tvl_history")


def test_defi_provenance_failed_input_contributes_no_date():
    """A DefiLlama call that failed returns empty — it must not contribute
    a snapshot date, but must also not fabricate an old one."""
    out = fetch_market.defi_provenance(
        chains=[], protocols=[{"name": "Aave"}], yields_stablecoin=[],
        bridges={"top_bridges": []},
        tvl_history={"Ethereum": _tvl("2026-08-01")},
        observed_at=FIXED_NOW,
    )
    assert out["sources"]["chains"] is None
    assert out["sources"]["bridges"] is None
    assert out["as_of"] == "2026-08-01"


def test_defi_provenance_observed_at_is_not_the_stamp():
    """observed_at exists for debugging only; as_of must come from the
    data whenever the data has a date of its own."""
    out = fetch_market.defi_provenance(
        chains=None, protocols=None, yields_stablecoin=None, bridges=None,
        tvl_history={"Ethereum": _tvl(LONG_AGO)}, observed_at=FIXED_NOW,
    )
    assert out["as_of"] == LONG_AGO
    assert out["observed_at"].startswith("2026-08-02")


def test_series_last_date_ignores_junk():
    assert fetch_market._series_last_date([]) is None
    assert fetch_market._series_last_date(None) is None
    assert fetch_market._series_last_date([{"date": "nope"}, {"x": 1}, "str"]) is None
    assert fetch_market._series_last_date(
        [{"date": "2026-01-02"}, {"date": "2026-03-04"}]) == "2026-03-04"


# ============================================================================
# 7. stock money-flow sidecar — payload stamp is the oldest bar
# ============================================================================

def test_stock_flow_row_carries_bar_date():
    rec = {"ticker": "UP", "name": "Up Co",
           "index_membership": ["NASDAQ-100"], "sector": "Tech"}
    bars = [{"date": (datetime(2026, 7, 1, tzinfo=timezone.utc)
                      + timedelta(days=i)).strftime("%Y-%m-%d"),
             "open": i + 1.0, "high": i + 2.0, "low": i + 0.5,
             "close": i + 1.5, "volume": 1000} for i in range(30)]
    out = sf._score_stock(rec, bars)
    assert out["as_of"] == "2026-07-30"


def test_stock_flow_payload_as_of_is_the_oldest_row():
    assert sf._oldest_as_of([{"as_of": "2026-08-01"}, {"as_of": "2026-07-20"}]) == "2026-07-20"
    assert sf._oldest_as_of([{"as_of": None}, {}]) is None
    assert sf._oldest_as_of([]) is None


def test_build_from_signals_forwards_row_dates(monkeypatch):
    monkeypatch.setattr(sf, "_load_universe", lambda: [
        {"ticker": "AAA", "name": "A", "index_membership": ["DJIA"], "sector": "Tech"},
        {"ticker": "BBB", "name": "B", "index_membership": ["DJIA"], "sector": "Tech"},
    ])
    payload = sf.build_from_signals([
        {"symbol": "AAA", "mfi": 70.0, "cmf": 0.1, "as_of": "2026-08-01"},
        {"symbol": "BBB", "mfi": 30.0, "cmf": -0.1, "as_of": LONG_AGO},
    ], write=False)
    assert payload["scored_count"] == 2
    # Oldest contributing bar wins.
    assert payload["as_of"] == LONG_AGO
    assert {s["symbol"]: s["as_of"] for s in payload["stocks"]} == {
        "AAA": "2026-08-01", "BBB": LONG_AGO}


def test_build_from_signals_undated_rows_yield_no_stamp(monkeypatch):
    monkeypatch.setattr(sf, "_load_universe", lambda: [
        {"ticker": "AAA", "name": "A", "index_membership": ["DJIA"], "sector": "Tech"},
    ])
    payload = sf.build_from_signals(
        [{"symbol": "AAA", "mfi": 70.0, "cmf": 0.1}], write=False)
    assert payload["scored_count"] == 1
    assert payload["as_of"] is None


# ============================================================================
# 8. whale sentiment composites — as_of is the OLDEST proxy series, not the
#    fetch clock
# ============================================================================
# The BTC and ETH whale composites used to stamp themselves with
# ``whale["fetched_at"][:10]``. That advances on every run, and the whale tree
# is stale-kept in pieces (bitinfocharts falls back to the previous
# distribution; glassnode / etherscan fall back to data/.stale/*.json), so a
# completely failed refresh still came out wearing today's date — under a UI
# tooltip promising "Observation date … Not the page build time".

def _dates(n: int, end: str) -> list[str]:
    last = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return [(last - timedelta(days=n - 1 - i)).strftime("%Y-%m-%d")
            for i in range(n)]


def _series(n: int, end: str, base: float = 100.0) -> list[dict]:
    return [{"date": d, "value": base + i} for i, d in enumerate(_dates(n, end))]


def _whale_payload(*, frozen_series: str | None = None,
                   fetched_at: str = "2026-08-02T23:00:00+00:00") -> dict:
    """A whale tree rich enough for all six BTC components to fire."""
    dist = [{"date": d, "b1k_10k": 1000.0 + i, "b10k_100k": 500.0,
             "b100k_1m": 100.0}
            for i, d in enumerate(_dates(31, "2026-07-25"))]
    btc = {k: _series(31, "2026-08-01") for k in (
        "hash_rate", "miners_revenue_usd", "avg_tx_usd",
        "output_volume_btc", "active_addresses")}
    if frozen_series:
        btc[frozen_series] = _series(31, LONG_AGO)
    return {"fetched_at": fetched_at,
            "distribution": {"buckets": dist},
            "btc": btc}


def test_whale_sentiment_as_of_is_not_the_fetch_clock():
    out = fetch_market.compute_whale_sentiment(_whale_payload())
    assert out["as_of"] != TODAY
    # Oldest contributing input: the bitinfocharts cohort row.
    assert out["as_of"] == "2026-07-25"
    # The clock survives, but only under an unambiguous name.
    assert out["fetched_at"] == "2026-08-02T23:00:00+00:00"


def test_whale_sentiment_takes_the_oldest_contributing_series():
    out = fetch_market.compute_whale_sentiment(
        _whale_payload(frozen_series="avg_tx_usd"))
    assert out["as_of"] == LONG_AGO, "min across inputs, never max"
    per_component = {c["name"]: c["as_of"] for c in out["components"]}
    assert per_component["Avg tx USD z30"] == LONG_AGO
    assert per_component["Hash rate vs 30d"] == "2026-08-01"


def test_whale_sentiment_as_of_does_not_advance_on_a_stale_keep():
    """Same data, later fetch clock — the date must not move."""
    first = fetch_market.compute_whale_sentiment(_whale_payload())
    later = fetch_market.compute_whale_sentiment(
        _whale_payload(fetched_at="2026-12-25T04:00:00+00:00"))
    assert later["as_of"] == first["as_of"]
    assert later["fetched_at"] != first["fetched_at"]


def test_whale_sentiment_undated_series_are_counted_not_absorbed():
    payload = _whale_payload()
    payload["btc"]["hash_rate"] = [{"date": None, "value": float(i)}
                                   for i in range(31)]
    out = fetch_market.compute_whale_sentiment(payload)
    assert out["undated_inputs"] == 1
    assert out["dated_inputs"] == 5
    assert out["as_of"] == "2026-07-25"


def test_whale_sentiment_carries_the_provenance_marker():
    """Consumers gate on this field before trusting as_of."""
    out = fetch_market.compute_whale_sentiment(_whale_payload())
    assert out["as_of_basis"] == fetch_market.WHALE_AS_OF_BASIS


def test_whale_sentiment_eth_as_of_is_the_oldest_series():
    whale = {"fetched_at": "2026-08-02T23:00:00+00:00", "eth": {
        "coin_metrics": {"AdrActCnt": _series(31, "2026-07-30"),
                         "TxCnt": _series(31, LONG_AGO)},
        "etherscan_daily": {"series": _series(31, "2026-08-01", 7200.0)},
    }}
    out = fetch_market.compute_whale_sentiment_eth(whale)
    assert out["as_of"] == LONG_AGO
    assert out["as_of"] != TODAY
    assert out["fetched_at"] == "2026-08-02T23:00:00+00:00"
    assert out["as_of_basis"] == fetch_market.WHALE_AS_OF_BASIS


def test_whale_sentiment_eth_no_data_branch_has_no_date():
    out = fetch_market.compute_whale_sentiment_eth(
        {"fetched_at": "2026-08-02T23:00:00+00:00", "eth": {}})
    assert out["available"] is False
    assert out["as_of"] is None, "no inputs ⇒ no date, never the fetch clock"


def test_obs_date_helpers_reject_junk():
    assert fetch_market._obs_date_of_series(None) is None
    assert fetch_market._obs_date_of_series([]) is None
    assert fetch_market._obs_date_of_series([{"date": "2026-01-01"}]) is None  # no value
    assert fetch_market._obs_date_of_series(
        [{"date": "2026-01-01", "value": 1},
         {"date": "2026-02-02", "value": None}]) == "2026-01-01"
    assert fetch_market._obs_date_of_row({"date": "2026-03-04x"}) == "2026-03-04"
    assert fetch_market._obs_date_of_row({"date": 20260304}) is None
    assert fetch_market._obs_date_of_row("nope") is None


# ============================================================================
# 9. Source guard — no clock read may be assigned to an `as_of`
# ============================================================================
# This is the guard that was missing: 126 tests were green while
# `"as_of": (whale.get("fetched_at") or "")[:10]` shipped. A stamp field named
# as_of must never be assigned from a fetch/build clock on the same statement.

CLOCK_TOKENS = ("fetched_at", "datetime.now", "time.time", "utcnow",
                "generated_at", "computed_at")


def test_no_as_of_is_assigned_from_a_clock_read():
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parent.parent
    for name in ("fetch_market.py", "signals.py", "fetch_stock_money_flow.py",
                 "scripts/snapshot_composites.py"):
        src = (root / name).read_text(encoding="utf-8")
        for m in re.finditer(r'^[^#\n]*["\']as_of["\']\s*:\s*(.+)$', src, re.M):
            value = m.group(1)
            for bad in CLOCK_TOKENS:
                assert bad not in value, (
                    f"{name}: as_of assigned from clock read {bad!r}: "
                    f"{m.group(0).strip()[:120]}")
        for m in re.finditer(r'^[^#\n]*\bas_of\s*=\s*(.+)$', src, re.M):
            value = m.group(1)
            for bad in CLOCK_TOKENS:
                assert bad not in value, (
                    f"{name}: as_of assigned from clock read {bad!r}: "
                    f"{m.group(0).strip()[:120]}")
