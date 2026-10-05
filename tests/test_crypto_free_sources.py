"""The free replacements for every CryptoCompare / CoinDesk dependency.

Since 2026-10 the whole CoinDesk/CryptoCompare API answers keyless requests
with 401 and the deployment will not buy a plan. These tests pin the free
sources that replaced it and the honesty rules they follow:

* daily close/volume series for the top-50 POC table + signal breadth:
  CoinGecko market_chart, then Coinbase -> Kraken -> Binance.US, complete UTC
  days only, cached per coin per UTC day, CoinGecko spend capped;
* the Alpine Large-Cap Crypto Index that took the CADLI chart's slot;
* community + developer stats (CoinGecko profile + GitHub REST);
* headline sentiment computed by Alpine Data from Google News RSS.

All offline: every HTTP call is mocked.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import fetch_market

ROOT = Path(__file__).resolve().parent.parent
TODAY = datetime(2026, 10, 5, tzinfo=timezone.utc).date()
NOW = datetime(2026, 10, 5, 9, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolated_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_market, "_STALE_DIR", tmp_path / ".stale")
    monkeypatch.setattr(fetch_market, "CACHE", tmp_path)
    monkeypatch.setattr(fetch_market.time, "sleep", lambda *_a, **_k: None)


def _ms(day, hour=0, minute=0, second=0):
    return int(datetime(day.year, day.month, day.day, hour, minute, second,
                        tzinfo=timezone.utc).timestamp() * 1000)


def _cg_chart(days=5, end=TODAY, price0=100.0):
    """market_chart body: one 00:00 UTC point per day up to `end` 00:00, plus
    the trailing intraday "now" sample CoinGecko appends."""
    pts = [(end - timedelta(days=days - 1 - i)) for i in range(days)]
    prices = [[_ms(d), price0 + i] for i, d in enumerate(pts)]
    vols = [[_ms(d), 1_000.0 + i] for i, d in enumerate(pts)]
    caps = [[_ms(d), 10_000.0 + i] for i, d in enumerate(pts)]
    now_ms = _ms(end, 9, 12, 33)
    return {"prices": prices + [[now_ms, 999.0]],
            "total_volumes": vols + [[now_ms, 9e9]],
            "market_caps": caps + [[now_ms, 9e12]]}


class _Router:
    """Fake `_get_status`: answers by URL substring, records every call."""

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, params=None, headers=None, timeout=25):
        self.calls.append((url, dict(params or {})))
        for needle, answer in self.routes:
            if needle in url:
                return answer(url, params) if callable(answer) else answer
        return 404, None

    def hosts(self):
        return [re.sub(r"^https://([^/]+)/.*$", r"\1", u) for u, _ in self.calls]


# --------------------------------------------------------------------------
# daily series: CoinGecko market_chart
# --------------------------------------------------------------------------

def test_coingecko_points_are_labelled_with_the_day_they_close():
    """The 00:00 UTC point carries the close of the day that just ended, so
    it is labelled with the previous day; the intraday "now" sample is a
    partial day and is dropped (never a moving intraday bar)."""
    r = _Router([("market_chart", (200, _cg_chart(days=5)))])
    with patch.object(fetch_market, "_get_status", r):
        status, s = fetch_market.coingecko_daily_series("solana", 180, TODAY)
    assert status == 200
    dates = [p["date"] for p in s["price"]]
    assert dates == ["2026-09-30", "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"]
    assert s["price"][-1]["value"] == 104.0          # 2026-10-05T00:00 sample
    assert 999.0 not in [p["value"] for p in s["price"]]
    assert s["volume"][-1] == {"date": "2026-10-04", "value": 1004.0}
    assert s["market_cap"][-1] == {"date": "2026-10-04", "value": 10004.0}
    url, params = r.calls[0]
    assert url == "https://api.coingecko.com/api/v3/coins/solana/market_chart"
    assert params == {"vs_currency": "usd", "days": "180", "interval": "daily"}


def test_coingecko_rejects_unsafe_ids_without_a_request():
    r = _Router([])
    with patch.object(fetch_market, "_get_status", r):
        for bad in ("../etc", "Bitcoin", "", None, "a/b"):
            assert fetch_market.coingecko_daily_series(bad, 180, TODAY) == (None, None)
    assert r.calls == []


def test_coingecko_demo_key_header_goes_only_to_coingecko(monkeypatch):
    monkeypatch.setattr(fetch_market, "COINGECKO_API_KEY", "demo-key-xyz")
    assert fetch_market._headers_for(
        "https://api.coingecko.com/api/v3/coins/x/market_chart")["x-cg-demo-api-key"] == "demo-key-xyz"
    for other in (fetch_market.COINBASE_CANDLES_URL.format(sym="BTC"),
                  fetch_market.KRAKEN_OHLC_URL, fetch_market.BINANCE_US_KLINES_URL,
                  fetch_market.GOOGLE_NEWS_RSS, "https://api.github.com/repos/a/b"):
        assert "demo-key-xyz" not in json.dumps(fetch_market._headers_for(other))


# --------------------------------------------------------------------------
# daily series: exchange fallbacks
# --------------------------------------------------------------------------

def _day_s(d):
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def test_coinbase_drops_open_candle_and_prices_volume_in_usd():
    rows = [[_day_s(TODAY), 1, 2, 1, 1.5, 10.0],                     # open candle
            [_day_s(TODAY - timedelta(days=1)), 1, 2, 1, 2.0, 30.0],
            [_day_s(TODAY - timedelta(days=2)), 1, 2, 1, 1.0, 20.0],
            [_day_s(TODAY - timedelta(days=3)), 1, 2, 1, 0.0, 20.0]]  # bad close
    r = _Router([("coinbase.com", (200, rows))])
    with patch.object(fetch_market, "_get_status", r):
        s = fetch_market.coinbase_daily_series("btc", 180, TODAY)
    assert [p["date"] for p in s["price"]] == ["2026-10-03", "2026-10-04"]
    assert s["volume"][-1] == {"date": "2026-10-04", "value": 60.0}   # 30 x 2.0
    assert s["market_cap"] == []
    assert r.calls[0][0] == "https://api.exchange.coinbase.com/products/BTC-USD/candles"


def test_kraken_uses_vwap_volume_and_handles_errors():
    y = TODAY - timedelta(days=1)
    body = {"error": [], "result": {"XXBTZUSD": [
        [_day_s(y), "1", "2", "1", "10", "9.5", "4", 3],
        [_day_s(TODAY), "1", "2", "1", "11", "11", "1", 1]], "last": 1}}
    r = _Router([("kraken.com", (200, body))])
    with patch.object(fetch_market, "_get_status", r):
        s = fetch_market.kraken_daily_series("BTC", 180, TODAY)
    assert s["price"] == [{"date": y.isoformat(), "value": 10.0}]
    assert s["volume"] == [{"date": y.isoformat(), "value": 38.0}]   # 4 x 9.5
    assert r.calls[0][1]["pair"] == "BTCUSD"
    with patch.object(fetch_market, "_get_status",
                      _Router([("kraken.com", (200, {"error": ["EQuery:Unknown asset pair"]}))])):
        assert fetch_market.kraken_daily_series("FOO", 180, TODAY) is None


def test_binance_us_tries_usd_then_usdt_and_uses_quote_volume():
    y = TODAY - timedelta(days=1)
    kl = [[_day_s(y) * 1000, "1", "2", "1", "5", "100", 0, "480.5"]]
    r = _Router([("symbol=FOOUSDT", (200, kl))])

    def fake(url, params=None, headers=None, timeout=25):
        r.calls.append((url, dict(params)))
        return (200, kl) if params["symbol"] == "FOOUSDT" else (400, None)

    with patch.object(fetch_market, "_get_status", fake):
        s = fetch_market.binance_us_daily_series("FOO", 180, TODAY)
    assert [c[1]["symbol"] for c in r.calls] == ["FOOUSD", "FOOUSDT"]
    assert s["volume"] == [{"date": y.isoformat(), "value": 480.5}]
    assert "api.binance.com" not in json.dumps(r.calls)            # 451 from US hosts


# --------------------------------------------------------------------------
# crypto_daily_series: chain, cache, budget, bounded staleness
# --------------------------------------------------------------------------

def _cb_rows(n=20, end=TODAY):
    return [[_day_s(end - timedelta(days=i)), 1, 2, 1, 50.0 + i, 3.0] for i in range(1, n + 1)]


def test_coingecko_first_then_cached_for_the_rest_of_the_utc_day():
    r = _Router([("market_chart", (200, _cg_chart(days=30)))])
    with patch.object(fetch_market, "_get_status", r):
        s1 = fetch_market.crypto_daily_series("solana", "SOL", 180, today=TODAY)
        s2 = fetch_market.crypto_daily_series("solana", "SOL", 180, today=TODAY)
    assert s1["source"] == "coingecko" and s1["volume_basis"] == "aggregate"
    assert s1["as_of"] == "2026-10-04" and s1["coingecko_calls"] == 1
    assert s2["cache"] == "today" and s2["coingecko_calls"] == 0
    assert len(r.calls) == 1, "a complete-day series must not be re-fetched the same UTC day"
    # ...but the next UTC day it is refreshed.
    with patch.object(fetch_market, "_get_status", r):
        fetch_market.crypto_daily_series("solana", "SOL", 180, today=TODAY + timedelta(days=1))
    assert len(r.calls) == 2


def test_falls_back_to_exchanges_in_order_and_says_volume_is_exchange_only():
    r = _Router([("market_chart", (429, None)), ("coinbase.com", (404, None)),
                 ("kraken.com", (200, {"error": [], "result": {"SOLUSD": [
                     [_day_s(TODAY - timedelta(days=1)), "1", "2", "1", "120", "119", "5", 9]]}}))])
    with patch.object(fetch_market, "_get_status", r):
        s = fetch_market.crypto_daily_series("solana", "SOL", 180, today=TODAY)
    assert s["source"] == "kraken" and s["volume_basis"] == "exchange"
    assert s["market_cap"] == []
    assert s["attempts"] == ["coingecko:429", "coinbase:none", "kraken:ok"]
    assert r.hosts() == ["api.coingecko.com", "api.exchange.coinbase.com", "api.kraken.com"]


def test_fallback_series_from_today_gets_one_coingecko_retry_then_is_kept():
    r = _Router([("market_chart", (429, None)), ("coinbase.com", (200, _cb_rows()))])
    with patch.object(fetch_market, "_get_status", r):
        first = fetch_market.crypto_daily_series("solana", "SOL", 180, today=TODAY)
        again = fetch_market.crypto_daily_series("solana", "SOL", 180, today=TODAY)
    assert first["source"] == "coinbase"
    # Retry asks CoinGecko only; the exchange series fetched earlier today is
    # complete and is kept rather than re-pulled.
    assert again["source"] == "coinbase" and again["cache"] == "today"
    assert [h for h in r.hosts()].count("api.exchange.coinbase.com") == 1


def test_all_sources_down_serves_bounded_stale_cache_with_its_own_date():
    good = _Router([("market_chart", (200, _cg_chart(days=30, end=TODAY - timedelta(days=3))))])
    with patch.object(fetch_market, "_get_status", good):
        fetch_market.crypto_daily_series("solana", "SOL", 180, today=TODAY - timedelta(days=3))
    down = _Router([])
    with patch.object(fetch_market, "_get_status", down):
        s = fetch_market.crypto_daily_series("solana", "SOL", 180, today=TODAY)
    assert s["stale"] is True and s["as_of"] == "2026-10-01"      # never advanced
    # Past DAILY_SERIES_STALE_MAX_DAYS the cache is refused: a gap, not a lie.
    with patch.object(fetch_market, "_get_status", down):
        late = fetch_market.crypto_daily_series(
            "solana", "SOL", 180,
            today=TODAY + timedelta(days=fetch_market.DAILY_SERIES_STALE_MAX_DAYS))
    assert late["price"] == [] and late["source"] is None


def test_budget_breaker_stops_coingecko_after_three_429s():
    b = fetch_market.CoinGeckoBudget(max_calls=50, pace_s=0)
    r = _Router([("market_chart", (429, None)), ("coinbase.com", (200, _cb_rows()))])
    with patch.object(fetch_market, "_get_status", r):
        for cid in ("a", "b", "c", "d", "e"):
            fetch_market.crypto_daily_series(cid, "SOL", 180, today=TODAY, budget=b)
    assert b.calls == 3 and b.summary()["breaker_tripped"] is True
    assert r.hosts().count("api.coingecko.com") == 3


def test_sweep_caps_coingecko_attempts_per_coin_per_day_and_reports_them():
    top = [{"id": f"coin-{i}", "symbol": f"C{i}"} for i in range(3)]
    r = _Router([("market_chart", (500, None)), ("coinbase.com", (200, _cb_rows()))])
    with patch.object(fetch_market, "_get_status", r):
        for _ in range(4):        # four hourly runs on the same UTC day
            res = fetch_market.fetch_top_daily_series(
                top, 3, 180, today=TODAY,
                budget=fetch_market.CoinGeckoBudget(pace_s=0))
    cg = r.hosts().count("api.coingecko.com")
    assert cg == 3 * fetch_market.COINGECKO_MAX_ATTEMPTS_PER_COIN_PER_DAY
    meta = res["meta"]
    assert meta["coingecko_calls_today"] == cg
    assert meta["by_source"] == {"coinbase": 3}
    assert meta["served_from_today_cache"] == 3
    assert meta["coingecko_this_run"]["calls"] == 0


def test_resolve_coingecko_id_prefers_cached_list_then_best_rank():
    assert fetch_market.resolve_coingecko_id(
        "btc", [{"id": "bitcoin", "symbol": "btc", "name": "Bitcoin"}]) == ("bitcoin", "Bitcoin")
    body = {"coins": [{"id": "fake-btc", "symbol": "BTC", "market_cap_rank": None},
                      {"id": "bitcoin", "symbol": "BTC", "name": "Bitcoin", "market_cap_rank": 1},
                      {"id": "btc-clone", "symbol": "BTC", "market_cap_rank": 900}]}
    with patch.object(fetch_market, "_get_status", _Router([("/search", (200, body))])):
        assert fetch_market.resolve_coingecko_id("BTC") == ("bitcoin", "Bitcoin")


# --------------------------------------------------------------------------
# Alpine Large-Cap Crypto Index
# --------------------------------------------------------------------------

def _days(n, end=TODAY - timedelta(days=1)):
    return [(end - timedelta(days=n - 1 - i)).isoformat() for i in range(n)]


def _cg_series(dates, prices, caps):
    return {"price": [{"date": d, "value": p} for d, p in zip(dates, prices)],
            "volume": [{"date": d, "value": 1.0} for d in dates],
            "market_cap": [{"date": d, "value": c} for d, c in zip(dates, caps)],
            "source": "coingecko", "volume_basis": "aggregate"}


def test_alpine_index_is_cap_weighted_price_return_and_documented():
    dates = _days(3)
    top = [{"id": "a", "symbol": "a", "name": "Alpha"}, {"id": "b", "symbol": "b", "name": "Beta"}]
    series = {"a": _cg_series(dates, [10, 11, 11], [300, 330, 330]),
              "b": _cg_series(dates, [5, 5, 4], [100, 100, 80])}
    idx = fetch_market.compute_alpine_index(top, series, n=2, window_days=3, now=NOW)
    assert idx["available"] is True and idx["base_date"] == dates[0]
    # day 1: 0.75*1.10 + 0.25*1.00 = 1.075 ; day 2: (330/430)*1 + (100/430)*0.8
    lv1 = 100 * 1.075
    lv2 = lv1 * ((330 / 430) * 1.0 + (100 / 430) * 0.8)
    assert [p["value"] for p in idx["series"]] == [100.0, round(lv1, 4), round(lv2, 4)]
    assert idx["name"] == "Alpine Large-Cap Crypto Index" and idx["computed_by"] == "Alpine Data"
    assert "CADLI" not in idx["name"] and "CoinDesk" not in idx["name"]
    assert "not CoinDesk's CADLI" in idx["method"]["caveats"]
    assert {c["symbol"] for c in idx["constituents"]} == {"A", "B"}
    assert round(sum(c["weight_pct"] for c in idx["constituents"]), 1) == 100.0


def test_alpine_index_excludes_stablecoins_wrapped_rwa_and_exchange_series():
    dates = _days(5)
    flat = [1.0, 1.001, 0.999, 1.0, 1.0]
    top = [{"id": "bitcoin", "symbol": "btc", "name": "Bitcoin"},
           {"id": "tether", "symbol": "usdt", "name": "Tether"},
           {"id": "new-dollar", "symbol": "ndl", "name": "New Dollar"},      # peg heuristic
           {"id": "wrapped-bitcoin", "symbol": "wbtc", "name": "Wrapped Bitcoin"},
           {"id": "some-staked-thing", "symbol": "sx", "name": "Staked X"},  # name heuristic
           {"id": "tether-gold", "symbol": "xaut", "name": "Tether Gold"},
           {"id": "solana", "symbol": "sol", "name": "Solana"},
           {"id": "ethereum", "symbol": "eth", "name": "Ethereum"}]
    series = {c["id"]: _cg_series(dates, [10, 11, 12, 13, 14], [500] * 5) for c in top}
    series["new-dollar"] = _cg_series(dates, flat, [50] * 5)
    series["solana"] = {**_cg_series(dates, [1, 2, 3, 4, 5], [9] * 5),
                        "source": "kraken", "market_cap": []}
    idx = fetch_market.compute_alpine_index(top, series, n=2, window_days=5, now=NOW)
    ex = idx["excluded"]
    assert ex["stablecoin"] == ["USDT"]
    assert ex["stablecoin (every close in the window within ±3% of $1)"] == ["NDL"]
    assert ex["wrapped, staked or bridged version of another coin"] == ["WBTC", "SX"]
    assert ex["tokenized real-world asset (gold, loans, funds)"] == ["XAUT"]
    assert ex["no CoinGecko market-cap history in this run"] == ["SOL"]
    assert {c["coin_id"] for c in idx["constituents"]} == {"bitcoin", "ethereum"}


def test_alpine_index_monthly_reconstitution_is_recorded():
    dates = [d for d in _days(40) if True]
    # "b" overtakes "c" in market cap during the window; selection only
    # changes at the first close of a new calendar month.
    top = [{"id": x, "symbol": x, "name": x.upper()} for x in ("a", "b", "c")]
    caps_b = [50 if d < "2026-09-15" else 200 for d in dates]
    series = {"a": _cg_series(dates, [10] * 40, [1000] * 40),
              "b": _cg_series(dates, [2] * 40, caps_b),
              "c": _cg_series(dates, [3] * 40, [100] * 40)}
    idx = fetch_market.compute_alpine_index(top, series, n=2, window_days=40, now=NOW)
    assert idx["initial_constituents"] == ["A", "C"]
    assert idx["constituent_changes"] == [{"date": "2026-10-01", "added": ["B"], "removed": ["C"]}]
    assert {c["symbol"] for c in idx["constituents"]} == {"A", "B"}


def test_alpine_index_missing_price_is_a_listed_gap_never_interpolated():
    dates = _days(4)
    top = [{"id": "a", "symbol": "a", "name": "A"}, {"id": "b", "symbol": "b", "name": "B"}]
    sa = _cg_series(dates, [10, 12, 14, 16], [100] * 4)
    sb = _cg_series(dates, [10, 10, 10, 10], [100] * 4)
    sb["price"] = [p for p in sb["price"] if p["date"] != dates[2]]
    idx = fetch_market.compute_alpine_index(top, {"a": sa, "b": sb}, n=2, window_days=4, now=NOW)
    assert [p["date"] for p in idx["series"]] == [dates[0], dates[1], dates[3]]
    assert idx["gaps"] == [{"date": dates[2], "missing": ["B"]}]
    # dates[3] chains from dates[1]: 0.5*16/12 + 0.5*10/10
    lv1 = 100 * (0.5 * 1.2 + 0.5 * 1.0)
    assert idx["series"][-1]["value"] == round(lv1 * (0.5 * 16 / 12 + 0.5), 4)


def test_alpine_index_unavailable_with_reason_when_too_few_constituents():
    dates = _days(5)
    top = [{"id": "a", "symbol": "a", "name": "A"}]
    idx = fetch_market.compute_alpine_index(
        top, {"a": _cg_series(dates, [1, 2, 3, 4, 5], [1] * 5)}, n=10, window_days=5, now=NOW)
    assert idx["available"] is False and idx["series"] == []
    assert "needs 10" in idx["reason"]


# --------------------------------------------------------------------------
# community + developer stats
# --------------------------------------------------------------------------

def _gh_commits_response(last_page=None, body=None):
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = ({"Link": f'<https://api.github.com/x?page=2>; rel="next", '
                             f'<https://api.github.com/x?since=z&per_page=1&page={last_page}>; rel="last"'}
                    if last_page else {})
    resp.json.return_value = body if body is not None else [{}]
    return resp


def _community_router():
    prof = {"watchlist_portfolio_users": 2_000_000, "sentiment_votes_up_percentage": 81.5,
            "sentiment_votes_down_percentage": 18.5, "last_updated": "2026-10-05T03:51:50.000Z",
            "community_data": None, "developer_data": None}
    repo = {"stargazers_count": 80_000, "forks_count": 37_000, "subscribers_count": 4_000,
            "open_issues_count": 700, "pushed_at": "2026-10-04T22:00:00Z"}
    return _Router([("api.coingecko.com/api/v3/coins/", (200, prof)),
                    ("api.github.com/repos/", (200, repo))])


def test_community_dev_stats_fields_sources_and_unavailable(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_test_token")
    r = _community_router()
    seen_headers = []

    def fake_get(url, params=None, headers=None, timeout=None):
        seen_headers.append((url, headers))
        return _gh_commits_response(last_page=212)

    with patch.object(fetch_market, "_get_status", r), \
         patch.object(fetch_market.requests, "get", side_effect=fake_get):
        out = fetch_market.community_dev_stats(now=NOW, pace_s=0)
    btc = out["coins"]["btc"]
    assert btc["coingecko_watchlist_users"] == 2_000_000
    assert btc["github_repo"] == "bitcoin/bitcoin" and btc["github_stars"] == 80_000
    assert btc["github_open_issues_and_prs"] == 700 and btc["github_commits_30d"] == 212
    assert btc["sources"] == ["CoinGecko", "GitHub"]
    # Fields with no free source are NOT emitted (never zero-filled) and the
    # payload says why.
    for gone in ("twitter_followers", "reddit_subscribers", "reddit_active_users"):
        assert gone not in btc and gone in out["unavailable"]
    assert out["coingecko_calls"] == 4 and out["observed_date"] == "2026-10-05"
    # The Actions token rides only to api.github.com.
    assert all(h.get("Authorization") == "Bearer ghs_test_token"
               for u, h in seen_headers if u.startswith("https://api.github.com/"))
    assert fetch_market._github_headers("https://example.com/x").get("Authorization") is None


def test_community_dev_stats_runs_once_per_utc_day():
    r = _community_router()
    with patch.object(fetch_market, "_get_status", r), \
         patch.object(fetch_market.requests, "get", return_value=_gh_commits_response(body=[])):
        first = fetch_market.community_dev_stats(now=NOW, pace_s=0)
        n = len(r.calls)
        again = fetch_market.community_dev_stats(now=NOW + timedelta(hours=5), pace_s=0)
    assert len(r.calls) == n, "second run the same UTC day must not call anything"
    assert again["cache"] == "today" and again["coingecko_calls"] == 0
    assert again["fetched_at"] == first["fetched_at"]                # true data age
    assert first["coins"]["eth"]["github_commits_30d"] == 0          # empty list, no Link


def test_community_dev_stats_omits_a_coin_no_source_answered():
    with patch.object(fetch_market, "_get_status", _Router([])):
        out = fetch_market.community_dev_stats(now=NOW, pace_s=0)
    assert out["available"] is False and out["coins"] == {}


# --------------------------------------------------------------------------
# headline sentiment (Google News RSS, scored by Alpine Data)
# --------------------------------------------------------------------------

RSS = b"""<rss><channel>
<item><title>Bitcoin hits record high as ETF inflows surge - CoinDesk</title>
<link>https://news.google.com/rss/articles/a</link><pubDate>Mon, 05 Oct 2026 08:00:00 GMT</pubDate>
<source url="https://www.coindesk.com">CoinDesk</source></item>
<item><title>Exchange hack drains bitcoin wallets - Yahoo Finance</title>
<link>https://news.google.com/rss/articles/b</link><pubDate>Sat, 03 Oct 2026 12:00:00 GMT</pubDate>
<source url="https://finance.yahoo.com">Yahoo Finance</source></item>
<item><title>Bitcoin developers discuss mempool policy - Decrypt</title>
<link>https://news.google.com/rss/articles/c</link><pubDate>Sun, 27 Sep 2026 12:00:00 GMT</pubDate>
<source url="https://decrypt.co">Decrypt</source></item>
<item><title></title><link>https://x</link></item>
</channel></rss>"""


def _rss_response(body=RSS, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.content = body
    return resp


def test_google_news_rss_parses_and_strips_publisher_suffix():
    with patch.object(fetch_market.requests, "get", return_value=_rss_response()) as g:
        status, items = fetch_market.google_news_rss('"Bitcoin" crypto when:7d')
    assert status == 200 and len(items) == 3
    assert items[1]["title"] == "Exchange hack drains bitcoin wallets"
    assert items[1]["source"] == "Yahoo Finance"
    assert items[0]["date"] == "2026-10-05 08:00"
    assert g.call_args.args[0] == "https://news.google.com/rss/search"
    assert "Authorization" not in g.call_args.kwargs["headers"]


def test_summarize_headlines_counts_trend_and_keywords():
    with patch.object(fetch_market.requests, "get", return_value=_rss_response()):
        _s, items = fetch_market.google_news_rss("q")
    out = fetch_market.summarize_headlines(items, aliases={"bitcoin", "btc"}, now=NOW)
    assert (out["article_count"], out["positive"], out["negative"], out["neutral"]) == (3, 1, 1, 1)
    assert out["net_score"] == 0 and out["sample_capped"] is False
    # "Yahoo Finance" was stripped, so "fine" never matched.
    assert [a["sentiment"] for a in out["top_articles"]] == ["POSITIVE", "NEGATIVE", "NEUTRAL"]
    trend = {d["date"]: d for d in out["trend_7d"]}
    assert len(out["trend_7d"]) == 7 and "2026-09-27" not in trend   # outside the 7-day window
    assert trend["2026-10-05"]["pos"] == 1 and trend["2026-10-03"]["neg"] == 1
    assert trend["2026-10-04"] == {"date": "2026-10-04", "pos": 0, "neg": 0, "neu": 0, "net": 0}
    kws = {k["kw"] for k in out["top_keywords"]}
    assert "bitcoin" not in kws and "the" not in kws and "hack" in kws


def test_headline_sentiment_payload_carries_its_method():
    with patch.object(fetch_market.requests, "get", return_value=_rss_response()):
        out = fetch_market.headline_sentiment(now=NOW, pace_s=0)
    assert set(out["coins"]) == {"btc", "eth", "link", "ltc"}
    m = out["method"]
    assert m["computed_by"].startswith("Alpine Data")
    assert "Not CoinDesk or CryptoCompare labels" in m["not"] and "not an LLM" in m["not"]
    assert m["lexicon"]["positive"] and m["lexicon"]["negative"]
    assert out["coins"]["btc"]["query"] == '"Bitcoin" crypto when:7d'


def _js_list(src: str, name: str) -> list[str]:
    body = src.split(f"const {name} = [", 1)[1].split("];", 1)[0]
    return re.findall(r"'([^']*)'", body)


@pytest.mark.parametrize("rel", ["app.py", "v2/app.py"])
def test_js_and_python_keyword_lists_and_rule_are_identical(rel):
    """The frontend ADDS the backend's Google News counts to its own RSS
    counts, so both must score with the same lists and the same rule."""
    src = (ROOT / rel).read_text()
    assert _js_list(src, "_NEWS_POS_KEYWORDS") == list(fetch_market._NEWS_POS_KEYWORDS_PER_COIN)
    assert _js_list(src, "_NEWS_NEG_KEYWORDS") == list(fetch_market._NEWS_NEG_KEYWORDS_PER_COIN)
    fn = src.split("function scoreNewsItemSentiment(item){", 1)[1].split("\n}\n", 1)[0]
    assert "_NEWS_POS_RE.test(text)" in fn and "indexOf" not in fn   # whole words, not substrings


# --------------------------------------------------------------------------
# dashboards: no CryptoCompare in the browser, no key in the browser
# --------------------------------------------------------------------------

@pytest.mark.parametrize("rel", ["app.py", "v2/app.py"])
def test_browser_lookup_is_keyless_and_free(rel):
    src = (ROOT / rel).read_text()
    fn_start = src.index("// --- live crypto helpers (cache-miss fallback for lookupSymbol) ---")
    fn = src[fn_start: src.index("async function liveCryptoLookup(symbol){", fn_start) + 1200]
    for host in ("api.coingecko.com/api/v3/search", "/market_chart",
                 "api.exchange.coinbase.com", "api.kraken.com", "api.binance.us"):
        assert host in fn, host
    assert "cryptocompare.com" not in fn and "histoday?" not in fn
    assert "x-cg-" not in src and "COINGECKO_API_KEY" not in src     # never ship a key
    assert "api.binance.com" not in fn


@pytest.mark.parametrize("rel", ["app.py", "v2/app.py"])
def test_dashboards_read_the_new_fields_and_label_them(rel):
    src = (ROOT / rel).read_text()
    for old in ("social.cryptocompare", "social.cc_news", "(socialData().cryptocompare",
                "(socialData().cc_news", "cadli_btc", "live from CryptoCompare"):
        assert old not in src, f"{rel} still reads {old}"
    assert "socialData().community_dev" in src and "headline_sentiment" in src
    assert "Alpine Large-Cap Crypto Index" in src
    assert "Replaces the CoinDesk CADLI chart, which now needs a paid CoinDesk key" in src
    assert "COMPUTED BY ALPINE DATA" in src
    assert "<span class=\"tag\">CryptoCompare</span>" not in src
