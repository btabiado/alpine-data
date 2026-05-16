"""Tests for the rule-based insights engine and its per-tab tagging."""
from __future__ import annotations

import insights


# ---------- per-tab tagging ----------

def test_etf_insights_tagged_etf():
    """Anything coming out of _etf_insights should end up on the ETF tab."""
    payload = {
        "btc": {
            "daily": [
                {"date": "2024-01-10", "flow": 100.0, "cumulative": 100.0},
                {"date": "2024-01-11", "flow": -50.0, "cumulative": 50.0},
            ],
            "stats": {"all_time": 50.0},
            "by_fund_daily": {},
        },
        "eth": {},
        "market": {},
        "signals": {},
    }
    out = insights.build_insights(payload)
    etf = [i for i in out if i.get("kind") == "etf" or i.get("headline", "").startswith("BTC ETF")]
    assert etf, "expected at least one ETF-flow insight"
    for i in etf:
        assert i["tab"] == "etf", f"expected tab=etf but got {i.get('tab')!r} for {i.get('headline')!r}"


def test_signal_insights_tagged_signals():
    """STRONG BUY/SELL and signal flips belong on the Signals tab."""
    payload = {
        "btc": {},
        "eth": {},
        "market": {},
        "signals": {
            "btc": {
                "label": "STRONG BUY",
                "score": 65,
                "components": [{"name": "RSI", "contribution": 15},
                               {"name": "MACD", "contribution": 10},
                               {"name": "Funding", "contribution": 10}],
                "history": [{"score": -10}, {"score": 65}],
            }
        },
    }
    out = insights.build_insights(payload)
    sig = [i for i in out if i.get("kind") == "signal"]
    assert sig, "expected at least one signal insight"
    for i in sig:
        assert i["tab"] == "signals", f"expected tab=signals for {i.get('headline')!r}"


def test_market_insight_tab_classifier_known_patterns():
    """Headline-based market insight classifier must hit every known tab."""
    f = insights._market_insight_tab
    assert f({"headline": "Fear & Greed at 18 — extreme fear (contrarian buy zone)"}) == "trading"
    assert f({"headline": "BTC funding flipped negative (-0.0010%)"}) == "trading"
    assert f({"headline": "BTC DVOL crushed (-1.8σ vs 30d mean)"}) == "trading"
    assert f({"headline": "ETH/BTC at ~6-month low (0.04212)"}) == "trading"

    assert f({"headline": "ETH gas spike: base fee 88 gwei"}) == "defi"
    assert f({"headline": "ETH gas near zero (0.40 gwei)"}) == "defi"
    assert f({"headline": "Stablecoin supply +$2.10B over the last 7d"}) == "defi"
    assert f({"headline": "DEX 24h volume: $6.20B  ·  protocol fees: $48.5M"}) == "defi"
    assert f({"headline": "Base TVL +5.3% today ($2.1B)"}) == "defi"

    assert f({"headline": "BTC mempool congested: 120 sat/vB fastest fee"}) == "whale"
    assert f({"headline": "BTC mempool quiet (2 sat/vB)"}) == "whale"
    assert f({"headline": "BTC hashrate at 30-day high (640 EH/s)"}) == "whale"
    assert f({"headline": "BTC difficulty retarget in ~2.1 days: +4.8% (harder for miners)"}) == "whale"
    assert f({"headline": "BTC mining concentration high: top 2 pools = 58.0% of blocks"}) == "whale"
    assert f({"headline": "BTC on-chain transfer volume spike: Whale tx volume +2.4σ vs 30d mean"}) == "whale"
    assert f({"headline": "BTC active addresses +1.8σ vs 30d"}) == "whale"

    # Trading: open interest + long/short crowding
    assert f({"headline": "BTC open interest +2.1σ above 30d mean"}) == "trading"
    assert f({"headline": "ETH L/S ratio crowded long (2.85)"}) == "trading"
    assert f({"headline": "LINK L/S ratio crowded short (0.55)"}) == "trading"

    assert f({"headline": "DXY +1.2% today — typically inverse to risk assets including crypto"}) == "markets"
    assert f({"headline": "10Y Treasury yield crossed above 5.0% (5.02%)"}) == "markets"
    assert f({"headline": "Gold at 30-day high ($2,420.50/oz)"}) == "markets"
    assert f({"headline": "S&P 500 -2.4% today — risk-off may pressure crypto"}) == "markets"
    assert f({"headline": "📰 CoinDesk: Some headline goes here"}) == "markets"
    assert f({"headline": "ZANO (Zano) is trending #1 on CoinGecko"}) == "markets"
    assert f({"headline": "BTC price divergence: CoinGecko $79,200 vs Coinbase $79,500"}) == "markets"
    assert f({"headline": "DEX hot pool: PEPE/WETH on ethereum +45% with $80M volume"}) == "markets"
    assert f({"headline": "NASDAQ +1.80% on the day"}) == "markets"
    assert f({"headline": "Dow Jones -2.10% on the day"}) == "markets"
    assert f({"headline": "VIX crossed above 20 (22.4) — calm→fear"}) == "markets"
    assert f({"headline": "VIX fell below 30 (28.1) — panic→fear"}) == "markets"
    # Top-25 movers + BTC dominance + market-cap milestones
    assert f({"headline": "Top-25 24h gainer: SOL +7.4% (rank #5)"}) == "markets"
    assert f({"headline": "Top-25 24h loser: ADA -6.1% (rank #11)"}) == "markets"
    assert f({"headline": "Top-25 7d momentum: AVAX +18.2% week (rank #14)"}) == "markets"
    assert f({"headline": "Top-25 7d laggard: DOT -16.4% week (rank #16)"}) == "markets"
    assert f({"headline": "BTC dominance high: 61.2% — alt season unlikely"}) == "markets"
    assert f({"headline": "BTC dominance low: 43.8% — alt rotation in play"}) == "markets"
    assert f({"headline": "Total crypto market cap above $4T (now $4.12T)"}) == "markets"


def test_market_insight_tab_classifier_falls_back_to_markets():
    f = insights._market_insight_tab
    assert f({"headline": "something nobody recognises"}) == "markets"
    assert f({"headline": ""}) == "markets"


def test_every_insight_has_a_valid_tab():
    """Black-box: whatever build_insights emits, every item must carry a tab
    in the allowed vocabulary so the JS filter never receives an unknown."""
    payload = {
        "btc": {
            "daily": [
                {"date": "2024-01-10", "flow": 200.0, "cumulative": 200.0},
                {"date": "2024-01-11", "flow": -50.0, "cumulative": 150.0},
            ],
            "stats": {"all_time": 150.0},
        },
        "eth": {
            "daily": [{"date": "2024-07-23", "flow": 5.0, "cumulative": 5.0}],
            "stats": {"all_time": 5.0},
        },
        "market": {
            "fear_greed": [{"value": 18, "label": "Extreme Fear"}],
            "btc": {
                "funding": [{"rate": 0.0005}, {"rate": -0.0010}],
                "price": [{"value": 43200}],
            },
            "eth_gas": {"base_fee_gwei": 0.4, "fast_gwei": 0.6},
            "mempool": {"fees_sat_vb": {"fastestFee": 2}},
            "defillama": {"stablecoin_7d_change_usd": 2_100_000_000, "stablecoin_mcap_usd": 180_000_000_000,
                          "dex_volume_24h_usd": 6_200_000_000, "fees_24h_usd": 48_500_000},
        },
        "signals": {
            "btc": {"label": "STRONG SELL", "score": -55,
                    "components": [{"name": "SMA50", "contribution": -20}],
                    "history": [{"score": 10}, {"score": -55}]},
        },
    }
    out = insights.build_insights(payload)
    assert out, "expected the seeded payload to produce at least one insight"
    for i in out:
        assert "tab" in i, f"insight missing tab: {i!r}"
        assert i["tab"] in insights.VALID_TABS, \
            f"insight tab {i['tab']!r} not in VALID_TABS for {i.get('headline')!r}"


# ---------- whale: network velocity rule ----------

def _whale_payload(velocity_series: list[float]) -> dict:
    """Build a payload whose tx_volume_usd / active_addresses ratio matches
    the provided ``velocity_series`` (one entry per day, oldest first).
    active_addresses is held constant so the ratio = tx_volume directly,
    which keeps the test math simple.
    """
    addrs = 1_000_000.0
    rows_vol = []
    rows_addr = []
    for i, v in enumerate(velocity_series):
        date = f"2024-01-{i+1:02d}"
        rows_vol.append({"date": date, "value": v * addrs})
        rows_addr.append({"date": date, "value": addrs})
    return {
        "btc": {}, "eth": {}, "market": {}, "signals": {},
        "whale": {"btc": {
            "tx_volume_usd": rows_vol,
            "active_addresses": rows_addr,
        }},
    }


def test_network_velocity_spike_fires_and_tagged_whale():
    """A flat ratio for 30d then a big jump on day 31 should trip the
    network-velocity-spike rule and the insight must be tagged tab='whale'."""
    series = [1000.0] * 30 + [5000.0]  # day 31 is a huge ratio spike
    payload = _whale_payload(series)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "network velocity" in i.get("headline", "").lower()]
    assert hits, "expected network velocity spike insight to fire"
    for i in hits:
        assert i["tab"] == "whale", f"expected tab=whale, got {i.get('tab')!r}"
        assert i["kind"] == "anomaly"
        assert i["asset"] == "btc"


def test_network_velocity_no_spike_when_flat():
    """A flat ratio (no variance, latest equals mean) must not emit the
    velocity-spike anomaly."""
    series = [1000.0] * 31
    payload = _whale_payload(series)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "network velocity" in i.get("headline", "").lower()]
    assert not hits, f"did not expect velocity spike for flat ratio, got {hits!r}"


def test_network_velocity_skipped_when_series_too_short():
    """Fewer than 31 daily ratios → zscore unavailable → rule must stay silent."""
    series = [1000.0] * 10 + [9999.0]  # latest spikes but only 11 days total
    payload = _whale_payload(series)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "network velocity" in i.get("headline", "").lower()]
    assert not hits, "rule should be silent with <31 daily ratios"


# ---------- AI News tab insights ----------

def _ainews_payload(
    *,
    summary: dict | None = None,
    items: list[dict] | None = None,
    stocks: list[dict] | None = None,
    curated: dict | None = None,
    available: bool = True,
) -> dict:
    """Build a minimal payload that exercises only the AI insight rules.

    Other generators (etf/signals/whale) receive empty structures so they
    don't emit anything that could clutter the assertions.
    """
    market = {}
    market["ai_news"] = {
        "available": available,
        "items": items or [],
        "summary": summary or {},
    }
    if stocks is not None:
        market["stocks_signals"] = stocks
    if curated is not None:
        market["ai_curated"] = curated
    return {
        "btc": {}, "eth": {},
        "market": market,
        "signals": {},
    }


def test_ainews_sentiment_skew_positive_fires_and_tagged_ainews():
    summary = {"positive": 38, "negative": 6, "neutral": 16, "total": 60,
               "net_score": 32, "sentiment_label": "POSITIVE"}
    payload = _ainews_payload(summary=summary)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "sentiment skews POSITIVE" in i.get("headline", "")]
    assert hits, f"expected POSITIVE sentiment-skew insight, got {[i['headline'] for i in out]!r}"
    for i in hits:
        assert i["tab"] == "ainews"
        assert i["severity"] == "good"


def test_ainews_sentiment_skew_negative_fires():
    summary = {"positive": 5, "negative": 28, "neutral": 12, "total": 45,
               "net_score": -23, "sentiment_label": "NEGATIVE"}
    payload = _ainews_payload(summary=summary)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "sentiment skews NEGATIVE" in i.get("headline", "")]
    assert hits
    for i in hits:
        assert i["tab"] == "ainews"
        assert i["severity"] == "bad"


def test_ainews_sentiment_skew_silent_when_total_too_low():
    """Below the 15-article floor the rule must stay silent (avoid noise)."""
    summary = {"positive": 6, "negative": 0, "neutral": 2, "total": 8,
               "net_score": 6, "sentiment_label": "POSITIVE"}
    payload = _ainews_payload(summary=summary)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "sentiment skews" in i.get("headline", "")]
    assert not hits


def test_ainews_volume_surge_fires_when_total_gte_50():
    summary = {"positive": 20, "negative": 18, "neutral": 16, "total": 54,
               "net_score": 2, "sentiment_label": "NEUTRAL"}
    payload = _ainews_payload(summary=summary)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "AI news flow heavy" in i.get("headline", "")]
    assert hits, "expected volume-surge insight to fire at total=54"
    assert hits[0]["tab"] == "ainews"


def test_ainews_volume_surge_silent_below_threshold():
    summary = {"positive": 10, "negative": 10, "neutral": 10, "total": 30,
               "net_score": 0, "sentiment_label": "NEUTRAL"}
    payload = _ainews_payload(summary=summary)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "AI news flow heavy" in i.get("headline", "")]
    assert not hits


def test_ainews_source_dominance_fires_when_one_source_gt_40pct():
    items = (
        [{"title": f"tc {i}", "url": "u", "source": "TechCrunch AI"} for i in range(12)]
        + [{"title": f"v {i}", "url": "u", "source": "The Verge AI"} for i in range(5)]
        + [{"title": f"vb {i}", "url": "u", "source": "VentureBeat AI"} for i in range(4)]
        + [{"title": f"o {i}", "url": "u", "source": "OpenAI"} for i in range(4)]
    )
    summary = {"positive": 10, "negative": 8, "neutral": 7, "total": len(items),
               "net_score": 2, "sentiment_label": "NEUTRAL"}
    payload = _ainews_payload(summary=summary, items=items)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "AI news flow concentrated" in i.get("headline", "")]
    assert hits, f"expected source-dominance insight; got {[i['headline'] for i in out]!r}"
    assert "TechCrunch AI" in hits[0]["headline"]
    assert hits[0]["tab"] == "ainews"


def test_ainews_source_dominance_silent_when_spread_even():
    items = [{"title": f"t {i}", "url": "u", "source": s}
             for s in ("A", "B", "C", "D", "E") for i in range(5)]  # 25 items, 5/source
    summary = {"positive": 10, "negative": 8, "neutral": 7, "total": len(items),
               "net_score": 2, "sentiment_label": "NEUTRAL"}
    payload = _ainews_payload(summary=summary, items=items)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "AI news flow concentrated" in i.get("headline", "")]
    assert not hits


def test_ainews_top_ticker_strong_score_fires_for_ai_exposed_ticker():
    stocks = [
        {"symbol": "NVDA", "name": "Nvidia", "score": 62, "label": "STRONG BUY"},
        {"symbol": "GOOGL", "name": "Alphabet", "score": 10, "label": "HOLD"},
        {"symbol": "XYZ", "name": "Not AI", "score": 95, "label": "STRONG BUY"},  # filtered out
    ]
    payload = _ainews_payload(stocks=stocks)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if i.get("tab") == "ainews" and "NVDA" in i.get("headline", "")
            and "STRONG BUY" in i.get("headline", "")]
    assert hits, f"expected NVDA STRONG BUY ainews insight; got {[i['headline'] for i in out]!r}"
    assert hits[0]["asset"] == "NVDA"
    assert hits[0]["severity"] == "good"


def test_ainews_top_ticker_silent_when_no_strong_signal():
    stocks = [
        {"symbol": "NVDA", "name": "Nvidia", "score": 25, "label": "BUY"},
        {"symbol": "MSFT", "name": "Microsoft", "score": -10, "label": "HOLD"},
    ]
    payload = _ainews_payload(stocks=stocks)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if i.get("tab") == "ainews" and "AI-exposed ticker" in i.get("headline", "")]
    assert not hits


def test_ainews_ticker_flip_via_history_fires():
    """Sign flip from -40 → +45 across the 7d history window must trigger."""
    history = [{"date": f"2024-01-{d:02d}", "score": -40} for d in range(1, 8)]
    history.append({"date": "2024-01-08", "score": 45})
    stocks = [{"symbol": "AMD", "name": "Advanced Micro", "score": 45,
               "label": "BUY", "history": history}]
    payload = _ainews_payload(stocks=stocks)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if i.get("tab") == "ainews" and "AMD signal flipped" in i.get("headline", "")]
    assert hits, f"expected AMD flip insight; got {[i['headline'] for i in out]!r}"
    assert "positive" in hits[0]["headline"]


def test_ainews_ticker_flip_silent_when_near_zero():
    """A flip from -5 → +5 should NOT fire — too noisy near zero."""
    history = [{"date": f"2024-01-{d:02d}", "score": -5} for d in range(1, 8)]
    history.append({"date": "2024-01-08", "score": 5})
    stocks = [{"symbol": "AMD", "name": "Advanced Micro", "score": 5,
               "label": "HOLD", "history": history}]
    payload = _ainews_payload(stocks=stocks)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "AMD signal flipped" in i.get("headline", "")]
    assert not hits


def test_ainews_sentiment_price_divergence_fires():
    summary = {"positive": 30, "negative": 5, "neutral": 5, "total": 40,
               "net_score": 25, "sentiment_label": "POSITIVE"}
    stocks = [
        {"symbol": "NVDA", "name": "Nvidia", "score": -20, "label": "SELL"},
        {"symbol": "AMD",  "name": "AMD",    "score": -15, "label": "SELL"},
        {"symbol": "MSFT", "name": "MSFT",   "score": -10, "label": "HOLD"},
    ]
    payload = _ainews_payload(summary=summary, stocks=stocks)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "AI sentiment / price divergence" in i.get("headline", "")]
    assert hits, f"expected divergence insight; got {[i['headline'] for i in out]!r}"
    assert hits[0]["tab"] == "ainews"
    assert hits[0]["severity"] == "alert"


def test_ainews_mega_round_fires_for_fresh_billion_dollar_round():
    from datetime import datetime, timedelta
    recent_date = (datetime.utcnow() - timedelta(days=3)).strftime("%Y-%m-%d")
    curated = {
        "top_funded_companies": [
            {"name": "Anthropic", "valuation_usd": 185_000_000_000,
             "last_round_size_usd": 5_000_000_000,
             "last_round_date": recent_date,
             "last_round_stage": "Series F"},
            # Stale round — must be filtered out.
            {"name": "OldCo",     "valuation_usd": 10_000_000_000,
             "last_round_size_usd": 2_000_000_000,
             "last_round_date": "2024-01-01",
             "last_round_stage": "Series D"},
        ],
    }
    payload = _ainews_payload(curated=curated)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "AI mega-round" in i.get("headline", "")]
    assert hits, f"expected mega-round insight; got {[i['headline'] for i in out]!r}"
    assert "Anthropic" in hits[0]["headline"]
    assert hits[0]["tab"] == "ainews"
    assert hits[0]["severity"] == "good"
    assert "OldCo" not in hits[0]["headline"]


def test_ainews_mega_round_silent_when_round_too_small():
    from datetime import datetime, timedelta
    recent_date = (datetime.utcnow() - timedelta(days=2)).strftime("%Y-%m-%d")
    curated = {
        "top_funded_companies": [
            {"name": "SmallCo", "valuation_usd": 1_000_000_000,
             "last_round_size_usd": 100_000_000,
             "last_round_date": recent_date,
             "last_round_stage": "Series B"},
        ],
    }
    payload = _ainews_payload(curated=curated)
    out = insights.build_insights(payload, limit=100)
    hits = [i for i in out if "AI mega-round" in i.get("headline", "")]
    assert not hits


def test_ainews_rules_defensive_on_empty_payload():
    """A wholly empty market section must not crash the AI generator."""
    payload = {"btc": {}, "eth": {}, "market": {}, "signals": {}}
    out = insights._ainews_insights(payload)  # call generator directly
    assert out == []


def test_ainews_rules_defensive_on_malformed_summary():
    """Garbage in summary (None, strings) shouldn't raise."""
    payload = _ainews_payload(summary={"positive": None, "negative": "x", "total": "?",
                                       "net_score": None, "sentiment_label": None})
    out = insights._ainews_insights(payload)
    # No insight should fire, but no exception either.
    assert isinstance(out, list)


def test_ainews_tab_added_to_valid_tabs():
    """The VALID_TABS allowlist must include 'ainews' so the renderer accepts it."""
    assert "ainews" in insights.VALID_TABS


# ---------- regression: existing behaviour still holds ----------

def test_build_insights_respects_limit():
    """Verify the explicit limit argument truncates the output."""
    payload = {
        "btc": {
            "daily": [{"date": f"2024-01-{d:02d}", "flow": 100.0 if d % 2 else -50.0,
                       "cumulative": 0} for d in range(1, 31)],
            "stats": {"all_time": 1000.0},
        },
        "eth": {}, "market": {}, "signals": {},
    }
    out = insights.build_insights(payload, limit=3)
    assert len(out) <= 3
