"""Unit tests for the four newer fetcher/compute helpers in fetch_market.py.

Targets:
  - mempool_whale_transactions
  - poc_migration_series
  - blockchair_eth_stats
  - coin_metrics_eth_whale_metrics

All HTTP interaction is mocked via ``unittest.mock.patch`` on
``fetch_market._get`` — no live API calls.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

import fetch_market


# ============================================================================
# mempool_whale_transactions
# ============================================================================
#
# Helper: at btc_price_usd=100,000 USD/BTC and threshold=$1,000,000, the
# threshold in sats is 1e9 (i.e. 10 BTC).  A vout value of 2e9 (20 BTC) is
# ~$2M; 1.5e9 (15 BTC) is ~$1.5M; 5e8 (5 BTC) is ~$500k and below threshold.


def _tx(txid: str, max_vout_sats: int, *, coinbase: bool = False) -> dict:
    return {
        "txid": txid,
        "vin": [{"is_coinbase": coinbase}],
        "vout": [{"value": max_vout_sats}, {"value": max_vout_sats // 3}],
    }


def test_mempool_whale_zero_btc_price_short_circuits():
    """btc_price_usd == 0 must return [] without issuing any HTTP request."""
    with patch.object(fetch_market, "_get") as mget:
        out = fetch_market.mempool_whale_transactions(0)
        assert out == []
        mget.assert_not_called()


def test_mempool_whale_none_btc_price_short_circuits():
    """btc_price_usd is None: also returns [] with no HTTP."""
    with patch.object(fetch_market, "_get") as mget:
        out = fetch_market.mempool_whale_transactions(None)
        assert out == []
        mget.assert_not_called()


def test_mempool_whale_empty_blocks_returns_empty():
    """blocks endpoint returns [] -> output is []."""
    with patch.object(fetch_market, "_get", return_value=[]) as mget:
        out = fetch_market.mempool_whale_transactions(100_000.0)
        assert out == []
        # Only the blocks call should have been made.
        assert mget.call_count == 1


def test_mempool_whale_happy_path_filters_and_sorts():
    """Two blocks, several txs.

    Block A:
      - tx_big_a: max vout 2_000_000_000 sats (20 BTC ~ $2M)   -> included
      - tx_small: max vout   500_000_000 sats ( 5 BTC ~ $500k) -> excluded

    Block B:
      - tx_big_b: max vout 1_500_000_000 sats (15 BTC ~ $1.5M) -> included
    """
    blocks = [
        {"id": "blockhash_A", "height": 800001, "timestamp": 1_700_000_000},
        {"id": "blockhash_B", "height": 800002, "timestamp": 1_700_000_600},
    ]
    # A single page of 2 txs per block — since len(txs) < 25 the inner loop
    # breaks after the first page, so we don't need to keep returning data.
    txs_a = [
        _tx("tx_big_a", 2_000_000_000),
        _tx("tx_small", 500_000_000),
    ]
    txs_b = [
        _tx("tx_big_b", 1_500_000_000),
    ]

    # Sequence of _get responses:
    #   1) /v1/blocks
    #   2) /block/blockhash_A/txs/0
    #   3) /block/blockhash_B/txs/0
    responses = [blocks, txs_a, txs_b]

    def fake_get(url, *args, **kwargs):
        return responses.pop(0)

    with patch.object(fetch_market, "_get", side_effect=fake_get):
        out = fetch_market.mempool_whale_transactions(
            100_000.0, threshold_usd=1_000_000, n_blocks=2, max_per_block=25
        )

    # Two qualifying txs, sorted descending by USD value.
    assert len(out) == 2
    assert [t["txid"] for t in out] == ["tx_big_a", "tx_big_b"]
    assert out[0]["value_usd"] > out[1]["value_usd"]
    assert out[0]["value_btc"] == 20.0
    assert out[1]["value_btc"] == 15.0
    # Block metadata threads through correctly.
    assert out[0]["block_height"] == 800001
    assert out[1]["block_height"] == 800002


def test_mempool_whale_coinbase_excluded():
    """A coinbase tx above threshold must not appear in the output."""
    blocks = [{"id": "blockhash_X", "height": 900000, "timestamp": 1_700_001_000}]
    txs = [
        _tx("coinbase_tx", 5_000_000_000, coinbase=True),  # 50 BTC ~ $5M
        _tx("regular_big", 2_000_000_000),                  # 20 BTC ~ $2M
    ]
    responses = [blocks, txs]

    def fake_get(url, *args, **kwargs):
        return responses.pop(0)

    with patch.object(fetch_market, "_get", side_effect=fake_get):
        out = fetch_market.mempool_whale_transactions(
            100_000.0, threshold_usd=1_000_000, n_blocks=1, max_per_block=25
        )

    txids = [t["txid"] for t in out]
    assert "coinbase_tx" not in txids
    assert "regular_big" in txids
    assert len(out) == 1


# ============================================================================
# poc_migration_series
# ============================================================================


def _series(values: list[float], start_day: int = 1) -> list[dict]:
    """Build a [{date, value}] series with sequential YYYY-MM-DD dates."""
    out = []
    for i, v in enumerate(values):
        # Use a wide date range so day-of-month math stays simple.
        day = start_day + i
        # Force into 2024 with proper zero-padding; cap below 365 to be safe.
        out.append({"date": f"2024-{((day - 1) // 28) + 1:02d}-{((day - 1) % 28) + 1:02d}",
                    "value": v})
    return out


def test_poc_migration_series_insufficient_data_returns_empty():
    """Fewer than window_days + 1 common points -> []."""
    prices = _series([100.0] * 20)
    volumes = _series([1000.0] * 20)
    out = fetch_market.poc_migration_series(prices, volumes,
                                            lookback_days=90, window_days=30)
    assert out == []


def test_poc_migration_series_empty_inputs_returns_empty():
    """Empty inputs -> []."""
    assert fetch_market.poc_migration_series([], [], 90, 30) == []
    assert fetch_market.poc_migration_series(
        _series([100.0] * 50), [], 90, 30) == []


def test_poc_migration_series_output_length_matches_formula():
    """For N common points >= window_days+1, output length =
    min(lookback_days, N - window_days + 1)."""
    # 100 points, window=30, lookback=90 -> expect min(90, 71) = 71.
    prices = [{"date": f"2024-{i//28 + 1:02d}-{i%28 + 1:02d}", "value": 100.0 + i * 0.1}
              for i in range(100)]
    volumes = [{"date": p["date"], "value": 1000.0} for p in prices]
    out = fetch_market.poc_migration_series(prices, volumes,
                                            lookback_days=90, window_days=30)
    assert len(out) == 71

    # 50 points, window=30, lookback=90 -> expect min(90, 21) = 21.
    prices2 = [{"date": f"2024-{i//28 + 1:02d}-{i%28 + 1:02d}", "value": 100.0 + i * 0.1}
               for i in range(50)]
    volumes2 = [{"date": p["date"], "value": 1000.0} for p in prices2]
    out2 = fetch_market.poc_migration_series(prices2, volumes2,
                                             lookback_days=90, window_days=30)
    assert len(out2) == 21


def test_poc_migration_series_flat_data_constant_poc():
    """Near-flat price + uniform volume: rolling POC should be (nearly) constant.

    A truly identical price across the window makes hi == lo, in which case
    poc_migration_series skips that window. So oscillate within a small band so
    hi > lo but the centroid stays in the same bin.
    """
    # Alternate between 100.0 and 100.5 -> the volume is split evenly across
    # two bins each window, but the same bins are visited every window, so
    # the chosen POC bin (whichever wins by tie-break) is stable.
    n = 60
    prices = [{"date": f"2024-{i//28 + 1:02d}-{i%28 + 1:02d}",
               "value": 100.0 if i % 2 == 0 else 100.5}
              for i in range(n)]
    volumes = [{"date": p["date"], "value": 1000.0} for p in prices]
    out = fetch_market.poc_migration_series(prices, volumes,
                                            lookback_days=90, window_days=30,
                                            bins=10)
    assert len(out) > 0
    pocs = {round(row["poc"], 2) for row in out}
    # All windows see the same two price levels, so POC stays constant.
    assert len(pocs) == 1


def test_poc_migration_series_bins_param_respected():
    """All output POCs must lie within the window's price range."""
    # Increasing prices so each window has a different [lo, hi].
    n = 60
    prices = [{"date": f"2024-{i//28 + 1:02d}-{i%28 + 1:02d}",
               "value": 100.0 + i * 0.5}
              for i in range(n)]
    volumes = [{"date": p["date"], "value": 1000.0 + i * 10} for i, p in enumerate(prices)]
    out = fetch_market.poc_migration_series(prices, volumes,
                                            lookback_days=90, window_days=30,
                                            bins=20)
    assert len(out) > 0
    global_lo = min(p["value"] for p in prices)
    global_hi = max(p["value"] for p in prices)
    for row in out:
        # POC must be within the overall observed price range.
        assert global_lo <= row["poc"] <= global_hi


# ============================================================================
# blockchair_eth_stats
# ============================================================================


def test_blockchair_eth_stats_missing_data_key_returns_empty():
    """Response with no `data` key -> {}."""
    with patch.object(fetch_market, "_get", return_value={"context": {"code": 200}}):
        out = fetch_market.blockchair_eth_stats()
    assert out == {}


def test_blockchair_eth_stats_get_returns_none_returns_empty():
    """_get returns None (network failure) -> {}."""
    with patch.object(fetch_market, "_get", return_value=None):
        out = fetch_market.blockchair_eth_stats()
    assert out == {}


def test_blockchair_eth_stats_wei_conversions():
    """Verify wei->ETH conversions and that string scientific notation works."""
    payload = {
        "data": {
            "blocks_24h": 7200,
            "transactions_24h": 1_200_000,
            # Real Blockchair shape: wei strings (0.0021 ETH fee, 0.42 ETH value).
            "average_transaction_fee_24h": "2100000000000000",
            "average_transaction_value_24h": "420000000000000000",
            "circulation_approximate": "120e24",  # 120e24 wei = 120e6 ETH
            "burned": "5e24",                       # 5e24 wei = 5e6 ETH
            "burned_24h": "1e21",                   # 1e21 wei = 1000 ETH
            "inflation_24h": "2e21",                # 2e21 wei = 2000 ETH
            "market_price_usd": 3500.0,
            "layer_2": {
                "erc_20":  {"transactions_24h": 500_000},
                "erc_721": {"transactions_24h":  12_000},
            },
            "largest_transaction_24h": {
                "hash": "0xdeadbeef",
                "value_usd": 50_000_000.0,
            },
        }
    }
    with patch.object(fetch_market, "_get", return_value=payload):
        out = fetch_market.blockchair_eth_stats()

    assert out["supply_eth"] == 120e24 / 1e18  # i.e. 1.2e8
    assert out["burned_eth_total"] == 5e24 / 1e18
    assert out["burned_eth_24h"] == 1e21 / 1e18
    assert out["inflation_eth_24h"] == 2e21 / 1e18
    assert out["blocks_24h"] == 7200
    assert out["transactions_24h"] == 1_200_000
    assert out["market_price_usd"] == 3500.0
    assert out["erc20_transactions_24h"] == 500_000
    assert out["erc721_transactions_24h"] == 12_000
    assert out["largest_tx_24h"] == {"hash": "0xdeadbeef", "value_usd": 50_000_000.0}
    assert "fetched_at" in out
    # Fee/value are wei on the wire and must leave as ETH floats.
    assert out["avg_tx_fee_eth_24h"] == pytest.approx(0.0021)
    assert out["avg_tx_value_eth_24h"] == pytest.approx(0.42)
    assert out["transfer_volume_24h_usd"] == pytest.approx(1_200_000 * 0.42 * 3500.0)


def test_blockchair_eth_stats_avg_fee_is_eth_not_wei():
    """Regression: the live payload carried avg_tx_fee_eth_24h =
    "88120340117212" (a raw wei string). It must be a float in ETH — about
    0.000088 ETH, i.e. ~$0.24 at $2,700 — never a string, never >= 1 ETH."""
    payload = {"data": {
        "blocks_24h": 7178, "transactions_24h": 1510696,
        "average_transaction_fee_24h": "88120340117212",
        "average_transaction_value_24h": None,
        "market_price_usd": 2705.73,
    }}
    with patch.object(fetch_market, "_get", return_value=payload):
        out = fetch_market.blockchair_eth_stats()
    fee = out["avg_tx_fee_eth_24h"]
    assert isinstance(fee, float)
    assert fee == pytest.approx(88120340117212 / 1e18)
    assert 0 < fee < 1
    assert fee * 2705.73 == pytest.approx(0.2384, abs=1e-3)
    assert out["avg_tx_value_eth_24h"] is None
    assert out["transfer_volume_24h_usd"] is None


def test_blockchair_eth_stats_missing_largest_tx_still_populates():
    """Missing largest_transaction_24h => largest_tx_24h is None but the rest
    of the fields are still filled in."""
    payload = {
        "data": {
            "blocks_24h": 7200,
            "transactions_24h": 1_000_000,
            "circulation_approximate": "120000000000000000000000000",  # 1.2e26 wei
            "burned": None,
            "burned_24h": None,
            "inflation_24h": "0",
            "layer_2": {},
            # no largest_transaction_24h
        }
    }
    with patch.object(fetch_market, "_get", return_value=payload):
        out = fetch_market.blockchair_eth_stats()

    assert out["largest_tx_24h"] is None
    assert out["blocks_24h"] == 7200
    assert out["transactions_24h"] == 1_000_000
    assert out["supply_eth"] == 1.2e26 / 1e18  # i.e. 1.2e8
    # _wei_to_eth returns None for None/empty.
    assert out["burned_eth_total"] is None
    assert out["burned_eth_24h"] is None
    # inflation_eth_24h defaults to 0.0 when _wei_to_eth returns falsy.
    # ("0" makes _wei_to_eth return None; then `or 0.0` -> 0.0)
    assert out["inflation_eth_24h"] == 0.0
    # layer_2 empty => erc20/erc721 transaction counts are None.
    assert out["erc20_transactions_24h"] is None
    assert out["erc721_transactions_24h"] is None


# ============================================================================
# coin_metrics_eth_whale_metrics
# ============================================================================


def test_coin_metrics_eth_whale_metrics_get_returns_none():
    """The underlying fetch returns None (network failure) -> {}.

    Note: coin_metrics_eth_whale_metrics uses ``_coin_metrics_get`` (a
    Coin-Metrics-specific wrapper that injects an Authorization header) rather
    than the generic ``_get``. We patch that wrapper directly.
    """
    with patch.object(fetch_market, "_coin_metrics_get", return_value=None):
        out = fetch_market.coin_metrics_eth_whale_metrics()
    assert out == {}


def test_coin_metrics_eth_whale_metrics_partial_metrics():
    """Mock 2 of 3 metrics populated. The missing metric should be ABSENT from
    the output (not present as None or empty list).

    TxTfrValAdjUSD was dropped from the metrics list in 815238e / followup
    because it's paid-only and poisons the whole batch with 403 when free
    callers request it. Only AdrActCnt / TxCnt / SplyCur are requested now."""
    payload = {
        "data": [
            {
                "time": "2025-01-01T00:00:00.000Z",
                "AdrActCnt": "500000",
                "TxCnt": "1200000",
                # SplyCur intentionally missing
            },
            {
                "time": "2025-01-02T00:00:00.000Z",
                "AdrActCnt": "510000",
                "TxCnt": "1250000",
                # SplyCur intentionally missing
            },
        ]
    }
    with patch.object(fetch_market, "_coin_metrics_get", return_value=payload):
        out = fetch_market.coin_metrics_eth_whale_metrics()

    assert "AdrActCnt" in out
    assert "TxCnt" in out
    assert "SplyCur" not in out  # missing metric absent, not None / not [].
    assert "fetched_at" in out

    assert len(out["AdrActCnt"]) == 2
    assert out["AdrActCnt"][0] == {"date": "2025-01-01", "value": 500_000.0}
    assert out["TxCnt"][1] == {"date": "2025-01-02", "value": 1_250_000.0}


# ============================================================================
# fetch_headline_sentiment_by_coin + _score_news_item_sentiment
# ============================================================================
#
# The Research-tab "Top-25 news sentiment" card used to rely solely on the 5
# RSS feeds in `crypto_news_rss`, which only name ~14 of the top-25 coins. It
# then added CryptoCompare's per-coin news list, which needs a paid key since
# 2026-10. fetch_headline_sentiment_by_coin now runs one Google News RSS search
# per coin, scores the headlines with the same POS/NEG keyword lists the
# frontend uses for RSS items (Alpine Data's own rule), drops headlines already
# in the publisher-feed corpus, and emits per-coin aggregated counts.
#
# All HTTP is mocked via `unittest.mock.patch` on `fetch_market.requests.get`.

from unittest.mock import MagicMock


def _gnews_rss(titles: list[tuple[str, str]]) -> bytes:
    """Google News-shaped RSS: (title, publisher) pairs; title carries the
    " - Publisher" suffix exactly like the real feed."""
    items = "".join(
        f"<item><title>{t} - {src}</title><link>https://news.google.com/rss/articles/{i}</link>"
        f"<pubDate>Mon, 05 Oct 2026 0{i % 10}:00:00 GMT</pubDate>"
        f'<source url="https://example.com">{src}</source></item>'
        for i, (t, src) in enumerate(titles))
    return f"<rss><channel>{items}</channel></rss>".encode()


def _mock_gnews_response(titles, status_code: int = 200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = _gnews_rss(titles)
    return resp


def test_score_news_item_sentiment_positive_only_returns_positive():
    """Title with a positive keyword and no negative → POSITIVE."""
    item = {"title": "Bitcoin sees record inflows as ETF approval lifts price"}
    assert fetch_market._score_news_item_sentiment(item) == "POSITIVE"


def test_score_news_item_sentiment_negative_only_returns_negative():
    """Negative keyword in body, no positives → NEGATIVE."""
    item = {"title": "Market update", "body": "Major exchange suffers hack and outflows"}
    assert fetch_market._score_news_item_sentiment(item) == "NEGATIVE"


def test_score_news_item_sentiment_both_returns_neutral():
    """Both positive and negative keywords present → NEUTRAL (matches JS)."""
    item = {"title": "Crypto rally cools as exchange faces SEC probe"}
    assert fetch_market._score_news_item_sentiment(item) == "NEUTRAL"


def test_score_news_item_sentiment_neither_returns_neutral():
    """No keywords at all → NEUTRAL."""
    item = {"title": "Conference attendance grows year over year"}
    assert fetch_market._score_news_item_sentiment(item) == "NEUTRAL"


def test_score_news_item_sentiment_matches_whole_words_only():
    """Substring matching used to score "against" as a gain, "finance" as a
    fine, "path" as an ATH and "window" as a win. Whole words only now."""
    for title in ("Analysts bet against the dollar on Yahoo Finance",
                  "Developers outline a path through the next window",
                  "Download the winter report"):
        assert fetch_market._score_news_item_sentiment({"title": title}) == "NEUTRAL", title
    assert fetch_market._score_news_item_sentiment({"title": "SEC delays decision"}) == "NEGATIVE"
    assert fetch_market._score_news_item_sentiment({"title": "Price hits all-time high"}) == "POSITIVE"


def test_headline_sentiment_by_coin_aggregates_pos_neg_neu_correctly():
    """Three headlines for one coin — one positive, one negative, one
    neutral — aggregate into pos=1, neg=1, neu=1, net=0, and the publisher
    suffix never reaches the scorer."""
    coins = [{"symbol": "TON", "name": "Toncoin"}]
    titles = [("Toncoin sees record inflows after partnership", "Decrypt"),
              ("Toncoin validator hack drains funds", "CoinDesk"),
              ("Toncoin quarterly report released", "Yahoo Finance")]
    with patch.object(fetch_market.requests, "get",
                      return_value=_mock_gnews_response(titles)) as g:
        out = fetch_market._fetch_headline_sentiment_by_coin_impl(coins, sleep_between=0)
    assert g.call_args.kwargs["params"]["q"] == '"Toncoin" crypto when:7d'
    assert out["available"] is True
    row = out["coins"]["TON"]
    assert (row["total"], row["positive"], row["negative"], row["neutral"],
            row["net_score"]) == (3, 1, 1, 1, 0)
    assert row["symbol"] == "TON" and row["name"] == "Toncoin"
    assert len(row["recent"]) == 3
    assert {r["sentiment"] for r in row["recent"]} == {"POSITIVE", "NEGATIVE", "NEUTRAL"}
    assert {r["source"] for r in row["recent"]} == {"Decrypt", "CoinDesk", "Yahoo Finance"}
    assert all(" - " not in r["title"] for r in row["recent"])
    assert out["method"]["computed_by"].startswith("Alpine Data")


def test_headline_sentiment_by_coin_drops_headlines_already_in_rss(tmp_path, monkeypatch):
    """The frontend ADDS these counts to its own RSS counts, so a headline
    that is also in market.news must not be counted here."""
    monkeypatch.setattr(fetch_market, "_STALE_DIR", tmp_path / ".stale")
    monkeypatch.setattr(fetch_market.time, "sleep", lambda *_a: None)
    coins = [{"symbol": "BTC", "name": "Bitcoin"}]
    titles = [("Bitcoin rally continues", "CoinDesk"), ("Bitcoin miners sell", "The Block")]
    news = [{"title": "Bitcoin rally continues"}]
    with patch.object(fetch_market.requests, "get",
                      return_value=_mock_gnews_response(titles)):
        out = fetch_market.fetch_headline_sentiment_by_coin(coins, 25, news)
    row = out["coins"]["BTC"]
    assert row["total"] == 1 and row["excluded_duplicates"] == 1
    assert row["recent"][0]["title"] == "Bitcoin miners sell"


def test_headline_sentiment_by_coin_skips_failed_coin():
    """A non-200 for one coin is skipped; the rest still score."""
    coins = [{"symbol": "FIGR_HELOC", "name": "Figure Heloc"},
             {"symbol": "BTC", "name": "Bitcoin"}]
    bad = _mock_gnews_response([], status_code=503)
    good = _mock_gnews_response([("Bitcoin rally continues amid record inflows", "Src")])
    with patch.object(fetch_market.requests, "get", side_effect=[bad, good]):
        out = fetch_market._fetch_headline_sentiment_by_coin_impl(coins, sleep_between=0)
    assert "FIGR_HELOC" not in out["coins"]
    assert out["coins"]["BTC"]["positive"] == 1


def test_headline_sentiment_by_coin_empty_feed_omits_coin():
    """No headlines → the coin is omitted (frontend checks `if (rows[sym])`)
    and the result says unavailable rather than reporting zeros."""
    coins = [{"symbol": "XYZ", "name": "Nothing"}]
    with patch.object(fetch_market.requests, "get",
                      return_value=_mock_gnews_response([])):
        out = fetch_market._fetch_headline_sentiment_by_coin_impl(coins, sleep_between=0)
    assert out["available"] is False
    assert out["coins"] == {}


def test_headline_sentiment_by_coin_sends_no_credentials(monkeypatch):
    """Google News is keyless: no Authorization header, ever (the old CC
    fetcher sent CRYPTOCOMPARE_API_KEY; nothing reads it now)."""
    monkeypatch.setenv("CRYPTOCOMPARE_API_KEY", "should-not-travel")
    captured: dict = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured.update(url=url, params=params, headers=headers)
        return _mock_gnews_response([])

    with patch.object(fetch_market.requests, "get", side_effect=fake_get):
        fetch_market._fetch_headline_sentiment_by_coin_impl(
            [{"symbol": "BTC", "name": "Bitcoin"}], sleep_between=0)
    assert captured["url"] == fetch_market.GOOGLE_NEWS_RSS
    assert "Authorization" not in captured["headers"]
    assert "should-not-travel" not in repr(captured)


def test_headline_sentiment_by_coin_caps_to_top_n():
    """The public wrapper only fans out to the first N entries of markets_top."""
    coins = [{"symbol": f"S{i}", "name": f"Coin{i}"} for i in range(40)]
    queries: list[str] = []

    def fake_get(url, params=None, headers=None, timeout=None):
        queries.append(params.get("q"))
        return _mock_gnews_response([])

    with patch.object(fetch_market.requests, "get", side_effect=fake_get), \
         patch.object(fetch_market.time, "sleep"), \
         patch.object(fetch_market, "_stale_load", return_value=None):
        out = fetch_market.fetch_headline_sentiment_by_coin(coins, top_n=25)
    assert len(queries) == 25
    assert queries[0].startswith('"Coin0"') and queries[-1].startswith('"Coin24"')
    assert out["available"] is False


# ============================================================================
# _stale_path — cache keys can embed upstream symbols/ids; the resulting
# path must always be a single file directly inside the stale dir.
# ============================================================================


def test_stale_path_stays_inside_stale_dir(monkeypatch, tmp_path):
    stale_dir = tmp_path / ".stale"
    monkeypatch.setattr(fetch_market, "_STALE_DIR", stale_dir)
    assert (fetch_market._stale_path("cryptocompare_market_BTC")
            == stale_dir / "cryptocompare_market_BTC.json")
    for key in ("../../etc/passwd", "a/b\\c", "..", "", "/abs/path", "x/../../y"):
        p = fetch_market._stale_path(key)
        assert p.parent == stale_dir, key
        assert p.name.endswith(".json"), key
